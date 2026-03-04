# Re-crawl Scheduling and Freshness

## The Freshness Problem

The web is not static. Pages are added, modified, and deleted every second. A search engine that serves stale results delivers poor user experience — a user clicking a result only to find the page no longer exists, or the information is outdated, loses trust in the engine. But the web is enormous (hundreds of billions of pages), and re-crawling every page every day is computationally and network-wise infeasible. The crawler must constantly answer two questions:

1. **Which pages should be re-crawled?**
2. **How often should each page be re-crawled?**

This is fundamentally a resource allocation problem under constraints. You have finite crawl capacity and an effectively infinite set of pages that could be stale. The goal is to maximize the freshness of the pages that matter most, given limited resources.

### Why This Is Hard

- A page that changed 5 minutes after you crawled it sits stale until the next crawl — you have no way to know it changed unless you check.
- Re-crawling a page that has not changed wastes bandwidth, server load, and crawler capacity.
- Different pages change at vastly different rates — a news homepage may change every minute, while a research paper PDF has not changed in a decade.
- The importance of freshness varies by page type — a stale stock price is far worse than a stale "About Us" page.

---

## Crawl Budget

Each domain gets a **crawl budget** — the maximum number of pages the crawler will fetch from that domain within a given time period. This concept exists because:

- **Politeness**: Hitting a server too hard degrades its performance for real users.
- **Efficiency**: Crawler capacity is finite and must be distributed across millions of domains.
- **Prioritization**: Not all domains are equally important.

### What Determines Crawl Budget

| Factor | Description |
|--------|-------------|
| **Domain importance** | Measured by aggregate PageRank, traffic, authority signals. Higher importance = larger budget. |
| **Server capacity** | Fast-responding servers with generous robots.txt allowances can handle more crawl traffic. Slow/overloaded servers get smaller budgets. |
| **Content change rate** | Domains whose content changes frequently need more frequent visits to stay fresh. |
| **Content quality** | Domains with thin, duplicate, or spammy content get reduced budgets — no point crawling junk more often. |
| **Crawl yield** | If past crawls of a domain mostly returned 404s, soft errors, or duplicate content, reduce budget. |

### Google's Definition

Google defines crawl budget as **"the set of URLs that Google can and wants to crawl"**, composed of two components:

- **Crawl capacity limit**: The maximum number of simultaneous connections and requests Googlebot can make to a site without degrading the site's performance. This is determined automatically based on server response times, error rates, and limits set in Search Console.
- **Crawl demand**: How much Google *wants* to crawl, based on URL popularity, staleness, and site-level events (e.g., a site-wide URL restructure triggers a surge in crawl demand).

The actual crawl budget is the intersection: what Googlebot *can* crawl (capacity) intersected with what it *wants* to crawl (demand).

---

## Change Detection Strategies

### 1. Periodic Re-crawl (Fixed Interval)

The simplest approach. Assign a fixed re-crawl interval to each page (or class of pages).

**How it works:**
- Every page gets re-crawled every N days (e.g., 7 days).
- The interval might differ by page tier (important pages every 1 day, others every 30 days).

**Pros:**
- Dead simple to implement.
- Predictable load on target servers.

**Cons:**
- Wasteful — many pages do not change between crawls.
- Misses fast-changing pages between intervals.
- Does not learn or adapt.

### 2. Adaptive Re-crawl (Change Rate Estimation)

Estimate each page's change rate from its crawl history and schedule accordingly.

**How it works:**
- Track how often a page has changed across past crawls.
- Model changes as a Poisson process with rate parameter lambda.
- Estimate: `lambda = k_changes / n_crawls`
- Schedule next crawl at: `t = 1 / lambda`
- After each crawl, update the estimate (exponential moving average handles non-stationarity).

**Example:**
- Page was crawled 10 times. Changed 5 times. lambda = 0.5 changes/crawl.
- If crawls were daily, that is 0.5 changes/day, so re-crawl every 2 days.
- Over time, if the page starts changing less, lambda decreases and the interval stretches out.

**Pros:**
- Converges to near-optimal frequency per page.
- Naturally allocates more budget to fast-changing pages.

**Cons:**
- Requires crawl history (cold start problem for new URLs).
- Poisson model assumes stationary change rate — real pages may have bursts (e.g., election day on a news site).

