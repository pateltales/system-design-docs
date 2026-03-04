# Netflix — Data Storage & Caching Deep Dive

> Companion deep dive to the [interview simulation](01-interview-simulation.md). This document explores how Netflix stores and caches the data that powers 301M+ subscribers across 190+ countries -- from user profiles and viewing history to video segments and billing records.

---

## Table of Contents

1.  [Storage Landscape Overview](#1-storage-landscape-overview)
2.  [Apache Cassandra — The Primary NoSQL Database](#2-apache-cassandra--the-primary-nosql-database)
3.  [EVCache — Netflix's Distributed Caching Layer](#3-evcache--netflixs-distributed-caching-layer)
4.  [Multi-Layer Caching Architecture](#4-multi-layer-caching-architecture)
5.  [Amazon S3 — Video Asset Object Storage](#5-amazon-s3--video-asset-object-storage)
6.  [Amazon Aurora — Relational Data for Billing & Accounts](#6-amazon-aurora--relational-data-for-billing--accounts)
7.  [Elasticsearch — Search & Annotation Querying](#7-elasticsearch--search--annotation-querying)
8.  [Data Pipeline — From Viewing Events to ML Models](#8-data-pipeline--from-viewing-events-to-ml-models)
9.  [Netflix vs YouTube — Storage Comparison](#9-netflix-vs-youtube--storage-comparison)
10. [Interview Cheat Sheet](#10-interview-cheat-sheet)

---

## 1. Storage Landscape Overview

Netflix does not use a single database. It uses a polyglot persistence architecture where each data category is stored in the technology best suited to its access patterns, consistency requirements, and scale profile.

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                       Netflix Data Storage Landscape                        │
├──────────────────────┬──────────────────────┬───────────────────────────────┤
│ Data Category        │ Storage Technology   │ Why This Choice               │
├──────────────────────┼──────────────────────┼───────────────────────────────┤
│ User profiles        │ Cassandra            │ High write throughput,        │
│ Viewing history      │                      │ multi-region replication,     │
│ Bookmarks (resume)   │                      │ tunable consistency,          │
│ Content metadata     │                      │ no single point of failure    │
│ My List              │                      │                               │
│ Ratings              │                      │                               │
├──────────────────────┼──────────────────────┼───────────────────────────────┤
│ Session data         │ EVCache (Memcached)  │ Sub-millisecond latency,      │
│ Personalization      │                      │ 400M+ ops/sec,               │
│ Homepage data        │                      │ zone-aware replication        │
│ Search results cache │                      │                               │
├──────────────────────┼──────────────────────┼───────────────────────────────┤
│ Video segments       │ Amazon S3            │ Unlimited object storage,     │
│ Source masters       │                      │ 11 nines durability,          │
│ Artwork / subtitles  │                      │ serves as CDN origin          │
├──────────────────────┼──────────────────────┼───────────────────────────────┤
│ Billing & accounts   │ Amazon Aurora        │ ACID transactions,            │
│ Subscription state   │                      │ strong consistency for        │
│ Payment history      │                      │ financial data                │
├──────────────────────┼──────────────────────┼───────────────────────────────┤
│ Title search index   │ Elasticsearch        │ Full-text search,             │
│ Annotations          │                      │ fuzzy matching,               │
│                      │                      │ multi-language support        │
├──────────────────────┼──────────────────────┼───────────────────────────────┤
│ Analytics warehouse  │ Iceberg on S3        │ Petabyte-scale analytics,     │
│ ML training data     │                      │ columnar format,              │
│                      │                      │ schema evolution              │
└──────────────────────┴──────────────────────┴───────────────────────────────┘
```

**Key insight for interviews:** ~98% of streaming-path data (everything the user interacts with during browse and playback) flows through Cassandra and EVCache. The remaining 2% is billing/account data in Aurora and search in Elasticsearch. S3 holds the video assets themselves but is accessed through the CDN layer, not directly by clients.

---

## 2. Apache Cassandra -- The Primary NoSQL Database

### 2.1 Why Cassandra?

Cassandra is Netflix's database of record for nearly all streaming data. The choice was made early (circa 2011) when Netflix migrated from Oracle in its own data centers to a cloud-native architecture on AWS.

**The requirements that drove the choice:**

| Requirement | Why It Matters | How Cassandra Delivers |
|---|---|---|
| Multi-region writes | Netflix operates in 190+ countries. Users in Tokyo and New York both write viewing history concurrently. | Cassandra is masterless -- every node can accept writes. Async replication across regions. |
| No single point of failure | Any outage = global headline. No leader election downtime acceptable. | Peer-to-peer gossip protocol. No leader, no coordinator, no master. |
| Tunable consistency | Some data (bookmarks) tolerates eventual consistency. Some (account state) needs quorum. | Per-query consistency: ONE, QUORUM, LOCAL_QUORUM, ALL. |
| Linear scalability | Adding 50M subscribers per year. Storage and throughput must scale by adding nodes. | Consistent hashing ring. Add nodes = automatic data rebalancing. |
| High write throughput | Every second of viewing generates events: position updates, heartbeats, quality metrics. | Log-structured merge tree (LSM). Writes go to memtable first (memory), then flush to SSTable (disk). Writes are always fast. |

### 2.2 Scale Numbers

```
Netflix Cassandra Fleet (estimated, based on public disclosures):

  Clusters:            Hundreds (each microservice owns its cluster)
  Total nodes:         Tens of thousands (across all regions)
  Data stored:         Petabytes
  Transactions/sec:    Millions
  Regions:             3+ AWS regions (us-east-1, us-west-2, eu-west-1, plus more)
  Replication factor:  Typically 3 per region (RF=3)
```

### 2.3 Data Model Examples

Cassandra is a wide-column store. Data is modeled around query patterns, not normalized relationships.

**Viewing History Table:**

```
CREATE TABLE viewing_history (
    profile_id    UUID,
    watched_at    TIMESTAMP,
    title_id      UUID,
    duration_sec  INT,
    completed     BOOLEAN,
    device_type   TEXT,
    PRIMARY KEY ((profile_id), watched_at)
) WITH CLUSTERING ORDER BY (watched_at DESC);
```

- **Partition key:** `profile_id` -- all history for one profile lives on the same node set.
- **Clustering key:** `watched_at DESC` -- most recent first, so "Continue Watching" reads the first few rows.
- **Query:** `SELECT * FROM viewing_history WHERE profile_id = ? LIMIT 50;` -- single partition, single node, fast.

**Bookmark (Resume Position) Table:**

```
CREATE TABLE bookmarks (
    profile_id     UUID,
    title_id       UUID,
    position_sec   INT,
    updated_at     TIMESTAMP,
    PRIMARY KEY ((profile_id, title_id))
);
```

- **Partition key:** `(profile_id, title_id)` -- one row per profile-per-title.
- **Write pattern:** Overwritten every 10-30 seconds during playback. Cassandra handles this efficiently because writes are append-only in the LSM tree.
- **Consistency:** Written with `LOCAL_ONE` (fast, same region). Read with `LOCAL_ONE` (eventual consistency is fine -- if the bookmark is a few seconds off, the user barely notices).

### 2.4 Tunable Consistency in Practice

```
Consistency Levels Used at Netflix:

  ┌─────────────────┬──────────────────────────────────────────────────────────┐
  │ Level           │ Behavior                                                 │
  ├─────────────────┼──────────────────────────────────────────────────────────┤
  │ ONE             │ Write/read acknowledged by 1 replica.                    │
  │                 │ Fastest. Used for: bookmarks, heartbeats,                │
  │                 │ non-critical viewing events.                             │
  ├─────────────────┼──────────────────────────────────────────────────────────┤
  │ LOCAL_QUORUM    │ Majority of replicas in LOCAL region must acknowledge.   │
  │                 │ Used for: user profile reads, My List updates,           │
  │                 │ content metadata where stale data is visible.            │
  ├─────────────────┼──────────────────────────────────────────────────────────┤
  │ QUORUM          │ Majority of ALL replicas across ALL regions.             │
  │                 │ Rarely used -- cross-region latency penalty.             │
  │                 │ Netflix prefers LOCAL_QUORUM + async replication.        │
  ├─────────────────┼──────────────────────────────────────────────────────────┤
  │ ALL             │ Every replica must acknowledge.                          │
  │                 │ Almost never used in production. Defeats the purpose     │
  │                 │ of distributed systems.                                  │
  └─────────────────┴──────────────────────────────────────────────────────────┘

  Netflix's default pattern:
    Write at LOCAL_QUORUM + Read at LOCAL_ONE
    OR
    Write at LOCAL_ONE + Read at LOCAL_QUORUM

  This gives strong-enough consistency without cross-region latency.
```

### 2.5 Multi-Region Replication

```
                    Async Replication (typically < 1 second)
  ┌──────────────┐ ──────────────────────────────────> ┌──────────────┐
  │  us-east-1   │                                     │  eu-west-1   │
  │              │                                     │              │
  │  RF=3        │ <────────────────────────────────── │  RF=3        │
  │  (3 copies   │    Async Replication                │  (3 copies   │
  │   in 3 AZs)  │                                     │   in 3 AZs)  │
  └──────┬───────┘                                     └──────────────┘
         │
         │ Async Replication
         v
  ┌──────────────┐
  │  us-west-2   │
  │              │
  │  RF=3        │
  │  (3 copies   │
  │   in 3 AZs)  │
  └──────────────┘

  Total copies of each piece of data: 3 regions x 3 replicas = 9 copies
  Write path: Client writes to LOCAL region -> acknowledged by LOCAL_QUORUM (2/3)
              -> async replicated to other regions
  Conflict resolution: Last-write-wins (LWW) with synchronized clocks
```

**Why async and not sync?** Cross-region latency is 50-150ms. If every write had to wait for 3 regions to acknowledge (sync replication), write latency would be 150ms+ instead of ~1ms. For viewing history and bookmarks, eventual consistency with sub-second convergence is an excellent trade-off.

### 2.6 Contrast: Netflix (Cassandra) vs YouTube (Bigtable + Spanner)

| Dimension | Netflix (Cassandra) | YouTube (Bigtable + Spanner) |
|---|---|---|
| **Consistency model** | Eventually consistent (tunable) | Bigtable: eventually consistent. Spanner: externally consistent (strongest possible -- real clock-based). |
| **Why this model?** | Streaming data tolerates brief inconsistency. A bookmark being 2 seconds off is invisible to the user. | YouTube needs strong consistency for monetization (ad impressions, creator revenue). A dropped ad impression = lost revenue. |
| **Managed vs self-hosted** | Netflix runs its own Cassandra fleet on EC2. Full operational ownership. | Google runs Bigtable and Spanner as managed services. YouTube is a first-party consumer. |
| **Schema model** | Wide-column, CQL (SQL-like) | Bigtable: wide-column, no SQL. Spanner: relational, full SQL with distributed transactions. |
| **Multi-region** | Async replication, masterless | Spanner: synchronous replication using TrueTime (atomic clocks + GPS). ~7ms cross-region write latency. |
| **Trade-off** | Lower latency writes, possible stale reads | Higher write latency, guaranteed fresh reads |
| **Scale** | Tens of thousands of nodes | Millions of nodes (Google-wide) |

**Interview talking point:** "Netflix chose eventual consistency because their core data (what you watched, where you stopped) has a natural tolerance for staleness. YouTube chose strong consistency where money is involved (ad serving, creator payments) but uses Bigtable (eventually consistent) for the viewing metadata that doesn't need it."

---

## 3. EVCache -- Netflix's Distributed Caching Layer

### 3.1 What Is EVCache?

EVCache (Ephemeral Volatile Cache) is Netflix's distributed caching solution, built on top of **Memcached** with significant Netflix-specific additions. It sits between the application tier and Cassandra, absorbing the vast majority of read traffic.

> **Source:** [Caching for a Global Netflix](https://netflixtechblog.com/caching-for-a-global-netflix-7bcc457012f1) -- Netflix Tech Blog

### 3.2 Scale Numbers

```
EVCache Fleet (from Netflix public disclosures):

  Clusters:                ~200
  Memcached instances:     ~22,000
  Operations per second:   ~400 million (400M ops/sec)
  Data stored:             ~14.3 PB (petabytes)
  Items cached:            ~2 trillion
  Network throughput:      Tens of TB/sec across the fleet
```

These numbers make EVCache one of the largest Memcached deployments in the world.

### 3.3 Architecture

```
┌──────────────────────────────────────────────────────────────────────────┐
│                         EVCache Architecture                             │
│                                                                          │
│   Application (e.g., Playback Service)                                   │
│        │                                                                 │
│        v                                                                 │
│   ┌─────────────────────────────────────────────┐                        │
│   │          EVCache Client Library              │                        │
│   │  - Topology-aware routing                    │                        │
│   │  - Consistent hashing (shard selection)      │                        │
│   │  - Auto-discovery via Eureka                 │                        │
│   │  - Zone-aware read preference                │                        │
│   │  - Retry & fallback logic                    │                        │
│   └────────────┬───────────────┬────────────────┘                        │
│                │               │                                         │
│         ┌──────┘               └──────┐                                  │
│         v                             v                                  │
│   ┌───────────────┐           ┌───────────────┐          ┌─────────────┐ │
│   │    AZ-1       │           │    AZ-2       │          │    AZ-3     │ │
│   │               │           │               │          │             │ │
│   │  ┌─────────┐  │           │  ┌─────────┐  │          │ ┌─────────┐│ │
│   │  │Shard 0  │  │  Async    │  │Shard 0  │  │  Async   │ │Shard 0  ││ │
│   │  │Shard 1  │  │◄────────►│  │Shard 1  │  │◄────────►│ │Shard 1  ││ │
│   │  │Shard 2  │  │  Repl.   │  │Shard 2  │  │  Repl.   │ │Shard 2  ││ │
│   │  │  ...    │  │           │  │  ...    │  │          │ │  ...    ││ │
│   │  │Shard N  │  │           │  │Shard N  │  │          │ │Shard N  ││ │
│   │  └─────────┘  │           │  └─────────┘  │          │ └─────────┘│ │
│   └───────────────┘           └───────────────┘          └─────────────┘ │
│                                                                          │
│   Write: goes to ALL zones (replicated by client)                        │
│   Read:  goes to LOCAL zone only (lowest latency)                        │
└──────────────────────────────────────────────────────────────────────────┘
```

**Key design decisions:**

1. **Zone-aware replication:** Data is replicated across all Availability Zones. Writes are fanned out by the client to every zone. Reads only go to the local zone. This means if AZ-1 goes down, AZ-2 and AZ-3 still have all the data.

2. **Sharding within zones:** Within each AZ, data is distributed across shards using consistent hashing on the cache key. Each shard is a single Memcached instance.

3. **Auto-discovery:** EVCache instances register with Eureka (Netflix's service registry). The client discovers available nodes dynamically -- no static configuration files.

4. **Linear scalability:** Need more capacity? Add more shards. Consistent hashing ensures minimal key redistribution.

### 3.4 API -- Deliberately Simple

```
EVCache API (simplified):

  get(key)              → Returns cached value or null (cache miss)
  set(key, value, ttl)  → Writes to all zones, returns success/failure
  touch(key, ttl)       → Extends TTL without re-fetching data
  delete(key)           → Removes from all zones
```

That is the entire interface. No complex queries, no secondary indexes, no transactions. This simplicity is intentional -- it keeps the caching layer fast, predictable, and easy to reason about.

### 3.5 What Gets Cached?

| Cache Use Case | Key Pattern | Value | TTL | Hit Rate |
|---|---|---|---|---|
| Session data | `session:{sessionId}` | Auth tokens, profile context | 30 min | ~99% |
| Personalized home rows | `home:{profileId}:{device}` | Serialized row data (titles, artwork URLs) | 15-30 min | ~95% |
| Viewable catalog | `catalog:{region}:{maturity}` | Filtered title list for region/maturity | 1-6 hours | ~99% |
| Title metadata | `title:{titleId}` | Synopsis, cast, runtime, ratings | 1-24 hours | ~99% |
| Search results | `search:{query}:{region}` | Pre-computed result set | 5-15 min | ~80% |
| Resume position | `bookmark:{profileId}:{titleId}` | Position in seconds | 24 hours | ~95% |
| Recommendation scores | `reco:{profileId}:{algo}` | Ranked title list | 1-6 hours | ~90% |
| A/B test allocation | `abtest:{memberId}:{testId}` | Test cell assignment | 24 hours | ~99% |

### 3.6 Why Memcached and Not Redis?

This is a common interview question. Netflix chose Memcached over Redis for EVCache. Here is why:

| Dimension | Memcached (EVCache) | Redis |
|---|---|---|
| **Data model** | Pure key-value. Flat byte blobs. | Rich data structures: strings, hashes, lists, sets, sorted sets, streams, HyperLogLog. |
| **Threading** | Multi-threaded. Uses all CPU cores on a single instance. | Single-threaded event loop (Redis 6 added I/O threads but core is still single-threaded). |
| **Memory efficiency** | Slab allocator. Predictable memory usage, no fragmentation. | jemalloc. Can fragment under mixed workloads. |
| **Use case fit** | Netflix's caching is 99% `get`/`set` of serialized blobs. No need for server-side data structures. | Redis data structures add overhead (metadata per structure) that Netflix doesn't need. |
| **Replication** | EVCache handles replication at the client level (write to all zones). No server-side replication needed. | Redis has built-in replication (primary-replica), but Netflix wanted zone-aware replication with client-side control. |
| **Simplicity** | Fewer failure modes. No persistence, no replication logic, no Lua scripts. Each instance is stateless and replaceable. | More features = more things that can go wrong. Persistence (RDB/AOF) can cause latency spikes. |

**The decisive factor:** Netflix needed a caching layer, not a database. Memcached's simplicity (no persistence, no replication, no complex data types) is a feature, not a limitation. The Netflix team built exactly the replication and discovery semantics they needed on top of Memcached's simple, fast, multi-threaded core.

**Interview talking point:** "We chose Memcached for EVCache because our access pattern is >99% simple get/set of serialized protobuf or JSON blobs. Redis's data structures would add memory overhead and complexity for features we don't use. Memcached's multi-threaded architecture gives better per-instance throughput, and we built zone-aware replication at the client layer where we have full control over write fan-out semantics."

---

## 4. Multi-Layer Caching Architecture

### 4.1 The Full Read Path

When a Netflix user opens the app and browses the homepage, the request traverses multiple caching layers before hitting the database:

```
┌──────────────────────────────────────────────────────────────────────────────────┐
│                    Netflix Multi-Layer Caching Architecture                       │
│                                                                                  │
│  ┌──────────┐                                                                    │
│  │  Client   │  Layer 0: Client-side cache                                       │
│  │  Device   │  - In-memory (app process): recently viewed tiles, profile data   │
│  │           │  - On-disk: downloaded content metadata, artwork                  │
│  │           │  - Hit rate: ~60-70% of UI data requests never leave the device   │
│  └────┬─────┘                                                                    │
│       │ Cache miss                                                               │
│       v                                                                          │
│  ┌──────────┐                                                                    │
│  │  CDN      │  Layer 1: CDN edge cache (Open Connect)                           │
│  │  (OCA)    │  - Video segments: ~95% hit rate                                  │
│  │           │  - Artwork/images: ~90% hit rate                                  │
│  │           │  - NOT used for API responses (those go to AWS backend)            │
│  └────┬─────┘                                                                    │
│       │ API requests (not video segments)                                         │
│       v                                                                          │
│  ┌──────────┐                                                                    │
│  │  API      │  Layer 2: Application-level cache                                 │
│  │  Gateway  │  - Zuul / API gateway: rate limiting, routing                     │
│  │           │  - In-process caches: Guava/Caffeine for hot config data          │
│  └────┬─────┘                                                                    │
│       │                                                                          │
│       v                                                                          │
│  ┌──────────┐                                                                    │
│  │ EVCache   │  Layer 3: Distributed cache (EVCache / Memcached)                 │
│  │           │  - 400M ops/sec, sub-millisecond p50 latency                      │
│  │           │  - Hit rate: ~95%+ for most data categories                       │
│  │           │  - Zone-local reads, all-zone writes                              │
│  └────┬─────┘                                                                    │
│       │ Cache miss (~5% of requests)                                             │
│       v                                                                          │
│  ┌──────────┐                                                                    │
│  │Cassandra  │  Layer 4: Primary database                                        │
│  │           │  - Only ~5% of read requests reach here                           │
│  │           │  - All writes go here (and populate cache on write-through)        │
│  │           │  - Multi-region, eventually consistent                             │
│  └──────────┘                                                                    │
│                                                                                  │
│  Effective system hit rate (L0 through L3):                                       │
│    ~99%+ of user-facing reads are served from cache                              │
│    Cassandra handles ~1% of read volume + 100% of writes                         │
└──────────────────────────────────────────────────────────────────────────────────┘
```

### 4.2 Cache Population Strategies

```
Strategy            │ How It Works                          │ Used For
────────────────────┼───────────────────────────────────────┼──────────────────────────
Cache-aside         │ App checks cache first. On miss,      │ Most EVCache use cases.
(Lazy loading)      │ reads from DB, then writes to cache.  │ Viewing history, title
                    │ Simple but first request is always    │ metadata, search results.
                    │ slow (cold cache penalty).            │
────────────────────┼───────────────────────────────────────┼──────────────────────────
Write-through       │ App writes to cache AND DB on every   │ Bookmarks (resume pos).
                    │ write. Cache is always warm. Higher   │ User writes bookmark;
                    │ write latency (two writes per op).    │ cache is immediately
                    │                                       │ updated for next read.
────────────────────┼───────────────────────────────────────┼──────────────────────────
Cache warming       │ Background job pre-populates cache    │ Catalog data. When a new
(Proactive)         │ before traffic arrives. No cold       │ title launches, cache is
                    │ cache penalty.                        │ warmed across all zones
                    │                                       │ before the title goes
                    │                                       │ live in the UI.
────────────────────┼───────────────────────────────────────┼──────────────────────────
Event-driven        │ Kafka event triggers cache update.    │ When a user changes
invalidation        │ Decoupled from the write path.        │ their profile name,
                    │ Eventually consistent cache.          │ a Kafka event propagates
                    │                                       │ cache invalidation.
```

### 4.3 Cache Invalidation -- The Hard Problem

Netflix handles cache invalidation through several mechanisms:

1. **TTL-based expiry:** Every cached item has a TTL. Short TTLs for volatile data (session: 30 min), long TTLs for stable data (title metadata: 24 hours). This is the primary invalidation mechanism and handles 90% of cases.

2. **Explicit delete on write:** When a user updates their My List, the service writes to Cassandra and simultaneously deletes the stale cache entry. The next read triggers a cache-aside reload.

3. **Event-driven invalidation:** For data that multiple services read but one service writes (e.g., content metadata updated by the content team), a Kafka event notifies all consumers to invalidate their cached copy.

4. **Versioned keys:** Some caches use versioned keys (`home:v42:{profileId}`) so that a new version of the recommendation model can be deployed without invalidating the old cache. Both versions coexist briefly until TTL expires old versions.

---

## 5. Amazon S3 -- Video Asset Object Storage

### 5.1 What Lives in S3?

S3 is the origin store for all video-related assets. Nothing is served directly from S3 to end users -- the CDN (Open Connect) sits in front.

```
S3 Object Categories:

  ┌────────────────────────┬──────────────────────────────────────────────────┐
  │ Category               │ Details                                          │
  ├────────────────────────┼──────────────────────────────────────────────────┤
  │ Source masters          │ ProRes 4444, JPEG 2000 IMF packages.            │
  │                        │ Hundreds of GB per title.                        │
  │                        │ Archival -- read only during re-encoding.        │
  ├────────────────────────┼──────────────────────────────────────────────────┤
  │ Transcoded segments    │ fMP4 (fragmented MP4) segments.                  │
  │                        │ Each segment: 2-10 seconds of video, ~MB each.  │
  │                        │ Per title: ~120 encoding profiles x hundreds    │
  │                        │ of segments per profile = tens of thousands of  │
  │                        │ objects per title.                               │
  ├────────────────────────┼──────────────────────────────────────────────────┤
  │ Artwork                │ Box art, hero images, title cards.               │
  │                        │ Multiple sizes per title (phone, tablet, TV).    │
  │                        │ Multiple variants per A/B test.                  │
  │                        │ Can be millions of images total.                 │
  ├────────────────────────┼──────────────────────────────────────────────────┤
  │ Subtitles              │ TTML/WebVTT files. Dozens of languages per      │
  │                        │ title. Relatively small (KB each).               │
  ├────────────────────────┼──────────────────────────────────────────────────┤
  │ Audio tracks           │ Multiple languages, Dolby Atmos, stereo, 5.1.   │
  │                        │ Stored as separate segments for ABR switching.   │
  └────────────────────────┴──────────────────────────────────────────────────┘
```

### 5.2 Scale Math

```
Back-of-envelope: S3 storage per title

  Encoding profiles per title:     ~120 (per-title optimization)
  Segments per profile:            ~300 (for a 2-hour movie with 4-sec segments)
  Average segment size:            ~2 MB
  Total segments per title:        120 x 300 = 36,000 segments
  Total storage per title:         36,000 x 2 MB = 72 GB transcoded output

  Netflix catalog:                 ~17,000 titles
  Total transcoded storage:        17,000 x 72 GB = ~1.2 PB

  Plus source masters:             17,000 x ~200 GB = ~3.4 PB
  Plus artwork, subtitles, audio:  ~500 TB

  Rough total S3 footprint:        ~5+ PB (just for current catalog)
  Historical/archived content:     Significantly more
```

### 5.3 S3 as CDN Origin

S3 serves as the origin for the Open Connect CDN. When an OCA (Open Connect Appliance) does not have a requested segment, it pulls from S3 (or a regional fill server backed by S3). The CDN fill process is described in the CDN deep dive, but the key storage insight is:

- **S3 read pattern:** Highly sequential, large objects, mostly during off-peak CDN fill. Not random access.
- **Storage class:** Active catalog in S3 Standard. Archived masters in S3 Glacier or S3 Glacier Deep Archive.
- **Cross-region replication:** S3 CRR to replicate transcoded segments to S3 buckets in multiple regions, so CDN fill traffic stays regional.

---

## 6. Amazon Aurora -- Relational Data for Billing & Accounts

### 6.1 Why Relational for Billing?

Billing and account management require properties that Cassandra cannot provide:

- **ACID transactions:** "Charge the credit card AND update subscription state" must be atomic. If the charge succeeds but the state update fails, the user either pays without access or gets free access.
- **Strong consistency:** A user who just paid must immediately see their subscription as active. Eventually consistent data here means support calls.
- **Complex queries:** Finance teams need ad-hoc SQL queries: "Revenue by region by plan tier by month." This is relational, not key-value.

### 6.2 Aurora Configuration

```
Amazon Aurora (MySQL-compatible) for Netflix Billing:

  Engine:              Aurora MySQL 3.x
  Instance class:      db.r6g.16xlarge (or similar large instances)
  Multi-AZ:            Yes (synchronous replication within region)
  Global Database:     Yes (cross-region async replication, <1 second lag)
  Read replicas:       Multiple per region (for finance reporting queries)
  Storage:             Aurora auto-scales up to 128 TB per cluster
  Encryption:          AES-256 at rest, TLS in transit
  Backup:              Continuous to S3 with PITR (point-in-time recovery)
```

### 6.3 Aurora Global Database

```
┌────────────────────────┐         ┌────────────────────────┐
│   Primary Region       │         │   Secondary Region     │
│   (us-east-1)          │ <1 sec  │   (eu-west-1)          │
│                        │ ──────> │                        │
│   Writer Instance      │  async  │   Reader Instances     │
│   + Reader Replicas    │  repl.  │   (read-only)          │
│                        │         │                        │
│   All writes happen    │         │   Serves billing       │
│   here.                │         │   reads for EU users.  │
│                        │         │   Promotes to writer   │
│                        │         │   during failover      │
│                        │         │   (RPO < 1 sec).       │
└────────────────────────┘         └────────────────────────┘
```

**Contrast with Cassandra:** Aurora uses a single-writer model (one primary region owns writes). This is the opposite of Cassandra's multi-writer, masterless approach. The trade-off is clear: strong consistency for billing at the cost of write availability in secondary regions. If us-east-1 goes down, Aurora must failover (seconds to minutes of write unavailability). For billing data, this is acceptable -- a brief payment processing delay is far better than a billing inconsistency.

---

## 7. Elasticsearch -- Search & Annotation Querying

### 7.1 Search Requirements

Netflix search must handle:

- **Multi-language queries:** Users search in their local language. "Stranger Things" in Japanese, Korean, Arabic.
- **Fuzzy matching:** Typo tolerance. "Stranegr Things" should still work.
- **Entity search:** Search for actors ("Ryan Reynolds"), directors ("Spielberg"), genres ("sci-fi thriller").
- **Personalized ranking:** Same query, different result ranking per user. A user who watches a lot of horror will see horror titles ranked higher for ambiguous queries.

### 7.2 Architecture

```
Search Pipeline:

  User types "stranger" in search box
       │
       v
  Client sends typeahead request: GET /search/suggestions?q=stranger
       │
       v
  Search Service (backend)
       │
       ├──> EVCache: Check if "stranger" results are cached for this region
       │         │
       │         ├── HIT: Return cached results (enriched with per-user ranking)
       │         │
       │         └── MISS:
       │                │
       │                v
       │         Elasticsearch query:
       │           - Multi-match across title, cast, director, genre fields
       │           - Fuzzy matching with edit distance 2
       │           - Boosted by popularity score
       │           - Filtered by region availability
       │                │
       │                v
       │         Raw results → Personalization re-ranking → Cache in EVCache
       │
       v
  Return top 10 suggestions to client
```

### 7.3 Marken Annotation Service

Elasticsearch also powers Netflix's **Marken** annotation service, which stores rich metadata annotations on content:

- Scene-level tags (violence level, nudity, language)
- Content descriptors for accessibility
- Mood and tone classifications
- Queryable by the editorial and content teams

---

## 8. Data Pipeline -- From Viewing Events to ML Models

### 8.1 Scale of Data

Netflix processes approximately **140 million hours of viewing data per day**. Every play, pause, seek, buffer, quality switch, and UI interaction generates events.

### 8.2 Pipeline Architecture

```
┌──────────────────────────────────────────────────────────────────────────────┐
│              Netflix Data Pipeline (Viewing Events to Insights)              │
│                                                                              │
│  ┌──────────┐                                                                │
│  │  Client   │ Viewing events: play, pause, seek, buffer, quality switch,   │
│  │  Devices  │ UI interactions, error reports                                │
│  │           │ Volume: ~1 trillion events/day                                │
│  └────┬─────┘                                                                │
│       │                                                                      │
│       v                                                                      │
│  ┌──────────┐                                                                │
│  │  Kafka    │ Central event bus                                             │
│  │           │ - Multiple clusters per region                                │
│  │           │ - Hundreds of topics                                          │
│  │           │ - Petabytes of daily throughput                               │
│  │           │ - Retention: 3-7 days for real-time topics                    │
│  └────┬─────┘                                                                │
│       │                                                                      │
│       ├──────────────────────────────────────────┐                           │
│       │                                          │                           │
│       v                                          v                           │
│  ┌──────────┐                              ┌──────────┐                      │
│  │  Flink   │ Real-time stream processing  │  Spark   │ Batch processing     │
│  │          │                              │          │                      │
│  │ Use cases:                              │ Use cases:                      │
│  │ - Real-time metrics                     │ - Daily recommendation          │
│  │   (current viewers,                     │   model retraining              │
│  │    buffer rate)                          │ - Content popularity            │
│  │ - Anomaly detection                     │   aggregation                   │
│  │   (sudden quality                       │ - A/B test result               │
│  │    degradation)                          │   analysis                      │
│  │ - Real-time                             │ - Financial reporting           │
│  │   personalization                       │                                │
│  │   signal updates                        │                                │
│  └────┬─────┘                              └────┬─────┘                      │
│       │                                         │                            │
│       v                                         v                            │
│  ┌──────────┐                              ┌──────────┐                      │
│  │ EVCache   │ Real-time features          │  Iceberg  │ Data warehouse       │
│  │ Cassandra │ pushed back to              │  on S3    │                      │
│  │           │ serving layer               │           │ - Petabytes of       │
│  │           │                             │           │   historical data    │
│  └──────────┘                              │           │ - Columnar format    │
│                                            │           │ - Schema evolution   │
│                                            │           │ - Time travel        │
│                                            │           │   queries            │
│                                            └────┬─────┘                      │
│                                                 │                            │
│                                                 v                            │
│                                            ┌──────────┐                      │
│                                            │  ML       │                     │
│                                            │ Training  │                     │
│                                            │           │                     │
│                                            │ - Recommendation models         │
│                                            │ - Artwork personalization       │
│                                            │ - Encoding optimization         │
│                                            │ - Content demand forecasting    │
│                                            └──────────┘                      │
└──────────────────────────────────────────────────────────────────────────────┘
```

### 8.3 Why Iceberg on S3?

Netflix adopted **Apache Iceberg** (which Netflix co-created) as its table format for the data warehouse, replacing Hive:

| Feature | Hive (legacy) | Iceberg (current) |
|---|---|---|
| Schema evolution | Breaking. Adding a column requires rewriting partitions. | Non-breaking. Add, drop, rename, reorder columns without rewrite. |
| Partition evolution | Requires rewriting entire dataset. | Change partitioning scheme without rewriting data. |
| Time travel | Not supported. | Query data as-of any snapshot. Roll back bad writes. |
| Hidden partitioning | User must know partition scheme in queries. | Partitioning is transparent. Queries don't need partition predicates. |
| File format | Typically ORC or Parquet. | Parquet (with Iceberg metadata layer on top). |
| Concurrency | Limited. Conflicts on metastore. | Optimistic concurrency with snapshot isolation. |

---

## 9. Netflix vs YouTube -- Storage Comparison

### 9.1 Fundamental Differences

Netflix and YouTube face fundamentally different storage challenges despite both being video platforms:

```
┌─────────────────────────────┬─────────────────────────┬─────────────────────────┐
│ Dimension                   │ Netflix                 │ YouTube                 │
├─────────────────────────────┼─────────────────────────┼─────────────────────────┤
│ Content volume              │ ~17,000 titles          │ 800M+ videos            │
│                             │ (curated catalog)       │ (user-generated)        │
├─────────────────────────────┼─────────────────────────┼─────────────────────────┤
│ Daily uploads               │ ~100 titles/week        │ 500+ hours/minute       │
│                             │ (professional ingest)   │ (millions of uploads)   │
├─────────────────────────────┼─────────────────────────┼─────────────────────────┤
│ Encoding depth per title    │ ~120 profiles           │ ~10-15 profiles         │
│                             │ (per-title optimized)   │ (fixed ladder)          │
├─────────────────────────────┼─────────────────────────┼─────────────────────────┤
│ Segments per title          │ ~36,000                 │ ~1,000-3,000            │
│                             │ (120 profiles x 300)    │ (fewer profiles)        │
├─────────────────────────────┼─────────────────────────┼─────────────────────────┤
│ Primary storage bottleneck  │ Segment management      │ Metadata & index scale  │
│                             │ and CDN fill            │ (800M+ video index)     │
├─────────────────────────────┼─────────────────────────┼─────────────────────────┤
│ Metadata DB                 │ Cassandra               │ Bigtable + Spanner      │
│                             │ (eventually consistent) │ (strongly consistent    │
│                             │                         │  where needed)          │
├─────────────────────────────┼─────────────────────────┼─────────────────────────┤
│ Object storage              │ Amazon S3               │ Google Colossus (GFS2)  │
│                             │ (AWS-native)            │ (Google-native)         │
├─────────────────────────────┼─────────────────────────┼─────────────────────────┤
│ Caching layer               │ EVCache (Memcached)     │ Multiple (in-process    │
│                             │ 400M ops/sec            │ + distributed + CDN)    │
├─────────────────────────────┼─────────────────────────┼─────────────────────────┤
│ CDN model                   │ Own CDN (Open Connect)  │ Google Global Cache     │
│                             │ ISP-embedded appliances │ + Google's private      │
│                             │                         │ backbone (B4)           │
├─────────────────────────────┼─────────────────────────┼─────────────────────────┤
│ Why consistency model       │ Bookmark off by 2 sec   │ Ad impression count     │
│ differs                     │ is invisible to user.   │ must be exact for       │
│                             │ Eventual is fine.       │ billing. Strong needed. │
└─────────────────────────────┴─────────────────────────┴─────────────────────────┘
```

### 9.2 The Core Trade-off

```
Netflix's challenge:
  Small catalog (17K titles) x Deep encoding (120 profiles) x Many segments (300 per profile)
  = Segment management complexity
  = CDN fill/warming is the bottleneck (pre-position 36K segments per title on 18K+ OCAs)

YouTube's challenge:
  Enormous catalog (800M+ videos) x Shallow encoding (10-15 profiles) x Moderate segments
  = Metadata index scale
  = Serving long-tail content efficiently (most videos have < 100 views)
  = Cannot afford per-title encoding optimization at this volume
```

**Interview talking point:** "Netflix and YouTube optimize for opposite ends of the content spectrum. Netflix invests compute budget in encoding quality (120 profiles per title) because they have 17K titles viewed billions of times -- the amortized cost of better encoding is huge. YouTube invests in encoding speed and metadata scale because they ingest 500 hours of video per minute and most of it is long-tail content where per-title optimization would never pay back its compute cost."

---

## 10. Interview Cheat Sheet

### 10.1 One-Sentence Summaries

| Component | One-Sentence Summary |
|---|---|
| **Cassandra** | Masterless, eventually consistent NoSQL database storing 98% of streaming data (profiles, history, bookmarks) across tens of thousands of nodes with tunable consistency. |
| **EVCache** | Memcached-based distributed cache with zone-aware replication serving 400M ops/sec at sub-millisecond latency, absorbing 95%+ of read traffic before it reaches Cassandra. |
| **S3** | Object storage for all video assets -- source masters (hundreds of GB each) and transcoded segments (tens of thousands of objects per title) -- serving as the CDN origin. |
| **Aurora** | MySQL-compatible relational database for billing and accounts, providing ACID transactions and strong consistency for financial data with <1 sec cross-region replication. |
| **Elasticsearch** | Full-text search engine powering multi-language, fuzzy-matching title/person search with personalized result ranking. |
| **Data Pipeline** | Kafka -> Flink (real-time) / Spark (batch) -> Iceberg on S3 (warehouse) -> ML training. Processes 140M hours of viewing data per day. |

### 10.2 Key Numbers to Memorize

```
Cassandra:    Hundreds of clusters, tens of thousands of nodes, petabytes, millions TPS
EVCache:      200 clusters, 22K instances, 400M ops/sec, 14.3 PB, 2 trillion items
S3 per title: ~120 profiles x ~300 segments = ~36,000 objects, ~72 GB transcoded
Aurora:       <1 sec cross-region replication, ACID for billing
Pipeline:     140M hours of viewing data/day, ~1 trillion events/day
```

### 10.3 Common Interview Questions

**Q: "Why not just use Redis for caching?"**
A: Netflix's workload is >99% simple get/set of serialized blobs. Memcached's multi-threaded architecture gives better per-instance throughput for this pattern. Redis's data structures add memory overhead for features Netflix doesn't use. EVCache's zone-aware replication was custom-built at the client layer because no off-the-shelf solution provided the multi-AZ write fan-out semantics Netflix needed.

**Q: "Why Cassandra instead of DynamoDB?"**
A: Netflix started migrating to the cloud in 2008-2011. DynamoDB launched in 2012 and was immature. Cassandra gave Netflix full control over tuning, topology, and replication -- critical when you are running one of the largest streaming platforms in the world. Today, Netflix continues to invest in Cassandra because they have deep operational expertise and custom tooling (Priam for backup, Dynomite for proxy layer).

**Q: "How do you handle cache stampede?"**
A: When a popular cache key expires and thousands of requests simultaneously miss the cache and hit Cassandra, that is a stampede. Netflix handles this with: (1) request coalescing -- only one request fetches from DB, others wait for the result; (2) jittered TTLs -- add random jitter to TTLs so not all keys for the same data category expire simultaneously; (3) cache warming -- proactively refresh popular keys before TTL expiry.

**Q: "Why not a single database for everything?"**
A: Polyglot persistence. No single database excels at all access patterns. Cassandra is great for high-throughput, eventually consistent key-value writes but terrible for ad-hoc SQL reporting. Aurora is great for ACID transactions but cannot scale to millions of TPS for simple reads. Elasticsearch is great for full-text search but terrible for transactional writes. Each database handles the workload it was designed for.

---

*Next: [09-fault-tolerance-and-resilience.md](09-fault-tolerance-and-resilience.md) -- Chaos engineering, circuit breakers, and how Netflix stays up.*
