# Expiry & TTL Mechanism — Deep Dive

How Redis tracks key lifetimes, deletes expired keys, and balances memory reclamation against CPU overhead.

---

## 1. Setting TTL

Redis provides multiple commands for setting key expiration:

### Relative TTL (duration from now)

```
EXPIRE key 300              # Expire in 300 seconds
PEXPIRE key 300000          # Expire in 300,000 milliseconds
```

### Absolute TTL (specific point in time)

```
EXPIREAT key 1735689600     # Expire at Unix timestamp 1735689600
PEXPIREAT key 1735689600000 # Expire at Unix timestamp in milliseconds
```

### Inline with SET

```
SET key value EX 300        # Set + expire in 300 seconds
SET key value PX 300000     # Set + expire in 300,000 milliseconds
SET key value EXAT 1735689600    # Set + expire at Unix timestamp (Redis 6.2+)
SET key value PXAT 1735689600000 # Set + expire at Unix ms timestamp (Redis 6.2+)
```

### Querying TTL

```
TTL key                     # Remaining seconds (-1 = no TTL, -2 = key doesn't exist)
PTTL key                    # Remaining milliseconds
EXPIRETIME key              # Absolute Unix timestamp of expiry (Redis 7.0+)
PEXPIRETIME key             # Absolute Unix ms timestamp of expiry (Redis 7.0+)
```

### Removing TTL

```
PERSIST key                 # Remove TTL, make key permanent
```

### Internal representation

Regardless of which command is used, Redis stores the expiry as an **absolute Unix timestamp in milliseconds** in a separate hash table called the **expires dict**. The main dict maps keys to values; the expires dict maps keys to their expiry timestamps. Only keys with a TTL have an entry in the expires dict.

```
main dict:      key -> redisObject (the value)
expires dict:   key -> int64 (absolute expiry in ms since epoch)
```

When you run `EXPIRE key 300`, Redis computes `now_ms + 300000` and stores that absolute timestamp.

---

## 2. Passive Expiry (Lazy Deletion)

The simplest expiry mechanism: check if a key is expired **at the moment it is accessed**.

### How it works

Every command that touches a key calls `expireIfNeeded(key)` before doing anything else:

```c
int expireIfNeeded(redisDb *db, robj *key) {
    // 1. Look up key in expires dict
    long long when = getExpire(db, key);
    if (when < 0) return 0;         // No TTL set

    // 2. Compare against current time
    if (mstime() < when) return 0;  // Not expired yet

    // 3. Key is expired — delete it
    deleteExpiredKeyAndPropagate(db, key);
    return 1;
}
```

If the key is expired, it is deleted immediately, and the command behaves as if the key never existed (returns `nil`, `KEY_NOT_FOUND`, etc.).

### Properties

- **Zero CPU overhead for unaccessed keys.** If nobody asks for a key, no work is done.
- **Immediate correctness.** A client never sees a stale expired value.
- **Fatal flaw:** If millions of keys expire but are never accessed again, they remain in memory indefinitely. The expires dict still holds their entries, and the main dict still holds their values. Memory is never reclaimed.

This is why passive expiry alone is insufficient.

---

## 3. Active Expiry (Probabilistic Sweep)

Redis runs a background sweep to proactively find and delete expired keys, even if no client ever asks for them.

### How it works

The active expiry runs inside `serverCron`, which fires `hz` times per second (default `hz = 10`, so every 100ms). The function `activeExpireCycle` executes the following loop:

```
activeExpireCycle():
    for each database:
        loop:
            1. Sample 20 random keys from the expires dict
               (ACTIVE_EXPIRE_CYCLE_LOOKUPS_PER_LOOP = 20)
            2. Delete any sampled keys that are expired
            3. Count: what % of sampled keys were expired?
            4. If > 25% were expired → repeat the loop (more work to do)
               If <= 25% were expired → move to next database (under control)

        Time limit: stop if this cycle has consumed more than
        ~25% of the 1000/hz millisecond budget (25ms at hz=10)
```

### Properties

- **Adaptive.** When many keys are expiring (e.g., after a mass insertion with the same TTL), the loop repeats aggressively, spending more CPU to clean up. When few keys are expiring, it exits quickly after one sample.
- **Bounded CPU.** The time limit prevents the sweep from starving client requests. Even during a mass expiry event, active expiry never consumes more than ~25% of each `serverCron` tick.
- **Probabilistic, not exhaustive.** It samples random keys, so it does not guarantee that every expired key is found in every cycle. But statistically, expired keys are found and deleted within a few cycles.

### dynamic-hz (Redis 7.0+)

With `dynamic-hz yes` (default in Redis 7.0+), the `hz` value auto-adjusts between 1 and 500 based on the number of connected clients and overall activity. Under heavy load, `hz` increases, and active expiry runs more frequently.

---

## 4. Why the Hybrid Approach?