### 3. HTTP Conditional Requests

Use HTTP headers to ask the server if the page has changed since the last crawl.

**How it works:**
- On first crawl, store the response's `Last-Modified` timestamp and/or `ETag` header.
- On re-crawl, send:
  - `If-Modified-Since: <timestamp>` or
  - `If-None-Match: <etag>`
- If the page has not changed, the server returns **304 Not Modified** with no body.

**Pros:**
- Saves bandwidth — no page body transferred if unchanged.
- Server-authoritative — the server tells you definitively whether it changed.

**Cons:**
- Still requires an HTTP round-trip per page (network overhead, connection cost).
- Many servers do not implement conditional responses correctly.
- Dynamic pages often return 200 even when content has not meaningfully changed.

### 4. Sitemap-Based Freshness

Monitor XML sitemaps (`/sitemap.xml`) for `<lastmod>` changes.

**How it works:**
- Periodically fetch the site's sitemap.
- Compare `<lastmod>` dates with last crawl dates.
- Re-crawl only pages where `<lastmod>` is newer than the last crawl.

**Example sitemap entry:**
```xml
<url>
  <loc>https://example.com/page1</loc>
  <lastmod>2025-03-15T10:30:00+00:00</lastmod>
  <changefreq>daily</changefreq>
  <priority>0.8</priority>
</url>
```

**Pros:**
- Extremely efficient — one sitemap fetch can cover thousands of URLs.
- Gives structured metadata (priority, change frequency).

**Cons:**
- Many sites do not maintain sitemaps, or maintain them poorly.
- `<lastmod>` is often inaccurate (set to current date on every build, or never updated).
- `<changefreq>` is a hint that crawlers are free to ignore (and most do).

### 5. RSS/Atom Feed Monitoring

Subscribe to RSS or Atom feeds to discover new and updated content.

**How it works:**
- Identify RSS/Atom feeds for target sites (usually linked in HTML `<head>`).
- Poll feeds periodically (much cheaper than crawling the full site).
- New entries in the feed indicate new or updated pages — add those URLs to the crawl queue.

**Pros:**
- Very efficient for content-heavy sites (blogs, news, forums).
- Feeds typically contain only recent changes, which is exactly what you need.

**Cons:**
- Only works for sites that publish feeds (a shrinking percentage of the web).
- Feeds may not cover all pages on a site.
- Feed polling frequency becomes its own scheduling problem.

### 6. WebSub (PubSubHubbub)

A real-time push protocol. The crawler subscribes to a hub, and the hub pushes notifications when content changes.

**How it works:**
- Site publishes via a WebSub hub (specified in feed or HTTP headers).
- Crawler registers a callback URL with the hub for specific topics/feeds.
- When the publisher notifies the hub of an update, the hub pushes the new content to all subscribers.

**Pros:**
- Near-zero latency — updates arrive in real time.
- No wasted polling — you only hear about actual changes.

**Cons:**
- Requires site cooperation (must use a WebSub hub).
- Very low adoption outside the blogging/podcast ecosystem.
- Crawler must maintain a publicly accessible callback endpoint.

### Strategy Comparison

| Strategy | Latency to Detect Change | Bandwidth Cost | Implementation Complexity | Reliability | Coverage |
|----------|--------------------------|----------------|---------------------------|-------------|----------|
| Periodic re-crawl | High (up to full interval) | High (full page every time) | Very low | High (you control it) | Universal |
| Adaptive re-crawl | Medium (converges over time) | Medium (reduces wasted crawls) | Medium | High | Universal |
| HTTP conditional requests | Medium (still needs round-trip) | Low (304 = no body) | Low | Medium (server must support) | Most sites |
| Sitemap-based | Medium (depends on poll frequency) | Very low (one file for whole site) | Low | Low (sitemap accuracy varies) | Sites with sitemaps |
| RSS/Atom feeds | Low-Medium (depends on poll frequency) | Very low | Low | Medium | Sites with feeds |
| WebSub push | Very low (real-time) | Minimal | High | Medium (hub must be reliable) | Very limited |

**In practice, a production crawler combines multiple strategies.** Use WebSub/RSS where available, sitemaps as a supplement, adaptive scheduling as the core engine, and conditional requests to reduce bandwidth on every re-crawl.

