# Fargate Architecture — Deep Dive

> Companion doc for [interview-simulation.md](interview-simulation.md)
> Covers: Firecracker microVM isolation, Fargate capacity management, task sizing, platform versions, Fargate Spot, ephemeral storage, image pulling

---

## Table of Contents

1. [What Is Fargate?](#1-what-is-fargate)
2. [Isolation Model — Firecracker MicroVMs](#2-isolation-model--firecracker-microvms)
3. [Fargate vs EC2 Architecture](#3-fargate-vs-ec2-architecture)
4. [Task Sizing — CPU and Memory Combinations](#4-task-sizing--cpu-and-memory-combinations)
5. [Platform Versions](#5-platform-versions)
6. [Fargate Spot](#6-fargate-spot)
7. [Networking on Fargate](#7-networking-on-fargate)
8. [Storage on Fargate](#8-storage-on-fargate)
9. [Container Image Pulling](#9-container-image-pulling)
10. [Task Retirement and Maintenance](#10-task-retirement-and-maintenance)
11. [Fargate Capacity Management](#11-fargate-capacity-management)
12. [Fargate Quotas and Limits](#12-fargate-quotas-and-limits)
13. [Design Decisions and Trade-offs](#13-design-decisions-and-trade-offs)
14. [Interview Angles](#14-interview-angles)

---

## 1. What Is Fargate?

Fargate is a **serverless compute engine** for containers. You define what to run (task definition with CPU/memory), and AWS handles everything else:

```
What YOU specify:                    What AWS handles:
┌─────────────────────────┐         ┌──────────────────────────────────┐
│ Container image         │         │ Provisioning compute             │
│ CPU & memory            │         │ Patching OS                      │
│ IAM roles               │         │ Scaling infrastructure           │
│ Networking (VPC/subnets)│         │ Security isolation (Firecracker) │
│ Log configuration       │         │ Task placement on hosts          │
└─────────────────────────┘         │ Monitoring host health           │
                                    │ Replacing failed infrastructure  │
                                    └──────────────────────────────────┘
```

**Key principle**: Each Fargate task has its own **isolation boundary** and does not share kernel, CPU, memory, or ENI with any other task.

---

## 2. Isolation Model — Firecracker MicroVMs

### 2.1 How Fargate Isolates Tasks

```
┌─────────────────────────────────────────────────────────────────────┐
│                    Fargate Host (Bare Metal)                         │
│                                                                      │
│  ┌──────────────────┐  ┌──────────────────┐  ┌──────────────────┐  │
│  │ Firecracker      │  │ Firecracker      │  │ Firecracker      │  │
│  │ MicroVM          │  │ MicroVM          │  │ MicroVM          │  │
│  │                  │  │                  │  │                  │  │
│  │ ┌──────────────┐ │  │ ┌──────────────┐ │  │ ┌──────────────┐ │  │
│  │ │Guest Kernel  │ │  │ │Guest Kernel  │ │  │ │Guest Kernel  │ │  │
│  │ ├──────────────┤ │  │ ├──────────────┤ │  │ ├──────────────┤ │  │
│  │ │ containerd   │ │  │ │ containerd   │ │  │ │ containerd   │ │  │
│  │ ├──────────────┤ │  │ ├──────────────┤ │  │ ├──────────────┤ │  │
│  │ │ Container A  │ │  │ │ Container X  │ │  │ │ Container P  │ │  │
│  │ │ Container B  │ │  │ │              │ │  │ │ Container Q  │ │  │
│  │ │ (sidecar)    │ │  │ │              │ │  │ │ (sidecar)    │ │  │
│  │ └──────────────┘ │  │ └──────────────┘ │  │ └──────────────┘ │  │
│  │                  │  │                  │  │                  │  │
│  │ Task: Customer A │  │ Task: Customer B │  │ Task: Customer C │  │
│  │ ENI: eni-aaa     │  │ ENI: eni-bbb     │  │ ENI: eni-ccc     │  │
│  └──────────────────┘  └──────────────────┘  └──────────────────┘  │
│                                                                      │
│  Host Kernel + KVM                                                   │
│  Jailer (chroot, namespaces, cgroups, seccomp per microVM)          │
└─────────────────────────────────────────────────────────────────────┘
```

### 2.2 What Each Task Gets (Isolated)

| Resource | Isolation Level |
|----------|----------------|
| **Kernel** | Separate guest kernel per microVM |
| **CPU** | Dedicated CPU allocation (not shared) |
| **Memory** | Dedicated memory allocation (not shared) |
| **Network** | Dedicated ENI in customer's VPC |
| **Storage** | Dedicated ephemeral storage |
| **Process namespace** | Isolated (can't see other tasks' processes) |
| **File system** | Isolated (chroot + separate rootfs) |

### 2.3 Firecracker vs Containers vs Full VMs

| Aspect | Docker Container | Firecracker (Fargate) | Full VM (EC2) |
|--------|-----------------|----------------------|---------------|
| Kernel | **Shared** with host | **Separate** guest kernel | **Separate** guest kernel |
| Boot time | < 1 second | < 125 ms | 30–60 seconds |
| Memory overhead | < 1 MB | < 5 MB | 100–300 MB |
| Security | Namespace isolation (weaker) | Hardware VM isolation (strong) | Hardware VM isolation (strong) |
| Multi-tenancy | Risky (shared kernel) | Safe (separate kernels) | Safe but heavy |
| Device model | Host passthrough | 5 minimal devices | 100+ emulated devices |

### 2.4 Why Firecracker for Fargate?

Fargate runs **untrusted, multi-tenant workloads** — different AWS customers' containers on shared hardware. Container-level isolation (Docker) is insufficient:

- A kernel vulnerability could let one customer access another's data
- Containers share the host kernel's syscall interface — huge attack surface
- Namespace escapes have been demonstrated repeatedly

Firecracker provides **VM-level isolation** with **container-like performance**: separate kernel per task, < 5 MB overhead, < 125 ms boot time.

---

## 3. Fargate vs EC2 Architecture

### 3.1 Architectural Differences

```
EC2 Launch Type:
┌────────────────────────────────────────────┐
│  EC2 Instance (customer-managed)            │
│                                             │
│  ┌─────────┐  ┌─────────┐  ┌─────────┐   │
│  │ Task 1  │  │ Task 2  │  │ Task 3  │   │  ← Multiple tasks share
│  │ (1 vCPU)│  │ (2 vCPU)│  │ (1 vCPU)│   │    the same OS kernel
│  └─────────┘  └─────────┘  └─────────┘   │
│                                             │
│  ECS Agent + Docker/containerd              │
│  Amazon Linux 2 (customer-managed AMI)      │
└────────────────────────────────────────────┘

Fargate:
┌────────────────────────────────────────────┐
│  Fargate Host (AWS-managed)                 │
│                                             │
│  ┌───────────┐ ┌───────────┐ ┌───────────┐│
│  │ MicroVM 1 │ │ MicroVM 2 │ │ MicroVM 3 ││  ← Each task in its own
│  │ (Task 1)  │ │ (Task 2)  │ │ (Task 3)  ││    VM with separate kernel
│  │ Own kernel│ │ Own kernel│ │ Own kernel││
│  └───────────┘ └───────────┘ └───────────┘│
│                                             │
│  Host kernel + KVM + Jailer                 │
│  AWS-managed (no customer access)           │
└────────────────────────────────────────────┘
```

### 3.2 Comparison Matrix

| Feature | EC2 Launch Type | Fargate |
|---------|----------------|---------|
| **Instance management** | Customer (AMI, patches, scaling) | AWS |
| **Cost model** | Pay for EC2 instances (even idle) | Pay per task (vCPU-sec + GB-sec) |
| **Task isolation** | Shared kernel on instance | VM-level isolation per task |
| **GPU support** | Yes (P3, G4, etc.) | No |
| **Custom AMI** | Yes | No |
| **Placement strategies** | Yes (spread, binpack, random) | No (auto-spread by AZ) |
| **EBS volumes** | Instance-attached | Task-level EBS support |
| **Privileged mode** | Yes | No |
| **Host networking mode** | Yes | No (awsvpc only) |
| **SSH to host** | Yes | No |
| **Docker socket access** | Yes | No |
| **Max task size** | Limited by instance type | 16 vCPU, 120 GB memory |
| **Startup overhead** | Fast (if instance warm) | Slower (microVM boot + image pull) |
| **Spot pricing** | EC2 Spot Instances | Fargate Spot |

### 3.3 When to Use Fargate vs EC2

| Use Case | Recommendation | Why |
|----------|---------------|-----|
| Most web services | Fargate | No server management, per-task billing |
| GPU workloads (ML inference) | EC2 | Fargate doesn't support GPUs |
| Custom kernel modules | EC2 | Fargate doesn't allow kernel customization |
| Very large tasks (> 16 vCPU) | EC2 | Fargate max is 16 vCPU |
| Cost-optimized steady state | EC2 (Reserved Instances) | Fargate is ~20-30% more expensive |
| Burst/variable workloads | Fargate | Pay only for what you use |
| Compliance requiring privileged access | EC2 | Fargate disallows privileged mode |
| Docker-in-Docker builds | EC2 | Fargate doesn't expose Docker socket |

---

## 4. Task Sizing — CPU and Memory Combinations

### 4.1 Valid Fargate Task Sizes

| CPU (units) | vCPU | Memory Options | OS |
|-------------|------|----------------|-----|
| 256 | 0.25 | 512 MiB, 1 GB, 2 GB | Linux only |
| 512 | 0.5 | 1 GB, 2 GB, 3 GB, 4 GB | Linux only |
| 1024 | 1 | 2–8 GB (1 GB increments) | Linux, Windows |
| 2048 | 2 | 4–16 GB (1 GB increments) | Linux, Windows |
| 4096 | 4 | 8–30 GB (1 GB increments) | Linux, Windows |
| 8192 | 8 | 16–60 GB (4 GB increments) | Linux only (platform 1.4.0+) |
| 16384 | 16 | 32–120 GB (8 GB increments) | Linux only (platform 1.4.0+) |

### 4.2 Windows Constraint

- Minimum CPU for Windows containers: **1 vCPU (1024 units)**
- Windows supports only **x86_64** architecture
- Supported Windows versions: Server 2019 Full/Core, Server 2022 Full/Core

### 4.3 Task-Level vs Container-Level Resources

On Fargate, CPU and memory are specified **at the task level**, not per container:

```
Task: 2 vCPU, 4 GB memory
├── Container A: 1 vCPU, 2 GB (hard limit)
├── Container B: 0.5 vCPU, 1 GB (hard limit)
└── Remaining: 0.5 vCPU, 1 GB (available for burst or sidecar)
```

Individual container limits must fit within the task-level allocation.

---

## 5. Platform Versions

### 5.1 What Are Platform Versions?

Platform versions represent the Fargate runtime environment — the combination of kernel, container runtime, and ECS agent version.

| Platform Version | Key Features | Notes |
|-----------------|--------------|-------|
| **1.4.0** (latest for Linux) | EFS volumes, ephemeral storage config, 8/16 vCPU tasks, container dependencies, `initProcessEnabled` | Recommended |
| **1.3.0** | Task ENI, secrets injection, basic feature set | Legacy |
| **1.0.0** (Windows) | Windows container support | Windows-specific |

### 5.2 Automatic Patching

- New tasks always run on the **latest platform version revision** within the configured version
- AWS releases minor revisions for security patches and bug fixes
- You don't choose revisions — only the major platform version (1.3.0, 1.4.0)
- This means Fargate tasks are **always patched** — no customer action needed

---

## 6. Fargate Spot

### 6.1 How Fargate Spot Works

```
┌──────────────────────────────────────────────────────┐
│               AWS Fargate Fleet                       │
│                                                       │
│  ┌────────────────────────────────────────────────┐  │
│  │  On-Demand Capacity                             │  │
│  │  (guaranteed, higher price)                     │  │
│  │  ████████████████████████████████████████       │  │
│  └────────────────────────────────────────────────┘  │
│                                                       │
│  ┌────────────────────────────────────────────────┐  │
│  │  Spare Capacity (Spot)                          │  │
│  │  (can be reclaimed, ~70% discount)              │  │
│  │  ░░░░░░░░░░░░░░░░░░░░                          │  │
│  └────────────────────────────────────────────────┘  │
│                                                       │
│  When AWS needs spare capacity back:                  │
│  → 2-minute SIGTERM warning to Fargate Spot tasks     │
│  → Tasks must handle graceful shutdown                │
└──────────────────────────────────────────────────────┘
```

### 6.2 Fargate Spot Configuration

```json
{
    "capacityProviderStrategy": [
        {
            "capacityProvider": "FARGATE",
            "weight": 1,
            "base": 2
        },
        {
            "capacityProvider": "FARGATE_SPOT",
            "weight": 3
        }
    ]
}
```

**In this example:**
- First 2 tasks always on On-Demand (`base: 2`)
- Remaining tasks: 25% On-Demand, 75% Spot (`weight` ratio 1:3)

### 6.3 Fargate Spot Considerations

| Aspect | Details |
|--------|---------|
| **Interruption warning** | 2 minutes (SIGTERM) |
| **Pricing** | ~70% discount vs on-demand |
| **Availability** | Not guaranteed — depends on spare capacity |
| **Best for** | Stateless, fault-tolerant, batch workloads |
| **Not recommended for** | Latency-sensitive, stateful, singleton tasks |
| **Capacity provider** | `FARGATE_SPOT` (pre-defined) |

---

## 7. Networking on Fargate

### 7.1 awsvpc — The Only Network Mode

Fargate **requires** `awsvpc` network mode. Each task gets its own ENI:

```
┌──────────────────────────────────────────────┐
│  Fargate Task                                 │
│                                               │
│  ┌─────────────┐  ┌─────────────┐           │
│  │ Container A │  │ Container B │  (sidecar) │
│  │ :8080       │  │ :9090       │           │
│  └──────┬──────┘  └──────┬──────┘           │
│         │                │                   │
│         └────────┬───────┘                   │
│                  │                           │
│           ┌──────▼──────┐                    │
│           │  ENI        │                    │
│           │  10.0.1.45  │  (private IP)      │
│           │  sg-xxx     │  (security group)  │
│           └─────────────┘                    │
└──────────────────────────────────────────────┘
```

### 7.2 Load Balancer Integration

- Must use **`ip` target type** (not `instance`)
- Supports ALB (Layer 7), NLB (Layer 4), GLB
- UDP routing supported on platform version 1.4+

---

## 8. Storage on Fargate

### 8.1 Ephemeral Storage

| Feature | Details |
|---------|---------|
| Default size | 20 GB |
| Maximum size | 200 GB (configurable in task definition) |
| Persistence | Ephemeral — deleted when task stops |
| Encryption | AES-256 (configurable with customer-managed KMS key) |
| Shared between containers | Yes (within the same task, via bind mounts) |

### 8.2 Persistent Storage Options

| Storage Type | Description |
|-------------|-------------|
| **Amazon EFS** | Shared file system across tasks; persists beyond task lifecycle |
| **Amazon EBS** | Block storage; attached to a single task; persists beyond task lifecycle |
| **Bind mounts** | Share ephemeral storage between containers in same task |

---

## 9. Container Image Pulling

### 9.1 Image Pull — The Cold Start Bottleneck

```
Fargate Task Startup Timeline:
├── Provision microVM (Firecracker) ── ~1-3 seconds [INFERRED]
├── Configure networking (ENI) ─────── ~1-5 seconds [INFERRED]
├── Pull container image ──────────── 5-60+ seconds (biggest variable!)
├── Start containers ──────────────── ~1-2 seconds
├── Health check pass ─────────────── depends on config
└── Register with load balancer ───── ~0-30 seconds
```

The container image pull is typically the **dominant factor** in Fargate task startup latency.

### 9.2 Seekable OCI (SOCI) — Lazy Image Loading

SOCI enables Fargate to start containers **before the full image is downloaded**:

```
Traditional pull:                    SOCI pull:
Download 100% of image ──► Start    Download index ──► Start container
(30-60 seconds)                     Load layers on demand (as needed)
                                    (5-10 seconds to first execution)
```

- SOCI creates an index that maps container file paths to image layer offsets
- At startup, only the files actually needed are downloaded
- Remaining layers are fetched lazily on first access
- Best for large images (1 GB+) where only a fraction of files are needed at boot

### 9.3 Image Caching [INFERRED]

- Fargate hosts may cache popular base images (e.g., `python:3.12`, `node:20`)
- Image layers shared across tasks on the same host don't need re-downloading
- Content-addressed storage (by layer digest) enables cross-customer deduplication

---

## 10. Task Retirement and Maintenance

### 10.1 How Task Retirement Works

AWS retires Fargate tasks when:
- The platform version revision has a security vulnerability
- The underlying host needs maintenance
- Infrastructure rotation policies require it

**Process:**
1. AWS sends a notification (via email / EventBridge)
2. New tasks automatically run on the latest patched revision
3. Existing vulnerable tasks are eventually stopped (with grace period)
4. Service scheduler automatically replaces retired tasks

### 10.2 Task Recycling [INFERRED]

Fargate may periodically recycle long-running tasks to migrate them to patched hosts. The service scheduler ensures replacements are launched before old tasks are stopped, maintaining desired count.

---

## 11. Fargate Capacity Management

### 11.1 How AWS Manages Fargate Capacity [INFERRED]

```
┌─────────────────────────────────────────────────────────────────────┐
│              AWS Fargate Fleet Management [INFERRED]                  │
│                                                                      │
│  ┌──────────────────────────────────────────────────────────────┐   │
│  │  Capacity Pool                                                │   │
│  │                                                               │   │
│  │  ┌──────────┐ ┌──────────┐ ┌──────────┐     ┌──────────┐   │   │
│  │  │ Host 1   │ │ Host 2   │ │ Host 3   │ ... │ Host N   │   │   │
│  │  │ (Nitro   │ │ (Nitro   │ │ (Nitro   │     │ (Nitro   │   │   │
│  │  │  bare    │ │  bare    │ │  bare    │     │  bare    │   │   │
│  │  │  metal)  │ │  metal)  │ │  metal)  │     │  metal)  │   │   │
│  │  │          │ │          │ │          │     │          │   │   │
│  │  │ [μVM]    │ │ [μVM]    │ │ [μVM]    │     │ [μVM]    │   │   │
│  │  │ [μVM]    │ │ [μVM]    │ │          │     │ [μVM]    │   │   │
│  │  │ [μVM]    │ │          │ │          │     │          │   │   │
│  │  └──────────┘ └──────────┘ └──────────┘     └──────────┘   │   │
│  │                                                               │   │
│  │  Fleet auto-scaling:                                          │   │
│  │  - Pre-provisions hosts based on demand prediction            │   │
│  │  - Maintains buffer capacity for burst launches               │   │
│  │  - Balances across AZs                                        │   │
│  └──────────────────────────────────────────────────────────────┘   │
│                                                                      │
│  Spot Pool: Excess capacity offered at discount                      │
│  On-Demand Pool: Reserved capacity guaranteed for customers          │
└─────────────────────────────────────────────────────────────────────┘
```

### 11.2 No Overcommit [INFERRED]

Unlike EC2 instances where multiple containers share CPU, Fargate tasks get **dedicated** CPU and memory. AWS does not overcommit resources on Fargate:
- If you request 2 vCPU, you get 2 dedicated vCPUs
- If you request 4 GB memory, you get 4 GB dedicated
- This is enforced by Firecracker's cgroup configuration

This is one reason Fargate is more expensive than EC2: no resource sharing between tasks.

---

## 12. Fargate Quotas and Limits

| Resource | Limit |
|----------|-------|
| Max task CPU | 16 vCPU |
| Max task memory | 120 GB |
| Min task CPU | 0.25 vCPU (256 units) |
| Min task memory | 512 MiB |
| Ephemeral storage | 20 GB default, up to 200 GB |
| Network mode | awsvpc only |
| Privileged mode | Not supported |
| Host network mode | Not supported |
| GPU | Not supported |
| Default Fargate on-demand vCPU quota | 6 concurrent vCPUs (adjustable) |
| Burst launch rate (most regions) | 100 tasks |
| Sustained launch rate (most regions) | 20 tasks/sec |
| Task launch rate per service | 500/min |
| Docker socket | Not accessible |
| SSH to host | Not available |

---

## 13. Design Decisions and Trade-offs

### 13.1 Why Per-Task MicroVM (Not Shared)?

| Approach | Pro | Con |
|----------|-----|-----|
| Shared VM (multiple tasks per VM) | Lower overhead, faster startup | Weaker isolation — kernel shared between customers |
| Per-task VM (Fargate model) | Strong isolation — separate kernel per customer task | Higher overhead (~5 MB per microVM) |

For a multi-tenant service running untrusted code from millions of AWS accounts, the per-task VM model is the only responsible choice. The ~5 MB overhead is negligible compared to the security benefit.

### 13.2 Why awsvpc Only?

Fargate doesn't support `bridge` or `host` networking because:
- **bridge** requires a Docker bridge on the host — Fargate abstracts the host away
- **host** shares the host's network namespace — defeats isolation
- **awsvpc** gives each task its own ENI and IP — clean isolation, works with security groups

### 13.3 Cost: Fargate vs EC2

| Scenario | Fargate | EC2 |
|----------|---------|-----|
| Steady-state, 24/7 | ~20-30% more expensive | Cheaper (especially with Reserved Instances) |
| Bursty/variable | Cheaper (pay per task-second) | Wasteful (idle instances) |
| Ops cost | $0 (no server management) | Significant (patching, scaling, monitoring) |
| Total cost of ownership | Often lower when including ops | Lower compute cost, higher ops cost |

### 13.4 Why No GPU on Fargate?

GPUs require:
- PCIe passthrough (not supported by Firecracker's minimal device model)
- Specific instance types (P3, G4, etc.)
- NVIDIA drivers on the host
- Fargate's abstraction doesn't expose hardware selection

[INFERRED] Adding GPU support would require extending Firecracker's device model or using a different VMM, which would increase attack surface.

---

## 14. Interview Angles

### 14.1 Key Questions

**Q: "How does Fargate achieve multi-tenant isolation?"**

Each Fargate task runs in its own Firecracker microVM — a lightweight VM with its own guest kernel, memory, CPU, and network interface. This provides hardware-enforced isolation via KVM. Even if a customer's code exploits a container escape vulnerability, it's contained within the microVM. The Firecracker jailer adds defense-in-depth with chroot, network namespaces, cgroups, seccomp filters, and unprivileged execution.

**Q: "Why is Fargate task startup slower than EC2?"**

On EC2 with a warm instance, the container image may already be cached and the instance is running. Fargate must:
1. Provision a Firecracker microVM (~1-3s)
2. Set up an ENI in the customer's VPC (~1-5s)
3. Pull the container image (~5-60+s depending on size)
4. Start the container runtime and containers (~1-2s)

The image pull is the dominant factor. SOCI (Seekable OCI) helps by enabling lazy loading.

**Q: "A customer wants to run 1,000 Fargate tasks. What quota issues might they hit?"**

Default Fargate on-demand vCPU quota is **6 concurrent vCPUs** per account. At 1 vCPU per task, they can only run 6 tasks by default. They need to request a quota increase via the Service Quotas console. Also check:
- Burst launch rate: 100 tasks (may need ramping)
- Sustained launch rate: 20 tasks/sec
- ENI limits in their VPC subnets (1 ENI per task in awsvpc mode)

**Q: "Fargate vs EC2 — when would you recommend EC2?"**

EC2 when you need: GPUs, custom AMIs, privileged containers, Docker-in-Docker, host networking, very large tasks (> 16 vCPU), or cost optimization for steady-state workloads with Reserved Instances. Fargate for everything else — the reduced operational overhead usually outweighs the ~20-30% compute cost premium.

### 14.2 Numbers to Know

| Metric | Value |
|--------|-------|
| Max Fargate task CPU | 16 vCPU |
| Max Fargate task memory | 120 GB |
| Min Fargate task CPU | 0.25 vCPU |
| Firecracker microVM overhead | < 5 MB |
| Firecracker boot time | < 125 ms |
| Ephemeral storage default | 20 GB |
| Ephemeral storage max | 200 GB |
| Fargate Spot discount | ~70% |
| Spot interruption warning | 2 minutes (SIGTERM) |
| Default on-demand vCPU quota | 6 |
| Burst launch rate | 100 tasks |
| Sustained launch rate | 20 tasks/sec |
| Network mode | awsvpc only |

---

*Cross-references:*
- [Cluster Architecture](cluster-architecture.md) — Three-layer model, capacity options comparison
- [Task Placement](task-placement.md) — Why Fargate has no placement strategies
- [Networking and Service Discovery](networking-and-service-discovery.md) — awsvpc mode, ENI per task
- [Capacity Providers](capacity-providers.md) — Fargate and Fargate Spot capacity providers
