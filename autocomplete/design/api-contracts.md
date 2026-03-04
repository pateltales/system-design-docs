# Autocomplete System — API Contracts

> Continuation of the interview simulation. The interviewer asked: "Walk me through the API contracts for your autocomplete service."

---

## Base URL & Conventions

```
Base URL: https://suggest.amazon.com/v1
Content-Type: application/json
Authorization: API Key (external) or mTLS (internal service-to-service)
```

**Common conventions:**
- All endpoints require authentication (API key for external clients, mTLS for internal services).
- All responses include an `X-Request-Id` header for distributed tracing.
- All responses include an `X-Served-From` header indicating the cache layer that served the request.
- Timestamps are Unix epoch milliseconds.
- Prefixes are UTF-8 strings, max 100 characters.
- Rate limiting is per-user (authenticated) and per-IP (unauthenticated).

**Standard Response Headers:**

```
X-Request-Id: req_abc123def456
X-Served-From: cdn | redis | trie
X-Trie-Version: v2026020814
X-Latency-Ms: 3
X-Rate-Limit-Remaining: 95
```

---

## Interview-Focused APIs (Core — discussed in the interview)

### 1. GET /v1/suggestions — Autocomplete Suggestions

The primary API. Returns top-K query suggestions for a given prefix.

```
GET /v1/suggestions?prefix=amaz&limit=10&language=en
```

**Request Parameters:**

| Parameter | Type | Required | Default | Description |
|---|---|---|---|---|
| `prefix` | string | Yes | — | The search prefix (min 1 char, max 100 chars, URL-encoded) |
| `limit` | int | No | 10 | Number of suggestions to return (max 25) |
| `language` | string | No | `en` | ISO 639-1 language code |
| `category` | string | No | `all` | Filter suggestions by product category (e.g., `electronics`, `books`) |

**Request Headers:**

| Header | Required | Description |
|---|---|---|
| `Authorization` | Yes | API key or mTLS certificate |
| `X-User-Id` | No | Hashed user ID for personalization. If absent, returns global (non-personalized) results. |
| `X-Session-Id` | No | Session tracking for analytics. |
| `X-Device-Type` | No | `MOBILE` / `DESKTOP` / `TABLET`. May influence result formatting. |
| `X-Region` | No | Geographic region for regional relevance. Auto-detected from IP if absent. |

**Response — 200 OK:**

```json
{
    "prefix": "amaz",
    "suggestions": [
        {
            "text": "amazon prime",
            "score": 0.95,
            "category": "general",
            "trending": false,
            "metadata": {
                "estimated_results": 150000,
                "department": "Prime"
            }
        },
        {
            "text": "amazon kindle",
            "score": 0.87,
            "category": "electronics",
            "trending": false,
            "metadata": {
                "estimated_results": 85000,
                "department": "Electronics"
            }
        },
        {
            "text": "amazon prime day deals",
            "score": 0.82,
            "category": "deals",
            "trending": true,
            "metadata": {
                "estimated_results": 45000,
                "department": "Deals"
            }
        },
        {
            "text": "amazon fresh",
            "score": 0.78,
            "category": "grocery",
            "trending": false,
            "metadata": {
                "estimated_results": 32000,
                "department": "Grocery"
            }
        },
        {
            "text": "amazon music",
            "score": 0.75,
            "category": "digital",
            "trending": false,
            "metadata": {
                "estimated_results": 28000,
                "department": "Digital Music"
            }
        }
    ],
    "count": 5,
    "trie_version": "v2026020814",
    "served_from": "trie",
    "experiment_id": "exp_ranking_v3_20260208",
    "personalized": false,
    "latency_ms": 3
}
```

**Response Headers:**

```
HTTP/1.1 200 OK
Content-Type: application/json
Cache-Control: max-age=300, public
X-Request-Id: req_abc123def456
X-Served-From: trie
X-Trie-Version: v2026020814
X-Latency-Ms: 3
X-Rate-Limit-Remaining: 95
Vary: Accept-Language
```

