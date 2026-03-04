Design a Distributed Cache (Redis) as a system design interview simulation.

## Template
Follow the EXACT same format as the S3 interview simulation at:
src/hld/aws/s3/design/interview-simulation.md

Same structure:
- Interviewer/Candidate dialogue format
- PHASE 1: Opening & Problem Statement
- PHASE 2: Requirements Gathering & Scoping (functional + non-functional + scale numbers)
- PHASE 3: API Design
- PHASE 4: High-Level Architecture (iterative build-up: Attempt 0 → 1 → 2 → ..., starting from single server, finding problems, evolving)
- PHASE 5+: Deep dives on each component
- L5/L6/L7 rubric table after EACH deep dive phase — showing exactly how an L5, L6, and L7 candidate would answer differently. This is critical for understanding what "good" looks like at each level.
- Architecture evolution table after each phase
- Wrap-up phase with "what keeps you up at night"
- Links to supporting deep-dive docs

## Candidate Level
**L6 SDE-3 (Senior SDE)** candidate being interviewed by a Principal Engineer (L8).

The candidate's answers should demonstrate L6-level depth:
- Not just "what" but "why" and "why not the alternative"
- Quantitative reasoning (back-of-envelope math, concrete numbers)
- Awareness of operational concerns (monitoring, failure modes, blast radius)
- Proactive identification of tradeoffs before the interviewer asks

The L5/L6/L7 rubric tables after each phase should make it crystal clear how an L5 answer differs from L6 differs from L7.

## Output location
Create all files under: src/hld/distributedcache/design/

## Files to create

### 1. 01-interview-simulation.md — the main backbone
The full interview dialogue covering all phases and attempts.

### 2. 02-api-contracts.md — Redis API & Command Reference
This doc should list **all** Redis commands grouped by category. The interview simulation (Phase 3: API Design) will only cover a subset — the most important ones for the distributed cache design. But this doc should be a comprehensive reference.

**Structure**: For each command group, list every command with a one-line description. Mark commands covered in the interview with a star or highlight. For key commands, include: syntax, time complexity, and a brief "why this exists" note.

**Command groups to cover (ALL commands in each group)**:

- **String commands**: GET, SET (with EX/PX/NX/XX/KEEPTTL/GET options), MGET, MSET, MSETNX, INCR, DECR, INCRBY, DECRBY, INCRBYFLOAT, APPEND, STRLEN, GETRANGE, SETRANGE, SETNX (deprecated — use SET NX), SETEX (deprecated — use SET EX), PSETEX (deprecated), GETDEL, GETEX, LCS (Redis 7.0+), SUBSTR (deprecated alias of GETRANGE)

- **List commands**: LPUSH, RPUSH, LPUSHX, RPUSHX, LPOP, RPOP, LLEN, LRANGE, LINDEX, LSET, LINSERT, LREM, LTRIM, BLPOP, BRPOP, LPOS, LMOVE, BLMOVE, LMPOP (Redis 7.0+), BLMPOP (Redis 7.0+), RPOPLPUSH (deprecated — use LMOVE)

- **Set commands**: SADD, SREM, SISMEMBER, SMISMEMBER, SMEMBERS, SCARD, SPOP, SRANDMEMBER, SUNION, SINTER, SDIFF, SUNIONSTORE, SINTERSTORE, SDIFFSTORE, SINTERCARD (Redis 7.0+), SSCAN

- **Sorted Set commands**: ZADD (with NX/XX/GT/LT/CH/INCR options), ZREM, ZSCORE, ZMSCORE, ZRANK, ZREVRANK, ZRANGE (unified in Redis 6.2+ — replaces ZRANGEBYSCORE, ZRANGEBYLEX, ZREVRANGE, ZREVRANGEBYSCORE, ZREVRANGEBYLEX), ZRANGESTORE, ZCARD, ZCOUNT, ZLEXCOUNT, ZINCRBY, ZPOPMIN, ZPOPMAX, BZPOPMIN, BZPOPMAX, ZRANDMEMBER, ZUNIONSTORE, ZINTERSTORE, ZDIFFSTORE, ZUNION, ZINTER, ZDIFF, ZMPOP (Redis 7.0+), BZMPOP (Redis 7.0+), ZSCAN

- **Hash commands**: HSET, HGET, HMSET (deprecated — use HSET), HMGET, HDEL, HEXISTS, HLEN, HKEYS, HVALS, HGETALL, HINCRBY, HINCRBYFLOAT, HSETNX, HRANDFIELD, HSCAN, HEXPIRE/HPEXPIRE/HEXPIREAT/HPEXPIREAT/HTTL/HPTTL/HPERSIST (Redis 7.4+ — per-field TTL)

- **Key commands**: DEL, UNLINK (async DEL), EXISTS, TYPE, RENAME, RENAMENX, COPY, DUMP, RESTORE, OBJECT (ENCODING, REFCOUNT, IDLETIME, FREQ, HELP), SORT, SORT_RO, TOUCH, RANDOMKEY, SCAN, KEYS (avoid in production — blocks), WAIT, WAITAOF (Redis 7.2+)

- **Expiry commands** (subset of key commands, but important enough to call out): EXPIRE, PEXPIRE, EXPIREAT, PEXPIREAT, TTL, PTTL, PERSIST, EXPIRETIME (Redis 7.0+), PEXPIRETIME (Redis 7.0+)

- **HyperLogLog commands**: PFADD, PFCOUNT, PFMERGE. Note: 12 KB per key, ~0.81% standard error.

- **Bitmap commands**: SETBIT, GETBIT, BITCOUNT, BITOP (AND/OR/XOR/NOT), BITPOS, BITFIELD, BITFIELD_RO

