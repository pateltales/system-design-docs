# Rate Limiter — Design Philosophy & Trade-off Analysis

> Opinionated analysis of rate limiter design choices — not just "what" but "why this and not that." Each trade-off has a clear recommendation with reasoning.

---

## Trade-off 1: Token Bucket vs Sliding Window Counter

| | Token Bucket | Sliding Window Counter |
|---|---|---|
| **Burst handling** | Allows controlled bursts (spend accumulated tokens) | No bursts — strictly enforces window-based limits |
| **User experience** | Better — serves burst requests immediately | Stricter — rejects even if average rate is within limit |
| **Memory** | O(1) — 2 values per key | O(1) — 2 counters per key |
| **Accuracy** | Exact for sustained rate, burst is intentional | ~99.997% accurate (Cloudflare measurement) |
| **Configuration** | 2 parameters (rate + burst) | 1 parameter (limit per window) |

### Recommendation

**Token bucket for API rate limiting** — real-world API traffic is bursty. A mobile app might batch 5 requests at app launch. Token bucket serves them immediately from accumulated tokens. This is what AWS API Gateway and Stripe use. [VERIFIED]

**Sliding window counter for strict compliance limits** — "max 1,000 API calls per day, no exceptions." When the contract says N per window and bursts are not acceptable, sliding window counter is more appropriate. This is what Cloudflare uses. [VERIFIED]

### Key Insight
The choice depends on what you're optimizing for: **user experience** (burst-friendly → token bucket) vs **precision** (strict enforcement → sliding window).

---

## Trade-off 2: Centralized Redis vs Local Counters

| | Centralized Redis | Local Counters + Periodic Sync |
|---|---|---|
| **Accuracy** | Exact (atomic operations) | Approximate (stale between syncs) |
| **Per-request latency** | ~1ms (network roundtrip) | ~μs (in-memory check) |
| **Dependency** | Redis is on critical path (SPOF risk) | No per-request external dependency |
| **Over-admission** | None | Up to N × rate × T requests |
| **Throughput ceiling** | ~100K-1M ops/sec (Redis limited) | Unlimited (local memory) |

### Recommendation

**Centralized Redis for external API rate limiting** (Stripe, GitHub) — accuracy matters when you're billing customers for API usage or protecting against abuse. The 1ms overhead is acceptable for APIs with 50-100ms P99 latency. Stripe uses Redis-backed rate limiting. [VERIFIED]

**Local counters for internal traffic shaping** — when the goal is "don't overwhelm Service B" rather than "enforce exact per-client quotas." Approximate enforcement is fine. Google Doorman uses this model with quota leasing. [VERIFIED]

### Key Insight
Accuracy vs latency. For <100K rate limit checks/sec, Redis is fine. For >1M checks/sec, local counters with the Doorman model avoids the Redis bottleneck.

---

## Trade-off 3: Fail Open vs Fail Closed

| | Fail Open (allow all) | Fail Closed (reject all) |
|---|---|---|
| **When Redis is down** | All requests pass — no rate limiting | All requests blocked — total outage |
| **Risk** | Overload during attack + outage | Self-inflicted outage |
| **Availability** | Preserved | Sacrificed |
| **Protection** | Lost temporarily | Maintained |

### Recommendation

**Fail open for most API rate limiting** — a brief period without rate limiting is less damaging than blocking all traffic. Your customers experience degraded rate limiting; with fail closed, they experience a complete outage. Stripe fails open. [VERIFIED — Stripe blog]

**Fail closed for security-critical limits** — login attempt throttling, password reset limits, financial transaction limits. It's better to block logins for 30 seconds (Redis failover time) than to allow brute-force attacks during that window.

### Key Insight
The decision depends on what's worse: **letting too much traffic through** (overload risk) vs **blocking all traffic** (outage). For most APIs, availability > precision. For security, precision > availability.

### Mitigation for Fail Open
- Immediate PagerDuty alert on any fail-open event
- Local in-memory fallback with degraded (per-node) accuracy
- Redis Cluster with automatic failover (typical: <30 seconds)
- Monitor fail-open duration and request volume

---

## Trade-off 4: Per-Request Check vs Quota Allocation

