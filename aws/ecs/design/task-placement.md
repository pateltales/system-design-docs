# ECS Task Placement — Deep Dive

> Companion doc for [interview-simulation.md](interview-simulation.md)
> Covers: Placement pipeline, strategies (spread/binpack/random), constraints (distinctInstance/memberOf), cluster query language, combined strategies, Fargate vs EC2 placement

---

## Table of Contents

1. [Placement Pipeline Overview](#1-placement-pipeline-overview)
2. [Placement Strategies](#2-placement-strategies)
3. [Placement Constraints](#3-placement-constraints)
4. [Cluster Query Language](#4-cluster-query-language)
5. [Built-in and Custom Attributes](#5-built-in-and-custom-attributes)
6. [Combining Strategies and Constraints](#6-combining-strategies-and-constraints)
7. [Placement by Launch Type](#7-placement-by-launch-type)
8. [Scale-In Behavior](#8-scale-in-behavior)
9. [Task Groups](#9-task-groups)
10. [Practical Placement Patterns](#10-practical-placement-patterns)
11. [Design Decisions and Trade-offs](#11-design-decisions-and-trade-offs)
12. [Interview Angles](#12-interview-angles)

---

## 1. Placement Pipeline Overview

### 1.1 The Four-Step Pipeline (EC2 Launch Type)

When ECS needs to place a task, it runs a four-step pipeline:

```
┌─────────────────────────────────────────────────────────────────┐
│                   ECS Placement Pipeline                         │
│                                                                  │
│  Step 1: RESOURCE FILTER                                         │
│  ┌──────────────────────────────────────────────────────────┐   │
│  │ Filter instances by CPU, memory, ports, GPU              │   │
│  │ requirements from the task definition                     │   │
│  │                                                           │   │
│  │ 20 instances → 12 have enough resources                  │   │
│  └─────────────────────────────┬────────────────────────────┘   │
│                                │                                 │
│  Step 2: CONSTRAINT FILTER (binding)                             │
│  ┌─────────────────────────────▼────────────────────────────┐   │
│  │ Apply placement constraints                               │   │
│  │ (distinctInstance, memberOf expressions)                   │   │
│  │                                                           │   │
│  │ If NO instance matches → task stays PENDING               │   │
│  │                                                           │   │
│  │ 12 instances → 8 satisfy constraints                      │   │
│  └─────────────────────────────┬────────────────────────────┘   │
│                                │                                 │
│  Step 3: STRATEGY EVALUATION (best-effort)                       │
│  ┌─────────────────────────────▼────────────────────────────┐   │
│  │ Apply placement strategies in order                       │   │
│  │ (spread, binpack, random)                                 │   │
│  │                                                           │   │
│  │ Strategies are BEST EFFORT — they rank/sort               │   │
│  │ the remaining instances but don't reject them             │   │
│  │                                                           │   │
│  │ 8 instances → ranked by strategy preference               │   │
│  └─────────────────────────────┬────────────────────────────┘   │
│                                │                                 │
│  Step 4: SELECT                                                  │
│  ┌─────────────────────────────▼────────────────────────────┐   │
│  │ Pick the top-ranked instance(s)                           │   │
│  │ Place the task                                            │   │
│  └──────────────────────────────────────────────────────────┘   │
│                                                                  │
└─────────────────────────────────────────────────────────────────┘
```

### 1.2 Key Distinction: Constraints vs Strategies

| Aspect | Constraints | Strategies |
|--------|------------|------------|
| **Nature** | Binding (hard rules) | Best-effort (soft preferences) |
| **Effect** | Eliminate instances that don't match | Rank remaining instances |
| **On failure** | Task stays PENDING | Falls through to next strategy |
| **Types** | `distinctInstance`, `memberOf` | `spread`, `binpack`, `random` |
| **When to use** | Must-have requirements | Nice-to-have preferences |

---

## 2. Placement Strategies

### 2.1 Spread

**Goal**: Distribute tasks evenly across a specified dimension.

```
Spread by attribute:ecs.availability-zone

     us-east-1a          us-east-1b          us-east-1c
  ┌──────────────┐   ┌──────────────┐   ┌──────────────┐
  │ Instance A   │   │ Instance C   │   │ Instance E   │
  │  [Task 1]    │   │  [Task 2]    │   │  [Task 3]    │
  │              │   │              │   │              │
  │ Instance B   │   │ Instance D   │   │ Instance F   │
  │  [Task 4]    │   │  [Task 5]    │   │  [Task 6]    │
  └──────────────┘   └──────────────┘   └──────────────┘

  Each AZ gets the same number of tasks (or as close as possible)
```

**Valid fields:**
- `attribute:ecs.availability-zone` — spread across AZs (most common, **default for services**)
- `instanceId` (or `host`) — spread across instances (max 1 task per instance)
- Any built-in or custom attribute

**JSON configuration:**
```json
"placementStrategy": [
    {
        "type": "spread",
        "field": "attribute:ecs.availability-zone"
    }
]
```

**When to use**: High availability — if one AZ goes down, only 1/N of tasks are affected.

### 2.2 Binpack

**Goal**: Pack tasks onto the fewest instances to minimize cost.

```
Binpack by memory:

  Instance A (8 GB)         Instance B (8 GB)         Instance C (8 GB)
  ┌──────────────────┐     ┌──────────────────┐     ┌──────────────────┐
  │ ████████████████ │     │ ████████████████ │     │                  │
  │ Task 1 (2 GB)    │     │ Task 3 (2 GB)    │     │   EMPTY          │
  │ ████████████████ │     │ Task 4 (2 GB)    │     │   (can be        │
  │ Task 2 (2 GB)    │     │ ████████████████ │     │    terminated    │
  │ Task 5 (2 GB)    │     │ Task 6 (2 GB)    │     │    to save $)   │
  │ ████████████████ │     │ ████████████████ │     │                  │
  │ Task 7 (2 GB)    │     │                  │     │                  │
  └──────────────────┘     └──────────────────┘     └──────────────────┘
```

**Valid fields:**
- `cpu` — pack by CPU utilization
- `memory` — pack by memory utilization

**JSON configuration:**
```json
"placementStrategy": [
    {
        "type": "binpack",
        "field": "memory"
    }
]
```

**When to use**: Cost optimization — fewer instances needed, idle instances can be terminated.

### 2.3 Random

**Goal**: Place tasks on any available instance at random.

**No field required:**
```json
"placementStrategy": [
    {
        "type": "random"
    }
]
```

**When to use**: Testing, or when neither availability nor cost optimization matters.

### 2.4 Strategy Comparison

| Strategy | Goal | Best For | Downside |
|----------|------|----------|----------|
| **Spread (AZ)** | High availability | Production services | May leave instances underutilized |
| **Spread (instance)** | Max isolation | Each task on its own instance | Most expensive |
| **Binpack (memory)** | Cost optimization | Batch jobs, dev/staging | Concentrated failure blast radius |
| **Binpack (CPU)** | Cost optimization | CPU-intensive workloads | Same as above |
| **Random** | Simplicity | Testing | No optimization |

---

## 3. Placement Constraints

### 3.1 distinctInstance

**Rule**: Each task in the group must be on a different container instance.

```
distinctInstance with 3 tasks:

  Instance A        Instance B        Instance C        Instance D
  [Task 1] ✓       [Task 2] ✓       [Task 3] ✓       (available)

  Task 4 → Can go on Instance D, but NOT on A, B, or C
```

**JSON configuration:**
```json
"placementConstraints": [
    {
        "type": "distinctInstance"
    }
]
```

**Important edge case**: ECS checks the *desired status* of tasks. If a task has `STOPPED` desired status but hasn't actually stopped yet, a new task CAN still be placed on that instance (because the old task is considered "leaving").

**Limit**: Cannot run more tasks than you have instances. If desired count > available instances, excess tasks stay PENDING.

### 3.2 memberOf

**Rule**: Tasks must be placed on instances matching a cluster query language expression.

```json
"placementConstraints": [
    {
        "type": "memberOf",
        "expression": "attribute:ecs.instance-type =~ g2.* and attribute:ecs.availability-zone != us-east-1d"
    }
]
```

**Where memberOf can be specified:**
- `RunTask` — ad-hoc task placement
- `CreateService` / `UpdateService` — service-level constraints
- `RegisterTaskDefinition` — baked into the task definition itself

---

## 4. Cluster Query Language

### 4.1 Expression Syntax

```
subject operator [argument]
```

### 4.2 Subjects

| Subject | Description | Example |
|---------|-------------|---------|
| `attribute:name` | Built-in or custom attribute | `attribute:ecs.instance-type` |
| `agentConnected` | Agent connectivity status | `agentConnected == true` |
| `agentVersion` | Agent version | `agentVersion >= 1.50.0` |
| `ec2InstanceId` | EC2 instance ID | `ec2InstanceId in ['i-abc', 'i-def']` |
| `registeredAt` | Instance registration timestamp | `registeredAt >= 2024-01-01` |
| `runningTasksCount` | Number of running tasks on instance | `runningTasksCount < 5` |
| `task:group` | Task group name | `task:group == service:web` |

### 4.3 Operators

| Operator | Aliases | Description |
|----------|---------|-------------|
| `==` | `equals` | String equality |
| `!=` | `not_equals` | String inequality |
| `>` | — | Greater than |
| `>=` | — | Greater than or equal |
| `<` | — | Less than |
| `<=` | — | Less than or equal |
| `exists` | — | Attribute exists |
| `!exists` | `not_exists` | Attribute doesn't exist |
| `in` | — | Value in list `[a, b, c]` |
| `!in` | `not_in` | Value not in list |
| `=~` | `matches` | Java regex match |
| `!~` | `not_matches` | Java regex no match |

### 4.4 Boolean Operators

| Operator | Aliases |
|----------|---------|
| `&&` | `and` |
| `\|\|` | `or` |
| `!` | `not` |

Use parentheses for precedence: `(expr1 or expr2) and expr3`

### 4.5 Examples

```
# Only GPU instances
attribute:ecs.instance-type =~ g2.*

# Specific AZs only
attribute:ecs.availability-zone in [us-east-1a, us-east-1b]

# Custom attribute for environment
attribute:environment == production

# Instances with fewer than 5 running tasks
runningTasksCount < 5

# Combined: production GPU instances in specific AZs
attribute:environment == production and attribute:ecs.instance-type =~ p3.* and attribute:ecs.availability-zone in [us-east-1a, us-east-1b]

# Task anti-affinity (don't place where database tasks run)
not(task:group == database)

# ARM64 architecture
attribute:ecs.cpu-architecture == arm64
```

---

## 5. Built-in and Custom Attributes

### 5.1 Built-in Attributes

| Attribute | Description | Example Values |
|-----------|-------------|----------------|
| `ecs.ami-id` | AMI used to launch instance | `ami-1234abcd` |
| `ecs.availability-zone` | Availability Zone | `us-east-1a` |
| `ecs.instance-type` | EC2 instance type | `c5.xlarge`, `m5.2xlarge` |
| `ecs.os-type` | Operating system | `linux`, `windows` |
| `ecs.os-family` | OS family/version | `LINUX`, `WINDOWS_SERVER_2022_FULL` |
| `ecs.cpu-architecture` | CPU architecture | `x86_64`, `arm64` |
| `ecs.vpc-id` | VPC ID | `vpc-1234abcd` |
| `ecs.subnet-id` | Subnet ID | `subnet-1234abcd` |
| `ecs.awsvpc-trunk-id` | Trunk ENI present | (exists if trunking enabled) |
| `ecs.outpost-arn` | AWS Outpost ARN | (exists if on Outpost) |
| `ecs.capability.external` | External instance flag | (exists if ECS Anywhere) |

### 5.2 Custom Attributes

You can tag instances with custom attributes for fine-grained placement:

```bash
# Add custom attributes to an instance
aws ecs put-attributes \
    --cluster production \
    --attributes name=environment,value=production,targetId=arn:aws:ecs:...:container-instance/abc123

aws ecs put-attributes \
    --cluster production \
    --attributes name=gpu-model,value=v100,targetId=arn:aws:ecs:...:container-instance/abc123
```

**Naming rules:**
- Name: 1–128 characters (letters, numbers, hyphens, underscores, slashes, periods)
- Value: 1–128 characters (same + @, colons, spaces; no leading/trailing whitespace)

**Use cases:**
- Tagging instances as `environment=production` vs `environment=staging`
- Tagging GPU model: `gpu-model=v100` vs `gpu-model=a100`
- Tagging workload type: `workload=ml-training` vs `workload=web-serving`

---

## 6. Combining Strategies and Constraints

### 6.1 Multiple Strategies — Evaluation Order

When you specify multiple strategies, ECS evaluates them **sequentially**:

```json
"placementStrategy": [
    {
        "type": "spread",
        "field": "attribute:ecs.availability-zone"
    },
    {
        "type": "binpack",
        "field": "memory"
    }
]
```

**Evaluation:**
1. First, group instances by AZ and spread evenly
2. Within each AZ, binpack by memory (pick the instance with least available memory that still fits)

```
Result: Balanced across AZs AND cost-efficient within each AZ

  us-east-1a (AZ spread)          us-east-1b (AZ spread)
  ┌─────────────────────┐        ┌─────────────────────┐
  │ Instance A (packed)  │        │ Instance C (packed)  │
  │ [Task 1] [Task 3]   │        │ [Task 2] [Task 4]   │
  │ [Task 5]             │        │ [Task 6]             │
  │                      │        │                      │
  │ Instance B (empty)   │        │ Instance D (empty)   │
  │                      │        │                      │
  └─────────────────────┘        └─────────────────────┘

  binpack within AZ: A is packed before B is used
```

### 6.2 Strategies + Constraints Together

```json
{
    "placementStrategy": [
        { "type": "spread", "field": "attribute:ecs.availability-zone" },
        { "type": "binpack", "field": "memory" }
    ],
    "placementConstraints": [
        { "type": "memberOf", "expression": "attribute:ecs.instance-type =~ g2.*" }
    ]
}
```

**Pipeline:**
1. Filter by resources (CPU, memory, GPU, ports)
2. Filter by constraint: only `g2.*` instances
3. Rank by strategy: spread across AZs, then binpack by memory
4. Select top-ranked instance

### 6.3 Common Combined Patterns

| Pattern | Strategies | Constraints | Use Case |
|---------|-----------|-------------|----------|
| HA + Cost | spread(AZ) + binpack(memory) | — | Production services |
| HA + Isolation | spread(instance) | distinctInstance | Stateful tasks |
| GPU Workload | spread(AZ) | memberOf(instance-type =~ p3.*) | ML inference |
| Environment Isolation | binpack(memory) | memberOf(environment == prod) | Prod vs staging |
| Mixed AZ + Custom | spread(AZ) | memberOf(workload == web) | Workload segmentation |

---

## 7. Placement by Launch Type

### 7.1 Placement Support Matrix

| Feature | EC2 (ASG) | ECS Managed Instances | Fargate |
|---------|-----------|----------------------|---------|
| Placement strategies | Yes (all 3) | **No** (auto-spread by AZ) | **No** (auto-spread by AZ) |
| Placement constraints | Yes (both types) | Yes (constraints only) | **No** |
| Custom attributes | Yes | Yes | No |
| AZ spreading | Via strategy | Automatic | Automatic |

### 7.2 Fargate Placement

Fargate does not support placement strategies or constraints. ECS automatically:
- Spreads tasks across available AZs in the VPC subnets you specify
- When using both `FARGATE` and `FARGATE_SPOT`, spreading is **independent per capacity provider**

### 7.3 ECS Managed Instances Placement

ECS Managed Instances support constraints but NOT strategies:
1. Filter by CPU, GPU, memory, and port requirements
2. Apply placement constraints
3. Filter by capacity provider launch template requirements
4. Attempt to spread across AZs (automatic, not configurable)

---

## 8. Scale-In Behavior

### 8.1 How Strategies Affect Task Termination

When the desired count decreases, the strategy also determines **which tasks to stop**:

| Strategy | Scale-In Behavior |
|----------|-------------------|
| **Spread (AZ)** | Terminate tasks to maintain balance; random within the most-populated AZ |
| **Spread (instance)** | Terminate tasks to maintain balance across instances |
| **Binpack** | Terminate the task on the instance with the **most remaining resources** (free up whole instances) |
| **Random** | Random selection |

### 8.2 Binpack Scale-In Example

```
Before scale-in (desired: 6 → 4):

  Instance A: [Task 1] [Task 2] [Task 3]    (1 GB free)
  Instance B: [Task 4] [Task 5]              (3 GB free)
  Instance C: [Task 6]                       (5 GB free)

Binpack scale-in removes tasks from instances with MOST free resources:
  → Remove Task 6 from Instance C (5 GB free — most resources)
  → Remove Task 5 from Instance B (3 GB free — next most)

After scale-in:
  Instance A: [Task 1] [Task 2] [Task 3]    (still packed)
  Instance B: [Task 4]
  Instance C: (empty — can be terminated!)   ← cost savings
```

---

## 9. Task Groups

### 9.1 What Are Task Groups?

Task groups allow related tasks to influence placement decisions:

- **Service tasks**: Automatically grouped as `service:<service-name>`
- **RunTask tasks**: Can specify a custom `group` parameter
- **Default**: Tasks without a group are each in their own group

### 9.2 How Groups Affect Placement

**Spread strategy**: Considers tasks **within the same group** (not all tasks on the cluster):

```
Service A (group: service:web) — spread by AZ
  → Only considers web tasks when balancing across AZs

Service B (group: service:api) — spread by AZ
  → Only considers api tasks when balancing
  → Independent of Service A's distribution
```

**Task anti-affinity** (via cluster query language):
```
not(task:group == database)
```
This constraint prevents placing the task on instances that already have database tasks.

---

## 10. Practical Placement Patterns

### 10.1 Pattern: High-Availability Web Service

```json
{
    "placementStrategy": [
        { "type": "spread", "field": "attribute:ecs.availability-zone" }
    ]
}
```
Tasks evenly distributed across AZs. Losing one AZ affects at most 1/N tasks.

### 10.2 Pattern: Cost-Optimized Batch Processing

```json
{
    "placementStrategy": [
        { "type": "binpack", "field": "memory" }
    ]
}
```
Pack tasks onto fewest instances. Empty instances can be terminated by ASG/capacity provider.

### 10.3 Pattern: HA + Cost-Efficient Production

```json
{
    "placementStrategy": [
        { "type": "spread", "field": "attribute:ecs.availability-zone" },
        { "type": "binpack", "field": "memory" }
    ]
}
```
Spread across AZs first (availability), then pack within each AZ (cost).

### 10.4 Pattern: One Task Per Instance (Stateful/Heavy)

```json
{
    "placementConstraints": [
        { "type": "distinctInstance" }
    ],
    "placementStrategy": [
        { "type": "spread", "field": "attribute:ecs.availability-zone" }
    ]
}
```
Each task on a unique instance, spread across AZs.

### 10.5 Pattern: GPU Workload in Specific AZs

```json
{
    "placementConstraints": [
        {
            "type": "memberOf",
            "expression": "attribute:ecs.instance-type =~ p3.* and attribute:ecs.availability-zone in [us-east-1a, us-east-1b]"
        }
    ],
    "placementStrategy": [
        { "type": "spread", "field": "attribute:ecs.availability-zone" }
    ]
}
```

### 10.6 Pattern: Environment Isolation on Shared Cluster

```json
{
    "placementConstraints": [
        {
            "type": "memberOf",
            "expression": "attribute:environment == production"
        }
    ],
    "placementStrategy": [
        { "type": "spread", "field": "attribute:ecs.availability-zone" },
        { "type": "binpack", "field": "memory" }
    ]
}
```
Only place on instances tagged as production, then spread + binpack.

---

## 11. Design Decisions and Trade-offs

### 11.1 Why Best-Effort Strategies?

| Approach | Advantage | Disadvantage |
|----------|-----------|--------------|
| **Hard strategies (reject if not optimal)** | Perfect distribution | Tasks may stay PENDING forever |
| **Best-effort strategies (chosen)** | Tasks always get placed | Distribution may not be perfect |

ECS chose best-effort because placing a task quickly is more important than placing it perfectly. A slightly suboptimal placement is better than a task stuck in PENDING.

### 11.2 Spread vs Binpack — The Core Trade-off

```
                     ┌─────────────┐
                     │             │
        High         │   SPREAD    │        Spread: better HA, higher cost
    Availability     │   (AZ)      │        → Tasks distributed across AZs
                     │             │        → Losing 1 AZ loses ~33% tasks
                     └──────┬──────┘
                            │
                            │ Trade-off axis
                            │
                     ┌──────▼──────┐
                     │             │
        Cost         │  BINPACK    │        Binpack: lower cost, worse HA
    Optimization     │  (memory)   │        → Tasks packed on few instances
                     │             │        → Losing 1 instance could lose many tasks
                     └─────────────┘
```

**In practice**: Most production services use `spread(AZ) + binpack(memory)` — the best of both worlds.

### 11.3 ECS vs Kubernetes Scheduling

| Aspect | ECS | Kubernetes |
|--------|-----|------------|
| Strategies | spread, binpack, random | nodeAffinity, podAffinity/antiAffinity, taints/tolerations |
| Constraints | distinctInstance, memberOf | nodeSelector, requiredDuringScheduling |
| Query language | ECS cluster query language | Label selectors + field selectors |
| Preemption | No native preemption | Priority-based preemption |
| Custom schedulers | Not supported | Custom scheduler plugins |
| Topology spread | Via spread strategy | TopologySpreadConstraints |
| Complexity | Simple (3 strategies, 2 constraints) | Complex (many knobs) |
| Optimality | Best-effort, fast | Can be more optimal but slower |

### 11.4 Why Fargate Has No Placement Controls

Fargate is serverless — customers don't manage instances. AWS manages a fleet of hosts:
- Placement is an internal concern (AWS decides which host runs the microVM)
- Customers only specify subnets (for AZ distribution)
- AWS automatically spreads across AZs for availability
- No concept of "instances" from the customer's perspective

---

## 12. Interview Angles

### 12.1 Key Questions

**Q: "A customer wants to run a 100-task service across 3 AZs with maximum availability. What placement configuration?"**

```json
{
    "placementStrategy": [
        { "type": "spread", "field": "attribute:ecs.availability-zone" }
    ]
}
```
This gives ~33/33/34 tasks per AZ. Losing one AZ loses ~33 tasks. The service scheduler replaces them on surviving AZs (if capacity exists).

For even better isolation, add `distinctInstance` constraint — but this requires 100+ instances.

**Q: "A customer is running dev/staging on the same cluster as production. How do you isolate them?"**

Custom attributes + memberOf constraints:
1. Tag instances: `attribute:environment=production` or `attribute:environment=staging`
2. Production service constraint: `memberOf(attribute:environment == production)`
3. Staging service constraint: `memberOf(attribute:environment == staging)`

Alternative: Use separate clusters (simpler but more overhead).

**Q: "What happens if no instance matches the placement constraints?"**

The task stays in `PENDING` state indefinitely. ECS does not relax constraints. The service scheduler continues trying in each reconciliation cycle. The task will only be placed when:
- A matching instance becomes available (scale-up, new instance joins)
- The constraint is removed or updated

This is why constraints are called "binding" — they never compromise.

**Q: "How does ECS decide which tasks to kill during scale-in?"**

Depends on the strategy:
- Spread: Kill tasks from the most-populated dimension (AZ, instance) to maintain balance
- Binpack: Kill tasks from instances with the most free resources (to free up whole instances for termination)
- Random: Random selection

### 12.2 Numbers to Know

| Metric | Value |
|--------|-------|
| Placement strategies available | 3 (spread, binpack, random) |
| Placement constraint types | 2 (distinctInstance, memberOf) |
| Max containers per task definition | 10 |
| Max container instances per cluster | 5,000 |
| Built-in attributes | 10+ (AZ, instance-type, os-type, etc.) |
| Default service strategy | spread by ecs.availability-zone |
| Fargate placement support | None (auto-spread by AZ) |
| ECS Managed Instances strategies | None (auto-spread, constraints only) |

---

*Cross-references:*
- [Cluster Architecture](cluster-architecture.md) — Three-layer model, control plane components
- [Capacity Providers](capacity-providers.md) — How capacity scaling interacts with placement
- [Fargate Architecture](fargate-architecture.md) — Why Fargate has no placement controls
- [Networking and Service Discovery](networking-and-service-discovery.md) — How awsvpc affects placement (ENI limits)
