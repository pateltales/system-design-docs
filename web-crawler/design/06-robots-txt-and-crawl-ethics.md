# 06 — robots.txt, Politeness, and Crawl Ethics

> A well-behaved crawler is a welcomed crawler. An aggressive one gets IP-banned, blacklisted, and potentially sued.

---

## 1. robots.txt Standard (Robots Exclusion Protocol)

### 1.1 What It Is

`robots.txt` is a plain-text file placed at the root of a website that tells crawlers which paths they may or may not access. It was introduced informally in 1994 by Martijn Koster and later codified as an Internet standard (RFC 9309, published September 2022).

- **Location**: Always at `https://domain.com/robots.txt` (exactly this path, at the root).
- **Format**: Line-based, case-sensitive for paths, case-insensitive for directive names.
- **Fetched once per domain**, then cached (typically 24 hours).

### 1.2 Directives

```
# Example robots.txt
User-agent: *
Disallow: /admin/
Disallow: /private/
Disallow: /tmp/
Allow: /admin/public/
Crawl-delay: 5

User-agent: Googlebot
Disallow: /no-google/
Allow: /

Sitemap: https://example.com/sitemap.xml
Sitemap: https://example.com/sitemap-news.xml
```

| Directive | Meaning |
|---|---|
| `User-agent: *` | Rules apply to all crawlers |
| `User-agent: Googlebot` | Rules apply only to Googlebot |
| `Disallow: /admin/` | Do not crawl any URL starting with `/admin/` |
| `Allow: /admin/public/` | Exception — this sub-path IS allowed |
| `Crawl-delay: 5` | Wait 5 seconds between requests (not in original standard, but widely supported by Bing, Yandex, etc. Google ignores it — use Search Console instead) |
| `Sitemap: <url>` | Points crawler to XML sitemap(s) for discovery |

### 1.3 Matching Rules

Matching is **prefix-based** on the URL path:

| Rule | URL | Match? |
|---|---|---|
| `Disallow: /admin/` | `/admin/settings` | Yes (prefix match) |
| `Disallow: /admin/` | `/administrator` | No (`/admin/` != `/admini...`) |
| `Disallow: /admin` | `/administrator` | Yes (prefix match — no trailing slash) |
| `Allow: /admin/public/` | `/admin/public/page.html` | Yes |

**Priority when Allow and Disallow both match the same URL:**

- **Google's approach (longest-match-wins)**: The rule with the longer path pattern takes priority. Since `/admin/public/` (15 chars) is longer than `/admin/` (7 chars), the Allow wins.
- **Traditional approach**: Some parsers use first-match or most-specific-group. RFC 9309 codified the longest-match approach.

**Wildcards (Google extensions, widely adopted):**

| Pattern | Meaning | Example Match |
|---|---|---|
| `Disallow: /private/*/data` | `*` matches any sequence of characters | `/private/user123/data` |
| `Disallow: /*.pdf$` | `$` anchors to end of URL | `/docs/report.pdf` but NOT `/docs/report.pdf?v=2` |
| `Disallow: /search?q=*&page=` | Wildcard in query string | `/search?q=cats&page=3` |

**Important**: These wildcards are not part of the original 1994 spec. They were introduced by Google and are now supported by most major crawlers. Simple parsers may not handle them.

### 1.4 Caching and Error Handling

The crawler fetches `robots.txt` on **first visit** to a domain, then caches it.

| HTTP Status of robots.txt | Crawler Behavior | Rationale |
|---|---|---|
| **200 OK** | Parse and obey the rules | Normal case |
| **3xx Redirect** | Follow redirect (up to 5 hops), then parse final response | Standard redirect handling |
| **404 Not Found** | **No restrictions** — crawl everything | Site owner chose not to create one, assume open |
| **403 Forbidden** | **Disallow all** — crawl nothing (conservative) | Access denied suggests the site doesn't want crawlers |
| **5xx Server Error** | **Allow all** (optimistic, per Google) OR retry later | Temporary failure shouldn't permanently block crawling |
| **Timeout / Unreachable** | Retry with backoff, then treat as temporary allow | Network issues are transient |

