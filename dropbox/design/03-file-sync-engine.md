# Deep Dive: File Sync Engine — The Defining Feature of Dropbox

> **Context:** The sync engine is what makes Dropbox "Dropbox." Without it, you just have cloud storage (like S3 with a UI). This document covers the client-side sync engine, delta sync protocol, the Nucleus rewrite, and how changes propagate between devices.

---

## Opening

**Interviewer:**

Let's deep-dive into the sync engine. Walk me through how a file edit on one device ends up on all other devices.

**Candidate:**

> The sync engine is the single most complex component in Dropbox's architecture. It has to solve three simultaneous problems:
>
> 1. **Detect local changes** — watch the file system for edits, creates, deletes, moves
> 2. **Compute minimal diff** — figure out exactly which bytes changed and upload only those
> 3. **Apply remote changes** — download changes from other devices/collaborators and reconstruct files locally
>
> And it must do all three concurrently, reliably, across three different operating systems, while handling conflicts, offline edits, and millions of files.
>
> Let me walk through the entire pipeline.

---

## 1. Client-Side Sync Engine Architecture

**Candidate:**

> Here's the complete sync pipeline from file edit to all devices updated:
>
> ```
> ┌─────────────────────────────────────────────────────────────┐
> │                    CLIENT A (Source)                         │
> │                                                             │
> │  ┌──────────────┐    ┌──────────────┐    ┌──────────────┐  │
> │  │  File System  │───>│  File Watcher │───>│  Change      │  │
> │  │  (local disk) │    │  (OS events)  │    │  Detector    │  │
> │  └──────────────┘    └──────────────┘    └──────┬───────┘  │
> │                                                  │          │
> │                                                  ▼          │
> │  ┌──────────────┐    ┌──────────────┐    ┌──────────────┐  │
> │  │  Block Hasher │<───│  Chunker     │<───│  Change      │  │
> │  │  (SHA-256)    │    │  (4MB blocks) │    │  Queue       │  │
> │  └──────┬───────┘    └──────────────┘    └──────────────┘  │
> │         │                                                   │
> │         ▼                                                   │
> │  ┌──────────────┐    ┌──────────────┐    ┌──────────────┐  │
> │  │  Dedup Check  │───>│  Uploader    │───>│  Metadata    │  │
> │  │  (has_blocks?) │    │  (new blocks) │    │  Commit      │  │
> │  └──────────────┘    └──────────────┘    └──────────────┘  │
> └─────────────────────────────────────────────────────────────┘
>                              │
>                              ▼
> ┌─────────────────────────────────────────────────────────────┐
> │                        SERVER                               │
> │                                                             │
> │  ┌──────────────┐    ┌──────────────┐    ┌──────────────┐  │
> │  │  Block Store  │    │  Metadata    │    │  Notification │  │
> │  │  (Magic       │    │  Service     │    │  Service      │  │
> │  │   Pocket)     │    │  (Edgestore) │    │  (longpoll)   │  │
> │  └──────────────┘    └──────────────┘    └──────┬───────┘  │
> └─────────────────────────────────────────────────────────────┘
>                              │
>                              ▼
> ┌─────────────────────────────────────────────────────────────┐
> │                    CLIENT B (Destination)                    │
> │                                                             │
> │  ┌──────────────┐    ┌──────────────┐    ┌──────────────┐  │
> │  │  Longpoll     │───>│  Change      │───>│  Block       │  │
> │  │  Listener     │    │  Fetcher     │    │  Downloader  │  │
> │  └──────────────┘    └──────────────┘    └──────┬───────┘  │
> │                                                  │          │
> │                                                  ▼          │
> │  ┌──────────────┐    ┌──────────────┐    ┌──────────────┐  │
> │  │  File         │<───│  Block       │<───│  Diff        │  │
> │  │  Writer       │    │  Assembler   │    │  Calculator  │  │
> │  └──────────────┘    └──────────────┘    └──────────────┘  │
> └─────────────────────────────────────────────────────────────┘
> ```

### 1.1 File Watcher — OS-Level Change Detection

