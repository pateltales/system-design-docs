# DynamoDB Partitioning Model — Deep Dive

## Table of Contents

1. [Overview](#1-overview)
2. [Partition Key and Data Distribution](#2-partition-key-and-data-distribution)
3. [Partition Internals](#3-partition-internals)
4. [Partition Splits](#4-partition-splits)
5. [Request Router and Partition Map](#5-request-router-and-partition-map)
6. [Adaptive Capacity](#6-adaptive-capacity)
7. [Burst Capacity](#7-burst-capacity)
8. [Hot Partition Handling](#8-hot-partition-handling)
9. [Write Sharding](#9-write-sharding)
10. [Partition Key Design Best Practices](#10-partition-key-design-best-practices)
11. [Interview Angles](#11-interview-angles)

---

## 1. Overview

DynamoDB is a fully managed, key-value and document database. Under the hood, every table
is divided into **partitions** — allocations of storage backed by SSDs, automatically
replicated across multiple Availability Zones within an AWS Region.

**Key facts:**

| Property | Value |
|----------|-------|
| Partition management | Fully automatic, transparent to users |
| Partition storage | SSD-backed |
| Replication | 3 replicas across AZs (automatic) |
| Per-partition read throughput | 3,000 RCU |
| Per-partition write throughput | 1,000 WCU |
| Per-partition data size | 10 GB |
| Partition splits | Automatic, zero downtime |

Users never directly manage partitions. DynamoDB handles all partitioning, splitting,
replication, and rebalancing transparently.

---

## 2. Partition Key and Data Distribution

### 2.1 Primary Key Types

DynamoDB supports two primary key schemas:

**Simple primary key (partition key only):**

```
┌─────────────────────────────────────┐
│  Table: Users                        │
│  Partition Key: UserId               │
├─────────────────────────────────────┤
│  UserId = "U001" → Hash → Partition 3│
│  UserId = "U002" → Hash → Partition 1│
│  UserId = "U003" → Hash → Partition 2│
└─────────────────────────────────────┘
```

- DynamoDB applies an **internal hash function** to the partition key value
- Hash output determines which partition stores the item
- Items are NOT stored in sorted order within a partition (for simple PK)
- Each item is uniquely identified by its partition key

**Composite primary key (partition key + sort key):**

```
┌──────────────────────────────────────────────────────┐
│  Table: Orders                                        │
│  Partition Key: CustomerId    Sort Key: OrderDate     │
├──────────────────────────────────────────────────────┤
│  CustomerId = "C001"                                  │
│    → Hash → Partition 2                               │
│    → Within partition: sorted by OrderDate            │
│       OrderDate = "2024-01-01"                        │
│       OrderDate = "2024-01-15"                        │
│       OrderDate = "2024-02-01"                        │
│                                                       │
│  CustomerId = "C002"                                  │
│    → Hash → Partition 1                               │
│    → Within partition: sorted by OrderDate            │
└──────────────────────────────────────────────────────┘
```

- Hash function is applied to the **partition key only**
- Items with the same partition key are stored together on the same partition
- Within a partition key, items are sorted in **ascending order by sort key**
- This group of items with the same partition key = **item collection**
- No upper limit on distinct sort key values per partition key
- `Query` operations can retrieve multiple items with the same partition key

### 2.2 Hash Function

DynamoDB uses an internal hash function (not user-visible) to map partition key values
to partitions:

```
Partition Key Value → Hash Function → Hash Value → Partition Map → Physical Partition

Example [INFERRED]:
  "UserId-001"  → hash → 0x3A7F... → maps to Partition 7
  "UserId-002"  → hash → 0xB2C1... → maps to Partition 3
  "UserId-003"  → hash → 0x1E44... → maps to Partition 12
```

**Key properties of the hash function:**
- Deterministic: same key always maps to the same hash
- Uniform distribution: designed to spread keys evenly across the hash space
- One-way: cannot derive the key from the hash
- The hash space is divided into ranges, each assigned to a partition

### 2.3 Data Distribution Model

```
┌──────────────────────────────────────────────────────────────┐
│                    Hash Space                                 │
│                                                              │
│  0x0000...          0x5555...          0xAAAA...   0xFFFF... │
│  ├─────────────────┼─────────────────┼──────────────────┤   │
│  │   Partition 1   │   Partition 2   │   Partition 3     │   │
│  │                 │                 │                    │   │
│  │  Keys that hash │  Keys that hash │  Keys that hash   │   │
│  │  to 0x0000-     │  to 0x5555-     │  to 0xAAAA-       │   │
│  │    0x5554        │    0xAAA9        │    0xFFFF          │   │
│  └─────────────────┴─────────────────┴──────────────────┘   │
│                                                              │
│  Each partition: 3 replicas across 3 AZs                     │
│  Each partition: up to 10 GB data, 3000 RCU, 1000 WCU       │
└──────────────────────────────────────────────────────────────┘
```

---

## 3. Partition Internals

### 3.1 Physical Structure

Each partition is a self-contained unit [INFERRED based on AWS publications]:

```
┌─────────────────────────────────────────────────┐
│                  Partition                        │
├─────────────────────────────────────────────────┤
│                                                  │
│  ┌──────────────┐                               │
│  │  B-tree      │  ← Primary data structure     │
│  │  (on SSD)    │    for reads and range scans   │
│  └──────────────┘                               │
│                                                  │
│  ┌──────────────┐                               │
│  │  WAL         │  ← Write-ahead log for        │
│  │  (on SSD)    │    durability before B-tree    │
│  └──────────────┘    update                      │
│                                                  │
│  ┌──────────────┐                               │
│  │  Paxos Log   │  ← Replication log for        │
│  │              │    leader-based consensus      │
│  └──────────────┘                               │
│                                                  │
│  Metadata:                                       │
│  ├─ Hash range: [0x5555, 0xAAAA)                │
│  ├─ Leader replica: AZ-1                         │
│  ├─ Follower replicas: AZ-2, AZ-3               │
│  ├─ Current size: 6.2 GB / 10 GB                │
│  └─ Current throughput: 800 RCU / 400 WCU       │
│                                                  │
└─────────────────────────────────────────────────┘
```

### 3.2 Replication Per Partition

Every partition is replicated 3 times across AZs:

```
                ┌───────────────────┐
                │   Partition P1    │
                │   (Logical)       │
                └─────────┬─────────┘
                          │
           ┌──────────────┼──────────────┐
           │              │              │
     ┌─────▼─────┐ ┌─────▼─────┐ ┌─────▼─────┐
     │  Replica 1 │ │  Replica 2 │ │  Replica 3 │
     │  (Leader)  │ │ (Follower) │ │ (Follower) │
     │   AZ-a     │ │   AZ-b     │ │   AZ-c     │
     └───────────┘ └───────────┘ └───────────┘
           │
           │ Writes go through leader
           │ Leader replicates via Paxos
           │ Majority ACK (2 of 3) = committed
```

- **Leader:** Handles all writes and strongly consistent reads
- **Followers:** Handle eventually consistent reads, participate in Paxos voting
- **Leader election:** Via Multi-Paxos [INFERRED]; automatic on leader failure

### 3.3 Per-Partition Limits

| Resource | Limit | Notes |
|----------|-------|-------|
| Data size | 10 GB | Triggers split when exceeded |
| Read throughput | 3,000 RCU | ~12,000 eventually consistent 4 KB reads/sec |
| Write throughput | 1,000 WCU | 1,000 × 1 KB writes/sec |
| Item collections (with LSI) | 10 GB | Per partition key value |

**Critical understanding:** These are **per-partition** limits. A table can have unlimited
partitions, so total table throughput is unlimited (in on-demand mode) or up to provisioned
limits.

---

## 4. Partition Splits

### 4.1 Split Triggers

DynamoDB automatically splits a partition when either threshold is reached:

```
┌──────────────────────────────────────────────────────┐
│              Partition Split Triggers                  │
├──────────────────────────────────────────────────────┤
│                                                      │
│  Trigger 1: SIZE                                     │
│  ├─ Partition data exceeds 10 GB                    │
│  └─ Split into 2 partitions of ~5 GB each           │
│                                                      │
│  Trigger 2: THROUGHPUT                               │
│  ├─ Partition throughput exceeds 3,000 RCU           │
│  │  or 1,000 WCU consistently                       │
│  └─ Split into 2 partitions, each handling           │
│     a portion of the hash range                      │
│                                                      │
└──────────────────────────────────────────────────────┘
```

### 4.2 Split Process

```
Before split:
  Partition P1: hash range [0x0000, 0xFFFF]
  Data: 10.5 GB (exceeded limit)

Split operation [INFERRED]:
  1. Choose split point: midpoint of hash range (0x7FFF)
  2. Create new partition P1' for range [0x8000, 0xFFFF]
  3. Copy data belonging to P1' range to new partition
  4. Both P1 and P1' replicated to 3 AZs
  5. Update partition map (request router)
  6. Redirect requests for [0x8000, 0xFFFF] to P1'

After split:
  Partition P1:  hash range [0x0000, 0x7FFF]  ~5 GB
  Partition P1': hash range [0x8000, 0xFFFF]  ~5.5 GB

┌──────────────────────┐         ┌──────────────────────┐
│  Partition P1         │         │  Partition P1'        │
│  [0x0000, 0x7FFF]    │         │  [0x8000, 0xFFFF]    │
│  ~5 GB                │         │  ~5.5 GB              │
│  3 replicas           │         │  3 replicas           │
│  Own leader           │         │  Own leader           │
└──────────────────────┘         └──────────────────────┘
```

### 4.3 Split is Zero-Downtime

Partition splits are designed to be invisible to the application:
- No downtime during split
- Request router is updated atomically [INFERRED]
- In-flight requests to the old partition continue to work
- New requests are routed to the correct partition after the split
- Split operation is managed entirely by DynamoDB

### 4.4 Splits Are One-Way

**Partitions do not merge.** Once a partition is split, it stays split even if the data
or throughput decreases. This is important because:

- After a throughput spike that triggered splits, you have more partitions
- Each partition has its own 3,000 RCU / 1,000 WCU capacity
- If you later reduce provisioned throughput, it's divided across MORE partitions
- Each partition gets a smaller share of the total provisioned throughput

```
Example of the "split tax" [INFERRED]:
  t0: 1 partition, 1,000 WCU provisioned → 1,000 WCU available
  t1: Spike causes split → 4 partitions
  t2: Reduce provisioned to 400 WCU → 100 WCU per partition
      Even though 400 WCU total, each partition only gets 100 WCU
      → More likely to hit per-partition throttling

  Mitigation: Adaptive capacity (see Section 6)
```

**Note:** With adaptive capacity (which is now automatic), this "split tax" problem is
largely mitigated. DynamoDB can redistribute unused capacity to hot partitions.

---

## 5. Request Router and Partition Map

### 5.1 Architecture

Every DynamoDB API call goes through a **request router** that determines which partition
(and which replica) should handle the request:

```
┌──────────┐     ┌──────────────┐     ┌────────────────┐     ┌───────────┐
│  Client  │────▶│   DynamoDB   │────▶│  Request       │────▶│ Partition │
│  (SDK)   │     │   Endpoint   │     │  Router        │     │ (Leader   │
│          │     │              │     │                │     │  or       │
│          │     │              │     │  Partition Map │     │  Follower)│
└──────────┘     └──────────────┘     └────────────────┘     └───────────┘
```

### 5.2 Request Router Logic

```
┌───────────────────────────────────────────────────────────┐
│                Request Router Logic                        │
├───────────────────────────────────────────────────────────┤
│                                                           │
│  Input: API request (e.g., GetItem with PK = "U001")     │
│                                                           │
│  1. Extract partition key from request                    │
│  2. Hash the partition key → hash value                   │
│  3. Look up partition map:                                │
│     hash value → which partition owns this range?         │
│  4. Determine routing:                                    │
│     ├─ Write request → route to LEADER replica            │
│     ├─ Strongly consistent read → route to LEADER         │
│     └─ Eventually consistent read → route to ANY replica  │
│  5. Forward request to selected storage node              │
│                                                           │
│  If partition map is stale (split occurred):              │
│     → Storage node returns redirect                       │
│     → Router refreshes partition map                      │
│     → Retries request to correct partition                │
│                                                           │
└───────────────────────────────────────────────────────────┘
```

### 5.3 Partition Map

The partition map is a critical metadata structure [INFERRED]:

```
┌────────────────────────────────────────────────────────┐
│              Partition Map for Table "Orders"           │
├────────────────────────────────────────────────────────┤
│                                                        │
│  Hash Range          │ Partition │ Leader  │ Followers │
│  ────────────────────┼──────────┼─────────┼──────────│
│  [0x0000, 0x3FFF]   │   P1     │  AZ-a   │ AZ-b,c   │
│  [0x4000, 0x7FFF]   │   P2     │  AZ-b   │ AZ-a,c   │
│  [0x8000, 0xBFFF]   │   P3     │  AZ-c   │ AZ-a,b   │
│  [0xC000, 0xFFFF]   │   P4     │  AZ-a   │ AZ-b,c   │
│                                                        │
│  Updated on: partition split, leader election,         │
│              AZ failure, rebalancing                    │
│                                                        │
└────────────────────────────────────────────────────────┘
```

**Partition map properties:**
- Maintained by DynamoDB's control plane [INFERRED]
- Cached by request routers for fast lookup
- Updated when partitions split or leaders change
- Must be highly available — if the partition map is unavailable, no requests can be routed

### 5.4 Request Flow: GetItem

```
Client                Router              Partition P2 (Leader)
  │                     │                        │
  │  GetItem(PK="U01") │                        │
  │────────────────────▶│                        │
  │                     │                        │
  │                     │  hash("U01") = 0x5A..  │
  │                     │  Map: 0x5A → P2        │
  │                     │                        │
  │                     │  ConsistentRead=true   │
  │                     │  → route to leader     │
  │                     │────────────────────────▶│
  │                     │                        │
  │                     │                        │ Read from B-tree
  │                     │                        │
  │                     │◀────────────────────────│
  │                     │    Item data           │
  │◀────────────────────│                        │
  │     Response        │                        │
```

### 5.5 Request Flow: PutItem

```
Client                Router              Partition P2 (Leader)    P2 Followers
  │                     │                        │                     │
  │  PutItem(PK="U01") │                        │                     │
  │────────────────────▶│                        │                     │
  │                     │  hash → P2 (leader)   │                     │
  │                     │────────────────────────▶│                     │
  │                     │                        │                     │
  │                     │                        │ Write to WAL        │
  │                     │                        │                     │
  │                     │                        │ Paxos replicate ───▶│
  │                     │                        │                     │
  │                     │                        │◀── Majority ACK ────│
  │                     │                        │    (2 of 3)         │
  │                     │                        │                     │
  │                     │                        │ Update B-tree       │
  │                     │                        │                     │
  │                     │◀────────────────────────│                     │
  │◀────────────────────│    200 OK              │                     │
  │     Response        │                        │                     │
```

**When does PutItem return 200?**
- After the write is committed to the Paxos log (majority ACK = 2 of 3 replicas)
- The B-tree update may happen asynchronously after the WAL write [INFERRED]
- This ensures durability: even if the leader crashes, the write is on at least 2 replicas

---

## 6. Adaptive Capacity

### 6.1 What It Is

Adaptive capacity is an automatic feature (no configuration needed) that helps handle
uneven access patterns across partitions.

**The problem it solves:**

```
Table: 400 WCU provisioned, 4 partitions
  Without adaptive capacity:
    Each partition gets 400 / 4 = 100 WCU

    Partition 1: 50 WCU actual  ← underutilized
    Partition 2: 50 WCU actual  ← underutilized
    Partition 3: 50 WCU actual  ← underutilized
    Partition 4: 150 WCU actual ← THROTTLED! (exceeds 100 WCU)

  With adaptive capacity:
    DynamoDB observes the imbalance and rebalances:

    Partition 1: 50 WCU actual / 50 WCU allocated  ✓
    Partition 2: 50 WCU actual / 50 WCU allocated  ✓
    Partition 3: 50 WCU actual / 50 WCU allocated  ✓
    Partition 4: 150 WCU actual / 250 WCU allocated ✓
                                  (borrowed from others)

  Total: 400 WCU provisioned → 400 WCU used. No waste.
```

### 6.2 How It Works

```
┌──────────────────────────────────────────────────────────┐
│              Adaptive Capacity Flow                       │
├──────────────────────────────────────────────────────────┤
│                                                          │
│  1. DynamoDB monitors per-partition throughput usage      │
│     continuously                                         │
│                                                          │
│  2. Detects imbalance: one partition consuming more      │
│     than its fair share, others consuming less            │
│                                                          │
│  3. Instantly increases throughput capacity for the       │
│     hot partition                                         │
│     → Borrows from underutilized partitions              │
│                                                          │
│  4. Constraint: total table throughput cannot exceed      │
│     provisioned capacity                                  │
│     → Hot partition gets more, cold partitions give up   │
│                                                          │
│  5. If a single item is consistently hot:                │
│     → DynamoDB may ISOLATE it onto its own partition     │
│     → That partition gets up to 3,000 RCU / 1,000 WCU   │
│                                                          │
└──────────────────────────────────────────────────────────┘
```

### 6.3 Properties

| Property | Value |
|----------|-------|
| Activation | Automatic, every table, no configuration |
| Cost | No additional charge |
| Speed | Instant rebalancing |
| Scope | Within a single table |
| Constraint | Cannot exceed table's total provisioned throughput |
| Per-partition max | 3,000 RCU / 1,000 WCU (hard limit even with adaptive) |
| Works with | Both provisioned and on-demand modes |

### 6.4 Limitation

Adaptive capacity cannot help when:
- A single partition key receives more than 3,000 RCU or 1,000 WCU
  (this is a per-partition hard ceiling)
- Total table throughput is fully consumed (no spare capacity to borrow)
- Item collections with LSI exceed 10 GB (adaptive capacity will not split
  item collections when an LSI exists)

---

## 7. Burst Capacity

### 7.1 What It Is

DynamoDB reserves a portion of unused throughput capacity for handling short spikes:

| Property | Value |
|----------|-------|
| Reserve window | Up to **5 minutes (300 seconds)** of unused capacity |
| Consumption rate | Can be consumed faster than provisioned per-second throughput |
| Purpose | Handle short usage spikes without throttling |

### 7.2 How It Works

```
Provisioned: 100 WCU/sec

Second 1-59: Only using 20 WCU/sec
  → 80 WCU/sec unused × 59 seconds = 4,720 WCU banked

Second 60: Spike to 500 WCU/sec
  → 400 WCU/sec over provisioned
  → Burst capacity covers it (4,720 > 400)
  → No throttling!

Second 61-65: Still spiking at 500 WCU/sec
  → Consuming burst capacity at 400 WCU/sec over
  → 400 × 5 = 2,000 WCU consumed from burst
  → Still OK (4,720 - 2,000 = 2,720 remaining)

...but if spike continues for minutes, burst runs out → throttling
```

### 7.3 Important Caveats

- DynamoDB may consume burst capacity for **background maintenance** without notice
- Burst capacity is not guaranteed — it's best-effort
- Don't rely on burst capacity for sustained high throughput
- The 300-second window may change in the future (per AWS docs)

### 7.4 Burst vs Adaptive

| Feature | Burst Capacity | Adaptive Capacity |
|---------|---------------|-------------------|
| **Handles** | Short spikes (seconds) | Sustained imbalance (minutes+) |
| **Mechanism** | Banked unused capacity | Rebalance across partitions |
| **Duration** | Up to 5 min of reserves | Continuous |
| **Scope** | Per partition | Across table partitions |
| **Guaranteed** | Best-effort | Automatic, reliable |

---

## 8. Hot Partition Handling

### 8.1 What Makes a Partition "Hot"

A partition is hot when it receives disproportionately more traffic than other partitions.

**Common causes:**

| Cause | Example |
|-------|---------|
| Low cardinality partition key | Status = "ACTIVE" (most items) |
| Time-based partition key | Date = "2024-01-15" (all today's writes) |
| Popular entity | UserId = "celebrity" (millions of followers) |
| Seasonal pattern | ProductId = "holiday-special" (flash sale) |

### 8.2 DynamoDB's Layered Defense

```
Layer 1: Burst Capacity
  → Absorbs short spikes (seconds)
  → Uses banked unused throughput

Layer 2: Adaptive Capacity
  → Borrows from cold partitions
  → Works within total table provisioned capacity

Layer 3: Partition Isolation
  → For consistently hot single items
  → DynamoDB isolates the hot item to its own partition
  → That partition gets full 3,000 RCU / 1,000 WCU

Layer 4: On-Demand Mode
  → Table scales automatically, no provisioning
  → Each partition still limited to 3,000/1,000
  → But DynamoDB can add partitions proactively

If all layers are exceeded:
  → Throttling (ProvisionedThroughputExceededException)
  → SDK retries with exponential backoff
```

### 8.3 When Nothing Helps: Per-Partition Ceiling

Even with all mitigation, a single partition key cannot exceed:
- **3,000 RCU** (= 12,000 eventually consistent 4 KB reads/sec)
- **1,000 WCU** (= 1,000 × 1 KB writes/sec)

If you need more than this for a single key, you must use application-level
strategies (write sharding, caching with DAX, etc.).

---

## 9. Write Sharding

### 9.1 The Problem

When a single logical partition key receives too many writes:

```
Table: Votes
  Partition Key: CandidateId

  CandidateId = "candidate-A" → 50,000 votes/sec

  One partition key → one partition → max 1,000 WCU
  50,000 writes/sec >> 1,000 WCU → massive throttling
```

### 9.2 Random Suffix Sharding

Append a random number to the partition key to spread writes across partitions:

```
Instead of:
  PK = "candidate-A"  → all writes to 1 partition

Use:
  PK = "candidate-A.1"    → Partition X
  PK = "candidate-A.2"    → Partition Y
  PK = "candidate-A.3"    → Partition Z
  ...
  PK = "candidate-A.200"  → Partition W

  Random suffix 1-200 at write time

  200 partitions × 1,000 WCU = 200,000 WCU total capacity
  50,000 votes/sec easily handled
```

**Tradeoff:** Reading all votes for "candidate-A" requires querying all 200 shards
and aggregating results:

```
Read all votes:
  for i in 1..200:
    Query(PK = "candidate-A.{i}")
  aggregate results
```

### 9.3 Calculated Suffix Sharding

Use a deterministic function to calculate the suffix from another attribute:

```
Suffix = hash(VoterId) % 200

PK = "candidate-A.{suffix}"

Advantage: Given a VoterId, you can compute the exact shard
           → GetItem is efficient (no scatter-gather)

Disadvantage: Reading ALL votes still requires querying all shards
```

### 9.4 When to Use Each

| Strategy | Best For | Single-Item Read | Full Scan |
|----------|---------|-----------------|-----------|
| Random suffix | Write-heavy, no single-item reads | Impossible | Query all shards |
| Calculated suffix | Write-heavy + need single-item reads | Efficient (recalculate) | Query all shards |
| No sharding | Read-heavy, low write volume | Efficient | Efficient |

---

## 10. Partition Key Design Best Practices

### 10.1 Good vs Bad Partition Keys

| Key Choice | Quality | Reason |
|-----------|---------|--------|
| UserId (millions of users) | Excellent | High cardinality, uniform access |
| DeviceId (IoT fleet) | Good | Many devices, but check if some are hotter |
| Date (YYYY-MM-DD) | Bad | Only one value per day, all writes to one partition |
| Status (ACTIVE/INACTIVE) | Bad | 2 values, most items likely ACTIVE |
| Country (200 countries) | Moderate | Low cardinality, some countries much hotter |
| OrderId (UUID) | Excellent | Unique per item, perfect distribution |
| SessionId | Good | High cardinality, short-lived |

### 10.2 Design Principles

1. **High cardinality:** Many distinct values relative to total items
2. **Uniform access:** Values should be accessed with similar frequency
3. **Avoid temporal clustering:** Don't use a timestamp as the sole partition key
4. **Composite keys for access patterns:** Use sort key for range queries within a partition
5. **Write sharding for hot keys:** When a key must be hot, shard it

### 10.3 Common Patterns

**Time series data:**

```
Bad:  PK = "2024-01-15" (all writes to one partition per day)

Good: PK = "sensor-001" SK = "2024-01-15T10:30:00"
      (each sensor is its own partition, time is the sort key)

Good: PK = "2024-01-15.{shard}" SK = "sensor-001#10:30:00"
      (date-based with write sharding)
```

**Social media feed:**

```
PK = UserId, SK = PostTimestamp
  → Each user's posts in one partition, sorted by time
  → Query(PK = "user-123", SK begins_with "2024-01")
    returns all January posts for user-123
```

**E-commerce orders:**

```
PK = CustomerId, SK = OrderId
  → All orders for a customer in one partition
  → GetItem(PK = "cust-001", SK = "order-456") for specific order
  → Query(PK = "cust-001") for all orders by customer
```

---

## 11. Interview Angles

### 11.1 "How does DynamoDB partition data?"

**Strong answer:**

"DynamoDB applies an internal hash function to the partition key value. The hash output
maps to a position in a hash space that's divided into ranges, each owned by a partition.
Each partition stores up to 10 GB and handles up to 3,000 RCU / 1,000 WCU. When a
partition exceeds either limit, DynamoDB automatically splits it — dividing the hash
range in two and creating a new partition. Each partition is replicated 3 times across
AZs using Paxos-based replication with a leader per partition."

### 11.2 "What happens when a partition becomes hot?"

```
DynamoDB has a layered defense:

1. Burst capacity: 300 seconds of banked unused throughput absorbs short spikes

2. Adaptive capacity: Automatically borrows throughput from underutilized
   partitions. Works within the table's total provisioned capacity.

3. Partition isolation: For consistently hot single items, DynamoDB can
   isolate the item onto its own partition with full 3,000 RCU / 1,000 WCU.

4. But: there's a hard ceiling of 3,000 RCU / 1,000 WCU per partition.
   Beyond that, you need application-level strategies like write sharding
   or DAX caching.
```

### 11.3 "How is DynamoDB's partitioning different from the Dynamo paper?"

| Aspect | Dynamo Paper (2007) | DynamoDB (the service) |
|--------|--------------------|-----------------------|
| **Hashing** | Consistent hashing with virtual nodes | Hash-range partitioning with auto-split |
| **Replication** | Leaderless, sloppy quorum | Leader-per-partition with Paxos |
| **Conflict resolution** | Vector clocks, application-resolved | Not needed (leader handles writes) |
| **Partition management** | Manual, preference list | Fully automatic, managed by DynamoDB |
| **Membership** | Gossip protocol | Centralized partition map [INFERRED] |

**Critical interview point:** Do NOT confuse these two systems. DynamoDB evolved
significantly from the Dynamo paper. The key insight is that DynamoDB chose **strong
consistency as an option** (via leader-based writes), which the original Dynamo paper
explicitly traded away.

### 11.4 "A customer is seeing throttling. How do you diagnose?"

```
Step 1: Check CloudWatch metrics
  → ThrottledRequests: which operations are throttled?
  → ConsumedReadCapacityUnits / ConsumedWriteCapacityUnits
  → Is total consumption near provisioned capacity?

Step 2: Check per-partition metrics (via ContributorInsights)
  → Which partition keys are hot?
  → Is one key dominating traffic?

Step 3: Determine root cause
  → Hot partition key? → Consider write sharding or redesigning key
  → Overall capacity too low? → Increase provisioned capacity or switch to on-demand
  → Burst exhaustion? → Sustained spike exceeding provisioned capacity
  → GSI throttle cascading? → GSI throughput too low, back-pressuring base table

Step 4: Mitigation
  → Short term: Increase capacity, enable on-demand mode
  → Long term: Redesign partition key for uniform distribution
  → For single hot item: DAX cache in front of DynamoDB
```

### 11.5 "Why can't partitions merge?"

Merging partitions would require:
1. Stopping writes to both partitions during merge
2. Copying data from one partition to the other
3. Re-establishing Paxos replication for the merged partition
4. Updating the partition map atomically

This would cause availability impact and complexity for a marginal benefit (saving a
few empty partitions). Since each partition consumes resources proportional to its data
and throughput, empty partitions are cheap. The engineering tradeoff favors simplicity.

[INFERRED — AWS has not publicly documented the rationale for no-merge design]

---

## Appendix: Key Numbers

| Property | Value |
|----------|-------|
| Per-partition read throughput | 3,000 RCU |
| Per-partition write throughput | 1,000 WCU |
| Per-partition data size | 10 GB |
| Burst capacity reserve | 300 seconds (5 minutes) |
| Replicas per partition | 3 (across 3 AZs) |
| Item size limit | 400 KB |
| Partition key max length | 2,048 bytes |
| Sort key max length | 1,024 bytes |
| Table throughput (on-demand default) | 40,000 RRU / 40,000 WRU |
| Table throughput (provisioned default) | 40,000 RCU / 40,000 WCU |
| Account throughput (provisioned) | 80,000 RCU / 80,000 WCU |