**What happens server-side:**

```
1. API Gateway validates API key, checks rate limit (token bucket)
2. If X-User-Id present → route to origin (bypass CDN)
   If absent → route through CDN
3. CDN cache check: key = prefix + language
   → HIT: return cached response (TTL: 5 min)
   → MISS: forward to origin
4. Autocomplete Service receives request
5. Redis cache check: key = suggestions:{trie_version}:{prefix}
   → HIT: return cached response
   → MISS: continue to trie server
6. Trie Server: traverse compressed trie, return precomputed top-K
7. Content filter: check results against Redis blocklist
8. Personalization (if X-User-Id present):
   a. Fetch user profile from Redis
   b. Re-rank results based on user history
9. Trending merge: check Redis trending store, inject matching queries
10. Cache result in Redis (async, TTL: 10 min)
11. Return response
```

**Error Responses:**

| Status | Code | Description |
|---|---|---|
| 400 | `INVALID_PREFIX` | Prefix is empty, too long (>100 chars), or contains only whitespace |
| 400 | `INVALID_LANGUAGE` | Unsupported language code |
| 400 | `INVALID_LIMIT` | Limit is not a positive integer or exceeds 25 |
| 401 | `UNAUTHORIZED` | Missing or invalid API key |
| 429 | `RATE_LIMITED` | Client exceeded rate limit (100 req/sec per user, 1000 req/sec per IP) |
| 503 | `SERVICE_UNAVAILABLE` | Autocomplete service is down (all fallbacks exhausted) |
| 504 | `TIMEOUT` | Request timed out (> 100ms) |

**Error Response Format:**

```json
{
    "error": {
        "code": "INVALID_PREFIX",
        "message": "Prefix must be between 1 and 100 characters",
        "request_id": "req_abc123def456"
    }
}
```

**Rate Limiting:**

| Scope | Limit | Window | Response |
|---|---|---|---|
| Per authenticated user | 100 req/sec | Sliding window | 429 with `Retry-After` header |
| Per IP (unauthenticated) | 1000 req/sec | Sliding window | 429 with `Retry-After` header |
| Global | 5M req/sec | N/A | 503 (system overload) |

---

## Extended APIs

### 2. POST /v1/suggestions/batch — Batch Prefix Lookup

Look up suggestions for multiple prefixes in a single request. Useful for predictive prefetching (e.g., prefetching results for "a", "am", "ama", "amaz" in one call when the user starts typing).

```
POST /v1/suggestions/batch
```

**Request Body:**

```json
{
    "prefixes": ["a", "am", "ama", "amaz"],
    "limit": 10,
    "language": "en"
}
```

**Response — 200 OK:**

```json
{
    "results": {
        "a": {
            "suggestions": [
                {"text": "amazon prime", "score": 0.95, "trending": false},
                {"text": "apple watch", "score": 0.88, "trending": false},
                ...
            ],
            "count": 10
        },
        "am": {
            "suggestions": [
                {"text": "amazon prime", "score": 0.95, "trending": false},
                {"text": "amazon kindle", "score": 0.87, "trending": false},
                ...
            ],
            "count": 10
        },
        "ama": { ... },
        "amaz": { ... }
    },
    "trie_version": "v2026020814",
    "latency_ms": 8
}
```

**Constraints:**
- Maximum 10 prefixes per batch request
- Same rate limiting applies (each prefix counts as 1 request toward the limit)

**Use case:** Client-side prefetching. When the user focuses the search box, prefetch common starting prefixes to make the first few keystrokes feel instant.

---

### 3. GET /v1/trending — Trending Queries

Returns currently trending queries. No prefix filter — returns the global trending list.

```
GET /v1/trending?limit=20&language=en
```

**Response — 200 OK:**

