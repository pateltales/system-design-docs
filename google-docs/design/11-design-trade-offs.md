# 11 - Design Trade-offs

## Overview

Every system design interview ultimately comes down to **trade-offs**. Google Docs makes specific choices that optimize for its use case — real-time, online, multi-user document editing at Google scale. Understanding *why* those choices were made, *what was sacrificed*, and *when the alternative is better* is what separates a strong candidate from one who merely describes the system.

This document covers seven major trade-offs. For each one: what Google chose, why, what the alternative looks like, and when the alternative wins.

---

## 1. OT vs. CRDT

### What Google Docs Chose: OT (Operational Transformation)

Google Docs uses a **centralized, server-authoritative OT** system. All operations for a document flow through a single OT server, which determines the canonical order and transforms operations to resolve conflicts.

### The Alternative: CRDT (Conflict-Free Replicated Data Types)

CRDTs are **decentralized** data structures that guarantee convergence without a central server. Each client independently applies operations, and the mathematical properties of CRDTs ensure all replicas converge to the same state.

### Deep Comparison

| Dimension | OT (Google Docs) | CRDT (e.g., Yjs, Automerge) |
|-----------|-------------------|------------------------------|
| **Server requirement** | Required (operations must pass through) | Not required (P2P possible) |
| **Offline editing** | Limited (ops queued, conflicts possible on reconnect) | Full support (merge guaranteed) |
| **Consistency model** | Linearizable (total order from server) | Eventually consistent (convergence guaranteed) |
| **Memory overhead** | Low (only current state + pending ops) | Higher (tombstones, vector clocks, metadata per character) |
| **Operation complexity** | O(n) per transform (n = concurrent ops) | O(1) per operation (but hidden costs in metadata) |
| **Correctness proof** | Hard (many buggy OT implementations exist) | Built into the math (easier to prove correct) |
| **Undo/redo** | Complex (must inverse-transform through history) | Complex (different set of problems) |
| **Central point of failure** | Yes (OT server) | No |
| **Adoption at scale** | Google Docs (proven at 1B+ users) | Figma (partial CRDT), Yjs (growing adoption) |

### Why Google Chose OT

1. **Historical context**: Google Docs was built in 2006 (acquired from Writely). CRDTs for collaborative text editing were not mature until ~2015+. OT was the proven approach.

2. **Server authority simplifies permissions**: With OT, the server can reject unauthorized operations before they're applied. With CRDTs, once an operation is applied locally, you can't "un-apply" it without additional complexity.

3. **Lower client-side memory**: CRDTs for text (like Yjs or Automerge) maintain **tombstones** — deleted characters are marked as deleted but not removed, because other replicas may reference them. A document with 1M edits might have 5M tombstones. OT does not have this problem.

```
CRDT memory overhead example:

Document text: "Hello" (5 visible characters)
Edit history: 1,000,000 insertions and deletions over document lifetime

OT state:
  Current document: "Hello" → ~5 bytes
  Op log: stored on server, not in client memory

CRDT state (Yjs/Automerge):
  Current document: "Hello" → 5 bytes
  Tombstones: ~995,000 deleted character markers → ~10-20 MB
  Vector clocks: per-character metadata → ~5-10 MB
  Total client memory: ~15-30 MB for a 5-character document

This is a real, documented problem with CRDT-based editors at scale.
Periodic "garbage collection" (compaction) mitigates it but adds complexity.
```

4. **Total ordering simplifies version history**: OT operations are totally ordered by the server. Revision 847,293 is unambiguous. CRDTs have partial orders — "revision" is a less clean concept.

### When CRDTs Are the Better Choice

| Scenario | Why CRDT Wins |
|----------|---------------|
| **Offline-first applications** | Users need to edit without internet for hours/days (e.g., field workers, airplanes). CRDTs guarantee merge on reconnection. OT requires the server. |
| **Peer-to-peer collaboration** | No central server (e.g., local network collaboration, decentralized apps). OT cannot function without a server. |
| **Edge computing** | Processing at the edge (IoT, mobile mesh networks) where a central server is too far or unreliable. |
| **Reduced server costs** | CRDTs offload conflict resolution to clients. No per-document server state needed. |
| **Privacy-sensitive contexts** | Users don't want document content flowing through a central server. CRDTs enable E2E encrypted collaboration. |

