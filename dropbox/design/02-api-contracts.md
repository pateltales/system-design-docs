# Deep Dive: Dropbox/Google Drive Platform APIs — Complete API Contracts

> **Context:** This document is the comprehensive API reference for a Dropbox-like cloud file storage & sync system. The interview simulation (Phase 3 of `01-interview-simulation.md`) covers a subset; this is the full catalog.

---

## PHASE: API Design Deep Dive

**Interviewer:**

Let's go through all the API surfaces of a Dropbox-like system. Walk me through each group, the key endpoints, and the design decisions behind them.

**Candidate:**

> I'll organize the APIs into 9 groups, from most critical (file upload — the revenue-generating path) to supporting services (webhooks, admin). For each endpoint I'll show the HTTP contract and highlight the design insight.
>
> A critical observation first: **Dropbox uses POST for almost everything**, including reads like download and list_folder. This is unusual — most REST APIs use GET for reads. The reason: Dropbox passes complex parameters in the request body (JSON), and GET requests can't have bodies per HTTP spec. Rather than encoding complex nested parameters in URL query strings, Dropbox chose POST universally. This is a pragmatic deviation from REST orthodoxy.

---

## 1. File Upload APIs — The Most Critical Path

> This is where Dropbox's core innovation lives: **block-level deduplication during upload**. The chunked upload protocol enables resumability, parallelism, and dedup — three things a simple PUT endpoint can't provide.

### 1.1 Simple Upload (Small Files < 150 MB)

```
POST /2/files/upload
Headers:
  Authorization: Bearer <access_token>
  Content-Type: application/octet-stream
  Dropbox-API-Arg: {
    "path": "/Documents/report.pdf",
    "mode": "add",           // "add" | "overwrite" | {"update": "<rev>"}
    "autorename": true,      // if conflict, rename to "report (1).pdf"
    "mute": false,           // if true, suppress desktop notifications
    "strict_conflict": false
  }
Body: <raw file bytes>

Response 200:
{
  "name": "report.pdf",
  "id": "id:a4ayc_80_OEAAAAAAAAAXw",
  "client_modified": "2026-02-20T12:00:00Z",
  "server_modified": "2026-02-20T12:00:01Z",
  "rev": "014830142489660000001",
  "size": 7945,
  "path_lower": "/documents/report.pdf",
  "path_display": "/Documents/report.pdf",
  "content_hash": "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"
}
```

**Design notes:**
- `mode: {"update": "<rev>"}` enables **optimistic concurrency** — upload fails if the file was modified since the client last saw it (CAS / Compare-And-Swap). This is how conflicts are detected.
- `content_hash` is returned so the client can verify integrity.
- Simple upload is a convenience — internally it goes through the same block storage pipeline, just without explicit chunking.

### 1.2 Chunked/Resumable Upload (Large Files)

> This is the critical path for large files and the foundation of block-level dedup. Three-phase protocol:

**Phase 1: Start a session**

```
POST /2/files/upload_session/start
Headers:
  Authorization: Bearer <access_token>
  Content-Type: application/octet-stream
  Dropbox-API-Arg: {
    "close": false,
    "content_hash": "sha256_of_first_chunk"  // optional, for server-side verification
  }
Body: <first chunk bytes, ~4 MB>

Response 200:
{
  "session_id": "AAAAAFRxNWVYR3VkUF9S"
}
```

**Phase 2: Append chunks**

```
POST /2/files/upload_session/append_v2
Headers:
  Authorization: Bearer <access_token>
  Content-Type: application/octet-stream
  Dropbox-API-Arg: {
    "cursor": {
      "session_id": "AAAAAFRxNWVYR3VkUF9S",
      "offset": 4194304    // byte offset = confirms where we are
    },
    "close": false
  }
Body: <next chunk bytes, ~4 MB>

Response 200: (empty body — success)
```

**Phase 3: Finish (commit)**

```
POST /2/files/upload_session/finish
Headers:
  Authorization: Bearer <access_token>
  Content-Type: application/octet-stream
  Dropbox-API-Arg: {
    "cursor": {
      "session_id": "AAAAAFRxNWVYR3VkUF9S",
      "offset": 12582912   // total bytes uploaded so far
    },
    "commit": {
      "path": "/Videos/presentation.mp4",
      "mode": {"update": "014830142489660000001"},
      "autorename": false,
      "mute": false
    }
  }
Body: <final chunk bytes (may be smaller than 4 MB)>

Response 200:
{
  "name": "presentation.mp4",
  "id": "id:b5bzc_91_PFBAAAAAAAAYx",
  "rev": "014830142489660000002",
  "size": 15728640,
  "content_hash": "9f86d081884c7d659a2feaa0c55ad015...",
  "path_display": "/Videos/presentation.mp4"
}
```

