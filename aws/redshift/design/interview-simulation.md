# System Design Interview Simulation: Design Amazon Redshift (Cloud Data Warehouse)

> **Interviewer:** Principal Engineer (L8), Amazon Redshift Team
> **Candidate Level:** SDE-3 (L6 — Senior Software Development Engineer)
> **Duration:** ~60 minutes
> **Date:** February 13, 2026

---

## PHASE 1: Opening & Problem Statement (~2 min)

**Interviewer:**
Welcome. I'm on the Redshift team — Principal Engineer. Today I'd like you to design a **cloud data warehouse** — think Amazon Redshift. A system where customers load structured data at petabyte scale and run complex analytical SQL queries over it. We're talking about the core query engine and storage layer, not just a managed service wrapper.

I care about how you reason about columnar storage, massively parallel processing, and the tradeoffs of distributing data across a cluster. I'll push on your decisions — that's calibration, not a negative signal.

Take it away.

---

## PHASE 2: Requirements Gathering & Scoping (~5 min)

**Candidate:**
Thanks. "Cloud data warehouse" is a wide space — let me scope what we're building before drawing anything.

**Functional Requirements — what operations do we need?**

> "At its core, this is a SQL-over-big-data system. The key operations:
>
> - **CREATE TABLE / DROP TABLE** — define schemas for structured data (unlike S3 which is schema-on-read, a warehouse is schema-on-write)
> - **COPY / INSERT** — bulk-load data from external sources (S3, DynamoDB, streaming) and single-row inserts
> - **SELECT with JOINs, GROUP BY, aggregations** — the bread and butter: analytical queries scanning billions of rows, joining large tables, computing aggregates
> - **CREATE MATERIALIZED VIEW** — precomputed query results for repeated analytics
> - **UNLOAD** — export query results back to S3
>
> A few clarifying questions:
> - **Are we designing for OLAP (analytics) or OLTP (transactions)?**"

**Interviewer:** "This is purely OLAP. No single-row point lookups at high concurrency. Think BI dashboards, ETL pipelines, ad-hoc analyst queries."

> "- **What's the data model?** Star schema / snowflake schema with fact and dimension tables?"

**Interviewer:** "Yes, customers typically use star schemas — large fact tables (billions of rows) joined with smaller dimension tables (millions of rows). But don't over-constrain yourself — some customers denormalize everything into wide tables."

> "- **Do we need to support querying data that lives outside the warehouse, e.g., directly in S3?**"

**Interviewer:** "Mention it — that's Spectrum. But focus on the core engine first."

> "- **Is this a provisioned cluster or serverless model?**"

**Interviewer:** "Start with provisioned clusters — a fixed set of nodes. We can discuss serverless later."

**Non-Functional Requirements:**

> "The non-functional requirements for a data warehouse are fundamentally different from OLTP databases:
>
> | Dimension | Requirement | Rationale |
> |---|---|---|
> | **Query Performance** | Sub-second to minutes for analytical queries over billions of rows | BI dashboards need fast response; complex ETL can take minutes |
> | **Scale** | Petabytes of structured data | Enterprise data warehouses store years of transactional history |
> | **Throughput** | Hundreds of concurrent queries | Multiple analysts + BI tools + ETL jobs hitting the warehouse simultaneously |
> | **Durability** | No data loss — continuous backups | Warehouse is the source of truth for analytics; losing it means re-running ETL pipelines |
> | **Availability** | 99.9%+ | Analytics can tolerate brief outages but not frequent ones |
> | **Cost Efficiency** | 1/10th the cost of traditional data warehouses | This was Redshift's original value prop vs Teradata/Oracle |
> | **SQL Compatibility** | Standard SQL with window functions, CTEs, subqueries | Analysts use SQL — can't require them to learn a new language |
> | **Compression** | 3-10x compression ratio | Columnar storage + encoding should dramatically reduce storage costs |
>
> A critical distinction from OLTP: we optimize for **scan throughput** (reading millions of rows fast), not **point-lookup latency** (reading one row fast). This drives every architectural decision."

**Interviewer:**
Good. You mentioned cost efficiency. Redshift launched in 2013 and was genuinely 1/10th the cost of Teradata. What architectural decisions make that possible?

**Candidate:**

> "Three things:
> 1. **Commodity hardware** instead of specialized appliances — standard EC2 instances with local SSDs or managed S3 storage
> 2. **Columnar storage with compression** — an analytical query touching 3 out of 100 columns reads 3% of the data, and compression further reduces it by 3-10x
> 3. **Separation of compute and storage** (with RA3 nodes) — you don't need to keep hot compute attached to cold data. Store everything in S3, cache hot data locally.
>
> Together, these mean a petabyte warehouse on Redshift costs ~$1,000/TB/year vs $10,000-50,000/TB/year for legacy appliances."

---

### L5 vs L6 vs L7 — Phase 2 Rubric

| Aspect | L5 (SDE-2) | L6 (SDE-3) | L7 (Principal) |
|---|---|---|---|
| **Functional Reqs** | Lists CRUD + SELECT operations | Proactively distinguishes OLAP vs OLTP, raises Spectrum, materialized views, bulk loading | Additionally discusses cross-cluster data sharing, federated queries, streaming ingestion |
| **Non-Functional** | Mentions performance and scale | Quantifies compression ratios, explains why scan throughput matters more than point-lookup latency | Frames NFRs as cost-per-TB economics, compares with Teradata/Snowflake/BigQuery business models |
| **Scoping** | Accepts problem as given | Drives clarifying questions about workload type and data model | Negotiates scope by proposing phased approach: core engine → distribution → query optimization → external data |

---

## PHASE 3: Scale Estimation & API Design (~5 min)

**Candidate:**

> "Let me estimate warehouse-scale numbers."

#### Storage Estimates

> "A large enterprise Redshift deployment:
>
> - **Raw data**: 500 TB of structured data (fact + dimension tables)
> - **Columns per table**: average 50 columns, some wide tables with 500+
> - **Rows in largest fact table**: 10 billion rows
> - **With columnar compression** (~4x average): **125 TB** actual storage
> - **Number of tables**: 1,000-10,000 tables across 20-50 schemas
>
> Redshift supports up to 128 nodes per cluster. An ra3.16xlarge node has 16 slices, so a max cluster has 128 × 16 = **2,048 slices** of parallelism."

#### Query Traffic

> "- **Concurrent queries**: 50-500 at peak (not millions like OLTP — each query is heavy)
> - **Data scanned per query**: 1 GB to 1 TB (depending on query complexity and zone map effectiveness)
> - **Query latency**: 1 second (simple dashboard) to 10 minutes (complex ETL)
> - **Bulk loads**: COPY from S3, 1-100 GB per load, happening every 15 min to hourly"

#### Key API Operations

> "The primary interface is SQL over JDBC/ODBC, but the system APIs include:
>
> | Operation | Description | Performance Target |
> |---|---|---|
> | `COPY FROM s3://...` | Bulk load from S3 (parallel, compressed) | 1 GB/sec per node throughput |
> | `SELECT ... JOIN ... GROUP BY` | Analytical query | Sub-second to minutes |
> | `UNLOAD TO s3://...` | Export results to S3 | Parallel write across slices |
> | `CREATE TABLE ... DISTKEY() SORTKEY()` | Define table with distribution + sort | Metadata operation |
> | `VACUUM` | Reclaim space and re-sort after loads | Background maintenance |
> | `ANALYZE` | Update table statistics for query optimizer | Background maintenance |
>
> The client connects to the **leader node** via PostgreSQL wire protocol (JDBC/ODBC on port 5439). Redshift is based on PostgreSQL 8.0.2 but with significant modifications for columnar MPP."

**Interviewer:**
Port 5439 specifically — good. Why PostgreSQL wire protocol and not a custom protocol?

**Candidate:**

> "Compatibility. Every BI tool (Tableau, Looker, QuickSight, Power BI), every ETL framework (Informatica, dbt, Airflow), and every SQL client already speaks PostgreSQL wire protocol. By reusing it, Redshift gets instant ecosystem compatibility on day one. The alternative — a custom protocol — would require every tool vendor to build a Redshift-specific driver. That's a go-to-market death sentence for a new data warehouse.
>
> The tradeoff is that PostgreSQL protocol has limitations — it's single-threaded per connection, which is why Redshift uses result caching and materialized views rather than trying to push massive result sets through a single TCP connection."

---

### L5 vs L6 vs L7 — Phase 3 Rubric

