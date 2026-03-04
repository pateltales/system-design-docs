# System Design Interview Simulation: Design Dropbox / Google Drive (Cloud File Storage & Sync)

> **Interviewer:** Principal Engineer (L8)
> **Candidate:** SDE-3 / L6
> **Duration:** ~60 minutes
> **Date:** 2026-02-20

---

## PHASE 1: Opening & Problem Statement (~2 min)

**Interviewer:**

Let's design a cloud file storage and synchronization system like Dropbox. Users should be able to store files in the cloud and have them automatically sync across all their devices. Where would you like to start?

---

## PHASE 2: Requirements Gathering & Scoping (~5 min)

**Candidate:**

> Before jumping in, I want to scope this carefully. File sync is deceptively complex — "put a file in a folder and it shows up on another device" sounds simple, but the engineering behind it is massive.
>
> **Functional Requirements:**
> - **File upload/download**: Upload files of any type and size (up to multi-GB). Download files on any device.
> - **Automatic sync**: Changes to files on one device automatically propagate to all other devices and collaborators. This is THE defining feature.
> - **Block-level delta sync**: Only upload/download the changed portions of a file, not the entire file on every edit. A 1-byte change to a 1 GB file should NOT re-upload 1 GB.
> - **Block-level deduplication**: Identical content blocks stored once, referenced many times. Saves massive storage.
> - **File versioning**: Maintain history of all file versions. Restore to any previous version within retention window (30-180 days).
> - **Sharing & collaboration**: Share folders/files with specific users (viewer/editor) or via shareable links (public/password-protected/expiring).
> - **Conflict resolution**: When two users edit the same file without syncing, detect and resolve conflicts safely (no data loss).
> - **Offline support**: Users can edit files offline. Changes sync when connectivity returns.
> - **Chunked/resumable uploads**: Large file uploads can resume from where they left off after network failures.
>
> **Out of scope for this interview** (but worth noting): real-time collaborative editing (Google Docs OT/CRDT), mobile camera upload, desktop search indexing, paper/docs editor, admin console.

**Interviewer:**

Good. What about non-functional requirements?

**Candidate:**

> | Dimension | Requirement | Rationale |
> |-----------|-------------|-----------|
> | **Reliability** | Zero data loss, ever | Users entrust their most important files to us — losing a file is catastrophic |
> | **Durability** | 12 nines (99.9999999999%) | Dropbox Magic Pocket's target; comparable to S3's 11 nines |
> | **Availability** | 99.99% (4 nines) | File access should almost never be down |
> | **Sync latency** | < 1 minute for small files, minutes for large files | Fast enough to feel "automatic" but doesn't need sub-second like chat |
> | **Consistency** | Strongly consistent metadata, eventually consistent block storage | Two users listing the same shared folder MUST see the same files; block replication can lag slightly |
> | **Scale** | 700M+ users, 3+ exabytes stored, billions of API calls/month | Dropbox-scale numbers |
> | **Bandwidth efficiency** | Block-level delta sync, dedup | Users are on varying network speeds; minimize data transfer |
> | **Storage efficiency** | Block-level dedup + erasure coding | At exabyte scale, storage cost is THE dominant expense |
>
> **Back-of-envelope math:**
> - 700M registered users, ~70M daily active (10% DAU ratio)
> - ~1.2 billion files synced/day ÷ 86,400 sec = **~14,000 file operations/sec**
> - Average file ~100 MB, average 25 blocks per file → **~350,000 block operations/sec**
> - Dedup means only ~30-50% are new blocks → **~100K-175K new block writes/sec**
> - 70M DAU polling for changes every ~60 sec = **~1.17M metadata polls/sec** — this is the heaviest load
> - 3+ exabytes stored. At S3 pricing (~$0.023/GB/month), that's **~$70M/month**. Own hardware (Magic Pocket, ~$0.005/GB) = **~$15M/month**. Savings: **~$55M/month**. This is why Dropbox built Magic Pocket.
>
> The key insight: this is a **metadata-heavy, storage-cost-dominated** system. The primary technical challenge is the sync engine (keeping files consistent across devices), and the primary business challenge is storage cost (at exabyte scale, even pennies per GB matter enormously).
>
> **Contrast with WhatsApp:** WhatsApp is connection-heavy (50M+ persistent WebSocket connections). Dropbox is metadata-heavy (1M+ polls/sec, trillions of metadata entries). Different bottlenecks, different architectures. WhatsApp uses Erlang for connections; Dropbox uses MySQL for metadata.

---

### L5 vs L6 vs L7 — Phase 2 Rubric

| Aspect | L5 (SDE-2) | L6 (SDE-3) | L7 (Principal) |
|--------|------------|------------|-----------------|
| **Functional Reqs** | Lists upload, download, sharing | Adds delta sync and dedup as core requirements, offline support, conflict resolution | Frames delta sync and dedup as THE differentiators — without them, it's just S3 with a UI |
| **Non-Functional** | "High availability, low latency" | Quantifies: 12 nines durability, 4 nines availability, explains strong vs eventual consistency split | Calculates storage cost savings from own infrastructure, frames the entire architecture around cost optimization |
| **Scale Math** | "Lots of files" | Calculates 14K file ops/sec, 1.17M polls/sec, identifies metadata polling as heaviest load | Calculates S3 vs own-hardware cost differential ($55M/month), explains why it drives the Magic Pocket decision |
| **Contrast** | None | Contrasts with S3 (no sync, no dedup) | Contrasts with WhatsApp (connection-heavy vs metadata-heavy), Google Drive (document-centric vs file-centric) |

---

## PHASE 3: API Design (~5 min)

**Interviewer:**

Let's design the key APIs. Focus on the most critical paths.

**Candidate:**

