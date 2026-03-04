# ECS Monitoring, Health Checks & Operations — Deep Dive

## Table of Contents

1. [Overview](#1-overview)
2. [Task Lifecycle and State Machine](#2-task-lifecycle-and-state-machine)
3. [Health Checks](#3-health-checks)
4. [CloudWatch Container Insights](#4-cloudwatch-container-insights)
5. [EventBridge Integration](#5-eventbridge-integration)
6. [Service Events and Troubleshooting](#6-service-events-and-troubleshooting)
7. [Stopped Task Error Codes](#7-stopped-task-error-codes)
8. [Logging](#8-logging)
9. [Operational Patterns](#9-operational-patterns)
10. [Interview Angles](#10-interview-angles)

---

## 1. Overview

ECS monitoring operates at multiple layers:

```
┌───────────────────────────────────────────────────────────────┐
│                    Monitoring Stack                            │
├───────────────────────────────────────────────────────────────┤
│                                                               │
│  Application Layer                                            │
│  ├─ CloudWatch Logs (stdout/stderr from containers)           │
│  ├─ Custom metrics (application-emitted via SDK)              │
│  └─ X-Ray / OpenTelemetry traces                             │
│                                                               │
│  Service Layer                                                │
│  ├─ ELB health checks (HTTP/TCP)                             │
│  ├─ Service events (100 most recent)                         │
│  ├─ Deployment status and circuit breaker state              │
│  └─ Auto Scaling events                                      │
│                                                               │
│  Task Layer                                                   │
│  ├─ Container health checks (Docker HEALTHCHECK)             │
│  ├─ Task state changes (EventBridge)                         │
│  ├─ Stopped task reasons/error codes                         │
│  └─ Container Insights (CPU, memory, network, disk)          │
│                                                               │
│  Infrastructure Layer                                         │
│  ├─ Container instance metrics (EC2 launch type)             │
│  ├─ Container instance state changes (EventBridge)           │
│  └─ ECS agent health and connectivity                        │
│                                                               │
└───────────────────────────────────────────────────────────────┘
```

---

## 2. Task Lifecycle and State Machine

### 2.1 Complete Task States

Every ECS task progresses through a defined state machine:

```
┌──────────────────────────────────────────────────────────────────────┐
│                        Task State Machine                            │
├──────────────────────────────────────────────────────────────────────┤
│                                                                      │
│  PROVISIONING ──▶ PENDING ──▶ ACTIVATING ──▶ RUNNING                │
│       │                                         │                    │
│       │ (failure)                                │ (stop requested)   │
│       ▼                                         ▼                    │
│    STOPPED ◀── DEPROVISIONING ◀── STOPPING ◀── DEACTIVATING        │
│       │                                                              │
│       ▼                                                              │
│    DELETED (hidden — visible only in describe-tasks API)             │
│                                                                      │
└──────────────────────────────────────────────────────────────────────┘
```

### 2.2 State Details

| State | What Happens | Duration |
|-------|-------------|----------|
| **PROVISIONING** | Initial setup: ENI creation (awsvpc), volume attachment, resource reservation | Seconds to minutes |
| **PENDING** | Waiting for container agent to schedule task; waiting for available resources | Variable — can be long if no capacity |
| **ACTIVATING** | Pull images, create containers, configure networking, register with LB, configure service discovery | Seconds to minutes (depends on image size) |
| **RUNNING** | Task is operational, all essential containers running | Until stopped or failed |
| **DEACTIVATING** | Deregister from ELB target groups, deregister from service discovery | Includes deregistration delay |
| **STOPPING** | Send stop signal to containers, wait for graceful shutdown | Up to `stopTimeout` (default 30s, max 120s) |
| **DEPROVISIONING** | Cleanup: detach/delete ENI, release resources | Seconds |
| **STOPPED** | Terminal state, task is not running | Retained for ~1 hour then deleted |
| **DELETED** | Hidden terminal state, task record removed | Not visible in console |

### 2.3 Graceful Shutdown Sequence

When a task is stopped (RUNNING → DEACTIVATING → STOPPING → STOPPED):

```
┌──────────────────────────────────────────────────────────────┐
│                  Graceful Shutdown Flow                       │
├──────────────────────────────────────────────────────────────┤
│                                                              │
│  1. DEACTIVATING:                                            │
│     ├─ Deregister from ALB target group                     │
│     ├─ Deregistration delay begins (default 300s)            │
│     │  → ALB stops sending NEW requests                      │
│     │  → In-flight requests continue                         │
│     └─ Deregister from Cloud Map / Service Connect           │
│                                                              │
│  2. STOPPING:                                                │
│     ├─ Send STOPSIGNAL to container (default: SIGTERM)       │
│     ├─ Wait for stopTimeout (default 30s)                    │
│     │  → Application should handle SIGTERM gracefully:       │
│     │    - Stop accepting new connections                    │
│     │    - Finish processing in-progress requests            │
│     │    - Close database connections                        │
│     │    - Flush logs and metrics                            │
│     ├─ If still running after stopTimeout: send SIGKILL      │
│     └─ Container exits                                       │
│                                                              │
│  3. DEPROVISIONING:                                          │
│     ├─ Detach ENI (awsvpc mode)                             │
│     ├─ Release CPU/memory reservations                       │
│     └─ Cleanup volumes                                       │
│                                                              │
│  4. STOPPED                                                  │
│                                                              │
└──────────────────────────────────────────────────────────────┘
```

**Key detail — stopTimeout:**

| Platform | Default | Maximum |
|----------|---------|---------|
| Linux (EC2) | 30 seconds | 120 seconds |
| Linux (Fargate) | 30 seconds | 120 seconds |
| Windows | 30 seconds | 30 seconds (cannot be changed) |

**Tip:** Set `stopTimeout` in the task definition's container definition to give your
application adequate time to drain connections. If your app needs 45 seconds to finish
in-flight requests, set `stopTimeout: 60`.

### 2.4 Status Tracking

The ECS container agent tracks two statuses per task:

| Field | Meaning |
|-------|---------|
| `lastStatus` | Most recently known state of the task |
| `desiredStatus` | Target state ECS is transitioning the task toward |

**Useful for debugging:**

```
lastStatus: RUNNING, desiredStatus: STOPPED
  → Task is in process of being stopped

lastStatus: PENDING, desiredStatus: RUNNING
  → Task is trying to start but hasn't reached RUNNING yet

lastStatus: STOPPED, desiredStatus: STOPPED
  → Task has fully stopped
```

---

## 3. Health Checks

### 3.1 Three Layers of Health Checks

ECS uses up to three layers of health checking:

```
┌───────────────────────────────────────────────────────────────┐
│                 Health Check Layers                            │
├───────────────────────────────────────────────────────────────┤
│                                                               │
│  Layer 1: Container Health Check (Docker HEALTHCHECK)         │
│  ├─ Defined in task definition (or Dockerfile)               │
│  ├─ Runs INSIDE the container                                │
│  ├─ Checks application readiness                             │
│  └─ ECS-native, no external dependency                       │
│                                                               │
│  Layer 2: ELB Health Check                                    │
│  ├─ Defined on ALB/NLB target group                          │
│  ├─ Runs FROM the load balancer to the container             │
│  ├─ Checks HTTP response or TCP connection                   │
│  └─ Determines traffic routing                               │
│                                                               │
│  Layer 3: ECS Agent Heartbeat                                 │
│  ├─ Container instance agent sends heartbeat to control plane│
│  ├─ If missed: instance marked DRAINING or INACTIVE          │
│  └─ Only for EC2 launch type                                 │
│                                                               │
└───────────────────────────────────────────────────────────────┘
```

### 3.2 Container Health Check (Docker HEALTHCHECK)

**Configuration in task definition:**

```json
{
  "containerDefinitions": [
    {
      "name": "my-app",
      "healthCheck": {
        "command": ["CMD-SHELL", "curl -f http://localhost:8080/health || exit 1"],
        "interval": 30,
        "timeout": 5,
        "retries": 3,
        "startPeriod": 60
      }
    }
  ]
}
```

**Parameters:**

| Parameter | Default | Range | Description |
|-----------|---------|-------|-------------|
| `command` | — | — | Command to run (CMD or CMD-SHELL) |
| `interval` | 30s | 5-300s | Time between health checks |
| `timeout` | 5s | 2-60s | Max time for check to complete |
| `retries` | 3 | 1-10 | Consecutive failures before UNHEALTHY |
| `startPeriod` | 0s | 0-300s | Grace period after container start (failures don't count) |

**Health statuses:**

| Status | Meaning |
|--------|---------|
| `HEALTHY` | Health check command exited with 0 |
| `UNHEALTHY` | Health check failed `retries` consecutive times |
| `UNKNOWN` | Health check hasn't run yet or container has no health check |

**State transitions:**

```
Container starts
     │
     ▼
  UNKNOWN ──(startPeriod expires)──▶ UNKNOWN
     │                                  │
     │ (health check passes)            │ (health check passes)
     ▼                                  ▼
  HEALTHY ◀────────────────────────  HEALTHY
     │
     │ (retries consecutive failures)
     ▼
  UNHEALTHY
     │
     │ (1 pass)
     ▼
  HEALTHY
```

### 3.3 How Health Check Failures Trigger Task Replacement

When a task becomes UNHEALTHY:

```
Container health check fails `retries` times
     │
     ▼
Task marked UNHEALTHY
     │
     ▼
Is this an essential container?
     ├─ NO → Task continues (non-essential container failure tolerated)
     │
     └─ YES → Service scheduler triggers replacement
               │
               ▼
          Can we start a replacement task?
          (check maximumPercent)
               │
               ├─ YES → Start replacement task
               │         Wait for it to be HEALTHY
               │         Stop unhealthy task
               │
               └─ NO (at maximumPercent) →
                    Stop unhealthy task to free capacity
                    Start replacement task
```

**Important:** If the health check is defined in the Dockerfile but NOT in the task
definition, ECS **still respects it** — Docker reports the container health status,
and ECS reads it. However, defining it in the task definition gives you more control.

### 3.4 ELB Health Check

Configured on the ALB/NLB target group, not in ECS:

```json
{
  "HealthCheckProtocol": "HTTP",
  "HealthCheckPort": "traffic-port",
  "HealthCheckPath": "/health",
  "HealthCheckIntervalSeconds": 30,
  "HealthCheckTimeoutSeconds": 5,
  "HealthyThresholdCount": 3,
  "UnhealthyThresholdCount": 3,
  "Matcher": {
    "HttpCode": "200-299"
  }
}
```

**Interaction with ECS:**

| Event | ECS Action |
|-------|-----------|
| Target registered, initial health check | Target in `initial` state, not receiving traffic |
| Health check passes `HealthyThresholdCount` times | Target becomes `healthy`, receives traffic |
| Health check fails `UnhealthyThresholdCount` times | Target becomes `unhealthy`, traffic stopped |
| ECS sees unhealthy target | Service scheduler replaces the task |

### 3.5 healthCheckGracePeriodSeconds

**Purpose:** Prevent ECS from killing tasks before they have time to warm up and pass
health checks.

```json
{
  "service": {
    "healthCheckGracePeriodSeconds": 120
  }
}
```

**What it does:**
- After a task starts, ECS ignores ELB health check failures for this duration
- Container health check failures are also ignored during this period
- Prevents ECS from entering a "launch → fail health check → replace → launch" loop

**When to increase:**
- Application has a long startup time (JVM warmup, cache loading, DB migrations)
- Using slow health check intervals on the ALB
- Container pulls a large image (slow start)

**Default:** 0 seconds (no grace period)

**Gotcha:** If your health check grace period is too short AND your app takes a while to start:
```
t0:00  Task starts
t0:05  Container still initializing
t0:05  ALB health check → fails (app not ready)
t0:10  healthCheckGracePeriodSeconds = 0 → ECS marks task unhealthy
t0:10  ECS stops task, launches replacement
t0:15  Replacement starts... same cycle repeats forever
```

### 3.6 Health Check Decision Flow

```
┌─────────────────────────────────────────────────────────────────┐
│            Which Health Check Determines Task Health?            │
├─────────────────────────────────────────────────────────────────┤
│                                                                 │
│  Has load balancer?                                             │
│    ├─ NO → Use container health check only                     │
│    │       (if defined; otherwise task is healthy if RUNNING)   │
│    │                                                            │
│    └─ YES → Both checks are evaluated:                         │
│              │                                                  │
│              ├─ Container HC: UNHEALTHY → task replaced         │
│              ├─ ELB HC: unhealthy → task replaced              │
│              └─ Both must pass for task to be "healthy"         │
│                                                                 │
│  healthCheckGracePeriodSeconds active?                          │
│    ├─ YES → Ignore all health check failures                   │
│    └─ NO  → Health check failures trigger replacement          │
│                                                                 │
└─────────────────────────────────────────────────────────────────┘
```

---

## 4. CloudWatch Container Insights

### 4.1 Standard vs Enhanced Observability

| Feature | Standard | Enhanced |
|---------|----------|----------|
| Cluster-level metrics | Yes | Yes |
| Service-level metrics | Yes | Yes |
| Task-level metrics | Yes | Yes |
| **Container-level metrics** | No | **Yes** |
| Pre-built dashboards | Basic | **Curated, detailed** |
| Infrastructure telemetry | Limited | **Comprehensive** |
| Recommendation | Legacy | **Use this (Dec 2024+)** |

### 4.2 Metrics Hierarchy

Container Insights collects metrics at four levels:

```
Cluster
  ├─ CpuUtilized, CpuReserved
  ├─ MemoryUtilized, MemoryReserved
  ├─ NetworkRxBytes, NetworkTxBytes
  ├─ RunningTaskCount
  └─ ContainerInstanceCount (EC2 only)

  Service
    ├─ CpuUtilized, CpuReserved
    ├─ MemoryUtilized, MemoryReserved
    ├─ NetworkRxBytes, NetworkTxBytes
    ├─ RunningTaskCount
    └─ DesiredTaskCount

    Task
      ├─ CpuUtilized, CpuReserved
      ├─ MemoryUtilized, MemoryReserved
      ├─ NetworkRxBytes, NetworkTxBytes
      ├─ EphemeralStorageUtilized, EphemeralStorageReserved
      └─ TaskHealth (HEALTHY/UNHEALTHY/UNKNOWN)

      Container (Enhanced only)
        ├─ CpuUtilized, CpuReserved
        ├─ MemoryUtilized, MemoryReserved
        └─ Container-specific metrics
```

### 4.3 Key Metrics for Monitoring

**Cluster capacity monitoring:**

| Metric | Alert Condition | Meaning |
|--------|----------------|---------|
| `CpuUtilized / CpuReserved` | > 80% | Cluster running hot, may need more capacity |
| `MemoryUtilized / MemoryReserved` | > 80% | Memory pressure, tasks may get OOM-killed |
| `RunningTaskCount` | Decreasing unexpectedly | Tasks are failing and not being replaced |

**Service health monitoring:**

| Metric | Alert Condition | Meaning |
|--------|----------------|---------|
| `RunningTaskCount < DesiredTaskCount` | For > 5 min | Tasks are not starting successfully |
| `CpuUtilized / CpuReserved` | > 90% at service level | Service needs more tasks or larger task size |
| `MemoryUtilized / MemoryReserved` | > 85% at service level | Risk of OOM, scale up or increase memory |

### 4.4 Important Behavior

**Metrics only appear when tasks are running:**
- A service with 0 running tasks produces **no metrics at all**
- This means CloudWatch alarms based on Container Insights metrics will enter
  `INSUFFICIENT_DATA` state when there are no tasks — not `OK`
- Design alarms accordingly (treat `INSUFFICIENT_DATA` as an alert for critical services)

### 4.5 Cost

Container Insights metrics are **custom metrics** charged at CloudWatch custom metric rates.
With enhanced observability and many containers, this can add up:

```
Approximate cost [INFERRED]:
  Standard: ~$0.30/metric/month × ~20 metrics per service = ~$6/service/month
  Enhanced: Additional container-level metrics, potentially 2-3x more

  For 50 services: ~$300-900/month for Container Insights metrics

  Consider: Do you need container-level granularity? Standard may suffice.
```

---

## 5. EventBridge Integration

### 5.1 Event Types

ECS sends four types of events to EventBridge:

| Event Type | Trigger | Use Cases |
|-----------|---------|-----------|
| **Task state change** | Task transitions between states | Alert on task failures, track lifecycle |
| **Container instance state change** | Instance resources/status change | Track capacity, detect agent disconnects |
| **Deployment state change** | Deployment status changes | Track deployments, alert on rollbacks |
| **Service action** | Service API operations | Audit trail, automation triggers |

### 5.2 Event Structure

```json
{
  "version": "0",
  "id": "event-id",
  "source": "aws.ecs",
  "detail-type": "ECS Task State Change",
  "account": "123456789012",
  "time": "2024-01-15T12:00:00Z",
  "region": "us-east-1",
  "detail": {
    "clusterArn": "arn:aws:ecs:us-east-1:123456789:cluster/my-cluster",
    "taskArn": "arn:aws:ecs:us-east-1:123456789:task/my-cluster/abc123",
    "taskDefinitionArn": "arn:aws:ecs:us-east-1:123456789:task-definition/my-app:5",
    "lastStatus": "STOPPED",
    "desiredStatus": "STOPPED",
    "stoppedReason": "Essential container in task exited",
    "stopCode": "EssentialContainerExited",
    "version": 3,
    "containers": [
      {
        "name": "my-container",
        "lastStatus": "STOPPED",
        "exitCode": 137,
        "reason": "OutOfMemoryError"
      }
    ]
  }
}
```

**Key fields for monitoring:**
- `lastStatus` + `desiredStatus`: current and target state
- `stoppedReason`: human-readable stop reason
- `stopCode`: machine-parseable stop code
- `containers[].exitCode`: container exit code (137 = OOM/SIGKILL, 1 = app error)
- `version`: incremented on each state change (use for deduplication)

### 5.3 Version Field for Deduplication

The `version` field in the `detail` object is critical:
- Incremented each time the resource changes state
- Two events with the same `version` for the same resource → duplicates
- Use for deduplication in event consumers
- Different from the top-level `version` (always 0, EventBridge metadata)

### 5.4 Common EventBridge Rules

**Alert on task failures:**

```json
{
  "source": ["aws.ecs"],
  "detail-type": ["ECS Task State Change"],
  "detail": {
    "lastStatus": ["STOPPED"],
    "stopCode": ["TaskFailedToStart", "EssentialContainerExited"]
  }
}
```

**Alert on deployment rollbacks:**

```json
{
  "source": ["aws.ecs"],
  "detail-type": ["ECS Deployment State Change"],
  "detail": {
    "eventName": ["SERVICE_DEPLOYMENT_FAILED"]
  }
}
```

**Track container instance disconnects:**

```json
{
  "source": ["aws.ecs"],
  "detail-type": ["ECS Container Instance State Change"],
  "detail": {
    "agentConnected": [false]
  }
}
```

### 5.5 EventBridge Rule Targets

| Target | Use Case |
|--------|----------|
| **SNS** | Alert on-call team via PagerDuty/Slack |
| **Lambda** | Auto-remediation (restart service, scale up) |
| **SQS** | Buffer events for batch processing |
| **Step Functions** | Complex remediation workflows |
| **CloudWatch Logs** | Persistent event storage for audit |

### 5.6 Important Considerations

**Multiple events per action:** A single operation can generate multiple events.
For example, stopping a task generates:
1. Task state change (RUNNING → DEACTIVATING)
2. Task state change (DEACTIVATING → STOPPING)
3. Task state change (STOPPING → DEPROVISIONING)
4. Task state change (DEPROVISIONING → STOPPED)
5. Container instance state change (resources freed)

**Design for this:** Event consumers must handle multiple events per logical operation.

---

## 6. Service Events and Troubleshooting

### 6.1 Service Events

ECS displays the 100 most recent service events in the console and API. Two sources:

| Source | Prefix | Content |
|--------|--------|---------|
| Service scheduler | `service (name)` | Task placement, health, deployment events |
| Auto Scaling | `Message` | Scaling events (only with scaling policies) |

**Deduplication:** Identical messages are suppressed until the cause changes or 6 hours pass.

### 6.2 Common Service Event Messages

**Successful operations:**

```
service my-service has reached a steady state.
  → All desired tasks are running and healthy

service my-service has started 2 tasks: task abc123, task def456.
  → Tasks launched successfully

service my-service registered 1 targets in target-group arn:...
  → Task registered with load balancer
```

**Resource issues:**

```
service my-service was unable to place a task because no container instance
met all of its requirements.
  → Not enough capacity (CPU, memory, ports, constraints)

service my-service was unable to place a task. Reason: You've reached
the limit on the number of tasks you can run concurrently.
  → Hit service quota for running tasks

service my-service is unable to consistently start tasks successfully.
  → Circuit breaker is detecting repeated failures
```

**Health check issues:**

```
service my-service (instance i-abc123) (port 8080) is unhealthy in
target-group arn:... due to (reason Health checks failed).
  → ALB health check failing

service my-service has stopped 1 running tasks: task abc123.
(Reason: Task failed container health checks.)
  → Container HEALTHCHECK failing
```

**Deployment issues:**

```
service my-service deployment (id) completed.
  → Deployment finished successfully

service my-service deployment (id) initiated by user was ROLLBACK.
  → Deployment rolled back (circuit breaker or CloudWatch alarm)
```

### 6.3 Troubleshooting Flowchart

```
Task not starting?
  │
  ├─ "no container instance met all requirements"
  │   ├─ Check CPU/memory available on instances
  │   ├─ Check placement constraints (AZ, instance type)
  │   ├─ Check port conflicts (host/bridge mode)
  │   └─ Check available ENIs (awsvpc mode)
  │
  ├─ "CannotPullContainer"
  │   ├─ Check ECR repository exists and image tag is correct
  │   ├─ Check task execution role has ECR pull permissions
  │   ├─ Check network connectivity (NAT gateway for private subnet)
  │   └─ Check VPC endpoints if no internet access
  │
  ├─ "TaskFailedToStart"
  │   ├─ Check container command / entrypoint
  │   ├─ Check environment variables and secrets
  │   ├─ Check resource limits (memory too low → OOM)
  │   └─ Check CloudWatch Logs for container output
  │
  └─ "ResourceInitializationError"
      ├─ Check ENI creation (awsvpc — subnet capacity, SG limits)
      ├─ Check secrets retrieval (Secrets Manager / SSM permissions)
      └─ Check EFS mount (security group, mount target exists)

Task keeps crashing?
  │
  ├─ Exit code 137 → OOM killed (increase memory)
  ├─ Exit code 1   → Application error (check logs)
  ├─ Exit code 139 → Segfault (debug application)
  └─ Check CloudWatch Logs for the container

Health checks failing?
  │
  ├─ Container health check
  │   ├─ Is the health check command correct?
  │   ├─ Is the app listening on the right port?
  │   ├─ Is startPeriod long enough for app warmup?
  │   └─ Is timeout long enough for the check to complete?
  │
  └─ ELB health check
      ├─ Is the health check path correct?
      ├─ Is the security group allowing ALB → container traffic?
      ├─ Is healthCheckGracePeriodSeconds long enough?
      └─ Does the health endpoint return 200-299?
```

---

## 7. Stopped Task Error Codes

### 7.1 Stop Codes

| Stop Code | Meaning |
|-----------|---------|
| `TaskFailedToStart` | Task could not start — image pull, secrets, ENI, or container start failure |
| `EssentialContainerExited` | An essential container in the task stopped |
| `UserInitiated` | User or automation called StopTask API |
| `ServiceSchedulerInitiated` | Service scheduler stopped the task (scale-in, deployment, health check) |
| `SpotInterruption` | Fargate Spot or EC2 Spot capacity reclaimed |
| `TerminationNotice` | Fargate task retirement or EC2 instance termination |

### 7.2 Error Categories and Common Causes

| Error | Common Causes | Fix |
|-------|--------------|-----|
| `CannotPullContainer` | Wrong image name, no ECR permissions, no network access | Fix image URI, check execution role, check NAT/VPC endpoints |
| `ResourceInitializationError` | ENI creation failed, secret retrieval failed, EFS mount failed | Check subnet IPs, check IAM permissions, check SG rules |
| `OutOfMemoryError` | Container exceeded memory limit | Increase task memory, fix memory leak |
| `ContainerRuntimeError` | Container runtime (Docker/containerd) failure | Check container config, try redeploying |
| `ContainerRuntimeTimeoutError` | Container took too long to start | Increase timeout, optimize startup |
| `CannotStartContainerError` | Invalid entrypoint/command, missing dependencies | Fix Dockerfile CMD/ENTRYPOINT |
| `CannotCreateVolumeError` | EBS volume attachment failure | Check volume config, AZ placement |
| `InternalError` | AWS-side failure | Retry, contact support if persistent |

### 7.3 Exit Codes

| Exit Code | Signal | Meaning |
|-----------|--------|---------|
| 0 | — | Clean exit (success) |
| 1 | — | Application error (generic) |
| 2 | — | Shell misuse (bad command) |
| 126 | — | Command not executable |
| 127 | — | Command not found |
| 137 | SIGKILL (9) | OOM killed or force stopped |
| 139 | SIGSEGV (11) | Segmentation fault |
| 143 | SIGTERM (15) | Graceful termination (normal stop) |

**Critical:** Exit code 137 is the most common issue in production.

```
Exit code 137 diagnosis:
  1. Check if container memory usage approached the limit
     → Container Insights: MemoryUtilized vs MemoryReserved
  2. Check dmesg on the host (EC2): "Out of memory: Kill process"
  3. Check CloudWatch Logs for OOM messages from the runtime
  4. Fix: Increase task memory, or fix the memory leak
```

---

## 8. Logging

### 8.1 Log Drivers

ECS supports multiple log drivers in the task definition:

| Driver | Destination | Use Case |
|--------|------------|----------|
| `awslogs` | CloudWatch Logs | Default, simplest setup |
| `awsfirelens` | Any (Kinesis, S3, Elasticsearch, Datadog, etc.) | Flexible routing |
| `splunk` | Splunk | Enterprise Splunk users |
| `fluentd` | Fluentd collector | Self-managed log pipeline |
| `json-file` | Local file (EC2 only) | Debugging, not production |

### 8.2 awslogs Configuration

```json
{
  "containerDefinitions": [
    {
      "name": "my-app",
      "logConfiguration": {
        "logDriver": "awslogs",
        "options": {
          "awslogs-group": "/ecs/my-app",
          "awslogs-region": "us-east-1",
          "awslogs-stream-prefix": "ecs",
          "awslogs-create-group": "true"
        }
      }
    }
  ]
}
```

**Log stream naming:** `{prefix}/{container-name}/{task-id}`
- Example: `ecs/my-app/abc123def456`

**IAM requirement:** Task execution role needs:
```json
{
  "Effect": "Allow",
  "Action": [
    "logs:CreateLogStream",
    "logs:CreateLogGroup",
    "logs:PutLogEvents"
  ],
  "Resource": "arn:aws:logs:*:*:log-group:/ecs/*"
}
```

### 8.3 FireLens (Advanced Log Routing)

FireLens uses a Fluent Bit or Fluentd sidecar container for flexible log routing:

```
┌──────────────┐    ┌──────────────┐    ┌─────────────────┐
│  Application │───▶│  FireLens    │───▶│  CloudWatch     │
│  Container   │    │  (Fluent Bit)│    │  Kinesis Firehose│
│  stdout/err  │    │  sidecar     │    │  S3             │
└──────────────┘    └──────────────┘    │  Elasticsearch  │
                                        │  Datadog        │
                                        └─────────────────┘
```

**Use FireLens when:**
- Need to send logs to non-CloudWatch destinations
- Need log transformation/filtering before storage
- Want to split logs (errors → one destination, info → another)
- Need to add metadata to log entries

### 8.4 Logging Best Practices

1. **Always use structured logging** (JSON) — enables CloudWatch Insights queries
2. **Set log retention** — default is "Never expire" (costly). Set 30-90 days for most services.
3. **Use awslogs-create-group: true** — auto-creates the log group if it doesn't exist
4. **Include request IDs** in logs for tracing across services
5. **Don't log secrets** — sanitize before logging

---

## 9. Operational Patterns

### 9.1 Complete Monitoring Setup

A well-monitored ECS service should have:

```
┌──────────────────────────────────────────────────────────┐
│              Production Monitoring Checklist              │
├──────────────────────────────────────────────────────────┤
│                                                          │
│  ☐ CloudWatch Logs                                       │
│    ├─ awslogs or FireLens configured                    │
│    ├─ Log retention policy set                          │
│    └─ Metric filters for ERROR/WARN counts              │
│                                                          │
│  ☐ Container Insights (enhanced)                        │
│    ├─ CPU/Memory utilization at task level               │
│    └─ Network I/O at task level                          │
│                                                          │
│  ☐ ALB Metrics                                          │
│    ├─ HTTPCode_Target_5XX_Count alarm                   │
│    ├─ TargetResponseTime (p99) alarm                    │
│    └─ UnHealthyHostCount alarm                          │
│                                                          │
│  ☐ ECS Service Events                                   │
│    └─ Monitor via EventBridge → SNS                     │
│                                                          │
│  ☐ EventBridge Rules                                    │
│    ├─ Task STOPPED with error → alert                   │
│    ├─ Deployment failed/rollback → alert                │
│    └─ Container instance disconnect → alert             │
│                                                          │
│  ☐ Auto Scaling Alarms                                  │
│    ├─ Scale-out: CPU > 70% for 3 min                   │
│    ├─ Scale-in: CPU < 30% for 15 min                   │
│    └─ Max task count alarm (nearing quota)              │
│                                                          │
│  ☐ Deployment Safety                                    │
│    ├─ Circuit breaker enabled                           │
│    └─ CloudWatch deployment alarms configured           │
│                                                          │
└──────────────────────────────────────────────────────────┘
```

### 9.2 Capacity Planning

**EC2 Launch Type:**

```
Instance capacity check:
  For each instance:
    Available CPU = Total CPU - Reserved CPU (by running tasks)
    Available Memory = Total Memory - Reserved Memory

  Can a new task fit?
    task.cpu ≤ available_cpu AND task.memory ≤ available_memory

  Warning thresholds:
    Cluster CPU reserved > 75% → consider adding instances
    Cluster Memory reserved > 80% → consider adding instances

  Use managed scaling (capacity providers) to automate this.
```

**Fargate:**

```
Fargate capacity:
  No instance management needed
  But: account-level quotas still apply
    Default: 6 Fargate vCPUs on-demand (can request increase)
    Launch rate: 100 burst / 20 sustained per minute

  Monitor: RunningTaskCount vs DesiredTaskCount
  If RunningTaskCount consistently < DesiredTaskCount → hitting quota
```

### 9.3 Incident Response Patterns

**Pattern 1: Service not healthy (tasks keep restarting)**

```
1. Check service events: DescribeServices → events
2. Check stopped tasks: ListTasks (desiredStatus=STOPPED) → DescribeTasks
3. Look at stoppedReason and stopCode
4. Common resolutions:
   - OOM (137): Increase memory
   - Image pull failure: Check ECR permissions/image URI
   - Health check failure: Check health endpoint, increase grace period
   - Secrets failure: Check Secrets Manager/SSM permissions
```

**Pattern 2: High latency / errors after deployment**

```
1. Check deployment status: DescribeServices → deployments
2. If circuit breaker triggered → deployment auto-rolled back
3. If still in progress:
   - Check new task logs for errors
   - Check ALB 5xx metrics (did errors spike at deployment time?)
   - Manual rollback: UpdateService with previous task definition
4. Post-mortem: Was it a code bug or config issue?
```

**Pattern 3: Cannot place tasks**

```
1. Check service events for "unable to place a task"
2. Identify the constraint:
   - "no container instance met all requirements"
     → Check CPU/memory on instances
     → Check placement constraints
   - "Reason: You've reached the limit"
     → Request quota increase
   - "Could not find a Fargate capacity provider"
     → Check capacity provider strategy
3. Resolution:
   - Add more instances (EC2)
   - Request Fargate quota increase
   - Relax placement constraints
   - Use smaller task sizes
```

### 9.4 Exec into Running Containers

ECS Exec lets you run commands in or get a shell in a running container:

```bash
# Enable ECS Exec on the service
aws ecs update-service \
  --cluster my-cluster \
  --service my-service \
  --enable-execute-command

# Run a command
aws ecs execute-command \
  --cluster my-cluster \
  --task abc123 \
  --container my-container \
  --interactive \
  --command "/bin/sh"
```

**Requirements:**
- SSM Agent in the container (most base images include it)
- Task role with SSM permissions
- Platform version 1.4.0+ (Fargate)
- `enableExecuteCommand: true` on the service or RunTask call

**Use cases:**
- Debug connectivity issues from inside the container
- Check file system state
- Run diagnostic commands
- **NOT for production troubleshooting regularly** — use logging instead

### 9.5 Task-Level Metadata

Every ECS task has access to a metadata endpoint:

```
Container: http://169.254.170.2/v4/{container-id}
Task:      http://169.254.170.2/v4/{container-id}/task
Stats:     http://169.254.170.2/v4/{container-id}/stats
```

**Available metadata:**
- Task ARN, cluster, family, revision
- Container ID, name, image, health status
- Network interfaces, private IPs
- Resource limits (CPU, memory)
- Stats: CPU usage, memory usage, network I/O, block I/O

**Use case:** Application can self-report its ECS context for logging, metrics tagging,
and service discovery.

---

## 10. Interview Angles

### 10.1 "How do you monitor an ECS service in production?"

**Structured answer covering all layers:**

1. **Logs:** CloudWatch Logs (or FireLens for advanced routing). Structured JSON logging.
   Metric filters for error counts.

2. **Metrics:** Container Insights for CPU/memory/network. ALB metrics for HTTP
   error rates and latency. Custom application metrics via CloudWatch SDK.

3. **Events:** EventBridge rules for task failures, deployment rollbacks, and
   instance disconnects. Route to SNS → PagerDuty for on-call alerting.

4. **Health checks:** Container health check (application-level readiness), ELB health
   check (network-level reachability), with appropriate grace period.

5. **Deployment safety:** Circuit breaker for automatic rollback on task failures.
   CloudWatch alarms for application-level metrics during deployment.

### 10.2 "A task keeps crashing — walk me through your investigation"

```
Step 1: Identify the failure mode
  → DescribeServices → events (what does the service scheduler say?)
  → ListTasks → DescribeTasks (what's the stopCode and stoppedReason?)

Step 2: Check the exit code
  → 137: OOM → increase memory or fix leak
  → 1: App error → check logs
  → 143: Normal SIGTERM (expected during deployments)

Step 3: Check logs
  → CloudWatch Logs → look for errors before the crash
  → Was there an unhandled exception? A panic? A connection failure?

Step 4: Check resource utilization
  → Container Insights → was CPU/memory near the limit before crash?
  → Was the container hitting the ephemeral storage limit?

Step 5: Check if it's environmental
  → Does it crash in all AZs or just one?
  → Did a dependency go down? (database, external API)
  → Was there a recent deployment? (check deployment events)
```

### 10.3 "What's the difference between container health check and ELB health check?"

| Dimension | Container Health Check | ELB Health Check |
|-----------|----------------------|------------------|
| **Where it runs** | Inside the container (Docker) | From the load balancer |
| **What it checks** | Application-defined command | HTTP response or TCP connect |
| **Who defines it** | Task definition (or Dockerfile) | Target group settings |
| **Scope** | Single container | Task as reachable from LB |
| **Speed** | Fast (local) | Slower (network round trip) |
| **Required** | No | No (but recommended with LB) |
| **Without LB** | Only health signal | Not applicable |

**When you need both:**
- Container health check catches: application deadlocks, corrupted state, dependency failures
  that the app can self-detect
- ELB health check catches: network issues, port binding failures, crashes that prevent
  any response

### 10.4 "How does ECS handle Fargate Spot interruptions?"

```
1. AWS needs the capacity back
2. Task receives SIGTERM signal
3. Task has 2 minutes to shut down gracefully
4. EventBridge: Task state change with stopCode = "SpotInterruption"
5. Service scheduler launches replacement task (on Spot or On-Demand,
   depending on capacity provider strategy)

Operational implications:
  - Application MUST handle SIGTERM gracefully (drain connections, save state)
  - Use capacity provider strategy with On-Demand base for minimum capacity
  - Monitor SpotInterruption events for frequency
  - Keep stopTimeout ≤ 120s (Spot gives only 2 minutes)
```

### 10.5 "How would you debug 'unable to place a task' errors?"

```
service my-service was unable to place a task because no container
instance met all of its requirements.

Systematic check:
  1. CPU: Do any instances have enough free CPU?
     → DescribeContainerInstances → remainingResources
  2. Memory: Do any instances have enough free memory?
     → Same API call
  3. Ports: Is the host port already in use? (host/bridge mode)
     → Check port mappings of running tasks
  4. Placement constraints: Are constraints too restrictive?
     → distinctInstance with small cluster?
     → attribute constraint filtering out all instances?
  5. AZ balance: Is spread(az) constraint preventing placement?
     → All instances in one AZ, trying to spread?
  6. ENI limits: awsvpc mode — instance out of ENI capacity?
     → Check ENI trunk support and limits
  7. GPU/Inferentia: Specialized resources not available?

Resolution order:
  1. Check if existing instances have capacity (most common)
  2. Add more instances or enable managed scaling
  3. Relax placement constraints
  4. Use smaller task sizes
  5. Switch to Fargate (eliminates placement issues)
```

### 10.6 Design Decision: Why Multiple Health Check Layers?

**Defense in depth:**

```
Layer 1 (Container HC): "Can the application respond to requests?"
  → Catches: app deadlocks, corrupted state, dependency failures
  → Self-assessment — the app knows its own health best

Layer 2 (ELB HC): "Can the load balancer reach the application?"
  → Catches: network issues, security group problems, port binding failures
  → External assessment — validates end-to-end connectivity

Layer 3 (Agent heartbeat): "Is the host machine functioning?"
  → Catches: host failures, network partitions, agent crashes
  → Infrastructure assessment — validates the execution environment

No single layer catches everything. Together they provide comprehensive coverage.
```

---

## Appendix A: Quick Reference

### Key CloudWatch Metrics

| Metric | Source | What It Tells You |
|--------|--------|-------------------|
| `CPUUtilization` | ECS (Container Insights) | CPU usage % of reserved |
| `MemoryUtilization` | ECS (Container Insights) | Memory usage % of reserved |
| `RunningTaskCount` | ECS (Container Insights) | Active tasks in service |
| `HTTPCode_Target_5XX_Count` | ALB | Server errors from tasks |
| `TargetResponseTime` | ALB | Latency to tasks |
| `UnHealthyHostCount` | ALB Target Group | Tasks failing health checks |
| `HealthyHostCount` | ALB Target Group | Tasks passing health checks |

### Common Exit Codes

| Code | Meaning | Action |
|------|---------|--------|
| 0 | Success | Normal |
| 1 | App error | Check logs |
| 137 | OOM/SIGKILL | Increase memory |
| 139 | Segfault | Debug app |
| 143 | SIGTERM | Normal shutdown |

### EventBridge Event Types

| detail-type | When |
|-------------|------|
| `ECS Task State Change` | Any task state transition |
| `ECS Container Instance State Change` | Instance joins/leaves/changes |
| `ECS Deployment State Change` | Deployment starts/succeeds/fails |
| `ECS Service Action` | CreateService, UpdateService, etc. |

### Troubleshooting Quick Commands

```bash
# View service events
aws ecs describe-services --cluster CLUSTER --services SERVICE \
  --query 'services[0].events[:10]'

# View stopped tasks
aws ecs list-tasks --cluster CLUSTER --service-name SERVICE \
  --desired-status STOPPED

# Describe a stopped task
aws ecs describe-tasks --cluster CLUSTER --tasks TASK_ARN \
  --query 'tasks[0].{stop:stopCode,reason:stoppedReason,containers:containers[*].{name:name,exit:exitCode,reason:reason}}'

# View container logs
aws logs get-log-events --log-group-name /ecs/SERVICE \
  --log-stream-name ecs/CONTAINER/TASK_ID --limit 100

# Exec into running container
aws ecs execute-command --cluster CLUSTER --task TASK_ARN \
  --container CONTAINER --interactive --command "/bin/sh"
```
