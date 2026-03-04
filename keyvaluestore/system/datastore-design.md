# Distributed Key-Value Store — Datastore Design

> Continuation of the interview simulation. The interviewer asked: "Walk me through the storage architecture — what data structures and stores does each node use, and what are the trade-offs?"

---

## Overview: Storage Domains & Choices

| Data Domain | Storage Engine / Store | Why This Choice |
|---|---|---|
| **Key-Value Data (per node)** | LSM-tree (Memtable + WAL + SSTables) | Optimized for high write throughput, sequential I/O |
| **Cluster Membership & Ring State** | In-memory gossip state + persistent snapshot | Decentralized, no external dependency on hot path |
| **Hinted Handoff Queue** | Local append-only log (per-node) | Durable, ordered, easy to replay |
| **Merkle Trees (anti-entropy)** | In-memory with periodic disk snapshots | Fast comparison, rebuilt from SSTables |
| **Bloom Filters** | In-memory (one per SSTable) | Avoid unnecessary disk reads for non-existent keys |
| **Client-side Cache** | In-process LRU cache in the SDK | Reduce network round-trips for hot keys |

---

## 1. LSM-Tree Storage Engine (Per Node) — The Heart of the System

### Why LSM-Tree over B-Tree?

This is the most important storage decision. Let's compare:

| Aspect | LSM-Tree | B-Tree (e.g., InnoDB, BoltDB) |
|---|---|---|
| **Write pattern** | Sequential append (WAL + memtable flush) | Random I/O (find page, update in place) |
| **Write throughput** | ⭐ Very high (~100K+ writes/sec per node) | Lower (each write = random seek) |
| **Write amplification** | Higher (compaction rewrites data multiple times) | Lower per-write |
| **Read latency** | May check multiple levels (memtable + L0 + L1 + ...) | Single tree traversal — generally faster |
| **Space amplification** | Temporary: multiple versions exist during compaction | Minimal — in-place updates |
| **Range scans** | Efficient within SSTables (sorted) | ⭐ Naturally efficient (tree structure) |
| **Disk I/O pattern** | Sequential reads/writes (SSD + HDD friendly) | Random reads/writes (SSD preferred) |

**Our choice: LSM-Tree** because:
1. Write throughput is the bottleneck at 100K writes/sec per node
2. Sequential I/O is 100-1000x faster than random I/O on spinning disks and significantly faster even on SSDs
3. Used by proven systems: Cassandra (LSM), LevelDB, RocksDB, HBase
4. B-Trees would be better if we were read-heavy with few writes (like a traditional RDBMS)

### LSM-Tree Architecture on Each Node

