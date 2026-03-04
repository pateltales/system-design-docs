# System Design Interview Simulation: View Counter System (YouTube-like)

> **Interviewer:** Principal Engineer (L8)
> **Candidate Level:** L6 — SDE-3 (Senior SDE)
> **Duration:** ~60 minutes
> **Date:** 2024-02-28

---

## PHASE 1: Opening & Problem Statement (~2 min)

---

**Interviewer:** Thanks for joining. Today I'd like you to design a view counter system — think of the view count you see below every YouTube video. How would you build a system that tracks and displays how many times a video has been watched?

**Candidate:**

> "Great problem — it's deceptively simple on the surface but has a lot of interesting depth around write scalability, consistency tradeoffs, and fraud detection. Before I dive in, let me spend a couple minutes gathering requirements so I'm designing the right system."

---

## PHASE 2: Requirements Gathering & Scoping (~8 min)

---

**Interviewer:** Go ahead — what do you want to clarify?

**Candidate:**

> "Let me break this into functional requirements, non-functional requirements, and scale numbers."
>
> **Functional Requirements — what does the system do?**
>
> 1. **Record a view event** — when a user watches a video, increment the count. A "view" likely requires a minimum watch duration (e.g., 30 seconds) to prevent drive-by inflation.
> 2. **Read view count** — return the current total view count for a video. This is the number shown below the player.
> 3. **Batch read** — return view counts for multiple videos at once (homepage, search results, recommendation feeds all show 20-50 thumbnails with view counts).
> 4. **Real-time analytics** — creators need a dashboard showing views over time (per minute/hour/day) for the last 48 hours. Think YouTube Studio's "Realtime" tab.
> 5. **Trending** — rank videos by view velocity (views per hour), not just total views. This powers the Trending page.
> 6. **Fraud detection** — bots inflate view counts to manipulate trending and ad revenue. The system must validate views and subtract fraudulent ones retroactively.
>
> I want to call out that fraud detection isn't an afterthought — it's a first-class concern. Without it, view counts are meaningless because they drive ad revenue (CPM-based monetization). YouTube reportedly rejects 15-20% of views as invalid.
>
> **Non-Functional Requirements:**
>
> | Requirement | Target | Rationale |
> |---|---|---|
> | **Write throughput** | ~800K events/sec (peak) | YouTube scale — every video play is a write event |
> | **Read latency** | < 10ms (single), < 50ms (batch) | View counts shown on every page load |
> | **Availability** | 99.99% | Core platform feature — if counts are down, the site looks broken |
> | **Consistency** | Eventual (seconds to minutes lag) | Users don't notice if count is a few seconds stale |
> | **Durability** | Counts must never be lost permanently | But losing a few seconds of in-flight counts during a crash is acceptable |
> | **Accuracy** | Approximate is fine for display | Exact accuracy matters for monetization — reconcile from event log |
>
> **Scale Numbers (back of envelope):**
>
> - **Videos on platform:** ~800 million
> - **Daily views:** ~40 billion → ~500K/sec average, ~800K/sec peak
> - **View event size:** ~200 bytes (videoId, userId, timestamp, device, country, referrer, watchDuration)
> - **Raw event storage:** 200B × 40B/day = **8 TB/day**, **~3 PB/year**
> - **Counter storage:** 800M videos × 8 bytes = **6.4 GB** — fits in a single Redis instance. With sharding metadata and analytics rollups, budget ~100 GB.
> - **Read QPS for view counts:** Homepage/search/recommendations = ~5M reads/sec
>
> This is fundamentally a **write-heavy** system. The read path is simple (return a cached number); the write path is where all the complexity lives.

**Interviewer:** Good scoping. You mentioned eventual consistency — can you give me a concrete scenario where that's fine and one where it might cause problems?

**Candidate:**

> "Sure. **Where it's fine:** I watch a BTS music video. The count shows '1.2B views.' Whether it's actually 1,200,000,000 or 1,200,003,247 right now doesn't matter — it updates within seconds. Nobody notices or cares.
>
> **Where it could cause problems:** Ad revenue calculation. If we're paying creators per view, a few seconds of lag in the count is fine, but we need an eventually-correct source of truth. That's why I'd keep the raw event log in Kafka and have a batch reconciliation job that computes exact counts from the log. The real-time counter is the fast path; the event log is the source of truth."

### L5 vs L6 vs L7 — Phase 2 Rubric

| Aspect | L5 (SDE-2) | L6 (SDE-3) | L7 (Principal) |
|---|---|---|---|
| **Requirements** | Lists basic read/write. Misses fraud detection or analytics | Covers all functional + non-functional. Identifies fraud as first-class concern | All of L6 + frames requirements in terms of business impact (ad revenue, creator trust, platform integrity) |
| **Scale numbers** | Gives rough estimates, some math errors | Precise back-of-envelope. Correctly identifies write-heavy nature | All of L6 + compares to analogous systems (ad click counting, Photon paper), sizes each storage tier |
| **Consistency** | Says "eventual consistency" without nuance | Distinguishes display (approximate OK) vs monetization (exact needed). Proposes dual-path (fast counter + event log) | All of L6 + discusses consistency guarantees per consumer (viewer, creator, advertiser) and how each path enforces its guarantee |
| **Proactive tradeoffs** | Waits for interviewer to ask | Volunteers the approximate vs exact tension before asked | Frames the entire system around the approximate/exact duality. References Google Photon paper for exactly-once ad counting |

---

## PHASE 3: API Design (~5 min)

---

**Interviewer:** Let's design the key APIs. What are the most important endpoints?

**Candidate:**

> "I'll focus on the three most critical API surfaces — the full API reference has more, but these are the ones that shape the architecture."
>
> **1. Record a View (Write Path)**
> ```
> POST /v1/videos/{videoId}/view
> X-Idempotency-Key: <client-generated-uuid>
>
> {
>   "userId": "user_789",          // null for anonymous
>   "sessionId": "sess_456",
>   "timestamp": 1709078400000,    // client event time
>   "deviceType": "mobile",
>   "country": "US",
>   "referrer": "search",
>   "watchDurationSeconds": 47,
>   "videoDurationSeconds": 240
> }
> ```
> Response: `202 Accepted` — fire-and-forget. The event is queued, not yet counted.
>
> Key design decisions here:
> - **202, not 200** — the view goes into Kafka, not directly to the database. Async processing.
> - **Idempotency key** — network retries must not double-count. Client generates a UUID per view event. Server deduplicates within a TTL window (e.g., 5 minutes).
> - **watchDurationSeconds** — the server validates: must be ≥ 30s or ≥ 50% of video length, whichever is shorter. Anything less isn't a "real" view.
>
> **2. Read View Count (Read Path)**
> ```
> GET /v1/videos/{videoId}/viewCount
> → { "viewCount": 1284739201, "formattedCount": "1.2B views" }
>
> GET /v1/videos/batch/viewCounts?ids=abc,def,ghi,...
> → { "counts": [{ "videoId": "abc", "viewCount": 1284739201 }, ...] }
> ```
> - Single: < 10ms. CDN-cacheable, 30s TTL.
> - Batch: < 50ms for 50 videos. Backed by Redis `MGET`.
>
> **3. Real-Time Analytics (Creator Dashboard)**
> ```
> GET /v1/videos/{videoId}/analytics/realtime
> → { "dataPoints": [{ "timestamp": "...", "views": 342 }, ...], "granularity": "minute" }
> ```
> - Minute-level granularity, last 48 hours.
> - Higher latency acceptable (< 500ms). Different storage backend than the counter.
>
> *For the full API reference including trending, demographics, and admin APIs, see [02-api-design.md](02-api-design.md).*

