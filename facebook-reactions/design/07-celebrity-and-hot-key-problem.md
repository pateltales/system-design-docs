# Celebrity and Hot Key Problem

## 1. The Hot Key Problem

In a Facebook Reactions system, reactions are stored in a database sharded by `entityId`
(the post being reacted to). This means **all reactions for a single post land on the
same shard**. For normal posts this is fine. For viral posts from celebrities, it is
catastrophic.

### Scale Math

Facebook has 3.07 billion monthly active users. TAO operates across hundreds of
thousands of shards, partitioned by entity.

Consider a celebrity with **100 million followers** who publishes a post that goes viral:

```
Followers:              100,000,000
React within first hour:       10%  =  10,000,000 reactions
Time window:                60 min  =  3,600 seconds

Reaction rate:  10,000,000 / 3,600  =  ~2,778 reactions/sec  (average)

Peak rate (first few minutes):      =  ~167,000 reactions/sec

Single MySQL shard capacity:        =  ~10,000 writes/sec
```

**167K writes/sec vs 10K capacity = 16.7x overload.** The shard either queues to
exhaustion, times out connections, or crashes entirely. Every other post on that shard
becomes collateral damage.

```
Normal post write path (works fine):

  User reacts ──> API Server ──> Shard #4821 ──> INSERT reaction
                                  (10 writes/sec -- no problem)


Viral post write path (shard meltdown):

  167K users/sec ──> API Servers ──> Shard #4821 ──> 167K INSERTs/sec
                                      |
                                      v
                                   OVERLOADED
                                   Queue depth explodes
                                   Latency spikes to seconds
                                   Timeouts cascade
                                   Other posts on shard #4821 affected
```

---

## 2. Solutions

### 2a. Sharded Counters

Instead of maintaining a single counter row per entity:

```
┌──────────────────────────────────────┐
│  reaction_counts                     │
│  (entityId, reaction_type, count)    │
│                                      │
│  ("post_123", "LIKE", 4,200,000)     │   <-- single row, single lock
└──────────────────────────────────────┘
```

Split the counter into **N shards**:

```
┌──────────────────────────────────────────────────┐
│  reaction_count_shards                           │
│  (entityId, shardId, reaction_type, count)       │
│                                                  │
│  ("post_123",   0, "LIKE", 16,405)               │
│  ("post_123",   1, "LIKE", 16,512)               │
│  ("post_123",   2, "LIKE", 16,389)               │
│  ...                                             │
│  ("post_123", 255, "LIKE", 16,490)               │
└──────────────────────────────────────────────────┘

Total LIKE count = SUM of all 256 shards = 4,200,000
```

**How writes work:**

Each incoming reaction picks a counter shard, either randomly or by
`hash(userId) % N`, and increments only that shard's row.

```
User reacts ──> shardId = hash(userId) % 256
             ──> UPDATE reaction_count_shards
                 SET count = count + 1
                 WHERE entityId = "post_123"
                   AND shardId = <computed>
                   AND reaction_type = "LIKE"
```

**Capacity math with 256 shards:**

```
Incoming rate:        167,000 writes/sec
Counter shards:       256
Per-shard rate:       167,000 / 256 = ~652 writes/sec per shard

MySQL shard capacity: ~10,000 writes/sec
Load factor:          652 / 10,000 = 6.5%   (comfortable)
```

**Trade-off:** Reads go from O(1) to O(N), but N is small (256). In practice the sum
is computed once and **cached**. Reads never fan out to 256 rows on every request.

---

### 2b. Write Buffering / Batching

Accumulate reactions in an in-memory buffer (Redis) for a short window (1-5 seconds),
then flush a single batch write to MySQL.

```
                    1-5 sec buffer
                   ┌─────────────┐
Users ──> API ──>  │   Redis     │ ──(every N sec)──> MySQL
                   │             │     batch write
                   │ LIKE: +347  │     UPDATE count
                   │ LOVE: +89   │       SET count = count + 347
                   │ HAHA: +22   │
                   └─────────────┘

Instead of 347 individual writes in 1 second,
MySQL receives 1 write every 1 second.
```

**Risks and mitigations:**

| Risk | Mitigation |
|------|------------|
| Redis crash loses buffered counts | Acceptable for aggregate counts (off by a few hundred at most). Individual reaction records are written to Kafka first (durable). |
| Counts temporarily lag reality | For a social network, counts being 1-5 seconds stale is invisible to users. |
| Buffer memory pressure | Cap buffer size per entity. Flush early if buffer exceeds threshold. |

**Individual reaction records** (who reacted with what) are written to a durable
Kafka topic immediately. The buffer only aggregates the counter deltas. This way
you never lose the record of who reacted -- only the counter might be briefly behind.

---

