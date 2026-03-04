# Dashboards, Panels & Visualization

## The Daily Interface — How Engineers Interact with the Monitoring System

Dashboards are how 95% of engineers interact with a monitoring system daily. They don't write PromQL queries from scratch or configure alert rules — they open a dashboard, scan the graphs, and decide whether everything is healthy. A dashboard that loads in 5 seconds instead of 2 seconds feels sluggish. A dashboard with 30 poorly organized panels causes more confusion than clarity.

---

## 1. Dashboard Architecture

### Conceptual Model

```
Dashboard
├── Global controls
│   ├── Time range picker (last 1h, last 24h, last 7d, custom)
│   ├── Auto-refresh interval (off, 10s, 30s, 1m, 5m)
│   └── Template variables (environment: prod/staging, region: us-east/eu-west)
│
├── Row: "Request Traffic"
│   ├── Panel: Total Request Rate (time-series line chart)
│   │   └── Query: sum(rate(http_requests_total[5m])) by (service)
│   ├── Panel: Error Rate % (time-series line chart, thresholds colored)
│   │   └── Query: sum(rate(http_requests_total{status=~"5.."}[5m])) / sum(rate(http_requests_total[5m])) * 100
│   └── Panel: P99 Latency (time-series line chart)
│       └── Query: histogram_quantile(0.99, sum(rate(http_request_duration_seconds_bucket[5m])) by (le))
│
├── Row: "Infrastructure"
│   ├── Panel: CPU Usage by Host (time-series, stacked area)
│   ├── Panel: Memory Usage (gauge, showing current %)
│   └── Panel: Disk I/O (time-series line chart)
│
└── Row: "Alerts"
    ├── Panel: Active Alerts (table, filtered by service)
    └── Panel: Alert History (timeline/annotations)
```

### How Dashboard Rendering Works

```
User opens dashboard URL:

1. LOAD DASHBOARD DEFINITION
   Fetch JSON from dashboard store (Postgres/MySQL)
   Contains: panel layout, queries, visualization options, variable definitions

2. RESOLVE TEMPLATE VARIABLES
   Replace $environment → "prod", $region → "us-east-1"
   These propagate into every panel's query

3. EXECUTE ALL PANEL QUERIES IN PARALLEL
   Dashboard with 20 panels → 20 concurrent queries to the TSDB
   Each query: /api/v1/query_range?query=...&start=...&end=...&step=...

4. PROGRESSIVE RENDERING
   Render each panel as soon as its query returns
   Don't wait for all queries — show partial results immediately
   Slowest panel doesn't block the fastest panel

5. STREAMING UPDATES (if auto-refresh is on)
   Every refresh_interval:
     Re-execute all queries with updated time range
     Update charts with new data points (smooth animation)
     Only fetch the delta (new data since last refresh) if supported
```

---

## 2. Panel Types and When to Use Each

### Time-Series Line Chart (Most Common)

```
Use for: Values that change over time
Examples: Request rate, latency percentiles, CPU usage, error rate

  Requests/sec
  300 ┤              ╭─╮
  250 ┤          ╭───╯ ╰──╮
  200 ┤      ╭───╯        ╰───╮
  150 ┤  ╭───╯                ╰───╮
  100 ┤──╯                        ╰──
      └──────────────────────────────
      9am    10am    11am    12pm   1pm

Best practices:
  • Use rate() for counters (raw counter values are misleading)
  • Multiple series on one chart: group by service or host
  • Color-code: green = good, yellow = warning, red = critical
  • Add threshold lines (horizontal dashed line at SLO target)
```

### Heatmap (for Distributions)

```
Use for: Histogram data — see the full distribution, not just averages
Examples: Request latency distribution, response size distribution

  Latency
  1000ms ░░░░░░░░░░░░░░░░░░░░░░░░
  500ms  ░░░░█░░█████░░░░░░░░░░░░
  100ms  ███████████████████░░░░██
  50ms   ████████████████████████
  10ms   ████████████████████████
         ────────────────────────
         9am  10am  11am  12pm

  ░ = few requests in this bucket
  █ = many requests in this bucket

Why heatmap > line chart for latency:
  A line chart of P99 shows ONE number.
  A heatmap shows the FULL distribution — you can see:
  • Bimodal distributions (cache hit vs miss)
  • Long tails that P99 misses
  • Shifts in distribution shape over time
```

