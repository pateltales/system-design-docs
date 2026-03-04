# 10. Scaling and Performance

> **Verification Note**: Web search was unavailable at document creation time.
> Numbers marked with `[UNVERIFIED]` are based on publicly known figures from
> training data (through early 2025) and should be cross-checked against the
> latest Stripe/Adyen/Visa press releases before using in an interview.

---

## 1. Scale Numbers (Stripe-Like Reference)

| Metric | Stripe-Scale Estimate | Source / Status |
|---|---|---|
| API requests/day | Hundreds of millions (reportedly 250M-500M+) | `[UNVERIFIED]` — Stripe has stated "hundreds of millions" in blog posts |
| Payment transactions/day | Millions (est. 10M-50M+) | `[UNVERIFIED]` — derived from ~$1T TPV/year (2023 annual letter) |
| Auth latency p50 | ~100-200ms | `[UNVERIFIED]` — includes network to acquirer |
| Auth latency p99 | <500ms target, <1s hard ceiling | `[UNVERIFIED]` — industry standard SLA |
| Total Payment Volume (annual) | ~$1 trillion+ (2023) | `[UNVERIFIED]` — Stripe annual letter 2023 reportedly stated this |
| Currencies supported | 135+ | `[UNVERIFIED]` — per Stripe docs |
| Acquiring countries | 46+ countries | `[UNVERIFIED]` — per Stripe docs |
| Merchants (businesses) | Millions (reportedly 3M+) | `[UNVERIFIED]` — various press reports |
| Uptime target | 99.999% (five nines) for payment auth | Industry standard for Tier-1 PSP |

### Visa Network Comparison (Peak Capability)

For context on what "internet-scale payments" means:

| System | Peak TPS | Notes |
|---|---|---|
| Visa VisaNet | ~65,000 TPS (capacity) | `[UNVERIFIED]` — reported peak capability |
| Visa typical | ~1,700 TPS average | `[UNVERIFIED]` — ~150M txns/day |
| Stripe | est. ~1,000-5,000+ TPS avg | `[UNVERIFIED]` — derived from volume estimates |
| Adyen | ~500-2,000+ TPS avg | `[UNVERIFIED]` — smaller than Stripe by volume |
| PayPal | ~1,000-3,000 TPS avg | `[UNVERIFIED]` — ~25B txns/year (2023) |

### What These Numbers Mean for System Design

```
At 10M payments/day (a reasonable "Stripe-like" design target):

  10,000,000 payments / 86,400 seconds = ~115 TPS average

  But traffic is NOT uniform:
  - Peak hours (6 PM local) = 3-5x average = ~350-580 TPS
  - Black Friday peak = 10-50x average = ~1,150-5,800 TPS
  - Flash sale micro-burst = 100x for seconds = ~11,500 TPS

  Each payment generates:
  - 1 payment record write
  - 2-4 ledger entries (double-entry)
  - 1 idempotency key write
  - 1-3 event log entries
  - 1 webhook dispatch

  So 115 TPS payments = 600-1,000+ writes/sec to various stores
  Peak: 5,800 TPS payments = 30,000-58,000 writes/sec
```

---

## 2. Read vs. Write Characteristics

### Payment Systems Are Write-Heavy on the Critical Path

This is a crucial distinction from other systems. Compare:

| System | Read:Write Ratio | Why |
|---|---|---|
| Netflix / YouTube | 100:1 to 1000:1 | Millions watch, few upload |
| Twitter / X | 100:1 | Millions read tweets, few tweet |
| Amazon Product Pages | 100:1 | Many browse, few buy |
| **Payment System** | **1:5 to 1:10** (critical path) | **Every txn = multiple writes** |
| Payment System (overall) | 3:1 to 5:1 | Dashboard reads, status queries add reads |

### Write Breakdown Per Payment Transaction

```
Single Payment Authorization (critical path writes):
  +---------------------------------------------------------+
  | Action                        | Store        | Writes   |
  +---------------------------------------------------------+
  | Create payment record         | Primary DB   | 1 INSERT |
  | Store idempotency key         | Redis + DB   | 1-2 SET  |
  | Write ledger entries          | Ledger DB    | 2-4 INS  |
  | Emit domain events            | Kafka        | 2-3 msgs |
  | Update merchant balance       | Primary DB   | 1 UPDATE |
  | Log audit trail               | Append-only  | 1-2 INS  |
  | Store PSP response            | Primary DB   | 1 UPDATE |
  +---------------------------------------------------------+
  Total: 9-14 write operations per single payment
```

### Read Patterns

```
Non-Critical-Path Reads:
  +----------------------------------------------+
  | Operation              | Frequency | Latency  |
  +----------------------------------------------+
  | Payment status query   | Per txn   | <50ms    |
  | Merchant dashboard     | Per minute| <200ms   |
  | Transaction search     | Ad-hoc    | <500ms   |
  | Settlement reports     | Daily     | Seconds  |
  | Reconciliation queries | Hourly    | Seconds  |
  +----------------------------------------------+

  Key insight: Reads can be served from replicas.
  Writes MUST go to the primary (consistency requirement).
```

### Why This Matters for Architecture

```
  Netflix-like (read-heavy):             Payment system (write-heavy critical path):

  +-----------+                          +-----------+
  |  CDN Edge | <-- cache everything     |  API GW   |
  +-----------+                          +-----------+
       |                                      |
  +-----------+                          +-----------+
  | Read      | <-- read replicas        | Write     | <-- primary DB, no caching
  | Replicas  |    scale horizontally    | Primary   |    consistency is king
  +-----------+                          +-----------+
       |                                      |
  +-----------+                          +-----------+
  | Object    | <-- S3/blob storage      | Write-    | <-- WAL, fsync, no shortcuts
  | Storage   |                          | Ahead Log |
  +-----------+                          +-----------+

  Scaling lever: add CDN nodes            Scaling lever: shard the database
```

---

## 3. Horizontal Scaling Strategy

### Overall Architecture

