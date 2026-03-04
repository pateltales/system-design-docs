# TinyURL System — API Contracts

> Continuation of the interview simulation. The interviewer asked: "Walk me through the API contracts for your URL shortening service."

---

## Base URL & Conventions

```
Base URL:        https://tinyurl.com
API Base:        https://tinyurl.com/api/v1   (for non-redirect endpoints)
Content-Type:    application/json
Versioning:      URL path versioning (/api/v1/...)
```

**Authentication model:**

| Context | Method | Details |
|---|---|---|
| External clients (create, delete, stats) | API Key | `X-API-Key` header on every request |
| Internal services (admin APIs) | mTLS | Mutual TLS with client certificates |
| Redirect (`GET /{suffix}`) | None | Public, no auth required — short URLs must be shareable freely |

**Common conventions:**
- All non-redirect endpoints require authentication (API key for external clients, mTLS for internal services).
- All responses include an `X-Request-Id` header (UUID v4) for distributed tracing.
- All responses include `X-Rate-Limit-Remaining` and `X-Rate-Limit-Reset` headers.
- Timestamps are ISO 8601 (e.g., `2026-02-09T10:30:00Z`) for human readability and timezone clarity.
- URL suffixes are 8-character base-36 strings (`[a-z0-9]{8}`), yielding 2.8 trillion unique combinations.
- Custom aliases are 4-30 characters, alphanumeric plus hyphens (`[a-z0-9-]{4,30}`).
- Rate limiting is per API key (authenticated) and per IP (unauthenticated redirect endpoint).

**Standard Response Headers:**

```
X-Request-Id: 550e8400-e29b-41d4-a716-446655440000
X-Rate-Limit-Remaining: 95
X-Rate-Limit-Reset: 1707474600
X-Served-From: cache | db
```

**Standard Error Response Format:**

All error responses follow a consistent envelope:

```json
{
    "error": {
        "code": "INVALID_URL",
        "message": "URL is malformed or exceeds 2048 characters",
        "requestId": "550e8400-e29b-41d4-a716-446655440000",
        "timestamp": "2026-02-09T10:30:00Z"
    }
}
```

---

## Interview-Focused APIs (Core — discussed in the interview)

These two APIs handle 99.9% of all traffic. The redirect endpoint alone accounts for ~95% of total requests.

```
Traffic profile:
- URL creation:  600M/month  = ~230 writes/sec (avg), ~1,150 writes/sec (5x peak)
- Redirects:     10B/month   = ~3,800 reads/sec (avg), ~15,000 reads/sec (peak)
- Read:write ratio = ~17:1
```

### 1. POST /api/v1/urls — Create Short URL

The primary write API. Accepts a long URL and returns a shortened version.

```
POST /api/v1/urls
```

**Request Headers:**

| Header | Required | Description |
|---|---|---|
| `X-API-Key` | Yes | API key for authentication and rate limiting |
| `Content-Type` | Yes | Must be `application/json` |
| `X-Idempotency-Key` | No | UUID for idempotent retries. If the same key is sent within 24 hours, the original response is returned without creating a duplicate. |

**Request Body:**

```json
{
    "longUrl": "https://example.com/very/long/path/to/page?param=value&utm_source=newsletter",
    "expiry": "2027-01-01T00:00:00Z",
    "customAlias": "my-brand",
    "userId": "user_abc123"
}
```

**Request Fields:**

| Field | Type | Required | Default | Constraints | Description |
|---|---|---|---|---|---|
| `longUrl` | string | Yes | — | RFC 3986 compliant, max 2048 chars, must start with `http://` or `https://` | The original URL to shorten |
| `expiry` | string (ISO 8601) | No | 1 year from now | Must be in the future, max 10 years from now | When the short URL expires |
| `customAlias` | string | No | Auto-generated 8-char suffix | 4-30 chars, `[a-z0-9-]`, no leading/trailing hyphens | User-chosen vanity alias |
| `userId` | string | No | — | Max 64 chars | Associates the URL with a user account for ownership tracking |

**Response — 201 Created:**

```json
{
    "shortUrl": "https://tinyurl.com/ab3k9x12",
    "suffix": "ab3k9x12",
    "longUrl": "https://example.com/very/long/path/to/page?param=value&utm_source=newsletter",
    "expiry": "2027-01-01T00:00:00Z",
    "createdAt": "2026-02-09T10:30:00Z",
    "userId": "user_abc123"
}
```

**Response Fields:**

| Field | Type | Always Present | Description |
|---|---|---|---|
| `shortUrl` | string | Yes | The full shortened URL, ready to share |
| `suffix` | string | Yes | The 8-char suffix (or custom alias) — the unique identifier |
| `longUrl` | string | Yes | Echo of the original URL |
| `expiry` | string (ISO 8601) | Yes | When this short URL will stop working |
| `createdAt` | string (ISO 8601) | Yes | When this mapping was created |
| `userId` | string | No | Present only if a userId was provided in the request |

**Response Headers:**

```
HTTP/1.1 201 Created
Content-Type: application/json
Location: https://tinyurl.com/ab3k9x12
X-Request-Id: 550e8400-e29b-41d4-a716-446655440000
X-Rate-Limit-Remaining: 94
X-Rate-Limit-Reset: 1707474600
```

**What happens server-side (numbered steps):**

