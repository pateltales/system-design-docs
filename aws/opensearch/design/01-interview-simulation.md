# System Design Interview Simulation: Design OpenSearch (Distributed Search & Analytics Engine)

> **Interviewer:** Principal Engineer (L8), AWS OpenSearch Team
> **Candidate Level:** SDE-3 (L6 — Senior Software Development Engineer)
> **Duration:** ~60 minutes
> **Date:** February 28, 2026

---

## PHASE 1: Opening & Problem Statement (~2 min)

**Interviewer:**
Welcome. I'm a Principal Engineer on the AWS OpenSearch team. For today's system design round, I want you to design a **distributed search and analytics engine** — think AWS OpenSearch or Elasticsearch. A system where clients can index documents (structured or unstructured), and then search across millions or billions of them with sub-second latency using full-text queries, filters, and aggregations.

I care about how you think through the data structures that make search fast, how you distribute data across a cluster, and the tradeoffs between indexing throughput, search latency, and operational complexity. I'll push on your decisions — that's calibration, not criticism.

Take it away.

---

## PHASE 2: Requirements Gathering & Scoping (~5 min)

**Candidate:**

> "Thanks! 'Search engine' is a broad design space — it could mean anything from a simple text index to a full observability platform. Let me scope this down.
>
> **Functional Requirements — what operations do we need?**
>
> - **Index a document** — store a JSON document with a given ID into a named index.
> - **Search documents** — full-text search across documents: given a query string like 'connection timeout error', return the most relevant matching documents, ranked by relevance.
> - **Aggregate/Analyze** — compute analytics over documents: counts, averages, histograms, top-N terms. Think 'how many 500 errors per minute, grouped by service?'
> - **Get by ID** — retrieve a specific document by its identifier.
> - **Bulk index** — ingest thousands of documents per request for high-throughput pipelines.
> - **Delete / Update** — remove or update existing documents.
>
> A few clarifying questions:
> - **What's the primary use case — application search or log analytics?** These have very different access patterns."

**Interviewer:** "Good question. Design for both. The system should handle application search (e-commerce product search, site search) AND log analytics (ingesting and searching application logs, infrastructure metrics). These are the two dominant use cases."

> "- **Do we need real-time search or is batch acceptable?** If I index a document, how quickly must it be searchable?"

**Interviewer:** "Near-real-time — within a second or so. Not instant, but not minutes either."

> "That's a critical constraint — it rules out batch-only architectures and tells me we need an incremental indexing approach. One more:
> - **Do we need to support structured queries (filters, ranges) alongside full-text search?**"

**Interviewer:** "Yes. Users need both. A query like 'find all error logs from service=payments in the last 24 hours containing connection refused' combines structured filtering (service, time range) with full-text search (connection refused)."

> "Perfect. That means we need two types of data structures: inverted indices for text search AND columnar stores (doc values) for filtering and aggregations. Let me capture the non-functional requirements.
>
> **Non-Functional Requirements:**
>
> | Dimension | Requirement | Rationale |
> |---|---|---|
> | **Search Latency** | p50 < 20ms, p99 < 100ms | Users expect instant search results. Log analysts need interactive exploration. |
> | **Indexing Throughput** | 100K+ documents/sec sustained | Log analytics generates massive ingest volume (thousands of servers, each emitting logs). |
> | **Near-Real-Time** | Document searchable within ~1 second of indexing | Near-real-time, not batch. Operators need to see recent logs quickly. |
> | **Scalability** | Billions of documents, petabytes of data | Log retention (30-365 days) at 100K docs/sec = billions of documents. |
> | **Availability** | 99.9%+ uptime | Search downtime means operators can't debug production issues. |
> | **Durability** | No data loss for indexed documents | Logs are the audit trail — losing them is unacceptable. |
> | **Multi-tenancy** | Multiple teams/indices on shared infrastructure | Cost efficiency requires shared clusters. |
>
> **Scale estimation:**
>
> - **Ingest rate**: 100K docs/sec × ~1 KB avg doc size = **100 MB/sec = 8.6 TB/day** of raw data
> - **30-day retention**: 8.6 TB × 30 = **~260 TB** of searchable data
> - **With 1 replica**: 260 TB × 2 = **520 TB** total storage
> - **Document count**: 100K/sec × 86,400 sec/day × 30 days = **~260 billion documents**
> - **Search load**: ~1,000 queries/sec from dashboards, alerts, and interactive users"

**Interviewer:**
Good scoping. You've identified the tension between indexing throughput and search latency — that's the fundamental tradeoff in this system. Let's move to APIs.

---

### L5 vs L6 vs L7 — Phase 2 Rubric

| Aspect | L5 (SDE-2) | L6 (SDE-3) | L7 (Principal) |
|---|---|---|---|
| **Functional Reqs** | Lists CRUD + search | Distinguishes search vs analytics, asks about use case (logs vs app search) | Additionally raises multi-language support, schema evolution, cross-cluster federation |
| **Non-Functional** | Mentions latency and scale | Quantifies p99 latency, ingest throughput, calculates storage needs | Frames NFRs as SLA commitments, discusses cost-per-query economics |
| **NRT Insight** | "Should be fast" | Identifies ~1s NRT window as a design constraint, understands it's not instant | Explains the segment-level mechanics behind NRT, discusses the refresh interval tradeoff |
| **Dual Data Structure** | "We need an index" | Identifies need for both inverted index AND doc values for different query types | Discusses BKD trees for range queries, discusses when each structure wins |

---

## PHASE 3: API Design (~5 min)

**Candidate:**

> "Let me define the core APIs. OpenSearch exposes a RESTful API over HTTP — every operation is a URL + JSON body.
>
> **1. Index a Document (Write Path)**
> ```
> PUT /{index}/_doc/{id}
> Content-Type: application/json
>
> {
>   "timestamp": "2026-02-28T10:15:30Z",
>   "service": "payments",
>   "level": "ERROR",
>   "message": "Connection refused to downstream-db:5432",
>   "host": "payments-pod-7a3f",
>   "trace_id": "abc-123-def"
> }
>
> Response: 201 Created
> {
>   "_index": "logs-2026-02-28",
>   "_id": "doc-uuid-1",
>   "_version": 1,
>   "result": "created"
> }
> ```
>
> **2. Bulk Index (Performance-Critical Write Path)**
> ```
> POST /_bulk
> Content-Type: application/x-ndjson
>
> {"index": {"_index": "logs-2026-02-28", "_id": "1"}}
> {"timestamp": "...", "service": "payments", "message": "..."}
> {"index": {"_index": "logs-2026-02-28", "_id": "2"}}
> {"timestamp": "...", "service": "orders", "message": "..."}
>
> Response: 200 OK
> { "took": 30, "errors": false, "items": [...] }
> ```
> The `_bulk` API is the performance-critical path — it amortizes per-request overhead across thousands of documents. Optimal batch size is 5-15 MB per request. This is how Fluent Bit, Logstash, and Firehose deliver data.
>
> **3. Search (Read Path — the most complex API)**
> ```
> POST /logs-*/_search
> {
>   "query": {
>     "bool": {
>       "must": [
>         { "match": { "message": "connection refused" } }
>       ],
>       "filter": [
>         { "term": { "service": "payments" } },
>         { "range": { "timestamp": { "gte": "2026-02-28T00:00:00Z" } } }
>       ]
>     }
>   },
>   "aggs": {
>     "errors_per_hour": {
>       "date_histogram": { "field": "timestamp", "calendar_interval": "hour" }
>     }
>   },
>   "size": 20,
>   "sort": [{ "timestamp": "desc" }]
> }
> ```
> This single request does three things simultaneously:
> 1. **Full-text search** (`match` on message) — uses the inverted index, scores with BM25
> 2. **Structured filtering** (`term` on service, `range` on timestamp) — uses doc values / BKD trees, no scoring needed
> 3. **Aggregation** (date histogram) — uses doc values to bucket documents by hour
>
> The `bool` query separates `must` (scored, affects relevance ranking) from `filter` (not scored, cached for reuse). This distinction is important: filters skip the scoring step and their results are cached at the segment level.
>
> **4. Get by ID**
> ```
> GET /logs-2026-02-28/_doc/doc-uuid-1
> ```
> Direct document retrieval — O(1) using translog or segment lookup. Not the primary access pattern.
>
> **Architectural decision in the API — why Query DSL instead of SQL?**
>
> Query DSL (the JSON-based query language) maps directly to the internal Lucene query tree. Each JSON clause becomes a Lucene query object. SQL would require a parser and optimizer — adding latency and limiting expressiveness. That said, OpenSearch does support SQL via the `_plugins/_sql` endpoint for analysts who prefer it, but it translates to Query DSL internally.
>
> For the full API reference, see [02-api-contracts.md](02-api-contracts.md)."

**Interviewer:**
Good. I like that you distinguished `must` from `filter` — that's an important performance optimization most candidates miss. Let's design the system.

---

### L5 vs L6 vs L7 — Phase 3 Rubric

