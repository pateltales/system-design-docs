Design a Rate Limiter as a system design interview simulation.

## Template
Follow the EXACT same format as the S3 interview simulation at:
src/hld/aws/s3/design/interview-simulation.md

Same structure:
- Interviewer/Candidate dialogue format
- PHASE 1: Opening & Problem Statement
- PHASE 2: Requirements Gathering & Scoping (functional + non-functional + scale numbers)
- PHASE 3: API Design
- PHASE 4: High-Level Architecture (iterative build-up: Attempt 0 → 1 → 2 → ..., starting from single server, finding problems, evolving)
- PHASE 5+: Deep dives on each component
- L5/L6/L7 rubric table after EACH deep dive phase — showing exactly how an L5, L6, and L7 candidate would answer differently. This is critical for understanding what "good" looks like at each level.
- Architecture evolution table after each phase
- Wrap-up phase with "what keeps you up at night"
- Links to supporting deep-dive docs

## Candidate Level
**L6 SDE-3 (Senior SDE)** candidate being interviewed by a Principal Engineer (L8).

The candidate's answers should demonstrate L6-level depth:
- Not just "what" but "why" and "why not the alternative"
- Quantitative reasoning (back-of-envelope math, concrete numbers)
- Awareness of operational concerns (monitoring, failure modes, blast radius)
- Proactive identification of tradeoffs before the interviewer asks

The L5/L6/L7 rubric tables after each phase should make it crystal clear how an L5 answer differs from L6 differs from L7.

## Output location
Create all files under: src/hld/ratelimiter/design/

## Files to create

### 1. 01-interview-simulation.md — the main backbone
The full interview dialogue covering all phases and attempts.

### 2. 02-api-contracts.md — Rate Limiter APIs & Client-Facing Contracts

This doc should list all the API surfaces of a rate limiting system — both the internal management APIs and the external-facing HTTP contracts (headers, response codes) that clients interact with. The interview simulation (Phase 3) will only cover a subset — this doc is the comprehensive reference.

**Structure**: For each API group, list every endpoint with HTTP method, path, request/response shape, and a brief description. Mark endpoints covered in the interview with a star or highlight.

**API groups to cover**:

- **Rate Limit Check APIs (hot path)**: The most performance-critical path. `POST /ratelimit/check` (given a client identifier + resource key, return allow/deny decision + remaining quota + reset time). This is called on EVERY incoming request — latency must be <1ms at P99 to avoid becoming the bottleneck. The check-and-decrement must be **atomic** — two concurrent requests must not both pass when only one slot remains. Alternative: the rate limiter is embedded as middleware (no separate HTTP call — in-process check against a shared counter store). The embedded approach avoids network hop latency but couples the rate limiter to the application. A sidecar proxy approach (Envoy, Istio) provides the best of both: no application code changes, minimal latency (localhost network hop), and centralized policy management.

- **Rate Limit Response Headers**: The standard HTTP headers that communicate rate limit state to clients. These are critical for well-behaved clients that self-throttle:
  - `X-RateLimit-Limit`: Maximum requests allowed in the current window (e.g., `1000`)
  - `X-RateLimit-Remaining`: Requests remaining in the current window (e.g., `742`)
  - `X-RateLimit-Reset`: Unix timestamp when the window resets (e.g., `1672531200`)
  - `Retry-After`: Seconds until the client should retry (included in `429` responses)
  - Response code: `429 Too Many Requests` with a JSON body explaining which limit was exceeded and when to retry
  - **Draft IETF standard (RateLimit header fields)**: The IETF is standardizing rate limit headers as `RateLimit-Limit`, `RateLimit-Remaining`, `RateLimit-Reset` (without the `X-` prefix). Stripe, GitHub, and Cloudflare each use slightly different header conventions. The lack of a universal standard means every API client must handle provider-specific header formats.

- **Rate Limit Rule Management APIs (control plane)**: `POST /rules` (create a new rate limit rule — specify: resource key pattern, limit, window size, algorithm, client tier, action on limit exceeded). `GET /rules` (list all rules, filterable by resource/tier). `GET /rules/{ruleId}` (get a specific rule). `PUT /rules/{ruleId}` (update a rule — changes propagate within seconds to all rate limiter nodes). `DELETE /rules/{ruleId}` (delete a rule). Rules define: **who** is rate-limited (by user ID, API key, IP address, or combination), **what** is rate-limited (specific API endpoint, endpoint group, global), **how much** (requests per second/minute/hour/day), **what algorithm** (token bucket, sliding window, etc.), and **what happens** when the limit is exceeded (reject with 429, queue, degrade to lower priority, log-only/shadow mode).

