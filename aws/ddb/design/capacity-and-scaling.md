# DynamoDB Capacity & Scaling — Deep Dive

## Table of Contents

1. [Overview](#1-overview)
2. [RCU and WCU Fundamentals](#2-rcu-and-wcu-fundamentals)
3. [Provisioned Capacity Mode](#3-provisioned-capacity-mode)
4. [On-Demand Capacity Mode](#4-on-demand-capacity-mode)
5. [Auto Scaling](#5-auto-scaling)
6. [DAX (DynamoDB Accelerator)](#6-dax-dynamodb-accelerator)
7. [Reserved Capacity](#7-reserved-capacity)
8. [Throttling](#8-throttling)
9. [Capacity Mode Selection](#9-capacity-mode-selection)
10. [Interview Angles](#10-interview-angles)

---

## 1. Overview

DynamoDB offers two capacity modes:

| Property | Provisioned | On-Demand |
|----------|------------|-----------|
| Capacity planning | You specify RCU/WCU | Automatic |
| Pricing | Per-hour for provisioned capacity | Per-request |
| Scaling | Auto-scaling (minutes to react) | Instant |
| Cost for steady workload | Lower | Higher (~6-7x per unit) |
| Cost for spiky workload | Higher (must provision for peak) | Lower (pay only for use) |
| Burst handling | 300-second burst capacity | 2x previous peak instantly |
| Default | No | Yes (recommended) |

---

## 2. RCU and WCU Fundamentals

### 2.1 Read Capacity Units (RCU)

| Read Type | Item Size | RCU Cost | Formula |
|-----------|-----------|----------|---------|
| Strongly consistent | ≤ 4 KB | 1 RCU | ceil(size_kb / 4) × 1 |
| Eventually consistent | ≤ 4 KB | 0.5 RCU | ceil(size_kb / 4) × 0.5 |
| Transactional | ≤ 4 KB | 2 RCU | ceil(size_kb / 4) × 2 |

**Examples:**

```
Item size: 1 KB
  SC read: ceil(1/4) × 1 = 1 RCU
  EC read: ceil(1/4) × 0.5 = 0.5 RCU

Item size: 6 KB
  SC read: ceil(6/4) × 1 = 2 RCU
  EC read: ceil(6/4) × 0.5 = 1 RCU

Item size: 20 KB
  SC read: ceil(20/4) × 1 = 5 RCU
  EC read: ceil(20/4) × 0.5 = 2.5 RCU
```

### 2.2 Write Capacity Units (WCU)

| Write Type | Item Size | WCU Cost | Formula |
|------------|-----------|----------|---------|
| Standard | ≤ 1 KB | 1 WCU | ceil(size_kb / 1) |
| Transactional | ≤ 1 KB | 2 WCU | ceil(size_kb / 1) × 2 |

**Examples:**

```
Item size: 0.5 KB
  Standard write: ceil(0.5/1) × 1 = 1 WCU
  Transactional: ceil(0.5/1) × 2 = 2 WCU

Item size: 3 KB
  Standard write: ceil(3/1) × 1 = 3 WCU
  Transactional: ceil(3/1) × 2 = 6 WCU
```

### 2.3 Capacity Calculation Examples

**Scenario 1: Simple read-heavy application**

```
Requirements:
  1,000 SC reads/sec, average item 2 KB
  100 writes/sec, average item 1 KB

Read capacity:
  1,000 × ceil(2/4) × 1 = 1,000 × 1 = 1,000 RCU

Write capacity:
  100 × ceil(1/1) × 1 = 100 WCU

Total provisioned: 1,000 RCU, 100 WCU
```

**Scenario 2: Mixed consistency reads**

```
Requirements:
  800 EC reads/sec, 200 SC reads/sec, average item 8 KB
  50 writes/sec, average item 2 KB

Read capacity:
  EC: 800 × ceil(8/4) × 0.5 = 800 × 2 × 0.5 = 800 RCU
  SC: 200 × ceil(8/4) × 1 = 200 × 2 = 400 RCU
  Total: 1,200 RCU

Write capacity:
  50 × ceil(2/1) = 50 × 2 = 100 WCU
```

**Scenario 3: With GSI write amplification**

```
Requirements:
  500 writes/sec, average item 1 KB
  2 GSIs (both keys present in every item)

Base table: 500 × 1 = 500 WCU
GSI-1: 500 × 1 = 500 WCU
GSI-2: 500 × 1 = 500 WCU
Total: 1,500 WCU (3x write amplification)
```

---

## 3. Provisioned Capacity Mode

### 3.1 How It Works

You specify the exact number of RCU and WCU for the table:

```json
{
  "TableName": "Orders",
  "BillingMode": "PROVISIONED",
  "ProvisionedThroughput": {
    "ReadCapacityUnits": 1000,
    "WriteCapacityUnits": 500
  }
}
```

You pay for provisioned capacity **per hour**, regardless of actual usage.

### 3.2 When to Use

- **Predictable workloads:** Steady traffic that you can forecast
- **Cost optimization:** Provisioned is 5-7x cheaper per unit than on-demand
- **Maximum control:** Need precise capacity management
- **Reserved capacity:** Can purchase reserved capacity for additional savings

### 3.3 Throughput Decrease Limits

| Time Window | Allowed Decreases |
|-------------|-------------------|
| First hour after last decrease | Up to 4 times |
| Each subsequent hour | 1 time |
| Maximum per day | Up to 27 times |

These limits prevent gaming the pricing model.

### 3.4 Per-Partition vs Table Throughput

```
Table provisioned: 10,000 RCU
Table has 10 partitions

Without adaptive capacity:
  Each partition: 10,000 / 10 = 1,000 RCU

With adaptive capacity (automatic):
  Hot partition may get more (up to 3,000 RCU)
  Cold partitions give up capacity
  Total still ≤ 10,000 RCU
```

---

## 4. On-Demand Capacity Mode

### 4.1 How It Works

No capacity planning — DynamoDB scales automatically:

```
┌───────────────────────────────────────────────────────┐
│              On-Demand Scaling Model                   │
├───────────────────────────────────────────────────────┤
│                                                       │
│  Traffic level ──▶ DynamoDB scales instantly           │
│                                                       │
│  Key rule: 2x previous peak                           │
│                                                       │
│  Previous peak: 10,000 RRU/sec                       │
│  ├─ Can instantly handle: 20,000 RRU/sec ✓           │
│  ├─ Beyond 20,000: may throttle briefly              │
│  └─ If sustained > 20,000 for 30 min:               │
│     new peak = 20,000 → can handle 40,000           │
│                                                       │
│  New tables start at:                                 │
│  ├─ 4,000 WRU/sec                                    │
│  └─ 12,000 RRU/sec                                   │
│                                                       │
└───────────────────────────────────────────────────────┘
```

### 4.2 Pricing

On-demand charges per request (RRU/WRU) instead of per hour:

```
Read Request Unit (RRU):
  1 RRU = 1 SC read of item ≤ 4 KB
  1 RRU = 2 EC reads of item ≤ 4 KB

Write Request Unit (WRU):
  1 WRU = 1 write of item ≤ 1 KB
```

### 4.3 The 2x Previous Peak Rule

```
Day 1: Peak traffic = 5,000 reads/sec
        → DynamoDB remembers: previous peak = 5,000
        → Can instantly handle up to 10,000 (2x)

Day 2: Traffic spikes to 10,000 reads/sec
        → Handled instantly ✓ (within 2x)
        → New previous peak = 10,000
        → Can now handle up to 20,000

Day 3: Traffic drops to 100 reads/sec
        → Previous peak still remembered as 10,000
        → Can still handle up to 20,000 instantly

Day 4: Traffic spikes to 25,000 reads/sec
        → 20,000 (2x peak) handled instantly ✓
        → 20,001-25,000: may need 30 min to ramp ⚠️
        → Throttling possible during ramp-up
```

### 4.4 Table-Level Throughput Limits

Default account quota: **40,000 RRU/WRU per table** (adjustable).

You can also set per-table maximum throughput to control costs:

```json
{
  "TableName": "Orders",
  "OnDemandThroughput": {
    "MaxReadRequestUnits": 20000,
    "MaxWriteRequestUnits": 10000
  }
}
```

Requests exceeding this cap are throttled.

### 4.5 Switching Between Modes

| Direction | Limit |
|-----------|-------|
| Provisioned → On-Demand | Up to 4 times per 24-hour rolling window |
| On-Demand → Provisioned | Anytime |

---

## 5. Auto Scaling

### 5.1 How It Works

Auto scaling uses **AWS Application Auto Scaling** with target tracking:

```
┌─────────┐      ┌───────────┐      ┌──────────────┐      ┌────────────┐
│DynamoDB  │─────▶│CloudWatch │─────▶│App Auto      │─────▶│ DynamoDB   │
│Consumed  │      │Alarm      │      │Scaling       │      │ UpdateTable│
│Capacity  │      │           │      │              │      │            │
│Metric    │      │Triggers   │      │Evaluate      │      │Adjust      │
│          │      │if target  │      │policy        │      │capacity    │
│          │      │exceeded   │      │              │      │            │
└─────────┘      └───────────┘      └──────────────┘      └────────────┘
```

### 5.2 Configuration

```
Target tracking policy:
  Table: Orders
  Metric: ConsumedReadCapacityUnits / ProvisionedReadCapacityUnits
  Target utilization: 70%
  Min capacity: 100 RCU
  Max capacity: 10,000 RCU
```

| Parameter | Range | Recommendation |
|-----------|-------|---------------|
| Target utilization | 20-90% | 70% (balance cost vs headroom) |
| Min capacity | 1+ | Enough for lowest expected traffic |
| Max capacity | Up to quota | High enough for peak traffic |

### 5.3 Scaling Behavior

**Scale-out (increase capacity):**
- Triggered when consumed capacity exceeds target for **2 consecutive minutes**
- Reacts within minutes
- Short spikes (< 1 minute) are handled by burst capacity, not auto scaling

**Scale-in (decrease capacity):**
- Triggered when **15 consecutive data points** are below target utilization
- More conservative than scale-out (avoids oscillation)
- Subject to throughput decrease limits (max 27/day)

### 5.4 Auto Scaling Delay

```
Total reaction time:

  1. Traffic spike starts:                t = 0 min
  2. CloudWatch detects (2 min):          t = 2 min
  3. Alarm triggers, policy evaluated:    t = 2-3 min
  4. UpdateTable API called:              t = 3 min
  5. New capacity takes effect:           t = 3-5 min
     ──────────────────────────────────────────────
     Total delay: ~3-5 minutes

During this delay: throttling if traffic exceeds provisioned + burst capacity
```

**This is why auto scaling alone is insufficient for sudden spikes.** For truly
unpredictable traffic, on-demand mode is better.

### 5.5 Auto Scaling for GSIs

- Each GSI has separate provisioned throughput
- Auto scaling must be configured independently per GSI
- **Critical:** If GSI auto scaling is not configured, GSI can throttle base table writes
- Console default: "Apply same settings to global secondary indexes"

---

## 6. DAX (DynamoDB Accelerator)

### 6.1 Architecture

```
┌────────────┐     ┌──────────────────────────┐     ┌──────────────┐
│Application │────▶│  DAX Cluster              │────▶│  DynamoDB    │
│            │     │                           │     │  Table       │
│            │     │  ┌────────┐ ┌────────┐   │     │              │
│            │     │  │Primary │ │Replica │   │     │              │
│            │     │  │Node    │ │Node    │   │     │              │
│            │     │  └────────┘ └────────┘   │     │              │
│            │     │  ┌────────┐              │     │              │
│            │     │  │Replica │              │     │              │
│            │     │  │Node    │              │     │              │
│            │     │  └────────┘              │     │              │
│            │     │                           │     │              │
│            │     │  Multi-AZ, in-memory     │     │              │
│            │     └──────────────────────────┘     └──────────────┘
```

### 6.2 Two Cache Types

| Cache | Stores | TTL | Used By |
|-------|--------|-----|---------|
| **Item cache** | Individual items (GetItem results) | 5 min default | GetItem, BatchGetItem |
| **Query cache** | Query/Scan result sets | 5 min default | Query, Scan |

### 6.3 Read-Through / Write-Through

**Read-through:**
```
GetItem → DAX checks item cache
  ├─ Cache HIT → return from cache (microseconds)
  └─ Cache MISS → read from DynamoDB → cache → return
```

**Write-through:**
```
PutItem → DAX writes to DynamoDB → updates item cache → returns
  → Query cache is NOT invalidated on writes
  → Query cache may return stale results after writes
```

### 6.4 Key Properties

| Property | Value |
|----------|-------|
| Latency (cache hit) | Microseconds (vs milliseconds for DDB) |
| Consistency | Eventually consistent **only** |
| SC reads | **Not cached** — pass through to DynamoDB |
| Multi-AZ | Yes |
| Cluster size | 1-11 nodes |
| Node types | Various (dax.r5.large, etc.) |
| VPC-only | Yes (EC2, not Lambda directly) |
| Encryption | At rest + in transit (TLS) |

### 6.5 When to Use DAX

```
Use DAX when:
  ✓ Read-heavy workload (>90% reads)
  ✓ Hot keys or non-uniform access (popular items cached)
  ✓ Need microsecond latency
  ✓ Want to reduce DynamoDB RCU costs
  ✓ Same items read repeatedly

Don't use DAX when:
  ✗ Need strongly consistent reads
  ✗ Write-heavy workload
  ✗ Low cache hit rate (< 90%) — adds cost without benefit
  ✗ Items change frequently and cache staleness is unacceptable
  ✗ Using Lambda (DAX requires VPC, adds cold start)
```

### 6.6 DAX Limitations

- **EC only:** Cannot read from DAX with strong consistency
- **VPC required:** DAX cluster must be in a VPC
- **Attribute name metadata:** DAX caches attribute names indefinitely — unbounded
  attribute names (timestamps, UUIDs as attribute names) cause memory exhaustion
- **No cross-region:** DAX cluster is regional
- **TransactGetItems:** Not cached (passes through)
- **Query cache staleness:** Write-through only updates item cache, not query cache

---

## 7. Reserved Capacity

### 7.1 How It Works

For provisioned mode, you can purchase reserved capacity for significant discounts:

```
On-Demand pricing:     $$$$$  (pay per request, most expensive per unit)
Provisioned pricing:   $$$    (pay per hour)
Reserved capacity:     $$     (commit for 1 or 3 years, up to 77% savings)
```

### 7.2 Reserved Capacity Terms

| Term | Approximate Discount |
|------|---------------------|
| 1 year, no upfront | ~25% over provisioned |
| 1 year, partial upfront | ~42% over provisioned |
| 1 year, all upfront | ~47% over provisioned |
| 3 year, all upfront | ~77% over provisioned |

[INFERRED — exact percentages vary by region and may change]

### 7.3 When to Use

- Stable, predictable baseline capacity
- Long-running workloads (12+ months)
- Cost optimization for large tables
- Combine with auto scaling: reserve baseline, scale for peaks

---

## 8. Throttling

### 8.1 What Causes Throttling

```
Throttling occurs when:
  1. Request rate exceeds provisioned capacity (provisioned mode)
  2. Request rate exceeds 2x previous peak within 30 min (on-demand mode)
  3. Per-partition limit exceeded (3,000 RCU / 1,000 WCU)
  4. GSI write capacity insufficient (cascades to base table)
  5. Account-level quota reached

Exception: ProvisionedThroughputExceededException
```

### 8.2 Throttling Diagnosis

```
CloudWatch Metrics:
  ├─ ThrottledRequests: Number of throttled requests
  ├─ ReadThrottleEvents: Read-specific throttling
  ├─ WriteThrottleEvents: Write-specific throttling
  └─ SystemErrors: DynamoDB internal errors (not throttling)

Contributor Insights:
  → Shows which partition keys are causing the most throttling
  → Identifies hot keys

Common patterns:
  1. Uniform throttling across all keys → capacity too low
  2. One key causing all throttling → hot key problem
  3. Write throttling correlating with GSI → GSI under-provisioned
```

### 8.3 SDK Retry Behavior

AWS SDKs handle throttling with exponential backoff:

```
Attempt 1: Request → ThrottledException
Wait: 50 ms (base delay)

Attempt 2: Retry → ThrottledException
Wait: 100 ms

Attempt 3: Retry → ThrottledException
Wait: 200 ms

... up to max retries (SDK default: 10)

If all retries exhausted → exception propagated to application
```

### 8.4 Throttling Resolution

| Cause | Resolution |
|-------|-----------|
| Overall capacity too low | Increase provisioned or switch to on-demand |
| Hot partition key | Redesign partition key, write sharding |
| GSI under-provisioned | Increase GSI WCU or use on-demand |
| Burst exceeds 300s reserve | More uniform traffic or switch to on-demand |
| Account quota | Request quota increase via AWS Support |

---

## 9. Capacity Mode Selection

### 9.1 Decision Matrix

```
┌─────────────────────────────────────┬────────────┬────────────┐
│ Scenario                            │ Provisioned│ On-Demand  │
├─────────────────────────────────────┼────────────┼────────────┤
│ Predictable, steady traffic         │    ✓✓✓     │     ✓      │
│ Spiky, unpredictable traffic        │      ✓     │    ✓✓✓     │
│ New application (unknown traffic)   │            │    ✓✓✓     │
│ Cost-optimized for large scale      │    ✓✓✓     │      ✓     │
│ Flash sale / event-driven spikes    │      ✓     │    ✓✓✓     │
│ Dev/test (low/no traffic)           │            │    ✓✓✓     │
│ Need reserved capacity savings      │    ✓✓✓     │            │
│ Zero operational overhead           │      ✓     │    ✓✓✓     │
└─────────────────────────────────────┴────────────┴────────────┘
```

### 9.2 Cost Comparison Example

```
Workload: 1,000 writes/sec sustained, 24/7

On-demand (us-east-1):
  1,000 WRU/sec × 86,400 sec/day × 30 days = 2,592,000,000 WRU/month
  At $1.25 per million WRU: $3,240/month

Provisioned:
  1,000 WCU × $0.00065/WCU/hour × 730 hours = $475/month

Savings with provisioned: ~85% cheaper for steady workloads!

But: If traffic is 0 for 20 hours/day and spikes to 5,000 for 4 hours:
  On-demand: only pay for actual requests → cheaper
  Provisioned: pay for 5,000 WCU × 24/7 → expensive
```

### 9.3 Hybrid Strategy

```
Strategy: Provisioned + auto scaling for baseline, with burst protection

  1. Provision for 70% of expected peak capacity
  2. Auto scaling with target utilization = 70%
  3. Min capacity = expected minimum traffic
  4. Max capacity = expected peak × 1.5 (headroom)
  5. Enable burst capacity (automatic, 300 seconds)

  For truly unpredictable spikes: consider on-demand mode
  For steady baseline + occasional spikes: provisioned + auto scaling
```

---

## 10. Interview Angles

### 10.1 "Explain the difference between provisioned and on-demand capacity"

"Provisioned mode requires you to specify RCU and WCU upfront — you pay per hour
regardless of usage, but the per-unit cost is much lower. On-demand mode scales
automatically and you pay per request — no capacity planning needed, but per-unit
cost is 5-7x higher. Provisioned with auto scaling is cost-effective for predictable
workloads. On-demand is ideal for unpredictable or spiky traffic where
over-provisioning would be wasteful."

### 10.2 "Walk me through an RCU/WCU calculation"

```
Scenario:
  Table with 500 SC reads/sec + 2,000 EC reads/sec
  Average item size: 6 KB
  200 writes/sec, average item 2 KB
  2 GSIs (all items indexed)

Reads:
  SC: 500 × ceil(6/4) × 1 = 500 × 2 = 1,000 RCU
  EC: 2,000 × ceil(6/4) × 0.5 = 2,000 × 2 × 0.5 = 2,000 RCU
  Total read: 3,000 RCU

Writes:
  Base table: 200 × ceil(2/1) = 200 × 2 = 400 WCU
  GSI-1: 200 × ceil(2/1) = 400 WCU
  GSI-2: 200 × ceil(2/1) = 400 WCU
  Total write: 1,200 WCU

Answer: Provision 3,000 RCU, 1,200 WCU
  (Add auto scaling with target 70% for headroom)
```

### 10.3 "When would you choose on-demand over provisioned?"

```
On-demand when:
  1. New application with unknown traffic patterns
  2. Traffic is highly variable (10x swings within minutes)
  3. Dev/test environment with sporadic usage
  4. Event-driven workloads (flash sales, launches)
  5. Operational simplicity is more important than cost

Provisioned when:
  1. Traffic is predictable and steady
  2. Cost optimization is critical (5-7x cheaper per unit)
  3. Can leverage reserved capacity for additional savings
  4. Auto scaling can handle expected variations
  5. Large-scale production with known capacity needs
```

### 10.4 "What is DAX and when would you use it?"

"DAX is an in-memory cache that sits in front of DynamoDB and delivers microsecond
read latency. It has two caches — an item cache for GetItem and a query cache for
Query/Scan results. It only supports eventually consistent reads; strongly consistent
reads pass through to DynamoDB. Use it for read-heavy workloads with hot keys
(popular items, leaderboards) where you need sub-millisecond latency and high
cache hit rates (>90%). Don't use it for write-heavy workloads, SC reads, or
workloads with low cache hit rates."

### 10.5 "A customer is getting throttled. Walk me through your investigation"

```
Step 1: Which metric is elevated?
  ThrottledRequests → overall throttling
  ReadThrottleEvents → read-specific
  WriteThrottleEvents → write-specific

Step 2: Is it capacity or hot key?
  → Contributor Insights: which partition keys are throttled?
  → If one key: hot key problem
  → If all keys: overall capacity too low

Step 3: Check GSI cascading
  → Is GSI WriteThrottleEvents elevated?
  → GSI throttling back-pressures base table
  → Under-provisioned GSI is a common hidden cause

Step 4: Resolution
  → Hot key: write sharding, DAX caching, key redesign
  → Overall: increase capacity, switch to on-demand, enable auto scaling
  → GSI: increase GSI WCU, or use on-demand (scales both)
  → Burst: if short spike, burst capacity should handle it
```

### 10.6 Design Decision: Why Two Modes Instead of Just On-Demand?

```
Why keep provisioned mode?

1. Cost: Provisioned is 5-7x cheaper per unit.
   For a table doing 10,000 writes/sec 24/7:
     On-demand: ~$3,240/month
     Provisioned: ~$475/month
   At scale, this difference is enormous.

2. Reserved capacity: Only available with provisioned mode.
   3-year reserved capacity adds another 77% discount.

3. Predictability: Fixed hourly cost is easier to budget.
   On-demand costs fluctuate with traffic.

4. Control: Some applications need guaranteed capacity,
   not best-effort scaling.

Why offer on-demand?
  → Not everyone can predict traffic
  → Zero-management is compelling
  → Better for spiky/event-driven workloads
  → Cost of over-provisioning may exceed on-demand pricing

Trade-off: Operational simplicity (on-demand) vs cost efficiency (provisioned)
```

---

## Appendix: Key Numbers

| Property | Value |
|----------|-------|
| RCU: 1 SC read | 4 KB |
| RCU: 1 EC read | 4 KB at 0.5 RCU |
| WCU: 1 write | 1 KB |
| Transaction cost multiplier | 2x (prepare + commit) |
| Per-partition read throughput | 3,000 RCU |
| Per-partition write throughput | 1,000 WCU |
| Per-partition data size | 10 GB |
| Burst capacity reserve | 300 seconds (5 minutes) |
| On-demand: initial throughput | 4,000 WRU / 12,000 RRU |
| On-demand: instant scaling | 2x previous peak |
| On-demand: ramp-up beyond 2x | ~30 minutes |
| Default table quota (on-demand) | 40,000 RRU / 40,000 WRU |
| Default table quota (provisioned) | 40,000 RCU / 40,000 WCU |
| Account quota (provisioned) | 80,000 RCU / 80,000 WCU |
| Auto scaling target range | 20-90% utilization |
| Auto scaling scale-out trigger | 2 consecutive minutes above target |
| Auto scaling scale-in trigger | 15 consecutive data points below target |
| Throughput decrease limit | 4 in first hour, then 1/hour, max 27/day |
| Mode switch (Provisioned → On-Demand) | Up to 4 times per 24-hour window |
| DAX latency | Microseconds |
| DAX consistency | Eventually consistent only |
| DAX cluster nodes | 1-11 |
| DAX item cache TTL | 5 minutes default |
