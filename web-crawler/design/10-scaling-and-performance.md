# 10 - Scaling and Performance

> How big is the web, how fast must we crawl, and where do the bottlenecks hide?

This document puts hard numbers on every dimension of a large-scale web crawler,
walks through back-of-envelope calculations an interviewer expects, identifies
the bottlenecks, and catalogs the optimizations that let you move from a
toy prototype to a production system crawling billions of pages.

---

## 1. Scale Numbers

### 1.1 The Web Itself

| Metric | Number | Source / Note |
|---|---|---|
| Google's indexed pages | ~400 billion | Google antitrust trial testimony (2023) |
| Google's official statement | "hundreds of billions of webpages" | Google Search documentation |
| Active websites | ~1.1 billion | Netcraft survey (registered domains with content) |
| Total registered domains | ~350 million | ICANN / Verisign domain reports |
| New pages per day | Billions added, modified, or removed | The web is not static; freshness is a first-class concern |

The web is enormous **and** constantly churning. A crawler is never "done." It must
re-crawl known pages to detect changes while simultaneously discovering new ones.

### 1.2 Google's Crawl Rate

| Metric | Number | Source / Note |
|---|---|---|
| Pages indexed | Hundreds of billions | Google official docs |
| Googlebot activity growth | 96% increase May 2024 to May 2025 | Cloudflare Radar report |
| Googlebot share of all crawling | ~50% of all bot crawl traffic | Cloudflare Radar report |
| Estimated crawl machines | Tens of thousands | Public infrastructure papers (GFS, Bigtable, Borg) |
| Infrastructure | Bigtable, MapReduce, Colossus/GFS, Borg | Google systems papers |

Google's crawler is the largest single consumer of web bandwidth on the planet.
Its architecture is the gold standard, but it operates at a scale roughly
**1,000x** what an interview question typically targets.

### 1.3 Common Crawl (Public Reference Point)

| Metric | Number |
|---|---|
| Pages per monthly crawl | ~2.5 - 3.5 billion |
| WARC data per crawl | 350 - 460 TiB |
| Unique hosts per crawl | ~30 - 40 million |
| Crawl duration | ~1 month continuous |

Common Crawl is the best publicly documented large-scale crawl. It provides a
realistic mid-point between a toy project and Googlebot.

### 1.4 Interview-Scale Target

The canonical interview problem:

> Crawl **1 billion pages** in a **1-week** cycle.

Deriving the throughput requirement:

```
1,000,000,000 pages / (7 days x 86,400 sec/day)
= 1,000,000,000 / 604,800
= ~1,653 pages/sec
```

With overhead (retries, politeness delays, failures, re-crawl priority):

> **Target: 2,000 - 5,000 pages/sec sustained**

This is the number every subsequent calculation is anchored to.

---

## 2. Back-of-Envelope Calculations

### 2.1 Master Summary Table

| Resource | Calculation | Result | Notes |
|---|---|---|---|
| **Raw storage** | 1B pages x 100 KB avg | 100 TB | Uncompressed HTML |
| **Compressed storage** | 100 TB / 5 (gzip ratio) | ~20 TB | gzip typically achieves 5x on HTML |
| **Metadata storage** | 1B pages x 500 bytes | ~500 GB | URL, timestamp, content hash, HTTP headers, etc. |
| **Total storage** | 20 TB + 500 GB | ~20.5 TB | Per crawl cycle; multiply by retention window |
| **Network bandwidth** | 5,000 pages/sec x 100 KB | 500 MB/sec = **4 Gbps** | Sustained; need headroom for bursts |
| **DNS lookups** | 5,000 pages/sec x 50% cache miss | 2,500 lookups/sec | Assumes 50% cache hit rate (conservative) |
| **URL dedup set (1B)** | 1B URLs x 8 bytes (fingerprint) | 8 GB | Fits in RAM on a single machine |
| **URL dedup set (10B)** | 10B URLs x 8 bytes | 80 GB | Needs sharding across machines |
| **Bloom filter (10B)** | 10 bits/URL x 10B URLs | ~12 GB | 1% false positive rate; fits single machine |
| **Frontier queue** | 10-100M URLs at steady state | < 10 GB | In-memory feasible; RocksDB on disk trivially handles it |
| **Crawler machines** | 5,000 pages/sec / 500 per machine | **10 machines** | Each machine: async I/O, 500 concurrent connections |