- **Client Tier / Quota Management APIs**: `GET /clients/{clientId}/quota` (current usage vs limits for a client). `PUT /clients/{clientId}/tier` (change a client's rate limit tier — e.g., free → pro → enterprise). `GET /clients/{clientId}/usage` (historical usage over time — useful for dashboards and billing). Different tiers get different limits: free tier (100 req/min), pro tier (1,000 req/min), enterprise tier (10,000 req/min, custom negotiated). Tier changes must propagate to all rate limiter nodes in near-real-time.

- **Override / Exemption APIs**: `POST /overrides` (create a temporary override — exempt a specific client from rate limiting during a migration, or increase limits for a specific event). `GET /overrides` (list active overrides). `DELETE /overrides/{overrideId}` (remove an override). Overrides are time-bound (auto-expire) and audited (who created them, why, when they expire). This is critical for operational flexibility — during incident response, you may need to bypass rate limits for internal services or temporarily increase limits for a partner.

- **Analytics / Monitoring APIs**: `GET /metrics/throughput` (current request throughput by resource, client, tier). `GET /metrics/rejections` (rate of 429 responses — by resource, client, reason). `GET /metrics/latency` (rate limiter decision latency — P50, P95, P99). `GET /metrics/top-clients` (clients consuming the most quota — useful for identifying abuse or need for tier upgrade). These power dashboards for operations teams to monitor rate limiter health and identify abusive or misconfigured clients.

- **Health / Ops APIs**: `GET /health` (rate limiter health — connectivity to counter store, rule cache freshness, decision latency). `POST /config/reload` (force reload rules from the rules store — used after a rule change that needs immediate propagation). `GET /config/active` (show currently active rules and their sources — useful for debugging "why was my request rejected?").

**Contrast with API Gateway rate limiting (Kong, AWS API Gateway)**:
- API Gateways provide rate limiting as one feature among many (auth, routing, transformation, logging). A dedicated rate limiter provides deeper capabilities: multi-dimension limiting (simultaneous limits on user + IP + API + global), more algorithms (sliding window counter, token bucket with burst), dynamic rules without redeployment, and fine-grained analytics.
- AWS API Gateway: Configurable per-stage throttling (requests/second + burst). Uses token bucket internally. Simple to configure but limited: no per-user limits without custom authorizer logic, no sliding window, no multi-dimension limiting. Adequate for basic API protection, insufficient for a multi-tenant platform like Stripe or GitHub.
- Kong: Flexible rate limiting plugin. Supports Redis-backed distributed counters. More configurable than AWS API Gateway but still tied to the gateway's request lifecycle — can't rate-limit non-HTTP workloads (queue consumers, batch jobs).

**Contrast with CDN/Edge rate limiting (Cloudflare)**:
- Cloudflare's rate limiting operates at the edge (before traffic reaches origin servers). Excellent for DDoS protection and bot mitigation. But edge rate limiting operates on HTTP-level signals (IP, URL path, headers) — it doesn't have application-level context (user ID, API key, subscription tier). A full rate limiting solution needs BOTH edge-level (block abusive IPs before they reach the application) and application-level (enforce per-user/per-tier quotas with business context).

**Contrast with client-side throttling**:
- Well-behaved clients implement client-side throttling using rate limit response headers. Google Cloud client libraries implement **adaptive throttling** — the client tracks its own rejection rate and proactively backs off before the server rejects. This reduces server-side load during overload. But client-side throttling is cooperative — malicious or buggy clients ignore it entirely. Server-side rate limiting is the enforcement mechanism; client-side throttling is an optimization.

**Interview subset**: In the interview (Phase 3), focus on: the rate limit check API (the hot path — atomic check-and-decrement, latency requirements), response headers/status codes (the client contract), and rule management (how operators configure limits). The full API list lives in this doc.

### 3. 03-rate-limiting-algorithms.md — Rate Limiting Algorithms Deep Dive

The algorithm is the heart of the rate limiter. Each algorithm makes different trade-offs between accuracy, memory, and implementation complexity.

- **Token Bucket**:
  - Concept: A bucket holds tokens. Each request consumes one token. Tokens are refilled at a constant rate. If the bucket is empty, the request is rejected. The bucket has a maximum capacity (burst size).
  - Parameters: `rate` (tokens added per second), `burst` (maximum bucket capacity). A bucket with rate=10, burst=50 means: sustained rate of 10 req/s, but can burst up to 50 requests if the bucket has accumulated tokens.
  - Implementation: Store two values per key — `tokens` (current count) and `last_refill_timestamp`. On each request: calculate elapsed time since last refill → add `elapsed × rate` tokens (capped at burst) → if tokens >= 1, decrement and allow; else reject. This is a single atomic operation in Redis (Lua script).
  - Pros: Allows bursts (good for real-world traffic patterns that are bursty, not uniform). Memory-efficient (2 values per key). Simple to implement. Well-understood.
  - Cons: The burst parameter is an extra knob to tune. A large burst allows a flood of requests that may overwhelm downstream services even though the average rate is within limits.
  - Used by: **AWS API Gateway** (token bucket is their documented algorithm), **Stripe** (token bucket with per-second and per-day limits), **NGINX** (`limit_req` module uses leaky bucket but configurable burst works like token bucket).
  - [VERIFIED — AWS API Gateway documentation explicitly states token bucket; Stripe engineering blog describes their rate limiter]

- **Leaky Bucket (as a queue)**:
  - Concept: Requests enter a FIFO queue (the bucket). The queue is drained at a fixed rate. If the queue is full, new requests are rejected. Output rate is perfectly smooth — no bursts.
  - Parameters: `rate` (outflow rate), `capacity` (queue size).
  - Implementation: Can be implemented as a queue with a fixed-rate consumer. In practice, for rate limiting (not traffic shaping), the leaky bucket is often implemented identically to token bucket but without allowing bursts above the rate.
  - Pros: Produces perfectly smooth output — ideal for traffic shaping where downstream services need a uniform request rate. Predictable processing rate.
  - Cons: Bursty traffic fills the queue → older requests may become stale while waiting. No burst tolerance — legitimate traffic spikes are delayed rather than served immediately. Queue management adds memory overhead.
  - Used by: **NGINX** (`limit_req` module is a leaky bucket implementation), **Shopify** (leaky bucket for their API rate limiting).
  - **Contrast with Token Bucket**: Token bucket allows bursts (spend accumulated tokens); leaky bucket smooths everything. Token bucket is better for APIs (users want fast responses, not queuing). Leaky bucket is better for traffic shaping (even out bursty traffic before it hits a backend). Most API rate limiters use token bucket; most traffic shapers use leaky bucket.

- **Fixed Window Counter**:
  - Concept: Divide time into fixed windows (e.g., 1-minute windows). Each window has a counter. Increment the counter per request. If counter > limit, reject.
  - Parameters: `window_size` (e.g., 60 seconds), `limit` (max requests per window).
  - Implementation: Redis key = `resource:client:window_number` (window_number = `timestamp / window_size`). `INCR` the key, set TTL = window_size. If count > limit, reject. Two Redis commands per request (INCR + conditional EXPIRE), combinable into one Lua script.
  - Pros: Extremely simple. Memory-efficient (one counter per key per window). Easy to understand and debug.
  - Cons: **Boundary burst problem**: A client can send `limit` requests at the end of window N and `limit` requests at the start of window N+1 — effectively 2× the rate in a short period spanning the boundary. Example: limit = 100/min. Client sends 100 requests at 0:59 and 100 requests at 1:00 → 200 requests in 2 seconds, but both windows show ≤100. This is the fundamental weakness of fixed window.
  - Used by: Many simple implementations, internal tools where exact precision isn't critical.
  - **Contrast with Sliding Window**: Fixed window is simpler but has the boundary burst problem. Sliding window (below) solves this at the cost of more state or computation.

- **Sliding Window Log**:
  - Concept: Store the timestamp of every request in a sorted set. To check the limit: count timestamps in the set that fall within `[now - window_size, now]`. If count >= limit, reject. Remove timestamps older than the window.
  - Parameters: `window_size`, `limit`.
  - Implementation: Redis sorted set. Key = `resource:client`. Score = timestamp. `ZADD` for each request. `ZREMRANGEBYSCORE` to evict old entries. `ZCARD` to count entries in the current window.
  - Pros: **Perfectly accurate** — no boundary burst problem, no approximation. The sliding window moves continuously with each request.
  - Cons: **Memory-intensive** — stores one entry per request. For a client making 10,000 req/min, that's 10,000 entries per key. At scale with millions of clients, this is prohibitive. Also, `ZCARD` on large sorted sets adds latency.
  - Used by: Rarely in production at scale due to memory cost. Useful for low-volume, high-precision use cases (audit logging, compliance).
  - **Contrast with Sliding Window Counter**: The log is exact but expensive. The counter (below) approximates but uses constant memory.

- **Sliding Window Counter** (the sweet spot):
  - Concept: Combine fixed window counters with a weighted average to approximate a sliding window. Maintain counters for the current and previous windows. The estimated count = `previous_window_count × overlap_percentage + current_window_count`. If estimated count >= limit, reject.
  - Example: Window = 1 min, limit = 100. At time 1:15 (15 seconds into the current minute): previous window (0:00-1:00) had 84 requests, current window (1:00-2:00) has 36 requests so far. Overlap of previous window = (60-15)/60 = 75%. Estimated count = 84 × 0.75 + 36 = 99. One more request would hit 100 → reject.
  - Parameters: `window_size`, `limit`.
  - Implementation: Two Redis counters per key (current window, previous window). Same constant memory as fixed window. Slightly more computation (one multiplication + addition). Can be done in a single Lua script.
  - Pros: Solves the boundary burst problem (approximately — not perfectly, but close enough). Constant memory (like fixed window). Simple implementation.
  - Cons: Not perfectly accurate — it's an approximation. The estimate can be off by a few percent when traffic distribution within a window is uneven. Cloudflare measured the error rate at **<0.003%** in practice — negligible for rate limiting purposes.
  - Used by: **Cloudflare** (their rate limiting uses sliding window counter). **Redis** documentation recommends this approach. Most production-grade rate limiters at scale use this algorithm.
  - [PARTIALLY VERIFIED — Cloudflare blog discusses sliding window approach; exact error rate figure needs verification]
  - **This is the recommended algorithm for most production systems** — best balance of accuracy, memory efficiency, and implementation simplicity.

- **Algorithm comparison table** (include in the doc):
  | Algorithm | Memory per key | Accuracy | Burst handling | Complexity | Best for |
  |---|---|---|---|---|---|
  | Token Bucket | O(1) — 2 values | Exact for rate | Allows controlled bursts | Low | API rate limiting with burst tolerance |
  | Leaky Bucket | O(1) — 2 values | Exact for rate | No bursts — smooths output | Low | Traffic shaping, uniform output rate |
  | Fixed Window | O(1) — 1 counter | Boundary burst problem | No burst control | Very low | Simple internal tools |
  | Sliding Window Log | O(N) — N = requests | Perfectly exact | No burst control | Medium | Low-volume, high-precision |
  | Sliding Window Counter | O(1) — 2 counters | ~99.997% accurate | No burst control | Low | Production API rate limiting at scale |

- **Contrast with circuit breakers (Hystrix, Resilience4j)**: Rate limiters control the rate of INCOMING requests (protecting the server from clients). Circuit breakers control OUTGOING requests (protecting the client from a failing server). They solve opposite problems but complement each other: a rate limiter on your API prevents overload; a circuit breaker on your downstream calls prevents cascading failures. Both are part of a defense-in-depth strategy, but they are architecturally distinct.

- **Contrast with load shedding**: Rate limiting applies quotas per client. Load shedding drops requests globally when the system is overloaded (regardless of which client sent them). Rate limiting is proactive (enforce limits before overload). Load shedding is reactive (drop requests during overload). A well-designed system has both: rate limiting prevents any single client from causing overload; load shedding handles the case where aggregate load exceeds capacity despite per-client limits.

### 4. 04-distributed-rate-limiting.md — Distributed Rate Limiting

The single hardest problem in rate limiter design: maintaining accurate counters across multiple rate limiter nodes without introducing unacceptable latency or inconsistency.

- **Why distributed is hard**: A single-node rate limiter is trivial — atomic increment on an in-process counter. But in a distributed system with N application servers (each running a rate limiter), a client's requests hit different servers. Without coordination, each server maintains its own counter → a client with a limit of 100 req/min could send 100 × N requests (100 per server). The counters must be shared.

- **Approach 1: Centralized counter store (Redis)**:
  - All rate limiter nodes read/write counters from a single Redis cluster.
  - Atomic operations: Redis `INCR` is atomic. A Lua script can atomically check-and-increment. No race conditions within a single Redis key.
  - Latency: ~0.5-1ms per Redis roundtrip (same-datacenter). This is added to every API request. For most APIs, 1ms overhead is acceptable.
  - **Race condition with GET-then-SET**: Naive implementations do `GET counter → check if < limit → SET counter+1`. Between GET and SET, another request may have incremented the counter → both pass. Solution: use `INCR` (atomic) or a Lua script that atomically reads, checks, and increments.
  - **Redis failure modes**: If Redis is unavailable, the rate limiter must decide: **fail open** (allow all requests — risk overload but maintain availability) or **fail closed** (reject all requests — protect backend but cause an outage). Most production systems **fail open** — it's better to temporarily lose rate limiting than to block all traffic. But failing open during a DDoS attack is dangerous. Best practice: fail open with aggressive monitoring and alerting so operators can intervene.
  - **Redis scaling**: A single Redis instance can handle ~100K operations/second. For a system doing 1M+ rate limit checks/second, Redis needs to be clustered and sharded. Shard by client ID or resource key. Use Redis Cluster or client-side consistent hashing.
  - [VERIFIED — Redis documentation describes atomic INCR and Lua scripting. Stripe engineering blog describes Redis-backed rate limiting.]

- **Approach 2: Local counter + periodic sync (approximate)**:
  - Each rate limiter node maintains local in-memory counters. Periodically (every 1-5 seconds), nodes sync their counts to a central store and pull the aggregated global count.
  - Pros: No per-request network hop. Rate limit check is in-memory (~microseconds). Network cost is amortized over the sync interval.
  - Cons: Between syncs, the global count is approximate. With N nodes and sync interval T, the maximum over-admission is `N × rate × T` requests above the limit. Example: 10 nodes, limit = 100/s, sync interval = 1s → up to 1,000 requests could pass (100 per node) when only 100 should. This is unacceptable for strict rate limiting.
  - Mitigation: Each node gets a **local quota** = `global_limit / N`. Node enforces its local quota independently. Periodically rebalance quotas based on actual traffic distribution across nodes. This is how **Google's rate limiter (Doorman)** works.
  - [PARTIALLY VERIFIED — Google published a paper on Doorman, a cooperative rate limiter using local quotas with central coordination]
  - Trade-off: accuracy vs latency. For strict per-user API limits (Stripe, GitHub), use centralized Redis. For approximate global traffic shaping (protect a backend from overload), local+sync is acceptable.

- **Approach 3: Sticky routing**:
  - Route all requests from a given client to the same rate limiter node (using consistent hashing on client ID). Each node is the sole owner of its clients' counters — no coordination needed.
  - Pros: No distributed coordination. Counters are exact (single writer per key).
  - Cons: Sticky routing breaks if a node fails (failover resets counters). Load imbalance (one heavy client monopolizes a node). Requires a load balancer that supports consistent hashing on client ID.
  - Used by: Some CDN edge rate limiters (Cloudflare uses sticky routing within a PoP).
  - Trade-off: simplicity vs resilience. Good for edge/CDN rate limiting where approximate limits during failover are acceptable. Not suitable for strict API rate limiting.

- **Approach 4: Distributed consensus (Raft/Paxos)**:
  - Use a consensus protocol to agree on counter values across nodes. Strongly consistent counters — no over-admission.
  - Pros: Perfect accuracy. No single point of failure.
  - Cons: Consensus rounds add latency (10-100ms per operation — far too slow for per-request rate limiting). Throughput is limited by the consensus leader.
  - This approach is NOT used for rate limiting in practice — the latency cost is too high. Mentioned here for completeness and to explain WHY Redis (single-writer, fast) is preferred over consensus (multi-writer, slow) for this use case.

- **The right answer for most systems**: Centralized Redis for strict per-client limits (the common case). Local counters with periodic sync for global/approximate limits (high-throughput edge rate limiting). Sticky routing for edge PoP rate limiting where simplicity matters. NEVER consensus — too slow.

- **Multi-datacenter rate limiting**: If your system runs in multiple regions (US-East, EU-West, AP-Southeast), a single Redis cluster can't serve all regions (cross-region latency = 50-200ms). Options:
  - **Per-region rate limiting**: Each region enforces limits independently. A client with limit 1000/min gets 1000/min in each region. If the client only uses one region, this is fine. If they spray requests across regions, they get N × limit. Acceptable for most use cases.
  - **Global rate limiting with async replication**: Each region writes to local Redis, periodically syncs to a global aggregator. Approximate but handles cross-region abuse.
  - **Split quotas**: Global limit divided across regions based on traffic share. Region US gets 60% of quota, EU gets 30%, AP gets 10%. Periodically rebalance based on actual traffic.
  - Stripe's approach: per-region rate limiting with a global safety net — if a client exceeds limits across all regions combined (detected asynchronously), flag for review.

- **Contrast with Google's Doorman**: Doorman is a cooperative rate limiter where clients request capacity from a central server. The central server allocates quotas based on total available capacity and per-client priority. Clients enforce their allocated quota locally. This inverts the typical model: instead of "check every request against a central counter," Doorman says "get a quota allocation, enforce it locally." Better for high-throughput systems (no per-request central call), but requires cooperative clients (malicious clients can ignore their quota).

- **Contrast with Envoy proxy rate limiting**: Envoy's rate limiting service (ratelimit) uses a Go-based gRPC service backed by Redis. The Envoy sidecar proxy calls the rate limit service for each request. This decouples rate limiting from the application but adds a network hop. Envoy's approach is well-suited for service mesh architectures (Istio) where all traffic flows through sidecars.

### 5. 05-rules-engine.md — Rules Engine & Configuration

The rules engine defines WHO is rate-limited, on WHAT resource, HOW MUCH, and WHAT HAPPENS when the limit is exceeded. A well-designed rules engine is the difference between a rate limiter that operators love and one that causes constant pain.

- **Rule structure**:
  ```
  {
    "id": "rule-123",
    "match": {
      "client_id": "*",           // or specific client ID, or regex
      "api_key_tier": "free",     // free | pro | enterprise | internal
      "resource": "/api/v1/users/*", // URL pattern or resource group
      "method": "GET",            // HTTP method or "*"
      "source_ip_cidr": "0.0.0.0/0" // optional IP range filter
    },
    "limit": {
      "requests": 100,
      "window": "1m",            // 1s, 1m, 1h, 1d
      "algorithm": "sliding_window_counter",
      "burst": 20                // for token bucket: max burst above sustained rate
    },
    "action": {
      "on_limit": "reject",      // reject (429) | queue | degrade | log_only
      "retry_after": "dynamic",  // fixed seconds or "dynamic" (based on window reset)
      "custom_response": {       // optional custom 429 response body
        "error": "rate_limit_exceeded",
        "message": "You have exceeded 100 requests per minute. Upgrade to Pro for higher limits.",
        "upgrade_url": "https://api.example.com/pricing"
      }
    },
    "priority": 10,              // lower = higher priority (evaluated first)
    "enabled": true,
    "created_at": "2026-01-15T10:00:00Z",
    "expires_at": null           // null = permanent, or timestamp for temporary rules
  }
  ```

- **Multi-dimension rate limiting**: A single request may be evaluated against MULTIPLE rules simultaneously. Example: a user makes a `POST /api/v1/orders` request. The rate limiter evaluates:
  1. Per-user limit: user U1 is limited to 100 req/min across all APIs
  2. Per-API limit: `/api/v1/orders` POST is limited to 10 req/min per user (write-heavy, protect the DB)
  3. Global API limit: `/api/v1/orders` POST is limited to 1,000 req/min across all users (protect the orders service)
  4. Per-IP limit: this IP is limited to 500 req/min (prevent credential stuffing from a single IP)
  ALL limits must pass for the request to be allowed. ANY limit exceeded → reject. The rejection response should indicate WHICH limit was exceeded.

- **Rule evaluation order and priority**: Rules are evaluated by priority (lowest number = highest priority). The first matching rule for each dimension is applied. Example: an enterprise client has a specific override rule (priority 1) that supersedes the default tier rule (priority 100). This allows fine-grained exceptions without modifying the base rules.

- **Shadow mode / log-only mode**: When deploying a new rate limit rule, start in **shadow mode** — the rule is evaluated and metrics are recorded, but requests are NOT rejected. This lets operators observe the rule's impact (how many requests WOULD be rejected, which clients are affected) before enforcing it. Shadow mode prevents "we deployed a rate limit and accidentally blocked our biggest customer" disasters. Once confident, flip to enforcement mode.

- **Rule propagation**: When a rule is created or updated via the management API, it must propagate to ALL rate limiter nodes within seconds. Options:
  - **Polling**: Each node polls the rules store every N seconds. Simple but delayed (up to N seconds stale). Used by most simple implementations.
  - **Push (Pub/Sub)**: Rule changes published to a Kafka topic or Redis Pub/Sub channel. Nodes subscribe and update immediately. Lower latency but more infrastructure.
  - **Config sync (etcd/ZooKeeper)**: Rules stored in a distributed config store. Nodes watch for changes. Strong consistency guarantees. More operational complexity.
  - Best practice: push for real-time propagation + periodic polling as a safety net (in case a push message is lost).

- **Contrast with AWS WAF rules**: AWS WAF provides rate-based rules at the edge (CloudFront/ALB). Rules are defined in JSON and evaluated before traffic reaches the application. WAF rules are limited to HTTP-level signals (IP, URL, headers, query params) — no application-level context (user ID, subscription tier). WAF rate limiting is a first line of defense; application-level rate limiting is the precision layer.

- **Contrast with Stripe's rate limiter configuration**: Stripe uses a multi-tier rate limiting system. Limits are defined per API endpoint AND per authentication method (API key vs OAuth). Stripe's rate limits are published in their API documentation and return standard headers. Stripe distinguishes between **test mode** and **live mode** limits (test mode is more permissive). Stripe's approach is a best-in-class reference for API rate limiting.

- **Contrast with GitHub's rate limiting**: GitHub defines rate limits per API version (REST vs GraphQL), per authentication method (unauthenticated vs token vs OAuth app), and per resource. GitHub's GraphQL API uses a **point-based system** — different queries cost different points based on complexity. This is more nuanced than simple request counting — a lightweight query costs 1 point, a complex query traversing many nodes costs 100+ points. This prevents a single expensive query from consuming the same quota as 100 cheap queries.

### 6. 06-multi-layer-rate-limiting.md — Multi-Layer Rate Limiting Architecture

A production rate limiting system operates at MULTIPLE layers. Each layer serves a different purpose. Defense in depth — no single layer handles everything.

- **Layer 1: Edge / CDN (Cloudflare, AWS CloudFront, Akamai)**:
  - Purpose: Block abusive traffic BEFORE it reaches your infrastructure. Volumetric DDoS protection, bot detection, IP-based rate limiting.
  - Signals: IP address, request rate per IP, geographic origin, known bad IP lists, HTTP fingerprinting (JA3 hash for TLS fingerprinting, header order analysis).
  - Limits: Coarse-grained. "No more than 1,000 requests/second from any single IP." "Block requests from known bot networks."
  - Latency added: ~0ms (evaluated at edge, before routing to origin).
  - Limitation: No application context — can't distinguish between user A and user B behind the same corporate NAT (same IP). Can't enforce per-user or per-API-key limits.
  - **Contrast with application-level**: Edge rate limiting is a blunt instrument — effective against volumetric attacks but useless for per-user API quota enforcement. Application-level rate limiting is a scalpel — precise per-user/per-endpoint limits but can't handle DDoS (the traffic has already reached your servers).

- **Layer 2: API Gateway / Load Balancer (Kong, NGINX, AWS API Gateway, Envoy)**:
  - Purpose: Enforce per-route and per-client rate limits AFTER authentication but BEFORE the request reaches application servers. This is where most API rate limiting lives.
  - Signals: Authenticated user ID, API key, request path, HTTP method, client tier (resolved after auth).
  - Limits: Medium-grained. "Free tier: 100 req/min per user. Pro tier: 1,000 req/min per user. This endpoint: 10 req/min per user."
  - Implementation: Gateway extracts the rate limit key (user ID + endpoint) from the authenticated request, checks against Redis counters, and returns 429 if exceeded.
  - Latency added: ~1-2ms (Redis call from the gateway).
  - Limitation: Gateway rate limiting is per-request. It can't enforce complex quotas (e.g., "max 1GB of data transfer per day" requires inspecting response sizes, which happens after the request is processed).

- **Layer 3: Application-level (in-process or sidecar)**:
  - Purpose: Fine-grained, business-logic-aware rate limiting. Enforce limits that require application context — e.g., "max 10 password attempts per account per hour" (requires knowing the account from the request body), or "max 5 file uploads per user per day" (requires knowing the upload count from the database).
  - Signals: Full application context — user identity, request payload, database state, business rules.
  - Limits: Fine-grained. Specific to business logic.
  - Implementation: Rate limit check embedded in the application code or enforced via a sidecar proxy (Envoy) with custom rate limit descriptors.
  - Latency added: ~0.1ms (in-process) to ~1ms (sidecar with Redis).
  - Limitation: Only applies to traffic that reaches the application. Can't protect against DDoS (Layer 1's job).

