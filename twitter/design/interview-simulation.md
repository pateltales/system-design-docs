# System Design Interview Simulation: Design Twitter

> **Interviewer:** Principal Engineer, Amazon  
> **Candidate Level:** SDE-3 (L6 — Senior Software Development Engineer)  
> **Duration:** ~60 minutes  
> **Date:** June 2, 2026

---

## PHASE 1: Opening & Problem Statement (~2 min)

**Interviewer:**  
Hey, thanks for coming in today. I'm a Principal Engineer on [redacted] org here at Amazon. For this round we're going to do a system design exercise. I want you to design Twitter — well, let's call it a Twitter-like social media platform. You know what Twitter is, right? Short-form posts, following people, a feed — that kind of thing.

Before you jump into anything, I want to be clear: there's no single "right" answer here. What I care about is your thought process — how you break down ambiguity, how you make trade-offs, and how you reason about scale. I'll be poking at your decisions along the way, so don't take that as a negative signal. That's just how I calibrate depth.

Take it away. How would you start?

---

## PHASE 2: Requirements Gathering & Scoping (~8 min)

**Candidate:**  
Thanks! Before I start drawing boxes, I'd like to align on scope. Twitter has a lot of features — tweeting, following, home timeline/feed, search, trending, direct messages, notifications, media uploads, ads… I want to make sure we focus on the highest-value pieces for this session.

Can I propose we scope to these **core functional requirements**?

1. **Post a tweet** (text, up to 280 chars; optionally with media)
2. **Follow / Unfollow** a user
3. **Home timeline (News Feed)** — see tweets from people you follow, in near-real-time, reverse-chronological (with possible ranking)
4. **User timeline** — see all tweets by a specific user

And de-prioritize (but mention): search, trending, DMs, notifications, ads, analytics.

**Interviewer:**  
That's a reasonable scope. I'd actually like you to keep **search** in your back pocket — we may circle back to it if we have time. But yeah, let's focus on posting, following, and the home timeline. Those are meaty enough.

What about non-functional requirements? What are you optimizing for?

**Candidate:**  
Good question. Here's how I'd frame the non-functionals:

| Dimension | Target |
|---|---|
| **Scale** | ~500M monthly active users, ~200M daily active users |
| **Tweet volume** | ~500M tweets/day → ~6,000 tweets/sec avg, peaks at ~12K/sec |
| **Read:Write ratio** | Heavily read-heavy. Rough estimate: 100:1 or higher. Every tweet is read by potentially thousands of followers. |
| **Availability** | High availability (favor AP in CAP). Users tolerate a few seconds of stale feed, but the service should never be "down." |
| **Latency** | Home timeline: < 200ms p99. Post tweet: < 500ms p99. |
| **Consistency** | Eventual consistency is acceptable for the feed. A tweet appearing 2-5 seconds late is fine. |
| **Durability** | Tweets must never be lost once acknowledged. |

**Interviewer:**  
Good. I like that you quantified the read-write ratio. That's going to drive a lot of your architecture. You mentioned 500M tweets/day — walk me through how you got that number.

**Candidate:**  
Sure. With ~200M DAU, I'm assuming roughly 2-3 tweets per active user per day on average (many users consume but don't post, power users post a lot — it averages out). That gives:

- 200M × 2.5 ≈ 500M tweets/day  
- 500M / 86,400 ≈ ~5,800 tweets/sec → I round to 6K/sec  
- With a 2x peak factor → ~12K writes/sec at peak

For reads: each user opens the app maybe 5-10 times a day, each time loading a timeline. 200M × 7 ≈ 1.4B timeline requests/day ≈ ~16K reads/sec average, ~50K+ at peak. And each timeline request might fan out to many backend calls.

**Interviewer:**  
That math checks out. Let's also think about storage. Give me a back-of-envelope estimate.

**Candidate:**  
Per tweet:
- tweet_id: 8 bytes  
- user_id: 8 bytes  
- text: 280 chars → ~280 bytes (UTF-8, worst case ~560 bytes)  
- timestamp: 8 bytes  
- metadata (like_count, retweet_count, media_urls, etc.): ~200 bytes  
- **Total per tweet: ~500 bytes** (conservatively ~1 KB with indices)