**Design insights:**

> - **Offset tracking**: The `offset` field in the cursor is a handshake — client tells server "I've sent X bytes so far," server verifies. If there's a mismatch (client crashed and retried with duplicate data), the server rejects. This prevents duplicate chunks.
> - **Resumability**: If the network drops after chunk 5 of 25, the client resumes from chunk 6 by calling `append_v2` with `offset = 5 * 4MB`. No re-upload of chunks 1-5.
> - **Block dedup happens at the finish step**: When the server receives `finish`, it looks at all the chunks, computes SHA-256 hashes, and stores only blocks it doesn't already have. The metadata entry (file → block list) is created atomically.
> - **Batch finish**: Dropbox also supports `upload_session/finish_batch` — commit multiple upload sessions at once. Useful when syncing many small files.

### 1.3 Block Dedup Check (Internal / Optimization)

```
// Internal API — client optimization to skip uploading blocks the server already has
POST /internal/blocks/has
Headers:
  Authorization: Bearer <access_token>
Body:
{
  "block_hashes": [
    "e3b0c44298fc1c149afbf4c8996fb924...",
    "9f86d081884c7d659a2feaa0c55ad015...",
    "d7a8fbb307d7809469ca9abcb0082e4f...",
    "5e884898da28047151d0e56f8dc62927..."
  ]
}

Response 200:
{
  "has": [true, true, false, true]
  // Server already has blocks 0, 1, and 3. Only block 2 needs uploading.
}
```

> **This is Dropbox's core innovation.** Before uploading, the client hashes each 4 MB block and asks the server which blocks it already has. If a user uploads a 1 GB file that's 99% identical to a file already on Dropbox (same OS installer, same PDF attachment shared by 1000 users), only the ~1% of unique blocks are actually transferred. **At Dropbox's scale, this reportedly saves 50-60% of total storage.**

**Interviewer:** How does the content_hash work exactly?

**Candidate:**

> The Dropbox content_hash algorithm:
> 1. Split file into **4 MB (4,194,304 byte) blocks**
> 2. Compute **SHA-256** of each block
> 3. **Concatenate** all block hashes (raw bytes, not hex)
> 4. Compute **SHA-256** of the concatenation
>
> ```
> Example: 10 MB file (3 blocks: 4MB + 4MB + 2MB)
>
> Block 0 (4 MB): SHA-256 → hash_0 (32 bytes)
> Block 1 (4 MB): SHA-256 → hash_1 (32 bytes)
> Block 2 (2 MB): SHA-256 → hash_2 (32 bytes)
>
> content_hash = SHA-256(hash_0 || hash_1 || hash_2)
>              = SHA-256(96 bytes)
>              = "a1b2c3d4..."
> ```
>
> **Why this scheme?**
> - **Incremental computation**: You can compute the hash block-by-block without loading the entire file into memory.
> - **Dedup-friendly**: The individual block hashes are exactly what's used for dedup lookups.
> - **Deterministic**: Same content always produces same hash, regardless of file name, path, or upload time.

---

## 2. File Download APIs

### 2.1 Download File

```
POST /2/files/download
Headers:
  Authorization: Bearer <access_token>
  Dropbox-API-Arg: {
    "path": "/Documents/report.pdf"
    // or "path": "rev:014830142489660000001"  (download specific version)
  }

Response 200:
Headers:
  Dropbox-API-Result: {
    "name": "report.pdf",
    "id": "id:a4ayc_80_OEAAAAAAAAAXw",
    "size": 7945,
    "rev": "014830142489660000001",
    "content_hash": "e3b0c44298fc1c149afbf4c8996fb924..."
  }
  Content-Type: application/octet-stream
Body: <raw file bytes>
```

> **Design note**: Metadata is returned in the `Dropbox-API-Result` header, not in the body (body is the file content). This avoids needing to parse a multipart response.

### 2.2 Download Folder as Zip

