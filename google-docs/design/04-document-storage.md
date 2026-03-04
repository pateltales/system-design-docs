# Deep Dive: Document Storage & State Management

> **Companion document to [01-interview-simulation.md](01-interview-simulation.md) -- Phase 6**
> This document expands on the document storage and state management discussion from the main interview simulation.

---

## Table of Contents

1. [Document Representation -- What IS a Google Doc?](#1-document-representation)
2. [Why Not HTML? Why Not Markdown?](#2-why-not-html-why-not-markdown)
3. [The Operation Log (Event Sourcing)](#3-the-operation-log-event-sourcing)
4. [Snapshots (Compaction)](#4-snapshots-compaction)
5. [Storage Layer Infrastructure](#5-storage-layer-infrastructure)
6. [Document Loading Flow](#6-document-loading-flow)
7. [Document Size Limits](#7-document-size-limits)
8. [Contrast with Other Systems](#8-contrast-with-other-systems)

---

## 1. Document Representation

### A Google Doc is NOT a File

A Google Doc is **not** a `.docx`, `.odt`, or `.html` file sitting on a disk somewhere. There is no "Google_Doc_123.docx" that gets opened, modified, and saved. Instead, a Google Doc is a **structured data model** -- a linear sequence of annotated items that exists as a combination of an operation log and periodic snapshots.

### The Annotated Linear Sequence

Internally, a Google Doc is best understood as a **flat, ordered sequence** of items. Each item is either:

- A **character** with associated formatting attributes
- A **structural marker** (paragraph break, heading delimiter, list marker, table boundary, image placeholder)

```
Document: "Hello World" (where "Hello" is bold, 11pt Arial)

Internal representation (conceptual):

Index:  0     1     2     3     4     5     6     7     8     9     10
Char:   H     e     l     l     o    ' '    W     o     r     l     d
Attrs:  bold  bold  bold  bold  bold  ---   ---   ---   ---   ---   ---
        11pt  11pt  11pt  11pt  11pt  11pt  11pt  11pt  11pt  11pt  11pt
        Arial Arial Arial Arial Arial Arial Arial Arial Arial Arial Arial
```

### Structural Markers in the Sequence

Structural elements like paragraph breaks, headings, and tables are represented as special markers within the same linear sequence:

```
Document with a heading and paragraph:

"My Title" (Heading 1)
"Some body text."

Linear sequence:
  [H1_START] M y   T i t l e [H1_END] [PARA_BREAK]
  [PARA_START] S o m e   b o d y   t e x t . [PARA_END]

Every item -- characters and structural markers alike --
has a position index. OT operations reference these indices.
```

### Why Linear? The Key Insight

The document is **linear** (a flat sequence) rather than **hierarchical** (a tree). This is the single most important design decision for the document model, and it was learned from the failure of Google Wave.

```
LINEAR model (Google Docs):
  Position 0: char 'H' {bold}
  Position 1: char 'e' {bold}
  Position 2: char 'l' {bold}
  ...
  OT operations: insert(pos, text), delete(pos, count), format(pos, len, attrs)
  Transform complexity: O(N) per pair, N = operation length

TREE model (Google Wave):
  <document>
    <paragraph id="p1">
      <text style="bold">Hello</text>
      <text> World</text>
    </paragraph>
  </document>
  OT operations: insert_node, delete_node, move_node, set_attribute, insert_text...
  Transform complexity: O(N^2) per pair in worst case, massive combinatorial explosion
```

---

## 2. Why Not HTML? Why Not Markdown?

### Why Not HTML?

HTML is a tree structure (the DOM). OT on tree structures requires transform functions for:

| Tree Operation | What It Does |
|---|---|
| `insertNode(parent, index, node)` | Insert a child element |
| `deleteNode(parent, index)` | Remove a child element |
| `moveNode(from, to)` | Reparent a node |
| `setAttribute(node, key, value)` | Change an attribute |
| `insertText(node, offset, text)` | Insert text within a text node |
| `deleteText(node, offset, count)` | Delete text within a text node |

The transform function matrix for these operations is enormous. Every pair must be handled:

```
                insertNode  deleteNode  moveNode  setAttribute  insertText  deleteText
insertNode         X            X          X           X            X           X
deleteNode         X            X          X           X            X           X
moveNode           X            X          X           X            X           X
setAttribute       X            X          X           X            X           X
insertText         X            X          X           X            X           X
deleteText         X            X          X           X            X           X

= 36 transform function pairs (minimum)
  Many with sub-cases (same parent? overlapping ranges? ancestor relationships?)
  Total distinct cases: easily 100+
```

**Google Wave tried this and failed.** The Wave OT protocol (documented in David Wang's 2010 whitepaper) operated on XML tree-structured documents. The transform function complexity was enormous, the code was bug-prone, and performance was poor. Google explicitly moved away from tree OT when building Google Docs.

With a **linear model**, the transform matrix shrinks dramatically:

```
              retain   insert   delete   format
retain          -        -        -        -       (retain vs anything = trivial)
insert          -        X        X        X
delete          -        X        X        X
format          -        X        X        X

= ~9 meaningful transform pairs
  Much simpler, much easier to prove correct
```

### Why Not Markdown?

Markdown is plain text with formatting syntax (`**bold**`, `# Heading`). While this makes OT simpler (it is just text OT), it has fundamental problems for a product like Google Docs:

| Problem | Impact |
|---|---|
| **Not WYSIWYG** | Users see `**bold**` instead of **bold** while editing. Google Docs must be what-you-see-is-what-you-get. |
| **Ambiguous parsing** | Markdown has multiple dialects (GFM, CommonMark, etc.) with different parsing rules. Edge cases abound. |
| **Limited formatting** | No colored text, no font selection, no precise table layout, no inline images at arbitrary positions. |
| **OT on syntax is fragile** | Inserting a character inside `**bold**` might break the formatting syntax. Example: inserting `*` turns `**bold**` into `***bold**` which changes meaning. |
| **Copy-paste complexity** | Pasting formatted text requires converting to Markdown syntax, which is lossy and surprising to users. |

### The Custom Intermediate Representation

Google Docs uses a **custom intermediate representation** that is:

- **Linear** (flat sequence, not a tree) -- simplifies OT
- **Rich** (each item carries formatting attributes) -- supports WYSIWYG
- **Precise** (every character has an unambiguous position) -- enables correct OT transforms
- **Extensible** (new structural markers can be added) -- supports tables, images, etc.

This is neither HTML nor Markdown. It is a purpose-built format optimized for the specific requirements of real-time collaborative OT on rich text.

---

## 3. The Operation Log (Event Sourcing)

### The Log is the Source of Truth

Every edit to a Google Doc is recorded as an **immutable entry** in an **append-only operation log**. The document "state" at any point in time is derived by replaying the log from the beginning (or from a snapshot).

This is **event sourcing** -- the same pattern used in financial systems, CQRS architectures, and database write-ahead logs.

### Log Entry Format

Each entry in the operation log contains:

```
{
  "operationId": "op-uuid-12345",           // globally unique
  "userId":      "alice@gmail.com",          // who made the edit
  "timestamp":   "2026-02-21T10:05:23.456Z", // when (server time)
  "operation":   [retain(5), insert("X"), retain(95)],  // the OT operation
  "parentRevision": 41,                      // the server revision this op was based on
  "serverRevision": 42                       // the revision assigned by the server after transform + apply
}
```

### Full Operation Log Example

```
Operation Log for document doc-abc-123:

+------+--------+----------------------------------------+---------------------------+--------+
| Rev  | User   | Operation                              | Timestamp                 | Parent |
+------+--------+----------------------------------------+---------------------------+--------+
|    1 | alice  | [insert("Hello")]                      | 2026-02-21T10:00:00.000Z  |      0 |
|    2 | alice  | [retain(5), insert(" World")]           | 2026-02-21T10:00:01.200Z  |      1 |
|    3 | bob    | [retain(5), delete(6)]                  | 2026-02-21T10:00:05.600Z  |      2 |
|    4 | bob    | [retain(5), insert(" Docs")]            | 2026-02-21T10:00:06.000Z  |      3 |
|    5 | alice  | [format(0, 5, {bold: true})]            | 2026-02-21T10:00:10.400Z  |      4 |
|    6 | carol  | [retain(10), insert("!")]               | 2026-02-21T10:00:15.800Z  |      5 |
|  ... | ...    | ...                                    | ...                       |    ... |
| 1000 | bob    | [retain(42), insert("conclusion")]      | 2026-02-21T14:30:00.000Z  |    999 |
+------+--------+----------------------------------------+---------------------------+--------+

State reconstruction:
  At rev 0:    ""
  At rev 1:    "Hello"
  At rev 2:    "Hello World"
  At rev 3:    "Hello"
  At rev 4:    "Hello Docs"
  At rev 5:    "Hello Docs"  (with "Hello" bolded)
  At rev 6:    "Hello Docs!" (with "Hello" bolded)
```

### Event Sourcing Benefits

| Benefit | How It Works |
|---|---|
| **Complete audit trail** | Every edit, by whom, when. Who deleted that paragraph? Check the log. |
| **Revision history** | Navigate to any revision by replaying the log up to that point. The "Version History" feature in Google Docs is built directly on this. |
| **Undo** | Apply the inverse of an operation. However, OT-aware undo is non-trivial: the inverse must be transformed against all subsequent operations to account for concurrent edits. |
| **Debugging** | If the document is in a bad state, replay the log to find the offending operation. Invaluable for diagnosing OT bugs (which are notoriously subtle). |
| **Conflict resolution** | The log provides the total ordering of operations. Any client can reconstruct the exact same state by replaying the same log. |
| **Disaster recovery** | The log is replicated across multiple regions. Even if a data center fails, the complete history is preserved elsewhere. |

### Event Sourcing Costs

| Cost | Impact | Mitigation |
|---|---|---|
| **Unbounded growth** | A heavily edited document could accumulate millions of operations over its lifetime. At ~100-200 bytes per operation, a document with 1 million ops consumes ~100-200 MB of log data. | Snapshots (compaction) + archival of old operations to cold storage. |
| **Replay latency** | Loading a fresh client by replaying 1 million operations takes minutes. Unacceptable for a product where documents must open in seconds. | Snapshots reduce replay to only the operations since the last snapshot. |
| **Storage cost at scale** | With billions of active documents, the aggregate operation log storage is in the **petabytes**. At Google's scale, this is a significant infrastructure cost. | Tiered storage: hot (recent ops) in Bigtable, warm (older ops) in cheaper replicas, cold (archived ops pre-snapshot) in GCS/Colossus. |
| **Replay correctness** | Replaying operations requires the OT transform functions to be deterministic and consistent across all versions of the software. A change to the transform logic could cause replayed documents to diverge from their expected state. | Snapshot as a "checkpoint" that doesn't require replaying ancient operations. Version the transform functions. |

---

## 4. Snapshots (Compaction)

### The Problem Snapshots Solve

Without snapshots, loading a document means replaying the **entire** operation log from the very first edit:

```
WITHOUT SNAPSHOTS:

Document created 3 years ago. 500,000 operations in the log.

Loading:
  1. Read all 500,000 operations from storage            ~2 seconds
  2. Replay all 500,000 operations sequentially           ~5 seconds
  3. Total document load time:                            ~7 seconds

  Unacceptable. Users expect < 2 second load times.
```

### How Snapshots Work

A snapshot is a **full capture of the document state** at a specific revision. It includes:

- Complete document content (all characters with their formatting attributes)
- All structural markers (paragraphs, headings, tables, images)
- Comment anchor positions
- Current revision number

```
WITH SNAPSHOTS:

Document created 3 years ago. 500,000 operations total.
Snapshot taken at operation 499,850. 150 operations since snapshot.

Loading:
  1. Read snapshot (one blob, ~50 KB)                     ~50 ms
  2. Read 150 recent operations                           ~20 ms
  3. Replay 150 operations against snapshot                ~5 ms
  4. Total document load time:                            ~75 ms

  Fast. User sees the document almost instantly.
```

### Snapshot Strategy

```
Snapshot Trigger Conditions:

  Condition 1: Every N operations (e.g., 100-500)
    - After operation 100, 200, 300, ... take a snapshot
    - Guarantees bounded replay on load
    - N is tuned based on operation size and replay cost

  Condition 2: Every M minutes of active editing (e.g., 5-15 minutes)
    - Even if fewer than N operations, periodic snapshots
      ensure freshness
    - Captures the state for "Version History" navigation

  Condition 3: On significant events
    - User explicitly creates a "named version"
    - Document is shared with new users (pre-warm for fast load)
    - Before major migrations or format changes

Snapshot Decision Flow:

  On every operation applied:
    ops_since_snapshot++
    if (ops_since_snapshot >= N) OR (time_since_snapshot >= M_minutes):
      trigger_snapshot()
      ops_since_snapshot = 0
```

### Snapshot Lifecycle

```
Timeline of a document's operation log and snapshots:

  Ops 1-100        [SNAPSHOT S1 at op 100]
  Ops 101-200      [SNAPSHOT S2 at op 200]
  Ops 201-300      [SNAPSHOT S3 at op 300]
  Ops 301-450      [SNAPSHOT S4 at op 450]    <-- latest snapshot
  Ops 451-475                                 <-- active operations (in hot storage)

Storage tiers:

  HOT  (Bigtable):   Ops 451-475 + pointer to snapshot S4
                      These are needed for active editing and new client loads.

  WARM (Bigtable):   Ops 301-450 + snapshots S3, S4
                      Needed for recent version history navigation.

  COLD (GCS/Colossus): Ops 1-300 + snapshots S1, S2
                        Rarely accessed. Needed for deep version history
                        or compliance/audit requests.
                        Cheaper storage. Higher access latency (acceptable).

Garbage collection:
  - Old operations BEFORE a snapshot can be archived to cold storage.
  - They are NOT deleted -- regulatory and audit requirements
    may demand complete history.
  - Old snapshots can be thinned out (keep every 10th snapshot
    in cold storage instead of every one).
```

### Snapshot Contents

```json
{
  "documentId": "doc-abc-123",
  "revision": 450,
  "timestamp": "2026-02-21T14:00:00.000Z",
  "content": {
    "items": [
      {"type": "heading_start", "level": 1},
      {"type": "char", "value": "M", "attrs": {"bold": true, "font": "Arial", "size": 18}},
      {"type": "char", "value": "y", "attrs": {"bold": true, "font": "Arial", "size": 18}},
      {"type": "char", "value": " ", "attrs": {"bold": true, "font": "Arial", "size": 18}},
      {"type": "char", "value": "D", "attrs": {"bold": true, "font": "Arial", "size": 18}},
      {"type": "char", "value": "o", "attrs": {"bold": true, "font": "Arial", "size": 18}},
      {"type": "char", "value": "c", "attrs": {"bold": true, "font": "Arial", "size": 18}},
      {"type": "heading_end"},
      {"type": "paragraph_break"},
      {"type": "char", "value": "S", "attrs": {"font": "Arial", "size": 11}},
      {"type": "char", "value": "o", "attrs": {"font": "Arial", "size": 11}},
      {"type": "char", "value": "m", "attrs": {"font": "Arial", "size": 11}},
      {"type": "char", "value": "e", "attrs": {"font": "Arial", "size": 11}},
      {"type": "char", "value": " ", "attrs": {"font": "Arial", "size": 11}},
      {"type": "char", "value": "t", "attrs": {"font": "Arial", "size": 11}},
      {"type": "char", "value": "e", "attrs": {"font": "Arial", "size": 11}},
      {"type": "char", "value": "x", "attrs": {"font": "Arial", "size": 11}},
      {"type": "char", "value": "t", "attrs": {"font": "Arial", "size": 11}}
    ],
    "comments": [
      {
        "commentId": "comment-1",
        "anchorStart": 8,
        "anchorEnd": 17,
        "text": "This needs more detail",
        "author": "bob@gmail.com",
        "resolved": false
      }
    ]
  },
  "checksum": "sha256:a1b2c3d4..."
}
```

> **Note:** In practice, the snapshot would be more efficiently encoded (not one JSON object per character). Run-length encoding of attributes, protobuf serialization, and compression would dramatically reduce the snapshot size. The above is a conceptual representation.

---

## 5. Storage Layer Infrastructure

### Storage System Selection [INFERRED]

Google likely uses a combination of purpose-built storage systems, each optimized for a specific access pattern:

```
+------------------------------------------------------------------+
|                    STORAGE ARCHITECTURE                           |
|                                                                   |
|  +------------------------+    +-------------------------------+  |
|  |   OPERATION LOG        |    |   DOCUMENT SNAPSHOTS          |  |
|  |   (Bigtable)           |    |   (GCS / Colossus)            |  |
|  |                        |    |                               |  |
|  |  Access pattern:       |    |  Access pattern:              |  |
|  |  - Append-heavy        |    |  - Write once, read on load   |  |
|  |  - Sequential reads    |    |  - Large blobs (10KB-1MB)     |  |
|  |  - Per-document scans  |    |  - Infrequent reads           |  |
|  |                        |    |                               |  |
|  |  Row key:              |    |  Object key:                  |  |
|  |  doc_id + revision     |    |  doc_id + snapshot_revision   |  |
|  |                        |    |                               |  |
|  |  Why Bigtable:         |    |  Why GCS/Colossus:            |  |
|  |  - Designed for        |    |  - Blob storage is cheap      |  |
|  |    append-heavy writes |    |  - High durability (11 9's)   |  |
|  |  - Efficient range     |    |  - No need for random access  |  |
|  |    scans (all ops for  |    |    within the snapshot         |  |
|  |    a doc since rev X)  |    |  - Lifecycle management       |  |
|  |  - Auto-sharding by    |    |    (auto-archive old          |  |
|  |    row key             |    |    snapshots to cheaper        |  |
|  |  - High write          |    |    storage classes)            |  |
|  |    throughput           |    |                               |  |
|  +------------------------+    +-------------------------------+  |
|                                                                   |
|  +------------------------+    +-------------------------------+  |
|  |   DOCUMENT METADATA    |    |   PRESENCE & CURSOR STATE     |  |
|  |   (Spanner)            |    |   (In-memory / Redis)         |  |
|  |                        |    |                               |  |
|  |  Access pattern:       |    |  Access pattern:              |  |
|  |  - Read-heavy          |    |  - Extremely high frequency   |  |
|  |  - Point lookups       |    |  - Ephemeral (lost on         |  |
|  |  - Globally consistent |    |    disconnect)                |  |
|  |  - Strong consistency  |    |  - No persistence needed      |  |
|  |    required            |    |                               |  |
|  |                        |    |  Storage:                     |  |
|  |  Data:                 |    |  - In-memory maps on the      |  |
|  |  - doc_id, title       |    |    OT server                  |  |
|  |  - owner, permissions  |    |  - Or Redis for shared        |  |
|  |  - sharing settings    |    |    presence across gateway     |  |
|  |  - latest_snapshot_rev |    |    servers                     |  |
|  |  - created/modified    |    |                               |  |
|  |                        |    |  Why in-memory:               |  |
|  |  Why Spanner:          |    |  - Microsecond latency        |  |
|  |  - Globally replicated |    |  - No durability needed       |  |
|  |  - Strongly consistent |    |  - Cursor data is recreated   |  |
|  |  - TrueTime for        |    |    on every reconnect          |  |
|  |    external            |    |                               |  |
|  |    consistency         |    |                               |  |
|  +------------------------+    +-------------------------------+  |
+------------------------------------------------------------------+
```

### Why Bigtable for the Operation Log

Bigtable is Google's sorted key-value store designed for high-throughput, append-heavy workloads. The operation log is a natural fit:

```
Bigtable Row Key Design for Operation Log:

  Row Key: doc_id#revision_number (zero-padded)

  Example rows for document "doc-abc-123":
    doc-abc-123#0000000001  →  {userId: alice, op: [insert("Hello")], ts: ...}
    doc-abc-123#0000000002  →  {userId: alice, op: [retain(5), insert(" World")], ts: ...}
    doc-abc-123#0000000003  →  {userId: bob,   op: [retain(5), delete(6)], ts: ...}
    ...
    doc-abc-123#0000001000  →  {userId: bob,   op: [retain(42), insert("conclusion")], ts: ...}

  Access patterns:
    1. Append new operation: Write to doc-abc-123#(latest_rev + 1)
       → Single row write. Bigtable handles this efficiently.

    2. Read operations since snapshot: Scan from doc-abc-123#(snapshot_rev + 1) to end
       → Range scan. Bigtable stores rows sorted by key, so this is a
         sequential read -- fast.

    3. Read single operation: Get doc-abc-123#(specific_rev)
       → Point lookup. Fast.

  Why zero-padded revision numbers?
    Bigtable sorts keys lexicographically. Without padding:
      doc-abc-123#1, doc-abc-123#10, doc-abc-123#2 (wrong order!)
    With padding:
      doc-abc-123#0000000001, doc-abc-123#0000000002, doc-abc-123#0000000010 (correct!)
```

### Why Spanner for Document Metadata

Permission checks must be **strongly consistent** and **globally available**:

```
Scenario: Permission enforcement

  1. Alice (in Tokyo) is editing document doc-abc-123.
  2. Owner (in New York) removes Alice's edit access.
  3. Permission change is written to Spanner.
  4. Spanner guarantees external consistency via TrueTime:
     - After the write completes, ALL subsequent reads
       worldwide will see the updated permission.
  5. Alice's next operation hits the OT server.
  6. OT server checks permission in Spanner (or cached, with
     push-based invalidation).
  7. Alice's operation is rejected. Her client switches to
     read-only mode.

  If we used an eventually consistent store:
    - Alice might continue editing for seconds or minutes
      after her access was revoked.
    - Security violation.

  Spanner's strong consistency eliminates this window.
```

### Why GCS/Colossus for Snapshots

Snapshots are large blobs written once and read infrequently:

```
Snapshot storage characteristics:
  - Size: 10 KB to 1 MB per snapshot (typical document)
  - Write: Once, when the snapshot is taken
  - Read: Once per document load (or version history navigation)
  - Lifecycle: Recent snapshots in standard storage,
               old snapshots in Nearline/Coldline/Archive

Cost comparison (illustrative, [INFERRED]):
  - Bigtable: ~$0.65/GB/month (SSD) -- expensive for blobs
  - GCS Standard: ~$0.02/GB/month -- 32x cheaper
  - GCS Coldline: ~$0.004/GB/month -- 162x cheaper than Bigtable

  For billions of snapshots, this cost difference is enormous.
```

---

## 6. Document Loading Flow

When a user opens a Google Doc, the following sequence occurs:

```
Document Loading Flow (5 Steps):

  User clicks "Open Document"
         |
         v
  +----------------------------------------------+
  | STEP 1: Load Metadata                        |
  |                                               |
  |  Client → REST API: GET /documents/{docId}    |
  |  Server → Spanner: Read metadata              |
  |    - Title, owner, sharing settings            |
  |    - User's permission level                   |
  |    - Latest snapshot revision pointer           |
  |  Server → Client: Metadata + permission level  |
  |                                               |
  |  If permission = NONE → 403 Forbidden, stop.  |
  |  Latency: ~50-100ms                           |
  +----------------------------------------------+
         |
         v
  +----------------------------------------------+
  | STEP 2: Load Snapshot + Replay Recent Ops     |
  |                                               |
  |  Server → GCS: Read latest snapshot            |
  |    - Full document state at revision S          |
  |    - Typically 10KB-1MB                         |
  |  Server → Bigtable: Read ops from S+1 to HEAD |
  |    - Typically 0-500 operations                |
  |    - ~100 bytes per op                         |
  |  Server: Replay ops against snapshot           |
  |    - Produces current document state            |
  |  Server → Client: Full document content        |
  |                                               |
  |  Latency: ~100-500ms (depending on ops count) |
  +----------------------------------------------+
         |
         v
  +----------------------------------------------+
  | STEP 3: Establish WebSocket Connection        |
  |                                               |
  |  Client → Gateway: WS /documents/{docId}/     |
  |                    collaborate                 |
  |  Gateway: Auth check, permission verification  |
  |  Gateway → OT Server: Route based on doc_id   |
  |  OT Server: Register client, assign user color |
  |  OT Server → Client: Current server revision,  |
  |                       list of active editors,   |
  |                       cursor positions          |
  |                                               |
  |  Latency: ~50-200ms (TCP + TLS + WS handshake)|
  +----------------------------------------------+
         |
         v
  +----------------------------------------------+
  | STEP 4: Catch-Up (Close the Gap)              |
  |                                               |
  |  Between Step 2 and Step 3, other editors      |
  |  may have made new edits. There is a gap       |
  |  between the document state the client         |
  |  received and the current server state.         |
  |                                               |
  |  OT Server → Client: All operations from       |
  |    the client's revision to the server's        |
  |    current revision.                            |
  |                                               |
  |  Client: Apply catch-up operations to local    |
  |          document state.                        |
  |                                               |
  |  Latency: ~10-50ms (usually just a few ops)   |
  +----------------------------------------------+
         |
         v
  +----------------------------------------------+
  | STEP 5: Render + Begin Editing                |
  |                                               |
  |  Client: Render the document in the editor     |
  |  Client: Display other editors' cursors        |
  |  Client: Enable editing (if permission allows) |
  |  Client: Enter SYNCHRONIZED state              |
  |    (client and server are at the same revision)|
  |                                               |
  |  User sees the document and can begin typing.  |
  |                                               |
  |  Total time from click to editable document:   |
  |  ~200ms - 1 second (typical)                   |
  +----------------------------------------------+
```

### Optimizations for Fast Loading

| Optimization | How It Works |
|---|---|
| **Snapshot freshness** | Trigger snapshots aggressively for frequently opened documents. Fewer ops to replay = faster load. |
| **Edge caching** | Cache recent snapshots at edge locations near users. A document opened by 1000 people in the same office only needs one GCS read. |
| **Pre-warming** | When a document is shared with new users, proactively create a fresh snapshot so their first load is fast. |
| **Progressive rendering** | Show the first page of the document immediately, then load the rest in the background. Users start reading (and often start editing at the top) while the rest loads. |
| **Metadata prefetch** | When the user sees a list of documents (Google Drive), prefetch metadata for documents they are likely to open. |

---

## 7. Document Size Limits

Google Docs enforces a limit of approximately **1.02 million characters** per document. This is not arbitrary -- it is a **performance limit** driven by OT complexity:

### Why OT Complexity Grows with Document Size

```
OT operations reference CHARACTER POSITIONS:
  insert(pos=500000, text="X")  -- insert at the 500,000th character
  delete(pos=999999, count=1)   -- delete the 999,999th character

Transform functions walk through operations component by component:
  transform([retain(500000), insert("X")], [retain(999999), delete(1)])

The retain components scale with DOCUMENT SIZE.
A 1 million character document means operations routinely have
retain(large_number) components, and the transform function must
process these.

Operation size:
  - In a 100-char document: [retain(50), insert("X"), retain(50)]
    3 components, ~30 bytes

  - In a 1M-char document: [retain(500000), insert("X"), retain(500000)]
    3 components, ~30 bytes (same!)
    BUT: the positions (500000) require more computation to validate
    and more care to transform correctly.

The real cost:
  - Server memory: The in-memory document model for a 1M-char document
    with formatting is ~10-50 MB. With 100 such documents on one OT server,
    that is 1-5 GB of RAM just for document state.

  - Transform latency: More characters = more edge cases in position
    arithmetic. While individual transforms are O(1) for position shifts,
    the probability of complex overlapping ranges increases with size.

  - Client rendering: The browser must render 1M characters with formatting.
    DOM manipulation at this scale is slow. Virtualized rendering helps
    but adds complexity.
```

### Comparison of Size Limits

| System | Limit | Why |
|---|---|---|
| **Google Docs** | ~1.02M characters | OT position complexity, client rendering performance |
| **Microsoft Word Online** | No hard character limit (but 50 MB file size) | File-based model, less sensitive to character count |
| **Notion** | No hard per-page limit (but recommends splitting) | Block-based model -- each block is independent, so total page length matters less |
| **Google Sheets** | 10M cells | Different model (cells, not characters) |

---

## 8. Contrast with Other Systems

### Google Docs vs Dropbox

```
+---------------------------+---------------------------+
|       GOOGLE DOCS         |        DROPBOX            |
+---------------------------+---------------------------+
| Document = structured     | Document = file (binary   |
| data model (op log +      | blob on disk)             |
| snapshots)                |                           |
+---------------------------+---------------------------+
| Edits = operations        | Edits = save entire file  |
| (insert, delete, format)  | (upload full .docx on     |
| sent in real-time         | every save)               |
+---------------------------+---------------------------+
| Conflicts resolved via    | Conflicts create          |
| OT automatically          | "conflicted copy" files   |
| in real-time              | that the user must        |
|                           | manually reconcile        |
+---------------------------+---------------------------+
| Storage: operation log    | Storage: file versions    |
| + periodic snapshots      | (full file per version)   |
| (efficient -- only        | (wasteful -- 50KB file    |
| changed data is stored)   | stored in full even for   |
|                           | a 1-character change)     |
+---------------------------+---------------------------+
| Optimized for: real-time  | Optimized for: file sync  |
| collaborative editing     | across devices            |
+---------------------------+---------------------------+
| File format: custom IR    | File format: native       |
| (NOT .docx)               | (.docx, .pdf, any file)   |
+---------------------------+---------------------------+
| Loading: metadata +       | Loading: download the     |
| snapshot + replay ops     | full file from Dropbox    |
| (fast, incremental)       | servers (entire file)     |
+---------------------------+---------------------------+
```

### Google Docs vs Notion

```
+---------------------------+---------------------------+
|       GOOGLE DOCS         |        NOTION             |
+---------------------------+---------------------------+
| Document model: LINEAR    | Document model: BLOCK-    |
| annotated sequence        | BASED (each paragraph,    |
| (characters + markers)    | heading, image, toggle    |
|                           | is an independent block)  |
+---------------------------+---------------------------+
| Collaboration: OT on the  | Collaboration: CRDT-      |
| full linear document.     | inspired approach on      |
| Any edit affects the      | individual blocks.        |
| entire position space.    | Blocks are independent -- |
|                           | editing block A does not  |
|                           | affect block B.           |
+---------------------------+---------------------------+
| Conflict likelihood:      | Conflict likelihood:      |
| HIGHER -- any two edits   | LOWER -- two users        |
| can potentially conflict  | editing different blocks  |
| because they share the    | never conflict. Only      |
| same position space.      | same-block edits need     |
|                           | resolution.               |
+---------------------------+---------------------------+
| Offline: LIMITED           | Offline: ROBUST           |
| (Chrome extension/PWA,    | (Blocks are independent,  |
| all offline ops must be   | so offline conflicts are  |
| transformed against all   | localized to individual   |
| online ops on reconnect)  | blocks, reducing          |
|                           | transform cost)           |
+---------------------------+---------------------------+
| Rich formatting: FULL     | Rich formatting: MODERATE |
| (fonts, colors, precise   | (block types, basic       |
| table layouts, headers,   | formatting within blocks, |
| footers, page breaks)     | no fine-grained layout    |
|                           | control)                  |
+---------------------------+---------------------------+
| Best for: Traditional     | Best for: Knowledge base, |
| documents (reports,       | wikis, project management |
| letters, papers)          | docs, flexible content    |
+---------------------------+---------------------------+
```

### Google Docs vs Microsoft 365 (Word Online)

```
+---------------------------+---------------------------+
|       GOOGLE DOCS         |   MICROSOFT 365 (WORD)    |
+---------------------------+---------------------------+
| Collaboration: OT         | Collaboration: Hybrid     |
| (centralized,             | approach -- uses "Fluid   |
| server-authoritative)     | Framework" (CRDT-like)    |
|                           | for Word Online co-       |
|                           | authoring [INFERRED]      |
+---------------------------+---------------------------+
| Native format: custom IR  | Native format: .docx      |
| (no file on disk)         | (file format, even for    |
|                           | online editing)           |
+---------------------------+---------------------------+
| Offline: Limited           | Offline: Better           |
| (Chrome/PWA only)         | (desktop Word app with    |
|                           | OneDrive sync)            |
+---------------------------+---------------------------+
| Feature depth: Good for   | Feature depth: Deeper     |
| most documents. Some      | formatting, mail merge,   |
| advanced Word features    | macros, advanced tables.  |
| not supported.            | Desktop app parity.       |
+---------------------------+---------------------------+
| Storage: Operation log    | Storage: .docx files in   |
| + snapshots in Google's   | OneDrive/SharePoint       |
| infrastructure            | with versioning           |
+---------------------------+---------------------------+
| Import/Export: Can         | Native: .docx IS the      |
| import/export .docx       | format. No import/export  |
| but it is a conversion    | step needed.              |
| (potentially lossy)       |                           |
+---------------------------+---------------------------+
```

---

## Interview Tips: What to Emphasize

### L6 Expectations for Document Storage

When discussing document storage in a system design interview, an L6 candidate should:

1. **Immediately clarify** that a Google Doc is NOT a file. It is a structured data model derived from an operation log. This shows you understand the fundamental architectural difference from file-based systems.

2. **Explain event sourcing** with both benefits AND costs. An L5 candidate says "store operations in a log." An L6 candidate explains WHY (audit trail, revision history, undo, debugging) and WHY NOT (unbounded growth, replay latency) and HOW TO MITIGATE THE COSTS (snapshots/compaction).

3. **Justify storage system choices** with access pattern reasoning. "Bigtable for the operation log because it is append-heavy and supports efficient range scans by row key" is L6. "Use a database" is L5.

4. **Walk through the loading flow** step by step, showing awareness of the gap between snapshot load and WebSocket connection (the catch-up step). This is a subtle but critical detail that demonstrates production experience.

5. **Know the document size limit** and explain it as a performance constraint of the OT model, not an arbitrary product decision.

### L7 and Beyond

An L7 candidate would additionally discuss:
- Compaction strategy tuning (time-based vs count-based vs hybrid, with cost modeling)
- Snapshot storage lifecycle management (hot/warm/cold tiering with cost per petabyte)
- Cold-start latency optimization for viral documents (pre-warming, edge caching)
- Operation log format versioning (how to evolve the operation format without breaking replay)
- Cost modeling at petabyte scale (operation log storage cost per year, snapshot storage cost, read/write IOPS cost)

---

*This is a companion document to the main interview simulation. For the full interview dialogue, see [01-interview-simulation.md](01-interview-simulation.md).*
*For the OT engine deep dive, see [03-operational-transformation.md](03-operational-transformation.md).*
*For cursor and presence, see [05-cursor-and-presence.md](05-cursor-and-presence.md).*