| Aspect | L5 (SDE-2) | L6 (SDE-3) | L7 (Principal) |
|---|---|---|---|
| **API Design** | Lists basic CRUD endpoints | Distinguishes bulk vs single-doc, explains must vs filter, shows realistic query | Additionally discusses scroll/PIT for deep pagination, search_after for cursor-based paging |
| **Query DSL** | Shows a simple match query | Shows bool query with mixed scored + filtered clauses and aggregation | Discusses function_score, nested queries, cross-index search patterns |
| **Bulk API** | Mentions batching | Specifies optimal batch size (5-15 MB), explains NDJSON format | Discusses back-pressure, retry strategy, dead letter queues for failed documents |
| **Design Rationale** | Describes the API | Explains WHY Query DSL maps to Lucene internals | Contrasts with SQL query planning, discusses query rewriting and optimization |

---

## PHASE 4: High-Level Architecture — Iterative Build-Up (~15 min)

**Candidate:**

> "Let me start with the simplest thing that could possibly work, find the problems, and fix them incrementally."

### Attempt 0: Single-Node Brute-Force Search

> "Simplest design — one server, documents stored as JSON files. Search = scan every file.
>
> ```
>     Client
>       │
>       ▼
>   ┌────────────────────────────┐
>   │   Single Server Process    │
>   │                            │
>   │   Index: write JSON file   │
>   │   Search: grep ALL files   │
>   │                            │
>   │   /data/                   │
>   │     doc1.json              │
>   │     doc2.json              │
>   │     ...                    │
>   │     doc1000000.json        │
>   └────────────────────────────┘
> ```
>
> Search is a linear scan: for each document, load from disk, parse JSON, check if text matches. O(N) per query."

**Interviewer:**
What's wrong with that?

> "Everything:
>
> | Problem | Impact |
> |---------|--------|
> | **O(N) search** | 1M docs × 1ms/doc = 1,000 seconds per query. Unusable. |
> | **No relevance ranking** | Which results are 'best'? grep gives yes/no, not ranked results. |
> | **No durability** | Server crash = data loss. |
> | **No concurrent access** | Single-threaded reads/writes block each other. |
> | **Single point of failure** | Server dies = service dies. |
>
> The fundamental problem is **O(N) search**. We need a data structure that makes text search O(1) per term, not O(N) per query."

### Attempt 1: Inverted Index on a Single Node

> "The key insight: **build an index at write time so reads are fast**. This is the inverted index — the core data structure behind every search engine since the 1960s.
>
> ```
>     Client
>       │ HTTP
>       ▼
>   ┌────────────────────────────────────────────────────┐
>   │               OpenSearch Node                       │
>   │                                                    │
>   │   ┌─────────────┐     ┌──────────────────────┐    │
>   │   │  REST API    │────▶│    Lucene Engine      │    │
>   │   │  Layer       │◀────│                      │    │
>   │   └─────────────┘     │  ┌─────────────────┐ │    │
>   │                       │  │ In-Memory Buffer │ │    │
>   │   ┌─────────────┐    │  │ (new docs, NOT   │ │    │
>   │   │ Text        │    │  │  searchable yet) │ │    │
>   │   │ Analysis    │    │  └────────┬──────────┘ │    │
>   │   │ Pipeline    │    │           │ refresh     │    │
>   │   │             │    │           │ (every 1s)  │    │
>   │   │ "Connection │    │           ▼             │    │
>   │   │  refused"   │    │  ┌─────────────────┐   │    │
>   │   │      ↓      │    │  │ Segment 0       │   │    │
>   │   │ [connection, │    │  │ (immutable)     │   │    │
>   │   │  refused]   │    │  ├─────────────────┤   │    │
>   │   └─────────────┘    │  │ Segment 1       │   │    │
>   │                       │  │ (immutable)     │   │    │
>   │   ┌─────────────┐    │  ├─────────────────┤   │    │
>   │   │ Translog    │    │  │ Segment 2       │   │    │
>   │   │ (WAL)       │    │  │ (immutable)     │   │    │
>   │   │ write-ahead │    │  └─────────────────┘   │    │
>   │   │ durability  │    │                        │    │
>   │   └─────────────┘    └──────────────────────┘    │
>   └────────────────────────────────────────────────────┘
> ```
>
> **The inverted index** — think of it like the index at the back of a textbook:
>
> ```
> Term           → Posting List (document IDs containing this term)
> ─────────────────────────────────────────────────────────
> "connection"   → [doc1, doc47, doc203, doc891]
> "refused"      → [doc1, doc47, doc512]
> "timeout"      → [doc3, doc47, doc99]
> "payments"     → [doc1, doc3, doc891]
> ```
>
> Searching for 'connection refused' = look up 'connection' → get posting list, look up 'refused' → get posting list, **intersect** → [doc1, doc47]. O(1) per term lookup, not O(N) per query.
>
> **How a document gets indexed (write path):**
> 1. Document arrives via REST API
> 2. **Text analysis pipeline** transforms raw text into tokens: `"Connection REFUSED!"` → char filter → tokenizer → lowercase filter → `["connection", "refused"]`. The analyzer choice determines what searches will match — wrong analyzer = 'my search doesn't find anything' bugs.
> 3. Tokens are added to the **in-memory index buffer** — NOT yet searchable
> 4. Document is written to the **translog** (write-ahead log) — this provides durability. If the node crashes, replay the translog to recover buffered documents.
> 5. Every ~1 second, **refresh**: the in-memory buffer is flushed into a new immutable **Lucene segment** on disk. Now it's searchable. This is the 'near-real-time' gap.
>
> **Why immutable segments?** Each segment is a self-contained mini-index. Once written, it never changes. This enables:
> - Lock-free concurrent reads (no read-write conflicts)
> - Simple caching (segment data never invalidates)
> - Crash recovery (no partial writes — a segment is either fully written or not)
>
> **Deletes are soft**: A delete just marks the document as deleted in a bitmap. The document is still in the segment until a background **segment merge** compacts segments and physically removes deleted docs.
>
> **BM25 relevance scoring**: At query time, rank results by relevance. BM25 (Best Matching 25) considers:
> - Term frequency (how often the term appears in this document) — with saturation (diminishing returns for repeated terms)
> - Inverse document frequency (how rare the term is across all documents — rare terms are more discriminating)
> - Document length normalization (short documents with a match rank higher than long documents)
> - Default parameters: k1=1.2 (term frequency saturation), b=0.75 (length normalization)
>
> **Contrast with RDBMS**: A database uses B-tree indexes — great for exact match (`WHERE id = 5`) and range queries (`WHERE price BETWEEN 10 AND 50`). Terrible for tokenized text search. Inverted indices are the opposite: great for text, mediocre for exact point lookups. This is why you use OpenSearch for search and a database for transactions.
>
> For the full deep dive, see [03-indexing-and-inverted-index.md](03-indexing-and-inverted-index.md)."

**Interviewer:**
Good. You've got fast search on one node. What breaks?

> "
> | Problem | Impact |
> |---------|--------|
> | **Single node storage limit** | One machine has maybe 2-4 TB usable disk. We need 520 TB. |
> | **Single node CPU limit** | One machine can handle maybe 100-500 queries/sec. We need 1,000+. |
> | **Single point of failure** | Node dies = search is down, data is at risk. |
> | **No horizontal scaling** | Can't add more capacity without replacing the machine. |
>
> We need to split the data across multiple machines."

### Attempt 2: Sharding — Split the Index Across Nodes

> "The index is too big for one node. Split it into **shards** — each shard is an independent Lucene index living on its own node.
>
> ```
>     Client
>       │
>       ▼
>   ┌─────────────────────────────────────────┐
>   │         Coordinating Node                │
>   │                                         │
>   │  INDEX request:                         │
>   │    shard = hash(_id) % 3                │
>   │    → route to correct shard             │
>   │                                         │
>   │  SEARCH request:                        │
>   │    1. Scatter query to ALL shards       │
>   │    2. Each shard returns top-N IDs      │  ← Query Phase
>   │       + BM25 scores (lightweight)       │
>   │    3. Merge top-N from all shards       │
>   │    4. Fetch full docs for final top-N   │  ← Fetch Phase
>   │    5. Return to client                  │
>   └──┬──────────────┬──────────────┬────────┘
>      │              │              │
>      ▼              ▼              ▼
>   ┌──────┐      ┌──────┐      ┌──────┐
>   │Node 1│      │Node 2│      │Node 3│
>   │      │      │      │      │      │
>   │Shard0│      │Shard1│      │Shard2│
>   │ (P)  │      │ (P)  │      │ (P)  │
>   │      │      │      │      │      │
>   │Local │      │Local │      │Local │
>   │Lucene│      │Lucene│      │Lucene│
>   │Index │      │Index │      │Index │
>   └──────┘      └──────┘      └──────┘
>
>   Document routing: shard = hash(_id) % num_primary_shards
> ```
>
> **Why two-phase search (query then fetch)?**
>
> Phase 1 (Query): Each shard runs the query locally and returns only doc IDs + scores — not full documents. This is lightweight (maybe 20 bytes per result). The coordinating node merges these sorted lists to find the global top-N.
>
> Phase 2 (Fetch): The coordinating node sends a multi-get for only the final top-N document IDs to the shards that hold them. Those shards return full `_source` documents.
>
> Why not just return full documents in phase 1? If we request top-20 results and have 5 shards, phase 1 fetches 100 doc IDs + scores (~2 KB total). Returning full documents would be 100 × 1 KB = 100 KB. At 1,000 queries/sec × 5 shards, that's the difference between 2 MB/sec and 100 MB/sec of internal network traffic. The two-phase approach keeps the scatter step lightweight.
>
> **Shard count is fixed at index creation** — `hash(_id) % N` requires N to be constant. If N changes, every document hashes to a different shard. Changing shard count requires creating a new index and reindexing all data. This is the #1 operational pain point in OpenSearch.
>
> **Contrast with DynamoDB**: DynamoDB uses consistent hashing with auto-splitting partitions. Partition count grows automatically as data grows. OpenSearch requires you to pick shard count upfront. DynamoDB handles this better for KV workloads, but OpenSearch's Lucene-based shards enable full-text search that DynamoDB can't do.
>
> For the full deep dive, see [05-sharding-and-distribution.md](05-sharding-and-distribution.md)."

