# Twitter System Design — Datastore Design

> Continuation of the interview simulation. The interviewer asked: "How is the datastore going to look like for storing and maintaining these data structures?"

---

## Overview: Data Domains & Storage Choices

| Data Domain | Storage Engine | Why This Choice |
|---|---|---|
| **Tweets** | Sharded MySQL (by `user_id`) | Structured data, strong durability (ACID), efficient user timeline queries via single-shard access |
| **User Profiles** | MySQL (same cluster, different table) | Structured, low-volume, relational (joins with tweets) |
| **Social Graph** (follows) | Apache Cassandra | Simple access patterns, massive scale (billions of edges), high write throughput, native multi-DC replication |
| **Home Timeline Cache** | Redis Cluster (Sorted Sets) | Pre-computed feeds, sub-ms reads, natural time-ordering |
| **Tweet Cache** | Redis / Memcached | Hot tweet hydration, high cache-hit rate for popular tweets |
| **User Cache** | Redis / Memcached | Frequently accessed user profiles for tweet hydration |
| **Media** | Amazon S3 + CloudFront CDN | Object storage for images/videos, globally distributed |
| **Counters** (likes, retweets, follower counts) | Redis (atomic counters) + async MySQL writeback | Fast increments, eventual persistence |
| **Search Index** | Elasticsearch / OpenSearch | Full-text inverted index for tweet search |
| **ID Generation** | Snowflake (in-process) | Globally unique, time-ordered, no DB round-trip |
| **Rate Limiting** | Redis (sliding window counters) | Distributed, fast, TTL-based expiry |
| **Idempotency Keys** | Redis (with 24h TTL) | Deduplication for tweet creation retries |

---

## 1. Tweet Store (MySQL — Sharded by `user_id`)

### Schema

```sql
-- ============================================================
-- TWEET TABLE
-- Sharded by: user_id (consistent hashing across N shards)
-- Each shard is a MySQL instance with 1 primary + 2 read replicas
-- ============================================================

CREATE TABLE tweets (
    tweet_id        BIGINT          NOT NULL,       -- Snowflake ID (embeds timestamp)
    user_id         BIGINT          NOT NULL,       -- Author. Also the shard key.
    content         VARCHAR(280)    NOT NULL DEFAULT '',
    media_urls      JSON            DEFAULT NULL,   -- ["https://media.twitter.com/img/abc.jpg"]
    reply_to_id     BIGINT          DEFAULT NULL,   -- FK to parent tweet (NULL if not a reply)
    quote_tweet_id  BIGINT          DEFAULT NULL,   -- FK to quoted tweet (NULL if not a quote)
    like_count      INT UNSIGNED    NOT NULL DEFAULT 0,
    retweet_count   INT UNSIGNED    NOT NULL DEFAULT 0,
    reply_count     INT UNSIGNED    NOT NULL DEFAULT 0,
    is_deleted      BOOLEAN         NOT NULL DEFAULT FALSE,
    created_at      TIMESTAMP(3)    NOT NULL,       -- Millisecond precision
    updated_at      TIMESTAMP(3)    NOT NULL DEFAULT CURRENT_TIMESTAMP(3) ON UPDATE CURRENT_TIMESTAMP(3),

    PRIMARY KEY (user_id, tweet_id),                -- Composite PK: shard key first, then tweet_id
    INDEX idx_tweet_id (tweet_id),                  -- For point lookups by tweet_id (within shard)
    INDEX idx_user_timeline (user_id, created_at DESC, is_deleted)  -- User timeline query
) ENGINE=InnoDB
  DEFAULT CHARSET=utf8mb4                           -- Full Unicode support (emojis)
  ROW_FORMAT=COMPRESSED;                            -- ~40% storage savings on text-heavy rows
```

### Why this Primary Key structure?

```
PRIMARY KEY (user_id, tweet_id)
```

