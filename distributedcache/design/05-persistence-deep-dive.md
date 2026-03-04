# Persistence (RDB + AOF) -- Deep Dive

Redis provides two complementary persistence mechanisms: point-in-time RDB snapshots
and append-only AOF logs. Understanding when, why, and how to use each is critical
for operating Redis as anything more than a throwaway cache.

---

## 1. RDB Snapshots

### BGSAVE: fork() + Copy-on-Write

RDB persistence creates a binary snapshot of the entire dataset at a point in time,
written to a file called `dump.rdb`.

**The mechanism:**

```
         fork()
Parent ──────────> Child
  │                  │
  │  (shares page    │  Iterates all keys
  │   table via      │  Serializes to dump.rdb
  │   COW)           │  Exits when done
  │                  │
  ▼                  ▼

Parent continues     Child writes RDB
serving clients      to temp file, then
with zero blocking   renames atomically
```

**Step-by-step BGSAVE:**

1. Parent calls `fork()`. The OS creates a child process that shares the parent's
   entire memory via the same page table. No data is copied yet.
2. The child process iterates every key-value pair and serializes them into a
   temporary RDB file on disk.
3. The parent continues serving read and write commands without blocking.
4. When a page is modified by the parent (a client writes to a key), the OS
   transparently duplicates that specific page before the write lands. This is
   copy-on-write (COW).
5. When the child finishes writing, it atomically renames the temp file to
   `dump.rdb` and exits.

### Fork/COW Memory Diagram

```
BEFORE fork():
┌─────────────────────────────────────┐
│         Parent Process              │
│  Page Table:                        │
│    Page 0 ──> Physical Frame 0      │
│    Page 1 ──> Physical Frame 1      │
│    Page 2 ──> Physical Frame 2      │
│    ...                              │
└─────────────────────────────────────┘

AFTER fork() (no writes yet):
┌──────────────────┐     ┌──────────────────┐
│  Parent Process   │     │  Child Process    │
│  Page Table:      │     │  Page Table:      │
│   Page 0 ─┐      │     │   Page 0 ─┐      │
│   Page 1 ─┤      │     │   Page 1 ─┤      │
│   Page 2 ─┤      │     │   Page 2 ─┤      │
└───────────┤──────┘     └───────────┤──────┘
            │                        │
            ▼ (shared, read-only)    │
     ┌──────────────┐               │
     │ Phys Frame 0 │ <─────────────┘
     │ Phys Frame 1 │ <─────────────┘
     │ Phys Frame 2 │ <─────────────┘
     └──────────────┘

AFTER parent writes to Page 1:
┌──────────────────┐     ┌──────────────────┐
│  Parent Process   │     │  Child Process    │
│  Page Table:      │     │  Page Table:      │
│   Page 0 ──> F0   │     │   Page 0 ──> F0  │
│   Page 1 ──> F1'  │     │   Page 1 ──> F1  │  (original)
│   Page 2 ──> F2   │     │   Page 2 ──> F2  │
└──────────────────┘     └──────────────────┘
                  │
                  ▼
          F1' = duplicated page with parent's new data
          F1  = original page, still used by child for snapshot
```

**Memory overhead of COW:**

- **Best case**: Near-zero additional memory. If the workload is read-heavy during
  the snapshot, few pages are modified, so few pages are duplicated.
- **Worst case**: 2x memory. If every page is modified while the child is still
  writing, every page gets duplicated.
- **Typical case**: 10-30% additional memory for write-moderate workloads.

### Fork Cost

- The `fork()` syscall itself takes approximately **10-20 ms per GB** of used memory.
  This is the time to duplicate the page table, not the data.
- For a 20 GB dataset: fork takes ~200-400 ms. During this time, Redis is blocked
  and cannot serve any commands.
- On very large instances (50+ GB), fork latency can exceed 1 second -- a real
  concern for latency-sensitive workloads.

### Linux Transparent Huge Pages (THP)

- Standard page size: **4 KB**. With THP enabled: **2 MB** pages.
- COW operates at page granularity. Modifying 1 byte in a 2 MB huge page forces
  the OS to copy the entire 2 MB page (instead of just 4 KB).
- This amplifies COW memory overhead by up to **512x** per modified region.
- **Redis strongly recommends disabling THP:**
  ```bash
  echo never > /sys/kernel/mm/transparent_hugepage/enabled
  ```

### Default Save Triggers (Redis 7.0+)

```
save 3600 1      # After 3600 sec (1 hour) if at least 1 key changed
save 300 100     # After 300 sec (5 min) if at least 100 keys changed
save 60 10000    # After 60 sec (1 min) if at least 10000 keys changed
```

These are OR conditions -- whichever fires first triggers a BGSAVE.

