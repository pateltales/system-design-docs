# URL Frontier & Crawl Scheduling — Deep Dive

---

## 1. What is the URL Frontier?

The URL Frontier is the central data structure of a web crawler: a **priority queue of URLs waiting to be fetched**. It is not a simple queue. It must simultaneously enforce four constraints that often conflict with each other:

| Constraint | What It Means | Why It's Hard |
|---|---|---|
| **Politeness** | Don't overwhelm any single host. Space requests to the same domain by at least `crawl-delay` (default 1 req/sec). | A naive priority queue ignores domains entirely. The highest-priority URLs might all be on the same host, causing a denial-of-service. |
| **Priority** | Crawl important pages first. "Importance" is a composite of PageRank, change frequency, freshness, depth from seed. | Priority and politeness conflict: the most important page might be on a domain you just hit, so you must wait. |
| **Deduplication** | Never enqueue a URL that has already been crawled or is already sitting in the frontier. | At billion-URL scale, a hash set of seen URLs consumes 8+ GB of memory. Approximate structures (Bloom filters) introduce false positives that silently drop pages. |
| **Freshness** | Re-crawl pages that have changed. Pages that change more frequently should be re-crawled more often. | Freshness re-inserts URLs into the frontier, conflicting with deduplication. You need "seen but stale" as a distinct state from "seen and fresh." |

Without all four, you get pathological behavior: hammering a single server (no politeness), wasting bandwidth on duplicates (no dedup), crawling irrelevant pages while important ones wait (no priority), or serving stale search results (no freshness).

---

## 2. Mercator-Style Frontier Architecture

The canonical design comes from the **Mercator** web crawler (Heydon & Najork, 1999). Its key insight is a **two-stage pipeline** that cleanly separates priority from politeness.

### 2.1 High-Level Architecture

```
                        URLs discovered by parser
                                  |
                                  v
                      +---------------------+
                      |    Dedup Filter      |  (Bloom filter or hash set)
                      | "Have I seen this?"  |
                      +---------------------+
                                  |
                          (novel URLs only)
                                  v
                      +---------------------+
                      |    Prioritizer       |  (assigns priority score)
                      +---------------------+
                                  |
                                  v
               +------+------+------+------+
               | FQ-1 | FQ-2 | FQ-3 |...   |   <-- FRONT QUEUES (priority)
               | (hi) | (med)| (low)|      |       N queues, one per level
               +------+------+------+------+
                                  |
                          (biased selector:
                           weighted random
                           favoring higher
                           priority queues)
                                  |
                                  v
                      +---------------------+
                      |      Router          |  Maps URL -> back queue
                      | hash(domain) % B    |  by domain
                      +---------------------+
                                  |
                                  v
          +-------+-------+-------+-------+-------+
          | BQ-1  | BQ-2  | BQ-3  | BQ-4  |...    |   <-- BACK QUEUES (politeness)
          |cnn.com|bbc.com|nyt.com|reu... |       |       One queue per domain
          +-------+-------+-------+-------+-------+
                                  |
                                  v
                      +---------------------+
                      |  Min-Heap of Ready   |
                      |  Times               |
                      | (next_allowed_time,  |
                      |  queue_id)           |
                      +---------------------+
                                  |
                          (fetcher pops heap,
                           waits if needed,
                           dequeues URL from
                           that back queue)
                                  |
                                  v
                      +---------------------+
                      |    Fetcher Threads   |
                      +---------------------+
```

### 2.2 Front Queues (Priority)

There are **F front queues**, numbered 1 through F, where queue 1 is highest priority. When a new URL arrives, the **Prioritizer** computes a priority score and assigns it to the corresponding queue:

```
Priority Score Calculation:
  score = w1 * pagerank(domain)          // static importance
        + w2 * change_frequency(url)     // how often it changes
        + w3 * (1 / (1 + depth))         // shallower = higher
        + w4 * staleness(url)            // time since last crawl

Map score to queue:
  if score > 0.8:  queue = FQ-1  (highest)
  if score > 0.5:  queue = FQ-2
  if score > 0.2:  queue = FQ-3
  else:            queue = FQ-4  (lowest)
```

