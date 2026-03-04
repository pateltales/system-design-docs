# Design Trade-offs — Why This and Not That

> Every design choice has a cost. This document analyzes the trade-offs
> Uber made — not just "what" they chose, but "why" they chose it,
> what alternatives were considered, and under what conditions
> a different choice would be better.

---

## Table of Contents

1. [H3 vs Geohash vs Quadtree vs S2](#1-h3-vs-geohash-vs-quadtree-vs-s2)
2. [Greedy vs Batched Dispatch](#2-greedy-vs-batched-dispatch)
3. [Upfront Pricing vs Metered Pricing](#3-upfront-pricing-vs-metered-pricing)
4. [Event Sourcing vs CRUD for Trips](#4-event-sourcing-vs-crud-for-trips)
5. [WebSocket vs MQTT vs SSE](#5-websocket-vs-mqtt-vs-sse)
6. [Kafka Backbone vs Direct Service Calls](#6-kafka-backbone-vs-direct-service-calls)
7. [Build vs Buy for Maps](#7-build-vs-buy-for-maps)
8. [MySQL/Schemaless vs NoSQL](#8-mysqlschemaless-vs-nosql)
9. [Simple Surge vs ML-Based Pricing](#9-simple-surge-vs-ml-based-pricing)
10. [Contraction Hierarchies vs Dijkstra vs A*](#10-contraction-hierarchies-vs-dijkstra-vs-a)

---

## 1. H3 vs Geohash vs Quadtree vs S2

### What Uber Chose: H3 (Hexagonal Hierarchical Spatial Index)

```
Uber developed and open-sourced H3 for spatial indexing in
surge pricing, demand forecasting, and driver heat maps.

Why hexagons?

  Square grid (geohash):
    ┌──┬──┬──┐
    │  │  │  │    4 edge-adjacent neighbors (N,S,E,W)
    ├──┼──┼──┤    4 corner-adjacent neighbors (NE,SE,SW,NW)
    │  │  │  │    Corner neighbors are √2 farther than edge neighbors
    ├──┼──┼──┤    → Non-uniform adjacency
    │  │  │  │    → "Corner problem" in range queries
    └──┴──┴──┘

  Hexagonal grid (H3):
     / \ / \ / \
    | A | B | C |    6 edge-adjacent neighbors
     \ / \ / \ /     ALL at the same distance
      | D | E |      No corner neighbors
       \ / \ /       → Uniform adjacency
                     → Smooth gradients for surge pricing

The corner problem in practice:
  With geohash, if you're looking for drivers "within 1 cell,"
  a driver in a diagonal-adjacent cell is 41% farther away
  than a driver in an edge-adjacent cell. This creates uneven
  coverage and requires ad-hoc corrections.

  With H3, all 6 neighbors are equidistant → kRing(1) gives
  a uniform search radius. No corrections needed.
```

### Trade-off Analysis

```
┌─────────────────┬──────────────┬──────────────┬──────────────┬──────────────┐
│ Criterion       │ H3           │ Geohash      │ Quadtree     │ S2           │
├─────────────────┼──────────────┼──────────────┼──────────────┼──────────────┤
│ Adjacency       │ Uniform      │ Non-uniform  │ Uniform      │ Uniform      │
│                 │ (6 neighbors)│ (8 neighbors)│ (varies)     │ (varies)     │
├─────────────────┼──────────────┼──────────────┼──────────────┼──────────────┤
│ Range queries   │ Excellent    │ Edge effects │ Good         │ Good         │
│                 │ (kRing)      │ at borders   │              │              │
├─────────────────┼──────────────┼──────────────┼──────────────┼──────────────┤
│ Hierarchical    │ Yes (16      │ Yes (12      │ Yes          │ Yes (30      │
│ resolution      │ levels)      │ levels)      │ (continuous) │ levels)      │
├─────────────────┼──────────────┼──────────────┼──────────────┼──────────────┤
│ Adoption        │ Growing      │ Widespread   │ Widespread   │ Moderate     │
│                 │ (Uber-led)   │ (Redis, DBs) │ (custom)     │ (Google-led) │
├─────────────────┼──────────────┼──────────────┼──────────────┼──────────────┤
│ Built-in DB     │ Limited      │ Redis GEOADD │ Custom only  │ Google Cloud │
│ support         │ (custom lib) │ PostGIS, etc │              │ Spanner      │
├─────────────────┼──────────────┼──────────────┼──────────────┼──────────────┤
│ Best for        │ Spatial      │ Simple       │ Dynamic      │ Spherical    │
│                 │ aggregation, │ "nearby"     │ point data   │ geometry,    │
│                 │ smooth grids │ queries      │ (varying     │ Google infra │
│                 │              │              │ density)     │              │
└─────────────────┴──────────────┴──────────────┴──────────────┴──────────────┘

When NOT to use H3:
  • You need built-in database support → geohash (Redis, PostgreSQL)
  • Your scale doesn't justify the complexity → geohash + Redis is simpler
  • You need variable-density indexing → quadtree adapts to data density
  • You're in the Google ecosystem → S2 integrates with Spanner/Bigtable

When to use H3:
  • Spatial aggregation is critical (surge pricing, heat maps, demand forecasting)
  • You need smooth gradients across cell boundaries
  • You're operating at Uber's scale with custom infrastructure
  • You can invest in H3 library integration (it's open-source, well-documented)

For most companies: geohash + Redis is the right starting point.
H3 is a refinement that pays off at massive scale with spatial aggregation needs.
```

---

## 2. Greedy vs Batched Dispatch

### What Uber Does: Both (Depending on Market Density)

```
Greedy dispatch:
  Rider requests → immediately find nearest driver → send offer.
  Latency: ~1 second from request to driver offer.
  Quality: locally optimal (best driver for THIS rider).
  Problem: may be globally suboptimal.

  Example:
    Driver D1 is 2 min from Rider R1 and 3 min from Rider R2.
    Driver D2 is 8 min from R1 and 4 min from R2.

    Greedy: assign D1→R1 (nearest). Then D2→R2.
    Total wait: 2 + 4 = 6 minutes.

    But optimal: D1→R2 (3 min), D2→R1 (8 min) — wait, that's worse.
    Or better: D2→R2 (4 min), D1→R1 (2 min) = 6 min — same.

    Real value of batching emerges with many riders and drivers:
    with 100 riders and 100 drivers, the globally optimal assignment
    can save significant total wait time vs greedy.

Batched dispatch:
  Accumulate requests for 2-5 seconds.
  Solve the assignment problem (riders ↔ drivers) globally.
  Use Hungarian algorithm or auction-based optimization.
  Latency: 2-5 seconds from request to driver offer.
  Quality: globally optimal (minimizes total wait across all riders).

Uber's approach:
  Dense markets (Manhattan, central London):
    Batched dispatch. Many riders and drivers available.
    2-5 second batch window produces meaningful optimization.
    The extra latency (2-5 seconds) is small relative to
    the total wait time (2-5 minutes).

  Sparse markets (suburbs, rural areas):
    Greedy dispatch. Few riders and drivers.
    Batching for 5 seconds might accumulate 0-1 additional requests.
    The optimization gain is negligible.
    Better to match immediately.
```

### Trade-off Summary

```
                Greedy                          Batched
  Latency:      ~1 second                       ~3-7 seconds
  Quality:      Locally optimal                 Globally optimal
  Complexity:   Simple (nearest driver)         Complex (optimization solver)
  Best for:     Sparse markets, fast response   Dense markets, efficiency
  Worst case:   Suboptimal assignments          Added latency with no benefit
                                                 (if batch has only 1 request)

The key insight: batch window length is the control variable.
  Too short (1 second): same as greedy — no optimization opportunity.
  Too long (10 seconds): rider waits 10 seconds before anything happens.
  Sweet spot: 2-5 seconds in dense markets.
  Uber can dynamically adjust the batch window based on market density.
```

---

## 3. Upfront Pricing vs Metered Pricing

### What Uber Chose: Upfront Pricing (2016)

```
Before 2016: metered (pay actual time + distance after trip).
After 2016: upfront (see exact price before trip).

Why Uber switched:

  1. Rider anxiety: "How much will this trip cost?"
     → Metered: unknown until trip ends. Rider watches meter with anxiety.
     → Upfront: known before trip starts. Rider decides with full information.

  2. Comparison shopping: "Should I take Uber or Lyft?"
     → Metered: can't compare without taking both trips.
     → Upfront: compare Uber $18 vs Lyft $16 before committing.

  3. Driver incentive alignment:
     → Metered: driver earns more on longer routes (misaligned incentive).
     → Upfront: rider pays a fixed price regardless of route.
        Driver is paid based on actual time+distance, so there's still
        incentive to be efficient (higher effective hourly rate).

  4. Platform margin optimization:
     → Metered: platform gets a fixed % of whatever the trip costs.
     → Upfront: platform sets the price independently of driver earnings.
        Over millions of trips, small margin improvements compound.

Trade-offs:
  PRO: Better rider experience, enables comparison shopping,
       margin optimization, rider can budget precisely.
  CON: Platform absorbs variance (loses money on expensive trips,
       makes money on cheap trips). Requires accurate ETA and route
       prediction (if predictions are wrong, margin suffers).
       Decouples rider payment from driver earnings — less transparent.

When metered is better:
  • When routes are highly unpredictable (e.g., adventure tourism)
  • When the platform doesn't have accurate ETA models yet
  • When regulations require metered pricing (some jurisdictions)
  • Early-stage ride-sharing (simpler to implement)
```

---

## 4. Event Sourcing vs CRUD for Trips

### What Uber Chose: Event Sourcing

```
Event sourcing: store immutable events, derive current state.
CRUD: store current state, mutate on each change.

Why event sourcing for trips?

  1. REGULATORY AND LEGAL REQUIREMENTS
     Cities, regulators, and insurance companies require trip data:
     "Where was the driver at 3:47 PM during this trip?"
     With CRUD: can only answer "where is the driver NOW."
     With event sourcing: replay events to T=3:47 PM → exact location.

  2. FINANCIAL AUDIT
     Payments involve money. Every dollar movement must be traceable.
     "Why was this rider charged $18.50?"
     With CRUD: "because that's what's in the database" (no history).
     With event sourcing: replay fare calculation events to show
     exactly how $18.50 was computed.

  3. DISPUTE RESOLUTION
     "The driver took a longer route and I was overcharged."
     With CRUD: hard to verify (current route ≠ disputed route).
     With event sourcing: replay GPS events → reconstruct exact route
     → compare to optimal route → adjudicate.

  4. BUG FIXES AND RECALCULATION
     Found a bug in the fare formula? With event sourcing, replay
     all trips from the last week through the corrected formula
     → recalculate fares → issue refunds.
     With CRUD: intermediate state is lost. Can't recalculate.

Trade-offs:
  PRO: Full audit trail, replay capability, temporal queries,
       decoupled consumers (multiple services consume events independently).
  CON: More storage (events accumulate), more complexity (need projection
       layer for current state queries), eventual consistency between
       event log and projections.

When CRUD is better:
  • Simple domains where history doesn't matter
  • Low-value transactions (no legal/audit requirements)
  • When storage costs are a primary concern
  • Small teams without event sourcing expertise
  • Data that is truly mutable (user preferences, settings)
```

---

## 5. WebSocket vs MQTT vs SSE

### What Uber Chose: WebSocket

```
Uber's real-time use cases:
  • Driver GPS upload (every 3-4 seconds) — bidirectional
  • Ride offer delivery — server → driver
  • Driver location push to rider — server → rider
  • Chat — bidirectional
  • Trip events — server → rider/driver

Why WebSocket (not MQTT or SSE)?

  WebSocket:
    + Bidirectional (driver sends GPS, receives offers)
    + Full-duplex (send and receive simultaneously)
    + Binary protocol support (efficient GPS encoding)
    + Well-supported in all mobile platforms
    - Stateful connections → complex scaling (sticky sessions)
    - Connection management overhead (heartbeats, reconnection)
    - Firewall/proxy issues (some corporate networks block WS)

  MQTT:
    + Extremely battery-efficient (2-byte header)
    + Designed for unreliable networks (QoS levels, will messages)
    + Lightweight client library (small app size)
    + Built-in pub/sub (topic-based routing)
    - Less suited for high-frequency data (GPS every 1-2 seconds)
    - Requires an MQTT broker (additional infrastructure)
    - Less common in general backend engineering

  SSE (Server-Sent Events):
    + Simple (HTTP-based, no special protocol)
    + Automatic reconnection in browsers
    + Works through all HTTP proxies
    - Unidirectional (server → client only)
    - Would require a separate channel for driver GPS upload
    - Text-only (no binary support in standard SSE)

Why Uber chose WebSocket over MQTT:
  1. Uber's app is typically in the foreground during trips
     → Battery optimization (MQTT's main advantage) is less critical.
  2. GPS data is high-frequency (every 1-2 seconds) and binary
     → WebSocket's binary frame support is more efficient.
  3. Bidirectional communication is needed on the SAME connection
     (driver sends GPS AND receives ride offers on one connection).
  4. WebSocket is a more standard protocol — easier to hire engineers,
     better library support, more operational tooling.

When to use MQTT instead:
  • IoT devices with severe battery/bandwidth constraints
  • Background messaging (notifications when app is not in foreground)
  • Instagram/Meta's use case: sporadic notifications, not continuous streams
  • When you need QoS guarantees (MQTT has built-in QoS 0/1/2)
```

---

## 6. Kafka Backbone vs Direct Service Calls

### What Uber Chose: Kafka for Most Data Flows, Direct RPCs for Latency-Critical Paths

```
Uber's hybrid approach:
  Kafka: location updates, trip events, pricing events, analytics
  Direct gRPC: dispatch decisions, ride offers, payment charges

Why not direct calls for everything?
  10+ services consume driver location updates.
  With direct calls: Location Service calls 10 services on every
  GPS update → 1.5M updates/sec × 10 calls = 15M RPCs/sec.
  If one consumer is slow → back-pressure on Location Service
  → GPS ingestion slows down → dispatch quality degrades.

  With Kafka: Location Service writes once to Kafka.
  10 consumers read independently at their own pace.
  If one consumer is slow, it falls behind but doesn't affect others.

Why not Kafka for everything?
  Kafka adds latency (typically 10-50ms for produce + consume).
  For dispatch: after deciding "Driver D1 gets this ride,"
  the offer must reach D1's phone in <1 second.
  Going through Kafka adds unnecessary latency.
  → Direct gRPC from Dispatch Service to Push Service → WebSocket → driver.

Trade-off summary:
  Kafka: decoupling, replay, multiple consumers, durability.
         Cost: added latency, operational complexity.
  Direct RPC: low latency, simple request-response.
         Cost: tight coupling, no replay, single consumer.

  Uber's rule of thumb:
    If the data has multiple consumers → Kafka.
    If the operation is latency-critical (<100ms) → direct RPC.
    For some events: BOTH (write to Kafka for durability,
    direct RPC for latency).
```

---

## 7. Build vs Buy for Maps

### What Uber Did: Started with Google Maps, Gradually Built In-House

```
Evolution:
  2012-2015: Google Maps API for routing, ETA, geocoding.
    Cost: $5-10 per 1,000 API calls × millions of daily calls = $$$.
    Limitation: no ride-sharing-specific features (pickup side of road,
    driver heading, ETA for drivers — not for general navigation).

  2015-2018: Gradually built proprietary mapping stack.
    Built from: driver GPS traces (millions of trips/day provide
    dense road coverage on ride-share-relevant roads).
    Capabilities: own routing (Contraction Hierarchies), own map matching,
    own geocoding, own traffic estimation, own ETA models.

  2018+: Largely independent of Google Maps for core operations.
    Still uses Google Maps for some features (map tiles for the rider app,
    geocoding for address search, some satellite imagery).

Why build in-house?

  1. COST
     At Uber's scale (~billions of ETA queries/day), Google Maps API
     costs would be enormous. Own routing engine: ~$0 per query.

  2. FEATURE CUSTOMIZATION
     Google Maps routes TO an address. Uber needs to route to
     the correct SIDE OF THE ROAD (rider is on the east side of 5th Ave,
     don't send the driver to the west side requiring a U-turn).
     Google Maps doesn't optimize for this.

  3. LATENCY
     Google Maps API: ~50-200ms per query (network round-trip to Google).
     Own routing engine: ~5-10ms (in-process, co-located with dispatch).
     This 10-40x improvement matters at millions of QPS.

  4. DATA ADVANTAGE
     Uber's own driver GPS traces are more relevant for ride-sharing
     roads than Google's general-purpose traffic data.
     Uber knows: actual average speeds on ride-share roads,
     pickup/dropoff time at specific locations, turn delays.

Trade-offs:
  PRO: Lower cost at scale, custom features, lower latency,
       proprietary data advantage.
  CON: Enormous engineering investment (hundreds of engineers over years),
       mapping is a deep, specialized domain (cartography, geospatial
       algorithms, data quality), coverage gaps on non-ride-share roads.

When to buy (use Google Maps):
  • Small to medium scale (Google Maps API is free up to a quota)
  • No ride-sharing-specific routing needs
  • No engineering capacity to build a mapping stack
  • Broad coverage needed (every road, not just ride-share roads)

When to build:
  • At Uber/Lyft scale (cost savings justify the investment)
  • Need features Google Maps doesn't offer (pickup-side routing,
    driver heading-aware ETA, ride-share-specific traffic)
  • Latency requirements that API calls can't meet (<10ms)
  • Competitive advantage from proprietary mapping data
```

---

## 8. MySQL/Schemaless vs NoSQL

### What Uber Chose: MySQL with a Custom Sharding Layer (Schemaless)

```
Why not Cassandra or DynamoDB for transactional data?

  Uber DOES use Cassandra — for write-heavy, analytics-oriented data
  (GPS history, trip event logs). But for transactional data
  (trips, users, payments): MySQL/Schemaless.

  Why MySQL over Cassandra for transactional data:
    1. ACID transactions: trip state transitions and payment charges
       require atomicity. Cassandra has lightweight transactions but
       they're slow and limited.
    2. Strong consistency: when a rider cancels a trip, the dispatch
       system must immediately see the cancellation. Cassandra's eventual
       consistency could lead to a cancelled trip still being dispatched.
    3. Operational familiarity: MySQL is the most widely known database.
       Uber's engineering team could hire, train, and operate MySQL
       infrastructure with confidence.
    4. Rich querying: JOIN, subquery, complex WHERE clauses.
       Schemaless wraps this with JSON blobs but the underlying
       MySQL still supports complex queries when needed.

  Why Schemaless over plain MySQL:
    1. Horizontal scaling: single MySQL instance can't handle
       Uber's write volume. Schemaless shards across many MySQL instances.
    2. Schema flexibility: store JSON blobs → no ALTER TABLE downtime.
       Application-level schema evolution.
    3. Change Data Capture: every write generates a Kafka event
       → enables event-driven architecture.
    4. Write buffering: absorbs burst traffic.

  Why Schemaless over DynamoDB:
    1. DynamoDB is AWS-only. Uber runs multi-cloud/on-prem.
    2. Cost: at Uber's scale, managed DynamoDB would cost significantly
       more than self-managed MySQL.
    3. Control: Uber can tune MySQL's behavior, add custom features
       (write buffering, CDC, custom sharding) — not possible with DynamoDB.

Trade-offs:
  PRO: ACID for payments/trips, strong consistency, familiar ops,
       full control, lower cost at extreme scale.
  CON: Custom engineering (Schemaless is Uber-internal — not open-source),
       operational burden (manage MySQL fleet), sharding complexity
       (cross-shard queries are expensive).

For most companies:
  Use DynamoDB (AWS) or Cloud Spanner (GCP) or PlanetScale (MySQL-compatible).
  Building a custom sharding layer only makes sense at Uber's scale
  with a large infrastructure engineering team.
```

---

## 9. Simple Surge vs ML-Based Pricing

### What Uber Did: Evolved from Simple Surge (2012) to ML Pricing (2017+)

```
Simple surge (2012-2016):
  multiplier = f(demand / supply)
  Same multiplier for all riders in the same cell.
  Transparent: "Your ride is 2.3x surge."
  Easy to explain, easy to implement.
  Problem: blunt instrument. Doesn't account for route length,
  rider value, market conditions, weather, events.

ML-based pricing (2017+):
  price = MLmodel(supply, demand, route, time, weather, events, ...)
  Market-level pricing (NOT per-rider personalization).
  Less transparent: "Your ride costs $24.50" (no visible multiplier).
  Better market clearing: the model finds prices that balance
  supply and demand more efficiently.

Why Uber evolved:
  1. Simple surge over-corrects: during a moderate demand spike,
     a 2.5x multiplier might reduce demand too much → drivers are idle.
     ML pricing can find the "Goldilocks" price more precisely.
  2. Route matters: a 30-minute highway trip and a 30-minute city trip
     in the same cell have different costs and demand elasticity.
     Simple surge treats them the same.
  3. Upfront pricing + ML: once rider sees a fixed price (not a multiplier),
     the platform can use any pricing logic behind the scenes.
     The shift to upfront pricing in 2016 enabled ML pricing in 2017.

Trade-offs:
  Simple surge:
    + Transparent (rider sees the multiplier)
    + Explainable (regulators understand it)
    + Simple implementation
    - Blunt (same multiplier for very different trips)
    - Suboptimal market clearing
    - Can't incorporate route/weather/events

  ML-based pricing:
    + Better market clearing (more precise supply/demand balance)
    + Incorporates many signals (route, weather, events, etc.)
    + Higher platform revenue at same rider satisfaction
    - Opaque ("why does this trip cost $24.50?")
    - Fairness concerns (are some riders systematically charged more?)
    - Regulatory scrutiny (some cities ban algorithmic pricing)
    - Complex to build, train, and monitor

Controversy:
  In 2017, reports suggested Uber was charging iPhone users more
  than Android users. Uber denied this, stating the model uses
  market-level features, not individual rider characteristics.
  Regardless, the perception of personalized pricing is a PR risk.
```

---

## 10. Contraction Hierarchies vs Dijkstra vs A*

### What Uber Chose: Customizable Contraction Hierarchies (CCH)

```
The routing problem: find the shortest-TIME path between two points
on a graph with ~24M nodes (USA road network).

Dijkstra:
  + Simple, correct, no preprocessing
  + Handles dynamic edge weights (traffic) natively
  - SLOW: explores millions of nodes per query
  - ~1-5 seconds per query
  - Impractical at Uber's QPS (millions/sec)

A*:
  + Faster than Dijkstra (heuristic guides search toward destination)
  + No preprocessing required
  + Handles dynamic weights
  - Still too slow: ~0.5-2 seconds per query
  - Heuristic must be admissible (hard to get right with traffic)

Contraction Hierarchies (CH):
  + FAST: <10ms per query (1000x faster than Dijkstra)
  + Well-studied algorithm (Geisberger et al., 2008)
  - Preprocessing: 1-4 hours (rebuild when road graph changes)
  - Standard CH doesn't handle traffic well
    (changing edge weights requires re-preprocessing)

Customizable Contraction Hierarchies (CCH):
  + <10ms per query
  + Separate topology from weights: preprocess topology once (hours),
    update weights in seconds when traffic changes
  + Handles real-time traffic without re-preprocessing
  - More complex implementation than standard CH
  - Higher memory (~2x the original graph for shortcuts)

Why CCH at Uber:
  1. Millions of ETA queries per second → <10ms per query is essential.
  2. Traffic changes continuously → need to update weights in seconds.
  3. CCH provides both: fast queries AND real-time traffic integration.

When to use Dijkstra/A*:
  • Small graphs (<100K nodes) where query time is acceptable
  • When preprocessing time is not available (fully dynamic graphs)
  • When simplicity is more important than performance
  • Educational contexts or prototypes

When to use CH/CCH:
  • Large graphs (millions of nodes) with real-time query requirements
  • When preprocessing time (hours) is acceptable
  • When you need millions of QPS at <10ms per query
  • Production routing engines at scale (Uber, Google Maps, OSRM)
```

---

## Summary: When to Use Uber's Choices vs Simpler Alternatives

```
┌──────────────────────┬──────────────────────┬──────────────────────┐
│ Uber's Choice        │ Simpler Alternative  │ Use Simple When      │
├──────────────────────┼──────────────────────┼──────────────────────┤
│ H3 (hexagonal grid)  │ Geohash + Redis      │ No spatial           │
│                      │                      │ aggregation needed   │
├──────────────────────┼──────────────────────┼──────────────────────┤
│ Batched dispatch     │ Greedy (nearest)     │ Sparse market,       │
│                      │                      │ <100 requests/min    │
├──────────────────────┼──────────────────────┼──────────────────────┤
│ Upfront pricing      │ Metered pricing      │ Early stage, no      │
│                      │                      │ reliable ETA model   │
├──────────────────────┼──────────────────────┼──────────────────────┤
│ Event sourcing       │ CRUD with audit log  │ No financial/legal   │
│                      │                      │ audit requirements   │
├──────────────────────┼──────────────────────┼──────────────────────┤
│ WebSocket            │ SSE or Long Polling  │ Unidirectional push, │
│                      │                      │ low update frequency │
├──────────────────────┼──────────────────────┼──────────────────────┤
│ Kafka backbone       │ Direct gRPC calls    │ <5 consumers per     │
│                      │                      │ event type           │
├──────────────────────┼──────────────────────┼──────────────────────┤
│ Own mapping stack    │ Google Maps API      │ <1M ETA queries/day  │
│                      │                      │ (API costs < eng     │
│                      │                      │  salary costs)       │
├──────────────────────┼──────────────────────┼──────────────────────┤
│ MySQL/Schemaless     │ DynamoDB / managed   │ Team < 50 engineers, │
│                      │ database             │ no custom infra team │
├──────────────────────┼──────────────────────┼──────────────────────┤
│ ML pricing           │ Simple surge         │ <1M trips/day,       │
│                      │ multiplier           │ transparency needed  │
├──────────────────────┼──────────────────────┼──────────────────────┤
│ Contraction          │ Dijkstra or A*       │ Graph < 100K nodes,  │
│ Hierarchies          │                      │ <100 queries/sec     │
└──────────────────────┴──────────────────────┴──────────────────────┘

The overarching principle:
  Uber's choices are correct FOR UBER.
  At smaller scale, the simpler alternative is almost always better.
  Complexity is a cost — only pay it when the scale demands it.
```
