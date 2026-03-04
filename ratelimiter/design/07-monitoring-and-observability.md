# Rate Limiter — Monitoring, Alerting & Observability

> A rate limiter that you can't observe is a liability. If you can't answer "why was this request rejected?" within 30 seconds, your rate limiter is hurting more than helping.

---

## Key Metrics to Track

### 1. Total Requests (Allowed + Rejected)

Total request volume broken down by client, resource, and tier. This is the denominator for rejection rate.

```
rate_limit_requests_total{
  client_id="user_abc",
  resource="/api/v1/orders",
  method="POST",
  tier="pro",
  decision="allow|reject",
  rule_id="rule-456",
  dimension="per_user|per_endpoint|per_ip|global"
}
```

### 2. Rejection Rate

Percentage of requests returning 429, broken down by client, resource, and rule.

```
rejection_rate = rejected / (allowed + rejected) × 100%
```

| Rejection Rate | Likely Cause | Action |
|---|---|---|
| 0% | Limits may be too loose | Review if limits are effective |
| 0.1-1% | Normal — catching occasional bursts | Monitor |
| 1-5% | Client may need a tier upgrade | Notify sales/support |
| 5-20% | Possible abuse or misconfigured rule | Investigate immediately |
| >20% | Misconfigured rule or active attack | Alert + potential auto-rollback |

### 3. Decision Latency

P50, P95, P99 latency of the rate limit check. The rate limiter should be **invisible** to request latency.

| Percentile | Target | If Exceeded |
|---|---|---|
| P50 | <0.5ms | — |
| P95 | <1ms | Investigate Redis latency |
| P99 | <2ms | Alert — approaching budget |
| P99.9 | <5ms | Critical — rate limiter becoming bottleneck |

Common causes of high decision latency:
- Redis latency (network congestion, overloaded node)
- Complex Lua scripts (token bucket calculation)
- Too many Redis calls per request (multi-dimension without pipelining)
- Rule matching taking too long (too many rules, no indexing)

### 4. Counter Store Health

Redis health metrics — the rate limiter is only as reliable as its counter store.

| Metric | What to Monitor | Alert Threshold |
|---|---|---|
| Redis latency | P99 response time | > 2ms |
| Connection pool | Active connections / pool size | > 80% utilization |
| Memory usage | Used memory / max memory | > 75% |
| Replication lag | Seconds behind primary | > 1 second |
| Evicted keys | Keys evicted due to memory pressure | Any eviction |
| Cluster health | Number of healthy nodes | < expected count |

### 5. Rule Evaluation Time

Time to match a request against rules. If you have thousands of rules, evaluation time can grow.

| Rule Count | Expected Evaluation Time | Strategy |
|---|---|---|
| < 100 | < 0.1ms (linear scan is fine) | No optimization needed |
| 100 - 1,000 | 0.1 - 1ms | Index rules by resource/tier |
| > 1,000 | > 1ms without optimization | Trie-based matching, precomputed rule groups |

### 6. Quota Utilization per Client

How close each client is to their limit. This is actionable business intelligence.

| Utilization | Interpretation | Action |
|---|---|---|
| < 1% | Client may have stale credentials or abandoned integration | Review with customer success |
| 1-50% | Normal usage | — |
| 50-80% | Healthy, growing usage | Monitor for trend |
| 80-90% | Approaching limit | Proactive outreach for tier upgrade |
| > 90% sustained | At risk of being rate limited | Urgent: notify client, suggest upgrade |

---

## Alerting

### Critical Alerts (PagerDuty — wake someone up)

| Alert | Condition | Why Critical |
|---|---|---|
| **Fail-open event** | Redis unavailable, rate limiter bypassed | Zero rate limiting — system unprotected |
| **Mass rejection spike** | Rejection rate > 20% across all clients | Likely misconfigured rule blocking everyone |
| **Decision latency spike** | P99 > 5ms for > 2 minutes | Rate limiter becoming a bottleneck |

### Warning Alerts (Slack — investigate during business hours)

| Alert | Condition | Why Important |
|---|---|---|
| **Client rejection spike** | Single client rejection rate > 5% | Client may be misbehaving or need upgrade |
| **Redis memory warning** | Memory > 75% of max | Risk of eviction (losing counters) |
| **Rule propagation delay** | Node rule cache > 60s stale | Inconsistent rate limiting across nodes |
| **Quota approaching** | Client consistently > 90% utilization | Proactive engagement for tier upgrade |

### Audit Alerts (Log — review periodically)

| Alert | Condition | Why Important |
|---|---|---|
| **Rule change** | Any rule create/update/delete | Audit trail for post-incident analysis |
| **Override created** | Any override created or expired | Track temporary exemptions |
| **Tier change** | Any client tier change | Track quota changes |

### Auto-Rollback on Misconfigured Rules

If rejection rate spikes >10× within 5 minutes of a rule change:
1. Automatically revert to the previous rule version
2. Alert the on-call engineer
3. Log the auto-rollback event for post-mortem

This prevents a single misconfigured rule from causing a prolonged outage.

---

## Dashboards

### 1. Operations Dashboard — "Is the rate limiter healthy?"

Real-time view for the on-call team.