- **`user_id` first**: In InnoDB, data is physically clustered by PK. Putting `user_id` first means all tweets by the same user are stored contiguously on disk. A user timeline query (`WHERE user_id = ? ORDER BY created_at DESC`) is a sequential scan — very fast.
- **`tweet_id` second**: Since tweet_id is a Snowflake ID (time-ordered), tweets within a user's partition are naturally in chronological order. This means `ORDER BY tweet_id DESC` ≈ `ORDER BY created_at DESC`.

### Sharding Strategy

```
                    ┌─────────────────────────────────────────┐
                    │           Shard Router / Proxy           │
                    │   shard = consistent_hash(user_id) % N   │
                    └────────┬──────────┬──────────┬──────────┘
                             │          │          │
                      ┌──────▼───┐ ┌────▼─────┐ ┌──▼────────┐
                      │ Shard 0  │ │ Shard 1  │ │ Shard N-1 │
                      │ Primary  │ │ Primary  │ │ Primary   │
                      │ + 2 Read │ │ + 2 Read │ │ + 2 Read  │
                      │ Replicas │ │ Replicas │ │ Replicas  │
                      └──────────┘ └──────────┘ └───────────┘
```

- **Number of shards**: Start with 64 shards. With ~500M tweets/day × 1KB = 500GB/day, each shard handles ~8GB/day. Plenty of headroom.
- **Consistent hashing**: Use a hash ring with virtual nodes to distribute `user_id` values across shards. Adding/removing shards only redistributes a fraction of users.
- **Rebalancing**: When a shard gets hot (a power user with millions of tweets), we can split the shard. Virtual nodes make this smoother — reassign some vnodes to a new physical shard.

### Key Queries & How They Execute

| Query | SQL | Shard Access | Performance |
|-------|-----|-------------|-------------|
| **User timeline** | `SELECT * FROM tweets WHERE user_id = ? AND is_deleted = 0 ORDER BY created_at DESC LIMIT 20` | Single shard (user_id is shard key) | Index scan on `idx_user_timeline`. ~1-2ms. |
| **Get tweet by ID** | `SELECT * FROM tweets WHERE tweet_id = ?` | Need to know which shard → use Tweet Cache first (99%+ hit rate). On cache miss, either: (a) derive `user_id` from a lightweight `tweet_id → user_id` mapping in Redis, or (b) scatter-gather across shards (expensive, rare). | Cache: <1ms. DB fallback: ~5-10ms. |
| **Post tweet** | `INSERT INTO tweets (tweet_id, user_id, content, ...) VALUES (?, ?, ?, ...)` | Single shard | ~2-5ms with sync replication to 1 replica. |
| **Delete tweet** | `UPDATE tweets SET is_deleted = 1 WHERE user_id = ? AND tweet_id = ?` | Single shard | Soft delete, ~2ms. |

### tweet_id → user_id Mapping (for cross-shard lookups)

Since we shard by `user_id` but sometimes need to look up a tweet by `tweet_id` alone (e.g., when hydrating the home timeline), we maintain a lightweight mapping:

```
Redis Key:    tweet_owner:{tweet_id}
Value:        user_id
TTL:          30 days (or never expire for recent tweets)
```

This mapping is populated on tweet creation and allows us to route a `GET /tweets/{tweet_id}` to the correct shard without scatter-gather.

---

## 2. User Profile Store (MySQL — Same Cluster as Tweets)

### Schema

```sql
CREATE TABLE users (
    user_id         BIGINT          NOT NULL AUTO_INCREMENT,
    username        VARCHAR(30)     NOT NULL,           -- Unique handle (@johndoe)
    display_name    VARCHAR(50)     NOT NULL,
    email           VARCHAR(255)    NOT NULL,
    password_hash   VARCHAR(255)    NOT NULL,           -- bcrypt hash
    bio             VARCHAR(160)    DEFAULT NULL,
    avatar_url      VARCHAR(512)    DEFAULT NULL,
    header_url      VARCHAR(512)    DEFAULT NULL,
    is_verified     BOOLEAN         NOT NULL DEFAULT FALSE,
    is_celebrity    BOOLEAN         NOT NULL DEFAULT FALSE,  -- For fanout threshold
    follower_count  INT UNSIGNED    NOT NULL DEFAULT 0,
    following_count INT UNSIGNED    NOT NULL DEFAULT 0,
    tweet_count     INT UNSIGNED    NOT NULL DEFAULT 0,
    created_at      TIMESTAMP       NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at      TIMESTAMP       NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,

    PRIMARY KEY (user_id),
    UNIQUE INDEX idx_username (username),
    UNIQUE INDEX idx_email (email)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
```

