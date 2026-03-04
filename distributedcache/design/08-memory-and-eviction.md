# Memory Management & Eviction — Deep Dive

How Redis manages finite memory, decides what to evict, and how its allocator compares to Memcached's slab model.

---

## 1. maxmemory Configuration

Redis does not limit its own memory usage by default. Without `maxmemory`, it grows unbounded until the operating system's OOM killer terminates the process — a silent, catastrophic failure in production.

```
# redis.conf
maxmemory 4gb

# Or at runtime
CONFIG SET maxmemory 4294967296
```

When `maxmemory` is reached, Redis applies the configured **eviction policy** before accepting new writes. If the policy is `noeviction`, write commands return errors (`OOM command not allowed`).

**Sizing guideline:** set `maxmemory` to roughly **75% of available RAM**. The remaining 25% is needed for:

- **fork() and COW pages** — `BGSAVE` and `BGREWRITEAOF` fork a child process. During the fork, the OS uses copy-on-write. If the parent mutates pages while the child is serializing, those pages are duplicated in physical memory. Under heavy write load, memory can temporarily double.
- **OS page cache and buffers** — the kernel needs memory for TCP buffers, file system cache, and general bookkeeping.
- **Output buffers** — replica output buffers, client output buffers, and the AOF rewrite buffer all live outside the data keyspace.

If you set `maxmemory` to 100% of RAM and then trigger a `BGSAVE`, the fork's COW overhead can push the process into swap or trigger the OOM killer.

---

## 2. The 8 Eviction Policies

When `maxmemory` is reached and a write command arrives, Redis must free memory. The `maxmemory-policy` setting determines *how* it chooses victims.

| Policy | Pool | Algorithm | When to Use |
|---|---|---|---|
| `noeviction` | — | Return `OOM` error on writes | Data loss is unacceptable. You accept write failures over silent data loss. Default when `maxmemory` is set. |
| `allkeys-lru` | All keys | Approximated LRU | **Best general-purpose cache policy.** Good default for most caching workloads. |
| `allkeys-lfu` | All keys | Approximated LFU | Scan-resistant workloads. Background jobs that touch every key won't pollute the cache (Redis 4.0+). |
| `allkeys-random` | All keys | Random | Uniform access pattern where every key is equally likely to be accessed. Simple and fast. |
| `volatile-lru` | Keys with TTL | Approximated LRU | Mixed workload: some keys are cache (have TTL), others are persistent (no TTL). Only cache keys are eviction candidates. |
| `volatile-lfu` | Keys with TTL | Approximated LFU | Same as `volatile-lru` but with frequency-based eviction (Redis 4.0+). |
| `volatile-random` | Keys with TTL | Random | Random eviction restricted to keys with TTL. |
| `volatile-ttl` | Keys with TTL | Nearest expiry | Evict keys closest to expiration. Useful when you want "almost expired" keys cleaned up first to free memory. |

**Key distinction — allkeys vs volatile:**

- `allkeys-*` policies consider every key in the database. This is the right choice when Redis is used purely as a cache.
- `volatile-*` policies only consider keys that have a TTL set. If no keys have a TTL, these policies behave like `noeviction` — writes fail. This is the right choice when Redis holds a mix of ephemeral cache data (with TTL) and persistent data (without TTL) that must never be evicted.

---

## 3. Approximated LRU — Implementation Detail

A textbook LRU cache uses a **doubly-linked list** threaded through a hash map. Every access moves the node to the head. Eviction pops from the tail. This is O(1) per operation but costs **16 bytes per key** (two pointers: `prev` and `next`). With 100 million keys, that is 1.6 GB of overhead just for the LRU bookkeeping.

Redis takes a different approach: **approximated LRU via sampling**.

### How it works

1. **Every `redisObject` has a 24-bit `lru` field** in its header. This field stores the Unix timestamp (in seconds, modulo 2^24) of the last time the key was accessed. Since the field is 24 bits, it wraps around every ~194 days. Redis handles the wraparound correctly when comparing timestamps.

2. **On every key access** (`lookupKey`), Redis updates this 24-bit timestamp to the current time.

3. **When eviction is needed**, Redis does not scan all keys. Instead:
   - It randomly samples `maxmemory-samples` keys (default: **5**).
   - Among those samples, it evicts the one with the **oldest** `lru` timestamp.

