# 10 - Scaling and Reliability

## Overview

Google Docs serves billions of users with sub-second collaboration latency. The scaling challenges are unique because the system is **stateful** (OT requires ordered, per-document state) and **real-time** (operations must round-trip in under 200ms for a good editing experience). This document breaks down the scale numbers, the architecture that handles them, and the reliability mechanisms that keep it running.

---

## 1. Scale Numbers

### 1.1 User and Document Scale

| Metric | Estimated Scale | Source/Reasoning |
|--------|----------------|------------------|
| Google Workspace users | 3B+ (total Google accounts) | Google public reports |
| Active Docs users (monthly) | 1B+ | Google Workspace has 3B+ users; Docs is a core product |
| Total documents stored | Tens of billions | Accumulation over 15+ years |
| Active documents (edited in last 30 days) | Hundreds of millions | ~10-20% of total |
| Concurrently edited documents (at any instant) | ~10-50M | Peak hours estimate |
| Peak concurrent editors per document | Up to 100 | Google's documented limit |
| Avg concurrent editors per active document | 1-3 | Most documents are solo-edited |

### 1.2 Operation Scale

| Metric | Estimated Scale | Derivation |
|--------|----------------|------------|
| Operations per second (global) | 50M+ | 10M concurrent sessions x 5 ops/sec avg typing |
| Operations per second (busy document, 50 editors) | 250 | 50 editors x 5 ops/sec |
| Operations per second (single editor) | 5-15 | Average typing speed + formatting |
| Operation size | 50-500 bytes | Insert: ~50B, Format: ~200B, Delete: ~50B |
| Total operation throughput | ~10-25 GB/s | 50M ops/s x 200B avg |
| Operation log writes per second | 50M+ | Every operation is persisted |

### 1.3 Storage Scale

| Metric | Estimated Scale | Derivation |
|--------|----------------|------------|
| Average document size (current snapshot) | 50-100 KB | Text + formatting metadata |
| Average operation log per document | 1-10 MB | Thousands to millions of operations over lifetime |
| Total snapshot storage | ~1 PB | 10B docs x 100KB avg |
| Total operation log storage | ~10-50 PB | 10B docs x 1-5MB avg op log |
| Daily new operation log data | ~100 TB | 50M ops/s x 200B x 86400s |

---

## 2. WebSocket Scaling

### 2.1 Connection Scale

```
100M+ concurrent WebSocket connections
(Users with documents open in browser tabs — many are idle)
```

### 2.2 Resource Calculation

```
Per-connection memory:
  - TCP buffer:        ~16 KB (kernel send + receive buffers, tuned down)
  - WebSocket state:    ~2 KB (session ID, auth token, doc_id, user info)
  - Application state: ~32 KB (pending ops buffer, cursor state, presence)
  ─────────────────────────────
  Total per connection: ~50 KB

For 100M connections:
  100,000,000 × 50 KB = 5,000,000,000 KB = ~5 TB total memory

Server capacity:
  Per gateway server: 128 GB RAM usable for connections
  Connections per server: 128 GB / 50 KB = ~2.5M connections
  Total servers needed: 100M / 2.5M = ~40 servers (minimum)

  With overhead (OS, GC, headroom):
  Realistic: 500K-1M connections per server
  Total servers: 100-200 Connection Gateway servers
```

### 2.3 Connection Gateway Architecture

```
                    Internet
                       │
            ┌──────────┼──────────┐
            │     Load Balancer    │
            │  (L4 - TCP sticky)  │
            └──────┬───┬───┬──────┘
                   │   │   │
        ┌──────────┤   │   ├──────────┐
        │          │   │   │          │
  ┌─────┴─────┐ ┌─┴───┴─┐ │   ┌─────┴─────┐
  │  Gateway   │ │Gateway│ │   │  Gateway   │
  │  Server 1  │ │Srv 2  │ │   │  Srv N     │
  │            │ │       │ │   │  (N=100+)  │
  │ 1M conns   │ │1M     │ │   │  1M conns  │
  └─────┬──────┘ └───┬───┘ │   └─────┬──────┘
        │            │     │         │
        └────────────┴─────┴─────────┘
                     │
              Internal Network
                     │
        ┌────────────┼────────────┐
        │            │            │
  ┌─────┴─────┐ ┌───┴───┐ ┌─────┴─────┐
  │ OT Server │ │  OT   │ │ OT Server │
  │  Pool A   │ │Pool B │ │  Pool C   │
  └───────────┘ └───────┘ └───────────┘
```

