# Scaling Decisions & Performance Trade-offs

Cross-cutting document tying together scaling decisions from all deep dives. Every scaling choice is a trade-off — this doc makes those trade-offs explicit.

---

## 1. Vertical vs Horizontal Scaling

Redis chose **horizontal scaling**: run multiple independent instances, coordinate them with Redis Cluster.

The alternative is Memcached's approach: a single multi-threaded process that uses all cores on one machine.

**Why horizontal?**
- Single-threaded simplicity per instance (no locks, no race conditions)
- Process isolation: one instance crashing doesn't take down others
- Independent failure domains: lose one shard, not the whole cache
- Linear scaling model: add more instances to add more capacity

**Trade-off**: operational complexity of managing a cluster vs simplicity of one fat instance.

| Factor | Vertical (Multi-threaded) | Horizontal (Multi-instance) |
|--------|---------------------------|----------------------------|
| Complexity per process | High (locks, threads) | Low (single-threaded) |
| Operational complexity | Low (one process) | High (cluster management) |
| Failure blast radius | Total (process dies = everything dies) | Partial (one shard dies) |
| Max capacity | Limited by one machine | Limited by number of machines |
| Scaling granularity | Coarse (bigger machine) | Fine (add one more shard) |

**Rule of thumb**: one Redis instance per CPU core, each with its own `maxmemory` setting. A 16-core machine runs 16 Redis instances, not one Redis instance using 16 threads.

---

## 2. Read Scaling

**Mechanism**: add follower replicas. Clients read from followers, write to the leader.

**Trade-off**: eventual consistency for reads. Replication is asynchronous — a follower might serve stale data.

| Use Case | Stale Reads Acceptable? | Why |
|----------|------------------------|-----|
| Caching | Almost always yes | Cache is inherently stale relative to source of truth |
| Session store | Usually yes | Session data changes infrequently during a request |
| Leaderboard | Depends | Slight lag in scores is usually fine; real-time ranking may not be |
| Rate limiting | Usually yes | Slight over-count is better than under-count |
| Distributed lock | **No** | Stale read could show lock as free when it's held |

**When stale reads are NOT acceptable**: use `WAIT` to block until N replicas acknowledge the write, or use an external consensus system.

**Important limitation**: read replicas don't help with hot WRITE keys. If one key is hammered with writes, all writes go to the same leader shard. Replicas only offload reads.

---

## 3. Write Scaling

**Mechanism**: shard across multiple leaders using Redis Cluster. Each leader owns a subset of the 16,384 hash slots.

Write throughput scales approximately linearly with leader count:
- 1 leader: ~180K writes/sec
- 3 leaders: ~540K writes/sec
- 10 leaders: ~1.8M writes/sec

**Trade-off**: cross-slot operations become impossible unless you use hash tags.

```
# These two keys land on different slots — can't MGET them in Cluster mode
GET user:1001:name
GET product:5002:price

# Hash tags force same slot — works but couples unrelated data
GET {user:1001}:name
GET {user:1001}:cart
```

**Fundamental limit**: you cannot scale writes for a single hot key. That key lives on exactly one shard. Application-level sharding (key splitting) is the only mitigation — see Section 9.

---

## 4. Memory Scaling

Total cluster memory = sum of all shard memory allocations.

| Cluster Size | Memory per Shard | Total Usable Memory |
|-------------|-----------------|-------------------|
| 3 leaders | 32 GB each | ~96 GB |
| 10 leaders | 32 GB each | ~320 GB |
| 50 leaders | 64 GB each | ~3.2 TB |
| 100 leaders | 64 GB each | ~6.4 TB |

**Trade-offs of more shards**:
- More failure domains (more things that can break)
- More gossip traffic (each node pings every other node)
- More operational complexity (monitoring, upgrades, resharding)
- More cross-slot limitations

**Practical limit**: ~1,000 nodes in a Redis Cluster. Beyond that, gossip protocol bandwidth becomes a bottleneck. Each node sends a PING to a random node every second, and each PING/PONG carries cluster state metadata. At 1,000 nodes, the metadata in each message is substantial.

