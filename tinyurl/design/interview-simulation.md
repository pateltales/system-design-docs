# TinyURL System Design — Mock Interview Simulation

> **Setting**: Amazon Principal Engineer (L8) interviewing an SDE3 (L6) candidate
> **Duration**: 45 minutes
> **Problem**: Design a URL shortening service like TinyURL or Bitly

---

## 🎬 The Interview Begins

---

### Interviewer (Principal Engineer):

> "Thanks for joining. I'm [name], a Principal Engineer on the infrastructure team. For today's system design round, I'd like you to design a **URL shortening service** — something like TinyURL or Bitly. Users should be able to create short URLs and be redirected to the original long URL when they visit the short link. Take it wherever you think is appropriate for the scale we'd see at Amazon-level. Go ahead."

---

### Candidate Response — Phase 1: Clarifying Requirements (3-5 minutes)

> "Before I jump in, I'd like to clarify a few things about scope and requirements."

**Functional Requirements:**

> "So the core functionality is two APIs:
> 1. **Create**: Given a long URL, return a short URL (e.g., `tinyurl.com/abc12345`)
> 2. **Redirect**: Given a short URL, redirect the user (HTTP 301/302) to the original long URL
>
> A few clarifying questions:
> - **Should the same long URL always map to the same short URL?** Or is it okay if the same long URL gets different short URLs each time?"

**Interviewer**: "Good question. Different short URLs is fine — each creation is a new mapping."

> "- **Do short URLs expire?** Or do they live forever?"

**Interviewer**: "Let's say they expire after some configurable time — default 1 year."

> "- **Do we need user accounts / authentication?** Or is it anonymous?"

**Interviewer**: "Let's support optional user accounts — users can see their created URLs. But anonymous creation is also fine."

> "- **Do we need analytics?** Like click counts, geographic data?"

**Interviewer**: "Let's keep that out of scope for now. Focus on the core create and redirect."

> "- **Custom short URLs?** Can users pick their own alias like `tinyurl.com/my-brand`?"

**Interviewer**: "Nice to have but not critical. Mention it but don't deep dive."

> "Perfect. So to summarize:
> - `CreateShortURL(longURL, userId?, expiry?) → shortURL`
> - `RedirectShortURL(shortURL) → 301/302 redirect to longURL`
> - URLs expire (default 1 year, configurable)
> - Optional user association
> - No analytics for now"

---

### Interviewer's Internal Assessment:

✅ *Good — the candidate is clarifying scope before designing. They asked about idempotency (same long URL mapping), expiry, auth, and analytics. This shows they think about product requirements, not just tech. For L6, I expect them to drive this conversation.*

> **L5 vs L6 distinction**: An L5 asks 2-3 basic questions. An L6 drives the conversation proactively, surfaces edge cases (idempotency, expiry), and summarizes requirements as a contract before proceeding.

---

### Candidate Response — Phase 2: Scale Estimation (3-5 minutes)

> "Let me estimate the scale to drive our design decisions."

#### Traffic Estimates

> "I'll assume:
> - **URL creations**: 600 million per month
>   - That's 600M / (30 × 24 × 3600) ≈ **~230 writes/sec**
>   - Peak: maybe 3-5x → **~1,000 writes/sec peak**
>
> - **URL redirects (reads)**: 10 billion per month (assuming ~17:1 read:write ratio, typical for a read-heavy service)
>   - That's 10B / (30 × 24 × 3600) ≈ **~3,800 reads/sec**
>   - Peak: **~15,000 reads/sec**
>
> So this is a **read-heavy** system — about 17:1 read to write ratio."

#### Storage Estimates

> "For data sizing, each URL mapping has:
> - Short URL suffix: **8 bytes** (8 ASCII chars, 1 byte each)
> - Long URL: **~50 bytes** average (variable, but typical URLs are 30-80 chars)
> - Creator ID: **8 bytes** (BIGINT)
> - Expiry timestamp: **8 bytes** (Unix timestamp or DB TIMESTAMP)
> - Total: **~74 bytes per row**
>
> Over time:
> - 600M/month × 12 = 7.2 billion/year
> - Over 100 years (theoretical max): 720 billion rows
> - 720B × 74 bytes = **~48 TB of data**
>
> This is significant — we'll need to think about horizontal scaling."