```json
{
    "trending": [
        {
            "query": "super bowl 2026",
            "trending_score": 10.0,
            "spike_ratio": 75.0,
            "category": "sports",
            "first_detected": 1707350400000
        },
        {
            "query": "prime day deals",
            "trending_score": 8.5,
            "spike_ratio": 50.0,
            "category": "deals",
            "first_detected": 1707346800000
        },
        ...
    ],
    "count": 20,
    "as_of": 1707350400000
}
```

**Use case:** Homepage "trending searches" widget, social sharing of popular queries.

**Cache-Control:** `max-age=60` (trending changes quickly, shorter TTL than suggestions).

---

### 4. POST /v1/feedback — Suggestion Click Feedback

Logs when a user clicks on an autocomplete suggestion. Used for:
- Measuring suggestion quality (click-through rate)
- A/B testing different ranking models
- Improving future rankings

```
POST /v1/feedback
```

**Request Body:**

```json
{
    "prefix": "amaz",
    "selected_suggestion": "amazon prime",
    "position": 1,
    "suggestions_shown": [
        "amazon prime",
        "amazon kindle",
        "amazon prime day deals",
        "amazon fresh",
        "amazon music"
    ],
    "session_id": "sess_abc123",
    "trie_version": "v2026020814",
    "experiment_id": "exp_ranking_v3_20260208",
    "timestamp": 1707350400000
}
```

**Response — 202 Accepted:**

```json
{
    "status": "accepted",
    "request_id": "req_xyz789"
}
```

**Processing:** Fire-and-forget. The feedback is written to Kafka (`suggestion-feedback` topic) and processed asynchronously by the analytics pipeline. Never blocks the user.

---

## Admin APIs (Internal Only)

These APIs are authenticated via mTLS and accessible only from internal networks.

### 5. POST /v1/admin/blocklist — Manage Content Blocklist

Add or remove terms from the content blocklist. Changes take effect immediately at the serve-time filter level.

```
POST /v1/admin/blocklist
```

**Request Body:**

```json
{
    "action": "ADD",
    "terms": ["offensive_term_1", "offensive_term_2"],
    "reason": "Policy violation — reported by Trust & Safety team",
    "requested_by": "admin@amazon.com",
    "ticket_id": "TT-12345"
}
```

**Response — 200 OK:**

```json
{
    "status": "applied",
    "terms_added": 2,
    "total_blocklist_size": 502345,
    "effective_at": 1707350400000,
    "note": "Terms will be excluded from next trie build (hourly). Serve-time filter is active immediately."
}
```

**Actions:**

| Action | Description |
|---|---|
| `ADD` | Add terms to the blocklist. Immediate serve-time effect. |
| `REMOVE` | Remove terms from the blocklist. Terms become available in next trie build. |
| `LIST` | Return the full blocklist (paginated). |

---

### 6. POST /v1/admin/trie/rebuild — Force Trie Rebuild

Trigger an immediate trie rebuild outside the normal hourly schedule. Used for emergency updates (e.g., after a large blocklist change).

```
POST /v1/admin/trie/rebuild
```

**Request Body:**

```json
{
    "reason": "Emergency blocklist update after incident TT-12345",
    "requested_by": "admin@amazon.com",
    "priority": "HIGH"
}
```

**Response — 202 Accepted:**

```json
{
    "status": "accepted",
    "build_id": "build_20260208_143022",
    "estimated_completion": 1707352800000,
    "track_at": "/v1/admin/trie/status?build_id=build_20260208_143022"
}
```

**Processing:** The rebuild is asynchronous. Use the status endpoint to track progress.

---

### 7. GET /v1/admin/trie/status — Trie Health & Status

Returns the current state of the trie infrastructure.

```
GET /v1/admin/trie/status
```

**Response — 200 OK:**