The **biased selector** dequeues from front queues using weighted random selection. Example weights: FQ-1 gets 60% of pulls, FQ-2 gets 25%, FQ-3 gets 10%, FQ-4 gets 5%. This ensures high-priority URLs are crawled first without completely starving low-priority ones.

### 2.3 Back Queues (Politeness)

There are **B back queues**, ideally one per active domain. The **Router** maps each URL from the front queues to the appropriate back queue:

```
Router logic:
  domain = extract_domain(url)
  queue_id = domain_to_queue_map.get(domain)
  if queue_id is None:
      queue_id = assign_empty_queue(domain)    // or hash(domain) % B
      domain_to_queue_map[domain] = queue_id
  back_queues[queue_id].enqueue(url)
```

Each back queue maintains a `next_allowed_crawl_time`. This is set to:

```
next_allowed_crawl_time = last_fetch_time + crawl_delay

where crawl_delay = max(robots_txt_delay, default_delay)
                   // default_delay is typically 1 second
```

### 2.4 Heap of Back Queue Ready Times

The fetcher doesn't poll all B back queues. Instead, a **min-heap** stores `(next_allowed_crawl_time, queue_id)` pairs. The fetch loop is:

```
while True:
    (ready_time, qid) = heap.pop_min()

    if now() < ready_time:
        sleep(ready_time - now())          // wait for politeness

    url = back_queues[qid].dequeue()
    fetch(url)

    // Update ready time
    new_ready_time = now() + crawl_delay(qid)
    if back_queues[qid].is_not_empty():
        heap.push( (new_ready_time, qid) )
    else:
        release_queue(qid)                 // free for reassignment
```

This gives O(log B) time per fetch operation, regardless of how many domains are active.

### 2.5 Why the Separation Matters

Without the two-stage design, consider what happens with a single priority queue:

```
BROKEN: Single priority queue
  1. cnn.com/breaking-news     (priority: 0.99)
  2. cnn.com/politics          (priority: 0.98)
  3. cnn.com/sports             (priority: 0.97)
  4. bbc.com/world              (priority: 0.60)
  5. nytimes.com/tech           (priority: 0.55)

Result: Fetcher hammers cnn.com three times in a row, violating politeness.
Meanwhile bbc.com and nytimes.com wait despite being ready.
```

With the Mercator design:

```
CORRECT: Two-stage Mercator
  Front queues feed all three CNN URLs to CNN's back queue.
  Heap says CNN's back queue isn't ready until t+1s.
  Fetcher pops bbc.com's back queue (ready now), fetches bbc.com/world.
  Then pops nytimes.com's back queue (ready now), fetches nytimes.com/tech.
  Then CNN's back queue is ready, fetches cnn.com/breaking-news.
  Then bbc.com is ready again... and so on round-robin.

Result: Priority is respected (CNN pages are in the front queues first),
        politeness is respected (each domain gets 1 req/sec max).
```

---

## 3. URL Deduplication

At web scale, deduplication is a first-class engineering problem. There are three levels.

### 3.1 Exact URL Deduplication

Before enqueuing a URL, check: "Have I seen this exact URL before?"

**Approach A: Hash Set**

Store the hash of every seen URL. Using a 64-bit hash (8 bytes) for 1 billion URLs:

```
Memory = 8 bytes * 1,000,000,000 = 8 GB
```

Exact (no false positives), but 8 GB is significant. For 10 billion URLs (Google-scale), that's 80 GB just for the dedup set. Must be sharded across machines.

**Approach B: Bloom Filter**

A probabilistic set. For 1% false positive rate with 1 billion URLs:

```
Optimal bits per element = -ln(0.01) / (ln(2))^2 ≈ 9.6 bits
Memory = 9.6 bits * 1,000,000,000 = 1.2 GB
Number of hash functions k = (m/n) * ln(2) ≈ 7
```

Space-efficient but has false positives: ~1% of novel URLs will be incorrectly classified as "already seen" and silently dropped.

| Approach | Memory (1B URLs) | False Positives | False Negatives | Deletions |
|---|---|---|---|---|
| Hash Set (64-bit) | 8 GB | None | None | Supported |
| Bloom Filter (1% FP) | 1.2 GB | ~1% | None | Not supported |
| Counting Bloom Filter | ~4.8 GB | ~1% | None | Supported |
| Cuckoo Filter | ~1.5 GB | ~1% | None | Supported |

