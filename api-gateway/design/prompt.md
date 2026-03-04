Design an API Gateway as a system design interview simulation.

## Template
Follow the EXACT same format as the Netflix interview simulation at:
src/hld/netflix/design/prompt.md

Same structure:
- Interviewer/Candidate dialogue format
- PHASE 1: Opening & Problem Statement
- PHASE 2: Requirements Gathering & Scoping (functional + non-functional + scale numbers)
- PHASE 3: API Design
- PHASE 4: High-Level Architecture (iterative build-up: Attempt 0 → 1 → 2 → ..., starting from no gateway, finding problems, evolving)
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
Create all files under: src/hld/api-gateway/design/

## Files to create

### 1. 01-interview-simulation.md — the main backbone
The full interview dialogue covering all phases and attempts.

### 2. 02-api-contracts.md — Gateway Management & Configuration APIs

This doc should list all the major API surfaces of an API Gateway — both the **data-plane APIs** (how client requests flow through the gateway) and the **control-plane / admin APIs** (how operators configure the gateway).

**Structure**: For each API group, list every endpoint with HTTP method, path, request/response shape, and a brief description.

**API groups to cover**:

- **Route Management APIs**: `POST /routes` (create route: match condition → upstream target), `GET /routes`, `GET /routes/{routeId}`, `PUT /routes/{routeId}`, `DELETE /routes/{routeId}`. Route matching: path prefix, exact path, regex, host header, HTTP method, header-based, query param-based. Weight-based routing for canary deployments (e.g., 90% v1, 10% v2). Route priority and ordering. Path rewriting rules (`/api/v1/users` → `/users` on upstream).

- **Service / Upstream Management APIs**: `POST /services` (register upstream service: name, host, port, protocol, health check config), `GET /services`, `PUT /services/{serviceId}`, `DELETE /services/{serviceId}`. Health check configuration: active (gateway probes the service) vs passive (gateway monitors request failures). Load balancing algorithm selection per service (round-robin, least-connections, consistent hashing, weighted).

- **Plugin / Middleware APIs**: `POST /plugins` (attach plugin to a route, service, or globally), `GET /plugins`, `PUT /plugins/{pluginId}`, `DELETE /plugins/{pluginId}`. Plugin types: rate-limiting, authentication, request transformation, response transformation, logging, CORS, IP restriction, bot detection. Plugin execution order (priority-based). Plugin configuration schema (each plugin has its own config shape).

- **Consumer / API Key APIs**: `POST /consumers` (create API consumer/client: name, API key, rate limit tier), `GET /consumers`, `PUT /consumers/{consumerId}`, `DELETE /consumers/{consumerId}`. API key management: generate, revoke, rotate. Consumer groups for shared rate limits and permissions. Usage tracking per consumer.

- **Rate Limiting Configuration APIs**: `POST /rate-limits` (define rate limit policy: requests per second/minute/hour, burst size, scope — global, per-consumer, per-route, per-IP), `GET /rate-limits`, `PUT /rate-limits/{policyId}`, `DELETE /rate-limits/{policyId}`. Rate limit response headers (`X-RateLimit-Limit`, `X-RateLimit-Remaining`, `X-RateLimit-Reset`). Quota management for tiered API plans.