```
                         ┌─────────────────────┐
                         │   Global DNS / CDN   │
                         │  (Cloudflare/Route53)│
                         └──────────┬──────────┘
                                    │
                    ┌───────────────┼───────────────┐
                    │               │               │
               ┌────┴────┐    ┌────┴────┐    ┌────┴────┐
               │  LB #1  │    │  LB #2  │    │  LB #3  │
               │ (NLB)   │    │ (NLB)   │    │ (NLB)   │
               └────┬────┘    └────┬────┘    └────┬────┘
                    │               │               │
        ┌───────┬──┴──┬───────┬────┴───┬───────┬──┴──┐
        │       │     │       │        │       │     │
      ┌─┴─┐  ┌─┴─┐ ┌─┴─┐  ┌─┴─┐   ┌──┴─┐  ┌─┴─┐ ┌┴──┐
      │API│  │API│ │API│  │API│   │API │  │API│ │API│
      │ 1 │  │ 2 │ │ 3 │  │ 4 │   │ 5  │  │ 6 │ │ 7 │
      └─┬─┘  └─┬─┘ └─┬─┘  └─┬─┘   └──┬─┘  └─┬─┘ └┬──┘
        │       │     │       │        │       │    │
        └───────┴─────┴───┬───┴────────┴───────┴────┘
                          │
          STATELESS — no local state, any server handles any request
                          │
            ┌─────────────┼──────────────┐
            │             │              │
      ┌─────┴─────┐ ┌────┴────┐  ┌──────┴──────┐
      │  DB Shard  │ │  Kafka  │  │Redis Cluster│
      │  Cluster   │ │ Cluster │  │             │
      └───────────┘ └─────────┘  └─────────────┘
```

### Component-by-Component Scaling

#### A. Stateless API Servers

```
Scaling approach: Horizontal, behind Network Load Balancers

  Why stateless?
  - No session affinity needed
  - Any server can handle any merchant's request
  - Failed server → LB routes to another, zero impact
  - Auto-scaling group adds/removes based on CPU/request count

  Sizing estimate (10M payments/day):
  - ~115 TPS average, ~5,800 TPS peak
  - Each server handles ~500-1,000 TPS (mostly I/O-bound, waiting on DB/PSP)
  - Need: 6-12 servers normal, 30-60 servers peak
  - Auto-scaling: min=10, desired=20, max=80
```

#### B. Database Sharding (by Merchant ID)

```
Shard Key: merchant_id

  Why merchant_id?
  - All queries for a payment include merchant context
  - Merchant dashboard queries stay within one shard
  - Settlement is per-merchant → single-shard operation
  - Avoids cross-shard transactions for most operations

  Shard routing:

  merchant_id → hash(merchant_id) % num_shards → shard_N

  Example with 16 shards:

    merchant_abc → hash("merchant_abc") = 0xA3F2... → A3F2 % 16 = 2 → shard_02
    merchant_xyz → hash("merchant_xyz") = 0x71B8... → 71B8 % 16 = 8 → shard_08

  ┌──────────┐  ┌──────────┐  ┌──────────┐       ┌──────────┐
  │ Shard 00 │  │ Shard 01 │  │ Shard 02 │  ...  │ Shard 15 │
  │ Primary  │  │ Primary  │  │ Primary  │       │ Primary  │
  │ + 2 repl │  │ + 2 repl │  │ + 2 repl │       │ + 2 repl │
  │          │  │          │  │          │       │          │
  │ Merchants│  │ Merchants│  │ Merchants│       │ Merchants│
  │ whose    │  │ whose    │  │ whose    │       │ whose    │
  │ hash % 16│  │ hash % 16│  │ hash % 16│       │ hash % 16│
  │ = 0      │  │ = 1      │  │ = 2      │       │ = 15     │
  └──────────┘  └──────────┘  └──────────┘       └──────────┘
```

#### C. Kafka Partitioning (by Payment ID)

```
Topic: payment-events
Partition key: payment_id

  Why payment_id (not merchant_id)?
  - Ordering guarantee: all events for one payment land in same partition
  - Even distribution: payment IDs are UUIDs → uniform hash distribution
  - If keyed by merchant_id: Amazon partition would be 1000x hotter than
    a small merchant's partition

  Partition layout:

  ┌──────────────────────────────────────────────────────┐
  │               Topic: payment-events                   │
  │                                                       │
  │  Partition 0: [pay_001 events] [pay_017 events] ...  │
  │  Partition 1: [pay_002 events] [pay_023 events] ...  │
  │  Partition 2: [pay_005 events] [pay_031 events] ...  │
  │  ...                                                  │
  │  Partition 63: [pay_064 events] [pay_128 events] ... │
  │                                                       │
  │  64 partitions → 64 consumer instances max            │
  └──────────────────────────────────────────────────────┘

  Ordering guarantee:

  payment_id = "pay_abc123"

  Event sequence (guaranteed in-order within partition):
    1. payment.created     → Partition 7
    2. payment.authorized  → Partition 7  (same key → same partition)
    3. payment.captured    → Partition 7
    4. payment.settled     → Partition 7
```

#### D. Redis Cluster

```
Redis use cases in payments:

  1. Idempotency keys     → SET with TTL (24-72h), ~100 bytes each
  2. Rate limiting         → Token bucket counters per merchant
  3. Payment status cache  → Reduces DB reads for status polling
  4. Distributed locks     → For refund/capture mutual exclusion

  Redis Cluster topology:

  ┌─────────────┐  ┌─────────────┐  ┌─────────────┐
  │ Redis Node 0│  │ Redis Node 1│  │ Redis Node 2│
  │ Slots 0-5460│  │Slots 5461-  │  │Slots 10923- │
  │ + 1 replica │  │10922        │  │16383        │
  │             │  │ + 1 replica │  │ + 1 replica │
  └─────────────┘  └─────────────┘  └─────────────┘

  Sizing (10M payments/day):
  - Idempotency: 10M keys * 100 bytes = ~1 GB (active set, 72h TTL = ~3 GB)
  - Rate limiting: 100K merchant counters * 50 bytes = ~5 MB (trivial)
  - Status cache: 1M active payments * 500 bytes = ~500 MB
  - Total: ~4-5 GB active memory → single mid-size cluster handles it
```

---

## 4. The Hot Merchant Problem

### The Problem

