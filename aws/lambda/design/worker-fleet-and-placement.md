# Lambda Worker Fleet and Placement — Deep Dive

> **Companion doc for:** [interview-simulation.md](interview-simulation.md) — Phase 5+ (Worker Fleet Management)
> **Last verified:** February 2026 against AWS Lambda documentation and Firecracker paper
> **Note:** Lambda's internal architecture is partially documented via re:Invent talks and the Firecracker NSDI paper. Sections marked [INFERRED] are architectural reasoning, not official documentation.

---

## Table of Contents

1. [Overview](#1-overview)
2. [Lambda Service Architecture — Control vs Data Plane](#2-lambda-service-architecture--control-vs-data-plane)
3. [Worker Hosts](#3-worker-hosts)
4. [Sandbox Lifecycle on a Worker](#4-sandbox-lifecycle-on-a-worker)
5. [Code Storage and Loading](#5-code-storage-and-loading)
6. [Placement — Routing Invocations to Sandboxes](#6-placement--routing-invocations-to-sandboxes)
7. [Sandbox Pooling and Caching](#7-sandbox-pooling-and-caching)
8. [Fleet Scaling — How Lambda Grows and Shrinks](#8-fleet-scaling--how-lambda-grows-and-shrinks)
9. [Eviction — When Sandboxes Are Destroyed](#9-eviction--when-sandboxes-are-destroyed)
10. [Multi-Tenancy and Isolation on Workers](#10-multi-tenancy-and-isolation-on-workers)
11. [Lambda Layers — Shared Code Mounting](#11-lambda-layers--shared-code-mounting)
12. [Provisioned Concurrency — Fleet Pre-Warming](#12-provisioned-concurrency--fleet-pre-warming)
13. [Availability and Fault Tolerance](#13-availability-and-fault-tolerance)
14. [Design Decision Analysis](#14-design-decision-analysis)
15. [Interview Angles](#15-interview-angles)

---

## 1. Overview

Lambda's worker fleet is the physical infrastructure that runs customer code. Understanding how Lambda manages this fleet is essential for explaining cold start behavior, scaling dynamics, and multi-tenant isolation.

### The Key Architecture Insight

```
Traditional server:  Customer provisions servers → runs code on their servers
Lambda:              AWS provisions a fleet of workers → Lambda places customer code on shared workers

The customer sees: "my function runs when invoked"
Behind the scenes: a fleet management system decides WHERE to run each invocation,
                   HOW to cache sandboxes for warm starts, and WHEN to evict idle sandboxes.
```

---

## 2. Lambda Service Architecture — Control vs Data Plane

Lambda's architecture separates the **control plane** (function management) from the **data plane** (invocation execution):

```
┌───────────────────────────────────────────────────────────┐
│                    CONTROL PLANE                           │
│                                                           │
│  ┌────────────┐  ┌──────────────┐  ┌───────────────────┐ │
│  │ API Gateway │  │ Function     │  │ Version/Alias     │ │
│  │ (CRUD ops)  │  │ Registry     │  │ Management        │ │
│  └────────────┘  └──────────────┘  └───────────────────┘ │
│                                                           │
│  CreateFunction, UpdateFunctionCode, PublishVersion, etc. │
└───────────────────────────────────────────────────────────┘

┌───────────────────────────────────────────────────────────┐
│                     DATA PLANE                             │
│                                                           │
│  ┌──────────────┐    ┌──────────────┐                     │
│  │ Frontend      │    │ Counting     │                     │
│  │ (Load Balancer│    │ Service      │                     │
│  │  + Router)    │    │ (Concurrency │                     │
│  └──────┬───────┘    │  Tracking)   │                     │
│         │            └──────────────┘                     │
│         │                                                  │
│         ▼                                                  │
│  ┌──────────────┐    ┌──────────────┐                     │
│  │ Placement     │    │ Sandbox      │                     │
│  │ Service       │    │ Manager      │                     │
│  │ (Find/create  │    │ (Per-worker  │                     │
│  │  sandbox)     │    │  lifecycle)  │                     │
│  └──────┬───────┘    └──────────────┘                     │
│         │                                                  │
│         ▼                                                  │
│  ┌─────────────────────────────────────────────┐          │
│  │            WORKER FLEET                      │         │
│  │                                              │         │
│  │  ┌─────────┐  ┌─────────┐  ┌─────────┐     │         │
│  │  │Worker 1 │  │Worker 2 │  │Worker N │     │         │
│  │  │         │  │         │  │         │     │         │
│  │  │ MicroVM │  │ MicroVM │  │ MicroVM │     │         │
│  │  │ MicroVM │  │ MicroVM │  │ MicroVM │     │         │
│  │  │ MicroVM │  │ MicroVM │  │ MicroVM │     │         │
│  │  └─────────┘  └─────────┘  └─────────┘     │         │
│  └─────────────────────────────────────────────┘          │
└───────────────────────────────────────────────────────────┘
```
[INFERRED — based on re:Invent talks and Firecracker paper. Exact service names may differ.]

### Component Responsibilities

| Component | Role |
|---|---|
| **Frontend / Load Balancer** | Receives invocation requests, authenticates, routes to placement service |
| **Counting Service** | Tracks concurrent executions per function; enforces concurrency limits |
| **Placement Service** | Finds an existing sandbox or creates a new one; decides which worker to use |
| **Sandbox Manager** | Per-worker daemon that manages Firecracker microVMs on that host |
| **Worker Host** | Physical/virtual server running multiple Firecracker microVMs for multiple tenants |

---

## 3. Worker Hosts

### What a Worker Host Looks Like [INFERRED]

```
WORKER HOST (bare-metal EC2 instance)
┌──────────────────────────────────────────────────────────┐
│  Host OS (Amazon Linux, minimal)                          │
│                                                          │
│  ┌────────────────────────────────────────────────────┐  │
│  │  Sandbox Manager (Lambda agent)                    │  │
│  │  • Manages Firecracker microVM lifecycle           │  │
│  │  • Downloads function code                         │  │
│  │  • Reports capacity to placement service           │  │
│  │  • Monitors sandbox health                         │  │
│  └────────────────────────────────────────────────────┘  │
│                                                          │
│  ┌──────────┐  ┌──────────┐  ┌──────────┐  ┌────────┐  │
│  │ MicroVM  │  │ MicroVM  │  │ MicroVM  │  │MicroVM │  │
│  │(Tenant A)│  │(Tenant B)│  │(Tenant A)│  │(Tent C)│  │
│  │Func: foo │  │Func: bar │  │Func: baz │  │Func: q │  │
│  │128MB     │  │1024MB    │  │512MB     │  │256MB   │  │
│  │ Python   │  │ Java     │  │ Node.js  │  │ Go     │  │
│  └──────────┘  └──────────┘  └──────────┘  └────────┘  │
│                                                          │
│  Firecracker VMM process per microVM                     │
│  Total memory: host RAM - OS overhead                    │
│  MicroVM density: depends on function memory configs     │
└──────────────────────────────────────────────────────────┘
```

### Worker Host Properties [INFERRED]

| Property | Estimated Value |
|---|---|
| **Hardware** | Bare-metal EC2 instances (Nitro system) |
| **MicroVM creation rate** | 150+ per second per host |
| **MicroVM boot time** | ~125ms (Firecracker boot) |
| **MicroVM memory overhead** | ~5 MB per microVM (Firecracker VMM process) |
| **Density** | Hundreds to thousands of microVMs per host (depending on function memory config) |
| **Multi-tenancy** | Multiple customers on same host, isolated by Firecracker microVMs |

### Density Example

```
Worker host with 384 GB RAM (a common bare-metal instance):
  OS + Sandbox Manager: ~2 GB
  Available for microVMs: ~382 GB

  If all functions use 128 MB: 382 GB / 128 MB ≈ 2,984 microVMs
  If all functions use 1 GB:   382 GB / 1 GB   ≈ 382 microVMs
  If all functions use 10 GB:  382 GB / 10 GB  ≈ 38 microVMs

  Real-world mix: 500-2000 microVMs per host  [INFERRED]
```

---

## 4. Sandbox Lifecycle on a Worker

### States

```
                   Code download
                   + MicroVM boot
    ┌──────────┐   + Init phase      ┌──────────┐
    │ CREATING  │──────────────────▶ │  READY    │
    └──────────┘                     └────┬─────┘
                                          │
                                     Invoke
                                          │
                                     ┌────▼─────┐
                                     │  BUSY     │
                                     │ (running  │
                                     │  handler) │
                                     └────┬─────┘
                                          │
                                     Handler returns
                                          │
                                     ┌────▼─────┐
                                     │  FROZEN   │◄──── Thawed on
                                     │ (idle,    │      next invoke
                                     │  cached)  │
                                     └────┬─────┘
                                          │
                                     Idle too long / evicted
                                          │
                                     ┌────▼─────┐
                                     │ DESTROYED │
                                     └──────────┘
```

### State Transitions

| From | To | Trigger |
|---|---|---|
| (none) | CREATING | New invocation, no available sandbox |
| CREATING | READY | Code downloaded, microVM booted, Init complete |
| READY | BUSY | Invocation assigned to this sandbox |
| BUSY | FROZEN | Handler returns (or times out) |
| FROZEN | BUSY | New invocation arrives, sandbox thawed |
| FROZEN | DESTROYED | Idle timeout, eviction, or maintenance recycling |
| BUSY | DESTROYED | Fatal error, OOM kill |

---

## 5. Code Storage and Loading

### How Function Code Gets to the Worker [INFERRED]

```
Developer deploys code
        │
        ▼
┌──────────────────┐
│ Lambda Control    │
│ Plane             │
│                  │
│ Stores code in   │
│ internal S3 →    │──────────┐
│ (encrypted,      │          │
│  optimized)      │          │
└──────────────────┘          │
                              │
        When sandbox is needed:
                              │
                              ▼
┌──────────────────────────────────────┐
│ Worker Host                          │
│                                      │
│ 1. Download code from internal S3    │
│ 2. Extract/mount into microVM        │
│ 3. Cache on local disk for reuse     │
│                                      │
│ Optimization: code-affinity routing  │
│ → route invocations to workers that  │
│   already have the code cached       │
└──────────────────────────────────────┘
```

### Code-Affinity Routing [INFERRED]

To minimize code download latency on cold starts, the placement service preferentially routes invocations to workers that already have the function's code cached on local disk:

```
Invocation for function F arrives
│
├── Placement checks: which workers have F's code cached?
│   ├── Worker 7 has F cached, has a frozen sandbox → WARM START (best case)
│   ├── Worker 12 has F cached, no sandbox but code on disk → Faster cold start
│   └── Worker 23 has no cache → Full cold start (download + boot + init)
│
└── Choose worker with best combination of:
    ├── Cached sandbox (prefer warm start)
    ├── Cached code (avoid download)
    └── Available capacity (enough memory for new microVM)
```

### Deployment Package Limits

| Package Type | Max Size | Loading |
|---|---|---|
| **.zip (zipped)** | 50 MB | Extracted to microVM filesystem |
| **.zip (unzipped)** | 250 MB (including layers) | Mounted in /var/task |
| **Container image** | 10 GB (uncompressed) | Pulled from ECR, cached on host |

### Container Image Optimization [INFERRED]

For container image functions (up to 10 GB):
- Lambda caches image layers on worker hosts
- Common base layers (e.g., `python:3.12-slim`) may be pre-cached across the fleet
- First cold start pulls the image (slower); subsequent cold starts on the same host use cached layers
- Lazy loading: Lambda may not pull the entire image upfront; it can load blocks on demand

---

## 6. Placement — Routing Invocations to Sandboxes

### The Placement Decision [INFERRED]

Every invocation triggers a placement decision:

```
Invocation arrives for function F, version V
│
├── Step 1: Check concurrency
│   └── Counting service: is function F under its concurrency limit?
│       ├── Over limit → THROTTLE (429)
│       └── Under limit → proceed
│
├── Step 2: Find existing sandbox
│   └── Placement service: is there a FROZEN sandbox for F:V?
│       ├── Yes → Route to that sandbox (WARM START)
│       └── No → proceed to Step 3
│
├── Step 3: Find worker with cached code
│   └── Placement service: which workers have F:V's code on disk?
│       ├── Found → Create new sandbox on that worker (COLD START, no download)
│       └── Not found → proceed to Step 4
│
└── Step 4: Find any available worker
    └── Placement service: which workers have enough free memory?
        ├── Found → Download code + create sandbox (FULL COLD START)
        └── Not found → Scaling needed (provision more workers)
```

### Placement Considerations [INFERRED]

| Factor | Goal |
|---|---|
| **Warm sandbox availability** | Minimize cold starts → route to frozen sandboxes first |
| **Code locality** | Minimize download time → prefer workers with cached code |
| **Memory capacity** | Don't over-commit → track per-worker memory utilization |
| **Multi-tenancy spread** | Security → don't put the same customer's functions on too few hosts |
| **AZ distribution** | Availability → spread across availability zones |
| **Worker health** | Reliability → avoid unhealthy or overloaded workers |

---

## 7. Sandbox Pooling and Caching

### How Lambda Keeps Sandboxes Warm

After an invocation completes, the sandbox is frozen, not destroyed. Lambda caches frozen sandboxes for potential reuse:

```
Sandbox Cache (per worker, per function)
┌────────────────────────────────────────────┐
│ Function: arn:aws:lambda:...:my-function:5 │
│                                            │
│ Sandbox #1: FROZEN (last used 30s ago)     │
│ Sandbox #2: FROZEN (last used 2 min ago)   │
│ Sandbox #3: BUSY (currently processing)    │
│                                            │
│ Next invocation → Thaw Sandbox #1          │
└────────────────────────────────────────────┘
```

### Cache Eviction Triggers

| Trigger | What Happens |
|---|---|
| **Idle timeout** | Sandbox not invoked for some period → destroyed. Exact timeout not documented; varies. |
| **Memory pressure** | Worker needs memory for new sandboxes → evict LRU (least recently used) frozen sandboxes |
| **Maintenance** | Lambda periodically recycles environments for runtime updates, even for continuously invoked functions |
| **Code deployment** | New function version deployed → old version sandboxes become candidates for eviction |
| **Configuration change** | Memory, timeout, env vars changed → existing sandboxes invalidated |

### Why Lambda Can't Keep All Sandboxes Forever

```
A region with 1 million functions, each with 1 sandbox at 128 MB:
  = 1M × 128 MB = 128 TB of frozen sandbox memory

Keeping all sandboxes warm forever would require 128 TB of RAM just for frozen state.
That's why Lambda must evict — it's a cache, not a database.
```

---

## 8. Fleet Scaling — How Lambda Grows and Shrinks

### Per-Function Scaling

Lambda scales at the function level, not the fleet level:

```
Function Scaling Rate:
  Standard:  1,000 new execution environments every 10 seconds
  Burst:     500 new concurrent invocations every 10 seconds
  RPS burst: 5,000 RPS every 10 seconds

Example: Function goes from 0 to 5,000 concurrent invocations
  t=0:    0 concurrent
  t=10s:  1,000 concurrent (1,000 new environments)
  t=20s:  2,000 concurrent (1,000 more)
  t=30s:  3,000 concurrent
  t=40s:  4,000 concurrent
  t=50s:  5,000 concurrent
  Total: ~50 seconds to reach 5,000 concurrency
```

### Fleet-Level Scaling [INFERRED]

The Lambda fleet must handle aggregate demand across all customers in a region:

```
Region-level scaling:
  Sum of all customers' concurrent invocations → total worker fleet demand

  Lambda maintains excess capacity to handle:
  1. Predictable daily patterns (business hours peak)
  2. Unpredictable spikes (viral events, flash sales)
  3. Per-function burst scaling (1,000 envs / 10s)

  Fleet provisioning is a background process:
  - Monitor aggregate utilization
  - Pre-provision workers based on historical patterns + safety margin
  - Decommission under-utilized workers during off-peak
```

### Account-Level Concurrency Limits

| Quota | Default | Purpose |
|---|---|---|
| **Concurrent executions** | 1,000 per region | Protect the fleet from any single account's spike |
| **Unreserved concurrency** | 100 (always reserved from total) | Ensure all functions can get at least some capacity |
| **Reserved concurrency** | Up to total - 100 | Guarantee capacity for critical functions |

---

## 9. Eviction — When Sandboxes Are Destroyed

### Eviction Priority [INFERRED]

When a worker needs to free memory for new sandboxes:

```
Eviction priority (destroy first):
  1. Frozen sandboxes for functions with no recent invocations (LRU)
  2. Frozen sandboxes for functions with many cached sandboxes (trim excess)
  3. Frozen sandboxes for low-priority functions (no reserved/provisioned concurrency)
  4. Never evict: Provisioned concurrency sandboxes (maintained at configured count)
```

### What Eviction Means for Customers

| Scenario | Customer Impact |
|---|---|
| **Sandbox evicted, next invocation arrives** | Cold start — full Init phase |
| **Code evicted from disk cache** | Longer cold start — code download + Init |
| **Worker decommissioned** | All sandboxes on that worker destroyed; placement routes to other workers |

### Maintenance Recycling

Lambda periodically destroys and recreates execution environments even for continuously invoked functions:

- **Purpose**: Apply runtime updates, security patches, and infrastructure changes
- **Timing**: Not documented; happens transparently
- **Impact**: Occasional cold starts even for functions with constant traffic
- **Customer should**: Never assume an environment will persist; design for stateless behavior

---

## 10. Multi-Tenancy and Isolation on Workers

### Isolation Model

```
Worker Host
┌───────────────────────────────────────────────────────────┐
│  Host OS (minimal, hardened)                               │
│                                                           │
│  ┌─────────────────┐  ┌─────────────────┐                │
│  │ Firecracker      │  │ Firecracker      │               │
│  │ MicroVM          │  │ MicroVM          │               │
│  │ (Customer A)     │  │ (Customer B)     │               │
│  │                  │  │                  │               │
│  │ Guest kernel     │  │ Guest kernel     │               │
│  │ Runtime          │  │ Runtime          │               │
│  │ Function code    │  │ Function code    │               │
│  │                  │  │                  │               │
│  │ ────────────── │  │ ────────────── │               │
│  │ Firecracker VMM  │  │ Firecracker VMM  │               │
│  │ (jailer sandbox) │  │ (jailer sandbox) │               │
│  └─────────────────┘  └─────────────────┘                │
│                                                           │
│  Isolation boundaries:                                    │
│  1. Firecracker microVM (VM-level isolation)              │
│  2. Jailer (chroot + cgroups + seccomp)                   │
│  3. Separate guest kernels (no shared kernel)             │
│  4. No shared filesystem between microVMs                 │
└───────────────────────────────────────────────────────────┘
```

### Defense-in-Depth

| Layer | Mechanism | Protects Against |
|---|---|---|
| **1. Firecracker microVM** | Hardware virtualization (KVM) | Guest-to-host escape via kernel exploit |
| **2. Jailer** | chroot + cgroups + seccomp-bpf | VMM process escape; limits VMM syscalls to ~25 |
| **3. Guest kernel** | Each microVM has its own kernel | Kernel exploits confined to guest |
| **4. Network isolation** | Separate tap devices per microVM | Network-level cross-tenant access |
| **5. Resource limits** | cgroups on VMM process | Resource exhaustion DoS |

### Same-Customer Colocation [INFERRED]

For the same customer, different functions may run on the same worker:
- Still isolated by Firecracker microVMs
- But share the same worker host hardware
- Code-affinity routing may increase colocation of the same customer's functions

---

## 11. Lambda Layers — Shared Code Mounting

### What Layers Are

Layers are archives (.zip) containing shared libraries, custom runtimes, or other dependencies that are extracted to `/opt` in the execution environment.

### Key Properties

| Property | Value |
|---|---|
| **Max layers per function** | 5 |
| **Layer + function total** | 250 MB unzipped (total across function code + all layers) |
| **Mount location** | `/opt/` directory in the execution environment |
| **Ordering** | Layers are extracted in the order listed; later layers can overwrite earlier ones |
| **Versioning** | Each layer version is immutable; new content requires a new version |
| **Sharing** | Layers can be shared across functions and even across AWS accounts |

### How Layers Work in the Execution Environment

```
Function deployment:
  Function code (.zip) → extracted to /var/task/
  Layer 1 (.zip)       → extracted to /opt/
  Layer 2 (.zip)       → extracted to /opt/ (overlaid on Layer 1)

Execution environment filesystem:
  /var/task/         ← Function code
  /opt/              ← Layers (merged)
  /opt/python/       ← Python libraries from layers
  /opt/extensions/   ← Lambda extensions from layers
  /tmp/              ← Ephemeral storage (512 MB - 10 GB)
```

### Layer Use Cases

| Use Case | Example |
|---|---|
| **Shared libraries** | NumPy, Pandas shared across multiple Python functions |
| **Custom runtimes** | Rust, C++, or other non-managed runtimes |
| **Extensions** | Monitoring agents (Datadog, New Relic) packaged as layers |
| **Common utilities** | Logging frameworks, error handling libraries |
| **Large dependencies** | ML model inference libraries (TensorFlow Lite) |

### Layer Caching on Workers [INFERRED]

Layers are likely cached on worker hosts similarly to function code:
- Popular layers (e.g., AWS SDK, common libraries) may be pre-cached across the fleet
- This reduces cold start time when multiple functions share the same layers
- Layer extraction happens during the Init phase

---

## 12. Provisioned Concurrency — Fleet Pre-Warming

### How Provisioned Concurrency Interacts with the Fleet

```
Provisioned Concurrency = 100 for function F

Lambda fleet behavior:
  1. Allocate 100 execution environments for F across multiple workers
  2. Run Init phase on each environment (can take 1-2 minutes total)
  3. Keep all 100 environments in READY state (not FROZEN — they skip freeze/thaw)
  4. When invocation arrives → route to a READY environment (warm start guaranteed)
  5. When environment crashes → Lambda automatically replaces it to maintain 100
  6. Rate: Lambda provisions up to 6,000 environments per minute
```

### Provisioned vs On-Demand Environments

| Dimension | On-Demand | Provisioned |
|---|---|---|
| **Creation** | On first invocation (cold start) | Pre-created before invocations |
| **State when idle** | FROZEN (can be evicted) | READY (always maintained) |
| **Eviction** | Yes (LRU eviction under memory pressure) | No (Lambda maintains count) |
| **Cost** | Pay only when invoked | Pay for provisioned time + invocation |
| **Cold start** | Yes (Init on first invocation) | No (Init already completed) |

---

## 13. Availability and Fault Tolerance

### Worker Failure Handling [INFERRED]

```
Worker fails (hardware, kernel panic, etc.)
        │
        ▼
All microVMs on that worker are lost
        │
        ▼
Placement service detects worker loss
(sandbox registry entries become stale)
        │
        ▼
Next invocations for affected functions:
├── Route to other workers with frozen sandboxes → Warm start
├── Route to other workers with cached code → Cold start (fast)
└── Route to any worker → Cold start (full)
```

### Multi-AZ Distribution [INFERRED]

Lambda distributes execution environments across multiple Availability Zones:
- If one AZ goes down, functions can still be invoked in other AZs
- Placement service maintains awareness of AZ topology
- VPC-attached functions may be constrained to specific AZs (where their subnets are)

### Control Plane vs Data Plane Isolation

| Failure | Impact |
|---|---|
| **Control plane down** | Cannot create/update functions; existing functions continue to be invoked normally |
| **Data plane (single worker) down** | Functions on that worker fail; placement routes to other workers |
| **Data plane (AZ) down** | Functions shift to other AZs; may see temporary cold starts |
| **Counting service down** | Concurrency limits may not be enforced; risk of over-scaling [INFERRED] |

---

## 14. Design Decision Analysis

### Decision 1: Bare-Metal Workers vs Virtual Workers

| Alternative | Pros | Cons |
|---|---|---|
| **Bare-metal EC2 instances** ← Lambda's choice | Direct hardware access for Firecracker; maximum performance; no nested virtualization | Longer provisioning time; less elastic fleet |
| **Virtual EC2 instances** | Faster to provision; more elastic | Nested virtualization overhead; Firecracker may not work efficiently |
| **Container-based workers** | Flexible; easy to manage | Can't run Firecracker microVMs inside containers |

**Why bare-metal**: Firecracker uses KVM for hardware virtualization. Running Firecracker on bare-metal instances gives direct access to the hardware virtualization extensions (VT-x), maximizing performance and avoiding nested virtualization overhead. The Nitro system provides the bare-metal foundation.

### Decision 2: Code-Affinity Routing [INFERRED]

| Alternative | Pros | Cons |
|---|---|---|
| **Random worker selection** | Simple; even distribution | Every cold start requires code download; slow |
| **Code-affinity routing** ← Lambda's approach | Reduces code download frequency; faster cold starts | Routing complexity; potential hotspots on popular workers |
| **Pre-distribute all code to all workers** | No download on cold start | Massive storage overhead; most code would never be used |

**Why code-affinity**: With millions of functions but bounded worker fleet, pre-distributing all code is impractical. Code-affinity routing caches code on workers where it's been used, reducing download latency for subsequent cold starts while only caching code that's actually invoked.

### Decision 3: Sandbox Eviction (Cache, Not Permanent)

| Alternative | Pros | Cons |
|---|---|---|
| **Keep all sandboxes forever** | No cold starts ever | Requires unbounded memory; prohibitively expensive |
| **Destroy after every invocation** | Simple; clean state | Cold start on every invocation; terrible latency |
| **Cache with LRU eviction** ← Lambda's approach | Warm starts for active functions; bounded memory | Cold starts when sandbox evicted; unpredictable eviction timing |

**Why caching**: The sandbox cache is a classic time-space trade-off. Lambda keeps sandboxes warm as long as practical (memory allows, function is active), but evicts idle ones to free resources. This gives warm starts for actively invoked functions while bounding resource usage.

---

## 15. Interview Angles

### Questions an Interviewer Might Ask

**Fleet Architecture:**
- "How does Lambda decide where to run my function?"
  - Answer: The placement service checks for (1) a frozen (cached) sandbox on any worker — warm start, (2) a worker with the function's code already cached — faster cold start, (3) any worker with enough memory — full cold start with code download. It also considers AZ distribution, worker health, and multi-tenancy spread. [INFERRED]

- "Why does Lambda sometimes give me a cold start even when my function has constant traffic?"
  - Answer: Two reasons: (1) Lambda periodically recycles execution environments for runtime updates and security patches. (2) If traffic increases, new concurrent requests require new sandboxes (even though existing ones are warm, they're busy handling current requests — Lambda is one-invocation-per-sandbox).

**Scaling:**
- "What's the limit on how fast Lambda can scale?"
  - Answer: 1,000 new execution environments per 10 seconds per function. So from 0 to 10,000 concurrency takes about 100 seconds. For instant capacity, use provisioned concurrency.

**Multi-Tenancy:**
- "How does Lambda prevent one customer's code from affecting another?"
  - Answer: Firecracker microVMs provide VM-level isolation — each function runs in its own microVM with its own guest kernel. The Firecracker jailer further constrains the VMM process with chroot, cgroups, and seccomp. Multiple customers' functions run on the same physical worker, but they're as isolated as separate VMs.

**Code Loading:**
- "What happens if my 10 GB container image takes too long to download?"
  - Answer: Lambda caches container image layers on worker hosts. The first cold start is slow (image pull), but subsequent cold starts on the same host reuse cached layers. Lambda also uses lazy loading — it doesn't necessarily download the entire image before starting; it can load blocks on demand. [INFERRED]

### Red Flags in Candidate Answers

| Red Flag | Why It's Wrong |
|---|---|
| "Lambda keeps a dedicated server for each customer" | Multi-tenant — many customers share the same workers |
| "Lambda spins up an EC2 instance for each invocation" | Lambda uses lightweight Firecracker microVMs (~125ms boot), not full EC2 instances |
| "Warm starts are guaranteed" | Sandboxes can be evicted anytime; even continuous traffic gets occasional cold starts |
| "Lambda scales instantly to any concurrency" | Limited to 1,000 new environments per 10 seconds per function |
| "The placement service is simple random routing" | Code-affinity routing, warm sandbox preference, AZ distribution, capacity checks [INFERRED] |
