# Autocomplete System — Data Collection & Aggregation Pipeline

> This document covers how query data flows from user searches to a deployable trie. The pipeline is the "write path" of the autocomplete system — it doesn't serve user requests but produces the artifact (the trie) that does. Understanding this pipeline is critical because **the quality of suggestions is directly determined by the quality of data collection and aggregation**.

---

## Table of Contents

1. [Query Log Collection](#1-query-log-collection)
2. [What to Log and What to Skip](#2-what-to-log-and-what-to-skip)
3. [Batch Aggregation Pipeline (Spark)](#3-batch-aggregation-pipeline-spark)
4. [Streaming Pipeline (Kafka + Flink)](#4-streaming-pipeline-kafka--flink)
5. [Trie Building Process](#5-trie-building-process)
6. [Trending Query Detection](#6-trending-query-detection)
7. [Freshness vs Accuracy Tradeoffs](#7-freshness-vs-accuracy-tradeoffs)
8. [Privacy and Data Retention](#8-privacy-and-data-retention)
9. [Pipeline Monitoring and Alerting](#9-pipeline-monitoring-and-alerting)
10. [Failure Modes and Recovery](#10-failure-modes-and-recovery)

---

## 1. Query Log Collection

Every time a user interacts with the search bar on Amazon, we generate a log event. These events are the raw material for building the autocomplete trie.

### Log Entry Schema

```
QueryLogEntry {
    // Core fields
    query_text: string              // "amazon prime video" — the actual search query
    timestamp: uint64               // Unix milliseconds: 1707350400000
    event_type: enum                // SEARCH_COMPLETED | SUGGESTION_CLICKED | SEARCH_ABANDONED

    // User context (anonymized)
    user_id: string                 // SHA-256 hash of actual user ID: "sha256:a1b2c3..."
    session_id: string              // "sess_abc123" — groups events in a single session
    device_type: enum               // MOBILE | DESKTOP | TABLET
    region: string                  // "us-east-1" | "eu-west-1" | "ap-northeast-1"
    language: string                // "en" | "es" | "ja"

    // Suggestion interaction
    suggestion_clicked: bool        // Did the user click an autocomplete suggestion?
    suggestion_position: int        // Which position was clicked (1-10, 0 if not clicked)
    suggestions_shown: List<string> // What suggestions were displayed (for CTR analysis)
    trie_version: string            // "v2026020814" — which trie version served these suggestions

    // Search outcome
    result_clicked: bool            // Did the user click a search result?
    purchase_made: bool             // Did the user buy something? (for conversion rate)
}
```

### Log Transport Architecture

```
┌──────────────┐     ┌────────────────┐     ┌──────────────┐
│ Amazon.com   │     │  Kinesis Data  │     │    Kafka     │
│ Search Bar   │────▶│   Firehose     │────▶│   Cluster    │
│ (client-side │     │  (buffering,   │     │              │
│  event)      │     │   batching)    │     │  Topic:      │
└──────────────┘     └────────────────┘     │  search-     │
                                            │  queries     │
┌──────────────┐     ┌────────────────┐     │              │
│ Amazon App   │     │  Kinesis Data  │     │  Partitions: │
│ (mobile)     │────▶│   Firehose     │────▶│  64          │
│              │     │                │     │  (by hash of │
└──────────────┘     └────────────────┘     │   user_id)   │
                                            └──────┬───────┘
                                                   │
                                    ┌──────────────┼──────────────┐
                                    │              │              │
                             ┌──────▼──────┐ ┌────▼────┐  ┌──────▼──────┐
                             │    S3       │ │  Flink  │  │  Other     │
                             │ (raw logs,  │ │(trending│  │ consumers  │
                             │  Parquet)   │ │ detect) │  │ (analytics)│
                             └─────────────┘ └─────────┘  └────────────┘
```

### Volume Estimates

| Metric | Value |
|---|---|
| Searches per day | ~5 billion |
| Log events per day (after filtering) | ~6 billion (includes suggestions shown, abandonments) |
| Events per second | ~70,000 |
| Average event size | ~200 bytes (JSON compressed) |
| Daily log volume | 6B × 200B = **~1.2 TB/day** |
| Kafka partition count | 64 (each partition handles ~1,100 events/sec) |
| S3 storage (30-day retention) | ~36 TB |

---

## 2. What to Log and What to Skip

Not every interaction should feed into the autocomplete trie. Logging too aggressively adds noise; logging too conservatively misses signals.

### Event Classification

| Event Type | Log? | Volume | Rationale |
|---|---|---|---|
| **Search completed** (user pressed Enter) | ✅ Yes (100%) | 5B/day | Primary signal — the user wanted this query |
| **Suggestion clicked** | ✅ Yes (100%) | ~2B/day | Strong signal — our suggestion was useful, include click position for ranking |
| **Search abandoned** (typed but didn't search) | ⚠️ Sample (10%) | ~500M (sampled) | Weak signal — might indicate user found suggestion, or gave up |
| **Intermediate keystrokes** | ❌ No | ~50B/day | Too noisy — every character generates an event. Not useful for trie building |
| **Bot/crawler traffic** | ❌ No (filter out) | ~500M/day | Not real user intent. Detected via User-Agent, rate patterns, IP reputation |
| **Internal/test queries** | ❌ No (filter out) | ~10M/day | Employee testing, load testing, etc. Filtered by internal IP ranges |

### Normalization (Applied Before Logging)

Before a query enters the pipeline, normalize it:

1. **Lowercase**: "Amazon Prime" → "amazon prime"
2. **Trim whitespace**: "  amazon prime  " → "amazon prime"
3. **Collapse multiple spaces**: "amazon  prime" → "amazon prime"
4. **Strip leading/trailing special characters**: "!amazon prime?" → "amazon prime"
5. **Unicode normalization**: NFC form to handle accented characters consistently

### Minimum Query Length

Queries shorter than 2 characters are excluded. Single-character queries ("a", "b") generate too many matches and aren't useful as autocomplete suggestions. They still trigger autocomplete lookups (the trie returns top-K for "a"), but they don't contribute to frequency counts.

---

## 3. Batch Aggregation Pipeline (Spark)

The batch pipeline is the primary data path. It processes the last 30 days of query logs to produce a scored query list — the input to the trie builder.

### Pipeline Architecture

```
SPARK BATCH PIPELINE (runs hourly)

┌─────────┐     ┌──────────┐     ┌────────────┐     ┌──────────┐     ┌──────────┐
│  S3     │     │  Read &  │     │ Normalize  │     │ GroupBy  │     │  Time-   │
│ (raw    │────▶│  Filter  │────▶│ + Dedup    │────▶│  Query   │────▶│  Decay   │
│  logs,  │     │  (last   │     │            │     │ + Count  │     │  Scoring │
│  30d)   │     │   30d)   │     │            │     │          │     │          │
└─────────┘     └──────────┘     └────────────┘     └──────────┘     └────┬─────┘
                                                                          │
  ┌───────────────────────────────────────────────────────────────────────┘
  │
  ▼
┌──────────┐     ┌───────────┐     ┌──────────┐     ┌──────────────┐
│ Content  │     │ Frequency │     │  Sort by  │     │  Output:     │
│ Filter   │────▶│ Threshold │────▶│  Score    │────▶│  Scored      │
│(blocklist│     │ (min 10   │     │(descending│     │  Query List  │
│ + ML)    │     │  searches)│     │          )│     │  (Parquet    │
└──────────┘     └───────────┘     └──────────┘     │   on S3)     │
                                                     └──────────────┘
```

### Step-by-Step Processing

#### Step 1: Read & Filter

```
// Spark pseudocode
raw_logs = spark.read.parquet("s3://search-logs/dt=2026-01-09/to/dt=2026-02-08/")

// Filter to relevant events only
filtered = raw_logs
    .filter(event_type IN ('SEARCH_COMPLETED', 'SUGGESTION_CLICKED'))
    .filter(NOT is_bot(user_agent, ip))
    .filter(NOT is_internal(ip))
    .filter(len(query_text) >= 2)
```

#### Step 2: Normalize & Dedup

```
normalized = filtered
    .withColumn("query_normalized", normalize(col("query_text")))
    .dropDuplicates(["user_id", "query_normalized", "timestamp"])  // exact dedup
```

#### Step 3: GroupBy & Count

```
aggregated = normalized
    .groupBy("query_normalized")
    .agg(
        count("*").alias("total_count"),
        countDistinct("user_id").alias("unique_users"),
        max("timestamp").alias("last_seen"),
        avg("purchase_made").alias("conversion_rate"),
        collect_set("region").alias("regions")
    )
```

#### Step 4: Time-Decay Scoring

```
// For each query, compute time-decayed score
// using daily buckets over the last 30 days

scored = aggregated_by_day
    .withColumn("decay_factor", exp(-0.1 * col("age_days")))
    .withColumn("weighted_count", col("daily_count") * col("decay_factor"))
    .groupBy("query_normalized")
    .agg(
        sum("weighted_count").alias("frequency_score"),
        first("conversion_rate").alias("conversion_rate"),
        first("last_seen").alias("last_seen")
    )
    .withColumn("final_score",
        0.5 * col("frequency_score") +
        0.2 * col("conversion_rate") * 100 +
        0.1 * (1.0 / (1 + datediff(current_date(), col("last_seen"))))
    )
```

#### Step 5: Content Filter

```
// Load blocklist
blocklist = spark.read.json("s3://config/content-blocklist.json")

// Filter blocked queries
clean = scored
    .join(blocklist, scored.query_normalized == blocklist.term, "left_anti")
    .filter(NOT contains_blocked_pattern(col("query_normalized")))
```

#### Step 6: Frequency Threshold & Output

```
// Remove queries with fewer than 10 total searches (noise reduction)
final = clean
    .filter(col("total_count") >= 10)
    .orderBy(desc("final_score"))

// Output: ~500M rows
final.write.parquet("s3://autocomplete-pipeline/scored-queries/dt=2026-02-08/")
```

### Spark Job Configuration

| Parameter | Value | Rationale |
|---|---|---|
| Executor count | 100 | Process 30 days of data in parallel |
| Executor memory | 8 GB | Comfortable for groupBy operations |
| Shuffle partitions | 2000 | Even distribution across executors |
| Input size | ~36 TB (30 days × 1.2 TB/day) | |
| Output size | ~50 GB (Parquet, compressed) | |
| Runtime | ~20 minutes | Dominated by shuffle (GroupBy) |

---

## 4. Streaming Pipeline (Kafka + Flink)

The streaming pipeline runs continuously, detecting trending queries in near-real-time. It complements the batch pipeline by providing fast detection of spikes.

### Architecture

```
STREAMING PIPELINE (continuous)

┌─────────┐     ┌────────────────────────────────────┐     ┌──────────┐
│  Kafka  │     │          Apache Flink               │     │  Redis   │
│ (search │────▶│                                      │────▶│(trending │
│  queries│     │  ┌──────────┐   ┌─────────────────┐ │     │ queries) │
│  topic) │     │  │ Tumbling │   │ Spike Detection │ │     │          │
│         │     │  │ Window   │──▶│ (compare vs     │ │     │ Key:     │
│         │     │  │ (5 min)  │   │  historical)    │ │     │ trending │
│         │     │  └──────────┘   └─────────────────┘ │     │ :queries │
│         │     │                                      │     └──────────┘
│         │     └──────────────────────────────────────┘
│         │                                                   ┌──────────┐
│         │────────────────────────────────────────────────────▶│ Historical│
│         │     (also feeds historical baseline table           │ Baseline │
│         │      via separate Flink job)                       │ (DynamoDB)│
│         │                                                    └──────────┘
```

### Flink Job: Windowed Query Counting

```
// Flink pseudocode

queryStream = env
    .addSource(KafkaSource("search-queries"))
    .filter(event -> event.type == SEARCH_COMPLETED)
    .map(event -> normalize(event.query_text))
    .filter(query -> query.length >= 2)

// Count queries in 5-minute tumbling windows
windowedCounts = queryStream
    .keyBy(query -> query)
    .window(TumblingProcessingTimeWindows.of(Time.minutes(5)))
    .aggregate(CountAggregator())
    // Output: (query, count, window_end_time)

// Compare against historical baseline
trendingQueries = windowedCounts
    .connect(historicalBaseline)  // from DynamoDB lookup
    .process(SpikeDetector(
        spike_threshold: 5.0,     // 5x above historical average
        min_count: 100,           // at least 100 queries in the window
        max_trending: 5000        // cap at 5000 trending queries
    ))
```

### Spike Detection Logic

```
class SpikeDetector:
    function processElement(current_count, query, window_time):
        // Get historical average for this query at this time-of-day
        historical = getHistoricalAverage(query, window_time.timeOfDay)

        if historical.avg == 0:
            // Brand new query — never seen before
            if current_count > NEW_QUERY_MIN_COUNT (100):
                trending_score = current_count * NEW_QUERY_BOOST (2.0)
                emit(query, trending_score, "NEW")
            return

        ratio = current_count / historical.avg

        if ratio > SPIKE_THRESHOLD (5.0):
            trending_score = min(ratio, MAX_BOOST (10.0))
            emit(query, trending_score, "SPIKE")

        // Also detect sudden drops (query going from popular to zero)
        if ratio < DROP_THRESHOLD (0.1) and historical.avg > 1000:
            emit_alert("Query '%s' dropped significantly" % query)
```

### Historical Baseline Table

The baseline is maintained by a separate Flink job that updates a DynamoDB table:

```
Table: query_baselines
Key: (query, hour_of_day, day_of_week)
Attributes:
    avg_5min_count: float       // average count per 5-min window
    stddev: float               // standard deviation
    last_updated: timestamp

// Updated daily from the batch pipeline
// Used by the streaming pipeline for spike detection
```

### Output: Trending Queries in Redis

```
// Redis data structure
Key: trending:queries
Type: Sorted Set (ZSET)
Members: query → trending_score

Example:
    "prime day deals"       → 10.0
    "super bowl 2026"       → 8.5
    "breaking news xyz"     → 7.2
    "new product launch"    → 5.5
    ...

TTL: 30 minutes (auto-expire trending queries that stop spiking)

// Autocomplete service reads this at query time:
trending = redis.zrevrange("trending:queries", 0, 4999)  // top 5000
```

---

## 5. Trie Building Process

The trie builder is a standalone service that takes the batch output (scored query list) and streaming output (trending queries) and produces a deployable trie binary.

### End-to-End Build Flow

```
TRIE BUILD PIPELINE (triggered hourly or on-demand)

Step 1          Step 2          Step 3           Step 4          Step 5
┌──────────┐   ┌──────────┐   ┌──────────────┐ ┌──────────┐   ┌──────────┐
│ Read     │   │ Read     │   │ Merge &      │ │ Sort     │   │ Build    │
│ Scored   │──▶│ Trending │──▶│ Deduplicate  │─▶│ Alpha-   │──▶│Compressed│
│ Queries  │   │ Overlay  │   │ (batch +     │ │ betically│   │ Trie     │
│ (S3)     │   │ (Redis)  │   │  trending)   │ │          │   │          │
└──────────┘   └──────────┘   └──────────────┘ └──────────┘   └────┬─────┘
                                                                    │
Step 10         Step 9          Step 8           Step 7         Step 6
┌──────────┐   ┌──────────┐   ┌──────────────┐ ┌──────────┐   ┌────▼─────┐
│ Health   │   │ Trie     │   │ Upload to    │ │ Serialize│   │ Propagate│
│ Check &  │◀──│ Servers  │◀──│ S3           │◀│ to Binary│◀──│ Top-K    │
│ Traffic  │   │ Pull &   │   │ (artifact    │ │ Format   │   │ (bottom- │
│ Switch   │   │ Load     │   │  store)      │ │          │   │  up)     │
└──────────┘   └──────────┘   └──────────────┘ └──────────┘   └──────────┘
```

### Detailed Steps

| Step | Action | Input | Output | Duration |
|---|---|---|---|---|
| 1 | Read scored query list | S3 Parquet files | In-memory DataFrame | ~2 min |
| 2 | Read trending queries | Redis ZSET | In-memory list (~5K queries) | ~0.1 sec |
| 3 | Merge batch + trending | Both lists | Combined scored list. Trending queries get score boost | ~1 min |
| 4 | Sort alphabetically | Combined list | Sorted by query text (enables efficient trie building) | ~2 min |
| 5 | Build compressed trie | Sorted query list | Trie with edge labels, terminal markers, frequencies | ~5 min |
| 6 | Propagate top-K | Trie without top-K | Trie with top-K at every node (bottom-up pass) | ~3 min |
| 7 | Serialize to binary | In-memory trie | Binary file (breadth-first traversal) | ~1 min |
| 8 | Upload to S3 | Binary file | `s3://autocomplete/tries/trie_2026020814.bin` | ~1 min |
| 9 | Trie servers pull | S3 artifact | In-memory mmap'd trie on each server | ~2 min |
| 10 | Health check + switch | Loaded trie | Live traffic serving new trie version | ~1 min |
| **Total** | | | | **~18 min** |

### Step 3: Merging Batch and Trending

```
function mergeQueries(batch_queries, trending_queries):
    merged = {}

    // Start with all batch queries
    for (query, score) in batch_queries:
        merged[query] = score

    // Overlay trending queries
    for (query, trending_score) in trending_queries:
        if query in merged:
            // Boost existing query's score
            merged[query] = merged[query] * (1 + trending_score * TRENDING_WEIGHT)
        else:
            // New trending query not in batch — add with base score
            merged[query] = trending_score * NEW_TRENDING_BASE_SCORE

    return merged
```

### Step 5: Building the Compressed Trie from Sorted Input

Building a compressed trie from a **sorted** list of queries is efficient — we can process queries sequentially, sharing prefixes naturally:

```
function buildTrieFromSorted(sorted_queries):
    root = TrieNode()

    for (query, score) in sorted_queries:
        insertIntoTrie(root, query, score)

    return root

function insertIntoTrie(root, query, score):
    // Standard compressed trie insertion
    // (see trie-and-ranking-deep-dive.md Section 5)
    // Traverse existing edges, split where needed, create new edges
```

### Step 6: Top-K Propagation (Bottom-Up)

```
function propagateTopK(node, K=10):
    // Base case: leaf/terminal node
    candidates = []
    if node.is_terminal:
        candidates.append(ScoredSuggestion(node.query, node.score))

    // Recurse into all children
    for child in node.children.values():
        propagateTopK(child, K)
        candidates.extend(child.top_k)

    // Sort by score descending, keep top K
    candidates.sort(key=lambda s: s.score, reverse=True)
    node.top_k = candidates[:K]
```

### Build Artifacts

```
s3://autocomplete/tries/
├── trie_2026020801.bin        // 7.2 GB — build at 1 AM
├── trie_2026020802.bin        // 7.2 GB — build at 2 AM
├── ...
├── trie_2026020814.bin        // 7.3 GB — build at 2 PM (latest)
├── manifest.json              // {"latest": "trie_2026020814.bin", "previous": [...]}
└── checksums.json             // SHA-256 of each trie file
```

---

## 6. Trending Query Detection

### Why Trending Matters

Without trending detection, a newly spiking query won't appear in suggestions until the next batch build (up to 1 hour). For time-sensitive events (breaking news, product launches, sales events), users expect immediate autocomplete support.

### Detection Algorithm: Z-Score Based Spike Detection

The Flink-based spike detector uses a modified Z-score approach:

```
function detectSpike(query, current_count, window_time):
    baseline = getBaseline(query, window_time)

    if baseline.count < MIN_HISTORY (7 data points):
        // Not enough history — use absolute threshold
        if current_count > ABSOLUTE_SPIKE_THRESHOLD (500):
            return TrendingResult(score: current_count / 100, type: "NEW")
        return null

    z_score = (current_count - baseline.mean) / max(baseline.stddev, 1)

    if z_score > Z_THRESHOLD (3.0) and current_count > MIN_COUNT (100):
        // Statistically significant spike
        boost = min(current_count / baseline.mean, MAX_BOOST (10.0))
        return TrendingResult(score: boost, type: "SPIKE")

    return null
```

### Trending Examples

| Scenario | Normal Rate | Current Rate | Ratio | Detected? |
|---|---|---|---|---|
| "prime day deals" on Prime Day | 1,000 / 5 min | 50,000 / 5 min | 50x | ✅ Yes — capped boost at 10.0 |
| "super bowl 2026" on Super Bowl Sunday | 500 / 5 min | 25,000 / 5 min | 50x | ✅ Yes |
| "iphone 16" (steady growth) | 5,000 / 5 min | 6,000 / 5 min | 1.2x | ❌ No — ratio < 5x |
| Brand new product (no history) | 0 / 5 min | 2,000 / 5 min | ∞ | ✅ Yes — absolute threshold |
| Bot spam "buy xyz" | 0 / 5 min | 10,000 / 5 min | ∞ | ⚠️ Yes — but content filter catches it |

### Trending Overlay at Serving Time

The autocomplete service merges trending results with trie results:

```
function getAutocompleteSuggestions(prefix, K=10):
    // 1. Get base suggestions from trie
    trie_results = trie.search(prefix)    // top-K from precomputed trie

    // 2. Get trending queries matching this prefix
    trending = redis.zrevrangebyscore("trending:queries", "+inf", 0)
    trending_matches = [q for q in trending if q.startsWith(prefix)]

    // 3. Merge: insert trending queries into trie results
    combined = merge(trie_results, trending_matches)

    // 4. Re-rank: trending queries get a boost
    for suggestion in combined:
        if suggestion in trending_matches:
            suggestion.score *= TRENDING_DISPLAY_BOOST (1.5)

    // 5. Sort and return top-K
    combined.sort(key=lambda s: s.score, reverse=True)
    return combined[:K]
```

---

## 7. Freshness vs Accuracy Tradeoffs

### Comparison of Pipeline Frequencies

| Approach | Freshness | Accuracy | Compute Cost | Complexity | Use Case |
|---|---|---|---|---|---|
| **Full rebuild every 24h** | Poor — up to 24h stale | High — full 30-day window | Low | Low | Small-scale applications |
| **Full rebuild every 1h** | Medium — up to 1h stale | High | Medium | Medium | **Our batch pipeline** |
| **Incremental update every 15 min** | Good | Medium — might miss patterns | Medium-High | High | Real-time-sensitive apps |
| **Real-time streaming into trie** | Excellent | Lower — noisy, no aggregation | High | Very high | Not recommended (quality issues) |
| **Hybrid: batch 1h + trending 5 min** | Good | High | Medium | Medium-High | **Our recommended approach** |

### Why Not Real-Time Updates?

Updating the trie in real-time (as each query comes in) has several problems:

1. **Noisy data**: A single query like "asdfgh" (keyboard mash) would immediately enter the trie. Batch processing aggregates over time, naturally filtering noise.

2. **Top-K invalidation**: Each insert could change the top-K at multiple ancestor nodes. Re-propagating top-K at query ingestion rate (~70K/sec) is computationally infeasible.

3. **Content filtering**: Offensive queries would briefly appear in suggestions before being caught. Batch processing applies content filtering before the trie is built.

4. **Concurrency**: Concurrent reads and writes on the trie require locking or lock-free data structures, adding complexity and potential latency spikes.

### The Hybrid Sweet Spot

Our hybrid approach gives us:
- **Hourly batch**: High-quality, noise-filtered trie for 99% of queries
- **5-minute trending overlay**: Fast detection of spikes for the 1% of queries that are time-sensitive
- **Combined**: Suggestions feel fresh (trending within 5-15 minutes) while maintaining high quality (batch-built trie)

### Staleness Analysis

| Scenario | Time to Appear in Suggestions | Acceptable? |
|---|---|---|
| New product launch ("iPhone 17") | 5-15 min (trending) → 1h (trie) | ✅ Yes |
| Breaking news event | 5-15 min (trending) | ✅ Yes |
| Seasonal query ("valentine's day gifts") | 1h (trie, frequency builds naturally) | ✅ Yes |
| Typo correction ("amazn" → should not appear) | Never (filtered by threshold) | ✅ Yes |
| Offensive query (new pattern) | Never (content filter) | ✅ Yes |

---

## 8. Privacy and Data Retention

### Data Classification

| Data Type | Classification | Retention | Storage |
|---|---|---|---|
| Raw query logs (with hashed user_id) | **Sensitive** | 30 days | S3 with encryption at rest |
| Aggregated query frequencies | **Internal** | 90 days | S3 |
| Trie binary (no PII) | **Internal** | 5 versions (~5 days) | S3 |
| User profiles (personalization) | **Sensitive** | 30-day TTL | Redis with encryption |
| Trending queries | **Internal** | 30-min TTL | Redis |

### GDPR / CCPA Compliance

1. **Right to deletion**: If a user requests data deletion, their hashed user_id is added to a deletion list. The next batch pipeline run excludes their queries. Since the trie only contains aggregated frequencies (no user_id), the trie itself doesn't need modification — removing one user's queries from the aggregation reduces the count by a negligible amount.

2. **Right to access**: Users can request their search history, which is stored (hashed) in the raw logs. The user profile in Redis (personalization) can also be exported.

3. **Consent**: Search queries are collected under Amazon's terms of service. Personalization requires explicit opt-in.

### Anonymization Pipeline

```
function anonymizeLog(event):
    event.user_id = sha256(event.user_id + DAILY_SALT)    // daily rotation
    event.ip_address = REDACTED                            // not logged
    event.precise_location = REDACTED                      // only region kept

    // After 30 days, even hashed user_id is stripped
    // Aggregated data retains no user-level information
    return event
```

### Data Minimization

- We log the **minimum necessary** for trie building: query text, timestamp, region, event type
- User-agent, IP address, and precise location are **not stored** in the query log
- Session-level data (session_id) is used only for deduplication and is stripped after aggregation

---

## 9. Pipeline Monitoring and Alerting

### Key Metrics Dashboard

| Metric | Normal Range | Alert Threshold | Alert Severity |
|---|---|---|---|
| Kafka consumer lag | < 10,000 events | > 100,000 events | P2 (Warning) |
| Spark job duration | 15-25 min | > 55 min | P1 (Critical) — risks missing hourly window |
| Trie build time | 10-20 min | > 40 min | P1 (Critical) |
| Trie size (bytes) | 7-8 GB | < 3 GB or > 15 GB | P1 (Critical) — anomalous output |
| Query count in trie | 450M-550M | < 200M or > 1B | P1 (Critical) |
| Content filter block rate | 0.1-0.5% | > 5% | P2 (Warning) — possible attack |
| Trie deploy success rate | 100% | < 100% | P1 (Critical) |
| Trending query count | 500-5000 | > 10,000 | P2 (Warning) — possible false positives |
| Flink checkpoint duration | < 30s | > 2 min | P2 (Warning) |

### Data Quality Checks (Automated)

After each trie build, before deployment:

```
function validateTrie(new_trie, previous_trie):
    checks = []

    // Size check
    size_ratio = new_trie.size / previous_trie.size
    checks.append(("Size ratio", 0.5 < size_ratio < 2.0))

    // Query count check
    count_ratio = new_trie.query_count / previous_trie.query_count
    checks.append(("Query count ratio", 0.8 < count_ratio < 1.2))

    // Top-100 stability check — top queries shouldn't change dramatically
    top_100_old = previous_trie.root.top_k[:100]
    top_100_new = new_trie.root.top_k[:100]
    overlap = len(set(top_100_old) & set(top_100_new))
    checks.append(("Top-100 overlap", overlap >= 80))  // at least 80% overlap

    // Content filter check — sample and verify
    sample = random_sample(new_trie.all_queries(), 1000)
    blocked = [q for q in sample if is_blocked(q)]
    checks.append(("No blocked content", len(blocked) == 0))

    // Latency check — sample lookups
    avg_latency = average([trie.search(random_prefix()) for _ in range(10000)])
    checks.append(("Avg lookup latency", avg_latency < 1.0))  // < 1ms

    if all(check[1] for check in checks):
        return PASS
    else:
        failed = [(name, result) for name, result in checks if not result]
        alert(f"Trie validation failed: {failed}")
        return FAIL
```

### Alerting and Escalation

```
ESCALATION PATH:

Metric anomaly detected
        │
        ▼
┌──────────────┐     ┌──────────────┐     ┌──────────────┐
│ CloudWatch   │────▶│ PagerDuty    │────▶│ On-Call       │
│ Alarm        │     │ (P1: page,   │     │ Engineer      │
│              │     │  P2: ticket) │     │               │
└──────────────┘     └──────────────┘     └──────────────┘

Actions:
- P1 (Critical): Page on-call, auto-rollback trie if deploy failed
- P2 (Warning): Create ticket, investigate during business hours
- P3 (Info): Log for weekly review
```

---

## 10. Failure Modes and Recovery

### Failure Scenarios

| Failure | Impact | Detection | Recovery | RTO |
|---|---|---|---|---|
| **Kafka broker down** | Log ingestion delayed | Kafka consumer lag spike | Kafka replication (3x). Consumer retries from last offset. | ~5 min |
| **S3 outage** | Can't read logs or store trie | Spark job fails to start | Trie servers keep serving current version. Retry on S3 recovery. | Hours (depends on S3) |
| **Spark job failure** | No new trie build this cycle | Job exit code != 0 | Automatic retry (up to 3 times). Alert if all retries fail. Serve previous trie. | 20-60 min |
| **Flink job crash** | Trending detection stops | Flink checkpoint timeout | Flink restarts from last checkpoint (exactly-once). Trending overlay goes stale. | ~2 min |
| **Trie build produces bad output** | Degraded suggestions | Validation checks fail | Abort deployment. Keep serving previous trie version. Alert. | 0 min (auto-prevented) |
| **Trie server OOM during loading** | Server can't serve | Health check fails | Load balancer routes to healthy servers. Investigate memory spike. | ~1 min |
| **Content filter service down** | Blocked queries might enter trie | Health check on filter service | Pause trie build until filter service recovers. Never build without filtering. | Variable |
| **Redis (trending) down** | No trending overlay | Redis health check | Serve trie-only results (no trending). Trending is a nice-to-have, not critical. | ~5 min |

### Recovery Principle: Always Serve Something

The autocomplete system follows a **graceful degradation** principle:

```
DEGRADATION CHAIN:

     Full functionality              Degraded but functional
┌─────────────────────┐         ┌──────────────────────┐
│ Fresh trie +        │  fail   │ Previous trie +      │
│ trending overlay +  │ ──────▶ │ trending overlay     │
│ personalization     │         │ (stale by 1 hour)    │
└─────────────────────┘         └──────┬───────────────┘
                                       │ fail
                                       ▼
                                ┌──────────────────────┐
                                │ Previous trie only   │
                                │ (no trending, no     │
                                │  personalization)    │
                                └──────┬───────────────┘
                                       │ fail
                                       ▼
                                ┌──────────────────────┐
                                │ Cached results       │
                                │ (Redis/CDN)          │
                                └──────┬───────────────┘
                                       │ fail
                                       ▼
                                ┌──────────────────────┐
                                │ Empty suggestions    │
                                │ (search bar still    │
                                │  works)              │
                                └──────────────────────┘
```

At every level, the user can still search — autocomplete is a **progressive enhancement**, not a hard dependency.

---

*This document complements the [Interview Simulation](interview-simulation.md) and is referenced by the [System Flows](flow.md) document.*