```
Traffic distribution follows a power law:

  ┌──────────────────────────────────────────────────┐
  │                                                    │
  │  ████                                              │
  │  ████                                              │
  │  ████                                              │
  │  ████ ████                                         │
  │  ████ ████                                         │
  │  ████ ████ ████                                    │
  │  ████ ████ ████ ████                               │
  │  ████ ████ ████ ████ ████ ████ ████ ████ ████ ... │
  │  Top1 Top2 Top3 Top4  T5   T6   T7   T8   ...    │
  │                                                    │
  │  Top 10 merchants = 60-80% of all traffic          │
  │  Top 1 merchant (e.g., Amazon) = 15-25%            │
  └──────────────────────────────────────────────────┘

  If sharding by hash(merchant_id):

  Shard 07 (has Amazon):   ████████████████████░░░░  85% capacity
  Shard 12 (has Uber):     ████████████░░░░░░░░░░░  55% capacity
  Shard 03 (small merch):  ██░░░░░░░░░░░░░░░░░░░░░  10% capacity

  Problem: Shard 07 is overloaded while shard 03 is nearly idle.
```

### Solution 1: Dedicated Shards for Hot Merchants

```
  Routing logic:

  if merchant_id in HOT_MERCHANT_LIST:
      shard = dedicated_shard_map[merchant_id]
  else:
      shard = hash(merchant_id) % num_general_shards

  ┌──────────────────────────────────────────────────────────┐
  │                    Shard Router                           │
  │                                                          │
  │  "Is this a hot merchant?"                               │
  │       │                                                  │
  │       ├── YES ──► Dedicated Shard Map                    │
  │       │           ┌──────────────────────┐               │
  │       │           │ Amazon  → Shard D1   │               │
  │       │           │ Uber    → Shard D2   │               │
  │       │           │ Shopify → Shard D3   │               │
  │       │           └──────────────────────┘               │
  │       │                                                  │
  │       └── NO ───► hash(merchant_id) % 16                 │
  │                   → General Shards 00-15                  │
  └──────────────────────────────────────────────────────────┘

  Dedicated Shards (beefier hardware):

  ┌──────────────┐  ┌──────────────┐  ┌──────────────┐
  │  Shard D1    │  │  Shard D2    │  │  Shard D3    │
  │  (Amazon)    │  │  (Uber)      │  │  (Shopify)   │
  │  64 CPU      │  │  32 CPU      │  │  32 CPU      │
  │  256GB RAM   │  │  128GB RAM   │  │  128GB RAM   │
  │  NVMe SSD    │  │  NVMe SSD    │  │  NVMe SSD    │
  │  3 replicas  │  │  2 replicas  │  │  2 replicas  │
  └──────────────┘  └──────────────┘  └──────────────┘

  General Shards (standard hardware):

  ┌──────────┐  ┌──────────┐       ┌──────────┐
  │ Shard 00 │  │ Shard 01 │  ...  │ Shard 15 │
  │ 16 CPU   │  │ 16 CPU   │       │ 16 CPU   │
  │ 64GB RAM │  │ 64GB RAM │       │ 64GB RAM │
  └──────────┘  └──────────┘       └──────────┘
```

### Solution 2: Sub-Sharding Within a Merchant

```
  For extremely hot merchants, even a dedicated shard isn't enough.
  Sub-shard by payment_id hash within the merchant:

  Routing:

  if merchant_id == "amazon":
      sub_shard = hash(payment_id) % 4    # 4 sub-shards for Amazon
      shard = amazon_shards[sub_shard]     # D1a, D1b, D1c, D1d

  ┌──────────────────────────────────────────────────────┐
  │                 Amazon Traffic                        │
  │                                                      │
  │  payment_id → hash(payment_id) % 4                   │
  │       │                                              │
  │  ┌────┼────────┬──────────┬──────────┐               │
  │  │    │        │          │          │               │
  │  ▼    ▼        ▼          ▼          ▼               │
  │ ┌────┐ ┌────┐ ┌─────┐ ┌─────┐                       │
  │ │D1a │ │D1b │ │D1c  │ │D1d  │                       │
  │ │25% │ │25% │ │25%  │ │25%  │                       │
  │ └────┘ └────┘ └─────┘ └─────┘                       │
  └──────────────────────────────────────────────────────┘

  Trade-off: Merchant-level queries (dashboard, reporting) now
  require scatter-gather across sub-shards.

  Mitigation: Async materialized views aggregate sub-shard data
  into a read-optimized reporting store.
```

### Solution 3: Hybrid Approach (Recommended)

```
  ┌────────────────────────────────────────────────────────────┐
  │                    Shard Routing Table                      │
  │                                                            │
  │  Tier 1 (>1M txns/day): Sub-sharded dedicated shards      │
  │    Amazon  → 4 sub-shards (D1a, D1b, D1c, D1d)           │
  │    Uber    → 2 sub-shards (D2a, D2b)                      │
  │                                                            │
  │  Tier 2 (>100K txns/day): Dedicated single shard           │
  │    Shopify → Shard D3                                      │
  │    Lyft    → Shard D4                                      │
  │    DoorDash→ Shard D5                                      │
  │                                                            │
  │  Tier 3 (everyone else): Hash-based general shards         │
  │    hash(merchant_id) % 16 → Shard 00 - Shard 15           │
  │                                                            │
  │  Routing table is stored in config service (etcd/consul)   │
  │  Updated when merchant traffic patterns change             │
  └────────────────────────────────────────────────────────────┘
```

---

## 5. Batch vs. Real-Time Processing

### Processing Mode by Operation

