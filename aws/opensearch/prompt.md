Design AWS OpenSearch (Managed Search & Analytics Service) as a system design interview simulation.

## Template
Follow the EXACT same format as the S3 interview simulation at:
src/hld/aws/s3/design/interview-simulation.md

Same structure:
- Interviewer/Candidate dialogue format
- PHASE 1: Opening & Problem Statement
- PHASE 2: Requirements Gathering & Scoping (functional + non-functional + scale numbers)
- PHASE 3: API Design
- PHASE 4: High-Level Architecture (iterative build-up: Attempt 0 → 1 → 2 → ..., starting from single server, finding problems, evolving)
- PHASE 5+: Deep dives on each component
- L5/L6/L7 rubric table after EACH deep dive phase — showing exactly how an L5, L6, and L7 candidate would answer differently. This is critical for understanding what "good" looks like at each level.
- Architecture evolution table after each phase
- Wrap-up phase with "what keeps you up at night"
- Links to supporting deep-dive docs

## Candidate Level
**L6 SDE-3 (Senior SDE)** candidate being interviewed by a Principal Engineer (L8).

The candidate's answers should demonstrate L6-level depth:
- Not just "what" but "why" and "why not the alternative"
- Quantitative reasoning (back-of-envelope math, concrete numbers)
- Awareness of operational concerns (monitoring, failure modes, blast radius)
- Proactive identification of tradeoffs before the interviewer asks

The L5/L6/L7 rubric tables after each phase should make it crystal clear how an L5 answer differs from L6 differs from L7.

## Output location
Create all files under: src/hld/aws/opensearch/design/

## Files to create

### 1. 01-interview-simulation.md — the main backbone
The full interview dialogue covering all phases and attempts.

### 2. 02-api-contracts.md — OpenSearch APIs

This doc should list all the major API surfaces of OpenSearch. The interview simulation (Phase 3) will only cover a subset — this doc is the comprehensive reference.

**Structure**: For each API group, list every endpoint with HTTP method, path, request/response shape, and a brief description. Mark endpoints covered in the interview with a star or highlight.

**API groups to cover**:

- **Document APIs**: The core CRUD operations. `PUT /{index}/_doc/{id}` (index a document), `GET /{index}/_doc/{id}` (get by ID), `POST /{index}/_update/{id}` (partial update), `DELETE /{index}/_doc/{id}`, `POST /_bulk` (bulk indexing — the performance-critical path). Include near-real-time visibility semantics (document is searchable after refresh, not immediately after indexing).

- **Search APIs**: The most complex and powerful surface. `POST /{index}/_search` (full-text search with Query DSL), `POST /_msearch` (multi-search — batch multiple searches), `GET /{index}/_count`, `POST /{index}/_search/scroll` (deep pagination via scroll API), `POST /_search/point_in_time` (PIT for consistent pagination). Cover query types: match, term, bool, range, nested, aggregations, highlights, suggesters. Explain the two-phase fetch (query phase → fetch phase).

