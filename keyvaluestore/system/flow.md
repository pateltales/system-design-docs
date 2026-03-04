# Distributed Key-Value Store — System Flows

> This document depicts and explains all the major flows in the distributed key-value store system. Each flow includes a sequence diagram, step-by-step breakdown, and discussion of edge cases.

---

## Table of Contents

1. [Write Flow (PUT)](#1-write-flow-put)
2. [Read Flow (GET)](#2-read-flow-get)
3. [Delete Flow](#3-delete-flow)
4. [Read Repair Flow](#4-read-repair-flow)
5. [Hinted Handoff Flow](#5-hinted-handoff-flow)
6. [Anti-Entropy Repair (Merkle Tree Sync)](#6-anti-entropy-repair-merkle-tree-sync)
7. [Node Join Flow](#7-node-join-flow)
8. [Node Leave / Decommission Flow](#8-node-leave--decommission-flow)
9. [Node Failure Detection (Gossip)](#9-node-failure-detection-gossip)
10. [TTL Expiry Flow](#10-ttl-expiry-flow)
11. [Compaction Flow](#11-compaction-flow)
12. [Conflict Resolution Flow](#12-conflict-resolution-flow)

---

## 1. Write Flow (PUT)

### Happy Path — All Replicas Healthy, QUORUM Consistency

```
Client SDK              Coordinator (Node B)         Node A (Replica)       Node C (Replica)
    │                         │                           │                       │
    │  1. PUT(K, V, TTL=24h)  │                           │                       │
    │  consistency=QUORUM     │                           │                       │
    │────────────────────────▶│                           │                       │
    │                         │                           │                       │
    │                         │  2. Hash(K) → ring pos    │                       │
    │                         │     Preference list:      │                       │
    │                         │     [B, A, C]             │                       │
    │                         │                           │                       │
    │                         │  3. Write locally:        │                       │
    │                         │     a. Append to WAL      │                       │
    │                         │     b. Insert into        │                       │
    │                         │        memtable           │                       │
    │                         │     c. Assign timestamp   │                       │
    │                         │        T1 (or vector      │                       │
    │                         │        clock entry)       │                       │
    │                         │                           │                       │
    │                         │  4. Forward write to      │                       │
    │                         │     replicas (parallel)   │                       │
    │                         │──────────────────────────▶│                       │
    │                         │───────────────────────────────────────────────────▶│
    │                         │                           │                       │
    │                         │                           │  5a. Node A:          │
    │                         │                           │  Append to WAL        │
    │                         │                           │  Insert into memtable │
    │                         │  6. ACK from Node A       │                       │
    │                         │◀──────────────────────────│                       │
    │                         │                           │                       │
    │                         │  QUORUM MET!              │                       │
    │                         │  (self + A = 2 of 3)      │                       │
    │                         │                           │                       │
    │  7. 200 OK              │                           │                       │
    │  { version: v_T1_B1,    │                           │                       │
    │    consistency: QUORUM } │                           │                       │
    │◀────────────────────────│                           │                       │
    │                         │                           │                       │
    │                         │                           │       5b. Node C:     │
    │                         │                           │       (arrives late)  │
    │                         │                           │       Append to WAL   │
    │                         │                           │       Insert memtable │
    │                         │  8. ACK from Node C       │                       │
    │                         │◀──────────────────────────────────────────────────│
    │                         │  (async — client already  │                       │
    │                         │   got response)           │                       │
```

### Step-by-Step Breakdown

| Step | Component | Action | Latency |
|------|-----------|--------|---------|
| 1 | Client SDK | Hashes key K → determines Node B is coordinator. Sends PUT request. | ~0.1ms (hash) |
| 2 | Coordinator | Computes preference list from the ring: [B, A, C] | ~0.01ms (in-memory lookup) |
| 3 | Coordinator | Writes to local WAL (fsync batched every 10ms) + memtable (skip list insert) | ~0.5ms |
| 4 | Coordinator | Sends write to A and C in parallel via gRPC | ~0.1ms (network send) |
| 5a | Node A | Receives write, appends to WAL + memtable | ~0.5ms |
| 6 | Coordinator | Receives ACK from A. Quorum (W=2) met: self + A = 2. | ~1ms network RTT |
| 7 | Coordinator | Returns 200 OK to client | ~0.1ms |
| 8 | Node C | ACK arrives asynchronously. Not waited on. | ~1-5ms |

**Total client-perceived latency: ~2-5ms** (dominated by network RTT to fastest replica)

---

## 2. Read Flow (GET)

### Happy Path — All Replicas Consistent, QUORUM

```
Client SDK              Coordinator (Node B)         Node A (Replica)       Node C (Replica)
    │                         │                           │                       │
    │  1. GET(K)              │                           │                       │
    │  consistency=QUORUM     │                           │                       │
    │────────────────────────▶│                           │                       │
    │                         │                           │                       │
    │                         │  2. Hash(K) → ring pos    │                       │
    │                         │     Preference list:      │                       │
    │                         │     [B, A, C]             │                       │
    │                         │                           │                       │
    │                         │  3. Read locally:         │                       │
    │                         │     a. Check memtable     │                       │
    │                         │     b. Check bloom filters│                       │
    │                         │     c. Read SSTable if    │                       │
    │                         │        bloom says "maybe" │                       │
    │                         │     Result: V2, T2        │                       │
    │                         │                           │                       │
    │                         │  4. Send read to replicas │                       │
    │                         │     (parallel)            │                       │
    │                         │──────────────────────────▶│                       │
    │                         │───────────────────────────────────────────────────▶│
    │                         │                           │                       │
    │                         │  5. Node A responds:      │                       │
    │                         │     V2, T2                │                       │
    │                         │◀──────────────────────────│                       │
    │                         │                           │                       │
    │                         │  QUORUM MET!              │                       │
    │                         │  (self + A = 2 of 3)      │                       │
    │                         │                           │                       │
    │                         │  6. Compare versions:     │                       │
    │                         │     B has V2,T2           │                       │
    │                         │     A has V2,T2           │                       │
    │                         │     → Consistent!         │                       │
    │                         │                           │                       │
    │  7. 200 OK              │                           │                       │
    │  Value: V2              │                           │                       │
    │  Version: v_T2          │                           │                       │
    │◀────────────────────────│                           │                       │
    │                         │                           │                       │
    │                         │  8. Node C responds:      │                       │
    │                         │     V1, T1 (STALE!)       │                       │
    │                         │◀──────────────────────────────────────────────────│
    │                         │                           │                       │
    │                         │  9. READ REPAIR:          │                       │
    │                         │     Send V2,T2 to Node C  │                       │
    │                         │     (async, background)   │                       │
    │                         │───────────────────────────────────────────────────▶│
    │                         │                           │                       │
```

### Local Read Path (Step 3 — Within a Single Node)

```
┌───────────────────────────────────────────────────────────┐
│                     Node B: Local Read                     │
│                                                           │
│  GET key=K                                                │
│      │                                                    │
│      ▼                                                    │
│  ┌─────────────────┐                                     │
│  │  1. Active       │  Found?                            │
│  │     Memtable     │──YES──▶ Return value (newest)      │
│  └────────┬────────┘                                     │
│           │ NOT FOUND                                     │
│           ▼                                               │
│  ┌─────────────────┐                                     │
│  │  2. Immutable    │  Found?                            │
│  │     Memtable     │──YES──▶ Return value               │
│  │     (if exists)  │                                    │
│  └────────┬────────┘                                     │
│           │ NOT FOUND                                     │
│           ▼                                               │
│  ┌─────────────────────────────────────────────────────┐ │
│  │  3. SSTables (newest to oldest)                      │ │
│  │                                                      │ │
│  │  For each SSTable level (L0, L1, L2, ...):          │ │
│  │    a. Check Bloom Filter                             │ │
│  │       ├── "Definitely NOT here" → skip this SSTable  │ │
│  │       └── "Maybe here" → continue to step b          │ │
│  │    b. Binary search in Index Block                   │ │
│  │       → Find the data block containing key K         │ │
│  │    c. Read data block from disk (or page cache)      │ │
│  │    d. Binary search within data block                │ │
│  │       ├── Found → Return value                       │ │
│  │       └── Not found → try next SSTable               │ │
│  │                                                      │ │
│  │  Optimization: For Leveled Compaction, L1+ SSTables  │ │
│  │  are non-overlapping, so at most 1 SSTable per level │ │
│  │  can contain key K.                                  │ │
│  └──────────────────────────────────────────────────────┘ │
│           │                                               │
│           │ NOT FOUND in any SSTable                      │
│           ▼                                               │
│      Return KEY_NOT_FOUND (404)                          │
│                                                           │
└───────────────────────────────────────────────────────────┘

Typical read performance:
- Memtable hit:     ~0.01 ms (in-memory, skip list lookup)
- L0 SSTable hit:   ~0.1 ms  (bloom filter check + page cache hit)
- L1+ SSTable hit:  ~0.5 ms  (bloom filter + 1 disk read if not cached)
- Key not found:    ~0.05 ms (all bloom filters say "definitely not here")
```

---

## 3. Delete Flow

```
Client SDK              Coordinator (Node B)         Node A (Replica)       Node C (Replica)
    │                         │                           │                       │
    │  DELETE(K)              │                           │                       │
    │  consistency=QUORUM     │                           │                       │
    │────────────────────────▶│                           │                       │
    │                         │                           │                       │
    │                         │  1. Create TOMBSTONE:     │                       │
    │                         │     {key=K,               │                       │
    │                         │      type=TOMBSTONE,      │                       │
    │                         │      timestamp=T3}        │                       │
    │                         │                           │                       │
    │                         │  2. Write tombstone to    │                       │
    │                         │     local WAL + memtable  │                       │
    │                         │     (same as a PUT)       │                       │
    │                         │                           │                       │
    │                         │  3. Forward tombstone to  │                       │
    │                         │     replicas (parallel)   │                       │
    │                         │──────────────────────────▶│                       │
    │                         │───────────────────────────────────────────────────▶│
    │                         │                           │                       │
    │                         │  4. ACK from Node A       │                       │
    │                         │◀──────────────────────────│                       │
    │                         │  QUORUM MET               │                       │
    │                         │                           │                       │
    │  5. 200 OK {deleted:    │                           │                       │
    │     true}               │                           │                       │
    │◀────────────────────────│                           │                       │
    │                         │                           │                       │
    │                         │         ┌─────────────────────────────────────┐   │
    │                         │         │  Tombstone lifecycle:                │   │
    │                         │         │  - Exists for gc_grace_seconds      │   │
    │                         │         │    (default 10 days)                │   │
    │                         │         │  - Propagated via read repair and   │   │
    │                         │         │    anti-entropy sync                │   │
    │                         │         │  - Garbage collected during         │   │
    │                         │         │    compaction after gc_grace expires│   │
    │                         │         └─────────────────────────────────────┘   │
```

**Why a tombstone and not a real delete?**
- SSTables are immutable — can't modify them in place
- Without tombstones, a stale replica that missed the delete would "resurrect" the key during read repair
- The tombstone is the "proof of deletion" that propagates to all replicas

---

## 4. Read Repair Flow

Read repair is triggered **during a normal read** when the coordinator detects that replicas have divergent data.

```
                    Coordinator (Node B)         Node A              Node C
                          │                        │                    │
  (During a QUORUM read)  │                        │                    │
                          │                        │                    │
  B has: K=V2, T2         │                        │                    │
                          │  Read response:        │                    │
                          │  K=V2, T2              │                    │
                          │◀───────────────────────│                    │
                          │                        │                    │
  QUORUM met: return V2   │                        │                    │
  to client               │                        │                    │
                          │  Read response:        │                    │
                          │  K=V1, T1 (STALE!)     │                    │
                          │◀───────────────────────────────────────────│
                          │                        │                    │
  Detect: T1 < T2         │                        │                    │
  → Node C is stale       │                        │                    │
                          │                        │                    │
  READ REPAIR (async):    │                        │                    │
  Send {K=V2, T2} to C    │                        │                    │
                          │────────────────────────────────────────────▶│
                          │                        │                    │
                          │                        │   Node C applies   │
                          │                        │   the newer value  │
                          │                        │   K=V2, T2         │
                          │        ACK             │                    │
                          │◀───────────────────────────────────────────│
                          │                        │                    │

Result: All 3 replicas now have K=V2, T2 ✓
```

**Key properties:**
- Read repair is **asynchronous** — it doesn't delay the client response
- It's **opportunistic** — only triggered on actual reads, not background
- It's **probabilistic** — only fixes keys that are read; rarely-read keys may remain divergent
- For full consistency, we rely on **anti-entropy repair** (see Flow #6)

---

## 5. Hinted Handoff Flow

Triggered when a replica node is down during a write.

```
Phase 1: Write with node down
─────────────────────────────

Client SDK         Coordinator (B)      Node A (UP)      Node C (DOWN ✗)    Node D (hint holder)
    │                    │                  │                   ✗                   │
    │ PUT(K,V)           │                  │                   ✗                   │
    │ QUORUM             │                  │                   ✗                   │
    │───────────────────▶│                  │                   ✗                   │
    │                    │                  │                   ✗                   │
    │                    │ Write locally ✓  │                   ✗                   │
    │                    │                  │                   ✗                   │
    │                    │ Forward to A ───▶│ Write ✓           ✗                   │
    │                    │ Forward to C ────────────────────────✗── TIMEOUT         │
    │                    │◀─────────────────│ ACK               ✗                   │
    │                    │                  │                   ✗                   │
    │                    │ QUORUM MET       │                   ✗                   │
    │                    │ (B + A = 2)      │                   ✗                   │
    │ 200 OK ◀───────────│                  │                   ✗                   │
    │                    │                  │                   ✗                   │
    │                    │ Store hint for C │                   ✗                   │
    │                    │ on Node D ──────────────────────────────────────────────▶│
    │                    │                  │                   ✗  Hint stored:     │
    │                    │                  │                   ✗  {target: C,      │
    │                    │                  │                   ✗   key: K,         │
    │                    │                  │                   ✗   value: V,       │
    │                    │                  │                   ✗   timestamp: T1}  │


Phase 2: Node C recovers — hints delivered
──────────────────────────────────────────

                                                            Node C (BACK UP ✓)    Node D
                                                                  │                   │
  Gossip: Node C status changes from DOWN → UP                   │                   │
                                                                  │                   │
  Node D detects C is back (gossip)                               │                   │
                                                                  │  Stream hints     │
                                                                  │  for Node C       │
                                                                  │◀──────────────────│
                                                                  │                   │
  Node C applies each hint as a normal write:                     │                   │
  - Append to WAL                                                 │                   │
  - Insert into memtable                                          │                   │
                                                                  │  ACK              │
                                                                  │──────────────────▶│
                                                                  │                   │
                                                                  │  Delete hint file │
                                                                  │                   │
  Result: Node C now has K=V, T1 ✓                                │                   │
  Full replication restored!                                      │                   │
```

---

## 6. Anti-Entropy Repair (Merkle Tree Sync)

Background process that detects and fixes **all** divergence between replicas — not just the keys that are read (unlike read repair).

```
Phase 1: Merkle Tree Comparison
───────────────────────────────

Node A                                                          Node B
  │                                                               │
  │  Both nodes maintain Merkle trees for shared vnode ranges     │
  │                                                               │
  │  Node A's tree:              Node B's tree:                  │
  │       ROOT_A                      ROOT_B                      │
  │      /      \                    /      \                     │
  │    H1_A    H2_A               H1_B    H2_B                   │
  │   / \      / \               / \      / \                    │
  │  h1  h2  h3  h4            h1  h2  h3  h4                   │
  │  ✓   ✗   ✓   ✓             ✓   ✗   ✓   ✓                    │
  │     (h2 differs!)              (h2 differs!)                  │
  │                                                               │
  │  1. Exchange root hashes                                      │
  │────────────────────────────────────────────────────────────▶ │
  │  ROOT_A hash                                                  │
  │◀────────────────────────────────────────────────────────────  │
  │  ROOT_B hash                                                  │
  │                                                               │
  │  ROOT_A ≠ ROOT_B → trees differ!                             │
  │                                                               │
  │  2. Exchange Level 1 hashes                                   │
  │────────────────────────────────────────────────────────────▶ │
  │  [H1_A, H2_A]                                                │
  │◀────────────────────────────────────────────────────────────  │
  │  [H1_B, H2_B]                                                │
  │                                                               │
  │  H1_A == H1_B ✓ (left subtree matches — skip!)              │
  │  H2_A ≠ H2_B ✗ (right subtree differs — drill down)        │
  │                                                               │
  │  3. Exchange Level 2 hashes for right subtree                │
  │────────────────────────────────────────────────────────────▶ │
  │  [h3_A, h4_A]                                                 │
  │◀────────────────────────────────────────────────────────────  │
  │  [h3_B, h4_B]                                                 │
  │                                                               │
  │  h3_A == h3_B ✓ (matches)                                   │
  │  h4_A ≠ h4_B ✗ (h4 leaf range has divergent keys)          │
  │                                                               │


Phase 2: Key Exchange for Divergent Range
─────────────────────────────────────────

Node A                                                          Node B
  │                                                               │
  │  4. Exchange actual keys+values in h4's range                │
  │                                                               │
  │  Node A sends keys in h4 range:                              │
  │  [{K1: V_A1, T1}, {K2: V_A2, T2}, {K3: V_A3, T3}]         │
  │────────────────────────────────────────────────────────────▶ │
  │                                                               │
  │  Node B sends keys in h4 range:                              │
  │  [{K1: V_B1, T1}, {K2: V_B2, T5}, {K4: V_B4, T4}]         │
  │◀────────────────────────────────────────────────────────────  │
  │                                                               │
  │  5. Both nodes reconcile:                                     │
  │                                                               │
  │  K1: A has T1, B has T1 → same ✓                             │
  │  K2: A has T2, B has T5 → B is newer → A applies V_B2       │
  │  K3: A has T3, B missing → B applies V_A3                    │
  │  K4: A missing, B has T4 → A applies V_B4                    │
  │                                                               │
  │  6. Both nodes are now consistent for this range ✓           │
  │                                                               │

Efficiency:
- Total data transferred: O(log N) hashes + divergent keys only
- For a tree with 1M keys where 10 keys diverge:
  - ~20 hashes exchanged (tree traversal)
  - 10 actual KV pairs transferred
  - vs. 1M KV pairs without Merkle trees → 100,000x more efficient
```

---

## 7. Node Join Flow

When a new node joins the cluster.

```
                    New Node (D)          Seed Node (A)        Existing Nodes (B, C)
                        │                      │                       │
  1. Startup            │                      │                       │
     Load config:       │                      │                       │
     seed_nodes=[A]     │                      │                       │
                        │                      │                       │
  2. Contact seed       │                      │                       │
     for gossip         │                      │                       │
     bootstrap          │                      │                       │
                        │  GOSSIP_HELLO        │                       │
                        │─────────────────────▶│                       │
                        │                      │                       │
                        │  GOSSIP_STATE        │                       │
                        │  (full cluster map)  │                       │
                        │◀─────────────────────│                       │
                        │                      │                       │
  3. D learns about     │                      │                       │
     all existing       │                      │                       │
     nodes and their    │                      │                       │
     token assignments  │                      │                       │
                        │                      │                       │
  4. D calculates its   │                      │                       │
     token assignments  │                      │                       │
     (256 vnodes)       │                      │                       │
     Strategy: take     │                      │                       │
     tokens from most   │                      │                       │
     loaded nodes       │                      │                       │
                        │                      │                       │
  5. D announces        │                      │                       │
     JOINING status     │                      │                       │
     via gossip         │                      │                       │
                        │──GOSSIP──────────────▶│──GOSSIP──────────────▶│
                        │  {D: JOINING,        │                       │
                        │   tokens: [...]}     │                       │
                        │                      │                       │
  6. Data streaming     │                      │                       │
     begins             │                      │                       │
                        │                      │                       │
     For each token     │                      │                       │
     range D takes      │                      │                       │
     from existing      │                      │                       │
     owners:            │                      │                       │
                        │  STREAM_DATA         │                       │
                        │  (KV pairs for       │                       │
                        │   token range X)     │                       │
                        │◀─────────────────────│                       │
                        │                      │                       │
                        │  STREAM_DATA         │                       │
                        │  (KV pairs for       │                       │
                        │   token range Y)     │                       │
                        │◀─────────────────────────────────────────────│
                        │                      │                       │
  7. During streaming:  │                      │                       │
     - Reads: old owner │                      │                       │
       still serves     │                      │                       │
     - Writes: go to    │                      │                       │
       old owner (or    │                      │                       │
       dual-written)    │                      │                       │
                        │                      │                       │
  8. Streaming complete │                      │                       │
     D changes status   │                      │                       │
     to NORMAL          │                      │                       │
                        │──GOSSIP──────────────▶│──GOSSIP──────────────▶│
                        │  {D: NORMAL}         │                       │
                        │                      │                       │
  9. D starts serving   │                      │                       │
     reads and writes   │                      │                       │
     for its token      │                      │                       │
     ranges             │                      │                       │
                        │                      │                       │
  10. Old owners delete │                      │                       │
      streamed data     │                      │                       │
      (eventually, via  │                      │                       │
      compaction)       │                      │                       │


Timeline:
─────────────────────────────────────────────────────────────────────
0 min      5 min        30 min              45 min
│          │             │                   │
▼          ▼             ▼                   ▼
D starts   Gossip        Streaming           D goes
           converges     in progress         NORMAL
           (cluster      (background,        (serving
           knows D)      throttled)          traffic)
```

**Key points:**
- Zero downtime — existing nodes continue serving traffic during the join
- Data streaming is throttled to avoid overwhelming the cluster's I/O bandwidth
- ~25% of data moves when going from 3 to 4 nodes (proportional, not total)

---

## 8. Node Leave / Decommission Flow

```
                    Leaving Node (C)       Remaining Nodes (A, B, D)
                        │                       │
  1. Operator sends     │                       │
     decommission       │                       │
     command            │                       │
                        │                       │
  2. C changes status   │                       │
     to LEAVING         │                       │
                        │──GOSSIP──────────────▶│
                        │  {C: LEAVING}         │
                        │                       │
  3. C streams ALL its  │                       │
     data to the next   │                       │
     owners on the ring │                       │
                        │                       │
     For each vnode C   │                       │
     owns:              │                       │
     - Determine next   │                       │
       owner on ring    │                       │
     - Stream all KV    │                       │
       pairs            │                       │
                        │  STREAM_DATA ────────▶│
                        │  (token range X       │
                        │   → Node A)           │
                        │                       │
                        │  STREAM_DATA ────────▶│
                        │  (token range Y       │
                        │   → Node D)           │
                        │                       │
  4. During streaming:  │                       │
     C still serves     │                       │
     reads/writes for   │                       │
     its ranges         │                       │
                        │                       │
  5. Streaming complete │                       │
     C releases tokens  │                       │
                        │──GOSSIP──────────────▶│
                        │  {C: DECOMMISSIONED,  │
                        │   tokens: []}         │
                        │                       │
  6. C shuts down       │                       │
     (can be removed    │                       │
     from the cluster)  │                       │
                        │                       │
  7. Remaining nodes    │                       │
     update ring to     │                       │
     exclude C          │                       │
```

---

## 9. Node Failure Detection (Gossip)

```
Every 1 second, each node runs a gossip round:

Node A                Node B              Node C              Node D
  │                      │                   │                   │
  │  Pick random peer:   │                   │                   │
  │  Node C              │                   │                   │
  │                      │                   │                   │
  │  GOSSIP_DIGEST:      │                   │                   │
  │  [(A,hb=1042,UP),   │                   │                   │
  │   (B,hb=887,UP),    │                   │                   │
  │   (C,hb=512,UP),    │                   │                   │
  │   (D,hb=330,UP)]    │                   │                   │
  │──────────────────────────────────────────▶│                   │
  │                      │                   │                   │
  │                      │                   │  Compare with     │
  │                      │                   │  local state:     │
  │                      │                   │  A: hb=1040 < 1042│
  │                      │                   │  → A has newer    │
  │                      │                   │  info about A     │
  │                      │                   │                   │
  │  GOSSIP_ACK:         │                   │                   │
  │  "I have newer info  │                   │                   │
  │   for D: hb=335"     │                   │                   │
  │◀─────────────────────────────────────────│                   │
  │                      │                   │                   │
  │  Update local state: │                   │                   │
  │  D.hb = 335          │                   │                   │
  │                      │                   │                   │

Convergence: After O(log N) rounds (~7 rounds for 100 nodes),
all nodes have consistent state.


FAILURE DETECTION (Phi Accrual Failure Detector):
─────────────────────────────────────────────────

Timeline for Node C going down:

T=0s:     Node C's last heartbeat received by others
T=1s:     No heartbeat from C (Phi = 1.2, threshold = 8)  → still UP
T=2s:     No heartbeat from C (Phi = 2.5)                 → still UP
T=5s:     No heartbeat from C (Phi = 5.8)                 → still UP
T=8s:     No heartbeat from C (Phi = 8.3 > threshold!)    → SUSPECT ⚠️
T=15s:    Still no heartbeat (Phi = 15.0)                  → confirmed DOWN ✗
T=15s+:   Other nodes notified via gossip                  → hinted handoff activates

The Phi Accrual Failure Detector adapts to network conditions:
- On a fast, stable network: phi threshold crossed quickly → fast detection
- On a slow, noisy network: phi increases slowly → fewer false positives
- Much better than a fixed timeout (e.g., "10 seconds = dead")
```

---

## 10. TTL Expiry Flow

```
Two mechanisms work together: lazy expiry (on read) and proactive cleanup (background).


Mechanism 1: Lazy Expiry (on read)
──────────────────────────────────

Client SDK              Coordinator (Node B)
    │                         │
    │  GET(K)                 │
    │────────────────────────▶│
    │                         │
    │                         │  Read from storage:
    │                         │  {key=K, value=V,
    │                         │   expires_at=1720051200000}
    │                         │
    │                         │  Check: current_time > expires_at?
    │                         │  1720137600000 > 1720051200000?
    │                         │  YES → key is expired!
    │                         │
    │  404 KEY_NOT_FOUND      │
    │◀────────────────────────│
    │                         │
    │                         │  (Optionally: write tombstone
    │                         │   to prevent future reads from
    │                         │   checking storage)


Mechanism 2: Proactive TTL Scanner (background)
────────────────────────────────────────────────

┌──────────────────────────────────────────────────────────┐
│  Background TTL Scanner Thread (per node)                 │
│                                                          │
│  Runs every: 60 seconds (configurable)                   │
│                                                          │
│  1. Scan memtable for entries where                      │
│     current_time > expires_at                            │
│     → Write tombstones for expired entries               │
│                                                          │
│  2. During compaction (piggyback):                       │
│     When merging SSTables, check each entry:             │
│     - If current_time > expires_at → drop the entry      │
│       (don't write it to the output SSTable)             │
│     - This is the most efficient cleanup path            │
│                                                          │
│  Note: We do NOT scan all SSTables proactively           │
│  (too expensive). We rely on:                            │
│  - Lazy expiry on reads (instant)                        │
│  - Compaction (eventual — cleans up during merge)        │
│  - Memtable scanner (catches recently expired keys)      │
│                                                          │
└──────────────────────────────────────────────────────────┘


Combined TTL Flow:

    T=0:      Key K written with TTL=24h
              expires_at = T + 86400s
    
    T=12h:    Key K is read → not expired → return value ✓
    
    T=25h:    Key K is read (lazy expiry):
              current_time > expires_at → 404
    
    T=48h:    Compaction runs → encounters K with expired TTL
              → discards K from output SSTable
              → Key K permanently removed from disk ✓
```

---

## 11. Compaction Flow

```
Leveled Compaction (L0 → L1):
─────────────────────────────

┌────────────────────────────────────────────────────────────────────┐
│  Background Compaction Thread                                      │
│                                                                    │
│  Trigger: L0 has 4+ SSTables (threshold)                          │
│                                                                    │
│  L0 (before):  [SS1: A-Z] [SS2: D-P] [SS3: M-Z] [SS4: A-F]     │
│                 (overlapping key ranges!)                          │
│                                                                    │
│  L1 (before):  [SS_a: A-D] [SS_b: E-L] [SS_c: M-R] [SS_d: S-Z] │
│                 (non-overlapping ✓)                                │
│                                                                    │
│  Step 1: Select all L0 SSTables (they may overlap)                │
│                                                                    │
│  Step 2: Find overlapping L1 SSTables                             │
│          L0 covers A-Z → all L1 SSTables overlap                  │
│                                                                    │
│  Step 3: Multi-way merge sort                                     │
│                                                                    │
│  Input files:  SS1, SS2, SS3, SS4 (L0) + SS_a, SS_b, SS_c, SS_d │
│                                                                    │
│  ┌─────────┐  ┌─────────┐  ┌─────────┐  ┌─────────┐            │
│  │   SS1   │  │   SS2   │  │   SS3   │  │   SS4   │  (L0)     │
│  └────┬────┘  └────┬────┘  └────┬────┘  └────┬────┘            │
│       │            │            │            │                    │
│  ┌────┴────┐  ┌────┴────┐  ┌────┴────┐  ┌────┴────┐            │
│  │  SS_a   │  │  SS_b   │  │  SS_c   │  │  SS_d   │  (L1)     │
│  └────┬────┘  └────┬────┘  └────┬────┘  └────┬────┘            │
│       │            │            │            │                    │
│       └────────────┴────────────┴────────────┘                    │
│                           │                                        │
│                    Multi-way merge                                  │
│                    - For duplicate keys: keep newest version       │
│                    - For tombstones: keep if < gc_grace,           │
│                      else discard both tombstone and value         │
│                    - For expired TTLs: discard                     │
│                           │                                        │
│                           ▼                                        │
│  L1 (after):  [SS_1: A-D] [SS_2: E-L] [SS_3: M-R] [SS_4: S-Z] │
│                (new files, non-overlapping ✓)                     │
│                                                                    │
│  Step 4: Atomically swap old files → new files                    │
│          (update SSTable manifest file)                            │
│                                                                    │
│  Step 5: Delete old L0 and old L1 SSTables                        │
│                                                                    │
│  Step 6: Rebuild bloom filters for new SSTables                   │
│                                                                    │
└────────────────────────────────────────────────────────────────────┘

I/O Impact:
- Compaction is I/O intensive (read + write entire SSTables)
- Rate limited: max 50 MB/sec compaction I/O to avoid starving foreground traffic
- Runs on separate thread pool (2-4 threads)
- If compaction backlog grows → latency increases (more SSTables to check on reads)
- Monitor: pending_compaction_bytes, sstable_count_per_level
```

---

## 12. Conflict Resolution Flow

When two clients write the same key concurrently to different coordinator nodes.

```
Scenario: Concurrent writes, Last-Write-Wins (LWW)
───────────────────────────────────────────────────

Client 1                Node A (Coord)    Node B              Node C (Coord)    Client 2
    │                      │                 │                      │               │
    │  PUT(K, V1)          │                 │                      │  PUT(K, V2)   │
    │  T=100               │                 │                      │  T=105        │
    │─────────────────────▶│                 │                      │◀──────────────│
    │                      │                 │                      │               │
    │                      │ Write K=V1,T=100│                      │Write K=V2,T=105
    │                      │ locally         │                      │locally        │
    │                      │                 │                      │               │
    │                      │ Forward to B ──▶│◀── Forward to B ────│               │
    │                      │ Forward to C ──────────────────────── │               │
    │                      │                 │                      │               │
    │                      │                 │  Node B receives:    │               │
    │                      │                 │  K=V1,T=100 from A   │               │
    │                      │                 │  K=V2,T=105 from C   │               │
    │                      │                 │  → Keeps V2 (T=105   │               │
    │                      │                 │    > T=100)          │               │
    │                      │                 │                      │               │
    │  200 OK (V1 written) │                 │                      │  200 OK       │
    │◀─────────────────────│                 │                      │──────────────▶│
    │                      │                 │                      │               │


State after both writes:
  Node A: K=V1, T=100   (will get V2 via read repair or anti-entropy)
  Node B: K=V2, T=105   ✓ (applied both, kept newer)
  Node C: K=V2, T=105   ✓ (will get V1, but V2 wins on timestamp)

On next QUORUM read:
  Read from B: K=V2, T=105
  Read from A: K=V1, T=100  ← stale!
  → Return V2 to client
  → Read repair: send V2,T=105 to Node A (async)

Final state: All nodes have K=V2, T=105 ✓


With Vector Clocks (conflict surfaced to client):
─────────────────────────────────────────────────

  Write 1 via Node A: K=V1, clock=[(A,1)]
  Write 2 via Node C: K=V2, clock=[(C,1)]

  On QUORUM read:
  → Neither clock dominates the other (concurrent!)
  → Return BOTH to client:
    { "siblings": [
        {"value": "V1", "clock": {"A": 1}},
        {"value": "V2", "clock": {"C": 1}}
    ]}

  Client merges and writes back:
  → PUT(K, V_merged, clock=[(A,1),(C,1),(B,1)])
  → Conflict resolved
```

---

## Flow Summary

| # | Flow | Trigger | Sync/Async | Hot Path? |
|---|------|---------|------------|-----------|
| 1 | Write (PUT) | Client request | Sync (wait for W ACKs) | ✅ Yes |
| 2 | Read (GET) | Client request | Sync (wait for R responses) | ✅ Yes |
| 3 | Delete | Client request | Sync (tombstone write) | ✅ Yes |
| 4 | Read Repair | During quorum read | Async (after client response) | Piggybacks on reads |
| 5 | Hinted Handoff | Write to downed replica | Async (background delivery) | ❌ Background |
| 6 | Anti-Entropy | Periodic timer (1h) | Async (background) | ❌ Background |
| 7 | Node Join | Operator action | Async (streaming) | ❌ Background |
| 8 | Node Leave | Operator action | Async (streaming) | ❌ Background |
| 9 | Failure Detection | Continuous (gossip) | Async (1s intervals) | ❌ Background |
| 10 | TTL Expiry | Read + compaction | Mixed | Lazy = hot path, GC = background |
| 11 | Compaction | Background trigger | Async (background) | ❌ Background |
| 12 | Conflict Resolution | Concurrent writes | On-read (LWW) or client-driven | Depends on strategy |

---

*This flow document complements the [interview simulation](interview-simulation.md), [API contracts](api-contracts.md), and [datastore design](datastore-design.md).*