**Gateway server responsibilities:**
1. Terminate TLS + WebSocket handshake
2. Authenticate user (validate session cookie / OAuth token)
3. Route messages to the correct OT server based on `doc_id`
4. Buffer outbound messages (batch small ops to reduce syscalls)
5. Handle connection lifecycle (ping/pong, reconnection, clean disconnect)
6. **Do NOT perform OT** — gateways are stateless relay nodes

**Why separate gateways from OT servers?**
- Gateways are **stateless** and horizontally scalable — add more to handle more connections
- OT servers are **stateful** (hold per-document state) — scaling them is harder
- Separation allows independent scaling of connection handling vs. OT processing
- A gateway failure only drops connections (clients reconnect); an OT server failure requires state recovery

---

## 3. OT Server Scaling

### 3.1 The Per-Document Serialization Bottleneck

OT **requires** that all operations for a single document be processed in a **total order** by a **single thread** (or equivalent serialization). This is a fundamental constraint, not an implementation choice.

```
Why single-threaded per document?

  OT transformation: transform(op_a, op_b) depends on the ORDER of a and b.
  If two servers process ops for the same document concurrently,
  they may apply different orderings → divergent document states.

  Therefore: one document = one serialization point = one OT server.

  This is the fundamental scalability bottleneck of OT-based systems.
```

### 3.2 Document-to-Server Mapping

```
Document Routing:

  doc_id → hash(doc_id) % num_ot_servers → OT Server assignment

  ┌──────────────┐
  │  Routing      │     doc_abc → OT Server 42
  │  Service      │     doc_def → OT Server 17
  │  (Consistent  │     doc_ghi → OT Server 42  (same server, different doc)
  │   Hashing)    │     doc_jkl → OT Server 89
  └──────────────┘

  Each OT server handles thousands of active documents.
  Each document occupies one "slot" — a single-threaded processing queue.

  Per OT server:
    - CPU cores: 32-64
    - Active documents: ~5,000-20,000
    - Each document gets a coroutine/fiber, not a full thread
    - Documents processed via event loop (not thread-per-document)
```

### 3.3 OT Server State Per Document

```
┌─────────────────────────────────────────────────┐
│  In-Memory State for Document "doc_abc"          │
├─────────────────────────────────────────────────┤
│                                                  │
│  doc_id:          "doc_abc"                      │
│  current_revision: 847,293                       │
│  document_snapshot: <latest state, ~100KB>       │
│                                                  │
│  pending_ops_queue: [op1, op2, op3]              │
│  │   (operations received but not yet processed) │
│                                                  │
│  connected_sessions: [                           │
│    {user: "alice", gateway: "gw-17", rev: 847290}│
│    {user: "bob",   gateway: "gw-42", rev: 847293}│
│    {user: "carol", gateway: "gw-17", rev: 847291}│
│  ]                                               │
│                                                  │
│  cursor_positions: {                             │
│    "alice": {pos: 1042, selection: null},         │
│    "bob":   {pos: 587,  selection: [587, 612]},   │
│    "carol": {pos: 2201, selection: null}          │
│  }                                               │
│                                                  │
│  last_activity: 2025-01-15T10:42:17Z             │
│  memory_usage: ~150KB                            │
│                                                  │
└─────────────────────────────────────────────────┘
```

### 3.4 Idle Document Eviction

Most documents are not actively edited at any given moment. The OT server only holds state for **active** documents.

