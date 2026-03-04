# System Design Interview Simulation: Design Google Docs (Real-Time Collaborative Document Editor)

> **Interviewer:** Principal Engineer (L8), Google Docs Collaboration Team
> **Candidate Level:** SDE-3 (L6 — Senior Software Development Engineer)
> **Duration:** ~60 minutes
> **Date:** February 21, 2026

---

## PHASE 1: Opening & Problem Statement (~2 min)

**Interviewer:**
Hey, welcome. I'm [name], Principal Engineer on the Google Docs collaboration team. For today's system design round, I'd like you to design a **real-time collaborative document editor** — think Google Docs. A system where multiple users can simultaneously edit the same document, see each other's cursors in real-time, and never lose data even when edits conflict.

The interesting part here isn't "build a text editor" — it's the **real-time collaboration**. How do you ensure that when Alice types "hello" at position 5 and Bob deletes a character at position 3 at the exact same time, both their screens converge to the same final document? That's the problem I want you to solve.

I'll push on your decisions — that's me calibrating depth, not a negative signal. Take it away.

---

## PHASE 2: Requirements Gathering & Scoping (~5 min)

**Candidate:**
Thanks! Real-time collaborative editing is a fascinating problem — the core challenge is distributed consistency over a mutable, shared data structure. Let me scope this carefully before drawing anything.

**Functional Requirements — what operations do we need?**

> "The core capabilities I'd expect from a Google Docs-like system:
>
> - **Document CRUD**: Create, read, update metadata (title, sharing), soft-delete (trash) documents.
> - **Real-time collaborative editing**: Multiple users editing the same document simultaneously, with edits appearing on all clients within ~100ms. This is the hard problem.
> - **Cursor & presence**: See other users' cursor positions and selections in real-time. The colored cursors with name labels.
> - **Revision history**: Auto-save named revisions. View document at any point in time. Restore to a previous version.
> - **Commenting & suggestions**: Comments anchored to text ranges. Suggestion mode (tracked changes with accept/reject).
> - **Sharing & permissions**: Owner / Editor / Commenter / Viewer roles. Link sharing and direct sharing.
> - **Rich text formatting**: Bold, italic, headings, lists, tables, images — not just plain text.
>
> A few clarifying questions:
> - **What's the primary collaboration model — real-time with a central server, or peer-to-peer?**"

**Interviewer:** "Good question. What's your take?"

> "Google Docs uses a **centralized, server-authoritative model**. All operations flow through a central server that imposes a total order. This is simpler than peer-to-peer — you avoid the need for vector clocks and the notoriously hard TP2/CP2 convergence property. Google can afford to run reliable, low-latency servers, so a centralized model is the right tradeoff. Peer-to-peer (like CRDTs) makes more sense for offline-first or decentralized apps.
>
> I'll design with the centralized model."

**Interviewer:** "Good. What about offline editing?"

> "Google Docs supports limited offline editing via a Chrome PWA extension — the document is cached in IndexedDB, edits are queued locally, and synced on reconnect. It's not the primary use case — Google Docs is online-first. I'll mention it but won't deep-dive unless you want."

**Interviewer:** "Mention it, focus on the online real-time path. Keep going."

**Non-Functional Requirements:**

> "Now the critical constraints. The collaboration model drives the NFRs more than the document features:
>
> | Dimension | Requirement | Rationale |
> |---|---|---|
> | **Latency** | Local edit < 50ms, remote edit visible < 200ms | Real-time feel. User types a character and sees it instantly (optimistic local apply). Other users see it within 200ms. |
> | **Consistency** | Convergence — all clients reach the same document state | The #1 correctness guarantee. After all operations are applied, every client's document must be identical. Not eventual consistency in the traditional sense — this is OT convergence. |
> | **Availability** | 99.99% (52 min downtime/year) | Users rely on Docs for work. Downtime means lost productivity for millions. |
> | **Durability** | No edit ever lost once acknowledged by the server | Every operation is persisted in the operation log. Even if a server crashes mid-operation, the log is the source of truth. |
> | **Concurrent editors** | Up to 100 simultaneous editors per document | Google Docs' actual limit. Beyond this, cursor broadcasts become O(N²) and OT server load grows linearly. Additional users can view. |
> | **Scale** | 3B+ users with access, 1B+ monthly active Docs users, hundreds of millions of active documents | Google Workspace scale — massive. |
> | **Document size** | Up to ~1.02 million characters per document | Practical limit — OT complexity grows with document size (positions reference character indices). |
>
> **Why latency matters so much:** In a collaborative editor, latency is the difference between 'feels like we're editing together' and 'feels like taking turns.' Google Docs achieves the former with **optimistic local application** — your edit is applied locally immediately, then sent to the server. You never wait for the server to type a character."

**Interviewer:**
You mentioned Operational Transformation — do you know the history there?

**Candidate:**

> "Yes. OT was originally proposed by Ellis and Gibbs in 1989 (the dOPT algorithm). Google's implementation is based on the **Jupiter collaboration protocol** from Xerox PARC (Nichols et al., 1995). Jupiter's key insight is that a **client-server architecture only needs the TP1 convergence property** — you don't need TP2, which is required for peer-to-peer OT and is notoriously difficult to implement correctly.
>
> Google Wave (launched 2009, discontinued 2012) used a more ambitious version of OT on XML tree-structured documents. That was over-engineered — OT on trees is extremely complex. Google Docs simplified this by treating the document as a **linear annotated sequence** rather than a tree, which dramatically reduces the number of transform function pairs needed.
>
> The Google Wave OT whitepaper (David Wang, 2009) is public and documents the client-server protocol, including the three-state client model (Synchronized / Awaiting ACK / Awaiting ACK + Buffer). Google Docs' protocol is a descendant of this."

**Interviewer:**
Good depth. Let's get some numbers and then architect this.

---

### L5 vs L6 vs L7 — Phase 2 Rubric

| Aspect | L5 (SDE-2) | L6 (SDE-3) | L7 (Principal) |
|---|---|---|---|
| **Functional Reqs** | Lists CRUD + real-time editing | Proactively raises OT vs CRDT, cursor presence, suggestion mode, offline. Knows the centralized model is a deliberate choice. | Additionally discusses document model choices (linear vs tree vs block), collaboration semantics (intention preservation, causality), and why Google moved away from Wave's approach. |
| **Non-Functional** | "It should be fast and reliable" | Quantifies latency (50ms local, 200ms remote), knows 100 editor limit, explains why latency matters for collaboration UX | Frames NFRs in terms of OT protocol properties (convergence, intention preservation, causality). Discusses the tension between consistency and latency in a globally distributed system. |
| **Technical depth** | "Use WebSockets for real-time" | Knows OT history (Jupiter, Wave), explains TP1 vs TP2, centralized vs peer-to-peer tradeoff | References specific papers, discusses correctness bugs in published OT algorithms (Imine et al. 2003), explains why Wave's tree OT was abandoned |
| **Scoping** | Accepts problem as given | Drives clarifying questions, negotiates offline out of scope | Proposes phased deep dives, identifies which components are most interesting for the interview |

---

## PHASE 3: Scale Estimation (~3 min)

**Candidate:**

> "Let me estimate Google Docs-scale numbers to ground our design decisions."

#### User & Document Scale

> "Google Workspace has over **3 billion users** with access to Docs (including free Gmail users). Google Docs specifically has over **1 billion monthly active users**.
>
> - **Active documents at any given time**: Let's estimate 100 million documents with at least one active editor right now.
> - **Documents with multiple simultaneous editors**: Maybe 10% — so **10 million documents with 2+ concurrent editors** at peak.
> - **Average concurrent editors per collaborative document**: ~3-5 users. A small team editing a shared doc."

#### Operation Traffic

> "Each active editor generates operations at typing speed:
> - **Typing speed**: ~5 characters/second for an active typist, but bursty. Average maybe 1-2 ops/sec including pauses.
> - **Operations per second globally**: 100M active documents × average 0.5 ops/sec = **50 million operations/sec** across all documents.
> - **Per-document peak**: 100 editors × 5 ops/sec = **500 ops/sec** for a maximally busy document. The OT server for that document must handle this.
> - **Cursor updates**: 100 editors × 10 cursor updates/sec = **1,000 cursor updates/sec** per document. These are broadcast to all editors — 1,000 × 99 = **~100K cursor messages/sec** for a fully loaded document."

#### Storage

> "- **Operation log**: Each operation is ~100-200 bytes (position, text, formatting metadata, user ID, timestamp). At 50M ops/sec, that's **~5-10 GB/sec** of new operation log data.
> - **Document snapshots**: Average doc is maybe 50KB. 1 billion docs = **50 PB** of snapshot data.
> - **Revision history**: Each document keeps months/years of history. The operation log is the source of truth — snapshots are compaction points.
> - **Total storage**: Petabytes of operation logs + document snapshots."

#### WebSocket Connections