```
1. API Gateway validates request, authenticates API key, checks rate limit
   → 401 if invalid key, 429 if rate limited

2. Validate longUrl format:
   - Must be RFC 3986 compliant
   - Max 2048 characters
   - Must start with http:// or https://
   - Check against URL blocklist (malicious/phishing domains)
   → 400 INVALID_URL if validation fails
   → 403 URL_BLOCKED if domain is blocklisted

3. Validate expiry:
   - Must be in the future
   - Must not exceed 10 years from now
   → 400 INVALID_EXPIRY if validation fails

4. If customAlias provided:
   a. Validate format: 4-30 chars, [a-z0-9-], no leading/trailing hyphens
   b. Check availability: SELECT 1 FROM url_mappings WHERE suffix = $1
   → 400 INVALID_ALIAS if format is wrong
   → 409 ALIAS_TAKEN if already in use

5. If no customAlias (the common path):
   a. Execute key claim query:
      UPDATE key_pool
      SET status = 'used', claimed_at = NOW()
      WHERE id = (
          SELECT id FROM key_pool
          WHERE status = 'available'
          ORDER BY id
          LIMIT 1
          FOR UPDATE SKIP LOCKED    ← concurrent-safe: skips rows locked by other txns
      )
      RETURNING suffix;
   b. This atomically claims a pre-generated key with zero contention
   → 503 SERVICE_UNAVAILABLE if key pool is exhausted

6. Write URL mapping to PostgreSQL:
   INSERT INTO url_mappings (suffix, long_url, expiry_time, user_id, created_at)
   VALUES ($1, $2, $3, $4, NOW());

7. Async (non-blocking): Populate Redis cache
   SET url:{suffix} {longUrl} EX {min(seconds_to_expiry, 3600)}
   - TTL is capped at 1 hour to keep cache fresh
   - This is async — we don't wait for Redis to respond before returning to the client

8. Return 201 Created with the short URL mapping
```

**Why FOR UPDATE SKIP LOCKED? (Interview talking point)**

```
Problem: 230 concurrent writes/sec all need a unique suffix.

Naive approach (auto-increment or random generation):
  - Auto-increment: predictable, security risk (enumerable URLs)
  - Random generation: collision risk, retry loops under high load
  - SELECT ... FOR UPDATE: works but creates a bottleneck (all txns wait on the same row)

Our approach (pre-allocated key pool + FOR UPDATE SKIP LOCKED):
  - Key generation is offline: a background job pre-generates millions of random suffixes
  - At write time, each transaction claims the NEXT available key
  - FOR UPDATE SKIP LOCKED means if row N is locked by txn A, txn B skips to row N+1
  - Zero contention, zero collisions, zero retries
  - Each transaction takes ~2ms instead of waiting in a queue

  ┌──────────┐     ┌──────────┐     ┌──────────┐
  │  Txn A   │     │  Txn B   │     │  Txn C   │
  │ claims   │     │ skips A  │     │ skips A,B│
  │ row 1    │     │ claims   │     │ claims   │
  │          │     │ row 2    │     │ row 3    │
  └──────────┘     └──────────┘     └──────────┘
       ↓                ↓                ↓
  All three complete in parallel — no waiting, no contention
```

**Error Responses:**

| Status | Code | Description | Example Trigger |
|---|---|---|---|
| 400 | `INVALID_URL` | URL is malformed or exceeds 2048 characters | `longUrl: "not-a-url"` |
| 400 | `INVALID_EXPIRY` | Expiry date is in the past or exceeds maximum (10 years) | `expiry: "2020-01-01T00:00:00Z"` |
| 400 | `INVALID_ALIAS` | Custom alias contains invalid characters or is too short/long | `customAlias: "a"` (too short) |
| 401 | `UNAUTHORIZED` | Missing or invalid API key | No `X-API-Key` header |
| 403 | `URL_BLOCKED` | The target URL domain is on the blocklist | `longUrl: "https://phishing-site.com/..."` |
| 409 | `ALIAS_TAKEN` | The requested custom alias is already in use | `customAlias: "google"` (taken) |
| 429 | `RATE_LIMITED` | Too many requests. Check `X-Rate-Limit-Reset` header | Exceeding 100 req/min |
| 503 | `SERVICE_UNAVAILABLE` | Key pool exhausted or database unavailable | Key pool below threshold |

**Error Response Example:**

```json
{
    "error": {
        "code": "ALIAS_TAKEN",
        "message": "The custom alias 'my-brand' is already in use. Please choose a different alias.",
        "requestId": "550e8400-e29b-41d4-a716-446655440000",
        "timestamp": "2026-02-09T10:30:00Z"
    }
}
```

---

### 2. GET /{suffix} — Redirect (Hot Path)

The highest-traffic endpoint in the entire system. This is the core value proposition — when someone clicks a short URL, they get redirected to the original long URL.

**Critical design choice**: No `/api/v1/` prefix. The redirect path must be as short as possible because it IS the short URL. `tinyurl.com/ab3k9x12` is cleaner than `tinyurl.com/api/v1/redirect/ab3k9x12`.

```
GET /ab3k9x12
```

**Request:** No body, no auth, no query parameters. Just the suffix in the URL path.

| Component | Value |
|---|---|
| Method | GET |
| Path | `/{suffix}` where suffix is 8 chars (auto-generated) or 4-30 chars (custom alias) |
| Headers | None required (standard browser headers are fine) |
| Body | None |
| Authentication | None — redirects are public |

**Response — 302 Found (Success):**

```
HTTP/1.1 302 Found
Location: https://example.com/very/long/path/to/page?param=value&utm_source=newsletter
Cache-Control: private, max-age=300
X-Request-Id: 550e8400-e29b-41d4-a716-446655440000
Content-Length: 0
```

The response has no body. The browser reads the `Location` header and immediately navigates to the target URL. Total response size is approximately 200 bytes.

