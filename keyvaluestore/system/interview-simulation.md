# System Design Interview Simulation: Design a Distributed Key-Value Store

> **Interviewer:** Principal Engineer (L8), Amazon  
> **Candidate Level:** SDE-3 (L6 — Senior Software Development Engineer)  
> **Duration:** ~60 minutes  
> **Date:** July 2, 2026

---

## PHASE 1: Opening & Problem Statement (~2 min)

**Interviewer:**  
Hey, welcome. I'm [name], Principal Engineer on the distributed systems team. For today's system design round, I'd like you to design a **distributed key-value store** — think something like DynamoDB, Redis Cluster, or Cassandra at a conceptual level. A system where clients can store and retrieve data by key, and it works across multiple machines.

I care about how you break down the problem, what tradeoffs you make, and how deep you can go on the distributed systems fundamentals. I'll be pushing on your decisions — that's not a negative signal, that's me calibrating depth.

Take it away.

---

## PHASE 2: Requirements Gathering & Scoping (~5 min)

**Candidate:**  
Thanks! Before I start drawing boxes, I want to align on scope. "Distributed key-value store" can mean a lot of things — from an in-memory cache like Redis to a persistent storage engine like DynamoDB. Let me ask some clarifying questions.

**Functional Requirements — what operations do we need?**

> "The core operations are obvious: `PUT(key, value)`, `GET(key)`, and `DELETE(key)`. But I want to clarify a few things:
> - **Do we need range queries?** Like `GET all keys between A and Z`? Or is it purely point lookups by exact key?"

**Interviewer**: "Good question. Let's focus on point lookups — exact key access. No range queries for now."

> "- **Do we need TTL / automatic expiry?** Like 'store this key for 24 hours then delete it'?"

**Interviewer**: "Yes, let's support optional TTL on keys."

> "- **What about conditional writes?** Like compare-and-swap — 'update this key only if the current value matches X'?"

**Interviewer**: "Mention it but don't deep dive. Focus on the core read/write path."

> "- **Value size constraints?** Are values small (< 1KB) like config data, or large (multi-MB) like blobs?"

**Interviewer**: "Let's say values are up to **1MB** max, but the common case is **1-10KB**. Think metadata, session data, user preferences — not media files."

> "- **Data model**: Is it flat key → value? Or do we support nested structures, columns, etc.?"

**Interviewer**: "Flat key → value. Keep it simple."

**Non-Functional Requirements:**

> "Now the critical part — the *distributed* requirements:
>
> | Dimension | My Proposal |
> |---|---|
> | **Availability** | High availability — the system should remain operational even when nodes fail. I'd lean toward an **AP** system (favoring availability over strict consistency), with tunable consistency. |
> | **Consistency** | **Tunable** — let the client choose per-request: strong consistency (quorum reads/writes) or eventual consistency (single-node reads for speed). |
> | **Partition Tolerance** | Mandatory — network partitions will happen in a distributed system. |
> | **Durability** | Data must be persisted to disk — not a pure in-memory cache. Writes are durable once acknowledged. |
> | **Latency** | Single-digit millisecond reads (p99 < 10ms), writes under 20ms p99. |
> | **Scalability** | Horizontally scalable — add more nodes to handle more data and traffic. Linear (or near-linear) scaling. |

**Interviewer:**  
I like that you're proposing tunable consistency — that's a mature choice. Walk me through why AP over CP?

**Candidate:**  
> "For most key-value use cases at Amazon's scale — session stores, shopping cart data, user preferences, feature flags — availability is more important than strict consistency. If a user adds an item to their cart and we have a brief inconsistency window, that's tolerable. But if the cart service is *down*, that's lost revenue.
>
> The famous Amazon Dynamo paper (2007) made exactly this tradeoff — they built an AP system with eventual consistency because they found that '**customers should be able to add items to their cart even if disks are failing, network routes are flapping, or data centers are being destroyed by tornados**.'
>
> But I don't want to make it a rigid choice — that's why tunable consistency. For use cases like distributed locks or leader election, clients can opt into stronger consistency (quorum reads + writes). For high-throughput caching, they can opt into eventual consistency."

**Interviewer:**  
Good reasoning. Let's get some numbers.

---

## PHASE 3: Scale Estimation (~3 min)

**Candidate:**

> "Let me estimate scale for an Amazon-grade key-value store."

#### Traffic Estimates

> "Assumptions:
> - **Total keys stored**: 10 billion (10^10) — large but realistic for a service backing multiple teams
> - **Average value size**: 5 KB
> - **Read throughput**: 1 million reads/sec (heavily read-skewed)
> - **Write throughput**: 100,000 writes/sec
> - **Read:Write ratio**: ~10:1
> - **Peak multiplier**: 3x → 3M reads/sec, 300K writes/sec at peak"

#### Storage Estimates