- **Geospatial commands**: GEOADD, GEODIST, GEOHASH, GEOPOS, GEOSEARCH (Redis 6.2+ — replaces GEORADIUS/GEORADIUSBYMEMBER), GEOSEARCHSTORE

- **Stream commands**: XADD, XREAD, XREADGROUP, XRANGE, XREVRANGE, XLEN, XTRIM, XDEL, XINFO (STREAM, GROUPS, CONSUMERS), XGROUP (CREATE, SETID, DELCONSUMER, DESTROY, CREATECONSUMER), XACK, XPENDING, XCLAIM, XAUTOCLAIM (Redis 6.2+)

- **Pub/Sub commands**: SUBSCRIBE, UNSUBSCRIBE, PUBLISH, PSUBSCRIBE, PUNSUBSCRIBE, PUBSUB (CHANNELS, NUMSUB, NUMPAT, SHARDCHANNELS, SHARDNUMSUB), SSUBSCRIBE (Redis 7.0+ — sharded), SUNSUBSCRIBE (Redis 7.0+), SPUBLISH (Redis 7.0+)

- **Transaction commands**: MULTI, EXEC, DISCARD, WATCH, UNWATCH. Note: Redis transactions are NOT like SQL transactions — no rollback, all-or-nothing execution, WATCH provides optimistic locking (CAS).

- **Scripting & Functions commands**: EVAL, EVALSHA, EVALRO, EVALSHA_RO, SCRIPT (LOAD, EXISTS, FLUSH, DEBUG). Redis 7.0+ Functions: FUNCTION (LOAD, DUMP, RESTORE, DELETE, FLUSH, LIST, STATS), FCALL, FCALL_RO. Note: scripts/functions run atomically on the single thread — blocking.

- **Connection commands**: AUTH, PING, ECHO, QUIT (deprecated in Redis 7.2+), SELECT, HELLO (Redis 6.0+ — protocol negotiation, RESP2↔RESP3), RESET, CLIENT (ID, GETNAME, SETNAME, INFO, LIST, KILL, PAUSE, UNPAUSE, NO-EVICT, NO-TOUCH, TRACKING, CACHING, GETREDIR, SETINFO)

- **Server/Admin commands**: INFO (sections: server, clients, memory, persistence, stats, replication, cpu, commandstats, latencystats, cluster, keyspace), DBSIZE, FLUSHDB, FLUSHALL, SAVE, BGSAVE, BGREWRITEAOF, LASTSAVE, CONFIG (GET, SET, RESETSTAT, REWRITE), TIME, SLOWLOG (GET, LEN, RESET), LATENCY (LATEST, HISTORY, RESET, GRAPH), MEMORY (USAGE, DOCTOR, MALLOC-STATS, PURGE, STATS), COMMAND (COUNT, DOCS, GETKEYS, INFO, LIST), DEBUG, MONITOR, SWAPDB, SHUTDOWN, FAILOVER, LOLWUT

- **ACL commands** (Redis 6.0+): ACL (LIST, GETUSER, SETUSER, DELUSER, CAT, GENPASS, WHOAMI, LOG, SAVE, LOAD, DRYRUN)

- **Cluster commands**: CLUSTER (INFO, NODES, SLOTS, SHARDS, MYID, MEET, ADDSLOTS, DELSLOTS, ADDSLOTSRANGE, DELSLOTSRANGE, SETSLOT, FAILOVER, RESET, REPLICATE, FLUSHSLOTS, KEYSLOT, COUNTKEYSINSLOT, GETKEYSINSLOT, LINKS, SAVECONFIG), READONLY, READWRITE, ASKING

- **Replication commands**: REPLICAOF (replaces SLAVEOF), PSYNC (internal)

- **Module commands** (Redis 4.0+): MODULE (LOAD, LOADEX, UNLOAD, LIST)

**Contrast with Memcached's API**: Memcached has ~15 commands total: get, gets, set, add, replace, append, prepend, cas, delete, incr, decr, touch, gat, gats, stats, flush_all, version, quit. No data structure operations. No scripting. No transactions. No pub/sub. This simplicity is intentional — Memcached is a focused cache, Redis is a data structure server.

**Interview subset**: In the interview simulation (Phase 3), focus the API discussion on the core commands that illustrate design decisions: GET/SET (basic KV), data structure commands (LPUSH, ZADD, HSET — show why Redis is more than a cache), EXPIRE/TTL (cache semantics), SUBSCRIBE/PUBLISH (messaging), MULTI/EXEC (transactions), EVAL (scripting), WAIT (replication awareness), CLUSTER SLOTS/MOVED (cluster protocol). The full list lives in this doc for reference.

### 3. 03-threading-and-event-loop.md — Threading Model & Event Loop
- Why single-threaded command execution? No locks, no context switching, predictable latency.
- Redis's `ae` event library: epoll (Linux), kqueue (macOS/BSD) multiplexing.
- How Redis achieves ~180K ops/sec single-threaded (verified benchmark). With 16-command pipelining: 1.5M+ ops/sec.
- Redis 6.0+ I/O threads: `io-threads` config, network read/write parallelized, command execution remains single-threaded. Default: I/O threads disabled.
- **Contrast with Memcached**: Multi-threaded (default 4 worker threads via `-t` flag). Main thread accepts connections, dispatches to workers. Global cache lock (later fine-grained). Scales CPU better but adds lock contention complexity.
- **Antirez's reasoning**: "Redis is a data structure server, and data structure operations are O(1) or O(log N) — CPU is not the bottleneck. Network I/O and memory are. Single-threaded avoids all concurrency bugs."
- **Scaling decision**: Why scale-out (multiple Redis instances) rather than scale-up (multi-threaded within one instance)? Trade-offs of each approach.

