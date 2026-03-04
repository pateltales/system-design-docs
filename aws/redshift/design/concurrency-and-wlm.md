# Deep Dive: Concurrency, Workload Management & Scaling in Amazon Redshift

> Companion document to [interview-simulation.md](./interview-simulation.md)

---

## Table of Contents

1. [The Concurrency Challenge in Data Warehouses](#1-the-concurrency-challenge-in-data-warehouses)
2. [Workload Management (WLM)](#2-workload-management-wlm)
3. [Manual WLM vs Automatic WLM](#3-manual-wlm-vs-automatic-wlm)
4. [Short Query Acceleration (SQA)](#4-short-query-acceleration-sqa)
5. [Concurrency Scaling](#5-concurrency-scaling)
6. [RA3 Managed Storage: Enabling Elastic Compute](#6-ra3-managed-storage-enabling-elastic-compute)
7. [Redshift Serverless](#7-redshift-serverless)
8. [Node Types: RA3 vs DC2](#8-node-types-ra3-vs-dc2)
9. [Monitoring and Diagnostics](#9-monitoring-and-diagnostics)
10. [Design Decisions & Tradeoffs](#10-design-decisions--tradeoffs)

---

## 1. The Concurrency Challenge in Data Warehouses

### Why Concurrency Is Hard in MPP

Unlike OLTP databases that handle millions of simple queries, a data warehouse handles fewer but heavier queries. Each analytical query may:
- Scan billions of rows across all compute nodes
- Use 100% of available I/O bandwidth for seconds to minutes
- Require gigabytes of working memory for hash joins and aggregations
- Redistribute data across the network between nodes

**The tension**: More concurrent queries → each gets fewer resources → more disk spilling → each query is slower → total throughput may actually **decrease** with higher concurrency.

```
Cluster resources (fixed):
  16 nodes × 96 GB RAM = 1,536 GB total compute memory
  16 nodes × 4 slices = 64 slices of parallelism

5 concurrent queries:
  Each gets: ~300 GB memory, full I/O for 1/5 of the time
  Per-query latency: 5 seconds

50 concurrent queries:
  Each gets: ~30 GB memory → hash tables spill to disk
  Per-query latency: 60 seconds (12x slower per query)
  Total throughput: 50/60 = 0.83 queries/sec vs 5/5 = 1 query/sec
  WORSE throughput with MORE concurrency!
```

This is why WLM exists — to control concurrency and prevent resource starvation.

---

## 2. Workload Management (WLM)

### Architecture

```
Incoming queries from clients
         │
         ▼
┌────────────────────────────────────────────────┐
│              WLM Router                          │
│                                                  │
│  Assigns queries to queues based on:            │
│  - User or user group                            │
│  - Query group (set by client)                   │
│  - Query characteristics                         │
│                                                  │
│  Assignment rules evaluated top-to-bottom        │
│  First matching rule wins                        │
└──────┬───────────┬───────────┬─────────────────┘
       │           │           │
       ▼           ▼           ▼
┌───────────┐ ┌───────────┐ ┌───────────┐ ┌───────────┐
│ Superuser │ │ Queue 1   │ │ Queue 2   │ │ Default   │
│ Queue     │ │ (Priority)│ │ (ETL)     │ │ Queue     │
│           │ │           │ │           │ │           │
│ Slots: 1  │ │ Slots: 5  │ │ Slots: 3  │ │ Slots: 10 │
│ Mem: n/a  │ │ Mem: 40%  │ │ Mem: 30%  │ │ Mem: 30%  │
│           │ │           │ │           │ │           │
│ Reserved  │ │ Timeout:  │ │ Timeout:  │ │ Timeout:  │
│ for admin │ │ 300 sec   │ │ 3600 sec  │ │ 600 sec   │
└───────────┘ └───────────┘ └───────────┘ └───────────┘
```

### Queue Properties

| Property | Description |
|---|---|
| **Concurrency level (slots)** | Max number of queries running simultaneously in this queue. Maximum 50 across all user queues (manual WLM). |
| **Memory percentage** | Percentage of total cluster memory allocated to this queue. Memory is divided equally among slots. |
| **Timeout** | Max runtime for a query in this queue. Queries exceeding timeout are cancelled (WLM timeout action). |
| **User groups** | Queries from these database users are routed to this queue. |
| **Query groups** | Queries with `SET query_group TO 'group_name'` are routed to this queue. |
| **Priority** | In automatic WLM: Highest, High, Normal, Low, Lowest. |

### The Superuser Queue

A **reserved queue** with 1 slot, always available:
- Only accessible to superusers
- Cannot be disabled or modified
- Ensures admins can always run diagnostic queries even when all user queues are full
- Critical for troubleshooting: when the cluster is overloaded, you need the superuser queue to run `SELECT * FROM stv_inflight` to see what's running

### Queue Assignment Rules

WLM evaluates rules in order:

```
1. Is the user a superuser running in superuser mode? → Superuser Queue
2. Check user-defined queue rules (top to bottom):
   a. User = 'etl_service' → ETL Queue
   b. User group includes 'analysts' → Analyst Queue
   c. Query group = 'priority' → Priority Queue
3. No rule matched → Default Queue
```

Clients can set their query group:
```sql
SET query_group TO 'etl';
-- all subsequent queries go to the ETL queue

RESET query_group;
-- back to default routing
```

---

## 3. Manual WLM vs Automatic WLM

### Manual WLM

You explicitly configure:
- Number of queues
- Slots per queue
- Memory per queue
- Timeout per queue

```
Example: Manual WLM Configuration

Queue 1 (Priority): 5 slots, 40% memory → each slot gets 8% memory
Queue 2 (ETL):      3 slots, 30% memory → each slot gets 10% memory
Queue 3 (Default):  10 slots, 30% memory → each slot gets 3% memory
                    ──────
                    18 slots total (max 50)
```

**The problem with manual WLM:**
- Slot memory is **statically divided**. A simple `SELECT COUNT(*)` gets the same 8% memory as a complex 10-table join.
- If you set high concurrency (20 slots), each slot gets only 5% of memory. Complex queries spill to disk. Everything slows down.
- If you set low concurrency (3 slots), simple queries wait behind complex ones even though they don't need many resources.

### Automatic WLM (Recommended)

Redshift dynamically manages memory and concurrency:

```
Automatic WLM:
  - No fixed slots (Redshift decides how many queries run concurrently)
  - Memory allocated based on query complexity
  - Simple queries get less memory → more can run concurrently
  - Complex queries get more memory → fewer run concurrently but don't spill to disk
  - Priority levels (Highest/High/Normal/Low/Lowest) instead of fixed queues
```

**How it works:**

```
Query A: Simple COUNT(*) → predicted small memory need
  → Allocated 2% of memory → runs alongside many other queries

Query B: Complex 5-table JOIN with GROUP BY → predicted large memory need
  → Allocated 30% of memory → fewer concurrent queries allowed

Redshift dynamically adjusts allocation based on:
  - Query execution plan complexity
  - Estimated memory requirements
  - Current cluster utilization
  - Query priority level
```

**Priority levels in automatic WLM:**

| Priority | Behavior |
|---|---|
| **Highest** | Preempts lower-priority queries for resources |
| **High** | Gets resources before Normal/Low/Lowest |
| **Normal** | Default priority |
| **Low** | Only gets resources after higher priorities are served |
| **Lowest** | Background — runs when cluster is otherwise idle |

### When to Use Manual vs Automatic

| Scenario | Recommendation |
|---|---|
| New cluster, standard workloads | **Automatic WLM** (default, simplest) |
| Need fine-grained slot/memory control | Manual WLM |
| Mix of quick dashboards + long ETL | **Automatic WLM** with priority levels |
| Very predictable, fixed workload | Manual WLM (optimized for known patterns) |

---

## 4. Short Query Acceleration (SQA)

### How SQA Works with WLM

SQA is a layer **on top of** WLM that identifies short-running queries and routes them to a fast lane, bypassing the regular queue.

```
Query arrives
     │
     ▼
┌─────────────────────────────┐
│ SQA Prediction               │
│                               │
│ ML model estimates runtime:  │
│ - Compile time               │
│ - Number of segments         │
│ - Table sizes                │
│ - Historical patterns        │
│                               │
│ Estimated runtime < threshold │
├─────────────┬────────────────┤
│ SHORT       │ LONG           │
│ → SQA lane  │ → WLM queue    │
│ (immediate) │ (may wait)     │
└─────────────┴────────────────┘
```

**SQA properties:**
- Enabled by default with automatic WLM
- The threshold is dynamically determined (typically 5-10 seconds)
- SQA uses a separate pool of resources that doesn't compete with WLM slots
- If the prediction is wrong (query runs longer than expected), the query is migrated to the appropriate WLM queue

**Impact:**

| Without SQA | With SQA |
|---|---|
| Dashboard query (0.5s) waits 30s in queue behind ETL | Dashboard query (0.5s) runs immediately in SQA lane |
| P99 latency for dashboards: 45 seconds | P99 latency for dashboards: 2 seconds |
| Analysts experience inconsistent response times | Dashboards are consistently snappy |

---

## 5. Concurrency Scaling

### The Problem

A cluster has fixed resources. During peak hours (9 AM when analysts arrive, month-end reporting), queries queue up:

```
Normal: 10 queries/min → all served by 10 WLM slots → no waiting
Peak:   50 queries/min → 10 WLM slots → 40 queries waiting → growing queue
```

Adding more nodes permanently is expensive for peak-only traffic.

### How Concurrency Scaling Works

```
┌─────────────────────────────────┐
│         Main Cluster             │
│   (always running, 16 nodes)    │
│                                  │
│   WLM queues saturated →        │
│   queries begin to wait         │
│                                  │
│   Trigger: queries queuing for  │
│   concurrency-scaling-eligible  │
│   queries                        │
└────────────┬────────────────────┘
             │ Overflow
             ▼
┌─────────────────────────────────┐
│  Concurrency Scaling Cluster 1   │
│  (transient, auto-provisioned)  │
│                                  │
│  Same node type as main cluster │
│  Reads from Redshift Managed    │
│  Storage (same data)            │
│                                  │
│  Handles overflow read queries  │
└─────────────────────────────────┘
             │ Still overflowing?
             ▼
┌─────────────────────────────────┐
│  Concurrency Scaling Cluster 2   │
│  (another transient cluster)    │
└─────────────────────────────────┘
     ... up to max_concurrency_scaling_clusters (default 10)
```

### Eligibility

**Eligible queries:**
- Read queries (SELECT)
- Write queries (INSERT, UPDATE, DELETE, COPY) — **only on RA3 node types**
- Queries using the result cache

**NOT eligible:**
- Queries on tables with **interleaved sort keys**
- Queries referencing **temporary tables**
- Queries using **Python UDFs**
- Queries that require maintenance operations (VACUUM, ANALYZE)

### Pricing

| Component | Cost |
|---|---|
| **Free credits** | 1 hour of concurrency scaling per day for each active cluster in your account |
| **Beyond free credits** | Per-second billing at on-demand rates for your node type |
| **Accumulation** | Unused free credits accumulate for up to 30 days |

**Example cost calculation:**

```
Main cluster: 4 × ra3.4xlarge nodes
Peak hours: 3 hours/day need concurrency scaling
Free credits: 1 hour/day
Billable: 2 hours/day × ~$13.04/hr (4 nodes × $3.26/hr on-demand) = ~$26/day extra

vs. Permanently adding 4 more nodes:
4 × ra3.4xlarge × 24 hours × $3.26/hr = ~$313/day
```

Concurrency scaling is 12x cheaper for 3-hour daily peaks.

### Configuration

```sql
-- Set maximum number of concurrency scaling clusters
ALTER CLUSTER SET max_concurrency_scaling_clusters = 5;

-- Enable concurrency scaling for a WLM queue
-- (in automatic WLM, set via parameter group)
-- The queue's concurrency_scaling mode must be 'auto'
```

### Cold Start Concern

When a concurrency scaling cluster spins up, its local SSD cache is **cold**. Initial queries hit S3 (Redshift Managed Storage) instead of local cache, adding latency at the worst time — during peak load.

[INFERRED — AWS does not document the exact cold-start latency for concurrency scaling clusters. In practice, cache warms quickly for hot data as queries execute.]

---

## 6. RA3 Managed Storage: Enabling Elastic Compute

### Architecture

RA3 nodes separate compute from storage by using **Redshift Managed Storage (RMS)**:

```
┌──────────────────────────────┐
│         RA3 Node              │
│                                │
│  ┌─────────────────────────┐  │
│  │    Compute Layer         │  │
│  │    (vCPU + RAM)          │  │
│  └─────────────────────────┘  │
│                                │
│  ┌─────────────────────────┐  │
│  │    Local SSD Cache       │  │
│  │    (Automatic tiering)   │  │
│  │                          │  │
│  │    Hot blocks cached     │  │
│  │    LRU eviction          │  │
│  └────────────┬────────────┘  │
│               │ Cache miss     │
└───────────────┼───────────────┘
                │
                ▼
┌──────────────────────────────┐
│   Redshift Managed Storage    │
│   (S3-backed)                 │
│                                │
│   ┌─────────────────────────┐ │
│   │  All table data          │ │
│   │  (columnar blocks)       │ │
│   │  11 9's durability       │ │
│   │  Cross-AZ replication    │ │
│   └─────────────────────────┘ │
│                                │
│   Accessible by:              │
│   - Main cluster nodes        │
│   - Concurrency scaling nodes │
│   - Data sharing consumers    │
│   - Restore operations        │
└──────────────────────────────┘
```

### Why RMS Enables Elastic Compute

Because data lives in S3 (not on local disks), **compute nodes are stateless with respect to durable data**:

1. **Concurrency scaling**: Transient clusters can read the same data from RMS
2. **Cluster resize**: Add/remove nodes without moving data between nodes (only cache redistribution)
3. **Data sharing**: Consumer clusters read producer's data from RMS
4. **Node replacement**: Failed node replaced, cache rebuilds from RMS automatically
5. **Snapshots**: Since data is already in S3, snapshots are metadata operations (not full copies)

### RA3 Node Specifications

| Node Type | vCPU | Memory | Slices per Node | Max Nodes per Cluster | Managed Storage per Node |
|---|---|---|---|---|---|
| **ra3.xlplus** | 4 | 32 GB | 2 | 32 | 32 TB |
| **ra3.4xlarge** | 12 | 96 GB | 4 | 128 | 128 TB |
| **ra3.16xlarge** | 48 | 384 GB | 16 | 128 | 128 TB |

### Managed Storage Pricing

Managed storage is billed separately from compute:
- Compute: per-node-hour (on-demand or reserved)
- Storage: per-GB-month for data stored in RMS

This separation means:
- Scale compute up/down without paying for more storage
- Store petabytes affordably (S3-backed)
- Only pay for active compute when queries run

---

## 7. Redshift Serverless

### Core Concepts

| Concept | Definition |
|---|---|
| **Namespace** | Storage-side: databases, schemas, tables, users, encryption keys, datashares |
| **Workgroup** | Compute-side: RPUs, VPC config, security groups, access/usage limits |

Relationship: One namespace ↔ one workgroup (1:1 mapping).

### RPUs (Redshift Processing Units)

RPUs are the compute capacity unit for Serverless:
- Base capacity: minimum RPUs allocated (minimum 8, increments of 8)
- Auto-scaling: Serverless automatically scales RPUs up during heavy workloads
- Billing: per-RPU-second, only when compute is active

### When Serverless vs Provisioned

| Factor | Provisioned Cluster | Redshift Serverless |
|---|---|---|
| **Workload pattern** | Steady, predictable | Variable, bursty, intermittent |
| **Management** | You choose nodes, configure WLM, manage snapshots | Zero management |
| **Cost model** | Per-node-hour (reserved: up to 77% discount) | Per-RPU-second (only when querying) |
| **Cost for 24/7 steady workload** | **Cheaper** (especially with reserved instances) | More expensive |
| **Cost for 4 hrs/day sporadic use** | Pay for idle 20 hrs | **Cheaper** (only pay 4 hrs) |
| **Max connections** | 2,000 (RA3) | 2,000 |
| **Concurrency scaling** | Supported | Auto-scaled natively (no separate feature needed) |
| **Data sharing** | Supported | Supported |
| **Max workgroups per account** | N/A | 25 |

### Cost Comparison Example

```
Workload: 100 RPU-hours per day

Serverless:
  100 RPU-hours × $0.375/RPU-hour = $37.50/day

Provisioned (equivalent — 4 × ra3.4xlarge):
  On-demand: 4 × $3.26/hr × 24 hrs = $313/day (but unused 75% of time)
  If idle 75% of time, effective: $313/day for same work as $37.50/day

  Reserved (1-year): 4 × $1.50/hr × 24 hrs = $144/day
  Still expensive if workload is sporadic
```

For sporadic workloads, Serverless is dramatically cheaper. For 24/7 workloads, reserved provisioned instances win.

---

## 8. Node Types: RA3 vs DC2

### Detailed Comparison

| Spec | ra3.xlplus | ra3.4xlarge | ra3.16xlarge | dc2.large | dc2.8xlarge |
|---|---|---|---|---|---|
| **vCPU** | 4 | 12 | 48 | 2 | 32 |
| **Memory** | 32 GB | 96 GB | 384 GB | 15 GB | 244 GB |
| **Slices** | 2 | 4 | 16 | 2 | 16 |
| **Max Nodes** | 32 | 128 | 128 | 128 | 128 |
| **Storage** | RMS (32 TB/node) | RMS (128 TB/node) | RMS (128 TB/node) | 160 GB NVMe | 2.56 TB NVMe |
| **Max Connections** | 2,000 | 2,000 | 2,000 | 500 | 2,000 |
| **Concurrency Scaling** | Yes | Yes | Yes | No | No |
| **Data Sharing** | Yes | Yes | Yes | No | No |
| **Compute-Storage Separation** | Yes | Yes | Yes | No | No |

### DS2 (Discontinued)

DS2 nodes are no longer available for new clusters. Existing DS2 clusters should migrate to RA3.

### Decision Framework

```
Is your data < 500 GB and ALL hot (frequently accessed)?
├── Yes → DC2 (dc2.large for < 160 GB, dc2.8xlarge for more)
│         Lowest latency, all data on local NVMe
└── No
    ├── Do you need concurrency scaling, data sharing, or resize flexibility?
    │   └── Yes → RA3
    ├── Is your data > 2 TB?
    │   └── Yes → RA3 (DC2 storage is limited)
    └── Otherwise → RA3 (better default for most workloads)
```

---

## 9. Monitoring and Diagnostics

### Key System Tables and Views

| Table/View | What It Shows |
|---|---|
| `STL_QUERY` | Completed queries with execution time, queue, aborted status |
| `STL_WLM_QUERY` | WLM queue assignment, wait time, execution time per query |
| `STV_INFLIGHT` | Currently running queries |
| `STV_WLM_SERVICE_CLASS_CONFIG` | WLM queue configuration |
| `STV_WLM_SERVICE_CLASS_STATE` | Current state of WLM queues (slots used, queries queued) |
| `SVL_QUERY_SUMMARY` | Step-by-step execution details per query |
| `SVL_QUERY_METRICS_SUMMARY` | Resource usage per query (memory, disk, CPU, network) |
| `STL_ALERT_EVENT_LOG` | Performance alerts (distribution skew, missing stats, etc.) |
| `SVV_TABLE_INFO` | Table size, distribution, sort key, unsorted percentage |
| `STV_BLOCKLIST` | Block-level storage distribution across slices |
| `STL_DISK_FULL_DIAG` | Disk full diagnostics |

### Common Diagnostic Queries

```sql
-- Top 10 longest-running queries in the last 24 hours
SELECT query, userid, querytxt, starttime, endtime,
       DATEDIFF(seconds, starttime, endtime) as duration_sec
FROM STL_QUERY
WHERE starttime > DATEADD(hour, -24, GETDATE())
ORDER BY duration_sec DESC
LIMIT 10;

-- Queries that spent the most time waiting in WLM queue
SELECT query, service_class, queue_start_time, queue_end_time,
       DATEDIFF(ms, queue_start_time, queue_end_time) as queue_wait_ms
FROM STL_WLM_QUERY
WHERE queue_start_time > DATEADD(hour, -24, GETDATE())
ORDER BY queue_wait_ms DESC
LIMIT 10;

-- Tables with high unsorted percentage (need VACUUM)
SELECT "table", size, tbl_rows, unsorted, stats_off
FROM SVV_TABLE_INFO
WHERE unsorted > 10 OR stats_off > 10
ORDER BY unsorted DESC;

-- Data skew across slices for a specific table
SELECT trim(name) as tablename, slice, COUNT(*) as blocks
FROM stv_blocklist b
JOIN stv_tbl_perm p ON b.tbl = p.id
WHERE name = 'orders'
GROUP BY name, slice
ORDER BY blocks DESC;

-- Current WLM queue utilization
SELECT service_class, num_queued_queries, num_executing_queries
FROM STV_WLM_SERVICE_CLASS_STATE;
```

---

## 10. Design Decisions & Tradeoffs

### Fixed Cluster vs Elastic Compute

| Approach | Provisioned + Concurrency Scaling | Fully Serverless |
|---|---|---|
| Base cost | Fixed (node-hours) | Zero when idle |
| Burst handling | Concurrency scaling clusters (seconds to start) | RPU auto-scaling |
| Cold start | Concurrency scaling: cold cache | Serverless: cold cache + cold compute |
| Cost predictability | **Predictable** (reserved instances) | Variable (depends on workload) |
| Management | You choose nodes, manage WLM | Zero management |
| Performance control | Full control over node types, WLM tuning | Limited control (RPU-based) |

### Concurrency vs Per-Query Performance

```
The fundamental tradeoff:

High concurrency (many slots):
  + More queries run simultaneously
  - Less memory per query → disk spilling
  - Each query is slower
  - Risk: all queries slow → nobody is happy

Low concurrency (few slots):
  + Each query gets more resources → faster
  - Queries wait in queue
  - Risk: long queue waits → users frustrated

Automatic WLM tries to find the optimal balance dynamically.
```

### WLM Priority vs Fair Scheduling

| Approach | When to Use |
|---|---|
| **Priority-based** (Automatic WLM with priorities) | When some workloads are genuinely more important (real-time dashboards > ad-hoc exploration) |
| **Fair scheduling** (equal priority for all) | When all workloads are equally important or you can't categorize them |
| **Dedicated queues** (Manual WLM with user routing) | When workloads have fundamentally different resource profiles (quick BI vs long ETL) |

### The Scale-Up vs Scale-Out Tradeoff

| Strategy | Approach | Best For |
|---|---|---|
| **Scale up** | Larger node type (dc2.large → dc2.8xlarge) | More memory per query, fewer nodes to manage |
| **Scale out** | More nodes (4 → 16 ra3.4xlarge) | More parallelism, more aggregate resources |
| **Elastic** | Concurrency scaling + right-sized base cluster | Variable workloads with predictable base + unpredictable peaks |

In practice, most production Redshift deployments use a combination:
1. Right-size the base cluster for average workload (scale-up/scale-out)
2. Enable concurrency scaling for peaks (elastic)
3. Use SQA to protect interactive queries from batch ETL (priority)