```
┌─────────────────────────────────────────────────────────────────────┐
│                         Node Storage Engine                         │
│                                                                     │
│  ┌──────────────────────────────────────────────────────────────┐  │
│  │                     WRITE PATH                                │  │
│  │                                                               │  │
│  │   Write Request                                               │  │
│  │       │                                                       │  │
│  │       ├──────────┐                                            │  │
│  │       │          │                                            │  │
│  │       ▼          ▼                                            │  │
│  │  ┌─────────┐  ┌──────────────────────┐                       │  │
│  │  │   WAL   │  │     Memtable         │                       │  │
│  │  │ (Write- │  │  (In-memory sorted   │                       │  │
│  │  │  Ahead  │  │   structure — skip   │                       │  │
│  │  │   Log)  │  │   list or red-black  │                       │  │
│  │  │         │  │   tree)              │                       │  │
│  │  │ Append- │  │                      │                       │  │
│  │  │ only,   │  │  Size: ~64 MB        │                       │  │
│  │  │ on disk │  │  (configurable)      │                       │  │
│  │  └─────────┘  └──────────┬───────────┘                       │  │
│  │                          │ When full (64 MB)                  │  │
│  │                          ▼                                    │  │
│  │                    ┌─────────────┐                            │  │
│  │                    │  Immutable  │  ← Old memtable becomes   │  │
│  │                    │  Memtable   │    read-only while         │  │
│  │                    │  (being     │    flushing to disk        │  │
│  │                    │   flushed)  │                            │  │
│  │                    └──────┬──────┘                            │  │
│  │                           │ Flush to disk                     │  │
│  │                           ▼                                   │  │
│  └───────────────────────────┼───────────────────────────────────┘  │
│                              │                                      │
│  ┌───────────────────────────┼───────────────────────────────────┐  │
│  │                     ON-DISK SSTables                           │  │
│  │                           │                                   │  │
│  │  Level 0 (L0):    ┌──────▼──────┐  ┌────────────┐           │  │
│  │  (unsorted,        │  SSTable 1  │  │ SSTable 2  │  ...      │  │
│  │   may overlap)     │  (newest)   │  │            │           │  │
│  │                    └─────────────┘  └────────────┘           │  │
│  │                           │                                   │  │
│  │                    Compaction (merge + sort + dedup)           │  │
│  │                           │                                   │  │
│  │  Level 1 (L1):    ┌──────▼──────────────────────────────┐   │  │
│  │  (sorted,          │  SSTable (larger, non-overlapping)  │   │  │
│  │   non-overlapping) └─────────────────────────────────────┘   │  │
│  │                           │                                   │  │
│  │                    Compaction                                  │  │
│  │                           │                                   │  │
│  │  Level 2 (L2):    ┌──────▼──────────────────────────────┐   │  │
│  │  (larger)          │  SSTables (even larger)              │   │  │
│  │                    └─────────────────────────────────────┘   │  │
│  │                           │                                   │  │
│  │                    ...continues to Level N                    │  │
│  │                                                               │  │
│  └───────────────────────────────────────────────────────────────┘  │
└─────────────────────────────────────────────────────────────────────┘
```

### Component Deep Dive

#### 1.1 Write-Ahead Log (WAL)

```
Purpose: Durability guarantee — if the node crashes before memtable is flushed,
         data can be recovered by replaying the WAL.

Format: Append-only binary log on disk

Structure:
┌─────────────────────────────────────────────────────────────┐
│ Record 1 │ Record 2 │ Record 3 │ ... │ Record N │           │
├──────────┴──────────┴──────────┴─────┴──────────┴───────────┤
│                                                              │
│  Each record:                                                │
│  ┌──────────┬───────────┬──────┬───────┬──────────┬───────┐ │
│  │ CRC32    │ Timestamp │ Key  │ Value │ TTL      │ Type  │ │
│  │ checksum │ (8 bytes) │ Len  │ Len   │ (8 bytes)│ PUT/  │ │
│  │ (4 bytes)│           │ +Key │ +Value│          │ DELETE│ │
│  └──────────┴───────────┴──────┴───────┴──────────┴───────┘ │
│                                                              │
└──────────────────────────────────────────────────────────────┘

Properties:
- Append-only → sequential I/O → very fast (~microseconds per write)
- CRC32 checksum per record → detects corruption
- Synced to disk (fsync) based on durability config:
  - fsync every write: safest, ~1ms per write (too slow for 100K writes/sec)
  - fsync every N ms (e.g., 10ms): batches syncs, loses at most 10ms of writes on crash
  - fsync on memtable flush: fastest, but can lose entire memtable on crash
- Recommendation: fsync every 10ms (batched) — good balance of durability and performance

Lifecycle:
- New WAL segment created when memtable is full and a new memtable starts
- Old WAL segment is deleted AFTER its corresponding memtable is flushed to SSTable
- Multiple WAL segments may exist during recovery (one per active/immutable memtable)
```

#### 1.2 Memtable