---

## Change Rate Classification

Not all pages change at the same rate. Classifying pages into change-rate buckets allows the scheduler to assign appropriate re-crawl intervals without per-page tuning.

### Classification Buckets

| Bucket | Change Frequency | Example Pages | Typical Re-crawl Interval | Priority |
|--------|-----------------|---------------|---------------------------|----------|
| **Very Frequent** | Every few minutes | News homepages, stock tickers, live sports scores, trending topics | 5-15 minutes | Critical |
| **Frequent** | Every few hours | Forum threads, social media feeds, e-commerce deals pages, weather forecasts | 1-6 hours | High |
| **Moderate** | Every few days | Product pages, blog posts, Wikipedia articles, job listings | 1-7 days | Medium |
| **Slow** | Every few weeks | Documentation, corporate pages, government sites, FAQs | 1-4 weeks | Low |
| **Static** | Rarely or never | Academic papers, archived content, legal documents, historical records | 1-6 months | Very Low |

### How to Classify a Page

1. **URL pattern heuristics**: `/news/`, `/blog/`, `/archive/` give strong signals before any crawl history exists.
2. **Historical change rate**: After several crawls, compute the empirical change rate and assign to the nearest bucket.
3. **Content type signals**: HTML pages change more than PDFs. Pages with timestamps/dates in content change more than pages without.
4. **Site-level priors**: If a site's homepage changes hourly, its subpages likely change more than average too.

### Re-crawl Interval Table

| Page Importance | Very Frequent Change | Frequent Change | Moderate Change | Slow Change | Static |
|-----------------|---------------------|-----------------|-----------------|-------------|--------|
| **Critical** (top 0.1%) | 5 min | 1 hour | 1 day | 3 days | 1 week |
| **High** (top 1%) | 15 min | 4 hours | 3 days | 1 week | 2 weeks |
| **Medium** (top 10%) | 1 hour | 12 hours | 7 days | 2 weeks | 1 month |
| **Low** (remaining) | 6 hours | 1 day | 14 days | 1 month | 3 months |

The re-crawl interval is a function of both change rate and importance. A critical page that changes slowly still gets re-crawled more often than a low-importance page that changes frequently — because the cost of serving stale content is higher for important pages.

---

## Freshness Metrics

### Age

**Definition:** Time elapsed since the page was last crawled.

```
age(page) = now - last_crawl_time(page)
```

Lower age is better. A page crawled 5 minutes ago has age = 5 minutes. A page crawled 30 days ago has age = 30 days.

Age is easy to compute but does not tell you whether the page actually changed — a page crawled 30 days ago might be perfectly fresh if it has not changed.

### Freshness (Binary)

**Definition:** Is the cached copy of the page identical to the live version?

```
freshness(page) = 1 if cached_copy == live_copy, else 0
```

This is the ground truth metric, but you cannot compute it without actually fetching the live page (which is the very operation you are trying to schedule).

In practice, freshness is estimated probabilistically:

```
estimated_freshness(page) = e^(-lambda * age)
```

Where lambda is the page's estimated change rate. A page with lambda = 1 change/day and age = 2 days has estimated freshness = e^(-2) = 0.135 (13.5% chance it is still fresh).

### Average Freshness

**Definition:** The fraction of pages in the index whose cached copies are current.

```
average_freshness = (1/N) * sum(freshness(page_i)) for all pages
```

**Targets:**
- Critical pages: 95%+ average freshness
- High-importance pages: 90%+
- Medium-importance pages: 80%+
- Low-importance pages: 60%+
- Overall index: 85%+

### Weighted Average Freshness

Not all pages matter equally. Weight freshness by importance:

```
weighted_freshness = sum(importance_i * freshness_i) / sum(importance_i)
```

This is the metric to optimize. A system with 99% freshness on unimportant pages and 50% freshness on important pages is worse than one with 70% freshness on unimportant pages and 95% freshness on important pages.

### Staleness Cost

**Definition:** The penalty incurred by serving a stale copy of a page.

Not all staleness is equal:
- A news article that is 1 hour stale during a breaking event has very high staleness cost.
- A documentation page that is 1 week stale has low staleness cost (it probably has not changed).
- A product page that is 1 day stale might have moderate staleness cost (price/availability might differ).