For a crawler, false positives mean missing pages (bad for coverage). False negatives mean re-crawling pages (bad for efficiency). Bloom filters trade coverage for memory.

**Practical choice**: Use a Bloom filter for the in-memory fast path, backed by a disk-based hash set (RocksDB) for the authoritative check. The Bloom filter catches 99% of duplicates instantly; only novel-looking URLs hit disk.

### 3.2 URL Canonicalization

Before any dedup check, normalize the URL to a canonical form. Otherwise `http://WWW.Example.Com:80/path/../page?b=2&a=1#top` and `http://www.example.com/page?a=1&b=2` would be treated as different URLs.

**Canonicalization rules (applied in order):**

```
Input:  HTTP://WWW.Example.Com:80/path/../page?b=2&a=1#top

Step 1: Lowercase scheme           -> http://WWW.Example.Com:80/path/../page?b=2&a=1#top
Step 2: Lowercase host             -> http://www.example.com:80/path/../page?b=2&a=1#top
Step 3: Remove default port        -> http://www.example.com/path/../page?b=2&a=1#top
        (port 80 for HTTP, 443 for HTTPS)
Step 4: Resolve path (remove ..)   -> http://www.example.com/page?b=2&a=1#top
Step 5: Remove fragment (#top)     -> http://www.example.com/page?b=2&a=1
Step 6: Sort query parameters      -> http://www.example.com/page?a=1&b=2
Step 7: Remove trailing slash      -> http://www.example.com/page?a=1&b=2
Step 8: Normalize www              -> http://example.com/page?a=1&b=2
        (optional: depends on policy; some sites treat www and non-www differently)
Step 9: Percent-decode unreserved  -> (decode %41 -> A, etc.)
Step 10: Normalize to HTTPS        -> https://example.com/page?a=1&b=2
         (optional: if site supports HTTPS)

Output: https://example.com/page?a=1&b=2
```

### 3.3 Content-Based Deduplication

Different URLs can serve identical content (e.g., `example.com/page` and `example.com/page?ref=twitter`). After fetching, compute a cryptographic hash of the response body:

```
content_hash = SHA-256(response_body)
```

If the hash matches a previously seen page, skip indexing. This is **content-addressed storage** --- the same principle behind Git and IPFS.

### 3.4 Near-Duplicate Detection (SimHash / MinHash)

Many web pages are *nearly* identical: same article with different ad sidebars, same product with different session tokens in the HTML. Cryptographic hashes won't catch these. We need **similarity-preserving hashes**.

#### SimHash (Charikar, 2002)

SimHash computes a 64-bit fingerprint such that **similar documents have similar fingerprints** (small Hamming distance).

**Algorithm:**

```
1. Extract features: tokenize document into shingles (e.g., 3-word shingles)
   "the quick brown fox" -> {"the quick brown", "quick brown fox"}

2. Hash each shingle to a 64-bit value using a standard hash (e.g., MurmurHash)

3. Initialize a 64-dimensional vector V = [0, 0, ..., 0]

4. For each shingle hash h:
   For each bit position i (0..63):
     if bit i of h is 1:  V[i] += weight(shingle)
     else:                 V[i] -= weight(shingle)

5. Final fingerprint: for each i, if V[i] > 0 then bit i = 1, else bit i = 0

Result: 64-bit fingerprint
```

**Near-duplicate detection:**

```
Two documents are near-duplicates if:
  hamming_distance(simhash_A, simhash_B) <= 3

hamming_distance = popcount(simhash_A XOR simhash_B)
```

**Google-scale implementation** (Manku, Jain, Sarma, 2007 --- "Detecting Near-Duplicates for Web Crawling"):

The challenge: given a new fingerprint, find all existing fingerprints within Hamming distance 3 among **8 billion** stored fingerprints. Brute-force is O(8B) per lookup.

Their solution: **partition the 64 bits into blocks**, then use multiple sorted tables where each table sorts fingerprints by a different block permutation. A query checks only fingerprints that match on at least one block, reducing the search space dramatically.