| Aspect | L5 (SDE-2) | L6 (SDE-3) | L7 (Principal) |
|---|---|---|---|
| **Scale Numbers** | Mentions "big data" generically | Quantifies: 500 TB raw, 125 TB compressed, 2,048 slices, 50-500 concurrent queries | Also estimates cost per query, network bandwidth between leader and compute nodes, metadata size |
| **API Design** | Lists SQL operations | Distinguishes COPY (bulk) vs INSERT (row), explains PostgreSQL wire protocol choice | Also discusses Data API (HTTP-based async), discusses why streaming ingestion needs different path |
| **Tradeoffs** | None raised | Explains PostgreSQL protocol tradeoff (compatibility vs performance) | Quantifies protocol overhead, compares with Arrow Flight / gRPC alternatives |

---

## PHASE 4: High-Level Architecture (Iterative Build-Up) (~10 min)

**Candidate:**

> "Let me start with the simplest thing that works, find the problems, and evolve."

### Attempt 0: Single PostgreSQL Instance

> "The simplest analytical database: one PostgreSQL server.
>
> ```
>     BI Tool / Analyst
>          │
>          ▼
>   ┌─────────────────────┐
>   │  Single PostgreSQL   │
>   │                      │
>   │  Row-oriented tables │
>   │  on local disk       │
>   │                      │
>   │  SELECT sum(amount)  │
>   │  FROM orders         │
>   │  WHERE date > '2024' │
>   │  GROUP BY region;    │
>   │                      │
>   │  Local SSD (2 TB)    │
>   └─────────────────────┘
> ```
>
> This works for small datasets — maybe up to 100 GB. But it has fundamental problems."

**Interviewer:**
What problems?

**Candidate:**

> "Three showstoppers:
>
> 1. **Capacity wall**: A single server maxes out at a few TB. Our warehouse has 500 TB of data.
>
> 2. **Query performance**: A row-oriented database stores data row-by-row on disk. For `SELECT sum(amount) FROM orders WHERE date > '2024'`, PostgreSQL reads the entire row (all 50 columns) even though the query only needs 2 columns (`amount` and `date`). At 500 TB, scanning all columns takes orders of magnitude longer than necessary.
>
> 3. **No parallelism**: One CPU processes the query. Modern analytics needs to scan billions of rows — we need multiple CPUs working in parallel.
>
> The first problem (capacity) motivates **distributed processing (MPP)**. The second problem (row-oriented waste) motivates **columnar storage**. Let me fix them one at a time."

### Attempt 1: Distribute Data Across Nodes (MPP)

> "Split the data across multiple machines. Each machine stores a portion and processes queries on its portion in parallel.
>
> ```
>     BI Tool / Analyst
>          │
>          ▼
>   ┌─────────────────────┐
>   │    Leader Node       │
>   │  (Query coordinator) │
>   │  - Parses SQL        │
>   │  - Builds query plan │
>   │  - Distributes work  │
>   │  - Aggregates results│
>   └─────┬───┬───┬────────┘
>         │   │   │
>    ┌────┘   │   └────┐
>    ▼        ▼        ▼
> ┌──────┐ ┌──────┐ ┌──────┐
> │Comp  │ │Comp  │ │Comp  │
> │Node 1│ │Node 2│ │Node 3│
> │      │ │      │ │      │
> │Rows  │ │Rows  │ │Rows  │
> │1-33M │ │34-66M│ │67-99M│
> └──────┘ └──────┘ └──────┘
> ```
>
> **How it works:**
> - The **leader node** receives the SQL, parses it, builds a query plan, and decides how to distribute work
> - Each **compute node** stores a subset of the rows and executes its portion of the query in parallel
> - The leader node **aggregates** partial results from all compute nodes and returns the final result
>
> This is the MPP (Massively Parallel Processing) architecture. A `SUM(amount) GROUP BY region` becomes:
> 1. Each compute node computes partial sums for regions in its data
> 2. Leader node merges the partial sums
>
> **Problem**: The data is still stored row-by-row on each compute node. That `SELECT sum(amount)` still reads all 50 columns on each node."

### Attempt 2: Columnar Storage

> "Instead of storing data row-by-row, store it column-by-column.
>
> **Row-oriented** (PostgreSQL, MySQL):
> ```
> Row 1: [id=1, name='Alice', region='US', amount=100, date='2024-01-01', ... 45 more columns]
> Row 2: [id=2, name='Bob',   region='EU', amount=200, date='2024-01-02', ... 45 more columns]
> ```
>
> **Column-oriented** (Redshift):
> ```
> Column 'id':     [1, 2, 3, 4, ...]       → stored in 1 MB blocks
> Column 'name':   ['Alice', 'Bob', ...]    → stored in 1 MB blocks
> Column 'region': ['US', 'EU', ...]        → stored in 1 MB blocks
> Column 'amount': [100, 200, ...]          → stored in 1 MB blocks
> Column 'date':   ['2024-01-01', ...]      → stored in 1 MB blocks
> ```
>
> **Why this is transformative for analytics:**
>
> | Metric | Row-Oriented | Column-Oriented |
> |---|---|---|
> | `SELECT sum(amount) WHERE date > '2024'` | Reads **all 50 columns** (100% of data) | Reads **only amount + date** (4% of data) |
> | Compression | Low (mixed data types per row) | **High** (same data type per column — integers compress well together) |
> | Compression ratio | 1.5-2x | **3-10x** (Redshift uses AZ64, LZO, ZSTD, Delta, RunLength, etc.) |
> | Blocks read for 3-column query on 100-column table | 100% of blocks | **3% of blocks** |
>
> Redshift stores data in **1 MB blocks**, one block per column per set of rows. Each block has a **zone map** — a min/max metadata record. If a query has `WHERE date > '2024-06-01'` and a block's zone map says max(date) = '2024-03-31', that entire 1 MB block is skipped. This can eliminate 95-98% of blocks for range-filtered queries on sorted data.
>
> **Now our architecture looks like:**
>
> ```
>     BI Tool / Analyst
>          │
>          ▼
>   ┌──────────────────────────┐
>   │       Leader Node         │
>   │  - SQL parsing            │
>   │  - Query optimization     │
>   │  - Execution plan → code  │
>   │  - Result aggregation     │
>   └─────┬───────┬───────┬────┘
>         │       │       │
>    ┌────┘       │       └────┐
>    ▼            ▼            ▼
> ┌─────────┐ ┌─────────┐ ┌─────────┐
> │ Compute │ │ Compute │ │ Compute │
> │ Node 1  │ │ Node 2  │ │ Node 3  │
> │         │ │         │ │         │
> │ Slice 0 │ │ Slice 2 │ │ Slice 4 │
> │ Slice 1 │ │ Slice 3 │ │ Slice 5 │
> │         │ │         │ │         │
> │ Columnar│ │ Columnar│ │ Columnar│
> │ 1MB blks│ │ 1MB blks│ │ 1MB blks│
> │ +zone   │ │ +zone   │ │ +zone   │
> │  maps   │ │  maps   │ │  maps   │
> └─────────┘ └─────────┘ └─────────┘
> ```
>
> Each compute node is divided into **slices** — the unit of parallelism. Each slice has its own memory, disk, and CPU allocation. For an ra3.4xlarge node, there are 4 slices per node. For ra3.16xlarge, 16 slices per node. The table data is distributed across slices — each slice owns a subset of the rows."

**Interviewer:**
Good evolution. You've solved two problems — capacity and scan efficiency. But I see a join problem. Walk me through what happens when you join two tables that are on different nodes.

**Candidate:**

> "This is the critical challenge of MPP. Let me illustrate:
>
> ```sql
> SELECT c.name, sum(o.amount)
> FROM orders o
> JOIN customers c ON o.customer_id = c.id
> GROUP BY c.name;
> ```
>
> If `orders` is on Node 1 and `customers` is on Node 3, the join can't happen locally — data must move across the network. This **data redistribution** is expensive.
>
> There are two redistribution strategies:
> 1. **Hash redistribution**: Both tables are re-distributed on the join key so matching rows land on the same node. Cost: O(rows in both tables).
> 2. **Broadcast**: The smaller table (customers) is copied to every node. Cost: O(rows in small table × number of nodes).
>
> Both are expensive. The solution is to avoid redistribution entirely by **co-locating data** using distribution keys. This is where distribution styles come in — I'll dive deeper in the next phase."

---

### Architecture Evolution Table — Phase 4

| Version | Architecture | Solved | New Problem |
|---|---|---|---|
| Attempt 0 | Single PostgreSQL | Works for small data | Capacity wall (TB limit), full table scans, no parallelism |
| Attempt 1 | MPP (leader + compute nodes), row-oriented | Distributes data, parallel processing | Row-oriented storage reads unnecessary columns |
| Attempt 2 | MPP + columnar storage + zone maps | Only reads needed columns, 3-10x compression, block-level skipping | Joins require expensive cross-node data redistribution |
| Next | + Distribution keys | Co-locate join data on same node | Need sort keys for efficient range scans |

