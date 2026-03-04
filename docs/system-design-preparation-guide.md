# System Design Interview Preparation Guide

> **Goal:** Prepare the minimum set of systems so that ANY interview question is a composition of patterns you already know.

---

## The Core Insight

Every system design interview question is a **composition of ~15 fundamental building blocks**. You don't need to memorize 50 systems — you need to deeply understand the building blocks, then compose them on the fly.

"Design Twitter" = fan-out pub/sub + cache + relational DB + CDN + rate limiter + search index
"Design Uber" = geospatial index + pub/sub + queue + cache + relational DB + rate limiter
"Design Dropbox" = blob storage + metadata DB + sync protocol + CDN + queue

If you know the building blocks, the interview becomes **assembly**, not **invention**.

---

## Section 1: What You Already Know (9 Services)

Your 9 AWS service designs cover these building block patterns:

| Pattern | Taught By | What You Learned |
|---|---|---|
| **Hash partitioning + auto-splitting** | DynamoDB, SQS | How to distribute data across nodes, when to split (10 GB or throughput threshold), hot partition handling |
| **Leader-based replication + consensus** | DynamoDB | Multi-Paxos, 3 replicas across AZs, majority commit (2/3), leader election in 1-3s |
| **Erasure coding for durability** | S3 | Reed-Solomon 8+3, 1.375x overhead vs 3x replication, 11 9's durability math |
| **Metadata/data separation** | S3 | Index (small, strongly consistent, fast lookups) vs data (large, immutable blobs) — scale independently |
| **Columnar storage + zone maps** | Redshift | 1 MB blocks, min/max per block, compression encodings, block-level predicate skipping |
| **MPP query execution** | Redshift | Leader/compute split, steps/segments/streams, distribution keys, sort keys, code compilation |
| **Fan-out / pub-sub** | SNS | Topic → N subscribers, parallel delivery, filter policies, protocol-specific retry |
| **Queue semantics** | SQS | Visibility timeout, at-least-once vs exactly-once, DLQ, long polling, message groups |
| **Stream processing** | Kinesis | Shard-based ordering, checkpointing, consumer groups, resharding |
| **Serverless compute** | Lambda | Cold start, Firecracker microVMs, warm sandbox reuse, SnapStart, event source mappings |
| **Container orchestration** | ECS | Task placement (filter → constraint → strategy), capacity providers, rolling/blue-green deploy |
| **Batch processing** | EMR | YARN resource management, Spark DAG execution, primary/core/task nodes, HDFS vs S3 |
| **MicroVM isolation** | Lambda | Firecracker, jailer, seccomp — VM-level multi-tenant isolation in <125ms |

**That's 13 patterns. But there are ~15 more that come up in interviews that NONE of these 9 services teach.** That's the gap.

---

## Section 2: The Gap — Patterns You're Missing

