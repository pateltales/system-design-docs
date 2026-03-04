# Deep Dive: Real-Time Analytics Pipeline

## 1. The Real-Time Dashboard Problem

### What Creators Actually See

YouTube Studio exposes a **real-time analytics dashboard** that shows creators a live-updating graph of view counts with **minute-level granularity** over the last 48 hours. This is a fundamentally different problem from "how many total views does this video have" (the counter system). It requires a dedicated **streaming analytics pipeline** that can:

1. Ingest millions of view events per second globally.
2. Aggregate them into time-bucketed counts per video.
3. Serve those aggregates to dashboards with sub-second query latency.
4. Retain multiple granularity levels for different time horizons.

### Why This Is Separate From the Counter System

The counter system (increment path) is optimized for **write throughput and eventual consistency of a single scalar** (total views). The analytics pipeline is optimized for **time-dimensional queries** ("how many views happened between 14:32 and 14:33 on Feb 26?"). Conflating these two concerns leads to poor performance on both axes.

```
Counter System:                Analytics Pipeline:
┌─────────────┐                ┌──────────────────────────┐
│ "Total = N" │                │ "Views per minute for    │
│  One scalar │                │  last 48h, per hour for  │
│  per video  │                │  last 90d, per day       │
└─────────────┘                │  forever"                │
                               │  Time-series data        │
                               └──────────────────────────┘
```

### Requirements and SLAs

| Requirement               | Target                                                    |
|---------------------------|-----------------------------------------------------------|
| End-to-end latency        | View event to dashboard update in < 60 seconds            |
| Query latency (P99)       | < 200ms for minute-level queries over 48h window          |
| Write throughput           | Sustain 2M+ events/sec globally                           |
| Data freshness             | Minute-level buckets finalized within 2 minutes of window close |
| Availability               | 99.9% for the analytics read path                         |
| Late event tolerance       | Accept events up to 5 minutes late; correct previously emitted aggregates |
| Retention                  | 48h at minute granularity, 90d at hour, indefinite at day |

The 60-second end-to-end latency is the critical SLA. Creators expect the graph to feel "live" but do not require sub-second freshness. This gives us room for batching and windowed aggregation.

---

## 2. Stream Processing Architecture

### High-Level Pipeline

```
┌──────────┐    ┌─────────────────┐    ┌──────────────────────┐    ┌────────────┐
│  View    │    │                 │    │  Stream Processor    │    │ Time-Series│
│  Events  │───>│  Kafka Topics   │───>│  (Flink / KStreams)  │───>│  Database  │
│  (global)│    │                 │    │  Windowed Aggregation│    │ (ClickHouse│
└──────────┘    └─────────────────┘    └──────────────────────┘    │  / Druid)  │
                                              │                    └─────┬──────┘
                                              │                          │
                                       ┌──────▼──────┐           ┌──────▼──────┐
                                       │  Alerts /   │           │  Dashboard  │
                                       │  Anomaly    │           │  Query API  │
                                       │  Detection  │           │             │
                                       └─────────────┘           └─────────────┘
```

### Detailed Data Flow

```
Step 1: Ingestion
─────────────────
View event (video_id, user_id, timestamp, geo, device, ...)
    │
    ▼
Kafka topic: "view-events"
    Partitioned by video_id (ensures all events for a video go to same partition)
    Retention: 7 days (allows reprocessing)
    Replication factor: 3

Step 2: Stream Processing (Apache Flink)
────────────────────────────────────────
Flink job consumes from "view-events"
    │
    ├── Assign event-time timestamps and watermarks
    ├── Key by video_id
    ├── Apply tumbling window (1 minute, event time)
    ├── Count events per window per video
    │
    ▼
Emit: (video_id, window_start, window_end, view_count)

Step 3: Sink to Time-Series DB
──────────────────────────────
Flink sink writes aggregated records to ClickHouse / Druid
    │
    ▼
Table: minute_view_counts
    video_id     | window_start        | view_count
    ─────────────┼─────────────────────┼───────────
    abc123       | 2026-02-27 14:32:00 | 4,217
    abc123       | 2026-02-27 14:33:00 | 3,891
```

### Kafka Topic Design

The Kafka topic for view events needs careful partitioning:

```
Topic: view-events
  Partitions: 256 (allows high parallelism)
  Partition key: video_id
  Value schema (Avro):
    {
      "video_id":   "string",
      "user_id":    "string",
      "event_time": "long (epoch millis)",
      "geo":        "string",
      "device":     "string",
      "session_id": "string"
    }
```

