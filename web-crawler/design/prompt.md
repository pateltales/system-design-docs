Design a Web Crawler (like Googlebot) as a system design interview simulation.

## Template
Follow the EXACT same format as the Netflix interview simulation at:
src/hld/netflix/design/prompt.md

Same structure:
- Interviewer/Candidate dialogue format
- PHASE 1: Opening & Problem Statement
- PHASE 2: Requirements Gathering & Scoping (functional + non-functional + scale numbers)
- PHASE 3: API Design
- PHASE 4: High-Level Architecture (iterative build-up: Attempt 0 → 1 → 2 → ..., starting from a single-threaded crawler, finding problems, evolving)
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
Create all files under: src/hld/web-crawler/design/

## Files to create

### 1. 01-interview-simulation.md — the main backbone
The full interview dialogue covering all phases and attempts.

### 2. 02-api-contracts.md — Crawler System APIs

This doc should list all the major API surfaces of a web crawling system — both the **internal APIs** (between crawler components) and the **external/management APIs** (for operators to control the crawl).

**Structure**: For each API group, list every endpoint/interface with method, path, request/response shape, and a brief description.

**API groups to cover**:

- **Crawl Management APIs**: `POST /crawl/jobs` (submit a new crawl job: seed URLs, depth limit, domain whitelist/blacklist, crawl rate, priority), `GET /crawl/jobs/{jobId}` (job status: running, paused, completed, URLs discovered/fetched/failed), `PUT /crawl/jobs/{jobId}/pause` (pause a crawl), `PUT /crawl/jobs/{jobId}/resume` (resume), `DELETE /crawl/jobs/{jobId}` (cancel). Configuration: max pages per domain, crawl delay, user-agent string, HTTP timeout, retry policy.

