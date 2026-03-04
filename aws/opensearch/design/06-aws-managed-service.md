# AWS OpenSearch Service: Managed Service Architecture Deep Dive

> **Context:** This document covers what AWS does *around* the open-source OpenSearch engine to
> turn it into a fully managed service. In an interview, this shows you understand not just
> the engine internals, but the operational envelope that makes it production-ready.

---

## Table of Contents

1. [Domain (Cluster) Provisioning](#1-domain-cluster-provisioning)
2. [Control Plane vs Data Plane](#2-control-plane-vs-data-plane)
3. [Blue-Green Deployments](#3-blue-green-deployments)
4. [Multi-AZ Deployment](#4-multi-az-deployment)
5. [Storage Tiers (Hot / UltraWarm / Cold)](#5-storage-tiers-hot--ultrawarm--cold)
6. [Security](#6-security)
7. [Monitoring](#7-monitoring)
8. [Automated Backups](#8-automated-backups)
9. [Limitations vs Self-Managed](#9-limitations-vs-self-managed)
10. [OpenSearch Serverless](#10-opensearch-serverless)
11. [Contrast: Managed vs Self-Managed](#11-contrast-managed-vs-self-managed)

---

## 1. Domain (Cluster) Provisioning

In AWS OpenSearch Service, a **domain** is the AWS term for a cluster. When you create a domain,
you configure the following:

### Instance Configuration

| Node Role | Purpose | Example Instance Types |
|---|---|---|
| **Data nodes** | Store indices, handle search + indexing | `r6g.large`, `r6g.xlarge`, `i3.xlarge` |
| **Dedicated master nodes** | Cluster state, shard allocation, not data | `m6g.large` (3 recommended) |
| **UltraWarm nodes** | Read-only warm storage, S3-backed | `ultrawarm1.medium`, `ultrawarm1.large` |
| **Cold storage** | Detached S3 storage, no compute | N/A (uses S3 directly) |

### Storage

- **EBS gp3**: General purpose, baseline 3000 IOPS / 125 MB/s, can provision up to 16,000 IOPS.
  Best cost-performance for most workloads.
- **EBS io2**: Provisioned IOPS for latency-sensitive workloads. Up to 64,000 IOPS per volume.
- Instance store (e.g., `i3` family): NVMe SSDs attached to the instance. High throughput but
  data is ephemeral (lost on instance stop).

### Access & Networking

- **VPC access**: Domain endpoints placed inside your VPC subnets. Traffic never crosses the
  public internet. Accessed via VPC endpoints or VPN/Direct Connect.
- **Public access**: Domain gets a public endpoint. Access controlled by IP-based or IAM-based
  access policies.

### Encryption & Authentication

- Encryption at rest (AWS KMS), encryption in transit (TLS 1.2), node-to-node encryption.
- Authentication via IAM, SAML 2.0 federation, or Amazon Cognito.
- Fine-grained access control (FGAC) for index/document/field-level permissions.

### Key Provisioning Decision: Sizing

```
Rule of thumb for data nodes:

  Storage needed = (source data) x (1 + replicas) x 1.45 (indexing overhead + OS reserved)

  Example: 500 GB source data, 1 replica
         = 500 x 2 x 1.45 = 1,450 GB total storage
         = with 3 data nodes: ~484 GB EBS per node

  Memory: aim for ~1 GB heap per 20-30 GB of data on that node
```

---

## 2. Control Plane vs Data Plane

This separation is fundamental to how AWS manages OpenSearch without touching your data.

```
+------------------------------------------------------------------+
|                        AWS ACCOUNT (Customer)                     |
+------------------------------------------------------------------+
|                                                                   |
|   +---------------------------+   +----------------------------+  |
|   |      CONTROL PLANE        |   |       DATA PLANE           |  |
|   |      (AWS-managed)        |   |    (Customer's cluster)    |  |
|   |                           |   |                            |  |
|   |  AWS Console / CLI / SDK  |   |  OpenSearch REST APIs      |  |
|   |        |                  |   |   GET /index/_search       |  |
|   |        v                  |   |   PUT /index/_doc/1        |  |
|   |  CreateDomain             |   |   POST /_bulk              |  |
|   |  UpdateDomainConfig       |   |   GET /_cluster/health     |  |
|   |  UpgradeElasticsearch     |   |                            |  |
|   |  DescribeDomain           |   |  +-------+  +-------+     |  |
|   |  DeleteDomain             |   |  | Node1 |  | Node2 |     |  |
|   |                           |   |  | (data)|  | (data)|     |  |
|   |  Provisioning Engine      |   |  +-------+  +-------+     |  |
|   |  Upgrade Orchestrator     |   |  +-------+  +-------+     |  |
|   |  Health Monitor           |   |  | Node3 |  |Master |     |  |
|   |  Auto-Tune                |   |  | (data)|  | (ded) |     |  |
|   |  Patch Manager            |   |  +-------+  +-------+     |  |
|   |                           |   |                            |  |
|   +---------------------------+   +----------------------------+  |
|                                                                   |
|   Control Plane APIs:              Data Plane APIs:               |
|   aws opensearch create-domain     curl https://domain/_search    |
|   aws opensearch update-config     curl https://domain/_bulk      |
|   aws opensearch upgrade-domain    curl https://domain/_cat/nodes |
+-------------------------------------------------------------------+
```

### Control Plane Responsibilities

| Responsibility | What AWS Does |
|---|---|
| **Provisioning** | Launches EC2 instances, attaches EBS, configures networking |
| **Version upgrades** | Orchestrates rolling or blue-green upgrades across nodes |
| **Patching** | Applies OS and OpenSearch security patches |
| **Monitoring** | Publishes CloudWatch metrics, detects unhealthy nodes |
| **Auto-Tune** | Adjusts JVM heap, queue sizes, cache settings based on usage patterns |
| **Node replacement** | Detects failed nodes, launches replacements, re-allocates shards |
| **Backup orchestration** | Triggers hourly automated snapshots to S3 |

### Data Plane Responsibilities

| Responsibility | What the Customer Controls |
|---|---|
| **Index management** | Create/delete indices, define mappings, configure settings |
| **Data ingestion** | Bulk indexing, single-doc writes, ingest pipelines |
| **Search** | Full-text queries, aggregations, SQL queries |
| **Cluster settings** | Subset of cluster-level settings (not all are exposed) |
| **ISM policies** | Index State Management — automated rollover, deletion, migration |

**Key insight for interviews:** The customer never has SSH access. All cluster management
goes through the control plane API or the OpenSearch REST API. AWS can perform maintenance
without customer intervention because the control plane has privileged access to the
underlying infrastructure.

---

## 3. Blue-Green Deployments

When you change a domain configuration (instance type, instance count, engine version, etc.),
AWS does NOT perform in-place updates. Instead, it uses a **blue-green deployment** strategy.

### How It Works

```
PHASE 1: Current state ("Blue" environment running)
+-------+-------+-------+
| Node1 | Node2 | Node3 |     <-- serving traffic
| shard | shard | shard |
|  0,1  |  2,3  |  4,5  |
+-------+-------+-------+
        |
        v  User triggers config change (e.g., r6g.large -> r6g.xlarge)

PHASE 2: AWS launches new "Green" environment in parallel
+-------+-------+-------+     +--------+--------+--------+
| Node1 | Node2 | Node3 |     | Node1' | Node2' | Node3' |
| (old) | (old) | (old) |     | (new)  | (new)  | (new)  |
| r6g.L | r6g.L | r6g.L |     | r6g.XL | r6g.XL | r6g.XL |
+-------+-------+-------+     +--------+--------+--------+
  serving traffic                 shards migrating -->

PHASE 3: Shard migration complete, health validated
+-------+-------+-------+     +--------+--------+--------+
| Node1 | Node2 | Node3 |     | Node1' | Node2' | Node3' |
| empty | empty | empty |     | shard  | shard  | shard  |
|       |       |       |     | 0,1    | 2,3    | 4,5    |
+-------+-------+-------+     +--------+--------+--------+
                                 traffic swapped here -->

PHASE 4: Old nodes terminated
                               +--------+--------+--------+
                               | Node1' | Node2' | Node3' |
                               | r6g.XL | r6g.XL | r6g.XL |
                               | shard  | shard  | shard  |
                               | 0,1    | 2,3    | 4,5    |
                               +--------+--------+--------+
                                 serving traffic (done)
```

### Important Characteristics

- **Temporarily doubles resources**: During migration, both old and new nodes exist. You are
  not billed for the extra nodes, but you need to ensure your VPC has enough IP addresses and
  your account has sufficient EC2 limits.
- **Minimizes downtime**: Traffic continues on old nodes during migration. Brief DNS switch
  at the end (seconds to minutes).
- **Indexing continues**: Writes go to both environments during the transition period.
- **Triggers**: Changing instance type, instance count, storage size, engine version,
  encryption settings, VPC configuration, snapshot settings.
- **Duration**: Can take minutes (small clusters) to hours (large clusters with many shards).

### When Blue-Green is NOT Used

Some changes are applied in-place without blue-green:
- Changing access policies
- Enabling/disabling slow logs
- Auto-Tune adjustments
- Changing CloudWatch alarm settings

---

## 4. Multi-AZ Deployment

### Zone Awareness

AWS OpenSearch supports deploying data nodes across **2 or 3 Availability Zones** for high
availability. This is called **zone awareness**.

```
                     Region: us-east-1
  +--------------------------------------------------+
  |                                                    |
  |  +------------+  +------------+  +------------+   |
  |  |   AZ-1a    |  |   AZ-1b    |  |   AZ-1c    |   |
  |  |            |  |            |  |            |   |
  |  | Data Node1 |  | Data Node2 |  | Data Node3 |   |
  |  | Primary: 0 |  | Primary: 1 |  | Primary: 2 |   |
  |  | Replica: 1 |  | Replica: 2 |  | Replica: 0 |   |
  |  |            |  |            |  |            |   |
  |  | Master (d) |  | Master (d) |  | Master (d) |   |
  |  +------------+  +------------+  +------------+   |
  |                                                    |
  +--------------------------------------------------+

  (d) = dedicated master node

  Zone awareness ensures: primary shard X and its replica
  are NEVER in the same AZ.
```

### 2-AZ vs 3-AZ

| Aspect | 2-AZ | 3-AZ (Recommended) |
|---|---|---|
| **AZ failure tolerance** | Partial — lose 50% of nodes | Full — lose 33%, remaining 66% can serve |
| **Replica placement** | Primary in AZ-A, replica in AZ-B | Distributed evenly across all 3 AZs |
| **Master nodes** | 2 masters = split-brain risk | 3 masters = quorum (2/3) survives 1 AZ loss |
| **Node count** | Must be even (multiples of 2) | Must be multiples of 3 |
| **Availability SLA** | 99.9% | 99.9% (but better in practice) |

### How Zone Awareness Works Under the Hood

1. OpenSearch has an `awareness.attributes` cluster setting with value `zone`.
2. Each node is tagged with its AZ (e.g., `node.attr.zone: us-east-1a`).
3. The shard allocator **enforces** that a primary and its replica(s) cannot share the same
   zone value.
4. During AZ failure, the cluster goes **yellow** (missing replicas), NOT red (missing
   primaries), because at least one copy of every shard survives in another AZ.

### Dedicated Master Placement (3-AZ)

With 3 dedicated master nodes across 3 AZs, losing one AZ still leaves 2 masters to form
a quorum. The cluster remains operational for both reads and writes. The missing replicas
are rebuilt when the AZ recovers.

---

## 5. Storage Tiers (Hot / UltraWarm / Cold)

OpenSearch Service provides three storage tiers for **lifecycle-based data management**. This
is especially important for log analytics where recent data is queried frequently but older
data is rarely accessed.

```
                        DATA LIFECYCLE

  Ingest --> [ HOT TIER ] --> [ ULTRAWARM TIER ] --> [ COLD TIER ]
              (hours-days)      (weeks-months)       (months-years)

  +-----------------------------------------------------------------+
  |                                                                   |
  |  HOT (Data Nodes)                                                |
  |  +----------+  +----------+  +----------+                       |
  |  | EBS gp3  |  | EBS gp3  |  | EBS gp3  |                       |
  |  | r6g.xl   |  | r6g.xl   |  | r6g.xl   |                       |
  |  | Read+    |  | Read+    |  | Read+    |                       |
  |  | Write    |  | Write    |  | Write    |                       |
  |  +----------+  +----------+  +----------+                       |
  |       |                                                          |
  |       | ISM policy: migrate after 7 days                         |
  |       v                                                          |
  |  ULTRAWARM (S3-backed + local SSD cache)                        |
  |  +--------------------------------------------------+           |
  |  | ultrawarm1.large  | ultrawarm1.large              |           |
  |  |                   |                               |           |
  |  |  Local SSD cache  |  Local SSD cache              |           |
  |  |    (hot data)     |    (hot data)                 |           |
  |  |       |           |       |                       |           |
  |  |       v           |       v                       |           |
  |  |  +--------------------------------------+         |           |
  |  |  |         Amazon S3 (durable store)    |         |           |
  |  |  |    Lucene segments stored as objects  |         |           |
  |  |  +--------------------------------------+         |           |
  |  +--------------------------------------------------+           |
  |       |                                                          |
  |       | ISM policy: migrate to cold after 90 days                |
  |       v                                                          |
  |  COLD (S3 only, detached)                                       |
  |  +--------------------------------------+                       |
  |  |         Amazon S3                     |                       |
  |  |   Index metadata + Lucene segments    |                       |
  |  |   No compute attached                 |                       |
  |  |   Must re-attach to UltraWarm to      |                       |
  |  |   query (takes minutes)               |                       |
  |  +--------------------------------------+                       |
  |                                                                   |
  +-----------------------------------------------------------------+
```

### Tier Comparison

| Aspect | Hot | UltraWarm | Cold |
|---|---|---|---|
| **Storage backend** | EBS (gp3/io2) | S3 + local SSD cache | S3 only |
| **Read/Write** | Read + Write | Read-only | Must attach first |
| **Latency** | Milliseconds | Milliseconds (cached), seconds (cache miss) | Minutes to attach |
| **Cost (storage)** | $$$ (EBS pricing) | $ (S3 pricing) | $ (S3 pricing) |
| **Cost (compute)** | $$$ (data node instances) | $$ (UltraWarm instances) | Near-zero |
| **Typical use** | Last 1-7 days | 7-90 days | 90+ days, compliance |
| **Managed by** | Standard shard allocation | UltraWarm nodes | Detached, no compute |

### Index State Management (ISM) — Automating Tier Transitions

ISM policies automate the lifecycle. Example policy:

```json
{
  "policy": {
    "policy_id": "log-lifecycle",
    "default_state": "hot",
    "states": [
      {
        "name": "hot",
        "transitions": [
          { "state_name": "warm", "conditions": { "min_index_age": "7d" } }
        ]
      },
      {
        "name": "warm",
        "actions": [{ "warm_migration": {} }],
        "transitions": [
          { "state_name": "cold", "conditions": { "min_index_age": "90d" } }
        ]
      },
      {
        "name": "cold",
        "actions": [{ "cold_migration": {} }],
        "transitions": [
          { "state_name": "delete", "conditions": { "min_index_age": "365d" } }
        ]
      },
      {
        "name": "delete",
        "actions": [{ "cold_delete": {} }]
      }
    ]
  }
}
```

### Cost Impact Example

```
Scenario: 10 TB of log data, 1 year retention

Without tiers (all hot):
  10 TB x 12 months x EBS gp3 + data nodes = ~$25,000/year

With tiers:
  Hot (7 days):      ~200 GB on EBS          =  ~$800/year
  UltraWarm (90 days): ~2.5 TB on S3+nodes   =  ~$3,000/year
  Cold (270 days):    ~7.3 TB on S3           =  ~$500/year
  Total                                       =  ~$4,300/year  (~83% savings)
```

---

## 6. Security

AWS OpenSearch Service provides defense-in-depth across network, authentication,
authorization, and encryption.

### 6.1 Network Isolation — VPC Access

```
+------------------------------------------------------+
|                   Customer VPC                        |
|                                                       |
|  +------------+        +---------------------------+  |
|  | Application|------->| VPC Endpoint (ENI)        |  |
|  | (EC2/ECS)  |  HTTPS | subnet-1a: 10.0.1.50     |  |
|  +------------+        | subnet-1b: 10.0.2.50     |  |
|                        | subnet-1c: 10.0.3.50     |  |
|  +------------+        +---------------------------+  |
|  | Lambda     |------->|          |                   |
|  | Function   |        |    OpenSearch Domain          |
|  +------------+        |    (private, no public IP)    |
|                        +---------------------------+  |
|                                                       |
|  Security Groups: Allow TCP 443 from app subnets      |
+------------------------------------------------------+
```

- Domain gets **Elastic Network Interfaces (ENIs)** in your VPC subnets.
- No public IP assigned. Traffic stays within VPC or goes through VPN/Direct Connect.
- Security groups control which sources can reach port 443.

### 6.2 Fine-Grained Access Control (FGAC)

FGAC uses the OpenSearch Security plugin (originally Open Distro). It provides:

| Level | What It Controls | Example |
|---|---|---|
| **Cluster-level** | Cluster-wide operations | Allow `cluster:monitor/*` |
| **Index-level** | Which indices a role can access | Role `logs-reader` can read `logs-*` |
| **Document-level** | Row-level security within an index | Tenant A sees only `{"tenant": "A"}` |
| **Field-level** | Column-level masking/exclusion | Hide `credit_card` field from analysts |

### 6.3 Authentication & Identity

```
Authentication Flow Options:

  1. IAM (SigV4)
     Client --> signs request with SigV4 --> OpenSearch validates with IAM

  2. SAML 2.0
     Client --> redirects to IdP (Okta/ADFS) --> SAML assertion --> OpenSearch Dashboards

  3. Amazon Cognito
     Client --> Cognito User Pool --> Cognito Identity Pool --> IAM role --> OpenSearch

  4. Internal user database
     Client --> HTTP Basic Auth --> OpenSearch internal users (stored in .opendistro_security)
```

- **IAM + resource-based policy**: Access policy on the domain specifies which IAM principals
  can access which HTTP methods/paths.
- **SAML 2.0**: For OpenSearch Dashboards SSO. Users authenticate via corporate IdP.
- **Cognito**: Provides user sign-up/sign-in and maps authenticated users to IAM roles.

### 6.4 Encryption

| Layer | Mechanism | Details |
|---|---|---|
| **At rest** | AWS KMS | EBS volumes, S3 (UltraWarm/cold), automated snapshots. AES-256. |
| **In transit** | TLS 1.2 | All HTTPS traffic between clients and domain. |
| **Node-to-node** | TLS 1.2 | Internal cluster communication (shard replication, cluster state). |

**Note:** Encryption at rest and node-to-node encryption can only be enabled at domain
creation time (cannot be toggled later without recreating the domain).

---

## 7. Monitoring

### CloudWatch Metrics (Key Ones to Know)

| Metric | What It Tells You | Alert Threshold |
|---|---|---|
| `ClusterStatus.green/yellow/red` | Overall cluster health | Alert on red |
| `CPUUtilization` | Node CPU usage | > 80% sustained |
| `JVMMemoryPressure` | Heap usage (%) | > 80% = danger |
| `FreeStorageSpace` | Available EBS space | < 20% of total |
| `SearchLatency` | p50/p99 search latency | Application-specific |
| `IndexingRate` | Docs indexed per second | Baseline deviation |
| `ThreadpoolSearchRejected` | Search requests rejected | > 0 = queue saturated |
| `ThreadpoolWriteRejected` | Write requests rejected | > 0 = backpressure |
| `MasterReachableFromNode` | Can data nodes reach master | Alert on false |
| `Nodes` | Node count | Alert if < expected |
| `AutomatedSnapshotFailure` | Backup failures | Alert on > 0 |

### CloudWatch Logs Integration

Three log types can be published to CloudWatch Logs:

1. **Slow logs (search)**: Queries exceeding a latency threshold. Helps identify expensive
   queries.
2. **Slow logs (indexing)**: Indexing operations exceeding a latency threshold.
3. **Error logs**: OpenSearch server-side errors (shard failures, mapping conflicts, etc.).

```
Example slow search log entry:

[2026-02-28T10:15:32,456][WARN][index.search.slowlog] [node-1]
  [my-index][3] took[2.5s], took_millis[2500], total_hits[15234],
  types[], stats[], search_type[QUERY_THEN_FETCH],
  source[{"query":{"match_all":{}}, "size":10000}]
```

### CloudTrail (API Audit)

CloudTrail logs **control plane** API calls (not data plane REST calls):
- `CreateDomain`, `DeleteDomain`, `UpdateDomainConfig`
- `DescribeDomain`, `ListDomainNames`
- `UpgradeDomain`, `StartServiceSoftwareUpdate`

This tells you **who** made infrastructure changes and **when**.

**Audit logs** (data plane operations like who searched what) are available through the
OpenSearch Security audit log feature, separate from CloudTrail.

---

## 8. Automated Backups

### How Automated Snapshots Work

```
  OpenSearch Cluster                     Amazon S3
  +------------------+                  +-------------------+
  | Lucene segments   | -- hourly -->   | Snapshot repo     |
  | (on EBS volumes)  |  incremental    | /snapshots/       |
  |                   |                 |   2026-02-28T00/  |
  | Index: logs-today |                 |   2026-02-28T01/  |
  | Index: products   |                 |   2026-02-28T02/  |
  +------------------+                  +-------------------+
                                           |
                                           | Only changed
                                           | Lucene segments
                                           | are uploaded
                                           | (incremental)
```

### Key Characteristics

| Aspect | Automated Snapshots | Manual Snapshots |
|---|---|---|
| **Frequency** | Hourly (configurable window) | On-demand |
| **Retention** | 14 days (cannot change) | Until you delete them |
| **Storage** | AWS-managed S3 bucket (no cost) | Your own S3 bucket |
| **Cross-region** | No | Yes (snapshot to S3, replicate, restore) |
| **Incremental** | Yes (only changed segments) | Yes |
| **Restore target** | Same domain or new domain | Any domain (same or different account) |

### Incremental Nature

OpenSearch snapshots are **segment-level incremental**:

1. First snapshot: uploads ALL Lucene segments for all indices.
2. Subsequent snapshots: only uploads segments that were created or changed since the last
   snapshot (due to merges, new documents, deletes).
3. This makes hourly snapshots fast and storage-efficient, even for large clusters.

### Restore Process

- Restoring closes the target index (if it exists) and replaces it with the snapshot data.
- You can restore specific indices (not necessarily the entire cluster).
- You can rename indices during restore to avoid conflicts.

---

## 9. Limitations vs Self-Managed

Understanding these trade-offs is critical for an interview answer about managed vs
self-managed.

| Limitation | AWS Managed | Self-Managed (EC2/K8s) |
|---|---|---|
| **Custom plugins** | Not allowed. Only AWS-bundled plugins. | Install any plugin. |
| **Cluster settings** | Subset exposed via API. Some settings locked. | Full access to `elasticsearch.yml` / `opensearch.yml`. |
| **OS-level access** | No SSH. No `journalctl`. No custom JVM flags. | Full SSH, OS tuning, custom GC settings. |
| **Version upgrades** | AWS-managed blue-green. You pick when. Cannot skip major versions. | Upgrade on your schedule, any path. |
| **Networking** | VPC or public. Cannot do custom DNS easily. | Full network control. |
| **Node roles** | Fixed: data, master, UltraWarm. Cannot create custom roles (e.g., coordinating-only is limited). | Any combination: coordinating-only, ingest-only, ML, etc. |
| **Scaling** | Add/remove nodes via API (triggers blue-green). | Add nodes, join cluster immediately. |
| **Cost** | ~30% premium over raw EC2+EBS for operational simplicity. | Lower infra cost, higher ops cost (people). |
| **Max nodes** | 80 data nodes per domain (soft limit). | No limit. |
| **Multi-cluster** | Cross-cluster search supported but limited. | Full cross-cluster replication and search. |

### When to Choose Managed

- Team lacks deep OpenSearch operational expertise.
- Need to minimize operational burden (patching, upgrades, backups).
- Workload fits within managed service limits.
- Standard plugins are sufficient.

### When to Choose Self-Managed

- Need custom plugins (e.g., custom analyzers, security integrations).
- Need full control over JVM tuning and OS-level configuration.
- Workload exceeds managed service limits (node count, cluster topology).
- Cost optimization at very large scale (> 100 nodes).

---

## 10. OpenSearch Serverless

OpenSearch Serverless is a **separate deployment model** that removes cluster management
entirely. It was introduced to address the operational overhead of provisioning and scaling
clusters.

### Architecture

```
+------------------------------------------------------------------+
|                  OpenSearch Serverless                             |
|                                                                   |
|  "Collection" (replaces cluster + index)                         |
|                                                                   |
|  +----------------------------+  +----------------------------+  |
|  |   INDEXING COMPUTE POOL    |  |    SEARCH COMPUTE POOL     |  |
|  |                            |  |                            |  |
|  |  +------+  +------+       |  |  +------+  +------+       |  |
|  |  | OCU  |  | OCU  |  ...  |  |  | OCU  |  | OCU  |  ...  |  |
|  |  +------+  +------+       |  |  +------+  +------+       |  |
|  |                            |  |                            |  |
|  |  Scales independently     |  |  Scales independently     |  |
|  |  based on ingest rate      |  |  based on query load       |  |
|  +----------------------------+  +----------------------------+  |
|                |                              |                   |
|                v                              v                   |
|  +-----------------------------------------------------------+  |
|  |                    Amazon S3                                |  |
|  |              (shared durable storage)                      |  |
|  +-----------------------------------------------------------+  |
|                                                                   |
+------------------------------------------------------------------+
```

### Key Concepts

| Concept | Description |
|---|---|
| **Collection** | Replaces "domain" and "index". A logical grouping of data. |
| **OCU (OpenSearch Compute Unit)** | The unit of compute. Each OCU = 6 GB RAM + some vCPU. Minimum 2 OCUs for indexing + 2 for search = 4 total. |
| **Indexing compute** | Dedicated pool for write operations. Scales based on ingest rate. |
| **Search compute** | Dedicated pool for read operations. Scales based on query load. |
| **S3 storage** | All data stored in S3. No EBS. Decoupled storage and compute. |

### Collection Types

| Type | Use Case | Characteristics |
|---|---|---|
| **Time-series** | Logs, metrics, traces | Optimized for append-only. No update/delete by ID. Full-text + aggregations. |
| **Search** | Application search, catalogs | Supports update/delete by ID. Full-text + aggregations. Lower write throughput than time-series. |
| **Vector search** | ML embeddings, similarity search | k-NN vector operations. Approximate nearest neighbor. |

### Serverless vs Provisioned Trade-offs

| Aspect | Serverless | Provisioned (Domain) |
|---|---|---|
| **Cluster management** | None | You size, monitor, scale |
| **Scaling** | Automatic (OCU-based) | Manual (add/remove nodes) |
| **Cost model** | Pay per OCU-hour + S3 storage | Pay for instances + EBS (always on) |
| **Min cost** | ~$700/month (4 OCUs minimum) | ~$50/month (single small instance) |
| **Features** | Subset of OpenSearch APIs | Full API surface |
| **Plugins** | No custom plugins, limited built-in | AWS-bundled plugins |
| **Cross-account** | Supports data access policies | VPC peering or resource policies |
| **Latency** | Can have cold-start latency | Consistent (always warm) |
| **ISM policies** | Not supported | Supported |
| **Alerting** | Not supported | Supported |
| **Dashboards** | Supported | Supported |
| **Max data** | 6 TB per collection (as of 2025) | Petabytes |

### When to Use Serverless

- Unpredictable or spiky workloads (scales to zero search/indexing independently).
- Teams that want zero operational overhead.
- Small-to-medium data volumes where the minimum OCU cost is acceptable.

### When NOT to Use Serverless

- Large data volumes (> 6 TB per collection).
- Need full API surface (ISM, alerting, anomaly detection).
- Cost-sensitive workloads with predictable traffic (provisioned can be cheaper).
- Need UltraWarm/cold tier lifecycle management.

---

## 11. Contrast: Managed vs Self-Managed

### Decision Framework

```
                     Self-Managed (EC2/K8s)
                            ^
                            |
            More control,   |
            more ops work   |
                            |
  Custom plugins needed? ---+---> Self-managed
  > 80 data nodes?      ---+---> Self-managed
  Custom JVM tuning?    ---+---> Self-managed
                            |
  - - - - - - - - - - - - -|- - - - - - - - - - - - - -
                            |
  Standard workload?    ---+---> Managed (Domain)
  Small team?           ---+---> Managed (Domain)
  Need backups/HA?      ---+---> Managed (Domain)
                            |
  Spiky / unpredictable ---+---> Serverless
  Zero ops tolerance    ---+---> Serverless
  < 6 TB data           ---+---> Serverless
                            |
            Less control,   |
            less ops work   |
                            v
                     OpenSearch Serverless
```

### Full Comparison

| Dimension | Self-Managed | AWS Managed (Domain) | OpenSearch Serverless |
|---|---|---|---|
| **Provisioning** | Manual (Terraform, Ansible, K8s operator) | API/Console (minutes) | API/Console (minutes) |
| **Scaling** | Manual node addition | Blue-green (minutes-hours) | Automatic (seconds-minutes) |
| **Upgrades** | Rolling restart, manual | Blue-green, AWS-orchestrated | AWS-managed, transparent |
| **Backups** | Manual snapshot scripts | Automated hourly + manual | AWS-managed |
| **Monitoring** | Self-built (Prometheus, Grafana) | CloudWatch integrated | CloudWatch integrated |
| **Security** | Self-configured (TLS, auth plugins) | Built-in (FGAC, KMS, VPC) | Built-in (IAM, encryption) |
| **HA** | Manual multi-AZ setup | Multi-AZ with zone awareness | Built-in replication |
| **Cost efficiency** | Best at large scale | Best at medium scale | Best for spiky/small workloads |
| **Operational burden** | High | Low | Near-zero |
| **Flexibility** | Maximum | Moderate | Limited |

### Interview Framing

When discussing this in an interview, frame the decision around the team's **operational
maturity** and **workload characteristics**:

> "For most teams, AWS OpenSearch Service (provisioned domains) is the right choice. You
> get Multi-AZ HA, automated backups, blue-green upgrades, and integrated security without
> managing infrastructure. The 30% cost premium over self-managed is almost always cheaper
> than the engineering time saved.
>
> I would choose self-managed only if we needed custom plugins, had extreme scale requirements,
> or needed OS-level tuning that the managed service does not expose.
>
> Serverless is compelling for teams with unpredictable traffic patterns, but the minimum
> cost (~$700/month) and API limitations make it unsuitable for large-scale or feature-rich
> deployments."

---

## Quick Reference: Interview Talking Points

1. **"What is a domain?"** — AWS's name for an OpenSearch cluster. Includes data nodes,
   optional dedicated masters, optional UltraWarm/cold storage.

2. **"How does AWS upgrade your cluster?"** — Blue-green deployment. New nodes launched,
   shards migrated, traffic swapped, old nodes terminated. Temporarily doubles resources.

3. **"How does HA work?"** — 3-AZ deployment with zone awareness. Shard allocator ensures
   primary and replica are in different AZs. 3 dedicated masters for quorum.

4. **"How do you handle cost for logs?"** — Three storage tiers. ISM policies automate
   hot -> warm -> cold -> delete lifecycle. UltraWarm is ~3x cheaper than hot. Cold is
   near-zero compute cost.

5. **"What can't you do with managed?"** — No custom plugins, no SSH, limited cluster
   settings, no custom node roles, max 80 data nodes.

6. **"When would you use Serverless?"** — Spiky workloads, zero-ops teams, < 6 TB.
   Trade-off: limited API surface, minimum cost floor, no ISM.