To disable RDB entirely:
```
save ""
```

### SAVE vs BGSAVE

| Property         | SAVE                    | BGSAVE                     |
|------------------|-------------------------|----------------------------|
| Blocking?        | Yes -- blocks all clients | No -- fork + background   |
| Memory overhead  | None (no fork)          | COW overhead (10-100%)     |
| Use case         | Shutdown only           | Production snapshots       |
| Command          | `SAVE`                  | `BGSAVE`                   |

`SAVE` is only used during controlled shutdown (`redis-cli shutdown save`) to
guarantee a final snapshot without the overhead of forking.

### RDB File Format

- Binary, compact, versioned format. Current version: **RDB version 10** (Redis 7.0).
- Includes: magic number, version, database selector, key-value pairs with expiry
  timestamps, checksum (CRC64).
- Extremely compact: a 1 GB in-memory dataset may produce a 200-400 MB RDB file
  (depending on data types and compressibility).
- Validation tool: `redis-check-rdb dump.rdb` detects corruption and reports errors.

---

## 2. AOF (Append-Only File)

### How It Works

Every write command that modifies the dataset is appended to the AOF file in Redis
protocol format (RESP). On restart, Redis replays the AOF to reconstruct the dataset.

```
Client:  SET user:1 "alice"
         INCR page_views
         LPUSH queue "job42"

AOF file (appendonly.aof):
  *3\r\n$3\r\nSET\r\n$6\r\nuser:1\r\n$5\r\nalice\r\n
  *2\r\n$4\r\nINCR\r\n$10\r\npage_views\r\n
  *3\r\n$5\r\nLPUSH\r\n$5\r\nqueue\r\n$5\r\njob42\r\n
```

### appendfsync Policies

| Policy      | Behavior                        | Data Loss Risk   | Performance     |
|-------------|---------------------------------|------------------|-----------------|
| `always`    | fsync after every write command | Near zero        | ~10x slower     |
| `everysec`  | fsync once per second (default) | Up to 1 second   | Minimal impact  |
| `no`        | Let OS decide when to flush     | Up to 30 seconds | Fastest         |

**`everysec`** is the recommended default. It provides a strong balance: you lose at
most 1 second of writes on a crash, with negligible performance overhead compared to
no persistence.

**`always`** calls `fsync()` after every write command. This guarantees that every
acknowledged write is on disk, but throughput drops significantly because every
command must wait for the disk I/O to complete.

**`no`** delegates flushing to the OS (typically every 30 seconds on Linux). Fastest
but most data loss on crash.

### AOF Rewrite

Over time, the AOF file grows unboundedly: 1 million INCRs on a counter produce
1 million lines, but the current state is just one key-value pair. AOF rewrite
compacts this.

**Trigger conditions (both must be met):**
```
auto-aof-rewrite-percentage 100   # AOF is 100% larger than last rewrite size
auto-aof-rewrite-min-size 64mb    # AOF is at least 64 MB
```

### AOF Rewrite Lifecycle

```
┌──────────────────────────────────────────────────────────┐
│                    AOF REWRITE PROCESS                    │
├──────────────────────────────────────────────────────────┤
│                                                          │
│  1. Trigger: AOF size exceeds threshold                  │
│     │                                                    │
│     ▼                                                    │
│  2. Parent calls fork()                                  │
│     │                                                    │
│     ├──────────────────────┐                             │
│     ▼                      ▼                             │
│  PARENT                  CHILD                           │
│  ┌────────────────┐    ┌─────────────────────┐          │
│  │ Continues       │    │ Iterates dataset    │          │
│  │ serving clients │    │ Writes minimal      │          │
│  │                 │    │ commands to          │          │
│  │ New writes go   │    │ temp AOF file       │          │
│  │ to BOTH:        │    │                     │          │
│  │  - old AOF      │    │ (Creates smallest   │          │
│  │  - rewrite buf  │    │  possible AOF that  │          │
│  │                 │    │  reconstructs the   │          │
│  └────────┬───────┘    │  current state)     │          │
│           │             └─────────┬───────────┘          │
│           │                       │                      │
│           │              3. Child finishes writing        │
│           │                       │                      │
│           ▼                       ▼                      │
│  4. Parent flushes rewrite buffer to new AOF             │
│     (appends all writes that happened during rewrite)    │
│           │                                              │
│           ▼                                              │
│  5. Atomic rename: new AOF replaces old AOF              │
│     (single rename() syscall -- atomic on POSIX)         │
│           │                                              │
│           ▼                                              │
│  6. Done. New compact AOF is now active.                 │
│                                                          │
└──────────────────────────────────────────────────────────┘
```