### Notes on User Table

- **Not sharded** (or minimally sharded). With 500M users × ~500 bytes = ~250GB — fits on a single beefy MySQL instance with read replicas.
- If sharding is needed later, shard by `user_id` (same ring as tweets).
- **`is_celebrity` flag**: Set to `true` when `follower_count` crosses the fanout threshold (e.g., 10K). Checked by the Fanout Service to decide push vs. pull.
- **Counters** (`follower_count`, `following_count`, `tweet_count`): Updated via Redis atomic counters for speed, then periodically flushed to MySQL (every 30-60 seconds) for persistence. See Counter Design section below.

---

## 3. Social Graph Store (Cassandra)

### Why Cassandra?

- **Scale**: Billions of follow edges. A single celebrity can have 50M+ followers.
- **Access patterns**: Simple key-value/wide-column lookups — no joins.
- **Write throughput**: Follows/unfollows are write-heavy during viral moments.
- **Multi-DC**: Native multi-datacenter replication.

### Data Model

We need two access patterns, so we maintain **two denormalized tables** (standard Cassandra practice):

```cql
-- ============================================================
-- TABLE 1: "Who follows user X?" (used by Fanout Service)
-- Partition Key: followee_id
-- Clustering Key: follower_id
-- ============================================================

CREATE TABLE user_followers (
    followee_id     BIGINT,
    follower_id     BIGINT,
    followed_at     TIMESTAMP,
    PRIMARY KEY (followee_id, follower_id)
) WITH CLUSTERING ORDER BY (follower_id ASC)
  AND compaction = {'class': 'LeveledCompactionStrategy'}
  AND gc_grace_seconds = 864000;

-- Query: Get all followers of user X
-- SELECT follower_id, followed_at FROM user_followers WHERE followee_id = ?;

-- Query: Check if user A follows user B
-- SELECT follower_id FROM user_followers WHERE followee_id = ? AND follower_id = ?;
```

```cql
-- ============================================================
-- TABLE 2: "Who does user X follow?" (used by Timeline Service)
-- Partition Key: follower_id
-- Clustering Key: followee_id
-- ============================================================

CREATE TABLE user_following (
    follower_id     BIGINT,
    followee_id     BIGINT,
    followed_at     TIMESTAMP,
    PRIMARY KEY (follower_id, followee_id)
) WITH CLUSTERING ORDER BY (followee_id ASC)
  AND compaction = {'class': 'LeveledCompactionStrategy'}
  AND gc_grace_seconds = 864000;

-- Query: Get all users that X follows
-- SELECT followee_id, followed_at FROM user_following WHERE follower_id = ?;
```

### Handling Celebrity Wide Partitions

A celebrity with 50M followers means the `user_followers` partition for that `followee_id` has 50M rows. Cassandra partitions over ~100MB start degrading. At ~50 bytes per row × 50M = 2.5GB — that's way too large.

**Solution: Partition bucketing**

```cql
CREATE TABLE user_followers_bucketed (
    followee_id     BIGINT,
    bucket          INT,            -- 0 to N-1 (e.g., 256 buckets)
    follower_id     BIGINT,
    followed_at     TIMESTAMP,
    PRIMARY KEY ((followee_id, bucket), follower_id)
) WITH CLUSTERING ORDER BY (follower_id ASC);
```

