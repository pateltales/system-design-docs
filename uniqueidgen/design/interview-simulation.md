# System Design Interview Simulation: Design a Distributed Unique ID Generator

> **Interviewer:** Principal Engineer (L8), Amazon
> **Candidate Level:** SDE-3 (L6 — Senior Software Development Engineer)
> **Duration:** ~60 minutes
> **Date:** February 7, 2026

---

## PHASE 1: Opening & Problem Statement (~2 min)

**Interviewer:**
Hey, welcome. I'm [name], Principal Engineer on the infrastructure platform team. For today's system design round, I'd like you to design a **distributed unique ID generator** — a system that generates unique identifiers at massive scale across many machines, with no single point of coordination. Think about systems like Twitter, Instagram, or Discord that need to create millions of IDs per second for tweets, posts, messages — each one globally unique.

I care about how you reason through the problem, what naive approaches you consider first, and how you evolve toward a production-grade solution. I'll push back on your decisions — that's not a negative signal, that's me calibrating depth.

Take it away.

---

## PHASE 2: Requirements Gathering & Scoping (~5 min)

**Candidate:**
Thanks! Before I jump into solutions, I want to make sure we're aligned on what "unique ID generator" means — the requirements can vary dramatically depending on the use case. Let me ask some clarifying questions.

**Functional Requirements — what properties must the IDs have?**

> "The most basic requirement is obvious: every generated ID must be globally unique — no two services, machines, or processes should ever produce the same ID. But I want to clarify several properties beyond uniqueness:
>
> - **Must IDs be sortable by time?** If I generate ID_A at time T1 and ID_B at time T2 where T1 < T2, should ID_A < ID_B? This is critical for databases — time-ordered IDs make range queries and pagination efficient."

**Interviewer**: "Yes. IDs should be **roughly time-ordered** — I don't need nanosecond precision, but IDs generated a second apart should sort correctly. This is sometimes called **k-sortable**."

> "- **What format should IDs be?** 64-bit integers? 128-bit UUIDs? Strings?"

**Interviewer**: "Let's go with **64-bit integers**. They fit in a single database column, are efficient for indexing, and are compatible with most programming languages natively."

> "- **Must ID generation work without coordination?** Meaning each server can generate IDs independently — no network call to a central authority?"

**Interviewer**: "Yes. That's the whole point — we need a **coordination-free** system. You should not need to call another service or database to generate an ID. The generation should happen **locally** on the machine."

> "- **Do we need to extract metadata from the ID?** For example, if I have an ID, can I determine *when* it was generated or *which machine* generated it?"

**Interviewer**: "That would be a nice-to-have. If the ID encodes a timestamp, being able to extract it is useful for debugging and analytics."

> "- **How many IDs per second do we need to support?** And across how many machines?"

**Interviewer**: "Let's say **100,000+ IDs per second across the system**, with the ability to burst higher. We'll have hundreds of machines generating IDs concurrently."

**Non-Functional Requirements:**

> "Let me lay out the key non-functional properties:
>
> | Dimension | My Proposal |
> |---|---|
> | **Uniqueness** | Globally unique across all workers, all time. Zero collisions — even a single duplicate ID could corrupt data. |
> | **Ordering** | Time-sortable (k-sortable) — IDs generated later should be numerically larger. |
> | **Performance** | Sub-microsecond latency per ID generation. No network call, no disk I/O — purely in-memory. |
> | **Availability** | Each worker generates IDs independently. No SPOF. If a central coordinator goes down, ID generation continues. |
> | **Compactness** | 64-bit integer — fits in a `BIGINT` database column, efficient B-tree indexing. |
> | **No Coordination** | Workers don't talk to each other or to a central service during ID generation. |

**Interviewer:**
Good scoping. I like that you immediately called out k-sortability and coordination-free generation — those are the key constraints that eliminate many naive approaches. Let's get into the numbers.

---

## PHASE 3: Scale Estimation (~3 min)

**Candidate:**

> "Let me estimate scale based on real-world systems."

#### Traffic Estimates

> "Some reference points:
> - **Twitter**: ~500 million tweets/day = ~6,000 tweets/sec average, ~15,000/sec peak
> - **Instagram**: ~100 million photos/day = ~1,200/sec average
> - **Discord**: ~40 million messages/day per large server guild, billions total
> - **Uber**: ~20 million trips/day, but each trip generates 100+ events (GPS pings, status updates) = billions of IDs/day
>
> For our design, let's target:
> - **100,000 IDs/sec sustained** across the system
> - **1 million IDs/sec peak** (10x burst factor)
> - **1,000 worker machines** generating IDs concurrently
> - **Per-worker**: 100 IDs/sec average, 1,000 IDs/sec peak"

#### ID Space Calculations (64-bit)

> "A 64-bit integer gives us 2^63 = **9.2 × 10^18** possible IDs (using signed integers, reserving the sign bit).
>
> At 100,000 IDs/sec:
> - Per day: 100,000 × 86,400 = **8.64 billion IDs/day**
> - Per year: **3.15 trillion IDs/year**
> - Time to exhaust 64-bit space: 9.2 × 10^18 / 3.15 × 10^12 = **~2.9 million years**
>
> So 64 bits is more than sufficient. The real question is how we *structure* those 64 bits."

#### Storage Impact

> "If each ID is stored in a database as a primary key:
> - 64-bit ID = 8 bytes
> - 10 billion records × 8 bytes = **80 GB** just for IDs
> - Compare to 128-bit UUIDs: 10 billion × 16 bytes = **160 GB** — 2x storage for IDs
> - B-tree index overhead: ~2-3x the raw ID size
>
> 64-bit IDs save significant storage and improve index locality (sequential IDs = fewer B-tree page splits)."

**Interviewer:**
Good. Those numbers will be important when we compare approaches. Now, let's start with the simplest possible solution and build up.

---

## PHASE 4: Naive Approaches — Building from Simplest to Complex (~10 min)

**Interviewer:**
Don't jump straight to the sophisticated solution. Start with the simplest thing that could work, and tell me why it fails. I want to see your reasoning process.

**Candidate:**

> "Absolutely. Let me walk through progressively more sophisticated approaches, explaining why each fails and how the next one improves on it."

### Approach 1: Single-Server Auto-Increment Counter

> "The simplest possible solution: one server with an atomic counter.
>
> ```
>     Client A ──▶ ┌──────────────────┐ ──▶ ID: 1
>     Client B ──▶ │  Counter Server  │ ──▶ ID: 2
>     Client C ──▶ │  counter = 0     │ ──▶ ID: 3
>                  │  counter++       │
>                  └──────────────────┘
> ```
>
> **Pros**: Dead simple. Guaranteed unique. Perfectly sequential.
>
> **Why it fails:**
> 1. **Single Point of Failure** — If this server goes down, no one can generate IDs. The entire system halts.
> 2. **Performance bottleneck** — Every ID requires a network round-trip to this server. At 100K IDs/sec, with 1ms network RTT, we'd need the counter to handle 100K increments/sec. A single mutex-protected counter tops out around 10-50K ops/sec on commodity hardware.
> 3. **No horizontal scaling** — Can't add more counter servers without coordination (which counter value is 'next'?).
> 4. **Geographic latency** — If services are in multiple regions, cross-region calls to a single counter add 50-200ms of latency per ID."

### Approach 2: Database AUTO_INCREMENT

