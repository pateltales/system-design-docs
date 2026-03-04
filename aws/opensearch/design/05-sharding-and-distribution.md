# OpenSearch: Sharding, Replication, and Cluster Topology

Deep-dive reference for system design interviews. Covers how OpenSearch distributes
data across nodes, replicates for durability, and coordinates reads and writes.

---

## 1. Sharding Model

An OpenSearch **index** is a logical namespace. Physically, it is split into **N primary shards**,
each of which can have **R replica copies**. Every shard -- primary or replica -- is a
self-contained **Lucene index**: a directory of inverted indices, doc values, stored fields,
and segment files.

```
Index "orders"
  N = 5 primary shards
  R = 1 replica per primary

  Total shard copies = N x (1 + R) = 5 x 2 = 10
```

Key properties:

| Property | Detail |
|---|---|
| Unit of parallelism | One shard. A 5-primary index can run a query on 5 threads in parallel. |
| Immutability | N (primary shard count) is fixed at index creation. Cannot be changed later without reindexing. R can be changed dynamically. |
| Lucene index | Each shard is literally a Lucene index with its own segment files, commit points, and merge scheduler. |
| Independence | Each shard can live on any data node. The master decides placement. |

Why is each shard a Lucene index? Because Lucene has no concept of distribution.
OpenSearch layers distribution on top of Lucene by treating each shard as an independent
search engine, then merging results at a coordinating layer.

```
Index "orders" (5 primaries, 1 replica)
==============================================================

  P0   P1   P2   P3   P4        <-- primary shards
  |    |    |    |    |
  R0   R1   R2   R3   R4        <-- replica shards

  Total shard copies on cluster: 10
  Each box = 1 Lucene index
```

---

## 2. Shard Sizing

This is the single most critical capacity planning decision in OpenSearch. Get it wrong
and you either destabilize the master or create unrecoverable hot spots.

### Rule of Thumb

**Target 10-50 GB per shard.** AWS documentation recommends staying under 50 GB.
For search-heavy workloads, 10-30 GB is better. For log analytics with mostly time-range
filters, 30-50 GB is acceptable.

### Too Small (Shard Explosion)

```
Problem: 200 microservices x daily index x 5 primaries x 2 copies
       = 200 x 365 x 5 x 2 = 730,000 shards/year

Each shard consumes:
  - ~1 MB heap on the master node for cluster state metadata
  - File handles for segment files
  - Thread pool slots during merges and refreshes
  - Network overhead for cluster state publication

730,000 shards x 1 MB = ~700 GB of master heap needed --> master OOM, cluster down
```

Symptoms of shard explosion:
- Master node GC pauses spike above 1 second
- Cluster state publication takes minutes instead of milliseconds
- Pending tasks queue grows unbounded
- Node joins/leaves trigger cascading rebalances that never finish

Mitigation:
- Use Index State Management (ISM) to delete or shrink old indices
- Use rollover indices with a max shard count policy
- Consolidate small indices (one index per team, not per service per day)
- Use data streams with ILM rollover

### Too Large (> 50 GB per shard)

Problems:
- **Shard recovery** after node failure requires copying the entire shard over the network.
  A 100 GB shard at 100 MB/s = ~17 minutes of recovery per shard.
- **Lucene merges** on large segments are CPU and I/O intensive. A single merge can
  stall indexing on that shard.
- **Unbalanced cluster**: if one shard is 100 GB and others are 10 GB, the node
  holding the large shard becomes a hot spot.
- **Relocating** a large shard during rebalancing is slow and blocks allocation.

### Sizing Worksheet

```
Given:
  - 500 GB of data per day
  - 30-day retention
  - Target shard size: 30 GB

Calculation:
  Daily data           = 500 GB
  Primary shards/day   = ceil(500 / 30) = 17 primaries
  With 1 replica       = 17 x 2 = 34 shard copies per day
  30-day retention     = 34 x 30 = 1,020 active shard copies
  Master heap for state= 1,020 x ~1 MB = ~1 GB (manageable)
  Total storage        = 500 x 30 x 2 = 30 TB (with replicas)
```

---

## 3. Document Routing

When a document is indexed, OpenSearch must decide which primary shard receives it.
The formula is deterministic:

```
shard_id = hash(_routing) % number_of_primary_shards
```

