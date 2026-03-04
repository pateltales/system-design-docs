# Deep Dive: Notification & Change Propagation

> **Context:** How changes propagate from one client to all other clients and collaborators in near-real-time. The notification system handles ~1.17 million polls per second and is the backbone of Dropbox's "automatic sync" experience.

---

## Opening

**Interviewer:**

Walk me through how a file change on Alice's laptop reaches Bob's desktop. What's the notification pipeline?

**Candidate:**

> The notification pipeline has two key properties:
> 1. **Two-phase**: notification ("something changed") is separate from data fetch ("here's what changed")
> 2. **Pull-based with push hint**: clients pull changes, but the server "pushes" a hint via long-polling
>
> This separation is deliberate — it keeps the notification layer lightweight and stateless.

---

## 1. Change Journal Architecture

**Candidate:**

> Every mutation (create, edit, delete, move, rename) is recorded in a per-namespace **change journal** — an append-only, ordered log:
>
> ```
> ┌─────────────────────────────────────────────────────────────┐
> │  CHANGE JOURNAL — Namespace 2001 (SharedProject)            │
> │                                                             │
> │  cursor │ change_type │ file_id │ path              │ rev   │
> │  ───────┼─────────────┼─────────┼───────────────────┼────── │
> │  3200   │ create      │ 42      │ /spec.docx        │ rev1  │
> │  3201   │ update      │ 42      │ /spec.docx        │ rev2  │
> │  3202   │ create      │ 43      │ /design.psd       │ rev1  │
> │  3203   │ move        │ 42      │ /docs/spec.docx   │ rev2  │
> │  3204   │ delete      │ 43      │ /design.psd       │ rev1  │
> │  3205   │ update      │ 42      │ /docs/spec.docx   │ rev3  │
> │  ...                                                        │
> │                                                             │
> │  Properties:                                                │
> │  - cursor is monotonically increasing (AUTO_INCREMENT)      │
> │  - Per-namespace (not global) — each namespace has own seq  │
> │  - Append-only (immutable — entries never updated/deleted)  │
> │  - Retained for sync window (e.g., 90 days)                │
> └─────────────────────────────────────────────────────────────┘
> ```
>
> **Why per-namespace (not per-user or global)?**
> - Per-user: A user with 5 shared folders would need 5 separate journals merged into one. Complex.
> - Global: Billions of changes per day across all users in a single journal. Unmanageable hot partition.
> - **Per-namespace**: Natural unit — all members of a shared folder care about the same changes. The journal serves all members equally. A user's sync tracks cursor positions across all their namespaces: `{ns_1001: 5000, ns_2001: 3205, ns_2002: 800}`.

---

## 2. Long-Polling Mechanism

**Candidate:**