### 2.2 Storage Breakdown

```
Per crawl cycle:
  Raw HTML:     1B x 100 KB         = 100 TB
  Compressed:   100 TB / 5          =  20 TB
  Metadata:     1B x 500 B          = 500 GB
  URL index:    1B x ~100 B         = 100 GB
  Link graph:   10B edges x 16 B    = 160 GB
                                     --------
  Total per cycle:                   ~21 TB

With 4-week retention (4 snapshots):  ~84 TB
With dedup across snapshots:          ~50-60 TB (many pages unchanged)
```

Storage technology choices:

| Tier | Technology | Use Case |
|---|---|---|
| Hot (current crawl) | Local SSD / NVMe | Active frontier, dedup set, metadata writes |
| Warm (recent crawls) | S3 / GCS Standard | WARC archives, queryable metadata |
| Cold (historical) | S3 Glacier / GCS Coldline | Long-term archival, compliance |

### 2.3 Network Bandwidth Breakdown

| Component | Bandwidth | Notes |
|---|---|---|
| HTTP response bodies | 500 MB/sec (4 Gbps) | Dominant cost |
| HTTP headers overhead | ~25 MB/sec | ~5 KB headers per request |
| DNS traffic | ~2.5 MB/sec | UDP packets, negligible |
| Internal cluster traffic | ~100 MB/sec | Frontier distribution, metadata writes |
| **Total egress** | **~625 MB/sec (~5 Gbps)** | Need 10 Gbps NIC per machine or distribute |

A single machine with a 10 Gbps NIC can theoretically handle this, but in
practice you distribute across 10+ machines, each consuming ~500 Mbps.
Geo-distributing crawlers (US, EU, Asia) also reduces cross-continent latency.

### 2.4 DNS Deep Dive

| Metric | Value |
|---|---|
| Lookups/sec at 5K pages/sec | 5,000 (worst case, 0% cache hit) |
| Expected cache hit rate | 50-80% (power-law: popular domains repeat) |
| Effective lookups/sec | 1,000 - 2,500 |
| Local resolver capacity (unbound) | 10,000+ queries/sec |
| TTL for caching | Respect DNS TTL, minimum floor of 5 min |
| Fallback resolvers | Multiple upstream (8.8.8.8, 1.1.1.1, self-hosted) |

A single `unbound` instance handles interview-scale easily. At Google-scale,
you run a fleet of caching resolvers and use asynchronous resolution (c-ares)
to avoid blocking fetcher threads on DNS.

### 2.5 Machine Count at Different Scales

| Scale | Pages/sec | Machines (@ 500 pg/s each) | Storage/cycle | Bandwidth |
|---|---|---|---|---|
| Interview (1B/week) | ~2,000 - 5,000 | 4 - 10 | ~20 TB | ~4 Gbps |
| Common Crawl (3B/month) | ~1,200 | 3 - 5 | ~60 TB | ~1 Gbps |
| Mid-tier search engine | ~50,000 | 100 | ~200 TB | ~40 Gbps |
| Googlebot (estimated) | ~230,000+ | Thousands | Petabytes | Hundreds of Gbps |

---

## 3. Bottleneck Analysis

### 3.1 Bottleneck Ranking Table

| Rank | Bottleneck | Impact | Latency Contribution | Mitigation |
|---|---|---|---|---|
| 1 | **Network I/O** | Dominates wall-clock time | 200-2000 ms per page (DNS + TCP + TLS + transfer) | Async I/O, connection pooling, HTTP keep-alive, geo-distribution |
| 2 | **DNS resolution** | Serializes fetches if synchronous | 10-200 ms per lookup (uncached) | Aggressive caching, prefetching, async resolver (c-ares), local unbound |
| 3 | **Politeness delays** | Intentional throttle per domain | 1-10 sec between requests to same host | Maximize domain parallelism; crawl many domains concurrently |
| 4 | **Content parsing** | CPU-bound; scales with page size | 1-50 ms per page (HTML parse + link extract) | Parallelize across cores, separate fetcher/parser workers |
| 5 | **Frontier operations** | Dequeue + enqueue + dedup per URL | < 1 ms (must stay here) | Hash-based dedup O(1), heap-based politeness O(log n) |
| 6 | **Storage writes** | I/O-bound at scale | 1-10 ms per page (batched) | Batch writes, compression, append-only WARC, S3 multipart |