> "Each active editor maintains a persistent WebSocket connection:
> - **100 million active editing sessions** = 100 million WebSocket connections at peak.
> - These are **stateful, long-lived connections** — not the typical stateless HTTP request. This is a fundamentally different scaling challenge."

**Interviewer:**
Good numbers. The per-document OT serialization is the key bottleneck — let's architect around that. Let's build this.

---

## PHASE 4: High-Level Architecture — Iterative Build-Up (~10 min)

**Candidate:**

> "Let me start with the simplest thing that could work, find the problems, and evolve."

### Attempt 0: Single Server, Full Document Save

> "The absolute simplest model — a web server with a database. Client loads the full document, edits locally, and periodically saves the entire document back to the server.
>
> ```
>     Client A                Client B
>       │                        │
>       ▼                        ▼
>   ┌────────────────────────────────┐
>   │         Web Server             │
>   │                                │
>   │   GET  /doc/123  → full doc    │
>   │   POST /doc/123  → save full   │
>   │         document               │
>   │                                │
>   │   ┌──────────────────────┐     │
>   │   │  Database (Postgres) │     │
>   │   │  doc_id → full_text  │     │
>   │   └──────────────────────┘     │
>   └────────────────────────────────┘
> ```
>
> This is how Word worked with SharePoint in the old days — 'check out' a file, edit, 'check in.'"

**Interviewer:**
What's wrong with it?

**Candidate:**

> "Everything, for our use case:
>
> | Problem | Impact |
> |---|---|
> | **No concurrent editing** | Last save wins. If Alice and Bob both edit, one overwrites the other's changes. |
> | **Full document on every save** | Sending 50KB for a 1-character edit is wasteful. At scale, this is enormous bandwidth. |
> | **No real-time visibility** | Alice can't see Bob's cursor or edits until Bob saves. No 'editing together' feel. |
> | **No revision history** | Overwritten data is gone. No undo across saves. |
> | **Data loss** | Last-writer-wins means edits are silently lost. Unacceptable. |
>
> We need a fundamentally different approach — **operation-based editing**."

### Attempt 1: Operation-Based Editing + Operation Log

> "Instead of saving the full document, the client sends **operations** — small, precise descriptions of edits:
>
> ```
> Operations:
>   insert(pos=5, text="hello")     — insert 'hello' at position 5
>   delete(pos=3, count=2)          — delete 2 chars starting at position 3
>   format(pos=5, len=3, bold=true) — bold 3 chars starting at position 5
> ```
>
> The server maintains an **operation log** (append-only) — every edit ever made to the document, in order. The document state is **derived** by replaying the log.
>
> ```
>     Client A                     Server
>       │                            │
>       │  insert(pos=5, "hello")    │
>       │ ──────────────────────────▶│
>       │                            │──▶ Append to operation log
>       │                            │──▶ Apply to document state
>       │         ACK                │
>       │ ◀──────────────────────────│
>       │                            │
>       │                            │
>   ┌──────────────────────────────────────┐
>   │              Operation Log           │
>   │  rev 1: insert(0, "H")      user: A │
>   │  rev 2: insert(1, "e")      user: A │
>   │  rev 3: insert(2, "l")      user: A │
>   │  rev 4: insert(3, "l")      user: A │
>   │  rev 5: insert(4, "o")      user: A │
>   │  ...                                │
>   └──────────────────────────────────────┘
> ```
>
> **What this gives us:**
> - **Revision history for free** — replay the log to any point in time.
> - **Efficient network** — send a tiny operation instead of the full document.
> - **Undo** — apply the inverse operation.
> - **Event sourcing** — the log is the source of truth, the document state is a read model."

**Interviewer:**
Better. But can two users edit at the same time?

**Candidate:**

> "Not yet — this is still **serial**. Operations are applied one at a time, in order. If Alice and Bob send operations simultaneously, the server processes them sequentially, which is correct but means one user is always waiting. And crucially, there's **no real-time push** — the other client has to poll to see changes.
>
> | Problem | Impact |
> |---|---|
> | **No concurrent editing** | Operations are serial — one user blocks the other |
> | **No real-time push** | Client B doesn't see Client A's edits until it polls |
> | **No cursor sharing** | No awareness of other users |
> | **Position conflicts** | If Alice inserts at pos 5 and Bob deletes at pos 3, Alice's operation is stale — after Bob's delete, position 5 should shift left to position 4 |
>
> That last problem is the critical one. **Concurrent operations reference positions that may have shifted.** This is where Operational Transformation comes in."

### Attempt 2: Real-Time Collaboration with Operational Transformation

> "This is the architectural leap. Two key additions:
>
> 1. **WebSocket** for bidirectional, persistent communication. No polling — the server pushes operations to all connected clients in real-time.
>
> 2. **OT engine** on the server that **transforms** concurrent operations to account for position shifts, ensuring all clients converge to the same document state.
>
> ```
>     Client A                   OT Server                 Client B
>       │                            │                        │
>       │ ◀───── WebSocket ─────────▶│◀───── WebSocket ──────▶│
>       │                            │                        │
>       │  insert(5,"X") [rev=10]    │                        │
>       │ ──────────────────────────▶│                        │
>       │                            │  (server is at rev 11  │
>       │                            │   — Bob already sent   │
>       │                            │   delete(3) at rev 10) │
>       │                            │                        │
>       │                            │  Transform:            │
>       │                            │  Alice's insert(5,"X") │
>       │                            │  against Bob's del(3)  │
>       │                            │  → insert(4,"X")       │
>       │                            │  (pos shifts left by 1 │
>       │                            │   because Bob deleted  │
>       │                            │   before pos 5)        │
>       │                            │                        │
>       │         ACK [rev=12]       │   insert(4,"X") [rev12]│
>       │ ◀──────────────────────────│──────────────────────▶ │
>       │                            │                        │
> ```
>
> **How OT works (the core idea):**
> - Every operation carries a **revision number** — the server revision it was based on.
> - When the server receives an operation based on rev `r`, and the server is at rev `r + n`, it **transforms** the incoming operation against the `n` operations that happened since rev `r`.
> - The transform function adjusts positions so the operation's **intent** is preserved even though the document has changed.
> - The server then applies the transformed operation, increments its revision, and broadcasts to all other clients.
>
> **Transform example:**
> ```
> Document: "ABCDEFGH" (8 chars)
>
> Alice (at rev 10): insert("X", pos=5)  → intent: insert X between E and F
> Bob   (at rev 10): delete(pos=3)        → intent: delete D
>
> Server receives Bob first → applies delete(3) → "ABCEFGH" (rev 11)
> Server receives Alice → she's based on rev 10, server is at 11
>   Transform insert(5,"X") against delete(3):
>     delete was at pos 3, before pos 5 → shift Alice's pos left by 1
>     → insert(4,"X")
> Server applies insert(4,"X") → "ABCEXFGH" (rev 12)
>
> Bob receives insert(4,"X") → applies to his view "ABCEFGH" → "ABCEXFGH" ✓
> Alice receives ACK → her local state already has X at pos 5 in original doc
>   She also receives Bob's delete(3), transformed against her insert:
>     → delete(3) (no shift needed — delete was before insert)
>   Applies → "ABCEXFGH" ✓
>
> Both converge to "ABCEXFGH" ✓
> ```
>
> **Cursor presence** is broadcast over the same WebSocket — each client sends its cursor position, and the server relays to all other clients. Cursor positions are also transformed when the document changes.
>
> **The architecture now:**
>
> ```
>                        ┌──────────────────────┐
>                        │       Clients         │
>                        │  (Browser/Mobile/PWA) │
>                        └──────────┬───────────┘
>                                   │ WebSocket (persistent, bidirectional)
>                        ┌──────────▼───────────┐
>                        │     OT Server         │
>                        │  (per-document)        │
>                        │                        │
>                        │  - Receives ops        │
>                        │  - Transforms against  │
>                        │    concurrent ops      │
>                        │  - Applies to doc state│
>                        │  - Broadcasts to all   │
>                        │    connected clients   │
>                        │  - Appends to op log   │
>                        └──────────┬────────────┘
>                                   │
>                        ┌──────────▼───────────┐
>                        │   Operation Log (DB)  │
>                        │  (append-only)        │
>                        │  rev → operation       │
>                        └──────────────────────┘
> ```"

**Interviewer:**
Good — this is the right direction. But I see several problems with this design. Can you identify them?

**Candidate:**

> "Yes, several:
>
> | Problem | Impact |
> |---|---|
> | **Single OT server** | Single point of failure. If it goes down, no editing for any document. |
> | **No snapshots** | Loading a document means replaying the entire operation log from the beginning. A document with 1 million operations takes forever to load. |
> | **No permissions** | Anyone can edit any document. No access control. |
> | **No offline support** | If the network drops, the user is stuck. |
> | **No rich text** | We only handle plain text operations. Bold, headings, tables not supported. |
> | **Operation log grows forever** | Storage grows unbounded. Old operations are never cleaned up. |
>
> Let me fix these."

### Attempt 3: Snapshots + Permissions + Offline Queueing

