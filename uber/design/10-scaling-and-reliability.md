# Scaling, Performance & Reliability — Keeping Rides Running

> Uber's failure mode isn't "video won't load" — it's "person is stranded."
> A rider on a dark street corner at 2 AM can't tolerate a 5-minute dispatch outage.
> A driver in traffic can't wait for a 30-second ETA recomputation.
> The reliability bar for a transportation platform is fundamentally higher
> than for a content platform.

---

## Table of Contents

1. [Scale Numbers](#1-scale-numbers)
2. [Latency Budgets](#2-latency-budgets)
3. [Availability Targets](#3-availability-targets)
4. [Graceful Degradation](#4-graceful-degradation)
5. [Multi-Region Architecture](#5-multi-region-architecture)
6. [Peak Event Handling](#6-peak-event-handling)
7. [Chaos Engineering & Resilience](#7-chaos-engineering--resilience)
8. [Contrasts](#8-contrasts)

---

## 1. Scale Numbers

```
┌──────────────────────────────────────────────────────────────┐
│ Metric                              │ Approximate Scale      │
├──────────────────────────────────────┼────────────────────────┤
│ Monthly active riders                │ ~150M [UNVERIFIED]     │
│ Active drivers (globally)            │ ~5M                    │
│ Trips per day                        │ ~30M+ [UNVERIFIED]     │
│ Operating cities                     │ 10,000+ across 70+     │
│                                      │ countries              │
├──────────────────────────────────────┼────────────────────────┤
│ GPS updates per second (peak)        │ ~1.5M                  │
│ GPS updates per day                  │ ~100-130B              │
│ GPS data per day                     │ ~10-13 TB              │
│ Kafka messages per day               │ Trillions [VERIFIED]   │
├──────────────────────────────────────┼────────────────────────┤
│ ETA queries per second               │ Millions               │
│ Dispatch decisions per second        │ ~100K-500K             │
│ WebSocket connections (concurrent)   │ ~15-25M                │
├──────────────────────────────────────┼────────────────────────┤
│ Average pickup time (major metros)   │ <3 minutes             │
│ Average trip duration                │ ~15-20 minutes         │
│ Average trip distance                │ ~5-8 miles             │
└──────────────────────────────────────┴────────────────────────┘

Back-of-envelope calculations:

  GPS updates/second:
    5M drivers × (1 update / 3.5 seconds average) ≈ 1.43M updates/sec
    (Not all 5M are online simultaneously — but at peak, this is
     the order of magnitude)

  GPS data per day:
    1.5M updates/sec × 86,400 sec/day = ~130B updates/day
    Each update: ~100 bytes (lat, lng, heading, speed, timestamp, driverId)
    130B × 100 bytes = ~13 TB/day (raw, before compression)

  Dispatch decisions per second:
    30M trips/day ÷ 86,400 sec/day ≈ 350 trips/sec average
    Each trip may involve 1-3 dispatch attempts (cascading on decline)
    Peak is 5-10x average (rush hours, events)
    → ~100K-500K dispatch decisions per second at peak
```

---

## 2. Latency Budgets

```
Ride Request → Driver Receives Offer (target: <5 seconds total)

  ┌────────────────────────────┬─────────────┐
  │ Step                       │ Budget      │
  ├────────────────────────────┼─────────────┤
  │ Rider taps "Request Ride"  │ 0ms         │
  │ → HTTP to API Gateway      │ ~100ms      │
  │ → Validate request         │ ~50ms       │
  │ → Compute upfront price    │ ~200ms      │
  │   (ETA + route + surge)    │             │
  │ → Find nearby drivers      │ ~100ms      │
  │   (geospatial index query) │             │
  │ → Dispatch decision        │ ~500ms      │
  │   (batch window + ranking) │             │
  │ → Send offer to driver     │ ~100ms      │
  │   (WebSocket push)         │             │
  │ → Display on driver's phone│ ~200ms      │
  │   (client rendering)       │             │
  ├────────────────────────────┼─────────────┤
  │ TOTAL                      │ ~1.3 seconds│
  │ (Well under 5-second budget│             │
  │  with room for retries     │             │
  │  and cascading)            │             │
  └────────────────────────────┴─────────────┘

  After the offer is sent:
    Driver has 15 seconds to accept.
    If decline/timeout → cascade to next driver (+15 seconds each).
    Worst case: 3 cascades = 45 seconds of matching time.
    Plus initial request processing = ~47 seconds.
    This is still well within the rider's tolerance
    (~60 seconds before frustration).

ETA Query (target: <100ms)
  ┌────────────────────────────┬─────────────┐
  │ Step                       │ Budget      │
  ├────────────────────────────┼─────────────┤
  │ Map-match input coordinates│ ~1ms        │
  │ CH shortest path query     │ ~5-10ms     │
  │ Traffic adjustment         │ ~2-5ms      │
  │ ML correction              │ ~2-5ms      │
  │ Network + serialization    │ ~5-10ms     │
  ├────────────────────────────┼─────────────┤
  │ TOTAL                      │ ~15-30ms    │
  │ (Well under 100ms budget)  │             │
  └────────────────────────────┴─────────────┘

Location Update Ingestion (target: <1 second)
  ┌────────────────────────────┬─────────────┐
  │ Step                       │ Budget      │
  ├────────────────────────────┼─────────────┤
  │ Driver phone → API gateway │ ~100-300ms  │
  │ Gateway → Kafka            │ ~10ms       │
  │ Kafka → Geospatial index   │ ~50-200ms   │
  │ Index update (Redis)       │ ~1ms        │
  ├────────────────────────────┼─────────────┤
  │ TOTAL                      │ ~200-500ms  │
  └────────────────────────────┴─────────────┘

Driver Location on Rider's Map (target: <2 seconds end-to-end)
  ┌────────────────────────────┬─────────────┐
  │ Step                       │ Budget      │
  ├────────────────────────────┼─────────────┤
  │ Driver GPS captured        │ 0ms         │
  │ → Driver phone → server    │ ~100-300ms  │
  │ → Server processes         │ ~50ms       │
  │   (map-match, snap to road)│             │
  │ → Push to rider via WS     │ ~100ms      │
  │ → Rider app renders on map │ ~100-200ms  │
  ├────────────────────────────┼─────────────┤
  │ TOTAL                      │ ~400-700ms  │
  │ (Well under 2-second budget│             │
  └────────────────────────────┴─────────────┘
```

---

## 3. Availability Targets

```
Not all services need the same availability:

  ┌─────────────────────────┬──────────────┬──────────────────────────┐
  │ Service                 │ Target       │ Why?                     │
  ├─────────────────────────┼──────────────┼──────────────────────────┤
  │ Dispatch                │ 99.99%       │ If dispatch is down,     │
  │                         │ (~53 min/yr) │ NO rides happen. Riders  │
  │                         │              │ are stranded.            │
  ├─────────────────────────┼──────────────┼──────────────────────────┤
  │ Location Service        │ 99.99%       │ Without driver locations │
  │                         │              │ matching quality drops   │
  │                         │              │ to zero. Can't find      │
  │                         │              │ nearby drivers.          │
  ├─────────────────────────┼──────────────┼──────────────────────────┤
  │ ETA / Routing           │ 99.9%        │ Can fall back to         │
  │                         │ (~8.7 hr/yr) │ straight-line distance   │
  │                         │              │ estimates (degraded but  │
  │                         │              │ functional).             │
  ├─────────────────────────┼──────────────┼──────────────────────────┤
  │ Surge Pricing           │ 99.9%        │ Can fall back to last    │
  │                         │              │ known surge values or    │
  │                         │              │ 1.0x (no surge).         │
  ├─────────────────────────┼──────────────┼──────────────────────────┤
  │ Payment Service         │ 99.9%        │ Payment can be retried.  │
  │                         │              │ Brief delay is tolerable │
  │                         │              │ (rider doesn't wait for  │
  │                         │              │ payment to exit the car).│
  ├─────────────────────────┼──────────────┼──────────────────────────┤
  │ WebSocket Gateway       │ 99.9%        │ Clients auto-reconnect.  │
  │                         │              │ Brief gap (2-5 seconds)  │
  │                         │              │ in live tracking.        │
  ├─────────────────────────┼──────────────┼──────────────────────────┤
  │ Ride Type Suggestions   │ 99%          │ If down, show all ride   │
  │ (recommendation engine) │ (~3.6 days/yr)│ types without            │
  │                         │              │ personalization. No      │
  │                         │              │ impact on core ride flow.│
  └─────────────────────────┴──────────────┴──────────────────────────┘
```

---

## 4. Graceful Degradation

```
When a service degrades, the system should still function,
even if at reduced quality.

┌──────────────────────┬──────────────────────┬──────────────────────┐
│ Service Down         │ Fallback             │ Impact               │
├──────────────────────┼──────────────────────┼──────────────────────┤
│ Routing (CH) engine  │ Straight-line        │ ETA accuracy drops   │
│                      │ distance × average   │ significantly.       │
│                      │ speed factor         │ Dispatch still works.│
│                      │ (Haversine × 1.4)    │ Rider sees "ETA      │
│                      │                      │ approximate."        │
├──────────────────────┼──────────────────────┼──────────────────────┤
│ Surge pricing        │ Use last known surge │ Some price mismatch. │
│ pipeline             │ values. If stale     │ Supply/demand may    │
│                      │ >10 min: default to  │ be imbalanced but    │
│                      │ 1.0x (no surge).     │ rides still work.    │
├──────────────────────┼──────────────────────┼──────────────────────┤
│ ML ETA correction    │ Use routing ETA      │ ETA is ~10-15% less  │
│                      │ without ML           │ accurate. Still      │
│                      │ correction.          │ functional.          │
├──────────────────────┼──────────────────────┼──────────────────────┤
│ Traffic data         │ Use historical       │ Traffic-unaware ETA. │
│ (real-time)          │ traffic patterns     │ Worse during unusual │
│                      │ for this time/day.   │ traffic events.      │
├──────────────────────┼──────────────────────┼──────────────────────┤
│ Payment gateway      │ Complete the trip.   │ Payment delayed.     │
│ (Stripe)             │ Queue payment for    │ Driver still gets    │
│                      │ retry. Rider can     │ paid (platform       │
│                      │ still request rides  │ absorbs risk).       │
│                      │ (credit-based).      │                      │
├──────────────────────┼──────────────────────┼──────────────────────┤
│ WebSocket gateway    │ Clients auto-        │ 2-5 second gap in    │
│ (partial)            │ reconnect. Critical  │ live tracking.       │
│                      │ events (ride offers) │ Ride offers still    │
│                      │ also sent via push   │ delivered (via push  │
│                      │ notifications.       │ notification).       │
├──────────────────────┼──────────────────────┼──────────────────────┤
│ Kafka (consumer lag) │ Real-time consumers  │ Surge pricing may be │
│                      │ fall behind. Direct  │ stale. Analytics     │
│                      │ RPCs used for        │ delayed. No trip     │
│                      │ latency-critical     │ impact (dispatch     │
│                      │ paths.               │ uses direct RPCs).   │
└──────────────────────┴──────────────────────┴──────────────────────┘

Key principle: The RIDE must still work, even if degraded.
  No service failure should prevent a rider from getting a ride
  (except dispatch or location service — those are critical path).
```

---

## 5. Multi-Region Architecture

```
Uber operates globally. The architecture must handle:
  • Riders in NYC connecting to nearby infrastructure
  • A driver in London serving local riders
  • A rider who travels from NYC to London and expects their
    account, payment methods, and ride history to be available

Architecture:

  ┌─────────────────┐   ┌─────────────────┐   ┌─────────────────┐
  │  US Region       │   │  EU Region       │   │  APAC Region    │
  │                  │   │                  │   │                 │
  │ Dispatch         │   │ Dispatch         │   │ Dispatch        │
  │ Location Service │   │ Location Service │   │ Location Service│
  │ Routing/ETA      │   │ Routing/ETA      │   │ Routing/ETA     │
  │ Surge Pricing    │   │ Surge Pricing    │   │ Surge Pricing   │
  │ Trip Service     │   │ Trip Service     │   │ Trip Service    │
  │ WebSocket GW     │   │ WebSocket GW     │   │ WebSocket GW    │
  │                  │   │                  │   │                 │
  │ Regional DB      │   │ Regional DB      │   │ Regional DB     │
  │ (trips, locations│   │ (trips, locations│   │ (trips, locations│
  │  driver state)   │   │  driver state)   │   │  driver state)  │
  └────────┬─────────┘   └────────┬─────────┘   └────────┬────────┘
           │                      │                       │
           └──────────────────────┼───────────────────────┘
                                  │
                    ┌─────────────▼──────────────┐
                    │   Global Services          │
                    │                            │
                    │   User Accounts            │
                    │   Payment Methods           │
                    │   Driver Profiles           │
                    │   Ride History (read-only)  │
                    │   Authentication / Auth     │
                    │                            │
                    │   Multi-region replicated   │
                    │   (consistent with conflict │
                    │    resolution)              │
                    └────────────────────────────┘

What's regional (not replicated across regions):
  • Trip data (a trip in NYC doesn't need to be in the London DC)
  • Driver locations (only relevant in the driver's current region)
  • Dispatch state (regional market)
  • Surge pricing (per-city computation)
  • Route/map data (per-region road network)

What's global (replicated):
  • User accounts (a user traveling internationally)
  • Payment methods (rider's credit card works worldwide)
  • Driver profiles (driver identity verification is global)
  • Ratings and ride history (viewable from any region)
  • Authentication tokens (sign in once, ride anywhere)

Routing users to the right region:
  • DNS-based routing (rider in NYC → US-East DC)
  • If rider travels to London → subsequent requests route to EU DC
  • Account data is already replicated there (global services)
  • Ride history from NYC is accessible from EU (read replicas)
```

---

## 6. Peak Event Handling

```
Peak events cause extreme, concentrated demand spikes:

  New Year's Eve:
    Demand at midnight: 5-10x normal for the hour
    Concentrated in: downtown areas, entertainment districts
    Duration: 30-60 minutes of extreme demand, then gradual decline

  Concert/sports game ending:
    Demand: 3,000-5,000 requests from a single venue in 15 minutes
    Concentrated in: within 500m of the venue exits
    Duration: 15-30 minute spike

  Severe weather (sudden rainstorm):
    Demand: 2-3x normal, spread across a city
    Duration: 1-2 hours (until weather clears)

Pre-scaling strategy:

  Known events (concerts, games, holidays):
    1. Pre-provision additional compute capacity
       (auto-scaling triggered by event calendar, not just load)
    2. Pre-warm caches (load venue area into geospatial index cache)
    3. Pre-position drivers (show surge predictions to drivers
       30 minutes before event ends — "higher earnings expected
       at Madison Square Garden at 10:30 PM")
    4. Pre-compute surge zones (anticipate the demand spike)
    5. Increased WebSocket gateway capacity for the area

  Dynamic scaling (unexpected spikes):
    1. Auto-scaling based on real-time metrics:
       • Ride request rate (requests/sec per region)
       • Dispatch latency (P99 approaching budget)
       • Location service query latency
       • Kafka consumer lag
    2. Scale-up latency: ~2-5 minutes for new instances
       (warm instances in standby pool reduce this to ~30 seconds)

  H3 cell splitting for dense events:
    Normal: surge computed at resolution 9 (~175m cells)
    Concert: cells around the venue may switch to resolution 10 (~66m cells)
    This prevents the surge from being averaged across a large area
    and allows more precise supply/demand measurement at the venue.
```

---

## 7. Chaos Engineering & Resilience

```
Uber has published about resilience testing practices:

Failure modes tested:
  1. Service instance crashes → circuit breaker kicks in, traffic
     routed to healthy instances, auto-restart
  2. Data center failure → traffic fails over to another DC
     within the same region (active-active if available)
  3. Kafka consumer lag → direct RPCs used for latency-critical
     paths, non-critical consumers catch up when Kafka recovers
  4. Database shard failure → replica promoted, reads served
     from replicas, writes blocked until promotion completes
  5. Redis cluster partial failure → geospatial index for affected
     region is stale → fallback to broader radius search or
     historical patterns

Circuit breaker pattern:
  If Service A calls Service B and gets >50% errors in 10 seconds:
    → Circuit opens: stop calling Service B
    → Use fallback (cached data, default values, degraded mode)
    → After 30 seconds: circuit half-opens (send a probe request)
    → If probe succeeds: circuit closes (resume normal calls)
    → If probe fails: circuit stays open, try again in 30 seconds

Bulkhead pattern:
  Dispatch Service has separate thread pools for:
    • UberX dispatch (highest priority, largest pool)
    • UberXL dispatch
    • UberBlack dispatch
    • Uber Pool dispatch

  If UberBlack dispatch is slow (rare vehicle type, complex matching),
  it doesn't consume threads from the UberX pool.
  UberX riders are unaffected by UberBlack slowness.

Retry with backoff:
  If a call to the routing service fails:
    Retry 1: after 100ms
    Retry 2: after 200ms
    Retry 3: after 400ms
    After 3 retries: fallback to straight-line estimate

  Jitter is added to retries to prevent thundering herd:
    actual_delay = base_delay × 2^attempt × (0.5 + random(0, 0.5))
```

---

## 8. Contrasts

### Uber vs Netflix Reliability

| Dimension | Uber | Netflix |
|---|---|---|
| **Failure mode** | "Can't get a ride" — rider physically stranded | "Video won't play" — viewer mildly annoyed |
| **Safety implication** | Yes (stranded late at night, miss a flight) | No |
| **Blast radius of outage** | Physical world (people can't travel) | Digital world (people can't watch) |
| **Availability target** | 99.99% for dispatch | 99.99% for streaming API |
| **Degradation strategy** | Fall back to simpler algorithms (straight-line ETA) | Fall back to cached/precomputed recommendations |
| **Data center failover** | Must be near-instant (rides in progress) | Can tolerate brief interruption (video buffers) |
| **State management** | Active trips have mutable, latency-critical state | Viewing state is low-value (resume position) |

### Uber vs Google Maps Reliability

| Dimension | Uber | Google Maps |
|---|---|---|
| **Failure impact** | Person can't travel | Person navigates by memory or asks for directions |
| **Real-time state** | Millions of active trips, moving drivers | No per-user active state (stateless navigation) |
| **Session statefulness** | High (trip state, dispatch state, payment state) | Low (navigation is mostly stateless client-side) |
| **Recovery complexity** | Must recover trip state, resume in-progress rides | Client reloads map, re-requests route |
| **Multi-party coordination** | Yes (rider, driver, platform) | No (single user) |

### Scaling Strategy Comparison

```
Uber vs typical web application:

  Typical web app (e-commerce, social media):
    • Scale concern: read/write throughput to the database
    • Solution: read replicas, caching (CDN, Redis), sharding
    • State: mostly static (user profiles, posts, products)
    • Real-time: optional (notifications, likes)

  Uber:
    • Scale concern: millions of moving points, real-time matching
    • Solution: geospatial index (in-memory, sharded by geography),
      event streaming (Kafka), specialized algorithms (CH for routing)
    • State: highly dynamic (driver locations change every 3-4 seconds)
    • Real-time: essential (dispatch, tracking, navigation)

  The fundamental difference:
    A typical web app serves mostly STATIC data (read a post,
    view a product). Caching works well because data doesn't change often.

    Uber serves DYNAMIC data that changes every few seconds.
    Caching is less effective because driver locations are stale
    within seconds. The system must continuously ingest, index,
    and query fast-moving data — this is the core scaling challenge.
```