| Missing Pattern | Why It Matters | How Often It Appears in Interviews |
|---|---|---|
| **B-tree / LSM-tree indexes** | Every system has a database. You must explain WHY an index makes a query fast (O(log N) vs O(N)) and WHEN to add one. | Every interview |
| **Write-ahead log (WAL)** | The foundational durability mechanism for databases. "How do you not lose data if the server crashes mid-write?" | 70% of interviews |
| **MVCC / transaction isolation** | Concurrent reads and writes without locking. Read-committed vs serializable. Phantom reads. | 50% |
| **Primary-replica replication** | Read replicas for read scaling. Replication lag. Failover. "How do you scale reads?" | Every interview |
| **Cache eviction (LRU/LFU)** | Every system needs caching. "What happens when the cache is full?" | Every interview |
| **Cache consistency patterns** | Cache-aside, write-through, write-behind, refresh-ahead. "How do you keep cache and DB in sync?" | Every interview |
| **Consistent hashing** | How to distribute load across N servers and handle server additions/removals with minimal disruption | 60% |
| **Inverted index** | The data structure behind search. Completely different from B-trees. "Design a search system." | 40% |
| **Rate limiting** | Token bucket, sliding window. "How do you prevent abuse?" Appears as a component in most systems. | 50% directly, 80% as component |
| **WebSocket / long-lived connections** | Chat, real-time updates, notifications. Fundamentally different from request-response HTTP. | 40% |
| **Edge caching / PoP** | CDN architecture. "How does a user in Tokyo get fast responses from a US-based service?" | 50% |
| **L4 vs L7 load balancing** | Every architecture has a load balancer. "Why ALB vs NLB?" "How does sticky sessions work?" | Every interview (as component) |
| **Exactly-once + idempotency** | Payment systems, financial transactions. "What if the same payment is processed twice?" | 30% |
| **Time-series storage** | Monitoring, metrics, IoT. Append-only with time-based aggregation and downsampling. | 20% |
| **Transcoding pipeline** | Video/media processing. DAG of transformations with fan-out to multiple output formats. | 15% |
| **Distributed counting at scale** | Real-time like/view/vote counters at billions of events. Approximate vs exact counts. | 30% |
| **Graph traversal / Graph DB** | Social graphs, friend recommendations, fraud detection. Distinct from relational joins — BFS, shortest path, mutual connections. | 25% |
| **Real-time media routing (WebRTC/SFU)** | Video/audio conferencing. SFU vs MCU, bandwidth estimation, codec negotiation. Fundamentally different from text WebSocket. | 20% |
| **Order matching / Order book** | Stock exchanges, auction systems. Price-time priority, limit vs market orders, sub-millisecond latency. | 15% |

---

## Section 3: Systems to Prepare (Prioritized)

### Tier 1 — MUST PREPARE (4 systems)

These fill the most critical gaps. Together with your 9 existing services, they cover **~90% of interview questions**.

---

#### 1. Relational Database (think: Aurora / Spanner)

**New patterns:** B-tree indexes, WAL, MVCC, primary-replica replication, connection pooling, full ACID

**Why #1 priority:** Every system design uses a relational DB somewhere. Interviewers expect you to reason about:
- "Should we add an index here?" (B-tree: O(log N) point lookup, range scan; cost: write amplification)
- "How do we scale reads?" (read replicas with async replication, replication lag tradeoff)
- "What happens if the server crashes mid-write?" (WAL: write to log first, replay on recovery)
- "How do concurrent reads and writes work?" (MVCC: each transaction sees a snapshot, no read locks)
- "When do we need transactions?" (ACID: atomicity for multi-row updates, isolation levels)

**Key concepts to study:**

| Concept | What to Know |
|---|---|
| **B-tree index** | Balanced tree on disk, O(log N) lookups, leaf pages linked for range scans. Clustered vs non-clustered. Covering indexes. |
| **WAL** | Append-only log written before data pages. Sequential write (fast) → random write (later). Enables crash recovery. |
| **MVCC** | Multi-Version Concurrency Control. Readers don't block writers. Each row has a version/timestamp. Old versions garbage-collected. |
| **Primary-replica** | Async replication: primary writes WAL → replicas replay. Replication lag = time between write on primary and visible on replica. |
| **Connection pooling** | Database connections are expensive (TCP + auth + memory). Pool reuses connections. PgBouncer, RDS Proxy. |
| **Query optimizer** | Cost-based: estimates row counts, index selectivity, join costs. EXPLAIN shows the plan. |