> The first step is detecting that a file changed. Dropbox uses **OS-native file system event APIs**:
>
> | OS | API | Capabilities | Limitations |
> |----|-----|-------------|-------------|
> | **Linux** | `inotify` | Per-file/directory watches, events: IN_CREATE, IN_MODIFY, IN_DELETE, IN_MOVED_FROM/TO | Watch limit per user (default 8192, Dropbox increases via sysctl). Each directory needs a separate watch. |
> | **macOS** | `FSEvents` | Per-directory recursive watches, coalesced events, persistent event IDs | Events are coalesced (may miss intermediate states). Less granular than inotify. |
> | **Windows** | `ReadDirectoryChangesW` | Per-directory watches with subtree option | Buffer overflow on rapid changes (many changes in a short window). Dropbox must handle overflow by doing a full directory scan. |
>
> **The watcher problem at scale:**
> A user with 500,000 files across 10,000 directories needs 10,000 inotify watches on Linux. The default kernel limit is 8,192. Dropbox increases this limit on installation. On macOS, FSEvents handles recursive watching natively (one watch per tree), so this is less of an issue.
>
> **Fallback: periodic scanning.** If the OS event API fails (buffer overflow, watch limit exceeded), Dropbox falls back to periodic full-directory scans. This is slower but correct. The scan computes file hashes and compares with the last known state.

### 1.2 Change Detection and Queuing

> When the file watcher fires an event, the sync engine doesn't immediately start syncing. Instead:
>
> 1. **Debounce**: Wait a short period (e.g., 500ms) for the file to stabilize. Applications like Word/Excel save files in multiple steps (write temp file → rename). Syncing mid-save would upload a corrupt intermediate state.
>
> 2. **Dequeue and prioritize**: Changes are queued and processed in priority order:
>    - Small files first (quick wins, user sees fast sync)
>    - Recently modified files over old changes
>    - User's active files over background sync
>
> 3. **Coalesce**: If the same file changes 5 times in 10 seconds (user is actively editing), only the final state is synced — not all 5 intermediate versions. This reduces upload volume dramatically.

---

## 2. Delta Sync — The Core Innovation

**Interviewer:**

Explain the delta sync mechanism. Why is it such a big deal?

**Candidate:**

> Delta sync is Dropbox's core technical differentiator. The idea: **only upload/download the bytes that actually changed, not the entire file.**
>
> ### How it works:
>
> 1. **Split file into 4 MB blocks.** A 100 MB file → 25 blocks.
> 2. **SHA-256 hash each block.** Each block gets a 32-byte fingerprint.
> 3. **Compare with server's block list.** The server stores the ordered list of block hashes for the current version.
> 4. **Upload only changed blocks.** If blocks 7 and 18 changed → upload 8 MB, not 100 MB.
>
> ### Concrete example:
>
> ```
> File: presentation.pptx (100 MB, 25 blocks)
>
> Previous version (on server):
>   Block list: [hash_0, hash_1, hash_2, ..., hash_6, hash_7_old, hash_8, ..., hash_24]
>
> Current version (on client after edit):
>   Block list: [hash_0, hash_1, hash_2, ..., hash_6, hash_7_NEW, hash_8, ..., hash_24]
>
> Delta: Only block 7 changed (user edited a few slides in the middle).
>
> Upload: 4 MB (block 7 only)
> Savings: 96 MB saved → 96% bandwidth reduction
> ```
>
> ### Bandwidth savings math:
>
> | Scenario | Without delta sync | With delta sync | Savings |
> |----------|-------------------|-----------------|---------|
> | Edit 1 slide in 100 MB pptx | Upload 100 MB | Upload ~4 MB (1 block) | **96%** |
> | Add a paragraph to a 50 MB doc | Upload 50 MB | Upload ~4 MB (1 block) | **92%** |
> | Append 1 KB to a 1 GB log file | Upload 1 GB | Upload ~4 MB (last block) | **99.6%** |
> | New 500 MB file (no previous version) | Upload 500 MB | Upload 500 MB (no delta available) | **0%** |
> | 500 MB file, 99% identical to another user's file | Upload 500 MB | Upload ~5 MB (dedup across users!) | **99%** |
>
> **At Dropbox's scale (1.2 billion files synced/day), delta sync reduces bandwidth by an order of magnitude.** This is the primary reason Dropbox can offer its service profitably.