### 3.2 Network I/O: The Primary Bottleneck

Network I/O dominates because every page fetch involves:

```
DNS lookup:           10 - 200 ms  (uncached)
TCP handshake:        20 - 100 ms  (1 RTT)
TLS handshake:        40 - 200 ms  (1-2 RTTs)
HTTP request/response: 50 - 1000 ms (depends on server + page size)
                      -------------------
Total per page:       120 - 1500 ms
```

At 5,000 pages/sec with 500 ms average latency, you need:

```
5,000 pages/sec x 0.5 sec = 2,500 concurrent connections
```

A single machine can handle ~500 concurrent async connections comfortably.
Hence **5-10 machines** at this concurrency level.

**Key mitigations:**

| Technique | Benefit | Implementation |
|---|---|---|
| Async I/O (epoll/kqueue) | Thousands of concurrent connections per thread | Netty, libuv, asyncio, tokio |
| Connection pooling | Eliminate TCP+TLS handshake for repeat domains | Per-host connection pool, max 1-2 connections per host |
| HTTP keep-alive | Reuse TCP connection for multiple requests | Default in HTTP/1.1; explicit in HTTP/1.0 |
| HTTP/2 multiplexing | Multiple requests over single connection | Reduces connection overhead for multi-page domains |
| Geo-distributed crawlers | Reduce RTT to target servers | Crawl EU sites from EU, Asia from Asia, etc. |
| Compressed responses | Reduce transfer size by 5x | Send `Accept-Encoding: gzip, br` header |

### 3.3 DNS Resolution: The Secondary Bottleneck

DNS is dangerous because a single synchronous lookup can block a fetcher
thread for up to 200 ms (or 30 seconds on timeout). At 5,000 pages/sec,
synchronous DNS would require 5,000 threads just for DNS waits.

**Solution stack:**

1. **Local caching resolver** (unbound): handles 10K+ queries/sec, respects TTLs
2. **Application-level cache**: in-process HashMap with TTL, avoids even the local resolver hop
3. **Async resolution** (c-ares / Netty DNS): non-blocking, integrates with event loop
4. **Prefetching**: when a page is dequeued from frontier, start DNS resolution for
   the next N pages in that domain's queue
5. **Negative caching**: cache NXDOMAIN results to avoid repeated lookups for dead domains

### 3.4 Politeness: The Intentional Bottleneck

Politeness (respecting `Crawl-delay` and rate-limiting per domain) is a bottleneck
**by design**. You cannot optimize it away; you work around it:

```
If Crawl-delay = 5 sec for example.com:
  Max rate for example.com = 1 page / 5 sec = 0.2 pages/sec

To maintain 5,000 pages/sec overall:
  Must crawl 5,000 / 0.2 = 25,000 domains concurrently (worst case)
  Realistically, most domains have no Crawl-delay → much fewer needed
```

The frontier must support **high domain parallelism**: thousands of domains
active simultaneously, each with its own rate limiter.

### 3.5 Content Parsing: The CPU Bottleneck

Parsing HTML and extracting links is CPU-bound:

```
HTML parse (jsoup/lxml):  1 - 10 ms per page
Link extraction:          0.1 - 1 ms per page
URL normalization:        0.01 ms per URL x ~50 URLs = 0.5 ms
                          ---------
Total:                    ~2 - 12 ms per page
```

At 5,000 pages/sec on a single core: 5,000 x 10 ms = 50 seconds of CPU per second.
You need **at least 50 cores** dedicated to parsing at peak. This is why large crawlers
separate **fetcher** and **parser** into different worker pools or even different machines.

### 3.6 The Pipeline View