---

### L5 vs L6 vs L7 — Phase 4 Rubric

| Aspect | L5 (SDE-2) | L6 (SDE-3) | L7 (Principal) |
|---|---|---|---|
| **Architecture Evolution** | Jumps to MPP + columnar immediately | Builds iteratively: single node → MPP → columnar, explains WHY each change | Additionally models the network cost of redistribution quantitatively (bytes/sec across cluster) |
| **Columnar Storage** | "It stores by column" | Explains 1 MB blocks, zone maps, compression types, quantifies 3-10x compression | Discusses block encoding selection (AZ64 for timestamps, Delta for sequential IDs, RunLength for low-cardinality), storage layout on disk |
| **Join Problem** | Doesn't identify it | Explains hash redistribution vs broadcast, introduces distribution keys | Quantifies cost: "broadcasting 10M row dimension table to 128 nodes = 128 × 10M × 100B = 128 GB of network traffic" |
| **Leader/Compute Split** | Mentions coordinator | Explains query plan → code compilation → distribution to slices | Discusses leader as bottleneck: result aggregation, single-threaded plan generation, memory limits for large result sets |

---

## PHASE 5: Deep Dive — Data Distribution (~8 min)

**Interviewer:**
You mentioned distribution keys. This is the single most impactful decision a Redshift user makes. Walk me through the options.

**Candidate:**

> "Data distribution determines how table rows are assigned to slices across the cluster. Redshift offers four distribution styles:
>
> ### 1. KEY Distribution
> ```sql
> CREATE TABLE orders (
>   order_id    BIGINT,
>   customer_id BIGINT,
>   amount      DECIMAL(10,2)
> )
> DISTSTYLE KEY
> DISTKEY (customer_id);
> ```
>
> Rows are hash-distributed on the DISTKEY column. All rows with `customer_id = 42` land on the same slice. If `customers` is also distributed on `id`, then a JOIN on `customer_id = id` is **co-located** — no redistribution needed.
>
> **When to use:** For large tables that are frequently joined. The DISTKEY should be the most common join column.
>
> **Risk:** Skewed distribution. If 40% of orders belong to one customer, one slice gets 40% of the data while others sit idle. This creates a **hot slice** — the query is only as fast as the slowest slice.
>
> ### 2. EVEN Distribution
> ```sql
> CREATE TABLE events (...)
> DISTSTYLE EVEN;
> ```
>
> Round-robin distribution — each row goes to the next slice in sequence. Guarantees perfectly uniform distribution.
>
> **When to use:** For tables that don't participate in joins, or when no single column is a good distribution key. Staging tables are a common use case.
>
> **Cost:** Every join requires redistribution since there's no co-location guarantee.
>
> ### 3. ALL Distribution
> ```sql
> CREATE TABLE regions (
>   region_id   INT,
>   region_name VARCHAR(50)
> )
> DISTSTYLE ALL;
> ```
>
> The **entire table** is replicated to every compute node. A join between a large KEY-distributed fact table and a small ALL-distributed dimension table is always co-located — no redistribution.
>
> **When to use:** Small dimension tables (< 1 million rows). The classic star schema pattern: fact table with DISTKEY on the most-joined dimension, remaining small dimensions with DISTSTYLE ALL.
>
> **Cost:** N× storage (one copy per node), and every INSERT/UPDATE must write to all nodes. Only viable for small, slowly-changing dimension tables.
>
> ### 4. AUTO Distribution (Default)
> ```sql
> CREATE TABLE my_table (...)
> DISTSTYLE AUTO;  -- default
> ```
>
> Redshift automatically chooses between ALL and EVEN based on table size. Small tables start as ALL, then switch to EVEN as they grow. Does not auto-select KEY — that requires human judgment about join patterns.
>
> ### The Distribution Decision Tree
>
> ```
> Is this table frequently JOINed?
> ├── No → EVEN (or AUTO)
> └── Yes
>     ├── Is it small (< 1M rows)? → ALL (replicate everywhere)
>     └── Is it large?
>         └── What column is it most frequently joined on?
>             └── That column → DISTKEY
>                 └── Is that column high-cardinality with uniform distribution?
>                     ├── Yes → Good DISTKEY choice
>                     └── No → Risk of data skew, consider EVEN
> ```
>
> ### What Happens During a Join — Redistribution Mechanics
>
> Let me show the three scenarios:
>
> **Scenario 1: Co-located join (no redistribution)**
> ```
> orders DISTKEY(customer_id) JOIN customers DISTKEY(id)
>
> Slice 0: orders where hash(customer_id) = 0 + customers where hash(id) = 0
> Slice 1: orders where hash(customer_id) = 1 + customers where hash(id) = 1
> ...
> → Join happens locally on each slice. Zero network traffic.
> ```
>
> **Scenario 2: Broadcast (small table sent to all nodes)**
> ```
> orders DISTKEY(customer_id) JOIN regions DISTSTYLE EVEN
>
> regions is small (200 rows), so Redshift broadcasts it to all nodes.
> Cost: 200 rows × 100 bytes × num_nodes = trivial.
> ```
>
> **Scenario 3: Hash redistribution (both tables reshuffled)**
> ```
> orders DISTKEY(customer_id) JOIN products DISTKEY(product_id)
>
> Join is ON orders.product_id = products.product_id
> Neither table is distributed on the join key!
> Redshift must redistribute BOTH tables on product_id.
> Cost: All rows of both tables move across the network. Brutal.
> ```
>
> **This is why distribution key choice matters so much.** Scenario 3 can turn a 2-second query into a 2-minute query."

**Interviewer:**
Good. What about the star schema anti-pattern — what if a fact table is joined with 5 different dimension tables on 5 different keys?

**Candidate:**

> "You can only have one DISTKEY per table. So you pick the **most expensive join** — typically the largest dimension or the most frequent join — and distribute the fact table on that key. The remaining dimensions either:
>
> 1. Use DISTSTYLE ALL (if small enough — and most dimension tables are)
> 2. Accept the redistribution cost (if large)
>
> In practice, most star schemas have one large fact table joined with several small-to-medium dimension tables. The pattern is:
> - Fact table: `DISTKEY(most_joined_foreign_key)`
> - Small dimensions (< ~1M rows): `DISTSTYLE ALL`
> - Large dimensions: `DISTKEY(primary_key)` matching the fact table's foreign key where possible
>
> If there are genuinely two competing large-table joins, you might need to denormalize — pre-join the tables during ETL to avoid the runtime redistribution."

---

### L5 vs L6 vs L7 — Phase 5 Rubric

| Aspect | L5 (SDE-2) | L6 (SDE-3) | L7 (Principal) |
|---|---|---|---|
| **Distribution Styles** | Lists KEY, EVEN, ALL | Explains when to use each, provides decision tree, describes redistribution mechanics | Quantifies redistribution cost in bytes, discusses hash function properties (uniform distribution across slices) |
| **Co-location** | "Put join data together" | Shows three join scenarios with concrete examples, explains why mismatched DISTKEY causes full redistribution | Discusses multi-table join ordering optimization, explains how the optimizer estimates redistribution cost |
| **Skew Awareness** | Not mentioned | Identifies hot-slice problem with skewed DISTKEY | Proposes monitoring (SVV_DISKUSAGE per slice), describes automatic redistribution detection in EXPLAIN plan |
| **Star Schema** | Not discussed | Explains fact-DISTKEY + dimension-ALL pattern, acknowledges single-DISTKEY limit | Discusses denormalization tradeoffs, late-materializing joins, and when to break star schema for performance |

---

### Architecture Evolution Table — After Phase 5

| Version | Change | Benefit |
|---|---|---|
| Attempt 0 | Single PostgreSQL | Works for < 100 GB |
| Attempt 1 | MPP (leader + compute nodes) | Horizontal scale + parallel execution |
| Attempt 2 | + Columnar storage + zone maps | Only read needed columns, 3-10x compression, block-level skipping |
| **Phase 5** | **+ Distribution styles (KEY/EVEN/ALL/AUTO)** | **Co-located joins eliminate cross-node data movement** |

---

## PHASE 6: Deep Dive — Sort Keys & Zone Maps (~7 min)

**Interviewer:**
You mentioned zone maps and sort keys earlier. Elaborate.

**Candidate:**