### Gauge (Current Value)

```
Use for: Single current values that have a natural range
Examples: CPU %, memory %, disk usage %, queue depth, connection pool utilization

  ╭───────────╮
  │   72%     │
  │  Memory   │
  │  ████████░░│
  ╰───────────╯

  Green: 0-70%    Yellow: 70-85%    Red: 85-100%

Best practice: Set color thresholds that match alert thresholds.
If the gauge is red, an alert should be firing (or about to fire).
```

### Single Stat / Big Number

```
Use for: Key metrics that should be visible at a glance
Examples: Current request rate, active users, error count today

  ┌─────────────────┐
  │                  │
  │     12,547       │
  │  requests/sec    │
  │    ▲ 15%         │
  │ (vs yesterday)   │
  │                  │
  └─────────────────┘

Include trend indicator (up/down arrow with %) for context.
A number without context is less useful than a number with a comparison baseline.
```

### Table

```
Use for: Comparing values across many items
Examples: Top 10 endpoints by latency, per-host resource usage, active alerts

  ┌────────────┬──────────┬─────────┬──────────┐
  │ Endpoint   │ QPS      │ P99 (ms)│ Error %  │
  ├────────────┼──────────┼─────────┼──────────┤
  │ /api/users │ 2,340    │ 45      │ 0.1%     │
  │ /api/orders│ 1,890    │ 120     │ 0.5%     │
  │ /api/search│ 5,670    │ 230     │ 0.3%     │
  │ /api/auth  │ 890      │ 35      │ 2.1%  ⚠ │
  └────────────┴──────────┴─────────┴──────────┘

  Sortable columns. Color-code cells based on thresholds.
  Clicking a row → drills down to that endpoint's detailed dashboard.
```

---

## 3. Template Variables — Making Dashboards Reusable

### The Problem

Without template variables, you need separate dashboards for:
- Production vs staging vs development
- US-East vs EU-West vs AP-Southeast
- Each individual service

That's `3 environments × 3 regions × 20 services = 180 dashboards` to maintain. Any change must be applied to all 180.

### The Solution

```
One dashboard with template variables:

  Variables:
    $environment = [prod, staging, dev]     ← dropdown at top of dashboard
    $region      = [us-east, eu-west, ap-se]
    $service     = [api, auth, payments, ...]

  Panel query:
    rate(http_requests_total{env="$environment", region="$region", service="$service"}[5m])

  When user selects env=prod, region=us-east, service=api:
    rate(http_requests_total{env="prod", region="us-east", service="api"}[5m])

  One dashboard serves all 180 combinations.
  Change the dashboard once → all combinations updated.
```

### Variable Cascading

Variables can depend on each other:

```
$environment → fetches available regions for that environment
$region      → fetches available services in that region
$service     → fetches available instances of that service

API call for variable values:
  GET /api/v1/label/region/values?match[]=up{env="$environment"}
  → Returns: ["us-east", "eu-west"]  (only regions where env=prod has targets)

This prevents showing invalid combinations
(e.g., service "payments-v2" doesn't exist in staging).
```

---

## 4. Query Optimization for Dashboards

Dashboards are the heaviest read workload on the monitoring system.

### Auto-Resolution (Step Alignment)