**Interviewer:** Why `202 Accepted` instead of `200 OK`? What if the event gets lost after you return 202?

**Candidate:**

> "Two reasons for 202. First, **latency** — if we synchronously wrote to a database before responding, p99 latency would spike during traffic bursts. We want the client to fire and forget. Second, **decoupling** — the ingestion layer shouldn't know or care about the processing pipeline behind it.
>
> On event loss: Kafka provides durability. The API server publishes to Kafka with `acks=all` (waits for all in-sync replicas to acknowledge). Once Kafka acks, the event is durable. The risk window is between the 202 response and the Kafka ack — if the API server crashes in that window, we lose the event. But that's a microsecond window, and at 800K views/sec, losing a handful of events per server crash is acceptable. For monetization, we reconcile from Kafka's event log anyway."

### L5 vs L6 vs L7 — Phase 3 Rubric

| Aspect | L5 (SDE-2) | L6 (SDE-3) | L7 (Principal) |
|---|---|---|---|
| **API design** | Basic REST endpoints, missing idempotency | 202 Accepted, idempotency key, batch endpoints, analytics. Explains each design choice | All of L6 + discusses API versioning strategy, backward compatibility, and client SDK design |
| **Async vs sync** | Defaults to synchronous writes | Proposes async (Kafka-backed) with clear justification. Analyzes the failure window | Discusses exactly-once delivery guarantees end-to-end, references Kafka idempotent producer |
| **Validation** | Doesn't mention view validation | watchDuration threshold, fraud signals in payload | Designs the validation as a pipeline with multiple stages, each with different latency guarantees |

---

## PHASE 4: High-Level Architecture (~15 min)

---

**Interviewer:** Great. Let's build the architecture. Start simple and evolve it.

### Attempt 0: Single Server (Naive)

**Candidate:**

> "Let me start with the simplest possible design and find problems."
>
> ```
> ┌──────────┐       ┌──────────────────────┐       ┌──────────┐
> │  Client  │──────→│  Application Server  │──────→│  MySQL   │
> │ (Player) │       │  POST /view → INSERT │       │  (views  │
> └──────────┘       │  GET /count → SELECT │       │   table) │
>                    └──────────────────────┘       └──────────┘
> ```
>
> - `POST /view` → `UPDATE videos SET view_count = view_count + 1 WHERE id = ?`
> - `GET /viewCount` → `SELECT view_count FROM videos WHERE id = ?`
>
> **Problems:**
> 1. **Write bottleneck**: Every view = 1 DB write. At 800K/sec, MySQL can't keep up (~10K writes/sec per instance). We'd need 80+ MySQL instances just for counters.
> 2. **Hot row lock contention**: Viral video = millions of concurrent `UPDATE` statements on the same row. Row-level locking serializes all writes. Throughput collapses.
> 3. **No durability guarantee**: If the DB is down, we lose views entirely. No buffering.
> 4. **Coupled read/write**: Read queries compete with write queries on the same DB.
> 5. **No fraud detection**: We're blindly counting every request.

**Interviewer:** Good analysis. How do you fix the write bottleneck?

### Attempt 1: Add Kafka as Write Buffer

**Candidate:**

> "The fundamental insight is: **decouple ingestion from processing.** The client doesn't need to know when the count updates — it just needs to know the event was received. So I'll put Kafka between the API and the database."
>
> ```
> ┌──────────┐     ┌─────────────┐     ┌─────────────────┐     ┌─────────────┐     ┌──────────┐
> │  Client  │────→│ API Gateway │────→│  Kafka Cluster  │────→│  Consumer   │────→│  Redis   │
> │ (Player) │     │ (202 Accept)│     │ (view-events    │     │  (aggregate │     │ (counter │
> └──────────┘     └─────────────┘     │  topic)         │     │   + flush)  │     │  store)  │
>                                      └─────────────────┘     └─────────────┘     └──────────┘
> ```
>
> **How it works:**
> 1. Client sends view event → API Gateway validates and publishes to Kafka → returns 202.
> 2. Kafka topic `view-events` partitioned by `hash(videoId)` — ensures ordering per video.
> 3. Consumer group reads events, **aggregates counts in memory** (micro-batching).
>    - Instead of 1 DB write per event, the consumer buffers: "video abc got +3,247 views in the last 5 seconds."
>    - Flushes aggregated delta to Redis every 5 seconds: `INCRBY video:abc:count 3247`
> 4. Redis serves read queries: `GET video:abc:count` → returns count in < 1ms.
>
> **Why Kafka?**
> - Absorbs write spikes. Kafka can handle millions of events/sec per cluster.
> - If consumers fall behind, events just queue up — no backpressure to clients.
> - Events are durable (replicated across brokers). If a consumer crashes, it replays from the last committed offset.
>
> **Improvement over Attempt 0:**
>
> | Metric | Attempt 0 | Attempt 1 |
> |---|---|---|
> | DB writes/sec | 800K (1 per event) | ~800 (1 per video per 5s flush) |
> | Write reduction | 1x | **~1000x** |
> | Client latency | Blocked on DB write | 202 immediate, < 5ms |
> | Crash recovery | Lost views | Replay from Kafka |
>
> **Remaining problems:**
> 1. **Viral video (hot key)**: One video = one Kafka partition = one consumer. A single consumer can't keep up with a viral video getting 100K events/sec.
> 2. **No fraud detection**: Still counting every event blindly.
> 3. **Single region**: All traffic to one region.

**Interviewer:** Let's focus on the hot key problem. What happens when a video goes viral?

### Attempt 2: Counter Sharding for Hot Keys

**Candidate:**

