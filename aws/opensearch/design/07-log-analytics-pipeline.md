# 07 --- Log Analytics Pipeline: End-to-End Observability with OpenSearch

> **Context:** This document walks through the complete log analytics pipeline --- from
> collecting logs at the source to searching, visualizing, and alerting on them in OpenSearch.
> In an interview, this demonstrates you can design end-to-end observability systems, not just
> configure a search engine.

---

## Table of Contents

1. [The Log Analytics Problem Space](#1-the-log-analytics-problem-space)
2. [Data Sources & Collection](#2-data-sources--collection)
3. [Ingestion Pipeline Architecture](#3-ingestion-pipeline-architecture)
4. [Index Strategy for Logs](#4-index-strategy-for-logs)
5. [Schema Design for Logs](#5-schema-design-for-logs)
6. [Search Patterns for Log Analytics](#6-search-patterns-for-log-analytics)
7. [Dashboards & Visualization](#7-dashboards--visualization)
8. [Scale Considerations](#8-scale-considerations)
9. [Operational Concerns](#9-operational-concerns)
10. [Comparison: Splunk vs Datadog vs ELK vs OpenSearch](#10-comparison-splunk-vs-datadog-vs-elk-vs-opensearch)
11. [Interview Cheat Sheet](#11-interview-cheat-sheet)

---

## 1. The Log Analytics Problem Space

### Why Logs Matter

Logs are the **primary evidence trail** for three critical concerns:

| Concern | What Logs Provide | Example |
|---|---|---|
| **Debugging** | Request-level detail of what happened and when | Stack trace showing NullPointerException in OrderService at 14:32:07 |
| **Compliance** | Immutable audit trail of system actions | SOC 2 requires 90-day retention of access logs |
| **Security** | Evidence of unauthorized access or anomalous behavior | 500 failed SSH attempts from a single IP in 10 minutes |

### The Scale Challenge

A mid-size company with 200 microservices, each emitting 1,000 log lines/minute:

```
200 services x 1,000 lines/min x 60 min x 24 hr = 288 million log lines/day
Average line size: 500 bytes
Daily volume: 288M x 500B = ~144 GB/day raw
With structured JSON enrichment: ~500 GB - 1 TB/day

Large enterprises: 5-50 TB/day
```

### The Three Requirements

Every log analytics system must balance three competing demands:

```
                    INGEST FAST
                   (keep up with
                    production)
                       /\
                      /  \
                     /    \
                    / The  \
                   / Triangle\
                  /   of Log  \
                 /  Analytics   \
                /________________\
   SEARCH FAST                  RETAIN CHEAPLY
  (sub-second                   (months/years
   for debugging)                at low cost)
```

- **Ingest fast**: Zero data loss, handle bursts, no backpressure on applications.
- **Search fast**: Developers need sub-second results when debugging a production incident at 3 AM.
- **Retain cheaply**: Compliance may require 1-7 years of retention; hot storage for all of it is prohibitively expensive.

---

## 2. Data Sources & Collection

### Source Types

```
+-------------------------------------------------------------------+
|                        DATA SOURCES                                |
+-------------------------------------------------------------------+
|                                                                     |
|  APPLICATION LOGS          INFRASTRUCTURE LOGS     METRICS/TRACES   |
|  +------------------+     +-------------------+   +---------------+ |
|  | stdout/stderr    |     | syslog            |   | CPU, memory   | |
|  | log4j / logback  |     | kernel (dmesg)    |   | disk, network | |
|  | structured JSON  |     | CloudTrail        |   | custom biz    | |
|  | access logs      |     | VPC Flow Logs     |   | metrics       | |
|  | error logs       |     | Route 53 DNS logs |   | OpenTelemetry | |
|  | audit logs       |     | ELB access logs   |   | traces        | |
|  +------------------+     | S3 access logs    |   | X-Ray spans   | |
|                           +-------------------+   +---------------+ |
+-------------------------------------------------------------------+
```

### Application Logs

| Format | Example | Pros | Cons |
|---|---|---|---|
| **Unstructured (plain text)** | `2026-02-28 ERROR Failed to process order 12345` | Human-readable, easy to emit | Requires parsing (grok/regex), brittle |
| **Semi-structured (log4j pattern)** | `%d{ISO8601} [%t] %-5level %logger - %msg%n` | Standard format, parseable | Still needs grok pattern |
| **Structured JSON** | `{"timestamp":"2026-02-28T10:15:00Z","level":"ERROR","service":"order-svc","message":"Failed to process order","order_id":12345}` | Machine-parseable, no grok needed | Slightly larger, harder to read raw |

**Best practice**: Emit structured JSON at the source. It eliminates parsing overhead in the pipeline and prevents field extraction failures.

### Infrastructure Logs

| Source | Format | Volume | Key Fields |
|---|---|---|---|
| **CloudTrail** | JSON (S3 delivery) | Low-medium | `eventName`, `userIdentity`, `sourceIPAddress` |
| **VPC Flow Logs** | Space-delimited or Parquet | High (every packet decision) | `srcaddr`, `dstaddr`, `srcport`, `dstport`, `action` |
| **ELB Access Logs** | Space-delimited (S3 delivery) | High | `request_url`, `target_status_code`, `response_time` |
| **CloudWatch Logs** | JSON | Varies | `@timestamp`, `@message`, `@logStream` |
| **Syslog (RFC 5424)** | `<priority>version timestamp hostname app-name procid msgid structured-data msg` | Medium | `facility`, `severity`, `hostname` |

### Metrics and Traces

| Signal | Purpose | Collection Method |
|---|---|---|
| **Metrics** | Numeric time-series (CPU at 72%, request count = 1500) | Prometheus scrape, CloudWatch agent, OTEL Collector |
| **Traces** | Distributed request flow across services | OpenTelemetry SDK, AWS X-Ray SDK, Jaeger client |
| **Correlation** | Link a trace ID in a log line to a span in a trace | Common `trace.id` field across logs, metrics, traces |

### Collection Agents Comparison

| Agent | Origin | Language | Footprint | Strengths | Weaknesses |
|---|---|---|---|---|---|
| **Fluent Bit** | CNCF | C | ~1 MB memory | Extremely lightweight, ideal for containers/K8s sidecars | Fewer plugins than Fluentd |
| **Fluentd** | CNCF | Ruby + C | ~40 MB memory | 700+ plugins, mature ecosystem | Higher resource usage than Fluent Bit |
| **Logstash** | Elastic | JRuby (JVM) | ~500 MB memory | Most powerful transformations, grok, rich filter plugins | Heavy JVM footprint, not suited for edge |
| **Data Prepper** | OpenSearch | Java | ~200 MB memory | Native OpenSearch integration, trace analytics, OTel-native | Smaller plugin ecosystem |
| **Filebeat** | Elastic | Go | ~15 MB memory | Lightweight file shipper, modules for common logs | Limited transformation ability |
| **OpenTelemetry Collector** | CNCF | Go | ~50 MB memory | Vendor-neutral, supports logs/metrics/traces | Newer for logs, still maturing |

**Typical deployment pattern:**

```
Container/Host                   Aggregation Layer           Destination
+-----------+                    +------------------+
| Fluent Bit| ----forward--->    | Fluentd /        | ----> OpenSearch
| (sidecar) |                    | Data Prepper     | ----> S3 (archive)
+-----------+                    | (centralized)    | ----> CloudWatch
                                 +------------------+
```

- Lightweight agents (Fluent Bit, Filebeat) on every node/pod.
- Heavier processors (Fluentd, Logstash, Data Prepper) in a centralized aggregation tier.

---

## 3. Ingestion Pipeline Architecture

### End-to-End Architecture

```
+----------+   +-----------+   +------------------+   +-----------+   +------------------+
|  Sources |-->| Collectors|-->|  Buffer Layer     |-->| Processors|-->|   OpenSearch      |
|          |   |           |   |                   |   |           |   |                  |
| App logs |   | Fluent Bit|   | Kafka / Kinesis / |   | Data      |   | Hot tier         |
| Infra    |   | Filebeat  |   | SQS               |   | Prepper / |   | (write + search) |
| logs     |   | OTel      |   |                   |   | Logstash  |   |                  |
| VPC Flow |   | Collector |   | - Backpressure    |   |           |   | Warm/Cold tiers  |
| Logs     |   |           |   | - Replay          |   | - Parse   |   | (ISM-managed)    |
| Cloud    |   |           |   | - Decouple        |   | - Enrich  |   |                  |
| Trail    |   |           |   | - Buffer bursts   |   | - Filter  |   | Dashboards       |
+----------+   +-----------+   +------------------+   | - Route   |   | (visualize)      |
                                                       +-----------+   +------------------+
                                                                              |
                                                                              v
                                                                       +-----------+
                                                                       |  Alerts   |
                                                                       | SNS/Slack |
                                                                       | PagerDuty |
                                                                       +-----------+
```

### Why a Buffer Layer Matters

Without a buffer, a spike in log volume or an OpenSearch slowdown causes **backpressure** that propagates all the way to the application, potentially causing log loss or application slowdowns.

| Problem | Without Buffer | With Buffer (Kafka/Kinesis) |
|---|---|---|
| **Burst traffic** | Processors overwhelmed, drop logs | Buffer absorbs burst, processors consume at own pace |
| **OpenSearch downtime** | Logs lost during maintenance window | Buffer retains data, replay after recovery |
| **Pipeline upgrade** | Must drain pipeline first | Pause consumer, upgrade, resume --- zero loss |
| **Multiple consumers** | Duplicate shipping from source | Single write to buffer, multiple consumers (OpenSearch, S3, SIEM) |
| **Ordering** | No guarantee | Partition by service/host for per-partition ordering |

**Buffer sizing rule of thumb:** Retain at least 24 hours of data in the buffer. At 1 TB/day, that means 1 TB of Kafka/Kinesis capacity.

### Data Prepper Pipeline Configuration

Data Prepper is OpenSearch's own ingestion processor, purpose-built for trace analytics and log pipelines.

```yaml
# data-prepper-pipelines.yaml

log-pipeline:
  source:
    kafka:
      bootstrap_servers:
        - "kafka-broker-1:9092"
        - "kafka-broker-2:9092"
      topics:
        - name: "application-logs"
          group_id: "data-prepper-logs"
      schema:
        type: "json"

  processor:
    - date:
        match:
          - key: "timestamp"
            patterns: ["ISO8601", "yyyy-MM-dd HH:mm:ss.SSS"]
        destination: "@timestamp"

    - grok:
        match:
          message:
            - "%{COMMONAPACHELOG}"
            - "%{SYSLOGLINE}"

    - add_entries:
        entries:
          - key: "pipeline"
            value: "data-prepper-v1"

    - geoip:
        source: "client.ip"
        target: "client.geo"

    - drop_events:
        when: '/log_level == "DEBUG"'

  sink:
    - opensearch:
        hosts: ["https://opensearch-node:9200"]
        index: "logs-%{yyyy.MM.dd}"
        bulk_size: 10
        flush_timeout: 60s
        username: "admin"
        password: "admin"

  buffer:
    bounded_blocking:
      buffer_size: 25600
      batch_size: 512
```

### Logstash Pipeline Configuration

```ruby
# logstash.conf

input {
  kafka {
    bootstrap_servers => "kafka-broker-1:9092,kafka-broker-2:9092"
    topics => ["application-logs"]
    group_id => "logstash-logs"
    codec => "json"
    consumer_threads => 4
  }
}

filter {
  # Parse timestamp
  date {
    match => ["timestamp", "ISO8601", "yyyy-MM-dd HH:mm:ss.SSS"]
    target => "@timestamp"
  }

  # Grok unstructured logs
  if ![level] {
    grok {
      match => {
        "message" => "%{TIMESTAMP_ISO8601:timestamp} \[%{DATA:thread}\] %{LOGLEVEL:level} %{DATA:logger} - %{GREEDYDATA:msg}"
      }
    }
  }

  # GeoIP enrichment
  if [client_ip] {
    geoip {
      source => "client_ip"
      target => "geo"
    }
  }

  # User-agent parsing
  if [user_agent] {
    useragent {
      source => "user_agent"
      target => "ua"
    }
  }

  # Drop debug logs in production
  if [level] == "DEBUG" {
    drop {}
  }
}

output {
  opensearch {
    hosts => ["https://opensearch-node:9200"]
    index => "logs-%{+YYYY.MM.dd}"
    user => "admin"
    password => "admin"
    ssl => true
  }
}
```

### Common Grok Patterns

```
# Apache Combined Log Format
%{IPORHOST:clientip} %{USER:ident} %{USER:auth} \[%{HTTPDATE:timestamp}\] \
"%{WORD:method} %{URIPATHPARAM:request} HTTP/%{NUMBER:httpversion}" \
%{NUMBER:response} %{NUMBER:bytes} "%{DATA:referrer}" "%{DATA:agent}"

# Nginx Access Log
%{IPORHOST:remote_addr} - %{USER:remote_user} \[%{HTTPDATE:time_local}\] \
"%{WORD:method} %{URIPATHPARAM:request} HTTP/%{NUMBER:http_version}" \
%{NUMBER:status} %{NUMBER:body_bytes_sent} "%{DATA:http_referer}" "%{DATA:http_user_agent}"

# Syslog (RFC 3164)
%{SYSLOGTIMESTAMP:syslog_timestamp} %{SYSLOGHOST:syslog_hostname} \
%{DATA:syslog_program}(?:\[%{POSINT:syslog_pid}\])?: %{GREEDYDATA:syslog_message}

# Java Stack Trace (multiline --- requires multiline codec upstream)
%{TIMESTAMP_ISO8601:timestamp} %{LOGLEVEL:level} %{DATA:class} - %{GREEDYDATA:message}
```

### Enrichment Stages

| Enrichment | Input | Output | Use Case |
|---|---|---|---|
| **GeoIP** | IP address | Country, city, lat/lon | Map visualization, geo-based alerting |
| **User-Agent parsing** | UA string | Browser, OS, device type | Client analytics |
| **DNS reverse lookup** | IP address | Hostname | Identify internal services |
| **Field extraction** | Raw message | Structured fields | Parse unstructured legacy logs |
| **Lookup table** | Service ID | Service name, team, tier | Enrich with organizational metadata |
| **Hash/anonymize** | PII fields | Hashed value | GDPR compliance |

---

## 4. Index Strategy for Logs

### Time-Based Indices

The standard pattern for log indices is **one index per time period**:

```
logs-2026.02.26
logs-2026.02.27
logs-2026.02.28    <-- today, actively written to
```

### Why Time-Based Indices

| Reason | Explanation |
|---|---|
| **Efficient deletion** | Drop an entire index instead of delete-by-query. O(1) vs O(n). |
| **Different retention per age** | ISM can move older indices to warm/cold tiers automatically. |
| **Query optimization** | Searching last 24 hours hits only 1-2 indices, not the entire dataset. |
| **Shard sizing** | Predictable shard sizes based on daily volume. |
| **Snapshot efficiency** | Yesterday's index is immutable --- only snapshot once. |

### Index Template

```json
PUT _index_template/logs-template
{
  "index_patterns": ["logs-*"],
  "priority": 100,
  "template": {
    "settings": {
      "number_of_shards": 5,
      "number_of_replicas": 1,
      "refresh_interval": "30s",
      "index.mapping.total_fields.limit": 2000,
      "index.translog.durability": "async",
      "index.translog.sync_interval": "30s",
      "index.translog.flush_threshold_size": "1gb",
      "index.sort.field": ["@timestamp"],
      "index.sort.order": ["desc"],
      "codec": "best_compression"
    },
    "mappings": {
      "dynamic": "strict",
      "properties": {
        "@timestamp":   { "type": "date" },
        "message":      { "type": "text" },
        "log.level":    { "type": "keyword" },
        "service.name": { "type": "keyword" },
        "host.name":    { "type": "keyword" },
        "trace.id":     { "type": "keyword" },
        "span.id":      { "type": "keyword" },
        "client.ip":    { "type": "ip" },
        "http.method":  { "type": "keyword" },
        "http.status":  { "type": "integer" },
        "http.url":     { "type": "keyword", "ignore_above": 2048 },
        "duration_ms":  { "type": "float" },
        "error.type":   { "type": "keyword" },
        "error.stack":  { "type": "text", "index": false }
      }
    }
  }
}
```

### Rollover Strategy

Instead of purely date-based, use **rollover** for more predictable shard sizes:

```json
// Step 1: Create a rollover-aware alias
PUT logs-write-000001
{
  "aliases": {
    "logs-write": { "is_write_index": true },
    "logs-read":  {}
  }
}

// Step 2: ISM policy triggers rollover
// Rollover when: index reaches 50 GB OR 1 day old
```

```
Timeline with rollover:

Day 1 (low traffic):
  logs-write-000001  (12 GB)  <-- write alias points here

Day 2 (still under 50 GB):
  logs-write-000001  (38 GB)  <-- still writing here
  (no rollover yet --- under 50 GB and under 1 day since last rollover check)

Day 2 (rollover triggered at 50 GB):
  logs-write-000001  (50 GB)  <-- read alias
  logs-write-000002  (0 GB)   <-- write alias moves here

Day 3 (burst traffic):
  logs-write-000001  (50 GB)  <-- read alias
  logs-write-000002  (50 GB)  <-- read alias, rollover triggered
  logs-write-000003  (0 GB)   <-- write alias
```

### Write and Read Aliases

```
Writes always go to:    logs-write  (points to latest index)
Reads always go to:     logs-read   (points to ALL log indices)

Application code NEVER references concrete index names like logs-write-000042.
This decouples the application from the index lifecycle.
```

### ISM Policy for Log Lifecycle

```json
PUT _plugins/_ism/policies/log-lifecycle
{
  "policy": {
    "policy_id": "log-lifecycle",
    "description": "Hot -> Warm -> Cold -> Delete lifecycle for logs",
    "default_state": "hot",
    "states": [
      {
        "name": "hot",
        "actions": [
          {
            "rollover": {
              "min_size": "50gb",
              "min_index_age": "1d"
            }
          }
        ],
        "transitions": [
          {
            "state_name": "warm",
            "conditions": { "min_index_age": "7d" }
          }
        ]
      },
      {
        "name": "warm",
        "actions": [
          { "warm_migration": {} },
          { "replica_count": { "number_of_replicas": 0 } },
          { "force_merge": { "max_num_segments": 1 } }
        ],
        "transitions": [
          {
            "state_name": "cold",
            "conditions": { "min_index_age": "30d" }
          }
        ]
      },
      {
        "name": "cold",
        "actions": [
          { "cold_migration": {} }
        ],
        "transitions": [
          {
            "state_name": "delete",
            "conditions": { "min_index_age": "90d" }
          }
        ]
      },
      {
        "name": "delete",
        "actions": [
          { "cold_delete": {} }
        ],
        "transitions": []
      }
    ],
    "ism_template": [
      {
        "index_patterns": ["logs-write-*"],
        "priority": 100
      }
    ]
  }
}
```

### Tier Breakdown

```
  Day 0          Day 7           Day 30          Day 90
    |               |               |               |
    v               v               v               v
+--------+     +--------+      +--------+      +---------+
|  HOT   |---->|  WARM  |----->|  COLD  |----->| DELETE  |
|        |     |        |      |        |      |         |
| SSD/gp3|     | UltraWarm    | S3 only |      | Gone    |
| 1 rep  |     | 0 replicas   | read-only      |         |
| r/w    |     | force-merged | attach to      |         |
|        |     | read-only    | query   |      |         |
+--------+     +--------+      +--------+      +---------+

Cost/GB:  $$$          $$            $            $0
Latency:  ms           ms-sec        min (attach)  N/A
```

---

## 5. Schema Design for Logs

### Common Schema (ECS-Aligned)

The Elastic Common Schema (ECS) provides a standard set of field names. OpenSearch follows
a compatible approach. Using a common schema means logs from different services are queryable
with the same field names.

```json
{
  "@timestamp": "2026-02-28T10:15:32.456Z",
  "message": "POST /api/orders returned 500 in 2340ms",

  "log.level": "ERROR",
  "log.logger": "com.example.OrderController",

  "service.name": "order-service",
  "service.version": "2.4.1",
  "service.environment": "production",

  "host.name": "ip-10-0-1-42",
  "host.ip": "10.0.1.42",

  "trace.id": "abc123def456",
  "span.id": "789ghi012",

  "http.method": "POST",
  "http.url": "/api/orders",
  "http.status_code": 500,
  "http.request.body.bytes": 1024,
  "http.response.body.bytes": 256,

  "client.ip": "203.0.113.50",
  "client.geo.country_name": "United States",

  "error.type": "NullPointerException",
  "error.message": "order.getCustomerId() returned null",
  "error.stack_trace": "java.lang.NullPointerException\n  at com.example.OrderController..."
}
```

### Keyword vs Text Field Decisions

| Field | Type | Rationale |
|---|---|---|
| `@timestamp` | `date` | Range queries, sorting, date histograms |
| `message` | `text` | Full-text search, analyzed with standard analyzer |
| `log.level` | `keyword` | Exact match filtering (ERROR, WARN), terms aggregation |
| `service.name` | `keyword` | Exact match, terms aggregation, cardinality low |
| `trace.id` | `keyword` | Exact match point queries, never analyzed |
| `host.name` | `keyword` | Exact match, terms aggregation |
| `http.url` | `keyword` (`ignore_above: 2048`) | Exact match, but cap length to prevent mapping explosion |
| `http.status_code` | `integer` | Range queries (>= 500), histogram aggregation |
| `duration_ms` | `float` | Range queries, percentile aggregation |
| `error.stack_trace` | `text` (`index: false`) | Stored but NOT indexed --- saves space, can still view in `_source` |
| `client.ip` | `ip` | IP range queries, CIDR support |

**Rule of thumb**: If you filter or aggregate on exact values, use `keyword`. If you need
full-text search, use `text`. If you only display it, consider `index: false`.

### Mapping Explosion Prevention

Dynamic mapping is dangerous for logs. A single misconfigured service emitting arbitrary
JSON keys can create thousands of fields, blowing up cluster state.

```json
// BAD: Dynamic mapping on (default)
// A log like {"user_preference_color_theme_dark_mode": true} creates a new field.
// 10,000 unique keys = 10,000 fields in mapping = cluster state bloat.

// GOOD: Strict mapping
{
  "mappings": {
    "dynamic": "strict"
  }
}

// ALTERNATIVE: dynamic = "false" (ignores unknown fields, still indexes known ones)
{
  "mappings": {
    "dynamic": "false",
    "properties": {
      "@timestamp": { "type": "date" },
      "message": { "type": "text" },
      "log.level": { "type": "keyword" }
    }
  }
}

// SAFETY NET: Hard limit on total fields
{
  "settings": {
    "index.mapping.total_fields.limit": 2000
  }
}
```

### Doc Values Optimization

Doc values are columnar on-disk structures used for sorting, aggregation, and scripting.
They are enabled by default for `keyword`, `numeric`, `date`, `ip`, and `boolean` fields.

```
For high-volume log fields that are ONLY searched (never aggregated or sorted):
  - Consider disabling doc_values to save disk space

  "some_field": {
    "type": "keyword",
    "doc_values": false    // saves ~15-20% disk for this field
  }

For text fields you need to aggregate on:
  - Use multi-field mapping

  "message": {
    "type": "text",
    "fields": {
      "keyword": {
        "type": "keyword",
        "ignore_above": 256
      }
    }
  }
```

### Disabling `_source` (Advanced Trade-off)

```
The _source field stores the original JSON document. It typically consumes 50-70% of
the index size.

Disabling it saves significant storage:
  "mappings": {
    "_source": { "enabled": false }
  }

TRADE-OFFS:
  + 50-70% storage reduction
  - Cannot view full original document
  - Cannot reindex (no source to read from)
  - Cannot use update API
  - Cannot use highlights on text fields

VERDICT: Only disable for extremely high-volume, low-value logs where you only need
         to search and aggregate, never view the original document. Most teams should
         keep _source enabled.
```

---

## 6. Search Patterns for Log Analytics

### Pattern 1: Time-Range + Keyword Filtering (Most Common)

"Show me ERROR logs from order-service in the last hour."

```json
GET logs-read/_search
{
  "query": {
    "bool": {
      "filter": [
        {
          "range": {
            "@timestamp": {
              "gte": "now-1h",
              "lte": "now"
            }
          }
        },
        { "term": { "log.level": "ERROR" } },
        { "term": { "service.name": "order-service" } }
      ]
    }
  },
  "sort": [{ "@timestamp": "desc" }],
  "size": 100
}
```

**Why `filter` context**: Filter clauses are cached and skip scoring. For log analytics,
relevance scoring is almost never needed --- you want exact matches, not "best matches."

### Pattern 2: Full-Text Search Within a Service

"Search for 'connection timeout' in payment-service logs."

```json
GET logs-read/_search
{
  "query": {
    "bool": {
      "must": [
        {
          "match_phrase": {
            "message": "connection timeout"
          }
        }
      ],
      "filter": [
        {
          "range": {
            "@timestamp": { "gte": "now-24h" }
          }
        },
        { "term": { "service.name": "payment-service" } }
      ]
    }
  },
  "highlight": {
    "fields": { "message": {} }
  },
  "sort": [{ "@timestamp": "desc" }],
  "size": 50
}
```

### Pattern 3: Trace ID Lookup (Point Query)

"Show me all logs for trace abc123def456."

```json
GET logs-read/_search
{
  "query": {
    "term": {
      "trace.id": "abc123def456"
    }
  },
  "sort": [{ "@timestamp": "asc" }],
  "size": 1000
}
```

This is a **point query** --- extremely fast on `keyword` fields. It retrieves the entire
request flow across all services in chronological order.

### Pattern 4: Error Rate Per Service (Aggregation)

"Show error rate per service over the last 6 hours, in 5-minute buckets."

```json
GET logs-read/_search
{
  "size": 0,
  "query": {
    "range": {
      "@timestamp": { "gte": "now-6h" }
    }
  },
  "aggs": {
    "per_5min": {
      "date_histogram": {
        "field": "@timestamp",
        "fixed_interval": "5m"
      },
      "aggs": {
        "by_service": {
          "terms": {
            "field": "service.name",
            "size": 20
          },
          "aggs": {
            "error_count": {
              "filter": {
                "term": { "log.level": "ERROR" }
              }
            },
            "error_rate": {
              "bucket_script": {
                "buckets_path": {
                  "errors": "error_count._count",
                  "total": "_count"
                },
                "script": "params.errors / params.total * 100"
              }
            }
          }
        }
      }
    }
  }
}
```

### Pattern 5: P99 Latency Over Time

"Show me the P50, P95, and P99 request latency for the API gateway."

```json
GET logs-read/_search
{
  "size": 0,
  "query": {
    "bool": {
      "filter": [
        { "range": { "@timestamp": { "gte": "now-24h" } } },
        { "term": { "service.name": "api-gateway" } }
      ]
    }
  },
  "aggs": {
    "latency_over_time": {
      "date_histogram": {
        "field": "@timestamp",
        "fixed_interval": "5m"
      },
      "aggs": {
        "latency_percentiles": {
          "percentiles": {
            "field": "duration_ms",
            "percents": [50, 95, 99]
          }
        }
      }
    }
  }
}
```

### Pattern 6: Top Error Messages (Significant Terms)

"What error messages are unusually frequent right now compared to the baseline?"

```json
GET logs-read/_search
{
  "size": 0,
  "query": {
    "bool": {
      "filter": [
        { "range": { "@timestamp": { "gte": "now-1h" } } },
        { "term": { "log.level": "ERROR" } }
      ]
    }
  },
  "aggs": {
    "unusual_errors": {
      "significant_terms": {
        "field": "error.type",
        "size": 10
      }
    }
  }
}
```

`significant_terms` compares term frequency in the foreground set (last 1 hour of errors)
against the background set (all errors) to surface terms that are **statistically unusual** ---
not just frequent.

---

## 7. Dashboards & Visualization

### OpenSearch Dashboards

OpenSearch Dashboards is the fork of Kibana that ships with OpenSearch. It provides the
visualization and exploration layer for log data.

### Key Dashboard Panels for Ops

```
+-----------------------------------------------------------------------+
|                    OPERATIONS DASHBOARD                                 |
|                                                                         |
| +---------------------------+  +------------------------------------+  |
| | Request Rate / min        |  | Error Rate by Service (%)          |  |
| |                           |  |                                    |  |
| |   ^                       |  |   order-svc    ████████  3.2%     |  |
| |   |    ___                |  |   payment-svc  ██████████████ 8.1%|  |
| |   |   /   \    ___       |  |   user-svc     ██  0.4%           |  |
| |   |  /     \  /   \      |  |   api-gateway  ████  1.1%         |  |
| |   | /       \/     \     |  |                                    |  |
| |   +---->  time  ---->    |  +------------------------------------+  |
| +---------------------------+                                          |
|                                                                         |
| +---------------------------+  +------------------------------------+  |
| | Latency Percentiles (ms) |  | Log Volume Heatmap (by hour/day)   |  |
| |                           |  |                                    |  |
| |  P99 -----.              |  |     Mon Tue Wed Thu Fri Sat Sun    |  |
| |  P95 ----.  \            |  | 00: [2] [1] [1] [2] [1] [1] [1]  |  |
| |  P50 ---.  \ \           |  | 06: [3] [3] [3] [3] [3] [1] [1]  |  |
| |         |   | |           |  | 12: [5] [5] [5] [5] [5] [2] [2]  |  |
| |         v   v v           |  | 18: [4] [4] [4] [4] [3] [2] [1]  |  |
| |   -------> time           |  | (color = volume intensity)        |  |
| +---------------------------+  +------------------------------------+  |
|                                                                         |
| +-------------------------------------------------------------------+  |
| | Top Error Messages (last 1 hour)                                   |  |
| |                                                                     |  |
| | 1. NullPointerException in OrderService.process()    (423 times)   |  |
| | 2. Connection timeout to payment-db:5432             (187 times)   |  |
| | 3. 429 Too Many Requests from rate-limiter           (95 times)    |  |
| +-------------------------------------------------------------------+  |
+-----------------------------------------------------------------------+
```

### Alerting Integration

OpenSearch supports built-in alerting via monitors, triggers, and destinations.

```json
// Monitor: Check error rate every 5 minutes
PUT _plugins/_alerting/monitors
{
  "name": "High Error Rate - Order Service",
  "type": "monitor",
  "schedule": {
    "period": { "interval": 5, "unit": "MINUTES" }
  },
  "inputs": [{
    "search": {
      "indices": ["logs-read"],
      "query": {
        "size": 0,
        "query": {
          "bool": {
            "filter": [
              { "range": { "@timestamp": { "gte": "now-5m" } } },
              { "term": { "service.name": "order-service" } },
              { "term": { "log.level": "ERROR" } }
            ]
          }
        }
      }
    }
  }],
  "triggers": [{
    "name": "Error spike",
    "severity": "1",
    "condition": {
      "script": {
        "source": "ctx.results[0].hits.total.value > 100"
      }
    },
    "actions": [{
      "name": "Notify Slack",
      "destination_id": "slack-ops-channel",
      "message_template": {
        "source": "Order service has {{ctx.results[0].hits.total.value}} errors in last 5 min."
      }
    }]
  }]
}
```

### Alert Destinations

| Destination | Use Case |
|---|---|
| **Amazon SNS** | Fan-out to email, SMS, Lambda, SQS |
| **Slack webhook** | Real-time team notification |
| **PagerDuty** | On-call escalation for critical alerts |
| **Custom webhook** | Integration with any HTTP endpoint (Jira, ServiceNow, etc.) |
| **Email (SES)** | Direct email notification |

### Anomaly Detection on Log Patterns (RCF)

OpenSearch includes the **Random Cut Forest (RCF)** algorithm for unsupervised anomaly detection.

```
How RCF works for logs:
1. Configure a detector on a numeric metric (e.g., error_count per 5-min bucket)
2. RCF builds a model of "normal" behavior over a training period (~1-2 weeks)
3. At runtime, each new data point gets an anomaly score (0-10+ scale)
4. Score > threshold --> anomaly alert

Use cases:
- Sudden spike in error rate that doesn't match historical pattern
- Unusual drop in request volume (service down?)
- Latency deviation outside normal variance
```

```json
// Create an anomaly detection job
POST _plugins/_anomaly_detection/detectors
{
  "name": "order-service-error-anomaly",
  "indices": ["logs-read"],
  "time_field": "@timestamp",
  "detection_interval": { "period": { "interval": 5, "unit": "Minutes" } },
  "feature_attributes": [{
    "feature_name": "error_count",
    "feature_enabled": true,
    "aggregation_query": {
      "error_count": {
        "filter": {
          "bool": {
            "must": [
              { "term": { "service.name": "order-service" } },
              { "term": { "log.level": "ERROR" } }
            ]
          }
        }
      }
    }
  }],
  "filter_query": {
    "match_all": {}
  }
}
```

---

## 8. Scale Considerations

### Sizing Calculation for Log Analytics

**Scenario: 100K events/sec, 90-day retention**

```
INGEST SIZING
=============
Events per second:   100,000
Avg event size:      500 bytes (structured JSON)
Ingest throughput:   100,000 x 500 B = 50 MB/sec = ~4.3 TB/day

OpenSearch single node can handle:
  - r6g.xlarge (4 vCPU, 32 GB RAM): ~10,000-15,000 events/sec bulk indexing
  - r6g.2xlarge (8 vCPU, 64 GB RAM): ~25,000-40,000 events/sec

Nodes needed for ingest:
  100,000 / 15,000 = ~7 data nodes (r6g.xlarge) minimum
  Add 30% headroom: ~9 data nodes
  With 1 replica: ~18 data nodes (or use 9 x r6g.2xlarge)

STORAGE SIZING
==============
Raw data per day:         4.3 TB
OpenSearch storage ratio: ~1.45x (inverted index + doc values + overhead)
Per day (no replicas):    4.3 x 1.45 = ~6.2 TB
Per day (1 replica):      6.2 x 2 = ~12.4 TB
With best_compression:    12.4 x 0.65 = ~8.1 TB/day on disk

TIERED RETENTION (90 days)
==========================
Hot tier (7 days):
  8.1 TB/day x 7 days = ~57 TB on EBS (gp3)
  57 TB / 9 nodes = ~6.3 TB per node (within EBS gp3 16 TB limit)

Warm tier (day 8-30, 23 days):
  After force-merge + 0 replicas: ~6.2 TB x 0.65 x 23 = ~93 TB on UltraWarm
  UltraWarm: up to 3 PB per domain

Cold tier (day 31-90, 60 days):
  ~6.2 TB x 0.65 x 60 = ~242 TB on S3 (cold storage)

TOTAL STORAGE FOOTPRINT
  Hot:   57 TB (EBS)
  Warm:  93 TB (S3 + cache)
  Cold: 242 TB (S3)
```

### Shard Strategy

```
Rule of thumb: 1 primary shard per 50 GB of expected index size

Daily index at 6.2 TB (no replicas):
  6,200 GB / 50 GB = 124 primary shards per daily index

That's a LOT of shards. Better approach: rollover at 50 GB.
  6,200 GB / 50 GB = ~124 rollovers per day
  Each rollover index: 1 primary shard + 1 replica = 2 shards

Total shards in hot tier (7 days):
  124 rollovers/day x 7 days x 2 (primary + replica) = ~1,736 shards
  Well within the recommended max of ~20 shards per GB of heap
  9 nodes x 32 GB heap x 20 = 5,760 shards capacity

For lower-volume environments (< 50 GB/day):
  Single daily index with 1-5 primary shards is fine
```

### Hot-Warm-Cold Node Sizing

| Tier | Node Type | Count | Instance | Storage | Total |
|---|---|---|---|---|---|
| **Hot** | Data | 9 | r6g.2xlarge (64 GB RAM) | 7 TB gp3 each | 63 TB |
| **Warm** | UltraWarm | 4 | ultrawarm1.xlarge (24 vCPU) | S3-backed | 93 TB |
| **Cold** | Cold storage | - | N/A (S3 only) | S3 | 242 TB |
| **Master** | Dedicated master | 3 | m6g.large (8 GB RAM) | 20 GB gp3 | - |

### Bulk Indexing Tuning

```json
// Index settings optimized for log ingestion throughput

PUT logs-write-000001/_settings
{
  "refresh_interval": "30s",
  "translog.durability": "async",
  "translog.sync_interval": "30s",
  "translog.flush_threshold_size": "1gb"
}

// Explanation:
// refresh_interval: 30s (default 1s)
//   - Reduces segment creation frequency
//   - Trades search "freshness" for write throughput
//   - Logs visible in search within 30s instead of 1s (acceptable for most use cases)
//
// translog.durability: async
//   - Fsync translog every sync_interval instead of every request
//   - Risk: lose up to 30s of data on node crash (acceptable for logs, data is in Kafka buffer)
//
// translog.flush_threshold_size: 1gb
//   - Larger translog before triggering Lucene commit
//   - Reduces number of Lucene commits, improves throughput
```

### Index Sorting by @timestamp

```json
// Configured in index template (shown earlier)
{
  "settings": {
    "index.sort.field": ["@timestamp"],
    "index.sort.order": ["desc"]
  }
}

// Effect: Documents within each segment are physically sorted by @timestamp.
//
// Benefit for range queries:
//   - "Give me logs from last 5 minutes" can use early termination
//   - Once the query reaches documents older than the range, it stops scanning
//   - Can turn a full-segment scan into a partial scan
//
// Cost:
//   - ~10-15% slower indexing (sorting on write)
//   - Worth it for log workloads where range queries dominate
```

---

## 9. Operational Concerns

### Log Pipeline Monitoring (Meta-Observability)

The pipeline that monitors your applications also needs monitoring. This is "observability
of the observability system."

```
What to monitor:                  Where to monitor it:

Pipeline lag (Kafka consumer lag)  --> Kafka JMX metrics --> Prometheus/CloudWatch
Events processed per second        --> Data Prepper metrics --> Prometheus
Indexing rejection rate             --> OpenSearch _cat/thread_pool?v --> CloudWatch
Bulk indexing errors                --> OpenSearch bulk response errors --> alert
Pipeline process CPU/memory         --> Container/host metrics
End-to-end latency (event time     --> Custom metric: @timestamp vs ingest time
  to searchable)
```

**Key metric**: End-to-end latency from event emission to searchable in OpenSearch. Target: < 60 seconds for operational logs.

### Dead Letter Queues (DLQ)

Documents that fail indexing (mapping conflict, parsing error, size limit exceeded) should
not be silently dropped.

```
Normal flow:
  Source --> Kafka --> Data Prepper --> OpenSearch

Error flow:
  Source --> Kafka --> Data Prepper --> OpenSearch (REJECT)
                           |
                           +--> DLQ (S3 bucket / Kafka DLQ topic)
                                  |
                                  +--> Alert: "X documents failed in last 5 min"
                                  +--> Manual review / replay after fix
```

Data Prepper DLQ configuration:

```yaml
log-pipeline:
  sink:
    - opensearch:
        hosts: ["https://opensearch:9200"]
        index: "logs-%{yyyy.MM.dd}"
        dlq:
          s3:
            bucket: "my-dlq-bucket"
            key_path_prefix: "dlq/logs/"
            region: "us-east-1"
```

### Schema Drift Handling

When services change their log format without coordination:

| Problem | Impact | Mitigation |
|---|---|---|
| New field added | Mapping explosion if dynamic=true | Use `dynamic: strict` or `dynamic: false` |
| Field type changes (string to int) | Mapping conflict, document rejected | Schema registry, validation at pipeline layer |
| Field removed | No impact (missing fields are fine) | None needed |
| Field value changes (enum grows) | Aggregation cardinality increases | Monitor cardinality, use `ignore_above` |

**Best practice**: Use a schema registry (e.g., Confluent Schema Registry with Kafka) to validate log schemas before they reach OpenSearch.

### Multi-Tenancy

| Approach | How It Works | Pros | Cons |
|---|---|---|---|
| **Separate indices** | `logs-teamA-*`, `logs-teamB-*` | Clean isolation, easy deletion | More shards, more ISM policies |
| **Filtered aliases** | Single `logs-*` index, alias with filter per team | Fewer shards | Complex alias management |
| **FGAC (document-level)** | Single index, DLS rules per role | Simplest index management | Performance overhead for DLS, complex security config |

For most organizations: **Separate indices per team/environment** provides the best balance
of isolation, performance, and operational simplicity.

### Cost Optimization Strategies

| Strategy | Savings | Trade-off |
|---|---|---|
| **Hot-warm-cold tiering** | 60-80% | Slower queries on older data |
| **Reduce replicas on warm/cold** | 50% storage | Lower availability (acceptable for older logs) |
| **best_compression codec** | 30-40% | Slightly slower indexing |
| **Drop DEBUG logs in pipeline** | 40-60% volume | Cannot search debug logs |
| **Sample high-volume logs** | Varies | Lose some events (OK for metrics, bad for audit) |
| **Disable _source on high-volume** | 50-70% storage | Cannot reindex, cannot view original |
| **Shorter hot retention** | Linear savings | Queries on older data are slower |
| **Reserved Instances** | 30-40% compute | Upfront commitment |

### Compliance

**GDPR Right to Deletion:**
```
Challenge: Logs are in immutable Lucene segments. You cannot delete a single document
           efficiently from a time-based index.

Options:
1. Delete entire index (if retention period aligns with GDPR request deadline)
2. Delete-by-query + force-merge (expensive, creates new segments)
3. Anonymize PII at ingestion (best: no PII stored = nothing to delete)
4. Use a separate PII-mapped field with hashing

Best practice: Anonymize or hash PII fields in the ingestion pipeline so OpenSearch
never stores raw PII.
```

**Audit Logging:**
```
OpenSearch Security audit logs track:
- Who authenticated and when
- What indices they accessed
- What queries they ran
- What documents they viewed/modified

Enable via: opensearch-security plugin configuration
Store audit logs in a separate, protected index with longer retention.
```

---

## 10. Comparison: Splunk vs Datadog vs ELK vs OpenSearch

### Feature Comparison

| Feature | Splunk | Datadog | ELK (Elastic) | OpenSearch |
|---|---|---|---|---|
| **License** | Proprietary | SaaS only | Elastic License 2.0 (source-available) | Apache 2.0 (truly open-source) |
| **Deployment** | On-prem or Cloud | SaaS only | Self-managed or Elastic Cloud | Self-managed or AWS Managed |
| **Query language** | SPL (proprietary) | Custom query syntax | KQL / Lucene / EQL | DQL / Lucene / SQL / PPL |
| **Ingest pipeline** | Heavy Forwarders | Agent (Datadog Agent) | Logstash / Elastic Agent | Data Prepper / Logstash |
| **Alerting** | Built-in, mature | Built-in, mature | Built-in (X-Pack) | Built-in (Alerting plugin) |
| **ML / Anomaly** | MLTK (powerful) | Built-in ML | ML (X-Pack, paid) | RCF (built-in, free) |
| **APM / Traces** | Splunk APM | Built-in APM | Elastic APM | Trace Analytics (Data Prepper) |
| **SIEM** | Splunk Enterprise Security | Cloud SIEM | Elastic SIEM | Security Analytics |
| **Dashboards** | Splunk Dashboards | Built-in | Kibana | OpenSearch Dashboards |
| **Maturity** | 20+ years | ~10 years | ~14 years | ~3 years (fork of ES 7.10) |

### Cost Model Comparison

| Platform | Pricing Model | Estimate for 1 TB/day | Notes |
|---|---|---|---|
| **Splunk** | Per GB ingested/day | $150K-$300K/year | Most expensive, but mature and feature-rich |
| **Datadog** | Per GB ingested + retention | $100K-$200K/year | Includes APM, infra monitoring; costs add up fast |
| **ELK (self-managed)** | Infrastructure only | $40K-$80K/year | Cheapest infra, but high ops cost; some features require paid license |
| **ELK (Elastic Cloud)** | Per node-hour + storage | $60K-$120K/year | Managed, but Elastic License restricts some uses |
| **OpenSearch (self-managed)** | Infrastructure only | $40K-$80K/year | Same infra cost as ELK, fully open-source |
| **OpenSearch (AWS Managed)** | Per node-hour + storage | $50K-$100K/year | ~30% premium over self-managed, includes ops |

### When to Choose Each

| Choose This | When |
|---|---|
| **Splunk** | Enterprise with existing Splunk investment, need mature SIEM, budget is not primary constraint, team knows SPL |
| **Datadog** | Want unified SaaS platform (logs + APM + infra), willing to pay premium for zero-ops, cloud-native |
| **ELK / Elastic** | Need Elastic-specific features (EQL, Canvas, Elastic Agent), OK with Elastic License, existing Elastic expertise |
| **OpenSearch** | Need true open-source (Apache 2.0), on AWS, want managed service, concerned about vendor lock-in from Elastic License |

### Migration Considerations

```
ELK --> OpenSearch:
  - API-compatible up to ES 7.10 (the fork point)
  - Index formats compatible (can snapshot-restore)
  - Kibana dashboards exportable to OpenSearch Dashboards (with minor edits)
  - Post-7.10 Elastic features (EQL, runtime fields) NOT available in OpenSearch
  - Security plugin differs: X-Pack Security vs OpenSearch Security plugin

Splunk --> OpenSearch:
  - No direct migration path; must re-architect ingest pipeline
  - SPL queries must be rewritten in OpenSearch query DSL or PPL
  - Splunk's heavy forwarder replaced with Fluent Bit + Data Prepper
  - Dashboard migration is manual
  - Typical timeline: 3-6 months for large deployments
```

---

## 11. Interview Cheat Sheet

### One-Paragraph Pipeline Summary

> "Application and infrastructure logs are collected by lightweight agents (Fluent Bit or
> Filebeat) running on every host or as Kubernetes sidecars. These agents forward logs to a
> durable buffer layer (Kafka or Kinesis) that decouples producers from consumers and absorbs
> traffic bursts. A processing layer (Data Prepper or Logstash) consumes from the buffer,
> parses unstructured logs with grok patterns, enriches with GeoIP and metadata, filters out
> noise like DEBUG logs, and bulk-indexes into OpenSearch using time-based rollover indices.
> An ISM policy automatically moves indices through hot (SSD, 7 days), warm (UltraWarm, 30
> days), cold (S3, 90 days), and delete stages. Developers search via OpenSearch Dashboards,
> monitors trigger alerts to Slack/PagerDuty on error spikes, and RCF anomaly detection catches
> unusual patterns without manual threshold tuning."

### 5 Key Numbers to Remember

| Number | What It Means |
|---|---|
| **50 GB** | Target shard size --- rollover index when it reaches 50 GB |
| **30 seconds** | `refresh_interval` for log workloads (default 1s is too aggressive) |
| **1.45x** | Storage overhead multiplier (raw data x 1.45 = index size before replicas) |
| **20 shards / GB heap** | Max shard count per node (e.g., 32 GB heap = 640 shards max) |
| **83%** | Approximate cost savings from hot-warm-cold tiering vs all-hot |

### Common Interviewer Probes and Short Answers

**"How do you handle a burst of logs during a deployment?"**
> The Kafka/Kinesis buffer absorbs the burst. Consumers (Data Prepper) process at their own
> pace. If OpenSearch cannot keep up, the buffer retains data for replay. Applications never
> experience backpressure.

**"What happens if OpenSearch goes down for maintenance?"**
> The buffer layer retains all logs during the outage window (sized for 24h+). When OpenSearch
> comes back, consumers replay from the last committed offset. Zero data loss.

**"How do you prevent a single noisy service from overwhelming the cluster?"**
> Rate limiting at the pipeline layer (Data Prepper can drop or sample), per-service index
> isolation (so one service's volume doesn't impact another's shard count), and bulk queue
> monitoring with backpressure signals.

**"How do you search across 90 days of logs?"**
> The read alias (`logs-read`) spans all indices across all tiers. Hot-tier indices respond
> in milliseconds, warm-tier in seconds. For cold-tier queries, you must first attach the
> index (minutes), so prefer time-bounded queries. OpenSearch prunes indices that fall outside
> the query's time range.

**"How do you handle high cardinality fields like user IDs?"**
> Use `keyword` type with `ignore_above: 256`. Avoid terms aggregations on high-cardinality
> fields (millions of unique values). Use `composite` aggregation with pagination instead.
> For very high cardinality (100M+ unique values), consider pre-aggregation in the pipeline.

**"What's the difference between Data Prepper and Logstash?"**
> Logstash is JVM-based, has 200+ plugins, and has been the standard for years. Data Prepper
> is OpenSearch-native, purpose-built for trace analytics and OTel, and integrates directly
> with OpenSearch features. Choose Logstash for complex transformations with existing expertise;
> choose Data Prepper for greenfield OpenSearch deployments, especially with tracing.

**"How do you handle GDPR log deletion requests?"**
> Best approach: anonymize PII at the ingestion pipeline layer so OpenSearch never stores raw
> PII. If PII is already stored, delete-by-query + force-merge is possible but expensive.
> Time-based index deletion aligns well if the retention period satisfies the GDPR timeline.

---
