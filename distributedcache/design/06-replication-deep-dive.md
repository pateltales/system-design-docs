# Replication & High Availability -- Deep Dive

Redis uses asynchronous leader-follower replication to provide read scaling and
fault tolerance. Combined with Redis Sentinel (or Redis Cluster's built-in failover),
it delivers high availability with automatic failover.

---

## 1. Async Leader-Follower Replication

### Architecture

```
         Writes                    Reads (optional)
           │                            │
           ▼                            ▼
    ┌─────────────┐   async     ┌─────────────┐
    │   LEADER    │ ──────────> │  FOLLOWER 1  │
    │  (primary)  │ ──────┐    └─────────────┘
    │             │       │
    │  Handles    │       │     ┌─────────────┐
    │  ALL writes │       └───> │  FOLLOWER 2  │
    └─────────────┘             └─────────────┘
```

**Key properties:**

- The leader processes **all write commands**. Followers are read-only by default
  (`replica-read-only yes`).
- Replication is **asynchronous**: the leader does not wait for followers to
  acknowledge writes before responding to the client. This means the leader returns
  `OK` to a SET command before any follower has received the data.
- The leader maintains an **in-memory replication backlog** -- a circular buffer of
  recent write commands. This buffer enables partial resynchronization when followers
  temporarily disconnect.
- Replication is **non-blocking on the leader side**: the leader continues serving
  clients during full and partial resyncs.

### Configuring a Follower

```bash
# On the follower instance
REPLICAOF <leader-host> <leader-port>

# Or in redis.conf
replicaof 192.168.1.10 6379

# To promote a follower to standalone
REPLICAOF NO ONE
```

### Replication Stream

After initial synchronization, the leader streams every write command to all
connected followers in real time. The stream uses the same RESP protocol as client
commands. Each byte in the stream has an **offset** (monotonically increasing),
which both leader and follower track to measure replication progress.

---

## 2. Full Resync

### When It Triggers

Full resynchronization occurs when:
1. A follower connects to a leader **for the first time**.
2. A follower reconnects after a long disconnection and the gap **exceeds the
   replication backlog size** (the leader no longer has the commands the follower
   missed).
3. The replication ID does not match (the follower was previously replicating from
   a different leader).

### Full Resync Flow

```
┌──────────────┐                           ┌──────────────┐
│   FOLLOWER   │                           │    LEADER    │
└──────┬───────┘                           └──────┬───────┘
       │                                          │
       │  1. PSYNC <replid> <offset>              │
       │  (or PSYNC ? -1 for first connect)       │
       │ ────────────────────────────────────────> │
       │                                          │
       │                           2. Leader sees full resync needed
       │                              Responds: +FULLRESYNC <replid> <offset>
       │ <──────────────────────────────────────── │
       │                                          │
       │                           3. Leader triggers BGSAVE
       │                              fork() + write dump.rdb
       │                              (continues serving clients)
       │                                          │
       │                              NEW WRITES during BGSAVE
       │                              buffered in replication buffer
       │                                          │
       │                           4. BGSAVE completes
       │                                          │
       │         5. RDB file streamed to follower  │
       │ <════════════════════════════════════════ │
       │         (bulk transfer, can be GBs)       │
       │                                          │
       │  6. Follower flushes old data            │
       │     Loads RDB into memory                │
       │                                          │
       │         7. Buffered writes streamed       │
       │ <════════════════════════════════════════ │
       │         (commands that arrived during     │
       │          RDB generation + transfer)       │
       │                                          │
       │  8. Follower applies buffered writes     │
       │     Now in sync. Continuous stream begins │
       │ <════════════════════════════════════════ │
       │                                          │
```

### Critical: Replication Buffer Overflow

During steps 3-5, the leader buffers all new write commands in an **output buffer**
for the follower. This buffer has a limit:

```
client-output-buffer-limit replica 256mb 64mb 60
```

This means:
- Hard limit: if the buffer reaches **256 MB**, the follower is disconnected.
- Soft limit: if the buffer stays above **64 MB** for **60 seconds**, the follower
  is disconnected.

**The dangerous cycle**: If the write rate is very high and the RDB is large (slow to
generate and transfer), the buffer can overflow. The follower is disconnected, then
reconnects, triggering another full resync, which again overflows the buffer. This
creates an **infinite resync loop** that consumes CPU (fork), memory (buffer), and
network (RDB transfer) without ever completing.

**Mitigations:**
- Increase `client-output-buffer-limit replica` to accommodate the burst.
- Use smaller datasets per instance (shard the data).
- Use faster disks and network to reduce RDB generation and transfer time.
- Ensure replication backlog is large enough to prevent unnecessary full resyncs
  (see section 3).

---

## 3. Partial Resync (PSYNC2, Redis 4.0+)

### Replication Backlog

The leader maintains a **circular buffer** called the replication backlog. Every byte
of the replication stream is written to this buffer along with its offset.

```
Replication Backlog (circular buffer):
┌──────────────────────────────────────────────────┐
│ ... SET x 1 | INCR y | DEL z | SET a 2 | ...    │
│     ▲                                   ▲        │
│     │                                   │        │
│   oldest                             newest      │
│   offset: 10485760         offset: 10487936      │
└──────────────────────────────────────────────────┘

Default size: repl-backlog-size 1mb (1,048,576 bytes)
```

When a follower disconnects and reconnects, it sends:
```
PSYNC <replication-id> <last-offset>
```

If `last-offset` is still within the backlog (i.e., the follower's gap fits in the
buffer), the leader responds with `+CONTINUE` and streams only the missing bytes.
No fork, no RDB, no full transfer.

### Backlog Sizing Formula

```
repl-backlog-size >= write_throughput_bytes_per_sec x max_expected_disconnect_seconds
```

**Example:**
- Write throughput: 10 MB/s
- Max expected disconnect (network blip, follower restart): 60 seconds
- Required backlog: 10 MB/s x 60 s = **600 MB**

```
repl-backlog-size 600mb
```

Setting this too small causes unnecessary full resyncs. Setting it too large wastes
memory. Monitor `master_repl_offset` growth rate to calibrate.

### PSYNC2: Dual Replication IDs (Redis 4.0+)

**The problem PSYNC2 solves:**

Before PSYNC2, when a follower was promoted to leader (after a failover), it got a
new replication ID. All other followers saw a different replication ID and triggered
a full resync -- even though they had nearly identical data.

**The solution:**

```
PROMOTED FOLLOWER (new leader):
  replication_id:        NEW_ID   (new primary identity)
  replication_id_2:      OLD_ID   (previous leader's ID, kept as secondary)
  second_repl_offset:    12345    (offset at the moment of promotion)
```

When other followers connect to the newly promoted leader and send:
```
PSYNC OLD_ID 12300
```

The new leader checks: "Is `OLD_ID` my secondary replication ID, and is offset
`12300` within my backlog?" If yes, it responds with `+CONTINUE` and streams only
the delta. **No full resync needed.**

```
BEFORE FAILOVER:
                                 repl_id = ABC123
┌──────────┐    replicates     ┌──────────┐    replicates     ┌──────────┐
│Follower A│ <──────────────── │  LEADER  │ ──────────────── >│Follower B│
│offset:120│                   │offset:125│                   │offset:118│
└──────────┘                   └──────────┘                   └──────────┘

AFTER FAILOVER (Follower A promoted):
                                 repl_id   = XYZ789 (new)
                                 repl_id_2 = ABC123 (old leader's ID)
┌──────────┐                   ┌──────────┐
│ NEW LEADER│                  │Follower B│
│(was Fol A)│                  │offset:118│
│offset:120 │                  │repl_id: ABC123│
└──────────┘                   └──────────┘
                                     │
      PSYNC ABC123 118               │
      ◄──────────────────────────────┘

      New leader checks: ABC123 == repl_id_2? YES
      Offset 118 in backlog? YES
      Response: +CONTINUE (partial resync!)
```

This dramatically reduces failover disruption. Without PSYNC2, every failover
triggers full resyncs on all followers.

---

## 4. WAIT Command

### Syntax

```
WAIT <numreplicas> <timeout_ms>
```

Blocks the client until `numreplicas` followers have acknowledged receiving all
writes issued by this client, or until `timeout_ms` expires.

### Example

```
SET critical-key "important-value"
WAIT 2 5000
# Blocks until 2 replicas confirm they received the SET,
# or 5 seconds elapse, whichever comes first.
# Returns the number of replicas that acknowledged.
```

### Important Limitations

- WAIT provides **best-effort synchronous replication**, NOT strong consistency.
- If the leader crashes **after** sending the write to followers but **before** WAIT
  returns, the data exists on followers and survives.
- If the leader crashes **after** acknowledging the client but **before** sending to
  any follower, the data is **lost** -- even though the client received OK and WAIT
  would have eventually returned.
- WAIT does not make Redis a CP system. It reduces the window of data loss but cannot
  eliminate it entirely.

### Use Cases

- Financial transactions where you want best-effort durability across nodes.
- Critical writes (user account creation, payment records) where losing even 1 second
  of data is unacceptable but you can tolerate the latency overhead.
- Combined with `min-replicas-to-write` for additional safety.

---

## 5. Replication Lag Monitoring

### Key Metrics

```
INFO replication
```

**On the leader:**
```
role:master
connected_slaves:2
slave0:ip=10.0.0.2,port=6379,state=online,offset=1234567,lag=0
slave1:ip=10.0.0.3,port=6379,state=online,offset=1234500,lag=1
master_repl_offset:1234567
repl_backlog_active:1
repl_backlog_size:1048576
repl_backlog_first_byte_offset:234567
```

**Lag calculation:**
```
Lag in bytes = master_repl_offset - slave_repl_offset
             = 1234567 - 1234500
             = 67 bytes

Lag in seconds = lag field (estimated by follower heartbeat, sent every second)
```

### Alerting Thresholds

| Metric                                         | Warning         | Critical        |
|------------------------------------------------|-----------------|-----------------|
| Lag in bytes                                   | > 10 MB         | > 100 MB        |
| Lag in seconds                                 | > 5 sec         | > 30 sec        |
| `connected_slaves` < expected count            | --              | Immediately     |
| `master_repl_offset` not increasing            | After 60 sec    | After 300 sec   |

### Reading Stale Data

Because replication is async, followers may serve stale data. For workloads that
require read-your-writes consistency, either:
1. Read from the leader.
2. Use WAIT after the write, then read from a follower.
3. Track offsets client-side and only read from followers that have caught up.

---

## 6. Redis Sentinel

Redis Sentinel is a **separate process** that provides monitoring, notification, and
automatic failover for Redis leader-follower deployments.

### Architecture

```
┌───────────┐     ┌───────────┐     ┌───────────┐
│ Sentinel 1│     │ Sentinel 2│     │ Sentinel 3│
│  :26379   │     │  :26379   │     │  :26379   │
└─────┬─────┘     └─────┬─────┘     └─────┬─────┘
      │                 │                 │
      │    monitors     │    monitors     │    monitors
      │    (PING)       │    (PING)       │    (PING)
      ▼                 ▼                 ▼
┌───────────┐     ┌───────────┐     ┌───────────┐
│   LEADER  │────>│ FOLLOWER 1│     │ FOLLOWER 2│
│   :6379   │────>│   :6380   │     │   :6381   │
└───────────┘     └───────────┘     └───────────┘
```

**Deploy at least 3 Sentinels** (odd number) so that a majority quorum can be
reached. With 3 Sentinels and quorum set to 2, the system tolerates 1 Sentinel
failure.

### SDOWN and ODOWN

**SDOWN (Subjective Down):**
- A single Sentinel decides a node is down because it has not received a valid reply
  to PING within `down-after-milliseconds` (default: **30000 ms** = 30 seconds).
- This is a **local, unilateral** judgment.

**ODOWN (Objective Down):**
- A Sentinel that has marked the leader as SDOWN asks other Sentinels: "Do you also
  think the leader is down?"
- If **quorum** Sentinels agree the leader is SDOWN, it is marked ODOWN.
- ODOWN only applies to the **leader**. Followers can be SDOWN but never ODOWN
  (there is no failover for followers, they just get flagged as unavailable).

```
Sentinel 1: PING leader → no reply for 30s → marks SDOWN
Sentinel 1: asks Sentinel 2, Sentinel 3: "Is leader down?"
Sentinel 2: "Yes, I also see SDOWN"
Sentinel 3: "Yes, I also see SDOWN"
             ───────────────────────
             Quorum (2) reached → ODOWN
             Failover begins.
```

### Sentinel Leader Election

Once ODOWN is declared, Sentinels must agree on **which Sentinel** will perform the
failover. They use a **Raft-like leader election protocol**:

1. The Sentinel that detected ODOWN increments its current epoch and requests votes
   from other Sentinels: "Vote for me to perform failover for epoch N."
2. Each Sentinel votes for the first candidate it receives in a given epoch
   (first-come-first-served).
3. The candidate that receives votes from a **majority** of Sentinels becomes the
   Sentinel leader and performs the failover.

### Failover Sequence

```
┌─────────────────────────────────────────────────────────────────┐
│                    SENTINEL FAILOVER SEQUENCE                    │
├─────────────────────────────────────────────────────────────────┤
│                                                                 │
│  1. ODOWN detected for leader                                   │
│     │                                                           │
│     ▼                                                           │
│  2. Sentinel leader election (Raft-like)                        │
│     Sentinels vote → one Sentinel wins majority                 │
│     │                                                           │
│     ▼                                                           │
│  3. Sentinel leader selects best follower                       │
│     Selection criteria (in order):                              │
│     a) Exclude followers marked SDOWN or disconnected           │
│     b) Exclude followers with stale data (large repl lag)       │
│     c) Lowest replica-priority value wins (0 = never promote)   │
│     d) Highest replication offset wins (most data)              │
│     e) Lowest run ID wins (tiebreaker)                          │
│     │                                                           │
│     ▼                                                           │
│  4. Promote chosen follower                                     │
│     Send: REPLICAOF NO ONE                                      │
│     Follower becomes a standalone leader                        │
│     │                                                           │
│     ▼                                                           │
│  5. Reconfigure remaining followers                             │
│     Send: REPLICAOF <new-leader-ip> <new-leader-port>           │
│     All followers now replicate from the new leader             │
│     │                                                           │
│     ▼                                                           │
│  6. Update old leader configuration                             │
│     If old leader comes back, Sentinel sends:                   │
│     REPLICAOF <new-leader-ip> <new-leader-port>                 │
│     Old leader demoted to follower                              │
│     │                                                           │
│     ▼                                                           │
│  7. Notify clients                                              │
│     Sentinels publish on Pub/Sub channels:                      │
│       +switch-master <name> <old-ip> <old-port>                 │
│                              <new-ip> <new-port>                │
│     Clients subscribed to Sentinel discover the new leader      │
│                                                                 │
└─────────────────────────────────────────────────────────────────┘
```

### Client Discovery

Clients do not hardcode the leader's address. Instead, they:
1. Connect to any Sentinel.
2. Ask: `SENTINEL get-master-addr-by-name <master-name>`
3. Sentinel returns the current leader's IP and port.
4. Client connects to the leader.
5. Client subscribes to Sentinel's Pub/Sub channel `+switch-master` to be notified
   of failovers.

Most Redis client libraries (Jedis, Lettuce, redis-py) have built-in Sentinel
support that handles this automatically.

### Split-Brain Mitigation

A network partition can cause the old leader to remain writable while a new leader
is elected on the other side of the partition. This creates **two leaders** (split
brain), and writes to the old leader are lost when the partition heals and it is
demoted to a follower.

**Mitigation:**

```
min-replicas-to-write 1
min-replicas-max-lag 10
```

This tells the leader: "Refuse writes unless at least 1 follower has acknowledged
replication within the last 10 seconds." If the leader is partitioned away from all
followers, it stops accepting writes, preventing the split-brain data loss.

**Trade-off**: This reduces availability. If all followers are down or lagging, the
leader refuses writes even though it is healthy. You are trading availability for
consistency.

### Sentinel Configuration

```
# sentinel.conf
sentinel monitor mymaster 192.168.1.10 6379 2
#                name     ip             port quorum

sentinel down-after-milliseconds mymaster 30000
# Time without reply before SDOWN (30 seconds)

sentinel failover-timeout mymaster 180000
# Max time for a failover operation (3 minutes)

sentinel parallel-syncs mymaster 1
# How many followers resync concurrently after failover
# (1 = one at a time, minimizes impact on remaining followers)
```

---

## 7. Sentinel vs Redis Cluster Failover

| Property               | Sentinel                              | Redis Cluster                       |
|------------------------|---------------------------------------|-------------------------------------|
| Use case               | Standalone Redis (data fits 1 node)   | Sharded Redis (data across N nodes) |
| Separate process?      | Yes (sentinel binary/mode)            | No (built into redis-server)        |
| Failure detection      | SDOWN → ODOWN via Sentinel quorum     | PFAIL → FAIL via node gossip        |
| Leader election        | Raft-like among Sentinels             | Raft-like among followers of failed leader |
| Promotion              | Sentinel sends REPLICAOF NO ONE       | Cluster follower self-promotes      |
| Client routing         | Client asks Sentinel for leader IP    | MOVED/ASK redirects, cluster-aware clients |
| Sharding               | No (Sentinel is HA only)              | Yes (16384 hash slots)             |
| Minimum nodes          | 1 leader + 1 follower + 3 Sentinels   | 3 leaders + 3 followers (6 nodes) |

**When to use which:**
- **Sentinel**: Your dataset fits on a single machine. You want HA and automatic
  failover without the complexity of a cluster.
- **Cluster**: Your dataset exceeds the memory of a single machine, or you need
  horizontal write scaling. Cluster provides both sharding and built-in failover.

Both use similar Raft-like election mechanisms, but they operate in different
protocols and contexts. You do **not** use Sentinel with Redis Cluster -- Cluster
handles its own failover internally.

---

## 8. Contrast with Memcached

| Feature                    | Redis                                    | Memcached                            |
|----------------------------|------------------------------------------|--------------------------------------|
| Replication                | Built-in async leader-follower           | **None**                             |
| Automatic failover         | Sentinel / Cluster                       | **None**                             |
| Read replicas              | Yes (follower can serve reads)           | **None**                             |
| Data survival on node loss | Followers have near-complete copy        | **Data gone permanently**            |
| Partial resync             | PSYNC2 with replication backlog          | **N/A**                              |
| Split-brain protection     | min-replicas-to-write                    | **N/A**                              |

**Memcached's approach to HA:**
- Memcached has **no replication**. If a node dies, all data on that node is lost.
- Clients can mitigate this at the application level by writing to multiple nodes
  (application-managed redundancy), but this is not a Memcached feature.
- Some proxy layers (e.g., mcrouter from Facebook) add replication on top of
  Memcached, but this is external infrastructure, not built into Memcached itself.
- The consistent hashing ring (used for sharding) simply routes around dead nodes,
  but the data on the dead node is gone. Cache misses spike until the data is
  repopulated from the backing store.

**Bottom line**: If you need your cache layer to survive node failures without a
thundering herd of cache misses hitting your database, Redis replication is the
answer. Memcached assumes you always have a backing data store that can handle the
load when cache nodes fail.

---

## Summary

Redis replication provides a spectrum of durability and availability guarantees:

1. **Async replication** gives you read scaling and basic redundancy with minimal
   latency overhead on the leader.
2. **Partial resync (PSYNC2)** minimizes disruption during brief disconnections and
   failovers by avoiding full RDB transfers.
3. **WAIT** provides best-effort synchronous replication for critical writes.
4. **Sentinel** automates failover for standalone deployments with SDOWN/ODOWN
   detection and Raft-like leader election.
5. **min-replicas-to-write** prevents split-brain data loss at the cost of
   availability.

None of this exists in Memcached. For any use case where data loss or extended
downtime is unacceptable, Redis replication and Sentinel (or Cluster) provide the
mechanisms to handle it.