### 4. 04-data-structures-and-encodings.md — Data Structures & Internal Encodings
- **SDS (Simple Dynamic Strings)**: Binary-safe, O(1) length, pre-allocation, max value size 512 MB.
- **Lists**: quicklist (linked list of listpacks/ziplists). Redis 7.0+ replaced ziplist with listpack everywhere.
- **Sets**: intset (small integer-only sets) → hashtable when exceeding threshold or containing non-integers.
- **Sorted Sets**: Dual structure — skiplist (O(log N) range queries) + hashtable (O(1) score lookup). Small sets: listpack encoding.
- **Hashes**: listpack (small) → hashtable (large). Encoding thresholds configurable via `hash-max-listpack-entries` / `hash-max-listpack-value`.
- **Streams**: Radix tree of listpacks. Consumer groups, XREAD, XACK. Append-only log structure.
- **HyperLogLog**: 12 KB per key, ~0.81% standard error for cardinality estimation.
- **Bitmaps**: String type with bit operations (SETBIT, GETBIT, BITCOUNT).
- **Geospatial**: Sorted set with geohash-encoded scores.
- **Why these encodings matter**: Memory-compact encodings (listpack) for small collections, switching to full data structures at configurable thresholds. This is a key Redis design philosophy — optimize for the common case (small collections) while supporting arbitrary scale.
- **Contrast with Memcached**: Strings only. No data structure operations server-side. All structure must be managed client-side (serialize/deserialize). Slab allocator with 1 MB page size, ~1.25x growth factor, ~42 slab classes. Max item size: 1 MB default.

### 5. 05-persistence-deep-dive.md — Persistence (RDB + AOF)
- **RDB snapshots**: `BGSAVE` uses fork() + copy-on-write (COW). Parent continues serving, child writes snapshot. Default save triggers: `save 3600 1 300 100 60 10000` (Redis 7.0+ defaults). Background save doesn't block main thread (except the fork() itself, which is ~ms per GB of used memory).
- **AOF (Append-Only File)**: Every write command appended to AOF. Three `appendfsync` policies: `always` (fsync every write — safest, slowest), `everysec` (default — fsync once per second, ≤1 sec data loss), `no` (OS decides when to flush — fastest, least safe).
- **AOF rewrite**: Background process rewrites AOF to minimal representation. `auto-aof-rewrite-percentage 100` (rewrite when AOF doubles), `auto-aof-rewrite-min-size 64mb`.
- **RDB-AOF hybrid (Redis 4.0+)**: `aof-use-rdb-preamble yes` (introduced 4.0 with default `no`; default `yes` since Redis 7.0). AOF rewrite produces RDB header + incremental AOF tail. Fast recovery of RDB + minimal data loss of AOF.
- **Multi-part AOF (Redis 7.0+)**: AOF split into base file + incremental files in a manifest. Cleaner management, atomic replacement.
- **Why both RDB and AOF?** RDB: compact (~10x smaller), fast recovery (just load binary), great for backups/disaster recovery, but potential data loss (minutes between saves). AOF: minimal data loss (≤1 second), human-readable/auditable, but slower recovery (replay commands), larger files.
- **fork() deep dive**: Why fork() is brilliant for persistence — COW gives you a consistent point-in-time snapshot without stopping writes. Trade-off: memory overhead during fork (worst case 2x if every page is modified). Linux transparent huge pages (THP) can amplify COW overhead — Redis recommends disabling THP.
- **Contrast with Memcached**: NO persistence whatsoever. Restart = cold cache. This is the fundamental philosophical difference: Memcached is a pure volatile cache, Redis is "a database that happens to be in memory."
- **Scaling decision**: When to use RDB only, AOF only, hybrid, or no persistence? Decision framework based on use case (pure cache vs. durable store).

### 6. 06-replication-deep-dive.md — Replication & High Availability
- **Async leader-follower replication**: Leader processes all writes, asynchronously streams to followers. Followers are read-only by default.
- **Full resync**: Follower connects → leader does BGSAVE → transfers RDB → follower loads it → leader streams buffered writes. Triggered on first connect or when replication backlog is insufficient.
- **Partial resync (PSYNC2, Redis 4.0+)**: Replication backlog (circular buffer, default `repl-backlog-size 1mb`). Uses replication ID + offset. PSYNC2 introduced dual replication IDs so followers can partial-resync after a failover without full resync.
- **WAIT command**: `WAIT <numreplicas> <timeout>` — blocks until N replicas ACK the write. NOT strong consistency — it's best-effort. If leader fails between write and replication, data is lost even with WAIT.
- **Replication lag monitoring**: `INFO replication` shows each replica's offset. `master_repl_offset` minus `slave_repl_offset` = lag in bytes.
- **Sentinel** (for standalone Redis, not Cluster):
  - Separate process, default port 26379. Monitors leader + followers.
  - **SDOWN** (Subjective Down): One Sentinel thinks a node is down (no reply within `down-after-milliseconds`).
  - **ODOWN** (Objective Down): Quorum of Sentinels agree the leader is down.
  - **Leader election**: Raft-like protocol to elect a Sentinel leader for failover. Sentinel leader picks the best follower (highest replication offset, lowest `replica-priority`), promotes it, reconfigures other followers.
  - **Split-brain mitigation**: `min-replicas-to-write` + `min-replicas-max-lag` — leader refuses writes if fewer than N replicas are reachable with lag < M seconds. Prevents stale leader from accepting writes during partition.