> "Sort keys determine the physical order of rows on disk within each slice. Zone maps are the metadata that make sort keys powerful.
>
> ### Zone Maps
>
> Every 1 MB column block has a **zone map** — a small metadata record storing the min and max values in that block. When a query has `WHERE date BETWEEN '2024-01-01' AND '2024-01-31'`, the query engine checks each block's zone map:
>
> - Block zone map says min='2024-01-05', max='2024-01-20' → **read this block**
> - Block zone map says min='2023-06-01', max='2023-06-30' → **skip this block entirely**
>
> If the data is unsorted (random order), zone maps are useless — every block contains a wide range of dates, so no blocks can be skipped. But if the data is **sorted by date**, the blocks naturally have narrow min/max ranges, and zone maps can skip 95-98% of blocks for range-filtered queries.
>
> ```
> UNSORTED data (zone maps useless):
> Block 1: dates [2024-01-05, 2023-06-15, 2022-11-30, ...]  min=2022-11-30, max=2024-01-05
> Block 2: dates [2023-03-22, 2024-06-01, 2022-01-10, ...]  min=2022-01-10, max=2024-06-01
> → Every block spans the full date range → can't skip anything
>
> SORTED data (zone maps effective):
> Block 1: dates [2022-01-01 ... 2022-01-31]  min=2022-01-01, max=2022-01-31
> Block 2: dates [2022-02-01 ... 2022-02-28]  min=2022-02-01, max=2022-02-28
> ...
> Block 24: dates [2024-01-01 ... 2024-01-31]  min=2024-01-01, max=2024-01-31
> → Query for Jan 2024 reads ONLY block 24 → skips 23/24 blocks = 96%
> ```
>
> ### Compound Sort Keys
>
> ```sql
> CREATE TABLE orders (...)
> COMPOUND SORTKEY (date, region, product_id);
> ```
>
> Rows are sorted by date, then region within date, then product_id within region. Like a phone book sorted by last name, then first name.
>
> **Key property:** Compound sort keys are only effective when the query filters on a **prefix** of the sort key columns:
>
> | Filter Columns | Zone Map Effectiveness |
> |---|---|
> | `WHERE date = '2024-01-15'` | Excellent — first sort key column |
> | `WHERE date = '2024-01-15' AND region = 'US'` | Excellent — prefix match |
> | `WHERE region = 'US'` (no date filter) | **Poor** — skips the first column, so data isn't sorted by region within each block |
> | `WHERE product_id = 42` (no date/region) | **Useless** — third column without prefix |
>
> The first column of a compound sort key is disproportionately important — it determines the physical order of blocks.
>
> ### Interleaved Sort Keys
>
> ```sql
> CREATE TABLE orders (...)
> INTERLEAVED SORTKEY (date, region, product_id);
> ```
>
> Interleaved sort keys give **equal weight** to all specified columns (up to 8 max). Unlike compound, filtering on any single column is effective — not just the prefix. This is done using a multidimensional space-filling curve (like a Z-order curve) that interleaves bits from each column.
>
> **Tradeoffs: Compound vs Interleaved**
>
> | Aspect | Compound | Interleaved |
> |---|---|---|
> | Filtering on first column | Excellent | Good |
> | Filtering on non-first column | Poor | Good (equal weight to all columns) |
> | VACUUM performance | Fast (simple re-sort) | **Slow** (must recompute Z-order curve) |
> | Max columns | No limit | **8 columns** |
> | Recommended for | Known, predictable query patterns (always filter by date first) | Ad-hoc query patterns (analysts filter by any column) |
> | Maintenance cost | Low | **High** (requires more frequent VACUUM REINDEX) |
>
> **My recommendation for most workloads:** Use **compound sort keys** with the most commonly filtered column first. In data warehouses, this is almost always a timestamp/date column because analytics queries are almost always time-bounded. Interleaved sort keys are only worth the VACUUM cost when you genuinely have unpredictable filter patterns.
>
> ### AUTO Sort Key
>
> If no sort key is specified, Redshift uses AUTO and automatically selects a sort key based on observed query patterns. This is reasonable for tables with unpredictable access patterns, but explicit sort keys are better when you know your query patterns.
>
> ### VACUUM — Why It Matters
>
> When you COPY new data into a table, the new rows are appended to an **unsorted region** at the end of each slice. They're not merged into the sorted region immediately because that would require rewriting all existing blocks.
>
> Over time, the unsorted region grows. Zone maps for unsorted blocks have wide min/max ranges, making them less effective. VACUUM re-sorts the table, merging the unsorted region into the sorted region.
>
> ```
> Before VACUUM:
> [sorted blocks: tight zone maps] [unsorted blocks: wide zone maps]
>         95% of data                    5% of data
>
> After VACUUM:
> [all blocks sorted: tight zone maps throughout]
> ```
>
> Redshift also runs **automatic VACUUM** in the background when it detects tables with a significant unsorted region."

**Interviewer:**
How do zone maps interact with compression?

**Candidate:**

> "They work on compressed blocks. Each 1 MB block is stored compressed on disk. The zone map records the min/max of the **uncompressed** values. When the query engine evaluates a predicate, it checks the zone map first (metadata only, no decompression), and only decompresses blocks that might contain matching rows.
>
> This means compression and zone maps are **complementary**, not in conflict:
> - Compression reduces I/O (fewer bytes read from disk)
> - Zone maps reduce the number of blocks read
> - Together: you read fewer blocks, and each block is smaller
>
> The compression encoding is per-column and per-data-type. Redshift supports:
> - **AZ64**: Amazon's proprietary encoding, best for numeric and date/time columns
> - **LZO**: General-purpose, good compression ratio
> - **ZSTD**: Higher compression than LZO, higher CPU cost
> - **Delta**: For sequential or near-sequential values (timestamps, auto-increment IDs)
> - **RunLength**: For columns with many consecutive repeated values (low-cardinality sorted columns)
> - **Byte-Dictionary**: For columns with fewer than 256 distinct values
> - **Mostly8/Mostly16/Mostly32**: When most values fit in a smaller integer type (e.g., column is BIGINT but most values fit in INT)
>
> Redshift can automatically choose the best encoding with `COPY ... COMPUPDATE ON` or `ANALYZE COMPRESSION`."

---

### L5 vs L6 vs L7 — Phase 6 Rubric

| Aspect | L5 (SDE-2) | L6 (SDE-3) | L7 (Principal) |
|---|---|---|---|
| **Zone Maps** | "Min/max metadata per block" | Explains sorted vs unsorted effectiveness with concrete examples, quantifies block-skipping (95-98%) | Discusses zone map storage overhead, interaction with predicate pushdown in Spectrum, zone map vs Bloom filters |
| **Sort Keys** | "Sort the data for faster queries" | Compound vs interleaved tradeoffs, prefix-only effectiveness, decision criteria | Discusses Z-order curve internals for interleaved, explains why VACUUM REINDEX is expensive (recompute all interleaved blocks) |
| **Compression** | "Compression reduces storage" | Lists encoding types with appropriate use cases, explains compression + zone map complementarity | Discusses encoding selection algorithms, column-level compression analysis, ANALYZE COMPRESSION output interpretation |
| **VACUUM** | Not mentioned | Explains sorted vs unsorted regions, why VACUUM matters, automatic VACUUM | Discusses VACUUM DELETE ONLY vs SORT ONLY vs FULL, impact on concurrent queries, VACUUM threshold tuning |

---

## PHASE 7: Deep Dive — Query Processing & Execution (~7 min)

**Interviewer:**
Walk me through what happens when a query arrives at the leader node.

**Candidate:**