4. **Eviction pool (Redis 3.0+):** Rather than discarding the non-evicted samples, Redis maintains a **pool of 16 eviction candidates** across successive sampling rounds. Each new round's samples are merged into the pool, keeping only the best (oldest-access) candidates. This significantly improves approximation quality because the pool accumulates good candidates over time.

### Approximation quality vs sample size

| `maxmemory-samples` | Approximation Quality | CPU Cost |
|---|---|---|
| 5 (default) | Reasonable — noticeably worse than true LRU on adversarial patterns, good enough for most workloads | Low |
| 10 | Very close to true LRU | Moderate |
| 20 | Nearly indistinguishable from true LRU | Higher |

### Memory overhead

**Zero additional bytes.** The 24-bit timestamp is stored in the existing `redisObject` header alongside the type, encoding, and reference count fields. There are no linked-list pointers, no separate data structure. This is why Redis chose approximation — it trades a small amount of eviction accuracy for a large memory savings.

---

## 4. LFU (Least Frequently Used) — Redis 4.0+

### The problem with LRU

LRU has a well-known weakness: **a single full scan pollutes the entire cache**. If a background job runs `KEYS *` or iterates over every key (via `SCAN`), every key's `lru` timestamp is refreshed. From LRU's perspective, all keys are now "recently used." The next eviction round will evict keys essentially at random, potentially kicking out genuinely hot keys.

LFU solves this by tracking **access frequency** rather than recency. A key that has been accessed 10,000 times won't be evicted just because a scan touched a cold key once.

### Implementation

Redis repurposes the same 24-bit `lru` field in `redisObject`, splitting it into two parts:

```
|<-------- 16 bits -------->|<-- 8 bits -->|
   last decrement timestamp     log counter
```

- **8-bit logarithmic counter (0-255):** Tracks access frequency. Saturates at 255.
- **16-bit timestamp:** Records the last time the counter was decremented (minutes granularity, wraps every ~45 days).

### Probabilistic increment

The counter does not increment by 1 on every access. Instead, it increments **probabilistically**:

```
P(increment) = 1 / (counter * lfu_log_factor + 1)
```

With the default `lfu-log-factor=10`:

| Accesses | Approximate Counter Value |
|---|---|
| 1 | 1 |
| 10 | ~8 |
| 100 | ~18 |
| 1,000 | ~38 |
| 10,000 | ~58 |
| 100,000 | ~78 |
| 1,000,000 | ~100 |
| 10,000,000 | ~255 (saturated) |

This logarithmic scaling means 8 bits can represent a wide range of access frequencies.

### Decay

Without decay, a key that was hot yesterday but cold today would never be evicted. Redis applies a **time-based decay**:

- Every `lfu-decay-time` minutes (default: **1 minute**), the counter is decremented by 1.
- This happens lazily: when the key is next accessed, Redis checks how many decay intervals have passed since the 16-bit timestamp and decrements accordingly.
- A key that was hot 4 hours ago (240 minutes) would have its counter reduced by 240. If it was at counter=100, it would drop to 0 — making it an excellent eviction candidate.

### New key initialization

New keys start with `counter = LFU_INIT_VAL = 5` (not 0). If new keys started at 0, they would be the immediate next eviction target, even before cold keys that have been sitting unused. Starting at 5 gives new keys a brief grace period to accumulate real access data.

---

## 5. jemalloc — Redis's Memory Allocator

### Why jemalloc?

Redis has used **jemalloc** as its default allocator since version 2.4. Before that, it used glibc's `malloc`, which suffered from severe fragmentation under Redis's allocation patterns (many small, variable-size allocations with frequent create/delete cycles).

jemalloc was designed for multi-threaded applications (it originated in FreeBSD) and provides:

- **Thread-local caches (tcache):** Reduce lock contention for small allocations.
- **Size classes:** Allocations are rounded up to the nearest size class, reducing external fragmentation.
- **Arena-based allocation:** Memory is divided into arenas, reducing contention and improving locality.

### Monitoring memory with INFO

```
> INFO memory
used_memory:1073741824          # Bytes allocated by Redis (data + overhead)
used_memory_human:1.00G
used_memory_rss:1288490188      # Resident set size from OS (actual physical memory)
used_memory_rss_human:1.20G
used_memory_peak:2147483648     # Historical peak of used_memory
used_memory_peak_human:2.00G
mem_fragmentation_ratio:1.20    # RSS / used_memory
mem_allocator:jemalloc-5.2.1
```

