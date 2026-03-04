# Deep Dive: Block Storage — Magic Pocket

> **Context:** Dropbox stores exabytes of data. The block storage layer (Magic Pocket) is the largest, most expensive, and most custom component in the architecture. Dropbox migrated from Amazon S3 to Magic Pocket in ~2015-2016, saving $74.6M over two years according to their S-1 filing.

---

## Opening

**Interviewer:**

Walk me through Dropbox's block storage architecture. Why did they build their own?

**Candidate:**

> The short answer: **economics**. At Dropbox's scale (3+ exabytes), S3 costs ~$70M/month. Own hardware costs ~$15M/month. The $55M/month savings justifies the enormous engineering investment.
>
> But it's not just cost — building Magic Pocket gave Dropbox control over erasure coding ratios, hardware selection, dedup integration, and performance optimizations impossible on a managed service.

---

## 1. Content-Addressable Storage (CAS)

**Candidate:**

> Magic Pocket is fundamentally a **content-addressable store** — blocks are stored by their SHA-256 hash, not by file path or any external identifier.
>
> ```
> ┌─────────────────────────────────────────────────────────┐
> │            CONTENT-ADDRESSABLE BLOCK STORE               │
> │                                                         │
> │   PUT(hash, data):                                      │
> │     if hash NOT in store:                                │
> │       store(hash → data)                                 │
> │       ref_count[hash] = 1                                │
> │     else:                                                │
> │       ref_count[hash] += 1   (dedup — skip storage)     │
> │                                                         │
> │   GET(hash) → data                                      │
> │                                                         │
> │   DELETE(hash):                                          │
> │     ref_count[hash] -= 1                                 │
> │     if ref_count[hash] == 0:                             │
> │       schedule_gc(hash)      (garbage collect after grace)│
> │                                                         │
> │   Key property: Two different files with identical content│
> │   share the same blocks. Content is the identity.        │
> └─────────────────────────────────────────────────────────┘
> ```
>
> **Why CAS?**
> - **Automatic dedup**: Identical blocks map to the same hash → stored once. No explicit dedup pass needed.
> - **Integrity verification**: Hash is both the identifier and the checksum. Retrieve a block, re-hash it — if hash doesn't match, the block is corrupt.
> - **Immutable**: A block's content never changes (the hash IS the content). Blocks are write-once. Simplifies replication, caching, and consistency.

---

## 2. Why Move Off S3?

**Interviewer:**

S3 is incredibly reliable. Why take on the risk of building your own storage?

**Candidate:**

> From Dropbox's S-1 filing: the migration to Magic Pocket saved **$74.6 million over two years** in infrastructure costs. Here's the full analysis:
>
> ### Cost comparison at Dropbox scale:
>
> | Factor | S3 | Magic Pocket |
> |--------|----|----|
> | **Storage cost/GB/month** | ~$0.023 (standard) | ~$0.005 (estimated, own hardware) |
> | **1 exabyte/month** | $23.6 million | $5.1 million |
> | **3 exabytes/month** | $70.8 million | $15.3 million |
> | **Annual savings** | — | **~$660 million** over S3 at 3 EB |
> | **Request cost** | $0.005 per 1000 PUTs, $0.0004 per 1000 GETs | Marginal cost (already paid for hardware) |
> | **Egress cost** | $0.09/GB out | Zero (own network) |
>
> ### Beyond cost — control:
>
> 1. **Custom erasure coding**: S3 uses its own internal encoding. Magic Pocket uses **LRC-(12,2,2)** — Locally Repairable Codes tuned specifically for Dropbox's access patterns and failure modes. This isn't possible on S3.
>
> 2. **Dedup integration**: S3 treats each object independently — no cross-object dedup. Magic Pocket integrates directly with the dedup layer, storing each unique block exactly once.
>
> 3. **Hardware optimization**: Magic Pocket uses SMR (Shingled Magnetic Recording) drives for density, custom server designs (7th generation, 2+ PB per server), and rack-scale engineering (20+ PB per rack). None of this is possible with S3.
>
> 4. **Performance**: Co-locating storage with compute in Dropbox's own data centers eliminates the network hop to AWS. Lower latency for block reads and writes.
>
> ### When to stay on S3:
>
> | Scale | Recommendation | Why |
> |-------|---------------|-----|
> | < 100 TB | S3 | Zero ops overhead, pay-per-use, infinite scale |
> | 100 TB - 10 PB | S3 (probably) | Building your own costs more in engineering than you save |
> | 10 PB - 100 PB | Evaluate | Breakeven zone — depends on engineering talent and growth trajectory |
> | > 100 PB | Build your own | Savings justify dedicated storage team |
> | > 1 EB (Dropbox) | Definitely build | Saving hundreds of millions per year |
>
> **Contrast with Netflix Open Connect**: Same pattern. Netflix replaced CloudFront (AWS CDN) with Open Connect (own CDN) for the same reason — at massive scale, own infrastructure is cheaper. Netflix serves ~15% of global internet traffic; Dropbox stores exabytes. Both outgrew AWS economics.