```
                    Network-bound          CPU-bound       I/O-bound
                   ┌─────────────┐       ┌───────────┐   ┌──────────┐
  Frontier ──────> │  DNS + HTTP  │ ────> │  Parse +   │ ──>│  Store   │ ──> Frontier
  (dequeue)        │  Fetch       │       │  Extract   │   │  (WARC)  │    (enqueue)
                   └─────────────┘       └───────────┘   └──────────┘
  Parallelism:          High                Medium           Low
  Concurrency:       2,500 conns          50+ cores       Batched I/O
  Scaling:          Add machines         Add cores/VMs    Add disks/S3
```

Each stage has different parallelism needs. Connecting them with queues
(in-memory or Kafka) lets each stage scale independently.

---

## 4. Performance Optimizations

### 4.1 Pipeline Architecture

The single most impactful optimization is decomposing the crawler into
**independent, queue-connected stages**:

```
┌──────────┐    ┌──────────┐    ┌──────────┐    ┌──────────┐    ┌──────────┐
│ Frontier  │───>│ DNS      │───>│ HTTP     │───>│ Parser   │───>│ Content  │
│ Dequeue   │    │ Resolver │    │ Fetcher  │    │ + Dedup  │    │ Store    │
│           │    │          │    │          │    │          │    │          │
│ 1 thread  │    │ async    │    │ async    │    │ N threads│    │ batch    │
│           │    │ c-ares   │    │ Netty    │    │ per core │    │ writer   │
└──────────┘    └──────────┘    └──────────┘    └──────────┘    └──────────┘
      │                                               │
      └───────────────── enqueue new URLs ────────────┘
```

**Benefits:**
- Each stage scales independently (add more fetcher machines without changing parsers)
- Backpressure propagates naturally (if store is slow, parser queue fills, fetcher slows)
- Different failure domains (DNS outage does not crash the parser)
- Easier to monitor (queue depths reveal bottlenecks)

### 4.2 Batch Operations

| Operation | Naive | Batched | Improvement |
|---|---|---|---|
| DNS lookups | 1 query per page | Prefetch next 100 domains | Amortize UDP overhead; pipeline with fetch |
| Dedup checks | 1 Bloom filter probe per URL | Batch 1000 URLs into single lock acquisition | Reduce lock contention |
| Content store writes | 1 write per page | Buffer 100 pages, write single WARC block | Sequential I/O, fewer syscalls |
| Metadata DB writes | 1 INSERT per page | Batch INSERT 500 rows | Database round-trip amortization |
| Frontier enqueue | 1 enqueue per discovered URL | Batch 50 URLs from same page | Single lock acquisition for queue |

### 4.3 Connection Reuse

HTTP keep-alive and connection pooling are critical for multi-page domains:

```
Without keep-alive (per page):
  TCP handshake:   50 ms
  TLS handshake:  100 ms
  HTTP exchange:  200 ms
  Total:          350 ms

With keep-alive (subsequent pages on same domain):
  HTTP exchange:  200 ms
  Total:          200 ms
  Savings:        43% latency reduction per page
```

Implementation rules:
- Maintain a per-host connection pool (max 1-2 connections per host for politeness)
- Close idle connections after 30 seconds
- Respect server's `Connection: close` header
- Track connection age; recycle after 100 requests or 5 minutes

### 4.4 Compression

```
Request header:
  Accept-Encoding: gzip, deflate, br

Impact:
  Average HTML page:  100 KB uncompressed
  With gzip:           20 KB (5x reduction)
  With Brotli:         15 KB (6.5x reduction)

Bandwidth savings at 5,000 pages/sec:
  Uncompressed: 500 MB/sec = 4 Gbps
  Compressed:   100 MB/sec = 0.8 Gbps
  Savings:      400 MB/sec = 3.2 Gbps
```

Most modern web servers serve compressed responses by default. This is
essentially free bandwidth savings -- just set the request header.

The tradeoff: decompression costs CPU (~0.1 ms per page for gzip), but this
is negligible compared to the bandwidth savings.

### 4.5 Early Termination

Not every HTTP response deserves full processing. Abort early to save resources:

| Check | When | Action | Savings |
|---|---|---|---|
| `Content-Length > 10 MB` | After headers received | Abort connection | Skip huge files (videos, archives) |
| `Content-Type` not `text/html` | After headers received | Skip or minimal processing | Skip images, PDFs, binaries |
| `HEAD` request first | Before GET | Check type + size cheaply | 1 small request instead of large GET |
| Duplicate content hash | After partial download (~4 KB) | Abort if simhash matches known page | Skip re-downloading unchanged pages |
| robots.txt disallowed | Before any request | Skip entirely | Zero network cost |

**HEAD-first strategy:**

```
For unknown URLs:
  1. Send HEAD request (~200 bytes response)
  2. Check Content-Type, Content-Length, Last-Modified
  3. If acceptable, send GET request
  4. Overhead: 1 extra RTT (~50 ms)
  5. Savings: avoid downloading 10 MB video files

Trade-off: extra RTT for every page vs. occasionally downloading junk.
Typically only used for unknown domains or URLs with suspicious extensions.
```

### 4.6 Adaptive Parallelism

Not all crawl targets are equal. Adapt concurrency dynamically:

```
Fast server (< 100 ms response):
  → Increase concurrent requests (up to politeness limit)
  → Prefetch more URLs from this domain

Slow server (> 2 sec response):
  → Reduce concurrent requests to 1
  → Increase timeout tolerance
  → Deprioritize in frontier

Failing server (5xx errors):
  → Exponential backoff: 1s → 2s → 4s → 8s → ... → 1 hour
  → Move domain to low-priority queue
  → Alert if high-value domain
```

### 4.7 Memory Management

At 5,000 pages/sec with 100 KB average, pages flow through memory at 500 MB/sec.
Without careful management, GC pauses (Java) or memory fragmentation (C++) will kill throughput.

| Technique | Benefit |
|---|---|
| Object pooling (byte buffers) | Avoid allocation/GC churn for HTTP response bodies |
| Streaming parse | Parse HTML as it arrives; do not buffer entire page |
| Off-heap storage (ByteBuffer.allocateDirect) | Reduce GC pressure for large buffers |
| Bounded queues between stages | Backpressure prevents OOM |
| Memory-mapped Bloom filter | OS manages paging; works beyond physical RAM |

---

## 5. Monitoring and Alerting

### 5.1 Key Metrics Table

| Metric | Target (1B/week) | Alert Threshold | What It Tells You |
|---|---|---|---|
| **Pages fetched/sec** (total) | 2,000 - 5,000 | < 1,500 for 5 min | Overall throughput health |
| **Pages fetched/sec** (per domain) | Varies by domain | > 2x `Crawl-delay` rate | Politeness violation risk |
| **HTTP error rate** | < 5% | > 10% for 5 min | Server issues, blocking, or bug |
| **Error rate by type** (4xx, 5xx, timeout, DNS) | Varies | Any type > 3% | Pinpoints failure category |
| **Frontier queue depth** | 10M - 100M | > 500M (growing unboundedly) | Spider trap or scope issue |
| **Frontier queue depth** (low) | 10M - 100M | < 1M (draining) | Crawl finishing or discovery failure |
| **Content store growth** | ~2.5 TB/day | < 1 TB/day | Throughput problem or high dup rate |
| **Duplicate rate** | 20-40% | > 60% | URL normalization bug or spider trap |
| **Unique domains crawled** | Growing over time | Plateaued | Discovery problem |
| **DNS cache hit rate** | 50-80% | < 30% | Cache too small or TTL too short |
| **DNS resolution latency (p99)** | < 100 ms | > 500 ms | Resolver overloaded or upstream issue |
| **HTTP fetch latency (p50)** | 200-500 ms | > 2 sec | Network issue or slow targets |
| **HTTP fetch latency (p99)** | < 5 sec | > 10 sec | Timeout tuning needed |
| **Parser queue depth** | < 10,000 | > 100,000 | Parser cannot keep up with fetcher |
| **Content store write latency** | < 50 ms (batch) | > 500 ms | Disk/S3 issue |
| **Bloom filter false positive rate** | < 1% | > 5% | Filter saturated; needs resizing |
| **Robots.txt cache hit rate** | > 90% | < 70% | Too many unique domains or cache eviction |
| **Memory usage (heap)** | < 80% | > 90% | OOM risk; need to tune or scale |