```
The query engine automatically selects data resolution based on:
  1. Query time range
  2. Panel pixel width

  ┌────────────────────────────────────────────────────────┐
  │ Time Range │ Panel Width │ Ideal Step │ Data Points    │
  ├────────────┼─────────────┼────────────┼────────────────┤
  │ 1 hour     │ 500 px      │ 7 seconds  │ ~500 points    │
  │ 24 hours   │ 500 px      │ 3 minutes  │ ~480 points    │
  │ 7 days     │ 500 px      │ 20 minutes │ ~504 points    │
  │ 30 days    │ 500 px      │ 1.5 hours  │ ~480 points    │
  │ 1 year     │ 500 px      │ 17.5 hours │ ~500 points    │
  └────────────┴─────────────┴────────────┴────────────────┘

  For a 1-year chart on a 500px panel:
    Raw data (10s): 365 × 24 × 360 = 31.5 million points per series
    Auto-resolution: ~500 points per series (using hourly rollups)
    Data reduction: 63,000x fewer points to fetch and render

  Grafana computes the step automatically:
    step = max(scrape_interval, (end - start) / panel_width_px)
  Then sends it in the query_range request.
```

### Dashboard Query Caching

```
Cache architecture:
  ┌──────────┐     ┌──────────┐     ┌──────────┐
  │Dashboard │────>│  Cache   │────>│  TSDB    │
  │ Frontend │     │ (Redis/  │     │ (query   │
  │          │<────│ Memcached)│<────│  engine) │
  └──────────┘     └──────────┘     └──────────┘

Cache key: hash(query + time_range + step + variables)
Cache TTL:
  • Last 1 hour: 15 seconds (data is still changing)
  • Last 24 hours: 60 seconds
  • Last 7 days+: 5 minutes (mostly historical, changes slowly)

Cache hit rates:
  • Same dashboard refreshed by 10 engineers → 9 cache hits, 1 miss
  • Auto-refresh every 30s with 15s cache TTL → ~50% hit rate
  • Historical queries (>24h ago) → ~90%+ hit rate

The key insight: time-series data is immutable once written.
Data from yesterday will NEVER change → cache indefinitely.
Only the "now" edge of the time range needs fresh data.
```

### Query Splitting for Parallel Execution

```
Long time range query optimization:

Original query: rate(http_requests_total[5m]) for the last 30 days

Split into 30 sub-queries (one per day):
  Day 1:  rate(...) from Jan 1 to Jan 2    → hits cache (immutable)
  Day 2:  rate(...) from Jan 2 to Jan 3    → hits cache (immutable)
  ...
  Day 29: rate(...) from Jan 29 to Jan 30  → hits cache (immutable)
  Day 30: rate(...) from Jan 30 to now     → cache miss (recent data)

Execute all 30 sub-queries in parallel.
29 hit cache → return instantly.
1 queries TSDB → takes ~100ms.
Merge results → return to dashboard.

Total latency: ~100ms (instead of scanning 30 days from scratch)

This is what the Cortex/Mimir query frontend does automatically.
```

---

## 5. Grafana — The Standard Dashboard Platform

### Grafana Architecture [VERIFIED — Grafana open-source project]

```
┌──────────────────────────────────────────────────────────────┐
│  Grafana                                                     │
│                                                              │
│  ┌────────────────────────────────────────────────────────┐  │
│  │  Frontend (React SPA)                                 │  │
│  │  • Panel renderers (line chart, heatmap, gauge, etc.) │  │
│  │  • Dashboard layout engine                            │  │
│  │  • Variable resolution                                │  │
│  │  • Time range picker                                  │  │
│  └──────────────┬─────────────────────────────────────────┘  │
│                 │ HTTP API calls                              │
│                 ▼                                             │
│  ┌────────────────────────────────────────────────────────┐  │
│  │  Grafana Backend (Go)                                 │  │
│  │  • Dashboard CRUD (stored in SQLite/Postgres/MySQL)   │  │
│  │  • Data source proxy (routes queries to backends)     │  │
│  │  • User authentication (LDAP, OAuth, SAML)            │  │
│  │  • Alerting engine (Grafana 8+ unified alerting)      │  │
│  │  • Plugin system (data sources, panels, apps)         │  │
│  └──────────────┬─────────────────────────────────────────┘  │
│                 │                                            │
│        ┌────────┼────────┬────────────┐                      │
│        ▼        ▼        ▼            ▼                      │
│  ┌──────┐ ┌──────┐ ┌──────────┐ ┌──────────┐               │
│  │Prom  │ │Influx│ │CloudWatch│ │Elastic   │  100+ data    │
│  │TSDB  │ │DB    │ │          │ │search    │  source       │
│  └──────┘ └──────┘ └──────────┘ └──────────┘  plugins      │
└──────────────────────────────────────────────────────────────┘
```

