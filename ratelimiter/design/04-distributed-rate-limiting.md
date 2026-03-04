# Rate Limiter — Distributed Rate Limiting Deep Dive

> The single hardest problem in rate limiter design: maintaining accurate counters across multiple rate limiter nodes without introducing unacceptable latency or inconsistency. This doc covers four approaches, their trade-offs, and multi-datacenter strategies.

---

## Why Distributed Is Hard

A single-node rate limiter is trivial — atomic increment on an in-process counter. No coordination, no latency, no consistency issues.

But in a distributed system with N application servers (each running a rate limiter), a client's requests hit different servers. Without coordination, each server maintains its own counter:

```
Client limit: 100 req/min

Server 1: counter = 98  → allows 2 more
Server 2: counter = 95  → allows 5 more
Server 3: counter = 99  → allows 1 more
...
Server N: counter = 97  → allows 3 more

Total allowed: up to 100 × N requests!
```

The counters **must** be shared. The question is: how?

---

## Approach 1: Centralized Counter Store (Redis)

All rate limiter nodes read/write counters from a single Redis cluster.

```
┌──────────┐  ┌──────────┐  ┌──────────┐
│  App     │  │  App     │  │  App     │
│  Server  │  │  Server  │  │  Server  │
│  1       │  │  2       │  │  N       │
└────┬─────┘  └────┬─────┘  └────┬─────┘
     │             │             │
     │   Each request: INCR key  │
     └──────┬──────┴──────┬──────┘
            │             │
     ┌──────▼─────────────▼──────┐
     │         Redis Cluster      │
     │                            │
     │  Key: user:abc:orders:POST │
     │  tokens: 42                │
     │  last_refill: 1672531200   │
     └────────────────────────────┘
```

### Atomic Operations

Redis `INCR` is atomic — it reads, increments, and returns the new value in one operation. A Lua script can atomically perform read → check → increment as a single unit. No race conditions within a single Redis key.

### The GET-then-SET Race Condition

**Broken (naive) implementation:**
```
Thread A: GET counter → 99
Thread B: GET counter → 99        ← both see 99
Thread A: if 99 < 100 → allow
Thread B: if 99 < 100 → allow     ← both pass!
Thread A: SET counter = 100
Thread B: SET counter = 100       ← counter shows 100, but 101 requests passed
```

**Fixed (atomic) implementation:**
```
Thread A: INCR counter → 100 → allow (100 ≤ 100)
Thread B: INCR counter → 101 → reject (101 > 100)
```

Or use a Lua script that atomically reads, checks, and increments. Redis executes Lua scripts without interleaving other commands.

### Latency

~0.5-1ms per Redis roundtrip (same-datacenter). This is added to every API request. For most APIs with P99 latency of ~50-100ms, adding 1ms is acceptable (~1-2% overhead).

For multi-dimension rate limiting (4 Redis keys per request), pipeline all 4 operations in a single roundtrip — still ~1ms total.

### Redis Failure Modes

If Redis is unavailable, the rate limiter must decide:

| Strategy | Behavior | Risk | When to use |
|---|---|---|---|
| **Fail open** | Allow all requests | Overload during attack + outage | Most API rate limiting (availability > precision) |
| **Fail closed** | Reject all requests | Self-inflicted outage | Security-critical limits (login attempts, password resets) |

**Most production systems fail open** — a brief period without rate limiting is less damaging than blocking all traffic. Stripe fails open for their API rate limiter. [VERIFIED — Stripe blog describes fail-open behavior]

**Exception:** Security-critical limits should fail closed. It's better to block logins for 30 seconds (Redis failover time) than to allow brute-force attacks.

**Best practice:** Fail open with:
- Immediate PagerDuty alert on any fail-open event
- Local in-memory fallback with degraded accuracy
- Monitoring that tracks fail-open duration and request volume during fail-open

### Redis Scaling

