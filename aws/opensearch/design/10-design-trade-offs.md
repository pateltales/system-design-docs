# 10 — Design Trade-Offs: The Opinionated Choices Behind OpenSearch

## Introduction

Every distributed system is a collection of trade-off decisions. There is no "best" architecture — only architectures that are best for a particular set of constraints. OpenSearch (and Elasticsearch before the fork) made deliberate choices about data structures, consistency, schema flexibility, and operational complexity. Each choice optimized for one axis and paid a price on another.

Understanding WHY OpenSearch chose X over Y is what separates L5 from L7 in system design interviews. An L5 says "OpenSearch uses inverted indices." An L7 says "OpenSearch chose inverted indices over B-trees because full-text search requires token-level lookup at sub-millisecond latency, and that choice is why point lookups by arbitrary field are slow — which is exactly why we pair it with a primary datastore."

This document covers 10 core design trade-offs. For each one: what was chosen, what was sacrificed, why, and when you would choose differently.

---

## Trade-Off 1: Inverted Index vs B-Tree

### What OpenSearch Chose

Inverted index: a mapping from every unique term to the list of documents containing that term (the posting list).

```
Term Dictionary          Posting Lists
───────────────          ─────────────────────
"database"          →    [doc1, doc4, doc7]
"distributed"       →    [doc1, doc2, doc7]
"search"            →    [doc2, doc3, doc4]
"engine"            →    [doc3, doc5]
```

A query for "distributed database" intersects the posting lists for both terms: `[doc1, doc7]`. This is an O(n) merge of sorted integer lists — fast, parallelizable, and cache-friendly.

### What It Sacrificed

- Efficient point lookups by arbitrary field (O(1) by primary key)
- Ordered range scans ("give me all rows where price BETWEEN 100 AND 200" without scanning posting lists)
- In-place updates (inverted indices are append-only; updating a field means reindexing the entire document)

### Why This Choice Was Made

Full-text search fundamentally requires token-level lookup. When a user searches "distributed database," the engine must find every document containing both terms, compute relevance scores (BM25), and rank results — all in milliseconds. B-trees are optimized for ordered key ranges, not for "which documents contain this word?"

An inverted index answers "which documents match these terms?" in O(k) where k is the number of posting list entries. A B-tree would require scanning every document's text field — O(N) where N is the total number of documents.

The inverted index also naturally supports:
- **BM25 scoring** — term frequency is stored in the posting list, document frequency is a metadata lookup. Relevance ranking comes for free.
- **Boolean logic** — AND = list intersection, OR = list union. These operations on sorted integer lists are trivially parallelizable.
- **Phrase queries** — position data stored alongside document IDs enables "find documents where 'connection' appears immediately before 'timeout'."

### Characteristic Comparison

| Dimension | Inverted Index | B-Tree |
|-----------|---------------|--------|
| Optimized for | Token lookup, full-text search | Range scans, point lookups |
| Query: "find docs containing word X" | O(posting list size) | O(N) full scan |
| Query: "find row where id = 42" | Possible but not primary use | O(log N) |
| Query: "all rows where price 100-200" | Requires doc values (columnar) | O(log N + result size) |
| Write pattern | Append to segment, merge later | In-place update |
| Concurrency | Lock-free reads (immutable segments) | Page-level locking |
| Storage | Compressed posting lists + term dictionary | Sorted pages |
| Relevance scoring | Native (TF-IDF, BM25) | Not supported |
| Used by | OpenSearch, Solr, Lucene | PostgreSQL, MySQL, DynamoDB |

### When You Would Choose a B-Tree Instead

- **OLTP workloads**: user profiles, order management, anything with primary key lookups
- **Ordered range scans**: "all orders from March" where rows are sorted by date
- **High update rates**: bank balances, inventory counts that change per-transaction
- **Point lookups**: "get user by ID" — B-tree gives O(log N), inverted index is overkill
- **JOINs**: B-tree indexes enable nested-loop and hash joins across tables. OpenSearch has no real join support

### The Hybrid Reality

OpenSearch does not purely rely on inverted indices. **Doc values** provide columnar storage for sorting, aggregations, and scripting. This is essentially a column-oriented structure alongside the inverted index — OpenSearch acknowledged that an inverted index alone is not enough for analytics. **BKD trees** handle numeric and geo-point range queries efficiently.

**Interview angle**: "OpenSearch chose inverted indices for search, but bolted on doc values for analytics. If the primary workload were OLAP, I'd start with a columnar store like ClickHouse and add search as a secondary concern."

---

## Trade-Off 2: Immutable Segments vs In-Place Updates

### What OpenSearch Chose

Lucene writes data into immutable segments. Once a segment is written, it never changes. Updates are implemented as a soft delete of the old document plus insertion of a new document into a new segment. Periodically, segments are merged: live documents are copied into a new, larger segment, and the old segments (with their tombstoned deletes) are discarded.

```
Write path:

  t=0  Index doc1 (v1)  → Segment A  [doc1-v1]           (immutable)
  t=1  Update doc1 (v2) → Segment B  [doc1-v2]           (immutable)
                           Segment A  [doc1-v1 DELETED]   (tombstone added to .del file)
  t=2  Merge triggered  → Segment C  [doc1-v2]           (new immutable segment)
                           Segments A, B discarded
```

### What It Sacrificed

- **In-place update efficiency**: every update rewrites the entire document, even if only one field changed
- **Write amplification**: data is written once to the translog, once to a segment, then again during merges — 3x or more amplification
- **Immediate consistency**: the updated document is not searchable until the next refresh
- **Disk space**: tombstoned documents consume space until merge completes. A system with a 50% update rate can temporarily double its disk usage

### Why This Choice Was Made

Immutability buys three critical properties:

1. **Lock-free concurrent reads**: No reader ever sees a partially written document. Segments are either fully visible or not. No read locks, no MVCC versioning, no snapshot isolation complexity. A search thread opens a point-in-time view of the segment list and reads without coordination.

2. **OS page cache efficiency**: Immutable files are perfect for `mmap`. The OS can cache them aggressively because they never change. No cache invalidation logic. An immutable file has exactly one version — the OS page cache, OpenSearch's field data cache, and any reverse proxy can cache it indefinitely.

3. **Trivial crash recovery**: If the process crashes mid-write, the incomplete segment is simply discarded. Committed segments are intact because they were never modified. The translog replays any unfinished operations. Compare this to a B-tree, which needs write-ahead logging and page-level recovery to handle a crash during an in-place update.

4. **Aggressive compression**: Knowing that data will not change allows Lucene to use techniques like FOR (Frame of Reference) encoding for posting lists, which compresses sorted integers by storing deltas.

### The Cost in Numbers

| Operation | Mutable Store (PostgreSQL) | Immutable Segments (OpenSearch) |
|-----------|---------------------------|-------------------------------|
| Update 1 field in 1 doc | Write ~100 bytes (the field + WAL) | Reindex entire doc (~KB), later merge |
| Write amplification | ~1-2x (WAL + heap) | ~3-5x (translog + segment + merge) |
| Read contention | Row-level locks (MVCC) | Zero (immutable = lock-free) |
| Crash recovery | Replay WAL, check page consistency | Discard incomplete segment, replay translog |
| Concurrent readers | Thousands (MVCC snapshots) | Unlimited (immutable files, no coordination) |
| Disk overhead during updates | ~1.2x (dead tuples until VACUUM) | ~2x (tombstones until merge) |

### When You Would Choose Mutable Storage

- **High update-rate OLTP**: e-commerce inventory, user session state, anything with frequent field-level updates
- **PostgreSQL's MVCC**: gives you mutable rows with snapshot isolation — better for update-heavy workloads
- **Update-to-read ratio > 1:10**: the write amplification of immutable segments becomes painful
- **Real-time bidding platforms**: bid prices update thousands of times per second per item. Immutable segments would generate enormous merge pressure

**Interview angle**: "Immutability is the right call for read-heavy search workloads. But if an interviewer describes a system with frequent per-field updates — like a real-time bidding platform updating bid prices — I'd push back on OpenSearch and recommend a mutable store with a search index as a derived view."

---

## Trade-Off 3: Near-Real-Time (NRT) vs Real-Time Search

### What OpenSearch Chose

After a document is indexed, it is not immediately searchable. It sits in an in-memory buffer until the next **refresh** operation (default: every 1 second). The refresh writes the buffer into a new Lucene segment and opens it for search. This is Near-Real-Time search — documents become searchable within ~1 second, not instantly.

```
Timeline:

  t=0.0s  Document indexed
          Written to: translog (durable) + in-memory buffer (not searchable)

  t=0.0s  GET /index/_doc/123       → 200 OK  (reads translog directly, real-time)
  t=0.0s  GET /index/_search?q=...  → NOT FOUND (segment not yet created)

  t=1.0s  Refresh fires:
          In-memory buffer → new Lucene segment → opened for search

  t=1.0s  GET /index/_search?q=...  → FOUND
```

### What It Sacrificed

Instant searchability. There is a window (up to `refresh_interval`) where a document exists in the system but cannot be found by search queries. This creates a subtle asymmetry: GET by _id is real-time (reads the translog), but search is NRT.

### Why This Choice Was Made

Creating a new Lucene segment for every single document would be catastrophically expensive. Each segment creation involves:
- Sorting and compressing posting lists
- Building the term dictionary (FST — finite state transducer)
- Writing to disk (or at least mmap)
- Opening a new IndexSearcher (re-reading segment metadata)

At 100,000 documents per second, batching into 1-second windows means one segment creation per second instead of 100,000. This is a 100,000x reduction in segment overhead.

### The refresh_interval Knob

| Setting | Use Case | Rationale |
|---------|----------|-----------|
| `1s` (default) | General search applications | Good balance of freshness and performance |
| `200ms` | Near-real-time dashboards | Tighter freshness at higher CPU cost; only if sub-second visibility is required |
| `5s` - `30s` | Log ingestion, bulk analytics | Logs rarely need sub-second searchability; longer intervals = fewer segments = less merge overhead |
| `-1` (disabled) | Bulk loading, reindexing | Disable refresh entirely during initial load. Manually refresh after bulk completes. Can improve bulk indexing throughput by 50%+ |

### When You Would Choose True Real-Time

- **Financial trading systems**: a trade must be immediately queryable for compliance and risk management
- **Chat applications**: a sent message must appear instantly in search results
- **Collaborative editing**: changes must be visible to all participants immediately
- **Inventory / booking systems**: "Is this seat available?" must reflect the absolute latest state

In these cases, use a database with synchronous index updates (PostgreSQL with GIN index, or a purpose-built system) rather than trying to force OpenSearch into a real-time role.

### The GET vs Search Asymmetry

This is a subtle but important point that interviewers love:

```
GET by _id:  Client → Coordinating Node → Primary/Replica Shard
                                           ↓
                                        Check translog first (in-memory, real-time)
                                        Then check segments (on-disk)
                                        → Always returns latest version

Search:      Client → Coordinating Node → Fan out to all shards
                                           ↓
                                        Each shard searches its segments only
                                        Translog is NOT searched
                                        → May miss documents written since last refresh
```

**Interview angle**: "The 1-second NRT gap is a feature, not a bug. If someone tells me they need sub-100ms search latency after write, I'd ask whether they actually need search or just need a lookup by ID — because GET by _id bypasses the refresh gap entirely by reading the translog. If they truly need real-time search, OpenSearch is the wrong tool."