- **URL Frontier APIs** (internal): `POST /frontier/enqueue` (add URL to crawl queue with priority and metadata: depth, parent URL, discovered timestamp), `GET /frontier/dequeue` (get next URL to crawl — must respect politeness: don't return a URL if the domain was crawled recently), `POST /frontier/mark-complete` (mark URL as crawled with result: success/failure/redirect/robots-blocked), `GET /frontier/stats` (queue depth, domains in queue, crawl rate per domain). The frontier is the most critical data structure — it determines WHAT to crawl next.

- **Fetcher APIs** (internal): `POST /fetch` (download a URL: HTTP GET with configurable timeout, user-agent, cookies, redirect following, HEAD request for content-type check before full GET). Response includes: HTTP status, headers, body (raw bytes), response time, final URL (after redirects), content-type, content-length. Must handle: HTTP/HTTPS, redirects (301/302/307), timeouts, connection refused, DNS resolution failure, SSL errors.

- **Parser / Extractor APIs** (internal): `POST /parse` (input: raw HTML/content + URL; output: extracted data). Extractions: outgoing links (absolute URLs, resolved against base URL), page title, meta description, canonical URL, robots meta tags (`noindex`, `nofollow`), structured data (JSON-LD, microdata), language, charset. For non-HTML content: extract links from XML sitemaps, RSS feeds, PDF links.

- **Content Store APIs** (internal): `POST /content/store` (store crawled page content: URL, content hash, raw HTML, extracted text, metadata, crawl timestamp), `GET /content/{urlHash}` (retrieve stored content by URL hash), `HEAD /content/{urlHash}` (check if content exists — for deduplication). Content-addressed storage: pages stored by content hash (SHA-256) to deduplicate identical pages at different URLs.

- **robots.txt Management APIs** (internal): `GET /robots/{domain}` (get parsed robots.txt for a domain), `POST /robots/refresh/{domain}` (force re-fetch of a domain's robots.txt). robots.txt parsing: `User-agent`, `Disallow`, `Allow`, `Crawl-delay`, `Sitemap`. Cache robots.txt with TTL (typically 24 hours). Must respect robots.txt — crawling a disallowed path violates the web standard and can get the crawler's IP blocked.

- **DNS Resolution APIs** (internal): `POST /dns/resolve` (resolve hostname to IP address(es)). Caching: DNS results cached with TTL from the DNS record. Custom DNS resolver (don't rely on OS resolver for millions of lookups/sec). Must handle: CNAME chains, IPv4/IPv6, DNS failures, stale cache entries.

- **Monitoring / Metrics APIs**: `GET /metrics` (crawl rate per second, pages fetched, errors by type, queue depth, content store size, DNS cache hit rate, robots.txt cache hit rate, duplicate page rate), `GET /health` (component health: frontier, fetcher workers, parser workers, content store, DNS resolver).

**Contrast with Googlebot**: Googlebot is the world's largest web crawler, crawling hundreds of billions of pages. It uses a distributed architecture with thousands of machines, has deep integration with Google's indexing and ranking pipeline, renders JavaScript (headless Chrome via Web Rendering Service — WRS), and re-crawls pages at different frequencies based on change rate. A typical interview crawler is a simplified version — no JS rendering, no ranking, no search index.

**Contrast with Common Crawl**: Common Crawl is an open-source web archive that crawls billions of pages and stores them in WARC format on S3. It runs monthly crawls. It's breadth-first (maximize coverage) not depth-first. It stores raw HTML (no rendering). Used primarily for research and ML training data, not real-time search.

**Interview subset**: In the interview (Phase 3), focus on: URL frontier (the core scheduling problem), fetcher (the I/O-intensive component), and crawl management (how operators control the crawl). The full API list lives in this doc.

### 3. 03-url-frontier.md — URL Frontier & Crawl Scheduling

The URL frontier is the heart of the crawler — it determines what to crawl next. This is the most important deep dive.

- **What is the URL frontier?**: A priority queue of URLs to be crawled. But not a simple queue — it must enforce:
  1. **Politeness**: Don't overwhelm any single domain. Space out requests to the same domain (e.g., 1 request per second per domain).
  2. **Priority**: More important pages should be crawled first. Priority based on: PageRank, page change frequency, page freshness, depth from seed.
  3. **Deduplication**: Don't enqueue URLs that have already been crawled or are already in the queue.
  4. **Freshness**: Re-crawl pages that have changed. Pages that change frequently should be re-crawled more often.
- **Mercator-style frontier architecture** (the canonical design from the Mercator web crawler paper):
  - **Front queues (priority queues)**: N priority queues, one per priority level. URLs are assigned to a priority queue based on their priority score. A prioritizer pulls from front queues in weighted order (higher priority queues get more pulls).
  - **Back queues (politeness queues)**: One queue per domain (or per IP). Each back queue maps to exactly one domain. A router maps URLs from the front queues to the appropriate back queue by domain. Each back queue has a "next allowed crawl time" — the fetcher cannot dequeue from a back queue until that time arrives (enforces crawl delay).
  - **Heap of back queue ready times**: A min-heap of (next_allowed_crawl_time, queue_id). The fetcher pops the heap to find the next queue that's ready to serve a URL. After dequeuing a URL, update the queue's next allowed time (current_time + crawl_delay) and push back into the heap.
  - This design cleanly separates priority (front queues) from politeness (back queues). Without this separation, a high-priority page on a polite domain could starve low-priority pages on fast domains.
- **URL deduplication**:
  - **Exact URL dedup**: Use a hash set (or Bloom filter) of seen URLs. Before enqueuing, check if the URL (after canonicalization) has been seen. Hash set is exact but memory-intensive for billions of URLs. Bloom filter is space-efficient (~10 bits per URL) but has false positives (URL incorrectly marked as seen → missed page).
  - **URL canonicalization**: Normalize URLs before dedup: lowercase scheme and host, remove default port, sort query parameters, remove fragment (#), resolve relative paths, remove trailing slash, handle www vs non-www. Example: `HTTP://WWW.Example.Com:80/path/../page?b=2&a=1#top` → `http://www.example.com/page?a=1&b=2`.
  - **Content-based dedup**: Even different URLs can serve identical content (mirrors, URL parameters that don't change content). Hash the page content (SHA-256 or simhash for near-duplicate detection). Store content hashes in the content store. If a newly fetched page has the same content hash as a previously stored page → mark as duplicate.
  - **Near-duplicate detection (SimHash / MinHash)**: Exact hash detects only byte-for-byte identical pages. Pages that differ by ads, timestamps, or session IDs will have different hashes but same content. **SimHash** (Charikar, 2002): compute a 64-bit fingerprint of the page's content. Two pages are near-duplicates if their SimHash values differ by ≤3 bits (Hamming distance). Google uses SimHash for web page deduplication at scale. **MinHash + LSH (Locality-Sensitive Hashing)**: estimate Jaccard similarity between sets of shingles (n-grams). Faster for batch comparison but more complex to implement.
- **Priority scoring**:
  - **Static priority**: Based on domain importance (e.g., wikipedia.org gets higher priority than a random blog). Can use historical PageRank as a proxy.
  - **Dynamic priority**: Based on page change frequency. Pages that change often should be re-crawled sooner. Estimate change rate from past crawls (e.g., this page changed in 3 out of the last 5 crawls → high change rate → high priority).
  - **Depth-based priority**: Pages closer to seed URLs (lower crawl depth) get higher priority. Breadth-first exploration.
  - **Freshness-based priority**: Pages not crawled recently get higher priority. Combine with change rate: a page that changes hourly and hasn't been crawled in 2 hours gets top priority.
- **Frontier persistence**: The frontier must survive crashes. Options:
  - **In-memory with WAL (Write-Ahead Log)**: Fast but limited by memory. WAL provides crash recovery. Suitable for smaller crawls.
  - **Disk-backed queue**: Use RocksDB, LevelDB, or BerkeleyDB as the backing store. Handles billions of URLs. Slower than in-memory but practically unlimited capacity.
  - **Distributed queue (Kafka)**: URLs published to Kafka topics. Each partition maps to a domain (or domain group). Consumers (fetcher workers) consume from partitions. Provides persistence, scalability, and replay capability. But: Kafka's ordering guarantees are per-partition, which works well for domain-based politeness.
- **Contrast with Googlebot**: Google's frontier is massively distributed across thousands of machines. Priority is deeply integrated with the search ranking pipeline — PageRank, crawl budget allocation per domain, change detection signals from Google Search Console. Googlebot's frontier must handle hundreds of billions of known URLs. The interview version is a simplified single-machine or small-cluster frontier.
- **Contrast with Scrapy**: Scrapy (Python framework) uses a simple FIFO or priority queue as the frontier. No built-in politeness enforcement (relies on `DOWNLOAD_DELAY` setting). No distributed frontier — single-process only (though Scrapy-Redis adds distributed support). Suitable for small-to-medium crawls, not web-scale.

### 4. 04-fetcher-and-dns.md — Fetcher, DNS Resolution & Network

The fetcher is the I/O-heavy component that downloads web pages.

- **Fetcher architecture**:
  - **Multi-threaded fetcher**: Each thread fetches one URL at a time. Simple but limited — OS thread overhead limits concurrency to ~1,000-10,000 threads. Each thread blocks on network I/O.
  - **Async I/O fetcher**: Use non-blocking I/O (epoll/kqueue, asyncio, Netty). A single thread manages thousands of concurrent HTTP connections. Much higher concurrency — 50,000-100,000 simultaneous connections per machine. This is the preferred approach for production crawlers.
  - **Fetcher pool**: Multiple fetcher machines, each handling a subset of domains. Domain-to-fetcher assignment is consistent (hash domain → fetcher), so politeness tracking is local to each fetcher.
- **HTTP client behavior**:
  - Follow redirects (301, 302, 307, 308) up to a maximum chain length (e.g., 5). Record the redirect chain in metadata. Detect redirect loops.
  - Set a proper `User-Agent` header identifying the crawler (e.g., `MyCrawler/1.0 (+http://example.com/crawler-info)`). Some sites block crawlers with generic or missing user-agents.
  - Respect `Content-Type`: only parse HTML (`text/html`). Skip images, videos, binaries. Optionally parse XML (sitemaps, RSS), JSON, PDF. Use `HEAD` request first to check content-type before downloading large files.
  - Handle `Content-Encoding`: decompress gzip/deflate/brotli responses.
  - Handle character encoding: detect charset from HTTP `Content-Type` header, HTML `<meta charset>` tag, or BOM. Convert to UTF-8 for consistent storage.
  - Set timeouts: connection timeout (5 seconds), read timeout (30 seconds), total timeout (60 seconds). Abort slow downloads to avoid tying up fetcher resources.
  - Handle HTTP errors: 4xx (client error — log and don't retry), 5xx (server error — retry with backoff), timeout (retry), connection refused (retry later).
- **DNS resolution at scale**:
  - OS DNS resolver is too slow for millions of lookups/sec (uses `/etc/resolv.conf`, synchronous, limited concurrency). Need a dedicated async DNS resolver.
  - **DNS caching**: Cache DNS results in-memory with TTL from the DNS record. For a crawler, most domains are crawled repeatedly, so DNS cache hit rate is >90%.
  - **DNS prefetching**: When a URL is dequeued from the frontier, resolve its DNS immediately (before the fetch starts). By the time the fetcher is ready to connect, the DNS result is cached.
  - **Custom DNS resolver**: Use a library like c-ares (async DNS), or run a local caching resolver like unbound or dnsmasq. Reduces dependence on external DNS servers.
  - **DNS-based politeness**: Map domain → IP. If multiple domains resolve to the same IP (shared hosting), politeness should be enforced per IP, not per domain. Otherwise, 10 domains on the same server each crawled at 1 req/sec = 10 req/sec to one server.
- **Politeness enforcement in the fetcher**:
  - Respect `robots.txt` `Crawl-delay` directive (e.g., `Crawl-delay: 10` means wait 10 seconds between requests to this domain).
  - Default politeness: 1 request per second per domain if no `Crawl-delay` specified.
  - Adaptive politeness: if the server responds slowly (high latency) or returns 429/503, increase the delay. If the server is fast, cautiously decrease the delay.
- **Handling traps and edge cases**:
  - **Spider traps**: Websites that generate infinite URLs (e.g., calendars with infinite next-month links, session IDs in URLs, relative links that create infinite depth). Mitigations: max depth limit, max pages per domain, detect URL patterns that grow without bound, content hash dedup.
  - **Soft 404s**: Pages that return HTTP 200 but are actually "Page Not Found" pages. Detect by: checking for known "not found" text patterns, comparing content similarity to a known 404 page on the domain.
  - **Dynamic content / JavaScript rendering**: Many modern websites load content via JavaScript. A basic fetcher (HTTP GET + HTML parse) misses this content. Solution: headless browser rendering (Puppeteer/Playwright/Chrome Headless). Trade-off: 10-100x slower and more resource-intensive than plain HTTP fetch. Googlebot uses WRS (Web Rendering Service) — a headless Chrome fleet — for JS rendering.
  - **Very large pages**: Set a max download size (e.g., 10 MB). Abort download if exceeded. Prevents memory issues from massive pages.
  - **Rate limiting by the target server**: Detect 429 (Too Many Requests) and `Retry-After` header. Back off immediately. Add the domain to a "slow down" list.
- **Contrast with Googlebot**: Googlebot uses a distributed fetcher fleet across multiple data centers. It renders JavaScript via WRS (headless Chrome). It uses Google's internal DNS infrastructure (not public DNS). It has sophisticated bot detection avoidance (but also gets blocked by many sites). The interview version uses a simpler async HTTP fetcher without JS rendering.
- **Contrast with Scrapy**: Scrapy uses Twisted (async reactor pattern) for HTTP fetching. Single-process, single-machine. Middleware pipeline for processing requests/responses. Built-in redirect handling, retry, user-agent rotation. Limited to ~100-500 concurrent requests per process. Not designed for web-scale crawling.

### 5. 05-content-processing-and-extraction.md — Parsing, Extraction & Storage

- **HTML parsing pipeline**:
  1. **Decode**: Convert raw bytes to text using detected character encoding (UTF-8, Latin-1, etc.).
  2. **Parse HTML**: Build a DOM tree from the HTML. Use a lenient parser that handles malformed HTML (the web is full of broken HTML). Libraries: jsoup (Java), BeautifulSoup/lxml (Python), golang.org/x/net/html (Go).
  3. **Extract links**: Find all `<a href>`, `<link href>`, `<img src>`, `<script src>`, `<frame src>` tags. Resolve relative URLs against the page's base URL. Filter: remove javascript: links, mailto: links, data: URIs. Respect `rel="nofollow"` (don't follow these links for crawling/ranking purposes).
  4. **Extract metadata**: `<title>`, `<meta name="description">`, `<meta name="robots">` (noindex, nofollow), `<link rel="canonical">` (canonical URL — the preferred URL for this content), `<html lang>` (language), structured data (JSON-LD, microdata, RDFa).
  5. **Extract text**: Strip HTML tags, extract visible text content. Used for content-based deduplication and (if building a search engine) indexing.
- **Non-HTML content handling**:
  - **XML sitemaps**: Parse `<sitemap>` and `<url>` elements. Extract URLs with `<lastmod>`, `<changefreq>`, `<priority>` hints. Sitemap index files contain links to other sitemaps. Discover sitemaps via `robots.txt` (`Sitemap: https://example.com/sitemap.xml`) or by convention (`/sitemap.xml`).
  - **RSS/Atom feeds**: Extract entry URLs. Useful for discovering new content quickly.
  - **PDF documents**: Extract text and links from PDF files (using libraries like Apache Tika or pdfminer). PDFs can contain outgoing links.
  - **Images / media**: Don't parse for links, but record metadata (URL, content-type, size). Useful if building an image search index.
- **Content storage**:
  - **Raw HTML storage**: Store the raw HTML of every crawled page. Useful for re-processing (re-parse, re-extract) without re-crawling. Storage format: WARC (Web ARChive) — the standard format used by the Internet Archive and Common Crawl. A WARC file contains the HTTP request, response headers, and response body for each crawled page.
  - **Content hash dedup**: Compute SHA-256 of the page body. Store content by hash (content-addressable storage). If two URLs serve the same content, store once.
  - **Storage backend**: Object storage (S3, GCS, HDFS) for raw content. Key-value store (HBase, Cassandra) for metadata (URL → crawl timestamp, content hash, status code, redirect target). The metadata store must support: fast lookup by URL, range scans for re-crawl scheduling, bulk writes for crawl results.
  - **Storage scale**: The web has ~5.5 billion indexed pages (Google). Each page averages ~50-100 KB. Total: ~275 TB - 550 TB of raw HTML for a full web crawl. Common Crawl stores ~3.5 billion pages per monthly crawl in ~350 TB of WARC files.
- **Content freshness tracking**:
  - Record: URL, last crawl time, content hash at last crawl, HTTP `Last-Modified` header, HTTP `ETag` header.
  - On re-crawl: send `If-Modified-Since` and `If-None-Match` headers. If server returns 304 Not Modified → content hasn't changed, no need to re-download. Saves bandwidth.
  - Track change frequency: how often does the content hash change between crawls? Use this to schedule future re-crawl priority.
- **Contrast with Google's indexing pipeline**: Google's content processing goes far beyond parsing — it includes rendering JavaScript (WRS), building an inverted index (for search), computing PageRank, extracting entities, detecting spam/quality, and feeding data into the Knowledge Graph. The interview crawler focuses only on parsing and storage.
- **Contrast with Common Crawl**: Common Crawl stores pages in WARC format on S3, publicly accessible. It crawls ~3.5 billion pages per monthly crawl. It doesn't build a search index — it's a raw data archive for research. No deduplication across crawls (each crawl is independent).

### 6. 06-robots-txt-and-crawl-ethics.md — robots.txt, Politeness & Ethics

- **robots.txt standard (Robots Exclusion Protocol)**:
  - Location: `https://domain.com/robots.txt`. Fetched once per domain (cached for 24 hours typically).
  - Directives: `User-agent: *` (applies to all crawlers), `User-agent: Googlebot` (applies to Googlebot only), `Disallow: /admin/` (don't crawl /admin/ and below), `Allow: /admin/public/` (exception to a broader Disallow), `Crawl-delay: 5` (wait 5 seconds between requests — not part of the original standard but widely supported), `Sitemap: https://domain.com/sitemap.xml` (location of XML sitemap).
  - **Matching rules**: More specific rules take priority. `Allow` overrides `Disallow` when both match (in most implementations — Google's implementation uses longest match wins). Path matching uses prefix matching (`Disallow: /foo` matches `/foo`, `/foobar`, `/foo/bar`). Wildcard support: `*` matches any sequence, `$` anchors to end of URL (Google extension).
  - **robots.txt caching**: Fetch on first visit to a domain. Cache for 24 hours (or as specified by HTTP cache headers). If robots.txt fetch fails with 5xx → assume all URLs are allowed (be optimistic). If 404 → no restrictions. If 403 → assume all URLs are disallowed (be conservative).
  - **Google's robots.txt parser**: Google open-sourced their robots.txt parser (C++ library, 2019). It's the reference implementation. Handles edge cases that simpler parsers get wrong.
- **Meta robots tags**: Per-page directives in the HTML `<meta name="robots" content="noindex, nofollow">`. `noindex` = don't add this page to the search index. `nofollow` = don't follow links on this page. `noarchive` = don't show a cached version. These are advisory — the crawler should respect them but they don't prevent crawling (the page is still downloaded to read the meta tag).
- **HTTP headers for crawl control**: `X-Robots-Tag` HTTP header — same semantics as meta robots tag but works for non-HTML content (PDFs, images). `Retry-After` header — tells the crawler when to retry (after server error or rate limiting).
- **Crawl politeness best practices**:
  - Identify your crawler with a descriptive `User-Agent` string and a contact page URL.
  - Respect `robots.txt` completely.
  - Respect `Crawl-delay` if specified.
  - Default crawl delay: 1 request/second per domain (conservative but universally safe).
  - Don't crawl during peak hours if possible (crawl at night in the target's timezone).
  - Provide a way for site owners to contact you about crawl issues (link in User-Agent).
  - Honor `429 Too Many Requests` and back off immediately.
- **Ethical considerations**:
  - **Copyright and content ownership**: Crawling is generally legal (public information), but storing/redistributing copyrighted content may not be. The Copiepresse case (Google), LinkedIn v. hiQ Labs (data scraping). Legal landscape is evolving.
  - **Personal data**: Crawling pages with personal information (names, emails, addresses) raises GDPR/CCPA concerns. Don't crawl and store PII unless you have a legal basis.
  - **Denial of Service**: An aggressive crawler can overwhelm a small website. Politeness enforcement is both ethical and practical — getting IP-blocked is bad for the crawler too.
  - **Opt-out mechanisms**: Respect robots.txt, provide a way for sites to request removal from the crawl.
- **Contrast with Googlebot**: Googlebot respects robots.txt (it helped create the standard). Google provides Search Console for site owners to control crawl rate, see crawl statistics, and request re-crawling. Google allocates a "crawl budget" per domain — larger, more important sites get more crawl resources. Google's crawler is so well-behaved that most site operators welcome it (it drives search traffic to their site).
- **Contrast with aggressive scrapers / bad actors**: Some scrapers ignore robots.txt, use no crawl delay, rotate user-agents to avoid detection, and overwhelm target sites. This is unethical and often illegal. A well-designed crawler should be a good citizen of the web.

### 7. 07-distributed-crawling.md — Distributed Architecture & Coordination

- **Why distribute the crawler?**:
  - The web has ~5.5 billion indexed pages. A single machine crawling at 100 pages/sec = 55 million seconds ≈ **1.7 years** to crawl the web once. To crawl the web in 1 week: need ~9,000 pages/sec → multiple machines.
  - Network bandwidth: each page ~100 KB → at 1,000 pages/sec = 100 MB/sec = 800 Mbps. A single machine's NIC can handle this, but DNS, parsing, and storage become bottlenecks.
  - Geographic distribution: crawl US sites from US data centers, European sites from Europe (lower latency, better performance).
- **Partitioning strategy — how to divide work across crawler nodes**:
  - **URL hash partitioning**: Hash(URL) % N → assign to node. Simple, uniform distribution. But: no locality (same domain's URLs scattered across all nodes → politeness enforcement is harder).
  - **Domain-based partitioning**: Hash(domain) % N → assign all URLs of a domain to the same node. Politeness is naturally local (each node manages its own domains). But: uneven distribution (popular domains have millions of URLs, unpopular domains have few). Mitigate with consistent hashing + virtual nodes for rebalancing.
  - **Hybrid**: Domain-based partitioning for politeness, with work-stealing for load balancing. If node A is idle and node B is overloaded, node A can steal work from node B's domain queue (but must coordinate politeness).
- **Coordination between nodes**:
  - **Centralized coordinator**: A master node assigns URL batches to workers, tracks which URLs have been crawled, handles dedup. Simple but the master is a single point of failure and a bottleneck. Suitable for small-to-medium crawls.
  - **Decentralized / masterless**: Each node manages its own frontier partition. URL dedup is partitioned (each node deduplicates within its domain partition). Discovered URLs are routed to the responsible node (by domain hash). No single point of failure. More complex but scales better.
  - **Message queue-based**: URLs are published to a distributed queue (Kafka, RabbitMQ). Each partition corresponds to a domain group. Consumer nodes pull URLs from their assigned partitions. Kafka provides persistence, scalability, and consumer group coordination.
- **URL dedup at scale**:
  - **Centralized dedup (Redis/Memcached set)**: Fast but limited by memory. 5 billion URLs × 8 bytes (hash) = 40 GB — fits in a single large Redis instance but just barely. Not scalable beyond one machine.
  - **Distributed Bloom filter**: Each node maintains a Bloom filter for its domain partition. ~10 bits per URL → 5 billion URLs = ~6 GB. False positive rate ~1% (some URLs will be skipped — acceptable for most crawls). Cannot remove URLs (standard Bloom filter). Use Counting Bloom Filter for removal support.
  - **Distributed hash table**: Partition the URL set across multiple nodes (each node holds URL hashes for its assigned key range). Exact dedup with scalable memory. Implemented via sharded Redis, Cassandra, or custom DHT.
  - **Checkpoint and persist**: Periodically checkpoint the URL dedup set to disk. On crash recovery, reload from checkpoint. Combined with WAL for recent URLs.
- **Crawl state management**:
  - Each URL has a state: discovered → queued → fetching → fetched → parsed → stored. State transitions must be tracked reliably.
  - Use a state machine per URL. Store state in a fast KV store (RocksDB local, or Cassandra distributed).
  - Handle retries: if a URL fetch fails, transition back to queued with a retry count and backoff delay.
  - Handle timeouts: if a URL has been in "fetching" state for too long (heartbeat timeout), assume the fetcher crashed and re-enqueue.
- **Fault tolerance**:
  - **Node failure**: If a crawler node dies, its domains are orphaned. Detection: heartbeat mechanism (nodes send heartbeats to a coordinator or to peers). Recovery: reassign orphaned domains to surviving nodes (rebalance). The frontier partition must be durable (on disk, not just in memory) so that URLs are not lost.
  - **Data loss prevention**: Write crawl results to durable storage (S3/HDFS) before marking the URL as complete. If the node crashes after fetching but before writing → the URL is re-fetched on recovery (idempotent, not a problem).
  - **Exactly-once semantics**: Hard to achieve in a distributed crawler. Instead, aim for at-least-once (some URLs may be crawled twice). Dedup at the content storage layer handles duplicate fetches.
- **Contrast with Googlebot**: Google's web crawler is a massively distributed system running on thousands of machines across multiple data centers. It's deeply integrated with Google's infrastructure (Bigtable for URL storage, MapReduce/Flume for batch processing, Borg for scheduling). The scale is orders of magnitude larger than an interview-scope crawler.
- **Contrast with Apache Nutch**: Nutch is an open-source web crawler built on Hadoop (MapReduce). It runs crawl cycles as MapReduce jobs: generate (select URLs from frontier) → fetch → parse → update (add new URLs to frontier). Batch-oriented, not real-time. Scales well via Hadoop's distributed computing but has high latency between crawl cycles (minutes to hours).

### 8. 08-crawl-scheduling-and-freshness.md — Re-crawl Scheduling & Freshness

- **The freshness problem**: The web changes constantly. Pages are added, modified, and deleted. A search engine needs fresh content — stale search results are a poor user experience. But re-crawling every page every day is infeasible (too many pages, too much bandwidth, too much server load). The crawler must decide: which pages to re-crawl, and how often.
- **Crawl budget**: Each domain gets a "crawl budget" — the maximum number of pages the crawler will fetch from that domain in a given time period. Determined by: domain importance (PageRank), server capacity (how fast can the server respond without being overwhelmed), content change rate. Google uses crawl budget allocation: more budget for important, fast, frequently-changing sites.
- **Change detection strategies**:
  - **Periodic re-crawl**: Re-crawl every page at a fixed interval (e.g., every 7 days). Simple but wasteful — many pages don't change that often.
  - **Adaptive re-crawl**: Estimate each page's change rate from historical data. Pages that changed frequently in the past get re-crawled more often. Poisson process model: if a page has changed k times in n crawls, estimate change rate λ = k/n. Schedule next crawl at t = 1/λ. Converges to optimal re-crawl frequency over time.
  - **HTTP conditional requests**: Use `If-Modified-Since` / `If-None-Match` headers. Server returns `304 Not Modified` if unchanged. Saves bandwidth (no body transfer) but still requires an HTTP round-trip per page.
  - **Sitemap-based freshness**: XML sitemaps include `<lastmod>` timestamps. Monitor sitemaps for changes. Re-crawl only pages with updated `<lastmod>`. Efficient but relies on site operators maintaining accurate sitemaps (many don't).
  - **RSS/Atom feed monitoring**: Subscribe to a site's RSS feed. New entries indicate new or updated pages. Efficient for blogs and news sites.
  - **WebSub (PubSubHubbub)**: Real-time push notification when a page changes. Site publishes update → hub notifies subscribers → crawler re-crawls immediately. Lowest latency but requires site cooperation and hub infrastructure.
  - **Change rate classification**: Classify pages into buckets: very frequent (minutes — news homepages), frequent (hours — active forums), moderate (days — product pages), slow (weeks/months — documentation), static (rarely changes — archived content). Allocate re-crawl budget by bucket.
- **Freshness metrics**:
  - **Age**: time since the page was last crawled. Lower is better.
  - **Freshness**: binary — is the cached copy current? (Has the page changed since last crawl?)
  - **Average freshness**: across all pages, what fraction have a current cached copy? Target: 90%+ freshness for the most important pages.
  - **Staleness cost**: Not all staleness is equal. A stale news article (1 hour old) is worse than a stale documentation page (1 week old). Weight freshness by page importance.
- **Crawl budget allocation algorithms**:
  - **Uniform allocation**: Equal crawl budget per domain. Fair but doesn't reflect domain importance.
  - **Proportional to size**: Larger sites get more budget. But large sites aren't necessarily more important.
  - **Proportional to change rate × importance**: Allocate budget to maximize expected freshness gain. Sites that are both important and frequently changing get the most budget.
  - **Optimization formulation**: Given a total crawl budget of B pages/day, allocate to domains to maximize Σ(importance_i × freshness_i). This is a constrained optimization problem. Greedy: sort domains by (importance × change_rate / current_freshness), allocate in order.
- **Contrast with Googlebot**: Google's crawl scheduler is deeply integrated with Search ranking. Pages that appear in more search results get higher re-crawl priority. Google Search Console lets site operators request indexing of specific URLs. Google re-crawls popular pages (news homepages) every few minutes and stable pages (Wikipedia articles) every few weeks.
- **Contrast with Common Crawl**: Common Crawl does monthly full-web crawls. No incremental re-crawl scheduling. Each crawl is independent. Freshness is not a goal — archival completeness is.

### 9. 09-spider-traps-and-quality.md — Spider Traps, Quality Control & Edge Cases

- **Spider traps**: Websites that generate infinite or near-infinite URLs, trapping the crawler in an endless loop.
  - **Types**:
    - **Calendar traps**: `/calendar/2024/01/01`, `/calendar/2024/01/02`, ... infinite date pages.
    - **Session ID traps**: URLs with session IDs that change on every request → infinite unique URLs for the same content. Example: `/page?sid=abc123`, `/page?sid=def456`.
    - **URL parameter combinatorial explosion**: `/products?color=red&size=large`, `/products?size=large&color=red` — different URLs, same content. With many parameters: millions of combinations.
    - **Infinite depth**: Relative links that create infinite path depth. `/a/b/../a/b/../a/b/...`.
    - **Deliberate traps (honeypots)**: Sites that intentionally create traps to detect and block crawlers. Hidden links visible only to bots.
  - **Detection and mitigation**:
    - **Max depth limit**: Stop following links beyond depth N (e.g., 15) from the seed URL.
    - **Max pages per domain**: Hard cap on pages crawled per domain per crawl cycle.
    - **URL pattern detection**: Detect URLs that contain increasing numeric sequences (calendar), random strings (session IDs), or repeating path segments. Use regex or heuristics to identify and skip.
    - **Content deduplication**: If the same content keeps appearing at different URLs → stop crawling that pattern. Use content hash or SimHash.
    - **URL length limit**: Skip URLs longer than N characters (e.g., 2048). Very long URLs are often generated dynamically and are trap indicators.
    - **Domain crawl rate monitoring**: If a domain's URL discovery rate greatly exceeds its useful content rate → likely a trap. Alert or cap.
- **Quality scoring**: Not all web pages are worth crawling. Prioritize quality.
  - **Spam detection**: Identify spam pages (keyword stuffing, link farms, cloaking — showing different content to crawlers vs users). Use content analysis and link analysis. Spam pages waste crawl budget and pollute the content store.
  - **Duplicate content clusters**: Group near-duplicate pages (SimHash). Keep the canonical version (shortest URL, or the one with the `<link rel="canonical">`). Skip the rest.
  - **Thin content**: Pages with very little text content (mostly ads, navigation, or boilerplate). May not be worth storing. Detect by text-to-HTML ratio or by comparing extracted text length to a threshold.
  - **Soft 404 detection**: Pages that return HTTP 200 but display "Page Not Found" content. Compare the page's content to a template soft-404 (many CMSes use the same error page). If similarity > threshold → treat as 404, don't store.
- **URL normalization edge cases**:
  - Protocol: `http` vs `https` (treat as different by default, unless you know the http version redirects to https).
  - Trailing slash: `/path` vs `/path/` (different URLs technically, but usually same content). Normalize by removing trailing slash (or keeping it — be consistent).
  - Fragment: `#section` is a client-side anchor, not sent to the server. Always strip fragments.
  - Encoding: `%7E` vs `~` (decode unreserved characters per RFC 3986).
  - Port: `:80` for HTTP and `:443` for HTTPS are defaults — remove them.
  - Case: scheme and host are case-insensitive, path is case-sensitive (on most servers).
  - www vs non-www: `www.example.com` vs `example.com`. Treat as different unless you know they're the same (via redirect or canonical tag).
  - Query parameter sorting: `/page?b=2&a=1` → `/page?a=1&b=2`. Normalizing query params reduces false duplicates. But: some sites use parameter order semantically (rare but possible).
- **Contrast with Googlebot**: Google has extensive spam detection (SpamBrain ML model), quality scoring (E-E-A-T), and trap detection. Google's Webmaster Guidelines define acceptable practices. Google penalizes sites that use cloaking, keyword stuffing, or link schemes. The interview crawler has simpler quality heuristics.
- **Contrast with Common Crawl**: Common Crawl does minimal quality filtering — it aims for completeness, not quality. Quality filtering is left to downstream consumers of the data. This is appropriate for a data archive but not for a search engine.

### 10. 10-scaling-and-performance.md — Scaling & Performance

Cross-cutting doc that ties together scaling decisions from all deep dives.

- **Scale numbers** (for a large-scale web crawler):
  - **The web**: ~5.5 billion indexed pages (Google), ~1.1 billion active websites, ~200 TB of text content (excluding media). Pages are added and removed constantly.
  - **Google's crawl rate**: Googlebot crawls hundreds of billions of pages. Re-crawl rate varies: popular pages every few minutes, stable pages every few weeks. Estimates suggest Googlebot makes ~10-20 billion requests/day.
  - **Common Crawl**: ~3.5 billion pages per monthly crawl, ~350 TB of WARC data per crawl.
  - **Interview-scale crawler**: Design for ~1 billion pages, crawl cycle of 1 week. 1 billion / (7 × 86,400) ≈ **1,653 pages/second**. With overhead (DNS, retries, failures): target **2,000-5,000 pages/second**.
- **Back-of-envelope calculations**:
  - **Storage**: 1 billion pages × 100 KB average = **100 TB** of raw content. With compression (gzip, ~5x): ~20 TB. Metadata (URL, hash, timestamps) per page: ~500 bytes × 1 billion = ~500 GB.
  - **Network bandwidth**: 5,000 pages/sec × 100 KB/page = 500 MB/sec = **4 Gbps** sustained. Need multiple machines with high-bandwidth NICs.
  - **DNS lookups**: 5,000 pages/sec × (assume 50% cache hit rate) = 2,500 DNS lookups/sec. Local DNS cache handles most; a caching resolver like unbound can handle 10K+ queries/sec easily.
  - **URL dedup set**: 1 billion URLs × 8 bytes (hash) = **8 GB**. Fits in memory on a single machine. For 10 billion URLs: 80 GB → needs sharding or Bloom filter (10 bits/URL × 10 billion = ~12 GB).
  - **Frontier queue**: At steady state, ~10-100 million URLs in the frontier. In-memory: feasible on a large machine. On-disk (RocksDB): no problem.
  - **Number of crawler machines**: If each machine can fetch 500 pages/sec (limited by network, DNS, politeness): 5,000 / 500 = **10 machines**. For Google-scale (~20 billion pages/day = ~230,000 pages/sec): need thousands of machines.
- **Bottleneck analysis**:
  - **Network I/O**: Primary bottleneck. Mitigate: async I/O, connection pooling, HTTP keep-alive, geographically distributed crawlers.
  - **DNS resolution**: Secondary bottleneck. Mitigate: aggressive caching, prefetching, custom async resolver.
  - **Frontier operations**: Dequeue + enqueue + dedup. Must be O(1) or O(log n). Use efficient data structures: hash-based dedup (O(1)), heap-based politeness scheduling (O(log n)).
  - **Content parsing**: HTML parsing is CPU-bound. Parallelize across cores. If parsing is the bottleneck, separate fetcher and parser workers (pipeline architecture).
  - **Storage writes**: Writing 500 MB/sec to disk. Use batch writes, compression, append-only storage (WARC files). Object storage (S3) handles this easily.
- **Performance optimizations**:
  - **Pipeline architecture**: Separate stages: frontier dequeue → DNS resolution → HTTP fetch → parse → store → enqueue new URLs. Each stage runs independently, connected by queues. Stages can have different parallelism (e.g., more fetcher workers than parsers).
  - **Batch operations**: Batch DNS lookups, batch writes to content store, batch URL dedup checks. Amortize per-operation overhead.
  - **Connection reuse**: HTTP keep-alive to reuse TCP connections to the same server. Significant savings when crawling many pages from the same domain.
  - **Compression**: Accept gzip/brotli in HTTP requests. Most servers serve compressed responses → less bandwidth.
  - **Early termination**: If a page is too large (>10 MB), abort. If content-type is not text/html (detected via HEAD request or Content-Type header), skip.
- **Monitoring and alerting**:
  - **Key metrics**: pages fetched/sec (total and per domain), error rate by type (DNS failure, timeout, 4xx, 5xx, connection refused), frontier queue depth, content store growth rate, duplicate page rate, unique domains crawled, DNS cache hit rate.
  - **Anomaly detection**: Sudden drop in crawl rate → network issue or target sites blocking. Sudden spike in error rate → fetcher bug or DNS failure. Frontier growing unboundedly → spider trap.
  - **Per-domain dashboards**: Monitor individual high-traffic domains. Detect if you're being blocked (increasing 403/429 responses).
- **Contrast with Googlebot**: Google's crawler runs on tens of thousands of machines. It's integrated with Google's infrastructure (Bigtable, MapReduce, Colossus/GFS, Borg). The scale is ~1000x a typical interview crawler. Google's monitoring and self-healing are world-class (automatic rebalancing, dead machine replacement, etc.).
- **Contrast with Apache Nutch**: Nutch runs on Hadoop. Its scaling model is batch-oriented: each crawl cycle is a MapReduce job. Scaling = adding more Hadoop nodes. Good throughput but high latency between cycles. Not suitable for real-time crawling.

### 11. 11-design-trade-offs.md — Design Philosophy & Trade-off Analysis

Opinionated analysis of web crawler design choices — not just "what" but "why this and not that."

- **Breadth-first vs depth-first crawling**:
  - Breadth-first (BFS): Crawl all pages at depth 1 before depth 2, etc. Covers more domains early. Better for discovering the "shape" of the web. Used by search engines (find important pages first — they tend to be closer to seeds). Risk: frontier queue grows very large (all URLs at the current depth).
  - Depth-first (DFS): Follow links deeply into one site before moving to the next. Better for comprehensive crawling of a single site. Risk: spider traps, gets stuck in one domain.
  - **Best-first (priority-based)**: Neither strict BFS nor DFS — use a priority queue and always crawl the highest-priority URL next. Priority based on: estimated page importance, change rate, freshness. This is what production crawlers actually use.

- **Exact dedup (hash set) vs probabilistic dedup (Bloom filter)**:
  - Hash set: exact, no false positives. Memory: 8 bytes per URL hash × 10 billion URLs = 80 GB. Needs sharding.
  - Bloom filter: space-efficient (10 bits per URL = 12 GB for 10 billion URLs). ~1% false positive rate (some URLs will be incorrectly marked as "already seen" and skipped). Cannot delete entries (standard Bloom filter). Good enough for crawling — missing 1% of pages is acceptable.
  - **Trade-off**: Memory vs accuracy. For interview-scale (1 billion URLs), hash set fits in memory (8 GB). For web-scale (100 billion URLs), Bloom filter is necessary.

- **Politeness: fixed delay vs adaptive delay**:
  - Fixed delay (e.g., 1 request/second per domain): simple, predictable, safe. But: wastes crawl budget on fast servers that could handle 10 req/sec, and may still overwhelm slow servers.
  - Adaptive delay: measure server response time. Fast responses → decrease delay. Slow responses → increase delay. 429/503 → back off sharply. More efficient use of crawl budget. But: more complex, risk of being too aggressive (slow detection of overload).
  - **Recommendation**: Start with fixed delay (1 req/sec), respect `Crawl-delay` from robots.txt. Add adaptive delay as an optimization later.

- **Centralized vs distributed frontier**:
  - Centralized: one machine holds the entire frontier. Simple coordination. But: single point of failure, memory limited, all URL routing goes through one machine.
  - Distributed: frontier partitioned across nodes (by domain hash). Scales horizontally. But: URL routing adds network hops, dedup requires distributed coordination, more complex failure handling.
  - **Decision point**: If crawling <100 million pages → centralized is fine. If crawling billions → distributed is necessary.

- **Store everything vs store selectively**:
  - Store everything (Common Crawl approach): raw HTML for every page. Maximum flexibility for reprocessing. But: massive storage costs.
  - Store selectively: only store "useful" pages (pass quality filter). Skip duplicates, spam, thin content. Less storage but if quality filter has bugs, you lose data.
  - **Recommendation**: Store everything in a cheap archive (S3 Glacier). Apply quality filtering downstream (index only high-quality pages). Storage is cheap; re-crawling is expensive.

- **Real-time crawling vs batch crawling**:
  - Real-time (Googlebot): continuous crawling, URLs are fetched as they're discovered or as re-crawl time arrives. Low-latency freshness. More complex (always-running system).
  - Batch (Apache Nutch): crawl in discrete cycles. Each cycle: generate URLs → fetch → parse → update frontier → repeat. Simpler to reason about. Higher latency (hours between cycles).
  - **Trade-off**: Freshness vs simplicity. For a search engine, real-time is necessary. For a data archive, batch is fine.

- **JavaScript rendering vs HTML-only**:
  - HTML-only: parse raw HTML. Fast, simple, low resource usage. But: misses content loaded by JavaScript (SPAs, dynamic pages). An increasing fraction of the web relies on JavaScript — some estimates say 10-20% of pages have critical content behind JS.
  - JavaScript rendering (headless browser): render the page in a full browser (headless Chrome via Puppeteer/Playwright). Sees the same content as a real user. But: 10-100x slower, 10-50x more resource-intensive (CPU, memory), more complex infrastructure (browser fleet management).
  - **Google's approach**: Googlebot renders all pages with WRS (Web Rendering Service), but there's a delay — pages are first indexed based on HTML, then re-indexed after rendering. This "two-wave indexing" balances speed with completeness.
  - **Recommendation for interview**: Start with HTML-only. Mention JS rendering as an enhancement. Most interviewers will be impressed that you know the trade-off.

- **DNS: OS resolver vs custom resolver**:
  - OS resolver: simple, uses `/etc/resolv.conf`. But: synchronous, limited concurrency (~100 concurrent lookups), subject to OS resolver bugs and caching policy.
  - Custom async resolver (c-ares, unbound): high concurrency (thousands of concurrent lookups), configurable caching, can target specific DNS servers. More setup but necessary at scale.
  - **Decision point**: For <100 pages/sec, OS resolver is fine. For >1,000 pages/sec, need a custom resolver.

- **Single-machine vs distributed architecture**:
  - Single machine: simpler to build, debug, and operate. With async I/O, a single machine can crawl ~1,000-5,000 pages/sec. Sufficient for many use cases (focused crawling, small-to-medium search engines).
  - Distributed: necessary for web-scale (millions of pages/sec). Adds complexity: coordination, fault tolerance, distributed dedup, network communication. Don't distribute prematurely.
  - **Recommendation for interview**: Start with a single machine design (Attempt 0-2), then distribute (Attempt 3+). This shows the interviewer you understand when and why to distribute.

## CRITICAL: The design must be focused on Web Crawling as a system
A web crawler is a distributed system that fetches, parses, and stores web pages at scale while being a good citizen of the web (politeness, robots.txt). The core challenges are: URL frontier design (what to crawl next), distributed fetching (I/O at scale), deduplication (don't crawl the same page twice), politeness (don't overwhelm target sites), freshness (re-crawl changed pages), and spider trap detection. Reference real-world implementations: Googlebot, Common Crawl, Apache Nutch, Scrapy. The candidate should demonstrate understanding of WHY each component exists and the trade-offs between approaches.

## CRITICAL: Iterative build-up (DO NOT skip this)
The architecture MUST evolve iteratively. Each attempt builds on the previous one by identifying a concrete problem and solving it. The candidate should NOT jump to the final architecture.

### Attempt 0: Single-threaded crawler
- A single loop: dequeue URL → fetch page → extract links → enqueue new URLs → repeat.
- URLs stored in a simple FIFO queue (BFS). Visited URLs tracked in a hash set. Pages stored in local files.
- **Problems found**: Only 1 page at a time (sequential I/O). No politeness (can overwhelm a server). No DNS caching (repeated DNS lookups). No robots.txt checking. ~1-5 pages/second.

### Attempt 1: Multi-threaded crawler with politeness
- Replace single thread with a thread pool (e.g., 50 threads). Each thread fetches independently.
- Add robots.txt fetching and caching. Respect `Disallow` rules.
- Add per-domain crawl delay (1 request/second per domain). Use domain-specific queues to enforce.
- Add DNS caching (in-memory LRU cache, TTL-based).
- **Problems found**: Thread overhead limits concurrency (~1,000 threads). Frontier (shared queue + visited set) becomes a contention bottleneck. No content dedup (different URLs, same content). No priority (FIFO treats all pages equally). All pages stored locally — storage fills up. ~50-200 pages/second.

### Attempt 2: Async I/O crawler with Mercator-style frontier
- Replace threads with async I/O (epoll + non-blocking sockets). Single event loop handles thousands of concurrent connections. ~5,000-50,000 concurrent fetches.
- Implement Mercator-style frontier: front queues (priority) + back queues (politeness) + heap (scheduling). Priority based on URL depth and domain importance.
- Add URL canonicalization before dedup. Add content-hash dedup (SHA-256 of page body).
- Add content storage to object storage (S3) or HDFS. Store in WARC format.
- **Problems found**: Single machine — limited by one machine's network bandwidth and CPU. Frontier is still in-memory — crashes lose all state. No re-crawl scheduling (one-pass crawl only). No spider trap detection. ~1,000-5,000 pages/second.

### Attempt 3: Distributed crawler
- Distribute across N machines. Partition by domain (hash(domain) % N). Each machine manages its own frontier partition and fetches its assigned domains.
- URL routing: when a new URL is discovered, hash its domain to determine which machine should crawl it. Send the URL to that machine's frontier.
- Frontier persistence: use RocksDB or LevelDB on each node. WAL for crash recovery. Periodic checkpointing.
- Centralized metadata store (Cassandra or HBase): URL → crawl state, timestamp, content hash.
- Node failure detection: heartbeats + coordinator (ZooKeeper or similar). Rebalance orphaned domains to surviving nodes.
- **Problems found**: No freshness tracking (pages crawled once and never re-visited). No adaptive politeness (fixed delay regardless of server capacity). No quality filtering (crawls spam and traps). No monitoring — debugging is blind. ~5,000-50,000 pages/second.

### Attempt 4: Intelligent crawling (freshness, quality, monitoring)
- Add re-crawl scheduler: track change frequency per page. Prioritize re-crawling frequently-changing pages. Use HTTP conditional requests (If-Modified-Since) to avoid re-downloading unchanged pages.
- Add spider trap detection: max depth, max pages per domain, URL pattern detection, content dedup across URLs.
- Add quality scoring: text-to-HTML ratio, spam classification, soft-404 detection. Skip low-quality pages.
- Add monitoring: per-domain crawl rate, error rate, queue depth, dedup rate, content store growth. Dashboards and alerts.
- Add sitemap parsing: discover new URLs from XML sitemaps. Use `<lastmod>` for freshness hints.
- **Problems found**: No JavaScript rendering (misses SPA content). No geographic distribution (high latency to distant servers). Dedup is exact hash only — near-duplicates slip through. Manual scaling (adding/removing nodes requires reconfiguration).

### Attempt 5: Production hardening
- **JavaScript rendering**: Add a headless browser farm (Puppeteer/Playwright fleet). Use for a subset of pages (SPA-heavy sites). Two-pass: HTML-first index, then rendered re-index.
- **Geographic distribution**: Deploy crawler clusters in multiple regions (US, Europe, Asia). Assign domains to the nearest cluster. Lower latency, better politeness (crawl US sites from US).
- **Near-duplicate detection**: Add SimHash/MinHash for near-duplicate detection. Cluster similar pages, keep canonical.
- **Auto-scaling**: Monitor crawl rate and queue depth. Auto-add/remove crawler nodes based on demand. Use Kubernetes for orchestration.
- **Data pipeline integration**: Feed crawled content into downstream systems: search indexer, ML training data, knowledge graph extraction.
- **Compliance**: GDPR handling for personal data in crawled content. Copyright awareness. Opt-out mechanism for site operators.

Each attempt MUST:
1. Start by identifying concrete problems with the previous attempt
2. Propose a solution
3. Explain WHY this approach (and why not the alternative — mention real-world crawler implementations where relevant)
4. End with "what's still broken?" to motivate the next attempt

## CRITICAL: Accuracy requirements

I DO NOT TRUST AI-GENERATED FACTS ABOUT SPECIFIC SYSTEMS. Every concrete claim about web crawler internals must be verifiable against official sources. Specifically:

1. **Use WebSearch and WebFetch tools** to look up official documentation and research papers BEFORE writing. Search for:
   - "Googlebot architecture how it works"
   - "Mercator web crawler frontier design paper"
   - "web crawler politeness robots.txt best practices"
   - "Common Crawl architecture scale numbers"
   - "Apache Nutch architecture Hadoop"
   - "URL frontier design distributed web crawler"
   - "SimHash near duplicate detection web pages"
   - "Bloom filter URL deduplication web crawler"
   - "Google web rendering service WRS JavaScript"
   - "web crawler spider trap detection"
   - "DNS resolution at scale web crawler"
   - "Scrapy architecture internals"
   - "WARC file format web archive"
   - "crawl scheduling freshness optimization"
   - "content-defined chunking deduplication"

   **BLANKET PERMISSION**: You have full permission to use WebSearch and WebFetch to read any online documentation, blog posts, or research papers. Do NOT ask the user for permission to read — just read. This applies to developers.google.com, commoncrawl.org, nutch.apache.org, research papers on arxiv, ACM, and any other reference source. Read as many pages as needed to verify facts.

2. **For every concrete number** (Googlebot crawl rate, Common Crawl size, web size), verify against official sources. If you cannot verify a number, explicitly write "[UNVERIFIED — check official sources]" next to it.

3. **For every claim about specific systems** (Googlebot rendering pipeline, Nutch MapReduce cycles, Mercator frontier design), if it's not from an official source, mark it as "[INFERRED — not officially documented]".

4. **CRITICAL: Do NOT conflate different crawler systems.** Each has a distinct architecture and purpose:
   - **Googlebot**: Search engine crawler, JS rendering, PageRank-driven priority, commercial scale
   - **Common Crawl**: Open archive, breadth-first, no rendering, research-oriented
   - **Apache Nutch**: Open-source, Hadoop-based, batch-oriented, extensible
   - **Scrapy**: Python framework, single-machine, developer-friendly, for focused crawling
   When discussing design decisions, ALWAYS explain WHY each system made its choice and how the alternatives differ.

## Key Web Crawler topics to cover

### Requirements & Scale
- Crawl 1 billion web pages in a reasonable timeframe (1 week for full crawl, continuous for re-crawl)
- Target crawl rate: 2,000-5,000 pages/second
- Handle ~100 TB of raw content storage
- Respect robots.txt and crawl politeness
- Detect and avoid spider traps
- Support incremental re-crawling for freshness
- Deduplication: URL-level and content-level

### Architecture deep dives (create separate docs as listed in "Files to create" above)

### Design evolution (iterative build-up — the most important part)
- Attempt 0: Single-threaded crawler (1-5 pages/sec)
- Attempt 1: Multi-threaded + politeness + robots.txt (50-200 pages/sec)
- Attempt 2: Async I/O + Mercator frontier + content dedup (1,000-5,000 pages/sec)
- Attempt 3: Distributed crawler (5,000-50,000 pages/sec)
- Attempt 4: Intelligent crawling (freshness, quality, monitoring)
- Attempt 5: Production hardening (JS rendering, geo-distribution, auto-scaling)

### Consistency & Data
- URL frontier: must be durable (survive crashes). Use WAL + periodic checkpoints.
- Content store: append-only (WARC format). Content-addressed (hash-keyed) for dedup.
- Crawl state: at-least-once semantics (some URLs may be crawled twice). Dedup at storage layer.
- Metadata: eventual consistency is fine (a slightly stale view of what's been crawled is acceptable).

## Contrasts to weave throughout the design

- **Googlebot vs interview crawler**: Google is 1000x scale, has JS rendering, deep integration with search ranking. Interview crawler is a simplified version.
- **BFS vs best-first**: BFS is simple but doesn't prioritize. Best-first (priority queue) is what production crawlers use.
- **Bloom filter vs hash set for dedup**: Space vs accuracy trade-off. Bloom filter for web-scale, hash set for smaller crawls.
- **Real-time vs batch crawling**: Continuous (Googlebot) vs periodic (Nutch). Trade-off: freshness vs simplicity.
- **HTML-only vs JS rendering**: 10-100x cost difference. Most pages don't need rendering. Use rendering selectively.

## What NOT to do
- Do NOT treat the web crawler as just a "download pages" tool — it's a complex distributed system with scheduling, dedup, politeness, and quality concerns.
- Do NOT confuse different crawler implementations — each has a distinct architecture and purpose.
- Do NOT jump to the final architecture. Build it step by step (Attempt 0 → 5).
- Do NOT make up scale numbers — verify against official sources or mark as unverified.
- Do NOT skip the iterative build-up.
- Do NOT write a Wikipedia article. This is an INTERVIEW SIMULATION with dialogue.
- Do NOT describe features without explaining WHY they exist (what problem they solve).
- Do NOT ask the user for permission to read online documentation — blanket permission is granted (see Accuracy requirements section).
- Do NOT ignore robots.txt and crawl ethics — this is a critical part of the design and interviewers expect you to discuss it.