```
Purpose: In-memory write buffer — absorbs writes at memory speed, serves
         recent reads without disk I/O.

Data Structure: Skip List (preferred over Red-Black Tree)

Why Skip List?
┌─────────────────────────────────────────────────────────────────────┐
│                                                                     │
│  Skip List:                                                         │
│  Level 3:  HEAD ──────────────────────────────────── 50 ──── NIL   │
│  Level 2:  HEAD ────────── 20 ──────────────── 50 ──── NIL        │
│  Level 1:  HEAD ── 10 ── 20 ── 30 ── 40 ── 50 ── 60 ── NIL      │
│  Level 0:  HEAD ── 10 ── 20 ── 30 ── 40 ── 50 ── 60 ── NIL      │
│                                                                     │
│  vs. Red-Black Tree:                                                │
│  - Skip list: O(log n) avg for insert/search, lock-free concurrent │
│  - Red-black tree: O(log n) worst case, but rebalancing needs locks│
│                                                                     │
│  Winner: Skip list — better concurrency (lock-free reads),          │
│  simpler implementation, used by LevelDB/RocksDB/Cassandra          │
└─────────────────────────────────────────────────────────────────────┘

Properties:
- Size threshold: 64 MB (configurable)
- When threshold reached:
  1. Current memtable becomes "immutable" (read-only)
  2. New empty memtable created for incoming writes
  3. Background thread flushes immutable memtable to disk as SSTable
- Concurrent access:
  - Writes: single-writer (or CAS-based lock-free inserts)
  - Reads: lock-free — safe to read while another thread writes
- Memory overhead: ~30-50% over raw data (skip list pointers, metadata)

Entry format in memtable:
┌──────────┬───────────────┬──────────┬──────────────┬──────────┐
│ Key      │ Value         │ Timestamp│ TTL/Expiry   │ Type     │
│ (bytes)  │ (bytes)       │ (uint64) │ (uint64)     │ PUT/DEL  │
└──────────┴───────────────┴──────────┴──────────────┴──────────┘
```

#### 1.3 SSTable (Sorted String Table)

```
Purpose: Immutable, sorted, on-disk data file. The primary persistent
         storage format for key-value data.

File Format:
┌─────────────────────────────────────────────────────────────────────┐
│                        SSTable File Layout                          │
│                                                                     │
│  ┌──────────────────────────────────────────────────────────────┐  │
│  │                      DATA BLOCKS                              │  │
│  │                                                               │  │
│  │  Block 0 (4KB):  [KV entry, KV entry, KV entry, ...]        │  │
│  │  Block 1 (4KB):  [KV entry, KV entry, KV entry, ...]        │  │
│  │  Block 2 (4KB):  [KV entry, KV entry, KV entry, ...]        │  │
│  │  ...                                                          │  │
│  │  Block N:        [KV entry, KV entry, ...]                   │  │
│  │                                                               │  │
│  │  Each KV entry:                                               │  │
│  │  ┌──────────┬─────────┬───────┬─────────┬───────┬──────────┐│  │
│  │  │Key Length │Key      │Value  │Value    │Tstamp │TTL/Type  ││  │
│  │  │(varint)  │(bytes)  │Length │(bytes)  │(8B)   │(9B)      ││  │
│  │  └──────────┴─────────┴───────┴─────────┴───────┴──────────┘│  │
│  │                                                               │  │
│  │  Within each block: entries sorted by key (ascending)        │  │
│  │  Blocks may use prefix compression for keys with shared      │  │
│  │  prefixes (saves ~20-40% space)                              │  │
│  └──────────────────────────────────────────────────────────────┘  │
│                                                                     │
│  ┌──────────────────────────────────────────────────────────────┐  │
│  │                      INDEX BLOCK                              │  │
│  │                                                               │  │
│  │  Maps: first key of each data block → block offset            │  │
│  │  ┌─────────────────────┬────────────────┐                    │  │
│  │  │ Block 0 first key   │ Offset: 0      │                    │  │
│  │  │ Block 1 first key   │ Offset: 4096   │                    │  │
│  │  │ Block 2 first key   │ Offset: 8192   │                    │  │
│  │  │ ...                 │ ...            │                    │  │
│  │  └─────────────────────┴────────────────┘                    │  │
│  │                                                               │  │
│  │  Used for binary search to find the right block for a key    │  │
│  └──────────────────────────────────────────────────────────────┘  │
│                                                                     │
│  ┌──────────────────────────────────────────────────────────────┐  │
│  │                      BLOOM FILTER                             │  │
│  │                                                               │  │
│  │  Probabilistic data structure:                                │  │
│  │  - "Key K is definitely NOT in this SSTable" → skip it       │  │
│  │  - "Key K MIGHT be in this SSTable" → check the data blocks  │  │
│  │                                                               │  │
│  │  False positive rate: ~1% with 10 bits per key               │  │
│  │  Memory: 10 bits × keys_in_sstable                           │  │
│  │  For 1M keys per SSTable: ~1.25 MB per bloom filter          │  │
│  └──────────────────────────────────────────────────────────────┘  │
│                                                                     │
│  ┌──────────────────────────────────────────────────────────────┐  │
│  │                      FOOTER                                   │  │
│  │  Index block offset, bloom filter offset, metadata, checksum │  │
│  └──────────────────────────────────────────────────────────────┘  │
└─────────────────────────────────────────────────────────────────────┘

Properties:
- IMMUTABLE: once written, never modified (append-only philosophy)
- Sorted by key: enables binary search and efficient merging
- Typical size: 64-256 MB per SSTable
- Compressed: LZ4 or Snappy block compression (~2-4x compression ratio)
- Each SSTable has exactly one bloom filter (loaded into memory on startup)
```

