Design AWS Lambda as a system design interview simulation.

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
**L6 SDE-3 (Senior SDE)** candidate being interviewed by a Principal Engineer (L8) on the Lambda team.

The candidate's answers should demonstrate L6-level depth:
- Not just "what" but "why" and "why not the alternative"
- Quantitative reasoning (back-of-envelope math, concrete numbers)
- Awareness of operational concerns (monitoring, failure modes, blast radius)
- Proactive identification of tradeoffs before the interviewer asks

The L5/L6/L7 rubric tables after each phase should make it crystal clear how an L5 answer differs from L6 differs from L7.

## Output location
Create all files under: src/hld/aws/lambda/design/

Files to create:
1. interview-simulation.md — the main backbone
2. Supporting deep-dive docs — adapt topics for Lambda

## CRITICAL: Accuracy requirements

I DO NOT TRUST AI-GENERATED FACTS ABOUT SPECIFIC AWS SERVICES. Every concrete claim about Lambda must be verifiable against official AWS documentation. Specifically:

1. **Use WebSearch and WebFetch tools** to look up AWS Lambda official documentation BEFORE writing. Search for:
   - "AWS Lambda developer guide site:docs.aws.amazon.com"
   - "AWS Lambda quotas and limits site:docs.aws.amazon.com"
   - "AWS Lambda execution environment lifecycle site:docs.aws.amazon.com"
   - "AWS Lambda cold start site:docs.aws.amazon.com"
   - "AWS Lambda Firecracker microVM"
   - "AWS Lambda SnapStart site:docs.aws.amazon.com"
   - "AWS Lambda concurrency reserved provisioned"
   - "AWS Lambda event source mappings site:docs.aws.amazon.com"
   - "AWS Lambda scaling behavior site:docs.aws.amazon.com"
   - "AWS Lambda networking VPC site:docs.aws.amazon.com"
   - "AWS Lambda architecture re:Invent Firecracker"
   - "Firecracker microVM paper NSDI"

2. **For every concrete number** (memory limits, timeout limits, deployment package size, concurrency limits, burst concurrency), verify against docs.aws.amazon.com. If you cannot verify a number, explicitly write "[UNVERIFIED — check AWS docs]" next to it.

3. **For every claim about Lambda internals** (Firecracker architecture, worker fleet management, sandbox lifecycle, cold start optimization), if it's not from an official AWS source (Firecracker paper, re:Invent talks, AWS blog), mark it as "[INFERRED — not officially documented]". Lambda's internals are well-documented compared to many services thanks to the Firecracker paper and re:Invent talks.

4. **DO NOT confuse Lambda with general container orchestration.** Lambda has a unique execution model (invocation-based, not long-running). Don't apply ECS/Kubernetes patterns to Lambda.

## Key Lambda topics to cover

### Requirements & Scale
- Serverless compute: run code without managing servers
- Event-driven: invoked by triggers (API Gateway, S3 events, SQS, Kinesis, etc.)
- Pay per invocation and compute time (GB-seconds)
- Auto-scales from 0 to thousands of concurrent executions
- Sub-second startup (warm) to seconds (cold start)

### Architecture deep dives
- **Execution environment lifecycle**: Init (cold start) → Invoke → Shutdown. Sandbox reuse for warm starts. How Lambda keeps sandboxes alive between invocations.
- **Firecracker microVMs**: What Firecracker is (lightweight VMM). Why microVMs over containers (multi-tenant security isolation). Boot time (~125ms). Memory overhead. How Firecracker enables safe multi-tenancy on shared hardware.
- **Worker fleet management**: How Lambda manages a fleet of worker hosts. Placement decisions. How it pre-warms sandboxes. Sandbox pooling.
- **Cold start deep dive**: What happens during a cold start — download code, start microVM, init runtime, run init code. Strategies to reduce: SnapStart (checkpoint/restore), provisioned concurrency, keeping functions warm.
- **Scaling model**: Synchronous invocations scale with concurrent requests. Burst concurrency limits (initial burst, then gradual scaling). How Lambda decides to create new sandboxes vs reuse existing ones.
- **Event source mappings**: How Lambda polls SQS/Kinesis/DynamoDB Streams. Batching. Parallelization factor. Bisect on error. How the poller scales.
- **Networking**: VPC-attached Lambda (Hyperplane ENI, shared across functions). How cold start for VPC Lambda was solved (pre-created ENIs).
- **Layers and extensions**: How layers work (shared code/libraries mounted into sandbox). Extensions (sidecar processes for monitoring, security).
- **SnapStart**: Checkpoint/restore using Firecracker snapshots. How it eliminates init-phase cold start for Java. Uniqueness concerns (random seeds, connections).

### Design evolution (iterative build-up)
- Attempt 0: One server, receives HTTP request, runs a function, returns response
- Attempt 1: Need multi-tenancy — can't run untrusted user code in the same process. Need isolation (containers? VMs?)
- Attempt 2: Firecracker microVMs — lightweight VM-level isolation with container-like speed
- Then: How to scale to millions of concurrent functions? Worker fleet management. How to reduce cold starts? Sandbox caching, SnapStart. How to handle event sources? Poller architecture.

### Key tradeoffs
- Cold start latency vs cost (provisioned concurrency)
- MicroVM vs container isolation: security vs overhead
- Synchronous vs asynchronous invocation: latency vs reliability
- Monolithic Lambda vs many small Lambdas: simplicity vs blast radius
- Lambda vs ECS/Fargate: invocation-based vs long-running

## What NOT to do
- Do NOT confuse Lambda with container orchestration (ECS/K8s)
- Do NOT make up Lambda-specific limits without verification
- Do NOT skip the iterative build-up (Attempt 0 → 1 → 2 → ...)
- Do NOT ignore Firecracker — it's the core innovation that makes Lambda work
- Do NOT write a Wikipedia article. This is an INTERVIEW SIMULATION with dialogue.