```
POST /2/files/download_zip
Headers:
  Authorization: Bearer <access_token>
  Dropbox-API-Arg: {
    "path": "/Project"
  }

Response 200:
Headers:
  Dropbox-API-Result: {"metadata": {"name": "Project", ...}}
  Content-Type: application/zip
Body: <zip file bytes>
```

### 2.3 Get Temporary Download Link

```
POST /2/files/get_temporary_link
Headers:
  Authorization: Bearer <access_token>
Body:
{
  "path": "/Photos/vacation.jpg"
}

Response 200:
{
  "metadata": { "name": "vacation.jpg", "size": 2048000, ... },
  "link": "https://dl.dropboxusercontent.com/apitl/1/AAC...signed-url..."
  // Link expires in 4 hours
}
```

> **Design note**: The temporary link is a **signed URL** that provides direct CDN access without requiring an API token. This is essential for:
> - Embedding images in web pages
> - Sharing with applications that can't handle OAuth
> - CDN delivery — the link points to edge servers, not origin

---

## 3. File Operations APIs

### 3.1 Delete (Soft Delete → Trash)

```
POST /2/files/delete_v2
Headers:
  Authorization: Bearer <access_token>
Body:
{
  "path": "/Documents/old_report.pdf"
}

Response 200:
{
  "metadata": {
    ".tag": "file",
    "name": "old_report.pdf",
    "id": "id:a4ayc_80_OEAAAAAAAAAXw",
    "path_display": "/Documents/old_report.pdf",
    "is_downloadable": true
    // File is now in trash, recoverable for 30/180 days
  }
}
```

### 3.2 Move / Rename (Metadata-Only Operation)

```
POST /2/files/move_v2
Headers:
  Authorization: Bearer <access_token>
Body:
{
  "from_path": "/Documents/report.pdf",
  "to_path": "/Archive/2026/report.pdf",
  "autorename": false,
  "allow_ownership_transfer": false
}

Response 200:
{
  "metadata": {
    "name": "report.pdf",
    "path_display": "/Archive/2026/report.pdf",
    "id": "id:a4ayc_80_OEAAAAAAAAAXw",  // Same ID! File identity preserved.
    "size": 7945,                          // Same size — no blocks copied
    "content_hash": "e3b0c44298fc1c149..."  // Same hash — no data moved
  }
}
```

> **Critical design insight**: Move is a **metadata-only operation** — it updates the `path` field in the metadata database. No blocks are copied. A 10 GB file moves instantaneously. This is only possible because files are identified by `fileId`, not by path. The block storage layer doesn't even know about file paths — it only knows block hashes.

### 3.3 Copy (Copy-on-Write)

```
POST /2/files/copy_v2
Headers:
  Authorization: Bearer <access_token>
Body:
{
  "from_path": "/Templates/invoice.xlsx",
  "to_path": "/Projects/Q1/invoice.xlsx"
}

Response 200:
{
  "metadata": {
    "name": "invoice.xlsx",
    "id": "id:NEW_UNIQUE_ID",          // New file ID
    "content_hash": "e3b0c44298fc1c149..." // Same hash as original!
    // New metadata entry pointing to the SAME block list
    // Zero additional storage until one copy is edited
  }
}
```

> **Copy-on-write**: Copying a file creates a new metadata entry that references the same blocks. Zero additional storage. When either copy is edited, only the edited blocks diverge. A 10 GB copy is instantaneous and free.

### 3.4 List Folder (Cursor-Based Pagination)

```
POST /2/files/list_folder
Headers:
  Authorization: Bearer <access_token>
Body:
{
  "path": "/Documents",
  "recursive": false,
  "include_deleted": false,
  "include_has_explicit_shared_members": true,
  "limit": 100
}

Response 200:
{
  "entries": [
    {
      ".tag": "file",
      "name": "report.pdf",
      "id": "id:a4ayc_80_OEAAAAAAAAAXw",
      "path_display": "/Documents/report.pdf",
      "size": 7945,
      "rev": "014830142489660000001",
      "content_hash": "e3b0c44298fc1c149..."
    },
    {
      ".tag": "folder",
      "name": "Subproject",
      "id": "id:c6czd_02_QGCAAAAAAAAZy",
      "path_display": "/Documents/Subproject"
    }
    // ... more entries
  ],
  "cursor": "AAGdm3pOcHFSR0JIUFBWT3...",  // Opaque cursor for pagination
  "has_more": true
}
```

**Continue pagination:**