```
  ┌────────────────────────┬────────────┬────────────────────────┐
  │ Operation              │ Mode       │ Latency Requirement    │
  ├────────────────────────┼────────────┼────────────────────────┤
  │ Authorization          │ Real-time  │ <500ms p99             │
  │ Capture                │ Real-time  │ <1s p99                │
  │ Refund initiation      │ Real-time  │ <1s p99                │
  │ 3DS authentication     │ Real-time  │ <2s (redirect flow)    │
  │ Webhook dispatch       │ Near-RT    │ <5s (with retry queue) │
  ├────────────────────────┼────────────┼────────────────────────┤
  │ Settlement             │ Batch      │ Daily (T+1 or T+2)    │
  │ Reconciliation         │ Batch      │ Hourly / Daily         │
  │ Payout to merchants    │ Batch      │ Daily / Weekly         │
  │ Dispute file ingest    │ Batch      │ Daily                  │
  │ Compliance reporting   │ Batch      │ Monthly / Quarterly    │
  ├────────────────────────┼────────────┼────────────────────────┤
  │ Dashboard metrics      │ Near-RT    │ Minutes (CDC pipeline) │
  │ Fraud scoring (async)  │ Near-RT    │ <100ms (inline) or     │
  │                        │            │ minutes (batch retro)  │
  │ Alerting               │ Near-RT    │ Minutes                │
  └────────────────────────┴────────────┴────────────────────────┘
```

### Real-Time Path (Authorization)

```
  Customer ──► API GW ──► Payment Service ──► PSP/Acquirer ──► Card Network
     │                         │                                    │
     │        <500ms           │          <300ms round-trip         │
     │◄────────────────────────│◄───────────────────────────────────│
     │                         │
     │                   Synchronous:
     │                   - Validate request
     │                   - Check idempotency
     │                   - Create payment record
     │                   - Call acquirer
     │                   - Update record with response
     │                   - Return to caller
     │
     │                   Budget breakdown:
     │                   - Request parsing + validation: 5ms
     │                   - DB write (create payment):    10ms
     │                   - Idempotency check (Redis):    2ms
     │                   - Fraud check (inline):         20-50ms
     │                   - Acquirer API call:             100-300ms  ← dominates
     │                   - DB write (update result):     10ms
     │                   - Response serialization:       2ms
     │                   ─────────────────────────────────
     │                   Total:                          150-380ms typical
```

### Batch Path (Settlement)

```
  ┌─────────────────────────────────────────────────────────┐
  │              Nightly Settlement Job                      │
  │              (runs at 02:00 UTC daily)                   │
  │                                                         │
  │  Step 1: Query all captured payments for the day        │
  │          SELECT * FROM payments                         │
  │          WHERE status = 'CAPTURED'                      │
  │          AND captured_at BETWEEN T-1 00:00 AND T 00:00  │
  │                                                         │
  │  Step 2: Group by acquirer + currency + merchant        │
  │                                                         │
  │  Step 3: Generate settlement files (ISO 8583 batch)     │
  │                                                         │
  │  Step 4: Submit to each acquirer via SFTP / API         │
  │                                                         │
  │  Step 5: Receive settlement confirmation (T+1 to T+3)  │
  │                                                         │
  │  Step 6: Update payment statuses to SETTLED             │
  │                                                         │
  │  Step 7: Calculate merchant payouts                     │
  │          (gross amount - fees - reserves)                │
  │                                                         │
  │  Step 8: Queue payout instructions to banking partner   │
  └─────────────────────────────────────────────────────────┘

  Duration: 30 minutes to 2 hours depending on volume
  Frequency: Once per day (some acquirers support multiple cutoffs)
```

### Near-Real-Time Path (CDC to Analytics)

```
  Primary DB ──► WAL ──► Debezium CDC ──► Kafka ──► Flink/Spark ──► Analytics DB
                                                                        │
                                                                   ┌────┴────┐
                                                                   │Dashboard│
                                                                   │(Grafana)│
                                                                   └─────────┘

  Lag: 30 seconds to 5 minutes typically

  Use cases served:
  - "How much volume did we process in the last hour?"
  - "What's the auth success rate for merchant X right now?"
  - "Are there anomalous decline rates on acquirer Y?"
```

---

## 6. Peak Traffic Handling

### The Peak Traffic Reality

```
  Normal day:          ████████░░░░░░░░░░░░  ~100%  baseline
  Holiday season:      ████████████████░░░░  ~200%  sustained
  Black Friday:        █████████████████████  ~500-1000%  peak hours
  Flash sale (1 min):  ████████████████████████████████  ~2000-5000%  burst

  [UNVERIFIED] Reference points:
  - Black Friday 2023 Shopify: reportedly ~$4.1B in sales over the weekend
  - Alibaba Singles Day 2023: reportedly 583,000 orders/sec peak
  - Amazon Prime Day: estimated 10-50x normal traffic

  Key insight: Auto-scaling is NECESSARY but NOT SUFFICIENT.
  Auto-scaling takes 2-5 minutes. A flash sale hits in seconds.
```

### Why Auto-Scaling Alone Fails for Payments

```
  ┌────────────────────────────────────────────────────────────────┐
  │ Problem: Cascading bottlenecks during scale-up                 │
  │                                                                │
  │ Auto-scaling adds API servers in 2 min ──► OK                  │
  │                                                                │
  │ BUT:                                                           │
  │ 1. DB connection pool is exhausted                             │
  │    - PostgreSQL default: 100 connections                       │
  │    - 60 new API servers × 10 connections each = 600 new conns  │
  │    - DB rejects connections → cascading failures               │
  │                                                                │
  │ 2. Acquirer rate limits kick in                                │
  │    - Acquirers impose TPS limits per merchant per BIN          │
  │    - Suddenly sending 10x traffic → acquirer returns 429s      │
  │    - These are NOT retryable immediately                       │
  │                                                                │
  │ 3. Kafka consumer lag spikes                                   │
  │    - More messages produced than consumers can process          │
  │    - Adding consumers takes time (rebalancing)                 │
  │    - Webhook delivery falls behind → merchants complain        │
  │                                                                │
  │ 4. Redis connection storms                                     │
  │    - New servers all connect to Redis simultaneously            │
  │    - Thundering herd on cache misses                            │
  └────────────────────────────────────────────────────────────────┘
```

### The Solution: Pre-Provisioning + Load Shedding

