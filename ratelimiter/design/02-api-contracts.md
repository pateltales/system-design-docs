# Rate Limiter — API Contracts & Client-Facing Contracts

> This is the comprehensive API reference for the rate limiting system. The [interview simulation](01-interview-simulation.md) (Phase 3) covers a subset — the hot-path check, response headers, and rule management. This doc covers everything.

---

## Quick Reference: Interview Subset vs Full API

| API Group | Covered in Interview? | Notes |
|---|---|---|
| Rate Limit Check (hot path) | **Yes** | The critical path — atomic check-and-decrement |
| Response Headers & Status Codes | **Yes** | Client contract (429, X-RateLimit-*, Retry-After) |
| Rule Management (CRUD) | **Yes** | Control plane for operators |
| Client Tier / Quota Management | No | Mentioned briefly |
| Override / Exemption APIs | No | Operational flexibility |
| Analytics / Monitoring APIs | No | Powers dashboards |
| Health / Ops APIs | No | Operational endpoints |

---

## 1. Rate Limit Check APIs (Hot Path)

> **This is the most performance-critical path in the entire system.** Called on EVERY incoming API request. Latency must be <1ms at P99 to avoid becoming the bottleneck. The check-and-decrement must be **atomic** — two concurrent requests must not both pass when only one slot remains.

### `POST /ratelimit/check`

**Request:**
```json
{
  "client_id": "user_abc123",
  "resource": "/api/v1/orders",
  "method": "POST",
  "client_tier": "pro",
  "source_ip": "203.0.113.42",
  "cost": 5
}
```

| Field | Type | Required | Description |
|---|---|---|---|
| `client_id` | string | Yes | Unique client identifier (user ID, API key, or session ID) |
| `resource` | string | Yes | The API resource being accessed (URL path or resource group) |
| `method` | string | No | HTTP method (GET, POST, etc.). Default: `*` (any) |
| `client_tier` | string | No | Client's subscription tier (free, pro, enterprise). Resolved from auth if omitted. |
| `source_ip` | string | No | Client's IP address (for IP-based limiting) |
| `cost` | integer | No | Request cost in points (for cost-based limiting). Default: 1 |

**Response (allowed):**
```json
{
  "decision": "ALLOW",
  "matched_rules": [
    {
      "rule_id": "rule-456",
      "dimension": "per_user",
      "limit": 1000,
      "remaining": 742,
      "reset_at": 1672531260
    },
    {
      "rule_id": "rule-789",
      "dimension": "per_endpoint",
      "limit": 100,
      "remaining": 67,
      "reset_at": 1672531260
    }
  ],
  "rate_limit_headers": {
    "X-RateLimit-Limit": 1000,
    "X-RateLimit-Remaining": 742,
    "X-RateLimit-Reset": 1672531260
  }
}
```

**Response (rejected):**
```json
{
  "decision": "REJECT",
  "matched_rules": [
    {
      "rule_id": "rule-456",
      "dimension": "per_user",
      "limit": 1000,
      "remaining": 0,
      "reset_at": 1672531260
    }
  ],
  "rate_limit_headers": {
    "X-RateLimit-Limit": 1000,
    "X-RateLimit-Remaining": 0,
    "X-RateLimit-Reset": 1672531260,
    "Retry-After": 18
  },
  "error": {
    "code": "rate_limit_exceeded",
    "message": "Per-user limit of 1000 req/min exceeded",
    "rule_id": "rule-456",
    "dimension": "per_user"
  }
}
```

### Deployment Models

The `POST /ratelimit/check` endpoint can be deployed in three ways:

| Model | Latency Added | Pros | Cons |
|---|---|---|---|
| **Dedicated service** (gRPC/HTTP) | ~1ms (network hop) | Clean separation, independent scaling | Network hop per request |
| **Embedded middleware** (in-process) | ~10μs (in-memory) | No network hop, lowest latency | Coupled to app language/lifecycle |
| **Sidecar proxy** (Envoy/Istio) | ~0.1ms (localhost hop) | No app code changes, centralized policy | Requires service mesh infrastructure |

Envoy's rate limiting uses the sidecar model — the Envoy proxy calls an external gRPC rate limit service for global limiting, and can also do local token bucket limiting in-process. The two can be combined: local token bucket absorbs bursts, global service handles cross-instance coordination. [VERIFIED — Envoy docs]

### Atomicity Requirement

**Critical:** The check-and-decrement MUST be atomic. A naive implementation:

```
GET counter            → returns 99
if counter < 100       → true, allow
SET counter = 100      → increment
```

Between GET and SET, another request reads counter=99 and also passes → 101 requests allowed when the limit is 100.

**Solution:** Redis atomic `INCR` (returns the new value — check AFTER increment) or a Lua script that atomically reads, checks, and decrements in a single operation.

---

## 2. Rate Limit Response Headers

> These headers communicate rate limit state to clients on EVERY API response (not just 429s). Well-behaved clients use them to self-throttle.

### Standard Headers (de facto)

| Header | Type | Description | Example |
|---|---|---|---|
| `X-RateLimit-Limit` | integer | Maximum requests allowed in the current window | `1000` |
| `X-RateLimit-Remaining` | integer | Requests remaining in the current window | `742` |
| `X-RateLimit-Reset` | integer | Unix timestamp when the window resets | `1672531260` |
| `Retry-After` | integer | Seconds until the client should retry (only on 429) | `18` |

### HTTP 429 Response

```http
HTTP/1.1 429 Too Many Requests
Content-Type: application/json
X-RateLimit-Limit: 1000
X-RateLimit-Remaining: 0
X-RateLimit-Reset: 1672531260
Retry-After: 18

{
  "error": "rate_limit_exceeded",
  "message": "You have exceeded 1000 requests per minute. Upgrade to Pro for higher limits.",
  "rule_id": "rule-456",
  "dimension": "per_user",
  "retry_after": 18,
  "upgrade_url": "https://api.example.com/pricing"
}
```

### IETF Draft Standard

The IETF HTTPAPI Working Group is standardizing rate limit headers via **draft-ietf-httpapi-ratelimit-headers** (draft-10, September 2025). The draft proposes two structured header fields:

| Draft Header | Purpose | Example |
|---|---|---|
| `RateLimit-Policy` | Describes the quota policy | `100;w=60` (100 requests per 60-second window) |
| `RateLimit` | Communicates remaining quota | `remaining=42, reset=18` |

The draft is **algorithm-agnostic** — it standardizes communication, not enforcement. As of February 2026, it remains a draft (not yet an RFC). [VERIFIED — IETF Datatracker]

### Header Conventions by Provider

| Provider | Limit Header | Remaining Header | Reset Header | Notes |
|---|---|---|---|---|
| **Stripe** | `X-RateLimit-Limit` | `X-RateLimit-Remaining` | `X-RateLimit-Reset` | — |
| **GitHub** | `x-ratelimit-limit` | `x-ratelimit-remaining` | `x-ratelimit-reset` | Lowercase. GraphQL also has `rateLimit` in response body. |
| **Twitter/X** | `x-rate-limit-limit` | `x-rate-limit-remaining` | `x-rate-limit-reset` | Hyphenated variant |
| **Cloudflare** | Varies by product | Varies | Varies | Uses various conventions depending on the service |

The lack of a universal standard means every API client must handle provider-specific header formats — which is exactly what the IETF draft aims to solve.

---

## 3. Rate Limit Rule Management APIs (Control Plane)

> The control plane for operators to configure rate limits dynamically — without code deployments.

### `POST /rules` — Create a Rule

```json
{
  "match": {
    "client_id": "*",
    "api_key_tier": "free",
    "resource": "/api/v1/users/*",
    "method": "GET",
    "source_ip_cidr": "0.0.0.0/0"
  },
  "limit": {
    "requests": 100,
    "window": "1m",
    "algorithm": "sliding_window_counter",
    "burst": null
  },
  "action": {
    "on_limit": "reject",
    "retry_after": "dynamic",
    "custom_response": {
      "error": "rate_limit_exceeded",
      "message": "Free tier: 100 requests per minute. Upgrade for higher limits.",
      "upgrade_url": "https://api.example.com/pricing"
    }
  },
  "priority": 100,
  "enabled": true,
  "mode": "shadow",
  "expires_at": null
}
```

**Response:**
```json
{
  "id": "rule-123",
  "created_at": "2026-02-26T10:00:00Z",
  "status": "active",
  "mode": "shadow"
}
```

### `GET /rules` — List Rules

```
GET /rules?resource=/api/v1/users/*&tier=free&enabled=true
```

**Response:**
```json
{
  "rules": [
    {
      "id": "rule-123",
      "match": { "..." },
      "limit": { "..." },
      "priority": 100,
      "mode": "enforce",
      "created_at": "2026-02-26T10:00:00Z"
    }
  ],
  "total": 1,
  "page": 1
}
```