---

## 3. Magic Pocket Architecture

**Candidate:**

> ### Hardware design:
>
> ```
> ┌───────────────────────────────────────────────────┐
> │              MAGIC POCKET RACK                     │
> │              (~20+ PB per rack)                    │
> │                                                   │
> │  ┌─────────────────────────────────────────────┐  │
> │  │  Server (7th gen)  ~2+ PB per server        │  │
> │  │  ┌────┐ ┌────┐ ┌────┐ ┌────┐ ... ┌────┐   │  │
> │  │  │SMR │ │SMR │ │SMR │ │SMR │     │SMR │   │  │
> │  │  │16TB│ │16TB│ │16TB│ │16TB│     │16TB│   │  │
> │  │  └────┘ └────┘ └────┘ └────┘     └────┘   │  │
> │  │  ~100+ SMR drives per server               │  │
> │  │  + SSD cache for hot data                   │  │
> │  │  + CPU for erasure coding computation        │  │
> │  └─────────────────────────────────────────────┘  │
> │                                                   │
> │  ┌─────────────────────────────────────────────┐  │
> │  │  Server (7th gen)  ~2+ PB                   │  │
> │  │  ... same ...                                │  │
> │  └─────────────────────────────────────────────┘  │
> │                                                   │
> │  ... ~10 servers per rack ...                     │
> │                                                   │
> │  Top-of-rack switch                               │
> │  Power distribution                               │
> └───────────────────────────────────────────────────┘
> ```
>
> **SMR (Shingled Magnetic Recording) drives**: These drives overlap magnetic tracks like roof shingles, increasing density but making random writes slower (must rewrite the overlapped portion). Perfect for Magic Pocket because blocks are **write-once, read-many** — sequential write, random read. SMR's write penalty doesn't matter for a CAS store.
>
> **7th-generation servers**: Dropbox has iterated on server design 7 times. Each generation increases density (PB per server), improves power efficiency, and reduces cost per GB. The latest generation stores 2+ PB per server.

---

## 4. Erasure Coding: LRC-(12,2,2)

**Interviewer:**

Explain the erasure coding scheme. Why LRC instead of Reed-Solomon?

**Candidate:**

> ### Background: Replication vs Erasure Coding
>
> ```
> 3x Replication:
>   Block "hello" → copy1, copy2, copy3 (on different machines)
>   Storage overhead: 200% (store 3x the data)
>   Read: read from any 1 copy (fast)
>   Repair: copy from surviving replica (simple)
>   Tolerates: 2 simultaneous failures
>
> Reed-Solomon (6,3):
>   Block → split into 6 data fragments + 3 parity fragments = 9 fragments
>   Store 9 fragments on 9 different machines
>   Storage overhead: 50% (9/6 = 1.5x the data)
>   Read: need any 6 of 9 fragments (read from 6 machines)
>   Repair: compute missing fragment from any 6 others (CPU-intensive)
>   Tolerates: 3 simultaneous failures
> ```
>
> ### LRC — Locally Repairable Codes: LRC-(12,2,2)
>
> ```
> LRC-(12,2,2) scheme:
>
> Data: 12 data fragments (d0, d1, ..., d11)
> Global parity: 2 fragments (g0, g1) — computed from all 12 data fragments
> Local parity: 2 fragments (l0, l1) — each computed from 6 data fragments
>
> Total: 16 fragments stored across 16 machines
> Storage overhead: 16/12 = 1.33x (33% overhead)
>
> Fragment layout:
>   Group A: d0, d1, d2, d3, d4, d5, l0    (l0 = XOR of d0..d5)
>   Group B: d6, d7, d8, d9, d10, d11, l1  (l1 = XOR of d6..d11)
>   Global:  g0, g1                          (computed from all d0..d11)
>
> ┌─────────────────────────────────────────────────────┐
> │  LRC-(12,2,2) Fragment Distribution                  │
> │                                                      │
> │  Group A (7 fragments, 7 machines):                  │
> │  [d0] [d1] [d2] [d3] [d4] [d5] [l0]               │
> │                                                      │
> │  Group B (7 fragments, 7 machines):                  │
> │  [d6] [d7] [d8] [d9] [d10] [d11] [l1]             │
> │                                                      │
> │  Global parity (2 fragments, 2 machines):            │
> │  [g0] [g1]                                           │
> │                                                      │
> │  Total: 16 fragments on 16 machines                  │
> └─────────────────────────────────────────────────────┘
> ```
>
> ### Why LRC over Reed-Solomon?
>
> The key advantage: **local repair**.
>
> ```
> Scenario: machine holding d3 fails.
>
> Reed-Solomon (12,4) repair:
>   Must read 12 fragments from 12 different machines
>   Network cost: 12 × fragment_size
>   CPU cost: full RS decode
>
> LRC-(12,2,2) local repair:
>   d3 is in Group A. Read d0, d1, d2, d4, d5, l0 (6 fragments)
>   d3 = l0 XOR d0 XOR d1 XOR d2 XOR d4 XOR d5
>   Network cost: 6 × fragment_size (HALF of RS)
>   CPU cost: simple XOR (much faster than RS decode)
> ```
>
> **At Dropbox's scale, disk failures happen constantly.** With exabytes of data across millions of drives, multiple drives fail every day. Each repair requires reading fragments from other machines — the network cost of repair is significant. LRC reduces repair network traffic by ~50% compared to Reed-Solomon, which matters enormously at scale.
>
> ### Storage overhead comparison:
>
> | Scheme | Overhead | Repair network cost | Failure tolerance |
> |--------|----------|-------------------|------------------|
> | 3x Replication | 200% | 1x (copy from replica) | 2 failures |
> | Reed-Solomon (12,4) | 33% | 12x fragments | 4 failures |
> | **LRC-(12,2,2)** | **33%** | **6x fragments (local repair)** | **2 local + 2 global** |
> | Reed-Solomon (6,3) | 50% | 6x fragments | 3 failures |