> "The hot key problem is the defining challenge of this system. Let me address it at two levels: Kafka consumption and Redis storage.
>
> **Kafka level:** A single consumer processing a hot partition can become a bottleneck. Solutions:
> - **Sub-partitioning**: Instead of `hash(videoId) % N`, use `hash(videoId + randomSalt) % N` for detected hot videos. This spreads one video's events across multiple partitions/consumers.
> - **Local aggregation on API servers**: Before even publishing to Kafka, each API server maintains an in-memory counter per hot videoId. Flush to Kafka every 1-2 seconds with an aggregated event: 'video abc got +847 views from this server.' With 100 API servers, this reduces Kafka events by 100x.
>
> **Redis level:** Even with reduced write frequency, one Redis key per video means one shard handles all writes for a viral video. Solution — **counter sharding**:
>
> ```
> Instead of:  video:abc:count = 1,284,739,201
>
> Use:         video:abc:count:0  = 12,847,392
>              video:abc:count:1  = 12,847,391
>              ...
>              video:abc:count:99 = 12,847,394
>
> Write: INCRBY video:abc:count:{random(0,99)} delta
> Read:  SUM(MGET video:abc:count:0 ... video:abc:count:99)
> ```
>
> - **Writes** go to a random shard → distributes across Redis cluster nodes.
> - **Reads** sum all shards → slightly more expensive, but view count reads are cached at CDN anyway.
> - **Dynamic sharding**: Not all videos need 100 shards. Monitor write rate per video. Default to 1 shard, auto-scale to 10/50/100 shards when a video is detected as 'hot' (> 1K writes/sec).
>
> Updated architecture:
>
> ```
> ┌──────────┐     ┌─────────────┐     ┌─────────────────┐     ┌──────────────┐     ┌───────────────┐
> │  Client  │────→│ API Gateway │────→│  Kafka Cluster  │────→│  Consumers   │────→│ Redis Cluster │
> │ (Player) │     │ + local     │     │ (view-events)   │     │ (aggregate   │     │ (sharded      │
> └──────────┘     │   counter   │     │                 │     │  per video)  │     │  counters)    │
>                  │   buffer    │     │                 │     │              │     │               │
>                  └─────────────┘     └─────────────────┘     └──────────────┘     └───────────────┘
>                       │                                             │
>                       │ hot key detection                           │ flush aggregated deltas
>                       │ (local aggregation)                         │ every 5 seconds
>                       ▼                                             ▼
>                  ┌─────────────┐                              ┌───────────────┐
>                  │ Hot Key     │                              │ Cassandra /   │
>                  │ Detector    │                              │ MySQL         │
>                  │ (rate       │                              │ (durable      │
>                  │  monitor)   │                              │  store)       │
>                  └─────────────┘                              └───────────────┘
> ```
>
> The Cassandra/MySQL layer is the durable store. Redis is the fast read cache. Consumers write to both:
> - Redis: real-time counter (INCRBY, fast, may lose data on crash)
> - Cassandra: durable append (async, for reconciliation)
>
> If Redis loses data (node failure), we rebuild from Cassandra + replay recent Kafka events."

### Architecture Evolution — Phase 4

| Component | Attempt 0 | Attempt 1 | Attempt 2 |
|---|---|---|---|
| **Ingestion** | Sync DB write | Kafka buffer | Kafka + local aggregation on API servers |
| **Processing** | None (direct write) | Consumer micro-batch | Consumer micro-batch + hot key detection |
| **Counter store** | MySQL row | Redis key | Redis sharded counters (dynamic sharding) |
| **Durable store** | MySQL | Redis (not durable) | Redis + Cassandra (dual write) |
| **Hot key handling** | None (row lock death) | None (single consumer bottleneck) | Counter sharding + local aggregation |
| **Fraud detection** | None | None | Not yet (next phase) |

### L5 vs L6 vs L7 — Phase 4 Rubric

| Aspect | L5 (SDE-2) | L6 (SDE-3) | L7 (Principal) |
|---|---|---|---|
| **Architecture evolution** | Jumps to final design. Doesn't show the journey | Starts naive, identifies specific bottlenecks, evolves step by step with quantified improvements | All of L6 + shows how each decision closes one problem but opens another. Maps the decision tree |
| **Hot key handling** | "Use sharding" without specifics | Counter sharding with concrete shard count, dynamic scaling, read/write cost analysis | All of L6 + discusses auto-detection algorithms, shard rebalancing during traffic shifts, and monitoring the effectiveness |
| **Write path** | "Write to database" or "Use a queue" | Kafka + micro-batching with quantified write reduction (1000x). Discusses crash recovery via offset replay | All of L6 + discusses exactly-once semantics, idempotent producers, transactional consumers, and compares Kafka vs Kinesis vs Pulsar |
| **Storage choices** | Picks one DB without justification | Hybrid: Redis (speed) + Cassandra (durability). Explains reconciliation | Discusses Redis persistence modes (RDB vs AOF), Cassandra counter column limitations (non-idempotent), and proposes compensating mechanisms |

---

## PHASE 5: Deep Dive — Write Path & Counting (~8 min)

---

**Interviewer:** Let's go deeper on the write path. Walk me through what happens from the moment a user clicks play to the count being updated.

**Candidate:**