> "Move the counter into a database with `AUTO_INCREMENT` / `SERIAL`:
>
> ```sql
> CREATE TABLE ids (
>     id BIGINT AUTO_INCREMENT PRIMARY KEY,
>     created_at TIMESTAMP DEFAULT NOW()
> );
>
> -- Generate ID:
> INSERT INTO ids (created_at) VALUES (NOW());
> -- Returns: id = 1, 2, 3, ...
> ```
>
> **Pros**: Simple, durable (persisted to disk), transactional, well-understood.
>
> **Why it fails:**
> 1. **Same bottleneck** — The database is now the single point of contention. Every ID generation is a write to the database.
> 2. **Throughput limit** — A single MySQL instance handles ~5,000-10,000 inserts/sec. We need 100K+.
> 3. **Replication issues** — If you use a primary-replica setup, the primary is still the bottleneck. If you use multi-primary, AUTO_INCREMENT collides between primaries.
> 4. **Network latency** — Still requires a network call to the DB for every ID.
>
> Multi-primary AUTO_INCREMENT hack:
> ```
> Primary 1: IDs = 1, 3, 5, 7, ... (increment by 2, start at 1)
> Primary 2: IDs = 2, 4, 6, 8, ... (increment by 2, start at 2)
> ```
> This works but breaks if you add a third primary — you'd need to change the increment factor, which requires downtime and data migration."

**Interviewer:**
Good. You identified the single point of failure and bottleneck immediately. What about UUIDs? They're designed for distributed uniqueness.

### Approach 3: UUID v4 (Random)

**Candidate:**

> "UUID v4 generates 128-bit random identifiers:
>
> ```
> Example: 550e8400-e29b-41d4-a716-446655440000
>
> Format: 32 hex digits in 8-4-4-4-12 pattern
> 122 bits of randomness (6 bits for version/variant)
> ```
>
> **Pros**:
> - Truly coordination-free — any machine can generate one with just a random number generator
> - Collision probability is astronomically low: with 122 random bits, you'd need ~2.7 × 10^18 UUIDs before hitting a 50% collision chance (birthday paradox)
>
> **Why it fails for our use case:**
> 1. **Not sortable** — UUIDs are random, so there's no temporal ordering. `UUID_A` generated at T1 and `UUID_B` generated at T2 have no ordering relationship. This kills range queries and pagination.
> 2. **128 bits, not 64** — We need 64-bit IDs. UUIDs are twice the size.
> 3. **Terrible for database indexes** — Random UUIDs cause massive B-tree fragmentation. Each new insert goes to a random page, causing constant page splits. This can **degrade write throughput by 2-5x** compared to sequential IDs.
> 4. **No metadata** — Can't extract timestamp or source information from a random UUID.
>
> ```
> B-tree index impact of random vs sequential IDs:
>
> Sequential IDs:          Random UUIDs:
> [1,2,3,4,5] → [6,7,8]   [a3f,2b1,9e4,1c7] → pages everywhere!
> ┌─────────┐              ┌─────────┐
> │ Page 1  │ ← always     │ Page 1  │ ← random
> │ append  │   appending  │ Page 23 │   scattered
> │ to end  │              │ Page 7  │   writes
> └─────────┘              │ Page 42 │
>                          └─────────┘
>
> Sequential: ~1 page split per full page
> Random: ~1 page split every few inserts → 10-100x more I/O
> ```"

### Approach 4: UUID v1 (Time-based)

> "UUID v1 embeds a timestamp and MAC address:
>
> ```
> UUID v1 structure (128 bits):
> ┌──────────────┬──────────┬──────────┬──────────────────┐
> │  time_low    │ time_mid │ time_hi  │ clock_seq + node │
> │  (32 bits)   │ (16 bits)│ (16 bits)│    (64 bits)     │
> └──────────────┴──────────┴──────────┴──────────────────┘
>
> Node = 48-bit MAC address of the generating machine
> Time = 60-bit timestamp (100-nanosecond intervals since Oct 15, 1582)
> ```
>
> **Pros**: Contains a timestamp, unique per machine (MAC address), no coordination needed.
>
> **Why it fails:**
> 1. **NOT k-sortable** — The timestamp bits are split across non-contiguous positions in the UUID. Sorting UUID v1s lexicographically does NOT sort them by time!
> 2. **128 bits** — Still doesn't fit our 64-bit requirement.
> 3. **Privacy concern** — The MAC address leaks which physical machine generated the ID. This is a security/privacy issue.
> 4. **MAC address collisions** — In cloud environments (AWS, Docker containers), MAC addresses can be duplicated across VMs."

**Interviewer:**
Good, you've identified why general-purpose UUID schemes don't work for our use case. Now, what about a centralized approach designed specifically for ID generation?

### Approach 5: Database Ticket Server (Flickr's Approach)

**Candidate:**

> "Flickr pioneered this in 2010. Use a dedicated MySQL instance purely for ID generation:
>
> ```sql
> -- Flickr's ticket server schema
> CREATE TABLE Tickets64 (
>     id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
>     stub CHAR(1) NOT NULL DEFAULT '',
>     PRIMARY KEY (id),
>     UNIQUE KEY stub (stub)
> ) ENGINE=InnoDB;
>
> -- Generate an ID:
> REPLACE INTO Tickets64 (stub) VALUES ('a');
> SELECT LAST_INSERT_ID();
> ```
>
> `REPLACE INTO` atomically deletes and re-inserts the row, incrementing the auto-increment counter. The table always has exactly one row.
>
> **Scaling**: Two ticket servers, each with a different starting offset:
>
> ```
> Ticket Server 1: auto_increment_increment=2, auto_increment_offset=1
>   → Generates: 1, 3, 5, 7, 9, ...
>
> Ticket Server 2: auto_increment_increment=2, auto_increment_offset=2
>   → Generates: 2, 4, 6, 8, 10, ...
>
> Load Balancer
>     ├── Server 1 → odd IDs
>     └── Server 2 → even IDs
> ```
>
> **Pros**: 64-bit IDs, simple, battle-tested at Flickr scale, roughly sequential.
>
> **Why it falls short:**
> 1. **Still requires a network call** — Every ID generation is a write to MySQL. At 100K IDs/sec, that's a lot of DB writes.
> 2. **Single point of failure** (sort of) — If Ticket Server 1 goes down, you lose 50% capacity. Not fully coordination-free.
> 3. **Not truly time-ordered** — IDs from Server 1 and Server 2 interleave: 1, 2, 3, 4 is ordered, but actual generation order might be 2, 1, 4, 3 (Server 2 was faster for those requests).
> 4. **Hard to scale beyond 2 servers** — Adding a third server requires changing the increment factor on all servers (disruptive).
> 5. **Latency** — Network RTT to DB + disk I/O for each ID. ~1-5ms per ID generation."

### Approach 6: Range Allocation

> "Each worker pre-allocates a range of IDs from a central authority:
>
> ```
> Central Authority (ZooKeeper/etcd):
>   next_range_start = 1
>
> Worker A requests range → gets [1, 1000]
>   next_range_start = 1001
>
> Worker B requests range → gets [1001, 2000]
>   next_range_start = 2001
>
> Worker A generates: 1, 2, 3, ..., 1000  (locally, no network call!)
> Worker B generates: 1001, 1002, ..., 2000 (locally!)
>
> When Worker A exhausts range → requests [2001, 3000] from central authority
> ```
>
> **Pros**:
> - Mostly coordination-free — only need a network call every 1,000 IDs
> - 64-bit, sequential within each worker's range
>
> **Why it falls short:**
> 1. **Gaps on server restart** — If Worker A generated IDs 1-500 then crashes, IDs 501-1000 are lost (never used). Over time, gaps accumulate.
> 2. **Not time-ordered across workers** — Worker A might generate ID 50 while Worker B generates ID 1500. ID 50 < 1500 but they were generated at the same time. No temporal ordering.
> 3. **Central authority still needed** — ZooKeeper/etcd is a dependency for range allocation. If it's down, workers that exhaust their range can't get new ones.
> 4. **Range size tradeoff** — Small ranges = frequent coordination. Large ranges = more wasted IDs on restart."