```
Document Lifecycle on OT Server:

  1. LOAD: First user opens document
     → Routing service assigns doc to OT server
     → OT server loads latest snapshot + recent ops from storage
     → Document state is now in memory (~100-200KB)

  2. ACTIVE: Users are editing
     → Operations processed in real-time
     → State updated in memory
     → Ops persisted to op log asynchronously

  3. IDLE: No operations for 5 minutes
     → Snapshot current state to storage
     → Evict from memory
     → Release the "slot"

  4. RE-LOAD: User returns after idle
     → Same or different OT server loads the document
     → Resumes from latest snapshot + any ops since snapshot

Memory management:
  - Active documents: ~150KB each
  - 20,000 docs per server: ~3GB (fits easily in memory)
  - Eviction keeps memory bounded regardless of total document count
```

### 3.5 OT Server Processing Throughput

```
Per-document processing:
  - Transform operation:      ~0.01ms (in-memory computation)
  - Permission check (cached): ~0.01ms
  - Persist to op log:         ~1-5ms  (async, batched)
  - Broadcast to sessions:     ~0.1ms  (enqueue to gateway)
  ─────────────────────────────
  Total per operation:         ~0.1ms  (transform + broadcast)
                               ~5ms    (including persistence)

  Max ops/sec per document: ~10,000 (limited by serialization)
  Practical limit: ~500 ops/sec per document
    (100 editors × 5 ops/sec = 500 ops/sec)

Per OT server:
  - 20,000 active documents
  - Average 10 ops/sec/document (some documents are very active, most are idle)
  - Total: ~200,000 ops/sec per OT server

Global:
  - 50M ops/sec / 200K ops/sec/server = ~250 OT servers
  - With headroom: 500-1,000 OT servers globally
```

---

## 4. Global Latency

### 4.1 The Latency Problem

```
Scenario: Alice is in Tokyo, Bob is in London.
OT server for their shared document is in Virginia (us-east).

Alice types a character:
  1. Alice's browser → WebSocket → Gateway (Tokyo)     ~5ms
  2. Gateway (Tokyo) → OT Server (Virginia)             ~150ms (transpacific)
  3. OT Server processes, transforms, persists           ~5ms
  4. OT Server → Gateway (London) → Bob's browser       ~80ms (transatlantic)
  ────────────────────────────────────────────────────
  Total Alice-to-Bob visible latency:                    ~240ms

Alice sees her own keystroke:                            ~0ms (optimistic local apply)
Alice gets server ACK:                                   ~160ms (round-trip to Virginia)
Bob sees Alice's keystroke:                              ~240ms
```

### 4.2 Optimistic Local Apply

The key UX optimization: **apply the operation locally before the server confirms it**.

```
Timeline from Alice's perspective:

t=0ms     Alice types "H"
          → Operation created: insert(pos=42, "H")
          → IMMEDIATELY applied to local document (Alice sees "H")
          → Operation sent to server via WebSocket

t=0-160ms Alice continues typing "e", "l", "l", "o"
          → Each character applied locally immediately
          → Each operation queued and sent to server
          → Alice experiences ZERO latency on her own keystrokes

t=160ms   Server ACKs "H" operation
          → Client confirms: local state is consistent with server
          → If server transformed the op, client adjusts

t=200ms   Bob's cursor update arrives
          → Alice sees Bob's cursor move (informational, non-blocking)

Net effect: Alice's typing experience is identical to a local editor.
            The 160ms server round-trip is completely hidden.
```

### 4.3 Regional OT Server Routing

```
Strategy: Route each document to the region with the most active editors.

Example:
  Document "doc_abc" has:
    - 3 editors in Tokyo
    - 1 editor in London
    - 1 editor in Virginia

  → Route to Asia-Pacific OT server (closest to majority)

  Latencies:
    Tokyo editors:    ~5ms round-trip (local region)
    London editor:    ~250ms round-trip (Asia → Europe)
    Virginia editor:  ~150ms round-trip (Asia → US East)

  vs. routing to Virginia:
    Tokyo editors:    ~150ms round-trip
    London editor:    ~80ms round-trip
    Virginia editor:  ~5ms round-trip

  Asia-Pacific routing is better: 3 users get <10ms vs. 2 users getting higher latency.

Migration: If the London and Virginia editors leave, and only Tokyo editors remain,
  the routing doesn't change (already optimal). If editors shift to mostly European,
  the document migrates to a European OT server at next idle→reload cycle.
```