#### Short URL Key Space

> "For the short URL suffix, I need to pick a character set and length:
> - Using `a-z` + `0-9` = **36 characters**
> - With **8 characters**: 36⁸ = **~2.8 trillion** possible combinations
> - We need 720 billion max → 2.8 trillion gives us ~4x headroom
> - That's plenty. 8 characters with base-36 works.
>
> If we wanted more headroom, we could use `a-z` + `A-Z` + `0-9` = 62 characters → 62⁸ = 218 trillion, but base-36 is simpler (case-insensitive URLs are more user-friendly) and sufficient."

---

### Interviewer's Internal Assessment:

✅ *Solid back-of-envelope math. They derived write/read rates, storage per row, and total storage. They justified the key length with capacity math. For L6, this is expected. I'd push back if they hand-waved the numbers.*

**Interviewer asks**: "Why not 7 characters? Or 6?"

> "Good question. 36⁷ = 78 billion, which covers 720 billion... wait, no. 36⁷ = 78 billion, but we need up to 720 billion. So 7 characters is NOT enough. We'd need 8 to get to 2.8 trillion. If we used base-62 (case-sensitive), 7 characters gives 62⁷ = 3.5 trillion, which would work. So it's a tradeoff: **8 chars base-36 (case-insensitive, user-friendly) vs 7 chars base-62 (shorter but case-sensitive)**. I'd lean toward 8 chars base-36 for usability."

---

### Interviewer's Internal Assessment:

✅ *Excellent recovery and tradeoff discussion. Shows they can think on their feet and reason about user-facing implications, not just pure math.*

> **L5 vs L6 distinction**: An L5 gets the math roughly right. An L6 uses the numbers to drive every subsequent decision — key length, cache size, shard count, server fleet. Every number calculated here should appear later in the design.

---

### Candidate Response — Phase 3: High-Level Design (5-7 minutes)

> "Let me sketch out the high-level architecture."

```
                            ┌─────────────┐
                            │   Clients   │
                            │ (browsers,  │
                            │  apps)      │
                            └──────┬──────┘
                                   │
                            ┌──────▼──────┐
                            │    Load     │
                            │  Balancer   │
                            └──────┬──────┘
                                   │
                    ┌──────────────┼──────────────┐
                    │              │              │
              ┌─────▼─────┐ ┌─────▼─────┐ ┌─────▼─────┐
              │ App Server│ │ App Server│ │ App Server│
              │    #1     │ │    #2     │ │    #3     │
              └─────┬─────┘ └─────┬─────┘ └─────┬─────┘
                    │              │              │
                    └──────────────┼──────────────┘
                                   │
                    ┌──────────────┼──────────────┐
                    │                             │
              ┌─────▼─────┐                ┌──────▼──────┐
              │   Redis   │                │ PostgreSQL  │
              │  (Cache)  │                │  (Primary   │
              └───────────┘                │   Storage)  │
                                           └──────┬──────┘
                                                  │
                                           ┌──────▼──────┐
                                           │   Read      │
                                           │  Replicas   │
                                           └─────────────┘
```

#### API Design

> "Two core endpoints:
> - `POST /api/v1/urls` — Create a short URL (returns the generated suffix)
> - `GET /{suffix}` — Redirect to the original long URL (302 by default)
>
> I'd use **302 (temporary redirect)** by default — it gives us control over analytics and handles URL expiry correctly. 301 would cache permanently in the browser, making expiry and updates impossible to enforce.
>
> 📄 *Full API specification including error codes, rate limiting, batch creation, and admin endpoints: see [api-contracts.md](api-contracts.md)*"

---

### Interviewer asks: "Walk me through the create flow in detail. How do you generate the short URL key?"

---

### Candidate Response — Phase 4: Key Generation Deep Dive (8-10 minutes)

> "This is the most interesting part of the design. Let me walk through three approaches, showing how each solves problems the previous one has."

---

#### Iteration 1: Hash the Long URL

> "The simplest approach: hash the long URL and use the first 8 characters as the suffix."