> "Let me trace a query end-to-end through the system.
>
> ### Step 1: Client Connection & Query Receipt
>
> The client connects to the **leader node** on port 5439 via PostgreSQL wire protocol. The leader is the single entry point — clients never communicate directly with compute nodes.
>
> ### Step 2: Parsing & Optimization
>
> The leader node **parses** the SQL into a query tree, then the **query optimizer** evaluates possible execution strategies:
> - Join order (which tables to join first)
> - Join type (hash join, merge join, nested loop)
> - Aggregation strategy (hash aggregate vs sort aggregate)
> - Data redistribution plan (which tables need redistribution for joins)
> - Whether to use a materialized view instead
>
> The optimizer uses **table statistics** (from ANALYZE) — row counts, distinct value counts, histogram distributions — to estimate the cost of each strategy. Bad statistics → bad plans → slow queries. This is why running ANALYZE after bulk loads matters.
>
> ### Step 3: Execution Plan → Compiled Code
>
> This is where Redshift diverges significantly from PostgreSQL. The optimizer produces an execution plan, and the execution engine translates this into three components:
>
> | Component | Definition |
> |---|---|
> | **Step** | A single operation (scan, join, aggregate, redistribute) |
> | **Segment** | A pipeline of steps that can execute without data exchange — the smallest unit of compiled code sent to a slice |
> | **Stream** | A collection of segments executed in sequence; one stream's output feeds the next |
>
> Redshift **compiles** each segment into optimized native code. This compiled code is cached — the first execution of a query includes compilation overhead, but subsequent executions with the same plan run from cache. The compiled code cache survives cluster reboots but is invalidated on version upgrades.
>
> ### Step 4: Distribution to Slices
>
> The compiled code is **broadcast to all compute node slices**. Each slice executes its segments in parallel on the data it owns.
>
> ### Step 5: Parallel Execution
>
> ```
>   Leader Node
>       │
>       │ Stream 1: Scan + Filter + Partial Aggregate
>       ▼
>   ┌───────┬───────┬───────┬───────┐
>   │Slice 0│Slice 1│Slice 2│Slice 3│  ← Each slice runs segments in parallel
>   │scan   │scan   │scan   │scan   │
>   │filter │filter │filter │filter │
>   │partial│partial│partial│partial│
>   │agg    │agg    │agg    │agg    │
>   └───┬───┴───┬───┴───┬───┴───┬───┘
>       │       │       │       │
>       │ Stream 2: Redistribute + Final Aggregate
>       ▼
>   ┌───────┬───────┬───────┬───────┐
>   │Slice 0│Slice 1│Slice 2│Slice 3│  ← Data may be redistributed between streams
>   │merge  │merge  │merge  │merge  │
>   │final  │final  │final  │final  │
>   │agg    │agg    │agg    │agg    │
>   └───┬───┴───┬───┴───┬───┴───┬───┘
>       │       │       │       │
>       ▼       ▼       ▼       ▼
>   ┌───────────────────────────────┐
>   │ Leader Node: merge results    │
>   │ Apply LIMIT, ORDER BY, final  │
>   │ Return to client              │
>   └───────────────────────────────┘
> ```
>
> Key points:
> - **Streams execute sequentially** — the leader waits for Stream 1 to complete before starting Stream 2
> - **Segments within a stream execute in parallel** across slices
> - Data redistribution (hash redistribution or broadcast) happens **between streams**
> - The leader processes results from the final stream (sorting, LIMIT, formatting)
>
> ### Step 6: Result Caching
>
> If the query has been run before and the underlying data hasn't changed, Redshift returns the **cached result** directly from the leader node — no compute node work at all. This is transparent to the client.
>
> ### Short Query Acceleration (SQA)
>
> Redshift identifies short-running queries (predicted by machine learning to finish in under a few seconds) and routes them to a **dedicated fast lane**, bypassing the regular WLM queue. This prevents small dashboard queries from waiting behind long-running ETL jobs. SQA uses a prediction model based on query compile time, number of segments, and table statistics."

**Interviewer:**
What happens when the leader node becomes a bottleneck?

**Candidate:**

> "The leader is a **single point** for several operations:
>
> 1. **Query planning**: Single-threaded — one plan at a time per query. Complex queries with many joins can take seconds just to plan.
> 2. **Result aggregation**: All compute nodes send partial results to the leader, which merges them. If the result set is large (millions of rows before LIMIT), the leader's memory can be exhausted.
> 3. **Connection handling**: Each client connection is a process on the leader. With 2,000 max connections (RA3), the leader manages all of them.
>
> Mitigations:
> - **Result caching**: Cached results bypass compute nodes entirely, reducing leader load
> - **Materialized views**: Precompute common aggregations so queries scan less data
> - **Concurrency scaling**: Offloads burst read traffic to transient clusters (each with its own leader)
> - **UNLOAD for large exports**: Instead of pulling millions of rows through the leader, UNLOAD writes directly from compute nodes to S3 in parallel
> - **Data API**: Async HTTP-based query execution that doesn't hold a persistent connection"

---

### L5 vs L6 vs L7 — Phase 7 Rubric

| Aspect | L5 (SDE-2) | L6 (SDE-3) | L7 (Principal) |
|---|---|---|---|
| **Query Pipeline** | "Parse, plan, execute" | Explains steps/segments/streams, code compilation, caching, stream-by-stream execution | Discusses code generation techniques (vectorized vs compiled), runtime optimization, adaptive execution |
| **Parallelism** | "Runs on all nodes" | Explains slice-level parallelism, redistribution between streams, leader aggregation | Discusses pipeline parallelism within segments, SIMD optimization, CPU cache-aware execution |
| **Leader Bottleneck** | Not identified | Identifies three bottleneck scenarios, proposes mitigations | Quantifies: "leader with 384 GB RAM can aggregate 200 GB result set but not 500 GB", discusses spill-to-disk |
| **SQA** | Not mentioned | Explains ML-based routing of short queries to fast lane | Discusses SQA prediction model accuracy, false positive impact (short queries waiting), threshold tuning |

---

## PHASE 8: Deep Dive — Concurrency, WLM & Scaling (~7 min)

**Interviewer:**
A cluster has fixed resources. How do you handle many concurrent queries competing for CPU, memory, and I/O?

**Candidate:**

> "This is the Workload Management (WLM) problem. Redshift's approach:
>
> ### Workload Management (WLM)
>
> WLM controls which queries run, when, and with how many resources. It uses **queues**:
>
> ```
> Incoming queries
>       │
>       ▼
> ┌─────────────────────────────────────┐
> │         WLM Queue Assignment         │
> │                                       │
> │  Rule 1: User = 'admin'  → Queue 1   │
> │  Rule 2: Group = 'etl'   → Queue 2   │
> │  Rule 3: Query < 10 sec  → SQA lane  │
> │  Default:                → Queue 3   │
> └─────┬──────────┬──────────┬──────────┘
>       │          │          │
>       ▼          ▼          ▼
> ┌──────────┐ ┌──────────┐ ┌──────────┐
> │ Queue 1  │ │ Queue 2  │ │ Queue 3  │
> │ Priority │ │ ETL jobs │ │ Default  │
> │ slots: 5 │ │ slots: 3 │ │ slots:10 │
> │ mem: 40% │ │ mem: 30% │ │ mem: 30% │
> └──────────┘ └──────────┘ └──────────┘
>       │          │          │
>       ▼          ▼          ▼
>      [Compute Node Resources]
> ```
>
> **Key WLM concepts:**
>
> - **Queues**: Each queue has a concurrency level (number of slots) and memory allocation (% of total)
> - **Slots**: A query gets one slot. If all slots in its queue are full, the query waits
> - **Memory**: Each slot gets queue_memory / concurrency_level. More slots = less memory per query = more disk spilling
> - **Superuser queue**: Reserved queue (1 slot) for admin operations — always available even when user queues are full
> - **Maximum 50 concurrency slots** across all user-defined queues (manual WLM)
>
> **Automatic WLM** (recommended): Redshift dynamically allocates memory and concurrency. Instead of fixed slots, the system adjusts based on query complexity. Simple queries get less memory; complex queries get more. This avoids the manual WLM trap of "set 20 slots → each gets 5% memory → complex queries spill to disk → everything slows down."
>
> ### Concurrency Scaling
>
> When all slots are full and queries are queuing, concurrency scaling kicks in:
>
> ```
> ┌───────────────────────────┐
> │    Main Cluster           │
> │    (always running)       │
> │                           │
> │    WLM queues full...     │
> │    queries waiting...     │
> └─────────────┬─────────────┘
>               │ Eligible queries overflow
>               ▼
> ┌───────────────────────────┐
> │  Concurrency Scaling      │
> │  Cluster 1 (transient)    │
> │  - Same data (via cache   │
> │    from Redshift Managed  │
> │    Storage on S3)         │
> │  - Handles burst queries  │
> └───────────────────────────┘
>               │
>               ▼ (more clusters if needed)
> ┌───────────────────────────┐
> │  Concurrency Scaling      │
> │  Cluster 2 (transient)    │
> └───────────────────────────┘
>     ... up to 10 clusters
> ```
>
> **How it works:**
> - Redshift automatically provisions **transient clusters** (identical hardware to your main cluster)
> - These clusters read data from **Redshift Managed Storage** (S3-backed) — the same data the main cluster uses
> - **Eligible queries**: Read queries (SELECT) and write queries on RA3 nodes
> - **Not eligible**: Queries using interleaved sort keys, temporary tables, Python UDFs
> - **Pricing**: Credit-based — you get 1 hour of free concurrency scaling credits per day per active cluster. Beyond that, per-second billing at on-demand rates
> - **Max concurrency scaling clusters**: 10 (configurable, can request increase)
>
> ### The RA3 Node Type & Managed Storage
>
> This is what makes concurrency scaling possible. RA3 nodes separate compute from storage:
>
> ```
> ┌───────────────────┐
> │    RA3 Node        │
> │                    │
> │  ┌──────────────┐  │
> │  │ Local SSD    │  │  ← Hot data cache
> │  │ Cache        │  │
> │  └──────┬───────┘  │
> │         │          │
> └─────────┼──────────┘
>           │ Cache miss
>           ▼
> ┌───────────────────┐
> │ Redshift Managed  │
> │ Storage (RMS)     │
> │                   │
> │ All data stored   │
> │ on S3 (durable)   │
> │ Automatically     │
> │ managed by        │
> │ Redshift          │
> └───────────────────┘
> ```
>
> **RA3 node types:**
>
> | Node Type | vCPU | Memory | Slices | Max Nodes | Managed Storage |
> |---|---|---|---|---|---|
> | ra3.xlplus | 4 | 32 GB | 2 | 32 | 32 TB per node |
> | ra3.4xlarge | 12 | 96 GB | 4 | 128 | 128 TB per node |
> | ra3.16xlarge | 48 | 384 GB | 16 | 128 | 128 TB per node |
>
> **DC2 nodes** (for comparison — compute + local storage, no separation):
>
> | Node Type | vCPU | Memory | Slices | Local Storage |
> |---|---|---|---|---|
> | dc2.large | 2 | 15 GB | 2 | 160 GB NVMe SSD |
> | dc2.8xlarge | 32 | 244 GB | 16 | 2.56 TB NVMe SSD |
>
> **When RA3 vs DC2:**
> - **RA3**: Data > 1 TB, or you want to scale compute independently of storage. Hot data cached locally, cold data on S3. Enables concurrency scaling and data sharing.
> - **DC2**: Data < 1 TB with consistently hot access pattern. All data on local NVMe SSDs — lowest latency. But no managed storage, no concurrency scaling."

