# Amazon S3 — Scaling & Performance Deep Dive

> Companion deep dive to the [interview simulation](interview-simulation.md). This document explores how S3 scales to handle exabytes of data, hundreds of millions of requests per second, and millions of concurrent customers.

---

## Table of Contents

1. [S3's Scale by the Numbers](#1-s3s-scale-by-the-numbers)
2. [Request Routing Architecture](#2-request-routing-architecture)
3. [Prefix-Based Auto-Partitioning (Performance Focus)](#3-prefix-based-auto-partitioning-performance-focus)
4. [Shuffle Sharding — Blast Radius Isolation](#4-shuffle-sharding--blast-radius-isolation)
5. [S3 Transfer Acceleration](#5-s3-transfer-acceleration)
6. [S3 Select — Predicate Pushdown](#6-s3-select--predicate-pushdown)
7. [Byte-Range Fetches — Parallel Downloads](#7-byte-range-fetches--parallel-downloads)
8. [Multi-AZ Architecture](#8-multi-az-architecture)
9. [Caching Layers](#9-caching-layers)
10. [Rate Limiting & Throttling](#10-rate-limiting--throttling)
11. [Performance Optimization Strategies](#11-performance-optimization-strategies)
12. [S3 Express One Zone (Newest Addition)](#12-s3-express-one-zone-newest-addition)
13. [Performance Comparison Table](#13-performance-comparison-table)
14. [Capacity Planning at S3 Scale](#14-capacity-planning-at-s3-scale)
15. [Cross-References](#15-cross-references)

---

## 1. S3's Scale by the Numbers

Amazon S3 is the largest object store on the planet. The numbers are staggering and worth
internalizing because they inform every design decision in the system.

### Headline Figures (as of 2024)

| Metric                  | Value                                  |
|-------------------------|----------------------------------------|
| Objects stored          | 350+ trillion                          |
| Peak request rate       | Hundreds of millions of requests/sec   |
| Data stored             | Exabytes (1 EB = 1,000 PB)            |
| Active customers        | Millions                               |
| AWS regions served      | 30+                                    |
| Storage classes         | 8 (Standard through Glacier Deep)      |
| Durability target       | 99.999999999% (11 nines)               |

### Growth Trajectory

```
Year     Objects Stored (approx)
─────    ───────────────────────
2013     2 trillion
2015     ~10 trillion (estimated)
2018     ~50 trillion (estimated)
2021     ~100 trillion
2023     ~280 trillion
2024     350+ trillion

Doubling period: roughly every 2 years
```

### What These Numbers Mean for Design

1. **350 trillion objects** means the metadata system alone must index more entries than
   any traditional database could handle. This is why S3 uses a custom distributed
   metadata store with aggressive partitioning.

2. **Hundreds of millions of requests per second** means no single component can be a
   bottleneck. Every layer (DNS, load balancers, front-end servers, metadata partitions,
   storage nodes) must scale horizontally.

3. **Exabytes of data** means the storage layer must handle disk failures as a routine
   event, not an exceptional one. At this scale, multiple disks fail every hour.

4. **Millions of active customers** means isolation is critical. One customer's traffic
   spike cannot degrade another customer's experience.

5. **30+ regions** means the system must be deployable and operable across diverse
   geographies with varying infrastructure quality.

### The "Rule of Exabytes"

At exabyte scale, events that seem improbable become inevitable:

```
If you have 1 million hard drives (realistic for S3):
  - At 2% annual failure rate (AFR): 20,000 drive failures per year
  - That is ~55 drive failures per day
  - Or ~2.3 drive failures per hour
  - Or roughly 1 drive failure every 26 minutes

If you have 350 trillion objects:
  - Even a 0.000001% corruption rate = 3.5 billion corrupted objects
  - This is why S3 checksums everything, everywhere, always
```

---

## 2. Request Routing Architecture

Every S3 request travels through a carefully layered routing stack designed to get
the request to the right server, in the right partition, with minimal latency.

### DNS Resolution

```
Client makes a request to:
  my-bucket.s3.us-east-1.amazonaws.com

Step 1: DNS Resolution
  ┌────────────────┐
  │ Client Browser  │
  │ or SDK          │
  └───────┬────────┘
          │ DNS query: my-bucket.s3.us-east-1.amazonaws.com
          ▼
  ┌────────────────┐
  │ Local DNS       │
  │ Resolver        │
  └───────┬────────┘
          │ Recursive lookup
          ▼
  ┌────────────────┐
  │ Route 53        │ AWS-managed authoritative DNS
  │ (AWS DNS)       │ Returns IPs based on:
  │                 │   - Client location (latency-based routing)
  │                 │   - Server health (health checks)
  │                 │   - Load distribution (weighted routing)
  └───────┬────────┘
          │ Returns IP(s) of regional endpoint
          ▼
  Client connects to the returned IP via HTTPS
```

### Regional Request Flow

Once DNS resolves, the full path of a request within a region looks like this:

```
┌─────────────────────────────────────────────────────────────────────┐
│ Region (us-east-1)                                                  │
│                                                                     │
│  ┌──────────────────────────┐                                      │
│  │ Network Load Balancer    │  Layer 4/7, terminates TLS           │
│  │ (NLB / ALB)              │  Distributes across front-ends       │
│  └────────────┬─────────────┘                                      │
│               │                                                     │
│  ┌────────────▼─────────────┐                                      │
│  │ Front-End Server Fleet   │  Thousands of stateless servers      │
│  │                          │  Parse HTTP, authenticate, authorize │
│  │  ┌─────┐ ┌─────┐ ┌─────┐│  Route to correct partition          │
│  │  │ FE1 │ │ FE2 │ │ FE3 ││  Handle multipart upload mgmt       │
│  │  └──┬──┘ └──┬──┘ └──┬──┘│                                      │
│  └─────┼───────┼───────┼───┘                                      │
│        │       │       │                                            │
│  ┌─────▼───────▼───────▼───┐                                      │
│  │ Partition Router         │  Maps bucket+key → partition ID      │
│  │                          │  Consults cached partition map       │
│  │  Partition Map:          │  Refreshes map on cache miss         │
│  │   [a-g*] → Partition 0  │                                      │
│  │   [g-m*] → Partition 1  │                                      │
│  │   [m-z*] → Partition 2  │                                      │
│  └──────────┬───────────────┘                                      │
│             │                                                       │
│     ┌───────┼───────┐                                              │
│     ▼       ▼       ▼                                              │
│  ┌─────┐ ┌─────┐ ┌─────┐                                          │
│  │ P0  │ │ P1  │ │ P2  │   Metadata Partitions                    │
│  │     │ │     │ │     │   Each owns a key range                   │
│  │     │ │     │ │     │   Replicated across AZs                   │
│  └──┬──┘ └──┬──┘ └──┬──┘                                          │
│     │       │       │                                              │
│     ▼       ▼       ▼                                              │
│  ┌─────────────────────────┐                                      │
│  │ Storage Node Fleet      │                                      │
│  │                         │                                      │
│  │  Millions of disks      │  Data is erasure-coded               │
│  │  organized into         │  Chunks spread across nodes          │
│  │  storage cells          │  Placement decided by metadata       │
│  └─────────────────────────┘                                      │
│                                                                     │
└─────────────────────────────────────────────────────────────────────┘
```

### Key Architectural Properties

**Stateless front-ends:**
- Any front-end server can handle any request for any bucket
- No session affinity, no sticky routing
- A front-end server failure just means the load balancer routes to another one
- Scaling: add more front-end servers when request rate increases

**Partition map caching:**
- Each front-end server caches the partition map locally
- The partition map tells which metadata partition owns which key range
- Cache TTL is short (seconds to minutes) to pick up partition splits quickly
- On cache miss: front-end queries the coordination service for the latest map

**Connection pooling:**
- Front-end servers maintain persistent connection pools to metadata partitions
- Avoids TCP handshake overhead for each request
- Connection pools are sized based on observed traffic patterns

### Request Lifecycle (PUT Object)

```
Time →

Client          Load Balancer    Front-End       Partition      Storage
  │                  │              │  Router        │  Node(s)
  │── PUT object ──►│              │               │              │
  │                  │── route ───►│               │              │
  │                  │              │── auth check ─►│ (IAM)       │
  │                  │              │◄─ auth OK ────│              │
  │                  │              │               │              │
  │                  │              │── lookup ─────►│              │
  │                  │              │  partition     │              │
  │                  │              │◄─ partition ──│              │
  │                  │              │   info         │              │
  │                  │              │               │              │
  │                  │              │── write data ─────────────►│
  │                  │              │   (erasure coded chunks)     │
  │                  │              │◄─ write ACKs ──────────────│
  │                  │              │   (quorum)                   │
  │                  │              │               │              │
  │                  │              │── commit ─────►│              │
  │                  │              │  metadata      │              │
  │                  │              │◄─ committed ──│              │
  │                  │              │               │              │
  │◄── 200 OK ─────│◄─────────────│               │              │
  │                  │              │               │              │
```

---

## 3. Prefix-Based Auto-Partitioning (Performance Focus)

This is one of the most important scaling mechanisms in S3 and a frequent interview
topic. Understanding how S3 partitions metadata is essential.

### Historical Context

**Before 2018:**
- S3 had hard per-prefix rate limits:
  - 3,500 PUT/COPY/POST/DELETE requests per second per prefix
  - 5,500 GET/HEAD requests per second per prefix
- A "prefix" is everything before the last `/` in the key
- Customers had to design their key naming to distribute load

**The old workaround: hash-prefix keys:**
```
BAD (sequential, all land on same partition):
  logs/2024-01-01/event-001.json
  logs/2024-01-01/event-002.json
  logs/2024-01-01/event-003.json

BETTER (hash prefix distributes across partitions):
  a1b2/logs/2024-01-01/event-001.json
  c3d4/logs/2024-01-01/event-002.json
  e5f6/logs/2024-01-01/event-003.json

Where a1b2, c3d4, e5f6 are hex hashes of the original key
```

**After 2018:**
- S3 introduced automatic prefix partitioning
- No more workarounds needed for most use cases
- S3 monitors traffic patterns and splits partitions transparently
- Customers can now use any key naming scheme

### How Auto-Partitioning Works

The fundamental mechanism is range-based metadata partitioning with dynamic splitting.

```
┌──────────────────────────────────────────────────────────────────┐
│ Metadata Partition Lifecycle                                      │
│                                                                   │
│ Step 1: New Bucket Created                                       │
│   ┌──────────────────────────────────┐                           │
│   │ Partition P0                      │                          │
│   │ Key range: [0x0000...  0xFFFF...] │  (entire keyspace)       │
│   │ Current load: 50 req/s            │                          │
│   └──────────────────────────────────┘                           │
│                                                                   │
│ Step 2: Traffic Grows                                            │
│   ┌──────────────────────────────────┐                           │
│   │ Partition P0                      │                          │
│   │ Key range: [0x0000...  0xFFFF...] │                          │
│   │ Current load: 8,000 req/s         │ ← approaching threshold │
│   └──────────────────────────────────┘                           │
│                                                                   │
│ Step 3: Split Triggered                                          │
│   ┌───────────────────┐  ┌───────────────────┐                  │
│   │ Partition P0       │  │ Partition P1       │                 │
│   │ [0x0000...0x7FFF..]│  │ [0x8000...0xFFFF..]│                 │
│   │ Load: 4,000 req/s  │  │ Load: 4,000 req/s  │                │
│   └───────────────────┘  └───────────────────┘                  │
│                                                                   │
│ Step 4: Hotspot Develops on P0                                   │
│   ┌───────────────────┐  ┌───────────────────┐                  │
│   │ Partition P0       │  │ Partition P1       │                 │
│   │ [0x0000...0x7FFF..]│  │ [0x8000...0xFFFF..]│                 │
│   │ Load: 15,000 req/s │  │ Load: 2,000 req/s  │                │
│   └───────────────────┘  └───────────────────┘                  │
│         ↓ SPLIT                                                   │
│   ┌──────────┐ ┌──────────┐  ┌───────────────────┐              │
│   │ P0       │ │ P2       │  │ P1                 │              │
│   │ [0-3FFF] │ │ [4-7FFF] │  │ [8000-FFFF]        │              │
│   │ 7500/s   │ │ 7500/s   │  │ 2000/s             │              │
│   └──────────┘ └──────────┘  └───────────────────┘              │
│                                                                   │
└──────────────────────────────────────────────────────────────────┘
```

### Split Mechanics in Detail

The split process involves several coordinated steps:

```
1. DETECT
   ┌────────────────────────────────────┐
   │ Monitoring Agent (per partition)    │
   │                                    │
   │ Tracks:                            │
   │   - PUT requests/sec               │
   │   - GET requests/sec               │
   │   - DELETE requests/sec            │
   │   - Total bytes/sec                │
   │   - P99 latency                    │
   │                                    │
   │ Trigger condition:                 │
   │   rate > threshold for             │
   │   sustained period (e.g., 5 min)   │
   └──────────────┬─────────────────────┘
                  │
2. PLAN           ▼
   ┌────────────────────────────────────┐
   │ Split Planner                      │
   │                                    │
   │ - Scan keys in partition           │
   │ - Find the median key (split pt)  │
   │ - Alternatively: find the key      │
   │   that best balances load          │
   │ - Prepare two new partition        │
   │   definitions                      │
   └──────────────┬─────────────────────┘
                  │
3. EXECUTE        ▼
   ┌────────────────────────────────────┐
   │ Split Executor                     │
   │                                    │
   │ - Create two new partition nodes   │
   │ - Copy metadata entries:           │
   │   Left half → new Partition A      │
   │   Right half → new Partition B     │
   │ - This is a clean range split,     │
   │   no key rehashing needed          │
   └──────────────┬─────────────────────┘
                  │
4. CUTOVER        ▼
   ┌────────────────────────────────────┐
   │ Partition Map Updater              │
   │                                    │
   │ - Atomically update partition map  │
   │   in coordination service          │
   │ - Old: P0 → [a*, z*]              │
   │   New: PA → [a*, m*]              │
   │        PB → [m*, z*]              │
   │ - Front-end servers detect stale   │
   │   map on next request (or via      │
   │   push notification)               │
   │ - Brief redirect period:           │
   │   old partition → new partition    │
   └────────────────────────────────────┘
```

### Worked Example: Traffic Ramp on a New Bucket

A customer creates a new bucket and begins uploading images with the prefix `images/`.
Over the next few hours, traffic ramps up:

```
Time T0 — Bucket Created:
  ┌──────────────────────────────────────────┐
  │ P0: [entire keyspace]                     │
  │ Traffic: 100 req/s                        │
  │ Status: HEALTHY                           │
  └──────────────────────────────────────────┘

Time T1 — 1 hour later (traffic growing):
  ┌──────────────────────────────────────────┐
  │ P0: [entire keyspace]                     │
  │ Traffic: 10,000 req/s                     │
  │ Status: SPLIT TRIGGERED                   │
  └──────────────────────────────────────────┘

Time T2 — After first split:
  ┌────────────────────┐  ┌────────────────────┐
  │ P0: [a* — m*]      │  │ P1: [m* — z*]      │
  │ Traffic: 5,000 req/s│  │ Traffic: 5,000 req/s│
  │ Status: HEALTHY     │  │ Status: HEALTHY     │
  └────────────────────┘  └────────────────────┘

Time T3 — Prefix 'images/' goes viral:
  ┌────────────────────┐  ┌────────────────────┐
  │ P0: [a* — m*]      │  │ P1: [m* — z*]      │
  │ Traffic: 50,000/s   │  │ Traffic: 5,000 req/s│
  │ (hotspot: images/*) │  │ Status: HEALTHY     │
  │ Status: SPLITTING   │  └────────────────────┘
  └────────────────────┘

Time T4 — After second split on P0:
  ┌──────────────┐ ┌──────────────┐ ┌────────────────────┐
  │ P0: [a*—g*]  │ │ P2: [g*—m*]  │ │ P1: [m*—z*]        │
  │ 5,000 req/s  │ │ 45,000 req/s │ │ 5,000 req/s         │
  │ HEALTHY      │ │ images/* hot  │ │ HEALTHY             │
  └──────────────┘ │ SPLITTING    │ └────────────────────┘
                   └──────────────┘

Time T5 — After third split (P2 splits):
  ┌──────────────┐ ┌───────────────────┐ ┌───────────────────┐ ┌─────────────┐
  │ P0: [a*—g*]  │ │ P2: [g*—images/m*]│ │ P3:[images/m*—m*] │ │ P1: [m*—z*] │
  │ 5,000 req/s  │ │ 22,500 req/s      │ │ 22,500 req/s      │ │ 5,000 req/s │
  │ HEALTHY      │ │ HEALTHY           │ │ HEALTHY           │ │ HEALTHY     │
  └──────────────┘ └───────────────────┘ └───────────────────┘ └─────────────┘
```

### Why This Matters in Interviews

The key insight is: **S3 can handle virtually unlimited request rates, but it needs
time to scale.** If you instantly send 100,000 req/s to a brand-new bucket, you will
get throttled (503 SlowDown). If you ramp up gradually, S3 splits partitions to keep up.

**Common interview follow-up:** "What happens if a customer sends 100K req/s immediately?"

Answer: S3 returns 503 SlowDown errors. The customer should either:
1. Ramp up gradually (let auto-partitioning catch up)
2. Contact AWS support to pre-partition the bucket
3. Spread traffic across multiple prefixes to hit different partitions from the start

---

## 4. Shuffle Sharding — Blast Radius Isolation

Shuffle sharding is a fundamental technique that S3 uses (and that AWS services in
general use) to minimize the impact of failures and noisy neighbors.

### The Problem

At S3's scale, failures are constant. Hardware fails. Software has bugs. Customers
send unexpected traffic patterns. The question is not "how do we prevent failures?" but
"how do we limit the blast radius of any single failure?"

### Traditional Sharding vs. Shuffle Sharding

```
TRADITIONAL SHARDING (BAD)
═══════════════════════════

  Shard 1              Shard 2              Shard 3
  ┌──────────────┐    ┌──────────────┐    ┌──────────────┐
  │ Customer A   │    │ Customer D   │    │ Customer G   │
  │ Customer B   │    │ Customer E   │    │ Customer H   │
  │ Customer C   │    │ Customer F   │    │ Customer I   │
  └──────────────┘    └──────────────┘    └──────────────┘

  Problem: If Shard 1 has issues (overloaded by Customer A),
  then Customer B and Customer C are ALSO affected.
  Blast radius = 100% of customers on that shard.


SHUFFLE SHARDING (GOOD)
════════════════════════

  8 total servers. Each customer assigned 2 random servers.

  Customer A → {Server 1, Server 5}
  Customer B → {Server 2, Server 6}
  Customer C → {Server 3, Server 7}
  Customer D → {Server 4, Server 8}
  Customer E → {Server 1, Server 6}
  Customer F → {Server 2, Server 7}
  Customer G → {Server 3, Server 8}
  Customer H → {Server 4, Server 5}

  Visual (which customers are on which server):

  Server: │ S1 │ S2 │ S3 │ S4 │ S5 │ S6 │ S7 │ S8 │
  ────────┼────┼────┼────┼────┼────┼────┼────┼────┤
  Cust A  │ X  │    │    │    │ X  │    │    │    │
  Cust B  │    │ X  │    │    │    │ X  │    │    │
  Cust C  │    │    │ X  │    │    │    │ X  │    │
  Cust D  │    │    │    │ X  │    │    │    │ X  │
  Cust E  │ X  │    │    │    │    │ X  │    │    │
  Cust F  │    │ X  │    │    │    │    │ X  │    │
  Cust G  │    │    │ X  │    │    │    │    │ X  │
  Cust H  │    │    │    │ X  │ X  │    │    │    │

  If Server 1 fails:
    - Customer A loses 1 of 2 servers → 50% capacity, still serving
    - Customer E loses 1 of 2 servers → 50% capacity, still serving
    - Customers B, C, D, F, G, H → completely unaffected

  If Customer A sends a traffic spike that overwhelms Server 1:
    - Customer E is partially affected (shares Server 1)
    - But Customer E still has Server 6 handling requests
    - All other customers: completely unaffected
```

### Blast Radius Math

With `n` total servers and each customer assigned `k` servers:

```
Number of possible assignments per customer = C(n, k)

Probability two customers share the EXACT same set of servers:
  P(exact overlap) = 1 / C(n, k)

Example: n = 100 servers, k = 5 per customer
  C(100, 5) = 75,287,520

  P(exact overlap) = 1 / 75,287,520 ≈ 0.0000013%

  Compare with traditional sharding (20 shards):
    P(same shard) = 1/20 = 5%

  Improvement factor: ~3,764,376x better isolation!
```

**Impact of a single server failure:**

```
Traditional sharding (20 shards, 5 servers each):
  1 server fails → 1 shard affected → 5% of customers lose ALL capacity

Shuffle sharding (100 servers, 5 per customer):
  1 server fails → affected customers lose 1/5 (20%) of capacity
  Number of customers affected = (num_customers * 5) / 100 = 5% partially affected
  But none of them lose ALL capacity → graceful degradation
```

### How S3 Implements Shuffle Sharding

S3 applies shuffle sharding at multiple layers:

```
Layer 1: Data Placement
  Object chunks are placed on random storage nodes
  ┌────────┐
  │ Object │ → Erasure-coded into 6 chunks
  └────────┘   Placed on: Node 17, Node 42, Node 89, Node 103, Node 156, Node 201
                (randomly selected from thousands of nodes)

Layer 2: Metadata Placement
  Each customer's metadata is on random metadata partitions
  Customer A's buckets → {Metadata node 3, 15, 28}
  Customer B's buckets → {Metadata node 7, 15, 41}

Layer 3: Front-End Assignment
  Load balancer distributes requests across front-ends randomly
  No sticky sessions → natural shuffle sharding

Layer 4: Network Path Diversity
  Multiple network paths between components
  Different customers' packets take different physical paths
```

### Shuffle Sharding in Failure Scenarios

```
Scenario: Storage Node 42 Fails
─────────────────────────────────

Before failure:
  Customer A data: Nodes {17, 42, 89, 103, 156, 201}
  Customer B data: Nodes {23, 42, 67, 145, 189, 220}
  Customer C data: Nodes {31, 55, 78, 112, 167, 199}

After Node 42 fails:
  Customer A: 5/6 chunks available → can reconstruct (erasure coding)
  Customer B: 5/6 chunks available → can reconstruct (erasure coding)
  Customer C: 6/6 chunks available → completely unaffected

  Both A and B continue serving reads with ZERO downtime.
  Background repair process creates replacement chunks on healthy nodes.
  After repair:
    Customer A data: Nodes {17, 89, 103, 156, 201, 244}  (Node 42 → 244)
    Customer B data: Nodes {23, 67, 145, 189, 220, 251}  (Node 42 → 251)
```

---

## 5. S3 Transfer Acceleration

### The Problem

Physics constrains data transfer over long distances. The speed of light in fiber is
roughly 200,000 km/s, which means a round trip from Sydney to Virginia (16,000 km)
takes about 160ms just for signal propagation. Add routing, queuing, and congestion,
and real-world RTT is often 200-300ms.

TCP throughput is fundamentally limited by:
```
Throughput ≈ Window_Size / RTT
```

For a 200ms RTT with a typical 64KB TCP window:
```
Throughput = 64 KB / 200ms = 320 KB/s = 2.56 Mbps
```

Even with TCP window scaling (e.g., 1 MB window):
```
Throughput = 1 MB / 200ms = 5 MB/s = 40 Mbps
```

This is far below the client's available bandwidth.

### The Solution: Transfer Acceleration

```
WITHOUT Transfer Acceleration:
══════════════════════════════

  Client            Public Internet (many hops)            S3 Region
  (Sydney)          200-300ms RTT, packet loss             (us-east-1)
  ┌────────┐                                               ┌──────────┐
  │        │ ─── TCP over public internet (slow) ────────► │          │
  │ Upload │     Many hops, variable latency               │ S3       │
  │ Client │     Packet loss causes TCP retransmits        │ Bucket   │
  │        │     High RTT limits TCP window efficiency     │          │
  └────────┘                                               └──────────┘

  Effective throughput: 5-40 Mbps (far below available bandwidth)


WITH Transfer Acceleration:
═══════════════════════════

  Client            Short Hop         AWS Backbone            S3 Region
  (Sydney)          (20ms RTT)        (private, optimized)    (us-east-1)
  ┌────────┐        ┌───────────┐     ┌──────────────┐       ┌──────────┐
  │        │──────►│ CloudFront │───►│ AWS Private   │─────►│          │
  │ Upload │ HTTPS │ Edge       │    │ Network       │      │ S3       │
  │ Client │ short │ (Sydney)   │    │               │      │ Bucket   │
  │        │ hop   │            │    │ Optimized     │      │          │
  └────────┘       └───────────┘    │ routing,      │      └──────────┘
                                     │ low latency,  │
                    20ms RTT         │ high bandwidth │       10-20ms RTT
                    Low packet loss  │ No congestion  │       from backbone
                                     └──────────────┘

  Step 1: Client uploads to nearest CloudFront edge (fast, short hop)
    - RTT: ~20ms (vs 200ms)
    - Throughput: up to line speed
    - Packet loss: minimal (short distance)

  Step 2: Edge forwards to S3 over AWS backbone (fast, private)
    - AWS controls the entire path
    - Optimized TCP settings
    - No public internet congestion
    - Pre-warmed, persistent connections

  Net effect: 50-500% faster uploads for distant clients
```

### Transfer Acceleration Speed Comparison

```
Upload: 1 GB file from Sydney to us-east-1

Without acceleration:
  RTT: 200ms
  Effective throughput: ~20 Mbps (best case with tuning)
  Time: 1 GB / 20 Mbps = 1 GB / 2.5 MB/s = ~400 seconds = ~6.7 minutes

With acceleration:
  Leg 1 (client → edge): RTT 20ms, throughput ~200 Mbps = 25 MB/s
  Leg 2 (edge → S3): RTT ~40ms, throughput ~500 Mbps = 62.5 MB/s
  Bottleneck: Leg 1 at 25 MB/s
  Time: 1 GB / 25 MB/s = ~40 seconds

  Speedup: ~10x
```

### When Transfer Acceleration Helps (and When It Doesn't)

```
HELPS:
  ✓ Client is far from the S3 region (intercontinental)
  ✓ Large file uploads (amortizes the small edge overhead)
  ✓ Client has high available bandwidth (so RTT is the bottleneck)
  ✓ Consistent upload performance needed (private backbone avoids congestion)

DOES NOT HELP:
  ✗ Client is close to the S3 region (e.g., EC2 in the same region)
  ✗ Very small files (connection setup overhead dominates)
  ✗ Client bandwidth is the bottleneck (RTT reduction doesn't help)
  ✗ S3 will automatically detect this and disable acceleration (no charge)
```

### Enabling Transfer Acceleration

```
Bucket-level setting:
  aws s3api put-bucket-accelerate-configuration \
    --bucket my-bucket \
    --accelerate-configuration Status=Enabled

Client uses accelerated endpoint:
  Normal:       my-bucket.s3.us-east-1.amazonaws.com
  Accelerated:  my-bucket.s3-accelerate.amazonaws.com
```

---

## 6. S3 Select — Predicate Pushdown

### The Problem

A common pattern: store large data files in S3, but only need a small subset of the data.

```
Traditional approach:
  ┌──────────┐                    ┌──────────┐
  │  Client  │◄── download ──────│ S3       │
  │          │    entire 1 TB    │ (1 TB    │
  │  Filter  │    CSV file       │  CSV)    │
  │  locally │                    └──────────┘
  │          │
  │  Keep    │
  │  10 MB   │
  │  of data │
  └──────────┘

  Problem:
    - Transfer 1 TB over the network (slow, expensive)
    - Client must parse and filter locally (CPU cost)
    - 99.999% of transferred data is discarded
```

### The Solution: Push the Query to Storage

```
S3 Select approach:
  ┌──────────┐                    ┌──────────┐
  │  Client  │◄── receive ───────│ S3       │
  │          │    only 10 MB     │ (1 TB    │
  │  Done!   │    of matching    │  CSV)    │
  │          │    rows           │          │
  └──────────┘                    │ Filter   │
                                  │ on       │
                                  │ storage  │
                                  │ nodes    │
                                  └──────────┘

  Benefits:
    - Transfer only 10 MB (vs 1 TB) → 100,000x less data
    - Faster: less network transfer time
    - Cheaper: lower data transfer costs
    - Client is simpler: no parsing/filtering code needed
```

### Architecture of S3 Select

```
┌──────────────────────────────────────────────────────────────────┐
│ S3 Select Request Flow                                           │
│                                                                  │
│ Client sends: SelectObjectContent(bucket, key, sql_expression)  │
│                                                                  │
│ ┌───────────┐     ┌──────────────┐     ┌──────────────────────┐ │
│ │           │────►│              │────►│ Storage Node 1       │ │
│ │  Client   │     │  Front-End   │     │ ┌──────────────────┐ │ │
│ │           │     │  Server      │     │ │ Read chunk 1     │ │ │
│ │           │     │              │     │ │ Parse CSV/JSON   │ │ │
│ │           │     │              │     │ │ Apply WHERE      │ │ │
│ │           │     │              │────►│ │ Project columns  │ │ │
│ │           │     │              │     │ │ Return matches   │ │ │
│ │           │     │              │     │ └──────────────────┘ │ │
│ │           │     │              │     └──────────────────────┘ │
│ │           │     │              │                               │
│ │           │     │              │     ┌──────────────────────┐ │
│ │           │     │              │────►│ Storage Node 2       │ │
│ │           │     │              │     │ ┌──────────────────┐ │ │
│ │           │◄────│   Streams    │◄────│ │ Read chunk 2     │ │ │
│ │  Receives │     │   results    │     │ │ Parse CSV/JSON   │ │ │
│ │  filtered │     │   back to    │     │ │ Apply WHERE      │ │ │
│ │  rows     │     │   client     │     │ │ Project columns  │ │ │
│ │           │     │              │     │ │ Return matches   │ │ │
│ └───────────┘     └──────────────┘     │ └──────────────────┘ │ │
│                                         └──────────────────────┘ │
└──────────────────────────────────────────────────────────────────┘
```

### SQL Syntax for S3 Select

```sql
-- Basic filtering
SELECT s.name, s.email, s.status
FROM S3Object s
WHERE s.status = 'ACTIVE'

-- Aggregations
SELECT COUNT(*) as total, AVG(CAST(s.amount AS FLOAT)) as avg_amount
FROM S3Object s
WHERE s.category = 'electronics'

-- LIMIT for sampling
SELECT *
FROM S3Object s
WHERE s.region = 'us-east-1'
LIMIT 1000
```

### Supported Formats and Capabilities

| Feature                | CSV          | JSON          | Parquet        |
|------------------------|--------------|---------------|----------------|
| Column projection      | Yes          | Yes           | Yes (very fast)|
| Row filtering (WHERE)  | Yes          | Yes           | Yes            |
| Aggregations           | SUM, AVG, etc| SUM, AVG, etc | SUM, AVG, etc  |
| LIMIT                  | Yes          | Yes           | Yes            |
| Compression            | GZIP, BZIP2  | GZIP, BZIP2   | Snappy, GZIP   |
| Column pushdown        | No (row fmt) | No (row fmt)  | Yes (columnar) |

**Parquet advantage:** Because Parquet is columnar, S3 Select can skip reading entire
columns that are not in the SELECT list. For a table with 100 columns where you only
need 3, this can reduce I/O by 97%.

---

## 7. Byte-Range Fetches — Parallel Downloads

### The Concept

S3 supports HTTP Range headers, allowing clients to request specific byte ranges of an
object. This enables parallel downloading of different parts of the same object.

### Sequential vs. Parallel Download

```
SEQUENTIAL DOWNLOAD:
════════════════════

  Object: 1 GB

  ┌────────────────────────────────────────────────────────────┐
  │████████████████████████████████████████████████████████████│
  └────────────────────────────────────────────────────────────┘
   ◄──────────── single stream, 8 seconds ──────────────────►

  GET /key HTTP/1.1
  → Stream entire 1 GB over one TCP connection
  → At 1 Gbps: ~8 seconds


PARALLEL BYTE-RANGE DOWNLOAD (4 threads):
═════════════════════════════════════════

  Object: 1 GB, split into 4 ranges

  Thread 1: GET /key  Range: bytes=0-268435455         (0-256 MB)
  Thread 2: GET /key  Range: bytes=268435456-536870911 (256-512 MB)
  Thread 3: GET /key  Range: bytes=536870912-805306367 (512-768 MB)
  Thread 4: GET /key  Range: bytes=805306368-1073741823(768-1024 MB)

  ┌──────────────────┐
  │ Thread 1: 256 MB │  ~2 seconds
  │██████████████████│
  ├──────────────────┤
  │ Thread 2: 256 MB │  ~2 seconds (parallel)
  │██████████████████│
  ├──────────────────┤
  │ Thread 3: 256 MB │  ~2 seconds (parallel)
  │██████████████████│
  ├──────────────────┤
  │ Thread 4: 256 MB │  ~2 seconds (parallel)
  │██████████████████│
  └──────────────────┘

  Total time: ~2 seconds (4x faster!)
  Client reassembles the 4 parts in order.
```

### Why Parallel Downloads Are Faster

```
Single TCP connection:
  - Limited by TCP congestion window
  - Limited by single server's outbound bandwidth allocation
  - Single point of failure (packet loss causes full stream backup)

Multiple parallel connections:
  - Each connection has its own congestion window
  - May hit different storage nodes (S3 can serve ranges independently)
  - Packet loss on one connection doesn't affect others
  - Aggregate bandwidth = N x single connection bandwidth (up to client limit)
```

### Byte-Range Use Cases Beyond Speed

```
Use Case 1: Resume failed downloads
  Download failed at byte 500 MB (of 1 GB)
  Resume: GET /key Range: bytes=524288000-1073741823
  → Only download the remaining 500 MB

Use Case 2: Read specific part of a file
  Video file: seek to timestamp 01:23:45
  Calculate byte offset for that timestamp
  GET /key Range: bytes=123456789-234567890
  → Only download the relevant segment

Use Case 3: Read file headers
  Parquet file: footer contains schema (last N bytes)
  GET /key Range: bytes=-1024
  → Read just the last 1024 bytes to get the schema
  → Then read only the column chunks you need

Use Case 4: Parallel processing (MapReduce-style)
  Worker 1: process bytes 0-256MB
  Worker 2: process bytes 256MB-512MB
  Worker 3: process bytes 512MB-768MB
  Worker 4: process bytes 768MB-1024MB
  → Each worker downloads and processes its range independently
```

### AWS SDK Automatic Parallelization

```
The AWS SDK (e.g., aws s3 cp) automatically uses parallel byte-range fetches
for large downloads. Configuration parameters:

  aws configure set default.s3.max_concurrent_requests 10
  aws configure set default.s3.multipart_threshold 64MB
  aws configure set default.s3.multipart_chunksize 16MB

  Default: 10 concurrent requests
  Each chunk: 8-16 MB
  For a 1 GB file: ~64 parallel requests of 16 MB each
```

---

## 8. Multi-AZ Architecture

S3 Standard stores data across a minimum of three Availability Zones within a region.
This provides resilience against the complete loss of any single AZ.

### What Is an Availability Zone?

```
An Availability Zone (AZ) is:
  - One or more physical data centers
  - Independent power, cooling, and networking
  - Connected to other AZs via low-latency private fiber
  - Physically separated (different buildings, often different campuses)
  - Close enough for <2ms latency between AZs in the same region

  Region: us-east-1
  ┌────────────────────────────────────────────────────────────────┐
  │                                                                │
  │   AZ-a                  AZ-b                  AZ-c            │
  │   ┌──────────┐         ┌──────────┐         ┌──────────┐     │
  │   │ ████████ │         │ ████████ │         │ ████████ │     │
  │   │ ████████ │         │ ████████ │         │ ████████ │     │
  │   │ Data Ctr │         │ Data Ctr │         │ Data Ctr │     │
  │   └────┬─────┘         └────┬─────┘         └────┬─────┘     │
  │        │                    │                    │             │
  │        │◄── 1-2ms RTT ────►│◄── 1-2ms RTT ────►│             │
  │        │  (private fiber)   │  (private fiber)   │             │
  │                                                                │
  │   Each AZ: independent power, cooling, networking             │
  │   Distance: typically 10-100 km apart                          │
  └────────────────────────────────────────────────────────────────┘
```

### S3's Multi-AZ Deployment

```
┌────────────────────────────────────────────────────────────────────────┐
│ Region: us-east-1                                                      │
│                                                                        │
│  AZ-a (us-east-1a)       AZ-b (us-east-1b)       AZ-c (us-east-1c)  │
│  ┌────────────────┐      ┌────────────────┐      ┌────────────────┐  │
│  │ FRONT-END      │      │ FRONT-END      │      │ FRONT-END      │  │
│  │ ┌────┐ ┌────┐ │      │ ┌────┐ ┌────┐ │      │ ┌────┐ ┌────┐ │  │
│  │ │FE-1│ │FE-2│ │      │ │FE-3│ │FE-4│ │      │ │FE-5│ │FE-6│ │  │
│  │ └────┘ └────┘ │      │ └────┘ └────┘ │      │ └────┘ └────┘ │  │
│  ├────────────────┤      ├────────────────┤      ├────────────────┤  │
│  │ METADATA       │      │ METADATA       │      │ METADATA       │  │
│  │ ┌────────────┐ │      │ ┌────────────┐ │      │ ┌────────────┐ │  │
│  │ │ Replica A  │ │      │ │ Replica B  │ │      │ │ Replica C  │ │  │
│  │ │ (Paxos)    │ │      │ │ (Paxos)    │ │      │ │ (Paxos)    │ │  │
│  │ └────────────┘ │      │ └────────────┘ │      │ └────────────┘ │  │
│  ├────────────────┤      ├────────────────┤      ├────────────────┤  │
│  │ WITNESS        │      │ WITNESS        │      │ WITNESS        │  │
│  │ ┌────────────┐ │      │ ┌────────────┐ │      │ ┌────────────┐ │  │
│  │ │ Witness A  │ │      │ │ Witness B  │ │      │ │ Witness C  │ │  │
│  │ │ (Lightweight│ │      │ │ (Lightweight│ │      │ │ (Lightweight│ │  │
│  │ │  Paxos node)│ │      │ │  Paxos node)│ │      │ │  Paxos node)│ │  │
│  │ └────────────┘ │      │ └────────────┘ │      │ └────────────┘ │  │
│  ├────────────────┤      ├────────────────┤      ├────────────────┤  │
│  │ STORAGE        │      │ STORAGE        │      │ STORAGE        │  │
│  │ ┌────┐ ┌────┐ │      │ ┌────┐ ┌────┐ │      │ ┌────┐ ┌────┐ │  │
│  │ │Node│ │Node│ │      │ │Node│ │Node│ │      │ │Node│ │Node│ │  │
│  │ │ 1  │ │ 2  │ │      │ │ 3  │ │ 4  │ │      │ │ 5  │ │ 6  │ │  │
│  │ └────┘ └────┘ │      │ └────┘ └────┘ │      │ └────┘ └────┘ │  │
│  │  (data chunks) │      │  (data chunks) │      │  (data chunks) │  │
│  └────────────────┘      └────────────────┘      └────────────────┘  │
│                                                                        │
│  ┌──────────────────────────────────────────────────────────────────┐ │
│  │ Cross-AZ Load Balancer                                           │ │
│  │ Routes requests to healthy front-ends in any AZ                  │ │
│  └──────────────────────────────────────────────────────────────────┘ │
│                                                                        │
│  PROPERTIES:                                                           │
│  • Data chunks distributed across all 3 AZs (erasure coding)         │
│  • Metadata replicated via Paxos across all 3 AZs                    │
│  • Front-end servers in all AZs behind cross-AZ load balancer        │
│  • Any single AZ can fail → system continues operating               │
│  • Quorum: 2 of 3 AZs must agree for writes (Paxos majority)        │
│                                                                        │
└────────────────────────────────────────────────────────────────────────┘
```

### AZ Failure Scenario

```
Scenario: AZ-b experiences complete power failure
═════════════════════════════════════════════════════

Before failure:
  AZ-a: FE-1, FE-2, Meta-A, Witness-A, Storage 1-2  [HEALTHY]
  AZ-b: FE-3, FE-4, Meta-B, Witness-B, Storage 3-4  [HEALTHY]
  AZ-c: FE-5, FE-6, Meta-C, Witness-C, Storage 5-6  [HEALTHY]

AZ-b goes down:
  AZ-a: FE-1, FE-2, Meta-A, Witness-A, Storage 1-2  [HEALTHY]
  AZ-b: FE-3, FE-4, Meta-B, Witness-B, Storage 3-4  [DOWN]
  AZ-c: FE-5, FE-6, Meta-C, Witness-C, Storage 5-6  [HEALTHY]

Impact analysis:
  Front-end:  4 of 6 servers still running (67% capacity) → OK
  Metadata:   2 of 3 Paxos replicas alive → quorum maintained → OK
  Witness:    2 of 3 alive → quorum maintained → OK
  Storage:    4 of 6 nodes alive → erasure coding tolerates this → OK

  Result: System continues operating with reduced capacity.
          No data loss. No inconsistency.
          Load balancer stops sending traffic to AZ-b.
          Background repair begins: re-replicate chunks from AZ-b to AZ-a and AZ-c.
```

### Why Three AZs?

```
Number of AZs    Quorum Size    Tolerable Failures    Cost
─────────────    ───────────    ──────────────────    ────
1                1              0                     Low
2                2              0 (split-brain risk)  Medium
3                2              1                     Medium-High
5                3              2                     High

3 AZs is the sweet spot:
  - Can lose 1 AZ and maintain quorum (2/3)
  - Reasonable cost (3x replication, or less with erasure coding)
  - Low cross-AZ latency (all within same metro area)
  - Covers the vast majority of failure scenarios
    (simultaneous failure of 2 AZs is extraordinarily rare)
```

---

## 9. Caching Layers

S3 employs multiple layers of caching to reduce latency and load on backend systems.

### Layer 1: Client-Side Caching

```
Client-Side Caching via HTTP Headers:
══════════════════════════════════════

  First request:
    Client → GET /photo.jpg → S3
    S3 → 200 OK
          Cache-Control: max-age=86400
          ETag: "abc123"
          Last-Modified: Wed, 01 Jan 2024 00:00:00 GMT
          [photo data]

  Client caches response locally.

  Second request (within 24 hours):
    Client → Cache hit! Serve from local cache.
    No network request at all.

  After cache expires:
    Client → GET /photo.jpg
             If-None-Match: "abc123"
    S3 → 304 Not Modified (no body)
    Client refreshes cache TTL, serves cached copy.

  If object has changed:
    Client → GET /photo.jpg
             If-None-Match: "abc123"
    S3 → 200 OK
          ETag: "def456"
          [new photo data]
    Client replaces cached copy.
```

### CloudFront CDN in Front of S3

```
┌──────────────────────────────────────────────────────────────────┐
│ CloudFront + S3 Architecture                                     │
│                                                                  │
│  ┌────────┐     ┌──────────────┐     ┌──────────┐              │
│  │ Client │────►│ CloudFront   │────►│ S3       │              │
│  │(Sydney)│     │ Edge (Sydney)│     │(us-east-1│              │
│  └────────┘     └──────────────┘     └──────────┘              │
│    20ms RTT       Cache HIT?                                    │
│                   ├─ YES: serve from edge (20ms total)         │
│                   └─ NO: fetch from S3 origin (200ms)          │
│                         then cache at edge for future requests  │
│                                                                  │
│  Cache hit rate for popular content: 90-99%                     │
│  99 of 100 requests served from edge → 20ms latency            │
│  1 of 100 requests fetches from origin → 200ms latency         │
│  Average latency: ~22ms (vs 200ms without CloudFront)          │
│                                                                  │
│  400+ edge locations worldwide                                   │
│  Automatic cache invalidation via TTL or explicit invalidation  │
└──────────────────────────────────────────────────────────────────┘
```

### Layer 2: Front-End Metadata Cache

```
Front-End Server Metadata Cache:
════════════════════════════════

  ┌──────────────────────────────────────────────────┐
  │ Front-End Server                                  │
  │                                                   │
  │  ┌───────────────────────────────┐               │
  │  │ In-Memory Metadata Cache      │               │
  │  │                               │               │
  │  │  Key: (bucket, object_key)    │               │
  │  │  Value: {                     │               │
  │  │    version_id,                │               │
  │  │    size,                      │               │
  │  │    storage_class,             │               │
  │  │    chunk_locations: [...],    │               │
  │  │    etag,                      │               │
  │  │    last_modified,             │               │
  │  │    cached_at                  │               │
  │  │  }                            │               │
  │  │                               │               │
  │  │  Eviction: LRU with TTL       │               │
  │  │  Cache size: tens of GB       │               │
  │  │  Hit rate: 90-99% for popular │               │
  │  │            objects             │               │
  │  └───────────────────────────────┘               │
  │                                                   │
  │  On cache HIT:                                    │
  │    - Validate with witness (lightweight check)   │
  │    - If still valid → use cached metadata        │
  │    - Skip full metadata partition lookup          │
  │    - Latency: ~1ms for metadata step             │
  │                                                   │
  │  On cache MISS:                                   │
  │    - Query metadata partition (full lookup)       │
  │    - Cache the result for future requests         │
  │    - Latency: ~5-10ms for metadata step          │
  └──────────────────────────────────────────────────┘

  Witness validation (strong consistency):
    Front-end: "Is (bucket, key, version_id) still the latest?"
    Witness:   "Yes" or "No, version_id has changed"

    This is much cheaper than a full metadata lookup.
    Witness stores only: (bucket, key) → latest_version_id
    Small data structure → fast lookups → fits in memory
```

### Layer 3: Storage Node Caching

```
Storage Node Cache Hierarchy:
═════════════════════════════

  ┌─────────────────────────────────────┐
  │ Storage Node                         │
  │                                      │
  │  ┌───────────────────────────┐      │
  │  │ L1: Application Buffer    │      │   Hit: <0.1ms
  │  │     (recently decoded     │      │   Size: MBs
  │  │      erasure-coded chunks)│      │
  │  └─────────────┬─────────────┘      │
  │                │ miss                │
  │  ┌─────────────▼─────────────┐      │
  │  │ L2: OS Page Cache         │      │   Hit: <0.5ms
  │  │     (kernel file cache)   │      │   Size: tens of GB
  │  │     Hot chunks stay in RAM│      │
  │  └─────────────┬─────────────┘      │
  │                │ miss                │
  │  ┌─────────────▼─────────────┐      │
  │  │ L3: SSD Cache Tier        │      │   Hit: <2ms
  │  │     (NVMe SSDs)           │      │   Size: TBs
  │  │     Frequently accessed   │      │
  │  │     chunks cached on SSD  │      │
  │  └─────────────┬─────────────┘      │
  │                │ miss                │
  │  ┌─────────────▼─────────────┐      │
  │  │ L4: HDD (primary storage) │      │   Access: 5-15ms
  │  │     Spinning disks        │      │   Size: PBs
  │  │     Bulk data storage     │      │
  │  └───────────────────────────┘      │
  │                                      │
  └─────────────────────────────────────┘

  Typical hit rates (for a well-warmed cache):
    L1: ~10% of requests (very recent repeats)
    L2: ~30% of requests (hot working set)
    L3: ~20% of requests (warm data)
    L4: ~40% of requests (cold data, must go to disk)

    Weighted average latency: 0.1*0.05 + 0.3*0.3 + 0.2*1.5 + 0.4*10 = 4.4ms
    vs. 10ms if every request went to HDD
```

---

## 10. Rate Limiting & Throttling

### Why S3 Throttles

S3 is a shared, multi-tenant system. Without rate limiting, a single customer could
consume all resources in a partition, degrading performance for everyone else on that
partition. Rate limiting protects both the system and other customers.

### Rate Limiting Layers

```
┌──────────────────────────────────────────────────────────────────┐
│ Rate Limiting Stack                                              │
│                                                                  │
│  Layer 1: Global Admission Control                              │
│  ┌────────────────────────────────────────────────┐             │
│  │ If overall system load > threshold:             │             │
│  │   → Shed lowest-priority requests first        │             │
│  │   → Return 503 Service Unavailable             │             │
│  │ This is the "last resort" safety valve          │             │
│  └────────────────────────────────────────────────┘             │
│                                                                  │
│  Layer 2: Per-Customer Rate Limiting                            │
│  ┌────────────────────────────────────────────────┐             │
│  │ Track request rate per AWS account              │             │
│  │ If account exceeds fair-share quota:            │             │
│  │   → Return 503 SlowDown for that account       │             │
│  │   → Other accounts unaffected                  │             │
│  │ Prevents "noisy neighbor" problem               │             │
│  └────────────────────────────────────────────────┘             │
│                                                                  │
│  Layer 3: Per-Partition Rate Limiting                            │
│  ┌────────────────────────────────────────────────┐             │
│  │ Track request rate per metadata partition        │             │
│  │ If partition is overloaded:                     │             │
│  │   → Return 503 SlowDown                        │             │
│  │   → Trigger auto-partitioning (split)          │             │
│  │ Protects individual partitions from overload    │             │
│  └────────────────────────────────────────────────┘             │
│                                                                  │
│  Layer 4: Per-Bucket Rate Limiting                              │
│  ┌────────────────────────────────────────────────┐             │
│  │ Buckets have implicit rate limits based on      │             │
│  │ their number of partitions                      │             │
│  │ New bucket (1 partition): ~3,500 PUT/s + 5,500  │             │
│  │   GET/s per prefix                              │             │
│  │ After auto-scaling: effectively unlimited       │             │
│  └────────────────────────────────────────────────┘             │
│                                                                  │
└──────────────────────────────────────────────────────────────────┘
```

### The 503 SlowDown Response

```xml
HTTP/1.1 503 Slow Down

<?xml version="1.0" encoding="UTF-8"?>
<Error>
  <Code>SlowDown</Code>
  <Message>Please reduce your request rate.</Message>
  <RequestId>4442587FB7D0A2F9</RequestId>
  <HostId>...</HostId>
</Error>
```

This is NOT an error in the traditional sense. It is S3 telling the client: "I can
handle this request, but not right now. Please try again shortly."

### Client-Side: Exponential Backoff with Jitter

The AWS SDK automatically handles 503 responses with exponential backoff and jitter:

```
Exponential Backoff Algorithm:
══════════════════════════════

  base_delay = 100ms
  max_delay  = 20,000ms  (20 seconds)
  multiplier = 2

  For attempt N (starting at 0):
    delay = min(max_delay, base_delay * 2^N)
    jittered_delay = random(0, delay)
    wait(jittered_delay)
    retry request

  Example sequence:
  ┌─────────┬────────────────┬──────────────────────────────────┐
  │ Attempt │ Base Delay     │ Actual Wait (with jitter)        │
  ├─────────┼────────────────┼──────────────────────────────────┤
  │ 0       │ (first try)    │ 0ms (immediate)                  │
  │ 1       │ 100ms          │ random(0, 100ms) → e.g., 73ms   │
  │ 2       │ 200ms          │ random(0, 200ms) → e.g., 142ms  │
  │ 3       │ 400ms          │ random(0, 400ms) → e.g., 287ms  │
  │ 4       │ 800ms          │ random(0, 800ms) → e.g., 551ms  │
  │ 5       │ 1,600ms        │ random(0, 1600ms) → e.g., 980ms │
  │ 6       │ 3,200ms        │ random(0, 3200ms) → e.g., 2.1s  │
  │ 7       │ 6,400ms        │ random(0, 6400ms) → e.g., 4.8s  │
  │ 8       │ 12,800ms       │ random(0, 12800ms) → e.g., 9.3s │
  │ 9       │ 20,000ms (cap) │ random(0, 20000ms) → e.g., 14s  │
  └─────────┴────────────────┴──────────────────────────────────┘
```

### Why Jitter Is Critical

```
WITHOUT jitter (thundering herd):
═════════════════════════════════

  Time 0ms:  100 clients all get 503
  Time 100ms: ALL 100 clients retry → another thundering herd → more 503s
  Time 200ms: ALL 100 clients retry → same problem
  ...
  System never recovers because retries are synchronized

  ┌───────────────────────────────────────────────────────────┐
  │ Request Rate                                               │
  │ ▲                                                          │
  │ │ ███                    ███                    ███        │
  │ │ ███                    ███                    ███        │
  │ │ ███                    ███                    ███        │
  │ │─│──────────capacity─────│───────────────────────│────── │
  │ │ │                       │                       │        │
  │ └─┴───────────────────────┴───────────────────────┴──────►│
  │   0ms                    100ms                   200ms    │
  │   All retry together → bursts that exceed capacity        │
  └───────────────────────────────────────────────────────────┘


WITH jitter (smooth retry distribution):
════════════════════════════════════════

  Time 0ms:   100 clients all get 503
  Time 0-100ms: clients retry at random times within the window

  ┌───────────────────────────────────────────────────────────┐
  │ Request Rate                                               │
  │ ▲                                                          │
  │ │ ███                                                      │
  │ │─│──────────capacity──────────────────────────────────── │
  │ │ │ ░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░                │
  │ │ │ ░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░                │
  │ │ │                                                        │
  │ └─┴──────────────────────────────────────────────────────►│
  │   0ms                                              200ms  │
  │   Retries spread out → stay below capacity                │
  └───────────────────────────────────────────────────────────┘
```

---

## 11. Performance Optimization Strategies

### For High Request Rates

```
Strategy 1: Distribute keys across prefixes
═══════════════════════════════════════════

  BAD (sequential keys, all hit same partition):
    events/2024-01-01T00:00:01.json
    events/2024-01-01T00:00:02.json
    events/2024-01-01T00:00:03.json

  GOOD (randomized prefixes):
    a3f2/events/2024-01-01T00:00:01.json
    7b91/events/2024-01-01T00:00:02.json
    d4e8/events/2024-01-01T00:00:03.json

  ALSO GOOD (date-partitioned, natural distribution):
    events/2024/01/01/event-a3f2b791.json
    events/2024/01/01/event-7b91d4e8.json
    events/2024/01/02/event-c5f1a023.json


Strategy 2: Use multipart upload for large objects
═══════════════════════════════════════════════════

  Object: 5 GB file

  Single PUT:
    - Max 5 GB per PUT
    - Single stream, single failure point
    - If it fails at 4.5 GB, restart from scratch

  Multipart upload:
    - Split into 500 parts of 10 MB each
    - Upload 10 parts in parallel → 10x throughput
    - If part 47 fails, retry just part 47
    - S3 assembles the parts server-side


Strategy 3: Ramp up traffic gradually
═════════════════════════════════════

  DON'T:
    Time 0: send 100,000 req/s to a brand-new bucket
    → 503 SlowDown everywhere

  DO:
    Time 0:   send 1,000 req/s
    Time 5m:  send 3,000 req/s  (S3 splits partitions)
    Time 10m: send 10,000 req/s (more splits)
    Time 15m: send 30,000 req/s (more splits)
    Time 20m: send 100,000 req/s (fully partitioned, handles load)
```

### For Latency Reduction

```
Strategy                      Latency Impact        When to Use
───────────────────────────   ──────────────        ──────────────────
Choose closest region         -50-200ms             Always
Transfer Acceleration         -50-80% for uploads   Distant uploads
CloudFront                    -50-90% for reads     Read-heavy, cacheable
VPC Endpoint                  -5-20ms               EC2 → S3 in same region
S3 Express One Zone           -80-90%               Ultra-low latency needs
Connection reuse              -5-10ms per request   High request rates
```

### For Throughput

```
Technique                     Throughput Gain        Mechanism
──────────────────────────    ──────────────        ──────────────────
Multi-connection downloads    2-10x                  Parallel byte-range
Multi-connection uploads      2-10x                  Multipart upload
TCP window tuning             1.5-3x                 Larger windows
VPC endpoints                 1.2-2x                 Avoid NAT/IGW bottleneck
S3 Express One Zone           Up to 10x              Optimized storage backend
```

### Decision Matrix: Which Optimization to Use

```
┌────────────────────────────────────────────────────────────────┐
│ Is your bottleneck...                                          │
│                                                                │
│ Request rate (too many requests)?                              │
│   └─► Spread across prefixes                                  │
│   └─► Ramp up gradually                                       │
│   └─► Use CloudFront for cacheable reads                     │
│                                                                │
│ Upload speed (large files too slow)?                          │
│   └─► Use multipart upload                                   │
│   └─► Use Transfer Acceleration (if distant)                 │
│   └─► Increase connection parallelism                        │
│                                                                │
│ Download speed (large files too slow)?                        │
│   └─► Use byte-range parallel fetches                        │
│   └─► Use CloudFront (if cacheable)                          │
│   └─► Increase connection parallelism                        │
│                                                                │
│ First-byte latency (time to first byte too high)?            │
│   └─► Use S3 Express One Zone (if single-AZ is acceptable)  │
│   └─► Use CloudFront (for reads)                             │
│   └─► Use VPC Endpoint (if from EC2)                         │
│   └─► Enable persistent connections / connection pooling     │
│                                                                │
│ Data transfer costs (too expensive)?                          │
│   └─► Use S3 Select (filter server-side)                     │
│   └─► Use CloudFront (cached content is cheaper)             │
│   └─► Use VPC Endpoint (no NAT gateway charges)              │
│                                                                │
└────────────────────────────────────────────────────────────────┘
```

---

## 12. S3 Express One Zone (Newest Addition)

### Overview

S3 Express One Zone is a high-performance storage class introduced in late 2023. It
provides single-digit millisecond latency for both reads and writes, making it suitable
for workloads that previously required block storage or in-memory caching.

### Architecture Differences

```
S3 Standard:
  ┌──────────────────────────────────────────────────────────┐
  │ Write path:                                              │
  │   Client → Front-End → Metadata (3-AZ Paxos)            │
  │                      → Storage (3-AZ erasure coding)     │
  │                                                          │
  │ Read path:                                               │
  │   Client → Front-End → Metadata (witness check)          │
  │                      → Storage (read from any AZ)        │
  │                                                          │
  │ Latency: 10-50ms first byte                              │
  │ Durability: 11 9's across 3 AZs                          │
  │ Cost: $0.023/GB/month (us-east-1)                        │
  └──────────────────────────────────────────────────────────┘


S3 Express One Zone:
  ┌──────────────────────────────────────────────────────────┐
  │ Write path:                                              │
  │   Client → Front-End → Metadata (single-AZ, no cross-AZ │
  │                         replication latency)             │
  │                      → Storage (single-AZ, possibly on  │
  │                         SSD/NVMe instead of HDD)         │
  │                                                          │
  │ Read path:                                               │
  │   Client → Front-End → Metadata (local, fast)            │
  │                      → Storage (local, SSD-backed)       │
  │                                                          │
  │ Latency: 1-5ms first byte (consistent)                   │
  │ Durability: 11 9's within 1 AZ                           │
  │ Cost: ~$0.16/GB/month (higher, SSD-backed)              │
  └──────────────────────────────────────────────────────────┘
```

### Why It Is Faster

```
Standard S3 write:
  1. Receive data at front-end                 (~1ms)
  2. Write to storage node in AZ-a             (~3ms)
  3. Replicate to AZ-b (cross-AZ network)      (~5ms)
  4. Replicate to AZ-c (cross-AZ network)      (~5ms)
  5. Wait for quorum (2 of 3 AZs confirm)      (~5ms)
  6. Commit metadata (3-AZ Paxos)              (~5ms)
  7. Return 200 OK to client                   (~1ms)
  Total: ~25ms

Express One Zone write:
  1. Receive data at front-end                 (~0.5ms)
  2. Write to local SSD/NVMe storage           (~0.5ms)
  3. No cross-AZ replication needed            (0ms)
  4. Commit metadata (local only)              (~0.5ms)
  5. Return 200 OK to client                   (~0.5ms)
  Total: ~2ms

  The elimination of cross-AZ replication is the primary speedup.
  SSD/NVMe storage (vs HDD) is the secondary speedup.
```

### Use Cases

| Use Case                   | Why Express One Zone?                          |
|----------------------------|------------------------------------------------|
| ML training data staging   | Need to feed data to GPUs at high speed        |
| Real-time analytics        | Intermediate results need low-latency storage  |
| HPC scratch storage        | Computation nodes need fast I/O                |
| Gaming asset loading       | Low-latency asset retrieval for players        |
| Financial tick data        | Sub-millisecond matters for trading systems    |

### Tradeoffs

```
ADVANTAGES:
  + 10x lower latency than S3 Standard
  + Higher throughput per connection
  + Consistent performance (less variability)
  + S3 API compatible (easy migration)

DISADVANTAGES:
  - Single AZ: if the AZ goes down, data is temporarily unavailable
  - Higher cost: ~7x more per GB than Standard
  - Less durable against AZ-level disasters (still 11 9's within the AZ)
  - Directory buckets only (different bucket type, some API differences)
  - Not available in all regions (still rolling out)

RECOMMENDATION:
  Use Express One Zone as a performance tier, not as primary storage.
  Store the canonical copy in S3 Standard (3 AZ, durable).
  Copy hot data to Express One Zone when low latency is needed.
  Treat it like a managed, durable cache.
```

---

## 13. Performance Comparison Table

### S3 Storage Classes: Performance Comparison

| Metric             | S3 Standard    | S3 Express One Zone | S3 Standard-IA  | S3 Glacier Instant |
|--------------------|----------------|---------------------|------------------|---------------------|
| First-byte latency | 10-50ms        | 1-5ms               | 10-50ms          | ~100ms              |
| Throughput/conn    | ~100 MB/s      | ~1 GB/s             | ~100 MB/s        | ~100 MB/s           |
| Availability SLA   | 99.99%         | 99.95%              | 99.9%            | 99.9%               |
| AZ replication     | 3 AZ           | 1 AZ                | 3 AZ             | 3 AZ                |
| Durability         | 11 9's (3 AZ)  | 11 9's (1 AZ)       | 11 9's (3 AZ)    | 11 9's (3 AZ)       |
| Min storage dur.   | None           | None                | 30 days          | 90 days             |
| Retrieval cost     | None           | None                | Per GB           | Per GB              |
| Storage cost/GB    | $0.023         | ~$0.16              | $0.0125          | $0.004              |

### S3 vs. Other AWS Storage Services

| Metric                | S3 Standard      | S3 Express    | EBS gp3          | EFS               | FSx Lustre       |
|-----------------------|------------------|---------------|-------------------|--------------------| ------------------|
| Type                  | Object           | Object        | Block             | File (NFS)         | File (Lustre)    |
| First-byte latency    | 10-50ms          | 1-5ms         | <1ms              | 1-10ms             | <1ms             |
| Throughput/connection  | ~100 MB/s        | ~1 GB/s       | Up to 1 GB/s      | Varies (burst)     | Up to 1 TB/s     |
| Max object/file size   | 5 TB             | 5 TB          | N/A (block)       | 48 TB              | N/A              |
| Max volume/FS size     | Unlimited        | Unlimited     | 64 TB             | Unlimited          | PBs              |
| Concurrent access      | Unlimited        | Unlimited     | 1 EC2 (or Multi)  | Thousands of EC2   | Thousands of EC2 |
| Durability             | 11 9's (3 AZ)   | 11 9's (1 AZ) | 99.999% (1 AZ)   | 11 9's (3 AZ)     | Depends on config|
| Cost model             | GB + requests    | GB + requests | GB + IOPS         | GB (+ throughput)  | GB + throughput  |
| Best for               | General objects  | Low-lat obj   | Database storage  | Shared filesystems | HPC, ML training |
| API style              | REST (HTTP)      | REST (HTTP)   | Block device      | POSIX (NFS)        | POSIX (Lustre)   |

### Latency Breakdown by Operation

| Operation            | S3 Standard | S3 Express | Notes                        |
|----------------------|-------------|------------|------------------------------|
| PUT (small, <1KB)    | 15-25ms     | 2-4ms      | Dominated by metadata write  |
| PUT (1 MB)           | 20-40ms     | 3-6ms      | + data write time            |
| PUT (100 MB, single) | 100-300ms   | 20-50ms    | + network transfer time      |
| GET (small, <1KB)    | 10-20ms     | 1-3ms      | Metadata + small data read   |
| GET (1 MB)           | 15-30ms     | 2-5ms      | + data transfer time         |
| HEAD                 | 5-15ms      | 1-3ms      | Metadata only                |
| DELETE               | 10-20ms     | 2-4ms      | Metadata update (mark deleted)|
| LIST (1000 objects)  | 50-200ms    | 10-30ms    | Metadata scan                |

---

## 14. Capacity Planning at S3 Scale

Running a system as large as S3 requires planning years into the future. This section
covers the physical and operational challenges.

### Hardware Procurement

```
Planning Timeline:
══════════════════

  T-18 months: Demand forecasting
    - Analyze growth trends per region, per storage class
    - Model: "At current growth rate, us-east-1 will need X PB by Q3 2025"
    - Factor in: new customer onboarding, seasonal patterns, major events

  T-12 months: Hardware ordering
    - Order hard drives (HDDs for bulk, SSDs for Express)
    - Order servers, networking equipment, racks
    - Lead times: 3-6 months for HDDs, 6-12 months for custom hardware

  T-6 months: Data center preparation
    - Ensure rack space is available
    - Ensure power capacity (each rack: ~10-20 kW)
    - Ensure cooling capacity
    - Ensure network connectivity

  T-3 months: Deployment and burn-in
    - Rack servers
    - Install disks
    - Run burn-in tests (detect early failures)
    - Join to S3 cluster
    - Begin accepting data

  T-0: Capacity available for customers
```

### Disk Failure Management

```
Scale of the Problem:
════════════════════

  Assume S3 has ~5 million hard drives globally (conservative estimate).
  Industry average HDD Annual Failure Rate (AFR): 1.5-2.5%

  At 2% AFR across 5 million drives:
    Annual failures:   5,000,000 * 0.02 = 100,000 drives/year
    Monthly failures:  100,000 / 12     = ~8,333 drives/month
    Daily failures:    100,000 / 365    = ~274 drives/day
    Hourly failures:   274 / 24         = ~11 drives/hour

  That is roughly 1 drive failure every 5 minutes, globally.

  Each failed drive must be:
    1. Detected (automated monitoring)
    2. Isolated (stop reading/writing to it)
    3. Repaired (erasure-coded data reconstructed from surviving chunks)
    4. Physically replaced (by a human, in a data center)
    5. Burned in (new drive tested before accepting data)

  Replacement budget:
    100,000 drives/year * ~$100/drive = $10M/year just in replacement drives
    Plus: human labor for physical replacement, logistics, recycling
```

### Network Capacity Planning

```
Network Budget:
══════════════

  Sources of internal network traffic in S3:

  1. Client ingress (writes):
     Customer uploads data → front-end → storage nodes
     Volume: hundreds of GB/s per region (aggregate)

  2. Cross-AZ replication:
     Every byte written to S3 Standard must be replicated to 2 other AZs
     If ingress = 100 GB/s, cross-AZ replication traffic = 200 GB/s

  3. Erasure coding repair:
     When a chunk is lost (disk failure), it must be reconstructed
     and re-replicated to a new node.
     With ~11 drive failures/hour, each needing ~100 GB of repair traffic:
       Repair traffic = 11 * 100 GB = ~1.1 TB/hour = ~300 MB/s sustained
       (This is manageable, but spikes during correlated failures)

  4. Background integrity verification:
     S3 continuously reads and verifies all stored data
     This generates significant read I/O but is spread across all nodes

  5. Lifecycle transitions:
     Moving objects between storage classes generates internal traffic
     (e.g., Standard → Glacier = read + write + delete)

  Total internal bandwidth requirement per region:
    Easily multiple TB/s of aggregate network capacity
    Each pair of AZs needs hundreds of Gbps of bandwidth
```

### Power and Cooling

```
Rough calculation for a single S3 region:

  Assume 50,000 servers in a large region (front-end + metadata + storage)
  Average power per server: ~500W (lower for storage, higher for compute)
  Total server power: 50,000 * 500W = 25 MW

  Networking equipment: ~15% overhead → 3.75 MW
  Cooling (PUE ~1.2): 25 MW * 0.2 = 5 MW overhead
  Miscellaneous (lighting, security, etc.): ~1 MW

  Total power for S3 in one large region: ~35 MW
  That is roughly equivalent to powering 25,000 homes

  Cooling: 25 MW of heat must be removed from the data center
    - Chilled water systems, economizers, hot/cold aisle containment
    - In cold climates: free cooling (use outside air)
    - In hot climates: significant HVAC investment
```

### Growth Modeling

```
Capacity Planning Model (simplified):
═════════════════════════════════════

  Current state (Jan 2024):
    Total storage in us-east-1: 500 PB
    Monthly growth rate: 3%
    Current utilization: 75%

  Projection:
    Month  Storage (PB)  Utilization  Action Needed
    ─────  ────────────  ───────────  ─────────────
    Jan    500           75%          -
    Feb    515           77%          -
    Mar    530           80%          Start planning expansion
    Apr    546           82%          -
    May    563           85%          Order hardware
    Jun    580           87%          -
    Jul    597           90%          Begin deployment
    Aug    615           92%          Deploy urgently
    Sep    633           95%          CRITICAL - capacity crunch
    Oct    652           98%          NEW CAPACITY ONLINE (expand to 800 PB)
    Nov    672           84%          Breathing room
    Dec    692           86%          Next planning cycle begins

  Key insight: with 3% monthly growth, storage doubles in ~24 months.
  You need to be ordering hardware 6-12 months BEFORE you need it.
  Undershoot = capacity crisis → customer impact.
  Overshoot = wasted capital → CFO is unhappy.
```

---

## 15. Cross-References

This document is part of a series covering Amazon S3's system design:

| Document | Description |
|---|---|
| [Interview Simulation](interview-simulation.md) | Full mock interview walkthrough |
| [Metadata & Indexing](metadata-and-indexing.md) | How S3 indexes 350+ trillion objects |
| [Data Storage & Durability](data-storage-and-durability.md) | Erasure coding, checksums, 11 nines |
| [Consistency & Replication](consistency-and-replication.md) | Strong consistency, Paxos, witness |
| [Storage Classes & Lifecycle](storage-classes-and-lifecycle.md) | Tiered storage, lifecycle policies |
| [System Flows](flow.md) | PUT, GET, DELETE, LIST step-by-step |
| [Security & Access Control](security-and-access-control.md) | IAM, bucket policies, encryption |
| [API Contracts](api-contracts.md) | REST API design, headers, status codes |

---

*This document focuses on scaling and performance. For durability, consistency, and*
*security topics, see the companion documents listed above.*
