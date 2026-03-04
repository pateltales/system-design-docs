# Monitoring the Monitoring System — The Meta-Problem

## "Who Watches the Watchmen?"

This is the most ironic and most important problem in monitoring system design. If the monitoring system fails and nobody notices, every other system in your infrastructure loses its safety net. A silent monitoring failure is worse than any application outage — because you don't know that anything is broken.

---

## 1. Why This Problem Is Hard

### The Circular Dependency

```
Normal monitoring:
  Application → emits metrics → Monitoring System → evaluates alerts → Pages on-call
  (If application breaks, monitoring detects it ✓)

Self-monitoring:
  Monitoring System → emits metrics → Monitoring System → evaluates alerts → ???
  (If monitoring system breaks, who detects it? It can't alert about itself ✗)

The monitoring system cannot reliably alert about its own failure
using its own alerting pipeline. This is a fundamental limitation.

Example scenario:
  1. Kafka cluster (ingestion buffer) goes down
  2. No new metrics are ingested
  3. TSDB has stale data
  4. Alert evaluator queries TSDB → gets old data → no threshold breach
  5. No alert fires
  6. Meanwhile, your production database is also down, but the monitoring
     system doesn't see the new error rate because ingestion is broken
  7. You find out from a customer tweet
```

### The Solution: Independent Health Monitoring

The monitoring system's health must be checked by something OUTSIDE the monitoring system — an independent, simpler system with a separate failure domain.

---

## 2. Self-Monitoring: Internal Health Metrics

The monitoring system should instrument itself like any other service. These metrics are the first line of defense.

### Ingestion Health

| Metric | What It Measures | Alert Threshold |
|---|---|---|
| `monitoring.ingestion.rate` | Data points ingested per second | Drop >50% vs expected → investigate |
| `monitoring.ingestion.lag_seconds` | Time between sample creation and storage | >60 seconds → ingestion is falling behind |
| `monitoring.ingestion.errors_total` | Failed ingestion attempts | >1% error rate → investigate |
| `monitoring.kafka.consumer_lag` | Messages pending in Kafka (not yet consumed) | >100K → consumers are falling behind |
| `monitoring.series.active_count` | Number of active time series | Sudden jump >20% → cardinality bomb |
| `monitoring.series.churn_rate` | New series created per minute | >10K/min → container restarts or label issues |

### Query Health

| Metric | What It Measures | Alert Threshold |
|---|---|---|
| `monitoring.query.latency_p99` | P99 query latency | >5 seconds → queries are too slow |
| `monitoring.query.error_rate` | Percentage of queries that fail | >1% → investigate |
| `monitoring.query.concurrent` | Number of concurrent queries | >80% of capacity → scale queriers |
| `monitoring.query.rejected_count` | Queries rejected (cost too high, rate limited) | >10/min → users hitting limits |

### Alerting Health

| Metric | What It Measures | Alert Threshold |
|---|---|---|
| `monitoring.alert.evaluation_latency` | Time to evaluate all alert rules | >30 seconds → alert rules too expensive |
| `monitoring.alert.evaluation_failures` | Alert rule evaluations that errored | >0 → fix the broken rule |
| `monitoring.alert.notification_latency` | Time from FIRING to notification sent | >30 seconds → notification pipeline slow |
| `monitoring.alert.notification_failures` | Failed notification deliveries | >0 → PagerDuty/Slack integration broken |
| `monitoring.alert.active_firing` | Number of currently firing alerts | Sudden spike → possible alert storm |

### Storage Health

| Metric | What It Measures | Alert Threshold |
|---|---|---|
| `monitoring.storage.disk_usage_percent` | Disk usage on TSDB nodes | >80% → scale or adjust retention |
| `monitoring.storage.compaction_duration` | Time for block compaction | >30 minutes → compaction falling behind |
| `monitoring.storage.wal_size_bytes` | WAL size on ingesters | >1 GB → flush is delayed |
| `monitoring.storage.s3_upload_failures` | Failed uploads to object storage | >0 → durability at risk |

---

## 3. Independent Health Checks — The Safety Net

### Architecture

