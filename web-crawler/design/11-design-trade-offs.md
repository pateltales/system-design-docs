# Design Philosophy and Trade-off Analysis

> Every design decision is a bet. This document explains what you are betting on,
> what you are giving up, and why one side of the bet wins in each context.

---

## 1. Breadth-First vs Depth-First Crawling

### The Core Question

When you pull a page and discover 50 outgoing links, do you crawl all 50 before
following any of *their* children (BFS), or do you immediately chase one link as
deep as it goes (DFS)?

The answer, in production, is **neither**.

### Option A: Breadth-First Search (BFS)

The frontier is a FIFO queue. You process every URL at depth *d* before touching
depth *d+1*.

```
Seed URLs (depth 0)
  ├── All links from seeds (depth 1)
  │     ├── All links from depth 1 (depth 2)
  │     │     └── ...
```

**Strengths:**
- Discovers the "shape" of the web early: how many domains, how they link.
- Important pages tend to live at shallow depths (homepages, category pages).
  PageRank correlates inversely with depth. Crawling shallow first captures
  high-value pages first.
- Natural parallelism: depth-1 URLs span many domains, so workers stay busy
  without colliding on politeness constraints.

**Weaknesses:**
- The frontier explodes. At depth 1 you might have 10K URLs. At depth 2,
  500K. At depth 3, 25M. Memory pressure is the defining constraint.
- No notion of priority. A spam page at depth 2 gets crawled before a critical
  news article at depth 3.

**Who uses it:** Early academic crawlers (the original Google prototype in the
1998 Brin/Page paper used a modified BFS).

### Option B: Depth-First Search (DFS)

The frontier is a LIFO stack. You follow one path as deep as it goes before
backtracking.

```
Seed URL → link A → link B → link C → ... → dead end → backtrack
```

**Strengths:**
- Low memory: the frontier is only as large as the current path depth.
- Good for exhaustive single-site crawling (e.g., archiving one domain).

**Weaknesses:**
- **Spider traps.** A calendar page with "next month" links is infinitely deep.
  DFS walks straight into it and never comes back.
- Ignores the rest of the web while drilling into one corner. Terrible for
  discovery.
- No prioritization. You are at the mercy of whatever links appear on the
  current page.

**Who uses it:** Almost nobody for general web crawling. Occasionally used for
targeted site-specific scrapers with a hard depth limit.

### Option C: Best-First (Priority-Based) --- The Production Answer

The frontier is a **priority queue**. Every URL gets a score. You always crawl
the highest-scored URL next, regardless of depth.

```
Priority score = f(PageRank estimate, freshness need, depth, domain importance, ...)
```

**Strengths:**
- Adapts to what matters. If a page at depth 5 has high estimated importance
  (many inlinks, from an authoritative domain), it jumps ahead of a junk page
  at depth 1.
- Depth is one signal, not the only signal. You get the BFS benefit (shallow
  pages score higher, all else equal) without being locked into strict
  level-order.
- Change-rate awareness: pages that change hourly get re-crawled before pages
  that change yearly.

**Weaknesses:**
- Priority computation adds complexity. You need heuristics or ML models to
  score URLs before you have even fetched them.
- Priority queue operations are O(log n) vs O(1) for a FIFO queue. At 10B URLs,
  this matters, so production systems use multi-level bucket queues instead of a
  single heap.

**Who uses it:** Every production search engine crawler. Googlebot, Bingbot,
Yandex.

### Comparison Table

| Dimension              | BFS (FIFO)          | DFS (LIFO)          | Best-First (Priority)     |
|------------------------|---------------------|---------------------|---------------------------|
| Frontier data structure| Queue               | Stack               | Priority queue / buckets  |
| Memory pressure        | Very high           | Low                 | High (but manageable)     |
| Discovery breadth      | Excellent           | Terrible            | Good (tunable)            |
| Handles spider traps   | Eventually (slow)   | No (gets stuck)     | Yes (low-priority)        |
| Prioritization         | None (depth only)   | None (recency only) | Full control              |
| Implementation effort  | Trivial             | Trivial             | Moderate to complex       |
| Used in production     | No (pure form)      | No                  | Yes, universally          |

### Recommendation

**Use best-first with depth as one priority signal.**

In an interview, start by saying "BFS" (shows you know important pages are
shallow), then immediately upgrade: "In practice, we use a priority queue so we
can incorporate PageRank estimates, freshness requirements, and domain importance
--- not just depth." This shows you understand the *why* behind the design, not
just the textbook algorithm.

---

## 2. Exact Dedup (Hash Set) vs Probabilistic Dedup (Bloom Filter)

### The Core Question

Before fetching a URL, you check: "Have I seen this before?" The question is
whether your answer needs to be *perfect* or *good enough*.

### Option A: Exact Deduplication (Hash Set)

Store a hash of every seen URL in a hash set. Lookup is O(1). No false
positives.

**The math:**
```
10 billion URLs
× 8 bytes per hash (64-bit fingerprint, e.g., xxHash64)
= 80 GB

With hash table overhead (~1.5x for open addressing):
= ~120 GB
```

120 GB does not fit in a single machine's RAM (typical server: 64-128 GB). You
need **sharding** --- partition the hash set across multiple machines, or use
disk-backed structures (RocksDB, LevelDB).

**Strengths:**
- Zero false positives. You never skip a URL you have not seen.
- Zero false negatives. You never re-crawl a URL you have seen.
- Simple mental model. "It is either in the set or it is not."

**Weaknesses:**
- Memory cost scales linearly with URL count. No compression.
- Requires sharding or disk I/O at web-scale, adding latency.
- Supports deletion (you can remove URLs), but this is rarely needed for a
  crawl frontier.

### Option B: Bloom Filter (Probabilistic)

A Bloom filter uses *k* hash functions mapping each element to *k* bit positions
in a bit array of size *m*. Membership checks may return false positives but
**never** false negatives.

**The math:**
```
For 1% false positive rate:
  Bits per element ≈ 9.6
  10 billion URLs × 9.6 bits = 96 billion bits = 12 GB

For 0.1% false positive rate:
  Bits per element ≈ 14.4
  10 billion URLs × 14.4 bits = 18 GB
```

12-18 GB fits comfortably in a single server's RAM. No sharding needed for the
filter itself.