**Interviewer:**
Excellent walkthrough. You've now shown me six approaches and why each fails. I want you to notice a pattern — what's the fundamental problem?

**Candidate:**

> "The fundamental tension is:
> - **Uniqueness without coordination** requires some form of partitioning — each worker needs its own 'namespace'
> - **Time ordering** requires a shared notion of time — but clocks are unreliable in distributed systems
> - **Compactness (64 bits)** limits how much information we can encode
>
> The solution needs to cleverly **encode both time AND worker identity** into 64 bits, in a way that:
> 1. Time bits are the most significant → natural sort order by time
> 2. Worker ID bits prevent collisions between machines
> 3. A sequence counter handles multiple IDs within the same time unit
>
> This is exactly what Twitter's Snowflake algorithm does."

---

### Interviewer's Internal Assessment:

✅ *Excellent progression. The candidate walked through six approaches — single counter, DB auto-increment, UUID v4, UUID v1, ticket servers, range allocation — and clearly articulated why each fails. The B-tree fragmentation analysis for UUIDs shows real database knowledge. The synthesis at the end (identifying the fundamental tension) demonstrates strong systems thinking. This is L6-level reasoning — they didn't just jump to Snowflake, they built a case for why it's necessary.*

---

## PHASE 5: High-Level Architecture (~5 min)

**Interviewer:**
Good synthesis. Now before you go into the bit-level details, sketch me the high-level architecture. How does this system look in production? What are all the components?

**Candidate:**

> "Let me draw the complete architecture. There are **two paths** for ID generation — the primary path (embedded SDK, no network) and a secondary path (REST/gRPC proxy behind a load balancer for legacy services). Let me show both."

```
 PATH 1: PRIMARY — Embedded SDK (no network, sub-microsecond)
 ════════════════════════════════════════════════════════════════

 ┌───────────────────────────────────────────────────────────────────────────┐
 │                          Application Services                             │
 │            (Tweet Service, Order Service, Chat Service, etc.)             │
 └─────────┬──────────────────────┬──────────────────────┬──────────────────┘
           │                      │                      │
    ┌──────▼──────┐        ┌──────▼──────┐        ┌──────▼──────┐
    │  App Server │        │  App Server │        │  App Server │
    │  (us-east)  │        │  (us-west)  │        │  (eu-west)  │
    │             │        │             │        │             │
    │ ┌─────────┐ │        │ ┌─────────┐ │        │ ┌─────────┐ │
    │ │Snowflake│ │        │ │Snowflake│ │        │ │Snowflake│ │
    │ │  SDK    │ │        │ │  SDK    │ │        │ │  SDK    │ │
    │ │ (lib)   │ │        │ │ (lib)   │ │        │ │ (lib)   │ │
    │ │         │ │        │ │         │ │        │ │         │ │
    │ │wkr=42   │ │        │ │wkr=7    │ │        │ │wkr=183  │ │
    │ │seq=0    │ │        │ │seq=0    │ │        │ │seq=0    │ │
    │ │ts=100042│ │        │ │ts=100042│ │        │ │ts=100042│ │
    │ └─────────┘ │        │ └─────────┘ │        │ └─────────┘ │
    │  ID = local │        │  ID = local │        │  ID = local │
    │  call ~0.1us│        │  call ~0.1us│        │  call ~0.1us│
    └─────────────┘        └─────────────┘        └─────────────┘
           │                      │                      │
           │ Registration         │ Registration         │ Registration
           │ (startup only)       │ (startup only)       │ (startup only)
           ▼                      ▼                      ▼

 PATH 2: SECONDARY — REST/gRPC Proxy (for legacy services, ~1-2ms)
 ════════════════════════════════════════════════════════════════════

 ┌─────────────────────────────────────────────────────────────────┐
 │  Legacy / External Services                                     │
 │  (Perl scripts, third-party integrations, batch jobs, etc.)    │
 └──────────────────────────┬──────────────────────────────────────┘
                            │
                            │  POST /v1/id/generate
                            │  POST /v1/id/batch
                            │  GET  /v1/id/{id}/decode
                            ▼
                 ┌─────────────────────┐
                 │   Load Balancer     │
                 │   (L7 — ALB/NLB)   │
                 │                     │
                 │  Health checks:     │
                 │  GET /healthz       │
                 │  Route: round-robin │
                 └────┬──────────┬─────┘
                      │          │
              ┌───────▼───┐ ┌───▼───────┐
              │ ID Proxy  │ │ ID Proxy  │       (stateless, horizontally
              │ Service A │ │ Service B │        scalable — each instance
              │           │ │           │        has its own Snowflake SDK
              │ ┌───────┐ │ │ ┌───────┐ │        with a unique worker_id)
              │ │  SDK  │ │ │ │  SDK  │ │
              │ │wkr=900│ │ │ │wkr=901│ │
              │ └───────┘ │ │ └───────┘ │
              └───────────┘ └───────────┘

 SHARED INFRASTRUCTURE
 ═════════════════════

 ┌─────────────────────────────────────────────────────────────────┐
 │               ZooKeeper / etcd Ensemble                         │
 │          (Worker ID Registration & Coordination)                │
 │                                                                 │
 │   /snowflake/workers/                                           │
 │     ├── worker-7   (app-server-2, us-west, session=0xA2)      │
 │     ├── worker-42  (app-server-1, us-east, session=0xB7)      │
 │     ├── worker-183 (app-server-3, eu-west, session=0xC1)      │
 │     ├── worker-900 (id-proxy-a,   us-east, session=0xD4)      │
 │     └── worker-901 (id-proxy-b,   us-east, session=0xE5)      │
 │                                                                 │
 │   Ephemeral nodes: auto-deleted on process crash                │
 │   Accessed at startup only — NOT on the hot path                │
 └─────────────────────────────────────────────────────────────────┘
            ▲                                         ▲
            │ register (startup)                      │ register (startup)
            │                                         │
     App Servers (Path 1)                    Proxy Services (Path 2)


 ┌─────────────────────────────────────────────────────────────────┐
 │                      NTP Infrastructure                         │
 │                                                                 │
 │   ┌──────────┐   ┌──────────┐   ┌────────────────┐            │
 │   │ NTP      │   │ NTP      │   │ Amazon Time    │            │
 │   │ Stratum 1│   │ Stratum 1│   │ Sync Service   │            │
 │   │ (GPS)    │   │ (atomic) │   │ (169.254.169.  │            │
 │   │          │   │          │   │  123)           │            │
 │   └──────────┘   └──────────┘   └────────────────┘            │
 │                                                                 │
 │   Keeps ALL worker clocks (app servers + proxies) within ~1ms  │
 │   of each other. Critical for k-sortability of generated IDs.  │
 └─────────────────────────────────────────────────────────────────┘


 ┌─────────────────────────────────────────────────────────────────┐
 │                 Admin API Gateway                                │
 │         (for operational / monitoring endpoints)                 │
 │                                                                 │
 │   Routes:                                                       │
 │     GET  /v1/admin/cluster         → Cluster overview           │
 │     GET  /v1/admin/clock/status    → Fleet clock drift          │
 │     GET  /v1/admin/workers/{id}    → Worker health              │
 │     POST /v1/admin/workers/register → Manual registration       │
 │                                                                 │
 │   Auth: mTLS + IAM role (ops team only)                        │
 │   Rate limit: 100 req/sec (admin, not high-traffic)            │
 └─────────────────────────────────────────────────────────────────┘
```