```
64-bit fingerprint split into blocks:
  |--- 16 bits ---|--- 16 bits ---|--- 16 bits ---|--- 16 bits ---|
       Block A          Block B          Block C          Block D

If Hamming distance <= 3, at least one block must be identical.
(Pigeonhole principle: 3 differing bits across 4 blocks means
 at least one block has 0 differing bits.)

Build 4 tables, each sorted by a different block.
For a query, look up matching entries in each table,
then verify full Hamming distance.
```

#### MinHash + LSH (for batch similarity)

MinHash estimates **Jaccard similarity** between document shingle sets:

```
J(A, B) = |A ∩ B| / |A ∪ B|

MinHash: apply k random hash functions to each shingle set.
         For each hash function, keep the minimum hash value.
         Signature = [min_hash_1, min_hash_2, ..., min_hash_k]

P(min_hash_i(A) == min_hash_i(B)) = J(A, B)

So the fraction of matching signature positions ≈ Jaccard similarity.
```

**Locality-Sensitive Hashing (LSH)** groups signatures into bands for efficient nearest-neighbor search:

```
Divide k=100 signature positions into b=20 bands of r=5 rows each.
Two documents are candidate pairs if they agree on ALL rows in ANY band.
P(candidate | J=0.8) ≈ 1 - (1 - 0.8^5)^20 ≈ 0.9996  (almost certain match)
P(candidate | J=0.2) ≈ 1 - (1 - 0.2^5)^20 ≈ 0.0064  (almost no false match)
```

| Method | Output | Similarity Metric | Scale | Use Case |
|---|---|---|---|---|
| SHA-256 | 256-bit hash | Exact equality only | Any | Exact duplicate detection |
| SimHash | 64-bit fingerprint | Cosine similarity (via Hamming) | Billions (Google) | Online near-dup detection |
| MinHash + LSH | k-dimensional signature | Jaccard similarity | Millions-Billions | Batch near-dup clustering |

---

## 4. Priority Scoring

Priority determines which URLs get crawled first. A composite score is computed from multiple signals:

### 4.1 Static Priority (Domain Importance)

Based on **historical PageRank** or domain authority. Does not change between crawls.

```
static_priority("cnn.com/...")     = 0.95   (major news site)
static_priority("myblog.xyz/...") = 0.10   (unknown blog)
```

At Google's scale, this is derived from the full web graph. For smaller crawlers, use domain registration age, inbound link count, or a curated whitelist.

### 4.2 Dynamic Priority (Change Frequency)

Pages that change more frequently are more valuable to re-crawl. Estimated from past crawl history:

```
change_rate(url) = num_changes_observed / num_crawls

If cnn.com/breaking-news changed 9 out of 10 times we crawled it:
  change_rate = 0.9  -> high dynamic priority

If example.com/about changed 0 out of 10 times:
  change_rate = 0.0  -> low dynamic priority
```

More sophisticated: Poisson process model. Estimate lambda (changes per day) from observed intervals. Optimal re-crawl interval = 1/lambda.

### 4.3 Depth-Based Priority

Shallower pages (fewer hops from a seed URL) tend to be more important. This gives a BFS-like crawl order:

```
depth_priority(url) = 1 / (1 + depth)

depth=0 (seed):     1.0
depth=1 (one hop):  0.5
depth=2:            0.33
depth=5:            0.17
```

### 4.4 Freshness-Based Priority

Pages not crawled recently get a freshness boost:

```
staleness(url) = (now - last_crawl_time) / expected_change_interval

If a page changes daily and was last crawled 3 days ago:
  staleness = 3.0  -> very stale, high re-crawl priority

If a page changes yearly and was last crawled 1 day ago:
  staleness = 1/365 ≈ 0.003  -> very fresh, low re-crawl priority
```

### 4.5 Composite Score

```
priority(url) = w1 * static_priority(domain)
              + w2 * change_rate(url)
              + w3 * depth_priority(url)
              + w4 * staleness(url)

Typical weights (tuned per crawler):
  w1 = 0.3   (domain importance)
  w2 = 0.3   (change frequency)
  w3 = 0.1   (depth)
  w4 = 0.3   (staleness)
```

---