**Products using CRDTs:**
- **Figma**: Uses a CRDT-inspired approach for their design canvas (not text, which is simpler for CRDTs)
- **Apple Notes**: Uses CRDTs for cross-device sync
- **Ink & Switch (Automerge)**: Research-driven CRDT library for local-first software
- **Linear**: Uses CRDTs for issue tracking state

### Interview Soundbite

> "Google chose OT because it was proven technology in 2006, and the centralized server gives them a natural enforcement point for permissions, total ordering for version history, and lower client memory. CRDTs would be better if they needed offline editing or peer-to-peer collaboration — but Google's use case is online-first with a server infrastructure they already have. If I were building a local-first note-taking app like Apple Notes, I'd choose CRDTs."

---

## 2. Centralized Server vs. Decentralized Architecture

### What Google Docs Chose: Centralized

A single OT server is the **source of truth** for each document. All clients connect to this server.

### The Alternative: Decentralized / Peer-to-Peer

Clients communicate directly with each other, or through lightweight relay servers that do not process operations.

### Deep Comparison

```
Centralized (Google Docs):

  Client A ──────┐
  Client B ──────┼──── OT Server ──── Database
  Client C ──────┘         │
                     (single source
                      of truth)

  Pros:
  + Simple consistency (server determines order)
  + Natural permission enforcement
  + Clean version history
  + Lower client complexity

  Cons:
  - Single point of failure per document
  - Server required for any collaboration
  - Server cost scales with active documents
  - Latency bottleneck (all ops go through server)


Decentralized (P2P with CRDTs):

  Client A ◄────► Client B
      ▲               ▲
      │               │
      └──────► Client C

  (optional relay servers for NAT traversal,
   but they don't process operations)

  Pros:
  + No server dependency
  + Lower operational cost
  + Lower latency (direct P2P)
  + Works offline, on LAN, anywhere

  Cons:
  - Complex consistency (CRDTs or similar)
  - Permission enforcement is harder (no gatekeeper)
  - Version history is harder (no total order)
  - Higher client memory/CPU
  - NAT traversal / connectivity challenges
```

### Quantitative Analysis

```
Cost comparison (10M concurrent documents):

Centralized:
  OT servers: 500 servers × $500/month = $250K/month
  Gateways:   150 servers × $500/month = $75K/month
  Storage:    50 PB (op logs + snapshots) = varies
  Total compute: ~$325K/month + storage

Decentralized:
  Relay servers: 50 servers × $500/month = $25K/month (just for NAT traversal)
  Storage: same (still need durable storage somewhere)
  Total compute: ~$25K/month + storage

  But: higher client-side CPU/memory cost, more complex client, no permission enforcement,
  harder to monetize without server-side features.
```

### When Decentralized Wins

- **Local-first software**: Apps designed to work without internet (Obsidian, Logseq)
- **Privacy-critical**: Medical records, legal documents where routing through Google is unacceptable
- **Gaming**: Real-time state sync where a central server adds too much latency
- **Developing regions**: Unreliable internet makes server dependency impractical

---

## 3. Event Sourcing (Op Log) vs. State Snapshots

### What Google Docs Chose: Both (Event Sourcing + Periodic Snapshots)

Google stores the **complete operation log** (every insert, delete, format operation ever performed) AND periodically saves **state snapshots** (the full document at a specific revision).

### Why Not Just Snapshots?

If you only store snapshots, you lose:
- **Version history**: "Show me this document as of March 15" requires a snapshot from that exact time
- **Fine-grained undo**: Cannot undo a specific user's change from 3 days ago
- **Audit trail**: "Who deleted paragraph 3?" is unanswerable
- **Collaboration replay**: Cannot replay what happened during a session

### Why Not Just Op Log?

If you only store the operation log, you suffer from:
- **Unbounded replay time**: Loading a document requires replaying every operation from the beginning

```
Document with 5 years of history:
  Total operations: ~2,000,000
  Replay time: 2,000,000 × 0.01ms = 20 seconds to open a document

  With snapshots (snapshot every 1,000 ops):
  Load latest snapshot: ~50ms
  Replay last 500 ops: 500 × 0.01ms = 5ms
  Total: ~55ms

  20 seconds vs 55 milliseconds — snapshots are essential.
```

### The Combined Approach

