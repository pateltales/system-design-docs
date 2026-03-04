# ECS Cluster Architecture — Deep Dive

> Companion doc for [interview-simulation.md](interview-simulation.md)
> Covers: Three-layer architecture, control plane, data plane, ECS agent, state management, cluster components, quotas

---

## Table of Contents

1. [Three-Layer Architecture](#1-three-layer-architecture)
2. [Provisioning Layer](#2-provisioning-layer)
3. [Controller Layer (Control Plane)](#3-controller-layer-control-plane)
4. [Capacity Layer (Data Plane)](#4-capacity-layer-data-plane)
5. [Core Abstractions](#5-core-abstractions)
6. [Cluster Configuration](#6-cluster-configuration)
7. [ECS Container Agent](#7-ecs-container-agent)
8. [State Management](#8-state-management)
9. [Control Plane / Data Plane Decoupling](#9-control-plane--data-plane-decoupling)
10. [Capacity Options — Four Launch Types](#10-capacity-options--four-launch-types)
11. [Cluster Lifecycle](#11-cluster-lifecycle)
12. [Service Quotas and Limits](#12-service-quotas-and-limits)
13. [API Rate Limits](#13-api-rate-limits)
14. [Design Decisions and Trade-offs](#14-design-decisions-and-trade-offs)
15. [Interview Angles](#15-interview-angles)

---

## 1. Three-Layer Architecture

ECS is organized into three distinct layers:

```
┌─────────────────────────────────────────────────────────────────────┐
│                      PROVISIONING LAYER                              │
│                                                                      │
│  Console ─── CLI ─── SDK ─── CDK/CloudFormation ─── Copilot CLI    │
│                                                                      │
│  Customer-facing APIs: CreateCluster, RegisterTaskDefinition,       │
│  CreateService, RunTask, UpdateService, StopTask, etc.              │
└────────────────────────────────┬────────────────────────────────────┘
                                 │
                                 ▼
┌─────────────────────────────────────────────────────────────────────┐
│                      CONTROLLER LAYER (Control Plane)                │
│                                                                      │
│  ┌──────────────────┐  ┌───────────────┐  ┌─────────────────────┐  │
│  │ Service          │  │ Task          │  │ Cluster State       │  │
│  │ Scheduler        │  │ Scheduler     │  │ Manager             │  │
│  │                  │  │               │  │                     │  │
│  │ Reconcile loop:  │  │ RunTask:      │  │ Agent heartbeats    │  │
│  │ desired vs actual│  │ one-off task  │  │ Task state tracking │  │
│  │ count            │  │ placement     │  │ Instance state      │  │
│  └──────────────────┘  └───────────────┘  └─────────────────────┘  │
│                                                                      │
│  ┌──────────────────┐  ┌───────────────┐  ┌─────────────────────┐  │
│  │ Deployment       │  │ Placement     │  │ Capacity Provider   │  │
│  │ Controller       │  │ Engine        │  │ Manager             │  │
│  │                  │  │               │  │                     │  │
│  │ Rolling update   │  │ Strategies +  │  │ Managed scaling     │  │
│  │ Blue/green       │  │ Constraints   │  │ signals to ASG      │  │
│  │ Circuit breaker  │  │ Filtering     │  │                     │  │
│  └──────────────────┘  └───────────────┘  └─────────────────────┘  │
└────────────────────────────────┬────────────────────────────────────┘
                                 │
                                 ▼
┌─────────────────────────────────────────────────────────────────────┐
│                      CAPACITY LAYER (Data Plane)                     │
│                                                                      │
│  ┌───────────────┐  ┌──────────────┐  ┌────────────────────────┐   │
│  │ EC2 Instances  │  │ Fargate      │  │ External Instances     │   │
│  │ (ECS Agent)    │  │ (Managed     │  │ (ECS Anywhere)         │   │
│  │                │  │  MicroVMs)   │  │                        │   │
│  │ Container      │  │              │  │ On-premises or         │   │
│  │ runtime        │  │ No agent     │  │ other clouds           │   │
│  │ (Docker/       │  │ visible to   │  │                        │   │
│  │  containerd)   │  │ customer     │  │ SSM-managed agent      │   │
│  └───────────────┘  └──────────────┘  └────────────────────────┘   │
│                                                                      │
│  ┌───────────────────────────────────────────────────────────────┐  │
│  │ ECS Managed Instances (NEW — recommended)                     │  │
│  │ AWS handles: provisioning, patching, scaling, maintenance     │  │
│  └───────────────────────────────────────────────────────────────┘  │
└─────────────────────────────────────────────────────────────────────┘
```

### Layer Responsibilities

| Layer | Responsibility | Analogy |
|-------|---------------|---------|
| **Provisioning** | Customer-facing interface (APIs, console, CLI) | The front desk |
| **Controller** | Scheduling, state management, deployment orchestration | The brain |
| **Capacity** | Actually running containers on compute resources | The muscles |

---

## 2. Provisioning Layer

### 2.1 Interfaces

| Interface | Description |
|-----------|-------------|
| AWS Management Console | Web UI for cluster/service/task management |
| AWS CLI | `aws ecs` commands |
| AWS SDKs | Programmatic access (Java, Python, Go, etc.) |
| AWS CDK | Infrastructure as Code (synthesizes to CloudFormation) |
| AWS Copilot | Opinionated CLI for containerized apps on ECS |
| CloudFormation | Template-based provisioning |

### 2.2 Key APIs

| API | Purpose | Category |
|-----|---------|----------|
| `CreateCluster` | Create a logical cluster | Cluster |
| `DeleteCluster` | Remove cluster | Cluster |
| `RegisterTaskDefinition` | Register a new task blueprint | Task Definition |
| `DeregisterTaskDefinition` | Mark task def inactive | Task Definition |
| `RunTask` | Launch N one-off tasks | Task |
| `StartTask` | Place task on specific instance | Task |
| `StopTask` | Stop a running task | Task |
| `CreateService` | Create long-running service | Service |
| `UpdateService` | Change desired count, task def, etc. | Service |
| `DeleteService` | Remove a service | Service |
| `RegisterContainerInstance` | Add an EC2/external instance to cluster | Instance |
| `DeregisterContainerInstance` | Remove instance from cluster | Instance |
| `DescribeTasks` | Get task state | Read |
| `ListTasks` | List tasks with filters | Read |
| `DescribeServices` | Get service state | Read |

---

## 3. Controller Layer (Control Plane)

### 3.1 Components [INFERRED]

The ECS control plane is a fully managed AWS service. Its internal architecture is not publicly documented, but based on public re:Invent talks and observable behavior:

```
┌─────────────────────────────────────────────────────────────┐
│                    ECS Control Plane [INFERRED]               │
│                                                               │
│  ┌─────────────────┐     ┌─────────────────┐                │
│  │ Frontend /       │     │ Cluster State    │                │
│  │ API Gateway      │────►│ Store            │                │
│  │                  │     │ (DynamoDB-like)  │                │
│  │ Rate limiting    │     │                  │                │
│  │ AuthN/AuthZ      │     │ Per-task state   │                │
│  └─────────────────┘     │ Per-instance     │                │
│                           │ Per-service      │                │
│                           └────────┬────────┘                │
│                                    │                          │
│  ┌─────────────────┐     ┌────────▼────────┐                │
│  │ Service          │     │ Placement        │                │
│  │ Scheduler        │────►│ Engine           │                │
│  │                  │     │                  │                │
│  │ Reconciliation   │     │ Filter →         │                │
│  │ loop (desired    │     │ Strategy →       │                │
│  │ vs running)      │     │ Constraint →     │                │
│  └─────────────────┘     │ Select           │                │
│                           └──────────────────┘                │
│  ┌─────────────────┐     ┌──────────────────┐                │
│  │ Deployment       │     │ Capacity Provider│                │
│  │ Controller       │     │ Manager          │                │
│  │                  │     │                  │                │
│  │ Rolling update / │     │ Scale-up signals │                │
│  │ Blue-green /     │     │ to ASG           │                │
│  │ Circuit breaker  │     │                  │                │
│  └─────────────────┘     └──────────────────┘                │
└─────────────────────────────────────────────────────────────┘
```

### 3.2 Service Scheduler — Reconciliation Loop

The service scheduler runs continuously, comparing desired state to actual state:

```
Every reconciliation cycle:
┌─────────────────────┐
│ Read desired count   │ (from service definition)
│ Read running count   │ (from cluster state store)
│ Read task health     │ (container + ELB health checks)
└──────────┬──────────┘
           │
           ▼
┌─────────────────────┐
│ Compute delta:       │
│ delta = desired -    │
│         healthy      │
└──────────┬──────────┘
           │
     ┌─────┼─────┐
     │     │     │
   delta  delta  delta
   > 0    = 0   < 0
     │     │     │
     ▼     ▼     ▼
   Launch  Do    Stop
   tasks   nothing excess
   via          tasks
   placement
   engine
```

**Throttle logic**: If tasks repeatedly fail to start (crash loop), the scheduler throttles restart attempts to prevent resource waste. It sends service event messages and resumes normal scheduling after a service update.

### 3.3 Task Health Evaluation

The service scheduler replaces unhealthy tasks following this logic:

1. If `maximumPercent` allows, launch a replacement task first
2. Wait for the replacement to become `HEALTHY`
3. Stop the unhealthy task
4. If `maximumPercent` limits capacity: stop unhealthy tasks one at a time (randomly), then start replacements

---

## 4. Capacity Layer (Data Plane)

### 4.1 Four Capacity Options

| Option | Who Manages Infra | Agent | Best For |
|--------|-------------------|-------|----------|
| **ECS Managed Instances** | AWS (provisioning, patching, scaling) | ECS Agent (AWS-managed) | Simplest EC2 experience |
| **EC2 with ASG** | Customer (instance type, AMI, ASG config) | ECS Agent (customer-managed) | Full control |
| **Fargate** | AWS (serverless) | No visible agent | No server management |
| **ECS Anywhere** | Customer (on-prem/external) | SSM Agent + ECS Agent | Hybrid deployments |

### 4.2 Container Runtimes

| Launch Type | Runtime |
|-------------|---------|
| EC2 (Amazon Linux 2) | Docker or containerd |
| EC2 (Amazon Linux 2023+) | containerd (default) |
| Fargate | containerd inside Firecracker microVM [INFERRED] |
| ECS Anywhere | Docker or containerd |

---

## 5. Core Abstractions

### 5.1 Abstraction Hierarchy

```
┌─────────────────────────────────────────────────────┐
│                    CLUSTER                            │
│  (Logical grouping of infrastructure + services)     │
│                                                      │
│  ┌────────────────────┐  ┌────────────────────────┐ │
│  │  SERVICE A          │  │  SERVICE B              │ │
│  │  desired: 3         │  │  desired: 2             │ │
│  │  task-def: web:5    │  │  task-def: api:12       │ │
│  │                     │  │                         │ │
│  │  ┌─────┐ ┌─────┐   │  │  ┌─────┐ ┌─────┐      │ │
│  │  │Task │ │Task │   │  │  │Task │ │Task │      │ │
│  │  │  1  │ │  2  │   │  │  │  1  │ │  2  │      │ │
│  │  └──┬──┘ └──┬──┘   │  │  └──┬──┘ └──┬──┘      │ │
│  │     │       │       │  │     │       │          │ │
│  │  ┌──▼──┐ ┌──▼──┐   │  │  ┌──▼──┐ ┌──▼──┐      │ │
│  │  │Cntr │ │Cntr │   │  │  │Cntr │ │Cntr │      │ │
│  │  │  A  │ │  A  │   │  │  │  B  │ │  B  │      │ │
│  │  │Cntr │ │Cntr │   │  │  └─────┘ └─────┘      │ │
│  │  │  B  │ │  B  │   │  │                         │ │
│  │  └─────┘ └─────┘   │  └────────────────────────┘ │
│  └────────────────────┘                              │
│                                                      │
│  Infrastructure:                                     │
│  ┌──────────┐ ┌──────────┐ ┌──────────┐             │
│  │EC2 Inst 1│ │EC2 Inst 2│ │Fargate   │             │
│  │(c5.xlarge)│ │(m5.large)│ │(managed) │             │
│  └──────────┘ └──────────┘ └──────────┘             │
└─────────────────────────────────────────────────────┘
```

### 5.2 Abstraction Definitions

| Abstraction | Definition | Lifecycle |
|-------------|-----------|-----------|
| **Cluster** | Logical grouping of infrastructure and services | Long-lived, per-region |
| **Task Definition** | Versioned JSON blueprint (image, CPU, memory, ports, volumes, roles) | Immutable revisions, up to 1,000,000 per family |
| **Task** | Running instantiation of a task definition | Short-lived (batch) or long-lived (via service) |
| **Service** | Maintains desired count of tasks, handles replacements | Long-lived, reconciliation loop |
| **Container Instance** | EC2 or external instance registered to a cluster | One instance → one cluster |

### 5.3 Task Definition — The Blueprint

A task definition is a JSON document that specifies:

| Parameter | Description | Example |
|-----------|-------------|---------|
| **Family** | Logical name for the task definition | `web-app` |
| **Container definitions** | 1–10 containers (image, CPU, memory, ports, env vars) | nginx, redis sidecar |
| **CPU / Memory** | Task-level resource allocation | 1 vCPU, 2 GB |
| **Network mode** | `awsvpc`, `bridge`, `host`, `none` | `awsvpc` |
| **Task role** | IAM role for the application code | S3 read, DynamoDB write |
| **Execution role** | IAM role for the ECS agent (pull image, send logs) | ECR pull, CloudWatch logs |
| **Volumes** | EFS, EBS, bind mounts, Docker volumes | EFS for shared storage |
| **Essential containers** | Which containers must be running for the task to be healthy | `true` for the main app |
| **Restart policy** | Whether to restart non-essential containers | Sidecar restart on failure |
| **Logging** | Log driver configuration (awslogs, fluentd, etc.) | CloudWatch Logs |
| **Launch type compatibility** | Fargate, EC2, or both | `["FARGATE", "EC2"]` |

**Two IAM roles — why?**
- **Task role**: Assumed by the application container. Grants access to AWS services (S3, DynamoDB, etc.)
- **Execution role**: Assumed by the ECS agent. Grants ability to pull images from ECR, push logs to CloudWatch, retrieve secrets from Secrets Manager

This separation follows least-privilege: the agent doesn't get application permissions, and the application doesn't get agent permissions.

### 5.4 Service — The Long-Running Manager

| Parameter | Description | Default |
|-----------|-------------|---------|
| **Desired count** | How many tasks to keep running | — |
| **Minimum healthy percent** | Floor during deployments | 100% |
| **Maximum percent** | Ceiling during deployments | 200% |
| **Deployment configuration** | Rolling update or CodeDeploy blue/green | Rolling |
| **Circuit breaker** | Rollback if deployment fails | Disabled |
| **Load balancer** | ALB/NLB/GLB target group | Optional |
| **Placement strategy** | spread, binpack, random | spread by AZ |
| **Placement constraints** | distinctInstance, memberOf | None |
| **Capacity provider strategy** | Which capacity providers to use | Cluster default |
| **Service Connect** | Service mesh configuration | Optional |
| **Service discovery** | Cloud Map DNS registration | Optional |

---

## 6. Cluster Configuration

### 6.1 Cluster Settings

| Setting | Description |
|---------|-------------|
| **Container Insights** | Automated metric/log collection via CloudWatch (additional cost) |
| **Default capacity provider strategy** | Which capacity providers are used when no strategy is specified |
| **Service Connect default namespace** | Namespace for service-to-service communication |
| **Execute Command** | Enable/disable `ecs exec` into running containers |
| **Managed storage** | Encryption configuration for Fargate ephemeral storage |

### 6.2 Cluster States

```
ACTIVE ──────────────────── Ready to accept tasks
   │
   ├── PROVISIONING ────── Capacity providers creating resources
   │       │
   │       ├── ACTIVE ──── Resources created successfully
   │       └── FAILED ──── Resources failed to create
   │
   └── DEPROVISIONING ──── Capacity providers deleting resources
           │
           └── INACTIVE ── Cluster deleted (temporarily discoverable)
```

### 6.3 Cluster Constraints

- An instance can only be registered to **one cluster at a time**
- A cluster is **region-specific** — cannot span regions
- A cluster can contain a **mix** of EC2, Fargate, Managed Instances, and External capacity
- A capacity provider strategy can only contain **one type** of provider (cannot mix Fargate with ASG in a single strategy)

---

## 7. ECS Container Agent

### 7.1 What the Agent Does

The ECS container agent runs on each EC2 container instance and is responsible for:

```
┌───────────────────────────────────────────────────────────────────┐
│                     EC2 Container Instance                         │
│                                                                    │
│  ┌─────────────────────────────────────────────────────────────┐  │
│  │                    ECS Agent                                 │  │
│  │                                                              │  │
│  │  ┌──────────────┐  ┌──────────────┐  ┌────────────────┐    │  │
│  │  │ Heartbeat    │  │ Task         │  │ Resource       │    │  │
│  │  │              │  │ Lifecycle    │  │ Reporting      │    │  │
│  │  │ Send status  │  │              │  │                │    │  │
│  │  │ to control   │  │ Start/stop   │  │ CPU, memory,   │    │  │
│  │  │ plane every  │  │ containers   │  │ ports, GPU     │    │  │
│  │  │ ~30s         │  │ per control  │  │ available on   │    │  │
│  │  │ [INFERRED]   │  │ plane cmds   │  │ this instance  │    │  │
│  │  └──────────────┘  └──────────────┘  └────────────────┘    │  │
│  │                                                              │  │
│  │  ┌──────────────┐  ┌──────────────┐  ┌────────────────┐    │  │
│  │  │ Image Pull   │  │ Log          │  │ Health Check   │    │  │
│  │  │              │  │ Collection   │  │                │    │  │
│  │  │ ECR / Docker │  │              │  │ Container-     │    │  │
│  │  │ Hub          │  │ Forward to   │  │ level health   │    │  │
│  │  │              │  │ CloudWatch / │  │ checks         │    │  │
│  │  │              │  │ Fluentd etc. │  │                │    │  │
│  │  └──────────────┘  └──────────────┘  └────────────────┘    │  │
│  └─────────────────────────────────────────────────────────────┘  │
│                                                                    │
│  ┌─────────────────────────────────────────────────────────────┐  │
│  │               Container Runtime (containerd / Docker)        │  │
│  │                                                              │  │
│  │  ┌──────────┐  ┌──────────┐  ┌──────────┐                  │  │
│  │  │Container │  │Container │  │Container │                  │  │
│  │  │  A       │  │  B       │  │  C       │                  │  │
│  │  └──────────┘  └──────────┘  └──────────┘                  │  │
│  └─────────────────────────────────────────────────────────────┘  │
└───────────────────────────────────────────────────────────────────┘
```

### 7.2 Agent Communication Model [INFERRED]

```
ECS Agent (on instance)              ECS Control Plane
        │                                    │
        │──── RegisterContainerInstance ─────►│  (at startup)
        │                                    │
        │──── Heartbeat (available ──────────►│  (periodic)
        │     resources, task states)        │
        │                                    │
        │◄─── Task placement decision ───────│  (when scheduler places task)
        │                                    │
        │     [Agent starts container]       │
        │                                    │
        │──── Task state update (RUNNING) ──►│
        │                                    │
        │     [Task completes or fails]      │
        │                                    │
        │──── Task state update (STOPPED) ──►│
        │                                    │
```

**Communication model**: The agent uses a **long-polling** model to receive commands from the control plane [INFERRED]. This is push-like from the agent's perspective — it maintains a connection and receives commands when the control plane has work for it.

### 7.3 Agent Failure Behavior

| Scenario | Behavior |
|----------|----------|
| Agent crashes | Running containers **continue running** (container runtime is separate) |
| Agent loses connectivity | Tasks keep running; control plane marks instance as disconnected after timeout |
| Agent reconnects | Sends current state; control plane reconciles |
| Instance terminated | All tasks on instance stop; service scheduler replaces them |

**Key principle**: The agent is an intermediary, not a requirement for running containers. If the agent dies, Docker/containerd keeps containers alive.

---

## 8. State Management

### 8.1 What State ECS Tracks [INFERRED]

```
Cluster State Store:
├── Clusters
│   ├── cluster-id → {name, status, settings, capacity-providers}
│   └── ...
├── Container Instances
│   ├── instance-id → {cluster, status, resources, agent-version, connected}
│   └── ...
├── Task Definitions
│   ├── family:revision → {containers, cpu, memory, network-mode, ...}
│   └── ...
├── Tasks
│   ├── task-id → {cluster, instance-id, task-def, status, health, started-at}
│   └── ...
├── Services
│   ├── service-id → {cluster, task-def, desired-count, running-count,
│   │                  deployment-config, load-balancer, events}
│   └── ...
└── Deployments
    ├── deployment-id → {service, task-def, desired, running, status}
    └── ...
```

### 8.2 Task States

```
PROVISIONING ──► PENDING ──► ACTIVATING ──► RUNNING ──► DEACTIVATING ──► STOPPING ──► STOPPED
                    │                          │
                    └── STOPPED                └── STOPPED
                    (placement failed)         (task failed/killed)
```

| State | Description |
|-------|-------------|
| **PROVISIONING** | Resources being prepared (Fargate: microVM + ENI) |
| **PENDING** | Waiting for container agent to start containers |
| **ACTIVATING** | Containers starting, health checks not yet passing |
| **RUNNING** | All essential containers running and healthy |
| **DEACTIVATING** | Task being drained from load balancer |
| **STOPPING** | Containers being stopped |
| **STOPPED** | Task has stopped (check `stoppedReason` for why) |

### 8.3 Consistency Model

| Operation | Consistency |
|-----------|------------|
| **Scheduling decisions** | Strong consistency — a task is placed on exactly one instance |
| **DescribeTasks** | Eventual consistency — may lag actual state by seconds |
| **ListTasks** | Eventual consistency |
| **Service desired count updates** | Strongly consistent — acknowledged immediately |
| **Task state transitions** | Eventual consistency — agent reports → control plane updates |

---

## 9. Control Plane / Data Plane Decoupling

### 9.1 Why This Matters

```
Scenario: ECS Control Plane Outage

┌─────────────────────────────────────┐
│  Control Plane: DOWN                 │
│  - Cannot create new services        │
│  - Cannot update desired count       │
│  - Cannot schedule new tasks         │
│  - Cannot receive task state updates │
└─────────────────────────────────────┘

┌─────────────────────────────────────┐
│  Data Plane: STILL RUNNING          │
│  ✓ Running tasks continue running   │
│  ✓ Containers serve traffic         │
│  ✓ Load balancer continues routing  │
│  ✓ Container health checks continue │
│  ✗ No new task placements           │
│  ✗ No task replacements on failure  │
│  ✗ No scaling decisions             │
└─────────────────────────────────────┘
```

**Critical insight**: Customer-facing workloads survive control plane outages because:
1. The container runtime (Docker/containerd) manages containers independently
2. The ECS agent manages the local lifecycle independently
3. Load balancers continue routing to healthy containers
4. Only new scheduling decisions require the control plane

### 9.2 Agent-Based vs Agentless Architecture

| Aspect | EC2 (Agent-Based) | Fargate (Agentless) |
|--------|-------------------|---------------------|
| Agent | ECS Agent on each instance | Managed by AWS (not visible) |
| Container runtime | Customer-managed containerd/Docker | AWS-managed containerd in microVM |
| Instance registration | Agent calls RegisterContainerInstance | AWS handles internally |
| Failure detection | Heartbeat timeout | AWS monitors microVM health |
| Patching | Customer responsibility (EC2 AMI) | AWS responsibility |

---

## 10. Capacity Options — Four Launch Types

### 10.1 Comparison Matrix

| Feature | ECS Managed Instances | EC2 (ASG) | Fargate | ECS Anywhere |
|---------|----------------------|-----------|---------|-------------|
| **Who provisions instances?** | AWS | Customer | AWS (serverless) | Customer |
| **Who patches OS?** | AWS | Customer | AWS | Customer |
| **Who scales?** | AWS (auto) | Customer (ASG + capacity provider) | AWS (per task) | Customer |
| **Instance type selection** | AWS optimized | Customer choice | N/A (CPU/memory only) | Customer |
| **GPU support** | Yes | Yes | No (as of early 2025) | Depends on hardware |
| **Pricing** | EC2 pricing | EC2 pricing | Per vCPU-sec + per GB-sec | EC2/on-prem cost |
| **Isolation** | Shared EC2 (multi-task) | Shared EC2 (multi-task) | Firecracker microVM (per task) | Customer managed |
| **Control** | Low | High | None | High |
| **Operational overhead** | Low | High | None | Highest |

### 10.2 When to Use Each

| Use Case | Recommended |
|----------|-------------|
| Most workloads (simplest EC2) | **ECS Managed Instances** |
| Full control over instance types, AMIs, GPU | **EC2 with ASG** |
| No server management, pay per task | **Fargate** |
| Hybrid (on-premises + cloud) | **ECS Anywhere** |
| Cost optimization with Spot | **EC2 ASG** or **Fargate Spot** |
| Burst/infrequent workloads | **Fargate** |
| Large, steady-state workloads | **EC2** (Reserved/Savings Plans) |

---

## 11. Cluster Lifecycle

### 11.1 Creating a Cluster

```bash
# Minimal cluster (Fargate-only)
aws ecs create-cluster --cluster-name production

# Cluster with capacity providers
aws ecs create-cluster \
    --cluster-name production \
    --capacity-providers FARGATE FARGATE_SPOT \
    --default-capacity-provider-strategy \
        capacityProvider=FARGATE,weight=1,base=2 \
        capacityProvider=FARGATE_SPOT,weight=3 \
    --settings name=containerInsights,value=enabled
```

### 11.2 Registering Instances

For EC2 launch type:
1. Launch EC2 instance with ECS-optimized AMI
2. ECS Agent starts automatically
3. Agent calls `RegisterContainerInstance`
4. Control plane adds instance to cluster
5. Instance reports available resources (CPU, memory, ports, GPU)

For ECS Managed Instances:
1. AWS handles all of the above automatically

### 11.3 Deleting a Cluster

Requirements before deletion:
- All services must be deleted (desired count = 0)
- All running tasks must be stopped
- All container instances must be deregistered

---

## 12. Service Quotas and Limits

### 12.1 Core Limits

| Resource | Limit | Adjustable? |
|----------|-------|------------|
| Clusters per account per region | 10,000 | Yes |
| Services per cluster | 5,000 | No |
| Tasks per service | 5,000 | No |
| Container instances per cluster | 5,000 | No |
| Containers per task definition | 10 | No |
| Task definition revisions per family | 1,000,000 | No |
| Capacity providers per cluster | 20 | No |
| Target groups per service | 5 | No |
| Security groups per awsvpcConfiguration | 5 | No |
| Subnets per awsvpcConfiguration | 16 | No |
| Tasks launched per RunTask call | 10 | No |
| Container instances per StartTask call | 10 | No |
| Tags per resource | 50 | No |
| Task definition size | 64 KB | No |
| ECS Exec sessions per container | 1,000 | No |
| Tasks in PROVISIONING state per cluster | 500 | No |
| Classic Load Balancers per service | 1 | No |

### 12.2 Task Launch Rate Limits

| Launch Type | Rate | Notes |
|-------------|------|-------|
| EC2 / External | 500 tasks/min per service per region | — |
| Fargate (most regions) | 500 tasks/min | — |
| Fargate (newer regions) | 125 tasks/min | af-south-1, ap-east-1, ap-northeast-3, etc. |

### 12.3 Fargate Quotas

| Resource | Default | Adjustable? |
|----------|---------|------------|
| Burst launch rate (most regions) | 100 tasks | Yes |
| Sustained launch rate (most regions) | 20 tasks/sec | Yes |
| vCPU resource count | 6 concurrent vCPUs | Yes |
| Burst launch rate (newer regions) | 25 tasks | Yes |
| Sustained launch rate (newer regions) | 5 tasks/sec | Yes |

**Note**: New AWS accounts may start with lower Fargate quotas that automatically increase with usage.

### 12.4 Service Discovery Limit

Services using **AWS Cloud Map** for service discovery have a reduced limit: **1,000 tasks per service** (instead of 5,000) due to Cloud Map quotas.

---

## 13. API Rate Limits

| API Category | Burst Rate | Sustained Rate |
|-------------|-----------|----------------|
| Agent modify | 200 req/sec | 120 req/sec |
| Service modify | 50 req/sec | 5 req/sec |
| Service read | 100 req/sec | 20 req/sec |
| Task definition modify | 20 req/sec | 1 req/sec |
| Cluster read | 50 req/sec | 20 req/sec |

**Important for interview**: The service modify rate limit of **5 req/sec sustained** means that a mass-deployment tool updating hundreds of services must implement backoff and queuing.

---

## 14. Design Decisions and Trade-offs

### 14.1 Why Three Layers?

| Decision | Rationale |
|----------|-----------|
| Separate provisioning from controller | APIs can evolve independently; rate limiting protects controller from API storms |
| Separate controller from capacity | Control plane outages don't kill running workloads; capacity types can be added independently |
| Controller is fully managed | Customers don't need to run etcd/Raft/ZooKeeper; AWS handles HA, backups, upgrades |

### 14.2 Why Agent-Based Architecture (EC2)?

| Alternative | Why Not Chosen |
|-------------|---------------|
| SSH-based (control plane SSHes into instances) | Doesn't scale; requires opening SSH ports; fragile |
| Pull-based (instances poll for work) | Higher latency; thundering herd on control plane |
| Agent with long-polling | **Chosen** — push-like semantics, no open inbound ports, scales well [INFERRED] |

### 14.3 Why Task Definition Is Immutable?

- Each revision is a new version (e.g., `web-app:5` → `web-app:6`)
- Running tasks reference a specific revision — updating the task def doesn't affect running tasks
- Enables safe rollbacks: just point the service back to a previous revision
- Audit trail: every deployed version is preserved

### 14.4 ECS vs Kubernetes Architecture

| Aspect | ECS | Kubernetes |
|--------|-----|------------|
| Control plane | Fully managed by AWS | Self-managed (EKS manages it for you) |
| State store | AWS-managed [INFERRED: DynamoDB-like] | etcd (distributed key-value store) |
| Scheduler | ECS placement engine | kube-scheduler |
| Agent | ECS Agent (per instance) | kubelet (per node) |
| Workload unit | Task (1–10 containers) | Pod (1+ containers) |
| Service mesh | Service Connect (Envoy) | Istio, Linkerd, etc. |
| Extensibility | Limited (AWS-defined abstractions) | Highly extensible (CRDs, operators) |
| Networking | awsvpc, bridge, host | CNI plugins (VPC CNI, Calico, etc.) |

---

## 15. Interview Angles

### 15.1 Key Questions

**Q: "Walk me through what happens when you call RunTask."**

1. API request hits the provisioning layer (authenticated, authorized, rate-limited)
2. Task definition is resolved (family:revision)
3. Controller's placement engine runs the placement pipeline:
   - Filter instances by launch type compatibility
   - Filter by resource requirements (CPU, memory, ports, GPU)
   - Apply placement constraints (distinctInstance, memberOf expressions)
   - Apply placement strategy (spread, binpack, random)
   - Select target instance(s)
4. If Fargate: request capacity from Fargate fleet (provision microVM + ENI)
5. For EC2: send task to ECS agent on selected instance via long-poll response [INFERRED]
6. Agent pulls image, creates container(s), starts them
7. Agent reports task state (RUNNING) to control plane
8. If service integration: register with load balancer target group

**Q: "How does ECS handle a container instance failure?"**

1. Agent stops sending heartbeats
2. Control plane detects heartbeat timeout (marks instance as disconnected)
3. Tasks on the instance are marked STOPPED (with reason: instance deregistered / connectivity lost)
4. Service scheduler's reconciliation loop detects running count < desired count
5. Placement engine selects new instance(s) for replacement tasks
6. New tasks are launched on healthy instances
7. Load balancer deregisters old tasks (connection draining) and registers new tasks

**Q: "Why are there two IAM roles on a task definition?"**

Task role vs execution role — separation of concerns:
- **Task role**: What the application code can do (read S3, write DynamoDB)
- **Execution role**: What the ECS infrastructure can do (pull ECR images, push CloudWatch logs, read Secrets Manager)

A compromised application container with only the task role cannot pull other teams' container images or read secrets it shouldn't access. The execution role is never exposed to application code.

### 15.2 Numbers to Know

| Metric | Value |
|--------|-------|
| Max clusters per account | 10,000 |
| Max services per cluster | 5,000 |
| Max tasks per service | 5,000 (1,000 with service discovery) |
| Max container instances per cluster | 5,000 |
| Max containers per task definition | 10 |
| Max task definition revisions | 1,000,000 per family |
| Task launch rate (EC2/Fargate) | 500/min per service |
| Fargate burst launch rate | 100 tasks (most regions) |
| Task definition max size | 64 KB |
| Capacity providers per cluster | 20 |
| Target groups per service | 5 |

---

*Cross-references:*
- [Task Placement](task-placement.md) — Placement strategies and constraints deep dive
- [Fargate Architecture](fargate-architecture.md) — Firecracker microVM isolation, Fargate capacity
- [Networking and Service Discovery](networking-and-service-discovery.md) — awsvpc, bridge, host modes, Cloud Map
- [Capacity Providers](capacity-providers.md) — Managed scaling, ASG integration
- [Deployment Strategies](deployment-strategies.md) — Rolling update, blue/green, circuit breaker
- [Monitoring and Operations](monitoring-and-operations.md) — Health checks, Container Insights, failure modes