#### Core Components

> "There are **seven components** in the production architecture:
>
> 1. **Snowflake SDK (Embedded Library)** — The heart of the system. An in-process library linked into every application server. Contains the Snowflake generator: timestamp + worker_id + sequence → 64-bit ID. **Zero network calls** for ID generation. This is what makes it sub-microsecond.
>
> 2. **Application Servers** — Any service that needs unique IDs (tweet creation, order placement, message sending). Each server has a Snowflake SDK instance with a unique worker_id. The SDK is just another dependency — like a logging library.
>
> 3. **Load Balancer (L7 — ALB/NLB)** — Sits in front of the ID Proxy Services for Path 2. Routes requests round-robin to healthy proxy instances. Performs health checks (`GET /healthz`) and drains unhealthy proxies. NOT on Path 1 — the embedded SDK has no load balancer because there's no network hop.
>
> 4. **ID Proxy Services** — Thin, stateless REST/gRPC servers for legacy clients that can't embed the SDK. Each proxy instance has its own Snowflake SDK with a unique worker_id. Horizontally scalable — add more instances behind the LB for more throughput. Adds ~1-2ms latency (network RTT) compared to the in-process SDK.
>
> 5. **ZooKeeper / etcd (Coordination Service)** — Manages worker ID assignment for *both* app servers and proxy instances. Each process registers at startup, gets a unique worker_id (0–1023), and creates an ephemeral node. If the process crashes, the ephemeral node expires and the worker_id is recycled. **Only used at startup** — not on the hot path.
>
> 6. **NTP Infrastructure** — Keeps all server clocks synchronized to within ~1ms. This is critical because Snowflake relies on local clocks for timestamps. If clocks drift, IDs become misordered. We use multiple NTP sources and monitor drift as a tier-1 metric.
>
> 7. **Admin API Gateway** — A separate gateway for operational endpoints: cluster health, clock drift monitoring, worker management, sequence statistics. Authenticated via mTLS + IAM roles, rate-limited, and accessible only to the ops team. This is how SREs interact with the system — not through the ID generation path."

#### Why Two Paths?

> "
> | Aspect | Path 1: Embedded SDK | Path 2: Proxy + LB |
> |--------|---------------------|---------------------|
> | **Latency** | ~0.1-0.5 us (in-process) | ~1-2 ms (network RTT) |
> | **Throughput** | 4M IDs/sec per worker | ~10K-50K IDs/sec per proxy (network-bound) |
> | **Dependencies** | Local clock only | LB + proxy + local clock |
> | **Availability** | Fully independent | Depends on LB + proxy health |
> | **Language support** | Java, Go, Python, Rust SDKs | Any language with HTTP/gRPC client |
> | **Use cases** | Core services (95% of traffic) | Legacy, batch jobs, external (5%) |
>
> The load balancer is **only on Path 2** — and that's intentional. Path 1 has no network hop at all. If the interviewer's mental model is 'every request goes through a load balancer,' this system breaks that assumption. The LB is for the minority of clients that can't use the SDK directly."

#### Why This Architecture?

> "The critical insight is the **separation of the hot path from the coordination path**:
>
> ```
> HOT PATH (every ID):                    COLD PATH (startup only):
> ┌─────────────────────────────┐         ┌─────────────────────────┐
> │  1. Read local clock (~20ns)│         │  1. Connect to ZK       │
> │  2. Compare timestamp (~1ns)│         │  2. Register worker ID  │
> │  3. Increment sequence (~1ns)│        │  3. Cache ID to disk    │
> │  4. Bit-shift + OR   (~2ns)│         │  4. Start heartbeat     │
> │                             │         │                         │
> │  Total: ~50-150ns           │         │  Total: ~50-200ms       │
> │  Network calls: ZERO        │         │  Network calls: 2-3     │
> │  Dependencies: local clock  │         │  Dependencies: ZK       │
> └─────────────────────────────┘         └─────────────────────────┘
>
> A server generating 1,000 IDs/sec makes:
>   - 1,000 hot-path calls/sec (in-process, sub-microsecond each)
>   - 0 cold-path calls/sec (already registered)
>   - 1 ZK heartbeat every ~10 seconds (background, async)
> ```
>
> This is fundamentally different from the ticket server or database approaches where EVERY ID requires a network call. That's why Snowflake can do 4 million IDs/sec per worker while a ticket server tops out at ~10K."

---

### Interviewer's Internal Assessment:

✅ *Strong architecture. The candidate correctly identified that the key architectural decision is embedding the generator as a library (not a service), which eliminates the network call bottleneck that killed all the previous approaches. The two-path design is mature — Path 1 (SDK) for the 95% case with zero network overhead, Path 2 (proxy + LB) for legacy compatibility. The load balancer placement is deliberate: only on Path 2, explicitly NOT on the hot path. The Admin API Gateway as a separate entry point shows operational maturity — data plane (ID generation) and control plane (monitoring/management) are properly separated. The NTP infrastructure as a first-class component (not an afterthought) is an L6+ signal — they understand that clock accuracy is the foundation this system stands on.*

---

## PHASE 6: Snowflake Algorithm — The Core Solution (~12 min)

**Interviewer:**
Alright, design the Snowflake-style solution. I want the exact bit layout, the math, and the edge cases.

**Candidate:**

> "Twitter open-sourced Snowflake in 2010. The core insight is: **pack timestamp, worker identity, and a sequence counter into a single 64-bit integer**, with the timestamp as the most significant bits for natural sort ordering."

### Bit Layout

> "Here's the exact layout:
>
> ```
> 64-bit Snowflake ID:
> ┌──────────────────────────────────────────────────────────────────┐
> │ 0 │        41 bits          │   10 bits   │    12 bits          │
> │   │      Timestamp          │  Worker ID  │  Sequence Number    │
> │   │   (ms since epoch)      │ (machine ID)│  (per-ms counter)   │
> └──────────────────────────────────────────────────────────────────┘
>  ▲           ▲                      ▲              ▲
>  │           │                      │              │
>  │           │                      │              └─ 12 bits = 4,096 IDs
>  │           │                      │                 per ms per worker
>  │           │                      │
>  │           │                      └─ 10 bits = 1,024 workers
>  │           │                         (can split: 5 datacenter + 5 machine)
>  │           │
>  │           └─ 41 bits = 2^41 ms = 2,199,023,255,552 ms
>  │              = ~69.7 years from custom epoch
>  │
>  └─ Sign bit (always 0 for positive IDs)
>
>
> Example ID generation:
> ───────────────────────
> Timestamp:  1706745600000 ms since custom epoch (Feb 1, 2024)
>   → Binary: 00000011000110100000011011001000000000000000 (41 bits)
>
> Worker ID:  42
>   → Binary: 0000101010 (10 bits)
>
> Sequence:   7 (7th ID generated in this millisecond)
>   → Binary: 000000000111 (12 bits)
>
> Combined:
>   0 | 00000011000110100000011011001000000000000000 | 0000101010 | 000000000111
>   └─┘└────────────────────────────────────────────┘└──────────┘└─────────────┘
>   sign           timestamp (41)                     worker(10)   sequence(12)
>
> Final 64-bit integer: 7,159,358,969,602,048,007
> ```"