> I'll focus on four critical API groups: chunked upload with dedup (the core innovation), sync protocol (long-poll + cursor), file operations (move/copy as metadata-only), and sharing. Full API reference is in `02-api-contracts.md`.
>
> **1. Chunked Upload with Block Dedup — the core innovation**
>
> ```
> // Phase 1: Start an upload session
> POST /files/upload_session/start
> Headers: Authorization: Bearer <token>
>          Content-Type: application/octet-stream
>          Dropbox-API-Arg: {"close": false}
> Body: <first chunk bytes>
>
> Response:
> {
>   "session_id": "AAAAAFJr3gY"
> }
>
> // Phase 2: Append chunks (repeat for each chunk)
> POST /files/upload_session/append_v2
> Headers: Dropbox-API-Arg: {
>   "cursor": {
>     "session_id": "AAAAAFJr3gY",
>     "offset": 4194304          // byte offset — resume point
>   },
>   "close": false
> }
> Body: <chunk bytes>
>
> Response: 200 OK (empty body on success)
>
> // Phase 3: Commit — finalize the upload
> POST /files/upload_session/finish
> Headers: Dropbox-API-Arg: {
>   "cursor": {
>     "session_id": "AAAAAFJr3gY",
>     "offset": 8388608
>   },
>   "commit": {
>     "path": "/Documents/report.pdf",
>     "mode": "update",
>     "autorename": false,
>     "content_hash": "e3b0c44..."  // block-level hash for integrity
>   }
> }
>
> Response:
> {
>   "id": "id:abc123",
>   "name": "report.pdf",
>   "path_display": "/Documents/report.pdf",
>   "rev": "015d6a1516e7a00000000024c980b0",
>   "size": 8388608,
>   "content_hash": "e3b0c44..."
> }
> ```
>
> Key design decisions:
> - **Three-phase upload** (start → append → finish): The session ID is the resumability handle. If the connection drops after chunk 5 of 25, the client resumes from chunk 6 using the session ID and offset. No re-upload of completed chunks.
> - **4 MB chunks**: Each chunk is a dedup block. Client computes SHA-256 per block. Before uploading, client can check which blocks the server already has → skip known blocks. A 1 GB file that's 99% identical to a previous version uploads only the changed blocks (~4-40 MB instead of 1 GB).
> - **content_hash**: Dropbox's specific hash: SHA-256 each 4 MB block → concatenate block hashes → SHA-256 the concatenation. This is verifiable, dedup-friendly, and incrementally computable.
> - **`rev` field**: Server-assigned revision string. Used for optimistic concurrency — upload fails if you're editing an outdated revision. This is the conflict detection mechanism.
>
> **2. Sync Protocol — Long-Poll + Cursor**
>
> ```
> // Step 1: Initial folder listing (get first cursor)
> POST /files/list_folder
> {
>   "path": "",              // root
>   "recursive": true,
>   "include_deleted": true
> }
>
> Response:
> {
>   "entries": [
>     {"tag": "file", "name": "doc.txt", "id": "id:abc", "rev": "015d...", ...},
>     {"tag": "folder", "name": "Photos", "id": "id:def", ...}
>   ],
>   "cursor": "AAHiR-HbhI3...",    // opaque cursor token
>   "has_more": false
> }
>
> // Step 2: Long-poll — wait for changes
> POST /files/list_folder/longpoll
> {
>   "cursor": "AAHiR-HbhI3...",
>   "timeout": 90                   // seconds (max)
> }
>
> Response (blocks until change or timeout):
> {
>   "changes": true,                // something changed!
>   "backoff": null                 // or seconds to wait before next poll
> }
>
> // Step 3: Fetch the actual changes
> POST /files/list_folder/continue
> {
>   "cursor": "AAHiR-HbhI3..."
> }
>
> Response:
> {
>   "entries": [
>     {"tag": "file", "name": "doc.txt", "id": "id:abc",
>      "rev": "016a...", "content_hash": "f47ac10b...",
>      "server_modified": "2026-02-20T15:30:00Z"}
>   ],
>   "cursor": "AAJkL-MnoPq...",    // NEW cursor
>   "has_more": false
> }
>
> // Repeat from Step 2 with new cursor...
> ```
>
> Why long-poll over WebSocket?
> - **Firewall-friendly** — long-poll is just an HTTP request with a long timeout. Corporate firewalls that block WebSocket pass HTTP fine.
> - **Stateless** — any server can handle any poll (no connection affinity needed). Load balancer routes to any healthy instance.
> - **Sufficient latency** — a few seconds delay for file sync is perfectly acceptable (unlike chat where sub-100ms matters).
> - **Simpler infrastructure** — no persistent connection management, no heartbeats, no reconnection logic on the server.
>
> **Contrast with WhatsApp:** WhatsApp uses WebSocket for sub-100ms message delivery. Dropbox uses long-poll because file sync tolerates seconds of delay. Different latency requirements → different protocol choices.
>
> **3. File Operations — Metadata-Only**
>
> ```
> POST /files/move_v2
> {
>   "from_path": "/OldFolder/report.pdf",
>   "to_path": "/NewFolder/report.pdf"
> }
>
> Response:
> {
>   "metadata": {
>     "id": "id:abc123",        // same ID — blocks unchanged
>     "name": "report.pdf",
>     "path_display": "/NewFolder/report.pdf",
>     "rev": "017b...",         // new rev (metadata changed)
>     "size": 8388608,
>     "content_hash": "e3b0c44..."  // same hash — content unchanged
>   }
> }
> ```
>
> Move and copy are **metadata-only operations** — no blocks are copied or moved. The file's `id` stays the same, the `content_hash` stays the same, only the `path` and `rev` change. A 10 GB file move is instantaneous. This is possible because blocks are content-addressed (stored by hash, not by path). Copy creates a new metadata entry pointing to the same block list (copy-on-write).
>
> **4. Sharing**
>
> ```
> POST /sharing/share_folder
> {
>   "path": "/TeamProject",
>   "members": [
>     {"member": {"email": "bob@example.com"}, "access_level": "editor"},
>     {"member": {"email": "carol@example.com"}, "access_level": "viewer"}
>   ]
> }
>
> Response:
> {
>   "shared_folder_id": "sf:12345",
>   "name": "TeamProject",
>   "access_type": "owner",
>   "members": [...]
> }
> ```
>
> Sharing creates a **shared namespace**. The folder appears in each member's file tree. Changes by any member propagate to all others via the long-poll notification channel. The namespace is the unit of: change journal scoping, permission checking, and sync cursor tracking.

**Interviewer:**

