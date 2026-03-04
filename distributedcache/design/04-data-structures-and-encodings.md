# Data Structures & Internal Encodings — Deep Dive

## Table of Contents
1. [SDS (Simple Dynamic Strings)](#1-sds-simple-dynamic-strings)
2. [Lists — quicklist](#2-lists--quicklist)
3. [Sets — intset / hashtable](#3-sets--intset--hashtable)
4. [Sorted Sets — skiplist + hashtable / listpack](#4-sorted-sets--skiplist--hashtable--listpack)
5. [Hashes — listpack / hashtable](#5-hashes--listpack--hashtable)
6. [Streams — radix tree of listpacks](#6-streams--radix-tree-of-listpacks)
7. [HyperLogLog](#7-hyperloglog)
8. [Bitmaps](#8-bitmaps)
9. [Geospatial](#9-geospatial)
10. [Why Compact Encodings Matter](#10-why-compact-encodings-matter)
11. [Contrast with Memcached](#11-contrast-with-memcached)

---

## 1. SDS (Simple Dynamic Strings)

Redis does not use C null-terminated strings. It uses its own string library called **SDS**
(Simple Dynamic Strings), defined in `sds.h` and `sds.c`.

### Why not C strings?

| Problem with C strings       | SDS solution                                        |
|------------------------------|-----------------------------------------------------|
| O(N) strlen                  | O(1) — `len` field stored in header                 |
| Buffer overflow risk         | Automatic bounds checking before append             |
| Not binary-safe (stops at \0)| Binary-safe — uses `len`, not null terminator       |
| Every append requires realloc| Pre-allocation strategy reduces reallocations        |
| No embedded metadata         | Header contains len, alloc, flags                   |

### SDS memory layout

```
  ┌──────────────────────────────────────────────────────────┐
  │                    SDS Header + Data                      │
  │                                                           │
  │  ┌──────┬───────┬───────┬──────────────────────┬──────┐  │
  │  │ len  │ alloc │ flags │      buf[]           │ '\0' │  │
  │  │(used)│(total)│(type) │   (actual string)    │      │  │
  │  └──────┴───────┴───────┴──────────────────────┴──────┘  │
  │                         ▲                                  │
  │                         │                                  │
  │               SDS pointer points here                     │
  │               (compatible with C string functions)        │
  └──────────────────────────────────────────────────────────┘
```

The SDS pointer points to the start of `buf[]`, so it can be passed directly to C string
functions like `printf("%s", sds_string)`. The header sits *before* the pointer in memory.

### SDS header types

Redis uses different header sizes based on string length to minimize memory overhead:

| Type       | Max Length        | Header Size | `len` / `alloc` Type |
|------------|-------------------|-------------|----------------------|
| `sdshdr5`  | 31 bytes          | 1 byte      | 5 bits in flags      |
| `sdshdr8`  | 255 bytes         | 3 bytes     | uint8_t              |
| `sdshdr16` | 65,535 bytes      | 5 bytes     | uint16_t             |
| `sdshdr32` | 4,294,967,295 B   | 9 bytes     | uint32_t             |
| `sdshdr64` | 2^64 - 1 bytes    | 17 bytes    | uint64_t             |

A 10-byte string uses `sdshdr8` (3-byte header) instead of `sdshdr64` (17-byte header).
For millions of small strings, this saves significant memory.

### Pre-allocation strategy

When an SDS string grows via `sdscat` or `sdscatlen`:

```
if new_length < 1 MB:
    allocate 2 × new_length       # Double the space
else:
    allocate new_length + 1 MB    # Grow by 1 MB increments
```

This amortizes the cost of repeated appends to O(1) per character on average (same strategy
as Java's `ArrayList` or Go's `slice`).

### Key properties

- **Max value size**: 512 MB (enforced by Redis, not SDS)
- **Binary safe**: Can store JPEG images, protobuf blobs, anything
- **Null terminated**: Still ends with `\0` for C compatibility, but `len` is the source of truth
- **Lazy free**: When truncated, excess memory is kept (can be reclaimed with `sdsRemoveFreeSpace`)

---

## 2. Lists — quicklist

### Encoding evolution

```
  Redis < 3.2          Redis 3.2 - 6.x         Redis 7.0+
  ─────────────        ────────────────         ──────────────
  ziplist (small)      quicklist of             quicklist of
       or              ziplists                 listpacks
  linkedlist (large)
```

### listpack — the building block

A listpack is a **contiguous byte array** that stores a sequence of entries:

```
  ┌────────┬─────────┬─────────┬─────────┬─────────┬─────┐
  │ total  │ entry 1 │ entry 2 │ entry 3 │   ...   │ EOF │
  │ bytes  │         │         │         │         │ 0xFF│
  └────────┴─────────┴─────────┴─────────┴─────────┴─────┘

  Each entry:
  ┌──────────────┬──────────┬──────────────┐
  │ encoding +   │ data     │ entry-len    │
  │ data length  │          │ (backtrack)  │
  └──────────────┴──────────┴──────────────┘
```

**Why listpack replaced ziplist**: Ziplist had a cascading update bug — inserting/deleting
entries could trigger chain updates of the `prevlen` field across many entries. Listpack
eliminates this by using a `backtrack` field (entry's own length) instead of storing the
previous entry's length.

Properties:
- **Cache-friendly**: Contiguous memory, no pointers, minimal fragmentation
- **Compact**: No per-entry overhead for pointers (a linked list node costs 16+ bytes in pointers alone)
- **O(N) access**: Must scan from one end to reach the Nth element
- **Small N is fine**: For lists under ~128 entries, sequential scan is faster than pointer chasing

### quicklist — linked list of listpacks

```
  ┌────────────────────────────────────────────────────────────┐
  │                        quicklist                            │
  │                                                             │
  │  ┌───────────┐    ┌───────────┐    ┌───────────┐           │
  │  │ quicklist │◄──►│ quicklist │◄──►│ quicklist │           │
  │  │ node      │    │ node      │    │ node      │           │
  │  │           │    │           │    │           │           │
  │  │ ┌───────┐ │    │ ┌───────┐ │    │ ┌───────┐ │           │
  │  │ │listpck│ │    │ │listpck│ │    │ │listpck│ │           │
  │  │ │ e1 e2 │ │    │ │ e3 e4 │ │    │ │ e5 e6 │ │           │
  │  │ │ e3    │ │    │ │ e5 e6 │ │    │ │ e7    │ │           │
  │  │ └───────┘ │    │ └───────┘ │    │ └───────┘ │           │
  │  └───────────┘    └───────────┘    └───────────┘           │
  │                                                             │
  │  head ──────────────────────────────────────► tail          │
  │  O(1) LPUSH/LPOP                    O(1) RPUSH/RPOP       │
  └────────────────────────────────────────────────────────────┘
```

### Configuration

| Config                      | Default | Meaning                                         |
|-----------------------------|---------|--------------------------------------------------|
| `list-max-listpack-size`    | -2      | Negative: max bytes per node (-2 = 8KB)          |
| `list-compress-depth`       | 0       | Compress inner nodes with LZF (0 = disabled)     |

`list-max-listpack-size` negative value mapping:

| Value | Max Node Size |
|-------|---------------|
| -1    | 4 KB          |
| -2    | 8 KB (default)|
| -3    | 16 KB         |
| -4    | 32 KB         |
| -5    | 64 KB         |

`list-compress-depth`: Number of uncompressed nodes to keep at each end. Example with
depth=1: head and tail nodes are uncompressed (hot), all interior nodes are LZF-compressed.

### Encoding transition

```
  ┌──────────────────────┐
  │  Always quicklist     │
  │  (since Redis 7.0)   │
  │                       │
  │  Small list:          │
  │  quicklist with 1     │
  │  listpack node        │
  │                       │
  │  Large list:          │
  │  quicklist with many  │
  │  listpack nodes       │
  └──────────────────────┘
```

In Redis 7.0+, lists always use quicklist. The distinction between "small" and "large" is
simply the number of quicklist nodes. A list with 3 entries has one listpack node inside
the quicklist. A list with 100,000 entries has many nodes.

---

## 3. Sets — intset / hashtable

### Encoding selection

```
                          ┌──────────────────┐
                          │   SET created     │
                          └────────┬─────────┘
                                   │
                          ┌────────▼─────────┐
                          │ All members are   │
                          │ integers AND      │──── No ───┐
                          │ count <= 512?     │           │
                          └────────┬─────────┘           │
                                   │ Yes                  │
                          ┌────────▼─────────┐           │
                          │    intset         │           │
                          └────────┬─────────┘           │
                                   │                      │
                          (element added that is          │
                           non-integer OR count           │
                           exceeds threshold)             │
                                   │                      │
                          ┌────────▼─────────┐           │
                          │   hashtable      │◄──────────┘
                          └──────────────────┘

  Redis 7.2+ addition:
  Small sets with mixed types can use listpack encoding
  (controlled by set-max-listpack-entries, default 128)
```

### intset

A **sorted array of integers** stored in contiguous memory:

```
  ┌──────────┬────────┬─────┬─────┬─────┬─────┬─────┐
  │ encoding │ length │  3  │  7  │ 15  │ 42  │ 99  │
  │ (int16/  │   (5)  │     │     │     │     │     │
  │  int32/  │        │     │     │     │     │     │
  │  int64)  │        │     │     │     │     │     │
  └──────────┴────────┴─────┴─────┴─────┴─────┴─────┘
                       ◄── sorted, binary search ──►
```

| Property           | Value                                              |
|--------------------|----------------------------------------------------|
| Membership test    | O(log N) via binary search                         |
| Add                | O(N) — may need to shift elements                  |
| Memory per element | 2 bytes (int16), 4 bytes (int32), or 8 bytes (int64) |
| Upgrade            | When a value exceeds current encoding range, all elements upgrade |

The encoding field determines integer width. If all values fit in int16, each entry is 2 bytes.
Adding a value > 32767 triggers an upgrade: all entries are re-encoded as int32 (4 bytes each).

### Configuration

| Config                       | Default | Effect                                       |
|------------------------------|---------|----------------------------------------------|
| `set-max-intset-entries`     | 512     | Max elements before intset → hashtable       |
| `set-max-listpack-entries`   | 128     | (Redis 7.2+) Max for listpack encoding       |

### hashtable encoding

When a set exceeds the intset threshold or contains a non-integer, Redis uses its standard
**dict** (hash table) with all values set to NULL — effectively a hash set.

- O(1) average membership test, add, remove
- Each entry: dictEntry struct (24 bytes) + SDS key + pointer overhead
- Progressive rehashing when load factor > 1 (or > 5 during background save)

---

## 4. Sorted Sets — skiplist + hashtable / listpack

Sorted sets are Redis's most sophisticated data structure internally because they need to
support two different access patterns efficiently.

### The dual-structure problem

| Operation                | Needed Structure      | Time Complexity     |
|--------------------------|-----------------------|---------------------|
| Get score by member      | Hash table            | O(1)                |
| Get rank by score        | Skiplist              | O(log N)            |
| Range by score           | Skiplist              | O(log N + M)        |
| Range by rank            | Skiplist              | O(log N + M)        |
| Add / update member      | Both                  | O(log N)            |
| Remove member            | Both                  | O(log N)            |

No single data structure does both O(1) point lookups by member AND O(log N) range queries
by score. So Redis maintains **both a skiplist and a hash table** in sync.

### Full encoding: skiplist + hashtable

```
  ┌────────────────────────────────────────────────────────┐
  │                    Sorted Set (zset)                    │
  │                                                         │
  │  ┌──────────────────────┐  ┌────────────────────────┐  │
  │  │     Hash Table       │  │       Skiplist          │  │
  │  │     (dict)           │  │                         │  │
  │  │                      │  │  Level 3: ──────────────│──┤
  │  │  "alice" → 95.0     │  │  Level 2: ──── ● ──────│──┤
  │  │  "bob"   → 87.0     │  │  Level 1: ● ── ● ── ● │  │
  │  │  "carol" → 92.0     │  │  Level 0: ● ── ● ── ● │  │
  │  │                      │  │           bob carol alice│  │
  │  │  O(1) score lookup   │  │          87.0 92.0 95.0 │  │
  │  │  by member name      │  │                         │  │
  │  │                      │  │  O(log N) range queries │  │
  │  │                      │  │  ordered by score       │  │
  │  └──────────────────────┘  └────────────────────────┘  │
  │                                                         │
  │  Both structures share the same SDS member strings      │
  │  (pointers, not copies — no memory duplication)         │
  └────────────────────────────────────────────────────────┘
```

### Redis skiplist structure

```
  Header
    │
    ▼
  ┌─────┐    ┌─────┐    ┌─────┐    ┌─────┐    ┌─────┐
  │ HDR │───►│ bob │───►│carol│───►│alice│───►│ NIL │  Level 0
  │     │    │87.0 │    │92.0 │    │95.0 │    │     │
  │     │───►│     │───────────────►│     │───►│     │  Level 1
  │     │    │     │    │     │    │     │    │     │
  │     │──────────────────────────►│     │───►│     │  Level 2
  └─────┘    └─────┘    └─────┘    └─────┘    └─────┘

  Each node has:
  - member (SDS string, shared with dict)
  - score (double)
  - backward pointer (for reverse traversal)
  - level[] array: forward pointer + span per level
```

**Skiplist properties**:
- Probabilistic balancing: each node gets a random level (P=0.25, max 32 levels)
- O(log N) search, insert, delete (expected)
- O(log N + M) range queries (M = number of elements in range)
- Simpler than balanced BSTs (AVL, Red-Black) — easier to implement range operations

**Why skiplist over balanced BST?**
- Range operations are trivial: follow forward pointers at level 0
- Concurrent-friendly structure (though Redis is single-threaded, this influenced the original design)
- Simpler implementation (~350 lines vs ~1000+ for Red-Black trees)
- Equivalent O(log N) performance for Redis's use cases

### Small encoding: listpack

```
  ┌──────────────────────────────────────────────────────┐
  │              listpack (sorted by score)               │
  │                                                       │
  │  ┌───────┬──────┬───────┬──────┬───────┬──────┐      │
  │  │ "bob" │ 87.0 │"carol"│ 92.0 │"alice"│ 95.0 │      │
  │  └───────┴──────┴───────┴──────┴───────┴──────┘      │
  │  member-score pairs, sorted by score                  │
  └──────────────────────────────────────────────────────┘
```

### Encoding transition

```
  ┌───────────────┐                    ┌────────────────────────┐
  │   listpack    │─── triggers ──────►│  skiplist + hashtable  │
  │   encoding    │    when:           │  encoding              │
  └───────────────┘                    └────────────────────────┘

  Triggers:
  1. Element count > zset-max-listpack-entries (default 128)
  2. Any member string > zset-max-listpack-value bytes (default 64)
```

### Configuration

| Config                        | Default | Effect                                    |
|-------------------------------|---------|-------------------------------------------|
| `zset-max-listpack-entries`   | 128     | Max elements for listpack encoding        |
| `zset-max-listpack-value`     | 64      | Max member string length for listpack     |

---

## 5. Hashes — listpack / hashtable

### Encoding selection

```
  ┌────────────────────┐
  │   HSET key f1 v1   │
  └─────────┬──────────┘
            │
  ┌─────────▼────────────────┐
  │  field count <= 128 AND  │──── No ────► hashtable
  │  all field names AND     │
  │  all values <= 64 bytes? │
  └─────────┬────────────────┘
            │ Yes
            ▼
        listpack
```

### listpack encoding

Fields and values are stored as alternating entries:

```
  ┌──────────────────────────────────────────────────────┐
  │              listpack (field-value pairs)             │
  │                                                       │
  │  ┌──────┬────────┬──────┬────────┬──────┬────────┐   │
  │  │field1│ value1 │field2│ value2 │field3│ value3 │   │
  │  └──────┴────────┴──────┴────────┴──────┴────────┘   │
  │                                                       │
  │  Lookup "field2":                                     │
  │  Scan entries: field1 (skip) → value1 (skip) →       │
  │  field2 (match!) → return value2                     │
  │                                                       │
  │  O(N) lookup but N is small (max 128 entries)        │
  └──────────────────────────────────────────────────────┘
```

### hashtable encoding

Uses Redis's `dict` — a hash table with incremental rehashing:

```
  ┌─────────────────────────────────────────┐
  │               dict (hash table)          │
  │                                          │
  │  ht[0] (active table)                   │
  │  ┌─────┬─────┬─────┬─────┬─────┐       │
  │  │  0  │  1  │  2  │  3  │ ... │       │
  │  └──┬──┴──┬──┴─────┴──┬──┴─────┘       │
  │     │     │           │                  │
  │     ▼     ▼           ▼                  │
  │  ┌─────┐┌─────┐   ┌─────┐              │
  │  │f1:v1││f2:v2│   │f3:v3│              │
  │  └─────┘└─────┘   └─────┘              │
  │                                          │
  │  ht[1] (rehash target — used during     │
  │         incremental rehashing)           │
  └─────────────────────────────────────────┘
```

**Incremental rehashing**: When the hash table needs to grow or shrink, Redis does not
rehash everything at once (that would block the event loop). Instead, it moves a few
buckets per command execution and during `serverCron`. Both `ht[0]` and `ht[1]` are
consulted during the rehash period. Lookups check `ht[0]` first, then `ht[1]`.

### Configuration

| Config                        | Default | Effect                                    |
|-------------------------------|---------|-------------------------------------------|
| `hash-max-listpack-entries`   | 128     | Max field-value pairs for listpack        |
| `hash-max-listpack-value`     | 64      | Max field/value byte length for listpack  |

### Redis 7.4+: Per-field TTL

Redis 7.4 added expiration at the hash field level:

| Command               | Description                              |
|-----------------------|------------------------------------------|
| `HEXPIRE key secs field [field ...]`  | Set TTL on specific fields   |
| `HPEXPIRE key ms field [field ...]`   | Set TTL in milliseconds      |
| `HTTL key field [field ...]`          | Get remaining TTL of fields  |
| `HPERSIST key field [field ...]`      | Remove TTL from fields       |

This eliminates the previous workaround of using separate top-level keys with TTLs to
simulate per-field expiration. Internally, fields with TTLs are tracked in a dedicated
expiration metadata structure within the hash.

---

## 6. Streams — radix tree of listpacks

Streams are Redis's append-only log data structure, introduced in Redis 5.0. They are
designed for event sourcing, message queues, and time-series data.

### Structure overview

```
  ┌──────────────────────────────────────────────────────────┐
  │                      Redis Stream                         │
  │                                                           │
  │  ┌──────────────────────────────────────────┐             │
  │  │           Radix Tree (rax)               │             │
  │  │                                           │             │
  │  │  Key: message ID prefix (milliseconds)   │             │
  │  │  Value: listpack of entries              │             │
  │  │                                           │             │
  │  │  "1609459200000" → [listpack]            │             │
  │  │  "1609459200001" → [listpack]            │             │
  │  │  "1609459200002" → [listpack]            │             │
  │  └──────────────────────────────────────────┘             │
  │                                                           │
  │  ┌──────────────────────────────────────────┐             │
  │  │         Consumer Groups                   │             │
  │  │                                           │             │
  │  │  Group "processors":                      │             │
  │  │    last_delivered_id: 1609459200001-0     │             │
  │  │    consumers:                             │             │
  │  │      "worker-1": PEL [id1, id3]          │             │
  │  │      "worker-2": PEL [id2]               │             │
  │  │                                           │             │
  │  │  Group "analytics":                       │             │
  │  │    last_delivered_id: 1609459200000-5     │             │
  │  │    consumers:                             │             │
  │  │      "analyzer-1": PEL [id4]             │             │
  │  └──────────────────────────────────────────┘             │
  └──────────────────────────────────────────────────────────┘
```

### Message ID format

```
  <millisecondsTimestamp>-<sequenceNumber>
  Example: 1609459200000-0, 1609459200000-1, 1609459200001-0
```

- Timestamp: milliseconds since Unix epoch (auto-generated or user-specified)
- Sequence: auto-incrementing within the same millisecond
- IDs are always increasing — enforced by Redis

### Radix tree + listpack storage

The radix tree (rax) stores entries efficiently:
- Tree keys are the ID prefixes (common millisecond timestamps are compressed)
- Tree values are listpacks containing message field-value data
- Multiple entries with the same field schema share a master entry in the listpack (field names stored once)

### Consumer groups

| Command                                     | Description                              |
|---------------------------------------------|------------------------------------------|
| `XGROUP CREATE stream group id`             | Create a consumer group                  |
| `XREADGROUP GROUP group consumer COUNT n STREAMS stream >` | Read new messages  |
| `XACK stream group id [id ...]`             | Acknowledge processed messages           |
| `XPENDING stream group`                     | List pending (unacknowledged) messages   |
| `XCLAIM stream group consumer min-idle id`  | Claim stuck messages from dead consumers |
| `XAUTOCLAIM stream group consumer min-idle start` | Auto-claim idle messages           |

### PEL (Pending Entry List)

The PEL tracks messages that have been delivered to consumers but not yet acknowledged:

```
  Consumer "worker-1" PEL:
  ┌───────────────────┬───────────────┬──────────────────┐
  │ Message ID        │ Delivery Time │ Delivery Count   │
  ├───────────────────┼───────────────┼──────────────────┤
  │ 1609459200000-0   │ 1609459200100 │ 1                │
  │ 1609459200001-3   │ 1609459200200 │ 3 (retried)      │
  └───────────────────┴───────────────┴──────────────────┘
```

If a consumer crashes, its PEL entries remain. Other consumers can use `XCLAIM` or
`XAUTOCLAIM` to take over those messages after a timeout (min-idle).

---

## 7. HyperLogLog

A probabilistic data structure for **cardinality estimation** (counting unique elements).

| Property          | Value                                              |
|-------------------|----------------------------------------------------|
| Memory per key    | 12 KB (dense), <1 KB (sparse, for small sets)     |
| Standard error    | ~0.81%                                             |
| Max cardinality   | 2^64 unique elements                               |
| Commands          | `PFADD`, `PFCOUNT`, `PFMERGE`                      |

### How it works (simplified)

1. Hash each element to a 64-bit value
2. Use the first 14 bits to select one of 16,384 registers
3. Count leading zeros in the remaining 50 bits
4. Store the maximum leading-zero count per register
5. Apply harmonic mean formula across all registers to estimate cardinality

### Encoding

- **Sparse**: For small HLLs, uses a run-length encoded representation (~200 bytes for <1000 elements)
- **Dense**: 16,384 registers x 6 bits = 12,288 bytes = 12 KB (fixed)
- Automatic promotion from sparse to dense when thresholds are exceeded

### Use cases

- Counting unique visitors, unique IPs, unique search queries
- Any "count distinct" problem where exact counts are not required
- Merging counts across time periods with `PFMERGE`

---

## 8. Bitmaps

Bitmaps are not a separate data type — they are **String values** with bit-level operations.

| Command                        | Description                                |
|--------------------------------|--------------------------------------------|
| `SETBIT key offset value`      | Set bit at offset to 0 or 1               |
| `GETBIT key offset`            | Get bit at offset                          |
| `BITCOUNT key [start end]`     | Count set bits in range                    |
| `BITOP AND/OR/XOR/NOT dest k1 [k2 ...]` | Bitwise operations between keys |
| `BITPOS key bit [start end]`   | Find first 0 or 1 bit                     |
| `BITFIELD key ...`             | Treat string as array of integers          |

### Memory

- Offset-based: `SETBIT key 1000000 1` allocates ~125 KB (offset/8 bytes)
- Sparse bitmaps waste memory — use HyperLogLog or Bloom filters instead
- Dense bitmaps (e.g., daily active users where user IDs are dense): very memory-efficient

### Use cases

- Feature flags per user: `SETBIT feature:dark_mode <user_id> 1`
- Daily active users: `SETBIT dau:2024-01-15 <user_id> 1`, then `BITCOUNT` for total
- Bloom filters: implemented on top of bitmap operations

---

## 9. Geospatial

Geospatial data is stored as a **Sorted Set** with **geohash-encoded scores**.

| Command                                          | Description                        |
|--------------------------------------------------|------------------------------------|
| `GEOADD key longitude latitude member`           | Add location                       |
| `GEODIST key member1 member2 [unit]`             | Distance between two members       |
| `GEORADIUS key lon lat radius unit`              | Find members within radius         |
| `GEOSEARCH key FROMMEMBER m BYRADIUS r unit`     | Search from member (Redis 6.2+)    |
| `GEOPOS key member`                              | Get coordinates of member          |
| `GEOHASH key member`                             | Get geohash string                 |

### How it works

1. Longitude and latitude are encoded into a 52-bit geohash
2. The geohash is stored as the score in a Sorted Set
3. Nearby locations have similar geohash prefixes
4. Radius queries use score ranges to find candidates, then filter by exact distance

### Properties

- Uses the same skiplist + hashtable structure as sorted sets
- Geohash precision: ~0.6mm at 52 bits
- Earth model: WGS-84 (same as GPS)
- No support for polygons or arbitrary shapes — radius and box queries only

---

## 10. Why Compact Encodings Matter

### Memory is the bottleneck

For an in-memory store, every byte of overhead matters. Consider a hash with 10 small fields:

| Encoding     | Approximate Memory per Hash | Overhead                         |
|--------------|-----------------------------|---------------------------------|
| listpack     | ~200-300 bytes              | Minimal — contiguous array       |
| hashtable    | ~800-1200 bytes             | dictEntry pointers, SDS headers  |

With 1 million hashes, the difference is **~500 MB - 900 MB**.

### CPU cache effects

```
  listpack (contiguous memory):
  ┌─────────────────────────────────────────────┐
  │ f1 v1 f2 v2 f3 v3 f4 v4 f5 v5             │  ← One cache line fetch
  └─────────────────────────────────────────────┘    covers multiple entries

  hashtable (pointer-based):
  ┌────┐   ┌────┐   ┌────┐   ┌────┐   ┌────┐
  │ ptr├──►│ ptr├──►│ ptr├──►│ ptr├──►│ ptr│    ← Each dereference
  └────┘   └────┘   └────┘   └────┘   └────┘      may cause a cache miss
    │        │        │        │        │
    ▼        ▼        ▼        ▼        ▼
  [SDS]    [SDS]    [SDS]    [SDS]    [SDS]       ← Scattered in memory
```

Modern CPUs fetch 64-byte cache lines. A listpack can serve multiple field lookups from
a single cache line. A hashtable requires pointer chasing across scattered memory locations,
causing L1/L2 cache misses.

### Automatic upgrade — transparent to the application

```
  Application code:                     Redis internal behavior:

  HSET user:1 name "Alice"              → listpack (1 field, small)
  HSET user:1 age "30"                  → listpack (2 fields, small)
  ...
  HSET user:1 field_129 "value"         → hashtable (exceeded 128 entries)
                                             Automatic conversion.
                                             Same commands, same API.
                                             Application sees no difference.
```

### Redis design philosophy

**Optimize for the common case.** Most Redis objects are small:
- Hash with 5-20 fields (user profiles, session data)
- Set with 10-50 members (tags, categories)
- Sorted set with 50-100 members (leaderboard page)
- List with 20-100 elements (recent activity feed)

These all fit comfortably in compact encodings, saving massive amounts of memory at scale.

### Complete encoding summary table

| Data Type   | Small/Compact Encoding    | Large/Full Encoding          | Transition Triggers                        |
|-------------|---------------------------|------------------------------|--------------------------------------------|
| String      | int (if integer ≤ 2^63)   | SDS (raw or embstr)          | Value is not an integer or > 44 bytes      |
| List        | quicklist (1 listpack node)| quicklist (many nodes)       | Always quicklist, nodes split by size      |
| Set         | intset                    | hashtable                    | Non-integer added OR count > 512           |
| Set (7.2+)  | listpack                  | hashtable                    | count > 128 or value > 64 bytes            |
| Sorted Set  | listpack                  | skiplist + hashtable         | count > 128 or member > 64 bytes           |
| Hash        | listpack                  | hashtable                    | fields > 128 or field/value > 64 bytes     |
| Stream      | radix tree + listpacks    | (always this encoding)       | N/A                                        |

### String encoding detail

Strings have three internal encodings:

| Encoding  | Condition                           | Storage                                |
|-----------|-------------------------------------|----------------------------------------|
| `int`     | Value is integer ≤ 2^63 - 1        | Stored directly in redisObject pointer |
| `embstr`  | String ≤ 44 bytes                   | redisObject + SDS in single allocation |
| `raw`     | String > 44 bytes                   | redisObject + separate SDS allocation  |

`embstr` is read-only. Any modification (APPEND, SETRANGE) converts it to `raw`. The
44-byte threshold ensures redisObject (16 bytes) + sdshdr8 (3 bytes) + string + null
terminator fits in a single 64-byte allocator bucket (jemalloc).

---

## 11. Contrast with Memcached

Memcached supports **strings only** — all data structure logic must live in the client.

### Data model comparison

| Aspect                     | Redis                              | Memcached                          |
|----------------------------|------------------------------------|------------------------------------|
| Data types                 | Strings, Lists, Sets, Sorted Sets, Hashes, Streams, HyperLogLog, Bitmaps, Geospatial | Strings only |
| Server-side operations     | LPUSH, ZADD, SINTER, HSET, etc.   | GET, SET, ADD, REPLACE, DELETE, INCR/DECR |
| Max value size             | 512 MB                             | 1 MB default (configurable)        |
| Structure manipulation     | Server-side (atomic)               | Client-side (read → modify → write)|
| Encoding optimization      | Multiple per type                  | None (raw bytes)                   |

### Memcached slab allocator

Memcached uses a **slab allocator** to avoid memory fragmentation:

```
  ┌────────────────────────────────────────────────────────────┐
  │              Memcached Slab Allocator                       │
  │                                                             │
  │  Slab Class 1 (96 bytes per chunk):                        │
  │  ┌──────┬──────┬──────┬──────┬──────┬──────┐               │
  │  │ item │ item │ free │ free │ item │ free │  ← 1 MB page  │
  │  └──────┴──────┴──────┴──────┴──────┴──────┘               │
  │                                                             │
  │  Slab Class 2 (120 bytes per chunk):                       │
  │  ┌──────┬──────┬──────┬──────┬──────┐                      │
  │  │ item │ free │ item │ item │ free │         ← 1 MB page  │
  │  └──────┴──────┴──────┴──────┴──────┘                      │
  │                                                             │
  │  Slab Class 3 (152 bytes per chunk):                       │
  │  ┌──────┬──────┬──────┬──────┐                             │
  │  │ item │ item │ free │ item │                ← 1 MB page  │
  │  └──────┴──────┴──────┴──────┘                             │
  │                                                             │
  │  ... up to ~42 slab classes                                │
  │  Growth factor: ~1.25x between classes                     │
  │  Page size: 1 MB                                           │
  └────────────────────────────────────────────────────────────┘
```

**How it works**:
1. Each slab class holds chunks of a fixed size (96, 120, 152, ... bytes)
2. Growth factor ~1.25x between classes (~42 classes total)
3. An item is stored in the smallest class that fits it
4. Pages (1 MB each) are allocated to slab classes on demand
5. LRU eviction operates per slab class

**Trade-offs**:
- Pro: Zero fragmentation within a slab class
- Pro: O(1) allocation (grab a free chunk from the class)
- Con: Internal fragmentation — a 97-byte item wastes 23 bytes in a 120-byte chunk
- Con: Slab calcification — pages assigned to one class cannot be easily reused by another
  (mitigated by slab reassignment / slab automove in modern Memcached)

### Memory management comparison

| Aspect                   | Redis                              | Memcached                        |
|--------------------------|------------------------------------|------------------------------------|
| Allocator                | jemalloc (default), tcmalloc, libc | Custom slab allocator              |
| Fragmentation handling   | Active defragmentation (Redis 4+)  | Slab classes minimize fragmentation|
| Memory overhead per key  | redisObject (16B) + encoding       | Item header (48-56B) + data        |
| Eviction policies        | 8 policies (LRU, LFU, random, TTL) | LRU per slab class                |
| Max memory config        | `maxmemory`                        | `-m` flag (default 64 MB)          |

### The fundamental trade-off

```
  ┌─────────────────────────────────────────────────────┐
  │                                                      │
  │  Memcached:  Simple data → Complex client logic     │
  │                                                      │
  │    Client must:                                      │
  │    - Serialize lists, sets, maps to strings          │
  │    - Read entire value, deserialize, modify,         │
  │      re-serialize, write back                        │
  │    - Handle race conditions with CAS                 │
  │    - Manage all data structure logic                 │
  │                                                      │
  │  Redis:     Rich data → Simple client logic          │
  │                                                      │
  │    Client just calls:                                │
  │    - LPUSH to add to a list                          │
  │    - SADD to add to a set                            │
  │    - ZADD to add to a sorted set                     │
  │    - HINCRBY to increment a hash field               │
  │    - All atomic, no CAS needed                       │
  │                                                      │
  └─────────────────────────────────────────────────────┘
```

This is the core reason Redis displaced Memcached for most use cases: **moving data structure
operations to the server eliminates network round-trips, race conditions, and serialization
overhead on the client side.**