```
POST /2/files/list_folder/continue
Body:
{
  "cursor": "AAGdm3pOcHFSR0JIUFBWT3..."
}

Response 200:
{
  "entries": [ /* next page */ ],
  "cursor": "BBHen4qPdIGTR1JKVQXVU4...",  // New cursor
  "has_more": false
}
```

> **Design note**: The cursor is **opaque** — clients must not parse or construct it. This gives the server freedom to encode pagination state however it wants (offset, page token, shard info) and change the encoding without breaking clients. This same cursor mechanism powers the sync protocol.

### 3.5 Search

```
POST /2/files/search_v2
Body:
{
  "query": "quarterly report",
  "options": {
    "path": "/Documents",
    "max_results": 20,
    "file_status": "active",        // "active" | "deleted"
    "filename_only": false,          // Search content too (if indexed)
    "file_extensions": ["pdf", "docx"]
  }
}

Response 200:
{
  "matches": [
    {
      "match_type": { ".tag": "filename" },
      "metadata": {
        ".tag": "metadata",
        "metadata": {
          "name": "Q1 Quarterly Report.pdf",
          "path_display": "/Documents/Q1 Quarterly Report.pdf",
          "size": 524288
        }
      }
    }
  ],
  "has_more": false,
  "cursor": "..."
}
```

---

## 4. Sync APIs — The Defining Feature

> The sync protocol is what separates Dropbox from plain cloud storage. Without this, you just have S3 with a nicer UI.

### 4.1 Long-Poll for Changes

```
POST https://notify.dropboxapi.com/2/files/list_folder/longpoll
Body:
{
  "cursor": "AAGdm3pOcHFSR0JIUFBWT3...",  // Last known cursor
  "timeout": 90   // Max seconds to wait (30-480, default 30)
}

Response 200 (after change occurs OR timeout):
{
  "changes": true,   // true = changes exist, go fetch them
  "backoff": null     // If non-null, wait this many seconds before next poll
}
```

> **Critical design decisions:**
>
> 1. **Two-phase protocol**: The longpoll only tells you "changes exist" — it doesn't include the actual changes. You then call `list_folder/continue` to get the details. Why? The longpoll server is stateless and lightweight — it just watches the change journal cursor. The heavier work of fetching and formatting change details is handled by the regular API servers.
>
> 2. **Long-polling, NOT WebSocket**: Dropbox chose long-polling over WebSocket. Why?
>    - **Firewall-friendly**: Long-poll is just an HTTP request with a long timeout. Many corporate firewalls block WebSocket (non-HTTP upgrade) but allow HTTP.
>    - **Stateless**: Any server can handle any poll — no connection affinity, no session stickiness. Load balancers route freely.
>    - **Simpler infrastructure**: No persistent connection management, no heartbeats, no reconnection logic on the server side.
>    - **Sufficient latency**: For file sync, a few seconds of delay is fine. Unlike chat (WhatsApp requires sub-100ms), file sync tolerance is measured in seconds.
>    - **Trade-off**: Higher latency than WebSocket (seconds vs milliseconds). But for file sync, this is acceptable.
>
> 3. **Separate hostname** (`notify.dropboxapi.com`): The longpoll service runs on dedicated infrastructure — it holds open connections for 30-90 seconds, which is very different from the short-lived API calls on the regular endpoint. Separate hostname = separate scaling, separate monitoring, separate failure domain.

### 4.2 The Complete Sync Loop

```
// The Dropbox sync protocol — runs continuously on the desktop client

// Step 1: Initial sync — get current state
cursor = call POST /2/files/list_folder { path: "" /* root */, recursive: true }
apply_changes(response.entries)

while (cursor.has_more) {
    cursor = call POST /2/files/list_folder/continue { cursor }
    apply_changes(response.entries)
}

// Step 2: Continuous sync loop
while (true) {
    // 2a: Long-poll — wait for changes
    poll_result = call POST /files/list_folder/longpoll { cursor, timeout: 90 }

    if (poll_result.backoff) {
        sleep(poll_result.backoff)  // Server is overloaded, back off
    }

    if (poll_result.changes) {
        // 2b: Fetch the actual changes
        response = call POST /2/files/list_folder/continue { cursor }
        apply_changes(response.entries)
        cursor = response.cursor  // Update cursor to latest position

        while (response.has_more) {
            response = call POST /2/files/list_folder/continue { cursor }
            apply_changes(response.entries)
            cursor = response.cursor
        }
    }

    // 2c: Loop back to longpoll with updated cursor
}
```

