# System Design Interview Simulation: Design AWS Lambda (Serverless Compute)

> **Interviewer:** Principal Engineer (L8), AWS Lambda Team
> **Candidate Level:** SDE-3 (L6 — Senior Software Development Engineer)
> **Duration:** ~60 minutes
> **Date:** February 12, 2026

---

## PHASE 1: Opening & Problem Statement (~2 min)

**Interviewer:**
Hey, welcome. I'm [name], Principal Engineer on the Lambda team. For today's system design round, I'd like you to design a **serverless compute platform** — think AWS Lambda. A system where customers upload code, and the platform runs that code in response to events — without customers managing any servers. We're talking about the core execution infrastructure: how you receive an invocation, spin up an isolated environment, run the code, and return the result.

I care about how you think through multi-tenant isolation, cold start latency, and scaling from zero to millions of concurrent executions. I'll push on your decisions — that's me calibrating depth, not a negative signal.

Take it away.

---

## PHASE 2: Requirements Gathering & Scoping (~5 min)

**Candidate:**
Thanks! Lambda is a unique system — it's invocation-based, not long-running, which changes almost every design decision compared to container orchestration. Let me scope this down carefully.

**Functional Requirements — what operations do we need?**

> "The core operations I'd expect:
> - **CreateFunction** — upload code (zip or container image), configure runtime, memory, timeout, environment variables.
> - **Invoke (Synchronous)** — call the function, block until it returns a response. Used by API Gateway, SDK calls.
> - **Invoke (Asynchronous)** — fire-and-forget: Lambda queues the event and processes it. Used by S3 events, SNS, etc.
> - **Event Source Mapping** — Lambda polls a stream/queue (SQS, Kinesis, DynamoDB Streams) and invokes the function with batches of records.
> - **UpdateFunctionCode / UpdateFunctionConfiguration** — deploy new code or change settings.
> - **PublishVersion / CreateAlias** — immutable versions and mutable aliases for traffic shifting.
>
> A few clarifying questions:
> - **What runtimes do we support?** I assume we need to support multiple: Node.js, Python, Java, .NET, Go, Ruby, plus custom runtimes and container images?"

**Interviewer:** "Yes, runtime flexibility is important. Lambda supports managed runtimes and custom runtimes via the Runtime API. Container image support up to 10 GB."

> "- **Do we need to handle both synchronous and asynchronous invocation models?**"

**Interviewer:** "Yes, both are critical. And event source mappings are a third model — Lambda pulls from streams/queues."

> "- **What about VPC connectivity?** Should functions be able to reach resources inside a customer's VPC?"

**Interviewer:** "Yes. VPC-attached Lambda is a key use case. And as you'll recall, it had a major cold start problem that was solved in an interesting way."

> "Right — I'll get to the Hyperplane ENI approach. Let me also ask: **Are we designing the control plane (function management) or the data plane (invocation path), or both?**"

**Interviewer:** "Focus on the data plane — the invocation path. How does a request come in, how does the function execute, how does the response go back. The control plane is important but less interesting architecturally."

**Non-Functional Requirements:**

