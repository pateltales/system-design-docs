Design Dropbox / Google Drive (Cloud File Storage & Sync) as a system design interview simulation.

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
Create all files under: src/hld/dropbox/design/

## Files to create

### 1. 01-interview-simulation.md — the main backbone
The full interview dialogue covering all phases and attempts.

### 2. 02-api-contracts.md — Dropbox/Google Drive Platform APIs

This doc should list all the major API surfaces of a Dropbox-like cloud file storage & sync platform. The interview simulation (Phase 3) will only cover a subset — this doc is the comprehensive reference.

**Structure**: For each API group, list every endpoint with HTTP method, path, request/response shape, and a brief description. Mark endpoints covered in the interview with a star or highlight.

**API groups to cover**:

- **File Upload APIs**: The most critical path. `POST /files/upload` (simple upload for small files < 150 MB), `POST /files/upload_session/start` (chunked/resumable upload — start a session), `POST /files/upload_session/append` (upload a chunk), `POST /files/upload_session/finish` (commit all chunks into a file). Chunked upload is essential for large files on unreliable networks. Each chunk is ~4 MB. Client computes SHA-256 hash per block for deduplication — server skips blocks it already has. This is Dropbox's core innovation: block-level dedup means uploading a 1 GB file that shares 99% content with an existing file only uploads the changed blocks (~10 MB instead of 1 GB).

- **File Download APIs**: `POST /files/download` (download a file — returns file content + metadata), `POST /files/download_zip` (download a folder as zip), `POST /files/get_temporary_link` (short-lived direct download URL for CDN delivery). Downloads go through edge CDN for performance. Large files are served in chunks with HTTP range requests for resumability.

- **File Operations APIs**: `POST /files/delete` (soft delete → trash), `POST /files/permanently_delete`, `POST /files/move` (move/rename — this is a metadata-only operation, no block copy), `POST /files/copy` (copy — also metadata-only if using block references, copy-on-write), `POST /files/create_folder`, `POST /files/list_folder` (paginated directory listing), `POST /files/list_folder/continue` (cursor-based pagination for large folders), `POST /files/search` (full-text search across file names and optionally content). Move and copy being metadata-only operations (not physical block copies) is a key architectural insight — blocks are content-addressed and reference-counted.

- **Sync APIs**: The defining feature that separates Dropbox from plain cloud storage. `POST /files/list_folder/longpoll` (long-poll: client asks "has anything changed since cursor X?" — server blocks until a change occurs or timeout, returns immediately on change). `POST /files/list_folder` + `POST /files/list_folder/continue` (cursor-based change enumeration — client maintains a cursor, fetches all changes since that cursor). The sync protocol is: (1) client calls longpoll with cursor → (2) server responds when changes exist → (3) client calls list_folder/continue to get the actual changes → (4) client applies changes locally → (5) client updates cursor → repeat. This is Dropbox's notification channel — NOT WebSocket (Dropbox chose long-polling over WebSocket for simplicity and firewall compatibility).