### 2c. Queue-Based Write Leveling

Write all reactions to Kafka first, then consume at a controlled rate that the
database can handle.

```
                         Kafka                          MySQL
                   ┌──────────────┐              ┌──────────────┐
Users ──> API ──>  │  topic:      │  ──(drain)──>│  reactions    │
  (167K/sec)       │  reactions   │   (10K/sec)  │  table        │
                   │              │              │              │
                   │  partition   │              │              │
                   │  by entityId │              │              │
                   └──────────────┘              └──────────────┘
                        │
                        │  Kafka absorbs the burst.
                        │  Consumer pulls at DB's pace.
                        │  Backlog drains over minutes.
                        │
                        │  167K/sec burst for 5 min = 50M events
                        │  Drain at 10K/sec = ~83 min to clear backlog
```

**Key design decisions:**

- **Partition by entityId**: maintains ordering per entity, so reaction and
  un-reaction for the same user on the same post are processed in order.
- **Consumer rate limiting**: consumer pulls at a rate the downstream shard can
  handle. Kafka handles the backpressure.
- **Latency trade-off**: counts for viral posts may lag by seconds to minutes.
  For celebrity posts this is expected and acceptable -- users see "4.2M" not
  "4,201,337".

---

### 2d. Separate Hot / Cold Paths

Detect viral posts in real time and route them through a specialized high-throughput
pipeline. Normal posts continue through the standard path.

```
                              ┌─────────────────┐
                              │  Hot Key         │
                              │  Detector        │
                              │                  │
                              │  rate > 1000/sec │
                              │  = HOT           │
                              └────────┬─────────┘
                                       │
                         ┌─────────────┴──────────────┐
                         │                            │
                    HOT PATH                     COLD PATH
                         │                            │
                         v                            v
              ┌────────────────┐           ┌────────────────┐
              │  Redis         │           │  MySQL         │
              │  (real-time    │           │  (direct       │
              │   counters)    │           │   write)       │
              │                │           │                │
              │  Sharded       │           │  Single row    │
              │  counters in   │           │  counter       │
              │  Redis         │           │                │
              └───────┬────────┘           └────────────────┘
                      │
                      │  async reconciliation
                      │  (every 30-60 sec)
                      v
              ┌────────────────┐
              │  MySQL         │
              │  (persistent   │
              │   storage)     │
              └────────────────┘
```

**Hot path details:**

- Redis handles 100K+ writes/sec easily (single-threaded, in-memory, O(1) INCR).
- Sharded counters within Redis further distribute lock contention.
- A background job reconciles Redis counters to MySQL periodically.
- Individual reaction records still flow through Kafka to MySQL for durability.

**Cold path details:**

- Normal MySQL write path. No extra infrastructure.
- This is the path ~99.9% of posts use. Only a few posts at any time are "hot."

**Detection:**

- Monitor reaction rate per `entityId` in a sliding window (e.g., last 60 seconds).
- When rate exceeds threshold (e.g., 1,000 reactions/sec), flag entity as hot.
- When rate drops below threshold for a sustained period, deactivate and drain
  remaining Redis counters to MySQL.

---

## 3. Read Hot Spot

A viral post is not only write-heavy -- it is also **read-heavy**. If 100M people
see the post in their News Feed, every feed render queries the reaction count.

```
100M feed renders over 1 hour = ~27,800 reads/sec for one entity
```

### Mitigations

| Technique | How it helps |
|-----------|-------------|
| **Short TTL cache** | Cache count with 5-10 second TTL. 27,800 reads/sec collapse to 1 cache miss per 5 seconds. |
| **Stale-while-revalidate** | Serve stale cached count immediately while asynchronously refreshing. Users never see latency, counts are at most a few seconds behind. |
| **Replicated hot keys** | For extremely hot keys, replicate the cached value across multiple cache servers. Scatter reads across replicas to prevent any single cache node from becoming a hot spot. |
| **TAO follower caches** | TAO's architecture already replicates data to follower cache tiers in multiple regions. Read load for a hot entity naturally distributes across follower caches. |

```
Normal read path:

  Feed render ──> Cache (hit) ──> return count
                     │
                     │ (miss, every 5 sec)
                     v
                  MySQL ──> populate cache


Hot key read path (replicated cache):

  Feed render ──> consistent hash ──> Cache replica 1 ──> return count
  Feed render ──> consistent hash ──> Cache replica 2 ──> return count
  Feed render ──> consistent hash ──> Cache replica 3 ──> return count
                                           │
                                      (miss on any replica)
                                           v
                                        MySQL ──> populate that replica
```

---

## 4. Hot Key Detection

Detecting hot keys in real time is itself a systems problem. You cannot maintain an
exact per-entity counter for every entity in the system (there are billions of posts).