### 2.1 Upload Path (Client → Server)

```
Sequence: File Edit → Sync Upload

Client                                    Server
  │                                          │
  │  1. File watcher detects change          │
  │  2. Read file, split into 4MB blocks     │
  │  3. SHA-256 hash each block              │
  │  4. Compare with cached server block list │
  │  5. Identify changed blocks              │
  │                                          │
  │──── POST /files/upload_session/start ───>│
  │<─── { session_id: "abc123" } ───────────│
  │                                          │
  │──── POST /upload_session/append_v2 ─────>│  (block 7 data, 4 MB)
  │<─── 200 OK ─────────────────────────────│
  │                                          │
  │──── POST /upload_session/finish ────────>│
  │     { commit: { path, mode: {update: rev} } }
  │                                          │
  │                                   6. Server verifies block hashes
  │                                   7. Store new blocks in Magic Pocket
  │                                   8. Update metadata (file → new block list)
  │                                   9. Increment change journal cursor
  │                                  10. Notify waiting longpoll clients
  │                                          │
  │<─── 200 { name, rev, content_hash } ───│
  │                                          │
  │  11. Update local metadata cache         │
  │  12. Mark file as UP_TO_DATE             │
```

### 2.2 Download Path (Server → Client)

```
Sequence: Remote Change → Local Sync

Client B                                  Server
  │                                          │
  │──── POST /list_folder/longpoll ─────────>│
  │     { cursor: "current_cursor" }         │
  │                                          │
  │     ... server holds connection ...      │
  │                                          │
  │     (Client A uploads a change)          │
  │                                          │
  │<─── { changes: true } ──────────────────│
  │                                          │
  │──── POST /list_folder/continue ─────────>│
  │     { cursor: "current_cursor" }         │
  │                                          │
  │<─── { entries: [{file, new_metadata}] } ─│
  │                                          │
  │  1. Compare new block list with local    │
  │  2. Identify blocks we don't have        │
  │                                          │
  │──── Download changed blocks ────────────>│
  │<─── Block data ─────────────────────────│
  │                                          │
  │  3. Reconstruct file from blocks         │
  │  4. Write to local disk                  │
  │  5. Update local metadata cache          │
  │  6. Update cursor                        │
  │                                          │
  │──── POST /list_folder/longpoll ─────────>│
  │     { cursor: "NEW_cursor" }             │
  │     ... waiting for next change ...      │
```

---

## 3. Fixed-Size Chunking vs Content-Defined Chunking

**Interviewer:**

You mentioned 4 MB fixed blocks. What happens when content is inserted at the beginning of a file?

**Candidate:**

