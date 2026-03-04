# Amazon S3 — Data Storage & Durability Deep Dive

> Companion deep dive to the [interview simulation](interview-simulation.md). This document explores how S3 physically stores object data and achieves 99.999999999% (11 nines) durability using erasure coding.

---

## Table of Contents

1.  [The Durability Challenge](#1-the-durability-challenge)
2.  [Storage Hardware Hierarchy](#2-storage-hardware-hierarchy)
3.  [Naive Approach: Simple Replication](#3-naive-approach-simple-replication)
4.  [Erasure Coding Primer](#4-erasure-coding-primer)
5.  [S3's Erasure Coding Scheme](#5-s3s-erasure-coding-scheme)
6.  [Durability Mathematics (Detailed)](#6-durability-mathematics-detailed)
7.  [Comparison: Replication vs Erasure Coding](#7-comparison-replication-vs-erasure-coding)
8.  [Object Chunking Strategy](#8-object-chunking-strategy)
9.  [Storage Node Architecture](#9-storage-node-architecture)
10. [Data Integrity — Defense in Depth](#10-data-integrity--defense-in-depth)
11. [Background Scrubbing Deep Dive](#11-background-scrubbing-deep-dive)
12. [Repair and Reconstruction](#12-repair-and-reconstruction)
13. [Data Placement Service](#13-data-placement-service)
14. [Disk Failure Handling](#14-disk-failure-handling)
15. [AZ Failure Handling](#15-az-failure-handling)

---

## 1. The Durability Challenge

### 1.1 What Does 11 Nines Actually Mean?

Durability of 99.999999999% means the probability of losing a single object in a
given year is 0.000000001% — that is, 1 in 10^11.

Put in concrete terms:

```
If you store 10,000,000 objects (10 million):
  Expected object losses per year = 10^7 / 10^11 = 10^-4 = 0.0001
  That is: you lose 1 object every 10,000 years.

If you store 10,000,000,000 objects (10 billion):
  Expected object losses per year = 10^10 / 10^11 = 0.1
  That is: you lose 1 object every 10 years.
```

At S3's actual scale (over 100 trillion objects as of 2023):

```
Expected object losses per year = 10^14 / 10^11 = 1,000
```

Even at 11 nines, at 100 trillion objects you would statistically expect ~1,000
object losses per year. This is why S3's actual engineering target is almost
certainly *higher* than 11 nines — they advertise 11 nines as a conservative
lower bound.

### 1.2 The Scale of the Problem

S3 stores over 100 trillion objects across millions of physical hard drives.
These drives are spinning 24/7, experiencing vibrations, thermal cycles, and
electromagnetic interference. The sheer fleet size means failures are not
exceptional events — they are continuous, routine, and expected.

```
Quick math on disk failures alone:
  Assume S3 operates ~10 million HDDs.
  Annual Failure Rate (AFR) for enterprise HDDs: ~2%
  Expected disk failures per year: 10,000,000 x 0.02 = 200,000
  Expected disk failures per day:  200,000 / 365 = ~548 disks/day
  Expected disk failures per hour: 548 / 24 = ~23 disks/hour
```

A disk fails roughly every 2-3 minutes. Durability engineering must treat disk
failure as a continuous, normal operational condition — not an emergency.

### 1.3 Types of Failures

Failures come in many forms and at many scales:

```
Failure Type          | Frequency          | Scope            | Detection
──────────────────────┼────────────────────┼──────────────────┼──────────────────
Sector-level bit rot  | Very frequent      | 1 chunk partial  | Checksum mismatch
Single disk failure   | ~23/hour at scale  | All chunks on    | SMART data,
                      |                    | that disk         | I/O errors
Storage node failure  | Multiple/day       | 10-50 disks      | Heartbeat timeout
(server crash)        |                    |                  |
Rack failure          | Weekly/monthly     | 10-40 nodes      | Network/power
(power/ToR switch)    |                    |                  | monitoring
AZ failure            | Rare (yearly)      | Thousands of     | Cross-AZ health
                      |                    | racks            | checks
Region failure        | Extremely rare     | All AZs          | Global monitoring
Software bug          | Unpredictable      | Potentially all  | Testing, canary
                      |                    | objects           | deployments
Operational error     | Unpredictable      | Varies           | Audit, automation
```

### 1.4 Correlated vs Independent Failures

This distinction is critical for durability modeling:

**Independent failures**: A disk failure on Node A says nothing about whether
Node B's disks will fail. These are modeled well by simple probability
multiplication.

**Correlated failures**: When a rack loses power, *all* disks in that rack fail
simultaneously. When an AZ floods, *all* racks in that AZ fail. A firmware bug
in a batch of drives from the same manufacturing lot may cause many drives to
fail within the same time window.

```
Correlated Failure Examples:
  ┌─────────────────────────────────────────────────────┐
  │ Power failure to Rack 7                             │
  │   → All 40 nodes in Rack 7 go offline               │
  │   → All ~2,000 disks on those nodes become           │
  │     unreachable simultaneously                       │
  │   → If 2+ chunks of the same object are on Rack 7,  │
  │     we lost 2+ chunks in one event, not independently│
  └─────────────────────────────────────────────────────┘

  ┌─────────────────────────────────────────────────────┐
  │ Drive firmware bug (Seagate lot #X)                  │
  │   → 50,000 drives from this lot deployed across      │
  │     fleet                                            │
  │   → Bug triggers under specific workload pattern     │
  │   → 5,000 drives fail within a 48-hour window        │
  │   → Massive correlated failure event                 │
  └─────────────────────────────────────────────────────┘
```

Correlated failures are the primary reason S3 advertises 11 nines rather than
the 16 nines that the raw math of erasure coding produces. The gap accounts for
real-world correlation, software bugs, and operational risk.

---

## 2. Storage Hardware Hierarchy

### 2.1 Physical Topology

S3's infrastructure is organized in a strict physical hierarchy. Each level
represents both a unit of capacity and a **failure domain**.

```
Region (e.g., us-east-1)
│
├── Availability Zone A (us-east-1a)
│   │
│   ├── Data Center 1
│   │   │
│   │   ├── Rack 1
│   │   │   ├── Storage Node 1  [48 x 16TB HDD]  = 768 TB raw
│   │   │   ├── Storage Node 2  [48 x 16TB HDD]  = 768 TB raw
│   │   │   ├── ...
│   │   │   └── Storage Node 40 [48 x 16TB HDD]  = 768 TB raw
│   │   │                         Rack total: ~30 PB raw
│   │   │
│   │   ├── Rack 2
│   │   │   └── ... (40 nodes)
│   │   │
│   │   └── Rack N
│   │       └── ... (40 nodes)
│   │
│   ├── Data Center 2
│   │   └── ...
│   │
│   └── Data Center M
│       └── ...
│
├── Availability Zone B (us-east-1b)
│   └── ... (independent power, cooling, networking)
│
└── Availability Zone C (us-east-1c)
    └── ... (independent power, cooling, networking)
```

### 2.2 Failure Domain Hierarchy

Each level has a distinct failure probability and blast radius:

```
Failure Domain      │ Blast Radius        │ Typical Cause
════════════════════╪═════════════════════╪═══════════════════════════════
Disk                │ ~16 TB              │ Mechanical wear, firmware bug
                    │                     │ head crash, bad sectors
────────────────────┼─────────────────────┼───────────────────────────────
Node (server)       │ ~768 TB (48 disks)  │ CPU/RAM failure, OS crash,
                    │                     │ NIC failure, motherboard
────────────────────┼─────────────────────┼───────────────────────────────
Rack                │ ~30 PB (40 nodes)   │ Top-of-rack switch failure,
                    │                     │ PDU failure, circuit breaker
────────────────────┼─────────────────────┼───────────────────────────────
Availability Zone   │ Petabytes-Exabytes  │ Natural disaster, utility
                    │                     │ power loss, cooling failure
────────────────────┼─────────────────────┼───────────────────────────────
Region              │ Everything          │ Catastrophic (extremely rare)
```

### 2.3 Why the Hierarchy Matters for Placement

The chunk placement algorithm must respect this hierarchy. The core rule:

> **No two chunks of the same object should share a failure domain below the
> level the system is designed to tolerate.**

In practice:
- No two chunks on the same disk (obviously)
- No two chunks on the same node
- No two chunks on the same rack (best effort)
- Chunks spread across at least 3 AZs (hard requirement)

### 2.4 Power and Network Topology

Physical infrastructure creates hidden correlations:

```
AZ-a Data Center Floor Plan (simplified):

  ┌──────────────────────────────────────────────────┐
  │                                                  │
  │   Power Feed A              Power Feed B          │
  │   ┌─────────┐              ┌─────────┐           │
  │   │ PDU A-1 │              │ PDU B-1 │           │
  │   └────┬────┘              └────┬────┘           │
  │        │                        │                │
  │   ┌────┴────┐  ┌─────────┐  ┌──┴──────┐         │
  │   │ Rack 1  │  │ Rack 2  │  │ Rack 3  │ ...     │
  │   │(Feed A) │  │(Feed A) │  │(Feed B) │         │
  │   └─────────┘  └─────────┘  └─────────┘         │
  │                                                  │
  │   If Power Feed A fails:                         │
  │     Racks 1 and 2 go down together               │
  │     → Correlated failure across those racks       │
  └──────────────────────────────────────────────────┘
```

A smart placement service accounts for shared power circuits and network
switches to minimize correlated risk, even within a single AZ.

---

## 3. Naive Approach: Simple Replication

Before understanding why erasure coding is necessary, it is important to see why
the simpler approach — replication — falls short.

### 3.1 How 3x Replication Works

```
Original object: "photo.jpg" (8 MB)

Write path:
  1. Client uploads 8 MB to S3
  2. S3 writes Copy 1 to Node A in AZ-a   (8 MB)
  3. S3 writes Copy 2 to Node B in AZ-b   (8 MB)
  4. S3 writes Copy 3 to Node C in AZ-c   (8 MB)

  Total stored: 24 MB for an 8 MB object = 3.0x overhead

Read path:
  1. Client requests "photo.jpg"
  2. S3 reads from the nearest/fastest copy
  3. Returns 8 MB to client
```

This is simple, fast, and easy to reason about. Every copy is a complete,
independent replica of the object. Reading requires no computation — just return
any one copy.

### 3.2 Durability Calculation for 3x Replication

**Scenario: No repair (static analysis)**

If we assume 3 independent copies with no repair mechanism:

```
P(single copy lost in 1 year) = AFR = 0.02

P(all 3 copies lost) = P(copy1 lost) x P(copy2 lost) x P(copy3 lost)
                      = 0.02 x 0.02 x 0.02
                      = 8 x 10^-6

Durability = 1 - 8 x 10^-6
           = 99.9992%
           = ~5 nines
```

Five nines is completely inadequate for S3. At 100 trillion objects, you would
lose 800 billion objects per year.

**Scenario: With repair (MTTR = 6 hours)**

In reality, when a copy is lost, S3 detects it and creates a new replica from
a surviving copy. Data loss only occurs if *all* copies are lost before repair
completes.

```
Step 1: First copy fails
  P(first copy fails in a year) = 0.02
  We now have 2 surviving copies.

Step 2: We race to repair. Repair takes MTTR = 6 hours.
  During this 6-hour window, a second copy must also fail for danger.

  P(second copy fails during 6-hour window):
    = AFR x (MTTR / hours_per_year)
    = 0.02 x (6 / 8760)
    = 0.02 x 6.849 x 10^-4
    = 1.37 x 10^-5

Step 3: Now 1 copy remains, another 6-hour repair window.
  P(third copy fails during 6-hour window) = 1.37 x 10^-5

Combined probability of data loss (all 3 copies lost with repair):
  P(loss) = P(1st fail) x P(2nd fail during repair) x P(3rd fail during repair)

  But we must account for there being 3 copies that could fail first,
  then 2 remaining that could fail second, etc.

  P(loss) = 3 x 0.02 x [2 x 1.37 x 10^-5] x [1 x 1.37 x 10^-5]
          = 3 x 0.02 x 2.74 x 10^-5 x 1.37 x 10^-5
          = 3 x 0.02 x 3.75 x 10^-10
          = 2.25 x 10^-11
          ≈ 10^-10.6

  Durability ≈ 10-11 nines (under ideal independent-failure assumptions)
```

This looks encouraging on paper, but it **only** holds under the assumption of
perfectly independent failures. In reality, correlated failures (rack power,
firmware bugs, AZ events) erode this significantly to approximately **8-9 nines**
in practice.

### 3.3 The Cost Problem

Even if replication could be made durable enough, the cost is prohibitive:

```
Storage overhead with 3x replication:

  For every 1 byte stored by the customer, S3 stores 3 bytes.
  Overhead: 3.0x

  At exabyte scale:
    Customer data:   1 EB (exabyte)
    Total stored:    3 EB
    Extra storage:   2 EB

  Cost of 2 EB extra storage (rough estimate):
    Enterprise HDD: ~$20 per TB
    2 EB = 2,000,000 TB
    Hardware cost alone: 2,000,000 x $20 = $40,000,000 ($40M)
    Plus: power, cooling, rack space, networking, ops

  Total overhead cost: easily $100M+ per exabyte of customer data
```

S3 stores many exabytes. Every fraction of storage overhead saved translates
to hundreds of millions of dollars.

### 3.4 Why Replication Is Not Enough for S3

| Metric                | 3x Replication | S3's Requirement     |
|----------------------|----------------|----------------------|
| Durability (ideal)   | ~10-11 nines   | 11 nines (guaranteed)|
| Durability (real)    | ~8-9 nines     | 11 nines             |
| Storage overhead     | 3.0x           | Target < 2.0x        |
| Cost for 1 EB stored | ~$100M+ extra  | Must be far lower    |
| Read latency         | Low (1 copy)   | Acceptable           |
| Write latency        | Low (3 copies) | Acceptable           |
| Correlated failure   | Vulnerable     | Must be resilient    |
| tolerance            |                |                      |

The conclusion is clear: S3 needs a scheme that delivers **higher durability**
at **lower storage cost**. That scheme is erasure coding.

---

## 4. Erasure Coding Primer

### 4.1 The Basic Concept

Erasure coding is a mathematical technique that adds **computed redundancy** to
data, allowing the original data to be reconstructed even when some pieces are
lost.

The core idea:
1. Split the original data into **k** equal-sized data chunks
2. Compute **m** additional parity chunks using the data chunks
3. Store all **(k + m)** chunks on different storage locations
4. Any **k** of the **(k + m)** chunks are sufficient to reconstruct the
   original data
5. The system can tolerate up to **m** simultaneous chunk losses

```
The fundamental trade-off:

  Replication: Store COMPLETE COPIES. Simple but expensive.
  Erasure coding: Store MATHEMATICAL FRAGMENTS. Complex but efficient.

  ┌───────────────────────────────────────────────────┐
  │ Replication (3 copies):                           │
  │   [AAAA] [AAAA] [AAAA]                           │
  │   3 x original size = 3.0x overhead               │
  │   Can lose 2 copies                               │
  │                                                   │
  │ Erasure coding (4 data + 2 parity):               │
  │   [A1] [A2] [A3] [A4] [P1] [P2]                  │
  │   6/4 x original size = 1.5x overhead             │
  │   Can lose 2 chunks                               │
  │                                                   │
  │ Same fault tolerance, HALF the storage cost!       │
  └───────────────────────────────────────────────────┘
```

### 4.2 Reed-Solomon Codes

S3 almost certainly uses Reed-Solomon codes, the most widely deployed family of
erasure codes. Here is what you need to know (and what you can skip in an
interview):

**Need to know (properties):**

- **MDS (Maximum Distance Separable)**: Reed-Solomon codes are *optimal* — for
  a given amount of redundancy, they provide the maximum possible fault
  tolerance. You cannot do better with any other code for the same overhead.

- **Systematic**: The first k chunks of the encoded output ARE the original
  data, unchanged. The m parity chunks are appended. This means:
  - If all k data chunks are available, NO decoding is needed — just
    concatenate the data chunks.
  - Decoding (an expensive computation) is only required when data chunks are
    missing and you must use parity chunks to reconstruct them.

- **Deterministic**: The same input data always produces the same parity chunks.
  No randomness, no state — purely mathematical.

- **Configurable**: You choose k and m based on your durability and cost targets.

**Good to know (math intuition, not required in interview):**

- Operates over Galois Fields, typically GF(2^8) — arithmetic on bytes where
  addition is XOR and multiplication uses lookup tables.
- The data chunks are treated as coefficients of a polynomial of degree k-1.
- Encoding evaluates this polynomial at (k + m) distinct points.
- Decoding uses any k of those points to reconstruct the polynomial via
  Lagrange interpolation.
- The maximum number of chunks is limited by the field size: for GF(2^8),
  you can have at most 255 total chunks (k + m <= 255).

### 4.3 Visual Explanation of Erasure Coding

```
Step 1: Start with original data (8 MB file)
  ┌────────────────────────────────────────────────────────────────┐
  │                      Original Data (8 MB)                      │
  └────────────────────────────────────────────────────────────────┘

Step 2: Split into k = 8 data chunks (1 MB each)
  ┌──────┬──────┬──────┬──────┬──────┬──────┬──────┬──────┐
  │  D1  │  D2  │  D3  │  D4  │  D5  │  D6  │  D7  │  D8  │
  │ 1 MB │ 1 MB │ 1 MB │ 1 MB │ 1 MB │ 1 MB │ 1 MB │ 1 MB │
  └──────┴──────┴──────┴──────┴──────┴──────┴──────┴──────┘

Step 3: Compute m = 3 parity chunks using Reed-Solomon encoding
  ┌──────┬──────┬──────┐
  │  P1  │  P2  │  P3  │    ← computed from D1..D8
  │ 1 MB │ 1 MB │ 1 MB │
  └──────┴──────┴──────┘

Step 4: Store all 11 chunks (8 data + 3 parity) on different nodes
  ┌──────┬──────┬──────┬──────┬──────┬──────┬──────┬──────┬──────┬──────┬──────┐
  │  D1  │  D2  │  D3  │  D4  │  D5  │  D6  │  D7  │  D8  │  P1  │  P2  │  P3  │
  │Node1 │Node2 │Node3 │Node4 │Node5 │Node6 │Node7 │Node8 │Node9 │Nd10  │Nd11  │
  └──────┴──────┴──────┴──────┴──────┴──────┴──────┴──────┴──────┴──────┴──────┘

  Total stored: 11 MB for 8 MB of data = 1.375x overhead
  Can lose ANY 3 of the 11 chunks and still reconstruct the 8 MB file
```

### 4.4 Reading with Erasure Coding

```
Scenario A: All data chunks available (common case, ~99.99% of reads)
  ┌──────┬──────┬──────┬──────┬──────┬──────┬──────┬──────┐
  │  D1  │  D2  │  D3  │  D4  │  D5  │  D6  │  D7  │  D8  │  → Concatenate
  └──────┴──────┴──────┴──────┴──────┴──────┴──────┴──────┘    → Return 8 MB
  No decoding needed! (Systematic code advantage)

Scenario B: D3 is lost (disk failed), use parity
  ┌──────┬──────┬──────┬──────┬──────┬──────┬──────┬──────┐
  │  D1  │  D2  │  ██  │  D4  │  D5  │  D6  │  D7  │  D8  │  D3 missing!
  └──────┴──────┴──────┴──────┴──────┴──────┴──────┴──────┘
                   ↓
  Use D1,D2,D4,D5,D6,D7,D8 + P1 (any 8 of 11 chunks)
                   ↓
  Reed-Solomon decode → reconstruct D3
                   ↓
  Concatenate D1..D8 → Return 8 MB

Scenario C: D3, D7, and P2 are all lost (3 failures!)
  ┌──────┬──────┬──────┬──────┬──────┬──────┬──────┬──────┬──────┬──────┬──────┐
  │  D1  │  D2  │  ██  │  D4  │  D5  │  D6  │  ██  │  D8  │  P1  │  ██  │  P3  │
  └──────┴──────┴──────┴──────┴──────┴──────┴──────┴──────┴──────┴──────┴──────┘
  Still have 8 chunks: D1,D2,D4,D5,D6,D8,P1,P3
  Reed-Solomon decode → reconstruct D3 and D7
  Concatenate D1..D8 → Return 8 MB ✓

Scenario D: 4 or more chunks lost → DATA LOSS
  Only 7 chunks remain. Need 8 to reconstruct. Object is unrecoverable.
```

### 4.5 Encoding and Decoding Performance

Erasure coding is not free — it has computational cost:

```
Encoding (write path):
  Input:  k data chunks
  Output: m parity chunks
  Cost:   O(k x m x chunk_size) — matrix multiplication

  For k=8, m=3, chunk_size=1MB:
    ~24 MB of XOR/multiply operations
    Modern CPUs with SIMD (AVX2/AVX-512): ~2-4 GB/s encoding throughput
    Time to encode 8 MB object: < 1 ms
    Negligible compared to network and disk I/O

Decoding (read path — only when chunks missing):
  Input:  any k of (k+m) chunks
  Output: reconstructed k data chunks
  Cost:   O(k^2 x chunk_size) — matrix inversion + multiplication

  Slightly more expensive than encoding due to matrix inversion,
  but still < 1 ms for typical S3 object sizes.
  Again negligible vs I/O.
```

---

## 5. S3's Erasure Coding Scheme

### 5.1 Likely Configuration: 8 Data + 3 Parity (8,3)

While AWS has not published the exact parameters, strong evidence (AWS re:Invent
talks, the 11 nines guarantee, and storage overhead analysis) points to an
**(8, 3)** configuration — or something very close to it.

```
Parameters:
  k = 8   (data chunks)
  m = 3   (parity chunks)
  n = 11  (total chunks = k + m)

Key properties:
  Storage overhead:    n / k = 11 / 8 = 1.375x
  Fault tolerance:     m = 3 simultaneous chunk losses
  Min chunks to read:  k = 8
  Chunk size:          object_size / k
```

### 5.2 Chunk Placement Strategy

This is where the hardware hierarchy from Section 2 becomes critical. Chunks
must be spread to minimize the impact of correlated failures.

```
11 chunks distributed across 3 Availability Zones:

  ┌─────────────────────────────────────────────────────────────────┐
  │                         REGION: us-east-1                       │
  │                                                                 │
  │  AZ-a (4 chunks)          AZ-b (4 chunks)     AZ-c (3 chunks)  │
  │  ┌─────────────────┐     ┌─────────────────┐  ┌──────────────┐ │
  │  │ Rack1           │     │ Rack5           │  │ Rack9        │ │
  │  │  └─ Node1 [D1]  │     │  └─ Node5 [D2]  │  │  └─ Node9 [D3]│ │
  │  │ Rack2           │     │ Rack6           │  │ Rack10       │ │
  │  │  └─ Node2 [D4]  │     │  └─ Node6 [D5]  │  │  └─Nd10 [D6] │ │
  │  │ Rack3           │     │ Rack7           │  │ Rack11       │ │
  │  │  └─ Node3 [D7]  │     │  └─ Node7 [D8]  │  │  └─Nd11 [P1] │ │
  │  │ Rack4           │     │ Rack8           │  │              │ │
  │  │  └─ Node4 [P2]  │     │  └─ Node8 [P3]  │  │              │ │
  │  └─────────────────┘     └─────────────────┘  └──────────────┘ │
  └─────────────────────────────────────────────────────────────────┘
```

### 5.3 Placement Rules (Strict)

1. **No two chunks on the same physical disk** — guarantees that a single disk
   failure can only destroy one chunk.

2. **No two chunks on the same storage node** — guarantees that a single server
   failure (CPU, RAM, motherboard, NIC) can only destroy one chunk.

3. **No two chunks on the same rack** (best effort) — protects against
   top-of-rack switch failure or PDU failure. With 11 chunks and potentially
   fewer than 11 available racks in an AZ, this may occasionally be relaxed.

4. **Spread across at least 3 AZs** — this is a hard requirement. With 11
   chunks across 3 AZs, the distribution is roughly 4-4-3 or 4-3-4.

5. **Balance chunk count per AZ** — no AZ should hold more than m chunks if
   possible, ensuring that losing an entire AZ still leaves k chunks available.

### 5.4 Why 4-4-3 Distribution Across AZs

```
Distribution: 4 chunks in AZ-a, 4 chunks in AZ-b, 3 chunks in AZ-c

Scenario: AZ-a goes completely offline
  Chunks lost: 4 (in AZ-a)
  Chunks remaining: 4 (AZ-b) + 3 (AZ-c) = 7

  k = 8 data chunks needed to reconstruct.
  We only have 7 chunks. THIS IS DATA LOSS.

Wait — is S3 not AZ-failure tolerant?
```

This is a subtle point. With a 4-4-3 distribution and k=8, losing the AZ with
4 chunks leaves only 7 — one short of what is needed. However, AWS states that
S3 is designed to sustain the loss of an entire AZ.

This implies one or more of:
- The actual scheme uses slightly different parameters (e.g., 6+3 or 8+4)
- The distribution is carefully constrained to be no more than 3 per AZ
  (3-4-4 with strict limits)
- Additional mechanisms (lazy repair, pre-staging) provide extra protection

A **3-4-4 distribution with the rule that no AZ holds more than (n - k) = 3
chunks** would ensure that losing any one AZ removes at most 3 chunks, leaving
at least 8 — exactly k. More realistically, S3 may use **(8, 4)** with 12 total
chunks distributed 4-4-4, which cleanly survives any single AZ loss.

```
Alternative: 8 data + 4 parity = 12 chunks, distributed 4-4-4

  AZ-a: 4 chunks   AZ-b: 4 chunks   AZ-c: 4 chunks

  AZ-a goes offline → 8 chunks remain → exactly k → can reconstruct ✓
  Storage overhead: 12/8 = 1.5x (still far less than 3.0x replication)
```

For the remainder of this document, we continue using the (8,3) model for
calculations, noting that the actual scheme may be (8,4) or similar.

---

## 6. Durability Mathematics (Detailed)

### 6.1 Variables and Assumptions

```
Variable │ Value              │ Meaning
═════════╪════════════════════╪══════════════════════════════════
n        │ 11                 │ Total chunks per object
k        │ 8                  │ Data chunks (minimum to reconstruct)
m        │ 3                  │ Parity chunks (max tolerable losses)
AFR      │ 0.02               │ Annual failure rate per disk
MTTR     │ 6 hours            │ Mean time to repair (rebuild a chunk)
H/year   │ 8,760              │ Hours per year
═════════╪════════════════════╪══════════════════════════════════
```

### 6.2 Step-by-Step: Per-Chunk Failure Probability During Repair

When one chunk is lost, the system begins repair. The question is: how likely
is it that *additional* chunks fail during the repair window?

```
Per-chunk failure probability during a 6-hour repair window:

  p = AFR x (MTTR / H_per_year)
  p = 0.02 x (6 / 8760)
  p = 0.02 x 6.849 x 10^-4
  p = 1.370 x 10^-5

This is the probability that any single chunk fails during a 6-hour window.
```

### 6.3 Step-by-Step: Binomial Model for Multiple Failures

Data loss occurs when 4 or more chunks (out of 11) fail before repair completes.
We model this using the binomial distribution.

```
P(exactly j chunks fail out of n) = C(n, j) x p^j x (1 - p)^(n-j)

Where:
  C(n, j) = n! / (j! x (n-j)!)  — binomial coefficient
  p = 1.370 x 10^-5             — per-chunk failure prob in repair window
  n = 11                         — total chunks
```

### 6.4 Computing P(data loss) = P(4 or more failures)

**P(exactly 4 failures):**
```
C(11, 4) = 11! / (4! x 7!) = 330

p^4 = (1.370 x 10^-5)^4
    = (1.370)^4 x 10^-20
    = 3.527 x 10^-20

(1 - p)^7 ≈ 1 - 7p ≈ 1 - 9.59 x 10^-5 ≈ 0.99990

P(4) = 330 x 3.527 x 10^-20 x 0.99990
     = 330 x 3.527 x 10^-20
     = 1.164 x 10^-17
```

**P(exactly 5 failures):**
```
C(11, 5) = 462

p^5 = (1.370 x 10^-5)^5
    = 1.370^5 x 10^-25
    = 4.832 x 10^-25

P(5) = 462 x 4.832 x 10^-25 x (1-p)^6
     = 462 x 4.832 x 10^-25
     = 2.232 x 10^-22
```

**P(exactly 6 failures):**
```
C(11, 6) = 462

p^6 = (1.370 x 10^-5)^6
    = 6.620 x 10^-30

P(6) = 462 x 6.620 x 10^-30
     = 3.058 x 10^-27
```

**P(7+) is negligible (< 10^-31)**

**Total P(data loss):**
```
P(loss) = P(4) + P(5) + P(6) + ...
        ≈ 1.164 x 10^-17  +  2.232 x 10^-22  +  3.058 x 10^-27  + ...
        ≈ 1.164 x 10^-17

Durability = 1 - P(loss)
           = 1 - 1.164 x 10^-17
           ≈ 99.9999999999999999%
           ≈ 17 nines
```

### 6.5 Why S3 Advertises 11 Nines, Not 17

The 17 nines result assumes **perfectly independent failures**. Real-world
factors erode this significantly:

```
Factor                     │ Impact on Durability
═══════════════════════════╪═══════════════════════════════════════
Independent disk failures  │ 17 nines (our calculation)
+ Correlated rack failures │ ~14-15 nines
+ Correlated AZ failures   │ ~13-14 nines
+ Drive firmware bugs       │ ~12-13 nines
+ Software bugs             │ ~12 nines
+ Operational errors        │ ~11-12 nines
+ Safety margin             │ 11 nines (advertised)
═══════════════════════════╪═══════════════════════════════════════
```

The gap between 17 nines (theoretical) and 11 nines (advertised) is the
**engineering margin** that absorbs real-world messiness.

### 6.6 Markov Chain Model (More Accurate)

A more rigorous analysis models the object as a Markov chain with states
representing the number of healthy chunks:

```
State diagram (chunks healthy → transitions):

  [11] ──(fail)──→ [10] ──(fail)──→ [9] ──(fail)──→ [8] ──(fail)──→ [LOST]
   ↑                 ↑                ↑                ↑
   └──(repair)───────┘──(repair)──────┘──(repair)──────┘

State │ Meaning          │ Transition Rates
══════╪══════════════════╪═══════════════════════════════════
  11  │ All chunks OK    │ Fail: 11 x λ_fail  → state 10
  10  │ 1 chunk lost     │ Fail: 10 x λ_fail  → state 9
      │                  │ Repair: μ_repair    → state 11
   9  │ 2 chunks lost    │ Fail: 9 x λ_fail   → state 8
      │                  │ Repair: μ_repair    → state 10
   8  │ 3 chunks lost    │ Fail: 8 x λ_fail   → LOST (absorbing)
      │                  │ Repair: μ_repair    → state 9
 LOST │ 4+ chunks lost   │ Absorbing state (unrecoverable)

Where:
  λ_fail  = AFR / H_per_year = 0.02 / 8760 = 2.28 x 10^-6 per hour per chunk
  μ_repair = 1 / MTTR = 1 / 6 = 0.167 per hour
```

Solving the steady-state equations for this Markov chain yields a data loss rate
consistent with our binomial estimate, confirming the ~16-17 nines result for
independent failures.

### 6.7 Durability Sensitivity Analysis

How durability changes with different parameters:

```
Configuration    │ Overhead │ Tolerate │ Durability*  │ Notes
═════════════════╪══════════╪══════════╪══════════════╪═══════════════
3x replication   │ 3.000x   │ 2 losses │ ~10 nines    │ Simple, costly
6 + 2 (RS)       │ 1.333x   │ 2 losses │ ~12 nines    │ Least overhead
6 + 3 (RS)       │ 1.500x   │ 3 losses │ ~14 nines    │ Good balance
8 + 3 (RS)       │ 1.375x   │ 3 losses │ ~17 nines    │ Likely S3 config
8 + 4 (RS)       │ 1.500x   │ 4 losses │ ~22 nines    │ AZ-loss tolerant
10 + 4 (RS)      │ 1.400x   │ 4 losses │ ~23 nines    │ High k
12 + 4 (RS)      │ 1.333x   │ 4 losses │ ~24 nines    │ Highest k
═════════════════╪══════════╪══════════╪══════════════╪═══════════════
* Under independent failure assumption, AFR=0.02, MTTR=6h
```

Key insight: increasing k (more data chunks) improves durability because the
per-chunk failure probability p is fixed, but you need more simultaneous
failures (m+1) for data loss — and the denominator in the binomial grows
faster than the numerator.

---

## 7. Comparison: Replication vs Erasure Coding

### 7.1 Comprehensive Comparison Table

```
┌──────────────────────────┬────────────────┬────────────────┬────────────────┐
│ Metric                   │ 3x Replication │ EC (8+3)       │ EC (6+3)       │
├──────────────────────────┼────────────────┼────────────────┼────────────────┤
│ Storage overhead         │ 3.0x           │ 1.375x         │ 1.5x           │
│ Fault tolerance          │ 2 failures     │ 3 failures     │ 3 failures     │
│ Durability (with repair) │ ~9-10 nines    │ ~16-17 nines   │ ~14 nines      │
│ Read latency (healthy)   │ Very low       │ Low-Medium     │ Low-Medium     │
│                          │ (read 1 copy)  │ (read 8 chunks)│ (read 6 chunks)│
│ Read latency (degraded)  │ Low            │ Higher         │ Higher         │
│                          │ (read alt copy)│ (decode needed)│ (decode needed)│
│ Write latency            │ Low            │ Medium         │ Medium         │
│                          │ (write 3 copies│ (encode + write│ (encode + write│
│                          │  in parallel)  │  11 chunks)    │  9 chunks)     │
│ Write amplification      │ 3x             │ 1.375x         │ 1.5x           │
│ Repair bandwidth         │ Copy 1 object  │ Read 8 chunks  │ Read 6 chunks  │
│ (per lost chunk)         │ = 1x obj size  │ + compute 1    │ + compute 1    │
│                          │                │ = 1x obj size  │ = 1x obj size  │
│ Repair I/O amplification │ 1x             │ 8x             │ 6x             │
│ Implementation           │ Very simple    │ Complex        │ Complex        │
│ complexity               │                │                │                │
│ Metadata complexity      │ Low            │ Higher         │ Higher         │
│                          │ (3 locations)  │ (11 locations) │ (9 locations)  │
│ Partial object read      │ Easy           │ Possible but   │ Possible but   │
│                          │ (seek to       │ more complex   │ more complex   │
│                          │  offset)       │ (find right    │ (find right    │
│                          │                │  chunk)        │  chunk)        │
└──────────────────────────┴────────────────┴────────────────┴────────────────┘
```

### 7.2 Repair I/O Amplification: The Hidden Cost

When a chunk is lost in an erasure-coded system, repair requires reading k
chunks to reconstruct 1 missing chunk. This is the **repair I/O amplification**:

```
Replication repair:
  Lost: Copy 2 of object O
  Repair: Read Copy 1 (1 full object) → Write Copy 2 (1 full object)
  I/O: 1x read + 1x write = 2x total I/O per repair

Erasure coding (8+3) repair:
  Lost: Chunk D3 of object O
  Repair: Read D1,D2,D4,D5,D6,D7,D8,P1 (8 chunks = 1 full object)
         → Compute D3 via RS decode
         → Write D3 (1 chunk = 1/8 of object)
  I/O: 8x chunk reads + 1x chunk write = effectively 8x + 0.125x

  Net: To reconstruct a single chunk (1/8 of the object),
       you must read 8/8 = the full object's worth of data.
       Repair I/O amplification = 8x
```

This is why repair bandwidth management (Section 12) is so critical. With
hundreds of thousands of disk failures per year, the repair system is
continuously churning through massive amounts of I/O.

### 7.3 When to Use Each

```
Use Replication:                  │ Use Erasure Coding:
──────────────────────────────────┼─────────────────────────────────────
• Small objects (< 1 MB)         │ • Medium to large objects (> 1 MB)
• Latency-critical hot data      │ • Cold or warm data (majority of S3)
• Metadata (small, critical)     │ • Bulk storage at exabyte scale
• Objects that need fast repair  │ • When storage cost matters most
• Config files, indexes          │ • When durability > 11 nines needed
```

S3 likely uses a **hybrid approach**: replication for very small objects and
internal metadata, erasure coding for the vast majority of customer data.

---

## 8. Object Chunking Strategy

### 8.1 The Size Problem

Not all objects are the same size, and the optimal chunking strategy varies
dramatically by object size. S3 supports objects from 0 bytes to 5 TB.

```
Object Size Distribution in a Typical S3 Bucket:

  Size Range     │ % of Objects │ % of Bytes │ Strategy
  ═══════════════╪══════════════╪════════════╪═══════════════════
  0 - 1 KB       │ ~25%         │ < 0.01%    │ Replication or
  1 KB - 128 KB  │ ~30%         │ < 0.1%     │  object packing
  128 KB - 1 MB  │ ~15%         │ ~1%        │
  1 MB - 8 MB    │ ~10%         │ ~5%        │ Single EC stripe
  8 MB - 64 MB   │ ~10%         │ ~15%       │ Single EC stripe
  64 MB - 1 GB   │ ~7%          │ ~30%       │ Multi-segment EC
  1 GB - 5 TB    │ ~3%          │ ~50%       │ Multi-segment EC
  ═══════════════╪══════════════╪════════════╪═══════════════════
```

The majority of objects are small, but the majority of bytes are in large
objects. The chunking strategy must handle both efficiently.

### 8.2 Small Objects (< 1 MB)

Erasure coding a 100 KB object with (8,3) would create 11 chunks of ~12.5 KB
each. This is problematic:

```
Problems with EC for small objects:
  1. Metadata overhead: each chunk needs metadata (chunk_id, checksum,
     location). For 11 chunks at ~100 bytes metadata each = 1,100 bytes
     of metadata for a 100 KB object = 1.1% metadata overhead.
  2. IOPS amplification: writing 11 small chunks means 11 disk seeks.
     For small files, seek time dominates transfer time.
  3. Network overhead: 11 RPCs to 11 different nodes. RPC overhead
     (headers, connection setup) may exceed the chunk payload.
```

**Solution: Object packing (batching)**

```
Small Object Packing:

  ┌─────────────────────────────────────────────────┐
  │              Pack (Container Object)              │
  │                                                   │
  │  ┌──────┐ ┌─────────┐ ┌───┐ ┌──────────┐ ┌───┐ │
  │  │Obj A │ │ Obj B   │ │C  │ │  Obj D   │ │ E │ │
  │  │50 KB │ │ 200 KB  │ │5KB│ │ 100 KB   │ │8KB│ │
  │  └──────┘ └─────────┘ └───┘ └──────────┘ └───┘ │
  │                                                   │
  │  Pack total: ~363 KB                              │
  └─────────────────────────────────────────────────┘
                      │
                      ▼
  Erasure code the ENTIRE pack as one unit (363 KB → 8+3 chunks)
  Each chunk: ~45 KB (much more reasonable)

  Index: {Obj A: offset 0, len 50KB}, {Obj B: offset 50KB, len 200KB}, ...
```

Alternatively, small objects may simply use **3x replication** since the
storage overhead penalty is tiny in absolute terms (3 x 100 KB = 300 KB — who
cares about 200 KB of waste when the object is 100 KB?).

### 8.3 Medium Objects (1 MB - 100 MB)

The sweet spot for erasure coding. A single erasure-coded stripe handles these
efficiently.

```
Example: 8 MB object with (8, 3) EC

  Object: 8 MB
  k = 8 data chunks, each 1 MB
  m = 3 parity chunks, each 1 MB
  Total: 11 chunks x 1 MB = 11 MB stored

  ┌──────┬──────┬──────┬──────┬──────┬──────┬──────┬──────┬──────┬──────┬──────┐
  │  D1  │  D2  │  D3  │  D4  │  D5  │  D6  │  D7  │  D8  │  P1  │  P2  │  P3  │
  │ 1 MB │ 1 MB │ 1 MB │ 1 MB │ 1 MB │ 1 MB │ 1 MB │ 1 MB │ 1 MB │ 1 MB │ 1 MB │
  └──────┴──────┴──────┴──────┴──────┴──────┴──────┴──────┴──────┴──────┴──────┘
  │      │      │      │      │      │      │      │      │      │      │
  Node1  Node2  Node3  Node4  Node5  Node6  Node7  Node8  Node9  Nd10   Nd11
  AZ-a   AZ-b   AZ-c   AZ-a   AZ-b   AZ-c   AZ-a   AZ-b   AZ-c   AZ-a   AZ-b
```

### 8.4 Large Objects (100 MB - 5 TB)

Large objects are split into multiple **segments**, each independently
erasure-coded. This provides several benefits:

```
Benefits of segmented erasure coding:

  1. Parallel I/O: segments can be read/written in parallel
  2. Partial repair: if one segment's chunk is corrupted, only that
     segment needs repair (not the entire multi-TB object)
  3. Range reads: S3 supports reading a byte range of an object.
     With segments, only the relevant segment(s) need to be read
  4. Memory efficiency: encoding/decoding happens per-segment,
     so the node only needs to buffer one segment at a time
```

```
Example: 40 MB object, segment size = 8 MB

  Object (40 MB) split into 5 segments:

  Segment 1 (8 MB): EC → [D1..D8 | P1..P3]  across 11 nodes (set A)
  Segment 2 (8 MB): EC → [D1..D8 | P1..P3]  across 11 nodes (set B)
  Segment 3 (8 MB): EC → [D1..D8 | P1..P3]  across 11 nodes (set C)
  Segment 4 (8 MB): EC → [D1..D8 | P1..P3]  across 11 nodes (set D)
  Segment 5 (8 MB): EC → [D1..D8 | P1..P3]  across 11 nodes (set E)

  Total chunks: 5 x 11 = 55 chunks
  Total stored: 5 x 11 MB = 55 MB (overhead still 1.375x)

  Note: Sets A-E may overlap (same node can hold chunks from
  different segments), but within a single segment, all 11 chunks
  are on distinct nodes.
```

```
Example: 1 TB object

  Segment size: 8 MB
  Number of segments: 1,048,576 MB / 8 MB = 131,072 segments
  Total chunks: 131,072 x 11 = 1,441,792 chunks
  Total stored: ~1.375 TB

  These 1.4 million chunks are distributed across thousands of
  nodes in 3 AZs.

  Metadata for this object:
    Segment index: 131,072 entries
    Each entry: segment_id, 11 x (node_id, chunk_id, checksum)
    Metadata size: ~131,072 x 11 x 50 bytes ≈ 72 MB of metadata

  This is why S3's metadata layer (index service) is itself a
  distributed database — a single object can generate tens of
  megabytes of chunk metadata.
```

### 8.5 Multipart Upload Alignment

S3's multipart upload API (required for objects > 5 GB, optional for > 5 MB)
maps naturally to the segmented erasure coding model:

```
Client uploads 100 MB file using multipart upload:

  Part 1 (8 MB)  ──→  Segment 1  ──→  EC  ──→  11 chunks stored
  Part 2 (8 MB)  ──→  Segment 2  ──→  EC  ──→  11 chunks stored
  ...
  Part 12 (8 MB) ──→  Segment 12 ──→  EC  ──→  11 chunks stored
  Part 13 (4 MB) ──→  Segment 13 ──→  EC  ──→  11 chunks stored (padded)

  CompleteMultipartUpload:
    → Creates the segment index
    → Object becomes readable
    → Returns success to client
```

Each part can be encoded and stored as it arrives, without waiting for the
entire object. This enables streaming uploads.

---

## 9. Storage Node Architecture

### 9.1 Physical Server Specification (Typical)

```
Storage-Optimized Server (e.g., AWS custom hardware):
  ┌─────────────────────────────────────────────────┐
  │  CPU:    2 x Intel Xeon (for EC encode/decode)  │
  │  RAM:    256-512 GB (caching, buffering)         │
  │  Boot:   2 x 480 GB SSD (RAID-1, OS + logs)     │
  │  Data:   48 x 16 TB HDD = 768 TB raw capacity   │
  │  NIC:    2 x 25 Gbps (bonded, to ToR switch)    │
  │  Power:  Dual PSU (A+B power feeds)              │
  └─────────────────────────────────────────────────┘
```

### 9.2 Software Architecture

```
Storage Node Software Stack:

  ┌──────────────────────────────────────────────────────────────┐
  │                    Storage Node Daemon                       │
  │                                                              │
  │  ┌────────────────────────────────────────────────────────┐  │
  │  │              Chunk Store (Core Component)               │  │
  │  │                                                         │  │
  │  │  Manages chunks on local disks:                         │  │
  │  │  ┌─────────────────────────────────────────────────┐    │  │
  │  │  │ Chunk Metadata (in-memory index + on-disk log): │    │  │
  │  │  │   {                                             │    │  │
  │  │  │     chunk_id:    "c-a1b2c3d4e5f6",              │    │  │
  │  │  │     object_id:   "o-1234567890ab",              │    │  │
  │  │  │     segment_id:  42,                            │    │  │
  │  │  │     chunk_index: 3,  // D4 in the stripe        │    │  │
  │  │  │     chunk_type:  "data",  // or "parity"        │    │  │
  │  │  │     checksum:    "sha256:abcdef...",            │    │  │
  │  │  │     size:        1048576,  // 1 MB              │    │  │
  │  │  │     disk_id:     "/dev/sdc",                    │    │  │
  │  │  │     offset:      83886080,  // byte offset      │    │  │
  │  │  │     created_at:  "2024-01-15T10:30:00Z"         │    │  │
  │  │  │   }                                             │    │  │
  │  │  └─────────────────────────────────────────────────┘    │  │
  │  │                                                         │  │
  │  │  Disk Layout:                                           │  │
  │  │    Chunks stored as variable-length records in a log-   │  │
  │  │    structured format, or as individual files on ext4/   │  │
  │  │    XFS. Trade-offs:                                     │  │
  │  │    - Log-structured: better write throughput, complex GC│  │
  │  │    - Individual files: simpler, more FS overhead        │  │
  │  │                                                         │  │
  │  │  Write Path:                                            │  │
  │  │    1. Receive chunk data over network                   │  │
  │  │    2. Compute checksum (SHA-256)                        │  │
  │  │    3. Verify checksum matches expected value            │  │
  │  │    4. Write chunk data to disk                          │  │
  │  │    5. Call fsync() to ensure data hits physical media   │  │
  │  │    6. Update local chunk index                          │  │
  │  │    7. Send ACK to caller                                │  │
  │  └────────────────────────────────────────────────────────┘  │
  │                                                              │
  │  ┌────────────────────────────────────────────────────────┐  │
  │  │           Integrity Checker (Background Scrubber)       │  │
  │  │                                                         │  │
  │  │  Purpose: detect bit rot before it causes data loss     │  │
  │  │                                                         │  │
  │  │  Operation:                                             │  │
  │  │    - Continuously reads chunks from disk                │  │
  │  │    - Computes checksum of each chunk                    │  │
  │  │    - Compares against stored checksum                   │  │
  │  │    - If mismatch: reports corrupted chunk to repair     │  │
  │  │      coordinator service                                │  │
  │  │    - Target: complete full disk scrub every 14 days     │  │
  │  │                                                         │  │
  │  │  Scrub scheduling:                                      │  │
  │  │    768 TB / 14 days = 54.8 TB/day = 2.28 TB/hour       │  │
  │  │    At 150 MB/s sequential read: ~4.3 hours to read      │  │
  │  │    one 16 TB disk. 48 disks in parallel → feasible      │  │
  │  │    but must be throttled to avoid impacting customer I/O │  │
  │  └────────────────────────────────────────────────────────┘  │
  │                                                              │
  │  ┌────────────────────────────────────────────────────────┐  │
  │  │                    Repair Agent                         │  │
  │  │                                                         │  │
  │  │  Receives repair requests from the repair coordinator:  │  │
  │  │    "Reconstruct chunk C3 of object O on this node"      │  │
  │  │                                                         │  │
  │  │  Process:                                               │  │
  │  │    1. Receive repair task: {object_id, chunk_index,     │  │
  │  │       source_chunks: [(node, chunk_id) x 8]}            │  │
  │  │    2. Fetch k=8 chunks from source nodes (parallel)     │  │
  │  │    3. Run Reed-Solomon erasure decoding                 │  │
  │  │    4. Extract the missing chunk from decoded output     │  │
  │  │    5. Verify checksum of reconstructed chunk            │  │
  │  │    6. Write chunk to local disk (via Chunk Store)       │  │
  │  │    7. Report success to repair coordinator              │  │
  │  │                                                         │  │
  │  │  Concurrency: handles multiple repair tasks in parallel │  │
  │  │  Throttling: repair I/O is lower priority than customer │  │
  │  │             reads/writes to avoid performance impact    │  │
  │  └────────────────────────────────────────────────────────┘  │
  │                                                              │
  │  ┌────────────────────────────────────────────────────────┐  │
  │  │                  Resource Manager                       │  │
  │  │                                                         │  │
  │  │  Monitors and reports node health:                      │  │
  │  │    - Disk health: SMART data (reallocated sectors,      │  │
  │  │      pending sectors, temperature, power-on hours)      │  │
  │  │    - Capacity: bytes used / bytes available per disk    │  │
  │  │    - I/O utilization: IOPS and bandwidth per disk       │  │
  │  │    - Network: bandwidth utilization, error rates        │  │
  │  │    - CPU/memory: utilization for EC compute             │  │
  │  │                                                         │  │
  │  │  Reports to control plane:                              │  │
  │  │    - Heartbeat every 5-10 seconds                       │  │
  │  │    - Capacity report every minute                       │  │
  │  │    - Health alerts (disk predicted failure) immediately  │  │
  │  │                                                         │  │
  │  │  I/O Throttling:                                        │  │
  │  │    Priority: Customer reads > Customer writes >         │  │
  │  │              Repair I/O > Scrub I/O                     │  │
  │  │    Uses cgroups or custom I/O scheduler to enforce      │  │
  │  └────────────────────────────────────────────────────────┘  │
  └──────────────────────────────────────────────────────────────┘
```

### 9.3 Disk Layout Detail

```
Single 16 TB HDD Layout:

  ┌─────────────────────────────────────────────────────────────────┐
  │ Disk Header (4 KB)                                              │
  │   - Disk UUID                                                   │
  │   - Node ID                                                     │
  │   - Format version                                              │
  │   - Creation timestamp                                          │
  ├─────────────────────────────────────────────────────────────────┤
  │ Chunk Index Region (variable, ~1 GB)                            │
  │   - B-tree or hash index: chunk_id → (offset, length, checksum)│
  │   - Cached entirely in RAM for fast lookups                     │
  ├─────────────────────────────────────────────────────────────────┤
  │ Chunk Data Region (~15.999 TB)                                  │
  │   ┌──────────┬──────────┬──────────┬──────────┬───────────────┐│
  │   │ Chunk 1  │ Chunk 2  │ Chunk 3  │ Chunk 4  │  ...          ││
  │   │ (1 MB)   │ (512 KB) │ (2 MB)   │ (1 MB)   │               ││
  │   └──────────┴──────────┴──────────┴──────────┴───────────────┘│
  │   Chunks are variable-length, stored sequentially               │
  │   Deleted chunks leave gaps → periodic compaction needed        │
  ├─────────────────────────────────────────────────────────────────┤
  │ Write-Ahead Log (WAL, ~10 GB)                                   │
  │   - New chunks written here first, then moved to data region    │
  │   - Provides crash consistency                                  │
  └─────────────────────────────────────────────────────────────────┘

  Approximate chunk count per disk:
    16 TB / 1 MB average chunk = ~16 million chunks per disk
    16 million chunks x 48 disks/node = ~768 million chunks per node
```

---

## 10. Data Integrity — Defense in Depth

### 10.1 The Four Layers of Checksums

S3 employs a defense-in-depth strategy with four independent checksum
verification layers. Data corruption must evade ALL four layers to cause
undetected data loss.

```
Layer │ Where                    │ What                       │ When
══════╪══════════════════════════╪════════════════════════════╪══════════════════
  1   │ Client ↔ S3 Frontend    │ Upload/download checksum   │ Every PUT/GET
  2   │ Per-chunk on disk        │ Chunk-level SHA-256        │ Every write,
      │                          │                            │ every read
  3   │ After EC decode          │ Reconstructed object       │ When degraded
      │                          │ checksum                   │ read occurs
  4   │ Background scrub         │ Periodic chunk verification│ Every 14-30 days
══════╪══════════════════════════╪════════════════════════════╪══════════════════
```

### 10.2 Layer 1: Upload / Download Checksums

```
Upload (PUT) Path:

  Client                          S3 Frontend
  ──────                          ────────────
    │                                  │
    │  PUT /bucket/key                 │
    │  Content-MD5: <base64-md5>       │
    │  x-amz-checksum-sha256: <hash>   │
    │  Body: <object data>             │
    │─────────────────────────────────→│
    │                                  │
    │                                  │── Receive all bytes
    │                                  │── Compute MD5 and/or SHA-256
    │                                  │── Compare with client-provided checksum
    │                                  │
    │                                  │── Match? → Continue to store
    │                                  │── Mismatch? → Return 400 BadDigest
    │                                  │
    │  200 OK                          │
    │  ETag: "<md5-of-object>"         │
    │←─────────────────────────────────│

  This catches: network corruption during upload (bit flips, truncation)

Download (GET) Path:

  Client                          S3 Frontend
  ──────                          ────────────
    │  GET /bucket/key                 │
    │─────────────────────────────────→│
    │                                  │── Read object (from chunks)
    │                                  │── Compute checksum
    │                                  │── Compare with stored checksum
    │  200 OK                          │
    │  x-amz-checksum-sha256: <hash>   │
    │  Body: <object data>             │
    │←─────────────────────────────────│
    │                                  │
    │── Verify checksum on client side │

  This catches: corruption during download, or if stored data was silently
  corrupted between storage and read
```

### 10.3 Layer 2: Per-Chunk Checksums

```
Chunk Write (inside storage node):

  ┌──────────────────────────────────────────────────────────┐
  │  1. Receive chunk bytes from network                     │
  │  2. Compute SHA-256 of chunk bytes                       │
  │  3. Compare with checksum sent by the frontend:          │
  │       Match? → proceed                                   │
  │       Mismatch? → reject, report error                   │
  │  4. Write chunk bytes to disk                            │
  │  5. fsync() — force to physical media                    │
  │  6. Read back chunk from disk (optional: write verify)   │
  │  7. Compute SHA-256 of read-back data                    │
  │  8. Compare with step 2 checksum:                        │
  │       Match? → chunk stored successfully                 │
  │       Mismatch? → disk sector error, retry on diff disk  │
  │  9. Store checksum in chunk metadata index               │
  └──────────────────────────────────────────────────────────┘

Chunk Read (when serving a GET request):

  ┌──────────────────────────────────────────────────────────┐
  │  1. Look up chunk location in index: disk + offset       │
  │  2. Read chunk bytes from disk                           │
  │  3. Compute SHA-256 of read bytes                        │
  │  4. Compare with stored checksum in index:               │
  │       Match? → return chunk to caller                    │
  │       Mismatch? → chunk is CORRUPTED                     │
  │         → Return error to caller                         │
  │         → Report to repair coordinator                   │
  │         → Caller will fetch from another chunk instead   │
  └──────────────────────────────────────────────────────────┘
```

### 10.4 Layer 3: Reconstruction Checksums

```
When a degraded read occurs (one or more data chunks missing):

  ┌──────────────────────────────────────────────────────────────┐
  │  1. Fetched 8 chunks (mix of data + parity)                  │
  │  2. Each chunk verified against its own checksum (Layer 2)   │
  │  3. Reed-Solomon decode → reconstruct missing data chunks    │
  │  4. Reassemble full object from 8 data chunks                │
  │  5. Compute SHA-256 of full reassembled object               │
  │  6. Compare with stored object-level checksum:               │
  │       Match? → return to client                              │
  │       Mismatch? → FATAL — possible silent corruption         │
  │         → Try different combination of chunks                │
  │         → If all combinations fail → data integrity alert    │
  └──────────────────────────────────────────────────────────────┘
```

### 10.5 Layer 4: Background Scrubbing

Covered in detail in Section 11.

### 10.6 End-to-End Integrity Flow

```
Complete flow for a PUT then GET, showing all checksum verification points:

  PUT "photo.jpg" (8 MB):
    [Client]
       │
       │ Content-MD5: abc123...
       ▼
    [S3 Frontend] ─── Layer 1: verify upload checksum ───
       │                                                  │
       │ Split into 8 data chunks + 3 parity             PASS
       ▼
    [Storage Nodes x11] ─── Layer 2: verify each chunk ──
       │                                                  │
       │ Each node writes chunk with checksum             PASS
       │ Each node does fsync + optional read-verify
       ▼
    [Chunks on Disk] ─── Layer 4: background scrub (ongoing) ──
       │                                                        │
       │ Periodic verification of all chunks                    CHECK
       ▼
    ─────── Time passes (hours, days, months) ───────

  GET "photo.jpg":
    [Client]
       │
       │ GET /bucket/photo.jpg
       ▼
    [S3 Frontend]
       │
       │ Read 8 data chunks from storage nodes
       ▼
    [Storage Nodes x8] ─── Layer 2: verify each chunk on read ──
       │                                                         │
       │ All 8 chunks pass checksum?                             │
       │   YES → return chunks (no decode needed)                PASS
       │   NO  → fetch parity chunk, decode                      │
       │         ─── Layer 3: verify reconstructed object ───    │
       ▼                                                         │
    [S3 Frontend] ─── Layer 1: compute download checksum ──      │
       │                                                         PASS
       │ ETag + checksum headers
       ▼
    [Client] ─── Client verifies checksum ──
                                              PASS → Data intact!
```

---

## 11. Background Scrubbing Deep Dive

### 11.1 Why Scrubbing is Essential

Disk data can silently corrupt over time — a phenomenon known as **bit rot**.
Unlike a full disk failure (which is obvious), bit rot affects individual
sectors without any error signal until the data is read and checked.

```
Bit Rot Statistics:
  - Undetected bit error rate for enterprise HDDs: ~1 in 10^15 bits read
  - 16 TB disk = 1.28 x 10^14 bits
  - Expected errors per full disk read: ~0.13 (one error per ~8 full reads)
  - Over a year with no scrubbing: a non-trivial number of chunks may
    have silent corruption

Without scrubbing, bit rot accumulates:
  Day 0:   0 corrupted chunks
  Day 30:  A few chunks per disk may have silent corruption
  Day 180: Many chunks per disk
  Day 365: Significant risk that multiple chunks of the same object
           are corrupted → potential data loss
```

Scrubbing detects corruption early, while enough healthy chunks still exist to
repair the corrupted one.

### 11.2 Scrub Process

```
Background Scrub Loop (per disk):

  ┌──────────────────────────────────────────────────────────┐
  │  WHILE disk is active:                                   │
  │    FOR each chunk on this disk (sequential scan):        │
  │      1. Read chunk bytes from disk                       │
  │      2. Compute SHA-256(chunk_bytes)                     │
  │      3. Fetch stored checksum from chunk index           │
  │      4. IF computed != stored:                           │
  │           a. Log corruption event:                       │
  │              {chunk_id, disk_id, expected_checksum,      │
  │               actual_checksum, timestamp}                │
  │           b. Mark chunk as CORRUPTED in local index      │
  │           c. Report to Repair Coordinator:               │
  │              "Chunk X on Node Y is corrupted, needs      │
  │               reconstruction from other chunks"          │
  │      5. IF computed == stored:                           │
  │           No action needed (chunk is healthy)            │
  │      6. Yield CPU/IO to avoid impacting customer traffic │
  │    END FOR                                               │
  │    Log: "Full scrub cycle completed for disk Z"          │
  │    Sleep(brief) then start next cycle                    │
  │  END WHILE                                               │
  └──────────────────────────────────────────────────────────┘
```

### 11.3 Scrub Rate Calculation

```
Target: Scrub each disk completely every 14 days

Per disk:
  Disk capacity: 16 TB
  Utilization: ~80% = 12.8 TB of actual chunk data
  Days per cycle: 14
  Data per day: 12.8 TB / 14 = 914 GB/day
  Data per hour: 914 / 24 = 38.1 GB/hour
  Data per second: 38.1 GB / 3600 = 10.6 MB/s

Per node (48 disks):
  Total scrub bandwidth: 48 x 10.6 MB/s = 508.8 MB/s
  Disk sequential read speed: ~150-200 MB/s per HDD
  Scrub is only ~7% of each disk's max bandwidth → manageable

Network impact: None (scrubbing is purely local I/O)
CPU impact: SHA-256 at 10.6 MB/s per disk = trivial for modern CPUs
            48 disks x 10.6 MB/s = 508.8 MB/s of SHA-256
            Modern CPU with SHA-NI extensions: 2+ GB/s → easily handles it
```

### 11.4 Scrub I/O Priority

```
I/O Priority Stack (highest to lowest):

  Priority 1: Customer GET (reads)
    → Latency-sensitive, directly impacts user experience
    → Never delayed by scrubbing

  Priority 2: Customer PUT (writes)
    → Latency-sensitive, impacts upload experience
    → Takes priority over background work

  Priority 3: Repair I/O
    → Time-sensitive: the longer a chunk is missing, the higher the
      risk of data loss if another chunk fails
    → Higher priority than scrubbing because it addresses a known gap
      in durability

  Priority 4: Background Scrub I/O
    → Important but not urgent
    → Can be paused/throttled when disks are busy with customer traffic
    → Automatically ramps up during off-peak hours (e.g., 2-6 AM local)

Implementation: Linux cgroups or custom I/O scheduler
  Scrub process assigned to low-priority cgroup:
    blkio.weight: 100 (vs 1000 for customer I/O)
    io.latency: 50ms (acceptable latency for scrub reads)
```

### 11.5 Scrubbing and Durability Interaction

```
Without scrubbing:
  Bit rot accumulates undetected
  By the time a chunk is needed (for a read or repair), it may already
  be corrupted
  If 4+ chunks of the same object are corrupted → silent data loss

  Timeline (no scrub, (8,3) EC):
  ─────────────────────────────────────────────────────────────────
  Month 0: All 11 chunks healthy
  Month 3: 1 chunk has bit rot (undetected)
  Month 8: 2 chunks have bit rot (undetected)
  Month 14: 3 chunks have bit rot (undetected)
  Month 20: 4 chunks have bit rot → SILENT DATA LOSS
            (only discovered when someone tries to read the object)
  ─────────────────────────────────────────────────────────────────

With scrubbing (14-day cycle):
  Corruption is detected within 14 days of occurring
  Repair is triggered immediately upon detection
  Window of vulnerability: maximum 14 days per chunk

  Timeline (14-day scrub, (8,3) EC):
  ─────────────────────────────────────────────────────────────────
  Day 0:  All 11 chunks healthy
  Day 10: 1 chunk develops bit rot
  Day 14: Scrub detects corrupted chunk → repair triggered
  Day 14.5: Repair complete, chunk rebuilt on new location
  Day 14.5: Back to 11 healthy chunks
  ─────────────────────────────────────────────────────────────────

  For data loss to occur with scrubbing:
    4+ chunks must become corrupted within the SAME 14-day window
    AND the corruptions must go undetected until the next scrub
    AND repair of earlier detections must not complete in time

  This is astronomically unlikely for independent failures.
```

---

## 12. Repair and Reconstruction

### 12.1 Repair Triggers

A repair is triggered when any of these events occur:

```
Trigger                    │ Detection Method          │ Priority
═══════════════════════════╪═══════════════════════════╪═══════════════
Disk failure               │ I/O errors, SMART alerts  │ HIGH
                           │ heartbeat shows disk gone │
Node failure               │ Heartbeat timeout (30s)   │ HIGH
                           │                           │
Chunk checksum mismatch    │ Read-time verification    │ HIGH
(on customer read)         │                           │
Chunk checksum mismatch    │ Background scrub          │ MEDIUM
(during scrub)             │                           │
Predictive disk failure    │ SMART: reallocated sectors│ LOW
                           │ exceeds threshold         │ (preemptive)
Rack failure               │ Multiple node timeouts    │ CRITICAL
                           │ from same rack            │
═══════════════════════════╪═══════════════════════════╪═══════════════
```

### 12.2 Repair Flow (Detailed)

```
Repair Flow for a Single Missing Chunk:

  Repair Coordinator (central service)
  ─────────────────────────────────────
    │
    │  1. Detects: "Chunk C3 of Object O is missing/corrupted"
    │     Source: scrub report, disk failure event, or read error
    │
    │  2. Looks up Object O's chunk map in metadata:
    │     O = {
    │       chunks: [
    │         C1:  (Node-1,  AZ-a, healthy),
    │         C2:  (Node-5,  AZ-b, healthy),
    │         C3:  (Node-9,  AZ-c, MISSING), ← THIS ONE
    │         C4:  (Node-2,  AZ-a, healthy),
    │         C5:  (Node-6,  AZ-b, healthy),
    │         C6:  (Node-10, AZ-c, healthy),
    │         C7:  (Node-3,  AZ-a, healthy),
    │         C8:  (Node-7,  AZ-b, healthy),
    │         P1:  (Node-11, AZ-c, healthy),
    │         P2:  (Node-4,  AZ-a, healthy),
    │         P3:  (Node-8,  AZ-b, healthy)
    │       ]
    │     }
    │
    │  3. Selects k=8 healthy chunks for reconstruction:
    │     Source chunks: C1, C2, C4, C5, C6, C7, C8, P1
    │     (Any 8 of the 10 remaining healthy chunks will work)
    │
    │  4. Selects a target node for the new chunk:
    │     Must be in AZ-c (to maintain AZ distribution)
    │     Must NOT be Node-9 (the failed source)
    │     Must NOT be on the same rack as Node-9 (if possible)
    │     Selected: Node-12 in AZ-c
    │
    │  5. Sends repair task to Node-12:
    │     {
    │       task: "reconstruct",
    │       object_id: O,
    │       missing_chunk_index: 3,  // C3
    │       source_chunks: [
    │         (Node-1,  C1),
    │         (Node-5,  C2),
    │         (Node-2,  C4),
    │         (Node-6,  C5),
    │         (Node-10, C6),
    │         (Node-3,  C7),
    │         (Node-7,  C8),
    │         (Node-11, P1)
    │       ],
    │       expected_checksum: "sha256:xyz..."
    │     }
    │
    ▼
  Node-12 (Repair Agent):
  ─────────────────────────
    │
    │  6. Fetch 8 source chunks in parallel:
    │     Node-1  → C1 (1 MB)  ─┐
    │     Node-5  → C2 (1 MB)   │
    │     Node-2  → C4 (1 MB)   │
    │     Node-6  → C5 (1 MB)   ├── 8 parallel reads
    │     Node-10 → C6 (1 MB)   │   Total data: 8 MB
    │     Node-3  → C7 (1 MB)   │
    │     Node-7  → C8 (1 MB)   │
    │     Node-11 → P1 (1 MB)  ─┘
    │
    │  7. Run Reed-Solomon erasure decoding:
    │     Input: C1, C2, C4, C5, C6, C7, C8, P1
    │     Output: reconstructed C3 (1 MB)
    │     Time: < 1 ms for 1 MB chunk
    │
    │  8. Verify reconstructed chunk:
    │     Compute SHA-256(C3_reconstructed)
    │     Compare with expected_checksum
    │     Match? → proceed
    │     Mismatch? → report error (one of the source chunks may be bad)
    │
    │  9. Write C3 to local disk (via Chunk Store):
    │     → checksum verification
    │     → fsync
    │     → update local index
    │
    │  10. Report success to Repair Coordinator
    │
    ▼
  Repair Coordinator:
  ─────────────────────
    │
    │  11. Update Object O's chunk map in metadata:
    │      C3: (Node-9, AZ-c) → (Node-12, AZ-c)
    │
    │  12. Delete reference to old C3 on Node-9 (if node is accessible)
    │
    │  13. Log repair completion:
    │      {object: O, chunk: C3, old_node: 9, new_node: 12,
    │       duration_ms: 450, source_chunks: 8, success: true}
    │
    ▼
  DONE: Object O is back to full 11/11 healthy chunks
```

### 12.3 Repair Bandwidth Estimation at Scale

```
Assumptions:
  Fleet: 10 million HDDs
  AFR: 2%
  Disk failures/day: 10,000,000 x 0.02 / 365 = 548 disks/day
  Chunks per disk: ~16 million
  Chunks to repair per day: 548 x 16,000,000 = 8.77 billion chunks/day

  But wait — each chunk is ~1 MB. Repairing 1 chunk requires reading 8 chunks.
  Total repair reads per day: 8.77 x 10^9 x 8 = 70.2 billion chunk reads/day
  Total repair read bandwidth: 70.2 x 10^9 x 1 MB = 70.2 exabytes/day

  That seems impossibly high. Let's reconsider.
```

In practice, the repair bandwidth is managed by:

```
Optimization 1: Batch repair
  When a disk fails, all 16M chunks on it need repair.
  But the source chunks for different objects overlap — many share the
  same source nodes. Repair can be batched: read a source chunk once,
  use it for multiple repair operations.

Optimization 2: Prioritized repair
  Not all chunks are equally urgent:
  - Objects with only 8 healthy chunks (1 more failure = data loss):
    CRITICAL priority
  - Objects with 9 healthy chunks: HIGH priority
  - Objects with 10 healthy chunks: MEDIUM priority

Optimization 3: Repair spread across entire fleet
  The 548 failed disks are spread across the fleet.
  Source chunks are spread across millions of nodes.
  Repair I/O is distributed: each node handles a small fraction.

  Per-node repair I/O:
    Total fleet: ~200,000 storage nodes
    Repair tasks per node per day: 8.77 x 10^9 / 200,000 = ~43,850 tasks
    Repair read per node per day: 43,850 x 8 MB = 351 GB/day = ~4 MB/s

    4 MB/s of repair reads per node is very manageable alongside
    normal customer I/O.

Optimization 4: Time budget
  MTTR target: 6 hours
  With 16 million chunks to repair per disk failure:
    Chunks/hour: 16,000,000 / 6 = 2,666,667 chunks/hour
    Chunks/second: ~741 chunks/second

  Each repair requires reading 8 MB and writing 1 MB:
    Read bandwidth: 741 x 8 MB = 5.9 GB/s
    Write bandwidth: 741 x 1 MB = 741 MB/s

  This is spread across thousands of source nodes and the target
  nodes, so no single node bears the full load.
```

### 12.4 Repair Prioritization

```
Priority Queue for Repair Tasks:

  ┌────────────────────────────────────────────────────────────┐
  │ CRITICAL (repair within minutes):                          │
  │   Objects with exactly k (8) healthy chunks.               │
  │   One more failure = DATA LOSS.                            │
  │   These are repaired FIRST, ahead of everything else.      │
  ├────────────────────────────────────────────────────────────┤
  │ HIGH (repair within 1 hour):                               │
  │   Objects with k+1 (9) healthy chunks.                     │
  │   Can tolerate 1 more failure, but margin is thin.         │
  ├────────────────────────────────────────────────────────────┤
  │ MEDIUM (repair within 6 hours):                            │
  │   Objects with k+2 (10) healthy chunks.                    │
  │   Normal operating margin. Standard repair priority.       │
  ├────────────────────────────────────────────────────────────┤
  │ LOW (repair within 24 hours):                              │
  │   Preemptive repairs: disk showing SMART warnings but      │
  │   not yet failed. Migrate chunks off the suspect disk.     │
  └────────────────────────────────────────────────────────────┘
```

---

## 13. Data Placement Service

### 13.1 Role and Responsibilities

The Data Placement Service decides **where** to store each chunk when a new
object is written. This is one of the most critical components for achieving
durability, because poor placement can create correlated failure vulnerabilities.

### 13.2 Inputs

```
Input                         │ Source                │ Purpose
══════════════════════════════╪═══════════════════════╪══════════════════════
AZ topology                   │ Config database       │ Ensure cross-AZ spread
Rack topology                 │ Config database       │ Avoid same-rack chunks
Node capacity (free space)    │ Resource managers     │ Balance utilization
Node health status            │ Heartbeats            │ Avoid unhealthy nodes
Current node I/O load         │ Resource managers     │ Avoid hot nodes
Power/network topology        │ Config database       │ Avoid shared PDU/switch
Recent placement history      │ Placement cache       │ Avoid re-placing on
                              │                       │ recently failed nodes
```

### 13.3 Placement Constraints (Hard Rules)

These constraints MUST be satisfied. If they cannot be, the write is rejected.

```
1. CROSS-AZ SPREAD: Chunks must be distributed across >= 3 AZs
   Enforcement: Select target AZs first, then select nodes within each AZ

2. NODE UNIQUENESS: No two chunks of the same object on the same node
   Enforcement: Track selected nodes, exclude from further selection

3. DISK UNIQUENESS: No two chunks on the same physical disk
   Enforcement: Inherent (since we use node uniqueness and typically
   assign one chunk per node)

4. CAPACITY: Target node must have sufficient free disk space
   Enforcement: Filter nodes below capacity threshold

5. HEALTH: Target node must be in "healthy" state
   Enforcement: Filter nodes not in healthy state
```

### 13.4 Placement Preferences (Soft Rules)

These are optimized for but not strictly required:

```
1. RACK DIVERSITY: Prefer chunks on different racks
   Reason: Rack-level failures (PDU, ToR switch) are correlated

2. LOAD BALANCE: Prefer nodes with lower current I/O utilization
   Reason: Spread write load, avoid creating hot spots

3. CAPACITY BALANCE: Prefer nodes with more free space
   Reason: Prevent some disks from filling up while others are empty

4. POWER DIVERSITY: Prefer nodes on different power circuits
   Reason: Power circuit failures affect all racks on that circuit

5. NETWORK DIVERSITY: Prefer nodes connected to different aggregation switches
   Reason: Aggregation switch failure affects all racks below it
```

### 13.5 Placement Algorithm

```
Pseudocode: Select 11 Nodes for (8,3) Erasure Coding

function selectPlacementGroup(objectSize, numChunks=11):
    // Step 1: Determine AZ distribution
    azList = getAvailableAZs()    // e.g., [AZ-a, AZ-b, AZ-c]
    assert len(azList) >= 3

    // Distribute chunks across AZs: 4-4-3 (round-robin)
    azDistribution = distributeEvenly(numChunks, azList)
    // e.g., {AZ-a: 4, AZ-b: 4, AZ-c: 3}

    selectedNodes = []

    // Step 2: For each AZ, select the required number of nodes
    for az, count in azDistribution:
        candidates = getHealthyNodes(az)
        candidates = filterByCapacity(candidates, objectSize / 8)
        candidates = excludeAlreadySelected(candidates, selectedNodes)

        // Step 3: Apply soft preferences via weighted scoring
        for node in candidates:
            node.score = computeScore(node)
                // score considers:
                //   + rack diversity bonus (different rack from selected)
                //   + capacity headroom bonus
                //   + low I/O utilization bonus
                //   + power diversity bonus
                //   - penalty for recent repair activity
                //   - penalty for SMART warnings

        // Step 4: Weighted random selection (not purely top-N)
        // Using randomness prevents pathological patterns and
        // naturally load-balances over time
        selected = weightedRandomSample(candidates, count)
        selectedNodes.extend(selected)

    // Step 5: Assign chunk indices to nodes
    shuffle(selectedNodes)  // randomize which node gets which chunk
    return zip(range(numChunks), selectedNodes)
    // e.g., [(D1, Node-1), (D2, Node-5), ..., (P3, Node-8)]
```

### 13.6 Placement Group Persistence

```
Once chunks are placed, the assignment is recorded as the object's CHUNK MAP:

Chunk Map for Object "s3://my-bucket/photo.jpg":
┌───────────┬────────────┬────────────┬────────┬─────────────────────┐
│ Chunk Idx │ Chunk Type │ Node ID    │ AZ     │ Checksum            │
├───────────┼────────────┼────────────┼────────┼─────────────────────┤
│ 0         │ Data (D1)  │ node-0a1f  │ AZ-a   │ sha256:7f83b1...   │
│ 1         │ Data (D2)  │ node-1b3e  │ AZ-b   │ sha256:a591a6...   │
│ 2         │ Data (D3)  │ node-2c5d  │ AZ-c   │ sha256:d7a8fb...   │
│ 3         │ Data (D4)  │ node-3d7c  │ AZ-a   │ sha256:ef2d12...   │
│ 4         │ Data (D5)  │ node-4e9b  │ AZ-b   │ sha256:b14361...   │
│ 5         │ Data (D6)  │ node-5fa1  │ AZ-c   │ sha256:c0535e...   │
│ 6         │ Data (D7)  │ node-6b23  │ AZ-a   │ sha256:2c624d...   │
│ 7         │ Data (D8)  │ node-7c45  │ AZ-b   │ sha256:fcde2b...   │
│ 8         │ Parity(P1) │ node-8d67  │ AZ-c   │ sha256:19581e...   │
│ 9         │ Parity(P2) │ node-9e89  │ AZ-a   │ sha256:3c9909...   │
│ 10        │ Parity(P3) │ node-0f01  │ AZ-b   │ sha256:af2bdb...   │
└───────────┴────────────┴────────────┴────────┴─────────────────────┘

This chunk map is stored in S3's metadata/index layer (a separate, highly
durable distributed database — itself replicated across AZs).

When the object is read, the metadata layer is queried first to obtain
the chunk map, then chunks are fetched from the indicated nodes.
```

---

## 14. Disk Failure Handling

### 14.1 Detection

Disk failures are detected through multiple channels:

```
Detection Method        │ Latency        │ Reliability
════════════════════════╪════════════════╪════════════════════════════
I/O errors              │ Immediate      │ High — OS reports to app
(read/write failure)    │                │
SMART monitoring        │ Proactive      │ Medium — not all failures
(predictive)            │ (hours/days)   │ are predicted
Heartbeat timeout       │ 30s - 1 min    │ High — detects node-level
(node-level)            │                │ issues that affect disk
Scrub mismatch          │ Up to 14 days  │ High — catches silent
(bit rot)               │                │ corruption
Latency monitoring      │ Seconds        │ Medium — abnormally slow
(degraded disk)         │                │ reads may indicate failing
```

### 14.2 Disk Failure Event Processing

```
Timeline of a Disk Failure Event:

  T+0s:     Disk /dev/sdg on Node-42 fails (head crash)
            └─ OS logs I/O errors
            └─ Storage daemon detects write failures

  T+1s:     Storage daemon marks /dev/sdg as FAILED locally
            └─ All chunks on /dev/sdg marked UNAVAILABLE
            └─ New writes are redirected to other disks on Node-42
            └─ Sends event to Repair Coordinator:
               "DISK_FAILURE: node=42, disk=sdg, chunks_affected=16234567"

  T+5s:     Repair Coordinator receives event
            └─ Looks up all objects that have chunks on Node-42:/dev/sdg
            └─ For each affected object:
               - Count remaining healthy chunks
               - Assign repair priority (see Section 12.4)
               - Queue repair task

  T+10s:    Repair begins (CRITICAL priority objects first)
            └─ Objects with only 8 healthy chunks are repaired immediately
            └─ Source chunks read from other nodes
            └─ Missing chunks reconstructed via erasure decoding
            └─ New chunks written to different nodes

  T+30min:  CRITICAL priority objects fully repaired

  T+2h:     HIGH priority objects fully repaired

  T+6h:     All objects fully repaired (MTTR target met)
            └─ Fleet is back to full durability
            └─ Total repair work: ~16M chunks reconstructed

  T+24h:    Operations team replaces the failed disk
            └─ New disk formatted and added to Node-42's pool
            └─ Node-42 back to full capacity
```

### 14.3 Disk Failure at Fleet Scale

```
Daily disk failure impact (fleet of 10M disks):

  Disks failing per day:         ~548
  Chunks affected per day:       ~548 x 16M = 8.77 billion
  Objects affected per day:      ~8.77B / 11 = ~797 million unique objects
                                 (each object has 11 chunks, so ~797M objects
                                  lose exactly 1 chunk each from disk failure)

  Repair tasks generated/day:    ~8.77 billion
  Total repair read I/O/day:     8.77B x 8 MB = 70.2 EB (distributed across fleet)
  Per-node repair read I/O/day:  70.2 EB / 200K nodes = 351 GB/node/day = ~4 MB/s
  Total repair write I/O/day:    8.77B x 1 MB = 8.77 PB
  Per-node repair write I/O/day: 8.77 PB / 200K nodes = 43.8 GB/node/day = ~0.5 MB/s

  Network: repair reads cross AZ boundaries
    Cross-AZ bandwidth for repair: significant but manageable
    S3 reserves dedicated bandwidth for repair traffic
```

### 14.4 Predictive Disk Replacement

```
SMART-Based Prediction:

  Monitored SMART attributes:
    - Reallocated Sector Count: sectors remapped due to errors
    - Current Pending Sector Count: sectors waiting to be remapped
    - Uncorrectable Sector Count: sectors that could not be read
    - Temperature: thermal stress
    - Power-On Hours: age indicator

  Prediction logic:
    IF reallocated_sectors > threshold (e.g., 100)
    OR pending_sectors > threshold (e.g., 10)
    OR uncorrectable_sectors > 0:
      → Mark disk as DEGRADED
      → Begin preemptive chunk migration:
         For each chunk on this disk:
           1. Read chunk from degraded disk (while it still works)
           2. Write chunk to a healthy disk (same node or different node)
           3. Update chunk map in metadata
      → No erasure decoding needed (chunk data is still readable)
      → After all chunks migrated: decommission disk

  This avoids the expensive erasure decode step by migrating while
  the disk is still readable. Preemptive migration costs 1x read + 1x write
  per chunk, vs 8x read + 1x write + decode for post-failure repair.
```

---

## 15. AZ Failure Handling

### 15.1 What Constitutes an AZ Failure

An Availability Zone failure means an entire AZ becomes unreachable. Causes
include:

- Utility power failure to the AZ campus (despite generator backups)
- Catastrophic cooling system failure
- Major network partition isolating the AZ
- Natural disaster (flood, earthquake, severe weather)

These are rare (roughly once every few years per AZ) but must be survivable.

### 15.2 Impact Assessment

```
With (8,3) EC and 4-4-3 distribution across 3 AZs:

  Case 1: AZ with 3 chunks goes down
    Chunks lost: 3
    Chunks remaining: 8 (in other 2 AZs)
    Remaining = k → OBJECT IS READABLE (barely)
    Durability: ZERO margin — one more chunk failure = data loss

  Case 2: AZ with 4 chunks goes down
    Chunks lost: 4
    Chunks remaining: 7 (in other 2 AZs)
    Remaining < k → OBJECT IS UNREADABLE
    DATA LOSS for this distribution ← THIS IS UNACCEPTABLE

Resolution: S3 must either:
  (a) Ensure no AZ holds more than m=3 chunks → 3-4-4 with strict cap
  (b) Use (8,4) EC (12 chunks, 4 parity) → any 4 lost chunks OK
  (c) Use a different AZ distribution strategy
```

Most likely, S3 uses approach (b) or a variant — enough parity chunks that
losing an entire AZ (up to ceil(n/3) chunks) is survivable.

### 15.3 AZ Failure Response

Assuming the system survives the AZ loss (enough chunks remain for all objects):

```
AZ Failure Response Timeline:

  T+0:       AZ-a becomes unreachable
             └─ Monitoring detects: all nodes in AZ-a offline
             └─ Customer reads continue from AZ-b and AZ-c
                (degraded: some objects need erasure decoding)

  T+5min:    Assessment: Is this a full AZ failure or transient?
             └─ Wait for confirmation before triggering mass repair
             └─ During this window: objects are in reduced-durability state

  T+15min:   Confirmed AZ failure: begin repair
             └─ For every object with chunks in AZ-a:
                - 3-4 chunks are missing (in AZ-a)
                - Need to reconstruct and place them in AZ-b and AZ-c
             └─ This is a MASSIVE operation

  T+15min    Mass repair phase:
  to         └─ Repair coordinator processes trillions of chunk repairs
  T+??:      └─ Prioritized: objects with fewest remaining chunks first
             └─ New chunks placed on nodes in AZ-b and AZ-c
             └─ Enormous cross-AZ bandwidth consumption

  Scale of repair:
    Assume AZ-a held 1/3 of all chunks
    Total objects in region: 10 trillion
    Objects affected: ~10 trillion (virtually all)
    Chunks to repair per object: 3-4
    Total chunk repairs: ~35 trillion
    At 1 MB per chunk: 35 exabytes of reconstructed data
    Repair bandwidth: limited by cross-AZ network capacity
    Estimated time to full repair: DAYS to WEEKS
```

### 15.4 AZ Recovery

```
Scenario A: AZ comes back online (transient failure)

  ┌─────────────────────────────────────────────────────────┐
  │  AZ-a was offline for 2 hours, then comes back          │
  │                                                         │
  │  Chunks in AZ-a are still on their disks.               │
  │  But are they TRUSTWORTHY?                              │
  │                                                         │
  │  Answer: NO, not automatically.                         │
  │  The AZ may have experienced data corruption during     │
  │  the failure (power loss can corrupt in-flight writes). │
  │                                                         │
  │  Process:                                               │
  │    1. Mark all chunks in AZ-a as UNVERIFIED              │
  │    2. Emergency scrub: verify every chunk via checksum   │
  │    3. Chunks that pass: mark as healthy, cancel repair   │
  │    4. Chunks that fail: repair normally                  │
  │    5. Gradually restore AZ-a to normal operation         │
  └─────────────────────────────────────────────────────────┘

Scenario B: AZ is permanently lost (natural disaster)

  ┌─────────────────────────────────────────────────────────┐
  │  AZ-a is destroyed. Data is gone.                       │
  │                                                         │
  │  All chunks that were in AZ-a must be reconstructed     │
  │  and placed on nodes in AZ-b and AZ-c.                  │
  │                                                         │
  │  Long-term: new AZ-d is provisioned to replace AZ-a.   │
  │  Chunks are gradually rebalanced across 3 AZs again.    │
  │                                                         │
  │  During the single-AZ period (weeks to months):         │
  │    - Durability is reduced (only 2 AZs)                 │
  │    - Extra parity may be temporarily added               │
  │    - Operational vigilance is heightened                  │
  └─────────────────────────────────────────────────────────┘
```

### 15.5 Design Lesson: Why AZ Independence Matters

```
AZs are designed to have INDEPENDENT failure modes:

  ┌────────────────────────────────────────────────────────────────┐
  │                                                                │
  │  AZ-a:                    AZ-b:                    AZ-c:       │
  │  ┌────────────────┐      ┌────────────────┐      ┌──────────┐ │
  │  │ Own power grid │      │ Own power grid │      │ Own power│ │
  │  │ Own cooling    │      │ Own cooling    │      │ Own cool │ │
  │  │ Own network    │      │ Own network    │      │ Own net  │ │
  │  │ Own building   │      │ Own building   │      │ Own bldg │ │
  │  │                │      │                │      │          │ │
  │  │ Separated by   │      │ Separated by   │      │          │ │
  │  │ miles          │      │ miles          │      │          │ │
  │  └────────────────┘      └────────────────┘      └──────────┘ │
  │                                                                │
  │  Low-latency interconnect (< 2ms) between AZs:                │
  │  AZ-a ←──────→ AZ-b ←──────→ AZ-c ←──────→ AZ-a              │
  │                                                                │
  │  Key: AZs share NOTHING except network connectivity.           │
  │  An earthquake that destroys AZ-a should NOT affect AZ-b.      │
  │  A power grid failure affecting AZ-b should NOT affect AZ-c.   │
  └────────────────────────────────────────────────────────────────┘
```

This physical independence is what makes the "spread chunks across 3 AZs"
strategy meaningful. If AZs shared a power grid, a single power failure could
take out multiple AZs simultaneously — invalidating the independence assumption
in all our durability math.

---

## Summary: How S3 Achieves 11 Nines

```
┌─────────────────────────────────────────────────────────────────────────┐
│                                                                         │
│  LAYER 1: ERASURE CODING                                                │
│    → (8,3) or (8,4) Reed-Solomon codes                                  │
│    → Can tolerate 3-4 simultaneous chunk losses per object              │
│    → Storage overhead: only 1.375x - 1.5x (vs 3x for replication)      │
│    → Mathematical foundation: 16-17 nines under independent failures    │
│                                                                         │
│  LAYER 2: INTELLIGENT PLACEMENT                                         │
│    → Chunks spread across 3 AZs                                         │
│    → No two chunks on the same node or rack                             │
│    → Minimizes correlated failure risk                                   │
│    → Accounts for power/network topology                                │
│                                                                         │
│  LAYER 3: CONTINUOUS INTEGRITY CHECKING                                 │
│    → 4 layers of checksums (upload, chunk, reconstruction, scrub)       │
│    → Background scrubbing: full disk scan every 14 days                 │
│    → Detects silent corruption before it accumulates                    │
│                                                                         │
│  LAYER 4: RAPID REPAIR                                                  │
│    → MTTR target: 6 hours per chunk                                     │
│    → Prioritized: objects with fewer healthy chunks repaired first       │
│    → Continuous: hundreds of disk failures per day are routine           │
│    → Preemptive: SMART monitoring migrates chunks off suspect disks     │
│                                                                         │
│  LAYER 5: OPERATIONAL DISCIPLINE                                        │
│    → Canary deployments to catch software bugs                          │
│    → Automated safeguards against misconfiguration                      │
│    → Disaster recovery testing                                          │
│    → The gap from 16-17 nines to 11 nines is the safety margin          │
│      that accounts for human and software error                         │
│                                                                         │
│  RESULT: 99.999999999% durability — designed to never lose your data    │
│                                                                         │
└─────────────────────────────────────────────────────────────────────────┘
```

---

## Key Interview Talking Points

If asked about S3 durability in an interview, hit these points:

1. **11 nines = statistically lose 1 object per 10 billion per year.** At S3's
   scale of 100+ trillion objects, engineering must exceed even this.

2. **Erasure coding, not replication.** Reed-Solomon (8,3) or similar gives
   1.375x overhead (vs 3x) with higher durability. Can lose any 3 chunks.

3. **Chunks placed across 3 AZs.** No two chunks share a failure domain (node,
   rack, AZ). This converts correlated failures into independent ones.

4. **Continuous repair.** Disk failures happen every 2-3 minutes. Repair is
   automated, prioritized, and always running. MTTR ~6 hours.

5. **Defense in depth for integrity.** Four checksum layers: upload, per-chunk,
   reconstruction, and background scrubbing every 14 days.

6. **The math.** Show you can derive: p = AFR x MTTR/8760, then use binomial
   distribution for P(m+1 failures out of n). Result: ~16-17 nines for
   independent failures. S3 advertises 11 nines as a conservative bound
   accounting for correlated failures and operational risk.

---

## Cross-References

| Document | Description |
|---|---|
| [Interview Simulation](interview-simulation.md) | Full S3 system design interview walkthrough |
| S3 Storage Classes | How Standard, IA, Glacier use different EC parameters and storage tiers |
| S3 Metadata & Index Layer | How the chunk maps and object index are stored and queried |
| S3 Request Routing | How GET/PUT requests reach the right storage nodes |
| AWS re:Invent ARC403 | "Backing Up 100 Trillion Objects" — primary source for durability architecture |

---

*This document is part of the system design interview preparation series. All
durability calculations use simplified models for interview purposes. Actual S3
implementation details are proprietary to AWS.*
