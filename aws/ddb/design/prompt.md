Design Amazon DynamoDB as a system design interview simulation.

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
**L6 SDE-3 (Senior SDE)** candidate being interviewed by a Principal Engineer (L8) on the DynamoDB team.

The candidate's answers should demonstrate L6-level depth:
- Not just "what" but "why" and "why not the alternative"
- Quantitative reasoning (back-of-envelope math, concrete numbers)
- Awareness of operational concerns (monitoring, failure modes, blast radius)
- Proactive identification of tradeoffs before the interviewer asks

The L5/L6/L7 rubric tables after each phase should make it crystal clear how an L5 answer differs from L6 differs from L7.

## Output location
Create all files under: src/hld/aws/ddb/design/

Files to create:
1. interview-simulation.md — the main backbone
2. Supporting deep-dive docs — adapt topics for DynamoDB

## CRITICAL: Accuracy requirements

I DO NOT TRUST AI-GENERATED FACTS ABOUT SPECIFIC AWS SERVICES. Every concrete claim about DynamoDB must be verifiable against official AWS documentation. Specifically:

1. **Use WebSearch and WebFetch tools** to look up AWS DynamoDB official documentation BEFORE writing. Search for:
   - "AWS DynamoDB developer guide site:docs.aws.amazon.com"
   - "AWS DynamoDB quotas and limits site:docs.aws.amazon.com"
   - "AWS DynamoDB partitioning site:docs.aws.amazon.com"
   - "AWS DynamoDB consistent reads site:docs.aws.amazon.com"
   - "AWS DynamoDB global tables site:docs.aws.amazon.com"
   - "AWS DynamoDB transactions site:docs.aws.amazon.com"
   - "AWS DynamoDB streams site:docs.aws.amazon.com"
   - "AWS DynamoDB adaptive capacity"
   - "AWS DynamoDB Paxos replication"
   - "Amazon DynamoDB architecture re:Invent"
   - "Amazon DynamoDB 2022 paper USENIX ATC"
   - "Amazon Dynamo paper 2007 vs DynamoDB differences"

2. **For every concrete number** (item size limits, RCU/WCU calculations, partition throughput, GSI limits), verify against docs.aws.amazon.com. If you cannot verify a number, explicitly write "[UNVERIFIED — check AWS docs]" next to it.

3. **For every claim about DynamoDB internals** (replication model, storage engine, partition management), if it's not from an official AWS source, mark it as "[INFERRED — not officially documented]".

4. **CRITICAL DISTINCTION: Amazon Dynamo paper (2007) vs AWS DynamoDB (the service).** These are DIFFERENT systems:
   - Dynamo paper (2007): leaderless replication, vector clocks, sloppy quorum, consistent hashing
   - DynamoDB (the service): leader-per-partition with Paxos, strong consistency opt-in, managed partitioning
   - DO NOT mix these up. If referencing the Dynamo paper, say "the original Dynamo paper" explicitly.

## Key DynamoDB topics to cover

### Requirements & Scale
- Key-value and document store with single-digit millisecond latency at any scale
- Partition key and sort key model
- On-demand vs provisioned capacity modes
- RCU/WCU capacity model and calculations
- Item size limit, table size (unlimited), partition throughput

### Architecture deep dives
- **Partitioning**: How data is distributed across partitions using partition key hash. How partitions split (by size: 10 GB, or by throughput). Partition map / request router.
- **Replication**: Leader-per-partition with Paxos. 3 replicas across AZs. Writes go through leader, majority ACK. Reads: eventually consistent (any replica) vs strongly consistent (leader only, 2x RCU).
- **Storage engine**: B-tree based storage on SSDs. WAL (write-ahead log) for durability.
- **Global Secondary Indexes (GSI)**: Asynchronous replication from base table to GSI. Eventually consistent only. GSI as a separate "table" with its own partitions.
- **Local Secondary Indexes (LSI)**: Same partition as base table, different sort key. Shares throughput with base table. 10 GB partition limit.
- **DynamoDB Streams**: Change data capture. Ordered by partition key. Kinesis-like shard model. 24-hour retention.
- **Global Tables**: Multi-region, multi-leader replication. Last-writer-wins conflict resolution. Async replication.
- **Transactions**: TransactWriteItems / TransactGetItems. ACID across up to 100 items. Two-phase protocol internally.
- **Adaptive capacity**: Burst capacity, adaptive capacity to handle hot partitions.

### Design evolution (iterative build-up)
- Attempt 0: Single server with a hash map
- Attempt 1: Add persistence (WAL + SSTable/B-tree)
- Attempt 2: Add replication (Paxos, 3 replicas across AZs)
- Then: How to scale beyond one machine? Partitioning by hash of partition key. Request router / partition map. How to handle hot keys? Adaptive capacity. How to add secondary access patterns? GSIs.

### Consistency & Replication
- Leader-based replication with Paxos (NOT leaderless like the Dynamo paper)
- Eventually consistent reads (default) vs strongly consistent reads (opt-in, from leader, 2x RCU)
- Global tables: multi-leader across regions, last-writer-wins, eventual cross-region consistency
- Transactions: serializable isolation for transactional operations

## What NOT to do
- Do NOT confuse the Dynamo paper with DynamoDB the service
- Do NOT claim DynamoDB uses leaderless replication or vector clocks (it doesn't)
- Do NOT make up partition throughput numbers without verification
- Do NOT skip the iterative build-up (Attempt 0 → 1 → 2 → ...)
- Do NOT write a Wikipedia article. This is an INTERVIEW SIMULATION with dialogue.