**Strengths:**
- Dramatically smaller. 12 GB vs 120 GB for the same URL count.
- All operations are in-memory, no disk I/O.
- Constant time, cache-friendly bit operations.

**Weaknesses:**
- **False positives.** At 1% FPR, 1 in 100 *new* URLs gets incorrectly
  classified as "already seen" and skipped. For a general web crawler, this is
  acceptable --- the web has enormous redundancy, and missing 1% of pages is
  invisible.
- **Cannot delete.** Standard Bloom filters do not support removal. If you
  need deletion (e.g., re-crawling URLs whose content has changed), you need
  a Counting Bloom filter (which uses 4x more space) or a Cuckoo filter.
- **Cannot enumerate.** You cannot list "all URLs I have seen." The filter only
  answers yes/no queries.

### The Scale Breakpoints

| URL Count      | Hash Set Size | Bloom (1% FPR) | Recommendation          |
|----------------|---------------|-----------------|-------------------------|
| 1 million      | 8 MB          | 1.2 MB          | Either works. Use hash set for simplicity. |
| 100 million    | 800 MB        | 120 MB          | Either works. Hash set still fine.         |
| 1 billion      | 8 GB          | 1.2 GB          | Hash set fits one machine. Edge case.      |
| 10 billion     | 80 GB         | 12 GB           | **Bloom filter wins.** Hash set needs sharding. |
| 100 billion    | 800 GB        | 120 GB          | Bloom filter, possibly sharded.            |

### Comparison Table

| Dimension            | Hash Set (Exact)        | Bloom Filter (Probabilistic)  |
|----------------------|-------------------------|-------------------------------|
| False positives      | 0%                      | Tunable (typically 0.1-1%)    |
| False negatives      | 0%                      | 0% (guaranteed)               |
| Space per element    | 8+ bytes                | ~1.2 bytes (1% FPR)          |
| Deletion support     | Yes                     | No (standard), Yes (counting) |
| Enumeration          | Yes                     | No                            |
| Disk-friendly        | Yes (LSM trees)         | Awkward (bit-level random access) |
| Sweet spot           | < 1B elements           | > 1B elements                 |

### Real-World Systems

- **Googlebot:** Uses a combination. Exact dedup for the URL frontier (sharded
  across the crawl cluster), content-level dedup using SimHash for near-duplicate
  detection.
- **Apache Nutch:** Uses a Bloom filter for URL-seen checks during the
  generate/fetch cycle.
- **Common Crawl:** Exact URL dedup (they have the infrastructure for
  petabyte-scale storage).

### Recommendation

**Interview scale (explain at the whiteboard): Hash set.** It is simpler, the
interviewer will understand it immediately, and at "interview scale" (tens of
millions of URLs) it fits in memory.