> **apply_changes()** for each entry:
> - If file is new or modified: compare block hashes with local → download only changed blocks → reconstruct file locally
> - If file is deleted: move local file to trash
> - If file is moved: update local path (no re-download needed)
> - If conflict detected: create conflicted copy locally

---

## 5. File Versioning APIs

### 5.1 List Revisions

```
POST /2/files/list_revisions
Body:
{
  "path": "/Documents/report.pdf",
  "mode": "path",    // "path" (follow path) | "id" (follow fileId)
  "limit": 10
}

Response 200:
{
  "is_deleted": false,
  "entries": [
    {
      "name": "report.pdf",
      "rev": "014830142489660000003",
      "size": 8192,
      "server_modified": "2026-02-20T14:00:00Z",
      "client_modified": "2026-02-20T13:59:55Z",
      "content_hash": "abc123..."
    },
    {
      "name": "report.pdf",
      "rev": "014830142489660000002",
      "size": 7945,
      "server_modified": "2026-02-19T10:00:00Z",
      "content_hash": "def456..."
    },
    {
      "name": "report.pdf",
      "rev": "014830142489660000001",
      "size": 5120,
      "server_modified": "2026-02-18T09:00:00Z",
      "content_hash": "ghi789..."
    }
  ]
}
```

### 5.2 Restore Previous Version

```
POST /2/files/restore
Body:
{
  "path": "/Documents/report.pdf",
  "rev": "014830142489660000001"   // Restore to this version
}

Response 200:
{
  "name": "report.pdf",
  "rev": "014830142489660000004",  // NEW rev (restore creates a new version)
  "size": 5120,
  "content_hash": "ghi789..."     // Same hash as the restored version
}
```

> **Design insight**: Restore is a **metadata-only operation** — it creates a new metadata entry pointing to the old version's block list. The blocks were never deleted (they're reference-counted). No data is copied. This is instantaneous regardless of file size.
>
> **Version retention**: Free accounts keep versions for 30 days. Business/Professional accounts keep versions for 180 days. After expiry, version metadata is deleted and block reference counts are decremented. Blocks with zero references are garbage-collected.

---

## 6. Sharing APIs

### 6.1 Create Shared Link

```
POST /2/sharing/create_shared_link_with_settings
Body:
{
  "path": "/Documents/report.pdf",
  "settings": {
    "requested_visibility": "public",      // "public" | "team_only" | "password"
    "link_password": null,
    "expires": "2026-03-20T00:00:00Z",
    "audience": "public",
    "access": "viewer",                     // "viewer" | "editor"
    "allow_download": true
  }
}

Response 200:
{
  "url": "https://www.dropbox.com/s/abc123/report.pdf?dl=0",
  "name": "report.pdf",
  "link_permissions": {
    "can_revoke": true,
    "resolved_visibility": "public",
    "effective_audience": "public"
  },
  "path_lower": "/documents/report.pdf",
  ".tag": "file"
}
```

### 6.2 Share Folder (Create Shared Namespace)

```
POST /2/sharing/share_folder
Body:
{
  "path": "/Projects/Q1",
  "acl_update_policy": "editors",     // Who can change sharing: "owner" | "editors"
  "member_policy": "anyone",           // Who can be added: "team" | "anyone"
  "shared_link_policy": "anyone",      // Who can create shared links: "anyone" | "members"
  "force_async": false
}

Response 200 (sync completion):
{
  ".tag": "complete",
  "shared_folder_id": "84528192421",
  "name": "Q1",
  "access_type": { ".tag": "owner" },
  "path_lower": "/projects/q1",
  "policy": {
    "acl_update_policy": "editors",
    "member_policy": "anyone",
    "shared_link_policy": "anyone"
  }
}
```

> **Design insight**: When a folder is shared, a **shared namespace** is created. This namespace:
> - Has its own **change journal** (sync cursor is per-namespace)
> - Has its own **ACL** (permission set)
> - Lives on a **single MySQL shard** (all metadata for the namespace is co-located for ACID)
> - Is **mounted** into each member's file tree at their chosen path

### 6.3 Add Folder Member