> ### The Complete Long-Poll Flow:
>
> ```
> Client (Alice)                  Notification Server            Change Journal
>   │                                    │                            │
>   │── POST /list_folder/longpoll ─────>│                            │
>   │   { cursor: "encoded_token",       │                            │
>   │     timeout: 90 }                  │                            │
>   │                                    │                            │
>   │            Server decodes cursor:   │                            │
>   │            ns_1001: cursor=5000     │                            │
>   │            ns_2001: cursor=3200     │                            │
>   │            ns_2002: cursor=800      │                            │
>   │                                    │                            │
>   │            Check: any namespace     │                            │
>   │            has changes beyond       │──── SELECT MAX(cursor)     │
>   │            client's cursor?         │     FROM change_journal    │
>   │                                    │     WHERE ns IN (...)      │
>   │                                    │<─── ns_2001: max=3205      │
>   │                                    │     (client has 3200, so   │
>   │                                    │      3205 > 3200 → YES)   │
>   │                                    │                            │
>   │   CASE A: Changes exist immediately │                            │
>   │<── { changes: true, backoff: null } │                            │
>   │                                    │                            │
>   │   --- OR ---                        │                            │
>   │                                    │                            │
>   │   CASE B: No changes yet            │                            │
>   │            Hold connection open...  │                            │
>   │            Subscribe to namespace   │                            │
>   │            change notifications     │                            │
>   │                                    │                            │
>   │            ... 45 seconds pass ...  │                            │
>   │                                    │                            │
>   │                                    │<─── Change in ns_2001!     │
>   │                                    │     (Bob edited a file)    │
>   │                                    │                            │
>   │<── { changes: true, backoff: null } │                            │
>   │                                    │                            │
>   │   --- OR ---                        │                            │
>   │                                    │                            │
>   │   CASE C: Timeout (90 seconds)      │                            │
>   │<── { changes: false }               │                            │
>   │                                    │                            │
>   │   In ALL cases, client immediately  │                            │
>   │   issues a NEW longpoll (or fetches │                            │
>   │   changes first if changes=true)    │                            │
> ```
>
> ### Timeout and backoff:
>
> - **Client timeout**: 30-480 seconds (default 30, Dropbox desktop client uses ~90)
> - **Server backoff**: If the server is overloaded, it returns `backoff: N` — client must wait N seconds before next poll. This is the server's pressure relief valve.
> - **Connection timeout handling**: If the HTTP connection itself drops (not the longpoll timeout), the client reconnects with exponential backoff (1s, 2s, 4s, 8s, max 60s).

---

## 3. Two-Phase Notification Protocol

**Interviewer:**

Why separate the notification from the data? Why not include the changes in the longpoll response?

**Candidate:**

> Three reasons:
>
> ### Reason 1: Keep the notification layer lightweight
> ```
> Lightweight longpoll response:
>   { "changes": true }     ← ~20 bytes
>
> Full change payload would be:
>   { "entries": [
>       { "name": "spec.docx", "rev": "rev3", "blocks": [...],
>         "size": 15000, "content_hash": "abc..." },
>       { "name": "design.psd", ... },
>       ... potentially hundreds of entries ...
>   ]}                        ← could be 100 KB+
> ```
> The notification server holds **millions of open connections**. If each response were 100 KB instead of 20 bytes, that's 5,000x more bandwidth from the notification tier.
>
> ### Reason 2: Different server infrastructure
> ```
> Notification server (notify.dropboxapi.com):
>   - Optimized for holding open connections
>   - Lightweight — just watches change journal cursors
>   - Separate hostname = separate scaling, monitoring, failure domain
>
> API server (api.dropboxapi.com):
>   - Optimized for request/response processing
>   - Fetches metadata, resolves paths, checks permissions
>   - Heavier processing per request
>
> Separating them lets each tier scale independently.
> ```
>
> ### Reason 3: Client may not need all changes
> ```
> Some clients use selective sync — they only care about
> certain folders. The notification says "changes exist,"
> and the client decides which changes to fetch based on
> its selective sync settings.
>
> If changes were bundled in the notification, the server
> would need to know each client's selective sync config —
> coupling the notification layer to the sync layer.
> ```
>
> ### The full two-phase flow:
>
> ```
> Phase 1: NOTIFICATION (lightweight)
>   Client ──── longpoll ───> Notification Server
>   Client <─── {changes: true} ─── Notification Server
>
> Phase 2: DATA FETCH (heavy lifting)
>   Client ──── list_folder/continue ───> API Server
>   Client <─── {entries: [...], cursor: "new"} ─── API Server
>
> Phase 3: APPLY
>   Client: for each changed file, compare block hashes,
>           download changed blocks, reconstruct locally
>
> Phase 4: LOOP
>   Client ──── longpoll (new cursor) ───> Notification Server
>   ... repeat ...
> ```

---

## 4. Notification Fan-Out

**Candidate:**