Partitioning by `video_id` ensures all events for a given video land on the same partition, which means a single Flink subtask handles all windows for that video. This avoids cross-partition shuffles during aggregation.

**Hot-partition risk**: Viral videos can produce extremely skewed load on one partition. Mitigation strategies:

- **Salted keys**: Append a random salt (0-7) to the partition key, then aggregate across salts in a second stage.
- **Separate hot-video pipeline**: Route videos exceeding a throughput threshold to a dedicated high-throughput topic with more partitions.

### Window Types Explained

Stream processing windows define how infinite event streams get chopped into finite, aggregatable chunks.

#### Tumbling Windows (Primary Choice for View Counting)

```
Event time ──────────────────────────────────────────────────►

Window 1          Window 2          Window 3
┌────────────┐    ┌────────────┐    ┌────────────┐
│ 14:00-14:01│    │ 14:01-14:02│    │ 14:02-14:03│
│  events: 47│    │  events: 52│    │  events: 38│
└────────────┘    └────────────┘    └────────────┘

Properties:
  - Fixed size (1 minute)
  - Non-overlapping
  - Every event belongs to exactly one window
  - Best for: periodic aggregation (our primary use case)
```

Tumbling windows are the right default for minute-level view counts because each minute bucket is independent and non-overlapping. Every view event maps to exactly one 1-minute window.

#### Sliding Windows

```
Event time ──────────────────────────────────────────────────►

       ┌────────────────────┐
       │  Window A (5 min)  │
       └────────────────────┘
            ┌────────────────────┐
            │  Window B (5 min)  │  slide = 1 min
            └────────────────────┘
                 ┌────────────────────┐
                 │  Window C (5 min)  │
                 └────────────────────┘

Properties:
  - Fixed size (e.g., 5 minutes)
  - Windows overlap (slide interval < window size)
  - One event can belong to multiple windows
  - Best for: moving averages, trend detection
```

Sliding windows are useful for computing "views in the last 5 minutes" as a rolling metric. They are more expensive because each event contributes to multiple windows. For dashboards, we typically compute tumbling windows in the pipeline and derive sliding aggregates at query time.

#### Session Windows

```
Event time ──────────────────────────────────────────────────►

User A:  ●  ● ●    ●                    ● ●  ●  ●
         └───────────┘                   └──────────┘
          Session 1                       Session 2
          (gap < 30min)    (gap > 30min)  (gap < 30min)

Properties:
  - Variable size (defined by inactivity gap)
  - Non-overlapping per key
  - Best for: user engagement analysis, session-level metrics
```

Session windows are not used for view counting directly but are valuable for engagement analytics (e.g., "average session duration on video X"). They group events by user activity with a configurable inactivity gap.

**Which windows do we use?**

| Use Case                          | Window Type | Parameters           |
|-----------------------------------|-------------|----------------------|
| Minute-level view counts          | Tumbling    | size = 1 min         |
| Hourly rollups                    | Tumbling    | size = 1 hour        |
| "Views in last 5 min" velocity    | Derived at query time from tumbling windows |
| User session analysis             | Session     | gap = 30 min         |
| Trending detection                | Sliding     | size = 10 min, slide = 1 min |

### Late-Arriving Events

In a distributed system, events do not arrive in order. A view that happened at 14:01:58 might arrive at the processing layer at 14:03:12 due to network delays, client-side buffering, or regional ingestion lag.

#### Event Time vs. Processing Time

```
                     Network    Queue     Processing
Event occurs ──────► delay ───► wait ───► arrival
   14:01:58          +15s       +40s      +22s
                                          ─────────
                               Processing time: 14:03:15
                               Event time:      14:01:58

If we use processing time:
  - Event counted in the 14:03 bucket (WRONG)
  - Dashboard shows inflated 14:03, deflated 14:01

If we use event time:
  - Event counted in the 14:01 bucket (CORRECT)
  - But the 14:01 window may have already been emitted...
```

We always use **event time** for correctness. This introduces the late-event problem.

#### Watermarks

A watermark is a declaration by the system: "I believe all events with event time <= W have arrived." It is a heuristic, not a guarantee.

```
Watermark progression:

Time ──────────────────────────────────────────────────►

Events arriving:
  t=14:00:03  t=14:00:47  t=14:01:02  t=14:00:58  t=14:01:15
                                        ▲
                                        └── Late! (event time 14:00:58
                                            arrived after 14:01:02)

Watermark (W):
  W=14:00:00 ──► W=14:00:30 ──► W=14:00:50 ──► W=14:01:00
                                                  │
                                Window 14:00-14:01 fires here
                                (watermark crossed window end)
```