| Redis Configuration | Throughput | Use Case |
|---|---|---|
| Single instance | ~100K ops/sec | Small-scale (< 100K rate limit checks/sec) |
| Redis Cluster (6+ nodes) | ~1M+ ops/sec | Production scale |
| Multiple clusters (sharded) | ~10M+ ops/sec | Extreme scale |

Shard by rate limit key (e.g., hash of `clientId:resource:dimension`). Use Redis Cluster (automatic sharding and failover) or client-side consistent hashing.

**Memory sizing:** 10M clients × 4 dimensions × 2 values per counter (token bucket: tokens + timestamp) × 16 bytes = ~1.3 GB. Well within a single Redis node's capacity. At this scale, we're throughput-limited, not memory-limited.

---

## Approach 2: Local Counter + Periodic Sync (Approximate)

Each rate limiter node maintains local in-memory counters. Periodically (every 1-5 seconds), nodes sync their counts to a central store and pull the aggregated global count.

```
┌──────────────┐  ┌──────────────┐  ┌──────────────┐
│  App Server  │  │  App Server  │  │  App Server  │
│  Local: 28   │  │  Local: 35   │  │  Local: 22   │
│  ↕ sync 1s   │  │  ↕ sync 1s   │  │  ↕ sync 1s   │
└──────┬───────┘  └──────┬───────┘  └──────┬───────┘
       │                 │                 │
       └────────┬────────┴────────┬────────┘
                │                 │
         ┌──────▼─────────────────▼──────┐
         │     Central Aggregator         │
         │     Global count: 85           │
         └────────────────────────────────┘
```

### Pros
- No per-request network hop. Rate limit check is in-memory (~microseconds).
- Network cost amortized over the sync interval.

### Cons — Over-Admission

Between syncs, the global count is approximate. Maximum over-admission:

```
Over-admission = N × rate × T

N = number of nodes
rate = per-node rate limit (or global limit if not divided)
T = sync interval

Example: 10 nodes, limit = 100/s, sync = 1s
Over-admission = 10 × 100 × 1 = 1,000 requests
(when only 100 should be allowed)
```

### Mitigation: Local Quotas

Give each node a **local quota** = `global_limit / N`. Each node enforces its own quota independently. Periodically rebalance quotas based on actual traffic distribution.

```
Global limit: 1,000 req/min
10 nodes → each node gets 100 req/min

Node 1 (heavy traffic): uses 95/100 → requests rebalance
Node 7 (light traffic): uses 12/100 → excess quota redistributed
```

This is how **Google's Doorman** works — a cooperative rate limiter where clients request capacity from a central server. [VERIFIED — Doorman GitHub design doc]

### Google Doorman Architecture

```
┌────────────────────────────────────────────┐
│              Doorman Master                 │
│  (global capacity allocation)               │
│  Algorithms: ProportionalShare, FairShare   │
│  Master election via distributed consensus  │
└────────────┬───────────┬───────────────────┘
             │           │
    ┌────────▼──┐  ┌─────▼───────┐
    │  Client 1  │  │  Client 2   │
    │  Quota: 60 │  │  Quota: 40  │
    │  Enforces  │  │  Enforces   │
    │  locally   │  │  locally    │
    └────────────┘  └─────────────┘
```

Key properties of Doorman:
- **Hierarchical**: clients → leaf servers → regional servers → global root
- **Capacity leases**: clients request capacity, server allocates quotas
- **Self-enforcing**: clients enforce their quota locally (no per-request central call)
- **Failure modes**: pessimistic (stop), optimistic (continue at requested rate), safe (use configured safe capacity)
- **Open-sourced** on GitHub by YouTube/Google
- [VERIFIED — Doorman design document on GitHub, SREcon16 presentation]

### When to Use Local + Sync