### `GET /rules/{ruleId}` — Get a Specific Rule

Returns the full rule definition including match criteria, limits, action, and audit history.

### `PUT /rules/{ruleId}` — Update a Rule

Changes propagate to all rate limiter nodes within seconds (via push notification + periodic polling as safety net).

```json
{
  "limit": {
    "requests": 200,
    "window": "1m"
  },
  "mode": "enforce"
}
```

### `DELETE /rules/{ruleId}` — Delete a Rule

Soft-delete with audit trail. The rule is deactivated immediately and permanently deleted after 30 days.

### Rule Fields Reference

| Field | Description | Values |
|---|---|---|
| `match.client_id` | Who is limited | `*` (all), specific ID, regex pattern |
| `match.api_key_tier` | Subscription tier | `free`, `pro`, `enterprise`, `internal` |
| `match.resource` | What is limited | URL pattern (`/api/v1/users/*`) or resource group |
| `match.method` | HTTP method | `GET`, `POST`, `*`, etc. |
| `match.source_ip_cidr` | IP range | CIDR notation (`203.0.113.0/24`) |
| `limit.requests` | How much | Integer (requests or points per window) |
| `limit.window` | Window size | `1s`, `1m`, `1h`, `1d` |
| `limit.algorithm` | Which algorithm | `token_bucket`, `sliding_window_counter`, `fixed_window`, `leaky_bucket` |
| `limit.burst` | Max burst (token bucket only) | Integer or null |
| `action.on_limit` | What happens | `reject` (429), `queue`, `degrade`, `log_only` (shadow mode) |
| `action.retry_after` | When to retry | `dynamic` (based on window reset) or fixed seconds |
| `priority` | Evaluation order | Integer (lower = higher priority) |
| `mode` | Enforcement mode | `shadow` (log only) or `enforce` (reject) |
| `expires_at` | Auto-expiry | ISO 8601 timestamp or null (permanent) |

---

## 4. Client Tier / Quota Management APIs

> Manage per-client rate limit tiers and view usage.

### `GET /clients/{clientId}/quota` — Current Usage

```json
{
  "client_id": "user_abc123",
  "tier": "pro",
  "quotas": [
    {
      "dimension": "per_user_global",
      "limit": 1000,
      "used": 258,
      "remaining": 742,
      "reset_at": 1672531260,
      "window": "1m"
    },
    {
      "dimension": "per_user_orders_post",
      "limit": 10,
      "used": 3,
      "remaining": 7,
      "reset_at": 1672531260,
      "window": "1m"
    }
  ]
}
```

### `PUT /clients/{clientId}/tier` — Change Tier

```json
{
  "tier": "enterprise",
  "effective_at": "2026-03-01T00:00:00Z",
  "reason": "Upgraded via sales agreement #SA-789"
}
```

Tier changes propagate to all rate limiter nodes in near-real-time.

### `GET /clients/{clientId}/usage` — Historical Usage

```
GET /clients/user_abc123/usage?from=2026-02-01&to=2026-02-26&granularity=1h
```

Returns time-series data of request counts, rejection counts, and quota utilization. Useful for dashboards and billing.

### Default Tier Limits

| Tier | Requests/min | Burst (token bucket) | Notes |
|---|---|---|---|
| **Free** | 100 | 20 | Sufficient for development/testing |
| **Pro** | 1,000 | 200 | Standard production use |
| **Enterprise** | 10,000 | 2,000 | Custom negotiated |
| **Internal** | 50,000 | 10,000 | Internal services, higher limits |

---

## 5. Override / Exemption APIs

> Temporary overrides for operational flexibility — migrations, incidents, partner events.

### `POST /overrides` — Create Override

```json
{
  "client_id": "user_abc123",
  "type": "increase",
  "new_limit": 5000,
  "window": "1m",
  "reason": "Data migration — ticket OPS-456",
  "created_by": "admin@example.com",
  "expires_at": "2026-02-27T00:00:00Z"
}
```

### `GET /overrides` — List Active Overrides

Returns all currently active overrides with creator, reason, and expiration.

### `DELETE /overrides/{overrideId}` — Remove Override

Immediately deactivates the override. Audit trail retained.

**Key properties:**
- Time-bound — auto-expire to prevent forgotten overrides
- Audited — who created them, why, when they expire
- Override types: `increase` (raise limit), `exempt` (bypass rate limiting entirely), `decrease` (emergency throttle)

---

## 6. Analytics / Monitoring APIs

> Power operational dashboards and abuse detection.