**You already know (from DynamoDB):** Partitioning, replication, consistency. The NEW thing is the index structure (B-tree vs hash) and the transaction model (full ACID vs DynamoDB's limited transactions).

**Comparison to cement understanding:**

| Aspect | DynamoDB | Aurora/PostgreSQL |
|---|---|---|
| Data model | Key-value / document | Relational (tables, joins, SQL) |
| Index | Hash (partition key) + range (sort key) | B-tree (any column, multiple indexes) |
| Scaling | Auto-partition by hash key | Read replicas (read scale) + vertical (write scale) |
| Transactions | Limited (100 items, same region) | Full ACID (any number of rows, complex queries) |
| Query flexibility | GetItem, Query (PK+SK only) | Arbitrary SQL (JOIN, subquery, window functions) |
| Consistency | Tunable (EC/SC per request) | Strong (within primary), eventual (replicas) |
| Best for | Known access patterns, extreme scale | Complex queries, joins, ad-hoc analytics |

---

#### 2. Distributed Cache (think: Redis / ElastiCache)

**New patterns:** Cache-aside, write-through, write-behind, LRU/LFU eviction, consistent hashing, hot key handling, cache stampede

**Why #2 priority:** "How would you add caching to reduce latency?" is the most common follow-up question in system design interviews. Cache invalidation is famously one of the two hard problems in CS.

**Key concepts to study:**

| Pattern | How It Works | Tradeoff |
|---|---|---|
| **Cache-aside** (lazy loading) | App checks cache → miss → read DB → write to cache → return | Simple. But first request always slow (cache miss). Stale data until TTL expires. |
| **Write-through** | App writes to cache AND DB on every write | Cache always fresh. But write latency increases (2 writes). Cache may hold data that's never read. |
| **Write-behind** (write-back) | App writes to cache only → cache async writes to DB | Fastest writes. But data loss risk if cache crashes before DB write. |
| **Refresh-ahead** | Cache proactively refreshes entries before TTL expires | No cache misses for hot data. But wastes resources refreshing cold data. |

| Problem | Description | Solution |
|---|---|---|
| **Cache stampede** (thundering herd) | Cache entry expires → 1,000 concurrent requests all miss → all hit DB simultaneously | Locking: only one request fetches from DB, others wait. Or: probabilistic early expiration. |
| **Hot key** | One cache key gets 10,000x more requests than others → single Redis node overloaded | Replicate hot keys to multiple nodes. Or: local in-process cache for hottest keys. |
| **Cache invalidation** | When does cached data become stale? How do you invalidate? | TTL (simple but stale window). Event-driven invalidation (fresh but complex). |
| **Consistent hashing** | How to distribute keys across N cache servers so adding/removing a server only moves 1/N of keys | Hash ring with virtual nodes. Used by Redis Cluster, Memcached. |

**Key numbers:**

| Metric | Value |
|---|---|
| Redis single-node throughput | ~100K ops/sec |
| Redis latency | < 1ms (in-memory) |
| Typical cache hit ratio target | > 95% |
| DB latency without cache | 5-50ms |
| Speedup with cache | 10-100x |

---

#### 3. Rate Limiter / API Gateway

**New patterns:** Token bucket, sliding window, distributed rate limiting, circuit breaker

**Why #3 priority:** Asked directly in ~30% of interviews ("Design a rate limiter"). Also appears as a component in every API-facing system ("How do you prevent abuse?").

**Key algorithms:**

| Algorithm | How It Works | Pros | Cons |
|---|---|---|---|
| **Token bucket** | Bucket holds N tokens. Each request consumes 1 token. Tokens refill at rate R/sec. If bucket empty → reject. | Allows bursts (up to bucket size). Simple. | Doesn't smooth traffic perfectly. |
| **Leaky bucket** | Queue of fixed size. Requests processed at constant rate. If queue full → reject. | Perfectly smooth output rate. | No bursts allowed. Delays requests. |
| **Fixed window counter** | Count requests in fixed time windows (e.g., 0:00-0:59, 1:00-1:59). If count > limit → reject. | Simple. Low memory (one counter per window). | Boundary problem: 100 requests at 0:59 + 100 at 1:00 = 200 in 1 second. |
| **Sliding window log** | Store timestamp of each request. Count requests in last N seconds. If > limit → reject. | Accurate. No boundary problem. | High memory (stores every timestamp). |
| **Sliding window counter** | Weighted combination of current + previous window counts. | Low memory + no boundary problem. | Approximate (not exact). |

**Distributed rate limiting challenge:**
If you have 10 API servers, each with its own counter, a client can send 10× the limit (100 per server × 10 servers = 1,000). Solutions:
- **Centralized counter** (Redis INCR): Accurate but adds latency per request. Single point of failure.
- **Local counters + sync**: Each server tracks locally, periodically syncs. Fast but approximate.
- **Sticky routing**: Route each client to one server (consistent hashing). Simple but uneven load.

---

#### 4. URL Shortener (TinyURL)

**New patterns:** Base62 encoding, counter-based ID generation, read-heavy system design

**Why #4 priority:** The "Hello World" of system design interviews. Often asked as a warmup or to junior/mid candidates. But even for L6, it's a great vehicle to practice the full interview format in a compact system.

**Core design in 5 minutes:**
```
Write path:
  Long URL → hash/counter → base62 encode → short code (7 chars = 62^7 = 3.5 trillion unique codes)
  Store: short_code → long_url in DB

Read path (100:1 read:write ratio):
  GET /abc1234 → lookup short_code in cache → cache miss → lookup in DB → 301 redirect
  Cache: Redis with LRU eviction. 80% of traffic hits 20% of URLs (Pareto).

Scale:
  100M new URLs/month, 10B redirects/month
  ~3,800 reads/sec, ~38 writes/sec
  Storage: 100M × 1KB = 100 GB/year (small)
```

**What makes this useful:** It's simple enough to practice end-to-end (requirements → scale → architecture → deep dive) in one sitting. The deep dives cover caching, database choice, ID generation (counter vs hash), analytics (click tracking pipeline using Kinesis → S3 → Redshift — hey, you already know those!).

---

### Tier 2 — HIGH VALUE (4 more systems)

These cover the next wave of common questions. Together with Tier 1, you're prepared for **~95% of interviews**.

---

#### 5. Search Engine (Elasticsearch / Typeahead)

**New pattern: Inverted index** — a fundamentally different data structure from B-trees or hash tables.

```
Forward index (database):        Inverted index (search):
  Doc 1 → ["the", "quick", "brown", "fox"]    "quick" → [Doc 1, Doc 5]
  Doc 2 → ["the", "lazy", "dog"]              "brown" → [Doc 1, Doc 3]
  Doc 3 → ["brown", "dog", "lazy"]            "dog"   → [Doc 2, Doc 3]
                                                "lazy"  → [Doc 2, Doc 3]
                                                "fox"   → [Doc 1]

Query: "quick brown" → intersect posting lists → Doc 1
```

**Key concepts:**
- **Tokenization**: Split text into tokens ("New York" → ["new", "york"] or ["new_york"]?)
- **Stemming**: Reduce words to root ("running" → "run", "better" → "good")
- **TF-IDF / BM25**: Relevance scoring — words that are rare in the corpus but frequent in a document score higher
- **Sharding**: Shard by document (each shard has full inverted index for its documents) vs shard by term (each shard owns certain terms)
- **Near-real-time**: New documents aren't searchable immediately — they're buffered and flushed to segments periodically (default 1 second in Elasticsearch)
- **Segment merging**: Small segments merged into larger ones (like LSM-tree compaction)

**When asked:** "Design a search system," "Design autocomplete/typeahead," "Design a product search for an e-commerce site"

---

#### 6. Chat / Real-time Messaging (WhatsApp / Slack)

**New pattern: WebSocket connections** — stateful, long-lived, bidirectional connections.

```
HTTP (request-response):           WebSocket (bidirectional):
  Client → Server: GET /messages     Client ↔ Server: persistent connection
  Server → Client: [messages]        Server pushes messages in real-time
  Connection closed.                  Connection stays open for hours.
```

**Key concepts:**
- **Connection management**: Each user has a WebSocket to a gateway server. Gateway tracks user → server mapping. If user is on Server 3, messages for that user must route to Server 3.
- **Online vs offline delivery**: Online → push via WebSocket. Offline → store in inbox, deliver when user reconnects.
- **Message ordering**: Within a 1:1 chat, messages must be ordered. In a group chat, causal ordering (reply after the message it replies to) but not necessarily total ordering across all senders.
- **Presence**: "User is typing…", "Last seen 5 min ago". Heartbeat-based — if no heartbeat for 30s, mark offline.
- **Fan-out**: In a group chat with 500 members, a message must be delivered to 500 connections. Write fan-out (write to each inbox) vs read fan-out (read from sender's outbox).
- **End-to-end encryption**: Signal protocol. Server can't read messages. Key exchange via public keys.

**When asked:** "Design WhatsApp," "Design Slack," "Design Facebook Messenger," "Design a real-time notification system"

---

#### 7. CDN (CloudFront / Akamai)

**New pattern: Edge caching with geographic distribution.**

```
Without CDN:
  User in Tokyo → request travels to US-East origin → 200ms RTT × multiple requests = slow

With CDN:
  User in Tokyo → request goes to Tokyo PoP (edge) → cache hit → <10ms
                                                    → cache miss → regional cache → origin
```

**Key concepts:**
- **PoP (Point of Presence)**: Data centers at network edges close to users. CloudFront has 400+ PoPs globally.
- **Cache hierarchy**: Edge (PoP) → Regional edge cache → Origin. Each level reduces origin load.
- **Origin shielding**: Consolidate cache misses through one regional node before hitting origin. Prevents origin from getting N× cache misses from N PoPs simultaneously.
- **Cache invalidation**: Purge by path, by tag, wildcard. Propagation time: seconds to minutes across all PoPs.
- **TTL strategy**: Static assets (images, CSS, JS) → long TTL (1 year + cache-busting via filename hash). API responses → short TTL (seconds) or no cache.
- **Anycast DNS**: Single IP address routes to nearest PoP based on BGP routing.
- **TLS termination**: SSL handshake happens at edge (close to user, fast), not at origin.

**When asked:** Any user-facing system ("How does the content reach users globally?"), "Design a CDN," or as a component of "Design Netflix/YouTube/Instagram"

---

#### 8. Notification System

**New pattern: Multi-channel delivery with user preferences.**

This system COMBINES patterns you already know (SNS fan-out + SQS queues + Lambda processing) with new concerns:

| Concept | Detail |
|---|---|
| **Multi-channel routing** | Same event → push notification + email + in-app badge. Each channel has different delivery semantics. |
| **User preferences** | User says "no email after 10 PM" or "only critical alerts via SMS." Preference store checked before every delivery. |
| **Device token management** | Push notifications need device tokens (APNS for iOS, FCM for Android). Tokens change on app reinstall, must be refreshed. |
| **Rate limiting per user** | Don't send 50 notifications in 1 minute. Coalesce: "You have 5 new messages" instead of 5 separate notifications. |
| **Priority + urgency** | "Your account was compromised" → immediate SMS. "Weekly digest" → batch and send Sunday morning. |
| **Template system** | Notifications are rendered from templates with variables. Localization (language, timezone). |

**When asked:** "Design a notification system," "Design push notifications for a mobile app"

---

### Tier 3 — DIFFERENTIATION (5 systems, for L6+ depth)

These are asked less frequently but demonstrate mastery. Prepare these **after** Tier 1 + 2.

---

#### 9. Video Streaming (YouTube / Netflix)

**New patterns:** Transcoding pipeline, adaptive bitrate streaming (HLS/DASH), chunked upload

**Key concepts:**
- Upload: chunked + resumable (if 2 GB upload fails at 1.5 GB, resume from 1.5 GB)
- Transcode: one source video → multiple resolutions (1080p, 720p, 480p) × multiple codecs (H.264, VP9, AV1). This is a DAG of tasks (like EMR but for video).
- Adaptive bitrate: player detects bandwidth and switches quality mid-stream. Video split into 2-10 second segments, each available in multiple qualities.
- CDN: video segments cached at edge. Most views are within 24 hours of upload.
- Storage: raw uploads in S3, transcoded segments in S3, CDN caches hot segments.

---

#### 10. Payment / Ledger System (Stripe)

**New pattern:** Correctness over performance — "what if money is lost?"

**Key concepts:**
- **Idempotency keys**: Client sends `Idempotency-Key: abc123` header. Server checks if this key was already processed. If yes, return same result. Prevents double-charging.
- **Double-entry bookkeeping**: Every transaction has a debit and a credit. Sum of all entries = 0. This makes reconciliation possible.
- **Saga pattern**: Multi-step transaction (charge card → reserve inventory → create order). If step 3 fails, compensate step 1 and 2 (refund card, release inventory). No distributed transactions — each step is a local transaction + compensation.
- **Reconciliation**: Compare your ledger with bank/payment processor records periodically. Find mismatches. This is how you detect bugs that lost money.
- **PCI compliance**: Credit card numbers must be isolated (PCI DSS). Tokenize at the edge — the rest of your system never sees raw card numbers.

---

#### 11. Monitoring / Metrics System (Datadog / CloudWatch)

**New pattern:** Time-series storage with hierarchical aggregation.

**Key concepts:**
- **Data model**: (metric_name, tags, timestamp, value). Example: `http_requests{service=api, status=200} 1707840000 42`
- **Ingestion**: Agents on every host push metrics every 10-60 seconds. At 10,000 hosts × 100 metrics × 1 sample/10s = 100K data points/sec.
- **Storage**: Append-only time-series. Recent data (last 24h) in memory/SSD. Older data downsampled (1-second → 1-minute → 1-hour) and archived.
- **Query**: "Average CPU across all hosts in region=us-east, last 1 hour, 1-minute resolution." This is a time-range scan + tag filter + aggregation.
- **Alerting**: Threshold (CPU > 90% for 5 min) or anomaly detection (CPU is 3 standard deviations above normal).
- **Distributed tracing**: Trace ID propagated across services. Span tree reconstructed for debugging.

---

#### 12. Distributed Coordination (ZooKeeper / etcd)

**New pattern:** The "glue" that holds distributed systems together.

**Key concepts:**
- **Leader election**: N servers compete. One becomes leader. Others detect leader failure and elect a new one. ZooKeeper ephemeral nodes: create `/election/leader` — if creator disconnects, node auto-deleted, others race to recreate it.
- **Distributed locks**: Acquire lock before modifying shared resource. ZooKeeper sequential ephemeral nodes for fair queuing.
- **Configuration management**: Store config in ZooKeeper. Services watch for changes, get notified instantly.
- **Service discovery**: Services register themselves. Clients discover available instances.
- **Consensus**: Raft or ZAB protocol. Majority quorum (3/5 or 2/3). Linearizable reads and writes.

---

#### 13. Collaborative Editor (Google Docs)

**New pattern:** Conflict resolution for concurrent edits.

**Key concepts:**
- **Operational Transformation (OT)**: Transform operations relative to each other. If User A inserts at position 5 and User B inserts at position 3, User A's position becomes 6 (shifted by B's insert).
- **CRDTs (Conflict-free Replicated Data Types)**: Data structures that mathematically guarantee convergence. No central server needed for conflict resolution. Used by Figma.
- **Cursor presence**: Each user's cursor position broadcast to all others. Requires low-latency pub/sub.
- **Versioning**: Every edit creates a version. Undo/redo operates on the version history.

---

## Section 4: The "Any System" Framework

When you encounter a system you've never designed before, **decompose it into building blocks:**

### Step 1: Identify the data
- What is stored? (structured → relational DB, unstructured → blob store, time-series → time-series DB)
- How much? (GB → single node, TB → partitioned, PB → distributed)
- Access pattern? (point lookup → key-value/cache, range scan → B-tree, full-text → inverted index, analytical → columnar)

### Step 2: Identify the communication
- Request-response? → HTTP + load balancer
- Real-time push? → WebSocket
- Async processing? → message queue (SQS)
- Fan-out to many? → pub/sub (SNS)
- Ordered stream? → Kinesis

### Step 3: Identify the scale concerns
- Read-heavy? → cache + read replicas + CDN
- Write-heavy? → partition writes, async processing, write-behind cache
- Compute-heavy? → batch (EMR) or serverless (Lambda)
- Global? → CDN + multi-region replication

### Step 4: Identify the reliability concerns
- Can we lose data? → replication, WAL, backups
- Can we show stale data? → strong consistency or cache invalidation
- Can we have downtime? → multi-AZ, failover, health checks
- Can we process duplicates? → idempotency keys, exactly-once

### Example: "Design Uber"

```
Decomposition:
1. Data: Riders, drivers, trips, locations → Relational DB (riders/drivers/trips) + geospatial index (locations)
2. Communication: Real-time driver location updates → WebSocket. Trip matching → pub/sub. Payment processing → queue.
3. Scale: Read-heavy (millions checking driver locations). Write-heavy (drivers updating locations every 3 sec).
4. Reliability: Can't lose trip/payment data. Can tolerate slightly stale driver locations (eventual consistency OK).

Building blocks used:
├── Relational DB (Aurora)     ← Tier 1, #1
├── Cache (Redis)              ← Tier 1, #2 (cache hot driver locations)
├── Rate Limiter               ← Tier 1, #3 (prevent abuse)
├── WebSocket                  ← Tier 2, #6 (real-time updates)
├── Pub/Sub (SNS)              ← Already know (trip events)
├── Queue (SQS)                ← Already know (payment processing)
├── Geospatial index           ← New (QuadTree/GeoHash — unique to location systems)
└── Payment system             ← Tier 3, #10 (idempotency, saga)
```

---

## Section 5: Coverage Verification

After preparing **Tier 1 + Tier 2** (8 new systems + 9 existing = 17 total), here's how common interview questions decompose:

| # | Interview Question | Building Blocks | Covered? |
|---|---|---|---|
| 1 | Design a URL shortener | Relational DB + Cache + Rate Limiter | Tier 1 |
| 2 | Design Twitter / News Feed | Relational DB + Cache + Fan-out (SNS) + CDN + Rate Limiter | Tier 1+2 |
| 3 | Design Instagram | Blob store (S3) + Relational DB + CDN + Cache + Notification | Tier 1+2 |
| 4 | Design WhatsApp / Chat | WebSocket + Relational DB + Cache + Queue (SQS) + Notification | Tier 1+2 |
| 5 | Design YouTube / Netflix | Blob store (S3) + CDN + Transcoding pipeline + Cache | Tier 2+3 |
| 6 | Design Uber / Lyft | Relational DB + Cache + WebSocket + Rate Limiter + Queue | Tier 1+2 (+ geospatial) |
| 7 | Design Dropbox / Google Drive | Blob store (S3) + Relational DB + Queue + CDN + Notification | Tier 1+2 |
| 8 | Design a Search Engine | Inverted index + Cache + CDN + Rate Limiter | Tier 1+2 |
| 9 | Design Typeahead / Autocomplete | Inverted index (trie variant) + Cache + CDN | Tier 2 |
| 10 | Design a Rate Limiter | Token bucket + Cache (Redis) + Distributed counting | Tier 1 |
| 11 | Design a Web Crawler | Queue (SQS) + Blob store (S3) + DynamoDB (URL dedup) + DNS | Already know + Tier 1 |
| 12 | Design a Notification System | Pub/sub (SNS) + Queue (SQS) + Relational DB + Cache | Tier 1+2 |
| 13 | Design a Metrics/Monitoring System | Time-series DB + Stream (Kinesis) + Cache + Alerting | Tier 3 |
| 14 | Design Amazon S3 | **Already designed** | Done |
| 15 | Design DynamoDB / Key-Value Store | **Already designed** | Done |
| 16 | Design a Message Queue (SQS) | **Already designed** | Done |
| 17 | Design a Pub/Sub System (SNS) | **Already designed** | Done |
| 18 | Design a Data Warehouse (Redshift) | **Already designed** | Done |
| 19 | Design a Serverless Platform (Lambda) | **Already designed** | Done |
| 20 | Design a Container Orchestrator (ECS/K8s) | **Already designed** | Done |
| 21 | Design a Batch Processing System (EMR/Spark) | **Already designed** | Done |
| 22 | Design a Stream Processing System (Kinesis/Kafka) | **Already designed** | Done |
| 23 | Design an API Gateway | Rate Limiter + Cache + Relational DB (config) + Load Balancer | Tier 1 |
| 24 | Design Google Docs | Collaborative editor (OT/CRDT) + WebSocket + Cache | Tier 2+3 |
| 25 | Design Facebook Likes/Reactions | Cache (Redis INCR) + Relational DB + Fan-out (SNS) + Rate Limiter | Tier 1+2 |
| 26 | Design a View Counter (YouTube) | Cache (Redis INCR) + Relational DB + Stream (Kinesis) + Batch aggregation | Tier 1 + Already know |
| 27 | Design a Voting System (Reddit) | Cache (Redis) + Relational DB + Rate Limiter + Idempotency | Tier 1 |
| 28 | Design a Payment System | Relational DB + Queue + Idempotency + Saga | Tier 1+3 |
| 29 | Design Yelp / Nearby Places | Relational DB + Cache + Geospatial index + CDN | Tier 1+2 (+ geospatial) |
| 30 | Design a Distributed Cache | **Tier 1, #2** | Tier 1 |
| 31 | Design a Load Balancer | Consistent hashing + Health checks + L4/L7 routing | Tier 1 (covered by cache + CDN) |
| 32 | Design a Task Scheduler | Queue (SQS) + DynamoDB + Lambda + Rate Limiter | Already know + Tier 1 |
| 33 | Design Ticketmaster | Relational DB + Cache + Queue + Rate Limiter + Notification | Tier 1+2 |
| 34 | Design a Stock Exchange / Trading System | Order book (matching engine) + Cache + Stream (Kinesis) + Relational DB | New pattern (order matching) |
| 35 | Design Video Conferencing (Zoom) | WebRTC/SFU + WebSocket (signaling) + Cache + CDN | New pattern (real-time media) |
| 36 | Design a Social Graph (Facebook Friends) | Graph DB/index + Cache + Relational DB + Queue | New pattern (graph traversal) |
| 37 | Design E-commerce (Amazon) | Relational DB + Cache + Search + Queue + Payment + Notification + Rate Limiter | Tier 1+2+3 |
| 38 | Design an Ad Click Aggregator | Stream (Kinesis) + Cache + Relational DB + Batch aggregation (EMR) | Tier 1 + Already know |
| 39 | Design Distributed Logging (ELK/Splunk) | Stream (Kinesis) + Inverted index + Blob store (S3) + Cache | Tier 2 + Already know |
| 40 | Design Food Delivery (DoorDash) | Relational DB + Cache + WebSocket + Geospatial + Queue + Payment | Tier 1+2 (+ geospatial) |

**Result: 40/40 questions are covered by Tier 1 + Tier 2 + existing knowledge** (with geospatial indexing being the one niche pattern that only appears in location-based systems — a QuadTree/GeoHash deep dive covers it).

---

## Section 6: Study Order

If you're time-constrained, here's the optimal order (each system builds on the previous):

```
Week 1:  Relational Database (Aurora) — foundational, everything depends on it
Week 2:  Distributed Cache (Redis) — pairs with Relational DB, "add caching" follow-up
Week 3:  Rate Limiter — small system, quick to study, immediately useful
Week 4:  URL Shortener — practice the full interview format end-to-end

Week 5:  Search Engine — new data structure (inverted index), stands alone
Week 6:  Chat System — new communication pattern (WebSocket), combines many Tier 1 blocks
Week 7:  CDN — completes the "content delivery" picture, pairs with S3
Week 8:  Notification System — integrates SNS + SQS + Tier 1 blocks

If time permits:
Week 9+: Pick from Tier 3 based on target company (video for Netflix, payments for Stripe, etc.)
```

---

## Section 7: The Meta-Skill

The systems above teach you the building blocks. But the **meta-skill** that wins interviews is:

1. **Decompose**: Break the unknown system into known building blocks
2. **Justify**: For every choice, explain WHY and what's the alternative ("I chose a B-tree index here because our query pattern is range scans. If it were point lookups only, a hash index would be better.")
3. **Quantify**: Back-of-envelope math. "At 10,000 QPS with 1 KB payloads, that's 10 MB/sec. A single Redis instance handles 100K ops/sec, so one node suffices."
4. **Evolve**: Start simple (single server), find the problem, evolve. Don't jump to the final architecture.
5. **Tradeoff**: Name the tradeoff explicitly. "We gain read performance but lose write consistency. Here's when that's acceptable."

This is exactly the pattern your 9 AWS service interview simulations follow. Apply it to every new system.