> When a file changes in a shared folder, all collaborators need to know:
>
> ```
> Scenario: Bob edits spec.docx in SharedProject (10 members)
>
> Server                         Notification Server(s)
>   │                                 │
>   │ 1. Append to NS 2001           │
>   │    change journal               │
>   │                                 │
>   │ 2. Lookup NS 2001 members:     │
>   │    [Alice, Bob, Charlie, ...]   │
>   │                                 │
>   │ 3. Signal notification service: │
>   │    "NS 2001 has new changes"    │
>   │─────────────────────────────────>│
>   │                                 │
>   │                    4. Check: which clients have  │
>   │                       active longpolls watching   │
>   │                       NS 2001?                    │
>   │                                                   │
>   │                    Active longpolls:               │
>   │                    - Alice (poll #A1) → respond   │
>   │                    - Charlie (poll #C1) → respond │
>   │                    - Dave (poll #D1) → respond    │
>   │                                                   │
>   │                    NOT active:                     │
>   │                    - Bob (HE made the change,     │
>   │                      his client already knows)    │
>   │                    - Eve (laptop is closed,       │
>   │                      no active poll)              │
>   │                    - Frank (on mobile, not polling │
>   │                      frequently)                  │
>   │                                                   │
>   │                    5. Respond to active longpolls: │
>   │                    Alice ← {changes: true}        │
>   │                    Charlie ← {changes: true}      │
>   │                    Dave ← {changes: true}         │
>   │                                                   │
>   │                    6. Eve and Frank will discover  │
>   │                       changes on their NEXT poll.  │
>   │                       No push, no queuing needed. │
> ```
>
> **This is much simpler than WhatsApp's fan-out** because:
> 1. Dropbox doesn't need to deliver the actual content — just "changes exist"
> 2. There's no offline message queue — if a client isn't polling, it picks up changes on next poll
> 3. The longpoll response is tiny (~20 bytes) vs WhatsApp message delivery (~1 KB+)
> 4. Dropbox tolerates seconds of delay; WhatsApp needs sub-100ms

---

## 5. Cursor Management

**Interviewer:**

Tell me more about the cursor. Why is it opaque?

**Candidate:**

> ### What the cursor encodes:
>
> ```
> Opaque cursor (what the client sees):
>   "AAGdm3pOcHFSR0JIUFBWT3NxeHBfRnl5..."
>
> Decoded (what the server knows):
>   {
>     "version": 3,                        // Cursor format version
>     "namespaces": {
>       "1001": 5000,                      // Alice's root: cursor position 5000
>       "2001": 3205,                      // SharedProject: position 3205
>       "2002": 800                        // AnotherShare: position 800
>     },
>     "namespace_list_version": 42,        // Tracks which namespaces user has
>     "created_at": "2026-02-20T12:00:00Z" // Expiry tracking
>   }
>
>   Base64-encoded and possibly encrypted/signed for tamper resistance.
> ```
>
> ### Why opaque?
>
> 1. **Server-side freedom**: The server can change cursor encoding (add fields, change format) without breaking any client. Clients never parse it — they just store and send it back.
>
> 2. **Multi-namespace compaction**: Instead of clients tracking cursors for each namespace separately, the server encodes all positions into a single token.
>
> 3. **Security**: Clients can't forge or manipulate cursors. A signed/encrypted cursor prevents a client from jumping backward (which could cause re-syncing old changes) or forward (which could skip changes).
>
> 4. **Namespace membership tracking**: The cursor includes information about which namespaces the user has access to. If Alice is added to a new shared folder, her next longpoll returns `changes: true` even though no files changed — the cursor's namespace list changed.

---

## 6. Webhook Notifications

**Candidate:**