## 5. Freshness & Re-Crawl Scheduling

Freshness is not a one-time concern --- it's an ongoing loop. The frontier must re-insert URLs for re-crawling on a schedule.

### 5.1 Uniform Re-Crawl

Simplest approach: re-crawl every URL every T days.

```
Problem: wastes bandwidth on pages that never change,
         while frequently-changing pages go stale between crawls.
```

### 5.2 Adaptive Re-Crawl

Assign each URL a re-crawl interval based on observed change rate:

```
if page changed on last crawl:
    interval = max(interval / 2, min_interval)     // crawl more often
else:
    interval = min(interval * 2, max_interval)      // back off

Typical: min_interval = 1 hour, max_interval = 30 days
```

This is **exponential backoff / advance** --- the same pattern used in TCP congestion control.

### 5.3 Freshness Objective

Cho and Garcia-Molina (2003) formalized the freshness problem:

```
Maximize:  average freshness = (1/N) * SUM( freshness(url_i) )

where freshness(url_i) = 1  if our copy matches the live page
                         0  otherwise

Subject to: total crawl rate <= R pages/day  (bandwidth constraint)
```

The optimal policy: allocate crawl rate proportional to change frequency, but with diminishing returns. Pages that change faster than you can crawl them should not consume your entire budget.

### 5.4 Re-Crawl Loop in the Frontier

```
                  +---------------------+
                  |   Crawl History DB   |
                  |  (url, last_crawl,   |
                  |   change_rate,       |
                  |   next_crawl_time)   |
                  +---------------------+
                            |
                   (scheduler thread scans
                    for urls where now() >
                    next_crawl_time)
                            |
                            v
                  +---------------------+
                  |   Re-inject into     |
                  |   URL Frontier       |
                  |   (skip dedup check  |
                  |    for re-crawls)    |
                  +---------------------+
                            |
                            v
                    (normal Mercator flow:
                     front queues -> back
                     queues -> fetcher)
```

Note: re-crawl URLs **bypass the dedup filter** (they are intentionally being re-enqueued). They go through the prioritizer to compete with newly discovered URLs for crawl bandwidth.

---

## 6. Frontier Persistence

The frontier must survive crashes. Billions of URLs cannot live only in memory.

### 6.1 In-Memory with Write-Ahead Log (WAL)

```
+-------------------+       +-------------------+
|   In-Memory       |       |    WAL on Disk    |
|   Front + Back    | ----> |  (append-only     |
|   Queues          |       |   operation log)  |
+-------------------+       +-------------------+

On crash: replay WAL to reconstruct frontier state.
```

- **Pros**: Fastest access, simple implementation.
- **Cons**: Memory-limited. At 100 bytes per URL entry, 1 billion URLs = 100 GB. Doesn't fit on one machine.

### 6.2 Disk-Backed (RocksDB / LevelDB)

Use an **LSM-tree** key-value store to persist the frontier:

```
+-----------------------+
|     RocksDB           |
|  Key: (priority, url) |
|  Value: metadata      |
+-----------------------+
        |
   +----+----+
   | MemTable |  (in-memory, fast writes)
   +----+----+
        |
   +----+----+
   | SSTable  |  (sorted, on-disk)
   | SSTable  |
   | SSTable  |
   +----------+
```

- **Pros**: Handles billions of URLs. Survives crashes natively. Sorted iteration for priority ordering.
- **Cons**: Slower than pure in-memory (microseconds vs nanoseconds). Compaction pauses.
- **Used by**: Apache Nutch (uses a segment-based disk structure), Mercator (Berkeley DB).

### 6.3 Distributed Queue (Kafka)

```
+---------------------+
|   Kafka Topic:      |
|   "urls-to-crawl"   |
|                     |
|   Partition 0       |  <-- domain hash 0
|   Partition 1       |  <-- domain hash 1
|   Partition 2       |  <-- domain hash 2
|   ...               |
+---------------------+
        |
  Consumer group:
  each fetcher node
  consumes from
  assigned partitions
```

