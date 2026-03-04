# ECS Deployment Strategies — Deep Dive

## Table of Contents

1. [Overview](#1-overview)
2. [Rolling Update Deployment](#2-rolling-update-deployment)
3. [Blue/Green Deployment with CodeDeploy](#3-bluegreen-deployment-with-codedeploy)
4. [Deployment Circuit Breaker](#4-deployment-circuit-breaker)
5. [CloudWatch Alarms for Deployment Monitoring](#5-cloudwatch-alarms-for-deployment-monitoring)
6. [Container Image Version Consistency](#6-container-image-version-consistency)
7. [Deployment Strategy Comparison](#7-deployment-strategy-comparison)
8. [Advanced Deployment Patterns](#8-advanced-deployment-patterns)
9. [Interview Angles](#9-interview-angles)

---

## 1. Overview

ECS supports two primary deployment strategies for services:

| Strategy | Controller | Traffic Shifting | Rollback | Complexity |
|----------|-----------|------------------|----------|------------|
| **Rolling Update** | ECS service scheduler | At task level (register/deregister from LB) | Replace new tasks with previous revision | Low |
| **Blue/Green** | AWS CodeDeploy | At listener level (canary/linear/all-at-once) | Reroute traffic to original task set | Medium-High |

**When to use which:**

```
Rolling Update:
  ✓ Simpler services without strict traffic control needs
  ✓ When you want ECS-native deployment (no external dependency)
  ✓ Gradual replacement is sufficient
  ✓ Cost-sensitive (no double capacity needed for full duration)

Blue/Green (CodeDeploy):
  ✓ Need precise traffic shifting (canary 10%, then 90%)
  ✓ Want test traffic validation before production switch
  ✓ Need instant rollback capability
  ✓ Compliance requires pre-production validation hooks
  ✓ Using ALB or NLB (required)
```

A third option — **external deployment controller** — allows any third-party system to manage
task sets directly via the ECS API. This is rarely used and not covered in depth here.

### Deployment Controller Configuration

Set at service creation time — **cannot be changed after service creation**:

```json
{
  "deploymentController": {
    "type": "ECS"          // Rolling update (default)
    // OR
    "type": "CODE_DEPLOY"  // Blue/green with CodeDeploy
    // OR
    "type": "EXTERNAL"     // External controller
  }
}
```

---

## 2. Rolling Update Deployment

### 2.1 Core Mechanism

The ECS service scheduler manages rolling updates natively. When you update the service's
task definition (or other parameters), ECS replaces old tasks with new ones in a controlled
manner.

**Two parameters control the behavior:**

| Parameter | Default | Description |
|-----------|---------|-------------|
| `minimumHealthyPercent` | 100 | Lower limit on healthy tasks as a percentage of `desiredCount` |
| `maximumPercent` | 200 | Upper limit on total running tasks (old + new) as a percentage of `desiredCount` |

### 2.2 How the Math Works

**Key rule:** Both values are rounded to determine actual task counts.

- `minimumHealthyPercent` → rounds **up** (conservative — keep more healthy tasks)
- `maximumPercent` → rounds **down** (conservative — don't over-provision)

**Example 1: Default settings (100/200) with desiredCount = 4**

```
minimumHealthyPercent = 100% → min healthy = ceil(4 × 1.0) = 4
maximumPercent = 200%        → max total   = floor(4 × 2.0) = 8

Strategy: ECS can launch up to 4 NEW tasks first (reaching 8 total),
          then stop 4 OLD tasks.
          At no point do we drop below 4 healthy tasks.

Timeline:
  t0: [OLD OLD OLD OLD]                     = 4 running ✓ (≥4 healthy)
  t1: [OLD OLD OLD OLD NEW NEW NEW NEW]     = 8 running ✓ (≤8 max)
  t2: [NEW NEW NEW NEW]                     = 4 running ✓ (new tasks healthy)
```

**Example 2: Settings (50/200) with desiredCount = 4**

```
minimumHealthyPercent = 50% → min healthy = ceil(4 × 0.5) = 2
maximumPercent = 200%       → max total   = floor(4 × 2.0) = 8

Strategy: ECS can stop 2 OLD tasks immediately (keeping 2 healthy),
          then start up to 6 NEW tasks.
          Faster but less capacity during transition.

Timeline:
  t0: [OLD OLD OLD OLD]                 = 4 running
  t1: [OLD OLD]                         = 2 running ✓ (≥2 healthy)
  t2: [OLD OLD NEW NEW NEW NEW]         = 6 running ✓ (≤8 max)
  t3: [NEW NEW NEW NEW]                 = 4 running ✓
```

**Example 3: Settings (100/150) with desiredCount = 4**

```
minimumHealthyPercent = 100% → min healthy = ceil(4 × 1.0) = 4
maximumPercent = 150%        → max total   = floor(4 × 1.5) = 6

Strategy: ECS can only launch 2 NEW tasks at a time (6 - 4 = 2 headroom).
          Must wait for new tasks to become healthy, then stop 2 old.
          Slower but less resource spike.

Timeline:
  t0: [OLD OLD OLD OLD]                 = 4 running
  t1: [OLD OLD OLD OLD NEW NEW]         = 6 running ✓ (≤6 max)
  t2: [OLD OLD NEW NEW]                 = 4 running ✓ (2 old stopped, 2 new healthy)
  t3: [OLD OLD NEW NEW NEW NEW]         = 6 running
  t4: [NEW NEW NEW NEW]                 = 4 running ✓
```

**Example 4: Settings (0/100) — "replace in place"**

```
minimumHealthyPercent = 0%  → min healthy = 0
maximumPercent = 100%       → max total   = 4

Strategy: ECS stops ALL old tasks, then starts all new tasks.
          Full downtime but no extra capacity needed.
          Only use for non-production or batch workloads.

Timeline:
  t0: [OLD OLD OLD OLD]     = 4 running
  t1: []                    = 0 running (downtime!)
  t2: [NEW NEW NEW NEW]     = 4 running
```

### 2.3 Task Replacement Sequence

```
┌──────────────────────────────────────────────────────────┐
│              Rolling Update — Step by Step                │
├──────────────────────────────────────────────────────────┤
│                                                          │
│  1. User calls UpdateService (new task definition)       │
│              │                                           │
│              ▼                                           │
│  2. ECS calculates batch size from min/max params        │
│              │                                           │
│              ▼                                           │
│  3. Launch batch of NEW tasks                            │
│     (respecting maximumPercent ceiling)                  │
│              │                                           │
│              ▼                                           │
│  4. Wait for NEW tasks to reach RUNNING + pass           │
│     health checks (container HC + ELB HC)                │
│              │                                           │
│              ├─── If health checks PASS ──┐              │
│              │                            ▼              │
│              │                  5. Register new tasks     │
│              │                     with load balancer     │
│              │                            │              │
│              │                            ▼              │
│              │                  6. Drain old tasks        │
│              │                     (deregistration delay) │
│              │                            │              │
│              │                            ▼              │
│              │                  7. Stop old tasks         │
│              │                            │              │
│              │                            ▼              │
│              │                  8. Repeat from step 3     │
│              │                     until all replaced     │
│              │                                           │
│              ├─── If health checks FAIL ──┐              │
│              │                            ▼              │
│              │                  Circuit breaker           │
│              │                  (if enabled) or           │
│              │                  retry indefinitely        │
│                                                          │
└──────────────────────────────────────────────────────────┘
```

### 2.4 Load Balancer Integration During Rolling Update

When the service uses an ALB or NLB:

```
┌─────────────┐    ┌────────────┐    ┌────────────────┐
│   Client     │───▶│    ALB     │───▶│  Target Group  │
└─────────────┘    └────────────┘    └────────────────┘
                                            │
                                     ┌──────┴──────┐
                                     │             │
                                  Old Tasks    New Tasks
                                  (draining)   (healthy)
```

**Sequence during task replacement:**

1. **New task starts** → enters RUNNING state
2. **Container health check** passes (if defined)
3. **ECS registers new task** with target group
4. **ALB health check** on the new target passes → target becomes `healthy`
5. **ECS deregisters old task** from target group
6. **Deregistration delay** begins (default 300 seconds)
   - ALB stops sending new connections to old target
   - Existing connections are allowed to complete
7. **After deregistration delay** → ECS stops the old task

**Critical detail:** The deregistration delay (set on the target group) determines how long
in-flight requests have to complete. If your requests are long-running (WebSocket, file
uploads), set this higher. For fast APIs, 30-60 seconds is usually sufficient.

### 2.5 Task Replacement for Unhealthy Tasks

**Important distinction:** Unhealthy task replacement is independent from deployments.

- When ECS replaces a task that fails health checks, it uses the **same task definition
  revision** as the unhealthy task — NOT the target revision of an in-progress deployment
- This prevents a cascading failure where unhealthy tasks get replaced with an untested
  new revision
- The deployment process only replaces tasks from old → new revision when the new revision
  has proven healthy

### 2.6 Steady-State Behavior

Between deployments, the service scheduler continuously reconciles:

```
Every ~30 seconds [INFERRED]:
  actual_count = count tasks in (RUNNING + healthy)
  if actual_count < desiredCount:
    launch (desiredCount - actual_count) new tasks
  if actual_count > desiredCount:
    stop (actual_count - desiredCount) tasks
```

This handles:
- Tasks that crash or get OOM-killed
- Container instances that become unreachable
- Tasks that fail health checks

---

## 3. Blue/Green Deployment with CodeDeploy

### 3.1 Architecture Overview

Blue/green deployments use AWS CodeDeploy as the deployment controller instead of the
ECS service scheduler.

```
┌─────────────────────────────────────────────────────────────────┐
│                    Blue/Green Architecture                       │
├─────────────────────────────────────────────────────────────────┤
│                                                                 │
│  ┌──────────┐     ┌────────────────┐                           │
│  │CodeDeploy│────▶│ Deployment     │                           │
│  │          │     │ Group          │                           │
│  └──────────┘     └───────┬────────┘                           │
│                           │                                     │
│                    ┌──────┴──────┐                              │
│                    │             │                              │
│              ┌─────▼─────┐ ┌────▼──────┐                       │
│              │ Production│ │   Test    │ (optional)             │
│              │ Listener  │ │ Listener  │                       │
│              │ :443      │ │ :8443     │                       │
│              └─────┬─────┘ └────┬──────┘                       │
│                    │            │                               │
│              ┌─────┴────────────┴─────┐                        │
│              │         ALB            │                        │
│              └─────┬────────────┬─────┘                        │
│                    │            │                               │
│              ┌─────▼─────┐ ┌───▼───────┐                      │
│              │ Blue TG   │ │ Green TG  │                      │
│              │ (original)│ │ (replace- │                      │
│              │           │ │  ment)    │                      │
│              └─────┬─────┘ └────┬──────┘                      │
│                    │            │                               │
│              ┌─────▼─────┐ ┌───▼───────┐                      │
│              │ Blue Tasks│ │Green Tasks│                      │
│              │ (old rev) │ │(new rev)  │                      │
│              └───────────┘ └───────────┘                      │
│                                                                 │
└─────────────────────────────────────────────────────────────────┘
```

### 3.2 Required Infrastructure

| Component | Requirement |
|-----------|------------|
| Load balancer | ALB or NLB (required) |
| Target groups | **Two** — one for blue (original), one for green (replacement) |
| Production listener | Required — serves real traffic |
| Test listener | Optional — allows validation before production switch |
| CodeDeploy application | Must be created for ECS platform |
| CodeDeploy deployment group | Configures traffic shifting strategy |
| AppSpec file | Defines task definition and container/port mapping |
| IAM role | CodeDeploy service role with ECS permissions |

### 3.3 Traffic Shifting Strategies

CodeDeploy provides three traffic shifting strategies:

#### Canary

Shifts traffic in **two increments**: a small percentage first, then the remainder.

```
                    100% ─┐                          ┌─ 100%
                          │                          │
Traffic to       Canary   │   Wait period            │
Green (%)        shift    │   (validation)           │
                          │                          │
                    10% ──┼──────────────────────────┘
                          │
                     0% ──┘
                          ├──────────────────────────┤
                          t0        time             t1
```

**Pre-defined canary configurations:**

| Configuration | First Shift | Wait | Second Shift |
|--------------|-------------|------|--------------|
| `ECSCanary10Percent5Minutes` | 10% | 5 min | 90% |
| `ECSCanary10Percent15Minutes` | 10% | 15 min | 90% |

#### Linear

Shifts traffic in **equal increments** at regular intervals.

```
                    100% ─┐                               ┌─ 100%
                          │                          ┌────┘
Traffic to                │                     ┌────┘
Green (%)                 │                ┌────┘
                          │           ┌────┘
                          │      ┌────┘
                    10% ──┼─────┘
                          │
                     0% ──┘
                          ├───┬───┬───┬───┬───┬───┬───┬───┤
                          t0  t1  t2  t3  t4  t5  t6  t7  t8
```

**Pre-defined linear configurations:**

| Configuration | Increment | Interval | Total Time |
|--------------|-----------|----------|------------|
| `ECSLinear10PercentEvery1Minutes` | 10% | 1 min | 10 min |
| `ECSLinear10PercentEvery3Minutes` | 10% | 3 min | 30 min |

#### All-at-Once

Shifts 100% of traffic immediately.

```
                    100% ──────────────────────────────── 100%
Traffic to                │
Green (%)                 │
                     0% ──┘
                          t0
```

**Pre-defined configuration:**

| Configuration | Behavior |
|--------------|----------|
| `ECSAllAtOnce` | Immediate 100% shift |

**NLB limitation:** NLB only supports `ECSAllAtOnce` — canary and linear are not available
with NLB.

### 3.4 Deployment Lifecycle

```
┌────────────────────────────────────────────────────────────────────┐
│              Blue/Green Deployment — Full Lifecycle                 │
├────────────────────────────────────────────────────────────────────┤
│                                                                    │
│  1. CreateDeployment (CodeDeploy)                                  │
│              │                                                     │
│              ▼                                                     │
│  2. CodeDeploy reads AppSpec                                       │
│     → Identifies new task definition + container/port              │
│              │                                                     │
│              ▼                                                     │
│  3. Create GREEN task set                                          │
│     → Launch new tasks with new task definition                    │
│     → Register in green target group                               │
│              │                                                     │
│              ▼                                                     │
│  4. Wait for green tasks to pass health checks                     │
│              │                                                     │
│              ├─── Test listener configured? ──┐                    │
│              │        NO                      │ YES                │
│              │                                ▼                    │
│              │                      5a. Route test listener         │
│              │                          to green target group       │
│              │                                │                    │
│              │                                ▼                    │
│              │                      5b. Run BeforeAllowTestTraffic │
│              │                          hook (Lambda)              │
│              │                                │                    │
│              │                                ▼                    │
│              │                      5c. Run AfterAllowTestTraffic  │
│              │                          hook (Lambda)              │
│              │                                │                    │
│              ├────────────────────────────────┘                    │
│              ▼                                                     │
│  6. Run BeforeAllowTraffic hook (Lambda)                          │
│              │                                                     │
│              ▼                                                     │
│  7. Traffic shifting begins                                        │
│     → Canary: 10% → wait → 90%                                   │
│     → Linear: 10% increments every N minutes                      │
│     → All-at-once: immediate 100%                                 │
│              │                                                     │
│              ▼                                                     │
│  8. Run AfterAllowTraffic hook (Lambda)                           │
│              │                                                     │
│              ▼                                                     │
│  9. Termination wait period                                        │
│     (configurable, default 0 — blue tasks terminated immediately)  │
│              │                                                     │
│              ▼                                                     │
│  10. Terminate BLUE task set                                       │
│      → Deregister blue tasks from blue target group                │
│      → Stop blue tasks                                             │
│              │                                                     │
│              ▼                                                     │
│  11. Deployment SUCCEEDED                                          │
│                                                                    │
└────────────────────────────────────────────────────────────────────┘
```

### 3.5 AppSpec File

The AppSpec file tells CodeDeploy what to deploy:

```yaml
version: 0.0
Resources:
  - TargetService:
      Type: AWS::ECS::Service
      Properties:
        TaskDefinition: "arn:aws:ecs:us-east-1:123456789:task-definition/my-app:5"
        LoadBalancerInfo:
          ContainerName: "my-container"
          ContainerPort: 8080
        PlatformVersion: "LATEST"     # Fargate only
        CapacityProviderStrategy:      # Optional override
          - Base: 1
            CapacityProvider: "FARGATE"
            Weight: 1
          - Base: 0
            CapacityProvider: "FARGATE_SPOT"
            Weight: 1
Hooks:
  - BeforeInstall: "LambdaFunctionToValidateBeforeInstall"
  - AfterInstall: "LambdaFunctionToValidateAfterInstall"
  - AfterAllowTestTraffic: "LambdaFunctionToValidateTestTraffic"
  - BeforeAllowTraffic: "LambdaFunctionToValidateBeforeTraffic"
  - AfterAllowTraffic: "LambdaFunctionToValidateAfterTraffic"
```

### 3.6 Lambda Lifecycle Hooks

CodeDeploy can invoke Lambda functions at key points during the deployment:

| Hook | When | Use Case |
|------|------|----------|
| `BeforeInstall` | Before green task set is created | Validate config, check dependencies |
| `AfterInstall` | After green tasks are running | Smoke test the new tasks |
| `AfterAllowTestTraffic` | After test listener routes to green | Run integration tests via test endpoint |
| `BeforeAllowTraffic` | Before production traffic shifts | Final validation, feature flag check |
| `AfterAllowTraffic` | After traffic shift completes | Post-deployment validation, metrics check |

**Lambda hook contract:**

```python
import boto3

codedeploy = boto3.client('codedeploy')

def handler(event, context):
    deployment_id = event['DeploymentId']
    lifecycle_event_hook_execution_id = event['LifecycleEventHookExecutionId']

    # Run your validation logic here
    validation_passed = run_smoke_tests()

    codedeploy.put_lifecycle_event_hook_execution_status(
        deploymentId=deployment_id,
        lifecycleEventHookExecutionId=lifecycle_event_hook_execution_id,
        status='Succeeded' if validation_passed else 'Failed'
    )
```

If any hook returns `Failed`, CodeDeploy initiates an automatic rollback.

### 3.7 Rollback Behavior

**Automatic rollback triggers (configured at deployment group level):**

- Deployment fails (any lifecycle hook returns Failed)
- CloudWatch alarm triggers during deployment
- Manual stop of deployment with rollback option

**What happens during rollback:**

```
┌─────────────────────────────────────────────┐
│            Rollback Sequence                 │
├─────────────────────────────────────────────┤
│                                             │
│  1. Traffic immediately shifts back to      │
│     blue (original) target group            │
│     → 100% shift, no canary/linear          │
│                                             │
│  2. Green task set tasks are stopped        │
│                                             │
│  3. Green task set is deleted               │
│                                             │
│  4. Deployment marked as ROLLED_BACK        │
│                                             │
│  Note: Original blue tasks were NEVER       │
│  stopped, so rollback is instant            │
│                                             │
└─────────────────────────────────────────────┘
```

**Key advantage:** Because blue tasks remain running throughout the deployment, rollback
is near-instantaneous — no need to relaunch old tasks.

### 3.8 Auto Scaling Interaction

**Critical edge case:** If auto scaling triggers during a blue/green deployment:

- CodeDeploy waits up to 5 minutes for the scaling event to complete
- If scaling is not complete after 5 minutes, the deployment **fails**
- Recommendation: disable auto scaling during blue/green deployments or use scheduled
  deployments during low-traffic periods

### 3.9 IAM Requirements

Blue/green deployments require a CodeDeploy service role with extensive permissions:

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Effect": "Allow",
      "Action": [
        "ecs:CreateTaskSet",
        "ecs:UpdateServicePrimaryTaskSet",
        "ecs:DeleteTaskSet",
        "ecs:DescribeServices",
        "ecs:UpdateService"
      ],
      "Resource": "*"
    },
    {
      "Effect": "Allow",
      "Action": [
        "elasticloadbalancing:DescribeTargetGroups",
        "elasticloadbalancing:DescribeListeners",
        "elasticloadbalancing:ModifyListener",
        "elasticloadbalancing:DescribeRules",
        "elasticloadbalancing:ModifyRule"
      ],
      "Resource": "*"
    },
    {
      "Effect": "Allow",
      "Action": [
        "lambda:InvokeFunction"
      ],
      "Resource": "arn:aws:lambda:*:*:function:CodeDeployHooks-*"
    },
    {
      "Effect": "Allow",
      "Action": [
        "cloudwatch:DescribeAlarms"
      ],
      "Resource": "*"
    },
    {
      "Effect": "Allow",
      "Action": [
        "sns:Publish"
      ],
      "Resource": "arn:aws:sns:*:*:CodeDeployTopic*"
    },
    {
      "Effect": "Allow",
      "Action": [
        "s3:GetObject",
        "s3:GetObjectVersion"
      ],
      "Resource": "arn:aws:s3:::*/CodeDeploy/*"
    },
    {
      "Effect": "Allow",
      "Action": [
        "iam:PassRole"
      ],
      "Resource": "*",
      "Condition": {
        "StringLike": {
          "iam:PassedToService": "ecs-tasks.amazonaws.com"
        }
      }
    }
  ]
}
```

---

## 4. Deployment Circuit Breaker

### 4.1 The Problem

Without a circuit breaker, a bad deployment can loop forever:
- New task starts → fails health check → gets replaced → new task starts → fails → repeat
- ECS keeps trying to launch new tasks indefinitely
- Old tasks may be stopped (depending on min/max settings), reducing capacity
- Manual intervention required to rollback

### 4.2 How the Circuit Breaker Works

The deployment circuit breaker is available for **rolling update** deployments only
(ECS deployment controller). It automatically detects failed deployments and optionally
rolls back.

```json
{
  "deploymentConfiguration": {
    "deploymentCircuitBreaker": {
      "enable": true,
      "rollback": true
    }
  }
}
```

**Detection logic** [INFERRED]:

```
The circuit breaker tracks deployment failures using a threshold formula:

  failure_threshold = min(10, max(2, desiredCount / 2))

  Examples:
    desiredCount = 1  → threshold = 2   (minimum)
    desiredCount = 4  → threshold = 2
    desiredCount = 6  → threshold = 3
    desiredCount = 10 → threshold = 5
    desiredCount = 20 → threshold = 10
    desiredCount = 50 → threshold = 10  (maximum)

When consecutive task launch failures reach the threshold:
  → If rollback = true:  deployment rolls back to last successful revision
  → If rollback = false: deployment stops (stuck in FAILED state)
```

### 4.3 Circuit Breaker States

```
┌──────────────────────────────────────────────────────────┐
│                                                          │
│  IN_PROGRESS ──failed tasks──▶ MONITORING                │
│       │                            │                     │
│       │                      ┌─────┴──────┐              │
│       │                      │            │              │
│       │               tasks recover   threshold hit      │
│       │                      │            │              │
│       │                      ▼            ▼              │
│       ├─── all replaced ──▶ COMPLETED   ROLLBACK         │
│       │                                   │              │
│       │                                   ▼              │
│       │                              rolled back to      │
│       │                              previous revision   │
│                                           │              │
│                                           ▼              │
│                                      COMPLETED           │
│                                      (rollback done)     │
│                                                          │
└──────────────────────────────────────────────────────────┘
```

### 4.4 What Counts as a Failure

A task is considered a failure when it:
- Fails to reach RUNNING state
- Fails the container health check
- Fails the ELB health check
- Gets stopped by ECS (OOM, essential container exit)

### 4.5 Circuit Breaker + Minimum Healthy Percent Interaction

```
Scenario: desiredCount = 4, minimumHealthyPercent = 100%, maximumPercent = 200%
          Circuit breaker enabled with rollback = true

Step 1: ECS launches 4 new tasks (can have 8 total)
Step 2: All 4 new tasks fail health checks → 4 failures
Step 3: Circuit breaker threshold = min(10, max(2, 4/2)) = 2
Step 4: 4 failures > 2 threshold → CIRCUIT BREAKER TRIPPED
Step 5: Rollback — stop failed new tasks, keep old tasks
Step 6: Service is back to 4 old healthy tasks
```

Because `minimumHealthyPercent = 100%`, the old tasks were never stopped, so rollback
is just "stop the new broken tasks."

---

## 5. CloudWatch Alarms for Deployment Monitoring

### 5.1 Overview

You can configure CloudWatch alarms to automatically fail (and optionally rollback)
deployments based on **application metrics**, not just task health.

This catches scenarios the circuit breaker misses:
- Tasks are running but returning 5xx errors
- Tasks are running but latency has spiked
- Tasks are running but business metrics have dropped

### 5.2 Configuration

```json
{
  "deploymentConfiguration": {
    "alarms": {
      "alarmNames": [
        "HighErrorRate-MyService",
        "HighLatency-MyService",
        "LowSuccessRate-MyService"
      ],
      "enable": true,
      "rollback": true
    }
  }
}
```

**Behavior:**
- During deployment: if any named alarm enters `ALARM` state → deployment fails
- If `rollback = true` → automatic rollback to previous revision
- If `rollback = false` → deployment stops, manual intervention needed

### 5.3 Combining with Circuit Breaker

Both mechanisms can be enabled simultaneously:

```json
{
  "deploymentConfiguration": {
    "deploymentCircuitBreaker": {
      "enable": true,
      "rollback": true
    },
    "alarms": {
      "alarmNames": ["HighErrorRate"],
      "enable": true,
      "rollback": true
    },
    "minimumHealthyPercent": 100,
    "maximumPercent": 200
  }
}
```

**Order of defense:**

| Layer | What it catches | Speed |
|-------|----------------|-------|
| Circuit breaker | Tasks that can't start or immediately crash | Fast (seconds) |
| CloudWatch alarms | Tasks running but misbehaving | Slower (minutes) |

### 5.4 CloudWatch Alarm Best Practices for Deployments

**Recommended alarms:**

```
1. HTTP 5xx rate > 5% for 2 consecutive periods (1 min each)
   → Catches application errors from new code

2. P99 latency > 2x baseline for 3 consecutive periods (1 min each)
   → Catches performance regressions

3. Target group unhealthy host count > 0 for 2 periods
   → Catches health check failures at the ALB level

4. Business metric (e.g., orders/min) drops > 30% from baseline
   → Catches subtle bugs that don't cause errors but break functionality
```

---

## 6. Container Image Version Consistency

### 6.1 The Problem

When you use a mutable image tag like `:latest` or `:v2`, the actual image content can
change between when you start a deployment and when ECS launches each task.

```
Scenario without version consistency:

  t0: Deploy my-app:latest (points to digest sha256:abc123)
  t1: Task 1 launches → pulls my-app:latest → gets sha256:abc123 ✓
  t2: Someone pushes new image tagged :latest → now sha256:def456
  t3: Task 2 launches → pulls my-app:latest → gets sha256:def456 ✗

  Result: Tasks 1 and 2 are running DIFFERENT code!
```

### 6.2 Version Consistency Feature

ECS resolves the container image tag to an **image digest** at the start of the deployment
and ensures all tasks use that same digest.

**Configuration:**

```json
{
  "service": {
    "deploymentConfiguration": {
      "versionConsistency": "enabled"
    }
  }
}
```

**Behavior when enabled:**

```
  t0: Deploy my-app:latest
  t1: ECS resolves :latest → sha256:abc123, stores digest
  t2: Task 1 launches → pulls sha256:abc123 ✓
  t3: Someone pushes new :latest → now sha256:def456
  t4: Task 2 launches → pulls sha256:abc123 ✓ (uses stored digest!)

  Result: All tasks in this deployment run sha256:abc123
```

### 6.3 When Resolution Happens

- Image digest is resolved when the **deployment is created** (UpdateService or CreateService)
- The resolved digest is stored with the deployment
- All tasks in that deployment use the stored digest
- A new deployment resolves the tag again (gets the latest digest at that point)

### 6.4 Interaction with Unhealthy Task Replacement

Unhealthy task replacement (outside of a deployment) uses the task definition's original
image reference — NOT the deployment's resolved digest. This is by design: unhealthy
task replacement should use the known-good version the task was originally deployed with.

---

## 7. Deployment Strategy Comparison

### 7.1 Feature Comparison

| Feature | Rolling Update | Blue/Green (CodeDeploy) |
|---------|---------------|------------------------|
| **Deployment controller** | ECS | CodeDeploy |
| **Load balancer required** | No (but recommended) | Yes (ALB or NLB) |
| **Traffic shifting** | Task-level (register/deregister) | Listener-level (canary/linear/all-at-once) |
| **Rollback speed** | Minutes (must relaunch old tasks) | Seconds (old tasks still running) |
| **Rollback mechanism** | New deployment with old revision | Traffic shift back to blue |
| **Validation hooks** | None | Lambda hooks at 5 lifecycle points |
| **Test traffic** | Not supported | Optional test listener |
| **Circuit breaker** | Yes (native) | Via CloudWatch alarms |
| **CloudWatch alarms** | Yes | Yes |
| **Max concurrent versions** | 2 (old + new) | 2 (blue + green) |
| **Extra capacity needed** | Depends on min/max settings | Full duplicate during deployment |
| **Setup complexity** | Low | Medium-High |
| **Fargate support** | Yes | Yes |
| **EC2 support** | Yes | Yes |
| **Can change after creation** | N/A (default) | No (set at service creation) |

### 7.2 Cost Comparison

```
Rolling Update (100/200):
  Steady state: N tasks
  During deployment: up to 2N tasks (briefly)
  Extra cost: ~2x for minutes during deployment

Rolling Update (50/100):
  Steady state: N tasks
  During deployment: N tasks (some old, some new)
  Extra cost: ~0 (but reduced capacity)

Blue/Green:
  Steady state: N tasks
  During deployment: 2N tasks (full duration of traffic shifting)
  Extra cost: ~2x for entire deployment duration (could be 30+ minutes)
  Additional cost: CodeDeploy (free for ECS), Lambda hooks (minimal)
```

### 7.3 Decision Matrix

```
┌─────────────────────────────────────┬────────────┬────────────┐
│ Scenario                            │  Rolling   │ Blue/Green │
├─────────────────────────────────────┼────────────┼────────────┤
│ Simple web service, low traffic     │    ✓✓✓     │     ✓      │
│ High-traffic API, zero-downtime     │     ✓✓     │    ✓✓✓     │
│ Need canary testing (% of traffic)  │            │    ✓✓✓     │
│ Need pre-deployment validation      │            │    ✓✓✓     │
│ Need instant rollback               │      ✓     │    ✓✓✓     │
│ Cost-sensitive                      │    ✓✓✓     │     ✓      │
│ Minimal setup / operational burden  │    ✓✓✓     │      ✓     │
│ No load balancer                    │    ✓✓✓     │            │
│ Compliance / audit requirements     │      ✓     │    ✓✓✓     │
│ Batch / worker services             │    ✓✓✓     │      ✓     │
│ Multiple target groups (5)          │    ✓✓✓     │     ✓✓     │
└─────────────────────────────────────┴────────────┴────────────┘
```

### 7.4 Rolling Update Is Sufficient When...

1. Your service is behind an ALB with health checks (automatic bad-deploy protection)
2. You have the circuit breaker enabled (automatic rollback on failure)
3. You have CloudWatch alarms configured (catches app-level issues)
4. Your deployment cadence is moderate (not deploying 50x/day)
5. You don't need canary testing at the traffic level

**This covers the majority of ECS services.**

### 7.5 Blue/Green Is Worth the Complexity When...

1. You need canary deployments — route 10% of real traffic to new version first
2. You need lifecycle hooks — run integration tests before allowing production traffic
3. You need a test listener — separate endpoint for QA validation
4. Instant rollback is critical — cannot afford minutes of relaunch time
5. Regulatory requirements mandate pre-production validation gates

---

## 8. Advanced Deployment Patterns

### 8.1 Zero-Downtime Rolling Update Pattern

```json
{
  "service": {
    "desiredCount": 4,
    "deploymentConfiguration": {
      "minimumHealthyPercent": 100,
      "maximumPercent": 200,
      "deploymentCircuitBreaker": {
        "enable": true,
        "rollback": true
      },
      "alarms": {
        "alarmNames": ["HighErrorRate", "HighLatency"],
        "enable": true,
        "rollback": true
      }
    },
    "healthCheckGracePeriodSeconds": 60
  }
}
```

**Target group settings:**
- Health check interval: 10 seconds
- Healthy threshold: 2 (20 seconds to declare healthy)
- Unhealthy threshold: 3 (30 seconds to declare unhealthy)
- Deregistration delay: 60 seconds (for in-flight requests)

**Timeline for this config:**

```
t0:00  — UpdateService called with new task definition
t0:05  — 4 new tasks PROVISIONING (reaching maximumPercent = 8)
t0:15  — 4 new tasks RUNNING, container health checks start
t0:30  — Container health checks pass, registered with ALB
t0:50  — ALB health checks pass (2 consecutive passes at 10s interval)
t0:50  — 4 old tasks deregistered from ALB
t1:50  — Deregistration delay expires (60s), old tasks stopped
t1:55  — Deployment complete, 4 new tasks serving traffic
         Total duration: ~2 minutes
```

### 8.2 Canary Blue/Green Pattern

```
CodeDeploy deployment group config:
  Traffic routing: ECSCanary10Percent15Minutes
  Original revision termination: Wait 60 minutes
  Auto rollback: On deployment failure + on alarm

Lifecycle hooks:
  AfterInstall: Smoke test Lambda
  AfterAllowTestTraffic: Integration test Lambda (via test listener)
  AfterAllowTraffic: Metrics validation Lambda

Timeline:
  t0:00  — Deployment starts
  t0:05  — Green tasks launched and running
  t0:10  — AfterInstall hook — smoke tests pass
  t0:15  — Test listener routes to green
  t0:20  — AfterAllowTestTraffic — integration tests pass
  t0:25  — BeforeAllowTraffic hook — checks pass
  t0:25  — 10% production traffic shifted to green
  t15:25 — 15 minutes at 10% — metrics look good
  t15:25 — Remaining 90% shifted to green
  t15:30 — AfterAllowTraffic — post-deploy validation passes
  t75:30 — 60-minute termination wait expires
  t75:35 — Blue tasks terminated
  t75:35 — Deployment complete
           Total duration: ~75 minutes (most is wait time)
```

### 8.3 Fast Rollback Pattern for Critical Services

For services where rollback speed is paramount:

```
Use Blue/Green with ECSAllAtOnce:
  → Green tasks launched in parallel with blue
  → Full validation via hooks before traffic switch
  → Traffic shifts instantly (all-at-once)
  → If anything goes wrong: instant rollback to blue
  → Blue tasks kept alive for 30 minutes after switch

This gives you:
  ✓ Pre-deployment validation (hooks)
  ✓ Fast switch (~seconds)
  ✓ Instant rollback (~seconds)
  ✗ No gradual traffic shifting (but tests compensate)
```

### 8.4 Multi-Service Coordinated Deployment

When multiple ECS services need to deploy together (e.g., API + Worker + Gateway):

**Approach 1: CodePipeline orchestration**

```
CodePipeline:
  Stage 1 (Source):   ECR image push triggers pipeline
  Stage 2 (Deploy):
    Action 1: Deploy Gateway service (blue/green)  ── parallel
    Action 2: Deploy API service (blue/green)       ── parallel
    Action 3: Deploy Worker service (rolling update) ── parallel
  Stage 3 (Validate): Lambda runs end-to-end tests
  Stage 4 (Approve):  Manual approval (optional)
```

**Approach 2: Step Functions orchestration**

```
Step Function:
  1. Update all task definitions (parallel)
  2. Deploy backend services (parallel)
  3. Wait for backend health
  4. Deploy frontend service
  5. Run integration tests
  6. If tests fail → rollback all services
```

### 8.5 Feature Flag Deployment Pattern

Decouple code deployment from feature release:

```
1. Deploy new code with feature behind flag (OFF)
   → Rolling update, fast, no risk

2. Enable feature flag for 5% of users
   → No ECS deployment needed

3. Monitor metrics for flagged users

4. Gradually increase to 100%

5. Clean up: remove flag, deploy cleanup code

Advantage: Rollback is instant (flip the flag)
           No need for blue/green complexity
           Works with simple rolling updates
```

---

## 9. Interview Angles

### 9.1 "Walk me through how you'd deploy a new version of an ECS service"

**Strong answer structure:**

1. **Choose strategy based on requirements:**
   - "For most services, rolling update with circuit breaker is sufficient"
   - "For critical, high-traffic services, blue/green with canary gives more control"

2. **Explain the mechanics:**
   - Rolling: "minimumHealthyPercent ensures we never drop below N healthy tasks,
     maximumPercent caps total capacity during transition"
   - Blue/green: "CodeDeploy creates a green task set, shifts traffic gradually,
     keeps blue alive for instant rollback"

3. **Address safety:**
   - "Circuit breaker catches tasks that can't start"
   - "CloudWatch alarms catch tasks that start but misbehave"
   - "Both can trigger automatic rollback"

### 9.2 "How does blue/green differ from rolling update in ECS?"

| Dimension | Key Points |
|-----------|-----------|
| **Traffic control** | Rolling: task-level. Blue/green: listener-level with canary/linear |
| **Rollback** | Rolling: new deployment (minutes). Blue/green: traffic reroute (seconds) |
| **Validation** | Rolling: health checks only. Blue/green: Lambda hooks + test traffic |
| **Cost** | Rolling: brief capacity spike. Blue/green: 2x capacity for full deployment |
| **Complexity** | Rolling: native ECS. Blue/green: CodeDeploy + ALB + 2 target groups |

### 9.3 "A deployment is stuck — tasks keep failing. What do you do?"

```
Without circuit breaker:
  1. Check ECS events: DescribeServices → events
  2. Check stopped task reason: DescribeTasks → stoppedReason
  3. Common causes:
     - Image pull failure → ECR permissions, image doesn't exist
     - Container crashes → check CloudWatch Logs
     - Health check failure → health check path, port, grace period
     - Resource constraints → not enough CPU/memory on instances
  4. Manual rollback: UpdateService with previous task definition

With circuit breaker:
  1. Circuit breaker auto-detects failures
  2. If rollback=true: automatically reverts to last good revision
  3. Check deployment events to understand what failed
  4. Fix the issue, deploy again
```

### 9.4 "How would you achieve zero-downtime deployment?"

**Required components:**
1. `minimumHealthyPercent = 100` — never fewer healthy tasks than desired
2. `maximumPercent >= 150` — headroom to launch new before stopping old
3. ALB with health checks — only route traffic to healthy tasks
4. `healthCheckGracePeriodSeconds` — give new tasks time to warm up
5. Deregistration delay — allow in-flight requests to complete
6. Circuit breaker — auto-rollback if new tasks can't start

**The math:** New tasks must be healthy and registered with ALB BEFORE old tasks
are deregistered. This is the fundamental guarantee.

### 9.5 "Why can't you change the deployment controller after service creation?"

The deployment controller (ECS vs CodeDeploy vs External) determines the fundamental
architecture of the service:

- **ECS controller:** single primary deployment, task-level replacement
- **CodeDeploy controller:** task sets (blue/green), listener manipulation, external
  deployment lifecycle management
- **External controller:** task sets managed by third-party

These are fundamentally different state machines. Switching mid-service would require
migrating state between controllers, which could leave the service in an inconsistent
state. It's safer to require a new service creation.

### 9.6 "How does minimumHealthyPercent interact with desiredCount of 1?"

```
desiredCount = 1, minimumHealthyPercent = 100:
  min healthy = ceil(1 × 1.0) = 1
  Can never stop the old task before new task is healthy
  → Must use maximumPercent > 100 (e.g., 200) to allow 2 tasks temporarily

desiredCount = 1, minimumHealthyPercent = 0:
  min healthy = 0
  Can stop old task first, then start new task
  → Brief downtime, but no extra capacity needed

desiredCount = 1, minimumHealthyPercent = 100, maximumPercent = 100:
  min healthy = 1, max total = 1
  → DEADLOCK: can't stop old (need 1 healthy) and can't start new (at max)
  → Deployment will never progress!
```

**This is a common interview gotcha.** With `desiredCount=1`, you MUST have either
`minimumHealthyPercent < 100` (accept downtime) or `maximumPercent > 100` (accept
temporary over-provisioning).

### 9.7 Design Decision: Why Two Separate Strategies?

**Why not just blue/green for everything?**

1. **Cost:** Blue/green requires 2x capacity for the entire deployment duration.
   Rolling updates only briefly exceed desired capacity.

2. **Complexity:** Blue/green requires ALB + 2 target groups + CodeDeploy + IAM role +
   AppSpec file. Rolling update requires nothing beyond the ECS service definition.

3. **LB requirement:** Many ECS services don't use a load balancer (workers, batch
   processors, internal services using Service Connect). These can only use rolling
   updates.

4. **Speed for simple cases:** Rolling update for a 4-task service takes ~2 minutes.
   Blue/green with canary takes 15-75 minutes.

**Why not just rolling update for everything?**

1. **No traffic-level control:** Rolling update replaces tasks one at a time. You can't
   say "send 10% of traffic to new version" — you can only control how many tasks exist.

2. **Slow rollback:** Rolling update rollback means creating a new deployment with the
   old revision, which takes minutes. Blue/green rollback is a traffic reroute (seconds).

3. **No validation hooks:** Can't run automated tests between "new tasks are running"
   and "new tasks receive production traffic."

---

## Appendix A: Quick Reference

### Rolling Update Parameters

| Parameter | Range | Default | Effect |
|-----------|-------|---------|--------|
| `minimumHealthyPercent` | 0-100 | 100 | Min healthy tasks as % of desired |
| `maximumPercent` | 100-200 | 200 | Max total tasks as % of desired |
| `healthCheckGracePeriodSeconds` | 0-2,147,483,647 | 0 | Seconds before health check failures count |

### Blue/Green Pre-defined Configs

| Name | Type | First Shift | Wait | Remaining |
|------|------|-------------|------|-----------|
| `ECSCanary10Percent5Minutes` | Canary | 10% | 5 min | 90% |
| `ECSCanary10Percent15Minutes` | Canary | 10% | 15 min | 90% |
| `ECSLinear10PercentEvery1Minutes` | Linear | 10% | 1 min each | 10 min total |
| `ECSLinear10PercentEvery3Minutes` | Linear | 10% | 3 min each | 30 min total |
| `ECSAllAtOnce` | All-at-once | 100% | — | — |

### Circuit Breaker Threshold Formula

```
threshold = min(10, max(2, desiredCount / 2))
```

### Deployment Decision Flowchart

```
Need traffic-level canary?
  ├─ YES → Blue/Green
  │
  └─ NO → Need lifecycle hooks / test traffic?
           ├─ YES → Blue/Green
           │
           └─ NO → Need instant rollback?
                    ├─ YES → Blue/Green (AllAtOnce)
                    │
                    └─ NO → Rolling Update + Circuit Breaker
```
