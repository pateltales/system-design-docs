# SQL vs NoSQL — The Complete Decision Framework

> A practical guide for system design interviews. Each tradeoff includes real-world examples to build your mental model.

---

## The 7 Tradeoffs to Consider When Choosing a Database

---

### 1. Data Model / Access Patterns

| Favor SQL | Favor NoSQL |
|---|---|
| Relational data with JOINs (e.g., users → orders → products) | Simple key-value lookups (e.g., `shortURL → longURL`) |
| Complex queries with WHERE, GROUP BY, aggregations | Access is always by primary key or a known partition key |
| Data has relationships that need referential integrity | Data is denormalized / self-contained in each record |

#### Real-World Examples

**SQL — E-Commerce Platform (Amazon/Shopify)**
- A customer places an order → you need to JOIN `users`, `orders`, `order_items`, `products`, `inventory`, `payments`
- "Show me all orders from user X in the last 30 days with product details" = multi-table JOIN
- Referential integrity ensures you can't have an `order_item` pointing to a non-existent `product`

**NoSQL — Session Store (Redis / DynamoDB)**
- Web app stores user sessions: `sessionID → { userId, cart, preferences, lastActive }`
- Access pattern is always: "give me the session for this ID" — pure key-value lookup
- No relationships, no JOINs, no complex queries — just fast GET/SET by key
- Netflix uses DynamoDB for this exact pattern

**NoSQL — Content Management System (MongoDB)**
- A blog platform where each post document contains `{ title, body, author, tags[], comments[] }`
- Each document is self-contained — no need to JOIN separate `comments` and `tags` tables
- Different post types can have different fields (video posts have `duration`, text posts have `wordCount`)

#### TinyURL Verdict
Data is key-value shaped (favors NoSQL), BUT the `FOR UPDATE SKIP LOCKED` key generation needs SQL → **SQL wins** because of the write pattern, not the read pattern.

---

### 2. Consistency Requirements

| Favor SQL (Strong Consistency) | Favor NoSQL (Eventual Consistency OK) |
|---|---|
| Duplicate keys would be catastrophic | Temporary stale reads are acceptable |
| Financial transactions, inventory counts | Social media feeds, analytics, logs |
| Need ACID transactions across rows | Single-record operations are sufficient |

#### Real-World Examples

**SQL — Banking / Payment Systems (Stripe, Square)**
- User transfers $500 from Account A to Account B
- This MUST be atomic: debit A AND credit B, or neither happens
- If this isn't ACID, money can disappear or be duplicated
- Stripe uses PostgreSQL for payment processing — you can't have "eventual" money

**SQL — Flight Booking System (Expedia, Booking.com)**
- Two users try to book the last seat on a flight simultaneously
- Without strong consistency + row-level locking, both could book the same seat
- The system needs `SELECT ... FOR UPDATE` to lock the seat row during booking

**NoSQL (Eventual Consistency OK) — Social Media Like Counts (Instagram, Twitter/X)**
- User likes a post → the like count shows "1,204" to one user and "1,203" to another for a few seconds
- This is fine! Nobody cares if the like count is off by 1 for 200ms
- Cassandra is used here — it replicates across nodes asynchronously, fast writes, eventual consistency

**NoSQL (Eventual Consistency OK) — DNS (Domain Name System)**
- When you update a DNS record, it can take minutes to hours to propagate globally
- This is acceptable — eventual consistency by design
- Amazon Route 53 uses DynamoDB-like infrastructure for DNS records

#### TinyURL Verdict
Two users getting the **same short URL** for different long URLs = broken system. Strong consistency matters for writes → **SQL wins**.

---

### 3. Write vs Read Ratio & Volume

| Favor SQL | Favor NoSQL |
|---|---|
| Moderate writes (hundreds–low thousands/sec) | Massive writes (tens of thousands+/sec) |
| Read-heavy with complex queries | Write-heavy or balanced workloads |
| Writes need transactional guarantees | Writes are simple appends/upserts |

#### Real-World Examples

**SQL — Ride Booking (Uber Core Trips Database)**
- New ride request = ~1,000 writes/sec (moderate)
- But each write involves complex transactions: match driver, update availability, create trip record, charge payment
- Transactional integrity matters more than raw write throughput
- Uber's core trip data uses MySQL (with their Schemaless layer on top)

