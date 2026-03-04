# Deep Dive: Redshift Spectrum & Data Lake Integration

> Companion document to [interview-simulation.md](./interview-simulation.md)

---

## Table of Contents

1. [The Data Lakehouse Problem](#1-the-data-lakehouse-problem)
2. [Redshift Spectrum Architecture](#2-redshift-spectrum-architecture)
3. [External Tables and the Glue Data Catalog](#3-external-tables-and-the-glue-data-catalog)
4. [Predicate Pushdown and Column Pruning](#4-predicate-pushdown-and-column-pruning)
5. [Partitioning: The Key to Spectrum Performance](#5-partitioning-the-key-to-spectrum-performance)
6. [File Formats: Columnar vs Row-Oriented](#6-file-formats-columnar-vs-row-oriented)
7. [Joining Local and External Tables](#7-joining-local-and-external-tables)
8. [Data Sharing: Cross-Cluster Access](#8-data-sharing-cross-cluster-access)
9. [Materialized Views](#9-materialized-views)
10. [Durability, Backups, and Recovery](#10-durability-backups-and-recovery)
11. [Design Decisions & Tradeoffs](#11-design-decisions--tradeoffs)

---

## 1. The Data Lakehouse Problem

### The Two-System Problem

Traditionally, organizations have two separate data systems:

```
┌──────────────────────┐     ┌──────────────────────┐
│     Data Lake          │     │    Data Warehouse     │
│                        │     │                       │
│  S3 (raw data)         │     │  Redshift (curated)   │
│  Parquet, ORC, JSON    │     │  Columnar, sorted     │
│  Petabytes, cheap      │     │  Fast queries         │
│  Schema-on-read        │     │  Schema-on-write      │
│  Query via Athena/EMR  │     │  Query via SQL        │
│                        │     │                       │
│  Problem: slow queries │     │  Problem: expensive   │
│  for interactive use   │     │  for petabyte storage  │
└──────────────────────┘     └──────────────────────┘

ETL pipeline copies data: Lake → Warehouse
  - Latency: hours to days
  - Duplication: same data in two places
  - Stale data: warehouse lags behind lake
```

### The Lakehouse Solution

Query both locations from a single SQL interface:

```
┌────────────────────────────────────────────┐
│           Redshift Cluster                   │
│                                              │
│  Local tables  ←─ hot data (frequently      │
│  (fast, sorted,    queried, needs sub-sec   │
│   compressed)      latency)                  │
│                                              │
│  External tables ←─ S3 data via Spectrum    │
│  (warm/cold data,   (less frequent queries, │
│   queried on-demand) massive volume)         │
│                                              │
│  Same SQL for both. JOINs across both.      │
└────────────────────────────────────────────┘
```

---

## 2. Redshift Spectrum Architecture

### The Separate Compute Fleet

Spectrum does NOT use your Redshift compute nodes to scan S3 data. It uses a **separate, AWS-managed fleet of compute nodes**:

```
┌────────────────────────────────────────────────────┐
│                                                      │
│  Client → Leader Node                                │
│             │                                        │
│             │ Query touches both local + external     │
│             │                                        │
│     ┌───────┴──────────────┐                        │
│     │                      │                        │
│     ▼                      ▼                        │
│  ┌────────────┐    ┌─────────────────────┐          │
│  │ Compute    │    │ Spectrum Layer       │          │
│  │ Nodes      │    │ (AWS-managed fleet)  │          │
│  │            │    │                      │          │
│  │ Process    │    │ 1. Read S3 files     │          │
│  │ local      │    │ 2. Apply predicates  │          │
│  │ table data │    │ 3. Column pruning    │          │
│  │            │    │ 4. Partial aggregate │          │
│  │            │    │ 5. Return filtered   │          │
│  │            │    │    results to compute│          │
│  │            │    │    nodes             │          │
│  └──────┬─────┘    └──────────┬──────────┘          │
│         │                      │                     │
│         │  JOIN results from   │                     │
│         │  both sources        │                     │
│         └──────┬───────────────┘                     │
│                │                                     │
│                ▼                                     │
│         Leader: merge + return                       │
└────────────────────────────────────────────────────┘

                    ┌─────────────┐
                    │  S3 Bucket   │
                    │              │
                    │  /orders/    │
                    │   year=2024/ │
                    │    month=01/ │
                    │     data.pq  │
                    │    month=02/ │
                    │     data.pq  │
                    └─────────────┘
```

**Why a separate fleet?**
1. Spectrum queries don't consume your cluster's CPU/memory
2. The Spectrum fleet is shared across all Redshift customers (AWS manages scale)
3. You can have a small Redshift cluster but query petabytes in S3
4. Spectrum nodes scale horizontally based on the data volume being scanned

### What Spectrum Pushes Down

The Spectrum layer does as much work as possible before sending data to your compute nodes:

| Operation | Pushed to Spectrum? | Impact |
|---|---|---|
| Partition pruning | **Yes** | Only reads matching partitions from S3 |
| Column pruning | **Yes** (for columnar formats) | Only reads needed columns from Parquet/ORC |
| Row filtering (WHERE) | **Yes** | Applies predicates before returning data |
| Aggregation (SUM, COUNT) | **Partial** | Computes partial aggregates to reduce data volume |
| JOINs | **No** | JOINs happen on your compute nodes |
| Complex expressions | **Some** | Simple predicates pushed down, complex UDFs may not be |

---

## 3. External Tables and the Glue Data Catalog

### Creating External Schemas and Tables

External tables are metadata pointers to S3 data, registered in the **AWS Glue Data Catalog** (or a Hive Metastore):

```sql
-- Step 1: Create an external schema pointing to a Glue database
CREATE EXTERNAL SCHEMA spectrum_schema
FROM DATA CATALOG
DATABASE 'my_data_lake_db'
IAM_ROLE 'arn:aws:iam::123456789012:role/RedshiftSpectrumRole'
CREATE EXTERNAL DATABASE IF NOT EXISTS;

-- Step 2: Create an external table
CREATE EXTERNAL TABLE spectrum_schema.orders (
  order_id      BIGINT,
  customer_id   BIGINT,
  amount        DECIMAL(10,2),
  order_status  VARCHAR(20)
)
PARTITIONED BY (year INT, month INT)
STORED AS PARQUET
LOCATION 's3://my-data-lake/orders/';

-- Step 3: Add partitions
ALTER TABLE spectrum_schema.orders
ADD PARTITION (year=2024, month=1)
LOCATION 's3://my-data-lake/orders/year=2024/month=01/';

-- Or use Glue Crawler to automatically discover partitions
```

### The Glue Data Catalog

```
┌────────────────────────────────────────────┐
│           AWS Glue Data Catalog             │
│                                             │
│  Database: my_data_lake_db                  │
│  ├── Table: orders                          │
│  │   ├── Columns: order_id, customer_id, ...│
│  │   ├── Format: Parquet                    │
│  │   ├── Location: s3://bucket/orders/      │
│  │   └── Partitions:                        │
│  │       ├── year=2024/month=01 → s3://...  │
│  │       ├── year=2024/month=02 → s3://...  │
│  │       └── ...                            │
│  └── Table: customers                       │
│      └── ...                                │
│                                             │
│  Shared across:                             │
│  - Redshift Spectrum                        │
│  - Athena                                   │
│  - EMR (Spark, Hive)                       │
│  - Glue ETL jobs                            │
└────────────────────────────────────────────┘
```

The Glue Data Catalog is the shared metadata store. A table registered in Glue is queryable from Redshift Spectrum, Athena, EMR, and Glue ETL — without duplicating data.

### Pseudocolumns

Spectrum provides special pseudocolumns for external tables:

| Pseudocolumn | Description |
|---|---|
| `$path` | Full S3 path of the file containing the row |
| `$size` | Size of the S3 file in bytes |
| `$spectrum_oid` | Unique identifier for the row [INFERRED — not commonly used] |

```sql
SELECT "$path", "$size", order_id, amount
FROM spectrum_schema.orders
WHERE year = 2024 AND month = 1
LIMIT 10;
```

### Limits

| Limit | Value |
|---|---|
| Max columns per external table | 1,597 (with pseudocolumns) / 1,600 (without) |
| Max partitions per ALTER TABLE ADD PARTITION | 100 |

---

## 4. Predicate Pushdown and Column Pruning

### Predicate Pushdown

```sql
SELECT customer_id, SUM(amount)
FROM spectrum_schema.orders
WHERE year = 2024 AND month = 6 AND amount > 1000
GROUP BY customer_id;
```

**What happens:**

```
Step 1 — Partition pruning (Glue Catalog):
  Only read partition year=2024/month=06
  Skip: all other year/month partitions
  Savings: If 48 months of data → read 1/48 = 2%

Step 2 — Column pruning (Spectrum):
  Parquet file has 10 columns
  Query needs: customer_id, amount
  Read only these 2 column groups from Parquet
  Savings: Read 2/10 = 20% of each file

Step 3 — Row filtering (Spectrum):
  Apply WHERE amount > 1000 on Spectrum nodes
  Only rows matching this predicate are sent to compute nodes
  If 5% of rows have amount > 1000 → 95% filtered out

Total data reduction: 2% × 20% × 5% = 0.02% of total data sent to compute nodes
```

### What Can Be Pushed Down

| Predicate Type | Pushdown? | Example |
|---|---|---|
| Partition column equality | **Yes** | `WHERE year = 2024` |
| Partition column range | **Yes** | `WHERE year BETWEEN 2023 AND 2024` |
| Simple column predicates | **Yes** | `WHERE amount > 1000` |
| String predicates | **Yes** | `WHERE status = 'COMPLETED'` |
| IS NULL / IS NOT NULL | **Yes** | `WHERE region IS NOT NULL` |
| LIKE (prefix) | **Partial** | `WHERE name LIKE 'Al%'` may push down |
| Complex expressions | **No** | `WHERE func(amount) > 1000` |
| Subqueries | **No** | `WHERE id IN (SELECT ...)` |

---

## 5. Partitioning: The Key to Spectrum Performance

### Why Partitioning Is Critical

Without partitioning, Spectrum must **list and scan every file** in the S3 location. With partitioning, Spectrum only reads files in matching partitions.

```
WITHOUT partitioning:
  s3://bucket/orders/
    file001.parquet (1 GB)
    file002.parquet (1 GB)
    ...
    file3650.parquet (1 GB)  ← 10 years of data, 3.65 TB total

  Query: WHERE date = '2024-06-15'
  Spectrum must scan ALL 3,650 files (3.65 TB) to find matching rows
  Cost: $5.00/TB × 3.65 TB = $18.25 per query!

WITH partitioning (by year/month/day):
  s3://bucket/orders/year=2024/month=06/day=15/
    file001.parquet (100 MB)

  Query: WHERE year = 2024 AND month = 6 AND day = 15
  Spectrum reads ONLY the matching partition: 100 MB
  Cost: $5.00/TB × 0.0001 TB = $0.0005 per query!

  Cost reduction: 36,500× cheaper
```

### Partitioning Strategies

| Strategy | Partition Columns | Best For |
|---|---|---|
| **Date-based** | year, month, day | Time-series data (logs, events, transactions) |
| **Date + category** | year, month, region | Data queried by time AND category |
| **Category only** | region, department | Data not time-ordered but always filtered by category |

### Partition Granularity Tradeoffs

| Granularity | Pros | Cons |
|---|---|---|
| **Year** | Few partitions, large files (good for Parquet) | Coarse filtering — reads unnecessary months |
| **Year/Month** | Good balance | 12 partitions per year |
| **Year/Month/Day** | Fine-grained filtering | 365 partitions/year; many small files if daily data is small |
| **Year/Month/Day/Hour** | Very fine for high-volume streams | Risk of tiny files (< 128 MB → inefficient for Parquet) |

**Rule of thumb**: Each partition should contain at least 128 MB of data for Parquet to be efficient. If partitions are too small, the overhead of reading many small files dominates.

---

## 6. File Formats: Columnar vs Row-Oriented

### Supported Formats

| Format | Type | Column Pruning | Compression | Recommended |
|---|---|---|---|---|
| **Parquet** | Columnar | **Yes** | Snappy, GZIP, LZO | **Best default** |
| **ORC** | Columnar | **Yes** | ZLIB, Snappy | Good alternative |
| **Avro** | Row-oriented (schema) | No | Snappy, Deflate | For schema evolution needs |
| **CSV** | Row-oriented | No | GZIP | Legacy; avoid for analytics |
| **JSON** | Row-oriented | No | GZIP | Semi-structured; slow |
| **Ion** | Row-oriented | No | GZIP | AWS-native semi-structured |
| **Hudi** | Columnar (with updates) | **Yes** | Snappy | For upsert workloads on data lake |
| **Delta Lake** | Columnar (with ACID) | **Yes** | Snappy | For ACID on data lake |

### Why Columnar Formats Matter for Spectrum

```
Parquet file (10 columns, 1 GB):
  Query needs 2 columns → reads 200 MB (column pruning)
  + Parquet has row group statistics → predicate pushdown
  + Snappy compression → maybe 150 MB actual S3 reads

CSV file (10 columns, 1 GB):
  Query needs 2 columns → reads ALL 1 GB (no column pruning)
  + GZIP compression → maybe 300 MB actual S3 reads
  + No statistics → no predicate pushdown

Parquet is 2-5× cheaper and faster than CSV for Spectrum queries.
```

### Parquet Internals (Relevant to Spectrum)

```
Parquet File Layout:
┌──────────────────────────┐
│ Row Group 1 (128 MB)      │
│   Column Chunk: id         │ ← Spectrum reads only needed column chunks
│   Column Chunk: name       │
│   Column Chunk: amount     │ ← If query needs 'amount', read this chunk
│   Column Chunk: region     │
│   ...                      │
│   Footer: min/max stats    │ ← Spectrum uses for predicate pushdown
├──────────────────────────┤
│ Row Group 2 (128 MB)      │
│   ...                      │
├──────────────────────────┤
│ File Footer                │
│   Schema                   │
│   Row group metadata       │
│   Column statistics        │
└──────────────────────────┘
```

Parquet row group statistics serve a similar role to Redshift's zone maps — they enable block-level predicate skipping.

---

## 7. Joining Local and External Tables

### The Hybrid Query Pattern

```sql
-- Local Redshift table: customers (small, frequently accessed)
-- External S3 table: orders (massive, historical)

SELECT c.name, c.region, SUM(o.amount) as total_spent
FROM spectrum_schema.orders o        -- S3 via Spectrum
JOIN public.customers c               -- local Redshift table
ON o.customer_id = c.id
WHERE o.year = 2024
GROUP BY c.name, c.region
ORDER BY total_spent DESC
LIMIT 100;
```

### Execution Flow

```
1. Leader Node: Build query plan
   - Identify spectrum_schema.orders as external (Spectrum)
   - Identify public.customers as local (compute nodes)

2. Spectrum Layer: Process external table
   - Partition prune: only year=2024
   - Column prune: only customer_id, amount
   - Row filter: (none beyond partition)
   - Partial aggregate: (if possible)
   - Return filtered data to compute nodes

3. Compute Nodes: Process local table
   - Scan customers table from local columnar storage
   - Build hash table on customers.id

4. Compute Nodes: Join
   - Probe hash table with Spectrum results (customer_id)
   - Apply GROUP BY, SUM aggregation

5. Leader: Final merge
   - ORDER BY total_spent DESC
   - LIMIT 100
   - Return to client
```

### Optimization Tips for Hybrid Queries

| Tip | Rationale |
|---|---|
| Keep frequently joined dimension tables LOCAL in Redshift | Avoids Spectrum overhead for small, hot tables |
| Keep massive historical fact tables in S3 (Spectrum) | Cost-effective for petabytes of cold data |
| Always partition external tables | Spectrum performance = f(data scanned), not f(total data) |
| Use columnar formats (Parquet/ORC) in S3 | Enables column pruning on the Spectrum side |
| Push filters to WHERE clause (not HAVING) | WHERE enables predicate pushdown; HAVING does not |

---

## 8. Data Sharing: Cross-Cluster Access

### Producer-Consumer Model

```
┌───────────────────────────────────────────────┐
│ Producer Cluster (ETL team)                     │
│                                                 │
│ CREATE DATASHARE ds_analytics;                  │
│ ALTER DATASHARE ds_analytics                    │
│   ADD SCHEMA public;                            │
│ ALTER DATASHARE ds_analytics                    │
│   ADD TABLE public.orders;                      │
│ ALTER DATASHARE ds_analytics                    │
│   ADD TABLE public.customers;                   │
│                                                 │
│ GRANT USAGE ON DATASHARE ds_analytics           │
│   TO ACCOUNT '111222333444';                    │
└───────────────────────────────────────────────┘
                    │
                    │ Live read access (no data copy)
                    ▼
┌───────────────────────────────────────────────┐
│ Consumer Cluster (BI team, different account)   │
│                                                 │
│ CREATE DATABASE analytics_db                    │
│   FROM DATASHARE ds_analytics                   │
│   OF ACCOUNT '999888777666';                    │
│                                                 │
│ SELECT * FROM analytics_db.public.orders        │
│   WHERE order_date > '2024-01-01';              │
│                                                 │
│ -- Reads LIVE data from producer's managed      │
│ -- storage. No ETL, no copying.                 │
└───────────────────────────────────────────────┘
```

### Data Sharing Properties

| Property | Detail |
|---|---|
| **Data movement** | None — consumer reads from producer's Redshift Managed Storage |
| **Consistency** | Strong — consumer within a transaction sees consistent snapshot |
| **Latency** | Live — producer commits are visible to new consumer transactions immediately |
| **Shareable objects** | Databases, schemas, tables, views (regular, late-binding, materialized), SQL UDFs |
| **Cross-account** | Yes — producer grants access to specific AWS accounts |
| **Cross-region** | Yes — supported across regions |
| **Write access** | Yes — consumers can INSERT/UPDATE shared objects |
| **Provisioned ↔ Serverless** | Supported — share between provisioned clusters and Serverless workgroups |

### Use Cases

| Use Case | Architecture |
|---|---|
| **Workload isolation** | ETL cluster (producer) shares curated data with BI cluster (consumer). BI queries don't impact ETL. |
| **Multi-team access** | Central data team shares trusted datasets with data science, finance, marketing teams (each with their own cluster). |
| **AWS Data Exchange** | Publish datashares as commercial data products on AWS Data Exchange. |
| **Dev/staging/prod isolation** | Prod cluster shares data with dev/staging for testing without copying. |

### Data Sharing vs Spectrum vs COPY

| Approach | Data Location | Latency | Management |
|---|---|---|---|
| **Data Sharing** | Producer's Managed Storage (S3) | Live, no delay | Zero — no ETL |
| **Spectrum** | Your S3 bucket (external tables) | Depends on S3 scan speed | Manage external tables, Glue Catalog |
| **COPY** | Loaded into Redshift local tables | Fastest query performance | ETL pipeline to load, schedule, manage |

---

## 9. Materialized Views

### How Materialized Views Work

```sql
-- Create a materialized view
CREATE MATERIALIZED VIEW mv_daily_sales AS
SELECT order_date, region, SUM(amount) as total, COUNT(*) as order_count
FROM orders
GROUP BY order_date, region;

-- Query the MV directly
SELECT * FROM mv_daily_sales WHERE region = 'US';

-- Or Redshift may auto-rewrite your query to use the MV
SELECT order_date, SUM(amount) FROM orders WHERE region = 'US' GROUP BY order_date;
-- ↑ Optimizer may transparently rewrite this to use mv_daily_sales
```

### Refresh Strategies

| Strategy | Command | When to Use |
|---|---|---|
| **Manual** | `REFRESH MATERIALIZED VIEW mv_name;` | Full control over timing |
| **Automatic (autorefresh)** | Set during creation or ALTER | When you need data freshness without manual intervention |
| **Scheduled** | Via Redshift Scheduler API | When you need refresh at specific times (e.g., after nightly ETL) |

**Incremental refresh**: Redshift detects which base table rows changed and applies only the delta to the MV. Much faster than full recomputation.

### Automatic Query Rewriting

The optimizer can transparently rewrite queries to use MVs:

```
User's query: SELECT region, SUM(amount) FROM orders GROUP BY region

Optimizer detects: mv_daily_sales has region, SUM(amount), grouped by (order_date, region)
                   We can further aggregate mv_daily_sales to get region-level totals!

Rewritten query: SELECT region, SUM(total) FROM mv_daily_sales GROUP BY region

This reads far fewer rows (365 days × 10 regions = 3,650 rows)
instead of scanning the full orders table (10 billion rows).
```

**Requirements for automatic rewriting:**
- MV must be up-to-date (recently refreshed)
- Query must be logically equivalent to a transformation of the MV
- MV must cover all needed columns

### Automated Materialized Views (Auto MV)

Redshift monitors query patterns and **automatically creates** materialized views for frequently-run queries:

1. Identifies repeated expensive queries
2. Creates MVs that would speed them up
3. Manages refresh automatically
4. Drops MVs that are no longer useful

Zero manual intervention. Useful for BI dashboards with predictable query patterns.

### Nested Materialized Views

```sql
-- Base MV: expensive join, computed once
CREATE MATERIALIZED VIEW mv_order_details AS
SELECT o.order_id, o.amount, o.order_date, c.name, c.region
FROM orders o
JOIN customers c ON o.customer_id = c.id;

-- Upper MV 1: daily summary by region
CREATE MATERIALIZED VIEW mv_daily_regional AS
SELECT order_date, region, SUM(amount) as total
FROM mv_order_details
GROUP BY order_date, region;

-- Upper MV 2: monthly summary by customer
CREATE MATERIALIZED VIEW mv_monthly_customer AS
SELECT DATE_TRUNC('month', order_date) as month, name, SUM(amount) as total
FROM mv_order_details
GROUP BY 1, 2;
```

The expensive join (orders × customers) is computed once in the base MV. Multiple upper MVs reuse the join result for different aggregations.

---

## 10. Durability, Backups, and Recovery

### RA3 Durability Model

```
┌─────────────────────────────────┐
│   RA3 Node (Compute)             │
│   Local SSD = CACHE only         │
│   If node fails: replace node,   │
│   rebuild cache from RMS         │
│   Data loss: ZERO                │
└──────────────┬──────────────────┘
               │
               ▼
┌─────────────────────────────────┐
│   Redshift Managed Storage (RMS) │
│   Backed by S3                   │
│   11 9's durability              │
│   Multi-AZ replication           │
│   This IS the source of truth    │
└─────────────────────────────────┘
```

### DC2 Durability Model

```
┌─────────────────────────────────┐
│   DC2 Node (Compute + Storage)   │
│   Local NVMe SSD = PRIMARY       │
│   Each block replicated to       │
│   another node in the cluster    │
│   If one node fails: replica     │
│   serves reads                    │
│   If cluster lost: restore       │
│   from snapshot                   │
└─────────────────────────────────┘
```

### Snapshot Strategy

| Type | Frequency | Retention | Behavior |
|---|---|---|---|
| **Automated** | Every 8 hrs or 5 GB/node change (whichever first) | 1-35 days (RA3); default 1 day | Cannot be disabled for RA3 |
| **Manual** | On-demand | **Indefinite** (persists after cluster deletion) | Must manually delete |
| **Cross-region copy** | Configurable | Configurable | For DR across regions |

**Snapshots are incremental**: Only changed blocks since the last snapshot are backed up. This makes frequent snapshots efficient.

**Restore behavior**: A new cluster is created. Data **streams on-demand** — you can start querying immediately. Remaining data loads in the background. No need to wait for full restore.

### Point-in-Time Recovery

With automated snapshots and their frequency (as often as every 15 minutes for high-change workloads), effective point-in-time recovery is possible within the retention window.

---

## 11. Design Decisions & Tradeoffs

### Load into Redshift vs Query via Spectrum

| Factor | Load into Redshift | Query via Spectrum |
|---|---|---|
| **Query latency** | **Sub-second to seconds** | Seconds to minutes |
| **Query cost** | Included in cluster cost | $5 per TB scanned |
| **Data freshness** | Delayed (ETL latency) | **Live** (reads S3 directly) |
| **Storage cost** | RMS pricing | **S3 pricing** (cheapest) |
| **Data volume limit** | Limited by managed storage | **Unlimited** (S3) |
| **Zone maps** | **Yes** (sort key + zone maps) | Row group stats in Parquet (less effective) |
| **Compression** | **Redshift-optimized** (AZ64, etc.) | File-level (Snappy, GZIP) |
| **Shared with other engines** | No (Redshift-only) | **Yes** (Athena, EMR, Glue) |

**Decision heuristic:**
- Query runs > 3× per day → load into Redshift (amortize ETL cost)
- Query runs < 1× per week → Spectrum (avoid ETL cost)
- Data shared with Athena/EMR → keep in S3 (Spectrum)
- Need sub-second latency → load into Redshift

### Redshift vs Athena

| Factor | Redshift | Athena |
|---|---|---|
| **Architecture** | Provisioned/Serverless MPP cluster | Serverless, per-query |
| **Pricing** | Node-hour or RPU-hour | $5 per TB scanned |
| **Best for** | Complex queries, high concurrency, dashboards | Ad-hoc exploration, infrequent queries |
| **Latency** | **Sub-second** (local data, result cache) | 3-30 seconds (S3 scan overhead) |
| **Concurrency** | Hundreds with concurrency scaling | Moderate (account-level limits) |
| **Data location** | Redshift Managed Storage + S3 (Spectrum) | S3 only |
| **Optimization** | Sort keys, distribution, zone maps, MVs | Partitioning, columnar format |
| **Management** | Some (unless Serverless) | **Zero** |

### Redshift vs Snowflake vs BigQuery

| Factor | Redshift | Snowflake | BigQuery |
|---|---|---|---|
| **Cloud** | AWS only | Multi-cloud | GCP only |
| **Compute-storage separation** | RA3 (newer) | **Day-one design** | **Day-one design** |
| **Pricing** | Node-hour / RPU-hour | Credit-based | $5/TB scanned (on-demand) or slots |
| **Ecosystem** | Deep AWS integration (S3, Glue, SageMaker, QuickSight) | Cloud-agnostic | Deep GCP integration |
| **Concurrency** | Concurrency scaling (10 clusters) | Multi-cluster warehouse (unlimited) | **Automatic** |
| **Data sharing** | Datashares (same + cross-account) | Secure Data Sharing | BigQuery Omni, Analytics Hub |
| **Semi-structured** | Limited (SUPER type) | **Native (VARIANT)** | **Native (STRUCT, ARRAY)** |

### The Data Tiering Strategy

```
┌─────────────────────────────────────────────────┐
│ HOT (local Redshift tables)                       │
│ - Last 90 days of data                            │
│ - Sort keys, distribution optimized               │
│ - Sub-second queries, result caching              │
│ - 10 TB                                          │
├─────────────────────────────────────────────────┤
│ WARM (S3 Parquet via Spectrum)                    │
│ - 90 days to 2 years                             │
│ - Partitioned by date, Parquet format             │
│ - Seconds query latency                          │
│ - 100 TB                                         │
├─────────────────────────────────────────────────┤
│ COLD (S3 Glacier via lifecycle)                   │
│ - Over 2 years                                    │
│ - Archive, not queryable without restore          │
│ - Cheapest storage                                │
│ - 500 TB                                         │
└─────────────────────────────────────────────────┘

ETL pipeline moves data between tiers based on age.
Single SQL interface queries hot + warm seamlessly.
```

This tiered approach gives the best balance of performance, cost, and storage scale for enterprise data warehouses.