| | Per-Request Check (Redis) | Quota Allocation (Google Doorman) |
|---|---|---|
| **Model** | Check central counter on every request | Get a quota lease, enforce locally |
| **Per-request latency** | ~1ms (Redis roundtrip) | ~μs (in-memory check) |
| **Central dependency** | Every request (critical path) | Periodic only (lease renewal) |
| **Accuracy** | Exact | Approximate (between renewals) |
| **Client cooperation** | Not required | Required (clients must self-enforce) |
| **Complexity** | Simple (Redis INCR/Lua) | Complex (quota allocation algorithm, lease management) |

### Recommendation

**Per-request for <100K checks/sec** — simpler, more accurate, and Redis handles this scale comfortably. This is what most production rate limiters use.

**Quota allocation for >1M checks/sec** — avoids the Redis bottleneck. Requires cooperative clients and a rebalancing mechanism. Google Doorman uses hierarchical quota allocation with ProportionalShare and FairShare algorithms. [VERIFIED — Doorman design doc]

### Key Insight
Simplicity vs throughput. Per-request Redis is the right default. Quota allocation is an optimization for extreme scale — don't reach for it unless Redis is actually the bottleneck.

---

## Trade-off 5: Single-Dimension vs Multi-Dimension

| | Single-Dimension | Multi-Dimension |
|---|---|---|
| **Limits** | One limit per client (e.g., 1,000 req/min) | Multiple simultaneous limits (per-user + per-endpoint + per-IP + global) |
| **Redis calls** | 1 per request | 2-4 per request (pipelined: still ~1ms) |
| **Protection** | Catches simple overuse | Catches nuanced abuse patterns |
| **Complexity** | Simple (one counter check) | Medium (multiple counters, rule matching) |
| **Rule management** | Trivial | Requires rule engine with priorities and conflict resolution |

### Recommendation

**Multi-dimension for production API platforms** — single-dimension has blind spots. A user within their global limit can send ALL requests to one expensive endpoint. Without per-endpoint limiting, this causes targeted overload. Without per-IP limiting, a botnet can spread abuse across many API keys.

**Single-dimension for internal/simple use cases** — if all you need is "Service A can make 5,000 req/s to Service B," single-dimension is sufficient.

### Key Insight
Protection completeness vs implementation complexity. Multi-dimension catches abuse vectors that single-dimension misses. The Redis overhead is negligible (pipelined calls in one roundtrip).

---

## Trade-off 6: Hard Limit vs Soft Limit (Grace Period)

| | Hard Limit | Soft Limit |
|---|---|---|
| **Behavior** | Request N+1 rejected immediately | Allow small grace period (e.g., 10% over for 10 seconds) |
| **Client experience** | Abrupt — may cause errors | Graceful — brief spikes tolerated |
| **Predictability** | Deterministic — limit is the limit | Less predictable — "sometimes I get 1,100, sometimes 1,000" |
| **Implementation** | Simple (if count > limit → reject) | Complex (track grace state, timeout) |

### Recommendation

**Hard limits for external APIs** — predictability is more important than friendliness. Clients can plan around a hard limit. A soft limit that sometimes allows more traffic makes it hard for clients to know what to expect. Stripe uses hard limits with clear documentation. [VERIFIED]

**Soft limits for internal services** — internal traffic is often bursty due to batch jobs, cron tasks, or retry storms. A brief grace period prevents false rejections on internal services.

### Key Insight
Predictability vs friendliness. External clients need deterministic behavior they can code against. Internal services benefit from tolerance for brief spikes.

---

## Trade-off 7: Middleware vs Dedicated Service vs Sidecar

| | Embedded Middleware | Dedicated Service | Sidecar Proxy (Envoy) |
|---|---|---|---|
| **Latency** | ~μs (in-process) | ~1ms (network hop) | ~0.1ms (localhost) |
| **Coupling** | Tight (same process, language) | None (separate service) | Loose (separate process, any language) |
| **Scaling** | Scales with application | Independent scaling | Scales with application |
| **Policy updates** | Requires app redeploy | API-driven, no redeploy | Config reload, no app changes |
| **Language support** | One language per implementation | Language-agnostic | Language-agnostic |

### Recommendation

**Embedded middleware for monoliths** — simplest option when you have one application. No infrastructure overhead.

**Sidecar (Envoy) for microservices** — decouples rate limiting from application code. Consistent enforcement across services regardless of language. Envoy supports both local token bucket and global rate limiting via external gRPC service. [VERIFIED — Envoy docs]

**Dedicated service for multi-tenant platforms** — when the rate limiter IS the product (Stripe, GitHub scale). Independent scaling, independent team, independent release cycle.