**Key detail**: During the rewrite, every new write command is written to two places:
the old AOF (so recovery works if the rewrite fails) and an in-memory rewrite buffer.
When the child finishes, the parent appends the rewrite buffer to the new file and
performs an atomic swap. No data is lost during the rewrite.

### AOF Corruption Repair

```bash
redis-check-aof --fix appendonly.aof
```

This tool scans the AOF for protocol errors. If corruption is found at the tail
(common after a crash mid-write), it truncates the file to the last valid command.
Data after the corruption point is lost, but the file becomes loadable.

---

## 3. RDB-AOF Hybrid (Redis 4.0+)

### Configuration

```
aof-use-rdb-preamble yes   # Introduced Redis 4.0 (default no); default yes since Redis 7.0
```

### How It Works

When an AOF rewrite is triggered, instead of writing Redis commands, the child
process writes the dataset in **RDB binary format** as the first section of the new
AOF file. After the RDB section, any commands that arrived during the rewrite are
appended in standard AOF format.

```
┌─────────────────────────────────────────┐
│            Hybrid AOF File              │
├─────────────────────────────────────────┤
│                                         │
│  ┌───────────────────────────────────┐  │
│  │     RDB PREAMBLE (binary)         │  │
│  │     - Full dataset snapshot       │  │
│  │     - Fast to load (~10-20s/GB)   │  │
│  └───────────────────────────────────┘  │
│  ┌───────────────────────────────────┐  │
│  │     AOF TAIL (text commands)      │  │
│  │     - Commands during rewrite     │  │
│  │     - Typically very small        │  │
│  └───────────────────────────────────┘  │
│                                         │
└─────────────────────────────────────────┘
```

### Recovery Process

1. Redis detects the RDB preamble magic bytes.
2. Loads the RDB section as a binary dataset (fast).
3. Replays the AOF tail commands (minimal, only the delta).

**Result**: Fast recovery (like RDB) with minimal data loss (like AOF). Best of both
worlds.

---

## 4. Multi-Part AOF (Redis 7.0+)

### The Problem with Monolithic AOF

Before 7.0, the AOF was a single file. During rewrite, both old and new AOF files
coexisted, doubling disk usage. File management was fragile.

### Multi-Part Architecture

```
appendonlydir/
├── appendonly.aof.1.base.rdb       # Base file (RDB format snapshot)
├── appendonly.aof.1.incr.aof       # Incremental file 1 (AOF commands)
├── appendonly.aof.2.incr.aof       # Incremental file 2 (AOF commands)
└── appendonly.aof.manifest          # Manifest tracking all parts
```

**Components:**

- **Base file**: RDB-format snapshot of the dataset at a point in time. Created
  during AOF rewrite.
- **Incremental files**: Standard AOF command logs appended after the base snapshot.
  New incremental files are created after each rewrite.
- **Manifest**: Text file listing all active parts in order. Redis reads this on
  startup to know which files to load and in what order.

**Rewrite in multi-part AOF:**

1. Child writes a new base file (RDB format).
2. Parent starts a new incremental file for new writes.
3. When child finishes, manifest is updated atomically to reference the new base
   and new incremental file.
4. Old base and old incremental files are deleted.

This is cleaner, more atomic, and avoids the single-file management issues of the
old approach.

---

## 5. Recovery Procedures

### Precedence Rules

- If **both** RDB and AOF are enabled and both files exist, **AOF takes precedence**.
  Rationale: AOF is more up-to-date (it captures every write, not just periodic
  snapshots).
- If only RDB exists, Redis loads from `dump.rdb`.
- If only AOF exists, Redis replays the AOF.
- If neither exists, Redis starts with an empty dataset.

### Recovery Time Benchmarks

| Format     | 1 GB Dataset | 10 GB Dataset | Mechanism              |
|------------|-------------|---------------|------------------------|
| RDB        | 10-20 sec   | 100-200 sec   | Binary deserialization |
| AOF (pure) | 60-120 sec  | 600-1200 sec  | Command replay         |
| Hybrid     | 10-20 sec + small replay | 100-200 sec + small replay | RDB load + delta replay |

RDB recovery is 5-10x faster than pure AOF recovery because binary deserialization
is far more efficient than parsing and executing individual commands.

### Recovery Verification

```bash
# Check RDB integrity before loading
redis-check-rdb dump.rdb

# Check AOF integrity before loading
redis-check-aof appendonly.aof

# Fix corrupted AOF (truncates at corruption point)
redis-check-aof --fix appendonly.aof
```

---

## 6. When to Use What -- Decision Framework

