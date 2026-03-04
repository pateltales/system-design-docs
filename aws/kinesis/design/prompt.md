Design Amazon Kinesis Data Streams as a system design interview simulation.

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
**L6 SDE-3 (Senior SDE)** candidate being interviewed by a Principal Engineer (L8) on the Kinesis team.

The candidate's answers should demonstrate L6-level depth:
- Not just "what" but "why" and "why not the alternative"
- Quantitative reasoning (back-of-envelope math, concrete numbers)
- Awareness of operational concerns (monitoring, failure modes, blast radius)
- Proactive identification of tradeoffs before the interviewer asks

The L5/L6/L7 rubric tables after each phase should make it crystal clear how an L5 answer differs from L6 differs from L7.

## Output location
Create all files under: src/hld/aws/kinesis/design/

Files to create:
1. interview-simulation.md — the main backbone
2. Supporting deep-dive docs — adapt topics for Kinesis

## CRITICAL: Accuracy requirements

I DO NOT TRUST AI-GENERATED FACTS ABOUT SPECIFIC AWS SERVICES. Every concrete claim about Kinesis must be verifiable against official AWS documentation. Specifically:

1. **Use WebSearch and WebFetch tools** to look up AWS Kinesis official documentation BEFORE writing. Search for:
   - "AWS Kinesis Data Streams developer guide site:docs.aws.amazon.com"
   - "AWS Kinesis quotas and limits site:docs.aws.amazon.com"
   - "AWS Kinesis shard model site:docs.aws.amazon.com"
   - "AWS Kinesis partition key site:docs.aws.amazon.com"
   - "AWS Kinesis retention period site:docs.aws.amazon.com"
   - "AWS Kinesis enhanced fan-out site:docs.aws.amazon.com"
   - "AWS Kinesis KCL checkpointing site:docs.aws.amazon.com"
   - "AWS Kinesis on-demand mode vs provisioned"
   - "AWS Kinesis vs Kafka comparison"
   - "AWS Kinesis resharding split merge"
   - "Amazon Kinesis architecture re:Invent"

2. **For every concrete number** (records per shard per second, MB/s per shard, max record size, retention limits, shard limits), verify against docs.aws.amazon.com. If you cannot verify a number, explicitly write "[UNVERIFIED — check AWS docs]" next to it.

3. **For every claim about Kinesis internals** (how shards are stored, replication model, how enhanced fan-out works internally), if it's not from an official AWS source, mark it as "[INFERRED — not officially documented]".

4. **DO NOT confuse Kinesis with Apache Kafka.** While both are streaming platforms, their architectures differ significantly. Kinesis uses shards (not partitions), has different scaling models, and different consumer models. If comparing to Kafka, be explicit about differences.

## Key Kinesis topics to cover

### Requirements & Scale
- Real-time streaming data ingestion and processing
- Shard model: each shard = unit of throughput capacity
- On-demand vs provisioned capacity modes
- Record ordering within a shard (by partition key)
- Retention: default, extended, long-term retention options
- Multiple consumers reading the same stream independently

### Architecture deep dives
- **Shard model**: Each shard handles X MB/s write, Y MB/s read, Z records/sec. Hash key range partitioning. How partition keys map to shards via MD5 hash. Why shard is the fundamental scaling unit.
- **Data ingestion (producers)**: PutRecord vs PutRecords (batch). Partition key selection and hot shard problem. Producer retries and sequence numbers. KPL (Kinesis Producer Library) aggregation and batching.
- **Data consumption (consumers)**: GetRecords polling vs enhanced fan-out (push via SubscribeToShard with HTTP/2). Shared throughput vs dedicated throughput per consumer. KCL (Kinesis Client Library) — lease table in DynamoDB, shard assignment, checkpointing.
- **Ordering guarantees**: Strict ordering within a shard by sequence number. No ordering across shards. How partition key determines shard placement.
- **Resharding**: Split and merge operations. How resharding affects consumers (parent/child shard transitions). Why resharding is slow and disruptive. On-demand mode automates this.
- **Retention and replay**: Default 24h, up to 365 days. How consumers can replay from any point using sequence numbers or timestamps. TRIM_HORIZON, LATEST, AT_TIMESTAMP iterators.
- **Durability**: Data replicated across 3 AZs synchronously. Once PutRecord returns success, data is durable.
- **Enhanced fan-out**: Dedicated 2 MB/s per shard per consumer. Push-based via HTTP/2. Why this matters for multiple consumers on the same stream.

### Design evolution (iterative build-up)
- Attempt 0: Single server receiving events, writing to a log file
- Attempt 1: Need durability — replicate the log across AZs
- Attempt 2: Need throughput — partition the log into shards by hash key
- Then: How do consumers track their position (checkpointing)? How to scale shards (resharding)? How to support multiple independent consumers? How to handle hot shards?

### Key tradeoffs
- Provisioned vs on-demand: cost predictability vs auto-scaling
- Polling (GetRecords) vs enhanced fan-out: cost vs latency and throughput isolation
- Fewer large shards vs many small shards: cost vs parallelism
- Kinesis vs SQS: stream processing (ordered replay) vs message queue (delete after processing)
- Kinesis vs Kafka (MSK): managed simplicity vs ecosystem and configurability

## What NOT to do
- Do NOT apply Kafka internals (ISR, controller broker, consumer groups with partition assignment) to Kinesis
- Do NOT make up shard throughput numbers without verification
- Do NOT skip the iterative build-up (Attempt 0 → 1 → 2 → ...)
- Do NOT write a Wikipedia article. This is an INTERVIEW SIMULATION with dialogue.
