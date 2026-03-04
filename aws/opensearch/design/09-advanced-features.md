# OpenSearch Advanced Features — Deep Dive

## k-NN Vector Search

OpenSearch can store dense vector embeddings alongside traditional text fields and search them by similarity. This turns a search engine into a vector database without a separate system.

### How It Works

Each document contains a `knn_vector` field — a float array of fixed dimension (e.g., 768 for BERT, 1536 for OpenAI ada-002). At query time you provide a vector and OpenSearch returns the k closest neighbors.

### Algorithms

| Algorithm | Library | Type | Best For |
|-----------|---------|------|----------|
| **HNSW** (Hierarchical Navigable Small World) | nmslib / Faiss / Lucene | Graph-based | Default, best recall/speed balance |
| **IVF** (Inverted File Index) | Faiss | Partition-based | Large datasets, lower memory |
| **Faiss** | Meta's library | Both HNSW and IVF | GPU support, large-scale |
| **Lucene native** | Lucene | HNSW variant | Simpler setup, no native lib dependency |

HNSW builds a multi-layer navigable graph. Upper layers are sparse (long-range jumps), lower layers are dense (fine-grained). Search starts at the top and descends — like skip lists in graph form.

### HNSW Parameters

| Parameter | Default | Effect |
|-----------|---------|--------|
| `ef_construction` | 512 | Neighbors explored during index build. Higher = better recall, slower indexing |
| `m` | 16 | Max edges per node. Higher = better recall, more memory |
| `ef_search` | 512 | Neighbors explored at query time. Higher = better recall, slower queries |

Rule of thumb: `ef_search >= k` (the number of results you want). For production, start with defaults and tune based on recall benchmarks.

### Distance Functions

| Function | Use Case | Notes |
|----------|----------|-------|
| **L2 (Euclidean)** | Raw embeddings, image similarity | Lower score = more similar |
| **Cosine similarity** | Text embeddings (most common) | Normalized direction comparison |
| **Inner product** | When vectors are pre-normalized | Equivalent to cosine for unit vectors |

### Approximate vs Exact k-NN

**Approximate k-NN** (default): Uses the HNSW/IVF index structure. Fast (sub-second even at millions of vectors) but may miss the true closest neighbor. Recall is typically 95-99%+ with good parameters.

**Exact k-NN** (brute force): Computes distance to every vector in the index. Guarantees the true k nearest neighbors. Only practical for small datasets or heavily filtered result sets. Triggered via the `script_score` query.

### Mapping Example

```json
PUT /product-embeddings
{
  "settings": {
    "index": {
      "knn": true,
      "knn.algo_param.ef_search": 256
    }
  },
  "mappings": {
    "properties": {
      "product_name": { "type": "text" },
      "category": { "type": "keyword" },
      "description_vector": {
        "type": "knn_vector",
        "dimension": 768,
        "method": {
          "name": "hnsw",
          "space_type": "cosinesimil",
          "engine": "nmslib",
          "parameters": {
            "ef_construction": 512,
            "m": 16
          }
        }
      }
    }
  }
}
```

### Approximate k-NN Query

```json
GET /product-embeddings/_search
{
  "size": 10,
  "query": {
    "knn": {
      "description_vector": {
        "vector": [0.12, -0.34, 0.56, "... 768 floats total"],
        "k": 10
      }
    }
  }
}
```

### Exact k-NN (Brute Force) via Script Score

```json
GET /product-embeddings/_search
{
  "size": 10,
  "query": {
    "script_score": {
      "query": {
        "bool": {
          "filter": { "term": { "category": "electronics" } }
        }
      },
      "script": {
        "source": "knn_score",
        "lang": "knn",
        "params": {
          "field": "description_vector",
          "query_value": [0.12, -0.34, 0.56, "..."],
          "space_type": "cosinesimil"
        }
      }
    }
  }
}
```

This first filters to `electronics`, then brute-force computes cosine similarity on the filtered set. Useful when the filter narrows results to a manageable size.

### k-NN with Pre-Filtering

```json
GET /product-embeddings/_search
{
  "size": 10,
  "query": {
    "knn": {
      "description_vector": {
        "vector": [0.12, -0.34, 0.56, "..."],
        "k": 10,
        "filter": {
          "term": { "category": "electronics" }
        }
      }
    }
  }
}
```

Efficient filtering applies the filter during the graph traversal itself (Lucene engine), not as a post-filter that might discard good candidates.

---

## Neural Search / Hybrid Search