---

## Trade-Off 4: Fixed Shard Count vs Dynamic Resharding

### What OpenSearch Chose

When you create an index, you declare the number of primary shards. That number is **fixed for the lifetime of the index**. Documents are routed to shards via `hash(_id) % num_shards`. This routing formula means the shard count cannot change without invalidating every document's location.

```
Index: products (5 primary shards)

  Document _id="abc"  →  hash("abc") % 5 = 2  →  Shard 2
  Document _id="xyz"  →  hash("xyz") % 5 = 0  →  Shard 0

  If you change to 6 shards:
    hash("abc") % 6 = 3  →  Shard 3  (WRONG — document is on Shard 2)
    Every document's location is now invalid
```

### What It Sacrificed

Elastic scaling without reindexing. If you chose 5 shards and your data grows 100x, you cannot simply add shards. You must create a new index with more shards and reindex all data into it.

### Why This Choice Was Made

Fixed shard counts buy three things:

1. **Deterministic routing**: Any node can compute which shard owns a document using `hash(_id) % num_shards` — pure math, no routing table lookup, no coordination, no consensus protocol.

2. **No data movement**: Adding a shard to a DynamoDB table triggers automatic data redistribution across partitions. OpenSearch avoids this entirely — shards are moved between nodes, but data within shards never moves.

3. **Predictable query fan-out**: The coordinator knows exactly how many shards to query. No dynamic discovery, no shard splits happening mid-query.

### The Cost of Getting It Wrong

```
Under-sharding (too few):
  3 shards, data grows to 150 GB  →  50 GB per shard
  Problems:
    - Single shard becomes bottleneck for both indexing and search
    - Recovery after node failure: restoring a 50 GB shard takes hours
    - Cannot parallelize queries beyond 3 threads per index
    - Merge operations on 50 GB segments are CPU-intensive

Over-sharding (too many):
  1000 shards, data is only 10 GB  →  10 MB per shard
  Problems:
    - Each shard costs ~10-50 MB heap on the cluster manager node
    - 1000 shards × 50 MB = 50 GB cluster state overhead
    - Query fan-out to 1000 shards: coordinator merges 1000 responses
    - File handle exhaustion: each shard has ~50 file handles
    - Multiply by daily indices: 365 days × 1000 shards = 365,000 shards
```

### Sizing Guidelines

| Cluster Size | Shard Size Target | Rule of Thumb |
|-------------|-------------------|---------------|
| Any | 10-50 GB per shard | Official recommendation |
| Any | < 20 shards per GB of JVM heap | Cluster state constraint |
| Time-series | Daily rollover at ~50 GB/day/shard | Use ISM rollover policies |
| Small dataset (< 10 GB) | 1 primary shard | Over-sharding small data is the #1 beginner mistake |
| Large dataset (> 1 TB) | 20-30 shards | Target 30-50 GB per shard |

### Workarounds

| Approach | What It Does | Limitations |
|----------|-------------|-------------|
| **Rollover API** | Creates new index when size/age/doc count threshold is met | Requires aliases; old indices retain original shard count |
| **Split API** | Doubles (or multiplies) shard count by splitting existing shards | Requires `index.number_of_routing_shards` set at creation time |
| **Shrink API** | Reduces shard count by merging shards | Must relocate all shards to one node first; target must be a factor of source |
| **Reindex API** | Full copy from old index to new index with different settings | Most flexible, most expensive; can take hours on large indices |

### When You Would Choose Dynamic Resharding

- **DynamoDB**: Automatically splits partitions when they get hot or exceed 10 GB. Zero operator intervention. Choose this when data growth is unpredictable.
- **Cassandra**: Virtual nodes (vnodes) enable gradual rebalancing when nodes are added. Choose this for write-heavy workloads with organic growth.
- **CockroachDB**: Automatic range splitting at 512 MB. Choose this when you need ACID + automatic scaling.

**Interview angle**: "I'd set shard count based on expected data volume at 6-12 months, targeting 30-50 GB per shard. For time-series data, I'd use rollover indices with ISM policies so each day/week gets a right-sized index. If the interviewer asks what the hardest operational challenge with OpenSearch is, this is the answer — fixed shard count requires upfront capacity planning that is often wrong."

---

## Trade-Off 5: Schemaless (Dynamic Mapping) vs Strict Schema

### What OpenSearch Chose

By default, OpenSearch uses **dynamic mapping**: when a document is indexed with a field that does not exist in the mapping, OpenSearch auto-detects the field type and adds it. A string that looks like a date becomes `date`. A string that does not becomes both `text` (for full-text search) and `keyword` (for exact match). A number becomes `long` or `float`.

```json
// Index this document with no prior mapping:
PUT /my-index/_doc/1
{ "name": "Alice", "age": 30, "signup_date": "2026-01-15" }

// OpenSearch auto-creates this mapping:
{
  "name":        { "type": "text", "fields": { "keyword": { "type": "keyword" } } },
  "age":         { "type": "long" },
  "signup_date": { "type": "date" }
}
```

### What It Sacrificed

- **Type safety**: the first document to use a field name determines its type for all future documents. If the first log line has `{"status": "200"}` (string) and the next has `{"status": 404}` (integer), the second fails to index.
- **Protection against mapping explosion**: any document can introduce new fields without limit
- **Predictable storage**: auto-detected types may not be optimal (e.g., storing a numeric ID as `text` + `keyword` wastes disk and indexing CPU)

### Why This Choice Was Made

Developer experience. You can `PUT /my-index/_doc/1 { "name": "test", "count": 42 }` without defining a schema first. This is powerful for prototyping, log ingestion (where log formats evolve), and onboarding new users. JSON in, search works. No DDL required.

