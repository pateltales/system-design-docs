# OpenSearch: Search Execution & Relevance Scoring Deep Dive

> **Context:** This document covers how OpenSearch executes searches end-to-end,
> how it scores documents for relevance, the full query DSL, aggregations,
> caching, and how it contrasts with Solr. This is the runtime read path —
> what happens after documents are already indexed and segments are built.

---

## Two-Phase Search: Query Then Fetch

Every search request in OpenSearch executes in two distinct phases. This is
not an optimization detail — it is fundamental to how distributed search works
and a common interview topic.

### Why Two Phases?

A naive approach would be: send query to every shard, have each shard return
full documents, merge at the coordinator. The problem: if you want top 10
results from 50 shards, each shard returns its local top 10 full documents
(50 x 10 = 500 full documents transferred), and 490 of those are thrown away.
Full documents can be kilobytes or megabytes. This wastes network bandwidth
and memory.

The two-phase approach solves this:
- **Query phase**: transfer only doc IDs + scores (tiny — ~16 bytes each)
- **Fetch phase**: retrieve full documents for ONLY the final top-N

### Phase 1: Query Phase (Scatter-Gather)

```
Client
  |
  |  POST /products/_search { "query": { "match": { "name": "wireless headphones" } }, "size": 10 }
  v
+---------------------+
| Coordinating Node   |   (any node can coordinate — the one that received the request)
+---------------------+
  |         |         |
  | scatter | scatter | scatter   (query sent to ONE copy of each shard — primary or replica)
  v         v         v
+-------+ +-------+ +-------+
|Shard 0| |Shard 1| |Shard 2|     (3 primary shards in this example)
+-------+ +-------+ +-------+
  |         |         |
  | Each shard:                    1. Runs query against ALL local Lucene segments
  | - Searches inverted index     2. Computes BM25 score per matching doc
  | - Builds local priority       3. Returns top-N (size + from) doc IDs + scores
  |   queue of size N                to coordinating node
  |         |         |
  v         v         v
+---------------------+
| Coordinating Node   |   Receives N results from EACH shard
|                     |   Merges into global priority queue (min-heap, size N)
| Global merge:       |   Result: globally-sorted top-N doc IDs + scores
| Shard0: [docA:9.2,  |
|          docD:7.1]   |   Note: "from" parameter means each shard returns
| Shard1: [docF:8.8,  |   (from + size) results — deep pagination is expensive
|          docB:6.5]   |
| Shard2: [docC:8.1,  |
|          docG:5.9]   |
|                     |
| Merged top-3:       |
| [docA:9.2, docF:8.8,|
|  docC:8.1]          |
+---------------------+
```

Key details:
- The coordinating node picks ONE copy of each shard (primary or replica)
  using adaptive replica selection (picks the replica with lowest queue time)
- Each shard searches ALL its Lucene segments (there is no cross-segment index)
- The per-shard priority queue is of size `from + size` (so `from=1000, size=10`
  means each shard must score and rank its top 1010 docs — deep pagination hurts)
- Only doc IDs (internal Lucene doc IDs) and scores are returned — no field data

### Phase 2: Fetch Phase (Multi-Get)

```
+---------------------+
| Coordinating Node   |   Has the globally-sorted top-N doc IDs
|                     |   Knows which shard each doc lives on
+---------------------+
  |              |
  | multi-get    | multi-get       (only contacts shards that have winning docs)
  v              v
+-------+     +-------+
|Shard 0|     |Shard 1|           Shard 2 is NOT contacted — none of its docs
+-------+     +-------+           made the global top-N
  |              |
  | Returns full | Returns full
  | _source for  | _source for
  | docA         | docF
  v              v
+---------------------+
| Coordinating Node   |   Assembles final response:
|                     |   { "hits": [docA, docF, docC], ... }
+---------------------+
  |
  v
Client
```

Key details:
- Only shards with documents in the final top-N are contacted
- Each shard loads the stored `_source` field from disk (or OS page cache)
- If `_source` filtering is specified (`"_source": ["name", "price"]`),
  the shard extracts only those fields before returning