```
  Strategy: Belt AND suspenders

  ┌──────────────────────────────────────────────────────────────┐
  │                                                              │
  │  BEFORE the event (Pre-Provisioning):                        │
  │                                                              │
  │  1. Scale API servers to 3-5x normal capacity 1 hour before  │
  │  2. Warm up DB connection pools                              │
  │  3. Pre-negotiate higher TPS limits with acquirers           │
  │  4. Add Kafka partitions + consumers                         │
  │  5. Scale Redis cluster (add nodes, rebalance slots)         │
  │  6. Pre-warm caches (merchant configs, routing rules)        │
  │  7. Disable non-critical background jobs                     │
  │  8. Put on-call team on active standby                       │
  │                                                              │
  │  DURING the event (Load Shedding):                           │
  │                                                              │
  │  1. Priority queuing: Tier 1 merchants get guaranteed capacity│
  │  2. Shed non-critical traffic first:                         │
  │     - Analytics webhooks: delay OK                           │
  │     - Dashboard API: degrade to cached data                  │
  │     - Reporting queries: reject with "try later"             │
  │  3. Circuit breakers on degraded acquirers                   │
  │  4. Failover routing: if acquirer A is slow, try acquirer B  │
  │                                                              │
  │  AFTER the event:                                            │
  │                                                              │
  │  1. Process backlogged webhooks                              │
  │  2. Run reconciliation to catch any discrepancies            │
  │  3. Scale down gradually (not all at once)                   │
  │  4. Post-incident review                                     │
  │                                                              │
  └──────────────────────────────────────────────────────────────┘
```

### Load Shedding Priority Tiers

```
  ┌─────────────────────────────────────────────────────┐
  │  Priority 1 (NEVER shed):                           │
  │    - Payment authorization                          │
  │    - Payment capture                                │
  │    - Refund processing                              │
  │                                                     │
  │  Priority 2 (Shed last, degrade first):             │
  │    - Webhook delivery (queue, deliver later)         │
  │    - Payment status queries (serve from cache)       │
  │                                                     │
  │  Priority 3 (Shed early):                           │
  │    - Dashboard/reporting queries                    │
  │    - Merchant onboarding                            │
  │    - Non-critical API endpoints                     │
  │                                                     │
  │  Priority 4 (Shed first):                           │
  │    - Analytics ingestion                            │
  │    - Batch reconciliation triggers                  │
  │    - Internal tooling                               │
  └─────────────────────────────────────────────────────┘
```

---

## 7. Rate Limiting

### Rate Limiting Layers

```
  Request flow through rate limiting:

  Client ──► CDN/WAF Rate Limit ──► API GW Rate Limit ──► Service Rate Limit
              │                       │                      │
              │ Per-IP:               │ Per-Merchant:        │ Per-Acquirer:
              │ 1000 req/min          │ Based on plan        │ Per-BIN limits
              │ (DDoS protection)     │ (see table below)    │ (external constraint)
              │                       │                      │
              │ Algo: Fixed window    │ Algo: Token bucket   │ Algo: Sliding window
              │ Store: CDN edge       │ Store: Redis         │ Store: Redis
```

### Per-Merchant Rate Limits

```
  ┌─────────────┬──────────────┬───────────────┬────────────────────┐
  │ Plan Tier   │ TPS Limit    │ Burst Allow   │ Daily Volume Limit │
  ├─────────────┼──────────────┼───────────────┼────────────────────┤
  │ Starter     │ 10 TPS       │ 20 TPS (10s)  │ 10,000 txns        │
  │ Growth      │ 100 TPS      │ 200 TPS (10s) │ 500,000 txns       │
  │ Enterprise  │ 1,000 TPS    │ 2,000 TPS(10s)│ Unlimited          │
  │ Custom      │ Negotiated   │ Negotiated    │ Negotiated         │
  └─────────────┴──────────────┴───────────────┴────────────────────┘
```

### Token Bucket Algorithm (Per-Merchant)

```
  Token Bucket for merchant "merchant_abc" (Growth plan):

  Bucket capacity: 200 tokens (burst limit)
  Refill rate: 100 tokens/second (sustained TPS limit)

  ┌──────────────────────────────────────────┐
  │ Bucket: [████████████████████░░░░░░░░░░] │
  │          160/200 tokens remaining         │
  │                                          │
  │ Each request consumes 1 token            │
  │ Tokens refill at 100/sec                 │
  │ If bucket empty → 429 Too Many Requests  │
  └──────────────────────────────────────────┘

  Redis implementation (pseudo-code):

  FUNCTION check_rate_limit(merchant_id):
      key = "ratelimit:{merchant_id}"
      now = current_timestamp_ms()

      # Atomic Lua script in Redis:
      tokens, last_refill = GET key

      elapsed = now - last_refill
      new_tokens = min(capacity, tokens + elapsed * refill_rate / 1000)

      IF new_tokens >= 1:
          SET key = (new_tokens - 1, now)
          RETURN ALLOWED
      ELSE:
          RETURN REJECTED, retry_after = (1 - new_tokens) / refill_rate
```

### Adaptive Rate Limiting

```
  Normal operation:           Degraded operation:

  ┌──────────────────┐       ┌──────────────────┐
  │ Limits: Standard │       │ Limits: Tightened │
  │                  │       │                  │
  │ Enterprise: 1000 │  ──►  │ Enterprise: 500  │
  │ Growth:     100  │       │ Growth:     50   │
  │ Starter:    10   │       │ Starter:    5    │
  └──────────────────┘       └──────────────────┘

  Triggers for tightening:
  - DB CPU > 80%
  - Acquirer error rate > 5%
  - Kafka consumer lag > 10,000 messages
  - API p99 latency > 2 seconds

  Implementation:
  - Health monitor publishes "system_load" metric
  - Rate limiter reads a "degradation_factor" from config (0.0 to 1.0)
  - Effective limit = base_limit * (1.0 - degradation_factor)
  - Factor 0.0 = normal, 0.5 = half capacity, 0.9 = emergency (10%)
```

---

## 8. Database Connection Management

### The Connection Bottleneck

```
  Why connections are the FIRST bottleneck:

  PostgreSQL default max_connections = 100
  Each connection ≈ 5-10 MB RAM on the DB server

  ┌───────────────────────────────────────────────────────┐
  │ 20 API servers × 10 connections each = 200 connections│
  │                                                       │
  │ PostgreSQL with 100 max_connections:                   │
  │   → 100 connections succeed                           │
  │   → 100 connections REJECTED                          │
  │   → 10 API servers are effectively dead               │
  │                                                       │
  │ Even with max_connections = 500:                       │
  │   → 500 × 10 MB = 5 GB RAM just for connections       │
  │   → Less RAM for actual query caching                  │
  │   → Performance degrades for everyone                  │
  └───────────────────────────────────────────────────────┘
```

