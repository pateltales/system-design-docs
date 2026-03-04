# System Design Interview Simulation: Design a Distributed Cache (Redis)

> **Interviewer:** Principal Engineer (L8)
> **Candidate Level:** SDE-3 (L6 — Senior Software Development Engineer)
> **Duration:** ~60 minutes
> **Date:** February 19, 2026

---

## PHASE 1: Opening & Problem Statement (~2 min)

**Interviewer:**
Hey, thanks for coming in. I'm a Principal Engineer and I've spent the last several years working on distributed storage systems. For today's system design round, I'd like you to design a **distributed in-memory data store** — think Redis. A system where clients can store and retrieve data with sub-millisecond latency, supporting rich data structures beyond simple key-value strings.

I'm interested in how you think about the architecture holistically — the threading model, persistence, replication, and how to scale it horizontally. I'll push back on your decisions to understand your reasoning, not to signal you're wrong.

Take it away.

---

## PHASE 2: Requirements Gathering & Scoping (~5 min)

**Candidate:**
Thanks! A distributed in-memory data store is a rich problem space — it can be a volatile cache, a durable data store, a message broker, or all three. Let me scope this down before drawing anything.

**Functional Requirements — what operations do we need?**

> "The core operations I'd expect for an in-memory data store:
> - **GET / SET** — basic key-value operations with sub-millisecond latency
> - **Rich data structures** — not just strings, but Lists, Sets, Sorted Sets, Hashes. This is what differentiates Redis from a simple cache like Memcached — it's a *data structure server*.
> - **Expiry (TTL)** — keys should auto-expire after a configurable time. Essential for caching use cases.
> - **Persistence** — optionally durable. Some users want pure volatile cache (like Memcached), others want data to survive restarts.
> - **Pub/Sub** — publish/subscribe messaging for real-time communication.
> - **Atomic operations** — INCR, LPUSH, ZADD should be atomic. Transactions (MULTI/EXEC) and scripting (Lua/EVAL) for multi-step atomicity.
>
> A few clarifying questions:
> - **What's the primary use case — pure cache or durable data store?**"

**Interviewer:** "Both. Some users treat it as a volatile cache in front of a database. Others use it as a primary data store for things like session data, rate limiting, or leaderboards. Your design should support both modes."

> "- **Do we need to support the full Redis command set, or a subset?**"

**Interviewer:** "Focus on the architecture. The command set matters for the API design phase, but architecturally, the key questions are: how do you store data in memory, how do you persist it, how do you replicate it, and how do you shard it."

> "- **What consistency model? Strong consistency or eventual consistency?**"

**Interviewer:** "Great question. What does Redis actually do?"

> "Redis uses **asynchronous replication** by default — writes go to the leader, and the leader asynchronously replicates to followers. So reads from followers are eventually consistent. There's a WAIT command that provides best-effort synchronous replication — it blocks until N followers acknowledge the write — but even WAIT isn't true strong consistency because if the leader crashes between the write and the replication, the acknowledged write can be lost.
>
> Redis Cluster explicitly states it does **not** provide strong consistency guarantees. It favors availability and partition tolerance — AP in CAP terms."

**Interviewer:** Good — you clearly know the system. Let's get to the non-functional requirements.

**Non-Functional Requirements:**

> "These define what makes an in-memory data store valuable:
>
> | Dimension | Requirement | Rationale |
> |---|---|---|
> | **Latency** | Sub-millisecond for reads and writes | The entire value proposition — if latency were acceptable at 10ms, we'd just use a database |
> | **Throughput** | ~180K ops/sec per node (single-threaded, no pipelining); ~1.5M+ ops/sec with pipelining | Verified Redis benchmark numbers |
> | **Memory** | Hundreds of GB per node (bounded by machine RAM) | In-memory store — data must fit in RAM |
> | **Availability** | 99.99%+ with automatic failover | Typically sits on the critical path — cache miss = database storm |
> | **Durability** | Configurable: none (pure cache) to ~1 second data loss (AOF everysec) | Users choose their durability/performance tradeoff |
> | **Scalability** | Cluster scales to ~1,000 nodes | Must handle datasets larger than a single machine's memory |
> | **Data model** | Rich data structures: Strings, Lists, Sets, Sorted Sets, Hashes, Streams, HyperLogLog, Bitmaps, Geospatial | Not just a key-value store — a data structure server |

**Interviewer:** You mentioned 180K ops/sec single-threaded. Why single-threaded?

**Candidate:**

> "That's a fundamental design decision that we'll dive into. The short answer: for an in-memory data store, **CPU is not the bottleneck — network I/O and memory are**. Data structure operations like hash table lookups and skiplist insertions are O(1) or O(log N), completing in nanoseconds. The expensive part is reading from the network socket and writing back the response.
>
> Going single-threaded eliminates all lock contention, deadlocks, and context switching overhead. It gives you **predictable, deterministic latency** — which is exactly what you want from a cache. Redis's creator, Antirez, explicitly chose simplicity over raw multi-core throughput.
>
> Memcached took the opposite approach — multi-threaded with a global lock (later fine-grained per-slab locking). It scales CPU better but at the cost of complexity, lock contention, and harder debugging."

---

### L5 vs L6 vs L7 — Phase 2 Rubric

| Aspect | L5 (SDE-2) | L6 (SDE-3) | L7 (Principal) |
|---|---|---|---|
| **Functional Reqs** | Lists GET/SET, mentions TTL | Proactively distinguishes "data structure server" from "cache", raises Pub/Sub, transactions, scripting | Additionally discusses Streams, client-side caching, modules, and when Redis is NOT the right choice |
| **Non-Functional** | Mentions low latency and high availability | Quantifies throughput (180K ops/sec), explains single-threaded rationale, knows consistency model (AP) | Frames NFRs in terms of failure modes: what happens during cache stampede, what's the blast radius of a leader failure |
| **Scoping** | Accepts problem as given | Drives clarifying questions about cache vs durable store, consistency model | Negotiates scope: "In 60 minutes, I'll focus on the core architecture and iteratively build up. I'll defer cross-datacenter replication and security to supporting docs." |
| **Redis vs Memcached** | Doesn't differentiate | Contrasts design philosophies: single-threaded vs multi-threaded, data structures vs strings only | Discusses why Redis "won" — network effects, richer use cases, Antirez's philosophy |

---

## PHASE 3: API Design (~3 min)

**Candidate:**

> "Before diving into architecture, let me briefly cover the API surface. Redis uses the **RESP (Redis Serialization Protocol)** — a text-based, human-readable wire protocol over TCP on port 6379.
>
> **Why text-based?** You can literally `telnet localhost 6379` and type commands. Antirez valued debuggability and ecosystem growth over the marginal bandwidth savings of a binary protocol. RESP3 (Redis 6.0+) adds richer type responses (maps, sets, booleans) while staying text-based.
>
> **Core commands for the interview** (full list in [02-api-contracts.md](02-api-contracts.md)):
>
> | Category | Key Commands | Why It Matters |
> |---|---|---|
> | **Strings** | `GET key`, `SET key value [EX seconds] [NX\|XX]` | Basic KV — but SET has conditional flags (NX = set-if-not-exists, XX = set-if-exists) making it useful for distributed locks |
> | **Lists** | `LPUSH`, `RPUSH`, `LPOP`, `RPOP`, `LRANGE` | Queues, stacks, capped collections |
> | **Sets** | `SADD`, `SMEMBERS`, `SINTER`, `SUNION` | Tags, unique visitors, set operations |
> | **Sorted Sets** | `ZADD key score member`, `ZRANGE`, `ZRANK` | Leaderboards, priority queues, time-series windows |
> | **Hashes** | `HSET key field value`, `HGET`, `HGETALL` | Object-like storage (user profiles, sessions) |
> | **TTL** | `EXPIRE key seconds`, `TTL key` | Cache semantics — auto-cleanup |
> | **Pub/Sub** | `SUBSCRIBE channel`, `PUBLISH channel message` | Real-time messaging |
> | **Transactions** | `MULTI`, `EXEC`, `WATCH` | Optimistic locking (not ACID like SQL — no rollback) |
> | **Scripting** | `EVAL script numkeys keys args` | Atomic multi-step operations via Lua |
> | **Replication** | `WAIT numreplicas timeout` | Best-effort synchronous replication |
> | **Cluster** | `CLUSTER SLOTS`, `-MOVED slot ip:port` | Cluster topology and redirection |
>
> **Contrast with Memcached's API**: Memcached has ~15 commands total: get, set, add, replace, delete, incr, decr, cas, touch. No data structure operations, no scripting, no transactions, no pub/sub. This simplicity is intentional — Memcached is a focused cache. Redis's rich API is why it's used as more than a cache."