```
                    Do you need data to survive restart?
                                │
                   ┌────────────┴────────────┐
                   │ NO                       │ YES
                   ▼                          ▼
          Disable both RDB             How much data loss
          and AOF.                     is acceptable?
          ┌─────────────┐                    │
          │ save ""      │       ┌───────────┼───────────┐
          │ appendonly no │       │           │           │
          └─────────────┘    Minutes       ~1 sec      Zero
                              │           │           │
                              ▼           ▼           ▼
                          RDB only    AOF everysec  AOF always
                          (warm       + RDB hybrid  (slowest,
                          restart)    (recommended)  safest)
```

| Scenario                       | Config                                  | Trade-off                        |
|--------------------------------|-----------------------------------------|----------------------------------|
| Pure volatile cache            | `save ""`, `appendonly no`              | No disk I/O, data lost on crash  |
| Cache with warm restart        | `save 3600 1 300 100 60 10000`          | Minutes of data loss, fast load  |
| Durable data store (balanced)  | `appendonly yes`, `appendfsync everysec` | Up to 1 sec loss, good perf     |
| Maximum durability             | `appendonly yes`, `appendfsync always`   | Near-zero loss, lower throughput |
| Best overall                   | AOF + RDB hybrid (default in 7.0+)      | Fast recovery + minimal loss    |

---

## 7. Contrast with Memcached

| Feature                  | Redis                                     | Memcached                        |
|--------------------------|-------------------------------------------|----------------------------------|
| RDB snapshots            | Yes (BGSAVE, fork + COW)                  | **No**                           |
| Append-only log          | Yes (AOF with 3 fsync policies)           | **No**                           |
| Hybrid persistence       | Yes (RDB preamble + AOF tail)             | **No**                           |
| Restart behavior         | Warm restart from RDB/AOF                 | **Cold start, empty cache**      |
| Data loss on crash       | 0 sec to ~1 sec (configurable)            | **100% data loss**               |
| Persistence philosophy   | "Database that happens to be in memory"   | "Pure volatile cache, nothing more" |

Memcached has **zero persistence** capabilities. When a Memcached node restarts, it
starts empty. When Redis restarts, it reloads its dataset from disk and continues
where it left off. This is the single biggest architectural difference between the two
systems and drives the choice between them for use cases that need any form of
durability.

---

## 8. Operational Concerns

### Backup Strategy

- **Do not run BGSAVE on the leader in production** under high write load. The fork()
  latency spike and COW memory overhead can impact client latency.
- Instead, use a **replica** (follower) for backups. The replica can run BGSAVE without
  affecting the leader's performance.
- Schedule BGSAVE during low-traffic periods (e.g., 3 AM) if running on the leader
  is unavoidable.

### Monitoring Keys

```
INFO persistence
```

| Metric                        | What It Tells You                                   |
|-------------------------------|-----------------------------------------------------|
| `rdb_last_bgsave_status`      | `ok` or `err`. Alert on `err`.                      |
| `rdb_last_bgsave_time_sec`    | Duration of last BGSAVE. Grows with dataset size.   |
| `rdb_last_save_time`          | Unix timestamp of last successful save.             |
| `aof_enabled`                 | Whether AOF is active.                              |
| `aof_last_rewrite_time_sec`   | Duration of last AOF rewrite.                       |
| `aof_current_size`            | Current AOF file size in bytes.                     |
| `aof_base_size`               | AOF size after last rewrite (growth = current-base).|

### Disk Space Management

- **RDB**: Compact. A 10 GB in-memory dataset might produce a 2-4 GB RDB file. Only
  one file at a time (atomic rename).
- **AOF**: Can grow very large if rewrite thresholds are set too high. A counter
  incremented 10 million times produces 10 million lines in the AOF.
- **AOF rewrite** reclaims space by writing only the current state. Ensure you have
  enough disk for the old AOF + new AOF during the rewrite.
- Rule of thumb: provision disk space at **3x your expected dataset size** to handle
  RDB + AOF + rewrite overhead comfortably.

### Configuration Checklist for Production

```
# Disable THP (run on the host, not in redis.conf)
echo never > /sys/kernel/mm/transparent_hugepage/enabled

# redis.conf
appendonly yes
appendfsync everysec
aof-use-rdb-preamble yes
auto-aof-rewrite-percentage 100
auto-aof-rewrite-min-size 64mb

# RDB as secondary safety net
save 3600 1 300 100 60 10000

# Prevent OOM during fork/COW
# Set maxmemory to ~60-70% of available RAM
maxmemory 12gb
maxmemory-policy allkeys-lru
```

---

## Summary

RDB and AOF are not competing mechanisms -- they are complementary layers. RDB gives
you fast, compact point-in-time snapshots. AOF gives you a write-ahead log with
configurable durability. The hybrid approach combines both into a single recovery
path that is both fast and safe. Multi-part AOF (Redis 7.0+) modernizes the file
management. Together, they allow Redis to serve as a durable data store, not just a
volatile cache -- a capability Memcached fundamentally lacks.