### The Math

> "Let me work through the capacity of each field:
>
> **Timestamp (41 bits):**
> - 2^41 = 2,199,023,255,552 milliseconds
> - 2,199,023,255,552 / 1000 / 60 / 60 / 24 / 365.25 = **69.7 years**
> - If our custom epoch is January 1, 2020, IDs are valid until ~2089
> - After that, we'd need to migrate to a new epoch or wider ID format
>
> **Worker ID (10 bits):**
> - 2^10 = **1,024 unique workers**
> - Can subdivide: 5 bits datacenter (32 DCs) + 5 bits machine (32 machines/DC)
> - Or: 3 bits region (8 regions) + 7 bits machine (128 machines/region)
>
> **Sequence Number (12 bits):**
> - 2^12 = **4,096 IDs per millisecond per worker**
> - Per worker throughput: 4,096 × 1,000 = **4,096,000 IDs/sec** (theoretical max)
> - Total system: 1,024 workers × 4,096,000 = **~4.2 billion IDs/sec**
>
> ```
> Capacity Summary:
> ┌──────────────────┬──────────────┬──────────────────────────────┐
> │ Field            │ Bits         │ Capacity                     │
> ├──────────────────┼──────────────┼──────────────────────────────┤
> │ Sign             │ 1            │ Always 0 (positive)          │
> │ Timestamp        │ 41           │ 69.7 years from epoch        │
> │ Worker ID        │ 10           │ 1,024 concurrent workers     │
> │ Sequence         │ 12           │ 4,096 IDs per ms per worker  │
> ├──────────────────┼──────────────┼──────────────────────────────┤
> │ TOTAL            │ 64           │ 4.2 billion IDs/sec system   │
> └──────────────────┴──────────────┴──────────────────────────────┘
> ```"

### Generation Algorithm (Pseudocode)

> "Here's the core algorithm — it's surprisingly simple:
>
> ```
> class SnowflakeGenerator:
>     EPOCH = 1577836800000  # Jan 1, 2020 00:00:00 UTC (custom epoch)
>     WORKER_ID_BITS = 10
>     SEQUENCE_BITS = 12
>     MAX_WORKER_ID = (1 << 10) - 1   # 1023
>     MAX_SEQUENCE = (1 << 12) - 1     # 4095
>
>     def __init__(self, worker_id):
>         assert 0 <= worker_id <= MAX_WORKER_ID
>         self.worker_id = worker_id
>         self.sequence = 0
>         self.last_timestamp = -1
>
>     def generate_id(self):
>         timestamp = current_time_ms() - EPOCH
>
>         if timestamp < self.last_timestamp:
>             # CLOCK WENT BACKWARDS! Critical error.
>             raise ClockMovedBackwardsError(
>                 f"Clock moved backwards by {self.last_timestamp - timestamp}ms"
>             )
>
>         if timestamp == self.last_timestamp:
>             # Same millisecond — increment sequence
>             self.sequence = (self.sequence + 1) & MAX_SEQUENCE
>             if self.sequence == 0:
>                 # Sequence exhausted (4096 IDs in this ms)!
>                 # Wait for next millisecond
>                 timestamp = wait_next_millis(self.last_timestamp)
>         else:
>             # New millisecond — reset sequence
>             self.sequence = 0
>
>         self.last_timestamp = timestamp
>
>         # Compose the 64-bit ID
>         id = (timestamp << (WORKER_ID_BITS + SEQUENCE_BITS))  # 22 left shift
>            | (self.worker_id << SEQUENCE_BITS)                 # 12 left shift
>            | self.sequence
>
>         return id
>
>     def wait_next_millis(self, last_ts):
>         ts = current_time_ms() - EPOCH
>         while ts <= last_ts:
>             ts = current_time_ms() - EPOCH
>         return ts
> ```
>
> **Key properties of this algorithm:**
> - **No network calls** — purely in-memory. Each `generate_id()` is a few CPU instructions.
> - **Latency**: ~1-2 microseconds per call (compare to ~1-5ms for DB ticket servers)
> - **Thread-safe**: Need a mutex/lock around `generate_id()`, but the critical section is tiny (sub-microsecond)
> - **Monotonic within a worker**: IDs always increase for a given worker"

**Interviewer:**
Good. Now let me push on the edge cases. What happens if the system clock goes backwards?

---

## PHASE 7: Deep Dive — Edge Cases & Failure Modes (~10 min)

### Edge Case 1: Clock Skew (Clock Goes Backwards)

**Candidate:**