**Interviewer:** Good. Let's build the architecture now — start simple and evolve it.

---

### L5 vs L6 vs L7 — Phase 3 Rubric

| Aspect | L5 (SDE-2) | L6 (SDE-3) | L7 (Principal) |
|---|---|---|---|
| **Protocol** | "It uses TCP" | Explains RESP, why text-based (debuggability), RESP3 evolution | Discusses protocol versioning strategy, backward compatibility, inline vs multibulk format |
| **API breadth** | Lists GET/SET | Covers data structures, TTL, transactions, scripting with "why" for each | Discusses API design tradeoffs: why WATCH-based optimistic locking instead of pessimistic, why Lua over stored procedures |
| **Memcached contrast** | Doesn't mention | Notes ~15 commands vs Redis's 400+ | Explains how API richness drove Redis's adoption: more use cases → more community → more tooling |

---

## PHASE 4: High-Level Architecture — Iterative Build-Up (~15 min)

**Candidate:**

> "Let me start with the simplest possible thing and iteratively fix the problems. This is how Redis actually evolved historically."

### Attempt 0: Single-Threaded In-Memory Hash Map

> "The simplest in-memory data store: a process with a hash map in memory.
>
> ```
>   ┌──────────────────────────┐
>   │     Single Process       │
>   │                          │
>   │   HashMap<String,String> │
>   │                          │
>   │   put("user:1", "Alice") │
>   │   get("user:1") → Alice  │
>   └──────────────────────────┘
> ```
>
> This is O(1) lookups, microsecond latency. But it has three fatal problems:
>
> 1. **No network access** — only the local process can use it
> 2. **No persistence** — data lost on restart
> 3. **Only strings** — no rich data structures
>
> Let's fix these one by one."

---

### Attempt 1: Add Network Protocol + Rich Data Structures

> "Let me make this accessible over the network and add the data structures that make Redis useful.
>
> ```
>   Clients (thousands of concurrent connections)
>     │  │  │  │  │
>     ▼  ▼  ▼  ▼  ▼
>   ┌──────────────────────────────┐
>   │   TCP Server (port 6379)     │
>   │   RESP Protocol              │
>   │                              │
>   │   Single-Threaded Event Loop │
>   │   (ae library: epoll/kqueue) │
>   │                              │
>   │   ┌────────────────────────┐ │
>   │   │ Data Structures:       │ │
>   │   │ • Strings (SDS)        │ │
>   │   │ • Lists (quicklist)    │ │
>   │   │ • Sets (intset/HT)    │ │
>   │   │ • Sorted Sets (skip+HT)│ │
>   │   │ • Hashes (listpack/HT)│ │
>   │   └────────────────────────┘ │
>   └──────────────────────────────┘
> ```
>
> **Key design decisions:**
>
> **1. Single-threaded event loop (not multi-threaded):**
> Redis uses its own event library called `ae`, which wraps the OS I/O multiplexer — epoll on Linux, kqueue on macOS/BSD. A single thread handles all client connections and all command execution. No threads = no locks = no deadlocks = predictable latency.
>
> Benchmark: ~180K GET/SET ops/sec single-threaded. With 16-command pipelining: ~1.5M+ ops/sec. Redis can handle 60,000+ concurrent connections.
>
> **Why not multi-threaded like Memcached?** Memcached uses a multi-threaded architecture (default 4 worker threads via `-t` flag). A main listener thread accepts connections and dispatches to workers. This scales CPU cores better but adds lock contention and debugging complexity. Redis chose simplicity — and for an in-memory store where operations take nanoseconds, the network I/O is the bottleneck, not CPU.
>
> Redis 6.0+ added I/O threads (`io-threads` config) to parallelize network read/write, but command execution still happens on a single thread.
>
> **2. RESP protocol (text-based, not binary):**
> Human-readable wire protocol. Client sends `*3\r\n$3\r\nSET\r\n$5\r\nmykey\r\n$5\r\nhello\r\n` and server responds `+OK\r\n`. You can debug with telnet. Antirez valued debuggability over marginal bandwidth savings.
>
> **3. Memory-efficient data structure encodings:**
> Small collections use compact encodings (listpack — a contiguous byte array) to save memory. Above configurable thresholds, they switch to full data structures (hash tables, skiplists). This optimizes for the common case — most keys have small collections."

**Interviewer:** OK, this solves network access and data structures. What's still broken?

**Candidate:**

> "Three problems:
> 1. **No persistence** — if the process crashes or restarts, all data is lost
> 2. **Single machine memory limit** — can only store as much as one machine's RAM
> 3. **Single point of failure** — one server dies, the service is down"

---

### Attempt 2: Add Persistence

> "Now I need data to survive restarts. Redis offers two persistence mechanisms, and I'd argue this is one of its most elegant design decisions.
>
> ```
>   ┌──────────────────────────────────────────┐
>   │   Redis Server (single-threaded)          │
>   │                                           │
>   │   In-Memory Dataset                       │
>   │   ┌─────────────────────┐                 │
>   │   │ All keys & values   │                 │
>   │   └────────┬────────────┘                 │
>   │            │                              │
>   │     ┌──────┴──────┐                       │
>   │     │             │                       │
>   │     ▼             ▼                       │
>   │  ┌──────┐    ┌──────────┐                 │
>   │  │ RDB  │    │   AOF    │                 │
>   │  │Snap- │    │(Append-  │                 │
>   │  │shot  │    │ Only     │                 │
>   │  │      │    │ File)    │                 │
>   │  │fork()│    │          │                 │
>   │  │+ COW │    │appendfsync│                │
>   │  └──┬───┘    └────┬─────┘                 │
>   │     │             │                       │
>   └─────│─────────────│───────────────────────┘
>         ▼             ▼
>    dump.rdb      appendonly.aof
>    (on disk)     (on disk)
> ```
>
> **RDB Snapshots (point-in-time snapshots):**
> - `BGSAVE` triggers `fork()` — creates a child process that shares the parent's memory pages via copy-on-write (COW)
> - Child writes the entire dataset to disk as a compact binary file (`.rdb`). Parent continues serving requests uninterrupted
> - Default save triggers (Redis 7.0+): `save 3600 1 300 100 60 10000` — save after 3600s if ≥1 key changed, after 300s if ≥100 keys changed, after 60s if ≥10000 keys changed
> - **Brilliant part**: fork() + COW gives you a consistent point-in-time snapshot without stopping writes. The OS handles memory page duplication transparently
> - **Trade-off**: worst case 2x memory during fork if every page is modified. Redis recommends disabling Linux Transparent Huge Pages (THP) — THP amplifies COW overhead from 4 KB pages to 2 MB pages
>
> **AOF (Append-Only File):**
> - Every write command is appended to a log file. On restart, Redis replays the log to reconstruct state
> - Three `appendfsync` policies:
>   - `always` — fsync every write. Safest (~0 data loss), but ~10x slower
>   - `everysec` (default) — fsync once per second. Max 1 second of data loss. Good balance
>   - `no` — let the OS flush when it wants. Fastest, but OS crash = up to 30 seconds of data loss
> - AOF rewrite: background process rewrites AOF to minimal representation. Triggers: `auto-aof-rewrite-percentage 100`, `auto-aof-rewrite-min-size 64mb`
>
> **RDB + AOF Hybrid (Redis 4.0+):**
> - `aof-use-rdb-preamble yes` (introduced Redis 4.0, default `yes` since 7.0). AOF rewrite produces an RDB-format header + incremental AOF commands appended after
> - On recovery: load RDB portion (fast, binary), then replay AOF tail (minimal commands). Best of both worlds
>
> **Redis 7.0+: Multi-part AOF** — AOF split into base file + incremental files tracked by a manifest. Cleaner file management, atomic replacement during rewrite.
>
> **Why both RDB and AOF?**
>
> | Dimension | RDB | AOF |
> |---|---|---|
> | **Data loss** | Minutes (between snapshots) | ≤1 second (with `everysec`) |
> | **Recovery speed** | Fast (load binary, no replay) | Slower (replay all commands) |
> | **File size** | Compact (~10x smaller) | Larger (command log) |
> | **Good for** | Backups, disaster recovery | Minimal data loss |
> | **CPU cost** | fork() overhead | Minimal (append-only) |
>
> The hybrid approach gives you the fast recovery of RDB with the minimal data loss of AOF."