- **Index Management APIs**: `PUT /{index}` (create index with mappings and settings), `DELETE /{index}`, `POST /{index}/_open`, `POST /{index}/_close`, `GET /{index}/_mapping`, `PUT /{index}/_mapping` (update mapping — additive only, can't change existing field types), `GET /{index}/_settings`, `PUT /{index}/_settings` (dynamic settings like replica count). Explain the distinction between static settings (shard count — set at creation, immutable) and dynamic settings (replica count — changeable anytime).

- **Cluster Management APIs**: `GET /_cluster/health` (green/yellow/red status), `GET /_cluster/state`, `GET /_cluster/stats`, `GET /_nodes/stats` (per-node metrics: JVM heap, disk, CPU, search/indexing rates), `POST /_cluster/reroute` (manual shard allocation). These are the operational APIs — essential for monitoring and troubleshooting.

- **Index Lifecycle Management (ILM/ISM)**: `PUT /_plugins/_ism/policies/{policy}` (OpenSearch-specific ISM policies), define phases: hot → warm → cold → delete. Automate index rollover based on size/age/doc count. Critical for log analytics use cases where data has a TTL.

- **Alias APIs**: `POST /_aliases` (atomic alias swaps — zero-downtime reindexing), `GET /_alias/{name}`. Aliases decouple application code from physical index names. Essential for blue-green index deployments.

- **Snapshot & Restore APIs**: `PUT /_snapshot/{repo}` (register snapshot repository — S3 backend), `PUT /_snapshot/{repo}/{snapshot}` (create snapshot), `POST /_snapshot/{repo}/{snapshot}/_restore`. Snapshots are incremental (only changed segments are stored). Critical for backup and disaster recovery.

- **Ingest Pipeline APIs**: `PUT /_ingest/pipeline/{id}` (define a pipeline of processors: grok, date, rename, script, etc.), `POST /{index}/_doc?pipeline={id}` (index with pipeline). Ingest pipelines transform documents at index time — alternative to Logstash for lightweight transformations.

- **Security APIs** (OpenSearch Security plugin): `PUT /_plugins/_security/api/roles/{role}`, `PUT /_plugins/_security/api/rolesmapping/{role}`, `PUT /_plugins/_security/api/internalusers/{user}`. Fine-grained access control: index-level, document-level, and field-level security.

**Contrast with Elasticsearch**: OpenSearch forked from Elasticsearch 7.10.2 (2021, after Elastic's license change from Apache 2.0 to SSPL). APIs are largely compatible with Elasticsearch 7.x but diverge in plugin namespaces (`_plugins/_ism` vs `_ilm`, `_plugins/_security` vs `_xpack/security`). AWS manages OpenSearch as a service — some APIs are restricted (no direct cluster settings modification, no plugin installation — AWS controls the plugin set).

**Interview subset**: In the interview (Phase 3), focus on: document indexing (bulk API — the write path), search (Query DSL + two-phase query/fetch), index management (sharding decisions), and cluster health (monitoring). The full API list lives in this doc.

### 3. 03-indexing-and-inverted-index.md — Write Path & Inverted Index

The inverted index is the core data structure that makes full-text search fast. This doc should cover:

- **Inverted index fundamentals**: A mapping from terms → list of documents containing that term (posting list). Analogous to the index at the back of a textbook. Enables O(1) term lookup instead of scanning every document.
- **Text analysis pipeline**: Character filters → tokenizer → token filters. Example: "The Quick Brown FOX!" → lowercase → ["the", "quick", "brown", "fox"] → stop words removal → ["quick", "brown", "fox"]. Each analyzer choice affects recall and precision.
- **Analyzers**: Standard (default — lowercase + stop words), simple (non-letter splits), whitespace, keyword (no tokenization), language-specific (stemming: "running" → "run"), custom analyzers. Wrong analyzer choice is the #1 cause of "search doesn't find what I expect" bugs.
- **Lucene segments**: Documents are indexed into immutable Lucene segments. Each segment is a self-contained inverted index. New documents go to an in-memory buffer → flushed to a new segment. Segments are periodically merged (segment merge). Immutability enables lock-free concurrent reads.
- **Near-real-time (NRT) search**: Document is indexed → sits in in-memory buffer → NOT searchable yet → `refresh` (default every 1 second) creates a new segment from the buffer → document becomes searchable. This is the "near-real-time" window. Configurable via `index.refresh_interval`.
- **Write path flow**: Client → coordinating node → route to primary shard (based on `_routing` or hash of `_id`) → write to translog (WAL for durability) + in-memory buffer → replicate to replica shards → acknowledge to client. Translog is fsync'd (configurable: every request or async every 5 seconds).
- **Bulk indexing performance**: `_bulk` API is critical for throughput. Batching reduces per-request overhead. Optimal batch size: 5-15 MB per request. Tuning: increase `refresh_interval` during bulk loads (e.g., 30s or -1 to disable), increase `index.translog.flush_threshold_size`, use multiple threads.
- **Segment merge**: Background process that merges small segments into larger ones. Reduces segment count → faster searches (fewer segments to scan). Merge policy: tiered merge (default) — merges segments of similar size. Merge is I/O intensive — can starve search if unthrottled. `index.merge.scheduler.max_thread_count` controls parallelism.
- **Doc values and column stores**: For sorting, aggregations, and scripting, OpenSearch uses doc values — a column-oriented data structure stored on disk. Inverted index answers "which documents contain term X?" Doc values answer "what is the value of field Y in document Z?" Both structures coexist per field.
- **Contrast with traditional databases**: RDBMS uses B-tree indexes (good for exact match, range queries). Inverted index is optimized for full-text search (tokenized terms, relevance scoring). This is why you use OpenSearch for search and a database for transactions — different data structures for different access patterns.

### 4. 04-search-and-relevance.md — Query Execution & Relevance Scoring

How search actually works, from query to ranked results.

- **Two-phase search (query then fetch)**:
  - **Query phase**: Coordinating node sends query to ALL shards (primary or replica). Each shard executes the query against its local inverted index, returns top-N document IDs + scores. Coordinating node merges results (global top-N from shard-local top-Ns).
  - **Fetch phase**: Coordinating node sends a multi-get for the top-N document IDs to the relevant shards. Shards return full `_source` documents. Coordinating node assembles final response.
  - Why two phases? Query phase is lightweight (returns only IDs + scores, not full documents). Fetch phase is heavier but only for top-N documents. This avoids transferring full documents from every shard during the query phase.
- **Relevance scoring**: TF-IDF (classic) vs BM25 (default since Elasticsearch 5.x / OpenSearch). BM25 adds term frequency saturation (diminishing returns for repeated terms) and document length normalization. BM25 parameters: k1 (term frequency saturation, default 1.2) and b (length normalization, default 0.75).
- **Query types**:
  - **match**: Full-text search with analysis. "quick brown fox" → analyzed → OR query on ["quick", "brown", "fox"].
  - **term**: Exact match, no analysis. Used for keyword fields (status codes, IDs).
  - **bool**: Combines queries with must/should/must_not/filter. `filter` context skips scoring → faster.
  - **range**: Numeric/date range queries. Uses BKD trees (k-d trees) for efficient range lookups.
  - **nested**: Queries on nested objects (preserves object boundaries). Requires nested mapping type.
  - **function_score**: Custom scoring (boost by recency, popularity, geo-distance).
- **Aggregations**: The analytics engine within OpenSearch.
  - **Bucket aggregations**: group documents (terms, date_histogram, range, filters).
  - **Metric aggregations**: compute stats (avg, sum, min, max, cardinality, percentiles).
  - **Pipeline aggregations**: operate on other aggregation results (moving_avg, derivative, cumulative_sum).
  - Aggregations run alongside search — same query can return both search results and analytics. This dual capability (search + analytics) is why OpenSearch replaces both search engines and analytics databases in many architectures.
- **Caching**: Query cache (caches filter results per segment — invalidated on segment merge), request cache (caches full search responses for identical requests), field data cache (in-memory for text field aggregations — avoid, use doc values instead).
- **Contrast with Solr**: Both built on Lucene. OpenSearch/Elasticsearch won adoption due to simpler REST API, better distributed architecture (built-in sharding/replication vs SolrCloud bolt-on), and richer aggregation framework. Solr has stronger XML/faceting heritage.

### 5. 05-sharding-and-distribution.md — Sharding, Replication & Cluster Topology

How data is distributed across nodes and how the cluster maintains availability.

- **Sharding model**: An index is divided into N primary shards (set at index creation, immutable). Each primary shard has R replica shards (configurable anytime). Total shards = N × (1 + R). Shard = a Lucene index = the unit of parallelism.
- **Shard sizing**: The most critical capacity planning decision. Rule of thumb: 10-50 GB per shard. Too small → overhead per shard (each shard consumes memory for metadata, segment info, caches). Too large → slow recovery, slow merges, unbalanced cluster.
- **Shard count considerations**: Each shard consumes ~1-5 MB of heap on the master node. Clusters with millions of shards (common in log analytics with daily indices) suffer "shard explosion" — master node OOM. ISM policies with rollover help control shard count.
- **Routing**: Documents are assigned to shards via `shard = hash(_routing) % num_primary_shards`. Default `_routing` is `_id`. Custom routing enables co-locating related documents on the same shard (e.g., all documents for a tenant on one shard) — enables shard-level queries instead of scatter-gather.
- **Node roles**:
  - **Master-eligible nodes**: Manage cluster state (index metadata, shard allocation, mappings). Lightweight — don't hold data. Run 3 dedicated master nodes for quorum (avoids split-brain).
  - **Data nodes**: Store shards, execute search and indexing. CPU/memory/disk intensive.
  - **Coordinating-only nodes**: Route requests, merge query-phase results, execute the fetch phase. Offload merge work from data nodes.
  - **Ingest nodes**: Execute ingest pipelines. Can be co-located with data or dedicated.
  - **UltraWarm nodes** (AWS-specific): Warm storage tier using S3-backed storage. Read-only indices at lower cost. Uses tiered caching (local SSD cache → S3).
  - **Cold storage** (AWS-specific): Detach indices to S3. Near-zero compute cost. Must be re-attached to query.
- **Shard allocation and rebalancing**: Master node tracks shard placement. Allocation awareness: spread replicas across AZs (zone awareness). Rebalancing: when a node joins/leaves, master reassigns shards to maintain even distribution. Shard relocation is I/O intensive — throttled by `cluster.routing.allocation.node_concurrent_recoveries`.
- **Split-brain prevention**: Master election requires a quorum (majority of master-eligible nodes). With 3 master-eligible nodes, quorum = 2. If network partitions isolate 1 master from 2 others, the 2-node partition elects a new master; the isolated node steps down. `discovery.zen.minimum_master_nodes` (legacy) / now automatic in OpenSearch.
- **Contrast with DynamoDB/Cassandra**: DynamoDB uses consistent hashing with virtual nodes — auto-scales partitions transparently. OpenSearch shards are fixed at index creation (no auto-split). This is a fundamental limitation — wrong shard count decision at creation time is hard to fix (must reindex). DynamoDB/Cassandra handle this better for KV workloads, but OpenSearch's Lucene-based shards enable full-text search that KV stores can't do.

### 6. 06-aws-managed-service.md — AWS OpenSearch Service Architecture

How AWS operates OpenSearch as a managed service.

- **Domain (cluster) provisioning**: Users create a "domain" (an OpenSearch cluster). Choose instance types (data, master, UltraWarm, cold), instance count, storage (EBS gp3/io2), VPC/public access, encryption, authentication.
- **Control plane vs data plane**: AWS control plane manages provisioning, upgrades, patching, monitoring. Data plane is the customer's OpenSearch cluster. Control plane uses AWS service APIs; data plane exposes OpenSearch REST APIs.
- **Blue-green deployments**: For configuration changes and version upgrades, AWS creates a new set of nodes (blue), migrates shards, validates health, then swaps traffic. Minimizes downtime. Can cause temporary 2x resource consumption.
- **Multi-AZ deployment**: Recommended production setup. Data nodes spread across 2 or 3 AZs. Zone awareness ensures replicas are in different AZs. If one AZ fails, replicas in other AZs serve traffic. 3-AZ deployment survives one AZ failure without data loss.
- **Storage tiers**:
  - **Hot** (EBS-backed data nodes): For frequently queried, recently indexed data. Standard instance types (r6g, r7g, etc.).
  - **UltraWarm** (S3-backed): For read-only, infrequently accessed data. Up to 3x cheaper than hot. Uses local SSD caching for performance.
  - **Cold storage** (S3, detached): Near-zero cost. Must re-attach to UltraWarm to query. Ideal for compliance/archival.
- **Security**:
  - **VPC access**: Domain deployed within customer's VPC. Private endpoints only.
  - **Fine-grained access control (FGAC)**: Powered by OpenSearch Security plugin. Index-level, document-level, field-level permissions. Integrates with IAM, SAML, Cognito.
  - **Encryption**: At-rest (KMS), in-transit (TLS), node-to-node encryption.
- **Monitoring**: CloudWatch metrics (cluster health, CPU, JVM pressure, search latency, indexing rate), CloudWatch Logs (slow logs, error logs), AWS CloudTrail (API audit).
- **Limitations vs self-managed**:
  - Cannot install custom plugins (AWS controls plugin set).
  - Cannot modify certain cluster settings (e.g., `discovery.*`, `network.*`).
  - Version upgrades are AWS-managed (can lag behind open-source releases).
  - No direct SSH access to nodes.
- **Serverless** (OpenSearch Serverless): Auto-scaling, no cluster management. Uses "collections" instead of indices. Separate compute for indexing and search (decouple write and read scaling). Two collection types: time-series (log analytics) and search (application search). Trade-off: higher per-query cost, less control, some API restrictions.
- **Contrast with self-managed OpenSearch/Elasticsearch**: Self-managed gives full control (any plugin, any setting, any version) but requires operational expertise (capacity planning, upgrades, monitoring, security patching). AWS-managed trades control for operational simplicity. For most teams, managed is the right choice unless they need custom plugins or cutting-edge versions.

### 7. 07-log-analytics-pipeline.md — Log Analytics (Primary Use Case)

Log analytics is the #1 use case for OpenSearch. This doc covers the end-to-end pipeline.

- **Data sources**: Application logs, infrastructure logs (CloudWatch, VPC Flow Logs), access logs (ALB, CloudFront), security logs (GuardDuty, WAF), custom metrics, trace data (OpenTelemetry).
- **Ingestion pipeline**:
  - **Fluentd / Fluent Bit**: Lightweight log collectors deployed as DaemonSets in Kubernetes or sidecars. Collect, parse, and forward logs.
  - **Logstash**: Heavier log processing pipeline. Rich filter ecosystem (grok, mutate, date, geoip). Runs as a separate process.
  - **Amazon Data Firehose** (formerly Kinesis Data Firehose): Managed delivery stream. Buffers, batches, and delivers logs to OpenSearch. Handles backpressure and retry.
  - **OpenSearch Ingestion** (OSI): AWS-managed, OpenTelemetry-based ingestion pipeline. Replaces self-managed Logstash.
  - **Direct bulk API**: Applications index directly via `_bulk` API. Simplest but couples application to OpenSearch.
- **Index strategy for logs**:
  - **Time-based indices**: One index per time period (daily: `logs-2025-01-15`, hourly for high-volume). Enables efficient deletion (drop old index vs delete-by-query).
  - **Index rollover**: Create new index when current index reaches a size/age/doc-count threshold. Combined with aliases: `logs-write` alias points to current index, `logs-read` alias spans all indices.
  - **Index templates**: Define mappings and settings that auto-apply to new indices matching a pattern (e.g., `logs-*`).
  - **Data streams** (OpenSearch 2.x): Abstraction over time-based indices. Append-only, auto-rollover, simplified management.
- **ISM (Index State Management)**: Automate lifecycle: hot (active indexing + search, EBS) → warm (read-only, UltraWarm) → cold (detached, S3) → delete. Example policy: hot for 7 days → warm for 30 days → cold for 90 days → delete after 365 days.
- **Schema design for logs**: Use ECS (Elastic Common Schema) or OpenTelemetry Semantic Conventions for field naming consistency across sources. Define strict mappings for known fields (avoid dynamic mapping's field explosion problem). Use `keyword` type for fields you filter/aggregate on, `text` type for fields you full-text search.
- **Dashboards (OpenSearch Dashboards / Kibana fork)**: Visualize logs with Discover (log explorer), Visualize (charts, graphs), Dashboards (saved layouts), Alerting (trigger notifications on query conditions).
- **Scale considerations**: High-volume log ingestion (100K+ events/sec) requires careful tuning: bulk batch size, refresh interval, shard count, merge throttling, JVM heap sizing. Common bottleneck: JVM garbage collection pauses under heavy indexing load.
- **Contrast with Splunk / Datadog / CloudWatch Logs**: OpenSearch is open-source and self-hostable. Splunk is proprietary with per-GB pricing (expensive at scale). Datadog is SaaS with per-host pricing. CloudWatch Logs Insights is serverless but limited query language. OpenSearch offers the best cost-to-flexibility ratio for teams willing to manage (or use AWS-managed) infrastructure.

### 8. 08-performance-and-scaling.md — Performance Tuning & Scaling

- **JVM heap sizing**: Set to 50% of available RAM, max 32 GB (beyond 32 GB, JVM loses compressed oops — pointers double in size, negating the extra heap). Remaining RAM for OS filesystem cache (Lucene segments are memory-mapped). JVM GC tuning: G1GC (default), monitor GC pauses via `_nodes/stats`.
- **Search performance**:
  - Reduce shard count (fewer shards = fewer query-phase round-trips).
  - Use `filter` context instead of `query` context when scoring isn't needed (filters are cached, queries are not).
  - Avoid deep pagination (`from` + `size` beyond 10,000 — use `search_after` or PIT).
  - Prefer `keyword` fields for exact match (skip analysis).
  - Use `routing` to limit queries to specific shards.
  - Warm up caches: field data cache, query cache, OS filesystem cache.
- **Indexing performance**:
  - Use `_bulk` API with 5-15 MB batches.
  - Increase `refresh_interval` (default 1s → 30s or disable during bulk loads).
  - Increase `translog.flush_threshold_size`.
  - Use auto-generated `_id` (avoids version check on each index operation).
  - Scale horizontally: more data nodes = more primary shards can index in parallel.
- **Scaling strategies**:
  - **Vertical**: Larger instance types (more CPU, RAM, disk). Simple but has a ceiling.
  - **Horizontal**: More data nodes. Requires proper shard count to distribute load.
  - **Read scaling**: Add replicas (each replica can serve search requests independently). Trade-off: more replicas = more disk + more replication overhead on writes.
  - **Write scaling**: More primary shards (must be set at index creation or reindex). More data nodes to host them.
  - **Tiered storage**: Move old data to UltraWarm/cold. Reduces hot-tier resource requirements.
- **Capacity planning**: Estimate daily ingest volume (GB/day), retention period, replica count, expected query load. Total storage = daily_volume × retention_days × (1 + replica_count) × 1.1 (overhead). Shard count = total_storage / target_shard_size (10-50 GB).
- **Monitoring key metrics**: Cluster health (green/yellow/red), JVM heap usage (stay below 85%), search latency (p50, p99), indexing rate, merge rate, GC pause time, disk watermarks (low: 85%, high: 90%, flood: 95%).
- **Contrast with ClickHouse / Apache Druid**: For pure analytics (aggregations on structured data), columnar stores like ClickHouse or Druid are significantly faster (10-100x for aggregation queries). OpenSearch's strength is combining full-text search with analytics. If your use case is pure analytics without text search, consider a columnar store instead.

### 9. 09-advanced-features.md — Advanced Features & Use Cases

- **k-NN (k-Nearest Neighbor) vector search**: Store dense vectors, search by similarity (cosine, L2, inner product). Powers semantic search, recommendation, image search. Uses HNSW (Hierarchical Navigable Small World) or IVF (Inverted File) algorithms. OpenSearch supports approximate k-NN (fast, not exact) and exact k-NN (brute force, slower).
- **Neural search**: Combine semantic (vector) search with lexical (BM25) search. Ingest pipeline encodes text to vectors at index time. Query-time hybrid scoring blends BM25 and k-NN scores. Better relevance than either approach alone.
- **Observability (OpenTelemetry integration)**: Traces, metrics, logs — the three pillars. OpenSearch can serve as a backend for all three. Trace Analytics plugin provides service maps, latency analysis, error analysis. Competes with Jaeger, Zipkin, Grafana Tempo.
- **Alerting**: Define monitors (scheduled queries), triggers (conditions on query results), actions (SNS, Slack, webhook, email). Example: alert if error rate > 5% in the last 5 minutes. Supports composite monitors, alert deduplication, and acknowledge/mute.
- **Anomaly detection**: ML-powered anomaly detection on time-series data. Random Cut Forest (RCF) algorithm. Detect anomalies in real-time without manual threshold setting. Use cases: detect spikes in error rates, unusual traffic patterns, infrastructure anomalies.
- **Cross-cluster replication (CCR)**: Replicate indices from a leader cluster to follower clusters. Use cases: disaster recovery (geo-redundant data), read scaling (local read replicas in multiple regions), data locality (keep data close to users).
- **Cross-cluster search**: Query across multiple clusters without replicating data. Useful for federated search across organizational boundaries.
- **SQL support**: Query OpenSearch using SQL syntax via `_plugins/_sql` endpoint. Translates SQL to OpenSearch Query DSL. Useful for analysts familiar with SQL. Supports SELECT, WHERE, GROUP BY, ORDER BY, JOIN (limited), subqueries.
- **Piped Processing Language (PPL)**: OpenSearch-specific query language. Pipe-based syntax similar to Splunk SPL. `search source=logs | where status=500 | stats count() by service | sort -count()`. More intuitive than Query DSL for log exploration.
- **Contrast with Pinecone / Weaviate (vector DBs)**: Purpose-built vector databases offer simpler APIs and potentially better k-NN performance for pure vector search. OpenSearch's advantage is combining vector search with full-text search, aggregations, and the existing operational ecosystem. If your use case is pure vector search (RAG, semantic search only), a dedicated vector DB may be simpler. If you need hybrid (text + vector + analytics), OpenSearch is a strong choice.

### 10. 10-design-trade-offs.md — Design Philosophy & Trade-off Analysis

Opinionated analysis of OpenSearch's design choices — not just "what" but "why this and not that."

- **Inverted index vs B-tree**: OpenSearch uses inverted indices (Lucene) for full-text search. RDBMS uses B-trees for exact match and range queries. Inverted indices excel at tokenized text search but are less efficient for exact-match point queries. This is why OpenSearch complements, not replaces, a database.
- **Immutable segments vs mutable pages**: Lucene writes immutable segments. RDBMS uses mutable B-tree pages. Immutability enables lock-free reads, simple caching, and crash recovery (no partial writes). Trade-off: deletes are "soft" (tombstones) until segment merge, so deleted documents still consume disk until merged away.
- **Near-real-time vs real-time**: OpenSearch's 1-second refresh interval means documents aren't searchable instantly. This is a deliberate trade-off: batching flushes to segments is far more efficient than flushing after every write. For use cases requiring instant visibility, reduce `refresh_interval` (at the cost of more small segments and higher merge overhead).
- **Fixed shard count vs auto-partitioning**: OpenSearch's shard count is immutable after index creation. DynamoDB/Cassandra auto-partition. Fixed shards are simpler (deterministic routing) but require upfront capacity planning. Wrong shard count requires full reindex. This is OpenSearch's biggest operational pain point.
- **Schemaless vs schema**: OpenSearch supports dynamic mapping (auto-detect field types) and explicit mapping. Dynamic mapping is convenient but dangerous at scale — a log field with high cardinality (e.g., user IDs as text) can cause mapping explosion and OOM. Best practice: use strict mapping for production indices.
- **Managed (AWS) vs self-managed**: AWS OpenSearch Service trades control (no custom plugins, limited settings) for operational simplicity (automated backups, upgrades, monitoring). Self-managed gives full control but requires deep expertise. For most teams: start managed, migrate to self-managed only if you hit managed-service limitations.
- **OpenSearch vs Elasticsearch**: Fork happened in 2021 when Elastic changed from Apache 2.0 to SSPL. OpenSearch is Apache 2.0, community-driven. Feature parity is close but diverging (OpenSearch: k-NN, anomaly detection, PPL; Elasticsearch: ESQL, universal profiling). API compatibility is high for 7.x workloads. Migration is straightforward for most use cases.
- **Single-purpose vs converged**: OpenSearch tries to be search + analytics + observability + vector DB. Each individual use case has a better specialized tool (ClickHouse for analytics, Pinecone for vectors, Datadog for observability). OpenSearch's value proposition is "good enough at all of them in one system" — reduces operational burden of running multiple systems. Trade-off: jack-of-all-trades vs master-of-one.

## CRITICAL: The design must be OpenSearch-centric
OpenSearch (and its Elasticsearch heritage) is the reference implementation. The design should reflect how OpenSearch actually works — its Lucene-based inverted index, shard distribution, query execution, and AWS managed service architecture. Where other systems (Solr, ClickHouse, DynamoDB, Pinecone) made different design choices, call those out explicitly as contrasts.

## CRITICAL: Iterative build-up (DO NOT skip this)
The architecture MUST evolve iteratively. Each attempt builds on the previous one by identifying a concrete problem and solving it. The candidate should NOT jump to the final architecture. The progression should feel natural:

### Attempt 0: Single-node full-text search
```
┌──────────┐         ┌─────────────────────────────┐
│  Client   │──HTTP──▶│  Single Server Process       │
│ (curl/app)│◀────────│  ┌───────────────────────┐   │
└──────────┘         │  │  Documents (flat files) │   │
                     │  │  doc1.json, doc2.json.. │   │
                     │  └───────────────────────┘   │
                     │  Search = grep/scan ALL docs  │
                     └─────────────────────────────┘
```
- Client sends a search query, server scans all documents sequentially (brute-force grep). Returns matching documents.
- No index structure — every query is a full scan.
- **Problems found**: Linear scan is O(N) per query — 1M docs × 10ms/doc = 10,000 seconds per query. No relevance ranking (which result is "best"?). No durability (crash = data loss). Single point of failure. Can't handle concurrent users.

### Attempt 1: Inverted index + Lucene engine on a single node
```
┌──────────┐         ┌──────────────────────────────────────────────┐
│  Client   │──HTTP──▶│  OpenSearch Node                              │
│           │◀────────│                                              │
└──────────┘         │  ┌──────────────┐    ┌──────────────────┐   │
                     │  │ REST API      │───▶│ Lucene Engine     │   │
                     │  │ Layer         │◀───│                  │   │
                     │  └──────────────┘    │ ┌──────────────┐ │   │
                     │                      │ │ In-Memory     │ │   │
                     │  ┌──────────────┐    │ │ Index Buffer  │ │   │
                     │  │ Text Analysis │    │ └──────┬───────┘ │   │
                     │  │ Pipeline      │    │        │refresh  │   │
                     │  │ char filter → │    │        ▼         │   │
                     │  │ tokenizer →   │    │ ┌──────────────┐ │   │
                     │  │ token filter  │    │ │ Immutable     │ │   │
                     │  └──────────────┘    │ │ Segments      │ │   │
                     │                      │ │ (inverted     │ │   │
                     │  ┌──────────────┐    │ │  index on     │ │   │
                     │  │ Translog     │    │ │  disk)        │ │   │
                     │  │ (WAL)        │    │ └──────────────┘ │   │
                     │  └──────────────┘    └──────────────────┘   │
                     └──────────────────────────────────────────────┘
```
**Components and interactions**:
- **REST API Layer**: Receives HTTP requests (index doc, search), dispatches to Lucene engine.
- **Text Analysis Pipeline**: On write — transforms raw text into tokens. `"The Quick Brown FOX!"` → char filter → tokenizer → lowercase filter → `["quick", "brown", "fox"]`. Analyzer choice directly affects what searches match.
- **Lucene Engine**: Maintains the inverted index (term → posting list). Search is O(1) term lookup + posting list intersection instead of O(N) scan.
- **In-Memory Index Buffer**: New documents land here first. NOT searchable yet.
- **Refresh** (default every 1 second): Flushes buffer into a new immutable **Segment** on disk. Now searchable. This is the "near-real-time" gap.
- **Immutable Segments**: Each segment is a self-contained inverted index. Immutability enables lock-free concurrent reads. Background **segment merge** compacts small segments into larger ones.
- **Translog (WAL)**: Write-ahead log for durability. Every write goes to translog first (fsync'd). If node crashes before refresh, replay translog to recover buffered docs.
- **BM25 scoring**: At query time, rank results by relevance (term frequency saturation + document length normalization).
- **Contrast with RDBMS**: B-tree index does exact match/range efficiently. Inverted index does tokenized text search efficiently. Different data structures for different access patterns — this is why OpenSearch complements a database, not replaces it.
- **Problems found**: Single node has limited storage (one disk) and compute (one CPU). Can't handle more data than one machine holds. Can't handle more queries than one machine processes. Single point of failure — node dies, everything is gone.

### Attempt 2: Sharding — split the index across multiple nodes
```
┌──────────┐
│  Client   │
└─────┬────┘
      │ HTTP
      ▼
┌─────────────────────┐
│  Coordinating Node   │  ← Routes requests, merges results
│                     │
│  1. Receives query  │
│  2. Scatter to all  │
│     shards (query   │
│     phase)          │
│  3. Merge top-N     │
│     from each shard │
│  4. Fetch full docs │
│     (fetch phase)   │
│  5. Return to client│
└──┬───────┬──────┬──┘
   │       │      │
   ▼       ▼      ▼
┌──────┐┌──────┐┌──────┐
│Node 1││Node 2││Node 3│
│      ││      ││      │
│Shard0││Shard1││Shard2│  ← Each shard = independent Lucene index
│(P)   ││(P)   ││(P)   │
│      ││      ││      │
│Local ││Local ││Local │
│Lucene││Lucene││Lucene│
│Engine││Engine││Engine│
└──────┘└──────┘└──────┘

Document routing: shard = hash(_id) % 3
```
**Components and interactions**:
- **Coordinating Node**: The entry point. Receives client requests. For indexing: routes document to the correct shard based on `hash(_id) % num_shards`. For search: scatters the query to ALL shards, collects results, merges.
- **Data Nodes (Node 1, 2, 3)**: Each hosts one or more primary shards. Each shard is a fully independent Lucene index with its own segments, translog, and in-memory buffer.
- **Two-phase search**:
  1. **Query phase**: Coordinating node sends query to all 3 shards in parallel. Each shard runs the query against its local inverted index, returns top-N doc IDs + BM25 scores (lightweight — no full documents transferred).
  2. **Fetch phase**: Coordinating node identifies global top-N from the merged shard results. Sends multi-get for just those doc IDs to the relevant shards. Shards return full `_source` documents.
  - Why two phases? Query phase transfers only IDs+scores (tiny). Fetch phase only fetches the final top-N documents. Avoids shipping full documents from every shard on every query.
- **Shard count is fixed at index creation** — `hash(_id) % N` requires N to be constant. Changing N means all documents hash to different shards → full reindex required. This is the #1 operational pain point.
- **Contrast with DynamoDB**: DynamoDB uses consistent hashing with auto-split partitions — partition count grows automatically as data grows. OpenSearch requires you to pick shard count upfront. DynamoDB handles this better for KV workloads, but OpenSearch's Lucene-based shards enable full-text search that KV stores can't do.
- **Problems found**: If Node 2 dies, Shard 1 is gone — data loss. No redundancy. All shards are primaries — if any node is down, the cluster has incomplete data and queries return partial results. No way to survive hardware failure.

### Attempt 3: Replication + master election for availability
```
┌──────────┐
│  Client   │
└─────┬────┘
      │
      ▼
┌─────────────────────┐
│  Coordinating Node   │  ← Can be a dedicated node or any data node
└──┬───────┬──────┬──┘
   │       │      │
   ▼       ▼      ▼

  AZ-1            AZ-2            AZ-3
┌──────────┐  ┌──────────┐  ┌──────────┐
│ Data      │  │ Data      │  │ Data      │
│ Node 1    │  │ Node 2    │  │ Node 3    │
│           │  │           │  │           │
│ Shard0(P)─┼──┼─Shard0(R)│  │           │  ← Primary → Replica replication
│           │  │           │  │ Shard0(R) │
│ Shard1(R) │  │ Shard1(P)─┼──┼─Shard1(R)│
│           │  │           │  │           │
│ Shard2(R) │  │           │  │ Shard2(P) │
└──────────┘  └──────────┘  └──────────┘

┌──────────┐  ┌──────────┐  ┌──────────┐
│ Master    │  │ Master    │  │ Master    │  ← 3 dedicated master-eligible nodes
│ Node 1    │  │ Node 2    │  │ Node 3    │     (quorum = 2 for leader election)
│ (leader)  │  │ (follower)│  │ (follower)│
└──────────┘  └──────────┘  └──────────┘

Cluster state (managed by master leader):
  - Which shards exist on which nodes
  - Index mappings and settings
  - Shard allocation decisions
```
**Components and interactions**:
- **Primary shards**: Accept writes. Replicate to replica shards after successful write.
- **Replica shards**: Read-only copies on different nodes (and different AZs via zone awareness). Serve search requests — doubles/triples read capacity. If the primary's node dies, master promotes a replica to primary.
- **Write flow**: Client → Coordinating Node → route to Primary Shard → write to translog + buffer → replicate to Replica Shards → ack to client (after replicas confirm, configurable).
- **Read flow**: Client → Coordinating Node → route to ANY copy (primary OR replica) of each shard → query phase → fetch phase → return. Replicas share read load.
- **Dedicated Master Nodes (3)**: Lightweight nodes that don't hold data. Manage cluster state: shard allocation table, index metadata, mappings. Leader election via quorum (majority of 3 = 2 nodes must agree). Prevents split-brain: if network partition isolates 1 master from 2 others, the 2-node partition elects a new leader; the isolated node steps down.
- **Zone awareness**: Master's allocation algorithm ensures replicas of the same shard are in different AZs. If AZ-2 goes down entirely, AZ-1 and AZ-3 still have at least one copy of every shard.
- **Cluster health signal**: **Green** (all primaries + all replicas allocated), **Yellow** (all primaries OK, some replicas unassigned — functional but degraded), **Red** (some primaries unassigned — data loss risk, partial results).
- **Shard rebalancing**: When a node joins or leaves, master redistributes shards to maintain even balance. Rebalancing is I/O intensive — throttled to avoid starving search traffic.
- **Problems found**: All data sits on hot EBS storage — old logs from 6 months ago consume the same expensive resources as today's logs. No automated lifecycle management (manual index deletion). Ingestion is ad-hoc (applications POST directly to `_bulk` — no centralized pipeline, no parsing, no buffering). No visualization layer — must query via raw API.

### Attempt 4: Ingestion pipeline + tiered storage + dashboards
```
                          DATA SOURCES
    ┌──────────┐  ┌──────────┐  ┌──────────────┐  ┌──────────┐
    │App Logs  │  │CloudWatch│  │VPC Flow Logs │  │Custom    │
    │(stdout)  │  │Logs      │  │              │  │Metrics   │
    └────┬─────┘  └────┬─────┘  └──────┬───────┘  └────┬─────┘
         │             │               │                │
         ▼             ▼               ▼                ▼
    ┌─────────────────────────────────────────────────────────┐
    │              INGESTION LAYER                            │
    │                                                         │
    │  ┌───────────┐  ┌────────────────┐  ┌───────────────┐  │
    │  │Fluent Bit │  │Amazon Data     │  │OpenSearch     │  │
    │  │/ Fluentd  │  │Firehose        │  │Ingestion (OSI)│  │
    │  │(collect,  │  │(buffer, batch, │  │(managed       │  │
    │  │ parse,    │  │ retry,         │  │ OTel-based    │  │
    │  │ forward)  │  │ backpressure)  │  │ pipeline)     │  │
    │  └─────┬─────┘  └──────┬─────────┘  └──────┬────────┘  │
    └────────┼───────────────┼───────────────────┼────────────┘
             │               │                   │
             ▼               ▼                   ▼
    ┌─────────────────────────────────────────────────────────┐
    │              OPENSEARCH CLUSTER                          │
    │                                                         │
    │  ┌──────────────────────────────────────────────────┐   │
    │  │ Ingest Pipeline (at-index-time transforms)       │   │
    │  │ grok → date → rename → geoip → enrich            │   │
    │  └──────────────────────┬───────────────────────────┘   │
    │                         │                               │
    │  INDEX STRATEGY (time-based + aliases)                  │
    │  ┌──────────────────────────────────────────────────┐   │
    │  │ logs-write alias ──▶ logs-2025-01-15 (current)   │   │
    │  │ logs-read alias  ──▶ logs-2025-01-* (all)        │   │
    │  │                                                  │   │
    │  │ Index Template: logs-* → mappings + settings     │   │
    │  │ Rollover: new index when size > 50GB or age > 1d │   │
    │  └──────────────────────────────────────────────────┘   │
    │                                                         │
    │  TIERED STORAGE (ISM policy drives transitions)        │
    │  ┌────────────┐  ┌─────────────┐  ┌──────────────┐    │
    │  │ HOT (EBS)  │─▶│ WARM        │─▶│ COLD         │    │
    │  │ 0-7 days   │  │ (UltraWarm) │  │ (S3 detached)│    │
    │  │ read+write │  │ 7-30 days   │  │ 30-365 days  │    │
    │  │ data nodes │  │ read-only   │  │ near-zero    │──▶ DELETE
    │  │ (r6g/r7g)  │  │ S3-backed   │  │ compute cost │    │
    │  │            │  │ SSD cache   │  │ re-attach to │    │
    │  │            │  │ 3x cheaper  │  │ query        │    │
    │  └────────────┘  └─────────────┘  └──────────────┘    │
    └─────────────────────────────────────────────────────────┘
             │
             ▼
    ┌─────────────────────────────────────────────────────────┐
    │              VISUALIZATION LAYER                        │
    │                                                         │
    │  ┌──────────────────────────────────────────────────┐   │
    │  │ OpenSearch Dashboards (Kibana fork)               │   │
    │  │                                                  │   │
    │  │ ┌──────────┐ ┌──────────┐ ┌────────┐ ┌────────┐│   │
    │  │ │ Discover │ │Visualize │ │Dash-   │ │Alerting││   │
    │  │ │ (log     │ │(charts,  │ │boards  │ │(monitors│   │
    │  │ │ explorer)│ │ graphs)  │ │(saved  │ │triggers,│   │
    │  │ │          │ │          │ │layouts)│ │actions) ││   │
    │  │ └──────────┘ └──────────┘ └────────┘ └────────┘│   │
    │  └──────────────────────────────────────────────────┘   │
    └─────────────────────────────────────────────────────────┘
```
**Components and interactions**:
- **Data Sources → Ingestion Layer**: Logs flow from applications, AWS services, infrastructure into collectors.
  - **Fluent Bit / Fluentd**: Deployed as DaemonSets (Kubernetes) or sidecars. Collect logs from stdout/files, parse (regex/JSON), forward to OpenSearch or Firehose. Lightweight — Fluent Bit uses ~450 KB memory.
  - **Amazon Data Firehose**: Managed delivery stream. Buffers logs (1-15 min or 1-128 MB), handles backpressure and retry. If OpenSearch is slow/down, Firehose buffers and retries — protects the cluster from being overwhelmed.
  - **OpenSearch Ingestion (OSI)**: AWS-managed pipeline based on Data Prepper (OpenTelemetry Collector). Replaces self-managed Logstash. Handles traces, metrics, and logs.
- **Ingestion Layer → Ingest Pipeline**: Documents hit OpenSearch's ingest pipeline before indexing. Processors transform documents at index time: `grok` (parse unstructured log lines into fields), `date` (parse timestamps), `geoip` (IP → geo coordinates), `rename`, `script`. Alternative to running Logstash separately.
- **Index Strategy**: Time-based indices (`logs-2025-01-15`) with aliases:
  - **Write alias** (`logs-write`): Points to the current active index. Application always writes to this alias — never hardcodes index names.
  - **Read alias** (`logs-read`): Spans all indices matching `logs-*`. Searches query across all time windows.
  - **Rollover**: When current index hits 50 GB or 1 day, automatically create a new index and repoint the write alias. Combined with **Index Templates** that auto-apply mappings + settings to any new `logs-*` index.
- **ISM (Index State Management)**: The lifecycle automation engine. Defines a state machine:
  - `hot` (0-7 days): Active indexing + search on fast EBS-backed data nodes.
  - `warm` (7-30 days): Migrate to **UltraWarm nodes** — S3-backed with local SSD caching. Read-only. ~3x cheaper than hot.
  - `cold` (30-365 days): Detach to S3. Near-zero compute cost. Must re-attach to UltraWarm to query.
  - `delete` (>365 days): Permanently remove.
  - ISM transitions are automatic — no human intervention. This is what makes OpenSearch viable for log analytics at scale.
- **OpenSearch Dashboards → Cluster**: Visualization layer that queries the cluster via the same REST APIs. Discover (interactive log explorer with query bar), Visualize (build charts/graphs), Dashboards (compose saved visualizations), Alerting (schedule queries, trigger SNS/Slack/webhook on conditions).
- **Contrast with Splunk / Datadog**: Splunk is proprietary with per-GB pricing — expensive at high volume. Datadog is SaaS with per-host pricing. CloudWatch Logs Insights is serverless but limited query language. OpenSearch is open-source with the best cost-to-flexibility ratio for teams willing to manage infrastructure.
- **Problems found**: Search is pure lexical (keyword matching only) — searching "memory issue" doesn't find documents saying "OOM error" or "heap exhaustion." No anomaly detection — operators must manually define alert thresholds. No cross-region resilience — if the AWS region goes down, search is down. Performance tuning is manual (shard sizing, JVM tuning, query optimization). No field-level security — all users see all data.

### Attempt 5: Production hardening — security, ML features, cross-cluster, monitoring
```
                    ┌──────────────────────────────────┐
                    │        SECURITY BOUNDARY          │
                    │  VPC + FGAC + Encryption          │
                    │                                  │
                    │  ┌────────────────────────────┐  │
                    │  │ IAM / SAML / Cognito       │  │
                    │  │ Authentication             │  │
                    │  └─────────┬──────────────────┘  │
                    │            │                      │
                    │            ▼                      │
                    │  ┌────────────────────────────┐  │
                    │  │ Fine-Grained Access Control│  │
                    │  │ (Security Plugin)          │  │
                    │  │                            │  │
                    │  │ Index-level: team-a can    │  │
                    │  │   only read logs-team-a-*  │  │
                    │  │ Document-level: filter by  │  │
                    │  │   tenant_id in query       │  │
                    │  │ Field-level: PII fields    │  │
                    │  │   hidden from analysts     │  │
                    │  └─────────┬──────────────────┘  │
                    └────────────┼──────────────────────┘
                                 │
                                 ▼
┌──────────────────────────────────────────────────────────────────────┐
│                     OPENSEARCH CLUSTER (Primary Region)              │
│                                                                      │
│  ┌────────────────────────────────────────────────────────────────┐  │
│  │  SEARCH + ANALYTICS ENGINE (from Attempts 1-4)                │  │
│  │  Coordinating → Data Nodes (shards) → Tiered Storage          │  │
│  └───────────┬──────────────────┬────────────────────────────────┘  │
│              │                  │                                    │
│  ┌───────────▼──────────┐  ┌───▼──────────────────────────────┐    │
│  │  VECTOR SEARCH       │  │  ANOMALY DETECTION               │    │
│  │  (k-NN Plugin)       │  │  (ML Plugin)                     │    │
│  │                      │  │                                  │    │
│  │  ┌────────────────┐  │  │  ┌────────────────────────────┐  │    │
│  │  │ Neural Search   │  │  │  │ Random Cut Forest (RCF)    │  │    │
│  │  │ Pipeline        │  │  │  │ Algorithm                  │  │    │
│  │  │                │  │  │  │                            │  │    │
│  │  │ Ingest: text──▶│  │  │  │ Time-series data ──▶       │  │    │
│  │  │ ML model ──▶   │  │  │  │ Detect anomalies without   │  │    │
│  │  │ dense vector   │  │  │  │ manual thresholds          │  │    │
│  │  │                │  │  │  │                            │  │    │
│  │  │ Query: hybrid  │  │  │  │ ──▶ Alerting Plugin ──▶    │  │    │
│  │  │ BM25 + k-NN    │  │  │  │     SNS / Slack / webhook  │  │    │
│  │  │ score fusion   │  │  │  └────────────────────────────┘  │    │
│  │  └────────────────┘  │  └──────────────────────────────────┘    │
│  │                      │                                          │
│  │  HNSW index per      │  ┌──────────────────────────────────┐    │
│  │  shard (approximate  │  │  OBSERVABILITY                   │    │
│  │  nearest neighbor)   │  │  (Trace Analytics Plugin)        │    │
│  └──────────────────────┘  │                                  │    │
│                            │  OpenTelemetry traces ──▶        │    │
│                            │  Service map + latency analysis  │    │
│                            └──────────────────────────────────┘    │
└───────────────┬──────────────────────────────────────────────────────┘
                │
                │  Cross-Cluster Replication (CCR)
                │  leader → follower (async)
                │
                ▼
┌──────────────────────────────────────────────────────────────────────┐
│              OPENSEARCH CLUSTER (DR / Read-Local Region)             │
│                                                                      │
│  Follower indices (read-only replicas of leader indices)            │
│  Serves local reads — reduces cross-region latency                  │
│  Promotes to leader if primary region fails                         │
└──────────────────────────────────────────────────────────────────────┘

                    MONITORING (external to cluster)
┌──────────────────────────────────────────────────────────────────────┐
│  ┌─────────────┐  ┌──────────────┐  ┌──────────────────────────┐   │
│  │ CloudWatch  │  │ Slow Logs    │  │ Cluster Health API       │   │
│  │ Metrics     │  │ (search >    │  │ /_cluster/health         │   │
│  │             │  │  500ms,      │  │ /_nodes/stats            │   │
│  │ CPU, JVM    │  │  index >     │  │                          │   │
│  │ heap, disk, │  │  1000ms)     │  │ Key alerts:              │   │
│  │ search p99, │  │              │  │ - JVM heap > 85%         │   │
│  │ indexing    │  │ → CloudWatch │  │ - Cluster status YELLOW  │   │
│  │ rate, GC    │  │   Logs       │  │ - Disk watermark > 85%   │   │
│  │ pauses      │  │              │  │ - Search latency p99     │   │
│  └─────────────┘  └──────────────┘  └──────────────────────────┘   │
└──────────────────────────────────────────────────────────────────────┘

    ALTERNATIVE: OPENSEARCH SERVERLESS
┌──────────────────────────────────────────────────────────────────────┐
│  No cluster management. Auto-scaling compute.                       │
│  "Collections" instead of indices.                                  │
│  Separate compute for indexing vs search (decouple write/read).     │
│  Two types: time-series (logs) | search (app search).              │
│  Trade-off: higher per-query cost, less control, API restrictions.  │
│  Use when: team lacks OpenSearch operational expertise.              │
└──────────────────────────────────────────────────────────────────────┘
```
**Components and interactions**:
- **Security boundary** (wraps the entire cluster):
  - **VPC access**: Cluster lives in customer's VPC. No public internet exposure. Access via VPC endpoints.
  - **Authentication**: IAM policies, SAML (corporate SSO), Amazon Cognito (user pools). Determines WHO you are.
  - **Fine-Grained Access Control (FGAC)**: OpenSearch Security plugin. Determines WHAT you can see. Three levels:
    - **Index-level**: Team A can only access `logs-team-a-*` indices.
    - **Document-level**: Within an index, filter documents by `tenant_id` — multi-tenant isolation without separate indices.
    - **Field-level**: Mask PII fields (email, IP) from analyst role while showing them to admin role.
  - **Encryption**: At-rest (AWS KMS), in-transit (TLS 1.2+), node-to-node encryption.
- **Vector Search (k-NN plugin)** ← NEW component:
  - **Neural Search Pipeline**: At index time, an ML model converts text to dense vectors (embeddings). Stored alongside the inverted index. At query time, hybrid scoring fuses BM25 lexical score + k-NN vector similarity score.
  - **HNSW index**: Each shard builds an HNSW (Hierarchical Navigable Small World) graph for approximate nearest neighbor search. O(log N) query time.
  - Solves the "memory issue" ≠ "OOM error" problem from Attempt 4: semantic similarity catches meaning, not just keywords.
  - **Contrast with Pinecone/Weaviate**: Dedicated vector DBs may have better pure-vector performance. OpenSearch's value is hybrid (text + vector + analytics) in one system.
- **Anomaly Detection (ML plugin)** ← NEW component:
  - **Random Cut Forest (RCF)**: Unsupervised ML algorithm. Learns normal patterns from time-series data (error rates, latency, request counts). Flags deviations as anomalies without manually defined thresholds.
  - Connects to **Alerting Plugin**: When anomaly detected → trigger SNS notification, Slack message, or webhook. Replaces brittle static threshold alerts.
- **Observability (Trace Analytics plugin)** ← NEW component:
  - Ingests OpenTelemetry traces. Builds service dependency maps. Provides latency breakdown across services. Competes with Jaeger, Zipkin, Grafana Tempo — but integrated into the same OpenSearch cluster that holds your logs.
- **Cross-Cluster Replication (CCR)** ← NEW component:
  - **Leader cluster** (primary region) replicates selected indices to **follower cluster** (DR region). Async replication — follower indices are read-only.
  - Use cases: (1) Disaster recovery — if primary region fails, promote follower. (2) Read locality — users in EU query the EU follower instead of crossing the Atlantic.
  - **Contrast with Cassandra's multi-directional replication**: Cassandra supports multi-writer (any node accepts writes). OpenSearch CCR is single-writer (only leader accepts writes). Simpler conflict resolution but limits write availability.
- **Monitoring** (external to cluster):
  - **CloudWatch Metrics**: CPU, JVM heap %, search latency p50/p99, indexing rate, GC pause duration, active shard count.
  - **Slow Logs**: Queries exceeding threshold (e.g., search > 500ms, index > 1000ms) logged to CloudWatch Logs for debugging.
  - **Key alerts to configure**: JVM heap > 85% (risk of OOM), cluster status YELLOW/RED, disk watermark > 85% (shard allocation blocked at 90%), search p99 > SLA.
- **OpenSearch Serverless** (alternative deployment model):
  - No cluster provisioning or management. AWS auto-scales compute and storage.
  - Uses "collections" (not indices). Separate compute pools for indexing and search — write throughput doesn't affect read latency.
  - Trade-off: Higher per-query cost. Fewer API features. Less tuning control. Best for teams without OpenSearch operational expertise.

## CRITICAL: Accuracy requirements

I DO NOT TRUST AI-GENERATED FACTS ABOUT SPECIFIC SYSTEMS. Every concrete claim about OpenSearch internals must be verifiable against official sources. Specifically:

1. **Use WebSearch and WebFetch tools** to look up OpenSearch documentation, AWS documentation, and Elasticsearch/Lucene internals BEFORE writing. Search for:
   - "OpenSearch architecture internals"
   - "AWS OpenSearch Service best practices"
   - "OpenSearch shard sizing guidelines"
   - "Lucene inverted index internals"
   - "OpenSearch UltraWarm cold storage"
   - "OpenSearch k-NN vector search"
   - "OpenSearch ISM index state management"
   - "OpenSearch vs Elasticsearch differences"
   - "OpenSearch Serverless architecture"
   - "OpenSearch bulk indexing performance tuning"
   - "OpenSearch BM25 relevance scoring"
   - "OpenSearch cross-cluster replication"
   - "OpenSearch anomaly detection RCF"
   - "OpenSearch Dashboards capabilities"
   - "AWS OpenSearch blue-green deployment"

   **BLANKET PERMISSION**: You have full permission to use WebSearch and WebFetch to read any online documentation, blog posts, or research papers. Do NOT ask the user for permission to read — just read. This applies to opensearch.org, docs.aws.amazon.com, aws.amazon.com/blogs, and any other reference source. Read as many pages as needed to verify facts.

2. **For every concrete number** (shard sizing recommendations, JVM heap limits, refresh intervals, disk watermarks), verify against official OpenSearch or AWS documentation. If you cannot verify a number, explicitly write "[UNVERIFIED — check OpenSearch docs]" next to it.

3. **For every claim about OpenSearch internals** (segment merge behavior, query execution flow, cluster state management), if it's not from an official source, mark it as "[INFERRED — not officially documented]".

4. **CRITICAL: Do NOT confuse OpenSearch with Elasticsearch post-fork features.** They diverge:
   - OpenSearch: ISM (not ILM), Security plugin (not X-Pack), PPL, k-NN plugin, anomaly detection, OpenSearch Dashboards (not Kibana)
   - Elasticsearch: ILM, X-Pack Security, ESQL, Elastic Agent/Fleet, Kibana
   - When discussing features, be clear about which system you're referencing.

## Key OpenSearch topics to cover

### Requirements & Scale
- Full-text search service with sub-100ms p99 search latency
- Support for log analytics (100K+ events/sec ingestion), application search, and observability
- Horizontal scaling from single-node dev to multi-hundred-node production clusters
- Multi-AZ deployment for high availability
- Tiered storage: hot (EBS) → warm (UltraWarm/S3) → cold (S3 detached) → delete

### Architecture deep dives (create separate docs as listed in "Files to create" above)

### Design evolution (iterative build-up — the most important part)
- Attempt 0: Single node, brute-force scan
- Attempt 1: Inverted index on single node (Lucene, analyzers, BM25)
- Attempt 2: Sharding for horizontal scale (distributed query, coordinating node)
- Attempt 3: Replication for availability (replicas, master election, zone awareness)
- Attempt 4: Log analytics pipeline + tiered storage (ingestion, ISM, time-based indices, dashboards)
- Attempt 5: Production hardening (vector search, anomaly detection, CCR, security, monitoring, serverless)

Each attempt MUST:
1. Start by identifying concrete problems with the previous attempt
2. Propose a solution
3. Explain WHY this approach (and why not the alternative — mention Solr, ClickHouse, DynamoDB where relevant)
4. End with "what's still broken?" to motivate the next attempt

### Consistency & Data
- Near-real-time search (1-second refresh interval, not instant)
- Translog (WAL) for write durability
- Eventual consistency for replicas (async replication from primary to replicas)
- Segment immutability enables lock-free reads
- No transactions — OpenSearch is not a database, it's a search engine

## What NOT to do
- Do NOT treat OpenSearch as "just a database with search" — it's a specialized search and analytics engine built on inverted indices. Frame it accordingly.
- Do NOT confuse OpenSearch with Elasticsearch post-fork features. Highlight differences where they exist.
- Do NOT jump to the final architecture. Build it step by step (Attempt 0 → 5).
- Do NOT make up internal implementation details — verify against official docs or mark as inferred.
- Do NOT skip the iterative build-up.
- Do NOT write a Wikipedia article. This is an INTERVIEW SIMULATION with dialogue.
- Do NOT describe features without explaining WHY they exist (what problem they solve).
- Do NOT ask the user for permission to read online documentation — blanket permission is granted (see Accuracy requirements section).
