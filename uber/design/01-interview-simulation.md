# System Design Interview Simulation: Design Uber / Lyft (Ride-Sharing Platform)

> **Interviewer:** Principal Engineer (L8), Uber Marketplace Team
> **Candidate Level:** SDE-3 (L6 — Senior Software Development Engineer)
> **Duration:** ~60 minutes
> **Date:** February 20, 2026

---

## PHASE 1: Opening & Problem Statement (~2 min)

**Interviewer:**
Welcome. I'm on the marketplace team at Uber — we own dispatch, matching, and the real-time infrastructure that connects riders and drivers. For today's system design round, I'd like you to design a **ride-sharing platform** — think Uber or Lyft. This isn't just a "map with cars" — I'm talking about real-time geospatial indexing of millions of moving points, dispatch and matching that makes sub-second decisions, dynamic pricing that balances supply and demand, trip lifecycle management with event sourcing, and the real-time communication infrastructure that ties it all together.

I care about how you think about the real-time geospatial challenge, the matching problem, and the trade-offs that make ride-sharing at scale work. I'll push on your choices — that's calibration, not criticism.

Take it away.

---

## PHASE 2: Requirements Gathering & Scoping (~5 min)

**Candidate:**
Thanks! Ride-sharing is a fascinating real-time marketplace. Let me scope this carefully — it spans geospatial indexing, dispatch, routing, pricing, payments, and real-time communication, each a deep topic.

**Functional Requirements — what operations do we need?**

> "Let me identify the core user-facing operations from both sides — rider and driver:
>
> **Rider-side:**
> - **Request a ride** — Specify pickup and dropoff, see upfront price, choose ride type (UberX, XL, Black, Pool)
> - **Live tracking** — Watch the driver's car move on the map in real-time during pickup and trip
> - **Trip lifecycle** — See driver info, ETA, route, fare breakdown, and receipt
> - **Rate & tip** — Rate the driver post-trip, add tip
> - **Payment** — Stored payment methods, automatic charging, fare disputes
>
> **Driver-side:**
> - **Go online/offline** — Toggle availability, send GPS location every 3-4 seconds while online
> - **Receive ride offers** — See pickup location, ETA, estimated fare, accept within 15 seconds
> - **Navigation** — Turn-by-turn directions to pickup and dropoff
> - **Earnings** — Track earnings, request payouts
>
> **Platform-side:**
> - **Dispatch** — Match riders with nearby available drivers
> - **Dynamic pricing** — Compute surge multipliers per geographic cell based on supply/demand
> - **ETA computation** — Route-based ETA at millions of QPS
>
> A few clarifying questions:
> - **Should I cover Uber Pool (shared rides)?** It's architecturally interesting — it turns dispatch from assignment into a vehicle routing problem."

**Interviewer:** "Yes, cover Pool at a high level — it's a great way to show you understand the matching complexity. Don't deep-dive the VRP solver."

> "- **Should I cover food delivery (Uber Eats)?** It's a three-party matching problem (customer ↔ restaurant ↔ courier) vs ride-sharing's two-party problem."

**Interviewer:** "Mention it as a contrast — different matching complexity, different ETA components. But focus on ride-sharing."

> "- **Geographic scope?** Uber operates in 10,000+ cities across 70+ countries."

**Interviewer:** "Yes, global. That makes the multi-region architecture discussion interesting."

**Non-Functional Requirements:**

> "Now the critical constraints. Ride-sharing is defined by its real-time properties:
>
> | Dimension | Requirement | Rationale |
> |---|---|---|
> | **Dispatch latency** | < 5 seconds from request to driver offer | Rider taps 'Request' → driver sees the offer. Each second of waiting feels like an eternity. Budget: API gateway (~100ms) → pricing (~200ms) → nearby drivers (~100ms) → dispatch decision (~500ms) → push to driver (~100ms) |
> | **ETA query latency** | < 100ms per query | ETA is the most frequently computed value — shown to riders, used for matching, used for fare estimation. Millions of QPS. |
> | **GPS ingestion** | ~1.5M updates/second at peak | ~5M active drivers, each sending GPS every 3-4 seconds. The geospatial index must absorb and query this firehose. |
> | **Pickup time** | < 3 minutes average in major metros | This is the product metric that matters most. If pickup takes 10 minutes, riders switch to Lyft or hail a taxi. |
> | **Location tracking** | < 2 seconds end-to-end | Driver GPS → server → rider's map. The car icon must feel "live." |
> | **Availability** | 99.99% for dispatch | If dispatch is down, riders are stranded. This isn't "video won't play" — it's "person can't get home." |
> | **Scale** | ~30M+ trips/day, trillions of Kafka messages/day | Kafka is the backbone for location updates, trip events, pricing events. |
> | **Surge refresh** | Every 1-2 minutes per geographic cell | Supply/demand changes quickly. Stale surge = incorrect pricing = market imbalance. |

**Interviewer:**
Good scoping. You called out the real-time geospatial challenge — the location firehose and the moving index. That's what makes this fundamentally different from a CRUD application. Why did you emphasize dispatch latency so specifically?

**Candidate:**

> "Because dispatch is the critical path of the entire system. Every second between the rider tapping 'Request' and the driver seeing the offer is a second the rider is staring at a spinner, wondering if the app is broken. And if we're slow, we lose to Lyft — riders will switch apps and whoever assigns a driver first wins. More importantly, dispatch latency directly determines pickup time: a 5-second dispatch decision followed by a 3-minute drive is fine. A 30-second dispatch decision followed by the same drive means the rider waited 30 seconds for no reason. And at Uber's scale — 30M+ trips per day — even a 1-second improvement in dispatch latency saves ~30 million seconds of rider waiting per day. That's about 347 person-days of waiting, every single day."

**Interviewer:**
Excellent quantitative reasoning. Let's get into APIs.

---

### L5 vs L6 vs L7 — Phase 2 Rubric

| Aspect | L5 (SDE-2) | L6 (SDE-3) | L7 (Principal) |
|---|---|---|---|
| **Functional Reqs** | Lists request ride, track driver, pay | Proactively raises both rider AND driver perspectives, mentions Pool as matching complexity, contrasts with food delivery | Additionally discusses safety features, regulatory compliance, driver supply management, insurance |
| **Non-Functional** | Mentions latency and availability | Quantifies latency budget breakdown, GPS ingestion rate (~1.5M/sec), explains pickup time as the product metric | Frames NFRs in business impact: dispatch latency → rider retention, surge accuracy → market clearing efficiency, availability → safety implications |
| **Taxi Contrast** | Doesn't mention taxis | Notes taxis have no real-time tracking, manual dispatch, fixed pricing | Explains how each Uber architectural choice (spatial index, dynamic pricing, automated dispatch) solves a specific inefficiency of the traditional taxi model |

---

## PHASE 3: API Design (~5 min)

**Candidate:**