### mem_fragmentation_ratio — the key diagnostic metric

```
mem_fragmentation_ratio = used_memory_rss / used_memory
```

| Value | Meaning |
|---|---|
| 1.0 - 1.2 | Healthy. Minimal fragmentation. |
| 1.2 - 1.5 | Moderate fragmentation. Worth monitoring. |
| > 1.5 | Significant fragmentation. Redis is using much more physical memory than its data requires. Investigate. |
| < 1.0 | **Redis is using swap.** This is a critical performance problem. Swap latency is 1000x-10000x worse than RAM. Fix immediately. |

### Per-key memory inspection

```
> MEMORY USAGE mykey
(integer) 72                    # Total bytes for this key (key + value + overhead)

> MEMORY DOCTOR
Sam, I have a few things to report...
```

`MEMORY DOCTOR` runs automated heuristics and reports issues like high fragmentation, high peak-to-current ratio, or excessive allocator overhead.

---

## 6. Active Defragmentation (Redis 4.0+)

Even with jemalloc, fragmentation accumulates over time — especially with workloads that create and delete many variable-size keys. Redis 4.0 introduced **active defragmentation**: a background process that moves allocations to compact memory and free up contiguous pages.

### How it works

1. Redis walks through all keys in the keyspace.
2. For each allocation, it checks whether jemalloc can provide a more compact location.
3. If yes, it allocates new memory, copies the data, updates the pointer, and frees the old allocation.
4. This runs incrementally during idle time, bounded by CPU limits.

### Configuration

```
# Enable active defrag
activedefrag yes

# Start defragmenting when fragmentation exceeds this %
active-defrag-threshold-lower 10

# Use maximum CPU effort when fragmentation exceeds this %
active-defrag-threshold-upper 100

# Minimum % of CPU time spent on defrag
active-defrag-cycle-min 1

# Maximum % of CPU time spent on defrag
active-defrag-cycle-max 25

# Minimum size of allocation to consider for defrag
active-defrag-max-scan-fields 1000
```

The CPU usage scales linearly between `cycle-min` and `cycle-max` as fragmentation moves between `threshold-lower` and `threshold-upper`.

**Requirement:** Active defragmentation only works with **jemalloc**. It relies on jemalloc's `je_malloc_usable_size()` and allocation introspection APIs.

---

## 7. Contrast with Memcached's Slab Allocator

Memcached takes a fundamentally different approach to memory management: the **slab allocator**.

### Slab allocator mechanics

1. Memory is divided into **pages** of 1 MB each.
2. Each page belongs to a **slab class**. Slab classes have fixed chunk sizes that grow by a `growth_factor` (default 1.25x):
   - Class 1: 96 bytes
   - Class 2: 120 bytes
   - Class 3: 150 bytes
   - ...
   - Class ~42: 1 MB (one item per page)
3. When an item is stored, Memcached finds the smallest slab class that fits the item and stores it in a chunk of that class.
4. A free chunk is taken from the page. If no free chunks, a new page is allocated to that slab class.
5. Once a page is assigned to a slab class, it traditionally stays in that class permanently.

### Slab calcification

Over time, the distribution of item sizes can shift. If the workload initially stored many 100-byte items, many pages would be assigned to the slab class for ~120-byte chunks. If the workload later shifts to 500-byte items, the 120-byte slab class has excess pages while the 500-byte class is starving.

Memcached mitigates this with **slab reassignment** (`automove`):
- `slab_reassign`: enables page-level rebalancing between slab classes.
- `slab_automove`: automatically moves pages from slab classes with free chunks to classes that are evicting.

### Comparison table

