# OpenSearch Performance Tuning and Scaling

This document covers the internals of tuning an OpenSearch cluster for production workloads. Understanding these mechanics is critical for system design interviews where the interviewer pushes on "how would you handle 10x traffic?" or "your p99 search latency spiked -- what do you check?"

---

## JVM Heap Sizing

OpenSearch runs on the JVM. Heap sizing is the single most impactful knob and also the easiest to get wrong.

### The 50% Rule

| Parameter | Recommendation | Why |
|-----------|---------------|-----|
| Heap size | 50% of instance RAM | Other 50% goes to OS filesystem cache |
| Max heap | 32 GB | Compressed ordinary object pointers (oops) limit |
| Min heap | Equal to max heap | Avoid costly heap resizing at runtime |

The reasoning behind the 32 GB ceiling:

- The JVM uses **compressed ordinary object pointers (oops)** when heap is at or below ~32 GB. Each pointer is 4 bytes.
- The moment heap exceeds ~32 GB, the JVM switches to uncompressed pointers: each pointer becomes 8 bytes.
- A 48 GB heap often has **less usable memory** than a 32 GB heap because the pointer overhead eats the extra space.
- If you need more memory, scale horizontally (more nodes) rather than giving one node 64 GB heap.

### Why the Other 50% Matters

Lucene (the search library underneath OpenSearch) stores its index segments on disk and relies heavily on the OS page cache via `mmap`. The OS filesystem cache is what makes "warm" queries fast -- segments that were recently read stay in RAM without consuming JVM heap. Starving the OS of RAM to give more to the JVM is counterproductive.

```
Instance with 64 GB RAM:
  - JVM heap: 32 GB  (OpenSearch objects, field data, aggregation buffers)
  - OS cache:  32 GB  (Lucene segment files, mmap'd, managed by kernel)
```

### Garbage Collection

| Setting | Default | Notes |
|---------|---------|-------|
| GC algorithm | G1GC | Default since OpenSearch 1.x / ES 7.x |
| `-Xms` / `-Xmx` | Set via `jvm.options` or `OPENSEARCH_JAVA_OPTS` | Always set equal |
| GC logging | Enabled by default | Check `gc.log` in the logs directory |

Monitor GC via:
```
GET _nodes/stats/jvm
```

Key fields to watch:
- `jvm.gc.collectors.young.collection_time_in_millis` -- young gen pauses (should be <50ms each)
- `jvm.gc.collectors.old.collection_time_in_millis` -- old gen pauses (should be rare and <1s)
- `jvm.mem.heap_used_percent` -- sustained >85% is a red flag

### Circuit Breakers

Circuit breakers prevent a single expensive query from crashing the node with an OutOfMemoryError. When a breaker trips, the request is rejected with a 429 -- which is far better than a dead node.

| Breaker | Setting | Default | Purpose |
|---------|---------|---------|---------|
| Total | `indices.breaker.total.limit` | 95% of heap | Aggregate limit across all breakers |
| Field data | `indices.breaker.fielddata.limit` | 40% of heap | Prevents loading too many field values for sorting/aggs |
| Request | `indices.breaker.request.limit` | 60% of heap | Limits per-request data structures (e.g., buckets in aggregation) |
| In-flight requests | `network.breaker.inflight_requests.limit` | 100% of heap | Limits total size of in-transit HTTP requests |

If you see frequent breaker trips, the solution is **not** to raise the limits. Instead:
- Reduce field data usage (use `keyword` not `text` for aggregations)
- Paginate aggregations with composite aggs instead of massive terms aggs
- Add more nodes to spread the data

---

## Search Performance

### Reduce Shard Count

Every search hits every shard (unless you use custom routing). The coordinating node performs a scatter-gather:

```
Client -> Coordinating Node -> [Shard 1, Shard 2, ..., Shard N] -> merge results -> Client
```

Each shard is a separate Lucene index. More shards = more fan-out = more network hops = higher tail latency. A single shard can handle millions of documents efficiently.

| Guideline | Value |
|-----------|-------|
| Target shard size | 10-50 GB |
| Max shards per node | ~600 (varies by node size) |
| Shards per index | Start with 1, increase only when a single shard is too large |

