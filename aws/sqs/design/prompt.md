Design Amazon SQS (Simple Queue Service) as a system design interview simulation.

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
**L6 SDE-3 (Senior SDE)** candidate being interviewed by a Principal Engineer (L8) on the SQS team.

The candidate's answers should demonstrate L6-level depth:
- Not just "what" but "why" and "why not the alternative"
- Quantitative reasoning (back-of-envelope math, concrete numbers)
- Awareness of operational concerns (monitoring, failure modes, blast radius)
- Proactive identification of tradeoffs before the interviewer asks

The L5/L6/L7 rubric tables after each phase should make it crystal clear how an L5 answer differs from L6 differs from L7. For example:
- L5 might say "use a distributed queue"
- L6 would explain the partitioning strategy, why it works, and what breaks
- L7 would discuss partition rebalancing, hot-partition mitigation, and cross-AZ latency implications

## Output location
Create all files under: src/hld/aws/sqs/design/

Files to create:
1. interview-simulation.md — the main backbone (like S3's)
2. Supporting deep-dive docs (similar to S3's api-contracts.md, metadata-and-indexing.md, etc.) — adapt topics for SQS

## CRITICAL: Accuracy requirements

I DO NOT TRUST AI-GENERATED FACTS ABOUT SPECIFIC AWS SERVICES. Every concrete claim about SQS must be verifiable against official AWS documentation. Specifically:

1. **Use WebSearch and WebFetch tools** to look up AWS SQS official documentation BEFORE writing. Search for:
   - "AWS SQS developer guide site:docs.aws.amazon.com"
   - "AWS SQS quotas and limits site:docs.aws.amazon.com"
   - "AWS SQS FIFO queues site:docs.aws.amazon.com"
   - "AWS SQS visibility timeout site:docs.aws.amazon.com"
   - "AWS SQS architecture"
   - "AWS SQS at-least-once delivery"
   - "AWS SQS exactly-once processing FIFO"
   - "AWS SQS long polling short polling"
   - "AWS SQS message retention"
   - "AWS SQS dead letter queue"
   - "AWS re:Invent SQS architecture talk"

2. **For every concrete number** (message size limits, retention periods, throughput limits, visibility timeout ranges), verify against docs.aws.amazon.com. If you cannot verify a number, explicitly write "[UNVERIFIED — check AWS docs]" next to it.

3. **For every claim about SQS internals** (replication model, storage backend, consistency model), if it's not from an official AWS source (blog post, re:Invent talk, documentation), mark it as "[INFERRED — not officially documented]". SQS internals are less publicly documented than S3's, so be honest about what's known vs speculated.

4. **DO NOT confuse SQS with other message queues.** Do not apply Kafka, RabbitMQ, or ActiveMQ internals to SQS. If you're inferring SQS architecture, say so explicitly.

## Key SQS topics to cover

### Requirements & Scale
- Standard queues vs FIFO queues (different guarantees, different limits)
- Message size limits, retention periods, throughput limits
- Delivery guarantees: at-least-once (standard) vs exactly-once processing (FIFO)
- Ordering: best-effort (standard) vs strict within message group (FIFO)

### Architecture deep dives
- How messages are stored and replicated across AZs (SQS replicates messages across multiple AZs for durability)
- Visibility timeout mechanism — how it works internally
- Long polling vs short polling — what happens at the infrastructure level
- Dead letter queues and redrive policy
- FIFO deduplication and message group ID — how ordering is enforced
- Delay queues
- Message lifecycle: send → store → receive → process → delete

### Design evolution (iterative build-up)
Start simple, find problems, evolve — same pattern as S3:
- Attempt 0: Single server with an in-memory queue
- Attempt 1: Add persistence (what if server crashes?)
- Attempt 2: Add replication (durability across AZs)
- Then: How to scale to millions of queues? How to handle hot queues? How to guarantee ordering in FIFO? How to implement visibility timeout across distributed consumers?

### Scaling & Performance
- Partitioning strategies for standard queues
- How FIFO queues limit throughput (and why)
- Batching (SendMessageBatch, ReceiveMessage MaxNumberOfMessages)
- Connection patterns (long polling reduces empty responses)

### Consistency & Delivery Guarantees
- Why standard queues are at-least-once (not exactly-once)
- How FIFO queues achieve exactly-once processing (dedup with MessageDeduplicationId)
- What "best-effort ordering" means in standard queues
- The distributed systems reason why you can't have unlimited throughput + strict ordering + exactly-once all at the same time

## What NOT to do
- Do NOT make up SQS-specific numbers without verification
- Do NOT claim "SQS uses Kafka internally" or any such unverified internal claim
- Do NOT skip the iterative build-up (Attempt 0 → 1 → 2 → ...). This is the most important part of the format.
- Do NOT write a Wikipedia article. This is an INTERVIEW SIMULATION with dialogue.