---

## 5. Latency vs Throughput Trade-offs

### Pipelining

Send multiple commands without waiting for individual responses.

```
# Without pipelining: 4 round trips
SET a 1      → OK
SET b 2      → OK
SET c 3      → OK
GET a        → "1"

# With pipelining: 1 round trip
[SET a 1, SET b 2, SET c 3, GET a] → [OK, OK, OK, "1"]
```

| Metric | Without Pipeline | With Pipeline (16 cmds) |
|--------|-----------------|------------------------|
| Throughput | ~180K ops/sec | ~1.5M+ ops/sec |
| Per-command latency | Same (~0.1ms local) | Same (~0.1ms local) |
| Network round trips | 1 per command | 1 per batch |
| Memory usage | Low | Higher (buffered responses) |

Pipelining does NOT reduce per-command latency. It amortizes the network round-trip time across multiple commands, increasing throughput.

### MULTI/EXEC (Transactions)

Batches commands for atomic execution. Commands are queued, then executed all-at-once.

- Adds overhead: queuing phase + execution phase
- Provides atomicity (no other command interleaves) but NOT rollback
- Useful when you need "all or nothing" execution

### Lua Scripts

Reduce round trips AND execute atomically on the server.

```lua
-- Rate limiter in one round trip
local current = redis.call('INCR', KEYS[1])
if current == 1 then
    redis.call('EXPIRE', KEYS[1], ARGV[1])
end
return current
```

**Critical trade-off**: Lua scripts run single-threaded and block ALL other commands during execution. A slow Lua script stalls the entire Redis instance.

**Rule**: keep Lua scripts under 5ms execution time. If your script does heavy computation, move that logic to the application.

### Summary Table

| Technique | Throughput Gain | Latency Impact | Atomicity | Complexity |
|-----------|----------------|----------------|-----------|-----------|
| No optimization | Baseline | Baseline | Per-command | Low |
| Pipelining | 5-10x | No change (amortized RTT) | None | Low |
| MULTI/EXEC | Moderate | Slight increase | Yes (no rollback) | Medium |
| Lua scripts | High (fewer round trips) | Blocks other commands | Yes | High |

---

## 6. Persistence vs Performance

Every persistence option adds overhead. The question is how much and whether it matters for your use case.

### RDB (Snapshots)

- `fork()` call blocks the main thread for **~10-20ms per GB** of memory used
- 25 GB instance → ~250-500ms freeze during fork
- After fork, background child writes to disk — minimal impact on parent
- COW (Copy-On-Write) can double memory usage in worst case (heavy writes during snapshot)

### AOF (Append-Only File)

| fsync Policy | Latency Impact | Data Loss Window |
|-------------|---------------|-----------------|
| `always` | +2-5ms per write (disk fsync on every command) | Zero (in theory) |
| `everysec` | +~1ms p99 (background fsync) | Up to 1 second |
| `no` | Negligible (OS decides when to flush) | Up to 30 seconds (OS buffer) |

### Decision Framework

| Use Case | Persistence Config | Rationale |
|----------|-------------------|-----------|
| Pure cache (rebuild from DB) | No persistence | Maximum performance; data is disposable |
| Cache with warm restart | RDB only, save every 15 min | Fast restart; acceptable data loss |
| Session store | AOF everysec | Lose at most 1 second of sessions |
| Primary data store | AOF everysec + RDB | Durability + fast recovery from RDB |
| Financial/critical data | AOF always + RDB | Minimal data loss; accept latency cost |

### Disabling Persistence Entirely

For pure cache mode, disable both RDB and AOF:

```
save ""
appendonly no
```

This gives the best possible performance: no fork overhead, no fsync latency, no background I/O.

---

## 7. Consistency vs Availability (CAP)

Redis Cluster is **AP** (Availability + Partition tolerance). It sacrifices consistency under network partitions.

### During a Network Partition

