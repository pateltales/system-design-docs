# Rate Limiter — Rules Engine & Configuration Deep Dive

> The rules engine defines WHO is rate-limited, on WHAT resource, HOW MUCH, and WHAT HAPPENS when the limit is exceeded. A well-designed rules engine is the difference between a rate limiter that operators love and one that causes constant pain.

---

## Rule Structure

```json
{
  "id": "rule-123",
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
      "message": "You have exceeded 100 requests per minute. Upgrade to Pro for higher limits.",
      "upgrade_url": "https://api.example.com/pricing"
    }
  },
  "priority": 100,
  "enabled": true,
  "mode": "enforce",
  "created_at": "2026-01-15T10:00:00Z",
  "updated_at": "2026-02-20T14:30:00Z",
  "expires_at": null,
  "created_by": "admin@example.com"
}
```

### Rule Fields Explained

| Field | Purpose | Values |
|---|---|---|
| **match.client_id** | WHO is limited | `*` (all), specific ID, regex |
| **match.api_key_tier** | Tier-based limits | `free`, `pro`, `enterprise`, `internal` |
| **match.resource** | WHAT is limited | URL pattern (`/api/v1/users/*`), resource group |
| **match.method** | HTTP method filter | `GET`, `POST`, `*` |
| **match.source_ip_cidr** | IP range filter | CIDR notation (`203.0.113.0/24`) |
| **limit.requests** | HOW MUCH | Integer (requests or points per window) |
| **limit.window** | Window size | `1s`, `1m`, `1h`, `1d` |
| **limit.algorithm** | Which algorithm | `token_bucket`, `sliding_window_counter`, `fixed_window`, `leaky_bucket` |
| **limit.burst** | Max burst (token bucket) | Integer or null |
| **action.on_limit** | WHAT HAPPENS | `reject` (429), `queue`, `degrade`, `log_only` |
| **action.retry_after** | Retry guidance | `dynamic` or fixed seconds |
| **priority** | Evaluation order | Integer (lower = higher priority) |
| **mode** | Enforcement mode | `shadow` (log only) or `enforce` (reject) |
| **expires_at** | Auto-expiry | ISO 8601 timestamp or null (permanent) |

---

## Multi-Dimension Rate Limiting

A single request may be evaluated against **MULTIPLE rules simultaneously**. This is critical — single-dimension limiting has blind spots that attackers exploit.

### Example: Multi-Dimension Evaluation

```
Request: POST /api/v1/orders
         by user U1
         from IP 203.0.113.42
         tier: pro

Rule 1 (per-user global):
  Match: client_id=U1, resource=*, method=*
  Limit: 1,000 req/min
  Counter: 847/1,000 → ✓ PASS

Rule 2 (per-endpoint per-user):
  Match: client_id=U1, resource=/api/v1/orders, method=POST
  Limit: 10 req/min
  Counter: 9/10 → ✓ PASS (barely)

Rule 3 (global endpoint):
  Match: client_id=*, resource=/api/v1/orders, method=POST
  Limit: 5,000 req/min (all users combined)
  Counter: 3,211/5,000 → ✓ PASS

Rule 4 (per-IP):
  Match: source_ip=203.0.113.0/24
  Limit: 500 req/min
  Counter: 501/500 → ✗ FAIL

Decision: REJECT
Reason: Rule 4 (per-IP limit exceeded)
Response: 429 with error indicating which limit was exceeded
```

### Why Multi-Dimension Matters

| Attack Vector | Single-Dimension (per-user only) | Multi-Dimension |
|---|---|---|
| User sends all quota to one expensive endpoint | Not caught (within global user limit) | Caught by per-endpoint limit |
| Credential stuffing from many IPs, one account | Not caught if under user limit | Caught by per-account + per-IP limits |
| Bot network from many IPs, many accounts | Not caught per-user | Caught by global endpoint limit |
| Single IP hammering many endpoints | Not caught per-user | Caught by per-IP limit |

**Implementation:** Each dimension is a separate Redis key. For a request matching 4 rules, make 4 Redis calls pipelined in a single roundtrip (~1ms total). ALL dimensions must pass for the request to be allowed.

---

## Rule Evaluation Order and Priority

Rules are evaluated by priority (lowest number = highest priority). For each dimension, the first matching rule is applied.