```
staleness_cost(page) = importance(page) * change_sensitivity(page) * age(page)
```

Where `change_sensitivity` captures how much users care about freshness for this type of content. News: very high. Archives: very low.

**The scheduler's objective is to minimize total staleness cost across all pages, subject to the crawl budget constraint.**

---

## Crawl Budget Allocation Algorithms

Given a total crawl budget of B pages per day distributed across D domains, how should you allocate?

### 1. Uniform Allocation

```
budget(domain_i) = B / D
```

Every domain gets the same budget.

**Pros:** Simple, fair.
**Cons:** Completely ignores importance, change rate, and size. A tiny personal blog gets the same budget as Wikipedia.

### 2. Proportional to Site Size

```
budget(domain_i) = B * (size_i / total_size)
```

Larger sites get more crawl budget.

**Pros:** Ensures larger sites (which likely have more important content) get adequate coverage.
**Cons:** Large does not mean important. A massive content farm should not get more budget than a smaller but more authoritative site.

### 3. Proportional to Change Rate x Importance (Best Approach)

```
budget(domain_i) = B * (change_rate_i * importance_i) / sum(change_rate_j * importance_j)
```

Allocate more budget to domains that are both important and fast-changing.

**Rationale:** The expected freshness gain from crawling a domain is proportional to how often its pages change (more changes = more staleness to fix) weighted by how much that freshness matters (importance).

### 4. Optimization Formulation

The formal optimization problem:

```
Maximize:   sum(importance_i * freshness_i)  for all pages i
Subject to: sum(crawl_frequency_i) <= B      (total budget constraint)
            crawl_frequency_i >= 0            (non-negativity)
            crawl_frequency_i <= max_rate_i   (politeness per domain)
```

Where `freshness_i = e^(-lambda_i / crawl_frequency_i)` (exponential freshness decay between crawls).

**Greedy Approximation:**

Since the exact optimization is complex, a greedy algorithm works well in practice:

1. For each page, compute a priority score:
   ```
   priority(page) = importance(page) * change_rate(page) / current_freshness(page)
   ```
2. Sort all pages by priority (descending).
3. Allocate crawl slots in priority order until the budget is exhausted.
4. Respect per-domain politeness limits — if a domain's allocation is maxed out, skip to the next page from a different domain.

This greedy approach maximizes the marginal freshness gain per crawl. Pages that are important, fast-changing, and currently stale get crawled first.

### 5. Multi-Queue Scheduling

In practice, the scheduler maintains multiple priority queues:

| Queue | Contents | Re-crawl Trigger |
|-------|----------|-------------------|
| **Urgent** | Pages detected as changed (via WebSub, RSS, sitemap) | Immediate |
| **High Priority** | Important pages past their re-crawl interval | Within minutes |
| **Normal** | Regular pages due for re-crawl | Within hours |
| **Background** | Low-priority pages, discovery crawls | When capacity allows |
| **Retry** | Pages that failed on last attempt (5xx, timeout) | Exponential backoff |

The URL frontier pulls from these queues in priority order, always respecting per-domain rate limits.

---

## Googlebot vs. Common Crawl: Contrasting Approaches

### Googlebot

Googlebot's re-crawl scheduling is deeply integrated with the search ranking system:

- **Ranking-driven priority**: Pages that appear in more search results (i.e., pages users actually see) get higher re-crawl priority. If a page is the #1 result for a popular query, it must be fresh.
- **Search Console integration**: Site owners can use Google Search Console to request indexing of specific URLs, temporarily boosting their crawl priority.
- **Crawl frequency range**: Popular pages on major news sites are re-crawled every few minutes. Stable pages on low-traffic sites may go weeks between crawls.
- **Budget components**: Crawl capacity limit (how fast Googlebot can hit the server without harm) intersected with crawl demand (how much Google wants the content).
- **Adaptive and real-time**: Googlebot monitors server response times in real time and backs off if the server slows down. It also accelerates crawling when it detects a site has undergone a major update (e.g., domain migration, sitemap refresh).
- **Quality signals**: Googlebot reduces crawl budget for low-quality pages (thin content, duplicate content, soft 404s). It actively avoids wasting resources on crawl traps and infinite URL spaces.