- **Layer 4: Per-service / internal rate limiting**:
  - Purpose: Protect internal services from noisy-neighbor problems in a microservices architecture. Service A should not be able to overwhelm Service B.
  - Signals: Calling service identity (mTLS certificate, service mesh identity), request path, rate.
  - Limits: Per-service quotas. "Service A can make 5,000 req/s to Service B. Service C can make 500 req/s to Service B."
  - Implementation: Service mesh (Istio/Envoy) rate limiting at the sidecar level, or in-process rate limiting in the service.
  - **Contrast with external API rate limiting**: External rate limiting protects the platform from clients. Internal rate limiting protects internal services from each other. The threat model is different — external limits prevent abuse; internal limits prevent accidental overload from a buggy or misconfigured service.

- **How layers work together**:
  ```
  Internet Traffic
      │
      ▼
  ┌─── Layer 1: Edge/CDN ─────────────────────────────┐
  │ Block DDoS, bot traffic, IP-based rate limiting    │
  │ Signals: IP, rate, geo, fingerprint                │
  │ Pass: legitimate-looking traffic                   │
  └────────────────────────┬───────────────────────────┘
                           │
                           ▼
  ┌─── Layer 2: API Gateway ───────────────────────────┐
  │ Authenticate → identify user/tier                  │
  │ Per-user, per-endpoint rate limiting               │
  │ Signals: user ID, API key, tier, endpoint          │
  │ Reject: 429 with RateLimit headers                 │
  └────────────────────────┬───────────────────────────┘
                           │
                           ▼
  ┌─── Layer 3: Application ───────────────────────────┐
  │ Business-logic-aware rate limiting                 │
  │ "Max 10 password attempts per account"             │
  │ "Max 5 file uploads per user per day"              │
  │ Signals: full application context                  │
  └────────────────────────┬───────────────────────────┘
                           │
                           ▼
  ┌─── Layer 4: Internal Services ─────────────────────┐
  │ Per-service quotas (noisy neighbor protection)     │
  │ "Service A → Service B: max 5,000 req/s"          │
  │ Signals: service identity (mTLS)                   │
  └────────────────────────────────────────────────────┘
  ```