**Interviewer:**
What about Redshift Serverless?

**Candidate:**

> "Redshift Serverless eliminates cluster management entirely. Instead of choosing node types and cluster sizes, you configure:
>
> - **Workgroup**: Compute resources measured in **RPU (Redshift Processing Units)** — base capacity in RPUs (minimum 8, increments of 8). Serverless auto-scales RPUs based on workload.
> - **Namespace**: Storage — databases, schemas, tables, encryption keys, users
>
> **When to use Serverless vs Provisioned:**
>
> | Aspect | Provisioned | Serverless |
> |---|---|---|
> | Workload pattern | Predictable, steady | Variable, bursty, infrequent |
> | Cost model | Per-node-hour (reserved/on-demand) | Per-RPU-hour (only when queries run) |
> | Management overhead | You choose nodes, resize, manage WLM | Zero — AWS manages everything |
> | Best for | Always-on production warehouses with predictable workloads | Dev/test, ad-hoc analytics, variable demand |
> | Max connections | 2,000 (RA3) | 2,000 |
>
> Serverless is not always cheaper — for steady 24/7 workloads, provisioned with reserved instances can be significantly less expensive. Serverless wins for workloads that are idle 80% of the time."

---

### L5 vs L6 vs L7 — Phase 8 Rubric

| Aspect | L5 (SDE-2) | L6 (SDE-3) | L7 (Principal) |
|---|---|---|---|
| **WLM** | "Queues for queries" | Explains slots, memory allocation, automatic vs manual WLM, SQA routing, superuser queue | Discusses memory spilling mechanics, WLM queue hop timeout, query priority within queues |
| **Concurrency Scaling** | "Add more clusters" | Explains transient clusters, RA3 managed storage enabling it, eligibility criteria, credit-based pricing | Discusses cold-start latency for transient clusters, cache warming strategies, cost modeling for burst vs steady workloads |
| **RA3 vs DC2** | Lists node types | Explains compute-storage separation, local SSD cache + S3 backing, when to use which | Discusses cache eviction policies, managed storage internals, tiered caching (block-level LRU), network bandwidth between node and S3 |
| **Serverless** | "No management" | Explains RPU model, workgroup/namespace split, when cheaper vs provisioned | Models total cost: "100 RPU-hours/day × $0.375/RPU-hour = $37.50/day vs 4x ra3.xlplus reserved = $25/day" |

---

## PHASE 9: Deep Dive — Redshift Spectrum (~6 min)

**Interviewer:**
What about data that's too cold or too large to load into the warehouse?

**Candidate:**

> "This is the Spectrum use case. Redshift Spectrum lets you query data **directly in S3** using the same SQL, without loading it into Redshift tables.
>
> ### How Spectrum Works
>
> ```
>     BI Tool
>       │
>       ▼
> ┌─────────────────┐
> │   Leader Node    │
> │   (same cluster) │
> └────────┬────────┘
>          │ Query plan includes
>          │ both local + external tables
>     ┌────┴────────────────┐
>     │                     │
>     ▼                     ▼
> ┌──────────┐     ┌────────────────────┐
> │ Compute  │     │  Spectrum Layer     │
> │ Nodes    │     │  (separate fleet    │
> │          │     │   of compute nodes  │
> │ Local    │     │   managed by AWS)   │
> │ tables   │     │                     │
> └──────────┘     │  Reads from S3      │
>                  │  Applies predicates │
>                  │  Runs aggregations  │
>                  │  Returns results    │
>                  │  to compute nodes   │
>                  └────────────────────┘
>                          │
>                          ▼
>                  ┌──────────────┐
>                  │  S3 Bucket   │
>                  │              │
>                  │  Parquet/ORC │
>                  │  CSV/JSON    │
>                  │  Hudi/Delta  │
>                  │  (partitioned│
>                  │   by date)   │
>                  └──────────────┘
> ```
>
> ### Key Architecture Points
>
> 1. **Separate compute fleet**: Spectrum nodes are NOT your Redshift compute nodes. They're a shared, AWS-managed pool. This means Spectrum queries don't consume your cluster's CPU/memory.
>
> 2. **Predicate pushdown**: Spectrum pushes WHERE clauses down to the S3 scan layer. If your query says `WHERE year = 2024`, and the data is partitioned by year, Spectrum only reads the `year=2024` partition from S3. If the data is in Parquet/ORC (columnar formats), Spectrum also does column pruning — only reading the needed columns.
>
> 3. **External tables via external schemas**: You define external tables pointing to S3 locations, registered in the AWS Glue Data Catalog (or an Apache Hive metastore):
>
>    ```sql
>    CREATE EXTERNAL SCHEMA spectrum_schema
>    FROM DATA CATALOG
>    DATABASE 'my_catalog_db'
>    IAM_ROLE 'arn:aws:iam::123456789012:role/RedshiftSpectrumRole';
>
>    -- Then query:
>    SELECT region, sum(amount)
>    FROM spectrum_schema.orders_external  -- S3 data
>    JOIN public.regions                   -- local Redshift table
>    ON orders_external.region_id = regions.id
>    GROUP BY region;
>    ```
>
> 4. **Join between local and external**: You can JOIN a local Redshift table with an external S3 table in the same query. The Spectrum layer scans S3, applies filters and aggregations, then sends the (much smaller) result to compute nodes for the join with local data.
>
> 5. **Supported formats**: Parquet, ORC, JSON, CSV, Avro, Ion, Hudi, Delta Lake
>
> 6. **Partitioning is critical**: Without partitioning, Spectrum scans ALL files in the S3 path. With partitioning (e.g., `s3://bucket/orders/year=2024/month=01/`), it only scans matching partitions. The difference can be 100x in cost and performance.
>
> ### When to Use Spectrum vs Loading Into Redshift
>
> | Scenario | Recommendation |
> |---|---|
> | Query runs frequently (daily dashboard) | **Load into Redshift** — local data is faster |
> | Query runs infrequently (ad-hoc exploration) | **Spectrum** — avoid the ETL cost of loading |
> | Data is too large to fit in cluster | **Spectrum** — S3 scales to exabytes |
> | Data needs to be shared with other engines (Athena, EMR, Glue) | **Spectrum** — data stays in S3, accessible by all |
> | Need sub-second latency | **Load into Redshift** — Spectrum adds S3 read latency |
>
> ### The Data Lakehouse Pattern
>
> Spectrum enables a **data lakehouse** architecture:
> - **Hot data** in Redshift local tables (fast, compressed, sorted, indexed with zone maps)
> - **Warm/cold data** in S3 (Parquet/ORC, partitioned, queryable via Spectrum)
> - **Single SQL interface** for both: analysts don't need to know where data lives"