#### 1.4 Bloom Filters

```
Purpose: Avoid unnecessary disk reads. Before checking an SSTable on disk,
         check the in-memory bloom filter first.

How it works:
┌─────────────────────────────────────────────────────────────────┐
│                                                                 │
│  Bit array:  [0, 0, 1, 0, 1, 0, 0, 1, 0, 1, 0, 0, ...]      │
│               ▲        ▲        ▲                               │
│               │        │        │                               │
│  Key "foo" → hash1() hash2() hash3() → set bits at positions  │
│                                                                 │
│  Lookup "bar" → hash1() hash2() hash3()                        │
│    → If ANY bit is 0 → "bar" is DEFINITELY NOT in this SSTable │
│    → If ALL bits are 1 → "bar" MIGHT be here (false positive)  │
│                                                                 │
│  False positive rate with 10 bits/key and 7 hash functions:     │
│    P(false positive) ≈ 0.82%                                   │
│                                                                 │
└─────────────────────────────────────────────────────────────────┘

Memory sizing:
- 10 bits per key × 10 billion keys (across all nodes)
- Per node (with 100 nodes): ~100M keys × 10 bits = 125 MB per node
- This is loaded entirely into RAM — trivial for a node with 64+ GB RAM

Impact on reads:
- Without bloom filter: read must check ALL SSTables → O(L) disk reads
  where L = number of SSTable levels
- With bloom filter: on average, check only 1-2 SSTables for an existing key
  and ZERO SSTables for a non-existent key (bloom filter says "not here")
- Reduces disk I/O by 10-100x for point lookups
```

---

## 2. Compaction Strategies

Compaction is the process of merging SSTables to remove duplicates, apply tombstones, and reduce the number of files that reads must check.

### Strategy 1: Size-Tiered Compaction (STCS)

```
When there are N SSTables of similar size, merge them into one larger SSTable.

L0: [S1] [S2] [S3] [S4]   ← 4 SSTables of ~64MB each
                │
         Compact when count reaches threshold (e.g., 4)
                │
                ▼
L1: [    S_merged (256MB)    ]

Properties:
- Simple: merge N similarly-sized files into 1
- Good write amplification: each byte is written ~O(log N) times total
- Bad space amplification: during compaction, both old and new SSTables exist
  → temporarily 2x the data
- Bad read performance: SSTables at the same level may have overlapping key ranges
  → a read might need to check multiple SSTables at L0

Best for: Write-heavy workloads where space is cheap and reads are rare
Used by: Cassandra (default), HBase
```

### Strategy 2: Leveled Compaction (LCS)

