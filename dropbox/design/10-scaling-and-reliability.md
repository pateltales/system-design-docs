# Deep Dive: Scaling & Reliability

> **Context:** Cross-cutting doc that ties together scaling decisions from all deep dives. Dropbox operates at exabyte scale with 700M+ users, handling 75B+ API calls per month across custom-built infrastructure.

---

## Opening

**Interviewer:**

Let's talk about scale. How does this system handle Dropbox-level traffic?

**Candidate:**

> Let me start with the verified scale numbers, then break down each subsystem's scaling approach.

---

## 1. Scale Numbers

**Candidate:**

> | Metric | Value | Source |
> |--------|-------|--------|
> | **Registered users** | 700M+ | Dropbox public filings |
> | **Paying subscribers** | 18.2M | Dropbox S-1/annual reports |
> | **Daily active users** | ~70M (estimated 10% DAU ratio) | Industry standard DAU/MAU |
> | **Data stored** | 3+ exabytes | Dropbox engineering blog |
> | **Content pieces tracked** | 550B+ | Dropbox engineering blog |
> | **API calls/month** | 75B+ | Dropbox engineering blog |
> | **Magic Pocket servers** | 7th gen, 2+ PB/server, 20+ PB/rack | Dropbox engineering blog |
> | **Edgestore entries** | Trillions | Dropbox engineering blog |
> | **Edgestore QPS** | Millions | Dropbox engineering blog |
> | **Edgestore cache hit rate** | 95% | Dropbox engineering blog |
> | **Cross-shard transactions** | 10M/sec | Dropbox engineering blog |
> | **S3 migration savings** | $74.6M over 2 years | Dropbox S-1 filing |
> | **Durability target** | 12 nines (99.9999999999%) | Dropbox engineering blog |

---

## 2. Back-of-Envelope Calculations

**Candidate:**

> ### File operations throughput:
>
> ```
> 75B API calls/month ÷ 30 days ÷ 86,400 sec = ~29,000 API calls/sec
>
> Estimated breakdown:
>   - Metadata reads (list_folder, get_metadata): ~60% = ~17,400/sec
>   - Sync polls (longpoll + continue):            ~20% = ~5,800/sec
>   - File uploads (chunked sessions):              ~10% = ~2,900/sec
>   - File downloads:                               ~5%  = ~1,450/sec
>   - Sharing/admin operations:                     ~5%  = ~1,450/sec
> ```
>
> ### Block storage throughput:
>
> ```
> Assume 1.2B files synced/day:
>   1.2B files ÷ 86,400 sec = ~14,000 file operations/sec
>
> Average file: ~100 MB → 25 blocks (at 4 MB each)
>   14,000 files/sec × 25 blocks = ~350,000 block operations/sec
>
> Dedup means ~40-50% of blocks already exist:
>   350,000 × 0.5 = ~175,000 new block writes/sec
>
> Each new block: ~4 MB (before compression)
>   175,000 × 4 MB = ~700 GB/sec raw block writes
>   With compression (~50%): ~350 GB/sec actual write throughput
>
> That's ~30 PB of new data per day!
> (Most of which is deduplicated — actual new unique data is much less)
> ```
>
> ### Metadata polling load:
>
> ```
> 70M DAU, each polls every ~60 seconds:
>   70,000,000 / 60 = ~1,170,000 polls/sec
>
> Each poll:
>   - Check namespace cursors (cached, ~1ms)
>   - Hold connection 30-90 seconds (idle)
>   - Response: ~20 bytes
>
> Concurrent open connections:
>   70M × (avg hold ~45s / 60s interval) ≈ 52.5M connections
>
> At 100K connections per notification server:
>   52.5M / 100K = ~525 notification servers
> ```
>
> ### Storage cost comparison:
>
> ```
> 3 exabytes = 3,000,000,000 GB
>
> On S3 (standard, $0.023/GB/month):
>   3,000,000,000 × $0.023 = $69,000,000/month = ~$828M/year
>
> On Magic Pocket (estimated $0.005/GB/month — own hardware):
>   3,000,000,000 × $0.005 = $15,000,000/month = ~$180M/year
>
> Annual savings: ~$648M/year
>
> (These are simplified — real costs include networking, power, cooling,
>  operations team, hardware refresh cycles. But the order of magnitude
>  savings is why Dropbox built Magic Pocket.)
> ```