> "Let me focus on the APIs that define the ride-sharing domain — ride request, driver location, and dispatch. The full API surface is broader (ratings, payments, driver management, safety) — documented in [02-api-contracts.md](02-api-contracts.md)."

### Ride Request API (the critical path)

> ```
> POST /v1/rides/request
> {
>   "riderId": "rider_abc123",
>   "pickup": { "lat": 40.7484, "lng": -73.9856 },
>   "dropoff": { "lat": 40.7580, "lng": -73.9855 },
>   "rideType": "UBER_X",
>   "paymentMethodId": "pm_xyz789",
>   "promoCode": "SAVE5"
> }
>
> Response:
> {
>   "rideId": "ride_def456",
>   "status": "MATCHING",
>   "upfrontPrice": {
>     "amount": 18.50,
>     "currency": "USD",
>     "breakdown": {
>       "baseFare": 2.55,
>       "distance": 5.60,
>       "time": 4.20,
>       "surgeMultiplier": 1.5,
>       "surgeAmount": 6.18,
>       "bookingFee": 2.75,
>       "promoDiscount": -2.78
>     }
>   },
>   "estimatedPickupTime": 180,
>   "estimatedTripDuration": 720
> }
> ```
>
> "Key design choice: the response includes the **upfront price** with a full breakdown. This is the price the rider commits to — not an estimate. If the actual trip costs more (traffic, detour), the platform absorbs the difference. This was a major architectural shift in 2016 — it decoupled rider payment from driver earnings."

### Driver Location API (the firehose)

> ```
> PUT /v1/drivers/{driverId}/location
> {
>   "lat": 40.7490,
>   "lng": -73.9854,
>   "heading": 180,
>   "speed": 12.5,
>   "accuracy": 8,
>   "timestamp": 1710512345
> }
>
> Response: 204 No Content (fire-and-forget — minimizes latency)
> ```
>
> "This endpoint receives ~1.5 million calls per second at peak. The response is 204 (no body) to minimize latency. The update flows: driver phone → API gateway → Kafka → geospatial index (Redis) + Cassandra (history). The API gateway does NOT wait for downstream processing — it acknowledges the update immediately and lets Kafka handle the fan-out."

### Dispatch Flow (internal, not external API)

> "Dispatch is an internal flow, not a public API:
> ```
> Ride request arrives
>   → Pricing Service: compute upfront price (ETA + route + surge)
>   → Geospatial Index: find available drivers within radius
>   → Dispatch Service: rank candidates by ETA, heading, rating, acceptance probability
>   → Select best driver → send ride offer via WebSocket + push notification
>   → Driver accepts → trip state: MATCHING → DRIVER_EN_ROUTE
>   → Driver declines/timeout → cascade to next driver
> ```"

**Interviewer:**
I notice you made the location update fire-and-forget with a 204 response. What happens if the update is lost?

**Candidate:**

> "Great question. GPS updates are **best-effort, not exactly-once**. If an update is lost — network blip, Kafka consumer lag — the next update arrives 3-4 seconds later and supersedes it. A lost location update is not lost information — it's superseded information. This is fundamentally different from, say, WhatsApp messaging where every message must be delivered. In Uber's domain, stale data is replaced by fresh data continuously. We don't need store-and-forward; we need high throughput and low latency."

**Interviewer:**
Good distinction. That's the right mental model for real-time location data vs messaging.

---

### L5 vs L6 vs L7 — Phase 3 Rubric

| Aspect | L5 (SDE-2) | L6 (SDE-3) | L7 (Principal) |
|---|---|---|---|
| **API Design** | Basic REST endpoints for ride request and location | Explains upfront pricing breakdown, fire-and-forget location updates (204), dispatch as internal flow | Discusses idempotency keys for ride requests, API versioning strategy, rate limiting per rider, anti-abuse (GPS spoofing detection) |
| **Data Model** | Lists fields | Explains WHY fields exist (heading for driver direction matching, accuracy for GPS quality filtering) | Discusses event sourcing for trip state, CQRS pattern (command vs query separation), API evolution when adding ride types |
| **Contrast** | None | Contrasts fire-and-forget location (Uber) vs store-and-forward messaging (WhatsApp) | Contrasts two-party ride API vs three-party food delivery API (restaurant prep time, courier assignment, customer notifications) |

---

## PHASE 4: Architecture Evolution (~25 min)

**Candidate:**

> "I'll evolve the architecture iteratively. Each attempt solves a specific problem and introduces the next challenge. This mirrors how Uber actually evolved."

---

### Attempt 0: Single Server with a Database

> ```
> ┌──────────────────────────────────────────────────────┐
> │                    Single Server                      │
> │                                                       │
> │  Web Server (REST API)                                │
> │     │                                                 │
> │     ▼                                                 │
> │  PostgreSQL Database                                  │
> │     • drivers (id, lat, lng, status, updated_at)      │
> │     • rides (id, rider_id, driver_id, status, fare)   │
> │     • users (id, name, email, payment_method)         │
> │                                                       │
> │  Dispatch Logic:                                      │
> │     SELECT * FROM drivers                             │
> │     WHERE status = 'AVAILABLE'                        │
> │     ORDER BY distance(lat, lng, pickup_lat, pickup_lng)│
> │     LIMIT 1                                           │
> │                                                       │
> │  Location Updates:                                    │
> │     Every 30 seconds, driver app calls:               │
> │     UPDATE drivers SET lat=?, lng=? WHERE id=?        │
> └──────────────────────────────────────────────────────┘
> ```
>
> "This is the absolute baseline. A single server, a SQL database, and a table scan to find the nearest driver. It works for a prototype — maybe 100 drivers in one city."

**What's broken?**

> "Everything, at scale:
> 1. **No spatial indexing**: `ORDER BY distance(...)` scans ALL drivers — O(n) per query. With 5M drivers, this takes seconds.
> 2. **Stale locations**: 30-second updates mean a driver could have moved 500+ meters. The 'nearest driver' might be blocks away.
> 3. **No dynamic pricing**: During peak demand, all rides are the same price → excess demand → long wait times → frustrated riders.
> 4. **Single server**: No horizontal scaling, single point of failure. If the server crashes, no one can ride.
> 5. **No real-time tracking**: Rider can't see the driver approaching. The experience is 'request and hope.'
> 6. **No route-based ETA**: Straight-line distance ignores rivers, highways, one-way streets. A driver 500m away across a river might be 15 minutes away by road."

**Interviewer:**
Right. This is a useful starting point to identify the fundamental challenges. What do you tackle first?

**Candidate:**

> "The geospatial index and real-time location updates. Without those, you can't find nearby drivers efficiently, and without frequent GPS updates, you can't make accurate matching decisions."

---

### Attempt 1: Real-Time Location + Geospatial Index

