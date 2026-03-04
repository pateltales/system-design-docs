# System Design Interview Simulation: Design Amazon DynamoDB (Managed NoSQL Database)

> **Interviewer:** Principal Engineer (L8), Amazon DynamoDB Team
> **Candidate Level:** SDE-3 (L6 — Senior Software Development Engineer)
> **Duration:** ~60 minutes
> **Date:** February 13, 2026

---

## PHASE 1: Opening & Problem Statement (~2 min)

**Interviewer:**
Hey, welcome. I'm [name], Principal Engineer on the DynamoDB team. For today's system design round, I'd like you to design a **fully managed NoSQL key-value and document database** — think Amazon DynamoDB. A system where clients can store and retrieve items using a primary key, with single-digit millisecond latency at any scale. We're talking about the core infrastructure — the storage engine, partitioning, replication, and the query model.

I care about how you reason through consistency, partitioning tradeoffs, and what it means to deliver predictable low-latency at massive scale. I'll push on your decisions — that's calibration, not a negative signal.

Take it away.

---

## PHASE 2: Requirements Gathering & Scoping (~5 min)

**Candidate:**
Thanks! DynamoDB is a rich system, so before I start drawing boxes, let me scope this down and clarify what we're building.

**Functional Requirements — what operations do we need?**

> "The core API operations I'd expect for a key-value/document store:
>
> - **PutItem** — write an item (a collection of attributes) to a table, identified by a primary key.
> - **GetItem** — retrieve a single item by its full primary key (partition key, or partition key + sort key).
> - **Query** — retrieve multiple items that share the same partition key, optionally filtering or ranging on the sort key.
> - **Scan** — read every item in the table (expensive, full-table scan).
> - **UpdateItem** — modify specific attributes of an existing item (or create it if it doesn't exist).
> - **DeleteItem** — remove an item by primary key.
> - **BatchGetItem / BatchWriteItem** — batch variants for throughput.
>
> A few clarifying questions:
> - **What's the data model — pure key-value, or something richer?**"

**Interviewer:** "Good question. What's your understanding of DynamoDB's data model?"

> "DynamoDB supports two types of primary keys:
>
> 1. **Simple primary key** — just a partition key (hash key). Each item is uniquely identified by this single attribute.
> 2. **Composite primary key** — partition key + sort key (range key). Multiple items can share the same partition key, but each must have a unique sort key. Items with the same partition key are stored together and sorted by sort key, enabling range queries.
>
> Beyond the primary key, items are schema-less — each item can have any number of attributes (up to the 400 KB item size limit), and different items in the same table can have completely different attributes. This makes it a key-value store with document-like flexibility."

**Interviewer:** "Exactly right. And secondary indexes?"

> "Yes — two types:
> - **Global Secondary Index (GSI)** — lets you query on a completely different partition key and sort key. Up to 20 GSIs per table. Eventually consistent only.
> - **Local Secondary Index (LSI)** — same partition key as the base table, but a different sort key. Up to 5 per table. Must be defined at table creation time. Supports strongly consistent reads. Has a 10 GB per partition key limit.
>
> I'll focus on the core read/write path first, then we can deep-dive into indexes."

**Interviewer:** "Good plan. What about non-functional requirements?"

**Non-Functional Requirements:**

> "DynamoDB's identity is defined by its non-functional properties. Let me lay them out:
>
> | Dimension | Requirement | Rationale |
> |---|---|---|
> | **Latency** | Single-digit milliseconds for reads and writes at any scale | This is DynamoDB's core promise. P50 < 5ms, P99 < 10ms for a single-item read/write. |
> | **Scalability** | Virtually unlimited throughput and storage | Tables can grow from 0 to petabytes, 0 to millions of requests/sec, without any schema change or downtime. |
> | **Durability** | Data durably persisted across 3 AZs | When DynamoDB returns HTTP 200, the write is durable. No data loss even if an entire AZ goes down. |
> | **Availability** | 99.999% for Global Tables, 99.99% for regional tables | The service must always be reachable. |
> | **Consistency** | Eventually consistent by default; strongly consistent opt-in | Default reads may return slightly stale data. Setting `ConsistentRead=true` reads from the leader — at 2x the RCU cost. GSIs support only eventually consistent reads. |
> | **Predictability** | Consistent low latency regardless of table size | Whether a table has 1 KB or 1 PB of data, a GetItem should take the same time. |
> | **Multi-tenancy** | Millions of tables across millions of customers sharing infrastructure | One customer's hot key must not degrade another customer's latency. |
>
> The latency and predictability guarantees are what make DynamoDB different from most databases. A traditional RDBMS gets slower as data grows. DynamoDB must not."

**Interviewer:**
You mentioned two consistency modes. Can you explain the difference more precisely?

**Candidate:**

> "Sure. DynamoDB replicates each partition's data across 3 storage nodes in different Availability Zones. One of those replicas is elected the **leader** using a Paxos-based protocol (per the 2022 USENIX ATC paper by Elhemali et al.):
>
> - **Eventually consistent read** (default): The request can be served by **any** of the 3 replicas. This means you might read slightly stale data if a recent write hasn't propagated to the replica you hit. It consumes **0.5 RCU** per 4 KB.
> - **Strongly consistent read**: The request is routed to the **leader replica** only, which is guaranteed to have the latest committed write. It consumes **1 RCU** per 4 KB — twice the cost.
>
> The key insight: DynamoDB is NOT leaderless like the original 2007 Dynamo paper. It uses a **leader-per-partition** model with Multi-Paxos for leader election and log replication. This is a critical distinction that many people get wrong."

**Interviewer:**
Good — that's an important distinction. Let's get some numbers.

---

### L5 vs L6 vs L7 — Phase 2 Rubric

| Aspect | L5 (SDE-2) | L6 (SDE-3) | L7 (Principal) |
|---|---|---|---|
| **Functional Reqs** | Lists CRUD operations, mentions primary key | Proactively distinguishes partition key vs composite key, raises GSI/LSI, knows item size limit (400 KB) | Additionally discusses Streams, transactions, Global Tables, TTL — scopes them for later |
| **Non-Functional** | Mentions low latency and scalability | Quantifies latency targets (single-digit ms), explains two consistency modes with RCU cost, references Paxos | Frames NFRs as SLA commitments, discusses blast radius isolation, distinguishes DynamoDB from the Dynamo paper |
| **Data Model** | "It's a key-value store" | Explains partition key + sort key model, schema-less items, 400 KB limit | Discusses implications: no joins, single-table design patterns, access-pattern-driven modeling |

---

## PHASE 3: Scale Estimation & Capacity Model (~5 min)

**Candidate:**

> "Let me estimate the scale and understand the capacity model, because DynamoDB's design is driven by the need to deliver predictable performance at any scale."

#### Scale Estimates

> "DynamoDB reportedly handles trillions of requests per day across hundreds of thousands of AWS customers. Let me work with representative numbers:
>
> - **Tables managed**: Hundreds of millions across all customers
> - **Peak request rate**: Tens of millions of requests per second globally
> - **Data stored**: Petabytes to exabytes across all tables
> - **Single large table**: Can grow to hundreds of TB or more
> - **Single partition throughput ceiling**: 3,000 RCU and 1,000 WCU per partition (verified from AWS docs)
> - **Partition size limit**: 10 GB per partition — when exceeded, the partition splits"

#### Capacity Model (RCU/WCU)

> "DynamoDB has a unique capacity model that's central to its design. Let me lay out the math:
>
> **Read Capacity Units (RCU):**
> - 1 RCU = 1 strongly consistent read/sec for items up to 4 KB
> - 1 RCU = 2 eventually consistent reads/sec for items up to 4 KB
> - For larger items: round up to next 4 KB boundary. A 20 KB item consumes 5 RCU for a strongly consistent read.
>
> **Write Capacity Units (WCU):**
> - 1 WCU = 1 write/sec for items up to 1 KB
> - For larger items: round up to next 1 KB boundary. A 3.5 KB item consumes 4 WCU.
>
> **Two capacity modes:**
>
> | Mode | How It Works | Best For |
> |---|---|---|
> | **On-Demand** | Pay-per-request, auto-scales instantly, no planning | Unpredictable workloads, new tables, spiky traffic |
> | **Provisioned** | You specify RCU/WCU, charged hourly, auto-scaling optional | Predictable workloads where you want cost control |
>
> **Default throughput quotas** (per AWS docs):
> - Per table (on-demand): 40,000 read request units / 40,000 write request units
> - Per table (provisioned): 40,000 RCU / 40,000 WCU
> - Per account (provisioned): 80,000 RCU / 80,000 WCU
> - These are soft limits — can be increased via service quota request.
>
> **Why the capacity model matters for design:**
> The RCU/WCU model directly maps to how DynamoDB allocates partition throughput. Each partition can handle up to 3,000 RCU and 1,000 WCU. If your table has 10,000 WCU provisioned, DynamoDB needs at least 10 partitions to serve that throughput. This means the partitioning scheme must be capacity-aware, not just data-size-aware."

**Interviewer:**
Good. That connection between capacity model and partitioning is important. Let's design the system.

---

## PHASE 4: API Design (~3 min)

**Candidate:**

> "Before architecture, let me nail down the core API contracts. These drive the system's internal data flow."

#### Core APIs

> ```
> // Write an item (create or full replace)
> PutItem(TableName, Item{pk, sk?, ...attributes}, ConditionExpression?)
>   → 200 OK | ConditionalCheckFailedException
>
> // Read a single item by full primary key
> GetItem(TableName, Key{pk, sk?}, ConsistentRead=false, ProjectionExpression?)
>   → Item | 200 OK (empty if not found)
>
> // Query items sharing a partition key, range on sort key
> Query(TableName, KeyConditionExpression, FilterExpression?,
>       ProjectionExpression?, ScanIndexForward=true, Limit?,
>       ExclusiveStartKey?, ConsistentRead=false)
>   → Items[], LastEvaluatedKey?, Count, ScannedCount
>
> // Update specific attributes (partial update, or create if missing)
> UpdateItem(TableName, Key{pk, sk?}, UpdateExpression,
>            ConditionExpression?, ReturnValues?)
>   → Attributes (old/new based on ReturnValues)
>
> // Delete an item
> DeleteItem(TableName, Key{pk, sk?}, ConditionExpression?)
>   → 200 OK
>
> // Batch operations
> BatchGetItem(RequestItems: {TableName: {Keys: [...], ConsistentRead?}}[])
>   → Responses, UnprocessedKeys    // max 100 items, 16 MB
>
> BatchWriteItem(RequestItems: {TableName: {PutRequest | DeleteRequest}[]}[])
>   → UnprocessedItems              // max 25 items, 16 MB
>
> // Transactions (ACID across up to 100 items)
> TransactWriteItems(TransactItems: {Put|Update|Delete|ConditionCheck}[])
>   → 200 OK                        // max 100 items, 4 MB aggregate
>
> TransactGetItems(TransactItems: {Get}[])
>   → Responses                     // max 100 items, 4 MB aggregate
> ```
>
> **Key API design decisions:**
> - **GetItem requires the full primary key** — you cannot get an item by only the sort key. This is because routing depends on the partition key hash.
> - **Query requires the partition key** — you can range on the sort key, but the partition key must be specified. This ensures the query hits a single partition (fast).
> - **Scan reads the entire table** — it's expensive and should be avoided in production. It exists for data export/migration.
> - **Conditional writes** via `ConditionExpression` enable optimistic concurrency control. The write only succeeds if the condition evaluates to true.
> - **Transactions** consume 2x the normal RCU/WCU because of the two-phase protocol (prepare + commit)."

**Interviewer:**
Why does Query require the partition key? Why can't I just query by sort key?

**Candidate:**

> "Because the partition key determines which physical partition stores the data. Without it, we'd have to fan out the query to every partition — essentially a Scan. The entire architecture is built on the principle that the partition key routes you to the right partition in O(1) time.
>
> If you need to query by a different attribute, that's exactly what a GSI is for — it creates a separate physical copy of the data organized by a different partition key."

---

### L5 vs L6 vs L7 — Phase 3/4 Rubric

| Aspect | L5 (SDE-2) | L6 (SDE-3) | L7 (Principal) |
|---|---|---|---|
| **Capacity Model** | Knows RCU/WCU exist | Calculates RCU/WCU for various item sizes, connects capacity to partitioning | Models cost: on-demand vs provisioned breakeven, reserved capacity economics |
| **API Design** | Lists GET/PUT operations | Explains why Query requires partition key, describes conditional writes and transactions with 2x cost | Discusses single-table design patterns, sparse GSIs, API pagination with LastEvaluatedKey |
| **Scale Reasoning** | "DynamoDB is fast" | Quantifies per-partition limits (3,000 RCU / 1,000 WCU / 10 GB), explains auto-split triggers | Discusses adaptive capacity, burst credits (5 min / 300 sec), hot partition isolation |

---

## PHASE 5: High-Level Architecture (~5 min)

**Candidate:**

> "Let me start with the simplest possible design, find the problems, and evolve."

#### Attempt 0: Single Server with a Hash Map

> "Simplest possible design — one machine, in-memory hash map:
>
> ```
>     Client
>       │
>       ▼
>   ┌──────────────────────────┐
>   │    Single Server          │
>   │                          │
>   │   PutItem(pk, item)      │
>   │   → hashmap[pk] = item   │
>   │                          │
>   │   GetItem(pk)            │
>   │   → return hashmap[pk]   │
>   │                          │
>   │   In-Memory HashMap      │
>   └──────────────────────────┘
> ```
>
> Fast? Yes — O(1) lookups. But three fatal problems."

**Interviewer:**
What are they?

**Candidate:**

> "1. **No durability** — if the process crashes or the machine loses power, all data is gone.
> 2. **No availability** — single point of failure. Machine goes down, service is down.
> 3. **No scalability** — limited to one machine's RAM. Can't store petabytes in memory.
>
> Let me fix durability first."

#### Attempt 1: Add Persistence (WAL + B-Tree on SSD)

> "To survive crashes, I need to persist writes to disk before acknowledging them:
>
> ```
>     Client
>       │
>       ▼
>   ┌──────────────────────────────────┐
>   │        Single Server             │
>   │                                  │
>   │  PutItem(pk, item):             │
>   │    1. Append to WAL (SSD)       │  ← sequential write, fast
>   │    2. Update in-memory B-tree   │
>   │    3. Periodically flush B-tree │
>   │       to SSD (checkpoint)       │
>   │    4. Return 200 OK             │
>   │                                  │
>   │  GetItem(pk):                   │
>   │    1. Lookup in B-tree (mem)    │
>   │    2. If not in memory, read    │
>   │       from SSD                  │
>   │    3. Return item               │
>   │                                  │
>   │  ┌─────────┐  ┌──────────────┐  │
>   │  │  WAL    │  │  B-Tree      │  │
>   │  │  (SSD)  │  │  (SSD+mem)   │  │
>   │  └─────────┘  └──────────────┘  │
>   └──────────────────────────────────┘
> ```
>
> **Why a Write-Ahead Log (WAL)?**
> - Writing to the B-tree on disk for every PutItem is slow (random I/O).
> - Instead, we append to a sequential log first (fast), update the in-memory tree, and periodically flush.
> - On crash recovery: replay the WAL from the last checkpoint to rebuild the in-memory state.
>
> **Why a B-tree (not an LSM-tree)?**
> Per the 2022 DynamoDB paper, DynamoDB storage nodes use a B-tree on SSD — not an LSM-tree. [INFERRED — the 2022 USENIX ATC paper mentions B-trees but the internal storage engine details are not fully public]. B-trees provide predictable read latency (no compaction storms), which aligns with DynamoDB's latency guarantees. LSM-trees have better write throughput but unpredictable read latency due to compaction.
>
> Now we have durability. But still no availability or scalability."

#### Attempt 2: Add Replication (Paxos, 3 Replicas Across AZs)

> "To survive machine failure and provide high availability, I replicate the data to 3 storage nodes across 3 Availability Zones:
>
> ```
>     Client
>       │
>       ▼
>   ┌──────────────┐
>   │ Request       │  ─── Knows which partition owns the key
>   │ Router        │
>   └──────┬───────┘
>          │
>    ┌─────┼──────────────────┐
>    │     │                  │
>    ▼     ▼                  ▼
>  ┌──────────┐ ┌──────────┐ ┌──────────┐
>  │  AZ-a    │ │  AZ-b    │ │  AZ-c    │
>  │ Storage  │ │ Storage  │ │ Storage  │
>  │ Node     │ │ Node     │ │ Node     │
>  │ (LEADER) │ │ (follower│ │ (follower│
>  │          │ │  )       │ │  )       │
>  │ WAL+     │ │ WAL+     │ │ WAL+     │
>  │ B-tree   │ │ B-tree   │ │ B-tree   │
>  └──────────┘ └──────────┘ └──────────┘
>       ▲              ▲           ▲
>       └──── Paxos replication ───┘
> ```
>
> **Replication protocol (per the 2022 DynamoDB paper):**
> - Each partition has 3 replicas. One is the **leader**, elected via Multi-Paxos.
> - **Writes** go to the leader. The leader appends to its WAL, then replicates the log entry to followers. Once a **majority (2 of 3)** acknowledge, the write is considered durable, and the leader responds 200 OK.
> - **Eventually consistent reads** can go to **any** of the 3 replicas (closest, fastest).
> - **Strongly consistent reads** go to the **leader** only, guaranteeing the latest committed data.
> - If the leader fails, Paxos elects a new leader from the remaining 2 replicas. The system remains available as long as 2 of 3 replicas are up.
>
> **Important: This is NOT leaderless.** The original 2007 Dynamo paper described a leaderless system with vector clocks, sloppy quorums, and consistent hashing. DynamoDB the service (launched 2012) uses a fundamentally different architecture — leader-per-partition with Paxos. No vector clocks, no sloppy quorums."

**Interviewer:**
Good — you're correctly distinguishing DynamoDB from the Dynamo paper. But we have 3 replicas of a single partition. What happens when a table grows to 100 TB?

**Candidate:**

> "100 TB can't fit on 3 machines. We need **partitioning** — splitting the table's data across many partition groups, each independently replicated.
>
> This leads to the real architecture. Let me draw it."

---

#### Attempt 3: Full Architecture — Partitioning + Replication

> ```
> FULL ARCHITECTURE:
>
>                            ┌──────────────────────┐
>                            │       Clients         │
>                            │  (SDKs, CLI, Console) │
>                            └──────────┬───────────┘
>                                       │ HTTPS (REST API)
>                            ┌──────────▼───────────┐
>                            │    Request Router     │
>                            │  (Stateless fleet)    │
>                            │                       │
>                            │  • Auth (SigV4)       │
>                            │  • hash(pk) →         │
>                            │    partition lookup    │
>                            │  • Route to leader    │
>                            │    or any replica     │
>                            └──────────┬───────────┘
>                                       │
>                    ┌──────────────────┼──────────────────┐
>                    │                  │                  │
>         ┌─────────▼────────┐  ┌──────▼──────┐  ┌───────▼────────┐
>         │  Partition        │  │ Partition    │  │  Partition      │
>         │  Metadata         │  │ Group 1      │  │  Group N        │
>         │  System           │  │              │  │                 │
>         │                   │  │ Leader (AZ-a)│  │ Leader (AZ-c)  │
>         │ hash(pk) →        │  │ Follow (AZ-b)│  │ Follow (AZ-a)  │
>         │   partition ID    │  │ Follow (AZ-c)│  │ Follow (AZ-b)  │
>         │                   │  │              │  │                 │
>         │ partition ID →    │  │ Paxos group  │  │ Paxos group    │
>         │   {leader, nodes} │  │ ≤10GB data   │  │ ≤10GB data     │
>         │                   │  │ ≤3,000 RCU   │  │ ≤3,000 RCU     │
>         │ Split/merge       │  │ ≤1,000 WCU   │  │ ≤1,000 WCU     │
>         │ decisions         │  │              │  │                 │
>         └───────────────────┘  └──────────────┘  └────────────────┘
> ```
>
> **The three key components:**
>
> 1. **Request Router** — Stateless fleet of front-end servers. Receives client requests, authenticates (SigV4), computes `hash(partition_key)` to determine which partition owns this key, then routes to the appropriate storage node (leader for writes/consistent reads, any replica for eventually consistent reads). Caches the partition map for fast lookups.
>
> 2. **Partition Metadata System** — Knows the mapping from partition key ranges to partition groups, and from partition groups to physical storage nodes. Handles partition splits, merges, and leader elections. This is the "brain" of the system.
>
> 3. **Storage Nodes (Partition Groups)** — Each partition group is a Paxos replication group of 3 storage nodes across 3 AZs. Each node stores a B-tree + WAL for the partition's data. A partition holds up to 10 GB of data and can serve up to 3,000 RCU and 1,000 WCU.
>
> **How a PutItem works:**
> 1. Client sends `PutItem(Table, {pk: "user123", sk: "order#456"}, {amount: 99.99})`
> 2. Request router authenticates, computes `hash("user123")`, looks up partition map → Partition Group 7
> 3. Router forwards the write to the **leader** of Partition Group 7 (in AZ-a)
> 4. Leader appends to its WAL, sends log entry to both followers
> 5. One follower (AZ-b) acknowledges → **majority (2/3)** reached
> 6. Leader applies the write to its B-tree, responds 200 OK to the router
> 7. Router returns 200 OK to the client
> 8. The third replica (AZ-c) eventually catches up (may lag by milliseconds)
>
> **How a GetItem works (eventually consistent):**
> 1. Client sends `GetItem(Table, {pk: "user123", sk: "order#456"})`
> 2. Router computes `hash("user123")` → Partition Group 7
> 3. Router picks **any** of the 3 replicas (e.g., the nearest one in AZ-b)
> 4. Replica does a B-tree lookup, returns the item
> 5. May return slightly stale data if the latest write hasn't replicated yet
>
> **How a GetItem works (strongly consistent):**
> Same as above, but step 3 routes to the **leader** instead of any replica. This guarantees the latest committed data."

**Interviewer:**
Good architecture. But I see an issue — how does the partition metadata system know when to split a partition? And what happens during a split?

**Candidate:**

> "Great question — let me address both:
>
> **Split triggers** (a partition splits when either threshold is exceeded):
> - **Size-based**: Partition data exceeds **10 GB**
> - **Throughput-based**: Partition receives more traffic than its allocated throughput can handle
>
> **How a split works:**
> 1. The partition metadata system detects a split is needed
> 2. It chooses a split point in the key range (e.g., the median key)
> 3. Two new partitions are created with fresh replication groups
> 4. Data is copied/migrated from the old partition to the two new partitions
> 5. The partition map is updated atomically
> 6. Request routers refresh their cached partition maps
> 7. The old partition is decommissioned
>
> During the split, the old partition continues serving requests. There may be brief elevated latency, but no downtime. The key design goal is that splits are transparent to clients.
>
> **Why this matters operationally:** A table with 100 GB of data and 10,000 WCU needs at least max(100GB / 10GB, 10,000 / 1,000) = max(10, 10) = 10 partitions. The system auto-adjusts, but understanding this math is critical for capacity planning."

---

#### Architecture Evolution After Phase 5

| | Attempt 0 | Attempt 1 | Attempt 2 | Attempt 3 (Current) |
|---|---|---|---|---|
| **Durability** | None (in-memory) | WAL + B-tree on SSD | 3x replication across AZs | 3x replication per partition |
| **Availability** | Single point of failure | Single point of failure | Paxos leader election | Paxos per partition, independent failure |
| **Scalability** | Single machine | Single machine | Single partition | Hash-based partitioning, auto-split |
| **Consistency** | Trivial (single writer) | Trivial (single writer) | Leader-based strong reads | Same, per partition |

---

### L5 vs L6 vs L7 — Phase 5 Rubric

| Aspect | L5 (SDE-2) | L6 (SDE-3) | L7 (Principal) |
|---|---|---|---|
| **Architecture** | Draws 3-tier diagram | Explains request router → partition map → storage nodes, correctly identifies leader-based Paxos replication | Discusses partition metadata system availability, how router caches are invalidated during splits, blast radius of metadata failures |
| **Replication** | "Replicate to 3 AZs" | Distinguishes DynamoDB (Paxos, leader-per-partition) from Dynamo paper (leaderless), explains majority-write | Discusses Multi-Paxos optimization, log replication vs state-machine replication, leader lease mechanism |
| **Partitioning** | "Shard by hash of key" | Explains split triggers (10 GB size, throughput ceiling), partition math for capacity planning | Discusses split mechanics during live traffic, partition migration without downtime, key-range vs hash-range tradeoffs |

---

## PHASE 6: Deep Dive — Partitioning Model (~8 min)

**Interviewer:**
Let's go deeper on partitioning. How exactly does DynamoDB map a partition key to a partition?

**Candidate:**

> "DynamoDB uses **hash-based partitioning** on the partition key. The system computes an internal hash function over the partition key value, producing a hash value in a fixed range. That range is divided among partitions:
>
> ```
> Hash Range:  [0 ──────────────────────────── 2^128)
>
> Partition P0: [0, hash_boundary_1)           → Storage Nodes {A, B, C}
> Partition P1: [hash_boundary_1, hash_bound_2) → Storage Nodes {D, E, F}
> Partition P2: [hash_boundary_2, hash_bound_3) → Storage Nodes {G, H, I}
> ...
> Partition Pn: [hash_boundary_n, 2^128)       → Storage Nodes {X, Y, Z}
>
> Request: GetItem(pk="user123")
>   → hash("user123") = 0x7A3F...
>   → falls in P1's range
>   → route to one of {D, E, F}
> ```
>
> **Why hash-based (not range-based)?**
>
> | Aspect | Hash-Based (DynamoDB's approach) | Range-Based |
> |---|---|---|
> | Point lookups (GetItem) | O(1) — hash directly to partition | O(log P) — binary search on ranges |
> | Uniform distribution | Excellent — hash function spreads keys evenly | Depends on key distribution, can create hotspots |
> | Range queries (Query by sort key) | Supported WITHIN a partition (items with same PK stored sorted by SK) | Natural across partitions |
> | Cross-partition range scans | Not supported by design — Query requires PK | Naturally efficient |
>
> The critical insight: DynamoDB separates **distribution** (across partitions via hash of PK) from **ordering** (within a partition via sort key). This is why you must specify the partition key in a Query — it determines which single partition to look in, and then the sort key enables range scans within that partition.
>
> **Within a partition**, items with the same partition key are stored **sorted by sort key** in the B-tree. This enables efficient `Query` operations with sort key conditions like `BETWEEN`, `begins_with`, `>`, `<`, etc."

**Interviewer:**
What happens if one partition key becomes extremely hot — say a celebrity's user ID during a viral event?

**Candidate:**

> "This is the hot partition problem. DynamoDB has evolved significantly to handle this:
>
> **Layer 1: Burst Capacity**
> - DynamoDB reserves unused throughput for later bursts
> - Retains up to **5 minutes (300 seconds)** of unused read and write capacity
> - If a partition was underutilized, it can absorb a burst above its provisioned limit
> - This handles short-lived spikes automatically
>
> **Layer 2: Adaptive Capacity**
> - If a partition consistently receives disproportionate traffic, DynamoDB **instantly increases that partition's throughput allocation** by borrowing unused capacity from other partitions
> - Example: A table with 400 WCU across 4 partitions (100 WCU each). Partitions 1-3 use 50 WCU. Partition 4 needs 150 WCU. Adaptive capacity automatically boosts Partition 4 to 150 WCU by reallocating unused capacity from Partitions 1-3
> - This is automatic and free — no configuration needed
> - **Hard ceiling**: A single partition can never exceed 3,000 RCU / 1,000 WCU regardless of adaptive capacity
>
> **Layer 3: Partition Split for Throughput**
> - If a partition consistently exceeds its throughput ceiling, the partition metadata system triggers a split
> - The hot partition's key range is divided into two partitions, each getting half the key range and independent throughput
> - This only helps if the hot traffic is spread across multiple keys within the partition. If ONE key is hot (a true hot key), splitting doesn't help — both halves of the split would still contain that key
>
> **Layer 4: Application-Level Mitigation (for true hot keys)**
> - Write sharding: Append a random suffix to the partition key (e.g., `celebrity#1`, `celebrity#2`, ..., `celebrity#10`) and scatter writes across 10 partition keys. Read by querying all 10 and merging.
> - Caching: Put DAX (DynamoDB Accelerator) in front to absorb repeated reads
> - These require application changes — DynamoDB can't solve a true single-key hotspot automatically
>
> **What the customer sees:** Throttling. DynamoDB returns `ProvisionedThroughputExceededException` (HTTP 400) when a partition is overloaded. SDKs implement exponential backoff with jitter. The key metric to monitor is `ThrottledRequests` in CloudWatch."

**Interviewer:**
Good. How does the request router know which partition to send a request to?

**Candidate:**

> "The request router maintains a **cached partition map** — a routing table that maps hash ranges to partition groups:
>
> ```
> Partition Map (simplified):
>   Table: UserOrders
>   [0x0000, 0x3FFF] → Partition Group 1 → Leader: node-a7, Followers: node-b3, node-c9
>   [0x4000, 0x7FFF] → Partition Group 2 → Leader: node-c2, Followers: node-a1, node-b8
>   [0x8000, 0xBFFF] → Partition Group 3 → Leader: node-b5, Followers: node-c4, node-a6
>   [0xC000, 0xFFFF] → Partition Group 4 → Leader: node-a3, Followers: node-b7, node-c1
> ```
>
> The router caches this map locally for fast lookups. When a partition splits or a leader changes, the partition metadata system pushes updates to all routers. During the brief transition:
> - If a router sends a request to the wrong partition, the storage node returns an error/redirect
> - The router refreshes its partition map and retries
> - This adds one extra round-trip (milliseconds) — rare and transparent to the client
>
> [INFERRED — the exact partition map refresh mechanism (push vs pull, gossip vs direct notification) is not officially documented, but the 2022 paper mentions a partition metadata system that routers consult]"

---

### L5 vs L6 vs L7 — Phase 6 Rubric

| Aspect | L5 (SDE-2) | L6 (SDE-3) | L7 (Principal) |
|---|---|---|---|
| **Partitioning scheme** | "Hash the partition key" | Explains hash-range mapping, split triggers (10 GB / throughput), within-partition sort key ordering | Discusses hash function properties, partition map consistency during splits, anti-entropy mechanisms |
| **Hot partitions** | "Distribute keys evenly" | Explains burst capacity (300 sec), adaptive capacity, per-partition ceiling (3,000/1,000), write sharding | Discusses isolation guarantees across tenants, how adaptive capacity interacts with on-demand mode, monitoring ThrottledRequests |
| **Request routing** | "Front-end routes to shard" | Explains partition map caching, refresh on split, redirect mechanism | Discusses routing latency during split transitions, partition map consistency protocol, blast radius of metadata service failure |

---

## PHASE 7: Deep Dive — Storage Nodes & Replication (~8 min)

**Interviewer:**
Let's go deeper on the storage layer. Tell me about the storage node architecture and how Paxos replication works in detail.

**Candidate:**

> "Each partition in DynamoDB is a **replication group** — typically 3 storage nodes, one per Availability Zone. Per the 2022 USENIX ATC paper by Elhemali et al., this is the internal structure:"

#### Storage Node Architecture

> ```
> Single Storage Node:
>
> ┌─────────────────────────────────────┐
> │         Storage Node                │
> │                                     │
> │  ┌────────────────────────────┐    │
> │  │   Write-Ahead Log (WAL)    │    │  ← Sequential append, SSD
> │  │   Ordered log entries       │    │
> │  │   Entry: {LSN, pk, sk,     │    │
> │  │           item_data, ts}   │    │
> │  └────────────────────────────┘    │
> │                                     │
> │  ┌────────────────────────────┐    │
> │  │   B-Tree (on SSD)          │    │  ← Indexed by (pk, sk)
> │  │   Sorted by (pk, sk)       │    │
> │  │   Supports point lookups   │    │
> │  │   and range scans          │    │
> │  └────────────────────────────┘    │
> │                                     │
> │  ┌────────────────────────────┐    │
> │  │   Replication State        │    │  ← Paxos state machine
> │  │   • Role: leader/follower  │    │
> │  │   • Current term/ballot    │    │
> │  │   • Committed LSN          │    │
> │  │   • Last applied LSN       │    │
> │  └────────────────────────────┘    │
> └─────────────────────────────────────┘
> ```
>
> **Why B-tree and not LSM-tree?**
>
> | Aspect | B-Tree (DynamoDB's choice) | LSM-Tree |
> |---|---|---|
> | Read latency | Predictable — O(log N) lookups, no compaction interference | Variable — may need to check multiple levels, compaction storms cause latency spikes |
> | Write throughput | Lower (random I/O to update tree) | Higher (sequential writes to memtable + WAL) |
> | Space amplification | Lower (data stored once) | Higher (data exists in multiple levels until compacted) |
> | Predictability | High — no background compaction storms | Low — compaction can spike latency |
>
> DynamoDB chose B-trees because **predictable latency** is the #1 priority. A compaction storm in an LSM-tree could cause P99 latency to spike from 5ms to 50ms — unacceptable for DynamoDB's SLA. [INFERRED — the specific storage engine details beyond 'B-tree on SSD' are inferred from the 2022 paper's references; the exact implementation is not fully public]"

#### Paxos Replication in Detail

> "The 2022 paper describes DynamoDB using **Multi-Paxos** for each partition's replication group:
>
> ```
> Write Path (PutItem):
>
>   Client → Request Router → Leader (Storage Node in AZ-a)
>
>   Leader:
>     1. Assigns a Log Sequence Number (LSN) to the write
>     2. Appends entry to local WAL
>     3. Sends PrepareAndAccept to followers:
>        "Accept log entry LSN=47: PutItem(pk=user123, sk=order#456, ...)"
>     4. Follower AZ-b: appends to WAL, responds ACK
>     5. Follower AZ-c: appends to WAL, responds ACK
>     6. Leader: 2/3 ACKs received → entry is COMMITTED
>     7. Leader applies entry to B-tree
>     8. Leader responds 200 OK to router → client
>
>   After commit:
>     • Followers apply the committed entry to their B-trees asynchronously
>     • An eventually consistent read on a follower may see
>       a slightly old version (committed but not yet applied to B-tree)
>       or the latest version (already applied)
> ```
>
> **Leader election:**
> - If the leader fails (detected via heartbeat timeout), the remaining 2 replicas run Paxos to elect a new leader
> - The new leader recovers any uncommitted log entries from the previous term
> - During election (typically sub-second), the partition is briefly unavailable for writes
> - Reads (eventually consistent) can still be served by followers during leader election
>
> **Why Paxos and not Raft?**
> - Paxos and Raft are functionally equivalent for this use case
> - DynamoDB predates Raft's wide adoption
> - The 2022 paper specifically mentions Paxos
> - Both provide the same guarantees: leader election + replicated log with majority quorum
>
> **Durability guarantee:**
> When DynamoDB returns HTTP 200, the write has been persisted to the WAL of at least 2 of 3 storage nodes, each in a different AZ. Even if the leader immediately dies, the data is safe on at least one other node."

**Interviewer:**
What happens to the third replica that was slow to acknowledge?

**Candidate:**

> "The slow replica is not 'lost' — it catches up:
>
> 1. **Normal case**: It acknowledges slightly late (milliseconds). The leader still streams subsequent log entries to it. It applies them in order.
>
> 2. **Slow/partitioned replica**: If a replica falls behind significantly (seconds to minutes), the leader continues sending log entries. The replica replays them when it comes back. As long as the WAL on the leader isn't truncated past the replica's last acknowledged LSN, it can catch up.
>
> 3. **Failed replica (hardware failure)**: The partition metadata system detects the failure, provisions a new storage node, and adds it to the replication group. The new node catches up by replaying the WAL from the leader or from a snapshot + WAL tail.
>
> The key point: writes complete with 2/3 acknowledgement. The third replica's slowness or failure does NOT block writes or increase client-visible latency. This is the beauty of majority quorum — you tolerate 1 slow/failed node without any impact."

#### Architecture Update After Phase 7

| Component | Before (Phase 5) | After (Phase 7) |
|---|---|---|
| **Storage Engine** | "B-tree + WAL" (mentioned briefly) | B-tree on SSD with WAL, per-partition, designed for predictable latency |
| **Replication** | "Paxos, 3 replicas" (briefly) | Multi-Paxos: leader assigns LSN, replicates log, commits on 2/3, applies to B-tree |
| **Consistency** | "Leader for strong reads" (briefly) | Leader serves strong reads from committed B-tree; followers serve eventual reads |
| **Recovery** | Not discussed | Replica catch-up via WAL replay; new replica provisioned on hardware failure |

---

### L5 vs L6 vs L7 — Phase 7 Rubric

| Aspect | L5 (SDE-2) | L6 (SDE-3) | L7 (Principal) |
|---|---|---|---|
| **Storage Engine** | "Store data on disk" | Explains B-tree vs LSM-tree tradeoff, why B-tree for predictable latency | Discusses B-tree page splits, SSD wear leveling, compaction-free design benefits for P99 |
| **Replication** | "Replicate across AZs" | Explains Multi-Paxos: leader election, log replication, majority commit, follower catch-up | Discusses Paxos ballot numbers, leader leases to avoid split-brain, log truncation strategy |
| **Recovery** | "Failover to another node" | Explains WAL replay, new replica provisioning, leader election timing | Discusses recovery time vs partition availability, read availability during leader election, gray failure detection |

---

## PHASE 8: Deep Dive — Consistent vs Eventually Consistent Reads (~5 min)

**Interviewer:**
Let's go deeper on the consistency model. Walk me through exactly what happens for each type of read and why eventually consistent is cheaper.

**Candidate:**

> "Let me trace both paths in detail:
>
> **Eventually Consistent Read (default):**
> ```
> Client → Router → ANY replica (nearest/fastest)
>
> 1. Router picks any of the 3 replicas (load-balanced, latency-optimized)
> 2. Chosen replica does a B-tree lookup
> 3. Returns the item as of its locally applied state
>
> Cost: 0.5 RCU per 4 KB (one eventually consistent read)
>
> Latency: Low — hits the nearest replica, no coordination
> Freshness: May be stale by milliseconds (time for leader's commit to propagate)
> ```
>
> **Strongly Consistent Read:**
> ```
> Client → Router → LEADER replica only
>
> 1. Router MUST route to the leader of the partition
> 2. Leader has all committed writes applied to its B-tree
> 3. Leader does a B-tree lookup
> 4. Returns the item — guaranteed to reflect all prior committed writes
>
> Cost: 1 RCU per 4 KB (one strongly consistent read)
>
> Latency: May be slightly higher — must go to leader, which may not be the closest replica
> Freshness: Always up-to-date with all committed writes
> ```
>
> **Why is eventually consistent cheaper (0.5 RCU vs 1 RCU)?**
>
> It's not that the read operation itself is computationally cheaper. Both do the same B-tree lookup. The cost difference reflects two things:
>
> 1. **Load distribution**: Eventually consistent reads spread across 3 replicas, so each replica serves ~1/3 of reads. Strongly consistent reads all go to the leader, creating a bottleneck. The 2x cost incentivizes customers to use eventually consistent when they can, reducing leader load.
>
> 2. **Leader is precious**: The leader handles ALL writes plus strongly consistent reads. Its throughput is the partition's write ceiling. Routing every read to the leader would halve write throughput. By pricing strongly consistent reads at 2x, DynamoDB discourages overloading the leader.
>
> **When do you NEED strongly consistent reads?**
> - Read-after-write scenarios (e.g., user updates profile, immediately reads it back)
> - Distributed locking / leader election via DynamoDB
> - Financial transactions where stale reads are unacceptable
>
> **When is eventually consistent fine?**
> - Displaying product catalog data (can tolerate seconds of staleness)
> - Analytics dashboards
> - Any scenario where 'last few milliseconds' of staleness is acceptable
>
> **Important limitations:**
> - Strongly consistent reads are NOT supported on Global Secondary Indexes — GSIs are always eventually consistent
> - Strongly consistent reads are supported on Local Secondary Indexes
> - DynamoDB Streams reads are always eventually consistent"

**Interviewer:**
What if the leader just committed a write but hasn't finished applying it to the B-tree? Could a strongly consistent read miss it?

**Candidate:**

> "No — the leader tracks the committed LSN. A strongly consistent read must return all writes up to the committed LSN, even if they haven't been applied to the B-tree yet. The leader can either:
>
> 1. Wait for the B-tree to catch up to the committed LSN (adds latency but guarantees freshness)
> 2. Read from the B-tree AND check the WAL for any entries between 'last applied' and 'committed' (more complex but faster)
>
> [INFERRED — the exact mechanism for serving strongly consistent reads during B-tree lag is not officially documented, but the committed LSN tracking is standard in Paxos implementations]
>
> The guarantee is: once DynamoDB returns 200 for a write, any subsequent strongly consistent read will see that write. This is read-committed isolation — reads never return uncommitted data, and strongly consistent reads never return stale data."

---

### L5 vs L6 vs L7 — Phase 8 Rubric

| Aspect | L5 (SDE-2) | L6 (SDE-3) | L7 (Principal) |
|---|---|---|---|
| **Consistency model** | "Eventually consistent is default, strongly consistent costs more" | Traces both read paths, explains why EC costs 0.5 RCU (load distribution, leader protection), knows GSI limitation | Discusses linearizability of strongly consistent reads, read-committed isolation for EC reads, how leader lease prevents stale strong reads |
| **When to use which** | "Use strongly consistent when you need fresh data" | Gives concrete examples (read-after-write, distributed locking vs catalogs), quantifies cost difference | Discusses application-level consistency patterns (conditional writes + EC reads as alternative to SC reads) |
| **Edge cases** | Doesn't consider | Raises leader-commit-vs-B-tree-apply question | Discusses leader failover during read, how uncommitted entries are handled on new leader |

---

## PHASE 9: Deep Dive — Global Secondary Indexes & Local Secondary Indexes (~8 min)

**Interviewer:**
Let's talk about secondary indexes. How do GSIs work internally, and why are they eventually consistent only?

**Candidate:**

> "A GSI is essentially a **separate DynamoDB table** maintained automatically by the system, with a different primary key schema:
>
> ```
> Base Table: UserOrders
>   PK: user_id    SK: order_id
>   Attributes: amount, status, created_at
>
> GSI: StatusIndex
>   PK: status     SK: created_at
>   Projected: user_id, order_id, amount
>
> When you write to UserOrders:
>   PutItem({user_id: 'u1', order_id: 'o5', amount: 99, status: 'PENDING', created_at: '2026-01-15'})
>
> DynamoDB ASYNCHRONOUSLY propagates to StatusIndex:
>   {status: 'PENDING', created_at: '2026-01-15', user_id: 'u1', order_id: 'o5', amount: 99}
> ```
>
> **Internal architecture of a GSI:**
>
> ```
>   Base Table                         GSI (StatusIndex)
>   ┌────────────────┐                ┌────────────────┐
>   │ Partition by   │   async        │ Partition by   │
>   │ hash(user_id)  │ ──replication──│ hash(status)   │
>   │                │                │                │
>   │ PG1: users a-m │                │ PG1: PENDING   │
>   │ PG2: users n-z │                │ PG2: SHIPPED   │
>   │                │                │ PG3: DELIVERED  │
>   └────────────────┘                └────────────────┘
>
>   • Different partition key → different partition layout
>   • Different number of partitions
>   • Separate throughput allocation (GSI has its own RCU/WCU)
>   • Completely separate physical storage
> ```
>
> **Why GSIs are eventually consistent only:**
>
> The base table and GSI are partitioned by DIFFERENT keys. A single PutItem on the base table touches one base-table partition, but the corresponding GSI update might go to a completely different GSI partition (potentially on different storage nodes). Making this synchronous would mean:
>
> 1. Every write would need to coordinate across two independent Paxos groups (base table partition + GSI partition)
> 2. This is a distributed transaction across partitions — it would at least double write latency
> 3. If the GSI partition is temporarily overloaded, it would block base table writes
>
> Instead, DynamoDB decouples them:
> - Base table write completes when the base-table Paxos group commits (fast, single partition)
> - GSI update is propagated asynchronously — usually within fractions of a second
> - If the GSI is slow (throttled, overloaded), it doesn't block base table writes
>
> **Cost implication:** When you write to a table with GSIs, you pay WCU for the base table write AND WCU for each GSI that needs updating. A table with 5 GSIs where every write touches all 5 indexes pays 6x WCU. This is why you must provision sufficient write capacity on GSIs — if GSI write capacity is insufficient, it can throttle the base table writes.
>
> **GSI key constraints:**
> - Up to 20 GSIs per table (default quota)
> - GSI partition key can be any scalar attribute (String, Number, Binary)
> - Sort key is optional
> - Supports multi-attribute keys: up to 4 attributes for partition key, up to 4 for sort key, total 8
> - Key values don't need to be unique (unlike the base table)
> - Items missing the GSI key attributes are NOT propagated to the GSI — this enables 'sparse indexes'"

**Interviewer:**
What about Local Secondary Indexes? How do they differ?

**Candidate:**

> "LSIs are fundamentally different from GSIs because they share the same partition:
>
> ```
> Base Table: UserOrders
>   PK: user_id    SK: order_id
>
> LSI: AmountIndex
>   PK: user_id    SK: amount  (same PK, different SK)
>
> ┌────────────────────────────────────┐
> │  Single Partition (same PK group)  │
> │                                    │
> │  Base Table data:                  │
> │    (user_id=u1, order_id=o1) → {amount: 50, status: PENDING}
> │    (user_id=u1, order_id=o2) → {amount: 99, status: SHIPPED}
> │    (user_id=u1, order_id=o3) → {amount: 25, status: PENDING}
> │                                    │
> │  LSI data (AmountIndex):           │
> │    (user_id=u1, amount=25) → {order_id: o3, status: PENDING}
> │    (user_id=u1, amount=50) → {order_id: o1, status: PENDING}
> │    (user_id=u1, amount=99) → {order_id: o2, status: SHIPPED}
> │                                    │
> └────────────────────────────────────┘
> ```
>
> **Key differences from GSIs:**
>
> | Aspect | GSI | LSI |
> |---|---|---|
> | Partition key | Different from base table | **Same** as base table |
> | Physical location | Separate table with its own partitions | **Co-located** in same partition as base table items |
> | Consistency | Eventually consistent only | **Supports strongly consistent reads** |
> | Throughput | Separate RCU/WCU (must be provisioned independently) | **Shares** throughput with base table |
> | Size limit | No per-partition limit | **10 GB per partition key value** (across base table + all LSIs) |
> | Creation | Can be added/removed anytime | **Must be defined at table creation** — cannot be added later |
> | Max per table | 20 | 5 |
>
> **The 10 GB limit is critical:**
> Because the LSI data lives in the same partition as the base table data, the total size of all items sharing the same partition key (in the base table AND all LSIs) cannot exceed 10 GB. If you exceed this, DynamoDB returns `ItemCollectionSizeLimitExceededException` and blocks further writes for that partition key.
>
> **When to use LSI vs GSI:**
> - Use **LSI** when you need strongly consistent reads on an alternate sort order, and you're confident item collections stay under 10 GB
> - Use **GSI** for everything else — they're more flexible, have no size limit per partition key, and can be added after table creation
> - In practice, GSIs are far more commonly used. LSIs are a legacy feature that many teams avoid due to the 10 GB restriction and the inability to add them after table creation."

---

### L5 vs L6 vs L7 — Phase 9 Rubric

| Aspect | L5 (SDE-2) | L6 (SDE-3) | L7 (Principal) |
|---|---|---|---|
| **GSI internals** | "It's an alternate index" | Explains GSI as a separate table with async replication, explains why GSIs are EC only (cross-partition coordination cost) | Discusses GSI backfill mechanism when creating a new GSI, GSI write throttling propagation to base table, sparse index optimization |
| **LSI vs GSI** | "LSI is local, GSI is global" | Compares throughput sharing, 10 GB limit, consistency, creation constraints | Discusses when 10 GB limit becomes a design constraint, single-table design patterns to avoid LSIs, GSI overloading pattern |
| **Cost awareness** | "GSIs cost extra" | Calculates write amplification (N GSIs = N+1 WCU), explains why GSI capacity must be >= base table | Models total cost: base + GSI storage + GSI WCU, discusses projection strategy for storage optimization |

---

## PHASE 10: Deep Dive — DynamoDB Streams (~5 min)

**Interviewer:**
Tell me about DynamoDB Streams. How do they work internally?

**Candidate:**

> "DynamoDB Streams is a **change data capture (CDC)** feature that provides an ordered, time-series log of item-level changes in a table:
>
> ```
> Table: UserOrders
>
> T1: PutItem(user1, order1, {amount: 50})       → Stream record: INSERT {new: {amount: 50}}
> T2: UpdateItem(user1, order1, {amount: 75})     → Stream record: MODIFY {old: {amount: 50}, new: {amount: 75}}
> T3: DeleteItem(user1, order1)                   → Stream record: REMOVE {old: {amount: 75}}
> ```
>
> **Stream configuration (StreamViewType):**
>
> | View Type | Content | Use Case |
> |---|---|---|
> | `KEYS_ONLY` | Only key attributes | Lightweight triggers, just know 'something changed' |
> | `NEW_IMAGE` | Entire item after modification | Replicate current state |
> | `OLD_IMAGE` | Entire item before modification | Audit trail, rollback |
> | `NEW_AND_OLD_IMAGES` | Both before and after | Delta processing, change detection |
>
> **Internal architecture:**
>
> ```
> ┌──────────────┐     ┌──────────────┐     ┌─────────────────┐
> │ Base Table   │     │ DynamoDB     │     │ Consumers        │
> │ Partition    │────▶│ Stream       │────▶│                  │
> │              │     │              │     │ • Lambda triggers │
> │ Write commits│     │ Stream Shards│     │ • Kinesis adapter │
> │ generate     │     │ (ordered by  │     │ • Custom apps    │
> │ stream       │     │  partition   │     │                  │
> │ records      │     │  key)        │     │                  │
> └──────────────┘     └──────────────┘     └─────────────────┘
> ```
>
> **Ordering guarantee:**
> - For a given partition key (i.e., a specific item), stream records appear **in the exact same order** as the writes to that item
> - Across different partition keys, there is no ordering guarantee
> - This is because stream records are generated from the partition's WAL, which is ordered
>
> **Shard model:**
> - Stream records are organized into **shards** (similar to Kinesis shards)
> - Each shard corresponds roughly to a base table partition
> - When a base table partition splits, the stream shard also splits into child shards
> - Applications must process parent shards before child shards to maintain order
> - Shards are created and deleted automatically
>
> **Retention:**
> - Stream records have a **24-hour lifetime** — after that, they're automatically deleted
> - This means consumers must process records within 24 hours or they're lost
> - For longer retention, pipe to Kinesis Data Streams, S3, or another durable store
>
> **Concurrency limit:**
> - At most **2 simultaneous readers per shard** for single-region tables
> - For global tables, recommended limit is **1 reader per shard** to avoid throttling
>
> **Common use cases:**
> 1. **Lambda triggers**: Automatically invoke a Lambda function on every table change (e.g., send notification when an order is placed)
> 2. **Cross-region replication**: Global Tables internally use Streams to replicate changes between regions
> 3. **Materialized views**: Maintain denormalized copies of data for different access patterns
> 4. **Audit logging**: Stream all changes to S3 for compliance
> 5. **Event-driven architectures**: Table changes as events in an event-driven system"

**Interviewer:**
Good. How does Streams relate to Global Tables?

**Candidate:**

> "Global Tables are built ON TOP of DynamoDB Streams. When you create a global table with replicas in us-east-1 and eu-west-1:
>
> 1. Each region's replica has DynamoDB Streams enabled
> 2. A replication service reads the stream in each region
> 3. It applies changes from one region's stream to the other region's table
> 4. This is **asynchronous** — changes propagate within typically 0.5-2.5 seconds (per AWS docs for the `ReplicationLatency` CloudWatch metric)
>
> This leads directly to our next deep dive — Global Tables."

---

### L5 vs L6 vs L7 — Phase 10 Rubric

| Aspect | L5 (SDE-2) | L6 (SDE-3) | L7 (Principal) |
|---|---|---|---|
| **Streams architecture** | "It's like a changelog" | Explains shard model, ordering guarantees (per-PK ordering), 24-hour retention, 2-reader limit | Discusses how stream shards map to table partitions, shard splitting during partition splits, exactly-once vs at-least-once semantics |
| **Use cases** | "Trigger Lambda" | Lists 5+ use cases with concrete examples, knows Global Tables uses Streams internally | Discusses backpressure handling, consumer checkpointing, DLQ for failed Lambda invocations |
| **Limitations** | Doesn't know | Knows 24-hour retention, 2 reader limit, no guaranteed cross-PK ordering | Discusses shard iterator expiration, read throughput limits, Streams vs Kinesis Data Streams for DynamoDB (PITR alternative) |

---

## PHASE 11: Deep Dive — Global Tables (~5 min)

**Interviewer:**
Walk me through Global Tables. How does multi-region work, and how are conflicts resolved?

**Candidate:**

> "DynamoDB Global Tables provides **multi-region, multi-active** replication:
>
> ```
> ┌─────────────────┐              ┌─────────────────┐
> │  us-east-1      │   async      │  eu-west-1      │
> │                 │◄────────────▶│                 │
> │  Replica Table  │  replication │  Replica Table  │
> │  (read + write) │              │  (read + write) │
> │                 │              │                 │
> │  DDB Streams ───┼──────────────┼──▶ Apply writes │
> │  Apply writes ◀─┼──────────────┼── DDB Streams   │
> └─────────────────┘              └─────────────────┘
>
>         ▲                                 ▲
>         │                                 │
>     US users write                    EU users write
>     and read locally                  and read locally
> ```
>
> **Key characteristics:**
>
> 1. **Multi-active (not active-passive)**: Any replica can accept reads AND writes. Users are routed to the nearest region for low-latency access.
>
> 2. **Asynchronous replication**: Changes are propagated via DynamoDB Streams. Expected replication latency: **0.5-2.5 seconds** between geographically proximate regions (monitored via `ReplicationLatency` CloudWatch metric).
>
> 3. **Last-writer-wins conflict resolution**: If the same item is updated concurrently in two regions, DynamoDB uses the `aws:rep:updatetime` system attribute to determine which write wins. The write with the later timestamp overwrites the other.
>
>    ```
>    T=0: us-east-1: UpdateItem(user1, {name: 'Alice'})  → timestamp = 1000
>    T=0: eu-west-1: UpdateItem(user1, {name: 'Bob'})    → timestamp = 1001
>
>    After replication converges:
>    Both regions: user1 = {name: 'Bob'}  (timestamp 1001 wins)
>    ```
>
> 4. **Consistency limitations:**
>    - Reads in one region may not reflect recent writes in another region (eventual cross-region consistency)
>    - **Strongly consistent reads only see locally committed writes** — they don't see writes from other regions that haven't replicated yet
>    - Transactions are NOT cross-region — ACID guarantees apply only within the region where the transaction is issued
>
> 5. **Two consistency modes** (as of Global Tables version 2019.11.21):
>    - **Multi-Region Eventual Consistency (MREC)** — default. Last-writer-wins for conflicts.
>    - **Multi-Region Strong Consistency (MRSC)** — newer mode. Provides strongly consistent reads that reflect all globally committed writes. Only available for same-account configurations. [This is a significant addition — it avoids last-writer-wins at the cost of higher latency]
>
> **Conflict avoidance strategies (from AWS docs):**
> - Route all writes for a given key to a single region (use IAM policies or application routing)
> - Use idempotent writes (e.g., `SET status = 'SHIPPED'` instead of `ADD counter 1`)
> - Avoid non-idempotent operations like `ADD` and `DELETE` from sets, which can produce unexpected results under concurrent writes
>
> **Resilience:**
> - If a region becomes isolated, the other replicas continue serving traffic
> - When the isolated region recovers, pending writes are automatically replicated
> - All previously successful writes are guaranteed to eventually propagate"

**Interviewer:**
What's the difference between Global Tables and just setting up cross-region replication yourself?

**Candidate:**

> "Global Tables is a managed service that handles all the complexity:
>
> - **Automatic stream consumption** — you don't need to write Lambda functions or Kinesis consumers
> - **Conflict resolution** — built-in last-writer-wins (or MRSC for strong consistency)
> - **Capacity synchronization** — in version 2019.11.21, write capacity is automatically synced across replicas (if using auto-scaling or on-demand)
> - **Schema synchronization** — GSI changes, table settings propagate automatically
> - **Operational monitoring** — `ReplicationLatency` metric, CloudWatch alarms
>
> Building this yourself with Streams + Lambda is possible but brittle — you'd need to handle replay, idempotency, ordering, error handling, and monitoring. Global Tables abstracts all of that."

---

### L5 vs L6 vs L7 — Phase 11 Rubric

| Aspect | L5 (SDE-2) | L6 (SDE-3) | L7 (Principal) |
|---|---|---|---|
| **Global Tables architecture** | "Replicate data across regions" | Explains Streams-based async replication, last-writer-wins, 0.5-2.5s latency, MREC vs MRSC modes | Discusses cross-region transaction limitations, MRSC latency cost, multi-account Global Tables for governance |
| **Conflict resolution** | Doesn't know the mechanism | Explains last-writer-wins with timestamps, gives concrete conflict example, lists avoidance strategies | Discusses why LWW works for most use cases but fails for counters/sets, proposes CRDT-like alternatives |
| **Resilience** | "Failover to another region" | Explains how isolated regions auto-recover, pending writes guaranteed to propagate | Discusses RPO (near-zero with MREC, zero with MRSC), RTO, DNS-based failover, Route 53 health checks |

---

## PHASE 12: Deep Dive — Transactions (~5 min)

**Interviewer:**
Tell me about DynamoDB transactions. How do they achieve ACID across multiple items?

**Candidate:**

> "DynamoDB supports transactions via `TransactWriteItems` and `TransactGetItems`. These provide ACID guarantees across up to 100 items, potentially across multiple tables:
>
> **Transaction limits (verified from AWS docs):**
> - Maximum 100 actions per transaction
> - Maximum 4 MB aggregate size
> - All items must be in the same AWS Region
> - Cannot target the same item with multiple operations in one transaction
>
> **Cost:** Transactions consume **2x the normal RCU/WCU** because of the two-phase protocol — one unit to prepare, one to commit.
>
> **Internal mechanism (two-phase protocol):**
>
> ```
> TransactWriteItems([
>   Put(Table1, {pk: 'a', sk: '1', amount: 100}),
>   Update(Table2, {pk: 'b', sk: '2'}, SET balance = balance - 100),
>   ConditionCheck(Table1, {pk: 'c', sk: '3'}, attribute_exists(active))
> ])
>
> Phase 1 — PREPARE:
>   For each item in the transaction:
>     1. Acquire a lock on the item (at the partition level)
>     2. Validate condition expressions
>     3. If any condition fails → abort entire transaction
>     4. Write prepare record to WAL
>
> Phase 2 — COMMIT:
>   For each item:
>     1. Apply the mutation
>     2. Release the lock
>     3. Write commit record to WAL
>
> If coordinator crashes between prepare and commit:
>   → Recovery process checks prepare records and either commits or rolls back
> ```
>
> [INFERRED — the exact internal protocol details (locking mechanism, coordinator design) are not fully public. AWS describes it as a two-phase protocol in documentation and re:Invent talks.]
>
> **Isolation levels (from AWS docs):**
>
> | Operation Pair | Isolation Level |
> |---|---|
> | Transaction ↔ `GetItem`, `PutItem`, `UpdateItem`, `DeleteItem` | **Serializable** |
> | Transaction ↔ `BatchGetItem`, `Query`, `Scan` | **Read-committed** |
> | Transaction ↔ Transaction | **Serializable** |
>
> This means:
> - A `GetItem` during an in-flight transaction will either see the pre-transaction state or the post-transaction state, never a partial state
> - A `Query` or `Scan` during a transaction may see some items in the pre-state and others in the post-state (read-committed, not serializable)
>
> **Conflict handling:**
> - If two transactions try to modify the same item, one will fail with `TransactionCanceledException`
> - If a non-transactional write conflicts with a transaction on the same item, the non-transactional write fails with `TransactionConflictException`
> - The `TransactionConflict` CloudWatch metric tracks these conflicts
> - SDKs do NOT automatically retry transaction conflicts — the application must handle retries
>
> **Idempotency:**
> - `TransactWriteItems` supports a `ClientRequestToken` (valid for 10 minutes)
> - Retrying with the same token is idempotent — won't double-apply the transaction
>
> **When to use transactions vs conditional writes:**
> - **Single item**: Use `ConditionExpression` on `PutItem`/`UpdateItem` — cheaper (1 WCU, not 2)
> - **Multiple items, same table**: Use `TransactWriteItems`
> - **Multiple items, cross-table**: Use `TransactWriteItems` (only option)
> - **Bulk writes without atomicity**: Use `BatchWriteItem` (25 items max, no conditions, no atomicity)"

---

### L5 vs L6 vs L7 — Phase 12 Rubric

| Aspect | L5 (SDE-2) | L6 (SDE-3) | L7 (Principal) |
|---|---|---|---|
| **Transaction mechanism** | "It's like a database transaction" | Explains two-phase protocol, 100-item limit, 2x WCU cost, serializable isolation for single-item ops | Discusses coordinator failure recovery, transaction vs optimistic concurrency (conditional writes), when NOT to use transactions |
| **Isolation levels** | Doesn't know specifics | Distinguishes serializable (GetItem + Tx) from read-committed (Query/Scan + Tx) | Discusses why Query/Scan are read-committed (scanning across partitions can't be serializable without global locks) |
| **Conflict handling** | "Retry on failure" | Explains TransactionCanceledException vs TransactionConflictException, SDKs don't auto-retry, idempotency tokens | Discusses conflict rate monitoring, transaction design to minimize conflicts, partition-level locking implications |

---

## PHASE 13: Deep Dive — DAX (DynamoDB Accelerator) (~3 min)

**Interviewer:**
Let's briefly cover DAX. When and why would you use it?

**Candidate:**

> "DAX is an **in-memory caching layer** that sits in front of DynamoDB, API-compatible, reducing read latency from single-digit **milliseconds** to single-digit **microseconds**:
>
> ```
> Without DAX:
>   App → DynamoDB → SSD-based B-tree lookup → ~5ms
>
> With DAX:
>   App → DAX Cluster → In-memory cache → ~50-200 microseconds (cache hit)
>                     → DynamoDB (cache miss) → ~5ms + cache population
> ```
>
> **Architecture:**
> - DAX runs as a cluster with a primary node and read replicas
> - Multi-AZ for availability
> - **Write-through**: Writes go through DAX to DynamoDB. DAX caches the written item.
> - **Read-through**: Reads check DAX first. On cache miss, DAX fetches from DynamoDB and caches the result.
>
> **Important limitations:**
> - DAX provides **eventually consistent data only** — not suitable for strongly consistent reads
> - `TransactGetItems` pass through DAX without caching (same as strongly consistent reads)
> - Must be deployed in a VPC (EC2 access only, not directly from Lambda outside VPC)
> - Attribute names used as top-level keys are cached indefinitely — using timestamps or UUIDs as attribute names (not values) can cause memory exhaustion
>
> **When to use DAX:**
> - Read-heavy workloads with repeated access to the same items (high cache hit rate needed, >90%)
> - Hot key scenarios (e.g., a viral product page)
> - Large-scale reads that would otherwise consume massive RCU
> - Applications where microsecond latency matters (real-time bidding, gaming leaderboards)
>
> **When NOT to use DAX:**
> - Write-heavy workloads (DAX adds overhead on writes for cache population)
> - Applications requiring strongly consistent reads
> - Workloads with low cache hit rates (random access across millions of keys)
> - When DynamoDB's native millisecond latency is sufficient"

---

## PHASE 14: Deep Dive — Auto-Scaling: On-Demand vs Provisioned (~3 min)

**Interviewer:**
One more topic — capacity management. How does on-demand mode actually work under the hood?

**Candidate:**

> "Let me compare both modes and then explain the internals:
>
> **Provisioned Mode:**
> - You specify RCU and WCU for the table (and each GSI separately)
> - DynamoDB allocates partitions to meet that throughput
> - You can enable auto-scaling: set target utilization (e.g., 70%), and DynamoDB adjusts provisioned capacity up/down based on CloudWatch metrics
> - Billed hourly for provisioned capacity whether you use it or not
> - Best for: predictable workloads with steady traffic
>
> **On-Demand Mode:**
> - No capacity planning. DynamoDB automatically allocates throughput based on traffic
> - Tables instantly accommodate up to double the previous peak traffic
> - If traffic exceeds double the previous peak, DynamoDB allocates more capacity (with brief throttling possible during ramp-up)
> - Pay-per-request: billed only for actual reads/writes consumed
> - Default and recommended for most workloads
> - Best for: unpredictable traffic, new tables, spiky workloads
>
> **How on-demand works internally:**
>
> [INFERRED — the following is based on observed behavior and AWS descriptions, not a public architecture paper]
>
> ```
> On-Demand Scaling:
>
> 1. DynamoDB monitors actual traffic per partition
> 2. Maintains 'headroom' — provisions more capacity than current traffic
> 3. If traffic doubles, existing headroom absorbs the spike
> 4. As traffic grows, DynamoDB splits partitions and adds capacity
> 5. Scales down by merging underutilized partitions (slowly)
>
> Key behavior: "instant" scaling up to 2x previous peak
>   Previous peak: 10,000 WCU
>   Traffic spike: 20,000 WCU → served without throttling
>   Traffic spike: 30,000 WCU → may throttle briefly while scaling
> ```
>
> **Cost comparison:**
>
> | Metric | On-Demand | Provisioned |
> |---|---|---|
> | Write cost | ~$1.25 per million WRU | ~$0.00065 per WCU-hour |
> | Read cost | ~$0.25 per million RRU | ~$0.00013 per RCU-hour |
> | Breakeven | Roughly at ~15-20% sustained utilization | Cheaper above ~15-20% sustained utilization |
> | Optimization | None needed | Auto-scaling + reserved capacity for further savings |
>
> [UNVERIFIED — exact pricing varies by region; check aws.amazon.com/dynamodb/pricing for current numbers]
>
> **Provisioned mode throughput decrease limits (from AWS docs):**
> - First hour: up to 4 decreases
> - Subsequent hours: 1 decrease per hour
> - Maximum 27 decreases per day
>
> This is why auto-scaling can't react instantly to traffic drops — there's a limit on how fast you can scale down."

---

### L5 vs L6 vs L7 — Phase 13/14 Rubric

| Aspect | L5 (SDE-2) | L6 (SDE-3) | L7 (Principal) |
|---|---|---|---|
| **DAX** | "It's a cache in front of DynamoDB" | Explains write-through/read-through, microsecond latency, EC-only limitation, when to use vs not | Discusses cache invalidation semantics, DAX + Global Tables interaction (stale data), attribute name memory issue |
| **Capacity modes** | "On-demand auto-scales" | Explains 2x-previous-peak behavior, cost breakeven (~15-20% utilization), provisioned decrease limits | Models total cost of ownership: on-demand vs provisioned + auto-scaling + reserved capacity, discusses cost optimization strategy |
| **Operational** | "Monitor CloudWatch" | Identifies key metrics: ConsumedReadCapacityUnits, ThrottledRequests, ReplicationLatency | Designs alarm strategy, capacity planning for anticipated events (Black Friday pre-warming), discusses table class optimization (Standard vs Standard-IA) |

---

## PHASE 15: Wrap-Up & Summary (~3 min)

**Interviewer:**
We're running low on time. Summarize your design and tell me what keeps you up at night operating this system.

**Candidate:**

> "**Design evolution — how we got here:**
>
> | Phase | Component | Started With | Evolved To | Why |
> |---|---|---|---|---|
> | 0→1 | Persistence | In-memory hash map | WAL + B-tree on SSD | Durability: survive crashes |
> | 1→2 | Availability | Single server | 3 replicas across AZs, Paxos leader election | Survive machine/AZ failure |
> | 2→3 | Scalability | Single partition | Hash-based partitioning, auto-split at 10 GB or throughput limit | Scale beyond one machine |
> | 3→ | Routing | None | Request router with cached partition map | O(1) key-to-partition routing |
> | — | Consistency | N/A | Leader-based: strong reads from leader (1 RCU), eventual reads from any replica (0.5 RCU) | Tunable consistency per request |
> | — | Secondary access | N/A | GSI (async, separate table) + LSI (co-located, same partition) | Query by non-key attributes |
> | — | Change capture | N/A | DynamoDB Streams: ordered per-PK, 24h retention, shard model | CDC for replication, triggers, audit |
> | — | Multi-region | N/A | Global Tables: async replication via Streams, last-writer-wins | Low-latency global access |
> | — | Transactions | N/A | TransactWriteItems/GetItems: 2-phase, 100 items, 2x WCU, serializable isolation | Multi-item ACID within region |
> | — | Caching | N/A | DAX: in-memory, microsecond reads, write-through, EC only | Hot-key relief, ultra-low latency |
>
> **Final Architecture:**
>
> ```
>                              ┌──────────────────────┐
>                              │       Clients         │
>                              └──────────┬───────────┘
>                                         │
>                              ┌──────────▼───────────┐
>                              │   Request Router      │
>                              │   (Auth, Routing,     │
>                              │    Rate Limiting)     │
>                              └──────────┬───────────┘
>                                         │
>                   ┌─────────────────────┼─────────────────────┐
>                   │                     │                     │
>        ┌──────────▼──────┐   ┌─────────▼────────┐   ┌───────▼──────────┐
>        │   Partition      │   │  Storage Nodes    │   │   DAX Cluster    │
>        │   Metadata       │   │  (per partition)  │   │   (optional)     │
>        │   System         │   │                   │   │                  │
>        │                  │   │  ┌──────────────┐ │   │  In-memory cache │
>        │  hash(pk) →      │   │  │ Leader(AZ-a) │ │   │  Microsecond     │
>        │  partition ID    │   │  │ + 2 followers │ │   │  reads           │
>        │                  │   │  │ (AZ-b, AZ-c) │ │   │                  │
>        │  Split/merge     │   │  │              │ │   │  Write-through   │
>        │  decisions       │   │  │ Paxos group  │ │   │  Read-through    │
>        │                  │   │  │ WAL + B-tree │ │   │                  │
>        └──────────────────┘   │  └──────────────┘ │   └──────────────────┘
>                               │                   │
>                               │  ┌──────────────┐ │
>                               │  │ GSI Partition │ │
>                               │  │ (separate     │ │
>                               │  │  Paxos group) │ │
>                               │  └──────────────┘ │
>                               │                   │
>                               │  DDB Streams ─────┼──▶ Lambda / Kinesis
>                               │                   │
>                               └───────────────────┘
>
>           Global Tables: Streams-based async replication ←─────▶ Other Regions
> ```
>
> **What keeps me up at night:**
>
> 1. **Hot partition / hot key thundering herd** — A single viral item (e.g., a flash sale product) can generate millions of reads/sec to one partition key. Adaptive capacity and burst help, but a true single-key hotspot exceeding 3,000 RCU / 1,000 WCU per partition cannot be solved by DynamoDB alone. It requires DAX, application-level write sharding, or both. Monitoring `ThrottledRequests` per table and per GSI is critical.
>
> 2. **Partition metadata system availability** — The partition metadata system is the brain. If it fails, request routers can't discover partitions, splits can't happen, leader elections may stall. It must be more available than DynamoDB itself — likely using its own Paxos-replicated state across AZs.
>
> 3. **GSI write throttling cascading to base table** — If a GSI's write capacity is undersized, it throttles, which cascades to throttle base table writes. Customers often forget to provision GSI capacity proportional to the base table. This is one of the most common operational surprises. Monitoring `WriteThrottleEvents` on GSIs is essential.
>
> 4. **Global Tables conflict resolution** — Last-writer-wins works for most cases but silently drops writes for concurrent updates. For counters, sets, or any non-idempotent operation, this can cause data loss. Educating customers to use idempotent writes and routing writes for a given key to a single region is an ongoing operational challenge.
>
> 5. **Partition split during sustained high traffic** — Splits take time. During the split, the old partition is still receiving traffic at the rate that triggered the split. If traffic is growing faster than splits can complete, there's a brief period of elevated throttling. The system should pre-split based on traffic trends, not just react to threshold breaches.
>
> 6. **10 GB LSI partition key limit** — Tables created with LSIs can hit the `ItemCollectionSizeLimitExceededException` without warning, blocking all writes for that partition key. This is a design-time decision that's hard to fix later (LSIs can't be removed without recreating the table). Monitoring item collection sizes proactively is critical.
>
> **Potential extensions:**
> - **Point-in-Time Recovery (PITR)** — Continuous backups, restore to any second in the last 35 days
> - **TTL (Time to Live)** — Automatic item expiration without consuming WCU
> - **PartiQL** — SQL-compatible query language on top of DynamoDB
> - **S3 Export** — Export table data to S3 for analytics without consuming RCU
> - **Zero-ETL integration** — Direct integration with Redshift, OpenSearch for analytics
> - **Resource-based policies** — Cross-account access without IAM role assumption"

---

## Interviewer's Final Assessment

**Hire Recommendation: Strong Hire (L6 — solid SDE-3 with depth in distributed systems)**

| Dimension | Rating | Notes |
|---|---|---|
| **Problem Decomposition** | Exceeds Bar | Clean iterative build-up from single server to full architecture. Correctly separated request routing, partition metadata, and storage. |
| **Requirements & Scoping** | Exceeds Bar | Distinguished partition key + sort key model, two consistency modes, RCU/WCU capacity model. Correctly distinguished DynamoDB from the Dynamo paper. |
| **Partitioning Design** | Exceeds Bar | Hash-based partitioning with auto-split triggers. Explained adaptive capacity, burst capacity, and the hot key problem with mitigation strategies. |
| **Replication & Consistency** | Exceeds Bar | Paxos-based leader-per-partition. Clearly traced both read paths. Explained 2x RCU cost for strong consistency. Knew GSI limitation. |
| **Storage Engine** | Meets Bar | Explained B-tree vs LSM-tree tradeoff. WAL + B-tree for predictable latency. |
| **Secondary Indexes** | Exceeds Bar | GSI as separate table with async replication. LSI co-located with 10 GB limit. Cost awareness (N+1 WCU). Practical guidance on when to use each. |
| **DynamoDB Streams** | Meets Bar | Ordering guarantees, shard model, 24-hour retention, use cases including Global Tables. |
| **Global Tables** | Exceeds Bar | Streams-based async replication, last-writer-wins, MREC vs MRSC modes, conflict avoidance strategies. |
| **Transactions** | Exceeds Bar | Two-phase protocol, 100-item limit, 2x cost, serializable vs read-committed isolation per operation type. |
| **Operational Maturity** | Exceeds Bar | "What keeps you up at night" covered hot keys, GSI throttle cascading, partition metadata availability, LWW conflicts, LSI limits. |
| **Communication** | Exceeds Bar | Structured with diagrams and tables. Drove the conversation with iterative build-up. Used concrete numbers and tradeoff tables. |
| **LP: Dive Deep** | Exceeds Bar | Went deep on Paxos replication, consistency read paths, GSI async replication rationale, and transaction isolation levels. |
| **LP: Think Big** | Meets Bar | Extensions section showed awareness of broader DynamoDB ecosystem (PITR, TTL, PartiQL, zero-ETL). |

**What would push this to L7:**
- Deeper discussion of partition metadata system design (its own replication, availability guarantees)
- Proposing a monitoring/observability architecture (dashboards, alarms, runbooks for common DynamoDB failures)
- Discussing the operational process of performing a partition split without client-visible impact
- Multi-tenant isolation: how DynamoDB prevents one customer's workload from affecting another's latency (resource isolation, admission control, shuffle sharding)
- Cost modeling at scale: $/TB/month for different workload patterns, reserved capacity optimization
- Discussing DynamoDB's evolution from the Dynamo paper: what was kept, what was changed, and why (engineering context)

---

## Key Differences: L5 vs L6 vs L7 Expectations for This Problem

| Aspect | L5 (SDE-2) | L6 (SDE-3) | L7 (Principal) |
|---|---|---|---|
| **Requirements** | Lists CRUD operations, knows primary key | Quantifies RCU/WCU, explains partition key + sort key model, distinguishes DynamoDB from Dynamo paper, knows item size limit (400 KB) | Frames requirements around customer access patterns, discusses single-table design, scopes features by interview time |
| **Architecture** | Correct hash-partition + replication design | Request router → partition metadata → storage nodes, Paxos leader-per-partition, iterative build-up | Discusses partition metadata availability, multi-tenant isolation, cell-based architecture for blast radius |
| **Partitioning** | "Hash the partition key, split when full" | Explains 10 GB / throughput split triggers, adaptive capacity, burst (300 sec), per-partition limits (3000/1000) | Discusses split mechanics during live traffic, pre-splitting for anticipated load, partition map consistency |
| **Replication** | "3 copies across AZs" | Multi-Paxos: leader election, log replication, majority commit, follower catch-up, WAL + B-tree | Discusses leader lease, split-brain avoidance, gray failure detection, read availability during leader election |
| **Consistency** | "Eventual or strong" | Traces both read paths, explains 0.5 vs 1 RCU cost, GSI EC-only limitation, leader protection rationale | Discusses linearizability, read-committed isolation for Query/Scan, application-level consistency patterns |
| **GSI/LSI** | "Indexes for different access patterns" | GSI as separate async table, LSI co-located with 10 GB limit, write amplification (N+1 WCU), sparse indexes | Discusses GSI backfill, LSI design-time tradeoff, single-table design to minimize indexes, GSI overloading |
| **Streams** | "Changelog for triggers" | Shard model, per-PK ordering, 24h retention, 2-reader limit, Streams powers Global Tables | Discusses exactly-once semantics, consumer checkpointing, shard splitting during partition splits |
| **Global Tables** | "Multi-region replication" | Streams-based async, LWW conflicts, MREC vs MRSC, 0.5-2.5s replication latency, conflict avoidance | Discusses cross-region transaction limitations, RPO/RTO analysis, CRDT alternatives to LWW |
| **Transactions** | "Multi-item ACID" | Two-phase protocol, 100-item / 4 MB limit, 2x WCU, serializable vs read-committed per op, idempotency token | Discusses coordinator failure recovery, transaction design for low conflict, cross-partition locking implications |
| **Operational** | "Monitor with CloudWatch" | Identifies specific failure modes (hot keys, GSI throttle cascade, partition metadata failure, LWW data loss) | Proposes alarm strategy, runbooks, capacity planning for events, game days for partition failure testing |
| **Communication** | Answers questions | Drives conversation, uses diagrams and tables, iterative naive→refined progression | Negotiates scope, manages time, proposes phased deep dives based on interview priorities |

---

## Appendix: Key Numbers Reference (Verified from AWS Docs)

| Property | Value | Source |
|---|---|---|
| Item size limit | 400 KB | AWS docs (WorkingWithItems) |
| Partition key value max length | 2,048 bytes | AWS docs (Naming) |
| Sort key value max length | 1,024 bytes | AWS docs (Naming) |
| Nested attribute depth | 32 levels | AWS docs (CoreComponents) |
| GSIs per table | 20 (default, adjustable) | AWS docs (ServiceQuotas) |
| LSIs per table | 5 | AWS docs (ServiceQuotas) |
| LSI item collection size limit | 10 GB per partition key value | AWS docs (LSI) |
| Per-partition read throughput | 3,000 RCU | AWS docs (bp-partition-key-design) |
| Per-partition write throughput | 1,000 WCU | AWS docs (bp-partition-key-design) |
| Per-partition data size | 10 GB | AWS docs (inferred from split behavior) |
| Per-table throughput (on-demand) | 40,000 RRU / 40,000 WRU | AWS docs (ServiceQuotas) |
| Per-table throughput (provisioned) | 40,000 RCU / 40,000 WCU | AWS docs (ServiceQuotas) |
| Per-account throughput (provisioned) | 80,000 RCU / 80,000 WCU | AWS docs (ServiceQuotas) |
| Tables per account per region | 2,500 (up to 10,000) | AWS docs (ServiceQuotas) |
| BatchGetItem max items | 100 | AWS docs (WorkingWithItems) |
| BatchGetItem max data | 16 MB | AWS docs (WorkingWithItems) |
| BatchWriteItem max items | 25 | AWS docs (WorkingWithItems) |
| BatchWriteItem max data | 16 MB | AWS docs (WorkingWithItems) |
| TransactWriteItems max items | 100 | AWS docs (transaction-apis) |
| TransactGetItems max items | 100 | AWS docs (transaction-apis) |
| Transaction aggregate size | 4 MB | AWS docs (transaction-apis) |
| Transaction WCU cost | 2x normal (prepare + commit) | AWS docs (transaction-apis) |
| Transaction idempotency token validity | 10 minutes | AWS docs (transaction-apis) |
| DynamoDB Streams retention | 24 hours | AWS docs (Streams) |
| Streams readers per shard | 2 (single-region), 1 recommended (global) | AWS docs (ServiceQuotas) |
| Burst capacity reserved | 5 minutes (300 seconds) of unused throughput | AWS docs (burst-adaptive-capacity) |
| Global Tables replication latency | 0.5-2.5 seconds typical | AWS docs (GlobalTables) |
| Global Tables max per account (MRSC) | 400 | AWS docs (ServiceQuotas) |
| RCU: strongly consistent read size | 4 KB per RCU | AWS docs (bp-partition-key-design) |
| RCU: eventually consistent read size | 8 KB per RCU (= 4 KB at 0.5 RCU) | AWS docs (ReadConsistency) |
| WCU: write size | 1 KB per WCU | AWS docs (bp-partition-key-design) |
| Provisioned throughput decrease limits | 4 in first hour, then 1/hour, max 27/day | AWS docs (ServiceQuotas) |
| Projected attributes across all indexes | 100 (for INCLUDE projection type) | AWS docs (ServiceQuotas) |
| DAX latency | Microseconds (vs milliseconds for DynamoDB) | AWS docs (DAX) |
| DAX consistency | Eventually consistent only | AWS docs (DAX) |

---

*For detailed deep dives on each component, see the companion documents:*
- [Partitioning Model](partitioning-model.md) — Hash-based partitioning, auto-split, adaptive capacity
- [Storage & Replication](storage-and-replication.md) — B-tree, WAL, Multi-Paxos, leader election
- [Consistency Model](consistency-model.md) — Eventually vs strongly consistent reads, read-committed isolation
- [Secondary Indexes](secondary-indexes.md) — GSI async replication, LSI co-location, sparse indexes
- [DynamoDB Streams](dynamodb-streams.md) — Shard model, ordering, Lambda triggers
- [Global Tables](global-tables.md) — Multi-region replication, last-writer-wins, MREC vs MRSC
- [Transactions](transactions.md) — Two-phase protocol, isolation levels, conflict handling
- [Capacity & Scaling](capacity-and-scaling.md) — On-demand vs provisioned, auto-scaling, DAX

*End of interview simulation.*