> "End-to-end flow:
>
> ```
> User clicks play
>       │
>       ▼
> ┌─────────────────────────────────────────────────────────────────────────┐
> │ 1. CLIENT (video player)                                               │
> │    - Starts playback timer                                             │
> │    - At 30s (or 50% of video): fire view event with UUID idempotency   │
> │      key, device fingerprint, watch duration, visibility state          │
> └──────────────────────────────┬──────────────────────────────────────────┘
>                                │ POST /v1/videos/{id}/view
>                                ▼
> ┌─────────────────────────────────────────────────────────────────────────┐
> │ 2. API GATEWAY / Load Balancer                                         │
> │    - Rate limit check: per (IP, videoId) — max 5 views/hour            │
> │    - Client-side signal validation (User-Agent, TLS fingerprint)       │
> │    - Deduplicate: check idempotency key in Redis SET (TTL 5 min)       │
> │    - If hot video detected: aggregate locally, flush every 1-2s        │
> │    - Publish to Kafka topic `view-events` (partition = hash(videoId))  │
> │    - Return 202 Accepted                                               │
> └──────────────────────────────┬──────────────────────────────────────────┘
>                                │
>                                ▼
> ┌─────────────────────────────────────────────────────────────────────────┐
> │ 3. KAFKA CLUSTER                                                       │
> │    - Topic: view-events, 256 partitions, RF=3, acks=all                │
> │    - Retention: 7 days (allows replay for reconciliation)              │
> │    - Throughput: ~160 MB/sec (800K events × 200 bytes)                 │
> └───────────┬─────────────────────────────────┬──────────────────────────┘
>             │                                 │
>             ▼                                 ▼
> ┌───────────────────────────┐   ┌──────────────────────────────────────┐
> │ 4A. COUNTER CONSUMER      │   │ 4B. ANALYTICS CONSUMER               │
> │  - Read events from Kafka │   │  - Read same events (separate group) │
> │  - Aggregate in-memory    │   │  - Feed into Flink for time-windowed │
> │    per videoId             │   │    aggregation (minute/hour/day)     │
> │  - Every 5s: flush delta  │   │  - Write to ClickHouse (time-series)│
> │    to Redis (INCRBY)      │   │  - Powers real-time analytics API   │
> │  - Also write to          │   └──────────────────────────────────────┘
> │    Cassandra (durable)    │
> │  - Commit Kafka offset    │
> └───────────────────────────┘
> ```
>
> **Key design decisions in this flow:**
>
> 1. **Why aggregate in-memory before flushing?** At 800K events/sec, if each event = 1 Redis INCRBY, Redis would need ~800K ops/sec just for counters. Redis can handle ~100K ops/sec per shard. With in-memory aggregation (5s window), we reduce to ~160K flushes/sec across all videos (assuming ~800K unique videos getting views in any 5s window). Much more manageable.
>
> 2. **What if a consumer crashes before flushing?** We lose at most 5 seconds of in-flight aggregations. When the consumer restarts, it replays from the last committed Kafka offset. Events are reprocessed. The counter might double-count those 5 seconds. At YouTube scale, this is a rounding error. For monetization, the batch reconciliation job (runs daily) recomputes exact counts from the raw event log.
>
> 3. **Why two consumer groups?** The counter path and analytics path have different processing needs. Counter consumers are simple (aggregate + flush). Analytics consumers run complex windowed aggregations in Flink. Separate consumer groups let them scale independently and fail independently.
>
> 4. **Kafka partition count (256):** Back-of-envelope: 800K events/sec ÷ 256 partitions = ~3,125 events/sec per partition. Each consumer can handle ~10K events/sec easily. So 256 partitions with ~80-100 consumers gives plenty of headroom. We can increase partitions later if needed."

**Interviewer:** You mentioned micro-batching reduces Redis writes by 1000x. Walk me through that math.

**Candidate:**

> "Sure. Without batching: 800K events/sec = 800K Redis INCRBY/sec.
>
> With 5-second micro-batching: in any 5-second window, events are distributed across ~800K distinct videos (power law — most videos get few views, a few get millions). But the consumer aggregates per video, so it flushes one INCRBY per video that received views in that window.
>
> Assume ~200K unique videos get at least 1 view in any 5-second window (the long tail of 800M videos means most get 0 views in any 5s). That's 200K flushes every 5s = **40K Redis ops/sec**. Down from 800K — a **20x reduction**.
>
> For the top 1% of videos (viral), the reduction is much bigger. A video getting 100K views/sec becomes a single INCRBY of +500,000 every 5 seconds instead of 100K individual INCRBYs. That's a **100,000x reduction** for that video.
>
> The aggregate reduction depends on the distribution, but realistically it's **20x-1000x** depending on traffic patterns. The 1000x figure applies to the hottest keys, which are exactly the ones we care about most."

*For the full deep dive on the write path, see [03-deep-dive-write-path.md](03-deep-dive-write-path.md).*

### L5 vs L6 vs L7 — Phase 5 Rubric

| Aspect | L5 (SDE-2) | L6 (SDE-3) | L7 (Principal) |
|---|---|---|---|
| **End-to-end flow** | Describes steps but misses consumer design | Full pipeline with parallel consumer groups, clear separation of counter vs analytics paths | All of L6 + discusses backpressure handling, consumer lag alerting, auto-scaling consumers based on lag |
| **Micro-batching math** | "Batching reduces writes" without numbers | Quantifies reduction with power-law reasoning. Shows math for hot vs cold videos | All of L6 + models the write amplification at each tier and derives optimal flush interval as a function of video popularity |
| **Failure analysis** | "Use replication" | Analyzes crash-before-flush scenario, quantifies data loss, proposes offset replay + daily reconciliation | Discusses exactly-once vs at-least-once tradeoffs, Kafka transactional API, and when the operational complexity of exactly-once isn't worth it |

---

## PHASE 6: Deep Dive — Read Path & Caching (~8 min)

---

**Interviewer:** Now let's look at the read side. How do you serve view counts at 5 million reads/sec with < 10ms latency?

**Candidate:**

> "The read path is much simpler than the write path. Here's the strategy:
>
> ```
> ┌──────────┐     ┌─────────┐     ┌───────────────┐     ┌───────────────┐
> │  Client  │────→│   CDN   │────→│  API Server   │────→│ Redis Cluster │
> │          │     │ (edge   │     │ (cache miss    │     │ (source of    │
> │          │     │  cache) │     │  handler)      │     │  truth for    │
> │          │     │ TTL=30s │     │               │     │  real-time    │
> │          │◄────│         │◄────│               │◄────│  counts)      │
> └──────────┘     └─────────┘     └───────────────┘     └───────────────┘
> ```
>
> **Layer 1 — CDN Edge Cache (handles ~90% of reads):**
> - View counts for popular videos are cached at CDN edge nodes (CloudFront, Fastly, etc.).
> - TTL: 30 seconds. A view count that's 30 seconds stale is perfectly acceptable.
> - For the top 1% of videos (which account for 80% of views AND 80% of reads — power law), the CDN cache hit rate is extremely high.
> - 5M reads/sec × 90% cache hit = only 500K requests reach origin.
>
> **Layer 2 — Application Server:**
> - Handles CDN cache misses.
> - For single video: `GET video:{id}:count` from Redis → < 1ms.
> - For batch: `MGET video:{id1}:count video:{id2}:count ...` → single Redis round-trip → < 2ms for 50 keys.
> - For sharded counters (hot videos): sum all shards. Pre-computed by the aggregation pipeline and cached in a separate key `video:{id}:count:total`.
>
> **Layer 3 — Redis Cluster:**
> - 500K reads/sec after CDN. Across a 10-shard Redis cluster, that's 50K reads/shard — well within Redis capacity.
> - Data is always fresh because the write pipeline updates Redis directly (write-through). No cache invalidation logic needed.
>
> **Why this works so well:**
> - View counts are a **read-your-writes-unnecessary** workload. If I watch a video, I don't expect the count to increment immediately in front of my eyes.
> - The data is small (one integer per video). Fits entirely in memory.
> - Stale reads are fine. 30-second staleness is invisible to users.
>
> **Cache stampede protection:**
> Even with this architecture, I'd protect against cache stampedes on viral videos when the CDN cache entry expires. Three options:
>
> | Strategy | How it works | Tradeoff |
> |---|---|---|
> | **Lock-based refresh** | First request acquires a lock, refreshes cache. Others wait or get stale data | Adds latency for the first request; requires distributed lock |
> | **Probabilistic early expiration** | Each request has a small probability of refreshing before TTL expires | Wastes some compute but avoids thundering herd |
> | **Write-through (never expire)** | Counter pipeline always pushes updates to CDN/cache. No expiration | Requires active push; simplest and best for this use case |
>
> I'd use **write-through** here. The aggregation consumer already updates Redis every 5 seconds — extend it to push to CDN via cache purge/update API. View counts never expire; they're always pushed."