```
┌──────────────────────────────────────────────────────────────┐
│  INDEPENDENT HEALTH MONITOR                                  │
│  (runs OUTSIDE the monitoring system's infrastructure)       │
│                                                              │
│  ┌────────────────────────────────────────────────────────┐  │
│  │  Synthetic Check (runs every 60 seconds)               │  │
│  │                                                       │  │
│  │  1. WRITE TEST: Push a synthetic metric                │  │
│  │     POST /v1/metrics                                   │  │
│  │     {name: "health_check.canary",                      │  │
│  │      value: current_timestamp,                         │  │
│  │      tags: {source: "health_checker"}}                 │  │
│  │                                                       │  │
│  │  2. READ TEST: Query the synthetic metric              │  │
│  │     GET /v1/query?metric=health_check.canary           │  │
│  │     Verify: returned value matches what was written    │  │
│  │     Verify: latency < 5 seconds                        │  │
│  │                                                       │  │
│  │  3. ALERT TEST: Verify alert evaluation is working     │  │
│  │     Check that a known "always-firing" test alert      │  │
│  │     is still in FIRING state                           │  │
│  │                                                       │  │
│  │  4. If ANY check fails:                                │  │
│  │     Send alert via INDEPENDENT channel:                │  │
│  │     • Direct SMS via Twilio API                        │  │
│  │     • Direct PagerDuty API call (not through            │  │
│  │       monitoring system's Alertmanager)                 │  │
│  │     • Email via SES/SendGrid                           │  │
│  │     • Slack webhook (direct, not through monitoring)   │  │
│  └────────────────────────────────────────────────────────┘  │
│                                                              │
│  Implementation options:                                     │
│  • Cloud Function (AWS Lambda, GCP Cloud Function)          │
│  • External monitoring service (Pingdom, UptimeRobot)       │
│  • Simple cron job on a separate server                     │
│  • Separate, minimal monitoring instance (a tiny Prometheus) │
│                                                              │
│  KEY REQUIREMENT: Must have NO shared infrastructure with    │
│  the primary monitoring system. Different servers, different │
│  cloud accounts if possible, different notification channels.│
└──────────────────────────────────────────────────────────────┘
```

### The Canary Metric Pattern

```
The "canary metric" is the simplest and most effective health check:

Every 60 seconds:
  1. Generate a known value: value = current_timestamp
  2. Write it to the monitoring system:
     POST /v1/metrics {name: "canary", value: <timestamp>, tags: {checker: "external"}}
  3. Wait 30 seconds (allow ingestion pipeline to process)
  4. Query it back:
     GET /v1/query?metric=canary&tags=checker:external&last=1m
  5. Verify:
     - Query returned a result (not empty)
     - Returned value matches what was written (end-to-end integrity)
     - Round-trip time < 60 seconds (end-to-end latency)

If the canary check fails:
  - Write path broken → no data is being ingested
  - Read path broken → queries are failing
  - Both → the monitoring system is completely down

This single check covers the entire pipeline:
  Agent → Ingestion Gateway → Kafka → Ingester → TSDB → Query Engine
  A failure at ANY point in this chain will cause the canary check to fail.
```

---

## 4. Multi-Layer Alerting Strategy

### Three Layers of Defense

```
LAYER 1: Self-monitoring (internal)
  The monitoring system monitors itself using its own metrics.
  Catches: individual component degradation, capacity issues, slow queries.
  Limitation: blind to total system failure (can't alert if alerting is down).

LAYER 2: Independent health checks (external)
  An external system (Lambda, cron, Pingdom) checks the monitoring system.
  Catches: total ingestion failure, total query failure, alert pipeline failure.
  Limitation: can only check externally-visible health (API endpoints).

LAYER 3: Absence-of-signal detection (cross-system)
  "If I haven't received any alerts in 24 hours, something might be wrong."
  A daily heartbeat: the monitoring system sends a "I'm alive" message at noon.
  If the message doesn't arrive → independent system alerts.
  Catches: silent failures where the system appears up but isn't functioning.

All three layers should use DIFFERENT notification channels:
  Layer 1: Alertmanager → PagerDuty (normal alert path)
  Layer 2: Lambda → direct Twilio SMS (independent of Alertmanager)
  Layer 3: Cron → direct email (independent of everything)
```

### Dead Man's Switch

```
The "dead man's switch" pattern:

The monitoring system sends a "heartbeat" metric every 60 seconds
to an EXTERNAL service (e.g., Deadman.io, PagerDuty heartbeat, custom Lambda).

If the external service doesn't receive the heartbeat for 5 minutes:
  → The monitoring system is down
  → External service sends alert via independent channel

This inverts the problem:
  Instead of detecting failure (hard — the broken system can't report its own failure),
  detect absence of success (easy — if I don't hear from you, you're dead).

Implementation:
  # In the monitoring system, a simple cron job:
  */1 * * * * curl -s https://heartbeat.external.com/monitoring-alive

  # External service:
  If no heartbeat for 5 minutes → SMS platform-oncall via Twilio
```

---

## 5. Google's Approach: Borgmon and Monarch

Google's internal monitoring architecture [VERIFIED — Google SRE Book, "Monitoring Distributed Systems" chapter] illustrates the meta-problem at extreme scale:

```
Google uses multiple layers of monitoring systems:

Monarch (primary):
  Google's main metrics monitoring system.
  Processes billions of time series.
  Used by all Google services.

Borgmon (predecessor / backstop):
  Prometheus was inspired by Borgmon.
  A simpler, older monitoring system.
  Used to monitor Monarch's health.

The principle:
  A complex system (Monarch) is monitored by a simpler system (Borgmon).
  The simpler system has fewer failure modes.
  If Monarch fails, Borgmon detects it.
  If Borgmon fails... it's simple enough that it rarely does.

You don't need Google's scale to apply this principle:
  Your "Borgmon" can be a Lambda function + Twilio SMS.
  Simple = reliable.
```

---

## 6. Failure Scenarios and Detection

### Scenario Analysis