---

## 3. Metadata Scaling (Edgestore)

**Candidate:**

> ### MySQL Sharding
>
> ```
> ┌────────────────────────────────────────────────────────┐
> │  EDGESTORE MYSQL SHARDING                              │
> │                                                        │
> │  Total: ~thousands of MySQL shards                     │
> │  Shard key: namespace_id                               │
> │  Each shard: 1 primary + 2 replicas (cross-AZ)        │
> │                                                        │
> │  Query routing:                                        │
> │    namespace_id → hash → shard_number → MySQL host     │
> │                                                        │
> │  Per-shard load:                                       │
> │    millions total QPS × 5% (cache miss) ÷ thousands    │
> │    of shards = ~50-100 QPS per shard                  │
> │    (easily within MySQL capacity)                      │
> │                                                        │
> │  Read replica usage:                                   │
> │    - Reads from replicas for non-critical queries      │
> │    - Writes always go to primary                       │
> │    - Sync-critical reads go to primary (strong         │
> │      consistency requirement)                          │
> └────────────────────────────────────────────────────────┘
> ```
>
> ### Hot Shard Mitigation
>
> ```
> Problem: A shared folder with 10,000 members generates
>          disproportionate traffic on one shard.
>
> Mitigations:
> 1. Dedicated hardware: Hot shards get faster machines
>    (more RAM, faster SSDs for MySQL storage)
>
> 2. Read replicas: Add more replicas for read-heavy hot shards
>    (directory listings for popular shared folders)
>
> 3. Aggressive caching: Cache directory listings, ACLs,
>    member lists for hot namespaces (already 95% cache hit)
>
> 4. Namespace splitting: [rare, complex] Split a very large
>    namespace into sub-namespaces on different shards.
>    Requires careful data migration.
>
> 5. Rate limiting: Throttle non-critical operations on hot
>    namespaces (e.g., search indexing, audit logging)
> ```
>
> ### Caching Layer
>
> ```
> Cache architecture:
>
>   Client → Load Balancer → App Server → Cache (95% hit) → MySQL (5%)
>
>   Cache: Memcached or Redis cluster
>   Hit rate: 95% (verified from Dropbox engineering blog)
>   Cache key patterns:
>     file:{file_id} → metadata JSON
>     dir:{ns}:{parent_id} → directory listing
>     ns:{ns_id} → namespace metadata + ACL
>     cursor:{ns_id} → latest cursor position
>
>   Impact:
>     Total QPS: millions
>     Cache-served: 95% × millions = vast majority
>     MySQL-served: 5% × millions ÷ thousands of shards = manageable
> ```

---

## 4. Block Storage Scaling (Magic Pocket)

**Candidate:**

> ```
> Scaling strategy:
>
> 1. HORIZONTAL: Add more storage servers and racks
>    Each rack: 20+ PB. Need more capacity? Add racks.
>    Hardware procurement cycles: order 6 months ahead of need.
>
> 2. DENSITY: Each server generation stores more per unit
>    7th gen: 2+ PB per server (up from ~500 TB in earlier gens)
>    SMR drives: higher density per drive
>
> 3. EFFICIENCY: Erasure coding (LRC) reduces overhead
>    33% overhead vs 200% for 3x replication
>    At 3 EB: saves ~4 EB of raw storage capacity
>
> 4. TIERING: Hot/warm/cold tiers match cost to access pattern
>    70% cold data on cheapest storage (aggressive erasure coding)
>    5% hot data on fast storage (replication for speed)
>
> Capacity planning:
>   - Monitor: storage utilization per tier, growth rate
>   - Forecast: linear extrapolation of growth + seasonal adjustment
>   - Threshold: order new hardware when projected to hit 70% capacity
>   - Lead time: 6-12 months from order to production
>   - Buffer: always maintain 30% headroom for burst growth
> ```

---

## 5. Notification System Scaling

**Candidate:**

