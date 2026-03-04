Design Uber / Lyft (Ride-Sharing Platform) as a system design interview simulation.

## Template
Follow the EXACT same format as the S3 interview simulation at:
src/hld/aws/s3/design/interview-simulation.md

Same structure:
- Interviewer/Candidate dialogue format
- PHASE 1: Opening & Problem Statement
- PHASE 2: Requirements Gathering & Scoping (functional + non-functional + scale numbers)
- PHASE 3: API Design
- PHASE 4: High-Level Architecture (iterative build-up: Attempt 0 → 1 → 2 → ..., starting from single server, finding problems, evolving)
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
Create all files under: src/hld/uber/design/

## Files to create

### 1. 01-interview-simulation.md — the main backbone
The full interview dialogue covering all phases and attempts.

### 2. 02-api-contracts.md — Uber Platform APIs

This doc should list all the major API surfaces of an Uber/Lyft-like ride-sharing platform. The interview simulation (Phase 3) will only cover a subset — this doc is the comprehensive reference.

**Structure**: For each API group, list every endpoint with HTTP method, path, request/response shape, and a brief description. Mark endpoints covered in the interview with a star or highlight.

**API groups to cover**:

- **Ride Request APIs**: The core user flow. `POST /rides/request` (request a ride — pickup location, dropoff location, ride type: UberX/XL/Black/Pool, payment method, scheduled time). Returns: estimated fare, estimated pickup ETA, surge multiplier. `GET /rides/{rideId}` (ride status: MATCHING, DRIVER_EN_ROUTE, ARRIVED, IN_PROGRESS, COMPLETED, CANCELLED). `PUT /rides/{rideId}/cancel` (cancel a ride — may have cancellation fee if driver already en route). `GET /rides/{rideId}/receipt` (trip receipt: fare breakdown, distance, duration, route taken, driver info, rating prompt). The ride request triggers the most architecturally interesting component — the **dispatch system** that matches riders with nearby available drivers in real-time.

- **Driver Location APIs**: The real-time heartbeat. `PUT /drivers/location` (driver sends GPS update — latitude, longitude, heading, speed, timestamp — every 3-4 seconds while online). This generates **billions of location updates per day** globally. The system must ingest, index, and query this firehose in real-time. `GET /drivers/nearby?lat={lat}&lng={lng}&radius={r}&type={rideType}` (find available drivers near a location — used by the dispatch system). This requires a **geospatial index** that supports real-time updates and sub-second queries. `PUT /drivers/status` (toggle driver online/offline/on-trip).

- **Matching / Dispatch APIs** (internal): `POST /dispatch/match` (given a ride request, find and assign the best available driver). The matching algorithm considers: driver proximity (GPS distance), ETA to pickup (route-based, not straight-line), driver rating, vehicle type, current supply/demand balance. For **Uber Pool / Lyft Shared**: match rider with an in-progress trip going in a similar direction (shared ride routing is an NP-hard optimization problem — Uber uses heuristics). The dispatch system is event-driven — a ride request triggers matching, and if the first driver declines, it cascades to the next best driver.

- **Pricing / Fare APIs**: `GET /rides/estimate` (fare estimate before requesting — base fare + per-mile + per-minute + surge multiplier + booking fee, less any promotions). `GET /pricing/surge?lat={lat}&lng={lng}` (current surge multiplier for a location). **Surge pricing (dynamic pricing)** is the most controversial and interesting pricing component: when demand exceeds supply in a geographic area, prices increase to incentivize more drivers to enter the area and to reduce marginal demand. Surge is computed per **geospatial cell** (hexagonal grid using H3 or square grid) based on real-time supply/demand ratio. `POST /rides/{rideId}/fare` (finalize fare after trip — actual distance, actual duration, tolls, surge at time of request). **Upfront pricing**: Uber moved from metered fares (pay for actual time+distance) to upfront fares (quoted price before trip) — this shifts risk from rider to platform but requires accurate ETA and route prediction.

- **ETA / Routing APIs**: `GET /eta?origin={lat,lng}&destination={lat,lng}` (estimated time of arrival — the most frequently called API). ETA computation uses: road network graph (OpenStreetMap or proprietary), real-time traffic data (from driver GPS traces), historical traffic patterns (time-of-day, day-of-week), and ML models for prediction. `GET /route?origin={lat,lng}&destination={lat,lng}` (turn-by-turn navigation route). **ETA accuracy is critical** — it's displayed to riders waiting for pickup, used for fare estimation, and used for driver assignment. Inaccurate ETAs erode trust. Uber uses a combination of **Dijkstra/A\* on a road graph** partitioned with **Contraction Hierarchies** for fast shortest-path queries, plus ML-based corrections for real-time traffic.

