# Deep Dive: Data Distribution & Sort Keys in Amazon Redshift

> Companion document to [interview-simulation.md](./interview-simulation.md)

---

## Table of Contents

1. [Why Distribution Matters in MPP](#1-why-distribution-matters-in-mpp)
2. [Distribution Styles: KEY, EVEN, ALL, AUTO](#2-distribution-styles-key-even-all-auto)
3. [Choosing a Distribution Key](#3-choosing-a-distribution-key)
4. [Data Redistribution During Joins](#4-data-redistribution-during-joins)
5. [Data Skew: Detection and Mitigation](#5-data-skew-detection-and-mitigation)
6. [Sort Keys: Compound vs Interleaved vs AUTO](#6-sort-keys-compound-vs-interleaved-vs-auto)
7. [Sort Keys and Zone Map Interaction](#7-sort-keys-and-zone-map-interaction)
8. [VACUUM and Sort Maintenance](#8-vacuum-and-sort-maintenance)
9. [Star Schema Design Patterns](#9-star-schema-design-patterns)
10. [Design Decisions & Tradeoffs](#10-design-decisions--tradeoffs)

---

## 1. Why Distribution Matters in MPP

In a Massively Parallel Processing (MPP) system, data is distributed across multiple compute nodes. Each node processes its local data in parallel. The performance of the system depends critically on:

1. **Parallelism balance**: If data is evenly distributed, all nodes finish at the same time. If data is skewed (one node has 80%), the query is only as fast as the overloaded node.

2. **Data locality for joins**: If two tables being joined have matching rows on the same node, the join happens locally with zero network I/O. If matching rows are on different nodes, data must move across the network — **redistribution** — which is the single most expensive operation in MPP query execution.

```
GOOD: Co-located join (no redistribution)
┌──────────┐ ┌──────────┐
│ Slice 0  │ │ Slice 0  │
│ orders   │ │ customers│
│ cust=1-5 │ │ id=1-5   │  ← Same slice has matching keys
│          │ │          │     Join is LOCAL
└──────────┘ └──────────┘

BAD: Non-co-located join (redistribution required)
┌──────────┐ ┌──────────┐
│ Slice 0  │ │ Slice 3  │
│ orders   │ │ customers│
│ cust=1-5 │ │ id=1-5   │  ← Different slices have matching keys
│          │ │          │     Data must MOVE across network
└──────────┘ └──────────┘
```

**Quantified cost of redistribution:**

For a 100M-row table with 200 bytes/row:
- Table size: 100M × 200B = 20 GB
- Full hash redistribution: all 20 GB moves across the network
- At 10 Gbps inter-node bandwidth: 20 GB / 1.25 GB/s ≈ **16 seconds** just for the data movement
- Compare: co-located join adds **0 seconds** of redistribution

This is why distribution key selection is the #1 performance tuning decision in Redshift.

---

## 2. Distribution Styles: KEY, EVEN, ALL, AUTO

### KEY Distribution

```sql
CREATE TABLE orders (
  order_id      BIGINT,
  customer_id   BIGINT,
  product_id    BIGINT,
  amount         DECIMAL(10,2),
  order_date    DATE
)
DISTSTYLE KEY
DISTKEY (customer_id);
```

**How it works**: Each row is hashed on the DISTKEY column. The hash value determines which slice stores the row. All rows with the same DISTKEY value are **guaranteed to be on the same slice**.

```
hash(customer_id) mod num_slices → slice assignment

customer_id = 42  → hash(42) mod 16 = 7  → Slice 7
customer_id = 42  → hash(42) mod 16 = 7  → Slice 7  (same slice!)
customer_id = 99  → hash(99) mod 16 = 3  → Slice 3
```

**Properties:**
- Co-located joins are possible when both tables use the same DISTKEY column
- Only ONE column can be the DISTKEY
- The column should have high cardinality (many distinct values) for even distribution
- The column should be the most frequently used join column

### EVEN Distribution

```sql
CREATE TABLE staging_events (...)
DISTSTYLE EVEN;
```

**How it works**: Round-robin assignment. Row 1 → Slice 0, Row 2 → Slice 1, Row 3 → Slice 2, etc.

**Properties:**
- Guarantees perfectly uniform distribution (no skew possible)
- **No co-location for any join** — every join requires redistribution
- Best for: staging tables, tables not used in joins, tables where no column is a good DISTKEY

### ALL Distribution

```sql
CREATE TABLE regions (
  region_id   INT,
  region_name VARCHAR(50),
  country     VARCHAR(50)
)
DISTSTYLE ALL;
```

**How it works**: The **entire table** is copied to every compute node. Every slice on every node has a full copy.

**Properties:**
- Any join with an ALL-distributed table is co-located (the full table is always local)
- Storage cost: N× (one copy per node, not per slice)
- Write cost: Every INSERT/UPDATE/DELETE must execute on all nodes
- Best for: Small, slowly-changing dimension tables (< 1M rows)

**Size guidelines:**
- Under 200K rows → almost always good for ALL
- 200K - 1M rows → consider ALL if frequently joined
- Over 1M rows → usually too expensive for ALL (storage + write overhead)

[INFERRED — these thresholds are not officially documented but are widely used in Redshift optimization guides]

### AUTO Distribution (Default)

```sql
CREATE TABLE my_table (...)
DISTSTYLE AUTO;  -- this is the default if not specified
```

**How it works**: Redshift automatically chooses:
- **ALL** for small tables (full replication)
- **EVEN** when the table grows beyond the ALL threshold

AUTO does **not** automatically select KEY distribution — choosing the right join column requires understanding the workload, which Redshift doesn't infer automatically.

---

## 3. Choosing a Distribution Key

### The DISTKEY Selection Algorithm

```
Step 1: Identify the table's most common JOINs
  - Look at the ETL pipeline and dashboard queries
  - Which column appears in JOIN ON clauses most often?

Step 2: Check cardinality of the candidate column
  - SELECT COUNT(DISTINCT candidate_col), COUNT(*) FROM table;
  - Ratio should be > 0.01 (at least 1% distinct values)
  - Ideally > 0.1 for good distribution

Step 3: Check distribution uniformity
  - SELECT candidate_col, COUNT(*) FROM table GROUP BY 1 ORDER BY 2 DESC LIMIT 10;
  - Top value should not exceed 5% of total rows
  - If top value is 40% of rows → that's a hot slice → bad DISTKEY

Step 4: Check if the join partner uses the same DISTKEY
  - Both sides of the JOIN must be distributed on the JOIN column
  - orders.customer_id (DISTKEY) JOIN customers.id (DISTKEY) → co-located
  - orders.customer_id (DISTKEY) JOIN products.id (DISTKEY) → NOT co-located
    (Different columns with different hash distributions)
```

### Common Patterns

| Table Type | Recommended DISTKEY | Rationale |
|---|---|---|
| **Fact table** (orders, events, transactions) | Most frequently joined foreign key (e.g., `customer_id`) | Co-locates with the largest dimension |
| **Large dimension** (customers, products) | Primary key (`id`) that matches fact table's FK | Enables co-located join with fact table |
| **Small dimension** (regions, categories, statuses) | DISTSTYLE ALL | Replicated everywhere, always co-located |
| **Staging table** | DISTSTYLE EVEN | Not joined; uniform distribution for parallel load |
| **Denormalized wide table** | Column used most in JOINs with other tables, or EVEN if standalone | Depends on query patterns |

### The Single-DISTKEY Limitation

Each table has exactly one DISTKEY. If a fact table is joined with 5 dimensions:

```sql
-- Which one should be the DISTKEY?
orders JOIN customers ON orders.customer_id = customers.id    -- DISTKEY candidate 1
orders JOIN products ON orders.product_id = products.id       -- DISTKEY candidate 2
orders JOIN stores ON orders.store_id = stores.id             -- Small table → ALL
orders JOIN dates ON orders.date_id = dates.id                -- Small table → ALL
orders JOIN categories ON orders.cat_id = categories.id       -- Small table → ALL
```

Strategy:
1. Small dimensions (stores, dates, categories) → DISTSTYLE ALL (co-located by replication)
2. Between customers and products, pick the one involved in the most expensive join (typically the one that produces the most rows or is used most frequently)
3. The other large dimension accepts redistribution cost

---

## 4. Data Redistribution During Joins

When a join can't be satisfied with co-located data, Redshift has three redistribution strategies:

### Strategy 1: No Redistribution (Co-located)

```
Query: orders (DISTKEY customer_id) JOIN customers (DISTKEY id)
       ON orders.customer_id = customers.id

Slice 0: orders(cust_id hashes to 0) + customers(id hashes to 0) → local join
Slice 1: orders(cust_id hashes to 1) + customers(id hashes to 1) → local join
...

Network cost: 0
```

### Strategy 2: Broadcast

```
Query: orders (DISTKEY customer_id) JOIN regions (DISTSTYLE EVEN, 200 rows)
       ON orders.region_id = regions.id

Regions is small → broadcast to all nodes:
  Each node receives a full copy of regions (200 × 100B = 20 KB per node)
  Then join locally

Network cost: 200 rows × 100 bytes × num_nodes = trivial
```

The optimizer chooses broadcast when one side of the join is small enough that copying it everywhere is cheaper than hash-redistributing both sides.

### Strategy 3: Hash Redistribution

```
Query: orders (DISTKEY customer_id) JOIN products (DISTKEY product_id)
       ON orders.product_id = products.product_id

Neither table is distributed on the join column (product_id).
Option A: Redistribute orders on product_id (expensive: 100M rows move)
Option B: Redistribute products on product_id (cheaper: 1M rows move)
Option C: Redistribute BOTH on product_id (most expensive)

Optimizer picks the cheapest option based on table sizes.

Network cost: Size of redistributed table(s)
```

### How to Identify Redistribution in EXPLAIN

```sql
EXPLAIN
SELECT c.name, SUM(o.amount)
FROM orders o JOIN customers c ON o.customer_id = c.id
GROUP BY c.name;
```

Look for these operations in the EXPLAIN output:
- `DS_DIST_NONE` → co-located join (no redistribution)
- `DS_BCAST_INNER` → inner table broadcast to all nodes
- `DS_DIST_BOTH` → both tables redistributed (worst case)
- `DS_DIST_ALL_NONE` → one table is ALL-distributed (always local)
- `DS_DIST_INNER` → inner table redistributed on join key
- `DS_DIST_OUTER` → outer table redistributed on join key

**Performance hierarchy** (best to worst):
1. `DS_DIST_NONE` (co-located) — zero network cost
2. `DS_DIST_ALL_NONE` (ALL table) — zero network cost (data already replicated)
3. `DS_BCAST_INNER` (broadcast small table) — small network cost
4. `DS_DIST_INNER` or `DS_DIST_OUTER` (redistribute one table) — moderate network cost
5. `DS_DIST_BOTH` (redistribute both) — maximum network cost

---

## 5. Data Skew: Detection and Mitigation

### What Is Data Skew?

Data skew occurs when the DISTKEY column has non-uniform distribution — some values appear much more frequently than others.

```
Example: DISTKEY(customer_id), but one enterprise customer has 40M orders out of 100M total

Slice 0: 40M rows (enterprise customer)  ← HOT SLICE
Slice 1:  4M rows
Slice 2:  4M rows
...
Slice 15: 4M rows

Query time = max(slice_time) = Slice 0's time
Slice 0 takes 10x longer than other slices → 90% of cluster sits idle
```

### Detecting Skew

```sql
-- Check distribution across slices
SELECT slice, COUNT(*) as rows
FROM stv_blocklist
WHERE tbl = (SELECT id FROM stv_tbl_perm WHERE name = 'orders')
GROUP BY slice
ORDER BY rows DESC;

-- Check DISTKEY value distribution
SELECT customer_id, COUNT(*) as cnt
FROM orders
GROUP BY customer_id
ORDER BY cnt DESC
LIMIT 20;
```

Key metrics:
- **Skew ratio** = max(rows_per_slice) / avg(rows_per_slice)
  - Ratio < 1.5 → healthy
  - Ratio 1.5 - 3.0 → moderate skew, investigate
  - Ratio > 3.0 → severe skew, change DISTKEY

### Mitigating Skew

| Strategy | When to Use |
|---|---|
| Choose a higher-cardinality DISTKEY | The best fix — find a column with more distinct values and even distribution |
| Switch to DISTSTYLE EVEN | If no column has good distribution; accept redistribution cost on joins |
| Pre-aggregate or filter before joining | Reduce the data volume before it hits the skewed slice |
| Composite key approach [INFERRED] | Concatenate two columns into a synthetic key to increase cardinality |

---

## 6. Sort Keys: Compound vs Interleaved vs AUTO

### Compound Sort Keys

```sql
CREATE TABLE orders (
  order_id      BIGINT,
  order_date    DATE,
  region        VARCHAR(10),
  customer_id   BIGINT,
  amount        DECIMAL(10,2)
)
COMPOUND SORTKEY (order_date, region);
```

Data is sorted by `order_date` first, then by `region` within each date.

**Zone map effectiveness by predicate pattern:**

| Predicate | Zone Map Skip Rate | Why |
|---|---|---|
| `WHERE order_date = '2024-01-15'` | **95-98%** | First sort column → data is physically grouped |
| `WHERE order_date = '2024-01-15' AND region = 'US'` | **98-99%** | Both columns in prefix order → very tight blocks |
| `WHERE region = 'US'` (no date filter) | **~0%** | Skips first column → region values spread across all date blocks |
| `WHERE amount > 1000` | **~0%** | Not a sort key column → no physical grouping |

**Key insight**: With compound sort keys, the **first column** is king. If your queries always filter by date, make date the first sort column. If 50% of queries filter by date and 50% by region, compound sort key only helps the 50% that filter by date.

### Interleaved Sort Keys

```sql
CREATE TABLE orders (...)
INTERLEAVED SORTKEY (order_date, region, product_id);
```

Interleaved sort keys use a **Z-order curve** (Morton code) to give **equal weight** to all specified columns. Instead of sorting strictly by the first column, it interleaves bits from each column to create a multidimensional sort order.

**Zone map effectiveness by predicate pattern:**

| Predicate | Zone Map Skip Rate | Why |
|---|---|---|
| `WHERE order_date = '2024-01-15'` | **80-90%** | Good, but not as good as compound (shared weight) |
| `WHERE region = 'US'` | **80-90%** | ALSO good (unlike compound where this was 0%) |
| `WHERE product_id = 42` | **80-90%** | ALSO good (all columns get equal weight) |
| `WHERE order_date = '2024-01-15' AND region = 'US'` | **95%+** | Multiple interleaved columns → very effective |

**Constraints:**
- Maximum **8 columns** in an interleaved sort key
- VACUUM REINDEX is **much more expensive** than compound VACUUM SORT
- Not recommended for columns that increase monotonically (timestamps, auto-increment IDs) — the Z-order curve doesn't work well with always-increasing values

### AUTO Sort Key

When no sort key is specified, Redshift uses AUTO and may automatically select a sort key based on observed query patterns. Redshift monitors which columns appear in WHERE clauses and may set a compound sort key based on usage.

### Decision Matrix: Compound vs Interleaved

| Criterion | Compound | Interleaved |
|---|---|---|
| Query pattern | Always filters by the same first column | Ad-hoc filtering on any column |
| Number of filter columns | 1-2 predictable columns | 3+ unpredictable columns |
| VACUUM cost | Low (simple sort) | **High** (Z-order recomputation) |
| Monotonically increasing columns | Works well (timestamps are great first columns) | **Bad** (Z-order degrades) |
| Max sort key columns | Unlimited | **8** |
| Concurrency scaling compatibility | Yes | **No** — queries with interleaved sort keys are not eligible |
| Typical use case | Time-series data, log analytics | Ad-hoc BI exploration |

**Recommendation**: Use compound sort keys for 90% of use cases. Most analytical workloads have a dominant time-based filter. Interleaved is for genuinely unpredictable access patterns.

---

## 7. Sort Keys and Zone Map Interaction

### The Synergy

Sort keys create physical ordering → zone maps record the resulting narrow min/max ranges → query engine uses zone maps to skip blocks.

```
Table: orders, COMPOUND SORTKEY (order_date)
1 billion rows, ~4 years of data

Column 'order_date':
Block 0:   min=2021-01-01, max=2021-01-08   (1 week of data per block)
Block 1:   min=2021-01-09, max=2021-01-15
...
Block 208: min=2024-12-25, max=2024-12-31

Query: WHERE order_date BETWEEN '2024-06-01' AND '2024-06-30'
→ Zone map scan: blocks 182-185 match (4 out of 208)
→ Skip: 204/208 blocks = 98% skipped
→ Read: 4 blocks × 1 MB = 4 MB instead of 208 MB
```

### Impact of Unsorted Data on Zone Maps

After bulk loads without VACUUM, the table has a sorted region and an unsorted region:

```
Sorted region (from initial load + VACUUM):
Block 0:   min=2021-01-01, max=2021-01-08   ← tight zone map
Block 1:   min=2021-01-09, max=2021-01-15   ← tight zone map
...
Block 200: min=2024-10-01, max=2024-10-08   ← tight zone map

Unsorted region (from recent COPY loads):
Block 201: min=2024-11-15, max=2024-12-28   ← wide zone map
Block 202: min=2024-11-01, max=2024-12-31   ← wide zone map
Block 203: min=2024-11-20, max=2024-12-15   ← wide zone map

Query: WHERE order_date = '2024-12-01'
→ Sorted region: check blocks 196-200 → maybe 1 match
→ Unsorted region: check blocks 201-203 → ALL match (wide ranges)
→ Reads: 1 sorted block + 3 unsorted blocks = 4 blocks
→ Without unsorted region: would read 1 block
→ 4x more I/O due to unsorted data
```

This is why regular VACUUM SORT is important after bulk loads.

### Zone Maps on Non-Sort-Key Columns with Distribution

Even without a sort key, the distribution key creates natural clustering:

```
Table: orders, DISTKEY(customer_id), no sort key

Slice 0 has customers with hash(id) = 0:
  customer_ids: {5, 21, 37, 53, ...}  (hash collisions to same slice)

Block 0 on Slice 0, column 'customer_id':
  values: [5, 5, 5, 21, 21, 37, ...]  min=5, max=53
  → Narrow range! Zone map can skip blocks for customer_id filters

Block 0 on Slice 0, column 'order_date':
  values: [2024-01-05, 2023-06-15, 2022-11-30, ...]  min=2022-11-30, max=2024-01-05
  → Wide range! Zone map can't skip (dates aren't sorted within the slice)
```

Distribution creates clustering for the DISTKEY column but not for other columns — that's what sort keys are for.

---

## 8. VACUUM and Sort Maintenance

### The Unsorted Region Problem

```
Timeline of a table's sort state:

Day 1: Initial COPY load → 100% sorted → zone maps effective
Day 2: COPY 5% new data → 95% sorted, 5% unsorted
Day 3: COPY 5% new data → 90% sorted, 10% unsorted
...
Day 20: → 0% sorted, 100% unsorted → zone maps useless

Without VACUUM, query performance degrades continuously.
```

### VACUUM SORT Mechanics

```
Before VACUUM SORT:
┌────────────────────────────────────────┐
│ Sorted region (blocks 0-200):          │
│   Tight zone maps, effective skipping  │
│                                        │
│ Unsorted region (blocks 201-220):      │
│   Wide zone maps, no effective skipping│
└────────────────────────────────────────┘

VACUUM SORT process:
1. Read all unsorted blocks
2. Sort them by the sort key
3. Merge with the sorted region (merge sort)
4. Write new blocks with correct sort order
5. Update zone maps
6. Mark old blocks for deletion

After VACUUM SORT:
┌────────────────────────────────────────┐
│ All blocks sorted (0-218):             │
│   Tight zone maps throughout           │
└────────────────────────────────────────┘
```

### VACUUM Performance Characteristics

| Factor | Impact |
|---|---|
| Unsorted region size | Larger unsorted region = longer VACUUM |
| Table size | Larger tables take longer (more data to merge) |
| Sort key type | Interleaved REINDEX is 5-10x slower than compound SORT [INFERRED] |
| Concurrent queries | VACUUM competes for resources; may slow concurrent queries |
| Frequency | More frequent = faster each time (smaller unsorted regions) |

### Automatic VACUUM

Redshift runs automatic VACUUM in the background:
- **VACUUM DELETE**: Reclaims space from deleted rows automatically
- **VACUUM SORT**: Re-sorts tables with significant unsorted percentages

Monitoring:
```sql
-- Check unsorted percentage
SELECT "table", unsorted, size, tbl_rows
FROM SVV_TABLE_INFO
WHERE unsorted > 5  -- tables with > 5% unsorted data
ORDER BY unsorted DESC;
```

---

## 9. Star Schema Design Patterns

### Classic Star Schema Layout

```
                    ┌──────────────────┐
                    │ dim_date         │
                    │ DISTSTYLE ALL    │
                    │ SORTKEY (date)   │
                    └────────┬─────────┘
                             │
┌───────────────┐   ┌───────┴──────────┐   ┌───────────────┐
│ dim_customers │   │   fact_orders     │   │ dim_products  │
│ DISTKEY (id)  │───│ DISTKEY (cust_id) │───│ DISTSTYLE ALL │
│ SORTKEY (id)  │   │ SORTKEY (date,    │   │ SORTKEY (id)  │
└───────────────┘   │   region)         │   └───────────────┘
                    └───────┬──────────┘
                            │
                    ┌───────┴──────────┐
                    │ dim_regions      │
                    │ DISTSTYLE ALL    │
                    │ SORTKEY (id)     │
                    └──────────────────┘
```

**Design rationale:**
- `fact_orders` has DISTKEY on `customer_id` (most frequent large-table join)
- `dim_customers` has DISTKEY on `id` (matching FK → co-located join)
- `dim_date`, `dim_products`, `dim_regions` are small → DISTSTYLE ALL (always co-located)
- `fact_orders` has COMPOUND SORTKEY on `(date, region)` — most queries filter by date first

### The Multi-Fact-Table Challenge

When you have multiple fact tables that join with each other:

```
fact_orders (DISTKEY customer_id)  JOIN  fact_returns (DISTKEY order_id)
ON fact_orders.order_id = fact_returns.order_id

Problem: DISTKEY mismatch — orders distributed by customer_id,
returns distributed by order_id. Hash redistribution required.
```

**Options:**
1. Change `fact_returns` DISTKEY to `customer_id` — if returns also frequently join with customers
2. Pre-join in ETL — create a denormalized table with orders + returns
3. Accept redistribution — if this join is infrequent

### Summary: The Distribution Strategy Recipe

```
For each table in your schema:
1. Is it a small dimension (< 1M rows)?
   → DISTSTYLE ALL

2. Is it a fact table or large dimension?
   → DISTSTYLE KEY
   → DISTKEY = most frequently joined column with high cardinality and even distribution

3. Is it a staging/temporary table not used in joins?
   → DISTSTYLE EVEN

4. For sort keys:
   → COMPOUND SORTKEY with the most-filtered column first (usually date/timestamp)
   → Add 1-2 more columns that commonly appear in WHERE clauses
```

---

## 10. Design Decisions & Tradeoffs

### Distribution Key vs Even Distribution

| Factor | KEY Distribution | EVEN Distribution |
|---|---|---|
| Join performance | **Excellent** (co-located joins) | Poor (always redistributes) |
| Distribution uniformity | Depends on data | **Always uniform** |
| Risk of hot slices | Yes (skewed data) | **None** |
| Maintenance complexity | Must choose wisely | **Zero** |
| Best for | Production fact + dimension tables | Staging, temp tables, unknown patterns |

### Compound vs Interleaved Sort Keys — When Each Wins

**Compound wins when:**
- 80%+ of queries filter by the same column (time-series, log analytics)
- You have a monotonically increasing column (date, timestamp)
- VACUUM frequency is a concern
- You use concurrency scaling

**Interleaved wins when:**
- Query patterns are genuinely unpredictable (ad-hoc exploration)
- Multiple columns are equally likely to appear in WHERE clauses
- You can tolerate expensive VACUUM REINDEX operations
- You don't need concurrency scaling

### ALL Distribution — Costs and Limits

```
Table: dim_products, 500K rows, 200 bytes/row = 100 MB

DISTSTYLE ALL on a 16-node cluster:
  Storage: 100 MB × 16 = 1.6 GB (16× overhead)
  INSERT 1 row: writes to 16 nodes (16× write amplification)
  UPDATE 1 row: updates on 16 nodes (16× write amplification)

DISTSTYLE ALL on a 128-node cluster:
  Storage: 100 MB × 128 = 12.8 GB (128× overhead)
  INSERT 1 row: writes to 128 nodes (128× write amplification)
```

ALL is viable because dimension tables are small and change infrequently. But scale the cluster and the overhead scales linearly.

### The DISTKEY Change Problem

Changing a DISTKEY requires recreating the table:

```sql
-- Cannot ALTER TABLE to change DISTKEY
-- Must: CREATE new table → INSERT INTO new FROM old → DROP old → RENAME new

CREATE TABLE orders_new (...)
DISTSTYLE KEY
DISTKEY (new_column);

INSERT INTO orders_new SELECT * FROM orders;
DROP TABLE orders;
ALTER TABLE orders_new RENAME TO orders;
```

This is a blocking operation on a large table. Plan DISTKEY choices carefully upfront.