```
┌─────────────────────────────────────────────────────────────────┐
│                                                                  │
│  Op Log:    [op1][op2][op3]...[op999][op1000][op1001]...[op1500] │
│                                  │                               │
│  Snapshots:                  Snap@1000                           │
│                                                                  │
│  To load current document:                                       │
│    1. Load Snap@1000                                             │
│    2. Replay op1001 through op1500                               │
│    3. Document at revision 1500 is ready                         │
│                                                                  │
│  To view document at revision 800:                               │
│    1. Load Snap@0 (initial empty document)                       │
│    2. Replay op1 through op800                                   │
│    OR                                                            │
│    1. Load Snap@1000                                             │
│    2. Reverse-apply op1000 through op801 (reverse operations)    │
│                                                                  │
└─────────────────────────────────────────────────────────────────┘
```

### Trade-off Summary

| Aspect | Op Log Only | Snapshots Only | Both (Google) |
|--------|-------------|----------------|---------------|
| Document load time | O(total ops) | O(1) | O(ops since last snapshot) |
| Storage cost | Lower (ops are small) | Higher (full doc per snapshot) | Highest (but optimized) |
| Version history | Full, fine-grained | Coarse (only at snapshot points) | Full, fine-grained |
| Undo capability | Any operation | Only roll back to snapshots | Any operation |
| Audit trail | Complete | None between snapshots | Complete |
| Complexity | Simple | Simple | More complex (manage both) |

### When Alternatives Win

- **Snapshots only**: Simple CRUD apps where history doesn't matter (e.g., a settings page)
- **Op log only**: Short-lived documents with limited history (e.g., ephemeral whiteboards, chat messages)

---

## 4. WebSocket vs. HTTP Polling

### What Google Docs Chose: WebSocket

Google Docs uses persistent WebSocket connections for real-time bidirectional communication between clients and servers.

### The Alternative: HTTP Long Polling / Short Polling

```
Short Polling:
  Client: GET /docs/abc/changes?since=rev847 every 1 second
  Server: Returns new operations (if any)
  Latency: 0-1000ms (average 500ms)

Long Polling:
  Client: GET /docs/abc/changes?since=rev847 (hangs until new data)
  Server: Holds connection open until new operations arrive, then responds
  Client: Immediately sends new request
  Latency: ~50-200ms (time to establish new connection after each response)

WebSocket:
  Client: Persistent bidirectional connection
  Server: Pushes operations immediately as they arrive
  Latency: ~1-5ms (just serialization + network)
```

### Quantitative Comparison

```
Scenario: 50 editors, each typing 5 chars/sec

WebSocket:
  Messages/second: 250 (50 × 5) operations sent to server
                  + 250 × 49 = 12,250 broadcasts to other clients
  Per-message overhead: ~50 bytes (WebSocket frame header)
  Bandwidth: 12,500 × 250 bytes avg = ~3 MB/s
  Latency: ~5ms per operation

HTTP Short Polling (1-second interval):
  Requests/second: 50 (one per client per second)
  Per-request overhead: ~500 bytes (HTTP headers, cookies, etc.)
  Operations bundled: ~5 ops per poll response
  Bandwidth: 50 × 500B headers + responses = ~50 KB/s headers alone
  Latency: 0-1000ms per operation (average 500ms)

  500ms average latency is UNACCEPTABLE for real-time editing.
  Characters appear half a second late → feels broken.

HTTP Long Polling:
  Connections/second: ~250 (new connection per broadcast event)
  Per-connection overhead: ~500 bytes + TCP handshake + TLS
  Bandwidth: 250 × 1KB = ~250 KB/s overhead
  Latency: ~50-200ms (connection establishment)

  Marginal for real-time editing. Google Docs used this early on
  before WebSocket was widely supported (pre-2012).
```

### Why WebSocket Wins for This Use Case

| Metric | WebSocket | HTTP Long Polling | HTTP Short Polling |
|--------|-----------|-------------------|-------------------|
| Latency | 1-5ms | 50-200ms | 500ms avg |
| Server connections | 1 persistent | 1 constantly recycled | 1 per interval |
| Header overhead | ~50B/msg | ~500B/request | ~500B/request |
| Bidirectional | Yes (native) | Simulated (2 connections) | No |
| Cursor updates | Efficient | Expensive | Very expensive |
| Battery (mobile) | Low (idle when no data) | Medium (constant reconnect) | High (constant polling) |
| Browser support | Universal (2024+) | Universal | Universal |

### When HTTP Polling Wins