- **Trip Lifecycle APIs**: `POST /trips/start` (driver confirms pickup — trip begins, meter starts), `POST /trips/end` (driver confirms dropoff — trip ends, fare finalized), `GET /trips/{tripId}/route` (real-time route tracking — rider sees driver's live location during trip). Trip events are recorded as an immutable event log: REQUESTED → MATCHED → DRIVER_EN_ROUTE → ARRIVED → STARTED → COMPLETED (or CANCELLED at any point). Each event includes timestamp, GPS coordinates, and metadata.

- **Payment APIs**: `POST /payments/charge` (charge rider after trip completion), `GET /payments/methods` (list rider's payment methods: credit card, debit, PayPal, Uber Cash), `POST /payments/methods` (add payment method), `POST /payments/split` (split fare between riders). Payments are processed **asynchronously** after trip completion — the rider doesn't wait for payment processing to exit the car. Uber uses a ledger-based system: each trip creates debit (rider) and credit (driver) entries. Driver payouts happen on a weekly cycle. `POST /payments/tip` (post-trip tip for driver).

- **Rating & Feedback APIs**: `POST /rides/{rideId}/rate` (rate driver 1-5 stars + optional comment, rate rider 1-5 stars from driver side). Ratings are **bidirectional** — both riders and drivers rate each other. Driver ratings below a threshold (varies by market, ~4.6) lead to deactivation warnings. Rider ratings affect driver acceptance behavior (drivers can see rider rating before accepting). `POST /rides/{rideId}/report` (report safety concern — triggers safety team review).

- **Driver Management APIs**: `GET /drivers/earnings` (daily/weekly earnings summary), `GET /drivers/heat-map` (demand heat map — shows high-demand areas to guide driver positioning), `PUT /drivers/preferences` (set vehicle type, ride type preferences, working hours), `GET /drivers/incentives` (current bonuses: surge zones, quest bonuses, consecutive trip bonuses). The heat map and incentives are Uber's tools for **supply management** — guiding drivers to high-demand areas to reduce rider wait times.

- **Safety APIs**: `POST /safety/share-trip` (share live trip tracking link with emergency contacts), `POST /safety/emergency` (trigger emergency response — shares location with 911, records audio), `GET /safety/driver-verification` (driver identity verification: photo match, background check status). Uber's safety features include: real-time trip sharing, in-app emergency button, driver photo verification, GPS trip recording, speed alerts.

- **Maps & Geocoding APIs**: `GET /geocode?address={address}` (address to lat/lng), `GET /reverse-geocode?lat={lat}&lng={lng}` (lat/lng to address), `GET /places/autocomplete?q={query}` (address autocomplete as user types — <100ms typeahead). Uber uses a combination of third-party map providers (Google Maps, Mapbox) and proprietary map data (from driver GPS traces) for routing and ETA.

**Contrast with traditional taxi dispatch**: Traditional taxi dispatch is centralized (one dispatcher, radio communication, manual assignment). Uber's dispatch is fully automated, distributed, and real-time. Traditional taxis have flat or metered rates; Uber has dynamic surge pricing. Traditional taxis have no bidirectional ratings. Traditional dispatch doesn't need geospatial indexing — the dispatcher knows the cab fleet by memory.

**Contrast with food delivery (DoorDash/Uber Eats)**: Food delivery has a THREE-party matching problem (customer ↔ restaurant ↔ driver), not two-party (rider ↔ driver). Food delivery has prep time variability (restaurant cooking speed is unpredictable). Food delivery routing is pickup→restaurant→dropoff; ride-sharing is pickup→dropoff. Food delivery batching (assigning multiple deliveries to one driver) is more aggressive than ride pooling.

**Interview subset**: In the interview (Phase 3), focus on: ride request + dispatch (the matching problem), driver location updates (real-time geospatial indexing), ETA computation (routing + traffic), and surge pricing (dynamic supply/demand balancing). The full API list lives in this doc.

### 3. 03-geospatial-indexing.md — Location Tracking & Geospatial Index

The geospatial index is the foundation of everything — matching, ETA, surge pricing, and heat maps all depend on knowing where drivers are right now.

- **The firehose**: Every active driver sends a GPS update every 3-4 seconds. At peak, Uber has ~5 million active drivers globally. That's ~1.5 million location updates per second, or ~130 billion updates per day. Each update is ~100 bytes (driverId, lat, lng, heading, speed, timestamp). That's ~150 MB/sec of raw location data. The system must ingest this, update the spatial index, and serve queries — all in real-time.
- **Geospatial indexing options**:
  - **Quadtree**: Recursive spatial subdivision. Each node represents a rectangular area. When a cell has too many points, it splits into 4 children. Dynamically adapts to density — dense areas (Manhattan) have deeper trees than rural areas. Read: O(log N) for point queries, range queries traverse relevant subtrees. Update: O(log N) — find old cell, remove, find new cell, insert. Problem: tree rebalancing under high update rates.
  - **Geohash**: Encode lat/lng into a string prefix (e.g., "9q8yyk"). Nearby locations share prefixes. Range queries become prefix scans on a sorted index (Redis, Cassandra). Simple to implement. Problem: geohash cells are rectangles aligned to the coordinate grid — edge effects at cell boundaries cause uneven coverage near the poles and at the antimeridian.
  - **H3 (Uber's hexagonal grid)**: Uber developed **H3**, a hierarchical hexagonal grid system. The earth is divided into hexagonal cells at multiple resolutions (from continental to ~1m²). Hexagons have uniform adjacency (each hex has 6 equidistant neighbors, unlike squares which have 4 close + 4 diagonal neighbors). This eliminates the "corner problem" in square grids. H3 is open-source and used for surge pricing, driver positioning, demand forecasting. [VERIFIED — Uber Engineering blog, open-source on GitHub]
  - **S2 Geometry (Google)**: Hierarchical spatial index based on a Hilbert curve projection of the sphere. Used by Google Maps, S2 maps 2D sphere coordinates to 1D cell IDs with good locality preservation. S2 uses square cells on a cube projection.
  - **R-tree / R*-tree**: Balanced tree of minimum bounding rectangles. Good for static spatial data (points of interest). Less suitable for highly dynamic data (millions of moving points updated per second) because insertions cause tree rebalancing.
  - **PostGIS / spatial databases**: PostgreSQL with PostGIS extension supports spatial queries natively (ST_DWithin, ST_Distance). Good at moderate scale but not designed for millions of updates per second.
- **Uber's approach (VERIFIED — from Uber Engineering blog posts):**
  - Uber built a custom in-memory geospatial index using **Google S2** cells (originally) and later **H3** hexagonal cells for many use cases.
  - The driver location service ingests GPS updates via Kafka, updates the in-memory spatial index, and serves nearest-driver queries.
  - The spatial index is **sharded by geographic region** — each city/region has its own index shard. This provides natural load balancing (Manhattan's shard handles Manhattan's drivers; rural Iowa's shard is light).
  - For nearest-driver queries: query the cell containing the pickup location + all adjacent cells. Filter by: driver status (available only), vehicle type, distance/ETA.
- **Accuracy vs freshness trade-off**: GPS updates arrive every 3-4 seconds. Between updates, a driver at 30 mph moves ~40-55 meters. The index is always slightly stale. For matching purposes, this is acceptable — we're finding NEARBY drivers, not pinpointing exact positions. For ETA, we use the last known position + current speed/heading for interpolation.
- **Contrast with Google Maps**: Google Maps indexes static points of interest (restaurants, gas stations) and road networks. These change slowly (daily/weekly updates). Uber indexes millions of MOVING points (drivers) that change position every 3-4 seconds. Google's challenge is scale of static data; Uber's challenge is velocity of dynamic data.
- **Contrast with food delivery**: DoorDash/Uber Eats also tracks delivery drivers but at lower volume (~1M active couriers vs ~5M Uber drivers). The spatial index is the same technology but at different scale. Food delivery also needs to index restaurants (static) — a hybrid static+dynamic spatial index.

### 4. 04-dispatch-and-matching.md — Real-Time Dispatch & Matching

The dispatch system is the brain of Uber — it matches ride requests with available drivers in real-time. This is the most latency-sensitive and business-critical component.

- **The matching problem**: Given a ride request at location (lat, lng), find the BEST available driver. "Best" is defined by:
  - **ETA to pickup**: Shortest time to reach the rider (route-based, not straight-line distance — a driver 500m away across a river may have a 15-minute ETA)
  - **Driver direction/heading**: A driver moving toward the pickup is better than one moving away (less U-turn time)
  - **Driver rating**: Higher-rated drivers preferred (marginal factor)
  - **Vehicle type match**: UberX, XL, Black, Pool each require matching vehicle types
  - **Supply/demand balance**: In Pool/shared rides, match with existing trips going in a similar direction
- **Dispatch flow**:
  ```
  Rider requests a ride
      │
      ├── Ride Service creates ride request (status: MATCHING)
      │
      ├── Dispatch Service queries Geospatial Index:
      │   "Find available drivers within 5km of pickup, type=UberX"
      │   → Returns N candidate drivers (~5-50 depending on density)
      │
      ├── For each candidate, compute ETA to pickup:
      │   Query Routing Service with (driver_location → pickup_location)
      │   → Returns time estimate (not just distance — uses road network + traffic)
      │
      ├── Rank candidates by: ETA × rating_weight × heading_bonus
      │   → Select best driver
      │
      ├── Send ride offer to best driver (push notification or in-app):
      │   Driver has ~15 seconds to accept
      │   │
      │   ├── Driver accepts → status: DRIVER_EN_ROUTE
      │   │   → Notify rider (show driver location + ETA)
      │   │
      │   └── Driver declines or timeout → cascade to next best driver
      │       → Repeat until matched or no candidates (SURGE or NO_DRIVERS_AVAILABLE)
      │
      └── If no match after exhausting candidates:
          → Expand search radius and retry
          → If still no match: notify rider "No drivers available"
  ```
- **Uber Pool / Lyft Shared (shared rides)**:
  - Two-party matching becomes a **routing optimization problem**: match a new rider with an in-progress trip going in a similar direction, such that the detour for existing passengers is minimized.
  - This is a variant of the **Vehicle Routing Problem (VRP)**, which is NP-hard. Uber uses heuristics: candidate trips within a detour threshold (e.g., <5 min extra for existing riders), pickup within a walkable distance of a main road, dropoffs roughly in the same direction.
  - Shared ride matching must balance: detour for existing riders (fairness), total trip time for new rider, driver utilization (more riders = more revenue per mile), and pickup ETA for new rider.
- **Batched matching vs greedy matching**:
  - **Greedy**: Match each ride request immediately with the best available driver. Simple, low latency. Problem: locally optimal but globally suboptimal — matching driver D1 with rider R1 might leave nearby rider R2 with a much worse match.
  - **Batched**: Accumulate ride requests over a short window (e.g., 2-5 seconds), then solve a global optimization — find the assignment of riders to drivers that minimizes total ETA across all pairs (a variant of the assignment problem). Better matches overall but adds latency (the batch window).
  - **Uber's approach**: Uber uses **batched matching** in high-demand areas. The batch window is short (~2 seconds). The optimization is solved using a variant of the Hungarian algorithm or auction-based assignment. In low-demand areas, greedy matching is used (not enough concurrent requests to batch).
  - [PARTIALLY VERIFIED — Uber Engineering blog discusses batched dispatch but exact algorithm details are proprietary]
- **Dispatch reliability**: The dispatch system must be highly available — if it goes down, no rides can be matched. Design for:
  - **Idempotent ride requests**: If the dispatch call fails, the rider can retry without creating a duplicate ride
  - **At-least-once delivery**: Ride offers to drivers must be delivered at least once (push notification + in-app polling as fallback)
  - **Timeout and cascade**: If a driver doesn't respond in 15 seconds, automatically cascade to the next driver
  - **State machine**: Ride status transitions are modeled as a state machine with well-defined transitions and guard conditions (e.g., can't transition from COMPLETED back to IN_PROGRESS)
- **Contrast with traditional taxi dispatch**: Taxi dispatchers manually assign cabs based on radio communication and mental model of fleet positions. No GPS, no optimization, no surge pricing. Uber's dispatch is fully automated and runs at million-request-per-day scale. The quality of matching (ETA accuracy, driver-rider proximity) is dramatically better.
- **Contrast with food delivery dispatch**: Food delivery dispatch has a THREE-party problem (customer ↔ restaurant ↔ courier). The dispatch must consider restaurant prep time (variable, unpredictable) in addition to courier ETA. Food delivery also batches multiple orders to one courier more aggressively (a courier picks up from 2-3 restaurants in one trip). Ride-sharing rarely has more than 1 pickup in UberX (Pool has 2-3).

### 5. 05-eta-and-routing.md — ETA Computation & Routing

ETA is the most frequently computed value in the Uber system — displayed to riders, used for matching, used for fare estimation, and displayed on the driver's navigation screen.

- **ETA computation pipeline**:
  1. **Road network graph**: The world's road network as a weighted directed graph. Nodes = intersections, edges = road segments. Edge weights = travel time (not distance). One-way streets are directed edges. Turn restrictions are modeled as edge adjacency constraints.
  2. **Graph source**: OpenStreetMap (open, global) + proprietary corrections from Uber's own driver GPS traces (millions of traces per day improve accuracy — detect new roads, road closures, actual turn times).
  3. **Shortest-path algorithm**: Dijkstra's or A* on the full graph is too slow for real-time queries (~seconds for long routes). Solution: **Contraction Hierarchies (CH)** — a preprocessing technique that adds "shortcut edges" to the graph, reducing query time from seconds to **<10 milliseconds**. CH preprocesses the graph once (hours of compute), then queries are orders of magnitude faster.
  4. **Traffic-aware ETA**: Static edge weights (speed limits) are insufficient. Real-time traffic from driver GPS traces adjusts edge weights dynamically. Historical traffic patterns (rush hour on this road segment is always slow) provide baseline predictions. ML models combine real-time + historical for the most accurate ETA.
  5. **ETA accuracy**: Uber targets <20% error on ETA predictions. ETA accuracy is A/B tested and continuously improved. Inaccurate ETAs lead to: rider frustration (waited longer than expected), incorrect fare estimates (upfront pricing relies on ETA), suboptimal driver matching (dispatch chose a farther driver because ETA was wrong).
- **Routing service architecture**:
  ```
  Client requests route (origin → destination)
      │
      ├── Geocode origin/destination if needed (address → lat/lng)
      │
      ├── Query Contraction Hierarchies index:
      │   Find shortest-time path on road graph
      │   Apply real-time traffic adjustments to edge weights
      │   Apply turn penalties (left turns across traffic are slower)
      │   → Returns: route geometry (polyline), estimated duration, distance
      │
      ├── Apply ML correction:
      │   Historical patterns for this route + time-of-day + day-of-week
      │   → Adjust ETA by predicted correction factor
      │
      └── Return: route polyline, ETA, distance, turn-by-turn directions
  ```
- **Map matching**: Driver GPS traces are noisy (GPS accuracy ±5-10 meters). Raw GPS coordinates don't align to roads. **Map matching** snaps GPS points to the most likely road segment using a Hidden Markov Model (HMM) or Viterbi algorithm. Uber's map matching processes millions of traces per day to: (1) display the driver's position on the correct road, (2) compute accurate trip distance for fare calculation, (3) generate traffic data from aggregate traces.
- **Live ETA updates during trip**: Once a trip starts, ETA to destination updates every few seconds as the driver progresses. The routing engine re-queries the remaining route with updated traffic. Dynamic rerouting: if traffic conditions change, suggest an alternative route to the driver.
- **Contrast with Google Maps**: Google Maps computes ETAs for the general public. Uber computes ETAs specifically for driver-to-rider and rider-to-destination scenarios. Key differences: Uber uses driver GPS traces as a proprietary traffic signal (millions of traces per day in each city); Google uses crowdsourced data from Android phones. Uber needs sub-second ETA responses at millions of QPS for dispatch decisions; Google Maps can tolerate slightly higher latency for consumer queries. Uber's routing must handle ride-specific constraints (no U-turns in heavy traffic, pickup on the correct side of the road).
- **Contrast with food delivery**: Food delivery ETA has two components: restaurant prep time (hard to predict — depends on order complexity, kitchen load) + courier travel time (similar to ride-sharing ETA). The courier travel ETA uses the same routing engine, but the total ETA is dominated by the unpredictable prep time component. Ride-sharing ETA is almost entirely travel time.

### 6. 06-surge-pricing.md — Dynamic Pricing & Supply/Demand Balancing

Surge pricing is Uber's most controversial and most architecturally interesting feature — a real-time marketplace that adjusts prices based on supply and demand.

- **Why surge pricing exists**: In a geographic area, if there are more ride requests than available drivers, without price adjustment: riders wait longer and longer, some riders never get matched, drivers have no incentive to reposition to high-demand areas. Surge pricing solves this by: (1) reducing marginal demand (price-sensitive riders wait or take alternatives), (2) increasing supply (drivers see higher prices and drive to the surge zone), (3) clearing the market (supply meets demand at the surge price).
- **Surge computation**:
  - Divide the city into **geospatial cells** (hexagons using H3 or squares). Each cell is independently evaluated.
  - For each cell, compute: `supply = count of available drivers in cell`, `demand = count of ride requests in cell in last N minutes` (or predicted demand from ML model).
  - `surge_multiplier = f(demand / supply)` — when demand/supply > threshold → surge activates. The function is typically a stepped or smoothed curve: 1.0x (normal), 1.2x, 1.5x, 2.0x, 3.0x+.
  - Surge is recomputed every **1-2 minutes** to reflect changing conditions.
  - Surge multiplier is applied to the base fare at time of ride request and locked in for the duration of the trip.
- **Surge architecture**:
  ```
  ┌──────────────────────────────────────────────────────────┐
  │ Surge Pricing Pipeline                                    │
  │                                                           │
  │ Input streams:                                            │
  │   • Driver locations (from Geospatial Index)              │
  │   • Ride requests (from Ride Service)                     │
  │   • Historical demand patterns (from data warehouse)      │
  │   • External events (concerts, sports, weather)           │
  │                                                           │
  │ Processing:                                               │
  │   1. Aggregate supply per H3 cell (resolution ~9, ~175m)  │
  │   2. Aggregate demand per H3 cell (requests + predictions)│
  │   3. Compute supply/demand ratio per cell                 │
  │   4. Apply surge function → multiplier per cell           │
  │   5. Smooth multipliers across adjacent cells             │
  │      (avoid sharp boundaries — rider walks 100m to        │
  │       avoid surge)                                        │
  │   6. Publish surge map to: Rider app (show surge zones),  │
  │      Driver app (heat map), Pricing Service (apply to     │
  │      fare estimates)                                      │
  │                                                           │
  │ Frequency: recomputed every 1-2 minutes                   │
  │ Latency: <30 seconds from data change to updated surge    │
  └──────────────────────────────────────────────────────────┘
  ```
- **Upfront pricing (2016)**: Uber moved from metered fares (pay actual time+distance after trip) to **upfront pricing** (quoted price before trip). The upfront price = `base_fare + per_mile × estimated_distance + per_minute × estimated_duration + surge × multiplier + booking_fee - promotions`. If actual trip takes longer (traffic, detour), Uber absorbs the difference. If actual trip is shorter, rider still pays the upfront price (Uber keeps the margin). This shifts risk from rider to platform but requires accurate ETA and route prediction.
- **ML-based pricing (2017+)**: Uber evolved from simple supply/demand-based surge to ML models that predict **willingness to pay** (WTP) and **market-clearing price**. Factors: route characteristics, time of day, rider history, device type (controversy: iPhone users charged more?), and predicted trip quality. [PARTIALLY VERIFIED — Uber has discussed ML-based pricing publicly but exact model features are proprietary]
- **Contrast with traditional taxis**: Taxis have regulated flat or metered rates. No dynamic pricing. This means during high-demand periods (New Year's Eve, rainstorms), taxis are underpriced → excess demand → riders can't find cabs. Surge pricing is economically efficient but socially controversial.
- **Contrast with airline pricing**: Airlines use dynamic pricing (revenue management) that adjusts prices based on demand, time to departure, seat availability, and customer segmentation. Similar concept to Uber's surge but at a much slower timescale (hours/days vs minutes). Airlines have a fixed supply (seats on a plane); Uber's supply is elastic (drivers can choose to go online or reposition).

### 7. 07-trip-lifecycle-and-payments.md — Trip State Machine & Payment Processing

The trip lifecycle is the backbone of the rider and driver experience — every interaction maps to a state transition.

- **Trip state machine**:
  ```
  IDLE ──(rider requests ride)──> MATCHING
  MATCHING ──(driver assigned)──> DRIVER_EN_ROUTE
  MATCHING ──(no drivers / timeout)──> CANCELLED
  DRIVER_EN_ROUTE ──(driver arrives at pickup)──> ARRIVED
  DRIVER_EN_ROUTE ──(rider cancels)──> CANCELLED (may have fee)
  DRIVER_EN_ROUTE ──(driver cancels)──> MATCHING (re-dispatch to new driver)
  ARRIVED ──(rider boards, driver starts trip)──> IN_PROGRESS
  ARRIVED ──(rider no-show after 5 min)──> CANCELLED (no-show fee charged)
  IN_PROGRESS ──(driver ends trip at dropoff)──> COMPLETED
  IN_PROGRESS ──(safety incident)──> EMERGENCY
  COMPLETED ──(payment processed)──> FINALIZED
  COMPLETED ──(dispute filed)──> DISPUTED
  ```
- **Event sourcing**: Each state transition is recorded as an immutable event with timestamp, GPS coordinates, and metadata. The trip's current state is derived by replaying events. This provides: full audit trail (important for disputes, safety reviews, insurance claims), replay capability (recompute fares from events), and temporal queries (where was the driver at 3:47 PM?).
- **Payment processing**:
  - **Asynchronous**: Rider exits car → trip marked COMPLETED → payment processed asynchronously. Rider doesn't wait for payment to complete.
  - **Payment flow**: Trip completed → calculate fare (distance × per-mile + duration × per-minute + surge + tolls + booking fee - promotions) → charge rider's payment method (Stripe/Braintree) → debit rider ledger → credit driver ledger (fare minus platform commission, typically 20-25%) → credit ledger entries for tips, tolls, bonuses.
  - **Ledger-based accounting**: Every financial transaction is a double-entry in a ledger. Rider debit = Driver credit + Platform commission. This ensures the books always balance. The ledger is the source of truth for all financial state.
  - **Driver payouts**: Drivers accumulate earnings during the week. Payouts happen on a fixed cycle (weekly) or on-demand (Uber's Instant Pay feature — transfer earnings to debit card instantly for a small fee). Payout uses ACH (slow, free) or debit push (instant, small fee).
  - **Fraud detection**: Payment fraud (stolen credit cards, chargebacks) is a significant cost. ML models detect anomalies: unusual trip patterns, high-value trips from new accounts, multiple payment methods from the same device, trips to/from airports with new accounts.
- **Fare disputes**: Riders can dispute fares (e.g., driver took a longer route, GPS error inflated distance). The dispute system compares: actual GPS trace vs expected route (from routing engine). If the driver deviated significantly from the optimal route, the fare is adjusted to the expected fare.
- **Contrast with traditional taxi meters**: Taxi meters calculate fare in real-time based on distance (wheel rotation sensor) + time (clock). Uber calculates fare from GPS traces + map-matched route. GPS-based fare calculation is more accurate for distance but introduces GPS noise. Taxi meters are tamper-resistant hardware; Uber's fare is computed server-side from GPS data.
- **Contrast with food delivery payments**: Food delivery has three parties to pay: restaurant (food cost), courier (delivery fee + tip), platform (commission). Ride-sharing has two parties: driver (fare - commission + tip), platform (commission). Food delivery payments are more complex because the platform intermediates between customer, restaurant, and courier — with different payment terms for each.

### 8. 08-real-time-communication.md — Real-Time Features & Push Infrastructure

Real-time communication is essential — riders must see driver location in real-time, drivers must receive ride offers instantly, and both must be notified of trip events.

- **Real-time features**:
  - **Live driver tracking**: Rider sees driver's location moving on the map in real-time (during DRIVER_EN_ROUTE, ARRIVED, IN_PROGRESS). Updates every 1-2 seconds. Uses WebSocket or Server-Sent Events (SSE) for push.
  - **Ride offer delivery**: When the dispatch system selects a driver, the ride offer must reach the driver within ~1 second. Uses push notification (APNs/FCM) + in-app WebSocket. Driver has ~15 seconds to accept.
  - **Trip event notifications**: Status changes (driver arrived, trip started, trip completed) pushed to rider and driver in real-time.
  - **ETA updates**: Rider sees updated ETA to pickup (refreshed every few seconds as driver approaches).
  - **Chat / calling**: In-app chat and masked phone calls between rider and driver (Twilio-based number masking — neither party sees the other's real phone number).
- **Push infrastructure**:
  - **WebSocket**: Persistent bidirectional connection between app and server. Used when the app is in foreground. Low latency (~100ms delivery). Connection managed by a WebSocket gateway service that maps userId → connection. Gateway is horizontally scaled, stateful (each connection lives on a specific gateway instance).
  - **Push notifications (APNs / FCM)**: Used when the app is in background or killed. Higher latency (~1-5 seconds). Less reliable (platform-dependent delivery guarantees). Used as fallback for critical events (ride offer, trip completion, payment receipt).
  - **Hybrid delivery**: For ride offers (most time-critical): send via WebSocket (if connected) AND push notification (as backup). The app deduplicates — processes whichever arrives first.
- **WebSocket gateway scaling**:
  ```
  ┌────────────┐     ┌────────────────────────┐
  │  Mobile    │────>│  Load Balancer          │
  │  App       │<────│  (sticky sessions       │
  │            │     │   for WebSocket)        │
  └────────────┘     └──────────┬─────────────┘
                                │
                     ┌──────────▼─────────────┐
                     │  WebSocket Gateway      │
                     │  (stateful — each       │
                     │   connection pinned to  │
                     │   a specific instance)  │
                     │                         │
                     │  userId → connection    │
                     │  mapping stored in Redis│
                     │  (for routing messages  │
                     │   to the right gateway) │
                     └──────────┬─────────────┘
                                │
                     ┌──────────▼─────────────┐
                     │  Backend Services       │
                     │  publish events to      │
                     │  message bus (Kafka)     │
                     │                         │
                     │  Gateway instances       │
                     │  subscribe to relevant   │
                     │  topics and push to      │
                     │  connected clients       │
                     └─────────────────────────┘
  ```
  - **Connection routing**: When the dispatch system needs to send a ride offer to driver D1, it looks up D1's gateway instance in Redis → sends the message to that gateway → gateway pushes to D1's WebSocket connection.
  - **Connection migration**: If a gateway instance fails, all connections drop. Clients reconnect to a new gateway instance. Client state (which ride they're tracking) is stored server-side — reconnection resumes tracking seamlessly.
- **Contrast with Instagram/Meta (MQTT)**: Instagram uses MQTT for mobile real-time (battery-efficient, 2-byte header). Uber uses WebSocket primarily because: (1) Uber's real-time data is heavier (GPS coordinates, route polylines, map data) — MQTT's tiny header advantage is marginal, (2) Uber's updates are more frequent during active trips (every 1-2 seconds vs Instagram's sporadic notifications), (3) The Uber app is typically in the foreground during active trips — battery optimization is less critical.
- **Contrast with WhatsApp messaging**: WhatsApp guarantees message delivery (store-and-forward, ack/retry). Uber's real-time tracking is best-effort — a missed location update is replaced by the next one 1 second later. The delivery guarantee requirements are fundamentally different: a missed WhatsApp message is lost information; a missed Uber location update is superseded by the next one.

### 9. 09-data-storage-and-analytics.md — Data Storage, Analytics & ML

Uber generates massive amounts of data — every GPS update, every trip event, every fare calculation, every search query. This data powers operations, analytics, pricing, and ML models.

- **Storage systems**:
  - **MySQL / Schemaless (Uber's custom sharded MySQL layer)**: Uber's primary transactional store for trips, users, payments. Uber built **Schemaless** — a sharded MySQL layer that provides: automatic sharding, write buffering, change data capture, and eventually-consistent secondary indexes. Schemaless was later replaced/complemented by **Docstore** (document store) in some use cases. [VERIFIED — Uber Engineering blog, "Designing Schemaless, Uber Engineering's Scalable Datastore Using MySQL"]
  - **Cassandra**: Used for high-write-throughput data: driver location history, trip event logs, activity feeds. Cassandra's strengths (linear write scaling, tunable consistency, multi-DC replication) match these access patterns.
  - **Redis**: Real-time data — geospatial index (driver locations), dispatch state, rate limiting, session data. Sub-millisecond read latency.
  - **Amazon S3 / HDFS**: Raw data lake for analytics. All GPS traces, trip events, pricing decisions, and engagement data land here for batch processing.
  - **Apache Kafka**: The backbone for real-time data pipelines. Driver location updates, trip events, pricing updates, and analytics events flow through Kafka. Uber runs one of the largest Kafka deployments in the world — **trillions of messages per day** across thousands of topics. [VERIFIED — Uber Engineering blog, "Uber's Real-Time Push Platform"]
  - **Elasticsearch**: Used for search (driver search, trip search by support agents), log aggregation, and monitoring.
  - **Apache Hive / Presto / Spark**: Batch analytics on the data lake. Powers: business intelligence dashboards, driver earnings reports, market analytics, regulatory reporting.
- **ML at Uber**:
  - **Michelangelo**: Uber's ML platform for training, deploying, and monitoring ML models at scale. Supports: feature engineering, model training (TensorFlow, XGBoost, PyTorch), model serving, A/B testing, and monitoring. [VERIFIED — Uber Engineering blog, "Meet Michelangelo: Uber's Machine Learning Platform"]
  - **ETA prediction**: ML model that predicts trip ETA from: road network features, real-time traffic, historical patterns, weather, and time-of-day. Trained on billions of historical trips.
  - **Surge prediction**: ML model that predicts demand surges 15-30 minutes ahead, allowing proactive supply positioning.
  - **Fraud detection**: ML models detect fraudulent trips (GPS spoofing, fake accounts, driver collusion), payment fraud (stolen cards, promo abuse), and safety incidents.
  - **Driver destination prediction**: Predict where a driver is likely heading (even without a trip) to improve matching — if a driver is heading toward a high-demand area, don't match them with a ride going the opposite direction.
- **Data pipeline architecture**:
  ```
  GPS Updates / Trip Events / Pricing Events
      │
      ▼
  Kafka (real-time stream)
      │
      ├── Real-time consumers:
      │   • Geospatial Index (driver locations)
      │   • Surge Pricing Pipeline
      │   • Real-time dashboards (Grafana/Atlas)
      │   • Anomaly detection (fraud, safety)
      │
      └── Batch pipeline:
          • Kafka → S3/HDFS (data lake)
          • Hive/Presto for analytics queries
          • Spark for ML training data preparation
          • Michelangelo for model training
  ```
- **Contrast with Netflix analytics**: Netflix processes 140M hours of viewing data per day to train recommendation models. Uber processes billions of GPS points per day to train ETA, pricing, and fraud models. Both are data-heavy ML-driven companies, but the data shape differs: Netflix has user×item interaction matrices (sparse, high-dimensional); Uber has geospatial time-series data (dense, low-dimensional but high-volume).
- **Contrast with Google Maps data pipeline**: Google Maps processes location data from billions of Android phones (anonymized) for traffic estimation. Uber processes location data from millions of drivers (identified) for the same purpose but at higher frequency (GPS every 3-4 seconds vs Android location every ~minute). Google's scale is broader (every Android phone); Uber's data is denser per geographic area (concentrated on roads, higher update frequency).

### 10. 10-scaling-and-reliability.md — Scaling, Performance & Reliability

Cross-cutting doc that ties together scaling decisions from all deep dives.

- **Scale numbers**:
  - **~150 million monthly active riders** (estimate, not officially disclosed for recent years)
  - **~5 million active drivers** globally
  - **~30+ million trips per day** (pre-COVID peak was higher)
  - **~1.5 million GPS updates per second** at peak (5M drivers × 1 update/3-4 sec)
  - **~130 billion GPS updates per day**
  - **Trillions of Kafka messages per day** [VERIFIED — Uber Engineering blog]
  - **Operating in 10,000+ cities across 70+ countries**
  - **Sub-3-minute average pickup time** in major metros
  - **Peak events**: New Year's Eve, major concerts, sports events — demand spikes 5-10x in concentrated areas
- **Latency budgets**:
  - **Ride request to ride offer**: <5 seconds (request → dispatch → find driver → send offer)
  - **ETA query**: <100ms (routing service query with Contraction Hierarchies)
  - **Location update ingestion**: <1 second (GPS update → Kafka → spatial index updated)
  - **Surge price refresh**: <2 minutes (data change → recomputed surge map)
  - **Payment processing**: <30 seconds after trip completion (async, non-blocking)
  - **Driver location on rider's map**: <2 seconds (driver GPS → server → WebSocket → rider app)
- **Reliability**:
  - **Dispatch availability**: 99.99%+ — if dispatch is down, no rides happen
  - **Location service availability**: 99.99%+ — if location service is down, matching quality degrades drastically
  - **Payment service**: 99.9%+ — payment can be retried; brief delays are tolerable
  - **Graceful degradation**: If the routing service is slow, fall back to straight-line distance estimates (less accurate but functional). If surge computation is delayed, use the last known surge values. If the recommendation engine (ride type suggestions) is down, show all ride types without personalization.
- **Multi-region architecture**: Uber operates globally with data centers in multiple regions. Each region handles its geographic area's traffic. Cross-region data replication for: user accounts (a user traveling from NYC to London should have their account accessible), payment state, driver profiles. Trip data is primarily regional — a trip in NYC doesn't need to be replicated to the Singapore DC in real-time.
- **Peak event handling**: Major events (New Year's Eve, Super Bowl, concert ending) cause extreme demand spikes in concentrated areas. Pre-scaling: Uber pre-provisions additional compute capacity for known events. Surge pricing activates to balance supply and demand. The geospatial index for the event area may need to be split into smaller cells to handle the density.
- **Contrast with Netflix reliability**: Netflix's failure mode is "video won't play" — degraded experience. Uber's failure mode is "can't get a ride" — potentially stranded. Uber's availability requirements are arguably higher because the service has real-world safety implications (stranded in an unfamiliar area, late to a flight). Netflix can show cached recommendations if the recommendation service is down; Uber can't dispatch a ride with stale driver locations.
- **Contrast with Google Maps reliability**: Google Maps is a navigation tool — if it's briefly unavailable, users use an alternative or wait. Uber is a transportation service — if dispatch is down, users are physically stranded. The blast radius of a failure is fundamentally different: Google Maps outage = inconvenience; Uber dispatch outage = inability to travel.

### 11. 11-design-trade-offs.md — Design Philosophy & Trade-off Analysis

Opinionated analysis of Uber's design choices — not just "what" but "why this and not that."

- **Geohash vs Quadtree vs H3 for spatial indexing**: Uber developed H3 (hexagonal grid) because: hexagons have uniform adjacency (6 equidistant neighbors vs squares' 4 close + 4 diagonal), no "corner problem" for range queries, hierarchical (multiple resolutions), and better coverage of the sphere (less distortion at poles). Trade-off: H3 is less widely adopted than geohash, requires a custom library, hexagons don't tile perfectly at all resolutions. For most companies at smaller scale, geohash + Redis is simpler and sufficient.
- **Batched dispatch vs greedy dispatch**: Batched dispatch (accumulate requests for 2-5 seconds, solve global optimization) produces better matches but adds latency. Greedy dispatch (match immediately) is simpler and faster. Uber uses batched in dense markets and greedy in sparse markets. Trade-off: batch window length — too long adds rider wait time, too short doesn't accumulate enough requests for meaningful optimization.
- **Upfront pricing vs metered pricing**: Upfront pricing (quote a price before the trip) gives riders price certainty but shifts risk to the platform (if actual trip costs more, platform absorbs the loss). Metered pricing is simpler but riders face uncertainty. Uber moved to upfront pricing because: rider satisfaction is higher with price certainty, it enables comparison shopping (rider sees the price before committing), and it allows ML-based pricing optimization.
- **Surge pricing: simple multiplier vs ML-based pricing**: Simple surge (demand/supply ratio → multiplier) is transparent and explainable. ML-based pricing can optimize for market clearing more efficiently but is a black box — raises fairness concerns (are some riders charged more than others for the same route?). Uber has evolved from simple surge to more sophisticated ML pricing, balancing efficiency with transparency and regulatory scrutiny.
- **Event sourcing for trips vs CRUD**: Uber uses event sourcing for the trip lifecycle — every state transition is an immutable event. CRUD would be simpler (just update the trip row). Event sourcing provides: full audit trail (critical for disputes, insurance, regulatory compliance), replay capability (recompute fares), and temporal queries. Trade-off: event sourcing adds storage overhead and requires a projection layer to derive current state.
- **WebSocket vs MQTT vs SSE for real-time**: Uber uses WebSocket for real-time driver tracking. MQTT would be more battery-efficient (Meta's choice for Instagram). SSE would be simpler (server → client only). Uber chose WebSocket because: bidirectional communication needed (driver sends location, server sends ride offers), higher data volume per connection (GPS coordinates every 1-2 seconds), and the app is in foreground during active trips (battery optimization less critical). Trade-off: WebSocket connections are stateful → more complex scaling (sticky sessions, connection state management).
- **Kafka as the backbone vs direct service-to-service calls**: Uber routes most data through Kafka (location updates, trip events, pricing events). Alternative: direct gRPC calls between services. Kafka provides: decoupling (producers and consumers evolve independently), replay (reprocess events after a bug fix), multiple consumers (location updates consumed by geospatial index, analytics, surge pricing, ETA service). Trade-off: Kafka adds latency (milliseconds) and operational complexity. For latency-critical paths (dispatch), Uber uses direct calls alongside Kafka (write to Kafka for durability, direct call for speed).
- **Build vs buy for maps**: Uber initially relied on Google Maps for routing and ETA. Over time, Uber built proprietary mapping capabilities (from driver GPS traces) to reduce dependency and cost, and to build features Google Maps doesn't optimize for (driver-side-of-road pickup, ride-specific ETAs). Trade-off: building a mapping stack is enormously expensive (hundreds of engineers), but at Uber's scale, the per-query cost savings and feature customization justify it.
- **MySQL (Schemaless) vs NoSQL**: Uber built Schemaless (a sharding layer on top of MySQL) rather than adopting a NoSQL database (Cassandra, DynamoDB). Reasoning: MySQL is well-understood, mature, with excellent tooling. Schemaless adds the horizontal scaling Uber needed while keeping MySQL's operational familiarity. Trade-off: Schemaless is a custom solution requiring in-house expertise. Most companies should use a managed NoSQL solution instead.

## CRITICAL: The design must be Uber-centric
Uber is the reference implementation. The design should reflect how Uber actually works — its dispatch system, geospatial indexing (H3/S2), ETA computation (Contraction Hierarchies), surge pricing, trip lifecycle (event sourcing), real-time infrastructure (WebSocket), Kafka backbone, and ML platform (Michelangelo). Where Lyft, traditional taxis, or food delivery platforms made different design choices, call those out explicitly as contrasts.

## CRITICAL: Iterative build-up (DO NOT skip this)
The architecture MUST evolve iteratively. Each attempt builds on the previous one by identifying a concrete problem and solving it. The candidate should NOT jump to the final architecture. The progression should feel natural:

### Attempt 0: Single server with a database
- Web server + SQL database. Rider calls in (or opens a basic web form) → request stored in DB → dispatcher (human or script) looks at DB for available drivers → assigns one. Driver location updated via periodic API call (every 30 seconds).
- **Problems found**: No real-time location tracking (30-second updates are too stale for matching), manual dispatch doesn't scale, no spatial indexing (finding nearby drivers requires scanning all drivers), no dynamic pricing, single point of failure, no real-time rider experience (no live tracking).

### Attempt 1: Real-time location + basic geospatial index
- **Driver GPS every 3-4 seconds** (via mobile SDK). Location updates stored in an in-memory spatial index (geohash in Redis or quadtree).
- **Nearest-driver query**: Given pickup location, find available drivers within radius. Uses spatial index for sub-second queries.
- **Basic dispatch**: Greedy — find nearest available driver, send ride offer, wait for acceptance, cascade on decline.
- **Contrast with taxis**: Traditional taxis have no GPS tracking, no spatial index. Dispatcher relies on radio communication and memory. Uber's automated dispatch already outperforms manual dispatch.
- **Problems found**: Greedy matching is locally optimal but globally suboptimal (matching D1 with R1 may strand R2). ETA based on straight-line distance is inaccurate (river crossings, one-way streets). No dynamic pricing — high demand → long wait times. No route-based ETA — can't distinguish between a driver 1km away on the same road vs 1km away across a highway.

### Attempt 2: Route-based ETA + basic pricing
- **Routing engine**: Build a road network graph from OpenStreetMap. Use A* or Contraction Hierarchies for shortest-path queries. ETA is now route-based (follows actual roads, respects one-way streets, accounts for turn penalties).
- **Traffic-aware ETA**: Use driver GPS traces to estimate real-time traffic on road segments. Adjust edge weights dynamically.
- **Basic dynamic pricing**: Divide city into grid cells. Count supply (available drivers) and demand (ride requests) per cell. If demand > supply × threshold → surge multiplier applied.
- **Contrast with Google Maps**: Google Maps computes ETAs for general public. Uber's routing is specialized for ride-sharing — pickup-side-of-road, driver heading direction, estimated time including walking to pickup.
- **Problems found**: Greedy dispatch still suboptimal. No shared rides (Pool). No real-time tracking for rider (rider doesn't see driver on map). No payment system (manual cash payment). Single-region — international expansion requires multi-region architecture.

### Attempt 3: Batched dispatch + real-time tracking + payments
- **Batched dispatch**: Accumulate ride requests for 2-5 seconds in dense areas. Solve assignment problem (riders ↔ drivers) to minimize total ETA globally. Use Hungarian algorithm or auction-based optimization.
- **Real-time tracking**: WebSocket connection between rider app and server. Driver's GPS updates pushed to rider's map in real-time (during DRIVER_EN_ROUTE and IN_PROGRESS).
- **Payment processing**: Stripe/Braintree integration. Asynchronous payment after trip completion. Ledger-based accounting (double-entry: rider debit, driver credit, platform commission).
- **Trip state machine**: Formal state machine for trip lifecycle (MATCHING → EN_ROUTE → ARRIVED → IN_PROGRESS → COMPLETED). Event sourcing for audit trail.
- **Contrast with food delivery**: DoorDash dispatch must also consider restaurant prep time (variable, unpredictable). Ride-sharing dispatch is simpler — only travel time matters, no "prep time" equivalent.
- **Problems found**: No shared rides (waste of driver capacity when two riders going in the same direction take separate cars). No driver supply management (drivers cluster in some areas, desert others). No ML-powered ETA (Contraction Hierarchies are accurate but don't capture time-of-day patterns, weather effects). Single-region backend.

### Attempt 4: Pool/Shared rides + supply management + ML
- **Uber Pool / Lyft Shared**: Match a new rider with an in-progress trip going in a similar direction. Routing optimization to minimize detour for existing passengers. This is a variant of the Vehicle Routing Problem (NP-hard) — use heuristics (detour threshold, direction similarity, pickup proximity).
- **Supply management**: Driver heat maps (show drivers where demand is high), incentive programs (surge zones, consecutive trip bonuses, quest bonuses), predicted demand (ML model forecasts demand 15-30 minutes ahead by area).
- **ML-powered ETA**: Train an ML model on billions of historical trips to predict ETA more accurately than pure routing. Features: road network path, real-time traffic, time-of-day, day-of-week, weather, special events.
- **Upfront pricing**: Quote a fixed price before the trip based on predicted route + ETA + surge. Shifts risk from rider to platform.
- **Contrast with airlines**: Airlines also use dynamic pricing (revenue management) but at a much slower timescale (hours/days). Both optimize for market clearing, but Uber's market clears in minutes while airlines' market clears over weeks.
- **Problems found**: Single-region infrastructure — if the data center fails, rides stop in that market. No chaos engineering — cascading failures between dispatch, routing, pricing, and location services. Kafka becomes a bottleneck (trillions of messages, many topics, consumer lag during spikes).

### Attempt 5: Multi-region + production hardening + advanced ML
- **Multi-region architecture**: Each major geographic market has its own infrastructure (geospatial index, dispatch, routing, surge). Global services for: user accounts, payment methods, driver profiles. Cross-region replication for globally mobile users.
- **Kafka at scale**: Sharded Kafka clusters per region. Dead letter queues for failed messages. Consumer lag monitoring and alerting. Back-pressure mechanisms for burst traffic.
- **Chaos engineering**: Uber has published about resilience testing — simulating service failures, DC failures, Kafka lag. Graceful degradation: if routing service is slow → fall back to straight-line ETA. If surge is stale → use last known values.
- **Michelangelo (ML platform)**: Centralized ML platform for training, deploying, and monitoring models. ETA models, surge prediction, fraud detection, driver destination prediction — all run on Michelangelo.
- **H3 hexagonal grid**: Replace geohash/S2 with H3 for surge pricing, demand forecasting, and heat maps. Uniform adjacency, hierarchical, better for spatial aggregation.
- **Safety features**: Real-time trip sharing, emergency button, driver photo verification, GPS trip recording, speed alerts, post-trip safety reports.
- **Contrast with Lyft**: Lyft's architecture is similar in broad strokes but Lyft has published less about its internals. Key differences: Lyft's dispatch algorithm, matching quality, and ML capabilities are generally considered slightly behind Uber's (smaller dataset for training). Lyft doesn't have an equivalent of H3 (uses standard geohash/S2).

## CRITICAL: Accuracy requirements

I DO NOT TRUST AI-GENERATED FACTS ABOUT SPECIFIC SYSTEMS. Every concrete claim about Uber internals must be verifiable against official sources. Specifically:

1. **Use WebSearch and WebFetch tools** to look up Uber Engineering blog, Uber open-source projects, and official documentation BEFORE writing. Search for:
   - "Uber engineering blog dispatch matching"
   - "Uber H3 hexagonal grid system"
   - "Uber engineering geospatial indexing"
   - "Uber engineering ETA computation"
   - "Uber surge pricing architecture"
   - "Uber Kafka deployment scale"
   - "Uber Michelangelo ML platform"
   - "Uber engineering Schemaless MySQL"
   - "Uber Ringpop consistent hashing"
   - "Uber engineering real-time push"
   - "Uber trip lifecycle event sourcing"
   - "Uber Contraction Hierarchies routing"
   - "Uber engineering map matching"
   - "Uber active drivers trips per day"
   - "Uber upfront pricing architecture"
   - "Lyft architecture dispatch system"
   - "DoorDash dispatch three party matching"

   **BLANKET PERMISSION**: You have full permission to use WebSearch and WebFetch to read any online documentation, blog posts, or research papers. Do NOT ask the user for permission to read — just read. This applies to eng.uber.com, uber.github.io, and any other reference source. Read as many pages as needed to verify facts.

2. **For every concrete number** (active drivers, trips per day, GPS updates per second, Kafka message volume), verify against Uber Engineering blog or official sources. If you cannot verify a number, explicitly write "[UNVERIFIED — check Uber Engineering Blog]" next to it.

3. **For every claim about Uber internals** (dispatch algorithm, spatial index implementation, pricing model), if it's not from an official source, mark it as "[INFERRED — not officially documented]".

4. **CRITICAL: Do NOT confuse Uber with Google Maps or food delivery.** These are different systems with different problems:
   - Uber: real-time two-party matching (rider ↔ driver), dynamic geospatial index (millions of moving points), surge pricing, trip lifecycle management
   - Google Maps: static routing and navigation, traffic estimation from crowdsourced data, no matching or dispatch
   - DoorDash/Uber Eats: three-party matching (customer ↔ restaurant ↔ courier), restaurant prep time uncertainty, more aggressive batching
   - Traditional taxis: manual dispatch, no spatial indexing, regulated flat pricing, no bidirectional ratings
   - When discussing design decisions, ALWAYS explain WHY Uber chose its approach and how alternatives reflect different operational models.

## Key Uber topics to cover

### Requirements & Scale
- Real-time ride-sharing platform with sub-3-minute average pickup time in major metros
- ~5M active drivers, ~30M+ trips/day, ~1.5M GPS updates/sec at peak
- Geospatial indexing of millions of moving points, updated every 3-4 seconds
- Sub-5-second ride request to driver offer latency
- Dynamic surge pricing recomputed every 1-2 minutes per geographic cell
- ETA queries at millions of QPS with <100ms latency
- Operating in 10,000+ cities across 70+ countries

### Architecture deep dives (create separate docs as listed in "Files to create" above)

### Design evolution (iterative build-up — the most important part)
- Attempt 0: Single server + SQL + manual/basic dispatch
- Attempt 1: Real-time GPS + geospatial index (geohash/quadtree) + greedy dispatch
- Attempt 2: Route-based ETA (Contraction Hierarchies) + basic surge pricing
- Attempt 3: Batched dispatch + real-time tracking (WebSocket) + payments + trip state machine
- Attempt 4: Pool/Shared rides + supply management + ML-powered ETA + upfront pricing
- Attempt 5: Multi-region + production hardening + Kafka at scale + H3 + Michelangelo + safety

Each attempt MUST:
1. Start by identifying concrete problems with the previous attempt
2. Propose a solution
3. Explain WHY this approach (and why not the alternative — mention Google Maps, traditional taxis, or food delivery where relevant)
4. End with "what's still broken?" to motivate the next attempt

### Consistency & Data
- MySQL/Schemaless for trip and user data (transactional, sharded)
- Cassandra for high-write-throughput data (location history, event logs)
- Redis for real-time state (geospatial index, dispatch state, rate limiting)
- Kafka for event streaming (location updates, trip events, pricing events — trillions of messages/day)
- S3/HDFS for data lake (analytics, ML training)
- Event sourcing for trip lifecycle (immutable event log, replay capability)
- Strong consistency for payments and trip state transitions
- Eventual consistency acceptable for location updates, surge pricing, analytics

## What NOT to do
- Do NOT treat Uber as "just a map with cars" — it's a real-time marketplace with dispatch, pricing, payments, safety, and ML. Frame it accordingly.
- Do NOT confuse Uber with Google Maps or food delivery. Highlight differences at every layer, don't blur them.
- Do NOT jump to the final architecture. Build it step by step (Attempt 0 → 5).
- Do NOT make up internal implementation details — verify against Uber Engineering Blog or mark as inferred.
- Do NOT skip the iterative build-up.
- Do NOT write a Wikipedia article. This is an INTERVIEW SIMULATION with dialogue.
- Do NOT describe features without explaining WHY they exist (what problem they solve).
- Do NOT ask the user for permission to read online documentation — blanket permission is granted (see Accuracy requirements section).
- Do NOT ignore the geospatial challenge — the real-time spatial index of millions of moving points is what makes Uber fundamentally different from a CRUD app. Treat it as a first-class architectural concern.