### 5.2 Anomaly Detection

| Anomaly | Possible Cause | Automated Response |
|---|---|---|
| Sudden crawl rate drop (> 50%) | Network failure, DNS outage, fetcher crash | Page operator, restart failed fetchers, failover DNS |
| Error rate spike (> 10%) | Target site blocking, fetcher bug, TLS issue | Increase backoff for affected domains, alert on-call |
| Frontier growing unboundedly | Spider trap, infinite calendar, parameterized URLs | Activate per-host URL limit, increase dedup aggressiveness |
| Frontier draining to zero | Scope too narrow, seed URLs exhausted | Inject new seed URLs, widen domain scope |
| Duplicate rate spike (> 60%) | URL normalization failure, canonicalization bug | Review recent code changes, check URL normalizer |
| DNS resolution failures spike | Upstream resolver down, rate-limited by provider | Failover to backup resolvers, increase local cache TTL |
| Storage write latency spike | Disk full, S3 throttling, network partition | Rotate to new disk, increase S3 request rate limit, alert |
| Single domain dominating queue | Aggressive spider trap from one domain | Apply per-domain URL cap, deprioritize domain |

### 5.3 Per-Domain Monitoring

For high-value domains (top 10,000 by PageRank or business value):

```
Dashboard per domain:
  - Pages crawled in last 24h
  - Error rate (4xx, 5xx, timeout)
  - Average response time
  - robots.txt last fetched + rules
  - Crawl-delay being respected?
  - Blocked? (increasing 403/429 rate)
  - Content freshness (% pages changed since last crawl)
```

**Blocking detection heuristic:**

```
If domain X shows:
  - 403 rate > 50% in last hour (was < 5% yesterday)
  - OR response bodies contain CAPTCHA markers
  - OR response time suddenly drops to < 10ms (serving cached block page)
Then:
  - Flag domain as "potentially blocking"
  - Reduce crawl rate by 90%
  - Alert operator for manual review
  - Do NOT circumvent blocks (ethical and legal obligation)
```

### 5.4 Operational Dashboards

```
┌─────────────────────────────────────────────────────────────────┐
│  Web Crawler - Operations Dashboard                             │
├─────────────────────────────────────────────────────────────────┤
│                                                                  │
│  Throughput          Errors              Frontier                │
│  ┌──────────┐       ┌──────────┐       ┌──────────┐            │
│  │ 4,231    │       │  2.3%    │       │  47.2M   │            │
│  │ pages/sec│       │ error    │       │  URLs    │            │
│  │ ▁▃▅▇▇▆▇ │       │ ▁▁▁▂▁▁▁ │       │ ▅▅▆▆▆▇▇ │            │
│  └──────────┘       └──────────┘       └──────────┘            │
│                                                                  │
│  Storage             DNS                 Domains                 │
│  ┌──────────┐       ┌──────────┐       ┌──────────┐            │
│  │ 12.4 TB  │       │  72%     │       │  1.2M    │            │
│  │ stored   │       │ cache hit│       │ unique   │            │
│  │ ▁▂▃▄▅▆▇ │       │ ▇▇▆▇▇▇▇ │       │ ▁▂▃▄▅▆▇ │            │
│  └──────────┘       └──────────┘       └──────────┘            │
│                                                                  │
│  Top Errors: timeout (1.1%) > 5xx (0.7%) > 4xx (0.4%) > DNS    │
│  Machines: 10/10 healthy | Queue: balanced across 8 partitions  │
└─────────────────────────────────────────────────────────────────┘
```

---

## 6. Scaling Strategies

### 6.1 Horizontal Scaling (Add Machines)

The primary scaling axis. Each crawler machine is stateless (frontier state
is in a shared store), so adding machines is straightforward:

```
Current: 10 machines x 500 pages/sec = 5,000 pages/sec
Need 2x: 20 machines x 500 pages/sec = 10,000 pages/sec
```

**Partitioning strategy:** Shard by domain hash. Each machine "owns" a set
of domains, ensuring politeness is enforced locally without coordination.