```
Normal:     [Client] → [Leader A] → [Follower A']
                         (majority partition)

Partition:  [Client] → [Leader A]    |    [Follower A']
                       (majority)    |    (minority)

            Majority side: continues serving reads AND writes
            Minority side: stops accepting writes after cluster-node-timeout
                           (default 15 seconds)
```

### Write Loss Scenario

```
1. Client sends SET x 42 to Leader A
2. Leader A responds OK to client
3. Leader A crashes BEFORE replicating to Follower A'
4. Follower A' gets promoted to leader
5. SET x 42 is permanently lost — client got OK but data is gone
```

### WAIT Mitigation

```
SET x 42
WAIT 1 5000    # Wait for 1 replica to ACK, timeout 5 seconds
```

WAIT reduces the window but does NOT eliminate it. The leader can die in the microseconds between the replica ACK and the WAIT response to the client.

### CAP Comparison

| System | CAP Choice | Behavior During Partition |
|--------|-----------|--------------------------|
| Redis Cluster | AP | Majority continues; minority stops writes |
| etcd | CP | Refuses writes if no quorum |
| ZooKeeper | CP | Refuses writes if no quorum |
| Cassandra | AP (tunable) | Continues with tunable consistency |
| DynamoDB | AP (tunable) | Continues; strong consistency option for reads |

**The trade-off is intentional**: for cache and data-structure workloads, a few lost writes during a rare leader failure are acceptable. Blocking all writes until quorum is established (CP) is usually worse for cache use cases.

---

## 8. Network Optimization

### TCP_NODELAY

Redis enables `TCP_NODELAY` by default. This disables Nagle's algorithm, which normally buffers small packets to reduce overhead.

- With Nagle: up to 40ms delay for small writes (waiting to batch)
- Without Nagle (Redis default): every response sent immediately

For an in-memory store doing sub-millisecond operations, a 40ms Nagle delay would be catastrophic.

### tcp-backlog

The listen backlog for incoming connections. Default: 511.

```
# Redis config
tcp-backlog 511

# Kernel must match or exceed
# Linux: /proc/sys/net/core/somaxconn
sysctl -w net.core.somaxconn=1024
```

Under high connection rates (thousands of new connections per second), a small backlog causes connection failures. Increase both the Redis config and the kernel setting.

### Connection Pooling

TCP handshake cost: ~0.5ms (local) to ~100ms+ (cross-region).

For a Redis command that takes 0.1ms, spending 0.5ms on a TCP handshake is a 6x overhead. Connection pooling amortizes this cost.

| Approach | New Connections/sec | Latency Overhead |
|----------|-------------------|-----------------|
| No pooling | 1 per command | +0.5ms per command |
| Connection pool (size 10) | ~0 (reuse) | ~0 (already connected) |
| Connection pool (size 100) | ~0 (reuse) | ~0; higher idle memory |

**Rule of thumb**: pool size = expected concurrent commands. Oversized pools waste file descriptors and memory; undersized pools cause command queuing.

### Kernel Tuning Checklist

| Parameter | Default | Recommended | Why |
|-----------|---------|------------|-----|
| `net.core.somaxconn` | 128 | 1024+ | Match tcp-backlog |
| `vm.overcommit_memory` | 0 | 1 | Prevent fork failure for RDB/AOF |
| `transparent_hugepage` | always | never | Avoid latency spikes from THP defrag |
| `net.ipv4.tcp_max_syn_backlog` | 128 | 1024+ | Handle SYN floods during connection bursts |

---

## 9. Hot Key Problem & Mitigation

### The Problem

One key receives disproportionate traffic. Since a key lives on exactly one shard, that shard becomes the bottleneck while others sit idle.

```
# Example: viral tweet counter
INCR tweet:viral:12345:likes    # 50,000 writes/sec to ONE shard
```

### Read-Hot Key Mitigation

| Technique | How It Works | Trade-off |
|-----------|-------------|-----------|
| Read replicas | Route reads to followers | Eventual consistency; replication lag |
| Client-side caching | Cache value in application memory | Stale data until invalidated |
| Server-assisted invalidation | Redis 6.0+ `CLIENT TRACKING` | Server tracks what clients cached; sends invalidation on change |