> ```
> ┌──────────────────────────────────────────────────────────────┐
> │                                                              │
> │  Driver's Phone ──(GPS every 3-4s)──> API Gateway            │
> │                                           │                  │
> │                                           ▼                  │
> │                                     Kafka Topic:             │
> │                                     driver.location          │
> │                                           │                  │
> │                              ┌────────────┼────────────┐     │
> │                              ▼            ▼            ▼     │
> │                        Geospatial    Cassandra     Analytics  │
> │                        Index         (history)     Pipeline  │
> │                        (Redis)                               │
> │                           │                                  │
> │  Rider requests ride ──>  │  Dispatch Service:               │
> │                           │  1. Query: nearby available       │
> │                           │     drivers within 3km radius    │
> │                           │  2. Rank by straight-line distance│
> │                           │  3. Send ride offer to nearest   │
> │                           │  4. Wait 15s for acceptance      │
> │                           │  5. If decline → next driver     │
> │                                                              │
> │  Geospatial Index:                                           │
> │    Data structure: geohash → Set[driverId]                   │
> │    OR: Redis GEOADD for built-in radius search               │
> │    Query: GEORADIUS(pickup_lat, pickup_lng, 3km)             │
> │    Latency: <10ms for nearby driver query                    │
> │                                                              │
> │  PostgreSQL: trips, users, payments (transactional data)     │
> └──────────────────────────────────────────────────────────────┘
> ```
>
> "Now we have:
> - **GPS every 3-4 seconds** (instead of 30): driver locations are fresh within seconds.
> - **Geospatial index in Redis**: nearby driver queries in <10ms instead of seconds.
> - **Kafka for fan-out**: location updates consumed by multiple services independently.
> - **Greedy dispatch**: find nearest, send offer, cascade on decline.
>
> **Contrast with traditional taxis**: Taxis have no GPS tracking, no spatial index. A dispatcher relies on radio communication and memory — 'Car 47, there's a pickup at 34th and 5th.' Uber's automated spatial index + greedy dispatch already outperforms human dispatchers at any scale."

**What's broken?**

> "1. **Straight-line ETA is inaccurate**: A driver 1km away on the same road is 2 minutes away. A driver 1km away across a river is 15 minutes away. We're ranking by the wrong metric.
> 2. **Greedy dispatch is locally optimal but globally suboptimal**: Matching the nearest driver to each rider independently may leave other riders with much longer waits.
> 3. **No dynamic pricing**: High demand → long wait times. No mechanism to balance supply and demand.
> 4. **No real-time rider tracking**: The rider still can't see the driver on the map."

---

### L5 vs L6 vs L7 — Attempt 0→1 Rubric

| Aspect | L5 (SDE-2) | L6 (SDE-3) | L7 (Principal) |
|---|---|---|---|
| **Spatial Index** | Mentions "use a spatial index" | Specifically proposes geohash or Redis GEOADD, explains O(1) vs O(n) tradeoff, sizes the write throughput (~1.5M/sec) | Discusses geohash vs quadtree vs H3, explains corner problem, proposes sharding strategy by geography |
| **Location Pipeline** | Driver sends location to server | Kafka-based fan-out, explains why fire-and-forget (not exactly-once), Cassandra for history | Discusses map matching (HMM/Viterbi), GPS noise filtering, heading/speed validation, compression for bandwidth |
| **Dispatch** | "Find the nearest driver" | Explains greedy dispatch, cascade on decline, 15-second timeout, why greedy is suboptimal | Quantifies the suboptimality gap, explains when batched dispatch is worth the added latency |

---

### Attempt 2: Route-Based ETA + Basic Surge Pricing

