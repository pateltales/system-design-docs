# Metrics Platform APIs — The Full API Surface

> A metrics/monitoring platform has seven core API groups:
> ingestion (the firehose), query (the read path), alerting (the critical path),
> dashboards (the UX path), service discovery (the control plane),
> metadata (the schema), and admin (the ops).

---

## Table of Contents

1. [Metrics Ingestion APIs](#1-metrics-ingestion-apis)
2. [Query APIs](#2-query-apis)
3. [Alerting APIs](#3-alerting-apis)
4. [Dashboard APIs](#4-dashboard-apis)
5. [Service Discovery / Target Management APIs](#5-service-discovery--target-management-apis)
6. [Metadata APIs](#6-metadata-apis)
7. [Admin / Configuration APIs](#7-admin--configuration-apis)
8. [Contrasts](#8-contrasts)

---

## 1. Metrics Ingestion APIs

The highest-throughput API — millions of data points per second.

### Push-Based Ingestion (Datadog model)

```
POST /v1/metrics
Content-Type: application/json
Authorization: Bearer <api-key>

{
  "series": [
    {
      "metric": "system.cpu.usage",
      "type": "gauge",
      "points": [
        [1710512340, 72.5],
        [1710512350, 73.1],
        [1710512360, 71.8]
      ],
      "tags": ["host:web-01", "env:prod", "service:api", "region:us-east-1"],
      "unit": "percent"
    },
    {
      "metric": "http.requests.total",
      "type": "counter",
      "points": [
        [1710512340, 154382],
        [1710512350, 154419],
        [1710512360, 154461]
      ],
      "tags": ["host:web-01", "method:GET", "status:200", "service:api"]
    }
  ]
}

Response:
202 Accepted
{ "status": "ok", "points_accepted": 6 }

Key design decisions:
  • 202 Accepted (not 200 OK): data is accepted for processing, not yet stored.
    Decouples ingestion from storage — the API returns immediately,
    Kafka/buffer handles the rest.
  • Batch format: multiple series, multiple points per series.
    One HTTP request carries hundreds of data points.
    At 10-second intervals and 100 metrics per host:
      Individual requests: 100 req/10s = 10 req/sec per host
      Batched: 1 req/10s = 0.1 req/sec per host
    For 10,000 hosts: 1,000 req/sec (batched) vs 100,000 req/sec (individual).
  • Tags as flat strings: "host:web-01" not {"host": "web-01"}.
    Compact format, fast parsing, easy to index.
  • Timestamp + value pairs: explicit timestamps from the agent.
    The server does NOT use its own wall clock — agent's clock
    is authoritative (handles clock skew with tolerance).
```

### Pull-Based Collection (Prometheus model)

```
The application exposes a /metrics endpoint in Prometheus exposition format.
The monitoring server scrapes this endpoint on a schedule.

GET /metrics HTTP/1.1
Host: web-01.prod.internal:9090

Response:
200 OK
Content-Type: text/plain; version=0.0.4

# HELP http_requests_total Total number of HTTP requests.
# TYPE http_requests_total counter
http_requests_total{method="GET",status="200"} 154461
http_requests_total{method="GET",status="404"} 842
http_requests_total{method="POST",status="200"} 28934
http_requests_total{method="POST",status="500"} 17

# HELP http_request_duration_seconds HTTP request latency histogram.
# TYPE http_request_duration_seconds histogram
http_request_duration_seconds_bucket{le="0.01"} 45231
http_request_duration_seconds_bucket{le="0.05"} 98234
http_request_duration_seconds_bucket{le="0.1"} 112345
http_request_duration_seconds_bucket{le="0.5"} 120456
http_request_duration_seconds_bucket{le="1"} 121890
http_request_duration_seconds_bucket{le="+Inf"} 122001
http_request_duration_seconds_sum 4523.89
http_request_duration_seconds_count 122001

# HELP system_cpu_usage_percent Current CPU usage.
# TYPE system_cpu_usage_percent gauge
system_cpu_usage_percent 72.5

Key design decisions:
  • Text format: human-readable, easy to debug (curl the endpoint).
  • No timestamps: Prometheus adds the scrape timestamp.
    The application doesn't need to manage clocks.
  • Metric types declared: # TYPE tells the TSDB how to interpret
    the value (counter vs gauge vs histogram).
  • Histogram buckets: pre-defined boundaries (le="0.01", le="0.05", ...).
    Each bucket is a cumulative counter. Quantiles computed at query time.
```

### OpenTelemetry OTLP Protocol

```
gRPC-based protocol for metrics, traces, and logs:

POST /opentelemetry.proto.collector.metrics.v1.MetricsService/Export
Content-Type: application/grpc

Protobuf message:
  ResourceMetrics {
    Resource { attributes: [("service.name", "api"), ("host.name", "web-01")] }
    ScopeMetrics {
      InstrumentationScope { name: "http-server", version: "1.0.0" }
      Metrics [
        {
          name: "http.server.request.duration"
          unit: "s"
          histogram {
            data_points [{
              start_time: 1710512340000000000
              time: 1710512350000000000
              count: 1523
              sum: 234.56
              bucket_counts: [450, 520, 300, 150, 80, 23]
              explicit_bounds: [0.01, 0.05, 0.1, 0.5, 1.0]
              attributes: [("http.method", "GET"), ("http.status_code", "200")]
            }]
          }
        }
      ]
    }
  }

Response: ExportMetricsServiceResponse { }

Key design decisions:
  • Protobuf + gRPC: more efficient than JSON (5-10x smaller on wire).
  • Unified protocol: same OTLP protocol for metrics, traces, and logs.
    Instrument once, export to any backend.
  • Semantic conventions: standardized attribute names
    (http.method, http.status_code, service.name) enable cross-vendor
    compatibility and automatic dashboard templates.
```

---

## 2. Query APIs

### Time-Series Query

```
GET /v1/query
  ?metric=system.cpu.usage
  &tags=host:web-01,env:prod
  &start=1710508800
  &end=1710512400
  &aggregation=avg
  &interval=60
  &group_by=host

Response:
{
  "series": [
    {
      "metric": "system.cpu.usage",
      "tags": {"host": "web-01", "env": "prod"},
      "points": [
        [1710508800, 45.2],
        [1710508860, 47.1],
        [1710508920, 44.8],
        ...
        [1710512340, 72.5]
      ],
      "aggregation": "avg",
      "interval": 60
    }
  ],
  "query_stats": {
    "series_scanned": 1,
    "points_scanned": 3600,
    "points_returned": 60,
    "execution_time_ms": 23
  }
}

Query parameters:
  metric:      metric name (required)
  tags:        filter by tag key-value pairs (comma-separated)
  start/end:   time range as Unix timestamps (required)
  aggregation: avg, sum, min, max, count, p50, p95, p99
  interval:    output resolution in seconds (auto-selected if omitted)
  group_by:    split result by tag key (produces multiple series)
```

### PromQL-Compatible Query (for Prometheus/Thanos/Mimir backends)

```
GET /api/v1/query_range
  ?query=avg(rate(http_requests_total{service="api"}[5m])) by (host)
  &start=1710508800
  &end=1710512400
  &step=60

Response:
{
  "status": "success",
  "data": {
    "resultType": "matrix",
    "result": [
      {
        "metric": {"host": "web-01"},
        "values": [
          [1710508800, "234.5"],
          [1710508860, "241.2"],
          ...
        ]
      },
      {
        "metric": {"host": "web-02"},
        "values": [
          [1710508800, "189.3"],
          ...
        ]
      }
    ]
  }
}
```

### Arithmetic / Derived Metrics

```
POST /v1/query/expression
{
  "expression": "(rate(http_requests_total{status=~'5..'}[5m]) / rate(http_requests_total[5m])) * 100",
  "start": 1710508800,
  "end": 1710512400,
  "step": 60
}

This computes the error rate as a percentage:
  (5xx requests per second / total requests per second) × 100

The query engine must:
  1. Evaluate both subqueries independently
  2. Align timestamps (both series must have the same time points)
  3. Perform element-wise division
  4. Multiply by 100
```

---

## 3. Alerting APIs

### Create Alert Rule

```
POST /v1/alerts/rules
{
  "name": "High CPU on API servers",
  "query": "avg(system.cpu.usage){service:api}",
  "condition": {
    "operator": ">",
    "threshold": 90,
    "for": "5m"
  },
  "severity": "critical",
  "notifications": [
    { "channel": "pagerduty", "routing_key": "api-team-oncall" },
    { "channel": "slack", "webhook": "#ops-alerts" }
  ],
  "tags": ["team:api", "env:prod"],
  "runbook_url": "https://wiki.internal/runbooks/high-cpu",
  "message": "CPU usage on {{host}} is {{value}}% (threshold: 90%)"
}

Response:
{
  "id": "alert_abc123",
  "status": "created",
  "state": "OK"
}
```

### List Active Alerts

```
GET /v1/alerts/active?severity=critical&tags=team:api

Response:
{
  "alerts": [
    {
      "id": "alert_abc123",
      "name": "High CPU on API servers",
      "state": "FIRING",
      "fired_at": "2024-03-15T14:30:00Z",
      "value": 94.2,
      "tags": {"host": "web-03", "service": "api"},
      "severity": "critical",
      "notifications_sent": [
        { "channel": "pagerduty", "sent_at": "2024-03-15T14:30:15Z", "status": "delivered" },
        { "channel": "slack", "sent_at": "2024-03-15T14:30:16Z", "status": "delivered" }
      ]
    }
  ]
}
```

### Alert Lifecycle

```
POST /v1/alerts/alert_abc123/acknowledge
{ "acknowledged_by": "engineer@company.com", "note": "Investigating" }

POST /v1/alerts/alert_abc123/snooze
{ "duration": "30m", "reason": "Known issue, deploying fix" }

POST /v1/alerts/alert_abc123/resolve
{ "resolved_by": "engineer@company.com", "resolution": "Scaled up API fleet" }

Alert states:
  OK → PENDING → FIRING → ACKNOWLEDGED → RESOLVED
                    ↓                         ↑
              (auto-resolves when metric      │
               drops below threshold) ────────┘
```

---

## 4. Dashboard APIs

```
POST /v1/dashboards
{
  "title": "API Service Health",
  "description": "Golden signals for the API service",
  "tags": ["team:api", "env:prod"],
  "template_variables": [
    { "name": "env", "default": "prod", "values": ["prod", "staging", "dev"] },
    { "name": "service", "default": "api", "query": "tag_values(service)" }
  ],
  "panels": [
    {
      "title": "Request Rate",
      "type": "timeseries",
      "position": { "x": 0, "y": 0, "width": 6, "height": 4 },
      "query": "sum(rate(http_requests_total{service=$service,env=$env}[5m])) by (status)",
      "display": {
        "line_width": 2,
        "fill_opacity": 0.1,
        "legend_position": "bottom"
      }
    },
    {
      "title": "P95 Latency",
      "type": "timeseries",
      "position": { "x": 6, "y": 0, "width": 6, "height": 4 },
      "query": "histogram_quantile(0.95, rate(http_request_duration_seconds_bucket{service=$service}[5m]))",
      "display": {
        "unit": "seconds",
        "thresholds": [
          { "value": 0.5, "color": "yellow" },
          { "value": 1.0, "color": "red" }
        ]
      }
    },
    {
      "title": "Error Rate",
      "type": "gauge",
      "position": { "x": 0, "y": 4, "width": 3, "height": 3 },
      "query": "(rate(http_requests_total{status=~'5..'}[5m]) / rate(http_requests_total[5m])) * 100",
      "display": {
        "unit": "percent",
        "thresholds": [
          { "value": 1, "color": "yellow" },
          { "value": 5, "color": "red" }
        ]
      }
    },
    {
      "title": "CPU Usage",
      "type": "heatmap",
      "position": { "x": 3, "y": 4, "width": 9, "height": 3 },
      "query": "system.cpu.usage{service=$service,env=$env}",
      "display": { "group_by": "host" }
    }
  ]
}

Response:
{
  "id": "dash_xyz789",
  "url": "/dashboards/dash_xyz789",
  "created_at": "2024-03-15T14:30:00Z"
}
```

### Dashboard Retrieval with Rendered Data

```
GET /v1/dashboards/dash_xyz789?start=1710508800&end=1710512400

Response includes both the dashboard definition AND the query results
for each panel, so the frontend can render immediately without
making additional API calls.

This is an optimization: instead of loading the dashboard definition,
then making N separate query API calls (one per panel),
the backend executes all panel queries in parallel and returns
everything in one response.
```

---

## 5. Service Discovery / Target Management APIs

For pull-based collection — the server needs to know WHAT to scrape.

```
GET /v1/targets

Response:
{
  "active_targets": [
    {
      "endpoint": "http://web-01.prod:9090/metrics",
      "labels": {"job": "api-server", "env": "prod", "host": "web-01"},
      "scrape_interval": "15s",
      "last_scrape": "2024-03-15T14:30:00Z",
      "last_scrape_duration": "0.023s",
      "health": "up",
      "last_error": ""
    },
    {
      "endpoint": "http://web-02.prod:9090/metrics",
      "labels": {"job": "api-server", "env": "prod", "host": "web-02"},
      "scrape_interval": "15s",
      "last_scrape": "2024-03-15T14:30:01Z",
      "health": "up"
    },
    {
      "endpoint": "http://db-01.prod:9090/metrics",
      "labels": {"job": "database", "env": "prod"},
      "scrape_interval": "30s",
      "last_scrape": "2024-03-15T14:29:45Z",
      "health": "down",
      "last_error": "connection refused"
    }
  ],
  "dropped_targets": [
    {
      "endpoint": "http://web-old.prod:9090/metrics",
      "reason": "relabel_config dropped target (label env=deprecated)"
    }
  ]
}

POST /v1/targets
{
  "endpoint": "http://new-service.prod:9090/metrics",
  "labels": {"job": "new-service", "env": "prod"},
  "scrape_interval": "15s"
}

Kubernetes service discovery:
  Instead of manually registering targets, the collector watches
  the Kubernetes API for pod/service changes and auto-discovers
  scrape targets. Annotations on pods define scrape configuration:

  annotations:
    prometheus.io/scrape: "true"
    prometheus.io/port: "9090"
    prometheus.io/path: "/metrics"
```

---

## 6. Metadata APIs

```
GET /v1/metrics/metadata

Response:
{
  "metrics": [
    {
      "name": "http_requests_total",
      "type": "counter",
      "description": "Total number of HTTP requests",
      "unit": "requests",
      "cardinality": 12500,
      "labels": ["method", "status", "service", "host", "env"]
    },
    {
      "name": "http_request_duration_seconds",
      "type": "histogram",
      "description": "HTTP request latency distribution",
      "unit": "seconds",
      "cardinality": 62500,
      "labels": ["method", "status", "service", "host", "env", "le"]
    },
    {
      "name": "system.cpu.usage",
      "type": "gauge",
      "description": "Current CPU usage percentage",
      "unit": "percent",
      "cardinality": 500,
      "labels": ["host", "env", "cpu_core"]
    }
  ]
}

GET /v1/tags?metric=http_requests_total

Response:
{
  "metric": "http_requests_total",
  "tags": {
    "method": ["GET", "POST", "PUT", "DELETE", "PATCH"],
    "status": ["200", "201", "204", "400", "401", "403", "404", "500", "502", "503"],
    "service": ["api", "auth", "payments", "notifications"],
    "host": ["web-01", "web-02", "web-03", ...],
    "env": ["prod", "staging", "dev"]
  },
  "cardinality": 12500,
  "cardinality_warning": false
}

Cardinality tracking is critical:
  GET /v1/metrics/cardinality/top?limit=10

  Response:
  {
    "top_metrics_by_cardinality": [
      { "metric": "http_request_duration_seconds_bucket", "cardinality": 625000, "warning": true },
      { "metric": "http_requests_total", "cardinality": 12500 },
      ...
    ],
    "total_active_series": 1250000,
    "cardinality_limit": 10000000
  }
```

---

## 7. Admin / Configuration APIs

```
POST /v1/retention
{
  "policies": [
    { "resolution": "raw", "retention": "15d" },
    { "resolution": "1m", "retention": "90d" },
    { "resolution": "5m", "retention": "365d" },
    { "resolution": "1h", "retention": "730d" }
  ]
}

POST /v1/downsampling
{
  "rules": [
    {
      "input_resolution": "raw",
      "output_resolution": "1m",
      "aggregations": ["avg", "min", "max", "sum", "count"],
      "delay": "15d"
    },
    {
      "input_resolution": "1m",
      "output_resolution": "5m",
      "aggregations": ["avg", "min", "max"],
      "delay": "90d"
    }
  ]
}

GET /v1/health

Response:
{
  "status": "healthy",
  "components": {
    "ingestion": { "status": "healthy", "rate": "1.2M points/sec", "lag": "0.5s" },
    "storage": { "status": "healthy", "disk_usage": "72%", "series_count": "8.5M" },
    "query_engine": { "status": "healthy", "p99_latency": "1.2s" },
    "alerting": { "status": "healthy", "rules_evaluated": 450, "firing": 3 },
    "compactor": { "status": "healthy", "last_compaction": "2024-03-15T14:00:00Z" }
  }
}
```

---

## 8. Contrasts

### Push (Datadog) vs Pull (Prometheus) API Design

| Aspect | Push (Datadog) | Pull (Prometheus) |
|---|---|---|
| **Ingestion API** | `POST /v1/metrics` (agent pushes batches) | `GET /metrics` (server scrapes target) |
| **Who initiates?** | Agent → Server | Server → Target |
| **Health detection** | Separate health check needed | Scrape failure = target down (free signal) |
| **Short-lived jobs** | Push metrics before process exits | Pushgateway (workaround) |
| **Firewall/NAT** | Works (outbound push) | Doesn't work (server can't reach inside) |
| **Rate control** | Server must handle any rate agents send | Server controls scrape frequency |

### Proprietary vs OpenTelemetry Protocol

| Aspect | Proprietary (Datadog API) | OTLP (OpenTelemetry) |
|---|---|---|
| **Format** | JSON over HTTPS | Protobuf over gRPC (or HTTP) |
| **Signals** | Metrics only (separate APIs for traces/logs) | Unified: metrics + traces + logs |
| **Vendor lock-in** | Yes (Datadog-specific format) | No (vendor-neutral, change exporter) |
| **Efficiency** | JSON: ~200 bytes per data point | Protobuf: ~20-40 bytes per data point |
| **Adoption** | Datadog customers | Industry standard (CNCF graduated) |