**Client-side caching (Redis 6.0+)**:

```
CLIENT TRACKING ON

# Client caches GET user:1001:name locally
# When another client modifies user:1001:name, Redis sends invalidation
# Client evicts from local cache, fetches fresh value on next access
```

Two modes:
- **Default mode**: server tracks per-key, per-client. Precise but uses memory on server.
- **Broadcasting mode**: server broadcasts invalidations by key prefix. Less memory, more invalidation messages.

### Write-Hot Key Mitigation

No amount of replicas helps — writes always go to the leader.

**Application-level key splitting**:

```
# Instead of one counter:
INCR tweet:viral:12345:likes

# Split across N sharded sub-keys:
INCR tweet:viral:12345:likes:{0}    # shard 0
INCR tweet:viral:12345:likes:{1}    # shard 1
...
INCR tweet:viral:12345:likes:{9}    # shard 9

# To read the total:
SUM of MGET tweet:viral:12345:likes:{0..9}
```

| Approach | Write Scaling | Read Complexity | Consistency |
|----------|--------------|----------------|-------------|
| Single key | 1 shard | Simple GET | Strong (single key) |
| Split into N keys | N shards | Application-level SUM | Eventually consistent (SUM races with INCRs) |

---

## 10. Benchmarking

### redis-benchmark Tool

Ships with Redis. Useful for establishing baseline performance.

```bash
# Default benchmark (all tests)
redis-benchmark

# Specific test: SET commands, 1M requests, 50 connections
redis-benchmark -t set -n 1000000 -c 50

# With pipelining (16 commands per batch)
redis-benchmark -t set -n 1000000 -c 50 -P 16

# Custom payload size (1KB values)
redis-benchmark -t set -n 1000000 -d 1024
```

### Verified Reference Numbers

Single Redis instance, single-threaded, no pipelining, 3-byte payload, local loopback:

| Command | Throughput (ops/sec) | Avg Latency |
|---------|---------------------|-------------|
| GET | ~180,000 | ~0.1ms |
| SET | ~180,000 | ~0.1ms |
| INCR | ~180,000 | ~0.1ms |
| LPUSH | ~180,000 | ~0.1ms |
| ZADD | ~120,000 | ~0.15ms |
| LRANGE 100 | ~50,000 | ~0.3ms |
| LRANGE 600 | ~15,000 | ~1.0ms |

With 16-command pipeline:

| Command | Throughput (ops/sec) |
|---------|---------------------|
| GET | ~1,500,000+ |
| SET | ~1,500,000+ |

### Variables That Affect Benchmarks

| Variable | Impact |
|----------|--------|
| Payload size | Larger payloads → lower throughput (more bytes to serialize/send) |
| Command type | Simple (GET/SET) vs complex (ZADD, LRANGE) |
| Network latency | Local loopback (~0.05ms) vs cross-AZ (~1ms) vs cross-region (~50ms+) |
| Connection count | Too few: underutilized. Too many: context switching overhead |
| Pipelining depth | Diminishing returns beyond ~16-32 commands per batch |
| Persistence | AOF always vs everysec vs disabled |
| TLS | Adds ~25-50% overhead (encryption/decryption) |

### Benchmark Flags Reference

| Flag | Meaning | Example |
|------|---------|---------|
| `-t` | Specific test (get, set, incr, lpush, etc.) | `-t set,get` |
| `-n` | Total number of requests | `-n 1000000` |
| `-c` | Concurrent connections | `-c 50` |
| `-P` | Pipeline depth | `-P 16` |
| `-d` | Data payload size in bytes | `-d 1024` |
| `-q` | Quiet mode (just show ops/sec) | `-q` |
| `--csv` | Output in CSV format | `--csv` |
| `-r` | Random key range | `-r 1000000` |

**Always benchmark YOUR workload, not synthetic defaults.** The default redis-benchmark uses tiny payloads, simple commands, and local loopback. Your production workload likely has larger payloads, mixed commands, network latency, and TLS — all of which reduce throughput significantly.