```
Bucket assignment: bucket = hash(follower_id) % NUM_BUCKETS

Write path (follow):
  1. Compute bucket = hash(follower_id) % 256
  2. INSERT INTO user_followers_bucketed (followee_id, bucket, follower_id, followed_at)
     VALUES (?, ?, ?, ?);

Read path (get all followers — for fanout):
  1. Fan out 256 parallel queries: one per bucket
  2. SELECT follower_id FROM user_followers_bucketed
     WHERE followee_id = ? AND bucket = ?;
  3. Merge results
```

- Each bucket holds 50M / 256 ≈ ~195K rows ≈ ~10MB per partition — well within Cassandra's sweet spot.
- The 256 parallel queries can be issued concurrently — Cassandra handles this well with its token-aware driver.
- For non-celebrities (< 10K followers), a single bucket (bucket = 0) suffices. We can dynamically expand buckets when a user crosses a threshold.

### Write Operations

```
Follow(A, B):
  1. INSERT INTO user_followers (followee_id=B, follower_id=A, followed_at=now())
  2. INSERT INTO user_following (follower_id=A, followee_id=B, followed_at=now())
  3. INCR follower_count of B (in Redis)
  4. INCR following_count of A (in Redis)
  -- All done atomically via a Kafka event handler (at-least-once with idempotent writes)

Unfollow(A, B):
  1. DELETE FROM user_followers WHERE followee_id=B AND follower_id=A
  2. DELETE FROM user_following WHERE follower_id=A AND followee_id=B
  3. DECR follower_count of B
  4. DECR following_count of A
```

### Replication & Consistency

```yaml
Keyspace replication:
  strategy: NetworkTopologyStrategy
  us-east-1: 3
  eu-west-1: 3
  ap-southeast-1: 3

Read consistency:  LOCAL_QUORUM  (2 of 3 replicas in local DC)
Write consistency: LOCAL_QUORUM  (2 of 3 replicas in local DC)
```

- LOCAL_QUORUM gives strong consistency within a region and eventual consistency across regions.
- Cross-region replication is async — a follow in US-East propagates to EU-West within 100-500ms.

---

## 4. Home Timeline Cache (Redis Cluster)

### Data Structure

```
┌─────────────────────────────────────────────────┐
│  Key: timeline:{user_id}                         │
│  Type: Sorted Set (ZSET)                         │
│                                                  │
│  Members:  tweet_id (string)                     │
│  Scores:   created_at (Unix epoch ms)            │
│                                                  │
│  Example:                                        │
│  ┌────────────────────┬────────────────────────┐ │
│  │ Score (timestamp)  │ Member (tweet_id)      │ │
│  ├────────────────────┼────────────────────────┤ │
│  │ 1717368000000      │ tw_7291038475610       │ │
│  │ 1717367700000      │ tw_7291038475590       │ │
│  │ 1717367400000      │ tw_7291038475550       │ │
│  │ ...                │ ...                    │ │
│  │ (max 800 entries)  │                        │ │
│  └────────────────────┴────────────────────────┘ │
└─────────────────────────────────────────────────┘
```

### Operations

```redis
# Fanout: Push tweet to a follower's timeline
ZADD timeline:{user_id} {timestamp_ms} {tweet_id}
# Keep timeline capped at 800 entries
ZREMRANGEBYRANK timeline:{user_id} 0 -801

# Read timeline: Get 50 most recent tweet_ids
ZREVRANGEBYSCORE timeline:{user_id} +inf -inf LIMIT 0 50

# Pagination: Get next 50 after a cursor (cursor = score of last seen tweet)
ZREVRANGEBYSCORE timeline:{user_id} ({cursor_score} -inf LIMIT 0 50

# Count new tweets since last seen
ZCOUNT timeline:{user_id} ({last_seen_score} +inf

# Delete a tweet from timeline (on tweet deletion)
ZREM timeline:{user_id} {tweet_id}

# TTL: Expire inactive timelines after 7 days
EXPIRE timeline:{user_id} 604800
```

### Memory Sizing