Daily: 500M × 1KB = **500 GB/day** of tweet data  
Yearly: ~180 TB/year of tweet data alone  
Over 5 years: ~1 PB

Media (images, videos) would be stored separately in object storage (like S3) and would dwarf text storage — potentially 10-50x more.

**Interviewer:**  
Good. That gives us a sense of the data volume. Let's move into the design.

---

## PHASE 3: High-Level Architecture (~10 min)

**Candidate:**  
Let me sketch the high-level architecture. I'll organize it around the core use cases.

```
                                    ┌─────────────┐
                                    │   Clients   │
                                    │ (iOS/Android/│
                                    │    Web)     │
                                    └──────┬──────┘
                                           │
                                    ┌──────▼──────┐
                                    │   CDN/Edge  │
                                    │  (CloudFront)│
                                    └──────┬──────┘
                                           │
                                    ┌──────▼──────┐
                                    │ API Gateway  │
                                    │ / Load       │
                                    │  Balancer    │
                                    └──────┬──────┘
                                           │
                    ┌──────────────────────┼──────────────────────┐
                    │                      │                      │
             ┌──────▼──────┐       ┌──────▼──────┐       ┌──────▼──────┐
             │  Tweet      │       │  Timeline   │       │  User /     │
             │  Service    │       │  Service    │       │  Graph      │
             │             │       │             │       │  Service    │
             └──────┬──────┘       └──────┬──────┘       └──────┬──────┘
                    │                      │                      │
                    │               ┌──────▼──────┐              │
                    │               │  Fanout     │              │
                    │               │  Service    │              │
                    │               └──────┬──────┘              │
                    │                      │                      │
             ┌──────▼──────┐       ┌──────▼──────┐       ┌──────▼──────┐
             │  Tweet      │       │  Timeline   │       │  Social     │
             │  Store      │       │  Cache      │       │  Graph      │
             │  (DB)       │       │  (Redis)    │       │  Store      │
             └─────────────┘       └─────────────┘       └─────────────┘
                    │
             ┌──────▼──────┐
             │  Media      │
             │  Store (S3) │
             └─────────────┘
```

**Core Services:**

1. **Tweet Service** — handles creating, reading, deleting tweets. Writes to the Tweet Store.
2. **Timeline Service** — serves the home timeline for a user. Reads from a pre-computed Timeline Cache.
3. **User / Social Graph Service** — manages user profiles, follow/unfollow relationships.
4. **Fanout Service** — when a tweet is posted, this service pushes the tweet to the timelines of all followers.

**Interviewer:**  
Walk me through the flow of posting a tweet, end to end. I want to understand how a tweet gets into someone's feed.

**Candidate:**  
Sure. Here's the **write path — posting a tweet:**