```
+------------------------------------------------------------------+
|                     CLIENT REQUEST                                |
|                    GET mykey                                      |
+------------------------------------------------------------------+
         |
         v
+------------------+     expired?     +------------------+
|  PASSIVE EXPIRY  |----------------->|  Delete key      |
|                  |       yes        |  Return nil      |
|  expireIfNeeded()|                  +------------------+
|  (on every       |
|   key access)    |       no
|                  |----------------->  Return value
+------------------+

         +              +
         |              |
    Catches keys        But keys never accessed
    immediately         remain in memory...
         |              |
         v              v

+------------------------------------------------------------------+
|                    ACTIVE EXPIRY                                  |
|                    (background sweep)                             |
|                                                                  |
|  Runs hz times/sec (default: every 100ms)                        |
|                                                                  |
|  +------------------------------------------------------------+  |
|  |  1. Sample 20 random keys from expires dict                |  |
|  |  2. Delete expired ones                                    |  |
|  |  3. If >25% were expired, repeat (more cleanup needed)     |  |
|  |  4. If <=25% were expired, stop (under control)            |  |
|  |  5. Time-limited: max ~25% of each hz cycle                |  |
|  +------------------------------------------------------------+  |
|                                                                  |
|  Catches keys that nobody accesses.                              |
|  Bounded CPU. Eventually finds all expired keys.                 |
+------------------------------------------------------------------+

TOGETHER:
  - Passive: instant correctness on access (zero overhead for unaccessed keys)
  - Active: eventual cleanup of unaccessed expired keys (bounded CPU)
  - Result: no stale reads + no unbounded memory leak
```

### Why not just passive?

**Memory leak.** Consider a system that writes 10 million keys with a 1-hour TTL. After 1 hour, all 10 million keys are expired. But if the application only reads 1 million of them, the other 9 million sit in memory forever (or until `maxmemory` eviction kicks them out indirectly). Passive expiry alone turns Redis into a memory leak for write-heavy, read-sparse workloads.

### Why not just active?

**Expensive.** To guarantee timely cleanup with active expiry alone, you would need to scan all keys with TTL frequently. With 100 million keys, even sampling becomes expensive if you need strong guarantees. And you still cannot guarantee that a client won't read a stale key between sweep cycles.

### The hybrid elegance

- **Passive** guarantees correctness: no client ever sees an expired key.
- **Active** guarantees eventual memory reclamation: expired keys that are never accessed are cleaned up within seconds to minutes, using bounded CPU.
- Together, they cover each other's weaknesses perfectly.

---

## 5. Replication of Expiry

### The rule: replicas do NOT independently expire keys

In a Redis replication setup (leader + followers), only the **leader** runs passive and active expiry. When the leader detects that a key is expired (through either mechanism), it:

1. Deletes the key locally.
2. Synthesizes a `DEL key` command.
3. Sends the `DEL` to all replicas via the replication stream.

Replicas apply the `DEL` just like any other replicated write.

### Why this design?

**Consistency.** If replicas independently expired keys based on their own clocks, clock skew between leader and replicas would cause keys to expire at different times on different nodes. A client reading from a replica might get `nil` for a key that the leader still considers valid, or vice versa.

By funneling all expiry decisions through the leader and replicating them as explicit `DEL` commands, all nodes converge to the same state.

### The stale-read window

There is a brief window where a replica may serve a stale read:

1. A key expires on the leader.
2. The leader deletes it and sends `DEL` to replicas.
3. Between the key expiring and the `DEL` arriving, a client reading from the replica sees the expired key.

In practice, replication lag is typically **< 1 millisecond** on a healthy cluster, so this window is negligible. However, under replication lag (network issues, replica overloaded), the window can widen.

Redis 3.2+ added a mitigation: replicas check the key's expiry timestamp on read and return `nil` if the key is logically expired, even if the `DEL` hasn't arrived yet. This eliminates stale reads for passive-expiry scenarios (client access), though the key still occupies memory on the replica until the `DEL` arrives.

---

## 6. Mass Expiry Events

### The problem

If you insert a large batch of keys at the same time with the same TTL:

```
for i in range(1_000_000):
    redis.set(f"key:{i}", "value", ex=3600)
```

All 1 million keys expire at roughly the same second. When the active expiry cycle samples 20 keys from the expires dict, it finds that nearly 100% are expired (far above the 25% threshold). The loop repeats aggressively, consuming its full CPU budget every `serverCron` tick.

### Impact

- **Latency spike.** While the active expiry loop is running, client commands are delayed (Redis is single-threaded). The time limit prevents total starvation, but clients will observe elevated p99 latency.
- **CPU spike.** The `expired_keys` counter in `INFO stats` jumps, and Redis CPU usage climbs.

### Mitigation: add jitter to TTLs

```python
import random

base_ttl = 3600  # 1 hour
spread = 300     # 5 minutes of jitter

for i in range(1_000_000):
    ttl = base_ttl + random.randint(0, spread)
    redis.set(f"key:{i}", "value", ex=ttl)
```

Instead of all keys expiring at T+3600, they expire between T+3600 and T+3900. The active expiry cycle processes them gradually over 5 minutes instead of all at once.

