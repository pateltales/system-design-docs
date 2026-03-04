# Distributed Architecture and Coordination

## Why Distribute?

A single machine simply cannot crawl the web at meaningful scale.

### The Math

```
Web scale:           ~5 billion indexable pages (conservative)
Single machine:      ~100 pages/sec (network + parse + store)
Time for 1B pages:   1,000,000,000 / 100 = 10,000,000 sec = ~115 days

Target: 1B pages in 1 week
Required throughput: 1,000,000,000 / (7 × 86,400) = ~1,653 pages/sec
Machines needed:     1,653 / 100 = ~17 machines (minimum)

For 5B pages in 1 week:
Required throughput: 5,000,000,000 / 604,800 = ~8,267 pages/sec
Machines needed:     ~83 machines
```

### Resource Bottlenecks on a Single Machine

| Resource           | Single Machine Limit  | At 5,000 pages/sec                    |
|--------------------|-----------------------|---------------------------------------|
| Network bandwidth  | 1 Gbps typical        | 5,000 × 100KB = 500 MB/s = **4 Gbps** |
| TCP connections    | ~65K ports            | 5,000 concurrent = OK, but DNS + timeouts eat ports |
| Disk I/O           | ~500 MB/s SSD         | 500 MB/s raw content + index writes = saturated |
| CPU (parsing)      | 8-16 cores            | HTML parse + link extract at 5K/s = all cores busy |
| Memory (frontier)  | 64-128 GB             | 1B URLs in memory = 50-100 GB just for URLs |

### Geographic Distribution

```
Crawling eu.example.com from US data center:
  RTT: ~120ms  →  TCP handshake + TLS + HTTP = ~500ms minimum

Crawling eu.example.com from EU data center:
  RTT: ~10ms   →  TCP handshake + TLS + HTTP = ~50ms minimum

10x latency improvement = 10x throughput per connection
```

Geographic locality matters because:
- Lower latency means faster crawls and less resource waste on idle connections
- Respects data sovereignty (some sites block foreign IPs)
- Reduces backbone transit costs
- Better politeness: local crawling appears less suspicious to site operators

---

## Partitioning Strategy

The central question: **how do you divide billions of URLs across N crawler nodes?**

### Strategy 1: URL Hash Partitioning

```
assigned_node = Hash(full_url) % N
```

```
URL: https://news.example.com/article/123    → Hash = 7829  → Node 7829 % 10 = 9
URL: https://news.example.com/article/456    → Hash = 3214  → Node 3214 % 10 = 4
URL: https://news.example.com/article/789    → Hash = 5501  → Node 5501 % 10 = 1
                                                                ↑
                                              Same domain, 3 different nodes!
```

**Problem**: Three nodes must each independently enforce politeness for `news.example.com`. They need to coordinate: "When did any of us last hit this domain?" This requires cross-node communication for every single fetch decision.

### Strategy 2: Domain-Based Partitioning

```
assigned_node = Hash(domain) % N
```

```
URL: https://news.example.com/article/123    → Hash("news.example.com") = 4821 → Node 1
URL: https://news.example.com/article/456    → Hash("news.example.com") = 4821 → Node 1
URL: https://news.example.com/article/789    → Hash("news.example.com") = 4821 → Node 1
                                                                                   ↑
                                                            All on same node. Politeness is local.
```

**Problem**: Power-law distribution. A handful of domains (wikipedia.org, reddit.com, amazon.com) have millions of pages. Most domains have fewer than 100 pages.

```
Domain Size Distribution (power law):

Pages │
      │ ██
      │ ██
      │ ██ ██
      │ ██ ██
      │ ██ ██ ██
      │ ██ ██ ██ ██ ██
      │ ██ ██ ██ ██ ██ ██ ██ ██ ██ ██ ██ ██ ██ ██ ██ ██ ██ ██
      └──────────────────────────────────────────────────────── Domains (ranked)
        ^                                                    ^
    wikipedia.org                                   tiny-blog.net
    (6M+ pages)                                     (3 pages)
```

**Mitigation**: Consistent hashing with virtual nodes. Large domains can be assigned multiple virtual node slots, spreading their load across physical machines while still keeping per-domain politeness local within each slot.

### Strategy 3: Hybrid (Domain + Work-Stealing)

Domain-based assignment as the baseline, with a work-stealing protocol for load balancing.

```
                    ┌─────────────────────────────────────────────┐
                    │           Domain Assignment Table            │
                    │                                             │
                    │  Hash Ring with Virtual Nodes:              │
                    │  ┌──────────────────────────────────┐      │
                    │  │  VN_0   VN_1   VN_2   VN_3  ... │      │
                    │  │   ↓      ↓      ↓      ↓        │      │
                    │  │ Node0  Node1  Node2  Node0  ...  │      │
                    │  └──────────────────────────────────┘      │
                    └─────────────────────────────────────────────┘
                                       │
                    ┌──────────────────┼──────────────────┐
                    ↓                  ↓                  ↓
              ┌───────────┐     ┌───────────┐      ┌───────────┐
              │  Node 0   │     │  Node 1   │      │  Node 2   │
              │           │     │           │      │           │
              │ Domains:  │     │ Domains:  │      │ Domains:  │
              │ wiki.org  │     │ reddit.com│      │ cnn.com   │
              │ bbc.co.uk │     │ github.com│      │ tiny1.com │
              │           │     │           │      │ tiny2.com │
              │ Load: 95% │     │ Load: 40% │      │ Load: 30% │
              │    ↑      │     │           │      │           │
              │ OVERLOADED│     │           │      │           │
              └─────┬─────┘     └─────┬─────┘      └─────┬─────┘
                    │                 ↑                    ↑
                    │    Work-steal   │   Work-steal       │
                    ├── bbc.co.uk ───→│                    │
                    └── (keeps wiki) ─┘                    │
                         Politeness for bbc.co.uk          │
                         transfers entirely to Node 1      │
```