- **Low-frequency updates**: Notification counts, dashboard refreshes (every 30-60 seconds)
- **Serverless architectures**: AWS Lambda / Cloud Functions don't support persistent connections
- **Firewall/proxy constraints**: Some corporate networks block WebSocket upgrades
- **Simple implementation**: HTTP polling requires no special server infrastructure
- **Non-interactive views**: Viewing (not editing) a document — polling every 5 seconds for changes is fine

---

## 5. Custom Rich Text Model vs. Markdown vs. HTML

### What Google Docs Chose: Custom Intermediate Representation

Google uses a proprietary document model with a tree structure (sections, paragraphs, text runs with attributes) and flat character indices. It is not HTML, not Markdown, not any standard format.

### Trade-off Analysis

```
┌─────────────────────────────────────────────────────────────────────┐
│                    Document Model Comparison                         │
├──────────────┬──────────────┬──────────────┬────────────────────────┤
│ Dimension    │ Custom Model │ Markdown     │ HTML/DOM               │
│              │ (Google)     │ (HackMD)     │ (CKEditor)             │
├──────────────┼──────────────┼──────────────┼────────────────────────┤
│ OT           │ 9 transform  │ 4 transform  │ 20+ transform pairs    │
│ complexity   │ pairs (ins,  │ pairs (ins,  │ (tree operations:      │
│              │ del, fmt)    │ del only)    │ split, merge, move)    │
├──────────────┼──────────────┼──────────────┼────────────────────────┤
│ WYSIWYG      │ Full         │ No (or split │ Full                   │
│              │              │ preview)     │                        │
├──────────────┼──────────────┼──────────────┼────────────────────────┤
│ Formatting   │ Full (bold,  │ Limited      │ Full (anything CSS     │
│ richness     │ fonts, color,│ (bold,italic │ can do)                │
│              │ tables,      │ links,code,  │                        │
│              │ images, etc.)│ basic tables)│                        │
├──────────────┼──────────────┼──────────────┼────────────────────────┤
│ Canonical    │ Yes (by      │ Mostly (some │ No (many valid DOMs    │
│ form         │ design)      │ ambiguity)   │ for same visual)       │
├──────────────┼──────────────┼──────────────┼────────────────────────┤
│ Portability  │ Low (Google  │ High (plain  │ Medium (HTML is        │
│              │ proprietary) │ text, many   │ standard but messy)    │
│              │              │ parsers)     │                        │
├──────────────┼──────────────┼──────────────┼────────────────────────┤
│ Dev cost     │ Very High    │ Low          │ Medium                 │
│ to build     │ (custom      │ (text editor │ (contentEditable       │
│              │ everything)  │ + parser)    │ + DOM diff)            │
├──────────────┼──────────────┼──────────────┼────────────────────────┤
│ Security     │ High (no raw │ High (no raw │ Low (XSS risk from     │
│              │ HTML)        │ HTML)        │ user HTML)             │
└──────────────┴──────────────┴──────────────┴────────────────────────┘
```

### Why Google Built Custom

1. **Full control over OT**: The internal model is designed so that every document state has exactly one valid representation. This is required for OT convergence.
2. **Rendering control**: Google renders on `<canvas>`, not DOM. A custom model maps cleanly to canvas rendering.
3. **Performance**: No browser DOM overhead. No contentEditable bugs.
4. **Feature flexibility**: They can add features (e.g., smart chips, linked objects) without being constrained by HTML semantics.

### When Alternatives Win

| Model | Best For | Example Products |
|-------|----------|-----------------|
| **Markdown** | Developer tools, documentation, knowledge bases | HackMD, Obsidian, GitHub |
| **HTML/DOM** | Simple rich text editing, email composition, CMS | Gmail compose, WordPress |
| **Custom** | Full-featured collaborative editors at massive scale | Google Docs, Notion (block-based custom model) |

**Key insight for interviews**: Building a custom model is a **massive** engineering investment. Only justified at Google/Notion/Figma scale. For a startup, use an existing framework (ProseMirror, Slate, Quill) which provide a semi-custom model with pre-built OT or CRDT support.

---

## 6. 100-User Concurrent Editor Limit vs. Unlimited

### What Google Docs Chose: Cap at 100 Concurrent Editors

Google Docs allows up to **100 concurrent editors** per document. Additional users can view but not edit.

### Why This Limit Exists

#### 6.1 Cursor Broadcast Cost