**Interviewer:**
What if Node 2 dies?

> "Shard 1 is gone. Data loss. Queries return partial results because one-third of the index is missing.
>
> | Problem | Impact |
> |---------|--------|
> | **No redundancy** | Node death = shard data loss. No way to recover. |
> | **Partial results** | Queries missing docs from dead shard — silently incomplete. |
> | **No read scaling** | Each shard has exactly one copy — can't serve more queries by adding more readers. |
> | **No failure detection** | Nobody knows a shard is missing. No cluster orchestration. |"

### Attempt 3: Replication + Master Election for High Availability

> "Add replica shards for redundancy and a master node for cluster orchestration.
>
> ```
>     Client
>       │
>       ▼
>   ┌─────────────────────────────────────────┐
>   │         Coordinating Node                │
>   │  (routes to primary OR replica)          │
>   └──┬──────────────┬──────────────┬────────┘
>      │              │              │
>      ▼              ▼              ▼
>
>    AZ-1              AZ-2              AZ-3
>   ┌─────────────┐  ┌─────────────┐  ┌─────────────┐
>   │ Data Node 1 │  │ Data Node 2 │  │ Data Node 3 │
>   │             │  │             │  │             │
>   │ Shard0 (P)──┼──┼─Shard0 (R) │  │             │
>   │             │  │             │  │ Shard0 (R)  │
>   │ Shard1 (R)  │  │ Shard1 (P)──┼──┼─Shard1 (R) │
>   │             │  │             │  │             │
>   │ Shard2 (R)  │  │             │  │ Shard2 (P)  │
>   └─────────────┘  └─────────────┘  └─────────────┘
>
>   ┌──────────┐  ┌──────────┐  ┌──────────┐
>   │ Master   │  │ Master   │  │ Master   │
>   │ Node 1   │  │ Node 2   │  │ Node 3   │
>   │ (elected │  │ (standby)│  │ (standby)│
>   │  leader) │  │          │  │          │
>   └──────────┘  └──────────┘  └──────────┘
>        ▲              ▲              ▲
>        └──────────────┴──────────────┘
>           Cluster state replication
>           (shard table, mappings, settings)
>
>   Write path: Client → Coord → Primary Shard → Replica Shards → Ack
>   Read path:  Client → Coord → ANY copy (Primary or Replica) → Return
> ```
>
> **Replica shards**: Each primary shard has R replicas on different nodes (and different AZs via zone awareness). Replicas serve two purposes:
> 1. **Redundancy**: If a node dies, replicas on other nodes still have the data. Master promotes a replica to primary.
> 2. **Read scaling**: Search requests can hit any copy (primary or replica). With 1 replica, each query can be served by 2 copies — doubling search throughput.
>
> **Write path with replication**:
> 1. Client → Coordinating Node → route to Primary Shard (based on `hash(_id) % N`)
> 2. Primary shard: write to translog + in-memory buffer
> 3. Primary shard → replicate to all replica shards (parallel)
> 4. Wait for replicas to acknowledge (configurable: `wait_for_active_shards`)
> 5. Ack to client
>
> **Dedicated master nodes (3)**: Lightweight nodes that hold no data. They manage **cluster state**:
> - Which indices exist, their mappings and settings
> - Which shards exist on which nodes (the shard allocation table)
> - Node membership (which nodes are alive)
>
> Leader election via quorum: majority of 3 = 2 must agree. If network partition isolates 1 master from 2 others, the 2-node partition elects a new leader; the isolated node steps down. This prevents **split-brain** (two masters accepting writes independently → data corruption).
>
> **Zone awareness**: Master's allocation algorithm ensures replicas of the same shard are in different AZs. If AZ-2 goes down entirely, AZ-1 and AZ-3 still have at least one copy of every shard. 3-AZ deployment survives any single-AZ failure without data loss.
>
> **Cluster health signal**:
> - **Green**: All primary and replica shards allocated. Everything is healthy.
> - **Yellow**: All primaries OK, but some replicas unassigned. Functional but degraded — one more failure could cause data loss.
> - **Red**: Some primary shards unassigned. Active data loss risk. Queries return partial/incorrect results.
>
> **Contrast with Cassandra**: Cassandra uses consistent hashing with virtual nodes — any node can accept reads and writes (masterless). OpenSearch has a master for cluster orchestration, and writes always go to the primary shard first. Cassandra's masterless design is simpler for availability, but OpenSearch's primary-based replication ensures consistent segment structure across replicas."

**Interviewer:**
Now you have redundancy and cluster management. What's still broken?

> "
> | Problem | Impact |
> |---------|--------|
> | **All data on expensive hot storage** | 6-month-old logs consume the same expensive EBS as today's logs. 520 TB of hot EBS = massive cost. |
> | **No lifecycle management** | Old indices pile up forever unless manually deleted. Operators forget → disk fills → cluster goes Red. |
> | **Ad-hoc ingestion** | Applications POST directly to `_bulk` — no buffering, parsing, or backpressure. If cluster is slow, apps fail. |
> | **No visualization** | Raw JSON API only. Operators need dashboards, not curl commands. |
> | **No schema management** | Dynamic mapping auto-detects types → field type explosion if logs are inconsistent. |"

### Attempt 4: Ingestion Pipeline + Tiered Storage + Dashboards