Why does Dropbox use a two-phase notification (long-poll says "changes exist" but doesn't include the changes)?

**Candidate:**

> Two reasons:
>
> 1. **Lightweight notification**: The long-poll response is tiny (just `{changes: true}`). If we stuffed the actual changes into the poll response, we'd need to serialize potentially thousands of file changes into the response body. The long-poll is a notification channel, not a data channel.
>
> 2. **Cursor consistency**: The client's cursor must advance atomically with the changes it processes. Separating notification from data fetching lets the client: (a) receive notification, (b) fetch changes at its own pace, (c) ACK by updating its cursor. If the client crashes between steps, it re-fetches from the old cursor — no data loss, no duplicate processing.
>
> **Contrast with WhatsApp:** WhatsApp pushes actual message content over the WebSocket (encrypted blob + metadata in one frame). This works because messages are small (~1 KB) and latency is critical. Dropbox's changes can be large (thousands of file entries) and latency tolerance is higher, so separation makes sense.

---

### L5 vs L6 vs L7 — Phase 3 Rubric

| Aspect | L5 (SDE-2) | L6 (SDE-3) | L7 (Principal) |
|--------|------------|------------|-----------------|
| **Upload API** | Simple POST with file body | Three-phase chunked upload with session ID, resumability, block-level dedup | Explains content_hash algorithm, why 4 MB block size, how dedup check happens before upload |
| **Sync Protocol** | "Poll the server for changes" | Long-poll + cursor, explains two-phase notification/data separation | Compares long-poll vs WebSocket with concrete trade-offs, explains why stateless matters for scaling |
| **File Operations** | "Move copies the file" | Move/copy are metadata-only (O(1) regardless of file size), explains content-addressing | Discusses copy-on-write semantics, reference counting for garbage collection |
| **Sharing** | "Share via link" | Shared namespaces, per-member file tree mounting, namespace-scoped change journals | Discusses ACL inheritance, cross-namespace metadata consistency |

---

## PHASE 4: High-Level Architecture — Iterative Build-Up (~15 min)

**Interviewer:**

Let's build the architecture. Start simple and evolve.

### Attempt 0: Single Server with Files on Local Disk

**Candidate:**

> Simplest possible thing:
>
> ```
> +--------+     HTTP PUT /upload     +------------------+
> | Client | -----------------------> |                  |
> |        |                          |   Single Server  |
> +--------+                          |   (Node.js/etc)  |
>                                     |                  |
> +--------+     HTTP GET /download   |   + Local Disk   |
> | Client | -----------------------> |   + PostgreSQL   |
> |        | <-- file bytes           |   (metadata)     |
> +--------+                          +------------------+
> ```
>
> Client uploads a file via HTTP PUT. Server stores it on local disk. Metadata (filename, path, size) in PostgreSQL. Download via GET.
>
> | Problem | Impact |
> |---------|--------|
> | No sync between devices | The whole point of Dropbox — useless without sync |
> | Local disk runs out of space | Can't scale storage beyond one server |
> | Entire file re-uploaded on every edit | 1-byte change to 1 GB file = re-upload 1 GB |
> | No deduplication | Same file uploaded by different users = stored multiple times |
> | Single point of failure | Server dies = all files lost |
> | No versioning | Overwritten files are gone forever |
> | No sharing | Files are per-user only |

**Interviewer:**

Let's fix the storage problem first.

### Attempt 1: Cloud Storage (S3) + Metadata Database

**Candidate:**

> Separate storage from compute. Use S3 for file content, MySQL for metadata.
>
> ```
> +--------+  HTTP   +----------------+     +------------------+
> | Client | ------> | API Server     | --> | Metadata DB      |
> |        |         | (upload/       |     | (MySQL)          |
> +--------+         |  download)     |     | path, size, rev, |
>                    +-------+--------+     | s3_key, owner    |
>                            |              +------------------+
>                            | PUT/GET
>                            v
>                    +------------------+
>                    |   Amazon S3      |
>                    |   (object store) |
>                    +------------------+
> ```
>
> Client uploads entire file → API server stores it in S3 → records metadata in MySQL (path, size, S3 key, owner, version). Download: look up S3 key in MySQL → fetch from S3 → return to client.
>
> **What's better:**
> - Virtually unlimited storage (S3 scales)
> - Metadata separated from content (can scale independently)
> - S3 provides durability (11 nines)
>
> **Contrast with S3 directly:** We add a metadata layer on top of S3 because S3 alone doesn't support: directory listing (S3 "folders" are just key prefixes), versioning UI, sharing, search, or sync. S3 is dumb storage; our metadata service is the brain.
>
> | Problem | Impact |
> |---------|--------|
> | Entire file re-uploaded on every edit | 1-byte change to 1 GB → re-upload 1 GB. Wastes bandwidth, slow |
> | No deduplication | 1000 users upload same PDF → stored 1000 times on S3. Wastes storage |
> | No sync | Client must manually upload/download — no auto-sync |
> | No real-time change notification | No way to know when collaborator changed a file |
> | S3 costs grow linearly with data | At exabyte scale, S3 is prohibitively expensive |

**Interviewer:**

The bandwidth waste from full-file uploads is terrible. How do we fix that?

### Attempt 2: Chunked Upload + Block Deduplication + Resumable Uploads

**Candidate:**

> This is where Dropbox's core innovation lives — block-level dedup.
>
> ```
> +--------+  block hashes  +----------------+     +------------------+
> | Client | -------------> | Block Server   | --> | Block Store      |
> | (sync  |  "do you have  |  (dedup check) |     | (S3 / CAS)       |
> |  engine|   these?"      +-------+--------+     | SHA-256 → blob   |
> |  )     |                        |               +------------------+
> |        | --- new blocks ------->|
> |        |                        |               +------------------+
> |        | -- commit metadata --> | Metadata Svc  | Metadata DB      |
> |        |                       | (file→blocks) | (MySQL)          |
> +--------+                       +--------------+ | fileId, blockList|
>                                                   | rev, path, size  |
>                                                   +------------------+
> ```
>
> **How it works:**
> 1. File is split into **4 MB blocks** on the client.
> 2. Client computes **SHA-256 hash** of each block.
> 3. Client sends block hashes to server: "Do you already have blocks with these hashes?"
> 4. Server checks its **content-addressable store** (blocks stored by hash). Returns: "I have blocks A, C, E. I need blocks B, D."
> 5. Client uploads **only the new blocks** (B, D).
> 6. Client commits metadata: file path → ordered list of block hashes.
>
> **Dedup in action:**
> - User edits 1 byte in a 1 GB file (250 blocks). Only 1 block changes → upload 4 MB instead of 1 GB. **99.6% bandwidth savings.**
> - 1000 users upload the same 100 MB PDF. 25 blocks, all identical → stored once, referenced 1000 times. **99.9% storage savings** for that file.
>
> **Content-addressable storage (CAS):** Blocks are stored by their SHA-256 hash, not by file path. Two completely different files that happen to share some content share those blocks. This is the same principle as Git's object store.
>
> **Resumable uploads:** Each chunk upload is independently acknowledged. If connection drops after chunk 5 of 25, resume from chunk 6 using the session ID and byte offset. No re-upload of completed chunks.
>
> **content_hash algorithm** (Dropbox's official scheme): Split file into 4 MB blocks → SHA-256 each block → concatenate block hashes → SHA-256 the concatenation. This hash is deterministic, dedup-friendly, and incrementally computable.
>
> **Contrast with S3 multipart upload:** S3 multipart also chunks files and supports resumable uploads. But S3 does NOT deduplicate across uploads — each upload stores its own parts independently. S3's multipart is about reliability; Dropbox's chunking is about dedup + reliability.
>
> | Problem | Impact |
> |---------|--------|
> | No sync — client must manually trigger upload/download | Not "Dropbox" without automatic sync |
> | No notification when a collaborator changes a file | Users must manually check for updates |
> | No conflict detection | Two users edit same file → data loss (last write wins silently) |
> | No sharing | Files are per-user only |
> | S3 still expensive at exabyte scale | Storage cost dominates |

**Interviewer:**

We have efficient uploads now. How do we make it sync automatically?

### Attempt 3: Sync Engine (File Watcher, Delta Sync, Conflict Detection)

**Candidate:**

> This is where we build the "magic" — the client-side sync engine that makes files automatically appear on all devices.
>
> ```
>                                    +-----------------------+
>                                    |  Notification Service |
>                                    |  (long-poll endpoint) |
>                                    +----------+------------+
>                                               |
>                           "changes exist"      |  long-poll
>                                               |
> +------------------+                          |          +------------------+
> | Client A         |    upload changed blocks |          | Client B         |
> | +-------------+  |  ----------------------->|          | +-------------+  |
> | | File Watcher|  |                          |          | | File Watcher|  |
> | | (inotify/   |  |    "changes exist"       |          | | (FSEvents)  |  |
> | |  FSEvents)  |  |  <------- long-poll -----+          | |             |  |
> | +------+------+  |                                     | +------+------+  |
> |        |         |    fetch changes                    |        |         |
> | +------v------+  |  ----------------------->           | +------v------+  |
> | | Block Hasher|  |    download new blocks              | | Block Hasher|  |
> | | (SHA-256)   |  |  <-------------------------------   | | (SHA-256)   |  |
> | +------+------+  |                                     | +------+------+  |
> |        |         |                                     |        |         |
> | +------v------+  |  +-------------+  +-----------+    | +------v------+  |
> | | Delta Sync  |  |  | Metadata    |  | Block     |    | | Delta Sync  |  |
> | | (upload new |  |  | Service     |  | Store     |    | | (download   |  |
> | |  blocks)    |  |  | (MySQL)     |  | (S3/CAS)  |    | |  new blocks)|  |
> | +-------------+  |  +-------------+  +-----------+    | +-------------+  |
> +------------------+                                     +------------------+
>
> Sync flow:
> 1. Client A edits file → File Watcher detects change
> 2. Block Hasher: split into 4MB blocks, hash each, compare with server's block list
> 3. Delta Sync: upload only changed blocks → commit metadata (new rev)
> 4. Server appends to change journal, increments cursor
> 5. Notification Service: Client B's long-poll returns {changes: true}
> 6. Client B fetches changes → downloads new blocks → reconstructs file locally
> ```
>
> **Client-side sync engine (Nucleus — Dropbox rewrote it in Rust):**
> - **File watcher**: Uses OS-level file system events (`inotify` on Linux, `FSEvents` on macOS, `ReadDirectoryChangesW` on Windows) to detect changes instantly.
> - **Block hasher**: Computes SHA-256 for each 4 MB block of the changed file. Compares with the server's block list for that file.
> - **Delta sync**: Uploads only blocks with new hashes. Downloads only blocks the local client doesn't have.
> - **Conflict detection**: On upload, client includes the `rev` of the version it was editing. If server's current `rev` is different (another client already synced), a conflict is detected → server creates a "conflicted copy" (e.g., `report (Alice's conflicted copy 2026-02-20).docx`). Both versions preserved, no data loss.
>
> **Change journal:** Every mutation (create, edit, delete, move) is appended to a per-namespace change journal with a monotonically increasing cursor. Clients track their cursor position and ask "what changed since cursor X?"
>
> **Long-polling:** Client calls `/files/list_folder/longpoll` with its cursor. Server blocks up to 90 seconds. Returns immediately when a change occurs. Client fetches changes, applies locally, updates cursor, repeats.
>
> **Contrast with Google Drive:** Google Docs uses **Operational Transforms (OT)** for real-time character-level collaborative editing — multiple cursors, instant merge, no "conflicted copies." But OT only works for Google's structured document formats (Docs, Sheets, Slides). For arbitrary binary files (.psd, .zip, .mp4), Google Drive falls back to **last-writer-wins** — the last upload silently overwrites with no conflict detection. Dropbox's "conflicted copy" approach is safer for arbitrary files.
>
> | Problem | Impact |
> |---------|--------|
> | No sharing | Files are per-user only — can't collaborate |
> | No folder-level permissions | Can't control who sees what |
> | S3 still expensive at exabyte scale | The cost problem hasn't been addressed |
> | No edge caching | Popular shared files hammer the origin |
> | Single data center | DC failure = total outage |

**Interviewer:**

Good. Add sharing and collaboration.

### Attempt 4: Sharing, Permissions, and Collaboration

**Candidate:**

> ```
>                    +-----------------------------------+
>                    |        Sharing Service             |
>                    |  - Shared namespace creation       |
>                    |  - ACL management (viewer/editor)  |
>                    |  - Shared link generation          |
>                    +----------------+------------------+
>                                     |
>                                     v
>   Alice's tree:             Shared Namespace             Bob's tree:
>   /Alice/Dropbox/           (ns:12345)                   /Bob/Dropbox/
>     └── TeamProject/ ◄────► TeamProject/  ◄────────────►   └── Work/
>         ├── doc.txt         ├── doc.txt                        └── TeamProject/
>         └── data.csv        └── data.csv                           ├── doc.txt
>                                                                    └── data.csv
>
>   Alice edits doc.txt:
>   1. Upload changed blocks → commit to ns:12345
>   2. Change journal for ns:12345 incremented
>   3. Bob's long-poll returns (he subscribes to ns:12345)
>   4. Bob downloads changed blocks → file updated locally
> ```
>
> **Shared namespaces:** When Alice shares a folder with Bob, a **shared namespace** is created. This namespace is the unit of:
> - **Change tracking**: Its own change journal with its own cursor.
> - **Permissions**: ACL (owner/editor/viewer) scoped to the namespace.
> - **Metadata**: All files in the namespace are on the same MySQL shard for ACID operations.
> - **Mounting**: The namespace is mounted into each member's file tree at potentially different paths. The mount is a metadata pointer, not a copy.
>
> **ACL inheritance**: Permissions cascade down the folder hierarchy. Share `/TeamProject/` with Bob as editor → Bob can edit all files and subfolders within it.
>
> **Shared links**: Generate a URL for any file/folder. Configurable: public, password-protected, expiring, team-only. Read-only access. No Dropbox account required to view.
>
> **Notification fan-out**: When Alice edits a file in a shared namespace, the notification service looks up all users who have this namespace mounted and responds to their active long-polls.
>
> **Contrast with Google Drive:** Google Drive has richer sharing — per-file sharing (not just per-folder), commenter role, domain-level sharing ("anyone in organization X"), and native collaborative editing for Docs/Sheets/Slides. Dropbox's sharing is simpler but works for any file type.
>
> **Contrast with S3:** S3 has IAM policies and bucket ACLs — designed for programmatic access, not end-user collaboration. No "shared folder" or "sync with collaborators" concept.
>
> | Problem | Impact |
> |---------|--------|
> | S3 costs enormous at exabyte scale | ~$70M/month at S3 pricing |
> | No control over storage hardware | Can't optimize erasure coding, placement, or tiering |
> | No edge caching | Viral shared links hammer origin |
> | Single data center | DC failure = total outage |
> | No integrity verification | Silent data corruption goes undetected |

**Interviewer:**

The S3 cost problem is killing us. How do we fix it at exabyte scale?

### Attempt 5: Production Hardening (Magic Pocket, Erasure Coding, Multi-DC, Edge Caching)

**Candidate:**

> This is where we graduate from "startup on AWS" to "planet-scale file storage."
>
> ```
> +======================== Dropbox Architecture ========================+
> |                                                                       |
> |  Clients (Desktop/Mobile/Web)                                        |
> |  - File watcher (OS events) → block hasher (SHA-256) → delta sync   |
> |  - Sync engine (Nucleus, written in Rust)                            |
> |  - Long-poll for change notification                                 |
> |                                                                       |
> |  ┌──── Load Balancer ────┐                                           |
> |  │                       │                                           |
> |  v                       v                                           |
> |  +------------------+  +------------------+                          |
> |  | API Servers      |  | Notification Svc |                          |
> |  | (upload, download,|  | (long-poll       |                          |
> |  |  file ops)       |  |  endpoint)       |                          |
> |  +--------+---------+  +--------+---------+                          |
> |           |                      |                                    |
> |           v                      v                                    |
> |  +------------------+  +------------------+                          |
> |  | Block Server     |  | Change Journal   |                          |
> |  | (dedup check,    |  | (per-namespace   |                          |
> |  |  upload/download) |  |  event log)     |                          |
> |  +--------+---------+  +------------------+                          |
> |           |                                                           |
> |  +--------v-------------------------------------------+              |
> |  |           Magic Pocket (Block Storage)              |              |
> |  |  +----------+  +-----------+  +----------------+   |              |
> |  |  | Hot Tier  |  | Warm Tier |  | Cold Tier      |   |              |
> |  |  | (SSD +    |  | (Erasure  |  | (Cross-region  |   |              |
> |  |  |  Replica) |  |  coded,   |  |  erasure coded |   |              |
> |  |  |           |  |  LRC-     |  |  3+1 or 2+1)   |   |              |
> |  |  |           |  |  12,2,2)  |  |                |   |              |
> |  |  +----------+  +-----------+  +----------------+   |              |
> |  |  3+ exabytes across multiple data centers           |              |
> |  +----------------------------------------------------+              |
> |                                                                       |
> |  +------------------+  +------------------+                          |
> |  | Metadata Service |  | Edge CDN         |                          |
> |  | (Edgestore)      |  | (shared link     |                          |
> |  | MySQL sharded by |  |  downloads)      |                          |
> |  | namespace ID     |  |                  |                          |
> |  | Trillions of     |  |                  |                          |
> |  | entries          |  |                  |                          |
> |  +------------------+  +------------------+                          |
> |                                                                       |
> |  Monitoring: Sync latency, Block write throughput, Dedup ratio       |
> |  Integrity: Background scrubbing (recompute + verify block hashes)   |
> +======================================================================+
> ```
>
> #### Magic Pocket (Own Block Storage)
> - Replace S3 with Dropbox's custom exabyte-scale content-addressable block store.
> - Custom hardware: 7th-generation servers, **2+ PB per server, 20+ PB per rack**. ~100 large-form-factor HDDs per chassis. SMR (Shingled Magnetic Recording) drives expanded capacity from 8 TB to 14 TB per disk. SSD caches for hot data.
> - **Durability target: 12 nines** (99.9999999999%).
> - Multi-zone architecture across US-West, US-Central, US-East.
> - Each "cell" stores ~50 PB of raw data, with a single master per cell.
> - **Cost savings**: $74.6M in total operational expense reduction over two years (from Dropbox's S-1 filing). Migration completed between Feb-Oct 2015, moving ~90% of an estimated 600 PB off S3.
>
> #### Erasure Coding (Tiered Strategy)
>
> | Tier | Strategy | Overhead | Use Case |
> |------|----------|----------|----------|
> | **Hot** | Full replication (2x+) | 2x+ | Freshly uploaded blocks, frequently accessed |
> | **Warm (intra-zone)** | LRC-(12,2,2) erasure coding | ~1.33x | Aged blocks, tolerates 3 failures |
> | **Warm (cross-zone)** | 1+1 replication across zones | 2x | Zone-level redundancy |
> | **Cold (3-region)** | 2+1 erasure coding | 1.5x | Rarely accessed, cross-region durability |
> | **Cold (4-region)** | 3+1 erasure coding | 1.33x | Maximum storage efficiency |
>
> Lifecycle: blocks start fully replicated (fast availability) → migrate to erasure-coded as they age (cost optimization). This hybrid minimizes cost while maintaining performance for active files.
>
> #### Metadata Service (Edgestore)
> - Graph storage system built on MySQL. Trillions of entries. Millions of QPS.
> - Sharded by namespace ID — all files in a namespace (shared folder) on the same shard for ACID.
> - Caching layer absorbs ~95% of reads.
> - Cross-shard transactions at 10M requests/sec via modified 2-phase commit.
> - 5 nines availability (99.999%).
>
> #### Edge CDN for Downloads
> - Popular shared links (viral files) cached at edge CDN.
> - Prevents origin hammering.
> - Large file downloads served with HTTP range requests for resumability.
>
> #### Integrity Verification (Scrubbing)
> - Background process reads every block, recomputes SHA-256, compares with stored hash.
> - Detects silent data corruption (bit rot).
> - Corrupted blocks repaired from erasure-coded parity fragments.
> - Same concept as ZFS scrub.
>
> #### Multi-DC
> - Metadata: MySQL replication across DCs (active-passive with fast failover for strong consistency).
> - Block storage: erasure-coded fragments distributed across zones/regions. Blocks available as long as enough fragments are reachable.
> - Each block stored in at least two separate zones, replicated within one second of upload.

---

### Architecture Evolution Table

| Attempt | Key Addition | Problem Solved | Key Technology |
|---------|-------------|---------------|----------------|
| 0 | Local disk + PostgreSQL | Baseline — simplest thing | HTTP, PostgreSQL, local filesystem |
| 1 | S3 + MySQL | Unlimited storage, metadata separation | S3, MySQL |
| 2 | Chunked upload + block dedup | Bandwidth savings (96%+), storage dedup, resumable uploads | SHA-256, content-addressable store |
| 3 | Sync engine + long-poll + conflict detection | Automatic sync, delta sync, safe conflict handling | File watcher, long-poll, change journal |
| 4 | Shared namespaces + ACLs + links | Collaboration, multi-user sync | Namespace mounting, ACL inheritance |
| 5 | Magic Pocket + erasure coding + multi-DC | Cost optimization ($55M/month savings), durability, reliability | Custom hardware, LRC erasure coding, scrubbing |

---

### L5 vs L6 vs L7 — Phase 4 Rubric

| Aspect | L5 (SDE-2) | L6 (SDE-3) | L7 (Principal) |
|--------|------------|------------|-----------------|
| **Architecture Evolution** | Jumps to "S3 + sync" in one step | Builds iteratively, each step motivated by concrete problems | Each step includes quantitative reasoning (bandwidth savings, cost differential), contrasts with alternatives |
| **Block Dedup** | "Hash files for dedup" | Block-level dedup with 4 MB chunks, content_hash algorithm, CAS | Discusses content-defined chunking vs fixed-size, rolling hash for insertion-resilience |
| **Sync Engine** | "Poll for changes" | Long-poll + cursor, file watcher, delta sync, conflict detection | Discusses Rust rewrite (Nucleus), streaming sync optimization, simulation testing |
| **Storage** | "Use S3" | Explains why own storage at exabyte scale, erasure coding tiers | Quantifies cost savings ($74.6M), discusses LRC vs Reed-Solomon, scrubbing for integrity |
| **Metadata** | "Use a database" | MySQL sharded by namespace, strong consistency requirement | Discusses Edgestore graph model, cross-shard 2-phase commit, 95% cache hit rate |

---

## PHASE 5: Deep Dive — Sync Engine (~8 min)

**Interviewer:**

The sync engine is Dropbox's core differentiation. Go deep on how it works, including the tricky parts.

**Candidate:**

> The sync engine runs on every client device. Dropbox rewrote it from scratch in Rust over four years — they call it **"Nucleus."** Let me walk through the complete sync cycle.
>
> **Upload path (local change → server → other clients):**
>
> ```
> Local filesystem event (e.g., file saved)
>   │
>   ▼
> ┌──────────────┐
> │ File Watcher  │ ◄── OS-level: inotify (Linux), FSEvents (macOS),
> │               │     ReadDirectoryChangesW (Windows)
> └──────┬───────┘
>        │
>        ▼
> ┌──────────────┐
> │ Event Dedup  │ ◄── Coalesce rapid events (e.g., text editor saves
> │ & Debounce   │     3 times in 1 second — only process last one)
> └──────┬───────┘
>        │
>        ▼
> ┌──────────────┐
> │ Block Hasher │ ◄── Split file into 4MB blocks
> │ (SHA-256)    │     Hash each block with SHA-256
> │              │     Compare with cached block list from server
> └──────┬───────┘
>        │ List of new/changed block hashes
>        ▼
> ┌──────────────┐
> │ Dedup Check  │ ◄── Send block hashes to server
> │ (has_blocks) │     Server responds: "I need blocks B, D"
> │              │     (Already have A, C, E)
> └──────┬───────┘
>        │ Only new blocks
>        ▼
> ┌──────────────┐
> │ Block Upload │ ◄── Compress (lz4/zlib) each new block
> │ (chunked)    │     Upload via upload_session API
> │              │     Each chunk independently ACKed (resumable)
> └──────┬───────┘
>        │
>        ▼
> ┌──────────────┐
> │ Metadata     │ ◄── Commit: file path → [block_hash_1, block_hash_2, ...]
> │ Commit       │     Include parent rev for conflict detection
> │              │     Server assigns new rev, appends to change journal
> └──────────────┘
> ```
>
> **Download path (server change → local):**
>
> ```
> Long-poll returns {changes: true}
>   │
>   ▼
> ┌──────────────┐
> │ Fetch Changes│ ◄── GET /files/list_folder/continue with cursor
> │              │     Returns: list of changed files + new metadata
> └──────┬───────┘
>        │ For each changed file:
>        ▼
> ┌──────────────┐
> │ Block Diff   │ ◄── Compare server's block list with local block list
> │              │     Identify blocks we don't have locally
> └──────┬───────┘
>        │ Only new blocks needed
>        ▼
> ┌──────────────┐
> │ Block        │ ◄── Download missing blocks from block store
> │ Download     │     Verify SHA-256 hash on receipt
> └──────┬───────┘
>        │
>        ▼
> ┌──────────────┐
> │ File         │ ◄── Reassemble file from blocks
> │ Reconstruct  │     Write to local filesystem atomically
> │              │     (write to temp file, then rename)
> └──────┬───────┘
>        │
>        ▼
> ┌──────────────┐
> │ Cursor Update│ ◄── Update local cursor position
> │              │     Resume long-poll with new cursor
> └──────────────┘
> ```
>
> **Streaming sync optimization** (Dropbox innovation): Traditional sync is two-phase: uploader uploads all blocks → commits metadata → server notifies downloaders → downloaders fetch blocks. Dropbox's "streaming sync" lets downloading clients **prefetch blocks before the upload fully commits**. The metaserver stores temporary state in memcache during in-progress commits. Result: up to **2x faster** multi-client sync for large files.
>
> **Nucleus threading model** (Rust sync engine):
> - Single **Control Thread** handles nearly all sync logic deterministically.
> - Network I/O offloaded to an **event loop**.
> - CPU-intensive work (hashing) offloaded to a **thread pool**.
> - Filesystem I/O runs on a **dedicated thread**.
> - This architecture makes the sync engine deterministic and testable — Dropbox runs millions of pseudorandom simulation tests daily.
>
> **Why Rust?** Dropbox calls Rust "a force multiplier" — the type system encodes complex invariants (sync state machine, file ownership, block references) that the compiler validates at compile time. Eliminated entire categories of bugs (null dereferences, data races, use-after-free) that plagued the legacy Python sync engine.
>
> See `03-file-sync-engine.md` for the complete deep dive.

---

### L5 vs L6 vs L7 — Phase 5 Rubric

| Aspect | L5 (SDE-2) | L6 (SDE-3) | L7 (Principal) |
|--------|------------|------------|-----------------|
| **Sync Flow** | "Detect change, upload file" | Full upload + download path with block diffing, dedup check, resumable chunks | Streaming sync optimization, discusses Nucleus threading model |
| **File Watcher** | "Watch the folder for changes" | OS-specific APIs (inotify, FSEvents), event debouncing | Discusses false positives, race conditions (file still being written), atomic writes |
| **Delta Sync** | "Upload changed parts" | Block-level: 4 MB blocks, SHA-256, only new blocks uploaded | Content-defined chunking (Rabin fingerprint) for insertion-resilience, rolling hash |
| **Conflict** | "Keep both versions" | Rev-based CAS, conflicted copy with username and date | Discusses why OT doesn't apply to binary files, Git-style 3-way merge trade-offs |

---

## PHASE 6: Deep Dive — Block Storage (Magic Pocket) (~8 min)

**Interviewer:**

Dropbox built their own storage system. Walk me through Magic Pocket and why they left S3.

**Candidate:**

> Magic Pocket is Dropbox's exabyte-scale content-addressable block store. It's one of the most impressive infrastructure projects in tech — they migrated ~600 PB off S3 in about 8 months.
>
> **Why leave S3?**
>
> | Factor | S3 | Magic Pocket |
> |--------|-----|-------------|
> | Storage cost per GB/month | ~$0.023 | ~$0.005 (estimated) |
> | At 1 exabyte | ~$23.6M/month | ~$5.1M/month |
> | Egress fees | Per-GB charges | Free (own network) |
> | Request fees | Per-request charges | Fixed cost (own hardware) |
> | Control | Black box | Full stack control |
> | Custom erasure coding | No (S3 manages internally) | Yes (LRC, tiered) |
> | Custom hardware | No | Yes (SMR drives, SSD cache, custom chassis) |
> | Total savings | — | **$74.6M over 2 years** (S-1 filing) |
>
> The pattern is the same as Netflix building Open Connect instead of using CloudFront: at massive scale, owning infrastructure is dramatically cheaper than renting it.
>
> **Architecture:**
>
> ```
> +==================== Magic Pocket Cell (~50 PB) ====================+
> |                                                                      |
> |  Single Master (per cell)                                           |
> |  - Tracks block placement                                          |
> |  - Assigns volume groups                                           |
> |  - Coordinates erasure coding                                      |
> |                                                                      |
> |  +------- OSD -------+  +------- OSD -------+  +------- OSD -----+ |
> |  | Object Storage     |  | Object Storage     |  | Object Storage  | |
> |  | Device             |  | Device             |  | Device          | |
> |  | ~1-2 PB per server |  | ~1-2 PB per server |  | ~1-2 PB/server  | |
> |  | ~100 LFF HDDs      |  | ~100 LFF HDDs      |  | ~100 LFF HDDs   | |
> |  | SSD cache layer    |  | SSD cache layer    |  | SSD cache layer | |
> |  | 100 Gb NIC         |  | 100 Gb NIC         |  | 100 Gb NIC      | |
> |  +-------------------+  +-------------------+  +-----------------+ |
> |                                                                      |
> |  Volume Group (6 data OSDs + 3 parity OSDs):                       |
> |  [D1] [D2] [D3] [D4] [D5] [D6] [P1] [P2] [P3]                    |
> |   └──────────────────┬───────────────────────┘                      |
> |                      Tolerates any 3 failures                       |
> |                                                                      |
> |  No filesystem — Dropbox writes directly to raw disks              |
> |  SMR drives: 14 TB each (vs 8 TB conventional) → 99%+ of fleet    |
> +====================================================================+
>
> Multi-zone deployment: US-West, US-Central, US-East
> Each block in at least 2 zones, replicated within 1 second of upload
> ```
>
> **Block lifecycle:**
> 1. **Upload**: Block received → immediately replicated to multiple OSDs (hot tier, 2x+). Available for download within seconds.
> 2. **Aging**: Background process aggregates blocks into "buckets" → applies erasure coding (LRC-12,2,2 for warm). Storage overhead drops from 2x to ~1.33x.
> 3. **Cold migration**: Rarely accessed blocks → cross-region erasure coding (2+1 or 3+1). Storage overhead drops further.
>
> **Garbage collection**: When all file versions referencing a block are deleted/expired, the block's reference count reaches zero → marked for GC → grace period → deleted. Reference counting must be exact — a block incorrectly collected while still referenced = data loss. This is one of the hardest correctness problems.
>
> **Integrity (scrubbing)**: Background process reads every block, recomputes SHA-256, compares with stored hash. Detects silent data corruption (bit rot from cosmic rays, disk firmware bugs). Corrupted blocks repaired from erasure-coded parity. Same concept as ZFS scrub.
>
> **Contrast with Google Colossus:** Google's Colossus (GFS successor) serves a similar role for Google Drive. Also uses Reed-Solomon erasure coding, custom hardware, exabyte-scale. The key difference: Google already had Colossus; Dropbox had to build Magic Pocket from scratch while running on S3.
>
> See `06-block-storage.md` for the complete deep dive.

---

### L5 vs L6 vs L7 — Phase 6 Rubric

| Aspect | L5 (SDE-2) | L6 (SDE-3) | L7 (Principal) |
|--------|------------|------------|-----------------|
| **Why Own Storage** | "S3 is expensive" | Quantifies cost differential, lists 5 reasons (cost, control, custom erasure, hardware, egress) | Calculates exact savings from S-1, explains the breakeven point |
| **Erasure Coding** | "Replicate data" | Explains Reed-Solomon, LRC, tiered strategy (hot replicated → warm erasure coded → cold cross-region) | Discusses LRC vs pure RS trade-offs, repair efficiency, SMR drive economics |
| **Integrity** | "Check for errors" | Scrubbing process, bit rot detection, repair from parity | Discusses failure modes (silent corruption rates, firmware bugs), why scrubbing frequency matters |
| **GC** | "Delete unused blocks" | Reference counting, grace periods, correctness challenges | Discusses GC being the hardest correctness problem, how a bug = data loss |

---

## PHASE 7: Deep Dive — Metadata Service (~5 min)

**Interviewer:**

How does the metadata service handle trillions of entries with strong consistency?

**Candidate:**

> Dropbox's metadata store is called **Edgestore** — a graph storage system built on MySQL. It's been running since ~2012.
>
> **Data model (graph-based):**
> - **Entities**: File, Folder, User, Team, SharedFolder — each with typed attributes.
> - **Associations**: Relationships between entities (e.g., UserOwnsFolderAssoc, FolderContainsFileAssoc). Bidirectional — query both directions efficiently.
> - Inspired by Facebook's TAO (graph storage on MySQL).
>
> **Architecture layers:**
> ```
> SDK (Go/Python) → Cores (stateless routing) → Cache (95% hit rate) → Engines → MySQL (InnoDB/SSD)
> ```
>
> **Why MySQL (not Cassandra, not DynamoDB)?**
> - **Strong consistency required** — two users listing the same shared folder must see identical contents. ACID transactions are non-negotiable for metadata.
> - **Relational queries** — "list all files in folder X" is a range query. "Find all shared folders for user Y" is a join. These are natural in SQL, awkward in NoSQL.
> - **Sharding by namespace ID** — all files in a shared folder are on the same shard, enabling atomic operations (create file + update parent folder's `lastModified` in one transaction).
>
> **Scale:**
> - Thousands of MySQL shards
> - Several trillion entries
> - Millions of queries per second
> - 95% of reads served from cache
> - 10M cross-shard transactions/sec (modified 2-phase commit with copy-on-write optimization — reduced write amplification by 95%)
> - 5 nines availability (99.999%)
>
> **Contrast with WhatsApp (Cassandra):** WhatsApp chose Cassandra (AP, eventually consistent) because chat tolerates brief staleness. Dropbox needs strong consistency for metadata — two users must never see conflicting directory listings. Different consistency requirements → different database choices.
>
> See `05-metadata-service.md` for the complete deep dive.

---

### L5 vs L6 vs L7 — Phase 7 Rubric

| Aspect | L5 (SDE-2) | L6 (SDE-3) | L7 (Principal) |
|--------|------------|------------|-----------------|
| **Database Choice** | "Use a database" | MySQL for strong consistency, sharded by namespace, explains why not Cassandra | Discusses Edgestore graph model, cross-shard 2PC, cache hit optimization |
| **Consistency** | "Consistent" | Explains why strong consistency is required for metadata (two users, same folder) | Discusses split consistency model (metadata strong, blocks eventual) |
| **Scaling** | "Shard the DB" | Trillions of entries, millions of QPS, 95% cache hit rate | Discusses hot shard mitigation, Panda (next-gen key-value store), Alki (cold metadata) |

---

## PHASE 8: Wrap-Up (~3 min)

**Interviewer:**

What keeps you up at night with this system?

**Candidate:**

> Three things:
>
> **1. Reference counting bugs in garbage collection.**
> A block is stored once but referenced by potentially millions of file versions across millions of users. If the reference count is off by one — specifically, if it reaches zero while a reference still exists — and GC collects the block, that's irreversible data loss. No amount of replication or erasure coding can recover a block that was intentionally deleted. Mitigation: extensive testing, grace periods before deletion, "soft delete" (mark but don't physically delete for days), and dual-bookkeeping with periodic reconciliation.
>
> **2. Metadata hot shards from viral shared folders.**
> A shared folder that goes viral (e.g., a public shared link on Reddit with millions of viewers) creates a hot shard. All metadata queries for that folder hit one MySQL shard. Meanwhile, 99.9% of other shards are idle. Mitigation: aggressive caching (95% cache hit rate helps enormously), read replicas for popular namespaces, and eventually sharding within a namespace for extreme cases.
>
> **3. Conflict resolution at scale with many collaborators.**
> A shared folder with 50 active editors making frequent changes creates a high probability of conflicts. Each "conflicted copy" pollutes the folder with extra files. Users get confused, don't know which version is correct, and may lose track of changes. Mitigation: improve the client UX for conflict resolution (show diffs, suggest merges), increase sync speed to reduce the conflict window, and for specific file types (Office docs), integrate with the application's merge capabilities.

---

### L5 vs L6 vs L7 — Phase 8 Rubric

| Aspect | L5 (SDE-2) | L6 (SDE-3) | L7 (Principal) |
|--------|------------|------------|-----------------|
| **Operational Concerns** | "Server might go down" | Three specific scenarios with quantified impact and concrete mitigations | Adds: silent data corruption (bit rot) statistics, cross-region network partition handling, SMR drive write amplification gotchas |

---

## Final Architecture Summary

```
+======================== Dropbox Architecture ========================+
|                                                                        |
|  Clients (Desktop/Mobile/Web)                                         |
|  - Nucleus sync engine (Rust)                                         |
|  - File watcher → block hasher → delta sync                          |
|  - Long-poll for change notification                                  |
|  - Conflict detection (rev-based CAS → "conflicted copy")            |
|                                                                        |
|  ┌──── Load Balancer ────┐                                            |
|  v                       v                                            |
|  +-----------------+  +---------------------+  +------------------+   |
|  | API Servers     |  | Notification Service|  | Block Servers    |   |
|  | (file ops,      |  | (long-poll, change  |  | (dedup check,   |   |
|  |  upload commit) |  |  journal, cursor)   |  |  upload/download)|   |
|  +--------+--------+  +----------+----------+  +--------+--------+   |
|           |                      |                      |             |
|           v                      v                      v             |
|  +------------------+  +------------------+  +--------------------+   |
|  | Edgestore        |  | Change Journal   |  | Magic Pocket       |   |
|  | (Metadata)       |  | (per-namespace   |  | (Block Storage)    |   |
|  | MySQL sharded    |  |  event log)      |  | 3+ exabytes        |   |
|  | by namespace     |  |                  |  | Content-addressable|   |
|  | Trillions of     |  |                  |  | Erasure coded      |   |
|  | entries          |  |                  |  | 12 nines durability|   |
|  | 95% cache hit    |  |                  |  | Multi-zone         |   |
|  +------------------+  +------------------+  +--------------------+   |
|                                                                        |
|  +------------------+                                                  |
|  | Edge CDN         |  ← Shared link downloads, viral files           |
|  +------------------+                                                  |
|                                                                        |
|  Monitoring: Sync latency, Dedup ratio, Block integrity, Cache hits   |
|  Integrity: Background scrubbing (hash verification, parity repair)   |
+========================================================================+
```

---

## Supporting Deep-Dive Documents

| Doc | Topic | File |
|-----|-------|------|
| 02 | API Contracts | [02-api-contracts.md](./02-api-contracts.md) |
| 03 | File Sync Engine (Nucleus) | [03-file-sync-engine.md](./03-file-sync-engine.md) |
| 04 | Chunked Upload & Block Dedup | [04-chunked-upload-and-dedup.md](./04-chunked-upload-and-dedup.md) |
| 05 | Metadata Service (Edgestore) | [05-metadata-service.md](./05-metadata-service.md) |
| 06 | Block Storage (Magic Pocket) | [06-block-storage.md](./06-block-storage.md) |
| 07 | Sharing & Permissions | [07-sharing-and-permissions.md](./07-sharing-and-permissions.md) |
| 08 | Notification & Change Propagation | [08-notification-and-change-propagation.md](./08-notification-and-change-propagation.md) |
| 09 | Conflict Resolution | [09-conflict-resolution.md](./09-conflict-resolution.md) |
| 10 | Scaling & Reliability | [10-scaling-and-reliability.md](./10-scaling-and-reliability.md) |
| 11 | Design Trade-offs | [11-design-trade-offs.md](./11-design-trade-offs.md) |

---

## Verified Sources

- [Dropbox Tech: Inside the Magic Pocket](https://dropbox.tech/infrastructure/inside-the-magic-pocket) — Block storage architecture
- [Dropbox Tech: Rewriting the Heart of Our Sync Engine](https://dropbox.tech/infrastructure/rewriting-the-heart-of-our-sync-engine) — Nucleus (Rust sync engine)
- [Dropbox Tech: Streaming File Synchronization](https://dropbox.tech/infrastructure/streaming-file-synchronization) — Streaming sync optimization
- [Dropbox Tech: (Re)Introducing Edgestore](https://dropbox.tech/infrastructure/reintroducing-edgestore) — Metadata service architecture
- [Dropbox Tech: Cross-shard Transactions at 10M req/sec](https://dropbox.tech/infrastructure/cross-shard-transactions-at-10-million-requests-per-second) — Cross-shard 2PC
- [Dropbox Tech: Magic Pocket Cold Storage Optimization](https://dropbox.tech/infrastructure/how-we-optimized-magic-pocket-for-cold-storage) — Erasure coding tiers
- [Dropbox Tech: Seventh-Generation Server Hardware](https://dropbox.tech/infrastructure/seventh-generation-server-hardware) — Custom hardware specs
- [Dropbox Developers: Content Hash](https://www.dropbox.com/developers/reference/content-hash) — content_hash algorithm spec
- [Dropbox S-1 Filing](https://www.sec.gov/Archives/edgar/data/1467623/000119312518055809/d451946ds1.htm) — $74.6M cost savings figure
- [GeekWire: Dropbox Saved Almost $75 Million](https://www.geekwire.com/2018/dropbox-saved-almost-75-million-two-years-building-tech-infrastructure/) — S3 migration story