- Highlighting, script fields, and inner hits are also computed in fetch phase

### Complete Two-Phase Flow (ASCII)

```
                            QUERY PHASE                          FETCH PHASE
                     ┌─────────────────────────┐          ┌──────────────────────┐
                     │                         │          │                      │
Client ──request──>  Coordinator               │          │                      │
                     │                         │          │                      │
                     ├──query──> Shard 0 ──────┤─IDs+──>  ├──get(docA)──> Shard 0│──_source──┐
                     │                  scores │          │                      │           │
                     ├──query──> Shard 1 ──────┤─IDs+──>  ├──get(docF)──> Shard 1│──_source──┤
                     │                  scores │          │                      │           │
                     ├──query──> Shard 2 ──────┤─IDs+──>  │  (Shard 2 skipped)   │           │
                     │                  scores │          │                      │           │
                     │                         │          │                      │           │
                     │  ┌──────────────────┐   │          │                      │           │
                     │  │ Global merge     │   │          │                      │           │
                     │  │ (priority queue) │   │          │                      │           │
                     │  │ → top-N IDs      │───┘          │  Assemble response  <────────────┘
                     │  └──────────────────┘              │                      │
                     │                                    │                      │
                     └────────────────────────────────────┴──────> Client
```

### Interview Talking Point: Deep Pagination Problem

With `from=10000, size=10`, each shard must return 10,010 scored results in the
query phase. With 50 shards, the coordinator merges 500,500 results. This is
why OpenSearch provides `search_after` (cursor-based pagination using the sort
values of the last result) and the `scroll` API (deprecated in favor of
point-in-time + search_after). Always mention this tradeoff in interviews.

---

## BM25 Relevance Scoring

OpenSearch uses **BM25** (Best Matching 25) as its default scoring function,
replacing the older TF-IDF since Elasticsearch 5.0 / Lucene 6.

### The Formula

For a query with terms `t1, t2, ..., tn`, the score for document `d` is:

```
score(d, Q) = Σ  IDF(ti) × [ tf(ti, d) × (k1 + 1) ]
             i=1              ─────────────────────────────────────
                              tf(ti, d) + k1 × (1 - b + b × dl / avgdl)
```

Where:
- **tf(t, d)** — term frequency: how many times term `t` appears in document `d`
- **dl** — document length (number of terms in document `d`)
- **avgdl** — average document length across the entire shard
- **IDF(t)** — inverse document frequency: `ln(1 + (N - df + 0.5) / (df + 0.5))`
  - `N` = total number of documents in the shard
  - `df` = number of documents containing term `t`
- **k1 = 1.2** (default) — term frequency saturation parameter
- **b = 0.75** (default) — document length normalization parameter

### Understanding Each Component

**IDF — "How rare is this term?"**
```
Term "the"   → appears in 99% of docs → IDF ≈ 0.01  (nearly worthless)
Term "kafka" → appears in 0.1% of docs → IDF ≈ 6.9   (very discriminating)
```
Rare terms contribute far more to relevance. This is why searching for
"kafka consumer group rebalance" ranks documents with "rebalance" higher
than those with just "kafka" — "rebalance" is rarer and more discriminating.

**TF with saturation — "How many times does the term appear?"**
```
                BM25 (saturates)              TF-IDF (linear)
Score           ┌──────────────               ┌─────────────/
contribution    │         ___________         │            /
from tf         │       /                     │          /
                │     /                       │        /
                │   /                         │      /
                │  /                          │    /
                │/                            │  /
                └──────────────────           └──────────────
                0  1  2  5  10  50            0  1  2  5  10  50
                   term frequency                term frequency
```

With BM25, a document mentioning "headphones" 50 times scores only slightly
higher than one mentioning it 5 times. With TF-IDF, 50 mentions scores 10x
higher — which rewards term-stuffing (spam). The saturation is controlled by
k1:

- **k1 = 0**: term frequency is completely ignored (only IDF matters)
- **k1 = 1.2** (default): moderate saturation — reasonable for most content
- **k1 = 2.0**: slower saturation — term frequency matters more

**Length normalization (b) — "Penalize long documents?"**
```
b = 1.0: full normalization — long documents are heavily penalized
b = 0.75 (default): moderate normalization
b = 0.0: no normalization — document length is ignored
```

A product title "Wireless Headphones" (2 words) with one mention of "headphones"
should score higher than a 5000-word product description with one mention.
The `b` parameter controls this — it normalizes tf by document length.

### When to Customize k1 and b

| Scenario | k1 | b | Reason |
|----------|-----|-----|--------|
| Short fields (titles, tags) | 1.2 | 0.25 | Less length variation — reduce length penalty |
| Long-form content (articles) | 1.2 | 0.75 | Default works well |
| Log search (exact matching) | 0.5 | 0.0 | Term freq barely matters, length irrelevant |
| E-commerce product names | 2.0 | 0.3 | Repeated keyword in name is intentional signal |

Setting per-field BM25 parameters:
```json
PUT /products
{
  "mappings": {
    "properties": {
      "title": {
        "type": "text",
        "similarity": {
          "type": "BM25",
          "k1": 1.2,
          "b": 0.25
        }
      }
    }
  }
}
```

### Why BM25 Beats TF-IDF (Interview Answer)

1. **Saturation**: TF-IDF is linear in term frequency — mentioning "cheap" 100
   times makes a document 100x more relevant. BM25 saturates — diminishing
   returns after a few mentions. This resists keyword stuffing.
2. **Better length normalization**: TF-IDF divides by sqrt(doc length). BM25
   uses a tunable parameter `b` with a more principled normalization.
3. **Probabilistic foundation**: BM25 is derived from the probabilistic
   relevance framework (Robertson-Sparck Jones). TF-IDF is heuristic.

### Scoring is Per-Shard (Important Gotcha)

BM25 uses shard-local statistics (document count N, average doc length avgdl,
document frequency df). With uneven shard sizes or skewed data distribution,
the same document could get different scores on different shards. Mitigations:
- Use `?search_type=dfs_query_then_fetch` — adds a pre-query phase to gather
  global term statistics (extra round trip, but accurate scores)
- Ensure even data distribution via good routing
- Use enough documents per shard (>1000) so local stats approximate global stats

---

## Query Types

OpenSearch provides a rich query DSL. These are the essential types.

### match — Full-Text Search

The workhorse query. Analyzes the query string, then searches the inverted index.

```json
GET /products/_search
{
  "query": {
    "match": {
      "description": {
        "query": "wireless noise cancelling headphones",
        "operator": "or",
        "minimum_should_match": "75%"
      }
    }
  }
}
```

How it works:
1. Query string "wireless noise cancelling headphones" is run through the
   same analyzer as the field (e.g., standard analyzer)
2. Produces tokens: ["wireless", "noise", "cancelling", "headphones"]
3. Each token is looked up in the inverted index for the `description` field
4. Results are combined with OR (default) or AND
5. Each matching doc is scored with BM25

Variants:
- `match_phrase` — tokens must appear in order, adjacent
- `multi_match` — search across multiple fields with optional per-field boosting
- `match_phrase_prefix` — autocomplete / search-as-you-type

```json
{
  "query": {
    "multi_match": {
      "query": "wireless headphones",
      "fields": ["title^3", "description", "brand^2"],
      "type": "best_fields"
    }
  }
}
```
`title^3` means matches in title are boosted 3x.

### term — Exact Match (No Analysis)

For keyword fields, enums, IDs — where you want exact matching, no tokenization.

```json
{
  "query": {
    "term": {
      "status": "published"
    }
  }
}
```

Critical distinction:
- `match` on a `text` field: "Running Shoes" → tokens ["running", "shoes"] → matches
- `term` on a `keyword` field: "Running Shoes" → exact match only "Running Shoes"

Common mistake: using `term` on a `text` field. "Running Shoes" won't match
because the indexed tokens are ["running", "shoes"] (lowercased), not the
original string. Always use `term` with `keyword` fields.