Work-stealing rules:
1. A node reports overload when its queue depth or CPU exceeds a threshold
2. It offers entire domains (never partial) so politeness stays local
3. The receiving node takes over all URLs for that domain
4. Rebalancing is periodic (every few minutes), not per-URL

### Comparison Table

| Criteria                 | URL Hash          | Domain-Based          | Hybrid                    |
|--------------------------|-------------------|-----------------------|---------------------------|
| Load balance             | Excellent (uniform) | Poor (power-law skew) | Good (work-stealing fixes skew) |
| Politeness enforcement   | Hard (cross-node) | Easy (local)          | Easy (local)              |
| Implementation complexity | Simple            | Medium                | High                      |
| Node addition/removal    | Reshuffles all URLs | Reshuffles domains   | Reshuffles domains + rebalance |
| Dedup coordination       | Per-URL routing   | Per-domain routing    | Per-domain routing        |
| **Best for**             | Dedup-heavy, politeness not critical | Most web crawlers | Large-scale production crawlers |

**Verdict**: Domain-based with consistent hashing is the standard choice. Add work-stealing if you operate at Google/Bing scale.

---

## Coordination Between Nodes

### Approach 1: Centralized Coordinator

```
                         ┌──────────────────────┐
                         │   Master Coordinator  │
                         │                       │
                         │  - URL batch dispatch │
                         │  - Global dedup set   │
                         │  - Crawl progress     │
                         │  - Node health        │
                         └───────────┬───────────┘
                                     │
                    ┌────────────────┼────────────────┐
                    │                │                 │
                    ↓                ↓                 ↓
              ┌──────────┐    ┌──────────┐     ┌──────────┐
              │ Worker 0 │    │ Worker 1 │     │ Worker 2 │
              │          │    │          │     │          │
              │ 1. Request│    │          │     │          │
              │    batch  │    │          │     │          │
              │ 2. Fetch  │    │          │     │          │
              │    pages  │    │          │     │          │
              │ 3. Return │    │          │     │          │
              │    results│    │          │     │          │
              │ + new URLs│    │          │     │          │
              └──────────┘    └──────────┘     └──────────┘
```

Flow:
1. Worker requests a batch of URLs from master
2. Master checks dedup, assigns unfetched URLs
3. Worker fetches, parses, returns content + discovered URLs
4. Master adds new URLs to frontier (after dedup)

**Pros**: Simple mental model. Dedup is trivial (one place). Easy to monitor.
**Cons**: Master is SPOF. Master becomes bottleneck at ~50+ workers (every discovered URL must flow through it). Master's memory limits frontier size.

**When to use**: Crawls under ~100M pages. Internal/enterprise crawlers. Prototyping.

### Approach 2: Decentralized / Masterless

```
   ┌──────────────────────────────────────────────────────────────┐
   │                    Crawler Cluster (Masterless)               │
   │                                                              │
   │  ┌──────────┐       ┌──────────┐       ┌──────────┐        │
   │  │  Node 0  │       │  Node 1  │       │  Node 2  │        │
   │  │          │       │          │       │          │        │
   │  │ Frontier │       │ Frontier │       │ Frontier │        │
   │  │ Partition│       │ Partition│       │ Partition│        │
   │  │ 0        │       │ 1        │       │ 2        │        │
   │  │          │       │          │       │          │        │
   │  │ Dedup    │       │ Dedup    │       │ Dedup    │        │
   │  │ Partition│       │ Partition│       │ Partition│        │
   │  │ 0        │       │ 1        │       │ 2        │        │
   │  └────┬─────┘       └────┬─────┘       └────┬─────┘        │
   │       │                  │                   │              │
   │       │    URL Routing (domain hash)         │              │
   │       │                  │                   │              │
   │       │  "Found URL for  │                   │              │
   │       │   reddit.com"    │                   │              │
   │       ├─────────────────→│                   │              │
   │       │  Hash("reddit")  │                   │              │
   │       │  % 3 = 1         │                   │              │
   │       │                  │  "Found URL for   │              │
   │       │                  │   cnn.com"        │              │
   │       │                  ├──────────────────→│              │
   │       │                  │  Hash("cnn")      │              │
   │       │                  │  % 3 = 2          │              │
   │  ┌────┴──────────────────┴───────────────────┴────┐         │
   │  │          Internal RPC / Message Bus             │         │
   │  │     (gRPC, ZeroMQ, or internal Kafka topic)     │         │
   │  └────────────────────────────────────────────────┘         │
   └──────────────────────────────────────────────────────────────┘
```

Each node:
- Owns a partition of the URL space (by domain hash)
- Maintains its own frontier and dedup filter for its partition
- Fetches URLs from its own frontier
- Routes discovered URLs to the correct owner node via RPC or message bus