```
Each level has a size limit. L0 = 10MB, L1 = 100MB, L2 = 1GB, L3 = 10GB, etc.
(Each level is 10x the previous.) SSTables within L1+ are NON-OVERLAPPING.

L0: [S1] [S2] [S3]          ← Small, may overlap (just flushed from memtable)
         │
    When L0 count reaches threshold
         │
         ▼
L1: [SS_a][SS_b][SS_c][SS_d]  ← Non-overlapping! Each covers a distinct key range
         │                       Total size ≤ 100MB
    When L1 total size exceeds limit
         │
         ▼
L2: [SS_1][SS_2][SS_3]...[SS_n]  ← Non-overlapping, total ≤ 1GB
         │
         ▼
L3: [SST_1][SST_2]...[SST_m]     ← Non-overlapping, total ≤ 10GB

Compaction process (L1 → L2):
1. Pick one SSTable from L1 (e.g., SS_b covering keys [M-R])
2. Find all SSTables in L2 that overlap with [M-R]
3. Merge them together → produce new non-overlapping SSTables in L2
4. Delete the old files

Properties:
- Better read performance: at most 1 SSTable per level needs to be checked
  (because non-overlapping) → O(1) per level, O(L) total
- Better space amplification: ~10% overhead (vs 50-100% for STCS)
- Worse write amplification: a single key might be rewritten O(L × 10) times
  as it moves through levels
- More I/O during compaction: frequent small merges vs. infrequent large merges

Best for: Read-heavy or balanced workloads, space-constrained environments
Used by: LevelDB, RocksDB (default)
```

### Our Choice: Leveled Compaction (LCS)

```
Rationale:
1. Our read:write ratio is 10:1 — reads matter more than minimizing write amplification
2. With quorum reads (R=2), each read hits 2 nodes — we want each node's read to be fast
3. Non-overlapping SSTables at each level means point lookups check at most 1 SSTable
   per level + bloom filter = typically 1-2 disk reads total
4. Space amplification of 10% vs 50-100% saves significant disk on a 2TB-per-node cluster

Trade-off accepted: Higher write amplification (~10-30x) means more disk I/O for
background compaction. We mitigate this with:
- SSD storage (compaction I/O is less impactful on SSDs vs HDDs)
- Rate-limiting compaction I/O to avoid starving foreground reads/writes
- Monitoring compaction backlog — if it grows, increase compaction thread pool
```

---

## 3. Tombstone Management & Garbage Collection

### The Tombstone Lifecycle

```
┌──────────────────────────────────────────────────────────────────┐
│                    Tombstone Lifecycle                            │
│                                                                  │
│  T=0: Client sends DELETE(key=K)                                │
│       │                                                          │
│       ▼                                                          │
│  T=0: Tombstone written: {key=K, type=TOMBSTONE, timestamp=T0}  │
│       - Written to WAL + memtable (like any other write)         │
│       - Replicated to N-1 replicas via normal write path         │
│       │                                                          │
│       ▼                                                          │
│  T=0 to T+gc_grace: Tombstone exists in SSTables                │
│       - Any read for key K encounters tombstone → returns 404    │
│       - Read repair propagates tombstone to stale replicas       │
│       - Anti-entropy repair (Merkle trees) propagates tombstone  │
│       │                                                          │
│       ▼                                                          │
│  T+gc_grace (default 10 days): Tombstone eligible for GC        │
│       - During compaction, tombstones older than gc_grace_seconds│
│         are permanently removed                                  │
│       - At this point, all replicas MUST have received the       │
│         tombstone (via read repair or anti-entropy)              │
│       │                                                          │
│       ▼                                                          │
│  After GC: Key K is fully purged from the system                │
│                                                                  │
└──────────────────────────────────────────────────────────────────┘

Why gc_grace_seconds = 10 days?
- If a replica was down for 9 days and comes back, it needs to learn about
  the deletion via anti-entropy repair (Merkle tree sync)
- The tombstone must still exist on other replicas to propagate
- If gc_grace were too short (e.g., 1 hour) and a node was down for 2 hours,
  it would miss the tombstone and "resurrect" the deleted key
- 10 days is a safe default — any node down longer than 10 days should be
  decommissioned and rebuilt anyway

Danger: Tombstone accumulation
- If many keys are deleted but compaction hasn't run, tombstones pile up
- Reads must skip over tombstones → slower reads
- Monitor: tombstone_count_per_read metric
- Mitigation: trigger manual compaction if tombstone ratio exceeds threshold
```

---

## 4. Cluster Metadata Store (Gossip State)

### What Metadata is Stored?

Each node maintains an in-memory view of the entire cluster:

```
ClusterState {
  nodes: Map<NodeId, NodeState>
  ring: ConsistentHashRing
  schema_version: uint64
}

NodeState {
  node_id: "node-b-us-east-1a"
  address: "10.0.1.42:7000"
  status: UP | SUSPECT | DOWN | JOINING | LEAVING
  heartbeat_generation: uint64      // Incremented each time node restarts
  heartbeat_version: uint64         // Incremented every heartbeat interval
  tokens: List<uint128>             // Vnode positions on the ring (256 tokens)
  availability_zone: "us-east-1a"
  rack: "rack-3"
  data_size_gb: 1850
  load: float                       // Relative load metric for rebalancing
  last_updated: timestamp
}
```

### Persistence

```
- Primary: In-memory (for speed)
- Backup: Periodically snapshot to disk (every 30 seconds)
  - File: /data/cluster_state.snapshot
  - On node restart: load from snapshot, then gossip to catch up

- The ring state (which vnodes each node owns) is persisted to a local file
  AND propagated via gossip. On startup, a node:
  1. Loads its own token assignments from local disk
  2. Contacts seed nodes to bootstrap gossip
  3. Receives the full cluster state within a few gossip rounds (~5-10 seconds)
```

### Seed Nodes

```
Every cluster has 3-5 "seed nodes" — well-known nodes that new/restarting
nodes contact first to bootstrap gossip.

Seed nodes are NOT special — they have no extra responsibilities beyond being
the initial contact point. Any node can be a seed.

Configuration (per node):
  seed_nodes: ["10.0.1.1:7000", "10.0.1.2:7000", "10.0.1.3:7000"]

Startup sequence:
1. Node starts → loads local state snapshot
2. Contacts seed nodes → exchanges gossip digests
3. Within O(log N) gossip rounds → has full cluster view
4. Begins serving requests
```

---

## 5. Hinted Handoff Store (Per Node)

### Purpose

When a write's target replica is down, the coordinator stores a "hint" — a deferred write that will be delivered when the target recovers.

### Storage Design

```
Location: /data/hints/{target_node_id}/

File format: Append-only log (similar to WAL)

┌────────────────────────────────────────────────────────────────┐
│  Hint File: /data/hints/node-c/hints_20260702_120000.log      │
│                                                                │
│  ┌──────────┬──────────┬──────┬───────┬──────────┬──────────┐ │
│  │ Target   │ Timestamp│ Key  │ Value │ TTL      │ Hint     │ │
│  │ Node ID  │ (8 bytes)│      │       │          │ Created  │ │
│  │ (string) │          │      │       │          │ At       │ │
│  └──────────┴──────────┴──────┴───────┴──────────┴──────────┘ │
│  ┌──────────┬──────────┬──────┬───────┬──────────┬──────────┐ │
│  │ node-c   │ T1       │ K1   │ V1    │ 86400    │ T1       │ │
│  └──────────┴──────────┴──────┴───────┴──────────┴──────────┘ │
│  ┌──────────┬──────────┬──────┬───────┬──────────┬──────────┐ │
│  │ node-c   │ T2       │ K2   │ V2    │ 0        │ T2       │ │
│  └──────────┴──────────┴──────┴───────┴──────────┴──────────┘ │
│  ...                                                           │
└────────────────────────────────────────────────────────────────┘

Delivery process:
1. Background thread monitors gossip for node-c status changes
2. When node-c status changes from DOWN → UP:
   a. Open hint files for node-c
   b. Stream hints to node-c (batch delivery for efficiency)
   c. node-c applies each hint as a normal write
   d. On successful delivery → delete the hint file
3. If hints are older than max_hint_window (default: 3 hours):
   → Discard them — the node was down too long for hints to be useful
   → Anti-entropy repair will handle full synchronization instead

Properties:
- Hints are stored on disk (durable — survive coordinator restart)
- Hints have a max window (3 hours default) to prevent unbounded growth
- Hints are a best-effort mechanism — NOT a guarantee of consistency
  (that's what anti-entropy repair is for)
- Hints consume disk space — monitor hints_stored_bytes metric

Sizing:
- At 100K writes/sec, if one node is down for 1 hour:
  - Writes destined for that node ≈ 100K/3 × 3600 ≈ 120M hint records
  - At ~5KB per hint: 120M × 5KB ≈ 600 GB of hints
  - This is a lot! Hence the 3-hour max window — limits to ~1.8 TB worst case
  - In practice, most outages are minutes, not hours
```