> "This is where we go from 'distributed search engine' to 'operational log analytics platform.'
>
> ```
>                           DATA SOURCES
>     ┌──────────┐  ┌──────────┐  ┌──────────────┐  ┌───────────┐
>     │App Logs  │  │CloudWatch│  │VPC Flow Logs │  │Custom     │
>     │(stdout)  │  │Logs      │  │              │  │Metrics    │
>     └────┬─────┘  └────┬─────┘  └──────┬───────┘  └─────┬─────┘
>          │             │               │                 │
>          ▼             ▼               ▼                 ▼
>     ┌──────────────────────────────────────────────────────────┐
>     │               INGESTION LAYER                            │
>     │                                                          │
>     │  ┌────────────┐  ┌──────────────┐  ┌────────────────┐   │
>     │  │ Fluent Bit  │  │ Amazon Data  │  │ OpenSearch     │   │
>     │  │ / Fluentd   │  │ Firehose     │  │ Ingestion (OSI)│   │
>     │  │             │  │              │  │                │   │
>     │  │ Collect,    │  │ Buffer,      │  │ Managed OTel-  │   │
>     │  │ parse,      │  │ batch,       │  │ based pipeline │   │
>     │  │ forward     │  │ retry,       │  │ (traces, logs, │   │
>     │  │             │  │ backpressure │  │  metrics)      │   │
>     │  └──────┬─────┘  └──────┬───────┘  └───────┬────────┘   │
>     └─────────┼───────────────┼──────────────────┼─────────────┘
>               │               │                  │
>               ▼               ▼                  ▼
>     ┌──────────────────────────────────────────────────────────┐
>     │               OPENSEARCH CLUSTER                          │
>     │                                                          │
>     │  ┌────────────────────────────────────────────────────┐  │
>     │  │ Ingest Pipeline (transforms at index time)         │  │
>     │  │ grok → date → geoip → rename → script              │  │
>     │  └─────────────────────┬──────────────────────────────┘  │
>     │                        │                                  │
>     │  INDEX STRATEGY (time-based + aliases)                   │
>     │  ┌────────────────────────────────────────────────────┐  │
>     │  │ logs-write alias ──▶ logs-2026-02-28 (current)     │  │
>     │  │ logs-read alias  ──▶ logs-2026-02-* (all of Feb)   │  │
>     │  │                                                    │  │
>     │  │ Index Template: logs-* → mappings + settings        │  │
>     │  │ Rollover: new index when size>50GB or age>1 day    │  │
>     │  └────────────────────────────────────────────────────┘  │
>     │                                                          │
>     │  TIERED STORAGE (ISM policy drives transitions)         │
>     │  ┌─────────────┐  ┌──────────────┐  ┌──────────────┐   │
>     │  │  HOT (EBS)  │─▶│  WARM        │─▶│  COLD        │   │
>     │  │  0-7 days   │  │  (UltraWarm) │  │  (S3 detached│   │
>     │  │  read+write │  │  7-30 days   │  │  30-365 days │   │
>     │  │  data nodes │  │  read-only   │  │  near-zero   │──▶ DELETE
>     │  │  (r6g/r7g)  │  │  S3-backed   │  │  compute     │   │
>     │  │             │  │  SSD cache   │  │  cost)       │   │
>     │  │             │  │  ~3x cheaper │  │              │   │
>     │  └─────────────┘  └──────────────┘  └──────────────┘   │
>     └──────────────────────────────────────────────────────────┘
>               │
>               ▼
>     ┌──────────────────────────────────────────────────────────┐
>     │               VISUALIZATION LAYER                        │
>     │                                                          │
>     │  ┌────────────────────────────────────────────────────┐  │
>     │  │ OpenSearch Dashboards (Kibana fork)                 │  │
>     │  │                                                    │  │
>     │  │ ┌──────────┐ ┌──────────┐ ┌────────┐ ┌──────────┐│  │
>     │  │ │ Discover │ │Visualize │ │Dash-   │ │ Alerting ││  │
>     │  │ │ (log     │ │(charts,  │ │boards  │ │ (monitors││  │
>     │  │ │ explorer)│ │ graphs)  │ │(saved  │ │  triggers││  │
>     │  │ │          │ │          │ │layouts)│ │  actions) ││  │
>     │  │ └──────────┘ └──────────┘ └────────┘ └──────────┘│  │
>     │  └────────────────────────────────────────────────────┘  │
>     └──────────────────────────────────────────────────────────┘
> ```
>
> **Ingestion Layer** — decouples data sources from the cluster:
> - **Fluent Bit / Fluentd**: Lightweight collectors deployed as DaemonSets in Kubernetes. Collect logs from stdout/files, parse, forward. Fluent Bit uses ~450 KB memory.
> - **Amazon Data Firehose**: Managed delivery stream. Buffers logs (1-15 min or 1-128 MB), handles backpressure and retry. If OpenSearch is slow, Firehose buffers — protects the cluster from being overwhelmed.
> - **OpenSearch Ingestion (OSI)**: AWS-managed pipeline based on Data Prepper. Handles traces, metrics, and logs via OpenTelemetry.
>
> **Ingest Pipelines** — transforms at index time inside OpenSearch:
> - `grok`: Parse unstructured log lines into structured fields using regex patterns
> - `date`: Parse timestamp strings into proper date fields
> - `geoip`: Convert IP addresses to geo-coordinates
> - Alternative to running Logstash as a separate process
>
> **Index Strategy** — time-based indices with aliases:
> - **Write alias** (`logs-write`): Points to the current active index. Applications write to the alias, never hardcode index names.
> - **Read alias** (`logs-read`): Spans all indices matching `logs-*`. Searches transparently query across all time windows.
> - **Rollover**: Automatically create a new index when the current one hits 50 GB or 1 day. Combined with **Index Templates** that auto-apply mappings + settings to new `logs-*` indices.
> - **Why time-based indices?** Deletion is O(1) — drop an index instead of delete-by-query (which would leave tombstones in segments until merge). This is critical at scale.
>
> **ISM (Index State Management)** — automated lifecycle:
> - Hot → Warm (UltraWarm): S3-backed with local SSD cache. Read-only. ~3x cheaper. ISM migrates automatically after 7 days.
> - Warm → Cold: Detach to S3 entirely. Near-zero compute cost. Re-attach to query. After 30 days.
> - Cold → Delete: After 365 days.
> - This is the key cost optimization. Without ISM, 520 TB of hot EBS is prohibitively expensive. With ISM, only the last 7 days (60 TB) are on hot storage.
>
> **OpenSearch Dashboards** — Kibana-fork visualization layer. Queries the cluster via the same REST API. Discover (log explorer), Visualize (charts), Dashboards (saved layouts), Alerting (schedule queries, trigger notifications).
>
> **Contrast with Splunk / Datadog**: Splunk is proprietary with per-GB pricing — expensive at high volume. Datadog is SaaS with per-host pricing. OpenSearch is open-source with the best cost-to-flexibility ratio at scale.
>
> For the full deep dive, see [07-log-analytics-pipeline.md](07-log-analytics-pipeline.md)."

**Interviewer:**
Good evolution. What's still missing for a production deployment?

> "
> | Problem | Impact |
> |---------|--------|
> | **No security** | All users see all data. No multi-tenancy isolation. PII exposed to everyone. |
> | **Lexical search only** | 'memory issue' doesn't find docs saying 'OOM error' or 'heap exhaustion.' No semantic understanding. |
> | **No anomaly detection** | Operators must manually set alert thresholds. Miss novel failure modes. |
> | **No cross-region resilience** | If the AWS region goes down, search is down. No DR strategy. |
> | **No monitoring** | Flying blind — no visibility into JVM heap, GC pauses, search latency, shard health. |"

### Attempt 5: Production Hardening — Security, ML Features, DR, Monitoring

> "
> ```
>                 ┌──────────────────────────────────────┐
>                 │          SECURITY BOUNDARY            │
>                 │    VPC + FGAC + Encryption             │
>                 │                                      │
>                 │  ┌──────────────────────────────┐    │
>                 │  │ IAM / SAML / Cognito         │    │
>                 │  │ (Authentication — WHO)        │    │
>                 │  └───────────┬──────────────────┘    │
>                 │              │                        │
>                 │              ▼                        │
>                 │  ┌──────────────────────────────┐    │
>                 │  │ Fine-Grained Access Control   │    │
>                 │  │ (Authorization — WHAT)        │    │
>                 │  │                              │    │
>                 │  │ Index-level: team-a reads    │    │
>                 │  │   only logs-team-a-*         │    │
>                 │  │ Document-level: filter by    │    │
>                 │  │   tenant_id                  │    │
>                 │  │ Field-level: mask PII from   │    │
>                 │  │   analyst role               │    │
>                 │  └───────────┬──────────────────┘    │
>                 └──────────────┼────────────────────────┘
>                                │
>                                ▼
> ┌────────────────────────────────────────────────────────────────────┐
> │                OPENSEARCH CLUSTER (Primary Region)                 │
> │                                                                    │
> │  ┌────────────────────────────────────────────────────────────┐   │
> │  │  SEARCH + ANALYTICS ENGINE (from Attempts 1-4)             │   │
> │  │  Coord → Data Nodes (shards) → Tiered Storage              │   │
> │  └──────────┬───────────────────────┬─────────────────────────┘   │
> │             │                       │                              │
> │  ┌──────────▼────────────┐  ┌──────▼───────────────────────┐     │
> │  │  VECTOR SEARCH        │  │  ANOMALY DETECTION            │     │
> │  │  (k-NN Plugin)        │  │  (ML Plugin)                  │     │
> │  │                      │  │                              │     │
> │  │  Ingest: text ──▶    │  │  Time-series data ──▶        │     │
> │  │    ML model ──▶      │  │  Random Cut Forest (RCF) ──▶ │     │
> │  │    dense vector      │  │  Detect anomalies without    │     │
> │  │                      │  │  manual thresholds           │     │
> │  │  Query: hybrid       │  │                              │     │
> │  │    BM25 + k-NN       │  │  ──▶ Alerting Plugin ──▶     │     │
> │  │    score fusion      │  │      SNS / Slack / webhook   │     │
> │  │                      │  │                              │     │
> │  │  HNSW graph per      │  └──────────────────────────────┘     │
> │  │  shard (approximate  │                                       │
> │  │  nearest neighbor)   │  ┌──────────────────────────────┐     │
> │  └──────────────────────┘  │  OBSERVABILITY                │     │
> │                            │  (Trace Analytics Plugin)     │     │
> │                            │                              │     │
> │                            │  OTel traces ──▶ service map │     │
> │                            │  + latency breakdown         │     │
> │                            └──────────────────────────────┘     │
> └───────────────┬────────────────────────────────────────────────────┘
>                 │
>                 │  Cross-Cluster Replication (CCR)
>                 │  leader → follower (async)
>                 │
>                 ▼
> ┌────────────────────────────────────────────────────────────────────┐
> │             OPENSEARCH CLUSTER (DR Region / Read-Local)            │
> │                                                                    │
> │  Follower indices (read-only replicas of leader indices)          │
> │  Serves local reads — reduces cross-region latency                │
> │  Promotes to leader if primary region fails                       │
> └────────────────────────────────────────────────────────────────────┘
>
>                 MONITORING (external to cluster)
> ┌────────────────────────────────────────────────────────────────────┐
> │  ┌─────────────┐  ┌──────────────┐  ┌────────────────────────┐   │
> │  │ CloudWatch  │  │ Slow Logs    │  │ Cluster Health API     │   │
> │  │ Metrics     │  │              │  │                        │   │
> │  │             │  │ search>500ms │  │ Key alerts:            │   │
> │  │ CPU, JVM    │  │ index>1000ms │  │ - JVM heap > 85%       │   │
> │  │ heap, disk, │  │              │  │ - Cluster YELLOW/RED   │   │
> │  │ search p99, │  │ → CloudWatch │  │ - Disk watermark >85%  │   │
> │  │ indexing    │  │   Logs       │  │ - Search p99 > SLA     │   │
> │  │ rate, GC    │  │              │  │ - GC pauses > 500ms    │   │
> │  └─────────────┘  └──────────────┘  └────────────────────────┘   │
> └────────────────────────────────────────────────────────────────────┘
>
>     ALTERNATIVE: OPENSEARCH SERVERLESS
> ┌────────────────────────────────────────────────────────────────────┐
> │  No cluster management. Auto-scaling compute.                     │
> │  "Collections" instead of indices.                                │
> │  Separate compute for indexing vs search (decouple write/read).   │
> │  Two types: time-series (logs) | search (app search).            │
> │  Trade-off: higher per-query cost, less control.                  │
> │  Use when: team lacks OpenSearch operational expertise.            │
> └────────────────────────────────────────────────────────────────────┘
> ```
>
> **Security** (wraps the entire cluster):
> - **VPC access**: Cluster deployed within customer's VPC. No public internet exposure.
> - **Authentication**: IAM, SAML (corporate SSO), Amazon Cognito.
> - **Fine-Grained Access Control (FGAC)**: OpenSearch Security plugin. Three levels:
>   - *Index-level*: Team A can only access `logs-team-a-*`
>   - *Document-level*: Filter documents by `tenant_id` — multi-tenant isolation without separate indices
>   - *Field-level*: Mask PII (email, IP) from analyst role
> - **Encryption**: At-rest (AWS KMS), in-transit (TLS), node-to-node encryption.
>
> **Vector Search (k-NN plugin)**:
> - At index time, an ML model converts text to dense vectors (embeddings). Stored alongside the inverted index.
> - At query time, **hybrid scoring** fuses BM25 lexical score + k-NN vector similarity score.
> - Uses HNSW (Hierarchical Navigable Small World) graph per shard for approximate nearest neighbor search. O(log N) query time.
> - Solves the 'memory issue' ≠ 'OOM error' problem: semantic similarity catches meaning, not just keywords.
>
> **Anomaly Detection (ML plugin)**:
> - Random Cut Forest (RCF) algorithm: unsupervised ML. Learns normal patterns from time-series data, flags deviations as anomalies without manual thresholds.
> - Connects to Alerting Plugin → SNS / Slack / webhook. Replaces brittle static-threshold alerts.
>
> **Cross-Cluster Replication (CCR)**:
> - Leader cluster replicates selected indices to follower cluster (async). Follower indices are read-only.
> - DR: if primary region fails, promote follower.
> - Read locality: users in EU query the EU follower instead of crossing the Atlantic.
>
> **Monitoring**:
> - CloudWatch Metrics: CPU, JVM heap %, search latency p50/p99, indexing rate, GC pause duration.
> - Slow Logs: Queries >500ms, indexing ops >1000ms → CloudWatch Logs for debugging.
> - Key alerts: JVM heap >85% (OOM risk), cluster YELLOW/RED, disk watermark >85% (shard allocation blocked at 90%), search p99 > SLA.
>
> **OpenSearch Serverless** (alternative):
> - No cluster management. Auto-scaling compute with OCU (OpenSearch Compute Units).
> - Uses 'collections' instead of indices. Separate compute for indexing and search.
> - Trade-off: higher per-query cost, fewer API features, less tuning control.
>
> For the full deep dives, see [06-aws-managed-service.md](06-aws-managed-service.md) and [09-advanced-features.md](09-advanced-features.md)."