> ```
> Architecture for 1.17M polls/sec:
>
> ┌──────────┐     ┌───────────────┐     ┌──────────────────────────┐
> │ Clients  │────>│ Load Balancer  │────>│ Notification Servers     │
> │ (70M DAU)│     │ (L4/L7)       │     │ (~525 servers)           │
> │          │     │               │     │ 100K connections each    │
> └──────────┘     └───────────────┘     └────────────┬─────────────┘
>                                                      │
>                                                      │ Subscribe to
>                                                      │ namespace changes
>                                                      ▼
>                                        ┌──────────────────────────┐
>                                        │ Message Bus (Kafka)       │
>                                        │                          │
>                                        │ Topics: per-namespace    │
>                                        │ or namespace-range       │
>                                        │                          │
>                                        │ "NS 2001 changed at     │
>                                        │  cursor 3206"           │
>                                        └──────────────────────────┘
>                                                      ▲
>                                                      │
>                                        ┌──────────────────────────┐
>                                        │ API Servers              │
>                                        │ (on metadata commit,     │
>                                        │  publish to message bus) │
>                                        └──────────────────────────┘
>
> Key scaling properties:
>   - Notification servers are STATELESS (any server handles any poll)
>   - No sticky sessions (load balancer distributes freely)
>   - Message bus handles the "which server has a client for NS X?" problem
>   - Each notification server subscribes to the namespace ranges it's serving
>   - Adding servers = adding capacity linearly
> ```

---

## 6. Multi-Data Center Architecture

**Candidate:**

> ```
> ┌────────────────────────────────────────────────────────────────┐
> │  MULTI-DC ARCHITECTURE                                         │
> │                                                                │
> │  DC-WEST (Primary for metadata)        DC-EAST (Secondary)     │
> │  ┌──────────────────────┐              ┌──────────────────┐   │
> │  │ MySQL Primary Shards │──── async ──>│ MySQL Replicas   │   │
> │  │ (write + read)       │   replication│ (read-only)      │   │
> │  │                      │              │                  │   │
> │  │ Notification Servers │              │ Notification     │   │
> │  │ API Servers          │              │ Servers          │   │
> │  │ Cache (Memcached)    │              │ API Servers      │   │
> │  └──────────────────────┘              │ Cache            │   │
> │                                        └──────────────────┘   │
> │  ┌──────────────────────┐              ┌──────────────────┐   │
> │  │ Magic Pocket         │              │ Magic Pocket     │   │
> │  │ (erasure-coded       │              │ (erasure-coded   │   │
> │  │  fragments)          │              │  fragments)      │   │
> │  └──────────────────────┘              └──────────────────┘   │
> │                                                                │
> │  Metadata: Active-Passive (write to primary DC, read from both)│
> │  Blocks: Active-Active (erasure-coded fragments across BOTH DCs)│
> │  Failover: Promote secondary MySQL replicas → new primary      │
> │  RPO: seconds (async replication lag)                          │
> │  RTO: minutes (failover + cache warm-up)                      │
> └────────────────────────────────────────────────────────────────┘
> ```
>
> **Why active-passive for metadata (not active-active)?**
>
> Active-active metadata would require **synchronous cross-DC replication** for every write — adding 10-50ms latency (cross-country network round trip) to every file create, edit, delete, and move. At 14,000 file operations/sec, this is prohibitive.
>
> Instead, Dropbox uses **active-passive with fast failover**:
> - Normal operation: all writes go to primary DC, reads from both
> - Replication: async MySQL replication (seconds of lag)
> - Failover: promote secondary, accept brief data loss (RPO = seconds of lag)
> - Recovery: reconcile any divergent writes after failover
>
> **Block storage is active-active**: Erasure-coded fragments are distributed across both DCs. As long as enough fragments are reachable (12 of 16 for LRC-(12,2,2)), blocks are available regardless of which DC has an outage.

---

## 7. Reliability & Durability

**Candidate:**