- **Default `_routing`** = the document `_id`.
- The hash function is MurmurHash3.
- The result is deterministic: given the same `_routing` and shard count, the same shard
  is always selected.

### Why Shard Count Is Immutable

The modulo operation depends on `number_of_primary_shards`. If you change it from 5 to 7,
the mapping of every document changes. Document with hash 12 goes to shard 2 with 5 shards
(12 % 5 = 2) but shard 5 with 7 shards (12 % 7 = 5). There is no way to remap without
reindexing every document.

This is fundamentally different from consistent hashing (used by DynamoDB, Cassandra) where
adding a node only remaps a fraction of keys.

```
hash("doc-abc") = 8438291

With 5 primaries:   8438291 % 5 = 1   --> Shard P1
With 7 primaries:   8438291 % 7 = 4   --> Shard P4   (DIFFERENT!)

Changing shard count = every document potentially moves = must reindex
```

### Custom Routing

You can set `_routing` explicitly to co-locate related documents on the same shard.

Use case: multi-tenant SaaS. Set `_routing = tenant_id` so all of tenant X's documents
land on the same shard.

```
PUT /orders/_doc/order-123?routing=tenant-42
{
  "tenant_id": "tenant-42",
  "amount": 99.99
}

Benefit:  GET /orders/_search?routing=tenant-42
          --> only hits 1 shard instead of all 5 --> 5x less fan-out
```

Trade-off: If one tenant has 10x more data than others, its shard becomes a hot spot.
Mitigation: use routing with `index.routing_partition_size` to spread a single routing
value across multiple shards (subset, not all).

---

## 4. Node Roles and Cluster Topology

### Node Roles

| Role | Purpose | Resource Profile |
|---|---|---|
| **Master-eligible** | Manage cluster state (shard allocation table, index metadata, mappings). Participate in master election. | Low CPU, low disk, moderate RAM for cluster state. Dedicated = no data. |
| **Data** | Store shard data. Execute indexing and search operations. Run Lucene merges. | High CPU, high disk I/O, high RAM (for OS page cache and field data). |
| **Coordinating-only** | Route requests, scatter queries to shards, gather and merge results. No data, not master-eligible. | Moderate CPU (for merging/sorting), moderate RAM (for aggregation buffers). |
| **Ingest** | Run ingest pipelines (grok, dissect, geoip, etc.) before indexing. | High CPU for parsing. Can be co-located with data or dedicated. |
| **UltraWarm** (AWS) | Store older indices on S3-backed storage with local SSD cache. Read-only. | Low local disk, backed by S3. ~90% cheaper than hot storage. |
| **Cold** (AWS) | Indices fully detached to S3. Must be attached before querying. | Near-zero local resources. S3 cost only. |

### Cluster Topology -- ASCII Diagram

```
                        +-------------------------------+
                        |        Load Balancer          |
                        +-------------------------------+
                                     |
                    +----------------+----------------+
                    |                |                |
              +-----------+   +-----------+   +-----------+
              |  Coord 1  |   |  Coord 2  |   |  Coord 3  |
              | (scatter/ |   | (scatter/ |   | (scatter/ |
              |  gather)  |   |  gather)  |   |  gather)  |
              +-----------+   +-----------+   +-----------+
                    |                |                |
      +-------------+----------------+----------------+----------+
      |              |               |                |          |
+----------+  +----------+  +----------+  +----------+  +----------+
| Master 1 |  | Master 2 |  | Master 3 |  |          |  |          |
| (active) |  | (standby)|  | (standby)|  |          |  |          |
+----------+  +----------+  +----------+  |          |  |          |
                                          |          |  |          |
      +-----------------------------------+          |  |          |
      |                                              |  |          |
+===========+  +===========+  +===========+  +===========+  +===========+
| Data-1    |  | Data-2    |  | Data-3    |  | Data-4    |  | Data-5    |
| (AZ-a)    |  | (AZ-b)   |  | (AZ-c)   |  | (AZ-a)    |  | (AZ-b)   |
|           |  |           |  |           |  |           |  |           |
| P0, R3    |  | P1, R4    |  | P2, R0   |  | P3, R1    |  | P4, R2   |
+===========+  +===========+  +===========+  +===========+  +===========+
      HOT TIER                                     |
      ---------------------------------------------|
                                                   |
                              +-----------+  +-----------+
                              | UltraWarm |  | UltraWarm |
                              | (S3-back) |  | (S3-back) |
                              | old logs  |  | old logs  |
                              +-----------+  +-----------+
                                WARM TIER
                                   |
                              +-----------+
                              |   Cold    |
                              | (S3 only) |
                              | archived  |
                              +-----------+
                               COLD TIER
```