```
┌──────────────────────────────────────────────────────────┐
│  RATE LIMITER OPERATIONS                                  │
│                                                           │
│  Status: ✅ HEALTHY              Fail-Open: NO           │
│  Decision P99: 0.8ms             Redis P99: 0.4ms        │
│                                                           │
│  ┌─────────────────────────────────────────────────────┐ │
│  │  Allowed vs Rejected (last 1 hour)                   │ │
│  │  ████████████████████████████████████░░ 97% / 3%     │ │
│  │                                                       │ │
│  │  By Layer:                                            │ │
│  │  Edge (L1):     ████████████████████████ 0.1% reject │ │
│  │  Gateway (L2):  ████████████████████████ 2.8% reject │ │
│  │  App (L3):      ████████████████████████ 0.3% reject │ │
│  │  Internal (L4): ████████████████████████ 0.0% reject │ │
│  └─────────────────────────────────────────────────────┘ │
│                                                           │
│  Top Rules Firing:                                        │
│  1. rule-456 (free tier global) — 12,340 rejections/hr  │
│  2. rule-789 (POST /orders per-user) — 892 rejections/hr│
│  3. rule-101 (per-IP limit) — 234 rejections/hr         │
└──────────────────────────────────────────────────────────┘
```

### 2. Client Dashboard — "Why am I getting 429s?"

Per-client view for customer support and sales.

```
┌──────────────────────────────────────────────────────────┐
│  CLIENT: user_abc123                                      │
│  Tier: Pro (1,000 req/min)                               │
│                                                           │
│  Current Quota Usage:                                     │
│  ████████████████████████████████████████░░░░ 847/1,000  │
│  Resets in: 18 seconds                                    │
│                                                           │
│  Last 24 Hours:                                           │
│  ┌───────────────────────────────────────┐               │
│  │ Requests:  142,000                     │               │
│  │ Rejections: 3,200 (2.3%)              │               │
│  │ Peak usage: 98% at 14:30 UTC          │               │
│  │ Primary endpoint: GET /api/v1/users   │               │
│  └───────────────────────────────────────┘               │
│                                                           │
│  Recommendation: Client consistently at >80%.             │
│  Suggest upgrade to Enterprise (10,000 req/min).          │
└──────────────────────────────────────────────────────────┘
```

### 3. Debug Dashboard — "Why was this request rejected?"

Given a request ID, show exactly which rules were evaluated and why the request was allowed or rejected. **Must answer "why was this request rejected?" in <30 seconds.**

```
┌──────────────────────────────────────────────────────────┐
│  REQUEST DEBUG: req-789abc                                 │
│  Time: 2026-02-26 10:15:42.123 UTC                       │
│  Decision: REJECT                                         │
│                                                           │
│  Rules Evaluated:                                         │
│  ┌─────────────────────────────────────────────────────┐ │
│  │ Rule       │ Dimension  │ Limit │ Count │ Result    │ │
│  │────────────│────────────│───────│───────│───────────│ │
│  │ rule-456   │ per_user   │ 1000  │ 847   │ ✅ PASS   │ │
│  │ rule-789   │ per_endpt  │ 10    │ 9     │ ✅ PASS   │ │
│  │ rule-101   │ per_ip     │ 500   │ 501   │ ❌ FAIL   │ │
│  │ rule-202   │ global     │ 5000  │ 3211  │ ✅ PASS   │ │
│  └─────────────────────────────────────────────────────┘ │
│                                                           │
│  Rejection Reason: per_ip limit exceeded (rule-101)      │
│  IP: 203.0.113.42                                         │
│  Counter: 501/500 req/min                                 │
│  Reset at: 2026-02-26 10:16:00 UTC (18s)                 │
└──────────────────────────────────────────────────────────┘
```

---

## Logging and Audit Trail

### Every Rate Limit Decision

Log every decision with structured fields:

```json
{
  "timestamp": "2026-02-26T10:15:42.123Z",
  "request_id": "req-789abc",
  "client_id": "user_abc123",
  "resource": "/api/v1/orders",
  "method": "POST",
  "source_ip": "203.0.113.42",
  "tier": "pro",
  "decision": "reject",
  "rules_evaluated": [
    {"rule_id": "rule-456", "dimension": "per_user", "count": 847, "limit": 1000, "result": "pass"},
    {"rule_id": "rule-101", "dimension": "per_ip", "count": 501, "limit": 500, "result": "fail"}
  ],
  "rejected_by": "rule-101",
  "decision_latency_ms": 0.8
}
```

### Retention

| Data | Retention | Purpose |
|---|---|---|
| Per-request decision logs | 30 days | Debugging, incident investigation |
| Aggregated metrics | 1+ year | Trend analysis, capacity planning |
| Rule change audit log | Permanent | Compliance, post-mortem analysis |
| Override history | 1 year | Audit trail for temporary exemptions |

---

## Contrast: Rate Limiter Monitoring vs API Analytics

| Aspect | Rate Limiter Monitoring | API Analytics (Datadog, New Relic) |
|---|---|---|
| **Focus** | Rate limiter's own health and decisions | Overall API performance |
| **Key metrics** | Rejection rate, decision latency, fail-open events | API latency, error rate, throughput |
| **Answers** | "Why was this request rejected?" | "Why is P99 latency high?" |
| **Connection** | Rate limiter monitoring explains the WHY behind API analytics anomalies |

Example: API analytics says "P99 latency spiked at 14:30." Rate limiter monitoring explains "because Redis went down at 14:28, we failed open, allowed 10× normal traffic, which overloaded the backend."

## Contrast: Rate Limiter Monitoring vs WAF Monitoring

| Aspect | Rate Limiter Monitoring | WAF Monitoring (AWS WAF, Cloudflare) |
|---|---|---|
| **Layer** | Application level (Layer 2-3) | Edge level (Layer 1) |
| **Granularity** | Per-client, per-tier, per-rule | Per-IP, per-rule, per-attack-type |
| **Signals** | User ID, API key, tier, business context | IP, URL, headers, attack signatures |
| **Catches** | Application-level abuse (API quota violations) | Network-level threats (DDoS, bots) |

Both are needed. WAF monitoring is the early warning system. Rate limiter monitoring is the precision instrument.

---

*See also: [Interview Simulation](01-interview-simulation.md) (Attempt 3) for the interview discussion of observability.*