```
Per entry:  tweet_id (8 bytes) + score (8 bytes) + Redis ZSET overhead ≈ 50 bytes
Per user:   800 entries × 50 bytes = 40 KB
Active users: 200M DAU (but only ~50M have warm caches at any time)
Total: 50M × 40 KB = 2 TB

Redis Cluster: 20 nodes × 128 GB RAM = 2.56 TB (with headroom)
Replication factor: 1 replica per shard → 40 nodes total
```

### Cluster Topology

```
┌──────────────────────────────────────────────────┐
│                  Redis Cluster                    │
│                                                   │
│  ┌──────────┐  ┌──────────┐       ┌──────────┐  │
│  │ Shard 0  │  │ Shard 1  │  ...  │ Shard 19 │  │
│  │ Primary  │  │ Primary  │       │ Primary  │  │
│  │ Slot 0-  │  │ Slot 819-│       │ Slot     │  │
│  │ 818      │  │ 1637     │       │ 15565-   │  │
│  │          │  │          │       │ 16383    │  │
│  │ Replica  │  │ Replica  │       │ Replica  │  │
│  └──────────┘  └──────────┘       └──────────┘  │
│                                                   │
│  Key routing: SLOT = CRC16(timeline:{user_id})    │
│               % 16384                             │
└──────────────────────────────────────────────────┘
```

---

## 5. Tweet Cache (Redis / Memcached)

### Purpose
Hydrate `tweet_id` → full tweet object for home timeline rendering. Avoids hitting MySQL for every tweet.

### Data Structure

```
Key:    tweet:{tweet_id}
Value:  JSON-serialized tweet object
TTL:    24 hours (hot tweets refreshed on access)

Example:
SET tweet:tw_7291038475610 '{
  "tweet_id": "tw_7291038475610",
  "user_id": "usr_4820193847",
  "content": "Hello world!",
  "media_urls": [],
  "created_at": "2026-06-02T22:05:30.000Z",
  "like_count": 1542,
  "retweet_count": 312,
  "reply_count": 87
}' EX 86400
```

### Batch Hydration Pattern

When the Timeline Service has 50 `tweet_ids` from the ZSET, it does:

```
MGET tweet:tw_001 tweet:tw_002 tweet:tw_003 ... tweet:tw_050
```

- Cache hits: return immediately (~0.5ms for 50 keys).
- Cache misses: batch query MySQL by `tweet_id` (using the `tweet_owner` mapping to route to correct shard), then populate cache.
- Expected hit rate: **99%+** (popular tweets are read millions of times).

### tweet_id → user_id Mapping (for shard routing)

```
Key:    tweet_owner:{tweet_id}
Value:  user_id
TTL:    30 days

Example:
SET tweet_owner:tw_7291038475610 "usr_4820193847" EX 2592000
```

Populated on tweet creation. Used to route `GET tweet by ID` to the correct MySQL shard.

---

## 6. User Cache (Redis)

### Data Structure

```
Key:    user:{user_id}
Value:  JSON-serialized user profile (subset of fields needed for tweet hydration)
TTL:    1 hour

Example:
SET user:usr_4820193847 '{
  "user_id": "usr_4820193847",
  "username": "johndoe",
  "display_name": "John Doe",
  "avatar_url": "https://media.twitter.com/avatar/4820193847.jpg",
  "is_verified": true,
  "is_celebrity": false
}' EX 3600
```

### Celebrity Following List (per user)

For the hybrid fanout read path, the Timeline Service needs to know which celebrities a user follows:

```
Key:    celebrity_following:{user_id}
Type:   Set (SMEMBERS)
Members: celebrity user_ids

Example:
SADD celebrity_following:usr_4820193847 "usr_5555555555" "usr_6666666666"

# On timeline read:
SMEMBERS celebrity_following:usr_4820193847
→ ["usr_5555555555", "usr_6666666666"]
→ Fetch latest tweets from these users directly from Tweet Store
```