### Filter Context vs Query Context

```json
// SLOW: query context -- computes relevance score for every matching doc
{
  "query": {
    "bool": {
      "must": [
        { "match": { "status": "active" } }
      ]
    }
  }
}

// FAST: filter context -- binary yes/no, cached in bitset, no scoring
{
  "query": {
    "bool": {
      "filter": [
        { "term": { "status": "active" } }
      ]
    }
  }
}
```

Filter context advantages:
- Results are cached in a bitset (subsequent queries are near-instant)
- No scoring computation (cheaper CPU)
- Automatically used for `range`, `term`, `exists` inside `filter`

**Rule of thumb**: if you do not need relevance ranking for a clause, put it in `filter`.

### Avoid Deep Pagination

`from + size` pagination forces OpenSearch to score and sort `from + size` documents on every shard, then discard `from` of them. At `from=10000, size=10`, each shard must produce 10,010 results.

| Method | Use Case | Limitation |
|--------|----------|------------|
| `from + size` | First few pages (UI) | Default limit 10,000; performance degrades linearly |
| `search_after` | Efficient deep pagination | Requires a sort value from the previous page; no random page jumps |
| Point-in-Time (PIT) + `search_after` | Consistent pagination over changing data | PIT keeps a snapshot; combine with `search_after` for stable paging |
| Scroll API | Batch export (deprecated for search) | Keeps search context open; ties up resources; avoid for user-facing |

### Keyword Fields for Exact Match

Text fields go through analysis (tokenize, lowercase, stemming). If you are filtering on `status = "active"`, the field must be `keyword` type, not `text`. Querying a `text` field for exact match works but is slower and prevents filter caching.

### Custom Routing

By default, a document's shard is `hash(_id) % num_shards`. Custom routing lets you colocate related documents:

```
PUT my-index/_doc/1?routing=tenant_42
{ "tenant_id": "tenant_42", "message": "..." }

GET my-index/_search?routing=tenant_42
{ "query": { "match": { "message": "hello" } } }
```

With routing, the query hits only the shard(s) for `tenant_42` instead of all shards. This is extremely effective for multi-tenant systems.

### Cache Warming

OpenSearch maintains several caches:
- **Node query cache**: caches filter results (LRU, 10% of heap by default)
- **Shard request cache**: caches full results of `size=0` aggregation queries
- **Filesystem cache**: OS-level, warms naturally as segments are read

You can proactively warm caches by running common queries after a node restart or a new index rollover. There is no built-in "warmup" API -- you just run the queries.

---

## Indexing Performance

### Bulk API Sizing

Never index documents one at a time. The `_bulk` API amortizes the overhead of HTTP, routing, and translog fsync across many documents.

| Parameter | Recommendation | Why |
|-----------|---------------|-----|
| Batch size | 5-15 MB per request | Too small = overhead per request. Too large = high memory pressure |
| Documents per batch | Varies (typically 1,000-10,000) | Depends on document size; target the MB range |
| Concurrent bulk threads | 3-5 per data node | Saturate I/O without overwhelming the merge scheduler |

### Refresh Interval

A "refresh" makes newly indexed documents visible to searches. Each refresh creates a new Lucene segment.

| Setting | Default | Bulk Load | Near Real-Time |
|---------|---------|-----------|----------------|
| `index.refresh_interval` | `1s` | `30s` or `-1` (disable) | `1s` |

During bulk ingestion, set `refresh_interval: -1` and call `_refresh` manually when done. Each refresh creates a segment that must later be merged, so frequent refreshes during bulk load waste I/O.

```
PUT my-index/_settings
{ "index": { "refresh_interval": "-1" } }

// ... bulk index millions of documents ...

POST my-index/_refresh

PUT my-index/_settings
{ "index": { "refresh_interval": "1s" } }
```

### Translog Settings

The translog is OpenSearch's write-ahead log. Every index operation is written to the translog before acknowledgment. A flush writes a new Lucene commit point and clears the translog.

| Setting | Default | Tuning for Throughput |
|---------|---------|----------------------|
| `index.translog.durability` | `request` (fsync per request) | `async` (fsync every 5s -- risk: lose up to 5s of data on crash) |
| `index.translog.flush_threshold_size` | `512mb` | `1gb` or `2gb` (fewer flushes, larger segments) |