**Pros**: No SPOF. Scales linearly. Each node is self-sufficient.
**Cons**: Requires URL routing protocol. Harder to get a global view of crawl progress. Debugging distributed state is painful.

### Approach 3: Message Queue-Based (Kafka)

```
   ┌───────────┐  ┌───────────┐  ┌───────────┐
   │ Crawler 0 │  │ Crawler 1 │  │ Crawler 2 │       (Producers + Consumers)
   └─────┬─────┘  └─────┬─────┘  └─────┬─────┘
         │               │               │
    ┌────┴───────────────┴───────────────┴────┐
    │              Apache Kafka                │
    │                                          │
    │  Topic: "urls-to-crawl"                  │
    │  ┌──────────┬──────────┬──────────┐     │
    │  │Partition 0│Partition 1│Partition 2│     │
    │  │          │          │          │     │
    │  │domain    │domain    │domain    │     │
    │  │group A   │group B   │group C   │     │
    │  └──────────┴──────────┴──────────┘     │
    │                                          │
    │  Topic: "crawled-content"                │
    │  ┌──────────┬──────────┬──────────┐     │
    │  │Partition 0│Partition 1│Partition 2│     │
    │  └──────────┴──────────┴──────────┘     │
    │                                          │
    │  Topic: "discovered-urls"                │
    │  ┌──────────┬──────────┬──────────┐     │
    │  │Partition 0│Partition 1│Partition 2│     │
    │  └──────────┴──────────┴──────────┘     │
    └──────────────────────────────────────────┘
              │                    │
              ↓                    ↓
    ┌──────────────┐    ┌──────────────────┐
    │  URL Router  │    │  Content Store   │
    │  (dedup +    │    │  (S3 / HDFS)     │
    │   re-enqueue)│    │                  │
    └──────────────┘    └──────────────────┘
```

Flow:
1. Seed URLs published to `urls-to-crawl`, partitioned by `Hash(domain)`
2. Crawlers are Kafka consumers in a consumer group. Each consumer reads from assigned partitions.
3. Crawler fetches page, publishes content to `crawled-content`, publishes discovered URLs to `discovered-urls`
4. URL Router service consumes `discovered-urls`, deduplicates, and publishes new URLs back to `urls-to-crawl`
5. Kafka handles persistence, rebalancing when crawlers join/leave, and exactly-once semantics (with transactions)

**Pros**: Battle-tested infrastructure. Kafka handles persistence, consumer group coordination, partition rebalancing automatically. Easy to add/remove crawlers. Built-in replay for recovery.
**Cons**: Additional infra dependency. Kafka's consumer rebalancing can cause pauses. Extra latency from message passing through Kafka rather than direct RPC.

### Coordination Comparison

| Aspect                | Centralized          | Decentralized        | Kafka-Based           |
|-----------------------|----------------------|----------------------|-----------------------|
| Single point of failure | Master is SPOF     | No SPOF              | Kafka cluster (replicated, so resilient) |
| Scalability ceiling   | ~50 workers          | ~1000+ nodes         | ~1000+ consumers      |
| Dedup approach        | Global set on master | Partitioned per node | Separate dedup service |
| Operational complexity | Low                 | High                 | Medium (Kafka is well-known) |
| Latency of URL routing | Direct              | RPC hop              | Kafka publish/consume hop |
| Recovery from failure | Restart master + replay | Repartition domains | Kafka rebalances consumers |
| **Best for**          | Small crawls (<100M) | Custom large-scale   | Production systems with existing Kafka |

---

## URL Dedup at Scale

The dedup problem: given billions of URLs already crawled, how do you efficiently answer "have I seen this URL before?" for every newly discovered URL.

### Approach 1: Centralized Set (Redis)

```
5 billion URLs
× 8 bytes per URL (assuming 8-byte hash stored, not full URL)
= 40 GB

Full URLs (average 80 bytes):
5B × 80 bytes = 400 GB  →  does not fit in one Redis instance
```

A Redis Cluster can shard across machines, but at 400 GB just for URLs, this is expensive and operationally heavy for a single purpose.

**When it works**: Crawls under ~500M URLs. Store URL hashes (8 bytes each) to fit ~4 GB in a single Redis instance.

### Approach 2: Distributed Bloom Filter

```
Standard Bloom Filter math:
  n = 5,000,000,000  (number of URLs)
  p = 0.01           (1% false positive rate)
  m = -n × ln(p) / (ln(2))^2 = ~47.9 billion bits = ~5.98 GB
  k = (m/n) × ln(2) = ~6.64 ≈ 7 hash functions

Partitioned across 10 nodes: ~600 MB per node
```

```
   Discovered URL: "https://example.com/page/42"

   Step 1: Hash(domain) → determines partition owner → Node 3

   Step 2: Node 3 checks its local Bloom filter
           ┌─────────────────────────────────────────────────┐
           │  Bloom Filter (Node 3's partition)               │
           │  600 MB, 4.8 billion bits                        │
           │                                                  │
           │  hash_1("https://example.com/page/42") → bit 729│
           │  hash_2("https://example.com/page/42") → bit 184│
           │  ...                                             │
           │  hash_7("https://example.com/page/42") → bit 503│
           │                                                  │
           │  All bits set? → YES → "Probably seen" (skip)    │
           │  Any bit unset? → NO → "Definitely new" (crawl)  │
           └─────────────────────────────────────────────────┘
```

