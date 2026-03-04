# EMR Deployment Modes — Serverless, EKS, and EC2 Compared

> **Companion doc for:** [interview-simulation.md](interview-simulation.md) — Phase 10 (EMR on EKS / Serverless)
> **Last verified:** February 2026 against AWS EMR documentation

---

## Table of Contents

1. [Overview — Three Deployment Modes](#1-overview--three-deployment-modes)
2. [EMR on EC2 — The Original Architecture](#2-emr-on-ec2--the-original-architecture)
3. [EMR on EKS — Kubernetes-Native Execution](#3-emr-on-eks--kubernetes-native-execution)
4. [EMR on EKS — Architecture Deep Dive](#4-emr-on-eks--architecture-deep-dive)
5. [EMR on EKS — Virtual Clusters and Job Runs](#5-emr-on-eks--virtual-clusters-and-job-runs)
6. [EMR Serverless — No Cluster Management](#6-emr-serverless--no-cluster-management)
7. [EMR Serverless — Architecture Deep Dive](#7-emr-serverless--architecture-deep-dive)
8. [EMR Serverless — Pre-Initialized Capacity](#8-emr-serverless--pre-initialized-capacity)
9. [EMR Serverless — Auto-Scaling Behavior](#9-emr-serverless--auto-scaling-behavior)
10. [Three-Way Comparison](#10-three-way-comparison)
11. [Decision Framework — Which Mode to Choose](#11-decision-framework--which-mode-to-choose)
12. [Migration Patterns](#12-migration-patterns)
13. [Design Decision Analysis](#13-design-decision-analysis)
14. [Interview Angles](#14-interview-angles)

---

## 1. Overview — Three Deployment Modes

Amazon EMR offers three deployment modes, each representing a different point on the control-vs-simplicity spectrum:

```
MORE CONTROL                                              MORE SIMPLICITY
◄──────────────────────────────────────────────────────────────────────►

  EMR on EC2            EMR on EKS            EMR Serverless
  ┌──────────┐          ┌──────────┐          ┌──────────┐
  │ You manage│          │ You manage│          │ AWS      │
  │ clusters, │          │ EKS, EMR  │          │ manages  │
  │ nodes,    │          │ runs as   │          │ everything│
  │ scaling,  │          │ K8s pods  │          │          │
  │ YARN      │          │           │          │ You just │
  │           │          │           │          │ submit   │
  │           │          │           │          │ jobs     │
  └──────────┘          └──────────┘          └──────────┘

  Full control          K8s ecosystem          Zero ops
  Most complex          Moderate               Simplest
```

### The Evolution Story

```
2009: EMR launches → EMR on EC2 (only option)
      "Managed Hadoop in the cloud"
      Still need to: pick instance types, size clusters, manage YARN

2020: EMR on EKS launches
      "Run Spark on your existing Kubernetes cluster"
      Share compute between Spark and other K8s workloads

2022: EMR Serverless launches
      "Just submit jobs — we handle everything else"
      No cluster, no nodes, no YARN, no Kubernetes
```

---

## 2. EMR on EC2 — The Original Architecture

### What You Get

```
┌─────────────────────────────────────────────────┐
│              EMR CONTROL PLANE                   │
│  (managed by AWS — multi-tenant, 99.99% SLA)   │
└──────────────────────┬──────────────────────────┘
                       │
        Provisions and manages
                       │
                       ▼
┌─────────────────────────────────────────────────┐
│              YOUR CLUSTER                        │
│                                                  │
│  ┌──────────┐  ┌──────────┐  ┌──────────┐      │
│  │ Primary  │  │ Core     │  │ Task     │      │
│  │ Node(s)  │  │ Nodes    │  │ Nodes    │      │
│  │          │  │          │  │          │      │
│  │ NameNode │  │ DataNode │  │ Executor │      │
│  │ RM       │  │ NM       │  │ NM       │      │
│  │ Hive     │  │ HDFS     │  │ No HDFS  │      │
│  └──────────┘  └──────────┘  └──────────┘      │
│                                                  │
│  YARN │ HDFS │ Spark │ Hive │ Presto │ HBase   │
│  Full open-source stack on your EC2 instances    │
└─────────────────────────────────────────────────┘
```

### What You're Responsible For

| Responsibility | Details |
|---|---|
| **Instance type selection** | Choose the right instance family and size for your workload |
| **Cluster sizing** | Set initial node counts and scaling boundaries |
| **Scaling configuration** | Configure managed scaling or custom auto-scaling |
| **YARN configuration** | Tune scheduler, container sizes, queue capacity |
| **Spot strategy** | Choose instance fleet types, allocation strategies |
| **HDFS management** | Configure replication factor, EBS volumes |
| **Bootstrap actions** | Install custom software, configure environment |
| **Security** | VPC, security groups, encryption, IAM roles |
| **Monitoring** | CloudWatch metrics, application UIs, logs |

### What AWS Manages

| Responsibility | Details |
|---|---|
| **EC2 provisioning** | Launch and terminate instances |
| **Framework installation** | Install Spark, Hive, Presto from release labels |
| **Managed scaling** | Auto-scale based on YARN metrics |
| **Spot replacement** | Request replacement capacity when Spot interrupted |
| **Cluster lifecycle** | State transitions, step execution, termination |
| **Patch management** | EMR release updates, AMI updates |

### Best Use Cases

- Full control over cluster configuration
- HBase (requires HDFS and long-running cluster)
- Presto/Trino interactive analytics (non-YARN workload)
- Complex multi-framework clusters (Spark + Hive + Presto on same cluster)
- Specific hardware requirements (GPU instances, high-memory instances)
- Existing Hadoop/YARN expertise

---

## 3. EMR on EKS — Kubernetes-Native Execution

### What It Is

EMR on EKS allows you to submit Spark jobs to an **Amazon EKS (Kubernetes) cluster**. Instead of YARN managing resources, **Kubernetes is the resource orchestrator**. Spark drivers and executors run as Kubernetes pods.

### Why It Exists

Organizations that have already standardized on Kubernetes want to:
1. **Consolidate compute** — run Spark alongside web services, ML training, and other workloads on the same K8s cluster
2. **Use K8s tooling** — leverage existing monitoring, logging, and deployment pipelines
3. **Avoid managing two schedulers** — YARN (for Spark) + K8s (for everything else) is redundant

### Key Concepts

| Concept | Definition |
|---|---|
| **EKS Cluster** | Your Amazon EKS cluster (you provision and manage it) |
| **Virtual Cluster** | A logical registration of EMR with a Kubernetes namespace. Maps 1:1 to a namespace. Consumes no additional resources. |
| **Managed Endpoint** | An interactive endpoint for running notebooks against the virtual cluster |
| **Job Run** | A Spark job submitted to a virtual cluster. Runs as Kubernetes pods. |
| **Release Label** | EMR version that determines Spark version and optimizations |

---

## 4. EMR on EKS — Architecture Deep Dive

### How a Job Run Works

```
1. User submits job
   aws emr-containers start-job-run --virtual-cluster-id ...
              │
              ▼
2. EMR on EKS Control Plane
   ├── Validates request
   ├── Creates Spark driver pod in the registered K8s namespace
   └── Sets resource requests/limits based on job config
              │
              ▼
3. Spark Driver Pod (in your EKS cluster)
   ├── Requests executor pods from Kubernetes scheduler
   ├── K8s scheduler places pods on available nodes
   └── Executors run as K8s pods alongside other workloads
              │
              ▼
4. Job Execution
   ├── Driver coordinates tasks across executor pods
   ├── Data read from/written to S3
   ├── Shuffle data: local pod storage (emptyDir or EBS-backed)
   └── Pods terminated when job completes
```

### Resource Sharing with Kubernetes

```
EKS Cluster (shared compute)
├── Namespace: spark-analytics
│   └── EMR Virtual Cluster registered here
│       ├── Spark Driver Pod (1 vCPU, 4 GB)
│       ├── Spark Executor Pod (4 vCPU, 16 GB) × 20
│       └── Spark Executor Pod (4 vCPU, 16 GB) × ...
│
├── Namespace: web-services
│   ├── API Server Pod × 3
│   └── Web Frontend Pod × 5
│
├── Namespace: ml-training
│   ├── Training Pod (8 vCPU, 32 GB GPU)
│   └── Data Preprocessing Pod × 4
│
└── Kubernetes scheduler manages ALL pods across ALL namespaces
```

### Kubernetes vs YARN

| Dimension | YARN (EMR on EC2) | Kubernetes (EMR on EKS) |
|---|---|---|
| **Resource unit** | Container (memory + vcores) | Pod (CPU requests/limits + memory requests/limits) |
| **Scheduler** | Capacity Scheduler or Fair Scheduler | K8s scheduler (bin-packing or spread) |
| **Node labels** | EMR-managed (CORE, ON_DEMAND) | Standard K8s node selectors, taints, tolerations |
| **Spot handling** | YARN node labels protect AMs | K8s PDB + node affinity for driver pods |
| **Storage** | HDFS on DataNodes | No HDFS — S3 only, emptyDir for shuffle |
| **Multi-tenancy** | YARN queues | K8s namespaces + ResourceQuotas |
| **Monitoring** | YARN UI, CloudWatch | Prometheus, Grafana, CloudWatch Container Insights |

### Advantages Over EMR on EC2

| Advantage | Details |
|---|---|
| **Resource consolidation** | Share EKS cluster compute across Spark, ML, web services |
| **K8s ecosystem** | Use existing monitoring, CI/CD, security tooling |
| **Multi-framework** | Run different Spark versions in different namespaces |
| **Faster job startup** | No cluster provisioning (EKS cluster already running) |
| **Finer resource control** | K8s requests/limits, priority classes, preemption |
| **Node diversity** | K8s Karpenter for dynamic node provisioning and Spot management |

### Limitations

| Limitation | Details |
|---|---|
| **Spark only** | No Hive, Presto, HBase, Flink (those require EMR on EC2) |
| **No HDFS** | Must use S3 for all storage; shuffle uses local pod storage |
| **K8s expertise required** | Must manage EKS cluster, node groups, networking |
| **No YARN features** | No managed scaling, no graceful decommissioning — use K8s equivalents |
| **Shuffle performance** | Pod-local storage may be slower than instance store HDFS |

---

## 5. EMR on EKS — Virtual Clusters and Job Runs

### Virtual Cluster Lifecycle

```
┌──────────┐     ┌──────────┐     ┌──────────┐     ┌──────────┐
│ RUNNING  │────▶│TERMINATING│───▶│TERMINATED │     │ ARRESTED │
└──────────┘     └──────────┘     └──────────┘     └──────────┘
                                                     (perm error)
```

### Creating a Virtual Cluster

```bash
# Register EMR with a Kubernetes namespace
aws emr-containers create-virtual-cluster \
  --name my-spark-cluster \
  --container-provider '{
    "id": "my-eks-cluster",
    "type": "EKS",
    "info": {
      "eksInfo": {
        "namespace": "spark-analytics"
      }
    }
  }'
```

**Key insight**: A virtual cluster is just a registration — it consumes no compute resources. It tells EMR "you're allowed to run jobs in this Kubernetes namespace."

### Submitting a Job Run

```bash
aws emr-containers start-job-run \
  --virtual-cluster-id vc-abc123 \
  --name my-spark-job \
  --execution-role-arn arn:aws:iam::123456789012:role/EMRonEKS-role \
  --release-label emr-6.15.0-latest \
  --job-driver '{
    "sparkSubmitJobDriver": {
      "entryPoint": "s3://my-bucket/scripts/my_job.py",
      "sparkSubmitParameters": "--conf spark.executor.instances=10 --conf spark.executor.memory=4G"
    }
  }'
```

---

## 6. EMR Serverless — No Cluster Management

### What It Is

EMR Serverless is the most abstracted deployment mode. You don't manage clusters, nodes, YARN, or Kubernetes. You create an **application** (a logical grouping for your jobs) and submit **job runs**. EMR Serverless automatically provisions workers, executes the job, and releases resources.

### Core Concepts

```
┌──────────────────────────────────────────┐
│        EMR SERVERLESS APPLICATION         │
│                                          │
│  • Created once, runs many jobs          │
│  • Specifies framework (Spark or Hive)   │
│  • Specifies EMR release version         │
│  • Has optional pre-initialized capacity │
│  • Has maximum capacity limit            │
│  • Runs in AWS-managed VPC               │
│                                          │
│  ┌─────────────────────────────────────┐ │
│  │ Job Run #1 (completed)              │ │
│  └─────────────────────────────────────┘ │
│  ┌─────────────────────────────────────┐ │
│  │ Job Run #2 (running)                │ │
│  │   Driver: 2 vCPU, 4 GB             │ │
│  │   Executors: 50 × 4 vCPU, 8 GB     │ │
│  │   Auto-scaled by EMR Serverless     │ │
│  └─────────────────────────────────────┘ │
│  ┌─────────────────────────────────────┐ │
│  │ Job Run #3 (pending)                │ │
│  └─────────────────────────────────────┘ │
└──────────────────────────────────────────┘
```

### What You're Responsible For

| Responsibility | Details |
|---|---|
| **Create application** | Specify framework, release label, capacity limits |
| **Submit jobs** | Provide Spark/Hive script, configuration, IAM role |
| **Set capacity limits** | Maximum vCPU and memory |
| **Optionally configure pre-initialized capacity** | Warm pool for fast startup |
| **Data access** | S3 paths and IAM permissions |

### What AWS Manages

| Responsibility | Details |
|---|---|
| **Worker provisioning** | Automatically provisions compute for each job |
| **Auto-scaling** | Scales workers up/down per job stage |
| **Resource isolation** | Each application runs in a dedicated VPC |
| **Framework installation** | Pre-configured Spark/Hive runtime |
| **Worker lifecycle** | Provision, setup, execute, decommission |
| **Multi-AZ** | Automatically distributes workers across AZs |

### Supported Frameworks

| Framework | Worker Types |
|---|---|
| **Apache Spark** | DRIVER + EXECUTOR |
| **Apache Hive** | DRIVER + TEZ_TASK |

**Not supported**: Presto/Trino, HBase, Flink, MapReduce (use EMR on EC2 for these)

---

## 7. EMR Serverless — Architecture Deep Dive

### How a Job Run Works

```
1. User submits job run
   aws emr-serverless start-job-run \
     --application-id app-abc123 \
     --execution-role-arn ... \
     --job-driver '{...}'
              │
              ▼
2. EMR Serverless Control Plane
   ├── Validates request (IAM, capacity limits)
   ├── Computes required resources for job
   └── Provisions workers
              │
              ▼
3. Worker Provisioning
   ├── Download container images
   ├── Provision driver worker
   ├── Provision executor workers
   └── Setup networking (VPC, S3 access)
              │
              ▼
4. Job Execution
   ├── Spark driver runs in driver worker
   ├── Executors process data from S3
   ├── EMR Serverless auto-scales executors per stage:
   │   Stage 1: 10 executors (scan phase)
   │   Stage 2: 50 executors (heavy computation)
   │   Stage 3: 5 executors (write results)
   └── Output written to S3
              │
              ▼
5. Cleanup
   ├── Excess workers released
   ├── Pre-initialized workers return to warm pool
   └── Job run marked COMPLETED
```

### Isolation Model

Each EMR Serverless application runs in its own **AWS-managed VPC**:

```
AWS Account
├── EMR Serverless Application A
│   └── Dedicated VPC (managed by AWS)
│       ├── Worker 1 (driver)
│       ├── Worker 2 (executor)
│       └── Worker 3 (executor)
│
├── EMR Serverless Application B
│   └── Dedicated VPC (different from A)
│       ├── Worker 1 (driver)
│       └── Worker 2 (executor)
│
└── Your VPC (peered for data access if needed)
```

Each application gets security isolation — one application's workers cannot communicate with another's.

---

## 8. EMR Serverless — Pre-Initialized Capacity

### The Cold Start Problem

Without pre-initialized capacity, every job run incurs startup latency:

```
WITHOUT pre-initialized capacity:
  Submit job → Provision workers (~2-5 min) → Execute → Release

WITH pre-initialized capacity:
  Submit job → Workers already ready (~seconds) → Execute → Return to pool
```

### Configuration

```bash
aws emr-serverless create-application \
  --type "SPARK" \
  --name "my-spark-app" \
  --release-label emr-6.6.0 \
  --initial-capacity '{
    "DRIVER": {
      "workerCount": 5,
      "workerConfiguration": {
        "cpu": "2vCPU",
        "memory": "4GB"
      }
    },
    "EXECUTOR": {
      "workerCount": 50,
      "workerConfiguration": {
        "cpu": "4vCPU",
        "memory": "8GB"
      }
    }
  }' \
  --maximum-capacity '{
    "cpu": "400vCPU",
    "memory": "1024GB"
  }'
```

### Key Properties

| Property | Value |
|---|---|
| **Worker types (Spark)** | DRIVER and EXECUTOR |
| **Worker types (Hive)** | DRIVER and TEZ_TASK |
| **Idle timeout** | 15 minutes (default); configurable |
| **Auto-stop** | Application stops after idle timeout; restarts on next job submission |
| **Billing** | Pre-initialized workers are billed even when idle |
| **Scaling beyond pool** | Jobs that need more workers than pre-initialized will auto-scale up to `maximum-capacity` |
| **Pool return** | After a job completes, workers return to the pre-initialized pool (up to `initialCapacity` count) |
| **Configuration changes** | Only allowed when application is in CREATED or STOPPED state |

### Memory Overhead Gotcha

Spark adds a configurable memory overhead (default: 10%) to driver and executor memory requests. Pre-initialized worker memory must be **greater than** the job's memory + overhead:

```
Job requests executor memory: 4 GB
Overhead: 10% → 0.4 GB
Actual need: 4.4 GB

Pre-initialized worker memory: 4 GB → WILL NOT be used (too small)
Pre-initialized worker memory: 5 GB → Will be used (sufficient)
```

**Best practice**: Align pre-initialized worker sizes with your job's actual resource requests including overhead.

### Cost Implications

```
Pre-initialized capacity cost:
  5 drivers × (2 vCPU × $0.052624/hr + 4 GB × $0.0057785/hr) = ~$0.64/hr
  50 executors × (4 vCPU × $0.052624/hr + 8 GB × $0.0057785/hr) = ~$12.84/hr
  Total: ~$13.48/hr while idle
  [UNVERIFIED — prices may vary by region and over time]

vs. Cold start cost:
  2-5 minutes per job startup × N jobs/day
  If 50 jobs/day with 3-min cold start: 150 min wasted per day
```

---

## 9. EMR Serverless — Auto-Scaling Behavior

### Per-Stage Scaling

Unlike EMR on EC2 (cluster-level scaling), EMR Serverless scales workers **per job per stage**:

```
Spark Job with 3 Stages:

Stage 1 (Scan):
  ┌─┐ ┌─┐ ┌─┐ ┌─┐ ┌─┐ ┌─┐ ┌─┐ ┌─┐ ┌─┐ ┌─┐
  │E│ │E│ │E│ │E│ │E│ │E│ │E│ │E│ │E│ │E│  10 executors (read from S3)
  └─┘ └─┘ └─┘ └─┘ └─┘ └─┘ └─┘ └─┘ └─┘ └─┘

Stage 2 (Shuffle + Aggregation):
  ┌─┐ ┌─┐ ┌─┐ ┌─┐ ... ┌─┐ ┌─┐ ┌─┐ ┌─┐ ┌─┐  50 executors (heavy compute)
  └─┘ └─┘ └─┘ └─┘     └─┘ └─┘ └─┘ └─┘ └─┘

Stage 3 (Write):
  ┌─┐ ┌─┐ ┌─┐ ┌─┐ ┌─┐
  │E│ │E│ │E│ │E│ │E│  5 executors (write to S3)
  └─┘ └─┘ └─┘ └─┘ └─┘

Workers not needed are released between stages.
```

### Capacity Limits

```json
{
  "maximum-capacity": {
    "cpu": "400vCPU",
    "memory": "1024GB"
  }
}
```

This limits the total resources across all concurrent jobs in the application.

---

## 10. Three-Way Comparison

### Feature Matrix

| Feature | EMR on EC2 | EMR on EKS | EMR Serverless |
|---|---|---|---|
| **Cluster management** | You manage | You manage EKS | AWS manages |
| **Resource scheduler** | YARN | Kubernetes | EMR Serverless internal |
| **Compute** | EC2 instances (your account) | EKS pods (your account) | Serverless workers (AWS account) |
| **Storage** | HDFS + S3 | S3 only | S3 only |
| **Frameworks** | Spark, Hive, Presto, HBase, Flink, MapReduce | Spark only | Spark, Hive |
| **Instance type control** | Full control | Full control (EKS node groups) | No control |
| **Spot integration** | Instance fleets, managed scaling | Karpenter, Spot node groups | AWS manages internally |
| **Scaling** | Managed scaling or custom | K8s HPA, Karpenter | Automatic per-job |
| **Startup time** | 5-15 min (cluster creation) | Seconds (EKS already running) | Seconds (pre-init) or 2-5 min (cold) |
| **Multi-tenant** | YARN queues | K8s namespaces | Separate applications |
| **GPU support** | Yes (P3/P4/G4 instances) | Yes (K8s GPU scheduling) | No |
| **Interactive notebooks** | EMR Studio, JupyterHub | EMR Studio, managed endpoints | EMR Studio |
| **Pricing** | EC2 + EMR fee | EC2 + EKS fee | Pay per vCPU-hour + GB-hour |

### Cost Comparison (Hypothetical 1 TB ETL Job)

| Mode | Cost Components | Estimated Cost |
|---|---|---|
| **EMR on EC2** | 20 × r5.2xlarge Spot × 30 min + EMR fee | ~$8-12 per run |
| **EMR on EKS** | EKS pod resources (same compute) | ~$6-10 per run (no EMR fee, but EKS fee) |
| **EMR Serverless** | vCPU-hours + GB-hours consumed | ~$10-15 per run (serverless premium) |

**Key insight**: EMR Serverless has higher per-unit cost but often lower total cost because you pay only for resources actually used (per-stage scaling, no idle capacity).

### Operational Complexity

```
Operational Effort Scale (1-10):

EMR on EC2:      ████████░░  (8/10)
  Cluster config, YARN tuning, Spot strategy, scaling, security, monitoring

EMR on EKS:      ██████░░░░  (6/10)
  EKS cluster management, namespace config, pod templates, but EMR handles Spark

EMR Serverless:  ██░░░░░░░░  (2/10)
  Create application, submit jobs, done
```

---

## 11. Decision Framework — Which Mode to Choose

### Decision Tree

```
Do you need HBase, Presto, or Flink?
├── Yes → EMR on EC2 (only option for these frameworks)
│
└── No → Do you need GPU instances?
    ├── Yes → EMR on EC2 or EMR on EKS
    │
    └── No → Do you have an existing EKS cluster?
        ├── Yes → Do you want to consolidate compute?
        │   ├── Yes → EMR on EKS
        │   └── No → EMR Serverless (simpler)
        │
        └── No → How important is operational simplicity?
            ├── Critical → EMR Serverless
            └── Flexible → Need HDFS or custom configuration?
                ├── Yes → EMR on EC2
                └── No → EMR Serverless
```

### By Workload Pattern

| Workload Pattern | Recommended Mode | Why |
|---|---|---|
| **Nightly batch ETL** | EMR Serverless | No cluster to manage; pay per job; auto-scales per stage |
| **Interactive analytics (Presto)** | EMR on EC2 | Presto requires YARN or dedicated cluster |
| **HBase real-time serving** | EMR on EC2 | HBase needs HDFS and long-running cluster |
| **ML training with GPU** | EMR on EC2 or EMR on EKS | GPU instance access |
| **Shared compute with microservices** | EMR on EKS | Consolidate Spark + web services on same K8s cluster |
| **Streaming (Flink)** | EMR on EC2 | Flink on YARN with long-running cluster |
| **Ad-hoc data exploration** | EMR Serverless + pre-initialized capacity | Fast startup, no cluster management |
| **CI/CD pipeline data tests** | EMR Serverless | Each test run is a job; no cluster overhead |
| **Multi-version Spark** | EMR on EKS | Different Spark versions in different K8s namespaces |

---

## 12. Migration Patterns

### EMR on EC2 → EMR Serverless

The most common migration path:

```
BEFORE (EMR on EC2):
  aws emr create-cluster ... → submit step → wait → terminate

AFTER (EMR Serverless):
  aws emr-serverless start-job-run ... → wait → done
```

**What changes:**
- No cluster configuration (instance types, YARN config, bootstrap actions)
- Replace `spark-submit --master yarn` with Serverless job driver config
- Change data paths if any used HDFS (all must be S3 now)
- Remove HDFS-dependent logic (HBase, HDFS temp tables)
- Remove custom scaling logic (Serverless scales automatically)

**What stays the same:**
- Spark application code (PySpark, Scala, Java)
- S3 data paths
- IAM permissions structure

### EMR on EC2 → EMR on EKS

```
BEFORE (EMR on EC2):
  YARN manages containers, HDFS for shuffle

AFTER (EMR on EKS):
  K8s manages pods, emptyDir/EBS for shuffle, S3 for everything else
```

**What changes:**
- Cluster provisioning → EKS cluster + virtual cluster registration
- YARN config → K8s pod templates, resource requests/limits
- Managed scaling → Karpenter or K8s cluster autoscaler
- YARN node labels → K8s node selectors, taints, tolerations
- Security groups → K8s network policies
- CloudWatch monitoring → Container Insights + Prometheus

---

## 13. Design Decision Analysis

### Decision 1: Why Three Modes Instead of One?

| Alternative | Pros | Cons |
|---|---|---|
| **Only EMR on EC2** | Comprehensive, supports all frameworks | Over-provisioned for simple batch jobs; operational burden |
| **Only EMR Serverless** | Simplest possible UX | Can't support HBase, Presto, Flink, GPU, HDFS, custom scheduling |
| **Three modes** ← AWS's choice | Right tool for each use case; customers can migrate gradually | Fragmented product surface; three sets of APIs/docs/expertise |

**Why three modes**: Different customers have fundamentally different needs. A startup running nightly Spark ETL should not need to learn YARN configuration. An enterprise running HBase + Presto + Spark needs full cluster control. A platform team on Kubernetes wants K8s-native execution. One size doesn't fit all.

### Decision 2: Why Isn't HDFS Available on EMR Serverless?

| Alternative | Pros | Cons |
|---|---|---|
| **HDFS on Serverless** | Data locality for shuffle, HBase support | Requires managing DataNodes, complicates serverless abstraction, breaks the "no infrastructure" promise |
| **S3-only on Serverless** ← EMR's choice | True serverless — no persistent infrastructure | No data locality; shuffle on remote storage or local worker disk |

**Why S3-only**: The entire point of Serverless is eliminating infrastructure management. HDFS would require persistent DataNodes that outlive individual jobs — the opposite of serverless. S3 as primary storage enables the serverless model. Shuffle uses local worker storage (ephemeral and auto-managed).

### Decision 3: Why Virtual Clusters on EKS Instead of Native K8s Spark Operator?

| Alternative | Pros | Cons |
|---|---|---|
| **K8s Spark Operator** (open source) | Community standard, full K8s native | No EMR optimizations, no managed Spark runtime, manual version management |
| **EMR on EKS virtual clusters** ← AWS's choice | EMR-optimized Spark runtime (2-3x faster [UNVERIFIED]), managed endpoints, EMR Studio integration | AWS-specific, lock-in |

**Why virtual clusters**: EMR on EKS isn't just "Spark on Kubernetes" — it includes the EMR-optimized Spark runtime with performance improvements. Virtual clusters provide a clean abstraction: register a namespace, submit jobs via the EMR API, get EMR-level monitoring and Studio integration. The virtual cluster itself is free (no resources consumed) — it's just a mapping.

---

## 14. Interview Angles

### Questions an Interviewer Might Ask

**Mode Selection:**
- "When would you choose EMR Serverless over EMR on EC2?"
  - Answer: Serverless for batch ETL, ad-hoc analytics, and any Spark/Hive workload where operational simplicity matters more than fine-grained control. EC2 for HBase, Presto, Flink, GPU workloads, HDFS-dependent workloads, or when you need custom YARN/cluster configuration.

- "What's the trade-off between EMR on EKS and EMR Serverless?"
  - Answer: EMR on EKS gives you K8s ecosystem integration and resource consolidation with other workloads, but you manage the EKS cluster. EMR Serverless gives zero ops but you can't share compute with non-EMR workloads and you lose K8s tooling.

**Architecture:**
- "How does EMR Serverless handle shuffle without HDFS?"
  - Answer: Workers have local storage for shuffle data. Since workers are per-job, shuffle data is co-located with the job's executors. For very large shuffles, this may hit local storage limits, requiring tuning of worker disk size or shuffle partition count.

- "What is a virtual cluster in EMR on EKS?"
  - Answer: A virtual cluster is a logical registration that maps EMR to a Kubernetes namespace. It's a 1:1 namespace mapping that consumes zero additional resources. It tells EMR "submit jobs as pods in this namespace." Multiple virtual clusters can exist on the same EKS cluster.

**Pre-Initialized Capacity:**
- "What problem does pre-initialized capacity solve?"
  - Answer: Cold start latency. Without it, each EMR Serverless job incurs 2-5 minutes of startup (worker provisioning, image download, setup). Pre-initialized capacity maintains warm workers that respond in seconds, at the cost of paying for idle capacity.

**Evolution:**
- "How would you explain the evolution from EMR on EC2 to EMR Serverless?"
  - Answer: It's the standard cloud evolution: from IaaS (you manage EC2 instances) to CaaS (you manage containers on K8s) to serverless (you submit jobs). Each step trades control for simplicity. The underlying Spark runtime remains the same — what changes is how infrastructure is managed.

### Red Flags in Candidate Answers

| Red Flag | Why It's Wrong |
|---|---|
| "EMR Serverless supports all EMR frameworks" | Only Spark and Hive; no Presto, HBase, Flink |
| "EMR on EKS replaces EMR on EC2" | Different capabilities; HBase/Presto/Flink still need EC2 mode |
| "Virtual clusters consume EKS resources" | Virtual clusters are zero-cost logical registrations; only job pods consume resources |
| "Use EMR Serverless for HBase" | HBase requires HDFS and long-running servers; incompatible with serverless model |
| "Pre-initialized capacity is free when idle" | You pay for pre-initialized workers even when no jobs are running |
| "EMR on EKS has HDFS" | No HDFS; all storage is S3 with local pod storage for shuffle |