**Interviewer:** What about the analytics queries — views per hour for the last 30 days? That's a different read pattern.

**Candidate:**

> "Completely different. Analytics queries are **OLAP-style time-range scans**, not key-value lookups.
>
> ```
> ┌──────────────┐     ┌─────────────┐     ┌──────────────────┐
> │ YouTube      │────→│ Analytics   │────→│ ClickHouse       │
> │ Studio UI    │     │ API Server  │     │ (pre-aggregated  │
> │              │     │             │     │  rollup tables)  │
> └──────────────┘     └─────────────┘     └──────────────────┘
> ```
>
> - **Storage:** ClickHouse (columnar, excellent for time-series aggregation).
> - **Data:** Pre-aggregated rollup tables, not raw events.
>   - `views_per_minute` — kept for 48 hours (the Realtime tab)
>   - `views_per_hour` — kept for 90 days
>   - `views_per_day` — kept forever
> - **Query:** `SELECT sum(views) FROM views_per_day WHERE videoId = 'abc' AND date BETWEEN '2024-02-01' AND '2024-02-28'` → ClickHouse answers in < 100ms.
> - **Why not query raw events?** 40B events/day. A 30-day range = 1.2 trillion events. Scanning that is prohibitively slow (hours). Pre-aggregation makes it instant.
>
> The rollup pipeline (Flink) runs continuously, consuming from Kafka and writing to ClickHouse. It's the same event stream, just processed differently than the counter pipeline — that's why we have two consumer groups."

*For the full deep dive on the read path, see [04-deep-dive-read-path.md](04-deep-dive-read-path.md).*

### Architecture Update After Phase 6

| Component | Before (Phase 5) | After (Phase 6) | Why the Change |
|---|---|---|---|
| **Read path** | Direct Redis lookup | CDN → API → Redis (3-tier) | 90% of reads served from CDN edge, sub-10ms globally |
| **Analytics storage** | Not addressed | ClickHouse with rollup tables | OLAP queries need columnar store, not key-value |
| **Cache strategy** | Implicit (Redis is the cache) | Write-through from pipeline to CDN | Eliminates cache invalidation and stampede problems |

### L5 vs L6 vs L7 — Phase 6 Rubric

| Aspect | L5 (SDE-2) | L6 (SDE-3) | L7 (Principal) |
|---|---|---|---|
| **Caching** | "Add Redis cache" | Multi-layer (CDN + Redis), quantifies hit rates, explains write-through vs invalidation | All of L6 + discusses CDN push vs pull invalidation, regional cache coherence, and cache warming strategies |
| **Analytics** | "Query the database" | Separate OLAP store (ClickHouse), pre-aggregated rollups, explains why raw event scan is infeasible | All of L6 + compares Lambda vs Kappa architecture, discusses exactly-once in rollup computation, handles late-arriving events |
| **Consistency** | "It's eventually consistent" | Quantifies staleness (30s CDN, 5s Redis), explains why each consumer has different needs | Discusses per-user consistency (creator sees different freshness than viewer), designs consistency SLAs per API endpoint |

---

## PHASE 7: Deep Dive — Fraud Detection (~8 min)

---

**Interviewer:** You mentioned fraud detection early on. How does it work?

**Candidate:**