### `GET /metrics/throughput`

Current request throughput by resource, client, and tier.

```json
{
  "period": "1m",
  "data": [
    { "resource": "/api/v1/orders", "allowed": 4521, "rejected": 23 },
    { "resource": "/api/v1/users", "allowed": 12034, "rejected": 0 }
  ]
}
```

### `GET /metrics/rejections`

Rate of 429 responses, broken down by resource, client, and rule.

### `GET /metrics/latency`

Rate limiter decision latency:
```json
{
  "p50_ms": 0.3,
  "p95_ms": 0.7,
  "p99_ms": 1.2,
  "p999_ms": 3.1
}
```

### `GET /metrics/top-clients`

Clients consuming the most quota — useful for identifying abuse or candidates for tier upgrades.

```json
{
  "top_clients": [
    { "client_id": "user_xyz", "requests_1h": 58420, "rejection_rate": 0.12 },
    { "client_id": "user_abc", "requests_1h": 42100, "rejection_rate": 0.0 }
  ]
}
```

---

## 7. Health / Ops APIs

> Operational endpoints for the rate limiter itself.

### `GET /health`

```json
{
  "status": "healthy",
  "redis": {
    "connected": true,
    "latency_ms": 0.4,
    "memory_used_mb": 847,
    "connections_active": 42
  },
  "rules_cache": {
    "last_sync": "2026-02-26T10:30:00Z",
    "rules_loaded": 156,
    "staleness_seconds": 2
  },
  "decision_latency_p99_ms": 1.1
}
```

### `POST /config/reload`

Force reload rules from the database. Used after a rule change that needs immediate propagation (bypasses the normal push/poll cycle).

### `GET /config/active`

Show currently active rules and their sources. Useful for debugging: "Why was my request rejected?"

```json
{
  "active_rules": 156,
  "rules_by_source": {
    "database": 150,
    "overrides": 4,
    "system_defaults": 2
  },
  "last_propagation": "2026-02-26T10:30:02Z"
}
```

---

## Contrast: Rate Limiter vs API Gateway Rate Limiting

| Aspect | Dedicated Rate Limiter | API Gateway (Kong, AWS API Gateway) |
|---|---|---|
| **Multi-dimension** | Per-user + per-endpoint + per-IP + global simultaneously | Usually single-dimension (per-API or per-stage) |
| **Algorithms** | Multiple (token bucket, sliding window, etc.) | Usually one (AWS API Gateway: token bucket) |
| **Dynamic rules** | API-driven, no redeployment | Often requires configuration change/redeployment |
| **Analytics** | Deep per-client, per-rule analytics | Basic throughput metrics |
| **Non-HTTP workloads** | Can rate-limit queue consumers, batch jobs | HTTP-only |
| **Best for** | Multi-tenant platforms (Stripe, GitHub, Twilio) | Simple API protection |

AWS API Gateway provides configurable per-stage throttling using token bucket (rate + burst parameters). Returns 429. Simple to configure but limited: no per-user limits without custom authorizer logic, no sliding window, no multi-dimension limiting. [VERIFIED — AWS docs]

Kong provides a rate limiting plugin with Redis-backed distributed counters. More configurable than AWS API Gateway but still tied to the gateway's request lifecycle.

## Contrast: Rate Limiter vs CDN/Edge Rate Limiting (Cloudflare)

| Aspect | Application Rate Limiter | CDN/Edge (Cloudflare) |
|---|---|---|
| **Signals** | User ID, API key, tier, request body | IP, URL, headers, TLS fingerprint |
| **Context** | Full application context | No application context |
| **Purpose** | Per-user quota enforcement | DDoS protection, bot mitigation |
| **Where** | After authentication | Before traffic reaches origin |

A complete system needs BOTH: edge-level (block abusive IPs before they reach the application) and application-level (enforce per-user/per-tier quotas with business context).

## Contrast: Server-Side vs Client-Side Throttling

Server-side rate limiting is the **enforcement** mechanism. Client-side throttling is an **optimization**.

Well-behaved clients use rate limit response headers to self-throttle. Google Cloud client libraries implement **adaptive throttling** — the client tracks its rejection rate and proactively backs off before the server rejects. This reduces server-side load during overload.

But client-side throttling is **cooperative** — malicious or buggy clients ignore it entirely. Server-side rate limiting must never rely on clients behaving correctly.

---

*See also: [Interview Simulation](01-interview-simulation.md) (Phase 3) for the interview-focused subset of these APIs.*