> ### 12 Nines of Durability (99.9999999999%)
>
> ```
> What does 12 nines mean?
>   At 550B content pieces:
>   Expected data loss per year: 550B × 10^-12 = 0.55 objects
>   ≈ less than 1 object lost per year across ALL of Dropbox
>
> How is this achieved?
>   1. Erasure coding: LRC-(12,2,2) can tolerate 4 simultaneous
>      fragment failures without data loss
>   2. Geographic distribution: fragments across multiple DCs
>   3. Background scrubbing: detect and repair corruption before
>      it accumulates
>   4. Rapid repair: when a drive/node fails, immediately reconstruct
>      missing fragments from surviving ones
>   5. Monitoring: continuous health checks on every fragment
> ```
>
> ### Failure Modes and Responses
>
> | Failure | Frequency | Impact | Response |
> |---------|-----------|--------|----------|
> | **Single drive failure** | Multiple per day (at this scale) | 1-2 fragments of many blocks unavailable | Automatic repair from parity/replicas within minutes |
> | **Storage node failure** | Weekly | Hundreds of fragments unavailable | Reconstruct from erasure coding, rebalance to new node |
> | **Rack failure** | Monthly | Thousands of fragments unavailable | Fragments distributed across racks — data still available from other racks |
> | **Network partition** | Rare | DC partially unreachable | Route traffic to healthy DC, queue writes |
> | **DC failure** | Very rare | Half of infrastructure down | Failover: promote secondary metadata, block reads from surviving fragments |
> | **Silent data corruption** | Constant (bit rot at this scale) | Block data corrupted without error | Scrubbing detects, erasure coding repairs |
> | **Metadata corruption** | Extremely rare | File "exists" but blocks are wrong | Checksums detect, restore from backup/replica |
>
> ### Scrubbing cadence:
>
> ```
> Total data: 3 exabytes (with erasure coding overhead: ~4 EB raw)
> Scrub cycle: every 2 weeks
> Required read rate: 4 EB / 14 days / 86,400 sec ≈ 3.3 TB/sec
> (distributed across thousands of storage nodes, this is feasible)
> ```

---

## 8. Monitoring & Alerting

**Candidate:**

> ### Key Metrics Dashboard:
>
> ```
> BLOCK STORAGE:
>   - Durability: fragments healthy / fragments total
>   - Corruption rate: corrupted blocks detected per hour
>   - Repair rate: blocks repaired per hour (must exceed corruption rate!)
>   - Capacity utilization: per tier (hot/warm/cold)
>   - Write throughput: GB/sec by tier
>   - Read latency: p50, p95, p99 by tier
>
> METADATA (EDGESTORE):
>   - Query latency: p50, p95, p99
>   - QPS: total, per-shard max
>   - Cache hit rate: overall, per key type
>   - Replication lag: primary → secondary DC
>   - Cross-shard transaction rate and latency
>   - Hot shard detection: shards exceeding N QPS
>
> SYNC:
>   - Sync latency: time from file edit to notification delivery (p50, p95)
>   - Upload latency: time from file change to blocks stored
>   - Download latency: time from notification to file written locally
>   - Conflict rate: conflicted copies created / total syncs
>   - Dedup hit rate: blocks skipped / total blocks in uploads
>
> NOTIFICATION:
>   - Active longpoll connections: total
>   - Poll response latency: time from change to longpoll response
>   - Message bus lag: events pending delivery
>   - Backoff rate: how often servers issue backoff to clients
>
> BUSINESS:
>   - Files synced per day
>   - New users / churned users
>   - Storage growth rate
>   - API error rate by endpoint
> ```
>
> ### Alerting philosophy:
>
> ```
> Critical (page on-call immediately):
>   - Durability below threshold (any permanent data loss risk)
>   - Metadata replication lag > 30 seconds
>   - Sync latency p95 > 5 minutes
>   - Error rate > 1% on any critical API endpoint
>
> Warning (investigate within hours):
>   - Cache hit rate drops below 90%
>   - Hot shard detected (> 2x average QPS)
>   - Storage capacity forecast: < 30 days to full on any tier
>   - Scrubbing falling behind schedule
>
> Info (review in next business day):
>   - Dedup ratio trending down (more unique data than expected)
>   - API latency p99 increasing trend
>   - Hardware failure rate above baseline
> ```