### Count-Min Sketch

A probabilistic data structure that estimates frequency of events in a stream using
sub-linear memory.

```
Incoming reactions stream:

  (post_A, LIKE), (post_B, LOVE), (post_A, LIKE), (post_A, HAHA), ...
       │
       v
  ┌──────────────────────────────┐
  │  Count-Min Sketch            │
  │                              │
  │  hash1(entityId) -> row 1    │
  │  hash2(entityId) -> row 2    │
  │  hash3(entityId) -> row 3    │
  │                              │
  │  Estimate = min(row values)  │
  │                              │
  │  Memory: O(width * depth)    │
  │  e.g., 10,000 * 5 = 50KB    │
  └──────────────────────────────┘
       │
       v
  if estimate(entityId) > threshold in sliding window:
       mark entityId as HOT
       activate sharded counters + write buffering
```

**Properties:**
- Never undercounts (may overcount slightly).
- Fixed memory regardless of number of distinct entities.
- Can be maintained per API server, or centrally in Redis.

### Activation / Deactivation Flow

```
1. Entity reaction rate crosses threshold
   ──> Mark entity as HOT in a distributed set (Redis SET or ZooKeeper)
   ──> Subsequent writes for this entity routed to hot path

2. Entity reaction rate drops below threshold for 5+ minutes
   ──> Drain remaining buffered counts to MySQL
   ──> Remove entity from HOT set
   ──> Subsequent writes routed back to cold path
```

---

## 5. Contrast with Other Platforms

| Platform | Problem | Approach |
|----------|---------|----------|
| **Facebook** | Celebrity posts with 100M+ follower reach. Millions of reactions in minutes. | Sharded counters in TAO, write buffering, hot/cold path split, follower caches for reads. |
| **Twitter** | Viral tweets from accounts with tens of millions of followers. Like/retweet counts spike. | Sharded counters, Redis-based real-time counters, fan-out-on-read for counts. Similar overall approach to Facebook. |
| **Reddit** | Popular posts on large subreddits get millions of votes. | **Fuzzy vote counts**: intentionally adds noise to displayed counts. This reduces the precision requirement, allowing more aggressive caching and less frequent counter reconciliation. Counts shown are approximate by design. |
| **YouTube** | Videos can accumulate millions of likes over hours/days. Premiere events cause spikes. | Write buffering and async count propagation. View and like counts are explicitly marked as approximate ("1.2M likes") and update with visible delay. |

### Key Insight

All platforms converge on the same core techniques:
1. **Shard the counter** to distribute write contention.
2. **Buffer writes** to reduce per-second pressure on persistent storage.
3. **Accept approximate counts** for display (exact counts are only needed for
   internal consistency, not for what the user sees).
4. **Cache aggressively** on the read path with short TTLs.

The differences are mostly in how transparently they communicate approximation to
users (Reddit is explicit about fuzzy counts; YouTube shows delayed counts; Facebook
shows near-real-time but internally approximate counts).

---

## Summary: Combined Architecture for a Hot Post

```
User taps reaction
       │
       v
  ┌──────────┐     ┌───────────────────┐
  │ API      │────>│ Kafka             │   (durable record of every reaction)
  │ Server   │     │ topic: reactions  │
  └──────────┘     └───────────────────┘
       │
       │  check: is this entity HOT?
       │
       ├──── NO (cold path) ──────────────> MySQL direct write
       │                                    (single counter row)
       │
       └──── YES (hot path) ──┐
                               v
                        ┌─────────────┐
                        │ Redis       │
                        │ sharded     │   INCR entityId:shard:reaction_type
                        │ counters    │
                        └──────┬──────┘
                               │
                               │ async reconciliation (every 30-60s)
                               v
                        ┌─────────────┐
                        │ MySQL       │   UPDATE count = redis_total
                        │ persistent  │
                        │ storage     │
                        └─────────────┘

  Meanwhile, Kafka consumer drains individual reaction records
  to MySQL at a controlled rate (write leveling).

  Read path:
  Feed render ──> Cache (TTL 5s, stale-while-revalidate)
                     │ miss
                     v
                  Redis (if hot) or MySQL (if cold)
```

This architecture handles the celebrity post scenario:

| Metric | Without mitigation | With mitigation |
|--------|-------------------|-----------------|
| Peak write rate to single MySQL shard | 167,000/sec | ~650/sec (sharded counters) or batched to ~1/sec (buffering) |
| Shard health | Crashed | Healthy at <10% capacity |
| Count accuracy | N/A (shard down) | Eventual (seconds behind reality) |
| Read latency for count | N/A (shard down) | <1ms (cache hit) |
| Collateral damage to other posts | All posts on shard affected | Zero -- hot post isolated to its own path |