> "Three critical improvements:
>
> **1. Periodic snapshots (compaction):**
> Every N operations (e.g., 100) or every M minutes, the server takes a **full snapshot** of the document state. Loading a document = load latest snapshot + replay only the operations since that snapshot. Old operations before the snapshot can be archived to cold storage.
>
> ```
> Operation Log:
>   ops 1-100    → archived (cold storage)
>   [SNAPSHOT at op 100: full document state]
>   ops 101-150  → active (hot storage)
>
> Loading doc = snapshot(100) + replay ops 101-150
>   Instead of replaying all 150 ops from scratch
> ```
>
> **2. Permission model:**
> Four levels: **Owner > Editor > Commenter > Viewer**
> - Checked on WebSocket connection establishment
> - Checked on every incoming operation (an editor operation from a viewer is rejected)
> - Permission changes are pushed in real-time (downgrade editor → viewer while editing → client switches to read-only mode)
>
> **3. Offline queueing:**
> When the client loses connectivity:
> - Continue editing locally (optimistic)
> - Queue operations in local storage (IndexedDB)
> - On reconnect: send all queued operations to server
> - Server transforms them against all operations that happened during offline period
> - Client receives and applies any missed operations"

**Interviewer:**
Better. But you still have a single OT server. What happens when it crashes? And what about global latency — a user in Tokyo editing a document whose server is in Virginia?

**Candidate:**

> "Exactly — those are the production-hardening concerns. Let me evolve to the final architecture."

### Attempt 4: Production Architecture (Global, Fault-Tolerant, Rich Text)

> "Let me address each problem:
>
> **Per-document OT routing (not a single server):**
> Each document is assigned to a specific OT server instance. Different documents go to different servers — **horizontal scaling by document ID**. The OT engine for document X is on server A, for document Y on server B.
>
> The key constraint: **a document's OT operations must be serialized through a single coordination point** (for total ordering). But different documents are completely independent.
>
> ```
>   ┌────────────────────────────────────────────────┐
>   │               Connection Gateway               │
>   │  (WebSocket termination, auth, routing)         │
>   │  Routes doc-123 → OT Server A                  │
>   │  Routes doc-456 → OT Server B                  │
>   │  Routes doc-789 → OT Server C                  │
>   └────────────┬──────────┬──────────┬─────────────┘
>                │          │          │
>         ┌──────▼──┐ ┌────▼────┐ ┌──▼──────┐
>         │ OT Srv A│ │ OT Srv B│ │ OT Srv C│
>         │ doc-123 │ │ doc-456 │ │ doc-789 │
>         │ doc-234 │ │ doc-567 │ │ doc-890 │
>         └────┬────┘ └────┬────┘ └────┬────┘
>              │           │           │
>         ┌────▼───────────▼───────────▼────┐
>         │     Operation Log (Spanner /    │
>         │     Bigtable) — replicated,     │
>         │     globally consistent         │
>         └─────────────────────────────────┘
> ```
>
> **Failover:** If OT Server A crashes, another server loads document 123's latest state from the operation log + latest snapshot, and takes over. The operation log is the durable source of truth, not the in-memory OT server state. Clients reconnect via the gateway, which routes them to the new server.
>
> **Global latency mitigation:**
> - **Optimistic local apply**: Client applies operations locally immediately — latency to server doesn't affect typing responsiveness.
> - **Regional OT servers**: Route each document's OT to the region with the most active editors. If 3 editors are in Asia and 1 in US, the OT server runs in Asia.
> - **Operation log replication**: Use Spanner for globally consistent replication of the operation log. Even if the OT server is in one region, the log is replicated globally.
>
> **Rich text OT:**
> Extend the operation model beyond plain text:
> - `retain(n)`: skip n items (no change)
> - `insert(text, attributes)`: insert text with formatting (bold, italic, font, size)
> - `delete(n)`: delete n items
> - `applyFormat(range, attributes)`: apply/remove formatting on a range
>
> Each format operation must be transformed against text operations and other format operations. This increases the transform function matrix from 3×3 (insert/delete/retain) to ~5×5+.
>
> **Comment anchoring:**
> Comments are anchored to text ranges (start position, end position). When text is edited, comment anchors must be adjusted using the same OT transform logic. If the anchored text is deleted, the comment becomes orphaned (shown as "text was deleted").
>
> **The full production architecture:**
>
> ```
>                           ┌────────────────────────┐
>                           │        Clients          │
>                           │  (Web, iOS, Android)    │
>                           └───────────┬────────────┘
>                                       │ WebSocket / HTTPS
>                           ┌───────────▼────────────┐
>                           │    Connection Gateway   │
>                           │  - WebSocket termination│
>                           │  - Authentication       │
>                           │  - Document routing     │
>                           │  - Rate limiting        │
>                           └───────────┬────────────┘
>                                       │
>              ┌────────────────────────┼──────────────────────────┐
>              │                        │                          │
>   ┌──────────▼──────────┐  ┌─────────▼──────────┐  ┌───────────▼──────────┐
>   │   OT Server Pool    │  │  Document Service   │  │  Presence Service    │
>   │  (stateful, per-doc)│  │  (stateless)        │  │  (ephemeral state)   │
>   │                     │  │                     │  │                      │
>   │  - Transform ops    │  │  - CRUD metadata    │  │  - Cursor positions  │
>   │  - Serialize order  │  │  - Permission checks│  │  - User colors       │
>   │  - Broadcast to     │  │  - Sharing          │  │  - Online indicators │
>   │    connected clients│  │  - Export (PDF,DOCX) │  │  - Throttled broadcast│
>   │  - Snapshot trigger  │  │  - Revision listing │  │                      │
>   └──────────┬──────────┘  └─────────┬──────────┘  └──────────────────────┘
>              │                        │
>   ┌──────────▼────────────────────────▼──────────┐
>   │            Storage Layer                      │
>   │                                               │
>   │  ┌──────────────┐  ┌────────────────────────┐ │
>   │  │ Operation Log │  │  Document Metadata &   │ │
>   │  │ (Bigtable /   │  │  Snapshots (Spanner /  │ │
>   │  │  Spanner)     │  │  GCS / Colossus)       │ │
>   │  │              │  │                        │ │
>   │  │ Append-only  │  │  doc_id → {title,      │ │
>   │  │ per-document │  │   owner, permissions,  │ │
>   │  │ rev → op     │  │   latest_snapshot,     │ │
>   │  └──────────────┘  │   revision_list}       │ │
>   │                     └────────────────────────┘ │
>   └───────────────────────────────────────────────┘
> ```"

**Interviewer:**
Good evolution. Let me summarize what you've built so far:

| Attempt | Architecture | Key Addition | Problem Solved |
|---|---|---|---|
| **0** | Single server, save full doc | Baseline | N/A |
| **1** | Operation-based + op log | Operations instead of full doc, event sourcing | Bandwidth, revision history |
| **2** | OT engine + WebSocket | Operational Transformation, real-time push | Concurrent editing, real-time visibility |
| **3** | Snapshots + permissions + offline | Compaction, access control, offline queue | Load performance, security, offline |
| **4** | Per-doc routing, global, rich text | Horizontal scaling, failover, formatting | Scale, reliability, rich text |

Now let's deep dive into the components. Where do you want to start?

**Candidate:**

> "The OT engine is the heart of this system — and the most technically interesting. Let me start there."

---

### L5 vs L6 vs L7 — Phase 4 Rubric

| Aspect | L5 (SDE-2) | L6 (SDE-3) | L7 (Principal) |
|---|---|---|---|
| **Architecture evolution** | Jumps to "use WebSockets and OT" without building up from simpler models | Builds iteratively: full-doc save → operations → OT → snapshots → production. Explains why each step fails and what the next step fixes. | Same iterative build-up, but also discusses alternative paths (CRDT-based architecture), why Google chose OT, and the organizational implications of each choice. |
| **OT understanding** | "OT resolves conflicts between concurrent edits" | Explains transform with a concrete example (position shifting), knows the revision-based protocol, explains the client state machine | Discusses transform function correctness (TP1/TP2), references published bugs in OT algorithms, explains why centralized OT avoids TP2 |
| **Scaling insight** | "Add more servers" | Per-document routing with stateful OT servers, failover via operation log replay, global latency mitigation via optimistic local apply | Discusses OT server memory management, hot document handling, connection gateway design, Spanner vs Bigtable tradeoffs for the op log |
| **Production thinking** | Doesn't mention monitoring or failure modes | Mentions failover, regional routing, rate limiting | Proposes specific monitoring metrics (OT transform latency p99, ops/sec per doc, snapshot frequency), discusses blast radius of OT server failures |

---

## PHASE 5: Deep Dive — Operational Transformation Engine (~12 min)

**Interviewer:**
Let's go deep on OT. Walk me through the transform functions and the client-server protocol in detail.

**Candidate:**

> "OT is the core innovation that makes real-time collaboration possible. Let me cover: (1) the operation model, (2) transform functions, (3) the client-server protocol, and (4) why Google chose OT over CRDTs."

