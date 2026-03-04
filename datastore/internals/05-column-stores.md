# Datastore Internals — Part 5: Column Stores Deep Dive

> This document explains column-oriented storage — the engine behind analytics databases like Amazon Redshift, Google BigQuery, and ClickHouse. Column stores flip how data is physically stored on disk, making analytical queries 10-100x faster than row-oriented databases.

---

## Table of Contents

1. [Row-Oriented vs Column-Oriented Storage](#1-row-oriented-vs-column-oriented-storage)
2. [Why Column Stores Are Fast for Analytics](#2-why-column-stores-are-fast-for-analytics)
3. [Column Compression Techniques](#3-column-compression-techniques)
4. [Vectorized Query Execution](#4-vectorized-query-execution)
5. [How Writes Work in Column Stores](#5-how-writes-work-in-column-stores)
6. [Column Stores in Real Databases](#6-column-stores-in-real-databases)
7. [Wide-Column Stores — A Different Beast](#7-wide-column-stores--a-different-beast)
8. [Strengths and Weaknesses Summary](#8-strengths-and-weaknesses-summary)

---

## 1. Row-Oriented vs Column-Oriented Storage

```
Consider a table with 1 billion rows:

  users table:
  ┌────────┬────────┬─────┬─────────┬───────────┐
  │ id     │ name   │ age │ country │ salary    │
  ├────────┼────────┼─────┼─────────┼───────────┤
  │ 1      │ Alice  │ 30  │ US      │ 120000    │
  │ 2      │ Bob    │ 25  │ UK      │ 95000     │
  │ 3      │ Charlie│ 35  │ US      │ 150000    │
  │ 4      │ Diana  │ 28  │ DE      │ 110000    │
  │ ...    │ ...    │ ... │ ...     │ ...       │
  └────────┴────────┴─────┴─────────┴───────────┘


ROW-ORIENTED STORAGE (MySQL, PostgreSQL):

  Stores all columns of one row TOGETHER on disk.
  
  Disk layout:
  [1, Alice, 30, US, 120000] [2, Bob, 25, UK, 95000] [3, Charlie, 35, US, 150000] ...
  ←──────── Row 1 ─────────→ ←──────── Row 2 ────────→ ←──────── Row 3 ──────────→
  
  Great for: SELECT * FROM users WHERE id = 42
  (Read one row = one contiguous disk read = fast!)
  
  Terrible for: SELECT AVG(salary) FROM users WHERE country = 'US'
  (Must read ALL rows, even though we only need 2 columns: salary and country)
  (Reads 5 columns × 1B rows = 5 billion values, but only needs 2 billion)


COLUMN-ORIENTED STORAGE (Redshift, BigQuery):

  Stores all values of one column TOGETHER on disk.
  
  Disk layout:
  File "id":      [1, 2, 3, 4, ...]              ← all IDs together
  File "name":    [Alice, Bob, Charlie, Diana, ...]  ← all names together
  File "age":     [30, 25, 35, 28, ...]           ← all ages together
  File "country": [US, UK, US, DE, ...]           ← all countries together
  File "salary":  [120000, 95000, 150000, 110000, ...]  ← all salaries together
  
  For: SELECT AVG(salary) FROM users WHERE country = 'US'
  → Only read "country" file + "salary" file (2 files, not all 5)
  → Skip "id", "name", "age" entirely!
  → Read 2B values instead of 5B → 2.5x less data from disk
  → AND each file is highly compressible (see section 3)
```

### Visual: How Data Lives on Disk

```
ROW STORAGE:
┌──────────────────────────────────────────────────────────────┐
│ Disk Block 1: [1,Alice,30,US,120K] [2,Bob,25,UK,95K]       │
│ Disk Block 2: [3,Charlie,35,US,150K] [4,Diana,28,DE,110K]  │
│ Disk Block 3: [5,Eve,32,FR,130K] [6,Frank,29,US,125K]      │
│ ...                                                          │
└──────────────────────────────────────────────────────────────┘

To get all salaries: read EVERY block (most data is wasted)

  Block 1: read 100 bytes, need 8 bytes (salary) → 92% wasted
  Block 2: read 100 bytes, need 8 bytes → 92% wasted
  × 1 billion rows = reads ~100 GB, uses ~8 GB. 92 GB wasted!


COLUMN STORAGE:
┌──────────────────────────────────────────────────────────────┐
│ File "salary":                                                │
│ Disk Block 1: [120000, 95000, 150000, 110000, 130000, ...]  │
│ Disk Block 2: [125000, 98000, 145000, 115000, 135000, ...]  │
│ ...                                                          │
└──────────────────────────────────────────────────────────────┘

To get all salaries: read ONLY the salary file
  
  Block 1: read 100 bytes, use 100 bytes → 0% wasted!
  × 1 billion rows = reads ~8 GB, uses ~8 GB. 0 GB wasted!
  
  That's 12x less disk I/O for this query!
  With compression (see below), it's even better: maybe 2 GB total.
```

---

## 2. Why Column Stores Are Fast for Analytics

```
THREE REASONS COLUMN STORES DESTROY ROW STORES FOR ANALYTICS:


REASON 1: READ ONLY WHAT YOU NEED

  Analytics query: SELECT AVG(salary) FROM users WHERE country = 'US'
  Table has 50 columns. Query touches 2 columns.
  
  Row store:  Read all 50 columns for every row  → 50x too much data
  Column store: Read only "country" and "salary"  → exactly what's needed
  
  Speedup: 25x less data read from disk


REASON 2: COMPRESSION IS WAY BETTER

  Column data is HOMOGENEOUS (all same type, similar values).
  
  Column "country": [US, UK, US, DE, US, FR, US, UK, US, US, ...]
  → Only ~200 unique values worldwide
  → Dictionary encoding: US=0, UK=1, DE=2, FR=3, ...
  → Stored as: [0, 1, 0, 2, 0, 3, 0, 1, 0, 0, ...]
  → Each value: 1 byte instead of 2-20 bytes
  → Compression ratio: 10-20x!
  
  Column "age": [30, 25, 35, 28, 32, 29, 31, ...]
  → Values are all between 0-120
  → Each value needs only 7 bits (not 32 bits for an int)
  → Bit-packing: 4-5x compression
  
  Row data: [1, "Alice Johnson", 30, "US", 120000]
  → Mixed types, varying lengths, hard to compress
  → Maybe 1.5-2x compression at best
  
  Result: Column store reads 10-20x LESS data from disk per query.


REASON 3: SEQUENTIAL I/O AND CPU CACHE EFFICIENCY

  Column file for "salary": [120000, 95000, 150000, 110000, ...]
  → All values are contiguous in memory
  → CPU can load them into L1/L2 cache efficiently
  → Modern CPU: process ~1 billion integers/sec from cache
  
  Row data: [...Alice..., 120000, ...Bob..., 95000, ...]
  → Salary values scattered among other columns
  → CPU cache constantly evicted by irrelevant data
  → Much slower processing


COMBINED SPEEDUP:

  For a query like "SELECT AVG(salary) FROM users WHERE country = 'US'"
  on 1 billion rows:

  Row store (PostgreSQL):
    Read: 50 GB (all columns, all rows)
    Decompress: minimal
    Process: ~30 seconds
  
  Column store (Redshift):
    Read: 2 GB (2 columns, compressed)
    Decompress: fast (simple integer decompression)
    Process: ~0.5 seconds
  
  60x faster! This is typical for analytics workloads.
```

---

## 3. Column Compression Techniques

```
Column stores use several compression techniques. These work well
because column data is homogeneous (same type, similar patterns).


TECHNIQUE 1: DICTIONARY ENCODING

  Original:  ["United States", "United Kingdom", "United States", "Germany", ...]
  Dictionary: {"United States": 0, "United Kingdom": 1, "Germany": 2, ...}
  Encoded:    [0, 1, 0, 2, ...]
  
  Space savings: 14 bytes → 1 byte per value = 14x compression
  Works best for: LOW CARDINALITY columns (few unique values)
  Example: country, status, category, gender


TECHNIQUE 2: RUN-LENGTH ENCODING (RLE)

  If data is SORTED by this column:
  Original: [US, US, US, US, US, UK, UK, UK, DE, DE, ...]
  Encoded:  [(US, 5), (UK, 3), (DE, 2), ...]
  
  5 million "US" rows → stored as (US, 5000000) = 1 entry!
  Works best for: sorted columns with many repeated values
  

TECHNIQUE 3: BIT-PACKING

  Column "age": values range 0-120
  Normal int: 32 bits per value
  Bit-packed: 7 bits per value (2^7 = 128, enough for 0-120)
  
  Compression: 32/7 = 4.6x
  Works best for: numeric columns with limited range


TECHNIQUE 4: DELTA ENCODING

  Column "timestamp" (sorted): 
    [1688000000, 1688000001, 1688000003, 1688000007, ...]
  Store as deltas from previous value:
    [1688000000, +1, +2, +4, ...]
  
  Deltas are small → can be bit-packed into very few bits
  Works best for: sorted numeric columns (timestamps, sequential IDs)


TECHNIQUE 5: NULL COMPRESSION (Bitmap)

  Column "middle_name": [NULL, NULL, "James", NULL, NULL, NULL, "Marie", ...]
  90% of values are NULL.
  
  Store a bitmap: [0, 0, 1, 0, 0, 0, 1, ...]  (1 bit per row!)
  Plus the non-null values separately: ["James", "Marie", ...]
  
  For 90% NULL column: 10x compression
  

TYPICAL COMPRESSION RATIOS:

  ┌────────────────────┬──────────────────┬──────────────┐
  │ Column type         │ Technique         │ Ratio        │
  ├────────────────────┼──────────────────┼──────────────┤
  │ Country/Status      │ Dictionary        │ 10-20x       │
  │ Sorted country      │ Dictionary + RLE  │ 100-1000x    │
  │ Age/Score           │ Bit-packing       │ 3-5x         │
  │ Timestamps (sorted) │ Delta + bit-pack  │ 10-20x       │
  │ Sparse/NULL columns │ Bitmap            │ 5-50x        │
  │                     │                    │              │
  │ Overall average     │ Mixed             │ 5-15x        │
  └────────────────────┴──────────────────┴──────────────┘
  
  A 100 GB dataset (row-oriented) might be 7-20 GB in a column store!
```

---

## 4. Vectorized Query Execution

```
Column stores don't just store data differently — they PROCESS it differently.

Traditional (row-at-a-time) execution:
  for each row:
    if row.country == "US":
      sum += row.salary
      count += 1
  
  Each iteration: fetch row from memory, extract fields, branch prediction
  Poor CPU cache utilization (jumping between columns in memory)

Vectorized (column-at-a-time) execution:
  // Step 1: Filter on country column (process 1000 values at once)
  mask = country_column[0:1000] == "US"   // SIMD instruction!
  
  // Step 2: Apply mask to salary column
  filtered_salaries = salary_column[0:1000] WHERE mask
  
  // Step 3: Sum the filtered salaries
  sum += SUM(filtered_salaries)           // SIMD instruction!
  
  Process 1000 values per CPU instruction using SIMD!
  (SIMD = Single Instruction, Multiple Data)
  
  Speedup: 5-10x faster than row-at-a-time

This is how ClickHouse processes billions of rows per second on a single node.
```

---

## 5. How Writes Work in Column Stores

```
Column stores are SLOW for writes. Here's why:

INSERT a new row: (id=5, name="Eve", age=32, country="FR", salary=130000)

Row store: Append the complete row to ONE file → 1 write
Column store: Write to 5 SEPARATE files:
  id file:      append 5
  name file:    append "Eve"
  age file:     append 32
  country file: append "FR"
  salary file:  append 130000
  → 5 writes, one per column!

For a table with 200 columns: 200 writes per INSERT. TERRIBLE!


SOLUTION: BUFFER WRITES, BATCH FLUSH

  Column stores use a strategy similar to LSM trees:
  
  1. Buffer writes in memory (row-oriented, for speed)
  2. When buffer fills up → convert to columnar format → flush to disk
  3. Background merge process combines small columnar files into large ones
  
  This is how Redshift, BigQuery, and Vertica handle writes.
  
  
  ┌────────────────────────────────────────────────────────┐
  │  Write Buffer (in memory, row-oriented):                │
  │  [5, Eve, 32, FR, 130000]                              │
  │  [6, Frank, 29, US, 125000]                            │
  │  [7, Grace, 31, UK, 140000]                            │
  │  ... (buffer ~1000-10000 rows)                         │
  │                                                        │
  │  When full → convert to columns and flush:             │
  │  id.col:      [5, 6, 7, ...]                           │
  │  name.col:    [Eve, Frank, Grace, ...]                 │
  │  age.col:     [32, 29, 31, ...]                        │
  │  country.col: [FR, US, UK, ...]                        │
  │  salary.col:  [130000, 125000, 140000, ...]            │
  └────────────────────────────────────────────────────────┘


THIS IS WHY COLUMN STORES ARE USED FOR ANALYTICS, NOT OLTP:

  OLTP (Online Transaction Processing):
    → Small, frequent writes (1 row at a time)
    → Column store: SLOW (must write to N column files)
    → Row store: FAST (append 1 row to 1 file)
  
  OLAP (Online Analytical Processing):
    → Bulk loads (millions of rows at once)
    → Column store: FAST (batch convert, sequential writes)
    → Reads: scan columns → column store is 10-100x faster
```

---

## 6. Column Stores in Real Databases

```
┌─────────────────────┬──────────────────────────────────────────────────┐
│ Database             │ Notes                                             │
├─────────────────────┼──────────────────────────────────────────────────┤
│ Amazon Redshift      │ Cloud data warehouse. Column-oriented.            │
│                     │ Based on ParAccel (PostgreSQL fork).              │
│                     │ Stores data in 1 MB blocks per column.           │
│                     │ Zone maps: min/max per block for pruning.        │
│                     │                                                    │
│ Google BigQuery      │ Serverless column store. Uses Dremel engine.     │
│                     │ Stores data in Capacitor columnar format.        │
│                     │ Massively parallel: scans PBs in seconds.        │
│                     │                                                    │
│ ClickHouse          │ Open-source column store by Yandex.              │
│                     │ Blazing fast: billions of rows/sec on 1 node.    │
│                     │ MergeTree engine (LSM-like + columnar).          │
│                     │                                                    │
│ Apache Parquet      │ Columnar FILE FORMAT (not a database).           │
│                     │ Used by Spark, Hive, Presto, Athena.             │
│                     │ De facto standard for data lake storage.         │
│                     │                                                    │
│ Apache ORC          │ Another columnar file format. Used by Hive.      │
│                     │ Similar to Parquet with different trade-offs.    │
│                     │                                                    │
│ Snowflake           │ Cloud data warehouse. Columnar + micro-partitions│
│                     │ Separates compute from storage.                  │
│                     │                                                    │
│ DuckDB              │ In-process columnar analytics DB.                │
│                     │ \"SQLite for analytics.\" Single binary, no server.│
│                     │ Surprisingly fast for local analytics.           │
│                     │                                                    │
│ Vertica             │ Enterprise column store. Projections (sorted     │
│                     │ copies of data in different column orders).      │
└─────────────────────┴──────────────────────────────────────────────────┘
```

---

## 7. Wide-Column Stores — A Different Beast

```
IMPORTANT: "Wide-column stores" (Cassandra, HBase) are NOT the same
as "column-oriented stores" (Redshift, BigQuery). Don't confuse them!

COLUMN-ORIENTED STORE (Redshift, BigQuery):
  → Stores each COLUMN in a separate file
  → Optimized for analytics: "scan all values of column X"
  → OLAP workload

WIDE-COLUMN STORE (Cassandra, HBase):
  → Stores data in ROWS, but each row can have DIFFERENT columns
  → Think of it as a 2D hash map: row_key → {column1: val1, column2: val2}
  → Each row can have millions of columns (hence "wide")
  → Data for a single row is stored together (not split by column!)
  → Optimized for: "give me all columns for row X" (point lookups)
  → Uses LSM tree internally (not columnar storage)

Example — Cassandra wide column:
  Row "user:42": {name: "Alice", age: 30, email: "alice@example.com"}
  Row "user:43": {name: "Bob", country: "UK"}  ← different columns!
  Row "user:44": {name: "Charlie", age: 25, phone: "555-1234", 
                  address: "123 Main St", ...}  ← many columns!

  Each row's data is stored TOGETHER on disk (row-oriented within a partition).
  NOT stored as separate column files.

  Use cases: time-series (wide rows for timestamp ranges),
  IoT data, messaging (row per conversation, column per message).

TL;DR:
  "Column store" (Redshift) = data stored BY COLUMN → analytics
  "Wide-column store" (Cassandra) = flexible schema, many columns per row → operational
```

---

## 8. Strengths and Weaknesses Summary

```
COLUMN STORE STRENGTHS:
═══════════════════════
  ✅ 10-100x faster for analytical queries (scan subset of columns)
  ✅ Excellent compression (5-15x) — homogeneous data compresses well
  ✅ Vectorized execution — process 1000s of values per CPU instruction
  ✅ Massively parallelizable — each column chunk is independent
  ✅ Great for data warehousing and reporting

COLUMN STORE WEAKNESSES:
════════════════════════
  ❌ Slow single-row writes (must write to N column files)
  ❌ Slow point lookups ("give me row 42" → read from N files)
  ❌ Not suitable for OLTP (transactions, frequent small writes)
  ❌ More complex architecture than row stores

WHEN TO USE:
  → Data warehousing and analytics
  → Reporting dashboards (aggregations, GROUP BY)
  → Large-scale data scanning (batch processing)
  → Data lakes (Parquet format)

WHEN NOT TO USE:
  → OLTP (web app backends, user-facing queries)
  → Frequent small writes/updates
  → Point lookups by primary key
  → Need ACID transactions
  → In these cases: use row-oriented (B-tree) or LSM-tree


DECISION MATRIX:

  "My queries scan millions of rows and touch 2-5 columns"
    → COLUMN STORE (Redshift, BigQuery, ClickHouse)
  
  "My queries fetch 1 row by ID and read all columns"
    → ROW STORE (PostgreSQL, MySQL)
  
  "My workload is write-heavy with point lookups"
    → LSM-TREE (Cassandra, RocksDB)
  
  "My data fits in RAM and I need sub-millisecond latency"
    → HASH INDEX (Redis)
```

---

*Previous: [← Hash Index Deep Dive](04-hash-index.md) | Next: [Comparison & When to Use What →](06-comparison-and-when-to-use-what.md)*