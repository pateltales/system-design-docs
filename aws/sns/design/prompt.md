Design Amazon SNS (Simple Notification Service) as a system design interview simulation.

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
**L6 SDE-3 (Senior SDE)** candidate being interviewed by a Principal Engineer (L8) on the SNS team.

The candidate's answers should demonstrate L6-level depth:
- Not just "what" but "why" and "why not the alternative"
- Quantitative reasoning (back-of-envelope math, concrete numbers)
- Awareness of operational concerns (monitoring, failure modes, blast radius)
- Proactive identification of tradeoffs before the interviewer asks

The L5/L6/L7 rubric tables after each phase should make it crystal clear how an L5 answer differs from L6 differs from L7.

## Output location
Create all files under: src/hld/aws/sns/design/

Files to create:
1. interview-simulation.md — the main backbone
2. Supporting deep-dive docs — adapt topics for SNS

## CRITICAL: Accuracy requirements

I DO NOT TRUST AI-GENERATED FACTS ABOUT SPECIFIC AWS SERVICES. Every concrete claim about SNS must be verifiable against official AWS documentation. Specifically:

1. **Use WebSearch and WebFetch tools** to look up AWS SNS official documentation BEFORE writing. Search for:
   - "AWS SNS developer guide site:docs.aws.amazon.com"
   - "AWS SNS quotas and limits site:docs.aws.amazon.com"
   - "AWS SNS message filtering site:docs.aws.amazon.com"
   - "AWS SNS FIFO topics site:docs.aws.amazon.com"
   - "AWS SNS message delivery retries site:docs.aws.amazon.com"
   - "AWS SNS fanout pattern SQS"
   - "AWS SNS dead letter queue"
   - "AWS SNS message attributes"
   - "AWS SNS mobile push notifications"
   - "AWS SNS SMS site:docs.aws.amazon.com"
   - "Amazon SNS architecture re:Invent"

2. **For every concrete number** (max subscriptions per topic, message size limit, throughput limits for FIFO topics), verify against docs.aws.amazon.com. If you cannot verify a number, explicitly write "[UNVERIFIED — check AWS docs]" next to it.

3. **For every claim about SNS internals** (how fan-out is implemented, how message filtering works internally, how delivery retries work), if it's not from an official AWS source, mark it as "[INFERRED — not officially documented]".

4. **DO NOT confuse SNS with SQS.** SNS is pub/sub (push-based, fan-out to multiple subscribers). SQS is a queue (pull-based, one consumer processes each message). They are often used together (SNS → SQS fan-out) but are architecturally different.

## Key SNS topics to cover

### Requirements & Scale
- Pub/sub messaging: one message published to a topic, delivered to all subscribers
- Subscriber types: SQS, Lambda, HTTP/S endpoints, email, SMS, mobile push
- Standard topics (high throughput, at-least-once, best-effort ordering) vs FIFO topics (strict ordering, exactly-once, limited throughput)
- Fan-out: one publish → thousands of subscribers
- Message filtering: subscribers receive only messages matching their filter policy

### Architecture deep dives
- **Pub/sub model**: Topics, subscriptions, publishers. How a single Publish call fans out to N subscribers in parallel. Delivery to heterogeneous endpoint types.
- **Fan-out architecture**: How SNS delivers to potentially thousands of subscribers. Parallel delivery. How it handles slow subscribers without blocking fast ones. Backpressure.
- **Message filtering**: Filter policies on subscription attributes. How filtering reduces unnecessary delivery (filter at SNS side, not subscriber side). String, numeric, prefix matching.
- **Delivery retries and DLQ**: Retry policies per delivery protocol (HTTP, SQS, Lambda have different retry behavior). Exponential backoff. Dead letter queues for failed deliveries. How SNS tracks delivery failures.
- **SNS + SQS fan-out pattern**: The most common architecture pattern. One topic → multiple SQS queues. Why this decouples producers from consumers. How message filtering reduces per-queue traffic.
- **FIFO topics**: Ordering by message group ID. Deduplication. Strict ordering guarantees. Throughput limits. Only SQS FIFO queues as subscribers. How FIFO topics relate to FIFO queues.
- **Mobile push notifications**: Platform endpoints (APNS, GCM/FCM). Device tokens. Platform applications. How SNS abstracts multi-platform push.
- **Message durability**: Messages stored across multiple AZs before returning success. If all delivery attempts fail, message goes to DLQ (if configured) or is lost.

### Design evolution (iterative build-up)
- Attempt 0: Direct point-to-point notification (publisher calls each subscriber directly)
- Attempt 1: Introduce a topic as an intermediary — publisher sends once, topic fans out
- Attempt 2: Need durability — store message before fan-out, replicate across AZs
- Then: How to handle thousands of subscribers without blocking? Parallel async delivery. How to filter messages per subscriber? How to handle failed deliveries? Retry + DLQ. How to guarantee ordering? FIFO topics.

### Key tradeoffs
- SNS (push/fan-out) vs SQS (pull/queue): when to use which
- Standard vs FIFO topics: throughput vs ordering guarantees
- Message filtering at SNS vs filtering at subscriber: efficiency vs flexibility
- At-least-once delivery: subscribers must be idempotent
- SNS + SQS vs EventBridge: simple fan-out vs content-based routing with richer filtering

## What NOT to do
- Do NOT confuse SNS with SQS — they solve different problems
- Do NOT make up SNS-specific limits without verification
- Do NOT skip the iterative build-up (Attempt 0 → 1 → 2 → ...)
- Do NOT ignore the SNS + SQS fan-out pattern — it's the most important architecture pattern for SNS
- Do NOT write a Wikipedia article. This is an INTERVIEW SIMULATION with dialogue.
