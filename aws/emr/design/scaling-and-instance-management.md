# EMR Scaling and Instance Management — Deep Dive

> **Companion doc for:** [interview-simulation.md](interview-simulation.md) — Phase 8 (Managed Scaling) & Phase 9 (Instance Fleets)
> **Last verified:** February 2026 against AWS EMR documentation

---

## Table of Contents

1. [Overview](#1-overview)
2. [Instance Groups vs Instance Fleets](#2-instance-groups-vs-instance-fleets)
3. [Instance Groups — Uniform Configuration](#3-instance-groups--uniform-configuration)
4. [Instance Fleets — Flexible Configuration](#4-instance-fleets--flexible-configuration)
5. [Spot Instance Integration](#5-spot-instance-integration)
6. [Spot Allocation Strategies](#6-spot-allocation-strategies)
7. [Managed Scaling](#7-managed-scaling)
8. [Managed Scaling — How Decisions Are Made](#8-managed-scaling--how-decisions-are-made)
9. [Managed Scaling — Configuration](#9-managed-scaling--configuration)
10. [Managed Scaling — Node Labels Integration](#10-managed-scaling--node-labels-integration)
11. [Scale-Up vs Scale-Down Dynamics](#11-scale-up-vs-scale-down-dynamics)
12. [Custom Auto Scaling (Legacy)](#12-custom-auto-scaling-legacy)
13. [Capacity Planning Patterns](#13-capacity-planning-patterns)
14. [Failure Modes and Troubleshooting](#14-failure-modes-and-troubleshooting)
15. [Design Decision Analysis](#15-design-decision-analysis)
16. [Interview Angles](#16-interview-angles)

---

## 1. Overview

EMR's instance and scaling architecture addresses a fundamental tension in distributed computing: **you can't predict resource demand at cluster creation time**, but provisioning too much wastes money and too little causes job failures or excessive queuing.

EMR solves this with two complementary mechanisms:
1. **Instance configuration** — how you specify which hardware to use (instance groups vs fleets)
2. **Managed scaling** — how EMR automatically adjusts cluster size based on workload

### The Cost Optimization Equation

```
Total EMR Cost = EC2 Instance Hours × Instance Price + EMR Fee

Optimization levers:
├── Instance type selection  → right-size for workload (memory vs compute)
├── Spot vs On-Demand mix    → 60-90% savings on Spot instances
├── Scaling efficiency       → don't pay for idle capacity
└── Instance fleet diversity → maximize Spot fulfillment rate
```

---

## 2. Instance Groups vs Instance Fleets

The first decision when creating an EMR cluster: which provisioning model to use.

### Comparison Table

| Dimension | Instance Groups | Instance Fleets |
|---|---|---|
| **Instance types per node type** | 1 per group (but up to 48 task groups) | Up to 30 per fleet (CLI/API), 5-15 per fleet (console) |
| **Purchasing model** | Either On-Demand or Spot per group | Mixed On-Demand + Spot in same fleet |
| **Capacity specification** | Number of instances | Target capacity in weighted units (vCPUs or custom) |
| **Spot allocation strategy** | Bid price per group | price-capacity-optimized, capacity-optimized, diversified, lowest-price |
| **Multi-AZ** | No — single subnet | Yes — specify multiple subnets, EMR picks best AZ |
| **Max task groups/fleets** | 48 task instance groups | 1 task instance fleet |
| **Total groups/fleets** | Up to 50 per cluster | 3 (primary + core + task) |
| **Max instances per fleet** | N/A | 4,000 (EMR 7.7.0+), 2,000 (earlier) |
| **Spot timeout/fallback** | No automatic fallback | Timeout → switch to On-Demand or terminate |
| **Complexity** | Simpler | More complex but more flexible |
| **Managed scaling** | Supported | Supported |
| **HA (multi-primary)** | EMR 5.23.0+ | EMR 5.36.1+, 6.8.1+, 6.12.0+ |

### When to Use Which

| Scenario | Recommendation | Why |
|---|---|---|
| **Simple, predictable workload** | Instance groups | Simpler configuration, one instance type is fine |
| **Cost-optimized production** | Instance fleets | Spot diversification, multi-AZ, weighted capacity |
| **Spot-heavy clusters** | Instance fleets | Better fulfillment with multiple instance types and allocation strategies |
| **Many different instance types for tasks** | Instance groups (up to 48) | Can specify different instance types across groups |
| **Multi-AZ requirement** | Instance fleets | Only fleets support multi-subnet |
| **Quick prototyping** | Instance groups | Faster to set up |

---

## 3. Instance Groups — Uniform Configuration

### Structure

```
EMR Cluster (Instance Groups)
│
├── Primary Instance Group
│   └── 1 instance (always exactly 1, or 3 for HA)
│       └── m5.xlarge (On-Demand)
│
├── Core Instance Group
│   └── 10 instances (all same type)
│       └── r5.2xlarge (On-Demand)
│
├── Task Instance Group #1
│   └── 20 instances
│       └── c5.4xlarge (Spot, bid $0.50)
│
├── Task Instance Group #2
│   └── 15 instances
│       └── m5.4xlarge (Spot, bid $0.60)
│
└── Task Instance Group #3
    └── 10 instances
        └── r5.4xlarge (Spot, bid $0.80)

Total: 56 instances across 5 groups
```

### Key Properties

| Property | Value |
|---|---|
| **Max instance groups** | 50 per cluster (1 primary + 1 core + up to 48 task) |
| **Instance type per group** | Exactly 1 (homogeneous within each group) |
| **Purchasing per group** | Either On-Demand or Spot (not mixed) |
| **Scaling** | Add/remove instances within a group; add/remove task groups |
| **Primary group** | Cannot be modified after creation |
| **Core group** | Can scale up/down (with graceful decommissioning) |

### Spot Diversification with Instance Groups

To achieve Spot diversification, create multiple task instance groups with different instance types:

```
Task Group #1: c5.4xlarge  Spot  (20 instances)
Task Group #2: c5a.4xlarge Spot  (20 instances)
Task Group #3: m5.4xlarge  Spot  (20 instances)
Task Group #4: m5a.4xlarge Spot  (20 instances)
Task Group #5: r5.4xlarge  Spot  (20 instances)
```

This spreads Spot requests across 5 capacity pools, reducing the risk of a single pool exhaustion causing widespread interruption.

---

## 4. Instance Fleets — Flexible Configuration

### Structure

```
EMR Cluster (Instance Fleets)
│
├── Primary Fleet
│   └── TargetOnDemandCapacity: 1
│       └── Instance types: [m5.xlarge, m5a.xlarge, m4.xlarge]
│           └── EMR picks lowest-price available type
│
├── Core Fleet
│   ├── TargetOnDemandCapacity: 10 (units)
│   └── TargetSpotCapacity: 20 (units)
│       └── Instance types: [
│              r5.2xlarge  (WeightedCapacity: 8),
│              r5.4xlarge  (WeightedCapacity: 16),
│              r5a.2xlarge (WeightedCapacity: 8),
│              m5.4xlarge  (WeightedCapacity: 16)
│           ]
│       └── Allocation: price-capacity-optimized
│
└── Task Fleet
    ├── TargetOnDemandCapacity: 0
    └── TargetSpotCapacity: 100 (units)
        └── Instance types: [
               c5.4xlarge   (WeightedCapacity: 16),
               c5.9xlarge   (WeightedCapacity: 36),
               c5a.4xlarge  (WeightedCapacity: 16),
               m5.4xlarge   (WeightedCapacity: 16),
               m5.8xlarge   (WeightedCapacity: 32),
               r5.4xlarge   (WeightedCapacity: 16),
               ... up to 30 types
            ]
        └── Allocation: price-capacity-optimized
```

### Key Properties

| Property | Value |
|---|---|
| **Fleet count** | Exactly 3 per cluster (primary + core + task) |
| **Instance types per fleet** | Up to 30 (CLI/API with allocation strategy) |
| **Console limit** | Primary/Core: 5 types, Task: 15 types |
| **Target capacity units** | vCPUs (console default) or custom weighted units (CLI) |
| **Max instances per fleet** | 4,000 (EMR 7.7.0+), 2,000 (earlier) |
| **Max EBS volumes per fleet** | 14,000 (EMR 7.7.0+), 7,000 (earlier) |
| **Multi-AZ** | Yes — specify multiple subnets; EMR picks one AZ |
| **On-Demand + Spot mix** | Within the same fleet |

### Weighted Capacity

Weighted capacity lets you define the "compute unit" each instance type provides:

```
Target Spot Capacity: 100 units

Options:
  m5.xlarge   → WeightedCapacity: 4  → need 25 instances to fill 100 units
  m5.2xlarge  → WeightedCapacity: 8  → need 13 instances to fill 100 units
  m5.4xlarge  → WeightedCapacity: 16 → need 7 instances to fill 100 units
```

EMR selects the optimal mix based on the allocation strategy (price, capacity, or both).

**Overage**: If the remaining capacity is less than the smallest instance type's weight, EMR may provision an extra instance, exceeding the target. For example, with 4 units remaining and the smallest option being 8 units, EMR launches one more instance, overshooting by 4 units.

### Spot Timeout and Fallback

Instance fleets support automatic fallback when Spot capacity is unavailable:

```json
{
  "SpotSpecification": {
    "TimeoutDurationMinutes": 120,
    "TimeoutAction": "SWITCH_TO_ON_DEMAND"
  }
}
```

| Timeout Action | Behavior |
|---|---|
| `SWITCH_TO_ON_DEMAND` | If Spot not fulfilled within timeout, provision On-Demand instead (only during initial cluster creation) |
| `TERMINATE_CLUSTER` | If Spot not fulfilled within timeout, terminate the cluster |

**Limitation**: The switch-to-on-demand fallback only works during initial cluster provisioning. If the timeout expires during a resize operation, unfulfilled Spot requests are simply cancelled.

### Multi-AZ Support

Instance fleets can specify multiple subnets (each in a different AZ):

```json
{
  "Ec2SubnetIds": ["subnet-abc123", "subnet-def456", "subnet-ghi789"]
}
```

EMR selects the best AZ based on:
- Instance type availability
- Spot capacity in each AZ
- Subnet IP address availability

**Important**: All instances always launch in a **single AZ** — EMR doesn't spread across AZs within a cluster (HDFS requires low-latency intra-cluster networking).

---

## 5. Spot Instance Integration

### Why Spot Matters for EMR

Big data workloads are among the best use cases for Spot Instances because:
- **Batch processing is interruptible** — failed tasks can be retried
- **YARN handles failures gracefully** — containers are rescheduled on surviving nodes
- **EMR's node type separation** — task nodes (Spot) don't store HDFS data

### Spot Savings

| Node Type | Typical Savings | Risk |
|---|---|---|
| **Task nodes (Spot)** | 60-90% vs On-Demand | Low — only executor containers lost; no data loss |
| **Core nodes (Spot)** | 60-90% vs On-Demand | Medium — HDFS data loss risk if many core nodes interrupted |
| **Primary node (Spot)** | 60-90% vs On-Demand | High — cluster failure on interruption (single-primary) |

### Recommended Configuration

```
Primary:  On-Demand (always)
Core:     On-Demand (production), Spot with caution (dev/test)
Task:     Spot (always, with diversification)
```

### Spot Interruption Handling

When a Spot instance is reclaimed:

```
EC2 sends 2-minute interruption notice
        │
        ▼
EMR receives notice
        │
        ▼
YARN NodeManager begins graceful shutdown
├── Stop accepting new containers
├── Notify ApplicationMasters of lost containers
└── Containers on this node marked FAILED
        │
        ▼
After 2 minutes: instance terminated
        │
        ▼
EMR requests replacement Spot capacity
├── Same instance type (instance groups)
└── Any configured type (instance fleets → better fulfillment)
```

---

## 6. Spot Allocation Strategies

Instance fleets support multiple Spot allocation strategies, each optimizing for different objectives:

### Strategy Comparison

| Strategy | Optimizes For | Interruption Risk | Best For |
|---|---|---|---|
| **price-capacity-optimized** (recommended, default EMR 6.10.0+) | Balance of price and capacity | Lowest | Most workloads |
| **capacity-optimized** (default EMR ≤ 6.9.0) | Deepest available capacity pool | Low | Workloads sensitive to interruption |
| **capacity-optimized-prioritized** | Capacity first, then user priority | Low | When you prefer specific instance types |
| **diversified** | Spread across all pools equally | Medium | Hedging against single-pool exhaustion |
| **lowest-price** | Cheapest available pool | Highest | Cost-insensitive batch with high retry tolerance |

### price-capacity-optimized (Recommended)

```
How it works:
1. Identify capacity pools with enough available instances
2. Among those pools, select the lowest-priced option
3. Result: Low price AND low interruption probability

Why it's better than lowest-price alone:
- lowest-price picks the cheapest pool, but that pool may have thin capacity
  → high interruption risk
- price-capacity-optimized avoids thin pools even if they're cheaper
  → better sustained workload execution
```

### On-Demand Allocation Strategies

| Strategy | Behavior |
|---|---|
| **lowest-price** (default) | Provisions the cheapest available On-Demand instance type |
| **prioritized** | Provisions based on user-defined priority values per instance type |

### Capacity Reservations

Instance fleets support EC2 Capacity Reservations for guaranteed On-Demand capacity:

| Reservation Type | How It Works |
|---|---|
| **Open** | Automatically matched to fleet requests; first-come-first-served |
| **Targeted** | Explicitly associated with the fleet via resource groups |

---

## 7. Managed Scaling

### Overview

Managed scaling (EMR 5.30.0+) is EMR's built-in auto-scaling that automatically adjusts cluster size based on YARN workload metrics.

```
┌──────────────────────────────────────────────────┐
│                EMR Control Plane                  │
│                                                  │
│  ┌──────────────────────────────────────────┐    │
│  │        Managed Scaling Service            │   │
│  │                                          │    │
│  │  1. Collect YARN metrics (CloudWatch)    │    │
│  │  2. Evaluate demand vs capacity          │    │
│  │  3. Decide: scale up, scale down, or     │    │
│  │     hold                                 │    │
│  │  4. Issue resize to cluster              │    │
│  └──────────────┬───────────────────────────┘    │
│                 │                                 │
└─────────────────┼─────────────────────────────────┘
                  │
                  ▼
         ┌────────────────┐
         │  EMR Cluster    │
         │                │
         │  Core: 5 → 8  │  (scale up)
         │  Task: 10 → 25│  (scale up)
         │                │
         └────────────────┘
```

### Supported Applications

Managed scaling works with **YARN-based applications only**:
- Spark
- Hadoop MapReduce
- Hive (on Tez or MapReduce)
- Flink

**NOT supported**: Presto/Trino (MPP engine, not YARN-based), HBase (always-on service, not workload-driven).

### Key Properties

| Property | Details |
|---|---|
| **Available since** | EMR 5.30.0+ (except 6.0.0) |
| **Scaling targets** | Core nodes and task nodes only (primary never scales) |
| **Metrics source** | CloudWatch (published by EMR metrics collector) |
| **Decision frequency** | Continuous evaluation [INFERRED — specific interval not documented] |
| **Scale-up speed** | Minutes (limited by EC2 provisioning time) |
| **Scale-down speed** | Slower (graceful decommissioning: up to 1 hour default timeout) |

---

## 8. Managed Scaling — How Decisions Are Made

### Scale-Up Triggers

Managed scaling adds capacity when:

1. **YARN containers are pending** — applications have requested containers but no resources are available
2. **Executor demand exceeds capacity** — Spark dynamic allocation wants more executors than the cluster can provide
3. **Application processing is backlogged** — tasks are queued, waiting for compute

```
Scale-Up Decision:

  Pending containers > 0 for sustained period
  AND available YARN memory < threshold
  AND current instances < MaximumCapacityUnits

  → Add instances (task nodes first, then core if needed)
```

### Scale-Down Triggers

Managed scaling removes capacity when:

1. **YARN resources are underutilized** — many containers completed, executors idle
2. **No shuffle data on the node** — Spark shuffle-aware scale-down checks if the node holds active shuffle files
3. **No ApplicationMaster on the node** — won't remove nodes running AMs

```
Scale-Down Decision:

  Available YARN memory > threshold for sustained period
  AND no active shuffle data on candidate nodes [INFERRED]
  AND no ApplicationMasters on candidate nodes
  AND current instances > MinimumCapacityUnits

  → Remove underutilized nodes (task nodes first, then core)
  → Graceful decommissioning: wait for containers to finish
```

### Scale-Down Intelligence

Managed scaling has become increasingly sophisticated:

| Feature | Since | Behavior |
|---|---|---|
| **Basic YARN metrics** | EMR 5.30.0 | Scale based on pending containers and available memory |
| **AM node awareness** | June 2023 | Won't scale down nodes running ApplicationMasters |
| **Spark shuffle awareness** | March 2022 | Won't scale down nodes with active shuffle data |
| **Instance group switching** | July 2023 | If Spot capacity unavailable in one task group, try another |
| **Node label awareness** | August 2024 (EMR 7.2.0+) | Scale On-Demand and Spot independently based on AM demand |

---

## 9. Managed Scaling — Configuration

### Parameters

| Parameter | Type | Description |
|---|---|---|
| **MinimumCapacityUnits** | Required | Minimum cluster size (lower bound) |
| **MaximumCapacityUnits** | Required | Maximum cluster size (upper bound) |
| **MaximumOnDemandCapacityUnits** | Optional | Max On-Demand instances; remainder is Spot. Defaults to MaximumCapacityUnits |
| **MaximumCoreCapacityUnits** | Optional | Max core nodes; remainder is task nodes. Defaults to MaximumCapacityUnits |

### Example Configuration

```json
{
  "ComputeLimits": {
    "UnitType": "Instances",
    "MinimumCapacityUnits": 2,
    "MaximumCapacityUnits": 100,
    "MaximumOnDemandCapacityUnits": 10,
    "MaximumCoreCapacityUnits": 17
  }
}
```

**Result**:
```
Minimum:  2 instances
Maximum:  100 instances

On-Demand: up to 10 instances → rest are Spot (up to 90)
Core:      up to 17 nodes     → rest are task (up to 83)
```

### Unit Types

| Unit Type | Measures | Used With |
|---|---|---|
| **Instances** | Number of EC2 instances | Instance groups |
| **VCPU** | Total vCPU count | Either |
| **InstanceFleetUnits** | Weighted capacity units | Instance fleets |

### CLI Example

```bash
aws emr create-cluster \
  --release-label emr-7.12.0 \
  --applications Name=Spark \
  --managed-scaling-policy '{
    "ComputeLimits": {
      "UnitType": "Instances",
      "MinimumCapacityUnits": 2,
      "MaximumCapacityUnits": 100,
      "MaximumOnDemandCapacityUnits": 10,
      "MaximumCoreCapacityUnits": 17
    }
  }' \
  --instance-groups \
    InstanceGroupType=MASTER,InstanceCount=1,InstanceType=m5.xlarge \
    InstanceGroupType=CORE,InstanceCount=4,InstanceType=r5.2xlarge
```

---

## 10. Managed Scaling — Node Labels Integration

### EMR 7.2.0+ Feature

Managed scaling with node labels independently tracks demand for:
- **ApplicationMaster containers** → must run on `ON_DEMAND` or `CORE` labeled nodes
- **Executor containers** → can run on any node (including Spot)

This means managed scaling can:
1. Scale up On-Demand nodes specifically for AM demand
2. Scale up Spot nodes specifically for executor demand
3. Avoid scaling down On-Demand nodes when AMs are running
4. Aggressively scale down Spot nodes when executors finish

```
Example:

3 Spark jobs running:
  → 3 ApplicationMasters need On-Demand nodes (3 × 4 GB = 12 GB AM demand)
  → 150 executors need any nodes (150 × 8 GB = 1200 GB executor demand)

Managed scaling with node labels:
  On-Demand: scale to 3 nodes (sufficient for 3 AMs)
  Spot:      scale to 20 nodes (sufficient for 150 executors)
```

### Configuration

```properties
# Enable node labels (EMR 7.x)
yarn.node-labels.enabled=true
yarn.node-labels.am.default-node-label-expression='ON_DEMAND'

# Set max AM resource percent to 100% (for managed scaling)
yarn.scheduler.capacity.maximum-am-resource-percent=1
```

**Important constraint**: Cannot place both AMs and executors exclusively on core or ON_DEMAND nodes when using managed scaling. The scaling algorithm needs flexibility to independently scale AM capacity vs executor capacity.

---

## 11. Scale-Up vs Scale-Down Dynamics

### Scale-Up

```
Trigger: Pending containers, YARN memory depleted

Timeline:
  t=0:    Managed scaling detects demand
  t=1m:   EC2 instances requested
  t=2-5m: Instances launched, AMI booted
  t=3-7m: Bootstrap actions run
  t=5-10m: YARN NodeManager registers with ResourceManager
  t=5-10m: Containers begin allocating on new nodes

Total: 5-10 minutes from demand to productive capacity
```

### Scale-Down

```
Trigger: Underutilized nodes, idle executors

Timeline:
  t=0:    Managed scaling identifies candidate nodes
  t=0:    Check: shuffle data? → skip if active
  t=0:    Check: ApplicationMaster? → skip if running AM
  t=0:    Begin graceful decommissioning
  t=0-60m: Wait for running containers to complete
  t=60m:  If still running → force decommission (default timeout)
  t=60m+: HDFS DataNode decommission (core nodes only)
           → replicate blocks to other nodes
  t=?:    Instance terminated

Total (task node): minutes to 1 hour
Total (core node): minutes to hours (HDFS replication delay)
```

### Asymmetry: Scale-Up Is Fast, Scale-Down Is Slow

This asymmetry is by design:
- **Scale-up** is urgent — applications are waiting for resources
- **Scale-down** must be careful — premature termination causes data loss or task failures

### Tuning Scale-Down Speed

For workloads where fast scale-down is acceptable (no shuffle data, no HDFS):

```properties
# Reduce Spark blacklist timeout from 1 hour to 1 minute
spark.blacklist.decommissioning.timeout=1m

# Reduce YARN decommission timeout
yarn.resourcemanager.nodemanager-graceful-decommission-timeout-secs=300
```

---

## 12. Custom Auto Scaling (Legacy)

Before managed scaling (EMR 5.30.0), EMR supported custom auto scaling using CloudWatch alarms:

### How It Worked

```
CloudWatch Alarm (e.g., YARNMemoryAvailablePercentage < 15%)
        │
        ▼
Auto Scaling Action (e.g., add 5 instances to task group)
        │
        ▼
EMR provisions instances
```

### Configuration

```json
{
  "AutoScalingPolicy": {
    "Rules": [
      {
        "Name": "ScaleOutOnYARNMemory",
        "Action": {
          "SimpleScalingPolicyConfiguration": {
            "AdjustmentType": "CHANGE_IN_CAPACITY",
            "ScalingAdjustment": 5,
            "CoolDown": 300
          }
        },
        "Trigger": {
          "CloudWatchAlarmDefinition": {
            "MetricName": "YARNMemoryAvailablePercentage",
            "ComparisonOperator": "LESS_THAN",
            "Threshold": 15,
            "Period": 300,
            "EvaluationPeriods": 1,
            "Statistic": "AVERAGE"
          }
        }
      }
    ]
  }
}
```

### Custom vs Managed Scaling

| Dimension | Custom Auto Scaling | Managed Scaling |
|---|---|---|
| **Configuration complexity** | High — define alarms, actions, thresholds, cooldowns | Low — just set min/max |
| **YARN awareness** | Indirect — via CloudWatch metrics | Direct — evaluates YARN demand, shuffle, AMs |
| **Shuffle awareness** | No | Yes (since March 2022) |
| **AM awareness** | No | Yes (since June 2023) |
| **Instance group switching** | No | Yes (since July 2023) |
| **Recommendation** | Legacy | Use managed scaling for all new clusters |

---

## 13. Capacity Planning Patterns

### Pattern 1: Transient Batch Job (Predictable Size)

```
Workload: Nightly ETL, 2 TB input, ~45 min runtime
Strategy: Fixed size, no scaling needed

Primary:  1 × m5.xlarge (On-Demand)
Core:     4 × r5.2xlarge (On-Demand) — minimal HDFS for shuffle
Task:     20 × c5.4xlarge (Spot, instance fleet with 10+ types)

Managed Scaling: OFF (cluster lifetime is short; scaling overhead not worth it)
Auto-terminate: ON (terminate after step completes)

Monthly cost: ~$5/run × 30 days = ~$150/month
```

### Pattern 2: Variable Batch Workload (Managed Scaling)

```
Workload: Hourly ETL, data volume varies 10x between peak and off-peak
Strategy: Managed scaling with wide range

Primary:  1 × m5.2xlarge (On-Demand)
Core:     4 × r5.2xlarge (On-Demand, fixed)

Managed Scaling:
  MinimumCapacityUnits: 4 (core nodes)
  MaximumCapacityUnits: 60
  MaximumOnDemandCapacityUnits: 8
  MaximumCoreCapacityUnits: 8

Task fleet: 10+ instance types, price-capacity-optimized

Off-peak: cluster runs with 4-8 nodes (min)
Peak:     scales to 50-60 nodes automatically
```

### Pattern 3: Long-Running Interactive Cluster

```
Workload: Presto/Hive analytics, 50+ concurrent users
Strategy: Large baseline with task node burst

Primary:  3 × m5.4xlarge (On-Demand, HA)
Core:     20 × r5.4xlarge (On-Demand) — Hive metastore + HDFS
Task:     0-100 × r5.4xlarge (Spot, managed scaling)

Managed Scaling:
  MinimumCapacityUnits: 20 (core nodes always running)
  MaximumCapacityUnits: 120
  MaximumOnDemandCapacityUnits: 30
  MaximumCoreCapacityUnits: 25

Note: Presto doesn't use YARN, so managed scaling won't help Presto.
      Use Presto's own auto-scaling or size the core nodes for Presto demand.
```

### Pattern 4: Cost-Minimal Dev/Test

```
Workload: Developer testing, small datasets, intermittent
Strategy: Smallest possible, Spot for everything non-critical

Primary:  1 × m5.xlarge (On-Demand)
Core:     1 × m5.xlarge (On-Demand)
Task:     0-5 × m5.xlarge (Spot, managed scaling)

Managed Scaling:
  MinimumCapacityUnits: 1
  MaximumCapacityUnits: 6
  MaximumOnDemandCapacityUnits: 2

Auto-termination: 1 hour idle timeout

Monthly cost: ~$50/month (with auto-termination and Spot savings)
```

---

## 14. Failure Modes and Troubleshooting

### Common Scaling Issues

| Problem | Cause | Solution |
|---|---|---|
| **Cluster won't scale up** | Spot capacity unavailable | Add more instance types; use instance fleets with price-capacity-optimized |
| **Cluster won't scale up** | MaximumCapacityUnits reached | Increase maximum or check if limit is appropriate |
| **Cluster won't scale down** | Active shuffle data on nodes | Use EMR 6.13+ for improved shuffle metrics; reduce `spark.blacklist.decommissioning.timeout` |
| **Cluster won't scale down** | ApplicationMasters on candidate nodes | Expected behavior — AM nodes are protected |
| **Scale-down too slow** | HDFS decommissioning on core nodes | Reduce HDFS usage; use S3 for persistent data |
| **Scaling causes job failures** | Speculative execution + Spot | Disable speculative execution (EMR default is disabled) |
| **Instances in ARRESTED state** | EMR 5.30.0/5.30.1 bug without Presto | Install Presto (even if not needed) or upgrade EMR version |
| **CloudWatch metrics missing** | Network configuration blocking metrics collector | Ensure outbound access to API Gateway endpoint; port 9443 open |
| **EBS over-utilization blocking scale-down** | Disk > 90% full on candidate nodes | Increase EBS volume size; clean up temp data |

### Network Requirements for Managed Scaling

Managed scaling requires the EMR metrics collector to publish data to CloudWatch via API Gateway:

```
EMR Cluster (Primary node)
    │
    └── Metrics Collector → API Gateway endpoint (public)
                              │
                              └── CloudWatch
                                    │
                                    └── Managed Scaling Service
```

**Critical**: Do NOT use private DNS with VPC endpoints for API Gateway, or the metrics collector will fail to publish data, and managed scaling won't work.

If using a custom EMR security group that removes the default "allow all outbound" rule:
- Primary → Service security group: Allow TCP outbound on **port 9443**
- Service security group → Primary: Allow TCP inbound on **port 9443**

---

## 15. Design Decision Analysis

### Decision 1: Why Two Provisioning Models (Instance Groups + Instance Fleets)?

| Alternative | Pros | Cons |
|---|---|---|
| **Only instance groups** | Simple, familiar | Poor Spot diversification, no multi-AZ, no weighted capacity |
| **Only instance fleets** | Maximum flexibility | Over-complicated for simple use cases |
| **Both** ← EMR's choice | Simple option for simple cases, flexible option for complex cases | Two mental models to learn; some features only on one or the other |

**Why both**: Instance groups existed first (simpler era of EMR). Instance fleets were added later for advanced Spot optimization. Removing instance groups would break existing customers. So both coexist.

### Decision 2: Why Managed Scaling Instead of Kubernetes HPA?

| Alternative | Pros | Cons |
|---|---|---|
| **Kubernetes HPA** | Cloud-native, well-understood | Doesn't understand YARN, shuffle data, AM placement |
| **CloudWatch-based auto scaling** (legacy EMR approach) | Flexible, user-controlled | Complex to configure correctly; no shuffle/AM awareness |
| **Managed scaling** ← EMR's choice | YARN-native, shuffle-aware, AM-aware, zero-config | Less user control; opaque decision-making |

**Why managed scaling**: YARN workloads have unique scaling signals (pending containers, shuffle data location, AM placement) that generic auto-scalers don't understand. Managed scaling encodes EMR-specific domain knowledge into the scaling algorithm.

### Decision 3: Why price-capacity-optimized as Default Spot Strategy?

| Strategy | Price | Interruption Rate | Outcome |
|---|---|---|---|
| **lowest-price** | Best | Worst | Cheap but unstable — frequent interruptions cause retries, wasting time |
| **capacity-optimized** | Good | Good | Stable but may overpay when cheaper pools are also deep |
| **price-capacity-optimized** ← default | Good | Good | Best balance — avoids thin pools (high interruption) while still optimizing price |

**Why**: Data analysis showed that lowest-price strategies led to higher total cost due to interruption-driven retries. price-capacity-optimized provides the best effective cost (instance cost + retry cost + time-to-completion).

---

## 16. Interview Angles

### Questions an Interviewer Might Ask

**Instance Configuration:**
- "When would you choose instance fleets over instance groups?"
  - Answer: Instance fleets when: (1) need Spot diversification across 10+ instance types, (2) need multi-AZ for higher availability, (3) want automatic allocation strategies like price-capacity-optimized, (4) need mixed On-Demand + Spot in the same fleet. Instance groups for simple, single-instance-type clusters.

- "How does weighted capacity work in instance fleets?"
  - Answer: Each instance type gets a weight proportional to its compute power (often vCPUs). The fleet target is expressed in total units, not instance count. EMR selects the optimal mix of instance types to fill the target. Example: target 100 units with m5.xlarge (weight 4, need 25) or m5.4xlarge (weight 16, need 7).

**Managed Scaling:**
- "How does EMR decide when to scale up?"
  - Answer: Managed scaling monitors YARN metrics — primarily pending containers and available memory. When applications need resources that aren't available (containers queued), managed scaling adds instances. It prefers adding task nodes (cheaper, faster to decommission) over core nodes.

- "Why is scale-down slower than scale-up?"
  - Answer: Three reasons: (1) graceful decommissioning waits for running containers to finish (up to 1-hour timeout), (2) core nodes require HDFS block replication to surviving nodes, (3) shuffle-aware scaling avoids removing nodes with active shuffle data (since EMR 6.x).

**Spot Integration:**
- "What's the best Spot allocation strategy for EMR?"
  - Answer: price-capacity-optimized (default since EMR 6.10.0). It balances price and capacity availability — picks the cheapest pool among those with sufficient capacity depth. lowest-price saves more per instance but leads to higher interruption rates, which increases total job cost due to retries.

- "What happens when a Spot instance is reclaimed mid-job?"
  - Answer: EC2 sends 2-minute warning. YARN marks the node as decommissioning. All containers on that node are eventually FAILED. ApplicationMasters (on On-Demand/CORE nodes, protected by node labels) detect the failures and request replacement containers from YARN. The job continues with temporarily reduced parallelism. No data loss for task nodes (no HDFS).

### Red Flags in Candidate Answers

| Red Flag | Why It's Wrong |
|---|---|
| "Use lowest-price Spot strategy to minimize cost" | Highest interruption rate; total cost often higher due to retries and wasted work |
| "Don't use managed scaling, I'll manually size the cluster" | EMR managed scaling is YARN-aware, shuffle-aware, and AM-aware — better than manual for most workloads |
| "Instance groups and instance fleets are interchangeable" | Different capabilities — fleets have multi-AZ, allocation strategies, weighted capacity; groups have up to 48 task groups |
| "Scale down immediately when demand drops" | Graceful decommissioning is essential — immediate termination causes task failures and HDFS data loss |
| "Use the same instance type for all Spot requests" | Single-pool concentration = high interruption risk; diversify across 10+ instance types |
| "Managed scaling works for Presto" | No — Presto is not YARN-based; managed scaling only works for Spark, Hive, MapReduce, Flink |