```
POST /2/sharing/add_folder_member
Body:
{
  "shared_folder_id": "84528192421",
  "members": [
    {
      "member": { ".tag": "email", "email": "bob@example.com" },
      "access_level": { ".tag": "editor" }
    },
    {
      "member": { ".tag": "email", "email": "charlie@example.com" },
      "access_level": { ".tag": "viewer" }
    }
  ],
  "quiet": false,       // Send email notification
  "custom_message": "Hey, I've shared the Q1 project folder with you."
}

Response 200: (empty — async operation, members notified)
```

### 6.4 List Folder Members

```
POST /2/sharing/list_folder_members
Body:
{
  "shared_folder_id": "84528192421",
  "limit": 100
}

Response 200:
{
  "users": [
    {
      "access_type": { ".tag": "owner" },
      "user": {
        "account_id": "dbid:AAH4f99T0taONIb-OurWxb...",
        "email": "alice@example.com",
        "display_name": "Alice Smith"
      },
      "is_inherited": false
    },
    {
      "access_type": { ".tag": "editor" },
      "user": {
        "account_id": "dbid:BBH4f99T0taONIb-OurWxc...",
        "email": "bob@example.com",
        "display_name": "Bob Jones"
      },
      "is_inherited": false
    }
  ],
  "cursor": "...",
  "has_more": false
}
```

---

## 7. Team / Admin APIs

### 7.1 Add Team Member

```
POST /2/team/members/add_v2
Body:
{
  "new_members": [
    {
      "member_email": "newuser@company.com",
      "member_given_name": "Jane",
      "member_surname": "Doe",
      "role": "member_only",           // "member_only" | "team_admin"
      "send_welcome_email": true
    }
  ]
}

Response 200:
{
  "complete": [
    {
      ".tag": "success",
      "profile": {
        "team_member_id": "dbmid:AABBB...",
        "email": "newuser@company.com",
        "status": { ".tag": "invited" },
        "membership_type": { ".tag": "full" }
      }
    }
  ]
}
```

### 7.2 Set Storage Quota

```
POST /2/team/member_space_limits/set_custom_quota
Body:
{
  "users_and_quotas": [
    {
      "user": { ".tag": "team_member_id", "team_member_id": "dbmid:AABBB..." },
      "quota_gb": 1000   // 1 TB quota for this user
    }
  ]
}
```

### 7.3 Get Team Storage Report

```
POST /2/team/reports/get_storage
Body:
{
  "start_date": "2026-01-01",
  "end_date": "2026-02-20"
}

Response 200:
{
  "start_date": "2026-01-01",
  "values": [
    {
      "date": "2026-01-01",
      "total_usage": 524288000000,    // 524 GB
      "shared_usage": 209715200000,   // 200 GB in shared folders
      "unshared_usage": 314572800000, // 314 GB in personal folders
      "member_count": 150
    }
    // ... daily entries
  ]
}
```

---

## 8. Webhook / Notification APIs

### 8.1 Register Webhook

```
// Configured via Dropbox App Console (not API)
// Webhook URL: https://myapp.example.com/dropbox-webhook

// Verification: Dropbox sends GET with a challenge parameter
GET https://myapp.example.com/dropbox-webhook?challenge=xyZ_aBcDeFgHiJk

// Your server must respond with the challenge:
Response 200:
Content-Type: text/plain
Body: xyZ_aBcDeFgHiJk
```

### 8.2 Webhook Notification (Server → Your App)

```
// When files change in any account that authorized your app:
POST https://myapp.example.com/dropbox-webhook
Headers:
  Content-Type: application/json
  X-Dropbox-Signature: HMAC_SHA256_of_body_with_app_secret
Body:
{
  "list_folder": {
    "accounts": [
      "dbid:AAH4f99T0taONIb...",   // Account IDs that have changes
      "dbid:BBH4f99T0taONIb..."
    ]
  },
  "delta": {
    "users": [12345678, 87654321]   // User IDs for team apps
  }
}
```

> **Design decisions:**
> - **Minimal payload**: Webhook only says "account X has changes" — not what changed. Your server must call `list_folder/continue` with the stored cursor to get actual changes. This keeps webhooks lightweight and prevents data leakage.
> - **At-least-once delivery**: Dropbox retries failed webhook deliveries with exponential backoff. Your handler must be idempotent.
> - **HMAC verification**: The `X-Dropbox-Signature` header lets you verify the webhook came from Dropbox (prevents spoofing).
> - **Batch notification**: Multiple accounts' changes are batched into a single webhook call (reduces HTTP overhead).

---

## 9. Content Hash Algorithm (Internal Reference)

