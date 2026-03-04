# Rate Limiter — Multi-Layer Rate Limiting Architecture

> A production rate limiting system operates at MULTIPLE layers. Each layer serves a different purpose with different signals and different threat models. No single layer handles everything. Defense in depth.

---

## The Four Layers

```
Internet Traffic
    │
    ▼
┌─── Layer 1: Edge / CDN ──────────────────────────────────────────┐
│ Cloudflare / AWS WAF / CloudFront / Akamai                       │
│ Block: DDoS, bot traffic, IP-based rate limiting                 │
│ Signals: IP address, request rate per IP, geographic origin,     │
│          known bad IP lists, TLS fingerprint (JA3 hash),         │
│          HTTP header order analysis                               │
│ Limits: Coarse. "No more than 1,000 req/s from any single IP"   │
│ Latency added: ~0ms (evaluated at edge, before routing)          │
│ Limitation: No application context — can't tell users apart      │
│ behind the same corporate NAT                                     │
└────────────────────────┬─────────────────────────────────────────┘
                         │
                         ▼
┌─── Layer 2: API Gateway / Load Balancer ─────────────────────────┐
│ Kong / NGINX / AWS API Gateway / Envoy / Custom                  │
│ Authenticate → identify user/tier → check Redis counters         │
│ Signals: Authenticated user ID, API key, request path,           │
│          HTTP method, client tier (resolved after auth)           │
│ Limits: Medium. "Free: 100 req/min. Pro: 1,000 req/min.         │
│          This endpoint: 10 req/min per user."                    │
│ Latency added: ~1-2ms (Redis roundtrip from gateway)             │
│ Limitation: Per-request only. Can't enforce "max 1GB data/day"   │
│ (requires inspecting response sizes, which happens after)        │
└────────────────────────┬─────────────────────────────────────────┘
                         │
                         ▼
┌─── Layer 3: Application (in-process or sidecar) ─────────────────┐
│ Business-logic-aware rate limiting                                │
│ Signals: Full application context — user identity, request        │
│          payload, database state, business rules                  │
│ Limits: Fine-grained.                                             │
│   "Max 10 password attempts per account per hour"                │
│   "Max 5 file uploads per user per day"                          │
│   "Max 3 payment retries per transaction"                        │
│ Latency added: ~0.1ms (in-process) to ~1ms (sidecar + Redis)    │
│ Limitation: Only sees traffic that reaches the application.       │
│ Can't protect against DDoS (Layer 1's job).                      │
└────────────────────────┬─────────────────────────────────────────┘
                         │
                         ▼
┌─── Layer 4: Internal Services ───────────────────────────────────┐
│ Per-service quotas (noisy neighbor protection)                    │
│ Signals: Calling service identity (mTLS certificate, service     │
│          mesh identity), request path, rate                       │
│ Limits: Per-service. "Service A → Service B: max 5,000 req/s.   │
│          Service C → Service B: max 500 req/s."                  │
│ Implementation: Service mesh (Istio/Envoy) rate limiting at      │
│                 sidecar level, or in-process rate limiting        │
│ Limitation: Only applies to internal traffic                      │
└──────────────────────────────────────────────────────────────────┘
```

---

## Layer 1: Edge / CDN

**Purpose:** Block abusive traffic BEFORE it reaches your infrastructure. Volumetric DDoS protection, bot detection, IP-based rate limiting.

### Signals Available
| Signal | Example | Use Case |
|---|---|---|
| IP address | `203.0.113.42` | Block known bad IPs |
| Request rate per IP | 10,000 req/s from one IP | DDoS detection |
| Geographic origin | Country, ASN | Block traffic from unexpected regions |
| TLS fingerprint (JA3) | Hash of TLS client hello | Bot detection (bots have distinctive fingerprints) |
| HTTP header order | Headers in unusual order | Bot detection |
| Known bad IP lists | Threat intelligence feeds | Block known botnets, Tor exit nodes |

### What It Can't Do
- **No application context** — can't distinguish between user A and user B behind the same corporate NAT (same IP). A company with 10,000 employees behind one IP looks like one client.
- **No per-user quotas** — doesn't know who the user is (authentication hasn't happened yet).
- **No per-endpoint limits** — can filter by URL pattern, but can't enforce different limits for different API endpoints based on cost.

### Contrast with Application-Level
Edge rate limiting is a **blunt instrument** — effective against volumetric attacks but useless for per-user API quota enforcement. Application-level rate limiting is a **scalpel** — precise per-user/per-endpoint limits but can't handle DDoS (the traffic has already reached your servers by the time rate limiting kicks in).

---

## Layer 2: API Gateway

**Purpose:** Enforce per-route and per-client rate limits AFTER authentication but BEFORE the request reaches application servers. This is where most API rate limiting lives.

### How It Works

```
Request → API Gateway
       → Authenticate (extract user ID, API key, tier)
       → Build rate limit key: user_id + endpoint + method
       → Check Redis counter (atomic INCR or Lua script)
       → If within limit → forward to application
       → If over limit → return 429 with RateLimit headers
```

### Gateway Implementations

| Gateway | Algorithm | Backend | Notes |
|---|---|---|---|
| **AWS API Gateway** | Token bucket | Built-in | Rate + burst per account/stage/method. Default: 10K RPS, 5K burst. [VERIFIED] |
| **Kong** | Multiple (plugin) | Redis | Flexible plugin ecosystem, supports multiple strategies |
| **NGINX** | Leaky bucket | In-memory (shared zone) | `limit_req` module. Returns 503 by default, not 429! [VERIFIED] |
| **Envoy** | External service | gRPC + Redis | Sidecar calls external rate limit service. Also supports local token bucket. [VERIFIED] |