```json
{
    "current_version": "v2026020814",
    "previous_versions": ["v2026020813", "v2026020812", "v2026020811", "v2026020810"],
    "build": {
        "last_successful": {
            "version": "v2026020814",
            "started_at": 1707350400000,
            "completed_at": 1707352800000,
            "duration_seconds": 2400,
            "query_count": 487234567,
            "trie_size_bytes": 7516192768
        },
        "in_progress": null,
        "next_scheduled": 1707356400000
    },
    "servers": {
        "total": 9,
        "healthy": 9,
        "by_az": {
            "us-east-1a": {"total": 3, "healthy": 3, "version": "v2026020814"},
            "us-east-1b": {"total": 3, "healthy": 3, "version": "v2026020814"},
            "us-east-1c": {"total": 3, "healthy": 3, "version": "v2026020814"}
        }
    },
    "cache": {
        "redis_hit_rate": 0.82,
        "cdn_hit_rate": 0.61,
        "redis_keys_count": 487234,
        "redis_memory_used_mb": 512
    },
    "trending": {
        "active_trending_queries": 2341,
        "flink_consumer_lag": 1234,
        "last_window_end": 1707350100000
    },
    "content_filter": {
        "blocklist_size": 502345,
        "last_updated": 1707350400000,
        "ml_classifier_version": "v3.2.1"
    }
}
```

---

### 8. GET /v1/admin/analytics — Query Analytics

Returns analytics on autocomplete performance and usage.

```
GET /v1/admin/analytics?from=1707264000000&to=1707350400000&granularity=hour
```

**Request Parameters:**

| Parameter | Type | Required | Default | Description |
|---|---|---|---|---|
| `from` | long | Yes | — | Start timestamp (Unix millis) |
| `to` | long | Yes | — | End timestamp (Unix millis) |
| `granularity` | string | No | `hour` | `minute`, `hour`, or `day` |

**Response — 200 OK:**

```json
{
    "period": {
        "from": 1707264000000,
        "to": 1707350400000
    },
    "summary": {
        "total_requests": 50000000000,
        "unique_prefixes_queried": 12000000,
        "suggestion_ctr": 0.42,
        "avg_latency_ms": 8.3,
        "p99_latency_ms": 45,
        "empty_result_rate": 0.08,
        "cache_hit_rate_overall": 0.944
    },
    "top_queries": [
        {"prefix": "a", "count": 1200000000, "ctr": 0.55},
        {"prefix": "i", "count": 800000000, "ctr": 0.48},
        {"prefix": "s", "count": 750000000, "ctr": 0.44},
        ...
    ],
    "time_series": [
        {
            "timestamp": 1707264000000,
            "requests": 2000000000,
            "ctr": 0.41,
            "p99_latency_ms": 42,
            "cache_hit_rate": 0.945
        },
        ...
    ],
    "experiments": [
        {
            "id": "exp_ranking_v3_20260208",
            "variant": "treatment",
            "traffic_pct": 50,
            "ctr": 0.44,
            "ctr_delta_vs_control": "+4.8%",
            "significance": 0.98
        }
    ]
}
```

---

## API Summary

### Core APIs (Interview Focus)

| # | Method | Endpoint | Description | Cacheable | Auth |
|---|---|---|---|---|---|
| 1 | GET | `/v1/suggestions` | Autocomplete suggestions for a prefix | ✅ CDN + Redis | API Key |

### Extended APIs

| # | Method | Endpoint | Description | Cacheable | Auth |
|---|---|---|---|---|---|
| 2 | POST | `/v1/suggestions/batch` | Batch prefix lookup | ❌ (POST) | API Key |
| 3 | GET | `/v1/trending` | Currently trending queries | ✅ Short TTL (60s) | API Key |
| 4 | POST | `/v1/feedback` | Log suggestion click | ❌ (fire-and-forget) | API Key |

### Admin APIs (Internal)

