# Data Storage & Caching

> Instagram's storage layer evolved from a single PostgreSQL database to a sophisticated
> multi-system architecture spanning TAO, Cassandra, MySQL/MyRocks, Redis, Memcache,
> Haystack, and f4.

---

## Table of Contents

1. [Storage Evolution](#1-storage-evolution)
2. [PostgreSQL (Early Days)](#2-postgresql-early-days)
3. [TAO (Social Graph Store)](#3-tao-social-graph-store)
4. [Cassandra (Write-Heavy Data)](#4-cassandra-write-heavy-data)
5. [MySQL + MyRocks (Structured Data)](#5-mysql--myrocks-structured-data)
6. [Redis (Feed Inboxes & Counters)](#6-redis-feed-inboxes--counters)
7. [Memcache (General Caching)](#7-memcache-general-caching)
8. [Haystack (Hot Photo Storage)](#8-haystack-hot-photo-storage)
9. [f4 (Warm Photo Storage)](#9-f4-warm-photo-storage)
10. [Elasticsearch (Search Index)](#10-elasticsearch-search-index)
11. [Data Pipeline](#11-data-pipeline)
12. [Contrasts](#12-contrasts)

---

## 1. Storage Evolution

```
2010 (launch)                          2012 (acquired)                    2024+
┌──────────────┐                    ┌──────────────┐              ┌──────────────────┐
│ PostgreSQL   │                    │ PostgreSQL   │              │ TAO (graph)      │
│ (everything) │  ──────────────>   │ Redis        │  ────────>   │ Cassandra (feeds)│
│ Amazon S3    │                    │ Memcached    │              │ MySQL/MyRocks    │
│ 3 engineers  │                    │ Amazon S3    │              │ Redis            │
│ 13 employees │                    │ Celery/RabbitMQ            │ Memcache         │
└──────────────┘                    │ ~100 EC2     │              │ Haystack/f4      │
                                    │ 13 employees │              │ Elasticsearch    │
                                    └──────────────┘              │ Data pipeline    │
                                                                  └──────────────────┘
```

**Key lesson:** There is no one-size-fits-all database. Instagram uses different stores for different access patterns.

---

## 2. PostgreSQL (Early Days)

**VERIFIED — from Instagram Engineering Blog "What Powers Instagram" (2012)**

Instagram's initial stack (2010-2012):
- **Django** (Python web framework) with **PostgreSQL** as the primary database
- Stored: users, posts, likes, comments, follows — everything
- Ran on **Amazon EC2** with Amazon S3 for photo storage
- **13 employees** serving **30+ million users** at time of Facebook acquisition (April 2012)
- ~100+ EC2 instances

**What worked:** PostgreSQL is excellent for a small-to-medium application. ACID transactions, rich query language, great tooling. Instagram scaled PostgreSQL further than most teams would have.

**What broke:** As Instagram grew beyond 30M users:
- The follows table (follower_id, followee_id) grew to hundreds of millions of rows — JOINs became slow
- Feed generation queries (join follows + posts + sort) couldn't keep up
- Write throughput hit limits — likes and follows are high-volume writes
- Sharding PostgreSQL is painful (no native horizontal sharding)

---

## 3. TAO (Social Graph Store)

**VERIFIED — from "TAO: Facebook's Distributed Data Store for the Social Graph", USENIX ATC 2013**

After acquisition, Instagram adopted Meta's infrastructure. TAO replaced ad-hoc PostgreSQL queries for social graph operations.

| Aspect | Detail |
|---|---|
| **Data model** | Objects (nodes) + Associations (directed edges) |
| **Persistent store** | Sharded MySQL |
| **Cache** | Two-tier: L1 (leaf, many per region) → L2 (root, few per region) |
| **Consistency** | Eventual across regions; read-after-write in leader region |
| **Read:Write ratio** | ~500:1 |
| **Scale** | Billions of reads/second (as of 2013 paper; much higher now) |

**What Instagram stores in TAO:**
- Follow/following edges: `(user_A, FOLLOWS, user_B)`
- Like edges: `(user_A, LIKES, post_P)`
- Comment associations: `(comment_C, ON_POST, post_P)`
- User objects, post objects, comment objects

See [05-social-graph.md](05-social-graph.md) for detailed TAO architecture.

---

## 4. Cassandra (Write-Heavy Data)

Instagram adopted Cassandra for high-write-throughput workloads that don't fit TAO's graph model.

**Use cases:**
- **Feed inboxes** (fan-out on write): when user A posts, write `(postId, timestamp)` to every follower's feed inbox
- **Activity/notifications**: "user_X liked your post" entries — high write volume, time-ordered
- **Stories metadata**: with column-level TTL (24 hours) — Cassandra handles automatic expiration
- **Direct message history**: write-heavy, time-series data

**Why Cassandra?**
- **Linearly scalable writes**: Add more nodes → get more write throughput. No single write bottleneck.
- **Tunable consistency**: `ONE` for fast writes (feed inboxes), `QUORUM` for important data
- **Time-series friendly**: Data modeled as wide rows with timestamp-based columns — natural fit for feeds and activity streams
- **TTL support**: Columns can expire automatically — perfect for Stories (24-hour TTL)
- **Multi-datacenter replication**: Async replication across data centers

| Aspect | Detail |
|---|---|
| **Data model** | Wide-column (partition key + clustering columns) |
| **Consistency** | Tunable (ONE, QUORUM, ALL) |
| **Replication** | Async, multi-datacenter |
| **Strengths** | Write throughput, time-series, TTL |
| **Weaknesses** | No JOINs, no transactions, read latency higher than Redis/Memcache |

---

## 5. MySQL + MyRocks (Structured Data)

**VERIFIED — from Facebook Engineering Blog "MyRocks: A Space- and Write-Optimized MySQL Database" (2016)**

Meta uses MySQL as the persistent store under TAO and for structured data that needs relational properties.

**MyRocks** is Meta's custom MySQL storage engine built on RocksDB (LSM-tree based):

| Aspect | MyRocks (LSM-Tree) | InnoDB (B+ Tree) |
|---|---|---|
| **Space efficiency** | ~2x less disk space (50% reduction) | B+ tree page fragmentation |
| **Write amplification** | 2-5x | 10-30x |
| **Compression** | Excellent (sorted, compacted data) | Moderate |
| **Read latency** | Slightly higher (may check multiple LSM levels) | Excellent (B+ tree lookup) |
| **SSD lifespan** | Better (lower write amplification) | Worse |

**What Instagram stores in MySQL/MyRocks:**
- User account data (username, email, bio, settings)
- Post metadata (postId, authorId, caption, location, timestamps)
- Content metadata (media dimensions, format, processing status)
- Billing and account management data (if applicable)

**Why MySQL and not just TAO?**
TAO is optimized for graph operations (objects + associations). Not everything is a graph edge. Structured data with rich querying needs (admin dashboards, analytics, content management) is better served by SQL.

---

## 6. Redis (Feed Inboxes & Counters)

**VERIFIED — from Instagram Engineering Blog "Storing Hundreds of Millions of Simple Key-Value Pairs in Redis" (2013)**

Redis is used for data that must be fast, in-memory, and supports rich data structures:

**Use cases:**

| Use Case | Data Structure | Example |
|---|---|---|
| **Feed inboxes** | Sorted Set | `ZADD feed:user-B 1705312800 post-uuid-xyz` |
| **Like counters** | String (INCR) | `INCR likes:post-uuid-abc` |
| **View counters** | HyperLogLog | `PFADD views:post-uuid-abc user-123` |
| **Rate limiting** | String with TTL | `SET ratelimit:user-123:follow 1 EX 3600` |
| **Stories seen state** | Set or Bitmap | `SADD seen:user-123:stories story-uuid-1` |
| **Online presence** | String with TTL | `SET online:user-123 1 EX 300` |
| **Session data** | Hash | `HSET session:abc userId user-123 device iOS` |

**Feed inbox details:**
```
Key:    feed:{userId}
Type:   Sorted Set
Score:  timestamp (or ranking score)
Member: postId

Operations:
  ZADD feed:user-B 1705312800 post-uuid-xyz   // Add post to feed
  ZREVRANGE feed:user-B 0 19                   // Get top 20 most recent
  ZREM feed:user-B post-uuid-xyz               // Remove post (unfollow/delete)
  ZCARD feed:user-B                            // Count items in feed
  ZRANGEBYSCORE feed:user-B 1705000000 +inf    // Get posts after timestamp
```

**Memory budget:**
- ~2B users × (assume 30% active with feed inbox in memory) × 500 entries × 50 bytes/entry
- = 600M × 500 × 50 = **~15 TB** of Redis memory for feed inboxes alone
- This is distributed across thousands of Redis shards

---

## 7. Memcache (General Caching)

**VERIFIED — from "Scaling Memcache at Facebook", USENIX NSDI 2013**

Meta's Memcache deployment is one of the largest in the world:

| Metric | Value (as of 2013 paper) |
|---|---|
| **Servers** | Thousands |
| **Requests/second** | Billions |
| **Items stored** | Trillions |
| **Read diversion** | >70% of reads served from cache |

**Architecture:**
- Organized hierarchically: within-cluster → within-region → across-regions
- **Lease mechanism**: Prevents thundering herds. When a cached item expires, the first client to request it gets a "lease" to recompute and fill the cache. Other clients wait or get a slightly stale value.
- **Invalidation pipeline**: When data changes in MySQL, a daemon (`mcsqueal`) tails the MySQL commit log and sends delete commands to Memcache servers — ensuring cache consistency.

**What Instagram caches in Memcache:**
- User profiles (bio, follower count, avatar URL)
- Post metadata (caption, media URLs, engagement counts)
- Social graph edges (is A following B? — cached in TAO's L1/L2 which uses Memcache)
- Feature flags and configuration
- Session data

**Why Memcache and not Redis for general caching?**
- Memcache is simpler (just key-value, no data structures) and slightly faster for simple get/set
- Redis adds rich data structures (sorted sets, lists, HyperLogLog) but at the cost of higher memory overhead per item
- Instagram uses Memcache for simple KV caching and Redis for structured data (feed inboxes, counters)

---

## 8. Haystack (Hot Photo Storage)

**VERIFIED — from "Finding a Needle in Haystack: Facebook's Photo Storage", USENIX OSDI 2010**

Haystack is Meta's custom photo storage system, designed to minimize I/O overhead for billions of small files.

### The Problem

Traditional filesystems waste I/O on metadata:
```
Standard filesystem (ext4/XFS):
  Read a photo:
    1. Directory lookup (disk I/O)       ← metadata overhead
    2. Read inode (disk I/O)             ← metadata overhead
    3. Read data blocks (disk I/O)       ← actual data
    Total: 3+ disk I/Os per photo

  At billions of photos, inode metadata doesn't fit in RAM.
  Each photo read = multiple disk seeks = slow.
```

### Haystack's Solution

```
Haystack:
  Read a photo:
    1. Lookup in-memory index: (photoId → volume_offset, size)  ← RAM, ~10 bytes/photo
    2. Seek to offset in volume file, read data                  ← 1 disk I/O
    Total: 1 disk I/O per photo

  Why: Multiple photos packed into a single large file (volume).
  The in-memory index is tiny (~10 bytes per photo) and fits in RAM.
```

### Architecture

```
┌───────────────┐     ┌───────────────┐     ┌───────────────┐
│ Haystack      │     │ Haystack      │     │ Haystack      │
│ Directory     │────>│ Cache         │────>│ Store         │
│               │     │               │     │               │
│ Maps photoId  │     │ Caches recent │     │ Physical      │
│ to Store URL  │     │ photos from   │     │ volumes on    │
│               │     │ write-enabled │     │ disk with     │
│               │     │ stores        │     │ in-memory     │
│               │     │               │     │ index         │
└───────────────┘     └───────────────┘     └───────────────┘
```

**Volume structure:**
```
Volume file (~100 GB):
┌──────┬──────┬──────┬──────┬──────┬──────┐
│Photo1│Photo2│Photo3│Photo4│Photo5│ ...  │
│(data)│(data)│(data)│(data)│(data)│      │
└──────┴──────┴──────┴──────┴──────┴──────┘

Each "needle" (photo) stored as:
[Header | Cookie | Key | Alt-Key | Flags | Size | Data | Checksum]

In-memory index per volume:
  (key, alt_key) → (offset_in_volume, data_size)
  ~10 bytes per photo
```

**Scale (from 2010 paper):**
- 260 billion images stored
- ~1 million image uploads per second at peak

---

## 9. f4 (Warm Photo Storage)

**VERIFIED — from "f4: Facebook's Warm BLOB Storage System", USENIX OSDI 2014**

f4 stores older, less-frequently-accessed photos using erasure coding instead of full replication.

### Why f4?

Haystack uses **3.6x effective replication** (3 replicas + geo-backup overhead). For warm data (old photos still accessed occasionally), this is wasteful.

Facebook observed: only ~8% of BLOBs account for ~82% of requests. The vast majority of photos are "warm" — rarely modified, infrequently accessed, but must still be available.

### Erasure Coding

f4 uses **Reed-Solomon (14, 10)** erasure coding:
- Data split into **10 data blocks**
- **4 parity blocks** computed
- Any 10 of 14 blocks can reconstruct the original data
- **Effective replication factor: 1.4x** (vs Haystack's 3.6x)
- **2.1x overall** with XOR buddy volume coding for additional fault tolerance

**Storage savings:**
- Haystack: 3.6x replication
- f4: 2.1x effective replication
- **Savings: ~42% less storage** for warm data
- At Meta's scale (exabytes of photos), this saves enormous amounts of disk

### Data Lifecycle

```
Photo uploaded → Haystack (hot, 3.6x replication)
        │
        │ After cooling period (days to weeks based on access frequency)
        │
        ▼
Photo migrated → f4 (warm, 2.1x replication via erasure coding)
        │
        │ Metadata updated to point to f4 location
        │ CDN continues to serve from cache (no user-visible change)
```

---

## 10. Elasticsearch (Search Index)

Used for text search across users, hashtags, locations, and post captions.

| Use Case | Index Contents |
|---|---|
| **User search** | Usernames, display names, bios |
| **Hashtag search** | Hashtag names, post counts |
| **Location search** | Place names, coordinates (geospatial index) |
| **Keyword search** | Post captions, alt text (introduced 2020) |
| **Content moderation** | Searching for banned content patterns |

**Note:** Meta primarily uses their own internal search systems (Unicorn) at scale. Elasticsearch may be used for specific workloads or was used in Instagram's pre-acquisition days. [PARTIALLY VERIFIED — Instagram mentioned Solr/ES in early engineering posts]

---

## 11. Data Pipeline

Instagram generates massive behavioral data that feeds ML training and analytics:

```
User actions (likes, views, scrolls, time-on-screen)
        │
        ▼
┌──────────────────┐
│ Scribe            │  Meta's log transport system
│ (event logging)   │  Collects behavioral events from all services
└────────┬─────────┘
         │
         ▼
┌──────────────────┐
│ Kafka / Stream    │  Real-time stream processing
│ Processing        │  For: live dashboards, real-time features,
│                   │  trending detection
└────────┬─────────┘
         │
         ├──> Real-time feature store (for online serving)
         │
         ▼
┌──────────────────┐
│ Data Warehouse    │  Hive/Presto on HDFS (batch processing)
│                   │  For: ML training, analytics, A/B testing
└────────┬─────────┘
         │
         ▼
┌──────────────────┐
│ ML Training       │  Train recommendation models, feed ranking,
│ Pipeline          │  content moderation models, search ranking
└──────────────────┘
```

**Scale:** Instagram processes billions of behavioral events per day. This data powers:
- Feed ranking model training (what did users engage with?)
- Explore/Reels recommendation (what content is interesting?)
- Content moderation (what patterns indicate policy violations?)
- A/B testing (which variant performed better?)

---

## 12. Contrasts

### Instagram vs Twitter — Storage

| Dimension | Instagram | Twitter |
|---|---|---|
| **Primary data** | Media-heavy (photos, videos) | Text-heavy (tweets) |
| **Graph store** | TAO (Meta's graph store) | FlockDB → Manhattan |
| **Feed store** | Redis sorted sets + Cassandra | Manhattan (internal KV store) |
| **Media storage** | Haystack + f4 (custom blob storage) | Less critical (text-first) |
| **Caching** | Memcache (trillions of ops/day across Meta) | Custom caching layer |
| **Key bottleneck** | Media storage and serving | Timeline assembly and delivery |

### Instagram vs Netflix — Storage Shape

| Dimension | Instagram | Netflix |
|---|---|---|
| **Item count** | Billions of photos/videos | Tens of thousands of titles |
| **Per-item size** | Small (KB-MB per photo, MB per short video) | Large (GB per title × ~120 profiles) |
| **Storage shape** | Many items × few variants | Few items × many variants |
| **Total storage** | Exabytes (dominated by photo count) | Petabytes (dominated by encoding variants) |
| **Blob storage** | Haystack + f4 (custom) | Amazon S3 |
| **Key bottleneck** | Metadata I/O for billions of files | Segment management per title |

### The Right Tool for Each Access Pattern

| Access Pattern | Store | Why |
|---|---|---|
| Social graph (follow/like/comment edges) | TAO | Purpose-built for graph operations, cached |
| Feed inboxes (fan-out writes) | Redis + Cassandra | Fast sorted sets (Redis), durable writes (Cassandra) |
| User/post metadata | MySQL/MyRocks | Relational, rich queries, ACID |
| General caching | Memcache | Simple KV, massive scale, lease mechanism |
| Photo/video storage (hot) | Haystack | 1 disk I/O per read, in-memory index |
| Photo/video storage (warm) | f4 | Erasure coding, 42% less storage |
| Search index | Elasticsearch/Unicorn | Full-text search, prefix matching |
| Stories metadata | Cassandra (with TTL) | Automatic expiration after 24 hours |
| Behavioral events | Scribe → Kafka → Hive | High-volume event ingestion and processing |