### 4.4 Latency Budget

```
┌──────────────────────────────────────────────────────────┐
│              End-to-End Latency Budget                    │
├──────────────────────────────────────────────────────────┤
│                                                          │
│  Component                    Target       Budget        │
│  ─────────────────────────    ──────       ──────        │
│  Client-side processing       < 5ms        5ms          │
│  WebSocket send (local)       < 1ms        1ms          │
│  Gateway routing              < 2ms        2ms          │
│  Network (gateway → OT)       < 150ms      variable*    │
│  OT transform + persist       < 10ms       10ms         │
│  Permission check (cached)    < 1ms        1ms          │
│  Broadcast to gateways        < 5ms        5ms          │
│  Network (OT → gateway)       < 150ms      variable*    │
│  Gateway → client WebSocket   < 2ms        2ms          │
│  Client-side apply            < 5ms        5ms          │
│                                                          │
│  *Network latency is geography-dependent                 │
│                                                          │
│  Total (same region):     ~30ms                          │
│  Total (cross-region):    ~200-300ms                     │
│  Total (cross-continent): ~300-500ms                     │
│                                                          │
│  User-perceived latency for own keystrokes: ~0ms         │
│  (optimistic local apply)                                │
│                                                          │
└──────────────────────────────────────────────────────────┘
```

---

## 5. Reliability

### 5.1 Data Durability Architecture

```
Every operation is persisted before being acknowledged.
Even if all OT servers crash, no acknowledged operation is lost.

Storage Layers:

Layer 1: OT Server Memory (volatile)
  - Current document state
  - In-flight operations
  - Lost on server crash

Layer 2: Operation Log (durable - Bigtable/Spanner)
  - Every operation written synchronously (before ACK)
  - Multi-region replication (Spanner: synchronous; Bigtable: async)
  - Append-only, immutable
  - Retention: forever (for version history)

Layer 3: Document Snapshots (durable - GCS/Bigtable)
  - Periodic checkpoints (every N operations or T minutes)
  - Compressed document state at a specific revision
  - Used for fast document loading (avoid replaying entire op log)
  - Retention: multiple snapshots kept (for point-in-time recovery)

Layer 4: Cross-Region Backup (disaster recovery)
  - Asynchronous replication to geographically distant region
  - RPO (Recovery Point Objective): seconds to minutes
  - RTO (Recovery Time Objective): minutes to hours
```

### 5.2 Operation Persistence Flow

```
Client sends operation
        │
        v
  ┌──────────┐
  │ OT Server │
  │           │
  │ 1. Transform against pending ops
  │ 2. Write to op log (SYNCHRONOUS)  ──────> ┌─────────────┐
  │    Wait for durability confirmation        │ Op Log      │
  │                                    <────── │ (Spanner/   │
  │ 3. Apply to in-memory state                │  Bigtable)  │
  │ 4. ACK to client                           │             │
  │ 5. Broadcast to other clients              │ Replicated  │
  │                                            │ across 3+   │
  └──────────┘                                 │ regions     │
                                               └─────────────┘

IMPORTANT: Step 2 (persist) happens BEFORE step 4 (ACK).
This guarantees: if the client received an ACK, the operation is durable.
```

### 5.3 OT Server Failover