Pure vector search captures meaning but misses exact keyword matches. Pure BM25 captures keywords but misses synonyms and paraphrases. Hybrid search combines both for better relevance than either alone.

### Architecture

```
User Query
    |
    v
+-------------------+
| Search Pipeline   |
|                   |
|  1. BM25 query    |----> Lexical results (score list A)
|  2. k-NN query    |----> Semantic results (score list B)
|                   |
|  3. Normalize     |  min-max or L2 normalization
|  4. Combine       |  arithmetic_mean, harmonic_mean, geometric_mean
|  5. Return merged |
+-------------------+
    |
    v
Combined ranked results
```

### Ingest Pipeline — Auto-Encode Text to Vectors

Instead of computing vectors client-side, OpenSearch can call an ML model during ingestion:

```json
PUT /_ingest/pipeline/text-embedding-pipeline
{
  "description": "Encode product descriptions to vectors",
  "processors": [
    {
      "text_embedding": {
        "model_id": "aB1cD2eF3",
        "field_map": {
          "description": "description_vector"
        }
      }
    }
  ]
}
```

The ML model can be deployed on OpenSearch ML nodes (local) or point to SageMaker / Bedrock / external endpoints. Documents indexed through this pipeline automatically get their `description` field encoded into `description_vector`.

### Hybrid Query with Score Normalization

```json
GET /product-embeddings/_search
{
  "query": {
    "hybrid": {
      "queries": [
        {
          "match": {
            "product_name": {
              "query": "wireless noise cancelling headphones"
            }
          }
        },
        {
          "knn": {
            "description_vector": {
              "vector": [0.12, -0.34, 0.56, "..."],
              "k": 50
            }
          }
        }
      ]
    }
  },
  "search_pipeline": {
    "phase_results_processors": [
      {
        "normalization-processor": {
          "normalization": {
            "technique": "min_max"
          },
          "combination": {
            "technique": "arithmetic_mean",
            "parameters": {
              "weights": [0.3, 0.7]
            }
          }
        }
      }
    ]
  }
}
```

The `weights` parameter controls the balance: `[0.3, 0.7]` means 30% BM25, 70% semantic. Tune based on your data — keyword-heavy domains (part numbers, SKUs) benefit from higher BM25 weight; natural language queries benefit from higher semantic weight.

### When to Use What

| Approach | Best For |
|----------|----------|
| BM25 only | Exact keyword matching, structured queries, known-item search |
| k-NN only | Semantic similarity, recommendations, image/audio search |
| Hybrid | General-purpose search where users mix keywords and natural language |

---

## Observability (OpenTelemetry Integration)

OpenSearch positions itself as an observability backend that can ingest and analyze all three pillars: traces, metrics, and logs — in one system.

### The Three Pillars

```
Application (instrumented with OpenTelemetry SDK)
    |
    |--- Traces  (request flow across services)
    |--- Metrics (CPU, latency, error rate, custom counters)
    |--- Logs    (structured application logs)
    |
    v
Data Prepper (collection + transformation pipeline)
    |
    v
OpenSearch (storage + analysis + visualization)
    |
    v
OpenSearch Dashboards (Trace Analytics, log explorer, metric dashboards)
```

### Trace Analytics Plugin

- **Service map**: Auto-generated dependency graph showing which services call which. Built from span parent-child relationships.
- **Latency analysis**: P50/P95/P99 per service and per operation. Identify slow endpoints.
- **Error rate tracking**: Percentage of spans with error status per service.
- **Trace detail view**: Waterfall visualization of a single request flowing through services.

### Data Prepper

Data Prepper is an open-source ingestion pipeline (like Logstash but OpenSearch-native):

```
OTel Collector / Application
        |
        v
  Data Prepper
  +--------------------------+
  | Source: otel_trace_source |
  | Processor: otel_traces    |
  |   - service_map           |
  |   - trace_group           |
  | Sink: opensearch          |
  +--------------------------+
        |
        v
  OpenSearch indices:
    otel-v1-apm-span-*
    otel-v1-apm-service-map
```

Supports: OTel traces/metrics/logs, HTTP source, Kafka source, S3 source. Processors can enrich, filter, aggregate, and route data.

### Competitive Landscape

