# System Design Interview Simulation: Design an API Gateway

> **Interviewer:** Principal Engineer (L8), Platform Infrastructure Team
> **Candidate Level:** SDE-3 (L6 — Senior Software Development Engineer)
> **Duration:** ~60 minutes
> **Date:** February 21, 2026

---

## PHASE 1: Opening & Problem Statement (~2 min)

**Interviewer:**
Hey, welcome. I'm on the platform infrastructure team — we build the systems that sit between external clients and all of our backend microservices. For today's system design round, I'd like you to design an **API Gateway**. Not just a reverse proxy that forwards requests — I'm talking about the full infrastructure component: request routing, authentication, rate limiting, load balancing, circuit breaking, observability, and extensibility via plugins.

I care about how you reason about cross-cutting concerns in a microservices architecture, the tradeoffs between different gateway implementations (Kong, Envoy, NGINX, AWS API Gateway), and how you'd evolve the design from simple to production-hardened.

Take it away.

---

## PHASE 2: Requirements Gathering & Scoping (~5 min)

**Candidate:**
Thanks! An API Gateway is a critical piece of infrastructure — it's the front door for every client request entering the system. Let me scope this carefully because the gateway touches almost every concern in a distributed system.

**Functional Requirements — what does the gateway do?**

> "Let me identify the core operations:
>
> - **Request Routing** — Match incoming requests to upstream services based on path, host, headers, method. Support path rewriting, regex matching, weight-based traffic splitting for canary deployments
> - **Authentication & Authorization** — Validate JWT tokens, API keys, OAuth tokens at the edge before requests reach backends. Inject identity headers for upstream services
> - **Rate Limiting** — Enforce request quotas per consumer, per route, per IP. Multi-dimensional rate limiting (e.g., 100 req/sec per IP AND 10,000 req/min per API key)
> - **Load Balancing** — Distribute traffic across healthy upstream instances. Support round-robin, least-connections, consistent hashing, P2C (power of two choices)
> - **Health Checking** — Active probes and passive monitoring to detect unhealthy upstreams and stop routing to them
> - **Circuit Breaking** — Prevent cascading failures by fast-failing when an upstream is overloaded or down
> - **Request/Response Transformation** — Modify headers, rewrite paths, transform bodies (JSON ↔ XML), add CORS headers
> - **Observability** — Structured access logs, metrics (request rate, error rate, latency percentiles), distributed tracing (inject/propagate trace IDs)
> - **TLS Termination** — Handle HTTPS at the gateway, communicate with upstreams over HTTP or mTLS
> - **Plugin/Middleware Architecture** — Allow extensibility via custom plugins without redeploying the gateway
>
> And on the management side:
> - **Route Configuration API** — CRUD for routes, services, consumers, plugins
> - **Dynamic Configuration** — Route changes take effect without restart or connection drops
> - **Multi-node Configuration Sync** — Propagate config changes across all gateway nodes
>
> A few clarifying questions:
> - **Should I cover WebSocket and gRPC support?**"

**Interviewer:** "Yes, mention them as routing concerns but don't deep-dive the protocol details."

> "- **Are we designing a self-hosted gateway (like Kong/Envoy) or a managed service (like AWS API Gateway)?**"

**Interviewer:** "Self-hosted. That's where the interesting architecture decisions are. Contrast with managed services where relevant."

> "- **Scale expectations?**"

**Interviewer:** "Think large scale — a company with hundreds of microservices and significant external traffic."

**Non-Functional Requirements:**