```
Normal state:
  ┌──────────────┐
  │ OT Server 42 │ ← handles doc_abc, doc_def, doc_ghi, ...
  │ (healthy)    │
  └──────────────┘

Server 42 crashes:

  t=0     Health check fails (3 consecutive misses, ~15 seconds)

  t=15s   Routing Service detects failure
          │
          ├── 1. Mark Server 42 as unhealthy
          │
          ├── 2. Reassign all documents to other servers
          │      doc_abc → Server 17
          │      doc_def → Server 89
          │      doc_ghi → Server 17
          │
          ├── 3. New servers load document state:
          │      a. Read latest snapshot from storage
          │      b. Replay operations from op log since snapshot
          │      c. Document state fully reconstructed
          │
          └── 4. Gateway servers notified of routing change
                 │
                 ├── Existing WebSocket connections are still open
                 ├── Gateways re-route messages to new OT servers
                 └── Clients experience a brief pause (~5-15s)
                     then resume seamlessly

  t=20s   All documents reassigned, editing resumes

Client experience during failover:
  - Operations sent during failover are buffered at gateway
  - Client sees a brief "Connecting..." indicator
  - No data loss (all ACKed ops were persisted)
  - Unacknowledged ops are retransmitted after reconnection
```

### 5.4 Snapshot Strategy

```
Snapshot triggers:
  1. Every 1,000 operations
  2. Every 5 minutes of activity
  3. When document becomes idle (all editors leave)
  4. Before document eviction from OT server memory

Snapshot contents:
  {
    doc_id:       "doc_abc",
    revision:     847293,
    timestamp:    "2025-01-15T10:42:17Z",
    document:     <serialized document tree, ~100KB>,
    active_comments: [...],
    suggestion_state: [...]
  }

Why snapshots matter:
  Without snapshots, loading a document requires replaying the ENTIRE op log
  from the beginning. For a document with 1M operations:
    - Op log replay: 1,000,000 × 0.01ms = 10 seconds (unacceptable)

  With snapshots (latest snapshot at revision 847,000):
    - Load snapshot: ~50ms
    - Replay 293 operations: 293 × 0.01ms = 3ms
    - Total: ~53ms (acceptable)

Snapshot retention:
  - Latest: always kept
  - Hourly: kept for 30 days (for version history)
  - Daily: kept for 1 year
  - Older: compressed and archived
```

---

## 6. Multi-Region Replication

### 6.1 Operation Log Replication

```
Primary: us-east (Virginia)
Replicas: europe-west (Belgium), asia-east (Taiwan)

For Spanner-based op log:
  ┌─────────────────────────────────────────────────────────┐
  │  Spanner synchronous replication (TrueTime)             │
  │                                                         │
  │  Write path:                                            │
  │    1. OT Server writes op to Spanner                    │
  │    2. Spanner replicates to majority of replicas        │
  │    3. Spanner confirms durability                       │
  │    4. OT Server ACKs to client                          │
  │                                                         │
  │  Latency cost: +5-10ms for cross-region consensus       │
  │  Benefit: zero data loss even if entire region goes down│
  │                                                         │
  │  RPO = 0 (no data loss)                                 │
  │  RTO = seconds (automatic failover)                     │
  └─────────────────────────────────────────────────────────┘

For Bigtable-based op log:
  ┌─────────────────────────────────────────────────────────┐
  │  Bigtable asynchronous replication                      │
  │                                                         │
  │  Write path:                                            │
  │    1. OT Server writes op to local Bigtable             │
  │    2. Bigtable ACKs immediately (single-region durable) │
  │    3. OT Server ACKs to client                          │
  │    4. Bigtable asynchronously replicates to other regions│
  │                                                         │
  │  Latency cost: ~0ms (async replication)                 │
  │  Risk: ~1-5 seconds of data loss if entire region fails │
  │                                                         │
  │  RPO = seconds (replication lag)                        │
  │  RTO = minutes (manual failover)                        │
  └─────────────────────────────────────────────────────────┘
```

### 6.2 Trade-off: Consistency vs. Latency

```
Spanner (synchronous):
  +  Zero data loss guarantee
  +  Strong consistency across regions
  -  +5-10ms write latency (cross-region consensus)
  -  Higher cost

Bigtable (asynchronous):
  +  Lower write latency (~1-2ms local)
  +  Lower cost
  -  Potential data loss on region failure (seconds of ops)
  -  Eventually consistent

Google likely uses:
  - Spanner for ACL data (strong consistency required for security)
  - Bigtable for operation logs (high write throughput, eventual consistency acceptable
    because the OT server is the source of truth while the document is active)
```

