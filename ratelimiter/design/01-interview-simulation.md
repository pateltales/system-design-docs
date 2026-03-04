# System Design Interview Simulation: Design a Rate Limiter

> **Interviewer:** Principal Engineer (L8), API Platform Team
> **Candidate Level:** SDE-3 (L6 — Senior Software Development Engineer)
> **Duration:** ~60 minutes
> **Date:** February 26, 2026

---

## PHASE 1: Opening & Problem Statement (~2 min)

**Interviewer:**

> "Welcome. I'm a Principal Engineer on the API platform team. For today's design round, I'd like you to design a **rate limiting system**. Not just 'add rate limiting to an API' — I want you to think of the rate limiter as the product itself. A system that multiple services can use to enforce request quotas. Take it wherever you think is appropriate. Go ahead."

---

## PHASE 2: Requirements Gathering & Scoping (~8 min)

**Candidate:**

> "Thanks. Before I dive in, I want to make sure I understand the scope. A rate limiter is deceptively simple on the surface — a HashMap with counters — but the distributed coordination problem is what makes it interesting. Let me clarify requirements."

**Functional Requirements:**

> "The core use case is: given an incoming API request, decide whether to **allow** or **reject** it based on configured rate limits. That breaks down into:
>
> 1. **Rate limit check (hot path)** — For every incoming request, check if the client has remaining quota. If yes, decrement and allow. If no, reject with `429 Too Many Requests` and appropriate headers.
> 2. **Rule management (control plane)** — Operators need to create, update, and delete rate limit rules without code deployments. Rules define who is limited, on what resource, how much, and what happens when exceeded.
> 3. **Multi-dimension limiting** — A single request should be evaluated against multiple rules simultaneously: per-user, per-endpoint, per-IP, per-tier, and global limits. ALL must pass.
> 4. **Response contract** — Clients need `X-RateLimit-Remaining`, `X-RateLimit-Limit`, `X-RateLimit-Reset` headers and `Retry-After` on 429 responses so well-behaved clients can self-throttle.
>
> A few clarifying questions:
> - **Is this a standalone service or embedded middleware?**"

**Interviewer:** "Good question. Design it so it can work both ways — as a dedicated service that API gateways call, and potentially as embedded middleware."

> "- **Do we need multiple rate limiting algorithms?** Token bucket, sliding window, etc.?"

**Interviewer:** "Yes, different use cases need different algorithms. Support at least two."

> "- **Multi-region?**"

**Interviewer:** "Start single-region, but tell me how you'd extend it."

> "- **What scale are we targeting?**"

**Interviewer:** "Think Stripe or GitHub scale — millions of API clients, billions of requests per day."

**Candidate:**

> "Got it. Let me scope the non-functional requirements and do some back-of-envelope math."

**Non-Functional Requirements:**

> "| Dimension | Requirement | Rationale |
> |---|---|---|
> | **Decision Latency** | <1ms P99 | Rate limiter must not become the bottleneck. Every API request pays this cost. |
> | **Throughput** | 1M+ rate limit checks/second | At Stripe/GitHub scale: 10B requests/day ÷ 86,400 ≈ 115K/s average, 5-10x peak → ~1M/s |
> | **Availability** | 99.99% (4 nines) | If rate limiter is down, we either fail open (risk overload) or fail closed (self-inflicted outage). Both bad. |
> | **Accuracy** | Near-exact per-client counts | A client with a limit of 100/min should not be able to send 200. Over-admission undermines trust. |
> | **Rule Propagation** | <5 seconds | When an operator changes a rule, all rate limiter nodes should reflect it within seconds. |
> | **Consistency** | Strong for counters, eventual for rules | Counter operations must be atomic (no double-counting). Rule propagation can tolerate seconds of staleness. |
>
> **Scale estimates:**
> - 10 billion requests/day → ~115K requests/sec average, ~1M/sec peak
> - 10 million unique API clients (by API key or user ID)
> - Each client checked against 2-4 rules per request → 2-4M rule evaluations/sec at peak
> - Counter storage: 10M clients × 4 dimensions × 2 counters (sliding window) = ~80M counters in Redis
> - At 16 bytes per counter → ~1.3 GB. Redis can handle this easily.
>
> One important distinction I want to call out: **rate limiting is not load shedding**. Rate limiting enforces per-client quotas proactively — it protects the platform from individual clients. Load shedding drops requests globally when the system is overloaded regardless of who sent them — it's reactive. We need both in a production system, but they're architecturally distinct. Similarly, **circuit breakers** (like Hystrix or Resilience4j) solve the opposite problem — they protect a client from a failing downstream service by stopping outgoing calls. Today we're designing the rate limiter specifically."

**Interviewer:** "Good. I like that you distinguished rate limiting from load shedding and circuit breaking — many candidates conflate them. Let's move to the API."

---

### L5 vs L6 vs L7 — Phase 2 Rubric

