# Deep Dive: Offline Editing & Sync

> **Companion document to [01-interview-simulation.md](01-interview-simulation.md)**
> This document covers offline editing capabilities, reconnection synchronization, and the challenges of reconciling diverged document states.

---

## Table of Contents

1. [Offline Mode Architecture](#1-offline-mode-architecture)
2. [Local Document Caching](#2-local-document-caching)
3. [Offline Editing: The Local Operation Queue](#3-offline-editing-the-local-operation-queue)
4. [Reconnection Sync Flow](#4-reconnection-sync-flow)
5. [Offline Challenges](#5-offline-challenges)
6. [Features Unavailable Offline](#6-features-unavailable-offline)
7. [Contrast with Other Systems](#7-contrast-with-other-systems)

---

## 1. Offline Mode Architecture

### How Offline Mode is Delivered

Google Docs offline editing is available via a **Chrome extension** (for Chrome browser) or as a **Progressive Web App (PWA)**. It is NOT available in all browsers or all contexts:

```
Offline Availability:

  Platform                 Offline Support
  ──────────────────────────────────────────
  Chrome (with extension)  YES -- full offline editing
  Chrome (PWA mode)        YES -- service worker caches the app
  Firefox, Safari, Edge    NO  -- no offline editing
  Google Docs Android app  YES -- app-level caching
  Google Docs iOS app      YES -- app-level caching

  Requirement: User must EXPLICITLY enable offline mode
  in Google Drive settings. It is not on by default.

  Why opt-in?
    - Caching documents locally consumes device storage.
    - Security: cached documents exist on the local disk
      in an unencrypted browser cache. Enterprise users
      may not want documents stored locally.
    - Not all documents need offline access -- only cache
      the ones the user is likely to edit offline.
```

### Architectural Overview

```
ONLINE MODE (normal):

  ┌───────────────┐         ┌──────────────────┐
  │  Browser /    │ ◄──────►│  OT Server       │
  │  Client App   │ WebSocket│  (source of truth)│
  │               │         │                  │
  │  Editor UI    │         │  Operation Log   │
  │  + OT Client  │         │  + Snapshots     │
  └───────────────┘         └──────────────────┘


OFFLINE MODE:

  ┌──────────────────────────────────────┐
  │  Browser / Client App               │
  │                                      │
  │  ┌────────────────┐                  │
  │  │  Editor UI     │                  │
  │  │  + OT Client   │                  │          ╳ No network
  │  └───────┬────────┘                  │          ╳ connection
  │          │                           │          ╳
  │          ▼                           │
  │  ┌────────────────┐                  │
  │  │  IndexedDB     │                  │
  │  │                │                  │
  │  │  - Cached doc  │                  │
  │  │    snapshot    │                  │
  │  │  - Pending ops │                  │
  │  │    queue       │                  │
  │  │  - Metadata    │                  │
  │  └────────────────┘                  │
  │                                      │
  │  Service Worker:                     │
  │  - Intercepts network requests       │
  │  - Serves cached app shell           │
  │  - Queues operations for later sync  │
  └──────────────────────────────────────┘
```

---

## 2. Local Document Caching

### What Gets Cached

When offline mode is enabled, the client proactively caches documents to local storage:

```
Cached data per document in IndexedDB:

  Key: doc-abc-123

  Value: {
    // Document content
    snapshot: {
      revision: 450,
      content: { ... },         // full document state at rev 450
      timestamp: "2026-02-21T14:00:00Z"
    },

    // Operations since snapshot (received before going offline)
    recentOps: [
      { rev: 451, op: [...], userId: "bob", ts: "..." },
      { rev: 452, op: [...], userId: "alice", ts: "..." },
      ...
      { rev: 475, op: [...], userId: "carol", ts: "..." }
    ],

    // Document metadata
    metadata: {
      title: "Q1 Project Plan",
      owner: "alice@company.com",
      myPermission: "editor",
      lastModified: "2026-02-21T15:30:00Z"
    },

    // Pending offline operations (created while offline)
    pendingOps: [],    // empty initially, grows during offline editing

    // Last known server revision
    lastKnownRevision: 475
  }

Storage size per document:
  Snapshot: ~10 KB - 1 MB (depends on document size)
  Recent ops: ~10 KB - 100 KB (25 ops at ~200 bytes each = ~5 KB)
  Metadata: ~1 KB
  Total: ~15 KB - 1.2 MB per document

IndexedDB quota (Chrome):
  Per origin: up to 60% of available disk space (e.g., 30 GB on a 50 GB disk)
  Practically: Google Docs caches the most recently accessed documents.
  Users can choose which documents to make available offline.
```

### Cache Update Strategy

```
When does the cache get updated?

  1. ON DOCUMENT OPEN (online):
     After the document loads, the client writes the current
     snapshot + recent ops to IndexedDB. This ensures the
     cached version is fresh.

  2. PERIODICALLY (while online and document is open):
     Every N minutes (e.g., 5), the client updates the cached
     snapshot with the current state. This limits the number
     of ops that need to be replayed if the user goes offline.

  3. ON EXPLICIT "MAKE AVAILABLE OFFLINE":
     User marks a document for offline access in Google Drive.
     The client downloads and caches the full document state.

  4. ON PAGE UNLOAD / TAB CLOSE:
     The client writes the latest state to IndexedDB before
     the page closes. This is the last-chance cache update.

Cache invalidation:
  - If the user opens a document online and the cached version
    is too old (e.g., 7+ days), the client downloads a fresh
    snapshot instead of replaying thousands of operations.
  - If the document is deleted or the user's access is revoked,
    the cache is cleared.
```

---

## 3. Offline Editing: The Local Operation Queue

### How Offline Editing Works

When the user is offline, the editor continues to function. Edits are applied **locally** and queued for later sync:

```
Offline editing flow:

  User types "Hello" at position 0:

  Step 1: Client's OT engine creates the operation:
    op = [insert("Hello")]

  Step 2: Apply to LOCAL document state (optimistic):
    Local doc: "" → "Hello"

  Step 3: Queue the operation in IndexedDB:
    pendingOps.push({
      localId: "local-001",
      op: [insert("Hello")],
      baseRevision: 475,      // last known server revision
      timestamp: "2026-02-21T16:00:00Z"
    })

  Step 4: There is NO server to send to. The operation stays in the queue.

  User continues editing... types " World":

  Step 5: Create operation:
    op = [retain(5), insert(" World")]

  Step 6: Apply locally:
    Local doc: "Hello" → "Hello World"

  Step 7: Queue:
    pendingOps.push({
      localId: "local-002",
      op: [retain(5), insert(" World")],
      baseRevision: 475,
      timestamp: "2026-02-21T16:00:05Z"
    })

  After 2 hours of offline editing:
    pendingOps = [local-001, local-002, ..., local-500]
    500 operations queued for sync.
```

### Local Operation Composition

To reduce the number of operations that need to be synced on reconnect, the client can **compose** adjacent operations:

```
Without composition:
  pendingOps = [
    insert("H"),
    retain(1), insert("e"),
    retain(2), insert("l"),
    retain(3), insert("l"),
    retain(4), insert("o"),
  ]
  = 5 separate operations for typing "Hello"

With composition:
  pendingOps = [
    insert("Hello")
  ]
  = 1 composed operation

The client periodically composes queued operations:
  compose(insert("H"), [retain(1), insert("e")])
    → insert("He")
  compose(insert("He"), [retain(2), insert("l")])
    → insert("Hel")
  ... and so on.

Benefits:
  - Fewer operations to send on reconnect
  - Fewer transforms needed on the server (O(M*N) where M is smaller)
  - Less storage in IndexedDB

When to compose:
  - Every K operations (e.g., every 10 ops, compose into 1)
  - On page unload (compose all pending ops into as few as possible)
  - Before sending on reconnect
```

### Offline Persistence Guarantees

```
What happens if the user closes the browser while offline?

  1. Service Worker persists operations to IndexedDB on every edit.
     Even if the browser crashes, at most the LAST operation is lost.

  2. On browser restart:
     - Service Worker reactivates.
     - Client opens the document from IndexedDB cache.
     - User sees their document with all offline edits intact.
     - pendingOps queue is still in IndexedDB, waiting for reconnection.

  3. On device restart:
     - Same as browser restart. IndexedDB survives reboots.
     - User opens Chrome, navigates to Docs, opens the document.
     - Cached document + pending ops are loaded from IndexedDB.

  What IS lost:
     - The last operation if the browser crashed mid-write to IndexedDB.
     - This is at most one character or one formatting change.
     - Acceptable: the user can re-type one character.
```

---

## 4. Reconnection Sync Flow

### The 6-Step Reconnection Protocol

```
Alice reconnects after 2 hours offline with 500 pending operations:

  Alice's Client                        OT Server
       |                                    |
       | STEP 1: Establish WebSocket        |
       | WS /documents/{docId}/collaborate  |
       |----------------------------------->|
       |                                    |
       |          Connection ACK            |
       |          Server rev: 2575          |
       |<-----------------------------------|
       |                                    |
       | STEP 2: Send offline operations    |
       |                                    |
       | "I'm at rev 475.                   |
       |  I have 500 pending ops.           |
       |  Here they are (composed into 50): |
       |  [op1, op2, ..., op50]"            |
       |----------------------------------->|
       |                                    |
       |          STEP 3: Server transforms |
       |          (see below)               |
       |                                    |
       |          STEP 4: Server sends      |
       |          2100 operations           |
       |          (rev 476 through 2575,    |
       |           transformed for Alice's  |
       |           context)                 |
       |<-----------------------------------|
       |                                    |
       | STEP 5: Client applies server ops  |
       | to local document.                 |
       |                                    |
       |          Server sends ACKs for     |
       |          Alice's 50 ops            |
       |          (rev 2576 through 2625)   |
       |<-----------------------------------|
       |                                    |
       | STEP 6: Convergence achieved       |
       | Client rev = Server rev = 2625     |
       | Both have identical document state |
       |                                    |
       | Client enters SYNCHRONIZED state   |
       | "Syncing..." indicator disappears  |
       |                                    |
```

### Step 3 In Detail: Server-Side Transform

```
Server-side processing of Alice's offline operations:

  Server state: at revision 2575 (2100 ops since Alice went offline at rev 475)
  Alice's ops: 50 composed operations, all based on revision 475

  Intervening operations: ops 476, 477, 478, ..., 2575 (2100 ops)

  For each of Alice's 50 operations (in order):
    Transform against ALL 2100 intervening operations:

    alice_op_1' = transform(transform(transform(...transform(
                    alice_op_1, server_op_476),
                    server_op_477),
                    ...),
                    server_op_2575)

    alice_op_2' = transform(transform(transform(...transform(
                    alice_op_2, server_op_476'),  // NOTE: server ops are
                    server_op_477'),              // also transformed
                    ...),                         // against Alice's ops
                    server_op_2575')

    ... (repeat for all 50 ops)

  Total transforms: 50 * 2,100 = 105,000 transform operations

  At ~1 microsecond per transform: ~105 ms (fast!)
  At ~10 microseconds per transform: ~1.05 seconds (acceptable)

  Compare to WITHOUT composition (500 raw ops):
    500 * 2,100 = 1,050,000 transforms
    At ~10 microseconds: ~10.5 seconds (slow, but manageable)

  After all transforms:
    - All 50 of Alice's ops are transformed and applied to server state.
    - Server revision: 2575 + 50 = 2625.
    - All 2100 intervening ops are also transformed for Alice's context.
    - These transformed ops are sent to Alice so she can apply them.
```

### What Alice Sees During Sync

```
User experience timeline:

  t=0:    Alice opens laptop. Browser reconnects to network.
          Google Docs detects network connectivity.

  t=500ms: Connection indicator changes from "Offline" to "Connecting..."
           WebSocket connection is being established.

  t=1s:   "Syncing your changes..."
          Progress indicator appears.
          Alice's pending operations are being sent to the server.

  t=2s:   Document content starts changing.
          Operations from Bob and Carol (during Alice's offline period)
          are being applied to Alice's local document.

          Alice may see:
          - New paragraphs appearing
          - Formatting changes
          - Text being added or removed
          - Comments appearing
          - Other users' cursors appearing

  t=3s:   "All changes saved."
          Sync is complete. Alice's edits are confirmed by the server.
          Other users' edits are applied locally.
          Document is fully up-to-date.

  Total visible sync time: ~2-3 seconds (typical)
  Worst case (many ops): ~10-30 seconds with progress bar.
```

---

## 5. Offline Challenges

### Challenge 1: Long Offline Periods Create Expensive Reconciliation

```
The core challenge: O(M * N) transform cost.

  Offline     Server ops    Transforms    Time        User Experience
  ops (M)     during (N)    (M * N)       (est.)
  ────────────────────────────────────────────────────────────────────
     10           50           500        < 1ms       Instant
     50          500        25,000        ~25ms       Instant
    100        1,000       100,000        ~100ms      Barely noticeable
    500        2,000     1,000,000        ~1-10s      "Syncing..." bar
  1,000        5,000     5,000,000        ~5-50s      Progress percentage
  5,000       10,000    50,000,000        ~1-5 min    Warning dialog

For very long offline periods (days):
  - Composition reduces M significantly
  - But N (server ops) cannot be reduced -- other users'
    edits are immutable
  - At some point, a full snapshot-based reconciliation
    is faster than OT-based transform
```

### Challenge 2: Same-Region Editing Creates Surprising Results

```
Multiple users editing the same paragraph while one is offline:

BEFORE OFFLINE:
  Paragraph: "The project has three main goals."

ALICE (offline, 2 hours):
  Edits to: "The REVISED project has THREE main goals and two sub-goals."
  Changes:  Added "REVISED", changed "three" to "THREE",
            added "and two sub-goals."

BOB (online, during those 2 hours):
  Edits to: "The project has three critical main goals."
  Changes:  Added "critical"

AFTER RECONCILIATION:
  Result:   "The REVISED project has THREE critical main goals and two sub-goals."

  Analysis:
    - Alice's "REVISED" is preserved (inserted at position not touched by Bob)
    - Alice's "THREE" is preserved (Bob didn't change "three")
    - Bob's "critical" is preserved (inserted at position Alice retained)
    - Alice's "and two sub-goals." is preserved (appended at end)

  This result is CORRECT -- all edits are preserved, positions are adjusted.

BUT consider a more adversarial case:

ALICE (offline):
  Rewrites entire paragraph:
  "We must focus on delivery timelines above all else."

BOB (online):
  Also rewrites entire paragraph:
  "Quality is the top priority for this quarter."

AFTER RECONCILIATION:
  Result:   "We Qmualst itfoyc ius on the delivertop py rimoelines rity..."

  WAIT -- that can't be right. Actually, OT would NOT produce
  garbled text. The operations are insert/delete, not "replace."

  What actually happens:
    Alice DELETES "The project has three main goals."
    Alice INSERTS "We must focus on delivery timelines above all else."

    Bob DELETES "The project has three main goals."
    Bob INSERTS "Quality is the top priority for this quarter."

    OT transformation:
      Both deletions target the same text.
      Delete-delete transform: second delete becomes no-op
      (text already deleted).

      Both insertions are at the same position (start of paragraph).
      Insert-insert at same position: tiebreak by userId.
      If alice < bob: Alice's text goes first.

    Result: "We must focus on delivery timelines above all else.
             Quality is the top priority for this quarter."

  Both paragraphs are preserved! But the user intended to REPLACE
  the paragraph, not append another. OT preserved both texts
  because it treats delete + insert as independent operations,
  not as a "replace" operation.

  This is the fundamental limitation: OT preserves edits
  mathematically, but the result may not match HUMAN INTENT
  when two users make large, overlapping changes.
```

### Challenge 3: Conflict Volume Scales with Offline Duration

```
Relationship between offline duration and conflict likelihood:

  Offline       Probability that      Expected user
  Duration      edits overlap with    reaction on
                other users' edits    reconnection
  ───────────────────────────────────────────────────
  < 1 min       Very low (~5%)        No surprises
  1-10 min      Low (~15%)            Minor adjustments
  10-60 min     Moderate (~40%)       Noticeable changes
  1-4 hours     High (~70%)           Significant review needed
  4-24 hours    Very high (~90%)      Major reconciliation
  > 24 hours    Near certain (99%)    Essentially a merge conflict

  The longer you are offline, the more the document has changed,
  and the more likely your edits overlap with others' edits.

  This is why Google Docs is ONLINE-FIRST:
    The product is designed for continuous connectivity.
    Offline mode is a FALLBACK, not a primary use case.
    The UI nudges users to go online ("You are offline.
    Changes will be saved when you reconnect.")
```

### Challenge 4: IndexedDB Storage Limits

```
Potential storage issues:

  1. Large documents:
     A 1MB document snapshot + 1000 pending ops (~200KB) = 1.2MB
     50 offline-enabled documents = 60MB
     Manageable within IndexedDB quota.

  2. Many pending operations:
     If a user edits heavily offline for 8 hours:
     ~5 ops/sec * 8 hours * 3600 sec/hour = 144,000 operations
     At ~200 bytes each = ~28.8 MB of pending ops
     Composition reduces this to ~1,000 composed ops = ~200 KB

  3. Browser storage pressure:
     IndexedDB shares storage quota with other origins.
     If the device is low on disk space, the browser may
     evict IndexedDB data. Service Workers can request
     persistent storage to prevent this:
       navigator.storage.persist()  // request persistent storage

  4. Cache staleness:
     If a user hasn't opened a document in weeks, the cached
     version may be very stale. Loading it offline gives a
     misleading view. The cache should have an expiry policy
     (e.g., clear cached docs not accessed in 30 days).
```

### Challenge 5: Concurrent Offline Editing by Multiple Users

```
Extreme case: Three users go offline simultaneously and edit:

  STARTING STATE (rev 100): "Hello World"

  Alice (offline): "Hello Wonderful World"  (inserted "Wonderful ")
  Bob   (offline): "Hello Beautiful World"  (inserted "Beautiful ")
  Carol (offline): "Hello World!"           (appended "!")

  All three reconnect at roughly the same time.

  The server processes them in the order they reconnect:

  1. Alice reconnects first:
     Server: "Hello World" (rev 100)
     Alice's op: insert("Wonderful ", pos=6) based on rev 100
     No intervening ops → apply directly.
     Server: "Hello Wonderful World" (rev 101)

  2. Bob reconnects second:
     Server: "Hello Wonderful World" (rev 101)
     Bob's op: insert("Beautiful ", pos=6) based on rev 100
     Transform against rev 101 (Alice's insert at pos 6):
       Same position → tiebreak by userId.
       "alice" < "bob" → Alice's insert goes first.
       Bob's insert shifts right by 10 (len of "Wonderful "):
       → insert("Beautiful ", pos=16)
     Server: "Hello Wonderful Beautiful World" (rev 102)

  3. Carol reconnects third:
     Server: "Hello Wonderful Beautiful World" (rev 102)
     Carol's op: insert("!", pos=11) based on rev 100
     Transform against rev 101 (insert 10 chars at pos 6):
       pos 6 < pos 11 → shift right by 10 → pos 21
     Transform against rev 102 (insert 10 chars at pos 16):
       pos 16 < pos 21 → shift right by 10 → pos 31
     → insert("!", pos=31)
     Server: "Hello Wonderful Beautiful World!" (rev 103)

  Final: "Hello Wonderful Beautiful World!"
    Alice's "Wonderful" ✓
    Bob's "Beautiful" ✓
    Carol's "!" ✓
  All three offline edits preserved. OT convergence holds.
```

---

## 6. Features Unavailable Offline

When offline, several collaborative features are inherently unavailable because they require server communication:

```
+---------------------------+------------------------------------------+
| Feature                   | Why It's Unavailable Offline             |
+---------------------------+------------------------------------------+
| Real-time collaboration   | No WebSocket to OT server.               |
| (seeing others' edits)    | Other users' edits are not received.     |
+---------------------------+------------------------------------------+
| Cursor presence           | No server to relay cursor positions.     |
| (seeing others' cursors)  | You don't know who else is editing.      |
+---------------------------+------------------------------------------+
| Comments from others      | New comments from other users are not     |
|                           | received. You can draft local comments   |
|                           | that will be sent on reconnect.          |
+---------------------------+------------------------------------------+
| Permission changes        | If the owner revokes your edit access     |
|                           | while you're offline, you won't know     |
|                           | until you reconnect. Your offline edits  |
|                           | may be rejected on sync.                 |
+---------------------------+------------------------------------------+
| Sharing                   | Cannot share the document with new users |
|                           | without server access.                   |
+---------------------------+------------------------------------------+
| Version history           | Cannot browse version history -- it's     |
| navigation                | stored on the server, not locally.       |
+---------------------------+------------------------------------------+
| Spell check (server-side) | Google's ML-based spell check and        |
|                           | grammar suggestions run on the server.   |
|                           | Basic browser spell check still works.   |
+---------------------------+------------------------------------------+
| Image insertion from web  | Cannot fetch images from URLs. Can       |
|                           | insert images from local disk (cached    |
|                           | as data URIs in the pending ops queue).  |
+---------------------------+------------------------------------------+
| Add-ons / extensions      | Third-party add-ons that call external   |
|                           | APIs will not function offline.          |
+---------------------------+------------------------------------------+

What IS available offline:
  - Text editing (insert, delete, copy, paste)
  - Formatting (bold, italic, headings, lists)
  - Table editing (add rows, merge cells)
  - Local spell check (browser-native)
  - Drafting comments (queued for sync)
  - Undo / redo (local operation stack)
  - Print (from cached local state)
  - Export to PDF (local rendering)
```

---

## 7. Contrast with Other Systems

### Google Docs vs Notion (Offline)

```
+----------------------------+----------------------------+
|        GOOGLE DOCS         |          NOTION            |
+----------------------------+----------------------------+
| Offline model:             | Offline model:             |
| LINEAR document with OT.   | BLOCK-BASED document with  |
| Offline edits create ops   | CRDT-inspired approach.    |
| that must be transformed   | Offline edits on one block |
| against ALL online ops     | are independent of edits   |
| on reconnect.              | on other blocks.           |
+----------------------------+----------------------------+
| Reconciliation cost:       | Reconciliation cost:       |
| O(M * N) where M = offline | O(M_b * N_b) PER BLOCK.   |
| ops and N = online ops     | M_b and N_b are ops on     |
| across the ENTIRE DOCUMENT.| the SAME block.            |
|                            | Cross-block edits: no      |
|                            | transforms needed.         |
+----------------------------+----------------------------+
| Conflict likelihood:       | Conflict likelihood:       |
| HIGH -- any edit anywhere  | LOW -- edits on different  |
| in the document affects    | blocks never conflict.     |
| the position space of all  | Conflicts only occur when  |
| other edits.               | two users edit the SAME    |
|                            | block offline.             |
+----------------------------+----------------------------+
| Offline robustness:        | Offline robustness:        |
| MODERATE -- Chrome/PWA     | HIGH -- desktop and mobile |
| only. Not all browsers.    | apps with robust offline   |
| Offline is a fallback.     | support. Notion treats     |
|                            | offline as a first-class   |
|                            | use case.                  |
+----------------------------+----------------------------+
| Offline duration tolerance:| Offline duration tolerance:|
| HOURS -- after many hours  | DAYS -- block independence |
| offline, reconnection is   | means even long offline    |
| slow and results may be    | periods have localized     |
| surprising.                | conflicts.                 |
+----------------------------+----------------------------+

Why Notion's block model helps offline:

  Notion document:
    Block 1: "Introduction" (paragraph)
    Block 2: "Background" (heading)
    Block 3: "The project started in 2024..." (paragraph)
    Block 4: [Image: architecture diagram]
    Block 5: "Conclusion" (paragraph)

  Alice (offline): Edits Block 3 (changes a sentence).
  Bob (online): Edits Block 5 (adds a conclusion sentence).

  Conflict? NO. Blocks 3 and 5 are independent.
  Alice's edits to Block 3 do not require ANY transformation
  against Bob's edits to Block 5.

  In Google Docs:
  Alice's edits are at position 150 (within the "Background" section).
  Bob's edits are at position 400 (within the "Conclusion" section).
  Even though they're editing different sections, Bob's insert at
  position 400 is based on rev 100. If Alice's insert at position 150
  is at rev 100 too, Bob's insert must be transformed against Alice's
  (position 400 may shift to 405 if Alice inserted 5 chars before it).

  The LINEAR model creates position dependencies even for edits
  in unrelated sections.
```

### Google Docs vs Apple iWork (Pages, Numbers, Keynote)

```
+----------------------------+----------------------------+
|        GOOGLE DOCS         |     APPLE iWORK            |
+----------------------------+----------------------------+
| Sync mechanism:            | Sync mechanism:            |
| OT with operation log.     | iCloud sync with           |
| Server transforms ops.     | LAST-WRITER-WINS or        |
| All edits preserved.       | automatic conflict          |
|                            | resolution at the record   |
|                            | level.                     |
+----------------------------+----------------------------+
| Offline capability:        | Offline capability:        |
| Browser-based, requires    | NATIVE APP. Full offline   |
| Chrome extension or PWA.   | editing. Documents stored  |
| Limited browser support.   | as local files on disk.    |
|                            | Sync via iCloud when       |
|                            | online.                    |
+----------------------------+----------------------------+
| Conflict resolution:       | Conflict resolution:       |
| Character-level OT.        | Record/field-level merge.  |
| Every edit preserved.      | For conflicting changes to |
|                            | the same field, last write |
|                            | wins. Some edits may be    |
|                            | silently overwritten.      |
+----------------------------+----------------------------+
| Real-time collaboration:   | Real-time collaboration:   |
| YES -- sub-200ms latency.  | YES (introduced later).    |
| Core product feature.      | Less mature than Google    |
|                            | Docs' implementation.      |
+----------------------------+----------------------------+
| Storage model:             | Storage model:             |
| Cloud-native. No local     | FILE-BASED. .pages files   |
| file. Document exists as   | stored locally AND in      |
| operation log + snapshots  | iCloud. Can email the file.|
| on Google servers.         |                            |
+----------------------------+----------------------------+
| Cross-platform:            | Cross-platform:            |
| Any browser (online mode). | Apple devices only          |
| Chrome only (offline).     | (Mac, iPad, iPhone).       |
|                            | iCloud.com web version     |
|                            | has limited features.      |
+----------------------------+----------------------------+

Apple's approach to offline conflicts:

  iWork uses iCloud's "record-level" conflict resolution:
    - A Keynote slide is a "record."
    - If Alice changes slide 5 offline and Bob changes slide 10 offline,
      no conflict -- different records.
    - If Alice AND Bob both change slide 5, iCloud detects a conflict:
      - For simple field changes (title, text): last write wins.
      - For complex changes: may show "Conflict" dialog letting
        the user choose which version to keep.
    - Unlike OT, iWork does NOT preserve both users' edits to the
      same field. One user's edit is lost (the earlier one).

  This is simpler to implement but provides weaker guarantees
  than Google Docs' OT. In Google Docs, both users' edits to
  the same paragraph are always preserved (interleaved by OT).
```

### Summary Comparison Table

```
                      Google Docs    Notion         Dropbox        Apple iWork
                      ────────────   ───────────    ────────       ───────────
Offline support       Moderate       Strong         Strong         Strong
Sync mechanism        OT             CRDT-inspired  File sync      iCloud sync
Conflict resolution   Automatic      Automatic      Conflicted     Last-writer-
                      (char-level)   (block-level)  copies         wins
Data loss on conflict None           None           Possible       Possible
Reconciliation cost   O(M*N)         O(M_b*N_b)     O(1) per file  O(1) per record
                      (global)       (per block)
Offline editing UX    Good           Excellent      N/A (file      Good
                                                    sync, not
                                                    editing)
Real-time collab      Excellent      Good           None           Moderate
```

---

## Interview Tips: What to Emphasize

### L6 Expectations for Offline Support

When discussing offline support in a system design interview, an L6 candidate should:

1. **Acknowledge that offline is secondary.** Google Docs is online-first. Offline mode is a fallback, not the primary use case. This shows you understand product priorities.

2. **Walk through the reconnection sync flow.** The 6-step protocol (connect, send queued ops, server transforms, server sends missed ops, client applies, convergence). Be specific about what happens at each step.

3. **Quantify the O(M x N) cost.** Show that 500 offline ops against 2000 server ops = 1M transforms, and estimate the time (~1-10 seconds). Propose mitigations (composition to reduce M).

4. **Identify the user experience problem.** Mathematically correct reconciliation can still surprise users. Two users rewriting the same paragraph offline produces a confusing merged result. This is a product/UX challenge, not just an engineering one.

5. **Contrast with block-based systems.** Explain why Notion's offline story is simpler -- block independence means edits to different blocks never conflict, dramatically reducing reconciliation cost.

### L7 and Beyond

An L7 candidate would additionally discuss:
- Progressive sync (send and transform operations in batches during reconnection, showing partial progress)
- Offline conflict preview (before applying reconciled changes, show the user a diff of what changed while they were offline)
- Offline permission enforcement (what if access was revoked while offline -- reject all pending ops? Notify but preserve?)
- Mobile-specific offline challenges (app may be killed by OS, must persist state to disk proactively)
- Bandwidth optimization for reconnection (delta compression of operations, batched WebSocket frames)

---

*This is a companion document to the main interview simulation. For the full interview dialogue, see [01-interview-simulation.md](01-interview-simulation.md).*
*For conflict resolution, see [06-conflict-resolution-and-consistency.md](06-conflict-resolution-and-consistency.md).*
*For document storage, see [04-document-storage.md](04-document-storage.md).*
