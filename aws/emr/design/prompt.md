Design Amazon EMR (Elastic MapReduce) as a system design interview simulation.

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
**L6 SDE-3 (Senior SDE)** candidate being interviewed by a Principal Engineer (L8) on the EMR team.

The candidate's answers should demonstrate L6-level depth:
- Not just "what" but "why" and "why not the alternative"
- Quantitative reasoning (back-of-envelope math, concrete numbers)
- Awareness of operational concerns (monitoring, failure modes, blast radius)
- Proactive identification of tradeoffs before the interviewer asks

The L5/L6/L7 rubric tables after each phase should make it crystal clear how an L5 answer differs from L6 differs from L7.

## Output location
Create all files under: src/hld/aws/emr/design/

Files to create:
1. interview-simulation.md — the main backbone
2. Supporting deep-dive docs — adapt topics for EMR

## CRITICAL: Accuracy requirements

I DO NOT TRUST AI-GENERATED FACTS ABOUT SPECIFIC AWS SERVICES. Every concrete claim about EMR must be verifiable against official AWS documentation. Specifically:

1. **Use WebSearch and WebFetch tools** to look up AWS EMR official documentation BEFORE writing. Search for:
   - "AWS EMR developer guide site:docs.aws.amazon.com"
   - "AWS EMR architecture site:docs.aws.amazon.com"
   - "AWS EMR on EKS site:docs.aws.amazon.com"
   - "AWS EMR Serverless site:docs.aws.amazon.com"
   - "AWS EMR cluster types master core task nodes"
   - "AWS EMR instance fleets vs instance groups"
   - "AWS EMR HDFS vs EMRFS vs S3"
   - "AWS EMR managed scaling"
   - "AWS EMR Spark on EMR"
   - "AWS EMR step execution"
   - "Amazon EMR architecture re:Invent"
   - "AWS EMR quotas and limits site:docs.aws.amazon.com"

2. **For every concrete number** (max nodes, instance type limits, step limits), verify against docs.aws.amazon.com. If you cannot verify a number, explicitly write "[UNVERIFIED — check AWS docs]" next to it.

3. **For every claim about EMR internals** (how EMRFS works, how managed scaling decisions are made, how YARN is configured), if it's not from an official AWS source, mark it as "[INFERRED — not officially documented]".

4. **DO NOT confuse EMR with raw Hadoop/Spark.** EMR adds significant AWS-specific layers on top of open-source frameworks. Be clear about what's EMR-specific vs what's vanilla Hadoop/Spark.

## Key EMR topics to cover

### Requirements & Scale
- Managed big data processing platform — run Spark, Hive, Presto, HBase at scale
- Cluster lifecycle: transient (job-specific) vs long-running clusters
- Three deployment modes: EMR on EC2, EMR on EKS, EMR Serverless
- Processing petabytes of data, thousands of nodes

### Architecture deep dives
- **Cluster architecture**: Primary (master) node, core nodes (HDFS + compute), task nodes (compute only). Why this separation matters for cost and data durability.
- **Storage layer**: HDFS (local, ephemeral) vs EMRFS (S3-backed, durable). Consistent view. When to use which. How EMRFS decouples storage from compute.
- **Resource management**: YARN for resource negotiation. How YARN allocates containers to applications. Capacity scheduler vs fair scheduler.
- **Managed scaling**: How EMR auto-scales core and task nodes based on YARN metrics. Scale-up speed vs scale-down graceful decommissioning. Spot instance integration for task nodes.
- **Instance fleets vs instance groups**: How EMR handles heterogeneous hardware. Spot instance diversification. On-demand vs spot allocation strategies.
- **EMR Serverless**: How it differs from EMR on EC2. Pre-initialized capacity. Auto-scaling per application. No cluster management.
- **Job/Step execution**: Step framework. Concurrent steps. Cluster auto-termination after last step. Action on failure (continue vs terminate).
- **Data locality and shuffle**: How Spark shuffle works on EMR. External shuffle service. S3 shuffle plugin. Performance implications.

### Design evolution (iterative build-up)
- Attempt 0: Run Spark on a single machine
- Attempt 1: Distribute across a cluster — need YARN for resource management, HDFS for shared storage
- Attempt 2: Managed cluster — provision EC2 instances, install frameworks, handle failures
- Then: How to decouple storage from compute (EMRFS/S3)? How to handle spot interruptions? How to auto-scale based on workload? How to go serverless (EMR Serverless)?

### Key tradeoffs
- HDFS vs S3 (EMRFS): data locality vs durability and elasticity
- Transient vs long-running clusters: cost vs startup latency
- Core nodes vs task nodes: data durability vs cost (spot instances on task nodes)
- EMR on EC2 vs EMR Serverless: control vs simplicity

## What NOT to do
- Do NOT describe vanilla Hadoop architecture as if it's EMR-specific
- Do NOT make up EMR-specific limits without verification
- Do NOT skip the iterative build-up (Attempt 0 → 1 → 2 → ...)
- Do NOT write a Wikipedia article. This is an INTERVIEW SIMULATION with dialogue.