- **Contrast with a single-layer rate limiter**: Most system design interview answers describe a single-layer rate limiter (API gateway with Redis). This is necessary but insufficient. A DDoS attack bypasses it (traffic overwhelms the gateway before rate limiting kicks in). Internal services can still overwhelm each other. Business-logic limits (password attempts) can't be enforced at the gateway. A complete answer describes multiple layers, each handling a different threat model.

### 7. 07-monitoring-and-observability.md — Monitoring, Alerting & Observability

A rate limiter that you can't observe is a liability. If you can't answer "why was this request rejected?" within 30 seconds, your rate limiter is hurting more than helping.

- **Key metrics to track**:
  - **Total requests**: Total request volume (allowed + rejected), broken down by client, resource, tier. This is the denominator for rejection rate.
  - **Rejection rate**: Percentage of requests returning 429. By client, by resource, by rule. A sudden spike in rejections may indicate: (a) a client bug (sending too many requests), (b) a misconfigured rule (too restrictive), or (c) a legitimate traffic increase (need to raise limits).
  - **Decision latency**: P50, P95, P99 latency of the rate limit check. If P99 exceeds ~5ms, the rate limiter is becoming a bottleneck. Common causes: Redis latency, Lua script complexity, network congestion.
  - **Counter store health**: Redis latency, connection pool utilization, memory usage, replication lag. If Redis is degrading, the rate limiter may fail open (allowing all requests) — you need to know this immediately.
  - **Rule evaluation time**: Time to match a request against rules. If you have thousands of rules, evaluation time can grow. Monitor and optimize rule matching (use indexing, not linear scan).
  - **Quota utilization per client**: How close each client is to their limit. Clients consistently at >90% utilization may need a tier upgrade. Clients consistently at <1% utilization may have stale credentials or abandoned integrations.