> "Now the critical constraints. Lambda's non-functional properties are what make it hard:
>
> | Dimension | Requirement | Rationale |
> |---|---|---|
> | **Multi-tenant Isolation** | VM-level isolation between customers | Running untrusted customer code on shared hardware — must prevent escape. This is the #1 security requirement. |
> | **Cold Start Latency** | < 1 second for most runtimes; < 200ms for warm invocations | Cold starts are Lambda's Achilles' heel. Customers feel every millisecond. |
> | **Scale** | 0 to thousands of concurrent executions per function; millions across the fleet | Must scale from literally zero (no servers) to massive concurrency, and back to zero. |
> | **Availability** | 99.99% (4 9's) | ~52 min downtime/year. Invocations must succeed even during partial infrastructure failures. |
> | **Invocation Latency** | Single-digit millisecond overhead for warm invocations | The platform overhead on top of function execution time must be minimal. |
> | **Timeout** | Up to 900 seconds (15 minutes) per invocation | Long-running but bounded — not infinite. |
> | **Payload** | 6 MB synchronous request/response; 256 KB async | Invocations carry event data in, response data out. Not for large data transfer. |
> | **Cost Model** | Pay per invocation + GB-seconds of compute | No charge when idle. This drives the entire architecture — you can't keep VMs running 'just in case.' |
>
> The cost model is what makes Lambda fundamentally different from ECS/Fargate/K8s. Customers pay nothing when there's no traffic, so we can't pre-allocate dedicated infrastructure per customer. We must share hardware across tenants while maintaining strict isolation."

**Interviewer:**
You mentioned VM-level isolation. Why not container isolation?

**Candidate:**

> "This is a critical design decision. In a multi-tenant serverless platform, we're running **arbitrary, untrusted customer code** on shared hardware. Container isolation (cgroups + namespaces) shares the host kernel with the container. A kernel vulnerability could let one customer's code escape the container and access another customer's data or code.
>
> VM isolation provides a much stronger boundary — each function runs in its own virtual machine with its own guest kernel. Even if the guest kernel is compromised, the hypervisor prevents escape to the host. The attack surface is the hypervisor (a much smaller surface than the entire host kernel).
>
> But traditional VMs (QEMU/KVM) are too heavy — they take seconds to boot and use hundreds of MB of memory. That's unacceptable for a platform where functions might only run for 50ms.
>
> This is exactly why AWS built **Firecracker** — a lightweight Virtual Machine Monitor (VMM) purpose-built for serverless. It gives you VM-level isolation with container-like boot times."

**Interviewer:**
Good. That's the right framing. Let's get some numbers.

---

### L5 vs L6 vs L7 — Phase 2 Rubric

| Aspect | L5 (SDE-2) | L6 (SDE-3) | L7 (Principal) |
|---|---|---|---|
| **Functional Reqs** | Lists invoke and create operations | Proactively distinguishes sync/async/event-source-mapping invocation models; asks about control plane vs data plane | Additionally discusses versioning/aliasing for safe deployments, extensions API, and how the programming model constrains architecture |
| **Non-Functional** | Mentions scale and latency | Quantifies cold start targets, frames the cost model as architectural driver, explains why VM isolation over containers | Frames NFRs around business impact: "pay-per-use means zero cost at idle drives us to time-share hardware, which forces the isolation question" |
| **Isolation** | "Use containers" | Explains container vs VM tradeoff, names Firecracker, explains the kernel attack surface argument | Discusses defense-in-depth (Firecracker + jailer + cgroups + seccomp + namespaces), threat model, comparison with gVisor/Kata |

---

## PHASE 3: Scale Estimation (~3 min)

**Candidate:**

> "Let me estimate Lambda-scale numbers to ground our design decisions."

#### Invocation Traffic

> "AWS has stated Lambda handles trillions of invocations per month. Let me work with reasonable numbers:
>
> - **Invocations per month**: ~10 trillion (10^13)
> - **Average invocations per second**: 10^13 / (30 × 86400) ≈ **3.8 million invocations/sec** average
> - **Peak invocations per second**: ~2-3x average ≈ **10 million invocations/sec** at peak
> - **Average function duration**: ~200ms (highly variable — from 1ms to 900 seconds)
> - **Average concurrent executions at any moment**: 3.8M × 0.2s = **~760,000 concurrent sandboxes** average, millions at peak"

#### Resource Estimates

> "For each concurrent execution, Lambda needs:
>
> - **Memory per sandbox**: 128 MB to 10,240 MB (customer-configured). Average ~512 MB.
> - **Firecracker overhead per microVM**: ~5 MB VMM memory [Source: Firecracker NSDI 2020 paper]
> - **Total fleet memory at 760K concurrent**: 760K × 512 MB ≈ **390 TB of RAM** just for function sandboxes
> - **Number of worker hosts**: If each host has 384 GB RAM and runs sandboxes using ~80% of memory: 390 TB / (384 GB × 0.8) ≈ **~1,270 hosts** minimum for average load. At peak, 3-5x more.
>
> But that's just one region. Lambda operates in 30+ regions. The global fleet is enormous."

#### Code Storage

> "- **Total functions**: Millions of distinct functions across all customers
> - **Average deployment package**: ~10 MB (zip). Some up to 250 MB (zip) or 10 GB (container image)
> - **Code must be cached on worker hosts** — downloading a 250 MB package on every cold start would be devastating for latency"

**Interviewer:**
Good. The code caching point is critical — we'll come back to that. Let's architect this.

---

## PHASE 4: High-Level Architecture (~5 min)

**Candidate:**

> "Let me start with the simplest thing that works, then find the problems and fix them."

#### Attempt 0: Single Server

> "Simplest possible design — one server that receives HTTP requests and runs functions:
>
> ```
>     Client
>       |
>       v
>   +---------------------+
>   |   Single Server     |
>   |                     |
>   |   Invoke(fn, event) |
>   |   -> load fn code   |
>   |   -> fork process   |
>   |   -> run handler    |
>   |   -> return result  |
>   |                     |
>   |   Local Disk (code) |
>   +---------------------+
> ```
>
> The function code is stored locally, we fork a process, run the handler, return the response."

**Interviewer:**
What's wrong with this?

**Candidate:**

> "Multiple critical problems:
>
> 1. **No isolation** — We're running untrusted customer code in forked processes on the same OS. One customer's code can read another's memory, files, environment variables. This is a non-starter for multi-tenancy.
>
> 2. **Single point of failure** — If the server dies, all invocations fail.
>
> 3. **No scaling** — One server can only handle so many concurrent processes. We need to spread load across many machines.
>
> The isolation problem is the most important. Let's fix that first."

#### Attempt 1: Container-Based Isolation

> "Let's run each function invocation in its own Linux container:
>
> ```
>     Client
>       |
>       v
>   +-----------+
>   |  Gateway  | --- routes invocations to worker hosts
>   +-----+-----+
>         |
>    +----+----+----+
>    |    |    |    |
>    v    v    v    v
>   +--+ +--+ +--+ +--+
>   |W1| |W2| |W3| |W4|   Worker Hosts
>   +--+ +--+ +--+ +--+
>    |    |    |    |
>   Containers running function code
> ```
>
> Each worker host runs multiple containers. The gateway knows which workers have capacity and routes invocations to them.
>
> **This is better but has a fatal flaw:**"

**Interviewer:**
What's the fatal flaw?

**Candidate:**

> "**Container isolation is insufficient for multi-tenant serverless.** Containers share the host kernel. All it takes is one Linux kernel CVE — a privilege escalation, a namespace escape — and Customer A's code can break out of the container and access Customer B's function code, secrets, or data.
>
> For a service like ECS or Kubernetes where a single customer runs their own containers on their own instances, this is acceptable. But for Lambda, where we're running **millions of different customers' code on the same physical hardware**, we need a stronger isolation boundary.
>
> We need **VM-level isolation** — but traditional VMs (QEMU/KVM with full device emulation) have two problems:
> - Boot time: 3-10+ seconds (unacceptable for cold starts)
> - Memory overhead: 100+ MB per VM for the QEMU process and guest kernel
>
> This is exactly the problem Firecracker was built to solve."

#### Attempt 2: Firecracker MicroVM Architecture

> "Firecracker is a lightweight VMM (Virtual Machine Monitor) written in Rust. It uses Linux KVM for hardware virtualization but replaces QEMU's bloated device model with a minimal one:
>
> | Property | QEMU/KVM (Traditional VM) | Firecracker MicroVM |
> |---|---|---|
> | **Boot time** | 3-10+ seconds | ~125ms [Source: Firecracker NSDI 2020 paper] |
> | **Memory overhead** | 100+ MB for QEMU + guest | ~5 MB for the VMM process [Source: Firecracker paper] |
> | **Device model** | Full emulation (USB, GPU, sound, etc.) | Minimal: virtio-net, virtio-block, serial, partial i8042, RTC only |
> | **Attack surface** | ~70K lines of C (QEMU) | ~50K lines of Rust [Source: Firecracker paper], memory-safe |
> | **Language** | C (memory-unsafe) | Rust (memory-safe) |
> | **Rate of creation** | ~1 VM/sec per host | **150+ microVMs/sec/host** [Source: Firecracker paper] |
>
> **Additional isolation layers** (defense in depth) [Source: Firecracker paper]:
> - **Jailer**: A process that sets up cgroups, namespaces, and seccomp filters before launching the Firecracker VMM. Even if the VMM is compromised, the jailer constrains what it can do.
> - **Seccomp**: The VMM process is restricted to ~25 syscalls [INFERRED from paper description — exact count may vary].
> - **Each microVM runs as an unprivileged process** — no root, no capabilities.
>
> Here's the evolved architecture with Firecracker:
>
> ```
>                         +-------------------+
>                         |     Clients       |
>                         | (API GW, SDK,     |
>                         |  S3 events, SQS)  |
>                         +--------+----------+
>                                  |
>                         +--------v----------+
>                         |   Front-End       |
>                         |   (Invoke API)    |
>                         |   Auth, Throttle, |
>                         |   Route           |
>                         +--------+----------+
>                                  |
>                         +--------v----------+
>                         |  Placement /      |
>                         |  Worker Manager   |
>                         |  "Where to run    |
>                         |   this function?" |
>                         +--------+----------+
>                                  |
>              +-------------------+-------------------+
>              |                   |                   |
>     +--------v------+  +--------v------+  +---------v-----+
>     |  Worker Host  |  |  Worker Host  |  |  Worker Host  |
>     |               |  |               |  |               |
>     | +----------+  |  | +----------+  |  | +----------+  |
>     | |Firecracker|  |  | |Firecracker|  |  | |Firecracker|  |
>     | | MicroVM   |  |  | | MicroVM   |  |  | | MicroVM   |  |
>     | | [Fn A]    |  |  | | [Fn C]    |  |  | | [Fn E]    |  |
>     | +----------+  |  | +----------+  |  | +----------+  |
>     | +----------+  |  | +----------+  |  | +----------+  |
>     | |Firecracker|  |  | |Firecracker|  |  | |Firecracker|  |
>     | | MicroVM   |  |  | | MicroVM   |  |  | | MicroVM   |  |
>     | | [Fn B]    |  |  | | [Fn D]    |  |  | | [Fn F]    |  |
>     | +----------+  |  | +----------+  |  | +----------+  |
>     +---------------+  +---------------+  +---------------+
> ```
>
> **How a synchronous invocation works in this design:**
> 1. Client sends `Invoke(function-name, event-payload)` to the front-end
> 2. Front-end authenticates (SigV4), checks throttling, looks up function metadata
> 3. Front-end asks the placement service: 'Where is a warm sandbox for this function?'
> 4. If a warm sandbox exists → route the invocation to that worker host (fast path)
> 5. If no warm sandbox → cold start: placement service selects a worker host, downloads code, boots a Firecracker microVM, initializes the runtime, runs function init code
> 6. Function handler executes, returns response
> 7. Response flows back through front-end to client
> 8. MicroVM is **kept alive** (frozen) for potential reuse — this is a 'warm' sandbox"

**Interviewer:**
Good — you've identified the key insight: Firecracker gives us VM-level isolation at container-like speeds. But I see several problems we need to solve. The placement service is doing a lot of work. Code download during cold start adds latency. And how does this scale to millions of concurrent functions?

**Candidate:**

> "Exactly — let me identify the problems:
>
> | Component | Current State | Problem |
> |---|---|---|
> | **Placement** | Centralized placement service | Single point of failure; how does it know which worker has warm sandboxes? |
> | **Cold start** | Download code + boot microVM + init runtime | Total cold start can be 1-10+ seconds depending on package size and runtime |
> | **Scaling** | Static worker fleet | How do we scale from 0 to thousands of concurrent executions for one function? |
> | **Sandbox lifecycle** | Keep sandboxes alive forever? | Wastes resources; need an eviction policy |
> | **Event sources** | Not addressed | How does Lambda poll SQS/Kinesis/DynamoDB Streams? |
>
> Let's deep-dive each one."

**Interviewer:**
Let's start with the execution environment lifecycle and cold starts — that's the heart of Lambda's performance story.

---

### L5 vs L6 vs L7 — Phase 4 Rubric

| Aspect | L5 (SDE-2) | L6 (SDE-3) | L7 (Principal) |
|---|---|---|---|
| **Architecture** | Draws gateway + workers, mentions containers | Drives iterative evolution from process → container → microVM, explains why each is insufficient | Discusses the Firecracker jailer, seccomp, defense-in-depth layers; compares with gVisor/Kata Containers |
| **Isolation reasoning** | "Use VMs for security" | Explains shared-kernel attack surface, quantifies Firecracker boot time and memory overhead from the paper | Discusses threat model: what specific attacks Firecracker defends against (Spectre/Meltdown mitigations, side channels), and what it doesn't |
| **Invocation flow** | Lists steps at high level | Distinguishes warm path vs cold path, explains sandbox reuse | Discusses the placement decision algorithm, how worker manager tracks sandbox state, cache-affinity routing |

---

## PHASE 5: Deep Dive — Execution Environment & Cold Starts (~10 min)

**Candidate:**

> "The execution environment lifecycle is the most important thing to understand about Lambda. Every performance and cost decision flows from this."

#### Execution Environment Lifecycle

> "A Lambda execution environment goes through three phases [Source: AWS Lambda Developer Guide — verified]:
>
> ```
> COLD START (new sandbox):
>
>   +--------+    +---------+    +----------+    +---------+
>   | Create |    |  Init   |    |  Invoke  |    | Freeze  |
>   | MicroVM| -> | Phase   | -> |  Phase   | -> | (keep   |
>   | + Code |    |         |    |          |    |  warm)  |
>   | Download    | Extension|    | Handler  |    |         |
>   +--------+    | Init    |    | runs     |    +---------+
>                 | Runtime |    |          |
>                 | Init    |    |          |
>                 | Function|    |          |
>                 | Init    |    |          |
>                 +---------+    +----------+
>
> WARM START (reused sandbox):
>
>                              +-----------+    +---------+
>                              |  Invoke   |    | Freeze  |
>                              |  Phase    | -> | (keep   |
>                              |           |    |  warm)  |
>                              | Handler   |    |         |
>                              | runs      |    +---------+
>                              +-----------+
>
> SHUTDOWN (after idle period or Lambda decides to reclaim):
>
>   +-----------+
>   | Shutdown  |
>   | Phase     |
>   | (cleanup) |  -> MicroVM destroyed
>   +-----------+
> ```
>
> **Init Phase** [Source: AWS docs — verified]:
> 1. **Extension init** — start all registered extensions
> 2. **Runtime init** — bootstrap the runtime (Node.js, Python, Java JVM, etc.)
> 3. **Function init** — run the customer's static initialization code (module-level code, global variables, connection setup)
>
> The Init phase has a **10-second timeout** for standard functions. For provisioned concurrency and SnapStart functions, it's up to **130 seconds or the configured function timeout (up to 15 minutes)** [Source: AWS docs — verified].
>
> **Invoke Phase**: The handler function executes. Duration is bounded by the function's configured timeout (max 900 seconds / 15 minutes) [Source: AWS docs — verified].
>
> **Shutdown Phase**: Lambda sends a Shutdown event to extensions. Duration: 0ms (no extensions), 500ms (internal extension), or 2,000ms (external extensions) [Source: AWS docs — verified].
>
> **Key point**: After an invocation completes, Lambda **freezes** the execution environment — the microVM process is paused, its memory is preserved, /tmp contents persist. On the next invocation to the same function, Lambda **thaws** the environment and jumps straight to the Invoke phase. This is why warm starts are so much faster."

**Interviewer:**
Walk me through exactly what happens during a cold start — every millisecond matters.

**Candidate:**

> "Let me break down the cold start into its component times. These are approximate and vary by runtime and configuration:
>
> ```
> COLD START BREAKDOWN:
>
> 1. Worker Selection + Scheduling:           ~10-50ms  [INFERRED]
>    Placement service picks a worker with capacity
>
> 2. Code Download:                            ~50-500ms [INFERRED — depends on package size]
>    Download function code from S3 (or pull container image from ECR)
>    - 1 MB zip from S3: ~50ms (intra-region)
>    - 50 MB zip from S3: ~200ms
>    - 250 MB unzipped with layers: ~500ms+
>    - 10 GB container image: seconds (but uses lazy loading / mount from cache)
>
> 3. Firecracker MicroVM Boot:                 ~125ms [Source: Firecracker NSDI 2020 paper]
>    Create VM with guest kernel, configure virtio devices, boot Linux
>
> 4. Runtime Init:                             ~50-500ms [INFERRED — depends on runtime]
>    - Python: fast (~50ms)
>    - Node.js: fast (~50-100ms)
>    - Java (JVM cold start): slow (~500ms-2s without SnapStart)
>    - .NET: moderate (~200-500ms)
>
> 5. Function Init (customer code):            variable
>    - Import libraries, establish DB connections, load ML models
>    - Can be 0ms to several seconds depending on what the code does
>
> TOTAL COLD START: ~300ms (Python, small package) to 5+ seconds (Java, large package, VPC)
> ```
>
> **The cold start is billed** — the Init Duration appears in CloudWatch logs and the customer pays for it [Source: AWS docs — verified].
>
> Cold starts occur for **< 1% of invocations** in typical production workloads [Source: AWS docs — verified]. But for latency-sensitive APIs, even 1% of requests being 2-5x slower is unacceptable."

**Interviewer:**
How does Lambda reduce cold starts?

**Candidate:**

> "Multiple strategies at different layers:
>
> **1. Sandbox Reuse (Warm Starts)**
> The most impactful optimization. After a function invocation completes, Lambda keeps the microVM alive (frozen) instead of destroying it. If another invocation arrives for the same function within the keep-alive window, Lambda thaws the existing sandbox — skipping the entire Init phase.
>
> ```
> Sandbox Lifecycle:
>
>   Created  -->  Active (handling invocation)  -->  Frozen (idle, warm)
>     ^                                                  |
>     |                                                  v
>     +--- destroyed (after idle timeout or eviction) <--+
>     |                                                  |
>     +--- thawed for next invocation <------------------+
> ```
>
> Lambda doesn't publicly document the exact idle timeout, but empirically sandboxes stay warm for **~5-15 minutes of inactivity** [INFERRED — based on community observations; not officially documented]. Lambda manages this dynamically based on fleet utilization.
>
> **2. Code Caching on Workers**
> Lambda caches function deployment packages on worker hosts [INFERRED from architecture]. If a function was recently invoked on a worker, the code is already present locally — no need to download from S3 again. This shaves off the code download latency even for cold starts.
>
> **3. Provisioned Concurrency** [Source: AWS docs — verified]
> Customers can pre-warm a specified number of execution environments:
>
> ```
> aws lambda put-provisioned-concurrency-config \
>   --function-name my-api \
>   --qualifier prod \
>   --provisioned-concurrent-executions 200
> ```
>
> Lambda pre-initializes 200 sandboxes that are always warm. First 200 concurrent requests get instant warm starts. Beyond 200, standard on-demand scaling kicks in (cold starts possible).
>
> - Allocation rate: **up to 6,000 environments per minute per function** [Source: AWS docs — verified]
> - Provisioned concurrency **costs money** even when idle — you're paying for pre-warmed capacity
> - Can be configured with Application Auto Scaling to schedule provisioned concurrency (e.g., more during business hours)
>
> **4. SnapStart** [Source: AWS docs — verified]
> For Java 11+, Python 3.12+, and .NET 8+, SnapStart eliminates the Init phase by:
>
> ```
> PUBLISH TIME (one-time):
>   1. Lambda initializes the function (boots microVM, starts JVM, runs init code)
>   2. Takes a Firecracker microVM snapshot — captures memory + disk state
>   3. Encrypts and caches the snapshot (multiple copies for resilience)
>
> INVOCATION TIME (every cold start):
>   1. Instead of full Init, Lambda restores from the cached snapshot
>   2. Thaws the microVM from the snapshot — JVM is already running, classes loaded
>   3. Jumps straight to the Invoke phase
>
> Result: Java cold start goes from ~2-5 seconds → sub-second
> ```
>
> **SnapStart limitations** [Source: AWS docs — verified]:
> - Only works with published versions (not $LATEST)
> - Cannot use provisioned concurrency simultaneously
> - Cannot use ephemeral storage > 512 MB
> - Cannot use Amazon EFS
>
> **SnapStart uniqueness concern** [Source: AWS docs — verified]:
> Since multiple execution environments restore from the same snapshot, anything generated during Init (random numbers, UUIDs, timestamps) will be **identical** across all environments. Customers must generate unique values in the handler, not during init.
>
> **5. Optimized Container Image Loading**
> For container image functions (up to 10 GB), Lambda doesn't download the entire image on cold start. It uses:
> - **Lazy loading**: Only fetch the filesystem blocks actually accessed during init [INFERRED from re:Invent 2022 talks on Lambda container support]
> - **Local caching**: Container layers are cached on worker hosts
> - **Deduplication**: Shared base layers (e.g., Amazon Linux 2) are cached once per worker, shared across functions"

**Interviewer:**
Tell me more about the Firecracker snapshot mechanism for SnapStart. What makes it work and what are the gotchas?

**Candidate:**

> "The Firecracker snapshot is a core capability. From the Firecracker paper and AWS blog posts:
>
> **How Firecracker snapshots work** [Source: Firecracker paper + AWS docs]:
> - Firecracker can serialize the complete microVM state: vCPU registers, RAM contents, device state
> - The snapshot is taken after Init completes but before the first Invoke
> - On restore, Firecracker loads the snapshot into a new microVM and resumes execution
> - The guest doesn't know it was snapshotted — it thinks it's a continuous execution
>
> **Gotchas that must be addressed:**
>
> 1. **Stale connections**: Network connections established during Init (e.g., to a database) will be dead after restore because the remote server has closed them. AWS SDKs handle this by reconnecting automatically, but third-party libraries may not.
>
> 2. **Stale credentials**: If you fetched IAM temporary credentials during Init, they might be expired when the snapshot is restored minutes or hours later. Must refresh credentials in the handler.
>
> 3. **Entropy / randomness**: `/dev/random` or `/dev/urandom` state is identical across all restored instances. If your Init code seeded a PRNG, all instances will generate the same 'random' sequence. Java's `java.util.UUID.randomUUID()` is affected — AWS provides Runtime Hooks (CRaC) to re-seed on restore.
>
> 4. **Snapshot staleness**: If you publish a new version, Lambda takes a new snapshot. But the old snapshot might still be serving requests during the transition. Lambda handles this via immutable versioning — each published version has its own snapshot."

> *For the full deep dive on execution environment lifecycle and cold starts, see [execution-environment-lifecycle.md](execution-environment-lifecycle.md).*

#### Architecture Update After Phase 5

> "Our execution environment understanding has evolved:
>
> | | Before (Phase 4) | After (Phase 5) |
> |---|---|---|
> | **Isolation** | ~~Containers~~ | Firecracker microVMs with jailer, seccomp, cgroups |
> | **Cold starts** | Unaddressed | Sandbox reuse (warm), provisioned concurrency, SnapStart, code caching |
> | **Lifecycle** | Not defined | Init → Invoke → Freeze → (Thaw → Invoke)* → Shutdown |
> | **Placement** | Centralized, naive | *(still needs design — next phase)* |

---

### L5 vs L6 vs L7 — Phase 5 Rubric

| Aspect | L5 (SDE-2) | L6 (SDE-3) | L7 (Principal) |
|---|---|---|---|
| **Cold start understanding** | "Cold starts are slow because the VM needs to boot" | Breaks down cold start into 5 components (scheduling, code download, VM boot, runtime init, function init) with approximate times for each | Quantifies each component, discusses variance across runtimes, explains why Java is worst (JVM class loading, JIT warmup), proposes measurement approach |
| **Mitigation strategies** | "Keep the sandbox warm" | Explains provisioned concurrency, SnapStart with snapshot mechanism and uniqueness concerns, sandbox reuse window | Discusses cache-affinity routing for code locality, tiered caching (L1 worker cache, L2 shared regional cache), container image lazy loading internals |
| **SnapStart depth** | Knows it exists | Explains checkpoint/restore mechanism, supported runtimes, uniqueness gotchas, limitations | Discusses Firecracker snapshot format, CRaC integration, how Lambda manages snapshot cache invalidation, blast radius of snapshot corruption |

---

## PHASE 6: Deep Dive — Worker Fleet & Placement (~10 min)

**Interviewer:**
Good cold start analysis. Now let's talk about the fleet. You have thousands of worker hosts, millions of microVMs, and you need to decide: when an invocation arrives, which worker host runs it?

**Candidate:**

> "This is the **placement problem** — the core scheduling challenge of Lambda. Let me think through this carefully because the wrong approach kills either performance or utilization.
>
> **What the placement service needs to decide:**
> 1. Is there a **warm sandbox** for this function? If so, route to it (fast path).
> 2. If not, which worker host should create a **new sandbox**? (cold path)
> 3. How many new sandboxes should be created to handle a burst of traffic?
> 4. When should sandboxes be **evicted** to reclaim resources?"

#### The Warm Path — Sandbox Routing

> "The placement service maintains a mapping: `function_id → list of warm sandboxes (worker host, sandbox ID, last_invocation_time, state)`.
>
> ```
> Sandbox Registry (conceptual):
>
>   function-abc-v3:
>     sandbox-1: worker-17, state=FROZEN, last_used=T-30s
>     sandbox-2: worker-42, state=FROZEN, last_used=T-120s
>     sandbox-3: worker-42, state=ACTIVE, current_invocation=req-789
>
>   function-xyz-v1:
>     sandbox-4: worker-8,  state=FROZEN, last_used=T-5s
> ```
>
> When an invocation arrives for `function-abc-v3`:
> 1. Check registry for a FROZEN sandbox → found sandbox-1 on worker-17
> 2. Mark sandbox-1 as ACTIVE
> 3. Route the invocation to worker-17, which thaws the sandbox
> 4. After invocation completes, mark sandbox-1 as FROZEN again
>
> If all sandboxes are ACTIVE (busy), we need a new one → cold start."

**Interviewer:**
How is this registry implemented? It's updated on every invocation — that's millions of updates per second.

**Candidate:**

> "Good question. A centralized registry wouldn't scale. Here's how I'd approach it:
>
> **Option 1: Centralized registry (doesn't scale)**
> - Single database tracking all sandboxes
> - Bottleneck: millions of reads and writes per second per region
> - Single point of failure
>
> **Option 2: Partitioned registry with consistent hashing** [INFERRED — this is the likely architecture]
> - Hash `function_id` to a registry partition
> - Each partition handles a subset of functions
> - Registry partitions are replicated for availability
> - Front-end servers cache recent routing decisions (with short TTL)
>
> **Option 3: Worker-side tracking with gossip** [INFERRED — alternative approach]
> - Each worker host tracks its own sandboxes
> - Workers publish sandbox availability to a distributed coordination layer
> - Front-end servers maintain approximate routing tables updated via gossip
>
> I'd lean toward **Option 2** — it gives us strong consistency for sandbox state (we can't have two invocations sent to the same sandbox simultaneously, since Lambda runs **one invocation per sandbox at a time**). The partitioning on `function_id` naturally groups all sandboxes for a function on the same registry shard."

#### The Cold Path — Worker Selection

> "When no warm sandbox exists, we need to pick a worker host for a new microVM. The placement algorithm must balance:
>
> 1. **Code locality**: Prefer a worker that already has the function's deployment package cached. Downloading code is the single largest cold-start contributor for large packages.
>
> 2. **Resource availability**: The worker must have enough free memory and CPU for the new sandbox's configured memory (128 MB to 10,240 MB).
>
> 3. **Multi-tenant packing**: We want to pack sandboxes from different customers onto the same host for utilization, but we need to be careful about **noisy neighbors** — one function consuming all CPU or network can affect others.
>
> 4. **Blast radius**: We should spread a single customer's sandboxes across multiple workers (and ideally multiple failure domains) so that a worker failure doesn't take out all their concurrent executions.
>
> ```
> Placement Algorithm (simplified):
>
>   Input: function_id, memory_required
>
>   1. Check: workers with cached code for function_id
>      -> filter by: has enough free memory
>      -> sort by: least loaded (best packing)
>      -> if found: SELECT THIS WORKER (code-warm cold start)
>
>   2. Else: any worker with enough free memory
>      -> sort by: best packing + failure domain diversity
>      -> SELECT THIS WORKER (full cold start: download code + boot VM)
>
>   3. If no worker has capacity:
>      -> THROTTLE the invocation (429 TooManyRequestsException)
>      -> signal fleet auto-scaler to add capacity
> ```"

**Interviewer:**
What about the scaling behavior? Lambda advertises that a function can go from 0 to thousands of concurrent executions. How does that work?

**Candidate:**

> "Lambda's scaling is governed by concurrency limits and scaling rates. Let me lay out the verified numbers:
>
> **Account-level concurrency** [Source: AWS docs — verified]:
> - Default: **1,000 concurrent executions** per region (soft limit, can be increased to tens of thousands)
> - 100 units always reserved for unreserved functions
> - Customers can request increases
>
> **Scaling rate** [Source: AWS docs — verified]:
> - **1,000 new execution environments per 10 seconds** per function
> - This means a function can scale from 0 to 1,000 concurrent sandboxes in 10 seconds
> - After 10 seconds, it can reach 2,000; after 20 seconds, 3,000; etc.
> - Each function scales independently
>
> **RPS limit** [Source: AWS docs — verified]:
> - **10x the concurrency quota** — so with 1,000 concurrency, you get 10,000 RPS max
> - This prevents a short-duration function from consuming quota disproportionately
>
> **Concurrency formula** [Source: AWS docs — verified]:
> ```
> Concurrency = Requests_per_second × Average_duration_in_seconds
>
> Example: 1000 RPS × 200ms = 200 concurrent executions
> Example: 1000 RPS × 2s   = 2000 concurrent executions
> ```
>
> **Reserved concurrency** [Source: AWS docs — verified]:
> Guarantees a portion of account concurrency for a specific function:
> - Function A: reserved = 500 → guaranteed 500, capped at 500
> - Function B: reserved = 300 → guaranteed 300, capped at 300
> - Remaining functions share the unreserved pool (200 from our 1000 total)
> - No additional cost for reserved concurrency
>
> **Provisioned concurrency** [Source: AWS docs — verified]:
> Pre-initializes environments:
> - Allocation rate: up to 6,000 per minute per function
> - Costs money even when idle
> - Eliminates cold starts up to the provisioned count
>
> **Why the scaling rate matters:**
> If a customer suddenly gets 10,000 concurrent requests and has 1,000 account concurrency:
> - At T=0: 0 sandboxes exist
> - At T=10s: 1,000 sandboxes created (all are cold starts)
> - Requests beyond 1,000 are **throttled** (429 response)
> - Customer must request a concurrency increase for more
>
> The scaling rate of 1,000 per 10 seconds is a **safety mechanism** — it prevents a flood of cold starts from overwhelming the worker fleet."

#### Sandbox Eviction

> "Keeping sandboxes alive forever would waste fleet resources. Lambda must evict sandboxes to reclaim memory for other functions. The eviction policy is likely:
>
> 1. **Idle timeout**: If a sandbox hasn't been invoked for N minutes, evict it [INFERRED — ~5-15 minutes based on community observations]
> 2. **LRU eviction**: When a worker needs memory for a new sandbox, evict the least recently used sandbox
> 3. **Periodic rotation**: Lambda recycles environments periodically (few hours) for security and updates [Source: AWS docs mention environments are not kept indefinitely]
> 4. **Fleet-level optimization**: Lambda globally optimizes which sandboxes to keep based on invocation patterns — functions that are invoked every 30 seconds are more valuable to keep warm than functions invoked once a day [INFERRED]"

> *For the full deep dive on worker fleet management and placement, see [worker-fleet-and-placement.md](worker-fleet-and-placement.md).*

#### Architecture Update After Phase 6

> | | Before (Phase 4) | After (Phase 6) |
> |---|---|---|
> | **Isolation** | Firecracker microVMs | Firecracker + jailer + seccomp (unchanged) |
> | **Placement** | ~~Centralized, naive~~ | **Partitioned sandbox registry, code-affinity routing, LRU eviction** |
> | **Scaling** | ~~Undefined~~ | **1,000 envs per 10s per function; reserved + provisioned concurrency** |
> | **Cold start** | Sandbox reuse, provisioned, SnapStart | + code-affinity placement reduces full cold starts |

---

### L5 vs L6 vs L7 — Phase 6 Rubric

| Aspect | L5 (SDE-2) | L6 (SDE-3) | L7 (Principal) |
|---|---|---|---|
| **Placement** | "Route to a worker with capacity" | Explains warm path (sandbox registry), cold path (code-affinity), and throttle path; discusses registry partitioning | Discusses placement optimization (bin packing vs spread), blast radius isolation, cell-based architecture for worker fleet |
| **Scaling** | "Lambda auto-scales" | Knows the specific scaling rate (1,000/10s), explains reserved vs provisioned concurrency with use cases | Discusses fleet-level auto-scaling (adding/removing worker hosts), capacity planning, how Lambda handles regional traffic shifts |
| **Eviction** | "Kill idle sandboxes" | Explains LRU eviction, idle timeout, periodic rotation | Discusses cost of keeping sandboxes warm (memory cost) vs cold start cost, optimal keep-alive policy as a function of invocation frequency |

---

## PHASE 7: Deep Dive — Event Source Mappings & Async Invocation (~8 min)

**Interviewer:**
Good fleet design. Now let's talk about the three invocation models. You mentioned synchronous, asynchronous, and event source mappings. The first is straightforward — request/response. Walk me through the other two.

**Candidate:**

> "Lambda has three distinct invocation models, and each has very different architectural implications:
>
> ```
> 1. SYNCHRONOUS:  Client waits for response
>    Client ---Invoke---> Lambda ---response---> Client
>    (API Gateway, SDK invoke, ALB)
>
> 2. ASYNCHRONOUS: Client fires and forgets, Lambda retries on failure
>    Client ---Event---> [Lambda Internal Queue] ---Invoke---> Function
>    (S3 events, SNS, CloudWatch Events, IoT)
>
> 3. EVENT SOURCE MAPPING: Lambda polls a source and invokes with batches
>    [SQS/Kinesis/DynamoDB] <---poll--- [Lambda Poller] ---Invoke---> Function
> ```
>
> Let me deep-dive each."

#### Asynchronous Invocation

> "When a client invokes a function asynchronously (e.g., S3 sending an object-created event):
>
> ```
> ASYNC INVOCATION FLOW:
>
>   S3 Event                              Lambda Function
>      |                                       |
>      v                                       |
>   +----------+     +---------+     +--------+--------+
>   | Invoke   | --> | Internal| --> | Invoke | Handle |
>   | (async)  |     | Queue   |     | (sync) | result |
>   |          |     | (SQS-   |     |        |        |
>   | Returns  |     |  like)  |     +--------+--------+
>   | 202 OK   |     |         |          |
>   | instantly |     +---------+          v
>   +----------+         |          Success? --> done
>                        |          Failure? --> retry (up to 2 retries)
>                        |          Still failing? --> DLQ or destination
>                        |
>                        +-- Messages retained for up to 6 hours
> ```
>
> **Key design details:**
> - Lambda returns **202 Accepted** immediately to the caller — not 200. The event is queued.
> - Lambda has an internal queue (likely built on SQS or a similar durable queue) [INFERRED]
> - **Retry behavior**: On failure, Lambda retries **twice** with delays between retries [Source: AWS docs — the default retry count is 2]
> - **Maximum event age**: Events can be configured to expire (0-6 hours) [Source: AWS docs]
> - **Destinations**: On success or failure, Lambda can send the result to another Lambda, SQS, SNS, or EventBridge [Source: AWS docs]
> - **Dead Letter Queue (DLQ)**: Failed events (after retries) can be sent to an SQS queue or SNS topic
> - **Payload limit**: 256 KB for async (vs 6 MB for sync) [Source: AWS docs — verified]
>
> **Why async matters architecturally:**
> The internal queue **decouples** the event producer from the function. If the function is slow or throttled, events queue up rather than failing immediately. This is essential for event-driven architectures where the event source can't retry (e.g., S3 sends one event per object creation)."

#### Event Source Mappings

> "Event source mappings are fundamentally different — Lambda is the **poller**, not the callee.
>
> **Supported sources** [Source: AWS docs — verified]:
> - Amazon SQS
> - Amazon Kinesis Data Streams
> - Amazon DynamoDB Streams
> - Amazon MSK (Managed Kafka)
> - Self-managed Apache Kafka
> - Amazon MQ (ActiveMQ, RabbitMQ)
> - Amazon DocumentDB (with MongoDB compatibility)
>
> ```
> EVENT SOURCE MAPPING ARCHITECTURE:
>
>   +----------+        +-----------+        +----------+
>   | Kinesis  | <-poll-| Lambda    | -invoke>| Lambda   |
>   | Stream   |        | Event     |        | Function |
>   | (shards) |        | Poller    |        |          |
>   +----------+        | Service   |        +----------+
>                       +-----------+
>                       | Manages:           |
>                       | - Polling frequency |
>                       | - Batch size       |
>                       | - Parallelization  |
>                       | - Error handling   |
>                       +--------------------+
> ```
>
> **How the poller works for Kinesis / DynamoDB Streams:**
> - Lambda maintains one poller per shard
> - Each poller reads records, batches them (configurable batch size), and invokes the function synchronously
> - **In-order processing**: Records within a shard are processed in order — Lambda won't read the next batch until the current one succeeds or fails
> - **Parallelization factor**: You can configure up to **10 concurrent batches per shard** [Source: AWS docs], allowing parallel processing within a single shard
> - **Bisect on error**: If a batch fails, Lambda can split it in half and retry each half — binary search for the poison record [Source: AWS docs]
> - **Maximum retry attempts**: Configurable for stream sources [Source: AWS docs — verified]
>
> **How the poller works for SQS:**
> - Lambda long-polls the SQS queue
> - Scales the number of pollers based on queue depth
> - Can process up to **1,000 batches of messages simultaneously** [INFERRED — Lambda scales pollers to match concurrency]
> - SQS provides at-least-once delivery — Lambda functions must be **idempotent** [Source: AWS docs — verified]
>
> **Batching configuration** [Source: AWS docs — verified]:
> - **BatchSize**: Maximum records per batch (varies by source)
> - **MaximumBatchingWindowInSeconds**: Max wait time to accumulate a batch (0-300 seconds)
> - Lambda invokes when ANY condition is met: batch size reached, batching window expires, or **payload reaches 6 MB** (hard limit)
>
> **Provisioned mode** (for SQS and Kafka) [Source: AWS docs — verified]:
> - Allows specifying minimum and maximum pollers
> - SQS: min 2-200, max 2-2,000 pollers
> - Kafka: min 1-200, max 1-2,000 pollers
> - Scales up to 1,000 concurrency per minute, 3x faster than default autoscaling"

**Interviewer:**
What happens if the function keeps failing on a Kinesis record? You said in-order processing — doesn't that mean the entire shard gets stuck?

**Candidate:**

> "Exactly — this is the **poison pill problem** for stream-based event sources. If one bad record causes the function to fail, and Lambda retries the same batch (because in-order processing means it can't skip ahead), the shard is stuck.
>
> **Mitigations:**
>
> 1. **Maximum retry attempts**: Configure a max number of retries. After that, Lambda skips the batch and moves on. Failed records are sent to an **on-failure destination** (SQS or SNS).
>
> 2. **Bisect on batch failure**: Lambda splits the failed batch in half, retries each half. This narrows down which record(s) are causing the failure — like a binary search. Eventually, the poison record is isolated in a batch of 1, retried, and then sent to the failure destination.
>
> 3. **Maximum record age**: Configure a maximum age for records (seconds to days). Records older than this are skipped.
>
> 4. **Function error handling**: The function itself should catch errors per-record and report partial failure (using `ReportBatchItemFailures` for SQS), so only the failed records are retried, not the whole batch.
>
> The combination of bisect + max retries + failure destination makes stream processing resilient to poison pills without permanently blocking the shard."

> *For the full deep dive on invocation models and event source mappings, see [invocation-models.md](invocation-models.md).*

#### Architecture Update After Phase 7

> | | Before (Phase 6) | After (Phase 7) |
> |---|---|---|
> | **Sync invoke** | Request → placement → worker → response | Unchanged |
> | **Async invoke** | Not designed | **Internal queue → retry with backoff → DLQ/destinations** |
> | **Event sources** | Not designed | **Poller service: per-shard for streams, auto-scaling for SQS, batching, bisect on error** |

---

### L5 vs L6 vs L7 — Phase 7 Rubric

| Aspect | L5 (SDE-2) | L6 (SDE-3) | L7 (Principal) |
|---|---|---|---|
| **Invocation models** | "Lambda can be triggered by events" | Clearly distinguishes sync/async/poll models, explains when each is used and why | Discusses the internal architecture of the async queue, how it provides durability guarantees, ordering semantics |
| **Event source mappings** | "Lambda reads from SQS" | Explains poller architecture, batching, parallelization factor, bisect on error, poison pill handling | Discusses poller scaling algorithms, checkpoint management for Kinesis, exactly-once vs at-least-once semantics |
| **Error handling** | "Retry on failure" | Explains retry behavior per model (2 retries async, configurable for streams), DLQ, destinations, bisect | Discusses partial batch failure reporting, idempotency patterns, how error handling design affects downstream systems |

---

## PHASE 8: Deep Dive — VPC Networking (~5 min)

**Interviewer:**
Let's talk about VPC networking. One of Lambda's most requested features was accessing resources inside a customer's VPC — databases, ElastiCache, internal APIs. But the original implementation had a terrible cold start penalty. Walk me through the problem and the fix.

**Candidate:**

> "This is a great example of how a straightforward approach can fail spectacularly at scale.
>
> **The Problem (Original VPC Lambda — pre-2019):**
>
> ```
> ORIGINAL APPROACH:
>
>   Each Lambda execution environment needed a dedicated ENI
>   (Elastic Network Interface) in the customer's VPC.
>
>   Cold Start Steps:
>   1. Boot Firecracker microVM            (~125ms)
>   2. Create a new ENI in customer's VPC  (~8-10 SECONDS)
>   3. Attach ENI to the microVM           (~1-2 seconds)
>   4. Initialize runtime + function       (~variable)
>
>   TOTAL COLD START: 10-15+ seconds for VPC Lambda!
> ```
>
> **Why ENI creation was so slow:**
> Creating an ENI involves: allocating a private IP from the VPC subnet, creating the network interface in EC2, attaching a security group, and creating a cross-account network path. This is a multi-step control plane operation — fast enough for EC2 instances that launch in minutes, but devastating for Lambda functions that should start in milliseconds.
>
> **The additional problem:** Each Lambda execution environment consumed one ENI. With thousands of concurrent executions, customers would **exhaust their VPC's ENI quota** (default 5,000 per region) and their subnet's IP addresses.
>
> **The Fix: Hyperplane ENIs (September 2019)** [Source: AWS docs — verified]
>
> AWS Lambda team worked with the VPC networking team to build **Hyperplane** — a managed network function that creates shared, pre-created ENIs:
>
> ```
> NEW APPROACH (Hyperplane):
>
>   +------------------+        +------------------+
>   | Lambda Worker    |        | Customer VPC     |
>   | Host             |        |                  |
>   | +----------+     |        | +------------+   |
>   | |MicroVM A | ----+--------+>| Hyperplane |   |
>   | +----------+     |  NAT   | |    ENI     |   |
>   | +----------+     | mapping| |            |   |
>   | |MicroVM B | ----+--------+>| (shared)   |   |
>   | +----------+     |        | +------+-----+   |
>   | +----------+     |        |        |         |
>   | |MicroVM C | ----+--------+>       v         |
>   | +----------+     |        | +------------+   |
>   |                  |        | | RDS / EC   |   |
>   +------------------+        | +------------+   |
>                               +------------------+
> ```
>
> **How Hyperplane ENIs work** [Source: AWS docs — verified]:
> - Lambda creates a **Hyperplane ENI** for each unique **subnet + security group combination**
> - Multiple execution environments **share the same ENI** — one ENI supports up to **65,000 connections** (port space)
> - ENIs are **created when the function is first configured for VPC**, not during cold start
> - Once the ENI exists, new execution environments simply **map to the existing ENI** — no new ENI creation needed
> - Functions with the **same subnet and security group** share ENIs, even across different functions [Source: AWS docs — verified]
>
> **Impact on cold start:**
> ```
> BEFORE (2019):  10-15 seconds (ENI creation in cold path)
> AFTER:          Same as non-VPC Lambda (~200ms-1s) — ENI already exists
> ```
>
> **Resource usage improvement:**
> - Before: 1 ENI per concurrent execution (1000 concurrent = 1000 ENIs)
> - After: 1 Hyperplane ENI per subnet+security-group combo (1000 concurrent = maybe 2-3 ENIs)
>
> **Important behaviors** [Source: AWS docs — verified]:
> - If a function is idle for **14 days**, Lambda reclaims the Hyperplane ENI and sets the function to `Inactive` state. Next invocation goes back through ENI creation (Pending state).
> - Removing VPC configuration takes **up to 20 minutes** as Lambda deletes the attached ENI.
> - VPC Lambda functions **cannot access the public internet by default**. They need a **NAT Gateway** in the VPC for outbound internet access.
> - Lambda functions use the execution role's permissions: needs `ec2:CreateNetworkInterface`, `ec2:DescribeNetworkInterfaces`, `ec2:DeleteNetworkInterface` (from `AWSLambdaVPCAccessExecutionRole` managed policy) [Source: AWS docs — verified]."

> *For the full deep dive on VPC networking, see [vpc-networking.md](vpc-networking.md).*

---

### L5 vs L6 vs L7 — Phase 8 Rubric

| Aspect | L5 (SDE-2) | L6 (SDE-3) | L7 (Principal) |
|---|---|---|---|
| **VPC problem** | "VPC Lambda is slow" | Explains the original ENI-per-execution model, quantifies the 10-15s cold start, explains why ENI creation is slow | Discusses IP exhaustion, ENI quota limits, the cross-account networking challenge, how the original design was a correct v1 that didn't anticipate scale |
| **Hyperplane solution** | "They fixed it" | Explains shared ENI with 65K connection capacity, pre-creation at config time, subnet+SG sharing | Discusses Hyperplane as a general AWS networking primitive (also used by EFS, RDS Proxy), the NAT mapping layer, IPv6 implications |
| **Operational concerns** | Not discussed | Mentions 14-day idle reclamation, NAT Gateway requirement for internet | Discusses monitoring ENI utilization, security group rule propagation latency, how to architect VPC Lambda for high-throughput use cases |

---

## PHASE 9: Deep Dive — Layers, Extensions & Observability (~5 min)

**Candidate:**

> "Let me cover three features that are important for Lambda's production use: layers, extensions, and observability."

#### Lambda Layers

> "Layers are a mechanism to share code and dependencies across functions [Source: AWS docs]:
>
> ```
> Without Layers:                    With Layers:
>
> Function A: 50MB                  Function A: 5MB code
>   - 5MB my code                     + Layer 1: 20MB (numpy)
>   - 20MB numpy                      + Layer 2: 15MB (pandas)
>   - 15MB pandas
>   - 10MB boto3                   Function B: 3MB code
>                                     + Layer 1: 20MB (numpy)  <-- SHARED
> Function B: 35MB                    + Layer 2: 15MB (pandas) <-- SHARED
>   - 3MB my code
>   - 20MB numpy
>   - 15MB pandas                  Total deployed: 5+3+20+15 = 43MB (not 85MB)
> ```
>
> **How layers work:**
> - A layer is a zip archive containing libraries, custom runtimes, or other dependencies
> - At cold start, layers are **extracted into `/opt`** in the execution environment
> - Functions can use up to **5 layers** [Source: AWS docs — verified]
> - Total unzipped size (function + all layers) must be ≤ **250 MB** [Source: AWS docs — verified]
> - Layers have **versions** (immutable) and can be shared across accounts via resource policies
> - Layers are cached on worker hosts alongside function code — shared layers benefit from better cache hit rates [INFERRED]"

#### Extensions

> "Extensions allow third-party tools (monitoring, security, governance) to integrate deeply with the Lambda execution environment [Source: AWS docs]:
>
> ```
> Execution Environment:
> +------------------------------------------+
> |                                          |
> |  +-----------+     +------------------+ |
> |  | Lambda    |     | Extension 1      | |
> |  | Runtime   |     | (e.g., Datadog)  | |
> |  | (Node.js) |     |                  | |
> |  +-----+-----+     +--------+---------+ |
> |        |                    |            |
> |  +-----v--------------------v---------+ |
> |  |        Extensions API              | |
> |  |        Telemetry API               | |
> |  +------------------------------------+ |
> |                                          |
> +------------------------------------------+
> ```
>
> **Two types:**
> - **Internal extensions**: Run as part of the runtime process (e.g., the AWS X-Ray SDK)
> - **External extensions**: Run as **separate processes** alongside the function runtime. They receive lifecycle events (Init, Invoke, Shutdown) from the Extensions API.
>
> **The extension lifecycle** [Source: AWS docs — verified]:
> 1. Extensions start during the Init phase (Extension init)
> 2. They receive Invoke events alongside the function
> 3. They receive a Shutdown event for cleanup (2,000ms window for external extensions)
> 4. Extensions can use the **Telemetry API** to receive logs, metrics, and traces
>
> **Why this matters architecturally**: Extensions run **inside the microVM**, so they don't break the isolation boundary. A monitoring extension from Datadog runs in the same sandbox as the function — it can't access other tenants' data."

#### Observability

> "For a platform running millions of concurrent functions, observability is critical:
>
> **Built-in metrics** (emitted to CloudWatch) [Source: AWS docs]:
> - `Invocations` — total invocations (successful + failed)
> - `Errors` — invocations that returned an error
> - `Throttles` — invocations throttled due to concurrency limits
> - `Duration` — function execution time (p50, p90, p99)
> - `ConcurrentExecutions` — concurrent executions at a point in time
> - `IteratorAge` — for stream-based event sources, the age of the last record (measures processing lag)
>
> **Logs**: Function output goes to CloudWatch Logs. Each invocation gets a `START`, `END`, and `REPORT` log line with request ID, duration, billed duration, memory used, and init duration (for cold starts) [Source: AWS docs — verified].
>
> **Tracing**: AWS X-Ray integration provides distributed tracing across Lambda → DynamoDB → SQS → etc.
>
> **Key operational signals for the Lambda team itself** [INFERRED]:
> - Cold start rate (% of invocations that are cold)
> - Cold start duration (p50, p99)
> - Sandbox utilization (% of time sandboxes are actively serving vs idle)
> - Worker host utilization (CPU, memory, network)
> - Throttle rate (are we under-provisioned?)
> - Code cache hit rate (are we downloading code too often?)"

---

## PHASE 10: Full Architecture & Wrap-Up (~5 min)

**Interviewer:**
We're running low on time. Summarize your design and tell me what keeps you up at night operating this system.

**Candidate:**

> "**Design evolution — how we got here:**
>
> | Component | Started With (Phase 4) | Evolved To | Why |
> |---|---|---|---|
> | **Isolation** | Forked processes (no isolation) | Firecracker microVMs + jailer + seccomp | Untrusted multi-tenant code needs VM-level isolation; Firecracker gives VM security at container speed |
> | **Cold starts** | Not addressed | Sandbox reuse, provisioned concurrency, SnapStart, code-affinity placement | Cold starts are Lambda's #1 customer complaint; each strategy addresses a different component |
> | **Placement** | Centralized routing | Partitioned sandbox registry, code-affinity routing, LRU eviction | Millions of placement decisions/sec need distributed tracking; code locality reduces cold start |
> | **Scaling** | Undefined | 1,000 envs per 10s per function; reserved + provisioned concurrency | Controlled scaling prevents fleet overwhelm; reserved guarantees capacity |
> | **Event models** | Sync only | Sync + async (internal queue + retry) + event source mappings (poller service) | Different triggers have different reliability and ordering requirements |
> | **VPC networking** | Not addressed | Hyperplane ENIs (shared, pre-created, 65K connections) | Original per-sandbox ENI caused 10-15s cold starts; Hyperplane eliminated this |
>
> **Final Architecture:**
>
> ```
>                         +-------------------------------+
>                         |          Clients              |
>                         | (API GW, SDK, S3 events,     |
>                         |  SQS, Kinesis, SNS, etc.)    |
>                         +------+-------+-------+-------+
>                                |       |       |
>                    +-----------+  +----+  +----+-----------+
>                    |              |       |                 |
>            +-------v-----+  +----v--+  +-v-----------+    |
>            | Sync Invoke |  | Async |  | Event Source |    |
>            | Front-End   |  | Queue |  | Mapping     |    |
>            | (API)       |  |       |  | (Pollers)   |    |
>            +------+------+  +---+---+  +------+------+    |
>                   |             |              |           |
>                   +------+------+--------------+           |
>                          |                                 |
>                   +------v--------+                        |
>                   | Counting /    |    +------------------+|
>                   | Throttling    |    | Control Plane    ||
>                   | Service       |    | (CreateFunction, ||
>                   | (concurrency) |    |  UpdateCode,     ||
>                   +------+--------+    |  Publish, etc.)  ||
>                          |             +------------------+|
>                   +------v--------+                        |
>                   | Placement /   |                        |
>                   | Sandbox       |                        |
>                   | Registry      |                        |
>                   | (warm/cold    |                        |
>                   |  routing)     |                        |
>                   +------+--------+                        |
>                          |                                 |
>          +---------------+-------------------+             |
>          |               |                   |             |
>   +------v------+ +------v------+ +----------v--+         |
>   | Worker Host | | Worker Host | | Worker Host  |         |
>   |             | |             | |              |         |
>   | +--------+  | | +--------+  | | +--------+   |         |
>   | |Firecrk.|  | | |Firecrk.|  | | |Firecrk.|   |         |
>   | |MicroVM |  | | |MicroVM |  | | |MicroVM |   |         |
>   | |[Fn A]  |  | | |[Fn C]  |  | | |[Fn E]  |   |         |
>   | +--------+  | | +--------+  | | +--------+   |         |
>   | +--------+  | | +--------+  | | +--------+   |         |
>   | |Firecrk.|  | | |Firecrk.|  | | |Firecrk.|   |         |
>   | |MicroVM |  | | |MicroVM |  | | |MicroVM |   |         |
>   | |[Fn B]  |  | | |[Fn D]  |  | | |[Fn F]  |   |         |
>   | +--------+  | | +--------+  | | +--------+   |         |
>   |             | |             | |              |         |
>   | Code Cache  | | Code Cache  | | Code Cache   |         |
>   +-------------+ +-------------+ +--------------+         |
>          |               |                |                |
>          +---------------+----------------+                |
>                          |                                 |
>                   +------v--------+                        |
>                   | Function Code |                        |
>                   | Storage (S3)  |                        |
>                   +---------------+                        |
> ```
>
> **What keeps me up at night:**
>
> 1. **Blast radius of a worker host failure.** A single worker host runs sandboxes for potentially hundreds of different customers' functions. If a host crashes, all those concurrent invocations fail simultaneously. Mitigation: limit how many sandboxes for the same function run on the same host (spread), and use health checks to quickly drain unhealthy hosts. But there's always a tension between packing (utilization) and spreading (blast radius).
>
> 2. **Correlated cold start storms.** If a popular function's sandboxes all get evicted simultaneously (e.g., during a fleet update or a host failure), the next burst of invocations triggers hundreds of cold starts at once, all competing for the same code download from S3, the same worker resources. This can cascade — the cold starts take longer because the worker is overloaded, which causes more requests to queue, which triggers more cold starts. Mitigation: stagger sandbox eviction, prioritize code cache retention, rate-limit cold starts per function.
>
> 3. **Noisy neighbor on shared hardware.** Firecracker uses cgroups to limit CPU and memory per microVM, but shared resources like L3 cache, memory bandwidth, and disk I/O are harder to isolate. A function doing heavy memory-bandwidth operations can slow down a co-located function. This is the fundamental tension of multi-tenant compute. Mitigation: performance anomaly detection, automatic migration of noisy tenants, overprovisioning shared resources.
>
> 4. **Security of the isolation boundary.** Firecracker is written in Rust (memory-safe), but the host kernel (which manages KVM) is not. A KVM vulnerability could allow guest-to-host escape. Side-channel attacks (Spectre/Meltdown variants) are an ongoing concern. Mitigation: kernel hardening, microcode updates, Spectre mitigations enabled (at a performance cost), regular Firecracker security audits. The defense-in-depth approach (jailer + seccomp + cgroups + namespaces + VM isolation) means multiple layers must be compromised simultaneously.
>
> 5. **SnapStart snapshot consistency.** If the snapshot cache becomes corrupted or a snapshot is restored from a stale version after a function update, customers could execute the wrong code version. The immutable versioning model (each published version has its own snapshot) mitigates this, but the cache invalidation and consistency mechanics must be bulletproof. A bug here means customers running old code without knowing it.
>
> 6. **Event source mapping lag.** For Kinesis stream processing, the `IteratorAge` metric tells you how far behind the consumer is. If the function is slow or errors frequently, the lag grows, and the customer's real-time pipeline becomes hours behind. The poller scaling must be aggressive enough to catch up, but controlled enough not to overwhelm the function's concurrency. Bisect-on-error and max-record-age are safety valves, but they mean data loss (skipped records).
>
> **Potential extensions:**
> - **Lambda@Edge / CloudFront Functions** — run functions at CDN edge locations for ultra-low latency
> - **Lambda Destinations** — route function results (success/failure) to other services
> - **Lambda Function URLs** — built-in HTTPS endpoints without API Gateway
> - **Lambda Telemetry API** — let extensions subscribe to function logs and metrics
> - **Lambda Response Streaming** — stream response back to client (up to 20 MB soft limit, with streaming payload up to 200 MB) rather than buffering the full response [Source: AWS docs — streaming response payload limit verified as 200 MB]
> - **Graviton (ARM) support** — run functions on Arm-based AWS Graviton processors for better price/performance"

---

## Interviewer's Final Assessment

**Hire Recommendation: Strong Hire (L6 — solid SDE-3)**

| Dimension | Rating | Notes |
|---|---|---|
| **Problem Decomposition** | Exceeds Bar | Clean evolution from process → container → microVM. Separated the invocation models cleanly. |
| **Requirements & Scoping** | Exceeds Bar | Immediately distinguished sync/async/ESM, asked control plane vs data plane, framed cost model as architectural driver. |
| **Scale Estimation** | Meets Bar | Good numbers: 3.8M invocations/sec, 760K concurrent sandboxes, 390 TB RAM. Used to drive placement decisions. |
| **Isolation Design** | Exceeds Bar | Strong Firecracker explanation — boot time, memory overhead, jailer, seccomp, Rust. Correctly explained why containers are insufficient. |
| **Cold Start Analysis** | Exceeds Bar | Broke down into 5 components with times. Covered all mitigation strategies: warm reuse, provisioned, SnapStart with uniqueness concerns. |
| **Placement & Fleet** | Meets Bar | Good sandbox registry design. Code-affinity placement was a strong insight. Could have gone deeper on bin packing algorithms. |
| **Event Source Mappings** | Exceeds Bar | Explained poller architecture, batching, bisect-on-error, poison pill handling. |
| **VPC Networking** | Exceeds Bar | Clear before/after narrative. Quantified the cold start problem (10-15s → ~200ms). Hyperplane ENI sharing explained well. |
| **Operational Maturity** | Exceeds Bar | "What keeps you up at night" was strong — correlated cold start storms, noisy neighbor, security boundary concerns. |
| **Communication** | Exceeds Bar | Drove the conversation, used diagrams and tables, built iteratively. Good use of verified vs inferred annotations. |
| **LP: Dive Deep** | Exceeds Bar | Went deep on Firecracker internals, SnapStart uniqueness, event source mapping error handling. |
| **LP: Invent and Simplify** | Meets Bar | Good use of existing patterns. SnapStart explanation showed understanding of innovation. |
| **LP: Think Big** | Meets Bar | Extensions section showed awareness of broader Lambda ecosystem. |

**What would push this to L7:**
- Deeper discussion of the Firecracker threat model (specific CVE classes, side-channel mitigations)
- Cell-based architecture for the worker fleet (independent failure domains, blast radius isolation)
- Fleet-level capacity planning: how many hosts per region, how to handle traffic shifts across regions
- Cost modeling: $/invocation breakdown, how Firecracker's density enables Lambda's pricing
- Discussing the trade-off between sandbox keep-alive duration and fleet cost — optimal eviction policy as a function of invocation frequency distribution
- Proposing a monitoring/observability architecture for the Lambda service itself (not just customer functions)

---

## Key Differences: L5 vs L6 vs L7 Expectations for This Problem

| Aspect | L5 (SDE-2) | L6 (SDE-3) | L7 (Principal) |
|---|---|---|---|
| **Requirements** | Lists invoke operation, mentions "serverless" | Distinguishes 3 invocation models, frames cost model as driver, explains isolation requirements with specific tradeoffs | Frames requirements around customer use cases (API backend vs stream processing vs cron), discusses pricing model implications on architecture |
| **Isolation** | "Use containers" or "use VMs" | Explains Firecracker with concrete numbers (125ms boot, 5MB overhead), explains why containers are insufficient for multi-tenant | Discusses full defense-in-depth (Firecracker + jailer + seccomp + cgroups), threat model, comparison with gVisor/Kata, side-channel mitigations |
| **Cold starts** | "Lambda has cold starts, they're slow" | Breaks down into 5 components with times, explains all mitigation strategies, discusses SnapStart uniqueness | Discusses optimal cold start budget allocation, cache hierarchy for code, predictive pre-warming, how cold start SLOs are defined and measured |
| **Placement** | "Route to an available worker" | Warm path (sandbox registry) vs cold path (code-affinity), discusses registry scaling | Discusses bin packing optimization, placement constraints (spread vs pack), cell-based fleet architecture, fleet auto-scaling |
| **Scaling** | "Lambda auto-scales" | Knows specific scaling rate (1,000/10s), explains reserved vs provisioned concurrency with formulas | Discusses fleet-level scaling (adding hosts), capacity planning, how burst limits were chosen, customer migration path for limit increases |
| **Event sources** | "Lambda can be triggered by SQS" | Explains poller architecture, batching, bisect, poison pill, ordering guarantees per source | Discusses checkpoint management, exactly-once delivery patterns, poller scaling algorithms, cross-service integration contracts |
| **VPC** | "Lambda can access VPC resources" | Explains before/after Hyperplane, quantifies cold start improvement, ENI sharing model | Discusses Hyperplane as a platform primitive, DNS resolution in VPC Lambda, PrivateLink integration, IPv6 support timeline |
| **Operational** | Mentions monitoring | Identifies specific failure modes (cold start storms, noisy neighbor, security boundary) | Proposes cell-based isolation, automated canary deployments for fleet updates, game day exercises, blast radius measurement |
| **Communication** | Responds to questions | Drives the conversation, iterative build-up, uses verified data | Negotiates scope, proposes phased deep dives, manages time, connects technical decisions to business outcomes |

---

## Verified Numbers Summary

All numbers in this document fall into three categories:

| Category | Notation | Meaning |
|---|---|---|
| **Verified** | `[Source: AWS docs — verified]` or no notation for well-known facts | Confirmed against official AWS documentation during research |
| **Sourced** | `[Source: Firecracker NSDI 2020 paper]` | From the published Firecracker paper (Agache et al., NSDI 2020) |
| **Inferred** | `[INFERRED]` | Reasonable engineering inference not from official documentation |

### Key Verified Lambda Quotas [Source: AWS docs]

| Resource | Limit |
|---|---|
| Memory | 128 MB — 10,240 MB (1 MB increments) |
| Timeout | 900 seconds (15 minutes) |
| Deployment package (zip) | 50 MB zipped, 250 MB unzipped |
| Container image | 10 GB |
| Ephemeral storage (/tmp) | 512 MB — 10,240 MB |
| Layers per function | 5 |
| Sync payload (request) | 6 MB |
| Sync payload (response) | 6 MB |
| Streamed response | 200 MB |
| Async payload | 256 KB |
| Default concurrency | 1,000 per region |
| Scaling rate | 1,000 environments per 10 seconds per function |
| RPS limit | 10x concurrency quota |
| Init phase timeout | 10 seconds (standard), up to 15 min (provisioned/SnapStart) |
| Shutdown phase | 0ms (no ext), 500ms (internal), 2,000ms (external) |
| File descriptors | 1,024 |
| Processes/threads | 1,024 |
| Environment variables | 4 KB total |
| Provisioned concurrency allocation | 6,000 per minute per function |
| Hyperplane ENI connections | 65,000 per ENI |
| ENI idle reclamation | 14 days |

### Key Firecracker Numbers [Source: NSDI 2020 Paper]

| Property | Value |
|---|---|
| Boot time | ~125ms |
| VMM memory overhead | ~5 MB |
| MicroVM creation rate | 150+ per second per host |
| VMM codebase | ~50K lines of Rust |
| Device model | Minimal: virtio-net, virtio-block, serial console, partial i8042, RTC |

---

*For detailed deep dives on each component, see the companion documents:*
- [Execution Environment Lifecycle](execution-environment-lifecycle.md) — Cold starts, Init/Invoke/Shutdown, sandbox reuse, SnapStart
- [Worker Fleet & Placement](worker-fleet-and-placement.md) — Sandbox registry, code-affinity routing, eviction, fleet scaling
- [Invocation Models](invocation-models.md) — Sync/async/event-source-mapping, retry semantics, error handling
- [VPC Networking](vpc-networking.md) — Hyperplane ENIs, pre-2019 vs post-2019, NAT Gateway, security groups
- [Firecracker Deep Dive](firecracker-deep-dive.md) — MicroVM architecture, jailer, seccomp, snapshots, security model

*End of interview simulation.*
