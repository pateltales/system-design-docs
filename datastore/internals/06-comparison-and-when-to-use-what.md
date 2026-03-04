# Datastore Internals — Part 6: Comparison & When to Use What

> This document ties everything together. We compare all four storage engine families side-by-side, provide a decision framework for system design interviews, and map real-world databases to their engines.

---

## Table of Contents

1. [The Big Comparison Table](#1-the-big-comparison-table)
2. [Read/Write Performance Comparison](#2-readwrite-performance-comparison)
3. [Decision Framework for System Design Interviews](#3-decision-framework-for-system-design-interviews)
4. [Real-World Database → Storage Engine Mapping](#4-real-world-database--storage-engine-mapping)
5. [Common System Design Scenarios — Which Engine?](#5-common-system-design-scenarios--which-engine)
6. [Multi-Engine Architectures — Using Multiple Stores Together](#6-multi-engine-architectures--using-multiple-stores-together)
7. [Interview Cheat Sheet](#7-interview-cheat-sheet)

---

## 1. The Big Comparison Table

```
┌──────────────────────┬──────────────┬──────────────┬──────────────┬──────────────┐
│                      │   B-TREE     │  LSM-TREE    │ HASH INDEX   │ COLUMN STORE │
│                      │  (MySQL,     │ (Cassandra,  │ (Redis,      │ (Redshift,   │
│                      │   Postgres)  │  RocksDB)    │  Memcached)  │  BigQuery)   │
├──────────────────────┼──────────────┼──────────────┼──────────────┼──────────────┤
│ POINT READ           │ ✅ Fast      │ ⚠️ Good      │ ✅✅ Fastest │ ❌ Slow      │
│ (GET by key)         │ 1-2 disk I/O │ 0-5 disk I/O │ 0 disk I/O   │ N disk I/Os  │
│                      │ ~0.1-1ms     │ ~0.01-1ms    │ ~0.0001ms    │ ~10-100ms    │
│                      │              │              │ (in-memory)  │              │
├──────────────────────┼──────────────┼──────────────┼──────────────┼──────────────┤
│ RANGE QUERY          │ ✅✅ Best    │ ⚠️ Moderate  │ ❌ N/A       │ ✅ Fast*     │
│ (keys A to B)        │ Linked leaves│ Merge from   │ Hash destroys│ *for column  │
│                      │ = sequential │ multiple SSTs│ ordering     │ scans only   │
├──────────────────────┼──────────────┼──────────────┼──────────────┼──────────────┤
│ POINT WRITE          │ ⚠️ Moderate  │ ✅✅ Fastest │ ✅✅ Fastest │ ❌ Slow      │
│ (PUT key=value)      │ Random I/O   │ Sequential   │ In-memory    │ Must write N │
│                      │ ~0.1-1ms     │ ~0.01ms      │ ~0.0001ms    │ column files │
├──────────────────────┼──────────────┼──────────────┼──────────────┼──────────────┤
│ BULK WRITE           │ ⚠️ Moderate  │ ✅ Fast      │ ✅ Fast      │ ✅ Fast      │
│ (load millions)      │ Random I/O   │ Sequential   │ (if fits RAM)│ Batch convert│
├──────────────────────┼──────────────┼──────────────┼──────────────┼──────────────┤
│ ANALYTICS            │ ❌ Slow      │ ❌ Slow      │ ❌ N/A       │ ✅✅ Best    │
│ (SUM, AVG, GROUP BY) │ Full scan    │ Full scan    │ No aggregate │ Column scan  │
│                      │ all rows     │ all rows     │ support      │ + compress   │
├──────────────────────┼──────────────┼──────────────┼──────────────┼──────────────┤
│ STORAGE MEDIUM       │ Disk (SSD)   │ Disk (SSD/   │ RAM          │ Disk (SSD/   │
│                      │              │ HDD)         │ (+ optional  │ HDD/Cloud)   │
│                      │              │              │ disk persist)│              │
├──────────────────────┼──────────────┼──────────────┼──────────────┼──────────────┤
│ DATA CAPACITY        │ TBs          │ TBs-PBs      │ GBs (RAM)    │ PBs          │
│                      │              │              │              │              │
├──────────────────────┼──────────────┼──────────────┼──────────────┼──────────────┤
│ TRANSACTIONS (ACID)  │ ✅ Full      │ ⚠️ Limited   │ ❌ No        │ ⚠️ Limited   │
│                      │ MVCC, locks  │ Row-level    │ (atomic ops  │ (batch-level)│
│                      │              │ only         │ only)        │              │
├──────────────────────┼──────────────┼──────────────┼──────────────┼──────────────┤
│ COMPRESSION          │ ⚠️ Moderate  │ ✅ Good      │ ❌ None      │ ✅✅ Best    │
│                      │ Page-level   │ Block-level  │ (in-memory)  │ 5-15x ratio  │
├──────────────────────┼──────────────┼──────────────┼──────────────┼──────────────┤
│ WRITE AMPLIFICATION  │ 5-10x        │ 10-30x       │ 1x (memory)  │ N/A (batch)  │
│                      │ (random I/O) │ (sequential) │              │              │
├──────────────────────┼──────────────┼──────────────┼──────────────┼──────────────┤
│ SPACE EFFICIENCY     │ 30-50% extra │ 10% extra    │ 50-70 bytes  │ 5-15x        │
│                      │ (fragmented  │ (leveled)    │ overhead/key │ compression  │
│                      │ pages)       │              │              │              │
├──────────────────────┼──────────────┼──────────────┼──────────────┼──────────────┤
│ PREDICTABILITY       │ ✅ Stable    │ ⚠️ Variable  │ ✅ Stable    │ ⚠️ Variable  │
│                      │ Always O(log │ Compaction   │ Always O(1)  │ Depends on   │
│                      │ n) per op    │ spikes       │              │ query size   │
├──────────────────────┼──────────────┼──────────────┼──────────────┼──────────────┤
│ MATURITY             │ 50+ years    │ ~15 years    │ ~15 years    │ ~20 years    │
│                      │ Battle-tested│ Proven       │ Proven       │ Proven       │
└──────────────────────┴──────────────┴──────────────┴──────────────┴──────────────┘
```

---

## 2. Read/Write Performance Comparison

```
WRITE THROUGHPUT (operations per second, single node):

  Hash Index (Redis):     100,000 - 500,000 ops/sec  ████████████████████████████
  LSM-Tree (RocksDB):      50,000 - 200,000 ops/sec  ████████████████████
  B-Tree (PostgreSQL):     10,000 -  50,000 ops/sec   ████████
  Column Store (Redshift):  1,000 -  10,000 ops/sec   ██ (individual inserts)
                          100,000+ rows/sec bulk load  ████████████████████ (bulk)


READ THROUGHPUT (point lookups, single node):

  Hash Index (Redis):     500,000 - 1,000,000 ops/sec ████████████████████████████
  B-Tree (PostgreSQL):     50,000 -   200,000 ops/sec ████████████████████
  LSM-Tree (Cassandra):    20,000 -   100,000 ops/sec ████████████████
  Column Store (Redshift):  1,000 -    10,000 ops/sec ██ (point lookups)


ANALYTICS THROUGHPUT (scan 1 billion rows):

  Column Store (ClickHouse): 1-5 seconds     ██
  Column Store (Redshift):   5-30 seconds     ████████
  B-Tree (PostgreSQL):       30-300 seconds   ████████████████████████████
  LSM-Tree (Cassandra):      60-600 seconds   ████████████████████████████████████


LATENCY (P99, single operation):

  Hash Index (Redis):      < 1 ms      ✅ Ultra-low
  B-Tree (PostgreSQL):     1-10 ms     ✅ Low  
  LSM-Tree (Cassandra):    5-50 ms     ⚠️ Variable (compaction spikes)
  Column Store (Redshift): 100ms-10s   ❌ High (designed for batch, not real-time)
```

---

## 3. Decision Framework for System Design Interviews

```
STEP 1: What kind of queries?
══════════════════════════════

  "Give me the value for key X"  (point lookup)
    → Consider: Hash Index, B-Tree, or LSM-Tree
    
  "Give me all values where key is between A and B"  (range query)
    → Consider: B-Tree or LSM-Tree (NOT hash — destroys ordering)
    
  "What's the average/sum/count of column X across all rows?"  (analytics)
    → Consider: Column Store (Redshift, BigQuery)
    
  "Give me all records matching complex conditions with JOINs"  (OLTP SQL)
    → Consider: B-Tree (PostgreSQL, MySQL)


STEP 2: What's the read/write ratio?
═════════════════════════════════════

  Mostly reads (10:1 or higher):
    → B-Tree (PostgreSQL, MySQL)
    → Hash Index (Redis) if data fits in RAM
    
  Mostly writes (1:10 or more writes than reads):
    → LSM-Tree (Cassandra, RocksDB)
    
  Balanced (similar reads and writes):
    → Either B-Tree or LSM — depends on other factors
    

STEP 3: What are the scale requirements?
═════════════════════════════════════════

  Data size < 100 GB, fits in RAM:
    → Hash Index (Redis) for max speed
    
  Data size 100 GB - 10 TB:
    → B-Tree (PostgreSQL) or LSM-Tree (Cassandra)
    → Single node or small cluster
    
  Data size 10 TB - 1 PB:
    → LSM-Tree (Cassandra) for operational workloads
    → Column Store (Redshift, BigQuery) for analytics
    → Distributed system, many nodes
    
  Data size > 1 PB:
    → Column Store (BigQuery, Redshift Spectrum)
    → LSM-Tree (HBase on HDFS)


STEP 4: What consistency/durability guarantees?
═══════════════════════════════════════════════

  Need ACID transactions (strong consistency):
    → B-Tree (PostgreSQL, MySQL)
    
  Eventual consistency is OK:
    → LSM-Tree (Cassandra, DynamoDB)
    → Hash Index (Redis)
    
  Don't care (analytics, batch):
    → Column Store


STEP 5: What latency requirements?
═══════════════════════════════════

  Sub-millisecond (< 1ms):
    → Hash Index (Redis, Memcached)
    
  Low latency (< 10ms):
    → B-Tree (PostgreSQL) or LSM-Tree (Cassandra)
    
  Batch/reporting (seconds to minutes OK):
    → Column Store (Redshift, BigQuery)
```

---

## 4. Real-World Database → Storage Engine Mapping

```
┌─────────────────────┬──────────────┬────────────────────────────────────┐
│ Database             │ Engine       │ Typical Use Case                    │
├─────────────────────┼──────────────┼────────────────────────────────────┤
│                     │              │                                     │
│ --- B-TREE BASED ---│              │                                     │
│ PostgreSQL          │ B+ tree      │ General-purpose OLTP, web backends │
│ MySQL (InnoDB)      │ B+ tree      │ Web apps, e-commerce, CMS          │
│ SQLite              │ B+ tree      │ Mobile apps, embedded, testing     │
│ Oracle              │ B+ tree      │ Enterprise OLTP                    │
│ SQL Server          │ B+ tree      │ Enterprise OLTP                    │
│                     │              │                                     │
│ --- LSM-TREE BASED--│              │                                     │
│ Apache Cassandra    │ LSM tree     │ Write-heavy, distributed, IoT      │
│ Amazon DynamoDB     │ LSM tree     │ Serverless KV store, session data  │
│ RocksDB             │ LSM tree     │ Embedded engine, used by others    │
│ LevelDB             │ LSM tree     │ Embedded engine, blockchain nodes  │
│ HBase               │ LSM tree     │ Hadoop ecosystem, big data         │
│ CockroachDB         │ LSM (Pebble) │ Distributed SQL, NewSQL           │
│ ScyllaDB            │ LSM tree     │ Cassandra-compatible, C++ perf     │
│ InfluxDB            │ LSM-like     │ Time-series data, monitoring       │
│                     │              │                                     │
│ --- HASH-BASED ---  │              │                                     │
│ Redis               │ Hash table   │ Caching, sessions, real-time data  │
│ Memcached           │ Hash table   │ Simple caching                     │
│ Riak (Bitcask)      │ Hash + log   │ KV store with disk persistence     │
│                     │              │                                     │
│ --- COLUMN STORES --│              │                                     │
│ Amazon Redshift     │ Columnar     │ Data warehouse, analytics          │
│ Google BigQuery     │ Columnar     │ Serverless analytics, data lake    │
│ ClickHouse          │ Columnar+LSM │ Real-time analytics, logs          │
│ Snowflake           │ Columnar     │ Cloud data warehouse               │
│ Apache Druid        │ Columnar     │ Real-time analytics, OLAP          │
│ DuckDB              │ Columnar     │ In-process analytics ("local BQ")  │
│                     │              │                                     │
│ --- HYBRID ---      │              │                                     │
│ MongoDB (WiredTiger)│ B-tree + LSM │ Document store, flexible schema    │
│ TiDB                │ B-tree + LSM │ HTAP (OLTP + OLAP in one)         │
│ CockroachDB         │ SQL + LSM    │ Distributed SQL with LSM storage  │
│                     │              │                                     │
│ --- SEARCH ---      │              │                                     │
│ Elasticsearch       │ Inverted idx │ Full-text search, log analytics    │
│ Apache Solr         │ Inverted idx │ Search engine                      │
│                     │              │                                     │
└─────────────────────┴──────────────┴────────────────────────────────────┘
```

---

## 5. Common System Design Scenarios — Which Engine?

```
SCENARIO 1: "Design a URL Shortener" (TinyURL)
────────────────────────────────────────────────
  Workload: Write a URL, read by short code
  Pattern: Point lookups, write once, read many
  Scale: Billions of URLs
  
  Best engine: LSM-Tree (Cassandra, DynamoDB)
  Why: Write-heavy initially, point lookups, massive scale, no range queries
  Cache: Redis in front for hot URLs


SCENARIO 2: "Design Twitter"
─────────────────────────────
  Workload: Post tweets, read timelines, search
  Pattern: Write tweets, fan-out reads, full-text search
  
  Tweets storage: LSM-Tree (Cassandra) — write-heavy, append-only
  Timeline cache: Redis (sorted sets) — fast reads, sorted by time
  User profiles: B-Tree (PostgreSQL) — relational, needs transactions
  Search: Elasticsearch (inverted index) — full-text search
  Analytics: Column Store (Redshift) — engagement metrics, dashboards


SCENARIO 3: "Design a Key-Value Store" (like DynamoDB)
───────────────────────────────────────────────────────
  Workload: PUT/GET/DELETE by key
  Pattern: High write throughput, point lookups
  Scale: Billions of keys, distributed
  
  Best engine: LSM-Tree
  Why: Write-optimized, sequential I/O, good for distributed systems
  Bloom filters for fast "not found" responses


SCENARIO 4: "Design an E-Commerce Platform" (like Amazon)
──────────────────────────────────────────────────────────
  Workload: Product catalog, orders, payments, search, recommendations
  
  Product catalog: B-Tree (PostgreSQL) — complex queries, transactions
  Orders/Payments: B-Tree (PostgreSQL) — ACID required!
  Product search: Elasticsearch — full-text + faceted search
  Session/Cart: Redis — fast, TTL-based expiry
  Recommendations: Column Store — analytics on user behavior
  Product images: Object Store (S3) — not a DB engine


SCENARIO 5: "Design a Chat System" (like WhatsApp)
────────────────────────────────────────────────────
  Workload: Send messages, read conversations, presence
  Pattern: Write-heavy (new messages), read recent messages
  
  Messages: LSM-Tree (Cassandra) — write-heavy, time-ordered
  Presence/Online status: Redis — in-memory, TTL-based
  User profiles: B-Tree (PostgreSQL) — relational data
  

SCENARIO 6: "Design a Metrics/Monitoring System" (like Datadog)
────────────────────────────────────────────────────────────────
  Workload: Ingest millions of data points/sec, query dashboards
  Pattern: Massive writes (time-series), range queries by time, aggregations
  
  Time-series data: LSM-Tree (InfluxDB, TimescaleDB) — write-optimized
  Long-term analytics: Column Store (ClickHouse) — fast aggregations
  Alerting state: Redis — fast checks, pub/sub for notifications


SCENARIO 7: "Design a Data Warehouse" (like Redshift)
──────────────────────────────────────────────────────
  Workload: Batch loads, complex analytical queries
  Pattern: Bulk writes, column scans, aggregations
  
  Best engine: Column Store
  Why: Analytics queries scan columns, compression saves storage
  10-100x faster than row-oriented for typical dashboard queries
```

---

## 6. Multi-Engine Architectures — Using Multiple Stores Together

Real systems almost ALWAYS use multiple storage engines. Here's the pattern:

```
TYPICAL WEB APPLICATION ARCHITECTURE:

  ┌───────────┐     ┌────────────────┐     ┌──────────────┐
  │  Client   │────▶│  Application   │────▶│  PostgreSQL  │
  │ (browser) │     │   Server       │     │  (B-tree)    │
  └───────────┘     │                │     │  Primary DB  │
                    │                │     └──────────────┘
                    │                │
                    │                │────▶ ┌──────────────┐
                    │                │     │    Redis      │
                    │                │     │  (Hash)       │
                    │                │     │  Cache Layer  │
                    │                │     └──────────────┘
                    │                │
                    │                │────▶ ┌──────────────┐
                    │                │     │ Elasticsearch │
                    │                │     │ (Inverted Idx)│
                    │                │     │  Search       │
                    │                │     └──────────────┘
                    └────────────────┘
                           │
                    ETL / Change Data Capture
                           │
                    ┌──────▼──────────┐
                    │   Redshift      │
                    │  (Column Store) │
                    │  Analytics/     │
                    │  Reporting      │
                    └─────────────────┘

Each engine handles what it's best at:
  PostgreSQL: Transactions, complex queries, source of truth
  Redis:      Caching hot data, sessions, rate limiting
  Elasticsearch: Full-text search, log search
  Redshift:   Analytics, dashboards, reports

Data flows from PostgreSQL → (ETL) → Redshift for analytics.
Hot data is cached in Redis for fast reads.
Search indexes are maintained in Elasticsearch.
```

---

## 7. Interview Cheat Sheet

```
QUICK REFERENCE — What to say in a system design interview:

"I'd use [X] because..."

PostgreSQL (B-tree):
  "...we need ACID transactions for payment/order data"
  "...our read-to-write ratio is high and we need complex queries"
  "...data integrity and consistency are critical"

Cassandra (LSM-tree):
  "...we have very high write throughput requirements"
  "...we need horizontal scalability across regions"
  "...eventual consistency is acceptable for this use case"
  "...our access pattern is point lookups by partition key"

DynamoDB (LSM-tree, managed):
  "...we want a serverless, fully-managed key-value store"
  "...we need predictable single-digit millisecond latency"
  "...our access patterns are well-defined partition key lookups"

Redis (Hash index):
  "...we need sub-millisecond latency for caching"
  "...we need real-time leaderboards (sorted sets)"
  "...we need distributed locks or rate limiting"
  "...our hot dataset fits in memory"

Redshift/BigQuery (Column store):
  "...we need to run analytical queries on large datasets"
  "...our queries aggregate over millions/billions of rows"
  "...we're building a data warehouse for reporting"

Elasticsearch (Inverted index):
  "...we need full-text search capabilities"
  "...we need to search across multiple fields with fuzzy matching"
  "...we're building a log analytics pipeline"


THINGS TO NEVER SAY:
  ❌ "Just use [one database] for everything"
  ❌ "Redis as the primary database" (unless you know the data fits in RAM)
  ❌ "Cassandra for transactions" (no ACID support)
  ❌ "PostgreSQL for 100K writes/sec" (use LSM-tree)
  ❌ "Redshift for real-time user-facing queries" (too slow)
```

---

*Previous: [← Column Stores](05-column-stores.md) | Next: [Indexing Deep Dive →](07-indexing.md)*