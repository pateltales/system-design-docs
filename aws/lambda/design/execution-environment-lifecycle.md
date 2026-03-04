# Lambda Execution Environment Lifecycle — Deep Dive

> **Companion doc for:** [interview-simulation.md](interview-simulation.md) — Phase 5+ (Execution Environment)
> **Last verified:** February 2026 against AWS Lambda documentation

---

## Table of Contents

1. [Overview](#1-overview)
2. [The Three Phases — Init, Invoke, Shutdown](#2-the-three-phases--init-invoke-shutdown)
3. [Init Phase — Cold Start Anatomy](#3-init-phase--cold-start-anatomy)
4. [Invoke Phase — Request Processing](#4-invoke-phase--request-processing)
5. [Shutdown Phase — Cleanup](#5-shutdown-phase--cleanup)
6. [Warm Starts — Sandbox Reuse](#6-warm-starts--sandbox-reuse)
7. [Frozen and Thawed States](#7-frozen-and-thawed-states)
8. [Cold Start Deep Dive — What Takes Time?](#8-cold-start-deep-dive--what-takes-time)
9. [SnapStart — Checkpoint/Restore](#9-snapstart--checkpointrestore)
10. [Provisioned Concurrency — Pre-Warmed Sandboxes](#10-provisioned-concurrency--pre-warmed-sandboxes)
11. [Concurrency and Scaling Model](#11-concurrency-and-scaling-model)
12. [Extensions Lifecycle](#12-extensions-lifecycle)
13. [/tmp Storage and State Persistence](#13-tmp-storage-and-state-persistence)
14. [Suppressed Init — Re-Initialization After Failure](#14-suppressed-init--re-initialization-after-failure)
15. [Lambda Quotas Reference](#15-lambda-quotas-reference)
16. [Design Decision Analysis](#16-design-decision-analysis)
17. [Interview Angles](#17-interview-angles)

---

## 1. Overview

Every Lambda function invocation runs inside an **execution environment** — an isolated sandbox based on a Firecracker microVM. The execution environment has a well-defined lifecycle that determines cold start latency, warm start behavior, and resource utilization.

Understanding this lifecycle is the key to understanding Lambda's performance characteristics and the architectural decisions that drive cold start optimization.

### The Core Insight

```
Lambda's execution model:

  Traditional server:  Server boots → stays running forever → handles many requests
  Lambda:              Sandbox created → handles one request at a time → frozen → thawed → handles next request → eventually destroyed

  The lifecycle IS the product. Every optimization (SnapStart, provisioned concurrency,
  warm starts) is about manipulating this lifecycle.
```

---

## 2. The Three Phases — Init, Invoke, Shutdown

```
                    EXECUTION ENVIRONMENT LIFECYCLE

  ┌────────────────┐    ┌────────────────┐    ┌────────────────┐
  │   INIT PHASE    │───▶│  INVOKE PHASE   │───▶│ SHUTDOWN PHASE │
  │                │    │                │    │                │
  │ • Download code│    │ • Run handler  │    │ • Signal       │
  │ • Start runtime│    │ • Process event│    │   extensions   │
  │ • Init function│    │ • Return result│    │ • Clean up     │
  │ • Init exts    │    │                │    │ • Terminate    │
  │                │    │                │    │                │
  │  "Cold Start"  │    │ Can repeat     │    │ After idle     │
  │  One-time cost │    │ many times     │    │ period         │
  └────────────────┘    │ (warm starts)  │    └────────────────┘
                        └────────────────┘
                              │    ▲
                              │    │
                         Freeze / Thaw
                        (between invocations)
```

### Phase Summary

| Phase | When It Runs | Duration | Billed? |
|---|---|---|---|
| **Init** | First invocation only (cold start) | Up to 10 seconds (standard); up to 15 min (provisioned concurrency/SnapStart) | Yes |
| **Invoke** | Every invocation | Up to 900 seconds (15 min) | Yes |
| **Shutdown** | After idle period (no invocations) | 0-2000 ms (depends on extensions) | No |

---

## 3. Init Phase — Cold Start Anatomy

The Init phase runs **once** when a new execution environment is created. It consists of three sequential sub-phases:

```
INIT PHASE
│
├── 1. Extension Init
│   └── Start all external extensions (from /opt/extensions/)
│       Extensions run in parallel with each other
│
├── 2. Runtime Init
│   └── Bootstrap the runtime (Node.js, Python, Java, etc.)
│       Load the runtime and prepare to execute function code
│
├── 3. Function Init
│   └── Run static initialization code (code outside the handler)
│       Import libraries, open DB connections, load config
│
└── [SnapStart only] 4. Before-Checkpoint Runtime Hooks
    └── Run hooks before taking the microVM snapshot
```

### What Happens in Each Sub-Phase

**Extension Init:**
- Lambda searches `/opt/extensions/` for executable files
- Each file is treated as an extension bootstrap
- All extensions start **in parallel**
- Extensions register with the Extensions API
- Extensions signal readiness via the `Next` API

**Runtime Init:**
- The runtime (e.g., Python 3.12 interpreter) starts
- Runtime loads its dependencies
- Runtime registers with the Runtime API
- Runtime signals readiness via the `Next` API

**Function Init:**
- Code outside the handler function runs
- This is where SDK clients, DB connections, and config are initialized
- This is typically the **largest contributor to cold start latency**

### Init Phase Timeout

| Configuration | Init Timeout |
|---|---|
| **Standard function** | 10 seconds |
| **Provisioned concurrency** | Up to 15 minutes (max of 130 seconds or configured timeout) |
| **SnapStart** | Up to 15 minutes |
| **Managed instances** | Up to 15 minutes |

If Init exceeds the timeout, the execution environment is terminated and the invocation fails.

### Init Failure Logging

```
# Timeout during init
INIT_REPORT Init Duration: 10000.04 ms Phase: init Status: timeout

# Extension crash during init
INIT_REPORT Init Duration: 1236.04 ms Phase: init Status: error Error Type: Extension.Crash
```

Note: Successful Init phases don't emit INIT_REPORT unless using SnapStart or provisioned concurrency.

---

## 4. Invoke Phase — Request Processing

The Invoke phase runs for **every invocation** — both cold starts (after Init) and warm starts (sandbox reused).

```
INVOKE PHASE
│
├── Lambda sends Invoke event to runtime
│   └── Runtime calls the function handler with event + context
│
├── Function handler executes
│   └── Process event, make API calls, compute results
│
├── Function returns response
│   └── Runtime sends response back to Lambda via Runtime API
│
└── Extensions receive Invoke event
    └── Extensions can process telemetry, logs, etc.
```

### Key Properties

| Property | Value |
|---|---|
| **Timeout** | Up to 900 seconds (15 minutes), configurable per function |
| **Concurrency** | One invocation at a time per execution environment |
| **Payload (sync)** | 6 MB request, 6 MB response |
| **Payload (async)** | 1 MB |
| **Streamed response** | Up to 200 MB (sync, response streaming) |

### What Happens on Invoke Failure

When a function crashes, times out, or errors during Invoke:

1. Lambda performs a **reset** (behaves like a Shutdown event)
2. Runtime is shut down
3. Shutdown event sent to extensions
4. If the execution environment is reused for the next invocation, Lambda re-initializes the runtime and extensions (**suppressed init**)
5. `/tmp` directory is **NOT cleared** — content persists

---

## 5. Shutdown Phase — Cleanup

The Shutdown phase occurs when Lambda decides to reclaim the execution environment (after a period of no invocations).

```
SHUTDOWN PHASE
│
├── Lambda sends Shutdown event to extensions
│   └── Reason: spindown (idle), timeout, or failure
│
├── Extensions perform cleanup
│   └── Flush buffers, close connections, send final telemetry
│
└── Lambda terminates the execution environment
    └── MicroVM destroyed, all state lost
```

### Shutdown Timeout

| Configuration | Shutdown Timeout |
|---|---|
| **No extensions** | 0 ms (immediate) |
| **Internal extensions only** | 500 ms |
| **One or more external extensions** | 2,000 ms |

If an extension doesn't complete within the timeout, Lambda sends SIGKILL.

### When Shutdown Happens

- Lambda auto-terminates execution environments after a period of idle time [exact duration not documented; varies]
- Lambda **also periodically recycles** environments even for continuously invoked functions (for runtime updates and maintenance)
- You **cannot assume an execution environment will persist** — design for stateless behavior

---

## 6. Warm Starts — Sandbox Reuse

### How Warm Starts Work

After an invocation completes, the execution environment is **frozen** (not destroyed). When the next invocation arrives for the same function version:

```
COLD START (first invocation):
  Init Phase (download code, start runtime, run init code)
  └── Invoke Phase (run handler)
  └── Freeze execution environment

WARM START (subsequent invocation):
  Thaw execution environment
  └── Invoke Phase (run handler — skip Init entirely)
  └── Freeze execution environment
```

### What Persists Across Warm Starts

| Resource | Persists? | Example |
|---|---|---|
| **Global variables** | Yes | SDK clients, DB connections, cached data |
| **`/tmp` directory** | Yes | Downloaded files, compiled templates |
| **Background processes** | Yes (resume on thaw) | Unfinished async operations |
| **Memory state** | Yes | In-memory caches, loaded models |
| **Network connections** | Maybe (may be stale) | DB connections may have timed out |
| **Environment variables** | Yes | Read from process environment |

### Optimization: Use Init Code for Expensive Setup

```python
import boto3

# This runs ONCE during Init (cold start), persists across warm starts
dynamodb = boto3.resource('dynamodb')
table = dynamodb.Table('MyTable')

def lambda_handler(event, context):
    # This runs EVERY invocation
    # Reuses the table client from init — no cold start overhead
    response = table.get_item(Key={'id': event['id']})
    return response['Item']
```

### Warm Start Frequency

- AWS states cold starts are typically **under 1% of invocations** in production
- In development/test environments (infrequent invocations), cold start percentage is higher
- Continuously invoked functions may still see occasional cold starts due to:
  - Scaling up (new execution environments for concurrent requests)
  - Periodic recycling for maintenance
  - Code deployments (new version requires new environments)

---

## 7. Frozen and Thawed States

### The Freeze/Thaw Mechanism

Between invocations, Lambda **freezes** the execution environment:

```
After Invoke completes:
  ┌───────────────────────────────────────┐
  │  Execution Environment (FROZEN)        │
  │                                       │
  │  CPU:     Stopped (no cycles)         │
  │  Memory:  Preserved (not freed)       │
  │  /tmp:    Preserved                   │
  │  Network: Connections may timeout     │
  │  Billing: NOT billed                  │
  │                                       │
  │  Process state snapshot in memory     │
  └───────────────────────────────────────┘

Next Invoke arrives:
  ┌───────────────────────────────────────┐
  │  Execution Environment (THAWED)        │
  │                                       │
  │  CPU:     Running again               │
  │  Memory:  Same state as when frozen   │
  │  /tmp:    Same files as before        │
  │  Network: May need reconnection       │
  │  Billing: Billed from this point      │
  │                                       │
  │  Handler called with new event        │
  └───────────────────────────────────────┘
```

### Implications for Function Design

1. **Connection pooling**: DB connections created during Init persist but may be stale. Validate before use.
2. **Background processes**: If you start a thread or async operation that doesn't complete before the handler returns, it will resume on the next thaw — potentially with stale data.
3. **Random seeds**: Random number generators initialized during Init will produce the same sequence on first invocation. For cryptographic randomness, generate in the handler.
4. **Timestamps**: A timestamp captured during Init will be stale on warm invocations.

---

## 8. Cold Start Deep Dive — What Takes Time?

### Cold Start Breakdown

```
Total Cold Start Time = Platform Time + Init Time

Platform Time (Lambda-managed, not billed):
  ├── Provision microVM (~125ms for Firecracker boot)  [INFERRED]
  ├── Download function code from S3
  │   └── ~50ms for small packages, ~1s+ for large (50MB zip)  [INFERRED]
  ├── Set up networking (ENIs for VPC functions)
  └── Mount layers

Init Time (billed):
  ├── Extension init
  ├── Runtime init
  │   └── Start interpreter/JVM/CLR
  └── Function init (your code outside handler)
      └── Import libraries, open connections, load config
```

### Cold Start by Runtime

| Runtime | Typical Cold Start | Why |
|---|---|---|
| **Python** | 100-500 ms | Lightweight interpreter, fast import |
| **Node.js** | 100-500 ms | V8 engine is fast to start |
| **Go** | 50-200 ms | Compiled binary, no runtime to start |
| **Ruby** | 200-600 ms | Interpreter startup |
| **Java** | 1-10 seconds | JVM startup, class loading, JIT compilation |
| **.NET** | 500ms - 3 seconds | CLR startup, assembly loading |
| **Custom runtime** | Varies | Depends on what you're starting |
| **Container image** | 1-10 seconds | Image pull, extraction, startup |

### Why Java Cold Starts Are Worst

Java's cold start problem is a compound of:
1. **JVM startup**: The JVM itself takes hundreds of milliseconds to initialize
2. **Class loading**: Every class used must be found, loaded, verified, and linked
3. **Framework initialization**: Spring Boot, Quarkus, etc. perform dependency injection, component scanning
4. **JIT compilation**: The JVM starts in interpreted mode; JIT compilation happens later

**This is exactly what SnapStart solves** — by snapshotting after JVM + framework initialization, it eliminates steps 1-3 from cold start.

### Factors That Increase Cold Start

| Factor | Impact | Mitigation |
|---|---|---|
| **Large deployment package** | More time to download | Minimize dependencies; use layers for shared code |
| **Many imports/dependencies** | More init time | Lazy loading; import only what's needed |
| **VPC attachment** | Used to add 10+ seconds (pre-2019) | Now solved with Hyperplane ENIs (~1-2s max) |
| **High memory setting** | Faster init (more CPU allocated) | Set memory to at least 512 MB for Java |
| **Container image** | Larger download; image extraction | Use multi-stage builds; minimize image size |
| **Heavy framework** | Spring Boot: 5-10s init | Use lightweight alternatives (Micronaut, Quarkus) or SnapStart |

---

## 9. SnapStart — Checkpoint/Restore

### How SnapStart Works

```
PUBLISH FUNCTION VERSION (one-time):
  ┌──────────────────────────────────────────┐
  │ 1. Lambda creates execution environment   │
  │ 2. Runs full Init phase:                 │
  │    - Start JVM                           │
  │    - Load classes                        │
  │    - Run static initializers             │
  │    - Framework initialization            │
  │    - Before-checkpoint hooks             │
  │ 3. Take Firecracker microVM snapshot     │
  │    - Memory state captured               │
  │    - Disk state captured                 │
  │ 4. Encrypt and cache snapshot            │
  │    - Multiple copies for resiliency      │
  └──────────────────────────────────────────┘

INVOKE (every cold start):
  ┌──────────────────────────────────────────┐
  │ 1. Restore from cached snapshot          │
  │    - Replay memory and disk state        │
  │    - Skip entire Init phase              │
  │ 2. Run after-restore hooks (≤10 seconds) │
  │    - Re-establish connections            │
  │    - Refresh credentials                 │
  │    - Re-seed random number generators    │
  │ 3. Invoke handler                        │
  └──────────────────────────────────────────┘

Result: Cold start drops from seconds → sub-second
```

### Supported Runtimes

| Runtime | SnapStart Support |
|---|---|
| **Java 11+** | Yes |
| **Python 3.12+** | Yes |
| **.NET 8+** | Yes (requires Amazon.Lambda.Annotations v1.6.0+) |
| **Node.js** | No |
| **Go** | No (already has fast cold starts) |
| **Ruby** | No |
| **Container images** | No |
| **Custom runtimes** | No |

### Uniqueness Concerns

The most critical SnapStart design issue: anything initialized during Init is **shared across all execution environments** restored from the same snapshot.

| Concern | Problem | Solution |
|---|---|---|
| **Random number seeds** | All environments start with same seed → same "random" numbers | Re-seed RNG in after-restore hook or handler |
| **UUIDs generated during init** | Same UUID in all environments | Generate UUIDs in handler, not init |
| **Network connections** | Connections established during init may be stale or shared | Validate and re-establish in after-restore hook |
| **Temporary credentials** | STS tokens from init may expire | Refresh in handler |
| **Timestamps** | `Instant.now()` during init returns init time, not invoke time | Capture time in handler |

### SnapStart Limitations

| Limitation | Details |
|---|---|
| **Provisioned concurrency** | Cannot use SnapStart + provisioned concurrency together |
| **Amazon EFS** | Not supported with SnapStart |
| **Ephemeral storage > 512 MB** | Not supported with SnapStart |
| **$LATEST version** | SnapStart only works on published versions and aliases |
| **Container images** | Not supported |

### SnapStart Pricing

| Runtime | Pricing |
|---|---|
| **Java** | No additional cost (included in standard Lambda pricing) |
| **Python, .NET** | Two components: (1) caching cost per published version (based on memory, min 3-hour charge), (2) restoration cost per snapshot restore (based on memory) |

---

## 10. Provisioned Concurrency — Pre-Warmed Sandboxes

### How It Works

Provisioned concurrency pre-initializes a specified number of execution environments **before invocations arrive**:

```
WITHOUT Provisioned Concurrency:
  Request → Cold Start (Init) → Invoke → Response
  Latency: 500ms-10s (depending on runtime)

WITH Provisioned Concurrency (e.g., 10):
  10 environments pre-initialized and waiting
  Request → Invoke (no Init) → Response
  Latency: single-digit ms (warm invocation)
```

### Key Properties

| Property | Value |
|---|---|
| **Allocation speed** | Up to 6,000 environments per minute per function |
| **Startup time** | 1-2 minutes for environments to come online |
| **Availability** | None accessible until all requested environments are ready |
| **Cost** | Additional charge (pay for provisioned environments even when idle) |
| **Counts toward** | Account concurrency limit |
| **Cannot combine with** | SnapStart |

### Reserved vs Provisioned Concurrency

| Dimension | Reserved Concurrency | Provisioned Concurrency |
|---|---|---|
| **What it does** | Sets max concurrency for the function (hard cap) | Pre-initializes environments (warm pool) |
| **Cold starts?** | Yes (environments created on-demand) | No (environments pre-initialized) |
| **Cost** | Free | Additional charge |
| **Guarantees** | Function won't exceed limit; other functions can't steal capacity | Function has warm environments ready |
| **Use case** | Prevent runaway scaling, isolate function capacity | Eliminate cold starts for latency-sensitive functions |

### Combined Configuration

```
Reserved concurrency: 400
Provisioned concurrency: 200

Result:
  - 200 pre-warmed environments (no cold start)
  - Can scale to 400 total (200 additional on-demand, with cold starts)
  - Cannot exceed 400 concurrent (hard cap)
  - Other functions cannot use these 400 slots
```

---

## 11. Concurrency and Scaling Model

### Concurrency Formula

```
Concurrency = Requests Per Second × Average Duration (seconds)

Example:
  100 requests/second × 0.5 second duration = 50 concurrent executions
```

### Account-Level Quotas

| Quota | Default | Adjustable? |
|---|---|---|
| **Concurrent executions (per region)** | 1,000 | Yes (to tens of thousands) |
| **Unreserved concurrency (always available)** | 100 (reserved from total) | No |
| **Maximum reservable** | Total - 100 (e.g., 900 out of 1,000) | Depends on total quota |
| **RPS limit** | 10× concurrency quota | Yes (with concurrency increase) |

### Scaling Rate

| Metric | Value |
|---|---|
| **Standard scaling rate** | 1,000 environments every 10 seconds per function |
| **On-demand burst** | 500 concurrency every 10 seconds per function |
| **RPS burst** | 5,000 RPS every 10 seconds per function |
| **Provisioned concurrency allocation** | 6,000 environments per minute per function |

### How Lambda Decides: New Environment vs Reuse

```
Invocation arrives for function F
│
├── Is there a FROZEN (idle) execution environment for F?
│   ├── Yes → THAW it → Warm start (no Init)
│   └── No → Is concurrency limit reached?
│       ├── Yes → THROTTLE (429 error)
│       └── No → CREATE new execution environment → Cold start (Init + Invoke)
```

### Throttling Behavior

| Invocation Type | Throttle Response |
|---|---|
| **Synchronous** | 429 error (TooManyRequestsException) |
| **Asynchronous** | Event queued for retry (up to 6 hours) |
| **Event source mapping** | Depends on source (SQS: returns messages to queue) |

---

## 12. Extensions Lifecycle

### Types

| Type | Runs As | Lifecycle | Use Case |
|---|---|---|---|
| **External** | Independent process | Init → Invoke → Shutdown (survives function invocation) | Monitoring agents, security scanners |
| **Internal** | Part of runtime process | Same as runtime | Wrapper scripts, in-process middleware |

### Extension Lifecycle

```
INIT PHASE
├── Extension Init (parallel with other extensions)
│   └── Extensions register via Extensions API
│   └── Extensions signal readiness via Next API
│
INVOKE PHASE
├── Extensions receive Invoke event
│   └── Can process telemetry, collect logs
│   └── Function and extensions share the timeout
│
SHUTDOWN PHASE
├── Extensions receive Shutdown event
│   └── Flush buffers, send final telemetry
│   └── Timeout: 2000 ms for external extensions
```

### Resource Sharing

Extensions share resources with the function:
- **CPU**: Same allocation (proportional to memory setting)
- **Memory**: Counted against function's memory limit
- **Storage**: Same `/tmp` directory
- **Network**: Same network context

**Impact**: Compute-intensive extensions increase function execution duration. Extension init time adds to cold start latency.

### Packaging

Extensions are packaged as **Lambda layers** and deployed to `/opt/extensions/`. Each file in that directory is treated as an extension executable.

---

## 13. /tmp Storage and State Persistence

### Configuration

| Property | Value |
|---|---|
| **Size** | 512 MB to 10,240 MB (in 1-MB increments) |
| **Default** | 512 MB |
| **Persistence** | Persists across invocations within same execution environment |
| **Cleared on** | Execution environment destruction (Shutdown) |
| **NOT cleared on** | Invoke failures, suppressed inits |
| **Cost** | Free for first 512 MB; additional charge for > 512 MB |

### Use Cases

1. **Download and cache reference data**: ML models, configuration files
2. **Compile templates**: Pre-compile templates on first invocation, reuse on warm starts
3. **Temporary file processing**: Unzip archives, process images
4. **Database query caching**: Cache frequent query results

### Best Practice: Check Before Downloading

```python
import os

def lambda_handler(event, context):
    model_path = '/tmp/model.pkl'

    # Check if model already cached from previous invocation
    if not os.path.exists(model_path):
        # Cold start or first time: download from S3
        s3.download_file('bucket', 'model.pkl', model_path)

    # Use cached model
    model = load_model(model_path)
    return model.predict(event['data'])
```

---

## 14. Suppressed Init — Re-Initialization After Failure

### What It Is

When a function crashes during Invoke, Lambda resets the execution environment. If the environment is reused for the next invocation, Lambda re-runs the Init phase — but this re-initialization is **not explicitly logged** as an INIT_REPORT in CloudWatch.

```
Invocation 1: Handler crashes (OOM, uncaught exception)
  └── Lambda resets environment (shutdown event to extensions)

Invocation 2: Same environment reused
  └── Suppressed Init (runs Init again, but not logged separately)
  └── REPORT line includes both Init + Invoke duration
  └── Telemetry API reveals suppressed init events
```

### Detecting Suppressed Inits

| Method | How |
|---|---|
| **REPORT log duration** | Duration in REPORT line is longer than expected (includes Init + Invoke) |
| **Telemetry API** | `INIT_START`, `INIT_RUNTIME_DONE`, `INIT_REPORT` events with `phase=invoke` |
| **CloudWatch (2024+)** | New format includes `Status: error` or `Status: timeout` in REPORT |

---

## 15. Lambda Quotas Reference

### Compute

| Resource | Quota |
|---|---|
| **Memory** | 128 MB to 10,240 MB (1-MB increments) |
| **vCPU equivalence** | 1 vCPU at 1,769 MB |
| **Timeout** | Up to 900 seconds (15 minutes) |
| **Concurrent executions** | 1,000 per region (default, adjustable) |
| **Scaling rate** | 1,000 environments / 10 seconds per function |
| **/tmp storage** | 512 MB to 10,240 MB |
| **File descriptors** | 1,024 |
| **Processes/threads** | 1,024 |

### Deployment

| Resource | Quota |
|---|---|
| **Deployment package (.zip)** | 50 MB (zipped), 250 MB (unzipped including layers) |
| **Container image** | 10 GB (uncompressed) |
| **Layers per function** | 5 |
| **Code storage** | 75 GB per region (adjustable) |
| **Environment variables** | 4 KB aggregate |

### Invocation

| Resource | Quota |
|---|---|
| **Synchronous payload** | 6 MB (request and response) |
| **Asynchronous payload** | 1 MB |
| **Streamed response** | 200 MB |
| **RPS per function** | 10× concurrency quota |

---

## 16. Design Decision Analysis

### Decision 1: One Invocation Per Execution Environment

| Alternative | Pros | Cons |
|---|---|---|
| **Multiple concurrent invocations per environment** | Better resource utilization; fewer cold starts | Complex thread safety; memory contention; harder isolation |
| **One invocation at a time** ← Lambda's choice | Simple programming model; no thread safety concerns; predictable resource usage | More environments needed; more cold starts under high concurrency |

**Why one at a time**: Simplicity is the product. Lambda's value proposition is "write a function, don't think about infrastructure." Concurrent invocations would require developers to handle thread safety, shared state, and race conditions — exactly the complexity Lambda eliminates.

### Decision 2: Freeze/Thaw Instead of Keeping Environments Hot

| Alternative | Pros | Cons |
|---|---|---|
| **Keep running continuously** | No thaw latency; background processes keep running | Billing complexity; resource waste; contradicts pay-per-use model |
| **Destroy after each invocation** | Clean state every time; no stale data issues | Cold start on EVERY invocation; terrible performance |
| **Freeze/Thaw** ← Lambda's choice | Warm starts (fast); no billing while frozen; preserves init state | Stale connections; frozen background processes; uniqueness issues |

**Why freeze/thaw**: It's the optimal balance. Customers don't pay while frozen (enabling the pay-per-use model), but they get warm-start performance when the environment is thawed. The trade-off is handling stale state, which is manageable.

### Decision 3: Why SnapStart Instead of Just Faster Runtimes?

| Alternative | Pros | Cons |
|---|---|---|
| **Optimize JVM startup** | Works within existing framework | Diminishing returns; JVM is already heavily optimized |
| **Use GraalVM native compilation** | Near-zero startup | Limited library compatibility; no dynamic class loading; different runtime behavior |
| **SnapStart (checkpoint/restore)** ← Lambda's choice | Captures the RESULT of initialization; works with any Java code; no code changes needed | Uniqueness concerns; limited to published versions; can't combine with provisioned concurrency |

**Why SnapStart**: Instead of making initialization faster, SnapStart **skips initialization entirely** by restoring from a snapshot. The insight is: most of cold start time is spent doing the same work every time (load JVM, load classes, initialize framework). By doing it once and snapshotting, every subsequent "cold start" is just a memory restore.

---

## 17. Interview Angles

### Questions an Interviewer Might Ask

**Lifecycle Fundamentals:**
- "Walk me through what happens when a Lambda function is invoked for the first time."
  - Answer: Cold start. Lambda provisions a Firecracker microVM, downloads function code from S3, starts the runtime (e.g., Python interpreter), runs function init code (imports, DB connections), then runs the handler. After the handler returns, the environment is frozen for reuse. Subsequent invocations thaw the frozen environment and skip Init entirely.

- "What persists between Lambda invocations?"
  - Answer: Global variables, `/tmp` directory contents, in-memory state, and partially completed background processes — all within the same execution environment. Network connections persist but may be stale. Nothing persists across different execution environments.

**Cold Start Optimization:**
- "How would you reduce cold start latency for a Java Lambda function?"
  - Answer: Three approaches in order of preference: (1) SnapStart — checkpoint/restore eliminates Init phase; free for Java. (2) Provisioned concurrency — pre-warms environments; costly but guarantees warm starts. (3) Code optimization — minimize dependencies, use lightweight frameworks (Micronaut over Spring Boot), increase memory (more CPU for faster init).

- "What are the uniqueness concerns with SnapStart?"
  - Answer: All environments restored from the same snapshot share identical Init state. Random seeds, UUIDs, timestamps, and network connections from Init are cloned. Solution: generate unique values in the handler or after-restore hooks, not during Init. This is similar to VM image cloning issues.

**Scaling:**
- "How does Lambda handle a sudden spike from 0 to 10,000 concurrent requests?"
  - Answer: Lambda scales at 1,000 new environments per 10 seconds. So reaching 10,000 would take about 100 seconds with gradual scaling. For immediate capacity, use provisioned concurrency. The scaling rate is per-function, per-region.

**Extensions:**
- "How do Lambda extensions affect cold start and execution?"
  - Answer: Extensions share the function's resources (memory, CPU). External extensions add to Init time (extension init sub-phase) and can add up to 2 seconds of Shutdown time. The `PostRuntimeExtensionsDuration` metric measures extra time after function execution.

### Red Flags in Candidate Answers

| Red Flag | Why It's Wrong |
|---|---|
| "Lambda environments run multiple invocations concurrently" | One invocation at a time per execution environment |
| "Cold starts happen 50% of the time" | Under 1% in production; frequent only in dev/test |
| "Use provisioned concurrency with SnapStart" | Cannot combine them — mutually exclusive |
| "The /tmp directory is cleared between invocations" | /tmp persists across invocations in the same environment |
| "Set memory to 128 MB to save cost" | Lower memory = less CPU = slower Init = longer cold starts; often more expensive per GB-second |
| "Lambda environments last forever" | Lambda periodically recycles environments for maintenance |