#### The Operation Model

> "Google Docs represents edits as **compound operations** — a sequence of components that traverse the document from start to end:
>
> | Component | Meaning | Example |
> |---|---|---|
> | `retain(n)` | Skip n characters (no change) | `retain(5)` = leave first 5 chars unchanged |
> | `insert(text)` | Insert text at current position | `insert("hello")` = insert 'hello' here |
> | `delete(n)` | Delete n characters from current position | `delete(3)` = remove next 3 chars |
>
> **An operation is a full traversal of the document.** Every character is either retained, deleted, or preceded by an insertion.
>
> Example: Insert 'X' at position 5 in a 10-character document:
> ```
> [retain(5), insert("X"), retain(5)]
>  ^^^^^^^^                ^^^^^^^^
>  skip first 5            keep remaining 5
> ```
>
> Delete characters 3-5 in a 10-character document:
> ```
> [retain(3), delete(2), retain(5)]
> ```
>
> The total length consumed by the operation must equal the document length. This is a validity check — if the operation references more or fewer characters than the document has, it's invalid."

#### Transform Functions

> "The **transform function** takes two concurrent operations (both based on the same document state) and produces transformed versions that can be applied in either order to reach the same result.
>
> ```
> transform(op_A, op_B) → (op_A', op_B')
>
> Such that:
>   apply(apply(doc, op_A), op_B') == apply(apply(doc, op_B), op_A')
> ```
>
> This is the **TP1 (Transformation Property 1)** convergence guarantee.
>
> **The transform algorithm walks both operations simultaneously**, component by component, adjusting positions as it goes. Let me trace through the key cases:
>
> **Case 1: Insert vs Insert (both insert at different positions)**
> ```
> op_A: [retain(3), insert("X"), retain(7)]   — insert X at pos 3
> op_B: [retain(7), insert("Y"), retain(3)]   — insert Y at pos 7
>
> Transform:
>   op_A': [retain(3), insert("X"), retain(8)]  — still at pos 3, but doc is now 1 longer
>   op_B': [retain(8), insert("Y"), retain(3)]  — pos 7→8 because X was inserted before pos 7
> ```
>
> **Case 2: Insert vs Insert (same position — tiebreak)**
> ```
> op_A: [retain(5), insert("X"), retain(5)]   — insert X at pos 5
> op_B: [retain(5), insert("Y"), retain(5)]   — insert Y at pos 5
>
> Tiebreak by user ID (lower ID goes first):
>   If A < B:
>     op_A': [retain(5), insert("X"), retain(6)]  — A's insert goes first
>     op_B': [retain(6), insert("Y"), retain(5)]  — B's insert shifts right
> ```
>
> **Case 3: Insert vs Delete**
> ```
> op_A: [retain(5), insert("X"), retain(5)]   — insert X at pos 5
> op_B: [retain(3), delete(1), retain(6)]      — delete char at pos 3
>
> Transform:
>   op_A': [retain(4), insert("X"), retain(5)]  — pos 5→4 because deletion before pos 5
>   op_B': [retain(3), delete(1), retain(7)]    — retain at end grows by 1 (X was inserted)
> ```
>
> **Case 4: Delete vs Delete (same character)**
> ```
> op_A: [retain(5), delete(1), retain(4)]   — delete char at pos 5
> op_B: [retain(5), delete(1), retain(4)]   — delete same char at pos 5
>
> Transform:
>   op_A': [retain(5), retain(4)]    — char already deleted, becomes no-op
>   op_B': [retain(5), retain(4)]    — char already deleted, becomes no-op
> ```
>
> **Case 5: Format vs Insert**
> ```
> op_A: [retain(3), format(5, {bold:true}), retain(2)]  — bold chars 3-8
> op_B: [retain(5), insert("X"), retain(5)]              — insert X at pos 5
>
> Transform:
>   op_A': [retain(3), format(6, {bold:true}), retain(2)] — bold range expands (X is within range)
>   op_B': [retain(5), insert("X"), retain(5)]            — no change (format doesn't shift positions)
> ```
>
> The **complexity** of OT comes from this combinatorial matrix. For N operation types, you need O(N²) transform pairs. Adding rich text formatting operations (bold, italic, font, size, color, link, heading level, list type, table operations...) dramatically increases the number of pairs."

**Interviewer:**
Good. Now walk me through the client-server protocol.

**Candidate:**

> "The client-server protocol is based on the **Jupiter protocol** with a **three-state client model** (documented in the Google Wave OT whitepaper by David Wang, 2009):
>
> ```
>                        ┌──────────────────┐
>                        │   SYNCHRONIZED   │
>                        │ No pending ops   │
>                        │ Client = Server  │
>                        └────────┬─────────┘
>                                 │ User makes an edit
>                                 │ → Send op to server
>                                 ▼
>                        ┌──────────────────┐
>                        │  AWAITING ACK    │
>                        │ 1 op in flight   │
>                        │ No buffer        │
>                        └────────┬─────────┘
>                                 │ User makes another edit
>                                 │ → Buffer it (don't send yet)
>                                 ▼
>                        ┌──────────────────┐
>                        │ AWAITING ACK +   │
>                        │ BUFFER           │
>                        │ 1 op in flight   │
>                        │ 1 op buffered    │
>                        └────────┬─────────┘
>                                 │ Server ACKs the in-flight op
>                                 │ → Send buffered op
>                                 │ → Back to AWAITING ACK
>                                 │
>                                 │ If user edits more while in this state
>                                 │ → compose new edit into the buffer
>                                 │   (don't grow the buffer unboundedly)
>                                 ▼
>                          ┌──────────────┐
>                          │ On server ACK│
>                          │ & empty      │
>                          │ buffer:      │
>                          │ → SYNCED     │
>                          └──────────────┘
> ```
>
> **Why only one in-flight operation?**
> If we allowed multiple unacknowledged operations, the server would need to track and transform against all of them. By limiting to one in-flight + one buffer, we simplify the protocol:
> - The buffer **composes** multiple local edits into a single operation (composition reduces the number of operations).
> - When the server ACKs the in-flight op, we send the buffer as the next operation.
>
> **What happens when the server sends an operation from another user?**
> While in AWAITING ACK or AWAITING ACK + BUFFER state:
> 1. The incoming server operation must be transformed against the client's in-flight operation (and buffer, if any).
> 2. The client's in-flight and buffered operations must also be transformed against the incoming operation.
> 3. This maintains the convergence invariant — both client and server will reach the same state.
>
> ```
> Client state: in-flight = op_A, buffer = op_B
> Server sends: op_S (from another user)
>
> Step 1: transform(op_A, op_S) → (op_A', op_S')
>   op_A' = what server already applied (our op, transformed by server)
>   op_S' = server's op, adjusted for our in-flight op
>
> Step 2: transform(op_B, op_S') → (op_B', op_S'')
>   op_S'' = server's op, adjusted for both our in-flight and buffer
>
> Step 3: Apply op_S'' to local document state
>   Update in-flight to op_A' (not needed for local state, but for future transforms)
>   Update buffer to op_B'
> ```"

**Interviewer:**
Now tell me why Google chose OT over CRDTs.

**Candidate:**

> "This is a critical architectural decision. Let me compare:
>
> | Aspect | OT (Google Docs) | CRDTs (Yjs, Automerge, Figma-inspired) |
> |---|---|---|
> | **Central server** | Required (for total ordering) | Not required — peer-to-peer possible |
> | **Convergence guarantee** | Transform function (TP1) | Mathematical commutativity built into data structure |
> | **Complexity** | O(N²) transform pairs for N op types | Simpler — no transform functions needed |
> | **Memory overhead** | Low — operations are compact | High — each character needs unique ID + tombstones for deleted chars |
> | **Server compute** | Transform on every operation | Minimal — merge is local |
> | **Offline support** | Limited (queue + transform on reconnect) | Excellent — merge any number of diverged states |
> | **Correctness** | Hard to prove correct (many published bugs — Imine et al. 2003) | Mathematically provable — commutativity is structural |
> | **Intent preservation** | Server can enforce intent (tiebreaking, ordering) | Weaker — mathematical convergence may not preserve intent |
> | **Maturity** | Deployed at Google scale for 15+ years | Newer — Yjs (2015+), Automerge (2017+) |
>
> **Why Google chose OT:**
> 1. **Google has reliable, low-latency servers.** The main disadvantage of OT (requires central server) is not a disadvantage for Google — they run the servers. CRDTs' advantage (no server needed) is irrelevant for Google's use case.
> 2. **Lower memory overhead.** CRDTs like RGA assign a unique ID to every character and keep tombstones for deleted characters. For a document with 1 million characters and 10 million total edits, the CRDT state could be 10x larger than the actual document. OT doesn't have this overhead.
> 3. **Server authority.** With OT, the server is the single source of truth and can enforce permissions, validation, and ordering. With CRDTs, there's no natural authority — any client can produce valid operations.
> 4. **Historical investment.** Google built the OT infrastructure for Wave in 2009 and evolved it for Docs. Switching to CRDTs would require rebuilding the entire collaboration stack.
>
> **When CRDTs win:**
> - **Offline-first apps** (Notion uses CRDT-inspired approach for block-level editing)
> - **Peer-to-peer** (no central server to depend on)
> - **Design tools** (Figma uses CRDT-inspired LWW registers + fractional indexing — works well for independent design objects)
>
> It's not that CRDTs are universally better or worse — the choice depends on the product's architecture and requirements. Google's centralized model makes OT the natural fit."