Updated on follow/unfollow: if the target user is a celebrity (`is_celebrity = true`), add/remove from this set.

---

## 7. Counters (Likes, Retweets, Follower Counts)

### Problem
Updating `like_count` directly in MySQL on every like would create massive write contention on hot tweets (a viral tweet getting 100K likes/sec).

### Solution: Redis Counters + Async Writeback

```
┌─────────┐     ┌──────────────┐     ┌───────────────────┐
│  Like   │────▶│ Redis Counter │────▶│ Async Writeback   │
│  Event  │     │ INCR tweet:   │     │ Service           │
│         │     │ {id}:likes    │     │ (batched, every   │
│         │     │               │     │  30-60 seconds)   │
└─────────┘     └──────────────┘     └─────────┬─────────┘
                                               │
                                        ┌──────▼──────┐
                                        │   MySQL     │
                                        │ UPDATE tweets│
                                        │ SET like_   │
                                        │ count = ?   │
                                        └─────────────┘
```

### Redis Counter Keys

```redis
# Tweet engagement counters
INCR tweet:{tweet_id}:like_count        # On like
DECR tweet:{tweet_id}:like_count        # On unlike
INCR tweet:{tweet_id}:retweet_count     # On retweet
INCR tweet:{tweet_id}:reply_count       # On reply

# User counters
INCR user:{user_id}:follower_count      # On follow
DECR user:{user_id}:follower_count      # On unfollow
INCR user:{user_id}:following_count     # On follow
DECR user:{user_id}:following_count     # On unfollow
INCR user:{user_id}:tweet_count         # On tweet
DECR user:{user_id}:tweet_count         # On delete
```

### Writeback Service

- Runs every 30-60 seconds.
- Scans Redis for dirty counters (using a Redis Set of modified keys, or a Kafka stream of counter events).
- Batch updates MySQL: `UPDATE tweets SET like_count = ? WHERE tweet_id = ?`
- After successful writeback, the Redis counter remains as the primary read source (faster than MySQL).
- On Redis failure: fall back to MySQL values. On recovery: re-seed Redis from MySQL.

### Like/Unlike — Tracking "Has user X liked tweet Y?"

```
Key:    user_likes:{user_id}
Type:   Set (SADD / SISMEMBER)
Members: tweet_id

# Like:
SADD user_likes:usr_4820193847 tw_7291038475610

# Check if liked (for is_liked_by_me):
SISMEMBER user_likes:usr_4820193847 tw_7291038475610 → 1 (true)

# Unlike:
SREM user_likes:usr_4820193847 tw_7291038475610
```

For persistence, likes are also written to a MySQL table:

```sql
CREATE TABLE likes (
    user_id     BIGINT NOT NULL,
    tweet_id    BIGINT NOT NULL,
    created_at  TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (user_id, tweet_id),
    INDEX idx_tweet_likes (tweet_id, user_id)  -- For "who liked this tweet" queries
) ENGINE=InnoDB;
```

---

## 8. Media Store (S3)

### Object Key Structure

```
s3://twitter-media-{region}/
  ├── images/
  │   └── {year}/{month}/{day}/{media_id}.{ext}
  │       e.g., images/2026/06/02/med_8a7f3b2c.jpg
  ├── videos/
  │   └── {year}/{month}/{day}/{media_id}/
  │       ├── original.mp4
  │       ├── 720p.mp4
  │       └── 360p.mp4
  └── avatars/
      └── {user_id}.jpg
```

### Media Metadata Table (MySQL)