**What happens server-side:**

```
1. Extract suffix from URL path
   - Parse the path: /ab3k9x12 → suffix = "ab3k9x12"
   - Validate format: must match [a-z0-9-]{4,30}

2. Check Redis cache first (fast path):
   GET url:ab3k9x12

3. Cache HIT (expected ~85% of the time):
   → Extract longUrl from cache value
   → Return 302 with Location header
   → Async: emit click event to Kafka for analytics
   → Total latency: ~2ms

4. Cache MISS (remaining ~15%):
   → Query PostgreSQL read replica:
     SELECT long_url, expiry_time
     FROM url_mappings
     WHERE suffix = 'ab3k9x12'
   → Total latency: ~10ms

5. If found in DB and not expired:
   → Async: populate Redis cache
     SET url:ab3k9x12 {longUrl} EX {min(seconds_to_expiry, 3600)}
   → Return 302 with Location header
   → Async: emit click event to Kafka

6. If not found in DB:
   → Return 404 with a user-friendly "URL not found" HTML page

7. If found but expired:
   → Return 410 Gone with a user-friendly "URL expired" HTML page
   → Async: DEL url:ab3k9x12 from Redis (cleanup stale cache)
```

**Redirect flow visualized:**

```
Browser                    Load Balancer           App Server              Redis            PostgreSQL
   │                            │                      │                    │                   │
   │  GET /ab3k9x12             │                      │                    │                   │
   │ ──────────────────────────>│                      │                    │                   │
   │                            │  forward request     │                    │                   │
   │                            │ ────────────────────>│                    │                   │
   │                            │                      │  GET url:ab3k9x12  │                   │
   │                            │                      │ ──────────────────>│                   │
   │                            │                      │                    │                   │
   │                            │                      │  cache HIT         │                   │
   │                            │                      │ <──────────────────│                   │
   │                            │                      │                    │                   │
   │                            │  302 Found           │                    │                   │
   │                            │  Location: long_url  │                    │                   │
   │  <────────────────────────────────────────────────│                    │                   │
   │                            │                      │                    │                   │
   │  GET long_url (to target)  │                      │                    │                   │
   │ ──────────────────────────────────────────────────────────────────────────────────────>    │
   │                            │                      │                    │                   │

   Total latency (cache hit): ~2ms server-side + network RTT
   Total latency (cache miss): ~10ms server-side + network RTT
```

**301 vs 302 Tradeoff (Critical Interview Discussion):**