| Use Case | Suitable? | Why |
|---|---|---|
| Per-client API limits (Stripe, GitHub) | No | Accuracy matters — a client should not get 10× their limit |
| Global traffic shaping (protect a backend) | Yes | Approximate is fine — goal is "don't overwhelm the service" |
| Internal service-to-service limiting | Yes | Approximate enforcement prevents noisy neighbors |
| High-throughput edge limiting (>1M checks/sec) | Yes | Avoids Redis bottleneck |

---

## Approach 3: Sticky Routing

Route all requests from a given client to the same rate limiter node using consistent hashing on client ID. Each node is the sole owner of its clients' counters — no coordination needed.

```
Load Balancer (consistent hash on client_id)
    │
    ├── hash(user_A) → Node 1 (owns user_A's counters)
    ├── hash(user_B) → Node 2 (owns user_B's counters)
    └── hash(user_C) → Node 1 (owns user_C's counters)
```

### Pros
- No distributed coordination. Counters are exact (single writer per key).
- No external dependency (no Redis).

### Cons
- **Node failure**: failover resets counters (new node starts at 0). During failover, the client gets a burst of free requests.
- **Load imbalance**: one heavy client monopolizes a node. If user_A sends 100× more traffic than user_B, Node 1 is overloaded while Node 2 is idle.
- **Requires consistent-hashing load balancer**: not all infrastructure supports this.
- **Scaling events**: adding/removing nodes reshuffles client assignments → counter resets.

### Used By
- Some CDN edge rate limiters. Cloudflare uses sticky routing within a PoP (Point of Presence). [PARTIALLY VERIFIED — referenced in Cloudflare discussions but not in their main engineering blog]
- Suitable for edge/CDN rate limiting where approximate limits during failover are acceptable.

---

## Approach 4: Distributed Consensus (Raft/Paxos)

Use a consensus protocol to agree on counter values across nodes. Strongly consistent counters — no over-admission.

### Why NOT This Approach

| Metric | Consensus (Raft) | Redis (Single Writer) |
|---|---|---|
| Latency per operation | 10-100ms (consensus round) | 0.5-1ms (single node) |
| Throughput | Limited by leader | ~100K+ ops/sec |
| Consistency | Strong (linearizable) | Strong for single-key ops |
| Failure handling | Automatic leader election | Sentinel/Cluster failover |

Consensus adds 10-100ms per operation — **far too slow** for per-request rate limiting where the budget is <1ms. The throughput is also limited by the consensus leader.

**This approach is NOT used for rate limiting in practice.** The latency cost is prohibitive. Redis achieves "good enough" consistency for this use case — single-writer per key with atomic operations provides the atomicity we need without the overhead of multi-node consensus.

**When interviewers ask "why not Raft?"** — the answer is: rate limiting needs AP (availability + partition tolerance), not CP (consistency + partition tolerance). We'd rather occasionally over-admit a few requests than add 100ms to every API call.

---

## Summary: Which Approach When?

| Approach | Accuracy | Latency | Complexity | Best For |
|---|---|---|---|---|
| **Centralized Redis** | Exact | ~1ms per request | Medium | Per-client API limits (most common) |
| **Local + Sync** | Approximate | ~μs per request | Medium | Global traffic shaping, high throughput |
| **Sticky Routing** | Exact (per node) | ~μs per request | Low | Edge/CDN rate limiting |
| **Consensus** | Perfect | 10-100ms per request | High | **Never for rate limiting** |

**The right answer for most systems:** Centralized Redis for strict per-client limits (the common case). Local counters with periodic sync for global/approximate limits. Sticky routing for edge PoP rate limiting. Never consensus.

---

## Multi-Datacenter Rate Limiting

If your system runs in multiple regions, a single Redis cluster can't serve all regions:

```
US-East ←── 50-200ms ──→ EU-West ←── 100-300ms ──→ AP-Southeast
```

Cross-region Redis latency of 50-200ms destroys our <1ms P99 requirement.

### Option 1: Per-Region Rate Limiting

Each region enforces limits independently with its own Redis cluster.