**Interviewer:** How does this compare to Memcached's approach?

**Candidate:**

> "Memcached has **no persistence at all**. Restart = cold cache. Every single cached value must be repopulated from the backing data store. This is the fundamental philosophical difference:
>
> - **Memcached** = pure volatile cache. Data is ephemeral by design. Simple, focused.
> - **Redis** = 'a database that happens to be in memory.' Persistence, replication, rich data structures make it usable as a primary data store for certain workloads (sessions, rate limiting, leaderboards).
>
> Neither is wrong — they're different tools for different philosophies."

**Interviewer:** Good. What's still broken?

**Candidate:**

> "Three problems remain:
> 1. **Single point of failure** — if this server dies, the service is down until we restart and replay
> 2. **Single machine memory limit** — can't store more data than one machine's RAM
> 3. **Can't scale reads** — all reads hit the one server"

---

### Attempt 3: Add Replication (Leader-Follower) + High Availability

> "I need redundancy. Let me add replicas and automatic failover.
>
> ```
>   Clients (writes)          Clients (reads)
>       │                     │  │  │
>       ▼                     ▼  ▼  ▼
>   ┌────────────┐     ┌──────────────┐  ┌──────────────┐
>   │   Leader   │────▶│  Follower 1  │  │  Follower 2  │
>   │ (read/write│     │  (read-only) │  │  (read-only) │
>   │  all writes│     │              │  │              │
>   │  go here)  │────▶│  async       │  │  async       │
>   └────────────┘     │  replication │  │  replication │
>         │            └──────────────┘  └──────────────┘
>         │
>   ┌─────┴──────┐
>   │  Sentinel  │ ← monitors leader, triggers failover
>   │  (3 nodes) │
>   └────────────┘
> ```
>
> **Asynchronous replication:**
> - Leader processes all writes and asynchronously streams them to followers
> - Followers are read-only by default and serve read traffic (eventually consistent)
> - **Full resync**: On first connect, leader triggers BGSAVE → streams RDB to follower → follower loads it → leader streams buffered writes accumulated during transfer
> - **Partial resync** (PSYNC2, Redis 4.0+): Uses a replication backlog (circular buffer, default `repl-backlog-size 1mb`) with replication ID + byte offset. If a follower disconnects briefly and the gap fits in the backlog, only the delta is sent. PSYNC2 introduced dual replication IDs so a promoted follower can partial-resync with other followers after failover.
>
> **WAIT command**: `WAIT 2 5000` — blocks until 2 followers acknowledge the write, or 5 seconds elapse. Provides best-effort synchronous replication. **Important caveat**: WAIT is NOT strong consistency. The leader acknowledges the write ("OK") immediately before replication. If the leader crashes before replication completes, that write is lost — even though the client already received "OK". WAIT is a separate command sent after the write, so there's always a gap where data can be lost.
>
> **Redis Sentinel for automatic failover:**
> - Separate processes (deploy ≥3 for quorum), default port 26379
> - **SDOWN (Subjective Down)**: One Sentinel thinks the leader is unreachable (no reply within `down-after-milliseconds`)
> - **ODOWN (Objective Down)**: Quorum of Sentinels agree the leader is down
> - Raft-like election among Sentinels. Winner picks the best follower (highest replication offset, lowest `replica-priority`), promotes it to leader, reconfigures other followers to replicate from the new leader
> - **Split-brain mitigation**: `min-replicas-to-write N` + `min-replicas-max-lag M` — the leader refuses writes if fewer than N replicas are reachable with lag < M seconds. This prevents a stale leader (on the minority side of a network partition) from accepting writes that will be lost."

**Interviewer:** What problems are left?

**Candidate:**

> "**Contrast with Memcached**: Memcached has no replication at all. Clients can write to multiple servers for redundancy, but there's no built-in mechanism. If a Memcached server dies, that data is gone — the application must repopulate from the database. This is acceptable for a pure volatile cache, but not for a data store.
>
> Two problems that we need to solve next:
> 1. **Single leader = write bottleneck** — all writes go through one server. Adding more replicas does NOT help writes — replicas are read-only copies
> 2. **Single machine memory limit** — the full dataset must fit on one machine. Replicas hold copies of the SAME data — they don't split it. If you have 200 GB of data and each machine has 64 GB of RAM, replication alone cannot help
>
> There's also a residual risk that persists regardless:
> 3. **Split-brain risk** — despite Sentinel, network partitions can still cause brief windows where two nodes think they're the leader. The `min-replicas-to-write` mitigation helps but doesn't eliminate it entirely"

---

### Attempt 4: Add Sharding (Redis Cluster)