### Solution: Connection Pooling with PgBouncer

```
  Without PgBouncer:

  ┌─────┐ ┌─────┐ ┌─────┐       ┌─────┐
  │API 1│ │API 2│ │API 3│  ...  │API 20│
  │10con│ │10con│ │10con│       │10con │
  └──┬──┘ └──┬──┘ └──┬──┘       └──┬──┘
     │       │       │             │
     └───────┴───────┴─────────────┘
                   │
              200 connections
                   │
              ┌────┴────┐
              │PostgreSQL│  ← overwhelmed
              │ max=100  │
              └─────────┘


  With PgBouncer (transaction-mode pooling):

  ┌─────┐ ┌─────┐ ┌─────┐       ┌─────┐
  │API 1│ │API 2│ │API 3│  ...  │API 20│
  │10con│ │10con│ │10con│       │10con │
  └──┬──┘ └──┬──┘ └──┬──┘       └──┬──┘
     │       │       │             │
     └───────┴───────┴─────────────┘
                   │
              200 connections (from app servers)
                   │
              ┌────┴─────┐
              │ PgBouncer │  ← multiplexes connections
              │ pool=200  │
              │ to DB=30  │  ← only 30 real DB connections
              └────┬─────┘
                   │
              30 connections
                   │
              ┌────┴────┐
              │PostgreSQL│  ← comfortable
              │ max=100  │
              └─────────┘

  How transaction-mode pooling works:

  1. App opens "connection" to PgBouncer (lightweight)
  2. App sends BEGIN → PgBouncer assigns a REAL DB connection
  3. App does queries within transaction
  4. App sends COMMIT → PgBouncer RELEASES the real connection
  5. Real connection is now available for another app "connection"

  Key insight: Most app connections are IDLE most of the time.
  A real DB connection is only needed during active transactions.
  With 200 app connections, typically only 20-30 are in a transaction
  at any given instant.
```

### Read Replicas

```
  ┌──────────────┐
  │   Primary    │◄── All writes go here
  │  (Read/Write)│
  └──────┬───────┘
         │ Streaming replication (async, <100ms lag)
    ┌────┼────┐
    │    │    │
  ┌─┴─┐┌─┴─┐┌─┴─┐
  │Rep││Rep││Rep│◄── Reads for non-critical queries
  │ 1 ││ 2 ││ 3 │
  └───┘└───┘└───┘

  What goes to replicas (OK with slight lag):
  - Dashboard queries
  - Transaction history searches
  - Reporting / analytics
  - Merchant configuration reads

  What MUST go to primary (consistency required):
  - Idempotency key checks (before write)
  - Payment status reads during state transitions
  - Balance checks before capture
  - Any read-then-write operation
```

### Connection Architecture Summary

```
  ┌──────────────────────────────────────────────────────────────┐
  │                                                              │
  │  API Servers (stateless, 20-60 instances)                    │
  │  Each has TWO connection pools:                              │
  │    - Write pool: points to PgBouncer (Primary)               │
  │    - Read pool:  points to PgBouncer (Replica LB)            │
  │                                                              │
  │  ┌────────────────┐         ┌────────────────┐               │
  │  │ PgBouncer       │         │ PgBouncer       │               │
  │  │ (Write)         │         │ (Read)          │               │
  │  │ App conns: 200  │         │ App conns: 400  │               │
  │  │ DB conns:  30   │         │ DB conns:  50   │               │
  │  └────────┬───────┘         └───────┬────────┘               │
  │           │                    ┌────┼────┐                    │
  │      ┌────┴────┐            ┌──┴─┐┌─┴──┐┌┴───┐               │
  │      │Primary  │            │Rep1││Rep2││Rep3│               │
  │      │(Write)  │            └────┘└────┘└────┘               │
  │      └─────────┘                                             │
  │                                                              │
  └──────────────────────────────────────────────────────────────┘
```

---

## 9. Back-of-Envelope Calculations

### Target: 10M Payments/Day

