# System Design Interview Simulation: Design a Web Crawler (like Googlebot)

> **Interviewer:** Principal Engineer (L8), Search Infrastructure Team
> **Candidate Level:** SDE-3 (L6 — Senior Software Development Engineer)
> **Duration:** ~60 minutes
> **Date:** February 23, 2026

---

## PHASE 1: Opening & Problem Statement (~2 min)

**Interviewer:**
Hey, welcome. I'm on the search infrastructure team. Today I'd like you to design a **web crawler** — think Googlebot, but scoped for an interview. Not just "download pages" — I'm talking about the full system: URL scheduling, distributed fetching at scale, politeness enforcement, deduplication, content storage, and re-crawl freshness management.

I care about how you think about scale, the URL frontier problem, and the tradeoffs between coverage, freshness, and politeness. I'll push on your choices — that's calibration, not criticism.

Take it away.

---

## PHASE 2: Requirements Gathering & Scoping (~5 min)

**Candidate:**
Thanks! A web crawler is deceptively complex — it touches distributed systems, networking, data structures, and ethics. Let me scope this before diving in.

**Functional Requirements — what operations do we need?**

> "Let me identify the core operations:
>
> **Crawl-side operations:**
> - **Seed URL ingestion** — Accept a set of seed URLs to start crawling from
> - **URL discovery** — Parse fetched pages for outgoing links, add new URLs to the frontier
> - **Page fetching** — Download web pages over HTTP/HTTPS
> - **Content parsing** — Extract links, metadata, and text from HTML
> - **Content storage** — Store raw HTML and extracted data durably
> - **URL deduplication** — Don't crawl the same URL (or same content) twice
> - **Robots.txt compliance** — Respect the Robots Exclusion Protocol for every domain
> - **Re-crawl scheduling** — Revisit pages that change, at appropriate intervals
>
> **Management operations:**
> - **Crawl job management** — Start, pause, resume, cancel crawl jobs
> - **Monitoring** — Crawl rate, error rates, queue depth, dedup rate, per-domain stats
> - **Rate control** — Operators can adjust crawl aggressiveness per domain
>
> A few clarifying questions:
> - **Are we building a general-purpose search engine crawler, or a focused/vertical crawler?**"

**Interviewer:** "Design for general-purpose web crawling — the goal is to crawl as much of the web as possible. Think search engine scale, but you don't need to build the search index or ranking pipeline."

> "- **Do we need to render JavaScript?** Modern SPAs load content via JS. Googlebot uses a Web Rendering Service (WRS) backed by headless Chromium for this."

**Interviewer:** "Start with HTML-only crawling. Mention JS rendering as a deep dive but don't architect it fully. Good awareness, though."

> "- **What's our target scale?** Google's index contains roughly 400 billion documents (per testimony during the Google antitrust trial). Common Crawl crawls ~2.5-3.5 billion pages per monthly crawl. For this interview, should I target something in between?"

**Interviewer:** "Target 1 billion pages for a full crawl cycle. That's a reasonable interview scope — large enough to force distribution, small enough to reason about."

**Non-Functional Requirements:**

> "Now the critical constraints. A web crawler is defined by its non-functional properties:
>
> | Dimension | Requirement | Rationale |
> |---|---|---|
> | **Crawl rate** | 2,000-5,000 pages/sec | 1B pages / (7 days × 86,400 sec) ≈ 1,653 pages/sec. With overhead: target ~2,000-5,000 |
> | **Politeness** | Max 1 req/sec per domain (default) | Overwhelming a server is unethical and gets us IP-blocked. Respect `Crawl-delay` from robots.txt |
> | **Storage** | ~100 TB raw content | 1B pages × 100 KB avg = 100 TB. With gzip (~5x compression): ~20 TB |
> | **URL dedup memory** | ~8 GB for hash set | 1B URLs × 8 bytes (64-bit hash) = 8 GB. Fits in memory on a single machine |
> | **Availability** | Survive node failures | A crawler node dying shouldn't lose frontier state or halt the crawl |
> | **Freshness** | Re-crawl changed pages within hours-days | Important pages (news) re-crawl hourly; stable pages (docs) weekly |
> | **Bandwidth** | ~4 Gbps sustained | 5,000 pages/sec × 100 KB = 500 MB/sec = 4 Gbps |
> | **Robots.txt compliance** | 100% | Non-negotiable. Violating robots.txt is unethical and can get us legally blocked |

**Interviewer:**
Good scoping. You mentioned 1 req/sec per domain as a politeness default — why that number specifically?

**Candidate:**

> "It's a conservative but universally safe default. There's no official standard — robots.txt's `Crawl-delay` directive isn't even part of the original spec (Google's parser ignores it, but Bing respects it). 1 req/sec means even a small shared-hosting server won't notice us. Google itself uses adaptive politeness — they measure server response times in real time and back off if the server slows down. But starting with 1 req/sec is safe, and we can optimize later with adaptive delays."

**Interviewer:**
Strong reasoning. Let's get into the architecture.

---

### L5 vs L6 vs L7 — Phase 2 Rubric

| Aspect | L5 (SDE-2) | L6 (SDE-3) | L7 (Principal) |
|---|---|---|---|
| **Functional Reqs** | Lists fetch, parse, store | Proactively raises robots.txt, deduplication, re-crawl scheduling, crawl job management | Additionally discusses JS rendering trade-offs, geographic crawl distribution, legal/GDPR concerns, crawl budget allocation |
| **Non-Functional** | Mentions "lots of pages" | Quantifies: 1B pages, 100 TB storage, 2,000-5,000 pages/sec, 8 GB dedup memory, bandwidth calc | Frames NFRs in business impact: freshness affects search quality, politeness affects legal risk, coverage vs depth trade-off |
| **Scale Math** | Doesn't do math | Back-of-envelope: pages/sec from total pages ÷ time, storage from pages × avg size, bandwidth from rate × size | Identifies bottleneck cascade: network I/O → DNS resolution → frontier ops → parsing → storage writes |
| **Real-world contrast** | Doesn't mention real crawlers | Cites Google's ~400B doc index, Common Crawl's ~3B pages/month, adaptive politeness | Explains WHY Google and Common Crawl made different choices (search ranking vs. archival completeness) |

---

## PHASE 3: API Design (~5 min)

**Candidate:**

> "Let me focus on the APIs that matter most for a web crawler design discussion — the URL frontier (the core scheduling problem), the fetcher (I/O-intensive component), and crawl management (operator control). The full API surface is documented in [02-api-contracts.md](02-api-contracts.md)."

### Crawl Management APIs (External)