### Monitoring

```
> INFO stats
...
expired_keys:15234567         # Total keys expired (cumulative)
expired_stale_perc:0.05       # % of keys in expires dict that are stale
expired_time_cap_reached_count:42  # Times active expiry hit its time limit
...
```

Track `expired_keys` rate (keys expired per second). A sudden spike indicates a mass expiry event.

---

## 7. Contrast with Memcached

| Aspect | Redis | Memcached |
|---|---|---|
| **Passive expiry** | Yes — `expireIfNeeded()` on every key access | Yes — check on access |
| **Active expiry** | Yes — probabilistic sweep `hz` times/sec | **No** — no background sweep |
| **Memory reclamation** | Active expiry proactively frees memory | Relies on LRU eviction when memory is full |
| **Expired but unaccessed keys** | Cleaned up within seconds by active expiry | Remain in memory until accessed or evicted by LRU pressure |
| **TTL precision** | Millisecond (stored as absolute ms timestamp) | Second (stored as absolute Unix timestamp) |
| **Per-field TTL** | Yes (Redis 7.4+ — hash fields) | No |

### Memcached's approach in detail

Memcached uses **lazy expiry only**. When a client requests a key, Memcached checks if it is expired. If yes, it deletes the item and returns a cache miss. There is no background thread that sweeps for expired items.

This means: if memory is not full, expired items that are never accessed sit in memory indefinitely. They are only cleaned up when:

1. A client accesses the expired key (lazy deletion), or
2. Memcached needs memory for a new item and the LRU eviction selects the expired item's slab.

In practice, this works fine for most Memcached use cases because:
- Memcached is typically run near its memory limit, so LRU eviction is constantly cycling out old items.
- Expired items are effectively "free" eviction candidates — the LRU will prefer them.

But in scenarios where memory is not under pressure, expired keys can linger in Memcached for a long time.

Redis's active expiry is more aggressive about reclaiming memory, which is important because Redis supports persistence (RDB/AOF) — expired keys in memory mean expired keys on disk.

---

## 8. Per-field TTL (Redis 7.4+)

Traditional Redis TTL applies to the entire key. If you have a hash representing a user session:

```
HSET user:1234 name "Alice" token "abc123" cart "{...}" preferences "{...}"
EXPIRE user:1234 86400    # Entire hash expires in 24 hours
```

But what if you want the `token` field to expire in 1 hour while the `preferences` field persists for 30 days? Before Redis 7.4, you would need separate keys for each field with different lifetimes.

### New commands (Redis 7.4+)

```
# Set TTL on individual hash fields
HEXPIRE key seconds FIELDS count field [field ...]
HPEXPIRE key milliseconds FIELDS count field [field ...]
HEXPIREAT key unix-timestamp FIELDS count field [field ...]
HPEXPIREAT key unix-ms-timestamp FIELDS count field [field ...]

# Query remaining TTL on hash fields
HTTL key FIELDS count field [field ...]
HPTTL key FIELDS count field [field ...]

# Remove TTL from hash fields (make permanent)
HPERSIST key FIELDS count field [field ...]
```

### Example

```
HSET user:1234 name "Alice" token "abc123" cart "{...}" preferences "{...}"

# Token expires in 1 hour
HEXPIRE user:1234 3600 FIELDS 1 token

# Cart expires in 2 hours
HEXPIRE user:1234 7200 FIELDS 1 cart

# Name and preferences: no per-field TTL (persist with the key)
EXPIRE user:1234 2592000    # Key-level: 30 days

# Check field TTLs
HTTL user:1234 FIELDS 2 token cart
# Returns: [3598, 7198]
```

### Use cases

- **User sessions:** Different session attributes with different lifetimes (auth token vs display preferences).
- **Rate limiting:** Hash fields representing individual rate-limit windows that expire independently.
- **Feature flags:** Per-user feature overrides that expire after an A/B test window.
- **Caching composite objects:** An object with fields sourced from different upstream systems, each with different cache durations.

### How it works internally

Each hash field with a TTL gets an entry in a per-key field-level expiry structure. The same passive + active expiry mechanisms apply at the field level: accessing an expired field triggers lazy deletion, and the background sweep also covers field-level expirations.

---

## Summary

- Redis stores TTL as an absolute Unix timestamp in milliseconds in a separate `expires` dict.
- **Passive expiry** checks on every key access — instant correctness, zero overhead for unaccessed keys.
- **Active expiry** samples 20 random keys per cycle, `hz` times per second — eventual cleanup of unaccessed expired keys with bounded CPU.
- The hybrid approach eliminates both stale reads (passive) and memory leaks (active).
- Replicas do not independently expire keys; the leader sends explicit `DEL` commands via replication.
- Add jitter to TTLs to avoid mass expiry latency spikes.
- Memcached uses lazy expiry only and relies on LRU eviction for memory reclamation of expired items.
- Redis 7.4+ supports per-field TTL on hashes via `HEXPIRE` and related commands.