### Auto-Generated IDs

When you supply your own `_id`, OpenSearch must check if a document with that ID already exists (a version lookup). Auto-generated IDs skip this check.

```
// Slower: OpenSearch must check if doc "abc123" exists
PUT my-index/_doc/abc123
{ "message": "hello" }

// Faster: no version check needed
POST my-index/_doc
{ "message": "hello" }
```

Use auto-generated IDs when you do not need to upsert or deduplicate.

### Disable Replicas During Bulk Load

Replicas double the indexing work. During an initial bulk load:

```
PUT my-index/_settings
{ "index": { "number_of_replicas": 0 } }

// ... bulk load ...

PUT my-index/_settings
{ "index": { "number_of_replicas": 1 } }
```

When replicas are re-enabled, OpenSearch copies the primary shards to the replica nodes. This is a single large transfer, which is more efficient than replicating every individual bulk request.

### Summary: Bulk Load Recipe

1. Create index with `number_of_replicas: 0`
2. Set `refresh_interval: -1`
3. Optionally set `translog.durability: async`
4. Index using `_bulk` with 5-15 MB batches, 3-5 threads per data node
5. Use auto-generated `_id` if possible
6. When done: call `_refresh`, restore `refresh_interval: 1s`, set `number_of_replicas: 1`

---

## Scaling Strategies

### Vertical vs Horizontal

| Dimension | Vertical Scaling | Horizontal Scaling |
|-----------|-----------------|-------------------|
| Mechanism | Larger instance (more CPU, RAM, disk) | More nodes in the cluster |
| Heap limit | Capped at 32 GB useful heap | Each new node adds another 32 GB heap |
| Downtime | Requires node restart (rolling) | Add nodes live, shards rebalance automatically |
| Cost curve | Exponential (2x instance = >2x cost) | Linear (2x nodes = ~2x cost) |
| Ceiling | Instance type limits (e.g., r6g.16xlarge) | Hundreds of nodes |

### Read Scaling

Every replica shard can independently serve search requests. Adding replicas is the simplest way to scale reads.

```
Cluster: 3 data nodes, 1 index with 3 primary shards, 1 replica
  - Total shard copies: 6 (3 primary + 3 replica)
  - Each search request can be served by any copy
  - Doubling replicas to 2 = 9 shard copies = ~50% more search throughput
```

Trade-off: more replicas = more disk usage and more indexing work (each doc is written to primary + all replicas).

### Write Scaling

The number of primary shards is set at index creation and **cannot be changed** (without reindexing or using the split API). To scale writes:

- **More primary shards**: each primary shard indexes independently; more shards = more parallel writes
- **More data nodes**: shards are distributed across nodes; more nodes = each node handles fewer shards
- **Time-based indices**: instead of one giant index, use `logs-2026.02.28` daily indices. Each new index can be sized appropriately.

### Tiered Storage (AWS Managed OpenSearch)

| Tier | Storage | Use Case | Cost |
|------|---------|----------|------|
| Hot | SSD (EBS gp3/io2) | Active indexing and search (last 1-7 days) | $$$$ |
| UltraWarm | S3-backed managed storage | Read-mostly data (7-30 days) | $$ |
| Cold | S3 (detached) | Rarely accessed, must be attached before querying | $ |

Data moves through tiers via Index State Management (ISM) policies:

```
Hot (0-7 days) --> UltraWarm (7-30 days) --> Cold (30-90 days) --> Delete
```

UltraWarm nodes use a different instance type (`ultrawarm1.medium.search`) and cannot accept writes -- only reads.

---

## Capacity Planning

### Storage Formula

```
total_storage = source_data_per_day
              x retention_days
              x (1 + number_of_replicas)
              x 1.1   (10% overhead for OS, segment merges, metadata)
```

**Example**: 50 GB/day, 30-day retention, 1 replica:

```
50 x 30 x 2 x 1.1 = 3,300 GB total storage needed
```

### Shard Count Formula

```
number_of_shards = ceil(total_storage / target_shard_size)
```