**Summary:** Googlebot treats re-crawl scheduling as an optimization problem — maximize the freshness of the pages that matter most to search quality, subject to server capacity constraints.

### Common Crawl

Common Crawl takes a fundamentally different approach:

- **Monthly full-web crawls**: Each crawl is a single, large snapshot of the web. There is no incremental re-crawl between snapshots.
- **No freshness goal**: The objective is archival completeness, not freshness. Each monthly crawl is an independent dataset.
- **Broad coverage over depth**: Common Crawl prioritizes visiting as many unique domains and pages as possible, rather than re-visiting the same pages frequently.
- **No prioritization by importance**: All pages are treated roughly equally. There is no concept of "this page appears in search results, so it must be fresh."
- **Downstream consumers handle freshness**: Researchers, companies, and tools that use Common Crawl data are responsible for merging multiple crawl snapshots if they need change detection.

**Summary:** Common Crawl is a breadth-first archival crawler. Freshness is not a design goal — completeness and accessibility of web data for research are.

### Side-by-Side Comparison

| Dimension | Googlebot | Common Crawl |
|-----------|-----------|--------------|
| **Primary goal** | Search result freshness | Archival completeness |
| **Re-crawl strategy** | Continuous, adaptive, priority-based | Monthly batch, no incremental |
| **Freshness target** | Minutes for top pages, weeks for tail | Not a goal |
| **Budget allocation** | Importance x change rate x crawl demand | Uniform / breadth-first |
| **Per-domain politeness** | Adaptive (monitors server health) | Fixed rate limits |
| **Change detection** | Multiple strategies (sitemaps, RSS, conditional, adaptive) | None (each crawl is independent) |
| **Site owner interaction** | Search Console, robots.txt, sitemaps | robots.txt only |
| **Scale** | Hundreds of billions of pages, continuous | ~3 billion pages per monthly crawl |
| **Data access** | Private (powers Google Search) | Public (free datasets on S3) |

---

## Putting It All Together: The Re-crawl Pipeline

A production re-crawl scheduler combines all of the above into a pipeline:

```
1. CHANGE SIGNALS arrive:
   - WebSub push notifications        --> Urgent queue
   - RSS feed new entries              --> High priority queue
   - Sitemap <lastmod> updates         --> High priority queue

2. ADAPTIVE SCHEDULER runs continuously:
   - For each known page, compute:
     priority = importance * change_rate / estimated_freshness
   - Pages past their re-crawl deadline --> Normal queue
   - Pages approaching deadline         --> Background queue

3. URL FRONTIER pulls from queues:
   Urgent > High Priority > Normal > Background > Retry
   - Respects per-domain rate limits
   - Respects robots.txt crawl-delay

4. FETCHER makes requests:
   - Uses If-Modified-Since / If-None-Match when possible
   - 304 Not Modified --> Update last_crawl_time, no reprocessing
   - 200 OK with new content --> Full reprocessing pipeline
   - 4xx/5xx --> Retry queue with exponential backoff

5. CHANGE RATE UPDATER:
   - If content changed: increment change counter, recalculate lambda
   - If content unchanged: increment crawl counter, recalculate lambda
   - Adjust page's change rate bucket if needed

6. BUDGET MONITOR:
   - Track crawl budget usage per domain
   - Throttle or boost domains based on remaining budget
   - Generate alerts if high-importance pages are consistently stale
```

### Key Takeaways

1. **Freshness is a resource allocation problem.** You cannot crawl everything as often as you want. The art is in deciding what to prioritize.
2. **Combine multiple change detection strategies.** No single strategy covers all cases. Push notifications for real-time, sitemaps for structured sites, adaptive scheduling for everything else.
3. **Weight freshness by importance.** A stale copy of a high-traffic page costs more than a stale copy of a rarely-visited page. Always optimize for weighted freshness.
4. **The Poisson model is a good starting point** for change rate estimation, but real-world pages have bursty, non-stationary change patterns. Use exponential moving averages to adapt.
5. **Crawl budget is bidirectional.** It is not just how much the crawler wants to fetch — it is also how much the server can handle. Respect server capacity to maintain long-term crawl access.