**NoSQL — IoT Sensor Data (Tesla, Industrial IoT)**
- 1 million cars each sending telemetry every second = **1 million writes/sec**
- Each write is a simple append: `{ carId, timestamp, speed, battery, location }`
- No transactions needed — just ingest as fast as possible
- Time-series DBs like InfluxDB or Cassandra handle this; SQL would choke

**NoSQL — Clickstream / Analytics (Google Analytics, Mixpanel)**
- Every click, page view, scroll event from millions of users = **hundreds of thousands of writes/sec**
- Each event is a simple document: `{ userId, event, timestamp, metadata }`
- BigQuery, Cassandra, or Kafka → data warehouse pipeline
- SQL would bottleneck on write throughput at this scale

**SQL — Hotel Reservation System (Marriott, Hilton)**
- Maybe 500 bookings/sec globally — well within SQL range
- Each booking needs a transaction: check room availability, reserve, charge, confirm
- PostgreSQL or Oracle handles this easily

#### TinyURL Verdict
228 writes/sec, 3,805 reads/sec — **easily within SQL range**. If it were 228K writes/sec, NoSQL (DynamoDB/Cassandra) would be the answer.

---

### 4. Scalability (Horizontal Sharding)

| Favor SQL | Favor NoSQL |
|---|---|
| Data fits in a few TB (or use Citus/Vitess for sharding) | Data is massive (PB-scale) and needs auto-sharding |
| You accept the operational cost of managing sharding | You want sharding built-in (DynamoDB, Cassandra, MongoDB) |
| Team has SQL sharding expertise | Team wants zero-config horizontal scaling |

#### Real-World Examples

**SQL with Sharding — Payments (Stripe)**
- Stripe uses PostgreSQL sharded across many nodes
- They built custom sharding (similar to Citus/Vitess approach)
- Total data is large (TBs) but needs ACID → worth the operational cost
- They accept the complexity because consistency is non-negotiable for money

**NoSQL with Auto-Sharding — User Activity Feed (Netflix Viewing History)**
- Netflix stores every show/movie every user has watched + progress + ratings
- 200M+ users × thousands of records each = **petabytes** of data
- Cassandra auto-shards by user ID — add nodes, data rebalances automatically
- No need for ACID transactions — just fast writes and reads by userId

**NoSQL with Auto-Sharding — Chat Messages (Discord)**
- Billions of messages across millions of servers
- Discord uses Cassandra (later moved to ScyllaDB for performance)
- Messages are partitioned by `(channel_id, bucket)` — auto-sharded
- Adding capacity = just add more nodes, no manual resharding

**SQL — Small-Medium SaaS App (Basecamp, Linear)**
- Total data might be 500GB–few TB
- Single PostgreSQL instance with read replicas handles everything
- No sharding needed at all — vertical scaling is sufficient

#### TinyURL Verdict
48 TB is manageable with PostgreSQL + Citus. If it were 480 TB+, DynamoDB's auto-sharding would be more attractive → **SQL is fine here with Citus**.

---

### 5. Schema Flexibility

| Favor SQL | Favor NoSQL |
|---|---|
| Schema is well-known and stable | Schema evolves frequently or varies per record |
| You want the DB to enforce data shape (NOT NULL, types, constraints) | Documents have varying fields per record |
| Migrations are acceptable | Schema-on-read is preferred |

#### Real-World Examples

