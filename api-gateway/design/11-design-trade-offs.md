# Design Trade-offs — Deep Dive

Opinionated analysis of API Gateway design choices — not just "what" but "why this and not that."

---

## 1. Centralized Gateway vs Service Mesh Sidecar

| Dimension | Centralized Gateway | Service Mesh (Istio/Envoy Sidecar) |
|---|---|---|
| Traffic | North-south (external → internal) | East-west (service-to-service) |
| Deployment | One fleet at the edge | Proxy per service (sidecar injection) |
| Auth | External auth (JWT, API keys, OAuth) | Internal auth (mTLS between services) |
| Rate limiting | Public rate limits, API tier enforcement | Service-to-service quotas |
| SPOF | Yes (if not HA), affects all external traffic | No single point, but sidecar adds overhead per service |
| Complexity | One system to manage | Control plane + sidecar per pod = significant operational overhead |

**Common pattern:** Use both. API gateway at the edge for external traffic. Service mesh internally for service-to-service security and observability. They are complementary, not competing.

---

## 2. Self-Hosted vs Managed

| Dimension | Self-Hosted (Kong/NGINX/Envoy) | Managed (AWS API Gateway) |
|---|---|---|
| Control | Full — custom plugins, any protocol, any tuning | Limited — Lambda authorizers, VTL templates, predefined limits |
| Operations | You handle upgrades, patches, scaling, monitoring | Zero operational overhead |
| Cost (low traffic) | Higher (min infrastructure cost) | Lower (pay per request) |
| Cost (high traffic) | Much lower | ~$9M/year at 100K RPS |
| Timeout limits | Configurable (no hard limit) | 29-second hard limit |
| Protocol support | HTTP, gRPC, TCP, WebSocket | HTTP, WebSocket (limited) |
| Vendor lock-in | None | High (AWS-specific) |

**Decision criteria:**
- Need custom plugins, gRPC, WebSocket, sub-5ms latency, or >100K RPS → **self-hosted**
- Want simplicity, low ops cost, standard REST APIs at moderate scale → **managed**

---

## 3. Configuration: Database vs File vs Control Plane

| Model | Gateway | Pros | Cons |
|---|---|---|---|
| **Database** (Kong/PostgreSQL) | Kong | Dynamic updates without restart, API-driven management, multi-node consistency | DB is a dependency (HA required), propagation delay (polling) |
| **Config file** (NGINX) | NGINX | Simple, git-versioned, no external dependency | Requires reload, not suitable for frequent changes |
| **Control plane / xDS** (Envoy/Istio) | Envoy | Fully dynamic, no reload, real-time, push-based | Requires control plane (another system to manage) |

**Trade-off:** Operational simplicity vs dynamic flexibility. For Kubernetes environments with frequent changes, xDS wins. For stable, infrequent-change environments, config files are simpler.

---

## 4. Token Bucket vs Sliding Window for Rate Limiting

| Dimension | Token Bucket | Sliding Window |
|---|---|---|
| Burst handling | Allows bursts up to bucket capacity | No bursts — smooth enforcement |
| Implementation | Simple (2 fields in Redis) | More complex (weighted window calculation) |
| Client mental model | "You can burst, but sustained rate is X/sec" | "You get exactly X requests per minute" |
| Backend protection | Less predictable (bursts hit backend) | More predictable (smooth traffic) |

**When to use token bucket:** Bursty traffic patterns (flash sales, mobile app launches), when burst tolerance is desirable.

**When to use sliding window:** Protecting sensitive backends (payment processing), when exact rate enforcement matters to API consumers.

**Industry default:** Token bucket (used by AWS, Stripe, GitHub).

---

## 5. JWT Validation (Local) vs Token Introspection (Remote)

| Dimension | JWT Local Validation | Token Introspection |
|---|---|---|
| Latency | ~1 ms (crypto only) | ~5-50 ms (network call to auth server) |
| Revocation | Not immediate — token valid until `exp` | Immediate — auth server checks revocation |
| Dependency | None (offline-capable) | Auth server must be available |
| Scalability | Unlimited (no external calls) | Auth server is a bottleneck |

**Mitigations for JWT revocation:**
- Short-lived tokens (5-15 minutes) + refresh tokens
- Token blacklist in Redis at gateway (checked per-request, adds ~1ms)
- Token versioning (bump user version, reject old tokens)