| # | Method | Endpoint | Description | Auth |
|---|---|---|---|---|
| 5 | POST | `/v1/admin/blocklist` | Manage content blocklist | mTLS |
| 6 | POST | `/v1/admin/trie/rebuild` | Force trie rebuild | mTLS |
| 7 | GET | `/v1/admin/trie/status` | Trie infrastructure status | mTLS |
| 8 | GET | `/v1/admin/analytics` | Query performance analytics | mTLS |

---

## Design Decisions

### Why REST Over gRPC?

| Factor | REST | gRPC |
|---|---|---|
| Browser compatibility | ✅ Native (fetch/XHR) | ❌ Requires gRPC-Web proxy |
| CDN cacheability | ✅ GET requests are naturally cacheable | ❌ POST-based, not cacheable by CDN |
| Tooling/debugging | ✅ curl, browser DevTools | ⚠️ Needs grpcurl, protobuf |
| Performance | ⚠️ JSON overhead (~2x size vs protobuf) | ✅ Binary format, smaller payload |
| Streaming | ❌ Not native (need SSE/WebSocket) | ✅ Native bidirectional streaming |

**Verdict**: REST for the external-facing suggestions API (browser compatibility, CDN caching). gRPC for internal service-to-service communication (autocomplete service ↔ trie server) where performance matters and browser compatibility is irrelevant.

### Why GET for Suggestions (Not POST)?

1. **Cacheability**: GET requests are cached by browsers, CDN, and proxies. POST requests are not. Since autocomplete results change slowly (hourly), caching is critical.
2. **Idempotency**: GET is inherently idempotent — retries are safe. POST requires idempotency keys.
3. **Simplicity**: Query parameters for a simple prefix + limit request are cleaner than a JSON body.
4. **Semantics**: We're retrieving data, not creating/modifying — GET is the correct HTTP verb.

**Exception**: The batch endpoint (POST /v1/suggestions/batch) uses POST because:
- The request body (list of prefixes) can be large
- Batch requests are typically made by internal clients, not browsers
- The batch endpoint is not CDN-cacheable anyway (results are combined)

### Why Include Metadata in the Response?

| Field | Purpose |
|---|---|
| `trie_version` | Debugging: which trie produced these results? Useful for incident investigation. |
| `served_from` | Performance monitoring: are requests hitting the trie or being served from cache? |
| `experiment_id` | A/B testing: attribute CTR to specific ranking experiments. |
| `trending` | UI: the client can visually highlight trending suggestions (e.g., with a flame icon). |
| `score` | Debugging only. Not exposed to end users. Helps engineers verify ranking behavior. |
| `latency_ms` | Performance monitoring: end-to-end server latency. |

### Why Rate Limit Per User AND Per IP?

| Scope | Threat | Limit |
|---|---|---|
| Per user (authenticated) | Misbehaving application, excessive polling, abuse | 100 req/sec |
| Per IP (unauthenticated) | Bot scraping, DDoS, brute-force prefix enumeration | 1000 req/sec |
| Global | System overload protection | 5M req/sec |

Dual-scope rate limiting ensures that:
- A single authenticated user can't monopolize the service
- Unauthenticated traffic (bots, scrapers) can't overwhelm the system
- The system has an absolute upper bound to prevent cascading failures

### Response Size Optimization

For mobile clients on slow connections, response size matters:

| Approach | Size (10 suggestions) | Tradeoff |
|---|---|---|
| Full response (with metadata) | ~1.5 KB | Most informative |
| Compact response (text + score only) | ~500 bytes | Minimal, fast |
| Text-only response (just suggestion strings) | ~200 bytes | Smallest, no metadata |

**Recommendation**: Default to full response. Clients can request compact format via `Accept: application/json; profile="compact"` header:

```json
{
    "suggestions": ["amazon prime", "amazon kindle", "amazon music", ...]
}
```

---

*This document complements the [Interview Simulation](interview-simulation.md) and the [System Flows](flow.md) document.*