> "Per key-value pair:
> - Key: ~50 bytes average (string keys like `user:12345:session`)
> - Value: ~5 KB average
> - Metadata (timestamp, TTL, version vector): ~100 bytes
> - **Total per entry**: ~5.15 KB → round to **5 KB**
>
> Total storage:
> - 10 billion × 5 KB = **50 TB** of raw data
> - With **3x replication**: 150 TB across the cluster
> - Each node holds ~2 TB → we need **~75 nodes** for storage alone
> - With headroom for compaction and growth: **~100 nodes**"

#### Bandwidth

> "Read bandwidth: 1M reads/sec × 5 KB = **5 GB/sec** outbound
> Write bandwidth: 100K writes/sec × 5 KB = **500 MB/sec** inbound (per replica; with 3 replicas, total internal bandwidth is ~1.5 GB/sec)"

**Interviewer:**  
Good. Those numbers will inform your partitioning strategy. Let's get into the architecture.

---

## PHASE 4: High-Level Architecture (~5 min)

**Candidate:**

> "Let me sketch the high-level architecture. The key design choice is: **leaderless replication** — this is the Dynamo-style architecture. No single leader per partition. Any node that holds a replica can accept reads and writes."

```
                            ┌─────────────┐
                            │   Clients   │
                            │ (app servers,│
                            │  services)  │
                            └──────┬──────┘
                                   │
                            ┌──────▼──────┐
                            │ Client-Side │
                            │   Library   │
                            │ (partition- │
                            │  aware SDK) │
                            └──────┬──────┘
                                   │
              ┌────────────────────┼────────────────────┐
              │                    │                    │
       ┌──────▼──────┐     ┌──────▼──────┐     ┌──────▼──────┐
       │   Node A    │     │   Node B    │     │   Node C    │
       │ Partitions: │     │ Partitions: │     │ Partitions: │
       │ [0-3], [4-7]│     │ [4-7],[8-11]│     │ [8-11],[0-3]│
       │  primary +  │     │  primary +  │     │  primary +  │
       │  replicas   │     │  replicas   │     │  replicas   │
       └──────┬──────┘     └──────┬──────┘     └──────┬──────┘
              │                    │                    │
              │         Gossip Protocol                │
              │◄──────────────────►│◄──────────────────►│
              │   (membership,     │                    │
              │    failure detect) │                    │
              └────────────────────┴────────────────────┘
```

#### Core Components

> "1. **Storage Nodes** — Each node stores a subset of the key space (partitions) plus replicas of adjacent partitions. Every node is equal — no master/slave distinction.
>
> 2. **Partition Ring (Consistent Hash Ring)** — Determines which nodes own which keys. Uses consistent hashing with virtual nodes for even distribution.
>
> 3. **Client-Side Library / SDK** — A partition-aware client that knows the ring topology. Routes requests directly to the correct coordinator node. No centralized proxy/gateway needed for the hot path.
>
> 4. **Gossip Protocol** — Nodes communicate membership, failure detection, and ring state changes via an epidemic-style gossip protocol. No centralized coordination service on the hot path.
>
> 5. **Storage Engine (per node)** — LSM-tree based engine: writes go to an in-memory memtable + write-ahead log (WAL), then flush to sorted SSTables on disk. Reads merge memtable + SSTables via bloom filters."

**Interviewer:**  
Why leaderless? Why not a leader-per-partition model like what Raft gives you?

**Candidate:**  
> "Great question. Both are valid — let me compare:
>
> | Aspect | Leaderless (Dynamo-style) | Leader-per-Partition (Raft/Paxos) |
> |---|---|---|
> | **Write availability** | Any replica can accept writes → higher availability during partitions | Only the leader accepts writes → if leader fails, need election (brief downtime) |
> | **Consistency** | Tunable (eventual → quorum) | Strong consistency by default (linearizable) |
> | **Write conflicts** | Possible — need conflict resolution (vector clocks, LWW) | No conflicts — leader serializes all writes |
> | **Complexity** | Conflict resolution is complex | Leader election is complex |
> | **Latency** | Lower tail latency (any replica can serve) | Writes always go through leader (may be remote) |
>
> I'm choosing **leaderless** because:
> 1. Our requirements favor availability over strict consistency (AP system)
> 2. At 100K writes/sec, leader-based would create hot spots — the leader for a popular partition becomes a bottleneck
> 3. With tunable consistency (quorum writes), we can still get strong consistency when needed
> 4. This is proven at scale — Cassandra and Riak use this model (inspired by the Amazon Dynamo paper)
>
> If the use case were a distributed database needing strong consistency (e.g., financial transactions), I'd choose leader-per-partition with Raft."

---

### Interviewer's Internal Assessment:

✅ *Good. The candidate made a deliberate architectural choice (leaderless), justified it against the alternative, and connected it back to the requirements. The comparison table is structured and shows depth. For L6, I expect this level of reasoning.*

---

## PHASE 5: Deep Dive — Partitioning (Consistent Hashing) (~10 min)