**Trade-offs**:

| Property             | Standard Bloom        | Counting Bloom         |
|----------------------|-----------------------|------------------------|
| Memory               | ~6 GB for 5B URLs     | ~24 GB (4 bits/counter)|
| False positives      | ~1% at optimal k      | ~1% at optimal k       |
| Deletion support     | No                    | Yes (decrement counters)|
| Use case             | URLs never removed    | Recrawl scheduling needs removal |

**The 1% false positive reality**: At 5B URLs with 1% FP rate, roughly 50M URLs could be falsely skipped. For web crawling, this is acceptable -- those URLs will likely be rediscovered in future crawl cycles.

### Approach 3: Distributed Hash Table

```
   ┌─────────────────────────────────────────────────────────┐
   │              Distributed Dedup Store                     │
   │                                                         │
   │  ┌───────────┐  ┌───────────┐  ┌───────────┐          │
   │  │  Shard 0  │  │  Shard 1  │  │  Shard 2  │   ...    │
   │  │           │  │           │  │           │          │
   │  │ Redis /   │  │ Redis /   │  │ Redis /   │          │
   │  │ RocksDB / │  │ RocksDB / │  │ RocksDB / │          │
   │  │ Cassandra │  │ Cassandra │  │ Cassandra │          │
   │  │           │  │           │  │           │          │
   │  │ URL hash  │  │ URL hash  │  │ URL hash  │          │
   │  │ → state   │  │ → state   │  │ → state   │          │
   │  │ → ts      │  │ → ts      │  │ → ts      │          │
   │  └───────────┘  └───────────┘  └───────────┘          │
   │                                                         │
   │  Routing: Hash(URL) % num_shards → target shard        │
   └─────────────────────────────────────────────────────────┘
```

**Exact dedup** (no false positives). Scales horizontally by adding shards. Can store metadata alongside the URL (last crawl time, HTTP status, etc.). More expensive per-URL than Bloom filter but far more capable.

**Storage back-end options**:

| Back-End    | Latency (p99) | Durability     | Ops complexity | Best for                    |
|-------------|---------------|----------------|----------------|-----------------------------|
| Redis Cluster | ~1ms        | AOF/RDB (lossy)| Medium         | Speed-critical dedup         |
| RocksDB (local) | ~0.1ms   | WAL + SST      | Low            | Co-located with crawler node |
| Cassandra   | ~5ms          | Replicated     | High           | Multi-datacenter durability  |
| ScyllaDB    | ~2ms          | Replicated     | Medium         | High-throughput Cassandra alt|

### Approach 4: Checkpoint and Persist

Regardless of the in-memory structure chosen, you need durability.

```
   Normal operation:
   ┌──────────────┐     ┌─────────────────────────┐
   │  In-Memory   │────→│  Write-Ahead Log (WAL)   │
   │  Bloom /     │     │                           │
   │  Hash Set    │     │  Append-only file on disk │
   │              │     │  Every new URL appended   │
   └──────────────┘     └─────────────────────────┘
         │
         │  Every N minutes (or N URLs)
         ↓
   ┌──────────────────────────┐
   │  Full Checkpoint to Disk  │
   │                           │
   │  Serialized Bloom filter  │
   │  or RocksDB SST snapshot  │
   │                           │
   │  Stored on local SSD +    │
   │  replicated to S3/HDFS    │
   └──────────────────────────┘

   Recovery after crash:
   1. Load latest checkpoint
   2. Replay WAL entries after checkpoint
   3. Resume crawling (some URLs may be re-fetched -- at-least-once)
```

### Dedup Strategy Comparison

| Strategy             | Memory (5B URLs)| False Positives | Deletion | Persistence     | Complexity |
|----------------------|-----------------|-----------------|----------|-----------------|------------|
| Centralized Redis    | ~40 GB (hashes) | None            | Yes      | AOF/RDB         | Low        |
| Distributed Bloom    | ~6 GB total     | ~1%             | No*      | Checkpoint+WAL  | Medium     |
| Counting Bloom       | ~24 GB total    | ~1%             | Yes      | Checkpoint+WAL  | Medium     |
| Distributed Hash Table| Scales linearly| None            | Yes      | Built-in        | High       |

*Standard Bloom filter. Counting Bloom variant supports deletion.

---

## Crawl State Management

Every URL progresses through a state machine. Tracking this state reliably is critical for correctness and recoverability.

### URL State Machine

```
                    ┌─────────────┐
                    │  DISCOVERED  │
                    │ (just found) │
                    └──────┬──────┘
                           │
                     dedup check
                     passes
                           │
                           ↓
                    ┌─────────────┐
                    │   QUEUED     │←──────────────────┐
                    │ (in frontier)│                    │
                    └──────┬──────┘                    │
                           │                           │
                     dequeued by                  retry (with
                     fetcher                      backoff)
                           │                           │
                           ↓                           │
                    ┌─────────────┐              ┌─────┴──────┐
                    │  FETCHING   │─── fail ───→│  RETRY     │
                    │ (in-flight) │              │            │
                    └──────┬──────┘              │ count < max│
                           │                     └────────────┘
                      success                          │
                           │                     count >= max
                           ↓                           │
                    ┌─────────────┐                    ↓
                    │   FETCHED   │             ┌─────────────┐
                    │ (raw HTML)  │             │   FAILED    │
                    └──────┬──────┘             │ (permanent) │
                           │                    └─────────────┘
                      parse + extract
                      links
                           │
                           ↓
                    ┌─────────────┐
                    │   PARSED    │
                    │ (links      │
                    │  extracted) │
                    └──────┬──────┘
                           │
                      content written
                      to storage
                           │
                           ↓
                    ┌─────────────┐
                    │   STORED    │
                    │ (complete)  │
                    └─────────────┘
```