### The Danger: Mapping Explosion

```
Scenario: An application logs arbitrary key-value pairs as field names

  Day 1:   { "user.preference.theme": "dark" }             →  1 field
  Day 2:   { "user.preference.language": "en" }             →  2 fields
  Day 30:  { "user.preference.<1000 unique keys>": "..." }  →  1000 fields

  Each field consumes:
    - ~1-5 KB of heap for the mapping metadata
    - Inverted index entry (term dictionary + posting list)
    - Doc values column
    - Stored fields entry
    - Cluster state size (mappings replicated to every node)

  At ~10,000 fields:  cluster performance degrades noticeably
  At ~50,000 fields:  mapping updates become slow, queries timeout
  At ~100,000 fields: cluster may become unstable, master node overwhelmed
```

### Mapping Strategies

| Strategy | Setting | Behavior | Use Case |
|----------|---------|----------|----------|
| Dynamic (default) | `"dynamic": true` | Auto-detect and index new fields | Dev/prototype only |
| Runtime | `"dynamic": "runtime"` | New fields queryable but not indexed | Exploration without mapping growth |
| False | `"dynamic": false` | Accept documents, ignore unknown fields silently | Log ingestion (store raw JSON but don't index every field) |
| Strict | `"dynamic": "strict"` | Reject documents with unmapped fields | Production (the correct default) |

### The Irony

The default (dynamic mapping) is wrong for production. This is one of the few cases where OpenSearch's default configuration actively harms users who do not know to change it. Every production deployment should use `strict` mappings with explicit field definitions.

```json
// What you should always do in production:
{
  "mappings": {
    "dynamic": "strict",
    "properties": {
      "title":      { "type": "text", "analyzer": "standard" },
      "price":      { "type": "float" },
      "category":   { "type": "keyword" },
      "created_at": { "type": "date" }
    }
  }
}
```

### When You Would Choose Strict Schema From the Start

- Any production deployment (which means: always in practice)
- Multi-tenant systems where tenants might send arbitrary fields
- Compliance environments where field types must be documented
- High-cardinality field names (user-generated keys, metric names)

**Interview angle**: "I'd always use strict mappings in production. Dynamic mapping is a footgun — it optimizes for the first 5 minutes of development and creates operational debt for the next 5 years. The one exception is a pure log exploration cluster where you set `dynamic: runtime` to allow querying without indexing unknown fields."

---

## Trade-Off 6: Denormalized Documents vs Normalized/Relational

### What OpenSearch Chose

OpenSearch stores flat, self-contained JSON documents. There are no JOINs across indices. Each document must contain all the data needed to search and display it. If a product belongs to a category, the category name is stored inside the product document, not in a separate category index referenced by ID.

```
Normalized (relational):                Denormalized (OpenSearch):

  orders table:                          order document:
  ┌────────┬─────────────┐               {
  │ id     │ customer_id │                 "order_id": 1,
  │ 1      │ 42          │                 "customer_name": "Alice",
  └────────┴─────────────┘                 "customer_email": "alice@co.com",
                                           "product_name": "Widget",
  customers table:                         "product_price": 29.99,
  ┌────────┬─────────┬──────────────┐      "quantity": 3
  │ id     │ name    │ email        │    }
  │ 42     │ Alice   │ alice@co.com │
  └────────┴─────────┴──────────────┘  Everything in one document.
                                       No joins needed.
  products table:                      One shard, one read, one response.
  ┌────────┬────────┬───────┐
  │ id     │ name   │ price │
  │ 7      │ Widget │ 29.99 │
  └────────┴────────┴───────┘
```

### What It Sacrificed

- **Storage efficiency**: customer name and email are duplicated across every order document for that customer. If Alice has 1,000 orders, her name is stored 1,000 times.
- **Update consistency**: If Alice changes her email, every order document containing her old email must be updated (reindexed). With 1,000 orders, that is 1,000 reindex operations.
- **Data integrity**: No foreign key constraints — nothing prevents an order from referencing a non-existent customer.

### Why This Choice Was Made

JOINs are expensive in distributed systems. A JOIN between two indices would require:

1. Query shard A of index 1 to find matching documents
2. Extract the join key from each result
3. Fan out to all shards of index 2 to find matching documents by those keys
4. Merge results across shards and indices
5. Compute relevance scores across the joined result set

This is cross-shard, cross-index coordination that kills latency. Denormalization ensures that answering a query requires hitting only the shards of a single index — the data is already pre-joined at ingest time.

### Limited JOIN Alternatives in OpenSearch

| Approach | How It Works | Performance Cost | Practical Limits |
|----------|-------------|-----------------|-----------------|
| **Nested fields** | Objects stored as hidden sub-documents in the same Lucene segment | 2-5x slower queries; each nested object is a hidden Lucene document | Hundreds of nested objects per document |
| **Parent-child (join field)** | Parent and child documents colocated on the same shard | Significant query overhead; routing must ensure colocation | Moderate relationship complexity |
| **Application-side join** | Query index A, extract IDs, query index B | Two round trips; no cross-index relevance scoring | Any scale, but latency compounds |
| **Denormalize at ingest** | Flatten all related data before indexing | Storage cost; update complexity | Best option for most cases |

### When You Would Choose Normalized/Relational

- **Complex relationships**: social graphs, organizational hierarchies, many-to-many relationships
- **High update frequency on shared entities**: if customer email changes hourly, denormalization creates a reindexing nightmare
- **Strong consistency requirements**: foreign key constraints, cascading updates, ACID transactions
- **Storage-sensitive environments**: denormalization can 10x storage for highly relational data

The canonical architecture: use PostgreSQL or MySQL as the source of truth (normalized). Feed OpenSearch as a derived, denormalized search index via Change Data Capture (CDC) — Debezium, DMS, or a custom pipeline.

```
Source of Truth (PostgreSQL)                 Search Index (OpenSearch)
┌──────────────────────┐                     ┌──────────────────────────┐
│ orders               │                     │ orders-search            │
│ customers            │  ──CDC pipeline──>  │ (denormalized documents) │
│ products             │  (Debezium/DMS)     │                          │
│ (normalized, ACID)   │                     │ (flat JSON, fast search) │
└──────────────────────┘                     └──────────────────────────┘
```

**Interview angle**: "I'd denormalize at ingest time using a CDC pipeline from the relational source of truth. The relational database owns correctness; OpenSearch owns search performance. If the interviewer asks about nested objects, I'd warn that each nested object is a hidden Lucene document — it's easy to accidentally create millions of hidden docs that degrade query performance."

---

## Trade-Off 7: Eventual Consistency vs Strong Consistency

### What OpenSearch Chose

OpenSearch provides a spectrum of consistency, not a single guarantee:

- **Search queries**: Eventual consistency. Documents are searchable only after a refresh (default 1 second). Even after refresh, different replicas may have different refresh states.
- **GET by _id**: Near-real-time. Reads the translog directly, bypassing the refresh gap.
- **Write durability**: Tunable via `wait_for_active_shards`. Default is 1 (only the primary must acknowledge). Can be set to `all` for synchronous replication.

```
Consistency spectrum in OpenSearch:

  Weak ◄──────────────────────────────────────────────► Strong

  Search after write      GET by _id        wait_for_active_shards=all
  (NRT, ~1s gap)         (translog read)    + refresh=wait_for
                                            (synchronous, expensive)

  ◄── Default behavior                         Must opt in ──►
```

### What It Sacrificed

- **Read-your-write guarantee for search**: you index a document and immediately search for it — it might not be there yet
- **Linearizability**: two clients reading the same index may see different results if they hit different replicas at different refresh states
- **Cross-index transactions**: no way to atomically write to two indices. No distributed transactions

### Why This Choice Was Made

Strong consistency for every search query would require:

1. Synchronous refresh on all replicas after every write (or batch)
2. A consensus protocol (Paxos/Raft) to ensure all replicas agree on segment visibility
3. Blocking reads until the refresh completes

This would turn every write into a synchronous, multi-node coordination event. For a system designed to handle tens of thousands of writes per second while serving thousands of concurrent search queries, this is untenable. Write throughput would drop by 10-100x.

More fundamentally: **search is not a source of truth**. The canonical data lives in a database. OpenSearch is a derived, denormalized copy. Given this role, staleness is acceptable — the database has the correct data for critical operations.

### Consistency Controls

| Mechanism | What It Controls | Options | Performance Impact |
|-----------|-----------------|---------|-------------------|
| `wait_for_active_shards` | How many shard copies must acknowledge a write | `1` (default), `2`, `all` | Higher = more durable, slower writes |
| `refresh` param on index | When the doc becomes searchable | `false` (default), `true` (immediate), `wait_for` (block until next refresh) | `wait_for` adds up to refresh_interval latency |
| `_preference` on search | Which shard copy to query | `_primary`, `_local`, custom string | `_primary` gives freshest results |
| Translog durability | When translog is fsync'd to disk | `request` (every op, default), `async` (every 5s) | `async` = faster writes, risk of 5s data loss on crash |

### The GET vs Search Consistency Asymmetry

```
Index a document:
  PUT /orders/_doc/123 { "status": "shipped" }

Immediately after:
  GET /orders/_doc/123          → 200 OK, returns document  (reads translog)
  GET /orders/_search?q=shipped → May return 0 results      (segment not refreshed)

This asymmetry is by design:
  - GET by _id is for "I know the document ID, give me the latest version" → real-time
  - Search is for "find documents matching criteria" → NRT, optimized for throughput
```

### When You Would Choose Strong Consistency

- **Financial transactions**: account balance must be immediately consistent after transfer
- **Inventory / booking**: showing 1 seat available when 0 remain causes overbooking
- **Collaborative editing**: users must see each other's changes immediately
- **Source-of-truth workloads**: any system where OpenSearch is the primary store (an anti-pattern)

For these, use PostgreSQL with serializable isolation, CockroachDB, or Google Spanner. If you also need search, feed OpenSearch from the strongly consistent source via CDC and accept the propagation delay.

**Interview angle**: "OpenSearch gives you tunable consistency, not strong consistency. If the interviewer's use case requires read-your-write for search, I'd ask whether GET by _id (which is real-time) solves the problem. If not, I'd add a caching layer or use `refresh=wait_for` on critical writes — but I'd flag the throughput cost. The deeper answer: OpenSearch is eventually consistent because it is not a source of truth — it is a search index derived from a consistent store."

---

## Trade-Off 8: Managed Service (AWS) vs Self-Managed

### What OpenSearch Chose to Offer

Both. OpenSearch is open-source (Apache 2.0), so anyone can run it. AWS also offers Amazon OpenSearch Service (managed) and OpenSearch Serverless (fully serverless). AWS's business incentives push heavily toward managed.

### What You Sacrifice With Managed (AWS OpenSearch Service)

| Dimension | Limitation |
|-----------|-----------|
| OS-level access | No SSH, no kernel tuning, no custom JVM flags beyond what the console exposes |
| Plugin freedom | Only AWS-approved plugins. No custom Lucene codecs, no experimental plugins |
| Version control | AWS decides when versions are available; lag behind open-source releases |
| Network topology | VPC-only by default; specific AZ placement rules apply |
| Configuration tuning | Cannot change cluster manager election timeout, some circuit breaker thresholds, or thread pool sizes beyond console options |
| Upgrade timing | Blue/green upgrades are managed but you cannot fully customize the process |
| Cost transparency | Opaque pricing per instance-hour; hard to optimize beyond instance type selection |

### What You Sacrifice With Self-Managed

| Dimension | Burden |
|-----------|--------|
| Upgrades | Rolling upgrades across dozens of nodes, testing compatibility, rollback planning |
| Security patches | You must monitor CVEs and apply patches. AWS does this automatically |
| Monitoring | You build your own monitoring stack (or use the cluster to monitor itself — circular) |
| Scaling | Manual capacity planning, node addition, shard rebalancing |
| Backups | Configure and test snapshot/restore to S3 yourself |
| Availability | You design multi-AZ topology, handle node failures, manage master elections |
| Security | Configure TLS certificates, RBAC, network policies, encryption at rest |

### Decision Framework

| Factor | Choose Managed | Choose Self-Managed |
|--------|---------------|-------------------|
| Team size | < 10 engineers | Dedicated platform/infra team (3+ people) |
| OpenSearch expertise | Limited or none | Deep operational experience |
| Compliance | Standard (SOC2, HIPAA via AWS) | Exotic requirements (air-gapped, custom encryption, FedRAMP) |
| Plugin needs | Standard plugins sufficient | Custom analyzers, plugins, Lucene codecs |
| Cluster count | 1-5 clusters | 10+ clusters (tooling amortizes) |
| Scale | < 20 nodes | 20+ nodes (cost savings significant) |
| Cloud | AWS | Multi-cloud or on-premises |

### Cost Break-Even Analysis

```
Managed (AWS OpenSearch Service):
  r6g.xlarge (4 vCPU, 32 GB) ≈ $0.335/hr ≈ $245/month per node
  10-node cluster ≈ $2,450/month (compute only)

Self-managed (EC2):
  r6g.xlarge ≈ $0.201/hr ≈ $147/month per node
  10-node cluster ≈ $1,470/month (compute only)
  + Engineer time: ~$1,000-2,000/month (fractional, for a skilled team)
  Total: ~$2,470-3,470/month

Break-even point: approximately 10-15 nodes

  Below 10 nodes:  Managed is cheaper (engineer time dominates)
  10-20 nodes:     Roughly equivalent
  Above 20 nodes:  Self-managed saves 20-30%
  Above 50 nodes:  Self-managed clearly wins — savings of $5,000-10,000+/month
```

These numbers are approximate and vary by region, instance type, and reserved instance pricing. The point is directional: small clusters favor managed, large clusters favor self-managed.

### OpenSearch Serverless: The Third Option

| Dimension | OpenSearch Service (Managed) | OpenSearch Serverless |
|-----------|----------------------------|---------------------|
| Pricing model | Per instance-hour | Per OCU-hour (compute) + per-GB storage |
| Scaling | Manual (change instance count/type) | Automatic (transparent) |
| Minimum cost | ~$100/month (smallest instance) | ~$350/month (minimum 4 OCUs for indexing + search) |
| Configuration | Instance types, node count, AZ, storage | Almost none (collection-level settings only) |
| Best for | Predictable, steady workloads | Spiky, unpredictable, or low-traffic workloads |
| Index management | Full control (mappings, settings, ILM) | Limited (no custom index settings) |

**Interview angle**: "For a startup or team without deep OpenSearch expertise, I'd go managed without hesitation. The operational burden of self-managing a distributed system is the fastest way to burn engineering cycles that should go toward product development. I'd only self-manage at scale (50+ nodes) with a dedicated platform team that already has Lucene/search expertise."

---

## Trade-Off 9: OpenSearch vs Elasticsearch (The Fork Decision)

### What Happened

In January 2021, Elastic changed Elasticsearch's license from Apache 2.0 to SSPL (Server Side Public License) + Elastic License. SSPL effectively prevents cloud providers from offering Elasticsearch as a managed service without open-sourcing their entire management stack. AWS responded by forking Elasticsearch 7.10.2 (the last Apache 2.0 version) and creating OpenSearch under the Linux Foundation.

```
Timeline:

  2010     Elasticsearch created by Shay Banon (Apache 2.0)
  2012     Elastic company founded
  2015     AWS launches Amazon Elasticsearch Service (managed)
  2019     Elastic introduces Elastic License for X-Pack features
  2021 Jan Elastic changes CORE license to SSPL — the breaking point
  2021 Apr AWS forks ES 7.10.2 → OpenSearch 1.0 (Apache 2.0)
  2021 Jul OpenSearch 1.0 GA release
  2022-24  Feature sets diverge significantly
  2024-26  Independent ecosystems, separate plugin marketplaces
```

### What OpenSearch Gained

- **True open-source license** (Apache 2.0) — anyone can use, modify, embed, and offer as a service
- **AWS-native integrations** — CloudWatch, IAM, VPC, S3 snapshots, UltraWarm/Cold storage as first-class features
- **Independent roadmap** — security analytics, observability features developed by AWS + community
- **Vendor-neutral governance** — Linux Foundation, not controlled by a single company
- **Security features built-in and free** — the former Open Distro security plugin became core. No paid tier for basic auth, RBAC, encryption

### What OpenSearch Lost

- **Elastic's ecosystem momentum** — Kibana became OpenSearch Dashboards, Beats agents required forking, Logstash needed compatibility plugins
- **Name recognition** — "Elasticsearch" is still better known; many developers do not know OpenSearch exists
- **Some enterprise features** — initially lagged on ML, advanced analytics, and some query capabilities that Elastic had built under proprietary license
- **Plugin compatibility** — Elasticsearch plugins do not work on OpenSearch and vice versa. The ecosystems diverged

### Current State of Divergence

| Feature | OpenSearch | Elasticsearch |
|---------|-----------|---------------|
| Security (auth, RBAC, encryption) | Built-in, free | Basic free; advanced requires paid subscription |
| Alerting | Built-in plugin, free | Watcher — requires paid subscription |
| Anomaly detection | Built-in (RCF algorithm), free | ML features require paid subscription |
| Vector search | k-NN plugin (HNSW, IVF via Faiss/nmslib) | Dense vector field + kNN search |
| Observability | Integrated (trace analytics, metrics) | Elastic Observability (separate paid product) |
| Query languages | Query DSL + SQL + PPL | Query DSL + SQL + ESQL + EQL |
| License | Apache 2.0 | SSPL + Elastic License |
| Governance | Linux Foundation (vendor-neutral) | Elastic NV (single company) |
| Managed offering | AWS OpenSearch Service | Elastic Cloud |

### Decision Framework

| Factor | Choose OpenSearch | Choose Elasticsearch |
|--------|------------------|---------------------|
| Licensing | Must be true open-source (embedding, SaaS) | SSPL/Elastic License acceptable |
| Cloud provider | AWS (native integration) | Elastic Cloud or GCP/Azure |
| Ecosystem investment | Greenfield or migrating from ES 7.x | Deeply invested in Elastic stack (Kibana, Beats, Agent) |
| Security features | Need built-in, free security | Willing to pay for Elastic subscription |
| Cost sensitivity | Want free features (alerting, anomaly detection, security) | Budget for Elastic subscriptions |
| Community | Prefer vendor-neutral governance | Prefer Elastic's focused, opinionated development |

**Interview angle**: "The fork was a licensing dispute, not a technical one. Both systems share the same Lucene core and are technically equivalent for most use cases. I'd choose based on ecosystem: AWS shop → OpenSearch, Elastic Cloud shop → Elasticsearch. The technical differences are secondary to the operational integration and licensing requirements."

---

## Trade-Off 10: Converged Platform vs Single-Purpose Tool

### What OpenSearch Chose

OpenSearch has evolved from a search engine into a converged platform spanning:

- **Full-text search** — the original use case
- **Log analytics** — competing with Splunk, Datadog
- **Observability** — trace analytics, metrics correlation, dashboards
- **Security analytics** — SIEM-like capabilities
- **Vector / semantic search** — k-NN plugin, neural search, hybrid search
- **Business analytics** — SQL, PPL, dashboards, reporting

One cluster, one query interface (mostly), one operational stack.

### What It Sacrificed

Best-in-class performance for any single use case. A system that does six things will not beat a system purpose-built for one thing.

```
Approximate capability comparison (directional, not benchmarks):

Use Case               OpenSearch      Best-in-Class Alternative
──────────────────────  ──────────────  ─────────────────────────────
Full-text search        ████████░░      Elasticsearch   ████████░░
Log analytics (OLAP)    ███████░░░      ClickHouse      █████████░
Vector search           ██████░░░░      Pinecone        █████████░
Time-series metrics     █████░░░░░      Prometheus      █████████░
SIEM / security         ██████░░░░      Splunk          ████████░░
Dashboards              ██████░░░░      Grafana         █████████░
```

### Why This Choice Was Made

Operational simplicity is a real, measurable cost savings. Running one OpenSearch cluster instead of five specialized systems means:

- One set of operational runbooks
- One monitoring stack (or self-monitoring)
- One team's expertise to develop
- One set of backups and disaster recovery plans
- One security and access control model
- One query language family to learn (DSL + SQL + PPL)
- One vendor relationship (if managed)

For a 5-person engineering team, this simplicity can be the difference between shipping features and drowning in infrastructure management.

### When to Use Specialized Tools Instead

| Workload | Specialized Tool | Choose It When |
|----------|-----------------|---------------|
| Pure OLAP / analytics | ClickHouse, Apache Druid, Apache Doris | Query speed on structured data is critical; billions of rows; 10-100x faster aggregations |
| Pure vector search | Pinecone, Weaviate, Qdrant | Vectors are the only workload; need serverless scaling, GPU acceleration, or billion-vector scale |
| Pure metrics / monitoring | Prometheus + Grafana | High-cardinality metrics at scale; PromQL is the industry standard; need sub-second metric queries |
| Pure log aggregation (cost-optimized) | Grafana Loki | Want to minimize cost; don't need full-text indexing on all fields; label-based querying sufficient |
| Pure log analytics (enterprise) | Splunk | Budget is not a constraint; need mature SIEM, compliance frameworks, detection rule libraries |
| Event streaming + processing | Kafka + Flink | Need real-time stream processing, not batch-then-search |
| Relational queries | PostgreSQL, MySQL | Need JOINs, transactions, foreign keys, ACID |

### The "Good Enough" Argument

OpenSearch wins not by being the best at anything, but by being good enough at everything while being a single system. This is the same argument that made PostgreSQL successful — it is not the best at full-text search, not the best at JSON, not the best at time-series, but it is good enough at all of them and avoids the operational overhead of multiple specialized systems.

```
Decision tree:

Is performance for a single workload your #1 concern?
  |
  YES → Use a specialized tool for that workload
  |
  NO  → How many workloads do you have?
          |
          1-2  → Specialized tools are manageable operationally
          |
          3-5  → OpenSearch convergence starts winning on operational cost
          |
          5+   → Strongly consider OpenSearch as the unified platform
                 (unless individual workloads demand specialized performance)
```

### Breaking Out: When a Workload Outgrows OpenSearch

The convergence strategy is not permanent. As individual workloads scale, you extract them:

```
Phase 1 (Startup):   OpenSearch handles search + logs + basic observability
Phase 2 (Growth):    Log volume hits 10 TB/day → break out to ClickHouse for log analytics
                     Keep OpenSearch for search + observability
Phase 3 (Scale):     Vector search hits 100M+ embeddings → break out to Pinecone
                     Metrics monitoring needs PromQL → add Prometheus + Grafana
                     OpenSearch becomes the search engine (its original purpose)
```

**Interview angle**: "I'd start with OpenSearch as the unified platform for search + logs + observability in a startup or mid-size company. As individual workloads grow past what OpenSearch handles well — say log analytics exceeding 10 TB/day — I'd break that workload out to a specialized system like ClickHouse while keeping OpenSearch for search. The converged approach is a starting strategy, not a permanent architecture."

---

## Summary Decision Matrix

| # | Trade-Off | OpenSearch Choice | Alternative | Choose Alternative When |
|---|-----------|------------------|-------------|------------------------|
| 1 | Inverted index vs B-tree | Inverted index (term → posting list) | B-tree (PostgreSQL, DynamoDB) | OLTP, point lookups, ordered range scans |
| 2 | Immutable segments vs in-place updates | Immutable + soft deletes + merge | Mutable rows (PostgreSQL MVCC) | High update-rate workloads (> 1:10 update-to-read ratio) |
| 3 | NRT vs real-time search | ~1s refresh interval | Synchronous index updates (RDBMS) | Must search immediately after write |
| 4 | Fixed shards vs dynamic resharding | Fixed at index creation | Auto-partitioning (DynamoDB, Cassandra) | Unpredictable data growth, no capacity planning team |
| 5 | Dynamic mapping vs strict schema | Dynamic by default | Strict mapping (also OpenSearch) | Always in production (the default is wrong) |
| 6 | Denormalized vs normalized | Flat JSON documents, no JOINs | Relational model (PostgreSQL, MySQL) | Complex relationships, frequent entity updates, ACID |
| 7 | Eventual vs strong consistency | Eventual for search, tunable for writes | Strong consistency (Spanner, CockroachDB) | Financial, inventory, collaborative editing |
| 8 | Managed vs self-managed | Both; AWS pushes managed | Self-managed (EC2, K8s) | 50+ nodes, dedicated platform team, custom plugins |
| 9 | OpenSearch vs Elasticsearch | Apache 2.0 fork | Elasticsearch (SSPL) | Deeply invested in Elastic ecosystem |
| 10 | Converged vs specialized | Multi-purpose platform | Purpose-built tools | Single workload at extreme scale |

---

## Interview Strategy

### Top 5 Trade-Offs to Bring Up Proactively

These demonstrate depth without being asked. Drop them naturally when discussing your design:

1. **Inverted index vs B-tree** (Trade-Off 1) — Shows you understand data structure selection at a fundamental level. Opens the door to discussing why OpenSearch is paired with a relational primary datastore.

2. **NRT vs real-time** (Trade-Off 3) — Shows you understand the write path deeply. Lets you discuss the refresh_interval tuning knob and the GET-vs-search asymmetry. This one surprises interviewers who assume OpenSearch is real-time.

3. **Fixed shards** (Trade-Off 4) — Shows you understand operational planning. Lets you discuss shard sizing, rollover indices, and capacity planning. If the interviewer asks "what's the hardest operational challenge?" — this is your answer.

4. **Denormalized documents** (Trade-Off 6) — Shows you understand distributed JOIN costs. Lets you explain the CDC pattern: relational source of truth → denormalized search index.

5. **Eventual consistency** (Trade-Off 7) — Shows you understand the CAP/PACELC spectrum. Lets you discuss the consistency controls and when OpenSearch is not the right choice. The GET-vs-search asymmetry is a strong talking point.

### Trade-Offs to Save for "What Would You Change?" Questions

When the interviewer asks "what are the limitations?" or "what would you do differently?":

6. **Dynamic mapping default** (Trade-Off 5) — "The default is wrong for production. I'd enforce strict mappings from day one to prevent mapping explosion."

7. **Fixed shard count** (Trade-Off 4, revisited) — "If I could change one thing about OpenSearch's architecture, it would be adding automatic shard splitting like DynamoDB. The fixed shard count is the biggest operational footgun."

8. **Converged platform** (Trade-Off 10) — "At scale, I'd break out log analytics to ClickHouse and keep OpenSearch for search. The converged approach works until individual workloads outgrow it."

### How to Frame Trade-Offs in an Interview

Use this template:

> "OpenSearch chose **[X]** because **[Y reason]**. The cost of that choice is **[Z]**. For our use case, that trade-off is **[acceptable / problematic]** because **[specific reason]**. If it were problematic, I'd consider **[alternative]**."

**Example:**

> "OpenSearch chose immutable segments because they enable lock-free concurrent reads and trivially correct crash recovery. The cost is write amplification — every update reindexes the entire document and later merges segments. For a log analytics use case with append-only writes, that trade-off is perfect — logs are never updated. For a real-time inventory system with frequent updates, I'd use PostgreSQL as the source of truth and feed OpenSearch via CDC."

This framing demonstrates four things:
- You understand the technical reason behind the choice
- You know the concrete cost
- You can evaluate whether the cost is acceptable for the specific problem
- You have an alternative ready when it is not

### The Meta-Pattern

Every trade-off in this document follows the same meta-pattern: **OpenSearch optimizes for read-heavy, search-centric, append-mostly workloads.** Every design choice — immutable segments, inverted indices, NRT refresh, eventual consistency, fixed shards, denormalized documents — sacrifices something that traditional databases optimize for (transactions, strong consistency, flexible schema evolution, in-place updates, dynamic resharding) in exchange for something search needs (relevance ranking, lock-free reads, high ingest throughput, horizontal fan-out, single-shard query execution).

The single most important sentence for an interview:

> **OpenSearch is a derived, denormalized, eventually consistent search index — not a source of truth. Every architectural decision flows from this premise.**

If you internalize this, you can reason about any OpenSearch trade-off from first principles.
