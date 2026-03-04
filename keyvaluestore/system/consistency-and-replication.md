# Distributed Key-Value Store — Consistency & Replication Deep Dive

> This is THE defining topic for distributed key-value stores. This document covers replication strategies, consistency models, quorum mechanics, conflict resolution, and the CAP theorem — all applied to our system.

---

## Table of Contents

1. [Replication Strategy: Why Leaderless?](#1-replication-strategy-why-leaderless)
2. [Quorum Mechanics (W + R > N)](#2-quorum-mechanics-w--r--n)
3. [Consistency Levels Explained](#3-consistency-levels-explained)
4. [Conflict Resolution Deep Dive](#4-conflict-resolution-deep-dive)
5. [Read Repair — Opportunistic Healing](#5-read-repair--opportunistic-healing)
6. [Hinted Handoff — Temporary Failure Tolerance](#6-hinted-handoff--temporary-failure-tolerance)
7. [Anti-Entropy — Full Consistency via Merkle Trees](#7-anti-entropy--full-consistency-via-merkle-trees)
8. [CAP Theorem Applied](#8-cap-theorem-applied)
9. [Sloppy Quorum vs Strict Quorum](#9-sloppy-quorum-vs-strict-quorum)
10. [Consistency During Network Partitions](#10-consistency-during-network-partitions)
11. [Layered Defense: How All Mechanisms Work Together](#11-layered-defense-how-all-mechanisms-work-together)

---

## 1. Replication Strategy: Why Leaderless?

### The Three Models

```
┌─────────────────────────────────────────────────────────────────────────┐
│                     Replication Architectures                            │
│                                                                         │
│  Model 1: Single Leader (Master-Slave)                                 │
│  ─────────────────────────────────────                                 │
│  ┌────────┐   sync/async    ┌────────┐                                │
│  │ Leader │───────────────▶│Follower│                                 │
│  │(writes)│───────────────▶│Follower│                                 │
│  └────────┘                └────────┘                                 │
│  Used by: PostgreSQL, MySQL, MongoDB                                   │
│  Pro: No write conflicts (leader serializes all writes)                │
│  Con: Leader is a bottleneck and SPOF                                  │
│                                                                         │
│  Model 2: Multi-Leader                                                 │
│  ─────────────────────                                                 │
│  ┌────────┐ ◀──conflict──▶ ┌────────┐                                │
│  │Leader 1│               │Leader 2│                                  │
│  │(DC-1)  │               │(DC-2)  │                                  │
│  └────────┘               └────────┘                                  │
│  Used by: CouchDB, Galera Cluster                                      │
│  Pro: Writes accepted in multiple DCs                                  │
│  Con: Write conflicts between leaders                                  │
│                                                                         │
│  Model 3: Leaderless ⭐ (Our Choice)                                   │
│  ────────────────────                                                  │
│  ┌────────┐  ┌────────┐  ┌────────┐                                  │
│  │ Node A │  │ Node B │  │ Node C │                                  │
│  │(replica)│  │(replica)│  │(replica)│                                │
│  └────────┘  └────────┘  └────────┘                                  │
│  ALL nodes are equal — any can accept reads and writes                 │
│  Used by: DynamoDB, Cassandra, Riak, Voldemort                        │
│  Pro: No SPOF, highest availability, any node serves any request      │
│  Con: Write conflicts possible — need resolution strategy              │
│                                                                         │
└─────────────────────────────────────────────────────────────────────────┘
```

### Why Leaderless for Our System?

| Requirement | Leader-Based | Leaderless |
|---|---|---|
| **High availability** (our #1 priority) | Leader failure = brief downtime during election | ✅ Any node can serve — no election needed |
| **Tunable consistency** | Strong by default, weaken with async replicas | ✅ Natural: W/R knobs give full spectrum |
| **100K writes/sec** | Leader bottleneck for hot partitions | ✅ Load spread across all replicas |
| **Multi-DC** | Cross-DC writes → high latency to remote leader | ✅ Write to local replicas, async cross-DC |
| **Partition tolerance** | Partition cuts off leader → writes fail | ✅ Both sides of partition can accept writes |

**The trade-off we accept:** Write conflicts are possible (two clients writing the same key via different coordinators). We handle this via Last-Write-Wins (default) or vector clocks (opt-in).

---

## 2. Quorum Mechanics (W + R > N)

### The Core Idea

```
N = Number of replicas for each key (typically 3)
W = Number of replicas that must ACK a write before success
R = Number of replicas that must respond to a read before returning

The QUORUM INVARIANT: W + R > N

When this holds, the read set and write set MUST overlap:
at least one node that participated in the last write will also
participate in the read → the client sees the latest value.
```

### Visual Proof

```
N = 3 replicas: [Node A, Node B, Node C]

W = 2 (write quorum):     Write goes to at least 2 of {A, B, C}
R = 2 (read quorum):      Read goes to at least 2 of {A, B, C}

Possible write sets (any 2 of 3):
  {A, B}  or  {A, C}  or  {B, C}

Possible read sets (any 2 of 3):
  {A, B}  or  {A, C}  or  {B, C}

For ANY combination of write set and read set, they share at least 1 node:
  Write {A, B} + Read {A, C} → overlap: A ✓
  Write {A, B} + Read {B, C} → overlap: B ✓
  Write {A, C} + Read {A, B} → overlap: A ✓
  Write {A, C} + Read {B, C} → overlap: C ✓
  Write {B, C} + Read {A, B} → overlap: B ✓
  Write {B, C} + Read {A, C} → overlap: C ✓

EVERY combination has overlap → strong consistency guaranteed!
```

### All Quorum Configurations

```
┌─────────────────────────────────────────────────────────────────────────┐
│                   Quorum Configurations (N=3)                            │
│                                                                         │
│  Configuration   │ W │ R │ W+R>N? │ Consistency  │ Use Case            │
│  ────────────────┼───┼───┼────────┼──────────────┼─────────────────────│
│  QUORUM (default)│ 2 │ 2 │ 4>3 ✓ │ STRONG       │ Most operations     │
│  ONE (fast read) │ 2 │ 1 │ 3=3 ✗ │ EVENTUAL     │ Caching, analytics  │
│  ONE (fast write)│ 1 │ 2 │ 3=3 ✗ │ EVENTUAL     │ Logging, events     │
│  ALL (strongest) │ 3 │ 3 │ 6>3 ✓ │ STRONGEST    │ Critical data       │
│  ONE+ONE (fast)  │ 1 │ 1 │ 2<3 ✗ │ VERY EVENTUAL│ Non-critical cache  │
│                                                                         │
│  Note: W+R=N (e.g., W=2,R=1) does NOT guarantee overlap!              │
│  Consider: Write to {A,B}, Read from {C} → no overlap → stale read!   │
│  You need STRICTLY greater than N.                                      │
│                                                                         │
└─────────────────────────────────────────────────────────────────────────┘
```

### Latency vs Consistency Trade-off

```
                  Low Latency                          Strong Consistency
                     ◀─────────────────────────────────────▶
                     
  W=1,R=1            W=1,R=2 or W=2,R=1      W=2,R=2         W=3,R=3
  ┌──────┐           ┌──────────────────┐     ┌──────────┐   ┌──────┐
  │Fastest│           │ Fast but still   │     │ QUORUM   │   │Slowest│
  │~1-2ms │           │ eventual         │     │ ~3-5ms   │   │~5-10ms│
  │       │           │ ~2-3ms           │     │ Strong   │   │All ACK│
  │Eventual│          │                  │     │consistency│   │needed │
  └──────┘           └──────────────────┘     └──────────┘   └──────┘
  
  Risk: stale                                 Our default    Risk: one slow
  reads, data loss                                          node = high p99
  if node fails                                             fails if any
  before replication                                        node is down
```

---

## 3. Consistency Levels Explained

### Level: ONE

```
Write Path (W=1):
  Client → Coordinator writes locally → returns immediately
  Coordinator asynchronously replicates to other N-1 nodes

  Risk: If coordinator crashes BEFORE replication, data is lost!
  Latency: ~1-2ms

Read Path (R=1):
  Client → Coordinator reads locally → returns immediately
  Does NOT check other replicas

  Risk: May return stale data (other nodes have newer version)
  Latency: ~1ms

When to use:
  - Data you can afford to lose (cache entries, session hints)
  - High-throughput ingestion where speed > consistency
  - Non-critical reads where occasional staleness is OK
```

### Level: QUORUM

```
Write Path (W=2 for N=3):
  Client → Coordinator writes locally + forwards to 2 replicas
  Waits for 1 replica ACK (self + 1 = 2 = W)
  Returns to client

  Guarantee: At least 2 of 3 nodes have the data
  Risk: None (within quorum semantics)
  Latency: ~3-5ms (dominated by slowest of the first 2 ACKs)

Read Path (R=2 for N=3):
  Client → Coordinator reads locally + reads from 1 other replica
  Compares versions → returns newest

  Guarantee: W+R=4 > N=3 → at least 1 node in the read set
             participated in the last write → fresh data guaranteed
  Read repair: if replicas disagree, send newest to stale replicas
  Latency: ~3-5ms

When to use:
  - Default for all operations
  - Shopping carts, user preferences, session stores
  - Any data that matters but doesn't need ALL-node consistency
```

### Level: ALL

```
Write Path (W=3 for N=3):
  Client → Coordinator writes to ALL 3 replicas
  Waits for ALL 3 ACKs
  Returns to client

  Guarantee: All replicas have the data
  Risk: If ANY node is down → write FAILS (503 INSUFFICIENT_REPLICAS)
  Latency: ~5-10ms (dominated by slowest replica — tail latency!)

Read Path (R=3 for N=3):
  Client → Coordinator reads from ALL 3 replicas
  Compares versions → returns newest

  Guarantee: Strongest possible consistency
  Risk: If ANY node is down → read FAILS
  Latency: ~5-10ms

When to use:
  - Distributed locks (must be absolutely consistent)
  - Financial transactions (cannot tolerate stale reads)
  - Rare — most use cases don't need this level

Danger: ALL reduces availability to the weakest link.
  If 1 of 3 nodes is down → ALL operations fail.
  With QUORUM, 1 of 3 can be down and operations still succeed.
```

---

## 4. Conflict Resolution Deep Dive

### Why Conflicts Happen

```
In a leaderless system, two clients can write the same key simultaneously
through different coordinator nodes. There's no single leader to serialize writes.

Timeline of a conflict:

  T=100ms: Client 1 → Node A: PUT(K, "apple")
  T=105ms: Client 2 → Node C: PUT(K, "banana")

  Both writes achieve quorum independently:
  - Client 1's write: A writes locally, forwards to B (ACK) → quorum met
  - Client 2's write: C writes locally, forwards to B (ACK) → quorum met

  Node B now received both writes. Which one does it keep?
  Node A has "apple", Node C has "banana" — who's right?
```

### Strategy 1: Last-Write-Wins (LWW) — Our Default

```
Rule: Each write has a timestamp. Highest timestamp wins. Period.

  Node A: K="apple",  T=100
  Node B: K="banana", T=105 (applied both, kept T=105)
  Node C: K="banana", T=105

  On next read → "banana" wins (T=105 > T=100)
  "apple" is silently discarded

Pros:
  ✅ Simple — no client-side logic
  ✅ Automatic — always converges to one value
  ✅ No storage overhead (no version vectors)

Cons:
  ✗ Data loss! "apple" is gone forever
  ✗ Clock skew: if Node A's clock is 1 second ahead, its writes
    always win even if they happened "earlier" in real time
  ✗ Not suitable for additive operations (e.g., add item to cart)

Mitigation for clock skew:
  - Use NTP (Network Time Protocol) on all nodes — typically < 10ms skew
  - Use hybrid logical clocks (HLC) that combine wall clock + logical counter
  - Accept that LWW is "last writer according to its own clock wins"

When LWW is appropriate:
  - Idempotent data: session tokens, cache entries, feature flags
  - Full-value writes: each write replaces the entire value (not partial updates)
  - Use cases where "latest write wins" matches business logic
```

### Strategy 2: Vector Clocks — When Conflicts Must Be Surfaced

```
A vector clock is a list of (node, counter) pairs that tracks the causal
history of a value. It answers: "did Write A happen before Write B,
or were they concurrent?"

Example with 3 nodes:

  Initial state: K has no value

  Step 1: Client 1 writes K="apple" via Node A
    Value: "apple"
    Clock: {A: 1}
    All replicas now have: ("apple", {A: 1})

  Step 2: Client 2 reads K, gets ("apple", {A: 1})
    Client 2 modifies and writes K="apple,banana" via Node A
    Value: "apple,banana"
    Clock: {A: 2}    ← A's counter incremented
    This DOMINATES {A: 1} → no conflict, clean overwrite

  Step 3: CONCURRENT WRITES (the interesting case)
    Client 3 (didn't read first) writes K="cherry" via Node C
    Value: "cherry"
    Clock: {C: 1}

    Now we have TWO versions:
    Version 1: ("apple,banana", {A: 2})     — on Node A, B
    Version 2: ("cherry",       {C: 1})     — on Node C

    Are they concurrent?
    {A: 2} vs {C: 1}:
      - {A: 2} doesn't dominate {C: 1} (no A entry in {C: 1})
      - {C: 1} doesn't dominate {A: 2} (no C entry in {A: 2})
      → CONCURRENT! Neither is "after" the other.

  Step 4: Client reads K (QUORUM)
    Coordinator gets both versions. Returns BOTH as "siblings":
    {
      "siblings": [
        {"value": "apple,banana", "clock": {"A": 2}},
        {"value": "cherry",       "clock": {"C": 1}}
      ]
    }

  Step 5: Client resolves the conflict
    Application logic: merge the shopping cart items
    Merged value: "apple,banana,cherry"
    Client writes back via Node B:
    Value: "apple,banana,cherry"
    Clock: {A: 2, C: 1, B: 1}    ← merges both clocks + increments B

    This clock DOMINATES both previous clocks:
    {A: 2, C: 1, B: 1} ≥ {A: 2}  ✓
    {A: 2, C: 1, B: 1} ≥ {C: 1}  ✓
    → Conflict resolved. All replicas converge to merged value.


Clock comparison rules:
  Clock X DOMINATES Clock Y if:
    For EVERY node in Y, X has an equal or greater counter.
  
  {A:2, B:1} dominates {A:1, B:1}      (A:2 ≥ A:1, B:1 ≥ B:1) ✓
  {A:2, B:1} dominates {A:2}           (A:2 ≥ A:2, B:1 ≥ 0)   ✓
  {A:2} does NOT dominate {C:1}        (no C in first clock)    ✗ → CONCURRENT
```

### Vector Clock Growth Problem

```
Problem: Vector clocks grow as more nodes coordinate writes.
  After writes via nodes A, B, C, D, E: clock = {A:3, B:2, C:5, D:1, E:4}
  With 100 nodes, each clock could have 100 entries → metadata bloat.

Solutions:
  1. Truncation: Remove the oldest entry when clock exceeds N entries (e.g., 10)
     Risk: may cause false conflicts (treating causally related writes as concurrent)
     Used by: Amazon Dynamo (original paper)

  2. Dotted Version Vectors: More space-efficient variant that tracks only
     the latest write per node, not full history.
     Used by: Riak

  3. Hybrid approach: Use vector clocks only for keys that opt in.
     Default to LWW for most keys (no clock overhead).
     Our choice: LWW default, vector clocks opt-in.
```

### Strategy 3: CRDTs (Conflict-free Replicated Data Types)

```
CRDTs are data structures designed to be automatically mergeable
without conflicts. They guarantee convergence by mathematical properties.

Examples:
  G-Counter (grow-only counter):
    Each node maintains its own counter.
    Value = sum of all counters.
    Node A: 5, Node B: 3, Node C: 7 → Total = 15
    Merge: take max of each node's counter.
    Node A says 5, Node B says 3 → max(5, _) = 5, max(_, 3) = 3 → 5+3 = 8

  PN-Counter (positive-negative counter):
    Two G-Counters: one for increments, one for decrements.
    Value = sum(increments) - sum(decrements)

  G-Set (grow-only set):
    Union of all elements. Elements can only be added, never removed.
    Merge: set union.

  OR-Set (observed-remove set):
    Each element has a unique tag. Remove removes specific tags.
    Concurrent add + remove → add wins (the new add has a new tag).

Pros:
  ✅ No conflict resolution needed — merge is automatic and correct
  ✅ No coordination needed — each replica applies operations independently
  ✅ Mathematically guaranteed to converge

Cons:
  ✗ Limited to specific data types (can't CRDT a JSON document easily)
  ✗ Can be space-inefficient (OR-Set tracks tombstones for removed elements)
  ✗ Counter semantics may be surprising (PN-Counter doesn't support "set to 0")

When to use:
  - Distributed counters (likes, view counts, inventory levels)
  - Collaborative editing (text CRDTs like Yjs, Automerge)
  - Feature flags (boolean CRDT: true wins over false)
```

---

## 5. Read Repair — Opportunistic Healing

```
Read repair is a consistency mechanism triggered during normal reads.
It's "free" in the sense that we're already doing the read — we just
add a check-and-fix step.

When it triggers:
  1. Client sends QUORUM read
  2. Coordinator reads from R nodes (including itself)
  3. Coordinator compares responses:
     - If all agree → no repair needed
     - If responses differ → the newest version is the "correct" one
  4. Coordinator sends the newest version to stale replicas (async)

Configuration:
  read_repair_chance: 1.0 (100% — check on every quorum read)
  
  Some systems use probabilistic read repair:
    read_repair_chance: 0.1 (check on 10% of reads — reduces background I/O)
  
  Our choice: 1.0 for QUORUM reads (always repair — it's cheap)

What read repair CANNOT do:
  - Fix keys that are never read (use anti-entropy for those)
  - Fix keys during a partition (can't reach the stale replica)
  - Guarantee immediate repair (it's async — there's a window)

Read repair + hinted handoff + anti-entropy form a layered defense:
  Layer 1: Hinted handoff    → catches writes during temporary failures
  Layer 2: Read repair       → catches divergence for actively-read keys
  Layer 3: Anti-entropy      → catches ALL divergence (background sweep)
```

---

## 6. Hinted Handoff — Temporary Failure Tolerance

```
Purpose: Maintain write availability when a replica node is temporarily down.

Mechanism:
  When Node C is down and a write targets it:
  1. Coordinator achieves quorum without C (W=2: self + one other node)
  2. A "hint" is stored on a healthy node (the coordinator or another node)
  3. The hint = the full write data + metadata: {target: C, key, value, timestamp}
  4. When gossip reports C is back UP, the hint holder delivers the data to C
  5. C applies the hint as a normal write
  6. Hint is deleted after successful delivery

Important limitations:
  - Hints have a max window (default: 3 hours)
    If C is down > 3 hours, hints are discarded → anti-entropy must fix it
  - Hints consume disk on the hint holder
    At 100K writes/sec with 1/3 targeting the downed node:
    ~33K hints/sec × 5KB = 165 MB/sec → 600 GB/hour
  - Hints are NOT a consistency guarantee — they're best-effort
  - If the hint holder ALSO crashes before delivery → hints lost

Configuration:
  max_hint_window_in_ms: 10800000  (3 hours)
  hints_directory: /data/hints
  hint_delivery_threads: 2
  hint_delivery_batch_size: 1000
```

---

## 7. Anti-Entropy — Full Consistency via Merkle Trees

```
Purpose: Detect and fix ALL divergence between replicas, not just
actively-read keys. This is the "nuclear option" for consistency.

Runs: Periodically (every 1 hour) or on-demand via admin API

Mechanism: Merkle tree comparison (see flow.md for full details)

Key properties:
  - Efficient: O(log N) hashes to find divergent keys
  - Complete: finds ALL divergent keys, not just read ones
  - Expensive: rebuilding Merkle trees requires scanning all SSTables
  - Background: doesn't impact foreground read/write latency (if throttled)

When anti-entropy is critical:
  1. Node was down for > max_hint_window (hints expired)
  2. Node was replaced with a fresh instance (no data at all)
  3. Bit rot or disk corruption silently changed data on one replica
  4. Bug in the application caused different values on different replicas

Operational concern: Full repair of a large cluster is expensive
  - Each node pair comparison: O(minutes) for Merkle tree rebuild + compare
  - Full cluster repair (all pairs): O(hours) for a 100-node cluster
  - Best practice: run "incremental repair" on a rolling basis
    (repair 1/7 of the keyspace per day → full repair every week)
```

---

## 8. CAP Theorem Applied

```
CAP Theorem: In the presence of a network partition, you can have either
Consistency or Availability — but not both.

Our system: AP (Availability + Partition tolerance)

                    ┌───────────────┐
                    │       C       │
                    │ (Consistency) │
                    │               │
                    │  CP systems:  │
                    │  - HBase      │
                    │  - Spanner    │
                    │  - ZooKeeper  │
                    └───────┬───────┘
                           / \
                          /   \
                         /     \
                        / CAP   \
                       / theorem \
                      /    says:  \
                     /  pick 2 of 3\
                    /               \
     ┌─────────────┐               ┌─────────────┐
     │      A      │               │      P      │
     │(Availability)│               │(Partition   │
     │              │               │ Tolerance)  │
     │ AP systems:  │               │             │
     │ - DynamoDB ⭐│               │ P is        │
     │ - Cassandra  │               │ mandatory   │
     │ - Riak       │               │ in any      │
     │ - Our system │               │ distributed │
     └─────────────┘               │ system      │
                                    └─────────────┘

Why we choose AP:
  - P is non-negotiable (network partitions WILL happen)
  - Between C and A, we choose A:
    - A shopping cart that accepts writes during a partition (possibly conflicting)
      is better than a shopping cart that's completely unavailable
    - We can resolve conflicts after the partition heals
    - DynamoDB proved this at Amazon scale

But it's nuanced:
  - With QUORUM consistency (W+R>N), we get strong consistency WHEN there's no partition
  - During a partition, QUORUM operations may fail (503) if not enough replicas are reachable
  - With ONE consistency, we stay available during partitions but accept eventual consistency
  - So our system is "AP with tunable consistency" — not purely AP or CP
```

### PACELC: A More Nuanced Model

```
PACELC extends CAP: in case of Partition, choose A or C;
Else (no partition), choose Latency or Consistency.

Our system: PA/EL (Partition → Availability, Else → Latency)

  During partition: choose Availability (accept writes on both sides)
  During normal operation: choose Low Latency (QUORUM reads, not ALL)

  PA/EC systems: Dynamo, Cassandra (our category)
  PC/EC systems: Spanner, CockroachDB (always consistent, higher latency)
  PA/EL systems: Pure caches (always fast, never consistent)
```

---

## 9. Sloppy Quorum vs Strict Quorum

### Strict Quorum

```
Rule: The W writes and R reads MUST go to the designated nodes in the
preference list for the key.

Example:
  Key K's preference list: [A, B, C]
  W=2: write must succeed on 2 of {A, B, C}
  R=2: read must succeed on 2 of {A, B, C}

  If A and B are down → write FAILS (only C available, can't reach W=2)

Pros: Guarantees W+R>N overlap → strong consistency
Cons: Reduced availability when designated nodes are down
```

### Sloppy Quorum

```
Rule: If designated nodes are unreachable, use OTHER healthy nodes
to meet the quorum requirement.

Example:
  Key K's preference list: [A, B, C]
  Node A is down.
  
  Sloppy quorum: write to B, C, and D (D is not in the preference list)
  W=2 met: B + C = 2 ✓ (or B + D, or C + D)
  
  D holds the data temporarily (as a "hinted handoff")
  When A comes back, D sends the data to A

Pros: Higher availability — writes succeed even when designated nodes are down
Cons: W+R>N NO LONGER GUARANTEES consistency!

  Why? Consider:
  Write to {B, D} with W=2 (A and C were slow)
  Read from {A, C} with R=2

  Write set {B, D} and Read set {A, C} have ZERO OVERLAP!
  → The read returns stale data.

  This is why sloppy quorum provides "availability, not consistency."
```

### Our Choice: Strict Quorum by Default

```
We use STRICT quorum:
  - Writes and reads only go to the designated preference list nodes
  - If not enough designated nodes are available → fail with 503
  - This preserves the W+R>N consistency guarantee

Sloppy quorum is available as a fallback for ONE consistency level:
  - Client requests consistency=ONE
  - If the designated coordinator is down, the SDK routes to the next node on the ring
  - That node writes locally and stores a hint for the original coordinator
  - Eventual consistency only — no quorum guarantee

Why not sloppy quorum for QUORUM consistency?
  - It breaks the overlap guarantee
  - A client requesting QUORUM expects strong consistency
  - If we silently downgrade to sloppy quorum, we violate the consistency contract
  - Better to return 503 (explicit failure) than return stale data (silent failure)
```

---

## 10. Consistency During Network Partitions

```
Scenario: Network partition splits the cluster into two halves.

    ┌─────────────────┐     PARTITION     ┌─────────────────┐
    │   Partition 1    │ ═══════════════  │   Partition 2    │
    │                  │   (no comm)       │                  │
    │  Node A          │                   │  Node C          │
    │  Node B          │                   │  Node D          │
    │                  │                   │  Node E          │
    └─────────────────┘                   └─────────────────┘

Key K has preference list: [A, C, E] (one node in each partition!)


Case 1: Client in Partition 1, consistency=QUORUM (W=2)
  - Node A writes locally ✓
  - Tries to reach C and E → TIMEOUT (partitioned)
  - Only 1 of 3 → cannot achieve W=2
  - WRITE FAILS with 503 INSUFFICIENT_REPLICAS
  
  Correct behavior! Better to fail than to silently lose consistency.


Case 2: Client in Partition 1, consistency=ONE (W=1)
  - Node A writes locally ✓
  - Returns success to client
  - Meanwhile, client in Partition 2 writes same key via Node C
  - Both writes succeed independently
  
  When partition heals:
  - Read repair or anti-entropy detects divergence
  - LWW or vector clocks resolve the conflict


Case 3: Key K has preference list [A, B, D] (2 in P1, 1 in P2)
  Client in Partition 1, consistency=QUORUM:
  - Write to A ✓, B ✓ → W=2 met → SUCCESS
  - Node D gets the write later (via hints after partition heals)
  
  Client in Partition 2, consistency=QUORUM:
  - Write to D ✓ → only 1 of 3 → FAILS (can't reach A or B)
  
  Interesting asymmetry: the partition with more replicas wins.


Summary:
  ┌────────────────┬──────────────────┬──────────────────────────────┐
  │ Consistency     │ During Partition │ Behavior                      │
  │ Level          │                  │                               │
  ├────────────────┼──────────────────┼──────────────────────────────┤
  │ ONE            │ ✅ Available      │ Both sides accept writes.     │
  │                │                  │ Conflicts resolved later.     │
  ├────────────────┼──────────────────┼──────────────────────────────┤
  │ QUORUM         │ ⚠️ Degraded      │ Side with ≥W replicas works. │
  │                │                  │ Other side fails (503).       │
  ├────────────────┼──────────────────┼──────────────────────────────┤
  │ ALL            │ ❌ Unavailable    │ Both sides fail (need all    │
  │                │                  │ replicas, can't reach across).│
  └────────────────┴──────────────────┴──────────────────────────────┘
```

---

## 11. Layered Defense: How All Mechanisms Work Together

```
The consistency mechanisms form a layered defense, each catching what
the previous layer missed:

┌─────────────────────────────────────────────────────────────────────┐
│                                                                     │
│  Layer 0: QUORUM WRITES (W=2)                                      │
│  ────────────────────────────                                      │
│  Ensures data reaches at least W replicas before ACK.              │
│  Handles: Normal operations. Catches 99%+ of cases.               │
│  Misses: Replica down during write, async replication lag.         │
│                                                                     │
│  Layer 1: HINTED HANDOFF                                           │
│  ────────────────────────                                          │
│  Stores writes for downed replicas, delivers when they recover.    │
│  Handles: Temporary node failures (minutes to hours).              │
│  Misses: Hints expired (node down > 3h), hint holder also crashes. │
│                                                                     │
│  Layer 2: READ REPAIR                                              │
│  ─────────────────────                                             │
│  Fixes stale replicas when they're read.                           │
│  Handles: Any divergence on actively-read keys.                    │
│  Misses: Keys that are written but never/rarely read.              │
│                                                                     │
│  Layer 3: ANTI-ENTROPY (Merkle Trees)                              │
│  ────────────────────────────────────                              │
│  Background sweep that finds and fixes ALL divergence.             │
│  Handles: Everything — the safety net for all cases above.         │
│  Misses: Nothing (given enough time). But runs infrequently.       │
│                                                                     │
│                                                                     │
│  Together, these layers ensure:                                     │
│  1. Most writes are immediately consistent (quorum)                │
│  2. Temporary failures are healed within minutes (hinted handoff)  │
│  3. Read-heavy keys converge quickly (read repair)                 │
│  4. All keys converge eventually (anti-entropy)                    │
│                                                                     │
│  Time to consistency:                                               │
│  - Happy path (quorum met):        0 ms (immediately consistent)   │
│  - Node down < 3h:                 seconds-minutes (hinted handoff)│
│  - Key is frequently read:         seconds (read repair)           │
│  - Key is rarely read, node down:  up to 1 hour (anti-entropy)    │
│                                                                     │
└─────────────────────────────────────────────────────────────────────┘
```

### When Each Layer Kicks In

```
Scenario 1: Normal operation (all nodes healthy)
  → Layer 0 (quorum writes) → immediately consistent ✓
  → Layers 1-3 are idle

Scenario 2: Node C goes down for 5 minutes
  → Layer 0: writes achieve quorum without C (W=2 via A+B) ✓
  → Layer 1: hints stored for C
  → C comes back → hints delivered → C catches up
  → Layers 2-3 not needed

Scenario 3: Node C goes down for 6 hours (> hint window)
  → Layer 0: writes achieve quorum without C ✓
  → Layer 1: hints stored but expire after 3h → discarded
  → C comes back with stale data
  → Layer 2: read repair fixes keys as they're read
  → Layer 3: anti-entropy repair fixes remaining keys (within 1h)

Scenario 4: Network partition splits cluster
  → Layer 0: QUORUM operations on the majority side succeed ✓
  → Layer 0: ONE operations on both sides succeed (eventual consistency)
  → Partition heals → Layers 2-3 reconcile divergent data
  → Conflicts resolved via LWW or vector clocks

Scenario 5: Silent data corruption on Node A (bit rot)
  → Layer 0: doesn't detect (A thinks its data is fine)
  → Layer 1: doesn't detect (no node failure)
  → Layer 2: read repair detects mismatch on reads → fixes A's data
  → Layer 3: anti-entropy detects via Merkle tree hash mismatch → fixes
```

---

*This document complements the [interview simulation](interview-simulation.md), [flow diagrams](flow.md), and [datastore design](datastore-design.md).*