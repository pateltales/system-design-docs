# DynamoDB Storage Engine & Replication — Deep Dive

## Table of Contents

1. [Overview](#1-overview)
2. [Storage Engine Architecture](#2-storage-engine-architecture)
3. [B-tree Storage](#3-b-tree-storage)
4. [Write-Ahead Log (WAL)](#4-write-ahead-log-wal)
5. [Write Path](#5-write-path)
6. [Read Path](#6-read-path)
7. [Replication with Multi-Paxos](#7-replication-with-multi-paxos)
8. [Leader Election](#8-leader-election)
9. [Failure Modes and Recovery](#9-failure-modes-and-recovery)
10. [Comparison: DynamoDB vs Dynamo Paper](#10-comparison-dynamodb-vs-dynamo-paper)
11. [Interview Angles](#11-interview-angles)

---

## 1. Overview

Each DynamoDB partition is a self-contained storage unit replicated across 3 Availability
Zones. Understanding the storage engine and replication model is essential for reasoning
about durability, consistency, and latency guarantees.

**Key architectural facts:**

| Component | Choice | Rationale |
|-----------|--------|-----------|
| Storage structure | B-tree on SSD | Predictable read latency (vs LSM-tree's compaction pauses) |
| Durability mechanism | Write-ahead log (WAL) | Crash recovery without data loss |
| Replication protocol | Multi-Paxos | Leader-per-partition with majority commit |
| Replica count | 3 (across 3 AZs) | Tolerates 1 AZ failure |
| Write acknowledgement | Majority (2 of 3) | Durability before client response |
| Storage medium | SSD | Single-digit millisecond latency |

---

## 2. Storage Engine Architecture

### 2.1 Per-Partition Storage Stack

Each partition replica maintains its own storage stack [INFERRED based on AWS publications
and the 2022 USENIX ATC DynamoDB paper]:

```
┌────────────────────────────────────────────────────────┐
│                  Partition Replica                       │
├────────────────────────────────────────────────────────┤
│                                                        │
│  ┌────────────────────────────────────────────┐       │
│  │             B-tree (on SSD)                 │       │
│  │                                             │       │
│  │  Serves reads: GetItem, Query, Scan         │       │
│  │  Updated after WAL commit                    │       │
│  │  Sorted by: partition key hash + sort key   │       │
│  └────────────────────────────────────────────┘       │
│                                                        │
│  ┌────────────────────────────────────────────┐       │
│  │          Write-Ahead Log (WAL)              │       │
│  │                                             │       │
│  │  Sequential writes for durability           │       │
│  │  Written BEFORE B-tree update               │       │
│  │  Used for crash recovery                     │       │
│  └────────────────────────────────────────────┘       │
│                                                        │
│  ┌────────────────────────────────────────────┐       │
│  │          Paxos Replication Log              │       │
│  │                                             │       │
│  │  Ordered log of Paxos proposals             │       │
│  │  Ensures agreement across replicas          │       │
│  │  Leader uses to replicate writes            │       │
│  └────────────────────────────────────────────┘       │
│                                                        │
│  SSD Storage:                                          │
│  ├─ B-tree data pages                                  │
│  ├─ WAL segments                                       │
│  └─ Paxos log entries                                  │
│                                                        │
└────────────────────────────────────────────────────────┘
```

### 2.2 Why SSD?

DynamoDB's core promise is **single-digit millisecond latency** at any scale. SSD is
essential because:

| Storage | Random Read Latency | Sequential Write | DynamoDB Fit |
|---------|-------------------|-----------------|-------------|
| HDD | 5-10 ms | ~100 MB/s | Too slow for reads |
| SSD | 0.1-0.2 ms | ~500 MB/s | Excellent — leaves headroom for network + software |
| NVMe SSD | 0.02-0.05 ms | ~3 GB/s | Even better, likely used in newer hardware |

With SSD, the storage layer contributes < 1 ms to total latency, leaving headroom for
network hops, Paxos round trips, and request routing.

---

## 3. B-tree Storage

### 3.1 Why B-tree (Not LSM-tree)?

This is a critical design decision. Many NoSQL databases (Cassandra, RocksDB, LevelDB)
use LSM-trees. DynamoDB chose B-trees [INFERRED from AWS publications]:

| Property | B-tree | LSM-tree |
|----------|--------|----------|
| **Read latency** | Predictable, O(log N) | Variable — may need to check multiple levels |
| **Write latency** | Slightly slower (random I/O) | Fast (sequential writes to memtable) |
| **Write amplification** | Lower | Higher (compaction rewrites data) |
| **Read amplification** | Lower | Higher (bloom filters help, but not perfect) |
| **Space amplification** | Higher | Lower (compaction reclaims space) |
| **Compaction pauses** | None | Periodic compaction causes latency spikes |
| **Predictability** | High | Variable (depends on compaction state) |

**DynamoDB's reasoning [INFERRED]:**
- Single-digit millisecond guarantee requires **predictable** latency
- LSM-tree compaction can cause unpredictable latency spikes (P99 degradation)
- B-tree provides consistent read performance regardless of write history
- With SSD, B-tree's random I/O penalty is minimal
- DynamoDB prefers **consistency of performance** over raw write throughput

### 3.2 B-tree Structure

```
                    ┌──────────────────┐
                    │    Root Node     │
                    │  [K1 | K2 | K3] │
                    └───┬──┬──┬──┬────┘
                       │  │  │  │
              ┌────────┘  │  │  └────────┐
              ▼           ▼  ▼           ▼
        ┌──────────┐  ┌──────────┐  ┌──────────┐
        │ Internal │  │ Internal │  │ Internal │
        │ [K4|K5]  │  │ [K6|K7]  │  │ [K8|K9]  │
        └──┬──┬──┬─┘  └──┬──┬──┬─┘  └──┬──┬──┬─┘
           │  │  │        │  │  │        │  │  │
           ▼  ▼  ▼        ▼  ▼  ▼        ▼  ▼  ▼
        ┌─────┐┌─────┐ ┌─────┐┌─────┐ ┌─────┐┌─────┐
        │Leaf ││Leaf │ │Leaf ││Leaf │ │Leaf ││Leaf │
        │Data ││Data │ │Data ││Data │ │Data ││Data │
        └─────┘└─────┘ └─────┘└─────┘ └─────┘└─────┘

  Keys in B-tree: hash(partition_key) + sort_key
  → Items with same partition key are physically adjacent
  → Within a partition key, items sorted by sort key
  → Enables efficient range scans (Query operations)
```

### 3.3 Key Ordering in B-tree

The B-tree is organized by:
1. **Hash of partition key** — determines which partition
2. **Sort key (raw value)** — determines order within partition key

```
B-tree key space for one partition:

  hash("customer-A") || "order-001"
  hash("customer-A") || "order-002"
  hash("customer-A") || "order-003"
  hash("customer-B") || "order-001"
  hash("customer-B") || "order-002"
  ...

  Query(PK = "customer-A") scans a contiguous range in the B-tree
  → Very efficient, single sequential read
```

---

## 4. Write-Ahead Log (WAL)

### 4.1 Purpose

The WAL ensures durability: every write is recorded in a sequential log on SSD **before**
the B-tree is updated. If the system crashes, the WAL is replayed to recover any writes
that were committed but not yet applied to the B-tree.

### 4.2 WAL Write Flow

```
┌──────────────────────────────────────────────────────────┐
│                   WAL Write Flow                          │
├──────────────────────────────────────────────────────────┤
│                                                          │
│  1. Write request arrives at partition leader            │
│                                                          │
│  2. Append write to WAL (sequential write to SSD)        │
│     → This is the point of durability                    │
│     → If crash after WAL write, data is recoverable      │
│                                                          │
│  3. Replicate WAL entry via Paxos to followers           │
│     → Wait for majority ACK (2 of 3)                    │
│                                                          │
│  4. After Paxos commit: update B-tree in memory/SSD      │
│     → This may happen asynchronously                     │
│     → B-tree update is NOT on the critical path          │
│                                                          │
│  5. Return success to client                             │
│     → After step 3 (Paxos commit), NOT after step 4     │
│                                                          │
└──────────────────────────────────────────────────────────┘
```

### 4.3 Crash Recovery

```
On replica restart after crash:

  1. Read WAL from last checkpoint
  2. Replay all committed WAL entries not yet in B-tree
  3. B-tree is now current
  4. Resume serving reads/writes

  WAL ensures: no committed write is ever lost
  B-tree ensures: reads are fast (no WAL scan needed)
```

---

## 5. Write Path

### 5.1 End-to-End Write Flow

```
Client          Request Router       Leader (AZ-a)        Follower (AZ-b)    Follower (AZ-c)
  │                  │                    │                      │                  │
  │  PutItem         │                    │                      │                  │
  │─────────────────▶│                    │                      │                  │
  │                  │  hash(PK) → P3     │                      │                  │
  │                  │  P3 leader = AZ-a  │                      │                  │
  │                  │───────────────────▶│                      │                  │
  │                  │                    │                      │                  │
  │                  │                    │ 1. Validate item     │                  │
  │                  │                    │    (size ≤ 400 KB,   │                  │
  │                  │                    │     PK exists, etc.) │                  │
  │                  │                    │                      │                  │
  │                  │                    │ 2. Append to WAL     │                  │
  │                  │                    │                      │                  │
  │                  │                    │ 3. Paxos Prepare     │                  │
  │                  │                    │─────────────────────▶│                  │
  │                  │                    │─────────────────────────────────────────▶│
  │                  │                    │                      │                  │
  │                  │                    │ 4. Paxos Accept      │                  │
  │                  │                    │◀─────────────────────│ Promise          │
  │                  │                    │◀─────────────────────────────────────────│
  │                  │                    │                      │                  │
  │                  │                    │    Majority (2/3)    │                  │
  │                  │                    │    achieved          │                  │
  │                  │                    │                      │                  │
  │                  │                    │ 5. Commit: update    │                  │
  │                  │                    │    B-tree            │                  │
  │                  │                    │                      │                  │
  │                  │◀───────────────────│ 6. 200 OK           │                  │
  │◀─────────────────│                    │                      │                  │
  │  200 OK          │                    │                      │                  │
  │                  │                    │ 7. Async: notify     │                  │
  │                  │                    │    followers to      │                  │
  │                  │                    │    apply to B-tree   │                  │
  │                  │                    │─────────────────────▶│                  │
  │                  │                    │─────────────────────────────────────────▶│
```

### 5.2 Write Latency Breakdown [INFERRED]

| Step | Typical Latency | Notes |
|------|----------------|-------|
| Client → Request Router | 1-2 ms | Network, TLS |
| Request Router → Leader | < 1 ms | Within-region routing |
| WAL append (SSD) | < 0.5 ms | Sequential SSD write |
| Paxos round trip (2 AZs) | 1-3 ms | Network to 2 followers + back |
| B-tree update | < 0.5 ms | Can be async |
| **Total** | **~3-7 ms** | Single-digit millisecond |

### 5.3 Conditional Writes

DynamoDB supports conditional writes (PutItem with ConditionExpression):

```
1. Leader reads current item from B-tree
2. Evaluates condition expression against current item
3. If condition is TRUE → proceed with write (WAL + Paxos)
4. If condition is FALSE → return ConditionalCheckFailedException
5. The read + condition check + write is atomic on the leader
```

This is the foundation for optimistic concurrency control in DynamoDB.

---

## 6. Read Path

### 6.1 Eventually Consistent Read

```
Client          Request Router       ANY Replica (e.g., Follower AZ-b)
  │                  │                    │
  │  GetItem(EC)     │                    │
  │─────────────────▶│                    │
  │                  │  hash(PK) → P3     │
  │                  │  EC read → any     │
  │                  │───────────────────▶│
  │                  │                    │
  │                  │                    │ Read from B-tree
  │                  │                    │
  │                  │◀───────────────────│ Item data
  │◀─────────────────│                    │
  │  Response        │                    │
```

**Properties:**
- Can be served by **any** of the 3 replicas
- Data may be slightly stale (follower hasn't applied latest Paxos commits)
- Typical staleness: milliseconds [INFERRED]
- Cost: 0.5 RCU per 4 KB (half the cost of strongly consistent)
- Better availability: can be served even if leader is unavailable

### 6.2 Strongly Consistent Read

```
Client          Request Router       Leader (AZ-a)
  │                  │                    │
  │  GetItem(SC)     │                    │
  │─────────────────▶│                    │
  │                  │  hash(PK) → P3     │
  │                  │  SC read → LEADER  │
  │                  │───────────────────▶│
  │                  │                    │
  │                  │                    │ Verify I'm still leader
  │                  │                    │ Read from B-tree
  │                  │                    │
  │                  │◀───────────────────│ Item data
  │◀─────────────────│                    │
  │  Response        │                    │
```

**Properties:**
- Must be served by the **leader** replica only
- Leader must verify it's still the leader (lease check) [INFERRED]
- Returns the most recent committed write
- Cost: 1.0 RCU per 4 KB (2x eventually consistent)
- Lower availability: if leader is down, strongly consistent reads fail
  (until new leader is elected)

### 6.3 Read Latency Comparison

| Read Type | Typical Latency | Cost | Availability |
|-----------|----------------|------|-------------|
| Eventually consistent | 1-5 ms | 0.5 RCU / 4 KB | Higher (any replica) |
| Strongly consistent | 1-5 ms | 1.0 RCU / 4 KB | Lower (leader only) |
| DAX (cache hit) | < 1 ms (microseconds) | DAX node cost | High (in-memory) |

### 6.4 Query vs GetItem vs Scan

| Operation | What It Does | Key Requirement | Consistency Options |
|-----------|-------------|----------------|-------------------|
| **GetItem** | Retrieve one item | Full primary key (PK + SK) | EC or SC |
| **Query** | Retrieve multiple items with same PK | Partition key required, optional SK conditions | EC or SC |
| **Scan** | Read every item in table | None (reads everything) | EC or SC |

**Query efficiency:**
- Items with the same partition key are stored contiguously in the B-tree
- Query on PK = sequential B-tree scan → very efficient
- Sort key conditions (=, <, >, BETWEEN, begins_with) are range scans on B-tree

---

## 7. Replication with Multi-Paxos

### 7.1 Why Paxos?

DynamoDB uses Multi-Paxos (not basic Paxos, not Raft) for replication [INFERRED from
AWS publications and the 2022 USENIX ATC paper]:

| Protocol | Used By | Key Property |
|----------|---------|--------------|
| Basic Paxos | Academic | One agreement per round |
| **Multi-Paxos** | **DynamoDB** [INFERRED] | Optimized for sequential agreements with stable leader |
| Raft | etcd, CockroachDB | Simplified Paxos with strong leader |

**Multi-Paxos optimization:** Once a leader is established, subsequent writes skip the
Prepare phase and go directly to Accept. This reduces latency from 2 round trips
(Prepare + Accept) to 1 round trip (Accept only).

### 7.2 Paxos Roles

For each partition:

| Role | Count | Responsibilities |
|------|-------|-----------------|
| **Leader (Proposer)** | 1 | Proposes writes, coordinates Paxos, serves SC reads |
| **Followers (Acceptors)** | 2 | Vote on proposals, serve EC reads |
| **All 3 are Learners** | 3 | Apply committed values to B-tree |

### 7.3 Normal Write (Steady State — Leader Established)

With Multi-Paxos and a stable leader, the Prepare phase is skipped:

```
Leader              Follower-1          Follower-2
  │                     │                    │
  │  Accept(seq=42,     │                    │
  │   PutItem data)     │                    │
  │────────────────────▶│                    │
  │─────────────────────────────────────────▶│
  │                     │                    │
  │  Accepted(42)       │                    │
  │◀────────────────────│                    │
  │  Accepted(42)       │                    │
  │◀─────────────────────────────────────────│
  │                     │                    │
  │  Majority ACK (2/3) │                    │
  │  → COMMITTED        │                    │
  │                     │                    │
  │  Commit(42)         │                    │
  │────────────────────▶│                    │
  │─────────────────────────────────────────▶│
```

**Latency:** 1 round trip (Accept → Accepted) + commit notification.

### 7.4 Write Durability Guarantee

A write is **committed** (and acknowledged to the client) when:
- The leader has written to its WAL
- At least 1 follower has written to its WAL
- Total: **2 of 3 replicas** have the write on durable storage

This means:
- If any single replica (including the leader) fails → write is safe
- Only simultaneous failure of 2 specific replicas loses the write
- Probability of losing a committed write ≈ 10^-11 per year [INFERRED]

### 7.5 Follower Catch-Up

If a follower falls behind (network partition, restart):

```
Leader              Slow Follower
  │                     │
  │  "You're behind.    │
  │   Your last seq     │
  │   = 38, current     │
  │   = 42"             │
  │                     │
  │  Log entries        │
  │  39, 40, 41, 42     │
  │────────────────────▶│
  │                     │
  │                     │ Apply entries
  │                     │ to WAL + B-tree
  │                     │
  │  "Caught up to 42"  │
  │◀────────────────────│
```

---

## 8. Leader Election

### 8.1 When Leader Election Occurs

- Current leader becomes unreachable (node failure, AZ failure, network partition)
- Leader voluntarily steps down (maintenance, rebalancing)
- Leader lease expires without renewal

### 8.2 Election Process [INFERRED]

```
┌──────────────────────────────────────────────────────┐
│              Leader Election via Paxos                 │
├──────────────────────────────────────────────────────┤
│                                                      │
│  1. Follower detects leader failure                  │
│     (heartbeat timeout)                              │
│                                                      │
│  2. Follower becomes Candidate                       │
│     Sends Prepare(proposal_number) to all replicas   │
│                                                      │
│  3. Other replicas respond with Promise              │
│     (if proposal_number > any they've seen)          │
│                                                      │
│  4. Candidate receives majority Promises (2 of 3)    │
│     → Becomes new Leader                             │
│                                                      │
│  5. New leader checks: any uncommitted entries?      │
│     → If yes, commits them first (Paxos guarantee)   │
│                                                      │
│  6. New leader starts serving writes and SC reads    │
│                                                      │
│  Typical election time: ~1-3 seconds [INFERRED]      │
│                                                      │
└──────────────────────────────────────────────────────┘
```

### 8.3 Leader Lease

To prevent split-brain (two leaders for the same partition):

```
Leader maintains a lease:
  - Periodically renewed with followers
  - If lease expires: leader stops serving writes and SC reads
  - Other replica can start election after lease expiry

This ensures:
  - At most ONE leader per partition at any time
  - Brief unavailability during leader transition
  - No split-brain writes
```

### 8.4 Impact on Availability

| Scenario | Writes | SC Reads | EC Reads |
|----------|--------|----------|----------|
| All 3 replicas healthy | ✓ | ✓ | ✓ |
| 1 follower down | ✓ (majority still exists) | ✓ | ✓ (2 replicas available) |
| Leader down | ✗ (until new leader elected) | ✗ | ✓ (followers available) |
| 1 AZ down (leader in that AZ) | ✗ briefly → ✓ after election | ✗ briefly → ✓ | ✓ |
| 2 AZs down | ✗ (no majority) | ✗ | Maybe (1 replica) |

**Key insight:** Eventually consistent reads remain available during leader election.
Strongly consistent reads and writes are briefly unavailable (seconds).

---

## 9. Failure Modes and Recovery

### 9.1 Single Replica Failure

```
Scenario: Follower in AZ-b fails

Impact:
  - Writes: Still work (leader + AZ-c follower = majority)
  - SC reads: Still work (leader is fine)
  - EC reads: Still work (leader + AZ-c follower available)
  - Durability: Slightly reduced (2 copies instead of 3)

Recovery:
  - DynamoDB detects failure, provisions replacement replica
  - New replica catches up from leader's Paxos log
  - Once caught up, returns to full 3-replica state
```

### 9.2 Leader Failure

```
Scenario: Leader in AZ-a fails

Impact:
  - Writes: Unavailable until new leader elected (~1-3 seconds)
  - SC reads: Unavailable until new leader elected
  - EC reads: Available (followers still serving)

Recovery:
  - One of the followers detects heartbeat timeout
  - Initiates Paxos election
  - Becomes new leader
  - Uncommitted entries are resolved
  - Writes and SC reads resume
```

### 9.3 AZ Failure

```
Scenario: Entire AZ-a goes down (leader was in AZ-a)

Impact:
  - All partitions with leaders in AZ-a lose their leader
  - Those partitions: writes + SC reads unavailable briefly
  - EC reads: available from surviving AZs
  - Partitions with leaders in AZ-b or AZ-c: unaffected

Recovery:
  - Leader elections across all affected partitions
  - New leaders elected from AZ-b and AZ-c replicas
  - Full availability restored in seconds
  - DynamoDB rebalances to replace AZ-a replicas
```

### 9.4 Network Partition

```
Scenario: AZ-a can talk to AZ-b but not AZ-c.
          Leader is in AZ-a.

Impact:
  - Leader (AZ-a) + Follower (AZ-b) = majority → writes still work
  - Follower in AZ-c: isolated but doesn't start election
    (it can't get majority alone)
  - EC reads to AZ-c: may return stale data (follower is behind)

Recovery:
  - When partition heals, AZ-c follower catches up from Paxos log
```

---

## 10. Comparison: DynamoDB vs Dynamo Paper

**CRITICAL:** These are different systems. The Dynamo paper (2007) describes an internal
Amazon system. DynamoDB (the AWS service) evolved significantly from it.

| Aspect | Dynamo Paper (2007) | DynamoDB (AWS Service) |
|--------|--------------------|-----------------------|
| **Replication** | Leaderless, sloppy quorum | **Leader-per-partition with Paxos** |
| **Consistency** | Always eventual | **Eventual (default) + Strong (opt-in)** |
| **Conflict resolution** | Vector clocks, app-resolved | **Not needed (leader serializes writes)** |
| **Partitioning** | Consistent hashing, virtual nodes | **Hash-range partitioning, auto-split** |
| **Membership** | Gossip protocol | **Centralized partition map** [INFERRED] |
| **Read repair** | Yes (during reads) | **Not needed (Paxos ensures consistency)** |
| **Hinted handoff** | Yes (sloppy quorum) | **Not applicable (leader-based)** |
| **Write path** | Write to N replicas, W must ACK | **Write to leader, Paxos majority ACK** |
| **Read path** | Read from R replicas, return latest | **EC: any replica. SC: leader** |
| **Anti-entropy** | Merkle trees | **Paxos log catch-up** [INFERRED] |

### Why DynamoDB Diverged

The Dynamo paper optimized for **availability over consistency** (AP in CAP theorem).
DynamoDB needed to offer **strong consistency as an option** while maintaining availability
for eventually consistent reads. Leader-based Paxos achieves this:

1. **Strong consistency:** Read from the leader, which has the latest committed write
2. **Eventual consistency:** Read from any replica (fast, available, but maybe stale)
3. **Simpler writes:** No conflict resolution needed — leader serializes all writes
4. **No vector clocks:** Application developers don't need to resolve conflicts
5. **Predictable behavior:** Every write goes through one path (leader → Paxos → commit)

---

## 11. Interview Angles

### 11.1 "Walk me through a DynamoDB write from client to durable storage"

```
1. Client calls PutItem via SDK → HTTPS to DynamoDB endpoint
2. Request router hashes partition key → determines partition P
3. Router looks up partition map → finds P's leader (AZ-a)
4. Routes request to leader
5. Leader validates: item ≤ 400 KB, primary key valid, etc.
6. Leader appends to local WAL (sequential SSD write)
7. Leader sends Paxos Accept to both followers
8. Followers append to their WALs, send Accepted back
9. Leader receives majority (2/3) ACK → write is COMMITTED
10. Leader updates B-tree (may be async)
11. Returns 200 OK to client via router
12. Followers update their B-trees asynchronously

Durability guarantee: write is on 2+ replicas' WALs before client gets 200
Latency: ~3-7 ms total
```

### 11.2 "Why B-tree and not LSM-tree?"

"DynamoDB promises single-digit millisecond latency at P99. LSM-trees are great for
write-heavy workloads but have unpredictable read latency due to compaction. When
compaction runs, reads must check multiple levels and may hit P99 spikes. B-trees
provide consistent O(log N) reads regardless of write activity. On SSD, the random
I/O penalty of B-trees is minimal. DynamoDB optimized for **predictable latency** over
raw write throughput."

### 11.3 "What happens if the leader fails during a write?"

```
Case 1: Write already committed (majority ACK received)
  → Client already got 200 OK
  → Write is durable on 2 replicas
  → New leader election, new leader has the write
  → No data loss

Case 2: Write not yet committed (only on leader's WAL)
  → Client has NOT received 200 OK (request in flight)
  → Leader fails, write is only on 1 replica
  → If that replica recovers before election: write may be committed by new leader
  → If that replica doesn't recover: write is lost
  → Client sees timeout/error, SDK retries the write
  → No data loss from client's perspective (idempotent writes or retry)

Key insight: DynamoDB only acknowledges writes AFTER Paxos commit,
so clients never see a successful write that's later lost.
```

### 11.4 "How does DynamoDB achieve single-digit ms latency?"

```
Optimization stack:
  1. SSD storage: < 0.5 ms for reads and writes
  2. B-tree: predictable O(log N) lookups, no compaction pauses
  3. Multi-Paxos: stable leader skips Prepare phase (1 RT instead of 2)
  4. Partition locality: items with same PK stored contiguously
  5. Request router caching: partition map cached for fast routing
  6. Connection pooling: persistent connections reduce TLS overhead
  7. Replication within region: cross-AZ latency < 2 ms

Typical breakdown:
  Network (client → DDB): 1-2 ms
  Routing: < 0.5 ms
  Storage I/O: < 0.5 ms
  Paxos (for writes): 1-3 ms
  Total: 3-7 ms
```

### 11.5 "Explain the difference between the Dynamo paper and DynamoDB"

This is an extremely common interview question for Amazon SDEs. See the comparison
table in Section 10. The key message:

"The Dynamo paper described a **leaderless**, always-available system with vector clocks
for conflict resolution. DynamoDB is a **leader-based** system using Paxos, offering both
eventual and strong consistency. This is a fundamental architectural difference — DynamoDB
trades a tiny bit of write availability (leader dependency) for much simpler semantics
(no application-level conflict resolution) and strong consistency support."

---

## Appendix: Key Numbers

| Property | Value |
|----------|-------|
| Replicas per partition | 3 (across 3 AZs) |
| Write commit quorum | 2 of 3 (majority) |
| Item size limit | 400 KB |
| Partition key max length | 2,048 bytes |
| Sort key max length | 1,024 bytes |
| Nested attribute depth | 32 levels |
| RCU: 1 strongly consistent read | 4 KB |
| RCU: 1 eventually consistent read | 4 KB at 0.5 RCU |
| WCU: 1 write | 1 KB |
| Per-partition throughput | 3,000 RCU / 1,000 WCU |
| Typical write latency | 3-7 ms |
| Typical read latency | 1-5 ms |
| DAX cache latency | Microseconds |
| Leader election time | ~1-3 seconds [INFERRED] |