- **Pros**: Distributed, durable, high throughput, built-in partitioning.
- **Cons**: Kafka's consumer model processes messages in order. **Politeness requires deferring consumption** (you can't consume a URL if its domain's crawl delay hasn't elapsed). Kafka doesn't support "skip this message, come back to it later." You'd need a separate delay mechanism (e.g., re-publish to a delay topic, or buffer in memory with timers).
- **Workaround**: Use Kafka for bulk URL distribution to fetcher nodes, but each node maintains its own local Mercator-style frontier for politeness enforcement.

### Persistence Comparison

| Approach | Capacity | Latency | Crash Recovery | Distributed | Politeness Fit |
|---|---|---|---|---|---|
| In-memory + WAL | ~100M URLs/node | Nanoseconds | WAL replay | Manual sharding | Excellent |
| RocksDB/LevelDB | Billions/node | Microseconds | Native | Manual sharding | Excellent |
| Kafka | Unlimited | Milliseconds | Native | Built-in | Poor (needs local buffer) |
| Hybrid (Kafka + local RocksDB) | Unlimited | Micro-Milli | Native | Built-in | Excellent |

---

## 7. Contrasts: Googlebot vs. Scrapy

### 7.1 Googlebot

Google's crawler operates at a scale that dwarfs everything else:

```
Scale:
  - Knows hundreds of billions of URLs
  - Crawls billions of pages per day
  - Distributed across thousands of machines in multiple datacenters

Frontier architecture:
  - Massively distributed, sharded by domain
  - Integrated with search ranking: crawl budget allocated based on
    PageRank, site quality signals, and user query demand
  - Crawl budget: each site gets a finite number of fetches per day
    based on site importance and server capacity
  - Uses SimHash (Manku et al.) for near-dup detection at 8B+ page scale
  - robots.txt respected with caching and periodic refresh
  - Crawl scheduling adapts per-page: news pages every few minutes,
    static about pages every few weeks

Key concepts:
  - "Crawl budget" = max pages Googlebot will fetch from your site per day
  - Determined by: crawl rate limit (server health) x crawl demand (page importance)
  - GoogleBot respects Crawl-delay in robots.txt
  - Different Googlebot user agents for desktop, mobile, images, video, etc.
```

### 7.2 Scrapy

Python's most popular crawling framework. Designed for single-site or small-scale crawling:

```
Frontier architecture:
  - Default: simple FIFO queue (or priority queue via DEPTH_PRIORITY)
  - In-memory by default (scrapy-redis for distributed)
  - No built-in Mercator-style politeness enforcement
  - Relies on DOWNLOAD_DELAY setting (global, not per-domain)
  - CONCURRENT_REQUESTS_PER_DOMAIN setting for basic domain limiting
  - AutoThrottle extension adapts delay based on server response times

Limitations for web-scale:
  - Single process (GIL-bound for CPU, asyncio/Twisted for I/O)
  - No built-in distributed coordination
  - No persistent frontier (unless using scrapy-redis or Frontera)
  - Dedup: in-memory set of URL fingerprints (doesn't scale to billions)

Frontera (scrapy extension for large-scale):
  - Adds HBase/Kafka-backed frontier
  - Mercator-like priority + politeness
  - Bridges Scrapy's simplicity with web-scale requirements
```

| Feature | Googlebot | Scrapy (default) | Scrapy + Frontera |
|---|---|---|---|
| Scale | Hundreds of billions of URLs | Thousands-millions | Millions-billions |
| Priority | PageRank + ML signals | Depth-based (FIFO or LIFO) | Configurable |
| Politeness | Per-domain crawl budget, adaptive | Global DOWNLOAD_DELAY | Per-domain queues |
| Dedup | SimHash + distributed hash tables | In-memory set | HBase-backed |
| Freshness | Adaptive per-page re-crawl | Manual (no built-in) | Configurable |
| Persistence | Distributed (Bigtable/Spanner-era) | None (memory only) | HBase / Kafka |
| Distribution | Thousands of machines | Single process | Multi-process via message bus |

---

## 8. End-to-End Flow: Putting It All Together