- **Contrast with Redis Cluster's built-in failover**: Cluster doesn't need Sentinel — failover is baked into the gossip protocol. But the mechanism is similar: PFAIL/FAIL detection, follower election, epoch-based configuration.
- **Contrast with Memcached**: No replication at all. Clients handle redundancy (e.g., write to multiple servers). If a Memcached node dies, that data is gone.
- **Scaling decision**: When to use Sentinel vs Cluster? Sentinel: simpler, good for ≤1 machine's worth of data, separate monitoring. Cluster: scales data horizontally, built-in failover, but more complex client requirements (smart clients).

### 7. 07-clustering-deep-dive.md — Redis Cluster
- **Hash slot model**: 16,384 hash slots. `CRC16(key)` (XMODEM variant, polynomial 0x1021) `mod 16384` → slot → node. Why 16,384? Good balance between cluster metadata size (2 KB bitmap per node) and max cluster size.
- **Cluster topology**: Each slot assigned to exactly one leader. Each leader has 0+ followers. Typical: 3 leaders × 3 followers = 9 nodes for a production cluster.
- **Cluster Bus**: Dedicated port (data port + 10,000, e.g., 6379 → 16379). Binary gossip protocol. Nodes exchange ping/pong messages containing: node ID, IP, port, flags, slot bitmap, cluster epoch.
- **Gossip protocol**: Probabilistic — each node pings a random node every second, plus any node not pinged within `cluster-node-timeout / 2`. Full cluster state eventually converges.
- **MOVED redirection**: Client sends command to wrong node → node replies `MOVED <slot> <ip>:<port>`. Client updates its slot-to-node mapping and retries. This is permanent — the slot lives on that node.
- **ASK redirection**: During slot migration, a key might be on the source or target node. Source replies `ASK <slot> <target>` if key not found locally. Client sends `ASKING` command to target, then retries. This is temporary — only during migration.
- **Hash tags**: `{user:1000}.profile` and `{user:1000}.settings` hash to the same slot because `CRC16("user:1000")` is computed on the `{...}` content. Enables multi-key operations on related keys.
- **Resharding / slot migration**: Live, no downtime. Source marks slot as MIGRATING, target marks as IMPORTING. Keys moved one-by-one with `MIGRATE` command (atomic per key). Clients experience ASK redirections during migration.
- **Failure detection**: PFAIL (Probable Failure): node doesn't respond within `cluster-node-timeout` (default 15,000 ms). FAIL: majority of leaders mark a node as PFAIL → promoted to FAIL → triggers failover.
- **Follower election in Cluster**: Followers of a failed leader hold an election. Follower with highest replication offset has priority. Requires majority of leaders to vote. Uses `currentEpoch` and `configEpoch` for consistency.
- **Replica migration**: If a leader loses all followers, a follower from a leader with excess followers automatically migrates. Prevents single points of failure.
- **Limitations**: No multi-key operations across slots (unless hash tags). No SELECT (only DB 0). Cluster protocol overhead. Max ~1,000 nodes recommended.
- **Contrast with Memcached**: No server-side clustering at all. Client-side consistent hashing (ketama algorithm). Servers are unaware of each other. Adding/removing a server causes cache misses for `~1/N` of keys. No resharding, no failover, no gossip.
- **Scaling decisions and trade-offs**:
  - Why hash slots instead of consistent hashing? Explicit slot assignment allows fine-grained control, deterministic migration, no virtual nodes needed.
  - Why gossip instead of a centralized coordinator (like ZooKeeper)? No single point of failure, simpler deployment, but slower convergence and higher bandwidth.
  - Why async replication in Cluster (no Raft/Paxos per slot)? Performance over consistency — Redis prioritizes availability and partition tolerance (AP in CAP), with best-effort consistency.

### 8. 08-memory-and-eviction.md — Memory Management & Eviction
- **maxmemory**: Hard memory limit. When reached, eviction policy kicks in.
- **8 eviction policies**:
  - `noeviction` — return errors on writes (reads still work). Default if maxmemory is set.
  - `allkeys-lru` — evict least recently used key from all keys.
  - `allkeys-lfu` — evict least frequently used key from all keys (Redis 4.0+).
  - `allkeys-random` — evict a random key.
  - `volatile-lru` — evict LRU key among keys with TTL set.
  - `volatile-lfu` — evict LFU key among keys with TTL set (Redis 4.0+).
  - `volatile-random` — evict random key among keys with TTL set.
  - `volatile-ttl` — evict key with nearest TTL expiry.
- **Approximated LRU**: Redis does NOT use a true LRU (no linked list traversal). Samples `maxmemory-samples` keys (default 5), evicts the one with the oldest access time. With samples=10, approximation is very close to true LRU. Trade-off: O(1) vs O(N) memory overhead.
- **LFU (Redis 4.0+)**: Logarithmic counter (8-bit, saturates at 255) + decay over time. `lfu-log-factor` (default 10) controls counter growth rate. `lfu-decay-time` (default 1 minute) controls how fast the counter decays. Better than LRU for scan-resistant caching.
- **jemalloc**: Redis's default memory allocator. Reduces fragmentation vs glibc malloc. `INFO memory` shows `mem_fragmentation_ratio` — ratio of RSS to used memory. Ratio > 1.5 suggests fragmentation; `MEMORY PURGE` or `activedefrag` can help.
- **Active defragmentation (Redis 4.0+)**: Background process that moves allocations to reduce fragmentation. Configurable thresholds and CPU limits.
- **Contrast with Memcached's slab allocator**: Memcached pre-allocates memory in slab classes (pages of 1 MB, chunk sizes grow by ~1.25x factor, ~42 classes). Pros: no fragmentation, predictable. Cons: internal fragmentation (e.g., 100-byte item in 128-byte chunk wastes 28 bytes), slab calcification (uneven distribution). Memcached has slab reassignment to mitigate this.
- **Scaling decisions**: Why approximated LRU instead of true LRU? Why LFU was added in 4.0 (real-world scan patterns defeating LRU). When to use `volatile-*` vs `allkeys-*` policies (mixed cache + persistent data workloads).