---

## 6. Merkle Tree Store (Anti-Entropy)

### Purpose

Merkle trees enable efficient comparison of data between replicas to find and fix divergence.

### Storage Design

```
Each node maintains one Merkle tree per vnode (token range) it owns.

Tree structure:
- Depth: 15-20 levels (configurable)
- Leaf nodes: each covers a sub-range of the vnode's key space
- Each leaf: hash of all KV pairs in that sub-range
- Internal nodes: hash of children

Storage:
- In memory for active comparisons
- Rebuilt periodically from SSTables (every 1 hour or on-demand)
- NOT persisted to disk (cheap to rebuild)

Size per tree:
- 2^15 = 32,768 leaf nodes
- Each node: 32 bytes (SHA-256 hash)
- Tree size: ~2 MB per vnode
- Per physical node (256 vnodes): ~512 MB
- Fits comfortably in RAM

Rebuild process:
1. Background thread scans all SSTables for the vnode's key range
2. For each key-value pair, compute hash and assign to leaf bucket
3. Build tree bottom-up
4. Replace the old tree atomically
```

---

## 7. Client-Side Cache

### Purpose

Reduce network round-trips for hot keys. The client SDK caches recent GET responses.

```
Location: In-process (within the client SDK / application)

Data structure: LRU Cache (Least Recently Used)

┌──────────────────────────────────────────────────────────┐
│  Client SDK Cache                                        │
│                                                          │
│  Max size: 100 MB (configurable per-client)              │
│  Eviction: LRU (least recently used)                     │
│  TTL: min(key_ttl, 30 seconds) — never cache longer      │
│       than the key's TTL, and cap at 30s for freshness   │
│                                                          │
│  Cache entry:                                            │
│  ┌──────┬───────┬──────────┬────────────┬──────────────┐ │
│  │ Key  │ Value │ Version  │ Fetched_At │ Expiry       │ │
│  └──────┴───────┴──────────┴────────────┴──────────────┘ │
│                                                          │
│  Cache behavior:                                         │
│  - GET with consistency=ONE: check cache first           │
│    → cache hit: return immediately (0 network calls)     │
│    → cache miss: fetch from cluster, populate cache      │
│  - GET with consistency=QUORUM or ALL: BYPASS cache      │
│    → always fetch from cluster (strong consistency)      │
│  - PUT/DELETE: invalidate the cached entry               │
│                                                          │
│  Why not cache for QUORUM?                               │
│  - QUORUM reads guarantee freshness by contacting        │
│    multiple replicas. A cached value defeats this.       │
│  - ONE reads already accept staleness — caching is       │
│    consistent with that tradeoff.                        │
│                                                          │
└──────────────────────────────────────────────────────────┘
```

---

## 8. Storage Sizing Summary

### Per-Node Storage Breakdown

| Component | Size | Location | Notes |
|---|---|---|---|
| **Key-Value SSTables** | ~2 TB | Disk (SSD) | 50TB / 100 nodes × 3 replicas ≈ 1.5TB primary + replicas |
| **WAL** | ~128 MB | Disk (SSD) | 2 segments × 64 MB each |
| **Memtable (active)** | 64 MB | RAM | Current write buffer |
| **Memtable (immutable)** | 64 MB | RAM | Being flushed to disk |
| **Bloom Filters** | ~125 MB | RAM | 10 bits per key × ~100M keys per node |
| **SSTable Index Blocks** | ~200 MB | RAM | Loaded on startup for fast block lookup |
| **Merkle Trees** | ~512 MB | RAM | 256 vnodes × 2 MB per tree |
| **Gossip State** | ~10 MB | RAM | Cluster membership for 100 nodes |
| **Hint Store** | 0 - 1.8 TB | Disk (SSD) | Depends on cluster health — 0 when healthy |
| **Compaction temp space** | ~200 GB | Disk (SSD) | Temporary during compaction |

### Per-Node Hardware Recommendation

