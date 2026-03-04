# DynamoDB Global Tables — Deep Dive

## Table of Contents

1. [Overview](#1-overview)
2. [Architecture](#2-architecture)
3. [MREC: Multi-Region Eventual Consistency](#3-mrec-multi-region-eventual-consistency)
4. [MRSC: Multi-Region Strong Consistency](#4-mrsc-multi-region-strong-consistency)
5. [Conflict Resolution](#5-conflict-resolution)
6. [Replication Mechanics](#6-replication-mechanics)
7. [Transactions in Global Tables](#7-transactions-in-global-tables)
8. [Settings Synchronization](#8-settings-synchronization)
9. [Operational Concerns](#9-operational-concerns)
10. [Interview Angles](#10-interview-angles)

---

## 1. Overview

DynamoDB Global Tables provide fully managed, multi-region, multi-active replication.
Any replica can serve both reads and writes.

| Property | MREC (Default) | MRSC |
|----------|---------------|------|
| Replication | Asynchronous | Synchronous |
| Consistency across regions | Eventually consistent | Strongly consistent |
| Conflict resolution | Last-writer-wins (timestamp) | No conflicts (serialized) |
| Minimum regions | 2+ | Exactly 3 |
| RPO | ~seconds | Zero |
| Write latency | Low (local) | Higher (cross-region sync) |
| LSI support | Yes | No |
| TTL support | Yes | No |
| Transactions | Region-local only | Not supported |
| Streams | Enabled by default, used for replication | Not used for replication, optional |

**Version:** Current is 2019.11.21 (v2). Legacy 2017.11.29 is deprecated.

---

## 2. Architecture

### 2.1 Multi-Region Active-Active

```
┌────────────────────┐        ┌────────────────────┐        ┌────────────────────┐
│    us-east-1       │        │    eu-west-1       │        │    ap-southeast-1  │
│                    │        │                    │        │                    │
│  ┌──────────────┐  │        │  ┌──────────────┐  │        │  ┌──────────────┐  │
│  │ Table Replica │  │◀──────▶│  │ Table Replica │  │◀──────▶│  │ Table Replica │  │
│  │ (read+write) │  │  async  │  │ (read+write) │  │  async  │  │ (read+write) │  │
│  └──────────────┘  │  repl   │  └──────────────┘  │  repl   │  └──────────────┘  │
│                    │        │                    │        │                    │
│  Same table name   │        │  Same table name   │        │  Same table name   │
│  Same key schema   │        │  Same key schema   │        │  Same key schema   │
│  Same GSIs         │        │  Same GSIs         │        │  Same GSIs         │
│                    │        │                    │        │                    │
└────────────────────┘        └────────────────────┘        └────────────────────┘
```

**Key architectural properties:**
- Every replica has the same table name, primary key schema, and GSI definitions
- Any replica can independently serve reads AND writes
- No single "primary" region — all are equal (multi-active)
- Application connects to the nearest regional DynamoDB endpoint
- Replication is transparent to the application

### 2.2 Replica Types (MRSC Only)

MRSC supports two types of replicas:

| Type | Capability | Use Case |
|------|-----------|----------|
| **Full replica** | Read + write | Application traffic |
| **Witness** | Holds replicated data, no read/write | Quorum participant only, lower cost |

MRSC requires exactly 3 regions: can be 3 full replicas or 2 full + 1 witness.

---

## 3. MREC: Multi-Region Eventual Consistency

### 3.1 How MREC Works

```
┌──────────────────┐              ┌──────────────────┐
│  Region A         │              │  Region B         │
│                   │              │                   │
│  PutItem(X = 10)  │              │                   │
│       │           │              │                   │
│       ▼           │              │                   │
│  Paxos commit     │              │                   │
│  (local, 3 AZ)    │              │                   │
│       │           │              │                   │
│       ▼           │              │                   │
│  200 OK to client │              │                   │
│       │           │              │                   │
│       ▼           │              │                   │
│  DDB Stream record│              │                   │
│       │           │              │                   │
│       └───────── async ─────────▶│  Apply write      │
│                   │  (~0.5-2.5s)  │  X = 10           │
│                   │              │                   │
└──────────────────┘              └──────────────────┘
```

### 3.2 Replication Latency

- Typical: **within 1 second** under normal conditions
- Range: **0.5 to 2.5 seconds** depending on region distance
- Published as `ReplicationLatency` CloudWatch metric per source-destination pair
- us-west-1 → us-west-2: lower latency (same continent)
- us-west-1 → af-south-1: higher latency (cross-continent)

### 3.3 Strongly Consistent Reads in MREC

**Critical nuance:**

| Scenario | SC Read Returns |
|----------|----------------|
| Item last written in **current region** | Latest value (correct) |
| Item last written in **different region** | **May return stale data!** |

```
t0: Write X=10 in us-east-1
t1: SC read X in us-east-1 → returns 10 ✓ (local write)
t2: SC read X in eu-west-1 → may return OLD value ✗ (replication lag)
t3: (1-2 seconds later) SC read X in eu-west-1 → returns 10 ✓
```

**This is a common interview gotcha.** SC reads in MREC Global Tables only guarantee
the latest value if the write was made in the same region.

### 3.4 Conditional Writes in MREC

Conditional writes (ConditionExpression) evaluate against the **local replica's version**:

```
Region A: Item X = {version: 1, color: "red"}
Region B: Replication lag — still has X = {version: 1, color: "red"}

Concurrent writes:
  Region A: UpdateItem(X, SET color = "blue", Condition: version = 1)
    → Condition passes (local version = 1) → X = {version: 2, color: "blue"}

  Region B: UpdateItem(X, SET color = "green", Condition: version = 1)
    → Condition passes (local version = 1) → X = {version: 2, color: "green"}

Both succeed! Conflict resolved by LWW based on timestamp.
```

---

## 4. MRSC: Multi-Region Strong Consistency

### 4.1 How MRSC Works

MRSC uses **synchronous replication** instead of streams-based async replication:

```
┌──────────────────┐              ┌──────────────────┐
│  Region A         │              │  Region B         │
│                   │              │                   │
│  PutItem(X = 10)  │              │                   │
│       │           │              │                   │
│       ▼           │              │                   │
│  Local Paxos +    │              │                   │
│  Sync replicate ──│──── sync ───▶│  Apply write      │
│                   │              │  X = 10            │
│       │           │              │       │           │
│       ▼           │              │       ▼           │
│  Both confirmed   │              │  Confirmed         │
│       │           │              │                   │
│       ▼           │              │                   │
│  200 OK to client │              │                   │
│                   │              │                   │
└──────────────────┘              └──────────────────┘

Write only returns 200 OK AFTER synchronous replication completes.
```

### 4.2 MRSC Properties

| Property | Value |
|----------|-------|
| RPO | Zero (no data loss on region failure) |
| SC reads | Return latest value regardless of write region |
| Concurrent write handling | `ReplicatedWriteConflictException` (retry) |
| Regions | Exactly 3 (within the same region set) |
| Region sets | US, EU, AP |
| Write latency | Higher (cross-region sync on critical path) |
| Transactions | NOT supported |
| TTL | NOT supported |
| LSI | NOT supported |

### 4.3 Conflict Handling in MRSC

MRSC prevents conflicts instead of resolving them:

```
Region A: UpdateItem(X, SET color = "blue")  → in progress
Region B: UpdateItem(X, SET color = "green") → concurrent

  Because MRSC synchronously coordinates:
    → Region B gets ReplicatedWriteConflictException
    → Region B can safely retry
    → No LWW needed, no data loss, no surprise overwrites
```

### 4.4 MRSC vs MREC Tradeoffs

```
Choose MREC when:
  ✓ Low write latency is critical (writes are local)
  ✓ Can tolerate stale SC reads from non-local writes
  ✓ Need transactions (region-local)
  ✓ Need TTL or LSI
  ✓ RPO of a few seconds is acceptable
  ✓ Need 2+ regions (not limited to 3)

Choose MRSC when:
  ✓ Must have strongly consistent reads across ALL regions
  ✓ Zero RPO is required (financial, compliance)
  ✓ Concurrent cross-region writes to same item are rare
    (otherwise, lots of ReplicatedWriteConflictExceptions)
  ✓ Can accept higher write latency
  ✓ Don't need transactions, TTL, or LSI
```

---

## 5. Conflict Resolution

### 5.1 Last-Writer-Wins (LWW) in MREC

When the same item is modified in multiple regions simultaneously:

```
Region A:                              Region B:
  t0: X = {color: "red"}              t0: X = {color: "red"}
  t1: UpdateItem(color = "blue")       t2: UpdateItem(color = "green")
      timestamp = 1000                      timestamp = 1001

Replication:
  Region A receives B's write: timestamp 1001 > 1000 → X = green
  Region B receives A's write: timestamp 1000 < 1001 → keep X = green

Both regions converge to X = {color: "green"}  (latest timestamp wins)
```

### 5.2 LWW Risks

| Risk | Scenario | Impact |
|------|----------|--------|
| **Silent data loss** | User A updates in us-east-1, User B updates in eu-west-1 within replication window | One user's update is silently overwritten |
| **Clock skew** | Region A's clock is ahead | Region A's writes always win, even if Region B wrote later in real time |
| **Non-intuitive** | Two valid updates → one is silently discarded | No notification, no error, no merge |

### 5.3 Conflict Avoidance Strategies

```
Strategy 1: Region-affinity writes
  → Route all writes for a given PK to the same region
  → No concurrent cross-region writes → no conflicts
  → Use Route 53 latency-based routing with PK-based affinity

Strategy 2: Conditional writes with version counter
  → UpdateItem with Condition: version = :expected
  → If conflict, one write fails → application retries
  → Doesn't prevent LWW, but detects conflicts

Strategy 3: Append-only pattern
  → Never update items, only insert new items
  → PK = entity, SK = timestamp
  → No conflicting updates possible
  → Read latest by querying with ScanIndexForward = false, Limit = 1

Strategy 4: Use MRSC
  → Synchronous replication prevents conflicts entirely
  → Concurrent writes get ReplicatedWriteConflictException
```

### 5.4 LWW Granularity

LWW is applied at the **item level**, not the attribute level:

```
Region A: UpdateItem(X, SET color = "blue")    — timestamp 1000
Region B: UpdateItem(X, SET size = "large")    — timestamp 1001

You might expect: X = {color: "blue", size: "large"} (merge both)
Actual result:    X = {color: "red", size: "large"}   (B's full write wins)

The entire item from Region B's write wins, overwriting Region A's color change.
```

**This is a common source of data loss.** Update only the attributes you intend
to change (not the full item), but be aware that the entire write operation is
the unit of LWW comparison.

---

## 6. Replication Mechanics

### 6.1 Streams-Based Replication (MREC)

```
┌─────────────────────────────────────────────────────────────┐
│              MREC Replication Flow                            │
├─────────────────────────────────────────────────────────────┤
│                                                             │
│  1. Write committed to Region A (local Paxos)              │
│  2. Stream record created in Region A's DDB Stream         │
│  3. Replication process reads stream record                │
│  4. Replication process writes to Region B                 │
│  5. Region B applies write (with LWW check)                │
│  6. Stream record created in Region B's stream             │
│     → BUT: Region B's stream record is marked as           │
│        replicated (not local), so it doesn't get           │
│        re-replicated back to Region A (no infinite loop)   │
│                                                             │
│  Note: Multiple changes in short period may be              │
│  combined into a single replicated write                    │
│                                                             │
└─────────────────────────────────────────────────────────────┘
```

**DDB Streams behavior in MREC:**
- Streams are enabled by default on all MREC replicas — cannot be disabled
- Stream records may differ slightly between replicas
- Per-item ordering is guaranteed, cross-item ordering may differ between replicas
- Replication process consumes 1 of 2 reader slots per shard

### 6.2 Synchronous Replication (MRSC)

MRSC does NOT use DynamoDB Streams for replication:
- Replication is part of the write path itself
- Write only returns success after cross-region sync
- Streams can be enabled optionally (for CDC, not replication)
- Stream records are identical across all replicas in MRSC

### 6.3 Write Capacity for Replication

Replication consumes write capacity on destination replicas:

```
Region A: 1,000 writes/sec → 1,000 WCU consumed locally
Region B: receives 1,000 replicated writes/sec → 1,000 WCU consumed

Total write capacity needed per region: local writes + replicated writes

If Region A gets 800 writes/sec and Region B gets 200 writes/sec:
  Region A needs: 800 (local) + 200 (from B) = 1,000 WCU
  Region B needs: 200 (local) + 800 (from A) = 1,000 WCU
```

---

## 7. Transactions in Global Tables

### 7.1 MREC: Region-Local Transactions

```
TransactWriteItems in MREC:
  - Atomic WITHIN the region where invoked
  - Items in the transaction are replicated individually (not atomically)
  - Other regions may see partial transaction results during replication

Example:
  Region A: TransactWriteItems([WriteA, WriteB, WriteC])
    → All 3 writes committed atomically in Region A ✓
    → Replication to Region B: WriteA arrives, WriteB arrives, WriteC arrives
    → Between arrivals, Region B may see: A done, B done, C not yet
    → Eventually all 3 are replicated ✓

For cross-region transaction consistency: use MRSC... except MRSC doesn't
support transactions at all. This is a genuine gap.
```

### 7.2 MRSC: No Transaction Support

Transactions are NOT supported on MRSC tables. Invoking TransactWriteItems or
TransactGetItems returns an error. This is because synchronous cross-region
coordination for transactions would have prohibitively high latency and complexity.

---

## 8. Settings Synchronization

### 8.1 Always Synchronized Across Replicas

- Capacity mode (provisioned / on-demand)
- Provisioned write capacity and write auto-scaling
- Key schema attributes
- GSI definitions, GSI write capacity, GSI write auto-scaling
- SSE (encryption) type
- Streams definition (MREC)
- TTL settings
- On-demand maximum write throughput

### 8.2 Synchronized but Overridable Per Replica

- Table provisioned **read** capacity and read auto-scaling
- GSI provisioned **read** capacity and read auto-scaling
- Table class (Standard / Standard-IA)
- On-demand maximum **read** throughput

**Why read capacity is overridable:** Different regions may have different read traffic
patterns. A region serving as the primary for a geographic area needs more read capacity
than a DR-only replica.

### 8.3 Never Synchronized

- Deletion protection
- Point-in-time recovery (PITR)
- Tags
- CloudWatch Contributor Insights
- Kinesis Data Streams
- Resource policies

---

## 9. Operational Concerns

### 9.1 Monitoring

**Key CloudWatch metrics:**

| Metric | Description | Alert Threshold |
|--------|------------|-----------------|
| `ReplicationLatency` | Time for write to appear in other region | > 5 seconds |
| `PendingReplicationCount` | Number of items awaiting replication | Increasing trend |
| `ConsumedWriteCapacityUnits` | Per-region consumption (includes replication) | > 80% of provisioned |
| `ThrottledRequests` | Per-region throttling (may be caused by replication) | Any |

### 9.2 Capacity Planning for Global Tables

```
Total write capacity per region:
  = Local write traffic
  + Sum of replicated writes from ALL other regions

Example: 3-region setup, each with 500 local writes/sec
  Each region needs: 500 (local) + 500 + 500 (replicated from 2 others)
  = 1,500 WCU per region

  Total across all regions: 4,500 WCU (3x the logical write rate)

Cost multiplier for N-region Global Tables:
  Each write is replicated to N-1 other regions
  Total WCU = local WCU × N
```

### 9.3 Adding / Removing Regions

**Adding a replica:**
- Creates new regional table
- Backfills existing data from source replica
- Cannot delete the source table for 24 hours after adding a replica
- During backfill: new replica receives writes but may not have all historical data

**Removing a replica:**
- Converts the regional table to a standalone single-region table
- Data remains in that region
- Stop replication but don't lose data

### 9.4 DAX with Global Tables

**Caution:** DAX caches locally. In a Global Tables setup:
- DAX in Region A caches item X = "old value"
- Region B updates X = "new value" → replicated to Region A
- DAX in Region A still returns "old value" until cache TTL expires
- Solution: Set DAX TTL appropriately, or bypass DAX for critical reads

### 9.5 Region Failover

```
If Region A becomes unavailable:
  MREC:
    → Writes that hadn't replicated are lost (RPO = seconds)
    → Traffic redirected to Region B via Route 53 health checks
    → No manual intervention needed for data
    → When Region A recovers: unreplicated data syncs

  MRSC:
    → No data loss (RPO = 0, synchronous replication)
    → Traffic redirected to surviving regions
    → 3rd region acts as quorum member for continued writes
    → Requires 2 of 3 regions operational
```

---

## 10. Interview Angles

### 10.1 "How do Global Tables handle conflicts?"

"MREC Global Tables use last-writer-wins based on internal timestamps. When the same item
is modified in two regions simultaneously, the write with the later timestamp wins, and
the other is silently discarded. This provides eventual convergence but can cause
silent data loss. Conflict avoidance strategies include region-affinity routing,
append-only patterns, and conditional writes. MRSC avoids conflicts entirely by using
synchronous replication — concurrent writes to the same item get a
ReplicatedWriteConflictException."

### 10.2 "What's the difference between MREC and MRSC?"

```
MREC (default):
  - Async replication via DynamoDB Streams
  - Low write latency (local commit only)
  - SC reads only consistent for local writes
  - LWW conflict resolution
  - RPO = seconds, RTO ≈ seconds
  - Supports transactions (region-local), TTL, LSI

MRSC:
  - Synchronous replication (cross-region on write path)
  - Higher write latency (cross-region sync)
  - SC reads consistent across ALL regions
  - No conflicts (serialized writes)
  - RPO = 0, RTO ≈ seconds
  - NO transactions, TTL, or LSI
  - Exactly 3 regions required
```

### 10.3 "A customer reports data inconsistency across regions. How do you investigate?"

```
Step 1: Check consistency mode
  → MREC? Stale reads are expected during replication lag
  → MRSC? Should not happen — investigate further

Step 2: Check ReplicationLatency metric
  → High latency (> 2.5s)? Region connectivity issue or throttling
  → Normal? Timing issue — customer read before replication completed

Step 3: Check for LWW conflicts
  → Was the item written in multiple regions within the replication window?
  → Check stream records for concurrent writes
  → LWW resolved in favor of later timestamp

Step 4: Check for throttling
  → If destination region is throttled, replication backs up
  → PendingReplicationCount increasing?
  → Increase write capacity on destination region

Step 5: Check DAX
  → Is DAX caching stale data?
  → DAX TTL may be longer than replication latency
```

### 10.4 "Why doesn't MRSC support transactions?"

"MRSC already synchronously coordinates writes across regions for single-item operations.
Adding transaction support (multi-item coordination across regions) would require a
distributed transaction protocol spanning 3 regions — multiple cross-region round trips
per transaction. The latency would be prohibitively high (hundreds of milliseconds for
cross-continent coordination), and the complexity of deadlock detection and rollback
across regions would be significant. AWS chose to keep MRSC simple and fast for
single-item operations."

### 10.5 Design Decision: Why Not Merge Concurrent Writes?

```
Why LWW instead of CRDT-based merging?

1. Generality: DynamoDB items are arbitrary key-value documents.
   Automatic merging requires semantic understanding of the data.
   (Is {color: "blue"} + {color: "green"} = {color: "bluegreen"}?)

2. Simplicity: LWW is deterministic and easy to reason about.
   CRDTs require specific data structures (counters, sets, registers)
   that constrain the application data model.

3. Predictability: With LWW, the result is always a complete,
   valid write from one region. Merge could produce invalid states.

4. Application control: Conflict avoidance (region affinity) is
   preferred over conflict resolution. Most applications can avoid
   cross-region concurrent writes.

Trade-off: Occasional silent data loss vs. complex merging semantics.
DynamoDB chose simplicity.
```

---

## Appendix: Key Numbers

| Property | Value |
|----------|-------|
| Replication latency (MREC) | Typically < 1 second, up to 2.5 seconds |
| RPO (MREC) | Seconds |
| RPO (MRSC) | Zero |
| Minimum regions (MREC) | 2 |
| Regions required (MRSC) | Exactly 3 |
| MRSC region sets | US, EU, AP (cannot span sets) |
| Stream readers per shard (MREC) | 1 for application (1 used by replication) |
| Write capacity cost | N regions × local writes (replication consumes WCU) |
| Global tables per account (MRSC) | 400 |
| Cannot delete source table after adding replica | 24 hours |
| Region recovery (isolated MREC replica) | 20 hours then converts to standalone |