**Cache duration:**
- Default: **24 hours** (Google's documented behavior).
- Respect HTTP cache headers (`Cache-Control`, `Expires`) if present.
- If the site is returning 5xx for robots.txt, Google will use the **last successfully cached version** for a reasonable period before falling back to "allow all."

### 1.5 Google's Open-Source robots.txt Parser

In **2019**, Google open-sourced their production robots.txt parser:

- **Repository**: [google/robotstxt](https://github.com/google/robotstxt) on GitHub
- **Language**: C++ library
- **Why it matters**: This is the **reference implementation** — the exact logic Googlebot uses.
- **Edge cases it handles** that simpler parsers miss:
  - BOM (Byte Order Mark) at start of file
  - Mixed line endings (CR, LF, CRLF)
  - Wildcard patterns (`*` and `$`)
  - Longest-match-wins priority resolution
  - UTF-8 encoded paths and percent-encoded equivalences
  - Groups with multiple `User-agent` lines

### 1.6 File Size Limit

Google **truncates** `robots.txt` at **500 KiB** (512,000 bytes). Anything beyond that limit is ignored. This prevents abuse (a malicious site could serve a multi-gigabyte robots.txt to waste crawler resources).

Practical implication: Keep your robots.txt concise. If you need hundreds of Disallow rules, consider restructuring your URL scheme instead.

### 1.7 Complete Realistic Example

```
# robots.txt for a large e-commerce site

# Default rules for all crawlers
User-agent: *
Disallow: /cart/
Disallow: /checkout/
Disallow: /account/
Disallow: /api/
Disallow: /search?*sort=
Disallow: /tmp/
Disallow: /*.json$
Allow: /api/public/
Crawl-delay: 2

# Google gets more access (no crawl-delay — managed via Search Console)
User-agent: Googlebot
Disallow: /cart/
Disallow: /checkout/
Disallow: /account/
Allow: /

# Block AI training crawlers entirely
User-agent: GPTBot
Disallow: /

User-agent: CCBot
Disallow: /

User-agent: anthropic-ai
Disallow: /

# Sitemaps
Sitemap: https://shop.example.com/sitemap-index.xml
Sitemap: https://shop.example.com/sitemap-products.xml
Sitemap: https://shop.example.com/sitemap-categories.xml
```

---

## 2. Meta robots Tags (Per-Page Directives)

While `robots.txt` controls access at the **path level**, meta robots tags provide **per-page** directives embedded in the HTML itself.

### 2.1 HTML Meta Tag

```html
<head>
  <meta name="robots" content="noindex, nofollow">
</head>
```

| Directive | Meaning |
|---|---|
| `noindex` | Do not add this page to the search index |
| `nofollow` | Do not follow any links on this page |
| `noarchive` | Do not show a cached copy in search results |
| `nosnippet` | Do not show a text snippet or video preview in search results |
| `noimageindex` | Do not index images on this page |
| `max-snippet: 50` | Limit text snippet to 50 characters |
| `max-image-preview: large` | Allow large image previews |
| `unavailable_after: 2025-12-31` | Remove from index after this date |

### 2.2 Key Distinction from robots.txt

**The crawler still downloads the page.** It must fetch the HTML, parse it, and read the meta tag before it can obey it. This is fundamentally different from `robots.txt`, which prevents the fetch entirely.

```
robots.txt: "Don't come to this door"
meta robots: "Come in, read the sign on the wall, and act accordingly"
```

This matters for crawl budget — the page still costs a request even if it says `noindex`.

### 2.3 Targeting Specific Crawlers

```html
<!-- Only affects Googlebot -->
<meta name="googlebot" content="noindex">

<!-- Affects all crawlers -->
<meta name="robots" content="nofollow">
```

### 2.4 X-Robots-Tag HTTP Header

The same directives can be delivered via HTTP response headers, which works for **non-HTML content** (PDFs, images, JSON responses):

```
HTTP/1.1 200 OK
Content-Type: application/pdf
X-Robots-Tag: noindex, nofollow
```

```
HTTP/1.1 200 OK
Content-Type: image/jpeg
X-Robots-Tag: googlebot: noindex
```

This is the only way to apply robots directives to files that don't have an HTML `<head>`.

---

## 3. HTTP Headers for Crawl Control

### 3.1 Retry-After

When a server rate-limits a crawler (429) or is temporarily down (503), it can include:

```
HTTP/1.1 429 Too Many Requests
Retry-After: 120
```

or

```
HTTP/1.1 503 Service Unavailable
Retry-After: Wed, 23 Oct 2024 07:28:00 GMT
```

A well-behaved crawler **must** honor this header:
- Parse the value (seconds or absolute date).
- Put this domain in a "cooldown" state.
- Do not send any requests to this domain until the Retry-After period expires.
- Resume crawling at a reduced rate after the cooldown.

### 3.2 X-Robots-Tag

As described above — same semantics as `<meta name="robots">` but delivered as an HTTP header. Useful for:
- PDFs, images, videos, and other binary content
- API responses you don't want indexed
- Responses served through CDNs where you can add headers but not modify body content

---

## 4. Crawl Politeness Best Practices

### 4.1 Identify Yourself

```
User-Agent: MyCompanyCrawler/1.0 (+https://mycompany.com/crawler-info; crawler@mycompany.com)
```

A good User-Agent string includes:
- **Crawler name and version** — so the site owner knows who is crawling.
- **Info URL** — a page explaining what the crawler does, why it crawls, and how to opt out.
- **Contact email** — so site owners can reach out with issues.

Never disguise your crawler as a browser (`Mozilla/5.0 ...`). This is deceptive and will get you blacklisted.

### 4.2 The Politeness Checklist

| Practice | Details |
|---|---|
| Respect robots.txt completely | Parse it correctly, cache it, re-fetch periodically |
| Respect Crawl-delay | If specified, wait that many seconds between requests to the domain |
| Default rate: 1 req/sec per domain | Conservative but universally safe baseline |
| Adaptive rate limiting | If server responds slowly, back off. If fast, you can cautiously increase (but never exceed what robots.txt allows) |
| Honor 429 and Retry-After | Back off immediately and wait the specified duration |
| Honor 503 | Server is overloaded — stop crawling this domain temporarily |
| Avoid peak hours | If possible, crawl at night in the target's timezone |
| Provide opt-out mechanism | Contact page, email, or robots.txt instructions |
| Use conditional requests | Send `If-Modified-Since` or `If-None-Match` to avoid re-downloading unchanged pages |
| Limit concurrent connections | No more than 1-2 parallel connections per domain |

### 4.3 Rate Limiting Architecture

```
Per-domain rate limiter:
  ┌─────────────────────────────────────────────┐
  │  Domain: example.com                        │
  │  robots.txt Crawl-delay: 3 seconds          │
  │  Last request: 2024-10-23T14:00:01Z         │
  │  Consecutive errors: 0                      │
  │  Current rate: 1 req / 3 sec                │
  │  Status: ACTIVE                             │
  ├─────────────────────────────────────────────┤
  │  Domain: fragile-blog.org                   │
  │  robots.txt Crawl-delay: 10 seconds         │
  │  Last request: 2024-10-23T14:00:05Z         │
  │  Consecutive errors: 2                      │
  │  Current rate: 1 req / 20 sec (backed off)  │
  │  Status: THROTTLED                          │
  ├─────────────────────────────────────────────┤
  │  Domain: down-site.net                      │
  │  Last request: 2024-10-23T13:55:00Z         │
  │  Consecutive 5xx: 5                         │
  │  Retry-After: 600 seconds                   │
  │  Status: COOLDOWN until 14:05:00            │
  └─────────────────────────────────────────────┘
```

### 4.4 Adaptive Backoff Strategy

```
On successful response (2xx):
    consecutive_errors = 0
    maintain or slightly increase rate (never exceed Crawl-delay)

On 429 Too Many Requests:
    if Retry-After header present:
        wait(Retry-After)
    else:
        wait(current_delay * 2)    # exponential backoff
    reduce crawl rate by 50%

On 5xx Server Error:
    consecutive_errors += 1
    wait(min(base_delay * 2^consecutive_errors, max_backoff))
    if consecutive_errors > 10:
        mark domain as COOLDOWN for 1 hour

On timeout:
    treat as 5xx (server overwhelmed)
```

---

## 5. Ethical Considerations

### 5.1 Copyright

Crawling publicly accessible information is **generally legal** in most jurisdictions — the act of fetching a public page is similar to a user visiting it in a browser. However, **storing, reproducing, and redistributing** copyrighted content raises significant legal issues.

**Key cases:**

| Case | Year | Outcome | Significance |
|---|---|---|---|
| **Copiepresse v Google** | 2007 | Belgian court ruled Google must remove cached copies of Belgian newspaper articles | Caching/displaying copyrighted content without permission can violate copyright |
| **LinkedIn v hiQ Labs** | 2022 | US Ninth Circuit ruled scraping publicly available LinkedIn profiles was not a CFAA violation | Accessing public data is not "unauthorized access" under CFAA |
| **Authors Guild v Google** | 2015 | US Supreme Court declined to hear appeal — Google Books scanning ruled fair use | Transformative use (search index, snippets) of copyrighted works can be fair use |

**For a web crawler designer:**
- Crawling and indexing: Generally OK for public content.
- Storing full copies: Legal risk — store only what you need (snippets, metadata, hashes).
- Redistributing content: High legal risk without license or fair use defense.
- The legal landscape is **actively evolving**, especially around AI training data.

### 5.2 Personal Data (GDPR / CCPA)

Crawled pages may contain **personally identifiable information (PII)**: names, emails, phone numbers, addresses.

- **GDPR (EU)**: Processing personal data requires a legal basis. "Legitimate interest" may apply for search engines, but you must:
  - Conduct a legitimate interest assessment.
  - Provide a mechanism for individuals to request removal.
  - Not process sensitive categories (health, religion, etc.) without explicit consent.
- **CCPA (California)**: Consumers have the right to know what data is collected and to request deletion.
- **Practical approach**: Avoid storing PII. If your crawler encounters pages with personal data, process minimally and provide removal mechanisms.

### 5.3 Denial of Service

An aggressive crawler can **unintentionally DDoS** a website, especially small sites running on limited infrastructure.

```
Scenario: Crawling a small WordPress blog

  Bad:  100 concurrent requests → site goes down
        Site owner's hosting bill spikes
        Other real users can't access the site

  Good: 1 request every 2 seconds → imperceptible load
        Site stays up for real users
        Crawler still gets all pages (just takes longer)
```

Politeness isn't just ethics — it's **practical**. A crashed site gives you errors, wastes your resources, and gets your IP banned.

### 5.4 Opt-Out Mechanisms

A responsible crawler provides multiple ways for site owners to opt out:

1. **robots.txt** — the standard mechanism. Your crawler must respect it.
2. **Email contact** — listed in your User-Agent string.
3. **Web form** — on your crawler info page.
4. **IP block** — site owners can block your crawler's IP range (you should publish it).
5. **Meta robots tags** — per-page opt-out that your crawler should honor.

---

## 6. Contrasts: Good vs. Bad Actors

### 6.1 Googlebot (The Gold Standard)

| Aspect | Googlebot Behavior |
|---|---|
| **robots.txt** | Strictly respects it (Google helped create the standard) |
| **Identification** | Clear User-Agent: `Googlebot/2.1 (+http://www.google.com/bot.html)` |
| **Rate control** | Adaptive crawl rate based on server response time. Site owners can adjust via Google Search Console |
| **Crawl budget** | Allocates a per-domain "crawl budget" — won't over-crawl even large sites |
| **Transparency** | Publishes IP ranges, documentation, and behavior details |
| **Owner tools** | Google Search Console: view crawl stats, request re-crawling, see errors, adjust crawl rate |
| **Cache/index control** | Fully respects meta robots, X-Robots-Tag, canonical tags |
| **Verification** | IP ranges published — site owners can verify a request is genuinely from Googlebot via reverse DNS |

### 6.2 Aggressive Scrapers / Bad Actors

| Aspect | Bad Actor Behavior |
|---|---|
| **robots.txt** | Completely ignored |
| **Identification** | Fake User-Agents (pretend to be a browser or Googlebot) |
| **Rate control** | No delay — as fast as possible, hundreds of concurrent connections |
| **Crawl budget** | None — scrape everything, exhaust the server |
| **Transparency** | Rotate IP addresses, use proxies, hide identity |
| **Owner tools** | None — no contact, no opt-out |
| **Impact** | Server overload, inflated bandwidth costs, stolen content |
| **Legality** | Often violates Terms of Service, potentially violates CFAA, GDPR, copyright law |

### 6.3 Summary Comparison

```
                        Googlebot              Aggressive Scraper
                     ──────────────         ──────────────────────
  robots.txt         Respected              Ignored
  User-Agent         Honest                 Spoofed
  Rate               Adaptive, polite       Maximum, no delay
  Contact            Published              Hidden
  IP addresses       Published ranges       Rotating proxies
  Site owner tools   Search Console         None
  Legal status       Welcomed               Banned / sued
  Ethical standing   Industry standard      Unethical
```

---

## 7. Implementation Checklist for Your Crawler

When building a web crawler, ensure you implement all of the following:

```
[x] robots.txt parser
    - Fetch and cache per domain (24h default)
    - Handle wildcards (* and $)
    - Implement longest-match-wins for Allow/Disallow conflicts
    - Handle HTTP error codes correctly (see table in 1.4)
    - Respect file size limits (truncate at 500 KiB)

[x] Rate limiter
    - Per-domain rate limiting
    - Default 1 req/sec
    - Respect Crawl-delay directive
    - Adaptive backoff on errors
    - Honor 429 + Retry-After

[x] Identification
    - Descriptive User-Agent with info URL and contact
    - Published IP ranges
    - Crawler info page explaining purpose and opt-out

[x] Meta robots support
    - Parse <meta name="robots"> tags
    - Parse X-Robots-Tag headers
    - Honor noindex, nofollow, noarchive

[x] Conditional fetching
    - Send If-Modified-Since / If-None-Match
    - Cache ETags and Last-Modified values
    - Skip re-downloading unchanged pages

[x] Opt-out mechanisms
    - robots.txt (automatic)
    - Contact email for manual requests
    - IP range publication for firewall blocking
```

---

## 8. Quick Reference: robots.txt Error Handling Table

| HTTP Status Code | Category | Crawler Action | Reasoning |
|---|---|---|---|
| 200 | Success | **Parse and obey rules** | Normal — rules are available |
| 301 / 302 | Redirect | **Follow redirect, parse final response** | robots.txt may have moved |
| 404 | Not Found | **No restrictions — crawl freely** | Site chose not to restrict crawlers |
| 403 | Forbidden | **Disallow ALL — crawl nothing** | Access denied = conservative assumption |
| 410 | Gone | **No restrictions — crawl freely** | Explicitly removed = no restrictions intended |
| 429 | Rate Limited | **Retry later with backoff** | Temporary — don't assume permanent restriction |
| 500 | Server Error | **Allow all (optimistic)** | Temporary failure, use last cached version if available |
| 502 / 503 | Server Unavailable | **Allow all (optimistic), retry later** | Transient issue — re-fetch robots.txt soon |
| Timeout | Unreachable | **Retry with backoff, then allow all** | Network issue — not an intentional restriction |

---

*Next: [07 — Deduplication and Content Processing](07-deduplication-and-content-processing.md)*