> Excellent question — this exposes the key weakness of fixed-size chunking.
>
> ### The problem with fixed-size chunking:
>
> ```
> Original file (12 MB, 3 blocks):
>   [----Block 0----][----Block 1----][----Block 2----]
>   Bytes 0-4MB       Bytes 4-8MB      Bytes 8-12MB
>
> After inserting 1 KB at the beginning:
>   [----Block 0'----][----Block 1'----][----Block 2'----][Block 3']
>   Bytes 0-4MB        Bytes 4-8MB       Bytes 8-12MB     12-12.001MB
>
> Every block boundary shifted by 1 KB!
> Block 0' ≠ Block 0 (different content due to shift)
> Block 1' ≠ Block 1 (different content due to shift)
> Block 2' ≠ Block 2 (different content due to shift)
>
> Result: ALL 3 blocks are "changed" → upload 12 MB + 1 KB
> Expected: Only ~4 MB should need uploading (the block containing the insertion)
> ```
>
> **Fixed-size chunking turns a 1 KB insertion into a full re-upload.** This is catastrophic for files where content is prepended (log files with timestamps, certain database files).
>
> ### Content-Defined Chunking (CDC) — the solution:
>
> CDC uses a **rolling hash (Rabin fingerprint)** to define block boundaries based on content, not position:
>
> ```
> Algorithm:
>   1. Slide a window (e.g., 48 bytes) across the file
>   2. At each position, compute Rabin fingerprint of the window
>   3. If fingerprint matches a pattern (e.g., lower 13 bits are zero),
>      mark this position as a block boundary
>   4. Average block size ≈ 2^13 = 8 KB (configurable)
>
> Key property: Block boundaries are determined by LOCAL content,
> not global position. Inserting data at the beginning only affects
> the boundary near the insertion point — all other boundaries stay.
> ```
>
> ```
> Original file (CDC):
>   [--Block A--][------Block B------][---Block C---][--Block D--]
>   (boundaries determined by content patterns)
>
> After inserting 1 KB at the beginning:
>   [--Block A'--][------Block B------][---Block C---][--Block D--]
>   (only Block A changed; B, C, D boundaries are content-determined
>    and unaffected by the insertion)
>
> Result: Only Block A' is different → upload ~8 KB, not entire file
> ```
>
> ### Trade-offs:
>
> | Aspect | Fixed-Size (4 MB) | Content-Defined (CDC) |
> |--------|-------------------|----------------------|
> | **Simplicity** | Very simple — just divide by block size | Complex — rolling hash, variable-size blocks |
> | **Metadata overhead** | Predictable — file_size / 4MB block hashes per file | Variable — more blocks for small avg size, more metadata |
> | **Insertion resilience** | Poor — insertion shifts all boundaries | Excellent — only local boundary affected |
> | **Dedup across edits** | Good for appends, bad for insertions | Good for all edit types |
> | **Dedup across files** | Good at 4 MB granularity | Better — smaller blocks = more dedup opportunities |
> | **Used by** | Dropbox (4 MB blocks as primary) | restic, borgbackup, rsync |
>
> Dropbox reportedly uses a **hybrid approach**: fixed 4 MB blocks for storage and the primary dedup layer, with sub-block rolling hashes for detecting which portions within a block changed. [INFERRED — exact internal implementation not publicly documented]
>
> **Contrast with rsync:** rsync uses a rolling checksum (Adler-32) at the byte level, which gives the finest granularity but creates massive amounts of metadata. Dropbox's 4 MB block granularity is a balance between dedup efficiency and metadata overhead.

---

## 4. The Nucleus Rewrite (Python → Rust)

**Interviewer:**

Tell me about Dropbox's sync engine rewrite.

**Candidate:**