> "```
> POST /crawl/jobs
> Request:  { seedUrls: [...], depthLimit: 15, domainWhitelist: [...],
>             maxPagesPerDomain: 100000, crawlDelay: 1.0, priority: "HIGH" }
> Response: { jobId, status: "RUNNING", createdAt }
>
> GET  /crawl/jobs/{jobId}
> Response: { jobId, status, urlsDiscovered, urlsFetched, urlsFailed, startedAt }
>
> PUT  /crawl/jobs/{jobId}/pause
> PUT  /crawl/jobs/{jobId}/resume
> DELETE /crawl/jobs/{jobId}
> ```
>
> **Why this matters:** Operators need to control the crawl. If a site owner complains we're crawling too aggressively, an operator pauses the crawl for that domain. If we discover a spider trap (a site generating infinite URLs), we cancel."

### URL Frontier APIs (Internal — the most critical)

> "```
> POST /frontier/enqueue
> Request:  { url, priority, depth, parentUrl, discoveredAt }
> Response: { accepted: true }   // or rejected if already seen
>
> GET  /frontier/dequeue
> Response: { url, priority, depth, domain }
> // CRITICAL: must respect politeness — won't return a URL
> // if the domain was crawled too recently
>
> POST /frontier/mark-complete
> Request:  { url, result: "SUCCESS|FAILURE|REDIRECT|ROBOTS_BLOCKED",
>             contentHash, statusCode, redirectUrl }
>
> GET  /frontier/stats
> Response: { queueDepth, domainsInQueue, crawlRatePerSec, topDomains: [...] }
> ```
>
> **Key design insight:** The frontier is not a simple queue. It's simultaneously a priority queue (what's most important?), a politeness enforcer (is this domain ready to be crawled?), and a dedup filter (have we seen this URL before?). This is the core data structure of the entire crawler — more on this in the architecture."

### Fetcher APIs (Internal)

> "```
> POST /fetch
> Request:  { url, timeout: 30000, userAgent: "MyCrawler/1.0 (+http://...)" }
> Response: { httpStatus, headers, body, responseTimeMs, finalUrl, contentType }
>
> POST /parse
> Request:  { url, rawHtml, contentType }
> Response: { outgoingLinks: [...], title, metaDescription, canonicalUrl,
>             robotsMeta: { noindex, nofollow }, language, contentHash }
> ```
>
> **Contrast with Googlebot:** Googlebot's fetch pipeline is far more sophisticated — it includes a Web Rendering Service (WRS) that runs headless Chromium to execute JavaScript. Google announced that WRS uses an 'evergreen' Chromium — always the latest version. For our interview crawler, we skip JS rendering. This means we'll miss content on SPAs (estimated 10-20% of pages have critical content behind JS), but we avoid 10-100x resource overhead."

**Interviewer:**
Good. You've identified the frontier as the critical component. Let's build this system step by step.

---

### L5 vs L6 vs L7 — Phase 3 Rubric

| Aspect | L5 (SDE-2) | L6 (SDE-3) | L7 (Principal) |
|---|---|---|---|
| **API Design** | Lists basic REST endpoints | Explains WHY the frontier API is the hardest (simultaneous priority + politeness + dedup), calls out the mark-complete feedback loop | Discusses idempotency guarantees (re-enqueue safety), API rate limiting for internal services, gRPC vs REST for internal APIs (latency matters) |
| **Frontier API** | "A queue with URLs" | Breaks down three responsibilities (priority, politeness, dedup), explains why dequeue is conditional on domain readiness | Discusses frontier API sharding (partition by domain hash), consistency model (eventual is fine for URL dedup), back-pressure signals |
| **Real-world contrast** | Doesn't contrast | Notes Googlebot uses WRS for JS rendering; we skip it for simplicity | Explains Google's two-phase indexing (HTML first, rendered second), how WRS caching behavior (30-day cache, ignores HTTP cache headers) affects content freshness |

---

## PHASE 4: High-Level Architecture — Iterative Build-Up (~15 min)

**Candidate:**

> "Let me start with the simplest thing that could possibly work, find the problems, and evolve. This is how I'd approach building any distributed system."

---

### Attempt 0: Single-Threaded Crawler

> "The absolute simplest crawler — a single loop:
>
> ```
>     Seed URLs
>         │
>         ▼
>     ┌─────────────────────────────────────────────┐
>     │           Single-Threaded Crawler            │
>     │                                              │
>     │   queue = [seed URLs]                        │
>     │   visited = {}  (hash set)                   │
>     │                                              │
>     │   while queue not empty:                     │
>     │       url = queue.dequeue()                  │
>     │       if url in visited: continue            │
>     │       html = HTTP_GET(url)                   │
>     │       links = parse_links(html)              │
>     │       save_to_disk(url, html)                │
>     │       visited.add(url)                       │
>     │       for link in links:                     │
>     │           queue.enqueue(link)                │
>     └─────────────────────────────────────────────┘
> ```
>
> URLs stored in a simple FIFO queue (breadth-first). Visited URLs tracked in an in-memory hash set. Pages saved as local files."

**Interviewer:**
What's wrong with this?

**Candidate:**

> "| Problem | Impact |
> |---------|--------|
> | **Sequential I/O** | Only 1 page at a time. Fetching a page takes ~200-500ms (DNS + TCP + HTTP). So ~2-5 pages/sec. At this rate, 1B pages = **6-15 years** |
> | **No politeness** | No delay between requests to the same domain. If we crawl cnn.com, we'll fire hundreds of requests/sec — we'll get IP-blocked instantly |
> | **No robots.txt** | We're violating the Robots Exclusion Protocol. This is both unethical and will get us blocked |
> | **No DNS caching** | Every URL triggers a fresh DNS lookup. DNS resolution takes ~10-100ms. Massive waste since many URLs share the same domain |
> | **No content dedup** | Different URLs can serve identical content (mirrors, URL parameters). We'll store duplicates |
> | **No priority** | FIFO treats all pages equally. A Wikipedia article and a random forum post get the same priority |
> | **Single point of failure** | If the process crashes, we lose the entire visited set and queue |
>
> Let me fix the most critical problems: concurrency and politeness."

---

### Attempt 1: Multi-Threaded Crawler with Politeness

> "Replace the single thread with a thread pool, add robots.txt and politeness:
>
> ```
>     Seed URLs
>         │
>         ▼
>     ┌─────────────────────────────────────────────────────────┐
>     │              Multi-Threaded Crawler                     │
>     │                                                         │
>     │   ┌─────────────┐     ┌──────────────────────────┐     │
>     │   │  robots.txt  │     │   Thread Pool (50 threads) │     │
>     │   │    Cache     │     │                            │     │
>     │   │              │     │  Thread 1: fetch url_a     │     │
>     │   │ domain →     │     │  Thread 2: fetch url_b     │     │
>     │   │  rules,      │     │  Thread 3: fetch url_c     │     │
>     │   │  crawl-delay │     │  ...                       │     │
>     │   └─────────────┘     │  Thread 50: fetch url_x    │     │
>     │                        └──────────┬───────────────┘     │
>     │                                   │                      │
>     │   ┌──────────────────────┐       │                      │
>     │   │ Per-Domain Queues    │◄──────┘ (enqueue new URLs)   │
>     │   │                      │                               │
>     │   │ cnn.com:    [u1,u2]  │  ┌────────────┐              │
>     │   │ wiki.org:   [u3,u4]  │  │  DNS Cache  │              │
>     │   │ github.com: [u5]     │  │  (LRU, TTL) │              │
>     │   └──────────────────────┘  └────────────┘              │
>     │                                                         │
>     │   ┌──────────────────────┐                              │
>     │   │  Visited Set         │                              │
>     │   │  (HashSet<String>)   │                              │
>     │   └──────────────────────┘                              │
>     │                                                         │
>     │   ┌──────────────────────┐                              │
>     │   │  Local File Storage  │                              │
>     │   └──────────────────────┘                              │
>     └─────────────────────────────────────────────────────────┘
> ```
>
> **What's better:**
> - 50 threads fetch concurrently. Each thread blocks on I/O but total throughput ≈ 50 × 2-5 pages/sec = **100-250 pages/sec**
> - robots.txt fetched and cached per domain (24h TTL). Check rules before every fetch. Respect `Disallow` paths
> - Per-domain queues enforce politeness: 1 request/sec per domain. Each domain queue tracks `last_crawl_time`
> - DNS cache (LRU with TTL from DNS records) eliminates redundant lookups. Cache hit rate >90% for a crawler
>
> **Contrast with Scrapy:** Scrapy uses a similar model — Twisted's async reactor pattern with `DOWNLOAD_DELAY` setting for politeness. Scrapy's default concurrency is 16 concurrent requests (`CONCURRENT_REQUESTS`). It's single-process, single-machine. Good for focused crawls, not web-scale."

**Interviewer:**
What's still broken?

**Candidate:**

> "| Problem | Impact |
> |---------|--------|
> | **Thread overhead** | OS threads cost ~1 MB stack each. 50 threads = 50 MB. Pushing to 1,000 threads = 1 GB and context-switching kills performance. Can't reach 5,000 pages/sec |
> | **Contention** | The shared frontier (queue + visited set) is a lock contention bottleneck. Every thread competes for the lock on enqueue/dequeue |
> | **No content dedup** | Different URLs serve identical content (mirrors, URL params that don't change content). We store it all |
> | **No priority** | FIFO within domain queues. A high-PageRank homepage and a 404'd deep link get equal treatment |
> | **Local storage** | Files on local disk. Disk fills up at ~100 TB. No durability if machine dies |
> | **Single machine** | 250 pages/sec × 7 days = only ~150M pages. Need 7x more throughput for 1B pages/week |"

---

### Attempt 2: Async I/O Crawler with Mercator-Style Frontier

> "Two fundamental changes: replace threads with async I/O, and implement the Mercator frontier design (from the seminal Mercator web crawler paper by Heydon & Najork).
>
> **Async I/O:** Instead of 50 threads each blocking on one connection, use non-blocking I/O (epoll on Linux, kqueue on macOS). A single event loop manages thousands of concurrent HTTP connections. Think Netty (Java), asyncio (Python), or libuv (Node.js). This gets us 5,000-50,000 simultaneous connections per machine.
>
> **Mercator-style frontier** — this is the key insight for the interview:
>
> ```
>     New URLs discovered
>         │
>         ▼
>     ┌─────────────────────────────────────────────────────────────┐
>     │                    URL FRONTIER                             │
>     │                                                             │
>     │  ┌─────────────────────────────────────────────────────┐   │
>     │  │              FRONT QUEUES (Priority)                 │   │
>     │  │                                                      │   │
>     │  │  Queue F1 (Priority HIGH):   [url_a, url_b, ...]    │   │
>     │  │  Queue F2 (Priority MEDIUM): [url_c, url_d, ...]    │   │
>     │  │  Queue F3 (Priority LOW):    [url_e, url_f, ...]    │   │
>     │  │                                                      │   │
>     │  │  Prioritizer: domain importance, depth, freshness    │   │
>     │  └──────────────────────┬────────────────────────────┘   │
>     │                         │ weighted pull                    │
>     │                         ▼                                  │
>     │  ┌──────────────────────────────────────────────────────┐  │
>     │  │              ROUTER (domain → back queue)             │  │
>     │  └──────────────────────┬───────────────────────────────┘  │
>     │                         │                                  │
>     │  ┌──────────────────────▼───────────────────────────────┐  │
>     │  │              BACK QUEUES (Politeness)                 │  │
>     │  │                                                      │  │
>     │  │  Queue B1 (cnn.com):     [url_x, url_y]             │  │
>     │  │  Queue B2 (wiki.org):    [url_z]                    │  │
>     │  │  Queue B3 (github.com):  [url_w]                    │  │
>     │  │  ...                                                 │  │
>     │  │  Each queue: next_allowed_crawl_time                 │  │
>     │  └──────────────────────┬───────────────────────────────┘  │
>     │                         │                                  │
>     │  ┌──────────────────────▼───────────────────────────────┐  │
>     │  │  MIN-HEAP of (next_allowed_time, queue_id)           │  │
>     │  │  Pop heap → find next ready back queue → dequeue URL │  │
>     │  │  After dequeue: update next_allowed_time, push back  │  │
>     │  └──────────────────────────────────────────────────────┘  │
>     │                                                             │
>     │  ┌──────────────────────────────────────────────────────┐  │
>     │  │  URL DEDUP: Bloom Filter or Hash Set                 │  │
>     │  │  + URL canonicalization before dedup check            │  │
>     │  └──────────────────────────────────────────────────────┘  │
>     └─────────────────────────────────────────────────────────────┘
>             │
>             ▼
>     ┌────────────────────┐     ┌──────────────────────┐
>     │  Async Fetcher      │     │  Content Store        │
>     │  (epoll/kqueue)     │────▶│  (S3 / HDFS)          │
>     │  5,000-50,000       │     │  WARC format           │
>     │  concurrent conns   │     │  Content-hash dedup    │
>     └────────────────────┘     └──────────────────────┘
> ```
>
> **Why this design (the Mercator insight)?**
> The key insight from the Mercator paper is the **separation of priority from politeness**:
> - **Front queues** decide WHAT is most important to crawl (priority)
> - **Back queues** decide WHEN it's safe to crawl a domain (politeness)
> - The **min-heap** efficiently schedules which back queue is ready next (O(log n) per dequeue)
>
> Without this separation, you have a fundamental conflict: a high-priority page on a polite domain (say, `small-blog.com` with Crawl-delay: 10) could starve low-priority pages on fast domains. Or a high-priority domain that was just crawled would block all other high-priority URLs.
>
> **URL canonicalization before dedup:**
> `HTTP://WWW.Example.Com:80/path/../page?b=2&a=1#top` → `http://www.example.com/page?a=1&b=2`
> Normalize: lowercase scheme/host, remove default port, resolve relative paths, sort query params, strip fragments.
>
> **Content-hash dedup:**
> Even different URLs can serve identical content (mirrors, URL params that don't change content). Compute SHA-256 of the page body. Store by content hash (content-addressable storage). If content hash matches a stored page → mark as duplicate, don't store again.
>
> **WARC storage:**
> Store pages in WARC (Web ARChive) format — the ISO 28500 standard used by the Internet Archive and Common Crawl. WARC files contain the HTTP request, response headers, and body for each page. Common Crawl stores ~2.5-3.5 billion pages per monthly crawl in ~350-460 TiB of WARC files on S3.
>
> **What's better:**
> - Async I/O: 5,000-50,000 concurrent connections on a single machine. ~1,000-5,000 pages/sec
> - Mercator frontier cleanly separates priority from politeness
> - URL canonicalization + content-hash dedup eliminates most duplicates
> - Content stored durably in object storage (S3) in standard WARC format
>
> **Contrast with Scrapy:** Scrapy also uses async I/O (Twisted reactor), but its frontier is a simple FIFO/priority queue with no politeness-aware dequeue — it relies on a global `DOWNLOAD_DELAY` setting. No Mercator-style back queues. Single-process only (Scrapy-Redis adds distributed support). Suitable for small-to-medium crawls, not billions of pages."

**Interviewer:**
The Mercator frontier is a strong answer. What's still broken?

**Candidate:**

> "| Problem | Impact |
> |---------|--------|
> | **Single machine** | One machine at 5,000 pages/sec is our target, but we're limited by one NIC (4 Gbps), one CPU (parsing), and memory (frontier + dedup). No fault tolerance — if this machine dies, we lose everything |
> | **In-memory frontier** | The frontier, visited set, and back queue heap are all in memory. A crash loses all crawl state. At 1B URLs, the visited set alone is 8 GB — pushing limits |
> | **No re-crawl** | This is a one-pass crawl. Pages crawled once are never revisited. Content goes stale immediately |
> | **No spider trap detection** | A calendar that generates infinite `/date/2024/01/01`, `/date/2024/01/02`, ... URLs will consume our entire crawl budget for one domain |
> | **No quality filtering** | We crawl spam, error pages, thin content — wasting bandwidth and storage |"

---

### Attempt 3: Distributed Crawler

> "We need to go multi-machine. The key decision: **how to partition work?**
>
> ```
>                     ┌────────────────────────────┐
>                     │     Coordinator (ZooKeeper)  │
>                     │  - Node health (heartbeats)  │
>                     │  - Domain → node assignment  │
>                     │  - Rebalancing on failure    │
>                     └──────────┬─────────────────┘
>                                │
>          ┌─────────────────────┼─────────────────────┐
>          │                     │                      │
>          ▼                     ▼                      ▼
>     ┌──────────┐         ┌──────────┐          ┌──────────┐
>     │  Node 1   │         │  Node 2   │          │  Node N   │
>     │           │         │           │          │           │
>     │ Domains:  │         │ Domains:  │          │ Domains:  │
>     │ a*.com    │         │ b*.com    │          │ z*.com    │
>     │ ...       │         │ ...       │          │ ...       │
>     │           │         │           │          │           │
>     │ ┌───────┐ │         │ ┌───────┐ │          │ ┌───────┐ │
>     │ │Frontier│ │         │ │Frontier│ │          │ │Frontier│ │
>     │ │(local) │ │         │ │(local) │ │          │ │(local) │ │
>     │ └───────┘ │         │ └───────┘ │          │ └───────┘ │
>     │ ┌───────┐ │         │ ┌───────┐ │          │ ┌───────┐ │
>     │ │Fetcher │ │         │ │Fetcher │ │          │ │Fetcher │ │
>     │ │(async) │ │         │ │(async) │ │          │ │(async) │ │
>     │ └───────┘ │         │ └───────┘ │          │ └───────┘ │
>     │ ┌───────┐ │         │ ┌───────┐ │          │ ┌───────┐ │
>     │ │Parser  │ │         │ │Parser  │ │          │ │Parser  │ │
>     │ └───────┘ │         │ └───────┘ │          │ └───────┘ │
>     │ ┌───────┐ │         │ ┌───────┐ │          │ ┌───────┐ │
>     │ │RocksDB │ │         │ │RocksDB │ │          │ │RocksDB │ │
>     │ │(dedup) │ │         │ │(dedup) │ │          │ │(dedup) │ │
>     │ └───────┘ │         │ └───────┘ │          │ └───────┘ │
>     └─────┬─────┘         └─────┬─────┘          └─────┬─────┘
>           │                     │                       │
>           │    URL routing: hash(domain) % N            │
>           │    (discovered URLs sent to owning node)    │
>           │                     │                       │
>           └─────────────────────┼───────────────────────┘
>                                 │
>                                 ▼
>                     ┌────────────────────────┐
>                     │   Shared Content Store   │
>                     │   (S3 / HDFS)            │
>                     │   WARC files             │
>                     └────────────────────────┘
>                     ┌────────────────────────┐
>                     │   Metadata Store         │
>                     │   (Cassandra / HBase)    │
>                     │   URL → crawl state      │
>                     └────────────────────────┘
> ```
>
> **Partitioning: Domain-based (not URL-hash-based). Here's why:**
>
> **URL-hash partitioning** (hash(URL) % N): Simple, uniform distribution. But the same domain's URLs get scattered across all nodes. Politeness enforcement requires cross-node coordination — node 1 needs to know if node 3 just crawled `cnn.com` before fetching another CNN page. This is expensive distributed coordination for the most frequent operation.
>
> **Domain-based partitioning** (hash(domain) % N): All URLs for a domain go to the same node. Politeness is naturally local — each node manages its own domains' crawl rates independently. The downside is uneven load (wikipedia.org has millions of URLs, small-blog.com has 10). We mitigate with consistent hashing + virtual nodes for rebalancing.
>
> **URL routing:** When page parsing discovers a new link to `example.com`, we hash `example.com` to determine which node owns it, and send the URL to that node's frontier. This is the only cross-node communication for URLs.
>
> **Frontier persistence:** Each node uses RocksDB (or LevelDB) on local SSD for its frontier and dedup set. RocksDB handles billions of key-value pairs with LSM-tree storage — much larger than memory allows. Write-Ahead Log (WAL) provides crash recovery.
>
> **Metadata store:** Cassandra or HBase for the global URL→crawl state mapping: URL, last crawl timestamp, content hash, HTTP status, redirect target. Supports fast lookup by URL and range scans for re-crawl scheduling.
>
> **Fault tolerance:**
> - Each node sends heartbeats to ZooKeeper
> - If a node dies, ZooKeeper detects it, and the coordinator reassigns its domains to surviving nodes
> - The dead node's frontier is on RocksDB on its SSD — if the SSD survives, we recover. If not, we re-discover those URLs from the metadata store
>
> **Contrast with Apache Nutch:** Nutch also distributes crawling, but via Hadoop MapReduce. Its crawl cycle is batch-oriented: generate (select URLs) → fetch → parse → update (add new URLs). Each cycle is a MapReduce job. This gives high throughput but high latency between cycles (minutes to hours). Our design is real-time — URLs are crawled as soon as they're discovered and politeness allows."

**Interviewer:**
Good distribution strategy. Why domain-based over URL-hash, specifically?

**Candidate:**

> "It comes down to the politeness constraint. Politeness is the most frequently checked constraint in a crawler — every single dequeue checks 'can I crawl this domain now?' With URL-hash partitioning, that check requires distributed coordination. With domain-based partitioning, it's a local memory lookup. At 5,000 dequeues/sec across the cluster, the overhead of distributed politeness coordination would be massive.
>
> The trade-off is load imbalance. But in practice, the web follows a power-law distribution — there are many more small domains than large ones. Consistent hashing with virtual nodes (e.g., 256 virtual nodes per physical node) spreads the load reasonably well. And if a node is still overloaded, we can implement work-stealing: an idle node takes over some domains from an overloaded one."

**Interviewer:**
What's still broken?

**Candidate:**

> "| Problem | Impact |
> |---------|--------|
> | **No freshness** | Pages are crawled once. A news homepage that changes every 5 minutes goes stale immediately |
> | **No adaptive politeness** | Fixed 1 req/sec regardless of server capacity. Fast servers (e.g., CDN-backed) could handle 10 req/sec; slow shared hosts should be 0.1 req/sec |
> | **No spider trap detection** | Calendar traps, session ID traps, infinite depth — waste crawl budget |
> | **No quality filtering** | We store spam, soft 404s, thin content |
> | **No monitoring** | We're flying blind. Can't tell if we're being blocked, hitting traps, or wasting budget |"

---

### Attempt 4: Intelligent Crawling (Freshness, Quality, Monitoring)

> "```
>     ┌──────────────────────────────────────────────────────────────┐
>     │                   INTELLIGENT CRAWLER                        │
>     │                                                              │
>     │  ┌────────────────────────────────────────────────────────┐ │
>     │  │  Re-Crawl Scheduler                                     │ │
>     │  │  - Track change frequency per page (content hash diff)  │ │
>     │  │  - Poisson model: λ = changes/crawls, next = 1/λ       │ │
>     │  │  - HTTP conditional: If-Modified-Since, If-None-Match   │ │
>     │  │  - Sitemap monitoring: check <lastmod> timestamps       │ │
>     │  └────────────────────────────────────────────────────────┘ │
>     │                                                              │
>     │  ┌────────────────────────────────────────────────────────┐ │
>     │  │  Spider Trap Detector                                   │ │
>     │  │  - Max depth: 15 levels from seed                       │ │
>     │  │  - Max pages/domain: 100,000                            │ │
>     │  │  - URL pattern detection: infinite numeric sequences    │ │
>     │  │  - Content dedup: same content at different URLs → trap │ │
>     │  │  - URL length limit: >2048 chars → skip                 │ │
>     │  └────────────────────────────────────────────────────────┘ │
>     │                                                              │
>     │  ┌────────────────────────────────────────────────────────┐ │
>     │  │  Quality Scorer                                         │ │
>     │  │  - Text-to-HTML ratio (low ratio → thin/boilerplate)   │ │
>     │  │  - Soft 404 detection (200 status + "not found" text)  │ │
>     │  │  - Spam classification (keyword stuffing, link farms)  │ │
>     │  │  - Duplicate cluster detection (SimHash, ≤3 bits diff) │ │
>     │  └────────────────────────────────────────────────────────┘ │
>     │                                                              │
>     │  ┌────────────────────────────────────────────────────────┐ │
>     │  │  Adaptive Politeness                                    │ │
>     │  │  - Measure server response time per domain              │ │
>     │  │  - Fast responses → cautiously decrease delay           │ │
>     │  │  - Slow/429/503 → increase delay sharply                │ │
>     │  │  - Respect Retry-After header                           │ │
>     │  └────────────────────────────────────────────────────────┘ │
>     │                                                              │
>     │  ┌────────────────────────────────────────────────────────┐ │
>     │  │  Monitoring & Alerting                                  │ │
>     │  │  - Crawl rate (total + per domain)                      │ │
>     │  │  - Error rate by type (DNS, timeout, 4xx, 5xx)          │ │
>     │  │  - Queue depth trend                                    │ │
>     │  │  - Dedup hit rate                                       │ │
>     │  │  - Content store growth                                 │ │
>     │  │  - Per-domain dashboards                                │ │
>     │  └────────────────────────────────────────────────────────┘ │
>     └──────────────────────────────────────────────────────────────┘
> ```
>
> **Re-crawl scheduling (the freshness problem):**
>
> The web changes constantly. We need to re-crawl pages, but re-crawling every page every day is infeasible. Solution: **adaptive re-crawl based on observed change rate.**
>
> For each page, track: how many times we've crawled it (n), how many times the content hash changed (k). Estimate change rate λ = k/n. Schedule next re-crawl at interval = 1/λ. A news homepage that changes every hour gets re-crawled hourly. A documentation page that hasn't changed in 3 months gets re-crawled monthly.
>
> Use HTTP conditional requests: send `If-Modified-Since` and `If-None-Match` headers. If the server returns `304 Not Modified`, we skip re-downloading — saves bandwidth. Not all servers support this, but many do.
>
> Monitor XML sitemaps: many sites publish `<lastmod>` timestamps. We can check sitemaps periodically and prioritize re-crawling pages with updated `<lastmod>`.
>
> **Near-duplicate detection (SimHash):**
>
> Exact SHA-256 hash detects only byte-for-byte identical pages. But pages that differ only by ads, timestamps, or session IDs should also be detected as duplicates. SimHash (Charikar, 2002) computes a 64-bit fingerprint of page content. Two pages are near-duplicates if their SimHash values differ by ≤3 bits (Hamming distance ≤3). Google uses SimHash for web page deduplication at scale — their paper 'Detecting Near-Duplicates for Web Crawling' (Manku et al.) describes mapping 8 billion web pages to 64-bit fingerprints.
>
> **Contrast with Googlebot:** Google's re-crawl scheduler is deeply integrated with Search ranking. Pages that appear in more search results get higher re-crawl priority. Google allocates a 'crawl budget' per domain — defined as 'the set of URLs that Google can and wants to crawl,' determined by crawl capacity limit and crawl demand. Site owners can use Google Search Console to request indexing of specific URLs and see crawl statistics."

**Interviewer:**
Good. What's still missing for production?

**Candidate:**

> "| Problem | Impact |
> |---------|--------|
> | **No JavaScript rendering** | ~10-20% of pages have critical content behind JS (SPAs). We miss it entirely |
> | **No geographic distribution** | All crawlers in one region. Crawling a server in Tokyo from US-East adds 200ms latency per request |
> | **Exact dedup only** | SimHash helps, but our content store still has near-duplicate clusters we could consolidate |
> | **Manual scaling** | Adding/removing nodes requires manual reconfiguration and rebalancing |"

---

### Attempt 5: Production Hardening

> "```
>                  ┌───────────────────────────────────┐
>                  │         Kubernetes Cluster          │
>                  │         (Auto-scaling)              │
>                  └──────────────┬────────────────────┘
>                                 │
>          ┌──────────────────────┼──────────────────────┐
>          │                      │                       │
>     ┌────▼──────┐         ┌────▼──────┐          ┌────▼──────┐
>     │ US-East    │         │ EU-West    │          │ AP-East    │
>     │ Cluster    │         │ Cluster    │          │ Cluster    │
>     │            │         │            │          │            │
>     │ US domains │         │ EU domains │          │ Asia       │
>     │            │         │            │          │ domains    │
>     └────┬───────┘         └────┬───────┘          └────┬───────┘
>          │                      │                       │
>          └──────────────────────┼───────────────────────┘
>                                 │
>          ┌──────────────────────┼──────────────────────┐
>          │                      │                       │
>     ┌────▼──────┐         ┌────▼──────┐          ┌────▼──────┐
>     │ JS Render  │         │ Content    │          │ Data       │
>     │ Farm       │         │ Store      │          │ Pipeline   │
>     │ (Headless  │         │ (S3/HDFS)  │          │ → Search   │
>     │  Chromium) │         │            │          │   Index    │
>     └────────────┘         └────────────┘          │ → ML Data  │
>                                                     │ → Knowledge│
>                                                     │   Graph    │
>                                                     └────────────┘
> ```
>
> **JavaScript rendering:**
> Add a headless browser farm (Puppeteer/Playwright fleet). Not for every page — only for SPA-heavy sites where HTML parsing yields no content. Two-pass approach: index HTML first, then re-index after rendering. This mirrors Google's approach — Googlebot's WRS uses an 'evergreen' Chromium version and caches resources for up to 30 days. But WRS is 10-100x more resource-intensive than HTML-only fetching.
>
> **Geographic distribution:**
> Deploy crawler clusters in multiple regions. Assign domains to the nearest cluster by geography (US domains from US-East, EU from EU-West, Asia from AP-East). Lower latency = higher throughput + better politeness. Reduces cross-ocean bandwidth.
>
> **Auto-scaling:**
> Deploy on Kubernetes. Monitor crawl rate and queue depth. If queue grows faster than dequeue rate, auto-scale fetcher pods. If DNS resolution becomes a bottleneck, scale DNS resolver pods independently. Each component scales independently based on its bottleneck.
>
> **Data pipeline integration:**
> Feed crawled content to downstream consumers: search indexer, ML training data pipeline, knowledge graph extraction, analytics. The crawler itself doesn't build the search index — it feeds a data pipeline that does."

---

### Architecture Evolution Summary

| Aspect | Attempt 0 | Attempt 1 | Attempt 2 | Attempt 3 | Attempt 4 | Attempt 5 |
|---|---|---|---|---|---|---|
| **Throughput** | 1-5 pg/s | 100-250 pg/s | 1K-5K pg/s | 5K-50K pg/s | 5K-50K pg/s | 50K+ pg/s |
| **Concurrency** | 1 thread | 50 threads | Async I/O, 50K conns | Distributed async | Distributed async | Geo-distributed |
| **Frontier** | FIFO queue | Per-domain queues | Mercator (front+back) | Partitioned Mercator | + re-crawl scheduling | + crawl budget |
| **Politeness** | None | 1 req/s per domain | Heap-scheduled | Local per node | Adaptive | Adaptive + geo |
| **Dedup** | Hash set | Hash set | Hash set + content hash | RocksDB + Bloom filter | + SimHash | + near-dedup clusters |
| **Storage** | Local files | Local files | S3/HDFS (WARC) | S3/HDFS + Cassandra | + freshness metadata | + render cache |
| **Fault tolerance** | None | None | None | Heartbeats + ZK | + RocksDB WAL | + K8s self-healing |
| **Quality** | None | None | Content dedup | Content dedup | Traps, spam, soft 404 | + ML quality model |

---

### L5 vs L6 vs L7 — Phase 4 Rubric

| Aspect | L5 (SDE-2) | L6 (SDE-3) | L7 (Principal) |
|---|---|---|---|
| **Iterative design** | Jumps to distributed from the start | Starts simple, evolves through 3-4 attempts, each motivated by concrete problems | 5+ attempts, each with quantitative justification (throughput numbers, bottleneck analysis), explains why NOT each alternative |
| **Frontier design** | "A queue" | Mercator (front+back queues, min-heap), explains priority/politeness separation | Derives the Mercator design from first principles, discusses how Google's crawl budget allocation differs, mentions the original paper |
| **Partitioning** | "Shard the URLs" | Domain-based partitioning with politeness rationale, consistent hashing | Analyzes the power-law distribution of domain sizes, work-stealing for load balance, discusses per-IP politeness for shared hosting |
| **Dedup** | "Check if URL visited" | Hash set + Bloom filter trade-off, URL canonicalization rules | SimHash for near-duplicates (cites Charikar 2002, Google's Manku et al. paper), Counting Bloom filters for deletion, discusses false positive impact |
| **Real-world systems** | Doesn't reference | Contrasts Scrapy (single-machine), Nutch (Hadoop batch), Common Crawl (archival) | Explains why each system made different architectural choices based on their goals |

---

## PHASE 5: Deep Dive — URL Frontier (~8 min)

**Interviewer:**
Let's go deep on the URL frontier. You mentioned the Mercator design — walk me through the details.

**Candidate:**

> "The URL frontier is the heart of the crawler. Full deep dive is in [03-url-frontier.md](03-url-frontier.md), but let me cover the key design decisions.
>
> **The Mercator architecture has four components:**
>
> 1. **Front queues (priority):** N priority queues (e.g., 3-10). Each URL gets assigned to a queue based on its priority score. A prioritizer pulls from these queues in weighted order — higher priority queues get more pulls. Priority is based on: domain importance (historical PageRank), depth from seed (shallower = higher), and change frequency (for re-crawls).
>
> 2. **Router:** Maps URLs from front queues to the appropriate back queue by domain. Simple hash: `hash(domain) % num_back_queues`. One back queue per domain (or domain group for memory efficiency).
>
> 3. **Back queues (politeness):** One FIFO queue per domain. Each has a `next_allowed_crawl_time` field. The fetcher cannot dequeue from a back queue until its time arrives. After dequeue: `next_allowed_time = now + crawl_delay_for_domain`.
>
> 4. **Min-heap of ready times:** A min-heap of `(next_allowed_crawl_time, back_queue_id)` tuples. The fetcher pops the heap to find the next back queue that's ready. After dequeuing a URL, update the queue's time and push back into the heap. This is O(log n) per operation where n = number of active domains.
>
> **URL dedup — the space-accuracy trade-off:**
>
> | Approach | Memory (1B URLs) | False Positives | Supports Deletion |
> |---|---|---|---|
> | Hash Set (64-bit) | 8 GB | 0% | Yes |
> | Bloom Filter (10 bits/element) | ~1.2 GB | ~1% | No |
> | Counting Bloom (4 bytes/element) | ~4 GB | ~1% | Yes |
> | Distributed (Redis sharded) | 8 GB (across shards) | 0% | Yes |
>
> For 1B URLs, a hash set fits in memory (8 GB) on a single machine. For 10B+ URLs (web-scale), a Bloom filter at ~10 bits/element = ~12 GB is the pragmatic choice. The ~1% false positive rate means we miss ~1% of URLs — acceptable for a crawler. For a 1% false positive rate, you need about 9.6 bits per element (from the theoretical minimum: c = -1.44 × log2(ε)).
>
> **Why not Kafka for the frontier?** Kafka seems natural (distributed, persistent, partitioned). But Kafka's consumer model doesn't support the politeness constraint well — you can't peek at a message and say 'I'll consume this one later when the domain is ready.' Kafka is better as the URL routing layer between nodes (discovered URL → owning node) than as the frontier itself."

**Interviewer:**
How do you handle the case where a back queue empties but the domain has more URLs in the front queues?

**Candidate:**

> "When a back queue empties, it's temporarily removed from the heap. The router continues to map incoming URLs for that domain into the back queue. When the first URL arrives for an empty back queue, the queue is re-initialized with `next_allowed_crawl_time = now` (immediately ready) and pushed back into the heap. This lazy initialization avoids maintaining millions of empty queues for domains we haven't discovered URLs for yet."

---

### L5 vs L6 vs L7 — Phase 5 Rubric

| Aspect | L5 (SDE-2) | L6 (SDE-3) | L7 (Principal) |
|---|---|---|---|
| **Frontier design** | "Priority queue of URLs" | Full Mercator: front queues (priority) + back queues (politeness) + min-heap scheduling. Explains WHY separation | Derives optimal number of front/back queues, discusses frontier compression (URL prefix trees), disk-backed frontier scaling |
| **Dedup** | "Check a set" | Hash set vs Bloom filter trade-off with memory calculations, URL canonicalization rules | Optimal Bloom filter sizing (9.6 bits/element for 1% FP), Counting Bloom for deletion, partitioned dedup across nodes |
| **Kafka discussion** | Doesn't mention | Explains why Kafka doesn't work well for politeness (can't defer consumption) | Discusses Kafka as URL routing layer between nodes vs. frontier, exactly-once vs at-least-once delivery semantics |

---

## PHASE 6: Deep Dive — Fetcher & DNS (~8 min)

**Interviewer:**
Let's go deep on the fetcher. You said async I/O — walk me through how the fetch pipeline works.

**Candidate:**

> "Full deep dive in [04-fetcher-and-dns.md](04-fetcher-and-dns.md). The fetcher is the I/O-intensive component — it spends most of its time waiting on network responses.
>
> **Async I/O architecture:**
>
> Instead of one-thread-per-connection, we use non-blocking I/O (epoll on Linux, kqueue on macOS). A single thread manages an event loop that handles thousands of concurrent HTTP connections. When we initiate a fetch, the socket is registered with epoll. When data arrives, epoll notifies us, and we process the response. No thread blocking, no context-switching overhead.
>
> With async I/O, a single machine can maintain 50,000-100,000 simultaneous HTTP connections. Compare to threads: OS thread overhead (~1 MB stack each) limits practical concurrency to ~1,000-10,000 threads.
>
> **The fetch pipeline (stages):**
>
> ```
>   Frontier Dequeue → DNS Resolve → robots.txt Check → HTTP Connect
>        → Send Request → Receive Response → Parse → Store → Enqueue New URLs
> ```
>
> Each stage runs independently. URLs flow through a pipeline of async stages connected by queues. Stages can have different parallelism — e.g., more fetcher capacity than parser capacity, since fetching is I/O-bound and parsing is CPU-bound.
>
> **DNS resolution at scale:**
>
> The OS DNS resolver (glibc's `getaddrinfo()`) is synchronous, limited to ~100 concurrent lookups, and uses `/etc/resolv.conf`. Totally inadequate for a crawler doing thousands of lookups/sec. We need a custom async DNS resolver.
>
> Options:
> - **c-ares:** Async DNS library (used by Node.js, curl). Supports thousands of concurrent lookups
> - **Local caching resolver (unbound/dnsmasq):** Runs on each crawler node. Caches DNS results with TTL. Can handle 10,000+ queries/sec. Reduces dependence on upstream DNS servers
>
> For a crawler, DNS cache hit rate is >90% because we crawl many pages from the same domains repeatedly. We also do DNS prefetching: resolve the domain as soon as a URL is dequeued from the frontier, before the fetch starts.
>
> **Critical DNS insight — per-IP politeness:**
>
> Multiple domains can resolve to the same IP address (shared hosting). If `site-a.com`, `site-b.com`, and `site-c.com` all resolve to `1.2.3.4`, and we crawl each at 1 req/sec, we're actually hitting that server at 3 req/sec. Politeness should be enforced **per IP**, not just per domain. The DNS resolver feeds IP information to the politeness enforcer.
>
> **HTTP client behavior:**
> - Follow redirects (301/302/307/308) up to max chain length (5). Detect redirect loops
> - Set a proper `User-Agent`: `MyCrawler/1.0 (+http://our-site.com/crawler-info)` — identifies us and provides contact info
> - `HEAD` request first for unknown content types. Only fetch `text/html` (plus XML sitemaps, RSS). Skip images, videos, binaries
> - Timeouts: connection (5s), read (30s), total (60s). Abort slow downloads
> - Handle 4xx: log, don't retry. 5xx: retry with exponential backoff. 429 (Too Many Requests): back off immediately, respect `Retry-After` header
> - Handle content encoding: decompress gzip/deflate/brotli
> - Handle charset: detect from Content-Type header, `<meta charset>` tag, or BOM. Convert to UTF-8
> - Max download size: 10 MB. Abort if exceeded"

**Interviewer:**
What about very large pages or streaming responses? How do you protect the fetcher?

**Candidate:**

> "Three layers of protection:
>
> 1. **HEAD-first check:** For URLs with unknown content type, send a HEAD request first. Check `Content-Type` and `Content-Length`. If it's an image/video/binary, or >10 MB, skip without downloading the body
>
> 2. **Streaming abort:** Read the response body in chunks. Track bytes received. If we exceed 10 MB, abort the connection immediately. Don't buffer the entire response in memory
>
> 3. **Per-connection timeout:** 60-second total timeout per fetch. If a server trickles data slowly (slowloris-style), we abort and move on. This prevents a few slow connections from tying up fetcher resources"

---

### L5 vs L6 vs L7 — Phase 6 Rubric

| Aspect | L5 (SDE-2) | L6 (SDE-3) | L7 (Principal) |
|---|---|---|---|
| **Async I/O** | "Use threads" | Explains epoll/kqueue, why async scales to 50K connections while threads cap at ~1K-10K | Discusses connection pooling, HTTP keep-alive for same-domain fetches, epoll edge-triggered vs level-triggered |
| **DNS** | "Just resolve the domain" | Custom async resolver (c-ares), caching with TTL, prefetching. Per-IP politeness insight | DNS-based load balancing detection (CDN domains resolve to different IPs), discusses IPv4/IPv6 dual-stack, DNS-over-HTTPS implications |
| **Error handling** | "Retry on failure" | Distinguishes 4xx (don't retry) from 5xx (retry with backoff), 429 handling, Retry-After | Discusses circuit breaker pattern per domain (if 5 consecutive failures, back off for domain entirely), adaptive timeout based on domain latency profile |

---

## PHASE 7: Deep Dive — Content Processing & Storage (~5 min)

**Interviewer:**
Tell me about the content processing pipeline and storage strategy.

**Candidate:**

> "Full deep dive in [05-content-processing-and-extraction.md](05-content-processing-and-extraction.md).
>
> **Parsing pipeline (5 stages):**
>
> 1. **Decode:** Raw bytes → text using detected charset (UTF-8, Latin-1, etc.)
> 2. **Parse HTML:** Build DOM tree using a lenient parser (the web is full of broken HTML). jsoup (Java), lxml (Python), or golang.org/x/net/html
> 3. **Extract links:** All `<a href>`, `<link href>`, `<img src>`, `<script src>`, `<frame src>`. Resolve relative URLs against the page's base URL. Strip `javascript:`, `mailto:`, `data:` URIs. Respect `rel=\"nofollow\"`
> 4. **Extract metadata:** `<title>`, `<meta name=\"description\">`, `<meta name=\"robots\">` (noindex/nofollow), `<link rel=\"canonical\">`, `<html lang>`, structured data (JSON-LD, microdata)
> 5. **Extract text:** Strip HTML tags, extract visible text for content-based dedup and (if building a search engine) indexing
>
> **Non-HTML content:**
> - XML sitemaps: parse `<url>` elements with `<lastmod>`, `<changefreq>`, `<priority>`. Discover via robots.txt `Sitemap:` directive or by convention at `/sitemap.xml`
> - RSS/Atom feeds: extract entry URLs for fast content discovery
> - PDFs: extract text and links (Apache Tika or pdfminer)
>
> **Storage architecture:**
>
> | Data | Store | Format | Scale |
> |---|---|---|---|
> | Raw HTML | S3/HDFS | WARC (ISO 28500) | ~100 TB (1B pages × 100KB) |
> | Page metadata | Cassandra/HBase | URL → {hash, timestamp, status, redirects} | ~500 GB (1B × 500 bytes) |
> | Content dedup index | Local RocksDB or Redis | SHA-256 hash → URL list | ~32 GB (1B × 32 bytes) |
> | Extracted text | S3/HDFS | JSON or Parquet | ~20 TB (compressed) |
>
> **WARC format:** The ISO 28500 standard for web archiving. Each WARC record contains: record type (request/response/metadata), HTTP request/response headers, and the response body. Used by the Internet Archive, Common Crawl (stores ~2.5-3.5B pages per monthly crawl in 350-460 TiB of WARC data), and national libraries worldwide.
>
> **Content freshness tracking:**
> For each URL, store: last crawl time, content hash at last crawl, HTTP `Last-Modified` header, HTTP `ETag`. On re-crawl, send conditional headers. Track: has the hash changed? If yes, update change rate estimate."

---

### L5 vs L6 vs L7 — Phase 7 Rubric

| Aspect | L5 (SDE-2) | L6 (SDE-3) | L7 (Principal) |
|---|---|---|---|
| **Parsing** | "Extract links from HTML" | 5-stage pipeline, lenient parser choice, relative URL resolution, nofollow handling | Discusses DOM-based vs regex parsing trade-offs, handling HTML5 quirks mode, `<base>` tag resolution, meta refresh redirects |
| **Storage** | "Store pages in a database" | WARC format choice with rationale (standard, re-processable), S3 for content + Cassandra for metadata separation | Discusses compression strategies (gzip per-page vs page-level dictionary compression), storage tiering (hot/warm/cold), retention policies |
| **Dedup** | "Check URL" | URL dedup + content hash dedup + SimHash for near-duplicates | Content-defined chunking for partial dedup, canonical URL consolidation across domains, discusses MinHash + LSH for batch near-duplicate clustering |

---

## PHASE 8: Deep Dive — robots.txt & Crawl Ethics (~5 min)

**Interviewer:**
How do you handle robots.txt? And what about the ethical side of crawling?

**Candidate:**

> "Full deep dive in [06-robots-txt-and-crawl-ethics.md](06-robots-txt-and-crawl-ethics.md). This is non-negotiable — a crawler that ignores robots.txt is not a crawler, it's a scraper.
>
> **robots.txt handling:**
>
> - Fetch `https://domain.com/robots.txt` on first visit to any domain. Cache for 24 hours (or per HTTP cache headers)
> - Parse directives: `User-agent`, `Disallow`, `Allow`, `Crawl-delay`, `Sitemap`
> - **Matching rules:** Most specific rule wins. `Allow: /admin/public/` overrides `Disallow: /admin/`. Path matching uses longest match (Google's approach). Wildcard support: `*` matches any sequence, `$` anchors to end
> - **Error handling:** If robots.txt fetch returns 5xx → assume all URLs are **allowed** (be optimistic — server may be temporarily down). If 404 → no restrictions. If 403 → assume all URLs are **disallowed** (be conservative)
> - Google open-sourced their robots.txt parser in 2019 — it's a C++ library on GitHub (`google/robotstxt`), the reference implementation
>
> **Meta robots tags (per-page):**
> - `<meta name=\"robots\" content=\"noindex, nofollow\">` — we must download the page to see this tag, but then respect it: don't index, don't follow links
> - `X-Robots-Tag` HTTP header — same semantics but works for non-HTML (PDFs, images)
>
> **Ethical considerations:**
>
> 1. **Identify yourself:** Descriptive User-Agent with contact URL. Site owners should be able to reach us
> 2. **Respect all directives:** robots.txt, Crawl-delay, 429 responses, Retry-After headers
> 3. **Don't overwhelm:** Default 1 req/sec per domain. A small blog on shared hosting could be taken down by aggressive crawling. This is effectively a DoS attack
> 4. **Legal landscape:** Crawling public data is generally legal, but storing/redistributing copyrighted content may not be. GDPR/CCPA apply to personal data in crawled content. The legal landscape is evolving (LinkedIn v. hiQ Labs, Copiepresse v. Google)
> 5. **Opt-out mechanism:** Provide a way for site owners to request removal from crawl"

**Interviewer:**
What happens if a site has robots.txt but also requires authentication?

**Candidate:**

> "We only crawl publicly accessible content. If a page requires authentication (returns 401/403), we skip it. We never attempt to bypass authentication, submit forms, or use credentials. robots.txt governs what's publicly accessible. Authenticated content is outside our scope entirely."

---

## PHASE 9: Deep Dive — Distributed Architecture (~5 min)

**Interviewer:**
Let's talk about the distributed coordination in more detail.

**Candidate:**

> "Full deep dive in [07-distributed-crawling.md](07-distributed-crawling.md).
>
> **Why distribute?**
> The math: 1B pages / (7 days × 86,400 sec) ≈ 1,653 pages/sec minimum. A single machine at 500 pages/sec (conservative, accounting for politeness, DNS, parsing overhead) needs ~4-10 machines. For Google-scale (~400B documents, continuous re-crawl), you need thousands.
>
> **Coordination model:**
>
> We use a **decentralized model with a lightweight coordinator:**
> - **Each node is autonomous:** manages its own frontier partition, fetches its assigned domains, does local dedup
> - **ZooKeeper for coordination:** node health (heartbeats), domain-to-node mapping, leader election for rebalancing
> - **Cross-node URL routing:** when node A parses a page and discovers a URL for `example.com` (owned by node B), it sends the URL to node B via a message queue (Kafka works well here — partition by domain hash, each node consumes its partition)
>
> **Why not a centralized master?**
> A master that assigns every URL and tracks all state becomes a bottleneck at scale. At 5,000 URLs/sec, the master handles 5,000 dedup checks + 5,000 enqueue ops + 5,000 dequeue ops = 15,000 ops/sec. That's manageable, but it's a single point of failure and doesn't scale to Google's level. Our decentralized model scales horizontally.
>
> **Fault tolerance:**
> - Node failure detected by ZooKeeper (missed heartbeats, default ~30 sec timeout)
> - Failed node's domains reassigned to surviving nodes (consistent hashing makes this a small rebalance — only ~1/N of domains move)
> - Frontier state: if the failed node's SSD is accessible, recover from RocksDB. If not, re-discover URLs from the metadata store (Cassandra) or re-crawl from sitemaps
> - Content already written to S3 is durable — we never lose fetched content
> - **At-least-once semantics:** some URLs may be fetched twice during recovery. Content dedup at the storage layer handles this gracefully
>
> **Contrast with Nutch:** Apache Nutch distributes via Hadoop MapReduce. Each crawl cycle is a batch job: generate → fetch → parse → update. Hadoop handles distribution, fault tolerance, and data locality. But it's batch-oriented with high latency between cycles. Our design is real-time — URLs are crawled as soon as discovered. Nutch was historically derived from the same project that spawned Hadoop — Hadoop's HDFS and MapReduce were extracted from Nutch's codebase."

---

### L5 vs L6 vs L7 — Phase 9 Rubric

| Aspect | L5 (SDE-2) | L6 (SDE-3) | L7 (Principal) |
|---|---|---|---|
| **Distribution** | "Add more machines" | Domain-based partitioning with rationale, consistent hashing, ZooKeeper for coordination | Discusses rack-aware placement, cross-datacenter replication, split-brain scenarios, quorum-based leader election |
| **Fault tolerance** | "Restart failed nodes" | Heartbeat detection, domain reassignment, RocksDB recovery, at-least-once semantics | Discusses graceful degradation (shed low-priority domains under load), cascading failure prevention, chaos engineering for crawlers |
| **Real-world** | Doesn't reference | Contrasts Nutch (Hadoop batch) vs real-time approach, explains trade-offs | Explains how Google's Borg orchestration differs from Kubernetes, discusses Bigtable vs Cassandra for URL metadata at Google's scale |

---

## PHASE 10: What Keeps You Up at Night?

**Interviewer:**
Final question — if this system were in production, what keeps you up at night?

**Candidate:**

> "Three things:
>
> **1. Spider traps consuming crawl budget silently.**
> A single website generating infinite URLs (calendar with infinite next-month links, session IDs creating unique URLs for the same content) can consume our entire crawl budget for a domain — or worse, for a crawler node. Detection requires combining multiple signals: URL pattern analysis, content hash dedup, max depth/pages limits, and crawl rate monitoring. The tricky ones are subtle — they don't trigger any single alarm, but collectively waste resources. I'd want anomaly detection on per-domain URL discovery rates vs. unique content rates.
>
> **2. Getting IP-blocked by major content providers.**
> If CNN, Wikipedia, or GitHub blocks our IP ranges, we lose huge chunks of the web. This could happen silently — the site returns 200 with a 'blocked' page instead of the real content. Detection requires content quality monitoring: if a domain's pages suddenly all have the same content hash, we're probably being served a block page. Prevention: strict politeness, identify our crawler clearly, provide an abuse contact, and have multiple IP ranges across providers.
>
> **3. Data freshness drift with no external signal.**
> Some pages change without any signal — no `Last-Modified` update, no sitemap `<lastmod>` change, no RSS feed entry. The only way to detect the change is to re-crawl and compare content hashes. But we can't re-crawl everything frequently — that's too expensive. So we have a blind spot: pages that change silently between our re-crawl intervals. For critical pages, we can increase re-crawl frequency. But for the long tail of pages, some staleness is inevitable. The art is minimizing the freshness impact by allocating re-crawl budget to the pages that matter most (high traffic × high change rate)."

**Interviewer:**
Great design. You showed strong iterative thinking, good awareness of real-world crawler systems, and you understand the core tension between coverage, freshness, and politeness. Strong L6. Thanks for your time.

---

## Supporting Deep-Dive Documents

| Doc | Topic | Link |
|---|---|---|
| 02 | API Contracts | [02-api-contracts.md](02-api-contracts.md) |
| 03 | URL Frontier & Scheduling | [03-url-frontier.md](03-url-frontier.md) |
| 04 | Fetcher, DNS & Network | [04-fetcher-and-dns.md](04-fetcher-and-dns.md) |
| 05 | Content Processing & Storage | [05-content-processing-and-extraction.md](05-content-processing-and-extraction.md) |
| 06 | robots.txt & Ethics | [06-robots-txt-and-crawl-ethics.md](06-robots-txt-and-crawl-ethics.md) |
| 07 | Distributed Crawling | [07-distributed-crawling.md](07-distributed-crawling.md) |
| 08 | Re-crawl Scheduling & Freshness | [08-crawl-scheduling-and-freshness.md](08-crawl-scheduling-and-freshness.md) |
| 09 | Spider Traps & Quality | [09-spider-traps-and-quality.md](09-spider-traps-and-quality.md) |
| 10 | Scaling & Performance | [10-scaling-and-performance.md](10-scaling-and-performance.md) |
| 11 | Design Trade-offs | [11-design-trade-offs.md](11-design-trade-offs.md) |

---

## Verified Sources

- [Google Crawl Budget Management](https://developers.google.com/search/docs/crawling-indexing/large-site-managing-crawl-budget) — crawl capacity limit, crawl demand definition
- [Google JavaScript SEO Basics](https://developers.google.com/search/docs/crawling-indexing/javascript/javascript-seo-basics) — WRS, evergreen Chromium, three-phase processing
- [Google's robots.txt parser (open-sourced 2019)](https://github.com/google/robotstxt) — C++ library, reference implementation
- [Google's robots.txt parser announcement](https://developers.google.com/search/blog/2019/07/repp-oss)
- [Google Index Size (~400B documents)](https://zyppy.com/seo/google-index-size/) — testimony during Google antitrust trial
- [Google Search: Organizing Information](https://www.google.com/intl/en_us/search/howsearchworks/how-search-works/organizing-information/) — "hundreds of billions of webpages"
- [Common Crawl Statistics](https://commoncrawl.github.io/cc-crawl-statistics/) — crawl size metrics per month
- [Common Crawl Latest Crawl](https://commoncrawl.org/latest-crawl) — ~2.5-3.5B pages per crawl, 350-460 TiB
- [Mercator Paper (Heydon & Najork)](https://courses.cs.washington.edu/courses/cse454/15wi/papers/mercator.pdf) — scalable web crawler architecture
- [SimHash / Near-Duplicate Detection (Manku et al., Google)](https://research.google.com/pubs/archive/33026.pdf) — 8B pages, 64-bit fingerprints, ≤3 bit Hamming distance
- [Bloom Filter (Wikipedia)](https://en.wikipedia.org/wiki/Bloom_filter) — 9.6 bits/element for 1% FP rate
- [WARC Format (ISO 28500)](https://iipc.github.io/warc-specifications/specifications/warc-format/warc-1.1/) — web archiving standard
- [Apache Nutch](https://en.wikipedia.org/wiki/Apache_Nutch) — Hadoop-based, generate→fetch→parse→update cycle
- [Scrapy Architecture](https://docs.scrapy.org/en/latest/topics/architecture.html) — Twisted reactor, middleware pipeline
- [Cloudflare: Who's Crawling Your Site in 2025](https://blog.cloudflare.com/from-googlebot-to-gptbot-whos-crawling-your-site-in-2025/) — Googlebot activity growth