### State Storage

```
Per-URL record:
┌─────────────────────────────────────────────────────────┐
│  Key:   SHA-256(normalized_url)  (32 bytes)             │
│                                                         │
│  Value:                                                 │
│    state:          ENUM (3 bits)                        │
│    retry_count:    uint8                                │
│    last_fetch_ts:  uint64 (epoch millis)                │
│    next_retry_ts:  uint64 (epoch millis, if retrying)   │
│    http_status:    uint16                               │
│    content_hash:   uint64 (SimHash, for content dedup)  │
│    etag:           string (for conditional GET)         │
│    last_modified:  string (for conditional GET)         │
│                                                         │
│  Total: ~80-120 bytes per URL                           │
└─────────────────────────────────────────────────────────┘

5 billion URLs × 120 bytes = 600 GB
→ Must be distributed (RocksDB per node, or Cassandra cluster)
```

### Local Storage: RocksDB

Each crawler node stores state for its assigned domains in a local RocksDB instance.

```
   Crawler Node
   ┌──────────────────────────────────────┐
   │                                      │
   │  ┌────────────┐    ┌──────────────┐ │
   │  │  Frontier   │    │   RocksDB    │ │
   │  │  (priority  │    │              │ │
   │  │   queue)    │    │  URL → State │ │
   │  │             │    │  URL → Meta  │ │
   │  │  In-memory  │    │              │ │
   │  │  top of     │    │  LSM tree    │ │
   │  │  queue +    │    │  on SSD      │ │
   │  │  RocksDB    │    │              │ │
   │  │  overflow   │    │  WAL for     │ │
   │  │             │    │  durability  │ │
   │  └────────────┘    └──────────────┘ │
   │                                      │
   └──────────────────────────────────────┘
```

RocksDB advantages for crawl state:
- Embedded (no network hop). Sub-millisecond reads.
- LSM tree handles write-heavy workload efficiently.
- WAL provides durability across crashes.
- Compaction keeps read performance stable over time.
- Data lives on the same machine as the crawler -- no external dependency.

### Handling Retries

```
Retry with exponential backoff:

  Attempt 1 fails → wait 1 sec   → retry
  Attempt 2 fails → wait 2 sec   → retry
  Attempt 3 fails → wait 4 sec   → retry
  Attempt 4 fails → wait 8 sec   → retry
  Attempt 5 fails → wait 16 sec  → retry
  Attempt 6 fails → MARK AS PERMANENTLY FAILED

  Backoff formula: delay = min(base × 2^attempt, max_delay)
  With jitter:     delay = random(0, min(base × 2^attempt, max_delay))
```

Failure categories and actions:

| HTTP Status / Error     | Action                              | Retry? |
|-------------------------|-------------------------------------|--------|
| 200 OK                  | Parse and store                     | No     |
| 301/302 Redirect        | Follow (up to 5 hops), enqueue final URL | No |
| 403 Forbidden           | Respect, mark failed                | No     |
| 404 Not Found           | Mark failed, remove from future crawls | No  |
| 429 Too Many Requests   | Back off hard, increase politeness delay | Yes (long delay) |
| 500 Internal Error      | Retry with backoff                  | Yes    |
| 503 Service Unavailable | Retry with backoff                  | Yes    |
| Connection timeout      | Retry with backoff                  | Yes    |
| DNS failure             | Retry once, then mark failed        | Yes (1x) |
| SSL/TLS error           | Mark failed (bad cert)              | No     |

### Handling Stuck "FETCHING" State

A URL stuck in FETCHING means the fetcher crashed or the request hung beyond the HTTP timeout.

```
Heartbeat / lease mechanism:

  1. Fetcher takes URL: set state = FETCHING, lease_expiry = now + 60s
  2. Fetcher sends heartbeat every 15s: renew lease_expiry = now + 60s
  3. Background reaper thread scans for URLs where:
       state == FETCHING AND lease_expiry < now
     → Reset state to QUEUED, increment retry_count
  4. If retry_count > max → state = FAILED
```

---

## Fault Tolerance

### Node Failure Detection and Recovery

```
   ┌──────────────────────────────────────────────────┐
   │           Cluster Membership Service              │
   │       (ZooKeeper / etcd / gossip protocol)        │
   │                                                   │
   │  Node 0: alive (last heartbeat: 2s ago)          │
   │  Node 1: alive (last heartbeat: 1s ago)          │
   │  Node 2: DEAD  (last heartbeat: 35s ago)  ← !!  │
   │  Node 3: alive (last heartbeat: 0s ago)          │
   └───────────────────────┬──────────────────────────┘
                           │
                   Node 2 declared dead
                   after 30s without heartbeat
                           │
                           ↓
              ┌────────────────────────┐
              │  Recovery Procedure     │
              │                        │
              │  1. Identify domains   │
              │     assigned to Node 2 │
              │                        │
              │  2. Reassign domains   │
              │     to remaining nodes │
              │     via consistent hash│
              │     (next node on ring)│
              │                        │
              │  3. New owner loads    │
              │     Node 2's frontier  │
              │     from durable store │
              │     (RocksDB on shared │
              │      disk / S3 backup) │
              │                        │
              │  4. URLs in FETCHING   │
              │     state → reset to   │
              │     QUEUED             │
              │                        │
              │  5. Resume crawling    │
              └────────────────────────┘
```