- **Alerting**:
  - **Rejection rate spike**: Alert if rejection rate exceeds threshold (e.g., >5% of requests for a specific client or resource). This catches both abusive clients and misconfigured rules.
  - **Rate limiter latency**: Alert if P99 decision latency exceeds 5ms. The rate limiter should be invisible to request latency — if it's slow, something is wrong.
  - **Fail-open events**: Alert IMMEDIATELY if the rate limiter fails open (Redis unavailable). During fail-open, you have no rate limiting — an attacker or buggy client could overload the system.
  - **Rule change audit**: Alert on every rule change (create, update, delete). A misconfigured rule can block legitimate traffic or allow abuse. Audit trail is critical for post-incident analysis.

- **Dashboards**:
  - **Operations dashboard**: Real-time view of allowed vs rejected requests, broken down by layer (edge, gateway, application). Shows which rules are firing, which clients are being throttled, and whether the rate limiter itself is healthy.
  - **Client dashboard**: Per-client view showing quota usage over time, rejection history, and current tier. Useful for customer support ("why am I getting 429s?") and sales ("this client needs a tier upgrade").
  - **Debug dashboard**: Given a specific request ID, show exactly which rules were evaluated, which counters were checked, and why the request was allowed or rejected. Critical for debugging false rejections.

- **Logging and audit trail**:
  - Log every rate limit decision: request ID, client ID, resource, rule matched, counter values, decision (allow/reject), timestamp.
  - For rejections, include additional context: which limit was exceeded, current count, limit value, window reset time. This makes debugging trivial.
  - Retention: keep detailed logs for 30 days. Keep aggregated metrics for 1+ year (trend analysis, capacity planning).

