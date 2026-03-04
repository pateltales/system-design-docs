Design a View Counter System (like YouTube View Count) as a system design interview simulation.

## Template
Follow the EXACT same format as the S3 interview simulation at:
src/hld/aws/s3/design/interview-simulation.md

Same structure:
- Interviewer/Candidate dialogue format
- PHASE 1: Opening & Problem Statement
- PHASE 2: Requirements Gathering & Scoping (functional + non-functional + scale numbers)
- PHASE 3: API Design
- PHASE 4: High-Level Architecture (iterative build-up: Attempt 0 → 1 → 2 → ..., starting from single server, finding problems, evolving)
- PHASE 5+: Deep dives on each component
- L5/L6/L7 rubric table after EACH deep dive phase — showing exactly how an L5, L6, and L7 candidate would answer differently. This is critical for understanding what "good" looks like at each level.
- Architecture evolution table after each phase
- Wrap-up phase with "what keeps you up at night"
- Links to supporting deep-dive docs

## Candidate Level
**L6 SDE-3 (Senior SDE)** candidate being interviewed by a Principal Engineer (L8).

The candidate's answers should demonstrate L6-level depth:
- Not just "what" but "why" and "why not the alternative"
- Quantitative reasoning (back-of-envelope math, concrete numbers)
- Awareness of operational concerns (monitoring, failure modes, blast radius)
- Proactive identification of tradeoffs before the interviewer asks

The L5/L6/L7 rubric tables after each phase should make it crystal clear how an L5 answer differs from L6 differs from L7.

## Output location
Create all files under: src/hld/viewcounter/design/

## Files to create

### 1. 01-interview-simulation.md — the main backbone
The full interview dialogue covering all phases and attempts.

### 2. 02-api-design.md — comprehensive API reference
This doc should list all the major API surfaces of a View Counter system. The interview simulation (Phase 3) will only cover a subset — this doc is the comprehensive reference.

**Structure**: For each API group, list every endpoint with HTTP method, path, request/response shape, and a brief description. Mark endpoints covered in the interview with a star or highlight.

**API groups to cover**:

- **View Ingestion APIs**: The core write path. `POST /videos/{videoId}/view` (record a view event — includes metadata: userId or anonymous sessionId, timestamp, deviceType, country, referrer, watchDurationSeconds). This is the **highest traffic endpoint** — every video play triggers it. Must handle massive write throughput (millions of events/sec for viral videos). Consider: should this be a fire-and-forget async call or synchronous? What happens when a view is recorded — is the count updated immediately or batched?

- **View Count Read APIs**: The read path. `GET /videos/{videoId}/viewCount` (return the current view count for a single video — this is what appears below every YouTube video). Must be extremely fast (<10ms). `GET /videos/batch/viewCounts?ids={id1,id2,...}` (batch fetch — used when rendering a page with multiple video thumbnails, e.g., homepage, search results, recommendations). `GET /channels/{channelId}/totalViews` (aggregate view count across all videos for a channel — YouTube Studio dashboard). Consider: what consistency guarantees? Is it okay to show slightly stale counts?

- **Analytics / Breakdown APIs**: `GET /videos/{videoId}/analytics?startDate={}&endDate={}&granularity={hour|day|week|month}` (time-series view data — views per hour/day/week for a video, used in YouTube Studio). `GET /videos/{videoId}/analytics/demographics` (view breakdown by country, device type, traffic source). `GET /videos/{videoId}/analytics/realtime` (real-time views in last 48 hours with minute-level granularity — the "Realtime" tab in YouTube Studio). These analytics APIs are read-heavy but tolerate higher latency (seconds, not milliseconds).

- **Trending / Leaderboard APIs**: `GET /trending?region={}&category={}` (top trending videos based on view velocity — views per hour, not just total views). `GET /leaderboard/allTime?category={}` (all-time most viewed videos). These require pre-computed rankings, not real-time aggregation.