```python
# Dropbox content_hash implementation

import hashlib

BLOCK_SIZE = 4 * 1024 * 1024  # 4 MB

def dropbox_content_hash(file_path):
    block_hashes = b""

    with open(file_path, "rb") as f:
        while True:
            block = f.read(BLOCK_SIZE)
            if not block:
                break
            block_hashes += hashlib.sha256(block).digest()

    return hashlib.sha256(block_hashes).hexdigest()

# Example: 10 MB file (3 blocks)
# Block 0 (4,194,304 bytes): SHA-256 → 32-byte hash_0
# Block 1 (4,194,304 bytes): SHA-256 → 32-byte hash_1
# Block 2 (1,611,392 bytes): SHA-256 → 32-byte hash_2
# content_hash = SHA-256(hash_0 + hash_1 + hash_2)
#              = SHA-256(96 bytes)
#              = "a1b2c3d4e5f6..."
```

> **Why this specific scheme?**
> 1. **Incremental**: Hash each block independently → you never need the whole file in memory
> 2. **Dedup-aligned**: Individual block hashes are exactly what's used for the block dedup lookup
> 3. **Verifiable**: Client computes content_hash locally, compares with server's value to verify integrity
> 4. **Deterministic**: Same file content → same hash, always. Independent of filename, path, upload time

---

## Contrast: Dropbox API vs Google Drive API vs S3 API

**Interviewer:** How do these APIs compare with Google Drive and S3?

**Candidate:**

> | Dimension | Dropbox API | Google Drive API | S3 API |
> |-----------|-------------|------------------|--------|
> | **Upload model** | Chunked + block dedup (only unique blocks uploaded) | Simple + resumable (whole-file, no block dedup) | Simple + multipart (parts for reliability, no dedup) |
> | **Sync mechanism** | Long-poll + cursor (built-in, first-class) | changes.list + pageToken + changes.watch (push via webhook) | S3 Event Notifications via SNS/SQS (eventual, coarse) |
> | **Change notification** | Long-polling (HTTP, firewall-friendly) | Webhook (push-based, requires public endpoint) | SNS/SQS events (async, infrastructure-level) |
> | **File identity** | fileId (survives move/rename) | fileId (same) | Key (path-based, rename = copy + delete) |
> | **Move/copy** | Metadata-only (O(1), instant) | Metadata-only (same) | Copy + delete (O(n), copies all data) |
> | **Versioning** | Built-in (30/180 days), revision-based | Built-in (revision-based, 100 versions retained) | Optional (bucket-level versioning, unlimited versions) |
> | **Conflict handling** | "Conflicted copy" (explicit, safe) | OT for Docs (real-time merge), last-writer-wins for files | Last-writer-wins (no conflict detection) |
> | **Namespace** | Hierarchical (true folders, ACL inheritance) | Flat with labels (files can be in multiple "folders") | Flat key-value (folders simulated via key prefix) |
> | **Sharing** | Folder-centric (share folders, not individual files) | File-centric (share individual files or folders) | IAM + bucket policies (programmatic, not user-facing) |
> | **Content hash** | Block-level content_hash (dedup-aligned) | MD5 checksum (whole-file) | ETag (MD5 for single-part, opaque for multipart) |
> | **Real-time collab** | No (conflicted copies) | Yes, for Google Docs/Sheets/Slides (OT/CRDT) | No (infrastructure service) |
> | **HTTP methods** | POST for everything (complex params in body) | RESTful (GET, POST, PATCH, DELETE) | RESTful (GET, PUT, DELETE, HEAD) |
>
> **Key philosophical differences:**
> - **Dropbox = file-centric sync engine**. The APIs are optimized for syncing binary files efficiently (block dedup, delta sync, resumability). Collaboration means "shared folders with conflict detection."
> - **Google Drive = document-centric collaboration platform**. The APIs are optimized for real-time collaboration on Google's native formats (Docs, Sheets, Slides). Regular file sync is secondary.
> - **S3 = infrastructure object store**. The APIs are optimized for programmatic blob storage (PUT/GET/DELETE). No sync, no collaboration, no user-facing features. S3 is what you build on, not what users interact with.

---

## Error Handling Patterns

**Interviewer:** How should clients handle errors?

**Candidate:**