---

## 7. Monitoring and Alerting

### 7.1 Key Metrics

| Metric | Description | Target | Alert Threshold |
|--------|-------------|--------|-----------------|
| **OT transform latency (p50)** | Time to transform one operation | < 0.05ms | > 1ms |
| **OT transform latency (p99)** | Tail latency for transforms | < 1ms | > 10ms |
| **Operations/sec/document** | Throughput per document | < 500 | > 800 |
| **Operations/sec (global)** | Total system throughput | ~50M | > 80M (capacity plan) |
| **WebSocket connections** | Total active connections | ~100M | > 120M (capacity plan) |
| **WebSocket connection errors** | Failed connection attempts | < 0.1% | > 1% |
| **Op log write latency (p99)** | Persistence latency | < 10ms | > 50ms |
| **Op log growth rate** | Daily storage increase | ~100TB/day | > 150TB (anomaly) |
| **Snapshot frequency** | Snapshots created per hour | varies | missed snapshot > 1 hour |
| **Document load time (p50)** | Time to load and render document | < 500ms | > 2s |
| **Document load time (p99)** | Tail latency for document loads | < 2s | > 5s |
| **Permission check latency** | Cached ACL lookup time | < 0.1ms | > 1ms |
| **Permission cache miss rate** | Fraction hitting database | < 5% | > 20% |
| **Cursor broadcast rate** | Cursor updates sent per second | varies | > 10K/doc (anomaly) |
| **OT server memory usage** | Per-server memory utilization | < 70% | > 85% |
| **Active documents per OT server** | Document distribution | ~10K | > 25K (rebalance) |
| **Failover time** | Time to recover from OT server failure | < 30s | > 60s |
| **Client reconnection rate** | Reconnections per minute | < 1000 | > 10,000 (incident) |

### 7.2 Dashboard Layout

```
┌──────────────────────────────────────────────────────────────────┐
│  GOOGLE DOCS - REAL-TIME COLLABORATION DASHBOARD                 │
├──────────────────────────────────────────────────────────────────┤
│                                                                  │
│  ┌─ Global Health ──────────┐  ┌─ Capacity ─────────────────┐   │
│  │ Status: ● HEALTHY        │  │ WebSocket Conns: 87M / 150M│   │
│  │ Active Docs: 12.3M       │  │ OT Servers:      412 / 600 │   │
│  │ Global Ops/s: 47.2M      │  │ Gateway Servers: 142 / 200 │   │
│  │ Avg Latency: 23ms        │  │ Op Log Storage:  42.3 PB   │   │
│  └──────────────────────────┘  └─────────────────────────────┘   │
│                                                                  │
│  ┌─ OT Transform Latency ──────────────────────────────────┐    │
│  │ p50: 0.03ms  p90: 0.12ms  p99: 0.8ms  p99.9: 4.2ms     │    │
│  │ ▁▂▃▃▃▃▃▂▂▂▂▂▃▃▃▃▃▃▂▂▂▂▂▂▃▃▃▃▃▂▂▂▁▁                     │    │
│  └──────────────────────────────────────────────────────────┘    │
│                                                                  │
│  ┌─ Regional Breakdown ────────────────────────────────────┐    │
│  │ Region       │ Conns  │ Docs   │ Ops/s  │ Latency(p50)  │    │
│  │ us-east      │ 22M    │ 3.1M   │ 12.4M  │ 18ms          │    │
│  │ us-west      │ 15M    │ 2.2M   │ 8.7M   │ 21ms          │    │
│  │ europe-west  │ 21M    │ 3.0M   │ 11.8M  │ 25ms          │    │
│  │ asia-east    │ 18M    │ 2.5M   │ 9.2M   │ 28ms          │    │
│  │ asia-south   │ 11M    │ 1.5M   │ 5.1M   │ 32ms          │    │
│  └──────────────────────────────────────────────────────────┘    │
│                                                                  │
│  ┌─ Alerts (last 24h) ─────────────────────────────────────┐    │
│  │ ⚠ 14:23 OT server ot-asia-42 high memory (87%)          │    │
│  │ ✓ 14:25 Auto-rebalanced 3K docs to ot-asia-43           │    │
│  │ ⚠ 09:11 Op log write latency spike (p99=62ms, 3 min)    │    │
│  │ ✓ 09:14 Resolved: Bigtable tablet split completed       │    │
│  └──────────────────────────────────────────────────────────┘    │
│                                                                  │
└──────────────────────────────────────────────────────────────────┘
```