- **Internal / Admin APIs**: `POST /videos/{videoId}/viewCount/adjust` (manual correction — subtract fraudulent views after bot detection). `GET /videos/{videoId}/viewCount/audit` (audit trail — history of adjustments). `PUT /videos/{videoId}/viewCount/freeze` (freeze count during investigation — the view count stops updating publicly while fraud analysis runs).

### 3. 03-deep-dive-write-path.md — View Ingestion & Write Path

The most architecturally interesting part. Cover:

- **The core problem**: YouTube gets ~800K video views per second globally. A single viral video (e.g., BTS music video premiere) can get millions of views per minute. Writing each view directly to a database would crush any single DB. This is a **write-heavy, read-light** system (opposite of most systems).

- **Event streaming architecture**:
  - Client sends view event → API Gateway → Kafka (or Kinesis) as the ingestion buffer
  - Why Kafka? Decouples ingestion from processing. Kafka can absorb massive write spikes without backpressure to clients. Partitioning by videoId ensures ordering per video.
  - Topic design: partition by videoId hash vs. by region? Tradeoffs of each.
  - Consumer groups: multiple consumers process events in parallel. Each consumer aggregates counts in-memory (micro-batching) and flushes to the database periodically (e.g., every 5-10 seconds).

- **Counting strategies — the heart of the design**:
  - **Naive: Increment counter in DB per event** — `UPDATE views SET count = count + 1 WHERE videoId = X`. Simple but deadly at scale. Every view = 1 DB write. Hot partition on viral videos. Lock contention.
  - **In-memory aggregation (micro-batching)**: Buffer counts in consumer memory. Flush aggregated delta (e.g., +3,247 views) to DB every N seconds. Reduces DB writes by 1000x. Tradeoff: if consumer crashes before flush, lose in-flight counts. Mitigation: Kafka offsets — replay from last committed offset.
  - **Multi-level aggregation**: Local counter (per consumer) → Regional aggregation → Global aggregation. Similar to how CDNs work — aggregate locally, merge globally.
  - **HyperLogLog for unique viewers**: If you need unique viewer count (not just total views), exact counting requires storing every userId — doesn't scale. HyperLogLog gives ~0.81% error with 12KB memory per video. Redis `PFADD` / `PFCOUNT`. When is approximate acceptable vs. exact required?

- **Database choice for counters**:
  - **Redis**: In-memory, `INCR` is O(1) and atomic. Perfect for hot counters. But: memory is expensive, persistence (RDB/AOF) has tradeoffs, single-threaded per shard.
  - **Cassandra**: Wide-column store, excellent write throughput, distributed counters (`counter` column type). But: Cassandra counters have known issues — not idempotent (retry = double count), eventual consistency, counter corruption bugs.
  - **MySQL/PostgreSQL with sharding**: Reliable, ACID. But: row-level locking on hot rows = bottleneck. Workaround: shard the counter (split videoId counter into N sub-counters, sum on read). Read fan-out tradeoff.
  - **Hybrid approach** (likely best): Redis for real-time hot counters + Cassandra/MySQL for durable storage. Periodic reconciliation.

- **Handling viral videos (hot partition problem)**:
  - A video going viral = thundering herd on one key. Even with sharding, one videoId maps to one shard.
  - Solutions: **Counter sharding** — split one counter into K sub-counters (e.g., `videoId:shard0` through `videoId:shard99`). Write to random shard, read sums all shards. Increases read cost but distributes write load.
  - **Write-behind buffer**: Queue writes to hot keys, flush in batches. Auto-detect hot keys via rate monitoring.
  - **Local aggregation**: Each application server maintains a local counter per hot videoId in memory, flushes periodically. Viral video with 100 app servers = 100x write reduction.

- **Idempotency and deduplication**:
  - Network retries can cause duplicate view events. Without deduplication, view counts inflate.
  - Options: (1) Client-generated idempotency key (UUID per view event), store in a dedup set (Redis SET with TTL). (2) Accept approximate counts — at YouTube scale, 0.1% overcounting is acceptable. (3) Kafka exactly-once semantics (idempotent producer + transactional consumer).
  - Dedup window: how long to remember seen events? 5 minutes? 1 hour? Memory vs. accuracy tradeoff.