---

### L5 vs L6 vs L7 — Phase 9 Rubric

| Aspect | L5 (SDE-2) | L6 (SDE-3) | L7 (Principal) |
|---|---|---|---|
| **Spectrum Architecture** | "Query S3 from Redshift" | Explains separate compute fleet, predicate pushdown, Glue Catalog integration, join between local and external | Discusses Spectrum fleet sizing, how AWS provisions Spectrum nodes per query, S3 request rate limits as bottleneck |
| **Partitioning** | Not mentioned | Explains partitioning is critical for cost/perf, provides 100x difference example | Quantifies: "1 PB unpartitioned = scan 1 PB at $5/TB = $5,000 per query. Partitioned by day = scan 2.7 TB = $13.50 per query" |
| **Format Selection** | Lists formats | Recommends columnar (Parquet/ORC) over row (CSV/JSON), explains why | Discusses Parquet page-level statistics, ORC bloom filters, stripe-level column encryption, row group sizing tradeoffs |
| **Load vs Spectrum** | "Load for fast, Spectrum for cold" | Provides decision matrix with 5 scenarios | Models break-even: "if query runs > 3x/day, loading is cheaper than Spectrum scanning" |

---

## PHASE 10: Deep Dive — Durability, Backups & Data Sharing (~5 min)

**Interviewer:**
How does Redshift ensure data durability, and how does data sharing work across clusters?

**Candidate:**

> "### Durability & Backups
>
> Redshift's durability strategy depends on the node type:
>
> **RA3 Nodes (Managed Storage):**
> Data is stored in **Redshift Managed Storage (RMS)**, backed by S3. This means:
> - All data is automatically replicated across 3 AZs (S3's durability model — 11 9's)
> - Local SSDs on RA3 nodes are a **cache**, not the source of truth
> - If a node fails, a new node is provisioned and warms its cache from S3 — no data loss
> - This is fundamentally different from DC2, where local SSDs ARE the primary storage
>
> **DC2 Nodes (Local Storage):**
> Data is stored on local NVMe SSDs. Redshift automatically replicates data within the cluster:
> - Each data block is replicated to another node in the cluster
> - If a node fails, the replica serves reads while a replacement node is provisioned
> - But if the entire cluster is lost (catastrophic failure), you rely on **snapshots**
>
> **Snapshots:**
> - **Automated snapshots**: Every 8 hours or after 5 GB per node of data changes (whichever comes first)
>   - Retention: 1-35 days for RA3 (default 1 day, cannot be disabled for RA3)
>   - Incremental: only changed blocks are backed up
> - **Manual snapshots**: Retained indefinitely, even after cluster deletion
> - **Cross-region snapshot copy**: For DR — automatically copy snapshots to another region
> - Snapshots are stored in S3 (internally managed by Redshift, encrypted)
>
> **Restore**: Creates a new cluster. Data streams on-demand during active queries — you can start querying before the full restore completes.
>
> ### Data Sharing
>
> Data sharing enables live, real-time access to data across clusters and accounts **without copying**:
>
> ```
> ┌──────────────────┐         ┌──────────────────┐
> │ Producer Cluster  │         │ Consumer Cluster  │
> │                   │         │                   │
> │ CREATE DATASHARE  │ ──────> │ CREATE DATABASE   │
> │ ds_orders         │  live   │ FROM DATASHARE    │
> │   ADD TABLE       │  read   │ ds_orders         │
> │   orders          │  access │                   │
> │   ADD SCHEMA      │         │ SELECT * FROM     │
> │   public          │         │ ds_orders_db      │
> │                   │         │ .public.orders    │
> └──────────────────┘         └──────────────────┘
> ```
>
> **Key properties:**
> - **Live access**: Consumer sees data as of the producer's latest committed transaction — strong transactional consistency
> - **No data movement**: Consumer reads directly from producer's managed storage (RMS/S3). No ETL, no copies.
> - **Cross-account**: Producer grants access to specific AWS accounts
> - **Cross-region**: Supported for data sharing across regions
> - **Granularity**: Share databases, schemas, tables, views (including materialized views), and SQL UDFs
> - **Write access**: Consumers can INSERT/UPDATE on shared objects (producer changes are immediately visible)
>
> **Use cases:**
> 1. **Workload isolation**: One ETL cluster loads data, multiple BI clusters consume via data sharing
> 2. **Multi-team access**: Data engineering shares curated datasets with data science teams
> 3. **AWS Data Exchange**: Monetize data by publishing datashares as commercial listings"

---

### L5 vs L6 vs L7 — Phase 10 Rubric

| Aspect | L5 (SDE-2) | L6 (SDE-3) | L7 (Principal) |
|---|---|---|---|
| **Durability** | "Data is backed up" | Distinguishes RA3 (S3-backed) vs DC2 (local replication), explains snapshot mechanics with concrete intervals | Discusses RPO analysis: RA3 RPO ≈ 0 (S3 is authoritative), DC2 RPO = time since last snapshot, quantifies data loss scenarios |
| **Snapshots** | "Regular backups" | Explains automated (8 hr / 5 GB threshold), manual (indefinite), cross-region copy, incremental, restore-during-query | Discusses snapshot storage costs, restore time as function of cluster size, snapshot scheduling strategy for compliance |
| **Data Sharing** | Not mentioned | Explains producer-consumer model, live access, no data movement, cross-account sharing | Discusses consistency guarantees across clusters, performance impact on producer, access control via IAM + Redshift grants |

---

## PHASE 11: Deep Dive — Materialized Views & Optimization (~4 min)

**Interviewer:**
How do materialized views work, and what about auto-optimization?

**Candidate:**

> "### Materialized Views
>
> A materialized view stores the **precomputed result** of a query. For a dashboard that runs `SELECT region, SUM(amount), COUNT(*) FROM orders GROUP BY region` every 5 minutes, computing it from scratch each time scans billions of rows. A materialized view computes it once and serves the cached result.
>
> ```sql
> CREATE MATERIALIZED VIEW mv_orders_by_region AS
> SELECT region, SUM(amount) as total_amount, COUNT(*) as order_count
> FROM orders
> GROUP BY region;
> ```
>
> **Refresh strategies:**
> - **Manual refresh**: `REFRESH MATERIALIZED VIEW mv_orders_by_region;` — you control when
> - **Automatic refresh (autorefresh)**: Redshift refreshes when base tables change and cluster resources are available
> - **Scheduled refresh**: Via Redshift Scheduler API — cron-based
> - **Incremental refresh**: Redshift identifies what changed in base tables and applies only the delta — much faster than full recomputation
>
> **Automatic query rewriting:**
> This is the powerful part. If a user runs a query that the optimizer recognizes can be satisfied by a materialized view, Redshift **automatically rewrites the query** to use the MV — even if the user didn't reference the MV. This is transparent and requires the MV to be up-to-date.
>
> **Automated Materialized Views (Auto MV):**
> Redshift analyzes query patterns and **automatically creates materialized views** for frequently-run queries. It handles the full lifecycle: creation, refresh, and deletion when no longer useful. No manual intervention required.
>
> **Nested materialized views:**
> You can build MVs on top of other MVs. For example, a base MV precomputes an expensive join, and multiple upper MVs apply different GROUP BY clauses to the base MV result. This avoids recomputing the expensive join multiple times.
>
> ### Other Optimization Features
>
> - **Result caching**: Identical queries (same SQL text, same data) return cached results from the leader node — no compute node work
> - **ANALYZE**: Updates table statistics (row count, distinct values, histograms). Critical for the optimizer to make good decisions.
> - **ANALYZE COMPRESSION**: Recommends optimal column encodings based on data analysis
> - **Automatic table optimization**: Redshift can automatically apply recommended sort keys and distribution styles based on query patterns"

---

## PHASE 12: Wrap-Up — "What Keeps You Up at Night?" (~3 min)

**Interviewer:**
We're running short on time. If you were running Redshift as a service, what keeps you up at night?

**Candidate:**

