# Amazon S3 --- Storage Classes & Lifecycle Deep Dive

> Companion deep dive to the [interview simulation](interview-simulation.md). This document explores S3's tiered storage architecture, lifecycle policies, and cost optimization strategies.

---

## Table of Contents

1. [Why Tiered Storage Exists](#1-why-tiered-storage-exists)
2. [Complete Storage Class Reference](#2-complete-storage-class-reference)
3. [Storage Class Comparison Table](#3-storage-class-comparison-table)
4. [How Storage Classes Work Internally](#4-how-storage-classes-work-internally)
5. [S3 Intelligent-Tiering --- Deep Dive](#5-s3-intelligent-tiering--deep-dive)
6. [Lifecycle Policies --- Configuration & Mechanics](#6-lifecycle-policies--configuration--mechanics)
7. [Lifecycle Evaluation Engine](#7-lifecycle-evaluation-engine)
8. [Transition Mechanics --- What Happens Physically](#8-transition-mechanics--what-happens-physically)
9. [Cost Optimization Strategies](#9-cost-optimization-strategies)
10. [Storage Class Constraints & Gotchas](#10-storage-class-constraints--gotchas)
11. [S3 Storage Lens & Analytics](#11-s3-storage-lens--analytics)
12. [Design Decisions Summary](#12-design-decisions-summary)

---

## 1. Why Tiered Storage Exists

### 1.1 Not All Data Is Equal

Every organization stores a spectrum of data with wildly different access characteristics:

- **Hot data**: accessed frequently, often multiple times per day. Examples include
  user-facing assets, active application logs, session data, and recently uploaded content.
- **Warm data**: accessed occasionally, perhaps a few times per month. Examples include
  last month's analytics reports, older application builds, and reference datasets.
- **Cold data**: rarely touched, maybe once or twice a year. Examples include compliance
  archives, historical backups, and audit trails.
- **Frozen data**: may never be accessed again, but must be retained for legal or regulatory
  reasons. Examples include seven-year financial records and healthcare data subject to HIPAA
  retention requirements.

Treating all of this data the same --- storing it on the fastest, most available, most
expensive media --- is an enormous waste of money.

### 1.2 The Cost Optimization Imperative

S3 customers collectively store **exabytes** of data. At that scale, even small per-GB
savings compound into millions of dollars.

The fundamental tradeoff that tiered storage exploits:

```
Fast, highly available storage   =   Expensive
Slow, less available storage     =   Cheap

Hot data needs speed       --> pay the premium
Cold data needs capacity   --> use cheap media
```

### 1.3 Concrete Example: A Company Storing 100 TB of Logs

Consider a SaaS company that ingests 100 TB of application logs per month. Their access
pattern is predictable: logs are heavily queried in the first 30 days for debugging and
monitoring, occasionally referenced for the next 60 days, and then retained purely for
compliance for one year.

**Scenario A --- Everything in S3 Standard:**

```
100 TB = 100,000 GB
Storage cost: 100,000 GB x $0.023/GB/month = $2,300/month
Annual cost:  $2,300 x 12 = $27,600/year
```

**Scenario B --- Lifecycle tiering:**

```
Day 0-30:   S3 Standard         100,000 GB x $0.023   = $2,300/month (1 month)
Day 30-90:  S3 Standard-IA      100,000 GB x $0.0125  = $1,250/month (2 months)
Day 90-365: S3 Glacier Flexible  100,000 GB x $0.0036  = $360/month  (9 months)

Annual cost: $2,300 + ($1,250 x 2) + ($360 x 9) = $8,040/year
```

**Savings: $19,560/year (71% reduction)**

And this is for just one month's worth of logs. If the company retains a rolling year of
logs, the savings multiply dramatically.

### 1.4 The Tiered Storage Mental Model

```
                         ACCESS FREQUENCY
                ┌──────────────────────────────┐
                │ High                     Low │
         ┌──────┼──────────────────────────────┼──────┐
  HIGH   │      │  S3 Standard              S3 │      │
         │      │  ($0.023/GB)          Glacier │      │
  COST   │      │                    Instant    │      │
  PER    │      │                  ($0.004/GB)  │      │
  GB     │      │                               │      │
  STORED │      │  S3 Standard-IA      Glacier  │      │
         │      │  ($0.0125/GB)      Flexible   │      │
  LOW    │      │                  ($0.0036/GB) │      │
         │      │  One Zone-IA       Glacier    │      │
         │      │  ($0.01/GB)      Deep Archive │      │
         │      │                 ($0.00099/GB) │      │
         └──────┼──────────────────────────────┼──────┘
                └──────────────────────────────┘
                         RETRIEVAL COST
                         (inverse relationship)
```

The key insight: **storage cost and retrieval cost are inversely related**. Cheaper storage
classes charge more to get your data back. This is the economic lever that makes tiered
storage work.

---

## 2. Complete Storage Class Reference

### 2.1 S3 Standard

**Use case:** Frequently accessed data with no special constraints. The default and most
commonly used storage class.

| Attribute              | Value                                     |
|------------------------|-------------------------------------------|
| Availability SLA       | 99.99%                                    |
| Durability             | 99.999999999% (11 nines)                  |
| Storage cost           | ~$0.023/GB/month (us-east-1)              |
| Retrieval fee          | None                                      |
| First-byte latency     | Milliseconds                              |
| Min storage duration   | None                                      |
| Min object size        | None (billed for actual size)             |
| AZ redundancy          | 3 AZs                                     |

**Typical workloads:**
- Content distribution and media hosting
- Big data analytics (active datasets)
- Dynamic website content
- Mobile and gaming application assets
- Any data that is accessed more than once per month

**Why it costs more:**
- Data is stored on high-performance media (SSDs or fast HDDs)
- Replicated (via erasure coding) across 3 AZs for maximum durability and availability
- Optimized for low-latency retrieval at any time
- No retrieval fee means S3 absorbs the I/O cost into the storage price

---

### 2.2 S3 Standard-Infrequent Access (Standard-IA)

**Use case:** Data accessed less than once a month, but requiring instant access when needed.
The sweet spot for backups, disaster recovery copies, and older datasets that are still
occasionally referenced.

| Attribute              | Value                                     |
|------------------------|-------------------------------------------|
| Availability SLA       | 99.9% (slightly lower than Standard)      |
| Durability             | 99.999999999% (11 nines)                  |
| Storage cost           | ~$0.0125/GB/month (us-east-1)             |
| Retrieval fee          | $0.01/GB                                  |
| First-byte latency     | Milliseconds                              |
| Min storage duration   | 30 days (billed for 30 even if deleted)   |
| Min object size        | 128 KB (billed for 128 KB even if smaller)|
| AZ redundancy          | 3 AZs                                     |

**Key tradeoffs vs Standard:**
- **45% cheaper** on storage per GB
- **Retrieval fee** of $0.01 per GB retrieved --- this is the catch
- Slightly lower availability SLA (99.9% vs 99.99%)
- Minimum billing constraints (30-day duration, 128 KB size)

**When Standard-IA is NOT a good choice:**
- Objects smaller than 128 KB (you'll be billed for 128 KB anyway)
- Objects accessed frequently (retrieval fees will exceed storage savings)
- Objects likely to be deleted within 30 days (minimum duration billing)

**Break-even analysis:**

```
Storage savings per GB per month:  $0.023 - $0.0125 = $0.0105
Retrieval cost per GB:             $0.01

If you retrieve data once per month:
  Savings:   $0.0105/GB
  Cost:      $0.01/GB
  Net:       $0.0005/GB savings (barely worth it)

If you retrieve data once every 3 months:
  Savings:   $0.0105 x 3 = $0.0315/GB over 3 months
  Cost:      $0.01/GB (one retrieval)
  Net:       $0.0215/GB savings (significant)

Rule of thumb: Standard-IA saves money when data is accessed < once/month
```

---

### 2.3 S3 One Zone-Infrequent Access (One Zone-IA)

**Use case:** Infrequently accessed, non-critical data that can be recreated if lost. The
cheapest option that still provides instant access.

| Attribute              | Value                                     |
|------------------------|-------------------------------------------|
| Availability SLA       | 99.5%                                     |
| Durability             | 99.999999999% within a single AZ          |
| Storage cost           | ~$0.01/GB/month (us-east-1)               |
| Retrieval fee          | $0.01/GB                                  |
| First-byte latency     | Milliseconds                              |
| Min storage duration   | 30 days                                   |
| Min object size        | 128 KB                                    |
| AZ redundancy          | 1 AZ only                                 |

**Critical distinction on durability:**
- Within its single AZ, One Zone-IA provides 11 nines of durability (same as Standard)
- But if that AZ is **destroyed** (fire, flood, earthquake), the data is gone
- This is the fundamental risk tradeoff: single-AZ means no geographic redundancy
- Standard-IA survives AZ destruction; One Zone-IA does not

**Good fit:**
- Thumbnails or resized images (originals stored elsewhere)
- Preprocessed analytics results (can be recomputed from raw data)
- Cross-region replicas (the original is the primary copy)
- Development and test data

**Bad fit:**
- Primary copies of any data you cannot recreate
- Compliance or legal data (regulations often require multi-AZ or multi-region)
- Any data whose loss would cause business disruption

---

### 2.4 S3 Glacier Instant Retrieval

**Use case:** Archive data that is rarely accessed (once per quarter or less) but absolutely
must be available instantly when needed. Think medical images, news media archives, or
satellite imagery.

| Attribute              | Value                                     |
|------------------------|-------------------------------------------|
| Availability SLA       | 99.9%                                     |
| Durability             | 99.999999999% (11 nines)                  |
| Storage cost           | ~$0.004/GB/month (us-east-1)              |
| Retrieval fee          | $0.03/GB                                  |
| First-byte latency     | Milliseconds (instant)                    |
| Min storage duration   | 90 days                                   |
| Min object size        | 128 KB                                    |
| AZ redundancy          | 3 AZs                                     |

**Why Glacier Instant Retrieval exists:**
Before this class was introduced (late 2021), customers who wanted low-cost archival
storage but instant access had an awkward choice:
- Standard-IA: instant access but $0.0125/GB (expensive for archives)
- Glacier Flexible: cheap at $0.0036/GB but retrieval takes hours

Glacier Instant Retrieval fills the gap: archive-level pricing ($0.004/GB) with
millisecond access. The catch is a higher retrieval fee ($0.03/GB) and 90-day minimum
storage duration.

**Cost comparison for 10 TB accessed once per quarter:**

```
Standard-IA:
  Storage: 10,000 GB x $0.0125 x 3 months = $375
  Retrieval: 10,000 GB x $0.01 = $100
  Total per quarter: $475

Glacier Instant Retrieval:
  Storage: 10,000 GB x $0.004 x 3 months = $120
  Retrieval: 10,000 GB x $0.03 = $300
  Total per quarter: $420

Savings with Glacier Instant: ~12%
(Savings increase dramatically if data is accessed less often)
```

---

### 2.5 S3 Glacier Flexible Retrieval (formerly S3 Glacier)

**Use case:** True archive data where waiting minutes to hours for retrieval is acceptable.
The classic archival tier for backups, compliance data, and historical records.

| Attribute              | Value                                     |
|------------------------|-------------------------------------------|
| Availability SLA       | 99.99% (after restore)                    |
| Durability             | 99.999999999% (11 nines)                  |
| Storage cost           | ~$0.0036/GB/month (us-east-1)             |
| First-byte latency     | Minutes to hours (see retrieval tiers)    |
| Min storage duration   | 90 days                                   |
| Min object size        | 40 KB (plus 32 KB metadata overhead)      |
| AZ redundancy          | 3 AZs                                     |

**Retrieval tiers:**

| Tier       | Latency       | Cost per GB | Cost per 1,000 requests | Best for                    |
|------------|---------------|-------------|-------------------------|-----------------------------|
| Expedited  | 1-5 minutes   | $0.03       | $10.00                  | Urgent, small retrievals    |
| Standard   | 3-5 hours     | $0.01       | $0.05                   | Normal archive access       |
| Bulk       | 5-12 hours    | $0.0025     | $0.025                  | Large-scale batch retrieval |

**Expedited retrieval provisioned capacity:**
- Expedited retrievals can fail during periods of high demand
- For guaranteed Expedited access, purchase **Provisioned Capacity Units**
- Each unit costs $100/month and guarantees up to 150 MB/s of retrieval throughput
  and 3 Expedited retrievals per 5 minutes
- Without provisioned capacity, Expedited requests are best-effort

**Restore process:**
```
1. Call RestoreObject API
   - Specify retrieval tier (Expedited / Standard / Bulk)
   - Specify number of days to keep the restored copy

2. S3 queues the retrieval job

3. After retrieval completes:
   - A temporary copy is created in S3 Standard
   - Original stays in Glacier
   - Temporary copy expires after specified number of days

4. You can GET the object while the temporary copy exists

5. After expiration, temporary copy is deleted
   - To access again, you must initiate another restore
```

---

### 2.6 S3 Glacier Deep Archive

**Use case:** Lowest-cost storage for data that will be retained for years and accessed
very rarely (if ever). Compliance archives, regulatory records, healthcare data, financial
audit trails.

| Attribute              | Value                                     |
|------------------------|-------------------------------------------|
| Availability SLA       | 99.99% (after restore)                    |
| Durability             | 99.999999999% (11 nines)                  |
| Storage cost           | ~$0.00099/GB/month (us-east-1)            |
| First-byte latency     | 12-48 hours (see retrieval tiers)         |
| Min storage duration   | 180 days                                  |
| Min object size        | 40 KB (plus 32 KB metadata overhead)      |
| AZ redundancy          | 3 AZs                                     |

**Retrieval tiers:**

| Tier     | Latency    | Cost per GB | Best for                           |
|----------|------------|-------------|------------------------------------|
| Standard | 12 hours   | $0.02       | Single object or small set restore |
| Bulk     | 48 hours   | $0.0025     | Restoring large datasets cheaply   |

**Cost perspective:**
```
1 PB (1,000,000 GB) in Glacier Deep Archive:
  Storage:  1,000,000 GB x $0.00099 = $990/month
  Annual:   $11,880/year

Same 1 PB in S3 Standard:
  Storage:  1,000,000 GB x $0.023 = $23,000/month
  Annual:   $276,000/year

Savings: $264,120/year (95.7% reduction)
```

This is why Glacier Deep Archive exists: for petabyte-scale data that must be retained
but almost never accessed, it costs less than $1,000/month per petabyte.

**When Deep Archive makes sense:**
- Data retention policies mandating 7+ years of storage
- Legal hold / litigation preservation
- Raw scientific data from completed research projects
- Historical surveillance footage
- Genomic sequencing archives

---

### 2.7 S3 Intelligent-Tiering

**Use case:** Data with unknown or changing access patterns. Intelligent-Tiering removes
the guesswork by automatically moving objects between tiers based on observed access
patterns --- with no retrieval fees.

| Attribute              | Value                                     |
|------------------------|-------------------------------------------|
| Availability SLA       | 99.9%                                     |
| Durability             | 99.999999999% (11 nines)                  |
| Storage cost           | Varies by tier (see below)                |
| Monitoring fee         | $0.0025 per 1,000 objects/month           |
| Retrieval fee          | None (automatic tier transitions)         |
| First-byte latency     | Milliseconds (for non-archive tiers)      |
| Min storage duration   | None                                      |
| Min object size        | None (but monitoring fee makes tiny objects expensive) |
| AZ redundancy          | 3 AZs                                     |

**Automatic tiers (always active):**

| Tier                    | Trigger                     | Storage cost       |
|-------------------------|-----------------------------|--------------------|
| Frequent Access         | Default / on any access     | ~$0.023/GB/month   |
| Infrequent Access       | 30 days without access      | ~$0.0125/GB/month  |
| Archive Instant Access  | 90 days without access      | ~$0.004/GB/month   |

**Optional archive tiers (opt-in required):**

| Tier                    | Trigger                     | Storage cost        |
|-------------------------|-----------------------------|---------------------|
| Archive Access          | 90-730 days without access  | ~$0.0036/GB/month   |
| Deep Archive Access     | 180-730 days without access | ~$0.00099/GB/month  |

Opting into archive tiers requires configuring the bucket or object-level settings.
Objects moved to archive tiers require restoration before access (same as Glacier).

---

## 3. Storage Class Comparison Table

| Feature                | Standard  | Standard-IA | One Zone-IA | Glacier Instant | Glacier Flexible | Glacier Deep Archive | Intelligent-Tiering |
|------------------------|-----------|-------------|-------------|-----------------|------------------|----------------------|---------------------|
| **Storage $/GB/mo**    | $0.023    | $0.0125     | $0.01       | $0.004          | $0.0036          | $0.00099             | Varies by tier      |
| **Retrieval $/GB**     | $0.00     | $0.01       | $0.01       | $0.03           | $0.01-$0.03      | $0.0025-$0.02        | $0.00               |
| **Availability SLA**   | 99.99%    | 99.9%       | 99.5%       | 99.9%           | 99.99%*          | 99.99%*              | 99.9%               |
| **Durability**         | 11 nines  | 11 nines    | 11 nines**  | 11 nines        | 11 nines         | 11 nines             | 11 nines            |
| **AZs**                | >= 3      | >= 3        | 1           | >= 3            | >= 3             | >= 3                 | >= 3                |
| **Min duration**       | None      | 30 days     | 30 days     | 90 days         | 90 days          | 180 days             | None                |
| **Min object size**    | None      | 128 KB      | 128 KB      | 128 KB          | 40 KB (+32 KB)   | 40 KB (+32 KB)       | None                |
| **First-byte latency** | ms        | ms          | ms          | ms              | min-hrs          | 12-48 hrs            | ms (non-archive)    |
| **Use case summary**   | General   | Infrequent  | Non-critical| Rare instant    | Archives         | Long-term archive    | Unknown patterns    |
|                        | purpose   | access,     | infrequent  | access          | (hrs OK)         | (days OK)            |                     |
|                        |           | instant     | access      | archives        |                  |                      |                     |

\* After restore to Standard
\** Within a single AZ; data lost if AZ is destroyed

---

## 4. How Storage Classes Work Internally

### 4.1 Standard / Standard-IA / One Zone-IA --- Always-Online Classes

These three classes share the same fundamental architecture. Data is stored on always-on
storage nodes and is immediately accessible:

```
                        PUT Object (Standard)
                               │
                               ▼
                    ┌──────────────────────┐
                    │   Front-End Router   │
                    └──────────┬───────────┘
                               │
                    ┌──────────▼───────────┐
                    │   Placement Engine   │
                    │   (select AZs and    │
                    │    storage nodes)    │
                    └──────────┬───────────┘
                               │
              ┌────────────────┼────────────────┐
              │                │                │
     ┌────────▼──────┐ ┌──────▼────────┐ ┌─────▼───────┐
     │   AZ-1 Node   │ │   AZ-2 Node   │ │  AZ-3 Node  │
     │  (data shard + │ │  (data shard + │ │ (data shard +│
     │   parity)      │ │   parity)      │ │  parity)     │
     └───────────────┘ └───────────────┘ └──────────────┘

     For One Zone-IA: only AZ-1 is used (single AZ)
```

**Key insight: Standard vs Standard-IA is primarily a billing distinction, not a
physical storage distinction.**

- Both may use the same storage hardware
- Both use the same erasure coding across 3 AZs
- S3 does not necessarily place IA data on slower disks
- The difference is in the billing model: IA charges less for storage but adds a
  per-GB retrieval fee
- S3 may co-locate IA and Standard data on the same physical disks

The availability SLA difference (99.99% vs 99.9%) reflects S3's operational commitment
rather than a physical infrastructure difference. AWS provides fewer guarantees for IA
because the expectation is that access is infrequent.

### 4.2 Glacier Flexible Retrieval --- Cold Storage Backend

Glacier Flexible Retrieval is fundamentally different from the always-online classes.
Data is moved to a separate cold storage infrastructure:

```
                    PUT Object (Glacier)
                           │
                           ▼
                ┌────────────────────┐
                │  Metadata Service  │
                │  (records object   │
                │   in Glacier)      │
                └─────────┬──────────┘
                          │
                ┌─────────▼──────────┐
                │  Cold Storage      │
                │  Backend           │
                │                    │
                │  Possible media:   │
                │  - Dense HDDs      │
                │    (powered down   │
                │     between uses)  │
                │  - Tape libraries  │
                │    (robotic tape   │
                │     retrieval)     │
                │  - Custom archival │
                │    hardware        │
                └────────────────────┘
```

**Data is "frozen":** it is not immediately accessible. The storage media may literally
be powered off or the data may be on tape cartridges stored in robotic libraries.

**Retrieval ("thawing") process by tier:**

```
Expedited (1-5 minutes):
  ┌──────────────┐     ┌──────────────────┐     ┌──────────────┐
  │ RestoreObject │ --> │ Provisioned HW   │ --> │ Temp copy in │
  │ API call      │     │ (dedicated       │     │ S3 Standard  │
  │               │     │  retrieval nodes)│     │ (N days)     │
  └──────────────┘     └──────────────────┘     └──────────────┘
  Uses pre-allocated hardware; may fail without provisioned capacity

Standard (3-5 hours):
  ┌──────────────┐     ┌──────────────────┐     ┌──────────────┐
  │ RestoreObject │ --> │ Queued retrieval  │ --> │ Temp copy in │
  │ API call      │     │ (processed in    │     │ S3 Standard  │
  │               │     │  FIFO order)     │     │ (N days)     │
  └──────────────┘     └──────────────────┘     └──────────────┘
  Jobs are queued and processed by shared retrieval workers

Bulk (5-12 hours):
  ┌──────────────┐     ┌──────────────────┐     ┌──────────────┐
  │ RestoreObject │ --> │ Low-priority     │ --> │ Temp copy in │
  │ API call      │     │ batch processing │     │ S3 Standard  │
  │               │     │ (off-peak)       │     │ (N days)     │
  └──────────────┘     └──────────────────┘     └──────────────┘
  Scheduled during off-peak hours for maximum efficiency
```

Once thawed, a **temporary copy** is placed in S3 Standard-equivalent storage. The
original remains in Glacier. The temporary copy auto-expires after the specified
retention period.

### 4.3 Glacier Deep Archive --- Ultra-Cold Storage

Deep Archive represents the lowest tier of the storage hierarchy:

- **Likely backed by tape libraries** or other offline/near-offline media
- Retrieval requires physically locating and mounting media, reading data, and staging
  it to online storage
- The 12-48 hour retrieval times reflect the physical constraints of the media
- Storage density is maximized: tapes can hold hundreds of terabytes per cartridge at
  a fraction of the cost of disk storage
- Tapes have a shelf life of 30+ years, making them ideal for long-term retention

**Why tape is still relevant:**
```
Cost per TB (approximate):
  Enterprise SSD:    $80-150/TB
  Enterprise HDD:    $15-25/TB
  LTO-9 Tape:       $5-7/TB

Tape is 3-5x cheaper per TB than HDD and 15-25x cheaper than SSD.
At petabyte scale, this difference is measured in millions of dollars.
```

---

## 5. S3 Intelligent-Tiering --- Deep Dive

### 5.1 How It Works Internally

Intelligent-Tiering monitors access patterns at the individual object level and
automatically moves objects between tiers:

```
Object uploaded to Intelligent-Tiering
  │
  ▼
┌─────────────────────────────────────────────────────┐
│              Frequent Access Tier                    │
│         (same pricing as S3 Standard)               │
│              ~$0.023/GB/month                        │
└──────────────────────┬──────────────────────────────┘
                       │
                       │  No access for 30 consecutive days
                       │
                       ▼
┌─────────────────────────────────────────────────────┐
│            Infrequent Access Tier                    │
│       (same pricing as S3 Standard-IA)              │
│              ~$0.0125/GB/month                       │
└──────────────────────┬──────────────────────────────┘
                       │
                       │  No access for 90 consecutive days
                       │
                       ▼
┌─────────────────────────────────────────────────────┐
│          Archive Instant Access Tier                 │
│     (same pricing as Glacier Instant Retrieval)     │
│              ~$0.004/GB/month                        │
└──────────────────────┬──────────────────────────────┘
                       │
                       │  No access for 90-730 days (OPT-IN)
                       │
                       ▼
┌─────────────────────────────────────────────────────┐
│            Archive Access Tier                       │
│    (same pricing as Glacier Flexible Retrieval)     │
│              ~$0.0036/GB/month                       │
│     [requires opt-in configuration]                 │
└──────────────────────┬──────────────────────────────┘
                       │
                       │  No access for 180-730 days (OPT-IN)
                       │
                       ▼
┌─────────────────────────────────────────────────────┐
│          Deep Archive Access Tier                    │
│    (same pricing as Glacier Deep Archive)           │
│              ~$0.00099/GB/month                      │
│     [requires opt-in configuration]                 │
└─────────────────────────────────────────────────────┘

         ▲ On any GET request, the object is
         │ automatically moved BACK to the
         │ Frequent Access tier (no retrieval fee
         │ for the first three tiers; archive tiers
         │ require restore)
```

### 5.2 Access Monitoring Mechanics

**Per-object tracking:**
- S3 maintains a last-access timestamp for every object in Intelligent-Tiering
- Any GET, HEAD, or copy operation updates this timestamp
- PUT does not reset the timer (uploading a new version creates a new object)

**Background evaluator:**
- A background service runs periodically (approximately daily)
- It scans object metadata and compares last-access timestamps against thresholds
- Objects that have crossed a threshold are queued for tier transition
- Transitions happen asynchronously (not at the exact 30/90/180-day mark)

**Automatic promotion on access:**
- When an object in the Infrequent or Archive Instant tier is accessed (GET),
  it is automatically moved back to the Frequent Access tier
- This happens transparently --- no special API call needed
- There is **no retrieval fee** for this promotion (unlike Standard-IA)
- For archive tiers (Archive Access and Deep Archive), a restore is needed first,
  then the object is promoted after access

### 5.3 The Monitoring Fee Consideration

Intelligent-Tiering charges $0.0025 per 1,000 objects per month for access monitoring.
This fee is independent of object size.

**Impact analysis:**

```
Scenario: 1 million objects, average size 1 MB (1 TB total)

Monitoring fee:  (1,000,000 / 1,000) x $0.0025 = $2.50/month
Storage (Standard): 1,000 GB x $0.023 = $23.00/month
Monitoring as % of storage: 10.9%

Scenario: 1 million objects, average size 1 KB (1 GB total)

Monitoring fee:  (1,000,000 / 1,000) x $0.0025 = $2.50/month
Storage (Standard): 1 GB x $0.023 = $0.023/month
Monitoring as % of storage: 10,869% (!!!)
```

**Conclusion:** Intelligent-Tiering is cost-effective for larger objects (100 KB+) but
can be extremely expensive relative to storage cost for many small objects. For workloads
with millions of tiny objects, manual lifecycle policies are more economical.

### 5.4 When to Use Intelligent-Tiering vs Manual Lifecycle

| Aspect                  | Intelligent-Tiering              | Manual Lifecycle Rules           |
|-------------------------|----------------------------------|----------------------------------|
| **Best for**            | Unknown or unpredictable access  | Well-known, predictable access   |
|                         | patterns                         | patterns                         |
| **Cost overhead**       | $0.0025 per 1,000 objects/month  | No extra fee                     |
|                         | (monitoring fee)                 |                                  |
| **Granularity**         | Per-object, based on actual      | Per-prefix or per-tag, based on  |
|                         | access history                   | object age                       |
| **Retrieval fees**      | None (for auto tiers)            | Yes (for IA and Glacier classes) |
| **Control**             | Automatic, hands-off             | Full manual control              |
| **Risk**                | Monitoring fee on many small     | Wrong policy = overpaying or     |
|                         | objects can be expensive          | unexpected retrieval fees        |
| **Latency guarantee**   | Milliseconds for non-archive     | Depends on target storage class  |
|                         | tiers                            |                                  |
| **Versioning support**  | Applies to current version only  | Can target noncurrent versions   |
| **Archive tiers**       | Optional opt-in required         | Direct transition to Glacier     |

**Decision flowchart:**

```
Do you know your data's access pattern?
  │
  ├── YES, it's predictable
  │     │
  │     └── Use manual lifecycle rules
  │         (cheaper, more control)
  │
  ├── NO, it's unpredictable
  │     │
  │     ├── Are most objects > 128 KB?
  │     │     │
  │     │     ├── YES --> Use Intelligent-Tiering
  │     │     │
  │     │     └── NO  --> Use S3 Analytics to study
  │     │               patterns first, then decide
  │     │
  │     └── Mixed sizes?
  │           │
  │           └── Use Intelligent-Tiering for large
  │               objects, Standard for small ones
  │
  └── PARTIALLY known
        │
        └── Use lifecycle for known-pattern prefixes,
            Intelligent-Tiering for unknown-pattern prefixes
```

---

## 6. Lifecycle Policies --- Configuration & Mechanics

### 6.1 Lifecycle Rule Structure

S3 lifecycle policies are defined as XML configurations attached to a bucket. Each rule
specifies a filter (which objects it applies to), a status (enabled/disabled), and one
or more actions.

**Complete lifecycle configuration example:**

```xml
<LifecycleConfiguration>
  <Rule>
    <ID>move-logs-to-ia</ID>
    <Filter>
      <Prefix>logs/</Prefix>
    </Filter>
    <Status>Enabled</Status>
    <Transition>
      <Days>30</Days>
      <StorageClass>STANDARD_IA</StorageClass>
    </Transition>
    <Transition>
      <Days>90</Days>
      <StorageClass>GLACIER</StorageClass>
    </Transition>
    <Expiration>
      <Days>365</Days>
    </Expiration>
    <AbortIncompleteMultipartUpload>
      <DaysAfterInitiation>7</DaysAfterInitiation>
    </AbortIncompleteMultipartUpload>
  </Rule>

  <Rule>
    <ID>archive-old-reports</ID>
    <Filter>
      <And>
        <Prefix>reports/</Prefix>
        <Tag>
          <Key>department</Key>
          <Value>finance</Value>
        </Tag>
      </And>
    </Filter>
    <Status>Enabled</Status>
    <Transition>
      <Days>60</Days>
      <StorageClass>GLACIER_IR</StorageClass>
    </Transition>
    <Transition>
      <Days>180</Days>
      <StorageClass>DEEP_ARCHIVE</StorageClass>
    </Transition>
  </Rule>

  <Rule>
    <ID>clean-old-versions</ID>
    <Filter>
      <Prefix>data/</Prefix>
    </Filter>
    <Status>Enabled</Status>
    <NoncurrentVersionTransition>
      <NoncurrentDays>30</NoncurrentDays>
      <StorageClass>STANDARD_IA</StorageClass>
    </NoncurrentVersionTransition>
    <NoncurrentVersionTransition>
      <NoncurrentDays>90</NoncurrentDays>
      <StorageClass>GLACIER</StorageClass>
    </NoncurrentVersionTransition>
    <NoncurrentVersionExpiration>
      <NoncurrentDays>365</NoncurrentDays>
    </NoncurrentVersionExpiration>
    <ExpiredObjectDeleteMarker/>
  </Rule>
</LifecycleConfiguration>
```

### 6.2 Supported Filter Types

| Filter Type          | Description                              | Example                          |
|----------------------|------------------------------------------|----------------------------------|
| **Prefix**           | Match objects whose key starts with      | `<Prefix>logs/</Prefix>`        |
|                      | the specified string                     |                                  |
| **Tag**              | Match objects with a specific tag        | `<Tag><Key>env</Key>`           |
|                      | key-value pair                           | `<Value>prod</Value></Tag>`     |
| **And**              | Combine prefix and/or multiple tags      | Prefix + Tag together            |
| **ObjectSizeGreater**| Match objects larger than N bytes        | `<ObjectSizeGreaterThan>128000` |
|                      |                                          | `</ObjectSizeGreaterThan>`      |
| **ObjectSizeLess**   | Match objects smaller than N bytes       | `<ObjectSizeLessThan>1048576`   |
|                      |                                          | `</ObjectSizeLessThan>`         |
| **(empty)**          | Match all objects in the bucket          | `<Filter/>`                     |

### 6.3 Supported Lifecycle Actions

**1. Transition** --- Move to a different storage class after N days:
```xml
<Transition>
  <Days>30</Days>
  <StorageClass>STANDARD_IA</StorageClass>
</Transition>
```
Can also use a specific date instead of a relative number of days:
```xml
<Transition>
  <Date>2026-06-01T00:00:00.000Z</Date>
  <StorageClass>GLACIER</StorageClass>
</Transition>
```

**2. Expiration** --- Delete the object after N days:
```xml
<Expiration>
  <Days>365</Days>
</Expiration>
```

**3. NoncurrentVersionTransition** --- Transition old versions (requires versioning):
```xml
<NoncurrentVersionTransition>
  <NoncurrentDays>30</NoncurrentDays>
  <StorageClass>GLACIER</StorageClass>
</NoncurrentVersionTransition>
```

**4. NoncurrentVersionExpiration** --- Delete old versions after N days:
```xml
<NoncurrentVersionExpiration>
  <NoncurrentDays>90</NoncurrentDays>
</NoncurrentVersionExpiration>
```
Can also limit the number of noncurrent versions retained:
```xml
<NoncurrentVersionExpiration>
  <NoncurrentDays>1</NoncurrentDays>
  <NewerNoncurrentVersions>3</NewerNoncurrentVersions>
</NoncurrentVersionExpiration>
```
This keeps the 3 most recent noncurrent versions and deletes older ones after 1 day.

**5. AbortIncompleteMultipartUpload** --- Clean up stale multipart uploads:
```xml
<AbortIncompleteMultipartUpload>
  <DaysAfterInitiation>7</DaysAfterInitiation>
</AbortIncompleteMultipartUpload>
```
Multipart uploads that are started but never completed (or aborted) continue to consume
storage. This action automatically aborts them after the specified number of days.

**6. ExpiredObjectDeleteMarker** --- Remove delete markers with no versions:
```xml
<ExpiredObjectDeleteMarker/>
```
In a versioned bucket, deleting an object creates a delete marker. If all noncurrent
versions have been deleted, the delete marker serves no purpose. This action cleans up
these orphaned delete markers.

---

## 7. Lifecycle Evaluation Engine

### 7.1 Architecture Overview

S3's lifecycle evaluation is a massive-scale background system that continuously
processes trillions of objects across millions of buckets:

```
                    ┌───────────────────────────────┐
                    │     Lifecycle Evaluator        │
                    │     (background service)       │
                    │                               │
                    │  Runs continuously, scanning   │
                    │  metadata partitions in order  │
                    └───────────────┬───────────────┘
                                    │
                     Scans metadata partitions
                     (each partition = subset of
                      a bucket's key space)
                                    │
                    ┌───────────────▼───────────────┐
                    │     For each object:          │
                    │                               │
                    │  1. Load lifecycle rules for  │
                    │     the object's bucket       │
                    │                               │
                    │  2. Check object metadata:    │
                    │     - creation date           │
                    │     - key prefix              │
                    │     - tags                    │
                    │     - object size             │
                    │     - current storage class   │
                    │     - version status          │
                    │                               │
                    │  3. Evaluate each rule:       │
                    │     - Does filter match?      │
                    │     - Has threshold passed?   │
                    │     - Is action valid for     │
                    │       current storage class?  │
                    │                               │
                    │  4. If rule matches:          │
                    │     → queue action            │
                    └───────────────┬───────────────┘
                                    │
              ┌─────────────────────┼─────────────────────┐
              │                     │                     │
    ┌─────────▼─────────┐ ┌────────▼────────┐ ┌──────────▼──────────┐
    │   Transition      │ │   Expiration    │ │   Multipart         │
    │   Worker          │ │   Worker        │ │   Cleanup Worker    │
    │                   │ │                 │ │                     │
    │  - Read object    │ │  - Delete       │ │  - Abort stale      │
    │    from current   │ │    object       │ │    multipart        │
    │    storage        │ │  - If versioned:│ │    uploads          │
    │  - Re-encode for  │ │    create       │ │  - Free incomplete  │
    │    target class   │ │    delete       │ │    parts            │
    │  - Write to new   │ │    marker       │ │                     │
    │    storage        │ │                 │ │                     │
    │  - Update         │ │                 │ │                     │
    │    metadata       │ │                 │ │                     │
    │  - Delete old     │ │                 │ │                     │
    │    data           │ │                 │ │                     │
    └───────────────────┘ └─────────────────┘ └─────────────────────┘
```

### 7.2 Key Characteristics of the Evaluation Engine

**Asynchronous, not real-time:**
- Lifecycle actions do not execute at the exact moment the threshold is crossed
- Actions are processed within **24-48 hours** of the threshold date
- This is by design: exact-second precision is unnecessary and would be prohibitively
  expensive at S3's scale

**Batched for efficiency:**
- Transitions are not executed one-by-one; they are batched
- The evaluator collects qualifying objects and processes them in bulk
- Batch processing reduces I/O overhead and improves throughput

**Partitioned evaluation:**
- S3's metadata is partitioned by key range
- Each evaluator instance handles a specific set of partitions
- This allows the evaluation to scale horizontally across the entire S3 fleet
- No single evaluator needs to know about all objects in all buckets

**Idempotent operations:**
- Each lifecycle action is designed to be idempotent
- If an evaluator crashes mid-batch, it can safely re-evaluate the same objects
- Objects already transitioned will be skipped (current storage class check)

### 7.3 Evaluation Timing

```
Object created at Day 0
Rule: Transition to STANDARD_IA after 30 days

Day 0   ─── Object created in S3 Standard
          │
Day 29  ─── Evaluator checks: 29 days < 30 days → no action
          │
Day 30  ─── Threshold crossed. Evaluator may or may not have
          │  scanned this partition yet today
          │
Day 30-32 ─ Evaluator scans partition, finds qualifying object,
             queues transition action
          │
Day 30-32 ─ Transition worker processes the action
             Object is now in STANDARD_IA

The exact timing depends on when the evaluator reaches this
object's partition. Could be Day 30, could be Day 31 or 32.
```

---

## 8. Transition Mechanics --- What Happens Physically

### 8.1 Standard to Standard-IA

This transition may be **purely a metadata change**:

```
Before:
  Metadata: { storage_class: "STANDARD", ... }
  Data:     [chunk_1 @ AZ1] [chunk_2 @ AZ2] [chunk_3 @ AZ3]

After:
  Metadata: { storage_class: "STANDARD_IA", ... }
  Data:     [chunk_1 @ AZ1] [chunk_2 @ AZ2] [chunk_3 @ AZ3]
            (same chunks, same locations, same erasure coding)
```

Since Standard and Standard-IA may use the same physical infrastructure, the transition
primarily affects billing metadata. The object remains on the same storage nodes with
the same erasure coding scheme.

In some cases, S3 may re-encode the data with slightly different parameters optimized
for infrequent access patterns, but this is an internal optimization detail.

### 8.2 Standard to Glacier Flexible Retrieval

This is a **heavyweight operation** involving actual data movement:

```
Step 1: Read current data
  ┌──────────────────────────────┐
  │  Standard Storage Nodes      │
  │  [chunk_1] [chunk_2] [chunk_3]│
  └──────────────┬───────────────┘
                 │ Read all chunks
                 ▼

Step 2: Re-encode for cold storage
  ┌──────────────────────────────┐
  │  Transition Worker           │
  │  - Reconstruct original data │
  │  - Re-encode with cold       │
  │    storage parameters        │
  │  - Possibly different chunk  │
  │    sizes and parity scheme   │
  └──────────────┬───────────────┘
                 │ Write new chunks
                 ▼

Step 3: Write to cold storage backend
  ┌──────────────────────────────┐
  │  Cold Storage Backend        │
  │  [cold_chunk_1] [cold_chunk_2]│
  │  (on powered-down HDDs,      │
  │   tape, or archival media)   │
  └──────────────────────────────┘

Step 4: Update metadata
  Metadata: {
    storage_class: "GLACIER",
    chunk_map: [cold_chunk_1, cold_chunk_2],
    cold_storage_location: "..."
  }

Step 5: Delete old Standard chunks
  Standard storage nodes release the space
```

**Why this is done in batch during off-peak hours:**
- Reading and re-writing data consumes significant I/O bandwidth
- Doing this for millions of objects simultaneously would impact live traffic
- S3 schedules heavy transitions during low-traffic periods

### 8.3 Glacier Retrieval (Restore)

When a client calls the RestoreObject API, the reverse process begins:

```
Client: POST /{bucket}/{key}?restore
Body: <RestoreRequest>
        <Days>7</Days>
        <GlacierJobParameters>
          <Tier>Standard</Tier>
        </GlacierJobParameters>
      </RestoreRequest>

                 │
                 ▼
  ┌──────────────────────────────┐
  │  Restore Job Queue           │
  │  (priority based on tier)    │
  │  Expedited > Standard > Bulk │
  └──────────────┬───────────────┘
                 │
                 ▼
  ┌──────────────────────────────┐
  │  Retrieval Worker            │
  │  1. Locate cold chunks       │
  │  2. Power up media / mount   │
  │     tape (if needed)         │
  │  3. Read cold chunks         │
  │  4. Reconstruct object       │
  │  5. Write temporary copy to  │
  │     Standard storage         │
  └──────────────┬───────────────┘
                 │
                 ▼
  ┌──────────────────────────────┐
  │  Temporary Standard Copy     │
  │  - Available for GET         │
  │  - Expires after N days      │
  │  - Original stays in Glacier │
  └──────────────────────────────┘

During restore:
  Object metadata: {
    storage_class: "GLACIER",
    restore_status: "in-progress" → "completed",
    restore_expiry: "2026-02-19T00:00:00Z"
  }
```

**Important:** The original object remains in Glacier. The restore creates a temporary
**copy** in Standard. You are billed for both the Glacier storage and the temporary
Standard copy during the restore period.

---

## 9. Cost Optimization Strategies

### 9.1 Strategy 1: Lifecycle-Based Tiering for Known Patterns

This is the most common and effective strategy for data with predictable access patterns.

**Example: Application log pipeline**

```
Day 0-30:    S3 Standard         $0.023/GB/month  — active debugging and monitoring
Day 30-90:   S3 Standard-IA      $0.0125/GB/month — occasional reference
Day 90-365:  S3 Glacier Flexible  $0.0036/GB/month — compliance retention
Day 365+:    Delete (or Deep Archive for long-term compliance)

Lifecycle policy:
  <Rule>
    <Filter><Prefix>app-logs/</Prefix></Filter>
    <Transition><Days>30</Days><StorageClass>STANDARD_IA</StorageClass></Transition>
    <Transition><Days>90</Days><StorageClass>GLACIER</StorageClass></Transition>
    <Expiration><Days>365</Days></Expiration>
  </Rule>
```

**Cost calculation for 1 TB of logs over 1 year:**

```
All in Standard:
  $0.023 x 1,000 GB x 12 months = $276.00/year

With lifecycle tiering:
  Month 1:       $0.023  x 1,000 =  $23.00
  Months 2-3:    $0.0125 x 1,000 =  $12.50 x 2 = $25.00
  Months 4-12:   $0.0036 x 1,000 =   $3.60 x 9 = $32.40
  Total:                                          $80.40/year

Savings: $195.60/year per TB (70.9% reduction)
```

**At scale (100 TB/month ingestion, rolling 1-year retention):**

```
Without lifecycle:  100 TB x 12 months avg = ~1,200 TB-months x $0.023 = $27,600/year
With lifecycle:     Complex calculation, but approximately $8,040/year
Savings:            ~$19,560/year
```

### 9.2 Strategy 2: Intelligent-Tiering for Unpredictable Patterns

When access patterns are unknown or vary by object, Intelligent-Tiering automates the
optimization:

```
Example: User-uploaded content platform
  - Some content goes viral (accessed millions of times)
  - Most content is accessed briefly then forgotten
  - No way to predict which content will be popular

Without Intelligent-Tiering:
  - Store everything in Standard: $0.023/GB (overpaying for cold content)
  - Store everything in Standard-IA: retrieval fees spike for viral content

With Intelligent-Tiering:
  - Viral content stays in Frequent Access tier: ~$0.023/GB
  - Forgotten content drops to Infrequent (30 days): ~$0.0125/GB
  - Really old content drops to Archive Instant (90 days): ~$0.004/GB
  - No retrieval fees when content suddenly becomes popular again
  - Monitoring fee: $0.0025 per 1,000 objects/month

For 10 million objects averaging 500 KB each (5 TB total):
  Monitoring: (10,000,000 / 1,000) x $0.0025 = $25/month
  If 80% of data is cold after 90 days:
    Hot: 1 TB x $0.023 = $23.00
    Cold: 4 TB x $0.004 = $16.00
    Total: $64.00/month (vs $115.00 all-Standard)
```

### 9.3 Strategy 3: S3 Storage Lens and Analytics

Use AWS's built-in tools to identify optimization opportunities:

```
S3 Storage Lens provides:
  ┌─────────────────────────────────────────────────────────────┐
  │  Dashboard                                                  │
  │                                                             │
  │  Total storage:        45.2 TB across 12 buckets            │
  │  Monthly cost:         $1,039.60                            │
  │                                                             │
  │  Recommendations:                                           │
  │  ┌───────────────────────────────────────────────────────┐  │
  │  │ 28 TB in "logs-prod" bucket has not been accessed     │  │
  │  │ in > 30 days. Consider transitioning to Standard-IA   │  │
  │  │ or Glacier.                                           │  │
  │  │ Potential savings: $322/month                          │  │
  │  └───────────────────────────────────────────────────────┘  │
  │  ┌───────────────────────────────────────────────────────┐  │
  │  │ 3.2 TB of incomplete multipart uploads detected       │  │
  │  │ across 5 buckets. Add AbortIncompleteMultipartUpload  │  │
  │  │ lifecycle rule.                                       │  │
  │  │ Potential savings: $73.60/month                        │  │
  │  └───────────────────────────────────────────────────────┘  │
  └─────────────────────────────────────────────────────────────┘
```

**S3 Analytics --- Storage Class Analysis:**
- Enable per-bucket or per-prefix
- Monitors access patterns for ~30 days
- Generates a report recommending when to transition objects to Standard-IA
- Provides a CSV export with daily access counts per age group

### 9.4 Strategy 4: Compress Before Storing

Application-level compression reduces the amount of data stored, directly reducing costs
across all storage classes:

```
Example: JSON log files
  Uncompressed:   1 TB
  gzip compressed: ~100 GB (10:1 ratio for JSON)

  Standard storage cost:
    Uncompressed: 1,000 GB x $0.023 = $23.00/month
    Compressed:   100 GB x $0.023   = $2.30/month
    Savings: $20.70/month (90%)

  Combined with lifecycle tiering:
    Even the tiered cost is reduced by 90%
```

**Common compression approaches:**
- gzip/zstd for individual objects
- Parquet or ORC for columnar data (also enables S3 Select for query pushdown)
- Application-level deduplication before upload

### 9.5 Strategy 5: One Zone-IA for Non-Critical Data

When data can be recreated or exists as a replica, One Zone-IA provides additional
savings:

```
Standard-IA:   $0.0125/GB/month (3 AZ)
One Zone-IA:   $0.01/GB/month   (1 AZ)
Savings:       20% additional reduction

Good candidates:
  - Cross-region replicas (the other region has the primary copy)
  - Derived data (thumbnails, transcoded video, preprocessed datasets)
  - Development and staging environment data
  - Cached or memoized computation results
```

### 9.6 Combined Strategy Example

A real-world data platform might combine all strategies:

```
┌─────────────────────────────────────────────────────────────────┐
│                     Data Classification                         │
│                                                                 │
│  User uploads (unpredictable access):                          │
│    → Intelligent-Tiering                                        │
│    → Auto-moves between tiers based on actual access            │
│                                                                 │
│  Application logs (predictable lifecycle):                     │
│    → Lifecycle policy: Standard → IA (30d) → Glacier (90d)     │
│    → Compressed with gzip before upload                         │
│                                                                 │
│  Analytics outputs (recreatable):                              │
│    → One Zone-IA                                                │
│    → Lifecycle: delete after 90 days (can be recomputed)       │
│                                                                 │
│  Compliance archives (must retain 7 years):                    │
│    → Direct upload to Glacier Deep Archive                      │
│    → No lifecycle needed (already cheapest tier)               │
│                                                                 │
│  Temporary processing data:                                     │
│    → Standard with aggressive expiration (7 days)              │
│    → AbortIncompleteMultipartUpload after 1 day                │
│                                                                 │
│  Monitoring: S3 Storage Lens for ongoing optimization          │
└─────────────────────────────────────────────────────────────────┘
```

---

## 10. Storage Class Constraints & Gotchas

### 10.1 Minimum Storage Duration Billing

If you store an object in a class with a minimum duration and delete it early, you are
still billed for the full minimum period:

```
Example: Upload 1 GB to Standard-IA, delete after 5 days

  Expected billing: 5 days of Standard-IA
  Actual billing:   30 days of Standard-IA (minimum duration)
  Cost: 1 GB x $0.0125 = $0.0125 (for 30 days, not 5)

Example: Upload 1 GB to Glacier Deep Archive, delete after 10 days

  Expected billing: 10 days of Deep Archive
  Actual billing:   180 days of Deep Archive (minimum duration)
  Cost: 1 GB x $0.00099 x 6 months = $0.00594
```

**Minimum storage durations by class:**

| Storage Class          | Minimum Duration |
|------------------------|------------------|
| Standard               | None             |
| Standard-IA            | 30 days          |
| One Zone-IA            | 30 days          |
| Glacier Instant        | 90 days          |
| Glacier Flexible       | 90 days          |
| Glacier Deep Archive   | 180 days         |
| Intelligent-Tiering    | None             |

### 10.2 Minimum Object Size Billing

Objects smaller than the minimum size are billed as if they were the minimum size:

```
Example: Upload a 10 KB object to Standard-IA

  Actual size:   10 KB
  Billed size:   128 KB
  Overhead:      118 KB of phantom storage you're paying for

For Glacier classes (Flexible and Deep Archive):
  Minimum object size: 40 KB
  Plus: 32 KB of metadata overhead added per object
  Effective minimum: 40 KB data + 32 KB metadata = 72 KB per object
```

**Impact of small objects:**

```
1 million 1 KB objects in Standard-IA:
  Actual data:  1 million x 1 KB = 1 GB
  Billed as:    1 million x 128 KB = 128 GB
  Storage cost: 128 GB x $0.0125 = $1.60/month (128x the expected cost!)

Same data in Standard:
  Billed as:    1 GB (no minimum)
  Storage cost: 1 GB x $0.023 = $0.023/month

Lesson: Standard is MUCH cheaper for many small objects even though
        the per-GB rate is higher
```

### 10.3 Glacier Restore Creates a Copy

When you restore an object from Glacier, you pay for both the Glacier storage AND the
temporary Standard copy:

```
1 TB object in Glacier Flexible:
  Glacier storage:  1,000 GB x $0.0036 = $3.60/month

Restore with 7-day expiry:
  Retrieval fee:    1,000 GB x $0.01 (Standard tier) = $10.00
  Temp copy:        1,000 GB x $0.023 x (7/30) = $5.37
  Glacier storage:  Still being charged ($3.60/month)
  Total for restore: ~$15.37 + ongoing Glacier storage
```

### 10.4 Transition Waterfall Constraints

Lifecycle transitions follow a one-way hierarchy. You cannot transition "upward":

```
Allowed transitions (downward only):

  S3 Standard
       │
       ├──→ S3 Standard-IA
       │         │
       ├──→ S3 One Zone-IA
       │
       ├──→ S3 Intelligent-Tiering
       │
       ├──→ S3 Glacier Instant Retrieval
       │         │
       ├──→ S3 Glacier Flexible Retrieval
       │         │
       └──→ S3 Glacier Deep Archive

NOT allowed:
  - Glacier → Standard-IA (must restore, then re-upload)
  - Standard-IA → Standard (lifecycle cannot do this)
  - One Zone-IA → Standard-IA (different AZ count, not possible)
  - Glacier Deep Archive → Glacier Flexible (not a valid transition)
```

To move data "upward," you must:
1. Restore the object from Glacier (creating a temporary Standard copy)
2. Copy the restored object to a new key/bucket with the desired storage class
3. Delete the original Glacier object

### 10.5 Transition Timing Is Not Exact

```
Rule: Transition to Standard-IA after 30 days

Expectation: Object transitions at exactly 30 days, 0 hours, 0 minutes
Reality:     Object transitions sometime between day 30 and day 32

This means:
  - Don't rely on exact transition timing for application logic
  - If you query an object's storage class on day 30, it might still be Standard
  - Plan for a 24-48 hour window of uncertainty
```

### 10.6 Lifecycle Rules and Versioning Interactions

When versioning is enabled, lifecycle behavior becomes more nuanced:

```
Versioned bucket with lifecycle expiration (365 days):

Day 0:    Upload file.txt (version v1 created, becomes current)
Day 100:  Upload file.txt again (version v2 created, v1 becomes noncurrent)
Day 365:  Lifecycle evaluates v1:
            - v1 is 365 days old → expiration applies
            - But v1 is already noncurrent!
            - Expiration on current version creates a delete marker
            - For noncurrent versions, use NoncurrentVersionExpiration

Common mistake: Setting Expiration without NoncurrentVersionExpiration
  → Current versions get delete markers, but old versions pile up forever
  → Storage costs keep growing even though objects appear "deleted"
```

### 10.7 Request Cost Differences

Different storage classes have different per-request costs:

```
PUT/COPY/POST/LIST requests (per 1,000):
  Standard:              $0.005
  Standard-IA:           $0.01 (2x Standard)
  One Zone-IA:           $0.01
  Glacier Instant:       $0.02 (4x Standard)
  Intelligent-Tiering:   $0.005

GET/SELECT requests (per 1,000):
  Standard:              $0.0004
  Standard-IA:           $0.001 (2.5x Standard)
  One Zone-IA:           $0.001
  Glacier Instant:       $0.01 (25x Standard)
  Intelligent-Tiering:   $0.0004
```

These request costs can be significant for workloads with many small, frequent requests
against IA or Glacier Instant classes.

---

## 11. S3 Storage Lens & Analytics

### 11.1 S3 Storage Lens

S3 Storage Lens is a cloud storage analytics feature that provides organization-wide
visibility into object storage usage, activity trends, and cost optimization recommendations.

**Key capabilities:**

```
┌───────────────────────────────────────────────────────────────────┐
│                    S3 Storage Lens Dashboard                     │
│                                                                   │
│  ┌─────────────────┐  ┌─────────────────┐  ┌─────────────────┐  │
│  │  Summary         │  │  Cost           │  │  Activity       │  │
│  │  Metrics         │  │  Efficiency     │  │  Metrics        │  │
│  │                  │  │                 │  │                 │  │
│  │  Total storage   │  │  % in Standard  │  │  GET/PUT rates  │  │
│  │  Object count    │  │  vs IA/Glacier  │  │  per bucket     │  │
│  │  Bucket count    │  │  Avg object age │  │  Bytes          │  │
│  │  Avg object size │  │  Savings opp.   │  │  transferred    │  │
│  └─────────────────┘  └─────────────────┘  └─────────────────┘  │
│                                                                   │
│  ┌─────────────────┐  ┌─────────────────┐  ┌─────────────────┐  │
│  │  Data            │  │  Detailed       │  │  Status         │  │
│  │  Protection      │  │  Status Codes   │  │  Codes          │  │
│  │                  │  │                 │  │                 │  │
│  │  Versioning %    │  │  200/403/404    │  │  Error rates    │  │
│  │  Encryption %    │  │  breakdown      │  │  by bucket      │  │
│  │  Replication %   │  │  per bucket     │  │                 │  │
│  └─────────────────┘  └─────────────────┘  └─────────────────┘  │
│                                                                   │
│  Recommendations:                                                 │
│  • Enable lifecycle policy on 5 buckets with no policy           │
│  • Move 28 TB of cold data to Standard-IA (save $322/mo)        │
│  • Clean 3.2 TB of incomplete multipart uploads                   │
│  • Enable versioning on 3 production buckets                     │
└───────────────────────────────────────────────────────────────────┘
```

**Lens configurations:**

| Feature                    | Free Tier                    | Advanced Tier ($0.20/million objects/month) |
|----------------------------|------------------------------|---------------------------------------------|
| Usage metrics              | 28 free metrics              | 35+ advanced metrics                        |
| Activity metrics           | No                           | Yes                                         |
| Recommendations            | Basic                        | Detailed with cost estimates                |
| Data retention             | 14 days                      | 15 months                                   |
| Prefix-level aggregation   | No                           | Yes                                         |
| CloudWatch publishing      | No                           | Yes                                         |

### 11.2 S3 Analytics --- Storage Class Analysis

Storage Class Analysis monitors access patterns for a specific bucket or prefix and
recommends when to transition data to Standard-IA.

**How it works:**

```
Enable Storage Class Analysis
  │
  │  Configure:
  │  - Bucket: my-data-bucket
  │  - Prefix filter: logs/ (optional)
  │  - Export destination: analytics-bucket/reports/
  │
  ▼
S3 collects access data for ~30 days
  │
  │  Tracks per object age group:
  │  - 0-14 days old: X% of total retrievals
  │  - 15-29 days old: Y% of total retrievals
  │  - 30-44 days old: Z% of total retrievals
  │  - ... up to 365+ days
  │
  ▼
After sufficient data, generates recommendations
  │
  │  Example output:
  │  "Objects older than 45 days in prefix 'logs/' account for
  │   only 3% of total retrievals. Transitioning to Standard-IA
  │   after 45 days would save approximately $X/month with
  │   minimal retrieval cost impact."
  │
  ▼
CSV export available for detailed analysis
```

**Important limitations:**
- Only recommends transitions to Standard-IA (not Glacier or other classes)
- Requires ~30 days of data collection before recommendations appear
- Does not automatically create lifecycle rules --- recommendations are advisory only
- Cannot be used retroactively (only analyzes access from the time it is enabled)

### 11.3 Using S3 Inventory for Audit

S3 Inventory provides a flat file listing of objects and their metadata on a daily or
weekly schedule:

```
Fields available in inventory reports:
  - Bucket name
  - Key name
  - Version ID
  - Size
  - Last modified date
  - Storage class
  - ETag
  - Multipart upload flag
  - Replication status
  - Encryption status
  - Object Lock status
  - Intelligent-Tiering access tier

Use cases:
  - Audit storage class distribution across millions of objects
  - Identify objects that should have transitioned but didn't
  - Feed into custom analytics pipelines for cost optimization
  - Compliance reporting (encryption status, lock status)
```

---

## 12. Design Decisions Summary

This section examines the key design decisions AWS made for S3's storage tiering system
and the rationale behind each.

| Decision                   | S3's Choice                        | Why                                                    |
|----------------------------|------------------------------------|--------------------------------------------------------|
| Number of tiers            | 7 storage classes                  | Different workloads have fundamentally different        |
|                            |                                    | cost/access tradeoffs; a single tier cannot optimize    |
|                            |                                    | for all patterns                                       |
| Glacier retrieval speeds   | 3 tiers (Expedited / Standard /    | Flexibility: customers pay more for faster access;     |
|                            | Bulk)                              | this maps to different physical retrieval strategies    |
| Intelligent-Tiering        | Per-object automatic movement      | Solves the "I don't know my access pattern" problem    |
|                            | based on observed access           | without requiring customers to analyze and predict     |
| Lifecycle evaluation       | Async, batch processing within     | Exact-second precision is unnecessary; batch           |
|                            | 24-48 hours                        | processing is orders of magnitude more efficient       |
| One Zone-IA                | Single-AZ option for non-critical  | Some data genuinely doesn't need cross-AZ durability;  |
|                            | data                               | removing cross-AZ replication reduces cost by 20%      |
| Minimum storage duration   | 30/90/180 days depending on class  | Prevents abuse (storing briefly in cheap tier to       |
|                            |                                    | reduce costs); ensures cold storage economics work     |
| Minimum object size        | 128 KB for IA, 40 KB for Glacier   | Small objects have high per-object overhead;            |
|                            |                                    | minimum size ensures the economics are sustainable     |
| Glacier restore = copy     | Temporary copy in Standard;        | Keeps cold storage architecture simple; avoids         |
|                            | original stays in Glacier           | "warming" cold storage nodes permanently               |
| Standard vs IA same infra  | Billing difference, not physical   | Simplifies infrastructure; the price difference is     |
|                            | storage difference                 | justified by the access pattern commitment             |
| Monitoring fee for         | $0.0025 per 1,000 objects/month    | Access tracking has real cost (per-object timestamps,  |
| Intelligent-Tiering        |                                    | background evaluation); fee covers infrastructure      |
| Lifecycle transitions      | One-way waterfall only             | Physical data movement is expensive; allowing          |
| direction                  |                                    | arbitrary transitions would create complex edge cases  |
| Storage Lens free tier     | 28 metrics, 14-day retention       | Democratizes cost optimization; advanced features      |
|                            |                                    | monetized at $0.20/million objects                     |

### 12.1 Why Not More Tiers?

Seven storage classes might seem like a lot, but each fills a distinct niche:

```
                         Instant Access                 Delayed Access
                    ┌────────────────────┐        ┌────────────────────┐
  Multi-AZ          │  Standard          │        │                    │
  Frequently        │  ($0.023)          │        │  (no class here —  │
  Accessed          │                    │        │   if you access    │
                    │                    │        │   data often, you  │
                    │                    │        │   need instant     │
                    │                    │        │   access)          │
                    ├────────────────────┤        ├────────────────────┤
  Multi-AZ          │  Standard-IA       │        │                    │
  Infrequently      │  ($0.0125)         │        │  (would be cheaper │
  Accessed          │                    │        │   but no demand)   │
                    ├────────────────────┤        ├────────────────────┤
  Single-AZ         │  One Zone-IA       │        │                    │
  Infrequently      │  ($0.01)           │        │                    │
  Accessed          │                    │        │                    │
                    ├────────────────────┤        ├────────────────────┤
  Multi-AZ          │  Glacier Instant   │        │  Glacier Flexible  │
  Rarely            │  ($0.004)          │        │  ($0.0036)         │
  Accessed          │                    │        │  (minutes-hours)   │
                    ├────────────────────┤        ├────────────────────┤
  Multi-AZ          │                    │        │  Glacier Deep      │
  Almost Never      │  (not economical — │        │  Archive           │
  Accessed          │   at this frequency│        │  ($0.00099)        │
                    │   delayed is fine) │        │  (12-48 hours)     │
                    └────────────────────┘        └────────────────────┘

  Plus: Intelligent-Tiering (automatic classification)
```

Each cell in this matrix represents a genuinely different customer need. Adding more
tiers would create marginal value with significant operational complexity.

---

## Footer

### Cross-References

- **Interview Simulation**: [interview-simulation.md](interview-simulation.md) ---
  Full S3 system design interview walkthrough covering architecture, consistency model,
  replication, and storage internals.

### Pricing Disclaimer

All prices referenced in this document are approximate and based on the **us-east-1**
(N. Virginia) region as of early 2025. Prices vary by region and may change over time.
Always consult the [official S3 pricing page](https://aws.amazon.com/s3/pricing/) for
current rates.

### Key Takeaways for Interview Preparation

1. **Know the seven storage classes** and their primary use cases. You don't need to
   memorize exact prices, but know the relative cost ordering and key tradeoffs.

2. **Understand the lifecycle evaluation engine**: it's async, batched, and processes
   within 24-48 hours. This is a great example of a design tradeoff (precision vs
   efficiency at scale).

3. **Be ready to discuss cost optimization**: lifecycle policies, Intelligent-Tiering,
   compression, and Storage Lens. Interviewers love candidates who think about cost.

4. **Know the gotchas**: minimum storage duration, minimum object size, restore-creates-
   a-copy, and one-way transition constraints. These show depth of understanding.

5. **Understand the physical differences**: Standard-to-IA is a billing change;
   Standard-to-Glacier is a physical data movement. This distinction demonstrates that
   you understand what happens under the hood, not just the API surface.

---

*This document is part of the Amazon S3 system design series. For the core architecture,*
*consistency model, and replication deep dive, see the [interview simulation](interview-simulation.md).*