**Interviewer:**  
Let's dig into partitioning. You mentioned consistent hashing. Walk me through it in detail — I want to understand how a key maps to a set of nodes.

**Candidate:**

> "Sure. Partitioning is how we distribute 10 billion keys across ~100 nodes so that:
> 1. Data is evenly distributed (no hot spots)
> 2. Adding/removing nodes minimizes data movement
> 3. Each key has a well-defined set of replica nodes"

### Naive Approach (and why it fails)

> "The naive approach: `node = hash(key) % N` where N = number of nodes.
>
> **Problem**: When N changes (add/remove a node), almost every key remaps to a different node. If we go from 100 to 101 nodes, ~99% of keys need to move. That's catastrophic — 50 TB of data shuffling."

### Consistent Hashing

> "Consistent hashing places both **nodes** and **keys** on a circular hash ring (0 to 2^128 - 1):
>
> ```
>                         0
>                    ╭────┼────╮
>                ╭───╯    │    ╰───╮
>            Node A       │       Node B
>               │    ┌────┘        │
>               │    │             │
>       ────────┤    │             ├────────
>               │    │             │
>               │    └────┐        │
>            Node D       │       Node C
>                ╰───╮    │    ╭───╯
>                    ╰────┼────╯
>                      2^128/2
>
>     Key K1 hashes to position X → walk clockwise → first node = Node B
>     Key K2 hashes to position Y → walk clockwise → first node = Node C
> ```
>
> - Each key is assigned to the **first node** encountered when walking clockwise from the key's hash position.
> - When a node is added, only the keys between the new node and its predecessor need to move.
> - When a node is removed, only its keys move to the next node clockwise.
> - **On average, only `K/N` keys move** (K = total keys, N = number of nodes) — versus `K × (N-1)/N` with naive hashing."

### The Problem with Basic Consistent Hashing

> "With just N physical nodes on the ring, the distribution is **uneven**. Some nodes end up responsible for a much larger arc of the ring than others. Also, when a node goes down, its entire load shifts to just one neighbor — causing a cascade."

**Interviewer**: "So how do you solve that?"

### Virtual Nodes (VNodes)

> "Instead of placing each physical node at one point on the ring, we place it at **many points** — these are called **virtual nodes (vnodes)**. Each physical node owns, say, 256 vnodes scattered around the ring.
>
> ```
>     Physical Node A → vnodes: A1, A2, A3, ..., A256
>     Physical Node B → vnodes: B1, B2, B3, ..., B256
>     Physical Node C → vnodes: C1, C2, C3, ..., C256
>
>     Ring (simplified):
>     ──A3──B1──C2──A1──B3──C1──A2──B2──C3──A1──...
>       │    │    │    │    │    │    │    │    │
>       └Keys assigned to the next vnode clockwise
> ```
>
> **Benefits:**
> 1. **Even distribution**: With 256 vnodes per node and 100 nodes = 25,600 vnodes on the ring. The arcs are small and uniform. Standard deviation of load drops dramatically.
> 2. **Graceful failure**: When Node A goes down, its 256 vnodes' load spreads across **many** other nodes (all nodes that are clockwise neighbors of A's vnodes), not just one. Load is distributed.
> 3. **Heterogeneous hardware**: A more powerful machine can be assigned more vnodes (e.g., 512 instead of 256), taking on proportionally more data.
> 4. **Incremental rebalancing**: When a new node joins, it takes vnodes from multiple existing nodes, rather than one huge chunk from a single neighbor."

**Interviewer:**  
Good. Now, how does replication work with the ring? If I write key K, which nodes store the replicas?

**Candidate:**

> "With a replication factor of N=3, the key is stored on the **first 3 *distinct physical nodes*** encountered clockwise from the key's position on the ring.
>
> ```
>     Key K hashes to position X on the ring.
>     Walk clockwise:
>       1st vnode encountered: B3 (Physical Node B) → Replica 1
>       2nd vnode encountered: B1 (Physical Node B) → SKIP (same physical node)
>       3rd vnode encountered: A2 (Physical Node A) → Replica 2
>       4th vnode encountered: C1 (Physical Node C) → Replica 3 ✓
>
>     Preference list for key K: [Node B, Node A, Node C]
> ```
>
> The key insight: we skip vnodes that belong to the same physical node. This ensures replicas are on distinct physical machines (and ideally distinct racks/availability zones for fault isolation).
>
> The ordered list of nodes responsible for a key is called the **preference list**. The first node in the list is the **coordinator** for that key."

**Interviewer:**  
What happens when a new node joins the cluster? Walk me through the rebalancing.

**Candidate:**