> Webhooks are for server-to-server integrations — a different use case from the client longpoll:
>
> ```
> Use case: "When files change in my Dropbox, trigger my CI/CD pipeline"
>
> Setup:
>   1. Developer registers webhook URL in Dropbox App Console
>   2. Dropbox verifies the URL (GET with challenge parameter)
>
> Verification:
>   GET https://myapp.com/webhook?challenge=abc123
>   Response: abc123  (echo the challenge)
>
> Notification flow:
>   File changes → Server appends to change journal →
>   Webhook service checks: any webhook subscribers for this account? →
>   POST https://myapp.com/webhook
>   Body: { "list_folder": { "accounts": ["dbid:AAH4f99..."] } }
>
> Delivery guarantees:
>   - At-least-once: webhook may be delivered multiple times
>   - Retry with exponential backoff (1min, 2min, 4min, ... up to 24 hours)
>   - After 24 hours of failures → webhook disabled, admin notified
>   - Your handler MUST be idempotent (handle duplicate deliveries)
>
> Payload is minimal:
>   "Account X has changes" — NOT what changed.
>   Your server calls list_folder/continue to get actual changes.
>   Same two-phase pattern as longpoll.
> ```

---

## 7. Scale of the Notification System

**Candidate:**

> ### Back-of-envelope math:
>
> ```
> Active users: 70M daily active (10% of 700M registered)
> Poll frequency: every ~60 seconds
> Polls per second: 70,000,000 / 60 ≈ 1,170,000 polls/sec
>
> Each poll:
>   - HTTP request held open for up to 90 seconds
>   - Server checks namespace cursors (cached, ~1ms)
>   - Response: ~20 bytes
>
> Concurrent connections (at any moment):
>   70M users × (avg hold time ~45 sec / 60 sec poll interval) ≈ 52.5M open connections
>
> This is a LOT of open connections, but each is:
>   - Idle (just waiting, no CPU usage)
>   - Lightweight (no data transfer while waiting)
>   - Stateless (server only needs to watch cursor changes)
>
> Compare to WhatsApp:
>   WhatsApp: 50M+ persistent WebSocket connections with active
>             message routing, presence tracking, typing indicators
>   Dropbox:  52M+ idle HTTP connections just waiting for a cursor change
>
>   Dropbox's connections are MUCH simpler to manage — no session state,
>   no message routing, no delivery guarantees during the connection.
> ```
>
> ### Scaling the notification tier:
>
> ```
> Notification servers are horizontally scalable:
>   - No sticky sessions (any server handles any poll)
>   - Load balancer distributes evenly
>   - Each server holds ~100K open connections
>   - 52M connections / 100K per server = ~520 notification servers
>
> When a namespace change occurs:
>   - Message bus (Kafka/Redis Pub-Sub) broadcasts: "NS 2001 changed"
>   - All notification servers check: do I have a client watching NS 2001?
>   - If yes → respond to that client's longpoll
>   - If no → ignore
>
> ┌──────────┐     ┌─────────────┐     ┌──────────────────┐
> │  API     │────>│  Message Bus │────>│ Notification     │
> │  Server  │     │  (Kafka)     │     │ Servers (520+)   │
> │  (change │     │              │     │                  │
> │  commit) │     │  "NS 2001    │     │ Each holds ~100K │
> │          │     │   changed"   │     │ open connections  │
> └──────────┘     └─────────────┘     └──────────────────┘
> ```

---

## 8. Notification Batching and Coalescing

**Candidate:**

> When a user is actively editing (saving every few seconds), we don't want to fire 10 notifications in 10 seconds:
>
> ```
> Without coalescing:
>   t=0s:  Bob saves → notification to Alice
>   t=2s:  Bob saves → notification to Alice
>   t=4s:  Bob saves → notification to Alice
>   t=6s:  Bob saves → notification to Alice
>   t=8s:  Bob saves → notification to Alice
>
>   Alice's client: 5 fetch cycles, each downloading the latest version
>   Wasted work: 4 out of 5 fetches are immediately obsoleted
>
> With coalescing (debounce window: 5 seconds):
>   t=0s:  Bob saves → wait...
>   t=2s:  Bob saves → reset timer...
>   t=4s:  Bob saves → reset timer...
>   t=6s:  Bob saves → reset timer...
>   t=8s:  Bob saves → reset timer...
>   t=13s: 5 seconds since last save → NOW notify Alice
>
>   Alice's client: 1 fetch cycle, gets the latest version
>   Saved: 4 redundant notification-fetch cycles
> ```
>
> **Implementation**: The notification server maintains a per-namespace "last notification sent" timestamp. If a change arrives within the debounce window of the last notification, it resets the timer. This naturally coalesces rapid changes.