> *For the full deep dive on OT, see [03-operational-transformation.md](03-operational-transformation.md).*

#### Architecture Update After Phase 5

> | | Before (Phase 4) | After (Phase 5) |
> |---|---|---|
> | **OT Engine** | "Transforms concurrent operations" (handwave) | **Jupiter-based three-state client protocol, compound operations with retain/insert/delete/format, TP1 convergence, server-imposed total ordering** |
> | **Storage** | Operation log (unspecified) | *(still unspecified — next deep dive)* |
> | **Presence** | "Broadcast cursor positions" | *(still handwave — will deep dive)* |

---

### L5 vs L6 vs L7 — Phase 5 Rubric

| Aspect | L5 (SDE-2) | L6 (SDE-3) | L7 (Principal) |
|---|---|---|---|
| **OT operations** | "Insert and delete" | Explains compound operations (retain/insert/delete as document traversal), shows concrete transform examples for each case pair | Discusses the operation composition optimization (composing multiple edits into one), and the implications for undo (inverse operations must be transformed) |
| **Transform functions** | "Adjust positions when concurrent edits happen" | Traces through insert×insert, insert×delete, delete×delete, format×insert with concrete examples. Explains tiebreaking. | Discusses transform function correctness proofs, references Imine et al. (2003) finding bugs in published algorithms, explains why TP2 is not needed in client-server model |
| **Client protocol** | "Client sends edits to server" | Explains the three-state model (Synchronized/Awaiting ACK/Awaiting ACK+Buffer), composition in buffer, transform of server ops against in-flight+buffer | Discusses the protocol's relationship to Jupiter state space, explains the diamond property, discusses protocol recovery after network partition |
| **OT vs CRDT** | "OT and CRDTs both handle concurrent edits" | Compares on 5+ dimensions (server, memory, offline, correctness, intent). Explains why Google chose OT given their server infrastructure. | Discusses specific CRDT algorithms (RGA, YATA/Yjs), tombstone GC problem, interleaving anomalies (Kleppmann 2019). Explains Figma's hybrid approach (LWW + fractional indexing). |

---

## PHASE 6: Deep Dive — Document Storage & State Management (~8 min)

**Interviewer:**
Good OT deep dive. Now let's talk about storage. How is a Google Doc actually stored? It's not a `.docx` file, right?

**Candidate:**

> "Right — a Google Doc is **not a file**. It's a **structured data model** stored as an operation log with periodic snapshots. This is fundamentally different from file-based systems like Dropbox or Word.
>
> **Document representation:**
> The document is a sequence of **annotated items** — characters with formatting attributes, plus structural markers (paragraph breaks, heading markers, table delimiters). Think of it as a linear stream:
>
> ```
> Document: "Hello World"
> Internal representation:
>   [
>     {char: 'H', attrs: {bold: true, font: 'Arial', size: 11}},
>     {char: 'e', attrs: {bold: true, font: 'Arial', size: 11}},
>     {char: 'l', attrs: {bold: true, font: 'Arial', size: 11}},
>     {char: 'l', attrs: {bold: true, font: 'Arial', size: 11}},
>     {char: 'o', attrs: {bold: true, font: 'Arial', size: 11}},
>     {char: ' ', attrs: {font: 'Arial', size: 11}},
>     {char: 'W', attrs: {font: 'Arial', size: 11}},
>     ...
>   ]
> ```
>
> This is NOT stored as HTML or Markdown. It's a **custom intermediate representation** optimized for OT operations. HTML would make OT on tree structures necessary (which Google learned from Wave is too complex). Markdown would limit WYSIWYG formatting."

#### Event Sourcing: The Operation Log

> "**The operation log is the source of truth.** Every edit is an immutable event in an append-only log:
>
> ```
> Operation Log for doc-123:
>
> ┌─────┬────────┬───────────────────────────────────┬────────────────────────┐
> │ Rev │ UserID │ Operation                         │ Timestamp              │
> ├─────┼────────┼───────────────────────────────────┼────────────────────────┤
> │   1 │ alice  │ [insert("Hello")]                 │ 2026-02-21T10:00:00.000│
> │   2 │ alice  │ [retain(5), insert(" World")]     │ 2026-02-21T10:00:01.234│
> │   3 │ bob    │ [retain(5), delete(6)]            │ 2026-02-21T10:00:05.678│
> │   4 │ bob    │ [retain(5), insert(" Docs")]      │ 2026-02-21T10:00:06.012│
> │   5 │ alice  │ [format(0, 5, {bold: true})]      │ 2026-02-21T10:00:10.456│
> │ ... │ ...    │ ...                               │ ...                    │
> └─────┴────────┴───────────────────────────────────┴────────────────────────┘
>
> Document state at any revision = replay ops 1 through that revision
> Current state: replay all ops → "Hello Docs" (with "Hello" bolded)
> ```
>
> **Event sourcing benefits:**
> - **Complete audit trail**: Every edit, by whom, when.
> - **Revision history**: Navigate to any point in time by replaying to that revision.
> - **Undo**: Apply the inverse of an operation (but must transform the inverse against all subsequent operations — OT-aware undo).
> - **Debugging**: If the document is in a bad state, replay the log to find which operation caused it.
>
> **Event sourcing costs:**
> - **Log grows forever**: A heavily edited document could have millions of operations.
> - **Replay is slow**: Loading a fresh client means replaying the entire log. Hence, snapshots."

#### Snapshots (Compaction)

> "Snapshots solve the replay problem:
>
> ```
> Without snapshots:
>   Load doc = replay ops 1 through 1,000,000  →  SLOW (minutes)
>
> With snapshots:
>   Snapshot at op 999,900: full document state (50KB)
>   Load doc = load snapshot + replay ops 999,901-1,000,000 (100 ops)  →  FAST (milliseconds)
> ```
>
> **Snapshot strategy:**
> - Take a snapshot every N operations (e.g., 100-500) OR every M minutes of activity.
> - Store snapshots in blob storage (GCS / Colossus).
> - Keep the last K snapshots for revision history navigation. Older snapshots can be garbage collected.
> - The snapshot captures: full document content + formatting + comment anchors + cursor state.
>
> **Loading a document (the full flow):**
> 1. Client opens document → REST API returns document metadata (title, permissions, sharing info).
> 2. Client requests document content → server loads latest snapshot + replays operations since snapshot.
> 3. Client establishes WebSocket connection → server sends current revision number.
> 4. Client receives any operations from other editors that happened between step 2 and step 3.
> 5. Client renders document and begins accepting local edits."

#### Storage Infrastructure [INFERRED]

> "Google likely uses a combination of:
>
> | Data | Storage System | Why |
> |---|---|---|
> | **Operation log** | Bigtable or Spanner | Append-heavy, time-ordered, per-document partitioning. Bigtable excels at sequential writes with row-key = `(doc_id, revision)`. Spanner adds global consistency. |
> | **Document snapshots** | GCS (Google Cloud Storage) / Colossus | Large blobs (50KB-1MB), infrequent reads (only on document load). Blob storage is cheap and durable. |
> | **Document metadata** | Spanner | Structured data (title, owner, permissions, sharing settings). Needs strong consistency for permission checks. Globally replicated. |
> | **Presence & cursor state** | In-memory (OT server) | Ephemeral — not persisted. Lost on disconnect. Redis or in-memory maps on the OT server. |
>
> **Why Spanner for metadata?** Permission checks must be strongly consistent and globally available. If Alice removes Bob's edit access, Bob must be denied immediately, regardless of which region he's in. Spanner's TrueTime-based external consistency guarantees this.
>
> **Document size limit** (~1.02 million characters): This is likely a performance limit. OT complexity scales with document size — every operation references character positions, and transform functions must walk the document. Beyond ~1M characters, the OT server's performance degrades."

> *For the full deep dive on document storage, see [04-document-storage.md](04-document-storage.md).*

#### Architecture Update After Phase 6

> | | Before | After (Phase 6) |
> |---|---|---|
> | **Document Model** | Unspecified | **Linear annotated sequence (not HTML, not Markdown). Custom IR optimized for OT.** |
> | **Storage** | "Operation log" | **Event-sourced op log (Bigtable/Spanner) + periodic snapshots (GCS) + structured metadata (Spanner)** |
> | **Loading** | Unspecified | **Snapshot + replay recent ops + WebSocket handshake** |

---

### L5 vs L6 vs L7 — Phase 6 Rubric

