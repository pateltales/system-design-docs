# EMR Step Execution and Job Lifecycle — Deep Dive

> **Companion doc for:** [interview-simulation.md](interview-simulation.md) — Phase 11 (Step Execution & Lifecycle)
> **Last verified:** February 2026 against AWS EMR documentation

---

## Table of Contents

1. [Overview](#1-overview)
2. [What Is a Step?](#2-what-is-a-step)
3. [Step States and Transitions](#3-step-states-and-transitions)
4. [Action on Failure — Controlling Cluster Fate](#4-action-on-failure--controlling-cluster-fate)
5. [Concurrent Steps](#5-concurrent-steps)
6. [Step Submission Methods](#6-step-submission-methods)
7. [Step Types](#7-step-types)
8. [Cluster Lifecycle Patterns](#8-cluster-lifecycle-patterns)
9. [Auto-Termination Policy](#9-auto-termination-policy)
10. [Termination Protection](#10-termination-protection)
11. [Job Orchestration — Beyond Single Clusters](#11-job-orchestration--beyond-single-clusters)
12. [Monitoring Steps and Jobs](#12-monitoring-steps-and-jobs)
13. [Failure Handling Patterns](#13-failure-handling-patterns)
14. [Transient vs Long-Running Clusters](#14-transient-vs-long-running-clusters)
15. [Design Decision Analysis](#15-design-decision-analysis)
16. [Interview Angles](#16-interview-angles)

---

## 1. Overview

Steps are the fundamental unit of work in EMR on EC2. Understanding how steps execute, fail, and interact with cluster lifecycle is essential because it determines:
- **Job reliability** — what happens when a step fails?
- **Cost** — does the cluster stay alive (and billing) after work is done?
- **Operational patterns** — transient vs long-running clusters

### The Key Abstraction

```
Cluster = Infrastructure (nodes, YARN, HDFS)
Step    = Unit of Work (Spark job, Hive query, custom JAR)

A cluster can run many steps.
A step runs on exactly one cluster.
Steps can be sequential or concurrent (EMR 5.28.0+).
```

---

## 2. What Is a Step?

A step is a discrete unit of work submitted to an EMR cluster. Each step contains:

| Component | Description | Example |
|---|---|---|
| **Type** | What kind of work | Spark, Hive, Custom JAR, Streaming |
| **JAR/Script** | The code to execute | `s3://bucket/scripts/etl.py` |
| **Arguments** | Parameters to pass | `--input s3://data/ --output s3://results/` |
| **ActionOnFailure** | What to do if this step fails | `CONTINUE`, `CANCEL_AND_WAIT`, `TERMINATE_CLUSTER` |
| **Name** | Human-readable identifier | "Nightly ETL Job" |

### Step Execution Model

```
                    EMR Cluster
┌─────────────────────────────────────────────┐
│                                             │
│  Step Queue (max 256 active)                │
│  ┌──────┐ ┌──────┐ ┌──────┐ ┌──────┐      │
│  │Step 1│ │Step 2│ │Step 3│ │Step 4│ ...   │
│  │RUN   │ │PEND  │ │PEND  │ │PEND  │      │
│  └──┬───┘ └──────┘ └──────┘ └──────┘      │
│     │                                       │
│     ▼                                       │
│  YARN ResourceManager                       │
│     │                                       │
│     ▼                                       │
│  Spark/Hive/MapReduce Application           │
│  running in YARN containers across          │
│  core and task nodes                        │
│                                             │
└─────────────────────────────────────────────┘
```

### Step Limits

| Limit | Value |
|---|---|
| **Max active steps (PENDING + RUNNING)** | 256 |
| **Max total steps over cluster lifetime** | Unlimited |
| **Step ID max length** | 256 characters |
| **Interactive job submissions** | Unlimited (independent of step limit) |

**Key distinction**: The 256-step limit applies to steps submitted via the EMR API. You can always submit unlimited jobs interactively (SSH to primary node and run `spark-submit` directly).

---

## 3. Step States and Transitions

### State Machine

```
                    ┌─────────┐
                    │ PENDING  │
                    └────┬────┘
                         │
                    ┌────▼────┐
              ┌─────│ RUNNING  │─────┐
              │     └────┬────┘     │
              │          │          │
         ┌────▼────┐ ┌───▼─────┐ ┌─▼──────────┐
         │ FAILED  │ │COMPLETED│ │ INTERRUPTED │
         └─────────┘ └─────────┘ └─────────────┘
                                    (EMR 5.28.0+)

  CANCELLED ← (when a prior step fails and ActionOnFailure cancels remaining)
```

### State Descriptions

| State | Meaning | Transition From |
|---|---|---|
| **PENDING** | Queued, waiting for a slot to run | Submission |
| **RUNNING** | Currently executing | PENDING |
| **COMPLETED** | Finished successfully (exit code 0) | RUNNING |
| **FAILED** | Finished with error (non-zero exit code) | RUNNING |
| **INTERRUPTED** | Cancelled while running (EMR 5.28.0+) | RUNNING (user-initiated cancel) |
| **CANCELLED** | Never ran; cancelled because a prior step failed | PENDING |

### Step Cancellation

| EMR Version | Cancel Capability |
|---|---|
| **EMR < 4.8.0** | Cannot cancel steps |
| **EMR 4.8.0+ (except 5.0.0)** | Can cancel PENDING steps |
| **EMR 5.28.0+** | Can cancel PENDING and RUNNING steps |

Cancelling a running step:
1. EMR sends SIGTERM to the step process
2. The YARN application is killed
3. All containers are released
4. Step state becomes INTERRUPTED

---

## 4. Action on Failure — Controlling Cluster Fate

Each step has an `ActionOnFailure` setting that determines what happens to the cluster and remaining steps when that step fails.

### Options

| Action | Behavior | Use Case |
|---|---|---|
| **CONTINUE** | Step marked FAILED, but cluster continues. Next step runs. | Independent steps that shouldn't block each other |
| **CANCEL_AND_WAIT** | Step marked FAILED, remaining steps CANCELLED. Cluster stays alive (WAITING state). | Want to investigate failure before proceeding |
| **TERMINATE_CLUSTER** | Step marked FAILED, remaining steps CANCELLED. Cluster terminates. | Transient clusters where failure = no reason to keep cluster alive |
| **TERMINATE_JOB_FLOW** | Alias for TERMINATE_CLUSTER (legacy name) | Same as above |

### Decision Matrix

```
Is this a transient cluster (auto-terminate after steps)?
├── Yes → TERMINATE_CLUSTER (no point keeping cluster alive on failure)
│
└── No → Are steps independent of each other?
    ├── Yes → CONTINUE (let other steps run regardless)
    │
    └── No → Are steps in a pipeline (each depends on previous)?
        ├── Yes → CANCEL_AND_WAIT (stop pipeline, investigate)
        └── No → CONTINUE (handle failures per-step)
```

### Example: ETL Pipeline

```
Step 1: Extract (ActionOnFailure: CANCEL_AND_WAIT)
  └── If fails → stop everything, investigate data source issue
Step 2: Transform (ActionOnFailure: CANCEL_AND_WAIT)
  └── If fails → stop, don't load bad data
Step 3: Load (ActionOnFailure: TERMINATE_CLUSTER)
  └── If fails → nothing left to do, terminate
  └── If succeeds → cluster auto-terminates (last step)
```

### Concurrent Steps Restriction

When `StepConcurrencyLevel > 1`:
- **ActionOnFailure is restricted to CONTINUE only**
- You CANNOT use CANCEL_AND_WAIT or TERMINATE_CLUSTER with concurrent steps
- This is because it's ambiguous which concurrent steps should be cancelled when one fails

Edge case: If you reduce StepConcurrencyLevel from >1 to 1 while steps are running, `TERMINATE_CLUSTER` may activate but `CANCEL_AND_WAIT` will not.

---

## 5. Concurrent Steps

### Overview (EMR 5.28.0+)

By default, EMR runs steps **sequentially** — one at a time, in submission order. Starting with EMR 5.28.0, you can configure **concurrent step execution** to improve cluster utilization:

```
Sequential (default, StepConcurrencyLevel=1):
  Step 1 ████████████░░░░░░░░░░░░
  Step 2 ░░░░░░░░░░░░████████░░░░
  Step 3 ░░░░░░░░░░░░░░░░░░░░████

Concurrent (StepConcurrencyLevel=3):
  Step 1 ████████████
  Step 2 ████████████████
  Step 3 ██████████████████████
  (all running simultaneously, sharing YARN resources)
```

### Configuration

```bash
# At cluster creation
aws emr create-cluster \
  --step-concurrency-level 10 \
  --release-label emr-5.28.0 \
  ...

# Modify running cluster
aws emr modify-cluster \
  --cluster-id j-ABC123 \
  --step-concurrency-level 5
```

### How Resources Are Shared

Concurrent steps share the cluster's YARN resources:
- Each step submits a YARN application
- YARN's scheduler (Capacity or Fair) allocates containers across all running applications
- Total YARN resources are divided among concurrent steps

**Critical sizing concern**: The primary node must have enough memory and CPU to run multiple step executor processes simultaneously. Each step's main process runs on the primary node.

### YARN Interaction

```
If StepConcurrencyLevel = 10 but YARN parallelism = 5:
  → Only 5 YARN applications run at a time
  → Other steps wait for YARN resources even though EMR allows 10 concurrent steps
```

The effective concurrency is `min(StepConcurrencyLevel, YARN_parallelism)`.

### Constraints

| Constraint | Details |
|---|---|
| **ActionOnFailure** | Must be CONTINUE when StepConcurrencyLevel > 1 |
| **Execution order** | PENDING → RUNNING transitions respect submission order, but COMPLETION order is non-deterministic |
| **Primary node sizing** | Must be large enough for multiple step executor processes |
| **YARN resources** | Shared among concurrent steps — may cause resource contention |

---

## 6. Step Submission Methods

### Method 1: At Cluster Creation

```bash
aws emr create-cluster \
  --name "ETL Cluster" \
  --release-label emr-7.12.0 \
  --applications Name=Spark \
  --steps '[
    {
      "Name": "Spark ETL",
      "Type": "Spark",
      "ActionOnFailure": "TERMINATE_CLUSTER",
      "Args": ["--class", "com.example.ETL", "s3://bucket/etl.jar"]
    }
  ]' \
  --auto-terminate \
  --instance-groups \
    InstanceGroupType=MASTER,InstanceCount=1,InstanceType=m5.xlarge \
    InstanceGroupType=CORE,InstanceCount=4,InstanceType=r5.2xlarge
```

This creates a **transient cluster**: create → run step → auto-terminate.

### Method 2: Add Steps to Running Cluster

```bash
aws emr add-steps \
  --cluster-id j-ABC123 \
  --steps '[
    {
      "Name": "Additional Analysis",
      "Type": "Spark",
      "ActionOnFailure": "CONTINUE",
      "Args": ["--class", "com.example.Analysis", "s3://bucket/analysis.jar"]
    }
  ]'
```

### Method 3: Interactive Submission (SSH)

```bash
# SSH to primary node
ssh -i key.pem hadoop@ec2-xxx-xxx-xxx-xxx.compute-1.amazonaws.com

# Submit directly (not tracked as an EMR step)
spark-submit --master yarn --deploy-mode cluster \
  --class com.example.ETL s3://bucket/etl.jar
```

**Key difference**: Interactive submissions are NOT tracked in EMR's step history. They don't count toward the 256-step limit and don't trigger ActionOnFailure.

---

## 7. Step Types

### Built-in Step Types

| Type | Framework | Example Use |
|---|---|---|
| **Spark** | Apache Spark | PySpark scripts, Scala/Java JARs |
| **Hive** | Apache Hive | HiveQL queries from S3 scripts |
| **Pig** | Apache Pig | Pig Latin scripts |
| **Streaming** | Hadoop Streaming | Map/reduce with any language (Python, Ruby, etc.) |
| **Custom JAR** | Any Java/Scala application | Custom Hadoop applications, data migration tools |
| **Command Runner** | `command-runner.jar` | Run any command on the primary node |

### Command Runner

The `command-runner.jar` is a versatile step type that runs arbitrary commands:

```bash
aws emr add-steps --cluster-id j-ABC123 \
  --steps 'Type=CUSTOM_JAR,Name="Run Script",Jar="command-runner.jar",Args=["spark-submit","--deploy-mode","cluster","s3://bucket/my_job.py"]'
```

Common uses:
- `spark-submit` with custom parameters
- `s3-dist-cp` for data transfer
- `hadoop distcp` for HDFS operations
- Custom shell scripts

### Script Runner

For running shell scripts directly:

```bash
aws emr add-steps --cluster-id j-ABC123 \
  --steps 'Type=CUSTOM_JAR,Name="Shell Script",Jar="s3://us-east-1.elasticmapreduce/libs/script-runner/script-runner.jar",Args=["s3://bucket/my_script.sh"]'
```

---

## 8. Cluster Lifecycle Patterns

### Pattern 1: Transient Cluster (Most Common for Batch)

```
Create Cluster → Run Steps → Auto-Terminate

Timeline:
  t=0:      Create cluster with steps and --auto-terminate
  t=5min:   Cluster reaches RUNNING, steps begin
  t=45min:  All steps COMPLETED
  t=45min:  Cluster enters WAITING → TERMINATING → TERMINATED

Cost: Pay only for the ~45 min of compute
```

**Configuration:**
```bash
aws emr create-cluster \
  --auto-terminate \
  --steps '[{"Type":"Spark", "ActionOnFailure":"TERMINATE_CLUSTER", ...}]' \
  ...
```

### Pattern 2: Long-Running Cluster with Step Submission

```
Create Cluster → Keep Alive → Submit Steps On-Demand → Eventually Terminate

Timeline:
  Day 1:    Create cluster (no --auto-terminate)
  Day 1:    Cluster reaches WAITING
  Day 1-N:  Submit steps as needed (ad-hoc queries, scheduled jobs)
  Day N:    Manually terminate when no longer needed

Cost: Pay for entire cluster lifetime (including idle WAITING time)
```

### Pattern 3: Long-Running with Auto-Termination Policy

```
Create Cluster → Work → Idle → Auto-Terminate After Timeout

Timeline:
  t=0:      Create cluster with auto-termination policy (1 hour idle)
  t=5min:   Cluster reaches RUNNING
  t=5-60min: Submit and run steps
  t=60min:  Last step completes
  t=120min: No activity for 60 min → auto-terminate triggers
  t=125min: TERMINATED

Cost: Pay for compute + 1 hour idle time (buffer for new work)
```

### Pattern 4: Scheduled Transient Clusters

```
Orchestrator (Step Functions / Airflow)
  │
  ├── 02:00 AM: Create cluster → Run nightly ETL → Auto-terminate
  ├── 06:00 AM: Create cluster → Run morning reports → Auto-terminate
  ├── 12:00 PM: Create cluster → Run midday refresh → Auto-terminate
  └── 06:00 PM: Create cluster → Run evening sync → Auto-terminate

Each cluster lives for ~30-60 minutes
Total daily cost: 4 × ~$10 = ~$40 instead of 24hr × ~$25/hr = ~$600
```

---

## 9. Auto-Termination Policy

### Overview

The auto-termination policy automatically terminates a cluster after it has been idle for a specified duration.

### Configuration

| Parameter | Details |
|---|---|
| **IdleTimeout** | Duration before auto-termination |
| **Minimum timeout** | 1 minute |
| **Maximum timeout** | 7 days |
| **Default timeout** | 60 minutes (1 hour) |

### What Counts as "Idle"

| EMR Version | Idle Conditions (ALL must be true) |
|---|---|
| **EMR 5.34.0+ and 6.4.0+** | No active YARN applications AND HDFS utilization < 10% AND no active EMR notebook/Studio connections AND no on-cluster application UIs in use AND no pending steps |
| **EMR 5.30.0 - 5.33.0 and 6.1.0 - 6.3.0** | No active YARN applications AND no active Spark jobs |

### Preventing Auto-Termination for Non-YARN Workloads

For applications like shell scripts or non-YARN processes that EMR can't detect:

```bash
# Touch this file to signal "I'm busy" (EMR 6.4.0+)
touch /emr/metricscollector/isbusy
```

EMR will not auto-terminate as long as this file's modification time is recent.

### CLI Commands

```bash
# Set auto-termination policy (timeout in seconds)
aws emr put-auto-termination-policy \
  --cluster-id j-ABC123 \
  --auto-termination-policy IdleTimeout=3600

# Remove auto-termination policy
aws emr remove-auto-termination-policy \
  --cluster-id j-ABC123

# Get current policy
aws emr get-auto-termination-policy \
  --cluster-id j-ABC123
```

---

## 10. Termination Protection

### Purpose

Termination protection prevents accidental cluster termination — both from API calls and from ActionOnFailure step actions.

### Behavior

| Scenario | Without Protection | With Protection |
|---|---|---|
| **User calls TerminateJobFlows** | Cluster terminates | API call rejected |
| **Step fails with TERMINATE_CLUSTER** | Cluster terminates | Cluster stays alive (step still FAILED) |
| **Auto-termination idle timeout** | Cluster terminates | Cluster terminates (protection doesn't block auto-termination) [UNVERIFIED] |
| **EC2 Spot reclamation** | Instance terminated | Instance still terminated (protection doesn't override EC2 Spot) |
| **AWS account suspension** | Cluster terminated | Cluster terminated (protection doesn't override account-level actions) |

### Key Rules

| Rule | Details |
|---|---|
| **Multi-primary clusters** | Termination protection is **automatically enabled** |
| **Disabling** | Must explicitly disable before terminating |
| **Default** | Disabled for single-primary clusters |

### Disabling Termination Protection

```bash
# Disable protection
aws emr modify-cluster-attributes \
  --cluster-id j-ABC123 \
  --no-termination-protected

# Then terminate
aws emr terminate-clusters \
  --cluster-ids j-ABC123
```

---

## 11. Job Orchestration — Beyond Single Clusters

### Why Orchestration?

Real-world data pipelines are not single steps on single clusters. They involve:
- Multiple EMR clusters (different configurations for different jobs)
- Dependencies between jobs (job B needs job A's output)
- Error handling and retry logic
- Notification on success/failure
- Scheduled execution

### AWS Step Functions

```
Step Functions State Machine
│
├── CreateCluster
│   └── EMR RunJobFlow API
│       └── Returns cluster ID
│
├── WaitForClusterReady
│   └── Poll DescribeCluster until WAITING
│
├── AddStep (ETL Job)
│   └── EMR AddJobFlowSteps API
│
├── WaitForStepComplete
│   └── Poll DescribeStep until COMPLETED/FAILED
│
├── Choice: Step succeeded?
│   ├── Yes → AddStep (Next Job)
│   └── No → SNS Notification → TerminateCluster
│
├── AddStep (Next Job)
│   └── ...
│
└── TerminateCluster
    └── EMR TerminateJobFlows API
```

### Apache Airflow (MWAA)

```python
# Airflow DAG for EMR pipeline
with DAG('emr_etl_pipeline', schedule_interval='@daily') as dag:

    create_cluster = EmrCreateJobFlowOperator(
        task_id='create_cluster',
        job_flow_overrides={
            'Name': 'nightly-etl',
            'ReleaseLabel': 'emr-7.12.0',
            'Instances': {...},
            'Steps': [{...}],
            'AutoTerminatingPolicy': {'IdleTimeout': 3600}
        }
    )

    wait_for_step = EmrStepSensor(
        task_id='wait_for_step',
        job_flow_id="{{ task_instance.xcom_pull('create_cluster') }}",
        step_id="{{ task_instance.xcom_pull('create_cluster', key='step_id') }}"
    )

    terminate_cluster = EmrTerminateJobFlowOperator(
        task_id='terminate_cluster',
        job_flow_id="{{ task_instance.xcom_pull('create_cluster') }}"
    )

    create_cluster >> wait_for_step >> terminate_cluster
```

### EMR Step Functions vs Airflow vs EMR Steps

| Dimension | EMR Steps (built-in) | Step Functions | Airflow |
|---|---|---|---|
| **Scope** | Within one cluster | Cross-cluster, cross-service | Cross-cluster, cross-service |
| **Dependencies** | Sequential or concurrent (same cluster) | Complex DAGs with branching, error handling | Complex DAGs with scheduling |
| **Error handling** | ActionOnFailure per step | Retry, catch, fallback states | Retry policies, SLA monitoring |
| **Scheduling** | None (submit manually or at cluster creation) | EventBridge rules | Built-in cron scheduling |
| **Multi-cluster** | No | Yes | Yes |
| **Cost** | Free (part of EMR) | Pay per state transition | MWAA environment cost |
| **Best for** | Simple sequential jobs on one cluster | Serverless orchestration, AWS-native | Complex data pipelines, team collaboration |

---

## 12. Monitoring Steps and Jobs

### Step-Level Monitoring

| Method | What You See | Best For |
|---|---|---|
| **EMR Console** | Step name, state, start/end time, logs link | Quick status check |
| **AWS CLI** (`list-steps`, `describe-step`) | Step details, state, creation time | Scripted monitoring |
| **CloudWatch Events** | Step state change events | Automated alerting |
| **Step logs** | stdout, stderr, controller logs in S3 | Debugging failures |

### Where Step Logs Live

```
S3 Log Location (configured at cluster creation):
s3://my-log-bucket/j-CLUSTERID/
├── steps/
│   ├── s-STEP1ID/
│   │   ├── controller.gz     ← Step controller output
│   │   ├── stdout.gz         ← Step stdout
│   │   └── stderr.gz         ← Step stderr
│   └── s-STEP2ID/
│       └── ...
├── node/
│   ├── i-INSTANCEID/
│   │   ├── applications/
│   │   │   ├── hadoop-yarn/    ← YARN logs
│   │   │   └── spark/          ← Spark event logs
│   │   └── daemons/
│   │       ├── instance-controller/
│   │       └── setup-devices/
│   └── ...
└── containers/
    └── ... (YARN container logs)
```

### Application-Level Monitoring

| Application | Monitoring Tool | URL |
|---|---|---|
| **YARN** | ResourceManager UI | `http://primary:8088/cluster` |
| **Spark** | Spark History Server | `http://primary:18080/` |
| **Hive** | Tez UI (via YARN) | `http://primary:8088/proxy/application_xxx/` |
| **Ganglia** | Ganglia web UI | `http://primary/ganglia/` |

### CloudWatch Metrics for Job Health

| Metric | What It Tells You |
|---|---|
| **IsIdle** | Whether the cluster has active YARN applications |
| **ContainerPending** | Number of containers waiting for allocation |
| **YARNMemoryAvailablePercentage** | Available YARN memory as percentage |
| **AppsRunning** | Number of running YARN applications |
| **AppsPending** | Number of pending YARN applications |
| **S3BytesRead / S3BytesWritten** | Data I/O volume to/from S3 |

---

## 13. Failure Handling Patterns

### Pattern 1: Retry on Transient Failure

```
Orchestrator (Step Functions):
  1. Submit step
  2. Wait for completion
  3. If FAILED:
     a. Check error type (logs/exit code)
     b. If transient (OOM, Spot interruption): retry (up to 3 times)
     c. If permanent (bad input, code bug): alert and stop
  4. If COMPLETED: proceed to next step
```

### Pattern 2: Checkpoint and Resume

```
Spark Job with Checkpointing:
  1. Read checkpoint marker from S3 (if exists)
  2. Resume from last checkpoint
  3. Process next batch
  4. Write checkpoint marker to S3
  5. If failure → restart job → resume from checkpoint
```

### Pattern 3: Dead Letter Queue for Failed Records

```
ETL Job:
  1. Read input from S3
  2. For each record:
     a. Try to transform
     b. If fails → write to s3://bucket/dead-letter/
     c. If succeeds → write to s3://bucket/output/
  3. Step COMPLETES even with some failed records
  4. Separate process handles dead letter records
```

### Common Step Failure Causes

| Failure | Symptom | Fix |
|---|---|---|
| **OOM (Out of Memory)** | Container killed, exit code 137 | Increase executor memory, add more partitions, optimize joins |
| **S3 access denied** | Permission error in stderr | Check IAM role, S3 bucket policy, EMRFS roles |
| **Missing dependency** | ClassNotFoundException | Add to `--jars` or bootstrap action |
| **Data skew** | Some tasks 100x slower than others | Salting join keys, repartition, broadcast small tables |
| **Spot interruption** | Container FAILED, node LOST | Add more instance types for Spot diversification |
| **Shuffle fetch failure** | FetchFailedException | Check disk space, increase shuffle partitions, use larger instances |
| **Driver OOM** | Driver exit code 137 | Increase `spark.driver.memory`, avoid `collect()` on large datasets |

---

## 14. Transient vs Long-Running Clusters

### Comparison

| Dimension | Transient Cluster | Long-Running Cluster |
|---|---|---|
| **Lifecycle** | Create → Run steps → Terminate | Create → Keep alive → Submit work → Eventually terminate |
| **Cost model** | Pay per job (cluster time = job time) | Pay continuously (including idle time) |
| **Startup latency** | 5-15 min per cluster creation | Zero (cluster already running) |
| **Data persistence** | S3 only (HDFS lost on termination) | HDFS available for caching, HBase regions |
| **Best for** | Batch ETL, scheduled jobs, CI/CD data tests | Interactive analytics, HBase, ad-hoc exploration |
| **Scaling** | Fixed size (cluster lives too short for scaling to matter) | Managed scaling adjusts over time |
| **Cost optimization** | Right-size per job; Spot for task nodes | Auto-termination policy; managed scaling down during idle |
| **Failure recovery** | Create new cluster and retry | Fix issue on existing cluster |

### When to Use Transient

```
Decision: Use transient cluster when:
  ✓ Job has predictable resource needs
  ✓ Input/output is all in S3
  ✓ Startup latency (5-15 min) is acceptable
  ✓ No need for HDFS persistence between runs
  ✓ Want to minimize cost (pay only for compute time)
  ✓ Orchestrator (Airflow/Step Functions) handles scheduling
```

### When to Use Long-Running

```
Decision: Use long-running cluster when:
  ✓ Interactive queries (Presto/Hive) with sub-second latency expectation
  ✓ HBase serving real-time reads (needs persistent HDFS)
  ✓ Frequent ad-hoc job submissions (don't want 5-15 min wait)
  ✓ Shared cluster for multiple teams (YARN queues)
  ✓ Complex bootstrap (custom software, large reference data)
    that takes too long to repeat per job
```

### Hybrid Pattern: Long-Running with Auto-Termination

```
Cluster with auto-termination (1 hour idle):
  Morning:  Analysts submit Hive queries → cluster active
  Midday:   No queries for 1 hour → cluster auto-terminates
  Afternoon: New query submitted → EMR creates new cluster (or use a scheduler)
  Evening:  Batch ETL runs on transient cluster
```

---

## 15. Design Decision Analysis

### Decision 1: Why Steps Instead of Direct YARN Submission?

| Alternative | Pros | Cons |
|---|---|---|
| **Direct YARN submission** (SSH + spark-submit) | Full control, immediate | No tracking, no retry, no ActionOnFailure, no API integration |
| **EMR Steps** ← EMR's choice | Tracked state, ActionOnFailure, API-driven, integrates with orchestrators | 256 active limit, less flexible than direct submission |

**Why steps**: Steps provide a managed abstraction over raw YARN submissions. They add state tracking (PENDING → RUNNING → COMPLETED/FAILED), failure handling (ActionOnFailure), API-driven submission (no SSH required), and integration with orchestrators like Step Functions and Airflow. The 256-limit is rarely hit since interactive submissions bypass it.

### Decision 2: Why 256 Active Steps Limit?

| Alternative | Pros | Cons |
|---|---|---|
| **Unlimited active steps** | No throttling | Primary node memory exhaustion; each step's executor process runs on primary |
| **256 limit** ← EMR's choice | Bounds primary node resource usage; sufficient for most workloads | May need workaround for extreme concurrency |

**Why 256**: Each active step has an executor process on the primary node that consumes memory and CPU. 256 is a practical upper bound that prevents primary node resource exhaustion while being far more than most clusters need. For higher concurrency, use interactive submission or orchestrate multiple clusters.

### Decision 3: Why Default to Sequential Steps?

| Alternative | Pros | Cons |
|---|---|---|
| **Concurrent by default** | Better utilization | ActionOnFailure semantics become ambiguous; resource contention between steps |
| **Sequential by default** ← EMR's choice | Simple mental model; ActionOnFailure works cleanly; no resource contention | Underutilized cluster when steps are independent |

**Why sequential**: Most step pipelines are inherently sequential (ETL → Transform → Load). Sequential execution makes ActionOnFailure semantics clear (CANCEL_AND_WAIT cancels the "next" step). Concurrent steps (opt-in since EMR 5.28.0) add complexity: ActionOnFailure is limited to CONTINUE, and YARN resource contention must be managed.

---

## 16. Interview Angles

### Questions an Interviewer Might Ask

**Step Fundamentals:**
- "What happens when a step fails with CANCEL_AND_WAIT?"
  - Answer: The failed step is marked FAILED. All remaining PENDING steps are marked CANCELLED. The cluster stays alive in WAITING state, allowing investigation or manual step submission. The cluster doesn't terminate until you explicitly terminate it.

- "What's the difference between submitting a step via the API vs SSH + spark-submit?"
  - Answer: API steps are tracked by EMR (state, logs, ActionOnFailure). They count toward the 256 active limit. Interactive spark-submit is not tracked by EMR — no state management, no ActionOnFailure, no limit. Use API steps for production pipelines, interactive for ad-hoc exploration.

**Concurrent Steps:**
- "When would you enable concurrent step execution?"
  - Answer: When running multiple independent workloads on the same cluster — e.g., 5 independent Spark ETL jobs that don't depend on each other. Concurrent execution improves utilization by sharing YARN resources. But you lose ActionOnFailure (must use CONTINUE), and must size the primary node for multiple executor processes.

**Cluster Lifecycle:**
- "Transient vs long-running cluster — when do you use each?"
  - Answer: Transient for batch ETL with predictable timing (cost = job duration only). Long-running for interactive analytics (Presto/Hive), HBase, or frequent ad-hoc work (avoid startup latency). Hybrid: long-running with auto-termination policy to reduce idle cost.

**Auto-Termination:**
- "How does EMR know when a cluster is idle?"
  - Answer: EMR 5.34.0+/6.4.0+ checks: no active YARN applications, HDFS utilization < 10%, no active notebook/Studio connections, no application UIs in use, no pending steps. All conditions must be true for the idle timeout to count.

**Orchestration:**
- "How do you orchestrate a multi-step ETL pipeline across clusters?"
  - Answer: Use Step Functions or Airflow. Step Functions: CreateCluster → AddStep → WaitForStep → branch on success/failure → TerminateCluster. Airflow: EmrCreateJobFlowOperator → EmrStepSensor → EmrTerminateJobFlowOperator. Both handle retry, error notification, and scheduling.

### Red Flags in Candidate Answers

| Red Flag | Why It's Wrong |
|---|---|
| "Steps run in parallel by default" | Sequential by default; concurrent requires StepConcurrencyLevel > 1 (EMR 5.28.0+) |
| "Use TERMINATE_CLUSTER with concurrent steps" | ActionOnFailure restricted to CONTINUE when StepConcurrencyLevel > 1 |
| "Interactive spark-submit counts as a step" | Interactive submissions are NOT tracked as steps and don't count toward the 256 limit |
| "Keep the cluster running 24/7 for batch jobs" | Transient clusters are far cheaper for scheduled batch workloads |
| "Steps can run on EMR Serverless" | EMR Serverless uses job runs, not steps (different API and abstraction) |
| "Auto-termination happens when no steps are running" | Multiple conditions must be met: no YARN apps, HDFS < 10%, no notebooks, no pending steps |
