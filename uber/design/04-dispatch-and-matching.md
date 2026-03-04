# Real-Time Dispatch & Matching — The Brain of Uber

> The dispatch system matches ride requests with available drivers in real-time.
> It's the most latency-sensitive and business-critical component.
> A 1-second delay in matching = thousands of riders waiting unnecessarily.

---

## Table of Contents

1. [The Matching Problem](#1-the-matching-problem)
2. [Dispatch Flow](#2-dispatch-flow)
3. [Greedy vs Batched Matching](#3-greedy-vs-batched-matching)
4. [Uber Pool / Shared Rides](#4-uber-pool--shared-rides)
5. [Dispatch Reliability](#5-dispatch-reliability)
6. [Contrasts](#6-contrasts)

---

## 1. The Matching Problem

Given a ride request at location (lat, lng), find the BEST available driver. "Best" is a multi-factor optimization:

### Matching Signals

| Signal | Weight | Why |
|---|---|---|
| **ETA to pickup** | Highest | Rider's #1 concern is "how fast will the driver arrive?" Route-based ETA, not straight-line distance |
| **Driver heading** | Medium | A driver heading toward the pickup requires no U-turn — faster effective ETA. A driver heading away needs to turn around — slower |
| **Driver rating** | Low | Higher-rated drivers provide better experience. Marginal factor — a 4.9-rated driver 5 min away loses to a 4.8-rated driver 2 min away |
| **Vehicle type match** | Hard filter | UberX request can't be matched with an UberBlack vehicle (wrong pricing tier). Hard constraint, not a scoring factor |
| **Acceptance probability** | Medium | ML model predicts likelihood a driver will accept this ride (based on driver's history, ride direction, ride length). Sending an offer to a driver who will decline wastes 15 seconds |
| **Supply/demand balance** | Context | In high-surge areas, don't "waste" the only available driver on a short trip when a longer, higher-revenue trip is queued. Controversial but economically rational |

### Match Score Function

```
score(driver, ride) = w1 × (1 / etaToPickup)
                    + w2 × headingBonus(driver.heading, angleToPickup)
                    + w3 × driver.rating
                    + w4 × acceptanceProbability(driver, ride)

where:
  headingBonus = cos(angleBetween(driver.heading, directionToPickup))
                 // 1.0 if heading directly toward pickup
                 // 0.0 if perpendicular
                 // -1.0 if heading directly away (but capped at 0)

  etaToPickup is from the Routing Service (route-based, traffic-aware)
```

---

## 2. Dispatch Flow

```
Rider taps "Request UberX"
    │
    ▼
┌─────────────────────────────────────────────────────────────────┐
│ STEP 1: Ride Creation                                            │
│                                                                  │
│ Ride Service creates ride record:                                │
│   rideId: "ride-uuid-abc"                                        │
│   status: MATCHING                                               │
│   pickup: (40.7484, -73.9857)                                    │
│   dropoff: (40.7614, -73.9776)                                   │
│   rideType: UberX                                                │
│   surgeMultiplier: 1.3 (locked at time of request)               │
│   upfrontFare: $14.00                                            │
└──────────────────────┬──────────────────────────────────────────┘
                       │
                       ▼
┌─────────────────────────────────────────────────────────────────┐
│ STEP 2: Candidate Generation                                     │
│                                                                  │
│ Dispatch queries Geospatial Index:                               │
│   "Find AVAILABLE drivers within 5km of (40.7484, -73.9857),    │
│    vehicleType = UberX"                                          │
│                                                                  │
│ Implementation:                                                  │
│   pickupCell = h3.geoToH3(40.7484, -73.9857, res=9)             │
│   searchCells = h3.kRing(pickupCell, k=2)  // 19 cells           │
│   candidates = flatMap(cell → index[cell])                       │
│   filtered = candidates.filter(AVAILABLE, UberX)                 │
│                                                                  │
│ Result: ~10-50 candidate drivers (depends on city density)       │
└──────────────────────┬──────────────────────────────────────────┘
                       │
                       ▼
┌─────────────────────────────────────────────────────────────────┐
│ STEP 3: ETA Computation (parallel)                               │
│                                                                  │
│ For each candidate driver, query Routing Service:                │
│   ETA(driverLocation → pickupLocation)                           │
│                                                                  │
│ This is the expensive step — 10-50 routing queries.              │
│ All queries fired in PARALLEL. Each takes ~5-20ms.               │
│ Total: ~20-50ms (parallelized, not sequential).                  │
│                                                                  │
│ Why not use straight-line distance?                              │
│   Driver A: 500m straight, same road → 1 min ETA                │
│   Driver B: 500m straight, across a river → 15 min ETA          │
│   Route-based ETA gives CORRECT ranking.                        │
└──────────────────────┬──────────────────────────────────────────┘
                       │
                       ▼
┌─────────────────────────────────────────────────────────────────┐
│ STEP 4: Ranking                                                  │
│                                                                  │
│ Score each candidate using the match score function.             │
│ Sort by score descending.                                        │
│ Select the top driver.                                           │
│                                                                  │
│ Time: ~1ms (simple scoring + sort on 10-50 candidates)           │
└──────────────────────┬──────────────────────────────────────────┘
                       │
                       ▼
┌─────────────────────────────────────────────────────────────────┐
│ STEP 5: Ride Offer                                               │
│                                                                  │
│ Send ride offer to selected driver via:                          │
│   1. WebSocket (if app is in foreground) — ~100ms delivery       │
│   2. Push notification (APNs/FCM) as backup — ~1-5s delivery     │
│                                                                  │
│ Offer contains: rider name, rating, pickup address,             │
│   dropoff address, estimated fare, surge multiplier.             │
│                                                                  │
│ Driver has 15 seconds to accept or decline.                      │
│ Timer starts when offer is delivered (ack from driver app).      │
└──────────────────────┬──────────────────────────────────────────┘
                       │
            ┌──────────┼──────────┐
            ▼          ▼          ▼
        ACCEPT      DECLINE    TIMEOUT
            │          │          │
            │          └──────────┘
            │                │
            │                ▼
            │     ┌───────────────────┐
            │     │ CASCADE to next    │
            │     │ best driver        │
            │     │                    │
            │     │ If exhausted:      │
            │     │  Expand radius     │
            │     │  (5km → 8km)       │
            │     │  Re-query index    │
            │     │                    │
            │     │ If still no match: │
            │     │  "No drivers       │
            │     │   available"       │
            │     └───────────────────┘
            │
            ▼
     ┌────────────────────┐
     │ MATCHED             │
     │                     │
     │ Ride status →       │
     │ DRIVER_EN_ROUTE     │
     │                     │
     │ Notify rider:       │
     │  driver name, photo,│
     │  vehicle, ETA,      │
     │  live map tracking  │
     └────────────────────┘

Total latency (request → driver offer): ~3-5 seconds
  Step 1 (ride creation):    ~50ms
  Step 2 (candidate gen):    ~10ms
  Step 3 (ETA computation):  ~50ms (parallelized)
  Step 4 (ranking):          ~5ms
  Step 5 (offer delivery):   ~100ms-5s (WebSocket vs push)
```

---

## 3. Greedy vs Batched Matching

### Greedy Matching

Match each ride request immediately with the best available driver.

```
Time 0.0s: Ride R1 arrives → match with nearest driver D1 ✓
Time 0.5s: Ride R2 arrives → D1 is taken, match with D3 (farther) ✗ suboptimal

Problem: R2 is actually closer to D1 than R1 was.
If we had waited 0.5 seconds, we could have assigned:
  R1 → D3 (slightly farther but acceptable)
  R2 → D1 (much closer)
Total ETA: lower
```

### Batched Matching

Accumulate ride requests over a short window (2-5 seconds), then solve a global assignment optimization.

```
Batch window: 2 seconds

Time 0.0s: R1 arrives → add to batch
Time 0.5s: R2 arrives → add to batch
Time 1.2s: R3 arrives → add to batch
Time 2.0s: Window closes → solve assignment:

  Available drivers: D1, D2, D3, D4
  Ride requests: R1, R2, R3

  Compute ETA matrix:
          D1    D2    D3    D4
    R1   3min  5min  8min  2min
    R2   1min  6min  4min  7min
    R3   7min  2min  3min  5min

  Optimal assignment (minimize total ETA):
    R1 → D4 (2 min)
    R2 → D1 (1 min)
    R3 → D2 (2 min)
    Total: 5 min

  Greedy would have given:
    R1 → D4 (2 min) — same
    R2 → D1 (1 min) — same (lucky)
    R3 → D3 (3 min) — worse (D2 was assigned to R1 first in greedy)
    Total: 6 min

  Savings: 17% reduction in total ETA
```

### The Assignment Problem

The batch optimization is a variant of the **assignment problem** (bipartite matching):
- **Input**: N riders, M drivers, N×M cost matrix (ETA from each driver to each rider)
- **Output**: Assignment that minimizes total cost
- **Algorithm**: Hungarian algorithm — O(N³) or O(N²M). For N=50 riders and M=200 drivers, this completes in <1ms.
- **Uber's approach**: Auction-based assignment (a variant of the Bertsekas auction algorithm) — more parallelizable than Hungarian, works well in practice for the batch sizes Uber sees.

[PARTIALLY VERIFIED — Uber Engineering blog discusses batched dispatch but exact algorithm details are proprietary]

### When to Use Which

| Context | Approach | Reasoning |
|---|---|---|
| **Dense market (Manhattan)** | Batched (2-3 sec window) | Many concurrent requests — batching finds globally better assignments |
| **Sparse market (suburban)** | Greedy | Few concurrent requests — not enough to batch meaningfully |
| **Ultra-high demand (NYE)** | Batched (shorter window, ~1 sec) | So many requests that even 1-second batches have enough for optimization |
| **Low supply** | Greedy | Few drivers available — no optimization opportunity |

---

## 4. Uber Pool / Shared Rides

Pool/Shared rides change matching from a **two-party problem** (rider ↔ driver) to a **routing optimization problem** (match new rider with in-progress trips going in a similar direction).

### How Pool Matching Works

```
Existing trip in progress:
  Driver D1: picked up Rider A at point P1
  Going to: Dropoff A at point D1
  Current position: C1

New request: Rider B at Pickup B, going to Dropoff D2

Can we add Rider B to D1's trip?

Check constraints:
  1. Detour to pick up B: is it < 5 min extra for Rider A?
  2. New route C1 → Pickup B → Dropoff A → Dropoff D2
     OR      C1 → Pickup B → Dropoff D2 → Dropoff A
     → Pick the ordering that minimizes total time
  3. Is the new total trip time for A still within tolerance?
  4. Is the pickup ETA for B acceptable?
  5. Does D1's vehicle have capacity? (Pool = max 3 riders typically)

If all constraints met: match B with D1
If not: try other in-progress Pool trips
If none suitable: assign B to a new driver (like regular UberX)
```

### The Vehicle Routing Problem

Pool matching is a variant of the **Vehicle Routing Problem (VRP)**, which is NP-hard:
- **Input**: Set of pickup/dropoff locations, vehicle capacities, time windows
- **Output**: Routes for vehicles that serve all requests while minimizing total travel time
- **Exact solution**: Infeasible for real-time (exponential time)
- **Uber's approach**: Greedy heuristics with local search optimization
  1. For each new rider, find candidate in-progress trips within a radius
  2. For each candidate trip, compute the detour cost (additional time for existing riders)
  3. Filter by detour threshold (e.g., <5 min extra)
  4. Select the trip that minimizes total cost (new rider wait + existing rider detour)
  5. If no good match, create a new trip

### Pool Economics

```
Without Pool:
  Rider A: $15 trip (full fare)
  Rider B: $12 trip (full fare)
  Total revenue: $27, two drivers used

With Pool:
  Rider A: $10 trip (discounted for sharing)
  Rider B: $8 trip (discounted for sharing)
  Total revenue: $18, ONE driver used
  Driver earns: ~$14 (more than a single non-Pool trip)

  Platform revenue per driver-hour: HIGHER (more rides/hour)
  Rider cost: LOWER (discounted fare)
  Driver earnings: HIGHER (more efficient utilization)
  Environmental impact: LOWER (fewer cars on the road)

  The catch: riders accept longer trips and shared space
```

---

## 5. Dispatch Reliability

The dispatch system is the single most critical service — if it goes down, NO rides happen.

### Failure Modes and Mitigations

| Failure | Impact | Mitigation |
|---|---|---|
| **Geospatial index down** | Can't find nearby drivers | Replicated index across availability zones. Fallback: use last known driver positions from Cassandra |
| **Routing service slow** | ETA computation delayed → matching delayed | Fallback: use straight-line distance × city-specific speed factor. Less accurate but functional |
| **WebSocket gateway down** | Can't deliver ride offer to driver | Fallback: push notification (APNs/FCM). Higher latency but reaches driver |
| **Kafka lag** | Location updates delayed → stale spatial index | Alert on consumer lag. Dispatch still works with slightly stale positions (30-second staleness is tolerable for matching) |
| **Database down** | Can't persist ride/trip records | Write-ahead to Kafka (durable). Replay to database when it recovers. In-memory state for active rides |

### Idempotency

```
Problem: Network retry causes duplicate ride request
  Rider taps "Request" → timeout → rider taps again
  Without idempotency: TWO rides created, TWO drivers dispatched

Solution: Idempotency key
  Client generates a unique requestId per ride attempt
  Server checks: "Have I seen this requestId before?"
  If yes: return the existing ride (don't create a new one)
  If no: create a new ride

  Implementation: Redis SET with TTL
    Key: "idempotency:{requestId}"
    Value: rideId
    TTL: 5 minutes
```

### Ride State Machine

```
┌────────┐     ┌──────────┐     ┌──────────────┐
│  IDLE  │────>│ MATCHING │────>│ DRIVER_EN    │
│        │     │          │     │ _ROUTE       │
└────────┘     └──────────┘     └──────────────┘
                    │                   │
                    │ (no match)        │ (driver arrives)
                    ▼                   ▼
              ┌──────────┐     ┌──────────────┐
              │CANCELLED │     │   ARRIVED    │
              │          │     │              │
              └──────────┘     └──────────────┘
                                       │
                    ┌──────────────────┤
                    │ (no-show)        │ (rider boards)
                    ▼                  ▼
              ┌──────────┐     ┌──────────────┐
              │CANCELLED │     │ IN_PROGRESS  │
              │(no-show  │     │              │
              │ fee)     │     └──────────────┘
              └──────────┘            │
                                      │ (driver ends trip)
                                      ▼
                               ┌──────────────┐
                               │  COMPLETED   │
                               │              │
                               └──────────────┘
                                      │
                                      │ (payment processed)
                                      ▼
                               ┌──────────────┐
                               │  FINALIZED   │
                               └──────────────┘

Guard conditions:
  • MATCHING → CANCELLED: only if no driver has accepted yet
  • DRIVER_EN_ROUTE → CANCELLED (by rider): cancellation fee if >2 min since match
  • DRIVER_EN_ROUTE → MATCHING: only if DRIVER cancels (re-dispatch to new driver)
  • ARRIVED → CANCELLED: only after 5-min no-show timer
  • IN_PROGRESS → COMPLETED: only driver can end trip
  • No backward transitions: can't go from COMPLETED back to IN_PROGRESS
```

---

## 6. Contrasts

### Uber Dispatch vs Traditional Taxi Dispatch

| Dimension | Uber | Traditional Taxi |
|---|---|---|
| **How** | Fully automated algorithm | Human dispatcher via radio |
| **Speed** | <5 seconds (request → driver offer) | 5-30 minutes (call → dispatcher → driver) |
| **Matching quality** | Route-based ETA, multi-factor scoring | Dispatcher's best guess based on memory |
| **Scale** | Millions of matches/day globally | Hundreds/day per dispatch center |
| **Optimization** | Global (batched assignment across all pending requests) | Local (one request at a time) |
| **Fairness** | Algorithmic (consistent scoring) | Human bias (dispatcher's preferences) |
| **Visibility** | Real-time tracking on map | "Your cab will be there in about 10 minutes" |

### Uber Dispatch vs Food Delivery Dispatch (DoorDash)

| Dimension | Uber | DoorDash |
|---|---|---|
| **Parties** | 2 (rider ↔ driver) | 3 (customer ↔ restaurant ↔ courier) |
| **Prep time** | None — trip starts immediately on pickup | 10-45 min restaurant prep (unpredictable) |
| **Batching** | Pool: 2-3 riders per trip max | Aggressive: courier picks up from 2-3 restaurants per trip |
| **Route complexity** | Pickup → dropoff (one segment) | Courier → restaurant → customer (two segments, sometimes three) |
| **Matching trigger** | Ride request (immediate dispatch) | Order placed (may delay dispatch until food is almost ready) |
| **ETA uncertainty** | Low (travel time is predictable) | High (restaurant prep time dominates variance) |
| **Optimization target** | Minimize rider wait time (pickup ETA) | Minimize food delivery time (prep + travel) while keeping food fresh |

### Greedy vs Batched — Summary

| Dimension | Greedy Matching | Batched Matching |
|---|---|---|
| **Latency** | Immediate (0 extra wait) | +2-5 seconds (batch window) |
| **Match quality** | Locally optimal | Globally optimal (within batch) |
| **Complexity** | Simple (best match per request) | Complex (assignment optimization) |
| **Best for** | Low-demand areas, sparse markets | High-demand areas, dense markets |
| **Algorithm** | Sort by score, pick top | Hungarian / auction algorithm |
| **Total ETA** | Higher (missed optimization opportunities) | ~10-20% lower (better global assignment) |