> "Three things:
>
> **1. Data skew and hot slices**
> A customer picks a bad distribution key and 80% of their data ends up on one slice. Their queries are 10x slower than expected. They file a support ticket saying 'Redshift is slow' when the real problem is their schema design. We need better monitoring (SVV_DISKUSAGE per slice, skew alerts) and better automatic redistribution — the AUTO distribution style is a step in the right direction, but it doesn't auto-select KEY distribution.
>
> **2. Concurrency scaling cold start**
> When a concurrency scaling cluster spins up, it starts with a cold cache. The first queries on the transient cluster hit S3 instead of local SSD — adding latency at exactly the moment when the customer is experiencing high load. We need predictive scaling (spin up concurrency scaling clusters before queues fill) and cache pre-warming strategies.
>
> **3. The boundary between Redshift and the data lake**
> Customers have data in S3 (Parquet/ORC), data in Redshift local tables, and data in Redshift Spectrum. The boundary is increasingly blurry. Spectrum performance is 5-10x slower than local tables, but local tables require ETL to load. The dream is a system where data placement is automatic — hot data migrates to local storage, cold data stays in S3, and the optimizer transparently chooses the best access path. RA3 Managed Storage is moving in this direction, but the Spectrum gap remains.
>
> **Bonus: Competition**
> Snowflake's separation of compute and storage was there from day one. BigQuery's serverless model means zero management. Databricks' Photon engine on Delta Lake is closing the performance gap. Redshift needs to keep innovating on the Serverless front and on the data lakehouse integration to remain competitive."

**Interviewer:**
That's great depth. Solid awareness of the competitive landscape and operational realities. Let's stop here.

---

### L5 vs L6 vs L7 — Wrap-Up Rubric

| Aspect | L5 (SDE-2) | L6 (SDE-3) | L7 (Principal) |
|---|---|---|---|
| **Operational Awareness** | "Monitoring is important" | Identifies specific pain points (skew, cold start, lake boundary) with concrete examples | Proposes system-level solutions: auto-tiering, predictive scaling, adaptive distribution |
| **Competitive Landscape** | Not mentioned | Names Snowflake, BigQuery, Databricks with specific architectural differences | Frames competition as product strategy: "Redshift's moat is AWS ecosystem integration — Glue, S3, SageMaker, QuickSight" |
| **What-if Thinking** | Doesn't engage | Proposes improvements to existing system | Proposes new capabilities: "Imagine Redshift auto-detecting that 90% of Spectrum queries hit the last 7 days and auto-promoting that data to local storage" |

---

## Final Architecture Diagram

```
                     ┌──────────────────────────┐
                     │     Clients (BI tools,    │
                     │     ETL, Analysts)         │
                     │     JDBC/ODBC port 5439   │
                     └────────────┬──────────────┘
                                  │
                     ┌────────────▼──────────────┐
                     │       Leader Node          │
                     │                            │
                     │  • SQL parsing             │
                     │  • Query optimization      │
                     │  • Code compilation        │
                     │  • Result aggregation      │
                     │  • Result caching          │
                     │  • WLM queue management    │
                     │  • SQA routing             │
                     └──────┬──────────┬──────────┘
                            │          │
              ┌─────────────┘          └──────────────┐
              ▼                                       ▼
    ┌──────────────────┐                    ┌──────────────────┐
    │  Compute Node 1  │       ...          │  Compute Node N  │
    │  (up to 128)     │                    │                  │
    │                  │                    │                  │
    │  Slice 0 ────────┼─ Columnar blocks  │  Slice M         │
    │  Slice 1         │  (1 MB, encoded)  │  Slice M+1       │
    │  ...             │  + zone maps      │  ...             │
    │                  │                    │                  │
    │  Local SSD cache │                    │  Local SSD cache │
    │  (RA3: cache)    │                    │  (DC2: primary)  │
    └────────┬─────────┘                    └────────┬─────────┘
             │                                       │
             └──────────────┬────────────────────────┘
                            │  RA3 only
                            ▼
              ┌──────────────────────────┐
              │  Redshift Managed Storage │
              │  (S3-backed, 11 9s)      │
              │                          │
              │  All data durably stored │
              │  Accessible by:          │
              │  • Main cluster nodes    │
              │  • Concurrency scaling   │
              │    clusters              │
              │  • Data sharing          │
              │    consumers             │
              └──────────────────────────┘

    ┌──────────────────────────────────────────────────┐
    │              Spectrum Layer                        │
    │  (Separate AWS-managed compute fleet)             │
    │                                                    │
    │  Reads from S3 → applies predicates/aggs          │
    │  → returns results to compute nodes               │
    │                                                    │
    │  S3 Data Lake: Parquet/ORC/CSV/JSON/Hudi/Delta   │
    │  Glue Data Catalog: external table metadata       │
    └──────────────────────────────────────────────────┘
```

---

## Key Numbers Reference (Verified Against AWS Docs)

| Metric | Value | Source |
|---|---|---|
| Max nodes per cluster | 128 (RA3 and DC2) | [Quotas page](https://docs.aws.amazon.com/redshift/latest/mgmt/amazon-redshift-limits.html) |
| Max total nodes per account/region | 200 | Quotas page |
| Max databases per cluster | 60 | Quotas page |
| Max databases per Serverless namespace | 100 | Quotas page |
| Max schemas per database | 9,900 | Quotas page |
| Max tables (ra3.4xlarge, ra3.16xlarge) | 200,000 | Quotas page |
| Max tables (large, xlarge, xlplus single-node) | 9,900 | Quotas page |
| Max connections (RA3) | 2,000 | Quotas page |
| Max connections (dc2.large) | 500 | Quotas page |
| Max concurrency scaling clusters | 10 | Quotas page |
| Max WLM concurrency slots (manual) | 50 | Quotas page |
| Columnar block size | 1 MB | Developer Guide |
| ra3.xlplus | 4 vCPU, 32 GB RAM, 2 slices, max 32 nodes | Management Guide |
| ra3.4xlarge | 12 vCPU, 96 GB RAM, 4 slices, max 128 nodes | Management Guide |
| ra3.16xlarge | 48 vCPU, 384 GB RAM, 16 slices, max 128 nodes | Management Guide |
| dc2.large | 2 vCPU, 15 GB RAM, 2 slices, 160 GB NVMe | Management Guide |
| dc2.8xlarge | 32 vCPU, 244 GB RAM, 16 slices, 2.56 TB NVMe | Management Guide |
| Max columns for external tables | 1,597 (with pseudocolumns) | Quotas page |
| Snapshot automated frequency | Every 8 hours or 5 GB/node (whichever first) | Management Guide |
| Snapshot retention (RA3) | 1-35 days (cannot be disabled) | Management Guide |
| Max snapshots | 700 | Quotas page |
| Serverless max workgroups per account | 25 | Quotas page |
| Idle session timeout (cluster) | 4 hours | Quotas page |
| Idle session timeout (Serverless) | 1 hour | Quotas page |
| PostgreSQL wire protocol port | 5439 | Developer Guide |

---

## Companion Deep-Dive Documents

For detailed technical deep dives, see:

1. [Columnar Storage & Compression](./columnar-storage.md) — 1 MB blocks, zone maps, encoding types, compression ratios, storage layout
2. [Data Distribution & Sort Keys](./distribution-and-sort-keys.md) — KEY/EVEN/ALL/AUTO, compound vs interleaved, VACUUM, zone map interaction
3. [Query Processing & Execution](./query-processing.md) — Steps/segments/streams, code compilation, SQA, result caching, EXPLAIN plans
4. [Concurrency & Workload Management](./concurrency-and-wlm.md) — WLM queues, concurrency scaling, RA3 managed storage, Serverless RPUs
5. [Spectrum & Data Lake Integration](./spectrum-and-data-lake.md) — External tables, Glue Catalog, partitioning, predicate pushdown, format selection

---

## Key Tradeoffs Summary

| Decision | Option A | Option B | When A | When B |
|---|---|---|---|---|
| **Distribution** | KEY | EVEN | Table frequently joined on a high-cardinality column | Table not joined, or no good single join column |
| **Distribution** | ALL | KEY | Small dimension table (< 1M rows) | Large table |
| **Sort key** | Compound | Interleaved | Known query patterns (always filter by date) | Ad-hoc unpredictable filter patterns |
| **Storage** | Load into Redshift | Query via Spectrum | Frequently queried, needs sub-second latency | Infrequently queried, too large to load, shared with other engines |
| **Node type** | RA3 | DC2 | > 1 TB, need compute-storage separation, concurrency scaling | < 1 TB, all-hot data, want lowest latency |
| **Cluster type** | Provisioned | Serverless | Steady 24/7 workload, want cost control | Variable/bursty, want zero management |
| **Redshift vs Athena** | Redshift | Athena | Complex queries, high concurrency, sub-second dashboards | Ad-hoc queries, pay-per-query, no infra management |
| **Redshift vs Snowflake** | Redshift | Snowflake | Deep AWS integration (S3, Glue, SageMaker, QuickSight) | Multi-cloud, stronger compute-storage separation from day one |
| **Compression** | AZ64 | ZSTD | Numeric/date columns (best for zone maps) | String/general data (highest compression) |