> This is one of the most significant rewrites in recent tech history. Dropbox rewrote their sync engine from Python to Rust, calling the new engine **Nucleus**.
>
> ### Why the rewrite?
>
> The original sync engine was written in Python. At Dropbox's scale (hundreds of millions of files per client, billions of sync operations), Python's limitations became critical:
>
> 1. **Memory usage**: Python objects have high overhead (~28 bytes per integer vs 8 bytes in Rust). A sync engine tracking 1 million files in memory uses significantly more RAM in Python.
> 2. **CPU performance**: Python's GIL (Global Interpreter Lock) prevents true parallelism. The sync engine does CPU-intensive work (hashing, diffing, state machine transitions) that benefits from multi-threading.
> 3. **Correctness**: The Python codebase had accumulated complex state management bugs that were hard to reproduce and fix. Rust's ownership model prevents entire categories of bugs at compile time.
>
> ### Nucleus architecture:
>
> | Aspect | Old (Python) | New (Nucleus / Rust) |
> |--------|-------------|---------------------|
> | **Language** | Python 2 → 3 | Rust |
> | **Concurrency model** | GIL-limited threading | Single control thread + async I/O |
> | **State management** | Distributed across modules | Centralized state machine |
> | **Sync approach** | Batch-based (collect all changes, process batch) | Streaming (process changes as they arrive) |
> | **Testing** | Integration tests (slow, flaky) | Simulation testing (deterministic, fast) |
> | **Performance** | Baseline | ~2x faster sync, lower memory |
>
> ### Key design decisions in Nucleus:
>
> **1. Single control thread:**
> > Instead of multiple threads racing to update sync state, Nucleus uses a single control thread that makes all state transitions. I/O (network, disk) happens asynchronously, but decisions about what to sync happen on one thread. This eliminates an entire class of concurrency bugs — no locks, no races, no deadlocks in the state machine.
> >
> > This is similar to Redis's single-threaded model — serialize all decisions, parallelize I/O.
>
> **2. Streaming sync:**
> > The old engine waited to collect a full batch of changes before processing. Nucleus processes changes as they stream in — start downloading block 1 while still discovering blocks 2, 3, 4 need downloading. This gives **~2x faster sync** for large change sets.
> >
> > ```
> > Old (batch):
> >   [Discover all changes]───>[Process batch]───>[Apply all]
> >   |---- 5 seconds ----|---- 10 seconds ----|-- 3 seconds --|
> >                                             Total: 18 seconds
> >
> > New (streaming):
> >   [Discover change 1]──>[Process 1]──>[Apply 1]
> >        [Discover 2]──>[Process 2]──>[Apply 2]
> >             [Discover 3]──>[Process 3]──>[Apply 3]
> >   |---- overlapped, ~9 seconds total ----|
> > ```
>
> **3. Simulation testing:**
> > The hardest part of testing a sync engine is reproducing real-world scenarios — network failures mid-sync, concurrent edits, OS event delays, disk full errors. Nucleus uses **simulation testing**: the entire sync engine runs in a simulated environment where time, network, and disk are controllable.
> >
> > The team can simulate "user edits file on laptop → goes offline → collaborator edits same file → user comes back online → conflict" deterministically, in milliseconds, without actual network or disk I/O.
> >
> > This is inspired by FoundationDB's simulation testing approach — model the entire system as a deterministic state machine and explore all possible interleavings.

---

## 5. Long-Polling vs WebSocket

**Interviewer:**

Why does Dropbox use long-polling instead of WebSocket for change notifications?

**Candidate:**

> This is a deliberate architectural choice that reflects Dropbox's product requirements:
>
> | Criterion | Long-Polling (Dropbox) | WebSocket (WhatsApp) |
> |-----------|----------------------|---------------------|
> | **Latency** | 1-5 seconds | < 100 ms |
> | **Acceptable?** | Yes — file sync is seconds-level | Yes — chat must be instant |
> | **Stateful connections** | No — each poll is independent HTTP request | Yes — persistent bidirectional connection |
> | **Load balancing** | Simple — any server handles any poll | Complex — need sticky sessions or connection registry |
> | **Firewall compatibility** | Excellent — it's just HTTP with long timeout | Poor — many corporate firewalls block WebSocket upgrade |
> | **Server-side complexity** | Low — hold HTTP response, release on change | High — connection lifecycle management, heartbeats, reconnection |
> | **Connection scaling** | Easy — no persistent connections to manage | Hard — WhatsApp needs Erlang/BEAM for 2.8M connections/server |
> | **Infrastructure** | Standard HTTP servers and load balancers | Specialized connection servers (WhatsApp uses Erlang) |
>
> **Why long-polling wins for Dropbox:**
>
> 1. **File sync tolerates seconds of delay.** If your file takes 3 seconds to appear on another device instead of 100ms, no one notices. Chat messages at 3-second delay would be unusable.
>
> 2. **Corporate firewall compatibility.** Dropbox's target market includes enterprises. Corporate proxies frequently block WebSocket (it upgrades from HTTP, which proxies don't understand). Long-poll is just an HTTP POST with a 90-second timeout — works everywhere.
>
> 3. **Stateless server architecture.** A long-poll request can be routed to any server. If a server dies, the client's next poll goes to a different server seamlessly. With WebSocket, the client must reconnect and the new server needs the client's context.
>
> 4. **Scale math:** 70M DAU polling every ~60 seconds = **1.17M polls/sec**. Each poll is a simple HTTP request that the server holds open. This is orders of magnitude simpler than managing 70M persistent WebSocket connections (which would require ~25 WhatsApp-style Erlang servers just for connections).
>
> **When WebSocket is the right choice:**
> - Real-time chat (WhatsApp, Slack) — sub-100ms latency required
> - Multiplayer games — 16ms tick rate
> - Live collaboration (Google Docs) — character-level real-time updates
> - Trading platforms — microsecond matters
>
> Dropbox is none of these. **The product requirement dictates the protocol.**

