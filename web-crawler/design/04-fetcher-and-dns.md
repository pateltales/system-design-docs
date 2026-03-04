# 04 — Fetcher, DNS Resolution, and Network Layer

The fetcher is where the crawler meets the real internet. Everything else — the
frontier, the URL dedup, the parser — is internal bookkeeping. The fetcher is
the component that opens TCP connections, sends HTTP requests, and deals with
the chaos of the actual web: slow servers, infinite redirects, spider traps,
broken encodings, and hostile bot-detection systems. Getting this layer right
determines whether your crawler can do 1,000 pages/sec or 100,000 pages/sec.

---

## 1. Fetcher Architecture

There are three progressively more sophisticated approaches to building the
fetch layer.

### 1.1 Multi-threaded Fetcher (Simple, Limited)

Each thread picks a URL, opens a connection, downloads the page, and hands it
off. The thread blocks on network I/O for the entire duration of the fetch.

```
┌─────────────────────────────────────────────────┐
│                  Fetcher Process                 │
│                                                  │
│  ┌──────────┐ ┌──────────┐     ┌──────────┐     │
│  │ Thread 1 │ │ Thread 2 │ ... │Thread 500│     │
│  │ fetch(u) │ │ fetch(u) │     │ fetch(u) │     │
│  │ [BLOCKED]│ │ [BLOCKED]│     │ [BLOCKED]│     │
│  └────┬─────┘ └────┬─────┘     └────┬─────┘     │
│       │             │                │           │
│       ▼             ▼                ▼           │
│   ┌─────────────────────────────────────────┐    │
│   │         OS Kernel (socket I/O)          │    │
│   └─────────────────────────────────────────┘    │
└─────────────────────────────────────────────────┘
```

**How it works:**
- Thread pool of N threads (typically 500-2,000).
- Each thread: dequeue URL → DNS resolve → TCP connect → TLS handshake →
  send HTTP request → read response → hand off to parser.
- Thread blocks at every network call. OS scheduler context-switches between
  threads while they wait.

**Why it's limited:**
- Each OS thread costs ~1 MB of stack memory. 1,000 threads = 1 GB just for
  stacks.
- Context switching between thousands of threads has CPU overhead.
- Practical ceiling: ~1,000-10,000 concurrent connections per machine.
- Most of each thread's lifetime is spent blocked on I/O, wasting the thread
  resource.

**When it's appropriate:**
- Small-to-medium crawls (< 10M pages).
- Prototyping. Easy to reason about — one URL per thread, sequential logic.
- Languages without good async support.

### 1.2 Async I/O Fetcher (Preferred at Scale)

A single event loop (or a small number of event loops) manages thousands of
concurrent connections without one-thread-per-connection.

```
┌──────────────────────────────────────────────────────────┐
│                    Fetcher Process                        │
│                                                          │
│  ┌────────────────────────────────────────────────────┐  │
│  │              Event Loop (single thread)            │  │
│  │                                                    │  │
│  │   ┌─────┐ ┌─────┐ ┌─────┐       ┌─────┐          │  │
│  │   │Conn1│ │Conn2│ │Conn3│  ...  │Conn │          │  │
│  │   │Ready│ │Wait │ │Ready│       │50000│          │  │
│  │   └──┬──┘ └─────┘ └──┬──┘       └─────┘          │  │
│  │      │               │                            │  │
│  │      ▼               ▼                            │  │
│  │  callback()      callback()                       │  │
│  │  read bytes      read bytes                       │  │
│  └────────────────────┬───────────────────────────────┘  │
│                       │                                  │
│                       ▼                                  │
│   ┌───────────────────────────────────────────────────┐  │
│   │    epoll (Linux) / kqueue (macOS) / IOCP (Win)    │  │
│   │    "Which sockets have data ready?"               │  │
│   └───────────────────────────────────────────────────┘  │
└──────────────────────────────────────────────────────────┘
```

**How it works:**
1. Register thousands of sockets with the OS kernel's event notification
   mechanism (epoll on Linux, kqueue on macOS).
2. Event loop calls `epoll_wait()` — blocks until *any* socket has data ready.
3. Kernel returns a list of ready sockets.
4. Event loop processes each ready socket: read available bytes, advance the
   HTTP state machine for that connection, fire callbacks.
5. Loop repeats.

**Why it's better:**
- One thread manages 50,000-100,000 simultaneous connections.
- Memory per connection: ~10-50 KB (socket buffer + HTTP parse state) vs.
  ~1 MB per thread.
- No context-switch overhead — the event loop is one thread doing a tight loop.
- CPU utilization is higher because you're never blocking on I/O.

