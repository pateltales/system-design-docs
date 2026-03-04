# System Design Interview Simulation: Design Amazon S3 (Object Store)

> **Interviewer:** Principal Engineer (L8), Amazon S3 Team
> **Candidate Level:** SDE-2 (L5 — Software Development Engineer)
> **Duration:** ~60 minutes
> **Date:** February 12, 2026

---

## PHASE 1: Opening & Problem Statement (~2 min)

**Interviewer:**
Hey, welcome. I'm [name], Principal Engineer on the S3 storage team. For today's system design round, I'd like you to design an **object storage system** — think Amazon S3. A system where clients can store arbitrary blobs of data (from a few bytes to terabytes), organize them in buckets, and retrieve them by key. We're talking about the core infrastructure, not just an API wrapper.

I care about how you think through durability, scale, and the tradeoffs involved in building something that stores *the world's data*. I'll push on your decisions — that's me calibrating depth, not a negative signal.

Take it away.

---

## PHASE 2: Requirements Gathering & Scoping (~5 min)

**Candidate:**
Thanks! S3 is a massive system, so before I draw anything, let me scope this down. "Object storage" spans a wide design space — I want to nail down what we're building.

**Functional Requirements — what operations do we need?**

> "The core API operations I'd expect:
> - **PUT Object** — upload an object (a blob of data) to a bucket with a given key.
> - **GET Object** — retrieve an object by bucket + key.
> - **DELETE Object** — remove an object.
> - **HEAD Object** — get metadata without downloading the body.
> - **LIST Objects** — list objects in a bucket, optionally filtered by prefix.
>
> A few clarifying questions:
> - **Do we need to support multipart upload?** For large objects (multi-GB), uploading in a single request is impractical."

**Interviewer:** "Yes, multipart upload is important. S3 objects can be up to 5 TB, and multipart upload is how we handle anything over ~100 MB."

> "- **Versioning?** Should we support keeping multiple versions of the same object?"

**Interviewer:** "Mention it, but don't deep dive. Focus on the core read/write path first."

> "- **Is this flat key-value, or do we have real directories?**"

**Interviewer:** "This is an important point. What's your understanding?"

> "S3 has a **flat namespace** — there are no real directories. A key like `photos/2024/vacation/img001.jpg` is just a string. The `/` delimiter is a convention, and the LIST API supports prefix + delimiter to *simulate* directory traversal. But internally, it's bucket + key → object. No hierarchy in the storage layer."

**Interviewer:** "Exactly right. That's an important design decision — keep going."

> "- **What about object metadata?** User-defined key-value metadata attached to objects?"

**Interviewer:** "Yes, support user metadata — but it's small (up to 2 KB), so it's a metadata concern, not a storage concern."

**Non-Functional Requirements:**

