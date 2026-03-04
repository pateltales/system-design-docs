# System Design Interview Simulation: Design Amazon ECS (Elastic Container Service)

> **Interviewer:** Principal Engineer (L8), Amazon ECS Team
> **Candidate Level:** SDE-3 (L6 -- Senior Software Development Engineer)
> **Duration:** ~60 minutes
> **Date:** February 13, 2026

---

## PHASE 1: Opening & Problem Statement (~2 min)

**Interviewer:**
Hey, welcome. I'm on the ECS team -- I've been here since before Fargate launched. For today's system design round, I'd like you to design a **container orchestration service** -- think Amazon ECS. A system where customers submit container workloads, and we figure out where to run them, keep them running, and handle failures. We're building the control plane and the scheduling infrastructure, not just a Docker wrapper.

I care about how you think through scheduling decisions, multi-tenancy, failure handling, and the tradeoffs between managed infrastructure (Fargate) vs customer-managed infrastructure (EC2). I'll push on your decisions -- that's calibration, not a negative signal.

Take it away.

---

## PHASE 2: Requirements Gathering & Scoping (~5 min)

**Candidate:**
Thanks! Container orchestration is a broad space -- there are fundamentally different approaches (ECS vs Kubernetes vs Nomad), so let me scope down to what we're actually building.

**Functional Requirements -- what operations do we need?**

> "The core abstractions I'd expect:
> - **Task Definition** -- a versioned blueprint that describes one or more containers: images, CPU/memory, ports, volumes, IAM roles, networking mode. This is the 'what to run.'
> - **Task** -- a running instantiation of a task definition. Short-lived or long-lived. This is the 'running instance.'
> - **Service** -- a long-running construct that maintains a desired count of tasks, handles replacements on failure, integrates with load balancers. This is 'keep N copies running forever.'
> - **Cluster** -- the logical grouping of infrastructure (container instances or Fargate capacity) where tasks run.
>
> Key API operations:
> - **RegisterTaskDefinition** -- register a new blueprint
> - **RunTask** -- launch N tasks (one-off, like a batch job)
> - **CreateService / UpdateService** -- create or update a long-running service with desired count
> - **StopTask** -- stop a specific task
> - **ListTasks / DescribeTasks** -- observe state
>
> A few clarifying questions:
> - **Do we need to support both customer-managed instances (EC2) and serverless (Fargate)?**"

**Interviewer:** "Yes, both. EC2 launch type where customers bring their own instances, and Fargate where we provide the compute. The architectural differences are important."

> "- **What about networking modes?** ECS supports `awsvpc`, `bridge`, `host`, and `none`. Should I cover all of them?"

**Interviewer:** "Focus on `awsvpc` as the primary mode -- it's what we recommend -- but mention the tradeoffs with `bridge` and `host`."

> "- **Deployment strategies?** Rolling update vs blue/green?"

**Interviewer:** "Both are important. Cover the rolling update as the default, and blue/green with CodeDeploy as the advanced option."

> "- **Service discovery?** DNS-based or API-based?"

**Interviewer:** "Mention it -- ECS integrates with AWS Cloud Map -- but don't deep-dive unless we have time."

**Non-Functional Requirements:**