> "Fraud detection is a multi-stage pipeline. I think of it as a funnel — cheap, fast checks first, expensive ML analysis later. Each stage filters out a different class of bad traffic.
>
> ```
> View Event arrives
>       │
>       ▼
> ┌──────────────────────────────────────┐
> │ STAGE 1: Client-Side Signals         │  ← Real-time (< 5ms)
> │ • User-Agent validation              │
> │ • TLS fingerprint (JA3)              │
> │ • JavaScript execution check         │
> │ • reCAPTCHA score (if suspicious)    │
> │                                      │
> │ Filters: ~5% of traffic (crude bots) │
> └──────────────┬───────────────────────┘
>                │ passed
>                ▼
> ┌──────────────────────────────────────┐
> │ STAGE 2: Rate Limiting               │  ← Real-time (< 2ms)
> │ • Per (IP, videoId): max 5/hour      │
> │ • Per (userId, videoId): max 3/hour  │
> │ • Per IP global: max 500/hour        │
> │ • Sliding window counter in Redis    │
> │                                      │
> │ Filters: ~3% of traffic              │
> └──────────────┬───────────────────────┘
>                │ passed
>                ▼
> ┌──────────────────────────────────────┐
> │ STAGE 3: Watch Behavior Validation   │  ← Near real-time (at event processing)
> │ • watchDuration ≥ 30s or ≥ 50%      │
> │ • Page Visibility API: tab visible?  │
> │ • User interaction signals           │
> │ • Heartbeat pattern analysis         │
> │                                      │
> │ Filters: ~7% of traffic              │
> └──────────────┬───────────────────────┘
>                │ passed → counted as valid view
>                │
>                ▼ (event also goes to batch analysis)
> ┌──────────────────────────────────────┐
> │ STAGE 4: Batch Fraud Analysis        │  ← Offline (daily/weekly Spark jobs)
> │ • ML models on millions of events    │
> │ • Features:                          │
> │   - Geographic clustering            │
> │   - Temporal patterns (bot cadence)  │
> │   - Device fingerprint clustering    │
> │   - View-to-engagement ratio         │
> │ • Result: retroactive subtraction    │
> │                                      │
> │ Catches: ~5% of remaining traffic    │
> └──────────────────────────────────────┘
> ```
>
> **Total rejection rate: ~15-20%** — consistent with YouTube's reported numbers.
>
> The key architectural insight is that **stages 1-3 are inline** (happen before the view is counted) while **stage 4 is offline** (subtracts views retroactively). This means:
>
> - Real-time counts are slightly inflated (include some fraud that stage 4 hasn't caught yet).
> - Daily batch jobs correct the counts downward. Users sometimes notice view counts decreasing — this is why.
> - The creator dashboard shows a note like 'view counts may be adjusted.'
>
> **Historical context:** YouTube used to freeze new video counts at 301 views while running fraud analysis. Once validated, the count would jump to the real number. They removed this around 2015 in favor of the real-time validation + retroactive subtraction approach I described. Better UX — no more mysterious '301 freeze.'
>
> **Retroactive adjustment must be atomic.** When batch analysis says 'subtract 142,839 views from video abc,' we can't just `DECRBY` — if the read happens between individual shard decrements, the count would be inconsistent. Instead, we:
> 1. Compute the new total.
> 2. Atomically swap the counter to the new value.
> 3. Write an audit log entry (who, when, how much, why).
> 4. Update the durable store (Cassandra) in the same transaction."

**Interviewer:** How do you detect sophisticated bots that mimic human behavior? Simple rate limiting won't catch those.

**Candidate:**

> "That's stage 4 — the ML-based detection. Sophisticated bots pass individual checks but fail when you analyze patterns at scale.
>
> **Feature engineering for the ML model:**
>
> | Feature | What it detects | Example |
> |---|---|---|
> | **Geographic concentration** | All views from one IP range/ASN | 50K views, all from a single /24 subnet |
> | **Temporal pattern** | Views at exact intervals | Views arriving every 3.0 seconds (humans are jittery) |
> | **Device fingerprint clustering** | Same browser config across 'different' users | 1000 'unique' users with identical canvas fingerprint |
> | **View-to-engagement ratio** | Views without any other interaction | 100K views but 0 likes, 0 comments, 0 subscribers |
> | **Session behavior** | Unrealistic browsing patterns | User watches 500 videos in 1 hour, 0 pauses |
> | **Watch duration distribution** | Bots watch exact same duration | All views exactly 31 seconds (just above threshold) |
>
> The model (gradient boosted trees — XGBoost/LightGBM — work well for tabular features like this) is trained on labeled data: views that were confirmed as bot traffic (from manual investigations) vs confirmed human traffic. It runs daily on the previous day's events and produces a set of view event IDs to subtract."

*For the full deep dive on fraud detection, see [05-deep-dive-fraud-detection.md](05-deep-dive-fraud-detection.md).*

### L5 vs L6 vs L7 — Phase 7 Rubric

| Aspect | L5 (SDE-2) | L6 (SDE-3) | L7 (Principal) |
|---|---|---|---|
| **Fraud awareness** | Mentions "add rate limiting" as an afterthought | Multi-stage pipeline with quantified rejection rates at each stage. Discusses inline vs offline tradeoff | All of L6 + discusses adversarial ML (bot operators adapting to detection), A/B testing fraud models, and the economics of fraud (cost to attack vs cost to defend) |
| **ML detection** | "Use ML to detect bots" (hand-wave) | Specific features, model type (GBT), training data source, batch vs online inference decision | All of L6 + discusses feature drift, model retraining cadence, false positive impact on legitimate creators, and appeals process |
| **Retroactive adjustment** | Doesn't consider it | Atomic swap, audit trail, consistency during adjustment | Designs the full adjustment pipeline including creator notification, revenue clawback implications, and regulatory compliance |

---

## PHASE 8: Deep Dive — Scale & Storage (~5 min)

---

**Interviewer:** Let's talk storage tiers. You mentioned Redis, Cassandra, and raw events. How do you organize the data lifecycle?

**Candidate:**

> "I think of this as a three-tier storage architecture, mirroring how the data cools over time:
>
> | Tier | Store | What's Stored | Retention | Latency | Cost |
> |---|---|---|---|---|---|
> | **Hot** | Redis Cluster | Current view counts (all 800M videos) | Forever (always current) | < 1ms | $$$ (memory) |
> | **Warm** | ClickHouse | Hourly/daily aggregates per video | Forever | < 100ms | $$ (SSD) |
> | **Cold** | S3 + Parquet | Raw view events | 90 days raw, summaries forever | Seconds (query via Athena) | $ (object storage) |
>
> **Cost optimization insight:** Power law distribution. ~1% of videos (8M) account for ~80% of all views. These are the only videos that need sharded counters, CDN cache priority, and hot-key detection. The remaining 792M videos get a few views per day and a single Redis key each.
>
> **Total Redis memory:** 800M videos × 8 bytes = 6.4 GB for counters. With metadata, sharded counters for hot videos, and overhead: ~100 GB total. A 10-node Redis cluster with 16 GB per node handles this easily.
>
> **Cassandra capacity:** Hourly aggregates: 800M videos × 24 hours × 365 days × ~50 bytes = ~350 TB/year. With RF=3 and compression: ~200 TB cluster. Grows linearly — add nodes as needed.
>
> **S3 cost:** 8 TB/day × 90 days = 720 TB. At $0.023/GB/month: ~$16,500/month. Very cheap for the raw event log that serves as source of truth."

**Interviewer:** What about replication and durability for Redis? You said it's the real-time source of truth.

**Candidate:**

> "Redis is the real-time source of truth for *display*, not for *billing*. Important distinction.
>
> **Redis replication:** Master-replica with Redis Cluster (automatic failover). Replication is asynchronous — if a master fails, the replica may be a few hundred milliseconds behind. At 800K views/sec, that's maybe ~160K events lost during failover.
>
> **Is that acceptable?** For display counts — yes, absolutely. 160K views on a platform doing 40B/day is a rounding error. Nobody will notice.
>
> **For billing/monetization** — no. We reconcile from Kafka. The raw event log in Kafka (7-day retention, replicated) is the durable source of truth. A daily Spark job recomputes exact counts from the event log and reconciles with Redis/Cassandra. Any discrepancy is corrected.
>
> So the durability model is:
> - **Kafka** → source of truth (7-day window, then archived to S3)
> - **Redis** → fast read cache, may lose seconds of data on failure
> - **Cassandra** → durable counter store, always eventually consistent with Kafka
> - **S3** → permanent archive, queryable for historical analysis"

*For the full deep dive on scale and storage, see [06-deep-dive-scale-and-storage.md](06-deep-dive-scale-and-storage.md).*

---

## PHASE 9: Deep Dive — Global Distribution (~5 min)

---

**Interviewer:** This is a global platform. How do you handle multi-region?

**Candidate:**

> "YouTube serves users on every continent. Three concerns: ingestion latency, global count consistency, and data sovereignty.
>
> **Multi-region ingestion:**
> ```
> ┌─────────────────────────────────────────────────────────────────────────────┐
> │                          Global Architecture                               │
> │                                                                            │
> │  US-East           EU-West           APAC                                  │
> │  ┌──────────┐     ┌──────────┐     ┌──────────┐                           │
> │  │ API GW   │     │ API GW   │     │ API GW   │   ← Users hit nearest    │
> │  │ + Kafka  │     │ + Kafka  │     │ + Kafka  │     region (GeoDNS)       │
> │  │ + Local  │     │ + Local  │     │ + Local  │                           │
> │  │ Consumer │     │ Consumer │     │ Consumer │                           │
> │  │ + Redis  │     │ + Redis  │     │ + Redis  │   ← Regional counters    │
> │  └────┬─────┘     └────┬─────┘     └────┬─────┘                           │
> │       │                │                │                                  │
> │       └────────────────┼────────────────┘                                  │
> │                        │                                                   │
> │                        ▼                                                   │
> │              ┌──────────────────┐                                          │
> │              │ Global Aggregator│  ← Merges regional deltas               │
> │              │ (US-East primary)│    every 5-10 seconds                    │
> │              │                  │                                          │
> │              │ Global Redis     │  ← Authoritative global count           │
> │              │ Global Cassandra │                                          │
> │              └──────────────────┘                                          │
> └─────────────────────────────────────────────────────────────────────────────┘
> ```
>
> **How it works:**
> 1. Each region has its own API Gateway, Kafka cluster, consumers, and Redis.
> 2. Regional consumers aggregate locally and produce a stream of deltas: 'video abc got +12,847 views in EU-West in the last 5 seconds.'
> 3. A global aggregator (in the primary region) consumes deltas from all regions and updates the global count.
> 4. Global count is replicated back to regional Redis instances for serving reads locally.
>
> **Consistency model:** I'd use a **hybrid CRDT-like approach**:
> - Each region maintains a **regional counter** (grow-only, eventually consistent).
> - Global count = sum of all regional counters.
> - Regional Redis caches the global count (refreshed every 5-10 seconds).
> - Users in EU see a count that's at most ~10 seconds stale. Perfectly fine.
>
> **Why not a centralized counter?** Cross-region writes add 100-200ms latency. With batching (send deltas every 5s), this is fine for the aggregation path. But regional Redis serving local reads must be fast (< 1ms). So we maintain local copies.
>
> **Failover:** If EU-West goes down:
> 1. DNS routes EU traffic to US-East (higher latency, but available).
> 2. EU-West's Kafka events are replicated to US-East via MirrorMaker 2.
> 3. When EU-West recovers, it replays from its Kafka log and catches up.
> 4. View counts may diverge slightly during failover — reconcile on recovery.
>
> **Data sovereignty (GDPR):**
> - Raw view events containing user PII (userId, IP) stay in the region of origin.
> - Only aggregated, anonymous counts (deltas) cross region boundaries.
> - This satisfies GDPR: personal data doesn't leave the EU."

*For the full deep dive on global distribution, see [08-deep-dive-global-distribution.md](08-deep-dive-global-distribution.md).*

### L5 vs L6 vs L7 — Phase 9 Rubric

| Aspect | L5 (SDE-2) | L6 (SDE-3) | L7 (Principal) |
|---|---|---|---|
| **Multi-region** | "Deploy in multiple regions" without details | Regional ingestion + global aggregation. CRDT-like approach. Quantifies staleness. Discusses failover | All of L6 + discusses active-active vs active-passive tradeoffs, split-brain scenarios, and network partition handling |
| **Data sovereignty** | Doesn't mention | Identifies GDPR. Separates PII (stays local) from aggregates (cross-region) | All of L6 + designs the data classification system, discusses right-to-deletion for view events, and audit logging for compliance |
| **Consistency** | "Eventually consistent" | Quantifies regional staleness (5-10s). Explains hybrid approach | Formal consistency model: CRDTs (G-Counter), proves convergence, discusses anti-entropy protocol for counter reconciliation |

---

## PHASE 10: Wrap-Up & Summary (~5 min)

---

**Interviewer:** Good. Let's wrap up. Can you summarize the overall architecture and tell me what keeps you up at night?

**Candidate:**

> "Here's the final architecture evolution:
>
> ### Design Evolution
>
> | Attempt | Architecture | Key Improvement |
> |---|---|---|
> | 0 | Single server + MySQL | Starting point — direct DB writes |
> | 1 | Kafka buffer + Redis | Decoupled ingestion from processing, 1000x write reduction |
> | 2 | Counter sharding + local aggregation | Solved hot key problem for viral videos |
> | 3 | Multi-stage fraud pipeline | 15-20% invalid views filtered, retroactive correction |
> | 4 | CDN + write-through caching | 90% read reduction, sub-10ms global read latency |
> | 5 | ClickHouse + rollup pipeline | Analytics queries in < 100ms on pre-aggregated data |
> | 6 | Multi-region with global aggregation | Low-latency globally, GDPR compliance, regional fault isolation |
>
> ### Final Architecture
>
> ```
> ┌───────────────────────────────────────────────────────────────────────────────────┐
> │                           VIEW COUNTER SYSTEM                                    │
> │                                                                                  │
> │  ┌──────────┐     ┌──────────────────┐     ┌──────────────┐                     │
> │  │  Client   │────→│  API Gateway     │────→│    Kafka     │                     │
> │  │ (Player)  │     │ • Rate limit     │     │ (view-events)│                     │
> │  │           │     │ • Dedup (idemp.) │     │ 256 parts    │                     │
> │  │           │     │ • Client signals │     │ RF=3         │                     │
> │  └──────────┘     │ • Local agg (hot)│     └──────┬───────┘                     │
> │       ▲            └──────────────────┘            │                             │
> │       │                                    ┌───────┴────────┐                    │
> │       │                                    │                │                    │
> │       │                              ┌─────▼──────┐  ┌─────▼──────┐             │
> │       │                              │  Counter   │  │  Analytics │             │
> │       │                              │  Consumer  │  │  Consumer  │             │
> │       │                              │ (micro-    │  │ (Flink     │             │
> │       │                              │  batch 5s) │  │  windows)  │             │
> │       │                              └─────┬──────┘  └─────┬──────┘             │
> │       │                                    │               │                    │
> │       │                              ┌─────▼──────┐  ┌─────▼──────┐             │
> │  ┌────┴─────┐     ┌──────────┐      │   Redis    │  │ ClickHouse │             │
> │  │   CDN    │◄────│ API Svr  │◄─────│  Cluster   │  │ (rollups)  │             │
> │  │ (30s TTL)│     │          │      │ (sharded   │  │ min/hr/day │             │
> │  └──────────┘     └──────────┘      │  counters) │  └────────────┘             │
> │                                     └─────┬──────┘                              │
> │                                           │                                     │
> │                                     ┌─────▼──────┐                              │
> │                                     │ Cassandra  │                              │
> │                                     │ (durable)  │                              │
> │                                     └────────────┘                              │
> │                                                                                  │
> │  ┌──────────────────────────────────────────────────────────────────┐             │
> │  │ FRAUD PIPELINE                                                  │             │
> │  │ Stage 1: Client signals → Stage 2: Rate limit → Stage 3: Watch │             │
> │  │ behavior → Stage 4: Batch ML (daily Spark) → Retroactive adjust│             │
> │  └──────────────────────────────────────────────────────────────────┘             │
> │                                                                                  │
> │  ┌──────────────────────────────────────────────────────────────────┐             │
> │  │ MULTI-REGION: Regional ingestion → Global aggregation           │             │
> │  │ Regional Redis for local reads, global aggregator merges deltas  │             │
> │  └──────────────────────────────────────────────────────────────────┘             │
> └───────────────────────────────────────────────────────────────────────────────────┘
> ```
>
> ### What Keeps Me Up at Night
>
> 1. **Hot key thundering herd during a surprise viral event.** Our dynamic counter sharding detects hot keys based on recent write rate, but a video that goes from 0 to 1M views/min in seconds (e.g., a celebrity death announcement) may overwhelm a single shard before the hot-key detector kicks in. Mitigation: pre-warm sharding for videos from accounts with large subscriber bases. But we can't predict everything.
>
> 2. **Kafka consumer lag during traffic spikes.** If consumers fall behind, the real-time count becomes increasingly stale. Users wouldn't notice 30 seconds of lag, but 10 minutes of lag on a viral video would be embarrassing. Mitigation: auto-scale consumers based on lag metric, but scaling takes minutes. Need to over-provision for headroom.
>
> 3. **Fraud model adversarial adaptation.** Bot operators study our detection and adapt. A model trained on last month's bots may miss this month's bots. We need continuous model retraining and a team dedicated to cat-and-mouse. False positives are equally dangerous — subtracting legitimate views from creators destroys trust.
>
> 4. **Cross-region consistency during network partitions.** If the inter-region link between EU and US goes down, regional counts diverge. When connectivity restores, we merge — but if both regions processed the same events (due to failover routing), we could double-count. Need careful deduplication during reconciliation.
>
> 5. **Counter overflow / precision at extreme scale.** The most-viewed YouTube video has ~14 billion views. A 32-bit integer overflows at ~2.1 billion — YouTube actually hit this with Gangnam Style and had to upgrade to 64-bit. At 64-bit (max ~9.2 quintillion), we're safe for decades. But sharded counters summed across 100 shards need careful overflow handling.
>
> ### Additional Features Built on This Infrastructure
>
> | Feature | How it uses view counter infrastructure |
> |---|---|
> | **Trending** | View velocity (views/hour) computed from ClickHouse rollups |
> | **Creator revenue** | Exact counts from Kafka event log reconciliation |
> | **Recommendation ranking** | View count as one signal in the recommendation model |
> | **Content moderation** | Sudden view spikes trigger human review for policy violations |
> | **A/B testing** | View count changes as a metric for thumbnail/title experiments |

**Interviewer:** Great job. Any questions for me?

**Candidate:**

> "Two quick ones. First, in the real YouTube system, how tightly coupled is the view counting infrastructure to the ads billing pipeline? I assumed they're separate systems with reconciliation, but I'm curious if there's a shared source of truth. Second, do you use CRDTs for the cross-region counter consistency, or is it more of a custom aggregation protocol?"

---

## Interviewer's Final Assessment

**Hire Recommendation: Strong Hire (L6 — demonstrates L6-level depth with flashes of L7 thinking)**

| Dimension | Rating | Notes |
|---|---|---|
| **Problem scoping** | Exceeds Bar | Identified all key requirements unprompted, especially fraud as first-class concern. Strong back-of-envelope math |
| **Architecture** | Meets Bar (Strong) | Clean evolution from naive to production. Good separation of counter vs analytics paths. Multi-region design solid |
| **Deep dive (Write)** | Exceeds Bar | Micro-batching with quantified math. Counter sharding with dynamic detection. Understood Kafka offset replay for recovery |
| **Deep dive (Read)** | Meets Bar | Multi-layer caching, write-through strategy, cache stampede protection. Could have gone deeper on CDN push mechanics |
| **Fraud detection** | Exceeds Bar | Multi-stage pipeline with rejection rates. ML features well-chosen. Retroactive adjustment with atomicity concerns |
| **Scale / storage** | Meets Bar | Three-tier storage well-designed. Good cost analysis. Power law insight |
| **Global distribution** | Meets Bar | Regional ingestion + global aggregation. GDPR insight. Could have discussed split-brain more formally |
| **Operational maturity** | Exceeds Bar | "What keeps me up at night" showed real production thinking — adversarial fraud, consumer lag, overflow |

### Key Differences Summary — L5 vs L6 vs L7 for View Counter

| Dimension | L5 (SDE-2) | L6 (SDE-3) | L7 (Principal) |
|---|---|---|---|
| **Core insight** | "Use a queue and a cache" | "Write-heavy system with hot-key problem and approximate-vs-exact duality" | "This is fundamentally a distributed counting problem with adversarial actors, analogous to ad click counting (Google Photon)" |
| **Fraud detection** | Afterthought: "add rate limiting" | First-class: multi-stage pipeline, ML features, retroactive correction | Systemic: adversarial ML, economics of fraud, false positive impact on creator trust |
| **Consistency** | "Eventually consistent" | Dual-path: fast (Redis) for display, exact (Kafka log) for billing | Formal consistency model per consumer type, CRDT proofs, anti-entropy protocols |
| **Hot keys** | "Use sharding" | Counter sharding with dynamic detection, local aggregation, quantified improvement | Adaptive sharding algorithms, workload prediction, shard rebalancing under load |
| **Multi-region** | "Deploy in multiple regions" | Regional ingestion + global aggregation, GDPR data separation | Active-active with CRDTs, formal partition tolerance analysis, cross-region exactly-once |
| **Operational** | Happy path only | Failure modes, recovery procedures, monitoring metrics | Chaos engineering, game days, runbooks, blast radius analysis, progressive rollouts |

---

*For detailed deep dives on each component, see the companion documents:*
- [02-api-design.md](02-api-design.md) — Comprehensive API reference
- [03-deep-dive-write-path.md](03-deep-dive-write-path.md) — View ingestion, Kafka, micro-batching, counter sharding
- [04-deep-dive-read-path.md](04-deep-dive-read-path.md) — Caching, CDN, consistency models, analytics queries
- [05-deep-dive-fraud-detection.md](05-deep-dive-fraud-detection.md) — Multi-stage fraud pipeline, ML detection, retroactive adjustments
- [06-deep-dive-scale-and-storage.md](06-deep-dive-scale-and-storage.md) — Back-of-envelope math, storage tiers, sharding, replication
- [07-deep-dive-realtime-analytics.md](07-deep-dive-realtime-analytics.md) — Flink pipeline, windowed aggregation, ClickHouse, downsampling
- [08-deep-dive-global-distribution.md](08-deep-dive-global-distribution.md) — Multi-region ingestion, CRDTs, failover, GDPR
