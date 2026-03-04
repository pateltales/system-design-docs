# Payment System — Reliability, SLAs & Observability Deep Dive

> Companion deep dive to the [interview simulation](01-interview-simulation.md). This document covers why payment systems have the highest reliability bar of any internet service, and the engineering practices required to meet that bar. Payment downtime directly equals lost revenue for every merchant on the platform — there is no "good enough" degradation for a failed payment.

---

## Table of Contents

1. [SLA Targets — Why These Numbers](#1-sla-targets--why-these-numbers)
2. [Health Monitoring — What to Watch](#2-health-monitoring--what-to-watch)
3. [Alerting Hierarchy — Escalation Matrix](#3-alerting-hierarchy--escalation-matrix)
4. [Distributed Tracing — End-to-End Visibility](#4-distributed-tracing--end-to-end-visibility)
5. [Dead Letter Queues — No Silent Drops](#5-dead-letter-queues--no-silent-drops)
6. [Circuit Breakers — Fail Fast, Recover Gracefully](#6-circuit-breakers--fail-fast-recover-gracefully)
7. [Graceful Degradation — The Critical Path](#7-graceful-degradation--the-critical-path)
8. [Disaster Recovery — When Regions Fail](#8-disaster-recovery--when-regions-fail)
9. [Incident Response — Financial Incidents](#9-incident-response--financial-incidents)
10. [Contrast with Netflix — Why Payments Are Harder](#10-contrast-with-netflix--why-payments-are-harder)
11. [Cross-References](#11-cross-references)

---

## 1. SLA Targets — Why These Numbers

Payment system SLAs are not arbitrary aspirational goals. Each number is driven by the financial reality that every second of downtime costs real money for every merchant on the platform.

### The Core SLA Table

| Metric                     | Target        | What It Means                                         |
|----------------------------|---------------|-------------------------------------------------------|
| **Availability (uptime)**  | 99.99%        | Max 52.6 minutes downtime per year                    |
| **Authorization latency**  | p99 < 500ms   | 99th percentile auth response under half a second     |
| **Data loss (RPO)**        | 0             | Zero financial transactions lost, ever                |
| **Recovery time (RTO)**    | < 5 minutes   | Full service restoration after a failure event        |
| **Data durability**        | 99.9999999%   | No ledger entry or payment record ever lost           |

### Why 99.99% and Not 99.9%?

```
Uptime Level    Annual Downtime    Monthly Downtime    Impact on Payment Platform
────────────    ───────────────    ────────────────    ──────────────────────────
99.9%           8h 46m             43.8 min            Unacceptable — nearly 9 hours/year
                                                       of merchants unable to accept payments

99.99%          52.6 min           4.38 min            Industry standard for Tier 1 PSPs
                                                       [UNVERIFIED — inferred from Stripe/Adyen
                                                       public SLA documentation patterns]

99.999%         5.26 min           26.3 sec            Aspirational for critical path only
                                                       (achievable for payment auth, not
                                                       for entire platform including dashboards)
```

**The financial argument**: If a PSP processes $1 billion per day across all merchants [UNVERIFIED — Stripe-scale estimate], every minute of downtime means approximately $694,000 in payments that cannot be processed. At 99.9% uptime (8h 46m/year), that is ~$365 million in unprocessable payments per year. At 99.99% (52.6 min/year), it is ~$36.5 million. Still painful, but an order of magnitude better.

This is why large PSPs like Stripe, Adyen, and PayPal engineer for 99.99% or better on the critical payment path [INFERRED — not officially documented by all named companies].

### Why p99 < 500ms for Authorization?

Payment authorization is synchronous — the merchant's checkout page is waiting. The user is staring at a spinner.

- **p50 target**: ~100-200ms (typical authorization round-trip through acquirer + card network + issuer) [UNVERIFIED — based on general industry benchmarks]
- **p95 target**: ~300ms
- **p99 target**: < 500ms

Beyond 500ms, cart abandonment rates increase significantly. Studies show that each additional 100ms of latency in checkout flows can reduce conversion by ~1% [UNVERIFIED — commonly cited in e-commerce performance literature, exact figures vary by source].

The 500ms budget must include:
1. Network hop: API gateway to payment service (~5ms)
2. Idempotency check: Redis lookup (~1-2ms)
3. Fraud scoring: Rule engine + ML model inference (~20-50ms)
4. Acquirer round-trip: PSP to acquirer to card network to issuer and back (~200-400ms)
5. Ledger write: State transition + ledger entries (~5-10ms)
6. Response serialization and return (~5ms)

The acquirer round-trip dominates the budget. This is why the PSP's own infrastructure latency must be minimized — there is very little room once you subtract the acquirer/network time.

### Why RPO = 0?

**RPO (Recovery Point Objective) = 0** means: no financial transaction can be lost, ever. Not even one. This is not aspirational — it is a hard requirement.

Consider: if a customer's card is charged $500 but the payment record is lost due to a database failure, the customer has lost $500 with no record that the PSP ever received it. This is a regulatory violation, a legal liability, and a trust-destroying event.

To achieve RPO = 0:
- **Synchronous replication** to at least one standby before acknowledging a write
- **Write-ahead logging (WAL)** shipped to durable storage before acknowledging
- **Event log (Kafka)** with `acks=all` and replication factor >= 3

The cost of synchronous replication is higher write latency (typically 1-5ms additional per write). For a payment system, this cost is non-negotiable.

### Why RTO < 5 Minutes?

**RTO (Recovery Time Objective) < 5 minutes** means: after a failure event (DB crash, region failure, network partition), the system must be fully processing payments again within 5 minutes.

5 minutes at $694K/minute = ~$3.5 million in unprocessable payments. This is the maximum acceptable blast radius of a single failure event.

Achieving < 5 minute RTO requires:
- **Automated failover** (no human in the loop for detection and switchover)
- **Pre-warmed standby** (standby DB with synchronous replication, not cold backup)
- **Connection pool pre-warming** (application servers pre-connected to standby)
- **Health check frequency**: Every 5-10 seconds, with failover triggered after 3 consecutive failures

---

## 2. Health Monitoring — What to Watch

A payment system has many dimensions of health. A global "up/down" check is grossly insufficient. You need per-acquirer, per-payment-method, per-geography, and per-endpoint visibility.

### 2.1 Per-Acquirer Success Rate Dashboards

This is the single most important operational metric for a multi-acquirer payment platform.

```
                    Per-Acquirer Success Rate Dashboard
    ┌──────────────────────────────────────────────────────────────┐
    │                                                              │
    │  Acquirer A (Visa/MC domestic)                               │
    │  ████████████████████████████████████████████░░  94.2%       │
    │  ▲ Normal range: 92-96%                                      │
    │                                                              │
    │  Acquirer B (Amex)                                           │
    │  ██████████████████████████████████████░░░░░░░░  85.1%       │
    │  ▼ ALERT: Dropped from 91% baseline (6% decline)            │
    │                                                              │
    │  Acquirer C (International)                                  │
    │  ██████████████████████████████░░░░░░░░░░░░░░░░  78.3%       │
    │  ─ Normal range: 75-82% (international has lower baselines)  │
    │                                                              │
    │  Acquirer D (Local India - UPI/Netbanking)                   │
    │  ████████████████████████████████████████████████  97.8%     │
    │  ▲ Normal range: 96-99%                                      │
    │                                                              │
    └──────────────────────────────────────────────────────────────┘
    Time window: Rolling 15-minute average
    Refresh: Every 30 seconds
```

**Why per-acquirer, not aggregate?** If Acquirer B's success rate drops from 91% to 85%, but your aggregate success rate only dips from 93% to 91%, you might miss it in a global dashboard. But for every Amex transaction routed to Acquirer B, 6% more customers are seeing payment failures. That is unacceptable.

**What causes acquirer success rate drops?**
- Acquirer infrastructure issues (partial outage, increased latency causing timeouts)
- Card network issues (Visa processing delays in a specific region)
- Issuing bank issues (a large issuing bank rejecting more transactions)
- Regulatory changes (new authentication requirements in a geography)
- Time-of-day patterns (batch settlement windows causing temporary congestion)

### 2.2 Per-Payment-Method Success Rates

Different payment methods have inherently different success rate baselines:

| Payment Method         | Typical Success Rate | Why                                                  |
|------------------------|----------------------|------------------------------------------------------|
| Saved card (token)     | 92-96%               | Card already validated; highest success rate          |
| New card               | 85-92%               | More likely to fail 3DS, typo in card number, etc.   |
| UPI (India)            | 95-99%               | Real-time, fewer intermediaries                       |
| Net banking            | 80-90%               | Bank page redirects, session timeouts, user drop-off  |
| Wallets                | 90-95%               | Pre-funded, fewer failure modes                       |
| Bank transfer (ACH/SEPA)| 95-99%             | Batch-based, failures are delayed not real-time       |

[UNVERIFIED — these ranges are approximate industry benchmarks and vary significantly by geography, merchant vertical, and time period]

A drop in card-on-file success rates from 95% to 88% is a critical signal — it likely indicates an acquirer or network issue, not a user error issue.

### 2.3 Latency Percentiles per Endpoint

```
                    Latency Percentile Dashboard (ms)
    ┌──────────────────────────────────────────────────────────────┐
    │ Endpoint                  p50      p95      p99      p99.9  │
    │ ─────────────────────     ────     ────     ────     ─────  │
    │ POST /payments            120      280      480      1200   │
    │ POST /payments/capture     80      150      300       800   │
    │ POST /payments/refund      60      120      250       600   │
    │ GET  /payments/{id}        15       40       80       200   │
    │ POST /webhooks (delivery) 200      800     2000      5000   │
    │ GET  /balance               8       20       50       150   │
    └──────────────────────────────────────────────────────────────┘

    Key insight: POST /payments p99 at 480ms is within SLA (< 500ms).
    If p99 rises above 500ms → immediate alert.
    p99.9 at 1200ms is expected (outlier acquirer responses, 3DS challenges).
```

**Why p99 and not p95 or average?** Averages hide problems. If your average latency is 120ms but your p99 is 2000ms, 1% of your merchants' customers are waiting 2 full seconds. At millions of transactions per day, 1% is tens of thousands of frustrated users daily.

### 2.4 Transaction Funnel Metrics

The payment funnel tracks every transaction from initiation to completion:

```
    Transaction Funnel (last 24 hours)
    ┌─────────────────────────────────────────────────────┐
    │                                                     │
    │  Initiated          1,000,000   ████████████  100%  │
    │       │                                             │
    │       ▼                                             │
    │  Authorized           920,000   ███████████   92%   │
    │       │                 ├── Declined by issuer: 5%  │
    │       │                 ├── Fraud blocked: 2%       │
    │       │                 └── Timeout/error: 1%       │
    │       ▼                                             │
    │  Captured              905,000   ██████████   90.5% │
    │       │                 ├── Voided by merchant: 1%  │
    │       │                 └── Auth expired: 0.5%      │
    │       ▼                                             │
    │  Settled               898,000   ██████████   89.8% │
    │       │                 ├── Settlement pending: 0.5%│
    │       │                 └── Settlement failed: 0.2% │
    │       ▼                                             │
    │  Net (after refunds)   870,000   █████████    87%   │
    │                         └── Refunded: 2.8%          │
    │                                                     │
    └─────────────────────────────────────────────────────┘
```

**Key diagnostic signals from the funnel:**
- **Initiated-to-Authorized drop > 10%**: Something is wrong with fraud rules (too aggressive) or acquirer connectivity.
- **Authorized-to-Captured drop > 2%**: Merchants are voiding too many auths, or auth expiry window is too short.
- **Captured-to-Settled drop > 0.5%**: Settlement engine issue or acquirer reconciliation problem.
- **Refund rate > 5%**: Potential fraud pattern or merchant quality issue.

---

## 3. Alerting Hierarchy — Escalation Matrix

Not all alerts are equal. A payment system needs a tiered alerting strategy that routes the right signal to the right person at the right urgency level.

### The Escalation Matrix

```
    ┌─────────────────────────────────────────────────────────────────────┐
    │                    ALERTING ESCALATION MATRIX                       │
    ├─────────────────────────────────────────────────────────────────────┤
    │                                                                     │
    │  SEVERITY 1 — PAGE ON-CALL IMMEDIATELY (< 5 min response)          │
    │  ═══════════════════════════════════════════════════                 │
    │  ● p99 latency > 500ms for payment auth (SLA breach)               │
    │  ● Overall success rate drop > 5% (from rolling baseline)          │
    │  ● Any acquirer success rate drops to 0% (full acquirer outage)    │
    │  ● Database primary unreachable                                     │
    │  ● Idempotency store (Redis) cluster unreachable                   │
    │  ● Payment service health check failing (zero throughput)          │
    │  │                                                                  │
    │  │  Escalation: On-call engineer → Engineering Manager (15 min)    │
    │  │              → VP Engineering (30 min) → CTO (1 hour)           │
    │  │                                                                  │
    │  SEVERITY 2 — PAGE ON-CALL (< 15 min response)                     │
    │  ═══════════════════════════════════════════════                     │
    │  ● Single acquirer success rate drop > 5% (partial degradation)    │
    │  ● p99 latency > 400ms (approaching SLA breach)                   │
    │  ● Webhook delivery failure rate > 10% (merchants not getting      │
    │    notified)                                                        │
    │  ● Fraud engine response time > 100ms (slowing auth pipeline)     │
    │  ● Kafka consumer lag > 10,000 messages (event processing delay)  │
    │  │                                                                  │
    │  │  Escalation: On-call engineer → Team lead (30 min)              │
    │  │                                                                  │
    │  SEVERITY 3 — ALERT (< 1 hour response, business hours)            │
    │  ═══════════════════════════════════════════════════                 │
    │  ● Any payment stuck in "pending" state > 10 minutes               │
    │  ● DLQ depth > 100 messages (backlog of unprocessed failures)     │
    │  ● Reconciliation discrepancy detected (any amount)                │
    │  ● Certificate expiry < 7 days (TLS certs for acquirer             │
    │    connections)                                                      │
    │  ● Disk usage > 80% on any payment service node                   │
    │  │                                                                  │
    │  │  Escalation: On-call engineer → relevant team in Slack          │
    │  │                                                                  │
    │  SEVERITY 4 — NOTIFY FINANCE TEAM                                   │
    │  ══════════════════════════════════                                  │
    │  ● Reconciliation discrepancy > $1,000                             │
    │  ● Settlement batch failure (merchants not paid on time)           │
    │  ● Chargeback rate for any merchant > 1% (card network penalty     │
    │    threshold)                                                       │
    │  ● Refund rate for any merchant > 5% (potential fraud pattern)     │
    │  │                                                                  │
    │  │  Escalation: Finance team → Risk team → Compliance              │
    │  │                                                                  │
    │  SEVERITY 5 — INFORMATIONAL (dashboard / weekly review)             │
    │  ══════════════════════════════════════════════════                  │
    │  ● Gradual latency increase trend (p99 rising over days)           │
    │  ● Acquirer success rate drift (slow decline over weeks)           │
    │  ● Storage growth projections                                      │
    │  ● Traffic pattern changes (new merchant onboarding spikes)        │
    │                                                                     │
    └─────────────────────────────────────────────────────────────────────┘
```

### Alert Fatigue Prevention

Alert fatigue is a real operational risk. If engineers receive 50 pages per week, they start ignoring them. Payment alert design must follow these principles:

1. **Every page must be actionable** — if there is no action to take, it should not be a page.
2. **Deduplication** — do not fire the same alert every 30 seconds. Fire once, suppress for a configurable window (e.g., 5 minutes), re-fire if still active.
3. **Auto-resolve** — alerts that recover automatically should auto-close (e.g., a brief latency spike that resolves in 2 minutes).
4. **Context in the alert** — include: what metric breached, what the current value is, what the threshold is, a runbook link, and a direct link to the relevant dashboard.
5. **Regular alert review** — monthly review of all alerts: which fired, which were actionable, which should be tuned.

---

## 4. Distributed Tracing — End-to-End Visibility

When a merchant reports "payment XYZ failed," the support engineer needs to trace the exact path that payment took through every service in the system. Without distributed tracing, debugging multi-service payment failures is guesswork.

### Trace Propagation Through the Payment Stack

```
    Trace ID: abc-123-def-456
    ┌──────────────────────────────────────────────────────────────────┐
    │                                                                  │
    │  [API Gateway]                                                   │
    │  span: api.receive_payment                                       │
    │  time: 0ms ─────────────────────────────────────── 485ms         │
    │  │  headers: { X-Trace-Id: abc-123-def-456,                      │
    │  │            X-Idempotency-Key: merchant-uuid-789 }             │
    │  │                                                               │
    │  ▼                                                               │
    │  [Payment Service]                                               │
    │  span: payment.process                                           │
    │  time: 5ms ────────────────────────────────────── 480ms          │
    │  │  attrs: { merchant_id: "m_123", amount: 5000,                 │
    │  │           currency: "USD", payment_method: "card" }           │
    │  │                                                               │
    │  ├──▶ [Idempotency Check]                                        │
    │  │    span: idempotency.check                                    │
    │  │    time: 6ms ── 8ms  (Redis lookup, 2ms)                      │
    │  │    result: NEW_REQUEST                                        │
    │  │                                                               │
    │  ├──▶ [Fraud Engine]                                             │
    │  │    span: fraud.score                                          │
    │  │    time: 10ms ────── 45ms  (35ms total)                       │
    │  │    attrs: { risk_score: 23, decision: "ALLOW",                │
    │  │             rules_triggered: ["velocity_ok", "geo_ok"],       │
    │  │             ml_model_version: "v2.3.1" }                      │
    │  │                                                               │
    │  ├──▶ [Acquirer Adapter — Acquirer A]                            │
    │  │    span: acquirer.authorize                                   │
    │  │    time: 50ms ──────────────────────── 430ms (380ms)          │
    │  │    attrs: { acquirer: "acquirer_a", network: "visa",          │
    │  │             response_code: "00", auth_code: "A12345",         │
    │  │             network_latency_ms: 350 }                         │
    │  │    │                                                          │
    │  │    └──▶ [Card Network + Issuer]  (external, not traced)       │
    │  │         estimated: 200-350ms                                  │
    │  │                                                               │
    │  ├──▶ [Ledger Service]                                           │
    │  │    span: ledger.create_entries                                │
    │  │    time: 435ms ── 470ms  (35ms)                               │
    │  │    attrs: { entries: 2, debit_account: "customer_card",       │
    │  │             credit_account: "merchant_pending" }              │
    │  │                                                               │
    │  └──▶ [Event Publisher]                                          │
    │       span: events.publish                                       │
    │       time: 472ms ── 478ms  (6ms, async fire-and-forget)         │
    │       attrs: { topic: "payment.authorized",                      │
    │                partition: 42 }                                    │
    │                                                                  │
    └──────────────────────────────────────────────────────────────────┘
    Total trace duration: 485ms
    Critical path: Acquirer round-trip (380ms / 485ms = 78% of total)
```

### Implementation: OpenTelemetry + Jaeger/Tempo

The industry standard for distributed tracing is **OpenTelemetry (OTel)** — a vendor-neutral observability framework that provides APIs, SDKs, and a collector for traces, metrics, and logs. OpenTelemetry is a CNCF project that emerged from the merger of OpenTracing and OpenCensus.

**Key components:**

| Component                | Role                                                      |
|--------------------------|-----------------------------------------------------------|
| **OTel SDK**             | Instruments each service; creates spans, propagates context|
| **Trace context**        | W3C Trace Context header (traceparent) propagated via HTTP headers and Kafka message headers |
| **OTel Collector**       | Receives spans from all services, batches, and exports     |
| **Backend (Jaeger/Tempo)** | Stores and queries traces. Jaeger is widely used; Grafana Tempo is newer and uses object storage for cost efficiency |
| **Frontend (Jaeger UI / Grafana)** | Visualizes traces, identifies slow spans, surfaces errors |

**Why this matters for payments specifically:**

1. **"Why did this payment fail?"** — The trace shows exactly which service returned an error, what the error code was, and what the upstream context was. Without tracing, the support engineer has to manually correlate logs across 5+ services using timestamps.

2. **Latency attribution** — The trace shows that 78% of authorization latency is the acquirer round-trip. This tells the routing team to consider a different acquirer for this card type, not to optimize the fraud engine.

3. **Acquirer behavior analysis** — Aggregate traces reveal that Acquirer B has a bimodal latency distribution (200ms or 1500ms, nothing in between), suggesting their infrastructure has a queueing bottleneck.

### Trace Sampling Strategy

At millions of transactions per day, tracing every transaction is expensive. But for a payment system, you cannot afford to miss the interesting ones.

**Recommended sampling strategy:**
- **100% for errors** — every failed payment is traced in full. Non-negotiable for debugging.
- **100% for high-value transactions** — above a configurable threshold (e.g., > $10,000).
- **100% for flagged merchants** — merchants under investigation or newly onboarded.
- **10-20% for normal successful transactions** — sufficient for latency analysis and dashboards.
- **Head-based sampling** — decide at the API gateway whether to sample this trace, propagate the decision downstream. This ensures either all spans or no spans for a given trace (no orphaned spans).

---

## 5. Dead Letter Queues — No Silent Drops

In a financial system, **no event can be silently dropped**. If a webhook delivery fails, if a ledger posting fails, if a settlement record fails to persist — it must be captured, stored, and eventually resolved. The Dead Letter Queue (DLQ) is the safety net.

### The DLQ Flow

```
    ┌─────────────────────────────────────────────────────────────────┐
    │                       DLQ FLOW DIAGRAM                          │
    │                                                                 │
    │                                                                 │
    │  ┌───────────┐    success    ┌──────────────┐                   │
    │  │  Event     │─────────────▶│  Destination  │                  │
    │  │  Source    │              │  Service      │                   │
    │  │ (Kafka)   │              │  (e.g., Ledger│                   │
    │  │           │              │   Webhooks,   │                   │
    │  │           │              │   Settlement) │                   │
    │  └───────────┘              └──────────────┘                    │
    │       │                           │                             │
    │       │                           │ failure                     │
    │       │                           ▼                             │
    │       │                    ┌──────────────┐                     │
    │       │                    │  Retry Logic  │                    │
    │       │                    │              │                     │
    │       │                    │ Attempt 1: immediate               │
    │       │                    │ Attempt 2: 1 sec delay             │
    │       │                    │ Attempt 3: 5 sec delay             │
    │       │                    │ Attempt 4: 30 sec delay            │
    │       │                    │ Attempt 5: 2 min delay             │
    │       │                    └──────────────┘                     │
    │       │                           │                             │
    │       │                           │ all retries exhausted       │
    │       │                           ▼                             │
    │       │                    ┌──────────────────┐                 │
    │       │                    │  DEAD LETTER      │                │
    │       │                    │  QUEUE (DLQ)      │                │
    │       │                    │                   │                │
    │       │                    │ Stores:            │                │
    │       │                    │ - Original event   │                │
    │       │                    │ - All error msgs   │                │
    │       │                    │ - Retry count      │                │
    │       │                    │ - Timestamps        │               │
    │       │                    │ - Source service    │                │
    │       │                    │ - Dest service      │               │
    │       │                    └──────────────────┘                 │
    │       │                           │                             │
    │       │                           ▼                             │
    │       │                    ┌──────────────────┐                 │
    │       │                    │  OPS DASHBOARD    │                │
    │       │                    │                   │                │
    │       │                    │ ● View DLQ depth  │                │
    │       │                    │ ● Inspect events   │               │
    │       │                    │ ● Manual replay    │               │
    │       │                    │ ● Bulk replay      │               │
    │       │                    │ ● Discard (with    │               │
    │       │                    │   audit trail)     │               │
    │       │                    └──────────────────┘                 │
    │                                                                 │
    └─────────────────────────────────────────────────────────────────┘
```

### What Goes Into the DLQ?

| Event Type                | Failure Scenario                                   | Impact If Dropped                          |
|---------------------------|----------------------------------------------------|--------------------------------------------|
| **Webhook delivery**      | Merchant endpoint returns 5xx or is unreachable    | Merchant does not know payment succeeded — may not ship the order |
| **Ledger posting**        | Ledger service DB is temporarily unavailable       | Financial records are incomplete — reconciliation will flag discrepancy |
| **Settlement record**     | Settlement batch fails for a subset of merchants   | Merchant does not receive payout on schedule |
| **Refund processing**     | Acquirer refund API returns transient error         | Customer does not get their money back      |
| **Event publish**         | Kafka is temporarily unavailable                   | Downstream consumers miss the event         |

### DLQ Operational Practices

1. **DLQ depth is a Severity 3 alert** — if the DLQ has > 100 messages, something systemic is wrong and needs attention within 1 hour.

2. **Every DLQ message must be resolved** — either replayed successfully or explicitly discarded with a documented reason. The DLQ must trend toward zero.

3. **Replay must be idempotent** — since the original processing may have partially succeeded, replaying a DLQ message must not cause duplicates. This is why every event handler must be idempotent (see [04-idempotency-and-exactly-once.md](04-idempotency-and-exactly-once.md)).

4. **DLQ retention** — retain messages for at least 30 days. Some regulatory requirements mandate longer retention of financial event failures.

5. **Separate DLQs per event type** — do not mix webhook DLQ messages with ledger DLQ messages. They have different urgency, different resolution procedures, and different responsible teams.

---

## 6. Circuit Breakers — Fail Fast, Recover Gracefully

A circuit breaker prevents a failing downstream service from cascading failures upstream. In a payment system, the most critical circuit breakers are on **acquirer connections** and **webhook delivery endpoints**.

### The Three-State Circuit Breaker

```
    ┌──────────────────────────────────────────────────────────────────┐
    │                 CIRCUIT BREAKER STATE DIAGRAM                     │
    │                                                                  │
    │                                                                  │
    │                      All requests                                │
    │                      pass through                                │
    │                    ┌──────────────┐                               │
    │           ┌───────▶│              │                               │
    │           │        │   CLOSED     │                               │
    │           │        │  (healthy)   │                               │
    │           │        │              │                               │
    │           │        └──────┬───────┘                               │
    │           │               │                                      │
    │           │               │ Failure threshold exceeded            │
    │           │               │ (e.g., 5 failures in 30 sec          │
    │           │               │  OR success rate < 80%)              │
    │           │               │                                      │
    │           │               ▼                                      │
    │           │        ┌──────────────┐                               │
    │           │        │              │                               │
    │           │        │    OPEN      │──── All requests fail         │
    │           │        │  (tripped)   │     immediately with          │
    │           │        │              │     "circuit open" error      │
    │           │        └──────┬───────┘                               │
    │           │               │                                      │
    │           │               │ Timeout expires                      │
    │           │               │ (e.g., 30 seconds)                   │
    │           │               │                                      │
    │           │               ▼                                      │
    │           │        ┌──────────────┐                               │
    │           │        │              │                               │
    │    Success│        │  HALF-OPEN   │──── Allow ONE probe           │
    │    (close │        │  (probing)   │     request through           │
    │    circuit│        │              │                               │
    │     again)│        └──────┬───────┘                               │
    │           │               │                                      │
    │           │               │ Probe fails                          │
    │           │               │ → back to OPEN                       │
    │           │               │   (reset timeout,                    │
    │           │               │    possibly increase it)             │
    │           └───────────────┘                                      │
    │                                                                  │
    │                                                                  │
    │  TIMING PARAMETERS:                                              │
    │  ─────────────────                                               │
    │  ● Failure threshold:  5 failures in 30-second window            │
    │  ● Open duration:      30 seconds (first trip)                   │
    │  ● Backoff:            Exponential — 30s, 60s, 120s, max 300s   │
    │  ● Half-open probes:   1 request (conservative for payments)     │
    │  ● Success to close:   3 consecutive successes in half-open      │
    │                                                                  │
    └──────────────────────────────────────────────────────────────────┘
```

### Circuit Breaker on Acquirer Connections

This is the most critical circuit breaker in the system. When an acquirer is down, continuing to send requests to it:
- Wastes the latency budget (timeout waiting for a response that will never come)
- Increases failure rate for merchants
- May overwhelm the acquirer further when it recovers (thundering herd)

**When the circuit opens on Acquirer A:**

```
    Normal flow:
    Payment ──▶ Router ──▶ Acquirer A ──▶ Card Network ──▶ Issuer

    Circuit open on Acquirer A:
    Payment ──▶ Router ──▶ [Circuit OPEN on A] ──▶ Acquirer B (failover)
                              │                        │
                              │ Fail fast (< 1ms)      │ Full processing
                              │ No wasted timeout      │ (350ms)
                              ▼                        ▼
                         Log event               Return auth result
                         for monitoring
```

**Important nuance**: Not all acquirers support the same card types or geographies. When failing over from Acquirer A to Acquirer B, the router must verify that Acquirer B can handle this specific transaction (correct card network, correct currency, correct geography). If no backup acquirer is eligible, the payment must fail with a clear error — do not silently route to an incompatible acquirer.

### Circuit Breaker on Webhook Delivery

Merchants' webhook endpoints go down regularly (deployments, outages, misconfiguration). Without a circuit breaker, the webhook delivery service will:
- Keep hammering the dead endpoint
- Fill up retry queues
- Waste resources on doomed requests
- Potentially trigger rate limiting on the merchant's infrastructure

**Webhook circuit breaker behavior:**

| State       | Behavior                                                       |
|-------------|----------------------------------------------------------------|
| **Closed**  | Deliver webhooks normally. Track success/failure per endpoint. |
| **Open**    | Stop delivery attempts. Queue events for this endpoint. Alert merchant via email/dashboard that their webhook endpoint is unreachable. |
| **Half-Open** | Send one probe event. If it succeeds, flush queued events (with rate limiting to avoid burst). If it fails, go back to Open. |

### Circuit Breaker vs Retry — When to Use Which

| Scenario                              | Use Retry           | Use Circuit Breaker    |
|---------------------------------------|----------------------|------------------------|
| Single transient failure (one 503)    | Yes (immediate retry)| No (not yet a pattern) |
| 3+ failures in quick succession       | Stop retrying        | Yes (trip the circuit)  |
| Acquirer returning errors for 2+ min  | No                   | Yes (fail fast, route to backup) |
| Webhook endpoint down for 1 hour      | No                   | Yes (stop hammering, queue events) |
| Database momentary connection reset   | Yes (with backoff)   | Only if persistent (> 30 sec) |

---

## 7. Graceful Degradation — The Critical Path

A payment system is composed of many services, but not all of them are equally critical. The key design principle is: **define the critical path and protect it. Everything else can degrade without stopping payments.**

### The Critical Path

```
    ┌─────────────────────────────────────────────────────────────────┐
    │                                                                 │
    │  CRITICAL PATH (must be up for payments to work):               │
    │  ══════════════════════════════════════════════                  │
    │                                                                 │
    │  Merchant ──▶ API Gateway ──▶ Payment Service ──▶ Acquirer      │
    │                    │               │                            │
    │                    │               ├── Idempotency Store        │
    │                    │               │   (Redis — critical)       │
    │                    │               │                            │
    │                    │               └── Payment DB               │
    │                    │                   (PostgreSQL — critical)   │
    │                    │                                            │
    │                                                                 │
    │  NON-CRITICAL PATH (can degrade without stopping payments):     │
    │  ═════════════════════════════════════════════════════           │
    │                                                                 │
    │  ● Fraud Engine ──── can fall back to rules-only               │
    │  ● Analytics ──────── can be delayed or unavailable            │
    │  ● Reporting ──────── can be delayed or unavailable            │
    │  ● Webhooks ───────── can be delayed (queued for later)        │
    │  ● Dashboard ──────── can be stale or unavailable              │
    │  ● Settlement ─────── batch job, can be delayed hours          │
    │  ● Reconciliation ─── batch job, can be delayed hours          │
    │                                                                 │
    └─────────────────────────────────────────────────────────────────┘
```

### Degradation Scenarios and Responses

| Component Down       | Impact                            | Fallback Strategy                                          | Risk During Fallback                        |
|----------------------|-----------------------------------|------------------------------------------------------------|---------------------------------------------|
| **Fraud engine**     | No ML scoring for transactions    | Fall back to rules-only engine (velocity checks, amount limits, BIN checks) | Higher fraud exposure; acceptable for minutes, not hours |
| **Analytics service**| No real-time dashboards           | Payments continue normally. Dashboards show stale data.    | Minimal — no financial risk                 |
| **Reporting service**| Settlement reports delayed         | Payments continue. Reports generated when service recovers. | Merchants cannot see reports temporarily    |
| **Webhook service**  | Merchants not notified in real-time| Events queued in Kafka. Delivered when service recovers. Merchants can poll the Events API as fallback. | Merchants may have delayed order fulfillment |
| **Ledger service**   | Ledger entries not posted          | **GRAY AREA** — payment can proceed, but ledger entries go to DLQ for later replay. Financial records temporarily incomplete. | Reconciliation discrepancies until replayed. Risk accepted for short duration only. |
| **Redis (idempotency)** | Cannot check for duplicate requests | **CRITICAL** — options: (a) fail open (allow requests, risk duplicates), or (b) fail closed (reject all requests until Redis recovers). Most PSPs choose (b) — better to reject than to double-charge. [INFERRED — not officially documented] |
| **PostgreSQL (primary)** | Cannot persist payment records | **CRITICAL** — automatic failover to standby. During failover window (< 5 min), payments are rejected. | Total payment outage during failover. This is the RTO scenario. |

### Design Principle: Async Everything That Isn't Authorization

The critical insight: the only synchronous operation in a payment is the authorization response. Everything else can (and should) be asynchronous:

```
    Synchronous (merchant is waiting):       Asynchronous (merchant is not waiting):
    ──────────────────────────────           ──────────────────────────────────────
    ● Receive payment request               ● Webhook delivery
    ● Idempotency check                     ● Ledger posting (can be ms-delayed)
    ● Fraud scoring (can degrade)           ● Analytics event publishing
    ● Acquirer authorization                ● Reconciliation
    ● Payment state persistence             ● Settlement batch
    ● Return auth response                  ● Reporting
                                            ● Email notifications
```

By making everything except the core auth flow asynchronous, you minimize the blast radius of any single component failure. If the webhook service goes down, payments keep flowing. If the analytics pipeline is backed up, payments keep flowing.

---

## 8. Disaster Recovery — When Regions Fail

Payment system DR is not optional. It is tested, automated, and exercised regularly. A DR event in a payment system is not just a technical incident — it is a financial event that may need to be reported to regulators.

### Replication Architecture

```
    ┌─────────────────────────────────────────────────────────────────┐
    │                                                                 │
    │  PRIMARY REGION (us-east-1)          STANDBY REGION (us-west-2) │
    │  ═════════════════════════           ════════════════════════    │
    │                                                                 │
    │  ┌───────────────┐     Synchronous    ┌───────────────┐         │
    │  │ PostgreSQL     │ ──replication───▶  │ PostgreSQL     │        │
    │  │ Primary        │    (WAL stream)    │ Standby        │        │
    │  │                │                    │ (hot standby)  │        │
    │  └───────────────┘                    └───────────────┘         │
    │                                                                 │
    │  ┌───────────────┐     Replication     ┌───────────────┐        │
    │  │ Redis Cluster  │ ──────────────────▶│ Redis Cluster  │       │
    │  │ (idempotency)  │   (async, ~ms lag) │ (warm standby) │       │
    │  └───────────────┘                    └───────────────┘         │
    │                                                                 │
    │  ┌───────────────┐     MirrorMaker     ┌───────────────┐        │
    │  │ Kafka Cluster  │ ──────────────────▶│ Kafka Cluster  │       │
    │  │ (event log)    │   (async, ~sec lag)│ (standby)      │       │
    │  └───────────────┘                    └───────────────┘         │
    │                                                                 │
    │  ┌───────────────┐                    ┌───────────────┐         │
    │  │ App Servers    │                    │ App Servers    │        │
    │  │ (active)       │                    │ (warm, pre-    │        │
    │  │                │                    │  connected)    │        │
    │  └───────────────┘                    └───────────────┘         │
    │                                                                 │
    └─────────────────────────────────────────────────────────────────┘
```

### Why Active-Passive, Not Active-Active?

For most payment systems, **active-passive** is preferred over active-active:

| Approach          | Pro                                  | Con                                                        |
|-------------------|--------------------------------------|------------------------------------------------------------|
| **Active-Passive**| Strong consistency trivial. Single write region. Simple failover. | Standby region is "wasted" capacity during normal operation. Cross-region latency for non-local merchants. |
| **Active-Active** | Both regions serve traffic. Lower latency for all merchants. Better resource utilization. | Financial consistency across regions is extremely hard. Split-brain risk. Double-charge risk if both regions process the same payment. Conflict resolution for ledger entries is a nightmare. |

Active-active payment processing requires distributed consensus for every payment write, which adds latency and complexity. For the vast majority of PSPs, the engineering cost of active-active does not justify the benefit. Only the largest PSPs (Visa, Mastercard network-level processing) operate active-active, and they have decades of custom infrastructure for it [INFERRED — not officially documented].

### Automated Failover Sequence

```
    Time    Event
    ─────   ─────────────────────────────────────────────────────
    T+0s    Health check detects primary DB unreachable
    T+5s    Second health check fails (confirmation)
    T+10s   Third health check fails → trigger failover
    T+15s   Automated failover begins:
              1. Promote standby PostgreSQL to primary
              2. Update DNS / service discovery to point to new primary
              3. App servers in standby region detect new primary
              4. Redis cluster in standby promoted
    T+60s   Standby region begins accepting payment traffic
    T+90s   All payment traffic flowing through new primary region
    T+120s  Kafka consumers in standby region begin processing
    T+180s  Webhook delivery resumes from standby region
    T+300s  Full system operational (< 5 min RTO target met)
```

### Post-Failover Reconciliation

After a failover, there is a critical window where transactions may be in an ambiguous state:

1. **Transactions committed to old primary but not yet replicated** — With synchronous replication and RPO = 0, this should not happen. But if async replication was in use (e.g., for Redis), there may be a small window of lost idempotency keys.

2. **In-flight transactions during failover** — Transactions that were being processed when the primary went down. These are in an unknown state. The reconciliation process must:
   - Query acquirers for the status of all in-flight transactions
   - Compare internal records against acquirer records
   - Resolve discrepancies (void authorizations that were not captured, confirm captured payments)

3. **Idempotency gap** — If a merchant retried during the failover window and the idempotency key was lost, there is a risk of duplicate processing. Post-failover reconciliation must detect and resolve any duplicates.

### DR Drill Schedule

DR is not a "set it and forget it" capability. It must be exercised regularly:

| Drill Type                    | Frequency     | What It Tests                                           |
|-------------------------------|---------------|---------------------------------------------------------|
| **Automated DB failover**     | Monthly       | PostgreSQL promotion, DNS update, app reconnection      |
| **Full region failover**      | Quarterly     | All services, all data stores, complete traffic shift    |
| **Chaos engineering (random)** | Weekly       | Random service/instance termination during business hours|
| **Acquirer failover**         | Monthly       | Circuit breaker activation, traffic rerouting to backup acquirer |
| **Kafka cluster failure**     | Quarterly     | Event log failover, consumer rebalancing                 |

---

## 9. Incident Response — Financial Incidents

Payment system incidents are not just engineering incidents — they are **financial incidents**. A 10-minute payment outage means real money was not processed, real merchants could not serve their customers, and real cardholders may have been charged without confirmation. The incident response process must reflect this gravity.

### Incident Classification

| Incident Class     | Example                                    | Response Time | Who Is Involved                     |
|--------------------|--------------------------------------------|---------------|--------------------------------------|
| **P0 — Critical**  | All payments failing (total outage)        | Immediate     | All hands, war room, exec notification|
| **P1 — Major**     | Single acquirer down (partial outage)      | < 15 min      | On-call + team lead + acquirer relations |
| **P2 — Moderate**  | Elevated latency, degraded success rate    | < 1 hour      | On-call engineer                     |
| **P3 — Minor**     | Dashboard unavailable, reports delayed     | < 4 hours     | On-call engineer, business hours     |
| **P4 — Cosmetic**  | Non-critical log errors, UI glitches       | Next sprint   | Engineering backlog                  |

### Runbooks for Common Scenarios

Every foreseeable incident type must have a written runbook. On-call engineers at 3 AM should not need to improvise.

**Runbook 1: Acquirer Down**
```
    TRIGGER: Circuit breaker tripped on Acquirer X, OR Acquirer X success rate < 50%

    STEP 1: Verify — check Acquirer X's status page and support channel
    STEP 2: Confirm circuit breaker has activated and traffic is routing to backup
    STEP 3: Monitor backup acquirer success rate (ensure it is handling the load)
    STEP 4: If no backup acquirer available for affected card types:
            → Post merchant-facing status page update
            → Notify merchant support team
    STEP 5: Contact Acquirer X support for ETA on resolution
    STEP 6: When Acquirer X recovers:
            → Circuit breaker will probe automatically (half-open state)
            → Monitor success rate during ramp-back
            → Verify no stuck transactions from the outage period
    STEP 7: Post-incident: reconcile all transactions during the outage window
```

**Runbook 2: Database Failover**
```
    TRIGGER: Primary DB health check failing for > 10 seconds

    STEP 1: Automated failover should trigger — verify it has started
    STEP 2: If automated failover did NOT trigger:
            → Manually promote standby: pg_ctl promote
            → Update service discovery / DNS
    STEP 3: Monitor application reconnection to new primary
    STEP 4: Verify payment throughput is recovering
    STEP 5: Run post-failover reconciliation job
    STEP 6: Plan recovery of old primary (rebuild as new standby)
    STEP 7: Post-incident: analyze why failover was needed,
            prevent recurrence
```

**Runbook 3: High Fraud Rate**
```
    TRIGGER: Fraud rate > 2% over 15-minute window (baseline: ~0.5%)

    STEP 1: Check fraud engine dashboards — is the ML model returning
            anomalous scores?
    STEP 2: Check if a specific merchant is driving the spike
            → If yes: temporarily increase risk threshold for that merchant
            → If widespread: increase global risk threshold (more 3DS challenges)
    STEP 3: Check for known fraud attack patterns (BIN attacks, credential
            stuffing)
    STEP 4: If fraud engine is malfunctioning:
            → Fall back to rules-only mode
            → Page ML engineering team
    STEP 5: Notify risk/compliance team
    STEP 6: Post-incident: analyze fraud patterns, update rules, retrain model
```

**Runbook 4: Reconciliation Mismatch**
```
    TRIGGER: Reconciliation job detects discrepancy > $1,000

    STEP 1: Identify the scope — which acquirer, which time window,
            how many transactions affected
    STEP 2: Classify the discrepancy:
            ● Timing difference (transaction in our ledger, not yet in
              acquirer file) → likely benign, will resolve in next cycle
            ● Amount mismatch → investigate individual transactions
            ● Missing from acquirer → did the acquirer receive and
              process these? Query their API.
            ● Missing from our ledger → DLQ or event processing failure?
              Check DLQ depth.
    STEP 3: If > $10,000 discrepancy: escalate to Finance immediately
    STEP 4: If any transactions are missing from both sides:
            → This is a potential data loss event. Escalate to P0.
    STEP 5: Document resolution for each discrepancy
    STEP 6: Post-incident: identify root cause and implement prevention
```

### Post-Incident Process

Every P0 and P1 incident must have a post-incident review (PIR, also known as a postmortem):

1. **Timeline** — minute-by-minute reconstruction of what happened
2. **Impact** — number of failed payments, dollar amount, number of affected merchants
3. **Root cause** — the actual cause, not "server crashed" but "why did the server crash and why did failover not handle it"
4. **Action items** — concrete, assigned, and tracked to completion
5. **Financial impact assessment** — for regulatory reporting and merchant communication

Payment PIRs often involve the Finance, Legal, and Compliance teams in addition to Engineering. If the incident caused merchants to lose revenue, there may be SLA credit obligations.

---

## 10. Contrast with Netflix — Why Payments Are Harder

This comparison is instructive because Netflix is often held up as the gold standard for reliability engineering (Chaos Monkey, etc.). But the reliability challenges for a payment system are fundamentally different and, in key ways, harder.

### The Core Difference: Degradation Is an Option vs Not an Option

```
    ┌────────────────────────────────────┬────────────────────────────────────┐
    │          NETFLIX                    │          PAYMENT SYSTEM            │
    ├────────────────────────────────────┼────────────────────────────────────┤
    │                                    │                                    │
    │  Recommendation engine down?       │  Fraud engine down?                │
    │  → Show "Popular on Netflix" row   │  → Fall back to rules-only         │
    │  → Users see generic content       │  → Accept higher fraud risk        │
    │  → Degraded but FUNCTIONAL         │  → Degraded but FUNCTIONAL         │
    │                                    │                                    │
    │  Personalization engine down?      │  Acquirer down?                    │
    │  → Show default homepage           │  → Route to backup acquirer        │
    │  → Users can still watch           │  → OR payment FAILS               │
    │  → Degraded but FUNCTIONAL         │  → No "default payment"           │
    │                                    │                                    │
    │  One CDN edge down?               │  Database down?                    │
    │  → Route to another edge           │  → Failover to standby            │
    │  → Minor quality/latency impact    │  → ALL payments stop until         │
    │  → Degraded but FUNCTIONAL         │    failover completes             │
    │                                    │  → Total outage, no degradation   │
    │                                    │                                    │
    │  Video quality degrades?           │  Payment "degrades"?              │
    │  → Drop from 4K to 1080p to 720p  │  → NOT POSSIBLE                   │
    │  → Still watchable                 │  → A payment either succeeds      │
    │  → Graceful quality reduction      │    or fails. You cannot charge     │
    │                                    │    "720p of $100."                │
    │                                    │                                    │
    │  Search engine down?              │  Ledger service down?             │
    │  → Users can still browse          │  → Payments can proceed but        │
    │  → Degraded but FUNCTIONAL         │    financial records are           │
    │                                    │    temporarily incomplete          │
    │                                    │  → FINANCIAL INTEGRITY AT RISK    │
    │                                    │                                    │
    └────────────────────────────────────┴────────────────────────────────────┘
```

### Key Differences in Reliability Engineering

| Dimension                | Netflix                                        | Payment System                                          |
|--------------------------|------------------------------------------------|---------------------------------------------------------|
| **Consistency model**    | Eventual consistency is fine. Who cares if "continue watching" is 30 seconds stale? | Strong consistency required. A balance showing wrong amount = financial liability. |
| **Data loss tolerance**  | Losing a user's viewing history row is annoying but not catastrophic. | Losing a payment record is a legal and financial incident. RPO must be 0. |
| **Idempotency urgency**  | Showing a recommendation twice is harmless.    | Processing a payment twice is charging the customer twice. |
| **Failure mode**         | Binary per-content: either you can stream this title or you cannot. But the platform has millions of titles — one unavailable title is invisible. | Binary per-transaction: either this payment succeeds or fails. There is no substitute. |
| **Blast radius**         | A failure affects viewing experience for some users for some content. | A failure affects the ability of ALL merchants to accept ANY payment. |
| **Regulatory exposure**  | Minimal — streaming is not regulated like financial services. | Extensive — PCI DSS, SOX, PSD2, RBI guidelines (India), state money transmitter licenses (US). |
| **SLA penalties**        | Users might cancel subscription (indirect revenue loss). | Merchants lose revenue directly. PSP may owe SLA credits. Regulatory fines possible. |

### What Netflix Got Right That Payments Should Adopt

Despite the differences, several Netflix reliability practices apply directly to payment systems:

1. **Chaos engineering** — Netflix's Chaos Monkey randomly kills instances in production. Payment systems should do this (carefully, during low-traffic windows, with circuit breakers and failover in place) to verify that failover actually works. [Netflix's Simian Army is well-documented in their tech blog.]

2. **Bulkhead isolation** — Netflix isolates services so one service's failure does not cascade. Payment systems must do the same — the fraud engine crashing must not take down the payment service.

3. **Timeout budgets** — Netflix sets strict timeout budgets for every service call. Payment systems must do the same, especially on the critical path where the total latency budget is 500ms.

4. **Feature flags** — Netflix can disable features in real-time. Payment systems should be able to disable non-critical features (e.g., turn off ML fraud scoring and fall back to rules) without a deployment.

### What Payments Must Do Differently

1. **No eventual consistency on the critical path** — Netflix can serve stale data. Payments cannot serve stale balances or stale payment states.

2. **No silent drops** — Netflix can drop a metric or a log line. Payments cannot drop a financial event. Hence the DLQ.

3. **No "retry later"** — Netflix can show "try again in a few minutes." A payment checkout cannot tell the customer "try buying this in a few minutes." The customer will go to a competitor.

4. **Auditability** — Every action in a payment system must be traceable for regulatory compliance. Netflix does not need to explain to a regulator why a particular movie was recommended.

---

## 11. Cross-References

| Topic                               | Document                                                              |
|--------------------------------------|-----------------------------------------------------------------------|
| Idempotency and exactly-once         | [04-idempotency-and-exactly-once.md](04-idempotency-and-exactly-once.md) |
| Ledger and double-entry bookkeeping  | [05-ledger-and-double-entry.md](05-ledger-and-double-entry.md)         |
| Fraud detection and risk engine      | [06-fraud-detection.md](06-fraud-detection.md)                         |
| Payment routing and acquirer failover| [07-payment-routing.md](07-payment-routing.md)                         |
| Data storage and infrastructure      | [08-data-storage-and-infrastructure.md](08-data-storage-and-infrastructure.md) |
| Scaling and performance              | [10-scaling-and-performance.md](10-scaling-and-performance.md)         |
| Interview simulation (full dialogue) | [01-interview-simulation.md](01-interview-simulation.md)               |

---

## Verification Notes

The following claims in this document could not be verified via live web sources during writing and are marked accordingly:

- **[UNVERIFIED]**: Specific SLA numbers attributed to Stripe/Adyen/PayPal (99.99% uptime). These are industry-standard targets for Tier 1 PSPs but official published SLAs may differ.
- **[UNVERIFIED]**: The "$694K per minute of downtime" calculation assumes $1B/day processing volume, which is a rough estimate for a Stripe-scale PSP.
- **[UNVERIFIED]**: Cart abandonment rate increase per 100ms of latency. This is widely cited in e-commerce performance literature but exact figures vary by study.
- **[UNVERIFIED]**: Payment method success rate ranges. These are approximate industry benchmarks and vary significantly by geography and merchant vertical.
- **[INFERRED]**: Active-passive vs active-active preferences for PSPs. Not officially documented by specific companies.
- **[INFERRED]**: Redis failure behavior (fail closed vs fail open). Specific PSP implementation choices are not publicly documented.
- **[INFERRED]**: Visa/Mastercard operating active-active at the network level.

All architectural patterns (circuit breakers, DLQs, distributed tracing, graceful degradation) are well-established industry practices documented in sources such as Microsoft Azure Architecture Patterns, Martin Fowler's writings on circuit breakers, and the OpenTelemetry specification.