---

## 6. Sync State Machine

**Candidate:**

> Every file and folder in the Dropbox client has a sync state. This is what drives the tray icon (green checkmark, blue arrows, red X):
>
> ```
> ┌─────────────────────────────────────────────────────┐
> │                  Sync State Machine                  │
> │                                                      │
> │                  ┌──────────┐                        │
> │        ┌────────>│UP_TO_DATE│<───────────┐          │
> │        │         │  (green  │            │          │
> │        │         │checkmark)│            │          │
> │        │         └────┬─────┘            │          │
> │        │              │                  │          │
> │        │         local change        sync complete  │
> │        │         or remote change        │          │
> │        │              │                  │          │
> │        │              ▼                  │          │
> │        │         ┌──────────┐            │          │
> │        │         │ SYNCING  │────────────┘          │
> │        │         │  (blue   │                       │
> │        │         │ arrows)  │                       │
> │        │         └────┬─────┘                       │
> │        │              │                             │
> │        │         conflict detected                  │
> │        │         or error                           │
> │        │              │                             │
> │   resolved            ▼                             │
> │        │         ┌──────────┐     ┌──────────┐     │
> │        ├─────────│CONFLICTED│     │  ERROR   │     │
> │        │         │  (badge) │     │  (red X) │     │
> │        │         └──────────┘     └────┬─────┘     │
> │        │                               │           │
> │        │                          retry succeeds   │
> │        └───────────────────────────────┘           │
> │                                                     │
> └─────────────────────────────────────────────────────┘
>
> Error substates:
>   - PERMISSION_DENIED: no access to shared folder
>   - STORAGE_FULL: user quota exceeded
>   - NETWORK_ERROR: transient, will retry
>   - FILE_LOCKED: OS-level lock (application has file open)
>   - PATH_TOO_LONG: exceeds OS path limit (Windows: 260 chars)
> ```
>
> **Aggregate state**: The tray icon shows the aggregate state across all files:
> - All UP_TO_DATE → green checkmark ✓
> - Any SYNCING → blue sync arrows 🔄
> - Any ERROR → red X ✗
> - Any CONFLICTED → conflict badge ⚠️

---

## 7. Conflict Detection and Resolution

**Interviewer:**

How does the sync engine handle conflicts?

**Candidate:**

> Conflicts occur when two clients edit the same file without syncing in between. Dropbox uses **optimistic concurrency** — no locks, detect conflicts on upload.
>
> ### Conflict detection mechanism:
>
> ```
> Timeline:
>
> Client A                Server                Client B
>   │                        │                     │
>   │  file.txt (rev: 5)     │  file.txt (rev: 5)  │
>   │                        │                     │
>   │  Edit file locally     │                     │
>   │                        │     Edit file locally│
>   │                        │                     │
>   │── Upload (rev: 5) ────>│                     │
>   │                        │                     │
>   │              Server accepts.                  │
>   │              New rev: 6.                      │
>   │                        │                     │
>   │<── 200 OK (rev: 6) ───│                     │
>   │                        │                     │
>   │                        │<── Upload (rev: 5) ─│
>   │                        │                     │
>   │              Server detects: client B's base │
>   │              rev (5) ≠ current rev (6).      │
>   │              CONFLICT!                        │
>   │                        │                     │
>   │                        │── 409 Conflict ────>│
>   │                        │                     │
>   │                        │    Client B creates:│
>   │                        │    "file (Bob's     │
>   │                        │     conflicted copy │
>   │                        │     2026-02-20).txt"│
>   │                        │                     │
>   │                        │<── Upload conflict ─│
>   │                        │    copy as new file │
> ```
>
> ### The "Conflicted Copy" approach:
>
> - **Winner**: First to sync (Client A). Their version becomes the canonical file.
> - **Loser**: Second to sync (Client B). Their version is saved as a new file with a descriptive name: `file (Bob's conflicted copy 2026-02-20).txt`
> - **User responsibility**: The user must manually review both versions and merge them.
>
> **Why this approach?**
>
> | Approach | Data loss risk | Complexity | User experience |
> |----------|---------------|------------|-----------------|
> | **Conflicted copy (Dropbox)** | Zero — both versions preserved | Low | User must merge manually |
> | **Last-writer-wins (Google Drive for non-Docs)** | Yes — loser's changes silently overwritten | Lowest | Seamless but dangerous |
> | **Automatic merge (Git)** | Low for text, high for binary | High | Good for developers, confusing for regular users |
> | **OT/CRDT (Google Docs)** | Zero | Very high | Best UX, but only works for specific structured formats |
>
> **Dropbox prioritizes data safety over convenience.** Losing a user's file edit is catastrophic. Having an extra "conflicted copy" file is annoying but recoverable. For a product used by hundreds of millions of non-technical users, this is the right trade-off.