### Key Grafana Features

**Data source plugins**: Connect to any backend — Prometheus, InfluxDB, CloudWatch, Elasticsearch, MySQL, PostgreSQL, Datadog, and 100+ others. Each plugin translates Grafana's query model into the backend's native query language.

**Unified alerting (Grafana 8+)**: Grafana includes its own alerting engine that can evaluate alert rules against any data source. Previously, alerting was delegated to the data source (e.g., Prometheus Alertmanager). Unified alerting centralizes alert management in Grafana.

**Annotations**: Mark events on time-series charts (deployments, config changes, incidents). When investigating a metric change, annotations show "what happened at that time." Can be manual or automatic (e.g., annotate every deployment from CI/CD webhooks).

**Dashboard provisioning**: Dashboards can be defined in JSON and provisioned via YAML configuration files or Terraform. This enables "dashboard-as-code" — version-controlled, reviewable, reproducible.

```yaml
# Grafana provisioning (YAML)
apiVersion: 1
providers:
  - name: 'default'
    folder: 'Platform'
    type: file
    options:
      path: /etc/grafana/dashboards
      # Grafana watches this directory and auto-imports JSON dashboards
```

### Grafana Cloud Stack

Grafana Cloud [VERIFIED — Grafana Labs product documentation] provides a fully managed observability stack:

| Component | Purpose | Open-Source Base |
|---|---|---|
| **Grafana** | Dashboarding & visualization | Grafana OSS |
| **Mimir** | Long-term metrics storage | Cortex/Mimir |
| **Loki** | Log aggregation | Loki |
| **Tempo** | Distributed tracing | Tempo |
| **OnCall** | On-call management | Grafana OnCall |
| **k6** | Load testing | k6 |

This is Grafana Labs' answer to Datadog — a fully integrated observability platform, but built on open-source foundations.

---

## 6. Dashboard Design Best Practices

### The RED Method (for Request-Driven Services)

```
For each service, create a dashboard with three rows:

R — Rate:      Request throughput (requests/second)
E — Errors:    Error rate (% of requests that fail)
D — Duration:  Latency (P50, P95, P99)

This gives a complete view of service health at a glance.
If all three are green → service is healthy.
If any one is degraded → investigate further.
```

### The USE Method (for Infrastructure Resources)

```
For each resource (CPU, memory, disk, network):

U — Utilization: How busy is the resource? (%)
S — Saturation:  How overloaded is it? (queue depth, wait time)
E — Errors:      How many errors? (disk errors, network drops)

Example CPU panel set:
  Utilization: avg(rate(node_cpu_seconds_total{mode!="idle"}[5m])) * 100
  Saturation:  node_load15 / count(node_cpu_seconds_total{mode="idle"})
  Errors:      rate(node_cpu_core_throttles_total[5m])
```

### Dashboard Hierarchy

```
Level 1: OVERVIEW DASHBOARD (executive view)
  • Overall system health (green/yellow/red per service)
  • Key business metrics (transactions/second, active users)
  • Active incidents
  → Used by: on-call, engineering leadership, war rooms

Level 2: SERVICE DASHBOARD (per service)
  • RED metrics for this service
  • Dependency health (is the database healthy? is the cache healthy?)
  • Recent deployments (annotations)
  → Used by: service team, on-call investigating alerts

Level 3: INFRASTRUCTURE DASHBOARD (per component)
  • USE metrics for databases, caches, message queues
  • Node-level metrics (CPU, memory, disk, network)
  • Container-level metrics (per-pod resource usage)
  → Used by: platform/infra team, debugging resource issues

Level 4: DEBUG DASHBOARD (deep dive)
  • Per-endpoint latency breakdown
  • Database query performance
  • Cache hit rates
  • Queue depths and consumer lag
  → Used by: engineers debugging specific issues
```