Key points about the diagram:
- **3 dedicated master nodes** across 3 AZs. They hold no data. They are lightweight.
- **Coordinating nodes** sit behind the load balancer and handle all client traffic.
  They shield data nodes from connection overhead.
- **Data nodes** are spread across AZs. Notice primaries and replicas are placed
  so that no primary and its replica share the same AZ (zone awareness).
- **UltraWarm/Cold** are AWS-specific tiered storage nodes for cost optimization.

### Why 3 Dedicated Masters?

Masters manage the cluster state: a data structure containing every index's settings,
mappings, and the location of every shard on every node. On a cluster with 10,000 shards,
this state can be several hundred MB. Publishing it on every change must be fast.

If masters also hold data, a heavy merge or a large aggregation can cause GC pauses,
delaying cluster state publication, which makes the whole cluster think the master is dead,
triggering a new election, causing shard allocation storms.

Dedicated masters avoid this entirely. They are cheap (c5.large is sufficient for most clusters).

---

## 5. Shard Allocation and Rebalancing

The **active master** is responsible for deciding which shard lives on which node. This
mapping is stored in the cluster state and published to all nodes.

### Allocation Rules

1. **Primary first**: When an index is created, the master allocates primaries to data nodes.
2. **Replica placement**: Replicas are allocated to different nodes than their corresponding primary.
3. **Zone awareness**: With `cluster.routing.allocation.awareness.attributes: zone`,
   the master ensures a primary in AZ-a has its replica in AZ-b or AZ-c. Never the same AZ.
4. **Disk watermarks**:
   - Low (85%): no new shards allocated to this node
   - High (90%): shards actively relocated away
   - Flood (95%): all indices on this node set to read-only

### Rebalancing

Triggered by:
- Node joins the cluster (new capacity available)
- Node leaves the cluster (shards on that node become unassigned)
- Index creation or deletion
- Manual reroute API call

Throttling:
```
cluster.routing.allocation.node_concurrent_recoveries: 2    (default)
cluster.routing.allocation.cluster_concurrent_rebalance: 2   (default)
indices.recovery.max_bytes_per_sec: 40mb                     (default)
```

These defaults prevent rebalancing from saturating network and disk I/O. On large clusters,
you may increase them during planned maintenance windows.

### Shard Distribution Example Across AZs

```
3 AZs, 6 data nodes (2 per AZ), Index with 3 primaries, 1 replica

AZ-a              AZ-b              AZ-c
+-----------+     +-----------+     +-----------+
| Data-1    |     | Data-3    |     | Data-5    |
|  P0       |     |  P1       |     |  P2       |
+-----------+     +-----------+     +-----------+
| Data-2    |     | Data-4    |     | Data-6    |
|  R1       |     |  R2       |     |  R0       |
+-----------+     +-----------+     +-----------+

If AZ-a goes down entirely:
  - P0 is lost. R0 (on Data-6, AZ-c) is promoted to primary.
  - R1 is lost. Master allocates a new R1 on a surviving node.
  - Zero data loss. Zero downtime (after promotion delay).
```

---

## 6. Split-Brain Prevention

Split-brain occurs when a network partition causes two groups of nodes to each elect
their own master. Both sides accept writes. When the partition heals, the cluster has
divergent state that cannot be automatically reconciled.

### Quorum-Based Master Election

```
Master election requires a QUORUM = (master-eligible nodes / 2) + 1

3 master-eligible nodes --> quorum = 2
5 master-eligible nodes --> quorum = 3

With 3 masters and a network partition:
  Partition A: [M1, M2]     --> 2 nodes, 2 >= 2 quorum --> CAN elect master
  Partition B: [M3]         --> 1 node,  1 < 2 quorum  --> CANNOT elect master

  Result: only one side has a master. No split-brain.
```

Why always an odd number? With 2 masters and a partition, each side has 1 node. Neither
reaches quorum of 2. The cluster is completely down. With 4 masters, a 2-2 split also
leaves both sides unable to elect. You pay for 4 nodes but get the same fault tolerance
as 3.

