# Alerting Pipeline, Anomaly Detection & On-Call

## The Most Operationally Critical Component

The alerting pipeline is where a monitoring system proves its value. Collection, storage, and dashboards are all important — but if an alert doesn't fire when a production system is failing, everything else is academic. A missed alert = an undetected outage = customer impact.

---

## 1. Alert Rule Evaluation Loop

### How an Alert Rule Works

```yaml
# Example alert rule
name: "High Error Rate on API"
query: |
  sum(rate(http_requests_total{status=~"5..", service="api"}[5m]))
  / sum(rate(http_requests_total{service="api"}[5m])) * 100
threshold: "> 5"          # Fire if error rate exceeds 5%
for: 3 minutes            # Must exceed for 3 continuous minutes
severity: critical
labels:
  team: platform
  service: api
annotations:
  summary: "API error rate is {{ $value }}%"
  runbook: "https://wiki.internal/runbooks/api-high-error-rate"
notify:
  - pagerduty: platform-oncall
  - slack: "#platform-alerts"
```

### The Evaluation Loop

```
┌──────────────────────────────────────────────────────────────┐
│  Alert Evaluator (runs continuously)                         │
│                                                              │
│  FOR each alert rule:                                        │
│    Every evaluation_interval (default: 15-60 seconds):       │
│                                                              │
│    1. EXECUTE the metric query against the TSDB              │
│       → Returns current value (e.g., error rate = 7.3%)      │
│                                                              │
│    2. COMPARE against threshold                              │
│       → 7.3% > 5%? YES → threshold exceeded                 │
│                                                              │
│    3. CHECK "for" duration                                   │
│       → Has it been > 5% for 3 continuous minutes?           │
│       → Track state transitions:                             │
│                                                              │
│    ┌─────────────────────────────────────────────────────┐   │
│    │  Alert State Machine                                │   │
│    │                                                     │   │
│    │   OK ──── threshold exceeded ────> PENDING          │   │
│    │   ▲                                    │            │   │
│    │   │                                    │            │   │
│    │   │  threshold no longer              "for" duration │   │
│    │   │  exceeded (before "for")           elapsed      │   │
│    │   │                                    │            │   │
│    │   └──────────────────┐                 ▼            │   │
│    │                      │             FIRING ──────────│──>│ SEND NOTIFICATION
│    │                      │                │             │   │
│    │                      │                │ threshold   │   │
│    │                      │                │ no longer   │   │
│    │                      │                │ exceeded    │   │
│    │                      │                ▼             │   │
│    │                      └──────── RESOLVED ────────────│──>│ SEND RESOLUTION
│    └─────────────────────────────────────────────────────┘   │
└──────────────────────────────────────────────────────────────┘
```

### Why the "for" Duration Matters

Without a "for" duration, transient spikes trigger alerts:

```
Without "for" duration:
  t=0  error_rate=1%   OK
  t=15 error_rate=6%   → FIRES! (on-call paged)
  t=30 error_rate=2%   → RESOLVES
  t=45 error_rate=7%   → FIRES! (on-call paged again)
  t=60 error_rate=1%   → RESOLVES
  → 2 false-positive pages in 1 minute

With "for: 3 minutes":
  t=0  error_rate=1%   OK
  t=15 error_rate=6%   → PENDING (start timer)
  t=30 error_rate=2%   → Back to OK (timer reset — spike was transient)
  t=45 error_rate=7%   → PENDING (start timer again)
  t=60 error_rate=8%   → Still PENDING (1 minute of 3 elapsed)
  ...
  t=225 error_rate=9%  → 3 minutes continuous → FIRING (real problem)
  → 0 false positives for transient spikes
```