```
┌──────────┐      ┌───────────────┐      ┌──────────────┐
│  Client   │─────▶│  App Server   │─────▶│  PostgreSQL  │
│           │      │               │      │              │
│ POST /urls│      │ 1. MD5(url)   │      │ INSERT       │
│           │      │ 2. Take 8 ch  │      │ (suffix,url) │
│           │◀─────│ 3. Return     │◀─────│              │
└──────────┘      └───────────────┘      └──────────────┘
```

> **Pros**: Deterministic — same long URL → same short URL. Simple to implement.
>
> **Cons**:
> - **Collisions**: MD5 → 128 bits, truncated to 8 base-36 chars (~41 bits). With 720B URLs, birthday problem makes collisions **near-certain**
> - **Collision resolution**: On collision, append counter and re-hash → additional DB lookups, complexity
> - **Same URL, different users**: Two users shortening the same URL get the same short URL — may not be desired
>
> ❌ **Verdict**: Collision handling adds unacceptable complexity at scale.

**Interviewer Assessment**: *"The candidate identified the approach and its limitations quickly. An L5 might stop here. Let's see if they can propose something better."*

---

#### Iteration 2: Distributed Counter (Snowflake-style ID)

> "To eliminate collisions entirely, use a centralized or distributed counter."

```
┌──────────┐      ┌───────────────┐      ┌──────────────┐      ┌──────────────┐
│  Client   │─────▶│  App Server   │─────▶│  ID Service  │      │  PostgreSQL  │
│           │      │               │      │ (Snowflake)  │      │              │
│ POST /urls│      │ 1. Get next ID│─────▶│ counter++    │      │              │
│           │      │ 2. Base36(id) │      │              │      │ INSERT       │
│           │◀─────│ 3. Store + ret│─────────────────────▶─────▶│ (suffix,url) │
└──────────┘      └───────────────┘      └──────────────┘      └──────────────┘
```

> **Pros**: Guaranteed unique — no collisions ever. High throughput.
>
> **Cons**:
> - **Separate service**: Need to manage a counter/ID generation service (Snowflake, ZooKeeper-based)
> - **Predictability**: Sequential IDs → users can guess neighboring URLs (security concern)
> - **Coordination**: Multi-datacenter counter coordination adds complexity
> - **Two systems**: ID service + database = more operational burden
>
> ⚠️ **Verdict**: Works, but introduces architectural complexity. Can we do better?

**Interviewer Assessment**: *"Good improvement over hashing. The candidate identified the security concern with sequential IDs — that shows awareness. An L6 should now propose something that eliminates the extra service."*

---

#### Iteration 3: Pre-allocated Key Table + FOR UPDATE SKIP LOCKED ⭐

> "This is the approach I prefer. It combines key generation and storage into a single system."

```
┌──────────┐      ┌───────────────┐      ┌──────────────────────────────────┐
│  Client   │─────▶│  App Server   │─────▶│           PostgreSQL             │
│           │      │               │      │                                  │
│ POST /urls│      │ Single atomic │      │  ┌─────────────────────────────┐ │
│           │      │ SQL query     │      │  │ url_mappings                │ │
│           │      │               │      │  ├─────────────────────────────┤ │
│           │◀─────│               │◀─────│  │ Row 1: claimed (in use)     │ │
└──────────┘      └───────────────┘      │  │ Row 2: claimed              │ │
                                          │  │ Row 3: LOCKED by Req A      │ │
                                          │  │ Row 4: ← Req B skips here  │ │
                                          │  │ Row 5: ← Req C skips here  │ │
                                          │  │ Row 6: unclaimed            │ │
                                          │  │ ...                         │ │
                                          │  └─────────────────────────────┘ │
                                          └──────────────────────────────────┘
```

> "Pre-populate the database with a large working set of 8-character suffixes (e.g., 10 billion keys). Each row starts with `expiry_time = epoch (Jan 1, 1970)`, meaning it's unclaimed.
>
> When a user creates a short URL, we run this **single atomic SQL query**:"
>
> ```sql
> WITH candidate AS (
>   SELECT *
>   FROM url_mappings
>   WHERE expiry_time < NOW()
>   LIMIT 1
>   FOR UPDATE SKIP LOCKED
> )
> UPDATE url_mappings
> SET expiry_time = NOW() + INTERVAL '1 year',
>     creator_id = :userId,
>     long_url = :longUrl
> FROM candidate
> WHERE url_mappings.id = candidate.id
> RETURNING url_mappings.*;
> ```