1. Client sends `POST /v1/tweets` with `{user_id, text, media_ids}` → hits the **API Gateway / Load Balancer**.
2. Request is routed to **Tweet Service**.
3. Tweet Service:
   a. Validates the request (text length, profanity filters, rate limiting).
   b. If media is attached, media was already uploaded via a separate `POST /v1/media/upload` endpoint to **Media Service** which stores it in S3 and returns a `media_id`.
   c. Generates a globally unique `tweet_id` (I'll discuss ID generation later).
   d. Writes the tweet to the **Tweet Store** (persistent DB).
   e. Returns `201 Created` to the client with the tweet_id.
4. **Asynchronously**, Tweet Service publishes a `TweetCreated` event to a **message queue** (e.g., Amazon SQS or Kafka).
5. **Fanout Service** consumes the event:
   a. Looks up the poster's follower list from the **Social Graph Service**.
   b. For each follower, prepends the `tweet_id` to that follower's timeline in the **Timeline Cache** (Redis sorted set, scored by timestamp).
6. The tweet is now in the followers' cached timelines.

**Interviewer:**  
Okay, stop there. Let's dig into fanout — that's where the interesting trade-offs are. What happens when a celebrity with 50 million followers posts a tweet?

---

## PHASE 4: Deep Dive — Fanout Strategy (~12 min)

**Candidate:**  
This is the classic **fanout-on-write vs. fanout-on-read** trade-off. Let me break it down:

### Fanout-on-Write (Push Model)
- When a user posts, we push the tweet_id into every follower's timeline cache.
- **Pros:** Timeline reads are fast — O(1) cache lookup, pre-computed.
- **Cons:** A celebrity with 50M followers means 50M writes per tweet. At 12K tweets/sec, if even 1% are from users with 1M+ followers, the fanout queue backs up. Write amplification is enormous.

### Fanout-on-Read (Pull Model)
- We don't pre-compute timelines. At read time, we fetch the list of people the user follows, then retrieve their recent tweets and merge/sort them.
- **Pros:** No write amplification.
- **Cons:** Read latency is high — you're doing N queries (one per followed user) at request time, then merge-sorting. This is expensive for users who follow 500+ accounts.

### My Recommendation: Hybrid Approach

I'd use a **hybrid model** — this is actually what Twitter does in practice:

| User Type | Fanout Strategy |
|---|---|
| **Regular users** (< 10K followers) | Fanout-on-write: push to followers' timeline caches. |
| **Celebrity/high-follower users** (> 10K followers) | Fanout-on-read: their tweets are NOT pushed. Instead, at read time, we merge them in. |

**How it works at read time:**
1. Timeline Service fetches the user's pre-computed timeline from Redis (tweets from regular users they follow).
2. Timeline Service also checks: "Does this user follow any celebrity accounts?"
3. If yes, it fetches the latest tweets from those celebrity accounts directly from the Tweet Store.
4. Merges the two lists, sorts by timestamp (or ranking score), and returns the top N results.

This caps the fanout per tweet — a regular user's tweet fans out to at most ~10K timeline caches, which is manageable. Celebrities skip fanout entirely, and their tweets are pulled on demand.

**Interviewer:**  
I like the hybrid approach. How do you determine the threshold — you said 10K followers. Is that configurable? What if someone goes from 9K to 11K followers?

**Candidate:**  
Good question. The threshold should be **configurable and not a hard binary cutoff**. I'd implement it as:

1. A configuration parameter stored in a feature flag / config service (e.g., 10K as default).
2. The social graph service maintains a **follower count** per user. When a follow event occurs, we check if the user crosses the threshold.
3. When a user crosses the threshold (goes from "regular" to "celebrity"), we:
   - Mark them as `is_celebrity = true` in the user metadata.
   - **Stop** fanning out their future tweets.
   - We do NOT need to retroactively clean up old timeline caches — old tweets will naturally age out.
4. When a user drops below the threshold, we can re-enable fanout. But in practice, this rarely happens, so I wouldn't over-engineer it.

There could also be a **grace zone** (hysteresis) — e.g., transition to celebrity at 12K, transition back at 8K — to avoid flip-flopping.

**Interviewer:**  
Smart. The hysteresis is a nice touch — shows you've dealt with this pattern before. Now let me push on the Timeline Cache. You said Redis sorted sets. Walk me through the data model in Redis.

**Candidate:**  
Sure. For each user's home timeline in Redis:

```
Key:    timeline:{user_id}
Type:   Sorted Set (ZSET)
Members: tweet_id (as string)
Score:   tweet timestamp (Unix epoch in milliseconds)
```

Operations:
- **Fanout write:** `ZADD timeline:{user_id} {timestamp} {tweet_id}` for each follower.
- **Read timeline:** `ZREVRANGEBYSCORE timeline:{user_id} +inf -inf LIMIT 0 50` → returns the 50 most recent tweet_ids.
- **Trimming:** We cap each timeline at ~800 entries. After each ZADD, we can `ZREMRANGEBYRANK timeline:{user_id} 0 -(max_size+1)` to evict the oldest entries.

Then, with the list of tweet_ids, we do a **multi-get** from the Tweet Store (or a tweet cache) to hydrate the full tweet objects.

**Memory estimation:**
- Each timeline entry: tweet_id (8 bytes) + score (8 bytes) + Redis overhead ≈ ~50 bytes per entry.
- 800 entries per user × 200M DAU = ~160B entries × 50 bytes = **~8 TB** of Redis memory.
- That's a lot, but with Redis Cluster across, say, 100 nodes with 128GB each (12.8 TB total), it's feasible. We can also only cache active users (e.g., users who've logged in within 7 days) to reduce the footprint.

**Interviewer:**  
What happens if a user hasn't been active in 30 days, their timeline cache expired, and they come back?

**Candidate:**  
Good edge case. If the cache is cold (miss), we fall back to **fanout-on-read**:

1. Fetch the user's following list from the Social Graph Service.
2. For each followed user, fetch their recent tweets from the Tweet Store (last N tweets, or tweets from the last 7 days).
3. Merge, sort, and return the timeline.
4. **Simultaneously**, backfill the cache: write this computed timeline into Redis so subsequent requests are fast.

This is essentially a **cache-aside with lazy backfill** pattern. The first request after a long absence may be slower (~500ms-1s instead of < 200ms), but subsequent requests are fast.

**Interviewer:**  
Good. Let's move on to the data stores.

---

## PHASE 5: Data Store Design (~10 min)

**Interviewer:**  
You mentioned a "Tweet Store" and a "Social Graph Store." What databases would you use and why?

**Candidate:**  
Let me break it down by data type:

### 1. Tweet Store

**Choice: Sharded MySQL (or PostgreSQL) + Read Replicas**

Why relational?
- Tweets are relatively structured: `tweet_id, user_id, text, created_at, media_urls, like_count, retweet_count`.
- We need strong durability guarantees — a tweet must not be lost once acknowledged.
- Relational DBs give us ACID for individual tweet writes.

**Schema:**
```sql
CREATE TABLE tweets (
    tweet_id    BIGINT PRIMARY KEY,   -- Snowflake ID (encodes timestamp)
    user_id     BIGINT NOT NULL,
    content     VARCHAR(280) NOT NULL,
    media_urls  JSON,                 -- Array of S3 URLs
    created_at  TIMESTAMP NOT NULL,
    like_count  INT DEFAULT 0,
    retweet_count INT DEFAULT 0,
    reply_to_id BIGINT,              -- NULL if not a reply
    is_deleted  BOOLEAN DEFAULT FALSE,
    INDEX idx_user_created (user_id, created_at DESC)  -- For user timeline
);
```

**Sharding strategy:** Shard by `tweet_id` (range-based on the embedded timestamp prefix from Snowflake IDs). This ensures recent tweets (hot data) are co-located and range scans are efficient.

Alternatively, shard by `user_id` — this co-locates all of a user's tweets on one shard, making user timeline queries single-shard. Trade-off: hot users may create hot shards. I'd lean toward `tweet_id`-based sharding with a secondary index approach for user timelines, or use `user_id` sharding with consistent hashing to spread load.

**Interviewer:**  
You said shard by tweet_id but your user timeline query needs `WHERE user_id = ? ORDER BY created_at DESC`. If tweets are sharded by tweet_id, that query is a scatter-gather across all shards. How do you handle that?

**Candidate:**  
Excellent point. This is a real tension. There are a few approaches:

**Option A: Shard by user_id.** This makes user timeline queries single-shard, but tweet-by-id lookups (needed for the home timeline hydration) become scatter-gather. Since tweet-by-id lookups are point queries, we can mitigate with a **distributed cache** (Memcached/Redis) in front of the tweet store — cache hit rate would be very high because popular tweets are read millions of times.

**Option B: Shard by tweet_id, maintain a secondary index.** We keep a separate table or index that maps `user_id → [tweet_ids]`. This is essentially a denormalized secondary index. Could be stored in a separate lightweight store (even a simple key-value store like DynamoDB).

**Option C: Dual-write.** Write the tweet to a tweet-id-sharded table (for point lookups) AND to a user-id-sharded table (for user timeline). Consistency is managed via the event queue — both writes happen in response to the `TweetCreated` event.

**My recommendation: Option A — shard by user_id**, with an aggressive tweet cache layer. Here's why:
- User timelines are a core use case — keeping them single-shard is high value.
- Tweet-by-id lookups (for home timeline hydration) are extremely cache-friendly because popular tweets are read many times. A cache hit rate of 99%+ is realistic.
- Simpler operational model — no dual-write consistency headaches.

**Interviewer:**  
That's a well-reasoned trade-off. I agree with option A. What about the social graph?

### 2. Social Graph Store

**Candidate:**  
The social graph stores follow relationships: `(follower_id, followee_id)`.

**Choice: Wide-column store like Apache Cassandra (or DynamoDB)**

Why NoSQL here?
- The access patterns are simple but high-volume:
  - `GET followers of user X` (for fanout) — could return millions of rows for celebrities
  - `GET users that X follows` (for timeline construction and "following" list)
  - `PUT follow(X, Y)` / `DELETE unfollow(X, Y)`
- No complex joins needed.
- Cassandra handles wide partitions well and scales horizontally with ease.
- We need high write throughput for follow/unfollow operations.

**Data model in Cassandra:**

```
Table: user_followers
Partition Key: followee_id
Clustering Key: follower_id
Columns: followed_at (timestamp)

Table: user_following  
Partition Key: follower_id
Clustering Key: followee_id
Columns: followed_at (timestamp)
```

Two tables for the two access patterns — this is a standard denormalization pattern in Cassandra.

For the `user_followers` table, a celebrity's partition could have millions of rows. We might need to **bucket** the partition (e.g., `followee_id:bucket_number`) to avoid Cassandra's wide partition performance degradation. Bucket assignment could be hash-based: `bucket = hash(follower_id) % num_buckets`.

**Interviewer:**  
You mentioned the follower count for the celebrity threshold. Where does that live?

**Candidate:**  
The follower count is maintained as a **counter** in the User Service's data store (could be a column in the user profile table in MySQL, or a Redis counter for fast access). It's updated atomically on follow/unfollow events:

- `INCR user:{user_id}:follower_count` on follow
- `DECR user:{user_id}:follower_count` on unfollow

We cache this in Redis for fast access during fanout decisions. Slight inaccuracy is tolerable — if the count is off by a few dozen, the fanout decision doesn't materially change.

---

## PHASE 6: ID Generation (~3 min)

**Interviewer:**  
You mentioned Snowflake IDs earlier. Tell me more about your ID generation strategy.

**Candidate:**  
We need tweet IDs to be:
- **Globally unique** across all shards.
- **Roughly time-ordered** (so we can sort by ID and get chronological order — avoids needing a separate timestamp index in some cases).
- **64-bit** (fits in a BIGINT column, efficient for storage and indexing).

I'd use a **Snowflake-like scheme** (which Twitter actually invented):

```
| 1 bit (unused) | 41 bits (timestamp ms) | 10 bits (machine/shard ID) | 12 bits (sequence) |
```

- **41 bits of timestamp** = ~69 years of unique milliseconds.
- **10 bits of machine ID** = 1,024 unique generator instances.
- **12 bits of sequence** = 4,096 IDs per millisecond per machine.
- Total capacity: **4M IDs/sec per machine**, practically unlimited with 1,024 machines.

Each Tweet Service instance runs its own Snowflake generator with a unique machine ID (assigned via ZooKeeper or a coordination service). IDs are generated locally with no network round-trip — very fast.

**Interviewer:**  
What's the failure mode if two machines get the same machine ID?

**Candidate:**  
That would cause ID collisions — catastrophic. The coordination service (ZooKeeper) must guarantee unique machine ID leases. If ZooKeeper is unavailable, a machine should **refuse to generate IDs** rather than risk collisions. We could also add a health check: on startup, generate a few test IDs and verify uniqueness against a lightweight ID registry. Defense in depth.

---

## PHASE 7: Reliability, Fault Tolerance & Operational Concerns (~8 min)

**Interviewer:**  
Let's talk reliability. What happens when the Fanout Service goes down? What happens if Redis goes down?

**Candidate:**  

### Fanout Service Failure

The Fanout Service consumes from a **durable message queue** (Kafka or SQS). If the service crashes:
- Messages remain in the queue (Kafka retains them for a configurable retention period; SQS has visibility timeout + dead-letter queues).
- When the service restarts (or other instances in the consumer group pick up), it resumes processing from the last committed offset.
- **No tweets are lost.** Timelines may be temporarily stale (followers don't see new tweets until fanout completes), but the system self-heals.

I'd run the Fanout Service as a **horizontally scaled consumer group** — many instances consuming from partitioned Kafka topics. If one instance dies, Kafka rebalances the partitions to surviving instances.

### Redis (Timeline Cache) Failure

Redis is a cache, not the source of truth. If a Redis node fails:

1. **Redis Cluster** automatically fails over to a replica (if using Redis Cluster with replicas — which we should).
2. On a full cache miss (cold start), we fall back to fanout-on-read as discussed earlier.
3. We should have **monitoring and alerting** on cache hit rates. If hit rate drops below, say, 95%, we know something is wrong.

For durability: Redis is ephemeral. We can enable Redis AOF (append-only file) for persistence, but I'd treat Redis as a cache and accept cache misses. The source of truth is always the Tweet Store.

**Interviewer:**  
What about rate limiting and abuse prevention? A malicious user could try to post thousands of tweets per second.

**Candidate:**  
Rate limiting is applied at the **API Gateway** layer:

1. **Per-user rate limits:** e.g., max 300 tweets/3 hours (Twitter's actual limit), max 1,000 follows/day.
2. **Implementation:** Token bucket or sliding window counter, backed by Redis.
   ```
   Key: ratelimit:tweet:{user_id}
   Value: count of tweets in the current window
   TTL: window duration (e.g., 3 hours)
   ```
3. **IP-based rate limiting** for unauthenticated endpoints (login, signup).
4. **Distributed rate limiting:** Since we have multiple API Gateway instances, the rate limit state must be shared — hence Redis.

Additionally:
- **Spam detection:** ML-based classifier that flags suspicious tweet patterns (identical content posted rapidly, URL spam, etc.). This runs asynchronously — tweets are posted but can be retroactively hidden.
- **Circuit breakers** on downstream service calls to prevent cascade failures.

**Interviewer:**  
Let's say we're running in multiple AWS regions. How do you handle multi-region?

**Candidate:**  
For a global service like Twitter:

1. **Users are routed to the nearest region** via DNS-based routing (Route 53 latency-based routing) or anycast.
2. **Tweet Store:** Each region has a full replica. Writes go to the primary region (or the user's "home" region) and are asynchronously replicated to other regions.
   - For MySQL: async replication or a multi-master setup with conflict resolution (CRDTs for counters like like_count).
   - Or use a globally distributed DB like CockroachDB or Amazon Aurora Global Database.
3. **Timeline Cache (Redis):** Each region has its own Redis cluster. The Fanout Service in each region processes events from the global Kafka stream and populates local caches.
4. **Social Graph:** Cassandra natively supports multi-datacenter replication — we'd set replication factor 3 per datacenter.

**Cross-region follows:** If a user in US-East follows a user in EU-West, the follow event propagates via Kafka to both regions' social graph stores. Eventual consistency is fine — a 1-2 second delay in cross-region propagation is acceptable.

---

## PHASE 8: Quick Touch on Search (if time allows) (~3 min)

**Interviewer:**  
We have a few minutes. You mentioned search earlier — give me the 30-second version.

**Candidate:**  
For tweet search, I'd use an **inverted index** — essentially Elasticsearch (or OpenSearch):

1. When a tweet is created, the `TweetCreated` event is also consumed by a **Search Indexer** service.
2. The indexer tokenizes the tweet text, extracts hashtags, and writes to an **Elasticsearch cluster**.
3. Search queries hit the Elasticsearch cluster directly (or via a Search Service that handles query parsing, ranking, and pagination).

**Index structure:**
- Each tweet is a document with fields: `tweet_id, user_id, text, hashtags, created_at, engagement_score`.
- Full-text search on `text`, filtering by `created_at` range, boosted by `engagement_score`.

**Scaling:** Elasticsearch shards across multiple nodes. Hot tweets (recent) are on faster storage; older tweets can be moved to warm/cold tiers.

For **trending topics:** A separate streaming pipeline (Kafka Streams or Flink) processes the tweet stream in real-time, counts hashtag frequency in sliding windows (e.g., 1-hour, 24-hour), and surfaces the top-N trending topics. Results are cached and refreshed every few minutes.

---

## PHASE 9: Wrap-Up & Amazon Leadership Principles (~4 min)

**Interviewer:**  
We're coming up on time. Let me ask you something more open-ended. In this design, what's the piece you'd be most worried about operating in production? Where would you invest the most engineering effort?

**Candidate:**  
The **Fanout Service** is the piece I'd lose sleep over. Here's why:

1. **It's the critical path between write and read.** If it falls behind, users see stale feeds — which directly impacts user experience and engagement.
2. **The load is bursty and unpredictable.** A single viral tweet from a celebrity-adjacent account (say, 500K followers — below the 10K celebrity threshold... wait, actually we'd want a lower threshold or dynamic adjustment) can cause a sudden spike.
3. **Operational complexity:** It needs to scale horizontally, handle backpressure gracefully, and have robust monitoring (queue depth, processing lag, error rates).

I'd invest in:
- **Comprehensive observability:** Real-time dashboards showing fanout lag per partition, queue depth, p99 processing time.
- **Auto-scaling:** Consumer group automatically scales based on queue depth.
- **Backpressure mechanisms:** If the queue grows beyond a threshold, temporarily switch more users to fanout-on-read mode (raise the celebrity threshold dynamically).
- **Chaos engineering:** Regularly inject failures to validate resilience.

**Interviewer:**  
That's a mature operational perspective. One last thing — tell me about a time you had to make a technical decision where you had to disagree with your team but the decision turned out to be the right call.

**Candidate:**  
*(This transitions into behavioral/Leadership Principle territory — "Have Backbone; Disagree and Commit")*

Sure. At my previous company, we were building a real-time notification system and the team wanted to use a polling-based approach for simplicity. I pushed hard for WebSockets + a pub-sub backbone, even though it was more complex upfront. The team was skeptical about operational overhead. I built a prototype over a weekend, showed the latency improvement (from 30-second polling to sub-second delivery), and more importantly, showed the reduction in server load (90% fewer HTTP requests). The team got on board, and we shipped it. Six months later, it was handling 10x the traffic we originally scoped without any scaling issues. The upfront investment paid off.

**Interviewer:**  
Good. I appreciate the specificity. That's a strong answer.

Alright, that's time. Any questions for me about the team or the role?

---

## Summary: Key Design Decisions

| Decision | Choice | Rationale |
|---|---|---|
| Fanout strategy | Hybrid (push for regular users, pull for celebrities) | Balances write amplification vs. read latency |
| Timeline cache | Redis Sorted Sets | Sub-ms reads, natural ordering by timestamp |
| Tweet storage | Sharded MySQL (by user_id) | Strong durability, efficient user timeline queries |
| Social graph | Cassandra (denormalized tables) | Handles wide partitions (millions of followers), high write throughput |
| ID generation | Snowflake IDs | Time-ordered, globally unique, 64-bit, no coordination needed per-request |
| Async processing | Kafka | Durable, partitioned, supports consumer groups for horizontal scaling |
| Media storage | S3 + CDN | Cost-effective, globally distributed |
| Search | Elasticsearch | Purpose-built for full-text search with ranking |
| Multi-region | Active-active with async replication | Low latency for global users, eventual consistency acceptable |

---

## Interviewer's Internal Assessment Notes

**Hire Recommendation: Strong Hire (L6)**

| Dimension | Rating | Notes |
|---|---|---|
| **Problem Decomposition** | Exceeds Bar | Clearly scoped the problem, identified the right sub-problems to focus on. Didn't boil the ocean. |
| **Scale & Estimation** | Meets Bar | Solid back-of-envelope math. Understood read-write ratio implications. |
| **Trade-off Analysis** | Exceeds Bar | Fanout hybrid approach was well-reasoned. Sharding discussion showed depth. The hysteresis idea for celebrity threshold was a nice touch. |
| **System Breadth** | Meets Bar | Covered all major components. Would have liked more on monitoring/observability. |
| **Operational Maturity** | Exceeds Bar | Failure modes, cache miss handling, rate limiting, multi-region — all discussed with practical depth. "What keeps you up at night" answer was strong. |
| **Communication** | Exceeds Bar | Structured, clear, checked in frequently. Good use of diagrams and tables. |
| **LP: Dive Deep** | Exceeds Bar | Voluntarily went deep on fanout, caching, and sharding without being pushed. |
| **LP: Have Backbone** | Meets Bar | Behavioral answer was specific and demonstrated conviction. |

**Areas for growth:** Could strengthen distributed transactions / consistency model discussions. Didn't discuss exactly-once semantics in the fanout pipeline (at-least-once with idempotent writes would be the answer).

---

*End of interview simulation.*