### bool — Compound Query

Combines multiple queries with boolean logic. The most important compound query.

```json
{
  "query": {
    "bool": {
      "must": [
        { "match": { "title": "wireless headphones" } }
      ],
      "should": [
        { "match": { "brand": "sony" } },
        { "range": { "rating": { "gte": 4.5 } } }
      ],
      "must_not": [
        { "term": { "status": "discontinued" } }
      ],
      "filter": [
        { "range": { "price": { "gte": 50, "lte": 200 } } },
        { "term": { "category": "electronics" } }
      ],
      "minimum_should_match": 1
    }
  }
}
```

| Clause     | Affects scoring? | Meaning |
|------------|-----------------|---------|
| `must`     | Yes | Document MUST match. Contributes to score. |
| `should`   | Yes | Document SHOULD match. Boosts score if it does. |
| `must_not` | No  | Document MUST NOT match. Excludes. Runs as filter. |
| `filter`   | No  | Document MUST match, but does NOT affect score. |

**Interview insight**: `filter` vs `must` is a critical distinction. Filters
are cacheable (OpenSearch caches the bitset of matching doc IDs per segment)
and skip scoring. For structured conditions (price ranges, status checks,
date ranges), always use `filter` — it is faster and results get cached.

### range — Numeric and Date Ranges

```json
{
  "query": {
    "range": {
      "timestamp": {
        "gte": "2026-01-01",
        "lt": "2026-02-01",
        "format": "yyyy-MM-dd"
      }
    }
  }
}
```

Implementation detail: range queries on numeric and date fields use **BKD trees**
(Block KD-trees), not the inverted index. BKD trees are a disk-efficient
multi-dimensional spatial index. They partition the numeric space into blocks,
enabling O(sqrt(N)) range lookups instead of scanning every term in the inverted
index. This is why numeric fields should be mapped as `integer`/`long`/`date`,
NOT as `keyword` — keyword fields would use the inverted index for range queries,
which is far slower.

### nested — Preserving Object Boundaries

Standard object arrays are flattened, losing field associations:

```json
{
  "reviews": [
    { "author": "alice", "rating": 5 },
    { "author": "bob",   "rating": 2 }
  ]
}
```

Internally flattened to:
```
reviews.author: ["alice", "bob"]
reviews.rating: [5, 2]
```

A query for `reviews.author=alice AND reviews.rating=2` would incorrectly
match — Alice's rating was 5, Bob's was 2, but the association is lost.

Nested fields solve this by indexing each array element as a hidden separate
Lucene document:

```json
PUT /products
{
  "mappings": {
    "properties": {
      "reviews": {
        "type": "nested",
        "properties": {
          "author": { "type": "keyword" },
          "rating": { "type": "integer" }
        }
      }
    }
  }
}
```

```json
{
  "query": {
    "nested": {
      "path": "reviews",
      "query": {
        "bool": {
          "must": [
            { "term": { "reviews.author": "alice" } },
            { "range": { "reviews.rating": { "gte": 4 } } }
          ]
        }
      }
    }
  }
}
```

Tradeoff: each nested object is a hidden Lucene document. A document with
100 nested objects creates 101 Lucene documents (1 parent + 100 nested).
This increases index size and slows queries.

### function_score — Custom Scoring

Override or modify BM25 scores with custom logic. Essential for e-commerce,
content platforms, and any system where relevance is not purely text-based.

```json
{
  "query": {
    "function_score": {
      "query": { "match": { "title": "headphones" } },
      "functions": [
        {
          "field_value_factor": {
            "field": "popularity",
            "modifier": "log1p",
            "factor": 2
          }
        },
        {
          "gauss": {
            "created_at": {
              "origin": "now",
              "scale": "30d",
              "decay": 0.5
            }
          }
        },
        {
          "filter": { "term": { "featured": true } },
          "weight": 5
        }
      ],
      "score_mode": "sum",
      "boost_mode": "multiply"
    }
  }
}
```