---

### Architecture Evolution Table

| Attempt | Key Addition | Problem Solved | New Problem Introduced |
|---------|-------------|---------------|----------------------|
| 0 | Single server, brute-force scan | None (starting point) | O(N) search, no durability, SPOF |
| 1 | Inverted index + Lucene (segments, translog, BM25) | O(1) search per term, durability via WAL | Single-node storage/compute limit, SPOF |
| 2 | Sharding (hash routing, 2-phase query/fetch) | Horizontal data distribution, parallel query | No redundancy, node death = data loss |
| 3 | Replicas + master election + zone awareness | HA, read scaling, split-brain prevention | All hot storage, no lifecycle, no ingestion pipeline |
| 4 | Ingestion pipeline + ISM + tiered storage + dashboards | Cost optimization, operational visibility, data lifecycle | No security, lexical-only search, no DR, no monitoring |
| 5 | Security (FGAC) + vector search + anomaly detection + CCR + monitoring | Multi-tenant security, semantic search, DR, observability | Operational complexity, tuning expertise required |

---

### L5 vs L6 vs L7 — Phase 4 Rubric

| Aspect | L5 (SDE-2) | L6 (SDE-3) | L7 (Principal) |
|---|---|---|---|
| **Iterative Build** | Jumps to final architecture | Builds incrementally, each step motivated by concrete problem | Additionally quantifies cost/performance impact at each step |
| **Inverted Index** | "We need an index" | Explains term→posting list, segment immutability, NRT refresh, translog | Discusses skip lists in posting lists, BKD trees for numeric ranges, segment merge policies |
| **Sharding** | "Split data across nodes" | Explains hash routing, two-phase query, fixed shard count tradeoff | Discusses custom routing for tenant co-location, search_after vs scroll for deep pagination |
| **Replication** | "Add replicas for redundancy" | Explains write-path replication, zone awareness, cluster health states | Discusses sync vs async replica writes, wait_for_active_shards semantics, replica lag impact |
| **Tiered Storage** | "Move old data to cheaper storage" | Explains ISM policy states, UltraWarm S3-backed architecture | Discusses cost modeling ($/GB/month per tier), rollover sizing tradeoffs, cold tier re-attach latency |

---

## PHASE 5: Deep Dive — Indexing & the Inverted Index (~8 min)

**Interviewer:**
Let's go deep on the write path and the inverted index. Walk me through exactly what happens when a document is indexed — from HTTP request to searchable segment.

**Candidate:**

> "Sure. Let me trace the complete write path for a single document, then discuss the data structures involved.
>
> **Step-by-step write path:**
>
> ```
>  Client: PUT /logs/_doc/1 {"message": "Connection refused to db:5432"}
>     │
>     ▼
>  Coordinating Node
>     │  shard = hash("1") % num_primary_shards → Shard 2
>     ▼
>  Data Node (hosts Shard 2 Primary)
>     │
>     ├──▶ 1. TRANSLOG: append operation to WAL (fsync'd)
>     │         → durability guarantee: if node crashes,
>     │           replay translog to recover
>     │
>     ├──▶ 2. IN-MEMORY BUFFER: add to Lucene's IndexWriter
>     │         → document is in memory but NOT searchable
>     │
>     ├──▶ 3. TEXT ANALYSIS (for each text field):
>     │         "Connection refused to db:5432"
>     │              ↓ standard tokenizer
>     │         ["Connection", "refused", "to", "db", "5432"]
>     │              ↓ lowercase filter
>     │         ["connection", "refused", "to", "db", "5432"]
>     │              ↓ (optional: stop word filter, stemming)
>     │         Tokens added to in-memory inverted index
>     │
>     ├──▶ 4. REPLICATE to replica shards (parallel)
>     │         → replicas perform same steps 1-3
>     │
>     └──▶ 5. ACK to client: 201 Created
>
>  [~1 second later: REFRESH]
>     │
>     └──▶ 6. In-memory buffer flushed to new SEGMENT on disk
>              → segment = immutable mini-index
>              → document is NOW searchable
>              → this is the "near-real-time" window
>
>  [periodically: FLUSH]
>     │
>     └──▶ 7. Translog cleared after segments are safely
>              committed to disk (fsync)
>
>  [background: SEGMENT MERGE]
>     │
>     └──▶ 8. Multiple small segments merged into fewer
>              large segments. Deleted docs purged.
>              Tiered merge policy (default): merge
>              segments of similar size.
> ```
>
> **The inverted index in detail:**
>
> For a field `message` with 4 documents:
> ```
> Doc 1: "Connection refused to db"
> Doc 2: "Connection timeout after 30s"
> Doc 3: "Payment processed successfully"
> Doc 4: "Connection refused by firewall"
>
> INVERTED INDEX:
> Term           → Posting List          (+ positions, offsets, payloads)
> ────────────────────────────────────────────────────────────────────
> "connection"   → [1, 2, 4]             freq: [1, 1, 1]
> "refused"      → [1, 4]               freq: [1, 1]
> "timeout"      → [2]                  freq: [1]
> "payment"      → [3]                  freq: [1]
> "processed"    → [3]                  freq: [1]
> "successfully" → [3]                  freq: [1]
> "db"           → [1]                  freq: [1]
> "firewall"     → [4]                  freq: [1]
> "30s"          → [2]                  freq: [1]
>
> DOC VALUES (columnar store, for sorting/aggregation):
> Doc ID → Field values (stored column-oriented on disk)
> ────────────────────────────────────────────────
> doc 1  → {service: "payments", level: "ERROR", timestamp: 1709...}
> doc 2  → {service: "payments", level: "WARN",  timestamp: 1709...}
> doc 3  → {service: "orders",   level: "INFO",  timestamp: 1709...}
> doc 4  → {service: "network",  level: "ERROR", timestamp: 1709...}
> ```
>
> The inverted index answers: 'which documents contain term X?' (for search)
> Doc values answer: 'what is the value of field Y for document Z?' (for sort/agg)
> Both data structures coexist per field — different structures for different access patterns.
>
> **Segment merge**: Segments accumulate over time (one per refresh cycle). Too many small segments = slow searches (must scan all segments). Background merge compacts them. Tiered merge policy (default): merges segments of similar size to maintain a logarithmic number of segments. Merge is I/O intensive — if unthrottled, it starves search traffic.
>
> **Why immutability matters**: Since segments never change after creation, there's no need for locks on reads. Multiple search threads can read the same segment concurrently without coordination. This is a huge performance win — lock-free reads at the segment level.
>
> For the full deep dive, see [03-indexing-and-inverted-index.md](03-indexing-and-inverted-index.md)."