- **Certificate / TLS APIs**: `POST /certificates` (upload SSL/TLS certificate and private key), `GET /certificates`, `DELETE /certificates/{certId}`. SNI-based certificate selection for multi-domain support. Automatic certificate renewal integration (Let's Encrypt / ACME). mTLS configuration for upstream services.

- **Health & Status APIs**: `GET /health` (gateway health status), `GET /status` (gateway metrics: active connections, request rate, error rate, latency percentiles), `GET /upstreams/{serviceId}/health` (per-upstream health check status). Cluster status for multi-node deployments.

- **Configuration Sync APIs** (for clustered deployments): `POST /config/sync` (propagate configuration changes to all gateway nodes), `GET /config/version` (current config version / hash), `POST /config/import` (bulk import configuration from file/JSON), `GET /config/export` (export full configuration as JSON).

**Contrast with AWS API Gateway**: AWS API Gateway is a fully managed service — no server management, auto-scaling, pay-per-request. But it's opinionated: limited customization, 29-second timeout, no WebSocket on HTTP APIs (only REST APIs), vendor lock-in. Self-hosted gateways (Kong, Envoy) give full control but require operational investment.

**Contrast with service mesh sidecars (Envoy/Istio)**: In a service mesh, every microservice has a sidecar proxy (Envoy) that handles routing, auth, rate limiting, observability. The API gateway is the **edge** entry point; sidecars handle **east-west** (service-to-service) traffic. Some teams use Envoy as both (Envoy as edge gateway + Envoy as sidecar). The gateway focuses on north-south concerns: external auth, public rate limiting, API versioning, request transformation.

**Interview subset**: In the interview (Phase 3), focus on: route configuration (how clients define routing rules), rate limiting configuration (the most asked-about feature), and health/status (operational visibility). The full API list lives in this doc.

### 3. 03-request-routing-engine.md — Routing & Request Matching

The core of the gateway — how incoming requests are matched to upstream services.

- **Request matching pipeline**: The gateway receives an HTTP request and must determine which upstream service to forward it to. Matching criteria (evaluated in order of specificity): exact host match → path match (exact > prefix > regex) → HTTP method → header conditions → query parameter conditions. First match wins (with priority ordering). No match → 404 or default route.
- **Trie-based path matching**: For large route tables (thousands of routes), linear scan is O(n). Use a **radix trie (Patricia tree)** for O(k) lookup where k = path segments. Example: `/api/v1/users/{id}/orders` → trie nodes: `api` → `v1` → `users` → `{id}` (wildcard) → `orders`. Path parameters extracted during traversal.
- **Path rewriting**: Transform the request path before forwarding. Examples: strip prefix (`/api/v1/users` → `/users`), add prefix, regex replacement. Useful for API versioning (external path includes version, internal service doesn't).
- **Host-based routing**: Route by `Host` header. Multi-tenant gateway: `api.tenant-a.com` → tenant A's services, `api.tenant-b.com` → tenant B's services. Combined with path routing for finer granularity.
- **Header-based routing**: Route by custom headers (e.g., `X-Api-Version: 2` → v2 service). Useful for A/B testing and canary deployments without changing URLs.
- **Weight-based traffic splitting**: Route a percentage of traffic to different upstreams. Example: 95% to v1, 5% to v2 (canary). Implemented via weighted random selection or consistent hashing with weights. Must be statistically accurate at low traffic volumes (use reservoir sampling or similar).
- **gRPC routing**: Support for HTTP/2-based gRPC requests. Route by gRPC service name and method (from the `content-type: application/grpc` header and `:path` pseudo-header like `/package.ServiceName/MethodName`). Protocol translation: accept REST from external clients, convert to gRPC for internal services (and vice versa).
- **WebSocket routing**: Upgrade HTTP connection to WebSocket. Gateway must handle the `Upgrade` header, maintain the persistent connection, and route to the correct upstream WebSocket server. Challenge: WebSocket connections are long-lived, so they pin resources on the gateway.
- **Hot reload of routing configuration**: Route changes must take effect without restarting the gateway or dropping active connections. NGINX requires `reload` (new worker processes, old ones drain). Envoy supports **xDS (discovery service)** for fully dynamic route updates — no reload needed. Kong stores config in PostgreSQL/Cassandra, polls for changes. Trade-off: polling interval vs push-based updates.
- **Contrast with NGINX**: NGINX uses a static configuration file. Route changes require `nginx -s reload` which spawns new worker processes and gracefully drains old ones. Works well for infrequent changes but not for dynamic environments where routes change every few seconds (e.g., Kubernetes pod scaling).
- **Contrast with Envoy**: Envoy uses **xDS protocol** (Route Discovery Service — RDS) to receive route updates dynamically from a control plane (like Istio). No config file reload needed. Routes update in-memory within seconds. Better for highly dynamic environments but requires a control plane.
- **Contrast with AWS API Gateway**: Routes are defined via CloudFormation / Terraform / Console. Propagation takes seconds to minutes. No hot reload concept — it's managed. Limited to HTTP/REST/WebSocket, no gRPC natively (must use HTTP API with gRPC passthrough).

### 4. 04-rate-limiting-and-throttling.md — Rate Limiting Deep Dive

Rate limiting is the most commonly asked deep-dive topic in API Gateway interviews.

- **Why rate limit?**: Protect backend services from overload, prevent abuse (scraping, brute force), enforce API usage tiers (free: 100 req/min, paid: 10,000 req/min), ensure fair resource allocation across consumers, protect against DDoS (first line of defense before WAF).
- **Rate limiting algorithms**:
  - **Fixed window**: Count requests in fixed time intervals (e.g., per minute starting at :00). Simple. Problem: burst at window boundary — 100 requests at 0:59 + 100 at 1:00 = 200 in 2 seconds while limit is 100/min.
  - **Sliding window log**: Store timestamp of every request. Count requests in the last N seconds. Accurate but memory-intensive (stores every timestamp).
  - **Sliding window counter**: Hybrid of fixed window and sliding log. Use two adjacent fixed windows, weight the previous window's count by the overlap fraction. Example: limit=100/min, previous window had 80 requests, current window has 30 requests, we're 40% through the current window → estimated count = 80 * 0.6 + 30 = 78. Good balance of accuracy and efficiency.
  - **Token bucket**: Bucket holds tokens, refilled at a constant rate. Each request consumes one token. If bucket is empty → reject. Allows bursts up to bucket size. Most commonly used in production (used by AWS, Stripe, etc.).
  - **Leaky bucket**: Requests enter a FIFO queue (the bucket). Processed at a constant rate. Overflow → reject. Smooths traffic to a constant rate. No bursts. Used when backend cannot tolerate any burst.
  - **Comparison table**: Algorithm | Burst handling | Memory | Accuracy | Implementation complexity. Token bucket is the most common for API gateways.
- **Distributed rate limiting**:
  - Single-node rate limiting is easy (in-memory counter). But gateways run as clusters — how to enforce a global rate limit across N gateway nodes?
  - **Centralized counter (Redis)**: Every request increments a counter in Redis. `INCR key` + `EXPIRE key ttl`. Accurate but adds ~1ms latency per request (Redis round-trip). Redis becomes a single point of failure and a bottleneck.
  - **Local counters with sync**: Each gateway node tracks its own counter. Periodically sync with central store. Allows burst up to (limit / N) × N = limit, but individual nodes may slightly over-admit between sync intervals. Trade-off: accuracy vs latency.
  - **Sliding window in Redis with Lua**: Use a Redis sorted set with timestamps as scores. `ZADD key timestamp member`, `ZREMRANGEBYSCORE key 0 (now - window)`, `ZCARD key`. Atomic via Lua script. Accurate sliding window but more Redis operations per request.
  - **Token bucket in Redis**: Store `{tokens, last_refill_time}` in Redis. On each request: compute tokens to add since last refill, subtract one, update. Atomic via Lua script. This is how Stripe implements rate limiting.
  - **Race conditions**: Two requests arrive simultaneously, both read the same counter value, both increment → one request counted twice. Solution: Redis `INCR` is atomic, or use Lua scripts for compound operations. Alternative: use Redis `MULTI/EXEC` or `SET NX`.
- **Rate limit headers**: Standard response headers: `X-RateLimit-Limit` (max requests), `X-RateLimit-Remaining` (remaining in window), `X-RateLimit-Reset` (seconds until window resets), `Retry-After` (seconds to wait when rate limited). Return `429 Too Many Requests` when limited.
- **Multi-dimensional rate limiting**: Apply multiple limits simultaneously: 100 requests/second per IP AND 1,000 requests/minute per API key AND 10,000 requests/hour per organization. Each dimension tracked independently. Request is rejected if ANY limit is exceeded.
- **Rate limiting scope hierarchy**: Global → per-service → per-route → per-consumer → per-IP. More specific limits override broader limits (or stack, depending on policy).
- **Throttling vs rate limiting**: Rate limiting = hard reject above threshold. Throttling = slow down (queue requests, add artificial delay). Some gateways support both: rate limit at the hard ceiling, throttle (add delay) as traffic approaches the limit.
- **Contrast with Kong**: Kong uses a rate-limiting plugin backed by Redis or PostgreSQL. Supports fixed window and sliding window. Configurable per consumer, route, or service. Cluster-wide limits via central data store.
- **Contrast with AWS API Gateway**: AWS provides built-in rate limiting (throttling): account-level (10,000 RPS default, burstable), stage-level, method-level, usage plan-level (for API keys). Uses token bucket. No custom algorithm choice — it's managed. Cannot change the algorithm or back it with your own Redis.
- **Contrast with Envoy**: Envoy supports local rate limiting (per-node, in-memory token bucket) and global rate limiting (calls out to an external rate limit service — `ratelimit` gRPC service). The external service is a separate deployment (often backed by Redis). This separation of data plane (Envoy) and rate limit logic (external service) is clean but adds another hop.

### 5. 05-authentication-and-authorization.md — Auth Layer

- **Authentication methods supported**:
  - **API Key**: Simplest. Client sends key in header (`X-API-Key`) or query param. Gateway looks up key in its consumer registry. Fast (hash lookup) but insecure if transmitted over non-TLS connections. Used for server-to-server calls, public APIs with usage tracking.
  - **JWT (JSON Web Token)**: Client sends a signed JWT in the `Authorization: Bearer <token>` header. Gateway **validates the JWT locally** — verifies signature (RSA/ECDSA public key or HMAC shared secret), checks expiration (`exp` claim), checks issuer (`iss`), checks audience (`aud`). No database lookup needed — the token is self-contained. Revocation is hard (token is valid until it expires unless you maintain a blacklist).
  - **OAuth 2.0**: Gateway acts as the **resource server**. Client obtains an access token from an authorization server (e.g., Auth0, Okta, Keycloak). Gateway validates the token — either by JWT validation (if the token is a JWT) or by **token introspection** (calling the auth server's `/introspect` endpoint to verify the token). Introspection adds latency but supports token revocation.
  - **mTLS (Mutual TLS)**: Both client and server present certificates. Gateway verifies the client certificate against a CA. Used for service-to-service communication (zero-trust architecture). Strong authentication but complex certificate management (rotation, revocation, CA hierarchy).
  - **Basic Auth**: Username + password in `Authorization: Basic <base64>` header. Simple but insecure (credentials sent with every request). Only acceptable over TLS. Rarely used in production APIs.
  - **HMAC Signature**: Client signs the request (method + path + timestamp + body hash) with a secret key. Gateway verifies the signature. Used by AWS (Signature V4), Stripe webhooks. Provides request integrity (tamper-proof) + authentication. More complex for clients to implement.
- **Authorization models**:
  - **RBAC (Role-Based Access Control)**: Assign roles to consumers, roles have permissions. Gateway checks: does this consumer's role allow access to this route? Simple and widely used. Roles defined in JWT claims or looked up from a policy store.
  - **ABAC (Attribute-Based Access Control)**: Policies based on attributes of the subject (user), resource, action, and environment. Example: "Allow access if user.department == 'engineering' AND resource.sensitivity == 'low' AND time.hour > 9 AND time.hour < 17". More flexible than RBAC but more complex to reason about.
  - **OPA (Open Policy Agent)**: Externalize authorization decisions to OPA. Gateway sends the request context to OPA, OPA evaluates Rego policies, returns allow/deny. Decouples policy from gateway code. Policies can be updated without redeploying the gateway. Used by Envoy and Kong.
  - **Scope-based (OAuth scopes)**: JWT contains `scope` claim (e.g., `read:users write:orders`). Gateway checks: does the token's scope include the required scope for this route? Fine-grained per-endpoint authorization.
- **Token caching**: JWT validation is CPU-intensive (signature verification). Cache validated tokens with their claims for the token's remaining TTL. Use a local in-memory cache (LRU) to avoid repeated validation. Token introspection results should also be cached (with shorter TTL to respect revocation).
- **Authentication flow in the gateway**:
  1. Client sends request with credentials (API key, JWT, etc.)
  2. Gateway's auth plugin extracts credentials from the configured location (header, query param, cookie)
  3. Validates credentials (local validation for JWT/API key, external call for OAuth introspection)
  4. On success: injects identity headers (`X-User-Id`, `X-User-Roles`) into the request for upstream services. Upstream services trust these headers (they come from the gateway, not the client).
  5. On failure: return `401 Unauthorized` (no credentials) or `403 Forbidden` (valid credentials, insufficient permissions).
- **Contrast with Kong**: Kong has plugins for key-auth, jwt, oauth2, basic-auth, hmac-auth, ldap-auth. Each is a separate plugin that can be attached to routes or services independently. Kong can act as an OAuth 2.0 provider itself (issuing tokens).
- **Contrast with AWS API Gateway**: AWS supports API keys, Lambda authorizers (custom auth logic in a Lambda function), Cognito authorizers, IAM auth. Lambda authorizers are powerful — any auth logic you can code — but add Lambda cold-start latency (~100-500ms on first call).
- **Contrast with Envoy/Istio**: Envoy supports JWT validation natively (filter `envoy.filters.http.jwt_authn`). For custom auth, Envoy calls an **external authorization service** (ext_authz filter) — a gRPC or HTTP service that returns allow/deny. Istio's `RequestAuthentication` and `AuthorizationPolicy` CRDs provide declarative auth config.

### 6. 06-load-balancing-and-service-discovery.md — Load Balancing & Discovery

- **Load balancing algorithms**:
  - **Round-robin**: Simple rotation across upstream instances. Equal distribution. No awareness of server load or response time. Works well when backends are homogeneous.
  - **Weighted round-robin**: Assign weights to upstreams. A server with weight 3 gets 3x the traffic of a server with weight 1. Useful for heterogeneous backends (bigger server = higher weight) or canary deployments (new version gets low weight initially).
  - **Least connections**: Route to the upstream with fewest active connections. Better for long-lived requests (WebSocket, file uploads). Requires the gateway to track active connections per upstream.
  - **Consistent hashing**: Hash a request attribute (IP, user ID, session cookie) to select an upstream. Same client always goes to the same server (sticky sessions). Minimizes redistribution when servers are added/removed (consistent hashing ring). Used when upstream caches are important (session affinity).
  - **Random with two choices (P2C)**: Pick two random upstreams, route to the one with fewer active connections. Surprisingly effective — provably better than pure random and nearly as good as least-connections, with O(1) decision time. Used by Envoy as default.
  - **Latency-based / EWMA**: Track exponentially weighted moving average of response times per upstream. Route to the fastest. Adapts to transient slowness. Risk: thundering herd to the fastest server.
- **Health checking**:
  - **Active health checks**: Gateway periodically sends probe requests (HTTP GET to `/health`) to each upstream. Configurable: interval (e.g., 5 seconds), timeout, unhealthy threshold (3 consecutive failures → mark unhealthy), healthy threshold (2 consecutive successes → mark healthy again). Consumes upstream bandwidth but provides proactive detection.
  - **Passive health checks (circuit breaking)**: Gateway monitors actual request success/failure rates. If failure rate exceeds threshold (e.g., 50% of last 10 requests fail) → mark upstream unhealthy and stop sending traffic. No extra probe traffic. But detects failures only after real requests fail.
  - **Hybrid**: Use both. Active checks for baseline health. Passive checks for rapid detection of sudden failures. Mark unhealthy on either signal.
  - **Health check endpoint design**: Return HTTP 200 with body `{"status": "healthy", "details": {"db": "ok", "cache": "ok"}}`. Include dependency checks. Return 503 if any critical dependency is down. Shallow check (just return 200 = "process is alive") vs deep check (verify DB, cache, downstream services). Deep checks are more useful but more expensive.
- **Service discovery integration**:
  - **Static configuration**: Upstream addresses hardcoded in config file. Simple, but requires restart/reload to add/remove instances. Suitable for small, stable environments.
  - **DNS-based**: Upstream defined as a DNS name (e.g., `user-service.internal`). DNS returns multiple A records. Gateway resolves on each request or caches with TTL. Problem: DNS TTL delays discovery of new instances. DNS doesn't indicate health.
  - **Consul / etcd / ZooKeeper**: Service registers itself with the registry on startup, deregisters on shutdown, sends heartbeats. Gateway watches the registry for changes. Real-time updates. Consul also provides health checking. Used by Kong (with DNS or Consul integration).
  - **Kubernetes Service / kube-proxy**: In Kubernetes, services are discovered via the Kubernetes API (Endpoints resource) or DNS (`service-name.namespace.svc.cluster.local`). Envoy in Kubernetes uses **EDS (Endpoint Discovery Service)** via Istio's control plane to get real-time endpoint lists.
  - **Eureka (Netflix)**: RESTful service registry. Services self-register, send heartbeats. Clients cache the registry and do client-side load balancing (Ribbon). Peer-to-peer replication between Eureka instances. Used by Netflix's Zuul gateway.
- **Connection pooling**: Gateway maintains persistent connections (keep-alive) to upstream services. Avoids TCP handshake + TLS handshake overhead per request. Pool size tuning: too small → connection contention, too large → exhausts upstream resources. HTTP/2 multiplexing reduces the need for large connection pools (multiple requests over one connection).
- **Retry and timeout**: Configure per-route: connect timeout (time to establish TCP connection, e.g., 1 second), request timeout (time for full response, e.g., 30 seconds), idle timeout (close connections idle for N seconds). Retry policy: retry on 502/503/504, max retries, retry budget (max 20% of requests can be retries — prevents retry storms).
- **Contrast with NGINX**: NGINX's `upstream` block supports round-robin, least_conn, ip_hash, hash (generic key). Active health checks available in NGINX Plus (commercial). Open-source NGINX only has passive health checks. Service discovery via DNS or third-party modules.
- **Contrast with Envoy**: Envoy has first-class service discovery via **EDS (Endpoint Discovery Service)**. Real-time endpoint updates from control plane. Supports all load balancing algorithms listed above plus custom ones. Built-in outlier detection (passive health checking). Connection pool management per upstream cluster. Retry budgets, circuit breaking, and locality-aware routing built in.
- **Contrast with AWS API Gateway**: No load balancing concept — routes to a single integration (Lambda, HTTP endpoint, VPC link). For load balancing behind AWS API Gateway, use ALB/NLB as the integration target. No service discovery — you specify the backend URL.

### 7. 07-circuit-breaking-and-resilience.md — Resilience Patterns

- **Circuit breaker pattern**:
  - **Three states**: Closed (normal, requests flow through) → Open (failure threshold exceeded, all requests immediately rejected with fallback/503) → Half-Open (after cooldown, allow a few probe requests to test recovery).
  - **Transition triggers**: Closed → Open: when failure rate exceeds threshold (e.g., >50% failures in last 10 seconds, or >5 consecutive failures). Open → Half-Open: after a configurable cooldown period (e.g., 30 seconds). Half-Open → Closed: if probe requests succeed. Half-Open → Open: if probe requests fail.
  - **Failure definition**: HTTP 5xx responses, timeouts, connection failures. Configurable: which status codes count as failures, whether timeouts count.
  - **Per-upstream circuit breakers**: Each upstream service gets its own circuit breaker. A failing service doesn't affect routing to healthy services. Avoid a single global circuit breaker.
  - **Metrics tracked**: failure count, success count, failure rate, consecutive failures, time in current state, half-open probe results.
- **Bulkhead pattern**: Isolate resources per upstream. Assign separate connection pools, thread pools, or request quotas to each upstream. If one upstream becomes slow (consuming all connections), it doesn't starve other upstreams. Like watertight compartments in a ship — a breach in one compartment doesn't sink the ship.
  - **Implementation**: Limit max concurrent requests per upstream (e.g., max 100 concurrent requests to `user-service`). If limit reached → immediately reject (503) or queue. Prevents a slow upstream from consuming all gateway capacity.
- **Timeout hierarchy**:
  - **Connection timeout**: Time to establish TCP connection to upstream. Typical: 1-3 seconds. Short — if upstream isn't reachable, fail fast.
  - **Request timeout**: Total time from sending request to receiving full response. Typical: 5-60 seconds depending on the endpoint. Includes upstream processing time.
  - **Idle timeout**: Close a keep-alive connection that has been idle for N seconds. Prevents resource leaks from abandoned connections.
  - **Global request timeout**: Maximum time from client request received to client response sent (including all retries). Prevents infinitely long request processing chains.
- **Retry policy**:
  - Retry on: 502, 503, 504, connection failure, timeout. Do NOT retry on 4xx (client errors) or 500 (server bug — will fail again).
  - **Retry budget**: Limit retries to a percentage of total requests (e.g., 20%). Prevents retry storms where retries amplify load on an already-struggling upstream.
  - **Exponential backoff with jitter**: Delay between retries: `min(cap, base * 2^attempt) + random_jitter`. Jitter prevents synchronized retries from multiple gateway nodes. AWS recommends: base=100ms, cap=10s, full jitter.
  - **Idempotency**: Only retry idempotent requests (GET, PUT, DELETE). Do NOT retry POST (non-idempotent) unless the upstream explicitly supports idempotency keys.
  - **Retry amplification cascade**: Service A retries 3x → Service B retries 3x → Service C receives 9x load. Solution: reduce retries at each layer, use retry budgets, set deadlines (propagate remaining time budget).
- **Fallback responses**: When circuit is open, return a fallback instead of an error. Example: return cached data (stale is better than nothing), return default values, return a degraded response. Depends on the use case — some APIs can't meaningfully degrade (e.g., payment processing).
- **Request hedging**: Send the same request to multiple upstream instances simultaneously, use the first response, cancel the rest. Reduces tail latency. Expensive (doubles load) — only use for latency-critical paths. Google uses this extensively (see "The Tail at Scale" paper).
- **Backpressure propagation**: When the gateway is overwhelmed, signal to clients to slow down. Use `429 Too Many Requests` + `Retry-After` header. Use HTTP/2 `GOAWAY` frame to stop accepting new streams. Prevents cascading failures upstream.
- **Contrast with Hystrix (Netflix)**: Hystrix introduced the circuit breaker pattern to mainstream. Uses separate thread pools per dependency (bulkhead). Provides fallback methods. Tracks rolling statistics (10-second windows, 10 buckets). **Note**: Hystrix is in maintenance mode — Resilience4j is the modern replacement (lighter, functional style, doesn't require dedicated thread pools).
- **Contrast with Envoy**: Envoy has built-in outlier detection (passive circuit breaking based on consecutive 5xx or consecutive gateway errors). Configurable per upstream cluster. Also supports max connections, max pending requests, max retries per cluster (bulkhead). Retry policies are declarative in route config.
- **Contrast with AWS API Gateway**: Limited resilience features. No circuit breaker. Retries for Lambda integrations (2 retries on throttle/5xx). Timeout max 29 seconds for REST APIs, 30 seconds for HTTP APIs. For resilience, you rely on the backend (Lambda retries, SQS dead letter queues) rather than the gateway.

### 8. 08-plugin-and-middleware-architecture.md — Extensibility

- **Plugin execution model**: Requests pass through an ordered chain of plugins (middleware). Each plugin can: inspect/modify the request, inspect/modify the response, short-circuit the chain (return a response immediately, e.g., auth failure → 401), or pass to the next plugin. Similar to Express.js middleware or Java servlet filters.
- **Plugin phases**:
  - **Access phase**: Before proxying. Auth, rate limiting, IP restriction, request validation. Can reject the request.
  - **Header filter phase**: After receiving upstream response headers but before body. Can modify response headers.
  - **Body filter phase**: As response body streams back. Can transform the response body (compression, encryption, scrubbing sensitive data).
  - **Log phase**: After response is sent to client. Logging, metrics, analytics. Cannot modify the response.
- **Plugin types (common)**:
  - **Authentication**: JWT, API key, OAuth, mTLS, Basic auth, LDAP, SAML.
  - **Rate limiting**: Token bucket, sliding window, multi-dimensional.
  - **Request transformation**: Add/remove/rename headers, rewrite path, modify query params, transform body (JSON ↔ XML, add/remove fields).
  - **Response transformation**: Modify response body, add security headers (CORS, CSP, HSTS), strip internal headers.
  - **Logging / observability**: Access logs, request/response body capture, distributed tracing injection (inject/propagate `traceparent` / `X-B3-TraceId` headers).
  - **Caching**: Cache GET responses with configurable TTL, cache key (URL + headers + query params), cache invalidation (purge API, TTL expiry, stale-while-revalidate).
  - **CORS**: Handle preflight (`OPTIONS`) requests, set `Access-Control-Allow-Origin`, `Access-Control-Allow-Methods`, `Access-Control-Allow-Headers`.
  - **IP restriction**: Allow-list or deny-list of client IPs/CIDRs. Geo-IP blocking.
  - **Bot detection**: Identify and block bots via User-Agent analysis, CAPTCHA integration, behavioral analysis.
  - **Request validation**: Validate request body against JSON Schema or OpenAPI spec. Reject malformed requests before they hit the backend. Reduces backend error handling burden.
- **Custom plugin development**: Allow users to write custom plugins in Lua (Kong/NGINX), Go (Kong), JavaScript (Express Gateway), Wasm (Envoy), or any language via external gRPC call (Envoy ext_proc).
  - **Lua (Kong/OpenResty)**: Runs inside the NGINX event loop. Low-overhead, no IPC. But Lua is a niche language and the sandboxed environment limits what you can do (no blocking I/O, limited library access).
  - **Wasm (Envoy)**: Compile plugins from C++, Rust, Go, AssemblyScript to Wasm. Runs in a sandboxed VM inside Envoy. Safe (can't crash the gateway), portable, polyglot. But Wasm overhead is higher than native code, and debugging is harder.
  - **External processing (Envoy ext_proc)**: Gateway sends the request to an external gRPC service for processing. The external service can modify headers, body, or reject the request. Any language. Adds network latency but fully decouples plugin logic from gateway. Good for complex plugins (ML-based bot detection, PII scrubbing).
- **Plugin configuration storage**: Where is plugin configuration stored?
  - **File-based (NGINX)**: Configuration in `nginx.conf`. Requires reload to apply changes.
  - **Database-backed (Kong)**: Configuration in PostgreSQL or Cassandra. Changes apply within seconds (polling interval). Supports multi-node clusters (all nodes read from the same DB). Declarative config export/import (YAML/JSON).
  - **xDS / control plane (Envoy)**: Configuration pushed from control plane (Istio) via xDS protocol. Fully dynamic. No config files, no database — config lives in Kubernetes CRDs or a control plane API.
- **Contrast with Kong**: Kong is built on OpenResty (NGINX + Lua). 100+ plugins available. Custom plugins in Lua or Go. DB-backed config (PostgreSQL/Cassandra) or DB-less mode (declarative YAML). Plugin priority determines execution order (higher priority runs first).
- **Contrast with Envoy**: Envoy uses a filter chain model. Built-in filters for routing, auth, rate limiting, etc. Custom filters via C++ (compiled into Envoy), Wasm (sandboxed), or external processing (gRPC call). xDS-driven configuration. Filter ordering defined in the listener config.
- **Contrast with AWS API Gateway**: Limited extensibility. Custom logic via Lambda authorizers (auth) or Lambda integrations (backend). No plugin architecture — you extend by putting Lambda functions in the path. VTL (Velocity Template Language) for request/response transformation — powerful but arcane syntax.

### 9. 09-observability-and-monitoring.md — Logging, Metrics, Tracing

- **The three pillars of observability**:
  - **Logs**: Structured access logs per request (timestamp, client IP, method, path, status code, latency, upstream, request ID). JSON format for machine parsing. Log levels: error (5xx), warn (4xx), info (2xx/3xx). Log sampling for high-throughput gateways (log 10% of successful requests, 100% of errors).
  - **Metrics**: Time-series numerical data. Key metrics:
    - **Request rate**: Requests per second (total, per route, per upstream, per status code).
    - **Error rate**: 4xx rate, 5xx rate, as percentage and absolute count.
    - **Latency**: p50, p95, p99, p99.9 latency. Break down into: gateway processing time, upstream response time, total end-to-end time. Use histograms (not averages — averages hide tail latency).
    - **Active connections**: Current concurrent connections to clients and to upstreams.
    - **Circuit breaker state**: Open/closed/half-open per upstream. Time spent in each state.
    - **Rate limiter state**: Current counter value per consumer, rejected request rate.
    - **Cache hit/miss rate**: For response caching plugins.
    - Export to Prometheus (pull-based, `/metrics` endpoint) or StatsD/Datadog (push-based).
  - **Distributed tracing**: Inject/propagate trace context headers across the request path. Gateway is the ideal place to start a trace (it's the entry point). Standards: W3C Trace Context (`traceparent` header), Zipkin B3 (`X-B3-TraceId`, `X-B3-SpanId`), Jaeger (`uber-trace-id`). Gateway creates a span with: start time, end time, upstream service, status code, error info. Exports to Jaeger, Zipkin, or OpenTelemetry Collector.
- **Request ID / correlation ID**: Gateway generates a unique ID for each request (`X-Request-Id`). Propagated to all upstream services. Appears in all logs and traces. Enables end-to-end request correlation across microservices. If client sends an `X-Request-Id`, gateway preserves it (don't overwrite).
- **Real-time dashboards**: Visualize request rate, error rate, latency percentiles in real-time (Grafana + Prometheus is the standard stack). Dashboard per route, per upstream, per consumer. Alerting: PagerDuty / OpsGenie integration triggered on: error rate > threshold, p99 latency > threshold, circuit breaker opened, upstream health check failing.
- **Audit logging**: Log all control-plane (admin) actions: who changed which route, when, what was the previous config. Immutable audit trail for compliance (SOX, HIPAA, PCI-DSS). Store in a separate, append-only log (not the same as access logs).
- **Log aggregation**: Gateway logs → Fluentd/Fluent Bit/Filebeat → Elasticsearch/Splunk/CloudWatch Logs. Structured JSON logs enable powerful queries: "show me all 5xx requests to /api/v1/orders in the last hour with latency > 2 seconds."
- **Contrast with Kong**: Kong has plugins for file-log, http-log, tcp-log, udp-log, syslog, datadog, prometheus, zipkin, opentelemetry. Prometheus plugin exposes `/metrics` endpoint with per-route, per-service, per-consumer metrics. Zipkin plugin injects trace headers.
- **Contrast with Envoy**: Envoy has built-in access logging (file, gRPC), metrics (Prometheus via stats filter), and tracing (Zipkin, Jaeger, Datadog, OpenTelemetry). Metrics are extremely granular — per-upstream-cluster, per-route, per-HTTP-method, per-response-code. Envoy is considered the gold standard for proxy observability.
- **Contrast with AWS API Gateway**: CloudWatch Logs for access logs (JSON or CSV). CloudWatch Metrics for request count, latency, 4xx/5xx count (per-stage, per-method). X-Ray for distributed tracing. Limited customization — you get what AWS provides. No Prometheus-native integration.

### 10. 10-scaling-and-performance.md — Scaling & Performance

Cross-cutting doc that ties together scaling decisions from all deep dives.

- **Scale numbers** (for a large-scale API gateway at a company like Stripe, Cloudflare, or Kong):
  - **Request throughput**: 100K-1M+ requests per second per node (depends on plugin chain complexity). NGINX: ~100K RPS with simple proxying. Envoy: ~50-100K RPS with auth + rate limiting + tracing. Kong: ~30-50K RPS with plugins enabled.
  - **Latency overhead**: Gateway adds 1-10ms of latency per request (excluding upstream). Breakdown: TLS termination (~1ms), routing (~0.1ms), auth plugin (~1-3ms for JWT validation, ~5-10ms for token introspection), rate limiting (~1ms with local counter, ~2-3ms with Redis), response transformation (~0.5ms), logging (~0.1ms).
  - **Concurrent connections**: 10K-100K concurrent connections per gateway node (NGINX worker can handle 10K+ connections via epoll/kqueue).
- **Horizontal scaling**: Gateway is stateless (or near-stateless if rate limiting state is externalized to Redis). Scale by adding more nodes behind a load balancer (L4 NLB or DNS round-robin). Each node independently processes requests.
- **L4 vs L7 load balancing in front of the gateway**:
  - **L4 (TCP/NLB)**: Routes based on IP:port. No TLS termination (gateway handles it). Very fast, very cheap. Cannot route by path or header. AWS NLB can handle millions of connections.
  - **L7 (ALB)**: Routes based on HTTP attributes (host, path). Can terminate TLS. More features but more expensive and slightly higher latency. Not needed if the gateway itself is the L7 router.
  - **Recommended**: L4 NLB in front of gateway nodes. Let the gateway handle all L7 concerns (routing, auth, rate limiting). Don't duplicate L7 logic.
- **Performance optimizations**:
  - **Event-driven architecture**: NGINX and Envoy use event-driven, non-blocking I/O (epoll on Linux, kqueue on BSD/macOS). One thread handles thousands of connections. No thread-per-connection overhead. This is why they can handle 10K+ concurrent connections with low memory.
  - **Connection pooling to upstreams**: Reuse TCP/TLS connections. Avoid handshake overhead per request. HTTP/2 multiplexing: multiple requests over a single connection.
  - **TLS session resumption**: Cache TLS sessions (session tickets or session IDs). Reduces TLS handshake from 2 round-trips to 1 on repeat connections. Significant latency savings for high-traffic gateways.
  - **Response caching**: Cache GET responses at the gateway. Cache key: URL + relevant headers. Eliminates upstream calls for cacheable responses. Configurable TTL, stale-while-revalidate. Use `Cache-Control`, `ETag`, `Last-Modified` headers. 80-90% cache hit rate for read-heavy APIs.
  - **Hot path optimization**: Profile the request processing pipeline. Move auth token caching, route lookup (trie), and rate limit checks (local counters) into the hot path (in-memory, no I/O). Push logging, metrics export, and analytics to background threads / async queues.
  - **Zero-copy proxying**: On Linux, use `sendfile()` / `splice()` to transfer data between client and upstream sockets without copying to userspace. NGINX uses this for static file serving. For dynamic proxying, zero-copy is harder but chunked transfer helps.
  - **Kernel bypass (DPDK)**: For extreme performance (millions of RPS), bypass the kernel's network stack entirely using DPDK or io_uring. Used by Cloudflare and specialized network appliances. Overkill for most API gateways.
- **High availability**:
  - **Active-active**: Multiple gateway nodes, all serving traffic. Load balanced by L4 NLB or DNS. Any node can handle any request (stateless). No single point of failure.
  - **Configuration HA**: Config database (PostgreSQL for Kong) must be replicated. If config DB goes down, gateways continue serving with the last-known config (cached locally). New config changes won't propagate until DB recovers.
  - **Redis HA for rate limiting**: Redis Sentinel or Redis Cluster for rate limiting state. If Redis goes down, fall back to local rate limiting (per-node) — less accurate but still functional. Don't let rate limiting infrastructure failure cause gateway failure.
  - **Graceful shutdown**: On shutdown, stop accepting new connections, drain existing connections (wait for in-flight requests to complete), then exit. Kubernetes: pod receives SIGTERM → gateway starts draining → readiness probe fails (LB stops sending traffic) → drain period → SIGKILL.
  - **Zero-downtime deployments**: Rolling update — deploy new version to one node at a time. Blue-green — deploy new version alongside old, switch traffic. Canary — send a small percentage of traffic to new version, monitor, gradually increase. All require the gateway to load new config without dropping connections.
- **Contrast with NGINX**: NGINX's worker process model is highly optimized. Each worker handles connections via epoll. Reload (`nginx -s reload`) is graceful — new workers start, old workers drain. NGINX Plus adds dynamic upstream management, active health checks, and a metrics dashboard.
- **Contrast with Envoy**: Envoy uses a multi-threaded architecture (one event loop per thread). xDS allows fully dynamic configuration without any reload. Hot restart feature: new Envoy process takes over from old process with zero dropped connections. Envoy's stats system is extremely detailed but can be memory-intensive at scale.
- **Contrast with AWS API Gateway**: Fully managed — AWS handles scaling, HA, patches, security. Auto-scales to handle bursts. No capacity planning needed. But: 10,000 RPS default limit (can be increased), 29-second timeout, 10 MB payload limit. For extreme scale (>100K RPS) or low-latency requirements (<5ms), self-hosted may be necessary.

### 11. 11-design-trade-offs.md — Design Philosophy & Trade-off Analysis

Opinionated analysis of API Gateway design choices — not just "what" but "why this and not that."

- **Centralized gateway vs service mesh sidecar**:
  - Centralized gateway: single point of entry for north-south traffic. Simpler to operate (one fleet). But: single point of failure if not HA, doesn't handle east-west (service-to-service) traffic.
  - Service mesh (Istio/Envoy sidecar): every service gets a proxy. Handles both north-south and east-west. Per-service auth, rate limiting, observability. But: operational complexity (sidecar injection, control plane management), resource overhead (CPU/memory per sidecar), latency overhead (extra hop per inter-service call).
  - **Common pattern**: Use both. API gateway at the edge for external traffic (public auth, rate limiting, API versioning). Service mesh internally for service-to-service security, observability, and traffic management. Gateway handles what external clients care about; mesh handles what internal services care about.
- **Self-hosted (Kong/NGINX/Envoy) vs managed (AWS API Gateway)**:
  - Self-hosted: full control over configuration, plugins, performance tuning, and cost. But: operational burden (upgrades, patches, scaling, monitoring), expertise required.
  - Managed: zero operational overhead, auto-scaling, built-in monitoring. But: less flexibility (limited plugins, timeout constraints, vendor lock-in), potentially higher cost at scale (pay-per-request pricing).
  - **Decision criteria**: If you need custom plugins, gRPC, WebSocket, sub-5ms latency, or >100K RPS → self-hosted. If you want simplicity, low operational cost, and standard REST APIs → managed.
- **Configuration in database vs config file vs control plane**:
  - Database (Kong/PostgreSQL): dynamic updates without restart, multi-node consistency, API-driven management. But: database is a dependency (HA required), slight propagation delay.
  - Config file (NGINX): simple, version-controllable (git), no external dependency. But: requires reload for changes, not suitable for frequent changes.
  - Control plane / xDS (Envoy/Istio): fully dynamic, no reload, real-time updates. But: requires a control plane (another system to manage), more complex architecture.
- **Token bucket vs sliding window for rate limiting**: Token bucket allows bursts (good for bursty traffic patterns), sliding window provides smoother rate enforcement (better for protecting sensitive backends). Token bucket is simpler to implement in Redis (two fields). Sliding window is more intuitive for API consumers ("100 requests per minute" means exactly that, no boundary burst).
- **JWT validation (local) vs token introspection (remote)**:
  - JWT local validation: fast (no network call), offline-capable, self-contained. But: no real-time revocation (token valid until expiry). Mitigations: short-lived tokens (5-15 minutes), maintain a revocation blacklist.
  - Token introspection: supports real-time revocation, auth server controls token validity. But: adds network latency per request, auth server is a dependency. Mitigation: cache introspection results with short TTL.
  - **Hybrid**: Use JWTs with short expiry (5 minutes) + refresh tokens. Gateway validates JWT locally. When JWT expires, client refreshes. Revocation takes effect within 5 minutes (acceptable for most use cases).
- **Synchronous auth vs asynchronous auth**:
  - Synchronous: gateway blocks until auth decision is made. Simple. But: auth latency directly impacts request latency.
  - Asynchronous: gateway forwards the request optimistically, auth runs in parallel. If auth fails, the request is already at the upstream. Dangerous for most cases — only viable when the upstream can handle unauthorized requests gracefully.
  - **Recommendation**: Synchronous auth is almost always correct. The latency hit (1-5ms for JWT) is negligible compared to the security risk of forwarding unauthenticated requests.
- **Single gateway vs per-team / per-domain gateways**:
  - Single gateway: one fleet, one config, one team manages it. Simpler. But: blast radius is global (a misconfiguration affects all services), the gateway team becomes a bottleneck (every service change requires gateway config change).
  - Per-team gateways: each team runs their own gateway. Autonomy, smaller blast radius. But: operational duplication, inconsistent configurations across teams.
  - **BFF (Backend for Frontend) pattern**: Separate gateways per client type (web, mobile, TV). Each BFF tailors the API for its client: aggregates calls, transforms responses, handles client-specific auth. Gateway per client type, not per backend team.
- **Gateway as a thin proxy vs gateway as an application layer**:
  - Thin proxy: gateway does routing, TLS, basic auth, rate limiting. Everything else is in the backend services. Simple, fast, low overhead.
  - Application layer: gateway does request aggregation (combine multiple backend calls into one response), response transformation, orchestration, caching, request validation. More powerful but more complex, harder to debug, and gateway becomes a monolith.
  - **Recommendation**: Keep the gateway thin. Move complex logic to backend services or a BFF layer. The gateway should be infrastructure, not application logic.
- **Latency vs safety (retries, circuit breakers)**:
  - More retries = higher availability but higher latency (and potential amplification).
  - Circuit breakers = fail fast (low latency when circuit is open) but temporary unavailability.
  - Trade-off: tune retry count, timeout, and circuit breaker thresholds based on SLO requirements. A payment API has different trade-offs (low retries, strict timeout) than a recommendation API (more retries, relaxed timeout).

## CRITICAL: The design must be focused on API Gateway as infrastructure
An API Gateway is infrastructure, not an application. The design should cover: request routing, rate limiting, authentication, load balancing, circuit breaking, observability, and extensibility. Reference real-world implementations: Kong, Envoy, NGINX, AWS API Gateway, Netflix Zuul. The candidate should demonstrate understanding of WHY each feature exists (what problem it solves) and the trade-offs between different implementation approaches.

## CRITICAL: Iterative build-up (DO NOT skip this)
The architecture MUST evolve iteratively. Each attempt builds on the previous one by identifying a concrete problem and solving it. The candidate should NOT jump to the final architecture.

### Attempt 0: Direct client-to-service communication (no gateway)
- Clients (web, mobile) call each microservice directly. Each service handles its own auth, rate limiting, TLS, CORS.
- **Problems found**: Duplicated cross-cutting concerns in every service, clients must know every service's address, no unified auth or rate limiting, every service exposed to the internet (large attack surface), client must make multiple calls for one page (chatty communication).

### Attempt 1: Simple reverse proxy (NGINX) with static routing
- Single NGINX instance as the entry point. Static config file maps URL paths to upstream services. TLS termination at NGINX. Clients call one address.
- **Problems found**: Static config requires restart for changes, no dynamic service discovery (if a service instance dies, NGINX doesn't know), no auth (each service still handles its own), no rate limiting, single point of failure.

### Attempt 2: Dynamic routing with service discovery
- NGINX or HAProxy replaced with a gateway that integrates with service discovery (Consul/Eureka). Gateway dynamically discovers upstream instances. Active health checks remove unhealthy instances. Load balancing across healthy instances.
- **Problems found**: No auth — any client can call any service, no rate limiting — a single client can overwhelm the system, no logging/tracing — debugging is blind, no circuit breaking — a slow service causes cascading timeouts.

### Attempt 3: Cross-cutting concerns (auth, rate limiting, observability)
- Add authentication (JWT validation or API key lookup) as a gateway plugin/middleware. Add rate limiting (token bucket backed by Redis for distributed state). Add structured access logging and distributed tracing (inject trace IDs). Add CORS handling at the gateway.
- **Problems found**: Rate limiting with centralized Redis adds latency and is a single point of failure. No circuit breaking — when an upstream is down, the gateway keeps sending requests (wasting resources, increasing latency). No response caching — identical GET requests always hit the upstream. No request transformation — clients must match the exact upstream API shape.

### Attempt 4: Resilience and performance (circuit breaking, caching, transformation)
- Add circuit breakers per upstream (open circuit → fast failure, fallback response). Add retry with exponential backoff and jitter. Add response caching for GET requests (cache at gateway, TTL-based, stale-while-revalidate). Add request/response transformation (path rewriting, header manipulation, body transformation). Add WebSocket support for real-time APIs.
- **Problems found**: Single gateway cluster is a single region — regional failure takes down all API traffic. Plugin logic is compiled into the gateway — updating a plugin requires a gateway redeployment. No canary routing for gradual rollouts. Admin API has no audit trail.

### Attempt 5: Production hardening (HA, multi-region, extensibility)
- **High availability**: Multi-node active-active behind an L4 NLB. Graceful shutdown with connection draining. Zero-downtime rolling deployments.
- **Multi-region**: Deploy gateway clusters in multiple regions. DNS-based or Anycast routing to nearest region. Configuration replication across regions.
- **Plugin architecture**: Support custom plugins via Wasm (sandboxed, safe, polyglot) or external processing (gRPC call to separate service). Hot-reload plugins without gateway restart.
- **Canary / traffic splitting**: Weight-based routing for gradual rollouts (95% v1, 5% v2). Header-based routing for internal testing.
- **Audit logging**: Log all admin/config changes with who, what, when. Immutable audit trail.
- **Rate limiting fallback**: If Redis is down, fall back to local (per-node) rate limiting. Less accurate but still functional. Don't let rate limiting infrastructure failure cause gateway failure.

Each attempt MUST:
1. Start by identifying concrete problems with the previous attempt
2. Propose a solution
3. Explain WHY this approach (and why not the alternative — mention real-world gateway implementations where relevant)
4. End with "what's still broken?" to motivate the next attempt

## CRITICAL: Accuracy requirements

I DO NOT TRUST AI-GENERATED FACTS ABOUT SPECIFIC SYSTEMS. Every concrete claim about API gateway internals must be verifiable against official sources. Specifically:

1. **Use WebSearch and WebFetch tools** to look up official documentation and engineering blogs BEFORE writing. Search for:
   - "Kong architecture internals"
   - "Envoy proxy architecture xDS"
   - "NGINX architecture worker process event loop"
   - "AWS API Gateway limits throttling"
   - "Netflix Zuul 2 architecture"
   - "Istio service mesh vs API gateway"
   - "rate limiting algorithms token bucket sliding window"
   - "circuit breaker pattern Hystrix Resilience4j"
   - "distributed rate limiting Redis"
   - "API gateway performance benchmark"
   - "Envoy filter chain Wasm"
   - "Kong plugin development"

   **BLANKET PERMISSION**: You have full permission to use WebSearch and WebFetch to read any online documentation, blog posts, or research papers. Do NOT ask the user for permission to read — just read. This applies to konghq.com, envoyproxy.io, nginx.org, docs.aws.amazon.com, istio.io, and any other reference source. Read as many pages as needed to verify facts.

2. **For every concrete number** (RPS benchmarks, latency overhead, connection limits), verify against official benchmarks or documentation. If you cannot verify a number, explicitly write "[UNVERIFIED — check official docs]" next to it.

3. **For every claim about specific gateway internals** (Kong plugin execution model, Envoy xDS protocol, NGINX reload behavior), if it's not from an official source, mark it as "[INFERRED — not officially documented]".

4. **CRITICAL: Do NOT conflate different gateways.** Each gateway (Kong, Envoy, NGINX, AWS API Gateway, Zuul) has distinct architecture and design philosophy:
   - **Kong**: OpenResty (NGINX + Lua), plugin-oriented, database-backed config
   - **Envoy**: C++, xDS-driven, filter chain, designed for service mesh
   - **NGINX**: C, event-driven workers, config-file-based, reload model
   - **AWS API Gateway**: Managed service, Lambda integration, pay-per-request
   - **Zuul**: Java/Netty (Zuul 2), Netflix OSS, dynamic filters
   When discussing design decisions, ALWAYS explain WHY each gateway made its architectural choice and how the alternatives differ.

## Key API Gateway topics to cover

### Requirements & Scale
- Single entry point for all client requests to a microservices backend
- Handle 100K-1M+ requests per second
- Add <5ms latency overhead per request
- Cross-cutting concerns: auth, rate limiting, logging, tracing, caching
- Dynamic routing with service discovery, health checking
- High availability: no single point of failure
- Extensibility: custom plugins without gateway redeployment

### Architecture deep dives (create separate docs as listed in "Files to create" above)

### Design evolution (iterative build-up — the most important part)
- Attempt 0: No gateway (direct client-to-service)
- Attempt 1: Simple reverse proxy (static routing, TLS termination)
- Attempt 2: Dynamic routing + service discovery + health checks
- Attempt 3: Auth + rate limiting + observability
- Attempt 4: Circuit breaking + caching + transformation
- Attempt 5: Production hardening (HA, multi-region, plugin architecture, hot reload)

### Consistency & Data
- Gateway is mostly stateless — state is externalized (Redis for rate limits, DB for config)
- Rate limiting consistency: distributed counters in Redis, eventual consistency acceptable (slight over-admission is OK, under-admission is not)
- Config propagation: how fast do route changes take effect across all gateway nodes?
- Session affinity: when needed, use consistent hashing (not sticky sessions on the gateway — the gateway should be stateless)

## Contrasts to weave throughout the design

- **Kong vs Envoy**: Kong is application-level (HTTP APIs, plugin marketplace, admin API). Envoy is infrastructure-level (L4+L7, xDS, Wasm, designed for service mesh). Kong is easier to get started with; Envoy is more powerful but harder to operate without a control plane (Istio).
- **Centralized gateway vs sidecar proxy**: Gateway = one fleet at the edge. Sidecar = proxy per service. Gateway for north-south; sidecar for east-west. Most production systems use both.
- **Self-hosted vs managed**: Operational control vs operational simplicity. Cost crossover point depends on traffic volume.
- **NGINX vs Envoy**: NGINX is battle-tested, widely understood, config-file-driven. Envoy is newer, dynamically configured (xDS), richer observability, but steeper learning curve.

## What NOT to do
- Do NOT treat the API gateway as just a reverse proxy — it's a critical infrastructure component with auth, rate limiting, observability, and resilience features.
- Do NOT confuse different gateway implementations — each has a distinct architecture and philosophy.
- Do NOT jump to the final architecture. Build it step by step (Attempt 0 → 5).
- Do NOT make up benchmark numbers — verify against official sources or mark as unverified.
- Do NOT skip the iterative build-up.
- Do NOT write a Wikipedia article. This is an INTERVIEW SIMULATION with dialogue.
- Do NOT describe features without explaining WHY they exist (what problem they solve).
- Do NOT ask the user for permission to read online documentation — blanket permission is granted (see Accuracy requirements section).
