# Rate Limiter — Rate Limiting Algorithms Deep Dive

> The algorithm is the heart of the rate limiter. Each algorithm makes different trade-offs between accuracy, memory, burst handling, and implementation complexity. This doc covers the five major algorithms, their Redis implementations, and when to use each.

---

## Algorithm Comparison Table

| Algorithm | Memory per key | Accuracy | Burst handling | Complexity | Best for |
|---|---|---|---|---|---|
| **Token Bucket** | O(1) — 2 values | Exact for rate | Allows controlled bursts | Low | API rate limiting with burst tolerance |
| **Leaky Bucket** | O(1) — 2 values | Exact for rate | No bursts — smooths output | Low | Traffic shaping, uniform output rate |
| **Fixed Window Counter** | O(1) — 1 counter | Boundary burst problem | No burst control | Very low | Simple internal tools, large windows |
| **Sliding Window Log** | O(N) — N = requests | Perfectly exact | No burst control | Medium | Low-volume, high-precision (audit) |
| **Sliding Window Counter** | O(1) — 2 counters | ~99.997% accurate | No burst control | Low | Production API rate limiting at scale |

---

## 1. Token Bucket

### Concept

A bucket holds tokens. Each request consumes one token (or more for cost-based limiting). Tokens are refilled at a constant rate. If the bucket is empty, the request is rejected. The bucket has a maximum capacity (the burst size).

```
Bucket capacity (burst) = 50 tokens
Refill rate = 10 tokens/second

Time 0: [■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■] 50/50
  → 30 requests arrive instantly
Time 0+: [■■■■■■■■■■■■■■■■■■■■                              ] 20/50
  → 3 seconds pass (30 tokens refilled, capped at 50)
Time 3: [■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■] 50/50
```

### Parameters

| Parameter | Description | Example |
|---|---|---|
| `rate` | Tokens added per second | 10 tokens/s |
| `burst` | Maximum bucket capacity | 50 tokens |

A bucket with rate=10, burst=50 means: sustained rate of 10 req/s, but can burst up to 50 requests if the bucket has accumulated tokens.

### Redis Implementation (Lua Script)

```lua
-- Token Bucket: atomic check-and-decrement
local key = KEYS[1]
local rate = tonumber(ARGV[1])       -- tokens per second
local burst = tonumber(ARGV[2])      -- max bucket capacity
local now = tonumber(ARGV[3])        -- current timestamp (milliseconds)
local requested = tonumber(ARGV[4])  -- tokens to consume (default: 1)

-- Get current state
local data = redis.call('HMGET', key, 'tokens', 'last_refill')
local tokens = tonumber(data[1]) or burst    -- start full
local last_refill = tonumber(data[2]) or now

-- Refill tokens based on elapsed time
local elapsed = (now - last_refill) / 1000.0  -- convert ms to seconds
tokens = math.min(burst, tokens + elapsed * rate)

-- Check and consume
local allowed = 0
if tokens >= requested then
    tokens = tokens - requested
    allowed = 1
end

-- Update state
redis.call('HMSET', key, 'tokens', tokens, 'last_refill', now)
redis.call('EXPIRE', key, math.ceil(burst / rate) * 2)  -- TTL: 2x fill time

return {allowed, math.floor(tokens)}  -- {1|0, remaining_tokens}
```

**Why Lua?** Redis executes Lua scripts atomically — no other command can interleave. This eliminates the GET-then-SET race condition.

### Pros
- Allows bursts — real-world API traffic is bursty (mobile app launches, batch operations). Token bucket serves bursts immediately from accumulated tokens.
- Memory-efficient — only 2 values per key (tokens + timestamp).
- Simple to implement and well-understood.

### Cons
- The `burst` parameter is an extra knob to tune. A large burst allows a flood that may overwhelm downstream services even when the average rate is within limits.
- Burst + rate are two parameters to configure vs one parameter for simpler algorithms.

### Used By
- **AWS API Gateway** — documentation explicitly states token bucket with `rate` (requests/second) and `burst` (bucket capacity) parameters. Default: 10,000 RPS rate, 5,000 burst per account per region. [VERIFIED — AWS docs]
- **Stripe** — uses token bucket for their request rate limiter, backed by Redis. One of four limiters they run (request rate, concurrent requests, fleet usage load shedder, worker utilization load shedder). Only the first two are true rate limiters; the other two are load shedders. [VERIFIED — Stripe engineering blog, 2017]
- **NGINX** — `limit_req` module's `burst` parameter with `nodelay` effectively creates token bucket behavior on top of the underlying leaky bucket. [VERIFIED — NGINX docs]