| Dimension | Redis (jemalloc) | Memcached (Slab Allocator) |
|---|---|---|
| **Allocation strategy** | General-purpose allocator with size classes and arenas | Fixed-size chunks within slab classes |
| **External fragmentation** | Possible; mitigated by jemalloc's size classes and active defrag | None; all chunks within a class are identical |
| **Internal fragmentation** | Minimal; jemalloc rounds up to nearest size class (fine-grained) | Can be significant; a 100-byte item in a 128-byte chunk wastes 28 bytes (22%) |
| **Memory overhead per item** | `redisObject` header (~16 bytes) + SDS string overhead | 48-byte item header per chunk |
| **Fragmentation over time** | Can degrade; active defrag (4.0+) helps | Stable within classes; slab calcification between classes |
| **Variable-size values** | Handles naturally; allocations sized to actual data | Rounded up to chunk boundary; internal waste |
| **Diagnostics** | `INFO memory`, `MEMORY USAGE`, `MEMORY DOCTOR` | `stats slabs`, `stats items` |
| **Defrag/rebalance** | Active defragmentation (background, CPU-limited) | Slab reassignment / automove (page-level, between classes) |
| **Multi-threaded behavior** | jemalloc uses per-thread arenas/tcache; Redis is single-threaded for data ops so this matters less | Slab allocator is thread-safe; Memcached is multi-threaded so this matters more |
| **Best suited for** | Variable-size values, complex data structures | Uniform small objects (simple key-value cache) |

---

## 8. Memory Optimization Tips

### Use compact encodings

Redis automatically uses memory-efficient encodings for small collections:

| Data Type | Compact Encoding | Condition | Normal Encoding |
|---|---|---|---|
| Hash | listpack | Entries <= `hash-max-listpack-entries` (128) AND all values <= `hash-max-listpack-value` (64 bytes) | hashtable |
| Set | listpack | Entries <= `set-max-listpack-entries` (128) AND all values are strings <= `set-max-listpack-value` (64 bytes) | hashtable |
| Set (integers only) | intset | Entries <= `set-max-intset-entries` (512) AND all members are integers | hashtable |
| Sorted Set | listpack | Entries <= `zset-max-listpack-entries` (128) AND all values <= `zset-max-listpack-value` (64 bytes) | skiplist + hashtable |
| List | listpack | Entries <= `list-max-listpack-size` (configurable) | quicklist (linked list of listpacks) |

A hash with 5 small fields stored as a listpack uses **~5-10x less memory** than the same data stored as a full hashtable.

### Inspect encodings

```
> OBJECT ENCODING mykey
"listpack"                    # Good — compact

> OBJECT ENCODING mykey
"hashtable"                   # Full encoding — uses more memory
```

If a key unexpectedly uses a full encoding, check whether it exceeded the listpack thresholds. Consider splitting large hashes into multiple smaller ones.

### Short key names

In memory-constrained environments, key names matter. Redis stores key names as SDS strings with a header. The key `user:1234:session:token` (25 bytes) uses more memory than `u:1234:s:t` (10 bytes). With millions of keys, this adds up.

Trade-off: readability vs memory. In most systems, readable keys are worth the cost. In extreme cases (billions of keys), abbreviate.

### Client-side compression

Redis stores values as opaque byte strings. You can compress values before writing:

```python
import zlib
compressed = zlib.compress(json.dumps(data).encode())
redis.set("key", compressed)

# On read
data = json.loads(zlib.decompress(redis.get("key")))
```

Compression ratios of 3x-10x are common for JSON/text. Trade-off: CPU time for compression/decompression on the client.

### Monitoring checklist

| Metric | Source | Alert Threshold |
|---|---|---|
| `used_memory` | `INFO memory` | Approaching `maxmemory` |
| `used_memory_peak` | `INFO memory` | Growing trend without corresponding data growth |
| `mem_fragmentation_ratio` | `INFO memory` | > 1.5 or < 1.0 |
| `evicted_keys` | `INFO stats` | Any evictions (if unexpected) |
| `maxmemory_policy` | `CONFIG GET` | Verify it matches intended policy |
| `used_memory_dataset` | `INFO memory` | Ratio to `used_memory` — if low, overhead dominates |

---

## Summary

- Set `maxmemory` to ~75% of RAM. Never run without it in production.
- `allkeys-lru` is the best default for pure caching. Switch to `allkeys-lfu` for scan-resistant workloads.
- Redis's approximated LRU uses zero extra memory per key (24-bit field in existing object header) and 5-sample default gives good-enough accuracy.
- LFU uses an 8-bit logarithmic counter with probabilistic increment and time-based decay. It solves LRU's cache pollution problem.
- Monitor `mem_fragmentation_ratio`. Enable active defragmentation if fragmentation exceeds 1.5.
- Memcached's slab allocator eliminates external fragmentation but wastes memory on internal fragmentation. Redis's jemalloc is more flexible for variable-size data.
- Use compact encodings (listpack, intset) for small collections — they use 5-10x less memory.
