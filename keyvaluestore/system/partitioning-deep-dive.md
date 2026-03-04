# Distributed Key-Value Store — Partitioning Deep Dive

> This document covers the second pillar topic for distributed KV stores: how we distribute data across nodes. It covers naive hashing, consistent hashing, virtual nodes, preference lists, rebalancing, and the tradeoffs between hash-based and range-based partitioning.

---

## Table of Contents

1. [Why Partition?](#1-why-partition)
2. [Naive Hash Partitioning (and Why It Fails)](#2-naive-hash-partitioning-and-why-it-fails)
3. [Consistent Hashing](#3-consistent-hashing)
4. [Virtual Nodes (VNodes)](#4-virtual-nodes-vnodes)
5. [Preference Lists & Replica Placement](#5-preference-lists--replica-placement)
6. [Rebalancing: Node Join](#6-rebalancing-node-join)
7. [Rebalancing: Node Leave](#7-rebalancing-node-leave)
8. [Hot Partition Detection & Mitigation](#8-hot-partition-detection--mitigation)
9. [Hash-Based vs Range-Based Partitioning](#9-hash-based-vs-range-based-partitioning)
10. [Ring State Dissemination (Gossip)](#10-ring-state-dissemination-gossip)

---

## 1. Why Partition?

```
Problem: 50 TB of data and 1M reads/sec cannot fit on a single machine.

Solution: Split the data across N machines (partitions/shards).
Each machine owns a subset of the key space.

Requirements for a good partitioning scheme:
  1. EVEN distribution — no node should have disproportionately more data
  2. MINIMAL movement — adding/removing nodes should move as little data as possible
  3. DETERMINISTIC — given a key, any node can compute which node owns it
     (no central lookup table needed on the hot path)
  4. FAULT-TOLERANT — node failure shouldn't make any data inaccessible

Our numbers:
  - 10 billion keys, 50 TB raw data
  - 100 nodes, each holds ~500 GB of primary data + replicas
  - 1M reads/sec, 100K writes/sec distributed across nodes
  - Each node handles ~10K reads/sec, ~1K writes/sec
```

---

## 2. Naive Hash Partitioning (and Why It Fails)

```
The simplest approach: node = hash(key) % N

Example with N = 4 nodes:
  hash("user:123") = 7   → 7 % 4 = 3 → Node 3
  hash("user:456") = 12  → 12 % 4 = 0 → Node 0
  hash("user:789") = 5   → 5 % 4 = 1 → Node 1

This works fine... until you add or remove a node.


THE PROBLEM: Adding a node (N=4 → N=5)
──────────────────────────────────────

Before (N=4):                     After (N=5):
  hash=7:  7 % 4 = 3 → Node 3     hash=7:  7 % 5 = 2 → Node 2  ← MOVED!
  hash=12: 12 % 4 = 0 → Node 0    hash=12: 12 % 5 = 2 → Node 2  ← MOVED!
  hash=5:  5 % 4 = 1 → Node 1     hash=5:  5 % 5 = 0 → Node 0  ← MOVED!

Almost EVERY key remaps to a different node!

How many keys move?
  On average: (N-1)/N × total_keys
  For N=100 → N=101: 99/100 = 99% of keys move!
  That's 99% × 50 TB = ~49.5 TB of data shuffling!

This is catastrophic:
  - Network saturated for hours/days during rebalancing
  - Reads fail for keys that are in transit
  - Writes may be lost if delivered to the wrong node
  - The cluster is effectively down during the transition

Conclusion: hash(key) % N is useless for dynamic clusters.
```

---

## 3. Consistent Hashing

### Core Concept

```
Instead of using modulo, place both NODES and KEYS on a circular ring.

The ring represents the full hash space: [0, 2^128)
  - We use a good hash function (e.g., MD5, MurmurHash3, xxHash)
    that maps any input to a 128-bit integer
  - Both node identifiers and keys are hashed onto this ring

                          0 (= 2^128)
                     ╭────────╮
                 ╭───╯        ╰───╮
             Node A               Node B
               │    ╭─ key K1      │
               │    │              │
      ─────────┤    │              ├─────────
               │    │              │
               │    ╰─ key K2      │
             Node D               Node C
                 ╰───╮        ╭───╯
                     ╰────────╯
                       2^64

Rule: A key is assigned to the FIRST node encountered when walking
      CLOCKWISE from the key's hash position.

  key K1 → hash to position X → walk clockwise → first node = Node B
  key K2 → hash to position Y → walk clockwise → first node = Node D


When a node is ADDED (Node E inserted between B and C):
  - Only keys between Node B and Node E need to move to Node E
  - All other keys stay on their current nodes
  - On average: K/N keys move (K = total keys, N = num nodes)
  - For N=100: only 1% of keys move! vs 99% with naive hashing.

When a node is REMOVED (Node C removed):
  - Only C's keys move to the next node clockwise (Node D)
  - All other keys stay put
  - Again, ~K/N keys affected
```

### Mathematical Comparison

```
                        Naive (hash % N)      Consistent Hashing
─────────────────────────────────────────────────────────────────
Keys moved on            (N-1)/N × K           K/N
add/remove:              ≈ 99% (for N=100)     ≈ 1% (for N=100)

Data moved               ~49.5 TB              ~500 GB
(50 TB, N=100→101):

Rebalancing time         Hours to days         Minutes
(at 1 Gbps):

Service disruption:      Severe                Minimal
```

---

## 4. Virtual Nodes (VNodes)

### The Problem with Basic Consistent Hashing

```
With only N physical nodes on the ring, the distribution is UNEVEN.

Example: 4 nodes randomly placed on the ring:

    ┌──────────────────────────────────────┐
    │                                      │
    │  Node positions (out of 360°):       │
    │  A: 10°, B: 20°, C: 200°, D: 350°  │
    │                                      │
    │  Arc sizes (data each node holds):   │
    │  A: 10°  (2.8% of data)  ← tiny!    │
    │  B: 180° (50% of data)   ← huge!    │
    │  C: 150° (41.7% of data) ← huge!    │
    │  D: 20°  (5.5% of data)  ← tiny!    │
    │                                      │
    │  Expected: 25% each. Actual: 2.8% to 50%!  │
    └──────────────────────────────────────┘

Problems:
  1. Uneven data distribution → some nodes are overloaded, others idle
  2. When a node fails, ALL its load transfers to ONE neighbor
     (the next clockwise node) → potential cascade failure
  3. Cannot handle heterogeneous hardware (all nodes get equal arcs)
```

### VNodes: The Solution

```
Instead of 1 position per physical node, assign MANY positions (virtual nodes).

Physical Node A → 256 vnodes: A_0, A_1, A_2, ..., A_255
Physical Node B → 256 vnodes: B_0, B_1, B_2, ..., B_255
Physical Node C → 256 vnodes: C_0, C_1, C_2, ..., C_255
Physical Node D → 256 vnodes: D_0, D_1, D_2, ..., D_255

Total vnodes on ring: 4 × 256 = 1,024

Ring (simplified, showing a subset):
─A_42─B_17─C_203─A_128─D_91─B_244─C_55─D_180─A_7─B_156─C_99─D_33─...

With 1,024 vnodes, the arc sizes are very uniform:
  Expected per vnode: 360° / 1024 = 0.35°
  Each physical node owns 256 arcs ≈ 90° total ≈ 25% of data
  Standard deviation of load: ~2-3% (vs 50%+ without vnodes)


Benefits of VNodes:

1. EVEN DISTRIBUTION
   ─────────────────
   256 random points per node → law of large numbers → near-uniform arcs
   With 100 nodes × 256 vnodes = 25,600 points on the ring
   Load variance < 5%

2. GRACEFUL FAILURE HANDLING
   ─────────────────────────
   When Node A fails, its 256 vnodes' data spreads across MANY nodes:

   Without vnodes:                  With vnodes:
   Node A fails → ALL A's data     Node A fails → A's 256 vnodes' data
   goes to Node B (next clockwise)  spreads across ~100+ different nodes
   → Node B gets 2x load!          → Each node gets ~1% extra load
   → Potential cascade!             → No cascade risk

3. HETEROGENEOUS HARDWARE
   ──────────────────────
   Powerful machine → more vnodes (e.g., 512)
   Weak machine → fewer vnodes (e.g., 128)
   Each machine gets proportional data and traffic

4. INCREMENTAL REBALANCING
   ───────────────────────
   New node takes vnodes from MANY existing nodes
   → Data streams from many sources in parallel
   → Faster rebalancing, no single-source bottleneck
```

### Choosing the Number of VNodes

```
How many vnodes per physical node?

Factor                        Impact
────────────────────────────────────────────────────────
More vnodes (e.g., 512)      Better load distribution
                              More metadata overhead (ring state is larger)
                              More Merkle trees per node (1 per vnode)
                              Faster rebalancing (more granular transfer)

Fewer vnodes (e.g., 32)      Worse load distribution
                              Less metadata
                              Fewer Merkle trees
                              Coarser rebalancing

Our choice: 256 vnodes per node

Reasoning:
  - 100 nodes × 256 vnodes = 25,600 points on ring
  - Ring state metadata: 25,600 × (16 bytes token + 32 bytes node_id) ≈ 1.2 MB
    → trivially small, fits in memory
  - Merkle trees: 256 per node × 2 MB each ≈ 512 MB
    → fits in RAM on a 64 GB machine
  - Load standard deviation at 256 vnodes: ~2-3%
    → acceptable uniformity
  - At 512 vnodes, std dev drops to ~1.5% but Merkle tree memory doubles
    → diminishing returns

Systems in production:
  - Cassandra: 256 vnodes per node (default)
  - DynamoDB: variable (AWS manages this internally)
  - Riak: 64 vnodes per node (smaller default, fewer Merkle trees)
```

---

## 5. Preference Lists & Replica Placement

### How Replicas Are Assigned

```
For replication factor N=3, each key is stored on 3 DISTINCT physical nodes.

Algorithm:
  1. Hash the key → position on the ring
  2. Walk clockwise from that position
  3. Collect the first N DISTINCT PHYSICAL NODES encountered
  4. Skip vnodes that belong to the same physical node as an already-collected node

Example:

  Key K hashes to position X.
  Walking clockwise from X:

  Position    VNode     Physical Node    Action
  ─────────────────────────────────────────────────
  X+1         B_17      Node B           ✓ Replica 1 (coordinator)
  X+2         B_244     Node B           SKIP (already have B)
  X+3         A_128     Node A           ✓ Replica 2
  X+4         B_91      Node B           SKIP (already have B)
  X+5         C_55      Node C           ✓ Replica 3
  DONE

  Preference list for key K: [B, A, C]

  Node B is the "coordinator" (first in the list).
  The client SDK routes requests for key K to Node B.
```

### Rack-Aware / AZ-Aware Placement

```
For fault tolerance, replicas should be on different racks and availability zones.

Enhanced algorithm:
  1. Walk clockwise, collect distinct physical nodes
  2. Additionally: ensure replicas are in different AZs (or racks)

Example with 3 AZs (us-east-1a, us-east-1b, us-east-1c):

  Position    VNode     Node     AZ              Action
  ──────────────────────────────────────────────────────────
  X+1         B_17      Node B   us-east-1a      ✓ Replica 1
  X+2         A_128     Node A   us-east-1a      SKIP (same AZ as B)
  X+3         D_55      Node D   us-east-1b      ✓ Replica 2
  X+4         C_91      Node C   us-east-1b      SKIP (same AZ as D)
  X+5         E_42      Node E   us-east-1c      ✓ Replica 3
  DONE

  Preference list: [B (1a), D (1b), E (1c)]
  → All 3 replicas in different AZs!
  → Survives an entire AZ outage without data loss

Trade-off: AZ-aware placement may mean walking further on the ring
to find a node in a different AZ → slightly uneven data distribution.
In practice, with 256 vnodes per node, this is negligible.
```

---

## 6. Rebalancing: Node Join

### Step-by-Step Process

```
Initial state: 4 nodes (A, B, C, D), 256 vnodes each = 1,024 vnodes

Adding Node E:

Step 1: E contacts seed nodes, bootstraps gossip
        E receives full cluster state (ring, node list)

Step 2: E is assigned 256 vnodes
        Strategy: take vnodes from the MOST LOADED nodes
        
        Before:                After:
        A: 256 vnodes          A: ~205 vnodes (gave ~51 to E)
        B: 256 vnodes          B: ~205 vnodes (gave ~51 to E)
        C: 256 vnodes          C: ~205 vnodes (gave ~51 to E)
        D: 256 vnodes          D: ~205 vnodes (gave ~51 to E)
                               E: ~204 vnodes (took ~51 from each)
        Total: 1,024           Total: 1,024 (unchanged)
        Each: 25%              Each: ~20% (5 nodes now)

Step 3: E announces JOINING status via gossip
        All nodes learn E is joining and which vnodes E will own

Step 4: Data streaming begins
        For each vnode E takes over:
        - The previous owner streams all KV pairs in that range to E
        - Streaming is throttled (e.g., 50 MB/sec) to avoid saturating I/O

        ┌──────────┐     stream data     ┌──────────┐
        │  Node A  │────────────────────▶│  Node E  │
        │ (51 vnodes)                    │ (receiving│
        └──────────┘                     │  data)   │
        ┌──────────┐     stream data     │          │
        │  Node B  │────────────────────▶│          │
        │ (51 vnodes)                    │          │
        └──────────┘                     │          │
        ┌──────────┐     stream data     │          │
        │  Node C  │────────────────────▶│          │
        │ (51 vnodes)                    │          │
        └──────────┘                     │          │
        ┌──────────┐     stream data     │          │
        │  Node D  │────────────────────▶│          │
        │ (51 vnodes)                    └──────────┘
        └──────────┘
        
        Data moved: ~20% of 50 TB = 10 TB
        At 50 MB/sec × 4 parallel streams = 200 MB/sec
        Time: 10 TB / 200 MB/sec ≈ 14 hours

Step 5: During streaming (the tricky part):
        Old owner continues serving reads/writes for in-transit vnodes
        Two approaches:
        
        Option A: Old owner serves until handoff complete
          - Simplest
          - Risk: writes during streaming may be lost if they go
            to the old owner after E thinks it owns the vnode
        
        Option B: Dual-write during transition
          - Writes go to BOTH old and new owner
          - Reads go to old owner (guaranteed to have all data)
          - More complex but no data loss risk
        
        Our choice: Option A with a brief "freeze" at the end
          - Old owner serves until streaming is 99%+ complete
          - Brief pause (ms) to flush remaining writes
          - Atomic ownership transfer via gossip
          - E starts serving

Step 6: E changes status to NORMAL via gossip
        Old owners mark transferred vnodes as "not mine"
        E fully operational
```

### Data Movement Calculation

```
Adding 1 node to a cluster of N nodes:

Data moved = Total_Data / (N + 1)

  N=3 → N=4:   50 TB / 4 = 12.5 TB moved (25% of total)
  N=99 → N=100: 50 TB / 100 = 500 GB moved (1% of total)
  N=100 → N=101: 50 TB / 101 = ~495 GB moved (~1% of total)

Compare to naive hashing:
  N=100 → N=101: 50 TB × 99/100 = 49.5 TB moved (99% of total!)

Consistent hashing: 100x less data movement!
```

---

## 7. Rebalancing: Node Leave

```
Graceful decommission of Node C:

Step 1: Operator sends decommission command to Node C
Step 2: C changes status to LEAVING via gossip
Step 3: C streams ALL its data to the next owners on the ring

        For each of C's 256 vnodes:
        - Determine which node is next clockwise on the ring
        - Stream all KV pairs for that vnode range to the next node

        ┌──────────┐     stream data     ┌──────────┐
        │  Node C  │────────────────────▶│  Node A  │ (gets C's vnodes 1-80)
        │ (LEAVING)│────────────────────▶│  Node B  │ (gets C's vnodes 81-170)
        │          │────────────────────▶│  Node D  │ (gets C's vnodes 171-256)
        └──────────┘                     └──────────┘

Step 4: C continues serving traffic during streaming
Step 5: Streaming complete → C releases all tokens
Step 6: C removed from gossip membership
Step 7: C shuts down

Data moved: All of C's data ≈ 50 TB / 4 = 12.5 TB
Distributed across remaining 3 nodes (~4.2 TB each)

After decommission:
  A: ~341 vnodes (33.3% of data)
  B: ~341 vnodes (33.3% of data)
  D: ~342 vnodes (33.3% of data)

  Automatically rebalanced! No manual intervention needed.


Ungraceful failure (Node C crashes):
  - No streaming possible — C is dead
  - Remaining nodes detect failure via gossip (10-30 seconds)
  - Data is NOT moved — instead:
    - Hinted handoff handles writes that C should receive
    - Read repair and anti-entropy handle existing data divergence
    - An operator eventually either:
      a) Restarts C (it rejoins and catches up via hints + anti-entropy)
      b) Replaces C with a new node (which streams data from replicas)
  - No data loss because every key has N=3 replicas on different nodes
```

---

## 8. Hot Partition Detection & Mitigation

```
Problem: Even with consistent hashing + vnodes, a single key can be
extremely popular (e.g., a viral product page, a trending hashtag).

That key's 3 replicas receive ALL the traffic for that key.
At 100K reads/sec for one key → 3 nodes each handle 33K extra reads/sec.

Detection:
  - Per-key request counter (sampled, not exhaustive)
  - Monitor: requests_per_key_per_second (top-K tracking)
  - Alert if any key exceeds threshold (e.g., 10K reads/sec)

Mitigation strategies:

Strategy 1: Client-Side Caching
  ┌──────────────────────────────────────────────┐
  │  Client SDK LRU Cache                         │
  │  - Cache hot keys with short TTL (1-5 sec)   │
  │  - 90%+ of repeated reads served from cache  │
  │  - Zero network calls for cache hits          │
  │  - Only works for the SAME client instance    │
  └──────────────────────────────────────────────┘

Strategy 2: Request Coalescing
  ┌──────────────────────────────────────────────┐
  │  If 100 concurrent requests arrive for the   │
  │  same key at the coordinator:                │
  │  - First request → actual read from storage  │
  │  - Remaining 99 → wait for first to complete │
  │  - Return the same value to all 100 callers  │
  │  - 100 requests → 1 storage read!            │
  └──────────────────────────────────────────────┘

Strategy 3: Read Replicas / Random Read Distribution
  ┌──────────────────────────────────────────────┐
  │  For consistency=ONE reads:                   │
  │  - Instead of always reading from the         │
  │    coordinator (first in preference list),    │
  │    randomly pick ANY of the N replicas        │
  │  - Spreads read load across all 3 replicas   │
  │  - Only safe for eventual consistency (ONE)  │
  └──────────────────────────────────────────────┘

Strategy 4: Key Splitting (application-level)
  ┌──────────────────────────────────────────────┐
  │  Application adds a random suffix to the key:│
  │  - Original: "trending:product:99999"        │
  │  - Split into: "trending:product:99999:0"    │
  │                "trending:product:99999:1"     │
  │                ...                            │
  │                "trending:product:99999:9"     │
  │  - 10 keys → 10 different partitions          │
  │  - Reads: query all 10, merge results         │
  │  - Only for extreme cases (adds complexity)   │
  └──────────────────────────────────────────────┘

Our recommendation priority:
  1. Client-side caching (simplest, most effective)
  2. Request coalescing (easy to implement in coordinator)
  3. Random read distribution for ONE consistency
  4. Key splitting (last resort — application complexity)
```

---

## 9. Hash-Based vs Range-Based Partitioning

```
Our system uses HASH-BASED partitioning. Let's compare with the alternative.

┌──────────────────────────────────────────────────────────────────────┐
│                                                                      │
│  Hash-Based Partitioning (Our Choice)                               │
│  ────────────────────────────────────                               │
│                                                                      │
│  key → hash(key) → position on ring → node                         │
│                                                                      │
│  "user:00001" → hash → 0x3A7F... → Node B                         │
│  "user:00002" → hash → 0xC912... → Node D                         │
│  "user:00003" → hash → 0x1B4E... → Node A                         │
│                                                                      │
│  Keys with similar names end up on DIFFERENT nodes                  │
│  (hash scatters them uniformly)                                     │
│                                                                      │
│                                                                      │
│  Range-Based Partitioning                                           │
│  ────────────────────────                                           │
│                                                                      │
│  key → compare directly → assigned to a range → node                │
│                                                                      │
│  Node A: keys [a - g]                                               │
│  Node B: keys [h - n]                                               │
│  Node C: keys [o - t]                                               │
│  Node D: keys [u - z]                                               │
│                                                                      │
│  "user:00001" → starts with 'u' → Node D                           │
│  "user:00002" → starts with 'u' → Node D                           │
│  "user:00003" → starts with 'u' → Node D                           │
│                                                                      │
│  Keys with similar names end up on the SAME node                    │
│  (preserves key ordering)                                           │
│                                                                      │
└──────────────────────────────────────────────────────────────────────┘
```

### Detailed Comparison

| Aspect | Hash-Based | Range-Based |
|---|---|---|
| **Point lookups** | ✅ O(1) — hash to find node | ✅ O(log N) — binary search ranges |
| **Range queries** | ❌ Scatter-gather (ALL nodes) | ✅ Single node or few adjacent nodes |
| **Data distribution** | ✅ Uniform (hash scatters evenly) | ⚠️ Can be skewed (popular key ranges) |
| **Hot spots** | ✅ Rare (unless single key is hot) | ❌ Common (sequential writes to same range) |
| **Rebalancing** | ✅ Proportional (K/N keys move) | ⚠️ May need range splitting |
| **Key ordering** | ❌ Lost (hash destroys order) | ✅ Preserved |
| **Prefix scans** | ❌ Expensive (scatter-gather) | ✅ Efficient (co-located) |

### Why We Chose Hash-Based

```
Our requirements:
  1. Point lookups only (GET/PUT/DELETE by exact key) — no range queries
  2. Uniform load distribution is critical at 1M reads/sec
  3. Simple, predictable partitioning with minimal hot spots

Hash-based is clearly better for our use case.

If we needed range queries:
  → Use range-based partitioning (like HBase, CockroachDB, Spanner)
  → Accept the hot-spot risk and mitigate with dynamic range splitting
  → Or use a hybrid: hash-partition by a prefix, range within partition
```

---

## 10. Ring State Dissemination (Gossip)

```
Every node needs to know the full ring state to route requests correctly.
This state is disseminated via the gossip protocol.

Ring state = {
  For each vnode on the ring:
    token_position: uint128
    owning_node: NodeId
    status: NORMAL | JOINING | LEAVING
}

Size: 25,600 vnodes × (16 bytes token + 32 bytes node_id + 1 byte status)
    = ~1.2 MB

This easily fits in memory on every node.


Dissemination protocol:
  1. When a node's ring state changes (join, leave, failure):
     - The originating node updates its local state
     - The change is included in the next gossip round
  2. Every 1 second, each node gossips with 1-3 random peers
  3. Gossip message includes a digest of the ring state version
  4. If the receiver has an older version → full state is sent
  5. Convergence: O(log N) rounds = ~7 seconds for 100 nodes

During convergence (ring state is in flux):
  - Some nodes have old state, some have new
  - A client might send a request to the wrong coordinator
  - That's OK: the node receiving the request checks if it's
    the correct coordinator. If not, it forwards the request
    to the correct node (transparent to the client).
  - This "forwarding" adds ~1ms latency but is rare and temporary.


Alternative: Central metadata service (e.g., ZooKeeper, etcd)
  Pros: Instant consistency of ring state
  Cons: Single point of failure, latency for every request
  
  Our choice: Gossip (no SPOF, works during partitions)
  Only use ZooKeeper for initial seed node discovery, not on the hot path.
```

---

## Summary: Complete Partitioning Architecture

```
┌─────────────────────────────────────────────────────────────────────┐
│                    Complete Partitioning Stack                        │
│                                                                     │
│  Layer 1: Hash Function                                             │
│  ──────────────────────                                             │
│  MD5 / MurmurHash3 / xxHash → 128-bit hash                        │
│  Converts any key to a position on the ring                         │
│                                                                     │
│  Layer 2: Consistent Hash Ring                                      │
│  ──────────────────────────────                                     │
│  25,600 vnodes (100 nodes × 256 vnodes each)                      │
│  Keys map to the first vnode clockwise from their hash position     │
│                                                                     │
│  Layer 3: Virtual Node → Physical Node Mapping                     │
│  ──────────────────────────────────────────────                     │
│  Each vnode maps to exactly one physical node                       │
│  Physical nodes own 256 vnodes each (configurable)                  │
│                                                                     │
│  Layer 4: Preference List (Replication)                             │
│  ──────────────────────────────────────                             │
│  Walk clockwise, collect first N distinct physical nodes            │
│  Skip same-physical-node vnodes, prefer different AZs              │
│  Preference list for key K: [B, A, C]                              │
│                                                                     │
│  Layer 5: Gossip-Based Ring State                                  │
│  ────────────────────────────────                                  │
│  Every node knows the full ring via epidemic gossip                 │
│  Converges in O(log N) rounds                                      │
│  Client SDK also maintains a local copy of the ring                │
│                                                                     │
│  Layer 6: Client SDK Routing                                       │
│  ────────────────────────────                                      │
│  SDK hashes key → looks up ring → sends directly to coordinator    │
│  No central proxy on the hot path!                                 │
│  If ring is stale → coordinator forwards to correct node           │
│                                                                     │
└─────────────────────────────────────────────────────────────────────┘
```

---

*This document complements the [interview simulation](interview-simulation.md), [consistency & replication](consistency-and-replication.md), and [flow diagrams](flow.md).*