### 9. 09-expiry-and-ttl.md — Expiry & TTL Mechanism
- **Setting TTL**: `EXPIRE key seconds`, `PEXPIRE key milliseconds`, `EXPIREAT key unix-timestamp`, `PEXPIREAT key unix-ms-timestamp`. Redis 7.0+ added `EXPIRETIME` / `PEXPIRETIME` to retrieve expiry as timestamp. TTL stored as absolute Unix timestamp internally.
- **Passive expiry (lazy deletion)**: On every key access, Redis checks if the key is expired. If yes, deletes it and returns "not found." Simple, zero overhead when keys are not accessed.
- **Active expiry (probabilistic sweep)**: Runs `hz` times per second (default `hz 10`, so 10 times/sec). Each cycle: sample 20 random keys from the set of keys with TTL. Delete expired ones. If >25% were expired, repeat immediately (loop). This ensures expired keys are cleaned up even if never accessed, while bounding CPU usage.
- **Why hybrid?** Passive alone would leak memory (unaccessed expired keys pile up). Active alone would be expensive (scanning all keys). The combination is elegant: passive catches accessed keys immediately, active probabilistically cleans up the rest.
- **Replication of expiry**: Replicas do NOT independently expire keys. The leader generates `DEL` commands and replicates them. This ensures consistency — a key is either expired on all nodes or none. Trade-off: replicas may serve stale reads for briefly-expired keys until the DEL propagates.
- **Contrast with Memcached**: Memcached also uses lazy expiry (check on access). But Memcached has no active expiry sweep — it relies on LRU eviction to reclaim memory from expired items. If memory isn't full, expired-but-unaccessed items sit in memory until accessed (or evicted by LRU pressure).

### 10. 10-scaling-and-performance.md — Scaling Decisions & Performance Trade-offs
This is a **cross-cutting doc** that ties together scaling decisions from all the deep dives into a coherent narrative. It should cover:

- **Vertical vs horizontal scaling**: Redis chose horizontal (add more instances/shards) over vertical (multi-threaded single instance). Why? Single-threaded simplicity, process isolation, independent failure domains. Trade-off: operational complexity of managing a cluster.
- **Read scaling**: Add followers for read replicas. Trade-off: eventual consistency for reads. When is this acceptable? (Caching use case: almost always. Session store: usually. Leaderboard: depends.)
- **Write scaling**: Shard across multiple leaders (Redis Cluster). Each leader handles a subset of hash slots. Write throughput scales linearly with leader count. Trade-off: cross-slot operations become impossible (or require hash tags).
- **Memory scaling**: Each shard holds a subset of data. Total cluster memory = sum of all shards. Trade-off: more shards = more failure domains, more gossip traffic, more operational complexity.
- **Latency vs throughput trade-offs**: Pipelining gives 5-10x throughput but doesn't reduce per-command latency. `MULTI/EXEC` batches commands atomically but adds overhead. Lua scripts reduce round trips but run single-threaded (blocking all other commands during execution).
- **Persistence vs performance**: RDB fork overhead (memory + CPU during fork). AOF fsync overhead (everysec adds ~1ms p99 latency vs no-fsync). Disabling persistence entirely for pure cache use cases.
- **Consistency vs availability**: Redis Cluster is AP (in CAP terms). During network partition, the partition with majority of leaders continues operating. Minority partition stops accepting writes (after `cluster-node-timeout`). Acknowledged writes can be lost if leader fails before replicating. WAIT mitigates but doesn't eliminate this.
- **Network optimization**: TCP_NODELAY (Redis enables by default for low latency), kernel tuning (somaxconn, tcp-backlog default 511). Redis can handle 60,000+ concurrent connections (tested via `redis-benchmark`).
- **Hot key problem and mitigation**: If one key gets disproportionate traffic, one shard is overloaded. Solutions: client-side caching (Redis 6.0+ server-assisted), read replicas for hot read keys, application-level key splitting for hot write keys.
- **Benchmarking**: `redis-benchmark` tool. Verified numbers: ~180K GET/SET ops/sec single-threaded (no pipelining), ~1.5M+ with 16-command pipeline. Mention variables: payload size, command type, network latency, number of connections.

### 11. 11-design-trade-offs.md — Design Philosophy & Trade-off Analysis
This doc should provide an **opinionated analysis** of Redis's design choices — not just "what" but "why this and not that":

