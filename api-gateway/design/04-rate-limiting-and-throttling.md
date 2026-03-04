# Rate Limiting & Throttling — Deep Dive

Rate limiting is the most commonly asked deep-dive topic in API Gateway interviews.

---

## 1. Why Rate Limit?

| Goal | Example |
|---|---|
| **Protect backend services** | Prevent a single client from overwhelming a service with 100K RPS |
| **Prevent abuse** | Stop scraping, brute-force login attempts, credential stuffing |
| **Enforce API usage tiers** | Free: 100 req/min, Pro: 10,000 req/min, Enterprise: unlimited |
| **Fair resource allocation** | Prevent one noisy consumer from starving others |
| **DDoS first line of defense** | Reject obvious floods before they reach WAF/backend |

---

## 2. Rate Limiting Algorithms

### 2.1 Fixed Window

Count requests in fixed time intervals (e.g., per minute starting at :00).

```
Window: 1 minute, Limit: 100

 :00             :01             :02
  |── Window 1 ──|── Window 2 ──|
  counter = 0→100  counter = 0→..

Problem — boundary burst:
  99 requests at 0:59 + 99 requests at 1:00
  = 198 requests in 2 seconds
  Both windows see < 100, so both pass!
```

**Pros:** Simple, O(1) memory per key (one counter + one timestamp).
**Cons:** The boundary burst problem allows up to 2x the intended rate.

### 2.2 Sliding Window Log

Store the timestamp of every request. Count requests in the last N seconds.

```
Window: 60 seconds, Limit: 100

Sorted set of timestamps:
  [t=1.2, t=1.5, t=3.8, t=5.1, ..., t=58.9, t=59.2]

New request at t=61.0:
  1. Remove entries older than t=1.0 (61.0 - 60)
  2. Count remaining entries: 97
  3. 97 < 100 → ALLOW, add t=61.0 to the set
```

**Pros:** Perfectly accurate — no boundary burst.
**Cons:** O(n) memory per key (stores every timestamp). At 100 req/min per consumer with 10,000 consumers = 1 million timestamps in memory.

### 2.3 Sliding Window Counter

Hybrid of fixed window and sliding log. Uses two adjacent fixed windows, weighted by overlap.

```
Window: 60 seconds, Limit: 100
Previous window count: 80
Current window count:  30
Current position: 40% through current window

Estimated count = previous_count × (1 - position) + current_count
               = 80 × 0.6 + 30
               = 48 + 30
               = 78
78 < 100 → ALLOW
```