With a target shard size of 30 GB:

```
ceil(3300 / 30) = 110 shards
```

If each data node can handle ~20 shards comfortably (depends on instance size), you need at least 6 data nodes (110 / 20).

### Instance Sizing Rule of Thumb

| Component | Guideline |
|-----------|-----------|
| RAM | Each data node should have enough RAM so that heap (50%) covers working set of field data + aggregation buffers |
| CPU | 1 vCPU per 2-5 active shards (depends heavily on query complexity) |
| Disk | EBS gp3 for most workloads; io2 for write-heavy (>10K docs/sec per node) |
| Network | Not usually the bottleneck; becomes one with cross-AZ replication |

---

## Monitoring Key Metrics

### Critical Metrics Table

| Metric | Source | Healthy | Warning | Critical |
|--------|--------|---------|---------|----------|
| Cluster health | `GET _cluster/health` | `green` | `yellow` (unassigned replicas) | `red` (unassigned primaries) |
| JVM heap used | `_nodes/stats/jvm` | <75% | 75-85% | >85% sustained |
| Search latency p99 | `_nodes/stats/indices/search` | <200ms | 200-500ms | >1s |
| Indexing rate | `_nodes/stats/indices/indexing` | Stable | Declining | Rejected (429s) |
| Merge time | `_nodes/stats/indices/merges` | Low relative to indexing | Merges dominating I/O | Merges blocking indexing |
| GC pause (old gen) | `_nodes/stats/jvm` | <200ms, rare | 200ms-1s | >1s or frequent |
| Disk watermark | Disk usage on data nodes | <85% | 85-90% (low watermark) | >95% (flood stage: index goes read-only) |
| Thread pool rejections | `_nodes/stats/thread_pool` | 0 | Occasional | Sustained rejections on `search` or `write` pools |

### Disk Watermarks

| Watermark | Default | Behavior |
|-----------|---------|----------|
| Low | 85% disk used | No new shards allocated to this node |
| High | 90% disk used | OpenSearch tries to relocate shards off this node |
| Flood stage | 95% disk used | All indices on this node become **read-only** (`index.blocks.read_only_allow_delete`) |

When flood stage triggers, you must free disk space and then manually remove the block:

```
PUT _all/_settings
{ "index.blocks.read_only_allow_delete": null }
```

---

## Common Performance Anti-Patterns

### 1. Leading Wildcard Queries

```json
// Terrible: scans every term in the inverted index
{ "query": { "wildcard": { "message": "*timeout*" } } }

// Better: use match if the field is analyzed
{ "query": { "match": { "message": "timeout" } } }

// If you truly need substring search: use n-gram tokenizer at index time
```

Leading wildcards (`*foo`) bypass the inverted index entirely and perform a linear scan of all terms. For a field with millions of unique terms, this is catastrophic.

### 2. High-Cardinality Terms Aggregations

```json
// Dangerous: if user_id has millions of unique values
{
  "aggs": {
    "top_users": {
      "terms": { "field": "user_id", "size": 100000 }
    }
  }
}
```

This loads all unique values into heap for each shard, sorts them, and merges across shards. Use `composite` aggregation for paginated iteration, or pre-aggregate in a separate summary index.

### 3. Shard Explosion

Each shard consumes resources even when empty:
- ~10-50 MB of heap per shard
- File descriptors for each segment
- Thread pool resources for shard-level operations

| Pattern | Shards Created | Problem |
|---------|---------------|---------|
| Daily index per customer (1,000 customers x 365 days) | 365,000 | Cluster master overwhelmed |
| Default 5 primary shards on every index | 5x more than needed | Scatter-gather overhead |

Solutions:
- Use `_rollover` with size/age conditions instead of calendar-based indices
- Use index templates with 1 primary shard as default
- Use data streams (built-in rollover)

### 4. Dynamic Mapping Causing Field Explosion

If dynamic mapping is enabled (default), every new JSON key creates a new field in the mapping. A field explosion (thousands of unique field names) causes:
- Massive cluster state (shipped to every node)
- Slow query compilation
- High heap usage for field metadata

Set `dynamic: strict` or `dynamic: false` on indices where the schema is known.

