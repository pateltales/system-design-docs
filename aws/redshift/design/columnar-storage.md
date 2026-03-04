# Deep Dive: Columnar Storage & Compression in Amazon Redshift

> Companion document to [interview-simulation.md](./interview-simulation.md)

---

## Table of Contents

1. [Why Columnar Storage for Analytics](#1-why-columnar-storage-for-analytics)
2. [Storage Architecture: Blocks, Columns, and Slices](#2-storage-architecture-blocks-columns-and-slices)
3. [Zone Maps: Block-Level Predicate Skipping](#3-zone-maps-block-level-predicate-skipping)
4. [Column Compression Encodings](#4-column-compression-encodings)
5. [Compression + Zone Maps: How They Work Together](#5-compression--zone-maps-how-they-work-together)
6. [RA3 Managed Storage vs DC2 Local Storage](#6-ra3-managed-storage-vs-dc2-local-storage)
7. [Data Loading: COPY Command and Block Formation](#7-data-loading-copy-command-and-block-formation)
8. [VACUUM: Maintaining Storage Health](#8-vacuum-maintaining-storage-health)
9. [Design Decisions & Tradeoffs](#9-design-decisions--tradeoffs)

---

## 1. Why Columnar Storage for Analytics

### The Fundamental Problem with Row Storage

In a row-oriented database (PostgreSQL, MySQL), data is stored as complete rows:

```
Disk Layout (Row-Oriented):
┌──────────────────────────────────────────────────────────────────┐
│ Row 1: [id=1, name='Alice', region='US', amount=100, date='2024-01-01', col6...col50] │
│ Row 2: [id=2, name='Bob',   region='EU', amount=200, date='2024-01-02', col6...col50] │
│ Row 3: [id=3, name='Carol', region='US', amount=150, date='2024-01-03', col6...col50] │
│ ...                                                                                     │
└──────────────────────────────────────────────────────────────────┘
```

For an analytical query:
```sql
SELECT region, SUM(amount) FROM orders WHERE date > '2024-01-01' GROUP BY region;
```

This query needs only 3 columns: `region`, `amount`, `date`. But the row-oriented storage forces the engine to read **all 50 columns** for every row — 47 columns of wasted I/O.

### Column-Oriented Storage

Redshift stores data column-by-column:

```
Disk Layout (Column-Oriented):
┌─────────────────────────────────────┐
│ Column 'id':     [1, 2, 3, 4, ...]  │  ← 1 MB blocks
│ Column 'name':   ['Alice','Bob',...] │  ← 1 MB blocks
│ Column 'region': ['US','EU','US',...] │  ← 1 MB blocks
│ Column 'amount': [100, 200, 150,...] │  ← 1 MB blocks
│ Column 'date':   ['2024-01-01',...]  │  ← 1 MB blocks
│ Column 'col6':   [...]               │  ← 1 MB blocks
│ ...                                   │
│ Column 'col50':  [...]               │  ← 1 MB blocks
└─────────────────────────────────────┘
```

Now the query reads only the `region`, `amount`, and `date` columns — **3 out of 50 columns = 6% of I/O**.

### Quantified Benefits

| Metric | Row-Oriented | Column-Oriented (Redshift) |
|---|---|---|
| Data read for 3-column query on 50-column table | 100% of table data | **6% of table data** |
| Compression ratio | 1.5-2x (mixed types per row) | **3-10x** (same type per column) |
| Records per 1 MB disk block | ~hundreds (wide rows) | **~thousands** (narrow column values) |
| Sequential I/O pattern | Yes (for row scans) | Yes (each column is sequential) |
| Point lookup (single row) | **Fast** (one read gets all columns) | Slow (must read from N column files) |

The compression advantage comes from **data type homogeneity**: a column of integers compresses far better than a row mixing integers, strings, dates, and booleans. Compression algorithms exploit patterns (runs, deltas, dictionary encoding) that only appear when values of the same type are stored together.

---

## 2. Storage Architecture: Blocks, Columns, and Slices

### The 1 MB Block

The fundamental storage unit in Redshift is a **1 MB block**. Each block contains values from **one column** for a contiguous range of rows.

```
Table: orders (50 columns, 1 billion rows)
Distributed across 16 slices (4 nodes × 4 slices/node)

Slice 0 owns ~62.5M rows

For Slice 0:
┌─────────────────────────────────────────────────────────┐
│ Column 'id' (BIGINT, 8 bytes per value)                 │
│                                                         │
│ Block 0: rows 0-124,999      [8 bytes × 125K ≈ 1 MB]  │
│ Block 1: rows 125,000-249,999                           │
│ Block 2: rows 250,000-374,999                           │
│ ...                                                     │
│ Block 499: rows ~62.4M-62.5M                           │
│                                                         │
│ Total: ~500 blocks for this column on this slice        │
├─────────────────────────────────────────────────────────┤
│ Column 'amount' (DECIMAL, compressed)                   │
│                                                         │
│ Block 0: rows 0-~300,000    [compressed → 1 MB]        │
│ Block 1: rows ~300,001-~600,000                         │
│ ...                                                     │
│ (Fewer blocks because compression fits more rows/block) │
├─────────────────────────────────────────────────────────┤
│ Column 'region' (VARCHAR, Byte-Dict compressed)         │
│                                                         │
│ Block 0: rows 0-~1,000,000  [highly compressed]         │
│ ...                                                     │
│ (Very few blocks — low-cardinality column compresses    │
│  extremely well with dictionary encoding)               │
└─────────────────────────────────────────────────────────┘
```

**Key insight**: The number of blocks per column varies dramatically based on the column's data type and compression. A BIGINT column uncompressed stores ~125K values per 1 MB block. A low-cardinality VARCHAR column with dictionary encoding might store 1M+ values per block.

### Rows Per Block

AWS documentation states that columnar storage stores up to **3x more records per data block** compared to row-oriented storage. This is because:

1. Each block contains values from only one column (narrower data)
2. Compression is more effective on homogeneous data
3. More rows per block = better zone map effectiveness (wider coverage per metadata entry)

### Column-to-Slice Mapping

```
┌─────────────────────────────────────────────┐
│ Compute Node 1                               │
│                                               │
│  ┌───────────────────┐ ┌───────────────────┐ │
│  │     Slice 0        │ │     Slice 1        │ │
│  │                     │ │                     │ │
│  │ orders.id     blks  │ │ orders.id     blks  │ │
│  │ orders.name   blks  │ │ orders.name   blks  │ │
│  │ orders.region blks  │ │ orders.region blks  │ │
│  │ orders.amount blks  │ │ orders.amount blks  │ │
│  │ orders.date   blks  │ │ orders.date   blks  │ │
│  │ ... (50 col groups) │ │ ... (50 col groups) │ │
│  │                     │ │                     │ │
│  │ Each slice owns a   │ │ Different rows than │ │
│  │ subset of rows,     │ │ Slice 0 (determined │ │
│  │ ALL columns for     │ │ by distribution key)│ │
│  │ those rows          │ │                     │ │
│  └───────────────────┘ └───────────────────┘ │
└─────────────────────────────────────────────┘
```

Each slice stores **all columns** for its subset of rows. A query that touches 3 columns only reads the 3 column block groups on each slice — the other 47 column groups are untouched.

---

## 3. Zone Maps: Block-Level Predicate Skipping

### What Zone Maps Are

Every 1 MB block has a **zone map** — a small metadata entry recording the **minimum and maximum values** stored in that block. Zone maps are maintained automatically and stored separately from the data blocks.

```
Zone Map for Column 'date' on Slice 0:
┌────────┬──────────────┬──────────────┐
│ Block  │   Min Value   │   Max Value   │
├────────┼──────────────┼──────────────┤
│ Blk 0  │ 2022-01-01   │ 2022-01-31   │
│ Blk 1  │ 2022-02-01   │ 2022-02-28   │
│ Blk 2  │ 2022-03-01   │ 2022-03-31   │
│ ...    │ ...          │ ...          │
│ Blk 23 │ 2023-12-01   │ 2023-12-31   │
│ Blk 24 │ 2024-01-01   │ 2024-01-31   │  ← MATCH
│ Blk 25 │ 2024-02-01   │ 2024-02-28   │
│ ...    │ ...          │ ...          │
└────────┴──────────────┴──────────────┘
```

For a query `WHERE date BETWEEN '2024-01-15' AND '2024-01-20'`:
- Block 24 zone map: min=2024-01-01, max=2024-01-31 → **might contain matches → READ**
- All other blocks: date range doesn't overlap → **SKIP**
- Result: Read 1 block out of 36 → **97% of blocks skipped**

### Zone Map Effectiveness Depends on Sort Order

**Sorted data (sort key = date):**
```
Block 0:  dates [2022-01-01 ... 2022-01-31]  → narrow range → zone map can skip
Block 1:  dates [2022-02-01 ... 2022-02-28]  → narrow range → zone map can skip
...
Block 24: dates [2024-01-01 ... 2024-01-31]  → narrow range → zone map MATCHES
```

**Unsorted data (no sort key on date):**
```
Block 0:  dates [2024-01-05, 2022-06-15, 2023-11-30, ...]  min=2022-06-15, max=2024-01-05
Block 1:  dates [2023-03-22, 2024-06-01, 2022-01-10, ...]  min=2022-01-10, max=2024-06-01
...
Every block spans the full date range → zone map can't skip ANYTHING
```

**Quantified impact:**

| Data State | Blocks Scanned (date filter for 1 month out of 3 years) | I/O Reduction |
|---|---|---|
| Sorted by date | ~1 block out of 36 | **97%** |
| Unsorted | All 36 blocks | **0%** (zone maps useless) |
| Partially sorted (80% sorted, 20% unsorted region) | ~1 sorted block + all unsorted blocks | **80%** for sorted region |

This is why **sort keys** and **VACUUM** are critical — they maximize zone map effectiveness.

### Zone Maps on Non-Sort-Key Columns

Zone maps exist on **every column**, not just sort key columns. However, they're only useful for non-sort-key columns if the data happens to have some natural clustering. For example:
- A `customer_id` column distributed by KEY on `customer_id` — each slice has a narrow range of customer IDs, so zone maps work well
- A `random_string` column — completely random, zone maps can't skip anything

---

## 4. Column Compression Encodings

Redshift supports multiple compression encodings, each optimized for specific data patterns:

### Encoding Types

| Encoding | Best For | How It Works | Compression Ratio |
|---|---|---|---|
| **AZ64** | Numeric, date/time | Amazon's proprietary algorithm. Combines dictionary, run-length, and delta encoding adaptively. Designed to maintain zone map compatibility. | High (recommended default for numeric/date) |
| **LZO** | General purpose, strings | Lempel-Ziv-Oberhumer. Fast compression/decompression with reasonable ratio. | Moderate (3-5x) |
| **ZSTD** | High compression needed | Facebook's Zstandard. Higher compression than LZO but more CPU. | High (5-8x), but slower decompression |
| **Delta** | Sequential/near-sequential values | Stores the difference between consecutive values. Ideal for auto-increment IDs, timestamps. | Very high for sequential data |
| **RunLength** | Low-cardinality sorted columns | Replaces consecutive identical values with (value, count). | Very high when data is sorted + low cardinality |
| **Byte-Dictionary** | Fewer than 256 distinct values | Builds a 256-entry dictionary. Each value stored as a 1-byte index. | High for low-cardinality |
| **Mostly8** | SMALLINT/INT/BIGINT where most values fit in 1 byte | Stores most values in 8 bits; outliers stored separately. | Moderate-High |
| **Mostly16** | INT/BIGINT where most values fit in 2 bytes | Stores most values in 16 bits. | Moderate-High |
| **Mostly32** | BIGINT where most values fit in 4 bytes | Stores most values in 32 bits. | Moderate |
| **Text255** | VARCHAR where most values < 255 chars | Dictionary-based for short strings. | Moderate |
| **Text32k** | VARCHAR where most values < 32K chars | Dictionary-based for longer strings. | Moderate |
| **RAW** | No compression | Stores values uncompressed. | 1x (no compression) |

### Choosing the Right Encoding

```
Decision Flow for Column Encoding:

Is the column a sort key?
├── Yes → AZ64 (or RAW for compound sort key first column in some cases)
│         [INFERRED — sort key + compression interaction not fully documented]
└── No
    ├── Is it numeric or date/time?
    │   ├── Sequential (auto-increment, timestamps)? → Delta or AZ64
    │   ├── General numeric? → AZ64 (recommended default)
    │   └── Mostly small values in a larger type? → Mostly8/16/32
    ├── Is it a string?
    │   ├── Fewer than 256 distinct values? → Byte-Dictionary
    │   ├── Short strings (< 255 chars)? → Text255 or LZO
    │   └── General strings? → LZO or ZSTD
    └── Is it boolean or very low cardinality?
        └── If sorted: RunLength. If unsorted: Byte-Dictionary.
```

### ANALYZE COMPRESSION

Redshift can automatically recommend encodings:

```sql
ANALYZE COMPRESSION orders;
```

Returns recommendations like:
```
Column      | Encoding  | Est. Reduction
------------+-----------+---------------
order_id    | AZ64      | 70%
customer_id | AZ64      | 65%
amount      | AZ64      | 60%
region      | Bytedict  | 90%
order_date  | AZ64      | 75%
status      | Bytedict  | 92%
```

When loading data with `COPY`, setting `COMPUPDATE ON` (or `PRESET` in newer versions) automatically applies compression based on data analysis.

---

## 5. Compression + Zone Maps: How They Work Together

### They Are Complementary, Not Conflicting

```
Without compression or zone maps:
  Query reads: 1,000 blocks × 1 MB = 1,000 MB

With zone maps only (sorted data, 97% skip):
  Query reads: 30 blocks × 1 MB = 30 MB

With compression only (4x compression):
  Query reads: 1,000 blocks × 250 KB = 250 MB

With BOTH (sorted + compressed):
  Query reads: 30 blocks × 250 KB = 7.5 MB  ← 133x reduction!
```

### How It Works Mechanically

1. Query has predicate `WHERE date = '2024-01-15'`
2. Engine reads **zone maps** (uncompressed metadata): check min/max for each block
3. Blocks where max < '2024-01-15' or min > '2024-01-15' → **skipped entirely** (no I/O)
4. Matching blocks: read **compressed** data from disk
5. **Decompress** in memory
6. Apply predicate to actual values
7. Return matching rows

The zone map check (step 2) operates on a small metadata structure — no decompression needed. This is why zone maps work regardless of compression encoding.

### Compression's Impact on Block Count

Compression changes how many rows fit per 1 MB block:

| Column | Uncompressed Size/Row | Encoding | Compressed Size/Row | Rows per 1 MB Block |
|---|---|---|---|---|
| order_id (BIGINT) | 8 bytes | AZ64 | ~2.4 bytes | ~420,000 |
| region (VARCHAR(10)) | ~12 bytes | Byte-Dict | ~1 byte | ~1,000,000 |
| amount (DECIMAL) | 8 bytes | AZ64 | ~3.2 bytes | ~312,000 |
| description (VARCHAR(500)) | ~200 bytes | LZO | ~60 bytes | ~16,600 |

More rows per block means fewer blocks per column, which means:
- Less total I/O
- Each zone map entry covers more rows (wider coverage)
- Fewer zone map entries to check

---

## 6. RA3 Managed Storage vs DC2 Local Storage

### RA3: Compute-Storage Separation

```
┌─────────────────────┐
│    RA3 Node          │
│                      │
│  ┌────────────────┐  │
│  │ Local SSD      │  │  ← Hot data cache (automatic LRU)
│  │ Cache Layer    │  │     Data automatically tiered based
│  │                │  │     on access frequency
│  └───────┬────────┘  │
│          │ Cache miss │
└──────────┼───────────┘
           ▼
┌─────────────────────┐
│ Redshift Managed    │
│ Storage (RMS)       │
│                     │
│ Backed by S3        │
│ 11 9's durability   │
│ All data lives here │
│ Shared by:          │
│  - Main cluster     │
│  - Concurrency      │
│    scaling clusters  │
│  - Data sharing      │
│    consumers         │
└─────────────────────┘
```

**Managed Storage capacity per node:**

| Node Type | Managed Storage per Node |
|---|---|
| ra3.xlplus | 32 TB |
| ra3.4xlarge | 128 TB |
| ra3.16xlarge | 128 TB |

[INFERRED — the exact local SSD cache size per RA3 node type is not clearly documented. AWS states RA3 nodes have "large local SSD-based caches" but specific sizes are managed automatically.]

### DC2: Tightly Coupled Compute + Storage

```
┌─────────────────────┐
│    DC2 Node          │
│                      │
│  ┌────────────────┐  │
│  │ Local NVMe SSD │  │  ← THIS IS the primary storage
│  │ (primary)      │  │     No S3 backing
│  │                │  │     Data replicated to another
│  │ dc2.large:     │  │     node within cluster
│  │   160 GB       │  │
│  │                │  │
│  │ dc2.8xlarge:   │  │
│  │   2.56 TB      │  │
│  └────────────────┘  │
└─────────────────────┘
```

### Decision Matrix: RA3 vs DC2

| Factor | RA3 | DC2 |
|---|---|---|
| Data volume | > 1 TB | < 1 TB |
| Hot data ratio | Some data cold | All data hot |
| Concurrency scaling | Supported | Not supported |
| Data sharing | Supported | Not supported |
| Resize without data migration | Yes (data in S3) | No (must redistribute) |
| Latency for hot data | Same as DC2 (cached locally) | Lowest possible (NVMe) |
| Latency for cold data | S3 fetch (~ms) | N/A (all data local) |
| Cost model | Compute + managed storage | Node-hour (includes storage) |

---

## 7. Data Loading: COPY Command and Block Formation

### COPY: The Preferred Loading Method

```sql
COPY orders
FROM 's3://my-bucket/orders/'
IAM_ROLE 'arn:aws:iam::123456789012:role/RedshiftLoadRole'
FORMAT AS PARQUET;
```

**Why COPY, not INSERT:**
- COPY loads data in **parallel** — each slice reads directly from S3
- COPY applies **compression** during load
- COPY is **bulk-optimized** — minimal per-row overhead
- INSERT is single-row, single-connection, and generates individual block writes

### Block Formation During COPY

```
S3 source files:
┌──────────────┐ ┌──────────────┐ ┌──────────────┐
│ file1.parquet│ │ file2.parquet│ │ file3.parquet│
└──────┬───────┘ └──────┬───────┘ └──────┬───────┘
       │                │                │
       ▼                ▼                ▼
┌─────────────────────────────────────────────────┐
│ COPY distributes rows to slices based on DISTKEY │
│                                                   │
│ Slice 0 receives rows with hash(DISTKEY) = 0     │
│ Slice 1 receives rows with hash(DISTKEY) = 1     │
│ ...                                               │
└────────┬─────────────────────────┬───────────────┘
         │                         │
         ▼                         ▼
  ┌──────────────────┐     ┌──────────────────┐
  │ Slice 0:          │     │ Slice 1:          │
  │ For each column:  │     │ For each column:  │
  │  1. Compress values│     │  1. Compress values│
  │  2. Pack into 1 MB│     │  2. Pack into 1 MB│
  │     blocks         │     │     blocks         │
  │  3. Compute zone   │     │  3. Compute zone   │
  │     map (min/max)  │     │     map (min/max)  │
  │  4. Append to      │     │  4. Append to      │
  │     unsorted region│     │     unsorted region│
  └──────────────────┘     └──────────────────┘
```

**Important**: New data from COPY is placed in an **unsorted region** at the end of the sorted data. The new blocks have their own zone maps, but since the data isn't interleaved with the sorted data, zone maps for the unsorted region may have wide min/max ranges — reducing their effectiveness.

### Optimal COPY Strategy

- **Split input files**: Match the number of S3 files to a multiple of the number of slices. If you have 16 slices, use 16, 32, or 48 files. This ensures even parallel loading.
- **File size**: Each file should be 1 MB to 1 GB after compression (smaller files = more overhead, larger files = less parallelism)
- **Sort before loading**: If data arrives pre-sorted by the sort key, COPY preserves the order in the unsorted region, making subsequent VACUUM faster

---

## 8. VACUUM: Maintaining Storage Health

### Why VACUUM Exists

After multiple COPY and DELETE operations:
1. **Unsorted region grows**: New data appended after sorted data
2. **Deleted rows marked** but not removed (tombstone markers)
3. Zone maps for unsorted blocks have wide min/max ranges
4. Disk space not reclaimed from deleted rows

### VACUUM Variants

| Command | What It Does |
|---|---|
| `VACUUM FULL tablename` | Re-sorts unsorted rows AND reclaims space from deletes |
| `VACUUM SORT ONLY tablename` | Re-sorts only (no space reclamation) |
| `VACUUM DELETE ONLY tablename` | Reclaims space only (no re-sorting) |
| `VACUUM REINDEX tablename` | For interleaved sort keys: recomputes the Z-order interleaving |

### Automatic VACUUM

Redshift runs **automatic VACUUM DELETE** in the background during periods of low activity. It identifies tables with a significant percentage of deleted rows and reclaims space.

Redshift also runs **automatic VACUUM SORT** for tables with unsorted regions, but only when the cluster has available resources.

### When to Run VACUUM Manually

- After a large bulk load that adds a significant unsorted region
- After a large DELETE operation
- When query performance degrades (zone maps becoming less effective)
- When `SVV_TABLE_INFO.unsorted` percentage is high (> 20%)

### VACUUM and Interleaved Sort Keys

VACUUM REINDEX is **significantly more expensive** for interleaved sort keys because it must recompute the Z-order curve for all rows — not just append new rows to the sorted order. This is the primary operational cost of interleaved sort keys.

---

## 9. Design Decisions & Tradeoffs

### Why 1 MB Block Size?

| Block Size | Pros | Cons |
|---|---|---|
| Smaller (64 KB) | More granular zone maps, less wasted I/O per block | More zone map entries (metadata overhead), more disk seeks |
| **1 MB (Redshift choice)** | Good balance of granularity and sequential read efficiency | Some wasted I/O when only a few rows match in a block |
| Larger (16 MB) | Fewer seeks, higher sequential throughput | Zone maps too coarse, too much wasted I/O |

The 1 MB choice optimizes for the analytical access pattern: sequential scans of large data volumes. It's large enough for efficient sequential I/O, small enough for meaningful zone map filtering.

[INFERRED — AWS has not publicly documented the reasoning behind the 1 MB choice, but it aligns with typical columnar storage block sizes (Parquet uses 128 MB row groups, ORC uses 64 MB stripes, both subdivided into column chunks).]

### Why Per-Column Compression (Not Per-Table)?

Each column has its own data type, cardinality, and value distribution. A single compression algorithm applied to the entire table would be suboptimal:
- `order_id` (sequential BIGINT) → Delta encoding is perfect
- `region` (10 distinct values) → Byte-Dictionary is perfect
- `description` (free text) → LZO or ZSTD is best

Per-column encoding lets Redshift apply the optimal algorithm for each column's data characteristics.

### Columnar vs Row-Oriented: When Each Wins

| Access Pattern | Columnar (Redshift) | Row-Oriented (PostgreSQL) |
|---|---|---|
| `SELECT 3 cols FROM 100-col table WHERE ...` | **Reads 3% of data** | Reads 100% of data |
| `SELECT * FROM orders WHERE id = 42` | Must read all column files, reconstruct row | **Single row read** |
| `INSERT INTO orders VALUES (...)` | Must write to all column files | **Single sequential write** |
| Compression | **3-10x** (same-type columns) | 1.5-2x (mixed-type rows) |
| Bulk scan (no predicate) | **Fewer I/O ops** (compressed) | More I/O ops |
| Point lookup | Slow (many random reads) | **Fast (one sequential read)** |

This is why Redshift is designed for OLAP (analytical scans) and NOT for OLTP (point lookups, single-row inserts at high concurrency).

### The Immutability Advantage

Redshift's columnar blocks are effectively **immutable** once written. Updates don't modify blocks in-place — they mark old rows as deleted and write new rows to the unsorted region. This immutability enables:
- Safe concurrent reads (no read-write conflicts at the block level)
- Simple backup strategy (snapshot changed blocks only)
- Efficient caching (blocks don't change, so cache invalidation is simple)
- Clean separation between the sorted region and new data

The cost is the need for VACUUM to reclaim space and re-sort — a classic tradeoff between write amplification and read performance.