### What It Can't Do
- **Complex quotas** — can't enforce "max 1GB of data transfer per day" (requires inspecting response sizes after the request is processed).
- **Business-logic limits** — can't enforce "max 10 password attempts per account" (requires understanding the request body and account state).

---

## Layer 3: Application

**Purpose:** Fine-grained, business-logic-aware rate limiting. Limits that require application context — the gateway doesn't have this information.

### Examples

| Limit | Why It Needs Application Context |
|---|---|
| Max 10 password attempts per account per hour | Requires knowing the account from the request body |
| Max 5 file uploads per user per day | Requires knowing upload count from database |
| Max 3 payment retries per transaction | Requires knowing the transaction ID and retry count |
| Max 1 account creation per IP per day | Requires cross-referencing IP with account creation history |

### Implementation Options

| Option | Latency | Coupling |
|---|---|---|
| **In-process middleware** | ~0.1ms (in-memory counter) | Tight — same process as the application |
| **Sidecar proxy (Envoy)** with custom descriptors | ~1ms | Loose — separate process |
| **Application library** calling Redis | ~1ms (Redis call) | Medium — library dependency |

---

## Layer 4: Internal Service-to-Service

**Purpose:** Protect internal services from noisy-neighbor problems in a microservices architecture. Service A should not be able to overwhelm Service B.

### The Noisy Neighbor Problem

```
                    ┌──────────────┐
                    │  Service B    │
                    │  (database    │
                    │   service)    │
                    └──┬───┬───┬───┘
                       │   │   │
              5000/s   │   │   │  500/s
             ┌─────────┘   │   └──────────┐
             │             │              │
        ┌────▼────┐  ┌────▼────┐   ┌─────▼───┐
        │Service A│  │Service C│   │Service D│
        │ (batch  │  │ (API    │   │ (cron   │
        │  job)   │  │  server)│   │  job)   │
        └─────────┘  └─────────┘   └─────────┘

Service A runs a batch job → sends 50,000 req/s to Service B
Service B becomes overloaded → Service C and D also fail
→ Cascading failure across the entire platform
```

### Solution: Per-Service Quotas

```
Service A → Service B: max 5,000 req/s
Service C → Service B: max 10,000 req/s
Service D → Service B: max 500 req/s
Total capacity of Service B: 20,000 req/s (with headroom)
```

### Implementation
- **Service mesh (Istio/Envoy):** Rate limiting at the sidecar level. Identity via mTLS certificates. No application code changes.
- **In-process middleware:** Each service enforces limits on outgoing calls. Requires library adoption.

### Contrast with External API Rate Limiting

| Aspect | External Rate Limiting | Internal Rate Limiting |
|---|---|---|
| **Protects against** | Clients abusing the API | Services overwhelming each other |
| **Threat model** | Malicious or buggy external clients | Buggy or misconfigured internal services |
| **Identity** | API key, user ID | Service identity (mTLS certificate) |
| **Limits** | Per-user, per-tier | Per-service |
| **Consequences of no limiting** | Individual user gets too much access | Cascading failure across platform |

---

## How Layers Work Together

### Scenario: DDoS Attack + Legitimate User Overuse

```
1. DDoS: 100,000 req/s from a botnet (many IPs)
   → Layer 1 (Edge): Cloudflare detects unusual traffic pattern
   → Blocks 95% based on IP rate + TLS fingerprint + geo
   → 5,000 req/s leak through (sophisticated bots)

2. Remaining 5,000 req/s reach API Gateway
   → Layer 2 (Gateway): Requests lack valid API keys
   → Return 401 Unauthorized (not even rate limited — auth fails first)
   → 0 bot requests reach the application

3. Meanwhile, legitimate user "power_corp" is making 2,000 req/min
   → Layer 2 (Gateway): power_corp is on "pro" tier (limit: 1,000 req/min)
   → First 1,000 allowed, remaining 1,000 get 429
   → power_corp sees: "Upgrade to Enterprise for 10,000 req/min"

4. power_corp's allowed requests include 50 password reset attempts
   → Layer 3 (Application): "Max 5 password resets per account per hour"
   → First 5 allowed, remaining 45 rejected at application level
   → Different 429 response: "Too many password reset attempts"

5. power_corp's batch service hammers the Orders service
   → Layer 4 (Internal): "power_corp_service → Orders: max 200 req/s"
   → Excess requests get 503 from the sidecar
   → Other services' access to Orders is unaffected
```

Each layer caught something the others couldn't:
- Layer 1: DDoS (volume-based, no auth context)
- Layer 2: Per-user quota (auth-based)
- Layer 3: Business-logic limit (password resets)
- Layer 4: Internal service protection (noisy neighbor)

---

## Contrast: Single-Layer vs Multi-Layer

| Threat | Single Layer (Gateway Only) | Multi-Layer (All 4) |
|---|---|---|
| DDoS (100K req/s) | Gateway overwhelmed before rate limiting kicks in | Layer 1 blocks at edge — gateway never sees it |
| Credential stuffing (many IPs) | Caught if per-account limit exists | Layer 1 + Layer 3 (both IP-level and account-level) |
| User exceeding API quota | Caught | Caught (same as single-layer) |
| Business-logic abuse (password attempts) | Not caught (gateway doesn't parse request body) | Layer 3 catches it |
| Internal service overwhelm | Not caught (gateway only sees external traffic) | Layer 4 catches it |

**A single-layer rate limiter is necessary but insufficient.** Most system design interview answers describe only Layer 2 (API gateway with Redis). A complete answer describes all four layers.

---

*See also: [Interview Simulation](01-interview-simulation.md) (Attempt 3) for the interview discussion of multi-layer architecture.*