| Aspect | 301 (Moved Permanently) | 302 (Found / Temporary) |
|---|---|---|
| Browser behavior | Caches redirect indefinitely; never hits our server again for this URL | Does NOT cache (unless `Cache-Control` set); hits our server on every visit |
| Server load | Zero for repeat visits from same browser | Every visit hits our server |
| Flexibility | Cannot change target URL or expire mapping (browser won't ask again) | Can update target, expire, or delete mapping at any time |
| Analytics | Cannot track repeat clicks from same browser | Can track every single click |
| SEO impact | Passes link equity ("link juice") to the target URL | Link equity stays with the short URL |
| Delete/expire | Broken — browser still redirects even after server-side deletion | Works correctly — server can return 404/410 |
| Our default | Use ONLY for explicitly permanent, immutable URLs | **Default choice for all URLs** |

**Our decision: 302 as default.** The flexibility to update, expire, and track URLs is far more valuable than the marginal server load reduction from 301. At 15,000 redirects/sec peak, each redirect is ~200 bytes of response — that is only 3 MB/sec of outbound bandwidth, which is trivial.

For users who explicitly want maximum redirect performance and do not need analytics or expiry, we can offer 301 as an opt-in via a `permanent: true` flag on the create API.

**Error Responses:**

| Status | Code | Description | Response Body |
|---|---|---|---|
| 404 | `URL_NOT_FOUND` | No mapping exists for this suffix | User-friendly HTML page with search box |
| 410 | `URL_EXPIRED` | The URL existed but has expired | User-friendly HTML page explaining expiry |

**Why HTML (not JSON) for redirect errors?**

Redirect errors are seen by end users in a browser, not by API consumers. A JSON error body would be confusing to a non-technical user who clicked a short link. Instead, we serve a styled HTML page with:
- A clear message ("This link has expired" or "This link doesn't exist")
- A search box to find the intended URL
- A link to create a new short URL

---

## Extended APIs

These APIs provide management, analytics, and batch capabilities. They are useful for building dashboards and integrations but are not the core interview focus.

### 3. DELETE /api/v1/urls/{suffix} — Delete Short URL

Permanently removes a URL mapping. The suffix returns to the "deleted" state (not reused, to avoid confusion).

```
DELETE /api/v1/urls/ab3k9x12
```

**Request Headers:**

| Header | Required | Description |
|---|---|---|
| `X-API-Key` | Yes | Must belong to the URL owner or an admin |

**Request:** No body.

**Response — 204 No Content:**

```
HTTP/1.1 204 No Content
X-Request-Id: 550e8400-e29b-41d4-a716-446655440000
```

**What happens server-side:**

```
1. Authenticate API key
2. Fetch URL mapping from DB
3. Authorization check: caller must be the URL owner (userId matches) or an admin
   → 403 FORBIDDEN if not authorized
4. Soft-delete in PostgreSQL:
   UPDATE url_mappings SET deleted_at = NOW() WHERE suffix = $1
5. Delete from Redis cache:
   DEL url:ab3k9x12
6. Return 204 No Content
```

**Why soft-delete?** We mark rows as deleted rather than physically removing them. This preserves audit trails, prevents suffix reuse (which could redirect to unintended targets), and allows recovery if deletion was accidental.

**Error Responses:**

| Status | Code | Description |
|---|---|---|
| 401 | `UNAUTHORIZED` | Missing or invalid API key |
| 403 | `FORBIDDEN` | Caller is not the URL owner or an admin |
| 404 | `URL_NOT_FOUND` | No mapping exists for this suffix |

---

### 4. GET /api/v1/urls/{suffix}/info — URL Information

Returns metadata about a short URL without performing a redirect. Useful for management dashboards and debugging.

```
GET /api/v1/urls/ab3k9x12/info
```

**Request Headers:**

| Header | Required | Description |
|---|---|---|
| `X-API-Key` | Yes | Must belong to the URL owner or an admin |

**Response — 200 OK:**

```json
{
    "suffix": "ab3k9x12",
    "shortUrl": "https://tinyurl.com/ab3k9x12",
    "longUrl": "https://example.com/very/long/path/to/page?param=value&utm_source=newsletter",
    "createdAt": "2026-02-09T10:30:00Z",
    "expiry": "2027-01-01T00:00:00Z",
    "userId": "user_abc123",
    "isExpired": false,
    "isCustomAlias": false,
    "totalClicks": 1547
}
```

**Response Fields:**

| Field | Type | Description |
|---|---|---|
| `suffix` | string | The 8-char suffix or custom alias |
| `shortUrl` | string | Full short URL |
| `longUrl` | string | The original target URL |
| `createdAt` | string (ISO 8601) | Creation timestamp |
| `expiry` | string (ISO 8601) | Expiration timestamp |
| `userId` | string or null | Owner's user ID, if set |
| `isExpired` | boolean | Whether the URL has passed its expiry |
| `isCustomAlias` | boolean | Whether this was a user-chosen alias vs auto-generated |
| `totalClicks` | integer | Total redirect count (from analytics, eventual consistency) |

**Error Responses:**

| Status | Code | Description |
|---|---|---|
| 401 | `UNAUTHORIZED` | Missing or invalid API key |
| 403 | `FORBIDDEN` | Caller is not the URL owner or an admin |
| 404 | `URL_NOT_FOUND` | No mapping exists for this suffix |

---

### 5. GET /api/v1/urls/{suffix}/stats — URL Statistics (Future Extension)

Returns detailed click analytics for a specific short URL. This endpoint requires an analytics pipeline (Kafka -> ClickHouse) that is out of scope for the core system design, but we define the API shape here for completeness.

```
GET /api/v1/urls/ab3k9x12/stats?from=2026-01-01T00:00:00Z&to=2026-02-09T23:59:59Z
```

**Request Parameters:**

| Parameter | Type | Required | Default | Description |
|---|---|---|---|---|
| `from` | string (ISO 8601) | No | 30 days ago | Start of analytics window |
| `to` | string (ISO 8601) | No | Now | End of analytics window |
| `granularity` | string | No | `day` | `hour`, `day`, or `week` |

**Response — 200 OK:**

```json
{
    "suffix": "ab3k9x12",
    "period": {
        "from": "2026-01-01T00:00:00Z",
        "to": "2026-02-09T23:59:59Z"
    },
    "totalClicks": 15847,
    "uniqueVisitors": 12403,
    "clicksByDay": [
        {"date": "2026-02-08", "clicks": 523, "uniqueVisitors": 412},
        {"date": "2026-02-07", "clicks": 487, "uniqueVisitors": 389},
        {"date": "2026-02-06", "clicks": 612, "uniqueVisitors": 498}
    ],
    "topReferrers": [
        {"referrer": "twitter.com", "clicks": 5420, "percentage": 34.2},
        {"referrer": "linkedin.com", "clicks": 3105, "percentage": 19.6},
        {"referrer": "direct", "clicks": 2847, "percentage": 18.0},
        {"referrer": "facebook.com", "clicks": 1523, "percentage": 9.6},
        {"referrer": "other", "clicks": 2952, "percentage": 18.6}
    ],
    "clicksByCountry": [
        {"country": "US", "clicks": 8234, "percentage": 52.0},
        {"country": "GB", "clicks": 2105, "percentage": 13.3},
        {"country": "IN", "clicks": 1847, "percentage": 11.7},
        {"country": "DE", "clicks": 1023, "percentage": 6.5},
        {"country": "other", "clicks": 2638, "percentage": 16.5}
    ],
    "clicksByDevice": [
        {"device": "mobile", "clicks": 9508, "percentage": 60.0},
        {"device": "desktop", "clicks": 5074, "percentage": 32.0},
        {"device": "tablet", "clicks": 1265, "percentage": 8.0}
    ]
}
```

**Implementation note:** Click data flows from the redirect endpoint via Kafka to ClickHouse. Analytics queries hit ClickHouse, not PostgreSQL. Data has eventual consistency — clicks may take up to 60 seconds to appear in stats.

```
Redirect Handler ──(async)──> Kafka (clicks topic) ──> ClickHouse (OLAP)
                                                              │
                                                              ▼
                                                    Stats API reads from here
```

---

### 6. GET /api/v1/users/{userId}/urls — List User's URLs

Returns a paginated list of all short URLs owned by a user. Supports filtering by status and cursor-based pagination.

```
GET /api/v1/users/user_abc123/urls?status=active&limit=50&cursor=eyJjcmVhdGVkQXQiOiIyMDI2LTAyLTA4VDEwOjMwOjAwWiJ9
```

**Request Parameters:**

| Parameter | Type | Required | Default | Description |
|---|---|---|---|---|
| `status` | string | No | `all` | Filter: `active` (not expired), `expired`, or `all` |
| `limit` | integer | No | 50 | Number of results per page (max 100) |
| `cursor` | string | No | — | Opaque pagination cursor from previous response |
| `sort` | string | No | `createdAt:desc` | Sort order: `createdAt:asc`, `createdAt:desc`, `clicks:desc` |

**Response — 200 OK:**

```json
{
    "userId": "user_abc123",
    "urls": [
        {
            "suffix": "ab3k9x12",
            "shortUrl": "https://tinyurl.com/ab3k9x12",
            "longUrl": "https://example.com/very/long/path",
            "createdAt": "2026-02-09T10:30:00Z",
            "expiry": "2027-01-01T00:00:00Z",
            "isExpired": false,
            "totalClicks": 1547
        },
        {
            "suffix": "zx8p3m5q",
            "shortUrl": "https://tinyurl.com/zx8p3m5q",
            "longUrl": "https://another-example.com/page",
            "createdAt": "2026-02-08T14:20:00Z",
            "expiry": "2027-02-08T14:20:00Z",
            "isExpired": false,
            "totalClicks": 823
        }
    ],
    "totalCount": 247,
    "nextCursor": "eyJjcmVhdGVkQXQiOiIyMDI2LTAyLTA4VDE0OjIwOjAwWiJ9",
    "hasMore": true
}
```

**Why cursor-based pagination (not offset)?**

| Approach | Pros | Cons |
|---|---|---|
| `OFFSET/LIMIT` | Simple, can jump to page N | Slow for large offsets (`OFFSET 10000` scans 10K rows), inconsistent with concurrent inserts |
| **Cursor-based** | Consistent results, O(1) regardless of page depth, works with `WHERE created_at < $cursor ORDER BY created_at DESC LIMIT 50` | Cannot jump to arbitrary page N |

For a URL list that users scroll through chronologically, cursor-based pagination is the clear winner.

**Error Responses:**

| Status | Code | Description |
|---|---|---|
| 401 | `UNAUTHORIZED` | Missing or invalid API key |
| 403 | `FORBIDDEN` | Caller does not own this userId |
| 400 | `INVALID_CURSOR` | Cursor is malformed or expired |

---

### 7. POST /api/v1/urls/batch — Batch URL Creation

Creates multiple short URLs in a single request. Useful for programmatic URL generation (e.g., marketing campaigns with hundreds of links).

```
POST /api/v1/urls/batch
```

**Request Headers:**

| Header | Required | Description |
|---|---|---|
| `X-API-Key` | Yes | API key for authentication |
| `X-Idempotency-Key` | Recommended | UUID for safe retries of the entire batch |

**Request Body:**

```json
{
    "urls": [
        {
            "longUrl": "https://example.com/page-1",
            "expiry": "2027-01-01T00:00:00Z"
        },
        {
            "longUrl": "https://example.com/page-2"
        },
        {
            "longUrl": "https://example.com/page-3",
            "customAlias": "promo-2026"
        }
    ],
    "userId": "user_abc123"
}
```

**Request Constraints:**

| Constraint | Value | Description |
|---|---|---|
| Max URLs per batch | 100 | Prevents oversized transactions |
| Max request body size | 1 MB | Nginx/ALB limit |
| Rate limit | 10 batch requests/min (free), 50/min (premium) | Prevents key pool exhaustion |

**Response — 200 OK (Partial Success Allowed):**

```json
{
    "results": [
        {
            "index": 0,
            "status": "created",
            "shortUrl": "https://tinyurl.com/m4n7p2q8",
            "suffix": "m4n7p2q8",
            "longUrl": "https://example.com/page-1",
            "expiry": "2027-01-01T00:00:00Z"
        },
        {
            "index": 1,
            "status": "created",
            "shortUrl": "https://tinyurl.com/k9x3j5w1",
            "suffix": "k9x3j5w1",
            "longUrl": "https://example.com/page-2",
            "expiry": "2027-02-09T10:30:00Z"
        },
        {
            "index": 2,
            "status": "error",
            "longUrl": "https://example.com/page-3",
            "error": {
                "code": "ALIAS_TAKEN",
                "message": "The custom alias 'promo-2026' is already in use"
            }
        }
    ],
    "summary": {
        "total": 3,
        "created": 2,
        "failed": 1
    }
}
```

**Server-side implementation:**

```
1. Validate all URLs upfront (fail-fast for obviously invalid requests)
2. Claim N keys in one transaction:
   UPDATE key_pool
   SET status = 'used', claimed_at = NOW()
   WHERE id IN (
       SELECT id FROM key_pool
       WHERE status = 'available'
       ORDER BY id
       LIMIT N                    ← N = number of URLs without customAlias
       FOR UPDATE SKIP LOCKED
   )
   RETURNING suffix;
3. Batch INSERT into url_mappings
4. Async: batch SET in Redis
5. Return results (some may succeed while others fail — partial success)
```

**Why partial success (not all-or-nothing)?** In a batch of 100 URLs, if 1 has a taken custom alias, the caller should not have to retry the other 99. Each URL in the batch is independent.

---

## Admin APIs (Internal Only — mTLS Required)

These APIs are not exposed to external clients. They are used by internal operations teams and automated systems. Authentication is via mutual TLS (mTLS) — no API keys.

### 8. POST /api/v1/admin/blocklist — Block Malicious URLs

Prevents creation of short URLs pointing to malicious destinations and blocks existing mappings.

```
POST /api/v1/admin/blocklist
```

**Request Body:**

```json
{
    "action": "ADD",
    "urls": [
        "malicious-site.com/phishing",
        "scam-domain.org/*",
        "*.malware-host.net"
    ],
    "reason": "phishing",
    "reportedBy": "trust-safety@tinyurl.com",
    "ticketId": "TRUST-2026-4521"
}
```

**Request Fields:**

| Field | Type | Required | Description |
|---|---|---|---|
| `action` | string | Yes | `ADD`, `REMOVE`, or `LIST` |
| `urls` | string[] | Yes (for ADD/REMOVE) | URL patterns to block. Supports wildcards (`*`). |
| `reason` | string | Yes (for ADD) | Reason: `phishing`, `malware`, `spam`, `legal`, `abuse` |
| `reportedBy` | string | Yes | Email of the person/system adding the block |
| `ticketId` | string | No | Reference to an internal ticket for audit trail |

**Response — 200 OK:**

```json
{
    "status": "applied",
    "patternsAdded": 3,
    "existingUrlsBlocked": 47,
    "totalBlocklistSize": 125847,
    "effectiveImmediately": true,
    "note": "47 existing short URLs pointing to these domains have been marked as blocked. They will now return 403 on redirect."
}
```

**What happens to existing URLs?**

When a domain is blocklisted:
1. New creations pointing to that domain are rejected with `403 URL_BLOCKED`
2. Existing short URLs pointing to that domain are marked as blocked in the DB
3. Redis cache entries for blocked URLs are invalidated
4. Redirect attempts to blocked URLs return `403 Forbidden` with a warning page

---

### 9. GET /api/v1/admin/health — System Health

Comprehensive health check for all system components. Used by monitoring dashboards and automated alerting.

```
GET /api/v1/admin/health
```

**Response — 200 OK:**

```json
{
    "status": "healthy",
    "timestamp": "2026-02-09T10:30:00Z",
    "components": {
        "database": {
            "leader": {
                "status": "up",
                "host": "pg-leader.internal:5432",
                "connections": {
                    "active": 45,
                    "idle": 55,
                    "max": 200
                },
                "replicationSlots": 3
            },
            "replicas": [
                {
                    "host": "pg-replica-1.internal:5432",
                    "status": "up",
                    "replicationLag": "150ms"
                },
                {
                    "host": "pg-replica-2.internal:5432",
                    "status": "up",
                    "replicationLag": "200ms"
                },
                {
                    "host": "pg-replica-3.internal:5432",
                    "status": "up",
                    "replicationLag": "180ms"
                }
            ]
        },
        "redis": {
            "status": "up",
            "mode": "cluster",
            "nodes": 6,
            "hitRate": "85.2%",
            "memoryUsage": "4.2GB / 32GB",
            "connectedClients": 120,
            "opsPerSecond": 45000
        },
        "keyPool": {
            "available": 2400000000,
            "used": 480000000,
            "total": 2880000000,
            "threshold": 1000000000,
            "status": "healthy",
            "estimatedRunway": "~310 days at current rate",
            "lastRefill": "2026-02-01T00:00:00Z"
        },
        "loadBalancer": {
            "status": "up",
            "activeConnections": 12500,
            "requestsPerSecond": 4200
        }
    }
}
```

**Health status values:**

| Status | Meaning | Action |
|---|---|---|
| `healthy` | All components operational, no degradation | None |
| `degraded` | System operational but with reduced capacity (e.g., 1 replica down) | Monitor, may self-heal |
| `unhealthy` | Critical component failure, service may be impaired | Page on-call, investigate immediately |

---

### 10. POST /api/v1/admin/keys/refill — Trigger Key Pool Refill

Manually triggers key pool replenishment. Normally, key generation runs as a scheduled background job when the pool drops below 1 billion keys. This endpoint allows manual intervention.

```
POST /api/v1/admin/keys/refill
```

**Request Body:**

```json
{
    "count": 100000000,
    "reason": "Proactive refill before marketing campaign launch",
    "requestedBy": "ops@tinyurl.com"
}
```

**Response — 202 Accepted:**

```json
{
    "status": "started",
    "jobId": "refill_20260209_103000",
    "keysToGenerate": 100000000,
    "estimatedTime": "15 minutes",
    "currentPoolSize": 2400000000,
    "targetPoolSize": 2500000000,
    "trackAt": "/api/v1/admin/keys/refill/status?jobId=refill_20260209_103000"
}
```

**Key generation process:**

```
1. Generate batch of random 8-char base-36 strings
2. Check for collisions against existing suffixes in DB (batch check)
3. Insert unique keys into key_pool table with status = 'available'
4. Rate: ~100K keys/sec insertion rate
5. 100M keys ≈ 15 minutes
```

**Key pool math (interview talking point):**

```
Suffix space:  36^8 = 2,821,109,907,456 (~2.8 trillion unique suffixes)
Creation rate:  600M URLs/month = ~7.2B URLs/year
Time to exhaust: 2.8T / 7.2B = ~390 years

Even at 10x growth, we have decades of runway before needing to:
  - Increase suffix length to 9 chars (36^9 = 101 trillion)
  - Or switch to base-62 (62^8 = 218 trillion)
```

---

### 11. GET /api/v1/admin/metrics — System Metrics

Returns real-time operational metrics. Powers the operations dashboard.

```
GET /api/v1/admin/metrics?window=5m
```

**Request Parameters:**

| Parameter | Type | Default | Description |
|---|---|---|---|
| `window` | string | `5m` | Time window for aggregation: `1m`, `5m`, `15m`, `1h` |

**Response — 200 OK:**

```json
{
    "window": "5m",
    "timestamp": "2026-02-09T10:30:00Z",
    "throughput": {
        "createPerSecond": 235,
        "redirectPerSecond": 3842,
        "deletePerSecond": 12,
        "totalPerSecond": 4089
    },
    "latency": {
        "redirect": {
            "p50": "2ms",
            "p95": "8ms",
            "p99": "25ms",
            "p999": "120ms"
        },
        "create": {
            "p50": "15ms",
            "p95": "45ms",
            "p99": "100ms",
            "p999": "350ms"
        }
    },
    "cache": {
        "hitRate": "85.2%",
        "missRate": "14.8%",
        "evictions": 1250,
        "memoryUsage": "4.2GB / 32GB"
    },
    "errors": {
        "rate": "0.02%",
        "byCode": {
            "400": 45,
            "401": 12,
            "404": 890,
            "409": 3,
            "410": 234,
            "429": 67,
            "500": 2,
            "503": 0
        }
    },
    "database": {
        "activeConnections": 45,
        "queryRate": 620,
        "avgQueryTime": "3ms",
        "replicationLag": "165ms"
    },
    "urls": {
        "totalActive": 487234567,
        "totalExpired": 123456789,
        "createdLast24h": 19800000,
        "expiredLast24h": 4200000
    }
}
```

---

## API Summary

### Core APIs (Interview Focus)

| # | Method | Endpoint | Description | Auth | Cache | Traffic |
|---|---|---|---|---|---|---|
| 1 | POST | `/api/v1/urls` | Create short URL | API Key | N/A | ~230/sec |
| 2 | GET | `/{suffix}` | Redirect to long URL | None | Redis + CDN | ~3,800/sec |

### Extended APIs

| # | Method | Endpoint | Description | Auth | Rate Limit |
|---|---|---|---|---|---|
| 3 | DELETE | `/api/v1/urls/{suffix}` | Delete a short URL | API Key (owner) | 50/min |
| 4 | GET | `/api/v1/urls/{suffix}/info` | URL metadata | API Key (owner) | 100/min |
| 5 | GET | `/api/v1/urls/{suffix}/stats` | Click analytics | API Key (owner) | 30/min |
| 6 | GET | `/api/v1/users/{userId}/urls` | List user's URLs | API Key (owner) | 30/min |
| 7 | POST | `/api/v1/urls/batch` | Batch create URLs | API Key | 10/min (free) |

### Admin APIs (Internal)

| # | Method | Endpoint | Description | Auth |
|---|---|---|---|---|
| 8 | POST | `/api/v1/admin/blocklist` | Block malicious URLs | mTLS |
| 9 | GET | `/api/v1/admin/health` | System health check | mTLS |
| 10 | POST | `/api/v1/admin/keys/refill` | Trigger key pool refill | mTLS |
| 11 | GET | `/api/v1/admin/metrics` | Real-time system metrics | mTLS |

---

## Design Decisions

### Why REST Over gRPC?

| Factor | REST | gRPC |
|---|---|---|
| Browser redirect compatibility | Browsers natively follow HTTP 302 Location headers | gRPC cannot issue browser redirects — would need a REST gateway |
| URL simplicity | `tinyurl.com/ab3k9x12` — clean, shareable | Would require REST gateway for the redirect path regardless |
| Client accessibility | Any HTTP client (curl, browsers, mobile apps) | Requires protobuf/gRPC client libraries |
| CDN caching | GET redirects are cacheable by CDN | Not applicable — POST-based |
| Tooling/debugging | Browser DevTools, curl, Postman | grpcurl, specialized tools |
| Performance | JSON overhead is negligible for our payload sizes (~200 bytes) | Protobuf would save ~50 bytes — irrelevant at this scale |

**Verdict:** REST for all APIs. The redirect endpoint (`GET /{suffix}`) MUST work in browsers — this is non-negotiable. Since we need REST for redirects anyway, using REST for all APIs keeps the system simple. gRPC could be used for internal service-to-service communication if we decompose into microservices later, but for the interview scope, REST is the right choice.

### Why 302 (Temporary) as Default?

```
                    ┌──────────────────────────────────────────┐
                    │ 302 gives us flexibility at the cost of  │
                    │ slightly higher server load. The server   │
                    │ load is trivially handleable.            │
                    └──────────────────────────────────────────┘

302 enables:
  ✓ URL expiration     — server can return 410 after expiry
  ✓ URL deletion       — server can return 404 after deletion
  ✓ Click analytics    — every visit hits server, so we can count
  ✓ Target URL updates — owner can change where the link points
  ✓ A/B testing        — redirect different percentages to different targets
  ✓ Geo-routing        — redirect to localized versions based on country

301 would break all of the above for repeat visitors.

Cost of 302:
  15,000 redirects/sec (peak) × 200 bytes = 3 MB/sec outbound
  This is ~0.003% of a 10 Gbps network link — completely negligible.
```

### Rate Limiting Strategy

| Client Type | Create | Redirect | Batch | Delete | Info/Stats |
|---|---|---|---|---|---|
| Anonymous (by IP) | 10/min | Unlimited | N/A | N/A | N/A |
| Free tier (API key) | 100/min | Unlimited | 5 batch/min | 50/min | 100/min |
| Premium (API key) | 1,000/min | Unlimited | 50 batch/min | 500/min | 1,000/min |
| Internal (mTLS) | 10,000/min | Unlimited | Unlimited | Unlimited | Unlimited |

**Implementation:** Redis-based sliding window counter.

```
Key format:   ratelimit:{apiKey}:{endpoint}:{minute_bucket}
Example:      ratelimit:key_abc123:create:202602091030
TTL:          120 seconds (auto-cleanup)
Operation:    INCR + EXPIRE (atomic via Lua script)

Lua script for atomic rate limit check:
  local current = redis.call('INCR', KEYS[1])
  if current == 1 then
      redis.call('EXPIRE', KEYS[1], 120)
  end
  return current

If the returned count exceeds the limit → return 429 with Retry-After header.
```

### Why No Auth for Redirect?

Short URLs are shared publicly — in tweets, emails, text messages, QR codes, printed materials. Adding authentication to the redirect path would break the entire use case.

Security for the redirect path comes from multiple layers:

| Layer | Protection |
|---|---|
| Random suffixes | 8-char base-36 = 2.8T combinations. Cannot enumerate or guess URLs. |
| URL blocklist | Malicious destination domains are blocked. Redirect returns 403. |
| Rate limiting | DDoS protection via per-IP rate limiting on the redirect endpoint (if needed). |
| Monitoring | Anomaly detection on redirect patterns (sudden spikes to same suffix). |
| Expiry | URLs expire by default (1 year). Stale links become harmless 410s. |

### Response Size Optimization for Redirect

The redirect endpoint is the hot path. Every byte matters at 15,000 req/sec peak.

```
Redirect response breakdown:
  HTTP/1.1 302 Found\r\n                                    → 22 bytes
  Location: https://example.com/path?params\r\n             → ~80 bytes (varies)
  Cache-Control: private, max-age=300\r\n                   → 37 bytes
  X-Request-Id: 550e8400-e29b-41d4-a716-446655440000\r\n   → 52 bytes
  Content-Length: 0\r\n                                     → 19 bytes
  \r\n                                                      → 2 bytes
  ─────────────────────────────────────────────────────────────────
  Total: ~212 bytes per response

  At 15,000 req/sec peak: 15,000 × 212 = 3.18 MB/sec outbound

  For comparison:
  - A single HD video stream: ~5 MB/sec
  - Our entire redirect traffic: less than one video stream
```

### Idempotency Strategy

For the create endpoint, idempotency prevents duplicate URL creation on retries (e.g., network timeout where the client does not know if the creation succeeded).

```
Client sends:
  POST /api/v1/urls
  X-Idempotency-Key: 7f3a1b2c-4d5e-6f7a-8b9c-0d1e2f3a4b5c

Server behavior:
  1. Check Redis: GET idempotency:{key}
  2. If found → return the cached response (no new URL created)
  3. If not found → process normally → cache response in Redis
     SET idempotency:{key} {response_json} EX 86400  (24-hour TTL)

This ensures:
  - First request: creates URL, caches response
  - Retry (same key): returns identical response, no duplicate
  - Different key: creates new URL (as expected)
```

### Why PostgreSQL (Not DynamoDB/Cassandra)?

This is a deeper discussion covered in [SQL vs NoSQL Tradeoffs](sql-vs-nosql-tradeoffs.md), but the API-level implications are:

| Feature | PostgreSQL Impact on API | NoSQL Impact on API |
|---|---|---|
| `FOR UPDATE SKIP LOCKED` | Enables our contention-free key claim | Not available — would need different key generation strategy |
| Transactions | Batch create is a single atomic operation | Each URL is an independent write — partial failures are messier |
| Strong consistency | Read-after-write guarantee for create → redirect | Eventual consistency — newly created URL might 404 briefly |
| Cursor pagination | Efficient `WHERE created_at < $cursor` | Depends on partition key design — may need secondary index |

---

## Appendix: cURL Examples

**Create a short URL:**

```bash
curl -X POST https://tinyurl.com/api/v1/urls \
  -H "Content-Type: application/json" \
  -H "X-API-Key: key_abc123def456" \
  -d '{
    "longUrl": "https://example.com/very/long/path?param=value",
    "expiry": "2027-01-01T00:00:00Z"
  }'
```

**Create with custom alias:**

```bash
curl -X POST https://tinyurl.com/api/v1/urls \
  -H "Content-Type: application/json" \
  -H "X-API-Key: key_abc123def456" \
  -d '{
    "longUrl": "https://example.com/product-launch",
    "customAlias": "launch-2026"
  }'
```

**Test a redirect (without following):**

```bash
curl -I https://tinyurl.com/ab3k9x12

# Output:
# HTTP/1.1 302 Found
# Location: https://example.com/very/long/path?param=value
# Cache-Control: private, max-age=300
```

**Delete a short URL:**

```bash
curl -X DELETE https://tinyurl.com/api/v1/urls/ab3k9x12 \
  -H "X-API-Key: key_abc123def456"
```

**Get URL info:**

```bash
curl https://tinyurl.com/api/v1/urls/ab3k9x12/info \
  -H "X-API-Key: key_abc123def456"
```

**Batch create:**

```bash
curl -X POST https://tinyurl.com/api/v1/urls/batch \
  -H "Content-Type: application/json" \
  -H "X-API-Key: key_abc123def456" \
  -H "X-Idempotency-Key: 7f3a1b2c-4d5e-6f7a-8b9c-0d1e2f3a4b5c" \
  -d '{
    "urls": [
      {"longUrl": "https://example.com/page-1"},
      {"longUrl": "https://example.com/page-2"},
      {"longUrl": "https://example.com/page-3"}
    ],
    "userId": "user_abc123"
  }'
```

---

*This document complements the [Interview Simulation](interview-simulation.md). For detailed system flows, see [Flow](flow.md). For database schema details, see [SQL vs NoSQL Tradeoffs](sql-vs-nosql-tradeoffs.md).*