| Tool | Storage | Query | UI | Notes |
|------|---------|-------|----|-------|
| **OpenSearch** | OpenSearch | DSL/SQL/PPL | Dashboards | Full-text + observability in one |
| **Jaeger** | Cassandra/ES/OpenSearch | Limited | Jaeger UI | Traces only, popular in k8s |
| **Zipkin** | MySQL/Cassandra/ES | Limited | Zipkin UI | Lighter, traces only |
| **Grafana Tempo** | Object storage (S3) | TraceQL | Grafana | Cheap storage, no indexing (trace ID lookup) |

OpenSearch advantage: you already have it for logs, now add traces and metrics without another system. Disadvantage: heavier than purpose-built trace stores.

---

## Alerting

Monitors run scheduled queries and fire alerts when conditions are met.

### Components

```
Monitor (scheduled query, e.g., every 5 min)
    |
    v
Trigger (condition: "error_count > threshold")
    |
    v
Action (notification: SNS, Slack, webhook, email, custom)
```

### Monitor Types

- **Per query monitor**: Runs a single query, evaluates trigger against result.
- **Per bucket monitor**: Groups results into buckets (like GROUP BY), evaluates trigger per bucket.
- **Per document monitor**: Evaluates trigger against individual documents.
- **Composite monitor**: Chains multiple monitors with AND/OR logic. Example: alert only if error rate is high AND latency is high (avoids noisy single-signal alerts).

### Example: Alert if Error Rate > 5%

```json
PUT /_plugins/_alerting/monitors
{
  "type": "monitor",
  "name": "High Error Rate",
  "schedule": {
    "period": {
      "interval": 5,
      "unit": "MINUTES"
    }
  },
  "inputs": [
    {
      "search": {
        "indices": ["application-logs-*"],
        "query": {
          "size": 0,
          "query": {
            "range": {
              "@timestamp": {
                "gte": "now-5m",
                "lte": "now"
              }
            }
          },
          "aggs": {
            "total_requests": { "value_count": { "field": "_id" } },
            "errors": {
              "filter": { "term": { "status_code": 500 } },
              "aggs": {
                "count": { "value_count": { "field": "_id" } }
              }
            }
          }
        }
      }
    }
  ],
  "triggers": [
    {
      "name": "error_rate_trigger",
      "severity": "1",
      "condition": {
        "script": {
          "source": "def total = ctx.results[0].aggregations.total_requests.value; def errors = ctx.results[0].aggregations.errors.count.value; return total > 0 && (errors / total) > 0.05;",
          "lang": "painless"
        }
      },
      "actions": [
        {
          "name": "notify-slack",
          "destination_id": "slack-webhook-dest-id",
          "message_template": {
            "source": "Error rate exceeded 5% in the last 5 minutes. Errors: {{ctx.results[0].aggregations.errors.count.value}} / Total: {{ctx.results[0].aggregations.total_requests.value}}"
          }
        },
        {
          "name": "notify-sns",
          "destination_id": "sns-topic-dest-id",
          "message_template": {
            "source": "ALERT: High error rate detected on {{ctx.monitor.name}}"
          }
        }
      ]
    }
  ]
}
```

### Operational Features

- **Deduplication**: Alerts fire once when triggered, not every interval. Re-fires only if condition clears and re-occurs.
- **Acknowledge**: On-call engineer acknowledges an alert to suppress repeat notifications until the condition resets.
- **Mute**: Temporarily silence a monitor during maintenance windows.
- **Throttling**: Minimum time between notifications for the same trigger (e.g., at most once per hour).

---

## Anomaly Detection

OpenSearch can detect anomalies in time-series data without manually setting thresholds. You define what to monitor, and the system learns normal patterns and flags deviations.

### Random Cut Forest (RCF) Algorithm

RCF is an unsupervised, online algorithm developed by Amazon:

1. Maintains a forest of random trees built from recent data points.
2. Each new data point is inserted into the forest; the algorithm measures how much the tree structure changes.
3. Points that cause large structural changes get high **anomaly scores** (they don't fit the learned distribution).
4. No training phase needed — the model continuously adapts as data arrives.

Key properties:
- **Unsupervised**: No labeled data required.
- **Online**: Adapts to concept drift (seasonal patterns, gradual changes).
- **Interpretable**: Provides anomaly grade (0-1) and confidence.
- **Handles seasonality**: Learns daily/weekly patterns automatically.

### Use Cases

- Error rate spikes (5xx suddenly jumps from 0.1% to 5%)
- Unusual traffic patterns (DDoS, bot crawling)
- Infrastructure anomalies (CPU/memory deviations from baseline)
- Business metric shifts (order volume drops, revenue anomalies)
- Security (unusual login patterns, data exfiltration)

### Example Detector Configuration

```json
POST /_plugins/_anomaly_detection/detectors
{
  "name": "error-rate-anomaly-detector",
  "description": "Detect unusual error rates in application logs",
  "time_field": "@timestamp",
  "indices": ["application-logs-*"],
  "feature_aggregations": [
    {
      "feature_name": "error_count",
      "feature_enabled": true,
      "aggregation_query": {
        "error_count": {
          "filter": {
            "range": { "status_code": { "gte": 500 } }
          },
          "aggs": {
            "count": { "value_count": { "field": "_id" } }
          }
        }
      }
    },
    {
      "feature_name": "avg_latency",
      "feature_enabled": true,
      "aggregation_query": {
        "avg_latency": {
          "avg": { "field": "response_time_ms" }
        }
      }
    }
  ],
  "detection_interval": {
    "period": { "interval": 5, "unit": "MINUTES" }
  },
  "window_delay": {
    "period": { "interval": 1, "unit": "MINUTES" }
  },
  "category_field": ["service_name"]
}
```

Key fields:
- `feature_aggregations`: What to monitor. Each feature is a numeric aggregation computed per interval.
- `detection_interval`: How often to run detection (5 min here).
- `window_delay`: Buffer for late-arriving data.
- `category_field`: Creates a separate detector per unique value (e.g., per service). Called **high-cardinality detection**.

### Start the Detector

```json
POST /_plugins/_anomaly_detection/detectors/<detector_id>/_start
```

### Query Anomaly Results

```json
GET /_plugins/_anomaly_detection/detectors/<detector_id>/results/_search
{
  "query": {
    "bool": {
      "filter": [
        { "range": { "anomaly_grade": { "gt": 0.7 } } },
        { "range": { "execution_start_time": { "gte": "now-1d" } } }
      ]
    }
  },
  "sort": [{ "anomaly_grade": "desc" }]
}
```

### Anomaly Detection vs Static Alerting

| Dimension | Static Alerting | Anomaly Detection |
|-----------|----------------|-------------------|
| Thresholds | Manual (error > 5%) | Learned from data |
| Seasonality | Unaware (false positives on weekends) | Adapts to patterns |
| Setup | Define exact conditions | Define what to monitor |
| Best for | Known failure modes | Unknown unknowns |

In practice, use both: anomaly detection for discovery, static alerts for known critical thresholds.

---

## Cross-Cluster Replication (CCR)

CCR asynchronously replicates indices from a leader cluster to one or more follower clusters.

### Architecture

```
Leader Cluster (us-east-1)         Follower Cluster (eu-west-1)
+-------------------+              +-------------------+
| index: orders     | --async----> | index: orders     |
| (read-write)      |   replicate  | (read-only)       |
+-------------------+              +-------------------+
                                   Can be promoted to
                                   leader on failover
```

Replication is at the index level. Changes (indexing, deletes, mapping updates) are shipped from leader to follower via a translog-like replay mechanism.

### Use Cases

| Use Case | Setup |
|----------|-------|
| **Disaster Recovery** | Leader in primary region, follower in DR region. Promote follower if primary fails. |
| **Read Scaling** | Followers in multiple regions serve local read traffic, reducing cross-region latency. |
| **Data Locality** | Comply with data residency requirements by replicating subsets to regional clusters. |
| **Migration** | Replicate to a new cluster, verify, then switch traffic. |

### Key Behaviors

- **Follower is read-only**: Writes are rejected. Only the replication process can modify the follower index.
- **Async replication**: There is a replication lag (typically seconds to low minutes). Not suitable for strong-consistency requirements.
- **Promotion**: On leader failure, stop replication on follower, then the follower index becomes a regular writable index. This is a manual (or scripted) process, not automatic failover.
- **Index-level granularity**: You choose which indices to replicate. Not all-or-nothing.
- **Auto-follow patterns**: Automatically replicate new indices that match a pattern (e.g., `logs-*`).

### Setup

```json
// Step 1: Register remote cluster connection on follower
PUT /_cluster/settings
{
  "persistent": {
    "cluster.remote": {
      "leader-cluster": {
        "seeds": ["leader-node1:9300", "leader-node2:9300"]
      }
    }
  }
}

// Step 2: Start replication
PUT /_plugins/_replication/orders/_start
{
  "leader_alias": "leader-cluster",
  "leader_index": "orders",
  "use_roles": {
    "leader_cluster_role": "cross_cluster_replication_leader_full_access",
    "follower_cluster_role": "cross_cluster_replication_follower_full_access"
  }
}
```

---

## Cross-Cluster Search

Query multiple clusters in a single request without replicating data between them.

### How It Works

```
Client query
    |
    v
Local Cluster (coordinating)
    |--- search local shards
    |--- fan out to Remote Cluster A
    |--- fan out to Remote Cluster B
    |
    v
Merge results, return to client
```

The local cluster acts as a coordinator. It sends sub-queries to remote clusters, collects results, and merges them. This is federated search.

### Setup

```json
PUT /_cluster/settings
{
  "persistent": {
    "cluster.remote": {
      "cluster-us": { "seeds": ["us-node:9300"] },
      "cluster-eu": { "seeds": ["eu-node:9300"] }
    }
  }
}
```

### Query Across Clusters

```json
GET /local-index,cluster-us:remote-index,cluster-eu:remote-index/_search
{
  "query": { "match": { "message": "error" } }
}
```

### CCR vs Cross-Cluster Search

| Dimension | CCR | Cross-Cluster Search |
|-----------|-----|---------------------|
| Data duplication | Yes (full copy) | No |
| Query latency | Local speed (data is local) | Cross-network latency |
| Availability | Works if remote is down | Fails if remote is down |
| Storage cost | 2x per replica | 1x |
| Use when | Low latency reads + DR needed | Occasional federated queries |

---

## SQL Support

OpenSearch includes a SQL plugin that translates SQL queries to Query DSL and executes them.

### Endpoint

```
POST /_plugins/_sql
{
  "query": "SELECT * FROM my-index WHERE status = 'error' LIMIT 10"
}
```

### Supported SQL Features

```sql
-- Basic SELECT with filtering
SELECT service_name, status_code, response_time
FROM application-logs-*
WHERE status_code >= 500
  AND @timestamp > '2025-01-01'
ORDER BY response_time DESC
LIMIT 100;

-- Aggregations
SELECT service_name,
       COUNT(*) AS request_count,
       AVG(response_time) AS avg_latency,
       PERCENTILE(response_time, 95) AS p95_latency
FROM application-logs-*
WHERE @timestamp > NOW() - INTERVAL 1 HOUR
GROUP BY service_name
HAVING COUNT(*) > 100
ORDER BY avg_latency DESC;

-- JOIN (limited — hash join between two indices)
SELECT o.order_id, o.amount, c.customer_name
FROM orders o
JOIN customers c ON o.customer_id = c.customer_id
WHERE o.status = 'pending';
```

### Explain — See the Generated DSL

```
POST /_plugins/_sql/_explain
{
  "query": "SELECT service_name, COUNT(*) FROM logs GROUP BY service_name"
}
```

Returns the equivalent Query DSL, useful for learning or debugging.

### Limitations

- JOINs are limited (no multi-level joins, performance varies).
- No subqueries in all contexts.
- Nested field access requires special syntax.
- Not a replacement for a relational database — it is a convenience layer over an inverted index.

### Output Formats

- `jdbc` (default): Tabular rows and columns.
- `csv`: Comma-separated values for export.
- `raw`: Raw OpenSearch JSON response.

---

## PPL (Piped Processing Language)

PPL is OpenSearch's pipe-based query language, inspired by Splunk's SPL. It reads left-to-right with each stage piping into the next, making it intuitive for log exploration.

### Syntax Pattern

```
source = <index> | command1 | command2 | command3
```

### Examples

```sql
-- Find top error-producing services in the last hour
source = application-logs-*
| where status_code >= 500
| where @timestamp > NOW() - INTERVAL 1 HOUR
| stats count() as error_count by service_name
| sort - error_count
| head 10

-- Average latency by service with percentiles
source = application-logs-*
| stats avg(response_time) as avg_latency,
        percentile(response_time, 95) as p95,
        percentile(response_time, 99) as p99
  by service_name
| sort - p99

-- Dedup: show latest log per unique trace_id
source = application-logs-*
| where trace_id IS NOT NULL
| sort - @timestamp
| dedup trace_id
| fields trace_id, service_name, status_code, @timestamp

-- Time-series: error count per 5-minute bucket
source = application-logs-*
| where status_code >= 500
| stats count() as errors by span(@timestamp, 5m)
| sort + span

-- Pattern detection: find common log message patterns
source = application-logs-*
| patterns message
| stats count() as frequency by patterns_field
| sort - frequency
| head 20

-- Rename and computed fields
source = orders
| where total_amount > 100
| eval discount_amount = total_amount * 0.1
| fields order_id, customer_id, total_amount, discount_amount
| sort - total_amount
```

### PPL vs SQL vs Query DSL

| Dimension | PPL | SQL | Query DSL |
|-----------|-----|-----|-----------|
| Audience | Log analysts, ops | Anyone who knows SQL | Developers |
| Style | Pipe-based, sequential | Declarative, set-based | JSON, programmatic |
| Exploration | Excellent (iterative refinement) | Good | Verbose for exploration |
| Full power | Most common operations | Most common operations | Complete feature set |

PPL is the best choice for ad-hoc log exploration in OpenSearch Dashboards. SQL is better for reporting. Query DSL is required for application code that needs full control.

---

## Contrast: OpenSearch vs Purpose-Built Vector Databases

### The Landscape

| System | Primary Purpose | Vector Search | Full-Text Search | Filtering | Operational Features |
|--------|----------------|---------------|-------------------|-----------|---------------------|
| **OpenSearch** | Search + analytics + observability | Yes (k-NN plugin) | Yes (inverted index, BM25) | Yes (full DSL) | Alerting, anomaly detection, dashboards |
| **Pinecone** | Managed vector DB | Yes (core product) | No (metadata only) | Metadata filters | Serverless, auto-scaling |
| **Weaviate** | Vector DB + hybrid | Yes (core product) | Yes (BM25 module) | GraphQL filters | Schema-based, modules |
| **Milvus** | Open-source vector DB | Yes (core product) | Limited | Attribute filters | GPU acceleration |
| **pgvector** | Postgres extension | Yes (add-on) | Yes (Postgres FTS) | Full SQL | Leverage existing Postgres |
| **Qdrant** | Vector DB | Yes (core product) | No | Payload filters | Rust, fast filtering |

### When to Choose OpenSearch for Vectors

- You already run OpenSearch for logs/search and want to add semantic search without another system.
- You need hybrid search (BM25 + vectors) as a first-class feature.
- You need rich filtering alongside vector search (OpenSearch's query DSL is far more powerful than metadata filters in Pinecone).
- You need the operational stack (alerting, anomaly detection, dashboards) around the same data.
- Your vector dataset is in the tens of millions range (OpenSearch handles this well).

### When to Choose a Purpose-Built Vector DB

- **Scale**: Pinecone/Milvus are optimized for billions of vectors with low latency. OpenSearch k-NN starts to struggle at very high vector counts per node.
- **Simplicity**: If all you need is vector CRUD + nearest-neighbor search, Pinecone's API is simpler than managing OpenSearch clusters.
- **Managed serverless**: Pinecone serverless auto-scales to zero. OpenSearch (even managed) requires provisioned instances.
- **GPU acceleration**: Milvus supports GPU-based indexing for massive datasets. OpenSearch does not.
- **Cost at extreme scale**: Purpose-built systems optimize storage and compute specifically for vectors; OpenSearch carries overhead from its general-purpose architecture.

### Decision Framework

```
Do you already use OpenSearch?
  |
  YES --> Do you need vector search alongside text search?
  |         |
  |         YES --> Use OpenSearch k-NN (hybrid search)
  |         NO  --> Add k-NN plugin anyway (simplicity)
  |
  NO --> Is vector search your primary use case?
          |
          YES --> How many vectors?
          |         |
          |         < 100M --> Pinecone (managed) or Weaviate (self-hosted)
          |         > 100M --> Milvus (GPU) or Pinecone
          |
          NO --> Do you need full-text search + vectors + operational tooling?
                  |
                  YES --> OpenSearch
                  NO  --> Pick the simplest option for your use case
```

### Performance Comparison (Approximate)

| Metric | OpenSearch k-NN | Pinecone | Milvus |
|--------|----------------|----------|--------|
| Query latency (1M vectors) | 10-50ms | 5-20ms | 5-30ms |
| Index build time | Moderate | Fast (managed) | Fast (GPU) |
| Max vectors per node | ~10-20M (memory bound) | Managed (abstracted) | 100M+ (with GPU) |
| Hybrid search | Native, first-class | Not available | Limited |
| Operational overhead | Cluster management | Zero (serverless) | Cluster management |

The key insight: OpenSearch is not the best vector database, and Pinecone is not the best search engine. OpenSearch is the best choice when you need both in one system. Purpose-built vector databases win when vectors are the only workload.