**Production scale (when the interviewer pushes): Bloom filter.** Mention the
false positive rate, explain why 1% is acceptable ("the web is redundant; we
will find that content through other links"), and note it saves 10x memory.

The upgrade path in the interview: "We start with a hash set. When we hit memory
limits, we switch to a Bloom filter and accept a 1% miss rate."

---

## 3. Politeness: Fixed Delay vs Adaptive Delay

### The Core Question

How long do you wait between consecutive requests to the *same* domain? Too fast
and you get blocked (or worse, you take down a small site). Too slow and you
waste crawl budget.

### Option A: Fixed Delay

One request per domain per second. Period. No exceptions.

```
fetch(example.com/page1)
sleep(1000ms)
fetch(example.com/page2)
sleep(1000ms)
...
```

**Strengths:**
- Dead simple. One config parameter.
- Predictable load on target servers. Site owners can reason about worst-case.
- Safe. You will almost never overwhelm any server with 1 req/sec.

**Weaknesses:**
- **Wastes budget on fast servers.** A CDN-backed site like wikipedia.org can
  handle thousands of requests per second. Crawling at 1/sec means it takes
  days to crawl a site that could be done in minutes.
- **Still too fast for fragile servers.** A personal WordPress blog on shared
  hosting might struggle with 1 req/sec sustained.
- **Ignores robots.txt `Crawl-delay`.** Some sites specify their preferred
  delay (e.g., `Crawl-delay: 10`). A fixed 1-second policy violates this.

### Option B: Adaptive Delay

Measure server behavior. Adjust delay dynamically.

```python
base_delay = 1.0  # seconds

def compute_delay(domain_stats):
    # Signal 1: Response time
    # If server responds in 50ms, it's fast → decrease delay
    # If server responds in 5000ms, it's struggling → increase delay
    delay = max(base_delay, domain_stats.avg_response_time * 10)

    # Signal 2: HTTP status codes
    if domain_stats.recent_429_count > 0:  # Too Many Requests
        delay *= 4  # Back off hard
    if domain_stats.recent_503_count > 0:  # Service Unavailable
        delay *= 8  # Back off harder

    # Signal 3: robots.txt Crawl-delay
    if domain_stats.crawl_delay:
        delay = max(delay, domain_stats.crawl_delay)

    # Signal 4: Exponential backoff on consecutive errors
    if domain_stats.consecutive_errors > 0:
        delay *= 2 ** domain_stats.consecutive_errors

    return min(delay, 3600)  # Cap at 1 hour
```

**Strengths:**
- **Maximizes throughput on fast servers.** Wikipedia gets crawled at 10 req/sec.
  A personal blog gets crawled at 1 req/10 sec.
- **Responsive to server distress.** 429 and 503 responses trigger immediate
  backoff.
- **Respects robots.txt `Crawl-delay`.** Treated as a floor, not a suggestion.

**Weaknesses:**
- Complexity. You need per-domain state tracking (response times, error counts,
  Crawl-delay values).
- **Risk of being too aggressive.** If the adaptive algorithm ramps up too
  quickly, you can spike a server's load before detecting distress.
- Gaming: a server could return artificially fast responses to lure the crawler
  into high request rates, then throttle or block. (Rare in practice.)

### Comparison Table

| Dimension              | Fixed Delay              | Adaptive Delay               |
|------------------------|--------------------------|------------------------------|
| Implementation effort  | Trivial (one parameter)  | Moderate (per-domain state)  |
| Throughput efficiency  | Low (wastes fast servers)| High (matches server capacity)|
| Safety for target      | Usually safe (not always)| Safer (responds to distress) |
| Respects Crawl-delay   | Only if ≤ fixed value    | Yes (always uses as floor)   |
| Risk of overwhelming   | Low for fast, medium for slow | Low (backoff on errors)  |
| Per-domain state       | None                     | Response times, error counts |

### Real-World Systems

- **Googlebot:** Adaptive. Google has stated it measures server response time
  and adjusts crawl rate. Webmasters can also set rate limits via Google Search
  Console.
- **Bing:** Adaptive. Similar approach, monitors server health signals.
- **Scrapy (framework):** Ships with `AUTOTHROTTLE` extension --- adaptive
  delay based on response latency. Default: target 1.0x response time as delay.
- **Apache Nutch:** Fixed delay by default (`fetcher.server.delay = 5`), with
  optional adaptive mode.

### Recommendation

**Start with fixed delay (1 request per second per domain).** Always respect
`Crawl-delay` from robots.txt as a minimum. This is the safe, simple starting
point.

**Add adaptive delay as an optimization.** The upgrade is: "We measure average
response time per domain. If the server is fast (< 200ms), we reduce delay to
500ms. If it returns 429 or 503, we exponentially back off. This doubles our
effective throughput without increasing risk."

In an interview, mentioning both and explaining the upgrade path scores higher
than jumping straight to adaptive (which might suggest you do not appreciate the
value of simplicity).

---

## 4. Centralized vs Distributed Frontier

### The Core Question

The URL frontier is the heart of the crawler --- it decides *what to crawl next*.
Should it live on one machine or be spread across many?

### Option A: Centralized Frontier

One machine (or one process) holds the entire frontier in memory or on local
disk. All crawler workers request URLs from this single source.

```
                    ┌──────────────┐
                    │   Frontier   │
                    │  (1 machine) │
                    └──────┬───────┘
                           │
              ┌────────────┼────────────┐
              │            │            │
         ┌────▼───┐  ┌────▼───┐  ┌────▼───┐
         │Worker 1│  │Worker 2│  │Worker 3│
         └────────┘  └────────┘  └────────┘
```

**Strengths:**
- **Simple coordination.** No distributed consensus, no partitioning logic, no
  cross-node dedup.
- **Global priority ordering.** The single frontier has a complete view, so
  priority decisions are globally optimal.
- **Politeness enforcement is trivial.** One place tracks per-domain last-access
  time.

**Weaknesses:**
- **Single point of failure.** Frontier machine dies, crawl stops.
- **Memory ceiling.** A single machine holds ~128-512 GB RAM. At 8 bytes per
  URL hash + 100 bytes per URL metadata, that is ~500M-2B URLs. Beyond that,
  you must spill to disk (slower) or shard.
- **Throughput bottleneck.** If 1,000 workers each request 10 URLs/sec, the
  frontier handles 10,000 dequeue operations/sec. Doable, but tight at scale.

### Option B: Distributed Frontier

The frontier is partitioned across *N* machines, typically by hashing the domain
name. Each partition owns all URLs for its assigned domains.

```
     ┌────────────┐  ┌────────────┐  ┌────────────┐
     │ Frontier 1 │  │ Frontier 2 │  │ Frontier 3 │
     │ (a-h.com)  │  │ (i-p.com)  │  │ (q-z.com)  │
     └─────┬──────┘  └─────┬──────┘  └─────┬──────┘
           │               │               │
      ┌────▼───┐      ┌────▼───┐      ┌────▼───┐
      │Workers │      │Workers │      │Workers │
      └────────┘      └────────┘      └────────┘
```

**Strengths:**
- **Horizontal scaling.** Add more frontier nodes to handle more URLs. No
  single-machine memory limit.
- **Fault isolation.** If frontier node 2 dies, domains i-p are affected, but
  a-h and q-z continue crawling.
- **Politeness is partition-local.** Since all URLs for `example.com` live on
  one partition, that partition enforces politeness without cross-node
  coordination.

**Weaknesses:**
- **No global priority view.** Each partition makes locally optimal priority
  decisions. The globally most important URL might sit in a partition that is
  not being drained fast enough.
- **Rebalancing pain.** If one partition gets "hot" (e.g., it owns
  `.com` domains while another owns `.museum` domains), load is uneven.
  Consistent hashing helps but does not eliminate skew.
- **Distributed dedup.** When worker 1 discovers a URL for a domain owned by
  partition 3, it must send that URL across the network. Dedup happens at the
  receiving partition.
- **Complexity tax.** Network partitions, message queues, replication for fault
  tolerance --- all the distributed systems problems arrive.

### The Scale Breakpoints

| Crawl Scale              | Pages to Crawl | Frontier Recommendation |
|--------------------------|----------------|-------------------------|
| Focused crawl (one site) | < 1M           | Centralized, in-memory  |
| Vertical crawl (one niche)| 1M-100M       | Centralized, disk-backed|
| Broad web crawl          | 100M-1B        | Centralized *can* work (barely) |
| Full web-scale           | > 1B           | Distributed, necessary  |

### Comparison Table

| Dimension              | Centralized              | Distributed                   |
|------------------------|--------------------------|-------------------------------|
| Max frontier size      | ~1-2B URLs (disk-backed) | Unlimited (add nodes)         |
| Priority optimality    | Global (perfect)         | Local (per-partition)         |
| Politeness enforcement | Trivial (one place)      | Trivial (partition-local)     |
| Fault tolerance        | SPOF                     | Partition-level isolation      |
| Coordination overhead  | None                     | Network, rebalancing, routing |
| Implementation effort  | Low                      | High                          |
| Operational effort     | Low                      | High                          |

### Real-World Systems

- **Mercator (original paper):** Centralized frontier with front/back queue
  architecture. Front queues for priority, back queues for politeness. This is
  the design most commonly referenced in system design interviews.
- **Googlebot:** Distributed. The crawl cluster is thousands of machines, each
  responsible for a partition of the URL space.
- **Apache Nutch:** Centralized (Hadoop-based). The frontier is a segment of
  the CrawlDB, read in batch during the "generate" phase.
- **Scrapy:** Centralized by default (in-memory). Can use `scrapy-redis` for
  distributed frontier.

### Recommendation

**Interview approach:** Start centralized (Mercator front/back queue design).
When the interviewer asks "how does this scale?", then distribute by
domain-hash partitioning. This progression shows you understand the trade-offs
and do not prematurely optimize.

**Production rule of thumb:** If your frontier fits in one machine's memory
(< ~100M URLs with full metadata, or ~1B with just hashes), stay centralized.
The operational simplicity is worth more than the theoretical scalability of
distribution.

---

## 5. Store Everything vs Store Selectively

### The Core Question

When you download a web page, do you keep the raw HTML forever, or do you run it
through quality filters and discard the junk?

### Option A: Store Everything

Every fetched page gets written to archival storage --- raw HTML, HTTP headers,
timestamp, everything. Filtering and processing happen downstream as separate
jobs.

```
Fetch → Store raw HTML to archive → Later: parse, filter, index
```

**Strengths:**
- **Reprocessing freedom.** Six months from now, you realize your parser had a
  bug that missed `<article>` tags. With raw HTML stored, you reprocess. Without
  it, you re-crawl (expensive, and the page may have changed or disappeared).
- **Regulatory/research value.** The Wayback Machine and Common Crawl exist
  precisely because someone stored everything.
- **Filter-bug safety net.** If your quality filter is too aggressive and drops
  good pages, you lose nothing --- the raw data is still there.

**Weaknesses:**
- **Storage cost.** Average web page: ~100 KB compressed. 10 billion pages ×
  100 KB = 1 PB. At S3 standard pricing (~$23/TB/month), that is $23,000/month.
  At S3 Glacier (~$4/TB/month), it is $4,000/month.
- **Storage infrastructure.** Petabyte-scale systems need careful partitioning,
  replication, and lifecycle management.

### Option B: Store Selectively

Apply quality filters at crawl time. Only persist pages that pass.

```
Fetch → Quality check → Pass? Store : Discard
```

Quality filters might check:
- Content length > minimum threshold (skip thin pages)
- Language detection (skip pages not in target languages)
- Spam/duplicate detection (skip near-duplicates via SimHash)
- Domain quality score (skip known spam domains)

**Strengths:**
- **Dramatically less storage.** If 70% of pages are junk, you store 3 billion
  instead of 10 billion. ~300 TB instead of 1 PB.
- **Faster downstream processing.** Indexers, ML pipelines, etc. process only
  quality data.

**Weaknesses:**
- **Irreversible data loss.** If your filter has a bug (and it will), you
  lose pages forever. Re-crawling is slow, expensive, and the page may no
  longer exist.
- **Filter maintenance burden.** Quality heuristics need constant tuning.
  What counts as "junk" changes over time.
- **Cold-start problem.** You cannot filter without historical data, but you
  cannot build historical data without storing things.

### The Cost Reality Check

```
Storage medium     | Cost/TB/month | 1 PB/month  | 10 PB/month
S3 Standard        | $23           | $23,000     | $230,000
S3 Infrequent      | $12.50        | $12,500     | $125,000
S3 Glacier         | $4            | $4,000      | $40,000
S3 Glacier Deep    | $1            | $1,000      | $10,000
HDFS (on-prem)     | ~$3-5         | $3,000-5,000| $30,000-50,000
```

At Glacier Deep Archive rates, storing 1 PB of raw HTML costs $1,000/month.
**Storage is cheap. Re-crawling is expensive.** A single re-crawl of 10 billion
pages costs more in compute, bandwidth, and time than years of cold storage.

### Comparison Table

| Dimension              | Store Everything         | Store Selectively          |
|------------------------|--------------------------|----------------------------|
| Storage cost           | High (but cheap if cold)  | Lower (30-50% less)       |
| Reprocessing ability   | Full                     | None (lost data is gone)    |
| Filter-bug risk        | Zero (data preserved)     | High (silent data loss)    |
| Downstream efficiency  | Lower (process more data) | Higher (pre-filtered)      |
| Regulatory compliance  | Easier (audit trail)      | Harder (prove what was seen)|
| Operational complexity | Storage management        | Filter maintenance          |

### Real-World Systems

- **Common Crawl:** Stores everything. Petabytes of raw WARC files on S3. The
  entire research community benefits from reprocessing.
- **Internet Archive (Wayback Machine):** Stores everything. Their mission *is*
  preservation.
- **Google:** Stores everything (for the pages it chooses to crawl). The raw
  page cache enables reprocessing when algorithms change.
- **Enterprise search crawlers:** Often store selectively --- they know their
  corpus (internal wiki, docs site) and can filter confidently.

### Recommendation

**Store everything in cold storage. Filter downstream.**

The argument is economic: storage costs decrease ~20% per year (Kryder's Law).
Compute and bandwidth for re-crawling do not decrease at the same rate. Raw data
is an *appreciating asset* --- every new algorithm, model, or use case can be
applied to historical data, but only if you kept it.

In an interview: "We store raw HTML in S3 Glacier. A separate processing
pipeline reads from the archive, applies quality filters, and feeds the index.
If our filters improve, we reprocess the archive without re-crawling."

---

## 6. Real-Time vs Batch Crawling

### The Core Question

Does the crawler run continuously (always fetching, always discovering), or does
it operate in discrete cycles (generate a URL list, fetch everything, process
results, repeat)?

### Option A: Real-Time / Streaming Crawl

The crawler is an always-running system. URLs are fetched as they are
discovered. The frontier is a live priority queue, continuously drained and
replenished.

```
┌─────────┐     ┌──────────┐     ┌────────┐     ┌───────┐
│Discover │────▶│ Frontier │────▶│ Fetch  │────▶│ Parse │──┐
│  URLs   │     │ (live PQ)│     │        │     │       │  │
└─────────┘     └──────────┘     └────────┘     └───────┘  │
      ▲                                                      │
      └──────────────── new URLs discovered ─────────────────┘
```

**Strengths:**
- **Low-latency freshness.** A breaking news article is discovered, fetched,
  and indexed within minutes. Critical for search engines.
- **Continuous resource utilization.** Workers are always busy. No idle time
  between cycles.
- **Responsive to change.** If a page signals it has changed (via sitemap ping
  or PubSubHubbub/WebSub), the crawler can react immediately.

**Weaknesses:**
- **Operational complexity.** An always-running distributed system needs
  monitoring, alerting, graceful degradation, and hot-deploy capability.
  You cannot "stop the world" to fix a bug.
- **State management.** The frontier, dedup set, and per-domain politeness
  state are live, mutable, distributed data structures. Consistency is hard.
- **Resource planning.** You need enough capacity to handle peak discovery rates,
  not just average rates.

### Option B: Batch Crawl

The crawl happens in discrete cycles. Each cycle has phases: generate URLs to
fetch, fetch them, parse results, update the URL database. The system is idle
between cycles (or the next cycle starts immediately, but phases do not
overlap).

```
Cycle N:
  [Generate] → [Fetch] → [Parse] → [Update DB]

Cycle N+1:
  [Generate] → [Fetch] → [Parse] → [Update DB]
```

**Strengths:**
- **Simpler to reason about.** Each phase has clear inputs and outputs. You can
  inspect intermediate state between phases.
- **Leverages batch infrastructure.** MapReduce, Spark, or batch job schedulers.
  Well-understood operational model.
- **Checkpoint and restart.** If the fetch phase fails halfway, restart from the
  last checkpoint. Batch systems have mature failure recovery.
- **Resource sharing.** The cluster runs crawl jobs during off-peak hours and
  other workloads otherwise.

**Weaknesses:**
- **High latency.** If a cycle takes 6 hours, a newly discovered URL waits up
  to 6 hours before being fetched. Breaking news is stale by the time it is
  indexed.
- **Bursty load on targets.** During the fetch phase, the crawler hits servers
  hard. Between cycles, it is silent. Target servers see spiky traffic.
- **Wasted freshness.** Pages that change hourly are treated the same as pages
  that change yearly. No differentiation within a cycle.

### Historical Context: Nutch and the Birth of Hadoop

Apache Nutch is the canonical batch crawler. It was one of the original
motivations for creating Hadoop:

1. **2002:** Doug Cutting starts Nutch, an open-source web crawler and search
   engine.
2. **2003:** Google publishes the GFS paper. Cutting realizes Nutch needs
   distributed storage.
3. **2004:** Google publishes the MapReduce paper. Cutting implements it for
   Nutch's crawl cycle (generate/fetch/parse/update are MapReduce jobs).
4. **2006:** The storage and compute layers are extracted from Nutch into a
   separate project: **Hadoop**.

Nutch's crawl cycle is literally:
```
bin/nutch generate   # MapReduce: select URLs from CrawlDB
bin/nutch fetch      # MapReduce: HTTP fetch
bin/nutch parse      # MapReduce: extract links and text
bin/nutch updatedb   # MapReduce: merge results back into CrawlDB
```

### Comparison Table

| Dimension              | Real-Time (Streaming)    | Batch (Cyclic)              |
|------------------------|--------------------------|------------------------------|
| Freshness latency      | Minutes                  | Hours to days                |
| Operational complexity | High (always running)    | Lower (discrete jobs)        |
| Failure recovery       | Complex (live state)     | Simple (checkpoint/restart)  |
| Resource utilization   | Continuous, smooth       | Bursty                       |
| Target server load     | Smooth                   | Spiky (fetch phase)          |
| Infrastructure         | Stream processing, queues| Batch frameworks (MapReduce) |
| Change-rate adaptation | Natural (priority-based) | Must be engineered into generation |

### Real-World Systems

- **Googlebot:** Real-time. Continuous crawling with priority-based scheduling.
  Can index a page within minutes of it appearing.
- **Bingbot:** Real-time. Similar to Google's approach.
- **Apache Nutch:** Batch. MapReduce-based cycles as described above.
- **Common Crawl:** Batch. Monthly crawl cycles producing WARC archives.
- **StormCrawler:** Real-time. Built on Apache Storm for streaming crawl.

### Recommendation

**The choice depends on your use case, not on what is "better":**

- **Building a search engine?** Real-time. Freshness is a ranking signal. Users
  expect to find content published an hour ago.
- **Building a web archive?** Batch. You want completeness, not speed.
  Monthly cycles are fine.
- **Building a data pipeline (e.g., ML training data)?** Batch. You process
  data in bulk anyway.

**In an interview:** "For a search engine, we use real-time crawling with a live
priority queue. I am aware this is more complex than batch. Batch (like Nutch's
MapReduce approach) is simpler but adds hours of latency. For search, that
latency is unacceptable."

---

## 7. JavaScript Rendering vs HTML-Only

### The Core Question

Modern web pages load content via JavaScript. If you only fetch the raw HTML,
you might get an empty `<div id="root"></div>` and miss all the actual content.
But rendering JavaScript is expensive. Is it worth it?

### The Scope of the Problem

```
Estimated percentage of pages with critical content behind JavaScript:
  2015: ~5%
  2018: ~10%
  2023: ~15-20%
  Trend: Increasing (React, Vue, Angular adoption)

Pages with "some" JS-loaded content (ads, recommendations, lazy images):
  ~50-60%

Pages where raw HTML is sufficient:
  ~80-85% (static sites, server-rendered, WordPress, news sites)
```

### Option A: HTML-Only Crawling

Fetch the raw HTML with an HTTP client (libcurl, httpx, etc.). Parse the
returned HTML. Do not execute JavaScript.

**Strengths:**
- **Fast.** A single HTTP request + HTML parse takes ~100-500ms. Can process
  thousands of pages per second per worker.
- **Low resources.** CPU and memory usage are minimal. No browser overhead.
- **Simple.** No browser binaries, no Chromium updates, no rendering pipeline.

**Weaknesses:**
- **Misses JS-rendered content.** Single-page applications (SPAs) built with
  React/Vue/Angular may return empty shells. You get the `<script>` tags but
  not the rendered DOM.
- **Misses lazy-loaded content.** Images, infinite scroll content, and
  below-the-fold elements loaded via Intersection Observer are invisible.
- **Increasingly incomplete.** As the web moves toward client-side rendering,
  the gap between HTML-only and rendered content grows.

### Option B: JavaScript Rendering (Headless Browser)

Launch a headless browser (Chromium via Puppeteer/Playwright, or Chrome Headless
directly). Navigate to the URL. Wait for JavaScript to execute. Extract the
rendered DOM.

**Strengths:**
- **Sees what the user sees.** The rendered DOM includes all JS-loaded content,
  lazy-loaded images, and dynamically generated markup.
- **Executes AJAX calls.** If the page fetches data from an API and renders it,
  the headless browser captures the result.
- **Handles modern web frameworks.** React, Vue, Angular, Next.js (CSR mode),
  Svelte --- all work correctly.

**Weaknesses:**
- **Slow.** Rendering a page takes 2-10 seconds (page load + JS execution +
  wait for network-idle). That is 10-100x slower than HTML-only.
- **Resource-heavy.** Each headless browser instance consumes 100-500 MB of RAM.
  Running 100 concurrent instances requires 10-50 GB of RAM.
- **Fragile.** Browser crashes, memory leaks, GPU issues (even headless
  Chromium uses GPU acceleration), Chromium version mismatches.
- **Security surface.** Executing arbitrary JavaScript from untrusted pages
  is a security risk. Sandboxing is critical.

### Resource Comparison

```
Method          | Pages/sec/worker | RAM per worker | CPU per worker
HTML-only       | 50-200           | 50-100 MB      | 0.1-0.5 cores
Headless Chrome | 0.5-5            | 200-500 MB     | 1-2 cores
```

For equivalent throughput (1,000 pages/sec):
```
HTML-only:       5-20 workers,  1-2 GB RAM,   1-10 cores
Headless Chrome: 200-2000 workers, 40-1000 GB RAM, 200-4000 cores
```

### Google's Approach: Two-Phase Rendering (WRS)

Google's Web Rendering Service (WRS) uses a **two-wave** approach:

1. **Wave 1 (immediate):** Fetch raw HTML. Extract text, links, metadata.
   Index what you can.
2. **Wave 2 (deferred):** Queue the page for rendering in an evergreen
   Chromium instance. Execute JavaScript. Extract the rendered DOM. Update
   the index with any new content found.

Key details:
- WRS uses the latest stable Chromium (hence "evergreen"). It supports modern
  JS features.
- Static resources (CSS, JS, images) are cached for up to 30 days to speed up
  rendering.
- Wave 2 can be delayed by hours or days, depending on crawl priority and
  rendering queue depth.
- Google has publicly stated that WRS processes "hundreds of millions of pages."

**Why two phases?** Most pages have *some* content in raw HTML (title, headings,
text). Wave 1 captures this immediately. Wave 2 fills in the gaps. This way,
a page is partially indexed quickly and fully indexed eventually.

### Comparison Table

| Dimension              | HTML-Only                | Headless Browser           |
|------------------------|--------------------------|----------------------------|
| Content completeness   | 80-85% of pages fine     | ~100% of pages             |
| Speed per page         | 5-20ms fetch + parse     | 2,000-10,000ms render      |
| Resources per worker   | 50-100 MB RAM            | 200-500 MB RAM             |
| Throughput per machine | 1,000-5,000 pages/sec    | 10-100 pages/sec           |
| Implementation effort  | Low (HTTP client + parser)| High (browser management)  |
| Maintenance burden     | Low                      | High (Chromium updates, crashes) |
| Security risk          | Low (no code execution)  | High (arbitrary JS)        |

### Real-World Systems

- **Googlebot + WRS:** Two-phase (HTML first, rendered second). Production at
  massive scale.
- **Bingbot:** Similar two-phase approach. Selective rendering for pages
  detected as JS-heavy.
- **Common Crawl:** HTML-only. They prioritize breadth over rendering fidelity.
- **Prerender.io / Rendertron:** Rendering-as-a-service for crawlers. Run
  headless Chrome, cache rendered pages, serve to bots.
- **Scrapy + Splash:** Splash is a lightweight JS rendering service designed
  for Scrapy integration.

### Recommendation

**For an interview:** Start with HTML-only. It handles the vast majority of
pages and is dramatically simpler and faster. Then mention: "For JavaScript-heavy
pages, we add a rendering service --- a pool of headless Chromium instances that
processes a queue of URLs flagged as needing JS execution. This is Google's
two-phase approach."

**For production:**
- If your target corpus is mostly static (news sites, blogs, government sites,
  Wikipedia): HTML-only is sufficient.
- If your target includes SPAs, e-commerce (product pages often JS-rendered),
  or social media: you need rendering, at least selectively.
- The two-phase approach is the best of both worlds: fast HTML-first indexing
  with deferred rendering for completeness.

---

## 8. DNS Resolution: OS Resolver vs Custom Resolver

### The Core Question

Every URL fetch starts with a DNS lookup: converting `example.com` to
`93.184.216.34`. At low crawl rates, the OS handles this invisibly. At high
crawl rates, DNS becomes a bottleneck you must explicitly manage.

### Option A: OS Resolver

Use the system's default DNS resolution. In code, this means calling
`getaddrinfo()` or equivalent, which reads `/etc/resolv.conf` and queries the
configured upstream DNS servers (usually ISP DNS or a local resolver like
`systemd-resolved`).

**Strengths:**
- **Zero implementation effort.** Every HTTP library uses it by default.
- **Local caching.** Most OS resolvers cache responses for the TTL duration.
  `nscd`, `systemd-resolved`, or the stub resolver handle this.
- **Correct behavior.** The OS handles `/etc/hosts` overrides, search domains,
  and other system-level DNS configuration.

**Weaknesses:**
- **Synchronous and blocking.** `getaddrinfo()` is a blocking call. On Linux,
  glibc allocates a thread per lookup from a small pool. The default pool size
  is small (varies by distro, often 4-20 threads). At high concurrency, lookups
  queue up.
- **Low concurrency ceiling.** In practice, the OS resolver handles ~100-500
  concurrent lookups before introducing significant latency.
- **No control over upstream servers.** You are at the mercy of
  `/etc/resolv.conf`. If the upstream server is slow or unreliable, you cannot
  easily fail over.
- **TTL ignorance.** Some OS caches do not respect TTL correctly, leading to
  stale records or cache misses.

### Option B: Custom Async Resolver

Use a dedicated DNS library that performs asynchronous, non-blocking resolution.
Popular choices:
- **c-ares** (used by libcurl, Node.js)
- **trust-dns / hickory** (Rust)
- **dnsjava** (Java)
- **aiodns** (Python, wraps c-ares)

Or run a local recursive resolver:
- **Unbound** --- lightweight, caching, DNSSEC-validating
- **CoreDNS** --- pluggable, Kubernetes-native

**Strengths:**
- **High concurrency.** Thousands of simultaneous lookups without blocking.
  Each lookup is an async socket operation, not a thread.
- **Configurable caching.** Control TTL behavior, negative caching, prefetching.
  Warm the cache proactively for domains in the frontier.
- **Target specific DNS servers.** Use Google Public DNS (`8.8.8.8`), Cloudflare
  (`1.1.1.1`), or authoritative servers directly. Fail over automatically.
- **Metrics and observability.** Track lookup latency, cache hit rate, failure
  rate per upstream server.

**Weaknesses:**
- **Implementation effort.** Must integrate an async DNS library or operate a
  local resolver.
- **Bypasses OS configuration.** `/etc/hosts` entries and search domains are
  not automatically respected (must be handled separately).
- **Operational overhead.** A local Unbound instance needs monitoring,
  configuration, and updates.

### The Throughput Ceiling

```
Crawl rate        | DNS lookups/sec* | OS Resolver      | Custom Resolver
10 pages/sec      | ~5               | Fine             | Overkill
100 pages/sec     | ~50              | Fine             | Overkill
1,000 pages/sec   | ~500             | Struggling       | Comfortable
10,000 pages/sec  | ~5,000           | Breaking         | Comfortable
100,000 pages/sec | ~50,000          | Impossible       | Needs optimization

* Assuming ~50% cache hit rate (half the lookups are for new domains)
```

### Comparison Table

| Dimension              | OS Resolver              | Custom Async Resolver       |
|------------------------|--------------------------|-----------------------------|
| Implementation effort  | Zero                     | Moderate                    |
| Concurrency ceiling    | ~100-500 lookups         | Thousands of lookups        |
| Cache control          | Limited                  | Full (TTL, prefetch, size)  |
| Upstream selection     | /etc/resolv.conf only    | Any server, with failover   |
| Observability          | Minimal                  | Full metrics                |
| Correctness            | Handles /etc/hosts       | Must handle separately      |
| Sweet spot             | < 100 pages/sec          | > 1,000 pages/sec           |

### Real-World Systems

- **Googlebot:** Custom DNS infrastructure. Google operates its own recursive
  resolvers and has direct peering with many authoritative DNS providers.
- **Apache Nutch:** OS resolver (runs on Hadoop, JVM handles DNS through
  `InetAddress.getByName()` which uses the OS resolver).
- **Scrapy:** OS resolver by default. Ships with optional `dnspython`-based
  caching resolver via `CachingThreadedResolver`.
- **curl/libcurl:** Supports c-ares as an optional async DNS backend. When
  built with c-ares, concurrent DNS lookups scale much better.

### Recommendation

**Below 100 pages/sec:** Do not bother with custom DNS. The OS resolver is fine.
Spend your engineering time elsewhere.

**100-1,000 pages/sec:** Add a local caching resolver (Unbound). This is a
one-time setup that dramatically improves DNS performance. The crawler still
uses the OS resolver, but it talks to a fast local cache instead of a remote
upstream.

**Above 1,000 pages/sec:** Use an async DNS library (c-ares, aiodns). Integrate
it into your event loop. This eliminates the blocking-thread bottleneck entirely.

**In an interview:** Mention DNS as a bottleneck: "At high crawl rates, DNS
becomes a bottleneck because the OS resolver is synchronous. We run a local
caching resolver and use an async DNS library to handle thousands of concurrent
lookups." This shows you have thought about the *system*, not just the
*algorithm*.

---

## 9. Single-Machine vs Distributed Architecture

### The Core Question

This is the meta-trade-off that encompasses all the others. Should the crawler
run on one machine or many? The answer determines your complexity budget for
everything else.

### Option A: Single Machine

One server, typically with async I/O (epoll/kqueue), runs the entire crawl:
frontier, fetcher, parser, dedup, and storage writer.

```
┌────────────────────────────────────────────────┐
│                 Single Machine                 │
│                                                │
│  ┌──────────┐  ┌─────────┐  ┌──────────────┐  │
│  │ Frontier │─▶│ Fetcher │─▶│ Parser/Store │  │
│  │ (memory) │  │ (async) │  │              │  │
│  └──────────┘  └─────────┘  └──────────────┘  │
│                                                │
│  ┌──────────┐  ┌──────────┐                    │
│  │  Dedup   │  │ DNS Cache│                    │
│  │(hash set)│  │          │                    │
│  └──────────┘  └──────────┘                    │
└────────────────────────────────────────────────┘
```

**What a single machine can do:**

A modern server (32 cores, 128 GB RAM, 10 Gbps NIC) with an async HTTP client
can achieve:
```
Network-bound limit:
  10 Gbps ÷ 100 KB avg page = ~12,500 pages/sec

CPU-bound limit (parsing):
  Depends on parser, but 5,000-10,000 pages/sec is typical

DNS-bound limit (with caching):
  ~2,000-5,000 new domain lookups/sec

Practical combined throughput:
  1,000-5,000 pages/sec

At 5,000 pages/sec:
  432 million pages/day
  ~13 billion pages/month
```

13 billion pages per month from one machine. The *entire indexed web* is
estimated at 50-100 billion pages. A single well-optimized machine could
theoretically crawl 10-25% of the indexed web in a month.

**Strengths:**
- **No distributed systems complexity.** No network partitions, no consensus,
  no distributed dedup, no message queues, no coordination protocols.
- **Easy to debug.** One log stream, one process, one machine.
  `strace`, `perf`, `htop` all work.
- **Easy to operate.** Deploy a binary. Start it. Monitor with standard tools.
- **Consistent state.** The frontier, dedup set, and politeness tracker are all
  in-process. No eventual consistency surprises.

**Weaknesses:**
- **Single point of failure.** Machine dies, crawl stops. Need checkpointing
  to resume.
- **Vertical scaling ceiling.** A single machine maxes out at ~128-512 GB RAM
  and ~10-25 Gbps network. Beyond that, you *must* distribute.
- **No geographic distribution.** Crawling from one data center means high
  latency to distant servers. A server in Asia adds 200-300ms RTT when crawled
  from US-East.

### Option B: Distributed Architecture

Multiple machines, each running a crawler worker, coordinated by shared
infrastructure (distributed frontier, message queues, shared dedup).

```
┌──────────┐ ┌──────────┐ ┌──────────┐
│ Worker 1 │ │ Worker 2 │ │ Worker N │
│ (fetch/  │ │ (fetch/  │ │ (fetch/  │
│  parse)  │ │  parse)  │ │  parse)  │
└────┬─────┘ └────┬─────┘ └────┬─────┘
     │            │            │
     ▼            ▼            ▼
┌─────────────────────────────────────┐
│    Distributed Frontier (Kafka /    │
│    Redis / custom partitioned PQ)   │
└─────────────────────────────────────┘
     │            │            │
     ▼            ▼            ▼
┌─────────────────────────────────────┐
│    Distributed Dedup (Bloom filter  │
│    per partition / shared Redis)    │
└─────────────────────────────────────┘
     │
     ▼
┌─────────────────────────────────────┐
│    Object Storage (S3 / HDFS)       │
└─────────────────────────────────────┘
```

**Strengths:**
- **Horizontal scaling.** Need more throughput? Add workers. Need more frontier
  capacity? Add frontier nodes.
- **Geographic distribution.** Workers in US-East crawl US sites. Workers in
  EU-West crawl European sites. Lower latency, better politeness.
- **Fault tolerance.** Worker 3 dies, its URL partition is reassigned to
  Worker 4. The crawl continues.
- **Unlimited scale.** Google, Bing, and other search engines crawl billions of
  pages per day using thousands of machines.

**Weaknesses:**
- **Distributed systems complexity.** Every data structure (frontier, dedup set,
  politeness tracker, DNS cache) becomes a distributed systems problem.
- **Coordination overhead.** URL routing (which worker owns which domain?),
  work stealing (idle worker takes from busy worker), load balancing.
- **Operational burden.** Monitoring N machines, deploying to N machines,
  debugging across N machines. Distributed tracing, log aggregation, alerting.
- **Cost.** Not just machine cost, but the engineering time to build, test, and
  operate the distributed system.

### The Critical Question: When Do You NEED to Distribute?

```
Crawl goal              | Pages/day   | Architecture   | Why
Personal project        | < 100K      | Single machine | Trivially fits
Company's own sites     | 100K-10M    | Single machine | Still fits
Vertical search engine  | 10M-100M    | Single machine | Pushing limits, still doable
Broad search engine     | 100M-1B     | Distributed    | Single machine ceiling
Google/Bing scale       | 1B-100B     | Distributed    | Thousands of machines
```

**The honest truth:** Most crawl jobs do not need distribution. A single
async-I/O machine handles millions of pages per day. Distribution is needed
when:

1. **Throughput exceeds single-machine network capacity** (~5,000-10,000
   pages/sec).
2. **Frontier size exceeds single-machine memory** (>1-2 billion URLs).
3. **Geographic locality matters** (crawling global web from one location adds
   latency).
4. **Fault tolerance is required** (the crawl cannot stop for even minutes).

### Comparison Table

| Dimension              | Single Machine           | Distributed                  |
|------------------------|--------------------------|------------------------------|
| Max throughput         | ~5,000 pages/sec         | Unlimited (add machines)     |
| Frontier capacity      | ~1-2B URLs               | Unlimited (add nodes)        |
| Fault tolerance        | SPOF (checkpoint to recover)| Worker-level isolation      |
| Geographic locality    | One location             | Multi-region possible        |
| Debugging              | Simple (one process)     | Complex (distributed tracing)|
| Deployment             | One binary, one machine  | Orchestration (K8s, etc.)    |
| Engineering cost       | Low                      | Very high                    |
| Operational cost       | Low                      | High                         |

### Real-World Systems

- **Single-machine crawlers that work at impressive scale:**
  - **Heritrix** (Internet Archive's crawler): Primarily single-machine with
    very high async throughput. Heritrix 3 can crawl millions of pages per day
    from one machine.
  - **Colly** (Go): Single-machine framework. Fast enough for most use cases.
  - **Scrapy** (Python): Single-machine by default. Handles hundreds of pages
    per second.

- **Distributed crawlers:**
  - **Googlebot:** Thousands of machines worldwide.
  - **Apache Nutch:** Distributed via Hadoop. MapReduce-based.
  - **StormCrawler:** Distributed via Apache Storm.
  - **Norconex Collector:** Can run single or distributed.

### Recommendation for Interviews

**The interview progression:**

1. **Attempt 0-1 (first 15 min):** Design a single-machine crawler. Focus on
   the core components: frontier, fetcher, parser, dedup, politeness. Show you
   understand the *problem* before the *scale*.

2. **Attempt 2 (next 10 min):** Identify bottlenecks. "At 5,000 pages/sec, we
   hit the network limit. The frontier has 2B URLs, pushing memory limits."

3. **Attempt 3 (final 10 min):** Distribute. Partition the frontier by domain
   hash. Add Kafka for URL routing. Shard the dedup Bloom filter. Deploy workers
   across regions.

This progression shows the interviewer three critical things:
- You can build something simple that works.
- You understand where it breaks.
- You know how to evolve it when it breaks.

Starting with "we need 10,000 Kubernetes pods and a Kafka cluster" is a red
flag. It signals you are reciting architecture without understanding constraints.

---

## Summary: Decision Matrix

| Trade-off                    | Start With           | Upgrade To             | Upgrade When                  |
|------------------------------|----------------------|------------------------|-------------------------------|
| Crawl order                  | BFS                  | Best-first (priority)  | Need prioritization           |
| URL dedup                    | Hash set             | Bloom filter           | > 1B URLs                     |
| Politeness                   | Fixed 1 req/sec      | Adaptive delay         | Need throughput optimization  |
| Frontier                     | Centralized          | Distributed            | > 1B URLs or need fault tolerance |
| Storage                      | Store everything     | (Do not downgrade)     | (Storage is cheap)            |
| Crawl mode                   | Batch                | Real-time              | Need freshness < 1 hour       |
| JS rendering                 | HTML-only            | Two-phase              | JS-heavy targets detected     |
| DNS                          | OS resolver          | Custom async           | > 1,000 pages/sec             |
| Architecture                 | Single machine       | Distributed            | > 5,000 pages/sec             |

The pattern is clear: **start simple, measure, upgrade when you hit a specific
wall.** Every upgrade adds complexity. Complexity is not free. The cost of
premature distribution is months of engineering time spent on problems you do
not have yet.

> "Make it work. Make it right. Make it fast." --- Kent Beck
>
> For web crawlers: "Make it work on one machine. Make the architecture right.
> Distribute it when you have proven you need to."