---

## 5. Storage Tiers

**Candidate:**

> Not all data is accessed equally. Dropbox uses tiered storage to balance cost and performance:
>
> ```
> ┌──────────────────────────────────────────────────────────────┐
> │                    STORAGE TIERS                              │
> │                                                              │
> │  HOT TIER (~5% of data)                                     │
> │  ┌──────────────────────────────────┐                       │
> │  │  Recently uploaded blocks         │                       │
> │  │  Frequently accessed files        │                       │
> │  │  Storage: SSD + fast HDD          │                       │
> │  │  Encoding: 3x REPLICATION         │  ← Fast reads (any    │
> │  │  Read latency: < 10 ms            │     single replica)   │
> │  │  Cost: $$$$                        │                       │
> │  └──────────────────────────────────┘                       │
> │           │ (after 24-72 hours, if not accessed)             │
> │           ▼                                                  │
> │  WARM TIER (~25% of data)                                   │
> │  ┌──────────────────────────────────┐                       │
> │  │  Files accessed occasionally      │                       │
> │  │  Storage: HDD (standard)          │                       │
> │  │  Encoding: LRC-(12,2,2)           │  ← Balanced: lower    │
> │  │  Read latency: 10-50 ms           │     cost, reasonable  │
> │  │  Cost: $$                          │     latency           │
> │  └──────────────────────────────────┘                       │
> │           │ (after 30+ days without access)                  │
> │           ▼                                                  │
> │  COLD TIER (~70% of data)                                   │
> │  ┌──────────────────────────────────┐                       │
> │  │  Old versions, rarely accessed    │                       │
> │  │  Storage: SMR HDD (high density)  │                       │
> │  │  Encoding: Aggressive erasure     │  ← Cheapest storage,  │
> │  │  (e.g., LRC with higher ratio)    │     higher latency    │
> │  │  Read latency: 50-200 ms          │                       │
> │  │  Cost: $                           │                       │
> │  └──────────────────────────────────┘                       │
> │                                                              │
> │  Key insight: ~70% of data is rarely accessed (old file      │
> │  versions, photos from years ago). Storing it on the cheapest│
> │  tier with aggressive erasure coding saves enormously.       │
> └──────────────────────────────────────────────────────────────┘
> ```
>
> **Tier migration**: Blocks are automatically migrated between tiers based on access patterns. A recently uploaded block starts in the hot tier. If not accessed for 24-72 hours, it migrates to warm. After 30+ days, cold. If a cold block is accessed (user opens an old file), it's temporarily promoted back to hot.

---

## 6. Write Path

**Candidate:**

