# OpenSearch API Contracts — Deep-Dive Reference

> **Purpose:** Exhaustive reference for every major OpenSearch API surface. Each entry includes the HTTP method, path, request/response JSON shapes, and behavioral notes relevant to system design interviews.
>
> **Companion document:** `01-interview-simulation.md` covers the design reasoning behind these APIs. This document is the "contract spec" you can reference when the interviewer asks "what does that API actually look like?"

---

## Table of Contents

1. [Document APIs](#1-document-apis)
2. [Search APIs](#2-search-apis)
3. [Index Management APIs](#3-index-management-apis)
4. [Cluster Management APIs](#4-cluster-management-apis)
5. [ISM (Index State Management) APIs](#5-ism-index-state-management-apis)
6. [Alias APIs](#6-alias-apis)
7. [Snapshot & Restore APIs](#7-snapshot--restore-apis)
8. [Ingest Pipeline APIs](#8-ingest-pipeline-apis)
9. [Security APIs (Fine-Grained Access Control)](#9-security-apis-fine-grained-access-control)
10. [OpenSearch vs Elasticsearch API Differences](#10-opensearch-vs-elasticsearch-api-differences)
11. [Interview Coverage Map](#11-interview-coverage-map)

---

## 1. Document APIs

Document APIs are the write-path primitives. Every document in OpenSearch is a JSON object stored in an index, identified by a unique `_id`.

### 1.1 Index a Document — `PUT /{index}/_doc/{id}`

Creates or replaces a document with the specified ID. If the index does not exist and dynamic index creation is enabled, the index is created automatically.

```bash
curl -X PUT "https://search-domain:443/logs-2026-02-28/_doc/doc-001" \
  -H "Content-Type: application/json" \
  -d '{
    "timestamp": "2026-02-28T10:15:30Z",
    "service": "payments",
    "level": "ERROR",
    "message": "Connection refused to downstream-db:5432",
    "host": "payments-pod-7a3f",
    "trace_id": "abc-123-def",
    "response_time_ms": 0
  }'
```

**Response (201 Created):**
```json
{
  "_index": "logs-2026-02-28",
  "_id": "doc-001",
  "_version": 1,
  "_primary_term": 1,
  "_seq_no": 42,
  "result": "created",
  "_shards": {
    "total": 2,
    "successful": 2,
    "failed": 0
  }
}
```

**Key behaviors:**
- If the `_id` already exists, the document is **replaced** (full overwrite), `result` becomes `"updated"`, and `_version` increments.
- Omit `{id}` and use `POST /{index}/_doc` to auto-generate an ID (UUID). Preferred for log analytics where you never look up by ID.
- The `_seq_no` and `_primary_term` fields enable optimistic concurrency control (see update API).
- **Near-real-time (NRT) visibility:** The document is NOT immediately searchable. It lands in an in-memory buffer (the translog provides durability). It becomes searchable after the next **refresh** (default: every 1 second). You can force visibility with `?refresh=true` (expensive) or `?refresh=wait_for` (blocks until next refresh).

### 1.2 Get a Document — `GET /{index}/_doc/{id}`

Retrieves a document by its ID. This is a **real-time** operation — it reads from the translog if the document has not yet been refreshed into a searchable segment.

```bash
curl -X GET "https://search-domain:443/logs-2026-02-28/_doc/doc-001"
```

**Response (200 OK):**
```json
{
  "_index": "logs-2026-02-28",
  "_id": "doc-001",
  "_version": 1,
  "_seq_no": 42,
  "_primary_term": 1,
  "found": true,
  "_source": {
    "timestamp": "2026-02-28T10:15:30Z",
    "service": "payments",
    "level": "ERROR",
    "message": "Connection refused to downstream-db:5432",
    "host": "payments-pod-7a3f",
    "trace_id": "abc-123-def",
    "response_time_ms": 0
  }
}
```

**Response (404 Not Found):**
```json
{
  "_index": "logs-2026-02-28",
  "_id": "doc-nonexistent",
  "found": false
}
```

**Key behaviors:**
- Unlike `_search`, `GET _doc` does NOT go through the query phase. It hashes the `_id` to determine the shard, then reads directly from that shard.
- Use `?_source_includes=field1,field2` to fetch only specific fields (reduces network transfer).
- `HEAD /{index}/_doc/{id}` returns 200/404 without the body — useful for existence checks.

### 1.3 Update a Document — `POST /{index}/_update/{id}`

Performs a partial update. Internally, OpenSearch reads the existing `_source`, merges the changes, and re-indexes the full document (Lucene segments are immutable — there is no in-place mutation).

```bash
curl -X POST "https://search-domain:443/logs-2026-02-28/_update/doc-001" \
  -H "Content-Type: application/json" \
  -d '{
    "doc": {
      "level": "WARN",
      "response_time_ms": 250
    }
  }'
```

**Response (200 OK):**
```json
{
  "_index": "logs-2026-02-28",
  "_id": "doc-001",
  "_version": 2,
  "_primary_term": 1,
  "_seq_no": 43,
  "result": "updated"
}
```

**Scripted update (atomic increment):**
```bash
curl -X POST "https://search-domain:443/logs-2026-02-28/_update/doc-001" \
  -H "Content-Type: application/json" \
  -d '{
    "script": {
      "source": "ctx._source.response_time_ms += params.delta",
      "lang": "painless",
      "params": { "delta": 50 }
    }
  }'
```

**Optimistic concurrency control:**
```bash
curl -X POST "https://search-domain:443/logs-2026-02-28/_update/doc-001?if_seq_no=42&if_primary_term=1" \
  -H "Content-Type: application/json" \
  -d '{
    "doc": { "level": "CRITICAL" }
  }'
```
If `_seq_no` or `_primary_term` do not match, the request returns `409 Conflict`.

### 1.4 Delete a Document — `DELETE /{index}/_doc/{id}`

Marks a document as deleted. Because Lucene segments are immutable, the document is not physically removed — a **tombstone** is written. The document is physically purged during the next **segment merge**.

```bash
curl -X DELETE "https://search-domain:443/logs-2026-02-28/_doc/doc-001"
```

**Response (200 OK):**
```json
{
  "_index": "logs-2026-02-28",
  "_id": "doc-001",
  "_version": 3,
  "result": "deleted",
  "_shards": {
    "total": 2,
    "successful": 2,
    "failed": 0
  }
}
```

**Delete by query (bulk deletion):**
```bash
curl -X POST "https://search-domain:443/logs-2026-02-28/_delete_by_query" \
  -H "Content-Type: application/json" \
  -d '{
    "query": {
      "range": {
        "timestamp": { "lt": "2026-02-01T00:00:00Z" }
      }
    }
  }'
```
**Response:**
```json
{
  "took": 1500,
  "timed_out": false,
  "total": 50000,
  "deleted": 50000,
  "batches": 50,
  "failures": []
}
```
> **Design note:** For time-series data, prefer dropping entire indices (e.g., `DELETE /logs-2026-02-01`) rather than `_delete_by_query`. Dropping an index frees disk instantly; delete-by-query writes tombstones that occupy space until merging.

### 1.5 Bulk API — `POST /_bulk` (Most Important Write API)

The Bulk API is the **performance-critical ingestion path**. It batches multiple index/update/delete operations into a single HTTP request, amortizing network round-trip and per-request overhead.

```bash
curl -X POST "https://search-domain:443/_bulk" \
  -H "Content-Type: application/x-ndjson" \
  -d '
{"index": {"_index": "logs-2026-02-28", "_id": "1"}}
{"timestamp": "2026-02-28T10:15:30Z", "service": "payments", "level": "ERROR", "message": "Connection refused"}
{"index": {"_index": "logs-2026-02-28", "_id": "2"}}
{"timestamp": "2026-02-28T10:15:31Z", "service": "orders", "level": "INFO", "message": "Order created"}
{"delete": {"_index": "logs-2026-02-27", "_id": "old-99"}}
{"update": {"_index": "logs-2026-02-28", "_id": "3"}}
{"doc": {"level": "WARN"}}
'
```

**Response (200 OK):**
```json
{
  "took": 30,
  "errors": false,
  "items": [
    {
      "index": {
        "_index": "logs-2026-02-28",
        "_id": "1",
        "_version": 1,
        "result": "created",
        "status": 201,
        "_shards": { "total": 2, "successful": 2, "failed": 0 }
      }
    },
    {
      "index": {
        "_index": "logs-2026-02-28",
        "_id": "2",
        "_version": 1,
        "result": "created",
        "status": 201,
        "_shards": { "total": 2, "successful": 2, "failed": 0 }
      }
    },
    {
      "delete": {
        "_index": "logs-2026-02-27",
        "_id": "old-99",
        "_version": 4,
        "result": "deleted",
        "status": 200,
        "_shards": { "total": 2, "successful": 2, "failed": 0 }
      }
    },
    {
      "update": {
        "_index": "logs-2026-02-28",
        "_id": "3",
        "_version": 2,
        "result": "updated",
        "status": 200
      }
    }
  ]
}
```

**NDJSON format rules:**
1. Each line is a separate JSON object terminated by `\n` (newline-delimited JSON).
2. Action lines (`index`, `create`, `update`, `delete`) alternate with document body lines.
3. `delete` actions have NO body line — only the action line.
4. The final line MUST end with `\n`.
5. No pretty-printing — each JSON object must be on a single line.
6. Content-Type must be `application/x-ndjson`, NOT `application/json`.

**Optimal batch sizing:**
| Metric | Recommendation | Rationale |
|---|---|---|
| **Payload size** | 5-15 MB per request | Below 5 MB under-utilizes the network; above 15 MB risks HTTP timeouts and increases memory pressure on the coordinating node |
| **Document count** | 1,000-10,000 docs per batch | Depends on document size; 1 KB docs = ~5,000 per batch for ~5 MB |
| **Concurrency** | 2-4 parallel bulk threads per node | Saturates indexing throughput without overwhelming the cluster |

**Near-real-time visibility semantics:**
- Documents indexed via `_bulk` follow the same NRT rules as single-document indexing.
- They land in the in-memory indexing buffer and translog immediately.
- They become searchable after the next refresh (default 1 second).
- The `_bulk` response returning 200 does NOT mean documents are searchable — only that they are durably written to the translog.
- If the node crashes after `_bulk` returns but before a flush, the translog replays on restart (no data loss).

**Error handling:**
- `_bulk` returns `200 OK` even if individual operations fail. You MUST check `"errors": true` and iterate over `items` to find failures.
- Common per-item errors: `400` (mapping conflict), `409` (version conflict), `429` (circuit breaker / too many requests).

---

## 2. Search APIs

Search is the read path — the most complex and feature-rich API surface. All search queries go through a **two-phase execution model**.

**Two-Phase Query/Fetch Execution:**
1. **Query phase:** The coordinating node broadcasts the query to every relevant shard (primary or replica). Each shard executes the query against its local Lucene segments and returns a **priority queue of (score, doc_id)** pairs — NOT the full documents. This is lightweight.
2. **Fetch phase:** The coordinating node merges the per-shard priority queues into a global top-N, then sends a multi-get to only the shards that hold the winning documents to retrieve their `_source`. This avoids transferring document bodies from every shard.

This two-phase design is critical for performance: if you request `size: 20` from an index with 100 shards, the query phase returns 100 x 20 = 2,000 lightweight doc IDs, then the fetch phase retrieves only 20 full documents.

### 2.1 Search — `POST /{index}/_search`

The primary search endpoint. Supports the full Query DSL.

```bash
curl -X POST "https://search-domain:443/logs-2026-02-*/_search" \
  -H "Content-Type: application/json" \
  -d '{
    "query": {
      "bool": {
        "must": [
          { "match": { "message": "connection refused" } }
        ],
        "filter": [
          { "term": { "service": "payments" } },
          { "range": { "timestamp": { "gte": "2026-02-28T00:00:00Z", "lt": "2026-02-28T23:59:59Z" } } }
        ],
        "should": [
          { "term": { "level": { "value": "ERROR", "boost": 2.0 } } }
        ],
        "must_not": [
          { "term": { "host": "canary-pod-01" } }
        ],
        "minimum_should_match": 0
      }
    },
    "aggs": {
      "errors_per_hour": {
        "date_histogram": {
          "field": "timestamp",
          "calendar_interval": "hour"
        },
        "aggs": {
          "avg_response": {
            "avg": { "field": "response_time_ms" }
          }
        }
      },
      "top_services": {
        "terms": { "field": "service", "size": 10 }
      }
    },
    "size": 20,
    "from": 0,
    "sort": [
      { "timestamp": "desc" },
      { "_score": "desc" }
    ],
    "_source": ["timestamp", "service", "level", "message"],
    "highlight": {
      "fields": {
        "message": { "fragment_size": 150, "number_of_fragments": 3 }
      }
    },
    "timeout": "10s"
  }'
```

**Response (200 OK):**
```json
{
  "took": 15,
  "timed_out": false,
  "_shards": {
    "total": 30,
    "successful": 30,
    "skipped": 10,
    "failed": 0
  },
  "hits": {
    "total": {
      "value": 4523,
      "relation": "eq"
    },
    "max_score": null,
    "hits": [
      {
        "_index": "logs-2026-02-28",
        "_id": "doc-001",
        "_score": 4.52,
        "_source": {
          "timestamp": "2026-02-28T10:15:30Z",
          "service": "payments",
          "level": "ERROR",
          "message": "Connection refused to downstream-db:5432"
        },
        "highlight": {
          "message": ["<em>Connection</em> <em>refused</em> to downstream-db:5432"]
        },
        "sort": [1709114130000, 4.52]
      }
    ]
  },
  "aggregations": {
    "errors_per_hour": {
      "buckets": [
        {
          "key_as_string": "2026-02-28T10:00:00.000Z",
          "key": 1709114400000,
          "doc_count": 342,
          "avg_response": { "value": 187.5 }
        }
      ]
    },
    "top_services": {
      "buckets": [
        { "key": "payments", "doc_count": 1205 },
        { "key": "orders", "doc_count": 890 }
      ]
    }
  }
}
```

#### Query DSL Reference

**`match` — Full-text search (uses the analyzer):**
```json
{ "match": { "message": { "query": "connection refused", "operator": "and" } } }
```
Analyzes the query text, produces tokens, looks them up in the inverted index. Default operator is `or` (any token matches); use `"operator": "and"` to require all tokens.

**`term` — Exact-value match (no analysis):**
```json
{ "term": { "service": "payments" } }
```
Looks up the exact value in the inverted index. Use for keyword fields, enums, IDs. Do NOT use `term` on `text` fields (the stored tokens are analyzed, but your query term is not — you will get unexpected misses).

**`bool` — Compound query (logical combination):**
```json
{
  "bool": {
    "must": [],
    "filter": [],
    "should": [],
    "must_not": []
  }
}
```
| Clause | Contributes to Score? | Semantics |
|---|---|---|
| `must` | Yes | AND — all clauses must match |
| `filter` | No (cached) | AND — must match, but no scoring. Results are cached in the filter cache. Always use `filter` for structured predicates. |
| `should` | Yes | OR — boosts score if matched |
| `must_not` | No (cached) | NOT — excludes matching documents |

**`range` — Numeric/date range filtering:**
```json
{ "range": { "timestamp": { "gte": "2026-02-28T00:00:00Z", "lt": "2026-03-01T00:00:00Z" } } }
```
Operators: `gt`, `gte`, `lt`, `lte`. For dates, supports date math: `"gte": "now-24h"`.

**`nested` — Query nested objects:**
```json
{
  "nested": {
    "path": "comments",
    "query": {
      "bool": {
        "must": [
          { "match": { "comments.author": "alice" } },
          { "range": { "comments.rating": { "gte": 4 } } }
        ]
      }
    }
  }
}
```
Required when fields are mapped as `"type": "nested"`. Without `nested`, object arrays are flattened and cross-object matches produce false positives (e.g., author=alice + rating=5 could match alice-rating-2 and bob-rating-5 separately).

**`function_score` — Custom relevance scoring:**
```json
{
  "function_score": {
    "query": { "match": { "title": "running shoes" } },
    "functions": [
      {
        "field_value_factor": {
          "field": "popularity",
          "factor": 1.2,
          "modifier": "sqrt",
          "missing": 1
        }
      },
      {
        "gauss": {
          "created_at": {
            "origin": "now",
            "scale": "7d",
            "decay": 0.5
          }
        }
      }
    ],
    "boost_mode": "multiply",
    "score_mode": "sum"
  }
}
```
Use for application search where BM25 alone is insufficient — e.g., boost products by sales count or recency.

### 2.2 Multi-Search — `POST /_msearch`

Executes multiple search requests in a single HTTP call. Reduces round-trip overhead for dashboards that issue many independent queries.

```bash
curl -X POST "https://search-domain:443/_msearch" \
  -H "Content-Type: application/x-ndjson" \
  -d '
{"index": "logs-2026-02-28"}
{"query": {"match": {"level": "ERROR"}}, "size": 0, "aggs": {"count": {"value_count": {"field": "_id"}}}}
{"index": "logs-2026-02-28"}
{"query": {"match": {"level": "WARN"}}, "size": 0, "aggs": {"count": {"value_count": {"field": "_id"}}}}
'
```

**Response:**
```json
{
  "took": 10,
  "responses": [
    {
      "took": 5,
      "hits": { "total": { "value": 1205, "relation": "eq" }, "hits": [] },
      "aggregations": { "count": { "value": 1205 } },
      "status": 200
    },
    {
      "took": 4,
      "hits": { "total": { "value": 3420, "relation": "eq" }, "hits": [] },
      "aggregations": { "count": { "value": 3420 } },
      "status": 200
    }
  ]
}
```

### 2.3 Count — `GET /{index}/_count`

Returns only the document count matching a query — no hits, no scoring, no fetch phase.

```bash
curl -X GET "https://search-domain:443/logs-2026-02-28/_count" \
  -H "Content-Type: application/json" \
  -d '{
    "query": {
      "term": { "level": "ERROR" }
    }
  }'
```

**Response:**
```json
{
  "count": 1205,
  "_shards": { "total": 5, "successful": 5, "skipped": 0, "failed": 0 }
}
```

### 2.4 Scroll API — `POST /_search?scroll=5m` (Deprecated Pattern)

Retrieves large result sets by maintaining a **point-in-time snapshot** of the index. Each scroll request returns a batch plus a `_scroll_id` for the next batch.

**Initial request:**
```bash
curl -X POST "https://search-domain:443/logs-2026-02-28/_search?scroll=5m" \
  -H "Content-Type: application/json" \
  -d '{
    "query": { "match_all": {} },
    "size": 1000,
    "sort": ["_doc"]
  }'
```

**Response:**
```json
{
  "_scroll_id": "DXF1ZXJ5QW5kRmV0Y2gBAAAAAAAAAD4...",
  "hits": {
    "total": { "value": 500000, "relation": "eq" },
    "hits": [ "... first 1000 docs ..." ]
  }
}
```

**Subsequent requests:**
```bash
curl -X POST "https://search-domain:443/_search/scroll" \
  -H "Content-Type: application/json" \
  -d '{
    "scroll": "5m",
    "scroll_id": "DXF1ZXJ5QW5kRmV0Y2gBAAAAAAAAAD4..."
  }'
```

**Cleanup (important to free resources):**
```bash
curl -X DELETE "https://search-domain:443/_search/scroll" \
  -H "Content-Type: application/json" \
  -d '{ "scroll_id": "DXF1ZXJ5QW5kRmV0Y2gBAAAAAAAAAD4..." }'
```

**Limitations:** Scroll contexts consume heap on every shard. Many concurrent scrolls can cause memory pressure. Prefer `search_after` for new implementations.

### 2.5 Point-in-Time (PIT) + `search_after` (Preferred Deep Pagination)

The modern replacement for scroll. A PIT is a lightweight snapshot that you combine with `search_after` for stateless pagination.

**Step 1 — Create a PIT:**
```bash
curl -X POST "https://search-domain:443/logs-2026-02-28/_search/point_in_time?keep_alive=5m"
```

**Response:**
```json
{
  "pit_id": "46ToAwMDaWR5BXV1aWQyKwZub2RlXzMAAAAAAAAAACoBYwADaWR4BXV1aWQxAgZub2RlXzEAAAAAAAAAAAEBYQADaWR5BXV1aWQyKwZub2RlXzIAAAAAAAAAAAwBYgACBHR5cGUFdXVpZA=="
}
```

**Step 2 — Search with PIT (first page):**
```bash
curl -X POST "https://search-domain:443/_search" \
  -H "Content-Type: application/json" \
  -d '{
    "size": 100,
    "query": { "match": { "level": "ERROR" } },
    "pit": {
      "id": "46ToAwMDaWR5BXV1aWQyKw...",
      "keep_alive": "5m"
    },
    "sort": [
      { "timestamp": "desc" },
      { "_shard_doc": "asc" }
    ]
  }'
```

**Step 3 — Next page (use `search_after` with last hit's sort values):**
```bash
curl -X POST "https://search-domain:443/_search" \
  -H "Content-Type: application/json" \
  -d '{
    "size": 100,
    "query": { "match": { "level": "ERROR" } },
    "pit": {
      "id": "46ToAwMDaWR5BXV1aWQyKw...",
      "keep_alive": "5m"
    },
    "sort": [
      { "timestamp": "desc" },
      { "_shard_doc": "asc" }
    ],
    "search_after": [1709114130000, 42]
  }'
```

**Step 4 — Delete PIT:**
```bash
curl -X DELETE "https://search-domain:443/_search/point_in_time" \
  -H "Content-Type: application/json" \
  -d '{ "pit_id": "46ToAwMDaWR5BXV1aWQyKw..." }'
```

**Why PIT + search_after is better than scroll:**
| Aspect | Scroll | PIT + search_after |
|---|---|---|
| State | Server-side scroll context per shard | Lightweight PIT; search_after is stateless |
| Concurrency | Each scroll holds resources | PIT is shared; search_after requests are independent |
| Sort flexibility | Fixed at scroll creation | Can change sort order between pages |
| Recommended | Legacy / re-index jobs | All new deep pagination use cases |

---

## 3. Index Management APIs

An index is a logical namespace that maps to one or more physical shards. Index management APIs control the lifecycle of indices, their mappings (schema), and their settings.

### 3.1 Create an Index — `PUT /{index}`

```bash
curl -X PUT "https://search-domain:443/logs-2026-02-28" \
  -H "Content-Type: application/json" \
  -d '{
    "settings": {
      "number_of_shards": 5,
      "number_of_replicas": 1,
      "refresh_interval": "1s",
      "index.codec": "best_compression",
      "analysis": {
        "analyzer": {
          "log_analyzer": {
            "type": "custom",
            "tokenizer": "standard",
            "filter": ["lowercase", "stop"]
          }
        }
      }
    },
    "mappings": {
      "properties": {
        "timestamp":       { "type": "date", "format": "strict_date_optional_time" },
        "service":         { "type": "keyword" },
        "level":           { "type": "keyword" },
        "message":         { "type": "text", "analyzer": "log_analyzer" },
        "host":            { "type": "keyword" },
        "trace_id":        { "type": "keyword" },
        "response_time_ms": { "type": "integer" },
        "tags":            { "type": "keyword" },
        "geo_location":    { "type": "geo_point" },
        "metadata": {
          "type": "nested",
          "properties": {
            "key":   { "type": "keyword" },
            "value": { "type": "text" }
          }
        }
      }
    }
  }'
```

**Response (200 OK):**
```json
{
  "acknowledged": true,
  "shards_acknowledged": true,
  "index": "logs-2026-02-28"
}
```

**Critical design knowledge — static vs dynamic settings:**

| Setting | Static or Dynamic | Explanation |
|---|---|---|
| `number_of_shards` | **Static** | Set at creation, CANNOT be changed. To change, you must reindex. Choose carefully. |
| `number_of_replicas` | **Dynamic** | Can be changed at any time. Increase for read throughput; decrease for write throughput. |
| `refresh_interval` | **Dynamic** | Default 1s. Set to `"-1"` to disable refresh during bulk ingest (major throughput boost). |
| `index.codec` | **Static** | `default` (LZ4) for speed, `best_compression` (zstd/deflate) for storage savings. |
| `analysis` | **Static** | Analyzers are baked into the index at creation. Changing them requires reindexing. |
| `index.routing.allocation.*` | **Dynamic** | Controls which nodes can hold this index's shards (hot/warm/cold tiering). |

### 3.2 Delete an Index — `DELETE /{index}`

```bash
curl -X DELETE "https://search-domain:443/logs-2026-02-27"
```

**Response:**
```json
{ "acknowledged": true }
```
Instantly frees all disk space for that index. For time-series data, deleting old indices is far more efficient than `_delete_by_query`.

### 3.3 Open / Close an Index — `POST /{index}/_close`, `POST /{index}/_open`

A closed index consumes no heap memory and no file descriptors — only disk. It cannot be read or written to.

```bash
# Close (frees cluster resources)
curl -X POST "https://search-domain:443/logs-2026-01-15/_close"

# Open (makes it searchable again)
curl -X POST "https://search-domain:443/logs-2026-01-15/_open"
```

**Response:**
```json
{ "acknowledged": true, "shards_acknowledged": true }
```

Use case: "cold" tier indices that are rarely queried. Close them to reduce cluster overhead; open on-demand.

### 3.4 Update Mappings — `PUT /{index}/_mapping`

Mappings are **additive only** — you can add new fields, but you CANNOT change the type of an existing field (e.g., `keyword` to `text`). To change a field type, you must reindex into a new index.

```bash
curl -X PUT "https://search-domain:443/logs-2026-02-28/_mapping" \
  -H "Content-Type: application/json" \
  -d '{
    "properties": {
      "region": { "type": "keyword" },
      "request_body": { "type": "text", "index": false }
    }
  }'
```

**Response:**
```json
{ "acknowledged": true }
```

Setting `"index": false` on `request_body` means the field is stored in `_source` but NOT indexed — you can retrieve it but not search on it. Saves disk and indexing CPU.

### 3.5 Update Index Settings — `PUT /{index}/_settings`

```bash
curl -X PUT "https://search-domain:443/logs-2026-02-28/_settings" \
  -H "Content-Type: application/json" \
  -d '{
    "index": {
      "number_of_replicas": 2,
      "refresh_interval": "30s"
    }
  }'
```

**Response:**
```json
{ "acknowledged": true }
```

**Common dynamic setting changes:**
- Before bulk ingest: `"refresh_interval": "-1"`, `"number_of_replicas": 0` (maximize throughput).
- After bulk ingest: `"refresh_interval": "1s"`, `"number_of_replicas": 1` (restore normal operations).

### 3.6 Get Index Information — `GET /{index}`

```bash
curl -X GET "https://search-domain:443/logs-2026-02-28"
```

Returns the full index definition including settings, mappings, and aliases. Useful for debugging mapping conflicts.

---

## 4. Cluster Management APIs

These APIs provide observability into cluster health, node performance, and shard allocation.

### 4.1 Cluster Health — `GET /_cluster/health`

The single most important operational API. Returns a one-word status and shard counts.

```bash
curl -X GET "https://search-domain:443/_cluster/health?wait_for_status=green&timeout=30s"
```

**Response:**
```json
{
  "cluster_name": "prod-search-cluster",
  "status": "green",
  "timed_out": false,
  "number_of_nodes": 12,
  "number_of_data_nodes": 9,
  "active_primary_shards": 450,
  "active_shards": 900,
  "relocating_shards": 0,
  "initializing_shards": 0,
  "unassigned_shards": 0,
  "delayed_unassigned_shards": 0,
  "number_of_pending_tasks": 0,
  "number_of_in_flight_fetch": 0,
  "task_max_waiting_in_queue_millis": 0,
  "active_shards_percent_as_number": 100.0
}
```

**Status meanings:**
| Status | Meaning | Action |
|---|---|---|
| **green** | All primary and replica shards are assigned | Healthy — no action |
| **yellow** | All primaries assigned, some replicas unassigned | Usually during scaling or node restart. Data is safe but redundancy is reduced. |
| **red** | Some primary shards are unassigned | **Data loss risk.** Some data is unreadable. Immediate investigation needed. |

**Per-index health:**
```bash
curl -X GET "https://search-domain:443/_cluster/health/logs-2026-02-28?level=shards"
```

### 4.2 Cluster State — `GET /_cluster/state`

Returns the full cluster state: metadata, routing table, node membership. Extremely verbose — use query parameters to filter.

```bash
# Get only metadata and routing table for a specific index
curl -X GET "https://search-domain:443/_cluster/state/metadata,routing_table/logs-2026-02-28"
```

### 4.3 Cluster Stats — `GET /_cluster/stats`

Aggregate statistics across the entire cluster.

```bash
curl -X GET "https://search-domain:443/_cluster/stats"
```

**Response (abridged):**
```json
{
  "cluster_name": "prod-search-cluster",
  "status": "green",
  "indices": {
    "count": 120,
    "shards": {
      "total": 900,
      "primaries": 450
    },
    "docs": {
      "count": 2500000000,
      "deleted": 5000000
    },
    "store": {
      "size_in_bytes": 524288000000
    }
  },
  "nodes": {
    "count": { "total": 12, "data": 9, "master": 3, "ingest": 3 },
    "jvm": {
      "max_uptime_in_millis": 86400000,
      "mem": {
        "heap_used_in_bytes": 21474836480,
        "heap_max_in_bytes": 32212254720
      }
    }
  }
}
```

### 4.4 Node Stats — `GET /_nodes/stats`

The most granular operational API. Returns per-node JVM, OS, and index-level metrics.

```bash
# Get specific metric categories
curl -X GET "https://search-domain:443/_nodes/stats/jvm,os,indices"
```

**Response (abridged, single node):**
```json
{
  "nodes": {
    "node-id-abc": {
      "name": "data-node-01",
      "host": "10.0.1.42",
      "jvm": {
        "mem": {
          "heap_used_in_bytes": 8589934592,
          "heap_max_in_bytes": 32212254720,
          "heap_used_percent": 26
        },
        "gc": {
          "collectors": {
            "young": { "collection_count": 5000, "collection_time_in_millis": 25000 },
            "old": { "collection_count": 5, "collection_time_in_millis": 1200 }
          }
        }
      },
      "os": {
        "cpu": { "percent": 45 },
        "mem": {
          "total_in_bytes": 68719476736,
          "used_in_bytes": 60129542144
        }
      },
      "fs": {
        "total": {
          "total_in_bytes": 2000000000000,
          "available_in_bytes": 800000000000
        }
      },
      "indices": {
        "indexing": {
          "index_total": 150000000,
          "index_time_in_millis": 3600000,
          "index_current": 5
        },
        "search": {
          "query_total": 5000000,
          "query_time_in_millis": 900000,
          "query_current": 2,
          "fetch_total": 5000000,
          "fetch_time_in_millis": 300000
        },
        "merges": {
          "current": 1,
          "total_time_in_millis": 7200000
        }
      }
    }
  }
}
```

**Key metrics to monitor in interviews:**
| Metric | Warning Threshold | Why It Matters |
|---|---|---|
| `heap_used_percent` | > 75% | Old GC pauses cause search latency spikes. JVM heap should be max 50% of RAM (rest for OS page cache). |
| `os.cpu.percent` | > 80% sustained | Indexing and merges are CPU-bound. |
| `fs.available` | < 20% | Disk pressure triggers shard relocation; < 5% triggers read-only mode. |
| `indexing.index_time / index_total` | Increasing ratio | Indicates merge pressure or slow disks. |
| `search.query_time / query_total` | > 100ms avg | Indicates slow queries, too many shards, or insufficient replicas. |
| `old gc collection_time` | > 5s per collection | Stop-the-world GC. Consider reducing heap pressure. |

### 4.5 Cluster Reroute — `POST /_cluster/reroute`

Manually move, cancel, or allocate shards. Used when automatic allocation is stuck.

```bash
curl -X POST "https://search-domain:443/_cluster/reroute" \
  -H "Content-Type: application/json" \
  -d '{
    "commands": [
      {
        "move": {
          "index": "logs-2026-02-28",
          "shard": 3,
          "from_node": "data-node-01",
          "to_node": "data-node-05"
        }
      },
      {
        "allocate_replica": {
          "index": "logs-2026-02-28",
          "shard": 2,
          "node": "data-node-03"
        }
      }
    ]
  }'
```

---

## 5. ISM (Index State Management) APIs

ISM is OpenSearch's plugin for automated index lifecycle management. It defines **policies** that transition indices through states (e.g., hot -> warm -> cold -> delete) based on age, size, or document count.

> **This is the OpenSearch equivalent of Elasticsearch's ILM (Index Lifecycle Management).** The policy structure is different.

### 5.1 Create/Update ISM Policy — `PUT /_plugins/_ism/policies/{policy_id}`

```bash
curl -X PUT "https://search-domain:443/_plugins/_ism/policies/logs-lifecycle" \
  -H "Content-Type: application/json" \
  -d '{
    "policy": {
      "description": "Lifecycle policy for log indices: hot -> warm -> cold -> delete",
      "default_state": "hot",
      "states": [
        {
          "name": "hot",
          "actions": [
            {
              "rollover": {
                "min_index_age": "1d",
                "min_primary_shard_size": "50gb"
              }
            }
          ],
          "transitions": [
            {
              "state_name": "warm",
              "conditions": {
                "min_index_age": "2d"
              }
            }
          ]
        },
        {
          "name": "warm",
          "actions": [
            {
              "replica_count": { "number_of_replicas": 1 }
            },
            {
              "index_priority": { "priority": 50 }
            },
            {
              "force_merge": { "max_num_segments": 1 }
            },
            {
              "allocation": {
                "require": { "data_tier": "warm" }
              }
            }
          ],
          "transitions": [
            {
              "state_name": "cold",
              "conditions": {
                "min_index_age": "14d"
              }
            }
          ]
        },
        {
          "name": "cold",
          "actions": [
            {
              "replica_count": { "number_of_replicas": 0 }
            },
            {
              "allocation": {
                "require": { "data_tier": "cold" }
              }
            },
            {
              "read_only": {}
            }
          ],
          "transitions": [
            {
              "state_name": "delete",
              "conditions": {
                "min_index_age": "90d"
              }
            }
          ]
        },
        {
          "name": "delete",
          "actions": [
            {
              "notification": {
                "destination": { "slack": { "url": "https://hooks.slack.com/..." } },
                "message_template": { "source": "Index {{ctx.index}} is being deleted." }
              }
            },
            {
              "delete": {}
            }
          ],
          "transitions": []
        }
      ],
      "ism_template": [
        {
          "index_patterns": ["logs-*"],
          "priority": 100
        }
      ]
    }
  }'
```

**Response:**
```json
{
  "_id": "logs-lifecycle",
  "_version": 1,
  "_primary_term": 1,
  "_seq_no": 0,
  "policy": {
    "policy_id": "logs-lifecycle",
    "description": "Lifecycle policy for log indices: hot -> warm -> cold -> delete",
    "default_state": "hot",
    "states": ["..."]
  }
}
```

**Policy lifecycle explained:**
| State | Age Trigger | Actions | Node Tier |
|---|---|---|---|
| **hot** | 0-2 days | Rollover at 1 day or 50 GB | Fast NVMe SSDs, high CPU |
| **warm** | 2-14 days | Force merge to 1 segment, reduce replicas | HDD or lower-IOPS storage |
| **cold** | 14-90 days | Read-only, 0 replicas | Cheapest storage, potentially S3 |
| **delete** | 90+ days | Notify then delete | N/A |

### 5.2 Get ISM Policy — `GET /_plugins/_ism/policies/{policy_id}`

```bash
curl -X GET "https://search-domain:443/_plugins/_ism/policies/logs-lifecycle"
```

### 5.3 Explain ISM State for an Index — `POST /_plugins/_ism/explain/{index}`

Shows the current ISM state of an index — which policy is attached, which state it is in, and when the next transition will occur.

```bash
curl -X POST "https://search-domain:443/_plugins/_ism/explain/logs-2026-02-25"
```

**Response:**
```json
{
  "logs-2026-02-25": {
    "index.plugins.index_state_management.policy_id": "logs-lifecycle",
    "index.opendistro.index_state_management.policy_id": "logs-lifecycle",
    "index": "logs-2026-02-25",
    "index_uuid": "abc123",
    "policy_id": "logs-lifecycle",
    "enabled": true,
    "policy_seq_no": 0,
    "policy_primary_term": 1,
    "state": { "name": "warm" },
    "action": { "name": "force_merge", "index": 0 },
    "retry_info": { "failed": false, "consumed_retries": 0 },
    "info": { "message": "Successfully completed force_merge action" }
  }
}
```

### 5.4 Delete ISM Policy — `DELETE /_plugins/_ism/policies/{policy_id}`

```bash
curl -X DELETE "https://search-domain:443/_plugins/_ism/policies/logs-lifecycle"
```

---

## 6. Alias APIs

An alias is a virtual name that points to one or more indices. Aliases enable **zero-downtime reindexing** — clients always target the alias, and you atomically swap which index it points to.

### 6.1 Create/Manage Aliases — `POST /_aliases`

The `_aliases` endpoint is atomic — all add/remove operations in a single request happen as one unit.

**Simple alias creation:**
```bash
curl -X POST "https://search-domain:443/_aliases" \
  -H "Content-Type: application/json" \
  -d '{
    "actions": [
      { "add": { "index": "logs-2026-02-28", "alias": "logs-current" } }
    ]
  }'
```

**Atomic alias swap (zero-downtime reindexing):**
```bash
curl -X POST "https://search-domain:443/_aliases" \
  -H "Content-Type: application/json" \
  -d '{
    "actions": [
      { "remove": { "index": "products-v1", "alias": "products" } },
      { "add":    { "index": "products-v2", "alias": "products" } }
    ]
  }'
```
Both actions happen atomically. At no point does the `products` alias point to zero indices or to both indices. Clients searching against `products` experience zero downtime.

**Filtered alias (multi-tenant pattern):**
```bash
curl -X POST "https://search-domain:443/_aliases" \
  -H "Content-Type: application/json" \
  -d '{
    "actions": [
      {
        "add": {
          "index": "logs-2026-02-28",
          "alias": "logs-payments",
          "filter": { "term": { "service": "payments" } },
          "is_write_index": false
        }
      }
    ]
  }'
```
Queries against `logs-payments` automatically apply the filter — the `payments` team only sees their own logs.

**Write alias (for rollover):**
```bash
curl -X POST "https://search-domain:443/_aliases" \
  -H "Content-Type: application/json" \
  -d '{
    "actions": [
      { "add": { "index": "logs-000001", "alias": "logs-write", "is_write_index": true } }
    ]
  }'
```
A write alias can point to multiple indices, but only one can be the `is_write_index`. Write requests (index, bulk) go to the write index; search requests fan out to all indices behind the alias.

### 6.2 Get Alias — `GET /_alias/{name}`

```bash
curl -X GET "https://search-domain:443/_alias/logs-current"
```

**Response:**
```json
{
  "logs-2026-02-28": {
    "aliases": {
      "logs-current": {}
    }
  }
}
```

### 6.3 Check Alias Existence — `HEAD /_alias/{name}`

```bash
curl -I "https://search-domain:443/_alias/logs-current"
# Returns 200 if exists, 404 if not
```

### 6.4 Delete Alias — `DELETE /{index}/_alias/{name}`

```bash
curl -X DELETE "https://search-domain:443/logs-2026-02-28/_alias/logs-current"
```

---

## 7. Snapshot & Restore APIs

Snapshots provide backup and recovery. They are **incremental** — after the first full snapshot, subsequent snapshots only store segments that changed. This makes frequent snapshots cheap.

### 7.1 Register a Snapshot Repository — `PUT /_snapshot/{repo}`

```bash
curl -X PUT "https://search-domain:443/_snapshot/s3-backup" \
  -H "Content-Type: application/json" \
  -d '{
    "type": "s3",
    "settings": {
      "bucket": "my-opensearch-snapshots",
      "region": "us-east-1",
      "base_path": "prod-cluster",
      "server_side_encryption": true,
      "max_snapshot_bytes_per_sec": "500mb",
      "max_restore_bytes_per_sec": "500mb"
    }
  }'
```

**Response:**
```json
{ "acknowledged": true }
```

Repository types: `s3` (most common in AWS), `fs` (shared filesystem), `azure`, `gcs`.

### 7.2 Create a Snapshot — `PUT /_snapshot/{repo}/{snapshot}`

```bash
curl -X PUT "https://search-domain:443/_snapshot/s3-backup/snapshot-2026-02-28?wait_for_completion=false" \
  -H "Content-Type: application/json" \
  -d '{
    "indices": "logs-2026-02-28,logs-2026-02-27",
    "ignore_unavailable": true,
    "include_global_state": false
  }'
```

**Response (async — snapshot started):**
```json
{ "accepted": true }
```

**Check snapshot status:**
```bash
curl -X GET "https://search-domain:443/_snapshot/s3-backup/snapshot-2026-02-28"
```

**Response:**
```json
{
  "snapshots": [
    {
      "snapshot": "snapshot-2026-02-28",
      "uuid": "abc-123",
      "version_id": 136297927,
      "version": "2.11.0",
      "indices": ["logs-2026-02-28", "logs-2026-02-27"],
      "state": "SUCCESS",
      "start_time": "2026-02-28T02:00:00.000Z",
      "end_time": "2026-02-28T02:15:30.000Z",
      "duration_in_millis": 930000,
      "shards": {
        "total": 10,
        "successful": 10,
        "failed": 0
      }
    }
  ]
}
```

**Incremental snapshot mechanics:**
- First snapshot: copies all Lucene segment files to the repository (full backup).
- Subsequent snapshots: only copies NEW segment files (segments produced since the last snapshot). Segments are immutable once written, so the delta is always "new segments only."
- A 1 TB index that receives 10 GB of new data between snapshots only transfers ~10 GB for the incremental snapshot.

### 7.3 Restore a Snapshot — `POST /_snapshot/{repo}/{snapshot}/_restore`

```bash
curl -X POST "https://search-domain:443/_snapshot/s3-backup/snapshot-2026-02-28/_restore" \
  -H "Content-Type: application/json" \
  -d '{
    "indices": "logs-2026-02-28",
    "ignore_unavailable": true,
    "include_global_state": false,
    "rename_pattern": "logs-(.+)",
    "rename_replacement": "restored-logs-$1",
    "index_settings": {
      "index.number_of_replicas": 0
    }
  }'
```

**Response:**
```json
{
  "accepted": true
}
```

**Key behaviors:**
- You cannot restore into an existing open index with the same name. Either close/delete the target index first, or use `rename_pattern`/`rename_replacement` to restore into a differently-named index.
- Setting `"number_of_replicas": 0` during restore speeds up recovery; increase replicas after restore completes.
- Restore is a shard-level operation — each shard is recovered independently, allowing parallelism.

### 7.4 Delete a Snapshot — `DELETE /_snapshot/{repo}/{snapshot}`

```bash
curl -X DELETE "https://search-domain:443/_snapshot/s3-backup/snapshot-2026-02-25"
```

Only deletes segment files that are not referenced by any other snapshot in the same repository. Safe to call without breaking other snapshots.

---

## 8. Ingest Pipeline APIs

Ingest pipelines apply a sequence of **processors** to documents before indexing. They transform, enrich, or filter documents on the coordinating or ingest node — no need for external ETL.

### 8.1 Create/Update a Pipeline — `PUT /_ingest/pipeline/{id}`

```bash
curl -X PUT "https://search-domain:443/_ingest/pipeline/logs-pipeline" \
  -H "Content-Type: application/json" \
  -d '{
    "description": "Parse and enrich application log documents",
    "processors": [
      {
        "grok": {
          "field": "raw_message",
          "patterns": ["%{TIMESTAMP_ISO8601:timestamp} %{LOGLEVEL:level} \\[%{DATA:thread}\\] %{GREEDYDATA:message}"],
          "ignore_failure": true
        }
      },
      {
        "date": {
          "field": "timestamp",
          "formats": ["ISO8601", "yyyy-MM-dd HH:mm:ss,SSS"],
          "target_field": "@timestamp",
          "timezone": "UTC"
        }
      },
      {
        "rename": {
          "field": "host.name",
          "target_field": "hostname",
          "ignore_missing": true
        }
      },
      {
        "script": {
          "lang": "painless",
          "source": "ctx.message_length = ctx.message.length(); if (ctx.level == 'ERROR') { ctx.priority = 'high'; } else { ctx.priority = 'normal'; }"
        }
      },
      {
        "remove": {
          "field": "raw_message",
          "ignore_missing": true
        }
      },
      {
        "set": {
          "field": "ingest_timestamp",
          "value": "{{_ingest.timestamp}}"
        }
      },
      {
        "lowercase": {
          "field": "level"
        }
      },
      {
        "pipeline": {
          "name": "geoip-enrichment",
          "ignore_failure": true
        }
      }
    ],
    "on_failure": [
      {
        "set": {
          "field": "ingest_error",
          "value": "{{_ingest.on_failure_message}}"
        }
      },
      {
        "set": {
          "field": "ingest_failed_processor",
          "value": "{{_ingest.on_failure_processor_type}}"
        }
      }
    ]
  }'
```

**Response:**
```json
{ "acknowledged": true }
```

**Common processors:**
| Processor | Purpose |
|---|---|
| `grok` | Parse unstructured text into structured fields using regex patterns (Logstash syntax) |
| `date` | Parse date strings into a proper date field |
| `rename` | Rename a field |
| `remove` | Remove a field |
| `set` | Set a field to a static or template value |
| `script` | Execute arbitrary Painless script for complex transforms |
| `lowercase` / `uppercase` | Normalize field values |
| `split` | Split a string field into an array |
| `json` | Parse a JSON string into a JSON object |
| `pipeline` | Call another pipeline (composition) |
| `dissect` | Simpler/faster alternative to grok for delimiter-based parsing |

### 8.2 Use a Pipeline When Indexing

**Per-request:**
```bash
curl -X PUT "https://search-domain:443/logs-2026-02-28/_doc/1?pipeline=logs-pipeline" \
  -H "Content-Type: application/json" \
  -d '{ "raw_message": "2026-02-28T10:15:30Z ERROR [main] Connection refused" }'
```

**Default pipeline on the index:**
```bash
curl -X PUT "https://search-domain:443/logs-2026-02-28/_settings" \
  -H "Content-Type: application/json" \
  -d '{ "index.default_pipeline": "logs-pipeline" }'
```

### 8.3 Simulate a Pipeline — `POST /_ingest/pipeline/_simulate`

Test a pipeline against sample documents without actually indexing.

```bash
curl -X POST "https://search-domain:443/_ingest/pipeline/logs-pipeline/_simulate" \
  -H "Content-Type: application/json" \
  -d '{
    "docs": [
      {
        "_source": {
          "raw_message": "2026-02-28T10:15:30Z ERROR [main] Connection refused to db:5432"
        }
      }
    ]
  }'
```

**Response:**
```json
{
  "docs": [
    {
      "doc": {
        "_source": {
          "@timestamp": "2026-02-28T10:15:30.000Z",
          "level": "error",
          "thread": "main",
          "message": "Connection refused to db:5432",
          "message_length": 31,
          "priority": "high",
          "ingest_timestamp": "2026-02-28T12:00:00.000Z"
        }
      }
    }
  ]
}
```

### 8.4 Get/Delete a Pipeline

```bash
# Get
curl -X GET "https://search-domain:443/_ingest/pipeline/logs-pipeline"

# Delete
curl -X DELETE "https://search-domain:443/_ingest/pipeline/logs-pipeline"
```

---

## 9. Security APIs (Fine-Grained Access Control)

OpenSearch provides a security plugin (originally Open Distro Security) with role-based access control at three granularity levels: index, document, and field.

### 9.1 Manage Roles — `PUT /_plugins/_security/api/roles/{role}`

```bash
curl -X PUT "https://search-domain:443/_plugins/_security/api/roles/logs-reader" \
  -H "Content-Type: application/json" \
  -d '{
    "cluster_permissions": [
      "cluster_composite_ops_ro"
    ],
    "index_permissions": [
      {
        "index_patterns": ["logs-*"],
        "allowed_actions": [
          "read",
          "search"
        ],
        "dls": "{\"bool\": {\"must\": [{\"term\": {\"service\": \"payments\"}}]}}",
        "fls": ["~secret_field", "~internal_notes"],
        "masked_fields": ["trace_id"]
      }
    ],
    "tenant_permissions": [
      {
        "tenant_patterns": ["payments-tenant"],
        "allowed_actions": ["kibana_all_read"]
      }
    ]
  }'
```

**Fine-Grained Access Control (FGAC) levels:**

| Level | Field in Role | What It Controls | Example |
|---|---|---|---|
| **Index-level** | `index_patterns` + `allowed_actions` | Which indices the role can access and what operations are allowed | `logs-*` with `read` only |
| **Document-level (DLS)** | `dls` | A query filter applied to every search — the user only sees matching documents | Only documents where `service=payments` |
| **Field-level (FLS)** | `fls` | Which fields are visible. Prefix with `~` to exclude, or list only included fields. | Hide `secret_field` and `internal_notes` |
| **Field masking** | `masked_fields` | Fields whose values are hashed (one-way) in search results | `trace_id` appears as a hash |

### 9.2 Manage Role Mappings — `PUT /_plugins/_security/api/rolesmapping/{role}`

Maps backend roles (IAM roles, LDAP groups, SAML attributes) to OpenSearch security roles.

```bash
curl -X PUT "https://search-domain:443/_plugins/_security/api/rolesmapping/logs-reader" \
  -H "Content-Type: application/json" \
  -d '{
    "backend_roles": [
      "arn:aws:iam::123456789:role/payments-team-role"
    ],
    "users": [
      "alice",
      "bob"
    ],
    "hosts": []
  }'
```

**Response:**
```json
{
  "status": "CREATED",
  "message": "'logs-reader' created."
}
```

### 9.3 Manage Internal Users — `PUT /_plugins/_security/api/internalusers/{username}`

```bash
curl -X PUT "https://search-domain:443/_plugins/_security/api/internalusers/alice" \
  -H "Content-Type: application/json" \
  -d '{
    "password": "StrongP@ssw0rd!",
    "backend_roles": ["payments-team"],
    "attributes": {
      "department": "engineering",
      "team": "payments"
    }
  }'
```

### 9.4 Get Security Configuration

```bash
# List all roles
curl -X GET "https://search-domain:443/_plugins/_security/api/roles"

# Get a specific role
curl -X GET "https://search-domain:443/_plugins/_security/api/roles/logs-reader"

# List all role mappings
curl -X GET "https://search-domain:443/_plugins/_security/api/rolesmapping"

# Get account details for current user
curl -X GET "https://search-domain:443/_plugins/_security/api/account"

# Auth info (who am I, what roles do I have)
curl -X GET "https://search-domain:443/_plugins/_security/authinfo"
```

---

## 10. OpenSearch vs Elasticsearch API Differences

OpenSearch forked from Elasticsearch 7.10 (Apache 2.0). The core APIs are largely compatible, but plugin-specific APIs diverge significantly.

| Feature | OpenSearch | Elasticsearch | Notes |
|---|---|---|---|
| **Index Lifecycle** | `_plugins/_ism/policies` (ISM) | `_ilm/policy` (ILM) | Different JSON structure. ISM uses explicit state machines; ILM uses phase-based config. |
| **Security** | `_plugins/_security/api/*` | `_xpack/security/*` | OpenSearch security is free (was Open Distro). Elasticsearch security requires a paid license (X-Pack). |
| **Alerting** | `_plugins/_alerting/*` | `_watcher/*` | OpenSearch Alerting has monitors, triggers, destinations. Elasticsearch uses Watcher with watches, actions. |
| **Anomaly Detection** | `_plugins/_anomaly_detection/*` | `_ml/*` (Machine Learning) | OpenSearch AD is free. Elasticsearch ML requires a paid Platinum license. |
| **SQL** | `_plugins/_sql` | `_sql` (X-Pack SQL) | Both support SQL over HTTP. OpenSearch also supports PPL (Piped Processing Language). |
| **k-NN (Vector Search)** | `_plugins/_knn/*` | Native `dense_vector` field | OpenSearch uses NMSLIB/Faiss libraries. Elasticsearch has native support since 8.x. |
| **Notifications** | `_plugins/_notifications/*` | Watcher actions | OpenSearch has a dedicated Notifications plugin. |
| **Trace Analytics** | `_plugins/_trace_analytics/*` | APM (Elastic APM) | OpenSearch supports OpenTelemetry natively. |
| **API Compatibility** | Supports `_opendistro` prefix (deprecated) and `_plugins` prefix | `_xpack` prefix | OpenSearch migrated from `_opendistro` to `_plugins` in version 1.x. |
| **License** | Apache 2.0 (fully open source) | SSPL / Elastic License 2.0 | OpenSearch is free for all features. Elasticsearch gates many features behind paid tiers. |
| **Version Numbering** | 1.x, 2.x (forked from ES 7.10) | 7.x, 8.x | OpenSearch 2.x is roughly equivalent to Elasticsearch 7.10 + additional features. Not version-compatible with ES 8.x. |

**Compatibility layer:** OpenSearch accepts both `Content-Type: application/json` and `application/x-ndjson`. It also supports the `_opendistro` API prefix for backward compatibility with Open Distro for Elasticsearch, though this is deprecated in favor of `_plugins`.

---

## 11. Interview Coverage Map

This section maps each API group to its relevance in the interview simulation (`01-interview-simulation.md`).

### Covered in Interview Simulation (Phase 3 — API Design)

These are the APIs you MUST be able to whiteboard from memory and explain the design reasoning for:

| API | Interview Relevance | Phase |
|---|---|---|
| `PUT /{index}/_doc/{id}` | Core write path — explain translog, NRT refresh, shard routing | Phase 3 (API Design), Phase 5 (Write Path) |
| `POST /_bulk` | Performance-critical ingestion — explain NDJSON, batch sizing, error handling | Phase 3 (API Design), Phase 5 (Write Path) |
| `POST /{index}/_search` | Core read path — explain Query DSL, bool query, two-phase query/fetch | Phase 3 (API Design), Phase 6 (Read Path) |
| `PUT /{index}` (create index) | Explain shard count static, replica dynamic, mapping types | Phase 4 (Data Model) |
| `GET /_cluster/health` | Operational understanding — green/yellow/red, shard allocation | Phase 8 (Operational Maturity) |
| `POST /_aliases` (atomic swap) | Zero-downtime reindexing pattern | Phase 8 (Operational Maturity) |
| ISM policies | Hot/warm/cold/delete lifecycle for cost optimization | Phase 8 (Operational Maturity) |

### Reference-Only (Know They Exist, Depth Not Required)

These APIs add depth if the interviewer probes, but are not expected in a 60-minute design round:

| API | When to Mention |
|---|---|
| `POST /_update/{id}` | If asked about update semantics — explain read-modify-reindex internally |
| `_delete_by_query` | If asked about data deletion — pivot to "prefer dropping indices for time-series" |
| PIT + `search_after` | If asked about deep pagination — shows you know the modern replacement for scroll |
| `_msearch` | If asked about dashboard performance — batching multiple queries |
| `GET /_nodes/stats` | If asked about monitoring — mention JVM heap, GC, disk metrics |
| `POST /_cluster/reroute` | If asked about stuck shard allocation or rebalancing |
| Snapshot/Restore | If asked about disaster recovery or cross-region replication |
| Ingest Pipelines | If asked about data transformation — "we can do ETL at ingest time" |
| Security (FGAC) | If asked about multi-tenancy — document-level security, field-level security |
| `function_score` | If asked about ranking in application search — custom scoring |
| `nested` query | If asked about complex document structures — avoiding false positives in object arrays |
| Scroll API | Mention only to say "deprecated in favor of PIT + search_after" |

### Interview Strategy

1. **Phase 3 (API Design):** Write 3 APIs on the whiteboard — single doc index, bulk index, search with bool query + aggregation. This demonstrates write path, read path, and Query DSL in under 3 minutes.
2. **If probed on pagination:** Jump to PIT + `search_after` and explain why it replaced scroll.
3. **If probed on operations:** Mention cluster health (green/yellow/red), ISM lifecycle (hot/warm/cold/delete), and alias-based zero-downtime reindexing.
4. **If probed on security:** Mention FGAC with document-level and field-level security — this is a differentiator from basic role-based access.

---

> **Navigation:** [01-interview-simulation.md](./01-interview-simulation.md) | **02-api-contracts.md** (this document)