> "The core problem with Attempt 3 is that we have ONE leader handling ALL the data and ALL the writes. Replicas only help with reads — they hold full copies of the SAME dataset. To solve both the write bottleneck and the memory limit, I need a fundamentally different approach: **sharding**.
>
> **What is sharding?** Instead of storing ALL keys on one leader, I split the keyspace into partitions and assign each partition to a DIFFERENT leader. Each leader is only responsible for a SUBSET of keys — not the full dataset.
>
> **Key insight:** Each shard is essentially its own mini Attempt 3 — a leader with its own followers, handling its own subset of the data. If I have 3 shards, I have 3 independent leader-follower groups, each holding roughly one-third of the keys. Writes are now distributed across 3 leaders instead of bottlenecked on 1. Memory is also distributed — 200 GB of data across 3 shards means ~67 GB per machine.
>
> Redis implements this as **Redis Cluster**.
>
> ```
>                     Clients
>                       │
>                       ▼
>              Smart Client Library
>              (maintains slot→node map)
>                 │     │     │
>          ┌──────┘     │     └──────┐
>          ▼            ▼            ▼
>   ┌─────────────┐ ┌────────────┐ ┌─────────────┐
>   │  Leader A   │ │  Leader B  │ │  Leader C   │
>   │ Slots 0-5460│ │Slots 5461- │ │Slots 10923- │
>   │             │ │   10922    │ │   16383     │
>   │             │ │            │ │             │
>   │  Follower   │ │  Follower  │ │  Follower   │
>   │    A'       │ │    B'      │ │    C'       │
>   └─────────────┘ └────────────┘ └─────────────┘
>         ◄──── Gossip Protocol (Cluster Bus) ────►
>              (port 16379 = data port + 10000)
> ```
>
> **Reading this diagram:** Each column (Leader A + Follower A', Leader B + Follower B', etc.) is one **shard** — an independent leader-follower group like what we built in Attempt 3. The difference is that instead of ONE group holding ALL data, we now have THREE groups, each holding a range of "hash slots" (explained below). Clients need to know which shard owns which keys — that's what the "Smart Client Library" does: it maintains a local map of slot→node assignments, computes which slot a key belongs to, and sends the command directly to the correct leader.
>
> **How it works:**
>
> **1. Hash slot model:** 16,384 hash slots. `CRC16(key) mod 16384` → slot → node.
>
> Key → `CRC16("user:1000")` = 7438 → slot 7438 → Leader B (owns slots 5461-10922)
>
> **Why 16,384 slots (not 65,536 or 4,096)?** Each node broadcasts its slot bitmap in gossip messages. 16,384 slots = 2 KB bitmap per node — reasonable overhead. 65,536 would be 8 KB per node — too much gossip traffic for large clusters. 16,384 supports up to ~1,000 nodes with ≥16 slots each.
>
> **2. Gossip protocol (Cluster Bus):** With multiple independent leader-follower groups, nodes need a way to discover each other and agree on who owns which slots. There's no central coordinator — instead, nodes periodically exchange state with each other (gossip), and eventually every node learns the full cluster topology. Nodes communicate on a dedicated port (data port + 10,000). Each node pings a random node every second, plus any node not pinged within `cluster-node-timeout / 2` (default 15,000 ms). Pings carry: node ID, IP, port, flags (leader/follower/PFAIL/FAIL), slot bitmap, current epoch, config epoch. Cluster state eventually converges.
>
> **3. Client-side redirection:**
> - `MOVED 7438 192.168.1.5:6379` — client sent command to wrong node. This is permanent — update the local slot map and retry.
> - `ASK 7438 192.168.1.5:6379` — slot is being migrated. This is temporary — send `ASKING` command to the target node, then retry. Only during migration.
>
> **4. Hash tags for multi-key operations:** Keys `{user:1000}.profile` and `{user:1000}.settings` hash to the same slot because CRC16 is computed only on the `{...}` content. This enables multi-key operations (MGET, transactions, Lua scripts) on co-located keys.
>
> **5. Resharding (live slot migration):** Source marks slot MIGRATING, target marks IMPORTING. Keys are moved atomically one-by-one with MIGRATE. Clients experience ASK redirections during migration. No downtime.
>
> **6. Failure detection & automatic failover:**
> - PFAIL: A node doesn't respond within `cluster-node-timeout` (default 15,000 ms) — the detecting node marks it as PFAIL (Probable Failure)
> - FAIL: Majority of leader nodes mark a node as PFAIL → promoted to FAIL → triggers automatic follower election
> - Followers of the failed leader hold an election (highest replication offset has priority). Requires majority of leaders to vote. Uses epoch-based config versioning.
>
> **Contrast with Memcached:** Memcached has no server-side clustering at all. Clients use consistent hashing (ketama algorithm) — servers are completely unaware of each other. Adding/removing a server: ~1/N keys need rehashing. No resharding, no slot migration, no failover, no gossip. Simpler but less capable."

**Interviewer:** What's the key tradeoff Redis Cluster makes?

**Candidate:**

> "The same fundamental tradeoff from Attempt 3 applies here, now multiplied across shards. **Availability over consistency.** Redis Cluster is AP in CAP terms:
>
> - Writes are acknowledged by the leader before replicating to followers (async replication)
> - If the leader fails after acknowledging a write but before replicating it, **that write is lost**
> - During a network partition, the partition with the majority of leaders continues operating. The minority side stops accepting writes after `cluster-node-timeout`
> - This is an intentional design choice: for a cache / data structure server, availability matters more than linearizability
>
> **What's still broken?**
>
> 1. **No strong consistency** — acknowledged writes can be lost during failover
> 2. **Hot key problem** — if one key gets disproportionate traffic, the shard owning it is overloaded
> 3. **Cross-slot limitations** — no multi-key operations across different slots (unless hash tags)
> 4. **Operational complexity** — clients must be 'cluster-aware' (smart clients)"

---

### Attempt 5: Production Hardening

> "The final layer — making this production-ready.
>
> **Memory management & eviction:**
> When `maxmemory` is reached, Redis needs an eviction policy. 8 policies available:
>
> | Policy | Evicts from | Strategy |
> |---|---|---|
> | `noeviction` | — | Return errors on writes (reads still work). Default. |
> | `allkeys-lru` | All keys | Approximated LRU (not true LRU — samples `maxmemory-samples` keys, default 5, evicts oldest) |
> | `allkeys-lfu` | All keys | Least Frequently Used (Redis 4.0+, logarithmic counter + decay) |
> | `allkeys-random` | All keys | Random eviction |
> | `volatile-lru` | Keys with TTL | LRU among keys with expiry set |
> | `volatile-lfu` | Keys with TTL | LFU among keys with expiry set |
> | `volatile-random` | Keys with TTL | Random among keys with expiry |
> | `volatile-ttl` | Keys with TTL | Nearest TTL expiry |
>
> **Why approximated LRU instead of true LRU?** True LRU requires a doubly-linked list with O(N) memory overhead. Redis samples a small number of keys and evicts the one with the oldest access time. With `maxmemory-samples 10`, the approximation is very close to true LRU. This is O(1) memory overhead.
>
> **TTL & expiry (dual mechanism):**
> - **Passive expiry (lazy deletion)**: On every key access, Redis checks if it's expired. If yes, delete and return "not found." Zero overhead for unaccessed keys.
> - **Active expiry (probabilistic sweep)**: Runs `hz` times/sec (default 10). Each cycle: sample 20 random keys with TTL, delete expired ones. If >25% were expired, repeat immediately. This cleans up unaccessed expired keys while bounding CPU usage.
> - **Replication of expiry**: Replicas do NOT independently expire keys. The leader generates DEL commands and replicates them. This ensures consistency — a key is either expired everywhere or nowhere.
>
> **Additional production features:**
> - **Pub/Sub**: Classic (fan-out to all cluster nodes) + Sharded Pub/Sub (Redis 7.0+, routed by channel hash slot)
> - **Lua scripting**: `EVAL`/`EVALSHA` for atomic multi-step operations. Runs on the single thread — long scripts block everything. Redis 7.0+ added Functions API.
> - **Client-side caching (Redis 6.0+)**: Server-assisted invalidation via `CLIENT TRACKING`. Dramatically reduces read load for hot keys.
> - **Security**: ACLs (Redis 6.0+) for per-user command/key restrictions. TLS support. AUTH.
> - **Monitoring**: `INFO` stats, `SLOWLOG` (default threshold 10ms), `LATENCY MONITOR`, `MEMORY DOCTOR`."

---

### Architecture Evolution Table

| Component | Attempt 0 | Attempt 1 | Attempt 2 | Attempt 3 | Attempt 4 | Attempt 5 |
|---|---|---|---|---|---|---|
| **Access** | Local only | TCP + RESP | TCP + RESP | TCP + RESP | TCP + RESP + Cluster | TCP + RESP + Cluster |
| **Data model** | HashMap<String,String> | Rich data structures (SDS, quicklist, skiplist, HT) | Same | Same | Same (per shard) | Same + Pub/Sub, Streams |
| **Threading** | Single thread | Single-threaded event loop (ae/epoll) | Same | Same | Same per node | Same + I/O threads (6.0+) |
| **Persistence** | None | None | RDB + AOF + Hybrid | Same | Same per shard | Same + tuning |
| **Replication** | None | None | None | Async leader-follower + Sentinel | Built-in per-slot replication | Same + WAIT |
| **Sharding** | N/A | N/A | N/A | N/A | 16384 hash slots + gossip | Same + resharding |
| **Memory mgmt** | Unbounded | Unbounded | Unbounded | Unbounded | Per-shard limits | Eviction policies + active expiry |
| **HA** | None | None | None | Sentinel | Cluster-internal failover | Production monitoring |