```
  ┌──────────────────────────────────────────────────────────────┐
  │  TRANSACTIONS PER SECOND (TPS)                               │
  │                                                              │
  │  10,000,000 payments / 86,400 seconds = ~116 TPS average    │
  │                                                              │
  │  But payments are not uniformly distributed:                  │
  │  - 80% of traffic in 12 "active" hours                       │
  │  - Effective TPS = 10M × 0.8 / (12 × 3,600) = ~185 TPS     │
  │  - Peak hour (2x of active avg) = ~370 TPS                   │
  │  - Black Friday peak (10x average) = ~1,160 TPS              │
  │  - Flash sale burst (50x for 1 min) = ~5,800 TPS             │
  │                                                              │
  │  Design target: Handle 5,000-6,000 TPS sustained burst       │
  └──────────────────────────────────────────────────────────────┘

  ┌──────────────────────────────────────────────────────────────┐
  │  STORAGE PER DAY                                             │
  │                                                              │
  │  Per payment record:                                         │
  │  - payments table row:        ~500 bytes                     │
  │  - ledger entries (2-4):      ~200 bytes × 3 = 600 bytes    │
  │  - event log entries (2-3):   ~300 bytes × 2.5 = 750 bytes  │
  │  - idempotency key:           ~100 bytes                     │
  │  - indexes overhead:          ~400 bytes                     │
  │  ────────────────────────────────────                        │
  │  Total per payment:           ~2,350 bytes ≈ 2.3 KB          │
  │                                                              │
  │  Daily storage:                                              │
  │  10M × 2.3 KB = 23 GB/day raw data                           │
  │                                                              │
  │  With replication factor 3:                                   │
  │  23 GB × 3 = 69 GB/day across replicas                       │
  │                                                              │
  │  Monthly: 23 GB × 30 = ~690 GB/month raw                     │
  │  Yearly:  23 GB × 365 = ~8.4 TB/year raw                     │
  │  With replicas: ~25 TB/year                                   │
  │                                                              │
  │  After 5 years with growth: ~200-300 TB total                 │
  │  (assuming 30% YoY growth in volume)                          │
  └──────────────────────────────────────────────────────────────┘

  ┌──────────────────────────────────────────────────────────────┐
  │  KAFKA THROUGHPUT                                            │
  │                                                              │
  │  Events per payment: ~3 (created, authorized, captured/failed)│
  │  Event size: ~500 bytes average (JSON with metadata)          │
  │                                                              │
  │  Average: 116 TPS × 3 events = ~350 messages/sec             │
  │  Peak:    5,800 TPS × 3 events = ~17,400 messages/sec        │
  │                                                              │
  │  Throughput:                                                  │
  │  Average: 350 × 500 bytes = 175 KB/sec = ~0.17 MB/sec        │
  │  Peak:    17,400 × 500 bytes = 8.7 MB/sec                    │
  │                                                              │
  │  With replication factor 3:                                   │
  │  Peak intra-cluster: 8.7 × 3 = ~26 MB/sec                    │
  │                                                              │
  │  This is well within Kafka's capability (GB/sec per broker).  │
  │  64 partitions with 3 brokers = comfortable headroom.         │
  │                                                              │
  │  Daily Kafka storage (7-day retention):                       │
  │  10M × 3 events × 500 bytes × 7 days = ~105 GB               │
  │  With replication: ~315 GB across the cluster                 │
  └──────────────────────────────────────────────────────────────┘

  ┌──────────────────────────────────────────────────────────────┐
  │  REDIS OPERATIONS/SECOND                                     │
  │                                                              │
  │  Per payment, Redis operations:                               │
  │  - Idempotency check:   1 GET                                │
  │  - Idempotency set:     1 SET (with TTL)                     │
  │  - Rate limit check:    1 Lua script (≈ 2 commands)           │
  │  - Status cache update: 1 SET                                │
  │  - Optional lock ops:   0-2 SET/DEL                           │
  │  ─────────────────────────────────                            │
  │  Total: ~5-7 Redis ops per payment                            │
  │                                                              │
  │  Average: 116 TPS × 6 = ~700 ops/sec                         │
  │  Peak:    5,800 TPS × 6 = ~35,000 ops/sec                    │
  │                                                              │
  │  Redis single node: handles ~100,000+ ops/sec easily          │
  │  Redis cluster (3 nodes): ~300,000+ ops/sec capacity          │
  │                                                              │
  │  Peak 35K ops/sec uses ~12% of a 3-node cluster.             │
  │  Plenty of headroom.                                          │
  │                                                              │
  │  Memory:                                                      │
  │  - Active idempotency keys (72h window):                      │
  │    10M/day × 3 days × 100 bytes = 3 GB                       │
  │  - Rate limit counters: ~5 MB (negligible)                    │
  │  - Status cache (active 1h): ~50 MB                           │
  │  - Total: ~3-4 GB → single node sufficient for memory         │
  └──────────────────────────────────────────────────────────────┘

  ┌──────────────────────────────────────────────────────────────┐
  │  API SERVER SIZING                                           │
  │                                                              │
  │  Assumptions:                                                 │
  │  - Each API server: 4 vCPU, 8 GB RAM                         │
  │  - Thread pool: 200 threads (I/O-bound, waiting on DB/PSP)   │
  │  - Each request takes ~300ms avg (mostly acquirer wait)       │
  │  - Effective TPS per server: 200 threads / 0.3s = ~660 TPS   │
  │  - With safety margin (70% target utilization): ~460 TPS      │
  │                                                              │
  │  Normal (185 TPS effective):                                  │
  │    185 / 460 = 1 server minimum (run 3 for redundancy)        │
  │                                                              │
  │  Peak (5,800 TPS):                                            │
  │    5,800 / 460 = ~13 servers minimum (run 18-20 for safety)   │
  │                                                              │
  │  Pre-provision for Black Friday:                              │
  │    Target 10,000 TPS headroom → 22-25 servers                 │
  └──────────────────────────────────────────────────────────────┘

  ┌──────────────────────────────────────────────────────────────┐
  │  DATABASE SHARD SIZING                                       │
  │                                                              │
  │  Single PostgreSQL node (beefy):                              │
  │  - 64 vCPU, 256 GB RAM, NVMe SSD                             │
  │  - Can handle ~5,000-10,000 write TPS (simple inserts)        │
  │  - With indexes and constraints: ~2,000-5,000 write TPS       │
  │                                                              │
  │  At 116 avg write TPS (× ~10 ops = 1,160 write ops/sec):     │
  │    1 shard handles normal load                                │
  │                                                              │
  │  At peak 5,800 TPS (× ~10 ops = 58,000 write ops/sec):       │
  │    58,000 / 3,000 (safe per shard) = ~20 shards needed        │
  │                                                              │
  │  Recommendation: Start with 16 shards                         │
  │    - Normal: each shard sees ~73 write ops/sec (very light)   │
  │    - Peak: each shard sees ~3,625 write ops/sec (manageable)  │
  │    - Leaves room for hot merchants + growth                   │
  └──────────────────────────────────────────────────────────────┘
```

### Quick Reference Card

```
  ┌────────────────────────────────────────────────────────────┐
  │         10M Payments/Day — Quick Numbers                    │
  ├────────────────────────────────────────────────────────────┤
  │ Average TPS:           ~116 (effective ~185 during peak 12h)│
  │ Peak TPS:              ~5,800 (Black Friday)                │
  │ Design ceiling TPS:    ~10,000                              │
  │                                                            │
  │ DB write ops/sec avg:  ~1,160                               │
  │ DB write ops/sec peak: ~58,000                              │
  │ DB shards needed:      16 (general) + 3-5 (dedicated hot)  │
  │                                                            │
  │ Kafka msgs/sec avg:    ~350                                 │
  │ Kafka msgs/sec peak:   ~17,400                              │
  │ Kafka partitions:      64                                   │
  │                                                            │
  │ Redis ops/sec avg:     ~700                                 │
  │ Redis ops/sec peak:    ~35,000                              │
  │ Redis memory:          ~3-4 GB                              │
  │                                                            │
  │ Storage/day:           ~23 GB (raw), ~69 GB (replicated)    │
  │ Storage/year:          ~8.4 TB (raw), ~25 TB (replicated)   │
  │                                                            │
  │ API servers normal:    3-5                                  │
  │ API servers peak:      18-25                                │
  └────────────────────────────────────────────────────────────┘
```

