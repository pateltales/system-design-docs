# Datastore Internals — Part 1: Fundamentals

> Before we dive into B-trees, LSM trees, or any specific storage engine, we need to understand the **fundamental problem** every database is trying to solve. This document starts from absolute zero.

---

## Table of Contents

1. [Why Not Just Use a HashMap in Memory?](#1-why-not-just-use-a-hashmap-in-memory)
2. [The Crash Problem — Why We Need Disk](#2-the-crash-problem--why-we-need-disk)
3. [How Disks Actually Work](#3-how-disks-actually-work)
4. [Sequential vs Random I/O — The Most Important Concept](#4-sequential-vs-random-io--the-most-important-concept)
5. [The Fundamental Tension in All Databases](#5-the-fundamental-tension-in-all-databases)
6. [The Write-Ahead Log (WAL) — Every Database's Safety Net](#6-the-write-ahead-log-wal--every-databases-safety-net)
7. [The Four Storage Engine Families](#7-the-four-storage-engine-families)
8. [Roadmap: Where We Go From Here](#8-roadmap-where-we-go-from-here)

---

## 1. Why Not Just Use a HashMap in Memory?

Let's start with the simplest possible "database":

```
// The world's simplest key-value store
HashMap<String, String> database = new HashMap<>();

database.put("user:123", "Alice");
database.put("user:456", "Bob");

String name = database.get("user:123");  // "Alice" — instant!
```

This works! It's fast (O(1) lookups), simple, and requires zero libraries.

**So why does anyone bother with MySQL, PostgreSQL, MongoDB, Redis, Cassandra, or any other database?**

### Problem 1: The Machine Crashes

```
Your program:
  1. database.put("user:789", "Charlie")   ← stored in RAM
  2. database.put("user:101", "Diana")     ← stored in RAM
  3. ⚡ POWER FAILURE ⚡
  4. Machine reboots...
  5. database.get("user:789")  →  NULL!  😱

Everything in RAM is gone. Forever.
RAM = volatile memory. When power goes away, data goes away.
```

### Problem 2: Data Doesn't Fit in Memory

```
Your data: 50 TB (50,000 GB)
Your RAM:  64 GB

You can fit 0.13% of your data in memory.
The other 99.87% needs to live somewhere else.

That somewhere = disk (HDD or SSD).
```

### Problem 3: Multiple Programs Need the Same Data

```
Without a database:
  Program A reads user:123 from its HashMap  → "Alice"
  Program B reads user:123 from its HashMap  → ??? (doesn't have it)
  
  Program A updates user:123 to "Alice Smith"
  Program B still has the old version (or nothing)

A database provides a shared, consistent view of data for all programs.
```

**Bottom line: We need to store data on DISK so it survives crashes, handles large datasets, and is shared across programs. That's why databases exist.**

---

## 2. The Crash Problem — Why We Need Disk

### RAM vs Disk: The Key Difference

```
┌────────────────────────────────────────────────────────┐
│                                                        │
│  RAM (Random Access Memory)                            │
│  ──────────────────────────                            │
│  ✅ Blazing fast: ~100 nanoseconds per access          │
│  ✅ Random access: read any location equally fast       │
│  ❌ VOLATILE: data disappears when power is lost       │
│  ❌ Expensive: ~$5-10 per GB                           │
│  ❌ Limited: typical server has 64-256 GB              │
│                                                        │
│  Think of it as: a whiteboard. Fast to write and read, │
│  but everything is erased when you leave the room.     │
│                                                        │
├────────────────────────────────────────────────────────┤
│                                                        │
│  Disk (SSD or HDD)                                     │
│  ─────────────────                                     │
│  ⚠️ Slower: ~0.1ms (SSD) to ~10ms (HDD) per access    │
│  ⚠️ Sequential access is MUCH faster than random       │
│  ✅ DURABLE: data survives power loss                  │
│  ✅ Cheap: ~$0.10-0.50 per GB (SSD)                   │
│  ✅ Large: can have terabytes per machine              │
│                                                        │
│  Think of it as: a notebook. Slower to flip through,   │
│  but everything stays even after you close it.         │
│                                                        │
└────────────────────────────────────────────────────────┘
```

### Speed Comparison (with real numbers)

```
Operation                           Time            Relative Speed
─────────────────────────────────────────────────────────────────
Read from CPU cache (L1)            ~1 ns           1x (baseline)
Read from RAM                       ~100 ns         100x slower
Read from SSD (random)              ~100,000 ns     100,000x slower
Read from SSD (sequential)          ~10,000 ns      10,000x slower
Read from HDD (random)              ~10,000,000 ns  10,000,000x slower
Read from HDD (sequential)          ~1,000,000 ns   1,000,000x slower
Network round-trip (same DC)        ~500,000 ns     500,000x slower

Key insight: 
  RAM is 1,000x faster than SSD for random reads.
  SSD is 10x faster for sequential reads than random reads.
  HDD is 100x faster for sequential reads than random reads.

This speed gap is THE driving force behind all storage engine design.
```

### The Analogy

```
Imagine you're a librarian:

RAM = The books on your desk right now
  → You can read any of them instantly (reach over and open it)
  → But you only have room for ~10 books on your desk
  → If there's a fire alarm and everyone evacuates, 
    you lose track of which books were on your desk

Disk = The bookshelves in the library
  → You have room for millions of books
  → But to read one, you have to:
    1. Walk to the right shelf (seek time)
    2. Find the right book (scan time)
    3. Walk back to your desk (transfer time)
  → The books stay on the shelves even after a fire alarm

Every database is essentially a system for organizing books
on the shelves (disk) so you can find them quickly,
while keeping the most frequently used ones on your desk (RAM).
```

---

## 3. How Disks Actually Work

### HDD (Hard Disk Drive) — The Spinning Platter

```
You don't NEED to know this for interviews, but it helps
understand WHY sequential I/O matters.

┌─────────────────────────────────────────┐
│         HDD: Spinning Platter           │
│                                         │
│         ┌───────────────┐               │
│        /    ○ ○ ○ ○      \              │
│       / ○              ○  \             │
│      / ○    ┌──────┐    ○  \            │
│     │ ○     │      │     ○  │           │
│     │ ○     │ SPIN │←ARM ○  │           │
│     │ ○     │      │     ○  │           │
│      \ ○    └──────┘    ○  /            │
│       \ ○              ○  /             │
│        \    ○ ○ ○ ○      /              │
│         └───────────────┘               │
│                                         │
│  The platter SPINS at 7,200-15,000 RPM  │
│  The arm MOVES to the right track       │
│  Then WAITS for the right sector to     │
│  spin under it                          │
│                                         │
│  Random read:                           │
│  1. Move arm to track (seek): ~5ms      │
│  2. Wait for sector (rotation): ~4ms    │
│  3. Read data (transfer): ~0.1ms        │
│  Total: ~9ms per random read            │
│  = ~110 random reads per second         │
│                                         │
│  Sequential read:                       │
│  1. Arm is already positioned           │
│  2. Sectors come one after another      │
│  3. Read data: ~0.01ms per sector       │
│  = ~100-200 MB/sec throughput           │
│                                         │
│  Sequential is 100x+ faster than random!│
│                                         │
└─────────────────────────────────────────┘
```

### SSD (Solid State Drive) — Flash Memory

```
┌─────────────────────────────────────────┐
│         SSD: Flash Memory Chips         │
│                                         │
│  ┌─────┐ ┌─────┐ ┌─────┐ ┌─────┐     │
│  │CHIP │ │CHIP │ │CHIP │ │CHIP │      │
│  │  1  │ │  2  │ │  3  │ │  4  │      │
│  └─────┘ └─────┘ └─────┘ └─────┘      │
│  ┌─────┐ ┌─────┐ ┌─────┐ ┌─────┐     │
│  │CHIP │ │CHIP │ │CHIP │ │CHIP │      │
│  │  5  │ │  6  │ │  7  │ │  8  │      │
│  └─────┘ └─────┘ └─────┘ └─────┘      │
│                                         │
│  No moving parts! Pure electronics.     │
│                                         │
│  Random read:  ~0.1ms (100 μs)         │
│  = ~10,000 random reads per second      │
│                                         │
│  Sequential read: ~500 MB/sec - 3 GB/sec│
│                                         │
│  Random is 100x faster than HDD!        │
│  But sequential is STILL 5-10x faster   │
│  than random, even on SSD.              │
│                                         │
│  Why? Because SSDs read in "pages"      │
│  (4KB-16KB). Reading sequentially means │
│  each page read is useful. Random reads │
│  may only need 100 bytes from a 4KB     │
│  page — wasting 97.5% of each read.    │
│                                         │
└─────────────────────────────────────────┘
```

---

## 4. Sequential vs Random I/O — The Most Important Concept

**This is THE most important concept in all of database storage design.** Every design decision in every storage engine traces back to this.

```
Sequential I/O:
  Reading/writing data that is stored NEXT TO EACH OTHER on disk.
  Like reading a book page by page, front to back.

Random I/O:
  Reading/writing data that is SCATTERED across the disk.
  Like reading page 5, then page 2000, then page 42, then page 999.
```

### Why Sequential Is So Much Faster

```
┌──────────────────────────────────────────────────────────────┐
│                                                              │
│  SEQUENTIAL I/O:                                             │
│  ┌────┬────┬────┬────┬────┬────┬────┬────┐                 │
│  │ D1 │ D2 │ D3 │ D4 │ D5 │ D6 │ D7 │ D8 │  ← contiguous │
│  └────┴────┴────┴────┴────┴────┴────┴────┘                 │
│  Read all 8: ONE operation, reads them in a single sweep     │
│  Time: ~0.1ms total                                          │
│                                                              │
│  RANDOM I/O:                                                 │
│  ┌────┐    ┌────┐         ┌────┐  ┌────┐                   │
│  │ D1 │....│ D2 │.........│ D3 │..│ D4 │   ← scattered    │
│  └────┘    └────┘         └────┘  └────┘                   │
│  Read all 4: FOUR separate operations, each needs a "seek"   │
│  Time: 4 × 0.1ms = 0.4ms (SSD)                              │
│  Time: 4 × 10ms  = 40ms  (HDD)                              │
│                                                              │
│  Summary:                                                    │
│  HDD: Sequential is 100-1000x faster than random             │
│  SSD: Sequential is 5-10x faster than random                 │
│                                                              │
└──────────────────────────────────────────────────────────────┘
```

### Real-World Impact

```
Scenario: Write 1 million key-value pairs to disk.

Approach A: Write each one to a sorted position on disk (RANDOM I/O)
  Each write: find the right position → move data → insert
  HDD: ~10ms per write × 1M = 10,000 seconds = 2.8 hours
  SSD: ~0.1ms per write × 1M = 100 seconds

Approach B: Append each one to the end of a file (SEQUENTIAL I/O)
  Each write: just add it to the end
  HDD: ~0.001ms per write × 1M = 1 second
  SSD: ~0.0001ms per write × 1M = 0.1 seconds

Approach A (random) on HDD: 2.8 HOURS
Approach B (sequential) on HDD: 1 SECOND

That's a 10,000x difference!

This is why databases care so much about I/O patterns.
```

---

## 5. The Fundamental Tension in All Databases

Here's the core insight that explains why there are different types of storage engines:

```
┌──────────────────────────────────────────────────────────────────┐
│                                                                  │
│  THE FUNDAMENTAL TENSION:                                        │
│                                                                  │
│  READS want data to be ORGANIZED                                │
│  (sorted, indexed, easy to find)                                │
│                                                                  │
│  WRITES want to just APPEND                                     │
│  (sequential, fast, no reorganization)                          │
│                                                                  │
│  You can't optimize for BOTH perfectly.                         │
│  Every storage engine makes a TRADEOFF.                         │
│                                                                  │
└──────────────────────────────────────────────────────────────────┘
```

### The Library Analogy

```
Imagine you're running a library with 1 million books.

STRATEGY A: Keep books perfectly sorted on shelves (alphabetical)
  ─────────────────────────────────────────────────────────
  Finding a book:  FAST! Go to the right section, scan shelf.
  Adding a new book: SLOW! You need to:
    1. Find the right position alphabetically
    2. SHIFT all the books after it to make room
    3. Insert the book
  If you get 100 new books per minute, you'll never keep up.
  
  This is like a B-TREE → great for reads, expensive for writes.
  Used by: MySQL, PostgreSQL, SQLite


STRATEGY B: Throw new books on a pile by the door
  ─────────────────────────────────────────────────────────
  Adding a book: FAST! Just toss it on the pile.
  Finding a book: SLOW! You have to search through the pile.
  
  Better version: throw books on the pile, but periodically
  sort the pile and merge it into the shelves.
  
  This is like an LSM-TREE → great for writes, reads need more work.
  Used by: Cassandra, RocksDB, LevelDB, HBase


STRATEGY C: Use a card catalog (index cards → shelf locations)
  ─────────────────────────────────────────────────────────
  Finding a book: FAST! Look up the card → go to shelf location.
  Adding a book: Medium. Put book anywhere + add a card.
  Problem: All the cards must fit in a card catalog (= RAM).
  
  This is like a HASH INDEX → fastest reads, but limited by RAM.
  Used by: Redis, Memcached


STRATEGY D: Group books by genre/topic instead of alphabetically
  ─────────────────────────────────────────────────────────
  "Give me all Science Fiction books": FAST! They're all together.
  "Give me book 'Dune' specifically": Slower. Must scan the genre.
  
  Great for: "Give me all data in column X" (analytics queries)
  Bad for: "Give me row 12345" (point lookups)
  
  This is like a COLUMN STORE → great for analytics, not for OLTP.
  Used by: Redshift, BigQuery, ClickHouse
```

### The Tradeoff Spectrum

```
                WRITE SPEED                              READ SPEED
                (how fast can I store data?)              (how fast can I find data?)
                
  ◀─────────────────────────────────────────────────────────────────▶
  
  Append-only        LSM Tree         B-Tree           Hash Index
  log                                                  (in-memory)
  
  ┌──────────┐    ┌──────────┐    ┌──────────┐     ┌──────────┐
  │ Writes:  │    │ Writes:  │    │ Writes:  │     │ Writes:  │
  │ FASTEST  │    │ FAST     │    │ MEDIUM   │     │ FAST     │
  │          │    │          │    │          │     │ (in RAM) │
  │ Reads:   │    │ Reads:   │    │ Reads:   │     │ Reads:   │
  │ SLOWEST  │    │ GOOD     │    │ FAST     │     │ FASTEST  │
  │ (scan    │    │ (bloom   │    │ (tree    │     │ (O(1)    │
  │  entire  │    │  filters │    │  lookup) │     │  lookup) │
  │  file)   │    │  help)   │    │          │     │          │
  └──────────┘    └──────────┘    └──────────┘     └──────────┘
  
  No real DB        Cassandra        MySQL            Redis
  uses this         RocksDB          PostgreSQL       Memcached
  alone             HBase            SQLite
                    LevelDB          MongoDB
```

---

## 6. The Write-Ahead Log (WAL) — Every Database's Safety Net

Before we get into specific storage engines, there's ONE concept that EVERY database uses: the Write-Ahead Log.

### The Problem WAL Solves

```
Scenario: You're updating a B-tree (like MySQL does).

  Step 1: Read page 42 from disk into memory          ✓
  Step 2: Modify the data in memory                    ✓
  Step 3: Write the modified page 42 back to disk      ← IN PROGRESS
                                                        ⚡ CRASH!

What happened? 
  - Maybe page 42 was partially written (half old, half new)
  - The data on disk is now CORRUPTED
  - We can't tell what the correct state is

This is called a "torn write" — the nightmare scenario for databases.
```

### The WAL Solution

```
Rule: BEFORE you modify any data on disk, FIRST write a log of 
what you're ABOUT to do to a separate file (the WAL).

Step 1: Write to WAL: "I'm going to change page 42, 
        key 'user:123' from 'Alice' to 'Alice Smith'"    ← SEQUENTIAL WRITE
Step 2: fsync the WAL (force it to disk)                  ← DURABLE
Step 3: Now modify page 42 in the actual data file        
Step 4: If crash at step 3 → on restart, REPLAY the WAL
        → Re-apply the change to page 42 → data is correct!

Why this works:
  - Writing to WAL is SEQUENTIAL (append to end of file) → FAST
  - The WAL is a simple, linear file → easy to replay, hard to corrupt
  - If the crash happens during WAL write → change never happened (that's OK)
  - If the crash happens during data write → WAL has the change, replay it
```

### Visual Walkthrough

```
Normal operation (no crash):

  ┌──────────────────────────────────────────────────────────┐
  │                     WAL FILE                              │
  │  [Change 1: set K1=V1] [Change 2: set K2=V2] [Change 3] │
  └──────────────────────────────────────────────────────────┘
                    ↓ periodically applied to ↓
  ┌──────────────────────────────────────────────────────────┐
  │                   ACTUAL DATA FILE                        │
  │  (B-tree pages / SSTable / whatever the engine uses)      │
  │  K1=V1, K2=V2, ...                                       │
  └──────────────────────────────────────────────────────────┘
  
  Once changes are safely in the data file → delete old WAL entries
  (this is called "checkpointing")


Crash recovery:

  1. On startup, database checks: "Is there a WAL file?"
  2. If yes → "Let me replay any changes that weren't applied to the data file"
  3. Replay each WAL entry → data file is now consistent
  4. Resume normal operation
  
  Time to recover: typically < 1 second (WAL is small)
```

### WAL in Different Databases

```
┌──────────────────┬──────────────────────────────────────────┐
│ Database          │ WAL implementation                       │
├──────────────────┼──────────────────────────────────────────┤
│ PostgreSQL       │ WAL (literally called "WAL")              │
│ MySQL (InnoDB)   │ Redo log                                  │
│ SQLite           │ WAL mode or Journal mode                  │
│ MongoDB          │ Journal                                   │
│ Cassandra        │ Commit log                                │
│ LSM-tree engines │ WAL (writes go to WAL + memtable)         │
│ Redis            │ AOF (Append Only File) — optional         │
│                  │                                            │
│ EVERY serious    │ Some form of WAL. It's universal.         │
│ database         │                                            │
└──────────────────┴──────────────────────────────────────────┘

Fun fact: The term "write-AHEAD" means you write the log AHEAD of
(before) the actual data change. Log first, data second. Always.
```

---

## 7. The Four Storage Engine Families

Now that we understand the fundamentals, let's preview the four main approaches to storage:

```
┌─────────────────────────────────────────────────────────────────────┐
│                                                                     │
│  Family 1: B-TREE (Read-Optimized, In-Place Updates)               │
│  ──────────────────────────────────────────────                    │
│  Idea: Keep data in a sorted tree structure on disk.               │
│        When you write, find the right spot and update in place.    │
│                                                                     │
│  Write: Find the right page → update it → write page back          │
│  Read:  Walk the tree → find the page → read the value             │
│                                                                     │
│  Pros: Fast reads (tree traversal = O(log n))                      │
│  Cons: Random I/O on writes (each write touches a random page)     │
│                                                                     │
│  Used by: MySQL, PostgreSQL, SQLite, MongoDB (WiredTiger),         │
│           SQL Server, Oracle                                        │
│                                                                     │
│  Best for: READ-HEAVY workloads, transactions, SQL databases       │
│                                                                     │
│  → Deep dive in 02-btree-deep-dive.md                              │
│                                                                     │
├─────────────────────────────────────────────────────────────────────┤
│                                                                     │
│  Family 2: LSM-TREE (Write-Optimized, Append-Only)                 │
│  ──────────────────────────────────────────────                    │
│  Idea: Never update data in place. Always append.                  │
│        Writes go to memory first, then flush to sorted files.      │
│        Background process merges the files (compaction).           │
│                                                                     │
│  Write: Append to WAL + insert into in-memory buffer               │
│  Read:  Check memory → check on-disk files (with bloom filters)    │
│                                                                     │
│  Pros: Very fast writes (all sequential I/O)                       │
│  Cons: Reads may check multiple files; background compaction I/O   │
│                                                                     │
│  Used by: Cassandra, RocksDB, LevelDB, HBase, CockroachDB,       │
│           ScyllaDB, InfluxDB                                        │
│                                                                     │
│  Best for: WRITE-HEAVY workloads, time-series, key-value stores   │
│                                                                     │
│  → Deep dive in 03-lsm-tree-deep-dive.md                          │
│                                                                     │
├─────────────────────────────────────────────────────────────────────┤
│                                                                     │
│  Family 3: HASH INDEX (In-Memory, O(1) Lookups)                    │
│  ──────────────────────────────────────────────                    │
│  Idea: Keep a hash table in RAM. Optionally persist to disk.       │
│                                                                     │
│  Write: Hash the key → store in memory (+ optional disk persist)   │
│  Read:  Hash the key → O(1) lookup in memory                       │
│                                                                     │
│  Pros: Fastest possible reads and writes (everything in RAM)       │
│  Cons: Data must fit in RAM (expensive at scale)                   │
│        No range queries (hash destroys order)                      │
│                                                                     │
│  Used by: Redis, Memcached, Riak (Bitcask)                        │
│                                                                     │
│  Best for: Caching, session stores, real-time leaderboards         │
│                                                                     │
│  → Deep dive in 04-hash-index.md                                   │
│                                                                     │
├─────────────────────────────────────────────────────────────────────┤
│                                                                     │
│  Family 4: COLUMN STORE (Analytics-Optimized)                      │
│  ──────────────────────────────────────────────                    │
│  Idea: Store data by COLUMN instead of by ROW.                     │
│        All values for column "age" are stored together.            │
│                                                                     │
│  Write: Split row into columns → append each column separately     │
│  Read:  To read 1 column → read 1 contiguous file (sequential!)   │
│                                                                     │
│  Pros: 10-100x faster for "scan column X" analytics queries        │
│        Excellent compression (similar values stored together)      │
│  Cons: Slow for point lookups ("give me all of row 12345")         │
│        Slow for transactional writes                               │
│                                                                     │
│  Used by: Amazon Redshift, Google BigQuery, ClickHouse,            │
│           Apache Parquet, Apache Cassandra (wide-column variant)   │
│                                                                     │
│  Best for: Data warehouses, analytics, reporting                   │
│                                                                     │
│  → Deep dive in 05-column-stores.md                                │
│                                                                     │
└─────────────────────────────────────────────────────────────────────┘
```

### Quick Decision Framework

```
"What storage engine should my system use?"

  Is your data small enough to fit in RAM?
    YES → Hash Index (Redis, Memcached)
    NO  ↓

  Is your workload mostly reads or writes?
    MOSTLY READS  → B-Tree (MySQL, PostgreSQL)
    MOSTLY WRITES → LSM-Tree (Cassandra, RocksDB)
    BALANCED      → Either works; LSM for scale, B-Tree for consistency
    
  Is your workload analytical (aggregations, scans)?
    YES → Column Store (Redshift, BigQuery, ClickHouse)
    NO  → Row-oriented (B-Tree or LSM)

  Do you need transactions (ACID)?
    YES → B-Tree based SQL database (PostgreSQL, MySQL)
    NO  → LSM-Tree based NoSQL (Cassandra, DynamoDB)
    
  Do you need range queries ("all users aged 20-30")?
    YES → B-Tree (sorted, range scans are efficient)
    NO  → LSM-Tree or Hash (point lookups only is fine)
```

---

## 8. Roadmap: Where We Go From Here

```
You are here: ★

  ★  01-fundamentals.md (THIS FILE)
  │   - Why databases exist
  │   - Disk vs RAM
  │   - Sequential vs Random I/O
  │   - The fundamental tension
  │   - Write-Ahead Log
  │
  ├── 02-btree-deep-dive.md
  │    Build from sorted array → BST → B-tree
  │    Step-by-step INSERT, SEARCH, DELETE
  │    Why MySQL/PostgreSQL use this
  │
  ├── 03-lsm-tree-deep-dive.md
  │    Memtable → WAL → SSTable → Compaction
  │    Bloom filters from scratch
  │    Why Cassandra/RocksDB use this
  │
  ├── 04-hash-index.md
  │    Hash tables, Redis internals
  │    Bitcask model, AOF persistence
  │    When to use in-memory stores
  │
  ├── 05-column-stores.md
  │    Row vs column storage
  │    Compression, vectorized queries
  │    Why Redshift/BigQuery are fast for analytics
  │
  ├── 06-comparison-and-when-to-use-what.md
  │    Side-by-side comparison
  │    Decision framework for system design interviews
  │
  └── 07-indexing.md
       Primary, secondary, composite indexes
       Inverted indexes (full-text search)
       Geospatial indexes
```

---

### Key Takeaways From This Document

```
1. Databases exist because RAM is volatile and limited.
   We MUST store data on disk for durability and scale.

2. Disk is 1,000-10,000x slower than RAM.
   Sequential disk I/O is 10-1000x faster than random I/O.
   → Storage engines are designed around this reality.

3. The fundamental tension: reads want organized data,
   writes want to just append. You can't optimize both perfectly.

4. Every database uses a Write-Ahead Log (WAL) for crash recovery.
   Write the LOG first, then the actual data. Universal concept.

5. Four main storage engine families:
   - B-Tree:     Read-optimized, in-place updates (MySQL, PostgreSQL)
   - LSM-Tree:   Write-optimized, append-only (Cassandra, RocksDB)
   - Hash Index:  In-memory, O(1) lookups (Redis)
   - Column Store: Analytics-optimized (Redshift, BigQuery)
```

---

*Next up: [B-Tree Deep Dive →](02-btree-deep-dive.md)*