```
Priority 1:   Enterprise client "bigcorp" override → 50,000 req/min
Priority 10:  Enterprise tier default → 10,000 req/min
Priority 100: Pro tier default → 1,000 req/min
Priority 200: Free tier default → 100 req/min
Priority 999: Global fallback → 50 req/min

Request from "bigcorp" (enterprise tier):
  → Matches Priority 1 (specific override) → 50,000 req/min
  → Priority 10 and 100 also match but are lower priority → skipped

Request from random enterprise user:
  → No match at Priority 1 (not "bigcorp")
  → Matches Priority 10 → 10,000 req/min
```

This allows fine-grained exceptions without modifying base rules. An enterprise client with a custom SLA gets a specific override (priority 1) that supersedes the default tier rule (priority 10).

### Rule Conflict Resolution

When multiple rules match at the same priority level for the same dimension:
1. **Most specific match wins** — `/api/v1/orders` beats `/api/v1/*` beats `*`
2. **Client-specific beats tier-based** — `client_id=abc` beats `api_key_tier=pro`
3. **If still ambiguous** — apply the most restrictive limit (conservative)

---

## Shadow Mode / Log-Only Mode

When deploying a new rate limit rule, start in **shadow mode**:

```
Rule creation workflow:

1. Create rule with mode: "shadow"
   → Rule is evaluated for every matching request
   → Metrics are recorded (would-be rejections counted)
   → Requests are NOT actually rejected

2. Monitor for 24-48 hours
   → Dashboard shows: "This rule would have rejected 0.3% of requests"
   → "Affected clients: user_abc (heavy), user_xyz (moderate)"
   → "No impact on top-10 revenue clients" ✓

3. If safe, flip to mode: "enforce"
   → Rule now actively rejects requests

4. If not safe, adjust limits or match criteria and repeat
```

### Why Shadow Mode Is Critical

Without shadow mode:
```
Day 1: Deploy rule "free tier: 100 req/min for /api/v1/*"
Day 1 + 5 min: Biggest customer (still on free tier, about to upgrade)
               starts getting 429s on their production integration
Day 1 + 30 min: Customer escalation, VP of Sales calls Engineering
Day 1 + 45 min: Rule rolled back in a panic
```

With shadow mode:
```
Day 1: Deploy rule in shadow mode
Day 2: Dashboard shows biggest customer would be affected
Day 2: Create an override for that customer before enforcing
Day 3: Enforce rule — no customer impact
```

Shadow mode prevents the "we deployed a rate limit and accidentally blocked our biggest customer" disaster.

---

## Rule Propagation

When a rule is created or updated via the management API, it must propagate to ALL rate limiter nodes within seconds.

### Propagation Mechanisms

| Mechanism | Latency | Reliability | Complexity |
|---|---|---|---|
| **Polling** | Up to N seconds stale | High (simple GET) | Low |
| **Push (Pub/Sub)** | Near real-time (~100ms) | Medium (messages can be lost) | Medium |
| **Config sync (etcd/ZooKeeper)** | Near real-time | High (strong consistency) | High |

### Recommended: Push + Polling

```
Rule change → PostgreSQL (source of truth)
          ↓
          → Kafka / Redis Pub/Sub (push notification)
          ↓
All rate limiter nodes receive push → update local cache immediately
          +
Periodic polling every 30s → safety net in case push was missed
```

**Why both?** Push for speed (propagation in <1 second). Polling as a safety net (in case a push message is lost due to network partition or subscriber restart). Within 30 seconds, all nodes are guaranteed to have the latest rules — even if push failed.

### Rule Cache Architecture

```
┌──────────────────────────────────────────┐
│             Rate Limiter Node             │
│                                           │
│  ┌─────────────────────────────────┐     │
│  │      Local Rules Cache           │     │
│  │  (in-memory, indexed by resource)│     │
│  │                                   │     │
│  │  Updated by:                      │     │
│  │  1. Push notification (immediate) │     │
│  │  2. Polling (every 30s, safety)   │     │
│  └─────────────────────────────────┘     │
│                                           │
│  Request → Match against local cache      │
│         → No DB call per request          │
│         → Cache miss? → Use default rule  │
└──────────────────────────────────────────┘
```

**Critical:** Rules are cached locally to avoid per-request database lookups. The database (PostgreSQL) is the source of truth, but the hot path never touches it.