This query:
1. Starts with BM25 text relevance for "headphones"
2. Boosts by log(1 + popularity × 2) — popular items rank higher
3. Applies Gaussian decay by recency — newer items rank higher
4. Adds weight of 5 for featured items
5. Functions are summed together, then multiplied with the original BM25 score

Common function types:
- `field_value_factor` — boost by a numeric field (popularity, sales count)
- `gauss` / `exp` / `linear` — decay functions (recency, geo distance)
- `script_score` — arbitrary Painless script for complex logic
- `random_score` — consistent random ordering (A/B testing)
- `weight` — static boost, often combined with a filter

---

## Aggregations

Aggregations compute analytics over the documents matching a query. They run
in the same request as the search — one round trip returns both search results
and analytics.

```json
GET /orders/_search
{
  "size": 0,
  "query": { "range": { "date": { "gte": "2026-01-01" } } },
  "aggs": {
    "sales_by_category": {
      "terms": { "field": "category", "size": 20 },
      "aggs": {
        "avg_price": { "avg": { "field": "price" } },
        "monthly_trend": {
          "date_histogram": {
            "field": "date",
            "calendar_interval": "month"
          }
        }
      }
    }
  }
}
```

This returns: top 20 categories by order count, with average price and monthly
order counts nested within each category. `"size": 0` means "don't return
search results, just aggregations."

### Bucket Aggregations — Group Documents

| Aggregation | Purpose | Example |
|------------|---------|---------|
| `terms` | Group by field value | Sales by category |
| `date_histogram` | Group by time interval | Orders per month |
| `range` | Group by numeric ranges | Price brackets |
| `filters` | Group by arbitrary queries | Segments (new vs returning users) |
| `histogram` | Group by fixed numeric interval | Price buckets of $10 |
| `geohash_grid` | Group by geographic area | Activity by region |

Bucket aggregations create buckets (groups) of documents. Each bucket can
contain nested sub-aggregations (metric or more buckets) — enabling
multi-dimensional analytics in a single query.

### Metric Aggregations — Compute Statistics

| Aggregation | Purpose | Notes |
|------------|---------|-------|
| `avg`, `sum`, `min`, `max` | Basic stats | Self-explanatory |
| `stats` / `extended_stats` | All basic stats at once | Includes variance, std_deviation |
| `cardinality` | Count distinct values | Uses **HyperLogLog** — approximate, O(1) memory |
| `percentiles` | P50, P95, P99, etc. | Uses **t-digest** — approximate, configurable precision |
| `value_count` | Count non-null values | Like SQL COUNT(field) |

**HyperLogLog for cardinality** deserves a callout. Counting exact distinct
values across distributed shards requires transmitting all values to the
coordinator — expensive for high-cardinality fields (user IDs, IP addresses).
HyperLogLog approximates cardinality with fixed ~4KB memory and <2% error rate.
Configurable via `precision_threshold` (higher = more accurate, more memory).

### Pipeline Aggregations — Aggregations on Aggregations

Pipeline aggregations take the output of other aggregations as input. They
run on the coordinating node after shard-level aggregation results are merged.

| Aggregation | Purpose | Example |
|------------|---------|---------|
| `derivative` | Rate of change | Day-over-day order growth |
| `moving_avg` | Smoothed trend | 7-day moving average of revenue |
| `cumulative_sum` | Running total | Cumulative revenue over months |
| `bucket_sort` | Sort/truncate buckets | Top 5 categories by revenue |
| `bucket_selector` | Filter buckets by condition | Only categories with avg price > $50 |

```json
{
  "aggs": {
    "monthly_revenue": {
      "date_histogram": { "field": "date", "calendar_interval": "month" },
      "aggs": {
        "revenue": { "sum": { "field": "amount" } },
        "revenue_growth": {
          "derivative": { "buckets_path": "revenue" }
        },
        "smoothed_revenue": {
          "moving_avg": { "buckets_path": "revenue", "window": 3 }
        }
      }
    }
  }
}
```

