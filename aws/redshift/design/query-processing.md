# Deep Dive: Query Processing & Execution in Amazon Redshift

> Companion document to [interview-simulation.md](./interview-simulation.md)

---

## Table of Contents

1. [End-to-End Query Lifecycle](#1-end-to-end-query-lifecycle)
2. [Leader Node: Parsing, Optimization, Planning](#2-leader-node-parsing-optimization-planning)
3. [Execution Plan: Steps, Segments, Streams](#3-execution-plan-steps-segments-streams)
4. [Code Compilation and Caching](#4-code-compilation-and-caching)
5. [Parallel Execution on Compute Nodes](#5-parallel-execution-on-compute-nodes)
6. [Result Caching](#6-result-caching)
7. [Short Query Acceleration (SQA)](#7-short-query-acceleration-sqa)
8. [EXPLAIN Plans: Reading and Optimizing](#8-explain-plans-reading-and-optimizing)
9. [Query Performance Factors](#9-query-performance-factors)
10. [Design Decisions & Tradeoffs](#10-design-decisions--tradeoffs)

---

## 1. End-to-End Query Lifecycle

```
┌────────────────────────────────────────────────────────────────┐
│                     Query Lifecycle                              │
│                                                                  │
│  1. Client → Leader (port 5439, PostgreSQL wire protocol)       │
│  2. Leader: Parse SQL → Query Tree                              │
│  3. Leader: Optimize → Execution Plan (cheapest strategy)       │
│  4. Leader: Check result cache → if hit, return immediately     │
│  5. Leader: Compile plan → Steps → Segments → Streams           │
│  6. Leader: Broadcast compiled code to all compute node slices  │
│  7. Slices: Execute segments in parallel (Stream 1)             │
│  8. Slices: Return Stream 1 results → Leader starts Stream 2   │
│  9. Repeat for each stream                                      │
│ 10. Compute nodes: Send final results to Leader                 │
│ 11. Leader: Merge, apply final ORDER BY/LIMIT, return to client│
└────────────────────────────────────────────────────────────────┘
```

**Key timing breakdown for a typical 5-second analytical query:**

| Phase | Time | Notes |
|---|---|---|
| Network round-trip (client → leader) | ~1 ms | Trivial |
| Parsing | ~1-5 ms | SQL → query tree |
| Optimization | 10-100 ms | Cost-based optimizer evaluating join orders |
| Code compilation (first run) | 100-500 ms | Generates native code; cached for subsequent runs |
| Code compilation (cached) | ~5 ms | Cache lookup |
| Broadcast to compute nodes | ~5-10 ms | Code distribution over high-bandwidth interconnect |
| Parallel execution | 3-4 sec | **Dominates** — scanning, joining, aggregating |
| Result aggregation at leader | 50-200 ms | Merging partial results |
| Return to client | ~1-10 ms | Depends on result set size |

[INFERRED — exact timing breakdown is not officially documented; these are representative ranges based on observed behavior]

---

## 2. Leader Node: Parsing, Optimization, Planning

### The Leader Node's Role

The leader node is the **single entry point** for all client connections. It handles:
- Connection management (up to 2,000 connections for RA3 / 500 for dc2.large)
- SQL parsing and validation
- Query optimization (cost-based)
- Execution plan generation
- Code compilation
- Result caching
- Result aggregation and return

The leader node does **not** store user table data — it stores only system catalog tables, query plans, and cached results.

### Query Optimization

The optimizer is cost-based and considers:

1. **Join ordering**: For a query joining 5 tables, there are 5! = 120 possible join orders. The optimizer evaluates costs to find the cheapest.

2. **Join type selection**:
   - **Hash join**: Build hash table on the smaller table, probe with the larger. Best for equi-joins.
   - **Merge join**: Both sides sorted on the join key. Efficient when data is pre-sorted (sort key matches join key).
   - **Nested loop join**: Only for non-equi-joins or very small tables. Rare in Redshift.

3. **Redistribution strategy**: Co-located, broadcast, or hash redistribution (see [distribution-and-sort-keys.md](./distribution-and-sort-keys.md)).

4. **Aggregation strategy**:
   - **Hash aggregate**: Build hash table of group keys. Good for many groups.
   - **Sort aggregate**: Sort by group keys, then scan. Good when data is already sorted.

5. **Materialized view rewriting**: Can the query be satisfied by an existing MV?

### The Importance of Statistics (ANALYZE)

The optimizer relies on table statistics to estimate costs:
- Row count
- Distinct value count per column
- Value distribution histograms
- NULL counts

**If statistics are stale** (e.g., after a large COPY without ANALYZE), the optimizer makes bad decisions:
- Chooses broadcast when hash redistribution would be cheaper
- Chooses wrong join order (joins large tables first)
- Underestimates result sizes, causing memory spills to disk

```sql
-- Update statistics after bulk load
ANALYZE orders;

-- Or let COPY do it automatically
COPY orders FROM 's3://...'
IAM_ROLE '...'
STATUPDATE ON;  -- updates statistics automatically after load
```

---

## 3. Execution Plan: Steps, Segments, Streams

The optimizer produces a logical plan, and the execution engine translates it into a physical execution plan with three levels:

### Steps

A **step** is the smallest operation unit:
- **Scan step**: Read column blocks from disk/cache, apply zone map filtering
- **Hash step**: Build a hash table for a join
- **Probe step**: Probe the hash table with rows from the other side
- **Aggregate step**: Compute SUM, COUNT, AVG, etc.
- **Sort step**: Sort rows by specified columns
- **Return step**: Send results back to the leader or to the next segment
- **Redistribute step**: Hash-distribute rows to other slices for the next stage

### Segments

A **segment** is a pipeline of steps that can execute **without data exchange** between slices. It's the smallest unit of compiled code sent to a slice.

```
Segment example (simple scan + filter + aggregate):
┌─────────────────────────┐
│ Segment 0:               │
│  Step 1: Scan 'orders'   │  ← Read from disk/cache
│  Step 2: Filter date>... │  ← Apply WHERE clause
│  Step 3: Partial Agg     │  ← Compute partial SUM
│  Step 4: Return to net   │  ← Send to next segment
└─────────────────────────┘
```

All steps within a segment run as a **pipeline** — data flows from step 1 through step 4 without materializing intermediate results to disk. This is a key performance optimization.

### Streams

A **stream** is a collection of segments that execute together across all slices. Streams execute **sequentially** — Stream 1 must complete before Stream 2 begins.

```
Stream 0: Scan + Filter + Partial Aggregate (on all slices in parallel)
          ↓ (data redistributed between streams)
Stream 1: Merge partial results + Final Aggregate (on all slices)
          ↓
Stream 2: Sort + Return to Leader
```

**Why sequential streams?** Because each stream's output may need to be redistributed before the next stream can process it. The leader waits for Stream N to complete and analyzes the results (e.g., detecting disk spilling) before generating the segments for Stream N+1.

### Visualizing a JOIN Query

```sql
SELECT c.region, SUM(o.amount)
FROM orders o
JOIN customers c ON o.customer_id = c.id
WHERE o.date > '2024-01-01'
GROUP BY c.region;
```

```
Stream 0:
  Segment 0 (runs on each slice):
    Step: Scan orders (zone map filter on date > '2024-01-01')
    Step: Filter remaining rows
    Step: Hash on customer_id → send to network

  Segment 1 (runs on each slice):
    Step: Scan customers
    Step: Build hash table on id

Stream 1:
  Segment 2 (runs on each slice):
    Step: Receive redistributed orders rows
    Step: Probe customers hash table (join)
    Step: Project region, amount
    Step: Partial aggregate (SUM by region)
    Step: Return to leader

Leader: Merge partial aggregates, apply final GROUP BY, return result
```

---

## 4. Code Compilation and Caching

### How Compilation Works

Redshift does **not** interpret query plans like traditional databases. Instead, it **compiles** each segment into optimized native code.

```
Query Plan Segment
       │
       ▼
┌──────────────────┐
│ Code Generator    │
│                    │
│ Translates plan   │
│ operations into   │
│ native code       │
│ optimized for the │
│ specific query    │
└────────┬─────────┘
         │
         ▼
┌──────────────────┐
│ Compiled Code     │
│                    │
│ Runs faster than  │
│ interpreted code  │
│ Uses less compute │
│ capacity          │
└──────────────────┘
```

**Benefits of compilation over interpretation:**
- No per-row interpretation overhead
- CPU branch prediction works better with compiled code
- Compiler can optimize across multiple steps (e.g., fusing a scan + filter into one loop)

### Compilation Caching

```
First execution of a query:
  Parse (5 ms) → Optimize (50 ms) → COMPILE (300 ms) → Execute (3 sec) → Return
  Total: ~3.4 sec

Second execution (same query plan):
  Parse (5 ms) → Optimize (50 ms) → CACHE HIT (5 ms) → Execute (3 sec) → Return
  Total: ~3.1 sec (saved 295 ms)
```

The compiled code cache:
- Stored **locally** on the cluster
- Also stored in a **virtually unlimited external cache** (survives reboots)
- **Invalidated on version upgrades** — after a Redshift version upgrade, all queries experience first-run compilation overhead

### Benchmarking Implication

From AWS documentation: **"When benchmarking your queries, you should always compare the times for the second execution of a query, because the first execution time includes the overhead of compiling the code."**

This is critical for accurate performance measurement. A 500 ms compilation overhead can make a 2-second query appear 25% slower on first run.

---

## 5. Parallel Execution on Compute Nodes

### Slice-Level Parallelism

Each compute node is divided into **slices**. Each slice has dedicated:
- CPU cores
- Memory
- Disk I/O bandwidth

| Node Type | Slices per Node | With 128 Nodes |
|---|---|---|
| ra3.xlplus | 2 | 256 slices |
| ra3.4xlarge | 4 | 512 slices |
| ra3.16xlarge | 16 | 2,048 slices |
| dc2.large | 2 | 256 slices |
| dc2.8xlarge | 16 | 2,048 slices |

A query on a cluster with 2,048 slices executes with 2,048-way parallelism. Each slice processes its local data independently, and results are merged.

### Execution Flow on a Slice

```
Slice N executing Segment 0:

1. Read zone maps for target columns
2. Identify blocks that pass predicates (skip others)
3. Read qualifying compressed blocks from disk/cache
4. Decompress blocks in memory
5. Apply WHERE clause to individual rows
6. Compute partial aggregate (if applicable)
7. Send results to network (for redistribution) or to next segment
```

Each slice works independently — no communication with other slices during a segment. Communication only happens **between segments** (during redistribution) or at the end (returning results to the leader).

### Network Communication Between Slices

```
During redistribution (between streams):

Slice 0 ─── hash(row.key) mod N = 3 ───→ Slice 3
Slice 1 ─── hash(row.key) mod N = 0 ───→ Slice 0
Slice 2 ─── hash(row.key) mod N = 1 ───→ Slice 1
Slice 3 ─── hash(row.key) mod N = 2 ───→ Slice 2

Every slice may send data to every other slice.
This is an all-to-all shuffle — the most expensive network operation.
```

The inter-node network bandwidth is critical for redistribution performance. RA3 and DC2 nodes use high-bandwidth cluster networking (typically 25 Gbps+).

---

## 6. Result Caching

### How Result Caching Works

When a query is executed, the leader node caches the result. On subsequent identical queries:

```
Query arrives at Leader
       │
       ▼
┌──────────────────────┐
│ Result Cache Check:    │
│                        │
│ 1. Same SQL text?      │
│ 2. Same user?          │──→ Cache HIT → Return cached result
│ 3. Underlying data     │                (no compute node work)
│    unchanged?          │
│ 4. Same session params?│──→ Cache MISS → Execute on compute nodes
└──────────────────────┘
```

**Cache invalidation**: The cache is automatically invalidated when:
- The underlying tables are modified (INSERT, UPDATE, DELETE, COPY)
- Table DDL changes (ALTER TABLE, DROP TABLE)
- Dependent objects change (function definitions, etc.)

**Cache scope**: Results are cached on the leader node of the specific cluster. Different clusters (including concurrency scaling clusters) do not share result caches.

### When Result Caching Helps

| Scenario | Cache Benefit |
|---|---|
| BI dashboard refreshing every 5 min, same queries | **Huge** — dashboard queries return instantly from cache |
| ETL query running once on new data | **None** — data always changes, cache always misses |
| Ad-hoc analyst queries | **Moderate** — same analyst may re-run queries |

---

## 7. Short Query Acceleration (SQA)

### The Problem SQA Solves

Without SQA:
```
WLM Queue (5 slots, all in use):
  Slot 1: ETL query (running 10 min)
  Slot 2: Dashboard query (would finish in 0.5 sec) ← WAITING for a slot
  Slot 3: Report query (running 3 min)
  Slot 4: ETL query (running 8 min)
  Slot 5: Report query (running 5 min)

Wait queue: [Dashboard query 0.5s, Dashboard query 0.3s, ...]

Dashboard queries wait minutes behind ETL jobs, even though they'd finish in milliseconds.
```

### How SQA Works

```
Query arrives
     │
     ▼
┌────────────────────────┐
│ SQA Prediction Model    │
│                          │
│ Estimates query runtime  │
│ based on:                │
│ - Compilation time       │
│ - Number of segments     │
│ - Table statistics       │
│ - Historical patterns    │
│                          │
│ Predicted < threshold?   │
├───────────┬──────────────┤
│ YES       │ NO           │
│ → SQA     │ → Regular    │
│   fast    │   WLM queue  │
│   lane    │              │
└───────────┴──────────────┘
```

**SQA properties:**
- Uses machine learning to predict whether a query will be short
- Short queries bypass the WLM queue and execute immediately in a dedicated fast lane
- No manual configuration needed — SQA is enabled by default with automatic WLM
- If the prediction is wrong (query runs longer than expected), the query is moved to the regular WLM queue

**The risk of false positives**: If SQA predicts a query is short but it's actually long, it consumes fast-lane resources. The system handles this by ejecting long-running queries from the fast lane.

---

## 8. EXPLAIN Plans: Reading and Optimizing

### Running EXPLAIN

```sql
EXPLAIN
SELECT c.region, SUM(o.amount) as total
FROM orders o
JOIN customers c ON o.customer_id = c.id
WHERE o.order_date > '2024-01-01'
GROUP BY c.region
ORDER BY total DESC
LIMIT 10;
```

### Key EXPLAIN Operators

| Operator | Meaning | Performance Implication |
|---|---|---|
| `XN Seq Scan` | Sequential scan of a table | Normal — reads all blocks (after zone map filtering) |
| `XN Hash Join` | Hash join between two tables | Normal for equi-joins |
| `XN Merge Join` | Merge join (both sides sorted) | Efficient when sort keys align with join key |
| `XN Hash` | Building hash table for join | Memory usage — large hash tables may spill to disk |
| `XN Sort` | Sorting rows | Can be expensive; check if sort key avoids this |
| `XN Aggregate` | Computing aggregates (SUM, COUNT) | Normal |
| `XN HashAggregate` | Hash-based aggregation | Good for many groups |
| `XN Network` | Sending data across the network | Redistribution — check DS_DIST type |
| `XN Limit` | Applying LIMIT | Applied at leader — reduces final result size |

### Distribution Labels in EXPLAIN

| Label | Meaning | Action |
|---|---|---|
| `DS_DIST_NONE` | Co-located join | Best case — no tuning needed |
| `DS_DIST_ALL_NONE` | One table is ALL-distributed | Good — no data movement |
| `DS_BCAST_INNER` | Inner table broadcast to all nodes | OK for small inner tables |
| `DS_DIST_INNER` | Inner table redistributed on join key | Moderate cost — consider changing DISTKEY |
| `DS_DIST_OUTER` | Outer table redistributed | Moderate cost |
| `DS_DIST_BOTH` | Both tables redistributed | **Worst case** — change DISTKEY or add ALL |

### Reading the Cost Estimate

```
→  XN Hash Join DS_DIST_NONE  (cost=0.00..1234567.89 rows=50000000 width=24)
                                     ^^^^^^^^^^^^^^^^ ^^^^^^^^^^^^^
                                     estimated cost   estimated rows
```

- **cost**: Relative cost units (not seconds). Lower is better. Compare costs between different plan options.
- **rows**: Estimated number of rows processed. If wildly inaccurate (e.g., estimates 1,000 rows but actually 10M), run ANALYZE.

---

## 9. Query Performance Factors

### Factors That Impact Query Speed

| Factor | Impact | How to Optimize |
|---|---|---|
| **Number of slices** | More slices = more parallelism | Add nodes or use larger node types |
| **Data distribution** | Skewed data → hot slices | Choose better DISTKEY, check skew |
| **Sort keys** | Good sort key → zone map block skipping | Use compound sort key on most-filtered column |
| **Compression** | Better compression → less I/O | Use ANALYZE COMPRESSION, apply recommended encodings |
| **Table statistics** | Stale stats → bad query plans | Run ANALYZE after bulk loads |
| **Unsorted data** | Unsorted region → zone maps ineffective | Run VACUUM after loads |
| **Concurrent queries** | Many queries → resource contention | Configure WLM, use concurrency scaling |
| **Result set size** | Large results → leader node bottleneck | Use UNLOAD for large exports, add LIMIT |
| **Disk spilling** | Insufficient memory → queries spill to disk | Increase WLM slot memory, reduce concurrency |
| **Data volume** | More data → longer scans | Archive old data, use Spectrum for cold data |

### The Performance Diagnostic Checklist

```
Query running slow? Follow this checklist:

1. Check EXPLAIN plan
   - Look for DS_DIST_BOTH → change distribution
   - Look for unexpected full table scans → check sort keys/zone maps

2. Check table health
   - SVV_TABLE_INFO: unsorted%, stats off?
   - Run VACUUM if unsorted > 20%
   - Run ANALYZE if stats are stale

3. Check data skew
   - stv_blocklist: rows per slice
   - If skew ratio > 3 → change DISTKEY

4. Check WLM
   - STL_WLM_QUERY: was the query queued? For how long?
   - If queued for > 30 sec → add concurrency or use SQA

5. Check disk spilling
   - STL_QUERY_METRICS: spill_to_disk?
   - If spilling → increase slot memory or reduce concurrency

6. Check compilation
   - First-run penalty? Re-run and compare.
```

---

## 10. Design Decisions & Tradeoffs

### Compiled Code vs Interpreted Execution

| Approach | Pros | Cons |
|---|---|---|
| **Compiled (Redshift)** | Faster execution, lower CPU per row, compiler optimizations | First-run compilation overhead (100-500 ms), cache invalidation on upgrades |
| **Interpreted (traditional)** | No first-run overhead, simpler implementation | Slower per-row processing, higher CPU utilization |

Redshift chose compilation because analytical queries scan billions of rows — even a small per-row savings compounds enormously. The one-time compilation cost is amortized over millions of rows.

### Leader Node as Single Coordinator

| Aspect | Single Leader | Multi-Leader [INFERRED alternative] |
|---|---|---|
| Simplicity | **Simple** — one coordination point | Complex — consensus required |
| Bottleneck risk | **Yes** — result aggregation, connection limits | Distributed load |
| Consistency | **Trivial** — one source of truth for plans | Must coordinate plans across leaders |
| Max connections | 2,000 (RA3) | Theoretically unlimited |

Redshift chose a single leader because:
1. Analytical queries have moderate concurrency (hundreds, not millions)
2. The leader does lightweight work (planning, aggregation) compared to compute nodes
3. Concurrency scaling adds transient clusters with their own leaders for burst traffic
4. Result caching reduces leader load for repeated queries

### Sequential Streams vs Fully Pipelined Execution

Streams execute sequentially — the leader waits for Stream N to complete before starting Stream N+1. A fully pipelined approach would overlap streams.

**Why sequential?**
- Data redistribution between streams requires all slices to complete their current segment before the next segment can begin (all-to-all shuffle)
- The leader can adapt subsequent streams based on runtime statistics from earlier streams (e.g., detected disk spilling → adjust memory allocation)
- Simpler coordination and error handling

**The cost**: Some potential parallelism is lost. But in practice, most queries have 2-4 streams, and the execution time is dominated by the scan/join/aggregate work within each stream, not the stream transitions.