> "Now the critical constraints. A container orchestration service has very different NFRs from, say, a storage system:
>
> | Dimension | Requirement | Rationale |
> |---|---|---|
> | **Availability** | 99.99% control plane availability (4 9's) | If the control plane is down, customers can't deploy, scale, or recover from failures. Running tasks should continue even if the control plane is temporarily unavailable. |
> | **Task Launch Latency** | Seconds for Fargate, sub-second for EC2 with warm instances | Developers expect containers to start fast. Fargate has higher cold-start overhead (microVM boot + image pull). |
> | **Scheduling Throughput** | Thousands of task launches per minute per cluster | Large customers run thousands of tasks. ECS supports up to 500 task launches per minute per service. |
> | **Scalability** | 5,000 tasks per service, 5,000 services per cluster, 10,000 clusters per account | These are the published ECS quotas. The system must handle the upper bounds. |
> | **Consistency** | Eventual consistency for task state, strong consistency for scheduling decisions | A task must never be double-scheduled on two instances. But DescribeTasks can lag behind real-time state by seconds. |
> | **Fault Tolerance** | Tasks survive control plane outages; service scheduler replaces failed tasks within minutes | The data plane (running containers) must be decoupled from the control plane. |
> | **Multi-Tenancy** | Millions of AWS accounts sharing the control plane | One customer's scheduling storm must not starve others. |
> | **Security / Isolation** | Task-level isolation (especially Fargate), IAM-based authorization | Fargate tasks from different customers must be fully isolated -- no shared kernel, no shared hardware (or at minimum, strong VM-level isolation). |

**Interviewer:**
You mentioned the control plane vs data plane distinction. Can you elaborate on why that matters?

**Candidate:**

> "Absolutely -- this is a critical architectural principle. The **control plane** handles scheduling, state management, and API operations. The **data plane** is where containers actually run. They must be decoupled:
>
> - If the control plane goes down, **running tasks must keep running**. A customer's production website shouldn't crash because the ECS API is having a bad day.
> - If a container instance loses connectivity to the control plane, its tasks should continue. The ECS agent on the instance manages the local container lifecycle independently.
> - Conversely, a misbehaving data-plane instance shouldn't bring down the control plane.
>
> This is different from a monolithic scheduler where the scheduler dying kills everything. ECS's agent-based architecture provides this decoupling naturally."

**Interviewer:**
Good. Let's get some scale numbers.

---

### Scale Estimation (~3 min)

**Candidate:**

> "Let me estimate ECS-scale numbers to ground our design."

#### Cluster & Task Scale

> "ECS is used by hundreds of thousands of AWS customers. Let me work with realistic numbers:
>
> - **Total clusters across all accounts**: ~1 million (10^6) [INFERRED -- not officially documented]
> - **Active tasks globally**: tens of millions at any point in time [INFERRED -- not officially documented]
> - **Task launches per day**: hundreds of millions (batch jobs, deployments, auto-scaling events) [INFERRED -- not officially documented]
> - **Per-cluster limits**: up to 5,000 container instances, 5,000 services, 5,000 tasks per service
> - **Task launch rate**: 500 tasks per minute per service (published quota)
> - **Per-account limit**: 10,000 clusters (adjustable)
>
> These numbers tell me the control plane must handle massive state management -- millions of task state transitions per hour."

#### Control Plane Load

> "The control plane needs to handle:
> - **API calls**: DescribeTasks, ListTasks, UpdateService, RunTask -- the read-heavy APIs (Describe/List) dominate
> - **State updates**: Every task goes through state transitions: PROVISIONING -> PENDING -> ACTIVATING -> RUNNING -> DEACTIVATING -> STOPPING -> DEPROVISIONING -> STOPPED. Each transition is an event the control plane must process and persist.
> - **Health checks**: The service scheduler must continuously monitor task health and replace failures.
> - **Scheduling decisions**: For every RunTask or service replacement, pick the best container instance (EC2) or allocate Fargate capacity."

#### Fargate Resource Allocation

> "Fargate tasks specify CPU and memory at the task level:
> - **CPU**: 0.25 vCPU to 16 vCPU
> - **Memory**: 0.5 GB to 120 GB (specific valid combinations exist)
> - Each Fargate task runs in its own isolation boundary (Firecracker microVM)
> - Fargate must maintain a large pool of pre-warmed capacity to meet launch latency targets
>
> The resource allocation problem for Fargate is essentially bin-packing across a fleet of physical hosts, but invisible to the customer."

**Interviewer:**
Good. Those numbers are reasonable. Let's architect this.

---

### L5 vs L6 vs L7 -- Phase 2 Rubric

| Aspect | L5 (SDE-2) | L6 (SDE-3) | L7 (Principal) |
|---|---|---|---|
| **Functional Reqs** | Lists task/service/cluster concepts | Proactively distinguishes Task Definition (blueprint) vs Task (instance) vs Service (desired count), asks about both launch types and deployment strategies | Additionally discusses ECS Anywhere, Capacity Providers as first-class abstractions, and how ECS differs from Kubernetes in its scheduling model |
| **Non-Functional** | Mentions availability and scalability | Quantifies published quotas (5,000 tasks/service, 500 launches/min), explains control plane / data plane decoupling, identifies multi-tenancy concerns | Frames NFRs in terms of blast radius isolation, cell-based architecture, and how control plane availability affects customer SLAs |
| **Scoping** | Accepts problem as given | Drives clarifying questions about networking modes, deployment strategies, Fargate vs EC2 | Negotiates scope based on interview time, proposes covering scheduling first then Fargate isolation as a deep dive |

---

## PHASE 3: API Design (~5 min)

**Candidate:**

> "Before drawing boxes, let me define the key API contracts. ECS has a rich API surface, but the core operations are:"

#### Task Definition Management

> ```
> RegisterTaskDefinition(
>   family: string,                    // e.g., "web-server"
>   containerDefinitions: [{
>     name: string,                    // e.g., "nginx"
>     image: string,                   // e.g., "nginx:latest"
>     cpu: int,                        // CPU units (1024 = 1 vCPU)
>     memory: int,                     // Hard limit in MiB
>     memoryReservation: int,          // Soft limit in MiB
>     portMappings: [{containerPort, hostPort, protocol}],
>     environment: [{name, value}],
>     secrets: [{name, valueFrom}],    // From SSM or Secrets Manager
>     healthCheck: {command, interval, timeout, retries, startPeriod},
>     logConfiguration: {logDriver, options},
>     essential: bool,                 // If true, task stops when this container stops
>   }],
>   networkMode: "awsvpc" | "bridge" | "host" | "none",
>   requiresCompatibilities: ["FARGATE" | "EC2"],
>   cpu: string,                       // Task-level CPU (required for Fargate)
>   memory: string,                    // Task-level memory (required for Fargate)
>   taskRoleArn: string,               // IAM role for task containers
>   executionRoleArn: string,          // IAM role for ECS agent (pull images, push logs)
>   volumes: [{name, host, efsVolumeConfiguration}],
> ) -> TaskDefinition {family: "web-server", revision: 3, taskDefinitionArn: "arn:..."}
> ```
>
> **Key design decisions in this API:**
> - **Two IAM roles**: `taskRoleArn` is what the application code uses to call AWS APIs. `executionRoleArn` is what the ECS infrastructure uses to pull images and write logs. Separation of concerns.
> - **CPU units**: 1024 units = 1 vCPU. Allows fine-grained allocation below 1 core.
> - **Essential containers**: If an essential container exits, the entire task is stopped. Non-essential containers (sidecars) can crash without killing the task.
> - **Up to 10 containers per task definition** (published quota).

#### Running Tasks

> ```
> RunTask(
>   cluster: string,
>   taskDefinition: string,           // family:revision or full ARN
>   count: int,                       // 1-10 per API call
>   launchType: "FARGATE" | "EC2",
>   networkConfiguration: {           // Required for awsvpc
>     awsvpcConfiguration: {
>       subnets: [string],            // Up to 16 subnets
>       securityGroups: [string],     // Up to 5 security groups
>       assignPublicIp: "ENABLED" | "DISABLED"
>     }
>   },
>   placementStrategy: [{type, field}],    // EC2 only
>   placementConstraints: [{type, expression}],  // EC2 only
>   overrides: {containerOverrides: [{name, command, environment, cpu, memory}]},
> ) -> {tasks: [Task], failures: [{arn, reason}]}
> ```
>
> **Why `count` maxes at 10**: This is a published API limit. For launching many tasks, call RunTask in a loop or use a service. The limit prevents a single API call from overwhelming the scheduler.

#### Service Management

> ```
> CreateService(
>   cluster: string,
>   serviceName: string,
>   taskDefinition: string,
>   desiredCount: int,                // How many tasks to keep running (up to 5,000)
>   launchType: "FARGATE" | "EC2",
>   deploymentConfiguration: {
>     minimumHealthyPercent: int,     // e.g., 100 -- never go below desired count
>     maximumPercent: int,            // e.g., 200 -- allow 2x tasks during deploy
>     deploymentCircuitBreaker: {
>       enable: bool,
>       rollback: bool,               // Auto-rollback on deployment failure
>     }
>   },
>   loadBalancers: [{
>     targetGroupArn: string,
>     containerName: string,
>     containerPort: int,
>   }],
>   networkConfiguration: {...},
>   placementStrategy: [{type, field}],
>   serviceRegistries: [{registryArn}],  // Cloud Map integration
>   capacityProviderStrategy: [{capacityProvider, weight, base}],
> ) -> Service
> ```
>
> **The critical parameters are `minimumHealthyPercent` and `maximumPercent`:**
> - `minimumHealthyPercent: 100, maximumPercent: 200` -- zero-downtime rolling deploy. ECS launches new tasks first, then drains old ones. Requires 2x capacity temporarily.
> - `minimumHealthyPercent: 50, maximumPercent: 100` -- allows killing half the tasks before launching new ones. Less safe, but uses no extra capacity.
> - `minimumHealthyPercent: 100, maximumPercent: 100` -- replacement style. Stop one, start one. Slowest but uses no extra capacity."

**Interviewer:**
Good API design. I like that you called out the two IAM role separation. Let's move to architecture.

---

### L5 vs L6 vs L7 -- Phase 3 Rubric

| Aspect | L5 (SDE-2) | L6 (SDE-3) | L7 (Principal) |
|---|---|---|---|
| **API Design** | Lists basic CRUD endpoints | Specifies exact parameters with types, explains design decisions (two IAM roles, essential containers, CPU units) | Additionally discusses API versioning, backward compatibility, idempotency tokens, and how the API evolved over time |
| **Deployment Config** | Mentions rolling update | Explains minimumHealthyPercent/maximumPercent tradeoffs with concrete examples | Discusses deployment state machine, circuit breaker thresholds, interaction with ALB draining, and blue/green lifecycle hooks |
| **Quotas in API** | Does not mention limits | Cites published limits (10 per RunTask, 5,000 per service, 10 containers per task def) | Discusses why limits exist (scheduler protection, blast radius), how throttling works, and quota adjustment process |

---

## PHASE 4: High-Level Architecture (~5 min)

**Candidate:**

> "Let me start with the simplest thing that could work, then find the problems and fix them."

#### Attempt 0: Single Server -- Run a Container Manually

> "The simplest possible design -- one EC2 instance, SSH in, `docker run`:
>
> ```
>     Developer
>       |
>       | SSH
>       v
>   +---------------------+
>   |   Single EC2 Host   |
>   |                     |
>   |   docker run        |
>   |   -d nginx:latest   |
>   |                     |
>   |   Docker Engine     |
>   |   +-------+         |
>   |   | nginx |         |
>   |   +-------+         |
>   +---------------------+
> ```
>
> This works on day one. Developer SSHs in, runs a container, it's alive."

**Interviewer:**
What happens when that instance dies?

**Candidate:**

> "The container is gone. Nobody restarts it. Nobody even knows it died unless someone is watching. And we have no way to run more than one instance of it, or spread across multiple hosts. Zero fault tolerance, zero scalability."

#### Attempt 1: Multiple Hosts -- Now We Need a Scheduler

> "OK, let's add more hosts. Now a developer has 10 EC2 instances and wants to run 5 copies of their web server. The immediate question is: **which hosts get which containers?**
>
> ```
>     Developer
>       |
>       | Which host should I run on?
>       |
>   +---+---+---+---+---+---+---+---+---+---+
>   |H1 |H2 |H3 |H4 |H5 |H6 |H7 |H8 |H9 |H10|
>   +---+---+---+---+---+---+---+---+---+---+
>       ?   ?   ?   ?   ?   ?   ?   ?   ?   ?
> ```
>
> The developer has to manually track which hosts have capacity, which are in which AZ, which are healthy. This is the scheduling problem. Let's introduce a **control plane** that makes these decisions."

#### Attempt 2: Introduce a Control Plane

> "This is the key architectural step. We separate the system into three layers -- matching how AWS describes ECS's architecture:
>
> ```
>                          +------------------------+
>                          |       Customers         |
>                          |  (CLI, SDK, Console)    |
>                          +-----------+------------+
>                                      | ECS API (HTTPS)
>                          +-----------v------------+
>                          |   Provisioning Layer    |
>                          |   (API Front-End)       |
>                          |   Auth, Validation,     |
>                          |   Rate Limiting         |
>                          +-----------+------------+
>                                      |
>                          +-----------v------------+
>                          |   Controller Layer      |
>                          |   (The Brain)           |
>                          |                         |
>                          |   +------------------+  |
>                          |   | Cluster State    |  |
>                          |   | Store            |  |
>                          |   +------------------+  |
>                          |   | Service          |  |
>                          |   | Scheduler        |  |
>                          |   +------------------+  |
>                          |   | Task Placement   |  |
>                          |   | Engine           |  |
>                          |   +------------------+  |
>                          +-----------+------------+
>                                      |
>                    +-----------------+-----------------+
>                    |                                   |
>       +------------v-----------+        +--------------v----------+
>       |   EC2 Capacity Layer   |        |   Fargate Capacity Layer|
>       |                        |        |                         |
>       |   Customer EC2         |        |   AWS-managed compute   |
>       |   instances with       |        |   Firecracker microVMs  |
>       |   ECS Agent            |        |   per task              |
>       |                        |        |                         |
>       |   +------+ +------+   |        |   +------+ +------+    |
>       |   |Agent | |Agent |   |        |   | Task | | Task |    |
>       |   |+----+| |+----+|   |        |   |      | |      |    |
>       |   ||task || ||task ||   |        |   +------+ +------+    |
>       |   |+----+| |+----+|   |        |                         |
>       |   +------+ +------+   |        +--------------------------+
>       +------------------------+
> ```
>
> **Three layers, matching AWS's own description:**
>
> 1. **Provisioning Layer** (API front-end): Handles API requests, authentication, validation. Stateless fleet behind a load balancer.
>
> 2. **Controller Layer** (the brain): This is the core of ECS. It contains:
>    - **Cluster State Store**: Tracks all clusters, container instances, tasks, services, and their current state. This is the source of truth.
>    - **Service Scheduler**: A control loop that continuously compares desired state (service definition) with actual state (running tasks) and takes corrective action.
>    - **Task Placement Engine**: Given a task to launch, decides which container instance (EC2) should host it, based on resource requirements, placement strategies, and constraints.
>
> 3. **Capacity Layer** (where containers run): Two fundamentally different models:
>    - **EC2**: Customer's instances, each running an ECS Agent that communicates with the control plane. The agent pulls task assignments, starts/stops containers via Docker, and reports state back.
>    - **Fargate**: AWS-managed infrastructure. Customer never sees the underlying host. Each task gets its own Firecracker microVM for isolation.
>
> **How a RunTask works in this design:**
> 1. Customer calls `RunTask(cluster, taskDef, count=3)` via the API
> 2. Provisioning layer authenticates (IAM/SigV4), validates parameters
> 3. Controller layer receives the request
> 4. Task Placement Engine evaluates: which container instances in this cluster have enough CPU/memory/ports? Which satisfy the placement constraints?
> 5. For EC2: Assigns tasks to specific instances. The ECS Agent on each instance picks up the assignment and runs `docker create` + `docker start`.
> 6. For Fargate: Allocates a Firecracker microVM, pulls the container image, starts the container.
> 7. Task state transitions: PROVISIONING -> PENDING -> RUNNING
> 8. Customer can observe state via `DescribeTasks`"

**Interviewer:**
Good -- I like the three-layer architecture. But I see several areas to dig into. How does the state store work? What happens when an instance dies? How does the agent communicate with the control plane? Let's start with the control plane and state management.

**Candidate:**

> "Exactly. Three areas to improve:
>
> | Layer | Current (Naive) | Problem |
> |-------|----------------|---------|
> | **State Store** | Unspecified | How do we store millions of task states durably and serve millions of reads/sec? |
> | **Scheduler** | Simple placement | How do we handle placement strategies (spread vs binpack), failures, and replacement? |
> | **Capacity** | Static | How do we auto-scale EC2 capacity? How does Fargate allocate microVMs? |
>
> Let's deep-dive each one."

---

### L5 vs L6 vs L7 -- Phase 4 Rubric

| Aspect | L5 (SDE-2) | L6 (SDE-3) | L7 (Principal) |
|---|---|---|---|
| **Architecture Evolution** | Jumps straight to a distributed system | Starts from single host, identifies scheduling problem, evolves to 3-layer architecture with clear rationale | Additionally discusses why ECS chose this architecture over alternatives (e.g., why not a Kubernetes-like watch-based model), and how the architecture enables independent scaling of each layer |
| **Control Plane Design** | "We need a scheduler" | Identifies three sub-components (state store, service scheduler, placement engine) and their distinct responsibilities | Discusses how the control plane itself is a distributed system -- replicated, partitioned by cluster or region, with leader election for the service scheduler |
| **Data Plane Understanding** | "Containers run on EC2" | Explains the ECS Agent model, how it communicates with control plane, and why running tasks survive control plane outages | Discusses agent heartbeat protocol, how stale agent state is reconciled, and the tradeoff between push vs pull models for task assignment |

---

## PHASE 5: Deep Dive -- Control Plane & State Management (~10 min)

**Interviewer:**
Let's dig into the control plane. You said there's a "cluster state store." What does it store, and how does it scale?

**Candidate:**

> "The state store is the source of truth for everything in ECS. Let me enumerate what it tracks:
>
> **State entities:**
>
> ```
> Cluster:
>   - cluster_id, account_id, region
>   - capacity_provider_strategy
>   - settings, tags
>
> ContainerInstance (EC2 only):
>   - instance_id, cluster_id
>   - registered_resources: {cpu: 4096, memory: 16384, ports: [...], gpus: 2}
>   - remaining_resources: {cpu: 1024, memory: 4096, ...}
>   - status: ACTIVE | DRAINING | DEREGISTERING
>   - agent_connected: bool
>   - running_tasks_count
>   - last_heartbeat_timestamp
>
> TaskDefinition:
>   - family, revision, arn
>   - container_definitions, volumes, network_mode
>   - cpu, memory requirements
>   - status: ACTIVE | INACTIVE
>
> Task:
>   - task_arn, cluster_id, task_definition_arn
>   - last_status: PROVISIONING | PENDING | ACTIVATING | RUNNING |
>                  DEACTIVATING | STOPPING | DEPROVISIONING | STOPPED
>   - desired_status: RUNNING | STOPPED
>   - container_instance_id (EC2) or fargate_allocation (Fargate)
>   - started_at, stopped_at, stop_code, stop_reason
>   - containers: [{name, last_status, exit_code, health_status}]
>   - connectivity_status, connectivity_at
>
> Service:
>   - service_name, cluster_id, task_definition
>   - desired_count, running_count, pending_count
>   - deployment_configuration
>   - load_balancers, service_registries
>   - deployments: [{id, status, task_definition, desired_count, running_count}]
> ```
>
> **Scale of the state store:**
> - Millions of clusters across all accounts
> - Tens of millions of active tasks (each with state transitions)
> - Heavy read traffic: DescribeTasks, ListTasks, ListServices are called constantly by monitoring, CI/CD, auto-scaling
> - Heavy write traffic: Every task state transition, every agent heartbeat, every scaling event
>
> This is a **high-throughput, low-latency state store** with both point lookups (describe a specific task) and range queries (list all tasks in a cluster)."

**Interviewer:**
What technology choices would you make for the state store?

**Candidate:**

> "The state store needs to be:
> 1. **Highly available** -- if it's down, the entire control plane is down
> 2. **Durable** -- losing task state means losing track of what's running
> 3. **Low latency** -- scheduling decisions depend on reading current state
> 4. **Horizontally scalable** -- millions of clusters, billions of state transitions per day
>
> **My choice: DynamoDB (or a similar partitioned key-value store)**
>
> [INFERRED -- not officially documented] AWS services commonly use DynamoDB internally. For ECS:
>
> ```
> Partition Strategy:
>   Tasks table:
>     Partition key: cluster_id
>     Sort key: task_arn
>     -> All tasks in a cluster are co-located for efficient listing
>     -> But hot clusters (5,000 tasks) might create hot partitions
>
>   ContainerInstances table:
>     Partition key: cluster_id
>     Sort key: instance_id
>
>   Services table:
>     Partition key: cluster_id
>     Sort key: service_name
> ```
>
> **Why DynamoDB makes sense:**
> - Single-digit millisecond reads/writes at any scale
> - Auto-scales throughput
> - Multi-AZ replication for durability and availability
> - Point lookups AND range queries (cluster_id + sort key)
>
> **Potential issues:**
> - **Hot partitions**: A cluster with 5,000 tasks and frequent DescribeTasks calls could create a hot partition. Mitigation: DynamoDB's adaptive capacity distributes throughput to hot partitions. Also, caching at the API layer.
> - **Consistency**: DynamoDB offers strong consistency reads, which is important when the scheduler reads remaining resources before placing a task. Stale reads could lead to over-scheduling."

**Interviewer:**
How does the ECS Agent communicate with the control plane?

**Candidate:**

> "The ECS Agent runs on every EC2 container instance. It's the bridge between the control plane and the data plane.
>
> **Communication model:**
>
> [INFERRED -- not officially documented] The agent likely uses a **long-polling or WebSocket-based** connection to the control plane:
>
> ```
> ECS Agent <----> Control Plane Communication:
>
> 1. HEARTBEAT (Agent -> Control Plane, periodic):
>    - "I'm alive"
>    - Current resource utilization (CPU, memory remaining)
>    - Running container statuses
>    - Every ~30 seconds [INFERRED]
>
> 2. TASK ASSIGNMENT (Control Plane -> Agent, event-driven):
>    - "Run this task definition on your instance"
>    - Agent pulls the image, creates containers, starts them
>
> 3. STATE REPORT (Agent -> Control Plane, event-driven):
>    - "Task X transitioned from PENDING to RUNNING"
>    - "Container Y exited with code 137 (OOMKilled)"
>    - "Container Z health check failed"
>
> 4. TASK STOP (Control Plane -> Agent, event-driven):
>    - "Stop task X with reason: service scaling down"
>    - Agent sends SIGTERM, waits stopTimeout, then SIGKILL
> ```
>
> **Why long-polling / persistent connection?**
> - Pure polling (agent polls every N seconds) adds latency to task launches and wastes API calls
> - Pure push (control plane pushes to agents) requires the control plane to track agent endpoints -- harder to scale
> - Long-polling is a good middle ground: agent opens a connection, control plane responds immediately when there's work, or after a timeout
>
> **What happens when the agent loses connectivity?**
> - Running tasks continue running -- the Docker daemon doesn't care about the agent
> - The agent keeps trying to reconnect
> - The control plane notices missing heartbeats and marks the instance as `agent_connected: false`
> - After a timeout, the control plane considers the instance unhealthy and may reschedule tasks
> - But it doesn't immediately kill tasks -- the instance might just have transient network issues
>
> **This is the key to control plane / data plane decoupling**: tasks survive agent disconnections and even control plane outages."

**Interviewer:**
What about the service scheduler -- the control loop that maintains desired count?

**Candidate:**

> "The service scheduler is arguably the most important component in ECS. It's a **reconciliation loop**:
>
> ```
> Service Scheduler Loop (runs continuously per service):
>
>   while true:
>     actual_count = count(tasks where service_id = S and status = RUNNING)
>     desired_count = service.desired_count
>
>     if actual_count < desired_count:
>       // Need more tasks -- a task failed, or desired count increased
>       deficit = desired_count - actual_count
>       for i in range(deficit):
>         place_and_launch_task(service.task_definition, service.cluster)
>
>     if actual_count > desired_count:
>       // Too many tasks -- desired count decreased, or deployment is draining
>       surplus = actual_count - desired_count
>       select surplus tasks using placement strategy (prefer least loaded AZ)
>       stop selected tasks
>
>     if deployment_in_progress:
>       manage_rolling_update()  // launch new, drain old, respect deployment config
>
>     sleep(reconciliation_interval)  // likely a few seconds [INFERRED]
> ```
>
> **Critical behaviors:**
>
> 1. **Replacement on failure**: If a task crashes, the RUNNING count drops below desired count. On the next reconciliation, the scheduler launches a replacement. This is reactive, not instant -- there's a detection delay plus scheduling time.
>
> 2. **AZ-balanced placement**: By default, services use `spread` strategy across availability zones. If you have `desiredCount: 6` across 3 AZs, you get 2 tasks per AZ. If an AZ goes down (losing 2 tasks), the scheduler places 2 replacement tasks in the remaining AZs (3+3 distribution) until the failed AZ recovers.
>
> 3. **Rate limiting**: The scheduler doesn't try to launch all deficit tasks at once. ECS has a launch rate of 500 tasks per minute per service. This prevents thundering herd on the placement engine.
>
> 4. **Steady state optimization**: Even when nothing is failing, the scheduler periodically checks that tasks are well-distributed. If an AZ imbalance develops (e.g., after a scaling event), it may rebalance by launching tasks in the under-represented AZ and draining from the over-represented one."

> *For the full deep dive on control plane internals, see the architecture notes below.*

#### Architecture Update After Phase 5

> "Our control plane has been fleshed out:
>
> | | Before (Phase 4) | After (Phase 5) |
> |---|---|---|
> | **State Store** | Unspecified | DynamoDB-like partitioned KV store, keyed by cluster_id |
> | **Agent Comm** | Unspecified | Long-polling / persistent connections, heartbeat + event-driven state reports |
> | **Service Scheduler** | Simple placement | Reconciliation loop: continuously compares desired vs actual, rate-limited task launches, AZ-balanced |
> | **Scheduler** | Simple placement | *(still need to detail placement strategies -- next phase)* |

---

### L5 vs L6 vs L7 -- Phase 5 Rubric

| Aspect | L5 (SDE-2) | L6 (SDE-3) | L7 (Principal) |
|---|---|---|---|
| **State Store** | "Use a database" | Proposes DynamoDB with partition strategy (cluster_id), discusses hot partitions and consistency needs | Discusses cross-region state replication, how state is sharded per cell for blast radius, and the tradeoff between consistency and scheduling throughput |
| **Agent Communication** | "Agent talks to the control plane" | Explains heartbeat + event-driven model, discusses what happens during connectivity loss, explains why tasks survive outages | Discusses the exact protocol (TACS -- Task and Container State, if known), message ordering guarantees, agent version skew handling |
| **Service Scheduler** | "Replace failed tasks" | Describes the reconciliation loop with pseudocode, explains AZ-balanced replacement, mentions rate limiting | Discusses scheduler scalability (how many services can one scheduler instance handle), leader election for scheduler partitions, and how to prevent scheduler storms |

---

## PHASE 6: Deep Dive -- Task Placement Strategies & Constraints (~8 min)

**Interviewer:**
You mentioned placement strategies earlier. Walk me through how ECS decides where to put a task.

**Candidate:**

> "Task placement is the core scheduling decision for EC2 launch type. When a task needs to be launched, the placement engine follows a **multi-step filtering and scoring pipeline**:
>
> ```
> Task Placement Pipeline:
>
> Step 1: RESOURCE FILTERING
>   Filter instances that have enough:
>   - CPU (remaining_cpu >= task_cpu_requirement)
>   - Memory (remaining_memory >= task_memory_requirement)
>   - Ports (required ports are available)
>   - GPU (if task requires GPU)
>   -> Eliminates instances that physically cannot run the task
>
> Step 2: CONSTRAINT FILTERING
>   Apply placement constraints:
>   - distinctInstance: no two tasks from same group on same instance
>   - memberOf: instance must match an expression
>     (e.g., "attribute:ecs.instance-type == c5.xlarge")
>     (e.g., "attribute:ecs.availability-zone == us-east-1a")
>   -> Constraints are HARD requirements -- they eliminate instances
>
> Step 3: STRATEGY SCORING
>   Apply placement strategies to rank remaining instances:
>   - spread: prefer instances that minimize concentration
>   - binpack: prefer instances with least remaining resources
>   - random: pick randomly
>   -> Strategies are BEST EFFORT -- ECS will still place if optimal isn't available
>
> Step 4: SELECT
>   Pick the top-ranked instance
> ```
>
> **Key insight: Constraints are hard filters, strategies are soft preferences.** A constraint violation prevents placement entirely. A strategy violation just means sub-optimal placement."

**Interviewer:**
Walk me through each strategy with a concrete example.

**Candidate:**

> "Let's say I have 4 instances across 2 AZs, and I need to place 4 tasks for a web service:
>
> ```
> Cluster State:
>   Instance A (us-east-1a): 2 vCPU free, 4 GB free, running 1 task
>   Instance B (us-east-1a): 3 vCPU free, 6 GB free, running 0 tasks
>   Instance C (us-east-1b): 1 vCPU free, 2 GB free, running 3 tasks
>   Instance D (us-east-1b): 4 vCPU free, 8 GB free, running 0 tasks
>
> Task requires: 1 vCPU, 1 GB memory
> ```
>
> **Strategy 1: `spread` on `attribute:ecs.availability-zone` (DEFAULT)**
>
> ```
> Goal: Distribute evenly across AZs for high availability
>
> Current distribution: AZ-a has 1 task, AZ-b has 3 tasks
> Place task 1 -> AZ-a (Instance B, 0 tasks) -- AZ-a is under-represented
> Place task 2 -> AZ-a (Instance A, 1 task) -- still under-represented
> Place task 3 -> AZ-b (Instance D, 0 tasks) -- now balanced
> Place task 4 -> AZ-a (Instance B) -- spread within AZ
>
> Result: A:2, B:2, C:3, D:1 -- balanced across AZs
> ```
>
> **Why this is the default for services:** If an AZ fails, you lose at most half your tasks. With `binpack`, all tasks might end up in one AZ, and you lose everything.
>
> **Strategy 2: `binpack` on `memory`**
>
> ```
> Goal: Pack tasks densely to minimize instance count (cost optimization)
>
> Prefer instances with LEAST remaining memory (most full):
> Instance C: 2 GB free (most packed) -- place here first
> Instance A: 4 GB free -- place next
> Instance B: 6 GB free -- then here
> Instance D: 8 GB free (most empty) -- last
>
> Result: C:4, A:2, B:0, D:0 -- packed onto fewest instances
> ```
>
> **Why binpack is good for cost:** If Instance B and D end up empty, you can terminate them (via auto-scaling) and save money. You're using fewer instances to run the same workload.
>
> **Why binpack is DANGEROUS for availability:** All tasks concentrated on fewer instances. If Instance C dies, you lose 4 tasks at once.
>
> **Strategy 3: `random`**
>
> ```
> Goal: No preference -- pick randomly from eligible instances
>
> Simple, low-overhead. Useful when you don't care about placement
> and just want fast scheduling.
> ```
>
> **Combining strategies (most powerful):**
>
> ```json
> \"placementStrategy\": [
>   {\"type\": \"spread\", \"field\": \"attribute:ecs.availability-zone\"},
>   {\"type\": \"binpack\", \"field\": \"memory\"}
> ]
> ```
>
> This means: **First** spread across AZs (for availability), **then within each AZ**, binpack by memory (for cost efficiency). This is the best-of-both-worlds approach -- AZ-level resilience with cost optimization within each AZ."

**Interviewer:**
What about placement constraints? Give me a real-world example.

**Candidate:**

> "Constraints are hard requirements. Two types:
>
> **1. `distinctInstance`:**
> No two tasks from the same task group can run on the same instance.
>
> ```
> Use case: Running a distributed database (like Redis cluster) where
> each node must be on a separate host for fault tolerance.
>
> If Instance A already runs Redis-node-1, Redis-node-2 cannot be
> placed on Instance A.
> ```
>
> **2. `memberOf` (expression-based):**
> Instance must match a boolean expression using instance attributes.
>
> ```
> Examples:
>   # Only place on GPU instances
>   \"expression\": \"attribute:ecs.instance-type =~ g4dn.*\"
>
>   # Only place in a specific AZ
>   \"expression\": \"attribute:ecs.availability-zone == us-east-1a\"
>
>   # Only place on instances with custom attribute 'environment=production'
>   \"expression\": \"attribute:environment == production\"
>
>   # Only place on instances NOT already running this task group
>   \"expression\": \"task:group != service:my-service\"
> ```
>
> **Key tradeoff with constraints: They reduce the solution space.** The more constraints you add, the fewer instances are eligible. If no instance satisfies all constraints, the task stays in PENDING state and the scheduler keeps retrying. This is the most common cause of tasks stuck in PENDING -- overly restrictive constraints plus insufficient capacity."

**Interviewer:**
How does this differ from Kubernetes scheduling?

**Candidate:**

> "Good question -- important to be explicit about the differences:
>
> | Aspect | ECS Task Placement | Kubernetes Scheduling |
> |---|---|---|
> | **Model** | Filter-then-score pipeline: constraints (hard) then strategies (soft) | Predicate filtering (hard) then priority scoring (soft) -- similar conceptual model |
> | **Configuration** | Placement strategies and constraints are per-service or per-RunTask call | Node affinity, pod affinity, taints/tolerations, topology spread constraints are per-pod |
> | **Strategies** | 3 built-in: spread, binpack, random | Extensible via scheduler plugins; dozens of built-in scoring functions |
> | **Preemption** | ECS does not preempt running tasks to make room | K8s supports pod preemption based on priority classes |
> | **Custom Schedulers** | Not supported -- ECS has one scheduler | K8s supports multiple custom schedulers |
> | **Complexity** | Simple and opinionated -- 3 strategies cover 90% of use cases | Highly configurable -- powerful but complex |
>
> ECS deliberately chose simplicity over extensibility. Three strategies cover the vast majority of use cases, and the lack of preemption avoids the complexity of priority-based scheduling."

#### Architecture Update After Phase 6

> | | Before (Phase 5) | After (Phase 6) |
> |---|---|---|
> | **Task Placement** | Simple placement | **4-step pipeline: resource filter -> constraint filter -> strategy score -> select** |
> | **Strategies** | Not detailed | **spread (HA), binpack (cost), random; combinable in priority order** |
> | **Constraints** | Not detailed | **distinctInstance, memberOf (expression-based); hard filters that reduce solution space** |

---

### L5 vs L6 vs L7 -- Phase 6 Rubric

| Aspect | L5 (SDE-2) | L6 (SDE-3) | L7 (Principal) |
|---|---|---|---|
| **Placement Model** | "Spread tasks across instances" | Explains the full 4-step pipeline (resource filter, constraint filter, strategy score, select) with concrete examples | Discusses how the placement engine scales, whether it's stateless (re-reads state each time) or cached, and how concurrent placements avoid double-booking |
| **Strategy Tradeoffs** | Knows spread and binpack exist | Demonstrates with concrete cluster state example, explains why spread is default for services, shows combined strategy approach | Analyzes the interaction between placement decisions and capacity provider scaling signals -- how binpack affects scale-in decisions |
| **Constraints** | Mentions "requirements" | Explains distinctInstance and memberOf with real use cases, identifies stuck-in-PENDING as the failure mode | Discusses constraint satisfaction as an NP-hard problem at scale, how ECS limits the solution space to keep scheduling fast |

---

## PHASE 7: Deep Dive -- Fargate Architecture & Isolation (~8 min)

**Interviewer:**
Let's talk about Fargate. You mentioned Firecracker microVMs. Walk me through how Fargate actually works.

**Candidate:**

> "Fargate is architecturally very different from EC2 launch type. With EC2, the customer manages instances and ECS just schedules tasks onto them. With Fargate, **AWS manages everything below the container** -- the customer never sees a host, an instance ID, or a VM.
>
> **The Fargate execution model:**
>
> ```
> Customer's Perspective:
>   RunTask(launchType=FARGATE, taskDef={cpu: 1024, memory: 2048, ...})
>   -> Task starts in seconds
>   -> Customer sees a task with an ENI and an IP address
>   -> Customer has no concept of the underlying host
>
> AWS's Perspective (behind the scenes):
>
> 1. ALLOCATION:
>    ECS Fargate control plane receives the task request
>    -> Find a physical host with enough capacity for the requested
>       CPU/memory
>    -> Allocate a Firecracker microVM on that host
>
> 2. MICROVM BOOT:
>    -> Boot a minimal Linux kernel inside the Firecracker microVM
>    -> ~125ms boot time [Firecracker's published benchmark]
>    -> The microVM has exactly the CPU/memory the task requested
>    -> No more, no less -- hard resource boundaries
>
> 3. NETWORKING:
>    -> Attach an ENI (Elastic Network Interface) to the microVM
>    -> The task gets its own private IP address in the customer's VPC
>    -> This is the awsvpc networking mode -- mandatory for Fargate
>
> 4. IMAGE PULL:
>    -> Inside the microVM, pull the container image from ECR or
>       DockerHub
>    -> This is often the slowest part (can be seconds to minutes
>       for large images)
>
> 5. CONTAINER START:
>    -> Start the container(s) defined in the task definition
>    -> Task transitions to RUNNING
>
> Total cold-start time: ~10-30 seconds typically
>   (dominated by image pull, not microVM boot)
> ```"

**Interviewer:**
Tell me more about Firecracker and why it matters for isolation.

**Candidate:**

> "Firecracker is a virtual machine monitor (VMM) built by AWS specifically for serverless workloads -- it was developed for Lambda and Fargate.
>
> **Why not just Docker containers for isolation?**
>
> ```
> Container Isolation (Docker/cgroups/namespaces):
>   - Containers share the host kernel
>   - A kernel exploit in one container could compromise others
>   - cgroups/namespaces provide resource isolation but NOT security isolation
>   - Acceptable when all containers belong to the same customer
>   - NOT acceptable for multi-tenant Fargate where different customers
>     share physical hosts
>
> VM Isolation (traditional VMs like QEMU/KVM):
>   - Each VM has its own kernel -- full security isolation
>   - But traditional VMs are heavyweight:
>     - ~500ms-seconds boot time
>     - 100s of MB memory overhead for the guest OS
>     - Complex device emulation (virtual disks, NICs, etc.)
>   - Too slow and wasteful for running a single container
>
> Firecracker MicroVM (the sweet spot):
>   - Each microVM has its own kernel -- same security isolation as VMs
>   - But stripped down to the bare minimum:
>     - ~125ms boot time (vs seconds for QEMU)
>     - ~5 MB memory overhead per microVM [Firecracker published numbers]
>     - Only emulates 5 devices: virtio-net, virtio-block, serial
>       console, keyboard (i8042), and a minimal legacy device
>     - No BIOS, no USB, no PCI -- nothing unnecessary
>   - Runs on top of KVM (Linux's built-in hypervisor)
>   - Rate-limited I/O to prevent noisy-neighbor
> ```
>
> **Security isolation model for Fargate:**
>
> ```
> Physical Host
> +--------------------------------------------------+
> |  Host Kernel (with KVM)                          |
> |                                                  |
> |  +------------------+  +------------------+      |
> |  | Firecracker      |  | Firecracker      |     |
> |  | MicroVM          |  | MicroVM          |     |
> |  |                  |  |                  |      |
> |  | Guest Kernel     |  | Guest Kernel     |     |
> |  | +------+------+  |  | +------+         |     |
> |  | |Ctr A1|Ctr A2|  |  | |Ctr B1|         |     |
> |  | +------+------+  |  | +------+         |     |
> |  | Customer A Task  |  | Customer B Task  |     |
> |  +------------------+  +------------------+      |
> |                                                  |
> |  Each microVM is a separate VM with its own      |
> |  kernel. Customer A cannot see or affect          |
> |  Customer B, even though they share a host.      |
> +--------------------------------------------------+
> ```
>
> **Why this matters:** In EC2 launch type, all containers on an instance share the host kernel and belong to the same customer. In Fargate, different customers' tasks can share the same physical host, so we need VM-level isolation. Firecracker provides this without the performance overhead of traditional VMs."

**Interviewer:**
How does Fargate handle capacity management -- keeping enough warm capacity for task launches?

**Candidate:**

> "This is the hidden complexity of Fargate. From the customer's perspective, capacity is infinite. Behind the scenes, AWS must maintain a massive fleet of physical hosts with pre-provisioned capacity.
>
> [INFERRED -- not officially documented] The Fargate capacity management likely works like this:
>
> ```
> Fargate Fleet Management:
>
> 1. CAPACITY POOLS:
>    - AWS maintains pools of physical hosts per region, per AZ
>    - Hosts are grouped by generation (Graviton2, Graviton3, Intel)
>    - Each host has a fixed amount of CPU/memory available for microVMs
>
> 2. BIN-PACKING:
>    - When a task arrives, Fargate bin-packs it onto an existing host
>      with enough remaining capacity
>    - This is an internal scheduling problem -- similar to EC2 placement
>      but invisible to the customer
>    - Must consider: requested CPU/memory, ENI availability,
>      AZ preference
>
> 3. PRE-WARMING:
>    - AWS likely maintains a buffer of idle capacity per AZ
>    - If utilization approaches the buffer threshold, new hosts are
>      provisioned
>    - This is how Fargate achieves fast task launch times -- the host
>      is already running, just need to boot a microVM
>
> 4. OVERCOMMIT (MAYBE):
>    - Fargate tasks have hard CPU/memory limits
>    - Unlike EC2 where you might overcommit CPU, Fargate likely does
>      NOT overcommit -- you get exactly what you asked for
>    - This is a tradeoff: less efficient fleet utilization, but
>      predictable performance for customers
> ```
>
> **Fargate Spot -- a window into capacity management:**
> Fargate Spot tasks can be interrupted when AWS needs the capacity back. This tells us that Fargate is using a capacity pool model where spot tasks run on 'excess' capacity, and on-demand tasks have reserved capacity."

#### Architecture Update After Phase 7

> | | Before (Phase 6) | After (Phase 7) |
> |---|---|---|
> | **Fargate Isolation** | Not detailed | **Firecracker microVMs per task; VM-level isolation via KVM; ~125ms boot, ~5MB overhead** |
> | **Fargate Networking** | Not detailed | **awsvpc mandatory; each task gets its own ENI + private IP in customer's VPC** |
> | **Fargate Capacity** | Not detailed | **AWS-managed fleet, bin-packed hosts, pre-warmed capacity pools, no customer-visible instances** |

---

### L5 vs L6 vs L7 -- Phase 7 Rubric

| Aspect | L5 (SDE-2) | L6 (SDE-3) | L7 (Principal) |
|---|---|---|---|
| **Fargate Model** | "Fargate is serverless containers" | Explains Firecracker microVMs, why containers alone aren't sufficient for multi-tenant isolation, compares to traditional VMs | Discusses Firecracker's device model (only 5 emulated devices), jailer for additional sandboxing, rate-limited I/O for noisy-neighbor prevention |
| **Isolation** | "Tasks are isolated" | Explains kernel-level isolation via separate guest kernels per microVM, contrasts with cgroups/namespace isolation | Discusses the security boundary model, how AWS handles side-channel attacks (Spectre/Meltdown), and whether tasks from different customers share physical hosts |
| **Capacity Management** | "AWS manages it" | Discusses bin-packing on hosts, pre-warmed capacity pools, Fargate Spot as evidence of capacity pool model | Discusses fleet management signals, how Fargate scales its physical fleet, the cost model of maintaining warm capacity, and how oversubscription decisions affect SLA |

---

## PHASE 8: Deep Dive -- Networking (~8 min)

**Interviewer:**
Let's talk about networking. You mentioned `awsvpc` mode. Walk me through the different networking modes and their tradeoffs.

**Candidate:**

> "ECS supports four networking modes for EC2 launch type, plus awsvpc is the only option for Fargate. Each mode has fundamentally different isolation and density characteristics:
>
> **Mode 1: `awsvpc` (Recommended, required for Fargate)**
>
> ```
> +--EC2 Instance (or Fargate host)---+
> |                                    |
> | Primary ENI (instance's own)       |
> | 10.0.1.100                         |
> |                                    |
> | Task ENI 1            Task ENI 2   |
> | 10.0.1.101            10.0.1.102   |
> | +----------+          +----------+ |
> | | Task A   |          | Task B   | |
> | | (nginx)  |          | (api)    | |
> | +----------+          +----------+ |
> +------------------------------------+
>
> Each task gets its own ENI with its own:
> - Private IP address
> - Security groups (task-level network isolation!)
> - DNS name
> - Can be registered in ALB target groups by IP
> ```
>
> **Advantages:**
> - Each task has its own IP -- no port conflicts
> - Security groups at the task level (not just instance level)
> - Tasks appear as first-class network citizens in the VPC
> - Same networking semantics as EC2 instances
>
> **Disadvantage: ENI limits!**
> - Each EC2 instance type has a maximum number of ENIs
> - A `c5.large` supports 3 ENIs total -- that's only 2 tasks (1 ENI for the instance itself)
> - This severely limits task density per instance
>
> **Mitigation: ENI trunking (`awsvpcTrunking`)**
> - When enabled (account setting), ECS creates a 'trunk' ENI that multiplexes multiple task ENIs
> - A `c5.large` goes from 2 tasks to ~12 tasks with trunking
> - This is critical for running meaningful numbers of awsvpc tasks on an instance
>
> **Mode 2: `bridge` (Default on Linux, not available for Fargate)**
>
> ```
> +--EC2 Instance--------------------------+
> |                                        |
> | Instance ENI: 10.0.1.100              |
> |                                        |
> | Docker Bridge Network (172.17.0.0/16) |
> |   +----------+     +----------+       |
> |   | Task A   |     | Task B   |       |
> |   | 172.17.2 |     | 172.17.3 |       |
> |   | :80->3247|     | :80->3248|       |
> |   +----------+     +----------+       |
> |                                        |
> | Host port 32471 -> Task A:80          |
> | Host port 32482 -> Task B:80          |
> +----------------------------------------+
>
> Tasks share the instance's ENI via Docker's bridge network.
> Dynamic port mapping: container port 80 is mapped to random
> host ports (32471, 32482).
> ```
>
> **Advantages:**
> - No ENI limit -- many tasks per instance
> - Higher density than awsvpc
>
> **Disadvantages:**
> - No task-level security groups (all tasks share instance SG)
> - Dynamic port mapping adds complexity
> - Tasks don't have their own IPs -- harder for service discovery
> - ALB integration requires dynamic port mapping (ALB discovers the random host port)
>
> **Mode 3: `host`**
>
> ```
> +--EC2 Instance--------------------------+
> |                                        |
> | Instance ENI: 10.0.1.100              |
> |                                        |
> |   +----------+     +----------+       |
> |   | Task A   |     | Task B   |       |
> |   | :80      |     | :8080    |       |
> |   +----------+     +----------+       |
> |                                        |
> | Container ports bind directly to host  |
> | Port 80 -> Task A                     |
> | Port 8080 -> Task B                   |
> +----------------------------------------+
>
> Containers use the host's network namespace directly.
> No NAT, no port mapping -- maximum network performance.
> ```
>
> **Advantages:**
> - Best network performance (no NAT overhead)
> - Simple -- container port = host port
>
> **Disadvantages:**
> - **Cannot run multiple tasks using the same port on one instance** (port conflict)
> - No dynamic port mapping
> - Limited to one task per port per instance
>
> **Summary of tradeoffs:**
>
> | Aspect | awsvpc | bridge | host |
> |---|---|---|---|
> | **Task IP** | Own IP per task | Shared (via Docker bridge) | Instance IP |
> | **Security Groups** | Per task | Per instance | Per instance |
> | **Port Conflicts** | None (own IP) | None (dynamic mapping) | Yes -- one task per port |
> | **Task Density** | Limited by ENIs (mitigated by trunking) | High | Low (port conflicts) |
> | **Performance** | Good (ENI) | Good (bridge overhead minimal) | Best (no NAT) |
> | **Fargate Support** | Yes (only mode) | No | No |
> | **ALB Integration** | Target by IP | Target by instance + port | Target by instance + port |"

**Interviewer:**
How does service discovery work with these networking modes?

**Candidate:**

> "ECS integrates with AWS Cloud Map for service discovery. The behavior depends on the networking mode:
>
> ```
> Service Discovery via AWS Cloud Map:
>
> 1. Create a Cloud Map namespace (private DNS namespace)
>    e.g., 'production.internal'
>
> 2. Create a Cloud Map service within the namespace
>    e.g., 'api.production.internal'
>
> 3. Configure ECS service with serviceRegistries pointing to
>    the Cloud Map service
>
> 4. When a task starts (RUNNING state):
>    ECS registers it with Cloud Map
>    -> Creates a DNS record: api.production.internal -> 10.0.1.101
>
> 5. When a task stops:
>    ECS deregisters it from Cloud Map
>    -> DNS record is removed
>
> 6. Other services discover by DNS:
>    dig api.production.internal
>    -> Returns IPs of all healthy tasks
> ```
>
> **Networking mode affects what gets registered:**
>
> | Mode | DNS Record Type | Value |
> |---|---|---|
> | awsvpc | A record | Task's private IP |
> | bridge | SRV record | Instance IP + dynamic port |
> | host | SRV record | Instance IP + host port |
>
> With awsvpc, you get clean A records -- clients just connect to the IP. With bridge/host, you need SRV records that include the port, which is more complex for clients to consume.
>
> **Important quota:** Services using service discovery are limited to **1,000 tasks per service** (due to AWS Cloud Map / Route 53 constraints), vs the normal 5,000 task limit."

---

### L5 vs L6 vs L7 -- Phase 8 Rubric

| Aspect | L5 (SDE-2) | L6 (SDE-3) | L7 (Principal) |
|---|---|---|---|
| **Networking Modes** | Knows awsvpc exists | Explains all three modes with diagrams, compares tradeoffs (ENI limits vs density vs isolation), mentions ENI trunking | Discusses the VPC-level implications (ENI limits per subnet, IP address exhaustion), how awsvpc interacts with VPC flow logs and security group rules at scale |
| **Service Discovery** | "Use DNS" | Explains Cloud Map integration, how records are registered/deregistered, difference between A vs SRV records per mode | Discusses health-check propagation latency, DNS TTL tradeoffs, and the alternative of ECS Service Connect (Envoy-based service mesh) |
| **ENI Trunking** | Doesn't mention | Explains the problem (ENI limits restrict density) and the solution (trunk ENI multiplexing) with concrete numbers | Discusses the trunk ENI implementation details, how VLAN tagging works within the trunk, and the interaction with VPC ENI quotas |

---

## PHASE 9: Deep Dive -- Capacity Providers & Auto Scaling (~8 min)

**Interviewer:**
Let's talk about capacity providers. In your Phase 4 design, the EC2 instances are static. How do we auto-scale?

**Candidate:**

> "Capacity providers are how ECS bridges the gap between 'how many tasks do I need' and 'how many instances do I need.' They solve the fundamental problem: **the task scheduler and the instance fleet operate at different abstractions.**
>
> ```
> WITHOUT Capacity Providers:
>   - You create an Auto Scaling Group (ASG) with min/max/desired
>   - You create an ECS service with desiredCount
>   - These two systems are INDEPENDENT
>   - If your service needs more tasks than instances can hold,
>     tasks get stuck in PENDING
>   - You have to manually configure ASG scaling policies to
>     match ECS demand -- error-prone
>
> WITH Capacity Providers:
>   - You create a capacity provider linked to an ASG
>   - ECS manages the ASG scaling FOR YOU
>   - When tasks need capacity, ECS signals the ASG to scale out
>   - When instances are underutilized, ECS signals scale-in
>   - The task scheduler and fleet management are COUPLED
> ```
>
> **Capacity Provider Types:**
>
> 1. **Fargate Capacity Provider**: Built-in, always available. Tasks automatically get Fargate capacity. Also `FARGATE_SPOT` for interruptible tasks at lower cost.
>
> 2. **Auto Scaling Group (ASG) Capacity Provider**: Links an ASG to an ECS cluster. Enables two key features:
>
> **Feature 1: Managed Scaling**
>
> ```
> Managed Scaling:
>   - ECS calculates: how many instances does the cluster need
>     to run all desired tasks?
>   - Uses a target tracking metric:
>     CapacityProviderReservation = (tasks needing capacity /
>                                    total capacity) * 100
>   - Target value typically 100% (just enough capacity)
>   - If reservation > 100%: tasks are waiting -> scale out
>   - If reservation < 100%: excess capacity -> scale in
>
>   Example:
>     Cluster has 4 instances, each can run 10 tasks (total: 40 slots)
>     Service needs 35 tasks
>     Reservation = 35/40 = 87.5%
>     Target = 100%, so no scale-in yet
>
>     Service scales to 50 tasks
>     Reservation = 50/40 = 125% -> scale out!
>     ASG adds 1 instance -> now 5 instances, 50 slots
>     Reservation = 50/50 = 100% -> steady state
> ```
>
> **Feature 2: Managed Termination Protection**
>
> ```
> Problem: ASG wants to scale in and terminate an instance.
> But that instance is running tasks!
>
> Without termination protection:
>   ASG terminates instance -> tasks die -> service scheduler
>   launches replacements on other instances -> brief outage
>
> With managed termination protection:
>   1. ASG picks an instance to terminate
>   2. ECS checks: are there tasks on this instance?
>   3. If yes: ECS sets instance to DRAINING state
>   4. Tasks are gracefully stopped (SIGTERM -> wait -> SIGKILL)
>   5. Service scheduler launches replacements on other instances
>   6. Once instance is empty, ECS allows ASG to terminate it
>   7. Zero task loss
> ```"

**Interviewer:**
Can you have multiple capacity providers in a cluster?

**Candidate:**

> "Yes! This is one of the most powerful features. A cluster can have up to **20 capacity providers** (published quota), and you can define a **capacity provider strategy** that splits tasks across them:
>
> ```
> Capacity Provider Strategy Example:
>
> Cluster has:
>   - CP1: ASG with c5.xlarge instances (compute-optimized)
>   - CP2: ASG with r5.xlarge instances (memory-optimized)
>   - CP3: FARGATE (serverless)
>
> Strategy for 'web-service':
>   capacityProviderStrategy: [
>     {capacityProvider: 'CP1', weight: 3, base: 2},
>     {capacityProvider: 'CP3', weight: 1, base: 0}
>   ]
>
> What this means:
>   - 'base: 2' on CP1 -> first 2 tasks ALWAYS go to CP1
>   - After base is filled, split remaining tasks 3:1
>     (75% on CP1, 25% on Fargate)
>   - This gives you: EC2 for baseline (cheaper),
>     Fargate for burst (no capacity planning needed)
>
> Result with desiredCount=10:
>   CP1: 2 (base) + 6 (75% of remaining 8) = 8 tasks on EC2
>   CP3: 0 (base) + 2 (25% of remaining 8) = 2 tasks on Fargate
> ```
>
> **This enables the 'EC2 for baseline, Fargate for burst' pattern:**
> - Run your steady-state workload on cheaper EC2 instances
> - Burst into Fargate for spikes (no need to pre-provision EC2 capacity)
> - The strategy automatically routes tasks to the right capacity provider
>
> **Default capacity provider strategy:**
> - Set at the cluster level
> - Applies when a RunTask or CreateService doesn't specify a launch type or strategy
> - Prevents the common mistake of launching tasks with no capacity"

#### Architecture Update After Phase 9

> | | Before (Phase 8) | After (Phase 9) |
> |---|---|---|
> | **Capacity Management** | Static ASG, manual scaling | **Capacity providers: managed scaling (reservation metric), managed termination protection (drain before terminate)** |
> | **Multi-Provider** | Not discussed | **Up to 20 providers per cluster; weighted strategies with base counts; EC2+Fargate hybrid** |

---

### L5 vs L6 vs L7 -- Phase 9 Rubric

| Aspect | L5 (SDE-2) | L6 (SDE-3) | L7 (Principal) |
|---|---|---|---|
| **Capacity Providers** | "Use Auto Scaling" | Explains managed scaling (CapacityProviderReservation metric, target tracking), managed termination protection (drain before terminate) | Discusses the feedback loop between service scheduler and fleet management, how CAS (Cluster Auto Scaling) avoids oscillation, and the tradeoff between scale-out speed and cost |
| **Multi-Provider Strategy** | Does not mention | Explains weight and base parameters, demonstrates EC2+Fargate hybrid pattern | Analyzes cost modeling: EC2 on-demand vs EC2 spot vs Fargate vs Fargate Spot, and how to optimize the weight distribution |
| **Scale-In** | "Remove instances" | Explains drain-before-terminate flow, why it prevents task loss | Discusses how to handle long-draining tasks, connection draining timeout, and the interaction with ALB deregistration delay |

---

## PHASE 10: Deep Dive -- Deployments (~8 min)

**Interviewer:**
Let's talk about deployments. How does ECS handle rolling updates and blue/green deployments?

**Candidate:**

> "Deployments are how you update a service's task definition (new image, new config, new resource allocation) without downtime. ECS supports three deployment controllers:
>
> **1. Rolling Update (ECS default, built-in)**
>
> ```
> Service Config:
>   desiredCount: 4
>   minimumHealthyPercent: 100
>   maximumPercent: 200
>
> Current State: 4 tasks running v1
> Goal: Update to v2
>
> Step 1: Launch 4 new v2 tasks (maxPercent=200 allows up to 8 total)
>   Running: [v1, v1, v1, v1, v2, v2, v2, v2]  (8 tasks)
>
> Step 2: Wait for v2 tasks to pass health checks
>   (ELB health check + container health check)
>
> Step 3: Register v2 tasks with ALB target group
>
> Step 4: Deregister v1 tasks from ALB target group
>   (wait for deregistration delay -- typically 300s)
>
> Step 5: Stop v1 tasks
>   Running: [v2, v2, v2, v2]  (4 tasks)
>
> Total time: ~5-10 minutes depending on health check intervals
>              and deregistration delay
> ```
>
> **With `minimumHealthyPercent: 50, maximumPercent: 100`:**
>
> ```
> Step 1: Stop 2 of 4 v1 tasks (50% minimum healthy)
>   Running: [v1, v1]  (2 tasks)
>
> Step 2: Launch 2 v2 tasks
>   Running: [v1, v1, v2, v2]  (4 tasks, at max)
>
> Step 3: Once v2 healthy, stop remaining v1 tasks
>   Running: [v2, v2]  (2 tasks)
>
> Step 4: Launch 2 more v2 tasks
>   Running: [v2, v2, v2, v2]  (4 tasks)
>
> Tradeoff: No extra capacity needed, but reduced availability
> during deployment (only 50% capacity in step 1)
> ```
>
> **Deployment Circuit Breaker:**
>
> ```
> Problem: What if v2 is broken? Tasks keep crashing, scheduler
> keeps launching replacements, in an infinite loop.
>
> Circuit Breaker behavior:
>   - ECS tracks how many tasks have failed to stabilize
>   - If failure count exceeds a threshold (based on desired count),
>     the deployment is marked as FAILED
>   - With 'rollback: true', ECS automatically rolls back to the
>     last stable deployment (v1)
>   - Without rollback, the deployment just stops and the service
>     remains in a degraded state
>
> deploymentCircuitBreaker: {
>   enable: true,
>   rollback: true  // auto-rollback on failure
> }
>
> This prevents the 'infinite crash loop' problem where a bad
> deployment consumes resources endlessly.
> ```"

**Interviewer:**
What about blue/green deployments?

**Candidate:**

> "Blue/green deployments use **AWS CodeDeploy** as an external deployment controller, providing more control over traffic shifting:
>
> ```
> Blue/Green Deployment with CodeDeploy:
>
> Setup:
>   - ALB with two target groups: Blue (TG1) and Green (TG2)
>   - Listener rules: production traffic -> TG1, test traffic -> TG2
>   - CodeDeploy application + deployment group configured
>
> Deployment Flow:
>
> 1. PROVISION GREEN:
>    CodeDeploy tells ECS to launch new tasks (v2) -> registered in TG2
>    Blue (v1) tasks still in TG1, serving production traffic
>
>    ALB Listener:
>    Port 443 -> TG1 (Blue, v1) [100% production traffic]
>    Port 8443 -> TG2 (Green, v2) [test traffic only]
>
> 2. TEST PHASE:
>    Run validation against TG2 (port 8443)
>    Lifecycle hooks can run Lambda functions for testing
>    If tests fail -> ROLLBACK (terminate green tasks, done)
>
> 3. TRAFFIC SHIFT:
>    CodeDeploy shifts ALB listener from TG1 to TG2
>    Shift strategies:
>    - AllAtOnce: instant cutover (fastest, riskiest)
>    - Linear10PercentEvery1Minute: gradual shift over 10 minutes
>    - Canary10Percent5Minutes: send 10% to green for 5 min,
>      then 100% if healthy
>
> 4. BAKE TIME:
>    After full traffic shift, wait (configurable) to monitor
>    for errors before terminating blue
>
> 5. CLEANUP:
>    Terminate blue (v1) tasks
>    TG2 is now production
>    Next deployment, TG2 becomes 'blue' and TG1 becomes 'green'
>
> Total time: 10-30 minutes depending on strategy
> ```
>
> **Tradeoff: Rolling Update vs Blue/Green:**
>
> | Aspect | Rolling Update | Blue/Green (CodeDeploy) |
> |---|---|---|
> | **Complexity** | Simple -- built into ECS | Complex -- requires CodeDeploy, two target groups, listener rules |
> | **Rollback Speed** | Slow (must re-deploy v1) | Instant (just switch listener back to blue TG) |
> | **Testing** | No pre-production test phase | Can test green in isolation before shifting traffic |
> | **Traffic Control** | All-or-nothing per task | Gradual: canary, linear, all-at-once |
> | **Cost During Deploy** | Up to 2x tasks briefly | 2x tasks for the entire deployment duration |
> | **ALB Required** | No (works without LB) | Yes (needs two target groups) |
>
> **When to use which:**
> - Rolling update: most services, especially internal services
> - Blue/green: customer-facing services where you need instant rollback and canary testing"

#### Architecture Update After Phase 10

> | | Before (Phase 9) | After (Phase 10) |
> |---|---|---|
> | **Deployments** | Not detailed | **Rolling update (built-in, minHealthy/maxPercent), blue/green (CodeDeploy + ALB dual target group)** |
> | **Deployment Safety** | Not discussed | **Circuit breaker (auto-rollback on failure), canary/linear traffic shifting, lifecycle hooks for testing** |

---

### L5 vs L6 vs L7 -- Phase 10 Rubric

| Aspect | L5 (SDE-2) | L6 (SDE-3) | L7 (Principal) |
|---|---|---|---|
| **Rolling Update** | "Update tasks one by one" | Explains minHealthyPercent/maxPercent tradeoffs with concrete sequences, discusses ALB deregistration delay | Discusses how the deployment state machine handles edge cases: what if an AZ goes down mid-deployment, how drain timeout interacts with deployment speed |
| **Blue/Green** | "Deploy new version alongside old" | Explains the CodeDeploy integration, dual target group model, traffic shifting strategies (canary, linear) | Discusses lifecycle hooks (BeforeAllowTraffic, AfterAllowTraffic), how to implement automated rollback based on CloudWatch alarms, and the cost of maintaining 2x capacity |
| **Circuit Breaker** | Does not mention | Explains the failure threshold, auto-rollback behavior | Discusses how the threshold is calculated, the interaction between circuit breaker and service scheduler (preventing task replacement storms), and manual override |

---

## PHASE 11: Deep Dive -- Monitoring & Health Checks (~5 min)

**Interviewer:**
How does ECS know when a task is healthy or unhealthy?

**Candidate:**

> "ECS uses multiple layers of health checking, and they interact in important ways:
>
> **Layer 1: Container Health Check (defined in task definition)**
>
> ```json
> \"healthCheck\": {
>   \"command\": [\"CMD-SHELL\", \"curl -f http://localhost:80/health || exit 1\"],
>   \"interval\": 30,
>   \"timeout\": 5,
>   \"retries\": 3,
>   \"startPeriod\": 60
> }
> ```
>
> - Runs inside the container via Docker HEALTHCHECK
> - States: UNKNOWN -> HEALTHY or UNHEALTHY
> - `startPeriod`: grace period for slow-starting apps (first 60s of failures are ignored)
> - If an **essential** container becomes UNHEALTHY, ECS stops the entire task
>
> **Layer 2: ELB Health Check (when using a load balancer)**
>
> ```
> ALB Target Group Health Check:
>   Path: /health
>   Interval: 30s
>   Healthy threshold: 3 consecutive successes
>   Unhealthy threshold: 2 consecutive failures
>   Timeout: 5s
>
> If a task fails ALB health checks:
>   1. ALB stops routing traffic to the task
>   2. ECS detects the unhealthy target
>   3. ECS stops the unhealthy task
>   4. Service scheduler launches a replacement
> ```
>
> **Layer 3: Agent Heartbeat (EC2 launch type)**
>
> ```
> ECS Agent sends heartbeats to control plane (~every 30s)
> If heartbeats stop:
>   1. Control plane marks agent_connected = false
>   2. After timeout, tasks on that instance are considered
>      status unknown
>   3. If the instance is truly dead, service scheduler
>      launches replacements elsewhere
> ```
>
> **Layer 4: Task State Transitions (EventBridge)**
>
> ```
> Every task state change generates an event:
>   {
>     \"source\": \"aws.ecs\",
>     \"detail-type\": \"ECS Task State Change\",
>     \"detail\": {
>       \"taskArn\": \"arn:aws:ecs:...\",
>       \"lastStatus\": \"STOPPED\",
>       \"stoppedReason\": \"Essential container exited\",
>       \"stopCode\": \"EssentialContainerExited\",
>       \"containers\": [{
>         \"exitCode\": 137,
>         \"reason\": \"OutOfMemoryError: Container killed\"
>       }]
>     }
>   }
>
> These events can trigger:
> - CloudWatch Alarms (alert on task churn)
> - Lambda (automated remediation)
> - SNS (notification to on-call)
> ```
>
> **CloudWatch Container Insights:**
> - Provides aggregated metrics: CPU utilization, memory utilization, network I/O, storage I/O
> - Metrics at cluster, service, and task level
> - Enables auto-scaling based on CPU/memory utilization
> - 'Enhanced observability' mode (account setting `containerInsights: enhanced`) provides per-container metrics
>
> **The interaction between health check layers is critical:**
>
> ```
> Scenario: Container health check says HEALTHY, but ALB health
> check says UNHEALTHY
>
> This can happen if:
>   - The container responds to local curl (internal health check)
>   - But doesn't respond to ALB probes on the actual service port
>   - e.g., the app started but can't connect to its database
>
> ECS behavior: ALB health check takes precedence for services
> behind a load balancer. The task will be replaced even though
> the container thinks it's healthy.
>
> This is the right behavior -- what matters is whether the task
> can serve EXTERNAL traffic, not just whether the process is alive.
> ```"

---

### L5 vs L6 vs L7 -- Phase 11 Rubric

| Aspect | L5 (SDE-2) | L6 (SDE-3) | L7 (Principal) |
|---|---|---|---|
| **Health Checks** | "Use health checks" | Explains three layers (container, ELB, agent), their interaction, and the precedence model | Discusses health check tuning (interval vs false positives), the startPeriod design decision, and how health check flapping triggers circuit breaker |
| **Monitoring** | "Use CloudWatch" | Explains Container Insights, EventBridge task state change events, the specific stop codes and exit codes that matter | Proposes an operational dashboard: task churn rate, deployment success rate, placement failure rate, scheduling latency percentiles, and how to set alarms |
| **Failure Scenarios** | Lists failures | Walks through a concrete scenario (ALB healthy vs container healthy disagree), explains the correct behavior | Discusses cascading failures: what if a downstream dependency fails, all tasks start failing health checks simultaneously, and the service scheduler tries to replace all 5,000 tasks at once |

---

## PHASE 12: Wrap-Up & Summary (~3 min)

**Interviewer:**
We're running low on time. Summarize your design and tell me what keeps you up at night operating this system.

**Candidate:**

> "**Design evolution -- how we got here:**
>
> | Component | Started With (Phase 4) | Evolved To | Why |
> |---|---|---|---|
> | **Architecture** | Single host with Docker | 3-layer: Provisioning (API), Controller (brain), Capacity (data plane) | Separate concerns that scale and fail independently |
> | **State Store** | Unspecified | Partitioned KV store (DynamoDB-like), keyed by cluster_id | Millions of state transitions/day; need low-latency reads for scheduling, range queries for listing |
> | **Task Placement** | Manual | 4-step pipeline: resource filter -> constraint filter -> strategy score -> select | Must handle diverse requirements: HA (spread), cost (binpack), hardware affinity (constraints) |
> | **Fargate Isolation** | Not discussed | Firecracker microVMs per task; VM-level isolation; ~125ms boot, ~5MB overhead | Multi-tenant security requires more than container namespaces; Firecracker gives VM isolation at container speed |
> | **Networking** | Not discussed | awsvpc (ENI per task, task-level SGs); bridge/host as alternatives; ENI trunking for density | Tasks need first-class network identity; ENI limits mitigated by trunking |
> | **Capacity** | Static instances | Capacity providers: managed scaling (reservation metric), managed termination protection, multi-provider strategies | Bridges the abstraction gap between 'desired tasks' and 'available instances' |
> | **Deployments** | Not discussed | Rolling update (minHealthy/maxPercent, circuit breaker) + blue/green (CodeDeploy, canary/linear traffic shift) | Zero-downtime updates with safety nets (circuit breaker, instant rollback) |
> | **Health & Monitoring** | Not discussed | 3-layer health checks (container, ELB, agent), EventBridge events, Container Insights | Multiple overlapping detection mechanisms; operational visibility at cluster/service/task level |
>
> **Key ECS Quotas (verified against AWS docs):**
>
> | Quota | Limit |
> |---|---|
> | Clusters per account | 10,000 (adjustable) |
> | Container instances per cluster | 5,000 |
> | Services per cluster | 5,000 |
> | Tasks per service | 5,000 (1,000 with service discovery) |
> | Containers per task definition | 10 |
> | Task definition revisions per family | 1,000,000 |
> | Capacity providers per cluster | 20 |
> | Tasks launched per RunTask call | 10 |
> | Task launch rate | 500/min per service |
> | Subnets per awsvpcConfiguration | 16 |
> | Security groups per awsvpcConfiguration | 5 |
>
> **What keeps me up at night:**
>
> 1. **Scheduler thundering herd.** Imagine a major customer with 100 services, each at 5,000 tasks, and their AZ goes down. That's 500,000 tasks that need replacement simultaneously. The scheduler must rate-limit replacements to avoid overwhelming the placement engine, the state store, and the capacity providers. But rate-limiting too aggressively means slow recovery. The tradeoff between recovery speed and system stability during large-scale failures is the hardest operational challenge.
>
> 2. **State store consistency under concurrent scheduling.** When two scheduling decisions happen concurrently for the same cluster, they both read 'remaining resources' for an instance. Both think there's room. Both place a task. Now the instance is overcommitted. We need either optimistic concurrency (check-and-set on remaining resources) or pessimistic locking (serialize scheduling per instance). Both have tradeoffs -- locking reduces throughput, optimistic concurrency causes retry storms.
>
> 3. **Fargate cold start latency tail.** The median Fargate launch is 10-20 seconds, dominated by image pull. But p99 can be much worse: large images, ECR throttling, or no warm capacity in the requested AZ. For latency-sensitive workloads, this is a real problem. Mitigations: image caching on Fargate hosts [INFERRED], seekable OCI (SOCI) for lazy image loading, and Fargate's capacity pools for pre-warming.
>
> 4. **Blast radius of control plane issues.** The ECS control plane is a shared service across all customers in a region. A bug in the service scheduler could affect every ECS service in us-east-1. AWS likely uses cell-based architecture [INFERRED -- not officially documented] to limit blast radius -- different customers' clusters are served by different cells of the control plane. But correlated failures (bad deployment to all cells, regional infrastructure issue) can still be wide-impact.
>
> 5. **Agent version skew.** The ECS Agent runs on customer EC2 instances. Customers don't always update it. The control plane must be backward-compatible with old agent versions while adding new features. This is a persistent source of bugs: new control plane feature assumes agent capability that old agents don't have. The agent version handshake at connection time is critical.
>
> 6. **ENI exhaustion in awsvpc mode.** Despite ENI trunking, a VPC has a finite number of IPs in each subnet. A customer running thousands of awsvpc tasks can exhaust their subnet's IP space. The failure mode is tasks stuck in PROVISIONING (can't get an ENI). This requires monitoring subnet IP utilization and alerting before exhaustion.
>
> **Architecture diagram -- final state:**
>
> ```
>                              +------------------------+
>                              |       Customers         |
>                              |  (CLI, SDK, Console)    |
>                              +-----------+------------+
>                                          | HTTPS (REST API)
>                              +-----------v------------+
>                              |   PROVISIONING LAYER    |
>                              |   API Front-End         |
>                              |   - IAM Auth (SigV4)    |
>                              |   - Rate Limiting       |
>                              |   - Request Validation  |
>                              +-----------+------------+
>                                          |
>                              +-----------v------------+
>                              |   CONTROLLER LAYER      |
>                              |                         |
>                              | +---------------------+ |
>                              | | State Store         | |
>                              | | (DynamoDB-like)     | |
>                              | | Clusters, Tasks,    | |
>                              | | Services, Instances | |
>                              | +---------------------+ |
>                              |                         |
>                              | +---------------------+ |
>                              | | Service Scheduler   | |
>                              | | Reconciliation loop | |
>                              | | Desired vs Actual   | |
>                              | +---------------------+ |
>                              |                         |
>                              | +---------------------+ |
>                              | | Placement Engine    | |
>                              | | Resource -> Constr  | |
>                              | | -> Strategy -> Pick | |
>                              | +---------------------+ |
>                              |                         |
>                              | +---------------------+ |
>                              | | Capacity Manager    | |
>                              | | CAS metric ->       | |
>                              | | ASG scaling signals | |
>                              | +---------------------+ |
>                              +-----------+------------+
>                                          |
>                    +---------------------+--------------------+
>                    |                                          |
>       +------------v-----------+            +-----------------v--------+
>       |   EC2 CAPACITY LAYER   |            |   FARGATE CAPACITY LAYER |
>       |                        |            |                          |
>       | Customer EC2 instances |            | AWS-managed hosts        |
>       | ECS Agent per instance |            | Firecracker microVM      |
>       |                        |            | per task                 |
>       | Agent <-> Control Plane|            |                          |
>       | - Heartbeat (30s)      |            | awsvpc networking        |
>       | - Task assignments     |            | ENI per task             |
>       | - State reports        |            |                          |
>       |                        |            | ~125ms microVM boot      |
>       | Placement strategies:  |            | ~5MB per-VM overhead     |
>       | spread/binpack/random  |            |                          |
>       |                        |            | Isolation: separate      |
>       | Capacity providers:    |            | kernel per customer task |
>       | Managed scaling + MTP  |            |                          |
>       +------------------------+            +--------------------------+
>                    |                                          |
>       +------------v-----------+            +-----------------v--------+
>       |   DEPLOYMENT LAYER     |            |   MONITORING LAYER       |
>       |                        |            |                          |
>       | Rolling Update (ECS)   |            | Container health checks  |
>       | - minHealthy/maxPct    |            | ELB health checks        |
>       | - Circuit breaker      |            | Agent heartbeats         |
>       |                        |            |                          |
>       | Blue/Green (CodeDeploy)|            | EventBridge task events  |
>       | - Dual target groups   |            | Container Insights       |
>       | - Canary/linear shift  |            | CloudWatch metrics       |
>       +------------------------+            +--------------------------+
> ```"