```
CPU:     16-32 cores (compaction is CPU-intensive for compression)
RAM:     64 GB minimum
         - 1 GB: memtables + WAL buffers
         - 125 MB: bloom filters
         - 200 MB: SSTable index blocks
         - 512 MB: Merkle trees
         - 10 MB: gossip state
         - ~30 GB: OS page cache (caches frequently accessed SSTable blocks)
         - ~30 GB: headroom for GC, connections, etc.

Disk:    4 TB NVMe SSD
         - 2 TB: SSTable data
         - 200 GB: compaction headroom
         - 128 MB: WAL
         - Up to 1.8 TB: hint store (worst case)
         - Remaining: headroom for growth

Network: 10 Gbps (replication, streaming, client traffic)
```

### Cluster-Wide Totals

| Metric | Value |
|---|---|
| **Nodes** | 100 |
| **Total raw data** | 50 TB |
| **Total with replication (3x)** | 150 TB |
| **Total disk provisioned** | 400 TB (4 TB × 100 nodes) |
| **Total RAM** | 6.4 TB (64 GB × 100 nodes) |
| **Bloom filters (cluster)** | ~12.5 GB |
| **Effective disk utilization** | ~37.5% (150 TB / 400 TB) — headroom for compaction + growth |

---

## Trade-Off Discussions

### SQL vs. NoSQL for the Core Store

| Aspect | SQL (e.g., sharded PostgreSQL) | NoSQL (LSM-based, custom) |
|---|---|---|
| **Data model** | Rich: tables, joins, indexes | Minimal: key → value (all we need) |
| **Write throughput** | Limited by B-tree + WAL overhead | ⭐ Very high (LSM sequential writes) |
| **Consistency model** | Strong (ACID by default) | Tunable (our requirement) |
| **Horizontal scaling** | Painful (sharding is bolted on) | ⭐ Native (consistent hashing, vnodes) |
| **Operational overhead** | High (schema migrations, connection pools) | Lower (schemaless, simpler ops model) |
| **Query flexibility** | ⭐ Rich SQL queries | Limited (point lookups only) |

**Verdict**: Custom LSM-based engine. We don't need SQL features, and the write throughput + horizontal scaling requirements demand it.

### In-Memory (Redis-like) vs. Disk-Backed (Our Choice)

| Aspect | In-Memory | Disk-Backed (LSM) |
|---|---|---|
| **Read latency** | ⭐ Sub-millisecond | ~1-5 ms (with bloom filters + page cache) |
| **Write durability** | Weak (lose data on crash unless AOF/RDB) | ⭐ Strong (WAL + fsync) |
| **Data size** | Limited by RAM (expensive at 50TB) | ⭐ Scales to petabytes on disk |
| **Cost per GB** | ~$5-10/GB (RAM) | ~$0.10-0.50/GB (SSD) |
| **50 TB cost** | ~$250K-500K (RAM alone) | ~$5K-25K (SSD) — 10-100x cheaper |

**Verdict**: Disk-backed with aggressive caching. The 50TB data size makes pure in-memory prohibitively expensive. We get near-in-memory performance for hot keys via bloom filters + OS page cache + client-side cache.

### Embedded Engine (RocksDB) vs. Custom Implementation

| Aspect | Use RocksDB | Build Custom |
|---|---|---|
| **Development effort** | ⭐ Minimal (mature library) | High (years of engineering) |
| **Tunability** | Good (many knobs) | ⭐ Full control |
| **Bug surface** | Well-tested (used by many) | New, untested |
| **Performance** | Excellent general-purpose | Can be optimized for our exact workload |
| **Dependency risk** | External dependency (Facebook/Meta) | No external dependency |

**Verdict for a real system**: Use RocksDB (or a fork like Speedb). No reason to build a storage engine from scratch — RocksDB is battle-tested and used by CockroachDB, TiDB, Cassandra (via RocksEngine). 

**In the interview context**: We discuss the LSM-tree concepts to demonstrate understanding, but in practice we'd use RocksDB as the embedded storage engine per node.

---

*This datastore design document complements the [interview simulation](interview-simulation.md) and [API contracts](api-contracts.md).*