```sql
CREATE TABLE media (
    media_id        VARCHAR(32)     NOT NULL,
    user_id         BIGINT          NOT NULL,       -- Uploader
    media_type      ENUM('image', 'video', 'gif') NOT NULL,
    s3_key          VARCHAR(512)    NOT NULL,
    content_type    VARCHAR(50)     NOT NULL,       -- image/jpeg, video/mp4
    file_size_bytes BIGINT          NOT NULL,
    width           INT             DEFAULT NULL,
    height          INT             DEFAULT NULL,
    duration_ms     INT             DEFAULT NULL,   -- For videos
    status          ENUM('pending', 'processing', 'ready', 'failed') NOT NULL DEFAULT 'pending',
    cdn_url         VARCHAR(512)    DEFAULT NULL,   -- CloudFront URL once ready
    created_at      TIMESTAMP       NOT NULL DEFAULT CURRENT_TIMESTAMP,

    PRIMARY KEY (media_id),
    INDEX idx_user_media (user_id, created_at DESC)
) ENGINE=InnoDB;
```

### Processing Pipeline

```
Client ──upload──▶ S3 (pre-signed URL)
                      │
                      ▼ S3 Event Notification
                  ┌──────────────┐
                  │ Media Process │
                  │ Lambda/SQS   │
                  ├──────────────┤
                  │ 1. Validate  │ (file type, size, virus scan)
                  │ 2. Moderate  │ (NSFW detection via ML)
                  │ 3. Transcode │ (video → multiple resolutions)
                  │ 4. Thumbnail │ (generate thumbnails)
                  │ 5. CDN URL   │ (generate CloudFront URL)
                  │ 6. Update DB │ (status = 'ready', cdn_url = ...)
                  └──────────────┘
```

---

## 9. Search Index (Elasticsearch)

### Index Mapping

```json
{
  "mappings": {
    "properties": {
      "tweet_id":          { "type": "keyword" },
      "user_id":           { "type": "keyword" },
      "username":          { "type": "keyword" },
      "content":           { "type": "text", "analyzer": "twitter_analyzer" },
      "hashtags":          { "type": "keyword" },
      "mentions":          { "type": "keyword" },
      "created_at":        { "type": "date" },
      "like_count":        { "type": "integer" },
      "retweet_count":     { "type": "integer" },
      "reply_count":       { "type": "integer" },
      "language":          { "type": "keyword" },
      "has_media":         { "type": "boolean" },
      "is_verified_author": { "type": "boolean" }
    }
  },
  "settings": {
    "number_of_shards": 32,
    "number_of_replicas": 1,
    "analysis": {
      "analyzer": {
        "twitter_analyzer": {
          "type": "custom",
          "tokenizer": "standard",
          "filter": ["lowercase", "twitter_stop", "twitter_stemmer"]
        }
      }
    }
  }
}
```

### Index Lifecycle (Hot-Warm-Cold)

```
Hot tier (last 7 days):    SSDs, 32 shards, full replicas
Warm tier (7-90 days):     HDDs, read-only, reduced replicas
Cold tier (90+ days):      Archived to S3, searchable via restore-on-demand
```

---

## 10. Rate Limiting Store (Redis)

### Sliding Window Counter

```redis
# Rate limit: 300 tweets per 3 hours
# Key: ratelimit:tweet:{user_id}:{window_id}
# Window ID = floor(current_time / window_size)

# On each tweet:
SET window_id = floor(now() / 10800)   # 10800 = 3 hours in seconds
INCR ratelimit:tweet:{user_id}:{window_id}
EXPIRE ratelimit:tweet:{user_id}:{window_id} 10800

# Check: if value > 300, reject with 429

# For more precise sliding window, use sorted sets:
ZADD ratelimit:tweet:{user_id} {now_ms} {request_id}
ZREMRANGEBYSCORE ratelimit:tweet:{user_id} 0 {now_ms - window_ms}
ZCARD ratelimit:tweet:{user_id}  # if > 300, reject
EXPIRE ratelimit:tweet:{user_id} {window_seconds}
```

---

## 11. Idempotency Store (Redis)

```redis
# On POST /tweets with Idempotency-Key header:
SET idempotency:{idempotency_key} '{response_json}' NX EX 86400

# NX = only set if not exists
# EX 86400 = expire after 24 hours

# Flow:
# 1. Check: GET idempotency:{key}
#    → If exists: return cached response (no-op)
#    → If not: process request, then SET with NX
```

---