> "Let me break down why this is elegant:
>
> 1. **`WHERE expiry_time < NOW()`** — finds any row that's either unclaimed (epoch) or expired
> 2. **`LIMIT 1`** — we only need one key
> 3. **`FOR UPDATE SKIP LOCKED`** — this is the secret sauce"

**Interviewer**: "Tell me more about `FOR UPDATE SKIP LOCKED`. Why is that important?"

> "Great question. Without `SKIP LOCKED`, here's what happens with concurrent requests:
>
> ```
> Request A: SELECT ... FOR UPDATE → locks Row 5
> Request B: SELECT ... FOR UPDATE → tries Row 5 → BLOCKED! Waits for A to commit
> Request C: SELECT ... FOR UPDATE → tries Row 5 → BLOCKED! Waits for A to commit
> ```
>
> This creates a **bottleneck** — all concurrent writes serialize on the same row.
>
> With `SKIP LOCKED`:
>
> ```
> Request A: SELECT ... FOR UPDATE SKIP LOCKED → locks Row 5
> Request B: SELECT ... FOR UPDATE SKIP LOCKED → Row 5 locked, SKIP → locks Row 6
> Request C: SELECT ... FOR UPDATE SKIP LOCKED → Rows 5,6 locked, SKIP → locks Row 7
> ```
>
> ```
> Row 1 - claimed (in use, not expired)
> Row 2 - claimed
> Row 3 - claimed
> Row 4 - locked by Request A (being claimed right now)
> Row 5 - unclaimed ← Request B skips to here
> Row 6 - unclaimed ← Request C skips to here
> Row 7 - unclaimed
> ...
> ```
>
> **Zero contention**. Each concurrent request gets its own row without blocking. This is a PostgreSQL feature (also available in MySQL 8+ and Oracle).
>
> **Why I prefer this over the other approaches**:
> - No collisions (each row is unique by definition)
> - No separate key generation service (the DB IS the key generator)
> - Expired URLs are automatically recycled (their rows become available again)
> - Single atomic operation (no race conditions)
> - No coordination across app servers needed"

---

### Interviewer's Internal Assessment:

✅ *Strong. The candidate compared three approaches with clear tradeoffs, chose one and justified it deeply. The `SKIP LOCKED` explanation shows they understand concurrency at a systems level. For L6, this level of depth on the core algorithm is expected.*

**Interviewer pushes back**: "What about the initial data load? You said 'pre-populate all possible 8-character keys.' That's 2.8 trillion rows. You can't actually insert 2.8 trillion rows into a database."

---

### Candidate Handles the Pushback

> "You're absolutely right — I should clarify. We would NOT pre-populate all 2.8 trillion keys. That would be ~200 TB just for the keys alone. Instead, we have two practical options:
>
> **Option A: Pre-populate a working set**
> - Generate, say, 10 billion keys upfront (enough for ~1.5 years at 600M/month)
> - Have a background job that generates more keys when the available pool drops below a threshold
> - Keys are generated offline using a deterministic algorithm (e.g., sequential base-36 counter, or random generation with uniqueness check)
>
> **Option B: Generate keys on-demand**
> - When a new URL is created, generate a random 8-char string
> - INSERT into the DB with a UNIQUE constraint on the suffix
> - If collision (UNIQUE violation), retry with a new random string
> - At 720B used out of 2.8T possible, collision probability is ~25% when mostly full — but early on it's negligible
> - Could combine with the `FOR UPDATE SKIP LOCKED` pattern by checking expiry
>
> **I'd go with Option A** — pre-populate a reasonable working set (billions of rows, not trillions) and refill as needed. This gives us the clean `SKIP LOCKED` pattern without the cost of 2.8 trillion rows."

---

### Interviewer's Internal Assessment:

✅ *Handled the pushback well. Acknowledged the flaw, proposed two fixes, chose one with reasoning. This is what I look for in L6 — they don't crumble under pushback, they adapt.*

---

#### Evolution Summary Table

