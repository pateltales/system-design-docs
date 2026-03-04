# DynamoDB Consistency Model — Deep Dive

## Table of Contents

1. [Overview](#1-overview)
2. [Eventually Consistent Reads](#2-eventually-consistent-reads)
3. [Strongly Consistent Reads](#3-strongly-consistent-reads)
4. [Consistency Across Features](#4-consistency-across-features)
5. [Read-Committed Isolation](#5-read-committed-isolation)
6. [Consistency in Transactions](#6-consistency-in-transactions)
7. [Consistency in Global Tables](#7-consistency-in-global-tables)
8. [Cost Implications](#8-cost-implications)
9. [Consistency Decision Framework](#9-consistency-decision-framework)
10. [Interview Angles](#10-interview-angles)

---

## 1. Overview

DynamoDB provides two consistency models for reads, controlled per-request:

| Model | Default? | Data Freshness | Cost | Availability |
|-------|----------|---------------|------|-------------|
| **Eventually Consistent** | Yes | May not reflect recent writes | 0.5 RCU per 4 KB | Higher (any replica) |
| **Strongly Consistent** | No | Reflects all prior writes | 1.0 RCU per 4 KB | Lower (leader only) |

**Fundamental guarantee (both models):**
- **Read-committed isolation:** DynamoDB never returns uncommitted or partially written data
- **Durability:** When PutItem returns HTTP 200, the write is durably persisted on at
  least 2 of 3 replicas
- **No dirty reads:** Reads always return committed values

---

## 2. Eventually Consistent Reads

### 2.1 How It Works

```
Client          Request Router       Any Replica (Leader or Follower)
  │                  │                    │
  │  GetItem         │                    │
  │  (default: EC)   │                    │
  │─────────────────▶│                    │
  │                  │                    │
  │                  │  Route to nearest  │
  │                  │  or least-loaded   │
  │                  │  replica           │
  │                  │───────────────────▶│
  │                  │                    │
  │                  │                    │ Read from B-tree
  │                  │                    │ (local data, may be
  │                  │                    │  slightly stale)
  │                  │                    │
  │                  │◀───────────────────│
  │◀─────────────────│   Item data       │
```

### 2.2 Why "Eventually" Consistent?

When a write is committed via Paxos, it's durable on at least 2 replicas. But the B-tree
update on follower replicas may lag slightly behind:

```
Timeline:
  t0: Client PutItem(X = 10)
  t1: Leader commits via Paxos (2/3 ACK) → 200 OK to client
  t2: Leader B-tree updated with X = 10
  t3: Follower-1 B-tree updated with X = 10  (< 1 ms later typically)
  t4: Follower-2 B-tree updated with X = 10  (< 1 ms later typically)

  If EC read hits Follower-2 between t1 and t4:
    → Returns old value of X (stale!)
    → This is the "eventual" part

  After t4: all replicas consistent
```

**Typical staleness:** Milliseconds. In practice, EC reads almost always return the
latest value. But there's no guarantee.

### 2.3 Properties

| Property | Value |
|----------|-------|
| Routing | Any replica (leader or follower) |
| Staleness | Typically milliseconds, no upper bound guarantee |
| Cost | 0.5 RCU per 4 KB |
| Availability | Higher — 3 replicas can serve reads |
| Use cases | Most reads in most applications |

### 2.4 When EC Reads Are Sufficient

- **Displaying user profiles:** Stale by milliseconds is invisible to users
- **Product catalog:** Price change visible milliseconds later is acceptable
- **Analytics dashboards:** Near-real-time is good enough
- **Social media feeds:** Eventual consistency is the natural expectation
- **Search results:** Slight delay in reflecting new items is acceptable

---

## 3. Strongly Consistent Reads

### 3.1 How It Works

```
Client          Request Router       Leader Replica (ONLY)
  │                  │                    │
  │  GetItem         │                    │
  │  ConsistentRead  │                    │
  │  = true          │                    │
  │─────────────────▶│                    │
  │                  │                    │
  │                  │  MUST route to     │
  │                  │  partition leader  │
  │                  │───────────────────▶│
  │                  │                    │
  │                  │                    │ 1. Verify I'm still leader
  │                  │                    │    (check lease)
  │                  │                    │
  │                  │                    │ 2. Read from B-tree
  │                  │                    │    (leader always has latest)
  │                  │                    │
  │                  │◀───────────────────│
  │◀─────────────────│   Item data       │
  │                  │   (guaranteed      │
  │                  │    latest)         │
```

### 3.2 Why the Leader?

The leader is the only replica guaranteed to have all committed writes applied:

```
Write flow:
  Leader → WAL → Paxos → Majority ACK → B-tree update

The leader's B-tree is updated as part of the commit path.
Follower B-trees are updated asynchronously after Paxos commit.

Therefore:
  Leader B-tree = always reflects all committed writes
  Follower B-tree = may lag behind by milliseconds
```

### 3.3 Leader Lease Verification [INFERRED]

Before serving a strongly consistent read, the leader must verify it's still the leader:

```
Why? Split-brain scenario:
  1. Leader AZ-a has a network partition
  2. Followers elect new leader in AZ-b
  3. Old leader (AZ-a) doesn't know it's been replaced
  4. If old leader serves SC read → may return stale data!

Prevention:
  Leader maintains a lease (time-based)
  → Before serving SC read: check "is my lease still valid?"
  → If lease expired: refuse the read, force re-election
  → This prevents stale reads from a deposed leader
```

### 3.4 Properties

| Property | Value |
|----------|-------|
| Routing | Leader replica ONLY |
| Staleness | None — reflects all prior committed writes |
| Cost | 1.0 RCU per 4 KB (2x EC) |
| Availability | Lower — only 1 of 3 replicas can serve |
| During leader election | **Unavailable** (for SC reads) |
| Use cases | When correctness depends on reading the latest value |

### 3.5 When SC Reads Are Necessary

- **Inventory check before purchase:** Must see current stock to avoid overselling
- **Account balance before transfer:** Must read latest balance
- **Lock acquisition:** Conditional write that reads and writes atomically
- **Read-modify-write patterns:** Read current value, compute new value, write
- **Idempotency checks:** Read whether a request was already processed

---

## 4. Consistency Across Features

### 4.1 Consistency Support Matrix

| Feature | Eventually Consistent | Strongly Consistent | Notes |
|---------|----------------------|-------------------|-------|
| **Table reads** (GetItem, Query, Scan) | Yes (default) | Yes (opt-in) | `ConsistentRead = true` |
| **Local Secondary Indexes (LSI)** | Yes (default) | Yes (opt-in) | Same partition as base table |
| **Global Secondary Indexes (GSI)** | Yes (always) | **No** | GSI is async-replicated |
| **DynamoDB Streams** | Eventually only | **No** | Async change capture |
| **Global Tables (cross-region)** | Eventually only | **No** | Async cross-region replication |
| **TransactGetItems** | Always serializable | N/A | Transaction-level consistency |
| **DAX (cache)** | Yes | **No** | In-memory cache, EC only |

### 4.2 Why Can't GSI Support Strong Consistency?

GSIs are implemented as **separate internal tables** with asynchronous replication from
the base table:

```
Base Table (Partition by UserId)
     │
     │ Async replication
     │ (write to base → async copy to GSI)
     ▼
GSI (Partition by Email)

Timeline:
  t0: PutItem to base table → committed
  t1: Async write to GSI → propagating
  t2: GSI updated

  SC read on GSI at t1 would require waiting for async replication
  → This would eliminate the point of async replication (performance)
  → So GSI only supports EC reads
```

### 4.3 Why Can LSI Support Strong Consistency?

LSIs are co-located with the base table on the **same partition**:

```
Same Partition Storage:
  ┌─────────────────────────────────┐
  │  Partition (by UserID)           │
  │                                  │
  │  Base table data: sorted by SK  │
  │  LSI-1 data: sorted by AltSK-1 │
  │  LSI-2 data: sorted by AltSK-2 │
  │                                  │
  │  All updated in the SAME write  │
  │  → Same Paxos commit            │
  │  → Leader has latest for all    │
  └─────────────────────────────────┘

Because LSI data is on the same partition as the base table,
the leader's B-tree includes the latest LSI data.
→ SC reads on LSI are possible.
```

---

## 5. Read-Committed Isolation

### 5.1 What It Means

DynamoDB provides **read-committed isolation** for all read operations:

- Reads **never** return uncommitted data
- Reads **never** return partially written data
- If a write is in progress (Paxos not yet committed), reads return the previous
  committed value

```
Timeline:
  t0: Item X = {name: "Alice", age: 30}  (committed)
  t1: PutItem(X = {name: "Alice", age: 31})  (Paxos in progress)
  t2: GetItem(X) → returns {name: "Alice", age: 30}  (previous committed)
  t3: Paxos commits the age=31 write
  t4: GetItem(X) → returns {name: "Alice", age: 31}  (new committed)

  At no point does a read return age=31 before it's committed.
```

### 5.2 Query and Scan Isolation

For **Query** (multi-item reads within a partition key):

```
Query reads are consistent within a single page:
  - Each page (up to 1 MB) is read atomically from one partition
  - Within a page: all items reflect the same point-in-time
  - Across pages (paginated query): items may reflect different points-in-time

  If a write happens between page 1 and page 2 of a paginated query:
  → Page 1 may show old data, page 2 may show new data
  → This is expected and documented behavior
```

For **Scan** (full table scan):

```
Scan reads:
  - Eventually consistent by default (can opt into SC)
  - Reads across multiple partitions, each partition snapshot may differ
  - Not transactionally consistent across the whole table
  - For point-in-time consistent snapshot: use TransactGetItems (up to 100 items)
    or export to S3 via PITR
```

---

## 6. Consistency in Transactions

### 6.1 TransactWriteItems

- **Serializable isolation:** All items in the transaction are written atomically
- Either ALL writes succeed or NONE succeed
- No other transaction can see partial results
- Conflict detection: if any item was modified between read and commit → transaction fails

### 6.2 TransactGetItems

- **Serializable isolation:** All items are read at the same point-in-time
- Provides a consistent snapshot across up to 100 items
- Even across different partitions — the transaction coordinator ensures consistency
- Always reads the latest committed values (similar to SC)
- Cost: 2x RCU (same as the 2x write cost for TransactWriteItems)

### 6.3 Non-Transactional Reads of Transactional Writes

```
If you use TransactWriteItems to write items A, B, C atomically:
  - TransactGetItems of A, B, C: sees all or none of the writes ✓
  - GetItem(A) with SC: sees A's new value (committed) ✓
  - GetItem(A) with EC: may or may not see the new value
  - Query across A, B, C: may see some new values but not all
    (Query is NOT transactionally consistent)

Key insight: Transaction writes are atomic, but non-transactional reads
don't see them atomically. Use TransactGetItems for consistent reads.
```

---

## 7. Consistency in Global Tables

### 7.1 Cross-Region Consistency

Global Tables replicate data across regions asynchronously:

```
Region: us-east-1                        Region: eu-west-1
┌──────────────────┐                    ┌──────────────────┐
│  Table Replica    │                    │  Table Replica    │
│                   │ ── DDB Streams ──▶ │                   │
│  PutItem(X=10)   │    async repl      │  X = 10 (later)  │
│  t0               │    ~0.5-2.5s       │  t0 + repl lag    │
└──────────────────┘                    └──────────────────┘
```

### 7.2 Consistency Guarantees

| Scenario | Guarantee |
|----------|-----------|
| Write in us-east-1, read in us-east-1 (SC) | Sees the write immediately |
| Write in us-east-1, read in us-east-1 (EC) | May see the write (typical: yes) |
| Write in us-east-1, read in eu-west-1 (EC) | May NOT see the write for 0.5-2.5s |
| Write in us-east-1, SC read in eu-west-1 | **Not supported across regions** |
| Write in us-east-1 AND eu-west-1 simultaneously | **Last-writer-wins** (by timestamp) |

### 7.3 Conflict Resolution

Global Tables use **last-writer-wins (LWW)** based on timestamps:

```
t0: Region A writes Item X = {color: "red"}   timestamp: 1000
t1: Region B writes Item X = {color: "blue"}  timestamp: 1001

Replication:
  Region A gets B's write (timestamp 1001 > 1000) → X = blue ✓
  Region B gets A's write (timestamp 1000 < 1001) → keeps X = blue ✓

Both regions converge to X = blue (latest timestamp wins)

Problem: If Region A's clock is ahead:
  t0: Region A writes Item X = {color: "red"}   timestamp: 1005 (clock ahead!)
  t1: Region B writes Item X = {color: "blue"}  timestamp: 1001

  Result: X = red (wrong! B's write was actually later in real time)

  Mitigation: AWS keeps region clocks tightly synchronized, but
  clock skew can still cause unexpected LWW outcomes.
```

### 7.4 Multi-Region Strong Consistency (MRSC) — Global Tables v2

AWS introduced Multi-Region Strong Consistency (MRSC) for Global Tables:

| Mode | Consistency | Conflict Resolution | Latency |
|------|------------|-------------------|---------|
| **MREC** (default) | Eventually consistent across regions | Last-writer-wins | Low |
| **MRSC** | Strongly consistent across regions | No conflicts (serialized) | Higher |

With MRSC:
- Reads in any region can be strongly consistent with writes from any region
- Writes are globally serialized [INFERRED — higher latency due to cross-region coordination]
- No LWW conflicts

---

## 8. Cost Implications

### 8.1 RCU Calculations

| Read Type | Item Size | RCU Cost | Formula |
|-----------|-----------|----------|---------|
| EC, 4 KB item | 4 KB | 0.5 RCU | ceil(4/4) × 0.5 |
| SC, 4 KB item | 4 KB | 1.0 RCU | ceil(4/4) × 1.0 |
| EC, 8 KB item | 8 KB | 1.0 RCU | ceil(8/4) × 0.5 |
| SC, 8 KB item | 8 KB | 2.0 RCU | ceil(8/4) × 1.0 |
| EC, 1 KB item | 1 KB | 0.5 RCU | ceil(1/4) × 0.5 = ceil(0.25) × 0.5 = 0.5 |
| SC, 1 KB item | 1 KB | 1.0 RCU | ceil(1/4) × 1.0 = 1.0 |
| EC, 20 KB item | 20 KB | 2.5 RCU | ceil(20/4) × 0.5 = 5 × 0.5 |
| SC, 20 KB item | 20 KB | 5.0 RCU | ceil(20/4) × 1.0 = 5 |
| TransactGetItems, 4 KB | 4 KB | 2.0 RCU | ceil(4/4) × 2.0 |

### 8.2 Cost Optimization

```
If your table does 10,000 reads/sec, average item 4 KB:

  All EC: 10,000 × 0.5 = 5,000 RCU
  All SC: 10,000 × 1.0 = 10,000 RCU  (2x cost!)

  Hybrid approach:
    90% EC (non-critical): 9,000 × 0.5 = 4,500 RCU
    10% SC (critical):     1,000 × 1.0 = 1,000 RCU
    Total: 5,500 RCU (vs 10,000 for all-SC)

  Savings: 45% cost reduction with hybrid approach
```

---

## 9. Consistency Decision Framework

### 9.1 Decision Tree

```
Does the read NEED the absolute latest value?
  │
  ├─ NO → Use Eventually Consistent (default)
  │       0.5 RCU, higher availability, slightly faster
  │
  └─ YES → Is this a multi-item read requiring atomicity?
            │
            ├─ YES → Use TransactGetItems
            │        2.0 RCU per 4 KB, up to 100 items
            │
            └─ NO → Use Strongly Consistent Read
                     1.0 RCU, leader-only, guaranteed latest
```

### 9.2 Pattern: Read-Modify-Write

```
WRONG (race condition):
  1. GetItem(EC) → X = 10
  2. Compute: X + 1 = 11
  3. PutItem(X = 11)
  → Another writer may have set X = 15 between steps 1 and 3
  → You overwrote with 11, losing the other write

RIGHT (conditional write):
  1. GetItem(SC) → X = 10
  2. Compute: X + 1 = 11
  3. PutItem(X = 11, ConditionExpression = "X = :old", :old = 10)
  → If someone changed X between steps 1 and 3:
     condition fails → ConditionalCheckFailedException → retry

EVEN BETTER (atomic update):
  1. UpdateItem(SET X = X + 1)
  → Atomic on the leader, no read needed, no race condition
```

### 9.3 Pattern: Idempotency Check

```
Before processing a request, check if already processed:
  1. GetItem(SC, requestId = "req-123")
     → Must be SC to avoid processing a request twice
  2. If exists → already processed, return cached result
  3. If not → process request, write result with PutItem
```

---

## 10. Interview Angles

### 10.1 "Explain eventually consistent vs strongly consistent reads"

"DynamoDB replicates each partition to 3 AZs using Paxos with a leader. Writes always
go through the leader and are committed when 2 of 3 replicas acknowledge. Eventually
consistent reads can go to any replica — fast and cheap at 0.5 RCU per 4 KB, but may
return slightly stale data if the replica hasn't applied the latest commit. Strongly
consistent reads must go to the leader, which always has the latest committed data —
guaranteed fresh but costs 1.0 RCU and is unavailable during leader election."

### 10.2 "Why can't GSIs support strong consistency?"

"GSIs are implemented as separate internal tables with their own partitions. Data is
replicated from the base table to the GSI asynchronously — the base table write commits
first, then the GSI write happens later. Because of this asynchronous replication,
the GSI data is inherently eventually consistent. Supporting strong consistency would
require synchronous GSI updates, which would add latency to every base table write
proportional to the number of GSIs — defeating the purpose of the async design."

### 10.3 "A customer reports stale reads. How do you diagnose?"

```
Step 1: Are they using EC or SC reads?
  → If EC: expected behavior. Recommend SC if freshness is critical.

Step 2: Are they reading from a GSI?
  → GSI is always EC. If they need fresh data, read from base table.

Step 3: Are they in a Global Tables setup?
  → Cross-region replication is async (0.5-2.5s lag).
  → Write in us-east-1, read in eu-west-1 → expect lag.
  → Solution: read from the region where the write happened.

Step 4: Are they reading immediately after a write?
  → EC read right after PutItem may hit a follower that hasn't applied the write.
  → Solution: Use SC read for read-after-write consistency.

Step 5: Is there a caching layer (DAX)?
  → DAX only supports EC reads. DAX cache may be stale.
  → Reads bypass DAX by setting ConsistentRead = true.
```

### 10.4 "What consistency level does a DynamoDB Scan provide?"

"Scan reads data page by page across partitions. Each page is read consistently
(read-committed), but across pages, the data may reflect different points in time.
This means a Scan is NOT a consistent snapshot of the entire table — items written
during the Scan may or may not be included. For a consistent multi-item read, use
TransactGetItems (up to 100 items). For a full-table consistent snapshot, use
Point-in-Time Recovery export to S3."

### 10.5 Design Decision: Why Default to EC Instead of SC?

```
1. Performance: EC can be served by any of 3 replicas
   → 3x more read capacity, better load distribution

2. Availability: EC works even during leader election
   → SC is unavailable for seconds during failover

3. Cost: EC costs half (0.5 vs 1.0 RCU per 4 KB)
   → For most workloads, 2x cost for freshness isn't justified

4. Most reads don't need SC:
   → User profiles, product listings, feeds, analytics
   → All tolerate millisecond-level staleness

5. Opt-in SC for when it matters:
   → Financial transactions, inventory checks, lock acquisition
   → Developer explicitly chooses when to pay the cost
```

---

## Appendix: Quick Reference

### Consistency by Feature

| Feature | EC | SC | Notes |
|---------|----|----|-------|
| Table GetItem | Yes | Yes | `ConsistentRead = true` |
| Table Query | Yes | Yes | `ConsistentRead = true` |
| Table Scan | Yes | Yes | Per-page consistency only |
| LSI | Yes | Yes | Same partition as base |
| GSI | Yes | **No** | Async replication |
| Streams | Yes | **No** | Async change capture |
| Global Tables (cross-region) | Yes | MRSC mode only | LWW or serialized |
| TransactGetItems | Serializable | N/A | 2x RCU cost |
| DAX | Yes | **No** | Cache bypass for SC |

### RCU Formula

```
EC: ceil(item_size_kb / 4) × 0.5
SC: ceil(item_size_kb / 4) × 1.0
TransactGet: ceil(item_size_kb / 4) × 2.0
```
