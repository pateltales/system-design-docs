# Datastore Internals — Part 4: Hash Index Deep Dive

> This document explains hash-based storage engines — the simplest and fastest storage model. We cover in-memory hash tables, Redis internals, the Bitcask model, and when to use in-memory stores vs disk-based ones.

---

## Table of Contents

1. [Hash Tables — The Fastest Data Structure](#1-hash-tables--the-fastest-data-structure)
2. [In-Memory Key-Value Stores](#2-in-memory-key-value-stores)
3. [Redis Internals — How the World's Most Popular Cache Works](#3-redis-internals)
4. [The Bitcask Model — Hash Index + Disk Persistence](#4-the-bitcask-model--hash-index--disk-persistence)
5. [Redis Persistence — RDB vs AOF](#5-redis-persistence--rdb-vs-aof)
6. [Memcached vs Redis](#6-memcached-vs-redis)
7. [When to Use Hash-Based Stores](#7-when-to-use-hash-based-stores)
8. [Strengths and Weaknesses Summary](#8-strengths-and-weaknesses-summary)

---

## 1. Hash Tables — The Fastest Data Structure

```
A hash table maps keys to values using a HASH FUNCTION.

How it works:
  1. Take a key: "user:42"
  2. Hash it:   hash("user:42") = 7392847
  3. Modulo:    7392847 % 16 = 15  (index into array of size 16)
  4. Store the value at array[15]

┌─────────────────────────────────────────────────────────┐
│  Hash Table (array of size 16)                           │
│                                                          │
│  Index:  [0] [1] [2] [3] [4] ... [15]                  │
│  Data:    -   -   -   -   -  ...  user:42 → "Alice"    │
│                                                          │
│  PUT("user:42", "Alice"):                                │
│    hash("user:42") % 16 = 15 → store at index 15       │
│    Time: O(1) ✓                                          │
│                                                          │
│  GET("user:42"):                                         │
│    hash("user:42") % 16 = 15 → read from index 15      │
│    Time: O(1) ✓                                          │
│                                                          │
│  DELETE("user:42"):                                      │
│    hash("user:42") % 16 = 15 → clear index 15          │
│    Time: O(1) ✓                                          │
│                                                          │
└─────────────────────────────────────────────────────────┘

Collision handling (when two keys hash to the same index):
  - Chaining: each array slot holds a linked list of entries
  - Open addressing: probe the next empty slot
  
  With a good hash function and load factor < 0.75,
  collisions are rare → average O(1) operations.


CRITICAL LIMITATION: NO RANGE QUERIES
  
  B-tree:  "Give me all keys between 'age:20' and 'age:30'" → Easy!
           (sorted structure, scan from 20 to 30)
  
  Hash:    "Give me all keys between 'age:20' and 'age:30'" → IMPOSSIBLE
           hash("age:20") = 7, hash("age:21") = 42, hash("age:22") = 3
           The hash function DESTROYS the ordering of keys.
           Must scan EVERY key → O(n). No better than unsorted array.
  
  If you need range queries → DON'T use hash-based storage.
  If you only need point lookups (GET by exact key) → hash is FASTEST.
```

---

## 2. In-Memory Key-Value Stores

```
WHY KEEP EVERYTHING IN MEMORY?

  Disk (SSD):  ~0.1 ms per random read   = ~10,000 reads/sec
  Memory:      ~0.0001 ms per read        = ~10,000,000 reads/sec
  
  Memory is 1,000x faster!
  
  If your data fits in RAM → keep it ALL in memory → maximum speed.

WHEN DOES DATA FIT IN RAM?

  Modern servers: 64 GB - 1 TB of RAM
  
  64 GB can hold:
    - ~640 million key-value pairs (if each is ~100 bytes)
    - ~1 billion small keys (counters, flags, small strings)
  
  For a cache layer or session store, this is often enough.
  For a full database with 100 TB of data, it's NOT enough → use disk-based.


THE VALUE PROPOSITION OF IN-MEMORY STORES:

  Use case: Cache frequently accessed data from a slower database
  
  Client → [Redis (in-memory cache)] → cache HIT → return instantly
                                     → cache MISS → [PostgreSQL (disk)] → return + cache it
  
  95% of reads hit the cache → 95% of reads are at memory speed.
  Only 5% go to the slow disk-based database.
  
  This is the #1 use case for Redis and Memcached in production.
```

---

## 3. Redis Internals

```
Redis = Remote Dictionary Server
The most popular in-memory data store. Let's understand how it works internally.


ARCHITECTURE: Single-Threaded Event Loop

  ┌────────────────────────────────────────────────────────────┐
  │  Redis Server Process                                       │
  │                                                             │
  │  ┌──────────────────────────────────────────────────────┐  │
  │  │  EVENT LOOP (single thread)                          │  │
  │  │                                                      │  │
  │  │  while (true):                                       │  │
  │  │    events = poll(sockets)   // check for client data │  │
  │  │    for event in events:                              │  │
  │  │      parse_command(event)                            │  │
  │  │      execute_command()     // O(1) hash operation    │  │
  │  │      send_response()                                 │  │
  │  │                                                      │  │
  │  └──────────────────────────────────────────────────────┘  │
  │                                                             │
  │  WHY SINGLE-THREADED?                                       │
  │  - No locks needed → no lock contention                    │
  │  - No context switching overhead                           │
  │  - Each command is atomic (no need for transactions)       │
  │  - Memory operations are so fast that CPU is not the       │
  │    bottleneck — network I/O is!                            │
  │                                                             │
  │  A single Redis instance: ~100,000-200,000 ops/sec         │
  │  The bottleneck is network, not CPU or memory.             │
  │                                                             │
  └────────────────────────────────────────────────────────────┘


DATA STRUCTURES INSIDE REDIS:

  Redis isn't just a hash table. It supports multiple data structures,
  each stored as values in the main hash table.

  ┌──────────────────┬────────────────────────────────────────────┐
  │ Type              │ Internal Implementation                     │
  ├──────────────────┼────────────────────────────────────────────┤
  │ String            │ Simple dynamic string (SDS)                 │
  │ (SET/GET)         │ O(1) get/set                                │
  │                   │                                              │
  │ Hash              │ ziplist (small) or hash table (large)       │
  │ (HSET/HGET)       │ O(1) get/set per field                      │
  │                   │                                              │
  │ List              │ quicklist (linked list of ziplists)         │
  │ (LPUSH/RPOP)      │ O(1) push/pop at ends                       │
  │                   │                                              │
  │ Set               │ intset (small, all integers) or hash table  │
  │ (SADD/SMEMBERS)   │ O(1) add/remove/check membership            │
  │                   │                                              │
  │ Sorted Set        │ skip list + hash table                      │
  │ (ZADD/ZRANGE)     │ O(log n) add, O(log n + k) range query     │
  │                   │ THE ONE STRUCTURE THAT SUPPORTS RANGES!     │
  │                   │                                              │
  │ Stream            │ Radix tree of listpacks                     │
  │ (XADD/XREAD)      │ Append-only log, like Kafka-lite            │
  └──────────────────┴────────────────────────────────────────────┘


MEMORY LAYOUT:

  Main dictionary:
  ┌────────────────────────────────────────────────────────┐
  │  Hash Table (dict)                                      │
  │                                                        │
  │  Bucket 0: → [key: "user:42", value_ptr: → String "Alice"]
  │  Bucket 1: → [key: "session:abc", value_ptr: → Hash {...}]
  │  Bucket 2: → NULL                                      │
  │  Bucket 3: → [key: "leaderboard", value_ptr: → SortedSet]
  │  ...                                                    │
  │                                                        │
  │  Each bucket: key (SDS string) + value (redisObject)   │
  │  redisObject: type + encoding + ptr_to_actual_data     │
  │                                                        │
  │  Memory overhead per key: ~50-70 bytes (for pointers,  │
  │  metadata, hash table entry). So 100M keys ≈ 5-7 GB   │
  │  of overhead BEFORE counting the actual data.          │
  └────────────────────────────────────────────────────────┘


EXPIRY / TTL:

  Redis supports setting a TTL (time-to-live) on any key:
    SET session:abc "data" EX 3600   ← expires in 1 hour
  
  How it works internally:
    - Separate "expires" hash table: key → expiry_timestamp
    - TWO eviction strategies:
      1. Lazy eviction: when a key is accessed, check if expired
      2. Active eviction: background task randomly samples keys,
         deletes expired ones (runs 10 times/sec, checks 20 random keys)
    
    This means expired keys might linger briefly before being cleaned up.
    But GET will never return an expired key (lazy check catches it).


MEMORY EVICTION (when Redis runs out of RAM):

  What happens when maxmemory is reached?
  
  Policies (configurable):
    noeviction:       Return error on writes (safe but blocks)
    allkeys-lru:      Evict least recently used key (most common)
    allkeys-lfu:      Evict least frequently used key
    volatile-lru:     Evict LRU among keys WITH an expiry set
    volatile-ttl:     Evict key with shortest TTL remaining
    allkeys-random:   Evict a random key
  
  Most production setups use: allkeys-lru
```

---

## 4. The Bitcask Model — Hash Index + Disk Persistence

```
What if we want hash table speed but also DURABILITY (survives crashes)?

The Bitcask model (used by Riak's default backend) is a clever solution:

  Keep a hash table in MEMORY (for fast lookups)
  Keep ALL data on DISK (in an append-only log)
  The hash table maps: key → (file_id, position_in_file, size)


WRITE PATH:

  PUT("user:42", "Alice"):
    1. Append to the active data file (on disk):
       [timestamp | key_size | value_size | key | value]
       [1688000000 | 7 | 5 | user:42 | Alice]
       
    2. Update in-memory hash table:
       hash["user:42"] = {file: "data_001.db", offset: 4096, size: 32}
    
    Disk write: SEQUENTIAL (append-only) → fast!
    Memory: O(1) hash update → instant!


READ PATH:

  GET("user:42"):
    1. Look up in hash table: hash["user:42"]
       → {file: "data_001.db", offset: 4096, size: 32}
    
    2. Seek to offset 4096 in data_001.db, read 32 bytes
       → "Alice"
    
    Memory lookup: O(1)
    Disk read: exactly 1 seek + read (no scanning!)
    Total: ~0.1ms (1 disk read)


COMPACTION (merging old files):

  Over time, old values accumulate (updates write new entries, old stay):
    data_001.db: [user:42=Alice] [user:99=Bob] [user:42=Alicia] [user:42=AJ]
    
  Only "user:42=AJ" (latest) matters. The rest are wasted space.
  
  Compaction scans old files, keeps only the latest version of each key,
  writes them to a new file. Similar to LSM compaction.


LIMITATION: All keys must fit in RAM!
  
  The hash table has an entry for EVERY key.
  Each entry: key (avg ~50 bytes) + file/offset/size (~24 bytes) = ~74 bytes
  
  100 million keys × 74 bytes = 7.4 GB of RAM just for the index
  1 billion keys × 74 bytes = 74 GB of RAM
  
  If you have more keys than fit in RAM → Bitcask won't work.
  Use LSM-tree or B-tree instead.
```

---

## 5. Redis Persistence — RDB vs AOF

```
Redis stores data in memory. But what if the server restarts?
Redis offers two persistence mechanisms:


OPTION 1: RDB (Redis Database Snapshots)
─────────────────────────────────────────
  Periodically saves a SNAPSHOT of all data to disk.
  
  save 900 1      ← snapshot if ≥1 key changed in 900 seconds
  save 300 100    ← snapshot if ≥100 keys changed in 300 seconds
  save 60 10000   ← snapshot if ≥10000 keys changed in 60 seconds

  How it works:
    1. Redis forks the process (copy-on-write)
    2. Child process writes ALL data to a temp file (dump.rdb)
    3. When done, atomically replaces old dump.rdb
    4. Parent continues serving requests (no downtime!)
  
  Pros: ✅ Compact (one binary file), ✅ fast restart (load whole file)
  Cons: ❌ Data loss between snapshots (up to minutes of data)
        ❌ Fork can be slow with large datasets (copy-on-write overhead)


OPTION 2: AOF (Append Only File)
─────────────────────────────────
  Logs EVERY write command to a file (like a WAL).
  
  SET user:42 Alice       ← written to AOF
  SET user:99 Bob         ← written to AOF  
  DEL user:42             ← written to AOF
  
  On restart: replay the AOF to rebuild the dataset.
  
  Sync policies:
    appendfsync always     ← fsync every command (safest, slowest)
    appendfsync everysec   ← fsync once per second (good compromise)
    appendfsync no         ← let OS decide when to flush (fastest, riskiest)
  
  Pros: ✅ At most 1 second of data loss (with everysec)
        ✅ Human-readable log file
  Cons: ❌ Larger file than RDB (every command is logged)
        ❌ Slower restart (must replay every command)
        ❌ AOF file grows unbounded → needs periodic REWRITE
  
  AOF Rewrite: compacts the AOF by replacing it with the minimal set
  of commands to recreate the current dataset.
  Before rewrite: SET x 1, SET x 2, SET x 3 (3 commands)
  After rewrite:  SET x 3 (1 command — only latest matters)


OPTION 3: RDB + AOF (Recommended for production)
──────────────────────────────────────────────────
  Use BOTH. RDB for periodic full snapshots, AOF for every-second durability.
  On restart: Redis uses AOF (more complete) if available, else RDB.
  
  This gives you:
    - Fast restarts (load RDB first, then apply recent AOF)
    - Minimal data loss (at most 1 second with AOF everysec)
    - Compact backups (RDB snapshots)
```

---

## 6. Memcached vs Redis

```
Both are in-memory key-value stores. When do you use which?

┌────────────────────┬─────────────────────┬─────────────────────┐
│ Feature             │ Redis                │ Memcached            │
├────────────────────┼─────────────────────┼─────────────────────┤
│ Data structures     │ Strings, Hashes,    │ Strings ONLY        │
│                     │ Lists, Sets, Sorted │                      │
│                     │ Sets, Streams, etc  │                      │
│                     │                      │                      │
│ Persistence         │ RDB + AOF           │ NONE (pure cache)   │
│                     │                      │                      │
│ Replication         │ Built-in primary-   │ None built-in       │
│                     │ replica replication  │                      │
│                     │                      │                      │
│ Clustering          │ Redis Cluster       │ Client-side          │
│                     │ (auto-sharding)     │ consistent hashing  │
│                     │                      │                      │
│ Threading           │ Single-threaded     │ Multi-threaded      │
│                     │ event loop          │                      │
│                     │                      │                      │
│ Memory efficiency   │ Higher overhead     │ Slab allocator,     │
│                     │ (rich data structs) │ less overhead        │
│                     │                      │                      │
│ Max value size      │ 512 MB              │ 1 MB (default)      │
│                     │                      │                      │
│ Typical throughput  │ 100-200K ops/sec    │ 200-400K ops/sec    │
│                     │                      │ (multi-threaded)    │
└────────────────────┴─────────────────────┴─────────────────────┘

CHOOSE REDIS WHEN:
  - Need data structures beyond strings (sorted sets, lists, etc.)
  - Need persistence (data survives restarts)
  - Need pub/sub, streams, Lua scripting
  - Need replication for high availability
  - "Swiss army knife" — does many things well

CHOOSE MEMCACHED WHEN:
  - Simple caching only (string key → string value)
  - Need maximum throughput (multi-threaded)
  - Don't need persistence
  - Want simplicity and predictability
  - Large-scale caching with simple needs (Facebook's use case)
```

---

## 7. When to Use Hash-Based Stores

```
PERFECT USE CASES FOR HASH-BASED / IN-MEMORY STORES:

1. CACHING
   Cache database query results, API responses, computed values.
   "Hot data in RAM, cold data on disk."
   → Redis or Memcached in front of PostgreSQL/MySQL

2. SESSION STORAGE
   Web session data: user login state, cart contents, preferences.
   → TTL-based: auto-expire sessions after 30 minutes
   → Redis with persistence (AOF) so sessions survive restarts

3. RATE LIMITING
   "User X can make 100 API calls per minute."
   → Redis INCR with TTL: increment counter, expire after 60 seconds

4. LEADERBOARDS / RANKINGS
   "Top 100 players by score"
   → Redis Sorted Set: ZADD leaderboard 9500 "player:42"
   → ZRANGE leaderboard 0 99 WITHSCORES → top 100 instantly

5. REAL-TIME COUNTERS
   Page views, likes, online user counts.
   → Redis INCR: atomic increment, no locks needed

6. PUB/SUB MESSAGING
   Real-time notifications, chat messages.
   → Redis PUBLISH/SUBSCRIBE

7. DISTRIBUTED LOCKS
   Coordinate access across multiple services.
   → Redis SET with NX (not exists) and PX (expiry)


DO NOT USE HASH-BASED STORES FOR:

  ❌ Primary database (data too large for RAM, need durability guarantees)
  ❌ Range queries ("all users aged 20-30") — hash destroys ordering
  ❌ Complex queries (joins, aggregations) — use SQL databases
  ❌ Data larger than available RAM — use disk-based stores
  ❌ Strong consistency requirements — Redis replication is async
```

---

## 8. Strengths and Weaknesses Summary

```
HASH-BASED STORE STRENGTHS:
═══════════════════════════
  ✅ Fastest possible reads: O(1) hash lookup in memory
     → ~0.0001 ms per read = ~10 million reads/sec
  
  ✅ Fastest possible writes: O(1) hash insert in memory
     → ~0.0001 ms per write (or ~0.01 ms with AOF persistence)
  
  ✅ Simple: no complex tree structures, no compaction, no pages
  
  ✅ Rich data structures (Redis): sorted sets, lists, streams
  
  ✅ Atomic operations: INCR, SETNX, etc. (no locks needed)
  
  ✅ TTL support: auto-expire keys (great for caching)


HASH-BASED STORE WEAKNESSES:
════════════════════════════
  ❌ Data must fit in RAM (expensive at scale)
     64 GB RAM ≈ $500-1000/month on cloud (vs $50/month for 1 TB SSD)
  
  ❌ No range queries (hash destroys key ordering)
  
  ❌ Durability concerns: Redis persistence has a small data loss window
  
  ❌ No complex queries: can't do JOINs, GROUP BY, etc.
  
  ❌ Memory overhead: each key costs ~50-70 bytes of metadata
     → 100M keys = 5-7 GB just for metadata, before data
  
  ❌ Replication is async (Redis): risk of data loss on failover


COMPARISON WITH DISK-BASED ENGINES:

┌──────────────────────┬───────────┬───────────┬──────────────┐
│                      │ Hash/Mem  │ B-Tree    │ LSM-Tree     │
├──────────────────────┼───────────┼───────────┼──────────────┤
│ Read latency         │ ~0.0001ms │ ~0.1-1ms  │ ~0.001-1ms   │
│ Write latency        │ ~0.0001ms │ ~0.1-1ms  │ ~0.01ms      │
│ Range queries        │ ❌ No     │ ✅ Fast   │ ⚠️ Moderate  │
│ Data capacity        │ ≤ RAM     │ ≤ Disk    │ ≤ Disk       │
│ Cost per GB          │ $$$       │ $         │ $            │
│ Durability           │ ⚠️ Weak   │ ✅ Strong │ ✅ Strong    │
│ Transactions (ACID)  │ ❌ No     │ ✅ Yes    │ ⚠️ Limited   │
│ Best for             │ Cache     │ OLTP/SQL  │ Write-heavy  │
└──────────────────────┴───────────┴───────────┴──────────────┘
```

---

*Previous: [← LSM-Tree Deep Dive](03-lsm-tree-deep-dive.md) | Next: [Column Stores →](05-column-stores.md)*