- **Single-threaded vs multi-threaded**: Why Antirez chose simplicity over raw throughput. The insight: "for an in-memory store, CPU is not the bottleneck — network and memory are." Memcached's multi-threaded approach works but introduces lock contention, debugging complexity, and non-deterministic behavior. Redis 6.0's compromise: I/O threads for network, single-threaded for commands.
- **RESP text protocol vs binary protocol**: RESP is human-readable (you can telnet to Redis and type commands). Easier to debug, easier to implement clients. Trade-off: slightly more bandwidth than a compact binary protocol. Antirez valued debuggability and ecosystem growth over raw efficiency. RESP3 (Redis 6.0+) adds richer types (maps, sets, booleans) while staying text-based.
- **fork() + COW vs write-ahead log**: Most databases use WAL for durability. Redis uses AOF (which is a WAL) AND RDB (which is fork-based). The fork approach is unique — it gives you a consistent snapshot without stopping writes, but relies on OS-level COW semantics. Trade-off: 2x memory worst case during fork.
- **Hash slots vs consistent hashing**: Redis Cluster uses explicit hash slots; Memcached uses consistent hashing. Hash slots: deterministic, fine-grained migration, explicit ownership. Consistent hashing: automatic rebalancing, no centralized slot map. Redis chose explicit control over automatic convenience.
- **Gossip vs centralized coordination**: Redis Cluster uses gossip (like Cassandra). Alternative: centralized coordinator (like ZooKeeper for Kafka, or etcd). Gossip: no SPOF, eventually consistent. Centralized: faster convergence, strongly consistent, but adds a dependency.
- **AP vs CP**: Redis Cluster is AP — it favors availability over consistency. During a partition, the majority side continues. Acknowledged writes can be lost. This is intentional: for a cache/data-structure-server, availability matters more than linearizability. Contrast with etcd/ZooKeeper which are CP.
- **"Data structure server" vs "cache"**: Redis's positioning as more than a cache. Persistence, replication, rich data structures — these features make Redis usable as a primary data store for certain use cases (session store, rate limiter, leaderboard). Memcached embraces being "just a cache" — simpler, focused, but more limited.
- **Why Redis won**: Network effects (more data structures → more use cases → more adoption → more community → more tooling). Single binary, easy to deploy. Antirez's philosophy of "do the simplest thing that works" resonated.

## CRITICAL: The design must be Redis-centric
Redis is the reference implementation. The design should reflect how Redis actually works — its architecture, data structures, persistence model, replication, clustering, etc. Where Memcached or other caching solutions made different design choices, call those out explicitly as contrasts (e.g., "Memcached chose X instead because Y — here's why Redis chose Z").

## CRITICAL: Iterative build-up (DO NOT skip this)
The architecture MUST evolve iteratively. Each attempt builds on the previous one by identifying a concrete problem and solving it. The candidate should NOT jump to the final architecture. The progression should feel natural:

### Attempt 0: Single-threaded in-memory hash map
- Just a process with a `HashMap<String, String>` in memory
- **Problems found**: No persistence (data lost on restart), no network access (only local), single data structure (only key-value strings)

### Attempt 1: Add network protocol + rich data structures
- TCP server on port 6379 with RESP (Redis Serialization Protocol) — a text-based, human-readable protocol. RESP2 types: Simple Strings (+), Errors (-), Integers (:), Bulk Strings ($), Arrays (*). RESP3 (Redis 6.0+) adds Maps, Sets, Booleans, Big Numbers, Verbatim Strings.
- Support multiple data structures: Strings (SDS, max 512 MB), Lists (quicklist of listpacks), Sets (intset / hashtable), Sorted Sets (skiplist+hashtable / listpack), Hashes (listpack / hashtable).
- Single-threaded event loop using `ae` library (epoll on Linux, kqueue on macOS/BSD) for I/O multiplexing. No threads = no locks = no deadlocks = predictable latency.
- **Why single-threaded?** No locks, no context switching, CPU is rarely the bottleneck for an in-memory store — network and memory are. Verified benchmark: ~180K GET/SET ops/sec single-threaded, ~1.5M+ with 16-command pipelining. Redis can handle 60,000+ concurrent connections.
- **Contrast with Memcached**: Memcached uses multi-threaded architecture (default 4 worker threads, configurable via `-t`). Main listener thread accepts connections, dispatches to workers via a connection queue. Global cache lock (later fine-grained per-slab locking). Scales CPU cores better but adds lock contention and debugging complexity. Redis chose single-threaded simplicity. Redis 6.0+ added I/O threads (`io-threads` config) for network read/write parallelization, but command execution remains single-threaded.
- **Antirez's philosophy**: "I'd rather have a system that is simple and correct than one that is complex and fast. Redis's bottleneck is network I/O, not CPU."
- **Problems found**: Data lost on restart (no durability), single machine memory limit, single point of failure

### Attempt 2: Add persistence
- **RDB snapshots**: `BGSAVE` triggers fork() + copy-on-write. Child process writes the in-memory dataset to an `.rdb` file while parent continues serving requests. Default save triggers in Redis 7.0+: `save 3600 1 300 100 60 10000` (after 3600s if ≥1 change, after 300s if ≥100 changes, after 60s if ≥10000 changes). Fork cost: ~10-20ms per GB of used memory for the fork() syscall itself; COW means only modified pages are duplicated.
- **AOF (Append-Only File)**: Log every write command to a file. Replay on restart to reconstruct state. Three `appendfsync` policies: `always` (fsync every write — safest, ~10x slower), `everysec` (default — fsync once/sec, max 1 sec data loss), `no` (OS decides, fastest, least safe). AOF rewrite: background process produces a minimal AOF. Triggers: `auto-aof-rewrite-percentage 100`, `auto-aof-rewrite-min-size 64mb`.
- **RDB + AOF hybrid (Redis 4.0+)**: `aof-use-rdb-preamble yes`. AOF rewrite produces an RDB-format base + incremental AOF commands appended. On recovery: load RDB portion (fast, binary), then replay AOF tail (minimal commands). Best of both worlds.
- **Multi-part AOF (Redis 7.0+)**: AOF split into base file + incremental files tracked by a manifest. Cleaner file management, atomic replacement during rewrite.
- **Why both RDB and AOF?** RDB = fast recovery (load binary, no replay), compact (good for backups, disaster recovery), but minutes of potential data loss. AOF = ≤1 sec data loss (with `everysec`), auditable, but slower recovery (replay all commands), larger files.
- **fork() is brilliant but has costs**: COW gives a consistent snapshot without stopping writes. But: worst case 2x memory during fork if all pages are modified. Disable Linux Transparent Huge Pages (THP) — THP amplifies COW overhead from 4 KB pages to 2 MB pages.
- **Contrast with Memcached**: Memcached has NO persistence at all — it's a pure volatile cache. If Memcached restarts, the cache is cold. This is a fundamental design philosophy difference: Memcached = pure cache, Redis = cache + data store ("a database that happens to be in memory").
- **Problems found**: Single machine memory limit, single point of failure, can't scale reads

