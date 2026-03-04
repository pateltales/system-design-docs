# API Contracts: Google Docs (Real-Time Collaborative Document Editor)

> **Companion document to:** [01-interview-simulation.md](01-interview-simulation.md)
> **Context:** Complete API specification for the collaborative editing system designed in the interview simulation.
> **Base URL:** `https://docs.googleapis.com/v1`
> **WebSocket URL:** `wss://docs-realtime.googleapis.com/v1`

---

## Table of Contents

1. [Authentication & Common Headers](#1-authentication--common-headers)
2. [Document CRUD APIs](#2-document-crud-apis)
3. [Real-Time Collaboration APIs (WebSocket)](#3-real-time-collaboration-apis-websocket)
4. [Document Content APIs (REST Fallback)](#4-document-content-apis-rest-fallback)
5. [Revision History APIs](#5-revision-history-apis)
6. [Commenting APIs](#6-commenting-apis)
7. [Sharing & Permission APIs](#7-sharing--permission-apis)
8. [Cursor & Presence APIs](#8-cursor--presence-apis-websocket)
9. [Suggestion Mode APIs](#9-suggestion-mode-apis)
10. [Comparison with Other Systems](#10-comparison-with-other-systems)
11. [Interview Subset: What to Focus On in 60 Minutes](#11-interview-subset-what-to-focus-on-in-60-minutes)

---

## 1. Authentication & Common Headers

Every REST request requires an OAuth 2.0 bearer token. WebSocket connections authenticate during the handshake.

**Common Request Headers:**

```
Authorization: Bearer <access_token>
Content-Type: application/json
Accept: application/json
X-Request-ID: <uuid>              # Idempotency / tracing
```

**Common Response Headers:**

```
Content-Type: application/json
X-Request-ID: <uuid>              # Echoed back for tracing
X-RateLimit-Remaining: 4500
X-RateLimit-Reset: 1740200000
```

**Common Error Response Format:**

```json
{
  "error": {
    "code": 403,
    "message": "The caller does not have permission to edit this document.",
    "status": "PERMISSION_DENIED",
    "details": [
      {
        "type": "ErrorInfo",
        "reason": "insufficientPermissions",
        "domain": "docs.googleapis.com",
        "metadata": {
          "requiredRole": "EDITOR",
          "currentRole": "VIEWER"
        }
      }
    ]
  }
}
```

**Standard Status Codes Used Across All Endpoints:**

| Code | Meaning | When Used |
|------|---------|-----------|
| `200` | OK | Successful GET, PATCH, PUT |
| `201` | Created | Successful POST that creates a resource |
| `204` | No Content | Successful DELETE |
| `400` | Bad Request | Malformed JSON, invalid field values, missing required fields |
| `401` | Unauthorized | Missing or expired OAuth token |
| `403` | Forbidden | Valid token but insufficient permissions (e.g., viewer trying to edit) |
| `404` | Not Found | Document, comment, revision, or permission ID does not exist |
| `409` | Conflict | Revision conflict (stale revision number), concurrent modification |
| `429` | Too Many Requests | Rate limit exceeded |
| `500` | Internal Server Error | Server-side failure |

---

## 2. Document CRUD APIs

These APIs manage document metadata -- title, owner, sharing settings, folder location. They do NOT serve document content (that is served via the content API or WebSocket channel).

### 2.1 Create Document

Creates a new blank document or a document from a template.

```
POST /documents
```

**Request Headers:**

```
Authorization: Bearer <access_token>
Content-Type: application/json
```

**Request Body:**

```json
{
  "title": "Q1 2026 Planning Doc",
  "folderId": "folder_abc123",
  "templateId": "template_meeting_notes"
}
```

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `title` | string | No | Document title. Defaults to `"Untitled document"`. |
| `folderId` | string | No | Parent folder ID in Google Drive. Defaults to user's root Drive folder. |
| `templateId` | string | No | Template ID to clone from. Omit for a blank document. |

**Response: `201 Created`**

```json
{
  "documentId": "doc_1a2b3c4d5e",
  "title": "Q1 2026 Planning Doc",
  "owner": {
    "userId": "user_alice_01",
    "email": "alice@company.com",
    "displayName": "Alice Chen"
  },
  "createdAt": "2026-02-21T10:00:00.000Z",
  "modifiedAt": "2026-02-21T10:00:00.000Z",
  "revision": 0,
  "folderId": "folder_abc123",
  "mimeType": "application/vnd.google-apps.document",
  "permissions": [
    {
      "permissionId": "perm_owner_01",
      "userId": "user_alice_01",
      "email": "alice@company.com",
      "role": "OWNER",
      "type": "user"
    }
  ],
  "link": "https://docs.google.com/document/d/doc_1a2b3c4d5e/edit"
}
```

**Error Responses:**

| Code | Reason |
|------|--------|
| `400` | Invalid `templateId` or `folderId` format |
| `401` | Missing or expired token |
| `403` | No permission to create documents in the specified folder |
| `404` | `templateId` or `folderId` not found |

---

### 2.2 Get Document Metadata

Returns document metadata (title, owner, last modified, sharing info). Does NOT return full document content -- use the content API or WebSocket for that.

```
GET /documents/{docId}
```

**Request Headers:**

```
Authorization: Bearer <access_token>
Accept: application/json
```

**Response: `200 OK`**

```json
{
  "documentId": "doc_1a2b3c4d5e",
  "title": "Q1 2026 Planning Doc",
  "owner": {
    "userId": "user_alice_01",
    "email": "alice@company.com",
    "displayName": "Alice Chen"
  },
  "createdAt": "2026-02-21T10:00:00.000Z",
  "modifiedAt": "2026-02-21T14:32:15.789Z",
  "revision": 1247,
  "folderId": "folder_abc123",
  "mimeType": "application/vnd.google-apps.document",
  "starred": false,
  "trashed": false,
  "permissions": [
    {
      "permissionId": "perm_owner_01",
      "userId": "user_alice_01",
      "email": "alice@company.com",
      "role": "OWNER",
      "type": "user"
    },
    {
      "permissionId": "perm_editor_02",
      "userId": "user_bob_02",
      "email": "bob@company.com",
      "role": "EDITOR",
      "type": "user"
    }
  ],
  "activeEditors": 3,
  "link": "https://docs.google.com/document/d/doc_1a2b3c4d5e/edit"
}
```

**Error Responses:**

| Code | Reason |
|------|--------|
| `401` | Missing or expired token |
| `403` | User has no access to this document |
| `404` | Document does not exist |

---

### 2.3 Update Document Metadata

Updates metadata fields -- rename, move to a different folder, star/unstar. Does NOT update document content.

```
PATCH /documents/{docId}
```

**Request Headers:**

```
Authorization: Bearer <access_token>
Content-Type: application/json
```

**Request Body:**

```json
{
  "title": "Q1 2026 Planning Doc (FINAL)",
  "folderId": "folder_xyz789",
  "starred": true
}
```

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `title` | string | No | New document title |
| `folderId` | string | No | Move to a different folder |
| `starred` | boolean | No | Star or unstar the document |

**Response: `200 OK`**

```json
{
  "documentId": "doc_1a2b3c4d5e",
  "title": "Q1 2026 Planning Doc (FINAL)",
  "modifiedAt": "2026-02-21T15:00:00.000Z",
  "folderId": "folder_xyz789",
  "starred": true
}
```

**Error Responses:**

| Code | Reason |
|------|--------|
| `400` | Empty title, invalid `folderId` |
| `401` | Missing or expired token |
| `403` | User is not an editor or owner |
| `404` | Document or target folder not found |

---

### 2.4 Delete Document (Soft Delete to Trash)

Moves the document to the trash. Does not permanently delete it. Trashed documents can be restored within 30 days.

```
DELETE /documents/{docId}
```

**Request Headers:**

```
Authorization: Bearer <access_token>
```

**Response: `204 No Content`**

No response body.

**Side Effects:**

- All active WebSocket connections to this document receive a `document_trashed` event.
- Active editors are notified and switched to read-only mode.
- The document remains accessible to the owner via the trash for 30 days.

**Error Responses:**

| Code | Reason |
|------|--------|
| `401` | Missing or expired token |
| `403` | Only the owner can trash a document |
| `404` | Document does not exist or is already trashed |

---

## 3. Real-Time Collaboration APIs (WebSocket)

This is the core innovation of Google Docs. The WebSocket channel carries:
- **Document operations** (insert, delete, format) -- the OT channel
- **Cursor/presence updates** -- where each user's cursor is
- **Server acknowledgments and broadcasts** -- transformed operations from other users

Unlike REST, this is a **persistent, stateful, bidirectional connection**. Each client maintains a local copy of the document and sends incremental operations -- NOT the full document on every keystroke.

### 3.1 WebSocket Handshake

```
WS /documents/{docId}/collaborate
```

**Connection URL:**

```
wss://docs-realtime.googleapis.com/v1/documents/doc_1a2b3c4d5e/collaborate
    ?token=<access_token>
    &clientId=<unique_client_id>
```

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `token` | string | Yes | OAuth 2.0 bearer token (passed as query param because WebSocket headers are limited) |
| `clientId` | string | Yes | Unique client session ID (UUID). Distinguishes multiple tabs from the same user. |

**HTTP Upgrade Request:**

```
GET /v1/documents/doc_1a2b3c4d5e/collaborate?token=<token>&clientId=<clientId> HTTP/1.1
Host: docs-realtime.googleapis.com
Upgrade: websocket
Connection: Upgrade
Sec-WebSocket-Key: dGhlIHNhbXBsZSBub25jZQ==
Sec-WebSocket-Version: 13
Sec-WebSocket-Protocol: google-docs-ot-v1
```

**HTTP Upgrade Response (success):**

```
HTTP/1.1 101 Switching Protocols
Upgrade: websocket
Connection: Upgrade
Sec-WebSocket-Accept: s3pPLMBiTxaQ9kYGzzhZRbK+xOo=
Sec-WebSocket-Protocol: google-docs-ot-v1
```

**Connection Rejection (insufficient permissions):**

```
HTTP/1.1 403 Forbidden
Content-Type: application/json

{
  "error": {
    "code": 403,
    "message": "Insufficient permissions. Viewer access does not allow collaboration.",
    "status": "PERMISSION_DENIED"
  }
}
```

Note: Viewers CAN connect to receive real-time updates (see other users' edits in real time), but they cannot send operations. The server rejects any operation messages from viewers.

### 3.2 Initial Sync Message (Server to Client)

Immediately after the WebSocket connection is established, the server sends an initialization message containing the current document state and connected users.

```json
{
  "type": "init",
  "documentId": "doc_1a2b3c4d5e",
  "revision": 1247,
  "content": {
    "ops": [
      {"insert": "Q1 2026 Planning Doc\n", "attributes": {"heading": 1, "bold": true}},
      {"insert": "\n"},
      {"insert": "Objectives\n", "attributes": {"heading": 2}},
      {"insert": "1. Launch new product line by March 15\n"},
      {"insert": "2. Hire 3 senior engineers\n"},
      {"insert": "3. Reduce P0 bug count to zero\n"}
    ]
  },
  "connectedUsers": [
    {
      "userId": "user_alice_01",
      "displayName": "Alice Chen",
      "color": "#4285F4",
      "cursorPosition": 42,
      "selectionStart": 42,
      "selectionEnd": 42,
      "lastActive": "2026-02-21T14:32:10.000Z"
    },
    {
      "userId": "user_bob_02",
      "displayName": "Bob Park",
      "color": "#EA4335",
      "cursorPosition": 105,
      "selectionStart": 100,
      "selectionEnd": 115,
      "lastActive": "2026-02-21T14:32:12.000Z"
    }
  ],
  "userPermission": "EDITOR",
  "serverTimestamp": "2026-02-21T14:32:15.000Z"
}
```

**Key fields:**
- `revision`: The server's current revision number. The client uses this as the base for its first operation.
- `content`: The full document content in the operational format (Delta-style). This is the document state at `revision`.
- `connectedUsers`: All users currently connected to this document, with their cursor positions and colors.
- `userPermission`: The connecting user's permission level (`OWNER`, `EDITOR`, `COMMENTER`, `VIEWER`).

### 3.3 The Three-State Client Protocol

The client follows the Jupiter-derived three-state protocol. Every WebSocket message falls into one of three categories:

| Message Direction | Message Type | Description |
|---|---|---|
| Client -> Server | `operation` | Client sends a local edit to the server |
| Server -> Client | `ack` | Server acknowledges the client's operation (with the assigned revision) |
| Server -> Client | `server_op` | Server broadcasts another user's (transformed) operation |

#### State 1: SYNCHRONIZED (no pending operations)

The client and server are in sync. The client's document state matches the server's. When the user makes an edit:

1. Apply the operation locally (optimistic).
2. Send the operation to the server.
3. Transition to AWAITING ACK.

#### State 2: AWAITING ACK (one operation in flight)

The client sent an operation and is waiting for the server's acknowledgment. If the user makes another edit:

1. Apply it locally (optimistic).
2. Buffer the operation (do NOT send it yet).
3. Transition to AWAITING ACK + BUFFER.

If the server sends an `ack`:

1. The in-flight operation is confirmed. Transition back to SYNCHRONIZED.

If the server sends a `server_op` (another user's edit):

1. Transform the incoming operation against the in-flight operation.
2. Apply the transformed operation locally.
3. Update the in-flight operation (transform it against the incoming operation).
4. Stay in AWAITING ACK.

#### State 3: AWAITING ACK + BUFFER (one in flight, one buffered)

The client has one operation in flight AND one buffered. If the user makes more edits:

1. Apply locally (optimistic).
2. **Compose** the new edit into the existing buffer (merge them into a single operation).
3. Stay in AWAITING ACK + BUFFER. The buffer does NOT grow unboundedly -- it is always composed into one operation.

If the server sends an `ack`:

1. The in-flight operation is confirmed.
2. Send the buffer as the new in-flight operation.
3. Transition to AWAITING ACK (buffer is now empty).

If the server sends a `server_op`:

1. Transform the incoming op against in-flight, then against buffer.
2. Transform in-flight and buffer against the incoming op.
3. Apply the doubly-transformed incoming op locally.
4. Stay in AWAITING ACK + BUFFER.

### 3.4 Client-to-Server Messages

#### Operation Message

Sent when the client submits a local edit to the server.

```json
{
  "type": "operation",
  "clientId": "client_uuid_abc",
  "revision": 1247,
  "operation": {
    "ops": [
      {"retain": 42},
      {"insert": "quarterly "},
      {"retain": 130}
    ]
  },
  "timestamp": "2026-02-21T14:33:00.123Z"
}
```

| Field | Type | Description |
|-------|------|-------------|
| `type` | string | Always `"operation"` |
| `clientId` | string | The client's unique session ID |
| `revision` | number | The server revision this operation is based on. The server uses this to determine which concurrent operations to transform against. |
| `operation.ops` | array | The compound operation as a sequence of retain/insert/delete components. The total input length must equal the document length at the specified revision. |
| `timestamp` | string | Client-side timestamp (for debugging, not for ordering -- the server imposes ordering). |

**Operation component types:**

| Component | Format | Meaning |
|---|---|---|
| Retain | `{"retain": 42}` | Skip 42 characters (leave unchanged) |
| Insert (plain) | `{"insert": "hello"}` | Insert text at current position |
| Insert (formatted) | `{"insert": "hello", "attributes": {"bold": true}}` | Insert formatted text |
| Delete | `{"delete": 5}` | Delete 5 characters from current position |
| Format | `{"retain": 10, "attributes": {"bold": true}}` | Apply formatting to 10 characters (retain + attributes = format change) |

**Example operations:**

Insert "X" at position 5 in a 10-character document:
```json
{"ops": [{"retain": 5}, {"insert": "X"}, {"retain": 5}]}
```

Delete characters 3-5 (2 chars) in a 10-character document:
```json
{"ops": [{"retain": 3}, {"delete": 2}, {"retain": 5}]}
```

Bold characters 5-10 in a 15-character document:
```json
{"ops": [{"retain": 5}, {"retain": 5, "attributes": {"bold": true}}, {"retain": 5}]}
```

Insert a newline and start a heading:
```json
{"ops": [{"retain": 50}, {"insert": "\n"}, {"insert": "New Section\n", "attributes": {"heading": 2}}, {"retain": 100}]}
```

### 3.5 Server-to-Client Messages

#### ACK Message

Sent by the server to confirm the client's operation was applied and assigned a revision number.

```json
{
  "type": "ack",
  "revision": 1248,
  "serverTimestamp": "2026-02-21T14:33:00.200Z"
}
```

| Field | Type | Description |
|-------|------|-------------|
| `type` | string | Always `"ack"` |
| `revision` | number | The revision number assigned to the client's operation. The client updates its confirmed revision to this value. |
| `serverTimestamp` | string | Server-side timestamp when the operation was applied. |

Upon receiving an ACK:
- If the client is in AWAITING ACK with no buffer: transition to SYNCHRONIZED.
- If the client is in AWAITING ACK + BUFFER: send the buffer as a new operation (with `revision` set to the ACK'd revision), transition to AWAITING ACK.

#### Server Operation Message

Sent by the server to broadcast another user's operation (already transformed by the server).

```json
{
  "type": "server_op",
  "userId": "user_bob_02",
  "displayName": "Bob Park",
  "revision": 1249,
  "operation": {
    "ops": [
      {"retain": 105},
      {"delete": 15},
      {"insert": "Complete onboarding for new hires"},
      {"retain": 47}
    ]
  },
  "serverTimestamp": "2026-02-21T14:33:01.456Z"
}
```

| Field | Type | Description |
|-------|------|-------------|
| `type` | string | Always `"server_op"` |
| `userId` | string | The user who made this edit |
| `displayName` | string | Display name (for UI -- cursor label) |
| `revision` | number | The revision number assigned to this operation |
| `operation.ops` | array | The operation, already transformed against concurrent operations by the server. The client must still transform this against any in-flight/buffered local operations. |
| `serverTimestamp` | string | When the server applied this operation |

**What the client does upon receiving a `server_op`:**

1. If in SYNCHRONIZED state: apply the operation directly to the local document. Update local revision.
2. If in AWAITING ACK state: transform the incoming op against the in-flight op, and vice versa. Apply the transformed incoming op. Update the in-flight op.
3. If in AWAITING ACK + BUFFER state: transform the incoming op against the in-flight op (yielding intermediate result), then transform the intermediate against the buffer, and vice versa. Apply the final transformed incoming op. Update both the in-flight and buffered ops.

#### Error Message

Sent by the server when an operation is rejected.

```json
{
  "type": "error",
  "code": "REVISION_MISMATCH",
  "message": "Operation based on revision 1240 but server is at 1250. Client must resync.",
  "currentRevision": 1250
}
```

| Error Code | Meaning | Client Action |
|---|---|---|
| `REVISION_MISMATCH` | Client's revision is too far behind (too many ops to transform) | Reload document from server |
| `INVALID_OPERATION` | Operation does not match document length or has invalid structure | Discard the operation, resync |
| `PERMISSION_DENIED` | User's role changed (e.g., downgraded to viewer while editing) | Switch to read-only mode |
| `DOCUMENT_DELETED` | Document was trashed by the owner | Show notification, close editor |

#### Permission Change Message

Pushed by the server when the user's permission level changes in real time.

```json
{
  "type": "permission_change",
  "newRole": "VIEWER",
  "previousRole": "EDITOR",
  "changedBy": {
    "userId": "user_alice_01",
    "displayName": "Alice Chen"
  },
  "serverTimestamp": "2026-02-21T14:35:00.000Z"
}
```

### 3.6 WebSocket Lifecycle

```
Client                              Server
  │                                    │
  │──── WS Upgrade Request ──────────▶│
  │                                    │ Authenticate token
  │                                    │ Check permissions
  │                                    │ Load document state
  │◀──── 101 Switching Protocols ─────│
  │                                    │
  │◀──── init (full doc + users) ─────│  ← Initial sync
  │                                    │
  │──── operation (rev=1247) ────────▶│  ← Client edits
  │                                    │ Transform against concurrent ops
  │                                    │ Apply to canonical state
  │                                    │ Append to operation log
  │◀──── ack (rev=1248) ──────────────│  ← Server confirms
  │                                    │
  │◀──── server_op (rev=1249) ────────│  ← Another user's edit
  │  Transform against in-flight       │
  │  Apply to local state              │
  │                                    │
  │──── cursor_update ───────────────▶│  ← Cursor moved
  │◀──── cursor_broadcast ────────────│  ← Other users' cursors
  │                                    │
  │◀──── ping ────────────────────────│  ← Heartbeat (every 30s)
  │──── pong ────────────────────────▶│
  │                                    │
  │──── close ───────────────────────▶│  ← Client disconnects
  │                                    │ Remove from connected users
  │                                    │ Broadcast user_left to others
```

### 3.7 Connection Recovery

If the WebSocket drops (network glitch, server failover), the client reconnects:

```
wss://docs-realtime.googleapis.com/v1/documents/doc_1a2b3c4d5e/collaborate
    ?token=<access_token>
    &clientId=<same_client_id>
    &lastRevision=1248
    &reconnect=true
```

The server responds with a **delta sync** instead of the full document:

```json
{
  "type": "reconnect_sync",
  "missedOperations": [
    {
      "userId": "user_bob_02",
      "revision": 1249,
      "operation": {"ops": [{"retain": 50}, {"insert": "new text"}, {"retain": 100}]}
    },
    {
      "userId": "user_carol_03",
      "revision": 1250,
      "operation": {"ops": [{"retain": 20}, {"delete": 5}, {"retain": 145}]}
    }
  ],
  "currentRevision": 1250,
  "connectedUsers": [ ... ]
}
```

The client applies the missed operations (transforming against any locally buffered operations), then resumes normal operation.

---

## 4. Document Content APIs (REST Fallback)

These REST endpoints serve document content for initial load (before WebSocket connects) and for export. They are NOT the real-time editing path.

### 4.1 Get Document Content

Returns the full document content as structured JSON. Used for initial document load, or by clients that do not support WebSocket (e.g., API consumers, bots).

```
GET /documents/{docId}/content
```

**Request Headers:**

```
Authorization: Bearer <access_token>
Accept: application/json
```

**Query Parameters:**

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `revision` | number | No | Return content at a specific revision. Omit for latest. |

**Response: `200 OK`**

```json
{
  "documentId": "doc_1a2b3c4d5e",
  "revision": 1247,
  "content": {
    "ops": [
      {"insert": "Q1 2026 Planning Doc\n", "attributes": {"heading": 1, "bold": true}},
      {"insert": "\n"},
      {"insert": "Objectives\n", "attributes": {"heading": 2}},
      {"insert": "1. Launch new product line by March 15\n"},
      {"insert": "2. Hire 3 senior engineers\n"},
      {"insert": "3. Reduce P0 bug count to zero\n"}
    ]
  },
  "title": "Q1 2026 Planning Doc",
  "wordCount": 28,
  "characterCount": 172,
  "lastModifiedBy": {
    "userId": "user_bob_02",
    "displayName": "Bob Park"
  },
  "modifiedAt": "2026-02-21T14:32:15.789Z"
}
```

**How the server constructs this response:**
1. Load the latest snapshot for `doc_1a2b3c4d5e`.
2. Replay all operations since the snapshot to reconstruct the current state.
3. Serialize the document state as a Delta-style JSON structure.
4. If a `revision` parameter was provided, replay only up to that revision.

**Error Responses:**

| Code | Reason |
|------|--------|
| `401` | Missing or expired token |
| `403` | User has no access to this document |
| `404` | Document does not exist, or specified `revision` does not exist |

---

### 4.2 Export Document

Exports the document in a specified format. This is an asynchronous operation for large documents -- the response may be a download URL rather than the file content.

```
POST /documents/{docId}/export
```

**Request Headers:**

```
Authorization: Bearer <access_token>
Content-Type: application/json
```

**Request Body:**

```json
{
  "format": "pdf",
  "revision": 1247,
  "options": {
    "includeComments": true,
    "includeSuggestions": false,
    "pageSize": "LETTER",
    "margins": {
      "top": "1in",
      "bottom": "1in",
      "left": "1in",
      "right": "1in"
    }
  }
}
```

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `format` | string | Yes | One of: `pdf`, `docx`, `html`, `txt`, `odt`, `rtf`, `epub` |
| `revision` | number | No | Export at a specific revision. Omit for latest. |
| `options.includeComments` | boolean | No | Include comments in the exported file. Default: `false`. |
| `options.includeSuggestions` | boolean | No | Include tracked suggestions. Default: `false`. |
| `options.pageSize` | string | No | Page size for PDF: `LETTER`, `A4`, `LEGAL`. Default: `LETTER`. |

**Response: `200 OK`**

```json
{
  "exportId": "export_xyz789",
  "format": "pdf",
  "status": "completed",
  "downloadUrl": "https://docs.googleapis.com/v1/exports/export_xyz789/download",
  "expiresAt": "2026-02-21T15:32:15.000Z",
  "fileSizeBytes": 245760
}
```

For large documents, the `status` may be `"processing"`:

```json
{
  "exportId": "export_xyz789",
  "format": "pdf",
  "status": "processing",
  "estimatedCompletionSeconds": 15,
  "pollUrl": "https://docs.googleapis.com/v1/exports/export_xyz789"
}
```

**Error Responses:**

| Code | Reason |
|------|--------|
| `400` | Invalid format or options |
| `401` | Missing or expired token |
| `403` | User has no access to this document |
| `404` | Document or specified revision not found |

---

## 5. Revision History APIs

Revisions are full snapshots of the document at specific points in time. Internally, revisions are reconstructed from the operation log, but the API presents them as opaque snapshots. Google Docs auto-saves named revisions periodically and on significant edits.

### 5.1 List Revisions

```
GET /documents/{docId}/revisions
```

**Request Headers:**

```
Authorization: Bearer <access_token>
Accept: application/json
```

**Query Parameters:**

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `pageSize` | number | No | Max results per page. Default: 50, Max: 200. |
| `pageToken` | string | No | Token for pagination (from previous response). |
| `startTime` | string | No | ISO 8601 timestamp. Only revisions after this time. |
| `endTime` | string | No | ISO 8601 timestamp. Only revisions before this time. |

**Response: `200 OK`**

```json
{
  "revisions": [
    {
      "revisionId": "rev_1247",
      "revision": 1247,
      "modifiedAt": "2026-02-21T14:32:15.789Z",
      "lastModifiedBy": {
        "userId": "user_bob_02",
        "displayName": "Bob Park"
      },
      "label": null,
      "isAutoSave": true,
      "changesSummary": "Edited section 'Objectives': added 3 bullet points"
    },
    {
      "revisionId": "rev_1200",
      "revision": 1200,
      "modifiedAt": "2026-02-21T13:00:00.000Z",
      "lastModifiedBy": {
        "userId": "user_alice_01",
        "displayName": "Alice Chen"
      },
      "label": "First Draft Complete",
      "isAutoSave": false,
      "changesSummary": "Named version: 'First Draft Complete'"
    },
    {
      "revisionId": "rev_1000",
      "revision": 1000,
      "modifiedAt": "2026-02-21T11:00:00.000Z",
      "lastModifiedBy": {
        "userId": "user_alice_01",
        "displayName": "Alice Chen"
      },
      "label": null,
      "isAutoSave": true,
      "changesSummary": "Created document and added initial structure"
    }
  ],
  "nextPageToken": "token_abc123",
  "totalRevisions": 47
}
```

**Note:** The API does NOT return one entry per operation (there could be millions). It returns **revision checkpoints** -- snapshots taken periodically (e.g., every 100 operations, or every few minutes of activity, or on explicit user action like "Name this version").

**Error Responses:**

| Code | Reason |
|------|--------|
| `401` | Missing or expired token |
| `403` | User must have at least `VIEWER` access. Revision history may be restricted by the owner. |
| `404` | Document not found |

---

### 5.2 Get Document at Specific Revision

Returns the full document content as it existed at a specific revision.

```
GET /documents/{docId}/revisions/{revId}
```

**Request Headers:**

```
Authorization: Bearer <access_token>
Accept: application/json
```

**Response: `200 OK`**

```json
{
  "revisionId": "rev_1200",
  "revision": 1200,
  "modifiedAt": "2026-02-21T13:00:00.000Z",
  "lastModifiedBy": {
    "userId": "user_alice_01",
    "displayName": "Alice Chen"
  },
  "label": "First Draft Complete",
  "content": {
    "ops": [
      {"insert": "Q1 2026 Planning Doc\n", "attributes": {"heading": 1, "bold": true}},
      {"insert": "\n"},
      {"insert": "Objectives\n", "attributes": {"heading": 2}},
      {"insert": "1. Launch new product line by March 15\n"}
    ]
  },
  "wordCount": 15,
  "characterCount": 82
}
```

**How the server constructs this:**
1. Find the closest snapshot at or before `rev_1200`.
2. Replay operations from the snapshot up to `rev_1200`.
3. Return the reconstructed document state.

**Error Responses:**

| Code | Reason |
|------|--------|
| `401` | Missing or expired token |
| `403` | Insufficient permissions to view revision history |
| `404` | Document or revision not found |

---

### 5.3 Restore to Previous Revision

Restores the document to the state at a specific revision. This does NOT rewrite history -- it creates a new revision that matches the old state. The operation log is preserved.

```
POST /documents/{docId}/revisions/{revId}/restore
```

**Request Headers:**

```
Authorization: Bearer <access_token>
Content-Type: application/json
```

**Request Body:**

```json
{
  "confirmRestore": true
}
```

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `confirmRestore` | boolean | Yes | Explicit confirmation to prevent accidental restores |

**Response: `200 OK`**

```json
{
  "documentId": "doc_1a2b3c4d5e",
  "restoredFromRevision": "rev_1200",
  "newRevision": "rev_1251",
  "revision": 1251,
  "modifiedAt": "2026-02-21T15:00:00.000Z",
  "restoredBy": {
    "userId": "user_alice_01",
    "displayName": "Alice Chen"
  }
}
```

**Side Effects:**
- All connected editors receive a `server_op` that transforms the document from the current state to the restored state.
- The operation is a single, potentially large operation: delete all current content, insert all restored content. This is OT-transformed against any concurrent edits.
- A notification is sent to all editors: "Alice Chen restored the document to a previous version."

**Error Responses:**

| Code | Reason |
|------|--------|
| `400` | `confirmRestore` is not `true` |
| `401` | Missing or expired token |
| `403` | Only editors and owners can restore revisions |
| `404` | Document or revision not found |
| `409` | Concurrent restore in progress -- another user is restoring simultaneously |

---

## 6. Commenting APIs

Comments are anchored to text ranges within the document. When the document text is edited, comment anchors must be adjusted -- this is a non-trivial problem handled by the OT engine.

### 6.1 Add Comment

Creates a new comment anchored to a text range in the document.

```
POST /documents/{docId}/comments
```

**Request Headers:**

```
Authorization: Bearer <access_token>
Content-Type: application/json
```

**Request Body:**

```json
{
  "content": "This objective seems too aggressive for Q1. Can we push to Q2?",
  "anchor": {
    "startIndex": 75,
    "endIndex": 112,
    "quotedText": "Launch new product line by March 15"
  },
  "revision": 1247
}
```

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `content` | string | Yes | The comment text. Supports @mentions: `"@bob@company.com what do you think?"` |
| `anchor.startIndex` | number | Yes | Character index where the anchor starts |
| `anchor.endIndex` | number | Yes | Character index where the anchor ends |
| `anchor.quotedText` | string | Yes | The text being commented on (used for display if the anchor becomes stale, and as a safety check that the anchor is correct) |
| `revision` | number | Yes | The document revision the anchor positions refer to. The server transforms the anchor to the current revision if the document has changed since. |

**Response: `201 Created`**

```json
{
  "commentId": "comment_abc123",
  "documentId": "doc_1a2b3c4d5e",
  "author": {
    "userId": "user_carol_03",
    "email": "carol@company.com",
    "displayName": "Carol Kim"
  },
  "content": "This objective seems too aggressive for Q1. Can we push to Q2?",
  "anchor": {
    "startIndex": 75,
    "endIndex": 112,
    "quotedText": "Launch new product line by March 15"
  },
  "status": "OPEN",
  "createdAt": "2026-02-21T14:35:00.000Z",
  "modifiedAt": "2026-02-21T14:35:00.000Z",
  "replies": [],
  "mentions": []
}
```

**Error Responses:**

| Code | Reason |
|------|--------|
| `400` | Empty content, invalid anchor indices, `quotedText` does not match the text at the specified range |
| `401` | Missing or expired token |
| `403` | User must have at least `COMMENTER` role |
| `404` | Document not found |
| `409` | `revision` is too old -- anchor positions cannot be reliably transformed |

---

### 6.2 List Comments

```
GET /documents/{docId}/comments
```

**Request Headers:**

```
Authorization: Bearer <access_token>
Accept: application/json
```

**Query Parameters:**

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `status` | string | No | Filter: `OPEN`, `RESOLVED`, `ALL`. Default: `OPEN`. |
| `pageSize` | number | No | Max results per page. Default: 50. |
| `pageToken` | string | No | Pagination token. |
| `includeReplies` | boolean | No | Include reply threads. Default: `true`. |

**Response: `200 OK`**

```json
{
  "comments": [
    {
      "commentId": "comment_abc123",
      "author": {
        "userId": "user_carol_03",
        "displayName": "Carol Kim"
      },
      "content": "This objective seems too aggressive for Q1. Can we push to Q2?",
      "anchor": {
        "startIndex": 75,
        "endIndex": 112,
        "quotedText": "Launch new product line by March 15"
      },
      "status": "OPEN",
      "createdAt": "2026-02-21T14:35:00.000Z",
      "modifiedAt": "2026-02-21T14:35:00.000Z",
      "replies": [
        {
          "replyId": "reply_def456",
          "author": {
            "userId": "user_alice_01",
            "displayName": "Alice Chen"
          },
          "content": "Good point. Let me check with the PM team.",
          "createdAt": "2026-02-21T14:40:00.000Z"
        }
      ]
    },
    {
      "commentId": "comment_xyz789",
      "author": {
        "userId": "user_bob_02",
        "displayName": "Bob Park"
      },
      "content": "We should add a metric for customer satisfaction here.",
      "anchor": {
        "startIndex": 150,
        "endIndex": 172,
        "quotedText": "Reduce P0 bug count to zero"
      },
      "status": "OPEN",
      "createdAt": "2026-02-21T14:38:00.000Z",
      "modifiedAt": "2026-02-21T14:38:00.000Z",
      "replies": []
    }
  ],
  "nextPageToken": null,
  "totalComments": 2
}
```

**Error Responses:**

| Code | Reason |
|------|--------|
| `401` | Missing or expired token |
| `403` | User must have at least `VIEWER` access |
| `404` | Document not found |

---

### 6.3 Reply to Comment

```
POST /documents/{docId}/comments/{commentId}/reply
```

**Request Headers:**

```
Authorization: Bearer <access_token>
Content-Type: application/json
```

**Request Body:**

```json
{
  "content": "I checked with PM -- they agree we should push to Q2. Updating the timeline."
}
```

**Response: `201 Created`**

```json
{
  "replyId": "reply_ghi789",
  "commentId": "comment_abc123",
  "author": {
    "userId": "user_alice_01",
    "displayName": "Alice Chen"
  },
  "content": "I checked with PM -- they agree we should push to Q2. Updating the timeline.",
  "createdAt": "2026-02-21T15:00:00.000Z"
}
```

**Error Responses:**

| Code | Reason |
|------|--------|
| `400` | Empty content |
| `401` | Missing or expired token |
| `403` | User must have at least `COMMENTER` role |
| `404` | Document or comment not found |

---

### 6.4 Resolve Comment

Marks a comment thread as resolved. Resolved comments are hidden from the default view but not deleted.

```
PUT /documents/{docId}/comments/{commentId}/resolve
```

**Request Headers:**

```
Authorization: Bearer <access_token>
Content-Type: application/json
```

**Request Body:**

```json
{
  "status": "RESOLVED"
}
```

**Response: `200 OK`**

```json
{
  "commentId": "comment_abc123",
  "status": "RESOLVED",
  "resolvedBy": {
    "userId": "user_alice_01",
    "displayName": "Alice Chen"
  },
  "resolvedAt": "2026-02-21T15:05:00.000Z"
}
```

To reopen a resolved comment, send `"status": "OPEN"`.

**Error Responses:**

| Code | Reason |
|------|--------|
| `400` | Invalid status value |
| `401` | Missing or expired token |
| `403` | User must have at least `COMMENTER` role to resolve |
| `404` | Document or comment not found |

---

### 6.5 Comment Anchor Adjustment Under Edits

This is a critical design concern. When document text is edited, comment anchors (startIndex, endIndex) must be adjusted using the same OT transform logic used for operations.

**Scenario 1: Text inserted before the comment anchor**

```
Before: "ABCDEFGHIJ" (10 chars)
Comment anchored to chars 5-8: "FGHI"

Edit: Insert "XX" at position 2

After:  "ABXXCDEFGHIJ" (12 chars)
Anchor shifts: 5→7, 8→10. Comment now anchors to chars 7-10: "FGHI" (same text)
```

**Scenario 2: Text inserted within the comment anchor**

```
Before: "ABCDEFGHIJ" (10 chars)
Comment anchored to chars 5-8: "FGHI"

Edit: Insert "XX" at position 6

After:  "ABCDEFXXGHIJ" (12 chars)
Anchor expands: 5-8 → 5-10. Comment now anchors to chars 5-10: "FXXGHI"
The comment range expands to include the inserted text.
```

**Scenario 3: Commented text is partially deleted**

```
Before: "ABCDEFGHIJ" (10 chars)
Comment anchored to chars 5-8: "FGHI"

Edit: Delete chars 6-9 (deletes "GHI" and "J")

After:  "ABCDEF" (6 chars)
Anchor shrinks: 5-8 → 5-6. Comment now anchors to chars 5-6: "F"
The comment remains but covers less text.
```

**Scenario 4: All commented text is deleted**

```
Before: "ABCDEFGHIJ" (10 chars)
Comment anchored to chars 5-8: "FGHI"

Edit: Delete chars 4-9 (deletes "EFGHI" and "J")

After:  "ABCD" (4 chars)
Anchor becomes degenerate: startIndex == endIndex == 4.
Comment is marked as orphaned. UI shows: "The text you commented on was deleted."
The comment is NOT deleted -- it remains visible to the author and others.
```

---

## 7. Sharing & Permission APIs

Manage who can access a document and at what level. Permission levels: **Owner > Editor > Commenter > Viewer**.

### 7.1 Share Document (Add Permission)

```
POST /documents/{docId}/permissions
```

**Request Headers:**

```
Authorization: Bearer <access_token>
Content-Type: application/json
```

**Request Body:**

```json
{
  "grants": [
    {
      "email": "bob@company.com",
      "role": "EDITOR",
      "type": "user"
    },
    {
      "email": "carol@company.com",
      "role": "COMMENTER",
      "type": "user"
    },
    {
      "email": "engineering@company.com",
      "role": "VIEWER",
      "type": "group"
    }
  ],
  "sendNotification": true,
  "message": "Please review the Q1 planning doc. Feedback due by Friday."
}
```

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `grants` | array | Yes | One or more permission grants |
| `grants[].email` | string | Yes | Email of the user or group |
| `grants[].role` | string | Yes | `OWNER`, `EDITOR`, `COMMENTER`, or `VIEWER` |
| `grants[].type` | string | Yes | `user`, `group`, or `domain` |
| `sendNotification` | boolean | No | Send an email notification. Default: `true`. |
| `message` | string | No | Custom message included in the notification email |

**Response: `201 Created`**

```json
{
  "permissions": [
    {
      "permissionId": "perm_editor_02",
      "email": "bob@company.com",
      "displayName": "Bob Park",
      "role": "EDITOR",
      "type": "user",
      "grantedAt": "2026-02-21T15:10:00.000Z",
      "grantedBy": {
        "userId": "user_alice_01",
        "displayName": "Alice Chen"
      }
    },
    {
      "permissionId": "perm_commenter_03",
      "email": "carol@company.com",
      "displayName": "Carol Kim",
      "role": "COMMENTER",
      "type": "user",
      "grantedAt": "2026-02-21T15:10:00.000Z",
      "grantedBy": {
        "userId": "user_alice_01",
        "displayName": "Alice Chen"
      }
    },
    {
      "permissionId": "perm_viewer_group_04",
      "email": "engineering@company.com",
      "displayName": "Engineering Team",
      "role": "VIEWER",
      "type": "group",
      "grantedAt": "2026-02-21T15:10:00.000Z",
      "grantedBy": {
        "userId": "user_alice_01",
        "displayName": "Alice Chen"
      }
    }
  ]
}
```

**Error Responses:**

| Code | Reason |
|------|--------|
| `400` | Invalid email format, invalid role |
| `401` | Missing or expired token |
| `403` | Only owner (and editors, if allowed by org policy) can share documents |
| `404` | Document not found |

---

### 7.2 Update Permission

Change a user's access level (e.g., upgrade viewer to editor, or downgrade editor to commenter).

```
PATCH /documents/{docId}/permissions/{permissionId}
```

**Request Headers:**

```
Authorization: Bearer <access_token>
Content-Type: application/json
```

**Request Body:**

```json
{
  "role": "VIEWER"
}
```

**Response: `200 OK`**

```json
{
  "permissionId": "perm_editor_02",
  "email": "bob@company.com",
  "displayName": "Bob Park",
  "role": "VIEWER",
  "previousRole": "EDITOR",
  "modifiedAt": "2026-02-21T15:15:00.000Z",
  "modifiedBy": {
    "userId": "user_alice_01",
    "displayName": "Alice Chen"
  }
}
```

**Side Effects:**

If the user is currently connected via WebSocket and their role is downgraded:
1. Permission change is written to the database (strongly consistent -- Spanner).
2. The server pushes a `permission_change` message over the user's WebSocket.
3. The user's client transitions to the appropriate mode (e.g., read-only for viewers).
4. Any unsent pending operations from the user are discarded.
5. In-flight operations are rejected by the server.

**Error Responses:**

| Code | Reason |
|------|--------|
| `400` | Invalid role value, cannot change owner's role |
| `401` | Missing or expired token |
| `403` | Only the owner can change permissions |
| `404` | Document or permission not found |

---

### 7.3 Revoke Permission

Remove a user's access to the document entirely.

```
DELETE /documents/{docId}/permissions/{permissionId}
```

**Request Headers:**

```
Authorization: Bearer <access_token>
```

**Response: `204 No Content`**

**Side Effects:**
- If the user is connected via WebSocket, they receive a `permission_change` with `newRole: null` and are disconnected.
- The user's presence is removed from the connected users list.
- Other connected users are notified of the user's departure.

**Error Responses:**

| Code | Reason |
|------|--------|
| `401` | Missing or expired token |
| `403` | Only the owner can revoke permissions. Cannot revoke the owner's own permission. |
| `404` | Document or permission not found |

---

### 7.4 Create Shareable Link

Generate a link that grants access to anyone who has it.

```
POST /documents/{docId}/link
```

**Request Headers:**

```
Authorization: Bearer <access_token>
Content-Type: application/json
```

**Request Body:**

```json
{
  "role": "COMMENTER",
  "scope": "ANYONE_WITH_LINK",
  "expiresAt": "2026-03-21T00:00:00.000Z"
}
```

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `role` | string | Yes | Access level: `VIEWER`, `COMMENTER`, or `EDITOR` |
| `scope` | string | Yes | `ANYONE_WITH_LINK` (public), `ANYONE_IN_ORGANIZATION` (domain-restricted), or `SPECIFIC_USERS` (link works but only for invited users) |
| `expiresAt` | string | No | ISO 8601 timestamp. Link expires after this time. Omit for no expiration. |

**Response: `201 Created`**

```json
{
  "linkId": "link_share_001",
  "url": "https://docs.google.com/document/d/doc_1a2b3c4d5e/edit?usp=sharing",
  "role": "COMMENTER",
  "scope": "ANYONE_WITH_LINK",
  "expiresAt": "2026-03-21T00:00:00.000Z",
  "createdAt": "2026-02-21T15:20:00.000Z",
  "createdBy": {
    "userId": "user_alice_01",
    "displayName": "Alice Chen"
  }
}
```

**Error Responses:**

| Code | Reason |
|------|--------|
| `400` | Invalid role or scope. `EDITOR` links may be restricted by org policy. |
| `401` | Missing or expired token |
| `403` | Only owner (or editors, depending on org policy) can create shareable links |
| `404` | Document not found |

---

## 8. Cursor & Presence APIs (WebSocket)

Cursor and presence information is exchanged over the **same WebSocket connection** used for document operations (Section 3). There are no separate REST endpoints for presence -- it is entirely ephemeral and not persisted.

### 8.1 Cursor Update (Client to Server)

Sent by the client whenever the user's cursor position or selection changes. Throttled to 10-20 updates per second on the client side.

```json
{
  "type": "cursor_update",
  "clientId": "client_uuid_abc",
  "cursorPosition": 142,
  "selectionStart": 130,
  "selectionEnd": 142,
  "revision": 1248
}
```

| Field | Type | Description |
|-------|------|-------------|
| `type` | string | Always `"cursor_update"` |
| `clientId` | string | The client session ID |
| `cursorPosition` | number | Character index of the cursor (caret position) |
| `selectionStart` | number | Start of selection range. Same as `cursorPosition` if no selection. |
| `selectionEnd` | number | End of selection range. Same as `cursorPosition` if no selection. |
| `revision` | number | The document revision these positions refer to. The server transforms positions to the current revision before broadcasting. |

### 8.2 Cursor Broadcast (Server to Client)

The server broadcasts cursor positions to all other connected clients. Positions are transformed to each client's local document state.

```json
{
  "type": "cursor_broadcast",
  "cursors": [
    {
      "userId": "user_alice_01",
      "displayName": "Alice Chen",
      "color": "#4285F4",
      "cursorPosition": 142,
      "selectionStart": 130,
      "selectionEnd": 142,
      "lastActive": "2026-02-21T14:33:05.000Z"
    },
    {
      "userId": "user_bob_02",
      "displayName": "Bob Park",
      "color": "#EA4335",
      "cursorPosition": 210,
      "selectionStart": 210,
      "selectionEnd": 210,
      "lastActive": "2026-02-21T14:33:04.500Z"
    }
  ]
}
```

**Cursor position stability under edits:**

When a `server_op` is applied, all cursor positions from other users must be transformed using the same OT logic:

```
If server_op is insert("XX", pos=100):
  Cursor at pos 50  → stays at pos 50  (insert is after cursor)
  Cursor at pos 150 → shifts to pos 152 (insert is before cursor, shift right by 2)
  Cursor at pos 100 → shifts to pos 102 (insert is at cursor position, cursor moves after inserted text)

If server_op is delete(pos=100, count=5):
  Cursor at pos 50  → stays at pos 50  (delete is after cursor)
  Cursor at pos 150 → shifts to pos 145 (delete is before cursor, shift left by 5)
  Cursor at pos 102 → moves to pos 100 (cursor was within deleted range, snaps to deletion point)
```

### 8.3 User Joined / User Left

Broadcast when a user connects to or disconnects from the document.

**User joined:**

```json
{
  "type": "user_joined",
  "user": {
    "userId": "user_dave_04",
    "displayName": "Dave Wilson",
    "email": "dave@company.com",
    "color": "#34A853",
    "role": "EDITOR"
  },
  "connectedUserCount": 4,
  "serverTimestamp": "2026-02-21T14:36:00.000Z"
}
```

**User left:**

```json
{
  "type": "user_left",
  "userId": "user_dave_04",
  "displayName": "Dave Wilson",
  "reason": "disconnected",
  "connectedUserCount": 3,
  "serverTimestamp": "2026-02-21T14:50:00.000Z"
}
```

| `reason` value | Meaning |
|---|---|
| `"disconnected"` | Network drop or closed tab |
| `"navigated_away"` | User navigated to another document |
| `"permission_revoked"` | User's access was revoked while connected |
| `"idle_timeout"` | User was idle for too long (e.g., 30 minutes) |

### 8.4 Heartbeat

The server sends periodic heartbeats to keep the WebSocket alive and detect dead connections.

**Server ping:**
```json
{
  "type": "ping",
  "serverTimestamp": "2026-02-21T14:33:30.000Z"
}
```

**Client pong:**
```json
{
  "type": "pong",
  "clientId": "client_uuid_abc",
  "timestamp": "2026-02-21T14:33:30.050Z"
}
```

If the server does not receive a pong within 30 seconds, it considers the client disconnected and broadcasts a `user_left` event.

### 8.5 User Color Assignment

Colors are assigned from a predefined palette of 20 high-contrast colors. Assignment rules:
1. When a user connects, the server assigns the first unused color from the palette.
2. If all 20 colors are in use (20+ editors), colors are recycled with a slight hue shift.
3. Colors persist within a session but may change across sessions (this is acceptable -- users identify collaborators by name, not color).

The color palette (Google Docs approximation):

```
#4285F4 (Blue)       #EA4335 (Red)        #34A853 (Green)
#FBBC05 (Yellow)     #FF6D01 (Orange)     #46BDC6 (Teal)
#7B1FA2 (Purple)     #C2185B (Pink)       #00897B (Dark Teal)
#5C6BC0 (Indigo)     #F4511E (Deep Orange) #00ACC1 (Cyan)
#8D6E63 (Brown)      #757575 (Grey)       #9E9D24 (Lime)
#1B5E20 (Dark Green) #0D47A1 (Dark Blue)  #BF360C (Dark Red)
#4A148C (Dark Purple) #006064 (Dark Cyan)
```

---

## 9. Suggestion Mode APIs

Suggestion mode (tracked changes) allows commenters and editors to propose edits without directly modifying the document. Suggestions are displayed as tracked changes: strikethrough for deletions, colored text for insertions. They require approval (accept/reject) by an editor or owner.

### 9.1 Create Suggestion

Submit an edit as a suggestion (tracked change) rather than a direct edit.

```
POST /documents/{docId}/suggestions
```

**Request Headers:**

```
Authorization: Bearer <access_token>
Content-Type: application/json
```

**Request Body:**

```json
{
  "operation": {
    "ops": [
      {"retain": 75},
      {"delete": 37},
      {"insert": "Launch MVP by end of Q2 2026"},
      {"retain": 60}
    ]
  },
  "revision": 1247,
  "comment": "Adjusted timeline based on PM feedback"
}
```

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `operation` | object | Yes | The proposed edit as an OT operation. Same format as real-time operations. |
| `revision` | number | Yes | The document revision this operation is based on. The server transforms it to the current revision. |
| `comment` | string | No | Optional note explaining the suggestion. |

**Response: `201 Created`**

```json
{
  "suggestionId": "suggestion_001",
  "documentId": "doc_1a2b3c4d5e",
  "author": {
    "userId": "user_carol_03",
    "displayName": "Carol Kim"
  },
  "operation": {
    "ops": [
      {"retain": 75},
      {"delete": 37},
      {"insert": "Launch MVP by end of Q2 2026"},
      {"retain": 60}
    ]
  },
  "comment": "Adjusted timeline based on PM feedback",
  "status": "PENDING",
  "createdAt": "2026-02-21T15:30:00.000Z",
  "affectedRange": {
    "startIndex": 75,
    "endIndex": 112,
    "originalText": "Launch new product line by March 15\n",
    "suggestedText": "Launch MVP by end of Q2 2026"
  }
}
```

**How suggestions are displayed:**

The document rendering engine shows suggestion markup inline:

```
Before:  "Launch new product line by March 15"
Display: "Launch new product line by March 15" (strikethrough, red)
         "Launch MVP by end of Q2 2026" (green, underline, with Carol's name)
```

Suggestions are NOT applied to the canonical document state. They exist as separate overlay operations that are rendered on top of the document.

**Side Effects:**
- All connected clients receive a `suggestion_created` WebSocket event and render the suggestion markup.
- If the text range affected by the suggestion is subsequently edited by another user, the suggestion's operation must be transformed (the suggestion tracks the same text even as it moves).

**Error Responses:**

| Code | Reason |
|------|--------|
| `400` | Invalid operation format, operation length does not match document |
| `401` | Missing or expired token |
| `403` | User must have at least `COMMENTER` role |
| `404` | Document not found |
| `409` | Revision too stale to transform |

---

### 9.2 Accept Suggestion

Apply the suggestion's operation to the canonical document, making it a permanent edit.

```
POST /documents/{docId}/suggestions/{suggestionId}/accept
```

**Request Headers:**

```
Authorization: Bearer <access_token>
Content-Type: application/json
```

**Request Body (optional):**

```json
{
  "comment": "Approved -- timeline updated per PM review."
}
```

**Response: `200 OK`**

```json
{
  "suggestionId": "suggestion_001",
  "status": "ACCEPTED",
  "acceptedBy": {
    "userId": "user_alice_01",
    "displayName": "Alice Chen"
  },
  "acceptedAt": "2026-02-21T15:45:00.000Z",
  "appliedRevision": 1255,
  "comment": "Approved -- timeline updated per PM review."
}
```

**Side Effects:**
- The suggestion's operation is applied to the canonical document as a new operation in the operation log.
- All connected clients receive the operation as a `server_op`.
- The suggestion markup is removed from the document rendering.

**Error Responses:**

| Code | Reason |
|------|--------|
| `401` | Missing or expired token |
| `403` | Only editors and owners can accept suggestions |
| `404` | Document or suggestion not found |
| `409` | Suggestion conflicts with concurrent edits and cannot be applied cleanly |

---

### 9.3 Reject Suggestion

Discard the suggestion without applying it.

```
POST /documents/{docId}/suggestions/{suggestionId}/reject
```

**Request Headers:**

```
Authorization: Bearer <access_token>
Content-Type: application/json
```

**Request Body (optional):**

```json
{
  "comment": "We decided to keep the original timeline."
}
```

**Response: `200 OK`**

```json
{
  "suggestionId": "suggestion_001",
  "status": "REJECTED",
  "rejectedBy": {
    "userId": "user_alice_01",
    "displayName": "Alice Chen"
  },
  "rejectedAt": "2026-02-21T15:45:00.000Z",
  "comment": "We decided to keep the original timeline."
}
```

**Side Effects:**
- The suggestion markup is removed from the document rendering.
- All connected clients receive a `suggestion_rejected` WebSocket event.
- The suggestion is retained in the system for audit purposes but hidden from the UI.

**Error Responses:**

| Code | Reason |
|------|--------|
| `401` | Missing or expired token |
| `403` | Only editors and owners can reject suggestions. The suggestion's author can also reject (withdraw) their own suggestion. |
| `404` | Document or suggestion not found |

---

## 10. Comparison with Other Systems

### Google Docs vs Microsoft 365 / Word Online

| Aspect | Google Docs | Microsoft 365 / Word Online |
|---|---|---|
| **Collaboration model** | Centralized OT (Jupiter protocol). Server imposes total order. | Uses a form of OT/CRDT hybrid for co-authoring. Operations are merged by a central service. [INFERRED] |
| **Real-time transport** | WebSocket (persistent, bidirectional) | WebSocket for Word Online; desktop Word uses periodic sync |
| **Offline editing** | Limited: Chrome PWA, IndexedDB cache, queue-and-sync | Robust: Desktop Word works fully offline, syncs via OneDrive on reconnect |
| **Conflict resolution** | Server transforms all concurrent operations automatically. No "conflicted copies." | Auto-merges non-conflicting edits. For heavy conflicts, may show merge UI. Desktop Word creates "conflicted" documents in rare cases. |
| **Document format** | Custom internal representation (linear annotated sequence). Not a file format. | Native .docx (OOXML). Word Online operates on .docx files stored in OneDrive/SharePoint. |
| **API model** | REST for metadata/content, WebSocket for real-time editing | Microsoft Graph API (REST) for metadata and content. WebSocket for co-authoring. |
| **Suggestion mode** | Full suggestion mode with inline tracked changes | "Track Changes" in Word -- more mature, with complex revision markup |
| **Key takeaway** | Online-first, real-time-first. Simpler document model optimized for OT. | File-first, offline-capable. Richer document model (.docx) but more complex collaboration. |

### Google Docs vs Notion

| Aspect | Google Docs | Notion |
|---|---|---|
| **Document model** | Linear annotated sequence (characters with attributes) | Block-based (each paragraph, heading, list, table is an independent "block") |
| **Collaboration model** | Centralized OT (fine-grained, character-level) | CRDT-inspired (block-level). Each block is independently editable. |
| **API complexity** | Complex WebSocket protocol with OT transforms | Simpler REST API. Blocks are CRUD-able independently. Real-time updates via WebSocket but simpler merge semantics. |
| **Conflict likelihood** | Higher -- two users editing the same paragraph create character-level conflicts that need OT resolution | Lower -- two users editing different blocks have no conflict. Same-block conflicts are simpler (block-level merge). |
| **Rich text** | Deep formatting support (fonts, sizes, colors, tables, images, headers/footers) | Simpler formatting. Blocks have types (paragraph, heading, list, code, image). Less control over fonts/sizes. |
| **Offline support** | Limited | More robust (CRDT properties make offline merge easier at block level) |
| **Key takeaway** | More powerful document editing. More complex collaboration protocol. | Simpler model, easier to reason about. Works well for notes and wikis. Not a replacement for a full document editor. |

### Google Docs vs Dropbox Paper

| Aspect | Google Docs | Dropbox Paper |
|---|---|---|
| **Collaboration model** | Full OT with character-level transforms | Simpler real-time collaboration (likely simplified OT or CRDT) |
| **Suggestion mode** | Full suggestion mode with accept/reject workflow | No suggestion mode |
| **Formatting** | Rich: fonts, sizes, colors, tables, headers/footers, images, TOC | Simpler: headings, bold, italic, lists, code blocks, images. No fonts/sizes/colors. |
| **Revision history** | Full revision history with named versions and restore | Basic revision history |
| **API surface** | Large: document CRUD, WebSocket OT, content, revisions, comments, permissions, suggestions, presence | Smaller: document CRUD, content, sharing. Simpler model. |
| **Key takeaway** | Full-featured document editor. Complex but powerful. | Lightweight collaborative editor. Simpler to implement but less capable. |

### Summary Matrix

| Feature | Google Docs | Microsoft 365 | Notion | Dropbox Paper |
|---|---|---|---|---|
| Real-time collaboration | OT (character-level) | OT/CRDT hybrid | CRDT-like (block-level) | Simplified real-time |
| Offline editing | Limited (PWA) | Full (desktop app) | Good (CRDT) | Limited |
| Suggestion mode | Yes | Yes (Track Changes) | No | No |
| Block-based model | No (linear) | No (linear/OOXML) | Yes | Partially |
| Comment anchoring | OT-adjusted anchors | OT-adjusted anchors | Block-level | Simplified |
| Document format | Custom IR | .docx (OOXML) | Block JSON | Custom |
| Cursor presence | Yes (100 user cap) | Yes | Yes | Yes |
| Revision history | Full + named versions | Full + versioning | Block-level history | Basic |

---

## 11. Interview Subset: What to Focus On in 60 Minutes

In a 60-minute system design interview, you cannot cover all 8 API groups in depth. Here is the recommended strategy:

### Must Cover (Core System -- spend 70% of API discussion here)

**1. Real-Time Collaboration WebSocket (Section 3)**

This is the defining technical challenge. Cover:
- The WebSocket connection lifecycle (handshake, init sync, operation exchange, close)
- The operation format (retain/insert/delete as document traversal)
- The three-state client protocol (Synchronized / Awaiting ACK / Awaiting ACK + Buffer)
- The three message types (client operation, server ack, server op from other user)
- One concrete OT transform example (insert vs delete with position shifting)

This demonstrates you understand the hard part of the system. An interviewer who sees you can explain the OT protocol over WebSocket will not question your ability to build REST CRUD endpoints.

**2. Document Content API (Section 4)**

Cover briefly:
- `GET /documents/{docId}/content` for initial load (snapshot + replay)
- Explain that real-time content delivery is via WebSocket, NOT REST
- Mention export as a secondary concern

**3. Sharing & Permissions (Section 7)**

Cover:
- The four permission levels (Owner > Editor > Commenter > Viewer)
- How permissions are enforced on the WebSocket channel (checked on connect AND on every operation)
- What happens when a permission changes while a user is actively editing (real-time downgrade)

**4. Revision History (Section 5)**

Cover:
- Revisions are reconstructed from the operation log, not stored as separate copies
- Snapshot compaction: latest snapshot + replay recent operations
- Restore creates a new revision (does not rewrite history)

### Should Mention (1-2 sentences each -- spend 20% here)

- **Commenting**: Comments anchored to text ranges, OT-adjusted. Briefly mention the orphaned comment problem.
- **Cursor/Presence**: Ephemeral, broadcast via the same WebSocket, throttled, O(N^2) bounded by 100-user cap.
- **Suggestion Mode**: Tracked changes as overlay operations. Accept/reject workflow.

### Skip Unless Asked (save for follow-up questions -- spend 10% here)

- **Document CRUD**: Standard REST. Not interesting for the interview.
- **Export formats**: Implementation detail. Not architecturally significant.
- **Color assignment for cursors**: Fun detail but not core.

### Interview Flow Recommendation

```
Phase 1 (2 min):  "Let me start with the API surface..."
                   Sketch the 8 API groups on the whiteboard.
                   Immediately call out: "The interesting API is the
                   WebSocket channel for real-time OT. Let me go deep there."

Phase 2 (8 min):  Deep dive on WebSocket protocol.
                   Draw the three-state client diagram.
                   Walk through one operation round-trip.
                   Show one OT transform example.

Phase 3 (3 min):  Document content (initial load flow).
                   Permissions model (4 levels, enforcement points).
                   Revision history (operation log + snapshots).

Phase 4 (2 min):  Briefly mention comments, presence, suggestions.
                   "I can go deeper on any of these if you'd like."
```

**Why this ordering works:**
The interviewer wants to see that you understand the hard problem (real-time collaboration via OT over WebSocket). If you spend 10 minutes on REST CRUD and 2 minutes on WebSocket, you have demonstrated the wrong priorities. Lead with the hard part, prove depth there, then show breadth by mentioning the supporting APIs.

---

*This document is a companion to the [interview simulation](01-interview-simulation.md). For deep dives on specific components, see:*
- [Operational Transformation](03-operational-transformation.md) -- OT algorithm, transform functions, Jupiter protocol
- [Document Storage](04-document-storage.md) -- Operation log, snapshots, storage infrastructure
- [Cursor & Presence](05-cursor-and-presence.md) -- Cursor synchronization, presence, scaling
- [Conflict Resolution](06-conflict-resolution-and-consistency.md) -- Edge cases, convergence guarantees
- [Permission & Sharing](08-permission-and-sharing.md) -- Access control, sharing model