---

## 9. Capacity Planning

**Candidate:**

> ```
> Planning cycle: quarterly, with monthly check-ins
>
> Inputs:
>   - Current utilization (storage, compute, network) per tier
>   - Growth rate (trailing 90-day linear + seasonal adjustment)
>   - Product roadmap (new features that change storage/compute patterns)
>   - Hardware refresh schedule (end-of-life servers to be replaced)
>
> Model:
>   projected_capacity_needed(month) =
>     current_usage +
>     (monthly_growth_rate × months_ahead) +
>     seasonal_factor(month) +
>     product_feature_impact
>
> Thresholds:
>   Order new hardware when:
>     projected utilization at T+9 months > 70%
>   (9 months = 6 months procurement + 3 months install/burn-in)
>
> Example:
>   Current storage: 3.0 EB (70% utilized → 4.3 EB raw)
>   Growth rate: 5% per quarter
>   Q1 next year forecast: 3.0 × 1.05^2 = 3.3 EB
>   Order 500 PB of new storage this quarter to handle growth
> ```

---

## Contrast: Dropbox vs Google vs WhatsApp Scaling

| Dimension | Dropbox | Google Drive | WhatsApp |
|-----------|---------|-------------|----------|
| **Primary bottleneck** | Metadata QPS (1M+ polls/sec) | Compute (OT/CRDT for real-time collab) | Connections (50M+ WebSocket) |
| **Storage** | Own (Magic Pocket, exabytes) | Own (Colossus, exabytes) | Minimal (messages deleted after delivery) |
| **Database** | MySQL (sharded, strong consistency) | Spanner (globally consistent) | Cassandra (eventually consistent) |
| **Caching** | Memcached/Redis (95% hit for metadata) | Bigtable + Memcache | Mnesia (Erlang in-memory) |
| **Notification** | Long-polling (HTTP, stateless) | Hybrid (polling + push) | WebSocket (stateful, persistent) |
| **Custom infra** | Magic Pocket (storage) | Colossus, Spanner, Borg (everything) | Erlang/BEAM VM (connections) |
| **Cost driver** | Storage ($15M+/month) | Compute + storage | Bandwidth |

---

## L5 vs L6 vs L7 — Scaling Rubric

| Aspect | L5 (SDE-2) | L6 (SDE-3) | L7 (Principal) |
|--------|------------|------------|-----------------|
| **Numbers** | "Lots of users and data" | Quantifies: 14K file ops/sec, 1.17M polls/sec, 175K block writes/sec | Calculates storage cost savings ($648M/year over S3), models capacity planning, forecasts growth |
| **Metadata** | "Shard the database" | Designs MySQL sharding by namespace, calculates per-shard QPS, 95% cache hit | Designs hot shard mitigation (dedicated hardware, read replicas, rate limiting), explains cross-shard transaction scaling at 10M/sec |
| **Multi-DC** | "Replicate across data centers" | Designs active-passive metadata, active-active blocks | Explains RPO/RTO trade-offs, why not active-active metadata (cross-DC latency at 14K writes/sec), reconciliation after failover |
| **Monitoring** | "Monitor everything" | Lists key metrics: sync latency, durability, cache hit rate | Designs alerting philosophy (critical/warning/info tiers), explains why repair rate must exceed corruption rate |
| **Reliability** | "12 nines durability" | Explains erasure coding tolerance, scrubbing for bit-rot | Calculates scrub read rate (3.3 TB/sec), models expected data loss (< 1 object/year at 550B objects) |

---

> **Summary:** Dropbox operates at a scale where every subsystem requires careful engineering: metadata handles millions of QPS (95% from cache, 5% from thousands of MySQL shards), block storage manages exabytes across custom hardware (Magic Pocket, LRC erasure coding, tiered storage), notifications serve 1.17M polls/sec across ~525 stateless servers, and multi-DC architecture provides resilience with active-passive metadata and active-active block storage. The dominant cost is storage ($15M+/month on own hardware, would be $70M+/month on S3), which drove the most significant infrastructure investment: building Magic Pocket. Capacity planning works on 9-month horizons to account for hardware procurement lead times.