The "for" duration is the primary knob for balancing **sensitivity** (catch real problems quickly) vs **specificity** (don't page for transient blips).

---

## 2. Alert Routing, Grouping & Deduplication

### Prometheus Alertmanager Architecture [VERIFIED — Prometheus Alertmanager documentation]

```
┌──────────────────────────────────────────────────────────────┐
│  Prometheus Alertmanager                                     │
│                                                              │
│  RECEIVE alerts from Prometheus                              │
│  (multiple Prometheus instances may send the same alert)     │
│                                                              │
│  ┌────────────────────────────────────────────────────────┐  │
│  │  1. DEDUPLICATION                                     │  │
│  │     Alerts with same labels = same alert               │  │
│  │     Don't notify twice for the same problem            │  │
│  └────────────────────────────────────────────────────────┘  │
│                     │                                        │
│                     ▼                                        │
│  ┌────────────────────────────────────────────────────────┐  │
│  │  2. GROUPING                                          │  │
│  │     Group related alerts into ONE notification         │  │
│  │     e.g., "50 hosts have high CPU" → 1 notification   │  │
│  │     not 50 separate pages                              │  │
│  │                                                       │  │
│  │     group_by: [service, alertname]                    │  │
│  │     group_wait: 30s     (wait for more alerts to join)│  │
│  │     group_interval: 5m  (re-send group every 5m)      │  │
│  └────────────────────────────────────────────────────────┘  │
│                     │                                        │
│                     ▼                                        │
│  ┌────────────────────────────────────────────────────────┐  │
│  │  3. INHIBITION                                        │  │
│  │     Suppress alerts when a higher-priority alert       │  │
│  │     is already firing                                  │  │
│  │                                                       │  │
│  │     Example: if "cluster_down" is firing,             │  │
│  │     suppress all "pod_unhealthy" alerts               │  │
│  │     for that cluster (they're symptoms, not causes)   │  │
│  └────────────────────────────────────────────────────────┘  │
│                     │                                        │
│                     ▼                                        │
│  ┌────────────────────────────────────────────────────────┐  │
│  │  4. SILENCING                                         │  │
│  │     Mute specific alerts during maintenance windows   │  │
│  │                                                       │  │
│  │     Example: "silence all alerts for host=web-03      │  │
│  │     from 2am-4am Saturday" (scheduled maintenance)    │  │
│  └────────────────────────────────────────────────────────┘  │
│                     │                                        │
│                     ▼                                        │
│  ┌────────────────────────────────────────────────────────┐  │
│  │  5. ROUTING                                           │  │
│  │     Route alerts to the right team/channel             │  │
│  │     based on labels                                    │  │
│  │                                                       │  │
│  │     route:                                            │  │
│  │       receiver: 'default-slack'                       │  │
│  │       routes:                                         │  │
│  │         - match: {severity: critical}                 │  │
│  │           receiver: 'pagerduty-oncall'                │  │
│  │         - match: {team: database}                     │  │
│  │           receiver: 'database-team-slack'             │  │
│  └────────────────────────────────────────────────────────┘  │
│                     │                                        │
│                     ▼                                        │
│  ┌────────────────────────────────────────────────────────┐  │
│  │  6. NOTIFICATION                                      │  │
│  │     Send to receivers: Slack, PagerDuty, Email,       │  │
│  │     OpsGenie, Webhook, etc.                           │  │
│  │                                                       │  │
│  │     Retry on failure with exponential backoff          │  │
│  │     (if PagerDuty is down, keep trying)               │  │
│  └────────────────────────────────────────────────────────┘  │
└──────────────────────────────────────────────────────────────┘
```

### Alertmanager Clustering (HA)

Alertmanager runs as a cluster of 3+ instances using a gossip protocol (Memberlist/HashiCorp Serf). All instances receive the same alerts from Prometheus. The gossip protocol ensures:

- **Deduplication across instances**: If Prometheus sends the same alert to all 3 Alertmanager instances, only ONE notification is sent (not 3)
- **Failover**: If one Alertmanager instance dies, the others continue sending notifications
- **Consistency**: All instances agree on alert state (which alerts are silenced, which are inhibited)

---

## 3. Alert Fatigue — The #1 Operational Problem

### What Is Alert Fatigue

Alert fatigue occurs when on-call engineers receive so many alerts that they start ignoring them — including real, critical alerts. This is the single biggest operational risk in monitoring.

```
Healthy alert volume:
  Critical pages per week: 2-5 (each page is actionable and requires human intervention)
  Warning notifications: 10-20 (informational, reviewed during business hours)

Unhealthy alert volume (alert fatigue):
  Critical pages per week: 50-100+
  → On-call engineer mutes phone, stops responding
  → Real critical alert fires at 3 AM → nobody responds
  → Customer-facing outage goes undetected for hours
```

### Root Causes of Alert Fatigue

| Cause | Example | Fix |
|---|---|---|
| **Too many non-actionable alerts** | "Disk at 70%" — so what? | Only alert if action is needed. Threshold at 90% |
| **Redundant alerts** | CPU high + memory high + disk I/O high — all same root cause | Alert on the cause, not symptoms. Use inhibition |
| **Missing runbooks** | Alert fires, on-call has no idea what to do | Every alert must link to a runbook |
| **Flapping** | Alert fires/resolves/fires/resolves rapidly | Increase "for" duration. Hysteresis thresholds |
| **Stale alerts** | Alert for a service that was decommissioned 6 months ago | Regular alert review cycles (quarterly) |
| **Wrong severity** | Everything is "critical" | Strict severity definitions (see below) |

### Severity Level Definitions

```
CRITICAL (page immediately, wake up on-call):
  → Customer-facing impact RIGHT NOW
  → Data loss in progress
  → Security breach detected
  → Requires human action within minutes
  → Examples: API error rate > 5%, database unreachable, payment failures

WARNING (Slack notification, review next business day):
  → Approaching a dangerous threshold but not there yet
  → Performance degraded but functional
  → No immediate customer impact
  → Examples: disk at 80%, memory trending upward, elevated latency

INFO (logged, visible on dashboards):
  → Informational, no action needed
  → Examples: deployment completed, scaling event, config change
```

**The golden rule**: If an on-call engineer is paged and doesn't need to take immediate action, the alert is misconfigured. Every critical page should result in human intervention.

### Hysteresis — Preventing Flapping

```
Simple threshold (flapping):
  Fire  when value > 90
  Resolve when value ≤ 90

  If value oscillates between 89 and 91:
    Fire → Resolve → Fire → Resolve → Fire → Resolve  (flapping)

Hysteresis threshold (stable):
  Fire  when value > 90
  Resolve when value < 80   ← different resolve threshold!

  If value oscillates between 89 and 91:
    Fire at 91 → stays fired (89 > 80) → stays fired → stable
  Only resolves when value drops significantly (below 80)
```

---

## 4. Anomaly Detection — ML-Based Alerting

### Why Static Thresholds Are Insufficient

```
Static threshold: "Alert if CPU > 90%"

Problem with seasonality:
  ┌─────────────────────────────────────────────────┐
  │  Monday-Friday, 9am-6pm:                        │
  │    CPU hovers around 80-85% (normal business    │
  │    hours traffic). A threshold of 90% is tight  │
  │    — frequent false positives.                   │
  │                                                  │
  │  Saturday, 3am:                                  │
  │    CPU is usually 15-20%. If it hits 50%,        │
  │    that's ANOMALOUS — something is wrong.        │
  │    But the 90% threshold doesn't fire.           │
  └─────────────────────────────────────────────────┘

A static threshold can't distinguish:
  85% CPU on Monday at 2pm = NORMAL
  50% CPU on Saturday at 3am = ANOMALOUS
```

### How Anomaly Detection Works

```
1. BASELINE LEARNING
   For each metric, the ML model learns the "normal" pattern:
   • Daily seasonality (peak at 2pm, low at 3am)
   • Weekly seasonality (weekdays higher than weekends)
   • Trend (gradual increase over months due to growth)
   • Noise level (how much variance is "normal")

   Algorithm options:
   • STL decomposition (Seasonal and Trend decomposition using Loess)
   • Holt-Winters exponential smoothing
   • Prophet (Meta's forecasting model)
   • Simple statistical: z-score on residuals after detrending

2. ANOMALY SCORING
   At each evaluation:
   • Predicted value = seasonal_component + trend_component
   • Residual = actual_value - predicted_value
   • If |residual| > k × standard_deviation → ANOMALOUS

   k = sensitivity parameter:
     k = 2: detects 5% of normal data as anomalous (noisy)
     k = 3: detects 0.3% of normal data as anomalous (balanced)
     k = 4: detects 0.006% (very conservative, may miss real anomalies)

3. ALERT
   If anomaly detected → PENDING → FIRING (same state machine)
   Anomaly alert message includes:
     "CPU on host web-03 is 52% at Saturday 3:15am.
      Expected value: 18% ± 5%. Deviation: +34% (6.8 sigma)."
```

### Datadog Watchdog [VERIFIED — Datadog product documentation]

Datadog's Watchdog is an automated anomaly detection system:
- Runs continuously across ALL metrics (no user configuration needed)
- Learns baselines per metric, per tag combination
- Flags anomalies in the Watchdog feed (a dedicated UI view)
- Groups related anomalies into "stories" (e.g., "latency anomaly + error rate anomaly on the same service → likely related")

**Trade-off**:
- Anomaly detection reduces false positives for seasonal patterns
- But can miss novel failure modes that don't look "anomalous" to the model
- Static thresholds are dumb but predictable; ML alerting is smart but can be surprising
- Best practice: use both — static thresholds for absolute limits ("CPU > 95% is NEVER OK"), anomaly detection for relative deviations ("this is unusual for this time of day")

---

## 5. On-Call Integration & Incident Lifecycle

### Alert → Incident → Resolution Flow

```
┌──────────────────────────────────────────────────────────────────────┐
│                                                                      │
│  1. ALERT FIRES                                                      │
│     Monitoring system evaluates rule → threshold exceeded → FIRING   │
│     → Sends to Alertmanager / Datadog Monitors                       │
│                                                                      │
│  2. NOTIFICATION SENT                                                │
│     Alertmanager → PagerDuty / OpsGenie                              │
│     PagerDuty: phone call + SMS + push notification to on-call       │
│                                                                      │
│  3. ACKNOWLEDGE (on-call responds)                                   │
│     On-call engineer acknowledges the page within 5 minutes          │
│     → Stops escalation timer                                         │
│     "I see the problem, I'm investigating"                           │
│                                                                      │
│  4. ESCALATION (if not acknowledged)                                 │
│     5 min → page backup on-call                                      │
│     10 min → page team lead                                          │
│     15 min → page engineering manager                                │
│     30 min → page VP of Engineering                                  │
│                                                                      │
│  5. INVESTIGATE                                                      │
│     On-call opens dashboards, checks logs, reviews recent deploys    │
│     Uses runbook linked in the alert                                 │
│                                                                      │
│  6. MITIGATE                                                         │
│     Take immediate action to stop customer impact:                   │
│     • Rollback deployment                                            │
│     • Scale up infrastructure                                        │
│     • Toggle feature flag off                                        │
│     • Redirect traffic away from unhealthy region                    │
│                                                                      │
│  7. RESOLVE                                                          │
│     Root cause fixed → metric returns to normal → alert auto-resolves│
│     Or: on-call manually resolves after confirming fix               │
│                                                                      │
│  8. POST-MORTEM                                                      │
│     After the incident: write a blameless post-mortem                │
│     • Timeline of events                                             │
│     • Root cause analysis                                            │
│     • Action items to prevent recurrence                             │
│     • Was the alert effective? Should it be tuned?                   │
│                                                                      │
└──────────────────────────────────────────────────────────────────────┘
```

### On-Call Platforms

| Platform | Key Feature | Integration |
|---|---|---|
| **PagerDuty** | Sophisticated escalation policies, incident response orchestration | Webhook from Alertmanager/Datadog |
| **OpsGenie** (Atlassian) | Team-based routing, Jira integration | Webhook, native Prometheus integration |
| **VictorOps** (Splunk) | Timeline-based incident management | Webhook |
| **Grafana OnCall** (open-source) | Native Grafana integration, IaC support | Direct Grafana/Alertmanager integration |

### Notification Channel Reliability

The notification channel itself must be reliable. If PagerDuty is down when a critical alert fires:

```
Mitigation: Multi-channel notification
  Primary:  PagerDuty (phone + SMS + push)
  Fallback: Slack (if PagerDuty unreachable)
  Last resort: Direct SMS via Twilio (no dependency on PagerDuty)

The monitoring system should:
  1. Try primary channel
  2. If delivery fails after 60 seconds → try fallback
  3. If fallback fails → try last resort
  4. Log notification delivery status (for auditing)
```

---

## 6. Alert Rule Best Practices

### The Four Golden Signals (Google SRE) [VERIFIED — Google SRE Book]

```
1. LATENCY: How long requests take
   Alert: "P99 latency > 500ms for 5 minutes"
   Query: histogram_quantile(0.99, rate(http_request_duration_seconds_bucket[5m]))

2. TRAFFIC: How much demand the system handles
   Alert: "Request rate dropped > 50% compared to last week same time"
   (Anomaly detection — not a static threshold)

3. ERRORS: Rate of failed requests
   Alert: "Error rate > 1% for 3 minutes"
   Query: sum(rate(http_requests_total{status=~"5.."}[5m]))
          / sum(rate(http_requests_total[5m]))

4. SATURATION: How "full" the system is
   Alert: "Memory usage > 90% of limit for 10 minutes"
   Query: container_memory_usage_bytes / container_spec_memory_limit_bytes
```

These four signals cover the vast majority of service health monitoring. Start with these before adding more specific alerts.

### Symptom-Based vs Cause-Based Alerting

```
CAUSE-BASED (fragile, noisy):
  Alert: "CPU > 90%"
  Alert: "Memory > 85%"
  Alert: "Disk I/O > 80%"
  Alert: "Network packet drops > 0"
  → 4 alerts fire simultaneously for the same root cause
  → On-call receives 4 pages, wastes time correlating

SYMPTOM-BASED (robust, actionable):
  Alert: "Error rate > 1%"
  Alert: "P99 latency > 500ms"
  → 1-2 alerts fire for the customer-visible symptom
  → On-call immediately knows: "users are affected"
  → Investigation reveals CPU/memory/disk as root cause

Best practice: ALERT ON SYMPTOMS, investigate with DASHBOARDS showing causes.
Page the on-call for "error rate is high" — they'll drill into CPU, memory,
disk, recent deploys on the investigation dashboard.
```

### Alert as Code

```yaml
# Alert rules version-controlled alongside application code
# Terraform provider for Datadog:
resource "datadog_monitor" "api_error_rate" {
  name    = "API Error Rate High"
  type    = "metric alert"
  query   = "sum(last_5m):sum:http.requests{status:5xx,service:api}.as_rate() / sum:http.requests{service:api}.as_rate() * 100 > 5"
  message = <<-EOT
    API error rate is {{value}}%, threshold is 5%.
    Runbook: https://wiki.internal/runbooks/api-errors
    @pagerduty-platform-oncall
  EOT

  monitor_thresholds {
    critical = 5
    warning  = 2
  }

  tags = ["service:api", "team:platform"]
}
```

Storing alert definitions as code ensures:
- Version history (who changed what, when)
- Code review for alert changes (peer review prevents bad thresholds)
- Consistency across environments (same alerts in staging and production, different thresholds)
- Disaster recovery (if the monitoring system is rebuilt, all alerts are restored from code)

---

## 7. Alerting at Scale

### Evaluation Performance

With thousands of alert rules:

```
1,000 alert rules × evaluation every 30 seconds = 33 evaluations/second

Each evaluation:
  1. Execute a metric query (P50 = 50ms, P99 = 500ms)
  2. Compare against threshold (microseconds)
  3. Update state machine (microseconds)

Total query load from alerting alone: 33 queries/second
This is significant — alerting is a constant query load on the TSDB.

Scaling strategies:
  • Shard alert rules across multiple evaluator instances
  • Use recording rules to pre-compute expensive alert queries
  • Separate alert evaluation from ad-hoc dashboard queries
    (alert queries must not be starved by a slow dashboard query)
```

### Alert Evaluation Must Be Prioritized

```
Priority order for query execution:
  1. Alert evaluation queries (HIGHEST — affects incident detection)
  2. Recording rule queries (pre-computation for dashboards and alerts)
  3. Dashboard queries (user-facing, latency matters)
  4. Ad-hoc/exploration queries (LOWEST — can tolerate delays)

Implementation:
  Separate query queues per priority level.
  Alert queries always execute first.
  If the system is overloaded, ad-hoc queries queue up,
  but alert queries never wait.
```

---

## 8. Comparison: Alerting Across Systems

| Aspect | Prometheus Alertmanager | Datadog Monitors | CloudWatch Alarms |
|---|---|---|---|
| **Alert evaluation** | Prometheus evaluates rules → sends to Alertmanager | Datadog backend evaluates | CloudWatch evaluates |
| **Grouping** | Sophisticated (group_by labels, group_wait, group_interval) | Basic (multi-alert grouping) | None (each alarm independent) |
| **Routing** | Label-based tree routing to receivers | Tag-based routing | SNS topics |
| **Silencing** | Time-based + matcher-based silences | Downtime scheduling | — |
| **Inhibition** | Built-in (suppress symptoms when cause is alerting) | Limited | — |
| **Deduplication** | Built-in (fingerprint-based, cluster gossip) | Built-in | — |
| **Anomaly detection** | Not built-in (needs external tools) | Watchdog (ML-based, automatic) | Anomaly detection alarms |
| **Composite alerts** | Recording rules + alert on composite metric | Composite monitors (AND/OR of multiple conditions) | Composite alarms |
| **Self-hosted** | Yes (open-source) | No (SaaS only) | No (AWS managed) |
| **Alert-as-code** | YAML rules (version-controlled) | Terraform provider | CloudFormation |

---

## Summary

| Component | Purpose | Key Insight |
|---|---|---|
| Alert state machine | OK → PENDING → FIRING → RESOLVED | "for" duration prevents transient false positives |
| Grouping | Combine related alerts into one notification | 500 hosts with same problem → 1 page, not 500 |
| Inhibition | Suppress symptom alerts when cause is firing | cluster_down inhibits all pod_unhealthy alerts |
| Anomaly detection | Learn baselines, alert on deviations | Handles seasonality that static thresholds can't |
| Alert fatigue | The #1 operational risk | Every page must be actionable — if not, fix or delete the alert |
| On-call escalation | Ensure someone always responds | Auto-escalate if no acknowledgment within 5 minutes |
| Symptom-based | Alert on user impact, not infrastructure metrics | "Error rate high" beats "CPU high" |
| Alert-as-code | Version-controlled alert definitions | Code review for alert changes prevents bad thresholds |
