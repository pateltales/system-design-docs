# Observability & Monitoring — Deep Dive

---

## The Three Pillars of Observability

### Pillar 1: Logs

Structured access logs per request.

**JSON log entry:**

```json
{
  "timestamp": "2025-06-15T14:23:07.123Z",
  "request_id": "req-a4f8c3e2",
  "client_ip": "203.0.113.42",
  "method": "POST",
  "path": "/api/v1/orders",
  "host": "api.example.com",
  "status": 201,
  "latency_ms": {
    "total": 152,
    "gateway": 4,
    "upstream": 148
  },
  "upstream": "order-service:8080",
  "route": "order-create-v1",
  "consumer": "mobile-app-ios",
  "user_agent": "MobileApp/3.2.1",
  "request_size_bytes": 1024,
  "response_size_bytes": 256,
  "rate_limit": {
    "remaining": 842,
    "limit": 1000
  },
  "trace_id": "4bf92f3577b34da6a3ce929d0e0e4736"
}
```

**Log levels mapped to HTTP status:**

| Status Range | Log Level | Sample Rate |
|---|---|---|
| 2xx, 3xx | INFO | 10% (sampled) |
| 4xx | WARN | 100% |
| 5xx | ERROR | 100% |

**Why 10% sampling for successes?** At 100K RPS, logging every request generates ~8.6 billion log entries per day. At ~500 bytes per entry, that's ~4 TB/day. Sampling successes at 10% reduces to ~400 GB/day while still providing statistical visibility. Always log 100% of errors — those are the ones you need to debug.

### Pillar 2: Metrics

Time-series numerical data. Export to Prometheus (pull-based) or StatsD/Datadog (push-based).

**Key metrics:**

| Metric | Type | Labels | Purpose |
|---|---|---|---|
| `gateway_requests_total` | Counter | `method`, `route`, `status`, `consumer` | Request rate (RPS) per dimension |
| `gateway_request_duration_seconds` | Histogram | `method`, `route`, `upstream` | Latency percentiles (p50, p95, p99) |
| `gateway_upstream_duration_seconds` | Histogram | `upstream` | Upstream response time (separate from gateway overhead) |
| `gateway_errors_total` | Counter | `route`, `status`, `error_type` | Error rate by type |
| `gateway_active_connections` | Gauge | `direction=client\|upstream` | Current concurrent connections |
| `gateway_circuit_breaker_state` | Gauge | `upstream`, `state` | Circuit breaker health per upstream |
| `gateway_rate_limiter_rejected_total` | Counter | `consumer`, `route` | Rate limit rejections |
| `gateway_cache_requests_total` | Counter | `route`, `status=hit\|miss` | Cache effectiveness |

**Why histograms, not averages?** A 50ms average with a 3-second p99 is a production disaster hiding behind a good-looking number. Histograms capture the full distribution.

**Latency breakdown:** Split into gateway processing time vs upstream response time. This immediately answers: "Is the gateway slow, or is the backend slow?"

### Pillar 3: Distributed Tracing

Follow a request from entry to exit across all services.

**How it works at the gateway:**
1. Receive request — check for incoming trace context headers
2. If no context exists, generate new trace ID and root span
3. If context exists, join the existing trace
4. Create a span: start time, name, attributes (route, upstream, status)
5. Forward trace headers to upstream
6. Close span when upstream responds

**Trace context header standards:**

| Standard | Header | Format | Used By |
|---|---|---|---|
| W3C Trace Context | `traceparent`, `tracestate` | `00-{trace-id}-{span-id}-{flags}` | OpenTelemetry (default) |
| Zipkin B3 | `X-B3-TraceId`, `X-B3-SpanId` | Separate headers | Zipkin, Spring Cloud Sleuth |
| Jaeger | `uber-trace-id` | `{trace-id}:{span-id}:{parent}:{flags}` | Jaeger (legacy) |

**Example trace visualization:**

```
Trace ID: 4bf92f3577b34da6

[api-gateway] ─────────────────────────────────────── 152ms
├── auth-check         0-8ms
├── rate-limit-check   8-9ms
├── request-transform  9-11ms
├── upstream-proxy     11-148ms
│   └── [order-service] ───────────────────────── 134ms
│       ├── validate     14-16ms
│       ├── check-inventory  16-89ms
│       │   └── [inventory-service]  19-86ms
│       │       └── db-query  22-83ms  ← SLOW
│       ├── create-order     102-138ms
│       └── publish-event    138-143ms
└── response-transform 148-150ms
```

This immediately reveals the bottleneck: `inventory-service > db-query` took 61ms.

---

## Request ID / Correlation ID

The simplest and most universally useful observability primitive.

```
Client → Gateway → Upstream Services

No X-Request-Id from client:
  Gateway generates: X-Request-Id: req-a4f8c3e2
  Forwards to upstream, returns to client

Client sends X-Request-Id:
  Gateway preserves it (client needs it for their own correlation)
```