**Hybrid (recommended):** JWTs with short expiry (5 min) + refresh tokens. Gateway validates locally. On expiry, client refreshes. Revocation effective within 5 minutes (acceptable for most cases).

---

## 6. Synchronous Auth vs Asynchronous Auth

| Dimension | Synchronous | Asynchronous |
|---|---|---|
| Flow | Gateway blocks until auth complete | Gateway forwards optimistically, auth in parallel |
| Latency impact | Auth latency directly impacts request latency | Lower latency (auth in parallel) |
| Security risk | None — unauthenticated requests never reach upstream | High — upstream receives potentially unauthenticated requests |

**Recommendation:** Synchronous auth is almost always correct. The 1-5ms hit for JWT validation is negligible compared to the security risk of forwarding unauthenticated requests. Async auth only viable when upstream can gracefully handle unauthorized requests (extremely rare).

---

## 7. Single Gateway vs Per-Team / Per-Domain Gateways

| Dimension | Single Gateway | Per-Team Gateways |
|---|---|---|
| Operations | One fleet, one config, one team | Each team operates their own |
| Blast radius | Global — misconfiguration affects all services | Scoped — misconfiguration affects one team |
| Bottleneck | Gateway team is a bottleneck for all changes | Teams are autonomous |
| Consistency | Uniform policies across all APIs | Inconsistent configurations |
| Cost | Efficient resource utilization | Operational duplication |

**BFF (Backend for Frontend) pattern:** Separate gateways per client type (web, mobile, TV). Each BFF tailors the API: aggregates calls, transforms responses, handles client-specific auth. Gateway per client type, not per backend team.

---

## 8. Thin Proxy vs Application Layer Gateway

| Dimension | Thin Proxy | Application Layer |
|---|---|---|
| Gateway does | Routing, TLS, basic auth, rate limiting | + Request aggregation, response transformation, orchestration, caching |
| Complexity | Low, fast, focused | High, harder to debug |
| Performance | Very fast — minimal processing | Slower — more processing per request |
| Maintainability | Simple — infrastructure concern | Complex — becomes a monolith with business logic |

**Recommendation:** Keep the gateway thin. Move complex logic to backend services or a BFF layer. The gateway should be **infrastructure, not application logic**.

Exceptions:
- Response caching (appropriate for gateway)
- CORS handling (cross-cutting, belongs in gateway)
- Request validation against OpenAPI spec (prevents malformed requests from reaching backend)

---

## 9. Latency vs Safety (Retries, Circuit Breakers)

| Dimension | More Retries | Fewer Retries |
|---|---|---|
| Availability | Higher (transient failures masked) | Lower (failures visible to client) |
| Latency (success) | Same | Same |
| Latency (failure) | Higher (wait for retries to exhaust) | Lower (fail fast) |
| Backend load | Higher (retries amplify load) | Lower |

| Dimension | Circuit Breaker | No Circuit Breaker |
|---|---|---|
| Failure response | Fast (503 immediately when open) | Slow (wait for timeout on every request) |
| Recovery detection | Probes in half-open state | No detection — keep hammering failing service |
| Complexity | More code, more config | Simpler |

**Tune by SLO requirements:**
- **Payment API:** Low retries (1), strict timeout (5s), aggressive circuit breaker (trip on 3 failures). Correctness > availability.
- **Recommendation API:** More retries (3), relaxed timeout (30s), lenient circuit breaker. Availability > strictness. A slow recommendation is better than no recommendation.

---

## 10. Summary: Decision Framework

```
Q: Need external traffic control?        → API Gateway
Q: Need internal service-to-service?      → Service Mesh
Q: Need both?                             → Both (most production systems)

Q: Need full control + high scale?        → Self-hosted (Kong, Envoy, NGINX)
Q: Want zero ops + moderate scale?        → AWS API Gateway

Q: Routes change frequently (K8s)?        → Envoy (xDS, dynamic)
Q: Routes change rarely?                  → NGINX (config file, simple)
Q: Want API management platform?          → Kong (admin API, plugin hub)

Q: Need burst tolerance for rate limiting?→ Token bucket
Q: Need exact rate enforcement?           → Sliding window

Q: Auth latency critical + can tolerate   → JWT local validation
   delayed revocation?
Q: Need instant revocation?               → Token introspection (+ caching)

Q: Business logic at the edge?            → Keep gateway thin, use BFF
Q: Multiple client types (web, mobile)?   → BFF per client type
```

The right answer is always "it depends on your requirements." But this framework gives you a systematic way to think through each decision in an interview.