### Data Loss Prevention Pipeline

```
   Fetcher                   Storage Pipeline
   ┌──────┐    ┌──────┐    ┌──────┐    ┌──────┐    ┌──────┐
   │Fetch │───→│Parse │───→│Write │───→│Confirm│───→│Mark  │
   │ HTML │    │ HTML │    │to S3/│    │write │    │URL as│
   │      │    │      │    │HDFS  │    │success│    │STORED│
   └──────┘    └──────┘    └──────┘    └──────┘    └──────┘
                                          │
                                     If write fails:
                                     URL stays in FETCHED
                                     Retry write later
                                     (content buffered locally)

   Key principle: URL state only advances when the NEXT stage
   confirms success. Never mark complete optimistically.
```

The critical invariant: **a URL is only marked STORED after content is durably written to the storage layer and acknowledged.** If the node crashes between fetch and store, the URL remains in FETCHING/FETCHED state. On recovery, it will be re-fetched. Since fetching is idempotent (GET requests), this is safe.

### At-Least-Once Semantics

```
   Scenario: Node crashes after fetching but before marking STORED

   Timeline:
   T=0   URL state: FETCHING
   T=1   HTTP GET succeeds, content in memory
   T=2   *** NODE CRASH ***
   T=3   (recovery) URL state still FETCHING, lease expired
   T=4   Reaper resets URL to QUEUED
   T=5   URL re-fetched by same or different node
   T=6   Content written to S3, URL marked STORED

   Result: Page was fetched twice. Content appears once in storage
           because storage layer does content dedup:

   Content dedup at storage:
     content_hash = SHA-256(normalized_html)
     S3 key = content_hash
     Writing the same content to same key = idempotent (no-op overwrite)
```

At-least-once is the practical guarantee for web crawlers. Exactly-once would require distributed transactions across fetch + store, which is prohibitively expensive and unnecessary -- content dedup at the storage layer makes duplicate fetches harmless.

### Failure Modes Summary

| Failure                         | Detection              | Recovery                                | Data Impact          |
|---------------------------------|------------------------|-----------------------------------------|----------------------|
| Single node crash               | Heartbeat timeout (30s)| Reassign domains, reload frontier       | Some URLs re-fetched |
| Network partition (node isolated)| Heartbeat timeout      | Isolated node pauses; rejoins later     | Temporary throughput drop |
| Disk failure on node            | I/O errors             | Node restarts, loads from S3 backup     | Frontier state from last checkpoint |
| Kafka broker failure            | Kafka replication      | Automatic failover to replica           | No data loss (replication factor 3) |
| Storage (S3) outage             | Write failures         | Buffer locally, retry; pause crawl if prolonged | Content backlogged locally |
| DNS infrastructure failure      | Resolution timeouts    | Fall back to cached DNS; pause new domains | Temporary crawl slowdown |

---

## Contrasts: Real-World Systems

### Googlebot

```
   Scale: ~billions of pages re-crawled continuously
   Infrastructure: Custom everything

   ┌──────────────────────────────────────────────────────┐
   │                   Google Crawl Stack                  │
   │                                                      │
   │  ┌──────────┐  Scheduling: Custom scheduler on Borg  │
   │  │  Borg    │  (replaced earlier GFS-based system)   │
   │  │ Scheduler│                                        │
   │  └────┬─────┘                                        │
   │       │                                              │
   │       ↓                                              │
   │  ┌──────────────────────────────────┐                │
   │  │  Thousands of Crawler Workers     │                │
   │  │  (across multiple data centers)   │                │
   │  │                                   │                │
   │  │  - Crawl scheduling per-URL      │                │
   │  │    (freshness-based priority)     │                │
   │  │  - Adaptive politeness           │                │
   │  │  - Per-IP rate limiting          │                │
   │  └──────────────┬───────────────────┘                │
   │                 │                                     │
   │                 ↓                                     │
   │  ┌──────────────────────────────────┐                │
   │  │  Bigtable                         │                │
   │  │  - URL table: state, metadata,   │                │
   │  │    crawl history, priority        │                │
   │  │  - Content table: raw HTML,      │                │
   │  │    parsed text, links             │                │
   │  │  - Scale: petabytes              │                │
   │  └──────────────────────────────────┘                │
   │                                                      │
   │  Key characteristics:                                │
   │  - Continuous crawl (no batch cycles)                │
   │  - Per-URL freshness estimation                      │
   │  - Crawl budget per site (not just rate limit)       │
   │  - Render JavaScript (headless Chrome at scale)      │
   └──────────────────────────────────────────────────────┘
```