| Aspect | L5 (SDE-2) | L6 (SDE-3) | L7 (Principal) |
|---|---|---|---|
| **Functional Reqs** | Lists "check if request is allowed" and "return 429" | Proactively raises multi-dimension limiting, shadow mode, rule management as a first-class concern | Additionally discusses cost-based limiting (GitHub's point system), adaptive rate limiting based on backend health |
| **Non-Functional** | Says "low latency" and "high availability" | Quantifies <1ms P99, calculates 1M checks/sec from 10B requests/day, sizes Redis memory | Frames NFRs in business terms — "1ms overhead is acceptable because API P99 is ~50ms; rate limiter is <2% of that" |
| **Scale Math** | Skips or does rough estimates | Calculates throughput, counter storage, Redis memory with specific numbers | Validates estimates against known systems — "Stripe processes ~hundreds of millions of API calls/day, so 10B/day is reasonable for our target" |
| **Scoping** | Accepts problem as given | Drives clarifying questions, distinguishes rate limiting from load shedding/circuit breaking | Negotiates scope based on time — "Let me cover the distributed coordination problem deeply, and mention multi-region as an extension" |

---

## PHASE 3: API Design (~5 min)

**Candidate:**

> "Let me define the key API surfaces. There are two distinct planes: the **data plane** (hot path, every request) and the **control plane** (rule management, infrequent).
>
> **Data Plane — Rate Limit Check (the critical path):**
>
> ```
> POST /ratelimit/check
>
> Request:
> {
>   "client_id": "user_abc123",
>   "resource": "/api/v1/orders",
>   "method": "POST",
>   "client_tier": "pro",
>   "source_ip": "203.0.113.42"
> }
>
> Response (allowed):
> {
>   "decision": "ALLOW",
>   "rate_limit_headers": {
>     "X-RateLimit-Limit": 1000,
>     "X-RateLimit-Remaining": 742,
>     "X-RateLimit-Reset": 1672531260
>   }
> }
>
> Response (rejected):
> {
>   "decision": "REJECT",
>   "rate_limit_headers": {
>     "X-RateLimit-Limit": 1000,
>     "X-RateLimit-Remaining": 0,
>     "X-RateLimit-Reset": 1672531260,
>     "Retry-After": 18
>   },
>   "error": {
>     "code": "rate_limit_exceeded",
>     "message": "Per-user limit of 1000 req/min exceeded",
>     "rule_id": "rule-456"
>   }
> }
> ```
>
> A critical implementation detail: the check-and-decrement must be **atomic**. If two requests arrive simultaneously and only one slot remains, exactly one must be allowed. A naive GET-then-SET has a race condition — the only safe approach is atomic increment (Redis `INCR`) or a Lua script that reads, checks, and decrements in a single atomic operation.
>
> In practice, this `POST /ratelimit/check` call may not be an actual HTTP call. Three deployment models:
> 1. **Dedicated service** — API gateway calls the rate limiter over gRPC/HTTP. Adds ~1ms network hop. Cleanest separation of concerns.
> 2. **Embedded middleware** — Rate limiting logic runs in-process within the application. No network hop (~microseconds). But couples rate limiter to the application's language and lifecycle.
> 3. **Sidecar proxy** (Envoy/Istio) — Rate limiter runs as a sidecar. Localhost network hop (~0.1ms). Best of both: no application code changes, minimal latency, centralized policy. This is what Envoy's rate limiting service does — the sidecar calls an external gRPC rate limit service for global limiting, and can also do local token bucket limiting in-process.
>
> **Control Plane — Rule Management:**
>
> ```
> POST   /rules              — Create a new rate limit rule
> GET    /rules              — List all rules (filterable by resource, tier)
> GET    /rules/{ruleId}     — Get a specific rule
> PUT    /rules/{ruleId}     — Update a rule
> DELETE /rules/{ruleId}     — Delete a rule
> ```
>
> **Client response contract (HTTP headers on every API response):**
>
> ```
> HTTP/1.1 429 Too Many Requests
> X-RateLimit-Limit: 1000
> X-RateLimit-Remaining: 0
> X-RateLimit-Reset: 1672531260
> Retry-After: 18
> Content-Type: application/json
>
> {
>   "error": "rate_limit_exceeded",
>   "message": "You have exceeded 1000 requests per minute. Upgrade to Pro for higher limits.",
>   "upgrade_url": "https://api.example.com/pricing"
> }
> ```
>
> Quick note: there's an IETF draft (draft-ietf-httpapi-ratelimit-headers) standardizing these as `RateLimit-Policy` and `RateLimit` headers without the `X-` prefix, but it's still a draft — not yet an RFC. Stripe, GitHub, and Cloudflare each use slightly different header conventions today. I'll use the `X-RateLimit-*` convention since it's the most widely adopted."

**Interviewer:** "Good. You covered the hot path, the control plane, and the client contract. I appreciate the deployment model discussion — sidecar vs embedded vs dedicated service. Let's move to the architecture."

---

### L5 vs L6 vs L7 — Phase 3 Rubric

| Aspect | L5 (SDE-2) | L6 (SDE-3) | L7 (Principal) |
|---|---|---|---|
| **API Design** | Defines a check endpoint and 429 response | Distinguishes data plane from control plane, shows request/response JSON, discusses atomicity of check-and-decrement | Additionally discusses API versioning, backward compatibility, idempotency of rule operations |
| **Deployment Models** | Says "middleware" or "separate service" | Compares three models (dedicated, embedded, sidecar) with latency trade-offs, references Envoy | Discusses how the choice affects operational concerns — deploys, rollbacks, independent scaling, blast radius |
| **Headers** | Mentions 429 status code | Lists X-RateLimit-* headers with Retry-After, mentions IETF draft | Discusses client-side adaptive throttling — how well-behaved clients use these headers to self-throttle (Google Cloud SDK approach) |

---

## PHASE 4: High-Level Architecture — Iterative Build-up (~20 min)

**Candidate:**

> "I'll build this up iteratively, starting from the simplest possible design and evolving it as we find problems."

---

#### Attempt 0: Single Server with In-Memory Counter

> "The simplest rate limiter: one application server with a `HashMap<String, Counter>`.
>
> ```
> ┌─────────────────────────────────────────────┐
> │              Application Server              │
> │                                               │
> │  Request → HashMap<clientId, counter>         │
> │            if counter < limit → allow          │
> │            else → reject (429)                 │
> │            reset counter every 60s             │
> │                                               │
> └─────────────────────────────────────────────┘
> ```
>
> This is a fixed window counter — divide time into 1-minute windows, increment per request, reject if over limit.
>
> **What's wrong with this?**
> 1. **No distribution** — We have N application servers behind a load balancer. Each server has its own HashMap. A client hitting different servers gets N × limit. With 10 servers and a limit of 100/min, the client can send 1,000 requests/min.
> 2. **Counters lost on restart** — Server restart or deployment resets all counters. Clients get a burst of free requests.
> 3. **Fixed window boundary burst** — A client can send 100 requests at 0:59 and 100 at 1:00 — 200 requests in 2 seconds, but both windows show ≤100. This is the fundamental weakness of fixed window counters. Twitter/X uses 15-minute fixed windows for their API rate limiting — the boundary burst problem exists but is acceptable at that window size.
> 4. **Single-dimension** — Only per-client limiting. No per-endpoint, per-tier, or global limits. A client can send all 100 requests to one expensive endpoint.
> 5. **No visibility** — No metrics, no dashboards, no way to answer 'why was my request rejected?'"

**Interviewer:** "Right. The distribution problem is the fundamental one. How do you solve it?"

---

#### Attempt 1: Centralized Redis + Token Bucket Algorithm

> "Move counters out of the application server into a shared Redis instance. All servers read/write the same counters.
>
> ```
> ┌──────────┐  ┌──────────┐  ┌──────────┐
> │  App     │  │  App     │  │  App     │
> │  Server  │  │  Server  │  │  Server  │
> │  1       │  │  2       │  │  N       │
> └────┬─────┘  └────┬─────┘  └────┬─────┘
>      │             │             │
>      └──────┬──────┴──────┬──────┘
>             │             │
>      ┌──────▼─────────────▼──────┐
>      │         Redis              │
>      │  Key: user:abc123          │
>      │  tokens: 42                │
>      │  last_refill: 1672531200   │
>      └────────────────────────────┘
> ```
>
> **Why token bucket?** It's the most widely used algorithm for API rate limiting because it naturally handles bursty traffic. Real-world API traffic is bursty — a mobile app might batch 5 requests at once, then go quiet. Token bucket allows controlled bursts (spend accumulated tokens) while enforcing a sustained rate. This is exactly what AWS API Gateway uses — their documentation explicitly states token bucket with `rate` (tokens/second) and `burst` (bucket capacity) parameters. Stripe also uses token bucket for their request rate limiter, backed by Redis.
>
> **Implementation — Redis Lua script for atomic token bucket:**
>
> ```
> -- Token Bucket (atomic Lua script)
> local key = KEYS[1]
> local rate = tonumber(ARGV[1])       -- tokens per second
> local burst = tonumber(ARGV[2])      -- max bucket capacity
> local now = tonumber(ARGV[3])        -- current timestamp (ms)
> local requested = tonumber(ARGV[4])  -- tokens to consume (usually 1)
>
> local data = redis.call('HMGET', key, 'tokens', 'last_refill')
> local tokens = tonumber(data[1]) or burst
> local last_refill = tonumber(data[2]) or now
>
> -- Refill tokens based on elapsed time
> local elapsed = (now - last_refill) / 1000
> tokens = math.min(burst, tokens + elapsed * rate)
>
> local allowed = tokens >= requested
> if allowed then
>     tokens = tokens - requested
> end
>
> redis.call('HMSET', key, 'tokens', tokens, 'last_refill', now)
> redis.call('EXPIRE', key, burst / rate * 2)  -- TTL: 2x time to fill bucket
>
> return {allowed and 1 or 0, math.floor(tokens)}
> ```
>
> This is atomic — Redis executes Lua scripts without interleaving other commands. No race condition possible.
>
> **Response headers:**
> - `X-RateLimit-Limit`: the burst capacity
> - `X-RateLimit-Remaining`: current tokens left
> - `X-RateLimit-Reset`: timestamp when bucket will be full again
> - `429 Too Many Requests` with `Retry-After` when tokens = 0
>
> **Why not the alternatives?**
> - **Leaky bucket**: Smooths output (no bursts) — better for traffic shaping than API rate limiting. NGINX's `limit_req` module uses leaky bucket, and Shopify uses it for their REST API (40-request bucket, 2 req/s leak rate). But API clients want fast responses, not queuing. Token bucket serves requests immediately if tokens are available.
> - **Fixed window**: Boundary burst problem (as we saw in Attempt 0).
> - **Sliding window log**: Stores every request timestamp in a Redis sorted set — O(N) memory per client. At 10K req/min × 10M clients, that's 100 billion entries. Prohibitively expensive.
> - **Sliding window counter**: Great accuracy with O(1) memory — Cloudflare uses this (their blog measured <0.003% error rate across 400M requests). I'll keep this as an alternative algorithm we support.
>
> **What's still broken?**
> 1. **Single Redis = SPOF** — If Redis goes down, the rate limiter fails. Do we fail open (allow all, risk overload) or fail closed (reject all, self-inflicted outage)?
> 2. **Single-dimension only** — Still just per-client limiting. No per-endpoint or per-tier.
> 3. **Hardcoded rules** — Limits are in application config. Changing them requires a deployment.
> 4. **No shadow mode** — Deploying a new rule immediately affects traffic. Could accidentally block a major customer."

**Interviewer:** "Good progression. The token bucket choice is well-justified. How do you address the Redis SPOF and rule management?"

---

#### Architecture Evolution After Attempt 1

| Component | Attempt 0 | Attempt 1 |
|---|---|---|
| **Counter Store** | In-memory HashMap (per-server) | ~~In-memory~~ → Centralized Redis (shared) |
| **Algorithm** | Fixed window counter | ~~Fixed window~~ → Token bucket (burst-friendly) |
| **Atomicity** | In-process (trivial) | Redis Lua script (atomic) |
| **Distribution** | None (N × limit problem) | ~~None~~ → Solved (shared counters) |
| **Dimensions** | Per-client only | Per-client only (still) |
| **Rules** | Hardcoded | Hardcoded (still) |

---

#### Attempt 2: Redis Cluster + Multi-Dimension Rules + Rule Management API

> "Three problems to solve: Redis HA, multi-dimension limiting, and dynamic rule management.
>
> ```
> ┌─────────────────────────────────────────────────────────────────┐
> │                        Control Plane                            │
> │                                                                 │
> │  ┌──────────────┐    ┌──────────────┐    ┌──────────────┐      │
> │  │  Rules API   │───▶│  PostgreSQL  │    │  Kafka /     │      │
> │  │  (CRUD)      │    │  (rules DB)  │───▶│  Redis PubSub│      │
> │  └──────────────┘    └──────────────┘    └──────┬───────┘      │
> │                                                  │              │
> └──────────────────────────────────────────────────┼──────────────┘
>                                                    │ push rule changes
> ┌──────────────────────────────────────────────────┼──────────────┐
> │                        Data Plane                 │              │
> │                                                   ▼              │
> │  ┌──────────┐  ┌──────────┐  ┌──────────┐  ┌──────────┐       │
> │  │  App     │  │  App     │  │  App     │  │  Local   │       │
> │  │  Server  │  │  Server  │  │  Server  │  │  Rules   │       │
> │  │  1       │  │  2       │  │  N       │  │  Cache   │       │
> │  └────┬─────┘  └────┬─────┘  └────┬─────┘  └──────────┘       │
> │       │             │             │                             │
> │       └──────┬──────┴──────┬──────┘                             │
> │              │             │                                    │
> │       ┌──────▼─────────────▼──────┐                             │
> │       │     Redis Cluster          │                             │
> │       │  (3+ nodes, auto-failover)│                             │
> │       │  Sharded by rate limit key│                             │
> │       └────────────────────────────┘                             │
> └─────────────────────────────────────────────────────────────────┘
> ```
>
> **Redis Cluster for HA:**
> A single Redis handles ~100K ops/sec. At 1M+ checks/sec with 2-4 Redis calls per check, we need horizontal scaling. Redis Cluster shards keys across nodes and provides automatic failover. We shard by the rate limit key (`clientId:resource:dimension`).
>
> **Fail-open on Redis failure:** If Redis becomes unavailable, we allow all requests and fire an **immediate alert**. Why fail open? Because a brief period without rate limiting is less damaging than blocking all traffic — our customers would experience a total outage. Stripe fails open for this reason. The one exception: **security-critical limits** like login attempt throttling should fail closed — it's better to block logins temporarily than to allow brute-force attacks.
>
> **Multi-dimension rate limiting:**
> A single request is evaluated against MULTIPLE rules simultaneously:
>
> ```
> Request: POST /api/v1/orders by user U1 from IP 203.0.113.42 (pro tier)
>
> Rule 1: Per-user limit    → user U1 ≤ 1,000 req/min (all APIs)     ✓ PASS
> Rule 2: Per-endpoint limit → POST /orders ≤ 10 req/min (per user)  ✓ PASS
> Rule 3: Global API limit  → POST /orders ≤ 5,000 req/min (all users) ✓ PASS
> Rule 4: Per-IP limit      → this IP ≤ 500 req/min                  ✗ FAIL
>
> Decision: REJECT (Rule 4 exceeded)
> Response indicates WHICH limit was exceeded → Rule 4, IP-based
> ```
>
> Each dimension is a separate Redis key. For this request, we make 4 Redis calls (pipelined — single round-trip, ~1ms total). ALL dimensions must pass. The rejection response tells the client exactly which limit was exceeded.
>
> **Rules engine:**
> Rules are stored in PostgreSQL (source of truth) and cached locally on each rate limiter node. Rule changes are pushed via Kafka or Redis Pub/Sub for near-real-time propagation, with periodic polling as a safety net (in case a push message is lost).
>
> **Shadow mode:** New rules start in `log_only` mode — the rule is evaluated and metrics are recorded, but requests are NOT rejected. Operators observe which clients would be affected before flipping to `enforce` mode. This prevents the 'we deployed a rate limit and accidentally blocked our biggest customer' disaster.
>
> **What's still broken?**
> 1. **No edge protection** — All traffic reaches our API gateway before rate limiting kicks in. A DDoS attack overwhelms the gateway before we can rate-limit. We need defense-in-depth — rate limiting at the edge.
> 2. **No internal service protection** — Microservice A can overwhelm microservice B. We rate-limit external clients but not internal callers.
> 3. **Limited observability** — We can't easily answer 'why was this specific request rejected?' or 'how close is client X to their limit?'"

**Interviewer:** "Solid. Multi-dimension with pipelined Redis is the right approach. Shadow mode shows operational maturity. Now, how do you handle DDoS and the observability gap?"

---

#### Architecture Evolution After Attempt 2

| Component | Attempt 1 | Attempt 2 |
|---|---|---|
| **Counter Store** | Single Redis | ~~Single Redis~~ → **Redis Cluster** (HA, sharded) |
| **Failure Mode** | Undefined | **Fail open** (with alerting). Fail closed for security limits. |
| **Dimensions** | Per-client only | ~~Single~~ → **Multi-dimension** (user + endpoint + IP + global) |
| **Rules** | Hardcoded | ~~Hardcoded~~ → **Dynamic** (PostgreSQL + push propagation + local cache) |
| **Shadow Mode** | None | **log_only mode** for new rules |
| **Algorithm** | Token bucket | Token bucket + **sliding window counter** (configurable per rule) |

---

### L5 vs L6 vs L7 — Phase 4 (Attempts 0-2) Rubric

| Aspect | L5 (SDE-2) | L6 (SDE-3) | L7 (Principal) |
|---|---|---|---|
| **Iterative Build** | Jumps to "Redis + token bucket" as first answer | Starts from single-server HashMap, identifies specific problems at each step, evolves naturally | Additionally shows back-of-envelope math at each step to justify whether the current design handles the scale |
| **Redis Design** | Says "use Redis" without details | Discusses atomic Lua scripts, pipelined multi-key lookups, Redis Cluster sharding, fail-open with alerting | Discusses Redis memory pressure, eviction policies, read replicas for rule cache vs write-primary for counters |
| **Algorithm Choice** | Picks one algorithm without justification | Compares token bucket vs sliding window with real-world examples (AWS, Cloudflare, Stripe), justifies choice | Discusses when to use which algorithm — token bucket for bursty API traffic, sliding window counter for strict compliance limits |
| **Rules Engine** | Limits are hardcoded | Dynamic rules in PostgreSQL, push propagation, shadow mode | Discusses rule evaluation complexity (O(rules) per request), indexing strategies, rule conflict resolution |
| **Multi-Dimension** | Single dimension (per-client) | Multi-dimension with pipelined Redis, explains which limit was exceeded | Discusses cost-based limiting (GitHub's point system) where different API calls cost different amounts |

---

#### Attempt 3: Multi-Layer Rate Limiting + Observability

> "The key insight: a single-layer rate limiter at the API gateway is necessary but insufficient. A DDoS attack overwhelms the gateway before rate limiting kicks in. Internal services can overwhelm each other. Business-logic limits (login attempts) can't be enforced at the gateway. We need multiple layers, each handling a different threat model.
>
> ```
> Internet Traffic
>     │
>     ▼
> ┌─── Layer 1: Edge / CDN ──────────────────────────────────────┐
> │ Cloudflare / AWS WAF / CloudFront                            │
> │ Block: DDoS, bot traffic, IP-based rate limiting             │
> │ Signals: IP, request rate, geo, TLS fingerprint (JA3)        │
> │ Latency added: ~0ms (evaluated at edge)                      │
> │ Limitation: No application context (can't distinguish users  │
> │ behind same NAT/VPN)                                         │
> └────────────────────────┬─────────────────────────────────────┘
>                          │
>                          ▼
> ┌─── Layer 2: API Gateway ─────────────────────────────────────┐
> │ Our Rate Limiter (from Attempt 2)                            │
> │ Authenticate → identify user/tier → check Redis counters     │
> │ Multi-dimension: per-user, per-endpoint, per-IP, per-tier    │
> │ Latency added: ~1-2ms (Redis round-trip)                     │
> │ Limitation: Per-request only. Can't enforce "1GB data/day"   │
> │ (requires inspecting response sizes post-processing)         │
> └────────────────────────┬─────────────────────────────────────┘
>                          │
>                          ▼
> ┌─── Layer 3: Application ─────────────────────────────────────┐
> │ Business-logic-aware rate limiting (in-process middleware)    │
> │ "Max 10 password attempts per account per hour"              │
> │ "Max 5 file uploads per user per day"                        │
> │ Signals: full application context — request body, DB state   │
> │ Latency added: ~0.1ms (in-process) to ~1ms (sidecar)        │
> └────────────────────────┬─────────────────────────────────────┘
>                          │
>                          ▼
> ┌─── Layer 4: Internal Services ───────────────────────────────┐
> │ Per-service quotas (noisy neighbor protection)               │
> │ 'Service A → Service B: max 5,000 req/s'                    │
> │ Enforced via service mesh (Istio/Envoy sidecar) with mTLS   │
> │ Signals: calling service identity                            │
> └──────────────────────────────────────────────────────────────┘
> ```
>
> **Why multiple layers?** Each layer operates on different signals and handles a different threat:
> - Layer 1 (Edge) is a **blunt instrument** — effective against volumetric DDoS but can't enforce per-user quotas (no application context).
> - Layer 2 (Gateway) is a **scalpel** — precise per-user limits but can't handle DDoS (traffic already reached our servers).
> - Layer 3 (Application) handles **business logic** — limits that require understanding the request (login attempts, complex queries).
> - Layer 4 (Internal) prevents **noisy neighbors** — one microservice overwhelming another.
>
> No single layer is sufficient. A DDoS bypasses Layer 2. A credential-stuffing attack bypasses Layer 1 (many unique IPs). An internal service bug bypasses Layers 1-3. Defense in depth.
>
> **Observability stack:**
>
> | Metric | What it measures | Alert threshold |
> |---|---|---|
> | **Rejection rate** | % of requests returning 429 (by client, resource, rule) | >5% for any client or resource |
> | **Decision latency** | P50/P95/P99 of rate limit check | P99 > 5ms |
> | **Fail-open events** | Rate limiter bypassed due to Redis unavailability | Any occurrence (immediate alert) |
> | **Counter store health** | Redis latency, connection pool, memory, replication lag | P99 Redis latency > 2ms |
> | **Quota utilization** | How close each client is to their limit | >90% sustained (suggest tier upgrade) |
> | **Rule changes** | Audit trail of every rule create/update/delete | Every change (audit log) |
>
> **Three dashboards:**
> 1. **Operations** — Real-time allowed vs rejected, by layer and rule. 'Is the rate limiter healthy?'
> 2. **Client** — Per-client quota usage over time. For support: 'Why am I getting 429s?'
> 3. **Debug** — Given a request ID, show every rule evaluated, counter values, and the allow/reject decision. Must answer 'why was this request rejected?' in <30 seconds.
>
> **What's still broken?**
> 1. **Single-region** — Redis Cluster is in one datacenter. Cross-region requests incur 50-200ms latency for rate limit checks. Not viable for a global API.
> 2. **Static limits** — If the backend is overloaded (high latency, errors), our rate limits don't adapt. We keep allowing traffic at the configured rate even though the backend can't handle it.
> 3. **All requests cost the same** — A simple GET and a complex database query consume the same quota. A user making expensive queries gets the same allowance as one making cheap reads."

**Interviewer:** "Multi-layer is exactly right. The observability piece — especially the debug dashboard — shows you've operated these systems. How do you extend to multi-region?"

---

#### Architecture Evolution After Attempt 3

| Component | Attempt 2 | Attempt 3 |
|---|---|---|
| **Layers** | Gateway only | ~~Single layer~~ → **4 layers** (edge + gateway + app + internal) |
| **Edge Protection** | None | **Cloudflare/WAF** for DDoS, IP-based limits |
| **Internal Limiting** | None | **Service mesh** (Envoy sidecar) for noisy neighbor protection |
| **Observability** | Basic | **Full stack**: rejection dashboards, debug endpoint, fail-open alerts, audit trail |
| **Region** | Single | Single (still) |
| **Adaptiveness** | Static limits | Static (still) |

---

#### Attempt 4: Multi-Region + Adaptive Rate Limiting + Cost-Based Limiting

> "Three final capabilities to make this production-grade at global scale.
>
> **Multi-region rate limiting:**
>
> ```
>                    ┌──────────────────────┐
>                    │   Global Aggregator   │
>                    │ (async, every 10-30s) │
>                    └──────┬──────┬────────┘
>                           │      │
>              ┌────────────┘      └────────────┐
>              ▼                                 ▼
>   ┌──────────────────┐              ┌──────────────────┐
>   │   US-East Region  │              │   EU-West Region  │
>   │                    │              │                    │
>   │  Redis Cluster     │              │  Redis Cluster     │
>   │  (60% of quota)    │              │  (30% of quota)    │
>   │                    │              │  ──── async ────── │
>   │  App Servers       │              │  App Servers       │
>   └──────────────────┘              └──────────────────┘
>                                               │
>                                    ┌──────────┘
>                                    ▼
>                         ┌──────────────────┐
>                         │  AP-Southeast     │
>                         │  Redis Cluster    │
>                         │  (10% of quota)   │
>                         └──────────────────┘
> ```
>
> A single Redis cluster can't serve all regions — cross-region latency is 50-200ms, destroying our <1ms P99 requirement. Options:
>
> 1. **Per-region rate limiting** — Each region enforces limits independently. Simple but a client spraying requests across regions gets N × limit. Acceptable for most use cases where clients stick to one region.
>
> 2. **Split quotas** (what I recommend) — Global limit divided across regions based on traffic share. US gets 60%, EU gets 30%, AP gets 10%. Periodically rebalance based on actual traffic. A global aggregator asynchronously detects cross-region abuse. This is close to what Stripe does — per-region enforcement with a global safety net.
>
> 3. **Global rate limiting with async replication** — Each region writes to local Redis, periodically syncs to a global view. Approximate but catches cross-region abuse.
>
> I'd start with option 2 (split quotas) as it's the best balance of accuracy and latency.
>
> **Adaptive rate limiting:**
> Static limits don't respond to backend health. If our orders service is degrading (P99 latency spiking), we should automatically reduce rate limits to protect it. Implementation:
>
> ```
> health_factor = target_p99 / actual_p99  (e.g., 100ms / 500ms = 0.2)
> effective_limit = base_limit × health_factor  (e.g., 1000 × 0.2 = 200)
> ```
>
> When the backend recovers, limits gradually restore (dampened — don't spike limits back to 100% instantly). This is similar to Google Cloud's adaptive throttling in their client libraries — the client tracks its rejection rate and backs off proactively. But here we're doing it server-side.
>
> **Cost-based rate limiting:**
> Not all requests are equal. GitHub's GraphQL API handles this elegantly with a **point-based system** — each query is assigned a cost based on estimated database load. A simple query costs 1 point; a complex query traversing many nodes can cost 100+ points. Rate limit is 5,000 points/hour (not 5,000 requests). The cost calculation looks at `first`/`last` arguments in GraphQL connections to estimate sub-requests, divides by 100, and rounds up.
>
> For our system:
> ```
> GET  /api/v1/users/{id}     → cost: 1 point  (simple read)
> POST /api/v1/orders          → cost: 5 points (write + validation)
> GET  /api/v1/reports/complex → cost: 20 points (heavy DB query)
>
> Rate limit: 1,000 points/minute (not 1,000 requests)
> ```
>
> This is fairer — a client making cheap reads gets more requests than one hammering expensive endpoints. Trade-off: we need to assign and maintain cost values for every endpoint, which adds operational complexity.
>
> **Final Architecture:**
>
> ```
> ┌─────────────────────────────────────────────────────────────────────┐
> │                         Control Plane                               │
> │  ┌──────────┐  ┌──────────┐  ┌──────────┐  ┌──────────────────┐   │
> │  │ Rules API│  │PostgreSQL│  │ Kafka    │  │ Global Aggregator│   │
> │  │ (CRUD)   │─▶│(rules DB)│─▶│(rule push│  │ (cross-region    │   │
> │  └──────────┘  └──────────┘  │+ events) │  │  abuse detection) │   │
> │                               └────┬─────┘  └──────────────────┘   │
> └────────────────────────────────────┼───────────────────────────────┘
>                                      │
> ┌────────────────────────────────────┼───────────────────────────────┐
> │  Layer 1: Edge / CDN               │                               │
> │  Cloudflare/WAF: DDoS, IP limits   │                               │
> └────────────────────────────────────┼───────────────────────────────┘
>                                      │
> ┌────────────────────────────────────┼───────────────────────────────┐
> │  Layer 2: API Gateway + Rate Limiter                               │
> │                                    ▼                               │
> │  ┌──────────┐  ┌──────────┐  ┌──────────┐                        │
> │  │  App 1   │  │  App 2   │  │  App N   │  Local rules cache     │
> │  └────┬─────┘  └────┬─────┘  └────┬─────┘  + health monitor      │
> │       └──────┬──────┴──────┬──────┘                               │
> │              ▼             ▼                                       │
> │       ┌────────────────────────┐                                   │
> │       │ Redis Cluster (region) │                                   │
> │       │ Token bucket + sliding │                                   │
> │       │ window counters        │                                   │
> │       └────────────────────────┘                                   │
> └────────────────────────────────────────────────────────────────────┘
>                                      │
> ┌────────────────────────────────────┼───────────────────────────────┐
> │  Layer 3: Application               │                               │
> │  Business-logic limits (in-process) │                               │
> └────────────────────────────────────┼───────────────────────────────┘
>                                      │
> ┌────────────────────────────────────┼───────────────────────────────┐
> │  Layer 4: Internal (Envoy sidecar)  │                               │
> │  Per-service quotas, mTLS identity  │                               │
> └─────────────────────────────────────────────────────────────────────┘
>
> Observability: Prometheus + Grafana dashboards, PagerDuty alerts,
>               structured logs, per-request debug endpoint
> ```"

**Interviewer:** "Excellent. The split-quota approach for multi-region is pragmatic. The adaptive rate limiting is a nice touch — most candidates don't think about that. Let's deep dive into a few areas."

---

#### Architecture Evolution After Attempt 4

| Component | Attempt 3 | Attempt 4 |
|---|---|---|
| **Region** | Single | ~~Single~~ → **Multi-region** (split quotas + global aggregator) |
| **Adaptiveness** | Static limits | ~~Static~~ → **Adaptive** (backend health → dynamic limits) |
| **Cost Model** | All requests equal | ~~Equal~~ → **Cost-based** (point system per endpoint) |
| **Cross-Region Abuse** | Not detected | **Global aggregator** with async detection |

---

### L5 vs L6 vs L7 — Phase 4 (Attempts 3-4) Rubric

| Aspect | L5 (SDE-2) | L6 (SDE-3) | L7 (Principal) |
|---|---|---|---|
| **Multi-Layer** | Mentions "use a CDN for DDoS" | Designs 4 specific layers with distinct threat models, explains why each layer exists and what it can't do | Discusses layer interaction — what happens when Layer 1 misconfigures a rule and blocks legitimate traffic? Blast radius per layer. |
| **Multi-Region** | Says "deploy Redis in each region" | Split quotas with traffic-based allocation, global aggregator for cross-region abuse | Discusses consistency trade-offs per region — US (source of truth) vs EU (eventual follower), quota rebalancing frequency, failover behavior |
| **Adaptive** | Doesn't mention | Describes health-factor-based limit adjustment with dampening | Discusses feedback loops — if adaptive limiting reduces limits, backend recovers, limits increase, backend overloads again. How to prevent oscillation (PID controller analogy). |
| **Cost-Based** | Doesn't mention | References GitHub's point system, proposes per-endpoint cost assignment | Discusses how to auto-compute cost (instrument endpoint latency/DB queries, derive cost from observed resource consumption) |

---

## PHASE 5: Deep Dive — Rate Limiting Algorithms (~5 min)

**Interviewer:** "Let's dig into algorithms. You mentioned token bucket and sliding window counter. Walk me through the trade-offs more concretely."

**Candidate:**

> "Sure. Here's how I think about algorithm selection:
>
> | Algorithm | Memory per key | Accuracy | Burst handling | Best for |
> |---|---|---|---|---|
> | **Token Bucket** | O(1) — 2 values | Exact for rate | Allows controlled bursts | API rate limiting (Stripe, AWS API Gateway) |
> | **Leaky Bucket** | O(1) — 2 values | Exact for rate | No bursts — smooths output | Traffic shaping (NGINX, Shopify REST API) |
> | **Fixed Window** | O(1) — 1 counter | Boundary burst problem | No burst control | Simple internal tools (Twitter/X's 15-min windows) |
> | **Sliding Window Log** | O(N) per client | Perfectly exact | No burst control | Low-volume, high-precision (audit/compliance) |
> | **Sliding Window Counter** | O(1) — 2 counters | ~99.997% accurate | No burst control | High-scale production (Cloudflare) |
>
> **Token bucket is our default** because API traffic is bursty and clients expect fast responses. A mobile app might fire 5 requests at app launch — token bucket serves them immediately from accumulated tokens.
>
> **Sliding window counter is our alternative** for strict compliance limits where 'max N per window' must be exact (no bursts allowed). Cloudflare's implementation is proven at massive scale — <0.003% error rate across 400 million requests.
>
> The sliding window counter formula is simple:
> ```
> estimated_count = previous_window_count × overlap_percentage + current_window_count
>
> Example: window = 1 min, limit = 100
> At time 1:15 (15s into current window):
>   Previous window (0:00-1:00): 84 requests
>   Current window (1:00-2:00): 36 requests so far
>   Overlap of previous window: (60-15)/60 = 75%
>   Estimated count = 84 × 0.75 + 36 = 99
>   → One more request would hit 100 → reject
> ```
>
> **Why not sliding window log in production?** At Stripe/GitHub scale (10K+ req/min per client × 10M clients), storing every timestamp would require O(100 billion) entries. A sorted set with ZADD + ZCARD per request is too slow and too memory-intensive. The counter approximation solves this.
>
> One more distinction worth calling out: **Shopify's GraphQL API** uses a cost-based leaky bucket — same leaky bucket mechanism but each query consumes a variable number of points based on query complexity, not just 1 point per request. It's the same idea as GitHub's point system but with a different underlying algorithm."

**Interviewer:** "Good. The Cloudflare stat about 0.003% error rate is compelling — that's practically exact for all purposes."

---

### L5 vs L6 vs L7 — Phase 5 Rubric

| Aspect | L5 (SDE-2) | L6 (SDE-3) | L7 (Principal) |
|---|---|---|---|
| **Algorithm Knowledge** | Describes 1-2 algorithms at surface level | Compares 5 algorithms with memory/accuracy/burst trade-offs, cites real systems | Additionally discusses hybrid approaches — token bucket for burst control + sliding window for window-based billing |
| **Quantitative** | Says "sliding window is more accurate" | Cites Cloudflare's <0.003% error rate, shows the weighted average formula with a concrete example | Derives the error bound mathematically — worst case is when all traffic concentrates at window boundaries |
| **Real Systems** | Says "Redis is common" | Maps algorithms to specific companies (AWS→token bucket, Cloudflare→sliding window, NGINX→leaky bucket, Shopify→leaky bucket) with verified facts | Discusses why each company chose their algorithm — Stripe's bursty API traffic vs NGINX's traffic-shaping use case |

---

## PHASE 6: Deep Dive — Distributed Coordination (~5 min)

**Interviewer:** "You mentioned the distributed coordination problem is what makes this hard. Walk me through the approaches and trade-offs."

**Candidate:**

> "Right. A single-node rate limiter is trivially correct — atomic in-process increment. The moment you have N servers, you need shared state. Four approaches, each with different trade-offs:
>
> **Approach 1: Centralized counter store (Redis)** — Our primary approach.
> All nodes read/write counters from Redis. Atomic INCR or Lua scripts prevent race conditions. ~0.5-1ms per Redis roundtrip (same datacenter). This is what Stripe uses — Redis-backed counters with Lua scripts for atomicity.
>
> The critical race condition to avoid: a naive implementation does `GET counter → check if < limit → SET counter + 1`. Between GET and SET, another request increments the counter → both pass when only one should. Solution: never use GET-then-SET. Use atomic `INCR` and check the returned value, or a Lua script that atomically reads, checks, and increments.
>
> **Approach 2: Local counters + periodic sync** — For approximate use cases.
> Each node maintains in-memory counters. Sync to a global aggregator every 1-5 seconds. Between syncs, the count is approximate. With N=10 nodes and sync interval T=1s, up to N × rate × T = 10 × 100 × 1 = 1,000 requests could pass when only 100 should.
>
> Mitigation: give each node a local quota = global_limit / N. This is Google Doorman's approach — a hierarchical system where clients request capacity leases from a central server. The server distributes quotas using ProportionalShare or FairShare algorithms. Clients enforce locally. No per-request central call. Trade-off: faster (no per-request network hop) but requires cooperative clients and a rebalancing mechanism.
>
> **Approach 3: Sticky routing** — Route all requests from a client to the same node via consistent hashing on client ID. Single writer per key — no coordination needed. But: node failure resets counters, and heavy clients cause load imbalance. Some CDN edge rate limiters use this within a PoP.
>
> **Approach 4: Distributed consensus (Raft/Paxos)** — Strongly consistent counters across nodes. But consensus rounds add 10-100ms latency — far too slow for per-request rate limiting. This is why Redis (single-writer, fast) is preferred over consensus (multi-writer, slow) for this use case. I mention it because interviewers sometimes ask 'why not use Raft?' — the answer is latency.
>
> **The right answer for most systems:**
>
> | Use Case | Approach | Why |
> |---|---|---|
> | Per-client API limits (Stripe, GitHub) | Centralized Redis | Accuracy matters. 1ms overhead is acceptable. |
> | Global traffic shaping (protect a backend) | Local + periodic sync | Approximate is fine. No per-request network hop. |
> | Edge/CDN rate limiting | Sticky routing | Simple. Approximate during failover is OK. |
> | Never | Consensus | Too slow for per-request checking. |"

**Interviewer:** "Good. You clearly articulated why consensus doesn't work here and when local counters are acceptable. The Doorman reference is solid."

---

### L5 vs L6 vs L7 — Phase 6 Rubric

| Aspect | L5 (SDE-2) | L6 (SDE-3) | L7 (Principal) |
|---|---|---|---|
| **Race Conditions** | Says "use Redis" | Explains the GET-then-SET race condition with a concrete example, shows why atomic INCR/Lua solves it | Discusses ABA problems, Redis pipeline ordering, and what happens when a Lua script takes too long (blocks Redis) |
| **Distributed Approaches** | Mentions centralized store | Compares 4 approaches (Redis, local+sync, sticky, consensus) with specific latency numbers and trade-off table | Discusses the CAP theorem implications — rate limiting needs availability (AP) not strong consistency (CP), which is why Redis (AP with single-writer) beats Raft (CP) |
| **Real Systems** | Says "companies use Redis" | References Stripe (Redis), Google (Doorman), CDN (sticky routing), explains why each chose their approach | Discusses hybrid approaches — Redis for critical clients, local counters for non-critical, with different SLAs per tier |

---

## PHASE 7: Deep Dive — Failure Modes & Operational Concerns (~5 min)

**Interviewer:** "What keeps you up at night with this system? What are the operational risks?"

**Candidate:**

> "Several things worry me:
>
> **1. Redis failure — fail open vs fail closed.**
> When Redis is unavailable, we fail open (allow all requests). This means: during Redis downtime, rate limiting is disabled. If a DDoS attack coincides with a Redis failure, we have no protection. Mitigations:
> - Redis Cluster with automatic failover (typical failover: <30 seconds)
> - Local in-memory fallback with degraded accuracy during Redis outage
> - Immediate PagerDuty alert on any fail-open event
> - For security-critical limits (login attempts, password resets): fail CLOSED — better to block logins for 30 seconds than allow brute force
>
> **2. Misconfigured rules.**
> An operator accidentally sets a global limit of 10 req/min instead of 10,000 req/min. This blocks essentially all traffic. Shadow mode mitigates new rules, but updates to existing rules are dangerous. Mitigations:
> - Rule change requires approval (two-person rule for production changes)
> - Automatic rollback if rejection rate spikes >10x within 5 minutes of a rule change
> - Rate limit on the rule management API itself (meta-rate-limiting)
>
> **3. Hot keys in Redis.**
> One extremely popular client (or an attack) causes one Redis key to receive millions of operations per second. Even with Redis Cluster, one key maps to one shard. Mitigations:
> - Key sharding: split a hot counter into N sub-counters (`key:0`, `key:1`, ..., `key:N-1`), aggregate on read. Trades accuracy for throughput.
> - Local caching with periodic sync for the hottest keys
>
> **4. Clock skew across nodes.**
> Token bucket uses timestamps for refill calculation. If server clocks drift, refill rates are wrong. Mitigation: always use the Redis server's clock (via `redis.call('TIME')`), not the application server's clock.
>
> **5. Rule propagation lag.**
> An operator deploys a new rule, but some nodes haven't received it yet. Requests hitting those nodes bypass the new rule. Mitigation: push + periodic polling as described. Accept that there's a ~1-5 second window where rules are inconsistent across nodes. For critical rule changes, a 'force sync' endpoint that triggers immediate propagation.
>
> **6. Cascading failure from rate limiting.**
> Rate limiting causes clients to retry aggressively → retry storm → more load → more rate limiting → more retries. Mitigation: the `Retry-After` header tells clients when to retry. Well-behaved clients (like Google Cloud SDK with adaptive throttling) respect this. But buggy clients ignore it. Solution: exponential backoff in the rate limit response — `Retry-After` increases with each subsequent rejection for the same client."

**Interviewer:** "The hot key problem is one most candidates miss. The meta-concern about retry storms is excellent — rate limiting can become a source of instability if clients don't back off properly."

---

### L5 vs L6 vs L7 — Phase 7 Rubric

| Aspect | L5 (SDE-2) | L6 (SDE-3) | L7 (Principal) |
|---|---|---|---|
| **Failure Modes** | Says "Redis can go down, we should have backups" | Distinguishes fail-open vs fail-closed by use case (API vs security), discusses automatic rollback on misconfigured rules | Discusses blast radius — if Redis Region-A fails, only Region-A rate limiting degrades. Quantifies: "30-second failover window × 1M req/s = 30M unmetered requests" |
| **Operational** | Mentions "monitoring" | Designs specific alerts (fail-open, rejection spike, latency), shadow mode, rule approval workflows | Discusses operational runbooks — what does the on-call do when each alert fires? Automation vs human judgment. |
| **Hot Keys** | Doesn't mention | Proposes sub-counter sharding with accuracy trade-off | Discusses adaptive sharding — automatically detect hot keys via monitoring and split them, with a recombination strategy when traffic normalizes |
| **Retry Storms** | Doesn't mention | Explains the cascading failure pattern, proposes Retry-After with escalation | Discusses server-side jitter in Retry-After to prevent thundering herd (all retries hitting at the same time) |

---

## PHASE 8: Wrap-Up & Summary (~3 min)

**Interviewer:** "We're running low on time. Summarize your design and tell me what you'd prioritize for a V1 launch."

**Candidate:**

> "**Design evolution — how we got here:**
>
> | Attempt | What We Built | Key Problem Solved |
> |---|---|---|
> | **0** | Single server + HashMap + fixed window | Baseline (everything is wrong) |
> | **1** | Centralized Redis + token bucket + headers | Distribution (shared counters) and burst handling |
> | **2** | Redis Cluster + multi-dimension + rules engine + shadow mode | HA, per-endpoint/per-tier limits, dynamic configuration |
> | **3** | Multi-layer (edge + gateway + app + internal) + observability | DDoS protection, business-logic limits, debugging |
> | **4** | Multi-region + adaptive + cost-based | Global scale, backend protection, fairness |
>
> **V1 launch priority (what I'd ship first):**
> 1. Attempt 2 — Redis Cluster with multi-dimension rules and shadow mode. This handles 90% of use cases.
> 2. Observability (from Attempt 3) — rejection dashboards, fail-open alerts, debug endpoint. Without observability, the rate limiter is a liability.
> 3. Edge layer (from Attempt 3) — DDoS protection via Cloudflare/WAF. This is configuration, not engineering.
>
> **V2 (after V1 is stable):** Multi-region, adaptive limiting, cost-based limiting.
>
> **What keeps me up at night:**
>
> 1. **Redis as a critical dependency** — Every API request now depends on Redis. We've turned a stateless API into a stateful one. Redis must be as reliable as the API itself — which means Redis Cluster with replication, monitoring, and a tested failover runbook.
>
> 2. **Rate limiting the wrong people** — A misconfigured rule that blocks legitimate customers is worse than no rate limiting at all. Shadow mode, approval workflows, and automatic rollback are essential safety nets.
>
> 3. **Retry storms** — Rate limiting can create the instability it's meant to prevent. If clients don't respect `Retry-After`, rejections cause retries, which cause more rejections. We need server-side protections (escalating Retry-After, jittered backoff) because we can't control client behavior.
>
> 4. **The gap between 'rate limited' and 'protected'** — Rate limiting prevents individual clients from overwhelming the system, but it doesn't protect against aggregate overload (all clients within limits, but combined load exceeds capacity). That's where load shedding and adaptive rate limiting come in — complementary mechanisms, not replacements."

**Interviewer:** "Strong answer. You built iteratively from a HashMap to a multi-region distributed system, made clear trade-offs at each step, and showed operational awareness. The distinction between rate limiting, load shedding, and circuit breaking throughout was particularly well done."

---

## Interviewer's Final Assessment

**Hire Recommendation: Strong Hire (L6 — solid Senior SDE with demonstrated system design maturity)**

| Dimension | Rating | Notes |
|---|---|---|
| **Problem Decomposition** | Exceeds Bar | Clean separation of data plane vs control plane, 4-layer defense model, iterative build from simple to complex |
| **Requirements & Scoping** | Exceeds Bar | Quantified scale (10B req/day → 1M checks/sec), sized Redis memory, distinguished rate limiting from adjacent concepts |
| **Technical Depth** | Meets Bar (Strong) | Solid algorithm comparison with real-system references. Token bucket Lua script. Multi-dimension with pipelined Redis. |
| **Distributed Systems** | Exceeds Bar | Clearly articulated why consensus is wrong, compared 4 approaches with trade-off table, explained race conditions and hot keys |
| **Operational Awareness** | Exceeds Bar | Fail-open vs fail-closed by use case, shadow mode, retry storms, automatic rollback, debug dashboards |
| **Communication** | Exceeds Bar | Iterative build-up felt natural. Proactively identified trade-offs before asked. |

---

## Key Differences: L5 vs L6 vs L7 Expectations for This Problem

| Aspect | L5 (SDE-2) | L6 (SDE-3) | L7 (Principal) |
|---|---|---|---|
| **Requirements** | Lists "check rate and return 429" | Quantifies scale, distinguishes rate limiting from load shedding/circuit breaking, drives scoping | Frames requirements in business context — "1ms overhead acceptable because it's <2% of API P99" |
| **Algorithm** | Describes one algorithm | Compares 5 algorithms with trade-offs, maps to real systems, justifies choice | Derives accuracy bounds mathematically, discusses hybrid algorithm strategies |
| **Architecture** | Jumps to "Redis + rate limit middleware" | Iterative build from HashMap to multi-layer multi-region, with clear problem→solution at each step | Quantifies capacity at each step, discusses blast radius of failures at each layer |
| **Distributed** | Says "use Redis" | Explains 4 approaches with latency/accuracy trade-offs, discusses race conditions and hot keys | Discusses CAP implications, hybrid approaches per client tier, quota rebalancing algorithms |
| **Operations** | Mentions monitoring | Designs specific alerts, shadow mode, fail-open/closed by use case, retry storm prevention | Discusses runbooks, automatic rollback, capacity planning, SLA implications of rate limiter unavailability |
| **Breadth** | Covers core rate limiting | Covers multi-layer, multi-dimension, multi-algorithm, dynamic rules, observability | Additionally covers adaptive limiting, cost-based limiting, cross-region coordination, organizational processes |

---

*For detailed deep dives on each component, see the companion documents:*
- [API Contracts](02-api-contracts.md) — Rate limiter API surfaces, headers, and client contracts
- [Rate Limiting Algorithms](03-rate-limiting-algorithms.md) — Token bucket, sliding window, leaky bucket — implementations and trade-offs
- [Distributed Rate Limiting](04-distributed-rate-limiting.md) — Centralized Redis, local counters, sticky routing, multi-region coordination
- [Rules Engine](05-rules-engine.md) — Rule structure, multi-dimension evaluation, shadow mode, propagation
- [Multi-Layer Rate Limiting](06-multi-layer-rate-limiting.md) — Edge, gateway, application, and internal service layers
- [Monitoring & Observability](07-monitoring-and-observability.md) — Metrics, alerts, dashboards, and debugging
- [Design Trade-offs](08-design-trade-offs.md) — Opinionated analysis of key design decisions