- **Contrast with API analytics platforms (Datadog, New Relic)**: API analytics platforms track overall API performance (latency, error rates, throughput). Rate limiter monitoring is more specific: it tracks the rate limiter's OWN health and decisions. The two are complementary — API analytics tells you "P99 latency spiked"; rate limiter monitoring tells you "because Redis went down and we failed open, allowing 10x normal traffic which overloaded the backend."

- **Contrast with WAF monitoring (AWS WAF, Cloudflare)**: WAF monitoring shows blocked requests at the edge (by IP, by rule, by attack type). Rate limiter monitoring shows throttled requests at the application level (by client, by tier, by API). WAF catches network-level threats; rate limiter catches application-level abuse.

### 8. 08-design-trade-offs.md — Design Philosophy & Trade-off Analysis

Opinionated analysis of rate limiter design choices — not just "what" but "why this and not that."

- **Token Bucket vs Sliding Window Counter**: Token bucket is the most popular algorithm (used by AWS, Stripe, many others) because it naturally handles bursty traffic — real-world API traffic is bursty, not uniform. Sliding window counter is more accurate (no burst loophole) but doesn't model burstiness. For API rate limiting, token bucket is usually the right choice. For strict compliance-driven limits ("max 1,000 API calls per day" — no bursts allowed), sliding window counter is better. Trade-off: user experience (burst-friendly) vs precision (strict enforcement).

- **Centralized (Redis) vs local counters**: Centralized Redis gives exact counts but adds 1ms latency per request and creates a SPOF (Redis down = fail open or fail closed, both bad). Local counters are faster (~microseconds) but approximate (each node tracks independently). For external API rate limiting (Stripe, GitHub), centralized Redis is standard — accuracy matters more than the 1ms cost. For internal traffic shaping (protect a backend from overload), local counters with periodic sync are sufficient — approximate enforcement is fine when the goal is "don't overwhelm the service" rather than "enforce exact per-client quotas."

- **Fail open vs fail closed**: When the counter store (Redis) is unavailable, should the rate limiter allow all requests (fail open) or reject all requests (fail closed)? Fail open maintains availability but loses protection — dangerous during an attack. Fail closed maintains protection but causes a self-inflicted outage — the rate limiter becomes the single point of failure. Most production systems fail open because availability is paramount — a brief period without rate limiting is less damaging than blocking all traffic. Stripe fails open. The trade-off changes for security-critical limits (login attempt limits should fail closed — better to block logins than allow brute force).

- **Per-request check vs quota allocation**: The standard model checks a central counter for every request. Google's Doorman model allocates quotas to nodes, which enforce locally. Per-request is simpler and more accurate. Quota allocation is faster (no per-request network hop) but requires a cooperative client and a quota rebalancing mechanism. Trade-off: simplicity vs throughput. For <100K rate limit checks/second, per-request Redis is fine. For >1M checks/second, quota allocation avoids the Redis bottleneck.

