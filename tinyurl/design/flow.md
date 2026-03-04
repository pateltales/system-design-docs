# TinyURL System — System Flows

> This document depicts and explains all the major flows in the TinyURL system. Each flow includes an ASCII sequence diagram, step-by-step latency breakdown, edge cases, and failure points.

**System context**:
- URL creation: 230 writes/sec average, 1,000 peak
- URL redirect: 3,800 reads/sec average, 15,000 peak
- PostgreSQL (leader + read replicas) with `FOR UPDATE SKIP LOCKED` for key generation
- Redis cache (cache-aside pattern, ~5-50GB)
- 8-char base-36 suffixes, URLs expire (default 1 year)
- 302 redirects by default (temporary — allows analytics and expiry enforcement)
- Pre-allocated key pool with background refill
- Multi-region: US-EAST (leader), EU-WEST and AP-EAST (followers)

---

## Table of Contents

1. [URL Creation (Write Path)](#1-url-creation-write-path)
2. [URL Redirect — Cache Hit (Hot Path)](#2-url-redirect--cache-hit-hot-path)
3. [URL Redirect — Cache Miss (Full Path)](#3-url-redirect--cache-miss-full-path)
4. [Key Pool Replenishment (Background Job)](#4-key-pool-replenishment-background-job)
5. [URL Expiry / Cleanup](#5-url-expiry--cleanup)
6. [Failover — Leader DB Down](#6-failover--leader-db-down)
7. [Failover — Redis Down](#7-failover--redis-down)
8. [Multi-Region Redirect](#8-multi-region-redirect)

---

## 1. URL Creation (Write Path)

### Happy Path — Shorten a Long URL

A user submits a long URL and receives a short URL backed by a pre-allocated 8-char base-36 suffix claimed atomically from the key pool.

```
Client          API Gateway       App Server        PostgreSQL (Leader)     Redis
  |                 |                |                    |                   |
  |  POST /api/v1/  |                |                    |                   |
  |  urls            |                |                    |                   |
  |  {long_url:      |                |                    |                   |
  |   "https://..."}|                |                    |                   |
  |────────────────>|                |                    |                   |
  |                 |                |                    |                   |
  |                 | Authenticate   |                    |                   |
  |                 | + validate     |                    |                   |
  |                 | API key        |                    |                   |
  |                 |                |                    |                   |
  |                 | Rate limit     |                    |                   |
  |                 | check (Redis   |                    |                   |
  |                 | INCR + EXPIRE) |                    |                   |
  |                 |───────────────>|                    |                   |
  |                 |                |                    |                   |
  |                 |                | Validate URL       |                   |
  |                 |                | format (RFC 3986)  |                   |
  |                 |                |                    |                   |
  |                 |                | Check blocklist    |                   |
  |                 |                | (Redis SISMEMBER)  |                   |
  |                 |                |──────────────────────────────────────>|
  |                 |                |                    |                   |
  |                 |                |<──────────────────────────────────────|
  |                 |                | Not blocked        |                   |
  |                 |                |                    |                   |
  |                 |                | Claim key:         |                   |
  |                 |                | FOR UPDATE         |                   |
  |                 |                | SKIP LOCKED        |                   |
  |                 |                | CTE                |                   |
  |                 |                |───────────────────>|                   |
  |                 |                |                    |                   |
  |                 |                |  Row claimed:      |                   |
  |                 |                |  suffix="ab3k9x12" |                   |
  |                 |                |<───────────────────|                   |
  |                 |                |                    |                   |
  |                 |                | SET cache (async,  |                   |
  |                 |                | non-blocking):     |                   |
  |                 |                | "ab3k9x12" ->      |                   |
  |                 |                | long_url           |                   |
  |                 |                |──────────────────────────────────────>|
  |                 |                |                    |                   |
  |                 |  201 Created   |                    |                   |
  |                 |  {short_url:   |                    |                   |
  |                 |   "tinyurl.com |                    |                   |
  |                 |   /ab3k9x12"} |                    |                   |
  |<────────────────|<───────────────|                    |                   |
  |                 |                |                    |                   |
```

### The SQL — FOR UPDATE SKIP LOCKED CTE

This is the key claim query. It atomically finds an unclaimed row and updates it in a single round-trip:

```sql
WITH claimed AS (
    SELECT suffix
    FROM url_mappings
    WHERE long_url IS NULL
      AND expiry_time < NOW()       -- available (expired or never used)
    ORDER BY suffix
    LIMIT 1
    FOR UPDATE SKIP LOCKED          -- skip rows locked by other transactions
)
UPDATE url_mappings
SET long_url    = :long_url,
    user_id     = :user_id,
    created_at  = NOW(),
    expiry_time = NOW() + INTERVAL '1 year'
WHERE suffix = (SELECT suffix FROM claimed)
RETURNING suffix, long_url, expiry_time;
```

**Why `SKIP LOCKED`?** Under concurrent writes (up to 1,000/sec peak), multiple app server instances try to claim keys simultaneously. `SKIP LOCKED` ensures each transaction claims a different row without blocking — no lock contention, no deadlocks.

### Step-by-Step Breakdown

| Step | Component | Action | Latency |
|---|---|---|---|
| 1 | API Gateway | Receive request, extract and validate API key | ~1ms |
| 2 | API Gateway | Rate limit check: `INCR user:{api_key}:minute` with `EXPIRE 60` (Redis) | ~1ms |
| 3 | App Server | Validate URL format against RFC 3986 (regex + DNS resolution check) | <1ms |
| 4 | App Server | Check URL blocklist: `SISMEMBER blocklist:urls <long_url>` (Redis) | ~1ms |
| 5 | PostgreSQL | Execute `FOR UPDATE SKIP LOCKED` CTE — claim one pre-allocated key | ~3-5ms |
| 6 | Redis | `SET url:ab3k9x12 <long_url> EX 31536000` (async, fire-and-forget) | ~1ms (async) |
| 7 | App Server | Build JSON response with short URL, expiry time, metadata | <1ms |
| **Total** | | | **~7-10ms** |

### Response Headers

```
HTTP/1.1 201 Created
Content-Type: application/json
Location: https://tinyurl.com/ab3k9x12
X-Request-Id: req_7f3a2b1c
X-RateLimit-Remaining: 94
X-RateLimit-Reset: 1706198460
Cache-Control: no-store
```

### Edge Cases

- **Custom alias requested**: Skip the `SKIP LOCKED` path entirely. Instead execute a direct insert:
  ```sql
  INSERT INTO url_mappings (suffix, long_url, user_id, created_at, expiry_time)
  VALUES (:custom_alias, :long_url, :user_id, NOW(), NOW() + INTERVAL '1 year')
  ON CONFLICT (suffix) DO NOTHING
  RETURNING *;
  ```
  If no row returned, the alias is already taken. Return `409 Conflict` with `error: ALIAS_TAKEN`.

- **Key pool exhausted**: The `SKIP LOCKED` query returns no rows (all pre-allocated keys are either in-use or locked by concurrent transactions). Response: return `503 Service Unavailable` with `Retry-After: 60` header. Simultaneously: trigger emergency key refill job and page on-call via PagerDuty with a critical alert.

- **URL on blocklist**: `SISMEMBER blocklist:urls <long_url>` returns `1` (true). Return `400 Bad Request` with `error: BLOCKED_URL, message: "This URL has been flagged as malicious or violates our terms of service."` Do not reveal the specific blocklist rule to the client.

- **URL format invalid**: Fails RFC 3986 validation (missing scheme, invalid characters, unresolvable host). Return `400 Bad Request` with `error: INVALID_URL`.

- **DB leader unavailable**: PostgreSQL leader is unreachable. The write fails. Circuit breaker opens after 5 consecutive failures within 10 seconds. Return `503 Service Unavailable`. Reads (redirects) are unaffected because they use read replicas and Redis cache.

- **Rate limit exceeded**: Redis `INCR` returns a value exceeding the per-user limit (100/minute for free tier, 1,000/minute for paid). Return `429 Too Many Requests` with `Retry-After` header.

- **Duplicate long URL submitted**: By design, the same long URL can have multiple short URLs (each creation is independent). If the caller wants idempotency, they should send an `Idempotency-Key` header; the app server checks Redis for the key and returns the previously created short URL if it exists.

---

## 2. URL Redirect — Cache Hit (Hot Path)

### Happy Path — Fastest Redirect via Redis Cache

This is the common case: a user clicks a short URL, the mapping is found in Redis, and the user is immediately redirected. Approximately **80% of all redirects** follow this path because popular URLs are accessed repeatedly and remain warm in cache.

```
Client              App Server (LB)          Redis                  PostgreSQL
  |                      |                     |                        |
  |  GET /ab3k9x12       |                     |                        |
  |  Host: tinyurl.com   |                     |                        |
  |─────────────────────>|                     |                        |
  |                      |                     |                        |
  |                      | GET url:ab3k9x12    |                        |
  |                      |────────────────────>|                        |
  |                      |                     |                        |
  |                      |  HIT!               |                        |
  |                      |  "https://www.      |                        |
  |                      |   example.com/      |                        |
  |                      |   very/long/path"   |                        |
  |                      |<────────────────────|                        |
  |                      |                     |                        |
  |                      | Check expiry:       |                        |
  |                      | TTL > 0? Yes        |                        |
  |                      |                     |                        |
  |  302 Found           |                     |                        |
  |  Location: https://  |                     |                        |
  |   www.example.com/   |                     |                        |
  |   very/long/path     |                     |                        |
  |<─────────────────────|                     |                        |
  |                      |                     |                        |
  |  (Browser follows    |                     |                        |
  |   redirect to        |                     |                        |
  |   destination)       |                     |                        |
  |                      |                     |                        |
```

### Step-by-Step Breakdown

| Step | Component | Action | Latency |
|---|---|---|---|
| 1 | Client | DNS resolution for `tinyurl.com` (cached by browser/OS) | ~0ms (cached) |
| 2 | Client | TCP + TLS handshake to nearest edge / load balancer | ~2-3ms (same region) |
| 3 | App Server | Parse path, extract suffix `ab3k9x12` | <0.1ms |
| 4 | App Server | Redis GET: `GET url:ab3k9x12` | ~1ms |
| 5 | Redis | Cache lookup — key found, return value | <0.5ms (server-side) |
| 6 | App Server | Validate TTL is positive (not expired) | <0.1ms |
| 7 | App Server | Build 302 response with Location header | <0.1ms |
| 8 | App Server | Send response to client | ~1ms |
| **Total (server-side)** | | | **~2-3ms** |
| **Total (user-perceived)** | | Including network RTT | **~5-8ms** |

### Response Headers

```
HTTP/1.1 302 Found
Location: https://www.example.com/very/long/path
Cache-Control: private, max-age=0, no-cache
X-Request-Id: req_9d4e8f2a
X-Served-From: cache
Content-Length: 0
```

**Why 302 (temporary) instead of 301 (permanent)?**
- 301 tells the browser to cache the redirect permanently — subsequent visits skip our server entirely.
- 302 forces every visit through our server, enabling: (a) analytics/click counting, (b) expiry enforcement, (c) URL updates if the owner changes the destination, (d) abuse detection.
- The tradeoff is slightly higher server load, but at 3,800 reads/sec this is well within capacity.

**Why `Cache-Control: private, max-age=0, no-cache`?**
- Prevents ISPs, CDNs, and shared caches from caching the redirect.
- Each request must reach our app server so we can enforce expiry and track analytics.

### What "Cache Hit" Means

A "cache hit" means the Redis key `url:<suffix>` exists and contains the destination long URL. The key was populated either:
1. **On creation** (Flow 1, step 6): When the URL was first shortened, the mapping was written to Redis asynchronously.
2. **On previous cache miss** (Flow 3, step 6): A prior redirect for this suffix missed the cache, fetched from PostgreSQL, and backfilled Redis.

Cache entries have a TTL matching the URL's expiry time (default 1 year). When the TTL expires, the key is evicted, and subsequent requests follow the cache-miss path (Flow 3).

### Edge Cases

- **Suffix not in Redis but URL exists in DB**: This is a cache miss — falls through to Flow 3.
- **Redis returns the value but TTL has already expired** (race condition): Extremely rare. The App Server checks the TTL via `TTL url:ab3k9x12` alongside the GET. If TTL <= 0, treat as expired and follow the cache-miss path to verify against the DB.
- **Malformed suffix** (non-base-36 characters, wrong length): Return `400 Bad Request` immediately without hitting Redis or DB. Regex validation: `^[a-z0-9]{8}$`.

---

## 3. URL Redirect — Cache Miss (Full Path)

### Redis Miss — Fall Back to PostgreSQL Read Replica

This path handles ~20% of redirects: the suffix is not in Redis (first access, cache eviction, or long-tail URL that hasn't been accessed recently). The app server queries a PostgreSQL read replica, checks expiry, and backfills the cache for future requests.

```
Client              App Server (LB)          Redis              PostgreSQL (Read Replica)
  |                      |                     |                        |
  |  GET /xk7m2p9q       |                     |                        |
  |  Host: tinyurl.com   |                     |                        |
  |─────────────────────>|                     |                        |
  |                      |                     |                        |
  |                      | GET url:xk7m2p9q    |                        |
  |                      |────────────────────>|                        |
  |                      |                     |                        |
  |                      |  MISS (nil)         |                        |
  |                      |<────────────────────|                        |
  |                      |                     |                        |
  |                      | SELECT long_url,    |                        |
  |                      |   expiry_time       |                        |
  |                      | FROM url_mappings   |                        |
  |                      | WHERE suffix =      |                        |
  |                      |   'xk7m2p9q'        |                        |
  |                      |────────────────────────────────────────────>|
  |                      |                     |                        |
  |                      |  Row found:         |                        |
  |                      |  long_url = "..."   |                        |
  |                      |  expiry = 2027-01-15|                        |
  |                      |<────────────────────────────────────────────|
  |                      |                     |                        |
  |                      | Check: expiry_time  |                        |
  |                      | > NOW()? YES        |                        |
  |                      |                     |                        |
  |                      | Backfill cache:     |                        |
  |                      | SET url:xk7m2p9q    |                        |
  |                      | <long_url>          |                        |
  |                      | EX <seconds_until   |                        |
  |                      |     expiry>         |                        |
  |                      | (async, non-block)  |                        |
  |                      |────────────────────>|                        |
  |                      |                     |                        |
  |  302 Found           |                     |                        |
  |  Location: <long_url>|                     |                        |
  |<─────────────────────|                     |                        |
  |                      |                     |                        |
```

### Step-by-Step Breakdown

| Step | Component | Action | Latency |
|---|---|---|---|
| 1 | Client | TCP + TLS to load balancer | ~2-3ms |
| 2 | App Server | Parse path, extract suffix `xk7m2p9q` | <0.1ms |
| 3 | App Server | Redis GET: `GET url:xk7m2p9q` | ~1ms |
| 4 | Redis | Cache lookup — key not found, return nil | <0.5ms |
| 5 | App Server | Open connection to read replica (pooled) | ~1ms |
| 6 | PostgreSQL | `SELECT long_url, expiry_time FROM url_mappings WHERE suffix = 'xk7m2p9q'` — B-tree index lookup on `suffix` | ~5-10ms |
| 7 | App Server | Check `expiry_time > NOW()` — URL still valid | <0.1ms |
| 8 | Redis | Backfill: `SET url:xk7m2p9q <long_url> EX <ttl>` (async, fire-and-forget) | ~1ms (async) |
| 9 | App Server | Build 302 response | <0.1ms |
| **Total (server-side)** | | | **~8-13ms** |
| **Total (user-perceived)** | | Including network RTT | **~15-25ms** |

### Latency Budget Breakdown

```
+---------------------------------------------------+
|              Total: ~20ms (typical)               |
+------+------+------+--------+-------+------+-----+
| Net  |Redis | Net  |   DB   |Check  |Redis | Bld |
| in   |miss  |to DB | query  |expiry | SET  | rsp |
| 2ms  | 1ms  | 1ms  |5-10ms  | <1ms  | 1ms  |<1ms |
+------+------+------+--------+-------+------+-----+
```

### Response Headers

```
HTTP/1.1 302 Found
Location: https://www.example.com/article/12345
Cache-Control: private, max-age=0, no-cache
X-Request-Id: req_3c7a9e1f
X-Served-From: db
Content-Length: 0
```

Note the `X-Served-From: db` header (vs. `cache` in Flow 2). This is useful for debugging and monitoring cache hit ratios.

### Edge Cases

- **URL found but expired** (`expiry_time < NOW()`):
  Return `410 Gone` with body `{"error": "URL_EXPIRED", "message": "This short URL has expired."}`.
  Do NOT cache the expired entry in Redis — the row will be recycled by the key pool (the suffix will be reused for a new URL). Caching it would serve stale data after recycling.

- **URL not found** (suffix does not exist in DB):
  Return `404 Not Found`.
  Optionally cache a negative result: `SET url:xk7m2p9q __NOT_FOUND__ EX 60` — this prevents repeated DB queries for the same non-existent suffix (protects against enumeration attacks or broken links generating sustained DB load). The 60-second TTL ensures newly created URLs become accessible quickly.

- **DB replica lag** (URL was just created but replica hasn't replicated yet):
  The URL was written to the leader (Flow 1) but the read replica is a few hundred milliseconds behind. The cache-miss path queries the replica and gets no result.
  - This is acceptable because: the user who created the URL hasn't shared it yet (they just received the short URL moments ago). By the time they share it and someone clicks it, replication will have caught up.
  - If the creator themselves immediately tests the link: they might get a brief 404. A simple client retry after 1 second resolves this.
  - In the worst case, the app server could fall back to the leader for a re-read, but this adds complexity for an extremely rare edge case.

- **Thundering herd on cache miss** (many concurrent requests for the same uncached suffix):
  If a viral URL's cache entry expires, hundreds of simultaneous requests could all miss the cache and slam the DB.
  Mitigation: Use a **singleflight / request coalescing** pattern — the first request that misses the cache acquires a short-lived lock (`SET url:xk7m2p9q:lock NX EX 5`), queries the DB, and populates the cache. All other concurrent requests for the same suffix wait on the lock (with a short timeout) and then read from the freshly populated cache.

---

## 4. Key Pool Replenishment (Background Job)

### Ensuring the Pre-Allocated Key Pool Never Runs Dry

The system pre-allocates billions of 8-char base-36 suffixes in the `url_mappings` table. These rows have `long_url = NULL` and `expiry_time = epoch` (1970-01-01), indicating they are available for claiming. A background job monitors the pool size and refills it when it drops below a threshold.

```
Scheduler (cron)    Key Generator Svc    PostgreSQL (Leader)     Monitoring (CloudWatch)
      |                   |                    |                        |
      | 1. Hourly         |                    |                        |
      |    trigger        |                    |                        |
      |──────────────────>|                    |                        |
      |                   |                    |                        |
      |                   | 2. Check pool size |                        |
      |                   | SELECT reltuples   |                        |
      |                   | FROM pg_class      |                        |
      |                   | WHERE relname =    |                        |
      |                   | 'url_mappings'     |                        |
      |                   |───────────────────>|                        |
      |                   |                    |                        |
      |                   | 3. Estimated total:|                        |
      |                   |    2.8 billion rows|                        |
      |                   |<───────────────────|                        |
      |                   |                    |                        |
      |                   | 4. Check used count|                        |
      |                   | SELECT COUNT(*)    |                        |
      |                   | FROM url_mappings  |                        |
      |                   | WHERE long_url     |                        |
      |                   |   IS NOT NULL      |                        |
      |                   |   AND expiry_time  |                        |
      |                   |   > NOW()          |                        |
      |                   |───────────────────>|                        |
      |                   |                    |                        |
      |                   | 5. Active URLs:    |                        |
      |                   |    1.9 billion     |                        |
      |                   |<───────────────────|                        |
      |                   |                    |                        |
      |                   | 6. Available =     |                        |
      |                   |    2.8B - 1.9B     |                        |
      |                   |    = 900 million   |                        |
      |                   |    < 1B threshold  |                        |
      |                   |    -> REFILL!      |                        |
      |                   |                    |                        |
      |                   | 7. Generate 100M   |                        |
      |                   |    random 8-char   |                        |
      |                   |    base-36 strings |                        |
      |                   |    in memory       |                        |
      |                   |                    |                        |
      |                   | 8. Deduplicate     |                        |
      |                   |    in memory       |                        |
      |                   |    (HashSet)       |                        |
      |                   |    ~99.997% unique |                        |
      |                   |                    |                        |
      |                   | 9. Batch INSERT    |                        |
      |                   |    via COPY:       |                        |
      |                   |    COPY url_mappings|                       |
      |                   |    (suffix,         |                       |
      |                   |     expiry_time)   |                        |
      |                   |    FROM STDIN      |                        |
      |                   |    ON CONFLICT     |                        |
      |                   |    (suffix)        |                        |
      |                   |    DO NOTHING      |                        |
      |                   |───────────────────>|                        |
      |                   |                    |                        |
      |                   |                    | 10. Insert rows,      |
      |                   |                    |     skip duplicates   |
      |                   |                    |     (~15 minutes)     |
      |                   |                    |                        |
      |                   | 11. Result:        |                        |
      |                   |     99,970,000     |                        |
      |                   |     inserted       |                        |
      |                   |     30,000 dupes   |                        |
      |                   |     skipped        |                        |
      |                   |<───────────────────|                        |
      |                   |                    |                        |
      |                   | 12. Log results +  |                        |
      |                   |     emit metrics   |                        |
      |                   |───────────────────────────────────────────>|
      |                   |                    |                        |
      |                   |                    |     Metric:            |
      |                   |                    |     key_pool_available |
      |                   |                    |     = 999,970,000     |
      |                   |                    |                        |
```

### Step-by-Step Breakdown

| Step | Phase | Action | Duration |
|---|---|---|---|
| 1 | Trigger | Cron fires hourly (or on-demand if pool < emergency threshold) | instant |
| 2-3 | Pool Size Check | `SELECT reltuples FROM pg_class` — fast approximate row count (no full table scan) | ~1ms |
| 4-5 | Active Count | `SELECT COUNT(*) FROM url_mappings WHERE long_url IS NOT NULL AND expiry_time > NOW()` — uses partial index | ~5-10s |
| 6 | Decision | Available = total - active. If < 1 billion: proceed with refill | instant |
| 7 | Generation | Generate 100M random strings: 8 chars each from `[a-z0-9]` (36 chars). Uses `SecureRandom` for uniform distribution. Memory: ~100M * 8 bytes = ~800MB | ~30s |
| 8 | Dedup | Insert into in-memory `HashSet`. Collision probability per string: `100M / 36^8 = 100M / 2.8T ~ 0.003%`. Out of 100M, expect ~3,000 internal duplicates | ~10s |
| 9-10 | Batch Insert | `COPY ... FROM STDIN` with `ON CONFLICT DO NOTHING`. PostgreSQL's COPY is orders of magnitude faster than individual INSERTs. Processes ~110K rows/sec | ~15 min |
| 11 | Result | Log: 99,970,000 inserted, 30,000 skipped (collisions with existing keys) | instant |
| 12 | Monitoring | Emit CloudWatch metric `key_pool_available`. Set alarm if < 100M | instant |
| **Total** | | | **~15-20 minutes** |

### Key Space Mathematics

```
Total possible 8-char base-36 suffixes: 36^8 = 2,821,109,907,456 (~2.8 trillion)

At 230 URL creations/sec:
  - Per day:    230 * 86,400  = ~20 million
  - Per year:   20M * 365     = ~7.3 billion
  - Per 5 years:              = ~36.5 billion

With key recycling (expired URLs free their suffix):
  - Active URLs at any time (1-year expiry): ~7.3 billion
  - Key space utilization: 7.3B / 2.8T = 0.26%

Conclusion: Key space is effectively infinite. Collisions during refill are negligible.
```

### Thresholds and Alerts

| Metric | Threshold | Action |
|---|---|---|
| Available keys | < 1 billion | Normal refill (cron job runs) |
| Available keys | < 100 million | WARN: Increase refill batch size, run more frequently |
| Available keys | < 10 million | CRITICAL: Page on-call, emergency refill, investigate why consumption spiked |
| Available keys | 0 | FATAL: URL creation returns 503. All hands on deck. |

### Edge Cases

- **Refill job overlaps with high write traffic**: The COPY command acquires row-level locks, not table-level locks. The `FOR UPDATE SKIP LOCKED` queries in Flow 1 simply skip any rows being inserted. No interference.

- **Refill job crashes midway**: COPY is transactional. If it fails, the entire batch is rolled back. The next hourly run retries. No partial state.

- **DB disk space pressure**: 100M rows at ~200 bytes each = ~20GB. Monitor disk usage. If disk is >80% full, skip refill and alert. The existing pool should last days at normal consumption rates.

- **Clock skew between app servers and DB**: The `expiry_time < NOW()` check in the SKIP LOCKED query uses the DB server's clock, not the app server's clock. This ensures consistent behavior across all app servers.

---

## 5. URL Expiry / Cleanup

### Two Complementary Mechanisms

URL expiry is handled by two independent mechanisms working together. Neither requires deleting rows from the database — expired rows are simply recycled when a new URL claims them via `FOR UPDATE SKIP LOCKED`.

### Mechanism 1: Lazy Cleanup (On Read)

When a user tries to access an expired short URL, the system detects the expiry at read time and returns an appropriate error.

```
Client              App Server               Redis                PostgreSQL (Replica)
  |                      |                     |                        |
  |  GET /old7url3        |                     |                        |
  |─────────────────────>|                     |                        |
  |                      |                     |                        |
  |                      | GET url:old7url3    |                        |
  |                      |────────────────────>|                        |
  |                      |                     |                        |
  |                      | Case A: HIT but     |                        |
  |                      | TTL already expired  |                        |
  |                      | (Redis auto-evicted) |                        |
  |                      | -> MISS (nil)        |                        |
  |                      |<────────────────────|                        |
  |                      |                     |                        |
  |                      | Query DB             |                        |
  |                      |────────────────────────────────────────────>|
  |                      |                     |                        |
  |                      | Row found:           |                        |
  |                      |  long_url = "..."    |                        |
  |                      |  expiry_time =       |                        |
  |                      |   2025-06-15         |                        |
  |                      |  (EXPIRED!)          |                        |
  |                      |<────────────────────────────────────────────|
  |                      |                     |                        |
  |                      | expiry_time < NOW()  |                        |
  |                      | -> URL is expired    |                        |
  |                      |                     |                        |
  |                      | DEL url:old7url3    |                        |
  |                      | (async, in case     |                        |
  |                      |  stale entry exists) |                        |
  |                      |────────────────────>|                        |
  |                      |                     |                        |
  |  410 Gone             |                     |                        |
  |  {"error":            |                     |                        |
  |   "URL_EXPIRED"}     |                     |                        |
  |<─────────────────────|                     |                        |
  |                      |                     |                        |
```

**Key insight**: The expired row is NOT deleted from the database. It remains in the `url_mappings` table with its `expiry_time` in the past. The `FOR UPDATE SKIP LOCKED` query in Flow 1 will eventually claim this row for a new URL, overwriting the `long_url`, `user_id`, `created_at`, and `expiry_time` fields. This is the recycling mechanism.

### Mechanism 2: Periodic Cache Cleanup (Background Job)

A background job periodically scans Redis and removes cache entries for URLs that are known to be expired. This prevents stale entries from consuming Redis memory.

```
Scheduler (cron)    Cache Cleanup Svc         Redis                PostgreSQL (Replica)
      |                   |                     |                        |
      | Every 6 hours     |                     |                        |
      |──────────────────>|                     |                        |
      |                   |                     |                        |
      |                   | 1. SCAN cursor 0    |                        |
      |                   |    MATCH url:*      |                        |
      |                   |    COUNT 1000       |                        |
      |                   |────────────────────>|                        |
      |                   |                     |                        |
      |                   | 2. Batch of 1000    |                        |
      |                   |    keys returned    |                        |
      |                   |<────────────────────|                        |
      |                   |                     |                        |
      |                   | 3. For each key:    |                        |
      |                   |    TTL url:<suffix>  |                        |
      |                   |    If TTL <= 0 or   |                        |
      |                   |    TTL = -1 (no exp)|                        |
      |                   |    -> DEL key       |                        |
      |                   |────────────────────>|                        |
      |                   |                     |                        |
      |                   | 4. Continue SCAN    |                        |
      |                   |    until cursor = 0 |                        |
      |                   |    (full iteration) |                        |
      |                   |                     |                        |
      |                   | 5. Log: deleted     |                        |
      |                   |    12,345 expired   |                        |
      |                   |    cache entries    |                        |
      |                   |                     |                        |
```

### Why No DB Cleanup Is Needed

```
Traditional approach:                 Our approach:
DELETE expired rows from DB           Keep expired rows in DB
  -> Fragmentation                    -> No fragmentation
  -> Vacuum overhead                  -> No vacuum spikes
  -> INSERT new keys later            -> Rows recycled in-place by SKIP LOCKED
  -> Two operations (DELETE + INSERT) -> One operation (UPDATE via CTE)
```

Expired rows serve as the key pool. The `expiry_time < NOW()` condition in the SKIP LOCKED query identifies them as available. This eliminates the need for a separate cleanup job and a separate key generation process — they are unified.

### Expiry Timeline Example

```
Time T+0:     URL created: suffix="ab3k9x12", long_url="https://...", expiry=T+1year
Time T+0:     Redis SET url:ab3k9x12 with TTL=31536000 (1 year in seconds)

Time T+6mo:   User clicks link -> Redis HIT -> 302 redirect (normal)

Time T+1yr:   Redis TTL expires -> key auto-evicted from Redis
Time T+1yr:   User clicks link -> Redis MISS -> DB query -> expiry_time < NOW()
              -> 410 Gone returned
              -> Row now available for recycling

Time T+1yr+2d: New URL creation -> SKIP LOCKED claims this row
              -> suffix="ab3k9x12" now points to a different long_url
              -> Redis SET with new mapping
```

### Edge Cases

- **User accesses URL on exact expiry second**: The check is `expiry_time < NOW()`, not `<=`. A URL expiring at `2027-01-15 00:00:00` is still valid at that exact second, expired at `00:00:01`. This matches user expectation ("valid for 1 year" means the full final day is included).

- **Race condition: URL recycled while user is being redirected**: Extremely unlikely (requires recycling during the few milliseconds of a redirect). Even if it happens, the user is redirected to the original destination (the old long_url was read before the UPDATE). The next request would see the new mapping.

- **Redis cache has stale entry after recycling**: The Flow 1 write path does a `SET url:<suffix>` with the new long_url, overwriting any stale entry. The only window for stale data is between the DB UPDATE and the Redis SET (a few milliseconds, async). Acceptable.

---

## 6. Failover — Leader DB Down

### Timeline: PostgreSQL Leader Failure and Recovery

When the PostgreSQL leader goes down, reads continue via replicas and Redis cache, but writes (URL creation) are temporarily unavailable until Patroni promotes a follower.

```
Time       Event                                              Impact
----       -----                                              ------
0s         Leader PostgreSQL goes down                        Writes start failing
           (hardware failure, network partition,
            or process crash)

0-5s       Patroni (HA manager) detects missing               Write failures accumulate
           heartbeat from leader. Waits for                   Circuit breaker counting:
           confirmation (avoid false positives).              failure 1... 2... 3... 4... 5

5s         Circuit breaker OPENS after 5 failures             App servers stop attempting
           in 10 seconds. All write requests                  writes. Return 503 immediately.
           immediately return 503.                            No wasted connections.

5-10s      Patroni initiates leader election.                 Reads continue normally:
           Follower #1 (most up-to-date) is                   - Redis cache serves ~80%
           selected for promotion.                            - Other replicas serve ~20%

10-15s     Patroni promotes Follower #1 to Leader.            New leader accepting writes,
           WAL (Write-Ahead Log) is replayed                  but app servers don't know yet.
           to ensure no data loss.

15-25s     DNS/connection string updated.                     App servers discover new leader
           PgBouncer or connection pooler                     via health checks or DNS refresh.
           detects new leader endpoint.

25-30s     App servers reconnect to new leader.               Circuit breaker in HALF-OPEN:
           Circuit breaker allows 1 test write.               tries one write to new leader.
           Write succeeds!

30s        Circuit breaker CLOSES.                            Normal operation resumes.
           All writes resume to new leader.                   Total write downtime: ~30 seconds.
```

### Detailed Sequence During Failover

```
Client           App Server        Circuit Breaker     PostgreSQL Leader    Patroni         Follower #1
  |                 |                    |                   |                 |                |
  | POST /api/v1/  |                    |                   |                 |                |
  | urls            |                    |                   |                 |                |
  |────────────────>|                    |                   |                 |                |
  |                 |───────────────────>| State: CLOSED     |                 |                |
  |                 |                    |───────────────────>| X DEAD          |                |
  |                 |                    | Timeout! fail++   |                 |                |
  |                 |                    |<──── timeout ─────|                 |                |
  |  503 Service    |<───────────────────|                   |                 |                |
  |  Unavailable    |                    |                   |                 |                |
  |<────────────────|                    |                   |                 |                |
  |                 |                    |                   |                 |                |
  |  ... 4 more failures ...            |                   |                 |                |
  |                 |                    |                   |                 |                |
  |                 |                    | fail = 5/5        |                 |                |
  |                 |                    | -> State: OPEN    |                 |                |
  |                 |                    |                   |                 |                |
  | POST /api/v1/  |                    |                   |                 |                |
  | urls            |                    |                   |                 |                |
  |────────────────>|                    |                   |                 |                |
  |                 |───────────────────>| State: OPEN       |                 |                |
  |                 |                    | -> Reject         |                 |                |
  |                 |                    |    immediately    |                 |                |
  |  503 (instant)  |<───────────────────|                   |                 |                |
  |<────────────────|                    |                   |                 |                |
  |                 |                    |                   |                 |                |
  |                 |                    |                   |   Failover      |                |
  |                 |                    |                   |   initiated     |                |
  |                 |                    |                   |                 |───────────────>|
  |                 |                    |                   |                 | PROMOTE to     |
  |                 |                    |                   |                 | Leader         |
  |                 |                    |                   |                 |<───────────────|
  |                 |                    |                   |                 |                |
  |  ... 30 seconds pass ...            |                   |                 |                |
  |                 |                    |                   |                 |                |
  |                 |                    | State: HALF-OPEN  |                 |                |
  |                 |                    | (allow 1 test)    |                 |                |
  |                 |                    |                   |                 |                |
  | POST /api/v1/  |                    |                   |                 |                |
  | urls            |                    |                   |                 |                |
  |────────────────>|                    |                   |                 |                |
  |                 |───────────────────>| Test write ────────────────────────────────────────>|
  |                 |                    |                   |                 |  (new leader)  |
  |                 |                    | Success!          |                 |                |
  |                 |                    |<───────────────────────────────────────────────────|
  |                 |                    | -> State: CLOSED  |                 |                |
  |  201 Created    |<───────────────────|                   |                 |                |
  |<────────────────|                    |                   |                 |                |
  |                 |                    |                   |                 |                |
```

### Circuit Breaker State Machine

```
                         5 write failures in 10s
    +--------+  ─────────────────────────────────>  +--------+
    |        |                                      |        |
    | CLOSED |                                      |  OPEN  |
    |(normal)|  <─────────────────────────────────  |(reject)|
    |        |        test write succeeds           |        |
    +--------+                                      +--------+
        ^                                               |
        |                                               |
        |           +------------+                      |
        |           |            |          30s timer    |
        +-----------| HALF-OPEN  |<─────────────────────+
        success     | (1 test    |
                    |  request)  |
                    |            |──────+
                    +------------+      |
                                        | failure
                                        | -> back to OPEN
                                        | (reset timer)
                                        v
                                    +--------+
                                    |  OPEN  |
                                    +--------+
```

### Circuit Breaker Configuration

| Parameter | Value | Rationale |
|---|---|---|
| Failure threshold | 5 failures in 10s | Detect sustained outage, not a single slow query |
| Open duration | 30 seconds | Enough time for Patroni failover to complete |
| Half-open test count | 1 request | Single probe is sufficient to verify recovery |
| Timeout per write | 3 seconds | Writes should complete in <10ms; 3s means something is wrong |
| Scope | Per app server instance | Each instance manages its own circuit breaker |

### Impact Analysis During Leader Failover

| Service | Impact | Duration |
|---|---|---|
| URL Redirects (reads) | **No impact** — served from Redis cache + read replicas | 0s downtime |
| URL Creation (writes) | **503 errors** — all writes fail | ~10-30s |
| Key Pool Refill | **Skipped** — cron job fails, retries next hour | 1 hour delay |
| Analytics writes | **Buffered** — app servers buffer events locally, flush after recovery | No data loss |

**Acceptability**: A 30-second outage for URL creation (which is not on the critical hot path) is a minor degradation. The critical path — URL redirects serving 3,800 req/sec — is completely unaffected.

---

## 7. Failover — Redis Down

### Fallback: All Reads Hit PostgreSQL Replicas

When Redis becomes unavailable, the system degrades gracefully by routing all cache-miss traffic directly to PostgreSQL read replicas. Latency increases but the service remains fully functional.

```
NORMAL OPERATION (Redis healthy):

Client --> App Server --> Redis GET (HIT) --> 302 redirect
                          ~1ms cache lookup
                          Total: ~5-8ms

DEGRADED OPERATION (Redis down):

Client --> App Server --> Redis GET (TIMEOUT, 50ms) --> Circuit breaker opens
                      --> PostgreSQL Replica SELECT   --> 302 redirect
                          ~5-10ms DB query
                          Total: ~15-25ms (first few requests include Redis timeout)

AFTER CIRCUIT BREAKER OPENS:

Client --> App Server --> [Skip Redis entirely] --> PostgreSQL Replica SELECT --> 302 redirect
                          ~5-10ms DB query
                          Total: ~10-18ms (no Redis timeout penalty)
```

### Detailed Sequence: Redis Failure Detection and Fallback

```
Client           App Server         Circuit Breaker (Redis)     Redis          PostgreSQL (Replica)
  |                 |                       |                     |                  |
  | GET /ab3k9x12   |                       |                     |                  |
  |────────────────>|                       |                     |                  |
  |                 | GET url:ab3k9x12      |                     |                  |
  |                 |──────────────────────>|                     |                  |
  |                 |                       |────────────────────>| X DOWN           |
  |                 |                       |                     |                  |
  |                 |                       | Timeout (50ms)      |                  |
  |                 |                       | fail++ (now 1/3)    |                  |
  |                 |                       |                     |                  |
  |                 | Fallback to DB        |                     |                  |
  |                 |──────────────────────────────────────────────────────────────>|
  |                 |                       |                     |                  |
  |                 | Result: long_url      |                     |                  |
  |                 |<──────────────────────────────────────────────────────────────|
  |                 |                       |                     |                  |
  |  302 Found      |                       |                     |                  |
  |<────────────────|                       |                     |                  |
  |                 |                       |                     |                  |
  |  ... 2 more timeouts ...               |                     |                  |
  |                 |                       |                     |                  |
  |                 |                       | fail = 3/3          |                  |
  |                 |                       | -> State: OPEN      |                  |
  |                 |                       |                     |                  |
  | GET /xk7m2p9q   |                       |                     |                  |
  |────────────────>|                       |                     |                  |
  |                 |──────────────────────>| State: OPEN         |                  |
  |                 |                       | -> SKIP Redis       |                  |
  |                 |                       |                     |                  |
  |                 | Direct to DB          |                     |                  |
  |                 |──────────────────────────────────────────────────────────────>|
  |                 |                       |                     |                  |
  |                 | Result: long_url      |                     |                  |
  |                 |<──────────────────────────────────────────────────────────────|
  |                 |                       |                     |                  |
  |  302 Found      |                       |                     |                  |
  |  (no Redis      |                       |                     |                  |
  |   timeout       |                       |                     |                  |
  |   penalty)      |                       |                     |                  |
  |<────────────────|                       |                     |                  |
  |                 |                       |                     |                  |
  |  ... 15 seconds pass ...               |                     |                  |
  |                 |                       |                     |                  |
  |                 |                       | State: HALF-OPEN    |                  |
  |                 |                       | (allow 1 test)      |                  |
  |                 |                       |                     |                  |
  | GET /mn4b8v2z   |                       |                     |                  |
  |────────────────>|                       |                     |                  |
  |                 |──────────────────────>| Test: GET           |                  |
  |                 |                       |────────────────────>|                  |
  |                 |                       |                     |                  |
  |                 |                       | Case A: Still down  |                  |
  |                 |                       | -> OPEN (reset)     |                  |
  |                 |                       |                     |                  |
  |                 |                       | Case B: Recovered!  |                  |
  |                 |                       | -> CLOSED           |                  |
  |                 |                       |                     |                  |
```

### Redis Circuit Breaker Configuration

| Parameter | Value | Rationale |
|---|---|---|
| Failure threshold | 3 consecutive timeouts | Redis is fast; 3 timeouts confirms it's down, not just slow |
| Timeout per request | 50ms | Normal Redis response is <2ms; 50ms is generous |
| Open duration | 15 seconds | Redis recovery is usually fast (restart/failover) |
| Half-open test | 1 GET request | Single probe sufficient |
| Scope | Per app server instance | Local circuit breakers |

### Impact Analysis: Redis Down

```
METRIC                  NORMAL (Redis up)          DEGRADED (Redis down)
------                  -----------------          ---------------------
Redirect latency        ~5-8ms (80% cache hit)     ~10-18ms (all DB reads)
                        ~15-25ms (20% cache miss)

DB read load            ~760 reads/sec             ~3,800 reads/sec
                        (20% of 3,800)             (100% — all reads go to DB)

DB replica capacity     3 replicas * 5K/sec        3 replicas * 5K/sec
                        = 15K total capacity        = 15K total capacity
                        Utilization: 5%             Utilization: 25%

URL creation            Unaffected                 Unaffected
                        (writes go to leader)       (writes go to leader)

User experience         <10ms redirects            ~15ms redirects
                                                   (barely noticeable)
```

**Capacity headroom**: With 3 read replicas each handling 5,000 reads/sec (15K total), the system can absorb the full 3,800 reads/sec without Redis. Even at peak load (15,000 reads/sec), the replicas can handle it. Redis is an optimization, not a dependency.

### Cache Re-Warming After Recovery

When Redis comes back online, the cache is empty. It re-warms naturally:

```
Time T+0:    Redis recovered. Circuit breaker closes.
             Cache is cold — all requests go to DB.

Time T+5m:   ~1.14M redirects have occurred (3,800/sec * 300s).
             Each cache miss populated Redis.
             Hot URLs (accessed multiple times) are now cached.
             Cache hit ratio climbing: ~30%

Time T+30m:  ~6.84M redirects. Most popular URLs are cached.
             Cache hit ratio: ~60%

Time T+2h:   Cache hit ratio back to steady state: ~80%
             Normal operation fully restored.
```

No manual intervention needed. The cache-aside pattern ensures Redis is populated organically by read traffic.

### Edge Cases

- **Redis is slow but not down** (responding in 20-30ms instead of <2ms): The circuit breaker won't open (responses are within the 50ms timeout). Latency is degraded but the system functions. Monitor p99 latency; if Redis is consistently slow, consider manual failover to a Redis replica or restart.

- **Redis partially available** (some commands work, some timeout — e.g., memory pressure causing evictions): Individual GET commands may succeed or fail. The circuit breaker may flap between OPEN and HALF-OPEN. Mitigation: use a sliding window (not consecutive failures) and require a sustained failure rate (>50% of requests failing in a 10-second window) to open the circuit.

- **Redis comes back with stale data** (e.g., restored from an old snapshot): Stale entries may map suffixes to old long_urls. This is handled by Redis TTLs — entries that should have expired will have expired. For entries within their TTL, the data is still valid (URL mappings are immutable during their lifetime). No risk of serving incorrect redirects.

- **Writes during Redis outage**: URL creation (Flow 1) still writes to Redis asynchronously (step 6). If Redis is down, the async SET silently fails. The URL is still created successfully in PostgreSQL. The cache entry will be populated on the first read (cache-miss path).

---

## 8. Multi-Region Redirect

### Architecture Overview

```
                         Global DNS (Route 53)
                      Latency-based routing
                    /           |           \
                   /            |            \
              US User       EU User       AP User
                 |              |              |
                 v              v              v
            US-EAST         EU-WEST        AP-EAST
         +-----------+   +-----------+   +-----------+
         | App + LB  |   | App + LB  |   | App + LB  |
         | Redis     |   | Redis     |   | Redis     |
         | DB Leader |   | DB Follower|   | DB Follower|
         +-----------+   +-----------+   +-----------+
               |                |              |
               |   async repl  |              |
               +──────────────>+              |
               |   (~100-500ms)|              |
               +──────────────────────────────+
               |   async repl  (~200-800ms)   |
```

**Key design decisions**:
- **Reads are local**: Each region serves redirects from its own Redis cache and DB follower.
- **Writes are centralized**: All URL creations go to US-EAST (the leader region) to avoid multi-leader conflict resolution complexity.
- **Replication is async**: PostgreSQL streams WAL records to followers with ~100-500ms lag (US-EU) and ~200-800ms lag (US-AP).

### Scenario A: EU User Accesses URL Created by US User (Normal Case)

The URL was created hours or days ago. Replication has long since completed. The EU region has the data.

```
EU User             Route 53         EU-WEST App        EU Redis         EU DB Follower
  |                    |                 |                  |                  |
  | GET tinyurl.com    |                 |                  |                  |
  |   /ab3k9x12       |                 |                  |                  |
  |───────────────────>|                 |                  |                  |
  |                    |                 |                  |                  |
  |                    | Latency-based   |                  |                  |
  |                    | routing: EU     |                  |                  |
  |                    | user -> EU-WEST |                  |                  |
  |                    |                 |                  |                  |
  | (DNS resolves to   |                 |                  |                  |
  |  EU-WEST LB IP)   |                 |                  |                  |
  |                    |                 |                  |                  |
  |  GET /ab3k9x12     |                 |                  |                  |
  |────────────────────────────────────>|                  |                  |
  |                    |                 |                  |                  |
  |                    |                 | GET url:ab3k9x12 |                  |
  |                    |                 |─────────────────>|                  |
  |                    |                 |                  |                  |
  |                    |                 | Case 1: HIT      |                  |
  |                    |                 | (popular URL,    |                  |
  |                    |                 |  previously       |                  |
  |                    |                 |  accessed in EU)  |                  |
  |                    |                 |<─────────────────|                  |
  |                    |                 |                  |                  |
  |  302 Found         |                 |                  |                  |
  |  Location: ...     |                 |                  |                  |
  |<────────────────────────────────────|                  |                  |
  |                    |                 |                  |                  |
  |                    |                 | Case 2: MISS     |                  |
  |                    |                 | (first EU access)|                  |
  |                    |                 |<─────────────────|                  |
  |                    |                 |                  |                  |
  |                    |                 | SELECT ...       |                  |
  |                    |                 | WHERE suffix =   |                  |
  |                    |                 |   'ab3k9x12'    |                  |
  |                    |                 |─────────────────────────────────── >|
  |                    |                 |                  |                  |
  |                    |                 | Found! (repl     |                  |
  |                    |                 | lag is 0 — URL   |                  |
  |                    |                 | created hours    |                  |
  |                    |                 | ago)             |                  |
  |                    |                 |<───────────────────────────────────|
  |                    |                 |                  |                  |
  |                    |                 | SET cache (async)|                  |
  |                    |                 |─────────────────>|                  |
  |                    |                 |                  |                  |
  |  302 Found         |                 |                  |                  |
  |<────────────────────────────────────|                  |                  |
  |                    |                 |                  |                  |
```

**Latency**: ~30-50ms (EU user to EU-WEST region), comparable to single-region performance. Cross-region latency avoided entirely.

### Scenario B: EU User Accesses URL JUST Created by US User (Replication Lag)

This is the edge case where a URL is accessed from a different region within milliseconds of creation. The async replication hasn't delivered the data to the EU follower yet.

```
US User             US-EAST App        US DB Leader      EU DB Follower      EU-WEST App         EU User
  |                    |                  |                   |                   |                  |
  | POST /api/v1/urls  |                  |                   |                   |                  |
  |───────────────────>|                  |                   |                   |                  |
  |                    |                  |                   |                   |                  |
  |                    | INSERT/UPDATE    |                   |                   |                  |
  |                    |─────────────────>|                   |                   |                  |
  |                    |                  |                   |                   |                  |
  |  201 Created       |                  |                   |                   |                  |
  |  short: /nw8p3q7r  |                  |                   |                   |                  |
  |<───────────────────|                  |                   |                   |                  |
  |                    |                  |                   |                   |                  |
  |  (US user shares   |                  |                   |                   |                  |
  |   link with EU     |                  | Async replication |                   |                  |
  |   colleague via    |                  | in progress...    |                   |                  |
  |   chat)            |                  | (~100-500ms lag)  |                   |                  |
  |                    |                  |                   |                   |                  |
  |                    |                  |                   |                   | GET /nw8p3q7r     |
  |                    |                  |                   |                   |<─────────────────|
  |                    |                  |                   |                   |                  |
  |                    |                  |                   |                   | Redis MISS        |
  |                    |                  |                   |                   |                  |
  |                    |                  |                   |                   | SELECT ... WHERE  |
  |                    |                  |                   |                   | suffix='nw8p3q7r'|
  |                    |                  |                   |                   |──────────────── >|
  |                    |                  |                   |                   |                  |
  |                    |                  |                   |  NOT FOUND!       |                  |
  |                    |                  |                   |  (replication     |                  |
  |                    |                  |                   |   hasn't arrived) |                  |
  |                    |                  |                   |──────────────────>|                  |
  |                    |                  |                   |                   |                  |
  |                    |                  |                   |                   |  404 Not Found   |
  |                    |                  |                   |                   |─────────────────>|
  |                    |                  |                   |                   |                  |
  |                    |                  |                   |                   |  (EU user waits  |
  |                    |                  |                   |                   |   2s, retries)   |
  |                    |                  |                   |                   |                  |
  |                    |                  |  Replication      |                   |                  |
  |                    |                  |  completes!       |                   |                  |
  |                    |                  |─────────────────>|                   |                  |
  |                    |                  |                   |                   |                  |
  |                    |                  |                   |                   | GET /nw8p3q7r    |
  |                    |                  |                   |                   |<─────────────────|
  |                    |                  |                   |                   |                  |
  |                    |                  |                   |                   | Redis MISS ->    |
  |                    |                  |                   |                   | DB query ->      |
  |                    |                  |                   |                   | FOUND!           |
  |                    |                  |                   |                   |                  |
  |                    |                  |                   |                   |  302 Found       |
  |                    |                  |                   |                   |─────────────────>|
  |                    |                  |                   |                   |                  |
```

### Handling the Replication Lag Edge Case

Three possible strategies, in order of simplicity:

| Strategy | How It Works | Latency | Complexity | Recommendation |
|---|---|---|---|---|
| **(a) Return 404, let user retry** | EU follower doesn't have the row yet. Return 404. User retries in 1-2 seconds; by then, replication has caught up. | 0ms extra | None | **Recommended** |
| **(b) Fall back to US-EAST leader** | On 404, EU app server re-queries the US-EAST leader DB across regions. Always returns the most up-to-date data. | +80-120ms (cross-region RTT) | Low | Acceptable for critical use cases |
| **(c) Return 404 with Retry-After header** | Return `404` with `Retry-After: 2` header. Well-behaved clients retry automatically. | +2s (retry delay) | None | Good for API clients |

**Recommendation: Strategy (a)** for the following reasons:
1. This edge case is extremely rare — it requires the URL to be accessed within ~500ms of creation from a different continent.
2. The creator just received the short URL and is sharing it (e.g., pasting into a chat). The recipient likely won't click it for several seconds or minutes.
3. Even if clicked immediately, a brief 404 followed by a successful retry is acceptable. URL shorteners are not mission-critical real-time systems.
4. Adding cross-region fallback (strategy b) adds complexity, cross-region DB connections, and potential cascading failures if the leader region is under load.

### Write Routing: All Writes Go to US-EAST

```
EU User Creates a Short URL:

EU User         EU-WEST LB          US-EAST App Server       US DB Leader
  |                 |                       |                      |
  | POST /api/v1/  |                       |                      |
  | urls            |                       |                      |
  |────────────────>|                       |                      |
  |                 |                       |                      |
  |                 | Route write to        |                      |
  |                 | US-EAST (leader       |                      |
  |                 | region)               |                      |
  |                 |──────────────────────>|                      |
  |                 |                       |                      |
  |                 |                       | FOR UPDATE           |
  |                 |                       | SKIP LOCKED          |
  |                 |                       |─────────────────────>|
  |                 |                       |                      |
  |                 |                       | Row claimed          |
  |                 |                       |<─────────────────────|
  |                 |                       |                      |
  |  201 Created    |                       |                      |
  |  (+ cross-region|                       |                      |
  |   latency)      |                       |                      |
  |<────────────────|<──────────────────────|                      |
  |                 |                       |                      |

Total latency for EU user creating a URL:
  ~150ms (including ~80ms cross-region RTT to US-EAST)
  vs ~10ms for a US user

This is acceptable:
  - URL creation is infrequent (230/sec globally, user does it once)
  - 150ms is imperceptible for a form submission
  - Avoids multi-leader complexity entirely
```

### Alternative: Active-Active with Partitioned Key Space

For systems requiring lower write latency globally, an active-active architecture partitions the key space by region:

```
Region       Key Prefix Range     Leader DB         Key Pool
------       ----------------     ---------         --------
US-EAST      a-l (first 12)       US Leader         ~1.4 trillion keys
EU-WEST      m-x (next 12)        EU Leader         ~1.4 trillion keys
AP-EAST      y-9, 0-9 (last 12)   AP Leader         ~1.4 trillion keys

Each region writes to its own leader. No cross-region writes needed.
Each region's suffixes start with a different character range -> no conflicts.
```

| | Single-Leader (current) | Active-Active (alternative) |
|---|---|---|
| Write latency (non-local) | ~150ms (cross-region) | ~10ms (local leader) |
| Complexity | Low | High (3 leaders, conflict-free partitioning) |
| Failover complexity | Patroni promotes 1 follower | Each region has its own failover chain |
| Data consistency | Strong (single leader) | Partition-level strong, global eventual |
| Recommendation | **Use this for <1K writes/sec** | Consider for >10K writes/sec globally |

### Multi-Region Latency Comparison

| Scenario | Path | Latency |
|---|---|---|
| US user, URL cached in US Redis | US App -> US Redis (HIT) -> 302 | ~5-8ms |
| EU user, URL cached in EU Redis | EU App -> EU Redis (HIT) -> 302 | ~5-8ms |
| EU user, URL NOT in EU Redis or DB | EU App -> EU Redis (MISS) -> EU DB (MISS) -> 404 | ~15ms |
| EU user, URL in EU DB not Redis | EU App -> EU Redis (MISS) -> EU DB (HIT) -> 302 | ~15-25ms |
| EU user creates URL (routes to US) | EU LB -> US App -> US DB -> 201 | ~150ms |
| AP user, URL cached in AP Redis | AP App -> AP Redis (HIT) -> 302 | ~5-8ms |
| AP user creates URL (routes to US) | AP LB -> US App -> US DB -> 201 | ~200ms |

---

## Flow Summary Table

| # | Flow | Trigger | Sync/Async | Hot Path? | Typical Latency | Frequency |
|---|---|---|---|---|---|---|
| 1 | URL Creation | User POST | Sync | No (230/sec avg) | ~7-10ms | ~600M/month |
| 2 | Redirect (cache hit) | User GET | Sync | **Yes** | ~5-8ms | ~80% of reads |
| 3 | Redirect (cache miss) | User GET | Sync | **Yes** | ~15-25ms | ~20% of reads |
| 4 | Key Pool Refill | Cron / threshold | Async (background) | No | ~15-20 min | Hourly check |
| 5 | URL Expiry Cleanup | On-read + cron | Lazy + async | No | N/A | Continuous |
| 6 | Leader DB Failover | DB failure | Async (operational) | Emergency | 10-30s downtime | Rare |
| 7 | Redis Failover | Redis failure | Sync (fallback) | Emergency | +10ms per request | Rare |
| 8 | Multi-Region Redirect | User GET (cross-region) | Sync | Yes | ~30-50ms (local) | ~30% of reads |

### Traffic Distribution Across Flows

```
Total redirect traffic: 3,800 req/sec (avg), 15,000 req/sec (peak)

                          3,800 req/sec
                               |
                    +----------+-----------+
                    |                      |
              Same region              Cross-region
              ~70% (2,660/s)           ~30% (1,140/s)
                    |                      |
              +-----+-----+         +-----+-----+
              |           |         |           |
          Cache HIT   Cache MISS  Cache HIT  Cache MISS
          80% (2,128) 20% (532)   80% (912)  20% (228)
              |           |         |           |
          Flow 2      Flow 3    Flow 8+2    Flow 8+3
          ~5ms        ~20ms      ~35ms       ~50ms


Write traffic: 230 req/sec (avg), 1,000 req/sec (peak)
  -> 100% routed to US-EAST leader
  -> All follow Flow 1
  -> ~7-10ms per request
```

### Monitoring: Key Metrics to Track

| Metric | Normal | Warning | Critical |
|---|---|---|---|
| Redis cache hit ratio | >80% | <70% | <50% |
| Redirect p99 latency | <50ms | <100ms | >200ms |
| URL creation p99 latency | <20ms | <50ms | >100ms |
| Key pool available | >1B | <1B | <100M |
| DB replication lag | <500ms | <2s | >5s |
| Circuit breaker (DB) state | CLOSED | HALF-OPEN | OPEN |
| Circuit breaker (Redis) state | CLOSED | HALF-OPEN | OPEN |
| Error rate (5xx) | <0.01% | <0.1% | >1% |

---

*This document complements the [Interview Simulation](interview-simulation.md). For API specifications, see [API Contracts](api-contracts.md). For caching layer details, see [Scaling & Caching](scaling-and-caching.md). For failover database decisions, see [SQL vs NoSQL Tradeoffs](sql-vs-nosql-tradeoffs.md).*