| Aspect | L5 (SDE-2) | L6 (SDE-3) | L7 (Principal) |
|---|---|---|---|
| **Document model** | "Store the document as text/HTML" | Explains the annotated linear sequence model, why not HTML (tree OT complexity) and why not Markdown (WYSIWYG limitations) | Discusses the internal document tree structure (paragraphs, text runs, tables as elements), and how OT operations map to this structure |
| **Event sourcing** | "Save the document on every edit" | Explains operation log as source of truth, benefits (audit trail, revision history, undo), costs (unbounded growth, replay latency) | Discusses compaction strategies (time-based vs count-based vs hybrid), archive policies, cost modeling for petabytes of operation logs |
| **Storage infra** | "Use a database" | Identifies Bigtable for op log (append-heavy), GCS for snapshots (blobs), Spanner for metadata (consistency). Justifies each choice. | Discusses Spanner's TrueTime for globally consistent permission checks, Bigtable row key design for efficient per-document reads, snapshot storage lifecycle management |
| **Loading flow** | "Client requests the document" | 5-step loading flow: metadata → snapshot + replay → WebSocket → catch-up → render | Discusses cold-start latency optimization, pre-warming for popular documents, edge caching of snapshots |

---

## PHASE 7: Deep Dive — Cursor Synchronization & Presence (~5 min)

**Interviewer:**
Let's talk about the colored cursors — how do you show other users' cursors and selections in real-time?

**Candidate:**

> "Cursor presence is the visual indicator that makes collaboration feel 'real.' It seems simple but has interesting scaling challenges.
>
> **Cursor state per user:**
> ```
> {
>   userId: "alice",
>   displayName: "Alice Chen",
>   color: "#4285F4",           // consistent per user
>   cursorPosition: 142,         // character index
>   selectionStart: 142,         // same as cursor if no selection
>   selectionEnd: 142,
>   lastActive: "2026-02-21T10:05:00Z"
> }
> ```
>
> **Broadcast mechanism:**
> Cursor updates are sent over the same WebSocket channel as document operations. When Alice moves her cursor, the OT server relays the update to all other connected clients:
>
> ```
> Alice → Server: cursorUpdate(pos=142)
> Server → Bob:   cursorUpdate(userId=alice, pos=142)
> Server → Carol: cursorUpdate(userId=alice, pos=142)
> ```
>
> **Throttling is critical:**
> Users move their cursor constantly — clicking, arrow keys, mouse movement, typing. Sending every cursor movement would flood the WebSocket:
> - A fast typist moves the cursor 5+ times per second.
> - Mouse selection changes cursor continuously.
>
> Solution: **Throttle cursor updates to ~10-20 per second per user.** On the client, interpolate between received positions for smooth cursor animation. The cursor doesn't teleport — it glides.
>
> **Cursor position stability under edits:**
> When another user edits the document, all cursor positions must be adjusted. This uses the same OT transform logic:
>
> ```
> Alice's cursor is at position 100.
> Bob inserts 5 characters at position 50.
>
> Alice's cursor must shift: 100 → 105
> (Bob's insert was before Alice's cursor, so everything after shifts right by 5)
>
> Bob deletes 3 characters at position 120.
> Alice's cursor stays at 105.
> (Bob's delete was after Alice's cursor — no shift needed)
> ```
>
> **Scaling challenge — O(N²):**
> With N editors, each sending cursor updates, and each update broadcast to N-1 others:
> - 100 editors × 10 updates/sec = 1,000 updates/sec
> - Each broadcast to 99 others = 99,000 cursor messages/sec
>
> **Mitigations:**
> 1. **Viewport-based culling**: Only show cursors for users editing text visible in the current viewport. No need to render a cursor that's 50 pages away.
> 2. **Reduced frequency for distant cursors**: Cursors outside the viewport get updates at 1-2/sec instead of 10-20/sec.
> 3. **The 100-editor cap**: Google limits simultaneous editors to 100 specifically to bound this O(N²) broadcast.
> 4. **Server-side aggregation**: Batch cursor updates — send one combined update with all cursor positions every 50-100ms instead of individual messages.
>
> **User colors:**
> Each user is assigned a distinct color from a fixed palette. Colors should be visually distinguishable — high contrast against white background. Google uses ~20 distinct colors and assigns them based on join order. Colors persist within a session but may change across sessions.
>
> **Presence indicators:**
> The document header shows avatars of all users currently viewing/editing. Presence is ephemeral:
> - Maintained via WebSocket heartbeat (ping every 30 seconds).
> - If heartbeat fails for 30 seconds → user marked as disconnected → avatar removed.
> - On reconnect → avatar reappears."

> *For the full deep dive on cursor and presence, see [05-cursor-and-presence.md](05-cursor-and-presence.md).*

---

### L5 vs L6 vs L7 — Phase 7 Rubric

| Aspect | L5 (SDE-2) | L6 (SDE-3) | L7 (Principal) |
|---|---|---|---|
| **Cursor updates** | "Send cursor position over WebSocket" | Explains throttling (10-20/sec), interpolation on client, position adjustment via OT transforms | Discusses cursor update batching strategies, priority queuing (nearby cursors get higher priority), viewport-aware subscription |
| **Scaling** | Doesn't consider N² problem | Identifies O(N²) broadcast, explains mitigations (viewport culling, reduced frequency, 100-editor cap) | Proposes server-side cursor aggregation service, discusses the tradeoff between cursor update frequency and WebSocket bandwidth |
| **Presence** | "Show who's online" | Heartbeat-based presence, disconnect timeout, avatar display | Discusses presence across multiple tabs/devices (same user, multiple sessions), presence state machine, conflict between sessions |

---

## PHASE 8: Deep Dive — Conflict Resolution & Consistency (~5 min)

**Interviewer:**
Let's talk about edge cases. What happens when operations conflict in tricky ways?

**Candidate:**

> "The OT engine handles most conflicts automatically, but there are interesting edge cases.
>
> **The fundamental guarantee — convergence:**
> All clients will eventually see the same document content, regardless of the order operations arrive. This is enforced by the server's total ordering + OT transforms.
>
> **Edge Case 1: Insert-Insert at same position**
> ```
> Alice inserts "X" at pos 5. Bob inserts "Y" at pos 5. (Both based on same revision.)
>
> Server receives Alice first → applies insert(5, "X") → "...X..."
> Server transforms Bob's insert(5, "Y") against Alice's insert(5, "X"):
>   Same position → tiebreak by user ID (lower ID goes first)
>   If alice < bob: Bob's insert shifts to pos 6 → "...XY..."
>
> Result: "...XY..." — deterministic, same on all clients.
> ```
>
> **Edge Case 2: Delete-Delete overlap**
> ```
> Alice deletes chars 5-10. Bob deletes chars 8-15. (Overlapping ranges.)
>
> Server receives Alice first → deletes 5-10 → document shrinks by 5
> Server transforms Bob's delete(8-15):
>   Chars 8-10 already deleted by Alice → skip those
>   Chars 11-15 still exist but shifted → delete(5-10) in new positions
>
> No double deletion — the overlap is handled correctly.
> ```
>
> **Edge Case 3: Comment on deleted text**
> ```
> Alice adds a comment anchored to chars 10-20 ("this needs work").
> Bob deletes chars 5-25 (which includes the commented text).
>
> The comment anchor [10, 20] is within the deleted range.
> The comment becomes orphaned — displayed as "the text you commented on was deleted."
> The comment is NOT deleted — Bob might not have seen it.
> ```
>
> **Edge Case 4: Permission change during active editing**
> ```
> Alice is editing (editor role).
> Owner changes Alice's role to viewer while Alice is mid-edit.
>
> Server detects permission change → pushes notification over WebSocket.
> Alice's client switches to read-only mode.
> Any pending (unsent) operations from Alice are discarded.
> In-flight operation (already sent to server) may be rejected — server checks permission on every operation.
> ```
>
> **Edge Case 5: Offline reconnection with massive divergence**
> ```
> Alice goes offline for 2 hours and types 500 local operations.
> Meanwhile, Bob and Carol make 2,000 operations on the server.
>
> On reconnect:
> 1. Server receives Alice's 500 ops, all based on a revision 2,000 ops behind.
> 2. Server must transform Alice's 500 ops against 2,000 server ops.
> 3. Total transforms: 500 × 2,000 = 1,000,000 transform operations.
> 4. This takes time — possibly seconds. During this, Alice's UI shows "Syncing..."
> 5. The result preserves Alice's edits, but they're interleaved with Bob and Carol's.
> ```
>
> **Consistency guarantee summary:**
>
> | Property | Guarantee |
> |---|---|
> | **Convergence** | All clients reach the same document state (given no new edits) |
> | **Intention preservation** | Each operation's effect is preserved in spirit (bold chars 5-10 → the same text is bolded, even if positions shifted) |
> | **Causality** | If operation A happened before B (A caused B), all clients see A before B |
> | **Total ordering** | The server imposes a single, deterministic order on all operations |"

