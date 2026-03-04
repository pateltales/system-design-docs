# ECS Capacity Providers and Auto Scaling — Deep Dive

> Companion doc for [interview-simulation.md](interview-simulation.md)
> Covers: Capacity provider types, managed scaling, managed termination protection, CapacityProviderReservation, service auto scaling, base/weight strategy

---

## Table of Contents

1. [Capacity Providers Overview](#1-capacity-providers-overview)
2. [Capacity Provider Strategy — Base and Weight](#2-capacity-provider-strategy--base-and-weight)
3. [Fargate Capacity Providers](#3-fargate-capacity-providers)
4. [ASG Capacity Providers](#4-asg-capacity-providers)
5. [Managed Scaling](#5-managed-scaling)
6. [Managed Termination Protection](#6-managed-termination-protection)
7. [ECS Managed Instances](#7-ecs-managed-instances)
8. [Service Auto Scaling (Task-Level)](#8-service-auto-scaling-task-level)
9. [Two Levels of Scaling](#9-two-levels-of-scaling)
10. [Design Decisions and Trade-offs](#10-design-decisions-and-trade-offs)
11. [Interview Angles](#11-interview-angles)

---

## 1. Capacity Providers Overview

### 1.1 What Are Capacity Providers?

Capacity providers define **where** tasks run and **how** infrastructure scales to support them.

```
┌─────────────────────────────────────────────────────────────────┐
│                       ECS Cluster                                │
│                                                                  │
│  Capacity Provider Strategy:                                     │
│  ┌─────────────┐  ┌─────────────┐  ┌─────────────────────────┐ │
│  │ FARGATE     │  │ FARGATE_SPOT│  │ ASG: my-asg             │ │
│  │ (on-demand) │  │ (~70% off)  │  │ (EC2 instances)         │ │
│  │             │  │             │  │                          │ │
│  │ base: 2     │  │ weight: 3   │  │ Managed scaling: ON     │ │
│  │ weight: 1   │  │             │  │ Managed termination: ON │ │
│  └─────────────┘  └─────────────┘  └─────────────────────────┘ │
│                                                                  │
│  Tasks distributed: base first, then by weight ratio             │
└─────────────────────────────────────────────────────────────────┘
```

### 1.2 Types of Capacity Providers

| Type | Infrastructure | Management | Best For |
|------|---------------|-----------|----------|
| **FARGATE** | Serverless (on-demand) | Fully AWS-managed | Most workloads |
| **FARGATE_SPOT** | Serverless (spot) | Fully AWS-managed, interruptible | Fault-tolerant batch |
| **ASG Capacity Provider** | EC2 Auto Scaling group | Customer-managed (with ECS managed scaling) | Full control, GPU |
| **ECS Managed Instances** | EC2 (AWS-managed) | Fully AWS-managed | Simplified EC2 |

### 1.3 Constraints

- Up to **20 capacity providers** per cluster
- A capacity provider strategy can only contain **one type** (cannot mix Fargate with ASG in the same strategy)
- A default strategy can be set at the cluster level (used when no strategy is specified per service/task)

---

## 2. Capacity Provider Strategy — Base and Weight

### 2.1 How Base and Weight Work

```
Strategy: [
    { provider: FARGATE,      base: 2, weight: 1 },
    { provider: FARGATE_SPOT, base: 0, weight: 3 }
]

Task distribution for 10 tasks:
├── Base first: 2 tasks → FARGATE (guaranteed on-demand)
├── Remaining 8 tasks distributed by weight ratio (1:3):
│   ├── 2 tasks → FARGATE (1/4 of 8)
│   └── 6 tasks → FARGATE_SPOT (3/4 of 8)
└── Total: 4 FARGATE + 6 FARGATE_SPOT

Task distribution for 1 task:
└── 1 task → FARGATE (base of 2, so first tasks go to base provider)
```

### 2.2 Base and Weight Rules

| Parameter | Description | Default | Limit |
|-----------|-------------|---------|-------|
| **base** | Minimum tasks on this provider (filled first) | 0 | Only ONE provider per strategy can have base > 0 |
| **weight** | Relative proportion for remaining tasks | Console: 1, API: 0 | At least one provider must have weight > 0 |

### 2.3 Strategy Examples

**Example 1: All Fargate On-Demand**
```json
[{ "capacityProvider": "FARGATE", "base": 0, "weight": 1 }]
```

**Example 2: Fargate with Spot savings**
```json
[
    { "capacityProvider": "FARGATE", "base": 2, "weight": 1 },
    { "capacityProvider": "FARGATE_SPOT", "base": 0, "weight": 3 }
]
```
Base 2 on-demand for reliability, 75% of remaining on Spot.

**Example 3: EC2 primary, Fargate burst**
```json
[
    { "capacityProvider": "my-asg-provider", "base": 10, "weight": 2 },
    { "capacityProvider": "FARGATE", "base": 0, "weight": 1 }
]
```
First 10 tasks on EC2 (cheaper), overflow 2:1 EC2-to-Fargate ratio.

---

## 3. Fargate Capacity Providers

### 3.1 Pre-Defined Providers

ECS provides two built-in Fargate capacity providers:

| Provider | Pricing | Availability | Interruption |
|----------|---------|-------------|--------------|
| **FARGATE** | On-demand (pay per vCPU-sec + GB-sec) | Guaranteed | None |
| **FARGATE_SPOT** | ~70% discount | Best-effort (spare capacity) | 2-minute SIGTERM warning |

### 3.2 Scaling Behavior

Fargate scaling is per-task — no instance management:
- ECS creates a Firecracker microVM for each task
- No ASG, no instances to manage
- Subject to Fargate quotas (burst rate, sustained rate, vCPU limit)

---

## 4. ASG Capacity Providers

### 4.1 Architecture

```
┌─────────────────────────────────────────────────────────────────┐
│                    ECS Control Plane                              │
│                                                                  │
│  ┌──────────────────┐     ┌──────────────────────────────────┐  │
│  │ Service Scheduler │     │ Capacity Provider Manager        │  │
│  │                   │     │                                   │  │
│  │ "I need 3 more   │────►│ "ASG has 5 instances, room for   │  │
│  │  tasks placed"    │     │  2 tasks. Need to scale ASG."    │  │
│  └──────────────────┘     │                                   │  │
│                           │ CapacityProviderReservation        │  │
│                           │ metric → ASG target tracking       │  │
│                           └──────────┬───────────────────────┘  │
│                                      │                           │
└──────────────────────────────────────┼───────────────────────────┘
                                       │
                                       ▼
┌──────────────────────────────────────────────────────────────────┐
│                    Auto Scaling Group                              │
│                                                                   │
│  ┌──────────┐  ┌──────────┐  ┌──────────┐     ┌──────────┐     │
│  │ Instance │  │ Instance │  │ Instance │ ... │ Instance │     │
│  │    1     │  │    2     │  │    3     │     │    N     │     │
│  │ [Task]   │  │ [Task]   │  │ [Task]   │     │ [empty]  │     │
│  │ [Task]   │  │ [Task]   │  │          │     │          │     │
│  └──────────┘  └──────────┘  └──────────┘     └──────────┘     │
│                                                                   │
│  Managed Scaling: ECS adjusts ASG desired count via target       │
│  tracking on CapacityProviderReservation metric                  │
│                                                                   │
│  Managed Termination Protection: ECS prevents ASG from           │
│  terminating instances that still have running tasks             │
└──────────────────────────────────────────────────────────────────┘
```

### 4.2 Creating an ASG Capacity Provider

```bash
aws ecs create-capacity-provider \
    --name my-asg-provider \
    --auto-scaling-group-provider \
        autoScalingGroupArn=arn:aws:autoscaling:...:autoScalingGroup:...,\
        managedScaling='{status=ENABLED,targetCapacity=100,minimumScalingStepSize=1,maximumScalingStepSize=10}',\
        managedTerminationProtection=ENABLED
```

---

## 5. Managed Scaling

### 5.1 How Managed Scaling Works

When managed scaling is enabled, ECS automatically manages the ASG's desired count using a custom CloudWatch metric called **CapacityProviderReservation**:

```
CapacityProviderReservation = (tasks needing capacity / total capacity) × 100

Target: Keep CapacityProviderReservation at targetCapacity (default: 100%)
```

**The feedback loop:**

```
ECS calculates CapacityProviderReservation
         │
         ▼
CloudWatch publishes metric
         │
         ▼
ASG target tracking policy reacts:
  If metric > target → Scale OUT (add instances)
  If metric < target → Scale IN (remove instances)
         │
         ▼
New instances join cluster → ECS agent registers
         │
         ▼
ECS places pending tasks on new instances
         │
         ▼
CapacityProviderReservation drops → Stabilizes
```

### 5.2 Target Capacity

| Setting | Description | Default |
|---------|-------------|---------|
| **targetCapacity** | What % of capacity should be reserved | 100 |
| **minimumScalingStepSize** | Min instances to add/remove per scaling action | 1 |
| **maximumScalingStepSize** | Max instances to add/remove per scaling action | 10,000 |

**Target capacity < 100**: Provides a buffer of empty instances (spare capacity for faster task placement). E.g., `targetCapacity=80` means ECS tries to keep 20% capacity headroom.

**Target capacity = 100**: No spare capacity; instances are fully utilized.

### 5.3 Managed Scaling Advantages

| Aspect | Without Managed Scaling | With Managed Scaling |
|--------|------------------------|---------------------|
| ASG scaling | Customer configures scaling policies | ECS manages via CapacityProviderReservation |
| Task awareness | ASG doesn't know about ECS tasks | ECS drives scaling based on task needs |
| Over-provisioning | Likely (scaling based on CPU/memory, not tasks) | Minimal (scaling based on actual task capacity needs) |
| Under-provisioning | Possible | Unlikely (ECS signals exact need) |

---

## 6. Managed Termination Protection

### 6.1 The Problem

Without termination protection, ASG scale-in might terminate an instance with running tasks:

```
ASG scale-in event:
  Instance A: 3 running tasks → ASG terminates this instance!
  → 3 tasks killed unexpectedly
  → Service scheduler must replace them elsewhere
  → Temporary capacity loss
```

### 6.2 How Managed Termination Protection Works

```
ASG wants to scale in (terminate Instance A)
         │
         ▼
ECS checks: Does Instance A have running tasks?
         │
    ┌────┼────┐
   Yes       No
    │         │
    ▼         ▼
  PROTECT    ALLOW
  Instance   termination
    │
    ▼
  ECS drains tasks from Instance A:
  1. Stop sending new tasks to this instance
  2. Wait for running tasks to complete or be replaced elsewhere
  3. Once instance is empty: remove protection
  4. ASG terminates the empty instance
```

### 6.3 Managed Instance Draining

Managed termination protection triggers **managed instance draining**:

1. Instance is marked for termination by ASG
2. ECS sets instance to `DRAINING` state
3. ECS stops placing new tasks on this instance
4. Service scheduler launches replacement tasks on other instances
5. Existing tasks are gracefully stopped (SIGTERM → wait → SIGKILL)
6. Once all tasks are stopped and replacements are running → instance is unprotected
7. ASG terminates the empty instance

**On by default** for ASG capacity providers.

---

## 7. ECS Managed Instances

### 7.1 What Are Managed Instances?

A newer option where AWS fully manages EC2 instances:

| Aspect | ASG Capacity Provider | ECS Managed Instances |
|--------|----------------------|----------------------|
| Instance provisioning | Customer (ASG + launch template) | AWS |
| Patching | Customer | AWS |
| Instance type selection | Customer | AWS (optimized selection) |
| Scaling | Managed scaling (via CapacityProviderReservation) | AWS-managed |
| Cost | EC2 pricing | EC2 pricing |
| Control | High (choose instance types, AMI) | Low (AWS decides) |
| Placement strategies | Supported | NOT supported (auto-spread) |

### 7.2 When to Use Managed Instances vs ASG

| Scenario | Recommendation |
|----------|---------------|
| Simplest EC2 experience | ECS Managed Instances |
| Specific instance types (GPU, memory-optimized) | ASG Capacity Provider |
| Custom AMI | ASG Capacity Provider |
| Placement strategies needed | ASG Capacity Provider |
| Minimize operational overhead | ECS Managed Instances |

---

## 8. Service Auto Scaling (Task-Level)

### 8.1 Four Scaling Types

| Type | How It Works | Best For |
|------|-------------|----------|
| **Target tracking** | Maintain a target metric value (thermostat model) | Most workloads |
| **Step scaling** | Predefined steps based on CloudWatch alarm thresholds | Precise control |
| **Scheduled scaling** | Scale based on time/date | Predictable patterns |
| **Predictive scaling** | ML-based analysis of historical patterns | Daily/weekly cycles |

### 8.2 Target Tracking — The Most Common

```bash
aws application-autoscaling put-scaling-policy \
    --service-namespace ecs \
    --scalable-dimension ecs:service:DesiredCount \
    --resource-id service/my-cluster/my-service \
    --policy-name cpu-target-tracking \
    --policy-type TargetTrackingScaling \
    --target-tracking-scaling-policy-configuration '{
        "TargetValue": 70.0,
        "PredefinedMetricSpecification": {
            "PredefinedMetricType": "ECSServiceAverageCPUUtilization"
        },
        "ScaleOutCooldown": 60,
        "ScaleInCooldown": 300
    }'
```

### 8.3 Available Metrics

| Metric | Description |
|--------|-------------|
| `ECSServiceAverageCPUUtilization` | Average CPU across all tasks in service |
| `ECSServiceAverageMemoryUtilization` | Average memory across all tasks |
| `ALBRequestCountPerTarget` | Requests per target (for ALB-backed services) |
| Custom CloudWatch metric | Any metric you define |

### 8.4 Cooldown Periods

| Direction | Purpose | Behavior |
|-----------|---------|----------|
| **Scale-out cooldown** | Prevent excessive scaling up | After adding tasks, wait before adding more |
| **Scale-in cooldown** | Protect availability | After removing tasks, wait before removing more |

**Special case**: If a scale-out alarm fires during scale-in cooldown, the scale-out happens immediately (availability takes priority over cost).

### 8.5 Scaling During Deployments

| Scaling Direction | During Deployment |
|-------------------|-------------------|
| Scale-out | **Continues** (can add more tasks) |
| Scale-in | **Suspended** (won't remove tasks mid-deploy) |

---

## 9. Two Levels of Scaling

### 9.1 The Two-Layer Model

```
┌─────────────────────────────────────────────────────────────┐
│                                                              │
│  Layer 1: SERVICE AUTO SCALING (Task Count)                  │
│                                                              │
│  CloudWatch metric (CPU > 70%) → Increase desired count     │
│  desired count: 4 → 8 tasks                                 │
│                                                              │
│  ┌─── But where do the new tasks run? ───┐                  │
│  │                                        │                  │
│  ▼                                        │                  │
│                                                              │
│  Layer 2: CAPACITY PROVIDER SCALING (Infrastructure)         │
│                                                              │
│  CapacityProviderReservation > 100% → ASG adds instances    │
│  New instances join cluster → ECS places pending tasks       │
│                                                              │
│  OR                                                          │
│                                                              │
│  Fargate: No infrastructure scaling needed (per-task)        │
│                                                              │
└─────────────────────────────────────────────────────────────┘
```

### 9.2 The Sequence

```
1. Traffic increases → CPU utilization rises above target (70%)
2. Service auto scaling increases desired count (4 → 8)
3. ECS scheduler tries to place 4 new tasks
4. If EC2 cluster: not enough capacity → tasks PENDING
5. CapacityProviderReservation rises above targetCapacity
6. Managed scaling adds instances to ASG
7. New instances register with ECS
8. Pending tasks placed on new instances
9. Service has 8 running tasks

If Fargate: steps 4-7 are instant (no instance provisioning)
```

### 9.3 Why Two Layers?

| Scenario | Service Scaling Only | Capacity Scaling Only | Both |
|----------|---------------------|---------------------|------|
| Tasks need to scale | ✓ Increases desired count | ✗ Doesn't change task count | ✓ |
| Infrastructure insufficient | ✗ Tasks stay PENDING | ✓ Adds instances | ✓ |
| Right-sizing | ✗ May waste instances | ✗ May have too many/few tasks | ✓ |

You need both: service scaling decides **how many tasks**, capacity scaling provides **where to run them**.

---

## 10. Design Decisions and Trade-offs

### 10.1 Fargate vs ASG Cost

| Workload Pattern | Cheaper Option | Why |
|-----------------|---------------|-----|
| Steady, 24/7, predictable | EC2 (Reserved Instances/Savings Plans) | RI/SP gives 40-70% discount |
| Variable, bursty | Fargate | Pay per task-second, no idle waste |
| Mixed | EC2 baseline + Fargate burst | Best of both worlds |
| Spot-tolerant batch | Fargate Spot or EC2 Spot | 60-90% discount |

### 10.2 Why CapacityProviderReservation Instead of CPU/Memory?

Traditional ASG scaling (CPU utilization) is task-unaware:
- ASG scales based on instance CPU, not task needs
- May have 20% CPU free but no room for another 2-vCPU task
- Over-provisioning is common

CapacityProviderReservation is task-aware:
- Directly measures "tasks needing capacity / available capacity"
- Scales precisely based on actual task placement needs
- No over/under-provisioning

### 10.3 Strategy: Base + Weight Design

The base/weight model enables sophisticated capacity management:

```
Strategy: base=5 on EC2, weight 2:1 EC2:Fargate

Scaling sequence:
Tasks 1-5:   All on EC2 (base)
Task 6-7:    EC2 (weight 2)
Task 8:      Fargate (weight 1)
Task 9-10:   EC2
Task 11:     Fargate
...

This gives EC2 a cost-optimized baseline and Fargate for burst.
```

---

## 11. Interview Angles

### 11.1 Key Questions

**Q: "How does ECS know when to add more EC2 instances?"**

ECS publishes a custom CloudWatch metric called `CapacityProviderReservation`, which represents the ratio of task capacity needed vs available. When this metric exceeds the `targetCapacity` (default 100%), the ASG's target tracking policy triggers a scale-out. This is task-aware — unlike traditional CPU-based scaling, it directly measures whether there's room for more tasks. ECS also handles the reverse: when tasks are removed, the metric drops, triggering scale-in (with managed termination protection ensuring running tasks aren't killed).

**Q: "A customer wants to run 80% on Spot and 20% on-demand. How?"**

```json
[
    { "capacityProvider": "FARGATE", "base": 2, "weight": 1 },
    { "capacityProvider": "FARGATE_SPOT", "base": 0, "weight": 4 }
]
```
First 2 tasks guaranteed on-demand (base). Remaining: 1:4 ratio → 20% on-demand, 80% Spot. The base ensures at least 2 tasks survive a Spot reclamation.

**Q: "What happens if an ASG scale-in event tries to terminate an instance with running tasks?"**

With managed termination protection enabled (default): ECS blocks the termination, marks the instance as DRAINING, stops placing new tasks on it, waits for existing tasks to be replaced on other instances, then allows ASG to terminate the empty instance. This prevents task disruption during scale-in.

**Q: "Why are there two separate scaling mechanisms (service auto scaling + capacity provider scaling)?"**

They operate at different levels. Service auto scaling adjusts the desired task count based on application load (CPU, memory, request count). Capacity provider scaling adjusts infrastructure to support those tasks. You might have 10 tasks on 5 instances, then service scaling increases to 20 tasks — now capacity scaling needs to add more instances to fit 10 additional tasks. With Fargate, capacity scaling is instant (no instances). With EC2, there's a delay while instances launch and join the cluster.

### 11.2 Numbers to Know

| Metric | Value |
|--------|-------|
| Max capacity providers per cluster | 20 |
| Max base providers per strategy | 1 |
| Default targetCapacity | 100% |
| Fargate Spot discount | ~70% |
| Fargate Spot warning | 2 minutes (SIGTERM) |
| CloudWatch metric interval | 1 minute |
| Default Fargate vCPU quota | 6 |
| Service auto scaling types | 4 (target tracking, step, scheduled, predictive) |

---

*Cross-references:*
- [Cluster Architecture](cluster-architecture.md) — Capacity layer, launch type comparison
- [Fargate Architecture](fargate-architecture.md) — Fargate-specific quotas and pricing
- [Task Placement](task-placement.md) — How placement interacts with capacity availability
- [Deployment Strategies](deployment-strategies.md) — Scaling behavior during deployments