> ```
> Block Write Path:
>
> Client                  Intake Server           Storage Nodes
>   │                          │                      │
>   │── Upload block ─────────>│                      │
>   │   (4 MB, compressed)     │                      │
>   │                          │                      │
>   │                    1. Decompress                 │
>   │                    2. Verify SHA-256 hash        │
>   │                    3. Check: block already exists?│
>   │                       If yes → skip (dedup hit)  │
>   │                       If no → proceed            │
>   │                          │                      │
>   │                    4. Erasure encode:             │
>   │                       Split into 12 data frags   │
>   │                       Compute 2 global parity    │
>   │                       Compute 2 local parity     │
>   │                       Total: 16 fragments        │
>   │                          │                      │
>   │                    5. Write fragments to 16 nodes │
>   │                          │──── frag_0 ──────────>│ Node 0
>   │                          │──── frag_1 ──────────>│ Node 1
>   │                          │──── ...               │ ...
>   │                          │──── frag_15 ─────────>│ Node 15
>   │                          │                      │
>   │                    6. Wait for quorum ACK         │
>   │                       (e.g., 14 of 16 nodes ACK) │
>   │                          │                      │
>   │                    7. Record block in index:      │
>   │                       hash → [node_0:frag_0,     │
>   │                               node_1:frag_1, ...]│
>   │                          │                      │
>   │<── 200 OK (block stored)─│                      │
>   │                          │                      │
>   │   Durability: block is now erasure-coded across  │
>   │   16 machines in at least 2 failure domains.     │
>   │   Can survive up to 4 simultaneous node failures.│
> ```

---

## 7. Read Path

**Candidate:**

> ```
> Block Read Path:
>
> Client                  Read Server              Storage Nodes
>   │                          │                      │
>   │── GET block (hash) ─────>│                      │
>   │                          │                      │
>   │                    1. Look up block index:       │
>   │                       hash → fragment locations  │
>   │                          │                      │
>   │                    2. Read strategy:              │
>   │                       HOT tier: read from any    │
>   │                       1 replica (fast path)      │
>   │                       WARM/COLD: read 12 data    │
>   │                       fragments (minimum needed) │
>   │                          │                      │
>   │                          │──── read frag_0 ─────>│
>   │                          │──── read frag_1 ─────>│
>   │                          │──── ... (12 frags) ──>│
>   │                          │                      │
>   │                    3. Reassemble block from frags │
>   │                    4. Verify SHA-256 hash         │
>   │                       (detect corruption)        │
>   │                    5. Compress for transfer       │
>   │                          │                      │
>   │<── Block data ───────────│                      │
> ```
>
> **Hedged reads**: For latency-sensitive reads, the read server may issue parallel requests to more fragments than needed (e.g., read 14 fragments instead of 12). Use the first 12 responses. This eliminates tail latency from slow nodes, at the cost of extra network traffic.

---

## 8. Garbage Collection

**Interviewer:**

How do you safely delete blocks when they might still be referenced?

**Candidate:**

> Garbage collection in a content-addressable store is one of the **hardest correctness problems** in the system. A block incorrectly collected while still referenced = **permanent data loss**.
>
> ```
> Garbage Collection Protocol:
>
> Phase 1: MARK
>   Scan metadata service (Edgestore) for all block references.
>   Build a "live set" of all block hashes that are currently referenced
>   by any file version in any namespace.
>
>   This is a MASSIVE scan — trillions of metadata entries,
>   hundreds of billions of block references.
>   Runs as a background job, takes hours.
>
> Phase 2: SWEEP
>   Compare block index with live set.
>   Blocks NOT in the live set → candidates for deletion.
>
>   BUT: race condition! Between mark and sweep, new references
>   could have been created. So:
>
> Phase 3: GRACE PERIOD
>   Mark candidates with a "pending deletion" timestamp.
>   Wait a grace period (e.g., 7 days).
>   After grace period: re-check that the block is STILL unreferenced.
>   If still unreferenced → safe to delete.
>   If referenced again during grace period → cancel deletion.
>
> Phase 4: DELETE
>   Permanently remove block data from all storage nodes.
>   Remove from block index.
>   This is irreversible — if the reference count was wrong,
>   the data is GONE.
>
> Safety mechanisms:
>   - Grace period (7 days) catches most race conditions
>   - "Tombstone" records in block index (know a block was deleted)
>   - Audit trail (log all GC decisions for forensic analysis)
>   - Canary testing (run GC in dry-run mode first, verify results)
>   - Rate limiting (delete at most N blocks per hour — limits blast radius)
> ```
>
> **The nightmare scenario**: A bug in the reference counting causes a block's ref_count to be 0 when it should be 1. GC deletes the block. A user opens their file and gets a "file corrupt" error — blocks are missing. This is why Dropbox invests heavily in GC correctness:
> - Multiple verification passes before deletion
> - Long grace periods
> - Ability to halt GC instantly (kill switch)
> - Regular reconciliation between metadata and block storage