---

## 2. Leaky Bucket (as a Queue)

### Concept

Requests enter a FIFO queue (the bucket). The queue is drained at a fixed rate. If the queue is full, new requests are rejected. Output rate is perfectly smooth — no bursts.

```
Incoming requests (bursty)    →    Bucket (queue)    →    Outgoing (smooth)
████ ██ ████████ █            →    [■■■■■■■■■■]      →    ■ ■ ■ ■ ■ ■ ■ ■
                                   capacity = 10          rate = 2/sec
```

### Parameters

| Parameter | Description | Example |
|---|---|---|
| `rate` | Outflow rate (requests drained per second) | 2 req/s |
| `capacity` | Queue size (max pending requests) | 10 requests |

### Implementation

For rate limiting (not traffic shaping), the leaky bucket is often implemented identically to token bucket but without allowing bursts above the rate. In NGINX's implementation, it tracks a counter that "leaks" at the configured rate.

### Pros
- Produces perfectly smooth output — ideal for traffic shaping where downstream services need a uniform request rate.
- Predictable processing rate.

### Cons
- Bursty traffic fills the queue → older requests may become stale while waiting.
- No burst tolerance — legitimate traffic spikes are delayed rather than served immediately.
- Queue management adds memory overhead.

### Used By
- **NGINX** — `limit_req` module is explicitly a leaky bucket implementation. Key detail: NGINX returns **503 Service Unavailable** by default (not 429). You must explicitly configure `limit_req_status 429;` to get the correct rate limiting status code. The `burst` parameter creates a queue; `nodelay` processes queued requests immediately. [VERIFIED — NGINX official docs and blog]
- **Shopify** — uses leaky bucket for their REST Admin API: bucket size of 40 requests, leak rate of 2 requests/second. Returns 429 when exceeded. Shopify Plus stores get larger buckets and up to 20 req/s leak rate. [VERIFIED — Shopify developer docs]

### Token Bucket vs Leaky Bucket

| Aspect | Token Bucket | Leaky Bucket |
|---|---|---|
| **Bursts** | Allows bursts (spend accumulated tokens) | No bursts — smooths everything |
| **Output** | Variable rate (bursty) | Fixed rate (smooth) |
| **Best for** | APIs (users want fast responses) | Traffic shaping (even out bursty traffic) |
| **Client experience** | Immediate response if tokens available | May queue requests |

Most API rate limiters use token bucket. Most traffic shapers use leaky bucket.

---

## 3. Fixed Window Counter

### Concept

Divide time into fixed windows (e.g., 1-minute windows). Each window has a counter. Increment the counter per request. If counter > limit, reject.

```
Window 1 (0:00-1:00)    Window 2 (1:00-2:00)    Window 3 (2:00-3:00)
[████████░░ ] 80/100    [██████████ ] 100/100   [███░░░░░░░ ] 30/100
                        → FULL, reject new       → accepting
```

### Parameters

| Parameter | Description | Example |
|---|---|---|
| `window_size` | Window duration | 60 seconds |
| `limit` | Max requests per window | 100 |

### Redis Implementation

```
Key:   ratelimit:{client_id}:{resource}:{window_number}
       where window_number = floor(timestamp / window_size)

INCR key         → returns new count
EXPIRE key TTL   → set TTL = window_size (auto-cleanup)
if count > limit → reject
```

Two Redis commands per request (INCR + conditional EXPIRE), combinable into one Lua script.

### Pros
- Extremely simple — one counter per key per window.
- Memory-efficient.
- Easy to understand and debug.

### Cons
- **Boundary burst problem** — the fundamental weakness:
  ```
  Limit: 100 requests per minute

  Time 0:58 → Client sends 100 requests (Window 1: 100/100) ✓
  Time 1:01 → Client sends 100 requests (Window 2: 100/100) ✓

  Result: 200 requests in ~3 seconds — 2x the intended rate!
  Both windows show ≤100, so neither triggers a rejection.
  ```