```
machine_id = hash(domain) % num_machines

Benefits:
  - Politeness enforced locally (no distributed rate limiter needed)
  - Connection pools are per-machine (reuse connections to owned domains)
  - Domain-level state (robots.txt, crawl history) is local

Drawbacks:
  - Rebalancing on machine add/remove (consistent hashing mitigates)
  - Hot domains (wikipedia.org) overload one machine (split by path prefix)
```

### 6.2 Vertical Scaling (Bigger Machines)

Sometimes cheaper than horizontal:

| Resource | Upgrade | Effect |
|---|---|---|
| RAM: 16 GB to 128 GB | Bloom filter + frontier + DNS cache all in-memory | Eliminate disk I/O for hot data |
| NIC: 1 Gbps to 25 Gbps | Single machine handles full bandwidth | Fewer machines, simpler ops |
| CPU: 8 to 64 cores | Parser parallelism on one box | Fewer machines for parse stage |
| Disk: HDD to NVMe | WARC write throughput 10x | Storage ceases to be a bottleneck |

### 6.3 Geo-Distribution

```
┌─────────────┐     ┌─────────────┐     ┌─────────────┐
│  US-East     │     │  EU-West     │     │  AP-East     │
│  Crawl .com  │     │  Crawl .eu   │     │  Crawl .jp   │
│  .org, .net  │     │  .de, .fr    │     │  .cn, .kr    │
│  5 machines  │     │  3 machines  │     │  2 machines  │
└──────┬───────┘     └──────┬───────┘     └──────┬───────┘
       │                     │                     │
       └─────────────────────┼─────────────────────┘
                             │
                    ┌────────┴────────┐
                    │  Central Store   │
                    │  (S3 / GCS)      │
                    │  + Metadata DB   │
                    └─────────────────┘
```

Benefits:
- 50-200 ms RTT savings per request (significant at 5,000 pages/sec)
- Better compliance with regional data laws
- Resilience to regional network outages

Complexity:
- Distributed frontier coordination
- Cross-region deduplication
- Operational overhead of multi-region deployment

---

## 7. Contrasts: Real Systems

### 7.1 Googlebot

| Aspect | Interview Crawler | Googlebot |
|---|---|---|
| Scale | 1B pages/week | 100s of billions indexed, continuous |
| Machines | ~10 | Tens of thousands |
| Storage | ~20 TB/cycle | Petabytes (Colossus/GFS) |
| Metadata store | PostgreSQL / Redis | Bigtable |
| Processing | Single pipeline | MapReduce / Flume pipelines |
| Orchestration | Cron + scripts | Borg (predecessor to Kubernetes) |
| Frontier | Redis / RocksDB | Custom distributed priority queue |
| Freshness | Weekly re-crawl | Continuous; minutes for news, days/weeks for static |
| Monitoring | Grafana + PagerDuty | Borgmon (predecessor to Prometheus), self-healing |
| Rendering | Skip JavaScript (or headless for subset) | Full JavaScript rendering (Chromium-based WRS) |
| Scale factor | 1x | ~1,000x |

Googlebot's key architectural differences:
- **Continuous crawl** rather than batch cycles (no "start" and "finish")
- **Priority-based re-crawl**: news pages every few minutes, static pages every few weeks
- **Full rendering**: runs JavaScript in headless Chromium (Web Rendering Service)
- **Self-healing**: automatically detects and recovers from failures without human intervention
- **Deeply integrated** with indexing, ranking, and serving (not a standalone system)

### 7.2 Apache Nutch

| Aspect | Interview Crawler | Apache Nutch |
|---|---|---|
| Architecture | Custom async pipeline | Hadoop MapReduce jobs |
| Crawl cycle | Continuous or streaming | Batch: generate → fetch → parse → update |
| Scaling model | Add crawler machines | Add Hadoop nodes |
| Storage | WARC files on S3 | HDFS (HBase for crawl state) |
| Throughput | 2,000-5,000 pages/sec | Variable; batch-limited by MapReduce overhead |
| Latency between cycles | None (continuous) | Hours (full MapReduce cycle) |
| Strength | Low latency, real-time | Proven at scale, open-source, integrates with Solr/ES |
| Weakness | Must build everything | High latency, batch-oriented, complex Hadoop ops |