---

## 7. Comparison: Dashboarding Platforms

| Aspect | Grafana | Datadog | CloudWatch | New Relic |
|---|---|---|---|---|
| **Data sources** | 100+ plugins (any TSDB) | Datadog backend only | AWS services only | New Relic backend only |
| **Customizability** | Highly customizable (open-source, extensible) | Opinionated but polished | Limited | Moderate |
| **Learning curve** | Moderate (many options, flexible) | Low (guided setup, curated) | Low (AWS-native) | Low |
| **Cost** | Free (self-hosted) or Grafana Cloud | Per-host + per-metric pricing | Per-dashboard + per-metric | Per-GB ingested |
| **Dashboard-as-code** | JSON + YAML provisioning + Terraform | Terraform provider | CloudFormation | Terraform provider |
| **Collaboration** | Annotations, shared dashboards, playlists | Notebooks, shared dashboards | — | Workloads |
| **Alerting** | Unified alerting (built-in) | Monitors (built-in) | CloudWatch Alarms | NRQL alerts |
| **Mobile** | Grafana app (view-only) | Full mobile app | AWS Console app | Full mobile app |

### When to Choose Each

- **Grafana**: You have a heterogeneous stack (multiple data sources), want flexibility, and have engineers to operate it. Best for: platform teams, multi-cloud, open-source-first organizations.
- **Datadog**: You want a fully integrated SaaS platform with minimal operational overhead. Best for: startups, mid-size companies, teams without dedicated platform engineering.
- **CloudWatch**: You're all-in on AWS and want zero setup for AWS service monitoring. Best for: AWS-native shops, simple monitoring needs.
- **New Relic**: You want full-stack observability with strong APM and distributed tracing. Best for: application-centric teams, development-focused monitoring.

---

## 8. Performance Considerations

### Dashboard Load Time Budget

```
Target: Dashboard fully rendered in < 2 seconds

Budget breakdown:
  Dashboard definition load:     50ms (from Postgres/MySQL)
  Template variable resolution:  100ms (label value queries)
  Panel query execution:         500-1000ms (parallel, depends on query complexity)
  Data transfer (JSON):          100-200ms (depends on data volume)
  Client-side rendering:         200-300ms (chart rendering, layout)
  ──────────────────────────────────────
  Total:                         ~1-2 seconds

Biggest bottleneck: panel query execution.
Optimization levers:
  1. Query result caching (eliminate repeated queries)
  2. Auto-resolution (fetch fewer data points for long time ranges)
  3. Recording rules (pre-compute expensive aggregations)
  4. Parallel execution (all panels query simultaneously)
```

### Preventing Dashboard-Induced TSDB Overload

A popular dashboard viewed by 50 engineers, each with 30-second auto-refresh, generates:

```
50 users × 20 panels/dashboard × 2 refreshes/minute = 2,000 queries/minute

Mitigation:
  1. Query caching: 50 users hitting same dashboard → 1 cache miss + 49 cache hits
  2. Rate limiting: max queries per dashboard per minute
  3. Shared cursors: if multiple users view the same dashboard with the same
     variables and time range, serve the same cached result
  4. "No auto-refresh by default" for expensive dashboards
```

---

## Summary

| Component | Purpose | Key Insight |
|---|---|---|
| Panel types | Different visualizations for different data | Line chart for trends, heatmap for distributions, gauge for current state |
| Template variables | One dashboard serves all environments/services | Reduces dashboard sprawl from 180 to 1 |
| Auto-resolution | Match data granularity to display resolution | 1-year chart doesn't need 10-second data |
| Query caching | Avoid re-computing immutable historical data | Cache historical results indefinitely, short TTL for recent |
| Grafana | De facto standard for open-source dashboarding | 100+ data source plugins, dashboard-as-code |
| Dashboard hierarchy | Overview → Service → Infrastructure → Debug | Different audiences need different levels of detail |
| RED/USE methods | Structured approach to dashboard design | RED for services, USE for infrastructure |