> "This is the most critical edge case. Clocks can go backwards for several reasons:
> - **NTP correction** — NTP synchronization can adjust the clock backwards by milliseconds or even seconds
> - **Leap seconds** — The OS may 'smear' a leap second or step backwards
> - **VM live migration** — Clock can jump when a VM is migrated between physical hosts
> - **Manual clock adjustment** — An operator accidentally changes the system time
>
> **Why it's dangerous**: If the clock goes backwards, we might generate an ID with a *smaller* timestamp than a previously generated ID — but with sequence=0. This could produce a **duplicate ID** if the (timestamp, worker_id, sequence) tuple matches a previously generated one.
>
> **Strategies to handle clock skew:**
>
> **Strategy 1: Refuse to generate IDs (Twitter Snowflake's approach)**
> ```
> if timestamp < last_timestamp:
>     raise ClockMovedBackwardsError(...)
>     // Or: return error code, let the caller retry
> ```
> - Simple, safe, no duplicates possible
> - But: the worker is DOWN until the clock catches up — could be seconds or minutes
>
> **Strategy 2: Wait for clock to catch up**
> ```
> if timestamp < last_timestamp:
>     sleep(last_timestamp - timestamp)
>     timestamp = last_timestamp
> ```
> - Better availability than outright refusing
> - But: blocks the calling thread for potentially seconds
>
> **Strategy 3: Use last_timestamp instead of current clock (extend time)**
> ```
> if timestamp < last_timestamp:
>     timestamp = last_timestamp  // pretend the clock didn't go back
>     // Continue with sequence increment
> ```
> - Best availability — never blocks
> - Risk: if the clock goes back significantly (minutes), the 'last_timestamp' drifts far from real time, and IDs are no longer truly time-ordered
>
> **Strategy 4: Logical clock / Lamport-style (Discord's approach)**
> ```
> if timestamp <= last_timestamp:
>     timestamp = last_timestamp  // use the higher of real vs logical time
> ```
> - IDs remain monotonically increasing regardless of clock behavior
> - Trade-off: IDs may not reflect real wall-clock time during clock skew periods
>
> **My recommendation**: Strategy 4 for small clock skew (< 5 seconds) — use last_timestamp. For large clock skew (> 5 seconds), refuse to generate IDs and alert operators. This balances availability with correctness."

**Interviewer:**
Good analysis. Now, how does NTP actually work, and how does it interact with this?

**Candidate:**

> "NTP (Network Time Protocol) synchronizes clocks across machines:
>
> ```
> NTP Correction Behavior:
> ─────────────────────────
> Small drift (< 128ms):   NTP slews the clock — gradually speeds up or slows
>                           down the clock rate. No backwards jump. SAFE.
>
> Medium drift (128ms-1000s): NTP steps the clock — instant jump forward or
>                             backward. DANGEROUS for Snowflake.
>
> Large drift (> 1000s):   NTP panics — refuses to adjust, logs error.
>                          Manual intervention needed.
> ```
>
> **Best practice for Snowflake workers:**
> - Configure NTP with `tinker panic 0` — never panic, always try to correct
> - Use `slew mode` (ntpd -x) — always slew, never step. Maximum slew rate is 500 ppm (0.05%), so the clock adjusts smoothly. But: if the drift is > 128ms, slewing takes a long time to converge.
> - Monitor clock offset: alert if |offset| > 10ms. If > 100ms, drain the worker and restart.
> - Use hardware timestamping (PTP) for sub-microsecond accuracy in critical environments."

### Edge Case 2: Sequence Exhaustion (> 4,096 IDs in 1 ms)

**Interviewer:**
What if a worker needs to generate more than 4,096 IDs in a single millisecond?

**Candidate:**

> "When the sequence counter rolls over from 4095 to 0, we've exhausted all IDs for this millisecond on this worker. The algorithm handles this by **busy-waiting** until the next millisecond:
>
> ```
> if self.sequence == 0:  // rolled over!
>     timestamp = wait_next_millis(self.last_timestamp)
>
> // wait_next_millis spins until clock advances:
> while current_time_ms() <= last_timestamp:
>     // spin
> ```
>
> **Impact analysis:**
> - 4,096 IDs/ms = 4,096,000 IDs/sec per worker. For most use cases, this is far more than enough.
> - If we consistently hit this limit, we have options:
>   1. **Add more workers** — spread the load across more worker IDs
>   2. **Pre-generate IDs** — generate in batches during quiet periods
>   3. **Increase sequence bits** — steal from worker ID bits:
>
> ```
> Alternative bit layouts:
> ┌──────────────────────────────────────────────────────────────┐
> │ Layout          │ Workers │ Seq/ms  │ IDs/sec/worker │ Total│
> ├──────────────────────────────────────────────────────────────┤
> │ 10 worker + 12 seq │ 1,024 │ 4,096   │ 4.1M          │ 4.2B │
> │ 8 worker + 14 seq  │ 256   │ 16,384  │ 16.4M         │ 4.2B │
> │ 12 worker + 10 seq │ 4,096 │ 1,024   │ 1.0M          │ 4.2B │
> │ 5 worker + 17 seq  │ 32    │ 131,072 │ 131M          │ 4.2B │
> └──────────────────────────────────────────────────────────────┘
>
> Note: Total system capacity is always 2^22 × 1000 = ~4.2 billion IDs/sec
> regardless of how you split the bits. The split is a TRADEOFF between
> max workers and max throughput per worker.
> ```
>
> **The busy-wait is not a real problem** because:
> - We're waiting at most 1ms (until the clock ticks forward)
> - At 4M IDs/sec per worker, you'd need truly exceptional traffic to hit this
> - If you are hitting it, the right answer is more workers, not longer sequences"

### Edge Case 3: Worker ID Assignment & Collisions

**Interviewer:**
How do you prevent two workers from getting the same worker ID? If two workers have worker_id=42, they could generate identical IDs in the same millisecond.

**Candidate:**

> "Worker ID collision is a **uniqueness-breaking** bug — it MUST be prevented. Several strategies:
>
> **Strategy 1: Pre-configured worker IDs**
> ```
> # In config file / environment variable:
> WORKER_ID=42
>
> # Each deployment assigns a unique worker ID manually or via
> # infrastructure tooling (Terraform, Kubernetes ordinal index)
> ```
> - Simple but error-prone — human mistakes can cause collisions
> - Works for small, static deployments
>
> **Strategy 2: ZooKeeper / etcd coordination (Twitter's approach)**
> ```
> On startup:
> 1. Worker connects to ZooKeeper
> 2. Creates an ephemeral sequential node: /snowflake/workers/worker-{sequential}
> 3. ZooKeeper assigns a unique sequence number → this is the worker ID
> 4. If the worker dies, the ephemeral node is deleted → worker ID recycled
>
> /snowflake/workers/
>   ├── worker-0000000001  (Node A, session=abc)
>   ├── worker-0000000002  (Node B, session=def)
>   └── worker-0000000003  (Node C, session=ghi)
> ```
> - Guarantees uniqueness — ZooKeeper provides sequential, unique IDs
> - Ephemeral nodes handle worker crashes — ID is released when session expires
> - Dependency on ZooKeeper — but only at startup, not on the hot path
>
> **Strategy 3: Database-backed worker registry**
> ```sql
> CREATE TABLE worker_registry (
>     worker_id INT PRIMARY KEY,
>     hostname VARCHAR(255),
>     registered_at TIMESTAMP,
>     last_heartbeat TIMESTAMP
> );
>
> -- On startup: INSERT with unique constraint
> -- On shutdown: DELETE
> -- Stale workers (no heartbeat for 5 min): reclaimed
> ```
>
> **Strategy 4: IP/MAC-based derivation**
> ```
> worker_id = hash(IP_address + process_port) % 1024
> ```
> - No coordination needed
> - Risk: hash collisions (two different IP:port combos hash to same worker_id)
> - Mitigation: verify uniqueness against a registry on startup
>
> **My recommendation**: ZooKeeper/etcd for registration with a fallback. The coordination happens only at startup (not per-ID), so the ZooKeeper dependency is acceptable. Store the assigned worker_id locally — if ZooKeeper is briefly unavailable during startup, the worker can use its last-known ID if it hasn't been reassigned."

**Interviewer:**
What if ZooKeeper is down and a worker needs to start?

**Candidate:**

> "Good edge case. Strategies:
>
> 1. **Cache the last worker ID on disk** — On startup, read from local file. If ZooKeeper is down, use cached ID with a warning log. Risk: if the worker was replaced and the old ID was reassigned, we have a collision. Mitigation: verify against ZooKeeper when it comes back.
>
> 2. **Pre-provisioned pool** — Have a pool of worker IDs pre-assigned per host in configuration management (Puppet/Chef/Ansible). No runtime coordination needed.
>
> 3. **Graceful degradation** — If ZooKeeper is down and no cached ID, refuse to generate IDs. Alert operators. This is the safest option — duplicates are worse than downtime."

---

### Interviewer's Internal Assessment:

✅ *Strong edge case analysis. The candidate covered the three critical failure modes — clock skew, sequence exhaustion, worker ID collision — with multiple strategies for each. The NTP slew vs step distinction shows operational knowledge. The bit layout tradeoff table demonstrates quantitative reasoning. The ZooKeeper discussion was practical, not theoretical. This is solid L6+ depth.*

---

## PHASE 8: Alternative Approaches & Comparisons (~5 min)

**Interviewer:**
You've designed the Snowflake approach. Now compare it to other real-world approaches. What do Instagram, Discord, and MongoDB do differently?

**Candidate:**

### Instagram's Approach (2012)

> "Instagram needed 64-bit sortable IDs but didn't want to deploy a separate Snowflake service. They used **PostgreSQL's PL/pgSQL** to generate IDs inside the database:
>
> ```
> Instagram ID layout (64 bits):
> ┌───────────────────────────────────────────────────┐
> │  41 bits           │  13 bits     │  10 bits      │
> │  Timestamp         │  Shard ID    │  Sequence     │
> │  (ms since epoch)  │  (DB shard)  │  (per-shard)  │
> └───────────────────────────────────────────────────┘
>
> - Epoch: January 1, 2011
> - 13 bits shard ID = 8,192 logical shards
> - 10 bits sequence = 1,024 IDs per ms per shard
> - Generated inside a PostgreSQL stored procedure
>   using the shard ID of the database instance
>
> CREATE OR REPLACE FUNCTION next_id(OUT result BIGINT) AS $$
> DECLARE
>     epoch BIGINT := 1314220021721;
>     seq_id BIGINT;
>     now_ms BIGINT;
>     shard_id INT := 5;  -- unique per PG shard
> BEGIN
>     SELECT nextval('table_id_seq') % 1024 INTO seq_id;
>     SELECT FLOOR(EXTRACT(EPOCH FROM now()) * 1000) INTO now_ms;
>     result := (now_ms - epoch) << 23;
>     result := result | (shard_id << 10);
>     result := result | seq_id;
> END;
> $$ LANGUAGE PLPGSQL;
> ```
>
> **Key difference from Snowflake**: ID generation happens *inside the database*, not in a standalone service. No separate deployment. But tied to PostgreSQL — every ID requires a DB call."

### Discord's Approach (2016)

> "Discord generates Snowflake-style IDs but with some modifications:
>
> ```
> Discord Snowflake (64 bits):
> ┌───────────────────────────────────────────────────────┐
> │  42 bits           │  5 bits  │  5 bits  │  12 bits   │
> │  Timestamp         │ Worker   │ Process  │  Sequence  │
> │  (ms since epoch)  │  ID      │  ID      │  Counter   │
> └───────────────────────────────────────────────────────┘
>
> Epoch: January 1, 2015 (Discord's custom epoch)
> 42 bits timestamp = ~139 years (vs Snowflake's 69 years with 41 bits)
> Worker ID + Process ID = 1,024 unique generators
> 12 bits sequence = 4,096 per ms
> ```
>
> **Key difference**: Uses 42 bits for timestamp (one more than Snowflake) by using a later epoch, giving more years of operation. Also explicitly separates worker and process IDs."

### MongoDB ObjectId (2009)

> "MongoDB uses 96-bit (12-byte) IDs — not 64-bit, but worth comparing:
>
> ```
> MongoDB ObjectId (96 bits):
> ┌──────────────┬──────────┬──────────┬──────────────┐
> │  32 bits     │  40 bits │  24 bits │  24 bits     │
> │  Timestamp   │  Random  │  Random  │  Counter     │
> │  (seconds)   │  (per    │  (per    │  (increment) │
> │              │  machine)│  process)│              │
> └──────────────┴──────────┴──────────┴──────────────┘
>
> - Timestamp: seconds (not ms) → coarser ordering
> - 40 bits random per machine + 24 bits random per process:
>   provides uniqueness without coordination
> - 24 bits counter: 16.7M IDs per second per process
> ```
>
> **Key difference**: 96 bits (larger than 64), second-level timestamp granularity, uses randomness instead of deterministic worker IDs."

### Comparison Table

> "Here's the full comparison:
>
> ```
> ┌───────────────────┬──────────┬──────────┬───────────┬───────────┬──────────────┐
> │ Approach          │ Bits     │ Sortable │ Coord-    │ Per-Worker │ Real-World   │
> │                   │          │ by Time  │ Free?     │ IDs/sec   │ User         │
> ├───────────────────┼──────────┼──────────┼───────────┼───────────┼──────────────┤
> │ Auto-Increment    │ 64       │ ✅ Yes    │ ❌ No     │ ~10K      │ Simple apps  │
> │ UUID v4           │ 128      │ ❌ No     │ ✅ Yes    │ Unlimited │ General use  │
> │ UUID v1           │ 128      │ ❌ Sort*  │ ✅ Yes    │ Unlimited │ Legacy       │
> │ Ticket Server     │ 64       │ ✅ ~Yes   │ ❌ No     │ ~10K      │ Flickr       │
> │ Range Alloc       │ 64       │ ❌ No     │ ❌ Partial │ Unlimited │ Google       │
> │ Snowflake         │ 64       │ ✅ Yes    │ ✅ Yes**  │ 4.1M      │ Twitter      │
> │ Instagram         │ 64       │ ✅ Yes    │ ❌ No     │ ~1K/shard │ Instagram    │
> │ Discord           │ 64       │ ✅ Yes    │ ✅ Yes**  │ 4.1M      │ Discord      │
> │ MongoDB ObjectId  │ 96       │ ✅ ~Yes   │ ✅ Yes    │ 16.7M     │ MongoDB      │
> │ ULID              │ 128      │ ✅ Yes    │ ✅ Yes    │ Unlimited │ Various      │
> │ UUID v7 (2024)    │ 128      │ ✅ Yes    │ ✅ Yes    │ Unlimited │ Future std   │
> └───────────────────┴──────────┴──────────┴───────────┴───────────┴──────────────┘
>
> * UUID v1 has timestamp but bits are not in sort order
> ** Coordination-free for ID generation; needs coordination for worker ID assignment
> ```"

---

## PHASE 9: Operational Concerns & Production Readiness (~5 min)

**Interviewer:**
Let's talk about running this in production. What monitoring, alerts, and operational procedures do you need?

**Candidate:**

### Monitoring & Alerting

> "**Critical metrics to monitor:**
>
> ```
> ┌──────────────────────────────────────────────────────────────────────┐
> │ Metric                    │ Alert Threshold     │ Action             │
> ├──────────────────────────────────────────────────────────────────────┤
> │ clock_offset_ms           │ > 10ms              │ Investigate NTP    │
> │ (NTP offset)              │ > 100ms             │ Drain worker       │
> │                           │ > 500ms             │ Kill worker        │
> ├──────────────────────────────────────────────────────────────────────┤
> │ clock_backwards_events    │ > 0                 │ Page on-call       │
> │                           │                     │ Check NTP config   │
> ├──────────────────────────────────────────────────────────────────────┤
> │ sequence_exhaustion_count │ > 10/sec            │ Add workers        │
> │ (times seq rolled over)   │ > 100/sec           │ Scale urgently     │
> ├──────────────────────────────────────────────────────────────────────┤
> │ ids_generated_per_sec     │ > 3M/sec/worker     │ Approaching limit  │
> │                           │ > 3.5M/sec/worker   │ Critical: add      │
> │                           │                     │ workers             │
> ├──────────────────────────────────────────────────────────────────────┤
> │ worker_id_collisions      │ > 0                 │ CRITICAL: stop     │
> │                           │                     │ one worker          │
> ├──────────────────────────────────────────────────────────────────────┤
> │ epoch_remaining_years     │ < 10 years          │ Plan migration     │
> │                           │ < 5 years           │ Begin migration    │
> └──────────────────────────────────────────────────────────────────────┘
> ```"

### Epoch Selection

> "The custom epoch is a **one-time irreversible decision**:
>
> - **Why not use Unix epoch (1970)?** Because 41 bits from 1970 = 1970 + 69.7 years = **2039**. Only ~13 years left! We'd burn 54 years of ID space for time before our system existed.
> - **Best practice**: Set the epoch to the launch date of the system (or slightly before). This maximizes the usable time range.
> - **Example**: Epoch = Jan 1, 2020 → System works until 2089. Epoch = Jan 1, 2025 → System works until 2094.
>
> ```
> Time range for different epochs (41 bits):
>
> Unix Epoch (1970):   |████████████████████░░░░| 2039  ← only 13 years left!
> Custom 2020:         |░░░░░░░░░░░░░░░░░░░░████████████████████████| 2089
> Custom 2025:         |░░░░░░░░░░░░░░░░░░░░░░████████████████████████| 2094
>                      1970                    2025                    2094
> ```"

### Graceful Degradation

> "What happens when things go wrong:
>
> | Failure | Impact | Mitigation |
> |---|---|---|
> | **ZooKeeper down** (startup) | New workers can't register | Use cached worker ID from disk; alert ops |
> | **ZooKeeper down** (running) | No impact — already have worker ID | Monitor; reconnect when available |
> | **Clock goes backwards (<5s)** | Use logical clock (last_timestamp) | Log warning; monitor drift |
> | **Clock goes backwards (>5s)** | Stop generating IDs | Alert; drain traffic; fix NTP |
> | **Sequence exhausted** | Wait 1ms (max) | Transparent; monitor frequency |
> | **Worker process crashes** | Lost in-flight IDs | Ephemeral ZK node expires; ID recycled |
> | **Entire DC outage** | Workers in that DC stop | Other DCs continue independently |"

---

## PHASE 10: Putting It All Together (~3 min)

**Interviewer:**
We're running low on time. Summarize your design and tell me what keeps you up at night.

**Candidate:**

> "**Summary of the complete design:**
>
> | Component | Choice | Why |
> |---|---|---|
> | **Algorithm** | Snowflake-style: 41-bit timestamp + 10-bit worker + 12-bit sequence | Time-sorted, 64-bit, no coordination on hot path |
> | **Bit layout** | 1 + 41 + 10 + 12 = 64 bits | 69.7 years lifespan, 1024 workers, 4096 IDs/ms/worker |
> | **Performance** | ~1-2 microseconds per ID (in-memory, no I/O) | Purely CPU-bound; no network, no disk |
> | **Worker registration** | ZooKeeper ephemeral sequential nodes | Unique worker IDs; auto-reclaimed on crash |
> | **Clock skew handling** | Logical clock for small drift; refuse + alert for large drift | Balance availability vs correctness |
> | **Epoch** | Custom (system launch date) | Maximizes 41-bit timestamp range |
> | **Sequence overflow** | Busy-wait until next millisecond | At most 1ms delay; transparent to callers |
> | **Metadata extraction** | Right-shift to extract timestamp, worker ID | Useful for debugging, analytics, routing |
> | **Monitoring** | NTP offset, sequence exhaustion, worker collisions | Proactive alerting before failures |
>
> **What keeps me up at night:**
>
> 1. **NTP failures causing clock skew** — If NTP fails across a fleet of machines, clocks drift independently. IDs may appear out of order across workers. Solution: monitor NTP sync status as a fleet-wide dashboard; treat NTP failure as a severity-1 event.
>
> 2. **Worker ID exhaustion** — With 10 bits, we have 1,024 workers max. In a large microservices environment with many ID-generating services, this could be limiting. Solution: allocate worker ID ranges per service (e.g., service A gets 0-127, service B gets 128-255) or use 12-bit worker ID (sacrifice 2 bits of sequence).
>
> 3. **Epoch expiry** — In 69.7 years, the timestamp overflows. This is a long time, but I've seen systems outlive their expected lifetime. Solution: Plan the migration strategy now (dual-write period, new epoch), but don't execute for decades.
>
> 4. **ID as a covert channel** — Snowflake IDs leak information: timestamp (when), worker ID (where). Competitors could analyze our IDs to estimate traffic volume, growth rate, infrastructure size. Solution: if needed, encrypt or hash the ID for external exposure.
>
> **Potential extensions:**
> - **Multi-region awareness** — Encode region in worker ID bits for locality-aware routing
> - **UUID v7 compatibility** — Map our 64-bit structure into the new UUID v7 standard (128-bit, time-ordered) for systems that require UUIDs
> - **Embedded sequence guarantees** — For consumers that need strict ordering within a partition, use the timestamp+sequence to provide per-worker total ordering"

---

## Interviewer's Final Assessment

**Hire Recommendation: Strong Hire (L6)**

| Dimension | Rating | Notes |
|---|---|---|
| **Problem Decomposition** | Exceeds Bar | Walked through 6 naive approaches before reaching Snowflake. Each rejection was well-reasoned with specific failure modes. |
| **Scale & Estimation** | Exceeds Bar | Concrete math: 69.7-year timestamp range, 4.2B IDs/sec system capacity, B-tree fragmentation analysis for UUIDs. |
| **Trade-off Analysis** | Exceeds Bar | Bit layout tradeoff table (workers vs sequence), clock skew strategies (4 options with pros/cons), comparison of 10+ approaches. |
| **Algorithm Design** | Exceeds Bar | Pseudocode was correct and complete. Covered all edge cases in the generation logic. |
| **Edge Case Handling** | Exceeds Bar | Clock skew (4 strategies), sequence exhaustion (math + alternatives), worker ID collision (4 strategies), NTP slew vs step. |
| **Real-World Knowledge** | Exceeds Bar | Referenced Twitter Snowflake, Instagram, Discord, MongoDB, Flickr, UUID v7. Knew implementation details. |
| **Operational Maturity** | Exceeds Bar | Monitoring table with specific thresholds, NTP configuration advice, epoch selection reasoning, graceful degradation matrix. |
| **Communication** | Exceeds Bar | Structured progression from naive to complex. Used diagrams, tables, pseudocode. Checked understanding. |
| **LP: Dive Deep** | Exceeds Bar | NTP slew vs step, B-tree page splits, PostgreSQL stored procedure for Instagram IDs — deep technical knowledge. |
| **LP: Think Big** | Meets Bar | Mentioned multi-region, UUID v7 compatibility, ID as a covert channel — forward-looking concerns. |

**Areas for growth:** Could have discussed testing strategies (how do you test for ID uniqueness across 1,000 workers? chaos engineering for clock skew). Could have discussed the relationship between ID ordering and distributed system causality (Lamport clocks, happens-before).

---

## Key Differences: L5 vs L6 Expectations for This Problem

| Aspect | L5 (SDE2) Expectation | L6 (SDE3) Expectation |
|---|---|---|
| **Requirements** | "We need unique IDs" | Drives conversation: k-sortability, 64-bit constraint, coordination-free, metadata extraction |
| **Naive Approaches** | Mentions UUID as an option | Walks through 4-6 approaches progressively, explaining why each fails with specific reasons |
| **Snowflake Design** | "Use timestamp + worker + sequence" | Exact bit layout with math (69.7 years, 4096/ms), pseudocode, custom epoch reasoning |
| **Clock Skew** | "Use NTP" | 4 strategies for handling backwards clock, NTP slew vs step, monitoring thresholds |
| **Worker ID** | "Assign them manually" | ZooKeeper ephemeral nodes, 4 assignment strategies, collision prevention |
| **Sequence Overflow** | Doesn't mention | Math proof (4096 per ms), bit layout tradeoff table, busy-wait analysis |
| **Real-World Systems** | Knows Snowflake exists | Compares Snowflake, Instagram, Discord, MongoDB, Flickr, UUID v7 with implementation details |
| **Operational Thinking** | Focuses on happy path | Monitoring metrics with thresholds, graceful degradation matrix, epoch planning |
| **Information Leakage** | Doesn't consider | Identifies ID as a covert channel for competitor analysis |

---

*This interview simulation complements the [API contracts](api-contracts.md), [system flows](flow.md), and [naive-to-scale evolution](naive-to-scale-evolution.md).*