```
 Seed URLs
     |
     v
+----------+     +------------------+     +-----------+
|  Dedup   | --> |   Prioritizer    | --> | Front     |
|  Filter  |     | (compute score,  |     | Queues    |
| (Bloom + |     |  assign to       |     | FQ1..FQn  |
|  RocksDB)|     |  queue level)    |     |           |
+----------+     +------------------+     +-----------+
     ^                                         |
     |                                   (biased pull)
     |                                         |
     |                                         v
     |                                   +-----------+
     |                                   |  Router   |
     |                                   | (domain   |
     |                                   |  -> back  |
     |                                   |   queue)  |
     |                                   +-----------+
     |                                         |
     |                                         v
     |                                   +-----------+     +-----------+
     |                                   | Back      | <-> | Min-Heap  |
     |                                   | Queues    |     | of ready  |
     |                                   | BQ1..BQb  |     | times     |
     |                                   +-----------+     +-----------+
     |                                         |
     |                                   (pop heap, wait
     |                                    if needed,
     |                                    dequeue URL)
     |                                         |
     |                                         v
     |                                   +-----------+
     |                                   |  Fetcher  |
     |                                   |  (HTTP    |
     |                                   |   GET)    |
     |                                   +-----------+
     |                                         |
     |                                         v
     |                                   +-----------+
     |                                   |  Parser   |
     |                                   | (extract  |
     |                                   |  links)   |
     |                                   +-----------+
     |                                         |
     |                                   (new URLs)
     |                                         |
     +-----------------------------------------+
                   (back to dedup)

  Concurrently:
  +------------------+
  | Re-Crawl         |
  | Scheduler        | --> re-injects stale URLs into frontier
  | (scans crawl     |     (bypasses dedup)
  |  history DB)     |
  +------------------+
```

---

## 9. Key Numbers for Estimation

| Parameter | Typical Value | Notes |
|---|---|---|
| Politeness delay | 1 second per domain | Respect robots.txt Crawl-delay |
| Front queues | 3-10 | One per priority level |
| Back queues | 10,000 - 1,000,000 | One per active domain |
| Bloom filter size (1B URLs, 1% FP) | 1.2 GB | 9.6 bits/element, 7 hash functions |
| Hash set size (1B URLs) | 8 GB | 8 bytes per 64-bit hash |
| SimHash fingerprint | 64 bits | Hamming distance <= 3 for near-dup |
| URLs per day (Google) | ~billions | Estimate: 5-10 billion |
| URLs per day (modest crawler) | ~10-100 million | 100 fetchers * 1 URL/sec * 86400 sec |
| Re-crawl interval (news) | Minutes-hours | High change frequency |
| Re-crawl interval (static) | Days-weeks | Low change frequency |
| RocksDB read latency | ~5-50 microseconds | SSD-backed |
| Kafka publish latency | ~1-5 milliseconds | Including replication |

---

## 10. References

1. **Heydon, A., & Najork, M.** (1999). "Mercator: A Scalable, Extensible Web Crawler." *World Wide Web*, 2(4), 219-229. --- The foundational paper for the front-queue/back-queue architecture described throughout this document.

2. **Charikar, M.** (2002). "Similarity Estimation Techniques from Rounding Algorithms." *Proceedings of the 34th Annual ACM Symposium on Theory of Computing (STOC)*, 380-388. --- Introduced SimHash, the locality-sensitive hash function for cosine similarity.

3. **Manku, G. S., Jain, A., & Das Sarma, A.** (2007). "Detecting Near-Duplicates for Web Crawling." *Proceedings of the 16th International Conference on World Wide Web (WWW)*, 141-150. --- Google's web-scale implementation of SimHash for near-duplicate detection across 8 billion web pages.

4. **Cho, J., & Garcia-Molina, H.** (2003). "Effective Page Refresh Policies for Web Crawlers." *ACM Transactions on Database Systems*, 28(4), 390-426. --- Formalized the freshness optimization problem and optimal re-crawl scheduling.

5. **Broder, A. Z.** (1997). "On the Resemblance and Containment of Documents." *Proceedings of the Compression and Complexity of Sequences*, 21-29. --- Introduced MinHash for estimating Jaccard similarity, foundational for near-duplicate detection.

6. **Bloom, B. H.** (1970). "Space/Time Trade-offs in Hash Coding with Allowable Errors." *Communications of the ACM*, 13(7), 422-426. --- The original Bloom filter paper, used universally for URL deduplication in crawlers.