> "Now the critical constraints:
>
> | Dimension | Requirement | Rationale |
> |---|---|---|
> | **Latency overhead** | < 5ms added per request (excluding upstream) | Gateway is on every request path. Adding 50ms kills tail latency. Breakdown: TLS termination (~1ms) + routing (~0.1ms) + auth (~1-3ms for JWT) + rate limiting (~1ms with local counter, ~2-3ms with Redis) |
> | **Throughput** | 100K-1M+ RPS per node | Kong achieves ~137K RPS in basic proxy mode, ~96K RPS with rate limiting + key auth enabled. NGINX can handle similar or higher with simple proxying |
> | **Availability** | 99.99% (four 9's) | Gateway down = entire platform down. Must be the most reliable component |
> | **Concurrent connections** | 10K-100K per node | NGINX worker can handle 10K+ connections via epoll/kqueue. Event-driven architecture is essential |
> | **Dynamic config** | Route changes in < 5 seconds | In a Kubernetes environment, pods scale every few seconds. Can't require restarts for route changes |
> | **Horizontal scalability** | Linear scale-out | Gateway must be stateless (or near-stateless with externalized state). Scale by adding nodes behind L4 NLB |
> | **Extensibility** | Custom plugins without gateway redeployment | Teams need custom auth logic, transformations, etc. without waiting for gateway team releases |

**Interviewer:**
Good scoping. You quantified the latency budget breakdown — I want to come back to that during the rate limiting discussion. Why did you call out "near-stateless with externalized state"?

**Candidate:**

> "Because the gateway is almost stateless — it doesn't store user data or business state. But rate limiting requires counters, and those counters need to be consistent across gateway nodes. If I have 10 gateway nodes and a rate limit of 100 req/sec per consumer, each node can't independently allow 100 — that's 1,000 total. So rate limiting state is externalized to Redis. The gateway itself is stateless; Redis holds the counters. This matters for horizontal scaling — I can add or remove gateway nodes without redistributing state."

**Interviewer:**
Strong. That's the kind of nuance I'm looking for. Let's move on.

---

### L5 vs L6 vs L7 — Phase 2 Rubric

| Aspect | L5 (SDE-2) | L6 (SDE-3) | L7 (Principal) |
|---|---|---|---|
| **Functional Reqs** | Lists routing, auth, rate limiting | Proactively raises circuit breaking, plugin extensibility, dynamic config reload, request transformation | Additionally discusses gRPC/WebSocket protocol concerns, multi-tenancy, BFF pattern, east-west vs north-south traffic |
| **Non-Functional** | Mentions "low latency" and "high availability" | Quantifies latency budget breakdown per component, cites concrete RPS benchmarks (Kong ~137K RPS), explains externalized state for scaling | Frames NFRs in business impact: gateway latency compounds across all services, availability SLO determines architecture redundancy |
| **Contrast** | Doesn't mention alternatives | Notes self-hosted vs managed tradeoff, mentions Kong/Envoy/NGINX | Explains how each gateway's architecture (NGINX workers, Envoy xDS, Kong Lua plugins) leads to different capability profiles |

---

## PHASE 3: API Design (~5 min)

**Candidate:**

> "Let me focus on the APIs that matter most for a system design discussion — the control-plane APIs for route and rate limit configuration, and how the data-plane request flow works. The full API surface (services, consumers, certificates, health, config sync) is documented in [02-api-contracts.md](02-api-contracts.md)."

### Control-Plane APIs (Route Management)

> "```
> POST /admin/routes
> Request:  {
>     name: "user-service-route",
>     match: { path_prefix: "/api/v1/users", methods: ["GET", "POST"] },
>     upstream: { service_id: "user-service" },
>     plugins: ["jwt-auth", "rate-limiting"],
>     strip_prefix: true,           // /api/v1/users/123 → /users/123 on upstream
>     weight: { "user-service-v1": 90, "user-service-v2": 10 }  // canary
> }
> Response: { route_id, created_at, status: "active" }
>
> GET /admin/routes                // list all routes
> GET /admin/routes/{routeId}      // get specific route
> PUT /admin/routes/{routeId}      // update route
> DELETE /admin/routes/{routeId}   // delete route
> ```
>
> **Why this matters architecturally:** Route configuration is the core of the gateway. The `match` object defines how incoming requests map to upstreams. The `weight` field enables canary deployments — send 10% of traffic to v2, observe error rates, gradually increase. The `strip_prefix` option handles API versioning (external clients use `/api/v1/...`, internal services see `/...`).
>
> **How routes are stored and propagated:**
> - **Kong approach:** Routes stored in PostgreSQL. Gateway nodes poll the DB for changes (configurable interval, default 5 seconds). All nodes converge on the same config. Alternatively, Kong supports DB-less declarative mode with YAML config files.
> - **Envoy approach:** Routes received via xDS protocol (specifically RDS — Route Discovery Service). Control plane pushes route updates to Envoy over gRPC streams. No database, no polling — real-time push. This is why Envoy is preferred in highly dynamic environments like Kubernetes.
> - **NGINX approach:** Routes defined in `nginx.conf`. Changes require `nginx -s reload` — master process spawns new workers with new config, old workers drain existing connections. Works well for infrequent changes."

### Rate Limiting Configuration API

> "```
> POST /admin/rate-limits
> Request:  {
>     scope: "consumer",           // global | per-service | per-route | per-consumer | per-IP
>     consumer_id: "mobile-app",
>     limits: [
>         { window: "second", max: 100 },
>         { window: "minute", max: 5000 },
>         { window: "hour",   max: 100000 }
>     ],
>     algorithm: "token_bucket",   // token_bucket | sliding_window | fixed_window
>     burst_size: 150,             // allow burst up to 150 before limiting
>     redis_cluster: "rate-limit-redis"
> }
> Response: { policy_id, status: "active" }
> ```
>
> **Key design choice:** Multi-dimensional rate limiting. A single request is checked against multiple limits simultaneously: per-IP AND per-consumer AND per-route. Rejected if ANY limit is exceeded. This prevents both individual abuse (per-IP) and aggregate overload (per-consumer).
>
> **Rate limit response headers** (standard practice, used by Stripe, GitHub, etc.):
> ```
> X-RateLimit-Limit: 100         // max requests in window
> X-RateLimit-Remaining: 42      // remaining in current window
> X-RateLimit-Reset: 1708531200  // unix timestamp when window resets
> Retry-After: 3                 // seconds to wait (on 429 response)
> ```"

### Data-Plane Request Flow

> "```
> Client Request → Gateway
>     │
>     ├── 1. TLS Termination (decrypt HTTPS)
>     ├── 2. Route Matching (trie-based path lookup)
>     ├── 3. Plugin Chain — Access Phase:
>     │       ├── Authentication (JWT validate / API key lookup)
>     │       ├── Rate Limiting (check Redis counter)
>     │       ├── IP Restriction (allow/deny list)
>     │       └── Request Transformation (add/remove headers, rewrite path)
>     ├── 4. Load Balancing (select upstream instance)
>     ├── 5. Proxy Request to Upstream
>     ├── 6. Plugin Chain — Response Phase:
>     │       ├── Response Transformation (add security headers, CORS)
>     │       └── Caching (store GET response if cacheable)
>     └── 7. Plugin Chain — Log Phase:
>             ├── Access Logging (structured JSON)
>             ├── Metrics Export (Prometheus counters/histograms)
>             └── Distributed Tracing (inject/propagate trace headers)
> ```
>
> **Contrast with AWS API Gateway:** AWS uses a completely different model — there's no plugin chain. Instead, you wire up Lambda authorizers for auth, Lambda integrations for backend logic, and VTL templates for request/response transformation. Powerful but opinionated and harder to extend."

**Interviewer:**
Good. You've clearly separated control plane from data plane. Let's build the architecture. Start simple and evolve.

---

### L5 vs L6 vs L7 — Phase 3 Rubric

| Aspect | L5 (SDE-2) | L6 (SDE-3) | L7 (Principal) |
|---|---|---|---|
| **API Design** | Lists CRUD endpoints for routes | Explains match semantics (prefix, regex, host, headers), weight-based canary routing, strip_prefix for versioning | Discusses API versioning strategy for the gateway's own admin API, backward compatibility, config schema validation |
| **Rate Limiting Config** | "Set a rate limit per API key" | Multi-dimensional limits, algorithm selection, burst size, Redis backing, standard response headers | Discusses quota management for API monetization tiers, rate limit inheritance hierarchy, grace periods |
| **Config Propagation** | "Store config in a database" | Contrasts three approaches: DB polling (Kong), xDS push (Envoy), config reload (NGINX) with tradeoffs | Discusses config versioning, rollback, canary config deployment, config drift detection across nodes |

---

## PHASE 4: High-Level Architecture — Iterative Build-Up (~15 min)

**Candidate:**

> "Let me start with the simplest thing that works, find the problems, and evolve. This iterative build-up is how I'd actually think about designing this system."

---

### Attempt 0: Direct Client-to-Service Communication (No Gateway)

> "No gateway at all. Clients (web, mobile, third-party) call each microservice directly:
>
> ```
>     Mobile App        Web App         Third-Party
>         │                │                │
>         │ /users/123     │ /orders/456    │ /products
>         ▼                ▼                ▼
>     ┌──────────┐   ┌──────────┐   ┌──────────┐
>     │  User     │   │  Order   │   │ Product  │
>     │  Service  │   │  Service │   │ Service  │
>     │  :8001    │   │  :8002   │   │  :8003   │
>     └──────────┘   └──────────┘   └──────────┘
>
>     Each service handles its own:
>     - TLS termination
>     - Authentication
>     - Rate limiting
>     - CORS headers
>     - Logging
> ```
>
> Every service is directly exposed to the internet. Each implements its own auth, rate limiting, CORS, logging."

**Interviewer:**
What's wrong with this?

**Candidate:**

> "Everything:
>
> | Problem | Impact |
> |---------|--------|
> | **Duplicated cross-cutting concerns** | Every service implements auth, rate limiting, CORS, logging independently. 50 services × each concern = massive duplication and inconsistency |
> | **Client complexity** | Clients must know every service's address. If a service moves or scales, clients must be updated. One page load might require 5 different service calls |
> | **Large attack surface** | Every service exposed to the internet. Any vulnerability in any service is directly exploitable |
> | **No unified rate limiting** | Each service rate-limits independently. A malicious client can hammer 50 services simultaneously |
> | **No central observability** | No single place to see all request traffic, error rates, latency across the platform |
> | **Protocol coupling** | Clients must speak whatever protocol each service uses (REST, gRPC, GraphQL). Can't do protocol translation |
>
> This is the 'distributed monolith' anti-pattern for cross-cutting concerns. Let me fix it."

---

### Attempt 1: Simple Reverse Proxy (Static Routing)

> "Put a single NGINX instance as the entry point. Static config file maps URL paths to upstream services:
>
> ```
>     All Clients
>         │
>         │  HTTPS
>         ▼
>     ┌──────────────────────────────────┐
>     │        NGINX Reverse Proxy        │
>     │                                    │
>     │  nginx.conf:                       │
>     │    /api/users → user-service:8001  │
>     │    /api/orders → order-service:8002│
>     │    /api/products → product:8003    │
>     │                                    │
>     │  TLS Termination (HTTPS → HTTP)    │
>     │  Single IP address for clients     │
>     └──────────┬───────────────────────┘
>                │  HTTP (internal network)
>         ┌──────┼──────┐
>         ▼      ▼      ▼
>     ┌──────┐┌──────┐┌──────┐
>     │ User ││Order ││ Prod │
>     │ Svc  ││ Svc  ││ Svc  │
>     └──────┘└──────┘└──────┘
> ```
>
> **What's better:**
> - Single entry point — clients call one address
> - TLS termination centralized — services communicate over plain HTTP internally
> - Attack surface reduced — only the proxy is exposed to the internet
>
> **Contrast with AWS API Gateway:** AWS API Gateway provides this out of the box — managed reverse proxy with auto-scaling, TLS, and a console for route configuration. No NGINX config files to manage. But at this basic level, even NGINX does the job.
>
> **What's still broken:**"

> "| Problem | Impact |
> |---------|--------|
> | **Static config** | Route changes require editing `nginx.conf` and running `nginx -s reload`. In Kubernetes where pods scale every few seconds, this doesn't work |
> | **No service discovery** | If a service instance dies, NGINX doesn't know. It keeps sending traffic to the dead instance. Upstream addresses are hardcoded |
> | **No auth** | Every service still handles its own authentication. No centralized identity validation |
> | **No rate limiting** | A single client can overwhelm the entire system. No protection against abuse |
> | **Single point of failure** | One NGINX instance — if it dies, everything is down |
> | **No observability** | Basic NGINX access logs but no structured metrics, no distributed tracing |"

---

### Attempt 2: Dynamic Routing with Service Discovery

> "Replace static config with dynamic service discovery. The gateway integrates with a service registry (Consul, Eureka, Kubernetes Endpoints) to automatically discover healthy upstream instances:
>
> ```
>     All Clients
>         │
>         ▼
>     ┌──────────────────────────────────────────────┐
>     │            API Gateway (Dynamic)              │
>     │                                                │
>     │  ┌───────────────┐   ┌────────────────────┐  │
>     │  │ Route Table    │   │ Service Discovery  │  │
>     │  │ (in-memory,   │   │ Integration        │  │
>     │  │  trie-based)  │   │ (Consul/Eureka/    │  │
>     │  │               │   │  K8s Endpoints)    │  │
>     │  └───────────────┘   └────────────────────┘  │
>     │                                                │
>     │  ┌────────────────────────────────────────┐   │
>     │  │ Health Checker                          │   │
>     │  │ Active: HTTP GET /health every 5 sec   │   │
>     │  │ Passive: monitor 5xx rates, circuit    │   │
>     │  │          break on >50% failure          │   │
>     │  └────────────────────────────────────────┘   │
>     │                                                │
>     │  Load Balancer: round-robin / least-conn       │
>     └──────────────────┬─────────────────────────────┘
>                        │
>         ┌──────────────┼──────────────┐
>         ▼              ▼              ▼
>     ┌──────┐      ┌──────┐      ┌──────┐
>     │User-1│      │User-2│      │User-3│
>     └──────┘      └──────┘      └──────┘
>         (user-service: 3 healthy instances)
> ```
>
> **What's better:**
> - Gateway dynamically discovers upstream instances — when a service scales up or down, the gateway knows within seconds
> - Active health checks: gateway probes `/health` endpoints every 5 seconds. 3 consecutive failures → mark unhealthy, stop routing. 2 consecutive successes → mark healthy again
> - Passive health checks: monitor actual request failures. If >50% of last 10 requests fail → mark unhealthy immediately. Catches failures faster than active probes
> - Load balancing across healthy instances — round-robin for homogeneous backends, least-connections for long-lived requests
>
> **Contrast with Envoy:** Envoy receives upstream endpoints via EDS (Endpoint Discovery Service) from a control plane (Istio). Real-time updates, no polling. Envoy uses P2C (Power of Two Choices) as its default load balancing algorithm — pick two random upstreams, route to the one with fewer active connections. Provably better than pure random and nearly as good as least-connections, with O(1) decision time.
>
> **Contrast with Kong:** Kong discovers upstreams via DNS or integrations with Consul. Kong's admin API allows registering upstream targets manually or dynamically. Health checks are configurable per upstream (active + passive).
>
> **What's still broken:**"

> "| Problem | Impact |
> |---------|--------|
> | **No authentication** | Any client can call any service. No identity validation at the edge |
> | **No rate limiting** | A single abusive client can overwhelm the system. No fair resource allocation |
> | **No observability** | Debugging is blind — no structured logging, no tracing, no metrics dashboard |
> | **No circuit breaking** | A slow upstream causes cascading timeouts. Gateway keeps sending requests, accumulating connections |"

---

### Attempt 3: Cross-Cutting Concerns (Auth, Rate Limiting, Observability)

> "Now we add the core value proposition of an API Gateway — the cross-cutting concerns that every request needs:
>
> ```
>     All Clients
>         │
>         ▼
>     ┌──────────────────────────────────────────────────────┐
>     │                    API Gateway                        │
>     │                                                       │
>     │  Request Flow (Plugin Chain):                         │
>     │                                                       │
>     │  ┌─────────┐  ┌─────────────┐  ┌──────────────┐     │
>     │  │  Auth    │→│ Rate Limiter │→│ IP Restrict   │     │
>     │  │  Plugin  │  │  Plugin      │  │  Plugin       │     │
>     │  │ (JWT or  │  │ (token      │  │ (allow/deny  │     │
>     │  │ API key) │  │  bucket +   │  │  CIDR list)  │     │
>     │  └─────────┘  │  Redis)      │  └──────────────┘     │
>     │                └─────────────┘                        │
>     │                       │                                │
>     │               ┌──────▼──────┐                         │
>     │               │ Route + LB  │                         │
>     │               │ + Proxy     │                         │
>     │               └──────┬──────┘                         │
>     │                      │                                │
>     │  ┌─────────┐  ┌─────▼─────┐  ┌──────────────┐       │
>     │  │  CORS   │→│ Response   │→│ Access Log    │       │
>     │  │  Plugin │  │ Transform  │  │ + Metrics     │       │
>     │  └─────────┘  └───────────┘  │ + Tracing     │       │
>     │                               └──────────────┘       │
>     └──────────────────────────────────────────────────────┘
>                        │
>                   ┌────┴────┐
>                   ▼         ▼
>               ┌──────┐  ┌──────────────────────┐
>               │Redis │  │ Upstream Services     │
>               │(rate │  │                       │
>               │limit │  │                       │
>               │state)│  │                       │
>               └──────┘  └──────────────────────┘
> ```
>
> **Authentication — JWT validation at the gateway:**
> 1. Client sends `Authorization: Bearer <JWT>` header
> 2. Gateway validates JWT locally — verify RSA/ECDSA signature, check `exp` (expiry), `iss` (issuer), `aud` (audience)
> 3. On success: inject `X-User-Id`, `X-User-Roles` headers into the request. Upstream services trust these headers (they come from the gateway, not the client)
> 4. On failure: return `401 Unauthorized`
>
> **Why JWT validation at the gateway?** No database lookup needed — the token is self-contained. Validation is CPU-only (~1-3ms for RSA signature verification). Revocation is the tradeoff — a JWT is valid until it expires. Mitigation: short-lived tokens (5-15 minutes) + refresh tokens. Gateway caches validated tokens in an LRU cache for the token's remaining TTL.
>
> **Rate Limiting — token bucket backed by Redis:**
>
> Token bucket algorithm: bucket holds tokens, refilled at a constant rate. Each request consumes one token. Bucket empty → reject with 429. Allows bursts up to bucket capacity.
>
> **Why token bucket?** It's the most commonly used in production — used by AWS, Stripe, GitHub. It allows controlled bursts (real-world traffic is bursty) while enforcing an average rate. The sliding window counter is a good alternative — more intuitive for consumers ('100 requests per minute' means exactly that, no boundary burst) but slightly more complex.
>
> **Distributed rate limiting via Redis:**
> ```
> -- Lua script (atomic in Redis)
> local key = KEYS[1]           -- e.g., "ratelimit:consumer:mobile-app:second"
> local tokens = tonumber(redis.call('GET', key) or bucket_max)
> local last_refill = tonumber(redis.call('GET', key..':ts') or now)
> local elapsed = now - last_refill
> local refill = math.floor(elapsed * refill_rate)
> tokens = math.min(bucket_max, tokens + refill)
> if tokens > 0 then
>     tokens = tokens - 1
>     redis.call('SET', key, tokens)
>     redis.call('SET', key..':ts', now)
>     return 1  -- allowed
> else
>     return 0  -- rejected
> end
> ```
> This is how Stripe implements rate limiting — token bucket state in Redis, compound operations via Lua scripts for atomicity.
>
> **Observability — the three pillars:**
> - **Structured access logs**: JSON format, per-request (timestamp, client IP, method, path, status, latency, upstream, request ID)
> - **Metrics**: Prometheus-compatible `/metrics` endpoint. Key metrics: request rate, error rate (4xx, 5xx), latency histograms (p50, p95, p99), active connections, rate limiter rejections, circuit breaker state
> - **Distributed tracing**: Gateway injects trace context headers (`traceparent` per W3C Trace Context or `X-B3-TraceId` per Zipkin). Gateway creates a span with start/end time, upstream, status. Exports to Jaeger/Zipkin/OpenTelemetry Collector
>
> **Contrast with Kong:** Kong provides all of this via plugins — `jwt`, `key-auth`, `rate-limiting`, `prometheus`, `zipkin`, `cors`, `ip-restriction`. Each plugin is independently attachable to routes, services, or globally. Kong's rate-limiting plugin supports `local` (in-memory), `cluster` (PostgreSQL-backed), and `redis` (Redis-backed) strategies.
>
> **Contrast with AWS API Gateway:** AWS provides API keys, Lambda authorizers (custom auth), built-in throttling (token bucket, default 10,000 RPS per account per region, burstable to 5,000), CloudWatch Logs + Metrics, X-Ray for tracing. Limited customization — you get what AWS provides. Can't change the rate limiting algorithm or back it with your own Redis.
>
> **What's still broken:**"

> "| Problem | Impact |
> |---------|--------|
> | **Rate limiting Redis is a SPOF** | If Redis goes down, rate limiting fails. Do we fail open (allow all) or fail closed (reject all)? Both are bad |
> | **No circuit breaking** | When an upstream is down, gateway keeps sending requests — wasting resources, accumulating connections, increasing latency for clients |
> | **No response caching** | Identical GET requests always hit the upstream. For read-heavy APIs, this wastes upstream capacity |
> | **No request transformation** | Clients must match the exact upstream API shape. Can't do protocol translation or payload shaping |
> | **Single point of failure** | Still a single gateway instance |"

---

### Attempt 4: Resilience and Performance (Circuit Breaking, Caching, Transformation)

> "Now we add resilience patterns that separate a production gateway from a prototype:
>
> **Circuit breaker per upstream (three states):**
>
> ```
>     ┌──────────┐   failure rate > 50%   ┌──────────┐
>     │  CLOSED  │ ─────────────────────► │   OPEN   │
>     │ (normal, │                         │ (all     │
>     │  traffic │                         │  rejected│
>     │  flows)  │                         │  with    │
>     └──────────┘                         │  503 or  │
>          ▲                               │  fallback│
>          │ probe succeeds                └────┬─────┘
>          │                                    │
>     ┌────┴─────┐     after 30s cooldown      │
>     │HALF-OPEN │ ◄───────────────────────────┘
>     │(limited  │
>     │ probe    │  probe fails → back to OPEN
>     │ requests)│
>     └──────────┘
> ```
>
> - **Closed → Open:** When failure rate exceeds threshold (e.g., >50% of last 10 requests fail, or >5 consecutive 5xx)
> - **Open → Half-Open:** After a configurable cooldown period (e.g., 30 seconds)
> - **Half-Open → Closed:** If probe requests succeed
> - **Half-Open → Open:** If probe requests fail
>
> **Per-upstream circuit breakers** — each upstream gets its own. A failing user-service doesn't affect routing to order-service. Avoid a single global circuit breaker.
>
> **Bulkhead pattern:** Separate connection pools per upstream. If order-service becomes slow (consuming all connections), it doesn't starve user-service. Like watertight compartments in a ship.
>
> **Retry with exponential backoff + jitter:**
> - Retry on: 502, 503, 504, connection failure, timeout
> - Do NOT retry on: 4xx (client error) or 500 (server bug)
> - Retry budget: limit retries to 20% of total requests. Prevents retry storms
> - Backoff: `min(cap, base * 2^attempt) + random_jitter`. Jitter prevents synchronized retries from multiple gateway nodes
> - Idempotency: only retry idempotent requests (GET, PUT, DELETE). Do NOT retry POST unless upstream supports idempotency keys
>
> **Response caching:**
> - Cache GET responses at the gateway (in-memory LRU or Redis)
> - Cache key: URL + relevant headers (e.g., `Accept`, `Authorization`)
> - TTL-based, with `stale-while-revalidate` support
> - Respect `Cache-Control`, `ETag`, `Last-Modified` headers from upstream
> - 80-90% cache hit rate achievable for read-heavy APIs
>
> **Request/Response transformation:**
> - Path rewriting: `/api/v1/users/123` → `/users/123` on upstream
> - Header manipulation: add/remove/rename headers
> - Body transformation: inject fields, scrub sensitive data, JSON ↔ XML
>
> **Contrast with Hystrix/Resilience4j:** Netflix Hystrix introduced the circuit breaker pattern to mainstream. Uses separate thread pools per dependency (bulkhead). Provides fallback methods. *Note: Hystrix is in maintenance mode — Resilience4j is the modern Java replacement* (lighter, functional style, doesn't require dedicated thread pools). Envoy has built-in outlier detection (passive circuit breaking based on consecutive 5xx) plus max connections, max pending requests, max retries per cluster (bulkhead).
>
> **What's still broken:**"

> "| Problem | Impact |
> |---------|--------|
> | **Single region** | Regional failure takes down all API traffic |
> | **Single gateway instance** | Still a SPOF. No HA |
> | **Plugin updates require redeployment** | Updating a plugin = redeploying the entire gateway fleet. Risky, slow |
> | **No canary routing** | Can't gradually roll out new service versions |
> | **No audit trail** | Admin API changes aren't logged — no accountability for config changes |"

---

### Attempt 5: Production Hardening (HA, Multi-Region, Extensibility)

> "This is where the gateway becomes production-ready infrastructure:
>
> **High Availability — multi-node active-active behind L4 NLB:**
>
> ```
>     ┌──────────────────────────────────────────────────────┐
>     │                     Clients                            │
>     └───────────────────────┬────────────────────────────────┘
>                             │
>     ┌───────────────────────▼────────────────────────────────┐
>     │            L4 Network Load Balancer (NLB)               │
>     │     (TCP-level, no TLS termination, very fast)          │
>     │     Routes by IP:port, health checks gateway nodes      │
>     └──────┬────────────┬────────────┬───────────────────────┘
>            │            │            │
>     ┌──────▼────┐ ┌────▼──────┐ ┌──▼────────┐
>     │ Gateway   │ │ Gateway   │ │ Gateway   │
>     │ Node 1    │ │ Node 2    │ │ Node 3    │
>     │           │ │           │ │           │
>     │ TLS term  │ │ TLS term  │ │ TLS term  │
>     │ Route     │ │ Route     │ │ Route     │
>     │ Auth      │ │ Auth      │ │ Auth      │
>     │ Rate limit│ │ Rate limit│ │ Rate limit│
>     │ CB + LB   │ │ CB + LB   │ │ CB + LB   │
>     └─────┬─────┘ └─────┬─────┘ └─────┬─────┘
>           │             │             │
>           └──────┬──────┴──────┬──────┘
>                  ▼             ▼
>         ┌──────────────┐  ┌────────────────────┐
>         │ Redis Cluster │  │ Config DB          │
>         │ (rate limit   │  │ (PostgreSQL for    │
>         │  counters)    │  │  Kong, or xDS      │
>         │ Sentinel/     │  │  control plane     │
>         │ Cluster mode  │  │  for Envoy)        │
>         └──────────────┘  └────────────────────┘
> ```
>
> **Why L4 NLB in front (not L7 ALB)?**
> - L4 routes by IP:port — very fast, very cheap, millions of connections
> - L7 ALB would duplicate routing logic the gateway already handles
> - Let the gateway handle all L7 concerns (routing, auth, rate limiting)
> - NLB health-checks the gateway nodes (TCP connect or HTTP GET /health)
>
> **Graceful shutdown & zero-downtime deployments:**
> - On shutdown: stop accepting new connections → drain existing connections (wait for in-flight requests) → exit
> - In Kubernetes: pod receives SIGTERM → gateway starts draining → readiness probe fails (NLB stops sending traffic) → drain period → SIGKILL
> - Rolling update: deploy new version to one node at a time. NLB routes around draining nodes
>
> **Multi-region deployment:**
> - Deploy gateway clusters in multiple regions
> - DNS-based or Anycast routing to nearest region
> - Config replication across regions (PostgreSQL replication or shared xDS control plane)
>
> **Plugin extensibility — support custom plugins without gateway restart:**
> - **Lua (Kong/OpenResty):** Runs inside NGINX event loop. Low overhead, no IPC. But Lua is a niche language and the sandboxed environment limits what you can do
> - **Wasm (Envoy):** Compile plugins from C++, Rust, Go, AssemblyScript to Wasm. Runs in a sandboxed VM inside Envoy. Safe (can't crash the gateway), portable, polyglot. Higher overhead than native code
> - **External processing (Envoy ext_proc):** Gateway sends request to an external gRPC service for processing. Any language. Adds network latency but fully decouples plugin logic from gateway
>
> **Canary / traffic splitting:**
> - Weight-based routing: `{ "v1": 95, "v2": 5 }` — send 5% to new version
> - Header-based routing: `X-Canary: true` → route to v2 for internal testing
> - Monitor error rates on v2 before increasing weight
>
> **Rate limiting fallback:**
> - If Redis is down, fall back to local (per-node) rate limiting. Less accurate (each node allows `limit/N` independently) but still functional
> - **Critical design decision:** Don't let rate limiting infrastructure failure cause gateway failure. Rate limiting is a safety mechanism — its failure shouldn't create an outage. Fail open with degraded accuracy
>
> **Audit logging:**
> - Log all admin/config changes: who changed which route, when, what was the previous config
> - Immutable audit trail for compliance (SOX, HIPAA, PCI-DSS)
> - Stored separately from access logs"

**Interviewer:**
Excellent walk-through. You've built up from direct client-to-service communication to a production-hardened API Gateway. Let me summarize the evolution:

---

### Architecture Evolution Table

| Attempt | Key Addition | Problem Solved | Key Technology |
|---------|-------------|---------------|----------------|
| 0 | No gateway (direct client-to-service) | Baseline — shows why a gateway is needed | — |
| 1 | NGINX reverse proxy (static routing) | Single entry point, TLS termination, reduced attack surface | NGINX, static `nginx.conf` |
| 2 | Dynamic routing + service discovery + health checks | Auto-discover upstreams, remove unhealthy instances, load balance | Consul/Eureka/K8s Endpoints, active+passive health checks |
| 3 | Auth + rate limiting + observability | Centralized auth (JWT), abuse prevention (token bucket + Redis), debugging visibility | JWT, Redis, Prometheus, Jaeger |
| 4 | Circuit breaking + retry + caching + transformation | Resilience against upstream failures, performance for read-heavy APIs | Circuit breaker (3 states), exponential backoff, LRU cache |
| 5 | HA + multi-region + plugin architecture + canary | No SPOF, global reach, extensibility, safe deployments | L4 NLB, Wasm/Lua/ext_proc, weight-based routing |

---

### L5 vs L6 vs L7 — Phase 4 Rubric

| Aspect | L5 (SDE-2) | L6 (SDE-3) | L7 (Principal) |
|---|---|---|---|
| **Iterative build-up** | Jumps to "use Kong" without explaining why simpler approaches fail | Builds incrementally, each step motivated by concrete problems with previous attempt. Contrasts with real gateways at each step | Additionally quantifies problems (latency numbers, failure rates) and discusses organizational tradeoffs (gateway team as bottleneck) |
| **Rate limiting** | "Add Redis for rate limiting" | Explains token bucket algorithm, Lua script atomicity, distributed counter challenge, multi-dimensional limits | Discusses rate limiting economics (API monetization tiers), graceful degradation when Redis fails, sliding window vs token bucket tradeoffs |
| **Resilience** | "Add circuit breakers" | Explains 3-state machine, per-upstream isolation, bulkhead pattern, retry budgets | Discusses retry amplification cascades across service layers, request hedging for latency-critical paths, backpressure propagation |
| **Production hardening** | "Deploy multiple instances" | L4 NLB, graceful shutdown, zero-downtime rolling updates, rate limiting fallback | Discusses config drift detection, blast radius containment, gateway as infrastructure vs application layer |
| **Gateway contrasts** | Names one gateway | Contrasts Kong (Lua, DB-backed), Envoy (C++, xDS), NGINX (config reload) at each evolution step | Explains WHY each gateway made its architectural choice and when to choose each |

---

## PHASE 5: Deep Dive — Request Routing Engine (~8 min)

**Interviewer:**
Let's go deeper on routing. You mentioned trie-based path matching — walk me through how the routing engine works for a gateway with thousands of routes.

**Candidate:**

> "The routing engine is the hottest path in the gateway — every single request goes through it. It must be fast.
>
> **Request matching pipeline (evaluated in order of specificity):**
> 1. Exact host match (`api.example.com` vs `api.staging.example.com`)
> 2. Path match: exact → prefix → regex (in decreasing specificity)
> 3. HTTP method (GET, POST, etc.)
> 4. Header conditions (`X-Api-Version: 2` → route to v2 service)
> 5. Query parameter conditions
>
> First match wins (with priority ordering). No match → 404 or default route.
>
> **Trie-based path matching (radix trie / Patricia tree):**
>
> For small route tables (< 100 routes), linear scan is fine. But for thousands of routes, linear scan is O(n) per request. At 100K RPS, that's 100K × O(n) path comparisons per second.
>
> Use a **radix trie** for O(k) lookup where k = number of path segments:
>
> ```
> Routes:
>   /api/v1/users           → user-service
>   /api/v1/users/{id}      → user-service
>   /api/v1/users/{id}/orders → order-service
>   /api/v1/products        → product-service
>   /api/v2/users           → user-service-v2
>
> Trie:
>                 api
>                / \
>              v1   v2
>             / \     \
>          users products  users → user-service-v2
>          /  \       \
>       (leaf) {id}   (leaf) → product-service
>         ↓      \
>   user-service  orders → order-service
>                    ↓
>              user-service
> ```
>
> Path parameters (`{id}`) are wildcard nodes — they match any segment and extract the value during traversal.
>
> **Hot reload of routing configuration:**
>
> Route changes must take effect without restarting the gateway or dropping active connections:
> - **NGINX:** `nginx -s reload` sends SIGHUP to master process. Master spawns new worker processes with new config. Old workers stop accepting new connections, finish in-flight requests, then exit. Graceful but requires a reload signal — not suitable for changes every few seconds
> - **Envoy:** xDS protocol (specifically RDS — Route Discovery Service). Control plane pushes route updates to Envoy over a gRPC stream. Envoy updates its in-memory route table atomically. No reload, no new workers, no dropped connections. Fully dynamic — routes can change every second
> - **Kong:** Config stored in PostgreSQL. Gateway nodes poll DB for changes (configurable interval, typically 1-5 seconds). On detecting changes, route table is rebuilt in-memory. In DB-less mode, config is loaded from a declarative YAML file and can be hot-reloaded via admin API
>
> For the full deep dive, see [03-request-routing-engine.md](03-request-routing-engine.md)."

**Interviewer:**
How do you handle gRPC and WebSocket routing?

**Candidate:**

> "**gRPC routing:** gRPC uses HTTP/2 with `content-type: application/grpc` and a `:path` pseudo-header like `/package.ServiceName/MethodName`. The gateway routes by gRPC service name and method, similar to HTTP path routing. The key challenge is protocol translation — accept REST from external clients, convert to gRPC for internal services (and vice versa). Envoy excels here with native gRPC support and gRPC-JSON transcoding.
>
> **WebSocket routing:** Gateway handles the HTTP `Upgrade: websocket` header, establishes the persistent connection, and routes to the correct upstream WebSocket server. Challenge: WebSocket connections are long-lived, pinning resources on the gateway. This interacts with the bulkhead pattern — need separate connection quotas for WebSocket vs HTTP to prevent WebSocket connections from starving HTTP traffic."

---

### L5 vs L6 vs L7 — Phase 5 Rubric

| Aspect | L5 (SDE-2) | L6 (SDE-3) | L7 (Principal) |
|---|---|---|---|
| **Path matching** | "Match URL to service" | Explains radix trie for O(k) lookup, wildcard nodes for path parameters, specificity ordering | Discusses route compilation, regex DFA engines, route conflict resolution, route testing frameworks |
| **Hot reload** | "Restart to apply changes" | Contrasts NGINX reload (new workers) vs Envoy xDS (in-memory update) vs Kong (DB polling) with tradeoffs | Discusses route consistency during reload (partial updates?), A/B route testing, config versioning and rollback |
| **Protocol support** | "Supports HTTP" | Explains gRPC routing (`:path` pseudo-header, gRPC-JSON transcoding) and WebSocket (Upgrade header, long-lived connections) | Discusses HTTP/2 vs HTTP/3 (QUIC), protocol detection, L4 vs L7 routing for non-HTTP protocols |

---

## PHASE 6: Deep Dive — Rate Limiting (~8 min)

**Interviewer:**
Rate limiting is the most common deep-dive topic in API Gateway interviews. Walk me through the algorithms and the distributed challenge.

**Candidate:**

> "Rate limiting is deceptively simple in concept but challenging in distributed systems.
>
> **Why rate limit?**
> - Protect backend services from overload (a single misbehaving client shouldn't take down the system)
> - Prevent abuse (scraping, brute force attacks)
> - Enforce API usage tiers (free: 100 req/min, paid: 10,000 req/min)
> - Ensure fair resource allocation across consumers
> - First line of defense against DDoS (before WAF)
>
> **Algorithm comparison:**
>
> | Algorithm | Burst Handling | Memory | Accuracy | Production Usage |
> |-----------|---------------|--------|----------|-----------------|
> | **Fixed window** | Bad (boundary burst: 2× limit in 2 seconds at window boundary) | O(1) — one counter | Moderate | Simple use cases |
> | **Sliding window log** | Perfect (tracks every request) | O(n) — stores every timestamp | Perfect | Too expensive for high throughput |
> | **Sliding window counter** | Good (weighted estimate from two windows) | O(1) — two counters | Good (~99.7%) | Cloudflare |
> | **Token bucket** | Controlled burst up to bucket size | O(1) — tokens + timestamp | Good | Stripe, AWS, GitHub |
> | **Leaky bucket** | None (constant drain rate) | O(n) — FIFO queue | Perfect | Backends that can't tolerate burst |
>
> **Token bucket is the most common for API gateways** because real-world traffic is bursty. A mobile app might send 10 requests simultaneously when a screen loads, then nothing for 30 seconds. Token bucket allows this burst (up to bucket capacity) while enforcing an average rate.
>
> **Distributed rate limiting — the hard problem:**
>
> Single-node rate limiting is easy (in-memory counter). But gateways run as clusters. 10 gateway nodes with a limit of 100 req/sec — each node can't independently allow 100.
>
> **Approach 1: Centralized counter (Redis)**
> - Every request increments a counter in Redis. `INCR key` + `EXPIRE key ttl`
> - Accurate but adds ~1-3ms latency per request (Redis round-trip)
> - Redis becomes a dependency — must be highly available (Sentinel or Cluster)
> - Race conditions: use `INCR` (atomic) or Lua scripts for compound operations (token bucket)
>
> **Approach 2: Local counters with periodic sync**
> - Each gateway node tracks its own counter
> - Periodically sync with central store (every 100ms or every 100 requests)
> - Allows slight over-admission between sync intervals
> - Tradeoff: accuracy vs latency. Good enough for most use cases
>
> **Approach 3: Token bucket in Redis (Stripe's approach)**
> - Store `{tokens, last_refill_time}` in Redis
> - On each request: compute tokens to add since last refill, subtract one, update
> - Atomic via Lua script (the one I showed in Attempt 3)
> - This is production-proven at Stripe's scale
>
> **Rate limiting scope hierarchy:**
> ```
> Global (entire gateway)
>   └── Per-Service (user-service)
>       └── Per-Route (/api/v1/users)
>           └── Per-Consumer (mobile-app)
>               └── Per-IP (192.168.1.1)
> ```
> More specific limits override broader ones. A premium consumer might have 10,000 req/min while the global default is 1,000.
>
> **Contrast with Kong:** Kong's rate-limiting plugin supports fixed window and sliding window. Strategies: `local` (in-memory, per-node, fastest but not distributed), `cluster` (PostgreSQL-backed, consistent but slower), `redis` (Redis-backed, best balance). Configurable per consumer, route, or service.
>
> **Contrast with Envoy:** Envoy separates concerns — local rate limiting (per-node, in-memory token bucket) and global rate limiting (calls out to an external `ratelimit` gRPC service). The external service is a separate deployment (typically backed by Redis). Clean separation of data plane (Envoy) and rate limit logic (external service), but adds another network hop.
>
> **Contrast with AWS API Gateway:** Built-in throttling using token bucket. Account-level default: 10,000 RPS, burst 5,000. Configurable per-stage and per-method. Usage plans for API key-based quotas. No custom algorithm choice — it's managed.
>
> For the full deep dive, see [04-rate-limiting-and-throttling.md](04-rate-limiting-and-throttling.md)."

---

### L5 vs L6 vs L7 — Phase 6 Rubric

| Aspect | L5 (SDE-2) | L6 (SDE-3) | L7 (Principal) |
|---|---|---|---|
| **Algorithms** | "Use rate limiting" | Compares 5 algorithms with burst handling, memory, accuracy tradeoffs. Explains why token bucket is most common | Discusses algorithm selection criteria (traffic pattern analysis), adaptive rate limiting, congestion-based throttling |
| **Distributed** | "Use Redis" | Explains centralized vs local+sync approaches, Lua script atomicity, race conditions, Stripe's implementation | Discusses consistency models (exact vs approximate), cell-based rate limiting, local counters with gossip protocol sync |
| **Failure mode** | Not mentioned | Explains Redis failure → fall back to local rate limiting. Don't let rate limiting failure cause gateway failure | Discusses failure budgets, rate limiting as a safety mechanism vs correctness requirement, graceful degradation hierarchy |

---

## PHASE 7: Deep Dive — Load Balancing & Circuit Breaking (~5 min)

**Interviewer:**
You mentioned P2C load balancing and per-upstream circuit breakers. How do these work together?

**Candidate:**

> "Load balancing and circuit breaking are complementary — load balancing distributes traffic across healthy instances, circuit breaking detects and isolates unhealthy ones.
>
> **Load balancing algorithms:**
>
> | Algorithm | How It Works | Best For |
> |-----------|-------------|----------|
> | **Round-robin** | Rotate across instances | Homogeneous backends, short requests |
> | **Weighted round-robin** | Higher weight = more traffic | Canary deployments, heterogeneous servers |
> | **Least connections** | Route to instance with fewest active connections | Long-lived requests (WebSocket, uploads) |
> | **Consistent hashing** | Hash a key (user ID, session cookie) to select instance | Session affinity, upstream caching |
> | **P2C (Power of Two Choices)** | Pick 2 random instances, route to one with fewer connections | General purpose — Envoy's default. O(1) decision |
> | **EWMA (latency-based)** | Track exponentially weighted moving average latency per instance | Adapting to transient slowness |
>
> **P2C is elegant:** It's provably better than pure random and nearly as good as least-connections, but with O(1) decision time (no need to track all connections). Envoy uses this as default for good reason.
>
> **Circuit breaking prevents cascading failures:**
>
> Without circuit breaking: upstream is slow → gateway accumulates connections waiting for responses → connection pool exhausts → gateway can't serve ANY upstream → cascading failure.
>
> With circuit breaking: upstream failure rate exceeds threshold → circuit opens → requests immediately rejected with 503 or fallback response → gateway resources freed for healthy upstreams → after cooldown, probe to test recovery.
>
> **Retry budget prevents retry storms:**
>
> Without retry budget: Service A retries 3× → Service B retries 3× → Service C receives 9× load. This is the **retry amplification cascade** — the most common cause of cascading failures in microservices.
>
> Solution: Retry budget — limit retries to 20% of total requests. If the system is already failing (many retries happening), additional retries are suppressed.
>
> For the full deep dives, see [06-load-balancing-and-service-discovery.md](06-load-balancing-and-service-discovery.md) and [07-circuit-breaking-and-resilience.md](07-circuit-breaking-and-resilience.md)."

---

### L5 vs L6 vs L7 — Phase 7 Rubric

| Aspect | L5 (SDE-2) | L6 (SDE-3) | L7 (Principal) |
|---|---|---|---|
| **Load balancing** | "Use round-robin" | Compares algorithms, explains P2C and why Envoy chose it, discusses session affinity tradeoffs | Discusses locality-aware routing, zone-weighted load balancing, outlier detection interaction with LB |
| **Circuit breaking** | "Stop sending to failed services" | Explains 3-state machine, per-upstream isolation, failure rate vs consecutive failure thresholds | Discusses circuit breaker calibration, interaction with retry budgets, request hedging for tail latency |
| **Retry storms** | Not mentioned | Explains retry amplification cascade, retry budgets, idempotency requirements | Discusses deadline propagation (remaining time budget), adaptive retry (reduce retries under load), hedge requests |

---

## PHASE 8: Deep Dive — Plugin Architecture (~5 min)

**Interviewer:**
You mentioned Lua, Wasm, and ext_proc for custom plugins. How do you choose between them?

**Candidate:**

> "The plugin execution model determines the gateway's extensibility — how teams add custom logic without modifying the gateway core:
>
> **Plugin phases (when in the request lifecycle does the plugin run):**
> 1. **Access phase** — Before proxying. Auth, rate limiting, IP restriction. Can reject the request
> 2. **Header filter phase** — After receiving upstream response headers but before body. Can modify response headers
> 3. **Body filter phase** — As response body streams back. Can transform body (compression, scrubbing sensitive data)
> 4. **Log phase** — After response is sent. Logging, metrics. Cannot modify the response
>
> **Plugin execution models compared:**
>
> | Model | Language | Overhead | Safety | Flexibility | Used By |
> |-------|----------|----------|--------|-------------|---------|
> | **Lua (OpenResty)** | Lua | Very low (in-process) | Moderate (sandboxed but shares NGINX memory) | Limited (no blocking I/O, limited libraries) | Kong |
> | **Wasm** | C++, Rust, Go, AssemblyScript → Wasm bytecode | Low-moderate (VM overhead) | High (sandboxed VM, can't crash gateway) | High (polyglot, portable) | Envoy |
> | **ext_proc (external gRPC)** | Any language | High (network hop per request) | Highest (separate process) | Highest (full language runtime, any library) | Envoy |
> | **Lambda authorizer** | Any (via AWS Lambda) | High (cold start ~100-500ms) | Highest (separate service) | High | AWS API Gateway |
>
> **Decision framework:**
> - For **performance-critical** plugins (auth, rate limiting): Lua or Wasm. In-process, sub-millisecond overhead
> - For **complex logic** (ML-based bot detection, PII scrubbing): ext_proc or external gRPC call. Full language runtime, any library
> - For **polyglot teams** who want plugin portability: Wasm. Compile from any language, runs anywhere
> - For **simple use cases** with managed infrastructure: Lambda authorizer. Zero ops burden
>
> **Kong's plugin model:**
> Kong runs on OpenResty (NGINX + LuaJIT). Plugins are Lua modules that hook into NGINX phases. Plugin priority determines execution order (higher priority runs first). Kong has 100+ bundled plugins. Custom plugins can be written in Lua or Go (via Go PDK). Config stored in PostgreSQL — plugin changes propagate within seconds.
>
> **Envoy's filter chain:**
> Envoy uses a filter chain model. Built-in filters for routing, auth, rate limiting. Custom filters via C++ (compiled into Envoy), Wasm (sandboxed VM), or ext_proc (external gRPC). Filter ordering defined in the listener config. xDS-driven — filter config can be updated dynamically.
>
> For the full deep dive, see [08-plugin-and-middleware-architecture.md](08-plugin-and-middleware-architecture.md)."

---

### L5 vs L6 vs L7 — Phase 8 Rubric

| Aspect | L5 (SDE-2) | L6 (SDE-3) | L7 (Principal) |
|---|---|---|---|
| **Plugin model** | "Add middleware" | Compares Lua/Wasm/ext_proc with overhead, safety, flexibility tradeoffs. Explains plugin phases | Discusses plugin versioning, plugin marketplace governance, plugin performance budgets (max latency per plugin) |
| **Kong vs Envoy** | Names one | Explains architectural difference: Kong=Lua in NGINX event loop, Envoy=Wasm VM or gRPC call | Discusses migration path between gateways, plugin portability, control plane architecture for plugin management |

---

## PHASE 9: Wrap-Up (~3 min)

**Interviewer:**
Good. Last question: you're running this API Gateway in production, handling 500K RPS across 20 microservices. What keeps you up at night?

**Candidate:**

> "Three things:
>
> **1. Rate limiting Redis failure during a traffic spike**
>
> A flash sale drives 5× normal traffic. Redis Cluster experiences a split-brain during automatic failover. Rate limiting is now degraded — some nodes have stale counters, some have lost counters entirely. Do we fail open (allow unlimited traffic, risk overwhelming backends) or fail closed (reject everything, outage)?
>
> Neither is good. The answer is **graceful degradation**: fall back to per-node local rate limiting with `limit/N` per node. Less accurate but still functional. And critically — the gateway must continue serving traffic. Rate limiting is a safety mechanism; its failure shouldn't cause a gateway outage. This fallback must be tested regularly, not just documented.
>
> **2. Config change blast radius**
>
> An engineer pushes a route configuration change that has a subtle regex bug. The regex causes catastrophic backtracking on certain URL patterns, pegging CPU on all gateway nodes. Within seconds, the entire gateway fleet is unresponsive.
>
> Mitigations: config validation (lint regex patterns, check for backtracking), canary config deployment (apply to one node, observe, then roll out), automatic rollback on error rate spike, audit logging (who changed what, when). The gateway team needs the same deployment discipline as any production service — probably more, because a gateway bug affects every service.
>
> **3. Upstream dependency that's slow but not failing**
>
> A payment service starts responding in 5 seconds instead of 200ms. It's not failing (no 5xx) — it's just slow. Circuit breaker doesn't open because the failure rate is 0%. But the gateway is accumulating connections waiting for responses, exhausting the connection pool for that upstream. The bulkhead pattern helps (separate pool per upstream) but if the payment service is critical and all its connections are busy, payment flows are effectively down.
>
> This is the 'gray failure' problem — the hardest to detect and mitigate. Mitigations: latency-based circuit breaker (open circuit if p99 > threshold, not just on errors), aggressive request timeouts, load shedding at the gateway (reject requests when connection pool is >80% full rather than queuing indefinitely)."

**Interviewer:**
The gray failure scenario is the most realistic — that's what causes actual production incidents most often. The regex catastrophic backtracking is a great operational awareness example. Thanks for the thorough walk-through.

---

### L5 vs L6 vs L7 — Phase 9 Rubric

| Aspect | L5 (SDE-2) | L6 (SDE-3) | L7 (Principal) |
|---|---|---|---|
| **Operational risks** | "Server failures, scaling" | Identifies specific scenarios (Redis split-brain during spike, config regex bug, gray failure) with concrete mitigations | Discusses organizational response: incident management, game days, blast radius containment, gateway SLO that bounds all service SLOs |
| **Depth** | Lists risks | Explains root cause and mitigation for each, discusses the fail-open vs fail-closed decision | Proposes preventive architecture changes, discusses observability gaps, latency-based circuit breaking |

---

## Final Architecture Summary

```
┌──────────────────────────────────────────────────────────────────────┐
│                              CLIENTS                                  │
│           Web │ Mobile │ Third-Party │ Internal Services              │
└──────────────────────────┬───────────────────────────────────────────┘
                           │  HTTPS
              ┌────────────▼────────────┐
              │    L4 NLB (TCP)          │
              │    (health checks        │
              │     gateway nodes)       │
              └──────┬────┬────┬────────┘
                     │    │    │
              ┌──────▼──┐ │  ┌▼────────┐
              │Gateway 1│ │  │Gateway N │  ← Active-Active, stateless
              │         │ │  │         │
              │ TLS Term│ │  │ TLS Term│
              │ Routing │ │  │ Routing │     ← Radix trie, O(k) lookup
              │ Auth    │ │  │ Auth    │     ← JWT local validation
              │ Rate Lim│ │  │ Rate Lim│     ← Token bucket + Redis
              │ CB + LB │ │  │ CB + LB │     ← Per-upstream circuit breaker
              │ Plugins │ │  │ Plugins │     ← Lua/Wasm/ext_proc
              │ Logging │ │  │ Logging │     ← Structured JSON + tracing
              └────┬────┘ │  └────┬────┘
                   │      │       │
         ┌─────────┴──────┴───────┴─────────┐
         │                                   │
    ┌────▼─────────┐              ┌─────────▼────────┐
    │ Redis Cluster │              │ Config Store      │
    │ (rate limit   │              │ (PostgreSQL/xDS   │
    │  counters)    │              │  control plane)   │
    │ Sentinel HA   │              │                   │
    └──────────────┘              └───────────────────┘
         │
         │  Fallback: local rate limiting
         │  if Redis is down
         │
    ┌────▼──────────────────────────────────┐
    │         UPSTREAM SERVICES              │
    │                                        │
    │  user-svc  order-svc  product-svc     │
    │  payment-svc  search-svc  ...         │
    │                                        │
    │  Discovered via: Consul/Eureka/K8s    │
    │  Health checked: active + passive      │
    │  Load balanced: P2C / round-robin      │
    │  Circuit broken: per-upstream          │
    └────────────────────────────────────────┘

    ┌────────────────────────────────────────┐
    │         OBSERVABILITY                   │
    │                                        │
    │  Prometheus (metrics, /metrics)        │
    │  Jaeger/Zipkin (distributed tracing)   │
    │  ELK/Splunk (structured access logs)  │
    │  Grafana (dashboards + alerting)      │
    └────────────────────────────────────────┘
```

---

## Supporting Deep-Dive Documents

| # | Document | Topic |
|---|----------|-------|
| 1 | [01-interview-simulation.md](01-interview-simulation.md) | This file — the main interview dialogue |
| 2 | [02-api-contracts.md](02-api-contracts.md) | Gateway management & configuration APIs |
| 3 | [03-request-routing-engine.md](03-request-routing-engine.md) | Routing & request matching |
| 4 | [04-rate-limiting-and-throttling.md](04-rate-limiting-and-throttling.md) | Rate limiting deep dive |
| 5 | [05-authentication-and-authorization.md](05-authentication-and-authorization.md) | Auth layer |
| 6 | [06-load-balancing-and-service-discovery.md](06-load-balancing-and-service-discovery.md) | Load balancing & discovery |
| 7 | [07-circuit-breaking-and-resilience.md](07-circuit-breaking-and-resilience.md) | Resilience patterns |
| 8 | [08-plugin-and-middleware-architecture.md](08-plugin-and-middleware-architecture.md) | Extensibility |
| 9 | [09-observability-and-monitoring.md](09-observability-and-monitoring.md) | Logging, metrics, tracing |
| 10 | [10-scaling-and-performance.md](10-scaling-and-performance.md) | Scaling & performance |
| 11 | [11-design-trade-offs.md](11-design-trade-offs.md) | Design philosophy & trade-off analysis |

---

## Verified Sources

- Kong Gateway performance: [~137K RPS basic proxy, ~96K RPS with rate limiting + key auth](https://docs.konghq.com/gateway/latest/production/performance/performance-testing/)
- AWS API Gateway throttling: [10,000 RPS default, 5,000 burst, 29-second timeout](https://docs.aws.amazon.com/apigateway/latest/developerguide/api-gateway-request-throttling.html)
- Stripe rate limiting: [Token bucket in Redis with Lua scripts](https://stripe.com/blog/rate-limiters)
- Envoy xDS protocol: [LDS, RDS, CDS, EDS, SDS discovery services](https://www.envoyproxy.io/docs/envoy/latest/api-docs/xds_protocol)
- Netflix Zuul 2: [Netty-based, non-blocking, event loop architecture](https://netflixtechblog.com/zuul-2-the-netflix-journey-to-asynchronous-non-blocking-systems-45947377fb5c)
- NGINX worker architecture: [Event-driven, epoll, master-worker model](https://engineeringatscale.substack.com/p/nginx-millions-connections-event-driven-architecture)