```
US-East:        limit = 1,000 req/min → enforced locally
EU-West:        limit = 1,000 req/min → enforced locally
AP-Southeast:   limit = 1,000 req/min → enforced locally
```

**Problem:** A client spraying requests across all 3 regions gets 3,000 req/min instead of 1,000.

**When acceptable:** Most clients use one region. Cross-region abuse is uncommon and can be detected asynchronously.

### Option 2: Split Quotas (Recommended)

Global limit divided across regions based on traffic share:

```
Global limit: 1,000 req/min

US-East (60% of traffic):    600 req/min
EU-West (30% of traffic):    300 req/min
AP-Southeast (10% of traffic): 100 req/min
```

Periodically rebalance based on actual traffic. A global aggregator asynchronously detects cross-region abuse (if a client exceeds the global limit across all regions combined).

```
┌──────────────────────────────────────┐
│         Global Aggregator             │
│  Async check every 10-30 seconds      │
│  Detects cross-region abuse           │
│  Rebalances quotas across regions     │
└──────┬──────────┬──────────┬─────────┘
       │          │          │
 ┌─────▼────┐ ┌──▼──────┐ ┌─▼─────────┐
 │ US-East  │ │ EU-West │ │ AP-South  │
 │ Redis    │ │ Redis   │ │ Redis     │
 │ 600/min  │ │ 300/min │ │ 100/min   │
 └──────────┘ └─────────┘ └───────────┘
```

### Option 3: Global Rate Limiting with Async Replication

Each region writes to local Redis. Periodically syncs to a global view. Approximate — there's a window where cross-region abuse isn't detected — but catches it within seconds.

### Stripe's Approach

Stripe uses per-region rate limiting with a global safety net: if a client exceeds limits across all regions combined (detected asynchronously), the client is flagged for review. This prioritizes low-latency local enforcement over perfect global accuracy. [PARTIALLY VERIFIED — Stripe's blog describes Redis-backed rate limiting; multi-region specifics are inferred from their multi-region infrastructure]

---

## Contrast: Doorman vs Traditional Per-Request Checking

| Aspect | Per-Request (Redis) | Doorman (Quota Leasing) |
|---|---|---|
| **Model** | Check central counter for every request | Get a quota allocation, enforce locally |
| **Per-request latency** | ~1ms (Redis call) | ~μs (in-memory check) |
| **Accuracy** | Exact | Approximate (between lease renewals) |
| **Central dependency** | Per-request (critical path) | Periodic only (lease renewal) |
| **Failure behavior** | Fail open or closed immediately | Continues with last lease until expiry |
| **Throughput** | Limited by Redis (~100K-1M ops/sec) | Unlimited (local enforcement) |
| **Client cooperation** | Not required | Required (malicious clients can ignore quota) |

Doorman inverts the model: instead of "check every request against a central counter," it says "get a quota allocation, enforce it locally." Better for high-throughput systems but requires cooperative clients.

## Contrast: Envoy Sidecar vs Embedded vs Dedicated Service

| Aspect | Embedded Middleware | Sidecar (Envoy) | Dedicated Service |
|---|---|---|---|
| **Latency** | ~μs (in-process) | ~0.1ms (localhost) | ~1ms (network) |
| **Coupling** | Tight (same process, same language) | Loose (separate process, any language) | None (separate service) |
| **Scaling** | Scales with app | Scales with app | Independent scaling |
| **Policy updates** | Requires app redeploy | Sidecar config reload | API-driven, no redeploy |
| **Best for** | Monoliths, simple cases | Service mesh (Istio) | Multi-service platforms |

Envoy supports both local rate limiting (token bucket in the sidecar) and global rate limiting (external gRPC service with Redis). The two can be combined. [VERIFIED — Envoy docs]

---

*See also: [Interview Simulation](01-interview-simulation.md) (Phase 6) for the interview discussion of distributed coordination.*