- **Single-dimension vs multi-dimension rate limiting**: Simple rate limiters enforce one limit per client (e.g., 1,000 req/min). Production rate limiters enforce multiple simultaneous limits: per-user, per-endpoint, per-IP, global. Multi-dimension is more complex (multiple Redis lookups per request, multiple rules to evaluate) but prevents abuse vectors that single-dimension misses (e.g., one user sending all their quota to a single expensive endpoint). Trade-off: implementation complexity vs protection completeness.

- **Hard limit vs soft limit (grace period)**: A hard limit rejects request N+1 immediately. A soft limit allows a small grace period (e.g., 10% over the limit for 10 seconds) before rejecting. Soft limits are friendlier for clients experiencing brief spikes. Hard limits are simpler and more predictable. Trade-off: client experience vs enforcement simplicity. Stripe uses hard limits with clear documentation. Some internal rate limiters use soft limits to avoid false rejections on bursty internal traffic.

- **Rate limiting as middleware vs dedicated service**: Embedding rate limiting in the application (as middleware) avoids network hops but couples the rate limiter to the application's lifecycle and language. A dedicated rate limiting service (like Envoy's ratelimit) decouples policy from application code but adds latency. A sidecar proxy (Envoy in service mesh) is the middle ground — co-located with the application (low latency), separate process (decoupled lifecycle). Trade-off: latency vs operational independence. For monoliths, middleware is fine. For microservices, a dedicated service or sidecar is better.

- **Fixed rules vs adaptive rate limiting**: Fixed rules (100 req/min per user, period) are simple and predictable. Adaptive rate limiting adjusts limits based on real-time system health (if backend latency increases → lower the rate limit dynamically). Adaptive is more resilient to unexpected load patterns but harder to reason about ("why was I rejected? the limit changed while I was calling"). Trade-off: operational simplicity vs resilience. Google Cloud uses adaptive throttling in client libraries.

- **Build vs buy**: Building a rate limiter from scratch gives full control but requires engineering investment. Using a managed service (AWS API Gateway throttling, Cloudflare rate limiting, Kong plugin) is faster to deploy but limits customization. For most companies, API gateway rate limiting is sufficient. For companies with complex multi-tenant APIs (Stripe, Twilio, GitHub), a custom rate limiter is justified — the nuances of multi-dimension limiting, custom tier management, and detailed analytics require it.

## CRITICAL: The design must be focused on the RATE LIMITER as a system

This is not "add rate limiting to an API" — it's "design a rate limiting system." The rate limiter is the product. Treat it as a first-class distributed system with its own:
- API surface (management + enforcement)
- Storage layer (counter stores, rule stores)
- Algorithm selection (token bucket, sliding window, etc.)
- Distributed coordination problem (accurate counts across nodes)
- Multi-layer deployment model (edge → gateway → application → internal)
- Observability stack (metrics, alerts, dashboards, debugging)
- Operational concerns (fail-open/fail-closed, rule propagation, dynamic configuration)

## CRITICAL: Iterative build-up (DO NOT skip this)
The architecture MUST evolve iteratively. Each attempt builds on the previous one by identifying a concrete problem and solving it. The candidate should NOT jump to the final architecture. The progression should feel natural:

### Attempt 0: Single server with in-memory counter
- Application server with a HashMap<clientId, counter>. On each request: increment counter, check against limit, reject if exceeded. Counter resets every minute (fixed window).
- **Problems found**: No distribution — each server has its own counter, so a client hitting N servers gets N × limit. In-memory counters lost on restart. Fixed window has the boundary burst problem. No per-endpoint or per-tier limits — one-size-fits-all. No visibility (no metrics, no dashboards).

### Attempt 1: Centralized Redis + Token Bucket algorithm
- Move counters to Redis. All application servers check/increment the same Redis counters. Use token bucket algorithm (atomic Lua script in Redis). Configurable rate and burst per client.
- Standard response headers: `X-RateLimit-Limit`, `X-RateLimit-Remaining`, `X-RateLimit-Reset`. Return `429 Too Many Requests` when limit exceeded.
- **Contrast with in-memory**: Redis is shared across all servers — accurate global count. Redis is persistent (RDB/AOF) — counters survive restarts. Redis `INCR` is atomic — no race conditions.
- **Problems found**: Single Redis instance is a SPOF — if Redis goes down, rate limiting fails. Only one dimension (per-client) — no per-endpoint or per-tier limits. Rules are hardcoded in application config — changing limits requires a deployment. No shadow mode — deploying a new rule immediately affects traffic.

### Attempt 2: Redis Cluster + multi-dimension rules + rule management API
- Redis Cluster for HA and horizontal scaling. Shard counters by rate limit key.
- Multi-dimension rate limiting: per-user, per-endpoint, per-IP evaluated simultaneously. All must pass.
- Rules engine: rules stored in a database, cached locally on rate limiter nodes. Management API for CRUD operations on rules. Rules include: match criteria, limit, algorithm, action, priority.
- Shadow mode: new rules deployed in log-only mode first.
- Fail-open on Redis failure (with alerting).
- **Contrast with single-dimension**: Multi-dimension catches abuse patterns that single-dimension misses (e.g., user sending all quota to one expensive endpoint).
- **Problems found**: All rate limiting happens at the API gateway level — no edge protection (DDoS traffic reaches the gateway and overwhelms it before rate limiting kicks in). No internal service-to-service rate limiting (noisy neighbor problems). Monitoring is basic — hard to debug "why was my request rejected?"

### Attempt 3: Multi-layer rate limiting + observability
- **Layer 1 (Edge)**: Cloudflare/AWS WAF for IP-based rate limiting and DDoS protection. Blocks volumetric attacks before they reach infrastructure.
- **Layer 2 (API Gateway)**: Per-user, per-endpoint rate limiting with Redis (from Attempt 2).
- **Layer 3 (Application)**: Business-logic-aware limits (login attempts, file uploads) enforced in application code.
- **Layer 4 (Internal)**: Per-service rate limiting via service mesh (Envoy sidecar) to prevent noisy neighbor problems.
- Observability: rejection rate dashboards, per-client quota utilization, decision latency metrics, fail-open alerting, rule change audit logs. Debug endpoint: "given request X, show exactly which rules were evaluated and why it was allowed/rejected."
- **Contrast with single-layer**: Defense in depth. Each layer handles a different threat model. No single layer is sufficient alone.
- **Problems found**: Single-region Redis means cross-region requests incur high latency for rate limit checks. No adaptive rate limiting — limits are static even when the backend is overloaded. Client tiers are static — no dynamic adjustment based on usage patterns.

