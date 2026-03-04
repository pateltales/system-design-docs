# Redis Cluster — Deep Dive

## Table of Contents

1. [Hash Slot Model](#1-hash-slot-model)
2. [Cluster Topology](#2-cluster-topology)
3. [Cluster Bus (Gossip Protocol)](#3-cluster-bus-gossip-protocol)
4. [Client Redirection: MOVED and ASK](#4-client-redirection-moved-and-ask)
5. [Hash Tags](#5-hash-tags)
6. [Resharding / Slot Migration](#6-resharding--slot-migration)
7. [Failure Detection](#7-failure-detection)
8. [Follower Election and Promotion](#8-follower-election-and-promotion)
9. [Epoch-Based Configuration](#9-epoch-based-configuration)
10. [Replica Migration](#10-replica-migration)
11. [Limitations](#11-limitations)
12. [Contrast with Memcached](#12-contrast-with-memcached)
13. [Scaling Decisions and Trade-offs](#13-scaling-decisions-and-trade-offs)

---

## 1. Hash Slot Model

Redis Cluster partitions the entire keyspace into exactly **16,384 hash slots** (numbered 0 through 16,383). Every key maps deterministically to one slot, and every slot is assigned to exactly one leader node.

### Key-to-Slot Mapping

```
Key: "user:1000"

Step 1: CRC16("user:1000")   = 0xB72E   (46,894 in decimal)
Step 2: 46894 mod 16384      = 14126
Step 3: Slot 14126 → Node C  (based on current slot assignment table)
```

The mapping pipeline:

```
  key
   |
   v
CRC16(key)          # XMODEM variant, polynomial 0x1021
   |                # 16-bit output: 0x0000 – 0xFFFF (0 – 65,535)
   v
mod 16384           # Reduce to slot range: 0 – 16,383
   |
   v
Slot Table Lookup   # slot → leader node
   |
   v
Target Node
```

### CRC16 Details

| Property        | Value                                          |
|-----------------|------------------------------------------------|
| Algorithm       | CRC16-XMODEM (also called CRC16-CCITT-FALSE)  |
| Polynomial      | 0x1021 (x^16 + x^12 + x^5 + 1)               |
| Initial value   | 0x0000                                         |
| Output bits     | 16                                             |
| Output range    | 0 – 65,535                                     |

### Why Exactly 16,384 Slots?

The number 16,384 (2^14) was chosen as a deliberate engineering trade-off:

| Factor                     | 16,384 slots          | 65,536 slots (2^16)    | 4,096 slots (2^12)    |
|----------------------------|-----------------------|------------------------|-----------------------|
| Bitmap size per node       | ~2 KB (16384/8)       | ~8 KB (65536/8)        | ~512 bytes (4096/8)   |
| Gossip ping/pong payload   | ~2 KB per message     | ~8 KB per message      | ~512 bytes per message |
| Max practical cluster size | ~1,000 nodes          | ~1,000 nodes           | ~250 nodes            |
| Slot granularity           | Fine enough           | Excessive              | Too coarse             |
| Gossip bandwidth at 1000 nodes | Manageable       | 4x overhead            | Low but limiting       |

**The math**: In gossip, every ping/pong message carries the full slot bitmap. With 1,000 nodes each exchanging ~2 KB messages, the overhead stays reasonable. At 8 KB per message (65,536 slots), the gossip bandwidth quadruples for no meaningful benefit. At 512 bytes (4,096 slots), the cluster tops out at ~250 useful nodes because you cannot split 4,096 slots finely enough across more nodes.

### Slot Assignment

Each slot is assigned to exactly one leader node. There is no overlap, no sharing:

```
Node A: slots 0     – 5,460    (5,461 slots)
Node B: slots 5,461 – 10,922   (5,462 slots)
Node C: slots 10,923 – 16,383  (5,461 slots)
```

The assignment is stored by every node in the cluster and propagated via gossip.

---

## 2. Cluster Topology

### Architecture

```
                        Redis Cluster (9-node example)
  ┌─────────────────────────────────────────────────────────────────────┐
  │                                                                     │
  │   Shard 0                Shard 1                Shard 2             │
  │   Slots 0–5460          Slots 5461–10922       Slots 10923–16383   │
  │                                                                     │
  │   ┌──────────┐          ┌──────────┐           ┌──────────┐        │
  │   │ Leader A │          │ Leader B │           │ Leader C │        │
  │   │ :6379    │          │ :6379    │           │ :6379    │        │
  │   └────┬─────┘          └────┬─────┘           └────┬─────┘        │
  │        │                     │                      │               │
  │    ┌───┴───┐             ┌───┴───┐              ┌───┴───┐          │
  │    │       │             │       │              │       │          │
  │  ┌─┴──┐ ┌─┴──┐       ┌─┴──┐ ┌─┴──┐        ┌─┴──┐ ┌─┴──┐       │
  │  │ A1 │ │ A2 │       │ B1 │ │ B2 │        │ C1 │ │ C2 │       │
  │  │:637│ │:637│       │:637│ │:637│        │:637│ │:637│       │
  │  └────┘ └────┘       └────┘ └────┘        └────┘ └────┘       │
  │  Follower Follower   Follower Follower    Follower Follower    │
  │                                                                     │
  └─────────────────────────────────────────────────────────────────────┘
```

### Topology Properties

| Property                          | Value / Detail                                         |
|-----------------------------------|--------------------------------------------------------|
| Minimum viable cluster            | 3 leader nodes (no followers = no HA)                  |
| Typical production setup          | 3 leaders x 3 followers = 9 nodes                      |
| Each leader-follower group        | Provides HA for one slot range (shard)                 |
| Supported databases               | Only DB 0 (SELECT is disabled in Cluster mode)         |
| Replication model                 | Asynchronous (same as standalone replication)           |
| Node identity                     | 40-character hex node ID, generated once, persisted     |

### Why Only DB 0?

Redis standalone supports 16 databases (0-15) via SELECT. In Cluster mode, allowing multiple databases would mean each key's slot must also carry a database identifier, complicating the slot-routing protocol. The Cluster specification simplifies this: every key lives in DB 0.

### Node Roles

Every node in the cluster is one of:

| Role     | Responsibilities                                                         |
|----------|--------------------------------------------------------------------------|
| Leader   | Owns hash slots, accepts reads and writes for those slots                |
| Follower | Replicates a leader, serves reads (if READONLY enabled), promotes on failure |

---

## 3. Cluster Bus (Gossip Protocol)

### Port Allocation

Every Redis Cluster node listens on two ports:

| Port              | Purpose                   | Protocol |
|-------------------|---------------------------|----------|
| Data port (6379)  | Client commands (GET, SET) | RESP     |
| Bus port (16379)  | Node-to-node gossip        | Binary   |

The bus port is always **data port + 10,000**. This is not configurable (as of Redis 7.x). The binary protocol used on the bus port is not RESP — it is a custom, compact binary format optimized for cluster metadata exchange.

### Gossip Message Contents

Each Ping/Pong message carries:

```
┌──────────────────────────────────────────────────────────┐
│                    Cluster Message                        │
├──────────────────────────────────────────────────────────┤
│  Sender Node ID          (40 bytes)                      │
│  Sender IP               (variable)                      │
│  Sender Data Port        (2 bytes)                       │
│  Sender Bus Port         (2 bytes)                       │
│  Sender Flags            (leader/follower/PFAIL/FAIL)    │
│  Sender Slot Bitmap      (2,048 bytes = 16384 bits)      │
│  Sender currentEpoch     (8 bytes)                       │
│  Sender configEpoch      (8 bytes)                       │
│  Sender Replication Offset (8 bytes)                     │
│  ─────────────────────────────────────────────────────── │
│  Gossip Section: info about N other random nodes         │
│    For each: node ID, IP, port, flags, ping_sent,        │
│              pong_received                                │
└──────────────────────────────────────────────────────────┘

Total per message: ~2 KB (dominated by the 2 KB slot bitmap)
```

### Gossip Scheduling

Each node follows this schedule:

1. **Every 1 second**: pick a random node from the known-nodes list and send it a PING.
2. **Additionally**: if any node has not been pinged within `cluster-node-timeout / 2` milliseconds, send it a PING immediately.

The second rule ensures that no node goes unmonitored for too long, even with unlucky random selection.

### Bandwidth Estimation

With N nodes in the cluster:

| Cluster Size (N) | Pings per node per timeout period | Total messages/sec (approx)       | Bandwidth per node         |
|-------------------|-----------------------------------|-----------------------------------|----------------------------|
| 10                | ~10                               | ~100 total/timeout                | Negligible                 |
| 100               | ~100                              | ~10,000 total/timeout             | ~200 KB/timeout (~13 KB/s) |
| 1,000             | ~1,000                            | ~1,000,000 total/timeout          | ~2 MB/timeout (~133 KB/s)  |

*(Assuming cluster-node-timeout = 15,000 ms = 15 seconds)*

At 1,000 nodes, each node sends/receives ~133 KB/s of gossip traffic. This is the practical upper bound — beyond this, gossip overhead starts consuming real bandwidth.

### Convergence

Gossip is **probabilistic and eventually consistent**. A state change (e.g., a node going down) propagates logarithmically:

- With N nodes, full cluster learns about a change in O(log N) gossip rounds
- For 100 nodes: ~7 rounds (~7 seconds worst case)
- For 1,000 nodes: ~10 rounds (~10 seconds worst case)

---

## 4. Client Redirection: MOVED and ASK

When a client sends a command to the wrong node, the node does not proxy the request. Instead, it returns a redirection error. There are two types.

### MOVED — Permanent Redirection

MOVED means "this slot permanently lives on another node — update your routing table."

```
Client                    Node A                    Node B
  │                      (owns slots 0–5460)       (owns slots 5461–10922)
  │                         │                         │
  │  GET user:1000          │                         │
  │  (slot 14126)           │                         │
  │ ───────────────────────>│                         │
  │                         │                         │
  │  -MOVED 14126           │                         │
  │   192.168.1.7:6379      │                         │
  │ <───────────────────────│                         │
  │                         │                         │
  │  [Client updates local slot map:                  │
  │   slot 14126 → 192.168.1.7:6379]                 │
  │                         │                         │
  │  GET user:1000          │                         │
  │ ──────────────────────────────────────────────────>│
  │                         │                         │
  │  "John Doe"             │                         │
  │ <──────────────────────────────────────────────────│
  │                         │                         │
```

**Error format**: `-MOVED <slot> <ip>:<port>`

Example: `-MOVED 7438 192.168.1.5:6379`

After receiving MOVED, a smart client:
1. Updates its local slot-to-node mapping for that slot
2. Retries the command on the correct node
3. All future commands for that slot go directly to the correct node

### ASK — Temporary Redirection (Migration in Progress)

ASK means "this particular key has already been migrated to the target, but the slot is still officially mine. Check the target for this one request."

```
Client                    Node A (source)           Node B (target)
  │                      (migrating slot 7438)      (importing slot 7438)
  │                         │                         │
  │  GET order:500          │                         │
  │  (slot 7438)            │                         │
  │ ───────────────────────>│                         │
  │                         │                         │
  │  [Key "order:500" not   │                         │
  │   found locally —       │                         │
  │   already migrated]     │                         │
  │                         │                         │
  │  -ASK 7438              │                         │
  │   192.168.1.7:6379      │                         │
  │ <───────────────────────│                         │
  │                         │                         │
  │  ASKING                 │                         │
  │ ──────────────────────────────────────────────────>│
  │                         │                         │
  │  OK                     │                         │
  │ <──────────────────────────────────────────────────│
  │                         │                         │
  │  GET order:500          │                         │
  │ ──────────────────────────────────────────────────>│
  │                         │                         │
  │  "shipped"              │                         │
  │ <──────────────────────────────────────────────────│
  │                         │                         │
```

**Error format**: `-ASK <slot> <ip>:<port>`

**Critical difference from MOVED**: the client does NOT update its slot map. The next request for slot 7438 still goes to Node A (the source), because the migration is not yet complete.

### MOVED vs ASK Comparison

| Property                   | MOVED                          | ASK                            |
|----------------------------|--------------------------------|--------------------------------|
| When returned              | Slot permanently on other node | Key migrated during resharding |
| Client updates slot map?   | Yes                            | No                             |
| Requires ASKING command?   | No                             | Yes (before retrying)          |
| Permanent or temporary?    | Permanent                      | Temporary                      |
| Frequency                  | After resharding completes     | During live resharding         |

### Smart Clients

Production clients maintain a local copy of the slot-to-node mapping:

| Client Library | Language | How it refreshes slot map                             |
|----------------|----------|-------------------------------------------------------|
| Jedis          | Java     | CLUSTER SLOTS on MOVED, or periodic refresh           |
| Lettuce        | Java     | CLUSTER SHARDS (Redis 7+), adaptive topology refresh  |
| redis-py       | Python   | CLUSTER SLOTS on startup, refresh on MOVED            |
| ioredis        | Node.js  | CLUSTER SLOTS, automatic refresh on errors            |

Commands used to fetch the slot map:

- `CLUSTER SLOTS` — returns slot ranges and their leader/follower nodes (deprecated in Redis 7.0)
- `CLUSTER SHARDS` — returns shard-oriented view (Redis 7.0+, recommended)

---

## 5. Hash Tags

Hash tags let you force multiple keys into the same hash slot so that multi-key operations work.

### How Hash Tags Work

```
Key: "{user:1000}.profile"

Step 1: Find first '{' at index 0
Step 2: Find first '}' after '{' at index 10
Step 3: Content between braces: "user:1000"
Step 4: CRC16("user:1000") mod 16384 = slot X

Key: "{user:1000}.settings"

Step 4: CRC16("user:1000") mod 16384 = slot X   ← Same slot!
```

### Rules for Hash Tag Extraction

1. Find the **first** occurrence of `{`
2. Find the **first** occurrence of `}` **after** that `{`
3. If the substring between them is **non-empty**, use it as the hash input
4. Otherwise, hash the entire key as usual

| Key                       | Hash input       | Reason                                    |
|---------------------------|------------------|-------------------------------------------|
| `{user:1000}.profile`    | `user:1000`      | Content between first `{}`                |
| `{user:1000}.settings`   | `user:1000`      | Same hash tag, same slot                  |
| `foo{}{bar}`             | entire key       | First `{}` is empty, so tag ignored       |
| `foo{{bar}}`             | `{bar`           | First `{` to first `}` gives `{bar`       |
| `{}.user`                | entire key       | Empty content between `{}`                |
| `user:1000`              | `user:1000`      | No braces, entire key is hashed           |

### Use Cases for Hash Tags

```
# These all land on the same slot because of {user:1000}:

SET   {user:1000}.name      "Alice"
SET   {user:1000}.email     "alice@example.com"
SET   {user:1000}.prefs     '{"theme":"dark"}'
HSET  {user:1000}.sessions  "abc123" "active"

# Multi-key operations now work:
MGET  {user:1000}.name {user:1000}.email
```

### CROSSSLOT Error

Without hash tags, multi-key operations on keys in different slots fail:

```
> MGET user:1000 user:2000
(error) CROSSSLOT Keys in request don't hash to the same slot
```

**Operations affected by CROSSSLOT**:
- `MGET`, `MSET`, `DEL` (multiple keys)
- `MULTI/EXEC` transactions with keys in different slots
- Lua scripts accessing keys in different slots
- `SUNION`, `SINTER`, `SDIFF` across different slots

---

## 6. Resharding / Slot Migration

Resharding moves hash slots from one node to another while the cluster remains online. Zero downtime.

### Migration Protocol — Step by Step

```
┌─────────────────────────────────────────────────────────────────────┐
│                    Slot Migration Protocol                           │
│                    (migrating slot 7438 from A → B)                 │
├─────────────────────────────────────────────────────────────────────┤
│                                                                     │
│  Step 1: Mark slot as MIGRATING on source                          │
│  ─────────────────────────────────────────                          │
│  Node A> CLUSTER SETSLOT 7438 MIGRATING <B-node-id>               │
│                                                                     │
│  Step 2: Mark slot as IMPORTING on target                          │
│  ─────────────────────────────────────────                          │
│  Node B> CLUSTER SETSLOT 7438 IMPORTING <A-node-id>               │
│                                                                     │
│  Step 3: Migrate keys one by one                                   │
│  ────────────────────────────────                                   │
│  For each key in slot 7438 on Node A:                              │
│    Node A> CLUSTER GETKEYSINSLOT 7438 100                          │
│    → returns up to 100 keys                                        │
│    Node A> MIGRATE <B-ip> <B-port> <key> 0 5000                   │
│    → atomically moves key to B, deletes from A                     │
│    (repeat until no keys remain)                                   │
│                                                                     │
│  Step 4: Finalize — notify all nodes                               │
│  ────────────────────────────────────                               │
│  ALL NODES> CLUSTER SETSLOT 7438 NODE <B-node-id>                 │
│  → every node updates its slot table                               │
│                                                                     │
└─────────────────────────────────────────────────────────────────────┘
```

### Detailed Sequence Diagram

```
  Operator/Tool         Node A (source)          Node B (target)         Other Nodes
       │                     │                        │                      │
       │  SETSLOT 7438       │                        │                      │
       │  MIGRATING B        │                        │                      │
       │────────────────────>│                        │                      │
       │                     │ [slot 7438 now in      │                      │
       │                     │  MIGRATING state]      │                      │
       │                     │                        │                      │
       │  SETSLOT 7438       │                        │                      │
       │  IMPORTING A        │                        │                      │
       │───────────────────────────────────────────── >│                      │
       │                     │                        │ [slot 7438 now in    │
       │                     │                        │  IMPORTING state]    │
       │                     │                        │                      │
       │  GETKEYSINSLOT      │                        │                      │
       │  7438 100           │                        │                      │
       │────────────────────>│                        │                      │
       │  [key1, key2, ...]  │                        │                      │
       │<────────────────────│                        │                      │
       │                     │                        │                      │
       │  MIGRATE B key1     │                        │                      │
       │────────────────────>│───── DUMP+RESTORE ────>│                      │
       │                     │<──── OK ───────────────│                      │
       │                     │ [deletes key1 locally] │                      │
       │  OK                 │                        │                      │
       │<────────────────────│                        │                      │
       │                     │                        │                      │
       │  ... repeat for all keys ...                 │                      │
       │                     │                        │                      │
       │  SETSLOT 7438       │                        │                      │
       │  NODE B             │                        │                      │
       │────────────────────>│                        │                      │
       │───────────────────────────────────────────── >│                      │
       │──────────────────────────────────────────────────────────────────── >│
       │                     │                        │                      │
       │               [All nodes: slot 7438 → B]     │                      │
```

### MIGRATE Command Internals

The `MIGRATE` command is atomic at the single-key level:

1. Source serializes the key (DUMP)
2. Source sends serialized data to target (via a direct socket connection)
3. Target restores the key (RESTORE)
4. Target sends OK to source
5. Source deletes the key locally
6. If any step fails, the key remains on the source (no data loss)

**MIGRATE options**:

| Option   | Purpose                                               |
|----------|-------------------------------------------------------|
| COPY     | Do not delete from source after transfer              |
| REPLACE  | Overwrite key on target if it already exists          |
| KEYS     | Migrate multiple keys in one MIGRATE call (Redis 3.0.6+) |

### Behavior During Migration

| Scenario                         | What happens on source (Node A)                        |
|----------------------------------|--------------------------------------------------------|
| Key exists on source             | Serves the key normally                                |
| Key does NOT exist on source     | Returns `-ASK 7438 <B-ip>:<B-port>`                   |
| New write to migrating slot      | Accepted if key is still on source, ASK if already gone|

### The Big Key Problem

`MIGRATE` serializes and transfers the key on the main thread, blocking all other operations:

| Key size | Approximate MIGRATE time | Impact                        |
|----------|--------------------------|-------------------------------|
| 1 KB     | < 1 ms                   | Negligible                    |
| 1 MB     | ~1–5 ms                  | Minor                         |
| 100 MB   | ~100–500 ms              | Noticeable latency spike      |
| 1 GB     | ~1–5 seconds             | Severe — blocks entire node   |

**Mitigations**:
- Redis 7.0+ improved non-blocking migration for large keys
- Break large data structures into smaller keys before migration
- Use `redis-cli --cluster reshard` for automated, paced resharding
- Monitor with `CLUSTER INFO` during resharding

### Automated Resharding

```bash
# Automated tool handles the entire protocol:
redis-cli --cluster reshard <any-node-ip>:<port> \
  --cluster-from <source-node-id> \
  --cluster-to <target-node-id> \
  --cluster-slots 1000 \
  --cluster-yes
```

This moves 1,000 slots from source to target, executing all four steps automatically.

---

## 7. Failure Detection

Redis Cluster uses a two-phase failure detection mechanism to avoid false positives from transient network issues.

### Phase 1: PFAIL (Probable Failure)

```
Node A                         Node X (suspect)
  │                               │
  │  PING                        │
  │──────────────────────────── >│
  │                               │ (no response)
  │  ... cluster-node-timeout ... │
  │  (default 15,000 ms)         │
  │                               │
  │  [Node A marks X as PFAIL]   │
  │  (local opinion only)        │
```

- **PFAIL is subjective**: it is one node's local opinion that another node might be down.
- PFAIL status is included in gossip messages so other nodes learn about it.
- PFAIL alone does NOT trigger failover.

### Phase 2: FAIL (Confirmed Failure)

```
Node A (marks X as PFAIL)
  │
  │  Gossip includes "X is PFAIL"
  │─────────────> Node B (also marks X as PFAIL via its own timeout)
  │─────────────> Node C (also marks X as PFAIL)
  │─────────────> Node D (leader, sees majority PFAIL)
  │
  │  Node D checks: majority of leaders report X as PFAIL?
  │  Leaders: A=PFAIL, B=PFAIL, C=PFAIL, D=PFAIL  →  YES
  │
  │  Node D promotes X from PFAIL → FAIL
  │  Broadcasts FAIL message to entire cluster
  │
  │  [All nodes now know X is FAIL]
  │  [Failover process begins for X's followers]
```

### Failure Detection Parameters

| Parameter                | Default   | Description                                      |
|--------------------------|-----------|--------------------------------------------------|
| `cluster-node-timeout`   | 15,000 ms | Time before a non-responding node is marked PFAIL|
| Majority threshold       | N/2 + 1   | Number of leaders that must agree on PFAIL       |

### PFAIL vs FAIL

| Property        | PFAIL                          | FAIL                                    |
|-----------------|--------------------------------|-----------------------------------------|
| Scope           | Local (one node's opinion)     | Global (cluster-wide consensus)         |
| Trigger         | No PONG within timeout         | Majority of leaders report PFAIL        |
| Propagation     | Via gossip (piggybacked)       | Via dedicated FAIL broadcast            |
| Triggers failover? | No                          | Yes                                     |
| Cleared when    | Node responds to PING again    | Node rejoins and is reachable           |

### Failure Detection Timeline (Typical)

```
T = 0s      Node X stops responding
T = 15s     Nodes that pinged X recently mark it PFAIL (cluster-node-timeout)
T = 15–20s  PFAIL propagates via gossip to other leaders
T = 20–25s  Majority of leaders have PFAIL reports → promoted to FAIL
T = 25–30s  Follower election begins
T = 30–35s  New leader elected, starts accepting writes

Total failover time: ~15–35 seconds with default settings
```

Lowering `cluster-node-timeout` speeds up detection but increases false positives during network hiccups.

---

## 8. Follower Election and Promotion

When a leader is marked FAIL, its followers compete to become the new leader.

### Election Protocol

```
  Follower X1              Follower X2              Leaders (A, B, C)
  (offset: 1,000,500)     (offset: 1,000,200)
       │                       │                         │
       │  [Detects leader X    │                         │
       │   marked as FAIL]     │                         │
       │                       │                         │
       │  [Calculates delay:   │                         │
       │   500ms base          │                         │
       │   + 0ms (highest      │  [Calculates delay:     │
       │     offset)]          │   500ms base             │
       │                       │   + rank*1000ms          │
       │                       │   = 1500ms]              │
       │                       │                         │
       │  FAILOVER_AUTH_REQUEST│                         │
       │  (after 500ms)        │                         │
       │─────────────────────────────────────────────── >│
       │                       │                         │
       │                       │                         │ [Each leader votes
       │                       │                         │  for first valid
       │                       │                         │  request per epoch]
       │                       │                         │
       │  FAILOVER_AUTH_ACK    │                         │
       │  (from A)             │                         │
       │<───────────────────────────────────────────────│
       │  FAILOVER_AUTH_ACK    │                         │
       │  (from B)             │                         │
       │<───────────────────────────────────────────────│
       │                       │                         │
       │  [X1 has majority     │                         │
       │   (2 of 3 leaders)]   │                         │
       │                       │                         │
       │  [X1 promotes itself: │                         │
       │   increments configEpoch,                       │
       │   takes slot ownership,│                        │
       │   broadcasts UPDATE]   │                        │
       │                       │                         │
       │  X2 reconfigures as   │                         │
       │  follower of X1       │                         │
```

### Delay Calculation

The delay ensures the follower with the most data wins:

```
delay = 500ms + random(0, 500ms) + (FOLLOWER_RANK * 1000ms)

Where:
  FOLLOWER_RANK = 0 for the follower with highest replication offset
                  1 for second highest
                  2 for third highest
                  ...
```

| Follower | Replication Offset | Rank | Delay (approx)   |
|----------|--------------------|------|-------------------|
| X1       | 1,000,500          | 0    | 500–1,000 ms      |
| X2       | 1,000,200          | 1    | 1,500–2,000 ms    |
| X3       | 999,800            | 2    | 2,500–3,000 ms    |

X1 sends its request first, collects votes before X2 even asks.

### Majority Requirement

A follower needs votes from **more than half** of the reachable leader nodes:

| Leaders in cluster | Votes needed | Can tolerate failures |
|--------------------|-------------|------------------------|
| 3                  | 2           | 1 leader down          |
| 5                  | 3           | 2 leaders down         |
| 7                  | 4           | 3 leaders down         |

### After Promotion

The winning follower:

1. Increments its `configEpoch` to a new unique value (higher than any seen)
2. Updates its slot bitmap to claim the failed leader's slots
3. Broadcasts an UPDATE message to all nodes
4. Starts accepting client writes for those slots
5. Other followers of the old leader reconfigure to replicate from the new leader

---

## 9. Epoch-Based Configuration

Epochs are logical clocks that resolve conflicts in a distributed cluster without a central coordinator.

### Two Types of Epoch

| Epoch          | Scope          | Purpose                                        | Who increments it       |
|----------------|----------------|------------------------------------------------|-------------------------|
| `currentEpoch` | Cluster-wide   | Global logical clock, monotonically increasing | Any node during failover|
| `configEpoch`  | Per-node       | When this node last acquired its slot ownership| The node itself         |

### How Epochs Prevent Split-Brain

Consider a network partition where two followers of the same leader both try to become leader:

```
Partition 1                    Partition 2
┌─────────────────────┐       ┌─────────────────────┐
│ Leader A  (healthy) │       │ Leader B  (healthy) │
│ Leader C  (healthy) │       │ Follower X1 (of X)  │
│ Follower X2 (of X)  │       │                     │
│                     │       │ Leader X is FAIL     │
└─────────────────────┘       └─────────────────────┘

Partition 2: X1 wants to become leader of X's slots
  - X1 sends FAILOVER_AUTH_REQUEST
  - Only Leader B can vote (only leader in this partition)
  - B votes YES, but 1 vote < majority (need 2 of 3 leaders)
  - X1 CANNOT promote itself → no split-brain

Partition 1: X2 wants to become leader of X's slots
  - X2 sends FAILOVER_AUTH_REQUEST
  - Leaders A and C can vote → 2 votes = majority
  - X2 promotes itself successfully with configEpoch = currentEpoch + 1
```

### Conflict Resolution

If two nodes somehow both claim the same slot (should not happen, but can occur with manual intervention):

```
Node P claims slot 7438 with configEpoch = 5
Node Q claims slot 7438 with configEpoch = 8

Resolution: Q wins (higher configEpoch)

All nodes update: slot 7438 → Q
```

The rule is simple: **highest configEpoch wins**. This is unambiguous because:
- Each configEpoch is unique (guaranteed by the voting protocol)
- configEpoch only increases
- All nodes converge to the same answer

### Epoch Progression Example

```
Time    Event                           currentEpoch    configEpoch
────    ─────                           ────────────    ───────────
T0      Cluster starts                  1               A=1, B=1, C=1
T1      Node D added, takes slots       2               D=2
T2      Leader A fails                  2
T3      Follower A1 elected             3               A1=3
T4      Leader B fails                  3
T5      Follower B1 elected             4               B1=4
```

---

## 10. Replica Migration

Replica migration is an automatic rebalancing feature that ensures every leader has at least one follower for high availability.

### The Problem

```
BEFORE: Leader A loses both followers (hardware failure in same rack)

  Leader A         Leader B         Leader C
  slots 0–5460    slots 5461–10922 slots 10923–16383
     │                │                │
  (none!)          ┌──┴──┐          ┌──┴──┐
                   B1    B2         C1    C2

  Leader A has ZERO followers → if A fails, those slots are LOST
```

### Automatic Replica Migration

```
AFTER: Replica migration kicks in

  Leader A         Leader B         Leader C
  slots 0–5460    slots 5461–10922 slots 10923–16383
     │                │                │
     B2            ┌──┘             ┌──┴──┐
  (migrated!)      B1              C1    C2

  B2 automatically detaches from B and reattaches to A
  B still has B1 (satisfies cluster-migration-barrier = 1)
  A now has a follower again
```

### Configuration

| Parameter                    | Default | Meaning                                             |
|------------------------------|---------|-----------------------------------------------------|
| `cluster-migration-barrier`  | 1       | A leader must retain at least this many followers    |
|                              |         | before one of its followers can migrate away         |

With the default of 1:
- A leader with 2 followers can donate 1 (keeps 1)
- A leader with 1 follower cannot donate (would drop to 0)
- A leader with 3 followers can donate up to 2

### Selection Logic

When multiple followers from multiple leaders are available to migrate:
1. The cluster picks a follower from the leader with the **most followers** (to balance the count)
2. Among tied leaders, the follower with the lowest node ID is chosen (deterministic)

---

## 11. Limitations

### Operational Constraints

| Limitation                          | Detail                                                            |
|-------------------------------------|-------------------------------------------------------------------|
| No multi-key ops across slots       | MGET, MSET, SUNION, etc. fail with CROSSSLOT unless hash tags used|
| Only DB 0                           | SELECT command is disabled; no multi-database support              |
| KEYS scans local node only          | Must run on every node to see all keys (use SCAN instead)         |
| Max ~1,000 nodes                    | Gossip bandwidth becomes excessive beyond this                    |
| Cluster protocol overhead           | Extra 2 KB per gossip message; bus port required                  |
| Cross-slot transactions             | MULTI/EXEC only works within a single slot (use hash tags)        |
| Pub/Sub scope                       | PUBLISH in Cluster is broadcast to all nodes (bandwidth cost)     |
| Lua script key access               | All keys accessed by a script must be in the same slot            |
| No atomic cross-slot operations     | Cannot atomically move data between slots                         |

### Things That Work Differently

| Feature           | Standalone Redis                | Redis Cluster                         |
|-------------------|---------------------------------|---------------------------------------|
| SELECT            | 16 databases (0–15)            | DB 0 only                             |
| KEYS              | Scans entire keyspace          | Scans local node's keyspace only      |
| SCAN              | Full keyspace                  | Local node only (use each node)       |
| CONFIG            | Single node                    | Must run on each node separately      |
| FLUSHALL          | Clears all DBs                 | Clears only the local node's data     |
| Replication       | Configurable                   | Automatic within shard                |

---

## 12. Contrast with Memcached

Memcached has no server-side clustering. The "cluster" is entirely a client-side concept.

### Architecture Comparison

```
Redis Cluster                              Memcached "Cluster"
─────────────                              ────────────────────

┌───────────────────────────┐              ┌───────────────────────────┐
│        Client             │              │        Client             │
│  ┌──────────────────┐     │              │  ┌──────────────────┐     │
│  │ Smart routing:   │     │              │  │ Consistent hash: │     │
│  │ Slot→Node map    │     │              │  │ Ketama algorithm │     │
│  │ (from server)    │     │              │  │ (client-only)    │     │
│  └────────┬─────────┘     │              │  └────────┬─────────┘     │
│           │               │              │           │               │
└───────────┼───────────────┘              └───────────┼───────────────┘
            │                                          │
   ┌────────┼────────┐                        ┌────────┼────────┐
   │        │        │                        │        │        │
┌──┴──┐  ┌──┴──┐  ┌──┴──┐              ┌──┴──┐  ┌──┴──┐  ┌──┴──┐
│ N1  │←→│ N2  │←→│ N3  │              │ M1  │  │ M2  │  │ M3  │
│     │←→│     │←→│     │              │     │  │     │  │     │
└─────┘  └─────┘  └─────┘              └─────┘  └─────┘  └─────┘
 Servers communicate                    Servers are ISOLATED
 via gossip protocol                    No inter-server communication
```

### Feature-by-Feature Comparison

| Feature                    | Redis Cluster                        | Memcached                           |
|----------------------------|--------------------------------------|-------------------------------------|
| Partitioning               | Server-side hash slots (16,384)      | Client-side consistent hashing      |
| Hashing algorithm          | CRC16 mod 16384                      | Ketama (MD5-based, 150 vnodes/server)|
| Server awareness           | Nodes know about each other          | Servers are completely independent   |
| Gossip protocol            | Yes (cluster bus)                    | None                                |
| Automatic failover         | Yes (follower election)              | None — client must handle failures  |
| Live resharding            | Yes (MIGRATE protocol)              | No — add/remove causes rehashing    |
| Data migration on scale    | Explicit, key-by-key migration       | None — ~1/N keys rehashed (cache miss)|
| Replication                | Built-in async replication           | None built-in                       |
| Multi-key operations       | Within same slot (hash tags)         | Client batches per server           |
| Protocol overhead          | Gossip bus, MOVED/ASK redirects      | Zero inter-server overhead          |
| Complexity                 | High                                 | Very low                            |
| Max practical cluster size | ~1,000 nodes                         | Unlimited (no coordination)         |

### What Happens When Adding a Server

**Redis Cluster**: Controlled resharding. Specific slots are migrated key-by-key from existing nodes to the new node. Zero data loss.

**Memcached**: Client updates its server list. The consistent hash ring changes, causing ~1/N of keys to map to different servers. Those keys are cache misses (data is not migrated — it is simply lost from the old server's perspective).

```
Memcached: Adding 4th server to a 3-server cluster

Before: 3 servers, each owns ~33% of keyspace
After:  4 servers, each owns ~25% of keyspace
Result: ~25% of keys (1/4) now hash to the wrong server → cache misses
        No data is moved — clients simply start writing to the new locations
```

---

## 13. Scaling Decisions and Trade-offs

### Hash Slots vs Consistent Hashing

| Criterion                    | Hash Slots (Redis)                        | Consistent Hashing (Memcached/Dynamo)     |
|------------------------------|-------------------------------------------|-------------------------------------------|
| Slot assignment              | Explicit, admin-controlled                | Automatic based on hash position          |
| Migration granularity        | Per-slot (fine-grained)                   | Per-range (depends on vnode placement)    |
| Deterministic migration      | Yes — you choose exactly which slots move | Partially — depends on hash ring changes  |
| Virtual nodes needed?        | No                                        | Yes (for balanced distribution)           |
| Metadata size                | 2 KB bitmap per node                      | O(V*N) where V=vnodes, N=nodes           |
| Rebalancing control          | Move specific slots to specific nodes     | Add/remove vnodes, hope for balance       |
| Implementation complexity    | Moderate                                  | Lower                                     |

**Why Redis chose hash slots**: explicit control. An operator can decide exactly which slots go where, move them one at a time, and know precisely which data is being migrated. With consistent hashing, adding a node changes the hash ring and you have less control over what moves where.

### Gossip vs Centralized Coordinator (ZooKeeper/etcd)

| Criterion                    | Gossip (Redis Cluster)                    | Centralized (ZooKeeper/etcd)              |
|------------------------------|-------------------------------------------|-------------------------------------------|
| Single point of failure      | None                                      | Coordinator is SPOF (mitigated by quorum) |
| Deployment complexity        | Just Redis nodes                          | Redis + separate ZK/etcd cluster          |
| Convergence speed            | O(log N) rounds (~seconds)                | Immediate (single source of truth)        |
| Split-brain risk             | Mitigated by epoch voting                 | Mitigated by leader election              |
| Consistency model            | Eventually consistent metadata            | Strongly consistent metadata              |
| Operational overhead         | Lower                                     | Higher (maintain coordinator cluster)     |
| Scalability                  | ~1,000 nodes (gossip limit)               | Depends on coordinator capacity           |

**Why Redis chose gossip**: simplicity of deployment and no external dependencies. A Redis Cluster is self-contained — you do not need to set up, monitor, and maintain a separate coordination service. The trade-off is slower convergence and a practical node limit of ~1,000.

### Async Replication in Cluster

Redis Cluster uses asynchronous replication (same as standalone Redis). This means:

```
Client          Leader          Follower
  │               │                │
  │  SET key val  │                │
  │──────────────>│                │
  │               │                │
  │  OK           │                │
  │<──────────────│                │
  │               │                │
  │               │  Replicate     │
  │               │  (async)       │
  │               │───────────────>│
  │               │                │

  If leader crashes HERE, the write is lost.
  The follower has not received it yet.
```

**Why async?**

| Replication Model | Latency Impact        | Data Safety               | Use Case Fit               |
|-------------------|-----------------------|---------------------------|----------------------------|
| Synchronous       | +1 RTT per write      | No data loss on failover  | Financial transactions     |
| Asynchronous      | No added latency      | Small window of data loss | Cache, session store, counters |

Redis targets the cache and data-structure-store use case, where **low latency matters more than zero data loss**. A cache miss after failover is acceptable; a 2x latency penalty on every write is not.

The WAIT command provides optional synchronous behavior:
```
SET key value
WAIT 1 5000    # Wait for at least 1 follower to ACK, timeout 5 seconds
```
But WAIT does not make the write durable against all failure scenarios — the follower might still lose the data if it crashes before persisting.

---

## Summary: Redis Cluster at a Glance

```
┌──────────────────────────────────────────────────────────────────┐
│                     Redis Cluster Architecture                    │
├──────────────────────────────────────────────────────────────────┤
│                                                                  │
│  Partitioning:    16,384 hash slots, CRC16 mod 16384            │
│  Topology:        Leaders + Followers, gossip-connected          │
│  Client routing:  MOVED (permanent) / ASK (temporary)           │
│  Co-location:     Hash tags {tag} for multi-key ops             │
│  Resharding:      Live, per-key MIGRATE, zero downtime          │
│  Failure detect:  PFAIL (local) → FAIL (majority consensus)     │
│  Failover:        Follower election, majority vote, ~15-35s     │
│  Consistency:     Epoch-based, highest configEpoch wins          │
│  Replication:     Async (configurable with WAIT)                │
│  Replica safety:  Automatic replica migration                   │
│  Max cluster:     ~1,000 nodes                                  │
│                                                                  │
└──────────────────────────────────────────────────────────────────┘
```