### Attempt 3: Add replication (leader-follower)
- Asynchronous replication: leader processes writes, asynchronously replicates to followers via the replication stream.
- Followers serve read traffic (eventually consistent reads). Followers are read-only by default.
- **Full resync** on first connect: leader does BGSAVE → streams RDB to follower → follower loads it → leader streams buffered writes accumulated during transfer.
- **Partial resync** via replication backlog (circular buffer, default size `repl-backlog-size 1mb`). Uses replication ID + byte offset. If disconnection is brief and the gap fits in the backlog, only the missing commands are streamed. PSYNC2 (Redis 4.0+): dual replication IDs enable partial resync even after failover (follower promoted to leader keeps old replication ID).
- **WAIT command**: `WAIT <numreplicas> <timeout>` blocks until N followers ACK the write. Provides best-effort synchronous replication semantics. **Important**: WAIT is NOT strong consistency — if the leader fails between write and replication, data is lost even if WAIT returned successfully.
- **Sentinel** for automatic failover:
  - Separate process, default port 26379. Deploy ≥3 Sentinels for quorum.
  - Monitors leader + followers. **SDOWN** (Subjective Down): one Sentinel thinks leader is down. **ODOWN** (Objective Down): quorum agrees.
  - Raft-like leader election among Sentinels. Winning Sentinel picks best follower (highest replication offset, lowest `replica-priority`), promotes it, reconfigures others.
  - **Split-brain mitigation**: `min-replicas-to-write N` + `min-replicas-max-lag M` — leader refuses writes if fewer than N replicas are reachable with lag < M seconds. Prevents stale leader from accepting writes.
- **Contrast with Memcached**: No replication at all. Clients handle redundancy. Node dies = data gone.
- **Problems found**: Single leader = write bottleneck, single machine memory limit for the full dataset, split-brain during network partitions

### Attempt 4: Add sharding (Redis Cluster)
- **16,384 hash slots**, distributed across leader nodes. Key → `CRC16(key)` (XMODEM variant, polynomial 0x1021) `mod 16384` → slot → node.
- **Why 16,384 slots?** Balance between metadata size (~2 KB bitmap per node in gossip messages) and granularity. 65,536 would be 8 KB per node — too much gossip overhead. 16,384 supports up to ~1,000 nodes with reasonable slot-per-node ratios.
- Each node is a leader for its assigned slots, with its own followers for HA.
- **Cluster Bus**: Dedicated port (data port + 10,000). Binary gossip protocol. Nodes exchange ping/pong carrying: node ID, IP, port, flags (leader/follower/PFAIL/FAIL), slot bitmap, current epoch, config epoch.
- **Gossip protocol**: Each node pings a random node every second + any node not pinged within `cluster-node-timeout / 2`. Eventually consistent cluster state.
- **Client-side redirection**: `MOVED <slot> <ip>:<port>` (permanent — update local slot map) and `ASK <slot> <ip>:<port>` (temporary — during migration only, send `ASKING` first).
- **Resharding**: Live slot migration, no downtime. Source marks slot MIGRATING, target marks IMPORTING. Keys moved atomically with `MIGRATE`. Clients experience ASK redirections during migration.
- **Hash tags**: `{tag}` in key forces hashing only the tag content. Enables multi-key operations on co-located keys.
- **Failure detection**: PFAIL (one node thinks another is down after `cluster-node-timeout`, default 15,000 ms) → FAIL (majority of leaders agree) → automatic follower election + promotion. Epoch-based config versioning prevents conflicts.
- **Contrast with Memcached**: Memcached uses client-side consistent hashing (ketama algorithm) — servers are completely unaware of each other. No resharding support, no server-side clustering, no failover. Adding/removing servers: ~1/N keys rehashed (with consistent hashing), but no coordination, no migration, no redirections. Simpler but less capable.
- **Trade-offs**: No multi-key ops across slots (unless hash tags). Only DB 0. Async replication means acknowledged writes can be lost during failover. Cross-slot atomicity requires Lua scripts with hash tags.
- **Problems found**: No strong consistency (async replication can lose acknowledged writes during failover), hot key problem, operational complexity, client must be "cluster-aware"

### Attempt 5: Production hardening
- **Memory management**: `maxmemory` limit with 8 eviction policies (noeviction, allkeys-lru/lfu/random, volatile-lru/lfu/random/ttl). Approximated LRU: sample `maxmemory-samples` keys (default 5), evict oldest. LFU (Redis 4.0+): logarithmic counter with decay, better for scan-resistant workloads.
- **TTL and expiry**: Passive expiry (check on access) + active expiry (probabilistic sweep: `hz` times/sec, default 10; sample 20 random keys with TTL, delete expired, repeat if >25% were expired). Replicas receive DEL from leader — no independent expiry.
- **Pub/Sub**: Classic Pub/Sub (fan-out to all cluster nodes). Sharded Pub/Sub (Redis 7.0+): messages routed by channel hash slot, scales with cluster.
- **Lua scripting**: `EVAL` / `EVALSHA` for atomic multi-step operations. Runs on the single thread — long scripts block everything. Redis 7.0+ added Functions API (persistent, named functions).
- **Client-side caching (Redis 6.0+)**: Server-assisted invalidation. Client subscribes via `CLIENT TRACKING ON`. Server sends invalidation messages when tracked keys change. Two modes: default (per-key tracking) and broadcasting (prefix-based). Dramatically reduces read load for hot keys.
- **Security**: ACLs (Redis 6.0+, enhanced in 7.0): per-user command restrictions, key pattern restrictions. `AUTH` command. TLS support. `rename-command` to disable dangerous commands (FLUSHALL, DEBUG, CONFIG).
- **Monitoring**: `INFO` (stats sections), `SLOWLOG` (log commands exceeding `slowlog-log-slower-than`, default 10ms), `LATENCY MONITOR`, `MEMORY DOCTOR`, `CLIENT LIST`, `COMMAND DOCS` (Redis 7.0+).
- **Operational concerns**: Redis 7.0+ features: listpack replacing ziplist everywhere, multi-part AOF, sharded Pub/Sub, ACL v2 with selectors, Functions API.