---

## 8. Offline Sync

**Candidate:**

> Offline support is a core Dropbox requirement — users edit files on airplanes, in basements, in areas with no connectivity. Here's how it works:
>
> ### Offline edit flow:
>
> 1. **Local edits while offline**: User edits files normally. The sync engine detects changes via file watcher and queues them in a **local change journal**.
>
> 2. **Change journal persistence**: The local change journal is persisted to disk (SQLite database). Even if the app crashes or the machine reboots, queued changes survive.
>
> 3. **Reconnection**: When network is available again:
>    - Sync engine replays the local change journal in order
>    - For each change: compute block hashes → dedup check → upload new blocks → commit metadata
>    - Simultaneously: fetch remote changes via longpoll/continue
>
> 4. **Conflict resolution**: If a queued local change conflicts with a remote change that happened while offline → conflicted copy created (same mechanism as online conflicts).
>
> ```
> Offline timeline:
>
> Client A (offline)        Server         Client B (online)
>   │                          │               │
>   │  Goes offline            │               │
>   │  ✈️ Airplane mode        │               │
>   │                          │               │
>   │  Edit file1.txt          │               │
>   │  Create file2.txt        │               │
>   │  Delete file3.txt        │               │
>   │                          │               │
>   │  (changes queued         │    Edit file1.txt (synced to server)
>   │   in local journal)      │               │
>   │                          │               │
>   │  ✈️ Lands, online again  │               │
>   │                          │               │
>   │─── Longpoll (old cursor)─>│              │
>   │<── {changes: true} ──────│               │
>   │─── Fetch changes ────────>│              │
>   │<── file1.txt changed ────│               │
>   │                          │               │
>   │  Detect conflict: file1.txt was edited   │
>   │  locally AND remotely while offline.     │
>   │                          │               │
>   │  1. Download B's version of file1.txt    │
>   │  2. Upload A's version as "file1        │
>   │     (A's conflicted copy).txt"          │
>   │  3. Upload file2.txt (no conflict)      │
>   │  4. Sync delete of file3.txt            │
> ```

---

## 9. Performance Optimizations

**Candidate:**