### 4. 04-deep-dive-read-path.md — View Count Reads & Caching

- **Read patterns**:
  - Homepage: batch fetch ~20-50 video view counts. Must be <50ms total.
  - Video watch page: single video count. Must be <10ms.
  - Search results: batch fetch ~10-20 video counts.
  - YouTube Studio (creator analytics): time-series queries, higher latency acceptable (1-2s).

- **Caching strategy**:
  - **CDN-level caching**: View counts for popular videos cached at CDN edge. TTL: 30-60 seconds (stale counts are acceptable). Reduces load on origin by 90%+.
  - **Application-level cache (Redis/Memcached)**: Cache pre-computed view counts. Write-through from the aggregation pipeline. Cache invalidation: the aggregation consumer updates Redis directly after flushing to DB — no cache invalidation needed, it's always write-through.
  - **Cache stampede on viral videos**: When cache expires, thousands of concurrent requests hit the DB. Solutions: (1) Lock-based refresh — only one request refreshes, others wait. (2) Probabilistic early expiration — each request has a small probability of refreshing before TTL expires. (3) Never expire — always update via write-through.

- **Consistency model**:
  - **Eventual consistency is acceptable** for public-facing view counts. YouTube's view count famously "freezes" at 301 views for new videos (legacy behavior for fraud verification). Current behavior: counts update with a few seconds to minutes of delay.
  - **Stronger consistency for creator analytics**: Creators expect accurate counts in YouTube Studio. But even here, minutes-level lag is acceptable.
  - **Read-your-writes for the viewer**: After you watch a video, you might expect the count to increment. But YouTube doesn't guarantee this — and users don't really notice.

- **Materialized views for analytics**:
  - Pre-aggregate views by time window (hourly, daily, monthly) into summary tables.
  - Use a Lambda architecture or Kappa architecture:
    - **Lambda**: Batch layer (daily MapReduce/Spark jobs recompute exact counts from raw event log) + Speed layer (real-time approximate counts from stream processing). Merge at query time.
    - **Kappa**: Single stream processing pipeline (Kafka Streams / Flink) handles both real-time and historical reprocessing. Simpler to maintain but harder to guarantee correctness for historical recomputation.
  - Why not just query raw events? At 800K views/sec, raw event storage grows at ~70B events/day. Scanning raw events for a time-range query is prohibitively slow.

### 5. 05-deep-dive-fraud-detection.md — Bot Detection & View Validation

This is what makes view counting hard beyond just "increment a counter."

- **Why fraud detection matters**:
  - View counts drive ad revenue (CPM-based). Inflated views = advertiser fraud.
  - View counts drive trending/recommendations. Bot views = manipulation of platform integrity.
  - YouTube reportedly rejects ~15-20% of views as invalid.