---

## Contrast: AWS WAF Rules vs Application-Level Rules

| Aspect | AWS WAF Rules | Application Rate Limiter Rules |
|---|---|---|
| **Where** | Edge (CloudFront / ALB) | Application layer |
| **Signals** | IP, URL, headers, query params | User ID, API key, tier, request body |
| **Context** | No application context | Full application context |
| **Rule types** | IP allow/block lists, rate-based rules, SQL injection patterns | Per-user quotas, per-endpoint limits, business logic limits |
| **Configuration** | JSON rules, deployed via AWS console/CLI | API-driven, shadow mode, hot-reload |
| **Use case** | First line of defense (DDoS, bots) | Precision layer (per-user, per-tier) |

AWS WAF is a **first line of defense**. Application-level rate limiting is the **precision layer**. Both are needed.

---

## Contrast: Stripe's Multi-Tier Limiter System

Stripe runs **four** distinct limiters, not just one. This is a best-in-class reference for production rate limiting:

| Limiter | Type | What It Does |
|---|---|---|
| **Request Rate Limiter** | Rate limiter | Token bucket. Limits each user to N req/s. Most commonly triggered. |
| **Concurrent Requests Limiter** | Rate limiter | Caps simultaneous in-flight requests (e.g., max 20 concurrent). Not token bucket — tracks active requests. |
| **Fleet Usage Load Shedder** | Load shedder | Reserves a percentage of infrastructure for critical operations. Non-critical requests get 503 during overload. |
| **Worker Utilization Load Shedder** | Load shedder | Last line of defense. Prioritizes traffic into tiers: critical methods → POSTs → GETs → test mode. Sheds progressively. |

[VERIFIED — Stripe engineering blog, 2017]

**Key insight:** Only the first two are true rate limiters. The other two are **load shedders** — they protect the system from aggregate overload, regardless of which client sent the request. Rate limiting (per-client quotas) and load shedding (global capacity protection) are distinct but complementary.

Stripe also distinguishes **test mode** vs **live mode** limits — test mode is more permissive to allow developers to iterate quickly.

---

## Contrast: GitHub's Point-Based System

GitHub's GraphQL API uses **cost-based rate limiting** instead of simple request counting:

| Aspect | Simple Request Counting | GitHub's Point System |
|---|---|---|
| **Unit** | 1 request = 1 unit | Each query assigned a cost (1-100+ points) |
| **Fairness** | Unfair — cheap and expensive queries cost the same | Fair — expensive queries consume more quota |
| **Limit** | N requests per window | 5,000 points/hour (regular users) |
| **Calculation** | Trivial (increment by 1) | Complex (analyze query complexity) |

GitHub calculates points by:
1. Counting sub-requests needed for each connection (using `first`/`last` args)
2. Dividing total by 100
3. Rounding to nearest whole number (minimum 1 point)

Example: querying 100 repos × 50 issues × 60 labels = 5,101 sub-requests = **51 points**.

[VERIFIED — GitHub official docs]

**When to use cost-based limiting:** When your API has endpoints with dramatically different resource costs. A simple user lookup shouldn't consume the same quota as a complex analytics query. GitHub's approach prevents a single expensive query from consuming the same quota as 100 cheap queries.

---

## Rule Management Best Practices

### 1. Always Start in Shadow Mode
Never deploy a new rule directly in enforcement mode. Observe for 24-48 hours first.

### 2. Approval Workflow for Production Changes
Rule changes to production should require approval (two-person rule). A misconfigured rule can block all traffic.

### 3. Automatic Rollback
If rejection rate spikes >10× within 5 minutes of a rule change, automatically revert to the previous rule version.

### 4. Audit Trail
Every rule change (create, update, delete, mode change) is logged with: who, what, when, why. Essential for post-incident analysis.

### 5. Rule Versioning
Keep a history of rule versions. Allow rollback to any previous version. Show diffs between versions.

### 6. Expiring Rules
Temporary rules (event overrides, migration windows) should have `expires_at` set. Forgotten overrides are a common operational problem.

### 7. Rule Testing
Provide a "dry run" API: `POST /rules/test` — given a sample request, show which rules would match and what the decision would be. Useful before deploying.

---

*See also: [Interview Simulation](01-interview-simulation.md) (Attempt 2) for the interview discussion of rules engine design.*