Notable aspects:
- **Crawl budget**: Google assigns each site a crawl budget based on the site's importance and server capacity. It won't waste resources crawling low-value pages on even large sites.
- **Freshness estimation**: Each URL gets a predicted change rate. A news homepage might be re-crawled every 5 minutes. A corporate "About Us" page might be re-crawled monthly.
- **JavaScript rendering**: Googlebot renders pages with a headless Chrome instance to index JavaScript-heavy SPAs. This is enormously expensive -- it is essentially running a browser for every page.

### Apache Nutch

```
   Architecture: Batch processing on Hadoop

   ┌──────────────────────────────────────────────────────┐
   │              Apache Nutch Crawl Cycle                 │
   │                                                      │
   │  Cycle N:                                            │
   │                                                      │
   │  ┌──────────┐    MapReduce    ┌──────────────┐      │
   │  │ CrawlDB  │───────────────→│  GENERATE     │      │
   │  │ (HBase / │   "Which URLs  │  Fetch list   │      │
   │  │  HDFS)   │    to crawl?"  │  for this     │      │
   │  │          │                │  cycle         │      │
   │  └──────────┘                └──────┬───────┘      │
   │                                      │              │
   │                                      ↓              │
   │                              ┌──────────────┐      │
   │                              │   FETCH       │      │
   │                              │   MapReduce   │      │
   │                              │   job         │      │
   │                              │               │      │
   │                              │   Each mapper │      │
   │                              │   fetches a   │      │
   │                              │   batch of    │      │
   │                              │   URLs        │      │
   │                              └──────┬───────┘      │
   │                                      │              │
   │                                      ↓              │
   │                              ┌──────────────┐      │
   │                              │   PARSE       │      │
   │                              │   MapReduce   │      │
   │                              │   job         │      │
   │                              │               │      │
   │                              │   Extract     │      │
   │                              │   text, links,│      │
   │                              │   metadata    │      │
   │                              └──────┬───────┘      │
   │                                      │              │
   │                                      ↓              │
   │  ┌──────────┐                ┌──────────────┐      │
   │  │ CrawlDB  │←──────────────│   UPDATE      │      │
   │  │ (updated)│   Merge new   │   MapReduce   │      │
   │  │          │   URLs and    │   job         │      │
   │  └──────────┘   states      └──────────────┘      │
   │                                                      │
   │  Then: Cycle N+1 starts                              │
   └──────────────────────────────────────────────────────┘

   Historical note: Hadoop was originally extracted from Nutch's
   distributed file system (NDFS) and MapReduce implementations.
   Doug Cutting created both Nutch and Hadoop. Nutch begat Hadoop.
```

**Nutch trade-offs**:

| Advantage                         | Disadvantage                          |
|-----------------------------------|---------------------------------------|
| Scales via Hadoop cluster         | High latency between crawl cycles (hours) |
| Well-tested, open source          | Batch model: new URLs wait for next cycle |
| Integrates with Solr/Elasticsearch | Heavy JVM + Hadoop overhead per job  |
| Plugin architecture (parsers, filters) | MapReduce job startup overhead    |
| Fault tolerant (Hadoop handles failures) | Not suitable for real-time crawling |

### System Comparison

```
                    Googlebot          Nutch             Custom (this design)
                    ─────────          ─────             ───────────────────
   Model:          Continuous          Batch cycles      Continuous
   Scale:          Billions/day        Millions/cycle    Millions-Billions/week
   URL Storage:    Bigtable            HBase/HDFS        RocksDB + Cassandra
   Scheduling:     Borg                Hadoop YARN       Kafka consumer groups
   Crawl Latency:  Seconds-minutes     Hours (per cycle) Seconds-minutes
   JS Rendering:   Full (headless)     Plugin-based      Optional (headless)
   Politeness:     Per-site budget     robots.txt        Per-domain rate limit
   Fault Model:    Custom redundancy   Hadoop retries    At-least-once + dedup
   Team Size:      100s of engineers   Community OSS     Small team feasible
```

---

## Full Architecture: Putting It All Together