### OpenSearch Discovery Protocol

OpenSearch (and Elasticsearch 7+) uses a protocol based on Raft-like consensus:
- Nodes discover each other via seed hosts or DNS
- A leader (master) is elected by quorum vote
- Cluster state changes are committed only when a quorum of master-eligible nodes acknowledge
- The `discovery.seed_hosts` setting lists initial nodes to contact
- The `cluster.initial_master_nodes` setting is used only for the very first bootstrap

### Network Partition Handling

```
Normal:       M1 <--> M2 <--> M3    (M1 is active master)

Partition:    [M1, M2] | [M3]

M1 remains master (has quorum of 2).
M3 steps down. Data nodes connected only to M3 cannot join the cluster.
They reject writes and searches until the partition heals.

Partition heals:
M3 rejoins, receives latest cluster state from M1.
Data nodes behind M3 rejoin, receive shard assignments.
```

---

## 7. Write Path with Replication

### Step-by-Step Flow

```
Client
  |
  | 1. PUT /orders/_doc/abc
  v
Coordinating Node
  |
  | 2. Route: hash("abc") % 5 = shard 2
  |    Look up cluster state: P2 is on Data-3
  v
Data-3 (Primary Shard P2)
  |
  | 3a. Write to TRANSLOG (append-only WAL on disk) -- durability
  | 3b. Write to IN-MEMORY BUFFER (not yet searchable)
  | 3c. Validate mapping, assign version, sequence number
  |
  | 4. REPLICATE: forward operation to all in-sync replica copies
  v
Data-6 (Replica Shard R2)
  |
  | 5a. Write to its own translog
  | 5b. Write to its own in-memory buffer
  | 5c. ACK back to primary
  |
  v
Data-3 (Primary)
  |
  | 6. All in-sync replicas ACK'd (or wait_for_active_shards met)
  v
Coordinating Node
  |
  | 7. Return 201 Created to client
  v
Client
```

### Translog and Refresh

The in-memory buffer is **not searchable** until a **refresh** occurs:
- Default refresh interval: 1 second
- Refresh creates a new Lucene segment in the OS file system cache
- This is why OpenSearch is "near real-time" (NRT), not real-time

The translog provides durability between refreshes:
- Every operation is appended to the translog before acknowledgment
- On node crash, the translog is replayed to recover operations since the last Lucene commit
- `index.translog.durability`: `request` (fsync per operation, default) or `async` (fsync every 5s, faster but risks 5s of data loss)

### `wait_for_active_shards`

Controls how many shard copies must be active before the write proceeds:

| Setting | Behavior |
|---|---|
| `1` (default) | Only the primary must be active. Replicas receive the write asynchronously. |
| `2` | Primary + 1 replica must be active before returning. |
| `all` | Primary + all replicas must be active. Strongest durability, highest latency. |

```
PUT /orders/_settings
{
  "index.write.wait_for_active_shards": "2"
}
```

---

## 8. Read Path (Query Then Fetch)

OpenSearch splits search into two phases to avoid transferring full documents from
every shard.

### Query Phase

```
Client
  |
  | GET /orders/_search { "query": { "match": { "status": "shipped" } }, "size": 10 }
  v
Coordinating Node
  |
  | 1. Fan out query to one copy of EACH shard (primary or replica)
  |    Uses adaptive replica selection (ARS): picks the copy with
  |    lowest response time and queue depth.
  |
  +--------+--------+--------+--------+
  |        |        |        |        |
  v        v        v        v        v
 S0       S1       S2       S3       S4     (one copy each, any mix of P/R)
  |        |        |        |        |
  | Each shard runs the query locally against its Lucene index.
  | Returns top-10 {_id, _score} pairs (lightweight, no _source).
  |        |        |        |        |
  +--------+--------+--------+--------+
  |
  v
Coordinating Node
  |
  | 2. Merge 5 x 10 = 50 results. Global top-10 by _score.
```

### Fetch Phase

```
Coordinating Node
  |
  | 3. Send multi-GET for the winning 10 doc IDs to the shards that own them.
  |    Only fetches _source from ~2-3 shards (not all 5).
  |
  +--------+--------+
  |        |        |
  v        v        v
 S1       S3       S4       (only shards that have the winning docs)
  |        |        |
  | Return full _source for requested docs.
  |        |        |
  +--------+--------+
  |
  v
Coordinating Node
  |
  | 4. Assemble final response with _source, _score, highlights, etc.
  v
Client
```

