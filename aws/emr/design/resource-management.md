# EMR Resource Management (YARN) — Deep Dive

> **Companion doc for:** [interview-simulation.md](interview-simulation.md) — Phase 7 (Resource Management)
> **Last verified:** February 2026 against AWS EMR documentation

---

## Table of Contents

1. [Overview](#1-overview)
2. [YARN Architecture on EMR](#2-yarn-architecture-on-emr)
3. [Resource Model — Memory, VCores, Containers](#3-resource-model--memory-vcores-containers)
4. [How EMR Calculates Default Resources](#4-how-emr-calculates-default-resources)
5. [Container Lifecycle](#5-container-lifecycle)
6. [Schedulers — Capacity vs Fair](#6-schedulers--capacity-vs-fair)
7. [ApplicationMaster Placement](#7-applicationmaster-placement)
8. [Dynamic Resource Allocation (Spark)](#8-dynamic-resource-allocation-spark)
9. [Spark maximizeResourceAllocation](#9-spark-maximizeresourceallocation)
10. [Graceful Decommissioning](#10-graceful-decommissioning)
11. [YARN Node Labels on EMR](#11-yarn-node-labels-on-emr)
12. [Multi-Application Clusters — Resource Contention](#12-multi-application-clusters--resource-contention)
13. [YARN HA — ResourceManager Failover](#13-yarn-ha--resourcemanager-failover)
14. [Monitoring YARN Resources](#14-monitoring-yarn-resources)
15. [Configuration Reference](#15-configuration-reference)
16. [Design Decision Analysis](#16-design-decision-analysis)
17. [Interview Angles](#17-interview-angles)

---

## 1. Overview

YARN (Yet Another Resource Negotiator) is the resource management layer in every EMR cluster. It answers one fundamental question: **which application gets which resources on which node?**

In a world where multiple Spark jobs, Hive queries, and other frameworks compete for the same finite pool of memory and CPU, YARN is the referee.

### Why YARN Matters for an EMR Interview

YARN is where several EMR design tensions converge:
- **Multi-tenancy within a cluster** — multiple applications competing for containers
- **Spot instance resilience** — ApplicationMaster placement determines job survival
- **Scaling dynamics** — YARN metrics drive managed scaling decisions
- **Resource efficiency** — over-provisioning wastes money, under-provisioning causes queuing

### YARN in the EMR Stack

```
┌───────────────────────────────────────────────┐
│  Applications: Spark, Hive, Flink, Presto     │
│  (submit apps to YARN)                        │
├───────────────────────────────────────────────┤
│  YARN ResourceManager (primary node)          │
│  ├── Scheduler (capacity/fair)                │
│  ├── ApplicationMaster tracking               │
│  └── Node health monitoring                   │
├───────────────────────────────────────────────┤
│  YARN NodeManagers (core + task nodes)        │
│  ├── Container management                     │
│  ├── Resource tracking (memory + vcores)      │
│  └── Log aggregation                          │
├───────────────────────────────────────────────┤
│  HDFS / EMRFS / S3 (storage layer)            │
└───────────────────────────────────────────────┘
```

---

## 2. YARN Architecture on EMR

### Components

```
PRIMARY NODE
┌──────────────────────────────────────────────────────┐
│                 ResourceManager                       │
│                                                      │
│  ┌──────────────┐  ┌────────────────────────────┐    │
│  │  Scheduler    │  │  ApplicationMaster Manager  │   │
│  │              │  │                            │    │
│  │  Allocates   │  │  Tracks all running        │    │
│  │  containers  │  │  ApplicationMasters        │    │
│  │  based on    │  │  across the cluster        │    │
│  │  capacity/   │  │                            │    │
│  │  fairness    │  │  Launches AM containers    │    │
│  └──────────────┘  └────────────────────────────┘    │
│                                                      │
│  ┌──────────────────────────────────────────────┐    │
│  │  Node Tracker                                 │   │
│  │  Monitors heartbeats from all NodeManagers    │   │
│  │  Detects dead/decommissioned nodes            │   │
│  └──────────────────────────────────────────────┘    │
└───────────────────────┬──────────────────────────────┘
                        │
        ┌───────────────┼───────────────┐
        │               │               │
CORE NODE 1       CORE NODE 2       TASK NODE 1
┌──────────────┐  ┌──────────────┐  ┌──────────────┐
│ NodeManager  │  │ NodeManager  │  │ NodeManager  │
│              │  │              │  │              │
│ ┌──────────┐ │  │ ┌──────────┐ │  │ ┌──────────┐ │
│ │Container │ │  │ │Container │ │  │ │Container │ │
│ │(AM)      │ │  │ │(Executor)│ │  │ │(Executor)│ │
│ └──────────┘ │  │ └──────────┘ │  │ └──────────┘ │
│ ┌──────────┐ │  │ ┌──────────┐ │  │ ┌──────────┐ │
│ │Container │ │  │ │Container │ │  │ │Container │ │
│ │(Executor)│ │  │ │(Executor)│ │  │ │(Executor)│ │
│ └──────────┘ │  │ └──────────┘ │  │ └──────────┘ │
└──────────────┘  └──────────────┘  └──────────────┘
```

### Key Concepts

| Concept | Definition |
|---|---|
| **ResourceManager (RM)** | Central authority that allocates resources to applications. Runs on primary node(s). |
| **NodeManager (NM)** | Per-node agent that manages containers on that node. Reports resources to RM via heartbeats. |
| **ApplicationMaster (AM)** | Per-application process that negotiates resources from RM and coordinates task execution. E.g., Spark driver in YARN cluster mode. |
| **Container** | A bundle of resources (memory + vcores) allocated on a specific node. Executors run inside containers. |
| **Queue** | Logical partition of cluster resources. Applications are submitted to queues. |
| **Scheduler** | Plugin that decides how to allocate containers among competing applications (Capacity or Fair scheduler). |

---

## 3. Resource Model — Memory, VCores, Containers

### The Two Resources YARN Manages

YARN tracks two resources per node:

| Resource | What It Represents | How It's Set |
|---|---|---|
| **Memory (MB)** | Physical RAM available for containers | `yarn.nodemanager.resource.memory-mb` |
| **VCores** | Virtual CPU cores available for containers | `yarn.nodemanager.resource.cpu-vcores` |

### Container Allocation

When an application requests a container, it specifies:
- Memory (in MB) — must be a multiple of `yarn.scheduler.minimum-allocation-mb`
- VCores — must be a multiple of `yarn.scheduler.minimum-allocation-vcores`

YARN rounds up to the nearest multiple:
```
Requested: 2500 MB
Minimum allocation: 1024 MB
Allocated: 3072 MB (3 × 1024, rounded up)
```

### Node Resource Calculation

```
Total Instance RAM:  16,384 MB (m5.xlarge)
Reserved for OS:     ~4,096 MB (EMR reserves memory for system processes, HDFS, etc.)
YARN available:      12,288 MB (yarn.nodemanager.resource.memory-mb)

Total Instance vCPUs: 4
YARN available:       4 vcores (yarn.nodemanager.resource.cpu-vcores)
```

### Container Count Per Node

```
Containers per node = min(
    yarn.nodemanager.resource.memory-mb / container_memory_mb,
    yarn.nodemanager.resource.cpu-vcores / container_vcores
)

Example (m5.xlarge):
    = min(12288 / 3072, 4 / 1) = min(4, 4) = 4 containers
```

---

## 4. How EMR Calculates Default Resources

EMR automatically sets YARN and MapReduce memory parameters based on the EC2 instance type. This is one of EMR's key value-adds — you don't need to manually calculate JVM heap sizes.

### Default Values by Instance Type

| Instance Type | vCPUs | RAM (GB) | YARN Memory (MB) | Map Memory (MB) | Reduce Memory (MB) | Map JVM Heap | Reduce JVM Heap |
|---|---|---|---|---|---|---|---|
| **c5.xlarge** | 4 | 8 | 6,144 | 1,536 | 3,072 | -Xmx1229m | -Xmx2458m |
| **m5.xlarge** | 4 | 16 | 12,288 | 1,536 | 3,072 | -Xmx1229m | -Xmx2458m |
| **m5.2xlarge** | 8 | 32 | 24,576 | 3,072 | 6,144 | -Xmx2458m | -Xmx4916m |
| **r5.2xlarge** | 8 | 64 | 53,248 | 6,656 | 13,312 | -Xmx5325m | -Xmx10650m |
| **d2.xlarge** | 4 | 30.5 | 23,424 | 2,928 | 5,856 | -Xmx2342m | -Xmx4685m |

### Resource Allocation Formula [INFERRED]

Based on documented values, EMR follows this general pattern:

```
1. Total YARN Memory = Instance RAM - OS/system reserved (typically 20-25% of RAM)
2. Container Memory  = YARN Memory / number of containers
3. JVM Heap           ≈ Container Memory × 0.8 (accounts for JVM overhead)
```

The 80% JVM heap rule accounts for:
- JVM metaspace
- Thread stacks
- Direct memory buffers
- GC overhead

### HBase Impact on Resource Allocation

When HBase is installed, YARN gets less memory because HBase RegionServer consumes a significant portion:

| Setting | Without HBase | With HBase (c1.xlarge example) |
|---|---|---|
| `yarn.scheduler.maximum-allocation-mb` | 2,048 | 2,560 |
| `yarn.nodemanager.resource.memory-mb` | 5,120 | 2,560 |

**Rule**: If running HBase on the cluster, expect ~50% less YARN memory per node.

### JVM Reuse

EMR sets `mapred.job.jvm.num.tasks` to **20** by default (regardless of instance type). This means a single JVM runs up to 20 map/reduce tasks before being replaced, avoiding JVM startup overhead for short-lived tasks.

---

## 5. Container Lifecycle

### From Request to Execution

```
1. Application submits           2. RM schedules              3. NM launches
   resource request                 container                     container

┌────────────┐         ┌──────────────────┐         ┌──────────────────┐
│ AppMaster  │────────▶│ ResourceManager   │────────▶│ NodeManager      │
│            │ Request │                  │ Allocate│                  │
│ "Need 4GB, │ 4GB,   │ Checks queue     │ on      │ Creates cgroup   │
│  2 vcores" │ 2cores │ capacity, finds  │ Node X  │ Starts JVM       │
│            │        │ available node   │         │ Runs task/exec   │
└────────────┘         └──────────────────┘         └──────────────────┘
```

### Container States

```
NEW → ALLOCATED → ACQUIRED → RUNNING → COMPLETE
                                    └→ KILLED (preempted or AM requested kill)
                                    └→ FAILED (task failure, OOM, etc.)
```

### What Happens Inside a Container

For a **Spark executor container**:
```
Container (e.g., 4096 MB, 2 vcores)
┌──────────────────────────────────────────┐
│  JVM Process                              │
│  ├── Heap: 3276 MB (~80% of container)   │
│  │   ├── Execution memory (shuffle, join) │
│  │   └── Storage memory (cached RDDs)     │
│  ├── Off-heap: ~512 MB                    │
│  │   ├── JVM metaspace                    │
│  │   ├── Direct buffers (Netty)           │
│  │   └── Thread stacks                    │
│  └── Overhead: ~308 MB (YARN overhead)    │
│                                           │
│  Threads: task threads (up to 2 cores)    │
│  Network: shuffle data, broadcast vars    │
└──────────────────────────────────────────┘
```

### Container Failure Handling

| Failure | Detection | Recovery |
|---|---|---|
| **OOM killed** | Kernel OOM killer → NM detects exit | AM requests replacement container from RM |
| **Task exception** | JVM exit code ≠ 0 → NM reports to RM | AM retries task (up to `spark.task.maxFailures`, default 4) |
| **Node failure** | NM heartbeat timeout → RM marks node LOST | All containers on that node marked FAILED; AM requests replacements |
| **Spot reclamation** | 2-min warning → node lost | Same as node failure; YARN reschedules on surviving nodes |
| **Preemption** | Scheduler takes container for higher-priority app | AM requests replacement; preempted container killed with signal |

---

## 6. Schedulers — Capacity vs Fair

### Capacity Scheduler (EMR Default)

The Capacity Scheduler divides cluster resources into **queues** with guaranteed minimum capacity:

```
Total Cluster: 100% capacity
│
├── default queue: 100% (single queue by default on EMR)
│   ├── Application 1 (Spark job)
│   ├── Application 2 (Hive query)
│   └── Application 3 (Spark job)
```

**Key Properties:**

| Property | Value | Meaning |
|---|---|---|
| **Queue capacity** | Percentage of cluster | Guaranteed minimum for each queue |
| **Maximum capacity** | Percentage of cluster | Maximum a queue can use when others are idle |
| **Elasticity** | Queue can use idle capacity from other queues | Resources returned when the owning queue needs them |
| **Ordering** | FIFO within a queue (or fair with `intra-queue-preemption`) | Applications within a queue are served in submission order |
| **Preemption** | Optional — can be enabled for inter-queue preemption | Higher-priority queues can reclaim resources from lower-priority ones |

**When to Use Capacity Scheduler:**
- Multi-tenant clusters with SLA requirements
- When different teams need guaranteed resource shares
- Batch workloads where FIFO ordering within a queue is acceptable

### Fair Scheduler

The Fair Scheduler gives every application an equal share of resources:

```
Total Cluster: 100% capacity
│
├── App 1: 33.3% (auto-calculated)
├── App 2: 33.3% (auto-calculated)
└── App 3: 33.3% (auto-calculated)

If App 2 finishes:
├── App 1: 50%
└── App 3: 50%
```

**Key Properties:**

| Property | Value | Meaning |
|---|---|---|
| **Fairness** | Resources divided equally among running apps | No starvation — every app gets some resources |
| **Preemption** | Enabled by default | Apps that exceed their fair share may have containers killed |
| **Min share** | Per-queue minimum | Guaranteed minimum, regardless of other queues |
| **Weights** | Per-queue weights | Unequal sharing (e.g., queue A gets 2× resources of queue B) |
| **Steady Fair Share** | Based on weights even when some queues are empty | Prevents queue starvation when queues are added/removed |

**When to Use Fair Scheduler:**
- Interactive workloads where all queries should get responsive resource allocation
- Hive/Presto clusters with many concurrent short queries
- When you want to avoid long queue wait times

### Comparison Table

| Dimension | Capacity Scheduler | Fair Scheduler |
|---|---|---|
| **Default on EMR** | Yes | No (must configure) |
| **Resource guarantee** | Per-queue guaranteed capacity | Fair share (equal or weighted) |
| **Ordering within queue** | FIFO (default) | Fair (equal share) |
| **Preemption** | Optional, inter-queue | Default, inter-app |
| **Best for** | Batch processing, multi-tenant SLAs | Interactive queries, mixed workloads |
| **Complexity** | Need to define queue hierarchy | Simpler — auto-calculates shares |
| **Starvation risk** | Yes — small apps wait behind large ones in same queue | No — every app gets fair share |

### Configuration on EMR

**Capacity Scheduler (default):**
```json
[
  {
    "Classification": "capacity-scheduler",
    "Properties": {
      "yarn.scheduler.capacity.root.queues": "default,production,dev",
      "yarn.scheduler.capacity.root.production.capacity": "60",
      "yarn.scheduler.capacity.root.dev.capacity": "20",
      "yarn.scheduler.capacity.root.default.capacity": "20"
    }
  }
]
```

**Fair Scheduler:**
```json
[
  {
    "Classification": "yarn-site",
    "Properties": {
      "yarn.resourcemanager.scheduler.class": "org.apache.hadoop.yarn.server.resourcemanager.scheduler.fair.FairScheduler"
    }
  }
]
```

---

## 7. ApplicationMaster Placement

### Why AM Placement Is Critical

The ApplicationMaster (AM) is the coordinator for each YARN application. For Spark, it's the driver in cluster mode. If the AM dies, the entire application fails — all executor containers become orphaned and are killed.

### The Spot Instance Problem

```
WRONG: AM on Spot task node
  → Spot reclaimed → AM killed → All executors orphaned → Application FAILS

RIGHT: AM on On-Demand core node
  → Spot task nodes reclaimed → Only executors lost → AM reschedules on survivors
```

### YARN Node Labels Solution

EMR uses YARN node labels to restrict AM placement:

```
Primary node: no labels (no YARN containers typically)
Core nodes:   CORE label (On-Demand)
Task nodes:   no CORE label (Spot)

AM scheduling: yarn.node-labels.am.default-node-label-expression = 'CORE'
→ AMs placed ONLY on CORE-labeled nodes
→ Spot reclamation cannot kill AMs
```

### AM Resource Requirements

The AM itself consumes a container's worth of resources:

| Framework | AM Memory (Default) | AM VCores |
|---|---|---|
| **Spark (cluster mode)** | `spark.driver.memory` + overhead | `spark.driver.cores` |
| **MapReduce** | `yarn.app.mapreduce.am.resource.mb` (typically 3072 MB) | 1 |
| **Hive on Tez** | `tez.am.resource.memory.mb` | 1 |
| **Flink** | `jobmanager.memory.process.size` | 1 |

For a cluster running many concurrent applications, AM overhead adds up. 20 concurrent Spark jobs with 3 GB AM each = 60 GB of memory dedicated just to AMs.

---

## 8. Dynamic Resource Allocation (Spark)

### What It Is

Spark's dynamic allocation automatically adjusts the number of executors based on workload:

```
Stage 1: Heavy computation    → Scale up to 100 executors
Stage 2: Small shuffle read   → Scale down to 20 executors
Stage 3: Heavy computation    → Scale back up to 80 executors

Without dynamic allocation: all 100 executors allocated for entire job duration
With dynamic allocation: executors released when idle, requested when needed
```

### Configuration on EMR

Dynamic allocation is **enabled by default** on EMR 4.4.0+:

```
spark.dynamicAllocation.enabled = true
spark.shuffle.service.enabled = true  (required for dynamic allocation)
```

The external shuffle service is critical because:
- Without it, when an executor is removed, its shuffle data is lost
- The external shuffle service persists shuffle data independently of executors
- EMR automatically configures this as a YARN auxiliary service

### Key Dynamic Allocation Parameters

| Parameter | Default on EMR | Meaning |
|---|---|---|
| `spark.dynamicAllocation.enabled` | `true` | Enable dynamic allocation |
| `spark.dynamicAllocation.minExecutors` | `0` | Minimum executors to maintain |
| `spark.dynamicAllocation.maxExecutors` | `∞` | Maximum executors (bounded by cluster capacity) |
| `spark.dynamicAllocation.initialExecutors` | `0` | Initial number of executors |
| `spark.dynamicAllocation.executorIdleTimeout` | `60s` | Remove executor after this idle time |
| `spark.dynamicAllocation.schedulerBacklogTimeout` | `1s` | Request more executors if tasks pending for this long |
| `spark.dynamicAllocation.sustainedSchedulerBacklogTimeout` | `1s` | Continue requesting executors at this interval |
| `spark.shuffle.service.enabled` | `true` | External shuffle service (required) |

### How Dynamic Allocation Interacts with YARN

```
Spark Job Running (dynamic allocation ON)
│
├── Pending tasks in scheduler backlog (> 1 second)
│   └── Spark requests more executor containers from YARN
│       └── YARN allocates containers on available nodes
│           └── Spark starts executors in those containers
│
├── Executors idle for > 60 seconds
│   └── Spark releases executor containers back to YARN
│       └── Shuffle data preserved by external shuffle service
│       └── YARN can use those resources for other applications
│
└── Stage completes
    └── Executors become idle → eventually released
```

---

## 9. Spark maximizeResourceAllocation

### What It Does

EMR's `maximizeResourceAllocation` is a convenience flag that automatically calculates optimal Spark executor settings to use all available cluster resources:

```json
[
  {
    "Classification": "spark",
    "Properties": {
      "maximizeResourceAllocation": "true"
    }
  }
]
```

### What It Configures

| Setting | Value When Enabled |
|---|---|
| `spark.default.parallelism` | 2 × total YARN vcores in the cluster |
| `spark.driver.memory` | Based on the smaller instance type (primary or core node) |
| `spark.executor.memory` | Based on core and task instance types |
| `spark.executor.cores` | Based on core and task instance types |
| `spark.executor.instances` | Based on cluster instance count (unless dynamic allocation is on) |

### When to Use vs. Not Use

| Use Case | maximizeResourceAllocation | Manual Configuration |
|---|---|---|
| **Single-application cluster** | Yes — use all resources for one Spark job | No |
| **Multi-application cluster** | No — one Spark job would starve others | Yes — set resource limits per app |
| **Transient job cluster** | Yes — cluster exists for one job | No |
| **Long-running interactive cluster** | No — need to share resources | Yes — use queues + limits |

### Caveat with Dynamic Allocation

When both `maximizeResourceAllocation` and `spark.dynamicAllocation.enabled` are true (which is the default), the `spark.executor.instances` setting is ignored in favor of dynamic allocation. Spark will still scale up to use all cluster resources if needed, but it will also scale down when idle.

---

## 10. Graceful Decommissioning

### The Problem

When EMR scales down (removes nodes), it must handle:
1. **Running containers** — killing them would fail tasks and waste work
2. **HDFS data** — removing a DataNode risks data loss if blocks aren't replicated elsewhere
3. **Shuffle data** — removing a node with shuffle files forces re-computation of map stages

### The Solution: Graceful Decommissioning

EMR gracefully decommissions YARN, HDFS, and other daemons before terminating instances:

```
Scale-down request received
│
├── YARN NodeManager Decommissioning
│   ├── Stop assigning new containers to this node
│   ├── Wait for existing containers to complete
│   ├── Timeout: 3600 seconds (1 hour) default
│   └── After timeout: force decommission
│       (YARN reschedules affected containers)
│
├── HDFS DataNode Decommissioning (core nodes only)
│   ├── Stop accepting new block writes
│   ├── Replicate all blocks to other DataNodes
│   ├── Wait until all blocks are safely replicated
│   └── Only then: DataNode shuts down
│
└── Instance Termination
    └── EC2 instance terminated after both
        YARN and HDFS decommissioning complete
```

### Key Configuration

| Parameter | Default | Meaning |
|---|---|---|
| `yarn.resourcemanager.nodemanager-graceful-decommission-timeout-secs` (EMR 5.12.0+) | 3600 (1 hour) | Max time to wait for containers to finish before force decommission |
| `yarn.resourcemanager.decommissioning.timeout` (EMR < 5.12.0) | 3600 | Same, older parameter name |

### Task Nodes vs Core Nodes

| Node Type | Decommission Speed | What Must Complete |
|---|---|---|
| **Task node** | Fast — no HDFS data to migrate | Only wait for running containers (or timeout) |
| **Core node** | Slow — must replicate HDFS blocks | Wait for containers AND HDFS block replication |

### HDFS Replication Factor Limits on Scale-Down

The number of core nodes cannot go below the HDFS replication factor:

| Replication Factor | Min Core Nodes | Cluster Size Where This Applies |
|---|---|---|
| `dfs.replication = 1` | 1 | Clusters with 1-3 instances (default for small clusters) |
| `dfs.replication = 2` | 2 | Clusters with 4-9 instances |
| `dfs.replication = 3` | 3 | Clusters with 10+ instances |

If you try to scale core nodes below the replication factor, HDFS cannot fully replicate some blocks, so EMR will only partially decommission nodes.

### Warning: Direct Instance Termination

Using `modify-instance-groups` with `EC2InstanceIdsToTerminate` **terminates instances immediately** without graceful decommissioning. This can cause:
- HDFS data loss
- Task failures
- Unpredictable cluster behavior

Always use the standard scaling mechanism (modify target count) instead of direct instance termination.

---

## 11. YARN Node Labels on EMR

Node labels are EMR's mechanism to control container placement based on node type and instance purchasing model.

### Label Assignment by EMR Version

| EMR Version | Labels | AM Default Placement |
|---|---|---|
| **5.19.0 - 5.x** | `CORE` on core nodes | AMs on `CORE` nodes only |
| **6.x** | Disabled by default | AMs can run anywhere (must manually enable) |
| **7.x** | `ON_DEMAND` and `SPOT` by market type | AMs on `ON_DEMAND` nodes only |
| **7.2+** | Market-type labels + managed scaling awareness | Independent scaling for AM capacity vs executor capacity |

### How Labels Interact with Scheduling

```
Capacity Scheduler with Node Labels:

Partition: CORE (On-Demand core nodes)
├── ApplicationMasters allocated here
└── Guaranteed AM resources

Partition: <default> (all nodes without labels)
├── Executor containers
└── All node types contribute
```

### EMR 7.x Innovation: Market-Type Labels

Instead of labeling by node role (core/task), EMR 7.x labels by **purchase type**:

```
ON_DEMAND label: All On-Demand instances (core and task)
SPOT label: All Spot instances (core and task)

AM placement: ON_DEMAND → survives Spot reclamation
```

This is more flexible because:
- On-Demand task nodes can also host AMs (not just core)
- Spot core nodes won't get AMs (even though they're "core")
- Better reflects the actual risk factor (Spot vs On-Demand, not core vs task)

---

## 12. Multi-Application Clusters — Resource Contention

### The Problem

A long-running EMR cluster often runs multiple concurrent applications:
- Hive queries from analysts
- Spark ETL jobs from data engineers
- Presto interactive queries
- HBase serving real-time reads

Without resource management, one greedy Spark job could consume all cluster resources and starve all other applications.

### Solution: Queue-Based Resource Isolation

```
Cluster Resources: 200 GB RAM, 80 vcores
│
├── production queue (60% = 120 GB, 48 vcores)
│   ├── Nightly Spark ETL (80 GB allocated)
│   └── Available: 40 GB
│
├── interactive queue (30% = 60 GB, 24 vcores)
│   ├── Hive query #1 (10 GB)
│   ├── Presto query #2 (15 GB)
│   └── Available: 35 GB
│
└── dev queue (10% = 20 GB, 8 vcores)
    ├── Test Spark job (5 GB)
    └── Available: 15 GB
```

### Application Submission to Queues

```bash
# Submit Spark job to production queue
spark-submit --queue production --class MyJob myjar.jar

# Submit Hive query to interactive queue
hive --hiveconf mapreduce.job.queuename=interactive -f query.sql
```

### Resource Contention Patterns

| Pattern | Problem | Solution |
|---|---|---|
| **One app hogs all resources** | Other apps can't get containers | Set `spark.dynamicAllocation.maxExecutors` per app; use queue capacity limits |
| **Many small apps, each with AM overhead** | AM containers consume significant memory | Minimize concurrent apps; use Spark Thrift Server for SQL queries |
| **AM on Spot node killed** | Application fails entirely | Use node labels to place AMs on On-Demand/CORE nodes |
| **Container OOM kills** | Tasks fail, YARN kills container | Increase `spark.executor.memory` or `mapreduce.map.memory.mb` |
| **Queue starvation** | Low-priority queue never gets resources | Enable preemption; set minimum share guarantees |

---

## 13. YARN HA — ResourceManager Failover

### Architecture

In multi-primary EMR clusters (3 primary nodes), YARN runs with HA:

```
Primary #1:  Active ResourceManager
Primary #2:  Standby ResourceManager
Primary #3:  Standby ResourceManager

ZooKeeper (on all 3 primary nodes) → detects failure, triggers failover
```

### Failover Behavior

| Event | What Happens |
|---|---|
| **Active RM fails** | ZooKeeper detects heartbeat loss; one standby promoted to active |
| **During failover** | Running containers continue executing; new container requests queued briefly |
| **Client redirect** | RM web UI at port 8088 auto-redirects to the new active RM |
| **Recovery** | New active RM reconstructs state from NodeManager heartbeats and application reports |

### Check RM Status

```bash
# From any primary node
yarn rmadmin -getAllServiceState
```

Output:
```
primary1:8032  active
primary2:8032  standby
primary3:8032  standby
```

---

## 14. Monitoring YARN Resources

### Key YARN Metrics

| Metric | What It Tells You | Alert Threshold |
|---|---|---|
| **Available Memory (MB)** | Free memory across all NodeManagers | < 10% of total → cluster is memory-constrained |
| **Available VCores** | Free vcores across all NodeManagers | < 10% → CPU-constrained |
| **Pending Containers** | Containers waiting for allocation | > 0 for extended periods → need more nodes |
| **Running Containers** | Active containers across cluster | Baseline metric for cluster utilization |
| **App State: ACCEPTED** | Apps waiting for AM container | > 0 → AM can't be placed (check node labels, queue capacity) |
| **App State: RUNNING** | Active applications | High count may indicate resource contention |
| **Decommissioning Nodes** | Nodes in graceful decommission | Indicates scaling activity |

### YARN Web UI

The ResourceManager web UI at `http://primary-node:8088/cluster` shows:
- Cluster metrics (memory, vcores, containers)
- Running and completed applications
- Node statuses
- Queue resource allocation
- Application logs (aggregated)

### CloudWatch Integration

EMR publishes YARN metrics to CloudWatch:
- `YARNMemoryAvailablePercentage` — available memory as percentage of total
- `ContainerPendingRatio` — pending / (pending + allocated) containers
- These metrics drive **managed scaling** decisions

---

## 15. Configuration Reference

### Essential YARN Properties

| Property | Default on EMR | Purpose |
|---|---|---|
| `yarn.nodemanager.resource.memory-mb` | Auto-calculated per instance type | Total memory available to YARN on each node |
| `yarn.nodemanager.resource.cpu-vcores` | Instance vCPU count | Total vcores available on each node |
| `yarn.scheduler.minimum-allocation-mb` | 1 (MB) | Smallest container memory unit |
| `yarn.scheduler.maximum-allocation-mb` | Auto-calculated | Largest single container |
| `yarn.scheduler.minimum-allocation-vcores` | 1 | Smallest vcore allocation |
| `yarn.scheduler.maximum-allocation-vcores` | Auto-calculated | Largest vcore allocation |
| `yarn.nodemanager.vmem-check-enabled` | `false` (EMR default) | Disable virtual memory check (prevents spurious container kills) |
| `yarn.log-aggregation-enable` | `true` | Aggregate container logs to S3 or HDFS |

### Essential Spark-on-YARN Properties

| Property | Default on EMR | Purpose |
|---|---|---|
| `spark.executor.memory` | Auto-calculated | Executor JVM heap size |
| `spark.executor.cores` | Auto-calculated | Cores per executor |
| `spark.executor.instances` | Dynamic | Number of executors (dynamic allocation overrides) |
| `spark.driver.memory` | Auto-calculated | Driver (AM) memory |
| `spark.dynamicAllocation.enabled` | `true` | Scale executors up/down |
| `spark.shuffle.service.enabled` | `true` | External shuffle service |
| `spark.default.parallelism` | 2 × total vcores | Default partition count for RDDs |

### Essential MapReduce Properties

| Property | Default on EMR | Purpose |
|---|---|---|
| `mapreduce.map.memory.mb` | Auto-calculated per instance type | Map container memory |
| `mapreduce.reduce.memory.mb` | Auto-calculated (typically 2× map) | Reduce container memory |
| `mapreduce.map.java.opts` | Auto-calculated (~80% of container) | Map JVM heap |
| `mapreduce.reduce.java.opts` | Auto-calculated (~80% of container) | Reduce JVM heap |
| `mapred.job.jvm.num.tasks` | 20 | Tasks per JVM (JVM reuse) |

---

## 16. Design Decision Analysis

### Decision 1: Why YARN Instead of Custom Container Orchestration?

| Alternative | Pros | Cons |
|---|---|---|
| **Custom AWS orchestrator** | Optimized for EMR, full control, cloud-native | Must reinvent resource negotiation; Spark/Hive won't run natively |
| **YARN** ← EMR's choice | Industry standard, native Spark/Hive/Flink integration, community support | Designed for on-prem (pre-cloud), limited GPU support, no native Spot awareness |
| **Kubernetes** | Modern, GPU-native, rich ecosystem, cloud-native | Heavy overhead, YARN-to-K8s translation needed for existing frameworks |

**Why YARN**: Spark, Hive, Flink, and HBase are all built to run on YARN. Using YARN means EMR can run unmodified open-source frameworks. The EMR-specific optimizations (node labels, managed scaling, decommissioning) are layered on top of YARN, not replacements for it.

### Decision 2: Why Default to Capacity Scheduler?

| Alternative | Pros | Cons |
|---|---|---|
| **Capacity Scheduler** ← EMR default | Simple single-queue setup works for most clusters; guaranteed capacity for multi-tenant | FIFO within queue can starve small apps |
| **Fair Scheduler** | Better for interactive workloads; no starvation | More complex configuration; preemption overhead |

**Why Capacity**: Most EMR clusters run a single application type (Spark ETL or Hive queries). The default single-queue Capacity Scheduler works fine. Customers who need multi-tenant isolation can configure queues. The Fair Scheduler is better for interactive Hive/Presto clusters but adds configuration complexity.

### Decision 3: Dynamic Allocation by Default

| Alternative | Pros | Cons |
|---|---|---|
| **Static allocation** | Predictable resource usage; simpler debugging | Wasteful — executors idle between stages; must know cluster size at job submission |
| **Dynamic allocation** ← EMR default | Efficient — releases idle executors; adapts to workload | Requires external shuffle service; scaling lag between stages |

**Why dynamic by default**: EMR customers run diverse workloads with varying resource needs. Dynamic allocation automatically right-sizes executor count, preventing waste. The external shuffle service (also default on EMR) ensures shuffle data survives executor removal.

---

## 17. Interview Angles

### Questions an Interviewer Might Ask

**Resource Model:**
- "How does YARN decide where to place a container?"
  - Answer: The scheduler (Capacity or Fair) maintains a view of each node's available memory and vcores. When an AM requests a container, the scheduler finds a node with sufficient free resources. It considers queue capacity, node labels, and data locality preferences.

- "What happens when a container runs out of memory?"
  - Answer: The Linux kernel's OOM killer terminates the process. YARN's NodeManager detects the non-zero exit and reports the container as FAILED. The ApplicationMaster can retry the task (Spark retries up to `spark.task.maxFailures` times, default 4).

**Scheduling:**
- "When would you choose Fair Scheduler over Capacity Scheduler?"
  - Answer: Fair Scheduler for interactive workloads (Hive/Presto clusters) where many short queries run concurrently and none should starve. Capacity Scheduler for batch-oriented clusters where you want guaranteed resource allocation per team/project.

**Graceful Decommissioning:**
- "What's the difference between scaling down task nodes vs core nodes?"
  - Answer: Task nodes are fast — only wait for running containers (1-hour timeout). Core nodes are slow — must wait for containers AND replicate all HDFS blocks to surviving DataNodes. The HDFS replication factor sets the minimum core node count.

**Dynamic Allocation:**
- "Why does Spark on EMR need an external shuffle service?"
  - Answer: Without it, dynamic allocation can't remove executors safely — their shuffle data would be lost, forcing re-computation of map stages. The external shuffle service runs as a YARN auxiliary service that persists shuffle files independently of executor lifetime.

**Multi-Tenancy:**
- "How do you prevent one Spark job from starving other applications?"
  - Answer: Three mechanisms: (1) YARN queues with capacity limits, (2) `spark.dynamicAllocation.maxExecutors` per application, (3) Scheduler preemption to reclaim resources from over-allocated queues. For Spark SQL workloads, Spark Thrift Server can share one Spark context across multiple SQL sessions, reducing AM overhead.

### Red Flags in Candidate Answers

| Red Flag | Why It's Wrong |
|---|---|
| "YARN manages only memory" | YARN manages both memory and vcores |
| "Just increase memory if containers get OOM-killed" | May need to fix application code (e.g., reduce broadcast variable size, increase partitions) |
| "ApplicationMaster is a minor overhead" | AM for 20 concurrent Spark jobs = 60+ GB of memory; it's significant |
| "Scaling down is instant" | Core nodes require HDFS block replication; 1-hour YARN decommission timeout |
| "Fair scheduler is always better" | Preemption overhead and complexity; Capacity is simpler and fine for single-app clusters |
| "Disable dynamic allocation for predictability" | Wastes resources; dynamic allocation is EMR default for good reason |
