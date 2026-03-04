Design Amazon Redshift as a system design interview simulation.

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
**L6 SDE-3 (Senior SDE)** candidate being interviewed by a Principal Engineer (L8) on the Redshift team.

The candidate's answers should demonstrate L6-level depth:
- Not just "what" but "why" and "why not the alternative"
- Quantitative reasoning (back-of-envelope math, concrete numbers)
- Awareness of operational concerns (monitoring, failure modes, blast radius)
- Proactive identification of tradeoffs before the interviewer asks

The L5/L6/L7 rubric tables after each phase should make it crystal clear how an L5 answer differs from L6 differs from L7.

## Output location
Create all files under: src/hld/aws/redshift/design/

Files to create:
1. interview-simulation.md — the main backbone
2. Supporting deep-dive docs — adapt topics for Redshift

## CRITICAL: Accuracy requirements

I DO NOT TRUST AI-GENERATED FACTS ABOUT SPECIFIC AWS SERVICES. Every concrete claim about Redshift must be verifiable against official AWS documentation. Specifically:

1. **Use WebFetch tools** to look up AWS Redshift official documentation BEFORE writing. Look up:
   - "AWS Redshift developer guide site:docs.aws.amazon.com"
   - "AWS Redshift quotas and limits site:docs.aws.amazon.com"
   - "AWS Redshift columnar storage site:docs.aws.amazon.com"
   - "AWS Redshift MPP architecture site:docs.aws.amazon.com"
   - "AWS Redshift distribution styles site:docs.aws.amazon.com"
   - "AWS Redshift sort keys site:docs.aws.amazon.com"
   - "AWS Redshift Spectrum site:docs.aws.amazon.com"
   - "AWS Redshift concurrency scaling site:docs.aws.amazon.com"
   - "AWS Redshift workload management WLM site:docs.aws.amazon.com"
   - "AWS Redshift AQUA site:docs.aws.amazon.com"
   - "AWS Redshift Serverless site:docs.aws.amazon.com"
   - "AWS Redshift materialized views site:docs.aws.amazon.com"
   - "AWS Redshift data sharing site:docs.aws.amazon.com"
   - "Amazon Redshift architecture re:Invent"
   - "Amazon Redshift re-invented SIGMOD paper"

2. **For every concrete number** (max nodes per cluster, max columns per table, block size, slice count, concurrency limits), verify against docs.aws.amazon.com. If you cannot verify a number, explicitly write "[UNVERIFIED — check AWS docs]" next to it.

3. **For every claim about Redshift internals** (how columnar storage is organized, how zone maps work, how the query optimizer makes distribution decisions), if it's not from an official AWS source, mark it as "[INFERRED — not officially documented]".

4. **CRITICAL DISTINCTION: Redshift vs traditional RDBMS vs other data warehouses.** Redshift is a columnar, MPP (massively parallel processing) data warehouse — NOT a transactional OLTP database:
   - Redshift: columnar storage, MPP, optimized for analytical queries (OLAP), not for single-row lookups
   - Traditional RDBMS (PostgreSQL, MySQL): row-oriented, optimized for transactional workloads (OLTP)
   - DO NOT describe Redshift as if it were a general-purpose relational database

## Key Redshift topics to cover

### Requirements & Scale
- Cloud data warehouse for petabyte-scale analytical workloads
- SQL-compatible (based on PostgreSQL 8.0.2 with significant modifications)
- Columnar storage for high compression and fast analytical queries
- MPP architecture: leader node + compute nodes, data distributed across slices
- Provisioned clusters vs Redshift Serverless
- Integration with the AWS data ecosystem (S3, Glue, Lake Formation, Kinesis, EMR)