**Pros:** O(1) memory (two counters), ~99.997% accurate (per Cloudflare's analysis).
**Cons:** Approximate — not perfectly exact. But close enough for production use.

### 2.4 Token Bucket

Bucket holds tokens, refilled at a constant rate. Each request consumes one token. Empty bucket → reject. **This is the most commonly used algorithm in production** (AWS, Stripe, GitHub).

```
Bucket: capacity=10, refill_rate=2/sec

t=0.0:  tokens=10       Request → ALLOW, tokens=9
t=0.0:  tokens=9        Request → ALLOW, tokens=8
  ... (8 more rapid requests)
t=0.0:  tokens=1        Request → ALLOW, tokens=0
t=0.0:  tokens=0        Request → REJECT (429)

t=0.5:  refill: 0 + (0.5 × 2) = 1 token
        tokens=1         Request → ALLOW, tokens=0

t=5.0:  refill: 0 + (5.0 × 2) = 10 tokens (capped at capacity)
        tokens=10        (bucket full again)
```

**Key property:** Allows bursts up to bucket capacity, but sustains only at the refill rate. A bucket with capacity=100 and refill_rate=10/sec allows a burst of 100 but sustains 10 req/sec.

**Pros:** Allows controlled bursts, O(1) memory (2 fields: tokens + last_refill_time), simple to implement.
**Cons:** Burst tolerance may be undesirable for sensitive backends.

### 2.5 Leaky Bucket

Requests enter a FIFO queue (the bucket). Processed at a constant rate. Overflow → reject.

```
Queue capacity: 5, drain_rate: 1 req/sec

3 requests arrive at t=0: Queue: [R1, R2, R3]
R1 processed at t=0, R2 at t=1, R3 at t=2
3 more arrive at t=0.5: Queue: [R2, R3, R4, R5, R6] (size=5, FULL)
R7 arrives → REJECT (queue overflow)
```

**Key difference from token bucket:** Leaky bucket enforces a perfectly smooth output rate. Token bucket allows bursts.

**Pros:** Smooth output rate, backend sees constant load.
**Cons:** No burst tolerance, added latency (requests wait in queue).

### Algorithm Comparison

| Algorithm | Burst Handling | Memory | Accuracy | Best For |
|---|---|---|---|---|
| Fixed Window | Poor (2x boundary) | O(1) | Low | Quick implementation |
| Sliding Window Log | None (exact) | O(n) | Perfect | Low-volume, precision-critical |
| Sliding Window Counter | Controlled | O(1) | High (~99.997%) | Production HTTP rate limiting |
| Token Bucket | Allows bursts | O(1) | High | General-purpose API limiting |
| Leaky Bucket | No bursts (smooth) | O(queue) | High | Burst-sensitive backends |

**Interview default:** Token bucket for most API gateway use cases. Mention sliding window counter as the alternative if burst tolerance is not needed.

---

## 3. Distributed Rate Limiting

Single-node rate limiting is trivial (in-memory counter). The problem gets hard with N gateway nodes.

```
             Client (100 req/s limit)
                    |
               Load Balancer
              /      |      \
         Node A    Node B    Node C
         (33 req)  (33 req)  (34 req)

Without coordination:
  Each node allows 100 → 300 req/s globally!
```

### 3.1 Centralized Counter (Redis)

The most common approach. Use Redis as a shared, atomic counter.

```
-- Fixed window with Redis INCR
key = "ratelimit:{client_id}:{window_start}"

current_count = REDIS.INCR(key)      -- atomic increment

if current_count == 1:
    REDIS.EXPIRE(key, window_seconds)  -- set TTL on first request

if current_count > limit:
    REJECT (429)
else:
    ALLOW
```

**Why INCR works:** Redis `INCR` is atomic. Even with 100 concurrent requests from 50 nodes, each gets a unique, sequential counter value. No race condition.

**Trade-offs:**
- Adds 1-3ms latency per request (Redis round-trip)
- Redis becomes a dependency — if Redis is down, rate limiting is down
- Redis must be highly available (Sentinel or Cluster)

### 3.2 Local Counters with Periodic Sync

Each node tracks its own counter, periodically syncs with central store.

```
Node A: local=40, synced_global=120
Node B: local=35, synced_global=120
Node C: local=45, synced_global=120

Sync interval: every 5 seconds
Between syncs: estimates are stale
Worst case over-admission: (N-1) × requests_per_sync_interval
```

**Trade-offs:** Lower latency (local memory), less accurate. Acceptable for "soft" limits.

### 3.3 Token Bucket in Redis (Stripe's Approach)

Store token bucket state in Redis. Compute refills lazily (only on request).

```lua
-- Redis Lua script (atomic execution)
local key = KEYS[1]
local now = tonumber(ARGV[1])
local max_tokens = tonumber(ARGV[2])
local refill_rate = tonumber(ARGV[3])

-- Get current bucket state
local bucket = redis.call('HMGET', key, 'tokens', 'last_refill')
local tokens = tonumber(bucket[1]) or max_tokens
local last_refill = tonumber(bucket[2]) or now

-- Calculate tokens to add since last refill
local elapsed = math.max(0, now - last_refill)
tokens = math.min(max_tokens, tokens + elapsed * refill_rate)

-- Try to consume
if tokens >= 1 then
    tokens = tokens - 1
    redis.call('HMSET', key, 'tokens', tokens, 'last_refill', now)
    redis.call('EXPIRE', key, math.ceil(max_tokens / refill_rate) * 2)
    return {1, math.floor(tokens)}  -- ALLOWED, remaining
else
    redis.call('HMSET', key, 'tokens', tokens, 'last_refill', now)
    return {0, 0}  -- REJECTED
end
```

**Why Lua?** Redis executes Lua scripts atomically — no other command can interleave. Eliminates the race condition where two requests both read count=99 and both get allowed.

**Why this is Stripe's approach:** Stripe processes millions of API calls per second. Token bucket in Redis gives per-merchant burst tolerance (important for flash sales), lazy refill (no background jobs), and atomic execution.

### 3.4 Race Conditions

```
Time    Node A                    Node B
----    ------                    ------
t=0     GET counter → 99          GET counter → 99
t=1     99 < 100, ALLOW           99 < 100, ALLOW
t=2     SET counter = 100         SET counter = 100

Both allowed! Actual count = 101 (exceeds limit)
```

**Solutions:**

| Solution | How | Drawback |
|---|---|---|
| Redis INCR | Atomic increment-and-return | Single operation only |
| Lua scripting | Entire read-compute-write runs atomically | Blocks Redis briefly |
| MULTI/EXEC | Batch commands atomically | No conditional logic inside transaction |

**Recommendation:** Use `INCR` for simple counting. Use Lua scripts for anything more complex. Never use GET-then-SET.

---

## 4. Rate Limit Headers & HTTP Semantics

```
HTTP/1.1 200 OK
X-RateLimit-Limit: 100
X-RateLimit-Remaining: 57
X-RateLimit-Reset: 1700000060

---

HTTP/1.1 429 Too Many Requests
X-RateLimit-Limit: 100
X-RateLimit-Remaining: 0
X-RateLimit-Reset: 1700000060
Retry-After: 23

{
  "error": "rate_limit_exceeded",
  "message": "Rate limit exceeded. Try again in 23 seconds."
}
```

| Header | Description |
|---|---|
| `X-RateLimit-Limit` | Max requests allowed in current window |
| `X-RateLimit-Remaining` | Requests remaining in current window |
| `X-RateLimit-Reset` | Unix timestamp when window resets |
| `Retry-After` | Seconds to wait before retrying (only on 429) |

Always mention 429 and these headers in interviews — shows you think about the client contract.

---

## 5. Multi-Dimensional Rate Limiting

Apply multiple limits simultaneously. A request must pass ALL of them.

```yaml
rate_limits:
  - key: client_ip        limit: 100/s     reason: Prevent single-IP floods
  - key: api_key           limit: 1000/min  reason: Per-developer plan limits
  - key: organization_id   limit: 10000/hr  reason: Per-org quotas
  - key: global            limit: 50000/s   reason: System capacity protection
```

**Why multiple dimensions?**

```
Scenario: API key "abc123" shared across a botnet of 10,000 IPs

Per-IP only:     Each IP sends 5 req/s (under 100/s limit) → all pass
Per-API-key:     50,000 req/s total → CAUGHT and blocked

Scenario: Attacker rotates API keys from single IP

Per-key only:    Each key sends 1 req/s → all pass
Per-IP:          10,000 req/s from one IP → CAUGHT and blocked
```

Neither dimension alone is sufficient. Defense in depth requires multiple limits.

---

## 6. Scope Hierarchy

```
                Global          50,000 req/s across entire gateway
                  |
              Per-Service       10,000 req/s to "payments" service
                  |
              Per-Route         500 req/s to POST /payments/charge
                  |
             Per-Consumer       100 req/min for API key "abc123"
                  |
               Per-IP           20 req/s from 1.2.3.4
```

A request must pass limits at every level. The most restrictive applicable limit wins.

---

## 7. Throttling vs Rate Limiting

| Aspect | Rate Limiting | Throttling |
|---|---|---|
| Action on excess | Hard reject (429) | Slow down (queue, delay, degrade) |
| Client experience | Binary: allowed or denied | Gradual: response gets slower |
| When to use | Billing, abuse prevention, hard limits | User-facing, graceful degradation |

**Throttling strategies:**
- **Queue with backpressure**: Excess requests queued, processed as capacity frees up. Client sees higher latency but gets a response.
- **Artificial delay**: `delay = (count - soft_limit) × 100ms`. Slows client down proportionally.
- **Priority degradation**: Serve from cache, return reduced-fidelity response, skip enrichment.

---

## 8. Real-World Implementations

### Kong

- Supports **fixed window** and **sliding window** algorithms
- Backend stores: Redis (distributed) or PostgreSQL
- `fault_tolerant: true` → if Redis is down, allow requests (fail open)
- Configurable per-consumer, per-route, or per-service

### AWS API Gateway

- Built-in **token bucket** algorithm
- Default: 10,000 RPS account-level, 5,000 burst capacity per region
- Usage Plans for API key-based limits (throttle + quota)
- No custom algorithm choice — it's managed

### Envoy

- **Local rate limiting**: Per-node, in-memory token bucket. Zero external dependencies.
- **Global rate limiting**: Calls external gRPC rate limit service (backed by Redis). Accurate across cluster but adds latency.
- Can combine both: local as first pass, global for precise enforcement.

### Comparison

| Feature | Kong | AWS API Gateway | Envoy |
|---|---|---|---|
| Algorithm | Fixed / Sliding window | Token bucket | Token bucket (local) / Configurable (global) |
| Distributed store | Redis or PostgreSQL | AWS-managed (internal) | Redis (via external gRPC service) |
| Burst support | Limited | Yes | Yes |
| Granularity | Consumer, IP, header, path | API key, account | Arbitrary descriptors |
| Failure mode | Configurable (open/closed) | Fail closed (managed) | Configurable |

---

## 9. Interview Checklist

```
[ ] WHY:           State goals (protect backend, abuse, billing, fairness, DDoS)
[ ] ALGORITHM:     Token bucket (default), sliding window counter (if precision needed)
[ ] DISTRIBUTED:   Redis + Lua scripts, mention atomicity and race conditions
[ ] HEADERS:       429, X-RateLimit-*, Retry-After
[ ] MULTI-DIM:     Per-IP, per-key, per-org — must pass ALL
[ ] SCOPE:         Global → service → route → consumer → IP
[ ] THROTTLING:    Distinguish from hard reject (queue, delay, degrade)
[ ] REAL-WORLD:    Reference Stripe (token bucket + Redis + Lua), Cloudflare, or AWS
```

**Common follow-up questions:**

| Question | Strong Answer |
|---|---|
| "What if Redis goes down?" | Fail open or fall back to local per-node limits with safety margin. |
| "How handle clock skew?" | Use Redis server time (TIME command), not local node time. |
| "What about WebSocket?" | Rate limit on connection establishment, not individual messages. |
| "What's the overhead?" | Redis INCR: ~0.1ms. Lua script: ~0.5ms. Acceptable for most use cases. |