---

## 9. Priority Ordering

**Candidate:**

> When multiple changes are pending, not all are equally urgent:
>
> ```
> Priority order (highest to lowest):
>
> 1. User's OWN changes (from other devices)
>    - "I saved on my laptop, I expect it on my phone NOW"
>    - Highest urgency — user is aware of the change
>
> 2. Changes in actively-open folders
>    - User has the folder open in Finder/Explorer
>    - They'll notice stale content immediately
>
> 3. Changes from close collaborators (same shared folder)
>    - Important for collaboration but not user-initiated
>
> 4. Changes in background-synced folders
>    - User isn't looking at these right now
>    - Can be deferred slightly
>
> 5. Bulk operations (restore from backup, large folder restructure)
>    - Hundreds of file changes at once
>    - Process in background, don't flood the UI
> ```

---

## Contrast: Dropbox vs WhatsApp vs Google Drive vs S3

| Aspect | Dropbox | WhatsApp | Google Drive | S3 |
|--------|---------|----------|-------------|-----|
| **Channel** | Long-polling (HTTP) | WebSocket (persistent) | Webhook + polling | SNS/SQS events |
| **Latency** | 1-5 seconds | < 100 ms | ~1-5 seconds | Seconds to minutes |
| **Payload** | "Changes exist" (20 bytes) | Full message content (~1 KB) | "Changes exist" | Event record (~1 KB) |
| **Two-phase** | Yes (notification + fetch) | No (message delivered directly) | Yes (notification + fetch) | No (event contains details) |
| **Offline handling** | No queue — pick up on next poll | Offline message queue (stored until delivered) | No queue | SQS retention (14 days) |
| **Fan-out** | O(N) per shared folder | O(N) per group (max 1024) | O(N) per shared resource | Event → all subscribers |
| **Statefulness** | Stateless (any server) | Stateful (connection registry) | Stateless | Stateless |

---

## L5 vs L6 vs L7 — Notification Rubric

| Aspect | L5 (SDE-2) | L6 (SDE-3) | L7 (Principal) |
|--------|------------|------------|-----------------|
| **Mechanism** | "Poll the server periodically" | Designs long-poll with cursor, two-phase notification, separate notification hostname | Calculates 1.17M polls/sec, designs message bus fan-out architecture, explains coalescing/batching |
| **Change journal** | "Log changes in a database" | Designs per-namespace journal with monotonic cursor, explains why per-namespace | Discusses cursor encoding (multi-namespace, version, expiry), journal retention, and how cursor changes when user joins/leaves shared folders |
| **Fan-out** | "Notify all users" | Designs fan-out from change journal to active longpolls, explains why inactive clients don't need queuing | Contrasts with WhatsApp fan-out (offline queues, delivery guarantees), calculates scale of fan-out for large shared folders |
| **Contrast** | None | Contrasts with WebSocket (WhatsApp) | Full comparison: Dropbox (stateless pull), WhatsApp (stateful push), Google Drive (hybrid), S3 (infrastructure events) |

---

> **Summary:** The notification system is the nervous system of Dropbox's sync — it tells clients "something changed" so they can fetch and apply updates. Built on long-polling (not WebSocket) for simplicity, statefulness, and firewall compatibility, it handles 1.17M polls/sec from 70M daily active users. The two-phase design (lightweight notification + separate data fetch) keeps the notification tier simple and scalable — ~520 servers holding 100K connections each. Change journals (per-namespace, monotonically increasing cursors) are the source of truth, and opaque cursor tokens give the server flexibility to evolve the encoding without breaking clients.