**Libraries / Frameworks:**
- **Java**: Netty (NIO-based, used by Elasticsearch, Cassandra, gRPC-Java)
- **Python**: asyncio + aiohttp
- **Node.js**: libuv (the core of Node's event loop)
- **C/C++**: libevent, libuv, Boost.Asio
- **Go**: goroutines (green threads on top of epoll — runtime handles it)
- **Rust**: Tokio

**Implementation note — multi-loop:** For machines with many cores, run one
event loop per core (e.g., Netty's EventLoopGroup with N threads, where N =
number of CPU cores). Each loop handles its own subset of connections. This
avoids contention on a single loop.

### 1.3 Fetcher Pool (Distributed Fleet)

At web scale, a single machine isn't enough. You run a fleet of fetcher
machines, each responsible for a subset of domains.

```
                     ┌─────────────┐
                     │  Frontier /  │
                     │  URL Queue   │
                     └──────┬──────┘
                            │
              ┌─────────────┼─────────────┐
              │             │             │
              ▼             ▼             ▼
     ┌────────────┐ ┌────────────┐ ┌────────────┐
     │  Fetcher 1 │ │  Fetcher 2 │ │  Fetcher N │
     │            │ │            │ │            │
     │ Domains:   │ │ Domains:   │ │ Domains:   │
     │ a.com      │ │ c.com      │ │ y.com      │
     │ b.com      │ │ d.com      │ │ z.com      │
     │            │ │            │ │            │
     │ Politeness │ │ Politeness │ │ Politeness │
     │ tracking:  │ │ tracking:  │ │ tracking:  │
     │ LOCAL      │ │ LOCAL      │ │ LOCAL      │
     └────────────┘ └────────────┘ └────────────┘
```

**Domain-to-fetcher assignment:**
```
fetcher_id = hash(domain) % num_fetchers
```

This consistent mapping means:
- All URLs for `example.com` go to the same fetcher machine.
- Politeness state (last request time, crawl delay, robots.txt cache) is
  **local** to that machine. No distributed coordination needed.
- If a fetcher goes down, reassign its domains to others (consistent hashing
  with virtual nodes helps minimize reshuffling).

### Comparison Table: Threading vs. Async I/O

| Aspect                  | Multi-threaded              | Async I/O (epoll/kqueue)       |
|-------------------------|-----------------------------|--------------------------------|
| Concurrency per machine | 1,000-10,000 connections    | 50,000-100,000 connections     |
| Memory per connection   | ~1 MB (thread stack)        | ~10-50 KB (socket + state)     |
| CPU overhead            | Context switching            | Minimal (one thread, tight loop) |
| Programming model       | Sequential (easy)           | Callback/future-based (harder) |
| Debugging               | Stack traces are clear      | Callback chains are harder     |
| I/O utilization         | Low (threads block)         | High (never blocks)            |
| Libraries               | java.util.concurrent, pthreads | Netty, asyncio, libuv, Tokio |
| Best for                | Small crawls, prototypes    | Production web-scale crawlers  |
| Latency per request     | Same                        | Same                           |
| Throughput              | Lower                       | 10-50x higher                  |

---

## 2. HTTP Client Behavior

The fetcher's HTTP client must handle the full complexity of real-world HTTP.
This section covers every aspect.

### 2.1 The Fetch Pipeline

```
 URL from frontier
       │
       ▼
 ┌─────────────┐     ┌──────────────┐     ┌───────────────┐
 │ DNS Resolve  │────▶│ TCP Connect  │────▶│ TLS Handshake │
 │ (cached?)    │     │ (timeout 5s) │     │ (if HTTPS)    │
 └─────────────┘     └──────────────┘     └───────┬───────┘
                                                   │
       ┌───────────────────────────────────────────┘
       ▼
 ┌─────────────┐     ┌──────────────┐     ┌───────────────┐
 │ Send HTTP   │────▶│ Read Response│────▶│ Decompress    │
 │ Request     │     │ Headers      │     │ (gzip/br)     │
 └─────────────┘     └──────────────┘     └───────┬───────┘
                                                   │
       ┌───────────────────────────────────────────┘
       ▼
 ┌─────────────┐     ┌──────────────┐     ┌───────────────┐
 │ Read Body   │────▶│ Charset      │────▶│ Hand off to   │
 │ (max 10 MB) │     │ Decode→UTF-8 │     │ Parser        │
 └─────────────┘     └──────────────┘     └───────────────┘
```

### 2.2 Redirect Handling

HTTP redirects are extremely common on the web. The fetcher must handle them
correctly.

**Rules:**
- Follow redirects: 301 (Moved Permanently), 302 (Found), 307 (Temporary
  Redirect), 308 (Permanent Redirect).
- **Max chain length: 5 hops.** After 5 redirects, abort and log.
- **Record the full redirect chain.** If A → B → C, store the mapping so that
  later, if you see a link to A, you can resolve it to C directly.
- **Detect redirect loops.** If any URL in the chain repeats, abort.
- **Handle relative redirect URLs.** `Location: /new-path` must be resolved
  against the current URL's base.
- **Cross-domain redirects:** If `a.com` redirects to `b.com`, the redirected
  fetch must respect `b.com`'s politeness rules. Check `b.com`'s robots.txt
  before proceeding.

**Redirect semantics matter:**
- 301/308: The URL has permanently moved. Update the canonical URL in your
  index. Don't re-crawl the old URL.
- 302/307: Temporary. Keep the old URL in the frontier for future re-crawls.

### 2.3 User-Agent Header

```
User-Agent: MyCrawler/1.0 (+https://example.com/crawler-info)
```

- Identify your crawler so site owners can look you up.
- Include a contact URL or email.
- Some sites serve different content based on User-Agent (cloaking). For
  search engine crawlers, this is a problem — Googlebot sometimes verifies
  its identity via reverse DNS.

### 2.4 Content-Type Handling

Not everything on the web is HTML. The fetcher must decide what to download.

**Strategy:**
1. If the URL path ends in `.jpg`, `.png`, `.gif`, `.mp4`, `.pdf`, `.zip` —
   skip it (or handle separately if you index images/PDFs).
2. For unknown URLs, optionally send a **HEAD request first** to check
   `Content-Type` before downloading the body.
3. Only parse `text/html` and `application/xhtml+xml`.
4. For everything else: log metadata (URL, Content-Type, size) but don't
   download the body.

**HEAD-then-GET tradeoff:**
- HEAD request adds one extra round trip (~50-100ms).
- Worth it if many URLs are non-HTML (e.g., crawling a file-heavy site).
- Not worth it for general web crawling where >90% of URLs are HTML.
- Compromise: skip HEAD for URLs that look like HTML (no file extension, or
  `.html`/`.htm`), use HEAD for ambiguous URLs.

### 2.5 Content-Encoding (Compression)

Most modern web servers compress responses. The fetcher must handle:

```
Request:
  Accept-Encoding: gzip, deflate, br

Response:
  Content-Encoding: gzip
  [compressed body]
```

- **gzip**: Most common. Every HTTP library supports it.
- **deflate**: Rare but still seen. zlib decompression.
- **br (Brotli)**: Increasingly common. Better compression than gzip. Requires
  Brotli library.
- **zstd**: Emerging. Supported by some CDNs (Cloudflare).

**Always send `Accept-Encoding`** — compressed responses are 60-80% smaller,
saving bandwidth and time.

### 2.6 Charset Detection and UTF-8 Conversion

The web is a mess of character encodings. The fetcher must normalize everything
to UTF-8.

**Detection priority (highest to lowest):**
1. **BOM (Byte Order Mark)** at the start of the document:
   - `EF BB BF` → UTF-8
   - `FF FE` → UTF-16 LE
   - `FE FF` → UTF-16 BE
2. **Content-Type HTTP header**: `Content-Type: text/html; charset=windows-1251`
3. **HTML meta tag**: `<meta charset="UTF-8">` or
   `<meta http-equiv="Content-Type" content="text/html; charset=ISO-8859-1">`
4. **Auto-detection**: Libraries like ICU or chardet can guess encoding from
   byte patterns. Accuracy is ~90-95%.
5. **Default**: If all else fails, assume UTF-8 (or ISO-8859-1 per HTTP/1.1
   spec for text/* types).

**After detection:** Convert to UTF-8 using iconv or equivalent. If conversion
fails (invalid byte sequences), replace with U+FFFD (replacement character)
rather than crashing.

### 2.7 Timeouts

Every network operation needs a timeout. Without them, a single slow server
can block a fetcher thread/connection forever.

| Timeout         | Value | What it guards against                          |
|-----------------|-------|-------------------------------------------------|
| DNS resolution  | 5s    | Unresponsive DNS server                         |
| TCP connect     | 5s    | Server not accepting connections                |
| TLS handshake   | 5s    | Slow TLS negotiation                            |
| First byte      | 10s   | Server accepted connection but not responding   |
| Read (per chunk)| 30s   | Server sending data very slowly (trickle attack)|
| Total request   | 60s   | Overall cap for the entire fetch                |

**If any timeout fires:** Abort the connection, record the failure, and move on.
The URL goes back to the retry queue (see error handling below).

### 2.8 Error Handling and Retry Logic

```
┌────────────────┐
│  HTTP Response  │
└───────┬────────┘
        │
        ▼
   ┌─────────┐     ┌──────────────────────────────────────┐
   │ 2xx OK  │────▶│ Success. Parse and extract links.    │
   └─────────┘     └──────────────────────────────────────┘
        │
   ┌─────────┐     ┌──────────────────────────────────────┐
   │ 3xx     │────▶│ Follow redirect (up to 5 hops).      │
   └─────────┘     └──────────────────────────────────────┘
        │
   ┌─────────┐     ┌──────────────────────────────────────┐
   │ 4xx     │────▶│ Client error. Log and discard.       │
   │(not 429)│     │ Do NOT retry (URL is bad/forbidden). │
   └─────────┘     └──────────────────────────────────────┘
        │
   ┌─────────┐     ┌──────────────────────────────────────┐
   │  429    │────▶│ Too Many Requests. Respect           │
   │         │     │ Retry-After header. Back off sharply. │
   │         │     │ Add domain to "slow down" list.      │
   └─────────┘     └──────────────────────────────────────┘
        │
   ┌─────────┐     ┌──────────────────────────────────────┐
   │ 5xx     │────▶│ Server error. Retry with exponential │
   │         │     │ backoff: 1s, 2s, 4s, 8s, 16s.       │
   │         │     │ Max 3-5 retries, then give up.       │
   └─────────┘     └──────────────────────────────────────┘
        │
   ┌─────────┐     ┌──────────────────────────────────────┐
   │ Timeout │────▶│ Retry (same backoff as 5xx).         │
   └─────────┘     └──────────────────────────────────────┘
        │
   ┌─────────┐     ┌──────────────────────────────────────┐
   │ConnRefus│────▶│ Server down. Retry much later        │
   │/DNS fail│     │ (minutes, not seconds). May mark     │
   │         │     │ domain as temporarily unreachable.    │
   └─────────┘     └──────────────────────────────────────┘
```

**Exponential backoff formula:**
```
delay = min(base_delay * 2^attempt, max_delay) + random_jitter
```
Where `base_delay = 1s`, `max_delay = 60s`, `jitter = random(0, 1s)`.

The jitter prevents the "thundering herd" problem where all retries for a
domain fire at exactly the same time.

### 2.9 Max Download Size

- **Hard limit: 10 MB.** If `Content-Length` header says > 10 MB, skip without
  downloading.
- If no `Content-Length` (chunked transfer), read up to 10 MB, then abort the
  connection.
- Why 10 MB? The vast majority of useful HTML pages are < 1 MB. Pages > 10 MB
  are almost always binary files served with wrong Content-Type, auto-generated
  garbage, or data dumps.

---

## 3. DNS Resolution at Scale

DNS resolution is a hidden bottleneck that can cripple a crawler. A crawler
fetching 10,000 pages/sec across 5,000 different domains needs 5,000 DNS
lookups/sec. The default OS resolver cannot handle this.

### 3.1 Why the OS Resolver is Too Slow

```
Application calls getaddrinfo("example.com")
       │
       ▼
┌──────────────────────────────────────────┐
│       OS Stub Resolver (glibc/musl)      │
│                                          │
│  - Synchronous / blocking call           │
│  - Typically limited to ~100 concurrent  │
│    lookups (nscd or systemd-resolved     │
│    have small thread pools)              │
│  - Each lookup: 1-100ms (cache miss)     │
│  - At 100 concurrent: max ~1,000-5,000   │
│    lookups/sec                           │
│  - No async API (getaddrinfo blocks)     │
└──────────────────────────────────────────┘
```

A crawler doing 10,000 fetches/sec needs 10,000 DNS lookups/sec. The OS
resolver caps out at ~1,000-5,000. The fetcher stalls waiting for DNS.

### 3.2 Solution: Multi-Layer DNS Caching

```
┌─────────────────────────────────────────────────────┐
│              Fetcher DNS Resolution Stack            │
│                                                     │
│  ┌───────────────────────────────────────────────┐  │
│  │  Layer 1: In-Process Cache (ConcurrentHashMap)│  │
│  │  - Key: domain name                           │  │
│  │  - Value: IP address + TTL expiry             │  │
│  │  - Hit rate: 90-95% for web crawlers          │  │
│  │  - Lookup: O(1), ~100 nanoseconds             │  │
│  └──────────────────────┬────────────────────────┘  │
│                         │ miss                      │
│                         ▼                           │
│  ┌───────────────────────────────────────────────┐  │
│  │  Layer 2: Local Caching Resolver              │  │
│  │  (unbound / dnsmasq on localhost)             │  │
│  │  - Handles 10,000-50,000 queries/sec          │  │
│  │  - Caches responses with proper TTL           │  │
│  │  - Hit rate: additional 3-5%                  │  │
│  └──────────────────────┬────────────────────────┘  │
│                         │ miss                      │
│                         ▼                           │
│  ┌───────────────────────────────────────────────┐  │
│  │  Layer 3: Upstream DNS (8.8.8.8, 1.1.1.1)    │  │
│  │  - Only ~2-5% of queries reach here           │  │
│  │  - Round trip: 1-50ms                         │  │
│  └───────────────────────────────────────────────┘  │
└─────────────────────────────────────────────────────┘
```

**Why 90%+ cache hit rate?**
- Crawlers tend to crawl many pages from the same domain in bursts (due to
  domain-based politeness queuing).
- The web has power-law domain distribution: a small number of domains have
  most of the pages.
- DNS TTLs are usually 300s-3600s. Once resolved, a domain stays cached for
  minutes to hours.

### 3.3 DNS Prefetching

Don't wait until fetch time to resolve DNS. Resolve it as soon as the URL is
dequeued from the frontier.

```
URL dequeued from frontier
       │
       ├──▶ DNS prefetch (async, non-blocking)
       │        │
       │        ▼
       │    IP cached in local map
       │
       ▼
  Wait for politeness timer
       │
       ▼
  Fetch (DNS already resolved — instant lookup from cache)
```

This hides DNS latency behind the politeness delay. Since you're waiting 1+
seconds between requests to the same domain anyway, you have plenty of time to
resolve the next URL's domain in the background.

### 3.4 Custom Async Resolver

For the highest performance, use an async DNS library instead of the blocking
OS resolver.

**c-ares (C Async Resolver):**
- Used internally by Node.js, curl, and many high-performance systems.
- Fully asynchronous: non-blocking DNS queries.
- Integrates with event loops (epoll/kqueue).
- Can handle thousands of concurrent DNS queries.

**Usage pattern with async I/O:**
```
Event Loop:
  1. Need to resolve "example.com"
  2. c-ares sends DNS UDP packet (non-blocking)
  3. Event loop continues processing other connections
  4. DNS response arrives → callback fires
  5. IP address stored in cache, fetch proceeds
```

### 3.5 DNS-Based Politeness (Critical and Often Missed)

This is a subtle but critical issue. Multiple domains can resolve to the same
IP address (shared hosting, CDNs, load balancers).

**The problem:**
```
Domain          IP Address
─────────────   ──────────
blog1.host.com  → 203.0.113.5
blog2.host.com  → 203.0.113.5
blog3.host.com  → 203.0.113.5
blog4.host.com  → 203.0.113.5
shop1.host.com  → 203.0.113.5
...
(50 domains on same shared hosting server)
```

If each domain is crawled at 1 req/sec and 50 domains share the same IP:
**50 req/sec hitting one physical server.** The server can't handle it. Your
crawler just accidentally DDoS'd a shared hosting provider.

**The solution:**
- After DNS resolution, track politeness **per IP address** in addition to
  per domain.
- Enforce a global rate limit per IP: e.g., max 5 req/sec to any single IP,
  regardless of how many domains point to it.
- Data structure: `Map<IP, RateLimiter>` alongside `Map<Domain, RateLimiter>`.
- The effective delay is: `max(domain_delay, ip_delay)`.

```
Per-domain politeness:  1 req/sec to blog1.host.com  ✓
Per-IP politeness:      max 5 req/sec to 203.0.113.5 ✓
Effective behavior:     All 50 domains combined ≤ 5 req/sec
```

---

## 4. Politeness Enforcement in the Fetcher

### 4.1 robots.txt Crawl-delay

```
# robots.txt for example.com
User-agent: *
Crawl-delay: 10
Disallow: /private/
```

- `Crawl-delay: 10` means wait at least 10 seconds between requests.
- This is a **non-standard but widely used** directive (not in the original
  robots.txt spec, but respected by Bing, Yandex, and most polite crawlers).
- Googlebot does **not** respect Crawl-delay (it uses its own adaptive system
  in Google Search Console).
- If Crawl-delay is absent, use a sensible default: 1 request per second per
  domain.

### 4.2 Default Politeness

When no Crawl-delay is specified:

```
default_delay = 1 second per domain
```

This means for a domain with 10,000 pages, it takes ~2.8 hours to crawl at
1 req/sec. This is intentionally slow. Being a good citizen matters more than
speed for any individual domain.

### 4.3 Adaptive Politeness

Static delays are suboptimal. A large site with powerful servers (amazon.com)
can handle more traffic than a small blog on shared hosting.

**Adaptive algorithm:**

```
For each domain, track:
  - avg_response_time (moving average over last 10 requests)
  - error_rate (4xx/5xx over last 20 requests)

Rules:
  IF avg_response_time < 200ms AND error_rate < 1%:
      delay = max(0.5s, crawl_delay)        # cautiously faster
  ELIF avg_response_time < 500ms AND error_rate < 5%:
      delay = max(1.0s, crawl_delay)        # normal
  ELIF avg_response_time < 2000ms:
      delay = max(2.0s, crawl_delay)        # server is struggling
  ELIF avg_response_time >= 2000ms OR error_rate > 10%:
      delay = max(5.0s, crawl_delay)        # back way off
  IF received 429 or 503:
      delay = max(30s, Retry-After header)  # sharp backoff
      add domain to "slow_domains" set
```

**Key principle:** Never go faster than the explicit Crawl-delay, but go
slower if the server is struggling.

---

## 5. Handling Traps and Edge Cases

The real web is full of pathological cases that can waste crawler resources or
cause infinite loops.

### 5.1 Spider Traps

A spider trap generates an infinite (or effectively infinite) number of URLs.

**Common types:**

| Trap Type           | Example                                          | Why it's infinite                     |
|---------------------|--------------------------------------------------|---------------------------------------|
| Calendar links      | `/calendar?date=2024-01-01`, `...01-02`, etc.   | Every day is a new URL, forever       |
| Session IDs in URL  | `/page?sid=abc123`, `/page?sid=def456`           | New session = new URL for same page   |
| Sort/filter combos  | `/products?sort=price&color=red&size=M&page=1`  | Combinatorial explosion of parameters |
| Relative link loops | `/a/b/c/../../../a/b/c/../../../a/b/c/...`      | Path normalization creates cycles     |
| Infinite pagination | `/page/1`, `/page/2`, ... `/page/999999`         | No last page                          |
| Deliberate traps    | Honeypot links hidden in HTML to trap bots       | Intentional                           |

**Mitigations (use ALL of these):**

1. **Max depth per domain.** Don't follow links more than N hops deep from
   the seed URL (e.g., depth 15-20). Most useful content is within 5-10 hops.

2. **Max pages per domain.** Cap at 100,000 - 1,000,000 pages per domain per
   crawl cycle. If you hit the cap, stop and move on.

3. **URL pattern detection.** If you see many URLs that differ only in a query
   parameter value (e.g., `?sid=...`), recognize the pattern and deduplicate:
   ```
   /page?sid=abc123  ─┐
   /page?sid=def456   ├─▶ Normalize to: /page (strip session params)
   /page?sid=ghi789  ─┘
   ```
   Common parameters to strip: `sid`, `session`, `jsessionid`, `phpsessid`,
   `utm_source`, `utm_medium`, `utm_campaign`, `fbclid`, `gclid`.

4. **Content hash deduplication.** Even if URLs are different, if the content
   hash (SimHash/MinHash) is the same, stop exploring that URL pattern.

5. **Path depth limit.** If the URL path has more than 10 segments
   (`/a/b/c/d/e/f/g/h/i/j/k`), it's probably a trap.

### 5.2 Soft 404s

A soft 404 is when a server returns HTTP 200 (OK) but the page content is
actually a "Page Not Found" message.

**Why it matters:** Your crawler thinks it found a real page and indexes it.
Your search index fills up with garbage "Page Not Found" pages.

**Detection approaches:**

1. **Content similarity to known 404.** For each domain, deliberately request
   a URL that definitely doesn't exist (e.g., `/this-page-does-not-exist-xyz`).
   Store the content hash of the response. For every subsequent 200 response,
   compare against this known 404 template. If similarity > 90%, it's a soft
   404.

2. **Title/heading detection.** Look for patterns in `<title>` or `<h1>`:
   "Page Not Found", "404", "Error", "doesn't exist", etc.

3. **Content length.** If the page is suspiciously short (< 1 KB) and doesn't
   have meaningful content, flag it.

4. **Machine learning.** Train a classifier on features: page length, keyword
   presence, similarity to known 404 templates, HTTP status code context.
   Google uses this approach.

### 5.3 Dynamic Content / JavaScript Rendering

Modern websites (SPAs — React, Angular, Vue) render content client-side with
JavaScript. The raw HTML the crawler downloads is often an empty shell:

```html
<!DOCTYPE html>
<html>
<body>
  <div id="root"></div>
  <script src="/bundle.js"></script>
</body>
</html>
```

No content. No links. Useless to a traditional crawler.

**Solution: Headless Browser Rendering**

```
┌──────────────────────────────────────────────────────────┐
│                  Rendering Pipeline                       │
│                                                          │
│  ┌────────────┐     ┌──────────────────┐                 │
│  │ Fetch HTML │────▶│ Does it need JS? │                 │
│  │ (fast)     │     │ (heuristic check)│                 │
│  └────────────┘     └────────┬─────────┘                 │
│                         yes/ \no                         │
│                        /     \                           │
│              ┌────────▼──┐  ┌▼────────────┐              │
│              │ Headless   │  │ Parse HTML  │              │
│              │ Browser    │  │ directly    │              │
│              │ (Chromium) │  │ (fast path) │              │
│              │            │  └─────────────┘              │
│              │ Load page  │                               │
│              │ Execute JS │                               │
│              │ Wait for   │                               │
│              │ network    │                               │
│              │ idle       │                               │
│              │ Extract    │                               │
│              │ rendered   │                               │
│              │ DOM        │                               │
│              └────────────┘                               │
└──────────────────────────────────────────────────────────┘
```

**Headless browser tools:**
- **Puppeteer** (Chrome/Chromium, Node.js)
- **Playwright** (Chromium/Firefox/WebKit, multiple languages)
- **Selenium** (older, heavier)

**Performance impact:**
- Traditional fetch: ~100ms per page, 10,000+ pages/sec per machine.
- Headless browser: ~2-10 seconds per page, 100-500 pages/sec per machine.
- **10-100x slower and 10-50x more memory.**

**Googlebot's Approach (Web Rendering Service — WRS):**
- Uses an evergreen (always up-to-date) Chromium instance.
- Two-pass indexing: First, index the raw HTML. Later, render with JS and
  re-index.
- Caches external resources (JS, CSS, fonts) for up to 30 days to speed up
  rendering.
- Rendering is deferred — may take hours to days after initial crawl.
- Separate rendering fleet (WRS cluster) from the fetcher fleet.

**Heuristic: Does this page need JS rendering?**
- If the raw HTML `<body>` has < 100 characters of visible text → probably
  needs rendering.
- If the page includes known SPA framework bundles (React, Angular, Vue) →
  probably needs rendering.
- If the domain has historically needed rendering → render all pages from it.

### 5.4 Very Large Pages

- Enforce 10 MB max download size.
- If Content-Length > 10 MB, skip immediately.
- If chunked transfer (no Content-Length), stream-read and abort at 10 MB.
- Log the URL and size for analysis (it might be a misconfigured server).

### 5.5 Rate Limiting (429 Too Many Requests)

```
HTTP/1.1 429 Too Many Requests
Retry-After: 60
```

When you receive a 429:
1. **Stop all requests to that domain immediately.**
2. **Parse `Retry-After` header.** It can be:
   - Seconds: `Retry-After: 120` (wait 120 seconds)
   - Date: `Retry-After: Thu, 01 Jan 2026 00:00:00 GMT`
3. **If no Retry-After:** Back off for 60 seconds minimum.
4. **Add domain to a "throttled" set** with an increased delay (e.g., 5x the
   previous delay).
5. **Gradually reduce the delay** after successful requests without 429s.

---

## 6. Contrasts: Googlebot vs. Scrapy

### 6.1 Googlebot

Google's crawler is the most sophisticated web crawler ever built.

**Architecture:**
- Distributed fetcher fleet across multiple data centers worldwide.
- Fetchers are assigned geographic regions to crawl from the closest data
  center (reduces latency, respects geo-restrictions).
- Each fetcher machine handles thousands of concurrent connections (async I/O).
- Total crawl capacity: estimated billions of pages per day.

**DNS:**
- Google operates its own public DNS (8.8.8.8) and internal DNS
  infrastructure.
- DNS resolution is essentially free — resolved from Google's global DNS
  cache.
- Can handle millions of DNS queries per second.

**JavaScript Rendering (WRS):**
- Separate service: Web Rendering Service (WRS).
- Evergreen Chromium (always latest stable version).
- Two-pass: crawl raw HTML first, render JS later (hours to days lag).
- Caches static resources (JS, CSS, images) for up to 30 days across renders.
- Rendering cluster is separate from fetcher cluster for resource isolation.

**Bot Detection Handling:**
- Googlebot's IP ranges are published and can be verified via reverse DNS.
- Sites that block Googlebot get penalized in rankings (officially or not).
- Google can detect when sites serve different content to Googlebot vs. users
  (cloaking) and penalizes this.

**Politeness:**
- Does NOT respect `Crawl-delay` in robots.txt.
- Instead, uses its own adaptive system based on server response times and
  errors.
- Site owners control crawl rate via Google Search Console.
- Automatically reduces crawl rate if a server shows signs of overload.

### 6.2 Scrapy

Scrapy is the most popular open-source web crawling framework (Python).

**Architecture:**
- Single-process, single-machine.
- Uses **Twisted** (async reactor pattern) for non-blocking I/O.
- Despite being single-process, can handle many concurrent requests via async.
- Default `CONCURRENT_REQUESTS = 16` (conservative, tunable up to hundreds).
- Default `CONCURRENT_REQUESTS_PER_DOMAIN = 8`.

**Middleware Pipeline:**
```
Request → [Downloader Middleware Chain] → HTTP Download → [Response Middleware Chain] → Spider
```
Built-in middleware handles:
- Redirect following (RedirectMiddleware)
- Retry on failure (RetryMiddleware, default 2 retries)
- Robots.txt compliance (RobotsTxtMiddleware)
- HTTP cache (HttpCacheMiddleware)
- Cookies (CookiesMiddleware)
- User-Agent rotation (UserAgentMiddleware)

**Limitations for web-scale:**
- Single machine. No built-in distributed coordination.
- Python's GIL limits CPU-bound processing (HTML parsing).
- Scrapy-Redis and Scrapy-Cluster exist for distributed setups, but they're
  community add-ons, not production-grade at Google scale.

### Comparison Table

| Aspect                 | Googlebot                          | Scrapy                             |
|------------------------|------------------------------------|------------------------------------|
| Scale                  | Billions of pages/day              | Thousands-millions of pages/day    |
| Architecture           | Distributed fleet, 1000s of machines| Single process, single machine    |
| I/O Model              | Async (custom C++ infrastructure)  | Async (Twisted reactor)           |
| Concurrency            | Millions of connections             | 16 default, tunable to ~hundreds  |
| JS Rendering           | Yes (WRS, headless Chromium)       | No (requires Splash or Playwright)|
| DNS                    | Google's global DNS infrastructure | OS resolver (or Twisted DNS)      |
| Politeness             | Adaptive (ignores Crawl-delay)     | Respects Crawl-delay, configurable|
| Redirect handling      | Custom, sophisticated              | Built-in middleware                |
| Retry logic            | Custom, per-domain adaptive        | Built-in, default 2 retries       |
| Distribution           | Native                             | Not built-in (Scrapy-Redis addon) |
| Language               | C++ / Java (internal)              | Python                            |
| Use case               | Indexing the entire web             | Targeted scraping, small crawls   |

---

## 7. End-to-End Fetch Flow: Putting It All Together

```
┌────────────────────────────────────────────────────────────────────┐
│                     COMPLETE FETCH PIPELINE                        │
│                                                                    │
│  URL from Frontier                                                │
│       │                                                            │
│       ▼                                                            │
│  ┌─────────────────────┐                                          │
│  │ 1. CHECK ROBOTS.TXT │  Cached? Use cache.                     │
│  │    (Is URL allowed?) │  Not cached? Fetch robots.txt first.    │
│  └──────────┬──────────┘  Blocked? Discard URL, done.            │
│             │ allowed                                              │
│             ▼                                                      │
│  ┌─────────────────────┐                                          │
│  │ 2. POLITENESS WAIT  │  Check per-domain AND per-IP delay.     │
│  │    (Rate limiting)   │  Sleep until enough time has passed.    │
│  └──────────┬──────────┘                                          │
│             │ ready                                                │
│             ▼                                                      │
│  ┌─────────────────────┐                                          │
│  │ 3. DNS RESOLVE      │  Check in-process cache → local         │
│  │    (Get IP address)  │  resolver → upstream DNS.               │
│  └──────────┬──────────┘  Timeout after 5s.                      │
│             │ resolved                                             │
│             ▼                                                      │
│  ┌─────────────────────┐                                          │
│  │ 4. TCP + TLS CONNECT│  Connection timeout: 5s.                │
│  │                      │  TLS for HTTPS (most of the web).      │
│  └──────────┬──────────┘                                          │
│             │ connected                                            │
│             ▼                                                      │
│  ┌─────────────────────┐                                          │
│  │ 5. SEND HTTP REQUEST│  GET /path HTTP/1.1                     │
│  │                      │  Host: example.com                      │
│  │                      │  User-Agent: MyCrawler/1.0              │
│  │                      │  Accept-Encoding: gzip, br              │
│  └──────────┬──────────┘                                          │
│             │                                                      │
│             ▼                                                      │
│  ┌─────────────────────┐                                          │
│  │ 6. READ RESPONSE    │  Check status code.                     │
│  │    HEADERS           │  Check Content-Type (HTML only).        │
│  │                      │  Check Content-Length (< 10 MB).        │
│  └──────────┬──────────┘                                          │
│             │ ok                                                   │
│             ▼                                                      │
│  ┌─────────────────────┐                                          │
│  │ 7. READ + DECOMPRESS│  Stream body. Decompress gzip/br.       │
│  │    BODY              │  Abort if > 10 MB.                      │
│  │                      │  Read timeout: 30s.                     │
│  └──────────┬──────────┘                                          │
│             │                                                      │
│             ▼                                                      │
│  ┌─────────────────────┐                                          │
│  │ 8. CHARSET → UTF-8  │  Detect encoding. Convert to UTF-8.     │
│  └──────────┬──────────┘                                          │
│             │                                                      │
│             ▼                                                      │
│  ┌─────────────────────┐                                          │
│  │ 9. HAND OFF         │  Raw HTML → Parser → Link Extractor     │
│  │    TO PARSER         │  → Content Extractor → Storage          │
│  └─────────────────────┘                                          │
│                                                                    │
│  At ANY step: timeout/error → retry queue (with backoff)          │
└────────────────────────────────────────────────────────────────────┘
```

---

## 8. Key Numbers to Remember

| Metric                            | Value                          |
|-----------------------------------|--------------------------------|
| Threads per machine (threaded)    | 1,000-10,000                   |
| Connections per machine (async)   | 50,000-100,000                 |
| DNS cache hit rate                | >90%                           |
| DNS lookup (cache miss)           | 1-100ms                        |
| TCP + TLS handshake               | 50-200ms                       |
| Typical page download             | 100-500ms                      |
| Pages/sec per machine (async)     | 5,000-20,000                   |
| Pages/sec with JS rendering       | 100-500                        |
| Memory per thread                 | ~1 MB                          |
| Memory per async connection       | ~10-50 KB                      |
| Default politeness delay          | 1 sec/domain                   |
| Max redirect chain                | 5 hops                         |
| Max download size                 | 10 MB                          |
| Max retries                       | 3-5 per URL                    |
| Exponential backoff base          | 1 second                       |
| JS rendering slowdown factor      | 10-100x vs. raw HTML fetch     |