| Approach | Collision Risk | Latency | External Dependencies | Operational Complexity |
|---|---|---|---|---|
| Hash-based | High (birthday problem) | ~5ms (hash + insert + retry) | None | Low (but collision handling is complex) |
| Distributed Counter | Zero | ~7ms (ID service + insert) | ID Service (Snowflake/ZK) | Medium (separate service to manage) |
| **Pre-allocated + SKIP LOCKED** ⭐ | **Zero** | **~3ms (single query)** | **None** | **Low (DB is the key generator)** |

> **Why Iteration 3 wins**: Single system (PostgreSQL), single atomic query, zero collisions, zero contention, automatic key recycling when URLs expire. The DB *is* the key generator — no external service needed.
>
> 📄 *Deep dive into SKIP LOCKED mechanics, MVCC internals, collision probability math, and pre-population strategy: see [key-generation-deep-dive.md](key-generation-deep-dive.md)*

---

### Candidate Response — Phase 5: Read Path & Caching (5-7 minutes)

> "Now let me walk through the **read path** — what happens when a user visits a short URL."

#### Read Flow

> ```
> 1. User visits https://tinyurl.com/ab3k9x12
> 2. Load balancer routes to an app server
> 3. App server checks Redis cache: GET "ab3k9x12"
>    ├── Cache HIT: Return the longURL, send 302 redirect
>    └── Cache MISS:
>        4. Query PostgreSQL: SELECT longURL FROM URLMappings WHERE suffix = 'ab3k9x12'
>        5. If found and not expired:
>            - Write to Redis: SET "ab3k9x12" → longURL (with TTL matching expiry)
>            - Return 302 redirect to longURL
>        6. If not found or expired:
>            - Return 404
> ```

#### Why Cache? (Redis)

> "The read:write ratio is 17:1, and URL access follows a **power-law / Zipf distribution** — a small percentage of URLs get the majority of traffic. Think of a viral tweet with a shortened link — that one URL might get millions of hits.
>
> **Cache sizing**:
> - If 1% of URLs account for ~50% of traffic (typical Zipf distribution)
> - 720B total URLs × 1% = 7.2B hot keys
> - 7.2B × ~66 bytes (8 byte key + 50 byte URL + overhead) ≈ **~475 GB**
> - This fits in a Redis cluster (a few nodes with 128GB each)
>
> **Caching strategy: Cache-Aside (Look-Aside)**:
> - App checks cache first
> - On miss, queries DB and populates cache
> - I prefer this over read-through because it gives us control over TTLs and invalidation
>
> **Cache eviction**:
> - TTL-based: Set Redis TTL to match URL expiry time
> - LRU eviction for memory pressure: least recently used URLs get evicted first — they'll be re-cached on next access"

---

### Interviewer asks: "What about cache invalidation? What if a URL is updated or expires?"

> "Good question. For TinyURL, this is actually simple because:
>
> 1. **URLs are essentially immutable** — once created, the mapping doesn't change (unlike a user profile)
> 2. **Expiry handling**: We set the Redis TTL to match the URL expiry. When the TTL expires, Redis auto-evicts it. Next access hits DB, which also checks expiry.
> 3. **If we allow URL updates** (rare case): We'd invalidate the cache entry on write — `DEL "ab3k9x12"` in Redis after updating PostgreSQL. Next read repopulates the cache.
>
> Cache invalidation is usually the hardest problem in distributed systems, but TinyURL's immutable-write-once data model makes it straightforward."

---

#### Iterative Scaling — How Caching Reduces Infrastructure

> "Let me show how each caching layer reduces our infrastructure requirements."

**Iteration 1: No Cache (DB Only)**
```
Client → App Server → PostgreSQL Replicas
```
> - 15K peak reads/sec, each hitting PostgreSQL
> - Need ~3 replicas (each handles ~5K reads/sec) + leader = **4 DB nodes + app servers**
> - Total: ~12 servers (4 DB + 8 app)
> - **Cost: High, every read hits disk or buffer pool**

**Iteration 2: + Redis Cache**
```
Client → App Server → Redis (80% hit) → PostgreSQL (20% miss)
```
> - 80% cache hit rate (Zipf distribution — popular URLs cached)
> - PostgreSQL sees only 3K reads/sec → 1 replica sufficient
> - Total: ~8 servers (2 DB + 3 Redis + 3 app)
> - **43% fewer servers, 80% less DB load**