> ```
> NEW COMPONENTS:
>
> ┌─────────────────────────────────────────────────────────┐
> │  Routing Engine                                         │
> │                                                         │
> │  Road network graph from OpenStreetMap:                 │
> │    ~24M nodes (intersections), ~58M edges (road segments)│
> │                                                         │
> │  Algorithm: Contraction Hierarchies (CH)                │
> │    Preprocessing: 1-4 hours (done once, offline)        │
> │    Query: <10ms (bidirectional search UP the hierarchy)  │
> │    1000x faster than Dijkstra                           │
> │                                                         │
> │  Why CH, not Dijkstra?                                  │
> │    Dijkstra on 24M nodes: ~1-5 seconds per query        │
> │    CH on 24M nodes: <10 milliseconds per query          │
> │    At millions of QPS, this difference is existential.   │
> │                                                         │
> │  Traffic-aware: Use driver GPS traces to estimate       │
> │    real-time speed per road segment. Adjust edge weights.│
> │    traffic_multiplier = base_speed / current_speed      │
> └─────────────────────────────────────────────────────────┘
>
> ┌─────────────────────────────────────────────────────────┐
> │  Surge Pricing Service                                  │
> │                                                         │
> │  Divide city into grid cells (geohash or hex cells).    │
> │  For each cell, every 1-2 minutes:                     │
> │    supply = count(available drivers in cell)             │
> │    demand = count(ride requests in last 2 min)           │
> │    ratio = demand / supply                               │
> │    multiplier = surgeFunction(ratio)                     │
> │                                                         │
> │  Rider sees: "Your ride is 1.8x surge"                  │
> │  Driver sees: heat map of surge zones                   │
> │                                                         │
> │  Economic effect:                                       │
> │    Higher price → fewer riders request → demand drops    │
> │    Higher earnings → more drivers go online → supply grows│
> │    → Market clears: supply ≈ demand                     │
> └─────────────────────────────────────────────────────────┘
>
> UPDATED DISPATCH:
>   Rank drivers by ROUTE-BASED ETA (not straight-line distance).
>   Factor in: driver heading (already facing the right direction?),
>              road conditions, traffic on the route to pickup.
> ```
>
> "Now dispatch ranks by actual driving time, not straight-line distance. And surge pricing creates a feedback loop that balances supply and demand.
>
> **Contrast with Google Maps**: Google Maps also uses Contraction Hierarchies for routing, but it optimizes for general-purpose navigation. Uber's routing is specialized: it considers which side of the road the rider is on (to avoid U-turns at pickup), the driver's current heading (a driver heading north won't be matched with a rider to the south if it requires a U-turn), and pickup-specific ETA (including the time to pull over and find the rider)."

**What's broken?**

> "1. **Greedy dispatch is still suboptimal**: We're ranking by ETA now (better than distance), but still matching one rider at a time.
> 2. **No real-time tracking**: Rider still can't see the driver on the map.
> 3. **No payment system**: Still cash-based or manual. No automated payment flow.
> 4. **No trip state machine**: No formal lifecycle management. What happens if the rider cancels? If the driver cancels? If there's a safety incident?
> 5. **Single region**: Can't scale internationally."

---

### Attempt 3: Batched Dispatch + Real-Time Tracking + Payments

> ```
> NEW COMPONENTS:
>
> ┌─────────────────────────────────────────────────────────┐
> │  Batched Dispatch (in dense markets)                    │
> │                                                         │
> │  Instead of matching each rider immediately:            │
> │  1. Accumulate requests for 2-5 seconds                 │
> │  2. Solve assignment problem: riders ↔ drivers           │
> │     to minimize total ETA across all riders              │
> │  3. Use Hungarian algorithm or auction-based solver      │
> │                                                         │
> │  Example:                                               │
> │    3 riders (R1, R2, R3) and 3 drivers (D1, D2, D3)    │
> │    ETA matrix:                                          │
> │         D1    D2    D3                                  │
> │    R1 [ 3     7     5  ]                                │
> │    R2 [ 8     2     4  ]                                │
> │    R3 [ 5     6     3  ]                                │
> │                                                         │
> │    Greedy: R1→D1(3), R2→D2(2), R3→D3(3) = total 8 min  │
> │    Optimal may yield total 7 min (depends on full matrix)│
> │                                                         │
> │  Trade-off: 2-5 seconds of added latency for better     │
> │  global matching quality. Worth it in dense markets.    │
> └─────────────────────────────────────────────────────────┘
>
> ┌─────────────────────────────────────────────────────────┐
> │  WebSocket Gateway (Real-Time Tracking)                 │
> │                                                         │
> │  Persistent bidirectional connection:                   │
> │    Rider ←→ Gateway ←→ Backend Services                 │
> │                                                         │
> │  Data flow for live tracking:                           │
> │    Driver GPS → Server → map-match → snap to road       │
> │    → push to rider via WebSocket → render car on map    │
> │    Latency: <2 seconds end-to-end                       │
> │                                                         │
> │  Scaling: ~15-25M concurrent connections                │
> │  Each gateway instance: ~50K-100K connections            │
> │  Need: ~200-400 instances                               │
> │  Connection routing: Redis (userId → gatewayInstanceId) │
> │  Sticky sessions at L4 load balancer                    │
> └─────────────────────────────────────────────────────────┘
>
> ┌─────────────────────────────────────────────────────────┐
> │  Payment Service                                        │
> │                                                         │
> │  Trip completes → fare calculated → charge rider async  │
> │                                                         │
> │  Ledger-based accounting (double-entry):                │
> │    Rider debit = Driver credit + Platform commission     │
> │    The books always balance.                            │
> │                                                         │
> │  Asynchronous: rider exits car → payment in background  │
> │  If charge fails → retry → fallback card → Uber Cash   │
> │                                                         │
> │  Driver payouts: weekly (ACH) or instant (debit push)   │
> └─────────────────────────────────────────────────────────┘
>
> ┌─────────────────────────────────────────────────────────┐
> │  Trip State Machine (Event Sourcing)                    │
> │                                                         │
> │  IDLE → MATCHING → DRIVER_EN_ROUTE → ARRIVED            │
> │       → IN_PROGRESS → COMPLETED → FINALIZED             │
> │                                                         │
> │  Every state transition = immutable event:              │
> │    { tripId, eventType, timestamp, gps, metadata }      │
> │  Events stored in Kafka + Cassandra                     │
> │  Current state = projection of events (materialized     │
> │    in MySQL/Schemaless for fast reads)                   │
> │                                                         │
> │  Why event sourcing? Trips involve money, safety,       │
> │  legal liability. Full history is non-negotiable for    │
> │  disputes, insurance, and regulatory compliance.        │
> └─────────────────────────────────────────────────────────┘
> ```
>
> "**Contrast with food delivery**: DoorDash dispatch is fundamentally harder because it must consider restaurant prep time — a highly unpredictable variable. A restaurant might say '15 minutes' but take 30 minutes. Ride-sharing dispatch only depends on travel time, which is predictable from road conditions and traffic data. Food delivery also has three parties to pay (restaurant, courier, platform), not two."

**What's broken?**

> "1. **No shared rides (Pool)**: Two riders going in the same direction take separate cars — wastes driver capacity and costs riders more.
> 2. **No supply management**: Drivers cluster in some areas, desert others. No mechanism to guide driver positioning.
> 3. **No ML-powered ETA**: Contraction Hierarchies give good ETAs but miss time-of-day patterns, weather effects, traffic light timing.
> 4. **Still metered pricing**: Rider doesn't know the exact cost until the trip ends — anxiety, surprise bills.
> 5. **Single region**: All infrastructure in one data center. If it goes down, the entire market stops."

---

### L5 vs L6 vs L7 — Attempt 2→3 Rubric

| Aspect | L5 (SDE-2) | L6 (SDE-3) | L7 (Principal) |
|---|---|---|---|
| **Dispatch** | "Match nearest driver by ETA" | Explains batched dispatch, shows the assignment matrix, quantifies when greedy vs batched is appropriate (dense vs sparse markets) | Discusses dynamic batch window (adjusts based on market density), A/B testing dispatch algorithms, handling heterogeneous ride types in the same batch |
| **Real-Time Tracking** | "Push driver location to rider" | WebSocket gateway architecture, connection routing via Redis, client-side interpolation for smooth animation | Discusses graceful degradation on gateway failure, binary protocol for bandwidth efficiency, priority queues (ride offers > location updates) |
| **Payments** | "Charge the rider" | Ledger-based double-entry, async processing, retry with fallback payment methods | Discusses upfront pricing decoupling (rider payment vs driver earnings), revenue recognition timing, cross-border payment challenges |

---

### Attempt 4: Pool/Shared Rides + ML + Upfront Pricing

> ```
> NEW COMPONENTS:
>
> ┌─────────────────────────────────────────────────────────┐
> │  Uber Pool / Shared Rides                               │
> │                                                         │
> │  Match a new rider with an IN-PROGRESS trip going in    │
> │  the same direction.                                    │
> │                                                         │
> │  This is a variant of the Vehicle Routing Problem (VRP):│
> │    NP-hard → use heuristics, not exact solutions.       │
> │                                                         │
> │  Matching criteria:                                     │
> │    • Direction similarity (< 30° deviation)             │
> │    • Detour threshold (< 5 min added for existing rider)│
> │    • Pickup proximity (new rider within 3 min of route) │
> │    • Remaining capacity (vehicle has empty seats)        │
> │                                                         │
> │  Economics:                                             │
> │    Each rider pays ~40% less than a solo ride.           │
> │    Driver earns ~15-20% more per hour (more riders per  │
> │    hour, less deadheading between trips).                │
> │    Platform revenue per driver-hour increases.           │
> └─────────────────────────────────────────────────────────┘
>
> ┌─────────────────────────────────────────────────────────┐
> │  ML-Powered ETA (via Michelangelo)                      │
> │                                                         │
> │  Routing engine (CH) gives a good baseline ETA.         │
> │  ML model applies a correction factor:                  │
> │    correctedETA = routingETA × mlCorrectionFactor        │
> │                                                         │
> │  Features:                                              │
> │    • Routing engine ETA (baseline)                       │
> │    • Time of day, day of week                           │
> │    • Weather (rain/snow → slower)                       │
> │    • Special events (concert ending → traffic spike)    │
> │    • Historical accuracy for this route                 │
> │    • Number of turns, traffic lights                    │
> │                                                         │
> │  Training: billions of historical trips                 │
> │    (predicted ETA vs actual trip duration)               │
> │  Retraining: daily on latest trip data                  │
> │  Accuracy improvement: ~10-15% over routing-only ETA    │
> │                                                         │
> │  Deployed on Michelangelo (Uber's ML platform):         │
> │    Feature Store (online: Redis, offline: Hive)         │
> │    Model serving: <5ms inference latency                │
> └─────────────────────────────────────────────────────────┘
>
> ┌─────────────────────────────────────────────────────────┐
> │  Upfront Pricing (2016)                                 │
> │                                                         │
> │  Show rider the EXACT price before the trip:            │
> │    upfront_price = (base + per_mile × est_miles         │
> │                     + per_min × est_minutes)            │
> │                    × surge + tolls + booking_fee - promo │
> │                                                         │
> │  If actual trip costs more → platform absorbs loss.     │
> │  If actual trip costs less → platform keeps margin.     │
> │  Over millions of trips, variance averages out.         │
> │                                                         │
> │  KEY: This decouples rider payment from driver earnings.│
> │    Rider pays a price. Driver earns based on actuals.   │
> │    Platform manages the spread.                         │
> └─────────────────────────────────────────────────────────┘
>
> ┌─────────────────────────────────────────────────────────┐
> │  Supply Management                                      │
> │                                                         │
> │  • Driver heat maps: show where demand is high          │
> │  • Surge zones visible on driver app                    │
> │  • Quest bonuses: "Complete 50 trips this week = $100"  │
> │  • ML demand forecasting: predict demand 15-30 min ahead│
> │    → position drivers BEFORE the demand materializes    │
> │                                                         │
> │  Contrast with airlines:                                │
> │    Airlines use revenue management (dynamic pricing)    │
> │    at a much slower timescale (hours/days).              │
> │    Both optimize for market clearing, but Uber's market │
> │    clears in minutes while airlines' clears over weeks. │
> │    Airlines have fixed supply (seats); Uber's supply    │
> │    is elastic (drivers can go online/offline).           │
> └─────────────────────────────────────────────────────────┘
> ```

**What's broken?**

> "1. **Single-region infrastructure**: If the data center fails, rides stop in that market. A rider in NYC and a rider in London use the same dispatch infrastructure.
> 2. **Kafka bottleneck**: Trillions of messages, consumer lag during traffic spikes. No dead letter queues for failed messages.
> 3. **No chaos engineering**: Cascading failures between dispatch, routing, pricing, and location services are untested.
> 4. **Geohash limitations for surge**: Square grid cells have non-uniform adjacency (corner problem). Creates sharp surge boundaries."

---

### Attempt 5: Multi-Region + Production Hardening + H3

> ```
> ┌──────────────────────────────────────────────────────────────┐
> │                    FINAL ARCHITECTURE                        │
> │                                                              │
> │  ┌─────────────┐  ┌─────────────┐  ┌─────────────┐          │
> │  │ US Region   │  │ EU Region   │  │ APAC Region │          │
> │  │             │  │             │  │             │          │
> │  │ Dispatch    │  │ Dispatch    │  │ Dispatch    │          │
> │  │ Location Svc│  │ Location Svc│  │ Location Svc│          │
> │  │ Routing/ETA │  │ Routing/ETA │  │ Routing/ETA │          │
> │  │ Surge       │  │ Surge       │  │ Surge       │          │
> │  │ Trip Svc    │  │ Trip Svc    │  │ Trip Svc    │          │
> │  │ WS Gateway  │  │ WS Gateway  │  │ WS Gateway  │          │
> │  │ Kafka       │  │ Kafka       │  │ Kafka       │          │
> │  │ Redis       │  │ Redis       │  │ Redis       │          │
> │  │ MySQL shards│  │ MySQL shards│  │ MySQL shards│          │
> │  └──────┬──────┘  └──────┬──────┘  └──────┬──────┘          │
> │         │                │                │                  │
> │         └────────────────┼────────────────┘                  │
> │                          │                                   │
> │              ┌───────────▼────────────┐                      │
> │              │   Global Services     │                      │
> │              │                       │                      │
> │              │   User Accounts      │                      │
> │              │   Payment Methods     │                      │
> │              │   Driver Profiles     │                      │
> │              │   Auth / Identity     │                      │
> │              │   (Multi-region       │                      │
> │              │    replicated)        │                      │
> │              └───────────────────────┘                      │
> │                                                              │
> │  H3 Hexagonal Grid (Uber's open-source spatial index):      │
> │    Resolution 9 (~175m cells) for surge pricing              │
> │    Uniform 6-neighbor adjacency → smooth surge gradients     │
> │    No corner problem (unlike geohash squares)                │
> │    Hierarchical: aggregate to coarser resolutions for        │
> │    city-level demand forecasting                             │
> │                                                              │
> │  Kafka at Scale:                                             │
> │    Regional clusters (separate Kafka per region)             │
> │    uReplicator for cross-DC replication                      │
> │    Dead letter queues for failed messages                    │
> │    Consumer lag monitoring + auto-scaling                    │
> │    Backpressure for burst traffic                            │
> │                                                              │
> │  Resilience:                                                 │
> │    Circuit breakers (if routing slow → straight-line ETA)    │
> │    Bulkhead (separate thread pools per ride type)            │
> │    Retry with exponential backoff + jitter                   │
> │    Graceful degradation (surge stale → use last known values)│
> │                                                              │
> │  Safety Features:                                            │
> │    Trip sharing (send link to friends/family)                │
> │    Emergency button → alert safety team + 911               │
> │    Driver photo verification                                │
> │    GPS trip recording (every trip is logged)                 │
> │    Speed alerts during active trips                          │
> │                                                              │
> │  Michelangelo (ML Platform):                                │
> │    ETA prediction, surge forecasting, fraud detection,       │
> │    driver destination prediction — all centralized           │
> │    Feature Store: online (Redis) + offline (Hive)            │
> │    Model registry, A/B testing, monitoring                  │
> └──────────────────────────────────────────────────────────────┘
> ```
>
> "**Contrast with Lyft**: Lyft's architecture is similar in broad strokes — geospatial index, dispatch, dynamic pricing, real-time tracking. Key differences: Uber open-sourced H3 (Lyft uses standard geohash/S2), Uber built Michelangelo as a centralized ML platform, Uber built Schemaless (custom MySQL sharding). Lyft has published less about its internals. Uber's dataset advantage (more trips → more GPS data → better traffic models → better ETA → better matching → more riders → more trips) creates a data flywheel that's hard to replicate."

**Interviewer:**
Good evolution. I want to pull on several threads now. Let's start with the geospatial challenge.

---

### L5 vs L6 vs L7 — Attempt 4→5 Rubric

| Aspect | L5 (SDE-2) | L6 (SDE-3) | L7 (Principal) |
|---|---|---|---|
| **Multi-Region** | "Deploy in multiple regions" | Explains what's regional (trips, dispatch, surge) vs global (accounts, payments), DNS-based routing, cross-region replication | Discusses consistency challenges for globally mobile users, conflict resolution for concurrent account updates, regulatory data residency requirements |
| **Resilience** | "Add redundancy" | Circuit breakers, graceful degradation with specific fallbacks per service, retry with backoff | Discusses chaos engineering practices, bulkhead isolation per ride type, blast radius containment, pre-scaling for known events |
| **H3 / Spatial** | "Use a grid" | Explains H3 hexagonal advantages (uniform adjacency, no corner problem), resolution choice, smooth surge gradients | Discusses resolution 9 vs 10 dynamic switching for events, cross-resolution aggregation, H3 vs S2 for different use cases |

---

## PHASE 5: Deep Dive — Geospatial Index & Location Service (~5 min)

**Interviewer:**
Let's dig into the geospatial index. You have ~1.5 million GPS updates per second. Walk me through how you ingest, index, and query that.

**Candidate:**

> "The location pipeline has three stages: ingest, index, and query.
>
> **Ingest**: Driver's phone sends GPS (lat, lng, heading, speed, accuracy) every 3-4 seconds. Hits the API gateway → written to Kafka topic `driver.location.updates`. The API returns 204 immediately — fire-and-forget. Kafka absorbs the 1.5M msg/sec write throughput across a sharded cluster.
>
> **Index**: A Kafka consumer reads location updates and updates the geospatial index in Redis. The index structure is essentially: `H3_cell_id → Set[driverId]` and `driverId → {lat, lng, heading, speed, status, h3Cell}`. When a driver moves from one H3 cell to another (crosses a ~175m boundary), remove from old cell, add to new cell. Within-cell movement: just update the driver's coordinates. Redis handles this at sub-millisecond latency per operation.
>
> **Query**: When a ride request comes in, the dispatch service queries: 'Find available drivers near this pickup location.' The query: convert pickup lat/lng to an H3 cell → get that cell plus kRing(1) (6 adjacent cells) → union of all driverIds in those cells → filter by status=AVAILABLE → compute ETA from each driver to the pickup point → sort by ETA → return top N candidates. If not enough drivers in kRing(1), expand to kRing(2), kRing(3), etc.
>
> **Sharding**: The geospatial index is sharded by geographic region. NYC drivers are in one Redis cluster, London drivers in another. A ride request in NYC only queries the NYC shard. This keeps each shard small enough to fit in memory.
>
> For details, see [03-geospatial-indexing.md](03-geospatial-indexing.md)."

**Interviewer:**
Why H3 instead of geohash? You mentioned the 'corner problem' — explain that.

**Candidate:**

> "With a square grid (geohash), each cell has 8 neighbors: 4 edge-adjacent and 4 corner-adjacent. The edge neighbors are distance `d` away. The corner neighbors are `d × √2 ≈ 1.41d` away — 41% farther. When I do a 'nearby drivers' search by looking at adjacent cells, corner-adjacent drivers are significantly farther than edge-adjacent ones. This creates uneven coverage: drivers in corner cells appear 'nearby' but are actually much farther. You need ad-hoc corrections — expand the search radius, check additional cells, etc.
>
> With H3 hexagons, every cell has exactly 6 neighbors, all at the same distance. kRing(1) gives a uniform search radius. kRing(2) gives a larger uniform radius. No corrections needed. For surge pricing, this matters even more: smoothing multipliers across adjacent cells produces clean gradients with hexagons (all 6 neighbors at equal distance) but uneven gradients with squares (corner neighbors get over-smoothed).
>
> That said, for most companies, geohash + Redis is perfectly fine. The corner problem is a refinement, not a showstopper. H3 matters at Uber's scale where the refinement compounds across billions of queries."

---

## PHASE 6: Deep Dive — Dispatch & Matching (~5 min)

**Interviewer:**
You mentioned greedy vs batched dispatch. When would you NOT use batched?

**Candidate:**

> "Batched dispatch adds 2-5 seconds of latency. That's only worth it if the batch accumulates enough requests to enable meaningful optimization. In a dense market like Manhattan at rush hour — maybe 50 ride requests in 5 seconds — the Hungarian algorithm can find assignments that save significant total wait time vs greedy.
>
> But in a suburban area at 2 PM — maybe 1-2 requests in 5 seconds — the batch has nothing to optimize. You've added 5 seconds of latency for no benefit. The rider waits 5 seconds staring at a spinner for the same result they'd get instantly with greedy dispatch.
>
> So Uber uses both: batched in dense markets where the optimization gain exceeds the latency cost, and greedy in sparse markets where immediate matching is better. The threshold is dynamic — probably tuned by market and time-of-day based on request density.
>
> For Pool rides, batched dispatch is almost always used because the matching problem is inherently multi-rider: 'Should I add this new rider to Trip A or Trip B, or create a new trip?' This requires considering multiple options simultaneously, which greedy can't do.
>
> For details, see [04-dispatch-and-matching.md](04-dispatch-and-matching.md)."

**Interviewer:**
What if a driver declines a ride offer? Walk me through the cascade.

**Candidate:**

> "When the dispatch service selects the best driver (D1), it sends a ride offer via WebSocket + push notification. D1 has 15 seconds to accept.
>
> If D1 declines (or times out):
> 1. Remove D1 from the candidate pool for this ride.
> 2. The dispatch service already has the ranked list of candidates from the initial query. Select the next best driver (D2).
> 3. Send ride offer to D2. Another 15-second window.
> 4. If D2 also declines → D3. Typically up to 3-4 cascades.
> 5. After 3-4 declines: either expand the search radius (find drivers farther away) or notify the rider that no drivers are available.
>
> The candidate list may need to be refreshed if the cascade takes too long — drivers move, become unavailable. After ~30 seconds, the original nearby-drivers query is stale. So if we cascade past D3, we re-query the geospatial index for fresh candidates.
>
> Importantly, each ride request has an idempotency key. If a driver receives the same offer twice (network retry), the app deduplicates by offer ID."

---

## PHASE 7: Deep Dive — ETA & Routing (~5 min)

**Interviewer:**
Explain Contraction Hierarchies. Why is it 1000x faster than Dijkstra?

**Candidate:**

> "The key insight is that Dijkstra explores all directions equally — it expands outward like a ripple, touching millions of nodes before finding the destination. In a 24M-node graph, that's 1-5 seconds per query.
>
> Contraction Hierarchies exploit a simple observation: most real-world routes go 'up' to a major road (highway), travel along it, then go 'down' to a local street at the destination. This is exactly how humans drive.
>
> **Preprocessing** (done once, takes hours): Order all nodes by 'importance' — highways are most important, residential streets least. Then 'contract' nodes from least to most important. Contracting a node means: for each pair of neighbors (u, w) where u→v→w is a shortest path, add a shortcut edge u→w with the combined weight. Remove v from the active graph. This builds a hierarchy: residential streets at the bottom, highways at the top, with shortcut edges that encode 'skip the small roads.'
>
> **Query** (done millions of times, <10ms each): Run a bidirectional search — forward from the origin going UP the hierarchy, backward from the destination going UP the hierarchy. Both searches only traverse UPWARD edges. They meet at a high-importance node (a highway or major road). The path through the hierarchy is then unpacked by expanding shortcuts back to the original road segments.
>
> Why it's fast: Dijkstra explores millions of nodes in all directions. CH explores only thousands of nodes upward. The two searches meet at the 'top' of the hierarchy — like driving from your local street to the highway, taking the highway, then exiting to the destination's local street.
>
> **Traffic handling**: Standard CH has a problem — if edge weights change (traffic), you need to re-preprocess (hours). Customizable CH (CCH) separates the topology from the weights: preprocess the topology once, update weights in seconds when traffic changes. This allows real-time traffic integration without re-preprocessing.
>
> For details, see [05-eta-and-routing.md](05-eta-and-routing.md)."

---

## PHASE 8: Deep Dive — Surge Pricing & Payments (~5 min)

**Interviewer:**
Surge pricing is controversial. Why not just cap it?

**Candidate:**

> "If you cap surge at, say, 2x, what happens during extreme demand events — New Year's Eve, a concert ending in the rain?
>
> At 2x cap: demand is still much higher than supply. Many riders request, few get rides. Wait times spike to 20-30 minutes. The riders who get rides are whoever happened to request first — not necessarily the riders who need it most urgently. And drivers have no incentive to drive toward the surge zone because the earnings cap limits their upside.
>
> Without the cap: surge rises to, say, 5x. Demand drops significantly (many riders choose to wait, take transit, or walk). Supply increases (drivers see the high multiplier and drive toward the area). Within 10-15 minutes, supply and demand balance. Wait times drop to 3-5 minutes. Riders who urgently need a ride can get one — at a higher price.
>
> The economic argument is straightforward: Uber's supply is elastic. Higher prices create more supply (drivers go online). This is different from a fixed-supply market like concert tickets where surge pricing only reduces demand without increasing supply.
>
> That said, Uber DOES cap surge during declared emergencies (hurricanes, snowstorms) — this is both regulatory and PR-driven. And they've evolved from showing a visible multiplier ('2.3x surge') to just showing the upfront price ('$24.50'). This is less transparent but generates less backlash.
>
> For details, see [06-surge-pricing.md](06-surge-pricing.md)."

**Interviewer:**
Walk me through the payment flow when a trip completes.

**Candidate:**

> "Payment is asynchronous — the rider exits the car immediately, payment happens in the background.
>
> 1. Driver taps 'End Trip' → TRIP_COMPLETED event published to Kafka.
> 2. Fare Service calculates: compare upfront price vs actual trip cost (actual distance × per-mile + actual duration × per-minute). If they differ significantly, may adjust.
> 3. FARE_CALCULATED event published.
> 4. Payment Service picks up the event → charges rider's payment method via Stripe/Braintree (1-5 second API call).
> 5. If charge succeeds: PAYMENT_CHARGED event → push notification to rider with receipt.
> 6. If charge fails: retry → try backup card → deduct from Uber Cash → create outstanding balance.
>
> Ledger entries (double-entry):
> - Debit: Rider Account $18.50
> - Credit: Driver Account $13.87
> - Credit: Platform Revenue $4.63
> - (Separate entry for tip: 100% to driver, not commissionable)
>
> Key architectural insight: the rider payment and driver earnings are DECOUPLED. Rider pays the upfront price. Driver earns based on actual time + distance. The platform manages the spread. This means some trips are profitable and some aren't — it averages out over millions of trips.
>
> For details, see [07-trip-lifecycle-and-payments.md](07-trip-lifecycle-and-payments.md)."

---

## PHASE 9: Deep Dive — Data Storage & Real-Time Infrastructure (~5 min)

**Interviewer:**
Walk me through your storage strategy. How do you decide what goes where?

**Candidate:**

> "The principle is 'right tool for the right access pattern':
>
> **MySQL / Schemaless** (Uber's custom sharded MySQL layer): Transactional data — trips, users, payments. Needs ACID transactions (trip state + payment must be atomic), strong consistency (rider cancels → dispatch must see it immediately), and queryable (find trips by rider, by driver, by status).
>
> **Cassandra**: High-write-throughput, time-series-like data — driver location history (1.5M writes/sec), trip event logs (append-only). Cassandra's linear write scaling and TTL support (auto-expire old GPS data after 90 days) are perfect here. But no ACID transactions, so NOT used for payment or trip state.
>
> **Redis**: Real-time, latency-critical state — geospatial index (driver locations for nearby search), dispatch state (current ride offers), WebSocket connection routing (userId → gateway instance), rate limiting. Sub-millisecond reads, in-memory. No persistence for geospatial data — it's regenerated from GPS updates within seconds of a Redis restart.
>
> **Kafka**: The backbone for everything. Location updates, trip events, pricing events, analytics. Trillions of messages per day. Decouples producers from consumers, enables replay, supports multiple consumers per event. Uber built uReplicator for efficient cross-DC Kafka replication.
>
> **S3/HDFS**: Data lake for batch analytics and ML training. All GPS traces, trip events, pricing decisions land here. Hive/Presto for analytics queries, Spark for ML training data preparation, Michelangelo for model training.
>
> The key question to ask: 'Does this data need ACID transactions?' If yes → MySQL. 'Does it need sub-millisecond reads?' If yes → Redis. 'Is it high-write, time-series, eventually-consistent?' → Cassandra. 'Does it need multiple consumers and replay?' → Kafka.
>
> For details, see [09-data-storage-and-analytics.md](09-data-storage-and-analytics.md)."

**Interviewer:**
Why did Uber build Schemaless instead of using DynamoDB or another managed NoSQL?

**Candidate:**

> "Three reasons: cost, control, and familiarity.
>
> **Cost**: At Uber's scale, managed DynamoDB would cost significantly more than self-managed MySQL. Uber already had MySQL expertise and infrastructure.
>
> **Control**: Schemaless provides features Uber needs — custom sharding, write buffering, Change Data Capture (every write generates a Kafka event) — that DynamoDB doesn't offer in the same way. Uber can tune MySQL behavior for their specific workload.
>
> **Familiarity**: MySQL is the most widely known database. The engineering team could operate it confidently.
>
> But for most companies, this is the WRONG choice. Building a custom sharding layer requires a dedicated infrastructure engineering team. DynamoDB, PlanetScale, or Cloud Spanner are better defaults unless you're operating at Uber's scale with hundreds of infrastructure engineers. The overarching principle from this design: Uber's choices are correct FOR UBER. At smaller scale, the simpler alternative is almost always better.
>
> For details, see [11-design-trade-offs.md](11-design-trade-offs.md)."

---

## PHASE 10: Wrap-Up (~3 min)

**Interviewer:**
Good discussion. If you were on-call for this system, what keeps you up at night?

**Candidate:**

> "Three things:
>
> **1. Cascading failures during peak events.** New Year's Eve at midnight: demand spikes 5-10x. If the surge pricing pipeline is slow → stale surge → incorrect pricing → supply/demand imbalance → dispatch gets flooded with unserviceable requests → location service gets overwhelmed by the query volume → geospatial index latency spikes → dispatch degrades further. Each system's failure mode triggers the next. The mitigation is circuit breakers, bulkheads (separate thread pools per ride type), and graceful degradation (stale surge → use last known values, slow routing → straight-line ETA). Pre-scaling for known events (concerts, holidays) is critical.
>
> **2. Payment consistency across async boundaries.** The trip completes, fare is calculated, payment is charged — all asynchronously. What if the fare calculation succeeds but the payment charge fails? What if the payment charge succeeds but the receipt generation fails? What if the payment charge is duplicated (network retry)? Every step needs idempotency keys, at-least-once delivery with deduplication, and reconciliation jobs that run hourly to detect and fix inconsistencies. The ledger must always balance.
>
> **3. GPS data quality at scale.** Everything — dispatch, ETA, surge, fare calculation — depends on GPS accuracy. But GPS is inherently noisy (±5-10m), and mobile devices make it worse (urban canyons, tunnels, indoor parking). If the map matching (HMM/Viterbi) assigns a driver to the wrong road, the dispatch might match them with a rider on the other side of a highway. If GPS noise inflates trip distance, the rider gets overcharged. At 1.5M GPS updates per second, even a 0.1% error rate is 1,500 bad updates per second. Continuous monitoring of GPS quality metrics (accuracy distribution, impossible speed events, map-match confidence) is essential."

**Interviewer:**
Solid analysis. You've covered the system end-to-end with good depth on the geospatial challenge, dispatch matching, and the real-time infrastructure. Nice job connecting the trade-offs to concrete engineering decisions.

---

### L5 vs L6 vs L7 — Overall Rubric

| Aspect | L5 (SDE-2) | L6 (SDE-3) | L7 (Principal) |
|---|---|---|---|
| **Architecture** | Identifies key components (dispatch, routing, payments) | Iterative evolution (single server → geospatial → batch dispatch → Pool → multi-region), explains WHY each step was needed | Quantifies trade-offs at each step, connects architectural choices to business metrics (pickup time, rider retention, driver utilization), discusses what to measure and A/B test |
| **Geospatial** | "Use a database with geospatial queries" | H3 vs geohash trade-off, GPS ingestion pipeline, kRing-based search, sharding by geography | Map matching (HMM/Viterbi), GPS noise filtering, traffic estimation from aggregate driver speeds, how to handle coverage gaps |
| **Dispatch** | "Find the nearest available driver" | Greedy vs batched, cascade on decline, explains when each is appropriate | Pool as VRP (NP-hard → heuristics), dispatch quality metrics (total wait time, match rate, driver utilization), ML-based acceptance prediction |
| **Pricing** | "Use surge pricing when demand is high" | Surge computation per H3 cell, supply/demand measurement (actual + predicted), upfront pricing economics | ML pricing evolution, pricing experimentation (A/B testing price curves), rider/driver retention elasticity, regulatory constraints |
| **Data** | "Use a database" | Right tool per access pattern (MySQL vs Cassandra vs Redis vs Kafka), explains WHY each choice | Schemaless vs DynamoDB trade-off, event sourcing for audit/compliance, Kafka at scale (consumer lag, DLQ, uReplicator), training-serving skew in ML |
| **Real-Time** | "Push updates to the rider" | WebSocket gateway architecture, connection routing, client interpolation, hybrid push delivery | Binary protocol optimization, priority queues, backpressure, gateway failure recovery, fire-and-forget vs exactly-once |
| **Reliability** | "Add load balancers and replicas" | Circuit breakers, graceful degradation per service, availability tiers (dispatch 99.99% vs payments 99.9%) | Chaos engineering, blast radius containment, pre-scaling for events, cascading failure analysis, GPS quality monitoring |
| **Contrasts** | None | Traditional taxis (no spatial index, fixed pricing), Google Maps (no dispatch, general routing) | Food delivery (three-party matching, prep time), airlines (fixed supply, slow timescale), Lyft (similar but less data flywheel), WhatsApp (exactly-once vs best-effort) |

---

## Supporting Deep-Dive Documents

| # | Document | Focus Area |
|---|---|---|
| 02 | [02-api-contracts.md](02-api-contracts.md) | Full API surface: ride request, location, dispatch, pricing, trip lifecycle, payments, ratings, safety |
| 03 | [03-geospatial-indexing.md](03-geospatial-indexing.md) | H3 hexagonal grid, geohash/quadtree/S2 comparison, driver location service, GPS ingestion pipeline |
| 04 | [04-dispatch-and-matching.md](04-dispatch-and-matching.md) | Greedy vs batched dispatch, Hungarian algorithm, Pool/VRP, cascade reliability |
| 05 | [05-eta-and-routing.md](05-eta-and-routing.md) | Contraction Hierarchies, map matching (HMM/Viterbi), traffic estimation, ML ETA correction |
| 06 | [06-surge-pricing.md](06-surge-pricing.md) | Surge computation pipeline, supply/demand measurement, spatial/temporal smoothing, upfront pricing |
| 07 | [07-trip-lifecycle-and-payments.md](07-trip-lifecycle-and-payments.md) | Trip state machine, event sourcing, ledger-based accounting, fraud detection, fare disputes |
| 08 | [08-real-time-communication.md](08-real-time-communication.md) | WebSocket gateway, push notifications, live driver tracking, ride offer delivery, connection scaling |
| 09 | [09-data-storage-and-analytics.md](09-data-storage-and-analytics.md) | MySQL/Schemaless, Cassandra, Redis, Kafka, data lake, Michelangelo ML platform |
| 10 | [10-scaling-and-reliability.md](10-scaling-and-reliability.md) | Scale numbers, latency budgets, availability targets, graceful degradation, multi-region, chaos engineering |
| 11 | [11-design-trade-offs.md](11-design-trade-offs.md) | H3 vs geohash, greedy vs batched, upfront vs metered, event sourcing vs CRUD, WebSocket vs MQTT, build vs buy maps |