> "Say we have nodes A, B, C and we add Node D:
>
> 1. **Node D announces itself** via the gossip protocol. All nodes learn about D.
> 2. **D is assigned vnodes** — either randomly placed on the ring, or strategically placed to take vnodes from the most loaded nodes.
> 3. **For each vnode D takes over**, the data that was previously owned by the clockwise predecessor's range needs to be **streamed** to D.
> 4. **Streaming happens in the background** — the old owner continues serving reads and writes during the transfer. Once D has caught up, the ring state is updated atomically (via gossip consensus), and D starts serving those vnodes.
> 5. **Key movements are proportional** — D takes ~1/4 of the keyspace (if we had 3 nodes, now 4). Only ~25% of data moves, and it moves from multiple existing nodes (not just one).
>
> During rebalancing:
> - **Reads**: Continue working — the old node still has the data until handoff completes.
> - **Writes**: Go to both old and new owner during the transition window (or just the old owner, depending on the coordination strategy).
> - **No downtime**."

---

### Interviewer's Internal Assessment:

✅ *Excellent depth on consistent hashing. The candidate covered naive hashing failure, basic consistent hashing, virtual nodes with concrete numbers (256 vnodes), skipping same-physical-node in preference list, and rebalancing mechanics. This is L6+ territory — they didn't just say "use consistent hashing" but explained the machinery.*

---

## PHASE 6: Deep Dive — Replication & Consistency (~10 min)

**Interviewer:**  
Let's talk about the write and read paths in detail. You mentioned quorum — walk me through exactly what happens when a client writes a key.

**Candidate:**

### Write Path (PUT)

> "Here's the step-by-step write path:
>
> ```
> Client                 Coordinator (Node B)           Replica Nodes (A, C)
>   │                         │                              │
>   │  PUT key=K, value=V     │                              │
>   │  consistency=QUORUM     │                              │
>   │────────────────────────▶│                              │
>   │                         │                              │
>   │                         │  1. Hash(K) → position       │
>   │                         │     on ring                  │
>   │                         │  2. Preference list:         │
>   │                         │     [B, A, C]                │
>   │                         │                              │
>   │                         │  3. Write locally            │
>   │                         │     (memtable + WAL)         │
>   │                         │                              │
>   │                         │  4. Send write to            │
>   │                         │     replicas A and C         │
>   │                         │     (in parallel)            │
>   │                         │──────────────────────────────▶│
>   │                         │                              │
>   │                         │  5. Wait for W-1 ACKs        │
>   │                         │     (W=2 for QUORUM with     │
>   │                         │      N=3: need 2 total,      │
>   │                         │      already have self=1,    │
>   │                         │      need 1 more)            │
>   │                         │◀──────────────────────────────│
>   │                         │     ACK from Node A           │
>   │                         │                              │
>   │  6. ACK to client       │  (Node C's ACK arrives       │
>   │     (W=2 met)           │   later — async, still       │
>   │◀────────────────────────│   applied but not waited on) │
>   │                         │                              │
> ```
>
> **Quorum math**: With N=3 replicas, W=2 (write quorum), R=2 (read quorum):
> - **W + R > N** → 2 + 2 > 3 → **True** → guaranteed overlap between write and read sets
> - This means at least one node that participated in the write will also participate in the read → **strong consistency**
>
> For **eventual consistency**, client sets W=1, R=1 — faster but no overlap guarantee."

**Interviewer:**  
What happens if Node C is down when the write comes in? Does the write fail?

**Candidate:**

### Hinted Handoff

> "No! The write doesn't fail. This is where **hinted handoff** comes in.
>
> When Node C is unreachable:
> 1. The coordinator (B) still needs to achieve W=2. It has itself + Node A's ACK = 2. **The write succeeds** (quorum met).
> 2. For the third replica (C), the coordinator **writes a "hint"** to a temporary location — either on itself or on another healthy node (say Node D).
> 3. The hint contains: `{target: C, key: K, value: V, timestamp: T}` — basically, "when Node C comes back, deliver this write to it."
> 4. Node D (the hint holder) periodically checks if C is back online (via gossip). When C recovers, D **forwards the hinted data** to C.
> 5. Once C confirms receipt, the hint is deleted.
>
> ```
>     Normal: Write to B, A, C (all healthy)
>     
>     C is down:
>     Write to B ✅, A ✅, C ❌ → quorum met (W=2)
>     Hint stored on D: {for: C, key: K, value: V}
>     
>     C recovers:
>     D detects C is back (gossip) → D sends hint to C → C applies it
>     Full replication restored.
> ```
>
> **Key insight**: Hinted handoff ensures that temporary node failures don't cause permanent data divergence. It's a **best-effort** mechanism — for long-term divergence (e.g., node down for days), we rely on **anti-entropy repair** (Merkle trees), which I'll cover shortly."

**Interviewer:**  
Now, here's the hard part. Two clients write the same key simultaneously on different nodes. What happens?

### Conflict Resolution

**Candidate:**