**Iteration 3: + CDN (for 301 permanent URLs)**
```
Client → CDN (50% hit) → App Server → Redis → PostgreSQL
```
> - CDN handles 50% of remaining traffic (repeat visitors to same URLs)
> - Tricky: 302 redirects aren't CDN-cacheable by default. Options:
>   1. Use 301 for permanent URLs (CDN caches them)
>   2. Use `Cache-Control: public, max-age=300` with 302
>   3. Edge workers (Lambda@Edge / Cloudflare Workers)
> - Total: ~5 servers (2 DB + 2 Redis + 1 app) + CDN
> - **58% fewer servers than Iteration 2**

**Iteration 4: + Browser Cache + Multi-Region**
```
Browser Cache → CDN → Regional App Server → Regional Redis → DB (leader in primary region)
```
> - 301 = browser caches forever; 302 + `Cache-Control: max-age=300` = 5 min local cache
> - Repeat clicks from same user never hit our infrastructure
> - Multi-region: each region has local Redis + read replica
> - Total per region: ~4 servers (1 DB replica + 1 Redis + 2 app) + CDN
> - **Minimal infrastructure per region, global low latency**

##### Scaling Evolution Summary

| Iteration | Cache Layers | Peak DB Reads/sec | Server Count | Monthly Cost Est. |
|---|---|---|---|---|
| 1. No cache | None | 15,000 | ~12 | ~$8,000 |
| 2. + Redis | Redis | 3,000 | ~8 | ~$5,500 |
| 3. + CDN | Redis + CDN | 1,500 | ~5 + CDN | ~$3,500 |
| 4. + Browser + Multi-Region | All layers | ~300/region | ~4/region | ~$2,500/region |

> This progression shows how each caching layer **multiplicatively** reduces load on the database. This is exactly the kind of iterative thinking interviewers want to see — start simple, add complexity only when justified by numbers.
>
> 📄 *Detailed cache sizing (Zipf math), Redis cluster design, hot key analysis, and CDN configuration: see [scaling-and-caching.md](scaling-and-caching.md)*

---

### Interviewer's Internal Assessment:

✅ *Excellent read path design. The cache sizing with Zipf distribution shows quantitative thinking. The iterative scaling progression demonstrates that the candidate can reason about cost and infrastructure tradeoffs at each layer — exactly what we expect at L6.*

> **L5 vs L6 distinction**: An L5 says "add Redis." An L6 shows the quantitative impact of each caching layer, sizes the cache using Zipf distribution, and explains why 302 redirects complicate CDN caching.

---

### Candidate Response — Phase 6: Database Design & Scaling (5-7 minutes)

#### Database Choice: PostgreSQL

> "I'm choosing PostgreSQL because:
> 1. **`FOR UPDATE SKIP LOCKED`** — core to our key generation strategy
> 2. **B-Tree index** on the short URL suffix — O(log n) lookups for reads
> 3. **ACID transactions** — guarantees no duplicate key assignment
> 4. **Mature replication** — single-leader replication for read scaling
> 5. **Citus extension** — horizontal sharding when we outgrow a single node"

#### Table Schema

> ```sql
> CREATE TABLE url_mappings (
>     id          BIGSERIAL PRIMARY KEY,
>     suffix      CHAR(8) NOT NULL UNIQUE,
>     long_url    VARCHAR(2048),
>     creator_id  BIGINT,
>     expiry_time TIMESTAMP NOT NULL DEFAULT '1970-01-01',
>     created_at  TIMESTAMP DEFAULT NOW()
> );
>
> CREATE INDEX idx_suffix ON url_mappings(suffix);
> CREATE INDEX idx_expiry ON url_mappings(expiry_time) WHERE expiry_time < NOW();
> ```
>
> **Key indexes**:
> - `idx_suffix`: B-Tree on suffix for fast read lookups
> - `idx_expiry`: **Partial index** on `expiry_time` — only indexes expired rows. This makes the `WHERE expiryTime < NOW()` query in our key generation fast, without indexing the billions of active rows.

#### Replication