## Complete Data Flow Diagram

```
                            POST /v1/tweets
                                  │
                                  ▼
                          ┌───────────────┐
                          │ Tweet Service  │
                          └───────┬───────┘
                                  │
                    ┌─────────────┼─────────────────┐
                    │             │                  │
                    ▼             ▼                  ▼
            ┌──────────┐  ┌────────────┐   ┌────────────────┐
            │ MySQL    │  │ Redis      │   │ Kafka          │
            │ (tweets  │  │ tweet cache│   │ TweetCreated   │
            │  table)  │  │ + owner    │   │ event          │
            └──────────┘  │ mapping    │   └────────┬───────┘
                          └────────────┘            │
                                          ┌─────────┼──────────┐
                                          │         │          │
                                          ▼         ▼          ▼
                                   ┌──────────┐ ┌────────┐ ┌────────────┐
                                   │ Fanout   │ │ Search │ │ Counter    │
                                   │ Service  │ │ Indexer│ │ Service    │
                                   └────┬─────┘ └───┬────┘ └─────┬──────┘
                                        │           │             │
                              ┌─────────┘           │             │
                              │                     │             │
                              ▼                     ▼             ▼
                      ┌──────────────┐    ┌──────────────┐ ┌──────────┐
                      │ Redis        │    │ Elasticsearch│ │ Redis    │
                      │ Timeline     │    │ (tweet index)│ │ Counters │
                      │ Cache        │    └──────────────┘ └──────────┘
                      │ (per follower│
                      │  ZADD)       │
                      └──────────────┘



                          GET /v1/timeline/home
                                  │
                                  ▼
                          ┌───────────────┐
                          │ Timeline      │
                          │ Service       │
                          └───────┬───────┘
                                  │
                    ┌─────────────┼──────────────────┐
                    │             │                   │
                    ▼             ▼                   ▼
            ┌──────────────┐ ┌──────────┐    ┌──────────────┐
            │ Redis        │ │ Redis    │    │ MySQL        │
            │ Timeline     │ │ Tweet    │    │ (celebrity   │
            │ ZREVRANGE    │ │ Cache    │    │  tweets -    │
            │ → tweet_ids  │ │ MGET    │    │  fanout on   │
            └──────────────┘ │ → tweets │    │  read)       │
                             └──────────┘    └──────────────┘
                                  │
                                  ▼
                          ┌──────────────┐
                          │ Redis        │
                          │ User Cache   │
                          │ + Like Sets  │
                          │ (hydration)  │
                          └──────────────┘
```

---

## Summary: Storage Requirements at Scale

| Store | Size Estimate | Instance Count | Notes |
|-------|--------------|----------------|-------|
| **MySQL (Tweets)** | ~180 TB/year | 64 shards × 3 replicas = 192 instances | Sharded by user_id |
| **MySQL (Users)** | ~250 GB | 1 primary + 4 read replicas | Single instance sufficient |
| **MySQL (Likes)** | ~1 TB/year | Co-located with tweet shards | Same shard key (user_id) |
| **Cassandra (Social Graph)** | ~500 GB (30B edges × ~16 bytes + overhead) | 12 nodes per DC × 3 DCs = 36 nodes | RF=3 per DC |
| **Redis (Timeline Cache)** | ~2 TB | 40 nodes (20 shards × 2) | Only active users cached |
| **Redis (Tweet Cache)** | ~500 GB | 10 nodes | 24h TTL, 99%+ hit rate |
| **Redis (User Cache)** | ~50 GB | 4 nodes | 1h TTL |
| **Redis (Counters)** | ~100 GB | 4 nodes | Atomic INCR/DECR |
| **S3 (Media)** | ~5 PB/year | Managed service | Images + video transcodes |
| **Elasticsearch** | ~50 TB (hot) | 32 shards, 16 nodes | Hot-warm-cold tiering |

---

*This datastore design document complements the [interview simulation](../interview-simulation.md) and [API contracts](api-contracts.md).*