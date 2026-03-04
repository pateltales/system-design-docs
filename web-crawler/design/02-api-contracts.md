# Web Crawler -- API Contracts

> Every boundary in the system is defined here: external operator-facing APIs
> and internal service-to-service contracts. Each entry shows method, path,
> request/response shapes, error codes, and design rationale.

---

## Table of Contents

1. [Crawl Management APIs (External)](#1-crawl-management-apis-external)
2. [URL Frontier APIs (Internal)](#2-url-frontier-apis-internal)
3. [Fetcher APIs (Internal)](#3-fetcher-apis-internal)
4. [Parser / Extractor APIs (Internal)](#4-parser--extractor-apis-internal)
5. [Content Store APIs (Internal)](#5-content-store-apis-internal)
6. [robots.txt Management APIs (Internal)](#6-robotstxt-management-apis-internal)
7. [DNS Resolution APIs (Internal)](#7-dns-resolution-apis-internal)
8. [Monitoring / Metrics APIs (External)](#8-monitoring--metrics-apis-external)
9. [Contrast with Googlebot](#9-contrast-with-googlebot)
10. [Contrast with Common Crawl](#10-contrast-with-common-crawl)
11. [Interview Subset -- What to Focus On](#11-interview-subset--what-to-focus-on)

---

## 1. Crawl Management APIs (External)

These are the operator-facing APIs. A human or automation pipeline submits
crawl jobs, monitors progress, and controls execution. Think of this as the
"control plane" of the crawler.

---

### 1.1 Submit a New Crawl Job

| Field | Value |
|-------|-------|
| **Method** | `POST` |
| **Path** | `/crawl/jobs` |
| **Auth** | API key (header `X-Api-Key`) |
| **Idempotency** | Client may supply `Idempotency-Key` header |

**Request Body**

```json
{
  "name": "news-sites-daily",
  "seedUrls": [
    "https://www.bbc.com",
    "https://www.reuters.com",
    "https://www.apnews.com"
  ],
  "configuration": {
    "depthLimit": 5,
    "maxPagesPerDomain": 10000,
    "crawlDelayMs": 1000,
    "userAgent": "MyCrawler/1.0 (+https://example.com/bot)",
    "requestTimeoutMs": 30000,
    "domainWhitelist": ["bbc.com", "reuters.com", "apnews.com"],
    "domainBlacklist": [],
    "crawlRatePerSecond": 10,
    "retryPolicy": {
      "maxRetries": 3,
      "backoffMs": [1000, 5000, 15000],
      "retryableStatusCodes": [429, 500, 502, 503, 504]
    },
    "respectRobotsTxt": true,
    "followRedirects": true,
    "maxRedirects": 5,
    "allowedContentTypes": ["text/html", "application/xhtml+xml"],
    "maxContentLengthBytes": 10485760
  },
  "priority": "HIGH",
  "scheduleCron": null,
  "callbackUrl": "https://example.com/webhooks/crawl-complete"
}
```

**Response -- 201 Created**

```json
{
  "jobId": "cj-20240115-a8f3c",
  "name": "news-sites-daily",
  "status": "QUEUED",
  "priority": "HIGH",
  "seedUrlCount": 3,
  "createdAt": "2024-01-15T10:30:00Z",
  "estimatedStartTime": "2024-01-15T10:30:05Z",
  "links": {
    "self": "/crawl/jobs/cj-20240115-a8f3c",
    "status": "/crawl/jobs/cj-20240115-a8f3c",
    "pause": "/crawl/jobs/cj-20240115-a8f3c/pause",
    "cancel": "/crawl/jobs/cj-20240115-a8f3c"
  }
}
```

**Error Responses**

| Status | Reason |
|--------|--------|
| `400` | Missing `seedUrls`, invalid depth/rate, conflicting whitelist+blacklist |
| `409` | Duplicate `Idempotency-Key` with different body |
| `422` | Seed URLs unreachable or malformed |
| `429` | Too many concurrent jobs for this API key |

---

### 1.2 Get Job Status

| Field | Value |
|-------|-------|
| **Method** | `GET` |
| **Path** | `/crawl/jobs/{jobId}` |

**Response -- 200 OK**

```json
{
  "jobId": "cj-20240115-a8f3c",
  "name": "news-sites-daily",
  "status": "RUNNING",
  "priority": "HIGH",
  "createdAt": "2024-01-15T10:30:00Z",
  "startedAt": "2024-01-15T10:30:05Z",
  "progress": {
    "urlsDiscovered": 84230,
    "urlsFetched": 12450,
    "urlsFailed": 87,
    "urlsInQueue": 71693,
    "domainsVisited": 3,
    "pagesPerSecond": 8.7,
    "bytesDownloaded": 2147483648,
    "duplicatesSkipped": 1203
  },
  "configuration": { "...same as submitted..." },
  "errorBreakdown": {
    "TIMEOUT": 42,
    "HTTP_4XX": 18,
    "HTTP_5XX": 15,
    "DNS_FAILURE": 8,
    "CONNECTION_REFUSED": 4
  }
}
```

**Status Values**

| Status | Description |
|--------|-------------|
| `QUEUED` | Accepted, waiting for worker capacity |
| `RUNNING` | Actively crawling |
| `PAUSED` | Operator paused; queue state preserved |
| `COMPLETED` | All reachable URLs within limits have been crawled |
| `CANCELLED` | Operator cancelled; partial results available |
| `FAILED` | Unrecoverable error (e.g., all seeds unreachable) |

---

### 1.3 Pause a Job

| Field | Value |
|-------|-------|
| **Method** | `PUT` |
| **Path** | `/crawl/jobs/{jobId}/pause` |

**Request Body** -- (empty, or optional reason)

```json
{
  "reason": "Target site reporting elevated error rates"
}
```

**Response -- 200 OK**

```json
{
  "jobId": "cj-20240115-a8f3c",
  "status": "PAUSED",
  "pausedAt": "2024-01-15T11:45:00Z",
  "reason": "Target site reporting elevated error rates",
  "note": "In-flight fetches will complete; no new URLs will be dequeued."
}
```

| Status | Reason |
|--------|--------|
| `404` | Job not found |
| `409` | Job is not in RUNNING state (already paused, completed, etc.) |

---

### 1.4 Resume a Job

| Field | Value |
|-------|-------|
| **Method** | `PUT` |
| **Path** | `/crawl/jobs/{jobId}/resume` |

**Response -- 200 OK**

```json
{
  "jobId": "cj-20240115-a8f3c",
  "status": "RUNNING",
  "resumedAt": "2024-01-15T12:00:00Z",
  "pauseDurationSeconds": 900
}
```

| Status | Reason |
|--------|--------|
| `409` | Job is not in PAUSED state |

---

### 1.5 Cancel (Delete) a Job

| Field | Value |
|-------|-------|
| **Method** | `DELETE` |
| **Path** | `/crawl/jobs/{jobId}` |

**Response -- 200 OK**

```json
{
  "jobId": "cj-20240115-a8f3c",
  "status": "CANCELLED",
  "cancelledAt": "2024-01-15T12:05:00Z",
  "partialResults": {
    "urlsFetched": 12450,
    "contentStorePrefix": "store://cj-20240115-a8f3c/"
  },
  "note": "Content already stored is retained for 30 days unless explicitly purged."
}
```

| Status | Reason |
|--------|--------|
| `404` | Job not found |
| `409` | Job already COMPLETED or CANCELLED |

---

### 1.6 Configuration Reference

All fields in the `configuration` block:

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `depthLimit` | int | `5` | Max link-follow depth from seed URLs. 0 = seeds only. |
| `maxPagesPerDomain` | int | `10000` | Stop crawling a domain after this many pages. Prevents runaway on huge sites. |
| `crawlDelayMs` | int | `1000` | Minimum gap between requests to the same domain. Overridden by robots.txt Crawl-delay if larger. |
| `userAgent` | string | `"WebCrawler/1.0"` | User-Agent header sent with every request. Must include contact info per convention. |
| `requestTimeoutMs` | int | `30000` | Per-request timeout covering DNS + connect + transfer. |
| `domainWhitelist` | string[] | `[]` | If non-empty, **only** these domains are crawled. Mutually exclusive with blacklist. |
| `domainBlacklist` | string[] | `[]` | These domains are never crawled. Ignored if whitelist is set. |
| `crawlRatePerSecond` | float | `10` | Global rate limit across all domains. Per-domain limit is `1000 / crawlDelayMs`. |
| `retryPolicy.maxRetries` | int | `3` | Number of retries on retryable failures. |
| `retryPolicy.backoffMs` | int[] | `[1000,5000,15000]` | Delay before each retry. Exponential backoff. |
| `retryPolicy.retryableStatusCodes` | int[] | `[429,500,502,503,504]` | HTTP codes that trigger retry. |
| `respectRobotsTxt` | bool | `true` | Honor robots.txt directives. Set false only for owned domains. |
| `followRedirects` | bool | `true` | Automatically follow HTTP redirects. |
| `maxRedirects` | int | `5` | Cap on redirect chain length. Prevents infinite loops. |
| `allowedContentTypes` | string[] | `["text/html"]` | Only fetch URLs whose HEAD (or Content-Type) matches. |
| `maxContentLengthBytes` | int | `10485760` | Skip resources larger than this (10 MiB default). |

---

## 2. URL Frontier APIs (Internal)

The **URL frontier** is the most critical data structure in the entire crawler.
It is the priority queue that decides *what to crawl next*. It must balance:

- **Priority** -- high-value pages first.
- **Politeness** -- never hammer a single domain.
- **Freshness** -- re-crawl pages whose content changes frequently.
- **Distributed coordination** -- multiple fetcher workers pull from it concurrently.

The frontier is partitioned by domain. Each domain has its own sub-queue. The
dequeue operation picks the highest-priority URL from a domain that has not been
contacted within the politeness window.

---

### 2.1 Enqueue a URL

| Field | Value |
|-------|-------|
| **Method** | `POST` |
| **Path** | `/frontier/enqueue` |
| **Internal only** | Called by the parser after extracting outgoing links |

**Request Body**

```json
{
  "url": "https://www.bbc.com/news/world-europe-12345",
  "jobId": "cj-20240115-a8f3c",
  "priority": 7,
  "depth": 2,
  "parentUrl": "https://www.bbc.com/news",
  "discoveredAt": "2024-01-15T10:35:42Z",
  "metadata": {
    "anchorText": "Europe crisis deepens",
    "sourcePageTitle": "BBC News - World"
  }
}
```

| Field | Type | Description |
|-------|------|-------------|
| `url` | string | Fully resolved, canonicalized URL |
| `jobId` | string | Which crawl job discovered this URL |
| `priority` | int (1-10) | 10 = highest. Seeds start at 10; each depth level decreases by 1 (configurable) |
| `depth` | int | How many hops from the nearest seed URL |
| `parentUrl` | string | The page that contained this link |
| `discoveredAt` | ISO-8601 | Timestamp of discovery |
| `metadata` | object | Optional context (anchor text, link position, etc.) |

**Response -- 202 Accepted**

```json
{
  "url": "https://www.bbc.com/news/world-europe-12345",
  "action": "ENQUEUED",
  "queuePosition": 4821,
  "domainQueue": "bbc.com",
  "domainQueueDepth": 342
}
```

**Deduplication**: The frontier checks a Bloom filter (+ periodic exact-match
verification against the content store) before enqueuing. If the URL was
already seen:

```json
{
  "url": "https://www.bbc.com/news/world-europe-12345",
  "action": "SKIPPED_DUPLICATE",
  "firstSeenAt": "2024-01-15T10:31:12Z"
}
```

---

### 2.2 Dequeue Next URL

| Field | Value |
|-------|-------|
| **Method** | `GET` |
| **Path** | `/frontier/dequeue` |
| **Internal only** | Called by fetcher workers in a poll loop |

**Query Parameters**

| Param | Type | Description |
|-------|------|-------------|
| `workerId` | string | Identifies the fetcher worker (for tracking) |
| `batchSize` | int | Number of URLs to dequeue at once (default 1, max 50) |

**Politeness Contract**: The frontier will **never** return a URL for domain D
if domain D was last contacted fewer than `crawlDelayMs` milliseconds ago.
This is enforced server-side, not by the caller.

**Response -- 200 OK**

```json
{
  "urls": [
    {
      "url": "https://www.reuters.com/business/finance-2024",
      "jobId": "cj-20240115-a8f3c",
      "priority": 8,
      "depth": 1,
      "parentUrl": "https://www.reuters.com",
      "assignedTo": "fetcher-worker-07",
      "assignedAt": "2024-01-15T10:36:00Z",
      "leaseExpiresAt": "2024-01-15T10:37:00Z",
      "attemptNumber": 1,
      "domain": "reuters.com",
      "robotsRules": {
        "allowed": true,
        "crawlDelayMs": 2000
      }
    }
  ],
  "nextAvailableIn": null
}
```

**Key design points:**

- **Lease**: Each dequeued URL has a lease (default 60 seconds). If the fetcher
  does not call `/frontier/mark-complete` within the lease, the URL is returned
  to the queue. This prevents lost URLs when workers crash.
- **`nextAvailableIn`**: If no URL is currently available (all domain queues
  are in their politeness cooldown), the response includes how many
  milliseconds until the next URL becomes eligible. The fetcher can sleep
  or long-poll.

**Response -- 204 No Content**

Returned when the frontier is completely empty for the given job(s).

---

### 2.3 Mark URL Complete

| Field | Value |
|-------|-------|
| **Method** | `POST` |
| **Path** | `/frontier/mark-complete` |
| **Internal only** | Called by fetcher after successful or failed fetch |

**Request Body**

```json
{
  "url": "https://www.reuters.com/business/finance-2024",
  "jobId": "cj-20240115-a8f3c",
  "workerId": "fetcher-worker-07",
  "result": "SUCCESS",
  "httpStatus": 200,
  "contentHash": "sha256:a1b2c3d4e5f6...",
  "fetchDurationMs": 450,
  "outgoingLinksCount": 87,
  "completedAt": "2024-01-15T10:36:05Z"
}
```

| `result` Value | Meaning |
|----------------|---------|
| `SUCCESS` | Page fetched and parsed successfully |
| `FAILED_TIMEOUT` | Request timed out |
| `FAILED_DNS` | DNS resolution failed |
| `FAILED_CONNECTION` | TCP connection refused or reset |
| `FAILED_SSL` | TLS handshake or certificate error |
| `FAILED_HTTP_4XX` | Client error (404, 403, etc.) |
| `FAILED_HTTP_5XX` | Server error (will retry if retries remain) |
| `FAILED_ROBOTS_BLOCKED` | Discovered after enqueue that robots.txt disallows |
| `FAILED_CONTENT_TOO_LARGE` | Content-Length exceeds configured max |
| `FAILED_UNSUPPORTED_TYPE` | Content-Type not in allowed list |
| `SKIPPED_DUPLICATE_CONTENT` | Content hash matches already-stored page |

**Response -- 200 OK**

```json
{
  "url": "https://www.reuters.com/business/finance-2024",
  "acknowledged": true,
  "retryScheduled": false,
  "domainNextEligibleAt": "2024-01-15T10:38:05Z"
}
```

If `result` indicates a retryable failure and retries remain:

```json
{
  "url": "https://www.reuters.com/business/finance-2024",
  "acknowledged": true,
  "retryScheduled": true,
  "retryAttempt": 2,
  "retryAt": "2024-01-15T10:36:10Z"
}
```

---

### 2.4 Frontier Statistics

| Field | Value |
|-------|-------|
| **Method** | `GET` |
| **Path** | `/frontier/stats` |

**Query Parameters**

| Param | Type | Description |
|-------|------|-------------|
| `jobId` | string | Filter by job (optional; omit for global stats) |

**Response -- 200 OK**

```json
{
  "global": {
    "totalUrlsEnqueued": 1542300,
    "totalUrlsDequeued": 456700,
    "totalUrlsCompleted": 452100,
    "totalUrlsFailed": 4600,
    "totalUrlsPending": 1085600,
    "totalUrlsInFlight": 48,
    "duplicatesSkipped": 23400
  },
  "perDomain": [
    {
      "domain": "bbc.com",
      "pending": 71200,
      "completed": 8430,
      "failed": 12,
      "inFlight": 8,
      "lastContactedAt": "2024-01-15T10:36:04Z",
      "effectiveCrawlDelayMs": 2000,
      "avgFetchDurationMs": 380
    },
    {
      "domain": "reuters.com",
      "pending": 54100,
      "completed": 3200,
      "failed": 45,
      "inFlight": 4,
      "lastContactedAt": "2024-01-15T10:36:02Z",
      "effectiveCrawlDelayMs": 1000,
      "avgFetchDurationMs": 520
    }
  ],
  "bloomFilterFillRatio": 0.12,
  "bloomFilterEstimatedFalsePositiveRate": 0.001,
  "leaseExpirations": {
    "last5Minutes": 3,
    "last1Hour": 12
  }
}
```

---

## 3. Fetcher APIs (Internal)

The fetcher is the I/O-intensive workhorse. It downloads web pages. It must
handle every edge case the internet throws at it: redirects, timeouts, broken
TLS, gzip bombs, slow-loris attacks, and more.

---

### 3.1 Fetch a URL

| Field | Value |
|-------|-------|
| **Method** | `POST` |
| **Path** | `/fetch` |
| **Internal only** | Called by fetcher workers after dequeuing from frontier |

**Request Body**

```json
{
  "url": "https://www.bbc.com/news/world-europe-12345",
  "jobId": "cj-20240115-a8f3c",
  "options": {
    "timeoutMs": 30000,
    "connectTimeoutMs": 5000,
    "userAgent": "MyCrawler/1.0 (+https://example.com/bot)",
    "cookies": [],
    "followRedirects": true,
    "maxRedirects": 5,
    "acceptEncoding": "gzip, deflate, br",
    "customHeaders": {
      "Accept": "text/html,application/xhtml+xml",
      "Accept-Language": "en-US,en;q=0.9"
    },
    "headFirst": true,
    "maxBodyBytes": 10485760,
    "verifySsl": true
  }
}
```

| Field | Description |
|-------|-------------|
| `timeoutMs` | Total request timeout (DNS + connect + transfer) |
| `connectTimeoutMs` | TCP connection timeout only |
| `headFirst` | If true, send a HEAD request first to check Content-Type and Content-Length before downloading the body. Saves bandwidth on non-HTML resources. |
| `maxBodyBytes` | Abort transfer if body exceeds this. Defense against gzip bombs and huge files. |
| `verifySsl` | Whether to reject invalid/self-signed certificates |

**Response -- 200 OK (successful fetch)**

```json
{
  "url": "https://www.bbc.com/news/world-europe-12345",
  "finalUrl": "https://www.bbc.com/news/world/europe-12345",
  "fetchedAt": "2024-01-15T10:36:05Z",
  "result": "SUCCESS",
  "http": {
    "statusCode": 200,
    "headers": {
      "Content-Type": "text/html; charset=utf-8",
      "Content-Length": "54321",
      "Last-Modified": "2024-01-15T09:00:00Z",
      "ETag": "\"abc123\"",
      "Cache-Control": "max-age=300",
      "X-Robots-Tag": "index, follow",
      "Content-Encoding": "gzip"
    }
  },
  "body": "<html>...full decompressed HTML...</html>",
  "metrics": {
    "dnsResolutionMs": 12,
    "connectMs": 35,
    "tlsHandshakeMs": 48,
    "firstByteMs": 120,
    "totalTransferMs": 450,
    "bodyBytes": 54321,
    "compressedBytes": 18200,
    "redirectChain": [
      {
        "url": "https://www.bbc.com/news/world-europe-12345",
        "statusCode": 301,
        "location": "https://www.bbc.com/news/world/europe-12345"
      }
    ]
  },
  "contentType": "text/html",
  "charset": "utf-8",
  "contentLength": 54321
}
```

**Response -- 200 OK (failed fetch)**

The fetcher API itself returns 200 for all completed attempts. The inner
`result` field conveys whether the target page was reachable.

```json
{
  "url": "https://www.example-down.com/page",
  "finalUrl": null,
  "fetchedAt": "2024-01-15T10:36:10Z",
  "result": "FAILED_TIMEOUT",
  "error": {
    "type": "TIMEOUT",
    "message": "Connection timed out after 30000ms",
    "phase": "CONNECT",
    "retriable": true
  },
  "http": null,
  "body": null,
  "metrics": {
    "dnsResolutionMs": 15,
    "connectMs": 30000,
    "tlsHandshakeMs": null,
    "firstByteMs": null,
    "totalTransferMs": 30015,
    "bodyBytes": 0,
    "compressedBytes": 0,
    "redirectChain": []
  }
}
```

**Error types the fetcher must handle:**

| Error Type | Phase | Retriable | Description |
|------------|-------|-----------|-------------|
| `TIMEOUT` | CONNECT or TRANSFER | Yes | TCP connect or body transfer exceeded deadline |
| `DNS_FAILURE` | DNS | Yes (transient) / No (NXDOMAIN) | Domain does not resolve or DNS server unreachable |
| `CONNECTION_REFUSED` | CONNECT | Yes | Target port is closed |
| `CONNECTION_RESET` | TRANSFER | Yes | Server reset mid-transfer |
| `SSL_ERROR` | TLS | No | Certificate invalid, expired, or hostname mismatch |
| `SSL_HANDSHAKE_TIMEOUT` | TLS | Yes | TLS negotiation timed out |
| `TOO_MANY_REDIRECTS` | REDIRECT | No | Redirect chain exceeded `maxRedirects` |
| `REDIRECT_LOOP` | REDIRECT | No | URL appeared twice in redirect chain |
| `CONTENT_TOO_LARGE` | TRANSFER | No | Body exceeds `maxBodyBytes` |
| `UNSUPPORTED_PROTOCOL` | CONNECT | No | Not HTTP or HTTPS (e.g., FTP link) |

**Redirect handling:**

| HTTP Status | Behavior |
|-------------|----------|
| `301 Moved Permanently` | Follow. Store the mapping for future canonicalization. |
| `302 Found` | Follow. Do NOT store as permanent redirect. |
| `307 Temporary Redirect` | Follow. Preserve original HTTP method. |
| `308 Permanent Redirect` | Follow. Preserve method. Store mapping. |
| `Meta refresh / JS redirect` | Not followed by the fetcher. Parser may extract these for re-enqueue. |

---

## 4. Parser / Extractor APIs (Internal)

The parser takes raw HTML (or other content) and extracts structured
information. It runs after every successful fetch.

---

### 4.1 Parse Content

| Field | Value |
|-------|-------|
| **Method** | `POST` |
| **Path** | `/parse` |
| **Internal only** | Called by the pipeline after fetcher returns |

**Request Body**

```json
{
  "url": "https://www.bbc.com/news/world/europe-12345",
  "contentType": "text/html",
  "charset": "utf-8",
  "rawHtml": "<html><head><title>Europe crisis deepens</title>...",
  "responseHeaders": {
    "Content-Type": "text/html; charset=utf-8",
    "X-Robots-Tag": "index, follow"
  }
}
```

**Response -- 200 OK**

```json
{
  "url": "https://www.bbc.com/news/world/europe-12345",
  "parsedAt": "2024-01-15T10:36:06Z",
  "title": "Europe crisis deepens as talks stall",
  "metaDescription": "European leaders failed to reach agreement...",
  "canonicalUrl": "https://www.bbc.com/news/world/europe-12345",
  "language": "en",
  "charset": "utf-8",
  "robotsMeta": {
    "index": true,
    "follow": true,
    "noarchive": false,
    "nosnippet": false
  },
  "outgoingLinks": [
    {
      "url": "https://www.bbc.com/news/world/europe-67890",
      "anchorText": "Previous summit outcomes",
      "rel": [],
      "isInternal": true
    },
    {
      "url": "https://www.reuters.com/article/europe-talks",
      "anchorText": "Reuters analysis",
      "rel": ["nofollow", "external"],
      "isInternal": false
    }
  ],
  "outgoingLinksCount": 87,
  "structuredData": {
    "jsonLd": [
      {
        "@context": "https://schema.org",
        "@type": "NewsArticle",
        "headline": "Europe crisis deepens as talks stall",
        "datePublished": "2024-01-15T09:00:00Z",
        "author": {
          "@type": "Person",
          "name": "Jane Reporter"
        }
      }
    ],
    "microdata": [],
    "openGraph": {
      "og:title": "Europe crisis deepens",
      "og:type": "article",
      "og:image": "https://www.bbc.com/images/europe-crisis.jpg"
    }
  },
  "feeds": [
    {
      "type": "RSS",
      "url": "https://www.bbc.com/news/world/europe/rss.xml",
      "title": "BBC News - World Europe"
    }
  ],
  "extractedText": "European leaders failed to reach agreement during...",
  "extractedTextLength": 3420
}
```

**Link resolution rules:**

- Relative URLs are resolved against the page's `<base href>` tag, or the
  page URL if no base tag.
- Fragment-only links (`#section`) are discarded.
- `javascript:` and `mailto:` URLs are discarded.
- Links with `rel="nofollow"` are included in the output but flagged. The
  frontier can decide whether to respect nofollow.
- Duplicate links on the same page are deduplicated (only the first occurrence
  is kept).

---

### 4.2 Non-HTML Content Parsing

The parser also handles non-HTML content types. The same `/parse` endpoint
is used; the `contentType` field determines the parsing strategy.

**XML Sitemap (contentType: `application/xml` or `text/xml`)**

```json
{
  "url": "https://www.bbc.com/sitemap.xml",
  "contentType": "application/xml",
  "parsedAs": "SITEMAP",
  "sitemapEntries": [
    {
      "url": "https://www.bbc.com/news/article-1",
      "lastModified": "2024-01-15T08:00:00Z",
      "changeFrequency": "hourly",
      "priority": 0.8
    },
    {
      "url": "https://www.bbc.com/news/article-2",
      "lastModified": "2024-01-14T12:00:00Z",
      "changeFrequency": "daily",
      "priority": 0.6
    }
  ],
  "sitemapIndexUrls": [
    "https://www.bbc.com/sitemap-news.xml",
    "https://www.bbc.com/sitemap-sport.xml"
  ]
}
```

**RSS / Atom Feed (contentType: `application/rss+xml` or `application/atom+xml`)**

```json
{
  "url": "https://www.bbc.com/news/rss.xml",
  "contentType": "application/rss+xml",
  "parsedAs": "RSS_FEED",
  "feedTitle": "BBC News - Top Stories",
  "feedEntries": [
    {
      "url": "https://www.bbc.com/news/article-99",
      "title": "Breaking: New development",
      "publishedAt": "2024-01-15T09:30:00Z"
    }
  ]
}
```

**PDF (contentType: `application/pdf`)**

```json
{
  "url": "https://www.example.com/report.pdf",
  "contentType": "application/pdf",
  "parsedAs": "PDF",
  "title": "Annual Report 2024",
  "outgoingLinks": [
    {
      "url": "https://www.example.com/appendix",
      "anchorText": "See Appendix A"
    }
  ],
  "extractedText": "Annual Report 2024. Revenue increased...",
  "pageCount": 42
}
```

---

## 5. Content Store APIs (Internal)

Content-addressed storage using SHA-256 hashes. Every fetched page is stored
exactly once regardless of how many URLs point to it (deduplication).

---

### 5.1 Store Content

| Field | Value |
|-------|-------|
| **Method** | `POST` |
| **Path** | `/content/store` |
| **Internal only** | Called after successful fetch + parse |

**Request Body**

```json
{
  "url": "https://www.bbc.com/news/world/europe-12345",
  "contentHash": "sha256:a1b2c3d4e5f67890abcdef1234567890abcdef1234567890abcdef1234567890",
  "rawHtml": "<html>...full HTML...</html>",
  "extractedText": "European leaders failed to reach agreement...",
  "metadata": {
    "title": "Europe crisis deepens as talks stall",
    "language": "en",
    "charset": "utf-8",
    "contentType": "text/html",
    "contentLength": 54321,
    "canonicalUrl": "https://www.bbc.com/news/world/europe-12345",
    "lastModified": "2024-01-15T09:00:00Z",
    "etag": "\"abc123\"",
    "structuredData": { "...": "..." }
  },
  "crawlInfo": {
    "jobId": "cj-20240115-a8f3c",
    "fetchedAt": "2024-01-15T10:36:05Z",
    "fetchDurationMs": 450,
    "httpStatusCode": 200,
    "depth": 2,
    "parentUrl": "https://www.bbc.com/news"
  }
}
```

**Response -- 201 Created (new content)**

```json
{
  "contentHash": "sha256:a1b2c3d4e5f6...",
  "action": "STORED",
  "storagePath": "s3://crawler-content/sha256/a1/b2/a1b2c3d4e5f6...",
  "compressedSizeBytes": 18200,
  "urlsMappedToThisHash": [
    "https://www.bbc.com/news/world/europe-12345"
  ]
}
```

**Response -- 200 OK (duplicate content, new URL mapping)**

```json
{
  "contentHash": "sha256:a1b2c3d4e5f6...",
  "action": "DUPLICATE_URL_ADDED",
  "storagePath": "s3://crawler-content/sha256/a1/b2/a1b2c3d4e5f6...",
  "urlsMappedToThisHash": [
    "https://www.bbc.com/news/world/europe-12345",
    "https://www.bbc.com/news/world-europe-12345"
  ],
  "note": "Content already stored; added URL as additional mapping."
}
```

---

### 5.2 Retrieve Content

| Field | Value |
|-------|-------|
| **Method** | `GET` |
| **Path** | `/content/{urlHash}` |

Where `urlHash` is the SHA-256 of the canonicalized URL (not the content hash).

**Response -- 200 OK**

```json
{
  "url": "https://www.bbc.com/news/world/europe-12345",
  "urlHash": "sha256:url-hash-here...",
  "contentHash": "sha256:a1b2c3d4e5f6...",
  "rawHtml": "<html>...",
  "extractedText": "European leaders failed...",
  "metadata": { "...": "..." },
  "crawlHistory": [
    {
      "fetchedAt": "2024-01-15T10:36:05Z",
      "httpStatusCode": 200,
      "contentHash": "sha256:a1b2c3d4e5f6..."
    },
    {
      "fetchedAt": "2024-01-14T10:30:00Z",
      "httpStatusCode": 200,
      "contentHash": "sha256:aaaa1111bbbb..."
    }
  ]
}
```

---

### 5.3 Check Content Existence (Dedup)

| Field | Value |
|-------|-------|
| **Method** | `HEAD` |
| **Path** | `/content/{urlHash}` |

Returns headers only. Used by the frontier for fast deduplication before
enqueuing.

**Response -- 200 OK (exists)**

```
HTTP/1.1 200 OK
X-Content-Hash: sha256:a1b2c3d4e5f6...
X-Last-Fetched: 2024-01-15T10:36:05Z
X-Content-Length: 54321
Content-Length: 0
```

**Response -- 404 Not Found (not yet crawled)**

```
HTTP/1.1 404 Not Found
Content-Length: 0
```

---

## 6. robots.txt Management APIs (Internal)

Every domain must have its robots.txt fetched and parsed before any page on
that domain is crawled. This service caches parsed rules and refreshes them
periodically.

---

### 6.1 Get Parsed robots.txt for a Domain

| Field | Value |
|-------|-------|
| **Method** | `GET` |
| **Path** | `/robots/{domain}` |

**Query Parameters**

| Param | Type | Description |
|-------|------|-------------|
| `userAgent` | string | Which user-agent to match rules against (default: configured crawler UA) |

**Response -- 200 OK**

```json
{
  "domain": "bbc.com",
  "fetchedAt": "2024-01-15T06:00:00Z",
  "expiresAt": "2024-01-16T06:00:00Z",
  "ttlSeconds": 86400,
  "cacheStatus": "HIT",
  "source": "https://www.bbc.com/robots.txt",
  "raw": "User-agent: *\nDisallow: /search\nAllow: /news\nCrawl-delay: 2\nSitemap: https://www.bbc.com/sitemap.xml\n...",
  "parsed": {
    "matchedUserAgent": "*",
    "rules": [
      { "type": "DISALLOW", "path": "/search" },
      { "type": "DISALLOW", "path": "/cgi-bin/" },
      { "type": "DISALLOW", "path": "/tmp/" },
      { "type": "ALLOW", "path": "/news" },
      { "type": "ALLOW", "path": "/sport" }
    ],
    "crawlDelaySeconds": 2,
    "sitemaps": [
      "https://www.bbc.com/sitemap.xml",
      "https://www.bbc.com/sitemap-news.xml"
    ]
  },
  "checkUrl": {
    "usage": "GET /robots/{domain}/check?url=<encoded-url>",
    "description": "Convenience endpoint to check if a specific URL is allowed"
  }
}
```

**Response -- 404 Not Found**

Returned when the domain has no robots.txt (or it returned 404). In this case,
all paths are considered allowed.

```json
{
  "domain": "example-no-robots.com",
  "fetchedAt": "2024-01-15T06:00:00Z",
  "expiresAt": "2024-01-16T06:00:00Z",
  "status": "NOT_FOUND",
  "interpretation": "No robots.txt found. All paths are allowed per specification."
}
```

**Parsing rules (per the robots.txt specification):**

| Directive | Behavior |
|-----------|----------|
| `User-agent` | Match against our crawler's user-agent string. Fall back to `*` wildcard. |
| `Disallow` | Path prefix matching. `/foo` blocks `/foo`, `/foobar`, `/foo/bar`. |
| `Allow` | Overrides a Disallow for a more specific path. Longest match wins. |
| `Crawl-delay` | Minimum seconds between requests. Used as the floor for `crawlDelayMs`. |
| `Sitemap` | URLs of XML sitemaps. Enqueued into the frontier automatically. |
| Unknown directives | Ignored silently. |

---

### 6.2 Refresh robots.txt for a Domain

| Field | Value |
|-------|-------|
| **Method** | `POST` |
| **Path** | `/robots/refresh/{domain}` |

Forces a re-fetch of the domain's robots.txt, regardless of cache TTL. Used
when the crawler encounters unexpected 403s that might indicate a robots.txt
change.

**Response -- 200 OK**

```json
{
  "domain": "bbc.com",
  "previousFetchedAt": "2024-01-15T06:00:00Z",
  "newFetchedAt": "2024-01-15T10:40:00Z",
  "changed": true,
  "diff": {
    "added": [
      { "type": "DISALLOW", "path": "/private/" }
    ],
    "removed": [],
    "crawlDelayChanged": false
  },
  "affectedUrlsInFrontier": 42,
  "note": "42 queued URLs under /private/ have been removed from the frontier."
}
```

---

## 7. DNS Resolution APIs (Internal)

DNS resolution is on the critical path of every fetch. A custom caching
resolver avoids hitting upstream DNS for every request and provides resilience
against DNS outages.

---

### 7.1 Resolve a Hostname

| Field | Value |
|-------|-------|
| **Method** | `POST` |
| **Path** | `/dns/resolve` |
| **Internal only** | Called by fetcher before TCP connect |

**Request Body**

```json
{
  "hostname": "www.bbc.com",
  "recordTypes": ["A", "AAAA"],
  "preferIPv4": true,
  "timeoutMs": 5000
}
```

**Response -- 200 OK (cache hit)**

```json
{
  "hostname": "www.bbc.com",
  "resolvedAt": "2024-01-15T10:36:04Z",
  "cacheStatus": "HIT",
  "cacheTtlRemainingSeconds": 245,
  "records": {
    "A": [
      { "address": "151.101.0.81", "ttl": 300 },
      { "address": "151.101.64.81", "ttl": 300 }
    ],
    "AAAA": [
      { "address": "2a04:4e42::81", "ttl": 300 }
    ]
  },
  "cnameChain": [
    "www.bbc.com -> www.bbc.com.cdn.cloudflare.net -> 151.101.0.81"
  ],
  "selectedAddress": "151.101.0.81",
  "resolutionTimeMs": 0
}
```

**Response -- 200 OK (cache miss, resolved from upstream)**

```json
{
  "hostname": "www.reuters.com",
  "resolvedAt": "2024-01-15T10:36:04Z",
  "cacheStatus": "MISS",
  "records": {
    "A": [
      { "address": "104.18.24.55", "ttl": 60 }
    ],
    "AAAA": []
  },
  "cnameChain": [
    "www.reuters.com -> reuters.map.fastly.net -> 104.18.24.55"
  ],
  "selectedAddress": "104.18.24.55",
  "resolutionTimeMs": 23
}
```

**Response -- 200 OK (resolution failure)**

```json
{
  "hostname": "www.nonexistent-domain-xyz.com",
  "resolvedAt": "2024-01-15T10:36:04Z",
  "cacheStatus": "NEGATIVE_CACHE",
  "error": {
    "type": "NXDOMAIN",
    "message": "Domain does not exist",
    "retriable": false,
    "negativeCacheTtlSeconds": 3600
  },
  "records": null,
  "selectedAddress": null,
  "resolutionTimeMs": 45
}
```

**DNS error types:**

| Error Type | Retriable | Description |
|------------|-----------|-------------|
| `NXDOMAIN` | No | Domain does not exist. Negative-cached for 1 hour. |
| `SERVFAIL` | Yes | Upstream DNS server error. Retry after backoff. |
| `TIMEOUT` | Yes | No response from DNS server within deadline. |
| `REFUSED` | Yes (with different resolver) | Upstream refused the query. |

**Caching strategy:**

- Positive results cached for `min(record TTL, 24 hours)`.
- Negative results (NXDOMAIN) cached for 1 hour.
- SERVFAIL cached for 30 seconds (to avoid hammering a broken upstream).
- Cache is in-memory (HashMap) with LRU eviction when capacity is reached.
- For high-throughput crawling, the resolver uses asynchronous I/O (e.g., Netty
  or Java NIO) to avoid blocking a thread per resolution.

---

## 8. Monitoring / Metrics APIs (External)

Operators and dashboards use these endpoints to observe the crawler's health
and performance in real time.

---

### 8.1 Get Metrics

| Field | Value |
|-------|-------|
| **Method** | `GET` |
| **Path** | `/metrics` |

**Query Parameters**

| Param | Type | Description |
|-------|------|-------------|
| `format` | string | `json` (default) or `prometheus` |
| `jobId` | string | Filter by job (optional) |

**Response -- 200 OK (JSON format)**

```json
{
  "timestamp": "2024-01-15T10:40:00Z",
  "uptime": "4h 10m 00s",
  "crawlRate": {
    "currentPagesPerSecond": 8.7,
    "peakPagesPerSecond": 12.3,
    "avgPagesPerSecond": 9.1,
    "currentBytesPerSecond": 1048576
  },
  "pages": {
    "totalFetched": 452100,
    "totalSuccess": 447500,
    "totalFailed": 4600,
    "totalDuplicatesSkipped": 23400,
    "totalInFlight": 48
  },
  "errors": {
    "byType": {
      "TIMEOUT": 1820,
      "DNS_FAILURE": 430,
      "CONNECTION_REFUSED": 210,
      "SSL_ERROR": 85,
      "HTTP_4XX": 1200,
      "HTTP_5XX": 855
    },
    "errorRate": 0.0102,
    "last5MinErrorRate": 0.008
  },
  "frontier": {
    "totalQueueDepth": 1085600,
    "activeDomainsCount": 3,
    "domainsWithPendingUrls": 3,
    "avgDomainQueueDepth": 361867,
    "bloomFilterFillRatio": 0.12
  },
  "contentStore": {
    "totalDocumentsStored": 424100,
    "totalStorageSizeBytes": 85899345920,
    "totalStorageSizeHuman": "80 GiB",
    "duplicateContentRate": 0.055,
    "avgDocumentSizeBytes": 202560
  },
  "dns": {
    "cacheSize": 3200,
    "cacheHitRate": 0.94,
    "cacheMissRate": 0.06,
    "avgResolutionTimeMs": 18,
    "negativeCacheEntries": 120
  },
  "robots": {
    "cacheSize": 3,
    "cacheHitRate": 0.99,
    "domainsWithCrawlDelay": 2,
    "urlsBlockedByRobots": 1450
  },
  "system": {
    "activeFetcherWorkers": 50,
    "cpuUsagePercent": 35.2,
    "memoryUsedMb": 4096,
    "memoryMaxMb": 8192,
    "openFileDescriptors": 2048,
    "activeConnections": 48
  }
}
```

---

### 8.2 Health Check

| Field | Value |
|-------|-------|
| **Method** | `GET` |
| **Path** | `/health` |

**Response -- 200 OK (healthy)**

```json
{
  "status": "HEALTHY",
  "timestamp": "2024-01-15T10:40:00Z",
  "checks": {
    "frontier": { "status": "UP", "latencyMs": 2 },
    "contentStore": { "status": "UP", "latencyMs": 15 },
    "dnsResolver": { "status": "UP", "latencyMs": 1 },
    "robotsCache": { "status": "UP", "latencyMs": 1 },
    "fetcherPool": {
      "status": "UP",
      "activeWorkers": 50,
      "idleWorkers": 0,
      "queuedTasks": 12
    }
  }
}
```

**Response -- 503 Service Unavailable (degraded)**

```json
{
  "status": "DEGRADED",
  "timestamp": "2024-01-15T10:40:00Z",
  "checks": {
    "frontier": { "status": "UP", "latencyMs": 2 },
    "contentStore": {
      "status": "DOWN",
      "latencyMs": 5000,
      "error": "Connection to S3 timed out"
    },
    "dnsResolver": { "status": "UP", "latencyMs": 1 },
    "robotsCache": { "status": "UP", "latencyMs": 1 },
    "fetcherPool": {
      "status": "UP",
      "activeWorkers": 50,
      "idleWorkers": 0,
      "queuedTasks": 12
    }
  }
}
```

---

## 9. Contrast with Googlebot

Google's web crawler (Googlebot) is the world's largest and most sophisticated
crawler. Understanding it provides context for where our design sits on the
complexity spectrum.

| Dimension | Our System | Googlebot |
|-----------|-----------|-----------|
| **Scale** | Millions of pages per job | Hundreds of billions of known pages; billions crawled daily |
| **Infrastructure** | Single cluster or small fleet | Thousands of machines across global data centers |
| **URL Frontier** | Priority queue per domain, single coordinator | Distributed priority system factoring in PageRank, change rate, page importance, freshness requirements |
| **JavaScript Rendering** | Not supported (HTML-only) | Web Rendering Service (WRS) -- headless Chromium at scale, deferred rendering queue |
| **Crawl Scheduling** | Operator-configured crawl rate | Adaptive: crawl rate adjusts per site based on server response time, error rate, and site owner preferences via Search Console |
| **Duplicate Detection** | SHA-256 content hash | SimHash / near-duplicate detection across the entire web corpus |
| **robots.txt** | Standard parsing + cache | Google-specific extensions, stricter compliance, per-product user-agents (Googlebot-Image, Googlebot-News) |
| **Politeness** | Fixed delay per domain | Dynamic: increases delay if server slows down, reduces if server is fast. Site owners set preferred rate in Google Search Console. |
| **Output** | Content store (raw HTML + text) | Feeds directly into the indexing and ranking pipeline (inverted index, knowledge graph, featured snippets) |
| **Re-crawl** | Manual re-submission or cron | Continuous re-crawl prioritized by predicted change rate (news sites every minutes, static pages every weeks/months) |
| **Monitoring** | Internal metrics dashboard | Exposes crawl stats to site owners via Google Search Console (pages crawled/day, crawl errors, crawl budget) |

**Key Googlebot-specific concepts not in our design:**

- **Crawl budget**: Each site gets a budget based on its perceived importance and server health. Low-quality sites get fewer crawls.
- **Rendering queue**: Pages requiring JavaScript are fetched first (raw HTML), then queued for WRS rendering, then re-processed. Two-phase crawl.
- **Caffeine**: Google's incremental indexing system that processes pages continuously rather than in batch.
- **URL parameter handling**: Google tries to detect URL parameters that do not change page content (e.g., session IDs, tracking params) to avoid duplicate crawls.

---

## 10. Contrast with Common Crawl

Common Crawl is an open-source project that produces monthly snapshots of the
web, freely available for research and ML training.

| Dimension | Our System | Common Crawl |
|-----------|-----------|--------------|
| **Purpose** | Targeted crawling for a specific use case | Broad web archive for public use |
| **Scale** | Millions of pages per job | ~2.5-3.5 billion pages per monthly crawl |
| **Data Volume** | GiBs to TiBs per job | 350-460 TiB of WARC data per monthly crawl |
| **Strategy** | Configurable (BFS, priority, domain-scoped) | Breadth-first to maximize domain and page coverage |
| **Depth** | Configurable per job (typically 3-10) | Shallow -- typically 1-3 levels from seed |
| **Storage Format** | Custom content store (content-addressed, JSON metadata) | WARC (Web ARChive) format -- industry standard, stores raw HTTP request+response |
| **Deduplication** | URL-level + content-hash exact match | URL-level; some near-duplicate detection across crawls |
| **Extracted Content** | Structured: title, links, text, structured data | Raw HTML stored; text extraction done by downstream consumers (e.g., CCNet, C4 dataset) |
| **Access** | Private, internal APIs | Public S3 bucket (s3://commoncrawl), free to download |
| **Use Cases** | Production system feeding a specific pipeline | Research papers, ML training data (GPT, BERT, etc.), web analytics studies |
| **Frequency** | On-demand or scheduled per job | Monthly crawls since 2011; ~100+ monthly snapshots available |
| **Cost Model** | Self-funded infrastructure | Non-profit; funded by grants and donations; infrastructure donated by AWS |
| **robots.txt** | Configurable compliance | Respects robots.txt; some argue coverage suffers as a result |

**Common Crawl data products:**

- **WARC files**: Raw HTTP responses (request + response headers + body).
- **WAT files**: Metadata extracted from WARC files (HTTP headers, HTML metadata).
- **WET files**: Plain text extracted from HTML pages.
- **URL index**: Columnar index mapping URLs to WARC file offsets for random access.

---

## 11. Interview Subset -- What to Focus On

In a system design interview, you will not have time to cover all eight API
groups. The interviewer expects depth over breadth. Focus on these three in
Phase 3 (API design):

### Tier 1: Must Cover

| API Group | Why It Matters |
|-----------|---------------|
| **URL Frontier** (`/frontier/*`) | This is the **core scheduling data structure**. It encapsulates the hardest problems: priority, politeness, deduplication, distributed coordination, and lease management. Interviewers love asking about it because it tests data structure knowledge (priority queue, hash maps, Bloom filters) and distributed systems thinking (what happens when a worker crashes?). |
| **Fetcher** (`/fetch`) | This is the **I/O-intensive workhorse**. It tests your knowledge of networking: redirects, timeouts, TLS, connection pooling, error handling. Interviewers want to see that you understand what happens between "send HTTP request" and "get response." |
| **Crawl Management** (`/crawl/jobs/*`) | This is the **operator control plane**. It shows you understand that a production system needs human oversight: pause, resume, cancel, configuration. It also tests API design fundamentals: REST conventions, idempotency, error codes. |

### Tier 2: Mention But Don't Deep-Dive

| API Group | What to Say |
|-----------|------------|
| **robots.txt** | "We cache parsed robots.txt per domain with a 24-hour TTL. The frontier checks it before dequeuing. We respect Crawl-delay as a floor for our politeness interval." |
| **DNS Resolution** | "We run a caching DNS resolver to avoid upstream lookups on every fetch. Positive cache with record TTL, negative cache for NXDOMAIN. Async resolution to avoid blocking." |
| **Content Store** | "Content-addressed storage using SHA-256. HEAD request for dedup check. Stores raw HTML plus extracted text and metadata." |

### Tier 3: Skip Unless Asked

| API Group | When It Comes Up |
|-----------|-----------------|
| **Parser / Extractor** | Only if interviewer asks "what do you extract from each page?" |
| **Monitoring / Metrics** | Only if interviewer asks "how do you know if the crawler is healthy?" |

### The 30-Second Frontier Pitch

If you have time to explain only one thing well, make it the frontier:

> "The frontier is a distributed priority queue partitioned by domain. Each
> domain has its own sub-queue sorted by priority. The dequeue operation picks
> the highest-priority URL from a domain whose politeness timer has expired.
> URLs are leased to workers with a timeout -- if the worker doesn't report
> back, the URL returns to the queue. We use a Bloom filter for fast
> approximate dedup and fall back to an exact check against the content store
> for borderline cases. The frontier is the single most important component
> because it controls what the crawler does next."