> "This is the fundamental challenge of leaderless replication. Let me walk through the scenario:
>
> ```
>     Time T1: Client 1 writes K=V1 to Node A (coordinator)
>              Node A forwards to B (ACK) → quorum met
>              Node A forwards to C (slow, in flight)
>     
>     Time T2: Client 2 writes K=V2 to Node C (coordinator)
>              Node C forwards to B (ACK) → quorum met
>              Node C forwards to A (slow, in flight)
>     
>     Result:
>       Node A has: K=V1 (plus V2 arriving late)
>       Node B has: K=V1 then K=V2 (or vice versa depending on timing)
>       Node C has: K=V2 (plus V1 arriving late)
>     
>     CONFLICT! Which value wins?
> ```
>
> There are three common strategies:

#### Strategy 1: Last-Write-Wins (LWW)

> "Each write gets a timestamp. On conflict, the **highest timestamp wins**. Simple and used by Cassandra.
>
> **Pros**: Simple, no client-side logic
> **Cons**: 
> - Clock skew between nodes can cause 'earlier' writes to win over 'later' ones
> - Data loss — the 'losing' write is silently discarded
> - Not suitable when both writes are meaningful (e.g., two items added to a shopping cart)
>
> **When to use**: When data is immutable or idempotent (session tokens, cache entries)."

#### Strategy 2: Vector Clocks + Application-Level Resolution

> "Each value carries a **vector clock** — a list of `(node, counter)` pairs that tracks the causal history of the value.
>
> ```
>     Write 1 (via Node A): K=V1, clock=[(A,1)]
>     Write 2 (via Node C): K=V2, clock=[(C,1)]
>     
>     These clocks are CONCURRENT (neither dominates the other)
>     → CONFLICT DETECTED
>     
>     On next read, the client receives BOTH versions:
>       [{value: V1, clock: [(A,1)]}, {value: V2, clock: [(C,1)]}]
>     
>     The CLIENT resolves the conflict (e.g., merge shopping cart items)
>     and writes back:
>       K=V_merged, clock=[(A,1),(C,1),(B,1)]   ← merges both histories
> ```
>
> **Pros**: No data loss — conflicts are surfaced, not silently resolved
> **Cons**: Pushes complexity to the client. Vector clocks can grow large.
>
> **When to use**: When both concurrent writes are meaningful (shopping carts, collaborative editing)."

#### Strategy 3: CRDTs (Conflict-free Replicated Data Types)

> "Use mathematically designed data structures that can be merged automatically without conflicts — like G-Counters (grow-only counters), OR-Sets (observed-remove sets), etc.
>
> **Pros**: Automatic conflict resolution, no client logic
> **Cons**: Limited to specific data types, can be space-inefficient
>
> **When to use**: Counters, sets, flags — specific use cases."

> "**My recommendation for our system**: Support **LWW as the default** (simple, works for most use cases) but allow clients to opt into **vector clock mode** for keys where conflict resolution matters. This is the DynamoDB approach."

**Interviewer:**  
Good. Now walk me through the read path — especially what happens when replicas have different values.

### Read Path (GET) + Read Repair

**Candidate:**

> "Here's the read path:
>
> ```
> Client                 Coordinator (Node B)           Replica Nodes (A, C)
>   │                         │                              │
>   │  GET key=K              │                              │
>   │  consistency=QUORUM     │                              │
>   │────────────────────────▶│                              │
>   │                         │                              │
>   │                         │  1. Determine preference     │
>   │                         │     list: [B, A, C]          │
>   │                         │                              │
>   │                         │  2. Read locally (B)         │
>   │                         │     + send read to A, C      │
>   │                         │     (in parallel)            │
>   │                         │──────────────────────────────▶│
>   │                         │                              │
>   │                         │  3. Wait for R-1 responses   │
>   │                         │     (R=2, already have       │
>   │                         │      self=1, need 1 more)    │
>   │                         │                              │
>   │                         │  Node A responds:            │
>   │                         │◀──────────────────────────────│
>   │                         │  K=V2, timestamp=T2          │
>   │                         │                              │
>   │                         │  B has: K=V2, timestamp=T2   │
>   │                         │  A has: K=V2, timestamp=T2   │
>   │                         │  → Consistent! Return V2     │
>   │                         │                              │
>   │  4. Return V2           │                              │
>   │◀────────────────────────│                              │
>   │                         │                              │
>   │                         │  5. Node C responds (late):  │
>   │                         │◀──────────────────────────────│
>   │                         │  K=V1, timestamp=T1 (STALE!) │
>   │                         │                              │
>   │                         │  6. READ REPAIR:             │
>   │                         │  Send V2 (newer) to Node C   │
>   │                         │──────────────────────────────▶│
>   │                         │                              │
> ```
>
> **Read repair** is an opportunistic consistency mechanism:
> - During a quorum read, the coordinator gets values from R nodes
> - If it detects that some nodes have stale data (older timestamp or smaller vector clock), it **sends the latest value to the stale nodes**
> - This happens **asynchronously** — the client already got its response
> - Over time, this gradually heals divergence across replicas
>
> Read repair is **probabilistic** — it only fixes keys that are actually read. For keys that are written but rarely read, we need **anti-entropy repair** (Merkle trees) as a background sweep."