### Attempt 4: Multi-region + adaptive rate limiting + advanced features
- **Multi-region rate limiting**: Per-region Redis clusters with split quotas. Global limit divided across regions based on traffic share. Async aggregation detects cross-region abuse.
- **Adaptive rate limiting**: Dynamic limit adjustment based on backend health. If backend P99 latency spikes → automatically reduce rate limits to protect the system. When backend recovers → restore normal limits. Configurable sensitivity and dampening.
- **Client tier management**: Dynamic tier assignment based on usage patterns. Automated "upgrade suggestion" when a client consistently hits limits. Self-service tier management portal.
- **Rate limit by cost**: Not all requests are equal. A complex database query costs more than a simple read. Assign "cost" to each request type (like GitHub's GraphQL point system). Rate limit by total cost per window, not just request count.
- **Contrast with static rate limiting**: Adaptive rate limiting responds to real-time system health. Static rate limiting enforces fixed limits regardless of system state. Adaptive is more resilient but harder to reason about.
- **Contrast with GitHub's point-based system**: GitHub's GraphQL API charges different "points" for different queries based on complexity. This is more fair than flat request counting — a simple query shouldn't consume the same quota as a complex one. Trade-off: implementation complexity (need to compute cost per request) vs fairness.

## CRITICAL: Accuracy requirements

I DO NOT TRUST AI-GENERATED FACTS ABOUT SPECIFIC SYSTEMS. Every concrete claim about specific rate limiter implementations must be verifiable against official sources. Specifically:

1. **Use WebSearch and WebFetch tools** to look up engineering blogs and official documentation BEFORE writing. Search for:
   - "Stripe rate limiter architecture engineering blog"
   - "Cloudflare rate limiting sliding window"
   - "GitHub API rate limiting documentation"
   - "AWS API Gateway throttling token bucket"
   - "Google rate limiter Doorman paper"
   - "Envoy proxy rate limiting service"
   - "Redis rate limiting Lua script"
   - "NGINX limit_req leaky bucket"
   - "Kong rate limiting plugin"
   - "IETF RateLimit header fields draft"
   - "Twitter API rate limiting"
   - "Shopify API rate limiting leaky bucket"
   - "Cloudflare bot management rate limiting"
   - "Google Cloud adaptive throttling client library"
   - "circuit breaker vs rate limiter difference"
   - "distributed rate limiting Redis race condition"

   **BLANKET PERMISSION**: You have full permission to use WebSearch and WebFetch to read any online documentation, blog posts, or research papers. Do NOT ask the user for permission to read — just read. Read as many pages as needed to verify facts.

2. **For every concrete claim about a specific system** (Stripe uses token bucket, Cloudflare uses sliding window, GitHub uses points for GraphQL), verify against official documentation or engineering blogs. If you cannot verify, explicitly write "[UNVERIFIED — check official docs]" next to it.

3. **For every algorithm description**, verify against established computer science literature or official documentation. Rate limiting algorithms are well-documented — there's no excuse for inaccuracy.

4. **CRITICAL: Do NOT confuse these related but distinct concepts**:
   - **Rate limiting**: Enforce per-client request quotas (protect the platform from clients)
   - **Load shedding**: Drop requests globally during overload (protect the system from aggregate load)
   - **Circuit breaking**: Stop making calls to a failing downstream service (protect the client from a failing server)
   - **Backpressure**: Signal upstream to slow down when downstream can't keep up (flow control)
   - **Throttling**: Umbrella term that can mean any of the above — be specific about which mechanism you mean
   - When discussing design decisions, ALWAYS explain how rate limiting differs from these adjacent concepts and when each is appropriate.

## Key Rate Limiter topics to cover

### Requirements & Scale
- Rate limiting system for a high-scale API platform (think Stripe, GitHub, Twilio)
- Millions of rate limit checks per second across multiple regions
- <1ms P99 decision latency (rate limiter must not become the bottleneck)
- Multi-dimension: per-user, per-endpoint, per-IP, per-tier, global
- Multi-layer: edge, API gateway, application, internal service-to-service
- Dynamic rule management without code deployment
- Support for multiple algorithms (token bucket, sliding window)
- Accurate distributed counting (atomic check-and-decrement across nodes)
- Fail-open with aggressive alerting on counter store failures
- Rich observability: rejection rates, quota utilization, decision latency, debug endpoints

### Architecture deep dives (create separate docs as listed in "Files to create" above)

### Design evolution (iterative build-up — the most important part)
- Attempt 0: Single server + in-memory HashMap + fixed window counter
- Attempt 1: Redis + token bucket + response headers + 429 responses
- Attempt 2: Redis Cluster + multi-dimension rules + rules engine + shadow mode
- Attempt 3: Multi-layer (edge + gateway + application + internal) + observability
- Attempt 4: Multi-region + adaptive rate limiting + cost-based limiting

Each attempt MUST:
1. Start by identifying concrete problems with the previous attempt
2. Propose a solution
3. Explain WHY this approach (and why not the alternative — mention Stripe, Cloudflare, GitHub, or Google where relevant)
4. End with "what's still broken?" to motivate the next attempt

### Consistency & Data
- Redis for real-time counters (atomic INCR, Lua scripts for complex algorithms)
- Database (PostgreSQL/MySQL) for rules and configuration (source of truth)
- Local cache for rules (avoid per-request DB lookups)
- Eventual consistency acceptable for rule propagation (seconds delay OK)
- Strong consistency required for counter operations (must be atomic — no double-counting)
- Fail-open on Redis failure (availability > precision in most cases)
- Fail-closed for security-critical limits (login attempts, password resets)

## What NOT to do
- Do NOT treat the rate limiter as a simple counter — it's a distributed system with coordination, consistency, and failure mode challenges. Frame it accordingly.
- Do NOT confuse rate limiting with load shedding, circuit breaking, or backpressure. Distinguish them clearly.
- Do NOT jump to the final architecture. Build it step by step (Attempt 0 → 4).
- Do NOT make up implementation details about specific systems (Stripe, Cloudflare, GitHub) — verify or mark as unverified.
- Do NOT skip the iterative build-up.
- Do NOT write a Wikipedia article. This is an INTERVIEW SIMULATION with dialogue.
- Do NOT describe features without explaining WHY they exist (what problem they solve).
- Do NOT ask the user for permission to read online documentation — blanket permission is granted (see Accuracy requirements section).
- Do NOT ignore the distributed coordination problem — accurate counting across multiple nodes is what makes a rate limiter fundamentally harder than a HashMap. Treat it as a first-class architectural concern.
