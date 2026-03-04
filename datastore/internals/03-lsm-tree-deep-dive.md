# Datastore Internals — Part 3: LSM-Tree Deep Dive

> This document explains LSM trees (Log-Structured Merge Trees) from scratch. LSM trees are the storage engine behind Cassandra, RocksDB, LevelDB, HBase, and many modern databases. They flip the B-tree tradeoff: **writes are blazing fast, reads need a bit more work.**

---

## Table of Contents

1. [The Problem LSM Trees Solve](#1-the-problem-lsm-trees-solve)
2. [The Core Idea: "Never Update in Place"](#2-the-core-idea-never-update-in-place)
3. [Building an LSM Tree Step by Step](#3-building-an-lsm-tree-step-by-step)
4. [Component 1: The Memtable (In-Memory Write Buffer)](#4-component-1-the-memtable-in-memory-write-buffer)
5. [Component 2: The Write-Ahead Log (WAL)](#5-component-2-the-write-ahead-log-wal)
6. [Component 3: SSTables (Sorted String Tables)](#6-component-3-sstables-sorted-string-tables)
7. [How WRITES Work — Step by Step](#7-how-writes-work--step-by-step)
8. [How READS Work — Step by Step](#8-how-reads-work--step-by-step)
9. [Bloom Filters — Explained from Scratch](#9-bloom-filters--explained-from-scratch)
10. [Compaction — Keeping Things Manageable](#10-compaction--keeping-things-manageable)
11. [How DELETES Work — Tombstones](#11-how-deletes-work--tombstones)
12. [Write Amplification, Read Amplification, Space Amplification](#12-write-amplification-read-amplification-space-amplification)
13. [LSM Trees in Real Databases](#13-lsm-trees-in-real-databases)
14. [Strengths and Weaknesses Summary](#14-strengths-and-weaknesses-summary)

---

## 1. The Problem LSM Trees Solve

Remember from the B-tree chapter:

```
B-tree writes are RANDOM I/O:
  Each write finds a specific page on disk → reads it → modifies it → writes it back.
  On HDD: ~10ms per write = ~100 writes/sec
  On SSD: ~0.1ms per write = ~10,000 writes/sec

For many workloads, this isn't fast enough:
  - Time-series data: sensors sending 100K readings/sec
  - Logging: applications generating 50K log entries/sec
  - Message queues: processing 200K messages/sec
  - Social media: 100K posts/likes/comments per second
```

**LSM trees convert ALL writes to SEQUENTIAL I/O.**

```
LSM tree writes:
  Each write appends to a file (sequential) or writes to memory.
  On HDD: ~100,000 writes/sec (sequential append is fast!)
  On SSD: ~500,000+ writes/sec

That's 100-1000x faster than B-tree writes on HDD!
```

---

## 2. The Core Idea: "Never Update in Place"

```
B-TREE PHILOSOPHY:
  "When I update a value, I go find where it lives and change it right there."
  
  Like erasing a word in a notebook and writing the new word in the same spot.
  You need to find the right page first (random I/O).


LSM-TREE PHILOSOPHY:
  "When I update a value, I just write the new version at the END.
   I'll sort it out later."
  
  Like writing notes on sticky notes and tossing them in a pile.
  Later, you organize the pile (compaction).
  The writing itself is FAST because you just append.


Why this works:
  - Appending to the end of a file = SEQUENTIAL I/O = fast
  - No need to find and read the old page first
  - Multiple writes to the same key → each is a new entry,
    the latest one wins (the older versions are ignored)
```

---

## 3. Building an LSM Tree Step by Step

Let's build an LSM tree from scratch, solving problems one at a time:

```
ATTEMPT 1: Just append everything to a file
─────────────────────────────────────────────

Write "user:1 = Alice":  append to file
Write "user:2 = Bob":    append to file
Write "user:1 = Alicia": append to file  (update!)
Write "user:3 = Charlie": append to file

File contents (in order of writes):
  [user:1=Alice] [user:2=Bob] [user:1=Alicia] [user:3=Charlie]

READ "user:1":
  Scan the ENTIRE file from end to beginning...
  Find "user:1=Alicia" (the newest entry) → return "Alicia"
  
Problem: Reading requires scanning the whole file → O(n). SLOW!


ATTEMPT 2: Keep writes sorted in memory, flush periodically
─────────────────────────────────────────────────────────────

Keep a sorted data structure in memory (a "memtable").
When it gets big, sort it and write to disk as a sorted file.

Memory (memtable):        Disk (sorted files):
  user:1 = Alicia          File 1: [user:1=Alice, user:2=Bob]
  user:3 = Charlie         
  user:4 = Diana           

Now reads can use binary search on disk files → O(log n)!

Problem: What if the machine crashes? Memory is lost!


ATTEMPT 3: Add a Write-Ahead Log (WAL)
────────────────────────────────────────

Before writing to memory, first append to a log file on disk.
If crash → replay the log to rebuild memory.

Write path:
  1. Append to WAL (sequential disk write — fast!)
  2. Insert into memtable (memory — instant!)

Problem: After many flushes, we have lots of small sorted files.
Reading must check ALL of them → slow.


ATTEMPT 4: Merge sorted files (compaction)
───────────────────────────────────────────

Periodically merge small sorted files into bigger sorted files.
This reduces the number of files reads must check.

THIS IS THE FULL LSM TREE!

  Write: WAL + memtable (fast)
  Read:  Check memtable → check sorted files (with bloom filters)
  Background: Merge files (compaction)
```

---

## 4. Component 1: The Memtable (In-Memory Write Buffer)

```
WHAT IS IT?
  An in-memory sorted data structure that buffers writes before
  they're flushed to disk.

DATA STRUCTURE: Skip List (usually)

  What's a skip list? A probabilistic sorted data structure.
  Think of it as a sorted linked list with "express lanes":

  Level 3:  HEAD ──────────────────────────────── 50 ──── NIL
  Level 2:  HEAD ────────── 20 ──────────── 50 ──── NIL
  Level 1:  HEAD ── 10 ── 20 ── 30 ── 40 ── 50 ── 60 ── NIL
  Level 0:  HEAD ── 10 ── 20 ── 30 ── 40 ── 50 ── 60 ── NIL

  To find 40:
    Start at top level → HEAD → 50 (too far) → drop down
    Level 2: HEAD → 20 → 50 (too far) → drop down  
    Level 1: 20 → 30 → 40 → FOUND!

  Insert: O(log n) average
  Search: O(log n) average  
  Sorted iteration: O(n) — just scan level 0

  Why skip list over red-black tree?
  - Skip list supports CONCURRENT reads without locks
  - Multiple threads can read while one thread writes
  - Simpler to implement
  - Used by LevelDB, RocksDB, Cassandra


HOW IT WORKS:

  Every write goes to the memtable:

  PUT("user:3", "Charlie")  →  memtable: {user:3: "Charlie"}
  PUT("user:1", "Alice")    →  memtable: {user:1: "Alice", user:3: "Charlie"}
  PUT("user:2", "Bob")      →  memtable: {user:1: "Alice", user:2: "Bob", user:3: "Charlie"}

  Data is automatically sorted by the skip list!


SIZE LIMIT:
  
  Memtable has a size limit (e.g., 64 MB).
  When it fills up:
    1. Current memtable becomes "immutable" (read-only)
    2. A NEW empty memtable is created for new writes
    3. Background thread FLUSHES the immutable memtable to disk as an SSTable
    4. After flush completes, the immutable memtable is discarded

  ┌────────────────────────────────────────────────────────┐
  │  Timeline:                                              │
  │                                                        │
  │  T=0:    Memtable (active, receiving writes)           │
  │  T=100:  Memtable is full (64 MB)                      │
  │          → Becomes immutable (no more writes here)     │
  │          → New memtable created (writes go here now)   │
  │  T=101:  Background flush starts                       │
  │          [Immutable memtable] ──flush──▶ [SSTable on disk] │
  │  T=102:  Flush complete. Delete old WAL. Free memory.  │
  │                                                        │
  │  During T=100-102:                                     │
  │  - New writes → new active memtable ✓                  │
  │  - Reads → check new memtable, then immutable, then disk │
  │  - No downtime! System keeps running during flush.     │
  │                                                        │
  └────────────────────────────────────────────────────────┘
```

---

## 5. Component 2: The Write-Ahead Log (WAL)

```
(We covered WAL in detail in 01-fundamentals.md, so this is brief.)

PURPOSE: Prevent data loss if the machine crashes before the memtable
         is flushed to disk.

EVERY write does TWO things:
  1. Append to WAL on disk (sequential write — fast, ~microseconds)
  2. Insert into memtable in memory

If the machine crashes:
  - Memtable (in RAM) is lost
  - But WAL (on disk) survives!
  - On restart: replay the WAL → rebuild the memtable
  - No data lost ✓

LIFECYCLE:
  1. New WAL created when a new memtable starts
  2. Writes append to WAL
  3. When memtable is flushed to SSTable → old WAL is deleted
     (the SSTable now has the durable copy of the data)

COST: Every write does one extra sequential disk write (the WAL append).
  This is very cheap — sequential writes are ~1000x faster than random writes.
  Typically adds < 0.01ms per write.
```

---

## 6. Component 3: SSTables (Sorted String Tables)

```
WHAT IS AN SSTABLE?
  A file on disk containing key-value pairs SORTED BY KEY.
  "SSTable" stands for "Sorted String Table."
  
  It's the on-disk representation of a flushed memtable.


WHAT DOES AN SSTABLE FILE LOOK LIKE?

  ┌────────────────────────────────────────────────────────┐
  │  SSTable File                                           │
  │                                                        │
  │  ┌──────────────────────────────────────────────────┐  │
  │  │  DATA SECTION (sorted key-value pairs)           │  │
  │  │                                                  │  │
  │  │  Block 1: [age:20=Alice, age:21=Zara, ...]      │  │
  │  │  Block 2: [age:30=Charlie, age:31=Diana, ...]   │  │
  │  │  Block 3: [age:40=Eve, age:41=Frank, ...]       │  │
  │  │  ...                                             │  │
  │  │                                                  │  │
  │  │  Each block is ~4 KB, compressed                │  │
  │  │  Within each block: entries sorted by key        │  │
  │  └──────────────────────────────────────────────────┘  │
  │                                                        │
  │  ┌──────────────────────────────────────────────────┐  │
  │  │  INDEX SECTION                                   │  │
  │  │                                                  │  │
  │  │  "age:20" → offset 0                            │  │
  │  │  "age:30" → offset 4096                         │  │
  │  │  "age:40" → offset 8192                         │  │
  │  │  ...                                             │  │
  │  │                                                  │  │
  │  │  Maps the first key of each block → file offset  │  │
  │  │  Used for binary search to find the right block  │  │
  │  └──────────────────────────────────────────────────┘  │
  │                                                        │
  │  ┌──────────────────────────────────────────────────┐  │
  │  │  BLOOM FILTER                                    │  │
  │  │  (explained in detail in section 9)              │  │
  │  │  Quickly answers: "Is key K in this SSTable?"    │  │
  │  └──────────────────────────────────────────────────┘  │
  │                                                        │
  │  ┌──────────────────────────────────────────────────┐  │
  │  │  FOOTER: offsets to index and bloom filter        │  │
  │  └──────────────────────────────────────────────────┘  │
  │                                                        │
  └────────────────────────────────────────────────────────┘


KEY PROPERTY: SSTables are IMMUTABLE
  Once written, an SSTable is NEVER modified.
  Updates and deletes create NEW SSTables (with newer versions).
  Old versions are cleaned up during compaction.

WHY IMMUTABLE?
  1. No read-write conflicts — readers never fight with writers
  2. Simple crash recovery — a file is either complete or not
  3. Easy caching — the file never changes, so caches are always valid
  4. Enables efficient compaction (merge-sort of sorted files)
```

---

## 7. How WRITES Work — Step by Step

```
Client calls: PUT("user:42", "Alice")

Step 1: APPEND to WAL
  ┌─────────────────────────────────────────┐
  │ WAL file (append-only, on disk):         │
  │ ... [prev entries] [PUT user:42=Alice]  │
  └─────────────────────────────────────────┘
  Time: ~0.01 ms (sequential disk write)

Step 2: INSERT into Memtable
  ┌─────────────────────────────────────────┐
  │ Memtable (in memory, sorted):            │
  │ { ..., user:41=Frank, user:42=Alice,     │
  │   user:43=Bob, ... }                     │
  └─────────────────────────────────────────┘
  Time: ~0.001 ms (memory operation)

Step 3: RETURN success to client
  Total write latency: ~0.01 ms
  (Compare to B-tree: ~0.1-1 ms due to random I/O)

DONE! That's it. The write is complete.
No disk page lookup. No random I/O. Just append + memory insert.


WHEN THE MEMTABLE IS FULL (64 MB):

Step 4: FREEZE the memtable (make it immutable)
Step 5: Create a new empty memtable (new writes go here)
Step 6: FLUSH the frozen memtable to disk as a new SSTable
  
  Frozen memtable (sorted entries):
    user:1=Alice, user:2=Bob, user:3=Charlie, ...
    
  Write to disk as SSTable:
    [data blocks] [index] [bloom filter] [footer]
  
  This is a sequential write of ~64 MB → takes ~0.1 seconds
  
Step 7: DELETE the old WAL (SSTable now has the durable data)
Step 8: FREE the memory used by the frozen memtable
```

---

## 8. How READS Work — Step by Step

Reads are more complex in LSM trees because data might be in multiple places:

```
Client calls: GET("user:42")

The data could be in:
  1. The active memtable (newest data, in memory)
  2. The immutable memtable (being flushed, in memory)
  3. SSTable files on disk (flushed data, possibly multiple files)

We check from NEWEST to OLDEST and return the first match.


Step 1: CHECK ACTIVE MEMTABLE (in memory)
  ┌─────────────────────────────────────────┐
  │ Memtable: search for "user:42"           │
  │ Skip list lookup: O(log n)               │
  │                                          │
  │ Found?                                   │
  │   YES → return value immediately! DONE.  │
  │   NO  → continue to step 2              │
  └─────────────────────────────────────────┘
  Time: ~0.001 ms


Step 2: CHECK IMMUTABLE MEMTABLE (if one exists, in memory)
  ┌─────────────────────────────────────────┐
  │ Same as step 1 but for the old memtable  │
  │ Found? YES → return. NO → continue.     │
  └─────────────────────────────────────────┘
  Time: ~0.001 ms


Step 3: CHECK SSTABLES ON DISK (newest to oldest)
  
  We may have multiple SSTables:
    SSTable-5 (newest)
    SSTable-4
    SSTable-3
    SSTable-2
    SSTable-1 (oldest)
  
  For each SSTable (newest first):
  
    a. Check BLOOM FILTER (in memory)
       "Is user:42 POSSIBLY in this SSTable?"
       ├── NO (definite)  → SKIP this SSTable entirely! 
       └── MAYBE (yes)    → continue to step b
       Time: ~0.001 ms
    
    b. Check INDEX (in memory)
       Binary search the index to find which data block
       might contain "user:42"
       Time: ~0.001 ms
    
    c. READ the data block from disk
       Read ~4 KB from the identified position
       Time: ~0.1 ms (SSD)
    
    d. Search within the data block for "user:42"
       Time: ~0.001 ms
    
    Found? YES → return value. DONE.
    NOT FOUND → try the next (older) SSTable


TYPICAL PERFORMANCE:

  Key EXISTS and is in memtable:      ~0.001 ms (memory only!)
  Key EXISTS and is in newest SSTable: ~0.1 ms (1 disk read)
  Key EXISTS and is in 3rd SSTable:    ~0.3 ms (3 disk reads)
  Key DOES NOT EXIST:                  ~0.005 ms (all bloom filters say "no")

  The bloom filter is the hero for non-existent keys!
  Without it: must check EVERY SSTable → many disk reads.
  With it: all bloom filters say "definitely not here" → zero disk reads.
```

### Why Reads Are Slower Than B-Trees

```
B-tree read:  1-2 disk reads (always, regardless of history)
LSM read:     0-5 disk reads (depends on how many SSTables exist)

If there are 10 SSTables and the key is in the oldest one:
  → Check 9 bloom filters (fast, in memory)
  → Read 1 data block from SSTable-1
  → Total: ~0.1 ms + small bloom filter overhead

But if bloom filters have false positives:
  → Might read 2-3 data blocks unnecessarily
  → Total: ~0.2-0.3 ms

Compaction helps by reducing the number of SSTables.
A well-compacted LSM tree has similar read performance to a B-tree.
```

---

## 9. Bloom Filters — Explained from Scratch

Bloom filters are THE key optimization that makes LSM-tree reads fast. Let's understand them from zero.

### The Problem

```
We have 10 SSTables on disk. A client asks: GET("user:42")

Without bloom filters:
  Must read from EACH SSTable to check if "user:42" is there.
  10 SSTables × 1 disk read each = 10 disk reads = ~1ms
  
  Even if "user:42" is in the FIRST SSTable, we still
  checked 10 SSTables (we check newest to oldest, so we
  stop at the first match, but what if it's in the oldest?)

WITH bloom filters:
  Before reading from disk, ask the in-memory bloom filter:
  "Is user:42 in this SSTable?"
  
  SSTable-10 bloom filter: "Definitely NOT here" → SKIP
  SSTable-9 bloom filter:  "Definitely NOT here" → SKIP
  SSTable-8 bloom filter:  "Definitely NOT here" → SKIP
  SSTable-7 bloom filter:  "MAYBE here" → read from disk → found!
  
  Only 1 disk read instead of 10!
```

### What IS a Bloom Filter?

```
A bloom filter is a space-efficient probabilistic data structure that 
answers ONE question: "Is element X in set S?"

Possible answers:
  "DEFINITELY NOT in the set" → 100% accurate, trust it
  "PROBABLY in the set"       → might be wrong (false positive)

It can NEVER say "yes" when the answer is "no."
It CAN say "probably yes" when the answer is actually "no" (false positive).

False positive rate: typically ~1% (configurable)
  → 99% of the time, when it says "probably yes", the key IS there
  → 1% of the time, we do an unnecessary disk read (acceptable cost)
```

### How a Bloom Filter Works — Visual Walkthrough

```
A bloom filter is just a BIT ARRAY (a bunch of 0s and 1s) + some hash functions.

Example: 20-bit array with 3 hash functions

SETUP:
  Bit array:  [0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0]
  Position:    0  1  2  3  4  5  6  7  8  9 10 11 12 13 14 15 16 17 18 19

  Hash functions:
    h1(key) = some_hash(key) mod 20 → gives a position 0-19
    h2(key) = another_hash(key) mod 20
    h3(key) = third_hash(key) mod 20


ADDING "user:42" to the bloom filter:
  h1("user:42") = 3
  h2("user:42") = 11
  h3("user:42") = 17
  
  Set bits at positions 3, 11, 17 to 1:
  
  [0, 0, 0, 1, 0, 0, 0, 0, 0, 0, 0, 1, 0, 0, 0, 0, 0, 1, 0, 0]
             ↑                       ↑                    ↑
            pos 3                  pos 11               pos 17


ADDING "user:99" to the bloom filter:
  h1("user:99") = 7
  h2("user:99") = 3    ← same as user:42's h1! That's OK.
  h3("user:99") = 14
  
  Set bits at positions 7, 3, 14 to 1:
  
  [0, 0, 0, 1, 0, 0, 0, 1, 0, 0, 0, 1, 0, 0, 1, 0, 0, 1, 0, 0]
             ↑           ↑              ↑        ↑        ↑
            3 (already   7             11       14       17
             set)


CHECKING "Is user:42 in the set?"
  h1("user:42") = 3  → bit[3]  = 1 ✓
  h2("user:42") = 11 → bit[11] = 1 ✓
  h3("user:42") = 17 → bit[17] = 1 ✓
  
  ALL bits are 1 → "PROBABLY YES" ✓ (and it IS in the set)


CHECKING "Is user:77 in the set?" (NOT in the set)
  h1("user:77") = 5  → bit[5]  = 0 ✗
  
  At least one bit is 0 → "DEFINITELY NOT" ✓ 
  (We can stop at the first 0 — don't even need to check h2 and h3)


CHECKING "Is user:55 in the set?" (NOT in the set)
  h1("user:55") = 3  → bit[3]  = 1 ✓  (set by user:42 and user:99)
  h2("user:55") = 7  → bit[7]  = 1 ✓  (set by user:99)
  h3("user:55") = 14 → bit[14] = 1 ✓  (set by user:99)
  
  ALL bits are 1 → "PROBABLY YES" 
  
  BUT user:55 is NOT in the set! This is a FALSE POSITIVE.
  The bits happen to be set by OTHER keys.
  
  We'll do an unnecessary disk read and find nothing.
  This happens ~1% of the time. An acceptable cost.
```

### Bloom Filter Sizing

```
To achieve a 1% false positive rate:
  Need ~10 bits per key and 7 hash functions
  
  For an SSTable with 1 million keys:
    10 bits × 1M = 10 Mbit = 1.25 MB
    
  That's tiny! 1.25 MB of RAM to avoid millions of unnecessary disk reads.

For an entire database with 10 billion keys across 100 SSTables:
  ~100M keys per SSTable × 10 bits = 125 MB per SSTable
  100 SSTables × 125 MB = 12.5 GB total bloom filter memory
  
  On a machine with 64 GB RAM, this is ~20% of memory.
  Totally worth it for the I/O savings.
```

---

## 10. Compaction — Keeping Things Manageable

### The Problem Without Compaction

```
Every time the memtable fills up, a NEW SSTable is created on disk.
Over time, SSTables accumulate:

After 1 hour:   10 SSTables
After 1 day:    240 SSTables
After 1 week:   1,680 SSTables

Reading becomes slow:
  - Must check bloom filter for each SSTable
  - False positives add up: 1% × 1,680 = ~17 unnecessary disk reads per query!
  - Multiple versions of the same key across SSTables waste space

Also wastes disk space:
  - Old versions of updated keys still exist in old SSTables
  - Deleted keys (tombstones) still take up space
```

### What Is Compaction?

```
Compaction = merging multiple SSTables into fewer, larger SSTables.

It's like merge sort:
  - Take N sorted files
  - Merge them into 1 sorted file
  - During the merge: discard old versions, apply tombstones

BEFORE compaction:
  SSTable-1: [user:1=Alice, user:3=Charlie]
  SSTable-2: [user:1=Alicia, user:2=Bob]        ← user:1 updated!
  SSTable-3: [user:2=TOMBSTONE, user:4=Diana]    ← user:2 deleted!

AFTER compaction (merge all 3):
  SSTable-new: [user:1=Alicia, user:3=Charlie, user:4=Diana]
  
  What happened:
  - user:1: "Alice" (old) replaced by "Alicia" (newer) ✓
  - user:2: "Bob" (old) deleted by TOMBSTONE ✓
  - user:3: "Charlie" kept as-is ✓
  - user:4: "Diana" kept as-is ✓
  
  Delete old SSTables (1, 2, 3). Keep only SSTable-new.
```

### Strategy 1: Size-Tiered Compaction

```
Idea: When you have N SSTables of similar size, merge them into 1 bigger SSTable.

BEFORE:
  [16MB] [16MB] [16MB] [16MB]  ← 4 SSTables of ~16MB each
  
  When 4 accumulate → merge into 1:
  
AFTER:
  [64MB]  ← 1 SSTable of 64MB

Then when you get 4 × 64MB:
  [64MB] [64MB] [64MB] [64MB] → merge into [256MB]

And so on: 256MB → 1GB → 4GB → ...

Visual:
  Level 0 (16MB each):   [S1] [S2] [S3] [S4] → compact → Level 1
  Level 1 (64MB each):   [M1] [M2] [M3] [M4] → compact → Level 2
  Level 2 (256MB each):  [L1] [L2] [L3] [L4] → compact → Level 3
  Level 3 (1GB each):    [XL1]

Pros: Simple. Low write amplification (each byte is written ~4-5 times total).
Cons: May have overlapping key ranges at the same level → slower reads.
      Space amplification: during compaction, both old and new files exist.

Used by: Cassandra (default), HBase
```

### Strategy 2: Leveled Compaction

```
Idea: Each level has a SIZE LIMIT. SSTables within a level are NON-OVERLAPPING.

  Level 0: memtable flushes land here (may overlap)
  Level 1: total size ≤ 100 MB, SSTables don't overlap
  Level 2: total size ≤ 1 GB, SSTables don't overlap  
  Level 3: total size ≤ 10 GB, SSTables don't overlap

When Level 1 exceeds its limit:
  1. Pick 1 SSTable from Level 1
  2. Find all overlapping SSTables in Level 2
  3. Merge them → produce new non-overlapping SSTables in Level 2
  4. Delete old files

KEY BENEFIT: At each level (L1+), SSTables are non-overlapping!
  → A point lookup only needs to check AT MOST 1 SSTable per level
  → Much better read performance

Pros: Better read performance. Lower space amplification (~10%).
Cons: Higher write amplification (~10-30x). More background I/O.

Used by: LevelDB, RocksDB (default)
```

### Choosing Between Them

```
                    Size-Tiered           Leveled
─────────────────────────────────────────────────────────
Write speed         ✅ Faster             ❌ Slower (more merging)
Read speed          ❌ Slower             ✅ Faster (non-overlapping)
Space usage         ❌ Higher (2x temp)   ✅ Lower (10% overhead)
Write amplification ✅ Lower (4-5x)       ❌ Higher (10-30x)
Best for            Write-heavy           Balanced/read-heavy
```

---

## 11. How DELETES Work — Tombstones

```
PROBLEM: SSTables are IMMUTABLE. You can't go into an SSTable 
and remove a key-value pair. So how do you delete?

ANSWER: Write a special marker called a TOMBSTONE.

  Client calls: DELETE("user:42")
  
  Step 1: Append to WAL: [DELETE user:42]
  Step 2: Insert into memtable: {user:42: TOMBSTONE}
  
  A tombstone says: "This key is deleted. Ignore any older values."

  Later, when reading:
    Memtable: user:42 = TOMBSTONE
    SSTable-3: user:42 = "Alice"
    
    Memtable is newer → tombstone wins → return "NOT FOUND"

  During compaction:
    When the compaction process encounters a key with a tombstone
    AND the tombstone is old enough (past the "grace period"):
    → Discard both the tombstone and all older values
    → Key is truly gone from disk ✓

WHY A GRACE PERIOD?
  In a distributed system, other replicas might still have the old value.
  If we remove the tombstone too quickly, the old value could
  "come back" via read repair or anti-entropy sync.
  The grace period (e.g., 10 days) gives enough time for the
  tombstone to propagate to all replicas.
```

---

## 12. Write Amplification, Read Amplification, Space Amplification

These are the three costs of LSM trees. Understanding them helps you compare with B-trees.

```
WRITE AMPLIFICATION
  How many times is each byte of data written to disk?
  
  User writes 1 KB of data. What actually gets written?
    1. WAL append: 1 KB                        (1st write)
    2. Memtable flush to SSTable: 1 KB          (2nd write)
    3. Level 0 → Level 1 compaction: 1 KB       (3rd write)
    4. Level 1 → Level 2 compaction: 1 KB       (4th write)
    5. Level 2 → Level 3 compaction: 1 KB       (5th write)
    ...
  
  Leveled compaction: ~10-30x write amplification
  Size-tiered compaction: ~4-10x write amplification
  B-tree: ~5-10x write amplification
  
  BUT: All LSM writes are SEQUENTIAL. All B-tree writes are RANDOM.
  Sequential write at 30x amplification can still be faster than
  random write at 5x amplification!


READ AMPLIFICATION
  How many disk reads for a single point lookup?
  
  Best case (key in memtable):     0 disk reads
  Typical (key in recent SSTable): 1 disk read
  Worst case (key in oldest level): 1 per level = ~5-7 disk reads
  Key doesn't exist:                0 disk reads (bloom filters!)
  
  B-tree: always 1-3 disk reads (predictable)
  LSM: 0-7 disk reads (variable, usually 0-2)


SPACE AMPLIFICATION
  How much disk space does the data use beyond the actual data size?
  
  Leveled compaction: ~10% extra (non-overlapping levels, minimal waste)
  Size-tiered compaction: up to 2x during compaction (old + new files)
  B-tree: ~30-50% extra (page fragmentation, partially filled pages)
  
  LSM with leveled compaction is actually BETTER than B-tree for space!


SUMMARY:
┌────────────────────┬────────────────┬────────────────┐
│                    │    LSM-Tree    │    B-Tree      │
├────────────────────┼────────────────┼────────────────┤
│ Write amplification│ 10-30x         │ 5-10x          │
│ Write I/O type     │ SEQUENTIAL ✅  │ RANDOM ❌      │
│ Read amplification │ 0-7 reads      │ 1-3 reads      │
│ Space amplification│ 10% (leveled)  │ 30-50%         │
│ Write throughput   │ ✅ Very high    │ ❌ Lower       │
│ Read latency       │ ⚠️ Variable    │ ✅ Predictable  │
└────────────────────┴────────────────┴────────────────┘
```

---

## 13. LSM Trees in Real Databases

```
┌──────────────────┬───────────────────────────────────────────────┐
│ Database          │ How it uses LSM trees                          │
├──────────────────┼───────────────────────────────────────────────┤
│ Apache Cassandra │ LSM tree per node. Size-tiered compaction      │
│                  │ (default) or leveled. Wide-column data model.  │
│                  │                                                 │
│ RocksDB          │ Embeddable LSM engine by Facebook/Meta.        │
│                  │ Leveled compaction by default.                  │
│                  │ Used INSIDE other databases (CockroachDB, etc) │
│                  │                                                 │
│ LevelDB          │ Google's original LSM engine. Leveled           │
│                  │ compaction (hence the name). Simpler, older.   │
│                  │                                                 │
│ HBase            │ LSM tree on top of HDFS (Hadoop filesystem).   │
│                  │ Columnar data model. Used for huge datasets.   │
│                  │                                                 │
│ CockroachDB      │ Uses RocksDB/Pebble as its storage engine.     │
│                  │ Adds distributed SQL on top of LSM.            │
│                  │                                                 │
│ ScyllaDB         │ Cassandra-compatible but written in C++.       │
│                  │ Custom LSM implementation for performance.     │
│                  │                                                 │
│ InfluxDB         │ Time-series database using LSM-like engine.    │
│                  │ Optimized for time-ordered writes.             │
│                  │                                                 │
│ MongoDB          │ WiredTiger offers LSM as an alternative to     │
│ (WiredTiger)     │ B-tree (though B-tree is the default).         │
│                  │                                                 │
│ SQLite (LSM ext) │ SQLite has an experimental LSM extension.      │
└──────────────────┴───────────────────────────────────────────────┘
```

---

## 14. Strengths and Weaknesses Summary

```
LSM-TREE STRENGTHS:
═══════════════════
  ✅ Blazing fast writes: all sequential I/O
     → 10-100x faster than B-tree on HDD
     → 5-10x faster than B-tree on SSD
  
  ✅ Great for write-heavy workloads
     → Time-series, logging, messaging, event sourcing
  
  ✅ Space efficient (with leveled compaction)
     → ~10% overhead vs ~30-50% for B-trees
  
  ✅ Simple crash recovery: replay WAL
     → No complex page-level recovery needed
  
  ✅ Immutable files: no read-write conflicts
     → Great for concurrent workloads


LSM-TREE WEAKNESSES:
════════════════════
  ❌ Reads can be slower (must check multiple SSTables)
     → Mitigated by bloom filters and compaction
  
  ❌ Background compaction uses CPU and disk I/O
     → Can cause latency spikes if not tuned properly
  
  ❌ Write amplification (10-30x for leveled compaction)
     → Each byte is rewritten multiple times during compaction
     → May reduce SSD lifespan (SSD writes are limited)
  
  ❌ Unpredictable read latency
     → Usually fast, but occasional slow reads when checking many SSTables
  
  ❌ Range queries across multiple SSTables can be expensive
     → Must merge-sort results from multiple files
     → B-tree range scans are simpler (linked leaf nodes)


WHEN TO USE LSM-TREES:
  → Write-heavy workloads (> 10K writes/sec)
  → Time-series data, logging, event streams
  → Key-value stores with point lookups
  → Large datasets that don't fit in RAM
  → When you can tolerate slightly variable read latency

WHEN NOT TO USE LSM-TREES:
  → Read-heavy, write-light workloads (B-tree is better)
  → Need consistent, predictable read latency (B-tree is better)
  → Heavy range queries (B-tree's linked leaves are better)
  → Need strong ACID transactions (B-tree databases have better support)
```

---

### Quick Reference: The Complete LSM-Tree Data Flow

```
                           CLIENT
                             │
                      PUT(key, value)
                             │
                    ┌────────▼────────┐
                    │   1. Write to   │
                    │      WAL        │ ← Sequential disk append
                    │   (durability)  │
                    └────────┬────────┘
                             │
                    ┌────────▼────────┐
                    │  2. Insert into │
                    │    Memtable     │ ← In-memory sorted structure
                    │  (skip list)    │
                    └────────┬────────┘
                             │
                    When memtable is full (64 MB):
                             │
                    ┌────────▼────────┐
                    │  3. Flush to    │
                    │    SSTable      │ ← Sequential disk write
                    │  (sorted file)  │
                    └────────┬────────┘
                             │
                    Over time, SSTables accumulate:
                             │
                    ┌────────▼────────┐
                    │  4. Compaction  │ ← Background: merge SSTables
                    │  (merge-sort)   │   Remove old versions + tombstones
                    │                 │   Reduce number of files
                    └─────────────────┘

  READ PATH:
    1. Check memtable (in memory)     ← ~0.001 ms
    2. Check bloom filters (in memory) ← ~0.001 ms per SSTable  
    3. Read SSTable data block (disk)  ← ~0.1 ms per disk read
    Return the NEWEST version found.
```

---

*Previous: [← B-Tree Deep Dive](02-btree-deep-dive.md) | Next: [Hash Index Deep Dive →](04-hash-index.md)*