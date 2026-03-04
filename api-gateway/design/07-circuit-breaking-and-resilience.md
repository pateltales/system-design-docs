# Circuit Breaking & Resilience — Deep Dive

---

## 1. Circuit Breaker Pattern

The circuit breaker stops the gateway from repeatedly sending requests to a failing upstream. Instead of letting failures accumulate, the circuit "trips" and fails fast.

### Three States

```
                failure threshold
                   exceeded
  ┌────────┐ ─────────────────► ┌────────┐
  │ CLOSED │                     │  OPEN  │
  │        │ ◄───────────────── │        │
  └────────┘   reset timeout    └────────┘
      ▲          expires              │
      │                               │
      │         ┌───────────┐         │
      └──────── │ HALF-OPEN │ ◄───────┘
       probe    └───────────┘  timer expires
      succeeds                 allow probe
```

| State | Behavior | Transition |
|---|---|---|
| **Closed** | All requests pass through. Failures counted in sliding window. | Failure rate > threshold (e.g., 50% in 60s) → Open |
| **Open** | All requests fail immediately with 503 or fallback. No traffic to upstream. | After cooldown (e.g., 30s) → Half-Open |
| **Half-Open** | Limited probe requests allowed. Rest still fail fast. | Probes succeed → Closed. Probes fail → Open. |

### What Counts as a Failure?

| Counted as Failure | NOT Counted |
|---|---|
| HTTP 500, 502, 503, 504 | HTTP 400, 401, 404, 409 |
| Connection refused / reset | HTTP 429 (debatable) |
| TCP timeout | Any other 4xx |
| TLS handshake failure | |

### Per-Upstream Circuit Breakers

Each upstream gets its own independent circuit breaker:

```
Gateway
  ├── user-service      [CLOSED]    ← healthy
  ├── order-service     [OPEN]      ← failing, fast-fail all requests
  ├── payment-service   [HALF-OPEN] ← probing recovery
  └── inventory-service [CLOSED]    ← healthy
```

If order-service is down, only those requests are affected. Without per-upstream isolation, a single failing service degrades everything.

---

## 2. Bulkhead Pattern

Named after watertight compartments in ships. One compartment floods, others stay sealed.

### The Problem Without Bulkheads

```
Gateway (shared connection pool: 500 connections)

  order-service is slow (5s per request)
  └── 480 connections stuck waiting
  └── user-service, payment-service share remaining 20
  └── Entire gateway appears degraded
```

### Resource Isolation Per Upstream

```
Gateway
  ├── user-service      pool: [150 connections]
  ├── order-service     pool: [150 connections]
  ├── payment-service   pool: [100 connections]
  └── inventory-service pool: [100 connections]
```

When order-service is slow:

```
  ├── user-service      pool: [20/150 used]   ← unaffected
  ├── order-service     pool: [150/150 FULL]  ← isolated, only this saturated
  │     └── New requests → 503 immediately
  ├── payment-service   pool: [15/100 used]   ← unaffected
  └── inventory-service pool: [10/100 used]   ← unaffected
```

**Bulkhead + circuit breaker together:** Bulkhead limits blast radius while upstream degrades. Circuit breaker detects degradation and stops traffic entirely. Without bulkhead, circuit breaker trips too late. Without circuit breaker, bulkhead pool stays saturated.

---

## 3. Timeout Hierarchy

```
|←── Global Request Timeout (e.g., 30s) ──────────────────→|
              |←── Connection Timeout (2s) ──→|
              |←──── Request Timeout (10s) ───────────────→|
              |     Idle Timeout (60s between data chunks)  |
```

| Timeout | Typical | Purpose |
|---|---|---|
| Connection timeout | 1-3s | TCP connection establishment. Short — fail fast. |
| Request timeout | 5-60s | Full response time. Route-specific. |
| Idle timeout | 30-120s | Close idle keep-alive connections. |
| Global request timeout | 10-60s | Hard cap including retries. |

**Key principle:** Timeouts must decrease as you go deeper. If gateway gives client 30s, inner service calls must have shorter timeouts.

---

## 4. Retry Policy

### What to Retry

