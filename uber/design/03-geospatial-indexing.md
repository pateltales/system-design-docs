# Geospatial Indexing — Location Tracking & Spatial Queries

> The geospatial index is the foundation of everything at Uber.
> Matching, ETA, surge pricing, and heat maps ALL depend on knowing where drivers are right now.
> Scale: ~1.5 million GPS updates per second, millions of moving points indexed in real-time.

---

## Table of Contents

1. [The Location Firehose](#1-the-location-firehose)
2. [Geospatial Indexing Options](#2-geospatial-indexing-options)
3. [H3 — Uber's Hexagonal Grid](#3-h3--ubers-hexagonal-grid)
4. [Driver Location Service Architecture](#4-driver-location-service-architecture)
5. [Nearest-Driver Query](#5-nearest-driver-query)
6. [Accuracy vs Freshness](#6-accuracy-vs-freshness)
7. [Contrasts](#7-contrasts)

---

## 1. The Location Firehose

Every active driver sends a GPS update every **3-4 seconds** while online. This is the heartbeat of the system.

### Scale Numbers

| Metric | Value | Confidence |
|---|---|---|
| Active drivers at peak | ~5 million globally | MEDIUM (Uber has stated "millions of drivers") |
| GPS update frequency | Every 3-4 seconds | HIGH (Uber Engineering blog) |
| Updates per second (peak) | ~1.25-1.67 million | DERIVED (5M ÷ 3-4 seconds) |
| Bytes per update | ~100 bytes | INFERRED (driverId + lat/lng + heading + speed + timestamp) |
| Raw data throughput | ~125-167 MB/sec | DERIVED |
| Updates per day | ~100-130 billion | DERIVED |
| Data per day | ~10-13 TB | DERIVED |

### Update Payload

```json
{
  "driverId": "drv-uuid-456",
  "lat": 40.748901,
  "lng": -73.985702,
  "heading": 270.5,        // degrees (0=north, clockwise)
  "speed": 12.5,            // m/s (~28 mph)
  "accuracy": 8.0,          // GPS accuracy radius in meters
  "timestamp": 1708437002345  // Unix epoch millis
}
```

### Why Every 3-4 Seconds?

```
At 30 mph (city driving), a driver moves:
  30 mph × 1609 m/mile ÷ 3600 sec = ~13.4 m/s

  In 3 seconds: ~40 meters
  In 4 seconds: ~54 meters
  In 10 seconds: ~134 meters
  In 30 seconds: ~402 meters

Trade-off:
  • More frequent (1 sec) → better accuracy, 3x more data, 3x more load
  • Less frequent (10 sec) → 134m between updates, driver could turn a corner
    and disappear from the spatial index's perspective
  • 3-4 seconds is the sweet spot: ~40-54m between updates is sufficient
    for matching (we need "nearby", not "exact position"), manageable data rate,
    acceptable battery impact on driver's phone
```

---

## 2. Geospatial Indexing Options

The system must answer one question billions of times per day: **"Which available drivers are near this location?"**

### Option A: Geohash (+ Redis/Sorted Set)

```
How it works:
  Encode (lat, lng) → alphanumeric string (e.g., "dr5ru7c")
  Nearby locations share prefixes: "dr5ru7" is a cell containing "dr5ru7c"
  Longer string = smaller cell = higher precision

  Precision levels:
    4 chars → ~39km × 19km cell
    5 chars → ~5km × 5km cell
    6 chars → ~1.2km × 600m cell
    7 chars → ~150m × 150m cell

  Range query: "Find all drivers in cell dr5ru7*"
    → Prefix scan on sorted index (Redis ZRANGEBYLEX)

  Adjacent cells: For a given cell, compute the 8 neighbors
    → Query all 9 cells (center + 8 neighbors) for completeness

Pros:
  • Simple to implement — just string operations
  • Works natively with Redis sorted sets
  • Widely adopted, well-understood

Cons:
  • Rectangular cells aligned to the coordinate grid
  • Edge effects: two points 1 meter apart but in different cells
    require querying adjacent cells (solved but adds complexity)
  • Cell sizes vary with latitude (cells near poles are distorted)
  • Non-uniform adjacency: 4 edge neighbors + 4 corner neighbors
    → corners are farther away than edges
```

### Option B: Quadtree

```
How it works:
  Recursively subdivide space into 4 quadrants
  When a cell has too many points, split into 4 children
  Dynamically adapts to density:
    Manhattan (dense) → many levels of subdivision → small cells
    Rural Iowa (sparse) → few levels → large cells

  ┌───────────┬───────────┐
  │           │     │     │
  │     NW    │ NE  │ NE' │   ← NE split because it has many drivers
  │           │     │     │
  ├───────────┼─────┼─────┤
  │           │           │
  │     SW    │     SE    │
  │           │           │
  └───────────┴───────────┘

Pros:
  • Adapts to density (efficient memory use)
  • Point query: O(log N)
  • Range query: traverse relevant subtrees

Cons:
  • Tree rebalancing under high update rates (millions/sec)
  • Not trivially distributable/shardable
  • In-memory only — hard to persist and replicate
  • Thread safety during concurrent updates requires locking or lock-free structures
```

### Option C: S2 Geometry (Google)

```
How it works:
  Projects Earth's surface onto a cube (6 faces)
  Applies a Hilbert curve to map 2D cell → 1D cell ID
  Hierarchical: 30 levels of resolution
    Level 12 → ~3.3km² cells
    Level 16 → ~0.05km² cells (50m × 50m)
    Level 20 → sub-meter cells

  Hilbert curve preserves locality: nearby cells on the sphere
  have nearby cell IDs → range queries on cell IDs find
  geographically nearby cells

Pros:
  • Mathematically rigorous (uniform coverage of the sphere)
  • Good locality preservation (Hilbert curve)
  • Hierarchical — easy to zoom in/out
  • Used by Google Maps, Google Earth

Cons:
  • Square cells (same adjacency issue as geohash)
  • More complex implementation than geohash
  • Requires S2 library (C++, Java, Go, Python)
```

### Option D: H3 — Uber's Hexagonal Grid ★

```
How it works:
  Divides Earth into hexagonal cells at 16 resolution levels
  Based on icosahedron projection (20-face polyhedron → unfold → hexagons)

  Resolution levels:
    Res 0  → ~4.3M km² (continental)
    Res 5  → ~253 km²
    Res 7  → ~5.2 km² (city district)
    Res 9  → ~0.1 km² (~105m edge, used for surge pricing)
    Res 12 → ~0.0003 km² (~9m edge)
    Res 15 → ~0.000001 km² (~0.5m edge)

  Key insight: HEXAGONS have UNIFORM ADJACENCY
    Every hexagon has exactly 6 neighbors, all equidistant

    Hexagon neighbors:        Square neighbors:
    ┌──┐                      ┌──┬──┬──┐
   / 1  \                     │ 1│ 2│ 3│
  /──────\──────\             ├──┼──┼──┤
  │  6   │center│  2   │      │ 4│ C│ 5│   ← corners (1,3,6,8) are
  \──────/──────/             ├──┼──┼──┤      ~1.41x farther than edges
   \  5  /                    │ 6│ 7│ 8│
    └──┘                      └──┴──┴──┘
  All 6 neighbors are equidistant     4 edge + 4 corner (non-uniform)
```

**VERIFIED — H3 is open-source on GitHub (uber/h3), announced in Uber Engineering blog 2018.**

---

## 3. H3 — Uber's Hexagonal Grid

### Why Uber Built H3

| Problem | Geohash/S2 Solution | H3 Solution |
|---|---|---|
| **Non-uniform adjacency** | Squares have 4 close + 4 diagonal neighbors (√2 farther) | Hexagons have 6 equidistant neighbors |
| **Range queries** | Must query 9 cells (center + 8 neighbors) to cover gaps | Query 7 cells (center + 6 neighbors) — more efficient |
| **Aggregation bias** | Rectangular cells create directional bias in aggregation | Hexagons minimize sampling bias (closer to circles) |
| **Surge pricing boundaries** | Rider walks 50m to cross a cell boundary and avoid surge | Hexagon boundaries are more natural — fewer "gaming the boundary" cases |
| **Distortion at scale** | Geohash cells distort significantly at high latitudes | Icosahedron projection minimizes distortion globally |

### H3 at Uber

H3 is used for:
- **Surge pricing**: Supply and demand aggregated per H3 cell (resolution 9, ~105m edge length). Surge multiplier computed per cell.
- **Demand forecasting**: ML models predict demand per H3 cell × time window. Used for driver positioning guidance (heat maps).
- **Market definition**: Cities divided into H3 regions for operational management.
- **Geofencing**: Airport zones, restricted areas, pricing zones defined as sets of H3 cells.
- **Analytics**: Trip data aggregated by H3 cell for business intelligence.

### H3 Operations

```
h3.geoToH3(lat, lng, resolution) → "892a100d2c3ffff"   // point → cell
h3.h3ToGeo("892a100d2c3ffff") → (lat, lng)             // cell → center point
h3.kRing("892a100d2c3ffff", 1) → [7 cells]             // cell + 6 neighbors
h3.kRing("892a100d2c3ffff", 2) → [19 cells]            // 2-ring (center + 2 layers)
h3.h3ToParent("892a100d2c3ffff", 7) → parent cell       // zoom out
h3.h3ToChildren("892a100d2c3ffff") → [7 child cells]   // zoom in
h3.h3Distance(cell1, cell2) → grid distance              // cell-to-cell distance
```

---

## 4. Driver Location Service Architecture

```
┌──────────────┐
│  Driver App   │
│  (GPS every   │
│   3-4 sec)    │
└──────┬───────┘
       │
       │  PUT /drivers/location
       ▼
┌──────────────────────────────────────────────────────────────────┐
│ Location Ingestion Service (horizontally scaled, stateless)      │
│                                                                  │
│ 1. Validate GPS data (lat/lng range, timestamp freshness)        │
│ 2. Map-match to nearest road segment (snap noisy GPS to road)    │
│ 3. Compute H3 cell ID at target resolution                      │
│ 4. Publish to Kafka topic: "driver-locations"                    │
│ 5. Update in-memory Geospatial Index (local shard)               │
└──────────────────────┬───────────────────────────────────────────┘
                       │
          ┌────────────┼────────────────┐
          ▼            ▼                ▼
┌─────────────┐ ┌─────────────┐ ┌──────────────┐
│  Kafka      │ │ Geospatial  │ │  Cassandra    │
│  (durability│ │ Index       │ │  (location    │
│   + fanout  │ │ (in-memory, │ │   history,    │
│   to other  │ │  sharded by │ │   analytics)  │
│   consumers)│ │  city/region)│ │               │
└─────────────┘ └─────────────┘ └──────────────┘
       │
       ├── Surge Pricing Consumer (aggregate supply per H3 cell)
       ├── Analytics Consumer (write to data lake)
       ├── ETA Service Consumer (real-time traffic estimation)
       └── Safety Consumer (speed alerts, route deviation)
```

### Sharding the Geospatial Index

The in-memory spatial index is **sharded by geographic region** (city or metropolitan area):

```
┌─────────────────────────────────────────────────────┐
│ Geospatial Index Shards                              │
│                                                      │
│ Shard: "new-york"                                    │
│   Drivers in NYC metro area                          │
│   ~50,000 active drivers at peak                     │
│   Index: H3 cell → Set[driverId]                     │
│   Memory: ~50K drivers × 100 bytes = ~5MB            │
│                                                      │
│ Shard: "san-francisco"                               │
│   Drivers in SF Bay Area                             │
│   ~30,000 active drivers at peak                     │
│                                                      │
│ Shard: "london"                                      │
│   Drivers in London metro area                       │
│   ~40,000 active drivers at peak                     │
│                                                      │
│ ...thousands of city/region shards globally           │
│                                                      │
│ Benefits:                                            │
│   • Natural load balancing (Manhattan is dense, Iowa  │
│     is sparse — each gets appropriately sized shard)  │
│   • Queries are always local (find drivers near       │
│     pickup = query ONE shard — the city shard)        │
│   • Independent scaling per city                      │
│   • Failure isolation (NYC shard failure doesn't      │
│     affect London)                                    │
└─────────────────────────────────────────────────────┘
```

### Index Data Structure

```
Primary index: H3 Cell → Set of Driver IDs

  cell "892a100d2c3ffff" → { drv-001, drv-042, drv-099 }
  cell "892a100d2c7ffff" → { drv-003, drv-017 }
  ...

Secondary index: Driver ID → Current Location + Metadata

  drv-001 → {
    lat: 40.7490, lng: -73.9860,
    heading: 270, speed: 12.5,
    h3Cell: "892a100d2c3ffff",
    status: AVAILABLE,
    vehicleType: UberX,
    rating: 4.92,
    lastUpdated: 1708437002345
  }

Update operation (on each GPS update):
  1. Look up driver's previous H3 cell from secondary index
  2. If cell changed:
     a. Remove driverId from old cell's set
     b. Add driverId to new cell's set
  3. Update secondary index with new location + metadata

  Complexity: O(1) amortized (hash map lookups)
  Most updates: driver stays in the same cell (cell is ~105m wide)
```

---

## 5. Nearest-Driver Query

The dispatch system asks: "Find available drivers near (lat, lng) for UberX."

```
Query: nearestDrivers(lat=40.7484, lng=-73.9857, type=UberX, limit=20)

Step 1: Compute H3 cell for the pickup location
  h3Cell = h3.geoToH3(40.7484, -73.9857, resolution=9)
  → "892a100d2c3ffff"

Step 2: Get the cell + neighbors (k-ring with k=1)
  cells = h3.kRing("892a100d2c3ffff", k=1)
  → 7 cells (center + 6 neighbors)

Step 3: Fetch all drivers from these 7 cells
  candidates = []
  for cell in cells:
    candidates += index[cell]  // O(1) per cell

Step 4: Filter candidates
  available = candidates.filter(d =>
    d.status == AVAILABLE &&
    d.vehicleType == UberX
  )

Step 5: Sort by distance to pickup
  available.sortBy(d => haversineDistance(d.location, pickup))

Step 6: Return top N
  return available[:20]

Performance:
  Steps 1-2: ~1 microsecond (H3 computation)
  Step 3: ~10 microseconds (7 hash map lookups)
  Steps 4-5: ~100 microseconds (filter + sort ~50-200 candidates)
  Total: <1 millisecond

If not enough candidates found:
  Expand to k=2 (19 cells), k=3 (37 cells), etc.
  Each ring adds ~6k cells at the periphery
```

### Why Route-Based ETA, Not Straight-Line Distance?

```
Scenario: Two drivers, both 500m from pickup

  Driver A: 500m straight line, ON THE SAME ROAD heading toward pickup
    → Route-based ETA: 1 minute

  Driver B: 500m straight line, ACROSS A RIVER with no bridge nearby
    → Route-based ETA: 15 minutes (must drive around the river)

If we sort by straight-line distance, A and B look equivalent.
Route-based ETA correctly identifies A as the much better match.

The dispatch system uses the geospatial index for CANDIDATE GENERATION
(find drivers within a radius) and then the ROUTING SERVICE for
CANDIDATE RANKING (compute actual ETA for each candidate).
```

---

## 6. Accuracy vs Freshness

### GPS Accuracy Limitations

```
GPS accuracy: ±5-10 meters (good conditions), ±20-50m (urban canyons)

Urban canyon problem:
  In Manhattan, tall buildings block/reflect GPS signals
  Driver's GPS may report position on the wrong street
  → Map matching (snap GPS to nearest road) is essential

Between updates:
  GPS arrives every 3-4 seconds
  At 30 mph, driver moves ~40-54m between updates
  The spatial index is always ~2 seconds stale

  For matching: This is acceptable — we're finding NEARBY drivers,
  not pinpointing exact positions. A 50m error doesn't affect
  whether a driver 500m away is a good match.

  For rider-facing tracking: We interpolate between updates.
  Client-side: animate the driver icon smoothly between GPS points
  using heading + speed for prediction. When the next update arrives,
  correct the position.
```

### Stale Data Handling

```
What if a driver's GPS stops updating? (phone dies, enters tunnel)

  Policy: If no GPS update for >30 seconds, mark driver as STALE
  STALE drivers are excluded from matching candidates
  If no update for >5 minutes, mark driver as OFFLINE

  The secondary index tracks lastUpdated timestamp
  A background sweep runs every 10 seconds to find stale drivers
```

---

## 7. Contrasts

### Uber vs Google Maps — Spatial Data

| Dimension | Uber | Google Maps |
|---|---|---|
| **Data type** | Dynamic (millions of moving points) | Mostly static (roads, POIs, businesses) |
| **Update rate** | GPS every 3-4 seconds per driver | Road network updated daily/weekly |
| **Query type** | "Find nearest N moving drivers" | "Find businesses near a location" |
| **Index type** | In-memory, real-time updated (H3/S2) | Persistent spatial index (S2), updated in batch |
| **Freshness** | Seconds (stale after 30s) | Days (stale after road network changes) |
| **Scale of moving entities** | ~5M active drivers | ~0 (static data) |
| **Challenge** | High-velocity updates on moving points | High-volume static data at global scale |

### Geohash vs H3 vs S2

| Dimension | Geohash | S2 (Google) | H3 (Uber) |
|---|---|---|---|
| **Cell shape** | Rectangle | Square (on cube face) | Hexagon |
| **Adjacency** | 8 neighbors (non-uniform) | 8 neighbors (non-uniform) | 6 neighbors (uniform) |
| **Projection** | Lat/lng grid | Cube + Hilbert curve | Icosahedron |
| **Distortion** | Severe at poles | Moderate (cube faces) | Minimal (icosahedron) |
| **Hierarchy** | Prefix-based (string) | Binary subdivision | Aperture 7 (7 children) |
| **Implementation** | Simple (string ops) | Complex (S2 library) | Moderate (H3 library) |
| **Best for** | Simple geofencing, caching | General-purpose spatial | Spatial aggregation, analytics |
| **Open-source** | Many implementations | Google S2 library | Uber H3 library |

### Uber vs Food Delivery — Spatial Index

| Dimension | Uber | DoorDash / Uber Eats |
|---|---|---|
| **Moving entities** | ~5M drivers | ~1M couriers (fewer) |
| **Static entities** | None (riders are point queries, not indexed) | Restaurants (indexed as static points) |
| **Index type** | Dynamic-only (drivers move) | Hybrid: dynamic (couriers) + static (restaurants) |
| **Update rate** | Same (~3-4 sec GPS) | Same for couriers, rare for restaurants |
| **Query** | "Nearest available drivers" | "Nearest available courier AND nearest restaurant that serves this food" |
| **Matching** | 2-party (rider ↔ driver) | 3-party (customer ↔ restaurant ↔ courier) |