### 5. Large _source Retrieval

If documents are large (e.g., 100 KB each) and you only need a few fields:

```json
// Bad: retrieves entire _source (100 KB per doc x 1000 results = 100 MB)
{ "query": { "match_all": {} }, "size": 1000 }

// Better: fetch only needed fields
{
  "query": { "match_all": {} },
  "size": 1000,
  "_source": ["timestamp", "status", "user_id"]
}

// Even better for pure filtering/aggregation: disable _source
{
  "query": { "match_all": {} },
  "size": 0,
  "aggs": { ... }
}
```

---

## Contrast: OpenSearch vs Columnar Stores (ClickHouse / Druid)

This comparison matters in interviews when the interviewer asks "why not just use ClickHouse?" or "when would you pick a different engine?"

### Architecture Comparison

| Dimension | OpenSearch | ClickHouse / Druid |
|-----------|-----------|-------------------|
| Storage layout | Inverted index (row-oriented per field) | Columnar (each column stored separately) |
| Primary strength | Full-text search + filtering | Aggregations over large datasets |
| Compression | Moderate (stored fields are row-based) | Excellent (columnar = high compression ratios) |
| Aggregation speed | Good for moderate cardinality | 10-100x faster for pure scan-and-aggregate |
| Full-text search | Native (BM25 scoring, analyzers, fuzzy) | Not supported or bolted on |
| Real-time ingestion | Yes (refresh_interval = 1s) | ClickHouse: yes. Druid: yes (with ingestion lag) |
| Exact match / filter | Fast (inverted index) | Fast (bitmap/bloom indexes) |
| Join support | None (denormalize at index time) | Limited (ClickHouse has JOINs; Druid does not) |

### When to Use What

| Workload | Best Engine | Why |
|----------|-------------|-----|
| Log search ("find me errors containing 'NullPointer' in the last hour") | OpenSearch | Full-text search on unstructured log messages |
| Dashboard aggregations ("count of 5xx errors per service, last 7 days") | ClickHouse / Druid | Pure aggregation; columnar is 10-100x faster |
| Observability platform (search + dashboards) | OpenSearch for search, ClickHouse for metrics | Hybrid: each engine does what it is best at |
| E-commerce product search (faceted, relevance-ranked) | OpenSearch | Relevance scoring, facets, suggestions |
| Ad-hoc analytics on event data (no text search) | ClickHouse | Column scans with high compression, SQL interface |

### The Key Interview Point

OpenSearch's inverted index makes it unbeatable for **finding documents that contain specific terms**. Columnar stores are unbeatable for **aggregating numeric columns across billions of rows**. If your workload is pure analytics (counts, sums, percentiles, GROUP BY) with no text search, a columnar store will be 10-100x faster and use less storage due to compression. If you need both text search and analytics, OpenSearch is the pragmatic single-engine choice -- or use a dual-engine architecture with OpenSearch for search and ClickHouse/Druid for dashboards.

---

## Quick Reference: Tuning Parameters

| Category | Parameter | Default | Recommended (Production) |
|----------|-----------|---------|--------------------------|
| JVM | `-Xms` / `-Xmx` | 1g | 50% of RAM, max 32g |
| Refresh | `index.refresh_interval` | `1s` | `30s` for write-heavy, `1s` for search-heavy |
| Translog | `index.translog.flush_threshold_size` | `512mb` | `1gb`-`2gb` for write-heavy |
| Translog | `index.translog.durability` | `request` | `async` if you can tolerate ~5s data loss |
| Replicas | `index.number_of_replicas` | `1` | `0` during bulk load, `1`-`2` in production |
| Merge | `index.merge.scheduler.max_thread_count` | `Math.max(1, cores/2)` | Reduce on spinning disks to `1` |
| Search | `index.max_result_window` | `10000` | Keep default; use `search_after` beyond |
| Circuit breaker | `indices.breaker.total.limit` | `95%` heap | Keep default; fix queries, not limits |
| Disk watermark (low) | `cluster.routing.allocation.disk.watermark.low` | `85%` | Adjust based on disk provisioning |
| Thread pool | `thread_pool.write.queue_size` | `10000` | Monitor rejections; increase if bursty |
