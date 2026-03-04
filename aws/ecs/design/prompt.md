Design Amazon ECS (Elastic Container Service) as a system design interview simulation.

## Template
Follow the EXACT same format as the S3 interview simulation at:
src/hld/aws/s3/design/interview-simulation.md

Same structure:
- Interviewer/Candidate dialogue format
- PHASE 1: Opening & Problem Statement
- PHASE 2: Requirements Gathering & Scoping (functional + non-functional + scale numbers)
- PHASE 3: API Design
- PHASE 4: High-Level Architecture (iterative build-up: Attempt 0 → 1 → 2, starting from single server, finding problems, evolving)
- PHASE 5+: Deep dives on each component
- L5/L6/L7 rubric table after EACH deep dive phase — showing exactly how an L5, L6, and L7 candidate would answer differently. This is critical for understanding what "good" looks like at each level.
- Architecture evolution table after each phase
- Wrap-up phase with "what keeps you up at night"
- Links to supporting deep-dive docs

## Candidate Level
**L6 SDE-3 (Senior SDE)** candidate being interviewed by a Principal Engineer (L8) on the ECS team.

The candidate's answers should demonstrate L6-level depth:
- Not just "what" but "why" and "why not the alternative"
- Quantitative reasoning (back-of-envelope math, concrete numbers)
- Awareness of operational concerns (monitoring, failure modes, blast radius)
- Proactive identification of tradeoffs before the interviewer asks

The L5/L6/L7 rubric tables after each phase should make it crystal clear how an L5 answer differs from L6 differs from L7.

## Output location
Create all files under: src/hld/aws/ecs/design/

Files to create:
1. interview-simulation.md — the main backbone
2. Supporting deep-dive docs — adapt topics for ECS

## CRITICAL: Accuracy requirements

I DO NOT TRUST AI-GENERATED FACTS ABOUT SPECIFIC AWS SERVICES. Every concrete claim about ECS must be verifiable against official AWS documentation. Specifically:

1. **Use WebSearch and WebFetch tools** to look up AWS ECS official documentation BEFORE writing. Search for:
   - "AWS ECS developer guide site:docs.aws.amazon.com"
   - "AWS ECS architecture site:docs.aws.amazon.com"
   - "AWS ECS task definition site:docs.aws.amazon.com"
   - "AWS ECS service scheduler site:docs.aws.amazon.com"
   - "AWS ECS Fargate vs EC2 launch type"
   - "AWS ECS cluster auto scaling"
   - "AWS ECS service discovery"
   - "AWS ECS capacity providers"
   - "AWS ECS task placement strategies"
   - "AWS ECS blue/green deployments"
   - "Amazon ECS architecture re:Invent"
   - "AWS ECS quotas and limits site:docs.aws.amazon.com"

2. **For every concrete number** (max tasks per service, max containers per task, resource limits), verify against docs.aws.amazon.com. If you cannot verify a number, explicitly write "[UNVERIFIED — check AWS docs]" next to it.

3. **For every claim about ECS internals** (control plane architecture, scheduler implementation, state management), if it's not from an official AWS source, mark it as "[INFERRED — not officially documented]".

4. **DO NOT confuse ECS with Kubernetes/EKS.** ECS has its own scheduling model, task definition format, and architecture. Do not apply Kubernetes concepts (pods, kubelet, etcd) to ECS unless explicitly comparing them.

## Key ECS topics to cover

### Requirements & Scale
- Container orchestration: run, stop, and manage containers at scale
- Task definitions (blueprint) vs Tasks (running instance) vs Services (long-running tasks with desired count)
- EC2 launch type vs Fargate launch type — architectural differences
- Cluster management, capacity planning

### Architecture deep dives
- **Control plane**: How the ECS control plane manages cluster state. Task scheduling decisions. Service scheduler vs task placement.
- **Task placement**: Strategies (spread, binpack, random) and constraints (distinctInstance, memberOf). How the scheduler decides which instance gets a task.
- **Fargate architecture**: How Fargate provides serverless containers. Firecracker microVMs. Isolation model. How Fargate allocates compute without you managing instances.
- **Networking**: awsvpc mode (ENI per task), bridge mode, host mode. Service discovery via Cloud Map. Load balancer integration (ALB/NLB target groups).
- **Service scheduler**: Desired count, minimum healthy percent, maximum percent. Rolling updates. How ECS handles task failures and replacements.
- **Capacity providers**: Auto Scaling group capacity providers. Managed scaling. Managed termination protection. How ECS signals ASG to scale.
- **Deployments**: Rolling update, blue/green (with CodeDeploy), circuit breaker. How deployment state machine works.
- **Monitoring & Health**: Container health checks, ELB health checks, task state transitions, CloudWatch Container Insights.

### Design evolution (iterative build-up)
- Attempt 0: Run a container on a single EC2 instance manually
- Attempt 1: Multiple instances — need a scheduler to decide where to place containers
- Attempt 2: Add a control plane that tracks cluster state, schedules tasks, handles failures
- Then: How to handle instance failures? How to scale automatically? How to do zero-downtime deployments? How to isolate tenants (Fargate)?

### Key tradeoffs
- EC2 vs Fargate: control & cost efficiency vs operational simplicity
- Binpack vs spread placement: cost optimization vs availability
- Rolling update vs blue/green: simplicity vs safety
- awsvpc vs bridge networking: isolation vs ENI limits

## What NOT to do
- Do NOT apply Kubernetes architecture to ECS (no etcd, no kubelet, no kube-scheduler)
- Do NOT make up ECS-specific limits without verification
- Do NOT skip the iterative build-up (Attempt 0 → 1 → 2 → ...)
- Do NOT write a Wikipedia article. This is an INTERVIEW SIMULATION with dialogue.