> *For the full deep dive, see [06-conflict-resolution-and-consistency.md](06-conflict-resolution-and-consistency.md).*

---

### L5 vs L6 vs L7 — Phase 8 Rubric

| Aspect | L5 (SDE-2) | L6 (SDE-3) | L7 (Principal) |
|---|---|---|---|
| **Conflict examples** | "OT handles conflicts" | Traces through specific edge cases (same-position insert, overlapping delete, comment on deleted text) with concrete examples | Discusses intention preservation failures (when OT's mathematical convergence doesn't match user intent), undo challenges in OT, and why some conflicts are policy decisions not algorithmic ones |
| **Offline reconciliation** | "Sync when back online" | Explains the O(M×N) transform cost, discusses the "Syncing..." UX, identifies interleaving as a user-facing problem | Proposes mitigation strategies (batch offline ops into coarser operations, regional conflict resolution, pre-transform on the client before sending to server) |
| **Consistency model** | "All users see the same thing" | Explains convergence, intention preservation, causality, total ordering as distinct properties | Discusses the relationship between OT convergence and distributed systems consistency models (linearizability, causal consistency), and why OT convergence is a stronger guarantee than eventual consistency |

---

## PHASE 9: Deep Dive — Sharing, Permissions & Security (~5 min)

**Interviewer:**
Let's briefly cover the permission model. How do you enforce that viewers can't edit?

**Candidate:**

> "**Permission levels** (ordered by capability):
>
> | Level | Can View | Can Comment | Can Suggest | Can Edit | Can Share | Can Delete |
> |---|---|---|---|---|---|---|
> | **Viewer** | Yes | No | No | No | No | No |
> | **Commenter** | Yes | Yes | Yes | No | No | No |
> | **Editor** | Yes | Yes | Yes | Yes | No | No |
> | **Owner** | Yes | Yes | Yes | Yes | Yes | Yes |
>
> **Enforcement points — defense in depth:**
>
> 1. **WebSocket connection**: When a client connects, the server checks the user's permission level. Viewers get a read-only connection — their operations are rejected at the protocol level.
>
> 2. **Every operation**: Even for editors, the server validates each operation before applying it. This catches bugs in the client or malicious clients.
>
> 3. **API endpoints**: REST APIs for metadata, comments, sharing all check permissions. Rate-limited to prevent abuse.
>
> **Sharing mechanisms:**
> - **Direct sharing**: Share with specific email addresses → stores ACL entries in Spanner: `(doc_id, user_email) → permission_level`
> - **Link sharing**: Generate a shareable link with configurable access (view/comment/edit). Anyone with the link can access. Can restrict to 'anyone in organization' for Google Workspace domains.
> - **Domain-level sharing**: Google Workspace admins can set org-wide policies.
>
> **Real-time permission changes:**
> If the owner downgrades Alice from editor to viewer while Alice is editing:
> 1. Permission change is written to Spanner (strongly consistent).
> 2. Server pushes a permission change notification over Alice's WebSocket.
> 3. Alice's client switches to read-only mode.
> 4. Any pending unsent operations are discarded.
> 5. In-flight operations are rejected by the server (which re-checks permissions).
>
> **Permission check performance:**
> Every operation triggers a permission check. At 500 ops/sec for a busy document, that's 500 permission checks/sec. Solution: **cache the ACL on the OT server**. Invalidate on permission change (push-based invalidation). The cache TTL is very short (seconds) — permission changes must take effect quickly."

> *For the full deep dive, see [08-permission-and-sharing.md](08-permission-and-sharing.md).*

---

## PHASE 10: Deep Dive — Scaling & Reliability (~5 min)

**Interviewer:**
Let's zoom out. How does this system handle Google-scale traffic? What are the bottlenecks?

**Candidate:**

> "**The key bottleneck is per-document serialization.** Each document's OT operations must be processed by a single coordination point (for total ordering). This is inherently serial per document — you can't parallelize OT across multiple servers for the same document.
>
> **Scaling strategy: shard by document, not by user**
>
> ```
> Document → hash(doc_id) → OT Server shard
>
> OT Server A: doc-1, doc-5, doc-9, ...
> OT Server B: doc-2, doc-6, doc-10, ...
> OT Server C: doc-3, doc-7, doc-11, ...
> ...
>
> 100 million active documents / 10,000 OT servers = 10,000 docs per server
> Most documents have 1-3 active editors → low load per document
> A few documents have 100 editors → those are the hot ones
> ```
>
> **Hot document handling:**
> A document with 100 active editors is a hot partition. The OT server for that document handles:
> - 500 ops/sec (100 editors × 5 ops/sec)
> - 100 WebSocket connections
> - 100K cursor messages/sec
>
> This is manageable for a single server, but it means that document can't be split across servers. The 100-editor cap is partly to bound this per-server load.
>
> **WebSocket scaling:**
> 100 million concurrent WebSocket connections is the real infrastructure challenge. Each connection is stateful, long-lived, and consumes a file descriptor + memory:
>
> | Resource | Per Connection | 100M Connections |
> |---|---|---|
> | Memory (kernel buffers + app state) | ~50 KB | 5 TB |
> | File descriptors | 1 | 100M |
> | Heartbeat bandwidth (ping/pong 30s) | ~100 B/30s | 333 MB/s |
>
> Solution: **Connection Gateway** — a fleet of thousands of servers that terminate WebSockets and route messages to the appropriate OT server. The gateway is stateless per-connection (it just routes). The OT server is stateful per-document.
>
> **Failover:**
> If an OT server crashes:
> 1. Gateway detects the failure (health check, timeout).
> 2. Gateway assigns the document to a new OT server (the standby or next in the consistent hash ring).
> 3. New OT server loads: latest snapshot + replay operations since snapshot from the durable operation log.
> 4. New OT server is ready to accept operations.
> 5. Clients reconnect (gateway routes them to the new server).
> 6. Total failover time: seconds (snapshot load + op replay). During failover, edits are queued on clients (same as offline mode).
>
> **Global latency:**
> A user in Tokyo editing a document whose OT server is in Virginia sees ~150ms round-trip. But with **optimistic local apply**, the user doesn't feel this latency — their edits appear instantly, and other users' edits arrive with ~150ms delay (acceptable).
>
> For documents with editors spread globally, Google may place the OT server in the region with the most active editors, minimizing latency for the majority.
>
> **Monitoring:**
>
> | Metric | What It Tells You | Alert Threshold |
> |---|---|---|
> | OT transform latency (p99) | OT server performance | > 50ms |
> | Operations/sec per document | Document activity level | > 1000 (potential abuse or bot) |
> | WebSocket connection count | Infrastructure load | > capacity |
> | Operation log growth rate | Storage cost trajectory | Abnormal spike |
> | Snapshot frequency | Compaction health | Too infrequent → slow loads |
> | Permission check latency | Auth performance | > 10ms |
> | Client-to-server round trip | User experience | > 500ms |
> | Document load time (p99) | User experience | > 3 seconds |"

> *For the full deep dive, see [10-scaling-and-reliability.md](10-scaling-and-reliability.md).*

---

### L5 vs L6 vs L7 — Phase 10 Rubric

| Aspect | L5 (SDE-2) | L6 (SDE-3) | L7 (Principal) |
|---|---|---|---|
| **Scaling model** | "Add more servers" | Explains per-document sharding, identifies the serialization bottleneck, calculates per-server load | Discusses OT server memory management (evicting idle documents), document migration between servers, hot document detection and proactive scaling |
| **WebSocket infra** | "Use WebSockets" | Calculates resource consumption for 100M connections, proposes connection gateway architecture | Discusses WebSocket protocol-level optimizations (binary framing, compression), connection draining during deploys, graceful degradation under load |
| **Failover** | "Have backup servers" | Describes the failover flow (detect → reassign → load state → reconnect), estimates failover time | Discusses blast radius containment (one OT server crash affects only its documents), cell-based architecture, automated failover vs manual intervention |
| **Monitoring** | "Monitor the servers" | Proposes 8+ specific metrics with alert thresholds | Discusses anomaly detection (sudden drop in ops = possible outage), capacity planning (ops/sec growth trends), runbook automation |

---

## PHASE 11: Wrap-Up & Summary (~3 min)

**Interviewer:**
We're running low on time. Summarize your design and tell me what keeps you up at night operating this system.

**Candidate:**