### 7.3 Critical Alert Scenarios

| Scenario | Detection | Automated Response | Manual Escalation |
|----------|-----------|-------------------|-------------------|
| OT server crash | Health check timeout (15s) | Reassign documents, reconnect clients | If > 3 servers in 10 min |
| Op log write failure | Write latency > 100ms | Retry with backoff; if persistent, stop ACKing ops | Immediately if write failures > 1% |
| WebSocket connection storm | Connection rate > 10x normal | Rate limit new connections per IP | If sustained > 5 min |
| Document stuck (no progress) | Ops queued but not processed > 30s | Restart document's OT fiber | If > 10 documents stuck |
| Region-wide outage | All health checks in region fail | DNS failover to nearest region | Immediately |
| Split brain (2 OT servers for 1 doc) | Duplicate doc_id detection | Fence old server, new server is authoritative | Immediately |

---

## 8. Capacity Planning

### 8.1 Growth Projections

```
Current (estimated):
  - 100M concurrent connections
  - 50M ops/sec
  - 500 OT servers
  - 150 gateway servers
  - 50 PB storage

2x growth scenario (18-24 months):
  - 200M concurrent connections  → 300 gateway servers
  - 100M ops/sec                 → 1,000 OT servers
  - 100 PB storage               → storage tier optimization needed

Bottleneck analysis:
  1. Gateway servers: Linear scaling, add more servers. Not a concern.
  2. OT servers: Linear scaling per document, but per-document limit is hard.
     100 editors/doc at 5 ops/sec = 500 ops/sec — well within capacity.
  3. Op log storage: 100TB/day × 365 = 36.5 PB/year. Need tiered storage:
     - Hot: last 30 days in Bigtable (fast reads)
     - Warm: 30 days - 1 year in cheaper storage
     - Cold: 1 year+ in archival storage (Colossus/GCS)
  4. Network bandwidth: 50M ops × 200B = 10 GB/s. Manageable.
```

---

## 9. Interview Talking Points

**If asked "How do you scale WebSocket connections to 100M?":**
> We separate connection termination from document processing. Stateless Gateway servers terminate WebSocket connections — each handles about 1M connections using epoll/kqueue. These gateways relay messages to stateful OT servers via internal RPC. This separation lets us scale connections independently from OT processing. At 50KB per connection, 100M connections require about 5TB of memory across 100-200 gateway servers.

**If asked "What's the bottleneck in scaling OT?":**
> The fundamental bottleneck is per-document serialization. OT requires a total ordering of operations per document, which means one document maps to one processing thread on one server. We scale by sharding documents across thousands of OT servers — each server handles ~10-20K documents. The per-document limit of ~500 ops/sec (100 editors) is why Google Docs caps concurrent editors at 100.

**If asked "What happens when an OT server crashes?":**
> No acknowledged operations are lost — every operation is persisted to the durable op log before being acknowledged to the client. The routing service detects the failure via health checks within 15 seconds, reassigns the failed server's documents to healthy servers, which reconstruct state by loading the latest snapshot and replaying recent operations from the op log. Clients experience a brief pause of 15-30 seconds, then resume editing seamlessly.

**If asked "How do you handle global latency?":**
> Two strategies. First, optimistic local apply: the client applies the operation to the local document immediately, before the server confirms it. The user sees zero latency on their own keystrokes. Second, regional routing: we route each document to the OT server region closest to the majority of its active editors, minimizing round-trip latency for most users. Cross-region latency (150-250ms) is hidden by the optimistic apply mechanism.