---

## 10. Contrast with Netflix

### Fundamentally Different Scaling Challenges

```
  ┌──────────────────────────┬──────────────────────────────────┐
  │        Netflix           │       Payment System             │
  ├──────────────────────────┼──────────────────────────────────┤
  │ Read-heavy (1000:1)      │ Write-heavy critical path (1:10)│
  │                          │                                  │
  │ Eventual consistency OK  │ Strong consistency REQUIRED      │
  │ (video metadata cache)   │ (money cannot be approximate)    │
  │                          │                                  │
  │ Stateless content        │ Stateful transactions            │
  │ (same video for all)     │ (each payment is unique)         │
  │                          │                                  │
  │ Idempotent reads         │ Non-idempotent writes            │
  │ (replay = no harm)       │ (duplicate charge = real harm)   │
  │                          │                                  │
  │ Failure = bad UX         │ Failure = lost money/compliance  │
  │ (rebuffer, lower quality)│ (legal, financial consequences)  │
  └──────────────────────────┴──────────────────────────────────┘
```

### Scaling Levers Compared

```
  ┌──────────────────────────┬──────────────────────────────────┐
  │    Netflix Scales By:    │  Payment System Scales By:       │
  ├──────────────────────────┼──────────────────────────────────┤
  │                          │                                  │
  │ + CDN edge nodes         │ + DB shards & replicas           │
  │   (push content closer)  │   (partition the write load)     │
  │                          │                                  │
  │ + Object storage (S3)    │ + Acquirer connections           │
  │   (unlimited capacity)   │   (negotiated, rate-limited)     │
  │                          │                                  │
  │ + Read caches (EVCache)  │ + Compute (API servers)          │
  │   (cache = primary path) │   (stateless, easy to add)       │
  │                          │                                  │
  │ + Encoding farms         │ + Kafka partitions & consumers   │
  │   (pre-process content)  │   (async event processing)       │
  │                          │                                  │
  │ + Bandwidth/peering      │ + Connection pooling             │
  │   (ISP interconnects)    │   (PgBouncer, Redis pools)       │
  │                          │                                  │
  │ + Lower quality codec    │ + Smart routing (failover)       │
  │   (graceful degradation) │   (try another acquirer)         │
  └──────────────────────────┴──────────────────────────────────┘
```

### Primary Bottleneck Comparison

```
  Netflix:

  User ──► CDN Edge ──► ISP ──► User's device
                  │
                  └── Bottleneck: BANDWIDTH
                      "Can we push enough bits/sec?"

  Solution: More CDN PoPs, better peering, adaptive bitrate

  ──────────────────────────────────────────────────────────────

  Payment System:

  User ──► API ──► DB ──► Acquirer ──► Card Network
                    │         │
                    │         └── Bottleneck: EXTERNAL TPS LIMITS
                    │             "Acquirer only allows N TPS"
                    │
                    └── Bottleneck: TRANSACTIONAL THROUGHPUT
                        "Can we write fast enough with consistency?"

  Solution: Shard DB, pool connections, multi-acquirer routing,
            pre-negotiate limits
```

### Failure Mode Comparison

```
  Netflix failure:                    Payment failure:

  "Video is buffering"               "Payment declined unexpectedly"
  → User annoyed                     → Customer can't complete purchase
  → User retries / lowers quality    → Merchant loses the sale
  → No lasting harm                  → Money may be in limbo
                                     → Reconciliation needed

  Netflix data loss:                  Payment data loss:

  "Recommendation was wrong"         "Transaction record missing"
  → User gets different suggestion   → Regulatory violation
  → Nobody notices                   → Audit failure
  → Zero financial impact            → Potential financial loss
                                     → Legal liability

  Recovery:                           Recovery:

  Netflix: Serve from another CDN    Payment: Manual reconciliation
           node, user doesn't even            with acquirer, may take
           notice the failover                days, involves money
```

### Cost Structure Comparison

```
  Netflix (simplified):              Payment System (simplified):

  ┌─────────────┬──────────┐        ┌─────────────┬──────────┐
  │ Component   │ % Cost   │        │ Component   │ % Cost   │
  ├─────────────┼──────────┤        ├─────────────┼──────────┤
  │ CDN/BW      │ 40-50%   │        │ Fraud/Risk  │ 25-30%   │
  │ Content     │ 30-40%   │        │ Infra (DB)  │ 20-25%   │
  │ Compute     │ 10-15%   │        │ Compliance  │ 15-20%   │
  │ Storage     │ 5-10%    │        │ Network fees│ 15-20%   │
  │             │          │        │ Compute     │ 10-15%   │
  └─────────────┴──────────┘        └─────────────┴──────────┘

  Netflix spends on BANDWIDTH        Payments spend on CONSISTENCY
  and CONTENT.                       and COMPLIANCE.
```

---

## Summary: Key Interview Talking Points

```
  1. Payment systems are WRITE-HEAVY on the critical path.
     (Unlike most web systems which are read-heavy.)

  2. Shard by merchant_id for locality, but watch for hot merchants.
     (Dedicated shards + sub-sharding for top-tier merchants.)

  3. Auto-scaling is necessary but not sufficient.
     (Pre-provision for known peaks; load-shed gracefully for unknowns.)

  4. The bottleneck is usually DB connections or acquirer TPS limits,
     NOT compute. (PgBouncer + multi-acquirer routing are critical.)

  5. Real-time for auth/capture, batch for settlement/recon.
     (Don't try to make everything real-time.)

  6. Back-of-envelope: 10M payments/day ≈ 116 TPS avg, ~5,800 peak,
     ~23 GB/day storage, ~35K Redis ops/sec peak.

  7. Contrast with Netflix: they scale bandwidth,
     we scale transactional throughput + consistency.
```

---

*Next: [11-monitoring-and-observability.md](11-monitoring-and-observability.md) — Monitoring, Alerting, and Observability*