```
Cursor broadcast is O(N^2):

  Every cursor movement by one user must be sent to all other users.
  N users × (N-1) recipients = N(N-1) messages per cursor event.

  N=10:    10 × 9  =    90 messages per cursor event
  N=50:    50 × 49 = 2,450 messages per cursor event
  N=100:  100 × 99 = 9,900 messages per cursor event
  N=200:  200 × 199= 39,800 messages per cursor event
  N=500:  500 × 499=249,500 messages per cursor event

  At 2 cursor events/second/user:
  N=100:  100 × 2 × 99 = 19,800 messages/second ← manageable
  N=500:  500 × 2 × 499 = 499,000 messages/second ← problematic

  With 50-byte cursor messages:
  N=100:  19,800 × 50B = ~1 MB/s ← fine
  N=500:  499,000 × 50B = ~25 MB/s ← significant bandwidth for one document
```

#### 6.2 OT Server Load

```
OT operations per second:
  N editors × avg_ops_per_second = total ops

  N=100:  100 × 5 = 500 ops/sec ← one document consuming significant server CPU
  N=500:  500 × 5 = 2,500 ops/sec ← requires dedicated server for one document

  Each operation is transformed against all pending concurrent ops.
  Transform cost: O(concurrent_ops), which grows with N.

  At N=100: avg concurrent ops ≈ 5-10, transform cost is bounded
  At N=500: avg concurrent ops ≈ 25-50, transform cost becomes expensive
```

#### 6.3 Client-Side Rendering

```
Each client must render:
  - N cursors with user names and colors
  - N presence indicators
  - All incoming operations (applied to local state)

  At N=100: rendering 100 cursors + 500 ops/sec is feasible
  At N=1000: rendering 1000 cursors + 5000 ops/sec may cause frame drops
```

### The Alternative: No Limit

A system without a limit would need:
- **Cursor aggregation**: Don't show individual cursors beyond N=50, show heatmaps instead
- **Operation batching**: Aggregate operations over time windows before broadcasting
- **Sharded documents**: Split document into sections, each with its own OT server
- **Tiered editing**: Only nearby editors' cursors shown in real-time

### When Higher Limits Are Needed

| Scenario | Approach |
|----------|----------|
| Company all-hands notes (500+ people) | Allow 100 editors, rest are viewers. Viewers see real-time updates but cannot type. |
| Wikipedia-style editing | Not real-time. Lock-based (edit sections independently). Different system entirely. |
| Live coding (audience of 10,000) | 1 editor, 10,000 viewers. Viewers get broadcast-only WebSocket (pub/sub, not OT). |

### Interview Soundbite

> "The 100-editor limit exists because of O(N^2) cursor broadcast cost and O(N) OT transform cost. At 100 editors and 5 ops/sec each, the OT server handles 500 ops/sec and broadcasts ~20,000 cursor messages/sec — manageable. At 500 editors, those numbers become 2,500 ops/sec and ~500,000 cursor messages/sec — that's a dedicated server for a single document. The limit is a pragmatic engineering boundary, not an arbitrary number."

---

## 7. Online-First vs. Offline-First

### What Google Docs Chose: Online-First

Google Docs is fundamentally an **online-first** application. The collaboration engine (OT) requires a server. While there is limited offline support (you can enable "offline mode" in settings), it is clearly a secondary experience.

### How Google's Offline Mode Works

```
Online mode (default):
  Edit → Send to OT server → Server transforms → ACK → Broadcast

Offline mode (must be explicitly enabled):
  1. Before going offline:
     - Chrome extension caches document locally (Service Worker + IndexedDB)
     - Latest snapshot + recent ops stored on device

  2. While offline:
     - User can edit the cached document
     - Operations stored locally in IndexedDB
     - No collaboration (no OT server)
     - No conflict resolution (just queuing)

  3. On reconnection:
     - Client sends all queued operations to server
     - Server transforms them against any operations that happened while offline
     - Conflicts are resolved by OT (but this can produce surprising results
       if both offline and online users edited the same paragraph)

  Limitations:
  - Only works in Chrome (Service Worker dependency)
  - Must be enabled per-document before going offline
  - No collaboration with other offline users
  - Large offline edits can produce confusing merge results
  - Images/embeds may not be available offline
```

### The Alternative: Offline-First

An offline-first system treats the local device as the primary data store and syncs with servers/peers when connectivity is available.