**Interviewer:**
What about deletes and updates? If segments are immutable, how do you delete a document?

> "Deletes are **soft deletes**. When you delete doc 1, we don't modify the segment containing it. Instead, we write a `.del` bitset file: 'doc 1 is deleted.' Searches skip any doc that's marked deleted. The document physically remains in the segment until a merge — the merge creates a new segment without the deleted docs and drops the old one.
>
> Updates = delete old version + index new version. There's no in-place update because segments are immutable. The `_update` API does a get + merge + reindex internally.
>
> This means deleted documents still consume disk until the next merge. In high-churn indices (frequent updates/deletes), merge pressure increases."

---

### L5 vs L6 vs L7 — Phase 5 Rubric

| Aspect | L5 (SDE-2) | L6 (SDE-3) | L7 (Principal) |
|---|---|---|---|
| **Write Path** | "Document goes to index" | Traces full path: translog → buffer → refresh → segment, with timing | Discusses translog fsync policies (request vs async), durability vs throughput tradeoff |
| **Inverted Index** | "Maps words to documents" | Explains posting lists, term frequency, doc values as separate structure | Discusses skip lists for posting list intersection, FST for term dictionary, compression |
| **NRT Mechanics** | "Documents are indexed quickly" | Explains refresh cycle, ~1s gap, buffer → segment flush | Discusses configuring refresh_interval for bulk loads, impact on segment count |
| **Segment Merge** | Not mentioned | Explains merge purpose, tiered policy, I/O impact | Discusses merge throttling, force merge for read-only indices, max_num_segments |
| **Deletes** | "Documents are removed" | Explains soft deletes, .del bitset, physical removal on merge | Discusses impact on disk usage, merge pressure from high-churn workloads |

---

## PHASE 6: Deep Dive — Search Execution & Relevance (~8 min)

**Interviewer:**
Now let's go deep on the read path. When a user searches for "connection refused" across 5 shards with 20 segments each, what exactly happens?

**Candidate:**

> "Let me trace the full search execution across the distributed system.
>
> **Two-phase distributed search in detail:**
>
> ```
>  Client: POST /logs-*/_search {"query": {"match": {"message": "connection refused"}}}
>     │
>     ▼
>  Coordinating Node
>     │
>     ├──▶ 1. RESOLVE INDICES: logs-* matches logs-2026-02-28,
>     │       logs-2026-02-27, etc. (via alias or wildcard)
>     │
>     ├──▶ 2. QUERY PHASE (scatter):
>     │       Send query to one copy (primary OR replica)
>     │       of each shard — 5 shards = 5 parallel requests
>     │
>     │    On each shard (5 shards × 20 segments = 100 segments total):
>     │    ┌─────────────────────────────────────────────────┐
>     │    │  For EACH of 20 segments:                       │
>     │    │    a. Look up "connection" in term dictionary   │
>     │    │       → get posting list [doc1, doc2, doc4]     │
>     │    │    b. Look up "refused" in term dictionary      │
>     │    │       → get posting list [doc1, doc4]           │
>     │    │    c. Intersect posting lists → [doc1, doc4]    │
>     │    │    d. Score each match with BM25:               │
>     │    │       score = Σ IDF(t) × (tf × (k1+1))         │
>     │    │              / (tf + k1 × (1-b+b×dl/avgdl))    │
>     │    │    e. Check .del bitset — skip deleted docs     │
>     │    │    f. Maintain per-segment priority queue (top-N)│
>     │    │                                                 │
>     │    │  Merge all segment results into shard-level     │
>     │    │  top-N priority queue                           │
>     │    │                                                 │
>     │    │  Return: [(doc_id, score), ...] for top-N       │
>     │    │  (just IDs + scores, NOT full documents)        │
>     │    └─────────────────────────────────────────────────┘
>     │
>     ├──▶ 3. MERGE: Coordinating node merges 5 sorted lists
>     │       into global top-N using a priority queue.
>     │       At this point we know the final top-20 doc IDs.
>     │
>     └──▶ 4. FETCH PHASE:
>            Send multi-get for just those 20 doc IDs to
>            the shards that hold them. Shards return full
>            _source documents. Coordinating node assembles
>            final response.
> ```
>
> **BM25 scoring — why it's better than TF-IDF:**
>
> Classic TF-IDF: score grows linearly with term frequency. A document mentioning 'connection' 100 times scores 10x higher than one mentioning it 10 times. That's usually not what you want — a document is relevant because it mentions the term, not because it repeats it excessively.
>
> BM25 adds **term frequency saturation**: after a few occurrences, additional matches have diminishing returns. The `k1` parameter (default 1.2) controls how quickly saturation kicks in. And `b` (default 0.75) controls document length normalization — long documents are slightly penalized because they're more likely to contain a term by chance.
>
> **Filters vs queries — a critical performance distinction:**
>
> In the `bool` query, `must` clauses are **scored** (compute BM25, affect ranking) and `filter` clauses are **unscored** (binary yes/no, skip scoring). Filters have two performance advantages:
> 1. Skip the scoring computation entirely
> 2. Filter results are **cached at the segment level**. Since segments are immutable, a cached filter result never becomes stale — it's valid until the segment is merged away. If 100 queries all filter on `service=payments`, the first query computes the bitset, and subsequent queries reuse it instantly.
>
> This is why `{ term: { service: 'payments' } }` should go in `filter`, not `must` — you don't want to score on an exact match, and you get segment-level caching for free.
>
> **Aggregation execution:**
>
> Aggregations run in parallel with the query. On each shard, after identifying matching documents, the aggregation framework iterates over matches and builds partial aggregation results (e.g., partial histograms). The coordinating node merges partial aggregations into the final result. For a `date_histogram`, each shard returns its bucket counts, and the coordinating node sums them.
>
> Aggregations use **doc values** (the columnar store) — not the inverted index. This is important: reading timestamp values for all matching documents from a column-oriented structure is much faster than from a row-oriented one.
>
> For the full deep dive, see [04-search-and-relevance.md](04-search-and-relevance.md)."

---

### L5 vs L6 vs L7 — Phase 6 Rubric

| Aspect | L5 (SDE-2) | L6 (SDE-3) | L7 (Principal) |
|---|---|---|---|
| **Query Execution** | "Search runs on all shards" | Traces full 2-phase flow: scatter → per-shard per-segment execution → merge → fetch | Discusses adaptive replica selection, shard-level concurrency control, coordinating node memory pressure |
| **BM25** | "Results are ranked" | Explains TF saturation, IDF, length normalization, k1/b parameters | Discusses when to customize k1/b, function_score for business boosting, explain API for debugging |
| **Filter Caching** | Not mentioned | Explains filter vs query context, segment-level caching benefit | Discusses cache eviction policies, cache warming, when filter cache hurts (high cardinality filters) |
| **Aggregations** | "Can compute counts" | Explains partial aggregation per shard → merge, doc values usage | Discusses cardinality approximation (HyperLogLog), composite aggregations for pagination, memory impact |

---

## PHASE 7: Deep Dive — Sharding, Capacity & AWS Managed Service (~8 min)

**Interviewer:**
Let's talk about shard sizing — the most common operational mistake. How do you decide on shard count, and what happens when you get it wrong?

**Candidate:**