---

### L5 vs L6 vs L7 — Phase 4 Rubric

| Aspect | L5 (SDE-2) | L6 (SDE-3) | L7 (Principal) |
|---|---|---|---|
| **Build-up approach** | Jumps to "use Redis Cluster" | Builds iteratively: hash map → network → persistence → replication → sharding → hardening | Same iterative build, but also discusses what was tried and abandoned (e.g., Redis's rejected multi-master approach) |
| **Threading** | "It's single-threaded" | Explains WHY single-threaded (CPU not bottleneck), contrasts with Memcached, mentions I/O threads | Discusses the I/O threads implementation detail (read→execute→write pipeline), why command execution can't be parallelized (shared mutable state) |
| **Persistence** | "It saves to disk" | Explains RDB fork/COW + AOF fsync policies + hybrid, with concrete numbers | Discusses fork() latency at scale (10-20ms per GB), THP impact, AOF rewrite blocking risks, when to disable persistence entirely |
| **Replication** | "It has replicas" | Explains full vs partial resync, replication backlog, PSYNC2, WAIT semantics | Discusses replication backlog sizing math (backlog ≥ write_rate × max_disconnect_time), split-brain probability |
| **Clustering** | "It shards data" | Explains hash slots, gossip, MOVED/ASK, failover mechanics | Discusses why 16384 slots (gossip bandwidth), epoch-based config resolution, replica migration algorithm |
| **Memcached contrast** | Doesn't mention | Contrasts at each layer (threading, persistence, replication, clustering) | Discusses why Memcached's choices are valid for its use case — "right tool for the right job" |

---

## PHASE 5: Deep Dive — Threading Model & Persistence (~10 min)

**Interviewer:**
Let's go deeper on two things. First: you keep saying single-threaded, but how does Redis handle thousands of concurrent connections on a single thread? And second: walk me through a BGSAVE in detail — what happens to the main thread?

**Candidate:**

> "Great questions — let me connect them because they're related.
>
> **The event loop in detail:**
>
> ```
> Redis Main Loop (simplified):
>
> while (true) {
>     // 1. Check for timer events (active expiry, BGSAVE schedule)
>     processTimeEvents()
>
>     // 2. Block on I/O multiplexer (epoll_wait / kevent)
>     //    Returns list of sockets with pending data
>     events = aeApiPoll(timeout)
>
>     // 3. Process each ready socket
>     for (event in events) {
>         if (event.type == READ) {
>             readQueryFromClient(event.fd)   // read RESP command
>             processCommand(client)          // execute (e.g., SET)
>         }
>         if (event.type == WRITE) {
>             writeReplyToClient(event.fd)    // send response
>         }
>     }
>
>     // 4. Background tasks (AOF fsync, replication, etc.)
>     beforeSleep()
> }
> ```
>
> This is the classic **reactor pattern**. The thread never blocks on I/O — `epoll_wait` returns immediately when any of the thousands of file descriptors has data. Each command (GET, SET, etc.) executes in microseconds because it's just an in-memory data structure operation. The thread processes one command, moves to the next. At 180K ops/sec, each command gets ~5.5 microseconds — but commands themselves take much less, so there's ample time for the I/O bookkeeping.
>
> **Redis 6.0+ I/O threads** split the network read/write across threads:
>
> ```
> Main thread: ──read──┬──execute──┬──write──
>                      │           │
> I/O thread 1: ─read──┘           └──write──
> I/O thread 2: ─read──┘           └──write──
> I/O thread 3: ─read──┘           └──write──
>
> Read phase:    All I/O threads parse incoming RESP commands (parallel)
> Execute phase: Main thread executes ALL commands (serial, single-threaded)
> Write phase:   All I/O threads send responses back (parallel)
> ```
>
> Command execution is still single-threaded — this is critical. The I/O threads only handle the network parsing and serialization. This eliminates the need for any locks on the data structures."

**Interviewer:** Now walk me through BGSAVE in detail.

**Candidate:**

> "BGSAVE is one of Redis's most elegant mechanisms. Here's the step-by-step:
>
> ```
> Time T0: BGSAVE triggered (manually or by save schedule)
>   │
>   ├─ Main thread calls fork()
>   │   ├─ OS creates child process
>   │   ├─ Child gets COPY of parent's page table (not the actual memory)
>   │   ├─ All memory pages are marked COW (copy-on-write)
>   │   └─ fork() itself takes ~10-20ms per GB of used memory
>   │
>   ├─ After fork():
>   │   ├─ PARENT (main thread) continues serving clients normally
>   │   │   └─ Any write modifies a page → OS duplicates that page first (COW)
>   │   │
>   │   └─ CHILD (background) writes the in-memory dataset to dump.rdb
>   │       └─ Child sees a frozen, consistent snapshot of all data
>   │       └─ Writes sequentially to disk (very I/O efficient)
>   │
>   ├─ Child finishes writing dump.rdb
>   │   └─ Atomically replaces old dump.rdb with new one
>   │   └─ Child exits, OS frees COW pages
>   │
>   └─ Main thread logs: 'Background saving terminated with success'
> ```
>
> **Why fork() + COW is brilliant:**
> - You get a **consistent point-in-time snapshot** without stopping the main thread
> - The OS handles the complexity — pages are shared until modified
> - Best case: if writes are rare during BGSAVE, memory overhead is near zero (pages shared)
> - Worst case: if every page is written during BGSAVE, memory doubles (every page copied)
>
> **Why fork() can be dangerous:**
> - Fork itself blocks the main thread for ~10-20ms per GB (page table duplication)
> - At 100 GB dataset → fork takes ~1-2 seconds of main thread blocking
> - Linux Transparent Huge Pages (THP) amplify the problem: COW granularity goes from 4 KB to 2 MB per page. A single byte write to a 2 MB huge page triggers a 2 MB copy. Redis strongly recommends `echo never > /sys/kernel/mm/transparent_hugepage/enabled`
>
> **Contrast with Memcached:** Memcached doesn't have this problem because it doesn't have persistence. No fork(), no COW overhead, no disk I/O. This is a feature, not a limitation — Memcached's philosophy is 'data is ephemeral.'"

> *For the full deep dive, see [03-threading-and-event-loop.md](03-threading-and-event-loop.md) and [05-persistence-deep-dive.md](05-persistence-deep-dive.md).*

#### Architecture Update After Phase 5

> | | Before (Phase 4) | After Deep Dive (Phase 5) |
> |---|---|---|
> | **Threading** | "Single-threaded" | Reactor pattern with ae/epoll event loop. Redis 6.0+ I/O threads for network read/write only. Command execution remains serial. |
> | **Persistence** | RDB + AOF + Hybrid | Deep understanding of fork()/COW mechanics, memory overhead, THP risks, AOF rewrite timing |

---

### L5 vs L6 vs L7 — Phase 5 Rubric

| Aspect | L5 (SDE-2) | L6 (SDE-3) | L7 (Principal) |
|---|---|---|---|
| **Event loop** | "Uses epoll" | Explains the reactor pattern, processCommand cycle, why it achieves high throughput | Discusses ae library internals, how timer events interleave with I/O events, `hz` config impact |
| **I/O threads** | Doesn't mention | Explains read→execute→write pipeline, why execution stays single-threaded | Discusses when I/O threads help (high connection count, large payloads) vs don't (small payloads, few connections) |
| **BGSAVE** | "It forks and saves" | Walks through fork/COW step by step, explains THP risk, quantifies fork latency | Discusses fork latency mitigation (jemalloc huge page avoidance), BGSAVE scheduling to avoid coinciding with AOF rewrite, monitoring RSS vs used_memory during fork |
| **AOF** | "Logs every write" | Explains fsync policies, rewrite mechanics, hybrid mode | Discusses AOF rewrite blocking (fdatasync in main thread during rewrite), multi-part AOF (7.0+), AOF verification (redis-check-aof) |

---

## PHASE 6: Deep Dive — Replication & Clustering (~10 min)

**Interviewer:**
Let's go deeper on replication and clustering. Walk me through what happens during a failover — what's the window where writes can be lost?

**Candidate:**

> "Let me trace a failover scenario step by step:
>
> ```
> Timeline of a Leader Failure in Redis Cluster:
>
> T0:  Leader A processes write W1, ACKs to client. W1 not yet replicated.
> T1:  Leader A crashes.
>      Followers A' and A'' still have data up to T0 minus replication lag.
>      Write W1 is LOST — it was ACKed but never replicated.
>
> T2:  Other leaders notice A is not responding to pings.
>      After cluster-node-timeout (default 15,000ms = 15 sec):
>      Nodes mark A as PFAIL (Probable Failure).
>
> T3:  Majority of leaders agree A is PFAIL → promoted to FAIL.
>      (Gossip messages need time to propagate — add ~seconds)
>
> T4:  Followers of A detect FAIL status.
>      Follower with highest replication offset initiates election.
>      Sends FAILOVER_AUTH_REQUEST to all leaders.
>      Needs majority of leaders to vote YES.
>
> T5:  Follower wins election, promotes itself to leader.
>      Increments configEpoch, broadcasts new slot ownership.
>      Starts accepting writes for A's slots.
>
> Total downtime: ~15-30 seconds (cluster-node-timeout + gossip + election)
> Data loss window: Any writes ACKed by A but not replicated before T1.
> ```
>
> **The data loss window is bounded by replication lag.** Typical replication lag is <1ms for a healthy cluster. But if the leader was under heavy write load, or the network was congested, lag could be higher.
>
> **How Sentinel failover differs from Cluster failover:**
>
> | Aspect | Sentinel | Redis Cluster |
> |---|---|---|
> | **Who detects failure?** | Sentinel processes (separate from Redis) | Leader nodes in the cluster (built-in) |
> | **Failure detection** | SDOWN (1 Sentinel) → ODOWN (quorum) | PFAIL (1 node) → FAIL (majority of leaders) |
> | **Who promotes?** | Elected Sentinel leader | Elected follower of the failed leader |
> | **Election protocol** | Raft-like among Sentinels | Epoch-based voting among leaders |
> | **When to use?** | Standalone Redis (data fits on 1 machine) | Redis Cluster (data sharded across machines) |"

**Interviewer:** How does slot migration work during resharding?

**Candidate:**

> "Live slot migration is one of Redis Cluster's most operationally important features. Here's the protocol:
>
> ```
> Migrating slot 7438 from Node A (source) to Node B (target):
>
> Step 1: Admin runs:
>   CLUSTER SETSLOT 7438 MIGRATING <B-node-id>  on Node A
>   CLUSTER SETSLOT 7438 IMPORTING <A-node-id>  on Node B
>
> Step 2: For each key in slot 7438 on Node A:
>   MIGRATE <B-ip> <B-port> <key> 0 5000
>   (atomically: dump key, send to B, B loads it, A deletes it)
>
> Step 3: During migration, client behavior:
>   Client → Node A: GET key_in_slot_7438
>     If key exists on A → return value (normal)
>     If key NOT on A (already migrated) → reply ASK 7438 <B-ip>:<B-port>
>       Client sends ASKING to B, then retries command → B serves it
>
> Step 4: All keys migrated. Admin runs:
>   CLUSTER SETSLOT 7438 NODE <B-node-id> on ALL nodes
>   Slot ownership permanently updated. Future requests → MOVED to B.
> ```
>
> **Key properties:**
> - **No downtime** — keys are migrated one by one while the cluster serves traffic
> - **Atomic per key** — MIGRATE is atomic (key is on exactly one node at any time)
> - **ASK redirections are temporary** — only during migration. Once complete, it's MOVED (permanent)
> - **Big key problem** — if a key has a huge value (e.g., 100 MB sorted set), MIGRATE blocks the source node's main thread for the duration of the transfer. Redis 7.0+ improved this with non-blocking MIGRATE for large keys.
>
> **Contrast with Memcached:** Memcached has no resharding at all. When you add/remove a server, the client recalculates consistent hashing. ~1/N keys now map to a different server and must be repopulated from the database. There's no migration, no redirection, no coordination between servers."

> *For the full deep dive, see [06-replication-deep-dive.md](06-replication-deep-dive.md) and [07-clustering-deep-dive.md](07-clustering-deep-dive.md).*

#### Architecture Update After Phase 6

> | | Before (Phase 4) | After Deep Dive (Phase 6) |
> |---|---|---|
> | **Replication** | "Async leader-follower" | Full understanding: PSYNC2, replication backlog sizing, WAIT semantics, replication lag monitoring |
> | **Failover** | "Automatic failover" | Detailed timeline: PFAIL→FAIL→election→promotion. Data loss window = replication lag. Sentinel vs Cluster failover. |
> | **Resharding** | "Live slot migration" | Full MIGRATING/IMPORTING protocol, ASK redirections, atomic per-key transfer, big key problem |

---

### L5 vs L6 vs L7 — Phase 6 Rubric

| Aspect | L5 (SDE-2) | L6 (SDE-3) | L7 (Principal) |
|---|---|---|---|
| **Failover** | "Replicas take over" | Traces the full timeline: PFAIL→FAIL→election→promotion. Quantifies downtime (~15-30s) and data loss window | Discusses tuning cluster-node-timeout (lower = faster failover but more false positives), partial write scenarios, client reconnection behavior |
| **Data loss** | "Some writes might be lost" | Explains exactly when and why: ACKed before replication, bounded by replication lag | Proposes monitoring: track replication lag, set alerts when lag > threshold, consider WAIT for critical writes with fallback |
| **Resharding** | "Move data between nodes" | Explains MIGRATING/IMPORTING/ASK protocol step by step | Discusses resharding scheduling (off-peak), big key migration impact, monitoring migration progress, rollback procedures |
| **Gossip** | Doesn't mention | Explains gossip protocol, epoch-based config | Discusses gossip bandwidth at scale (1000 nodes), convergence time, split-brain during network partition with gossip delay |

---

## PHASE 7: Deep Dive — Memory Management & Eviction (~8 min)

**Interviewer:**
Let's talk about memory. Redis stores everything in RAM — what happens when you run out? And how does Redis handle millions of keys with TTLs efficiently?

**Candidate:**

> "Two related but distinct problems: eviction (what to remove when memory is full) and expiry (what to remove when TTL has passed).
>
> **Eviction — when memory is full:**
>
> I discussed the 8 eviction policies earlier. Let me go deeper on the implementation:
>
> **Approximated LRU (default approach):**
> ```
> True LRU: Maintain a doubly-linked list of all keys ordered by access time.
>   Problem: O(N) memory overhead (prev/next pointers per key).
>   At 100M keys × 16 bytes per pointer pair = 1.6 GB of overhead just for LRU bookkeeping.
>
> Redis's approach: Each key stores a 24-bit timestamp of last access (in the key object header).
>   On eviction: sample maxmemory-samples keys (default 5), evict the one with oldest timestamp.
>   Memory overhead: 0 additional bytes (the 24-bit timestamp fits in existing object header padding).
>
> With maxmemory-samples=5:  reasonable approximation
> With maxmemory-samples=10: very close to true LRU
> With maxmemory-samples=20: nearly indistinguishable from true LRU
> ```
>
> **LFU (Least Frequently Used, Redis 4.0+):**
> ```
> Why LFU? LRU has a weakness: a one-time full-table scan (e.g., KEYS * or a background job
> iterating all keys) touches every key, making them all 'recently used.' LRU then evicts
> the actually-hot keys because they appear 'older' than the scan-touched keys.
>
> LFU tracks access FREQUENCY, not just recency:
>   • 8-bit logarithmic counter (0-255) per key — saturates at 255
>   • Counter increment is probabilistic: P(increment) = 1 / (counter × lfu-log-factor + 1)
>     With lfu-log-factor=10 (default): counter reaches ~100 after 1M accesses
>   • Counter decays over time: every lfu-decay-time minutes (default 1), counter -= 1
>   • This means: keys that were hot yesterday but cold today gradually lose their counter
> ```
>
> **Contrast with Memcached's slab allocator:**
> Memcached pre-allocates memory in slab classes (pages of 1 MB, chunk sizes grow by ~1.25x factor, ~42 classes). Pros: no fragmentation, predictable memory layout. Cons: internal fragmentation (a 100-byte item in a 128-byte chunk wastes 28 bytes), slab calcification (uneven distribution across classes). Redis uses jemalloc which is more flexible but can fragment over time.
>
> **Expiry — the TTL mechanism:**
>
> ```
> The elegant dual approach:
>
> 1. PASSIVE EXPIRY (lazy deletion):
>    On every key access:
>      if (key.expiry_timestamp < now()) {
>          deleteKey(key)
>          return KEY_NOT_FOUND
>      }
>    • Zero overhead for unaccessed keys
>    • Problem: if a key is never accessed, it sits in memory forever
>
> 2. ACTIVE EXPIRY (probabilistic sweep):
>    Runs hz times per second (default hz=10, so every 100ms):
>    do {
>        sample 20 random keys from the set of keys with TTL
>        delete the expired ones
>        count = number_expired / 20
>    } while (count > 0.25)  // repeat if >25% were expired
>
>    • This bounds CPU usage: if few keys are expired, one iteration is enough
>    • If many keys are expired (e.g., a mass expiry event), the loop continues
>      but caps at 25% of each hz cycle to avoid starving client requests
> ```
>
> **Why this hybrid approach?**
> - Passive alone: memory leak (unaccessed expired keys pile up indefinitely)
> - Active alone: expensive (would need to scan all keys regularly)
> - Hybrid: passive catches accessed keys immediately, active probabilistically cleans up the rest
>
> **Replication of expiry:** Replicas do NOT independently expire keys. The leader generates `DEL` commands and replicates them. This ensures consistency — a key is either expired on all nodes or none. Trade-off: replicas may briefly serve stale reads for expired keys until the DEL propagates."

> *For the full deep dive, see [08-memory-and-eviction.md](08-memory-and-eviction.md) and [09-expiry-and-ttl.md](09-expiry-and-ttl.md).*

#### Architecture Update After Phase 7

> | | Before (Phase 4) | After Deep Dive (Phase 7) |
> |---|---|---|
> | **Eviction** | "8 eviction policies" | Deep understanding: approximated LRU (sampling), LFU (logarithmic counter + decay), why LFU defeats scan-pollution. jemalloc vs Memcached's slab allocator. |
> | **Expiry** | "Passive + active expiry" | Implementation detail: hz-based sweep, 20-key sample, 25% threshold loop. Replication of DELs for consistency. |

---

### L5 vs L6 vs L7 — Phase 7 Rubric

| Aspect | L5 (SDE-2) | L6 (SDE-3) | L7 (Principal) |
|---|---|---|---|
| **Eviction** | "Use LRU" | Explains approximated LRU (sampling), quantifies memory overhead savings. Explains LFU and why it was added (scan resistance) | Discusses maxmemory-samples tuning (5 vs 10 vs 20), LFU counter mechanics (logarithmic increment, decay), volatile-* vs allkeys-* policy selection framework |
| **Expiry** | "Keys expire after TTL" | Explains passive + active dual mechanism with concrete numbers (20 keys, 25% threshold, hz=10) | Discusses expiry thundering herd (mass expiry → CPU spike), replication lag for DEL propagation, SCAN-based batch expiry for large keyspaces |
| **Memory** | "Stores data in RAM" | Explains jemalloc, fragmentation ratio, Memcached slab allocator contrast | Discusses active defragmentation (4.0+), memory purge, OOM killer risk, swap impact on latency, RSS monitoring vs used_memory |
| **Memcached contrast** | Doesn't mention | Notes slab allocator pros/cons | Explains slab calcification, slab reassignment, why slab approach is better for Memcached's uniform workload but worse for Redis's variable data structures |

---

## PHASE 8: Wrap-Up & Summary (~5 min)

**Interviewer:**
We're running short on time. Summarize your design and tell me — if you were on-call for this system at scale, what keeps you up at night?

**Candidate:**

> "**Design evolution — how we got here:**
>
> | Component | Started With (Attempt 0) | Evolved To (Attempt 5) | Why |
> |---|---|---|---|
> | **Architecture** | Single process with a hash map | Multi-node cluster with sharding, replication, persistence, eviction | Each iteration solved a specific problem in the previous design |
> | **Access** | Local-only | TCP + RESP protocol, single-threaded event loop | Network access with human-readable wire protocol |
> | **Threading** | N/A | Single-threaded command execution + I/O threads (6.0+) | Simplicity > raw throughput. CPU isn't the bottleneck. |
> | **Persistence** | None | RDB snapshots (fork/COW) + AOF (append log) + hybrid | Configurable durability: pure cache to ~1 sec data loss |
> | **Replication** | None | Async leader-follower, PSYNC2 partial resync, Sentinel | Read scaling + HA. Not strong consistency — AP in CAP. |
> | **Sharding** | N/A | Redis Cluster: 16384 hash slots, gossip, MOVED/ASK | Scale beyond one machine. Write scaling via multiple leaders. |
> | **Memory mgmt** | Unbounded | 8 eviction policies (approx LRU/LFU), active+passive expiry | Bounded memory. Dual expiry mechanism for efficiency. |
>
> **What keeps me up at night:**
>
> 1. **Cache stampede / thundering herd** — Leader fails, all clients reconnect to the new leader simultaneously. Or cache expires for a popular key, and 10,000 concurrent requests all miss the cache and hit the database at once. Mitigation: staggered reconnection with jitter, lock-based cache repopulation (only one request fetches from DB, others wait), `min-replicas-to-write` to limit stale leader writes.
>
> 2. **Hot key problem** — One key (e.g., a viral tweet's like counter) gets millions of ops/sec. The shard owning that key becomes a bottleneck. No amount of cluster scaling helps because the key is on one shard. Mitigation: client-side caching (Redis 6.0+, server-assisted invalidation), read replicas for read-heavy hot keys, application-level key splitting for write-heavy hot keys (e.g., split a counter across 10 sharded keys, sum on read).
>
> 3. **fork() latency for large datasets** — BGSAVE on a 100 GB dataset blocks the main thread for ~1-2 seconds during fork(). During this pause, no client requests are processed. Mitigation: schedule BGSAVE during low-traffic periods, use AOF-only persistence (no fork needed except for AOF rewrite), use replicas for backup (run BGSAVE on a follower, not the leader), consider disabling persistence entirely for pure cache use cases.
>
> 4. **Data loss during failover** — The fundamental AP tradeoff. Any write acknowledged by the leader but not yet replicated is lost when the leader fails. In most cases replication lag is <1ms, so the window is tiny. But under heavy write load or network congestion, the lag can grow. Mitigation: WAIT for critical writes (with timeout), `min-replicas-to-write` to refuse writes when replicas are unreachable, monitor replication lag and alert when it exceeds threshold.
>
> 5. **Memory fragmentation** — jemalloc generally handles this well, but workloads with many deletes/updates of variable-size values can fragment memory. `mem_fragmentation_ratio` > 1.5 means 50% of RSS is wasted. Mitigation: active defragmentation (Redis 4.0+), `MEMORY PURGE`, and worst case — restart the instance (reload from RDB/AOF with defragmented memory layout).
>
> 6. **Cluster split-brain** — During a network partition, both sides might think they're the leader for some slots. The minority side will stop accepting writes after `cluster-node-timeout`, but during that window, two leaders might accept conflicting writes. On partition heal, last-writer-wins by epoch — one set of writes is silently lost. Mitigation: `min-replicas-to-write` on all leaders, fast partition detection (lower `cluster-node-timeout` — but not too low to avoid false positives).
>
> **Extensions I'd explore with more time:**
>
> | Extension | Value |
> |---|---|
> | **Redis Streams** | Kafka-like log with consumer groups — replaces Pub/Sub for durable messaging |
> | **Client-side caching** | Server-assisted invalidation (Redis 6.0+) — reduces read load for hot keys by 10-100x |
> | **Redis Functions (7.0+)** | Persistent Lua-like functions — replaces EVAL with proper lifecycle management |
> | **Sharded Pub/Sub (7.0+)** | Pub/Sub routed by channel hash slot — scales with cluster instead of broadcasting to all nodes |
> | **Cross-datacenter replication** | Active-active with CRDTs or last-writer-wins. Not in open-source Redis but in Redis Enterprise |
> | **Security hardening** | ACL v2 (7.0+), TLS mutual auth, network segmentation, audit logging |"

---

## Interviewer's Final Assessment

**Hire Recommendation: Strong Hire (L6 — solid Senior SDE)**

| Dimension | Rating | Notes |
|---|---|---|
| **Problem Decomposition** | Exceeds Bar | Clean iterative build-up from hash map to Redis Cluster. Each attempt identified concrete problems and solved them. Never jumped to the final architecture. |
| **Requirements & Scoping** | Exceeds Bar | Distinguished "data structure server" from "cache" immediately. Quantified throughput. Knew the consistency model (AP). |
| **API Design** | Meets Bar | RESP protocol explanation, core command categories, Memcached API contrast. |
| **Threading Model** | Exceeds Bar | Reactor pattern, epoll event loop, I/O threads pipeline. Explained WHY single-threaded with conviction and quantitative reasoning. |
| **Persistence** | Exceeds Bar | Deep fork/COW walkthrough with THP risk. RDB + AOF + hybrid with concrete config values. |
| **Replication** | Exceeds Bar | Full/partial resync, PSYNC2, replication backlog, WAIT semantics. Traced failover timeline with data loss window analysis. |
| **Clustering** | Exceeds Bar | Hash slots, gossip, MOVED/ASK, slot migration protocol. Explained why 16384 slots. |
| **Memory Management** | Exceeds Bar | Approximated LRU sampling, LFU logarithmic counter + decay, dual expiry mechanism. |
| **Memcached Contrast** | Exceeds Bar | Consistent contrast at every layer — threading, persistence, replication, clustering, API, memory. Not dismissive — acknowledged Memcached's valid design choices for its use case. |
| **Operational Maturity** | Exceeds Bar | "What keeps you up at night" was strong — cache stampede, hot keys, fork latency, split-brain, fragmentation. Concrete mitigations for each. |
| **Communication** | Exceeds Bar | Structured, used diagrams and tables, drove the conversation. Iterative build-up felt natural and motivated. |

**What would push this to L7:**
- Deeper discussion of gossip protocol bandwidth at 1000-node scale and convergence guarantees
- Proposing a monitoring/observability architecture (dashboards for replication lag, memory fragmentation, slow queries, hot keys)
- Discussing the Redis module system and how it extends the data model
- Cost modeling: $/GB/month for different persistence modes, TCO of Redis Cluster vs managed services
- Discussing Redis's limitations honestly: when NOT to use Redis (datasets >> RAM, strong consistency requirements, complex queries)
- Cross-datacenter replication design with conflict resolution strategies

---

## Key Differences: L5 vs L6 vs L7 Expectations for This Problem

| Aspect | L5 (SDE-2) | L6 (SDE-3) | L7 (Principal) |
|---|---|---|---|
| **Requirements** | Lists GET/SET, mentions caching | Distinguishes "data structure server" from "cache", knows consistency model (AP), quantifies throughput | Frames requirements around failure modes, discusses when Redis is NOT the right choice |
| **Architecture** | "Use Redis" or jumps to Cluster | Iterative build-up: hash map → network → persistence → replication → sharding → hardening | Same iterative build, discusses alternatives considered and rejected, draws parallels to other distributed systems |
| **Threading** | "It's single-threaded" | Explains WHY (CPU not bottleneck), event loop pattern, I/O threads | Discusses ae library internals, when I/O threads help, alternative approaches (io_uring), Memcached's threading model pros/cons |
| **Persistence** | "Saves to disk" | RDB fork/COW + AOF fsync policies + hybrid, with trade-off table | Fork latency at scale, THP impact, when to disable persistence, AOF rewrite blocking risks |
| **Replication** | "Has replicas" | Full/partial resync, PSYNC2, WAIT semantics, Sentinel vs Cluster failover | Replication backlog sizing math, split-brain probability, monitoring replication lag |
| **Clustering** | "Shards data" | Hash slots, gossip, MOVED/ASK, slot migration protocol | Gossip bandwidth analysis, epoch-based config resolution, replica migration, consistent hashing alternative |
| **Memory** | "Uses LRU" | Approximated LRU + LFU, dual expiry, jemalloc vs slab allocator | Memory fragmentation analysis, active defrag, OOM risk, swap impact |
| **Memcached** | Doesn't mention | Contrasts at each layer with reasoning | Discusses why Memcached's choices are valid, "right tool for the right job" |
| **Operations** | Mentions monitoring | Identifies failure modes with concrete mitigations | Proposes blast radius isolation, game days, automated remediation, capacity planning |

---

*For detailed deep dives on each component, see the companion documents:*
- [02-api-contracts.md](02-api-contracts.md) — Complete Redis command reference (400+ commands by category)
- [03-threading-and-event-loop.md](03-threading-and-event-loop.md) — Event loop internals, I/O threads, Memcached threading contrast
- [04-data-structures-and-encodings.md](04-data-structures-and-encodings.md) — Internal encodings (SDS, quicklist, skiplist, listpack)
- [05-persistence-deep-dive.md](05-persistence-deep-dive.md) — RDB fork/COW, AOF fsync, hybrid, multi-part AOF
- [06-replication-deep-dive.md](06-replication-deep-dive.md) — PSYNC2, Sentinel, split-brain mitigation
- [07-clustering-deep-dive.md](07-clustering-deep-dive.md) — Hash slots, gossip, MOVED/ASK, slot migration
- [08-memory-and-eviction.md](08-memory-and-eviction.md) — Eviction policies, approximated LRU/LFU, jemalloc
- [09-expiry-and-ttl.md](09-expiry-and-ttl.md) — Passive + active expiry, replication of DELs
- [10-scaling-and-performance.md](10-scaling-and-performance.md) — Scaling decisions, performance trade-offs, benchmarking
- [11-design-trade-offs.md](11-design-trade-offs.md) — Design philosophy: single-threaded, RESP, hash slots vs consistent hashing, AP vs CP

*End of interview simulation.*