- **View validation pipeline (multi-stage)**:
  - **Stage 1 — Client-side signals (real-time)**: Is the request from a real browser/app? Check: User-Agent, JavaScript execution fingerprint, TLS fingerprint (JA3), cookie presence, reCAPTCHA score. Bots often fail these checks.
  - **Stage 2 — Rate limiting (real-time)**: Same IP watching same video 100 times in 1 hour? Same userId watching 500 different videos in 1 hour? Apply rate limits per (IP, videoId), per (userId, videoId), per (sessionId, videoId). Use sliding window counters in Redis.
  - **Stage 3 — Watch behavior analysis (near real-time)**: Did the user actually watch the video? Minimum watch duration threshold (e.g., 30 seconds or 50% of video, whichever is shorter — this is close to YouTube's actual rule). Was the video playing in a visible tab? (Page Visibility API). Did the user interact with the page (scroll, mouse movement)?
  - **Stage 4 — Batch fraud analysis (offline)**: ML models analyze patterns across millions of views. Features: geographic distribution (all views from one IP range?), temporal pattern (views spike at exact intervals = bot), device fingerprint clustering, view-to-engagement ratio (views but no likes/comments = suspicious). Run as daily/weekly Spark jobs. Subtract fraudulent views from counts retroactively.

- **The "301 views" freeze (historical context)**:
  - YouTube used to freeze view counts at 301 for new videos while running fraud analysis. If views passed validation, the count would jump to the real number. This was removed around 2015 in favor of real-time validation + retroactive subtraction.

- **Retroactive count adjustment**:
  - When batch analysis detects fraud, subtract views from the counter. This means view counts can **decrease** — users sometimes notice this.
  - Need an audit trail: who adjusted, when, by how much, reason.
  - Adjustment must be atomic — don't want to show a negative count during adjustment.

### 6. 06-deep-dive-scale-and-storage.md — Scale Numbers & Storage Design

- **Back-of-envelope calculations**:
  - YouTube: ~800K views/second (peak), ~500K views/second (average). ~40 billion views/day.
  - Each view event: ~200 bytes (videoId, userId, timestamp, deviceType, country, referrer, watchDuration, IP). Raw event storage: 200 bytes × 40B = ~8 TB/day = ~3 PB/year.
  - Counter storage: ~800M videos on YouTube. Each counter: 8 bytes (int64). Total: ~6.4 GB — fits in a single Redis instance. But with sharded counters (100 shards per hot video), analytics rollups, and metadata, budget ~100 GB for Redis.
  - Kafka throughput: 800K events/sec × 200 bytes = ~160 MB/sec. Well within Kafka's capacity (single cluster can handle GB/sec). Retention: 7 days = ~56 TB.

- **Data storage tiers**:
  - **Hot tier (Redis)**: Current view counts for all videos. Real-time counters. ~100 GB.
  - **Warm tier (Cassandra/MySQL)**: Hourly/daily aggregated counts per video. Analytics queries. Retention: forever.
  - **Cold tier (S3 + Parquet/ORC)**: Raw view events for batch analysis and fraud detection. Retention: 90 days (raw), summarized forever. Query via Spark/Presto/Athena.
  - **Cost optimization**: Most views are on a small percentage of videos (power law). Only ~1% of videos account for ~80% of views. Cache and optimize for these.

- **Sharding strategy**:
  - Shard by videoId (consistent hashing). Each shard handles a subset of videos.
  - Problem: viral video = hot shard. Mitigation: secondary sharding within a video (counter sharding as discussed in write path).
  - Shard count: start with 256 shards, grow to 1024+. Use virtual nodes for rebalancing.

- **Replication and durability**:
  - Redis: master-replica with sentinel or Redis Cluster. Async replication — can lose a few seconds of counts on master failure. Acceptable? At 800K views/sec, losing 5 seconds = losing ~4M views. For a system where exact accuracy isn't critical, this is fine. For billing/monetization, reconcile from Kafka replay.
  - Cassandra: replication factor 3, consistency level QUORUM for writes, ONE for reads (fast reads, strong writes).

### 7. 07-deep-dive-realtime-analytics.md — Real-Time Analytics Pipeline

- **The real-time dashboard problem**:
  - YouTube Studio shows creators a real-time view graph with minute-level granularity for the last 48 hours. This requires a streaming analytics pipeline separate from the counter system.

- **Stream processing architecture**:
  - Kafka → Flink/Kafka Streams → Time-windowed aggregation → Store in time-series DB
  - Window types: Tumbling windows (fixed, non-overlapping — e.g., 1-minute buckets), Sliding windows (overlapping — e.g., "views in last 5 minutes" updated every minute), Session windows (group events by user session gaps).
  - Late-arriving events: event time vs. processing time. Use watermarks to handle out-of-order events. Allow a lateness window (e.g., 5 minutes) — events arriving after the watermark are either dropped or trigger a correction.

- **Time-series storage**:
  - Options: InfluxDB, TimescaleDB (PostgreSQL extension), Apache Druid, ClickHouse.
  - Requirements: fast time-range queries, automatic downsampling (minute → hour → day as data ages), high write throughput.
  - **ClickHouse** is a strong choice: columnar storage, excellent compression for time-series data, handles billions of rows, SQL interface. Used by many companies for real-time analytics.
  - **Druid** is another option: designed for OLAP on event data, sub-second queries, built-in time-based partitioning.

- **Downsampling / rollup strategy**:
  - Raw events → 1-minute aggregates (kept for 48 hours) → 1-hour aggregates (kept for 90 days) → 1-day aggregates (kept forever).
  - Reduces storage by orders of magnitude while preserving queryability at appropriate granularities.
  - Rollup jobs: run periodically (e.g., hourly) to compact minute-level data into hour-level. Use idempotent upserts to handle reprocessing.

### 8. 08-deep-dive-global-distribution.md — Multi-Region Architecture

- **Why multi-region?**:
  - YouTube serves users globally. View events originate from every continent. Routing all events to a single region adds latency and creates a single point of failure.
  - Regulatory requirements: some countries require data to stay within borders (GDPR for EU user data).

- **Multi-region ingestion**:
  - Deploy ingestion endpoints (API Gateway + Kafka) in each major region (US-East, US-West, EU, APAC, etc.).
  - Local Kafka clusters receive view events from nearby users.
  - Cross-region replication: MirrorMaker 2 / Confluent Replicator replicates events to a central processing region for global aggregation.
  - Alternative: process locally, aggregate globally. Each region runs its own Flink consumers, produces partial counts, a global aggregator merges them.

- **Global view count consistency**:
  - View count is a single global number, but events arrive in multiple regions.
  - Option 1: **Centralized counter** — all regions send increments to one global Redis. Simple but cross-region latency on writes (100-200ms). Acceptable if writes are batched (aggregate locally for 5s, then send delta to global).
  - Option 2: **CRDT counter** — each region maintains a local counter (grow-only G-Counter). Global count = sum of all regional counters. Eventually consistent, conflict-free. No cross-region write latency. Read requires querying all regions (or pre-aggregate).
  - Option 3: **Hybrid** — regional Redis for fast local reads (cached global count), global aggregation pipeline for the source-of-truth count. Periodic sync (every 5-10 seconds).

- **Failover and disaster recovery**:
  - If one region goes down, DNS routes traffic to nearest healthy region.
  - Kafka replication ensures no event loss (events in the failed region's Kafka are replicated to other regions).
  - View counts may temporarily diverge during failover — reconcile when the region recovers.

## Key Themes to Emphasize Throughout

1. **Write-heavy system**: Unlike most systems, this is dominated by writes (view events). The read path is simple; the write path is where all the complexity lives.
2. **Approximate vs. exact**: View counts don't need to be perfectly accurate. This unlocks many optimizations (batching, HyperLogLog, eventual consistency) that wouldn't be possible in a financial system.
3. **Hot key problem**: Viral videos are the defining challenge. Any design must handle 1 video getting 10,000x more writes than average.
4. **Fraud detection as a first-class concern**: Without it, view counts are meaningless. This isn't an afterthought — it's part of the core architecture.
5. **Multi-level aggregation**: The pattern of "aggregate locally, merge globally" appears everywhere — in the write path (micro-batching), in multi-region (regional counters), and in analytics (time-window rollups).
6. **Lambda/Kappa architecture**: The tension between real-time approximate counts and batch-computed exact counts is a central design decision.

## Anti-patterns to call out in the interview

- Incrementing a DB counter per view event (won't scale)
- Using a single Redis key per video without sharding for hot keys
- Ignoring fraud detection ("just count every request")
- Synchronous writes to the database from the API layer
- Not discussing eventual consistency tradeoffs
- Using exact unique counting (storing all userIds) instead of probabilistic structures
- Ignoring the multi-region problem for a global platform

## Reference materials
- YouTube Engineering blog posts on view count infrastructure
- Google's Photon paper (exactly-once processing for ad click counting — similar problem)
- Apache Kafka documentation on exactly-once semantics
- Redis documentation on HyperLogLog
- Martin Kleppmann's "Designing Data-Intensive Applications" — Chapter 11 (Stream Processing)
- CRDT literature (G-Counter for distributed counting)
- ClickHouse documentation for real-time analytics