**SQL — Payroll / HR System (Workday, ADP)**
- Every employee record has the exact same fields: `name`, `salary`, `department`, `tax_id`, `hire_date`
- Schema never changes (it's defined by regulations and law)
- NOT NULL constraints and foreign keys prevent data corruption
- You absolutely cannot have an employee with a NULL salary or an invalid department ID

**NoSQL — Product Catalog (Amazon Marketplace)**
- A "Laptop" product has: `screenSize`, `ram`, `processor`, `gpu`
- A "T-Shirt" product has: `size`, `color`, `material`, `sleeveLength`
- A "Book" product has: `author`, `isbn`, `pageCount`, `publisher`
- Each product type has **completely different attributes** — rigid SQL schema doesn't work well
- MongoDB stores each product as a flexible document with varying fields
- This is why Amazon uses DynamoDB for their product catalog

**NoSQL — Event Logging (Datadog, Splunk)**
- Log events from different services have different fields
- Web server log: `{ method, path, statusCode, responseTime }`
- Payment log: `{ userId, amount, currency, provider, success }`
- Kubernetes log: `{ podName, namespace, container, exitCode }`
- Schema-on-read lets you ingest everything without predefined schemas

#### TinyURL Verdict
Schema is dead simple and never changes (4 columns: suffix, longURL, creatorID, expiry) → **slight edge to SQL** for enforcement, but doesn't really matter.

---

### 6. Latency Requirements

| Favor SQL | Favor NoSQL |
|---|---|
| Single-digit ms reads via B-Tree index are sufficient | Need guaranteed sub-ms reads at any scale |
| Acceptable to add Redis cache for hot data | Built-in caching or inherent single-digit ms guaranteed |
| Complex query performance matters | Simple lookup speed matters |

#### Real-World Examples

**NoSQL — Gaming Leaderboards (Fortnite, Clash Royale)**
- Real-time leaderboard showing top 100 players
- Needs sub-millisecond reads that scale to millions of concurrent players
- Redis Sorted Sets: `ZREVRANGE leaderboard 0 99` → returns top 100 in <1ms
- SQL with ORDER BY + LIMIT would work at small scale but can't guarantee <1ms at millions of concurrent requests

**NoSQL — Ad Serving (Google Ads, Facebook Ads)**
- When a webpage loads, an ad auction happens in **<10ms total**
- Within that 10ms: look up user profile, run auction algorithm, fetch winning ad creative
- DynamoDB or custom in-memory stores provide guaranteed single-digit ms
- SQL would be too unpredictable — B-Tree lookups can have occasional latency spikes during vacuum/compaction

**SQL — Business Intelligence Dashboard (Tableau, Looker)**
- "Show me revenue by region, grouped by quarter, for the last 3 years"
- This is a complex aggregation query that takes 200ms–2 seconds
- Sub-millisecond latency doesn't matter here — the user expects to wait a moment
- PostgreSQL with proper indexes excels at this; NoSQL can't do ad-hoc aggregations easily

**SQL — REST API for a SaaS App (GitHub, Jira)**
- API responds in 50-200ms (most of that is app logic, not DB)
- PostgreSQL serving indexed lookups in 1-5ms is more than sufficient
- Adding Redis cache for hot endpoints if needed

#### TinyURL Verdict
With a Redis cache in front, both SQL and NoSQL serve reads in <1ms for hot keys. Cold reads from PostgreSQL with B-Tree index are ~1-5ms → **acceptable, not a differentiator**.

---

### 7. Operational Complexity / Team Expertise

| Favor SQL | Favor NoSQL |
|---|---|
| Team knows PostgreSQL/MySQL well | Team is cloud-native, prefers managed services |
| You want a single system for key gen + storage | You're okay with separate systems (KGS + storage) |
| Open-source, portable across clouds | Cloud-specific (DynamoDB = AWS lock-in) |
| Rich ecosystem (pgAdmin, migrations, ORMs) | Simpler data model = less tooling needed |

#### Real-World Examples

**SQL — Traditional Tech Company (Shopify, GitHub)**
- Engineering team has 10+ years of PostgreSQL expertise
- Tooling built around SQL: ActiveRecord ORM, Rails migrations, pgbouncer for connection pooling
- Switching to DynamoDB would mean retraining the entire team + rewriting all data access code
- "The best database is the one your team knows"

**NoSQL — Cloud-Native Startup (a new SaaS built on AWS)**
- Small team (3-5 engineers) building fast on AWS
- DynamoDB: zero server management, auto-scaling, pay-per-request pricing
- No need to manage PostgreSQL instances, replicas, backups, connection pools, vacuuming
- Team can focus on product, not database operations

**Hybrid — Uber**
- Uses MySQL (SQL) for trip data where ACID matters
- Uses Cassandra (NoSQL) for driver location tracking (massive write throughput)
- Uses Redis (NoSQL) for real-time caching and geospatial queries
- Different databases for different access patterns within the same company

#### TinyURL Verdict
SQL collapses key generation + storage into one system → **less operational complexity**. With NoSQL, you'd need a separate Key Generation Service, making the architecture more complex.

---

## Quick Decision Framework (Memorize This for Interviews)

```
Ask yourself these 3 questions in order:

1. Do I need ACID transactions across multiple rows?
   → YES: SQL (PostgreSQL, MySQL)
   → NO: Continue...

2. Is my write volume > 10K/sec or data > 100TB?
   → YES: Lean NoSQL (DynamoDB, Cassandra) or NewSQL (CockroachDB, Spanner)
   → NO: Continue...

3. Is my data relational (JOINs needed) or key-value/document shaped?
   → Relational: SQL
   → Key-value/document: Either works — choose based on team expertise & ops preference
```

### Flowchart Version

```
                    Need ACID transactions?
                    /                      \
                  YES                       NO
                  /                          \
              → SQL                   Write volume > 10K/sec
              (PostgreSQL,             or data > 100TB?
               MySQL)                  /              \
                                     YES               NO
                                     /                  \
                                 → NoSQL            Data relational?
                                 (DynamoDB,          /           \
                                  Cassandra)       YES            NO
                                                   /               \
                                               → SQL          → Either works
                                                              (team preference)
```

---

## TinyURL-Specific Summary

| Tradeoff | Winner | Why |
|---|---|---|
| Data model | Tie | Key-value fits both, but `SKIP LOCKED` needs SQL |
| Consistency | **SQL** | Can't have duplicate short URLs |
| Write volume | **SQL** | 228/sec is trivial for PostgreSQL |
| Scalability | Tie | Citus (SQL) vs DynamoDB auto-sharding — both solve 48TB |
| Schema | **SQL** (slight) | Fixed 4-column schema, enforcement is nice |
| Latency | Tie | Redis cache in front of either |
| Ops complexity | **SQL** | One system (PostgreSQL) vs two (KGS + DynamoDB) |

**Overall: SQL (PostgreSQL + Citus) wins for TinyURL**, primarily because:
1. The `FOR UPDATE SKIP LOCKED` pattern elegantly combines key generation + storage
2. Scale (228 writes/sec) is well within SQL's comfort zone
3. One system to manage instead of two

---

## The Interview-Winning Answer

> "Both SQL and NoSQL work for TinyURL. I'd choose PostgreSQL because at 228 writes/sec and 3,805 reads/sec, we're well within SQL's comfort zone. The key advantage is the `FOR UPDATE SKIP LOCKED` pattern — it lets us combine key generation and storage into a single atomic operation, eliminating the need for a separate key generation service. For the 48TB storage requirement, we'd use Citus for horizontal sharding. If we were at 100x this scale, I'd consider DynamoDB with a separate key generation service, trading architectural simplicity for write scalability."

This shows you understand **both approaches** and can reason about **tradeoffs** — which is exactly what SDE2 interviews test.

---

## 8. Schema Design Rationale

A column-by-column justification for the TinyURL schema:

```sql
CREATE TABLE url_mappings (
    id          BIGSERIAL PRIMARY KEY,
    suffix      CHAR(8) NOT NULL UNIQUE,
    long_url    VARCHAR(2048),
    creator_id  BIGINT,
    expiry_time TIMESTAMP NOT NULL DEFAULT '1970-01-01',
    created_at  TIMESTAMP DEFAULT NOW()
);
```

For each column, explain:

- **id (BIGSERIAL PRIMARY KEY)**: Why auto-increment? Why not use suffix as PK? Answer: suffix is CHAR(8) = 8 bytes, same as BIGINT (8 bytes), but BIGSERIAL gives us a clustered storage order that's write-optimized. New rows go to the end of the heap. If suffix were the PK, random inserts would cause page splits. Also, BIGSERIAL is needed for Citus distribution.

- **suffix (CHAR(8) NOT NULL UNIQUE)**: Why CHAR(8) not VARCHAR(8)? CHAR is fixed-width = no 1-byte length prefix overhead, simpler memory layout, slightly faster comparisons. All suffixes are exactly 8 chars, so no wasted space. NOT NULL + UNIQUE enforces the invariant that no two mappings share a suffix.

- **long_url (VARCHAR(2048))**: Why 2048? It's the practical max URL length supported by most browsers (IE: 2083, Chrome: ~2MB but 2048 is the de facto standard). Why not TEXT? VARCHAR(2048) acts as a guard-rail — rejects absurdly long URLs at the DB level. NULLable for pre-populated rows that haven't been claimed yet.

- **creator_id (BIGINT)**: Why BIGINT not INT? INT max = ~2.1B, which could be exceeded at Amazon scale. BIGINT supports 9.2 quintillion. NULLable for anonymous URL creation.

- **expiry_time (TIMESTAMP NOT NULL DEFAULT '1970-01-01')**: Why epoch default? Pre-populated rows start with epoch, meaning `WHERE expiry_time < NOW()` naturally finds both unclaimed (epoch) and expired rows. NOT NULL ensures every row participates in the expiry index — no NULL-handling complexity. Why TIMESTAMP not BIGINT? TIMESTAMP supports native PostgreSQL date functions (INTERVAL arithmetic, NOW() comparisons) and is human-readable in queries.

- **created_at (TIMESTAMP DEFAULT NOW())**: Audit column. Useful for debugging, analytics, and potential future features. Minimal storage cost (8 bytes).

Add a "Storage Per Row" calculation table:
| Column | Type | Bytes |
|---|---|---|
| id | BIGSERIAL | 8 |
| suffix | CHAR(8) | 8 |
| long_url | VARCHAR(2048) | ~50 avg (1 byte length prefix + content) |
| creator_id | BIGINT | 8 (or 1 if NULL) |
| expiry_time | TIMESTAMP | 8 |
| created_at | TIMESTAMP | 8 |
| Row overhead | (heap tuple header) | ~23 |
| **Total** | | **~105 bytes/row** (with ~50 byte avg URL) |

Also add a "Why Not These Alternatives?" subsection:
- **Why not UUID as PK?** 16 bytes vs 8 bytes BIGINT. Doubles index size. Random UUIDs cause write amplification (random B-Tree inserts). No benefit since suffix already provides uniqueness.
- **Why not separate tables for keys and mappings?** Single table = single atomic UPDATE vs two-table = need a transaction spanning both. One table is simpler, fewer joins, fewer locks.
- **Why not JSONB for extensibility?** Fixed schema is known — 4 columns won't change. JSONB adds parsing overhead, no type safety, can't create standard B-Tree indexes on nested fields.

---

## 9. Index Strategy Deep Dive

### The Three Indexes

#### Index 1: Primary Key Index (Automatic)
```sql
-- Created automatically by PRIMARY KEY constraint
-- B-Tree on id column
```
- Purpose: Internal row identification, foreign key references
- Size: 8 bytes per entry × 10B rows = ~80 GB
- Not used for application queries directly — mainly for Citus distribution

#### Index 2: Suffix Unique Index (The Read Path Index)
```sql
CREATE UNIQUE INDEX idx_suffix ON url_mappings(suffix);
```
- Purpose: Fast O(log n) lookups for the redirect path (`GET /{suffix}`)
- B-Tree depth: log₃₀₀(10B) ≈ 4 levels (300 keys per 8KB page, ~8 byte keys)
- Lookup: 4 page reads × 8KB = 32KB I/O per lookup (usually cached in shared_buffers)
- Size calculation: 10B entries × (8 byte key + 6 byte TID) ≈ **~130 GB**
- Performance: <1ms with warm buffer pool, 2-5ms cold

Why UNIQUE INDEX vs just INDEX?
- UNIQUE enforces the constraint at the DB level — even if application logic has a bug, DB prevents duplicate suffixes
- UNIQUE index lookup can stop at first match (slightly faster than non-unique where DB must check for more)

#### Index 3: Partial Expiry Index (The Write Path Index)
```sql
CREATE INDEX idx_expiry_available ON url_mappings(expiry_time)
    WHERE expiry_time < NOW();
```
**This is the sophisticated index.** Explain:

- **What it does**: Only indexes rows where `expiry_time < NOW()` — i.e., expired or unclaimed rows
- **Why partial?**: Of 10B total rows, maybe 100M are expired/unclaimed at any time. A full index on expiry_time would be ~130 GB. The partial index is ~1.3 GB (100M × 14 bytes). That's **100x smaller**.
- **How it helps**: The `FOR UPDATE SKIP LOCKED` query has `WHERE expiry_time < NOW()` — this predicate matches the partial index exactly
- **Maintenance**: As rows are claimed (expiry_time set to future), they're removed from the partial index. As rows expire, they're added. This happens automatically.
- **Caveat**: `NOW()` in the index predicate is evaluated at INSERT/UPDATE time, not at query time. So the partial index is maintained based on the value at write time. PostgreSQL automatically re-evaluates during vacuum.

Show an index size comparison table:
| Index | Full Index Size | Partial Index Size | Savings |
|---|---|---|---|
| idx_suffix (UNIQUE) | 130 GB | N/A (full) | N/A |
| idx_expiry (full) | 130 GB | N/A | N/A |
| idx_expiry_available (partial) | N/A | ~1.3 GB | **99%** |

### Why NOT to Index long_url
- long_url avg = 50 bytes. B-Tree index on VARCHAR(2048) = huge index (~500 GB+)
- No query ever does `WHERE long_url = '...'` — we always look up by suffix
- If you needed "does this long_url already have a short URL?" you'd use a hash index or a separate lookup table, not a B-Tree on the main table

### Index Impact on Write Performance
Explain that each index adds overhead to writes:
| Operation | Without Indexes | With 3 Indexes |
|---|---|---|
| INSERT (pre-populate) | Fast | ~3x slower |
| UPDATE (claim a key) | Fast | ~2x slower (update expiry index, no change to suffix index) |
| SELECT by suffix | Full table scan | O(log n) = ~4 page reads |

At 228 writes/sec, even 3x index overhead is negligible. The read performance gain far outweighs write cost.

---

## 10. Sharding Topology with Citus

### Why Shard?
- Single PostgreSQL node: comfortable up to ~5-10 TB
- TinyURL: 48 TB over system lifetime
- Need: 10 shards × ~5 TB each

### Citus Architecture Diagram
```
                    ┌─────────────────┐
                    │   Coordinator   │
                    │  (Routes SQL)   │
                    └────────┬────────┘
                             │
          ┌──────────────────┼──────────────────┐
          │                  │                  │
    ┌─────▼─────┐     ┌─────▼─────┐     ┌─────▼─────┐
    │  Worker 1 │     │  Worker 2 │ ... │ Worker 10 │
    │  Shard 1  │     │  Shard 2  │     │ Shard 10  │
    │  ~4.8 TB  │     │  ~4.8 TB  │     │  ~4.8 TB  │
    │  + Replica │     │  + Replica│     │  + Replica │
    └───────────┘     └───────────┘     └───────────┘
```

### Setup DDL
```sql
-- 1. Create the distributed table
SELECT create_distributed_table('url_mappings', 'suffix');

-- 2. Set shard count (32 shards across 10 workers for flexibility)
SET citus.shard_count = 32;

-- 3. Citus automatically:
--    - Hashes suffix values
--    - Routes INSERTs/UPDATEs to correct shard
--    - Routes SELECT by suffix to single shard
```

### Distribution Key Choice: suffix
**Why suffix (not id, not creator_id)?**

| Distribution Key | Read Query Pattern | Write Pattern | Verdict |
|---|---|---|---|
| suffix | `WHERE suffix = ?` → single shard | `FOR UPDATE SKIP LOCKED` → single shard | Best |
| id | `WHERE suffix = ?` → ALL shards (scatter-gather) | Single shard | Terrible for reads |
| creator_id | `WHERE suffix = ?` → ALL shards | Grouped by user | Terrible for reads |

### How Queries Route

**Read (redirect)**:
```sql
SELECT long_url FROM url_mappings WHERE suffix = 'ab3k9x12';
-- Citus: hash('ab3k9x12') → shard 7 → route to Worker 3
-- Single-shard query: fast
```

**Write (claim key)**:
```sql
-- The FOR UPDATE SKIP LOCKED query runs on ONE shard at a time
-- Citus routes based on the suffix being claimed
-- Each shard has its own pool of available keys
```

### Rebalancing
When adding a new worker (e.g., Worker 11):
```sql
SELECT citus_rebalance_start();
-- Citus moves some shards from existing workers to the new one
-- Online operation — reads continue during rebalance
-- Writes to moving shards are briefly paused (seconds)
```

### Cross-Shard Query Patterns
Most queries are single-shard (by suffix). But some admin queries need cross-shard:

| Query | Pattern | Performance |
|---|---|---|
| `SELECT ... WHERE suffix = ?` | Single shard | <5ms |
| `SELECT COUNT(*) WHERE expiry_time < NOW()` | All shards (parallel) | ~100ms |
| `SELECT * WHERE creator_id = ?` | All shards (scatter-gather) | ~200ms |

For the `creator_id` query: if "show my URLs" becomes a frequent use case, add a **co-location table** or a separate lookup table: `CREATE TABLE user_urls (creator_id BIGINT, suffix CHAR(8))` distributed by `creator_id`.

---

## Companion Files

For deeper exploration of topics referenced in this document:

| Topic | File |
|---|---|
| Key generation approaches (hash, counter, SKIP LOCKED) | [key-generation-deep-dive.md](key-generation-deep-dive.md) |
| API contracts and error handling | [api-contracts.md](api-contracts.md) |
| System flows (write path, read path, failover) | [flow.md](flow.md) |
| Multi-layer caching and scaling iterations | [scaling-and-caching.md](scaling-and-caching.md) |
| Interview walkthrough with L5/L6 calibration | [interview-simulation.md](interview-simulation.md) |