Nutch's batch model:

```
Cycle 1:                    Cycle 2:
┌──────────┐               ┌──────────┐
│ Generate  │ (select URLs) │ Generate  │
│ (MR job)  │               │ (MR job)  │
└────┬──────┘               └────┬──────┘
     │                           │
┌────┴──────┐               ┌────┴──────┐
│ Fetch     │ (crawl pages) │ Fetch     │
│ (MR job)  │               │ (MR job)  │
└────┬──────┘               └────┬──────┘
     │                           │
┌────┴──────┐               ┌────┴──────┐
│ Parse     │ (extract)     │ Parse     │
│ (MR job)  │               │ (MR job)  │
└────┬──────┘               └────┬──────┘
     │                           │
┌────┴──────┐               ┌────┴──────┐
│ UpdateDB  │ (merge state) │ UpdateDB  │
│ (MR job)  │               │ (MR job)  │
└───────────┘               └───────────┘

Time: ──────2-6 hours──────>──────2-6 hours──────>
```

Each cycle is 4 MapReduce jobs. The overhead of launching jobs, shuffling data,
and coordinating reduces effective throughput. The advantage is that Hadoop
handles fault tolerance, distribution, and storage automatically.

### 7.3 Summary Comparison

| Dimension | Interview Crawler | Nutch | Googlebot |
|---|---|---|---|
| Complexity to build | Medium | Low (open source) | Extreme (decade+ of engineering) |
| Latency | Low (continuous) | High (batch) | Very low (continuous, priority-based) |
| Throughput ceiling | ~50K pages/sec | ~10K pages/sec (Hadoop-limited) | ~230K+ pages/sec |
| Operational cost | Medium (10 machines) | High (Hadoop cluster) | Massive (thousands of machines) |
| JavaScript rendering | Optional (headless subset) | Plugin-based (limited) | Full (Chromium-based WRS) |

---

## 8. Interview Tips

### 8.1 Numbers You Must Know Cold

```
1B pages / 1 week = ~1,650 pages/sec → round to 2,000-5,000 with overhead
1 page ≈ 100 KB → 1B pages = 100 TB raw → 20 TB compressed
5,000 pages/sec x 100 KB = 500 MB/sec = 4 Gbps
1 machine ≈ 500 pages/sec → 10 machines for interview scale
1B URLs x 8 bytes = 8 GB (fits in RAM for dedup)
Bloom filter: 10 bits/element at 1% FP rate
```

### 8.2 The Scaling Story Arc

When asked "how would you scale this?", walk through these levels:

```
Level 1: Single machine, async I/O
  - "With async I/O, one machine handles ~500 pages/sec"
  - "Good for prototyping and small-scale crawls"

Level 2: Horizontal scaling (10 machines)
  - "Shard by domain hash, each machine owns a set of domains"
  - "Centralized frontier in Redis, or partitioned frontier"
  - "This handles 1B pages/week"

Level 3: Geo-distribution (3 regions)
  - "Reduce latency to target servers"
  - "Partition by TLD or geo-IP"
  - "Central metadata store, regional crawl workers"

Level 4: Google-scale (thousands of machines)
  - "Custom distributed systems at every layer"
  - "Bigtable, MapReduce, Borg, custom networking"
  - "Years of engineering; not expected in an interview"
```

### 8.3 Common Follow-Up Questions

| Question | Key Point in Answer |
|---|---|
| "What's the bottleneck?" | Network I/O (latency), then DNS. Parsing is CPU-bound but secondary. |
| "How do you handle a hot domain?" | Politeness limits it anyway. Split by URL path prefix if needed for parallelism. |
| "What if you need fresher data?" | Priority queue: news = hours, blogs = days, static = weeks. Adaptive re-crawl scheduling. |
| "How do you know it's working?" | Pages/sec, error rate, frontier depth, duplicate rate. Anomaly detection on all four. |
| "What breaks first at 10x scale?" | Frontier becomes a distributed systems problem. Dedup needs sharding or Bloom filter federation. |
| "How much does it cost?" | 10 machines x ~$500/month (cloud) = $5K/month. Storage: 20 TB on S3 = ~$460/month. Total: ~$6K/month. |