| Failure Scenario | Self-Monitoring Catches? | Independent Check Catches? | Dead Man's Switch Catches? |
|---|---|---|---|
| **One ingester OOM** | Yes (ingester health metrics) | Maybe (if write test routed to that ingester) | No (system still partially working) |
| **Kafka cluster down** | No (metrics about Kafka can't be ingested) | Yes (write test fails) | Yes (heartbeat not sent) |
| **All ingesters down** | No (no new metrics ingested) | Yes (write test fails) | Yes |
| **TSDB corruption** | Maybe (query errors increase) | Yes (read test returns wrong data) | No |
| **Alert evaluator crash** | No (alert about alert evaluator can't fire) | Yes (test alert stops firing) | No |
| **Alertmanager cluster down** | No (alert fires but notification fails) | Depends (if check verifies notification) | No |
| **Network partition (monitoring isolated)** | No (self-monitoring works internally) | Yes (external check can't reach monitoring) | Yes |
| **Slow degradation (queries 10x slower)** | Yes (query latency metrics) | Yes (read test times out) | No |
| **Complete datacenter failure** | No | Yes (if check runs externally) | Yes |

### Key Insight

No single monitoring layer catches all failure modes. You need all three:
- Self-monitoring for granular visibility into individual components
- Independent health checks for end-to-end pipeline verification
- Dead man's switch for total system failure detection

---

## 7. Operational Playbook

### When the Monitoring System Is Down

```
PRIORITY: Restore monitoring BEFORE investigating root cause.

Step 1: VERIFY (is monitoring actually down, or is it a false alarm?)
  • Check independent health check dashboard (on a separate system)
  • Manually curl the monitoring API: curl https://monitoring.internal/api/v1/status
  • Check if Grafana loads (even with stale data)

Step 2: COMMUNICATE
  • Post in #incident Slack channel: "Monitoring system degraded/down"
  • Engineers should know: "Your dashboards and alerts may be stale"

Step 3: TRIAGE
  • Is it ingestion (write path)? → Check Kafka, ingesters, agents
  • Is it query (read path)? → Check queriers, store gateways
  • Is it alerting? → Check alert evaluator, Alertmanager
  • Is it complete? → Check network, infrastructure, DNS

Step 4: RESTORE
  • Restart failed components
  • If data loss: Kafka replay from last known good offset
  • If corruption: restore TSDB from last good snapshot

Step 5: VERIFY RECOVERY
  • Canary metric write → read → success?
  • Test alert fires and notification delivered?
  • Dashboard queries return current data?

Step 6: POST-MORTEM
  • Why did the monitoring system fail?
  • Why didn't we detect it sooner?
  • How do we prevent this class of failure?
  • Do we need a new independent health check?
```

---

## 8. Monitoring System SLOs

### What SLOs Should the Monitoring System Have?

| SLO | Target | Rationale |
|---|---|---|
| **Ingestion availability** | 99.9% (8.7 hours downtime/year) | Brief gaps are tolerable; alerting has "for" duration |
| **Ingestion latency** (P99) | < 30 seconds | Alert evaluation needs reasonably fresh data |
| **Query availability** | 99.5% (43.8 hours/year) | Dashboards can be briefly unavailable |
| **Query latency** (P99) | < 5 seconds | Dashboard rendering must feel responsive |
| **Alert evaluation** | 99.95% (4.4 hours/year) | Most critical path — missed alerts = undetected outages |
| **Alert notification delivery** | 99.9% (8.7 hours/year) | Notifications must be delivered reliably |
| **End-to-end** (metric → alert → page) | < 2 minutes (P99) | Total detection time |

### Why Alert Evaluation Has the Highest SLO

```
If ingestion is down for 5 minutes:
  → You lose 5 minutes of metric data
  → Alerts can't evaluate (no fresh data)
  → But the "for" duration (3-5 minutes) provides a buffer
  → Likely impact: slight delay in detection, not a miss

If query is down for 5 minutes:
  → Dashboards don't load
  → Engineers can't investigate
  → But alerts still fire (separate evaluation pipeline)
  → Impact: inconvenience, not a safety issue

If alert evaluation is down for 5 minutes:
  → Active outages go undetected
  → Customer impact accumulates
  → MTTR increases dramatically
  → Impact: DIRECT SAFETY RISK

This is why alert evaluation has the highest availability target.
```

---

## Summary

| Component | Purpose | Key Insight |
|---|---|---|
| Self-monitoring | Internal health metrics | First line of defense, but blind to total failure |
| Independent checks | External canary write/read/alert tests | Catches end-to-end failures self-monitoring misses |
| Dead man's switch | Absence-of-signal detection | Inverts the problem: detect silence, not failure |
| Multi-layer alerting | Three independent notification channels | No single point of failure for alerting about monitoring |
| Canary metric | Write a value, read it back, verify | Simplest complete end-to-end health check |
| Simple backstop | A simpler system monitors the complex system | Google's principle: complexity requires simple watchdogs |
| Monitoring SLOs | Quantified reliability targets | Alert evaluation > ingestion > query availability |