### Adaptive Replica Selection (ARS)

Instead of round-robin across replicas, ARS picks the shard copy most likely to
respond fastest based on:
- Historical response times (exponential moving average)
- Current queue depth on the target node
- Number of in-flight requests to that node

This automatically routes around slow nodes (e.g., a node doing heavy merges)
without manual intervention.

### Why Two Phases?

If you skip the query phase and fetch full documents from every shard:
- 5 shards x 10 documents x avg 2 KB = 100 KB of data transferred
- But you only need 10 documents in the final result = 20 KB

With two phases:
- Query phase: 5 shards x 10 x ~50 bytes (just _id + _score) = 2.5 KB
- Fetch phase: ~10 x 2 KB = 20 KB
- Total: ~22.5 KB vs 100 KB

For large documents or high `size` values, the savings are dramatic.

---

## 9. Contrast with DynamoDB and Cassandra

| Dimension | OpenSearch | DynamoDB | Cassandra |
|---|---|---|---|
| **Partitioning** | Fixed shard count, modulo routing | Consistent hashing with auto-split | Consistent hashing (virtual nodes) |
| **Shard count** | Immutable at creation. Must reindex to change. | Automatic. Partitions split at 10 GB. Invisible to user. | Fixed virtual nodes per physical node. Token range assigned at join. |
| **Rebalancing on scale** | Manual: add nodes, shards rebalance. Shard count unchanged. | Automatic: partitions split and migrate transparently. | Automatic: token ranges reassigned via streaming. |
| **Replica model** | N primary shards x (1+R) copies. Replicas are full copies of Lucene index. | Synchronous replication across 3 AZs (built-in, not configurable). | Tunable replication factor. Eventual or tunable consistency (QUORUM, ALL). |
| **Consistency** | Near real-time (1s refresh). No read-after-write guarantee unless refresh forced. | Strong consistency available (strongly consistent reads). Immediate read-after-write. | Tunable: ONE, QUORUM, ALL. QUORUM gives strong consistency with RF=3. |
| **Access pattern** | Full-text search, aggregations, fuzzy matching, relevance scoring. | Key-value and simple queries on partition key + sort key. | Key-value and CQL queries on partition key + clustering columns. |
| **Capacity planning** | Manual: choose shard count, instance types, storage. Critical to get right. | On-demand or provisioned RCU/WCU. No shard sizing decisions. | Choose RF, node count, instance types. Less shard-level tuning. |
| **Failure recovery** | Replica promoted. New replica built by copying shard data (slow for large shards). | Transparent. AWS handles replication and failover. | Hinted handoff + read repair + anti-entropy repair. |
| **Hot shard problem** | Custom routing can cause skew. Mitigated by routing partition size. | Hot partition on popular key. Mitigated by adaptive capacity. | Hot partition on popular key. Mitigated by virtual nodes. |

### Key Interview Insight

OpenSearch chose **fixed modulo hashing** over consistent hashing because:
1. It guarantees even distribution when documents have uniformly distributed IDs.
2. It allows the coordinating node to compute the target shard in O(1) without
   consulting a ring or routing table.
3. The trade-off (immutable shard count) is acceptable because OpenSearch indices
   are often time-based and short-lived (daily/weekly rollover). You create a new
   index with the right shard count rather than reshard an existing one.

DynamoDB and Cassandra chose **consistent hashing** because:
1. Tables are long-lived (years). Resharding must happen transparently.
2. Data grows unpredictably. Auto-splitting at a threshold is essential.
3. They only do key-value lookups, so the overhead of a ring lookup is trivial
   compared to OpenSearch's scatter-gather across all shards.

---

## Quick Reference: Critical Numbers

| Metric | Guideline |
|---|---|
| Shard size | 10-50 GB |
| Max shards per node | ~1,000 (depends on heap) |
| Master heap for shard metadata | ~1 MB per shard |
| Dedicated master nodes | 3 (always odd) |
| Refresh interval (NRT) | 1 second default |
| Translog fsync | Per request (default) or async every 5s |
| Recovery throttle | 40 MB/s default, 2 concurrent recoveries per node |
| Disk watermark low/high/flood | 85% / 90% / 95% |
| Quorum | (master-eligible / 2) + 1 |