> "**Design evolution — how we got here:**
>
> | Attempt | Architecture | Key Innovation |
> |---|---|---|
> | **0** | Single server, save full document | Baseline — last-writer-wins, no collaboration |
> | **1** | Operation-based editing + op log | Event sourcing — efficient edits, revision history, undo |
> | **2** | OT engine + WebSocket | Real-time collaboration — concurrent edits converge via OT |
> | **3** | Snapshots + permissions + offline | Production basics — fast loading, access control, offline queue |
> | **4** | Per-doc routing, global, rich text | Scale — horizontal sharding, failover, formatting operations |
>
> **Final architecture components:**
>
> | Component | Design Choice | Why |
> |---|---|---|
> | **Collaboration model** | Centralized OT (Jupiter protocol) | Server authority, simpler than peer-to-peer, proven at Google scale |
> | **Communication** | WebSocket (persistent, bidirectional) | Sub-200ms latency for real-time editing |
> | **Client protocol** | Three-state model (Synced / Awaiting ACK / Awaiting ACK + Buffer) | Handles network latency gracefully, limits in-flight operations |
> | **Document model** | Linear annotated sequence | Optimized for OT — simpler than tree (Wave lesson), richer than plain text |
> | **Storage** | Event-sourced operation log + periodic snapshots | Complete history, efficient loading, durable |
> | **Storage infra** | Bigtable (op log) + GCS (snapshots) + Spanner (metadata) | Each optimized for its access pattern [INFERRED] |
> | **Scaling** | Per-document sharding to OT server pool | Each document is independent; bottleneck is per-document serialization |
> | **Failover** | Op log replay on new server | Stateful servers, but state is reconstructible from durable log |
> | **Presence** | Throttled cursor broadcasts over WebSocket | O(N²) bounded by 100-editor cap and viewport culling |
> | **Permissions** | Owner/Editor/Commenter/Viewer, cached ACL | Defense in depth — checked on connect + every operation |
>
> **What keeps me up at night:**
>
> 1. **OT correctness bugs.** OT transform functions are notoriously hard to get right. Imine et al. (2003) found bugs in multiple published algorithms. A bug in a transform function means documents can **diverge** — Alice and Bob see different content, and they don't know it. This is the worst failure mode because it's **silent**. Detection: periodically, clients send a checksum of their document state to the server. If checksums diverge → force reload from server state. Mitigation: extensive property-based testing of transform function pairs.
>
> 2. **Hot documents.** A viral Google Doc (shared by a CEO to 10,000 employees, everyone opens it at once) can overwhelm the OT server assigned to that document. 100 editors is the cap, but even 100 editors generating 500 ops/sec with cursor broadcasts is significant. If we need to move a hot document to a beefier server, that requires draining the WebSocket connections and migrating in-memory state — disruptive.
>
> 3. **Operational log corruption.** The operation log is the source of truth. If it's corrupted (bad write, storage failure), we lose document history. Even with Spanner's strong consistency and replication, a logical corruption (a bug writes an invalid operation to the log) can poison the document. Mitigation: validate every operation before appending, checksum the log, take frequent snapshots as recovery points.
>
> 4. **Global latency tail.** Optimistic local apply hides latency for your own edits. But seeing other users' edits is delayed by the round-trip to the OT server. If the OT server is in a distant region, collaborators in other regions see each other's edits with 200-300ms delay — noticeable. Worse, if the OT server region has a network issue, all editors worldwide are affected. Regional OT servers help, but migrating a document's OT between regions mid-editing is non-trivial.
>
> 5. **Offline reconciliation surprise.** A user who edited offline for hours reconnects and their edits are interleaved with everyone else's work. The OT result is mathematically correct (convergence preserved), but the user may be surprised — their carefully written paragraph is now mixed with Bob's edits. This is a **product problem** more than a technical one — we need to show the user what changed during their offline period and let them review.
>
> **Potential extensions:**
> - **Suggestion mode**: Tracked changes with accept/reject workflow. Suggestions are operations that are displayed but not applied to the canonical document until accepted.
> - **Real-time commenting**: Comments anchored to text ranges, with OT-adjusted anchors. Comment threads with @mentions and notifications.
> - **Import/export**: PDF, DOCX, ODT, HTML export. DOCX import with formatting preservation.
> - **AI integration**: Smart Compose, grammar suggestions, summarization — all operating on the same document model.
> - **Conflict-free collaboration at scale**: Investigating CRDTs for specific use cases (e.g., Notion-style block editing where blocks are independent)."

---

## Interviewer's Final Assessment

**Hire Recommendation: Strong Hire (L6 — solid SDE-3 with demonstrated depth)**

| Dimension | Rating | Notes |
|---|---|---|
| **Problem Decomposition** | Exceeds Bar | Clean iterative evolution from single server to production architecture. Each step motivated by specific problems. |
| **Requirements & Scoping** | Exceeds Bar | Strong functional/non-functional separation. Knew OT vs CRDT tradeoffs. Proactively scoped offline as secondary. |
| **OT Deep Dive** | Exceeds Bar | Traced through transform functions with concrete examples. Explained Jupiter protocol, three-state client model, TP1 convergence. Knew the Wave history. |
| **Storage Design** | Meets Bar | Event sourcing with snapshots. Reasonable storage layer choices (Bigtable/GCS/Spanner). Correctly identified snapshot compaction as critical. |
| **Cursor/Presence** | Meets Bar | Identified O(N²) scaling, proposed throttling and viewport culling. |
| **Conflict Resolution** | Exceeds Bar | Traced through 5 specific edge cases. Understood intention preservation and offline reconciliation cost. |
| **Permission Model** | Meets Bar | Four-level model with defense-in-depth enforcement. Real-time permission change handling. |
| **Scaling** | Exceeds Bar | Per-document sharding, connection gateway, failover via op log replay. Concrete resource calculations. |
| **Operational Maturity** | Exceeds Bar | "What keeps you up at night" identified 5 concrete risks with mitigations. OT correctness bugs and silent divergence was particularly insightful. |
| **Communication** | Exceeds Bar | Drove the conversation, used diagrams and tables, natural iterative progression. |

**What would push this to L7:**
- Deeper discussion of OT correctness proofs and testing strategies (property-based testing, fuzzing, formal verification)
- Proposing a specific cell-based failure isolation architecture
- Discussing the organizational complexity of maintaining OT transform functions as the document model evolves (adding new element types requires updating every transform pair)
- Cost modeling: operations/sec → compute cost, storage cost at petabyte scale, WebSocket connection cost
- Discussing how to migrate from OT to CRDT (or a hybrid) without disrupting 3 billion users

---

## Key Differences: L5 vs L6 vs L7 Expectations for This Problem

| Aspect | L5 (SDE-2) | L6 (SDE-3) | L7 (Principal) |
|---|---|---|---|
| **OT understanding** | "OT resolves edit conflicts" | Explains transform functions with examples, Jupiter protocol, three-state client model, TP1 convergence. Compares OT vs CRDT on 5+ dimensions. | References specific papers and known bugs. Discusses transform correctness proofs, TP1 vs TP2, composition optimization, OT-aware undo. |
| **Architecture** | "WebSocket server + database" | Iterative evolution with clear problem→solution at each step. Per-document sharding. Connection gateway. | Cell-based architecture, blast radius isolation, multi-region OT placement, document migration protocol. |
| **Storage** | "Store documents in a database" | Event sourcing with snapshots, appropriate storage systems for each data type | Discusses compaction strategies, operation log archival policies, cost per petabyte, snapshot lifecycle management |
| **Scaling** | "Scale horizontally" | Per-document serialization bottleneck, 100-editor cap rationale, WebSocket connection resource calculation | Hot document detection and proactive migration, capacity planning, connection draining during deploys |
| **Conflict resolution** | "OT handles it" | Traces through 5+ edge cases with concrete examples | Discusses intention preservation failures, undo in concurrent editing, policy vs algorithm decisions |
| **Operational thinking** | "Monitor the system" | 8+ specific metrics with alert thresholds. 5 specific "keeps me up at night" risks. | Runbook automation, blast radius analysis, chaos engineering for OT servers, progressive rollout of transform function changes |
| **Presence** | "Show cursors" | O(N²) analysis, throttling, viewport culling | Multi-device presence, presence state machine, cursor interpolation algorithms |
| **Permissions** | "Add access control" | 4-level model, defense-in-depth, real-time permission changes | Permission inheritance (folder-level), cross-organization sharing, compliance (audit trails, data residency) |

---

*For detailed deep dives on each component, see the companion documents:*
- [API Contracts](02-api-contracts.md) — Collaborative Editor APIs with WebSocket and REST
- [Operational Transformation](03-operational-transformation.md) — OT algorithm deep dive, transform functions, Jupiter protocol
- [Document Storage](04-document-storage.md) — Operation log, snapshots, storage infrastructure
- [Cursor & Presence](05-cursor-and-presence.md) — Cursor synchronization, presence, scaling
- [Conflict Resolution & Consistency](06-conflict-resolution-and-consistency.md) — Edge cases, convergence guarantees
- [Offline Support](07-offline-support.md) — Offline editing, reconnection sync
- [Permission & Sharing](08-permission-and-sharing.md) — Access control, sharing model
- [Rich Text & Formatting](09-rich-text-and-formatting.md) — Document model, formatting operations
- [Scaling & Reliability](10-scaling-and-reliability.md) — Infrastructure, failover, monitoring
- [Design Trade-offs](11-design-trade-offs.md) — OT vs CRDT, event sourcing, architectural choices

*End of interview simulation.*