---

### Interviewer's Internal Assessment:

✅ *Excellent. The candidate walked through write and read paths at a mechanistic level, covered hinted handoff, explained three conflict resolution strategies with tradeoffs, and described read repair. The quorum math (W+R>N) was correctly applied. The DynamoDB reference shows they know real systems. This is strong L6 depth.*

---

## PHASE 7: Deep Dive — Storage Engine (~5 min)

**Interviewer:**  
You mentioned LSM-trees for the storage engine. Why not B-trees? Walk me through the on-disk data structures.

**Candidate:**

> "The storage engine on each node is a **Log-Structured Merge-tree (LSM-tree)**. Here's why:
>
> | Aspect | LSM-Tree | B-Tree |
> |---|---|---|
> | **Write pattern** | Sequential (append to log) → very fast | Random I/O (update in-place) → slower |
> | **Write amplification** | Higher (compaction rewrites data) | Lower per-write, but each write is a random seek |
> | **Read performance** | May need to check multiple levels (memtable + SSTables) | Single tree traversal — generally faster for point reads |
> | **Space amplification** | Multiple versions during compaction | Minimal — in-place updates |
> | **Write throughput** | ⭐ Much higher | Lower |
>
> For a key-value store with 100K writes/sec per node, **write throughput is the bottleneck**. LSM-trees optimize for writes by converting random I/O into sequential I/O.
>
> **The architecture on each node:**
>
> ```
>     Write Path:
>     ┌─────────────────────────────────────────────────────┐
>     │                    Node Storage Engine               │
>     │                                                     │
>     │  1. Write to WAL (append-only, sequential)          │
>     │     └── Guarantees durability on crash               │
>     │                                                     │
>     │  2. Write to Memtable (in-memory sorted structure)  │
>     │     └── Red-black tree or skip list                  │
>     │     └── Serves fast reads for recently written data  │
>     │                                                     │
>     │  3. When memtable is full (~64MB):                  │
>     │     └── Flush to disk as an SSTable (sorted)         │
>     │     └── New empty memtable created                   │
>     │     └── Old WAL segment can be discarded             │
>     │                                                     │
>     │  Disk (SSTables):                                   │
>     │  ┌──────────┐  ┌──────────┐  ┌──────────┐         │
>     │  │ SSTable  │  │ SSTable  │  │ SSTable  │  Level 0 │
>     │  │ (newest) │  │          │  │ (oldest) │         │
>     │  └──────────┘  └──────────┘  └──────────┘         │
>     │       │              │              │               │
>     │       └──────────────┼──────────────┘               │
>     │                      ▼                              │
>     │  ┌────────────────────────────────────┐             │
>     │  │        Compacted SSTables          │  Level 1    │
>     │  │  (merged, deduplicated, sorted)    │             │
>     │  └────────────────────────────────────┘             │
>     │                      ▼                              │
>     │  ┌────────────────────────────────────┐             │
>     │  │      Compacted SSTables (larger)   │  Level 2    │
>     │  └────────────────────────────────────┘             │
>     └─────────────────────────────────────────────────────┘
>
>     Read Path:
>     1. Check memtable (in-memory) → O(log n) in skip list
>     2. Check bloom filters for each SSTable level
>        └── Bloom filter: probabilistic, says "definitely NOT here" or "maybe here"
>        └── Avoids reading SSTables that don't contain the key
>     3. If bloom filter says "maybe" → binary search within SSTable
>     4. Return the most recent version of the key found
> ```
>
> **Compaction** merges multiple SSTables into one, removing duplicates and deleted keys (tombstones). This is a background process that trades write amplification for better read performance."

**Interviewer:**  
How do you handle deletes? If the storage is append-only, how does a DELETE actually remove data?

**Candidate:**