> "For read scaling, we use **single-leader replication** with WAL streaming:
> - **Leader** handles all writes (key generation + URL creation)
> - **Followers** handle reads (URL lookups for redirects)
> - At 230 writes/sec, a single leader is fine
> - At 3,800 reads/sec (after cache), followers easily handle this
> - **Replication lag** is acceptable — a URL created 100ms ago not being readable on a follower is a non-issue (the user hasn't even shared the link yet)"

#### Sharding (for 48TB)

> "When a single PostgreSQL node can't hold all the data (typically >5-10TB), we shard:
>
> **Shard key**: The `suffix` column — hash-based sharding
> - `shard_id = hash(suffix) % num_shards`
> - This distributes data evenly since suffixes are random
>
> **Using Citus** (PostgreSQL extension):
> - Citus handles shard routing transparently
> - App servers still write normal SQL — Citus routes to the correct shard
> - With 48TB and 10 shards: ~5TB per shard — manageable
>
> **Why shard by suffix and not by creator_id**:
> - Reads are always by suffix (`GET /ab3k9x12` → lookup by suffix)
> - If we sharded by creator_id, every read would need to broadcast to all shards (scatter-gather) — terrible for latency
> - Sharding by suffix means reads hit exactly one shard"

> 📄 *Detailed schema rationale (column-by-column), index size calculations, and Citus sharding DDL: see [sql-vs-nosql-tradeoffs.md](sql-vs-nosql-tradeoffs.md)*
> 📄 *SQL vs NoSQL decision framework with 7 tradeoffs and real-world examples: see [sql-vs-nosql-tradeoffs.md](sql-vs-nosql-tradeoffs.md)*

---

### Interviewer's Internal Assessment:

✅ *Excellent database design. The partial index on expiry_time is a sophisticated optimization. Sharding justification with the scatter-gather tradeoff shows deep understanding. This is L6+ territory.*

**Interviewer asks**: "What about availability? What happens if the leader goes down?"

---

### Candidate Response — Phase 7: Availability & Fault Tolerance (3-5 minutes)

> "If the leader goes down:
>
> **Automatic failover**:
> - One follower is promoted to leader (PostgreSQL doesn't do this natively — we'd use **Patroni** or **pg_auto_failover** for automated failover)
> - During failover (typically 10-30 seconds):
>   - **Reads continue working** — followers still serve read queries
>   - **Writes fail** — no new URLs can be created for 10-30 seconds
>   - This is acceptable — returning a 503 for URL creation for 30 seconds is fine
>
> **Multi-region deployment** (for global availability):
> ```
> ┌──────────────────┐           ┌──────────────────┐
> │   US-EAST        │           │   EU-WEST        │
> │                  │           │                  │
> │  Leader ────────────sync────▶  Follower         │
> │  + Followers     │  repl.   │  + Followers      │
> │  + Redis         │           │  + Redis          │
> │  + App Servers   │           │  + App Servers    │
> └──────────────────┘           └──────────────────┘
> ```
>
> - Leader in one region, async followers in other regions
> - Reads served locally in each region (low latency)
> - Writes route to the leader region (slightly higher latency for non-leader regions)
> - If the leader region goes down entirely, promote a follower in another region
>
> **Alternative: Multi-leader replication**
> - Each region has its own leader
> - But this creates write conflicts: two regions might claim the same expired key simultaneously
> - Resolution: partition the key space by region (Region A gets keys starting with a-m, Region B gets n-z)
> - More complex but provides true active-active"

> 📄 *Detailed failover flows including circuit breaker state machines and multi-region redirect sequences: see [flow.md](flow.md)*

---

### Candidate Response — Phase 8: Wrap-Up & Summary (2-3 minutes)

> "Let me summarize the complete design:
>
> **Core Components**:
> 1. **App Servers** (stateless, behind load balancer) — handle HTTP requests
> 2. **PostgreSQL** (with Citus sharding) — primary data store, also serves as the key generation engine
> 3. **Redis** (cache-aside) — caches hot URLs, ~475GB across a few nodes
> 4. **Read Replicas** — serve the read-heavy traffic
>
> **Write Path** (Create URL):
> - App server → PostgreSQL leader → atomic `FOR UPDATE SKIP LOCKED` query → claims expired/unused row → returns short URL
>
> **Read Path** (Redirect):
> - App server → Redis (cache hit?) → if miss → PostgreSQL follower → populate Redis → 302 redirect
>
> **Key Design Decisions**:
> - 8-character base-36 suffix (2.8T possibilities for 720B needed)
> - `FOR UPDATE SKIP LOCKED` for contention-free key generation
> - PostgreSQL for ACID guarantees + mature ecosystem
> - Cache-aside Redis for hot URLs (Zipf distribution)
> - Shard by suffix for single-shard reads
> - Single-leader replication with automated failover
>
> **Potential Extensions** (if we had more time):
> - Click analytics (async event stream → Kafka → analytics DB)
> - Custom short URLs (check availability, reserve in same table)
> - Rate limiting (per-user, per-IP via Redis counters)
> - Link preview / safety check (async URL scanning for malware)"

---

### Interviewer's Final Assessment

| Criteria | Rating | Notes |
|---|---|---|
| **Requirements Gathering** | ✅ Strong | Drove the conversation, asked about expiry, idempotency, analytics scope |
| **Scale Estimation** | ✅ Strong | Correct math, justified key length, storage estimates |
| **High-Level Design** | ✅ Strong | Clean architecture, clear read/write paths |
| **Core Algorithm** | ✅ Excellent | `FOR UPDATE SKIP LOCKED` with deep concurrency understanding |
| **Database Design** | ✅ Excellent | Partial index, sharding justification, replication |
| **Caching** | ✅ Strong | Zipf distribution, cache-aside, TTL-based invalidation |
| **Tradeoff Discussion** | ✅ Strong | Compared 3 key gen approaches, SQL vs NoSQL reasoning |
| **Handling Pushback** | ✅ Strong | Pre-population correction handled gracefully |
| **Availability** | ✅ Good | Failover, multi-region, multi-leader tradeoffs |
| **Communication** | ✅ Strong | Structured, clear, drove the discussion |

**Overall**: **Strong Hire for L6 (SDE3)**

**Reasoning**: The candidate demonstrated depth in the core algorithm (key generation with `SKIP LOCKED`), made sound database choices with clear justification, handled scale estimation correctly, and adapted well to pushback. The multi-region availability discussion shows they think beyond single-data-center designs. The only area to probe further would be operational experience — how they'd monitor this in production, handle data migrations, etc.

---

## 📝 Key Differences Between L5 and L6 Expectations in This Interview

| Aspect | L5 (SDE2) Expectation | L6 (SDE3) Expectation |
|---|---|---|
| **Requirements** | Asks basic clarifying questions | Drives the conversation, proactively identifies scope |
| **Scale** | Gets the math roughly right | Precise math, uses it to drive every design decision |
| **Key Generation** | Proposes hashing or counters | Compares multiple approaches with tradeoffs, picks best |
| **Database** | "Use PostgreSQL" | Explains WHY (B-Tree, ACID, Citus), discusses index strategy |
| **Caching** | "Add Redis" | Sizes the cache, explains eviction strategy, Zipf distribution |
| **Pushback** | May struggle to adapt | Acknowledges flaw, proposes alternatives smoothly |
| **Availability** | Mentions "add replicas" | Discusses failover automation, multi-region, consistency tradeoffs |
| **Scope** | Solves the problem given | Proactively identifies extensions and future concerns |
| **Iterative Scaling** | "Add Redis" | Shows 4-iteration progression with quantitative server reduction at each layer |
| **System Evolution** | Presents final architecture directly | Builds up from simple to complex, explaining why each addition is needed |

---

## Companion Deep-Dive Files

This interview simulation provides the high-level narrative. For production-depth detail, see:

| Topic | File | Key Content |
|---|---|---|
| Key Generation | [key-generation-deep-dive.md](key-generation-deep-dive.md) | SKIP LOCKED mechanics, MVCC internals, collision math, pre-population strategy |
| API Specification | [api-contracts.md](api-contracts.md) | Full request/response schemas, error codes, rate limiting, batch & admin APIs |
| System Flows | [flow.md](flow.md) | 8 detailed flows with sequence diagrams, latency budgets, failure scenarios |
| Caching & Scaling | [scaling-and-caching.md](scaling-and-caching.md) | Zipf math, Redis sizing, CDN strategy, sharding topology, capacity planning |
| Database Tradeoffs | [sql-vs-nosql-tradeoffs.md](sql-vs-nosql-tradeoffs.md) | 7-tradeoff framework, schema rationale, index deep dive, Citus setup |