- **File Versioning APIs**: `POST /files/list_revisions` (list all versions of a file), `POST /files/restore` (restore a previous version), `POST /files/get_metadata` (get current metadata including content_hash — Dropbox's block-level hash). Every edit creates a new version. Versions are retained for 30 days (free) or 180 days (business). Restore is a metadata operation — point the file's block list to the old version's blocks.

- **Sharing APIs**: `POST /sharing/create_shared_link_with_settings` (create a shareable link — public, team-only, password-protected, with expiry), `POST /sharing/share_folder` (share a folder with specific users/groups — sets ACLs), `POST /sharing/add_folder_member` (add collaborator to shared folder), `POST /sharing/list_folder_members` (list who has access), `POST /sharing/update_folder_member` (change access level — viewer/editor/owner), `POST /sharing/remove_folder_member`. Sharing creates a **shared namespace** — the folder appears in each member's file tree as if it were their own. Changes sync to all members in real-time.

- **Team/Admin APIs**: `POST /team/members/add` (add team member), `POST /team/members/remove`, `GET /team/get_info` (team storage usage, member count), `POST /team/member_space_limits/set` (per-user storage quota), `POST /team/reports/get_storage` (admin reporting). Enterprise features: data loss prevention (DLP), audit logging, device approval, remote wipe.

- **Webhook/Notification APIs**: `POST /webhook/register` (register a webhook URL — server calls it when files change in the user's account), `GET /webhook/verify` (webhook verification challenge). Webhooks provide push-based notifications for server-to-server integrations (unlike longpoll which is for the Dropbox client itself).

- **Content Hash API** (internal): Dropbox uses a specific content hashing scheme: split file into 4 MB blocks → SHA-256 each block → SHA-256 the concatenation of all block hashes. This "content_hash" is exposed in metadata and used for: dedup (identical blocks across different files are stored once), integrity verification (client computes hash, server verifies), and sync (detect which blocks changed).

**Contrast with Google Drive API**:
- Google Drive is **document-centric** (native Google Docs/Sheets/Slides are first-class, stored as structured data, not files). Dropbox is **file-centric** (everything is a file/blob).
- Google Drive supports **real-time collaborative editing** via Operational Transforms / CRDTs (Google Docs). Dropbox does not — concurrent edits create "conflicted copies."
- Google Drive's sync is **less granular** — it syncs whole files, not block-level deltas. Dropbox's block-level delta sync is its core technical advantage.
- Google Drive has **richer permission model** (viewer, commenter, editor, owner + domain-level sharing). Dropbox's sharing is simpler (viewer/editor + shared links).

**Contrast with S3 API**:
- S3 is an **object store** — flat namespace, PUT/GET/DELETE, no sync, no versioning UI, no sharing, no conflict resolution. S3 is infrastructure; Dropbox is a product built on top of infrastructure like S3 (originally) or custom storage (Magic Pocket).
- S3's multipart upload is similar to Dropbox's chunked upload in mechanics but lacks block-level dedup.
- S3 has no change notification channel (no longpoll, no sync cursor). S3 Event Notifications via SNS/SQS are eventual and coarse-grained.

**Interview subset**: In the interview (Phase 3), focus on: chunked upload with dedup (the core innovation), sync protocol (longpoll + cursor), file operations (move/copy as metadata-only), and sharing (shared namespaces). The full API list lives in this doc.

### 3. 03-file-sync-engine.md — File Sync Engine (The Defining Challenge)

The sync engine is what makes Dropbox "Dropbox" — without it, you just have cloud storage (like S3). This doc should cover:

- **Client-side sync engine architecture**: The Dropbox desktop client watches the local Dropbox folder for changes (via OS file system events: `inotify` on Linux, `FSEvents` on macOS, `ReadDirectoryChangesW` on Windows). When a file changes: (1) detect the change → (2) compute block hashes → (3) compare with server's block list → (4) upload only changed blocks → (5) update metadata on server → (6) server notifies other clients → (7) other clients download changed blocks → (8) reconstruct file locally.
- **Delta sync (block-level)**: The key innovation. Files are split into 4 MB blocks. Each block is SHA-256 hashed. On edit, only changed blocks are uploaded. Example: a 100 MB file with 25 blocks. User edits a section in block 7. Only block 7 (~4 MB) is uploaded, not the entire 100 MB. This saves **96% bandwidth** for a typical edit.
  - **Content-defined chunking (CDC)**: Fixed-size chunking (4 MB blocks) breaks down when content is inserted at the beginning (shifts all block boundaries). CDC uses a rolling hash (Rabin fingerprint) to define block boundaries based on content, not position. This means inserting data at the beginning only affects 1-2 blocks, not all blocks. Dropbox uses a hybrid approach.
  - **Contrast with Google Drive**: Google Drive syncs whole files for non-Google-Docs files. A 1-byte change to a 100 MB file re-uploads 100 MB. Dropbox re-uploads ~4 MB. This is Dropbox's core technical moat.
  - **Contrast with rsync**: rsync uses a similar rolling-hash approach but works at the byte level (more granular). Dropbox operates at the block level (4 MB) for a better balance of dedup efficiency and metadata overhead.
- **Sync protocol (client-server)**:
  1. **Client → Server (upload path)**: File changes detected → compute block hashes → `POST /files/upload_session/start` → upload new blocks → `POST /files/upload_session/finish` → server updates metadata + block references → server increments change cursor.
  2. **Server → Client (download path)**: Client calls `POST /files/list_folder/longpoll` with current cursor → server blocks until changes exist → returns "changes available" → client calls `POST /files/list_folder/continue` → gets list of changed files + new metadata → for each changed file: compare block hashes, download only changed blocks → reconstruct file locally → update local cursor.
  3. **Long-polling vs WebSocket**: Dropbox chose long-polling over WebSocket. Why? (a) Simpler — no persistent connection state to manage. (b) Firewall-friendly — long-poll is just an HTTP request with a long timeout. Many corporate firewalls block WebSocket but allow HTTP. (c) Sufficient latency — a few seconds of delay for sync is acceptable (unlike chat where sub-100ms matters). (d) Stateless — load balancer can route each poll to any server. Contrast with WhatsApp where sub-100ms latency requires WebSocket.
- **Sync state machine**: Each file/folder has a sync state: `UP_TO_DATE` → `SYNCING` (upload or download in progress) → `UP_TO_DATE` (sync complete) → `ERROR` (conflict, permission denied, storage full) → `CONFLICTED` (concurrent edit detected). The desktop client's tray icon (green checkmark / blue sync arrows / red X) reflects the aggregate sync state.
- **Conflict detection**: When client A and client B both edit the same file offline, both upload changes. The server detects the conflict (client B's upload has an outdated parent version). Resolution: server keeps client A's version as the "winner" (first to sync) and creates a "conflicted copy" file for client B's version (e.g., `report (John's conflicted copy 2026-02-20).docx`). User must manually merge.
- **Contrast with Google Drive**: Google Docs uses **Operational Transforms (OT)** for real-time collaborative editing — multiple cursors, character-level conflict resolution, no "conflicted copies." But this only works for Google's native document formats. For arbitrary files (.psd, .zip, .exe), Google Drive falls back to last-writer-wins (no conflict detection), which is worse than Dropbox's conflicted copy approach.
- **Contrast with Git**: Git is a full DVCS with three-way merge, branches, and manual conflict resolution. Dropbox is simpler — no branches, no merge, just "keep both versions." Git is for developers; Dropbox is for everyone.

### 4. 04-chunked-upload-and-dedup.md — Chunked Upload & Block-Level Deduplication

The upload pipeline is the most compute-intensive client-side operation and the foundation of Dropbox's storage efficiency.

- **Chunking strategy**: Split file into blocks. Two approaches:
  - **Fixed-size chunking**: Divide file into fixed 4 MB blocks. Simple, predictable. Problem: if data is inserted at the beginning, all block boundaries shift → every block hash changes → no dedup benefit. Dropbox uses 4 MB fixed blocks as the default.
  - **Content-defined chunking (CDC)**: Use a rolling hash (Rabin fingerprint) to define block boundaries based on content patterns. When the rolling hash hits a specific value (e.g., lower 13 bits are zero → average block size ~8 KB), mark a boundary. Insertion-resilient: inserting data at the beginning only shifts one boundary, not all. Trade-off: variable block sizes, more complex metadata. Used by tools like restic, borgbackup.
  - Dropbox reportedly uses a hybrid: fixed 4 MB blocks for storage, with sub-block rolling hashes for delta detection within blocks.
- **Block-level deduplication**:
  - Client computes SHA-256 hash of each block.
  - Before uploading a block, client asks server: "Do you already have block with hash X?" (`has_blocks` check).
  - If server has it → skip upload (just reference the existing block). If not → upload the block.
  - **Dedup ratio**: Across all Dropbox users, dedup ratio is significant. Example: 1000 users share the same PDF attachment → stored once, referenced 1000 times. Internal estimates suggest Dropbox achieves ~50-60% dedup across its user base.
  - **Content-addressable storage (CAS)**: Blocks are stored by their hash, not by file path. Two different files with identical content share the same blocks. This is the same principle as Git's object store.
- **Resumable uploads**: Each upload session has a session ID. Client tracks which chunks were successfully uploaded. On failure (network drop, app crash), client resumes from the last successful chunk. No re-upload of completed chunks. Essential for large files on unreliable mobile networks.
- **Compression**: Blocks are compressed (zlib/lz4) before upload to reduce bandwidth. Server stores compressed blocks. Decompressed on download. Compression happens after chunking but before encryption (if applicable).
- **Upload pipeline sequence**: Detect file change → split into blocks → hash each block → check which blocks server already has → compress new blocks → upload new blocks → commit metadata (file path → ordered list of block hashes) → update sync cursor → notify other clients.
- **Dropbox's content_hash**: The file-level hash is: SHA-256 each 4 MB block → concatenate all block hashes → SHA-256 the concatenation. This gives a deterministic, dedup-friendly hash that can be computed incrementally (you don't need the whole file in memory).
- **Contrast with S3 multipart upload**: S3 multipart upload splits files into parts (5 MB - 5 GB each), uploads parts in parallel, then completes the upload. But S3 does NOT deduplicate parts across uploads — each upload stores its own parts independently. S3's multipart is about reliability and parallelism, not dedup.
- **Contrast with Git objects**: Git also uses content-addressable storage (SHA-1 hashes, migrating to SHA-256). Git packs objects with delta compression. The principle is identical — Dropbox and Git both store content by hash and deduplicate identical content.

### 5. 05-metadata-service.md — Metadata Service & Namespace Management

The metadata service is the "brain" of Dropbox — it knows where every file is, who owns it, what version it's at, and what blocks compose it.

- **File metadata model**: Each file entry contains:
  - `fileId`: Globally unique identifier (not path-based — files can be moved without changing ID).
  - `namespaceId`: Which namespace (user's root, or shared folder) the file belongs to.
  - `path`: Full path within the namespace (e.g., `/Documents/report.pdf`).
  - `fileName`: Just the name (e.g., `report.pdf`).
  - `size`: File size in bytes.
  - `contentHash`: Dropbox content hash (block-level hash).
  - `blockList`: Ordered list of block hashes that compose this file (the critical link between metadata and block storage).
  - `rev`: Revision ID — opaque string that changes on every edit. Used for optimistic concurrency (upload fails if `rev` doesn't match server's current `rev`).
  - `serverModified`: Timestamp when the server received the latest version.
  - `clientModified`: Timestamp from the client's local clock (can be inaccurate — never trust client timestamps for ordering).
  - `isDeleted`: Soft delete flag (file is in trash, can be restored).
  - `sharingInfo`: Sharing metadata (shared folder ID, permissions, shared link info).
- **Namespace management**: Dropbox organizes files into **namespaces**. Each user has a root namespace (their Dropbox folder). Shared folders are separate namespaces that appear in multiple users' file trees. A namespace is the unit of: permission checking, sync cursor tracking, and change journal scoping.
  - **Why namespaces?** A shared folder between Alice and Bob must appear in both `/Alice/Dropbox/SharedProject/` and `/Bob/Dropbox/SharedProject/`. Without namespaces, you'd need to store the file twice (in each user's tree). With namespaces, the shared folder is a single namespace mounted into both trees. Changes in the namespace are visible to both.
- **Versioning**: Every edit creates a new revision. The metadata service stores the current revision and can retrieve past revisions. Versions are retained for 30 days (free) or 180 days (business/professional). Restore is a metadata-only operation: point the file's `blockList` to the old revision's block list. Old blocks are never deleted while any revision references them (reference counting).
- **Move/rename semantics**: Moving a file (e.g., `/A/file.txt` → `/B/file.txt`) is a metadata-only operation — update the `path` field. No blocks are copied. This is O(1) regardless of file size. Same for rename. This is only possible because files are identified by `fileId`, not by path.
- **Copy semantics**: Copying a file creates a new metadata entry pointing to the same `blockList`. Copy-on-write: the blocks are shared until the copy is edited (at which point the edited blocks diverge). A 10 GB file copy is instantaneous and uses zero additional storage until one copy is modified.
- **Metadata database**: Dropbox uses **MySQL** for metadata (sharded by namespace ID). Why MySQL over NoSQL?
  - Strong consistency required — metadata operations (file create, move, delete, version increment) must be ACID. Two clients concurrently creating `/folder/file.txt` must not both succeed.
  - Relational queries — list folder contents (`SELECT * FROM files WHERE namespace_id = ? AND parent_path = ?`), search by name, join with sharing info.
  - MySQL is well-understood, battle-tested, and Dropbox's engineering team has deep MySQL expertise.
  - Sharding by namespace ID ensures all files in a namespace (including shared folders) are on the same shard — essential for atomic operations within a folder.
  - **Contrast with Cassandra (WhatsApp's choice)**: WhatsApp chose Cassandra (AP, eventually consistent) because chat messages tolerate brief staleness. Dropbox needs strong consistency for metadata — two users must never see conflicting directory listings for the same shared folder.
- **Consistency model**: Metadata operations are **strongly consistent** (read-after-write). After client A uploads a file, client B's next sync must see it. This is achieved via MySQL's ACID guarantees within a shard, and cross-shard coordination for operations spanning namespaces.
  - Block storage is **eventually consistent** — a newly uploaded block may take a few seconds to replicate across data centers. This is acceptable because metadata (which is strongly consistent) gates access to blocks — you can't reference a block until the metadata commit succeeds.
- **Contrast with S3**: S3 has a flat namespace (bucket/key, no real folders). Dropbox has a hierarchical namespace (true folders with listing, nesting, permissions). S3 "folders" are just key prefixes — listing a "folder" in S3 is a prefix scan, not a directory lookup. Dropbox's metadata service makes directory operations (list, move, delete folder) efficient.

### 6. 06-block-storage.md — Block Storage (Magic Pocket)

Dropbox stores exabytes of data. The block storage layer is the largest and most expensive component.

- **Content-addressable storage (CAS)**: Blocks are keyed by their SHA-256 hash. To store a block: compute hash → check if hash exists → if not, store the block with key = hash. To retrieve: provide hash → get block content. Identical content is stored exactly once, regardless of how many files reference it.
- **Dropbox's Magic Pocket**:
  - Dropbox stored all user data on Amazon S3 until ~2015-2016. Then they built **Magic Pocket**, their own exabyte-scale block storage system, and migrated off S3.
  - **Why move off S3?** (a) **Cost**: At Dropbox's scale (hundreds of petabytes → exabytes), S3 storage costs dominate the P&L. Building your own is cheaper at scale. Similar to Netflix building Open Connect. (b) **Control**: Custom storage allows optimizations impossible on S3 (e.g., custom erasure coding ratios, fine-grained placement policies, hardware-specific optimizations). (c) **Performance**: Lower latency by co-locating storage with compute in Dropbox's own data centers. (d) **Dedup integration**: Tight integration between the dedup layer and the storage layer — S3 treats each object independently.
  - Magic Pocket runs across **multiple data centers** on custom hardware.
  - Data is **durably stored within seconds** of upload (sync write to multiple nodes).
  - Achieved **99.9999999% (nine 9s) durability** — comparable to S3's eleven 9s claim.
  - **Contrast with Netflix Open Connect**: Similar pattern — both Netflix and Dropbox built custom infrastructure to replace AWS services at scale. Netflix replaced CloudFront with Open Connect (CDN); Dropbox replaced S3 with Magic Pocket (storage). Both were driven by cost optimization at massive scale.
- **Erasure coding vs replication**:
  - **Replication (3x)**: Store 3 copies of every block across 3 different machines/racks. Simple, fast reads (read from any copy). Storage overhead: 200% (store 3x the data). Used by HDFS, early S3, Cassandra.
  - **Erasure coding (e.g., Reed-Solomon 6+3)**: Split block into 6 data fragments, compute 3 parity fragments. Store 9 fragments across 9 machines. Can reconstruct the block from any 6 of 9 fragments. Storage overhead: 50% (1.5x vs 3x for replication). Trade-off: reads require reading from 6 machines (higher latency, more network), repair is more compute-intensive.
  - Dropbox Magic Pocket uses **erasure coding** for cold/warm data (most data — files rarely accessed) and **replication** for hot data (recently uploaded, frequently accessed). This hybrid minimizes storage cost while maintaining read performance for active files.
  - **Contrast with HDFS**: HDFS defaults to 3x replication. Facebook's HDFS uses erasure coding for cold data (similar to Dropbox). Google's Colossus (GFS successor) uses Reed-Solomon erasure coding.
- **Storage tiers**:
  - **Hot tier**: Recently uploaded blocks, frequently accessed files. Stored on SSDs or fast HDDs with replication. Low latency reads.
  - **Warm tier**: Files accessed occasionally. Erasure-coded on HDDs. Higher read latency but much lower cost.
  - **Cold tier**: Old file versions, rarely accessed archives. Erasure-coded with aggressive ratios (e.g., 10+4), possibly on archival media. Lowest cost, highest latency.
  - Blocks are automatically migrated between tiers based on access patterns (age, read frequency).
- **Garbage collection**: When a file is deleted or a version expires, the block references are removed. But the blocks themselves may still be referenced by other files (dedup) or other versions. Garbage collection: periodically scan for blocks with zero references → mark for deletion → wait grace period → delete. Reference counting must be accurate — a block incorrectly collected while still referenced = data loss. This is one of the hardest correctness problems in a content-addressable store.
- **Integrity verification**: Periodic background process reads every block, recomputes its hash, and compares with the stored hash. Detects silent data corruption (bit rot). Corrupted blocks are repaired from erasure-coded parity or replicas. This is called **scrubbing** — same concept as ZFS scrub.
- **Contrast with S3**: S3 is a managed service — Dropbox doesn't control the hardware, placement, or encoding. Magic Pocket gives Dropbox full control. S3 charges per GB stored and per request; Magic Pocket has a fixed cost (hardware + data center + power) that's cheaper at exabyte scale.
- **Contrast with Google Colossus**: Google's Colossus (GFS successor) is the storage layer for Google Drive. It uses Reed-Solomon erasure coding, is co-located in Google's data centers, and supports exabyte-scale. Conceptually similar to Magic Pocket — both are custom-built exabyte-scale block stores replacing earlier reliance on simpler storage systems.

### 7. 07-sharing-and-permissions.md — Sharing & Permissions

Sharing transforms Dropbox from "personal backup" to "collaboration platform." It's also where the most complex metadata and consistency challenges arise.

- **Sharing model**:
  - **Shared folders**: A folder is shared with specific users. It appears in each user's file tree. All members see the same content, synced in real-time. Changes by any member propagate to all others.
  - **Shared links**: A URL that provides access to a file or folder. Can be: public (anyone with the link), team-only, password-protected, with expiry date. Read-only access. No account required to view.
  - **Member roles**: Owner (full control, can transfer ownership), Editor (add/edit/delete files), Viewer (read-only). Roles are per-shared-folder, not per-file.
- **ACL (Access Control List) inheritance**: Permissions cascade down the folder hierarchy. If Alice shares `/Project/` with Bob as Editor, Bob can edit all files and subfolders within `/Project/`. A subfolder can have additional sharing (e.g., `/Project/Secret/` shared with a smaller group) but cannot reduce permissions from the parent. This hierarchical ACL model is similar to Unix file permissions but more nuanced.
- **Shared namespaces**: When Alice shares a folder with Bob, a **shared namespace** is created. This namespace has its own:
  - Change journal (sync cursor is per-namespace).
  - Permission set (ACL).
  - Metadata shard (all files in the namespace are on the same MySQL shard for ACID operations).
  - The shared namespace is "mounted" into each member's file tree at potentially different paths (Alice has it at `/SharedProject/`, Bob has it at `/Work/SharedProject/`). This mount is a metadata pointer, not a copy.
- **Team/organization features**: Dropbox Business adds:
  - Team folders (admin-managed, not owned by any individual).
  - Admin console (manage members, set storage quotas, audit activity).
  - Data loss prevention (DLP) — detect and prevent sharing of sensitive data.
  - Device approval — restrict which devices can sync.
  - Remote wipe — delete Dropbox data from a lost/stolen device.
  - Audit log — track all file and sharing operations.
- **Conflict with sharing**: Shared folders amplify conflicts. If Alice and Bob both edit the same file in a shared folder simultaneously, the conflict resolution mechanism (conflicted copy) kicks in. The probability of conflicts increases with the number of collaborators and the frequency of edits.
- **Contrast with Google Drive**: Google Drive has a richer sharing model — per-file sharing (not just per-folder), commenter role, domain-level sharing (anyone in organization X can view), and real-time collaborative editing for Google Docs/Sheets/Slides. The collaborative editing eliminates most conflicts for document types. Dropbox's sharing is simpler but works for any file type.
- **Contrast with S3**: S3 has IAM policies and bucket ACLs — designed for programmatic access, not end-user collaboration. No concept of "shared folder" or "sync with collaborators." S3 permissions are infrastructure-level; Dropbox permissions are user-level.

### 8. 08-notification-and-change-propagation.md — Notification & Change Propagation

How changes propagate from one client to all other clients and collaborators in near-real-time.

- **Change journal (event log)**: Every mutation (file create, edit, delete, move, rename) is recorded as an entry in a per-namespace change journal. Entries are ordered by a monotonically increasing **cursor** (similar to WhatsApp's sequence numbers per conversation). The journal is the source of truth for "what changed."
  - Schema: `(namespaceId, cursor) → {changeType, filePath, fileId, newMetadata, timestamp}`
  - Used for: sync (client asks "what changed since cursor X?"), audit log (admin reviews activity), webhook notifications (push to integrations).
- **Long-polling notification channel**:
  - Client sends `POST /files/list_folder/longpoll` with its current cursor.
  - Server holds the request open (long-poll, up to 90 seconds).
  - If a change occurs in any of the client's namespaces → server responds immediately with `{changes: true}`.
  - Client then calls `POST /files/list_folder/continue` to fetch the actual changes.
  - This two-phase approach (notification + fetch) keeps the long-poll lightweight (no payload on the notification itself).
  - **Why long-poll over WebSocket?** (a) Simpler infrastructure — no persistent connection state. (b) Firewall-friendly — HTTP long-poll works through corporate proxies that block WebSocket. (c) Stateless — any server can handle any poll request (no connection affinity). (d) Sufficient for file sync — sub-second notification is nice but seconds-level delay is acceptable (unlike chat).
  - **Contrast with WhatsApp**: WhatsApp uses WebSocket because chat requires sub-100ms delivery. Dropbox uses long-poll because file sync tolerates seconds of delay. Different products, different trade-offs.
- **Notification fan-out**: When user A edits a file in a shared folder with 10 collaborators:
  1. Server appends to the namespace's change journal.
  2. Server looks up all users who have this namespace mounted.
  3. For each user with an active long-poll → respond immediately.
  4. For users without active polls → they'll pick up the change on their next poll.
  5. For webhook subscribers → enqueue a webhook delivery.
  - This is much simpler than WhatsApp's fan-out because: (a) Dropbox doesn't need to deliver the actual content (just "changes exist") and (b) there's no offline queue — if the client isn't polling, it picks up changes next time it polls.
- **Cursor-based sync protocol**: The cursor is an opaque token (not a timestamp, not a sequence number) that represents a position in the change journal. Advantages: (a) Client doesn't need to understand the cursor format. (b) Server can change cursor encoding without breaking clients. (c) Cursor can encode multiple namespaces' positions compactly.
- **Webhook notifications**: For server-to-server integrations (e.g., "when a file is uploaded to this folder, trigger a CI/CD pipeline"). Webhook payload is minimal — just "changes occurred, call the API to get details." Webhooks are delivered with at-least-once semantics, retries with exponential backoff.
- **Contrast with Google Drive**: Google Drive uses a similar change notification model (changes.list with pageToken, changes.watch for push notifications). But Google Drive also supports **real-time collaboration notifications** via Google Docs — character-level changes streamed via WebSocket-like channel. Dropbox only notifies at the file level.
- **Contrast with S3 Event Notifications**: S3 can send events (ObjectCreated, ObjectDeleted) to SNS/SQS/Lambda. But S3 events are: (a) coarse-grained (whole-object, not block-level), (b) eventually consistent (events may be delayed), (c) not designed for client sync (no cursor, no longpoll). S3 events are for infrastructure automation, not user-facing sync.

### 9. 09-conflict-resolution.md — Conflict Resolution

Conflicts are inevitable in a multi-device, multi-user sync system. How you handle them defines the user experience.

- **When conflicts occur**:
  1. **Multi-device**: User edits a file on laptop, then edits the same file on phone before laptop syncs. Both devices upload different versions.
  2. **Shared folder**: Alice and Bob both edit the same file in a shared folder before syncing.
  3. **Offline edits**: User edits files on an airplane. On landing, the changes conflict with changes made by collaborators while the user was offline.
- **Optimistic concurrency control**: Dropbox uses optimistic concurrency — no file locking, no exclusive access. Any client can edit any file at any time. Conflicts are detected on sync (not prevented). Why optimistic over pessimistic (locking)?
  - Locking is incompatible with offline access (can't acquire a lock without server connectivity).
  - Locking creates resource contention (a forgotten lock blocks all collaborators).
  - File conflicts are rare in practice (<0.1% of syncs) — optimizing for the common case (no conflict) is correct.
- **Conflict detection**: When client A uploads a new version:
  - Client includes the `rev` (revision ID) of the version it was editing.
  - Server checks: does client A's `rev` match the server's current `rev`?
  - If yes → no conflict, accept the upload, increment `rev`.
  - If no → conflict. Another client already uploaded a newer version.
  - This is identical to a **CAS (Compare-And-Swap)** operation or HTTP `If-Match` ETag conditional write.
- **Dropbox's conflict resolution strategy: "Conflicted Copy"**:
  - When a conflict is detected, the "winner" (first to sync) version becomes the canonical file.
  - The "loser" (second to sync) version is saved as a separate file: `filename (User's conflicted copy YYYY-MM-DD).ext`.
  - Both versions are preserved. The user is responsible for manually reviewing and merging.
  - This approach is **safe** (no data loss), **simple** (no merge logic), and **transparent** (user sees both versions).
  - Trade-off: not great UX for frequent collaborators. But Dropbox prioritizes data safety over convenience — losing data is worse than having an extra "conflicted copy" file.
- **Contrast with Google Drive (Operational Transforms)**:
  - Google Docs uses **Operational Transforms (OT)** for real-time collaborative editing. Multiple users edit the same document simultaneously. Each edit is an "operation" (insert character at position X, delete range Y-Z). The OT algorithm transforms concurrent operations to maintain consistency. No "conflicted copies" — conflicts are resolved automatically at the character level.
  - But OT only works for structured document formats (text, spreadsheets, slides). For arbitrary binary files (.psd, .zip, .mp4), Google Drive falls back to **last-writer-wins** — the last upload overwrites previous versions without creating a conflicted copy. This is WORSE than Dropbox's approach (silent data loss vs explicit conflicted copy).
- **Contrast with Git (Three-Way Merge)**:
  - Git performs a three-way merge: common ancestor + version A + version B → merged result. Text-level conflict markers show where automatic merge failed. Developer resolves manually.
  - Dropbox could theoretically do three-way merge for text files but chose not to — (a) most Dropbox files are binary (photos, PDFs, Office docs), not mergeable text, (b) automatic merge is risky for non-technical users (corrupt Office XML?), (c) "conflicted copy" is always safe.
- **Contrast with CRDTs (Conflict-free Replicated Data Types)**:
  - CRDTs (used by Figma, some collaborative editors) mathematically guarantee convergence without central coordination. Different from OT (which requires a central server to sequence operations).
  - CRDTs are great for specific data structures (counters, sets, text) but don't apply to arbitrary binary files. Relevant for the "real-time collaboration" part of Google Docs, not for Dropbox's file sync.
- **Vector clocks / version vectors**: An alternative to Dropbox's simple `rev` for conflict detection. A vector clock tracks `{deviceA: version3, deviceB: version5}`. Allows detecting concurrent edits (neither is "newer" — they diverged from a common ancestor). Dropbox's `rev` is simpler (server-assigned, monotonically increasing) because Dropbox uses a centralized server — decentralized conflict detection (vector clocks) is unnecessary when you have a single source of truth.

### 10. 10-scaling-and-reliability.md — Scaling & Reliability

Cross-cutting doc that ties together scaling decisions from all deep dives.

- **Scale numbers** (Dropbox-scale):
  - **700+ million registered users** (as of ~2021-2023).
  - **Over 15 million paying subscribers**.
  - **1.2+ billion files uploaded per day** [VERIFY against Dropbox blog].
  - **Exabytes of data stored** in Magic Pocket.
  - **Hundreds of petabytes synced per day** across all clients.
  - File count: **hundreds of billions of files** tracked in the metadata service.
- **Read vs write patterns**: File sync is read-heavy at the block level (many clients downloading the same shared file) but metadata-heavy at the sync layer (every client polls for changes frequently, even if no files changed). The long-poll notification service absorbs the metadata polling load.
- **Metadata scaling**:
  - MySQL sharded by namespace ID. Each shard handles all metadata for its namespaces.
  - Hot shards: shared folders with many collaborators and frequent changes can create hot shards. Mitigation: split large namespaces across sub-shards, or move hot namespaces to dedicated hardware.
  - Dropbox reportedly runs **thousands of MySQL shards** for metadata.
  - Caching: metadata read cache (Memcached/Redis) in front of MySQL. Popular shared folders are heavily cached.
- **Block storage scaling**: Magic Pocket scales horizontally by adding more storage nodes. Erasure coding provides storage efficiency. New blocks are written to the hottest tier and migrated to colder tiers over time.
- **Edge caching for downloads**: Popular shared files (e.g., a shared link that goes viral) are cached at edge CDN nodes (Dropbox uses a combination of their own infrastructure and commercial CDNs). This avoids hammering the origin storage.
- **Multi-DC active-active for metadata**: Metadata must be consistent across data centers. Options:
  - **Active-passive**: One primary DC, reads from replicas. Simpler but failover is slow.
  - **Active-active with consensus**: Raft/Paxos across DCs. Strong consistency but higher write latency (cross-DC round trips).
  - Dropbox reportedly uses **active-passive MySQL with fast failover** for metadata (strong consistency within a DC, asynchronous replication for disaster recovery). For block storage, data is replicated/erasure-coded across DCs.
- **Back-of-envelope math**:
  - 1.2 billion files/day ÷ 86,400 seconds = ~14,000 files/sec
  - If average file has 25 blocks (100 MB ÷ 4 MB) → ~350,000 block operations/sec
  - But dedup means only ~30-50% of blocks are actually new → ~100,000-175,000 new block writes/sec
  - Metadata: with 700M users, assume 10% are active daily = 70M users. Each polls every ~60 seconds = ~1.17M metadata polls/sec. Heavy!
  - Storage: exabytes. At $0.023/GB/month (S3 standard), 1 exabyte = $23.6M/month. Magic Pocket at ~$0.005/GB/month (own hardware) = $5.1M/month. **~$18.5M/month savings**. This is why Dropbox built Magic Pocket.
- **Contrast with Google Drive**: Google uses Colossus (their own storage, successor to GFS) and Spanner (globally consistent metadata). Google's advantage: existing massive infrastructure. Dropbox had to build it from scratch.
- **Contrast with S3**: S3 is Dropbox's former storage backend. S3 scales infinitely (from Amazon's perspective) but costs more at Dropbox's scale. The migration from S3 to Magic Pocket is one of the most impressive infrastructure stories in tech.

### 11. 11-design-trade-offs.md — Design Philosophy & Trade-off Analysis

Opinionated analysis of Dropbox's design choices — not just "what" but "why this and not that."

- **Block-level dedup vs file-level dedup**: Dropbox deduplicates at the block level (4 MB chunks). Simpler alternative: deduplicate at the file level (store one copy of identical files). Block-level is more aggressive — a file that's 99% identical to another file shares 99% of its blocks. File-level dedup would store it twice. Block-level requires more metadata (block lists per file) and more complex upload logic but saves significantly more storage. The math: at Dropbox's scale, block-level dedup reportedly saves 50-60% of storage vs no dedup. File-level dedup would save much less (most files are unique, but many share blocks with other versions of themselves).
- **Own storage (Magic Pocket) vs cloud (S3)**: At small scale, S3 wins (zero operational burden, pay-as-you-go). At Dropbox's scale (exabytes), own storage wins (~$18.5M/month savings). The breakeven point is somewhere around hundreds of petabytes. For most companies, S3 is the right choice. For Dropbox, Netflix (Open Connect), and Facebook (storage infra), building your own is cheaper.
- **Strong consistency (metadata) vs eventual consistency (blocks)**: Metadata requires strong consistency (two users listing the same folder must see the same files). Block storage can be eventually consistent (a newly uploaded block takes seconds to replicate — OK because metadata gates access). This split consistency model is pragmatic — it provides user-facing consistency where it matters while allowing storage-level optimizations.
- **Delta sync (block-level) vs full-file sync**: Dropbox's block-level delta sync saves 90%+ bandwidth for typical edits. Google Drive syncs whole files (for non-Google-Docs). Why doesn't everyone do delta sync? Because it requires: (a) a sophisticated client-side sync engine that understands block hashing, (b) a content-addressable block store on the server, (c) complex metadata (per-file block lists). Google chose to invest in real-time collaborative editing instead. Different bets on where the value lies.
- **Long-polling vs WebSocket for change notification**: Dropbox chose long-polling. WhatsApp chose WebSocket. Trade-off: long-polling is simpler, stateless, firewall-friendly but has seconds of latency. WebSocket is low-latency but requires persistent connection management. For file sync, seconds of latency is fine. For chat, it's not.
- **Optimistic concurrency vs pessimistic locking**: Dropbox uses optimistic concurrency (no locks, detect conflicts on sync). Google Docs uses a centralized OT server (conceptually a write lock). Trade-off: optimistic allows offline editing but creates "conflicted copies." OT prevents conflicts but requires online connectivity for editing. Dropbox chose offline-first; Google chose collaboration-first.
- **Hierarchical namespace vs flat namespace**: Dropbox has true folders with inheritance and nesting. S3 has a flat key-value namespace (folders are simulated via key prefixes). Trade-off: hierarchical enables intuitive UX (folder operations, ACL inheritance) but adds complexity (move folder = update all children's paths, or use indirection via fileId). Flat namespace is simpler at the storage layer but pushes complexity to the application layer.
- **Client-heavy (desktop sync) vs web-first**: Dropbox started as a desktop sync app (heavy client, file watcher, local cache). Google Drive started web-first (thin client, Google Docs in browser). Trade-off: desktop sync provides offline access and native file system integration but requires a complex native client. Web-first is zero-install and always up-to-date but requires internet connectivity. The industry has converged: both now support both modes, but their architectures still reflect their origins.
- **Erasure coding vs replication**: Magic Pocket uses erasure coding (1.5x overhead) for cold data and replication (3x overhead) for hot data. Trade-off: erasure coding halves storage cost but increases read latency and repair complexity. The hybrid approach (replicate hot, erase-code cold) is the optimal balance for Dropbox's access patterns (~90% of data is rarely accessed).
- **Contrast: "Why didn't Dropbox just use S3 forever?"**: At small scale, S3 is unbeatable (zero ops, infinite scale, pay-per-use). At Dropbox's scale (exabytes), the economics flip: own hardware is ~4-5x cheaper per GB. But building Magic Pocket required years of engineering, a dedicated storage team, and operational maturity. For any company below Dropbox's scale, "just use S3" is the correct answer.

## CRITICAL: The design must be Dropbox-centric
Dropbox is the reference implementation. The design should reflect how Dropbox actually works — its block-level dedup, delta sync engine, long-poll change notification, content-addressable storage, Magic Pocket, and namespace-based sharing. Where Google Drive, S3, or other systems made different design choices, call those out explicitly as contrasts.

## CRITICAL: Iterative build-up (DO NOT skip this)
The architecture MUST evolve iteratively. Each attempt builds on the previous one by identifying a concrete problem and solving it. The candidate should NOT jump to the final architecture. The progression should feel natural:

### Attempt 0: Single server with files on local disk
- A web server with files stored on the local file system. Client uploads/downloads files via HTTP PUT/GET. File metadata (name, size, path) stored in a PostgreSQL table.
- **Problems found**: No sync between devices, no sharing, local disk runs out of space, single point of failure, entire file must be re-uploaded on any edit.

### Attempt 1: Cloud storage (S3) + metadata database
- **Object storage (S3)** for file content — separates storage from compute, virtually unlimited capacity.
- **Metadata database (MySQL)** for file metadata: path, size, owner, version, S3 key.
- Client uploads entire file to S3, server records metadata. Client downloads file from S3 by looking up S3 key in metadata.
- **Contrast with S3 directly**: We add a metadata layer on top of S3 to support features S3 doesn't have: versioning UI, directory listing, sharing, search. S3 is the dumb store; our metadata service is the brain.
- **Problems found**: Entire file re-uploaded on every edit (1-byte change to a 1 GB file → re-upload 1 GB), no dedup (identical files stored multiple times), no sync (manual upload/download), no real-time change notification.

### Attempt 2: Chunked upload + block deduplication + resumable uploads
- **Chunk files into 4 MB blocks**. Each block is SHA-256 hashed. Upload only blocks the server doesn't already have. Metadata stores an ordered list of block hashes per file.
- **Content-addressable storage**: blocks stored by hash, not by file path. Dedup is automatic — identical blocks across different files or versions are stored once.
- **Resumable uploads**: each chunk is acknowledged independently. Resume from last successful chunk on failure.
- **Contrast with S3 multipart upload**: S3 multipart also chunks, but doesn't deduplicate across uploads. Each upload is independent.
- **Problems found**: No sync — client must manually trigger upload/download. No notification when collaborator changes a file. No conflict detection when two users edit the same file.

### Attempt 3: Sync engine (file watcher, delta sync, conflict detection)
- **Client-side sync engine**: watches the local Dropbox folder for changes (OS file system events). On change: compute block hashes → compare with server → upload changed blocks only (delta sync). Automatic, background sync.
- **Change notification**: Long-poll endpoint. Client asks "has anything changed since cursor X?" Server responds immediately when changes occur. Client fetches changes and applies them locally.
- **Conflict detection**: On upload, client includes the revision it's editing from. If server's current revision is different (another client already synced a newer version), a conflict is detected → "conflicted copy" is created.
- **Delta sync**: Only changed blocks are uploaded/downloaded. A 1-byte edit to a 1 GB file uploads ~4 MB (one block), not 1 GB. **96% bandwidth savings**.
- **Contrast with Google Drive**: Google Drive doesn't do block-level delta sync for regular files. Google's investment is in Operational Transforms for Google Docs (real-time collaboration at the character level), not in sync efficiency for arbitrary files.
- **Problems found**: No sharing (files are per-user). No collaboration features. Storage is still on S3 (expensive at scale). No edge caching for downloads.

### Attempt 4: Sharing, permissions, and collaboration
- **Shared folders**: Create a shared namespace that appears in multiple users' file trees. ACL with viewer/editor roles. Changes sync to all collaborators in real-time via the long-poll notification channel.
- **Shared links**: Generate a URL for any file or folder. Configurable: public, password-protected, expiring.
- **Team features**: Admin console, storage quotas, audit logging.
- **Namespace isolation**: Each shared folder is its own namespace with its own change journal and cursor. This scopes sync operations and permissions.
- **Contrast with Google Drive**: Google Drive has per-file sharing (more granular), commenter role, and native collaborative editing for Docs/Sheets/Slides. Dropbox's sharing is folder-centric and simpler.
- **Problems found**: S3 costs are enormous at exabyte scale. No control over storage hardware. No erasure coding (S3 handles this internally but charges for it). No edge caching for popular shared links.

### Attempt 5: Production hardening (Magic Pocket, erasure coding, multi-DC, edge caching)
- **Magic Pocket (own block storage)**: Replace S3 with Dropbox's own exabyte-scale content-addressable block store. Custom hardware, custom erasure coding, custom placement policies. ~4-5x cheaper than S3 at Dropbox's scale.
- **Erasure coding**: Reed-Solomon for cold/warm data (1.5x overhead vs 3x for replication). Replication for hot data (low-latency reads).
- **Multi-DC**: Metadata: active-passive MySQL with fast failover. Block storage: erasure-coded fragments distributed across DCs. Blocks available as long as enough fragments are reachable.
- **Edge caching**: Popular shared files cached at CDN edge nodes. Viral shared links don't hammer origin storage.
- **Integrity verification**: Background scrubbing — read every block, verify hash, repair corruption from parity.
- **Garbage collection**: Reference-counted blocks. When all versions referencing a block are deleted/expired, block is garbage-collected.
- **Monitoring**: Block storage health (corruption rate, repair rate, capacity), metadata latency (MySQL query times, cache hit rates), sync latency (time from file edit to notification delivery), upload/download throughput.
- **Contrast with Google Drive**: Google uses Colossus (their own storage, successor to GFS) and has never relied on a third-party storage provider. Dropbox's migration from S3 to Magic Pocket is unique — most companies never reach the scale where this makes sense.

## CRITICAL: Accuracy requirements

I DO NOT TRUST AI-GENERATED FACTS ABOUT SPECIFIC SYSTEMS. Every concrete claim about Dropbox internals must be verifiable against official sources. Specifically:

1. **Use WebSearch and WebFetch tools** to look up Dropbox Tech Blog, Dropbox engineering talks, and relevant documentation BEFORE writing. Search for:
   - "Dropbox Magic Pocket storage system"
   - "Dropbox sync engine architecture"
   - "Dropbox block level deduplication"
   - "Dropbox content-defined chunking"
   - "Dropbox conflict resolution conflicted copy"
   - "Dropbox long polling sync protocol"
   - "Dropbox metadata sharding MySQL"
   - "Dropbox migration off S3 Magic Pocket"
   - "Dropbox erasure coding Reed-Solomon"
   - "Dropbox scale numbers users files"
   - "Dropbox content hash algorithm"
   - "Google Drive operational transforms CRDT"
   - "Google Colossus GFS successor"
   - "S3 multipart upload vs Dropbox chunked upload"

   **BLANKET PERMISSION**: You have full permission to use WebSearch and WebFetch to read any online documentation, blog posts, or research papers. Do NOT ask the user for permission to read — just read. This applies to dropbox.tech, blogs.dropbox.com, engineering blogs, tech talks, and any other reference source. Read as many pages as needed to verify facts.

2. **For every concrete number** (user count, files uploaded per day, storage capacity, dedup ratio, block size), verify against Dropbox Tech Blog or official sources. If you cannot verify a number, explicitly write "[UNVERIFIED — check Dropbox Tech Blog]" next to it.

3. **For every claim about Dropbox internals** (sync engine implementation, Magic Pocket architecture, metadata sharding strategy), if it's not from an official Dropbox source, mark it as "[INFERRED — not officially documented]".

4. **CRITICAL: Do NOT confuse Dropbox with Google Drive or S3.** These are different systems with different philosophies:
   - Dropbox: file-centric, block-level delta sync, desktop-first, own storage (Magic Pocket), "conflicted copies" for conflicts
   - Google Drive: document-centric, real-time OT collaboration, web-first, Colossus storage, last-writer-wins for non-Docs files
   - S3: object store, no sync, no dedup, flat namespace, infrastructure service
   - When discussing design decisions, ALWAYS explain WHY Dropbox chose its approach and how Google Drive's / S3's different choices reflect different product visions.

## Key Dropbox topics to cover

### Requirements & Scale
- Cloud file storage with real-time sync across devices and users
- 700M+ users, 1.2B+ files uploaded/day, exabytes stored
- Block-level delta sync — only changed blocks uploaded/downloaded
- Block-level dedup — identical blocks stored once, referenced many times
- Sub-minute sync latency for small files, minutes for large files
- Shared folders with real-time change propagation
- Versioning (30/180 day retention), conflict resolution

### Architecture deep dives (create separate docs as listed in "Files to create" above)

### Design evolution (iterative build-up — the most important part)
- Attempt 0: Single server + local disk
- Attempt 1: S3 + metadata DB (full-file upload)
- Attempt 2: Chunked upload + block dedup + resumable uploads
- Attempt 3: Sync engine (file watcher, delta sync, long-poll, conflict detection)
- Attempt 4: Sharing & collaboration (shared namespaces, ACLs, links)
- Attempt 5: Production hardening (Magic Pocket, erasure coding, multi-DC, edge caching)

Each attempt MUST:
1. Start by identifying concrete problems with the previous attempt
2. Propose a solution
3. Explain WHY this approach (and why not the alternative — mention Google Drive / S3 where relevant)
4. End with "what's still broken?" to motivate the next attempt

### Consistency & Data
- Metadata: strongly consistent (MySQL ACID, read-after-write)
- Block storage: eventually consistent (replicate/erase-code across DCs)
- Sync cursor: per-namespace, monotonically increasing
- Conflict detection: optimistic concurrency (rev-based CAS)
- Block dedup: content-addressed (SHA-256 keyed), reference-counted

## What NOT to do
- Do NOT treat Dropbox as "just cloud storage" — it's a **sync engine** with block-level dedup, delta sync, and conflict resolution on top of cloud storage. The sync engine is the product; storage is infrastructure.
- Do NOT confuse Dropbox with Google Drive or S3. Highlight differences at every layer.
- Do NOT jump to the final architecture. Build it step by step (Attempt 0 → 5).
- Do NOT make up internal implementation details — verify against Dropbox Tech Blog or mark as inferred.
- Do NOT skip the sync engine — it is THE defining feature of Dropbox. Every design decision must account for how files are synced between clients.
- Do NOT skip conflict resolution — it's the hardest UX problem in a sync system.
- Do NOT describe features without explaining WHY they exist (what problem they solve).
- Do NOT ask the user for permission to read online documentation — blanket permission is granted.
- Do NOT write a Wikipedia article. This is an INTERVIEW SIMULATION with dialogue.
