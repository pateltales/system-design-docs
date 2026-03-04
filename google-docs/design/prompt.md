Design Google Docs (Real-Time Collaborative Document Editor) as a system design interview simulation.

## Template
Follow the EXACT same format as the S3 interview simulation at:
src/hld/aws/s3/design/interview-simulation.md

Same structure:
- Interviewer/Candidate dialogue format
- PHASE 1: Opening & Problem Statement
- PHASE 2: Requirements Gathering & Scoping (functional + non-functional + scale numbers)
- PHASE 3: API Design
- PHASE 4: High-Level Architecture (iterative build-up: Attempt 0 → 1 → 2 → ..., starting from single server, finding problems, evolving)
- PHASE 5+: Deep dives on each component
- L5/L6/L7 rubric table after EACH deep dive phase — showing exactly how an L5, L6, and L7 candidate would answer differently. This is critical for understanding what "good" looks like at each level.
- Architecture evolution table after each phase
- Wrap-up phase with "what keeps you up at night"
- Links to supporting deep-dive docs

## Candidate Level
**L6 SDE-3 (Senior SDE)** candidate being interviewed by a Principal Engineer (L8).

The candidate's answers should demonstrate L6-level depth:
- Not just "what" but "why" and "why not the alternative"
- Quantitative reasoning (back-of-envelope math, concrete numbers)
- Awareness of operational concerns (monitoring, failure modes, blast radius)
- Proactive identification of tradeoffs before the interviewer asks

The L5/L6/L7 rubric tables after each phase should make it crystal clear how an L5 answer differs from L6 differs from L7.

## Output location
Create all files under: src/hld/google-docs/design/

## Files to create

### 1. 01-interview-simulation.md — the main backbone
The full interview dialogue covering all phases and attempts.

### 2. 02-api-contracts.md — Collaborative Editor APIs

**API groups to cover**:

- **Document CRUD APIs**: `POST /documents` (create new document — blank or from template), `GET /documents/{docId}` (get document metadata: title, owner, last modified, sharing info, NOT the full content), `PATCH /documents/{docId}` (update metadata — rename, move to folder), `DELETE /documents/{docId}` (soft delete → trash). Document content is NOT served via REST — it's served via the real-time collaboration channel.

- **Real-Time Collaboration APIs (WebSocket/SSE)**: The core innovation. `WS /documents/{docId}/collaborate` — bidirectional WebSocket connection for real-time editing. Client sends operations (insert character at position X, delete range Y-Z, format bold range A-B). Server broadcasts transformed operations to all connected clients. This is the Operational Transformation (OT) / CRDT channel. Unlike REST, this is a persistent, stateful connection. Each client maintains a local copy and sends incremental operations — NOT the full document on every keystroke.

- **Document Content APIs (REST fallback)**: `GET /documents/{docId}/content` (get full document content as JSON/HTML — used for initial load before WebSocket connects, or for export). `POST /documents/{docId}/export` (export as PDF, DOCX, ODT, TXT, HTML). These are NOT the real-time editing path — they're for loading and exporting.

- **Revision History APIs**: `GET /documents/{docId}/revisions` (list all revision snapshots — Google Docs auto-saves named revisions periodically and on significant edits), `GET /documents/{docId}/revisions/{revId}` (get document content at a specific revision), `POST /documents/{docId}/revisions/{revId}/restore` (restore to a previous version). Revision is a full snapshot of the document at a point in time, NOT an operation log (though internally, revisions are reconstructed from the operation log).