> "Shard sizing is the single most impactful capacity planning decision in OpenSearch, and getting it wrong is painful because shard count is immutable after index creation.
>
> **Shard sizing guidelines:**
>
> | Factor | Recommendation | Why |
> |--------|---------------|-----|
> | **Shard size** | 10-50 GB per shard | Too small: overhead per shard (memory for metadata, segment info). Too large: slow recovery, slow merges, unbalanced cluster. |
> | **Max shards per node** | ~25 shards per GB of JVM heap | Each shard consumes ~1 MB of heap for metadata. 30 GB heap × 25 = ~750 shards max per node. Exceeding this causes master instability. |
> | **Shard count** | total_data / target_shard_size | For 260 TB with 50 GB/shard → 5,200 primary shards. With 1 replica → 10,400 total shards. |
>
> **What happens when you get it wrong:**
>
> **Too many small shards (shard explosion)**:
> - Common in log analytics with daily indices. If each day has 5 primary shards, after a year you have 1,825 primary shards. With 2 replicas = 5,475 shards. Each shard uses master heap for cluster state tracking. Master becomes slow, unstable, potentially OOM.
> - Fix: use rollover (size-based, not time-based) to control shard count. Or use ISM to delete/merge old indices.
>
> **Too few large shards**:
> - If you have 3 shards for 500 GB of data, each shard is ~167 GB. Recovery after node failure requires copying 167 GB per shard — takes forever. Merge operations on 167 GB segments consume huge I/O. Searches can't parallelize well (only 3 shards to scatter across).
>
> **JVM heap sizing — the 32 GB rule:**
>
> Set JVM heap to 50% of available RAM, maximum 32 GB. Why the ceiling? The JVM uses **compressed ordinary object pointers** (compressed oops) for heaps ≤ 32 GB. Beyond 32 GB, pointers double in size from 4 bytes to 8 bytes — you effectively lose memory. A 31 GB heap is often more usable than a 40 GB heap.
>
> The remaining 50% of RAM is for the OS filesystem cache. Lucene segments are memory-mapped files — the OS cache keeps hot segments in memory, making searches fast. Starving the OS cache (by allocating too much heap) makes searches slow even if JVM GC is healthy.
>
> **Disk watermarks:**
> - **Low (85%)**: Stop allocating new shards to this node. Existing shards stay.
> - **High (90%)**: Start relocating shards away from this node to others.
> - **Flood stage (95%)**: Set all indices on this node to read-only. Prevent any more writes. This is emergency protection.
>
> **AWS managed service architecture:**
>
> ```
>  ┌───────────────────────────────────────────────────────┐
>  │  AWS CONTROL PLANE                                    │
>  │                                                       │
>  │  ┌──────────────┐  ┌──────────────┐  ┌────────────┐ │
>  │  │ Domain       │  │ Blue-Green   │  │ Automated  │ │
>  │  │ Provisioning │  │ Deployments  │  │ Snapshots  │ │
>  │  │ (CloudForm.) │  │ (upgrades,   │  │ (hourly to │ │
>  │  │              │  │  config      │  │  S3)       │ │
>  │  │              │  │  changes)    │  │            │ │
>  │  └──────────────┘  └──────────────┘  └────────────┘ │
>  └─────────────────────────┬─────────────────────────────┘
>                            │ manages
>                            ▼
>  ┌───────────────────────────────────────────────────────┐
>  │  CUSTOMER DATA PLANE (within customer's VPC)          │
>  │                                                       │
>  │  ┌──────────┐  ┌──────────┐  ┌──────────┐           │
>  │  │ Master   │  │ Master   │  │ Master   │ AZ-1/2/3  │
>  │  │ Node     │  │ Node     │  │ Node     │           │
>  │  └──────────┘  └──────────┘  └──────────┘           │
>  │                                                       │
>  │  ┌──────────┐  ┌──────────┐  ┌──────────┐           │
>  │  │ Data     │  │ Data     │  │ Data     │ HOT       │
>  │  │ Node     │  │ Node     │  │ Node     │ (EBS gp3) │
>  │  └──────────┘  └──────────┘  └──────────┘           │
>  │                                                       │
>  │  ┌──────────┐  ┌──────────┐                          │
>  │  │UltraWarm │  │UltraWarm │  WARM (S3-backed,       │
>  │  │ Node     │  │ Node     │   local SSD cache)       │
>  │  └──────────┘  └──────────┘                          │
>  │                                                       │
>  │  Cold storage: indices detached to S3                 │
>  └───────────────────────────────────────────────────────┘
> ```
>
> Key managed service features:
> - **Blue-green deployments**: For config changes and upgrades, AWS creates new nodes, migrates shards, validates health, then swaps. Minimizes downtime but temporarily doubles resource consumption.
> - **Automated hourly snapshots**: Incremental snapshots to S3. Only changed segments are stored.
> - **Multi-AZ**: 2-AZ or 3-AZ deployments with zone awareness. 3-AZ survives any single AZ failure.
>
> **Trade-offs vs self-managed**: AWS-managed means no custom plugins (AWS controls the plugin set), limited cluster settings, version upgrades controlled by AWS. But you get automated backups, monitoring, patching, and blue-green upgrades. For most teams, managed is the right choice.
>
> For the full deep dive, see [05-sharding-and-distribution.md](05-sharding-and-distribution.md) and [06-aws-managed-service.md](06-aws-managed-service.md)."

---

### L5 vs L6 vs L7 — Phase 7 Rubric

| Aspect | L5 (SDE-2) | L6 (SDE-3) | L7 (Principal) |
|---|---|---|---|
| **Shard Sizing** | "Use multiple shards" | Knows 10-50 GB guideline, explains too-many vs too-few shards failure modes | Calculates shards-per-node heap budget, discusses hot/warm sizing independently |
| **JVM Heap** | "Set heap to half RAM" | Explains 32 GB compressed oops limit, OS cache importance | Discusses G1GC tuning, heap pressure from aggregations, circuit breakers |
| **Managed Service** | "AWS manages the cluster" | Explains blue-green deployments, multi-AZ, automated snapshots | Discusses control plane vs data plane separation, managed service limitations, when to self-manage |
| **Disk Watermarks** | Not mentioned | Knows the three thresholds (85/90/95%) and their effects | Discusses watermark-triggered shard rebalancing cascade, preemptive monitoring |

---

## PHASE 8: Deep Dive — Performance Tuning & Advanced Features (~5 min)

**Interviewer:**
You're running this in production. What are the top performance issues you'd watch for and how would you tune them?

**Candidate:**

> "Let me cover the most common performance problems in priority order.
>
> **1. Search latency degradation:**
>
> Root causes and fixes:
> - **Too many shards per query**: If `logs-*` matches 365 indices × 5 shards = 1,825 shards to scatter to. Fix: use ISM to delete/cold-tier old indices so fewer shards participate.
> - **Deep pagination**: `from: 10000, size: 20` requires each shard to return 10,020 results to the coordinating node. Fix: use `search_after` (cursor-based pagination) instead.
> - **Expensive aggregations**: High-cardinality terms aggregations (unique user IDs) consume massive heap. Fix: use composite aggregation with pagination, or approximate with cardinality (HyperLogLog).
> - **Not using filter context**: Putting exact-match clauses in `must` instead of `filter` skips segment-level caching. Fix: always put non-scored conditions in `filter`.
>
> **2. Indexing throughput bottleneck:**
>
> Tuning for bulk indexing:
> - Batch size: 5-15 MB per `_bulk` request (not too small = per-request overhead, not too large = memory pressure)
> - Increase `index.refresh_interval` from 1s to 30s or disable (-1) during bulk loads. Each refresh creates a new segment — fewer refreshes = fewer small segments = less merge pressure.
> - Use auto-generated `_id` instead of client-specified IDs. Client IDs require a version check (does this ID already exist?) — an extra I/O per document.
> - Increase `index.translog.flush_threshold_size` to delay flush and batch more data.
> - Multiple bulk indexing threads (3-5 per data node) to saturate I/O.
>
> **3. JVM heap pressure / GC pauses:**
>
> Symptoms: search latency spikes correlated with GC pause events. Root causes:
> - Heap too large (>32 GB, lost compressed oops)
> - Field data cache loaded for text field aggregations (avoid — use doc values on keyword fields)
> - Too many shards (each consumes heap for metadata)
> - Large aggregation results held in memory
>
> **4. Advanced features for production:**
>
> **Vector search (k-NN)** — for semantic search:
> - Store dense vectors alongside documents. At query time, combine BM25 lexical scores with k-NN vector similarity using hybrid search.
> - HNSW (Hierarchical Navigable Small World) graph: builds a navigable graph per shard. O(log N) approximate nearest neighbor lookup. Parameters: `ef_construction` (build-time quality, default 512), `m` (connections per node, default 16).
> - Distance functions: L2 (Euclidean), cosine similarity, inner product.
>
> **Anomaly detection (RCF)**:
> - Random Cut Forest: unsupervised algorithm that models time-series data and assigns an anomaly grade to each observation.
> - No manual threshold needed — the algorithm learns what's 'normal' from the data.
> - Integrates with Alerting plugin: when anomaly grade exceeds threshold → trigger notification.
>
> **PPL (Piped Processing Language)**:
> - OpenSearch-specific query language: `search source=logs | where status=500 | stats count() by service | sort -count()`
> - More intuitive than Query DSL for log exploration. Similar to Splunk SPL.
>
> For the full deep dives, see [08-performance-and-scaling.md](08-performance-and-scaling.md) and [09-advanced-features.md](09-advanced-features.md)."

---

### L5 vs L6 vs L7 — Phase 8 Rubric

| Aspect | L5 (SDE-2) | L6 (SDE-3) | L7 (Principal) |
|---|---|---|---|
| **Search Tuning** | "Add more nodes" | Identifies specific causes (shard count, deep pagination, filter vs query) with fixes | Discusses adaptive replica selection, search thread pool sizing, coordinating-node memory budgeting |
| **Indexing Tuning** | "Use bulk API" | Knows optimal batch size, refresh interval tuning, auto-generated IDs | Discusses translog fsync policy tradeoffs, indexing buffer size, merge scheduler tuning |
| **JVM/GC** | "Use more memory" | Knows 32 GB limit, compressed oops, OS cache importance | Discusses G1GC region sizing, circuit breakers (field data, request, in-flight), heap dumps analysis |
| **k-NN** | Not mentioned | Knows HNSW, distance functions, hybrid search concept | Discusses ef_search vs recall tradeoff, memory impact of HNSW graphs, quantization for memory reduction |