### Used By
- **Twitter/X** — uses 15-minute fixed windows for their API rate limiting. Limits are per-endpoint and vary by auth method (OAuth 1.0a user context vs OAuth 2.0 app context). The large window size (15 minutes) makes the boundary burst problem less impactful in practice. Returns 429 with `x-rate-limit-limit`, `x-rate-limit-remaining`, `x-rate-limit-reset` headers. [VERIFIED — X API docs]
- Many simple internal rate limiters where exact precision isn't critical.

---

## 4. Sliding Window Log

### Concept

Store the timestamp of every request in a sorted set. To check the limit: count timestamps that fall within `[now - window_size, now]`. If count >= limit, reject. Remove timestamps older than the window.

```
Sorted set for client "user_abc":
  [1672531190, 1672531195, 1672531200, 1672531205, 1672531210, ...]

Now = 1672531260, window = 60s
Count timestamps >= 1672531200 → if count >= limit → reject
Remove timestamps < 1672531200 (old entries)
```

### Redis Implementation

```
Key:   ratelimit:{client_id}:{resource}
Type:  Sorted Set (score = timestamp)

ZADD key timestamp timestamp          → add this request
ZREMRANGEBYSCORE key 0 (now - window) → evict old entries
ZCARD key                              → count current entries
if count >= limit → reject
```

### Pros
- **Perfectly accurate** — no boundary burst problem, no approximation. The window slides continuously.

### Cons
- **Memory-intensive** — stores one entry per request. For a client making 10,000 req/min, that's 10,000 entries per key. At 10M clients × 10K entries = 100 billion entries. Prohibitive.
- `ZCARD` on large sorted sets adds latency.
- `ZREMRANGEBYSCORE` creates GC pressure.

### Used By
- Rarely used in production at scale due to memory cost.
- Useful for low-volume, high-precision use cases: audit logging, compliance tracking, security monitoring.

---

## 5. Sliding Window Counter (The Sweet Spot)

### Concept

Combine fixed window counters with a weighted average to approximate a sliding window. Maintain counters for the current and previous windows. The estimated count uses the overlap of the previous window.

```
Previous window (0:00-1:00): 84 requests
Current window  (1:00-2:00): 36 requests (so far)

Now = 1:15 (15 seconds into current window)
Overlap of previous window = (60 - 15) / 60 = 75%

Estimated count = 84 × 0.75 + 36 = 63 + 36 = 99

If limit = 100 → one more request would hit 100 → reject
```

```
│←── previous window ──→│←── current window ──→│
│         84 req         │      36 req          │
│                   │◄──overlap──►│              │
│                   │    75%     │              │
│                   │            │              │
0:00              0:45          1:15           2:00
                                 ↑ NOW
```

### Parameters

| Parameter | Description | Example |
|---|---|---|
| `window_size` | Window duration | 60 seconds |
| `limit` | Max requests per window | 100 |

### Redis Implementation (Lua Script)

```lua
-- Sliding Window Counter: atomic check-and-increment
local key_prefix = KEYS[1]
local limit = tonumber(ARGV[1])
local window = tonumber(ARGV[2])      -- window size in seconds
local now = tonumber(ARGV[3])         -- current timestamp

-- Calculate window boundaries
local current_window = math.floor(now / window)
local previous_window = current_window - 1
local window_offset = (now % window) / window  -- position within current window

-- Get counts
local current_key = key_prefix .. ':' .. current_window
local previous_key = key_prefix .. ':' .. previous_window

local current_count = tonumber(redis.call('GET', current_key) or '0')
local previous_count = tonumber(redis.call('GET', previous_key) or '0')

-- Calculate estimated count (weighted average)
local overlap = 1.0 - window_offset
local estimated = previous_count * overlap + current_count

-- Check limit
if estimated >= limit then
    return {0, math.floor(limit - estimated)}  -- rejected, negative remaining
end

-- Increment current window counter
redis.call('INCR', current_key)
redis.call('EXPIRE', current_key, window * 2)  -- TTL: 2x window

return {1, math.floor(limit - estimated - 1)}  -- allowed, remaining
```

### Pros
- Solves the boundary burst problem (approximately — not perfectly, but close enough for all practical purposes).
- Constant memory — same as fixed window (two counters per key).
- Simple implementation.