> "Deletes use **tombstones**. When a key is deleted:
> 1. We write a special marker called a **tombstone** — a record that says 'key K was deleted at time T'
> 2. The tombstone is written to the memtable and flushed to SSTables just like any other write
> 3. During reads, if we encounter a tombstone, we know the key was deleted — we return 'not found'
> 4. During compaction, when the compactor encounters both a value and a tombstone for the same key, it discards the value (and the tombstone, if it's old enough)
>
> **Why not just remove the data immediately?** Because SSTables are immutable — we can't modify them in place. The tombstone is the 'delete record' in the append-only log. It also serves a critical distributed purpose: the tombstone is replicated to other nodes to inform them of the deletion. Without it, a node that missed the delete would 'resurrect' the key on the next read repair."

---

## PHASE 8: Failure Detection & Anti-Entropy (~5 min)

**Interviewer:**  
How do nodes know when another node has failed?

**Candidate:**

### Gossip-Based Failure Detection

> "Nodes use a **gossip protocol** for membership and failure detection:
>
> ```
>     Every 1 second, each node:
>     1. Picks a random other node
>     2. Sends it a 'gossip digest' — a summary of what it knows about all nodes:
>        [(Node A, heartbeat=1042, status=UP),
>         (Node B, heartbeat=887, status=UP),
>         (Node C, heartbeat=512, status=UP),
>         ...]
>     3. The receiver compares with its own state:
>        - If it has newer info → sends updates back
>        - If the sender has newer info → applies the updates
>     4. Information spreads epidemically — in O(log N) rounds,
>        all nodes converge to the same view
> ```
>
> **Failure detection**: If a node's heartbeat counter hasn't increased within a timeout (e.g., a **Phi Accrual Failure Detector** that adapts to network conditions):
> - Node is first marked as **SUSPECT** (might be a temporary network blip)
> - If it remains unresponsive for a longer period → marked as **DOWN**
> - Other nodes take over its responsibilities (hinted handoff kicks in for writes)
>
> **Why gossip instead of a central heartbeat service?**
> - No single point of failure (the failure detector itself can't fail)
> - Scales to thousands of nodes (each node only talks to O(1) peers per round)
> - Naturally tolerant of network partitions (split-brain gossips independently in each partition and reconverges when healed)"

### Anti-Entropy with Merkle Trees

> "Read repair is opportunistic — it only fixes keys that are read. For full consistency across replicas, we use **Merkle tree-based anti-entropy repair**:
>
> ```
>     Each node maintains a Merkle tree per key range it owns:
>
>     Level 0 (root):          Hash(H1 + H2)
>                              /            \
>     Level 1:           H1=Hash(h1+h2)   H2=Hash(h3+h4)
>                         /      \           /      \
>     Level 2:          h1       h2        h3       h4
>                       │        │         │        │
>     Leaf (key range): [A-F]   [G-L]    [M-R]    [S-Z]
>                       Hash of  Hash of  Hash of  Hash of
>                       all KV   all KV   all KV   all KV
>                       pairs    pairs    pairs    pairs
>
>     Repair protocol between Node A and Node B:
>     1. Exchange root hashes
>        → If equal: trees are identical. Done. Zero data transferred.
>     2. If root hashes differ → exchange Level 1 hashes
>        → Identify which subtree differs
>     3. Recurse down to the leaf level to find the exact key range with differences
>     4. Exchange only the divergent keys
> ```
>
> **Efficiency**: Comparing two Merkle trees requires transferring only O(log N) hashes to identify the divergent keys. For a tree with millions of keys, we might only transfer a few hundred hashes + the actual divergent keys. This is far better than comparing every key."

---

## PHASE 9: Putting It All Together — Complete Write & Read Flows (~3 min)

**Candidate:**

> "Let me summarize the complete flows:
>
> ### Complete Write Flow
> ```
> 1. Client SDK hashes key K → finds coordinator Node B on the ring
> 2. Client sends PUT(K, V, TTL=24h, consistency=QUORUM) to Node B
> 3. Node B (coordinator):
>    a. Generates a timestamp (or vector clock entry)
>    b. Writes to local storage engine (WAL + memtable)
>    c. Sends write to replica nodes A and C (parallel)
> 4. Node A ACKs → quorum met (B + A = 2 of 3)
> 5. Node B ACKs to client → write complete
> 6. Node C ACKs later (async, still applied)
> 7. If Node C was down → hinted handoff: B stores hint for C
> ```
>
> ### Complete Read Flow
> ```
> 1. Client SDK hashes key K → finds coordinator Node B
> 2. Client sends GET(K, consistency=QUORUM) to Node B
> 3. Node B:
>    a. Reads locally (memtable → bloom filter → SSTables)
>    b. Sends read requests to A and C (parallel)
> 4. Node A responds with {V2, T2}
>    Node B has {V2, T2}
>    → Quorum met, return V2 to client
> 5. Node C responds late with {V1, T1} — stale
> 6. Read repair: Node B sends {V2, T2} to Node C (async)
> ```
>
> ### TTL Expiry Flow
> ```
> 1. Each key has an optional TTL stored as (creation_time + TTL_seconds)
> 2. On read: if current_time > expiry_time → return NOT_FOUND (lazy expiry)
> 3. Background compaction: discard expired keys during SSTable merges
> 4. Dedicated TTL scanner (background thread): periodically scans 
>    and writes tombstones for expired keys (proactive cleanup)
> ```"

---

## PHASE 10: Wrap-Up & Summary (~3 min)

**Interviewer:**  
We're running low on time. Summarize your design and tell me what keeps you up at night operating this system.

**Candidate:**

> "**Summary of the complete design:**
>
> | Component | Choice | Why |
> |---|---|---|
> | **Architecture** | Leaderless, Dynamo-style | High availability, tunable consistency |
> | **Partitioning** | Consistent hashing with virtual nodes (256 vnodes/node) | Even distribution, graceful rebalancing |
> | **Replication** | N=3 replicas, tunable W and R (quorum by default) | W+R>N gives strong consistency when needed |
> | **Conflict resolution** | LWW default, vector clocks optional | Simple default, power when needed |
> | **Storage engine** | LSM-tree (memtable + WAL + SSTables) | Optimized for high write throughput |
> | **Failure detection** | Gossip protocol + Phi Accrual detector | Decentralized, no SPOF |
> | **Temporary failure handling** | Hinted handoff | Maintains write availability during node failures |
> | **Permanent divergence repair** | Read repair + Merkle tree anti-entropy | Opportunistic + periodic full repair |
> | **Membership** | Gossip-based ring state dissemination | Decentralized, eventually consistent view |
>
> **What keeps me up at night:**
>
> 1. **Split-brain during network partitions** — In an AP system, both sides of a partition accept writes. When the partition heals, conflicts multiply. The conflict resolution strategy (LWW or vector clocks) needs to be battle-tested, and we need monitoring on conflict rates.
>
> 2. **Compaction storms** — LSM-tree compaction is I/O intensive. If compaction falls behind (more writes than compaction can keep up with), read latency degrades because queries need to check more SSTables. I'd invest in compaction rate monitoring and throttling.
>
> 3. **Hot keys** — Even with consistent hashing, a single viral key (e.g., a trending product page) can overwhelm the 3 nodes in its preference list. I'd add a client-side cache and request coalescing for hot keys.
>
> 4. **Cascading failures** — If one node goes down and its load shifts to neighbors, those neighbors might buckle under the extra load, causing a cascade. Vnodes mitigate this (load spreads across many nodes), but we'd need load shedding and circuit breakers.
>
> **Potential extensions:**
> - Cross-datacenter replication (async, with conflict resolution at the DC level)
> - Transactions across keys (2PC or Percolator-style)
> - Range queries (switch from hash-based to range-based partitioning — different tradeoffs)
> - Auto-tiering (hot data in memory/SSD, cold data on HDD/S3)"

---

## Interviewer's Final Assessment

**Hire Recommendation: Strong Hire (L6)**

| Dimension | Rating | Notes |
|---|---|---|
| **Problem Decomposition** | Exceeds Bar | Clearly scoped functional/non-functional requirements. Drove the conversation. |
| **Scale & Estimation** | Meets Bar | Solid math: 50TB raw, 150TB replicated, 100 nodes. |
| **Trade-off Analysis** | Exceeds Bar | Leaderless vs leader-based, LSM vs B-tree, LWW vs vector clocks — all with clear tradeoffs. |
| **Distributed Systems Depth** | Exceeds Bar | Consistent hashing with vnodes, quorum math, hinted handoff, read repair, Merkle trees, gossip protocol — mechanistic understanding, not just buzzwords. |
| **Conflict Resolution** | Exceeds Bar | Three strategies compared with use cases. DynamoDB reference shows real-world knowledge. |
| **Storage Engine** | Meets Bar | LSM-tree internals (memtable, WAL, SSTables, bloom filters, compaction, tombstones) were well explained. |
| **Operational Maturity** | Exceeds Bar | "What keeps you up at night" answer showed practical experience — compaction storms, hot keys, cascading failures. |
| **Communication** | Exceeds Bar | Structured, used diagrams and tables, checked in with the interviewer. |
| **LP: Dive Deep** | Exceeds Bar | Went deep on partitioning and consistency without being pushed. |
| **LP: Think Big** | Meets Bar | Mentioned extensions and cross-DC replication. |

**Areas for growth:** Could have discussed monitoring/observability more explicitly (what dashboards, what alerts). The transaction extension could have been explored briefly.

---

## Key Differences: L5 vs L6 Expectations for This Problem

| Aspect | L5 (SDE2) Expectation | L6 (SDE3) Expectation |
|---|---|---|
| **Requirements** | Identifies basic CRUD operations | Drives conversation: tunable consistency, TTL, conflict resolution strategy |
| **Partitioning** | "Use consistent hashing" | Explains vnodes, preference lists, rebalancing mechanics, why skip same-physical-node |
| **Replication** | "Replicate to 3 nodes" | Quorum math (W+R>N), explains what happens when quorum is/isn't met |
| **Conflicts** | "Use timestamps" (LWW only) | Compares LWW, vector clocks, CRDTs with use cases and tradeoffs |
| **Failure handling** | "Retry" or "failover" | Hinted handoff, read repair, anti-entropy with Merkle trees — layered defense |
| **Storage engine** | "Use a database" | Explains LSM-tree internals, bloom filters, compaction, tombstones |
| **Gossip protocol** | Doesn't mention | Explains epidemic gossip, Phi Accrual failure detector, O(log N) convergence |
| **Operational thinking** | Focuses on happy path | Identifies operational risks: compaction storms, hot keys, cascading failures |

---

*End of interview simulation.*