```
┌──────────────────────────────────────────────────────────────────────────┐
│                    Distributed Web Crawler Architecture                   │
│                                                                          │
│  ┌─────────────────────────────────────────────────────────────────┐    │
│  │                    Cluster Manager (etcd / ZooKeeper)            │    │
│  │  - Node membership    - Domain→Node assignment                  │    │
│  │  - Leader election    - Health monitoring                       │    │
│  └─────────────────────────────────┬───────────────────────────────┘    │
│                                    │                                     │
│         ┌──────────────────────────┼──────────────────────────┐         │
│         │                          │                          │         │
│         ↓                          ↓                          ↓         │
│  ┌─────────────────┐  ┌─────────────────┐  ┌─────────────────┐        │
│  │  Crawler Node 0  │  │  Crawler Node 1  │  │  Crawler Node 2  │        │
│  │  ┌─────────────┐ │  │  ┌─────────────┐ │  │  ┌─────────────┐ │        │
│  │  │  DNS Cache   │ │  │  │  DNS Cache   │ │  │  │  DNS Cache   │ │        │
│  │  └─────────────┘ │  │  └─────────────┘ │  │  └─────────────┘ │        │
│  │  ┌─────────────┐ │  │  ┌─────────────┐ │  │  ┌─────────────┐ │        │
│  │  │  Frontier    │ │  │  │  Frontier    │ │  │  │  Frontier    │ │        │
│  │  │  (priority Q)│ │  │  │  (priority Q)│ │  │  │  (priority Q)│ │        │
│  │  └─────────────┘ │  │  └─────────────┘ │  │  └─────────────┘ │        │
│  │  ┌─────────────┐ │  │  ┌─────────────┐ │  │  ┌─────────────┐ │        │
│  │  │  Politeness  │ │  │  │  Politeness  │ │  │  │  Politeness  │ │        │
│  │  │  Enforcer    │ │  │  │  Enforcer    │ │  │  │  Enforcer    │ │        │
│  │  └─────────────┘ │  │  └─────────────┘ │  │  └─────────────┘ │        │
│  │  ┌─────────────┐ │  │  ┌─────────────┐ │  │  ┌─────────────┐ │        │
│  │  │  Fetcher     │ │  │  │  Fetcher     │ │  │  │  Fetcher     │ │        │
│  │  │  Pool        │ │  │  │  Pool        │ │  │  │  Pool        │ │        │
│  │  └─────────────┘ │  │  └─────────────┘ │  │  └─────────────┘ │        │
│  │  ┌─────────────┐ │  │  ┌─────────────┐ │  │  ┌─────────────┐ │        │
│  │  │  RocksDB     │ │  │  │  RocksDB     │ │  │  │  RocksDB     │ │        │
│  │  │  (URL state) │ │  │  │  (URL state) │ │  │  │  (URL state) │ │        │
│  │  └─────────────┘ │  │  └─────────────┘ │  │  └─────────────┘ │        │
│  │  ┌─────────────┐ │  │  ┌─────────────┐ │  │  ┌─────────────┐ │        │
│  │  │  Bloom Filter│ │  │  │  Bloom Filter│ │  │  │  Bloom Filter│ │        │
│  │  │  (dedup)     │ │  │  │  (dedup)     │ │  │  │  (dedup)     │ │        │
│  │  └─────────────┘ │  │  └─────────────┘ │  │  └─────────────┘ │        │
│  └────────┬────────┘  └────────┬────────┘  └────────┬────────┘        │
│           │                     │                     │                 │
│           │    URL routing (discovered URLs sent      │                 │
│           │    to owning node by domain hash)         │                 │
│           ├─────────────────────┼─────────────────────┤                 │
│           │                     │                     │                 │
│           ↓                     ↓                     ↓                 │
│  ┌─────────────────────────────────────────────────────────────────┐   │
│  │                   Message Bus (Kafka / gRPC)                     │   │
│  │                                                                  │   │
│  │  Topics: discovered-urls, crawled-content, url-state-updates     │   │
│  └──────────────────────────────┬──────────────────────────────────┘   │
│                                 │                                      │
│                                 ↓                                      │
│  ┌─────────────────────────────────────────────────────────────────┐   │
│  │                    Storage Layer                                  │   │
│  │                                                                  │   │
│  │  ┌──────────────┐   ┌──────────────┐   ┌──────────────────┐    │   │
│  │  │  S3 / HDFS   │   │  Elasticsearch│   │  Cassandra       │    │   │
│  │  │  (raw HTML   │   │  (indexed     │   │  (URL metadata,  │    │   │
│  │  │   + content) │   │   content for │   │   crawl history) │    │   │
│  │  │              │   │   search)     │   │                  │    │   │
│  │  └──────────────┘   └──────────────┘   └──────────────────┘    │   │
│  └─────────────────────────────────────────────────────────────────┘   │
│                                                                          │
└──────────────────────────────────────────────────────────────────────────┘
```

### Data Flow Summary

```
1. Seed URLs injected
       │
       ↓
2. URL routed to owning node (domain hash)
       │
       ↓
3. Dedup check (Bloom filter on owning node)
       │
       ├── Already seen → discard
       │
       └── New → add to frontier
                    │
                    ↓
4. Politeness enforcer gates fetch timing
       │
       ↓
5. Fetcher pool issues HTTP GET
       │
       ↓
6. Raw HTML → parser → extracted links + content
       │                    │
       │                    └── Links routed to owning nodes (step 2)
       ↓
7. Content written to S3/HDFS
       │
       ↓
8. URL state updated to STORED in RocksDB
       │
       ↓
9. Content indexed in Elasticsearch (async)
```

---

## Key Takeaways

1. **Domain-based partitioning** is the standard for web crawlers because it keeps politeness enforcement local. The power-law skew is real but manageable with consistent hashing and work-stealing.

2. **Decentralized coordination** scales better than centralized, but Kafka-based coordination is the pragmatic middle ground -- it provides durability, scalability, and consumer group management out of the box.

3. **Bloom filters** are the go-to for URL dedup at scale. The 1% false positive rate is acceptable because missed URLs will be rediscovered in future crawl cycles.

4. **At-least-once semantics** are the right trade-off for crawling. Content dedup at the storage layer makes duplicate fetches harmless, and the alternative (exactly-once) requires expensive distributed transactions.

5. **RocksDB per node** for crawl state gives you sub-millisecond local reads with WAL durability. This is far better than making a network call to a remote database for every URL state transition.

6. **Fault tolerance comes from durability + idempotency**, not from preventing failures. Assume nodes will crash. Make sure frontier state survives (RocksDB + checkpoints to S3), and make sure re-fetching is harmless (content dedup).