### Cons
- Not perfectly accurate — it's an approximation. The estimate can be off by a few percent when traffic distribution within a window is very uneven.
- In practice, the error is negligible. Cloudflare measured it at **<0.003%** across 400 million requests from 270,000 distinct sources. [VERIFIED — Cloudflare engineering blog]

### Used By
- **Cloudflare** — their rate limiting uses sliding window counter (which they call "approximated sliding window" or "floating window"). The algorithm was described in detail in their engineering blog. [VERIFIED — Cloudflare blog: "How we built rate limiting capable of scaling to millions of domains"]
- Recommended by **Redis** documentation for rate limiting implementations.

### **This is the recommended algorithm for most production systems** — best balance of accuracy, memory efficiency, and implementation simplicity.

---

## Cost-Based Rate Limiting

Not all requests are equal. A lightweight GET is cheaper than a complex database query. Cost-based rate limiting assigns a "cost" to each request type and rate-limits by total cost per window, not just request count.

### GitHub's GraphQL Point System

GitHub's GraphQL API uses a **point-based cost system**:

- **Primary rate limit**: 5,000 points/hour for regular users, 10,000 for Enterprise Cloud.
- **Point calculation**: Based on estimated database cost. Count the number of sub-requests needed to fulfill each connection in the query (using `first`/`last` arguments), divide total by 100, round to nearest whole number (minimum 1 point).
- Example: querying 100 repos with 50 issues each with 60 labels = 5,101 sub-requests = **51 points**.
- **Secondary rate limits**: Queries without mutations = 1 point, with mutations = 5 points. Max 2,000 points/minute.
- [VERIFIED — GitHub official docs]

### Shopify's Cost-Based Leaky Bucket

Shopify's GraphQL Admin API uses a **calculated query cost** with a leaky bucket mechanism:
- Same leaky bucket as their REST API, but each query consumes a variable number of points based on query complexity.
- The cost depends on the fields and connections requested.
- [VERIFIED — Shopify developer docs]

### Trade-off

| Approach | Fairness | Complexity |
|---|---|---|
| Flat counting (1 request = 1 unit) | Unfair — expensive queries cost the same as cheap ones | Simple |
| Cost-based (each request has a cost) | Fair — expensive queries consume more quota | Complex — need to assign and maintain costs |

Cost-based limiting is more fair but requires per-endpoint cost assignment, which is an ongoing operational burden.

---

## Contrast: Rate Limiting vs Adjacent Concepts

### Rate Limiting vs Circuit Breakers

| | Rate Limiting | Circuit Breaking (Hystrix, Resilience4j) |
|---|---|---|
| **Direction** | Controls INCOMING requests | Controls OUTGOING requests |
| **Protects** | The server from clients | The client from a failing server |
| **Trigger** | Request count exceeds quota | Error rate or latency exceeds threshold |
| **Action** | Reject with 429 | Stop making calls, return fallback |
| **Architecture** | Server-side enforcement | Client-side enforcement |

They solve **opposite problems** but complement each other. A rate limiter on your API prevents overload; a circuit breaker on your downstream calls prevents cascading failures. Both are part of defense-in-depth.

### Rate Limiting vs Load Shedding

| | Rate Limiting | Load Shedding |
|---|---|---|
| **Scope** | Per-client quotas | Global (all clients) |
| **When** | Proactive — enforce limits before overload | Reactive — drop requests during overload |
| **Fairness** | Each client gets their quota | Priority-based (critical requests survive) |
| **Example** | "User A: max 1,000 req/min" | "System overloaded: dropping 50% of non-critical requests" |

Stripe uses both: rate limiting (token bucket per client) AND load shedding (fleet usage load shedder that reserves capacity for critical operations when the system is overloaded). [VERIFIED — Stripe blog describes 4 limiters, 2 of which are load shedders]

### Rate Limiting vs Backpressure

Rate limiting **rejects** excess requests. Backpressure **slows down** the sender. Backpressure is a flow-control mechanism (like TCP flow control, reactive streams) — it signals upstream to produce less. Rate limiting doesn't signal — it just drops.

---

*See also: [Interview Simulation](01-interview-simulation.md) (Phase 5) for the interview discussion of algorithm trade-offs.*