Flink generates watermarks using one of two strategies:

1. **Periodic watermarks**: Advance the watermark every N milliseconds to `max_event_time_seen - allowed_lateness`. This is the standard approach.
2. **Punctuated watermarks**: Advance based on special marker events in the stream.

```java
// Flink watermark strategy: allow 5 minutes of lateness
WatermarkStrategy
    .<ViewEvent>forBoundedOutOfOrderness(Duration.ofMinutes(5))
    .withTimestampAssigner((event, timestamp) -> event.getEventTime());
```

#### Lateness Handling

```
┌──────────────────────────────────────────────────────────┐
│              Event Arrival Timeline                       │
│                                                          │
│  Window: [14:00, 14:01)                                  │
│                                                          │
│  ──────┬──────────┬────────────┬──────────┬──────────►   │
│        │          │            │          │    time       │
│     14:01      14:02        14:04      14:06             │
│   Window      Watermark     Late but    Too late.        │
│   closes      at 14:01      within 5m   Dropped or       │
│   (initial    (events       lateness    sent to side     │
│    fire)      still         window.     output.          │
│               accepted)     Triggers                     │
│                             correction                   │
│                             fire.                        │
└──────────────────────────────────────────────────────────┘
```

**Three outcomes for a late event:**

1. **Within watermark delay (< 5 min late)**: The window has not yet been finalized. The event is included in the window's aggregate. No special handling needed.
2. **After watermark but within allowed lateness (Flink's `allowedLateness`)**: The window re-fires with an updated aggregate. The time-series DB receives an upsert that corrects the previous value.
3. **Beyond allowed lateness**: The event is routed to a **side output** (dead-letter topic) for offline reconciliation. It is not reflected in real-time dashboards.

```java
// Flink window with late event handling
stream
    .keyBy(ViewEvent::getVideoId)
    .window(TumblingEventTimeWindows.of(Time.minutes(1)))
    .allowedLateness(Time.minutes(5))
    .sideOutputLateData(lateOutputTag)
    .aggregate(new ViewCountAggregator());
```

The correction fires produce **upserts** to the time-series database. This means the sink must support idempotent writes (INSERT ON CONFLICT UPDATE or equivalent).

---

## 3. Time-Series Storage

### Requirements

The time-series database sits at the end of the streaming pipeline and serves dashboard queries. Its requirements:

| Requirement              | Detail                                                      |
|--------------------------|-------------------------------------------------------------|
| Write throughput         | 500K+ rows/sec (minute-level aggregates across all videos)  |
| Query latency            | P99 < 200ms for time-range scans on a single video          |
| Compression              | Efficient storage of time-ordered numeric data              |
| Time-range queries       | Native support for "WHERE time BETWEEN X AND Y"             |
| Automatic downsampling   | Built-in or easy-to-implement rollup from fine to coarse    |
| SQL or SQL-like interface | Reduces learning curve, integrates with existing tooling    |
| Horizontal scalability   | Scale writes and storage independently                      |

### Option Comparison

| Feature              | ClickHouse            | Apache Druid          | TimescaleDB            | InfluxDB              |
|----------------------|-----------------------|-----------------------|------------------------|-----------------------|
| **Storage model**    | Columnar              | Columnar + inverted   | Row (PostgreSQL ext)   | Custom TSM engine     |
| **Query language**   | SQL                   | SQL (Druid SQL) + native JSON | SQL (PostgreSQL) | InfluxQL / Flux       |
| **Write throughput** | Excellent (millions/s)| Excellent              | Good (limited by PG)  | Good                  |
| **Compression**      | Excellent (LZ4, ZSTD, delta, gorilla) | Good  | Moderate               | Good (gorilla for floats) |
| **Query latency**    | Sub-second on billions| Sub-second             | Depends on indexing    | Good for simple queries |
| **Scalability**      | Linear (sharding)     | Linear (segments)      | Limited horizontal     | Clustered (enterprise) |
| **Downsampling**     | Materialized views    | Built-in rollup        | Continuous aggregates  | Retention policies + CQ |
| **Operational cost** | Moderate              | High (ZK, coordinator) | Low (it is PostgreSQL) | Low (single binary)   |
| **Best for**         | General analytics at scale | Real-time OLAP    | Small-medium scale     | IoT / metrics         |

### ClickHouse: The Recommended Choice

ClickHouse is the strongest fit for this workload. Here is why:

**Columnar storage** means that a query like "sum view_count for video X between T1 and T2" only reads the `video_id`, `window_start`, and `view_count` columns. Other columns (geo, device breakdowns) are never loaded from disk. At scale, this is the difference between scanning 3 bytes per row vs 100 bytes per row.

**Compression** on time-series data is exceptional. ClickHouse applies delta encoding on timestamps (consecutive minute timestamps differ by 60000ms, which compresses to nearly zero), gorilla encoding on floating-point metrics, and LZ4/ZSTD on top. Compression ratios of 10-20x are typical.

**MergeTree engine** is purpose-built for time-series:

```sql
CREATE TABLE minute_view_counts (
    video_id     String,
    window_start DateTime,
    view_count   UInt64,
    geo          LowCardinality(String),
    device       LowCardinality(String)
)
ENGINE = ReplacingMergeTree(window_start)
PARTITION BY toYYYYMMDD(window_start)
ORDER BY (video_id, window_start)
TTL window_start + INTERVAL 48 HOUR;
```

Key design choices in this schema:

- **`ReplacingMergeTree`**: Handles upserts from late-event corrections. When Flink re-fires a window with an updated count, the new row replaces the old one (same primary key).
- **`PARTITION BY toYYYYMMDD`**: Each day is a separate partition. Dropping old data (TTL) is a metadata operation, not a row-by-row delete.
- **`ORDER BY (video_id, window_start)`**: Queries for "video X, last 48 hours" scan a contiguous range of sorted data. Extremely efficient.
- **`TTL`**: Automatic expiration of minute-level data after 48 hours. The rollup job must run before this TTL kicks in.
- **`LowCardinality`**: Dictionary-encoded strings for low-cardinality dimensions (geo, device). Saves memory and speeds up GROUP BY.

### Druid: The Alternative for Multi-Dimensional Slicing

If the analytics dashboard needs heavy multi-dimensional queries (e.g., "views per minute for video X, broken down by country and device type"), Druid's **bitmap indexes on dimensions** provide faster GROUP BY performance than ClickHouse for high-cardinality dimension combinations.

```
Druid ingestion spec (simplified):

{
  "dataSource": "minute_view_counts",
  "timestampSpec": { "column": "window_start", "format": "millis" },
  "dimensionsSpec": {
    "dimensions": ["video_id", "geo", "device"]
  },
  "metricsSpec": [
    { "type": "longSum", "name": "view_count", "fieldName": "view_count" }
  ],
  "granularitySpec": {
    "segmentGranularity": "HOUR",
    "queryGranularity": "MINUTE"
  }
}
```

Druid's operational complexity (ZooKeeper, Coordinator, Broker, Historical, MiddleManager nodes) makes it harder to run than ClickHouse. Choose Druid only if you need its specific strengths (sub-second multi-dimensional OLAP, built-in rollup).

---

## 4. Downsampling / Rollup Strategy

### The Storage Problem

At YouTube scale (10B+ views/day across 800M+ videos), storing minute-level aggregates indefinitely is not feasible:

```
Minute-level data:
  ~500M active videos × 1440 minutes/day = 720 billion rows/day
  Even at 20 bytes/row compressed = 14.4 TB/day
  For 1 year = 5.2 PB

This is unsustainable. Downsampling is mandatory.
```

### Rollup Tiers

```
┌─────────────────────────────────────────────────────────────────┐
│                     Rollup Pipeline                              │
│                                                                  │
│  Raw Events    1-Minute Aggs     1-Hour Aggs      1-Day Aggs    │
│  (Kafka)       (48 hours)        (90 days)        (forever)     │
│                                                                  │
│  ●●●●●●●● ──► ┌──┬──┬──┐ ──► ┌──────────┐ ──► ┌──────────┐   │
│  millions/s    │m1│m2│m3│     │  hour 1  │     │  day 1   │    │
│                └──┴──┴──┘     └──────────┘     └──────────┘    │
│                1440/day/video  24/day/video     1/day/video     │
│                                                                  │
│  Compression:  60x reduction   24x reduction    ───────────     │
│                (from minute)   (from hour)      Final tier      │
└─────────────────────────────────────────────────────────────────┘
```

### Data Volume at Each Tier

Assume 500M active videos, each receiving at least one view per relevant time window:

| Tier            | Granularity | Retention | Rows / day / video | Total rows / day | Storage / day (compressed) | Total storage |
|-----------------|-------------|-----------|--------------------|--------------------|---------------------------|---------------|
| Raw events      | Event-level | 7 days (Kafka) | varies        | ~10 billion        | ~200 GB (Kafka)           | ~1.4 TB       |
| Minute aggs     | 1 minute    | 48 hours  | up to 1,440        | ~2 billion*        | ~40 GB                    | ~80 GB (48h)  |
| Hourly aggs     | 1 hour      | 90 days   | up to 24           | ~140 million*      | ~2.8 GB                   | ~250 GB (90d) |
| Daily aggs      | 1 day       | Forever   | 1                  | ~10 million*       | ~200 MB                   | ~73 GB/year   |

*Not all videos are active every minute/hour. Active counts are estimated.

The key insight: **minute-level data for 48 hours costs ~80 GB**. Keeping it forever would cost ~14.6 TB/year. Downsampling to hourly reduces this to ~250 GB for 90 days. Daily aggregates are negligible.

### Rollup Implementation

#### Hourly Rollup (Minute -> Hour)

Runs every hour, aggregating the previous hour's minute-level data:

```sql
-- ClickHouse materialized view for automatic hourly rollup
CREATE MATERIALIZED VIEW hourly_view_counts
ENGINE = SummingMergeTree()
PARTITION BY toYYYYMM(window_start)
ORDER BY (video_id, window_start)
TTL window_start + INTERVAL 90 DAY
AS
SELECT
    video_id,
    toStartOfHour(window_start) AS window_start,
    sum(view_count) AS view_count
FROM minute_view_counts
GROUP BY video_id, toStartOfHour(window_start);
```

With ClickHouse materialized views, this rollup happens **automatically** as data is inserted into the minute table. No separate batch job needed.

#### Daily Rollup (Hour -> Day)

```sql
CREATE MATERIALIZED VIEW daily_view_counts
ENGINE = SummingMergeTree()
PARTITION BY toYYYYMM(window_start)
ORDER BY (video_id, window_start)
AS
SELECT
    video_id,
    toStartOfDay(window_start) AS window_start,
    sum(view_count) AS view_count
FROM hourly_view_counts
GROUP BY video_id, toStartOfDay(window_start);
```

#### Handling Reprocessing and Corrections

Late events cause Flink to re-fire windows, producing updated minute-level rows. These must propagate correctly through the rollup chain.

**Idempotent upserts** are critical. `ReplacingMergeTree` in ClickHouse deduplicates rows with the same primary key during background merges. For rollups, `SummingMergeTree` adds values for rows with the same key. When a corrected minute row arrives:

1. The old minute row and new minute row coexist until a merge.
2. For the hourly materialized view, both the old and new values are summed. This can cause double-counting.

**Solution**: Use `ReplacingMergeTree` (not `SummingMergeTree`) for rollup tables, and recompute the full hour when any constituent minute is updated. This is more expensive but correct.

```sql
-- Alternative: batch rollup job (runs hourly, idempotent)
INSERT INTO hourly_view_counts
SELECT
    video_id,
    toStartOfHour(window_start) AS window_start,
    sum(view_count) AS view_count
FROM minute_view_counts
WHERE window_start >= now() - INTERVAL 2 HOUR
  AND window_start < now() - INTERVAL 1 HOUR
GROUP BY video_id, toStartOfHour(window_start)
SETTINGS insert_deduplicate = 0;

-- The ReplacingMergeTree engine will keep only the latest version
-- of each (video_id, window_start) pair after background merge.
```

Running the rollup over a 2-hour lookback (instead of exactly the last hour) ensures that late corrections from the previous cycle are captured.

---

## 5. Query Patterns

### Pattern 1: Views in Last Hour for Video X

**Use case**: Creator opens YouTube Studio, sees the real-time graph.

```sql
SELECT
    window_start,
    view_count
FROM minute_view_counts
WHERE video_id = 'abc123'
  AND window_start >= now() - INTERVAL 1 HOUR
  AND window_start < now()
ORDER BY window_start;
```

**Performance**: With `ORDER BY (video_id, window_start)`, this is a range scan on a sorted index. ClickHouse reads ~60 rows (one per minute). Sub-millisecond execution.

### Pattern 2: Views Per Day for Last 30 Days

**Use case**: Creator looks at the "Last 28 days" analytics tab.

```sql
SELECT
    window_start,
    view_count
FROM daily_view_counts
WHERE video_id = 'abc123'
  AND window_start >= now() - INTERVAL 30 DAY
ORDER BY window_start;
```

**Performance**: 30 rows. Trivial.

### Pattern 3: Real-Time View Velocity

**Use case**: Detect if a video is going viral. Compare the view rate in the last 5 minutes to the average rate in the last hour.

```sql
WITH
    recent AS (
        SELECT sum(view_count) AS recent_views
        FROM minute_view_counts
        WHERE video_id = 'abc123'
          AND window_start >= now() - INTERVAL 5 MINUTE
    ),
    baseline AS (
        SELECT sum(view_count) / 12.0 AS avg_5min_views
        FROM minute_view_counts
        WHERE video_id = 'abc123'
          AND window_start >= now() - INTERVAL 1 HOUR
    )
SELECT
    recent.recent_views,
    baseline.avg_5min_views,
    recent.recent_views / baseline.avg_5min_views AS velocity_ratio
FROM recent, baseline;
```

A `velocity_ratio` above 3-5x might trigger a "trending" classification.

### Query Routing: Automatic Granularity Selection

The dashboard API should not expose raw table names to clients. Instead, a query router selects the appropriate granularity based on the requested time range:

```
┌─────────────────────────────────────────────────┐
│              Query Router Logic                  │
│                                                  │
│  Time range requested    Table to query          │
│  ─────────────────────   ──────────────────────  │
│  Last 48 hours           minute_view_counts      │
│  48 hours - 90 days      hourly_view_counts      │
│  > 90 days               daily_view_counts       │
│                                                  │
│  Edge case: "Last 7 days" at minute granularity  │
│  → Reject or auto-downsample to hourly           │
│    (7 days × 1440 min = 10,080 data points       │
│     is too many for a chart)                     │
└─────────────────────────────────────────────────┘
```

Implementation in the API layer:

```python
def route_query(video_id: str, start: datetime, end: datetime, granularity: str = "auto"):
    range_hours = (end - start).total_seconds() / 3600

    if granularity == "auto":
        if range_hours <= 48:
            granularity = "minute"
        elif range_hours <= 90 * 24:
            granularity = "hour"
        else:
            granularity = "day"

    table_map = {
        "minute": "minute_view_counts",
        "hour":   "hourly_view_counts",
        "day":    "daily_view_counts",
    }

    table = table_map[granularity]
    return f"""
        SELECT window_start, view_count
        FROM {table}
        WHERE video_id = '{video_id}'
          AND window_start >= '{start}'
          AND window_start < '{end}'
        ORDER BY window_start
    """
```

### Cross-Granularity Stitching

When a user requests "last 7 days" and we want minute-level for the most recent 48 hours but hourly for the remaining 5 days:

```sql
-- Stitch hourly (older) and minute (recent) data
SELECT window_start, view_count FROM (
    SELECT
        toStartOfHour(window_start) AS window_start,
        sum(view_count) AS view_count
    FROM minute_view_counts
    WHERE video_id = 'abc123'
      AND window_start >= now() - INTERVAL 48 HOUR
    GROUP BY toStartOfHour(window_start)

    UNION ALL

    SELECT window_start, view_count
    FROM hourly_view_counts
    WHERE video_id = 'abc123'
      AND window_start >= now() - INTERVAL 7 DAY
      AND window_start < now() - INTERVAL 48 HOUR
)
ORDER BY window_start;
```

The minute-level data is aggregated to hourly on-the-fly to produce a uniform granularity for the chart.

---

## 6. Pipeline Monitoring and Recovery

### Consumer Lag Monitoring

Consumer lag is the most critical health metric for any Kafka-based pipeline. It measures how far behind the consumer (Flink) is from the latest produced message.

```
Producer offset:  1,000,000
Consumer offset:    998,500
                  ─────────
Consumer lag:        1,500 messages

At 50,000 msgs/sec throughput:
  Lag = 1,500 / 50,000 = 0.03 seconds behind

Alert thresholds:
  WARNING:  lag > 100,000 messages (2 seconds)
  CRITICAL: lag > 5,000,000 messages (100 seconds)
             (approaching 60-second SLA violation)
```

**Monitoring tools**:
- **Kafka's built-in `consumer-groups` command**: `kafka-consumer-groups.sh --describe --group flink-analytics`
- **Burrow** (LinkedIn's open-source Kafka consumer lag checker): Evaluates lag trend, not just absolute value. A consumer with high but decreasing lag is healthy; low but increasing lag is not.
- **Flink metrics**: Expose `currentProcessingTime - currentEventTime` as a gauge. This directly measures the end-to-end event-time delay.

```
Dashboard: Pipeline Health
┌──────────────────────────────────────────────────────┐
│  Consumer Lag (messages)    [████████░░░░] 120K      │
│  E2E Latency (seconds)     [███░░░░░░░░░] 12s       │
│  Throughput (events/sec)   [██████████░░] 1.8M       │
│  Checkpoint Duration (ms)  [██░░░░░░░░░░] 850ms     │
│  Failed Checkpoints (24h)  [░░░░░░░░░░░░] 0         │
│  Backpressure              [░░░░░░░░░░░░] None       │
└──────────────────────────────────────────────────────┘
```

### Checkpoint and State Management in Flink

Flink maintains **operator state** for windowed aggregations. Each in-progress window has a partial aggregate (e.g., "video abc123, window 14:32-14:33, count so far = 2,847"). This state must survive failures.

#### Checkpointing

Flink periodically snapshots all operator state to durable storage (S3, HDFS):

```
Checkpoint cycle:

  ┌─────────┐     ┌─────────┐     ┌─────────┐
  │  CP 1   │     │  CP 2   │     │  CP 3   │
  │ t=10s   │     │ t=20s   │     │ t=30s   │
  └────┬────┘     └────┬────┘     └────┬────┘
       │               │               │
       ▼               ▼               ▼
  ┌─────────────────────────────────────────┐
  │         S3: checkpoint store            │
  │  cp-1/  cp-2/  cp-3/                   │
  │  (state snapshots + Kafka offsets)      │
  └─────────────────────────────────────────┘

On failure:
  1. Flink restarts from latest successful checkpoint (cp-3)
  2. Kafka consumer rewinds to the offsets stored in cp-3
  3. Events between cp-3 and the failure are reprocessed
  4. Window state is restored from cp-3 snapshot
  5. Pipeline resumes with exactly-once semantics
```

Configuration for production:

```java
StreamExecutionEnvironment env = StreamExecutionEnvironment.getExecutionEnvironment();

// Enable checkpointing every 10 seconds
env.enableCheckpointing(10_000, CheckpointingMode.EXACTLY_ONCE);

// Checkpoint must complete within 60 seconds or is discarded
env.getCheckpointConfig().setCheckpointTimeout(60_000);

// Allow only 1 checkpoint in progress at a time
env.getCheckpointConfig().setMaxConcurrentCheckpoints(1);

// Minimum 5 seconds between checkpoint completions
env.getCheckpointConfig().setMinPauseBetweenCheckpoints(5_000);

// Keep last 3 checkpoints for manual recovery
env.getCheckpointConfig().setExternalizedCheckpointCleanup(
    ExternalizedCheckpointCleanup.RETAIN_ON_CANCELLATION);

// Use RocksDB state backend for large state
env.setStateBackend(new EmbeddedRocksDBStateBackend());
env.getCheckpointConfig().setCheckpointStorage("s3://flink-checkpoints/view-analytics/");
```

**Why RocksDB?** The in-memory state backend (HashMapStateBackend) keeps all window state in JVM heap. With millions of active video windows, this can require 100+ GB of heap. RocksDB stores state on local SSD with only hot data in memory, allowing terabytes of state with bounded memory usage.

#### State Size Estimation

```
Active windows at any moment:
  ~500M active videos × 1 window per video (current minute) = 500M entries
  Each entry: video_id (16 bytes) + count (8 bytes) + metadata (16 bytes) = ~40 bytes
  Total state: ~20 GB

  With 5-minute allowed lateness: up to 6 concurrent windows per video
  Worst case: 500M × 6 × 40 bytes = ~120 GB

  → RocksDB state backend is mandatory at this scale
```

### Recovery From Failures

#### Scenario 1: Flink TaskManager Crash

```
Timeline:
  t=0     Last successful checkpoint (CP-47)
  t=5s    TaskManager crashes
  t=8s    JobManager detects failure
  t=10s   JobManager restarts task on new TaskManager
  t=15s   State restored from CP-47
  t=18s   Kafka consumer rewound to CP-47 offsets
  t=20s   Processing resumes, reprocessing 20s of events
  t=35s   Pipeline caught up, lag returns to normal

Impact: ~30 seconds of increased latency, no data loss
```

#### Scenario 2: Full Pipeline Restart (Deployment)

```
Steps:
  1. Trigger savepoint (like checkpoint but on-demand):
     flink savepoint <job-id> s3://flink-savepoints/view-analytics/

  2. Cancel old job:
     flink cancel <job-id>

  3. Deploy new version from savepoint:
     flink run -s s3://flink-savepoints/view-analytics/savepoint-xxxxx \
       view-analytics-job.jar

  4. New job resumes from exact state of the savepoint
     No data loss. No reprocessing beyond the savepoint boundary.
```

#### Scenario 3: Kafka Broker Failure

Kafka's replication factor of 3 means losing a single broker does not lose data. The Flink consumer transparently fails over to replica partitions. Consumer lag may spike briefly while new leader election completes (typically < 10 seconds).

#### Scenario 4: ClickHouse Sink Failure

If ClickHouse is temporarily unavailable, the Flink sink must buffer or apply backpressure:

```
Flink sink strategy:
  1. Batch writes (buffer 10,000 rows or 5 seconds, whichever comes first)
  2. On write failure: retry with exponential backoff (1s, 2s, 4s, max 30s)
  3. After 5 minutes of continuous failure: checkpoint still succeeds
     (Flink checkpoints Kafka offsets, not sink state)
  4. On recovery: Flink reprocesses from last checkpoint, re-derives
     all aggregates, and writes them to ClickHouse
  5. ReplacingMergeTree handles the duplicate/updated rows correctly
```

### Data Quality Validation

Even with exactly-once processing, data quality issues can arise from bugs in event producers, schema evolution errors, or clock skew on client devices.

#### Validation Checks

```
┌─────────────────────────────────────────────────────────┐
│  Validation                     Action on Failure       │
│  ─────────────────────────────  ─────────────────────── │
│  Event time in future (> 5min)  Route to dead letter    │
│  Event time too old (> 24h)     Route to dead letter    │
│  Missing video_id               Drop + increment metric │
│  video_id not in catalog        Drop + increment metric │
│  Duplicate event_id             Deduplicate (idempotent)│
│  Sum(minute aggs) != hourly     Alert + trigger recomp  │
│  agg for same video+hour                                │
└─────────────────────────────────────────────────────────┘
```

#### Cross-Pipeline Reconciliation

The counter system (total views) and the analytics pipeline (sum of all time-bucketed views) should agree. A daily reconciliation job compares:

```sql
-- Counter system total
SELECT total_views FROM video_counters WHERE video_id = 'abc123';

-- Analytics pipeline total (sum across all daily aggregates)
SELECT sum(view_count) FROM daily_view_counts WHERE video_id = 'abc123';

-- Discrepancy check
-- Acceptable drift: < 0.1% (due to late events beyond allowed lateness)
-- Alert if drift > 1%
```

This reconciliation catches systemic issues like:
- Lost events (Kafka topic misconfiguration, dropped partitions).
- Double-counting (duplicate event emission from producers).
- Rollup bugs (incorrect aggregation logic).

#### Alerting Rules

| Alert                             | Condition                              | Severity | Response                           |
|-----------------------------------|----------------------------------------|----------|------------------------------------|
| High consumer lag                 | Lag > 5M messages for > 2 min          | P1       | Check backpressure, scale up       |
| Checkpoint failures               | 3 consecutive checkpoint failures      | P1       | Investigate state backend, restart |
| Write sink errors                 | Error rate > 1% for > 5 min            | P1       | Check ClickHouse health            |
| Event-time skew                   | Watermark delay > 10 min               | P2       | Check event producers              |
| Reconciliation drift              | Counter vs analytics delta > 1%        | P2       | Trigger reprocessing job           |
| Dead letter queue growth          | > 10K messages/hour in DLQ             | P2       | Investigate malformed events       |
| Throughput drop                   | < 50% of expected throughput           | P2       | Check Kafka, check producers       |

---

## Summary

The real-time analytics pipeline is a distinct system from the view counter. It solves a different problem (time-dimensional queries vs. scalar totals) and uses different technology (stream processing + columnar time-series DB vs. distributed counters + cache).

```
End-to-end architecture:

  View Events → Kafka → Flink (tumbling windows, watermarks, late handling)
                                    │
                                    ▼
                              ClickHouse
                         ┌──────────────────┐
                         │ minute_view_counts│──► TTL 48h
                         │ hourly_view_counts│──► TTL 90d
                         │ daily_view_counts │──► Forever
                         └──────────────────┘
                                    │
                                    ▼
                            Dashboard Query API
                         (auto granularity routing)
```

The critical design decisions are:

1. **Event-time processing with watermarks** for correctness.
2. **Tumbling windows** for clean, non-overlapping minute buckets.
3. **ClickHouse with ReplacingMergeTree** for handling upserts from late corrections.
4. **Three-tier downsampling** (minute/hour/day) for cost-effective storage at scale.
5. **Flink checkpointing to S3** for exactly-once recovery.
6. **Cross-pipeline reconciliation** for catching systemic data quality issues.