---

## 9. Integrity Verification (Scrubbing)

**Candidate:**

> Data corruption happens. Hard drives develop bad sectors (bit rot). Cosmic rays flip bits. Firmware bugs corrupt data silently. Magic Pocket runs continuous **scrubbing** to detect and repair corruption:
>
> ```
> Scrubbing Process:
>
> For each block in the store (background, continuous):
>   1. Read all 16 fragments from storage nodes
>   2. Verify each fragment's local checksum
>   3. Reassemble the block from 12 data fragments
>   4. Compute SHA-256 of reassembled block
>   5. Compare with stored block hash (the block's key)
>
>   If match → block is healthy. Move to next block.
>   If mismatch → corruption detected!
>
>   Corruption repair:
>   1. Identify which fragment(s) are corrupted
>      (try assembling from different subsets of fragments)
>   2. Recompute corrupted fragment from healthy fragments
>      (erasure coding's whole purpose)
>   3. Write repaired fragment to a new storage node
>   4. Log the corruption event for analysis
>
> Scrub cycle: complete scan of all data every ~2 weeks
> (at exabyte scale, this means reading petabytes per day)
> ```
>
> **This is the same concept as ZFS scrub** — periodically verify all data against checksums and repair from redundancy. The difference: ZFS operates within a single machine, Magic Pocket scrubs across hundreds of thousands of machines.

---

## Contrast: Magic Pocket vs S3 vs Colossus vs HDFS

| Aspect | Magic Pocket | Amazon S3 | Google Colossus | HDFS |
|--------|-------------|-----------|-----------------|------|
| **Operator** | Dropbox | Amazon (managed) | Google (internal) | Open source |
| **Scale** | 3+ exabytes | Exabytes+ (multi-tenant) | Exabytes+ | Varies |
| **Encoding** | LRC-(12,2,2) | Unknown (internal) | Reed-Solomon | 3x replication (default) |
| **Storage overhead** | ~33% | Unknown | ~33-50% | 200% (3x) |
| **Durability target** | 12 nines | 11 nines | Not published | Depends on replication |
| **Content addressing** | Yes (SHA-256 CAS) | No (key-value) | No | No |
| **Cross-file dedup** | Yes (core feature) | No | No | No |
| **Custom hardware** | Yes (SMR drives, custom servers) | Yes (but opaque to users) | Yes | Commodity hardware |
| **Cost model** | Fixed (own hardware) | Pay-per-GB + per-request | Internal allocation | Own hardware |
| **Who should use** | >100 PB, CAS needed | Everyone else | Google-internal | Hadoop workloads |

---

## L5 vs L6 vs L7 — Block Storage Rubric

| Aspect | L5 (SDE-2) | L6 (SDE-3) | L7 (Principal) |
|--------|------------|------------|-----------------|
| **Storage choice** | "Use S3 for storage" | Explains when to use S3 vs build own, calculates breakeven | Cites $74.6M savings from S-1, explains Netflix Open Connect parallel, discusses total cost of ownership (hardware + power + ops team) |
| **Encoding** | "Replicate for durability" | Explains erasure coding vs replication trade-offs, knows Reed-Solomon | Explains LRC specifically, why local repair matters (50% less repair traffic), calculates storage overhead (33% vs 200%) |
| **Tiers** | "Store data on disk" | Designs hot/warm/cold tiers, explains access-pattern-based migration | Explains SMR drives for cold tier, SSD caching for hot, calculates that 70% of data is cold (drives tier ratios) |
| **GC** | "Delete unused blocks" | Designs mark-and-sweep with reference counting | Explains GC correctness challenges, grace periods, the nightmare scenario (false zero ref_count), kill switch mechanism |
| **Integrity** | "Use checksums" | Designs scrubbing process with periodic verification | Explains bit-rot at exabyte scale (guaranteed to happen), repair from erasure coding, scrub cycle timing, ZFS analogy |

---

> **Summary:** Magic Pocket is Dropbox's most impressive infrastructure feat — an exabyte-scale, content-addressable block store that replaced Amazon S3 and saves hundreds of millions of dollars per year. It uses LRC-(12,2,2) erasure coding (33% overhead vs 200% for replication), custom hardware with SMR drives (2+ PB per server, 20+ PB per rack), three storage tiers (hot/warm/cold), and continuous scrubbing for integrity. The content-addressable design integrates directly with block-level dedup, making identical blocks across all users automatically stored once. Building Magic Pocket is the right choice at exabyte scale — but for any company below hundreds of petabytes, S3 remains the correct answer.