- **Commenting APIs**: `POST /documents/{docId}/comments` (add a comment anchored to a text range), `GET /documents/{docId}/comments` (list comments — open, resolved, all), `POST /documents/{docId}/comments/{commentId}/reply` (reply to a comment thread), `PUT /documents/{docId}/comments/{commentId}/resolve` (mark as resolved). Comments are anchored to text ranges — when text is edited, comment anchors must move/adjust. This is a non-trivial problem (what happens to a comment on text that's deleted?).

- **Sharing & Permission APIs**: `POST /documents/{docId}/permissions` (share with specific users — viewer, commenter, editor), `PATCH /documents/{docId}/permissions/{permissionId}` (change access level), `DELETE /documents/{docId}/permissions/{permissionId}` (revoke access), `POST /documents/{docId}/link` (create shareable link — anyone with link can view/comment/edit). Permission model: Owner > Editor > Commenter > Viewer.

- **Cursor & Presence APIs**: `WS /documents/{docId}/presence` (part of the collaboration WebSocket — broadcast cursor position, selection range, user identity, and user color to all connected clients). This is what shows "Alice is editing here" with a colored cursor. Presence is ephemeral — not persisted. If a user disconnects, their cursor disappears.

- **Suggestion Mode APIs**: `POST /documents/{docId}/suggestions` (make an edit in "suggestion mode" — the edit is not applied but shown as a tracked change), `POST /documents/{docId}/suggestions/{suggestionId}/accept` (accept the suggestion — apply the edit), `POST /documents/{docId}/suggestions/{suggestionId}/reject` (reject — discard the edit). Suggestions are displayed differently (strikethrough for deletions, colored text for insertions) and require approval by an editor/owner.

**Contrast with Microsoft 365 / Word Online**:
- Microsoft uses a CRDT-like approach for co-authoring in Word Online (not classical OT like Google Docs). Both achieve real-time collaboration but with different consistency guarantees and conflict resolution strategies.
- Microsoft supports offline editing with conflict resolution on reconnect. Google Docs is primarily online-first (limited offline via Chrome extension/PWA).

**Contrast with Notion**:
- Notion uses block-based editing (each paragraph, heading, image is a "block" that can be independently edited and rearranged). Google Docs uses a linear document model. Block-based editing simplifies concurrent editing (different users editing different blocks rarely conflict) but limits layout flexibility.

**Contrast with Dropbox Paper**:
- Dropbox Paper is simpler — no suggestion mode, simpler formatting. Less complex OT/CRDT implementation.

**Interview subset**: In the interview (Phase 3), focus on: real-time collaboration (OT/CRDT operations over WebSocket), document content storage, permission model, and revision history.

### 3. 03-operational-transformation.md — Operational Transformation (OT) Deep Dive

This is THE core technical challenge. OT is what makes simultaneous editing possible without conflicts.

- **The problem**: Two users editing the same document simultaneously. Alice inserts "X" at position 5. Bob deletes character at position 3. If both operations are applied naively, the document diverges — Alice and Bob see different content. OT ensures both arrive at the same final state.
- **Operations**: An edit is represented as an "operation" — a sequence of components:
  - `retain(n)`: skip n characters (no change)
  - `insert(text)`: insert text at current position
  - `delete(n)`: delete n characters from current position
  - Example: Insert "hello" at position 5 in a 10-char document = `[retain(5), insert("hello"), retain(5)]`
- **Transform function**: `transform(op_A, op_B) → (op_A', op_B')` where:
  - Applying op_A then op_B' produces the same result as applying op_B then op_A'.
  - This is the **convergence property** — regardless of the order operations arrive, all clients converge to the same document state.
  - Example: Alice inserts "X" at position 5, Bob inserts "Y" at position 3.
    - If Alice applies her op first: document has "X" at pos 5. Now Bob's insert at pos 3 is still at pos 3 (before the X). Transform is trivial.
    - If Bob applies first: "Y" at pos 3. Alice's insert at pos 5 needs to shift to pos 6 (Y pushed everything after pos 3 right by 1). Transform shifts Alice's position.
- **Client-server model (Google Docs approach)**:
  - The server is the **single source of truth** (centralized OT).
  - Each client maintains: confirmed state (acknowledged by server), pending operations (sent but not ack'd), and buffer (unsent local edits).
  - Client sends operation to server → server transforms against concurrent operations → applies to canonical document → broadcasts transformed operation to all other clients → sends ACK to originating client.
  - **Server ordering**: The server imposes a total order on operations. All clients see operations in the same order. This is simpler than decentralized OT (no need for vector clocks or complex ordering).
- **Jupiter protocol**: Google Docs uses a variant of the Jupiter collaboration protocol (originally from Xerox PARC). Jupiter uses a 1D state space — each client and the server maintain a state vector, and operations are transformed along the path between states.
- **Undo in OT**: Undo is non-trivial. You can't just "reverse the last operation" because other users' operations may have interleaved. OT-aware undo must transform the inverse operation against all subsequent operations.
- **Complexity**: OT transform functions must handle N×N operation type combinations (insert×insert, insert×delete, delete×delete, format×insert, etc.). Each combination has its own transform logic. This combinatorial complexity makes OT implementations bug-prone.
- **Contrast with CRDTs (Conflict-free Replicated Data Types)**:
  - CRDTs guarantee convergence **without a central server** — operations commute mathematically. No transform function needed.
  - **RGA (Replicated Growable Array)**: A CRDT for text. Each character has a unique ID (siteId + logical clock). Insertions reference the ID of the character they're inserted after. Deletions mark characters as tombstones.
  - **Yjs, Automerge**: Open-source CRDT libraries for collaborative editing. Used by Figma (Yjs-inspired), VS Code Live Share, and others.
  - **Trade-offs**: OT is simpler for server-centric architectures (Google's model). CRDTs are better for peer-to-peer and offline-first (no central server needed). CRDTs have higher memory overhead (tombstones, unique character IDs). OT has higher server compute (transform on every operation).
  - Google chose OT because they have a reliable, low-latency centralized server. CRDTs make more sense for systems that must work offline or peer-to-peer.
- **Contrast with Figma**: Figma uses a CRDT-inspired approach for their multiplayer design tool. Each design element is a CRDT object. This works well for a design tool (objects are relatively independent) but would be more complex for a text editor (characters are highly interdependent).

### 4. 04-document-storage.md — Document Storage & State Management

How the document is stored, versioned, and loaded.

- **Document representation**: A Google Doc is NOT stored as a file (like .docx). It's stored as a **structured data model** — a tree of elements (paragraphs, headings, lists, tables, images) with formatting attributes. This enables collaborative editing at the element level.
  - Internally, the document is likely stored as a sequence of operations (the operation log) plus periodic snapshots. The current state = last snapshot + replay operations since snapshot.
- **Operation log (event sourcing)**: Every edit is stored as an operation in an append-only log. The complete document history is reconstructable by replaying the log from the beginning (or from a snapshot).
  - Log entry: `{operationId, userId, timestamp, operation, parentRevision}`
  - The log is the source of truth — the document "state" is derived from the log.
  - **Event sourcing benefits**: Full audit trail, revision history, undo, and the ability to reconstruct the document at any point in time.
  - **Event sourcing costs**: The log grows forever. Old operations must be compacted/snapshotted. Replaying a long log is slow.
- **Snapshots (compaction)**: Periodically (e.g., every 100 operations or every N minutes), the server takes a snapshot of the full document state. Older operations before the snapshot can be archived. Loading a document = load latest snapshot + replay operations since snapshot.
- **Storage layer**: Google likely uses a combination of:
  - **Bigtable / Spanner** for document metadata and the operation log (strongly consistent, globally replicated).
  - **GCS (Google Cloud Storage)** for document snapshots and exported files.
  - **Colossus** (Google's distributed file system, successor to GFS) as the underlying storage layer.
- **Document size limits**: Google Docs has a limit of ~1.02 million characters per document. This is likely a performance limit — OT complexity grows with document size (operation positions reference character indices).
- **Loading a document**: When a user opens a document:
  1. Load latest snapshot from storage.
  2. Replay all operations since the snapshot to get current state.
  3. Establish WebSocket connection for real-time collaboration.
  4. Receive any pending operations from other active editors.
  5. Render the document locally.
  - If the document has many uncompacted operations, loading is slow → trigger compaction.
- **Contrast with Dropbox/OneDrive**: Dropbox stores documents as files (binary blobs). Editing is whole-file sync (upload full file on save). No operation log, no real-time collaboration (conflicted copies instead). Google Docs stores documents as structured data with operation logs — fundamentally different storage model.
- **Contrast with Notion**: Notion uses a block-based model — each block is stored independently. Editing a paragraph only affects that block's storage. This is more modular but less flexible for complex document layouts.

### 5. 05-cursor-and-presence.md — Cursor Synchronization & Presence

Showing other users' cursors and selections in real-time — the visual indicator of collaboration.

- **Cursor state**: Each active user has: cursor position (character index), selection range (start, end), user identity (name, avatar), and assigned color (consistent per user across sessions).
- **Broadcast mechanism**: Cursor updates are sent via the same WebSocket channel as document operations. Each cursor update: `{userId, cursorPosition, selectionStart, selectionEnd}`. Broadcast to all other connected clients.
- **Throttling**: Users move their cursor constantly (mouse, arrow keys, clicking). Broadcasting every cursor movement would flood the WebSocket. Solution: throttle cursor updates to ~10-20 updates per second per user. Interpolate between updates on the client for smooth cursor movement.
- **Cursor position stability**: When another user edits the document, cursor positions of all users must be adjusted. If Alice's cursor is at position 10 and Bob inserts 5 characters at position 3, Alice's cursor should shift to position 15. This is handled by transforming cursor positions using the same OT transform functions used for operations.
- **Presence (who's viewing)**: The document header shows avatars of all users currently viewing/editing. Presence is ephemeral — maintained via WebSocket heartbeat. If a user's WebSocket disconnects (close tab, lose network), their presence is removed after a timeout (e.g., 30 seconds).
- **User colors**: Each user is assigned a distinct color for their cursor and name label. Colors are assigned from a fixed palette to ensure visibility. Ideally, colors persist across sessions (same user = same color, at least within a short time window).
- **Scalability**: With many simultaneous editors (e.g., 100 users in a document), cursor updates become N² (each of 100 users sends updates to 99 others = 9,900 cursor updates/sec at 1 update/sec/user). Solutions:
  - Only show cursors of users editing nearby text (viewport-based culling).
  - Further throttle cursor updates for users outside the current viewport.
  - Limit simultaneous editors (Google Docs caps at 100 simultaneous editors).
- **Contrast with Figma**: Figma shows cursor positions on a 2D canvas (x, y coordinates). This is simpler than text cursors (no need to track character positions that shift with edits). But Figma also shows viewport rectangles (what each user is looking at), which Google Docs doesn't.

### 6. 06-conflict-resolution-and-consistency.md — Conflict Resolution & Consistency

How the system ensures all users see the same document, even with concurrent edits.

- **Centralized OT convergence**: Google Docs uses a server-authoritative model. The server is the single source of truth. All operations pass through the server, which imposes a total order. Convergence is guaranteed because all clients apply the same operations in the same order.
- **Client state machine**: Each client maintains three states:
  - **Synchronized**: No pending operations. Client and server are in sync.
  - **Awaiting ACK**: Client sent an operation, waiting for server acknowledgment. New local edits are buffered.
  - **Awaiting ACK + Buffered**: Client sent an operation AND has new local edits buffered. When the ACK arrives, the buffer is transformed and sent as the next operation.
- **Server-side transform**: When the server receives operation A from client X while it has already applied operation B from client Y:
  1. Transform A against B: `A' = transform(A, B)`
  2. Apply A' to the server's document state.
  3. Broadcast A' to all clients except X.
  4. Send ACK to X (with the transformed operation, so X knows how its operation was applied).
- **Consistency guarantees**:
  - **Convergence**: All clients will eventually see the same document content (given no new edits).
  - **Intention preservation**: Each operation's intended effect is preserved (Alice intended to bold characters 5-10; even if other edits shift positions, the same text is bolded).
  - **Causality**: If operation A happened before operation B (A caused B), all clients see A before B.
- **Network partitions / disconnection**: If a client disconnects, they can continue editing locally (offline mode). On reconnection, all local operations are sent to the server. The server transforms them against all operations that happened while the client was offline. This can be a significant amount of transformation.
- **Conflict examples**:
  - **Insert-insert at same position**: Alice inserts "X" at pos 5, Bob inserts "Y" at pos 5. OT resolves by tiebreaking on userId (lower userId goes first). Result: "XY" or "YX" consistently across all clients.
  - **Delete-delete same range**: Both Alice and Bob delete the same word. OT detects the overlap and the second delete becomes a no-op. No double deletion.
  - **Format conflict**: Alice bolds text, Bob italicizes the same text. No conflict — both formatting attributes are applied. Result: bold italic.
- **Contrast with Dropbox**: Dropbox detects conflicts but doesn't resolve them — creates "conflicted copy" files. Google Docs resolves conflicts automatically in real-time using OT. Fundamentally different approaches because fundamentally different products (file sync vs document editing).
- **Contrast with Git**: Git uses three-way merge with conflict markers. Manual resolution required. Designed for asynchronous development workflows, not real-time collaboration.

### 7. 07-offline-support.md — Offline Editing & Sync

How Google Docs works without internet connectivity.

- **Offline mode**: Google Docs supports offline editing via a Chrome extension / PWA (Progressive Web App). The document is cached locally (IndexedDB in the browser). User can edit, and changes are queued as operations in a local log.
- **Reconnection sync**: When the user comes back online:
  1. Establish WebSocket connection.
  2. Send all queued offline operations to the server.
  3. Server transforms offline operations against all operations from other users that happened during the offline period.
  4. Server sends transformed operations back to the client.
  5. Client applies server-side changes to the local document.
  6. Client and server converge to the same state.
- **Offline challenges**:
  - Long offline periods → many operations to transform → slow reconnection.
  - If multiple users edited the same region of the document offline, the OT transformations can be complex and the result may be surprising (both users' edits are preserved but interleaved).
  - Offline editing is inherently limited — no real-time collaboration, no comments from others, no presence indicators.
- **Contrast with Notion**: Notion supports robust offline editing. Each block is independently editable, so offline conflicts are less likely (different users editing different blocks).
- **Contrast with Apple iWork**: iWork (Pages, Numbers, Keynote) supports offline editing with iCloud sync. Uses a last-writer-wins or conflict detection approach similar to Dropbox for non-collaborative edits.

### 8. 08-permission-and-sharing.md — Permission Model & Access Control

How sharing and permissions work for collaborative documents.

- **Permission levels**: Owner > Editor > Commenter > Viewer. Each level is a superset of the next:
  - Owner: full control (share, delete, transfer ownership)
  - Editor: edit content, resolve comments, change formatting
  - Commenter: add comments and suggestions, but not edit directly
  - Viewer: read-only access
- **Sharing mechanisms**:
  - **Direct sharing**: Share with specific email addresses. Granular control per user.
  - **Link sharing**: Generate a shareable link. Anyone with the link can view/comment/edit (configurable). Can restrict to "anyone in organization" for Google Workspace.
  - **Domain-level sharing**: Available in Google Workspace — share with "anyone in company.com."
- **Permission checking**: On every WebSocket connection and every operation, the server checks the user's permission level. An attempt to edit by a viewer is rejected. This check must be fast (cached ACL).
- **Real-time permission changes**: If a document owner downgrades Alice from editor to viewer while Alice is actively editing, Alice's WebSocket connection must be notified. Her client switches to read-only mode. Pending unsent operations are discarded.
- **Contrast with Dropbox**: Dropbox has simpler permissions (viewer/editor per shared folder, not per file). No commenter role. No link sharing with configurable access levels.
- **Contrast with Microsoft 365**: Similar permission model (viewer, editor, owner). Microsoft adds "co-author" as a distinct state when multiple editors are active.

### 9. 09-rich-text-and-formatting.md — Rich Text Model & Formatting

How formatting (bold, italic, headings, tables, images) is handled in the OT/CRDT model.

- **Document model**: The document is a tree of elements:
  - Root → Sections → Paragraphs → Text Runs (with formatting attributes)
  - Each Text Run has: text content + formatting attributes (bold, italic, font, size, color, link)
  - Block elements: paragraphs, headings, lists, tables, images, page breaks
  - Inline elements: text runs, inline images, links, mentions
- **Formatting operations**: Formatting is applied as operations on ranges:
  - `applyFormat(startIndex, endIndex, {bold: true})` — bold a range of text
  - These operations must be OT-transformed like insertion/deletion operations. If Alice bolds characters 5-10 and Bob inserts 3 characters at position 7 (within the bold range), the bold range must expand to 5-13.
- **Rich text OT complexity**: Adding formatting to OT significantly increases the number of operation type combinations:
  - insert × format, delete × format, format × format
  - Nested formatting (bold + italic + link on the same text)
  - Table operations (insert row, merge cells) × text operations
  - Each combination needs correct transform logic.
- **Contrast with Markdown editors (Notion, HackMD)**: Markdown editors store text with markup syntax. Collaborative editing on markdown is simpler — it's just text, so standard text OT applies. But rendering is less WYSIWYG.
- **Contrast with HTML-based editors (CKEditor, TinyMCE)**: HTML-based editors store content as HTML. OT on HTML trees (DOM operations) is more complex than OT on a linear document model. Google Docs likely uses a custom intermediate representation, not raw HTML.

### 10. 10-scaling-and-reliability.md — Scaling & Reliability

Cross-cutting doc that ties together scaling decisions from all deep dives.

- **Scale numbers (Google Docs-scale)**:
  - **Google Workspace has 3+ billion users** (including free Gmail users who can use Docs).
  - **Hundreds of millions of active documents** at any time.
  - **Peak concurrent editors per document**: up to 100 simultaneous editors (Google Docs limit).
  - **Operations per second**: millions globally across all active documents.
  - **Document storage**: petabytes of operation logs and document snapshots.
- **WebSocket scaling**: Each active document editing session maintains a WebSocket connection. With millions of concurrent editors, this is millions of persistent connections. Google uses custom infrastructure for connection management.
- **OT server scaling**: The OT server for a document must be single-threaded (total ordering of operations). This creates a serialization bottleneck per document. Solutions:
  - Each document's OT state is on a single server (stateful routing).
  - If the document's OT server fails, another server loads the latest state and takes over (state migration).
  - Documents with no active editors have no OT server — resources are allocated on demand.
- **Global latency**: Google operates globally. A user in Tokyo editing a document whose OT server is in Virginia sees ~150ms round-trip latency. Google mitigates this with:
  - Local speculation: client applies operations locally immediately (optimistic UI).
  - Regional OT servers: Google may route the OT server to the region with the most active editors.
- **Reliability**: Document data must not be lost. Multi-region replication of the operation log (Spanner/Bigtable). Even if a data center fails, the operation log is preserved.
- **Monitoring**: Operations per second per document, OT transform latency, WebSocket connection count, operation log growth, snapshot frequency, permission check latency, cursor broadcast rate.

### 11. 11-design-trade-offs.md — Design Philosophy & Trade-off Analysis

- **OT vs CRDT**: Google chose OT (centralized, server-authoritative) over CRDTs (decentralized, peer-to-peer). OT is simpler with a reliable server. CRDTs are better for offline-first / peer-to-peer. Trade-off: OT requires a live server connection; CRDTs work without one.
- **Centralized vs decentralized collaboration**: Google Docs uses a central server for operation ordering. This simplifies consistency but creates a dependency on the server. CRDTs (used by Figma, Yjs) eliminate this dependency at the cost of higher complexity and memory overhead.
- **Event sourcing (operation log) vs state snapshots**: Google Docs stores the operation log (event sourcing) rather than periodically saving the full document state. This enables rich revision history and undo but creates storage and replay costs. Periodic snapshots are compaction events that bound the replay cost.
- **WebSocket vs HTTP polling for real-time collaboration**: WebSocket provides sub-100ms bidirectional communication. HTTP polling would add seconds of latency — unacceptable for a real-time editor. For Google Docs, WebSocket is the right choice (unlike Dropbox which uses long-polling because file sync tolerates seconds of delay).
- **Rich text model vs Markdown vs HTML**: Google Docs uses a custom rich text model (not raw HTML, not Markdown). This gives full control over OT complexity and rendering. Markdown would be simpler for OT but less WYSIWYG. HTML would be familiar but OT on DOM trees is very complex.
- **100-user limit vs unlimited collaborators**: Google Docs limits simultaneous editors to 100. This bounds the O(N²) cursor broadcast and reduces OT server load. For larger audiences, Google Docs supports "viewer" mode for additional users beyond the limit.
- **Online-first vs offline-first**: Google Docs is primarily online-first (real-time OT requires a server). Offline support exists but is limited (Chrome extension, queue operations, sync on reconnect). Contrast with Notion which has more robust offline support because its block-based model reduces conflicts.

## CRITICAL: The design must be Google Docs-centric
Google Docs is the reference implementation. The design should reflect how Google Docs actually works — Operational Transformation, centralized server model, WebSocket-based real-time collaboration, event-sourced operation log, Google's infrastructure (Spanner, Bigtable, Colossus). Where other collaborative editors (Microsoft 365, Notion, Figma, Dropbox) made different choices, call those out as contrasts.

## CRITICAL: Iterative build-up (DO NOT skip this)
The architecture MUST evolve iteratively:

### Attempt 0: Single server, save full document on every edit
- Web server stores documents as files. Client loads full document, edits locally, sends full document back to save. No collaboration — last save wins.
- **Problems found**: No concurrent editing. Full document transfer on every save (wasteful for a 1-character edit). Last-writer-wins = data loss. No version history.

### Attempt 1: Operation-based editing + operation log
- Instead of sending the full document, client sends **operations** (insert char at pos X, delete range Y-Z). Server applies operations to the document and stores them in an **operation log** (event sourcing).
- Revision history is free — replay the log to any point. Undo = apply inverse operation.
- **Problems found**: No concurrent editing — operations are serial (one user at a time). No real-time collaboration. No cursor sharing.

### Attempt 2: Real-time collaboration with Operational Transformation
- **WebSocket connection** for bidirectional communication. Client sends operations, server broadcasts to all other clients.
- **OT engine**: Server transforms concurrent operations to ensure convergence. All clients apply operations in the same order. Total ordering by the server.
- **Cursor presence**: Broadcast cursor positions via the same WebSocket channel.
- **Contrast with Dropbox**: Dropbox would create "conflicted copies." Google Docs resolves conflicts automatically via OT. Different product, different approach.
- **Problems found**: Single server = single point of failure. No offline support. Full operation log grows forever (slow document loads). No permissions (anyone with the link can edit).

### Attempt 3: Snapshots + permissions + offline support
- **Periodic snapshots**: Every 100 operations, snapshot the full document state. Load = latest snapshot + replay recent operations. Old operations can be archived.
- **Permission model**: Viewer / Commenter / Editor / Owner. Check on WebSocket connect and on every operation.
- **Offline editing**: Cache document locally, queue operations, sync on reconnect (transform against missed operations).
- **Problems found**: Single-region deployment. Global latency for distant users. OT server is a per-document bottleneck. No rich text model (just plain text so far).

### Attempt 4: Production hardening (global infra, rich text, scaling)
- **Global infrastructure**: Multi-region OT servers. Route each document's OT to the region with most active editors. Replicate operation log via Spanner (globally consistent).
- **Rich text OT**: Extend OT to handle formatting operations (bold, italic, headings, tables, images). Formatting operations are OT-transformed alongside text operations.
- **WebSocket at scale**: Millions of concurrent connections. Stateful routing (each document's editor connections go to the same OT server). Failover: if OT server fails, another server loads latest state from operation log.
- **Comment system**: Comments anchored to text ranges. Range anchors adjusted when text is edited (via OT). Comment threads, @mentions, resolution.
- **Suggestion mode**: Track proposed changes. Display as tracked changes. Accept/reject workflow.
- **Monitoring**: OT latency, operation throughput, WebSocket connection count, conflict rate, document load time, snapshot frequency.

## CRITICAL: Accuracy requirements

I DO NOT TRUST AI-GENERATED FACTS ABOUT SPECIFIC SYSTEMS. Every concrete claim about Google Docs internals must be verifiable against official sources. Specifically:

1. **Use WebSearch and WebFetch tools** to look up Google Workspace Blog, Google Research papers, and relevant documentation BEFORE writing. Search for:
   - "Google Docs operational transformation architecture"
   - "Google Wave OT protocol"
   - "Jupiter collaboration protocol"
   - "Google Docs CRDT vs OT"
   - "Google Docs real-time collaboration technical"
   - "Google Workspace scale numbers users"
   - "Operational Transformation algorithm explained"
   - "CRDT vs OT comparison collaborative editing"
   - "Figma CRDT multiplayer"
   - "Yjs CRDT text editing"
   - "Google Spanner document storage"

   **BLANKET PERMISSION**: You have full permission to use WebSearch and WebFetch. Do NOT ask the user for permission.

2. **For every concrete number**, verify against official sources. If unverifiable, mark as "[UNVERIFIED]".

3. **For every claim about Google internals**, if not from an official source, mark as "[INFERRED]".

4. **CRITICAL: Do NOT confuse Google Docs with Dropbox, Notion, or Figma.** Each has a different collaboration model:
   - Google Docs: OT, centralized server, real-time, text/document focus
   - Dropbox: file sync, conflicted copies, no real-time co-editing
   - Notion: block-based, CRDT-inspired, real-time but simpler model
   - Figma: CRDT, 2D canvas, multiplayer design tool

## What NOT to do
- Do NOT treat this as "just a text editor" — the real-time collaboration (OT/CRDT), conflict resolution, and consistency guarantees are the interesting parts.
- Do NOT skip OT — it is THE defining technical challenge.
- Do NOT confuse OT with CRDT — explain both, contrast, and explain why Google chose OT.
- Do NOT jump to the final architecture. Build it step by step.
- Do NOT make up implementation details — verify or mark as inferred.
- Do NOT ask the user for permission to read online documentation.
- Do NOT write a Wikipedia article. This is an INTERVIEW SIMULATION with dialogue.