```
Offline-first architecture (e.g., Notion, Apple Notes):

  Local Database (SQLite / IndexedDB)
  ↕ (sync when online)
  Cloud Storage

  - Every change is immediately written locally
  - Sync runs in background when network is available
  - Conflicts resolved automatically (CRDTs) or manually (conflict UI)
  - No dependency on server for basic functionality
```

### Comparison

| Aspect | Online-First (Google Docs) | Offline-First (Notion/Apple Notes) |
|--------|---------------------------|-------------------------------------|
| **Editing without internet** | Very limited, must pre-enable | Full support, always works |
| **Collaboration model** | OT (requires server) | CRDT or custom merge |
| **Conflict resolution** | Server-authoritative (clean) | Client-side (may surprise users) |
| **Data freshness** | Always latest (if online) | May be stale until sync |
| **Infrastructure cost** | Higher (always-on servers) | Lower (sync is batch) |
| **User experience (online)** | Best-in-class real-time editing | Slightly slower (sync overhead) |
| **User experience (offline)** | Poor to nonexistent | Seamless |
| **Implementation complexity** | High (real-time OT) | High (CRDT + sync engine) |

### When Offline-First Wins

| Use Case | Why Offline-First |
|----------|-------------------|
| Mobile apps in developing regions | Connectivity is intermittent |
| Field work (construction, agriculture) | No WiFi, limited cellular |
| Air travel | Extended offline periods |
| Privacy-sensitive apps | Data stays on device |
| Note-taking apps | Users expect instant capture regardless of connectivity |
| Military/government | Disconnected environments |

### Quantitative Perspective

```
Google Docs user connectivity profile (estimated):
  - Always online: ~85% of usage time
  - Briefly offline (tunnel, elevator): ~10%
  - Extended offline (>5 min): ~5%

  For 85% of usage, online-first is optimal.
  For 5% of usage, offline-first would be significantly better.

  Google's calculus: optimize for the 85% case.
  The 5% extended-offline case is served by "good enough" offline mode.

Notion user connectivity profile (estimated):
  - Always online: ~70% (more mobile users, more global)
  - Briefly offline: ~15%
  - Extended offline: ~15%

  Notion invests more in offline because their user base
  experiences more offline time (mobile-heavy, global audience).
```

### Interview Soundbite

> "Google Docs is online-first because OT requires a server for conflict resolution, and 85%+ of usage happens online. Their limited offline mode queues operations locally and replays them when connectivity returns. If I were building a note-taking app for mobile users in regions with unreliable internet, I'd go offline-first with CRDTs — the extra client-side complexity is justified because offline is a primary use case, not an edge case."

---

## Summary Table

| # | Trade-off | Google's Choice | Alternative | When Alternative Wins |
|---|-----------|-----------------|-------------|----------------------|
| 1 | OT vs. CRDT | OT (server-authoritative) | CRDT (decentralized) | Offline-first, P2P, privacy-sensitive |
| 2 | Centralized vs. Decentralized | Centralized | P2P / edge computing | No reliable server, local-first apps |
| 3 | Event Sourcing vs. Snapshots | Both (op log + snapshots) | Snapshots only | Simple CRUD, no history needed |
| 4 | WebSocket vs. HTTP Polling | WebSocket | HTTP polling | Low-frequency updates, serverless |
| 5 | Custom Model vs. Markdown/HTML | Custom intermediate repr | Markdown / HTML | Startups, simple editors, dev tools |
| 6 | 100-user limit vs. Unlimited | 100 editors | No limit | Mass collaboration (Wikipedia-style) |
| 7 | Online-first vs. Offline-first | Online-first | Offline-first | Mobile, poor connectivity, note-taking |

---

## Meta-Advice for Interviews

When discussing trade-offs in a system design interview:

1. **Name both sides explicitly**: "We could go with X or Y. X gives us A and B, Y gives us C and D."
2. **State what you're choosing**: "For this system, I'll go with X because..."
3. **Acknowledge the cost**: "The cost of X is that we lose C, but that's acceptable because..."
4. **Know when to flip**: "If the requirements changed to offline-first, I'd switch to Y because..."
5. **Use numbers**: "At 100 concurrent editors, cursor broadcast is 20K messages/sec — manageable. At 1,000, it's 2M messages/sec — we'd need a different approach."

The interviewer is not looking for the "right" answer. They are looking for **structured reasoning about trade-offs**. Every choice has a cost. Strong candidates articulate both sides and justify their decision with the specific requirements of the system they're designing.
