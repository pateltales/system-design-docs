# Threading Model & Event Loop — Deep Dive

## Table of Contents
1. [Why Single-Threaded Command Execution?](#1-why-single-threaded-command-execution)
2. [The `ae` Event Library](#2-the-ae-event-library)
3. [Benchmark Numbers](#3-benchmark-numbers)
4. [Redis 6.0+ I/O Threads](#4-redis-60-io-threads)
5. [Pipelining](#5-pipelining)
6. [Contrast with Memcached's Multi-Threaded Model](#6-contrast-with-memcacheds-multi-threaded-model)
7. [Scaling Decision: Scale-Out vs Scale-Up](#7-scaling-decision-scale-out-vs-scale-up)

---

## 1. Why Single-Threaded Command Execution?

Redis executes all commands on a single thread. This is not a limitation — it is a deliberate
design choice driven by the nature of in-memory data stores.

### The bottleneck is NOT the CPU

For an in-memory key-value store, the dominant costs are:

| Operation              | Typical Latency      |
|------------------------|----------------------|
| Hash table lookup      | ~50-100 ns           |
| Skiplist insert        | ~200-500 ns          |
| Network round-trip     | ~100,000-500,000 ns  |
| System call (read/write) | ~1,000-5,000 ns    |

A single CPU core can execute millions of hash lookups per second. The time spent executing
the command is a tiny fraction of the total request lifecycle. Network I/O and memory bandwidth
are the real bottlenecks — not CPU compute.

### What single-threaded buys you

1. **No locks.** Every data structure operation is inherently atomic. `LPUSH` + `LTRIM` in a
   pipeline cannot interleave with another client's modification. No mutexes, no spinlocks,
   no CAS loops, no deadlocks.

2. **No context switching.** Thread context switches cost ~1-5 microseconds each. With thousands
   of concurrent clients and multi-threaded access, this adds up. A single-threaded model
   eliminates this entirely.

3. **Predictable latency.** No lock contention means no surprise latency spikes. Each command
   runs to completion without preemption. This makes p99 latency easier to reason about.

4. **Simpler code.** No concurrent data structure bugs. No race conditions. The entire Redis
   codebase is dramatically simpler than it would be with fine-grained locking.

### Antirez's design philosophy

Salvatore Sanfilippo (antirez) optimized for **simplicity over raw throughput**. A single Redis
instance doing 180K ops/sec covers the vast majority of use cases. When you need more, you
scale out with multiple instances — not by adding threads and locks to a single instance.

> "I think the mass of the Redis code is not suitable for threads... the whole idea of Redis
> is that data structures efficiency and simplicity produce a very good result."
> — antirez

---

## 2. The `ae` Event Library

Redis does not use libevent, libev, or libuv. It ships its own minimal event loop library
called **ae** (implemented in `ae.c`, ~700 lines of code).

### Why a custom library?

- Minimal footprint — no unnecessary abstractions
- Tightly integrated with Redis internals
- No external dependency management
- Exactly the features Redis needs, nothing more

### OS I/O multiplexer backends

`ae` wraps the best available I/O multiplexer on each platform:

| Platform      | Backend    | File              | Characteristics                      |
|---------------|------------|-------------------|--------------------------------------|
| Linux 2.6+    | `epoll`    | `ae_epoll.c`      | O(1) per event, edge/level triggered |
| macOS / BSD   | `kqueue`   | `ae_kqueue.c`     | O(1) per event, unified API          |
| Solaris       | `evport`   | `ae_evport.c`     | Event ports                          |
| Fallback      | `select`   | `ae_select.c`     | O(N) scan, 1024 FD limit            |

Selection happens at compile time via `#ifdef` in `ae.c`. On modern Linux, you always get epoll.

### The Reactor Pattern

`ae` implements the classic **reactor pattern**: a single thread monitors all file descriptors
for readiness and dispatches events to registered handlers.

```
                    ┌─────────────────────────────────────┐
                    │          Redis Main Thread           │
                    │                                      │
                    │  ┌──────────────────────────────┐    │
                    │  │       ae Event Loop           │    │
                    │  │                               │    │
                    │  │  1. processTimeEvents()       │    │
                    │  │     - Expiration callbacks     │    │
                    │  │     - serverCron (100ms)       │    │
                    │  │                               │    │
                    │  │  2. aeApiPoll(timeout)         │    │
                    │  │     - epoll_wait / kevent      │    │
                    │  │     - Blocks until events or   │    │
                    │  │       timeout                  │    │
                    │  │                               │    │
                    │  │  3. Process file events        │    │
                    │  │     - AE_READABLE → read cmd   │    │
                    │  │     - AE_WRITABLE → send reply │    │
                    │  │                               │    │
                    │  │  4. beforeSleep()              │    │
                    │  │     - Flush AOF buffer         │    │
                    │  │     - Handle cluster msgs      │    │
                    │  │     - Incremental rehashing    │    │
                    │  │                               │    │
                    │  └──────────┬───────────────────┘    │
                    │             │                         │
                    │             ▼                         │
                    │       Loop forever                    │
                    └─────────────────────────────────────┘
```

### Main loop pseudocode

```
while (server is running):
    # 1. Process time-based events (cron jobs, expiration)
    processTimeEvents()

    # 2. Calculate poll timeout (next time event deadline)
    timeout = nearest_time_event - now

    # 3. Block on I/O multiplexer
    ready_events = aeApiPoll(timeout)   # epoll_wait / kevent

    # 4. Dispatch ready file events
    for event in ready_events:
        if event.mask & AE_READABLE:
            event.readHandler(fd)       # readQueryFromClient
        if event.mask & AE_WRITABLE:
            event.writeHandler(fd)      # sendReplyToClient

    # 5. Housekeeping
    beforeSleep()                       # AOF flush, cluster, rehash
```

### How thousands of connections are handled

A single call to `epoll_wait` can return readiness for thousands of file descriptors at once.
Each client connection is just a file descriptor with a registered read/write handler. The
event loop processes them sequentially in the dispatch phase — no thread per connection needed.

```
  Client A ──fd=5──┐
  Client B ──fd=6──┤
  Client C ──fd=7──┼──► epoll_wait() ──► [fd=5: READABLE, fd=7: READABLE]
  Client D ──fd=8──┤                          │
  Client E ──fd=9──┘                          ▼
                                    Process fd=5: parse command, execute, queue reply
                                    Process fd=7: parse command, execute, queue reply
                                    Write replies when fds become WRITABLE
```

### Key event types in Redis

| Event Type      | Handler                  | Triggered When                        |
|-----------------|--------------------------|---------------------------------------|
| Accept          | `acceptTcpHandler`       | New client connects to listen socket  |
| Read            | `readQueryFromClient`    | Client sends command data             |
| Write           | `sendReplyToClient`      | Socket buffer ready for reply data    |
| Time            | `serverCron`             | Every 100ms (configurable via `hz`)   |
| Module          | Module-registered events | Custom module triggers                |

### `serverCron` — the heartbeat

Runs every `1000/hz` milliseconds (default `hz=10`, so every 100ms). Responsibilities:

- Expire keys (lazy + active expiration sampling)
- Rehash dict tables incrementally
- Trigger RDB/AOF background saves
- Replication heartbeat (PING replicas)
- Cluster heartbeat (PING/PONG gossip)
- Client timeout handling
- Memory usage reporting and eviction
- Resize hash tables if load factor thresholds are crossed

---

## 3. Benchmark Numbers

These numbers come from `redis-benchmark` on modern hardware (circa 2023-2024). All numbers
are approximate and vary significantly with hardware, OS, network, and configuration.

### Single-threaded baseline (no pipelining)

| Metric                   | Value           | Conditions                              |
|--------------------------|-----------------|-----------------------------------------|
| GET/SET throughput       | ~150-180K ops/s | 3-byte payload, single connection       |
| GET/SET throughput       | ~100-120K ops/s | 256-byte payload, single connection     |
| LPUSH                    | ~150-170K ops/s | Single element per command              |
| Average latency          | ~0.1-0.3 ms    | Local loopback, no pipelining           |
| p99 latency              | ~0.5-1.0 ms    | Local loopback, no pipelining           |

### With pipelining

| Pipeline Depth | Approximate Throughput | Improvement     |
|----------------|------------------------|-----------------|
| 1 (none)       | ~180K ops/s            | baseline        |
| 4              | ~500K ops/s            | ~2.8x           |
| 16             | ~1.2-1.5M ops/s        | ~7-8x           |
| 64             | ~2.0-2.5M ops/s        | ~12-14x         |

### Connection scaling

| Concurrent Connections | Throughput Impact        |
|------------------------|--------------------------|
| 1-50                   | Near-linear scaling      |
| 50-1,000               | Diminishing returns      |
| 1,000-10,000           | Slight degradation       |
| 10,000-60,000+         | Sustained (with tuning)  |

Redis can handle 60,000+ concurrent connections. Beyond ~10K, tune `maxclients`, OS `ulimit`,
and TCP backlog.

### What affects throughput

- **Payload size**: Larger values → more time in network I/O → lower ops/sec
- **Command complexity**: O(1) GET vs O(N) LRANGE of 1000 elements
- **Network latency**: Loopback vs cross-datacenter
- **Number of connections**: More connections = more FDs to poll
- **Pipelining**: Amortizes syscall overhead, massive throughput gain
- **TLS**: ~30-50% throughput reduction when encryption is enabled

---

## 4. Redis 6.0+ I/O Threads

Redis 6.0 introduced **threaded I/O** — but command execution remains single-threaded. This
is a critical distinction.

### Configuration

```
# redis.conf
io-threads 4              # Total threads including main (1 = disabled, default)
io-threads-do-reads yes   # Also parallelize reads (default: no, only writes)
```

### Architecture: Read-Execute-Write phases

```
  ┌─────────────────────────────────────────────────────────────┐
  │                    Request Lifecycle                         │
  │                                                             │
  │  ┌──────────────┐  ┌──────────────┐  ┌──────────────┐      │
  │  │  READ phase  │  │ EXECUTE phase│  │  WRITE phase │      │
  │  │  (parallel)  │→ │  (serial)    │→ │  (parallel)  │      │
  │  └──────────────┘  └──────────────┘  └──────────────┘      │
  │                                                             │
  │  I/O Thread 0 ─┐   Main thread     ┌─ I/O Thread 0        │
  │  I/O Thread 1 ─┤   executes ALL    ├─ I/O Thread 1        │
  │  I/O Thread 2 ─┤   commands        ├─ I/O Thread 2        │
  │  Main Thread  ─┘   sequentially    └─ Main Thread         │
  └─────────────────────────────────────────────────────────────┘
```

### Detailed flow

```
Phase 1: READ (parallel)
─────────────────────────
  Main thread distributes pending clients across I/O threads.
  Each I/O thread reads data from its assigned sockets (recv syscall)
  and parses the RESP protocol into command arguments.

    I/O Thread 0:  read(fd=5), read(fd=9),  parse → [SET key1 val1]
    I/O Thread 1:  read(fd=6), read(fd=10), parse → [GET key2]
    I/O Thread 2:  read(fd=7), read(fd=11), parse → [INCR counter]
    Main Thread:   read(fd=8), read(fd=12), parse → [LPUSH list v]
                                          │
                                     BARRIER (spin-wait)
                                          │
                                          ▼
Phase 2: EXECUTE (serial — main thread only)
──────────────────────────────────────────────
  Main thread processes ALL parsed commands sequentially.
  No locks needed. Data structure invariants preserved.

    Main Thread:  execute(SET key1 val1)
                  execute(GET key2)
                  execute(INCR counter)
                  execute(LPUSH list v)
                  → Queue responses in output buffers
                                          │
                                     BARRIER (spin-wait)
                                          │
                                          ▼
Phase 3: WRITE (parallel)
──────────────────────────
  I/O threads write response buffers to their assigned sockets.

    I/O Thread 0:  write(fd=5), write(fd=9)
    I/O Thread 1:  write(fd=6), write(fd=10)
    I/O Thread 2:  write(fd=7), write(fd=11)
    Main Thread:   write(fd=8), write(fd=12)
```

### Why no locks are needed

Command execution is **still single-threaded**. The I/O threads only touch:
- Socket read buffers (each thread reads its own assigned sockets)
- Socket write buffers (each thread writes its own assigned sockets)

No I/O thread ever touches the keyspace, database, or any shared data structure. The
barriers between phases ensure strict ordering.

### When I/O threads help

| Scenario                                    | Benefit        |
|---------------------------------------------|----------------|
| High connection count (1000+)               | Significant    |
| Large payloads (bulk reads/writes)          | Significant    |
| TLS enabled (encryption is CPU-intensive)   | Significant    |
| Few connections, small payloads             | Negligible     |
| CPU-bound commands (SORT, KEYS)             | None           |

### Recommended settings

| Cores Available | `io-threads` Setting | Reasoning                             |
|-----------------|----------------------|---------------------------------------|
| 1-2             | 1 (disabled)         | Overhead exceeds benefit              |
| 4               | 2-3                  | Leave cores for OS, background tasks  |
| 8+              | 4-6                  | Diminishing returns beyond 6-8        |

> Do not set `io-threads` higher than the number of available CPU cores. Redis documentation
> recommends no more than 8 I/O threads even on large machines.

---

## 5. Pipelining

Pipelining is the single most effective throughput optimization for Redis clients.

### The problem: network round-trip overhead

Without pipelining, each command requires a full round-trip:

```
Without pipelining (3 commands = 3 round-trips):
─────────────────────────────────────────────────
Client                              Server
  │── SET key1 val1 ──────────────────►│
  │                                    │ execute
  │◄──────────────────────── +OK ──────│
  │                                    │
  │── SET key2 val2 ──────────────────►│
  │                                    │ execute
  │◄──────────────────────── +OK ──────│
  │                                    │
  │── GET key1 ───────────────────────►│
  │                                    │ execute
  │◄──────────────────── "val1" ───────│

  Total time: 3 × (network RTT + execution time)
  If RTT = 0.2ms, execution = 0.005ms:
  Total ≈ 3 × 0.205ms = 0.615ms
```

### The solution: batch commands, batch responses

```
With pipelining (3 commands = 1 round-trip):
────────────────────────────────────────────
Client                              Server
  │── SET key1 val1 ─┐                │
  │── SET key2 val2 ─┼───────────────►│
  │── GET key1 ──────┘                │ execute all
  │                                    │
  │                    ┌──── +OK ──────│
  │◄───────────────────┼──── +OK ──────│
  │                    └── "val1" ─────│

  Total time: 1 × network RTT + 3 × execution time
  If RTT = 0.2ms, execution = 0.005ms:
  Total ≈ 0.2ms + 0.015ms = 0.215ms  (3.5x faster)
```

### Why it works

1. **Fewer syscalls**: One `write()` with 3 commands vs three separate `write()` calls
2. **Fewer TCP packets**: Nagle's algorithm and TCP segmentation pack commands together
3. **Fewer `epoll_wait` wake-ups**: The event loop processes all buffered commands in one pass
4. **Server-side batching**: Responses are queued and flushed together

### Pipelining vs transactions

| Aspect              | Pipelining                        | MULTI/EXEC Transaction            |
|---------------------|-----------------------------------|-----------------------------------|
| Atomicity           | No — other clients can interleave | Yes — commands run atomically     |
| Network round-trips | 1 for the batch                   | 1 for the batch                   |
| Error handling      | Per-command errors in response     | All-or-nothing with WATCH         |
| Server buffering    | Commands execute as they arrive    | Commands queued, execute on EXEC  |

Pipelining is a **client-side network optimization**. Transactions are a **server-side
atomicity guarantee**. They solve different problems and can be combined.

### Client library support

Most Redis clients support pipelining natively:

```python
# Python (redis-py)
pipe = r.pipeline(transaction=False)  # pipelining without MULTI/EXEC
pipe.set("key1", "val1")
pipe.set("key2", "val2")
pipe.get("key1")
results = pipe.execute()  # [True, True, b"val1"]
```

```java
// Java (Jedis)
Pipeline p = jedis.pipelined();
p.set("key1", "val1");
p.set("key2", "val2");
p.get("key1");
List<Object> results = p.syncAndReturnAll();
```

---

## 6. Contrast with Memcached's Multi-Threaded Model

Memcached uses a traditional multi-threaded architecture with a main listener thread and
a pool of worker threads.

### Memcached architecture

```
  ┌──────────────────────────────────────────────────┐
  │                  Memcached                        │
  │                                                   │
  │  ┌──────────────────┐                             │
  │  │  Main Listener   │                             │
  │  │  Thread           │                             │
  │  │  (accepts conns)  │                             │
  │  └────────┬─────────┘                             │
  │           │ dispatch via pipe notification         │
  │     ┌─────┼──────┬──────┬──────┐                  │
  │     ▼     ▼      ▼      ▼      ▼                  │
  │  ┌─────┐┌─────┐┌─────┐┌─────┐                    │
  │  │ Wkr ││ Wkr ││ Wkr ││ Wkr │  (default -t 4)   │
  │  │  1  ││  2  ││  3  ││  4  │                    │
  │  └──┬──┘└──┬──┘└──┬──┘└──┬──┘                    │
  │     │      │      │      │                        │
  │     ▼      ▼      ▼      ▼                        │
  │  ┌─────────────────────────────┐                  │
  │  │   Shared Hash Table          │                  │
  │  │   (per-slab locking)         │                  │
  │  └─────────────────────────────┘                  │
  │                                                   │
  │  Event library: libevent (per worker thread)      │
  └──────────────────────────────────────────────────┘
```

### Side-by-side comparison

| Aspect                  | Redis                              | Memcached                          |
|-------------------------|------------------------------------|------------------------------------|
| Command execution       | Single-threaded                    | Multi-threaded (worker pool)       |
| I/O handling            | Single-threaded (6.0+ threaded I/O)| Per-worker event loop (libevent)   |
| Event library           | ae (custom, ~700 lines)            | libevent (external, ~30K lines)    |
| Locking                 | None (single thread)               | Per-slab class locks               |
| Data structures         | Rich (lists, sets, sorted sets...) | Strings only                       |
| CPU core utilization    | 1 core per instance                | N cores per instance (via -t N)    |
| Scaling model           | Multiple instances + Cluster       | Single instance, more threads      |
| Connection dispatch     | All on main thread (or I/O threads)| Round-robin to workers via pipe    |
| Atomic operations       | Implicit (single thread)           | Explicit locks required            |
| Debugging complexity    | Low (deterministic execution)      | High (race conditions possible)    |

### Locking evolution in Memcached

1. **Early versions**: Global cache lock — all worker threads contend on a single mutex
2. **Later versions**: Per-slab class locking — finer granularity, less contention
3. **Current**: Per-item CAS (compare-and-swap) for certain operations

Even with fine-grained locking, Memcached can experience lock contention under heavy write
workloads with hot keys that map to the same slab class.

### Where Memcached's model wins

- **CPU-bound workloads**: Multiple threads can execute commands in parallel
- **Large multi-core machines**: Better utilization of 16+ cores in a single process
- **Simple GET/SET at scale**: No data structure overhead, just hash lookups

### Where Redis's model wins

- **Complex operations**: ZADD, LPUSH, SINTERSTORE — no locking needed
- **Predictable latency**: No lock contention spikes
- **Simpler operations**: Most Redis operations are so fast that multi-threading adds overhead without benefit
- **Debugging**: No race conditions, deterministic behavior

---

## 7. Scaling Decision: Scale-Out vs Scale-Up

Redis made a fundamental architectural choice: **scale out horizontally** rather than
scale up vertically with more threads.

### Redis's approach: one instance per core

```
  ┌────────────────────────────────────────────────────────┐
  │              8-Core Server                              │
  │                                                         │
  │  ┌──────────┐ ┌──────────┐ ┌──────────┐ ┌──────────┐  │
  │  │ Redis    │ │ Redis    │ │ Redis    │ │ Redis    │  │
  │  │ :6379    │ │ :6380    │ │ :6381    │ │ :6382    │  │
  │  │ (core 0) │ │ (core 1) │ │ (core 2) │ │ (core 3) │  │
  │  └──────────┘ └──────────┘ └──────────┘ └──────────┘  │
  │  ┌──────────┐ ┌──────────┐ ┌──────────┐ ┌──────────┐  │
  │  │ Redis    │ │ Redis    │ │ Redis    │ │ Redis    │  │
  │  │ :6383    │ │ :6384    │ │ :6385    │ │ :6386    │  │
  │  │ (core 4) │ │ (core 5) │ │ (core 6) │ │ (core 7) │  │
  │  └──────────┘ └──────────┘ └──────────┘ └──────────┘  │
  │                                                         │
  │  Redis Cluster manages slot distribution across         │
  │  instances. Each instance is independent.               │
  └────────────────────────────────────────────────────────┘
```

### Trade-offs

| Aspect                | Scale Out (Redis)                      | Scale Up (Memcached)               |
|-----------------------|----------------------------------------|------------------------------------|
| Failure isolation     | One instance crash affects 1/N data    | Process crash loses everything     |
| Memory efficiency     | Per-process overhead × N instances     | Single process, shared memory      |
| Operational complexity| Cluster management, slot migration     | Single process to manage           |
| Debugging             | Simple per-instance (single-threaded)  | Complex (thread interactions)      |
| Data locality         | Partition data by key hash             | All data in one address space      |
| Max memory            | Each instance ~25GB recommended        | Single process can use all RAM     |
| Atomic cross-key ops  | Only within same hash slot             | Any keys (with locking)            |
| Upgrade/restart       | Rolling restart, one instance at a time| Full process restart               |

### Why Redis chose this path

1. **Process isolation**: If one instance has a bug or OOM, others keep running. In a
   multi-threaded model, one bad thread can corrupt shared state and crash the entire process.

2. **Independent failure domains**: Each instance can be on different machines. Replicas
   provide high availability per shard. This is impossible with threads in one process.

3. **Simpler debugging**: `redis-cli DEBUG SLEEP 5` on one instance does not affect others.
   A stuck thread in a multi-threaded server can cascade via lock contention.

4. **No shared-nothing compromise**: Each instance owns its keyspace exclusively. No
   cross-instance coordination needed for commands (except multi-key commands spanning slots).

5. **Natural fit for cloud**: Containers and VMs map cleanly to individual Redis instances.
   Kubernetes StatefulSets, AWS ElastiCache node groups — all designed around process-level
   isolation.

### Practical deployment

```
Production setup (typical):

  3 masters × 1 replica each = 6 Redis instances
  16,384 hash slots divided across 3 masters

  Master 1 (slots 0-5460)      ◄──► Replica 1a
  Master 2 (slots 5461-10922)  ◄──► Replica 2a
  Master 3 (slots 10923-16383) ◄──► Replica 3a

  Each instance: ~8-25 GB RAM, pinned to a CPU core
  Total cluster: ~24-75 GB usable memory
  Throughput: ~500K-1M ops/sec aggregate
```

### When to consider multi-threaded alternatives

- You have a single monolithic dataset that cannot be partitioned
- Cross-key atomic operations are frequent and span the entire keyspace
- You are on a single massive machine and cannot run a cluster
- Your workload is CPU-bound (complex Lua scripts, heavy SORT operations)

In these cases, consider:
- **KeyDB**: Redis fork with multi-threaded command execution
- **Dragonfly**: Modern in-memory store, multi-threaded, Redis-compatible protocol
- **Garnet**: Microsoft's C# Redis-compatible cache with multi-threaded execution

---

## Summary

```
┌─────────────────────────────────────────────────────────────────┐
│                    Redis Threading Timeline                      │
│                                                                  │
│  v1.0 ─────── v3.0 ─────── v4.0 ─────── v6.0 ─────── v7.0+    │
│    │            │             │             │             │       │
│    │            │             │             │             │       │
│  Single      Single        Lazy-free     I/O threads   I/O      │
│  thread      thread +      (UNLINK,      for read/     threads  │
│  only        BG saves      FLUSHDB       write         + active │
│              (fork for      ASYNC on      phases.       defrag   │
│              RDB/AOF)      bg thread)    Execution     in bg    │
│                                          still single            │
│                                          threaded.               │
└─────────────────────────────────────────────────────────────────┘

Key insight: Redis has progressively offloaded NON-command work to
background threads while keeping command execution single-threaded.
This preserves the simplicity guarantee while addressing real-world
I/O bottlenecks.
```
