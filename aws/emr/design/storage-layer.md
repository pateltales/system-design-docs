# EMR Storage Layer — Deep Dive

> **Companion doc for:** [interview-simulation.md](interview-simulation.md) — Phase 6 (Storage Layer)
> **Last verified:** February 2026 against AWS EMR documentation

---

## Table of Contents

1. [Overview](#1-overview)
2. [The Three Storage Tiers](#2-the-three-storage-tiers)
3. [HDFS on EMR — Ephemeral Distributed Storage](#3-hdfs-on-emr--ephemeral-distributed-storage)
4. [EMRFS — S3 as a Hadoop-Compatible Filesystem](#4-emrfs--s3-as-a-hadoop-compatible-filesystem)
5. [S3A — The New Default (EMR 7.10.0+)](#5-s3a--the-new-default-emr-7100)
6. [EMRFS Consistent View — History and Deprecation](#6-emrfs-consistent-view--history-and-deprecation)
7. [Output Commit Problem — Why S3 Writes Are Hard](#7-output-commit-problem--why-s3-writes-are-hard)
8. [S3-Optimized Committer](#8-s3-optimized-committer)
9. [HDFS vs S3 — The Core Tradeoff](#9-hdfs-vs-s3--the-core-tradeoff)
10. [Data Locality — Does It Matter on EMR?](#10-data-locality--does-it-matter-on-emr)
11. [Shuffle Storage — The Hidden Challenge](#11-shuffle-storage--the-hidden-challenge)
12. [EBS Volumes on EMR](#12-ebs-volumes-on-emr)
13. [Instance Store Volumes](#13-instance-store-volumes)
14. [Storage Configuration Best Practices](#14-storage-configuration-best-practices)
15. [Design Decision Analysis](#15-design-decision-analysis)
16. [Interview Angles](#16-interview-angles)

---

## 1. Overview

Storage is the most consequential architectural decision in EMR because it determines:
- **Durability** — will your data survive cluster termination?
- **Cost** — are you paying for idle compute to keep storage alive?
- **Performance** — can you get data locality benefits?
- **Elasticity** — can you decouple storage from compute?

EMR's storage architecture evolved from HDFS-centric (mimicking on-premise Hadoop) to S3-centric (cloud-native), and this evolution is the single most important design insight for an interview.

### The Evolution

```
Phase 1 (2009):     HDFS only → coupled storage + compute
Phase 2 (2013):     EMRFS added → S3 as HDFS-compatible FS, but eventual consistency
Phase 3 (2020):     S3 strong consistency → EMRFS consistent view deprecated
Phase 4 (2024):     S3A replaces EMRFS as default connector (EMR 7.10.0+)

Direction of travel: HDFS is shrinking to a shuffle/temp role; S3 is primary storage.
```

---

## 2. The Three Storage Tiers

Every EMR cluster has three storage options available simultaneously:

```
┌──────────────────────────────────────────────────────────────────┐
│                     EMR CLUSTER STORAGE                          │
│                                                                  │
│  ┌─────────────────────┐                                        │
│  │   Amazon S3          │  ← Primary storage (via EMRFS or S3A) │
│  │   (external,         │    Input data, output data,           │
│  │    durable,          │    anything that must persist          │
│  │    11 9's)           │                                       │
│  └─────────────────────┘                                        │
│                                                                  │
│  ┌─────────────────────┐                                        │
│  │   HDFS               │  ← Cluster-level ephemeral storage    │
│  │   (replicated across │    Shuffle data, HBase regions,       │
│  │    core nodes,       │    temp tables                        │
│  │    ephemeral)        │                                       │
│  └─────────────────────┘                                        │
│                                                                  │
│  ┌─────────────────────┐                                        │
│  │   Local Filesystem   │  ← Instance-level ephemeral storage   │
│  │   (instance store    │    OS temp files, log buffering,      │
│  │    or EBS,           │    scratch space                      │
│  │    per-instance)     │                                       │
│  └─────────────────────┘                                        │
└──────────────────────────────────────────────────────────────────┘
```

### Comparison Matrix

| Property | S3 (EMRFS/S3A) | HDFS | Local Filesystem |
|---|---|---|---|
| **Durability** | 99.999999999% (11 9's) | Ephemeral — lost on cluster termination | Lost on instance termination |
| **Scope** | Global — shared across clusters, accounts | Cluster-scoped — only this cluster's nodes | Instance-scoped — only this machine |
| **Persistence** | Persists independently of any cluster | Dies with the cluster | Dies with the instance |
| **Performance** | Higher latency (network I/O), high throughput | Lower latency (data locality), high throughput | Lowest latency (local disk) |
| **Scalability** | Virtually unlimited | Limited by core node disk capacity | Limited by instance disk |
| **Cost** | Pay per GB stored + requests | Pay for EC2 instances (storage is "free" with compute) | Included with instance |
| **Access** | `s3://bucket/path` | `hdfs:///path` | `/mnt/...` or `/tmp/...` |
| **Replication** | Handled by S3 (3+ AZs automatically) | Configurable (1, 2, or 3 replicas across core nodes) | No replication |
| **Read-after-write** | Strong consistency (since Dec 2020) | Strong consistency (within cluster) | Strong consistency (local) |

---

## 3. HDFS on EMR — Ephemeral Distributed Storage

### Architecture

HDFS on EMR follows the standard Hadoop model, adapted for the cloud:

```
PRIMARY NODE
┌───────────────────────────────────┐
│           NameNode                 │
│                                   │
│  • Manages filesystem namespace   │
│  • Tracks block locations         │
│  • Handles client operations      │
│  • Memory: ~150 bytes per block   │
│    (~1 GB per million blocks)     │
└───────────┬───────────────────────┘
            │
            │ Block reports + heartbeats
            │
     ┌──────┴──────┐
     │              │
┌────▼────┐  ┌─────▼───┐  ┌──────────┐
│Core #1  │  │Core #2  │  │Core #3   │
│DataNode │  │DataNode │  │DataNode  │
│         │  │         │  │          │
│Block A  │  │Block A  │  │Block B   │
│Block B  │  │Block C  │  │Block C   │
│Block D  │  │Block D  │  │Block A   │  ← replication factor 3
└─────────┘  └─────────┘  └──────────┘
```

### Key Properties

| Property | Value |
|---|---|
| **Block size** | 128 MB (default, configurable) |
| **Replication factor** | 3 for ≥ 4 core nodes, 2 for ≤ 3 core nodes, 1 for single-node cluster |
| **NameNode memory** | ~150 bytes per block (~1 GB per million blocks) |
| **DataNode location** | Core nodes only (not task nodes) |
| **Underlying storage** | Instance store volumes + EBS volumes attached to core nodes |
| **Persistence** | NONE — all data lost when cluster terminates |
| **NameNode HA** | Available with 3 primary nodes (ZooKeeper-based failover) |

### When to Use HDFS on EMR

1. **Shuffle data**: Intermediate results between map and reduce phases. This is HDFS's primary role in modern EMR.
2. **HBase region storage**: HBase stores region data in HDFS. HBase on EMR requires HDFS.
3. **Temporary Hive tables**: Intermediate Hive query results before writing final output to S3.
4. **Performance-critical intermediate data**: When data locality matters for iterative algorithms (rare with modern S3 throughput).

### When NOT to Use HDFS

1. **Final output data**: Use S3 — HDFS data dies with the cluster
2. **Input data**: Use S3 — avoids the need to HDFS-copy data into the cluster at startup
3. **Shared data across clusters**: Use S3 — HDFS is cluster-scoped
4. **Data you care about**: Use S3 — HDFS is ephemeral

### The "Ephemeral HDFS" Mental Model

Think of HDFS on EMR as a fast scratch disk that happens to be distributed:

```
Traditional Hadoop:  HDFS IS the database. You store everything there.
EMR (cloud-native):  HDFS is temp storage. S3 is the database.
```

This shift is the most important architecture difference between on-premise Hadoop and EMR.

---

## 4. EMRFS — S3 as a Hadoop-Compatible Filesystem

### What EMRFS Is

EMRFS (EMR File System) is an implementation of the Hadoop `FileSystem` interface that translates HDFS API calls into S3 API calls. It allows Spark, Hive, and other frameworks to read/write S3 data as if it were HDFS, using `s3://` URIs.

```
┌──────────────────────────────────────────────────────────────┐
│                     APPLICATION LAYER                         │
│  Spark / Hive / Presto / MapReduce                           │
│                                                               │
│  Calls: FileSystem.open("s3://bucket/data/part-00000")       │
└──────────────────────┬───────────────────────────────────────┘
                       │
                       ▼
┌──────────────────────────────────────────────────────────────┐
│                     EMRFS LAYER                               │
│                                                               │
│  • Translates HDFS FileSystem API → S3 API                   │
│  • Handles multipart uploads for large files                 │
│  • Manages request signing (SigV4)                           │
│  • Encryption (SSE-S3, SSE-KMS, CSE-KMS, CSE-Custom)        │
│  • IAM role mapping (per-user or per-path EMRFS roles)       │
│  • Retry logic for transient S3 errors                       │
│  • S3-optimized committer for efficient writes               │
└──────────────────────┬───────────────────────────────────────┘
                       │
                       ▼
┌──────────────────────────────────────────────────────────────┐
│                     Amazon S3                                 │
│  Objects stored across 3+ AZs, 11 9's durability             │
└──────────────────────────────────────────────────────────────┘
```

### Key EMRFS Features

| Feature | Details |
|---|---|
| **S3 URI scheme** | `s3://` (EMRFS-specific; `s3a://` uses the Hadoop S3A connector) |
| **Encryption** | SSE-S3, SSE-KMS, CSE-KMS, CSE-Custom. Configured via EMR security configurations (EMR 4.8.0+) |
| **IAM roles per path** | EMR 5.10.0+ — different IAM roles for different S3 paths (e.g., sensitive data uses a restricted role) |
| **Consistency** | Leverages S3 strong read-after-write consistency (no additional consistency layer needed since Dec 2020) |
| **Retry logic** | Automatic retries for transient S3 errors (500, 503, throttling) |
| **Multipart upload** | Large files split into parts for parallel upload |

### EMRFS Deprecation Warning

Starting with **EMR 7.10.0**, the Apache Hadoop S3A connector has **replaced EMRFS as the default S3 filesystem**. EMRFS is still available but is on a deprecation path. New workloads should use S3A.

---

## 5. S3A — The New Default (EMR 7.10.0+)

### What Changed

| Property | EMRFS | S3A |
|---|---|---|
| **URI scheme** | `s3://` | `s3a://` |
| **Origin** | AWS proprietary | Apache Hadoop open-source |
| **Default since** | EMR 2.x through 7.9.x | EMR 7.10.0+ |
| **Maintained by** | AWS EMR team | Apache Hadoop community + AWS contributions |
| **S3 Express support** | No [UNVERIFIED] | Yes — S3 Express One Zone for low-latency |
| **Portability** | EMR-only | Works on any Hadoop distribution |
| **Committer** | S3-optimized committer (EMRFS-specific) | Magic committer, staging committer |

### Why the Switch

1. **Community alignment**: S3A is the Hadoop community standard. Using it reduces EMR-specific divergence and makes it easier for customers to migrate between Hadoop distributions.
2. **Feature velocity**: S3A gets new features (S3 Express, improved directory markers) from the broader Hadoop community.
3. **S3 Express One Zone**: New ultra-low-latency S3 tier that S3A supports natively.
4. **Reduced AWS-specific lock-in**: Customers using `s3a://` paths can run the same code on EMR, Databricks, or self-managed Hadoop.

### Migration Impact

For most workloads, the migration is transparent — just change `s3://` to `s3a://` in paths. However:
- EMRFS-specific features (IAM roles per path, EMRFS CLI) may need reconfiguration
- Custom applications that depend on EMRFS-specific behavior need testing
- The S3-optimized committer (EMRFS-specific) is replaced by the S3A Magic committer

---

## 6. EMRFS Consistent View — History and Deprecation

### The Problem (Pre-2020)

Before December 2020, Amazon S3 had **eventual consistency** for certain operations:
- **PUT of new object**: Read-after-write consistent (you could read immediately after writing)
- **Overwrite PUT or DELETE**: Eventually consistent (you might read old data briefly after overwriting)
- **LIST after PUT**: Eventually consistent (newly written files might not appear in listings immediately)

This was catastrophic for Hadoop-style workloads where a job writes output files and the next job immediately lists and reads them.

### The Solution: EMRFS Consistent View (CV)

EMRFS CV used a **DynamoDB table** as a metadata sidecar to track S3 objects:

```
┌──────────────────┐        ┌──────────────────┐
│  EMRFS on EMR    │───────▶│  DynamoDB Table   │
│                  │        │  (EmrFSMetadata)  │
│  Write file to   │        │                   │
│  S3, record in   │        │  object_path → {  │
│  DynamoDB        │        │    version,       │
│                  │        │    timestamp,     │
│  Read/List: check│        │    etag           │
│  DynamoDB first  │        │  }                │
└────────┬─────────┘        └──────────────────┘
         │
         ▼
┌──────────────────┐
│    Amazon S3      │
│  (eventually      │
│   consistent)     │
└──────────────────┘
```

### Configuration

```json
{
  "classification": "emrfs-site",
  "properties": {
    "fs.s3.consistent": "true",
    "fs.s3.consistent.retryPeriodSeconds": "10",
    "fs.s3.consistent.retryCount": "5",
    "fs.s3.consistent.metadata.tableName": "EmrFSMetadata"
  }
}
```

### Deprecation Timeline

| Date | Event |
|---|---|
| **Dec 1, 2020** | Amazon S3 delivers **strong read-after-write consistency** globally, for free, with no performance impact |
| **Post-Dec 2020** | EMRFS CV is no longer necessary — S3 is natively consistent |
| **June 1, 2023** | EMRFS CV reaches **end of standard support** for new EMR releases |
| **Current** | AWS recommends disabling CV and deleting the DynamoDB table to save costs |

### Interview Insight

This is a great interview talking point: "EMRFS consistent view was a creative workaround for S3 eventual consistency — using DynamoDB as a metadata sidecar. But when S3 itself became strongly consistent in 2020, the workaround became unnecessary overhead. This is a pattern where the infrastructure caught up to the application-level fix."

---

## 7. Output Commit Problem — Why S3 Writes Are Hard

### The Fundamental Problem

Hadoop's output commit protocol was designed for HDFS, where **rename is atomic and fast** (it's just a metadata operation in the NameNode). The standard commit process:

```
HDFS Commit (fast, correct):
1. Each task writes to a temp directory:     hdfs:///tmp/task-001/part-00000
2. Task succeeds → rename temp file to output: hdfs:///output/part-00000
3. Rename is O(1) — NameNode metadata update only
```

On S3, **rename doesn't exist** as a native operation. An S3 "rename" is actually:

```
S3 "Rename" (slow, expensive):
1. Copy object to new key     ← O(n) — full data copy over network
2. Delete old object           ← Additional API call
```

For a Spark job writing 10,000 output files totaling 1 TB, the "rename" phase would:
- Copy 1 TB of data from temp keys to output keys
- Make 20,000 API calls (10,000 copies + 10,000 deletes)
- Take minutes to hours
- **Double the S3 storage** temporarily (both temp and final copies exist)

### The Naive Solution: Direct Write

EMRFS introduced "direct write" — skip the rename and write directly to the final output path:

```
Direct Write:
1. Each task writes directly to: s3://bucket/output/part-00000
2. No rename needed
```

**Problem**: If a task fails partway through and is retried, the partial output and the retry output can both end up in the final path, leading to **duplicate or corrupt data**.

**Risk with speculative execution**: If Spark runs two copies of the same task speculatively, both may write to the same output path, causing data loss or corruption. This is why **speculative execution is disabled by default on EMR**.

---

## 8. S3-Optimized Committer

### The Solution

The EMRFS S3-optimized committer uses **S3 multipart upload** as an atomic commit mechanism:

```
S3-Optimized Commit:
1. Task writes file using multipart upload → parts uploaded to S3 but NOT completed
2. Task succeeds → committer records upload ID
3. Job commit → completes all multipart uploads atomically
4. Job fails → aborts all multipart uploads (no partial data left)
```

The key insight: a multipart upload is invisible to S3 readers until the `CompleteMultipartUpload` API call. This provides **atomicity without rename**.

```
┌───────────────┐     ┌────────────────┐     ┌───────────────┐
│  Spark Task    │     │  S3 Multipart   │     │  S3 Final     │
│                │     │  Upload (hidden) │     │  Object       │
│  Write parts  ─┼────▶│  Part 1          │     │               │
│               ─┼────▶│  Part 2          │     │               │
│               ─┼────▶│  Part 3          │     │               │
│                │     │                  │     │               │
│  Task commits  │     │  Upload ID saved │     │               │
└───────────────┘     └────────┬─────────┘     │               │
                               │               │               │
                    Job commit │               │               │
                               ▼               │               │
                    CompleteMultipartUpload ───▶│  Object       │
                    (atomic finalization)       │  appears      │
                                               │  instantly    │
                                               └───────────────┘
```

### Availability and Format Support

| EMR Version | Supported Formats |
|---|---|
| **EMR 5.19.0** | Parquet only (manual enablement required) |
| **EMR 5.20.0+** | Parquet only (enabled by default) |
| **EMR 6.4.0+** | Parquet, ORC, text-based (CSV, JSON) |

### When the S3-Optimized Committer Is NOT Used

The committer falls back to direct write when:
- Writing using Spark RDD API (only DataFrame/SQL API supported)
- Writing Parquet via Hive SerDe
- Writing dynamic partitions with `partitionOverwriteMode` set to `dynamic`
- Writing to custom partition locations
- Writing to a non-EMRFS filesystem (HDFS, S3A)

### Verifying Committer Usage

For EMR 5.14.0+, enable Spark INFO logging and look for:
```
"Direct Write: ENABLED"    → committer NOT being used (fallback to direct write)
```
If you don't see this message, the S3-optimized committer is active.

---

## 9. HDFS vs S3 — The Core Tradeoff

This is the most important storage question in any EMR interview.

### Comparison Table

| Dimension | HDFS | S3 (via EMRFS/S3A) |
|---|---|---|
| **Latency** | Low — local disk or same-rack network | Higher — cross-network to S3 endpoints |
| **Throughput** | High — bounded by disk I/O and network | Very high — S3 aggregate bandwidth scales with requests |
| **Data locality** | Yes — process data on the node where it's stored | No — data always comes over the network |
| **Durability** | Ephemeral — lost on cluster termination | 11 9's — survives anything short of S3 regional failure |
| **Cost model** | Pay for EC2 instances (storage is part of compute cost) | Pay per GB stored + API requests |
| **Elasticity** | Coupled to cluster size — more storage = more core nodes | Decoupled — any cluster can access any amount of S3 data |
| **Sharing** | Cluster-scoped — only accessible within the cluster | Global — accessible from any cluster, Lambda, Athena, etc. |
| **Rename** | O(1) — metadata update | O(n) — full copy + delete |
| **Listing** | Fast — NameNode in-memory | Slower — paginated S3 LIST (1,000 objects per page) |
| **Consistency** | Strong (within cluster) | Strong (since Dec 2020) |

### When HDFS Wins

| Scenario | Why HDFS |
|---|---|
| **HBase region storage** | HBase requires low-latency random reads from HDFS WAL and region files |
| **Shuffle-heavy workloads** | Intermediate shuffle data benefits from local disk speed |
| **Iterative algorithms** | ML algorithms that read the same data many times benefit from data locality and OS page cache |
| **Very small files** | S3 has per-request overhead; HDFS handles small files more efficiently for random access |

### When S3 Wins (Almost Everything Else)

| Scenario | Why S3 |
|---|---|
| **Input data storage** | Data persists, shared across clusters, no need to copy into cluster |
| **Output data storage** | Results survive cluster termination |
| **Transient clusters** | No HDFS setup time, no data copy time, cluster starts → reads from S3 → writes to S3 → terminates |
| **Cost optimization** | Decouple storage from compute — don't pay for idle EC2 to keep HDFS alive |
| **Multi-cluster access** | Multiple EMR clusters, Athena, Redshift Spectrum, and Lambda all read the same S3 data |
| **Large datasets** | S3 scales to exabytes; HDFS limited by core node count and disk size |

### The Design Evolution in the Interview

```
Candidate progression:

Attempt 0: "Store everything in HDFS" (on-prem Hadoop thinking)
  → Problem: Data lost on cluster termination, can't share across clusters

Attempt 1: "Add EMRFS for S3 access" (EMR's key innovation)
  → Problem: Performance — no data locality, S3 rename is expensive

Attempt 2: "Use HDFS for shuffle only, S3 for I/O" (modern best practice)
  → Decouple storage (S3) from compute (EC2)
  → HDFS is just a scratch disk for intermediate data
  → Enables transient clusters and elastic scaling
```

---

## 10. Data Locality — Does It Matter on EMR?

### What Data Locality Means

In traditional Hadoop, the scheduler tries to run tasks on the same node where the input data's HDFS blocks are stored. This avoids network transfer:

```
WITH data locality:     Task reads from local disk → ~100+ MB/s per disk, zero network
WITHOUT data locality:  Task reads from S3 → network-bound, shared bandwidth
```

### Does Data Locality Matter in Modern EMR?

**Short answer**: Increasingly less, for three reasons:

**1. Network bandwidth has caught up**

Modern EC2 instances have 10-100 Gbps network bandwidth. S3 can deliver aggregate throughput that scales with the number of concurrent requests. For large sequential reads (typical of Spark), S3 throughput often matches or exceeds HDFS.

| Instance Type | Network Bandwidth | Practical S3 Read Throughput |
|---|---|---|
| m5.xlarge | Up to 10 Gbps | ~500-800 MB/s with parallel reads |
| m5.4xlarge | Up to 10 Gbps | ~500-800 MB/s with parallel reads |
| m5.16xlarge | 25 Gbps | ~2 GB/s with parallel reads |
| m5n.24xlarge | 100 Gbps | ~5+ GB/s with parallel reads |

**2. Columnar formats reduce data volume**

Parquet and ORC with predicate pushdown and column pruning mean Spark only reads the columns and rows it needs. A query on a 1 TB table might only transfer 10 GB over the network.

**3. S3 throughput scales linearly**

S3 throughput scales with the number of concurrent connections. A 100-node cluster making 1000 concurrent S3 requests can achieve much higher aggregate throughput than HDFS on the same cluster.

### When Data Locality Still Matters

| Scenario | Why Locality Helps |
|---|---|
| **HBase random reads** | Single-row lookups need < 10ms latency; S3 adds 5-20ms per request |
| **Small file access patterns** | Many tiny S3 requests have per-request overhead that dominates transfer time |
| **Iterative ML algorithms** | Reading the same dataset 100 times benefits from OS page cache on local HDFS |
| **Shuffle-heavy workloads** | Shuffle is all-to-all network; having input data local reduces total network contention |

### Interview Insight

"Data locality was the primary optimization in on-premise Hadoop because network bandwidth was the bottleneck (1-10 Gbps shared). In the cloud, with 25-100 Gbps per instance and S3's infinite aggregate bandwidth, data locality matters less for bulk processing. The exception is shuffle — which is still network-bound and benefits from local storage."

---

## 11. Shuffle Storage — The Hidden Challenge

### What Shuffle Is

Shuffle is the all-to-all data transfer between map and reduce stages. In Spark, it happens at:
- `groupBy()`, `reduceByKey()`, `join()`, `distinct()`, `repartition()`
- Any operation that requires redistributing data across partitions

```
MAP SIDE (write shuffle files)          REDUCE SIDE (read shuffle files)

Executor A ──┐                    ┌──▶ Executor D
             │   Shuffle files    │
Executor B ──┼──(local disk)──────┼──▶ Executor E
             │                    │
Executor C ──┘                    └──▶ Executor F
```

### Why Shuffle Is the Last Bastion of Local Storage

Shuffle data is:
1. **Written and read once** — map outputs are consumed by reducers and then deleted
2. **Latency-sensitive** — reducer tasks block waiting for map outputs
3. **Volume can be massive** — a `join()` of two 1 TB tables can produce 2+ TB of shuffle data
4. **Random-access** — reducers read specific partitions from each mapper's output

These properties make local disk (HDFS or local filesystem) ideal for shuffle data.

### Shuffle Storage Options on EMR

| Option | Where Data Lives | Pros | Cons |
|---|---|---|---|
| **Local disk (default)** | Instance store or EBS on each node | Fastest — no network for local reads | Limited by disk capacity; lost on node failure |
| **HDFS** | Distributed across core nodes | Higher capacity than local disk; replicated | Slower than local; still ephemeral |
| **S3 shuffle plugin** [INFERRED] | Amazon S3 | Unlimited capacity; survives node failures | Higher latency; API cost; requires fast network |
| **External shuffle service** | Dedicated shuffle service | Decouples shuffle from compute; survives executor failures | Operational complexity; additional infrastructure |

### External Shuffle Service on EMR

EMR automatically configures Spark's **external shuffle service** on each node:
- Runs as a YARN auxiliary service
- Preserves shuffle data when executors are terminated (important for dynamic allocation)
- Without it, executor termination would force re-computation of shuffle data

Configuration (automatic on EMR):
```
spark.shuffle.service.enabled = true
spark.dynamicAllocation.enabled = true  (EMR 4.4.0+)
```

### Sizing HDFS for Shuffle

When designing an EMR cluster, the minimum core node HDFS should accommodate the expected shuffle volume:

```
Shuffle size estimation:
- Join of two tables: shuffle ≈ smaller table size (if broadcast join not possible)
- GroupBy: shuffle ≈ input size × selectivity
- Repartition: shuffle ≈ input partition size

Rule of thumb: provision HDFS capacity ≥ 2× expected peak shuffle volume
(to handle concurrent stages + spill overhead)
```

### Spark Node Decommissioning for Shuffle Protection

EMR configures Spark with shuffle-aware decommissioning settings:

| Setting | Default on EMR | Purpose |
|---|---|---|
| `spark.blacklist.decommissioning.enabled` | `true` | Deny-lists decommissioning nodes from new task assignments |
| `spark.blacklist.decommissioning.timeout` | `1h` | How long to deny-list a decommissioning node |
| `spark.decommissioning.timeout.threshold` | `20s` | Improves Spot instance handling (EMR 5.11.0+) |
| `spark.resourceManager.cleanupExpiredHost` | `true` | Cleans up cached data on decommissioned nodes |
| `spark.stage.attempt.ignoreOnDecommissionFetchFailure` | `true` | Prevents job failure from decommissioned node fetch failures |

These settings ensure that when a node is being scaled down or Spot-reclaimed, Spark:
1. Stops assigning new tasks to that node
2. Waits for in-progress tasks to complete
3. Doesn't fail the entire stage just because shuffle data on that node is temporarily unavailable

---

## 12. EBS Volumes on EMR

### Default EBS Allocation (EMR 5.22.0+)

EMR automatically attaches EBS volumes to instances based on instance size:

| Instance Size | Volumes | Size per Volume | Total EBS |
|---|---|---|---|
| *.large | 1 | 32 GiB | 32 GiB |
| *.xlarge | 2 | 32 GiB | 64 GiB |
| *.2xlarge | 4 | 32 GiB | 128 GiB |
| *.4xlarge | 4 | 64 GiB | 256 GiB |
| *.8xlarge | 4 | 128 GiB | 512 GiB |
| *.16xlarge | 4 | 256 GiB | 1,024 GiB |

### Key EBS Constraints

| Constraint | Value |
|---|---|
| **Max volumes per instance** | 25 |
| **Min EBS volume size (core nodes)** | 5 GB |
| **Max EBS volumes per launch request** | 2,500 across all instances |
| **Root volume default (EMR 6.15+)** | 15 GiB (gp3) |
| **Root volume default (EMR ≤ 6.14)** | 6-10 GiB (gp2) |
| **Snapshots** | NOT supported — cannot snapshot and restore EBS volumes in EMR |
| **EBS attachment timing** | Only at cluster startup or when adding task instance groups |
| **Manual detachment** | NOT allowed — detaching triggers instance replacement |
| **gp3 migration** | Requires launching a new cluster (cannot convert gp2 → gp3 in-place) |

### Available EBS Volume Types

| Type | Use Case on EMR |
|---|---|
| **gp3 (General Purpose SSD)** | Default for new clusters (EMR 6.15+). Good balance of IOPS and throughput. |
| **gp2 (General Purpose SSD)** | Legacy default. IOPS scales with volume size (3 IOPS/GiB). |
| **io1/io2 (Provisioned IOPS SSD)** | HBase workloads requiring consistent low-latency I/O. |
| **st1 (Throughput Optimized HDD)** | Large sequential reads/writes. Good for HDFS data storage at lower cost. |
| **sc1 (Cold HDD)** | Lowest cost. Infrequent access. Rarely used on EMR. |

---

## 13. Instance Store Volumes

### What They Are

Instance store volumes are physically attached NVMe SSDs on certain EC2 instance types. They provide:
- **Highest IOPS and throughput** of any storage option
- **Zero network latency** — directly attached to the host
- **Ephemeral** — data is lost when the instance stops, terminates, or the underlying drive fails

### Instance Types with Instance Store

| Instance Family | Instance Store | Notes |
|---|---|---|
| **m5d, r5d, c5d** | NVMe SSD | "d" suffix = instance store |
| **i3, i3en** | High-density NVMe SSD | Storage-optimized — massive local storage |
| **d2, d3** | HDD | Dense storage, high throughput |
| **m5, r5, c5** | None | No instance store — EBS only |

### Best Use on EMR

Instance store volumes are ideal for:
1. **HDFS storage on core nodes** — high-performance ephemeral storage
2. **Spark shuffle** — fastest possible shuffle performance
3. **HBase block cache and WAL** — low-latency local I/O
4. **Scratch space** — temp files, spill buffers

### EMR Behavior with Instance Store

- EMR automatically formats and mounts instance store volumes
- Instance store volumes are used by HDFS DataNode automatically
- Both instance store and EBS volumes contribute to HDFS capacity
- On shuffle-heavy workloads, instance store provides measurably better performance

---

## 14. Storage Configuration Best Practices

### Pattern 1: S3-Centric (Recommended for Most Workloads)

```
Input:    s3://input-bucket/data/
Output:   s3://output-bucket/results/
HDFS:     Minimal — used only for shuffle
Cluster:  Transient — terminate after job completes
```

**Configuration:**
- Small core node group (2-4 nodes) with enough EBS for shuffle
- Large task node group on Spot for compute
- No need for large HDFS — all persistent data in S3

### Pattern 2: HDFS-Heavy (HBase, Iterative ML)

```
Input:    Loaded from S3 → HDFS at cluster start
Output:   Written to S3 at job end
HDFS:     Large — stores working dataset
Cluster:  Long-running
```

**Configuration:**
- Large core node group with instance store volumes (i3 or d3 family)
- HDFS replication factor 3
- Periodic checkpointing to S3
- Multi-primary HA for cluster stability

### Pattern 3: Hybrid (Large ETL with Significant Shuffle)

```
Input:    s3://data-lake/raw/
Shuffle:  HDFS (local to cluster)
Output:   s3://data-lake/processed/
```

**Configuration:**
- Core nodes with r5d instances (memory + instance store for shuffle)
- Task nodes with r5 instances on Spot (compute only)
- HDFS sized for 2× expected shuffle volume

### Storage Decision Flowchart

```
Does the data need to survive cluster termination?
├── Yes → S3
└── No → Is it shuffle data?
    ├── Yes → Local disk / HDFS
    └── No → Is it HBase?
        ├── Yes → HDFS (with S3 backup)
        └── No → Is it intermediate query results?
            ├── Yes → HDFS (temp tables)
            └── No → S3 (default)
```

---

## 15. Design Decision Analysis

### Decision 1: Why Introduce EMRFS Instead of Just Using S3A?

| Alternative | Pros | Cons |
|---|---|---|
| **Raw S3A (Hadoop native)** | Open source, portable | Lacked consistent view (pre-2020), no EMR-specific optimizations |
| **EMRFS** ← EMR's choice (2013-2024) | Consistent view for eventual-consistency S3, S3-optimized committer, IAM role mapping, encryption integration | AWS-proprietary, s3:// vs s3a:// confusion, maintenance burden |
| **S3A** ← EMR's choice (2024+) | Community standard, S3 Express support, portable | Lost EMRFS-specific features (now replaced by S3 native capabilities) |

**Why the switch**: EMRFS was necessary when S3 was eventually consistent (2009-2020). The consistent view using DynamoDB was a critical feature. Once S3 became strongly consistent (Dec 2020), EMRFS's raison d'être disappeared. S3A is community-maintained, more portable, and supports newer S3 features like S3 Express One Zone.

### Decision 2: Why Ephemeral HDFS?

| Alternative | Pros | Cons |
|---|---|---|
| **Persistent HDFS** (data survives cluster termination) | Data locality, fast restarts | Couples storage to cluster, expensive idle storage, complex to manage across clusters |
| **Ephemeral HDFS** ← EMR's choice | Encourages S3 as primary storage, enables transient clusters, simpler cluster lifecycle | Shuffle data lost on termination, HBase needs careful planning |

**Why ephemeral**: EMR's design philosophy is to decouple storage (S3) from compute (EC2). Persistent HDFS would tie customers to specific clusters and discourage the transient cluster pattern that saves costs. HDFS on EMR is a performance optimization for shuffle, not a primary storage tier.

### Decision 3: Multipart Upload as Commit Protocol

| Alternative | Pros | Cons |
|---|---|---|
| **Standard Hadoop committer (rename)** | Works perfectly on HDFS | S3 rename = copy + delete = O(n) data, slow, expensive, doubles storage |
| **Direct write (no commit)** | Fast, no rename | No atomicity — partial failures leave corrupt data; speculative execution is dangerous |
| **Multipart upload commit** ← EMR's S3-optimized committer | Atomic — parts invisible until CompleteMultipartUpload; no rename needed; fast | EMRFS-specific (S3A uses similar but different Magic committer); limited format support initially |

**Why multipart upload**: It's the only S3 mechanism that provides atomicity. The parts are uploaded during task execution, invisible to readers. The commit phase calls `CompleteMultipartUpload` to make them visible atomically. This gives HDFS-like commit semantics without the rename problem.

---

## 16. Interview Angles

### Questions an Interviewer Might Ask

**Storage Fundamentals:**
- "Where should I store my data on EMR — HDFS or S3?"
  - Answer: S3 for anything that needs to persist. HDFS for shuffle, HBase regions, and iterative workloads. Modern best practice: S3-centric with HDFS as scratch space.

- "What happens to HDFS data when the cluster terminates?"
  - Answer: It's gone. HDFS on EMR is ephemeral. This is by design — it encourages S3 as primary storage and enables transient clusters.

**EMRFS Deep Dive:**
- "What problem did EMRFS consistent view solve?"
  - Answer: Before Dec 2020, S3 was eventually consistent for overwrites and listings. EMRFS CV used DynamoDB as a metadata sidecar to check whether S3 had caught up. After S3 became strongly consistent, CV became unnecessary and was deprecated.

- "Why is S3 rename expensive? How does EMR solve the output commit problem?"
  - Answer: S3 has no native rename — it's a full copy + delete. The S3-optimized committer uses multipart upload as an atomic commit mechanism: parts are uploaded during task execution (invisible), then completed atomically in the job commit phase.

**Architecture Decisions:**
- "Why is HDFS ephemeral on EMR instead of persistent?"
  - Answer: Design philosophy — decouple storage from compute. Persistent HDFS would tie customers to specific clusters, prevent transient cluster patterns, and increase costs. S3 at 11 9's durability is the better primary storage tier.

- "How do you size HDFS for a Spark workload?"
  - Answer: Primarily for shuffle. Estimate shuffle volume (typically proportional to join/groupBy data size), add 2× headroom. If using S3 for all I/O, HDFS can be minimal — just enough for shuffle + temp files.

**Performance:**
- "Does data locality matter on EMR?"
  - Answer: Decreasingly. Modern EC2 instances have 10-100 Gbps network, S3 aggregate bandwidth scales linearly, and columnar formats (Parquet/ORC) with predicate pushdown reduce data transfer. Data locality still matters for HBase random reads and shuffle-heavy workloads.

### Red Flags in Candidate Answers

| Red Flag | Why It's Wrong |
|---|---|
| "Store output data in HDFS" | HDFS is ephemeral — data lost on cluster termination |
| "EMRFS consistent view is essential for correctness" | Deprecated — S3 has been strongly consistent since Dec 2020 |
| "S3 is too slow for EMR workloads" | Modern instances have 10-100 Gbps network; S3 aggregate throughput scales with concurrency |
| "Data locality is critical for all EMR workloads" | Only for HBase and shuffle-heavy workloads; bulk processing with S3 performs well |
| "Use `s3://` paths everywhere" | EMRFS is being replaced by S3A (`s3a://`) starting EMR 7.10.0 |
| "Enable speculative execution for faster Spark jobs" | Dangerous with EMRFS direct write — can cause data loss for non-Parquet formats |