**Interviewer:**
Strong answer. One last question -- if you had to pick one area where ECS could improve, what would it be?

**Candidate:**

> "Task startup latency, especially for Fargate. The bottleneck is almost always the container image pull -- a 2GB image takes 30+ seconds to download. AWS has invested in solutions like **seekable OCI (SOCI)** which enables lazy loading of container images (start the container before the full image is downloaded, and pull layers on-demand). But adoption is still growing.
>
> If I were building this, I'd invest heavily in:
> 1. **Image caching on Fargate hosts** -- if another customer recently ran the same base image (e.g., `python:3.11`), don't re-download it. This requires content-addressed storage with deduplication at the image layer level.
> 2. **Predictive pre-warming** -- if a customer deploys every weekday at 9 AM, pre-pull their image at 8:55 AM.
> 3. **Snapshot-based fast start** -- take a memory snapshot of a running container (CRIU-style) and restore from snapshot instead of cold-starting. Lambda uses this (SnapStart for Java), and it could benefit Fargate too.
>
> The goal is sub-second Fargate task start for 90% of use cases."

---

## Interviewer's Final Assessment

**Hire Recommendation: Strong Hire (L6 -- solid Senior SDE)**

| Dimension | Rating | Notes |
|---|---|---|
| **Problem Decomposition** | Exceeds Bar | Clean 3-layer architecture with clear separation of provisioning/controller/capacity. Evolved organically from single host. |
| **Requirements & Scoping** | Exceeds Bar | Proactively distinguished all ECS abstractions (task def vs task vs service), quantified published quotas, explained control plane / data plane decoupling. |
| **API Design** | Meets Bar | Well-structured APIs with design rationale (two IAM roles, essential containers, deployment config tradeoffs). |
| **Control Plane** | Exceeds Bar | Detailed state store schema, agent communication model, service scheduler reconciliation loop with rate limiting. |
| **Task Placement** | Exceeds Bar | Full 4-step pipeline, concrete examples for each strategy, combined strategy explanation, comparison with K8s scheduling. |
| **Fargate Architecture** | Exceeds Bar | Firecracker microVM explanation was strong -- isolation model, comparison with containers and traditional VMs, capacity management reasoning. |
| **Networking** | Exceeds Bar | All three modes with diagrams and tradeoffs. ENI trunking, service discovery integration, A vs SRV record distinction. |
| **Capacity Providers** | Exceeds Bar | Managed scaling with CapacityProviderReservation metric, managed termination protection flow, multi-provider strategy with weight/base. |
| **Deployments** | Exceeds Bar | Rolling update with minHealthy/maxPercent sequences, blue/green with CodeDeploy and traffic shifting strategies, circuit breaker. |
| **Monitoring** | Meets Bar | Three-layer health checks with precedence, EventBridge events, Container Insights. |
| **Operational Maturity** | Exceeds Bar | "What keeps you up at night" covered scheduler thundering herd, state consistency under concurrency, Fargate cold start tail, blast radius, agent version skew, ENI exhaustion. |
| **Communication** | Exceeds Bar | Structured with tables and diagrams, evolved architecture iteratively, always explained the "why" before the "what." |
| **LP: Dive Deep** | Exceeds Bar | Went deep on Firecracker isolation, placement pipeline internals, and deployment state machine without prompting. |
| **LP: Think Big** | Meets Bar | Final answer on image caching, predictive pre-warming, and snapshot-based fast start showed forward-looking vision. |
| **LP: Are Right, A Lot** | Exceeds Bar | Correctly identified ECS-specific patterns (not K8s), cited published quotas, marked inferences appropriately. |