> "Now the critical constraints. S3 is defined by its non-functional properties even more than its API:
>
> | Dimension | Requirement | Rationale |
> |---|---|---|
> | **Durability** | 99.999999999% (11 9's) | An object stored in S3 should essentially never be lost. This is S3's #1 promise. |
> | **Availability** | 99.99% (4 9's) for Standard tier | ~52 minutes of downtime per year is acceptable; data loss is not. |
> | **Consistency** | Strong read-after-write | After a successful PUT, any subsequent GET must return the new object — no stale reads. |
> | **Scalability** | Trillions of objects, exabytes of storage | S3 stores over 350 trillion objects as of 2024. Must scale horizontally without limit. |
> | **Latency** | First-byte latency < 100ms for Standard tier | GET/PUT should return the first byte quickly; throughput matters for large objects. |
> | **Object Size** | 0 bytes to 5 TB per object | Small config files to massive video files — same API. |
> | **Multi-tenancy** | Millions of customers sharing infrastructure | One customer's workload must not impact others. |

**Interviewer:**
You mentioned strong read-after-write consistency. Do you know the history there? S3 wasn't always strongly consistent.

**Candidate:**
> "Right. Until December 2020, S3 was eventually consistent for overwrite PUTs and DELETEs. You could PUT a new version, do a GET immediately, and get the old version back. New object PUTs were read-after-write consistent (you'd never get a 404 after a successful PUT of a new key), but overwrites and deletes could return stale data.
>
> In December 2020, Amazon announced strong read-after-write consistency for all S3 operations — at no extra cost, no performance penalty. They achieved this by adding a consistency subsystem to the metadata layer — essentially a 'witness' protocol that ensures any read checks that it has the latest version before returning. This was a massive engineering effort because S3 couldn't sacrifice availability or performance to gain consistency."

**Interviewer:**
Good. That consistency change is one of the most significant architectural evolutions in S3's history. We'll dive deeper later. Let's get some numbers.

---

### L5 vs L6 vs L7 — Phase 2 Rubric

| Aspect | L5 (SDE-2) | L6 (SDE-3) | L7 (Principal) |
|---|---|---|---|
| **Functional Reqs** | Lists CRUD operations | Proactively raises multipart, versioning, flat namespace | Additionally discusses cross-region replication, lifecycle, access patterns |
| **Non-Functional** | Mentions durability and availability | Quantifies 11 9's, explains strong consistency history | Frames NFRs in terms of business impact and SLA commitments |
| **Scoping** | Accepts problem as given | Drives clarifying questions | Negotiates scope based on time constraints, proposes phased approach |

---

## PHASE 3: Scale Estimation (~3 min)

**Candidate:**

> "Let me estimate S3-scale numbers to ground our design decisions."

#### Storage Estimates

> "S3 reportedly stores over 350 trillion objects and handles millions of requests per second. Let me work with a subset that's still massive:
>
> - **Total objects stored**: 100 trillion (10^14)
> - **Average object size**: 256 KB (skewed — many small objects, fewer large ones)
> - **Total raw data**: 100 × 10^12 × 256 KB = **25.6 exabytes** of raw data
> - **With erasure coding overhead** (~1.4x): ~**36 exabytes** of actual storage
> - **Versus 3x replication**: would be 76.8 EB — erasure coding saves ~40 EB"

#### Request Traffic

> "- **GET requests**: 100 million per second at peak
> - **PUT requests**: 10 million per second at peak
> - **LIST requests**: 1 million per second (expensive — scans metadata index)
> - **Read:Write ratio**: ~10:1 (heavily read-skewed, like most storage systems)"

#### Bandwidth

> "- **Read bandwidth**: Assuming average GET returns 256 KB: 100M × 256 KB = **25.6 TB/sec** outbound
> - **Write bandwidth**: 10M × 256 KB = **2.56 TB/sec** inbound
> - These are enormous numbers — this is why S3 spans multiple data centers and availability zones."

#### Metadata

> "Each object needs metadata in the index:
> - Bucket name + object key: ~256 bytes average
> - Object metadata (ETag, size, storage class, version, ACL, creation time): ~512 bytes
> - Storage location pointers (which chunks, where): ~256 bytes
> - **Total per object**: ~1 KB of metadata
> - **Total metadata**: 100 trillion × 1 KB = **100 PB** of metadata
>
> This metadata layer is itself a massive distributed system."

**Interviewer:**
Good. The metadata layer being 100 PB is a critical point — some people forget that the index is its own scaling challenge. Let's architect this.

---

## PHASE 4: High-Level Architecture (~5 min)

**Candidate:**

> "Let me start with the simplest thing that works, then we'll find the problems and fix them."

#### Attempt 0: Single Server

> "Simplest possible design — one machine with a big disk:
>
> ```
>     Client
>       │
>       ▼
>   ┌────────────────────┐
>   │   Single Server    │
>   │                    │
>   │   PUT bucket/key   │
>   │   → write file to  │
>   │   /data/bucket/key │
>   │                    │
>   │   GET bucket/key   │
>   │   → read file from │
>   │   /data/bucket/key │
>   │                    │
>   │   Local Disk (HDD) │
>   └────────────────────┘
> ```
>
> The bucket is a directory, the key is the file path. Simple, works on day one."

**Interviewer:**
What happens when that server's disk dies?

**Candidate:**

> "Everything is gone. Zero durability. And we can't scale beyond one machine's storage capacity. We need redundancy."

#### Attempt 1: Replicated Servers

> "OK, let's add redundancy. Three servers, each in a different Availability Zone. Every PUT writes to all three:
>
> ```
>     Client
>       │
>       ▼
>   ┌──────────┐
>   │ Gateway  │ ─── routes PUTs to all 3, GETs to any 1
>   └────┬─────┘
>        │
>    ┌───┼──────────────────┐
>    │   │                  │
>    ▼   ▼                  ▼
>  ┌──────┐  ┌──────┐  ┌──────┐
>  │ AZ-a │  │ AZ-b │  │ AZ-c │
>  │Server│  │Server│  │Server│
>  │ Copy │  │ Copy │  │ Copy │
>  │  1   │  │  2   │  │  3   │
>  └──────┘  └──────┘  └──────┘
> ```
>
> Now we survive a disk failure or even an AZ going down."

**Interviewer:**
Better. But I see two problems — can you identify them?

**Candidate:**

> "Yes:
>
> 1. **3x storage cost** — We're storing 3 full copies of every object. At exabyte scale, that's 2 extra exabytes we're paying for.
>
> 2. **This doesn't scale** — We can only store as much as one server's disk capacity. When we have trillions of objects, we need to spread data across thousands of machines. But then how does the front-end know *which* of the thousands of machines has the data for a given key?
>
> That second problem is the key insight: **knowing where the data is** and **storing the data** are two fundamentally different problems."

#### Attempt 2: Separate Metadata from Data

> "This is the crucial architectural decision. Let me split the system into layers:
>
> - **Metadata layer** answers: 'Where is the data for bucket+key?'
> - **Data layer** answers: 'Here are the bytes, stored durably.'
> - **Front-end layer** handles: authentication, authorization, routing
>
> These concerns scale completely differently:
>
> | Concern | Size | Access Pattern | Consistency Need |
> |---------|------|---------------|-----------------|
> | Metadata | Small (~1 KB/object) | Fast point lookups + range scans (LIST) | Must be strongly consistent |
> | Data | Large (KB to TB/object) | Sequential reads/writes | Immutable once written — no consistency issues |
> | Front-end | Stateless | High fan-out | None (stateless) |
>
> Here's the architecture with naive-but-correct choices for each layer:
>
> ```
>                            ┌──────────────────────┐
>                            │       Clients         │
>                            │  (SDKs, CLI, Console) │
>                            └──────────┬───────────┘
>                                       │ HTTPS (REST API)
>                            ┌──────────▼───────────┐
>                            │    Front-End Layer    │
>                            │  (Stateless fleet)    │
>                            │  Auth (SigV4)         │
>                            │  Rate limiting        │
>                            │  Request routing      │
>                            └──────────┬───────────┘
>                                       │
>                    ┌──────────────────┼──────────────────┐
>                    │                                     │
>         ┌──────────▼─────────┐            ┌─────────────▼────────────┐
>         │  Metadata Layer    │            │   Data Layer             │
>         │                    │            │   (Storage Nodes)        │
>         │  Replicated DB     │            │                          │
>         │  bucket+key →      │            │   3x replication         │
>         │  {size, etag,      │            │   across 3 AZs           │
>         │   storage_location,│            │                          │
>         │   metadata}        │            │   AZ-a   AZ-b   AZ-c    │
>         │                    │            │   ┌───┐  ┌───┐  ┌───┐   │
>         │  (for now: a       │            │   │   │  │   │  │   │   │
>         │   replicated DB    │            │   └───┘  └───┘  └───┘   │
>         │   — we'll improve  │            │                          │
>         │   this)            │            │   (for now: full copies  │
>         │                    │            │    — we'll improve this) │
>         └────────────────────┘            └──────────────────────────┘
> ```
>
> **How a PUT works in this design:**
> 1. Client sends `PUT my-bucket/photo.jpg` to front-end
> 2. Front-end authenticates (SigV4), checks bucket policy
> 3. Front-end sends the data to 3 storage nodes (one per AZ) — they each store a full copy
> 4. Front-end writes metadata to the metadata DB: `(my-bucket, photo.jpg) → {size: 2MB, etag: 'abc', locations: [AZ-a:node7, AZ-b:node14, AZ-c:node22]}`
> 5. Returns 200 OK to client
>
> **How a GET works:**
> 1. Client sends `GET my-bucket/photo.jpg` to front-end
> 2. Front-end looks up metadata: where are the copies?
> 3. Fetches data from the nearest/fastest storage node
> 4. Streams bytes back to client"

**Interviewer:**
Good — I like that you separated metadata from data. That's the right architectural instinct. But I see several things we need to improve. The replicated DB won't scale to 100 trillion objects. The 3x replication is expensive. And what about consistency — what happens if a client PUTs an object and another client immediately GETs it?

**Candidate:**

> "Exactly — three areas to improve:
>
> | Layer | Current (Naive) | Problem |
> |-------|----------------|---------|
> | **Metadata** | Replicated DB | Can't handle 100M lookups/sec or 100 PB of index data |
> | **Data** | 3x replication | 3x storage cost — unsustainable at exabyte scale |
> | **Consistency** | Eventual (caches may serve stale data) | Violates our strong read-after-write requirement |
>
> Let's deep-dive each one and fix them."

**Interviewer:**
Let's start with the metadata layer — that's the brain of the system.

---

## PHASE 5: Deep Dive — Metadata & Indexing (~10 min)

**Candidate:**

> "In our current design, the metadata layer is a replicated database. Let's stress-test that choice against our scale numbers:
> - **100+ million lookups/sec** — can a single DB handle that? No.
> - **100 PB of index data** (100 trillion objects × 1 KB each) — can't fit on one machine.
> - Need both **point lookups** (GET by key) and **range scans** (LIST by prefix)
> - Must **auto-scale** as buckets grow from 0 to billions of objects
>
> Our replicated DB is going to crumble under this. Let me evolve it."

#### Attempt 0: Our Current Design — Single Replicated Database

> "Here's what we have — a replicated relational database. Table schema:
>
> ```
> objects (
>     bucket_name   VARCHAR,
>     object_key    VARCHAR,
>     version_id    VARCHAR,
>     etag          VARCHAR,
>     size          BIGINT,
>     storage_class VARCHAR,
>     chunk_map     BLOB,    -- list of chunk IDs and locations
>     user_metadata JSON,
>     created_at    TIMESTAMP,
>     PRIMARY KEY (bucket_name, object_key)
> )
> ```
>
> **Why this fails:**
> - A single database cannot handle 100M lookups/sec
> - 100 PB of index data doesn't fit on one machine
> - Single point of failure — violates our availability requirement
> - No horizontal scalability"

#### Better: Distributed Key-Value Store (Sharded)

> "Shard the metadata across many nodes using consistent hashing on `hash(bucket_name + object_key)`.
>
> ```
>     hash(bucket + key) → partition → node
>
>     Partition 0: keys with hash [0, 1000)       → Node A
>     Partition 1: keys with hash [1000, 2000)     → Node B
>     Partition 2: keys with hash [2000, 3000)     → Node C
>     ...
> ```
>
> **This is better, but has problems:**"

**Interviewer:**
What problems?

**Candidate:**

> "Two major issues:
>
> 1. **Hot buckets** — Some S3 buckets have billions of objects and receive millions of requests/sec (think a major CDN origin, or a data lake). If we hash by `bucket + key`, all objects in a hot bucket are spread across partitions uniformly — that's actually good for point lookups. But LIST requests by prefix become a nightmare because keys with the same prefix are scattered across random partitions. You'd need to fan out a LIST to every partition and merge results.
>
> 2. **Partition sizing** — With hash-based partitioning, we can't split a single hot partition without rehashing everything. If one partition gets 10x the traffic, we're stuck."

#### S3's Approach: Prefix-Based Auto-Partitioning

> "S3 solves this with **prefix-based auto-partitioning**:
>
> ```
> Instead of:  hash(bucket + key) → partition    (hash-based)
> S3 uses:     prefix(bucket + key) → partition   (range-based on key prefix)
>
> Initial state (new bucket):
>   All keys in bucket 'my-bucket' → single partition P0
>
> As traffic grows, S3 auto-splits:
>   P0: my-bucket/a* through my-bucket/m*
>   P1: my-bucket/n* through my-bucket/z*
>
> Further splits as hot prefixes emerge:
>   P0: my-bucket/a* through my-bucket/f*
>   P1: my-bucket/g* through my-bucket/m*
>   P2: my-bucket/n* through my-bucket/s*
>   P3: my-bucket/t* through my-bucket/z*
> ```
>
> **Why prefix-based is better for S3:**
>
> | Aspect | Hash-Based | Prefix-Based (S3's approach) |
> |---|---|---|
> | Point lookups | O(1) — excellent | O(log P) — lookup partition by prefix range, then point lookup within |
> | LIST by prefix | Fan-out to ALL partitions (terrible) | Route to 1-2 partitions that cover the prefix (excellent) |
> | Hot prefix handling | Can't split a hot key range | Auto-split the hot prefix into finer partitions |
> | Ordering | No ordering | Keys sorted within partitions — natural for LIST |
> | Rebalancing | Consistent hashing moves K/N keys | Split a partition in half, move half the keys |
>
> **The auto-split mechanism:**
> - S3 monitors request rate per partition
> - When a partition exceeds a threshold (historically 3,500 PUT/s or 5,500 GET/s per prefix), it splits
> - Splits are based on key distribution within the partition — find the median key, split there
> - Today, S3 auto-scales automatically — the old per-prefix limits are gone, but the mechanism is the same"

**Interviewer:**
How does the front-end know which partition to route to?

**Candidate:**

> "There's a **partition map** — a routing table that maps key prefixes to partitions. It's maintained by a coordination service:
>
> ```
> Partition Map (simplified):
>   [my-bucket/a*, my-bucket/f*]  → Partition P0 → Nodes {A, B, C}
>   [my-bucket/g*, my-bucket/m*]  → Partition P1 → Nodes {D, E, F}
>   [my-bucket/n*, my-bucket/z*]  → Partition P2 → Nodes {G, H, I}
>
> Request: GET my-bucket/images/cat.jpg
>   → prefix 'my-bucket/i' falls in P1
>   → route to one of {D, E, F}
> ```
>
> The partition map is cached by front-end servers and updated asynchronously. When a split happens, the coordination service publishes the new map, and front-ends refresh. During the brief transition, a request might go to the old partition, which can redirect to the new one."

**Interviewer:**
What about the LIST operation? If I LIST `my-bucket/photos/2024/`, how does that work?

**Candidate:**

> "LIST is more expensive than GET because it's a range scan:
>
> 1. Front-end looks up partition map for prefix `my-bucket/photos/2024/`
> 2. Identifies which partition(s) cover this prefix range (usually 1-2 partitions)
> 3. Sends the range scan request to those partitions
> 4. Each partition returns keys matching the prefix, sorted lexicographically
> 5. Front-end merges results (if spanning partitions) and paginates (max 1000 keys per response, with a continuation token)
>
> Because partitions are range-based and keys are sorted within each partition, LIST is efficient — it doesn't need to fan out to every partition like hash-based sharding would require.
>
> The continuation token encodes the last key returned, so the next page resumes from that point."

**Interviewer:**
Good. What about the metadata schema for versioning?

**Candidate:**

> "When versioning is enabled on a bucket, each PUT creates a new version instead of overwriting:
>
> ```
> Index entry with versioning:
>
>   Key: (bucket, object_key)
>   Versions:
>     v3 (latest): {version_id: 'abc', etag: '...', size: 1024, chunks: [...], created: T3}
>     v2:          {version_id: 'def', etag: '...', size: 2048, chunks: [...], created: T2}
>     v1:          {version_id: 'ghi', etag: '...', size: 512,  chunks: [...], created: T1}
>
>   GET without version_id → returns v3
>   GET with version_id=def → returns v2
>   DELETE without version_id → inserts a 'delete marker' as v4 (soft delete)
>   DELETE with version_id=def → permanently removes v2
> ```
>
> The version chain is stored as part of the metadata entry, ordered by creation time. Delete markers are special versions that cause GET to return 404 but preserve the history."

> *For the full deep dive on metadata partitioning, see [metadata-and-indexing.md](metadata-and-indexing.md).*

#### Architecture Update After Phase 5

> "So our metadata layer has evolved:
>
> | | Before (Phase 4) | After (Phase 5) |
> |---|---|---|
> | **Metadata** | Replicated relational DB | **Prefix-based auto-partitioned distributed KV store** |
> | **Data** | 3x replication across AZs | *(still 3x replication — let's fix this next)* |
> | **Consistency** | Eventual | *(still eventual — we'll address this)* |

---

### L5 vs L6 vs L7 — Phase 5 Rubric

| Aspect | L5 (SDE-2) | L6 (SDE-3) | L7 (Principal) |
|---|---|---|---|
| **Metadata approach** | "Use a distributed database" | Explains prefix-based auto-partitioning with split mechanics | Discusses partition map caching, split transitions, consistency of routing |
| **LIST operation** | "Scan the database with a prefix filter" | Explains why hash-based is bad for LIST, range-based enables efficient scans | Discusses continuation token encoding, cross-partition merge, ListObjectsV2 improvements |
| **Hot partition handling** | "Add more shards" | Explains auto-split on traffic threshold | Discusses split heuristics, historical 3,500/5,500 limits, progressive scaling |

---

## PHASE 6: Deep Dive — Data Storage & Durability (~10 min)

**Interviewer:**
Good, we've upgraded the metadata layer from a replicated DB to a prefix-based auto-partitioned KV store. Now let's look at the data layer. You're currently using 3x replication — can we do better?

**Candidate:**

> "Yes — let's revisit our data layer. Currently we store 3 full copies across AZs. The primary job is durability — 11 9's means if you store 10 million objects, you'd statistically lose one object every 10,000 years. Let me first check if 3x replication even gets us there."

#### Our Current Design: 3x Replication

> "This is what we have — 3 full copies of every object, each in a different Availability Zone.
>
> ```
> Object: my-bucket/report.pdf (1 MB)
>
> Copy 1 → AZ-a, Storage Node 7, Disk 3
> Copy 2 → AZ-b, Storage Node 14, Disk 1
> Copy 3 → AZ-c, Storage Node 22, Disk 5
> ```
>
> **Durability math for 3x replication:**
> - Annual disk failure rate (AFR): ~2%
> - Probability all 3 copies fail in the same year: (0.02)^3 = 8 × 10^-6
> - That's only 5 nines of durability — far short of 11 nines
> - Even with faster repair (replace failed copy within hours), we get maybe 8-9 nines
>
> **And the cost:** 3x storage overhead. At exabyte scale, that's 2 extra exabytes of storage we're paying for. Unacceptable."

**Interviewer:**
So how does S3 achieve 11 nines without 3x replication?

**Candidate:**

> "**Erasure coding.** Specifically, Reed-Solomon erasure coding. Instead of storing full copies, we split the object into data chunks and compute parity chunks.
>
> **How it works:**
>
> ```
> Object: 'report.pdf' (8 MB)
>
> Step 1: Split into k=8 data chunks (1 MB each)
>   D1 | D2 | D3 | D4 | D5 | D6 | D7 | D8
>
> Step 2: Compute m=3 parity chunks using Reed-Solomon math
>   P1 | P2 | P3
>
> Step 3: Distribute all 11 chunks across different storage nodes and AZs
>
>   AZ-a:  Node1[D1]  Node2[D4]  Node3[D7]  Node4[P2]
>   AZ-b:  Node5[D2]  Node6[D5]  Node7[D8]  Node8[P3]
>   AZ-c:  Node9[D3]  Node10[D6] Node11[P1]
>
> Total storage: 11 chunks × 1 MB = 11 MB for an 8 MB object
> Overhead: 11/8 = 1.375x (vs 3.0x for replication)
> ```
>
> **The magic:** You can reconstruct the original 8 MB from **any 8 of the 11 chunks**. Up to 3 chunks can be lost, and you lose nothing. This is far more space-efficient than replication for the same durability level."

**Interviewer:**
Walk me through the durability math.

**Candidate:**

> "Let me calculate. With an 8+3 erasure coding scheme across 11 independent storage nodes:
>
> - **Annual failure rate per chunk**: ~2% (disk failure)
> - **Data loss occurs when**: 4 or more chunks fail before repair (since we can tolerate 3 losses)
> - **Repair time**: When a chunk is detected as lost, we reconstruct it from the remaining chunks and place it on a new node. Let's say repair takes 6 hours.
>
> Using a Markov model:
> ```
> P(data loss) = P(4+ failures within repair window)
>              = C(11,4) × (repair_window_failure_rate)^4
>
> Where repair_window_failure_rate ≈ AFR × (repair_hours / 8760)
>                                   = 0.02 × (6/8760)
>                                   = 0.0000137
>
> P(4 failures) ≈ C(11,4) × (1.37 × 10^-5)^4
>               = 330 × (3.5 × 10^-19)
>               = 1.16 × 10^-16
> ```
>
> That's roughly **16 nines of durability** — well beyond S3's advertised 11 nines. The extra margin accounts for correlated failures (fire, flood), software bugs, and operational errors."

**Interviewer:**
Good math. What about read performance? Isn't erasure coding slower than reading a single replica?

**Candidate:**

> "Yes, there's a tradeoff:
>
> | Aspect | 3x Replication | Erasure Coding (8+3) |
> |---|---|---|
> | **Storage overhead** | 3.0x | 1.375x |
> | **Durability** | ~8-9 nines | ~16 nines (theoretical) |
> | **Read latency** | Read from nearest single copy — fast | Must fetch at least 8 of 11 chunks and reconstruct — slower |
> | **Write latency** | Write to 3 locations | Encode + write to 11 locations (but chunks are smaller) |
> | **Repair cost** | Copy full object (1x object size network) | Reconstruct 1 chunk from 8 chunks (higher compute, less network) |
> | **Read bandwidth** | 1x | Up to 1.375x (if fetching extra chunks for faster reconstruction) |
>
> **S3 mitigates the read latency:**
> - Fetch 8 chunks in parallel — wall-clock time is latency of the *slowest* of 8 parallel fetches
> - Use 'degraded reads': send requests to all 11 chunk holders, take the first 8 responses (the 3 slowest are ignored). This tail-latency hedging significantly reduces p99 latency
> - For small objects (< 1 MB), the overhead of erasure coding doesn't make sense. S3 likely uses simple replication for small objects and erasure coding for larger ones"

**Interviewer:**
What about data integrity? How do you make sure bits don't silently flip?

**Candidate:**

> "Checksums at every layer — defense in depth:
>
> 1. **Upload checksum** — Client computes a checksum (MD5, SHA-256, or CRC32C) and sends it with the PUT. S3 verifies it on receipt. If mismatch → reject the upload.
>
> 2. **Storage checksum** — Each chunk stored on disk has its own checksum. On every read, the storage node verifies the chunk checksum before returning data.
>
> 3. **End-to-end checksum** — After reconstructing the object from chunks, S3 verifies the full-object checksum (the ETag) before returning to the client.
>
> 4. **Background scrubbing** — A continuous background process reads every chunk on every disk, verifies checksums, and repairs any corruption by reconstructing the chunk from other data/parity chunks. This catches 'bit rot' — silent data corruption on disk.
>
> The scrubber is S3's immune system. Even if a disk silently corrupts data, the scrubber will catch it and heal it before it causes data loss."

> *For the full deep dive on erasure coding and durability, see [data-storage-and-durability.md](data-storage-and-durability.md).*

#### Architecture Update After Phase 6

> "Our data layer has evolved:
>
> | | Before (Phase 4) | After (Phase 6) |
> |---|---|---|
> | **Metadata** | ~~Replicated DB~~ | Prefix-based auto-partitioned KV store (Phase 5) |
> | **Data** | ~~3x replication~~ | **Erasure coding (Reed-Solomon 8+3) — 1.375x overhead, 11+ 9's durability** |
> | **Consistency** | Eventual | *(still eventual — this is the last piece to fix)* |

---

### L5 vs L6 vs L7 — Phase 6 Rubric

| Aspect | L5 (SDE-2) | L6 (SDE-3) | L7 (Principal) |
|---|---|---|---|
| **Durability approach** | "Replicate to multiple nodes" | Explains erasure coding with concrete encoding scheme (8+3) and durability math | Discusses correlated failures, repair time impact on durability, tail-latency hedging |
| **Storage overhead** | Doesn't quantify | Calculates 1.375x vs 3.0x | Discusses how small objects use replication vs large use EC — different strategy per size |
| **Integrity** | "Use checksums" | Describes layered checksums (upload, storage, end-to-end) | Explains background scrubbing, bit rot detection, repair cycle, scrub rate tuning |

---

## PHASE 7: Deep Dive — Consistency Model (~8 min)

**Interviewer:**
We've upgraded metadata and data storage. But there's one problem left from your Phase 4 design — consistency. Your current architecture is eventually consistent because front-end servers cache metadata. Can we do better?

**Candidate:**

> "Yes — and this is actually one of the most interesting engineering challenges in S3's history. Let me explain why our current design has a consistency problem:
>
> **Before (pre-Dec 2020):**
> ```
> Client A: PUT my-bucket/config.json (version 2)  → 200 OK
> Client B: GET my-bucket/config.json               → might return version 1 (stale!)
> ```
>
> **After (post-Dec 2020):**
> ```
> Client A: PUT my-bucket/config.json (version 2)  → 200 OK
> Client B: GET my-bucket/config.json               → guaranteed version 2 or newer
> ```
>
> **Why was S3 eventually consistent in the first place?**
>
> The metadata layer uses caching extensively. When you PUT an object, the metadata is written to the primary metadata store, but front-end servers cache metadata for fast reads. A GET might hit a stale cache on a different front-end and return the old version.
>
> ```
>   Client A → Front-End 1 → Metadata Primary: WRITE version 2 ✓
>   Client B → Front-End 2 → Metadata Cache: READ → returns version 1 (stale cache)
>   ```
>
> Invalidating caches across a globally distributed fleet is the classic distributed systems problem."

**Interviewer:**
So how did they solve it?

**Candidate:**

> "S3 introduced a **witness** (or cache-coherence check) in the read path. Here's the mechanism:
>
> ```
> WRITE PATH (PUT):
>   1. Front-end receives PUT request
>   2. Writes new metadata to the metadata store (primary)
>   3. Updates a 'witness' — a lightweight, strongly consistent register
>      that stores: (bucket+key) → latest_version_id
>   4. Returns 200 OK to client
>
> READ PATH (GET):
>   1. Front-end receives GET request
>   2. Checks local metadata cache for bucket+key
>   3. Contacts the witness: "What's the latest version of this key?"
>   4. If cache version matches witness version → return cached data (fast path)
>   5. If cache is stale → fetch latest metadata from primary store, update cache, return
>   ```
>
> **Why this works without killing performance:**
>
> - The witness stores only `(bucket+key → version_id)` — tiny data, fits in memory
> - The witness check is a **sub-millisecond in-memory lookup** — negligible added latency
> - On the fast path (cache hit, version matches), latency is barely affected
> - Only when the cache is stale do we pay the cost of a metadata primary read
> - The witness itself is replicated across AZs for availability, using a consensus protocol (likely Paxos or Raft)"

**Interviewer:**
Interesting. What are the challenges with this approach?

**Candidate:**

> "Several challenges S3 had to solve:
>
> 1. **Witness availability** — If the witness is down, all reads block. It must be as available as S3 itself. Solution: replicate the witness across AZs, use quorum reads (2-of-3 AZs).
>
> 2. **Witness scale** — 100 million reads/sec all checking the witness. Solution: the witness only stores version IDs (not full metadata), so each entry is < 100 bytes. Heavy caching with short TTLs, and partitioning the witness the same way metadata is partitioned.
>
> 3. **Write amplification** — Every PUT now has an extra write to the witness. But since it's a tiny write (just a version ID), the overhead is minimal.
>
> 4. **Migration** — S3 had to add this to a running system serving trillions of requests without downtime. They likely rolled it out region by region, bucket by bucket, using feature flags.
>
> 5. **Delete consistency** — When you DELETE an object, the witness must reflect that immediately. Otherwise, a GET after DELETE might still return the object from cache.
>
> The key insight from the 2021 paper by Bronson et al. is that **strong consistency in the metadata layer is sufficient** — the data chunks are immutable and addressed by content hash, so they don't have consistency issues. The consistency problem is entirely in the metadata layer."

> *For the full deep dive on consistency and replication, see [consistency-and-replication.md](consistency-and-replication.md).*

#### Architecture Evolution — Complete

> "After three deep dives, our architecture has evolved from the naive Phase 4 design to a production-grade system:
>
> | Component | Phase 4 (Naive) | Final (After Deep Dives) | Why the Change |
> |---|---|---|---|
> | **Metadata** | Replicated relational DB | Prefix-based auto-partitioned KV store | Single DB can't handle 100M lookups/sec or 100 PB; hash-based can't do LIST efficiently |
> | **Data storage** | 3x replication across AZs | Erasure coding (Reed-Solomon 8+3) | 3x is too expensive at exabyte scale; EC gives 11+ 9's at only 1.375x overhead |
> | **Consistency** | Eventual (stale cache reads) | Strong read-after-write via witness protocol | Customers need guaranteed consistency; witness adds sub-ms overhead |
>
> ```
> FINAL ARCHITECTURE (after all deep dives):
>
>                            ┌──────────────────────┐
>                            │       Clients         │
>                            └──────────┬───────────┘
>                                       │ HTTPS (REST API)
>                            ┌──────────▼───────────┐
>                            │    Front-End Layer    │
>                            │  Auth, Rate Limit,    │
>                            │  Request Routing      │
>                            └──────────┬───────────┘
>                                       │
>                    ┌──────────────────┼──────────────────┐
>                    │                  │                  │
>         ┌──────────▼──────────┐ ┌────▼────┐ ┌──────────▼──────────┐
>         │   Metadata Layer    │ │ Witness │ │    Data Layer        │
>         │                     │ │ (strong │ │                      │
>         │  Distributed KV     │ │ consist-│ │  Erasure Coding      │
>         │  auto-partitioned   │ │  ency)  │ │  (8+3 Reed-Solomon)  │
>         │  by prefix          │ │         │ │  across 3 AZs        │
>         │                     │ └─────────┘ │                      │
>         │  Handles:           │             │  1.375x overhead     │
>         │  - Point lookups    │             │  11+ 9's durability  │
>         │  - LIST (range scan)│             │  Checksums + scrub   │
>         │  - Auto-split hot   │             │                      │
>         │    partitions       │             │  AZ-a  AZ-b  AZ-c   │
>         └─────────────────────┘             └──────────────────────┘
> ```
>
> This is a dramatically different system from where we started — a single server with a local filesystem."

---

### L5 vs L6 vs L7 — Phase 7 Rubric

| Aspect | L5 (SDE-2) | L6 (SDE-3) | L7 (Principal) |
|---|---|---|---|
| **Consistency model** | "Use strong consistency" | Explains the witness protocol and why it's in the read path | Discusses migration strategy, witness availability, Paxos/Raft for witness replication |
| **History** | Doesn't know S3 was eventually consistent | Knows the 2020 change, explains why eventual was the original choice | References the Bronson et al. paper, explains metadata-only consistency insight |
| **Tradeoffs** | Doesn't discuss | Explains fast-path vs slow-path latency impact | Quantifies witness overhead, discusses write amplification and scale of witness itself |

---

## PHASE 8: Storage Classes & Lifecycle (~5 min)

**Interviewer:**
Let's talk about cost optimization. S3 has multiple storage classes — how would you design that?

**Candidate:**

> "Not all data is accessed equally. A company might upload a log file, analyze it for a week, then never touch it again for years but must retain it for compliance. Storing that on high-performance SSDs forever is wasteful. This is why S3 has **tiered storage classes**:
>
> | Storage Class | Use Case | Availability | Min Storage Duration | First-Byte Latency | Relative Cost (Storage) |
> |---|---|---|---|---|---|
> | **S3 Standard** | Frequently accessed data | 99.99% | None | Milliseconds | 1.0x (baseline) |
> | **S3 Standard-IA** | Infrequent access (>30 days) | 99.9% | 30 days | Milliseconds | ~0.45x |
> | **S3 One Zone-IA** | Infrequent, non-critical | 99.5% | 30 days | Milliseconds | ~0.36x |
> | **S3 Glacier Instant** | Archive, instant access | 99.9% | 90 days | Milliseconds | ~0.17x |
> | **S3 Glacier Flexible** | Archive, minutes-hours | 99.99% | 90 days | Minutes to hours | ~0.14x |
> | **S3 Glacier Deep Archive** | Long-term archive | 99.99% | 180 days | 12-48 hours | ~0.07x |
> | **S3 Intelligent-Tiering** | Unknown access patterns | 99.9% | None | Milliseconds | Auto-optimized |
>
> **How this works internally:**
>
> Each storage class corresponds to a different **storage backend** with different hardware and access patterns:
>
> - **Standard / Standard-IA**: Data on SSDs or fast HDDs, erasure-coded across 3 AZs, always online
> - **One Zone-IA**: Same as Standard-IA but data only in 1 AZ (cheaper, less durable for AZ-level failures)
> - **Glacier Flexible**: Data moved to a cold storage backend — possibly tape libraries or very dense, powered-down HDD arrays. 'Retrieval' means staging data from cold storage to hot storage, which takes time
> - **Glacier Deep Archive**: Tape or ultra-cold storage. 12-48 hours for retrieval
> - **Intelligent-Tiering**: S3 monitors access patterns per object and automatically moves objects between tiers (Frequent Access → Infrequent Access → Archive Instant Access → Archive → Deep Archive)"

**Interviewer:**
How do lifecycle policies work?

**Candidate:**

> "Lifecycle policies are rules defined on a bucket that automate storage class transitions and expirations:
>
> ```
> Example Lifecycle Policy:
>   Rule 1: Transition objects with prefix 'logs/' to Standard-IA after 30 days
>   Rule 2: Transition objects with prefix 'logs/' to Glacier after 90 days
>   Rule 3: Delete objects with prefix 'logs/' after 365 days
>   Rule 4: Abort incomplete multipart uploads after 7 days
>
> Timeline for 'logs/app-2024-01-15.log':
>   Day 0:   Uploaded → S3 Standard
>   Day 30:  Lifecycle service transitions → S3 Standard-IA
>   Day 90:  Lifecycle service transitions → Glacier Flexible
>   Day 365: Lifecycle service deletes object
> ```
>
> **Implementation:**
> - A background **Lifecycle Evaluator** service periodically scans object metadata
> - For each object, it checks creation time against lifecycle rules
> - Transitions involve: re-encoding the data for the target storage backend + updating metadata + deleting old chunks
> - The evaluator is partitioned the same way as metadata — each partition's evaluator handles its own objects
> - Transitions are batched for efficiency (don't move one object at a time)"

> *For the full deep dive on storage classes and lifecycle, see [storage-classes-and-lifecycle.md](storage-classes-and-lifecycle.md).*

---

## PHASE 9: Multipart Upload, Security & Advanced Features (~5 min)

**Candidate:**

> "Let me cover three critical features briefly: multipart upload, security, and event notifications."

#### Multipart Upload

> "For objects larger than ~100 MB, single-request upload is unreliable — network interruptions mean starting over. Multipart upload solves this:
>
> ```
> Multipart Upload Flow:
>
> 1. Client → S3: CreateMultipartUpload(bucket, key)
>    S3 → Client: upload_id = 'abc123'
>
> 2. Client uploads parts in parallel:
>    UploadPart(upload_id, part_number=1, body=<100MB chunk>) → ETag1
>    UploadPart(upload_id, part_number=2, body=<100MB chunk>) → ETag2
>    UploadPart(upload_id, part_number=3, body=<50MB chunk>)  → ETag3
>
> 3. Client → S3: CompleteMultipartUpload(upload_id, [
>      {part: 1, etag: ETag1},
>      {part: 2, etag: ETag2},
>      {part: 3, etag: ETag3}
>    ])
>    S3 validates all parts, composes the final object, returns 200 OK
>
> On failure:
>    Client → S3: AbortMultipartUpload(upload_id)
>    S3 cleans up all uploaded parts (or lifecycle does it after N days)
> ```
>
> **Key design details:**
> - Parts can be uploaded in parallel, out of order, and from different machines
> - Each part is independently erasure-coded and stored
> - Minimum part size: 5 MB (except the last part). Maximum: 5 GB. Maximum parts: 10,000.
> - CompleteMultipartUpload is atomic — either all parts are composed or none are
> - Incomplete multipart uploads consume storage — lifecycle policies should clean them up"

#### Security Model

> "S3's security is defense in depth:
>
> 1. **Authentication**: AWS SigV4 — every request is signed with the caller's secret key. The signature includes the HTTP method, path, headers, and a timestamp to prevent replay attacks.
>
> 2. **Authorization**: Three policy layers evaluated together:
>    - **IAM policies** (identity-based): what the *user/role* can do
>    - **Bucket policies** (resource-based): what the *bucket* allows
>    - **ACLs** (legacy): per-object access grants
>    - Evaluation: Explicit Deny > Explicit Allow > Implicit Deny
>
> 3. **Encryption at rest**:
>    - **SSE-S3**: S3 manages the encryption key (AES-256). Simplest.
>    - **SSE-KMS**: AWS KMS manages the key. Provides audit trail (who accessed what key when), key rotation, and per-key IAM policies.
>    - **SSE-C**: Customer provides the key with each request. S3 encrypts/decrypts but never stores the key.
>
> 4. **Encryption in transit**: All S3 traffic over HTTPS/TLS. You can enforce HTTPS-only via bucket policy.
>
> 5. **Block Public Access**: Account-level and bucket-level setting that overrides any policy granting public access. Prevents accidental data exposure."

#### Event Notifications

> "S3 can publish events (object created, deleted, etc.) to:
> - **SNS** — fan out to multiple subscribers
> - **SQS** — queue for processing
> - **Lambda** — serverless processing triggered by upload
> - **EventBridge** — for complex event routing and filtering
>
> This enables reactive architectures: upload an image → Lambda triggers → resize → store thumbnails."

> *For the full deep dives, see [security-and-access-control.md](security-and-access-control.md) and [api-contracts.md](api-contracts.md).*

---

## PHASE 10: Wrap-Up & Summary (~3 min)

**Interviewer:**
We're running low on time. Summarize your design and tell me what keeps you up at night operating this system.

**Candidate:**

> "**Design evolution — how we got here:**
>
> | Component | Started With (Phase 4) | Evolved To | Why |
> |---|---|---|---|
> | **Architecture** | Single server with local FS | 3-layer microservices: front-end, metadata, data | Separate concerns that scale differently |
> | **Metadata** | Replicated relational DB | Distributed KV store, prefix-based auto-partitioning | Single DB can't handle 100M req/sec; hash-based can't do LIST efficiently |
> | **Data storage** | 3x replication across AZs | Erasure coding (Reed-Solomon 8+3) across 3 AZs | 11 9's durability at 1.375x overhead (vs 3x cost) |
> | **Consistency** | Eventual (stale caches) | Strong read-after-write via witness protocol | No stale reads, sub-millisecond overhead on fast path |
>
> **Additional features built on top:**
>
> | Component | Design Choice | Why |
> |---|---|---|
> | **Namespace** | Flat: bucket + key (no real directories) | Simpler, faster lookups, prefixes simulate hierarchy |
> | **Large objects** | Multipart upload (parallel parts, atomic completion) | Reliable upload for objects up to 5 TB |
> | **Storage tiers** | 7 storage classes with lifecycle automation | Cost optimization: hot data on SSD, cold on tape |
> | **Security** | SigV4 auth, IAM + bucket policies, SSE-S3/KMS/C | Defense in depth, encryption at rest and in transit |
> | **Integrity** | Checksums at every layer + background scrubbing | Detect and repair corruption before data loss |
> | **Multi-tenancy** | Shuffle sharding, rate limiting, resource isolation | One customer's blast radius doesn't affect others |
>
> **What keeps me up at night:**
>
> 1. **Blast radius of failures** — At S3's scale, any bug can affect millions of customers. S3 uses **shuffle sharding** to limit blast radius: each customer's data is spread across a random subset of resources, so a failure in one subset only affects a fraction of customers. But correlated failures (an AZ going down, a kernel bug) can still have wide impact. The key is *cell-based architecture* — independent, isolated failure domains.
>
> 2. **Silent data corruption** — Hardware can silently corrupt data on disk without reporting an error. This is the most insidious failure mode because you don't know it's happening. The background scrubber is our defense, but it must be tuned carefully — too aggressive and it impacts customer traffic, too slow and corruption spreads. Monitoring scrub coverage (% of data verified per week) is critical.
>
> 3. **Metadata hotspots** — A single viral bucket or prefix can receive millions of requests/sec. Auto-partitioning handles this, but there's a lag between detecting the hotspot and completing the split. During that lag, the hot partition throttles. Pre-warming (proactively splitting when we know traffic is coming) and adaptive throttling (503 SlowDown responses) are mitigations.
>
> 4. **Cost of durability at scale** — Exabytes of data means exabytes of erasure coding overhead, petabytes of metadata, and constant background scrubbing and repair. The cost structure of S3 is directly tied to how efficiently we encode, store, and verify data. Any inefficiency is multiplied by 10^14 objects.
>
> 5. **Consistency subsystem failure** — If the witness protocol has a bug or the witness becomes unavailable, we either return stale data (violating our consistency guarantee) or block reads (violating availability). The witness must be as reliable as S3 itself, which means it needs its own replication, monitoring, and failover.
>
> **Potential extensions:**
> - **S3 Select / Glacier Select** — Push SQL-like queries to the storage layer (predicate pushdown), reducing data transfer
> - **S3 Object Lambda** — Transform objects on the fly during GET (resize images, redact PII)
> - **Cross-Region Replication** — Async replication for disaster recovery and compliance
> - **S3 Batch Operations** — Apply operations (copy, tag, invoke Lambda) to billions of objects
> - **S3 Access Points** — Named network endpoints with distinct permissions, simplifying multi-tenant access"

---

## Interviewer's Final Assessment

**Hire Recommendation: Strong Hire (L5 — solid SDE-2 with growth toward L6)**

| Dimension | Rating | Notes |
|---|---|---|
| **Problem Decomposition** | Exceeds Bar | Clean 3-layer architecture. Immediately separated metadata from data with clear reasoning. |
| **Requirements & Scoping** | Meets Bar | Good functional/non-functional separation. Knew about S3's flat namespace and consistency history. |
| **Scale Estimation** | Meets Bar | Solid numbers: 100T objects, 25.6 EB raw, 100 PB metadata. Used estimates to drive design decisions. |
| **Durability Design** | Exceeds Bar | Erasure coding explanation was strong — scheme, math, and tradeoffs vs replication. |
| **Consistency Deep Dive** | Exceeds Bar | Witness protocol explained clearly. Knew the 2020 change and why it mattered. |
| **Metadata Design** | Exceeds Bar | Prefix-based auto-partitioning was the right call. Explained LIST efficiency and hot partition handling. |
| **Storage Classes** | Meets Bar | Covered all tiers with correct characteristics. Lifecycle policies explained. |
| **Security** | Meets Bar | SigV4, IAM + bucket policies + ACLs, SSE-S3/KMS/C. Block Public Access mentioned. |
| **Operational Maturity** | Exceeds Bar | "What keeps you up at night" was strong — shuffle sharding, silent corruption, metadata hotspots. |
| **Communication** | Exceeds Bar | Structured, used diagrams and tables, checked in at transitions. Naive → refined progression was natural. |
| **LP: Dive Deep** | Exceeds Bar | Went deep on erasure coding durability math and consistency witness protocol unprompted. |
| **LP: Invent and Simplify** | Meets Bar | Good use of existing patterns (erasure coding, prefix partitioning) rather than inventing complexity. |
| **LP: Think Big** | Meets Bar | Extensions section showed awareness of broader S3 ecosystem. |

**What would push this to L6:**
- Deeper discussion of shuffle sharding mechanics (how customer-to-cell mapping works)
- Proposing monitoring/observability architecture (what dashboards, what alarms, runbooks)
- Discussing the operational process of rolling out the consistency change to a live system
- More nuance on failure modes: what happens during an AZ outage for in-flight multipart uploads
- Cost modeling: $/GB/month calculation for different storage classes, and how erasure coding scheme choice affects cost

---

## Key Differences: L5 vs L6 vs L7 Expectations for This Problem

| Aspect | L5 (SDE-2) | L6 (SDE-3) | L7 (Principal) |
|---|---|---|---|
| **Requirements** | Lists CRUD operations, mentions durability | Quantifies 11 9's, explains consistency history, raises multipart/versioning | Frames requirements around customer use cases and SLA commitments, discusses cost constraints |
| **Architecture** | Correct 3-layer design | Justifies separation of metadata/data with tradeoffs | Discusses cell-based architecture, failure domains, deployment topology |
| **Metadata** | "Shard the database" | Prefix-based auto-partitioning, LIST efficiency, hot partition splits | Discusses partition map consistency, routing during splits, secondary indexes |
| **Durability** | "Replicate across AZs" | Erasure coding with durability math, overhead comparison | Analyzes correlated failures, repair bandwidth, durability vs cost optimization curve |
| **Consistency** | "Use strong consistency" | Explains witness protocol, knows the 2020 change | Discusses migration strategy, witness scale, Paxos/Raft details, consistency-availability tradeoffs |
| **Storage classes** | Knows Standard and Glacier exist | Explains all tiers, lifecycle policies, Intelligent-Tiering mechanics | Discusses storage backend architecture, tape vs HDD for archive, cost-per-PB modeling |
| **Security** | "Use IAM and encryption" | SigV4, three policy layers, SSE-S3/KMS/C, Block Public Access | Discusses cross-account access patterns, VPC endpoints, audit trails, compliance (SOC2/HIPAA) |
| **Operational thinking** | Mentions monitoring | Identifies specific failure modes (hot partitions, silent corruption) | Proposes blast radius isolation strategy, game days, automated remediation runbooks |
| **Multipart upload** | Knows it exists | Explains the 3-step flow, parallel parts, abort cleanup | Discusses part placement strategy, incomplete upload cost, upload resumption semantics |
| **Communication** | Responds to questions | Drives the conversation, uses diagrams | Negotiates scope, proposes phased deep dives, manages interview time |

---

*For detailed deep dives on each component, see the companion documents:*
- [API Contracts](api-contracts.md) — S3 REST API design with request/response examples
- [Metadata & Indexing](metadata-and-indexing.md) — Distributed index, prefix partitioning, LIST operations
- [Data Storage & Durability](data-storage-and-durability.md) — Erasure coding, durability math, integrity checks
- [Consistency & Replication](consistency-and-replication.md) — Witness protocol, CRR/SRR, consistency guarantees
- [Storage Classes & Lifecycle](storage-classes-and-lifecycle.md) — Tiered storage, lifecycle policies, Intelligent-Tiering
- [System Flows](flow.md) — End-to-end flows for PUT, GET, DELETE, multipart, replication
- [Scaling & Performance](scaling-and-performance.md) — Shuffle sharding, transfer acceleration, auto-scaling
- [Security & Access Control](security-and-access-control.md) — IAM, encryption, bucket policies, compliance

*End of interview simulation.*