> Dropbox uses a consistent error pattern across all endpoints:
>
> ```json
> // 409 Conflict — most common error
> {
>   "error_summary": "path/not_found/..",
>   "error": {
>     ".tag": "path",
>     "path": { ".tag": "not_found" }
>   }
> }
> ```
>
> **Key error categories:**
>
> | HTTP Status | Error | Meaning | Client Action |
> |------------|-------|---------|---------------|
> | 400 | Bad request | Malformed JSON, missing required field | Fix request, don't retry |
> | 401 | Invalid token | Token expired or revoked | Refresh OAuth token, retry |
> | 409 | Conflict | **Path not found**, **file conflict**, **insufficient space**, **too many files** | Depends on specific error |
> | 409 | `path/conflict/file` | File already exists at that path | Use `autorename: true` or handle conflict |
> | 409 | `path/insufficient_space` | User's quota exceeded | Alert user, don't retry |
> | 429 | Rate limited | Too many requests | Respect `Retry-After` header, exponential backoff |
> | 500 | Internal error | Server error | Retry with exponential backoff |
> | 503 | Service unavailable | Temporary outage | Retry with backoff |
>
> **Rate limiting**: Dropbox uses per-app and per-user rate limits. The `Retry-After` header tells you how long to wait. The desktop client uses exponential backoff with jitter for all retryable errors.
>
> **Idempotency**: Upload sessions are idempotent — re-sending the same chunk at the same offset is a no-op. This is critical for reliable retry without data corruption.

---

## API Authentication & Security

```
// OAuth 2.0 with PKCE (for mobile/desktop apps)
// Authorization code flow:

// Step 1: Redirect user to authorize
GET https://www.dropbox.com/oauth2/authorize
  ?client_id=APP_KEY
  &response_type=code
  &code_challenge=BASE64URL(SHA256(code_verifier))
  &code_challenge_method=S256
  &redirect_uri=https://myapp.com/callback
  &token_access_type=offline    // "offline" = get refresh_token

// Step 2: Exchange code for tokens
POST https://api.dropboxapi.com/oauth2/token
Body: code=AUTH_CODE&grant_type=authorization_code&code_verifier=VERIFIER&client_id=APP_KEY

Response:
{
  "access_token": "sl.ACCESS_TOKEN...",
  "token_type": "bearer",
  "expires_in": 14400,          // 4 hours
  "refresh_token": "REFRESH_TOKEN...",
  "scope": "files.content.read files.content.write files.metadata.read",
  "uid": "12345678",
  "account_id": "dbid:AAH4f99T0taONIb..."
}
```

> **Scoped access**: Dropbox supports granular OAuth scopes — an app can request only the permissions it needs (e.g., read-only access to a specific folder). This follows the principle of least privilege.

---

## L5 vs L6 vs L7 — API Design Rubric

| Aspect | L5 (SDE-2) | L6 (SDE-3) | L7 (Principal) |
|--------|------------|------------|-----------------|
| **Upload API** | Describes simple upload endpoint | Designs chunked upload with session management, explains resumability | Adds block dedup check before upload, calculates bandwidth savings, explains why POST not PUT for idempotent chunk uploads |
| **Sync Protocol** | "Use polling to check for changes" | Designs long-poll + cursor protocol, explains two-phase notification, contrasts with WebSocket | Explains separate hostname for notification service, calculates 1.17M polls/sec load, discusses backoff and backpressure |
| **Move/Copy** | Designs move as copy + delete | Recognizes move is metadata-only, explains fileId-based identity | Explains copy-on-write, reference-counted blocks, why this is O(1) regardless of file size |
| **Consistency** | "Use ETags for caching" | Designs rev-based optimistic concurrency (CAS), explains conflict detection | Explains split consistency model (strong metadata, eventual blocks), why client timestamps are unreliable (serverModified vs clientModified) |
| **Error Handling** | Returns 200/400/500 | Designs typed error responses (409 with specific tags), rate limiting with Retry-After | Designs idempotent operations, explains why upload sessions must be idempotent for retry safety |
| **Contrast** | None | Contrasts with S3 (no sync, no dedup) | Contrasts Dropbox (file-centric), Google Drive (document-centric), and S3 (infrastructure), explains how API design reflects product philosophy |

---

> **Summary:** The API design reflects Dropbox's core identity as a **sync engine**, not just cloud storage. The chunked upload with block dedup, the long-poll notification protocol, and the metadata-only move/copy operations are all designed around one principle: **minimize data transfer and storage while keeping all devices in sync.** Every API decision traces back to this.