**What would push this to L7:**
- Deeper discussion of cell-based architecture for the control plane -- how ECS partitions its own infrastructure for blast radius isolation
- Proposing a formal SLA model: how control plane availability SLA maps to customer-visible impact
- Discussing multi-region ECS architecture and how task definitions, services, and clusters interact with regional boundaries
- Cost modeling: $/vCPU-hour for EC2 vs Fargate vs Fargate Spot, and how to build a capacity strategy optimizer
- Deeper treatment of the consistency model: exactly-once task scheduling guarantees, idempotency of the scheduler, and how to handle split-brain scenarios (agent thinks task is running, control plane thinks it's stopped)

---

## Key Differences: L5 vs L6 vs L7 Expectations for This Problem

| Aspect | L5 (SDE-2) | L6 (SDE-3) | L7 (Principal) |
|---|---|---|---|
| **Requirements** | Lists task/service concepts, mentions scaling | Quantifies published quotas, explains control plane/data plane decoupling, asks about networking and deployment modes | Frames requirements around blast radius, SLA commitments, multi-tenancy cost model, and operational runbooks |
| **Architecture** | Correct 3-layer design | Justifies each layer's separation, explains agent communication model, identifies state store as a critical component | Discusses control plane cell architecture, cross-AZ state replication, how the architecture enables independent deployments of each layer |
| **Task Placement** | "Spread tasks across instances" | Full placement pipeline with concrete examples, combined strategies, comparison to K8s | Discusses the NP-hardness of optimal placement, how ECS trades optimality for speed, and how placement interacts with capacity scaling signals |
| **Fargate** | "Serverless containers" | Firecracker microVMs, isolation model, capacity pool management | Discusses the Fargate fleet management economy, overcommit vs no-overcommit, and how Fargate Spot pricing signals excess capacity |
| **Networking** | "Use awsvpc" | All modes with tradeoffs, ENI trunking, service discovery record types | Discusses VPC-level impact (IP exhaustion, ENI quotas), how networking mode choice affects security posture, and ECS Service Connect (Envoy mesh) |
| **Deployments** | "Rolling update" | Both strategies with sequences, circuit breaker, traffic shifting | Discusses deployment as a state machine, edge cases (AZ failure mid-deploy), and how to build a deployment safety scoring system |
| **Capacity** | "Auto-scale instances" | Capacity providers with managed scaling, termination protection, multi-provider strategies | Discusses cost optimization loops, how to model reserved vs on-demand vs spot capacity, and Savings Plans interaction |
| **Operational Thinking** | Mentions monitoring | Identifies specific failure modes (scheduler thundering herd, state consistency, ENI exhaustion) | Proposes cell-based isolation, formal game-day scenarios, automated remediation runbooks, and chaos engineering for the control plane |
| **Communication** | Responds to questions | Drives the conversation, uses diagrams and tables, always explains "why" | Negotiates scope proactively, proposes phased deep dives based on interview time, connects design decisions to business impact |

---

*For detailed deep dives on each component, see the companion documents:*
- Architecture overview and three-layer model
- Task placement strategies and constraints
- Fargate architecture and Firecracker isolation
- Networking modes (awsvpc, bridge, host) and service discovery
- Capacity providers and auto scaling
- Deployment strategies (rolling, blue/green, circuit breaker)
- Monitoring, health checks, and operational concerns

*End of interview simulation.*