### Key Insight
Latency vs operational independence. As your system grows from monolith to microservices to platform, you naturally migrate from middleware → sidecar → dedicated service.

---

## Trade-off 8: Fixed Rules vs Adaptive Rate Limiting

| | Fixed Rules | Adaptive Rate Limiting |
|---|---|---|
| **Limits** | Static (100 req/min, always) | Dynamic (adjusts based on backend health) |
| **Predictability** | High — clients know the exact limit | Lower — "why was I rejected? the limit changed" |
| **Resilience** | Low — continues allowing traffic even if backend is overloaded | High — reduces limits when backend is degrading |
| **Complexity** | Simple (configure once) | Complex (health monitoring, limit adjustment, dampening, feedback loops) |

### Adaptive Rate Limiting Formula

```
health_factor = target_p99 / actual_p99
effective_limit = base_limit × health_factor

Example: target_p99 = 100ms, actual_p99 = 500ms
health_factor = 100/500 = 0.2
effective_limit = 1,000 × 0.2 = 200 req/min (reduced to protect backend)
```

### Recommendation

**Fixed rules as the default** — simpler, predictable, easier to debug. This is what most production rate limiters use.

**Adaptive as an enhancement** — add adaptive limiting on top of fixed rules for critical backends. When the backend is healthy, the adaptive factor is 1.0 (no change). When degrading, it reduces limits automatically.

**Beware feedback loops:** If adaptive limiting reduces limits → backend recovers → limits increase → backend overloads again → repeat. Use dampening (don't restore limits instantly — ramp up over minutes, not seconds) to prevent oscillation.

### Key Insight
Operational simplicity vs resilience. Fixed rules are the right default. Adaptive is a worthwhile addition for systems that experience unpredictable load patterns. Google Cloud client libraries implement client-side adaptive throttling. [VERIFIED — Google Cloud docs describe adaptive throttling]

---

## Trade-off 9: Build vs Buy

| | Build Custom | Buy / Use Managed Service |
|---|---|---|
| **Control** | Full control over algorithms, rules, analytics | Limited to provider's capabilities |
| **Time to deploy** | Weeks to months | Hours to days |
| **Customization** | Multi-dimension, cost-based, adaptive — anything | What the provider offers |
| **Cost** | Engineering time + infrastructure | Subscription/usage fees |
| **Maintenance** | Your team maintains it | Provider maintains it |

### Options Spectrum

| Solution | Customization | Effort |
|---|---|---|
| **AWS API Gateway throttling** | Very low (rate + burst per stage) | Minutes |
| **Cloudflare rate limiting** | Low (IP/URL-based rules) | Minutes |
| **Kong rate limiting plugin** | Medium (Redis-backed, configurable) | Hours |
| **Envoy + ratelimit service** | Medium-High (descriptor-based, extensible) | Days |
| **Custom rate limiter** | Full | Weeks-Months |

### Recommendation

**Managed services for most companies** — API gateway rate limiting (AWS API Gateway, Kong) covers 80% of use cases. Don't build custom unless you have a specific need.

**Custom for multi-tenant API platforms** — if you're Stripe, Twilio, or GitHub, the nuances of multi-dimension limiting, custom tier management, cost-based limiting, and detailed analytics justify custom engineering. The rate limiter is a competitive differentiator for your API platform.

### Key Insight
Build only what differentiates you. Rate limiting is infrastructure — for most companies, a managed solution is the right choice. For API-first companies where the developer experience is the product, custom rate limiting is worth the investment.

---

## Summary: Decision Framework

| Decision | Default Choice | Alternative When |
|---|---|---|
| **Algorithm** | Token bucket | Sliding window counter for strict compliance |
| **Counter store** | Centralized Redis | Local counters for >1M checks/sec |
| **Failure mode** | Fail open | Fail closed for security-critical limits |
| **Check model** | Per-request (Redis) | Quota allocation for extreme throughput |
| **Dimensions** | Multi-dimension | Single-dimension for simple internal use |
| **Limit type** | Hard limits | Soft limits for internal services |
| **Deployment** | Sidecar (Envoy) | Middleware for monoliths, dedicated for platforms |
| **Rule behavior** | Fixed rules | Add adaptive for critical backends |
| **Build/buy** | Managed (gateway/CDN) | Custom for multi-tenant API platforms |

---

*See also: [Interview Simulation](01-interview-simulation.md) for how these trade-offs are discussed in an interview context.*
