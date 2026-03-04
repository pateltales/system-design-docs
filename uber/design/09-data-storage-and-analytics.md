# Data Storage, Analytics & ML — The Data Backbone

> Uber generates massive amounts of data: every GPS update, every trip event,
> every fare calculation, every search query.
> This data powers operations (dispatch, routing, pricing), analytics
> (business intelligence, regulatory reporting), and ML models
> (ETA prediction, fraud detection, surge forecasting).

---

## Table of Contents

1. [Storage Systems Overview](#1-storage-systems-overview)
2. [MySQL / Schemaless / Docstore](#2-mysql--schemaless--docstore)
3. [Cassandra](#3-cassandra)
4. [Redis](#4-redis)
5. [Kafka — The Backbone](#5-kafka--the-backbone)
6. [Data Lake & Batch Analytics](#6-data-lake--batch-analytics)
7. [Michelangelo — ML Platform](#7-michelangelo--ml-platform)
8. [ML Use Cases at Uber](#8-ml-use-cases-at-uber)
9. [Contrasts](#9-contrasts)

---

## 1. Storage Systems Overview

```
Right tool for the right access pattern:

┌──────────────────────┬────────────────────────┬───────────────────────┐
│ Data Type            │ Storage System         │ Why This System?      │
├──────────────────────┼────────────────────────┼───────────────────────┤
│ User accounts,       │ MySQL / Schemaless     │ Transactional,        │
│ trip records,        │                        │ consistent, queryable │
│ payment records      │                        │                       │
├──────────────────────┼────────────────────────┼───────────────────────┤
│ Driver location      │ Cassandra              │ High-write throughput │
│ history, trip event  │                        │ (millions of writes/  │
│ logs, activity feeds │                        │ sec), linear scaling  │
├──────────────────────┼────────────────────────┼───────────────────────┤
│ Real-time state:     │ Redis                  │ Sub-millisecond reads │
│ geospatial index,    │                        │ in-memory, geospatial │
│ dispatch state,      │                        │ commands, pub/sub     │
│ rate limiting,       │                        │                       │
│ session data         │                        │                       │
├──────────────────────┼────────────────────────┼───────────────────────┤
│ Event streams:       │ Kafka                  │ High-throughput       │
│ location updates,    │                        │ streaming, multiple   │
│ trip events,         │                        │ consumers, replay,    │
│ pricing events       │                        │ durable              │
├──────────────────────┼────────────────────────┼───────────────────────┤
│ Raw data for         │ S3 / HDFS              │ Cheap storage for    │
│ analytics & ML       │                        │ petabytes of data    │
│ training             │                        │                       │
├──────────────────────┼────────────────────────┼───────────────────────┤
│ Search: trip lookup, │ Elasticsearch          │ Full-text search,    │
│ driver search,       │                        │ aggregations, log    │
│ log aggregation      │                        │ analytics            │
├──────────────────────┼────────────────────────┼───────────────────────┤
│ Batch analytics      │ Hive / Presto / Spark  │ SQL on data lake,    │
│ queries              │                        │ large-scale joins,   │
│                      │                        │ ML training data     │
└──────────────────────┴────────────────────────┴───────────────────────┘
```

---

## 2. MySQL / Schemaless / Docstore

**VERIFIED — Uber Engineering Blog: "Designing Schemaless, Uber Engineering's Scalable Datastore Using MySQL"**

### Why MySQL?

```
Uber chose MySQL as the foundation for transactional data because:
  • Well-understood, battle-tested RDBMS
  • Excellent tooling (backup, monitoring, replication)
  • Strong consistency guarantees (ACID transactions)
  • Huge talent pool (every engineer knows SQL)

But MySQL alone doesn't scale to Uber's write volume.
  Solution: Schemaless — a sharding layer on top of MySQL.
```

### Schemaless Architecture

```
Schemaless is Uber's custom sharding layer over MySQL.

Key design:
  1. Data stored as JSON blobs in MySQL (hence "schemaless")
     → Application defines the schema, not the database
     → Schema changes don't require ALTER TABLE (no downtime)

  2. Automatic sharding by a shard key (usually entity ID)
     → Trip data sharded by tripId
     → User data sharded by userId
     → Consistent hashing determines which MySQL shard holds each key

  3. Write buffering:
     → Writes go to a write buffer first, then async to MySQL
     → Provides write absorption for burst traffic

  4. Change Data Capture (CDC):
     → Every write to Schemaless generates a change event
     → Events published to Kafka
     → Downstream consumers (analytics, search, cache invalidation)
       process changes asynchronously

  5. Eventually-consistent secondary indexes:
     → Primary key lookup is strongly consistent (direct shard query)
     → Secondary indexes are maintained asynchronously via CDC
     → Secondary index queries may lag by milliseconds

Data model:
  ┌────────────────────────────────────────────┐
  │ Table: trips                                │
  │                                             │
  │ Primary key: tripId (UUID, shard key)       │
  │ Columns:                                    │
  │   tripId:     UUID                          │
  │   data:       JSON blob {                   │
  │                 riderId, driverId,           │
  │                 pickup, dropoff,             │
  │                 status, fare, events[...]    │
  │               }                             │
  │   created_at: timestamp                     │
  │   updated_at: timestamp                     │
  │                                             │
  │ Secondary indexes (eventually consistent):  │
  │   riderId → [tripId, tripId, ...]           │
  │   driverId → [tripId, tripId, ...]          │
  │   status + created_at → [tripId, ...]       │
  └────────────────────────────────────────────┘

Query patterns:
  • Get trip by ID: O(1) — direct shard lookup
  • Get trips by rider: secondary index → list of tripIds → fan-out reads
  • Get active trips by driver: secondary index (status=IN_PROGRESS + driverId)
```

### Docstore (Evolution of Schemaless)

```
Docstore is the evolution of Schemaless — a document-oriented
storage layer that provides:
  • Richer query capabilities (partial document updates, projections)
  • Better indexing (composite indexes, range queries)
  • Improved multi-region replication
  • Unified API across storage backends

[PARTIALLY VERIFIED — Uber has referenced Docstore in engineering talks
 but detailed architecture is not fully public]
```

---

## 3. Cassandra

```
Cassandra is used for high-write-throughput, time-series-like data.

Use cases at Uber:
  1. Driver location history
     • Partition key: driverId
     • Clustering key: timestamp
     • Write: ~1.5M writes/sec (one per GPS update)
     • Read: "Get driver's last 30 minutes of locations" (range query)

  2. Trip event logs (event sourcing backing store)
     • Partition key: tripId
     • Clustering key: timestamp
     • Write: every state transition event
     • Read: "Get all events for trip X" (replay for fare computation)

  3. Surge pricing history
     • Partition key: (h3CellId, date)
     • Clustering key: timestamp
     • Write: surge multiplier snapshot every 1-2 minutes
     • Read: "What was the surge at this location at this time?"

Why Cassandra for these patterns?
  • Linear write scaling: add nodes → more write throughput
  • Tunable consistency: eventual consistency is acceptable for
    location history and analytics
  • Multi-DC replication: built-in (important for global operations)
  • Time-series friendly: clustering key by timestamp → efficient
    range scans for "last N minutes" queries
  • TTL support: auto-expire old data (GPS history older than 90 days)

Why NOT Cassandra for trips/payments?
  • No transactions (can't atomically update trip + payment)
  • No strong consistency (need read-your-write for trip state)
  • No secondary indexes that are practical at scale
  → MySQL/Schemaless for transactional data, Cassandra for analytics data
```

---

## 4. Redis

```
Redis is used for all real-time, latency-critical state.

Use cases:
  1. Geospatial index (driver locations)
     • GEOADD / GEORADIUS commands for nearby driver search
     • Key: h3_cell_id → Set of driverIds with locations
     • Updated on every GPS update (~1.5M writes/sec)
     • Queried on every ride request ("find nearby drivers")

  2. Dispatch state
     • Current ride offers: offerId → {driverId, riderId, expiry}
     • Driver status: driverId → {status, currentTripId, lastOffer}
     • TTL on offers (15 seconds)

  3. Rate limiting
     • Rider request rate: riderId → request count (sliding window)
     • API rate limits: apiKey → request count
     • Implemented with Redis INCR + EXPIRE (sliding window counter)

  4. WebSocket connection routing
     • userId → {gatewayInstanceId, connectionId}
     • Updated on connect/disconnect
     • Queried when pushing messages to a user

  5. Session and cache
     • Auth session tokens
     • Pricing cache (cached fare estimates)
     • Surge multiplier cache (per H3 cell)

Redis deployment:
  • Cluster mode (sharded across many nodes)
  • Separate clusters for different use cases
    (geospatial cluster, dispatch cluster, cache cluster)
  • Memory: in-memory only (no persistence for geospatial data —
    it's regenerated from GPS updates within seconds of restart)
  • Persistence: enabled for dispatch state and rate limiting
    (Redis AOF for crash recovery)
```

---

## 5. Kafka — The Backbone

**VERIFIED — Uber Engineering Blog: "Uber's Real-Time Push Platform", "Building Reliable Reprocessing and Dead Letter Queues with Apache Kafka"**

```
Kafka is the central nervous system of Uber's data infrastructure.

Scale:
  • TRILLIONS of messages per day [VERIFIED]
  • Thousands of topics
  • Multiple Kafka clusters (per region, per use case)
  • One of the largest Kafka deployments in the world

Key topics:
  ┌──────────────────────────────────┬──────────────────────┐
  │ Topic                            │ Volume               │
  ├──────────────────────────────────┼──────────────────────┤
  │ driver.location.updates          │ ~1.5M msg/sec        │
  │ trip.events                      │ ~500K msg/sec        │
  │ pricing.events                   │ ~100K msg/sec        │
  │ payment.events                   │ ~50K msg/sec         │
  │ rider.engagement.events          │ ~200K msg/sec        │
  │ safety.events                    │ ~10K msg/sec         │
  │ analytics.page.views             │ ~1M msg/sec          │
  └──────────────────────────────────┴──────────────────────┘

Architecture:
  Producers:                    Kafka Cluster:           Consumers:
  ┌────────────┐               ┌──────────────┐        ┌────────────────┐
  │ Location   │──────────────>│              │───────>│ Geospatial     │
  │ Service    │               │  driver.     │───────>│ Index          │
  │            │               │  location.   │───────>│ Surge Pipeline │
  └────────────┘               │  updates     │───────>│ Analytics      │
  ┌────────────┐               │              │───────>│ ETA Training   │
  │ Trip       │──────────────>│  trip.events │───────>│ Payment Svc    │
  │ Service    │               │              │───────>│ Analytics      │
  │            │               │              │───────>│ Safety Monitor │
  └────────────┘               └──────────────┘        └────────────────┘

Why Kafka as the backbone?

  1. DECOUPLING
     Location Service doesn't need to know who consumes GPS updates.
     New consumers (e.g., a new ML pipeline) can subscribe without
     changing the producer.

  2. REPLAY
     Found a bug in the surge pricing pipeline? Fix the bug,
     rewind the consumer offset, replay the last hour of events.

  3. MULTIPLE CONSUMERS
     A single GPS update is consumed by: geospatial index (real-time),
     surge pricing (aggregation), ETA ML training (batch), analytics
     (data lake), and map matching (traffic estimation).
     Without Kafka, the Location Service would need to call 5 services.

  4. DURABILITY
     Events are persisted to disk (retention: 3-7 days for most topics).
     Even if a consumer is down for hours, it can catch up.

  5. BACKPRESSURE
     If a consumer is slow (e.g., analytics pipeline during a spike),
     Kafka buffers messages. The consumer processes at its own pace.
     The producer is never blocked.

Kafka challenges at Uber's scale:
  • Consumer lag during traffic spikes (surge events, peak hours)
    → Monitoring consumer lag, auto-scaling consumer groups
  • Topic sprawl (thousands of topics, hard to manage)
    → Topic naming conventions, ownership registry
  • Cross-DC replication (MirrorMaker / uReplicator)
    → Uber built uReplicator for efficient cross-DC Kafka replication
      [VERIFIED — Uber Engineering Blog: "uReplicator: Uber Engineering's
       Apache Kafka Replicator"]
  • Dead Letter Queues for failed messages
    → Messages that fail processing are moved to a DLQ for investigation
```

---

## 6. Data Lake & Batch Analytics

```
All operational data eventually lands in the data lake for
batch analytics and ML training.

Pipeline:
  Real-time events (Kafka)
      │
      ├── Kafka → S3/HDFS (hourly or daily ETL)
      │
      ▼
  ┌─────────────────────────────────┐
  │         Data Lake (S3/HDFS)     │
  │                                 │
  │  Raw data:                      │
  │    • GPS traces (petabytes)     │
  │    • Trip events (billions/day) │
  │    • Pricing decisions          │
  │    • Engagement events          │
  │    • Driver activity logs       │
  │                                 │
  │  Processed data:                │
  │    • Aggregated trip stats      │
  │    • Market metrics (by city,   │
  │      by hour, by day)           │
  │    • ML feature tables          │
  │    • Driver earnings reports    │
  └──────────────┬──────────────────┘
                 │
  ┌──────────────▼──────────────────┐
  │  Query Engines                   │
  │                                  │
  │  Apache Hive: SQL on HDFS       │
  │  Presto: interactive SQL queries │
  │  Apache Spark: large-scale       │
  │    transformations, ML training  │
  │    data preparation              │
  └──────────────┬──────────────────┘
                 │
  ┌──────────────▼──────────────────┐
  │  Consumers                       │
  │                                  │
  │  Business Intelligence:          │
  │    • Dashboards (trips/hour,     │
  │      revenue, wait times)        │
  │    • City ops reports            │
  │    • Driver earnings summaries   │
  │                                  │
  │  Regulatory reporting:           │
  │    • Trip data for city audits   │
  │    • Driver hours and earnings   │
  │    • Safety incident reports     │
  │                                  │
  │  ML training:                    │
  │    • ETA model training data     │
  │    • Surge prediction features   │
  │    • Fraud detection features    │
  │    • Demand forecasting          │
  └─────────────────────────────────┘
```

---

## 7. Michelangelo — ML Platform

**VERIFIED — Uber Engineering Blog: "Meet Michelangelo: Uber's Machine Learning Platform"**

```
Michelangelo is Uber's end-to-end ML platform for building,
deploying, and monitoring ML models at scale.

Pipeline:
  ┌─────────────┐    ┌──────────────┐    ┌────────────────┐
  │   Feature    │───>│   Model      │───>│   Model        │
  │   Store      │    │   Training   │    │   Serving      │
  │              │    │              │    │                │
  │  • Online    │    │  • TensorFlow│    │  • Online:     │
  │    features  │    │  • XGBoost   │    │    real-time   │
  │    (Redis)   │    │  • PyTorch   │    │    inference   │
  │  • Offline   │    │  • LightGBM  │    │    (<5ms)      │
  │    features  │    │              │    │  • Offline:    │
  │    (Hive)    │    │  Distributed │    │    batch       │
  │              │    │  training on │    │    predictions │
  │              │    │  GPU cluster │    │                │
  └─────────────┘    └──────────────┘    └────────────────┘
         │                  │                     │
         ▼                  ▼                     ▼
  ┌────────────────────────────────────────────────────────┐
  │                   Model Management                      │
  │                                                         │
  │  • Model registry (versioned models)                   │
  │  • A/B testing framework (deploy model variants)       │
  │  • Monitoring (prediction accuracy, drift detection)   │
  │  • Automatic retraining triggers                       │
  └────────────────────────────────────────────────────────┘

Feature Store:
  The Feature Store is critical — it ensures that features used
  during training are IDENTICAL to features used during inference.

  Online features (for real-time inference):
    Stored in Redis/Cassandra. Updated in real-time.
    Example: "current traffic speed on this road segment"
    Latency: <5ms per feature lookup

  Offline features (for batch training):
    Stored in Hive. Computed from batch pipelines.
    Example: "average trip duration on this route at this hour of day"
    Updated: hourly or daily

  Training-serving skew is a major ML problem:
    If training uses Hive features (computed at T-1 day)
    but serving uses Redis features (computed at T-now),
    the model sees different feature distributions → poor predictions.
    Michelangelo's Feature Store mitigates this by providing
    a unified API that guarantees consistent features.

Model Serving:
  Online serving: model loaded into a prediction service.
    Request comes in (e.g., ETA query) → features fetched from Feature Store
    → model inference → prediction returned.
    Latency target: <5ms per prediction.

  For latency-critical paths (ETA, surge):
    Models are compiled/optimized (ONNX, TensorRT).
    Inference runs on CPUs (not GPUs — GPU latency variance is too high
    for real-time serving at P99).
```

---

## 8. ML Use Cases at Uber

```
1. ETA PREDICTION
   Input: route features, traffic, time-of-day, weather, events
   Output: predicted trip duration (seconds)
   Training: billions of historical trips (predicted vs actual duration)
   Retraining: daily
   Impact: ~10-15% accuracy improvement over routing-only ETA

2. SURGE PREDICTION (demand forecasting)
   Input: historical demand patterns, time-of-day, events, weather
   Output: predicted demand per H3 cell in next 15-30 minutes
   Use: proactive supply positioning (driver heat maps)
   Impact: reduces rider wait times by positioning drivers ahead of demand

3. FRAUD DETECTION
   Input: account features, device fingerprint, trip patterns, payment history
   Output: fraud probability score
   Actions: block, flag for review, require verification
   Real-time: scored at ride request time (<10ms inference)
   Impact: millions of dollars of fraud prevented annually

4. DRIVER DESTINATION PREDICTION
   Input: driver's current location, heading, time-of-day, day-of-week
   Output: predicted destination (even without an active trip)
   Use: improve matching — if driver is heading home (north),
        match them with a rider going north, not south
   Impact: better match quality → shorter detours for drivers

5. MARKETPLACE PRICING
   Input: supply, demand, route, time, weather, events
   Output: market-clearing price (what price balances supply/demand?)
   Evolution: simple surge (2012) → upfront pricing (2016) → ML pricing (2017+)

6. DRIVER SUPPLY MANAGEMENT
   Input: historical patterns, current supply, predicted demand
   Output: incentive recommendations (bonus zones, quests)
   Use: decide where to show "hot zones" on driver's map
   Impact: redistributes supply toward areas with predicted demand

7. SAFETY ANOMALY DETECTION
   Input: GPS traces, acceleration data, trip patterns
   Output: anomaly score (detect: crashes, route deviations, unsafe driving)
   Real-time: monitored during active trips
   Actions: alert safety team, send check-in to rider
```

---

## 9. Contrasts

### Uber Data Pipeline vs Netflix Data Pipeline

| Dimension | Uber | Netflix |
|---|---|---|
| **Primary data** | Geospatial time-series (GPS traces) | User×item interaction matrix (viewing history) |
| **Data shape** | Dense, low-dimensional (lat, lng, time, speed) | Sparse, high-dimensional (millions of users × millions of titles) |
| **Volume** | ~130B GPS updates/day, trillions of Kafka msgs/day | ~140M hours of viewing/day, billions of events/day |
| **Real-time needs** | Critical (dispatch, ETA, surge — decisions in seconds) | Limited (recommendations can use yesterday's data) |
| **ML primary use** | ETA prediction, fraud, demand forecasting | Content recommendation, personalization |
| **Streaming backbone** | Kafka (trillions/day) | Kafka (but lower volume — content events, not GPS) |
| **Feature Store** | Michelangelo Feature Store (online + offline) | Custom feature pipelines |

### Uber Storage vs Google Maps Storage

| Dimension | Uber | Google Maps |
|---|---|---|
| **Location data source** | Millions of drivers (identified, high frequency) | Billions of Android phones (anonymized, lower frequency) |
| **GPS update frequency** | Every 3-4 seconds per driver | Every ~1 minute per Android device |
| **Coverage** | Dense on ride-share roads, sparse elsewhere | Broad coverage on all roads |
| **Traffic estimation** | Aggregate driver speeds per road segment | Aggregate Android device speeds per road segment |
| **Storage for location** | Redis (real-time) + Cassandra (history) | Bigtable / Spanner |
| **Map data** | OSM base + proprietary corrections from GPS traces | Proprietary (Street View, satellite, crowdsourced edits) |

### MySQL/Schemaless vs DynamoDB/Cassandra

| Dimension | MySQL/Schemaless (Uber) | DynamoDB (AWS managed) | Cassandra (open-source) |
|---|---|---|---|
| **Consistency** | Strong (per-shard) | Strong or eventual (configurable) | Eventual (tunable) |
| **Schema** | JSON blobs (flexible) | Key-value / document | Wide-column |
| **Sharding** | Custom (Uber-built) | Automatic (AWS-managed) | Automatic (consistent hashing) |
| **Transactions** | Yes (per-shard ACID) | Yes (limited, cross-item) | No (lightweight transactions only) |
| **Operational cost** | High (Uber manages everything) | Low (fully managed by AWS) | Medium (open-source, self-managed) |
| **Why Uber chose it** | MySQL familiarity, control, cost at scale | — | Used for write-heavy analytics data |
| **For most companies** | Overkill — use managed service | Best default choice | Good for write-heavy, time-series workloads |