## CRITICAL: Accuracy requirements

I DO NOT TRUST AI-GENERATED FACTS ABOUT SPECIFIC SYSTEMS. Every concrete claim about Redis internals must be verifiable. Specifically:

1. **Use WebSearch and WebFetch tools** to look up Redis official documentation and source code BEFORE writing. Search for:
   - "Redis architecture site:redis.io"
   - "Redis persistence RDB AOF site:redis.io"
   - "Redis replication site:redis.io"
   - "Redis Cluster specification site:redis.io"
   - "Redis Sentinel site:redis.io"
   - "Redis data structures internals"
   - "Redis RESP protocol specification"
   - "Redis single-threaded event loop"
   - "Redis memory management eviction policies site:redis.io"
   - "Redis 6.0 threaded I/O"
   - "Redis vs Memcached architecture comparison"
   - "Redis Cluster hash slots CRC16"
   - "Redis copy-on-write fork persistence"
   - "Antirez Redis design decisions" (Salvatore Sanfilippo's blog posts)
   - "Redis 7.0 new features"

   **BLANKET PERMISSION**: You have full permission to use WebSearch and WebFetch to read any online documentation, blog posts, or source code. Do NOT ask the user for permission to read — just read. This applies to redis.io, github.com/redis/redis, antirez.com, and any other reference source. Read as many pages as needed to verify facts.

2. **For every concrete number** (hash slot count, default port, replication backlog size, max key size, throughput benchmarks), verify against redis.io or official sources. If you cannot verify a number, explicitly write "[UNVERIFIED — check redis.io docs]" next to it.

3. **For every claim about Redis internals** (event loop model, data structure encodings, memory allocation), if it's not from an official source, mark it as "[INFERRED — not officially documented]".

4. **CRITICAL: Do NOT confuse Redis with Memcached.** These are different systems with different philosophies:
   - Redis: single-threaded (command execution), rich data structures, persistence, replication, clustering, Pub/Sub — positioned as a "data structure server" / "in-memory database"
   - Memcached: multi-threaded, simple key-value only (strings), no persistence, no built-in replication — positioned as a "pure volatile cache"
   - When discussing design decisions, ALWAYS explain WHY Redis chose its approach and how Memcached's different choice reflects a different philosophy.

## Key topics to cover

### Requirements & Scale
- In-memory key-value store with sub-millisecond latency
- Rich data structures: Strings, Lists, Sets, Sorted Sets, Hashes, Streams, HyperLogLog, Bitmaps, Geospatial
- Persistence options (RDB, AOF, hybrid)
- Scale: ~180K ops/sec per node (single-threaded, no pipelining), ~1.5M+ with pipelining. Hundreds of GB per node. Cluster scales to ~1,000 nodes.
- Use cases: caching, session store, rate limiting, leaderboards, pub/sub messaging, real-time analytics

### Architecture deep dives (create separate docs as listed in "Files to create" above)

### Design evolution (iterative build-up — the most important part)
- Attempt 0: In-memory hash map
- Attempt 1: Network protocol + data structures + single-threaded event loop
- Attempt 2: Persistence (RDB + AOF)
- Attempt 3: Replication + Sentinel (HA)
- Attempt 4: Sharding (Redis Cluster)
- Attempt 5: Production hardening (eviction, expiry, security, monitoring)

Each attempt MUST:
1. Start by identifying concrete problems with the previous attempt
2. Propose a solution
3. Explain WHY this approach (and why not the alternative — mention Memcached's choice where relevant)
4. End with "what's still broken?" to motivate the next attempt

### Consistency & Replication
- Asynchronous replication by default (leader → followers, eventual consistency)
- WAIT command for best-effort synchronous replication (not true strong consistency)
- Redis Cluster: no strong consistency guarantee — acknowledged writes can be lost during failover
- Split-brain scenarios and how Sentinel/Cluster handle them (or don't) — `min-replicas-to-write` mitigation
- Contrast: Memcached has no replication at all — clients handle redundancy

## What NOT to do
- Do NOT treat Redis as "just a cache" — it's an in-memory data structure server with persistence. Frame it accordingly.
- Do NOT confuse Redis with Memcached. Highlight differences, don't blur them.
- Do NOT jump to the final Redis Cluster architecture. Build it step by step.
- Do NOT make up internal implementation details — verify or mark as inferred.
- Do NOT skip the iterative build-up (Attempt 0 → 1 → 2 → 3 → 4 → 5).
- Do NOT write a Wikipedia article. This is an INTERVIEW SIMULATION with dialogue.
- Do NOT describe features without explaining WHY they exist (what problem they solve).
- Do NOT ask the user for permission to read online documentation — blanket permission is granted (see Accuracy requirements section).