### Architecture deep dives
- **MPP architecture**: Leader node (query parsing, planning, coordination, result aggregation) and compute nodes (data storage, query execution). How a query is compiled into segments and streams, then distributed to slices. Compute node types (RA3 vs DC2 vs DS2). Slices as the unit of parallelism.
- **Columnar storage**: Why columnar is better for analytics (read only needed columns, better compression). 1 MB blocks. Column encoding (compression types: AZ64, LZO, ZSTD, Byte-Dict, Delta, Mostly, RunLength, Text255/Text32k). Zone maps (min/max per block for predicate pushdown). How a table with 100 columns only reads 3 columns for a query touching 3 columns.
- **Data distribution**: Distribution styles (KEY, EVEN, ALL, AUTO). How distribution key affects query performance — co-located joins vs data redistribution. Redistribution operations (broadcast, hash redistribution). Choosing the right distribution key to minimize data movement.
- **Sort keys**: Compound vs interleaved sort keys. How sort keys interact with zone maps for predicate filtering. Impact on range-restricted scans. Automatic sort key selection (AUTO). VACUUM and ANALYZE for maintaining sort order after loads.
- **Query processing**: Query compilation into C++ code (query-as-code). Segments, streams, and steps. How the leader node builds the execution plan. Short query acceleration (SQA). Result caching.
- **Redshift Spectrum**: Query data directly in S3 without loading. Spectrum layer (separate compute fleet). Pushdown of predicates and aggregations to Spectrum nodes. Partitioning for performance. When to use Spectrum vs loading into Redshift.
- **Concurrency scaling**: Automatic addition of transient clusters to handle burst read queries. How concurrency scaling clusters are provisioned. Credit-based pricing. Which queries are eligible.
- **Workload Management (WLM)**: Queues, memory allocation, concurrency slots. Automatic WLM vs manual WLM. Query prioritization. Short Query Acceleration (SQA) — routing short queries to a fast lane.
- **Redshift Serverless**: No cluster management. RPU (Redshift Processing Units) based pricing. Auto-scaling compute. When to use Serverless vs provisioned clusters.
- **Data sharing**: Cross-cluster, cross-account data sharing without data movement. Producer-consumer model. Live data access without ETL.
- **Materialized views**: Automatic refresh, incremental refresh. Query rewriting to use MVs transparently. Auto MV — Redshift automatically creates and maintains MVs.

### Design evolution (iterative build-up)
- Attempt 0: Single PostgreSQL instance for analytics — works for small data
- Attempt 1: Data grows beyond single machine — need to shard/partition across nodes (MPP). Leader node coordinates, compute nodes store and process. But row-oriented storage scans too much data for analytical queries.
- Attempt 2: Switch to columnar storage — only read needed columns, massive compression (4-10x). Add zone maps for block-level predicate skipping. But joins across nodes require data movement.
- Then: Distribution keys to co-locate join data. Sort keys for efficient range scans. Concurrency scaling for burst query traffic. Spectrum to query cold data in S3 without loading. Materialized views for common aggregations.

### Consistency & Durability
- Redshift is NOT designed for ACID transactions in the traditional sense — it supports serializable isolation for concurrent operations but is optimized for bulk analytical workloads
- Continuous automatic backups to S3 (within the cluster's region)
- Cross-region snapshot copy for DR
- RA3 nodes: data stored in Redshift Managed Storage (RMS) backed by S3, with local SSD cache
- Point-in-time recovery

### Key tradeoffs
- Redshift vs Athena: dedicated warehouse (fast, complex queries, concurrency) vs serverless ad-hoc (no infra, pay per query, slower)
- Redshift vs BigQuery: provisioned MPP vs serverless with slot-based autoscaling
- Redshift vs Snowflake: tightly integrated with AWS ecosystem vs cloud-agnostic with separation of storage/compute
- Columnar vs row-oriented: analytical scans (columnar wins) vs point lookups/OLTP (row wins)
- Distribution KEY vs EVEN vs ALL: co-located joins vs uniform distribution vs full replication for small dimension tables
- Compound vs interleaved sort keys: single leading-column filtering vs multi-column flexible filtering (but slower VACUUM)
- Load into Redshift vs Spectrum: fast repeated queries (load) vs infrequent/cold data queries (Spectrum)
- Provisioned vs Serverless: predictable workload with cost control vs variable workload with zero management

## What NOT to do
- Do NOT treat Redshift as a general-purpose OLTP database — it's an OLAP data warehouse
- Do NOT confuse Redshift with Aurora or RDS — different purpose, different architecture
- Do NOT make up Redshift-specific limits or node specifications without verification
- Do NOT skip the iterative build-up (Attempt 0 → 1 → 2 → ...)
- Do NOT ignore the columnar storage deep dive — it's the foundation of why Redshift performs well for analytics
- Do NOT write a Wikipedia article. This is an INTERVIEW SIMULATION with dialogue.