> The sync engine has several performance optimizations beyond basic delta sync:
>
> ### 9.1 LAN Sync
> When multiple devices are on the same local network, Dropbox can sync files **directly between them** (peer-to-peer) instead of going through the cloud. LAN sync uses UDP broadcast to discover other Dropbox clients on the network, then transfers blocks directly. This is:
> - **Faster**: LAN speed (1 Gbps) vs internet upload (10 Mbps)
> - **Cheaper**: No internet bandwidth consumed
> - **Private**: Data stays on the local network
>
> ### 9.2 Streaming Sync (Nucleus)
> As described in the Nucleus section — start processing changes as they're discovered rather than waiting for a full batch.
>
> ### 9.3 Selective Sync / Smart Sync
> Users can choose which folders to sync locally (Selective Sync) or keep files in the cloud and download on demand (Smart Sync / virtual files). This reduces local disk usage and initial sync time.
>
> Smart Sync uses **OS-level placeholder files**:
> - **Windows**: Cloud Files API (similar to OneDrive's implementation)
> - **macOS**: File Provider API
> - These create lightweight placeholders that look like real files but download content on demand when opened.
>
> ### 9.4 Compression
> Blocks are compressed (zlib/lz4) before upload. The pipeline is:
> `chunk → hash (for dedup) → compress → encrypt (if applicable) → upload`
>
> Compression happens **after** hashing because the hash must be of the original content (for dedup — compressed content varies by algorithm/settings).
>
> ### 9.5 Parallel Block Transfer
> Multiple blocks can be uploaded/downloaded simultaneously (typically 4-8 parallel transfers). This saturates the network pipe and reduces sync time for multi-block changes.

---

## Contrast: Dropbox Sync vs Google Drive vs rsync vs Git

| Aspect | Dropbox Sync | Google Drive | rsync | Git |
|--------|-------------|-------------|-------|-----|
| **Sync granularity** | Block-level (4 MB) | Whole-file (for non-Docs) | Byte-level (rolling checksum) | Object-level (whole files, delta-packed) |
| **Change detection** | OS file events + hash comparison | OS file events + whole-file hash | Stat timestamps + rolling checksum | Explicit `git add` |
| **Notification** | Long-polling (seconds) | Webhook / polling | N/A (manual trigger) | N/A (manual push/pull) |
| **Conflict handling** | Conflicted copy | OT for Docs, last-writer-wins for files | Last-writer-wins (overwrite) | Three-way merge with conflict markers |
| **Offline support** | Full — queue changes, sync on reconnect | Partial — Google Docs offline mode | N/A (manual) | Full — commit locally, push when ready |
| **Target user** | Everyone (non-technical) | Everyone (collaboration-focused) | Sysadmins (manual tool) | Developers (version control) |
| **Client complexity** | Very high (file watcher, state machine, block management) | Medium (simpler — no block management) | Low (single command) | Medium (git objects, packfiles) |

---

## L5 vs L6 vs L7 — Sync Engine Rubric

| Aspect | L5 (SDE-2) | L6 (SDE-3) | L7 (Principal) |
|--------|------------|------------|-----------------|
| **Change detection** | "Watch the folder for changes" | Specifies OS-level APIs (inotify, FSEvents, RDCW), discusses debouncing and coalescing | Discusses fallback scanning on event overflow, priority queuing, the challenges of each OS API |
| **Delta sync** | "Only upload changed parts" | Explains 4 MB block hashing, SHA-256, concrete bandwidth savings math | Discusses CDC vs fixed-size chunking trade-offs, sub-block rolling hashes, insertion resilience problem |
| **Sync protocol** | "Poll the server for changes" | Designs long-poll + cursor two-phase protocol, explains statelessness | Compares long-poll vs WebSocket with quantitative analysis (1.17M polls/sec vs 70M persistent connections), explains why product requirements dictate protocol choice |
| **Conflict resolution** | "Handle conflicts somehow" | Explains optimistic concurrency with rev-based CAS, conflicted copy naming | Compares with Google Docs OT, Git three-way merge, CRDTs — explains why conflicted copy is the right trade-off for a non-technical user base |
| **Nucleus rewrite** | Not mentioned | Mentions Rust rewrite for performance | Explains single control thread (Redis-like), streaming sync (2x improvement), simulation testing (FoundationDB-inspired) |
| **Offline** | "Cache files locally" | Describes local change journal, replay on reconnect, conflict on replay | Designs the complete offline → online transition, discusses journal persistence, ordering guarantees, partial sync recovery |

---

> **Summary:** The sync engine is a ~500,000-line codebase that solves one of the hardest problems in distributed systems: keeping files consistent across unreliable networks, multiple operating systems, and concurrent users — while being invisible to the user. Every file edit triggers a pipeline of change detection → block hashing → dedup → upload → notification → download → reconstruction. The Nucleus rewrite (Python → Rust) improved performance 2x while reducing bugs through Rust's type system and simulation testing. The choice of long-polling over WebSocket and conflicted copies over automatic merge both reflect Dropbox's philosophy: **optimize for safety and simplicity at the product level, complexity at the engineering level.**