**Rules:**
- Every log entry includes `request_id`
- Every forwarded request includes `X-Request-Id` header
- Every response includes `X-Request-Id` header
- If client sends one, preserve it (don't overwrite)

**Why preserve client-sent IDs?** Mobile apps and partner systems generate their own correlation IDs. Preserving them enables end-to-end debugging spanning even the client side.

---

## Real-Time Dashboards

### Stack: Grafana + Prometheus

```
API Gateway ──scrape /metrics──► Prometheus (TSDB) ◄──PromQL──► Grafana (dashboards)
```

### Dashboard Organization

| Dashboard | Panels | Audience |
|---|---|---|
| Gateway Overview | Total RPS, error rate, p50/p95/p99, active connections, top routes | SRE / On-call |
| Per-Route Detail | RPS, error rate, latency percentiles, cache hit rate | Backend team |
| Per-Upstream Detail | Upstream latency, connection pool, circuit breaker state | Backend team |
| Per-Consumer Detail | Consumer RPS, rate-limit proximity, top routes | API platform team |
| Security | 401/403 rate, rate-limit rejections, IP blocks, JWT failures | Security team |

### Alerting Rules

```yaml
# Prometheus alert: high error rate
- alert: GatewayHighErrorRate
  expr: |
    rate(gateway_requests_total{status=~"5.."}[5m])
    / rate(gateway_requests_total[5m]) * 100 > 5
  for: 2m
  labels:
    severity: critical
  annotations:
    summary: "API Gateway 5xx error rate above 5%"

# Prometheus alert: circuit breaker opened
- alert: GatewayCircuitBreakerOpen
  expr: gateway_circuit_breaker_state{state="open"} == 1
  for: 0m
  labels:
    severity: critical
  annotations:
    summary: "Circuit breaker OPEN for {{ $labels.upstream }}"

# High p99 latency
- alert: GatewayHighP99Latency
  expr: |
    histogram_quantile(0.99, rate(gateway_request_duration_seconds_bucket[5m])) > 2.0
  for: 5m
  labels:
    severity: warning
```

---

## Audit Logging

Fundamentally different from access logs. Access logs = data-plane traffic. Audit logs = **control-plane actions** (who changed the system's configuration).

| Action | Fields Captured |
|---|---|
| Route created/updated/deleted | Actor, timestamp, route ID, previous config, new config |
| Plugin enabled/disabled | Actor, timestamp, plugin name, scope, config diff |
| Rate limit changed | Actor, timestamp, old limit, new limit |
| Certificate uploaded/rotated | Actor, timestamp, domain, fingerprint, expiry |

**Compliance:** SOX, HIPAA, PCI-DSS all require immutable audit trails. Store in append-only storage (S3 with Object Lock, immutable Elasticsearch indices).

---

## Log Aggregation Pipeline

```
GW Instances (JSON logs)
       │
       ▼
Log Shipper (Fluent Bit / Fluentd / Filebeat)
  - Parse JSON, enrich (pod name, region, env)
  - Buffer and batch
       │
       ▼
Log Storage (Elasticsearch / Splunk / CloudWatch / Loki)
  - Full-text search
  - Structured field queries
  - Retention policies
```

| Shipper | Language | Memory | Best For |
|---|---|---|---|
| Fluent Bit | C | ~5 MB | K8s sidecar, resource-constrained |
| Fluentd | Ruby + C | ~40 MB | Complex routing, many output plugins |
| Filebeat | Go | ~30 MB | Elastic ecosystem |

---

## Gateway Contrasts

### Kong
- Plugins: `file-log`, `http-log`, `prometheus`, `zipkin`, `opentelemetry`
- Prometheus plugin: per-route, per-service, per-consumer labels with latency histograms
- Audit logging: Enterprise only

### Envoy
- Built-in access logging (file, gRPC), metrics (Prometheus), tracing (Zipkin, Jaeger, OTel)
- **Gold standard for proxy observability** — metrics per-cluster, per-route, per-method, per-status, per-retry
- Thousands of stats out of the box

### AWS API Gateway
- CloudWatch Logs (JSON/CSV), CloudWatch Metrics, X-Ray for tracing
- Limited customization — predefined `$context` variables
- No Prometheus-native integration
- Per-method metrics available but costs extra

### Comparison

| Feature | Kong | Envoy | AWS API Gateway |
|---|---|---|---|
| Metrics export | Prometheus `/metrics` | Prometheus, StatsD, DogStatsD | CloudWatch only |
| Metrics granularity | Per-route, service, consumer | Per-cluster, route, method, status, retry | Per-stage, optional per-method |
| Tracing | Zipkin/OTel plugins | Built-in Zipkin/Jaeger/OTel | X-Ray |
| Audit logging | Enterprise only | Via admin access log | CloudTrail |
| Customizability | High | Very high | Low |

---

## Interview Key Points

1. **"We monitor RED: Rate, Errors, Duration."** Concise, correct framing.
2. **"Averages lie. We use p50/p95/p99 histograms."** Shows you understand tail latency.
3. **"Gateway generates Request ID, includes in every log, forwards to upstreams, returns to client."** Cheapest, highest-value observability feature.
4. **"We break latency into gateway time vs upstream time."** Instantly identifies where slowness lives.
5. **"Audit logs are separate from access logs — control-plane changes for compliance."** Shows operational maturity.
6. **"Sample successes at 10%, log 100% of errors."** Shows you think about observability cost at scale.
