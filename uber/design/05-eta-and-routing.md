# ETA Computation & Routing — The Most Queried Value

> ETA is the most frequently computed value in the Uber system.
> Displayed to riders, used for matching, used for fare estimation, shown on the driver's navigation.
> Target: <100ms per query at millions of QPS.

---

## Table of Contents

1. [ETA Computation Pipeline](#1-eta-computation-pipeline)
2. [Road Network Graph](#2-road-network-graph)
3. [Contraction Hierarchies](#3-contraction-hierarchies)
4. [Traffic-Aware ETA](#4-traffic-aware-eta)
5. [Map Matching](#5-map-matching)
6. [ML-Based ETA Correction](#6-ml-based-eta-correction)
7. [Live Navigation & Rerouting](#7-live-navigation--rerouting)
8. [Contrasts](#8-contrasts)

---

## 1. ETA Computation Pipeline

```
Query: ETA from driver at (40.749, -73.986) to rider at (40.748, -73.986)
    │
    ▼
┌─────────────────────────────────────────────────────────────┐
│ STEP 1: Map-Match Input Coordinates                          │
│                                                              │
│ Raw GPS is noisy (±5-10m). Snap to nearest road segment.    │
│ Driver at (40.749, -73.986) → snapped to 5th Ave, heading S │
│ Rider at (40.748, -73.986) → snapped to 34th St & 5th Ave  │
│                                                              │
│ Latency: ~1ms                                                │
└──────────────────────┬──────────────────────────────────────┘
                       │
                       ▼
┌─────────────────────────────────────────────────────────────┐
│ STEP 2: Shortest Path Query (Contraction Hierarchies)        │
│                                                              │
│ Query the preprocessed road graph for shortest-TIME path.    │
│ Not shortest distance — a highway may be longer in meters    │
│ but faster in minutes than a congested city street.          │
│                                                              │
│ Edge weights: base travel time (from speed limit + road type)│
│ Adjusted by: real-time traffic multiplier per segment        │
│                                                              │
│ Result: path geometry + base ETA                             │
│ Latency: ~5-10ms (Contraction Hierarchies)                   │
└──────────────────────┬──────────────────────────────────────┘
                       │
                       ▼
┌─────────────────────────────────────────────────────────────┐
│ STEP 3: Traffic Adjustment                                   │
│                                                              │
│ Apply real-time traffic data to edge weights along the path. │
│ Traffic data from: driver GPS traces (aggregate speed per    │
│ road segment, updated every ~1-2 minutes).                   │
│                                                              │
│ If segment S normally takes 30 seconds but current traffic   │
│ shows average speed is 50% of normal → adjust to 60 seconds.│
│                                                              │
│ Latency: ~2-5ms (lookup per segment along path)              │
└──────────────────────┬──────────────────────────────────────┘
                       │
                       ▼
┌─────────────────────────────────────────────────────────────┐
│ STEP 4: ML Correction                                        │
│                                                              │
│ ML model applies a correction factor based on:               │
│  • Historical patterns (this route at this time of day/week) │
│  • Weather (rain/snow slows traffic)                         │
│  • Special events (concert ending, game day)                 │
│  • Residual errors from routing (turn delays, traffic lights)│
│                                                              │
│ correctedETA = routingETA × mlCorrectionFactor               │
│                                                              │
│ Latency: ~2-5ms (model inference)                            │
└──────────────────────┬──────────────────────────────────────┘
                       │
                       ▼
Result: ETA = 180 seconds (3 minutes), confidence = 0.85
Total latency: ~10-20ms
```

---

## 2. Road Network Graph

The world's road network as a weighted directed graph.

```
Graph structure:
  Nodes (vertices): intersections, road endpoints
  Edges: road segments between intersections
  Edge properties:
    • length (meters)
    • speed limit (km/h or mph)
    • road type (highway, arterial, residential, one-way)
    • base travel time = length / speed_limit
    • turn restrictions (can't turn left from edge A to edge B)
    • traffic multiplier (real-time, 1.0 = normal, 2.0 = twice as slow)

Example:
  Node 1 (34th & 5th) ──[5th Ave, 300m, 40km/h, one-way south]──> Node 2 (33rd & 5th)
  Node 1 (34th & 5th) ──[34th St, 200m, 30km/h, one-way east]──> Node 3 (34th & Madison)

  Base travel time for edge 1→2: 300m ÷ (40km/h ÷ 3.6) = 27 seconds
  With traffic multiplier 1.5: 27 × 1.5 = 40.5 seconds

Scale:
  USA road network: ~24 million nodes, ~58 million edges
  Global: ~hundreds of millions of nodes
  Graph size in memory: ~10-50 GB (compressed adjacency list)
```

### Data Sources

| Source | Type | Quality | Update Frequency |
|---|---|---|---|
| **OpenStreetMap (OSM)** | Open, crowd-sourced | Good globally, excellent in cities | Continuous (volunteers) |
| **Proprietary corrections** | Uber's driver GPS traces | Excellent on ride-shared routes | Real-time (aggregated from millions of daily traces) |
| **Government data** | Road classifications, speed limits | Authoritative | Infrequent (annual) |

Uber uses OSM as the base and applies corrections from driver GPS traces: detect new roads, road closures, actual average speeds (which may differ from posted speed limits), and turn timing.

---

## 3. Contraction Hierarchies

**Dijkstra's algorithm** finds shortest paths but is too slow for real-time queries on a graph with millions of nodes.

```
Dijkstra on a 24M-node graph:
  Time: ~1-5 seconds per query
  QPS: ~1

Target: <10ms per query, millions of QPS
  → Need 1000x speedup over Dijkstra
```

### How Contraction Hierarchies (CH) Work

**VERIFIED — Contraction Hierarchies are a well-known algorithm (Geisberger et al., 2008). Uber Engineering blog mentions using CH for routing.**

```
PREPROCESSING (done once, takes hours):

1. Order all nodes by "importance"
   (importance = road class, number of connections, centrality)
   Highways > arterials > residential streets

2. "Contract" nodes from least to most important:
   For each node v being contracted:
     For each pair of neighbors (u, w) where u→v→w is a shortest path:
       Add a SHORTCUT edge u→w with weight = weight(u→v) + weight(v→w)
     Remove v from the active graph

   Example: Node v is on a residential street between u and w
   Contract v: add shortcut u→w (weight = u→v + v→w)
   The shortcut represents "go from u to w via v" but skips v in the graph

3. Result: augmented graph with original edges + shortcuts
   The graph is organized in layers:
     Bottom: residential streets (contracted first)
     Middle: arterial roads
     Top: highways (contracted last, most "important")

QUERY (done millions of times, <10ms each):

1. Run BIDIRECTIONAL search:
   Forward search from origin (going UP the hierarchy)
   Backward search from destination (going UP the hierarchy)

2. Both searches only traverse UPWARD edges (toward more important nodes)
   They meet at a high-importance node (usually a highway)

3. The path through the hierarchy is unpacked by expanding shortcuts
   back to the original road segments

Why it's fast:
  Dijkstra explores all directions equally → touches millions of nodes
  CH searches UPWARD only → touches thousands of nodes
  Meeting at the "top" of the hierarchy is like:
    "Drive from local street to highway, take highway, exit to local street"
  The hierarchical structure naturally models how people drive
```

### Performance

| Operation | Dijkstra | Contraction Hierarchies |
|---|---|---|
| **Preprocessing** | None | 1-4 hours (one-time, per graph update) |
| **Query time** | 1-5 seconds | **<10 milliseconds** |
| **Nodes explored per query** | Millions | ~1,000-5,000 |
| **Memory** | O(V + E) | O(V + E + shortcuts) — ~2x original graph |
| **Update traffic** | Edge weight change → free | Need to re-contract OR use Customizable CH |

### Customizable Contraction Hierarchies (CCH)

Standard CH needs re-preprocessing when edge weights change (traffic). This takes hours — not suitable for real-time traffic.

**Customizable CH** separates the hierarchy structure from the edge weights:
1. Preprocess the hierarchy topology once (hours)
2. When traffic changes, update ONLY the weights in the hierarchy (seconds)
3. Queries use the new weights immediately

This allows real-time traffic integration without re-preprocessing the entire graph.

---

## 4. Traffic-Aware ETA

### Traffic Data from Driver GPS Traces

```
Millions of Uber drivers driving every day generate a dense
traffic signal on ride-share-relevant roads.

Pipeline:
  1. Driver GPS update arrives (every 3-4 seconds)
  2. Map-match: snap GPS to road segment ID
  3. Compute speed on that segment:
     speed = distance_between_consecutive_GPS_points / time_between_them
  4. Aggregate: for each road segment, compute:
     • Average speed (moving average over last 5 minutes)
     • Sample count (how many drivers traversed this segment recently)
  5. Traffic multiplier = base_speed / current_average_speed
     If multiplier = 1.0: traffic is normal
     If multiplier = 2.0: traffic is twice as slow as normal

Storage:
  Key: (roadSegmentId, timeWindow)
  Value: { avgSpeed, sampleCount, multiplier }
  Updated: every 1-2 minutes
  Store: Redis (real-time) + Cassandra (historical)
```

### Historical Traffic Patterns

```
Real-time traffic alone is insufficient:
  • New road segment with no recent Uber drivers → no real-time data
  • Predicted traffic 15 minutes ahead → need historical patterns

Historical model:
  For each road segment, store:
    avgSpeed[dayOfWeek][hourOfDay][5minBucket]

  Example: 5th Ave between 34th and 33rd
    Monday 8:00 AM: 15 km/h (rush hour, slow)
    Monday 2:00 PM: 35 km/h (off-peak, fast)
    Saturday 2:00 PM: 25 km/h (weekend shopping, moderate)

  When real-time data is unavailable, fall back to historical:
    currentSpeed = realTimeSpeed ?? historicalSpeed[dow][hour][bucket]
```

---

## 5. Map Matching

GPS traces are noisy. Raw GPS coordinates don't align to roads. **Map matching** snaps GPS points to the most likely road segment.

```
Problem:
  Driver GPS reports (40.7490, -73.9860) with accuracy ±8m
  There are 3 road segments within 8 meters:
    • 5th Ave (heading south)
    • 34th St (heading east)
    • A pedestrian path (not driveable)

  Which road is the driver actually on?

Solution: Hidden Markov Model (HMM)

  States: road segments near the GPS point
  Observations: GPS coordinates
  Transition probabilities: based on road connectivity
    (if previous state was 5th Ave heading south,
     transition to 34th St heading east requires a turn —
     possible but less likely if heading hasn't changed)
  Emission probabilities: based on distance from GPS to road
    (closer road segment = higher probability)

  Viterbi algorithm: find the most likely SEQUENCE of road segments
  given the sequence of GPS observations

  Input:  [GPS₁, GPS₂, GPS₃, GPS₄, ...]
  Output: [5th Ave, 5th Ave, 5th Ave, 34th St, ...]
          (driver was on 5th Ave, then turned onto 34th St)
```

### Why Map Matching Matters

1. **Accurate fare calculation**: Trip distance is computed from the map-matched route, not raw GPS. GPS noise could inflate distance → overcharging.
2. **Correct driver display**: Show the driver icon on the correct road on the rider's map, not floating in a building.
3. **Traffic estimation**: Aggregate driver speeds per road segment. Without map matching, a driver on 5th Ave might be attributed to 34th St → wrong traffic data.
4. **Turn detection**: Detect when a driver turns → update navigation instructions.

---

## 6. ML-Based ETA Correction

Routing alone (CH + traffic) gives a good ETA but misses several factors:

```
Factors NOT captured by routing:
  • Traffic light timing (varies by intersection, time of day)
  • Turn delays (left turn across traffic takes longer)
  • Pickup logistics (driver needs to find rider, pull over, rider walks to car)
  • Weather effects (rain → slower driving, snow → much slower)
  • Special events (concert ending → sudden traffic spike)
  • Construction zones (not always in the road graph)
  • Historical bias (this route is consistently 10% slower than the graph predicts)

ML model:
  Input features:
    • Routing engine ETA (baseline)
    • Route length and segment count
    • Time of day, day of week
    • Weather conditions (API from weather service)
    • Number of turns, number of traffic lights (estimated from road graph)
    • Historical accuracy for this route (past predictions vs actual)
    • Special event indicators (from events calendar)

  Output:
    correctionFactor (e.g., 1.15 → add 15% to routing ETA)

  Training data:
    Billions of historical trips with:
      • Predicted ETA (from routing engine at time of request)
      • Actual trip duration
    → Train model to predict the residual: actual / predicted

  Deployed on Michelangelo (Uber's ML platform):
    Model retrained daily on latest trip data
    Inference: ~2-5ms per prediction
    Accuracy improvement: ~10-15% over routing-only ETA
```

---

## 7. Live Navigation & Rerouting

Once a trip starts, the driver follows turn-by-turn navigation. ETA updates in real-time.

```
During trip:
  Every 3-4 seconds (on each GPS update):
    1. Map-match current position to road segment
    2. Compute remaining route (from current position to dropoff)
    3. Apply real-time traffic to remaining segments
    4. Update ETA displayed to rider and driver

  If traffic conditions change significantly:
    • Recompute full route from current position
    • If new route is >2 minutes faster: suggest reroute to driver
    • Driver can accept or ignore the reroute

  Rerouting is conservative:
    • Don't reroute for <1 minute savings (annoying for driver)
    • Don't reroute if driver is already past the divergence point
    • Don't reroute onto unfamiliar small streets (driver comfort)
```

---

## 8. Contrasts

### Uber Routing vs Google Maps Routing

| Dimension | Uber | Google Maps |
|---|---|---|
| **Use case** | Driver-to-rider ETA, trip ETA | General-purpose navigation |
| **Optimization** | Time (fastest, not shortest distance) | Time (default), distance, or scenic |
| **Traffic source** | Driver GPS traces (millions/day, dense on ride-share roads) | Android phones (billions, broad coverage on all roads) |
| **Pickup-specific** | Consider which side of the road the rider is on; avoid illegal U-turns for pickup | Not pickup-aware — routes to the address generically |
| **Latency** | <10ms (CH, cached, millions of QPS) | ~50-200ms (public API, rate-limited) |
| **Coverage** | Excellent on ride-share-relevant roads; sparse on rural/residential | Excellent globally (every road) |
| **Cost** | Free (own infrastructure) | $5-10 per 1000 queries (Google Maps API pricing) |

### Routing Algorithm Comparison

| Algorithm | Preprocess Time | Query Time | Handles Traffic | Memory |
|---|---|---|---|---|
| **Dijkstra** | None | 1-5 seconds | Yes (reweight edges) | O(V+E) |
| **A\*** | None | 0.5-2 seconds | Yes | O(V+E) |
| **Contraction Hierarchies** | 1-4 hours | **<10ms** | With CCH variant | ~2x O(V+E) |
| **A\* + landmarks (ALT)** | Minutes | 50-200ms | Yes | O(V+E+landmarks) |
| **Transit Node Routing** | Hours | **<1ms** | Limited | ~3x O(V+E) |

### Uber ETA vs Food Delivery ETA

| Dimension | Uber ETA | DoorDash/Uber Eats ETA |
|---|---|---|
| **Components** | Travel time only | Restaurant prep time + travel time |
| **Uncertainty** | Low (travel time is predictable with traffic data) | High (prep time depends on order complexity, kitchen load, restaurant speed) |
| **Dominant factor** | Travel time (100% of ETA) | Prep time (often 60-80% of total ETA) |
| **ML challenge** | Predict traffic and turn delays | Predict restaurant prep time (harder — less structured data) |
| **Update frequency** | Every few seconds (GPS-based) | Less frequent (prep time doesn't update as smoothly) |