---

## PHASE 9: Wrap-Up (~3 min)

**Interviewer:**
Good discussion. Final question: you're the oncall engineer for a fleet of OpenSearch clusters serving log analytics for a large organization. What keeps you up at night?

**Candidate:**

> "Three things:
>
> **1. Shard explosion leading to master instability**
>
> This is the silent killer. Teams create daily indices with 5 shards each. After a year across 10 teams, you have 18,250 primary shards. With 1 replica = 36,500 shards. The master node spends more and more time managing cluster state. Eventually, master GC pauses cause shard reassignment storms — the cluster oscillates between Yellow and Green. The fix is prevention: enforce ISM policies that delete or cold-tier old indices, use rollover-based indexing (size-based, not time-based), and monitor shard count as a first-class metric. But by the time you notice, you're already in trouble.
>
> **2. Mapping explosion from uncontrolled dynamic mapping**
>
> If someone indexes a document with 10,000 unique field names (common with unstructured JSON logs), dynamic mapping creates 10,000 field mappings. Each mapping consumes cluster state memory. Across many indices, this balloons cluster state to gigabytes. Master nodes become slow to publish cluster state updates. Fix: enforce strict mappings via index templates, set `index.mapping.total_fields.limit` (default 1,000), and reject documents that don't match the schema.
>
> **3. A cascade failure from a single expensive query**
>
> One bad query — like a wildcard leading `*` search (`*timeout*`) or a high-cardinality terms aggregation on a text field — can consume the entire heap of every data node it hits. This triggers long GC pauses, which causes the master to think nodes are dead, which triggers shard reassignment, which generates I/O load, which makes everything worse. The cluster enters a death spiral. Fix: enable slow query logging, set `search.max_buckets` limit, use circuit breakers (`indices.breaker.total.limit`), and consider separating search and indexing workloads onto different node groups."

**Interviewer:**
Those are exactly the three operational nightmares I've seen in production. Good instincts. Thanks for the discussion — you demonstrated strong understanding of both the internal data structures and the operational concerns.

---

### L5 vs L6 vs L7 — Phase 9 Rubric

| Aspect | L5 (SDE-2) | L6 (SDE-3) | L7 (Principal) |
|---|---|---|---|
| **Operational Risks** | Generic: "cluster might go down" | Specific scenarios: shard explosion, mapping explosion, query cascade | Additionally discusses multi-tenant resource isolation, cross-cluster replication lag monitoring, upgrade rollback strategies |
| **Mitigation** | "Monitor and alert" | Specific preventive controls: ISM policies, mapping limits, circuit breakers | Discusses organizational governance: index template approval process, cost chargeback per team, capacity planning models |
| **Depth** | Surface-level | Explains the cascade mechanism (how one problem causes another) | Proposes architectural solutions: separate ingest and query clusters, read-only replicas for heavy analytics |

---

## Final Architecture Summary

```
                          DATA SOURCES
    ┌──────────┐  ┌──────────┐  ┌──────────────┐  ┌───────────┐
    │App Logs  │  │CloudWatch│  │VPC Flow Logs │  │OTel       │
    │(stdout)  │  │Logs      │  │              │  │Traces     │
    └────┬─────┘  └────┬─────┘  └──────┬───────┘  └─────┬─────┘
         │             │               │                 │
         ▼             ▼               ▼                 ▼
    ┌──────────────────────────────────────────────────────────┐
    │               INGESTION LAYER                            │
    │  Fluent Bit │ Amazon Data Firehose │ OpenSearch Ingestion │
    └───────────────────────┬──────────────────────────────────┘
                            │
                ┌───────────┴───────────┐
                │    SECURITY BOUNDARY   │
                │  VPC + FGAC + KMS      │
                └───────────┬───────────┘
                            │
    ┌───────────────────────▼──────────────────────────────────┐
    │               OPENSEARCH CLUSTER                          │
    │                                                          │
    │  ┌────────────────────────────────────────────────────┐  │
    │  │ Ingest Pipeline: grok → date → geoip → enrich      │  │
    │  └────────────────────────┬───────────────────────────┘  │
    │                           │                              │
    │  ┌──────────┐  ┌──────────┐  ┌──────────┐              │
    │  │ Master×3 │  │ Coord    │  │ Coord    │              │
    │  │ (cluster │  │ Node     │  │ Node     │              │
    │  │  state)  │  │ (route,  │  │          │              │
    │  └──────────┘  │  merge)  │  └──────────┘              │
    │                └─────┬────┘                              │
    │           ┌──────────┼──────────┐                        │
    │           ▼          ▼          ▼                        │
    │  AZ-1          AZ-2          AZ-3                       │
    │  ┌────────┐  ┌────────┐  ┌────────┐                    │
    │  │Data    │  │Data    │  │Data    │  HOT (EBS gp3)     │
    │  │Nodes   │  │Nodes   │  │Nodes   │  0-7 days          │
    │  │P+R     │  │P+R     │  │P+R     │                    │
    │  └────────┘  └────────┘  └────────┘                    │
    │                                                          │
    │  ┌───────────────────────────────┐                      │
    │  │ UltraWarm Nodes (S3+SSD)     │  WARM: 7-30 days     │
    │  └───────────────────────────────┘                      │
    │                                                          │
    │  ┌───────────────────────────────┐                      │
    │  │ Cold Storage (S3 detached)    │  COLD: 30-365 days   │
    │  └───────────────────────────────┘                      │
    │                                                          │
    │  PLUGINS:                                                │
    │  ┌──────────┐ ┌──────────┐ ┌──────────┐ ┌───────────┐  │
    │  │ k-NN     │ │ Anomaly  │ │ Alerting │ │ Trace     │  │
    │  │ (vector  │ │ Detect.  │ │ (monitors│ │ Analytics │  │
    │  │  search) │ │ (RCF)    │ │  SNS/    │ │ (OTel)    │  │
    │  │          │ │          │ │  Slack)  │ │           │  │
    │  └──────────┘ └──────────┘ └──────────┘ └───────────┘  │
    └──────────────────┬───────────────────────────────────────┘
                       │
                       │ CCR (async replication)
                       ▼
    ┌──────────────────────────────────────────────────────────┐
    │  DR CLUSTER (follower, read-only, separate region)       │
    └──────────────────────────────────────────────────────────┘

    MONITORING: CloudWatch Metrics + Slow Logs + Cluster Health API

    ┌──────────────────────────────────────────────────────────┐
    │  OpenSearch Dashboards                                    │
    │  Discover │ Visualize │ Dashboards │ Alerting │ PPL      │
    └──────────────────────────────────────────────────────────┘
```

---

## Supporting Deep-Dive Documents

| # | Document | Topic |
|---|----------|-------|
| 1 | [01-interview-simulation.md](01-interview-simulation.md) | This file — the main interview dialogue |
| 2 | [02-api-contracts.md](02-api-contracts.md) | Comprehensive OpenSearch API reference |
| 3 | [03-indexing-and-inverted-index.md](03-indexing-and-inverted-index.md) | Write path, inverted index, Lucene segments |
| 4 | [04-search-and-relevance.md](04-search-and-relevance.md) | Query execution, BM25, aggregations, caching |
| 5 | [05-sharding-and-distribution.md](05-sharding-and-distribution.md) | Sharding, replication, cluster topology |
| 6 | [06-aws-managed-service.md](06-aws-managed-service.md) | AWS OpenSearch Service architecture |
| 7 | [07-log-analytics-pipeline.md](07-log-analytics-pipeline.md) | End-to-end log analytics pipeline |
| 8 | [08-performance-and-scaling.md](08-performance-and-scaling.md) | Performance tuning & capacity planning |
| 9 | [09-advanced-features.md](09-advanced-features.md) | k-NN, anomaly detection, CCR, PPL, SQL |
| 10 | [10-design-trade-offs.md](10-design-trade-offs.md) | Design philosophy & trade-off analysis |

---

## Key Technical Facts Reference

| Fact | Value | Source |
|------|-------|--------|
| Default refresh interval | 1 second | OpenSearch docs — index settings |
| BM25 default k1 | 1.2 | Lucene BM25Similarity |
| BM25 default b | 0.75 | Lucene BM25Similarity |
| JVM heap max (compressed oops) | 32 GB | JVM specification — CompressedOops |
| Disk watermark — low | 85% | OpenSearch cluster settings |
| Disk watermark — high | 90% | OpenSearch cluster settings |
| Disk watermark — flood stage | 95% | OpenSearch cluster settings |
| Recommended shard size | 10-50 GB | OpenSearch/Elasticsearch best practices |
| Fork from Elasticsearch | 7.10.2 (2021) | Apache 2.0 vs SSPL license change |
| HNSW ef_construction default | 512 | OpenSearch k-NN plugin docs |
| HNSW m default | 16 | OpenSearch k-NN plugin docs |