### How Aggregations Work Internally: Doc Values

Aggregations do NOT use the inverted index. The inverted index maps
`term → [doc IDs]` — great for "which documents contain this term?" but
terrible for "what is the value of this field for this document?"

Instead, aggregations use **doc values** — a columnar, on-disk data structure:

```
Inverted Index (for search)          Doc Values (for aggregations)
term → doc IDs                       doc ID → field value

"electronics" → [1, 3, 7, 12]       doc 1 → "electronics"
"clothing"    → [2, 5, 8]           doc 2 → "clothing"
"books"       → [4, 6, 9]           doc 3 → "electronics"
                                     doc 4 → "books"
                                     ...
```

Doc values are:
- Built at index time (stored alongside the inverted index)
- Column-oriented (efficient for scanning one field across many docs)
- Memory-mapped (OS page cache handles hot data)
- Enabled by default for all field types except `text`

**Why not `text` fields?** Text fields are analyzed (tokenized), so a single
field value produces multiple tokens. Storing doc values for analyzed text
would require storing all tokens per doc, which is expensive. If you need to
aggregate on a string, use a `keyword` field (or a multi-field with both
`text` and `keyword`).

**Field data cache** is the fallback for aggregating on `text` fields. It
loads the entire inverted index into the JVM heap, inverted (term → doc IDs
becomes doc ID → terms). This is extremely memory-intensive and should be
avoided. Use `keyword` + doc values instead.

---

## Caching

OpenSearch uses three layers of caching. Understanding what each caches and
when it invalidates is important for explaining search performance.

### Query Cache (Node-Level, Per-Segment)

- **What**: Caches the results of `filter` clauses as bitsets (bit arrays where
  bit N = 1 means doc N matches the filter)
- **Scope**: Per segment, per node
- **Key**: The filter query itself
- **Invalidation**: When the segment is merged (segment is immutable, so if the
  segment exists, the cached bitset is still valid)
- **When used**: Only for `filter` context queries (not `must`/`should` which
  need scoring)

```
Query: { "bool": { "filter": { "term": { "status": "active" } } } }

Segment 0: status=active → bitset [1,0,1,1,0,0,1,0,...]  ← CACHED
Segment 1: status=active → bitset [0,1,1,0,1,0,0,1,...]  ← CACHED
Segment 2: (newly created) → not yet cached, compute on first query

After segments 0+1 merge into segment 3:
  Segment 0 cache → evicted (segment deleted)
  Segment 1 cache → evicted (segment deleted)
  Segment 3 → computed fresh on next query
```

This is why `filter` is faster than `must` for structured conditions — the
bitset is cached and reused across queries. A price range filter used by
thousands of search requests computes only once per segment.

### Request Cache (Shard-Level)

- **What**: Caches the entire shard-level response for a search request
- **Scope**: Per shard
- **Key**: The full request body (JSON)
- **Invalidation**: When the shard's data changes (any index, update, or delete)
  — specifically, on the next refresh
- **When used**: Only for requests with `size=0` (aggregation-only) by default;
  can be enabled for other requests via `request_cache=true`

This is extremely effective for dashboard queries. A dashboard showing
"orders per day for the last 90 days" sends the same aggregation request
repeatedly. After the first execution, subsequent requests hit the cache
(until new data is indexed and a refresh occurs).

### Field Data Cache (Node-Level, In-Heap)

- **What**: Un-inverts the inverted index for `text` fields to support
  aggregations and sorting
- **Scope**: Per segment, per node, lives on JVM heap
- **When used**: Only when aggregating or sorting on `text` fields
- **Problem**: Can consume enormous heap, cause GC pressure, OOM

**Avoid field data cache entirely.** Map string fields as `keyword` (or use
multi-fields) so aggregations use doc values (off-heap, memory-mapped) instead
of field data (on-heap, expensive).

### Why Segment Immutability Makes Caching Efficient

Lucene segments are immutable — once written, they never change. A segment
can only be deleted (after merging into a larger segment). This means:

1. **No invalidation problem**: A cached result for a segment is valid forever
   (until the segment is merged away). No need to track "has this segment's
   data changed?" — it hasn't, by definition.
2. **No cache coherence problem**: Multiple nodes caching the same segment's
   filter results will always agree — the underlying data is identical and
   immutable.
3. **Predictable cache lifecycle**: Caches are built up after segment creation
   and evicted after segment merge. No complex invalidation logic.

This is fundamentally different from caching in a mutable data store (like
a database buffer pool) where any write can invalidate cached reads.

```
Timeline:
  t0: Segment A created → cache empty
  t1: Query hits Segment A → result computed and cached
  t2: Same query hits Segment A → CACHE HIT (segment unchanged, guaranteed)
  t3: 100 more queries → all CACHE HIT
  t4: Segment A merged into Segment D → Segment A cache evicted
  t5: Query hits Segment D → result computed and cached (fresh)
```

---

## Contrast with Solr

Both OpenSearch and Solr are built on Apache Lucene. They share the same
foundational data structures (inverted index, BKD trees, doc values, segments).
The differences are in the distributed layer, API design, and ecosystem.

| Dimension | OpenSearch / Elasticsearch | Solr |
|-----------|--------------------------|------|
| **API** | REST + JSON natively. Clean, consistent. | XML-based historically, REST added later. Less uniform. |
| **Distribution** | Built-in from day one. Shard routing, rebalancing, replica management are core. | SolrCloud added later (ZooKeeper-dependent). Feels bolted on. |
| **Aggregations** | Rich aggregation DSL (buckets, metrics, pipeline). First-class feature. | Faceting (simpler). Stats component. Less composable. |
| **Real-time** | Near-real-time by default (1s refresh). Designed for streaming data. | NRT available but historically batch-oriented. |
| **Schema** | Dynamic mapping, schema-on-write. Flexible. | Schema.xml — explicit, more rigid. |
| **Ecosystem** | Kibana/OpenSearch Dashboards, Logstash/Data Prepper, Beats. Dominant in observability. | Banana (Kibana port), limited ecosystem. |
| **Cluster management** | Automatic shard rebalancing, split-brain protection, rolling upgrades. | ZooKeeper dependency for coordination. More operational overhead. |
| **Adoption** | Dominant. De facto standard for log analytics and application search. | Strong in traditional enterprise search (e-commerce catalogs, library systems). Declining share. |

**Why OpenSearch won adoption**: Simpler REST API (JSON in, JSON out),
distributed architecture that works out of the box without ZooKeeper, richer
aggregations that replaced the need for separate analytics tools, and a
strong ecosystem (ELK/OpenSearch stack) that solved the full observability
pipeline. Solr's XML heritage, ZooKeeper dependency, and simpler faceting
model made it feel like a previous generation's tool.

**When Solr still makes sense**: Solr has stronger XML/rich-document processing
(Tika integration), more mature multi-tenancy (collections), and certain
advanced search features (joins, grouping) that were historically better.
For pure enterprise document search without analytics needs, Solr remains
viable.

---

## Interview Cheat Sheet

**"Walk me through what happens when a user searches on an e-commerce site."**

1. Client sends `POST /products/_search` with a `bool` query (text match in
   `must`, price/category filters in `filter`, boost by popularity via
   `function_score`)
2. Coordinating node scatters query to one copy of each shard (adaptive
   replica selection)
3. Each shard: runs query against all Lucene segments, scores with BM25,
   applies function_score modifiers, returns top-N doc IDs + scores
4. Coordinator: merges shard results in global priority queue, identifies
   final top-N
5. Coordinator: multi-gets full `_source` from relevant shards (fetch phase)
6. Response includes hits + any aggregations (faceted navigation: category
   counts, price ranges, brand filters)

**Key numbers to cite:**
- BM25 defaults: k1=1.2, b=0.75
- HyperLogLog cardinality: ~4KB memory, <2% error
- Query cache: per-segment bitsets, invalidated only on merge
- Deep pagination: `from + size` results per shard per query — use `search_after`