| Retry | Do NOT Retry |
|---|---|
| 502, 503, 504 | 400 (client error, won't change) |
| Connection refused/reset | 401/403 (auth won't fix itself) |
| | 500 (likely a bug — will fail again) |
| | POST without idempotency key |

### Idempotency

| Method | Safe to Retry? | Why |
|---|---|---|
| GET | Yes | Read-only |
| PUT | Yes | Idempotent by definition |
| DELETE | Yes | Deleting already-deleted = no-op |
| POST | **NO** (unless idempotency key) | Could create duplicates |
| PATCH | **NO** (usually) | May not be idempotent |

### Retry Budget

Retries cannot exceed a percentage of total requests:

```
Retry Budget = 20% of successful requests

100 requests/sec succeed → max 20 retries/sec
Prevents retry storms during widespread outages
```

### Exponential Backoff with Jitter

```
delay = min(cap, base × 2^attempt) + random_jitter

base = 100ms, cap = 10s

Attempt 0: ~47ms
Attempt 1: ~183ms
Attempt 2: ~291ms
Attempt 3: ~612ms
```

**Jitter is critical.** Without it, all clients that failed at the same time retry at the same time, recreating the overload.

### Retry Amplification Cascade

The most dangerous failure mode:

```
Client → Service A → Service B → Service C

A retries 3x to B, B retries 3x to C:
  C receives 3 × 3 = 9 requests for every 1 original

With 4 layers: 3^3 = 27x amplification
```

**Mitigations:**
- Retry budget (20% cap) at each layer
- Only retry at the edge (gateway retries, inner services don't)
- Propagate deadlines (if 2s remains, don't retry)
- Circuit breakers at each layer

---

## 5. Fallback Responses

When circuit is open:

| Strategy | When | Example |
|---|---|---|
| Cached data | Stale-tolerant reads | Product catalog returns last-known prices |
| Default values | Feature flags, config | Return default feature flag values |
| Degraded response | Aggregation endpoints | User profile without recommendations |
| Static response | Status pages | Pre-configured "service degraded" page |
| Queue for later | Write operations | Accept request, queue, process on recovery |

Response should include staleness indicators:
- `X-Cache: HIT (stale)`
- `X-Fallback: true`
- `Age: 3600`

---

## 6. Request Hedging

Send the same request to multiple instances simultaneously. Use first response, cancel rest.

```
Gateway ──┬──► Instance A (50ms)   ← use this
          ├──► Instance B (200ms)  ← cancel
          └──► Instance C (800ms)  ← cancel
```

| Metric | Without Hedging | With Hedging (2 instances) |
|---|---|---|
| p50 latency | 10ms | ~10ms (no change) |
| p99 latency | 500ms | ~50ms (dramatic improvement) |
| Load on upstream | 1x | 2x (doubled) |

**Delayed hedging** (sweet spot): Send to A. If no response in p50 time (10ms), send to B. Most requests complete before hedge fires — extra load is ~5%.

**When NOT to hedge:** Non-idempotent requests, upstream already overloaded, expensive API calls.

---

## 7. Backpressure Propagation

Signal clients to slow down rather than silently dropping requests.

**HTTP/1.1:** `429 Too Many Requests` + `Retry-After` header.

**HTTP/2:**
- `WINDOW_UPDATE` — control data in flight
- `GOAWAY` — stop new streams, client reconnects (graceful drain)
- `MAX_CONCURRENT_STREAMS` — limit parallel requests per connection

**Load shedding priority** (keep first, shed last):
1. Health checks
2. Authentication requests
3. Read requests (GET)
4. Write requests (POST/PUT/DELETE)
5. Background/batch requests

---

## 8. Contrasts

### Hystrix vs Resilience4j

| Aspect | Hystrix (Netflix) | Resilience4j |
|---|---|---|
| Status | Maintenance mode (since 2018) | Actively maintained |
| Isolation | Thread pool per dependency (heavyweight) | Semaphore-based (lightweight) |
| Circuit breaker | Count-based sliding window | Count-based or time-based |
| Overhead | Higher (dedicated thread pools) | Lower (no extra threads) |
| Style | Annotation-driven | Functional, composable |

**Why Hystrix retired:** Thread-pool-per-dependency is expensive with 100 upstream dependencies. Modern reactive/async frameworks don't need thread isolation.

### Envoy

- **Circuit breaking:** Per-cluster limits: `max_connections`, `max_pending_requests`, `max_requests`, `max_retries`
- **Outlier detection:** Auto-eject unhealthy hosts (consecutive 5xx). 30s default ejection.
- **Panic threshold:** If >50% hosts ejected, disable outlier detection (use all hosts). Prevents total blackout.
- Retry backoff: base 25ms, max 250ms (configurable)

### AWS API Gateway

- **No circuit breaker** built-in
- Retries: 2 retries for Lambda (fixed, not configurable). No retries for HTTP integrations.
- Timeout: Max 29 seconds (REST) / 30 seconds (HTTP APIs)
- For resilience, rely on backend (Lambda retries, SQS dead letter queues)

---

## Summary: How Patterns Work Together

```
Incoming Request
       │
       ▼
┌──────────────┐
│ Rate Limiter │──── over limit ──► 429 + Retry-After
└──────┬───────┘
       │
       ▼
┌──────────────┐
│  Bulkhead    │──── pool full ───► 503
└──────┬───────┘
       │
       ▼
┌──────────────┐
│ Circuit      │──── OPEN ────────► Fallback (cache/default/503)
│ Breaker      │
└──────┬───────┘
       │ CLOSED
       ▼
┌──────────────┐
│ Send Request │──── timeout ─────► Retry? (budget, idempotency, backoff)
└──────┬───────┘
       │
       ▼
  200 OK Response
```

Each layer provides different protection. Together they ensure failures are detected quickly, blast radius is limited, and the system degrades gracefully.
