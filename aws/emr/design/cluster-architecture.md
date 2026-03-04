# EMR Cluster Architecture — Deep Dive

> **Companion doc for:** [interview-simulation.md](interview-simulation.md) — Phase 5 (Cluster Architecture)
> **Last verified:** February 2026 against AWS EMR documentation

---

## Table of Contents

1. [Overview](#1-overview)
2. [The Four-Layer Architecture](#2-the-four-layer-architecture)
3. [Node Types — Primary, Core, Task](#3-node-types--primary-core-task)
4. [Primary Node Deep Dive](#4-primary-node-deep-dive)
5. [Core Node Deep Dive](#5-core-node-deep-dive)
6. [Task Node Deep Dive](#6-task-node-deep-dive)
7. [High Availability — Multi-Primary Clusters](#7-high-availability--multi-primary-clusters)
8. [Application HA Behavior](#8-application-ha-behavior)
9. [Node Communication and Security Groups](#9-node-communication-and-security-groups)
10. [Bootstrap Actions](#10-bootstrap-actions)
11. [Cluster Lifecycle States](#11-cluster-lifecycle-states)
12. [YARN Node Labels — Spot Instance Protection](#12-yarn-node-labels--spot-instance-protection)
13. [Cluster Topologies — Common Configurations](#13-cluster-topologies--common-configurations)
14. [Service Quotas and Limits](#14-service-quotas-and-limits)
15. [Failure Modes and Recovery](#15-failure-modes-and-recovery)
16. [Design Decision Analysis](#16-design-decision-analysis)
17. [Interview Angles](#17-interview-angles)

---

## 1. Overview

An EMR cluster is a collection of EC2 instances organized into **node types** that cooperate to run distributed data processing frameworks. Unlike a raw Hadoop cluster, EMR adds a managed control plane that handles provisioning, configuration, monitoring, and teardown — abstracting away the operational burden of running distributed systems on cloud infrastructure.

### What Makes EMR's Architecture Distinct from Vanilla Hadoop

| Aspect | Raw Hadoop on EC2 | EMR |
|---|---|---|
| **Provisioning** | Manual EC2 launch, AMI baking, Ansible/Chef | API call → cluster in minutes |
| **Framework Installation** | Manual package management | Release labels (emr-7.x) with pre-configured framework versions |
| **Scaling** | Manual instance management | Managed scaling with YARN-aware decisions |
| **Spot Integration** | Manual bid management, no recovery | Native Spot support with capacity-optimized allocation, YARN node labels |
| **Storage** | HDFS only | HDFS + EMRFS (S3 as HDFS-compatible filesystem) |
| **High Availability** | Manual ZooKeeper + NameNode HA setup | Single config: `InstanceCount=3` for primary nodes |
| **Monitoring** | Manual Ganglia/Prometheus setup | CloudWatch integration, application UIs, step status tracking |

### The Core Insight

EMR's architecture separates **cluster management** (control plane) from **workload execution** (data plane). The control plane is a shared, multi-tenant AWS service that manages thousands of customer clusters. The data plane is per-customer EC2 instances running open-source frameworks. This separation means:

- Control plane availability (99.99%) is independent of individual cluster health
- One customer's cluster failure doesn't affect another customer
- The control plane can orchestrate scaling, monitoring, and termination without running on the cluster itself

---

## 2. The Four-Layer Architecture

EMR clusters have four conceptual layers, from bottom to top:

```
┌─────────────────────────────────────────────────────────┐
│                 LAYER 4: APPLICATIONS                    │
│  Hive │ Pig │ Spark SQL │ MLlib │ Spark Streaming       │
│  Presto/Trino │ HBase │ Flink │ Jupyter │ Zeppelin      │
├─────────────────────────────────────────────────────────┤
│              LAYER 3: PROCESSING FRAMEWORKS              │
│        Apache Spark (DAGs + in-memory caching)           │
│        Hadoop MapReduce (map/reduce programming model)   │
│        Apache Tez (optimized DAG execution)              │
├─────────────────────────────────────────────────────────┤
│            LAYER 2: RESOURCE MANAGEMENT                  │
│                     YARN                                 │
│          ResourceManager ←→ NodeManagers                 │
│          Node Labels (CORE / ON_DEMAND)                  │
├─────────────────────────────────────────────────────────┤
│                LAYER 1: STORAGE                          │
│   HDFS (ephemeral, local)   │   EMRFS (S3-backed)       │
│   NameNode + DataNodes      │   S3 as filesystem         │
│   Local filesystem          │                            │
└─────────────────────────────────────────────────────────┘
```

### Layer 1 — Storage

Three storage options available simultaneously on a cluster:

| Storage | Durability | Performance | Use Case |
|---|---|---|---|
| **HDFS** | Ephemeral — lost when cluster terminates | High throughput, data locality | Intermediate shuffle data, temp files, HBase region data |
| **EMRFS (S3)** | 99.999999999% (11 9's) | Higher latency than HDFS, but decoupled | Input data, output data, anything that must survive cluster termination |
| **Local filesystem** | Instance-level — lost if instance terminates | Lowest latency | Scratch space, OS-level temp files, log buffering |

### Layer 2 — Resource Management

YARN (Yet Another Resource Negotiator) manages compute resources across all nodes:
- **ResourceManager** runs on primary node(s) — allocates containers to applications
- **NodeManager** runs on every core and task node — manages containers on that node
- **ApplicationMaster** — per-application process that negotiates resources from ResourceManager

EMR 5.19.0+ adds **node labels** to YARN so application masters are placed only on core or on-demand nodes, protecting against Spot interruption.

### Layer 3 — Processing Frameworks

Frameworks submit applications to YARN:
- **Spark** — dominant framework, uses DAGs and in-memory caching
- **MapReduce** — legacy batch processing
- **Tez** — optimized DAG engine (used by Hive)
- **Flink** — stream processing

### Layer 4 — Applications

Higher-level tools that use the processing frameworks:
- **Hive** — SQL on Hadoop (runs on Tez or Spark)
- **Presto/Trino** — interactive SQL (MPP engine, doesn't use YARN in some configurations)
- **HBase** — column-family NoSQL on HDFS
- **JupyterHub / Zeppelin** — interactive notebooks

---

## 3. Node Types — Primary, Core, Task

This is the most fundamental architectural concept in EMR. Every cluster has exactly three types of nodes, each with distinct responsibilities:

```
                    ┌──────────────────────┐
                    │    PRIMARY NODE(S)    │
                    │                      │
                    │  • HDFS NameNode     │
                    │  • YARN ResManager   │
                    │  • Hive Metastore    │
                    │  • Spark History     │
                    │  • Step Coordinator  │
                    └──────────┬───────────┘
                               │
              ┌────────────────┼────────────────┐
              │                                  │
    ┌─────────▼─────────┐           ┌───────────▼──────────┐
    │    CORE NODES      │           │    TASK NODES         │
    │                    │           │                       │
    │  • HDFS DataNode   │           │  • NO HDFS            │
    │  • YARN NodeMgr    │           │  • YARN NodeMgr       │
    │  • Executors       │           │  • Executors          │
    │  • App Masters     │           │  • NO App Masters     │
    │                    │           │    (via node labels)   │
    └────────────────────┘           └───────────────────────┘
```

### Summary Table

| Property | Primary | Core | Task |
|---|---|---|---|
| **Role** | Cluster management + coordination | Storage (HDFS) + compute | Compute only |
| **HDFS** | NameNode | DataNode (stores blocks) | None |
| **YARN** | ResourceManager | NodeManager + containers | NodeManager + containers |
| **App Masters** | No (except on primary itself) | Yes — via node labels | No — prevented by node labels |
| **Count per cluster** | 1 or 3 | 1+ (one instance group/fleet) | 0+ (up to 48 instance groups, or 1 fleet) |
| **Spot recommended?** | No | Caution (HDFS data loss risk) | Yes |
| **What happens on failure?** | Cluster failure (single-primary) or failover (multi-primary) | HDFS under-replication, potential data loss if too many fail | Job containers re-scheduled to other nodes |
| **Can be scaled?** | No | Yes (add/remove instances) | Yes (add/remove instances) |

### Why Three Types?

The three-type separation is a **cost optimization and fault isolation** design:

1. **Primary**: Runs management services. You need exactly 1 (or 3 for HA). Making this separate means management overhead doesn't compete with workload compute.

2. **Core**: Couples HDFS storage with compute. These nodes store data blocks, so losing them means losing HDFS data. You want these on reliable instances (On-Demand or well-diversified Spot).

3. **Task**: Pure compute. No data stored. Losing a task node only loses in-progress containers that can be re-scheduled. This makes task nodes ideal for Spot Instances — you get cheap compute without risking data.

**The key tradeoff**: Core nodes couple storage and compute (like traditional Hadoop), while the core + task separation lets you decouple elastic compute from stable storage. With EMRFS/S3, you can minimize core nodes (just enough for HDFS shuffle space) and use many task nodes for compute.

---

## 4. Primary Node Deep Dive

The primary node (historically called "master node") is the brain of the cluster. It runs all coordination and management services.

### Services Running on Primary Node

| Service | Purpose | Port |
|---|---|---|
| **HDFS NameNode** | Manages filesystem namespace, block locations | 8020 (IPC), 9870 (Web UI) |
| **YARN ResourceManager** | Allocates containers across the cluster | 8032 (IPC), 8088 (Web UI) |
| **Hive Server2** | Accepts Hive SQL queries | 10000 |
| **Hive Metastore** | Stores table/partition metadata | 9083 |
| **Spark History Server** | Displays completed Spark application history | 18080 |
| **Presto/Trino Coordinator** | Query planning and coordination | 8889 |
| **Ganglia** | Cluster monitoring | 80 (Web) |
| **Livy** | REST API for Spark | 8998 |
| **JupyterHub** | Interactive notebooks | 9443 |
| **Zeppelin** | Interactive notebooks | 8890 |
| **EMR Step Runner** | Executes submitted steps | Internal |

### Primary Node Sizing

The primary node must be sized for:
- **NameNode memory**: ~1 GB per million HDFS blocks (each 128 MB block consumes ~150 bytes of NameNode memory)
- **ResourceManager memory**: Scales with number of running applications and containers
- **Metastore memory**: Scales with number of Hive tables and partitions
- **Network**: All DataNode heartbeats, block reports, and client connections flow through the primary

**Rule of thumb**: Primary node instance should be at least as large as core/task nodes, and often larger for clusters with many nodes or HDFS-heavy workloads.

### Single Point of Failure

In a single-primary cluster:
- If the primary node fails, **the entire cluster is lost**
- YARN cannot schedule new work
- HDFS becomes inaccessible (NameNode down)
- Steps in progress fail
- The only recovery is to create a new cluster

This is why production workloads on long-running clusters should use multi-primary (HA) configuration.

---

## 5. Core Node Deep Dive

Core nodes are the workhorses of the cluster — they provide both HDFS storage and compute capacity.

### Dual Role

```
┌─────────────────────────────────────────┐
│              CORE NODE                   │
│                                         │
│  ┌──────────────┐  ┌─────────────────┐  │
│  │ HDFS DataNode│  │ YARN NodeManager│  │
│  │              │  │                 │  │
│  │ Stores data  │  │ Runs containers │  │
│  │ blocks (3x   │  │ (Spark execu-  │  │
│  │ replicated)  │  │  tors, App     │  │
│  │              │  │  Masters)       │  │
│  └──────────────┘  └─────────────────┘  │
│                                         │
│  Instance Storage / EBS ──► HDFS blocks  │
│  Remaining memory ──► YARN containers    │
└─────────────────────────────────────────┘
```

### HDFS on Core Nodes

- Each core node runs a **DataNode** daemon that manages HDFS blocks on the node's storage
- Blocks are **replicated** across core nodes (default replication factor: 3 for clusters with ≥ 4 nodes, 2 for clusters with ≤ 3 nodes, 1 for single-node clusters)
- Storage can be instance store volumes (ephemeral NVMe SSDs) or EBS volumes
- **All HDFS data is lost when the cluster terminates** — HDFS is ephemeral in EMR

### Why Core Nodes Are Risky with Spot

If a Spot Instance core node is reclaimed:
1. The DataNode on that node goes offline
2. HDFS blocks stored on that node become under-replicated
3. NameNode begins re-replicating blocks from remaining copies to other core nodes
4. If too many core nodes are lost simultaneously, blocks with no remaining copies are **permanently lost**
5. Any ApplicationMaster running on that node dies (but YARN can restart it on another core node)

**Mitigation strategies:**
- Use On-Demand for core nodes in production
- If using Spot for core nodes, use multiple instance types for diversification
- Minimize HDFS usage — store outputs in S3 via EMRFS
- Keep the HDFS replication factor at 3

### Core Node Scaling

- You can **add** core nodes to a running cluster (scale out)
- You can **remove** core nodes (scale in), but EMR uses **graceful decommissioning**:
  - The DataNode is decommissioned first — blocks are migrated to other nodes
  - Only after HDFS blocks are safely replicated does the instance terminate
  - This prevents data loss but makes scale-down slower than scale-up
- There is exactly **one core instance group** (or one core instance fleet) per cluster

### Application Master Placement

Starting from EMR 5.19.0, YARN node labels ensure **ApplicationMasters run only on core nodes** (labeled `CORE`), not on task nodes. This prevents a Spot task node reclamation from killing the ApplicationMaster and failing the entire application.

---

## 6. Task Node Deep Dive

Task nodes are pure compute — no HDFS, no data persistence responsibility.

### What Task Nodes Run

```
┌─────────────────────────────────────────┐
│              TASK NODE                   │
│                                         │
│  ┌─────────────────────────────────────┐│
│  │         YARN NodeManager            ││
│  │                                     ││
│  │  ┌───────────┐  ┌───────────┐       ││
│  │  │ Executor  │  │ Executor  │ ...   ││
│  │  │ Container │  │ Container │       ││
│  │  └───────────┘  └───────────┘       ││
│  │                                     ││
│  │  NO DataNode                        ││
│  │  NO ApplicationMaster (via labels)  ││
│  └─────────────────────────────────────┘│
└─────────────────────────────────────────┘
```

### Why Task Nodes Exist

The fundamental value of task nodes:

1. **Spot-friendly**: Since they store no HDFS data, losing a task node loses only in-progress containers. YARN re-schedules those containers on surviving nodes. No data loss.

2. **Elastic compute**: You can rapidly add/remove task nodes without worrying about HDFS rebalancing. Scale-down is fast because there are no DataNode blocks to migrate.

3. **Cost optimization**: Run core nodes on On-Demand for stability, task nodes on Spot for cheap burst compute. A cluster with 10 core + 100 task (Spot) nodes can be dramatically cheaper than 110 core nodes.

### Task Node Configuration

- **Uniform instance groups**: Up to **48 task instance groups** per cluster, each with a different instance type and Spot bid
- **Instance fleet**: One task instance fleet that can specify up to 30 instance types with automatic diversification
- Task nodes are optional — a cluster can run with only primary + core nodes
- Task node count can be 0

### Spot Interruption Handling

When a Spot task node is reclaimed:
1. YARN NodeManager on that node stops heartbeating
2. ResourceManager marks the node as `LOST` after the timeout
3. All containers on that node are marked `FAILED`
4. **ApplicationMaster** (running on a core node, protected by node labels) detects the failed containers
5. ApplicationMaster requests replacement containers from ResourceManager
6. ResourceManager schedules replacement containers on surviving nodes

**Net effect**: The application continues with a temporary reduction in parallelism. No data is lost, no application fails — only a temporary performance dip.

---

## 7. High Availability — Multi-Primary Clusters

### Overview

Multi-primary clusters use **3 primary nodes** to eliminate the single point of failure. Automatic failover is handled by Apache ZooKeeper.

```
┌──────────────────────────────────────────────────────────────────┐
│                    3-PRIMARY HA CLUSTER                           │
│                                                                  │
│  ┌────────────────┐  ┌────────────────┐  ┌────────────────┐     │
│  │  PRIMARY #1     │  │  PRIMARY #2     │  │  PRIMARY #3     │   │
│  │                 │  │                 │  │                 │   │
│  │  Active         │  │  Standby        │  │  Standby        │   │
│  │  NameNode       │  │  NameNode       │  │  NameNode       │   │
│  │                 │  │                 │  │                 │   │
│  │  Active         │  │  Standby        │  │  Standby        │   │
│  │  ResourceMgr    │  │  ResourceMgr    │  │  ResourceMgr    │   │
│  │                 │  │                 │  │                 │   │
│  │  ZooKeeper      │  │  ZooKeeper      │  │  ZooKeeper      │   │
│  └────────────────┘  └────────────────┘  └────────────────┘     │
│           │                   │                   │              │
│           └───────────────────┼───────────────────┘              │
│                               │                                  │
│               ┌───────────────▼───────────────┐                  │
│               │     CORE + TASK NODES          │                 │
│               │     (unchanged from single-    │                 │
│               │      primary architecture)     │                 │
│               └───────────────────────────────┘                  │
└──────────────────────────────────────────────────────────────────┘
```

### Key Configuration Details

| Property | Value |
|---|---|
| **Number of primary nodes** | Exactly 3 (not 2, not 5) |
| **NameNode HA** | Active/Standby with automatic failover via ZooKeeper |
| **ResourceManager HA** | One active, two standby, automatic failover |
| **ZooKeeper** | Runs on all 3 primary nodes (quorum of 3) |
| **NameNode distribution** | EMR 5.x: 2 of 3 primaries run NameNode. EMR 6.x+: all 3 run NameNode |
| **Network** | Must use VPC (EC2-Classic not supported). Single AZ or multi-AZ with instance fleets |
| **Termination protection** | Automatically enabled — must explicitly disable before terminating |

### Version Requirements

| Configuration Type | Minimum EMR Version |
|---|---|
| **Instance Groups** | EMR 5.23.0+ |
| **Instance Fleets** | EMR 5.36.1, 5.36.2, 6.8.1, 6.9.1, 6.10.1, 6.11.1, 6.12.0+ |

### What Happens When a Primary Node Fails

1. **ZooKeeper detects** the failed node (heartbeat timeout)
2. **Automatic failover** promotes a standby NameNode/ResourceManager to active
3. **EMR control plane** detects the failed instance
4. **EMR provisions a replacement** primary node with the same configuration and bootstrap actions
5. **The replacement joins** as a new standby
6. **Cluster continues running** without interruption throughout this process

### External Service Requirements for HA

When using multi-primary clusters, several services require external databases because local databases on a single primary would be a single point of failure:

| Service | Requirement | Why |
|---|---|---|
| **Hive Metastore** | External MySQL database (PostgreSQL not supported for multi-primary) | Metastore data must survive primary node failure |
| **Hue** | External database in Amazon RDS | User preferences and query history |
| **Oozie** | External database in Amazon RDS | Workflow state and job history |
| **Presto/Trino** | External Hive metastore or AWS Glue Data Catalog | Table metadata |
| **Kerberos** | External KDC (Key Distribution Center) | Authentication tickets |

**Best practice**: Use **AWS Glue Data Catalog** as the Hive metastore for HA clusters — it's fully managed, durable, and shared across clusters.

---

## 8. Application HA Behavior

Not all applications behave identically during a primary node failover. Here's the complete breakdown:

### Full HA Support (Automatic Failover)

| Application | Behavior During Failover | Notes |
|---|---|---|
| **HDFS** | Active NameNode fails → standby promoted automatically | Clients experience brief pause (< 30 sec typically) |
| **YARN** | Active ResourceManager fails → standby promoted | Running containers continue; new submissions briefly delayed |
| **Spark** | Runs in YARN containers; inherits YARN HA | ApplicationMaster runs on core nodes, survives primary failure |
| **Tez** | Runs on YARN; behaves identically to YARN during failover | No special handling needed |
| **Ganglia** | Runs on all 3 primary nodes | Monitoring continues uninterrupted |
| **JupyterHub** | Installed on all 3 primary nodes | Recommend S3 notebook persistence |
| **Livy** | Installed on all 3 primary nodes | Current sessions lost on failover; must create new sessions |
| **Zeppelin** | Installed on all 3 primary nodes | Notes stored in HDFS; interpreter sessions isolated |

### HA with External Dependencies

| Application | Behavior | Requirement |
|---|---|---|
| **Hive** | HA if external metastore configured | External MySQL metastore required |
| **Hue** | Service components recover automatically | External RDS database required |
| **Oozie** | Oozie-server on all 3 primary nodes | External RDS database required |
| **Presto/Trino** | Coordinator runs on 1 primary node; CLI on all 3 | External Hive metastore or Glue Data Catalog |
| **HBase** | Automatic failover to standby | Manual reconnection needed if using REST/Thrift server |
| **Phoenix** | QueryServer on single primary node | All 3 primaries connect to it |

### Flink HA — Special Case

| EMR Version | Flink HA Behavior |
|---|---|
| **EMR 5.27.0 and earlier** | Manual HA config required (checkpointing + ZooKeeper state storage) |
| **EMR 5.28.0+** | Automatic JobManager HA enabled by default |
| **All versions** | JobManager runs as YARN ApplicationMaster on core nodes (not primary) — not directly affected by primary failure |

### No HA Impact (No Daemons)

These applications have no long-running daemons and are unaffected by failover:
- Mahout
- MXNet
- Pig
- TensorFlow
- Sqoop

### Unsupported Features in Multi-Primary Clusters

- EMR Notebooks (use JupyterHub instead)
- One-click persistent Spark History Server
- Persistent application user interfaces

---

## 9. Node Communication and Security Groups

### Two Classes of Security Groups

EMR uses a dual security group model:

```
┌──────────────────────────────────────────────────────────┐
│                     EMR CLUSTER                          │
│                                                          │
│  ┌────────────────────────────────────────────────────┐  │
│  │          EMR-Managed Security Groups                │  │
│  │  (auto-created, auto-configured by EMR)            │  │
│  │                                                    │  │
│  │  ┌────────────────┐    ┌───────────────────────┐   │  │
│  │  │ Primary SG     │    │ Core/Task SG          │   │  │
│  │  │                │◄──►│                       │   │  │
│  │  │ Allows inbound │    │ Allows inbound from   │   │  │
│  │  │ from Core/Task │    │ Primary node          │   │  │
│  │  │ nodes          │    │                       │   │  │
│  │  └────────────────┘    └───────────────────────┘   │  │
│  └────────────────────────────────────────────────────┘  │
│                                                          │
│  ┌────────────────────────────────────────────────────┐  │
│  │          Additional Security Groups (optional)      │  │
│  │  (user-defined, NOT modified by EMR)               │  │
│  │                                                    │  │
│  │  • SSH access (port 22) from admin IPs             │  │
│  │  • Application UI access (custom ports)            │  │
│  │  • Custom firewall rules                           │  │
│  └────────────────────────────────────────────────────┘  │
└──────────────────────────────────────────────────────────┘
```

### EMR-Managed Security Groups

EMR automatically creates and configures two managed security groups:

1. **Primary node security group** (`ElasticMapReduce-master`)
   - Allows all inbound traffic from core/task security group
   - Allows all outbound traffic
   - EMR adds rules as needed for cluster communication

2. **Core/Task node security group** (`ElasticMapReduce-slave`)
   - Allows all inbound traffic from primary security group
   - Allows all inbound traffic from other core/task nodes (same security group)
   - Allows all outbound traffic

**Critical rule**: Do NOT manually edit EMR-managed security groups. EMR adds rules dynamically, and manual edits can break cluster communication.

### Additional Security Groups

User-defined security groups for custom access:
- Only specified at cluster creation time (cannot add later)
- EMR never modifies these groups
- Common use: SSH access, web UI access, VPN connectivity

### Block Public Access

EMR's **Block Public Access** feature (account-level setting) prevents launching clusters with security group rules that allow public access on unauthorized ports. By default, only port 22 (SSH) is allowed for public access.

### Key Communication Patterns

| Source | Destination | Purpose | Protocol |
|---|---|---|---|
| Core/Task → Primary | HDFS operations | Block reports, heartbeats | TCP 8020 |
| Core/Task → Primary | YARN resource requests | Container allocation | TCP 8032 |
| Primary → Core/Task | YARN container launch | Start executors | TCP (dynamic) |
| Core ↔ Core | HDFS replication | Block transfers | TCP 50010 |
| Core/Task ↔ Core/Task | Shuffle | Map output transfer | TCP (dynamic) |
| External → Primary | SSH access | Administration | TCP 22 |
| External → Primary | Web UIs | Monitoring | TCP 8088, 18080, etc. |

---

## 10. Bootstrap Actions

Bootstrap actions are scripts that customize cluster instances before the cluster begins processing data.

### Execution Timeline

```
EC2 Instance Launch
        │
        ▼
┌──────────────────┐
│  Amazon Linux    │  Instance is provisioned with
│  AMI loaded      │  EMR-specific AMI
└────────┬─────────┘
         │
         ▼
┌──────────────────┐
│  BOOTSTRAP       │  Your custom scripts run here
│  ACTIONS         │  (up to 16 actions, sequential)
│  execute         │
└────────┬─────────┘
         │
         ▼
┌──────────────────┐
│  APPLICATION     │  EMR installs Spark, Hive,
│  INSTALLATION    │  Hadoop, etc.
└────────┬─────────┘
         │
         ▼
┌──────────────────┐
│  CLUSTER READY   │  Steps can be submitted
│  (RUNNING state) │
└──────────────────┘
```

### Key Properties

| Property | Value |
|---|---|
| **Maximum per cluster** | 16 bootstrap actions |
| **Execution order** | Sequential, in the order specified |
| **Execution user** | Hadoop user (can use `sudo` for root) |
| **Failure behavior** | Non-zero exit code → instance marked failed |
| **Cluster failure threshold** | Too many instance bootstrap failures → cluster terminates |
| **Shutdown action timeout** | 60 seconds max for shutdown scripts |
| **Re-execution on scaling** | Bootstrap actions run on newly added nodes |

### Common Bootstrap Action Use Cases

1. **Install additional software**: Python libraries, custom JARs, system packages
2. **Configure environment**: Set environment variables, create directories
3. **Customize Hadoop/Spark config**: Override default configurations
4. **Install monitoring agents**: DataDog, Splunk, custom agents
5. **Set up security**: Install certificates, configure Kerberos keytabs
6. **Pre-stage data**: Copy reference data to local disk for fast access

### Bootstrap Action Script Example

```bash
#!/bin/bash
# Install Python libraries for data science
sudo pip3 install pandas numpy scikit-learn boto3

# Create custom log directory
sudo mkdir -p /var/log/custom-app
sudo chown hadoop:hadoop /var/log/custom-app

# Copy config from S3
aws s3 cp s3://my-bucket/config/app.conf /home/hadoop/app.conf

# Install custom monitoring agent
sudo yum install -y amazon-cloudwatch-agent
```

### Shutdown Actions

Scripts placed in `/mnt/var/lib/instance-controller/public/shutdown-actions/` execute when the cluster terminates:
- Execute in parallel
- **60-second timeout** — must complete within 60 seconds
- Not guaranteed to run if the node terminates due to an error
- The directory doesn't exist by default on EMR 4.0+ — bootstrap actions must create it

---

## 11. Cluster Lifecycle States

An EMR cluster transitions through well-defined states:

```
STARTING ──► BOOTSTRAPPING ──► RUNNING ──► WAITING ──► TERMINATING ──► TERMINATED
                                  │                                        │
                                  │                               TERMINATED_WITH_ERRORS
                                  │
                                  └──► (steps execute during RUNNING)
```

### State Descriptions

| State | What Happens | Duration |
|---|---|---|
| **STARTING** | EC2 instances provisioned. AMI loaded. Instances initializing. | 2-5 min (depends on instance type and count) |
| **BOOTSTRAPPING** | Bootstrap action scripts execute on all instances. | Varies by script complexity |
| **RUNNING** | Applications installed. Steps begin executing sequentially. | Duration of steps |
| **WAITING** | All steps completed. Cluster idle, awaiting new work or termination. | Until manual termination or auto-termination |
| **TERMINATING** | Cluster shutting down. Resources being cleaned up. | 1-5 min |
| **TERMINATED** | All instances terminated. Cluster record retained for history. | Final state |
| **TERMINATED_WITH_ERRORS** | Cluster terminated due to a failure. | Final state |

### Step States (Within RUNNING)

Steps have their own lifecycle within the RUNNING state:

| Step State | Meaning |
|---|---|
| **PENDING** | Queued, waiting for previous steps to complete |
| **RUNNING** | Currently executing |
| **COMPLETED** | Finished successfully |
| **FAILED** | Encountered an error |
| **CANCELLED** | Cancelled because a previous step failed (default behavior) |

### Auto-Termination Behavior

| Cluster Configuration | Behavior After Last Step |
|---|---|
| **Auto-terminate enabled** | RUNNING → WAITING → TERMINATING → TERMINATED |
| **Keep alive (manual shutdown)** | RUNNING → WAITING (stays indefinitely until manually terminated) |
| **Auto-termination with idle timeout** | WAITING → after idle timeout expires → TERMINATING → TERMINATED |

### Termination Protection

| Feature | Behavior |
|---|---|
| **Enabled** | Cluster cannot be terminated by API/console until protection is disabled |
| **Auto-enabled for HA clusters** | Multi-primary clusters always have termination protection enabled |
| **Override** | Must explicitly disable via `modify-cluster-attributes --no-termination-protected` |
| **Purpose** | Prevents accidental termination of long-running production clusters |

---

## 12. YARN Node Labels — Spot Instance Protection

YARN node labels are EMR's mechanism to protect job reliability when using Spot Instances. The key problem: if an ApplicationMaster runs on a Spot task node and that node is reclaimed, the entire application fails — even though the data and most executors are fine.

### The Problem

```
WITHOUT Node Labels:

  ApplicationMaster on Task Node (Spot)
         │
         ▼
  Spot reclamation → AM killed → Entire Spark job FAILS
  (even though 95% of executors on other nodes are fine)
```

### The Solution

```
WITH Node Labels:

  ApplicationMaster on Core Node (On-Demand, labeled CORE)
         │
         ▼
  Task node Spot reclamation → Only executor containers lost
  → AM detects failures → Requests replacement containers
  → Job continues with temporary reduction in parallelism
```

### Node Label Configuration by EMR Version

| EMR Version | Default Behavior | Configuration |
|---|---|---|
| **EMR 5.19.0 - 5.x** | Node labels **enabled**. Core nodes labeled `CORE`. AMs placed on core only. | Automatic — no configuration needed |
| **EMR 6.x** | Node labels **disabled by default**. AMs can run on core or task. | Enable manually via `yarn.node-labels.enabled: true` and `yarn.node-labels.am.default-node-label-expression: 'CORE'` |
| **EMR 7.x** | Node labels by **market type**: `ON_DEMAND` and `SPOT` | AMs default to `ON_DEMAND` nodes. Can also restrict to `CORE` |
| **EMR 7.2+** | Managed scaling aware of node labels | Scales `ON_DEMAND` nodes independently for AM demand vs `SPOT` for executor demand |

### EMR 7.x Configuration Options

**Option A — AMs on On-Demand nodes only (recommended):**
```xml
yarn.node-labels.enabled: true
yarn.node-labels.am.default-node-label-expression: 'ON_DEMAND'
```

**Option B — AMs on Core nodes only:**
```xml
yarn.node-labels.enabled: true
yarn.node-labels.am.default-node-label-expression: 'CORE'
```

### Warning

Do NOT manually modify `yarn-site` and `capacity-scheduler` configuration properties related to node labels. EMR manages these automatically, and manual changes can break the node label feature, leading to ApplicationMasters being placed on Spot task nodes and job failures.

---

## 13. Cluster Topologies — Common Configurations

### Topology 1: Minimal Development Cluster

```
┌───────────────────┐
│ Primary (m5.xlarge)│
│ + Core (m5.xlarge) │  ← Single-node: primary IS the core
│ 1 instance         │
└───────────────────┘

Total: 1 instance
Cost: ~$0.192/hr (On-Demand, us-east-1)
Use: Development, testing, small data exploration
```

### Topology 2: Small Production Cluster

```
┌─────────────────────┐
│ Primary (m5.2xlarge) │  1 instance, On-Demand
└─────────┬───────────┘
          │
┌─────────▼───────────┐
│ Core (m5.2xlarge)    │  4 instances, On-Demand
│ HDFS + compute       │  HDFS replication factor: 2
└─────────────────────┘

Total: 5 instances
Cost: ~$1.92/hr
Use: Small-to-medium ETL jobs, Hive/Presto queries
```

### Topology 3: Cost-Optimized Production Cluster

```
┌─────────────────────┐
│ Primary (m5.4xlarge) │  1 instance, On-Demand
└─────────┬───────────┘
          │
┌─────────▼───────────┐
│ Core (r5.2xlarge)    │  10 instances, On-Demand
│ HDFS (shuffle only)  │  Minimal HDFS for shuffle data
└─────────┬───────────┘
          │
┌─────────▼───────────┐
│ Task (r5.4xlarge)    │  50 instances, Spot
│ Pure compute         │  Multiple instance groups
│                      │  for Spot diversification
└─────────────────────┘

Total: 61 instances
Cost: ~$25/hr (Spot savings on task nodes)
Use: Large Spark ETL, data in S3 via EMRFS
Key: Core nodes are On-Demand for HDFS stability.
     Task nodes are Spot for cheap compute.
     All persistent data in S3.
```

### Topology 4: High-Availability Production Cluster

```
┌─────────────────────┐
│ Primary (m5.4xlarge) │  3 instances, On-Demand
│ HA configuration     │  ZooKeeper quorum
└─────────┬───────────┘
          │
┌─────────▼───────────┐
│ Core (r5.4xlarge)    │  20 instances, On-Demand
│ HDFS + HBase regions │  High HDFS replication (3)
└─────────┬───────────┘
          │
┌─────────▼───────────┐
│ Task (r5.8xlarge)    │  100 instances, Spot
│ Spark executors      │  Instance fleet with 15+
│                      │  instance types
└─────────────────────┘

Total: 123 instances
Cost: ~$80/hr (significant Spot savings)
Use: Long-running HBase cluster with Spark analytics,
     interactive Presto queries
```

### Topology 5: Transient Job Cluster

```
┌─────────────────────┐
│ Primary (m5.xlarge)  │  1 instance, On-Demand
└─────────┬───────────┘
          │
┌─────────▼───────────┐
│ Core (m5.2xlarge)    │  2 instances, On-Demand
│ Minimal HDFS         │
└─────────┬───────────┘
          │
┌─────────▼───────────┐
│ Task (c5.4xlarge)    │  20 instances, Spot
│ Burst compute        │
└─────────────────────┘

Lifecycle: Create → Submit step → Auto-terminate
Duration: ~45 min per job
Cost: ~$5/hr × 0.75 hr = ~$3.75 per job run
Use: Nightly batch ETL, scheduled Spark jobs
Key: No long-running cluster cost. Data in/out via S3.
```

---

## 14. Service Quotas and Limits

### Cluster-Level Limits

| Quota | Default Value | Adjustable? |
|---|---|---|
| **Max active clusters per account per region** | 500 | Yes, via Service Quotas |
| **Max instances per instance fleet** | 4,000 (EMR 7.7.0+) | No [UNVERIFIED] |
| **Max core instance groups** | 1 per cluster | No |
| **Max task instance groups** | 48 per cluster | No |
| **Max instance types per instance fleet** | 30 [UNVERIFIED] | No |
| **Max bootstrap actions** | 16 per cluster | No |
| **Max primary nodes** | 1 or 3 (HA) | No |
| **Shutdown action timeout** | 60 seconds | No |

### API Rate Limits

| API Operation | Rate Limit |
|---|---|
| **Overall EMR API** | 200 req/sec per account per region |
| **RunJobFlow (create cluster)** | 10 req/sec |
| **AddJobFlowSteps** | 10 req/sec |
| **AddInstanceFleet** | Burst: 5/sec, Sustained: 0.5/sec |
| **Other operations** | Subject to overall 200 req/sec limit |

### Per-Cluster Limits

| Resource | Limit |
|---|---|
| **Max concurrent steps** | 256 (configurable; default varies by EMR version) |
| **Max pending steps** | 256 |
| **HDFS replication factor** | Configurable (default: 3 for ≥4 nodes, 2 for ≤3, 1 for single-node) |
| **EBS volumes per instance** | Varies by instance type |

---

## 15. Failure Modes and Recovery

### Failure Taxonomy

| Failure | Impact | Detection | Recovery |
|---|---|---|---|
| **Single primary fails (no HA)** | Cluster lost | EMR control plane | Create new cluster |
| **Single primary fails (HA)** | Automatic failover, brief pause | ZooKeeper heartbeat | EMR replaces failed node automatically |
| **Core node fails** | HDFS under-replication, lost containers | NameNode heartbeat, YARN NodeManager heartbeat | HDFS re-replicates blocks; YARN reschedules containers |
| **Many core nodes fail** | Potential HDFS data loss | HDFS reports missing blocks | If blocks lost: job fails. Retry from S3 input data |
| **Task node fails** | Lost containers only | YARN NodeManager heartbeat | YARN reschedules containers on surviving nodes |
| **Spot reclamation (task)** | Same as task node failure | 2-min warning from EC2 | YARN reschedules; EMR requests replacement Spot capacity |
| **Spot reclamation (core)** | HDFS data risk + lost containers | 2-min warning from EC2 | HDFS re-replicates; YARN reschedules; EMR requests replacement |
| **Bootstrap action fails** | Instance marked failed | Non-zero exit code | EMR retries instance provisioning; terminates cluster if too many fail |
| **Step fails** | Step marked FAILED | Step exit code / YARN app status | Depends on ActionOnFailure: CONTINUE, CANCEL_AND_WAIT, or TERMINATE_CLUSTER |
| **EBS volume fails** | Data on that volume lost | EC2 instance status check | EMR marks node unhealthy; HDFS re-replicates from other copies |

### Recovery Strategies

**For transient clusters:**
- Retry the entire cluster creation + job submission
- Since all input/output is in S3, no data loss
- Consider Step Functions or Airflow for orchestration with retry logic

**For long-running clusters:**
- Use multi-primary HA
- Use On-Demand for core nodes
- Use Spot diversification for task nodes (many instance types)
- Monitor CloudWatch metrics for early warning
- Set up SNS alerts for cluster state changes

---

## 16. Design Decision Analysis

### Decision 1: Why Three Node Types Instead of One?

| Alternative | Pros | Cons |
|---|---|---|
| **Single node type (all nodes equal)** | Simpler model, any node can do anything | Can't optimize Spot usage, management services compete with compute, HDFS data at risk everywhere |
| **Two types (primary + worker)** | Separates management from compute | Can't use Spot safely for workers (HDFS data risk) |
| **Three types (primary + core + task)** ← EMR's choice | Spot-safe task nodes, stable HDFS on core, dedicated management on primary | More complex model, requires YARN node labels for protection |

**Why EMR chose three types**: The task node concept exists specifically to enable Spot Instances safely. Without task nodes, every compute node would also store HDFS data, making Spot usage risky. The three-type model isolates risk: task nodes are expendable, core nodes are stable, primary manages everything.

### Decision 2: Why Exactly 3 Primary Nodes for HA?

| Count | Pros | Cons |
|---|---|---|
| **2 primary nodes** | Minimum redundancy | Split-brain problem with ZooKeeper (no quorum possible with 2) |
| **3 primary nodes** ← EMR's choice | ZooKeeper quorum (majority = 2 of 3), single-node failure tolerance | 3x primary cost, more network traffic |
| **5 primary nodes** | 2-node failure tolerance | Excessive cost for marginal benefit, 5x primary cost |

**Why 3**: ZooKeeper requires an odd-numbered quorum. With 3 nodes, a majority is 2, so the cluster tolerates exactly 1 primary node failure. 5 would tolerate 2, but the probability of 2 simultaneous primary failures is negligible, making 5 over-provisioned.

### Decision 3: Why YARN Instead of a Custom Scheduler?

| Alternative | Pros | Cons |
|---|---|---|
| **Custom AWS scheduler** | Optimized for EMR, full control | Must reinvent container management, app lifecycle, fairness; can't run unmodified Spark/Hive |
| **YARN** ← EMR's choice | Industry standard, Spark/Hive/Flink integrate natively, community support | Limited flexibility, YARN's design predates cloud-native patterns |
| **Kubernetes** | Modern, portable, rich ecosystem | Heavy overhead for data workloads, not optimized for data locality; this is what EMR on EKS addresses |

**Why YARN**: All major Hadoop ecosystem frameworks (Spark, Hive, Flink, HBase) are built to run on YARN. Using YARN means EMR can run unmodified open-source frameworks — a core product requirement. Customers bring their existing Spark code and it just works.

### Decision 4: Ephemeral HDFS vs. Persistent HDFS

| Alternative | Pros | Cons |
|---|---|---|
| **Persistent HDFS (survives cluster termination)** | Data locality, lower latency | Couples storage to cluster lifecycle, expensive (paying for instances even when idle) |
| **Ephemeral HDFS** ← EMR's choice | Cheap (only pay while cluster runs), encourages S3 usage | Must re-create HDFS data on new clusters, shuffle data lost |

**Why ephemeral**: EMR wants to decouple storage from compute. S3 (via EMRFS) provides durable, elastic storage. HDFS exists only for use cases that need it (HBase region storage, shuffle data). Making HDFS ephemeral nudges customers toward the cloud-native architecture of S3 as primary storage.

---

## 17. Interview Angles

### Questions an Interviewer Might Ask

**Architecture Fundamentals:**
- "Why does EMR have three node types? Why not just two?"
  - Answer: Task nodes enable safe Spot usage. The three-type model isolates data risk (core) from compute elasticity (task) and management (primary).

- "What happens when the primary node fails in a single-primary cluster?"
  - Answer: The entire cluster is lost — HDFS NameNode and YARN ResourceManager are gone. This is why HA clusters use 3 primary nodes with ZooKeeper-based automatic failover.

**Spot Instance Handling:**
- "How does EMR protect jobs from Spot interruption?"
  - Answer: Three mechanisms: (1) YARN node labels ensure ApplicationMasters run only on core/on-demand nodes, so Spot reclamation only kills executors, not the job. (2) Task nodes don't store HDFS data, so no data loss on reclamation. (3) YARN automatically reschedules failed containers on surviving nodes.

**HA Design:**
- "Why 3 primary nodes for HA? Why not 2?"
  - Answer: ZooKeeper requires an odd-numbered quorum. With 2 nodes, you can't distinguish a network partition from a real failure (split-brain). With 3, a majority is 2, so 1 failure is tolerated while maintaining quorum.

- "What services need external databases in HA mode?"
  - Answer: Hive (external MySQL metastore), Hue (RDS), Oozie (RDS), Presto (Glue Data Catalog or external metastore). These services store state that must survive primary node failure.

**Scaling Implications:**
- "How does adding task nodes differ from adding core nodes?"
  - Answer: Task nodes are fast — just launch EC2, start NodeManager, register with YARN. Core nodes are slower — must also start DataNode, HDFS needs to rebalance blocks to use the new storage. Scale-down is opposite: task nodes are fast (no HDFS data to migrate), core nodes are slow (must gracefully decommission DataNode and replicate blocks away).

**Cost Optimization:**
- "How would you design a cost-optimized EMR cluster for a nightly batch ETL job?"
  - Answer: Transient cluster with: 1 On-Demand primary, 2-4 On-Demand core (minimal HDFS for shuffle), 20+ Spot task nodes (instance fleet with 15+ types). Data in S3. Submit step with auto-terminate on completion. Use c5/r5 Spot for compute-heavy work.

### Red Flags in Candidate Answers

| Red Flag | Why It's Wrong |
|---|---|
| "Use Spot for primary nodes to save cost" | Primary node failure = cluster failure in single-primary mode |
| "Store output data in HDFS for speed" | HDFS is ephemeral — data lost when cluster terminates |
| "All nodes are the same, just run more of them" | Ignores the core/task distinction that enables safe Spot usage |
| "2 primary nodes for HA" | ZooKeeper can't form a quorum with 2 nodes (split-brain risk) |
| "Run ApplicationMasters on task nodes for more compute" | Spot reclamation kills the AM, failing the entire application |
| "HDFS replication factor of 1 to save storage" | Single copy = data loss on any single node failure |
