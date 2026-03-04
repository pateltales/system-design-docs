# 02 — WhatsApp Platform API Contracts

> Comprehensive API reference for a WhatsApp-like real-time chat application.
> Endpoints marked with **starred** are covered in the interview simulation (Phase 3).

---

## Table of Contents

1. [Authentication APIs](#1-authentication-apis)
2. [Messaging APIs](#2-messaging-apis)
3. [Real-Time Connection APIs (WebSocket)](#3-real-time-connection-apis-websocket)
4. [Group Chat APIs](#4-group-chat-apis)
5. [Media APIs](#5-media-apis)
6. [Status / Stories APIs](#6-statusstories-apis)
7. [Presence & Typing APIs](#7-presence--typing-apis)
8. [Contact & Profile APIs](#8-contact--profile-apis)
9. [Call Signaling APIs](#9-call-signaling-apis)
10. [Encryption Key APIs (Internal)](#10-encryption-key-apis-internal)
11. [Admin / Ops APIs (Internal)](#11-adminops-apis-internal)
12. [Contrast with Slack / Telegram / Discord](#contrast-with-slacktelegramdiscord)
13. [Interview Subset](#interview-subset--which-apis-to-focus-on)

---

## Common Conventions

| Convention | Detail |
|---|---|
| **Base URL** | `https://api.chatapp.io/v1` |
| **Auth** | Bearer JWT in `Authorization` header (except auth endpoints) |
| **Content-Type** | `application/json` unless noted (media upload uses `multipart/form-data` or chunked binary) |
| **Pagination** | Cursor-based — `cursor` (opaque string) + `limit` (default 50, max 200) |
| **Rate Limiting** | Per-user token bucket. `X-RateLimit-Remaining` and `Retry-After` headers on 429 |
| **Idempotency** | All mutating endpoints accept `Idempotency-Key` header. Clients generate a UUID per request. Server deduplicates within a 24-hour window |
| **Timestamps** | ISO-8601 / epoch milliseconds. Server assigns authoritative timestamps |
| **Encryption** | Message payloads are E2E encrypted blobs (base64-encoded). Server never sees plaintext |

---

## 1. Authentication APIs

WhatsApp uses phone numbers as identity — no email/password. Registration involves SMS OTP verification followed by device key exchange for E2E encryption.

---

### `POST /auth/register`

Begin registration with a phone number. Triggers an SMS OTP to the provided number.

**Request:**

```json
{
  "phoneNumber": "+14155552671",
  "deviceId": "a3f8c2e1-uuid",
  "deviceType": "ANDROID",
  "appVersion": "2.24.1.10"
}
```

**Response (200 OK):**

```json
{
  "registrationId": "reg-7f3a-4b2c-uuid",
  "phoneNumber": "+14155552671",
  "otpLength": 6,
  "retryAfterSeconds": 60,
  "method": "SMS"
}
```

**Error (429 Too Many Requests):**

```json
{
  "error": "RATE_LIMITED",
  "message": "Too many registration attempts. Retry after 3600 seconds.",
  "retryAfterSeconds": 3600
}
```

---

### `POST /auth/verify-otp`

Verify the SMS OTP. On success, returns access and refresh tokens. Also triggers the client to upload its E2E pre-key bundle (see Encryption Key APIs).

**Request:**

```json
{
  "registrationId": "reg-7f3a-4b2c-uuid",
  "phoneNumber": "+14155552671",
  "otp": "482913",
  "identityPublicKey": "base64-encoded-identity-key",
  "signedPreKey": {
    "keyId": 1,
    "publicKey": "base64-encoded-signed-prekey",
    "signature": "base64-encoded-signature"
  },
  "oneTimePreKeys": [
    { "keyId": 1, "publicKey": "base64-encoded-otpk-1" },
    { "keyId": 2, "publicKey": "base64-encoded-otpk-2" }
  ]
}
```

**Response (200 OK):**

```json
{
  "userId": "usr-9d4e-a1b2-uuid",
  "accessToken": "eyJhbGciOiJSUzI1NiIsInR5cCI6IkpXVCJ9...",
  "refreshToken": "dGhpcyBpcyBhIHJlZnJlc2ggdG9rZW4...",
  "accessTokenExpiresAt": "2025-06-15T12:30:00Z",
  "profile": {
    "userId": "usr-9d4e-a1b2-uuid",
    "phoneNumber": "+14155552671",
    "displayName": null,
    "profilePhotoUrl": null
  }
}
```

**Error (401 Unauthorized):**

```json
{
  "error": "INVALID_OTP",
  "message": "OTP is invalid or expired.",
  "attemptsRemaining": 2
}
```

---

### `POST /auth/refresh-token`

Exchange a valid refresh token for a new access token.

**Request:**

```json
{
  "refreshToken": "dGhpcyBpcyBhIHJlZnJlc2ggdG9rZW4...",
  "deviceId": "a3f8c2e1-uuid"
}
```

**Response (200 OK):**

```json
{
  "accessToken": "eyJhbGciOiJSUzI1NiIsInR5cCI6IkpXVCJ9...",
  "accessTokenExpiresAt": "2025-06-15T14:30:00Z",
  "refreshToken": "bmV3IHJlZnJlc2ggdG9rZW4..."
}
```

---

### `POST /auth/logout`

Invalidate the current session and remove the device from the connection registry.

**Request:**

```json
{
  "deviceId": "a3f8c2e1-uuid",
  "allDevices": false
}
```

**Response (200 OK):**

```json
{
  "success": true,
  "message": "Session invalidated."
}
```

---

## 2. Messaging APIs

The most critical path in the entire system. Messages are E2E encrypted — the server stores opaque encrypted blobs plus metadata (sender, recipient, timestamp, delivery status).

---

### `POST /messages/send` ⭐

Send a message to a 1:1 conversation. This is THE latency-sensitive hot path.

The `encryptedPayload` is a Signal Protocol ciphertext blob — the server cannot inspect or modify it. The `messageType` field is metadata visible to the server for routing and storage optimization, but the actual content (text, caption, location coordinates, etc.) is inside the encrypted blob.

**Request:**

```json
{
  "messageId": "msg-c1d2-e3f4-uuid",
  "conversationId": "conv-a1b2-c3d4-uuid",
  "recipientId": "usr-5678-efgh-uuid",
  "messageType": "TEXT",
  "encryptedPayload": "base64-encoded-signal-protocol-ciphertext",
  "timestamp": 1718451200000,
  "deviceId": "a3f8c2e1-uuid"
}
```

**Message types and their encrypted payloads:**

| `messageType` | What is inside `encryptedPayload` (after decryption) |
|---|---|
| `TEXT` | `{ "text": "Hello!" }` |
| `IMAGE` | `{ "mediaId": "...", "encryptionKey": "...", "mimeType": "image/jpeg", "thumbnailBase64": "...", "caption": "Look at this" }` |
| `VIDEO` | `{ "mediaId": "...", "encryptionKey": "...", "mimeType": "video/mp4", "thumbnailBase64": "...", "durationSeconds": 45 }` |
| `AUDIO` | `{ "mediaId": "...", "encryptionKey": "...", "mimeType": "audio/ogg", "durationSeconds": 12 }` |
| `DOCUMENT` | `{ "mediaId": "...", "encryptionKey": "...", "mimeType": "application/pdf", "fileName": "report.pdf", "fileSizeBytes": 204800 }` |
| `LOCATION` | `{ "latitude": 37.7749, "longitude": -122.4194, "name": "San Francisco", "address": "..." }` |
| `CONTACT` | `{ "displayName": "Alice", "phoneNumber": "+14155552671" }` |

**Response (202 Accepted):**

```json
{
  "messageId": "msg-c1d2-e3f4-uuid",
  "conversationId": "conv-a1b2-c3d4-uuid",
  "serverTimestamp": 1718451200123,
  "sequenceNumber": 48721,
  "status": "SENT"
}
```

**Why 202 (Accepted) and not 200 (OK)?**
The server has accepted the message for delivery but the recipient may not have received it yet. The actual delivery confirmation comes asynchronously via WebSocket (delivery receipt).

**Error (413 Payload Too Large):**

```json
{
  "error": "PAYLOAD_TOO_LARGE",
  "message": "Encrypted payload exceeds 64 KB limit. Use media upload for large content.",
  "maxPayloadBytes": 65536
}
```

---

### `GET /messages/{conversationId}` ⭐

Fetch paginated message history for a conversation. Uses cursor-based pagination for stable results even as new messages arrive.

**Request:**

```
GET /messages/conv-a1b2-c3d4-uuid?cursor=eyJzZXEiOjQ4NzIwfQ&limit=50&direction=BACKWARD
```

| Parameter | Type | Required | Description |
|---|---|---|---|
| `cursor` | string | No | Opaque cursor from previous response. Omit for latest messages |
| `limit` | int | No | Number of messages to return (default 50, max 200) |
| `direction` | enum | No | `BACKWARD` (older messages) or `FORWARD` (newer messages). Default `BACKWARD` |

**Response (200 OK):**

```json
{
  "conversationId": "conv-a1b2-c3d4-uuid",
  "messages": [
    {
      "messageId": "msg-c1d2-e3f4-uuid",
      "senderId": "usr-1234-abcd-uuid",
      "messageType": "TEXT",
      "encryptedPayload": "base64-encoded-ciphertext",
      "serverTimestamp": 1718451200123,
      "sequenceNumber": 48721,
      "status": "READ"
    },
    {
      "messageId": "msg-d2e3-f4a5-uuid",
      "senderId": "usr-5678-efgh-uuid",
      "messageType": "IMAGE",
      "encryptedPayload": "base64-encoded-ciphertext",
      "serverTimestamp": 1718451180456,
      "sequenceNumber": 48720,
      "status": "DELIVERED"
    }
  ],
  "pagination": {
    "nextCursor": "eyJzZXEiOjQ4NzE5fQ",
    "prevCursor": "eyJzZXEiOjQ4NzIyfQ",
    "hasMore": true
  }
}
```

**Why cursor-based and not offset-based?**
Messages arrive constantly. Offset-based pagination (page 1, page 2) shifts when new messages are inserted — a user scrolling back would see duplicate or missing messages. Cursors point to a stable position (the sequence number), immune to inserts.

---

### `PUT /messages/{messageId}/status` ⭐

Update the delivery status of a message. This powers delivery receipts (the single-check, double-check, blue-check UX).

**Request:**

```json
{
  "status": "DELIVERED",
  "deviceId": "a3f8c2e1-uuid",
  "timestamp": 1718451205000
}
```

| Status | Meaning | Trigger |
|---|---|---|
| `SENT` | Server accepted the message | Server ACK to sender |
| `DELIVERED` | Recipient's device received the message | Recipient's client sends this |
| `READ` | Recipient opened the conversation | Recipient's client sends this |

**Response (200 OK):**

```json
{
  "messageId": "msg-c1d2-e3f4-uuid",
  "status": "DELIVERED",
  "updatedAt": 1718451205123
}
```

**Note:** Status transitions are one-way: SENT -> DELIVERED -> READ. The server rejects backwards transitions.

---

### `DELETE /messages/{messageId}`

Delete a message. Supports "delete for me" (local only) and "delete for everyone" (within a time window, typically 1 hour 8 minutes after send).

**Request:**

```json
{
  "deleteType": "FOR_EVERYONE",
  "deviceId": "a3f8c2e1-uuid"
}
```

| `deleteType` | Behavior |
|---|---|
| `FOR_ME` | Server marks message as deleted for this user only. Other participants still see it |
| `FOR_EVERYONE` | Server sends a "revoke" signal to all participants. Only works within the time window and only for messages you sent |

**Response (200 OK):**

```json
{
  "messageId": "msg-c1d2-e3f4-uuid",
  "deleteType": "FOR_EVERYONE",
  "deletedAt": 1718451300000
}
```

**Error (403 Forbidden):**

```json
{
  "error": "DELETE_WINDOW_EXPIRED",
  "message": "Cannot delete for everyone — time window has passed. You can still delete for yourself."
}
```

---

## 3. Real-Time Connection APIs (WebSocket)

The persistent connection that makes chat "real-time." WhatsApp historically uses a custom XMPP-derived protocol over persistent TCP connections. For interview purposes, we model this as WebSocket.

---

### `WS /ws/connect` ⭐

Establish a persistent WebSocket connection. This is the primary channel for message delivery, receipts, typing indicators, and presence updates.

**Connection Handshake:**

```
GET /ws/connect HTTP/1.1
Host: gateway.chatapp.io
Upgrade: websocket
Connection: Upgrade
Authorization: Bearer eyJhbGciOiJSUzI1NiIs...
X-Device-Id: a3f8c2e1-uuid
X-Last-Seq: 48700
```

`X-Last-Seq` tells the server the client's last known sequence number. The server will deliver all messages since that sequence number on connect (offline sync).

**Connection ACK (server -> client):**

```json
{
  "type": "CONNECTION_ACK",
  "connectionId": "conn-x9y8-z7w6-uuid",
  "serverTime": 1718451200000,
  "heartbeatIntervalMs": 30000
}
```

---

### WebSocket Frame Types

All frames are JSON-encoded. The `type` field discriminates the frame kind.

---

#### `MESSAGE` (server -> client) ⭐

A new message delivered to the client.

```json
{
  "type": "MESSAGE",
  "messageId": "msg-c1d2-e3f4-uuid",
  "conversationId": "conv-a1b2-c3d4-uuid",
  "senderId": "usr-1234-abcd-uuid",
  "messageType": "TEXT",
  "encryptedPayload": "base64-encoded-ciphertext",
  "serverTimestamp": 1718451200123,
  "sequenceNumber": 48721
}
```

---

#### `MESSAGE_ACK` (client -> server) ⭐

Client acknowledges receipt of a message. Without this ACK, the server retries delivery with exponential backoff.

```json
{
  "type": "MESSAGE_ACK",
  "messageId": "msg-c1d2-e3f4-uuid",
  "deviceId": "a3f8c2e1-uuid",
  "timestamp": 1718451200200
}
```

---

#### `SEND_MESSAGE` (client -> server)

Client sends a message through the WebSocket (alternative to the REST `POST /messages/send`). In practice, the WebSocket path is preferred for lower latency.

```json
{
  "type": "SEND_MESSAGE",
  "messageId": "msg-new1-new2-uuid",
  "conversationId": "conv-a1b2-c3d4-uuid",
  "recipientId": "usr-5678-efgh-uuid",
  "messageType": "TEXT",
  "encryptedPayload": "base64-encoded-ciphertext",
  "timestamp": 1718451210000
}
```

---

#### `SEND_ACK` (server -> client) ⭐

Server acknowledges it has accepted a sent message. This is the single-check moment.

```json
{
  "type": "SEND_ACK",
  "messageId": "msg-new1-new2-uuid",
  "serverTimestamp": 1718451210050,
  "sequenceNumber": 48722,
  "status": "SENT"
}
```

---

#### `RECEIPT` (server -> client) ⭐

Delivery or read receipt from the recipient, forwarded to the sender.

```json
{
  "type": "RECEIPT",
  "messageId": "msg-c1d2-e3f4-uuid",
  "conversationId": "conv-a1b2-c3d4-uuid",
  "recipientId": "usr-5678-efgh-uuid",
  "status": "DELIVERED",
  "timestamp": 1718451205000
}
```

---

#### `TYPING` (bidirectional)

Ephemeral typing indicator. Not persisted. Short TTL (3-5 seconds) — if no refresh frame, the indicator disappears on the other end.

**Client -> Server:**

```json
{
  "type": "TYPING",
  "conversationId": "conv-a1b2-c3d4-uuid",
  "action": "START"
}
```

**Server -> Client (forwarded to the other participant):**

```json
{
  "type": "TYPING",
  "conversationId": "conv-a1b2-c3d4-uuid",
  "userId": "usr-1234-abcd-uuid",
  "action": "START",
  "expiresAtMs": 1718451215000
}
```

| `action` | Meaning |
|---|---|
| `START` | User started typing. Refresh every 3 seconds while still typing |
| `STOP` | User stopped typing (cleared the input, sent the message) |

---

#### `PRESENCE` (server -> client)

Presence update for a contact.

```json
{
  "type": "PRESENCE",
  "userId": "usr-5678-efgh-uuid",
  "status": "ONLINE",
  "lastSeenAt": null
}
```

```json
{
  "type": "PRESENCE",
  "userId": "usr-5678-efgh-uuid",
  "status": "OFFLINE",
  "lastSeenAt": 1718451300000
}
```

---

#### `HEARTBEAT` (bidirectional) ⭐

Keepalive ping/pong. Client sends at the interval specified in `CONNECTION_ACK`. If the server receives no heartbeat within 2x the interval, it marks the connection as dead and updates presence to offline.

**Client -> Server:**

```json
{
  "type": "HEARTBEAT",
  "timestamp": 1718451230000
}
```

**Server -> Client:**

```json
{
  "type": "HEARTBEAT_ACK",
  "timestamp": 1718451230005,
  "serverTime": 1718451230010
}
```

**Why heartbeats matter for mobile:** Mobile networks aggressively close idle TCP connections (NAT timeout as short as 30 seconds on some carriers). Heartbeats keep the NAT mapping alive.

---

#### `OFFLINE_SYNC` (server -> client)

Batch delivery of messages that accumulated while the client was offline. Sent immediately after `CONNECTION_ACK`.

```json
{
  "type": "OFFLINE_SYNC",
  "messages": [
    {
      "messageId": "msg-off1-uuid",
      "conversationId": "conv-a1b2-c3d4-uuid",
      "senderId": "usr-1234-abcd-uuid",
      "messageType": "TEXT",
      "encryptedPayload": "base64-encoded-ciphertext",
      "serverTimestamp": 1718440000000,
      "sequenceNumber": 48710
    }
  ],
  "hasMore": true,
  "syncCursor": "eyJzZXEiOjQ4NzEwfQ"
}
```

If `hasMore` is `true`, the client requests the next batch:

```json
{
  "type": "OFFLINE_SYNC_REQUEST",
  "syncCursor": "eyJzZXEiOjQ4NzEwfQ"
}
```

---

#### `ERROR` (server -> client)

Server-side error related to a specific frame.

```json
{
  "type": "ERROR",
  "code": "RECIPIENT_NOT_FOUND",
  "message": "The recipient user does not exist.",
  "relatedMessageId": "msg-new1-new2-uuid"
}
```

---

## 4. Group Chat APIs

Groups can have up to 1024 members. Group messages use fan-out on write — one send triggers N deliveries. Group E2E encryption uses Sender Keys (one encryption, N decryptions).

---

### `POST /groups` ⭐

Create a new group.

**Request:**

```json
{
  "name": "Weekend Hikers",
  "description": "Planning our Saturday hikes",
  "participantIds": [
    "usr-1111-uuid",
    "usr-2222-uuid",
    "usr-3333-uuid"
  ],
  "settings": {
    "onlyAdminsCanSend": false,
    "onlyAdminsCanEditInfo": false
  }
}
```

**Response (201 Created):**

```json
{
  "groupId": "grp-a1b2-c3d4-uuid",
  "name": "Weekend Hikers",
  "description": "Planning our Saturday hikes",
  "createdBy": "usr-9d4e-a1b2-uuid",
  "createdAt": "2025-06-15T10:00:00Z",
  "participants": [
    { "userId": "usr-9d4e-a1b2-uuid", "role": "ADMIN", "joinedAt": "2025-06-15T10:00:00Z" },
    { "userId": "usr-1111-uuid", "role": "MEMBER", "joinedAt": "2025-06-15T10:00:00Z" },
    { "userId": "usr-2222-uuid", "role": "MEMBER", "joinedAt": "2025-06-15T10:00:00Z" },
    { "userId": "usr-3333-uuid", "role": "MEMBER", "joinedAt": "2025-06-15T10:00:00Z" }
  ],
  "settings": {
    "onlyAdminsCanSend": false,
    "onlyAdminsCanEditInfo": false,
    "maxParticipants": 1024
  },
  "conversationId": "conv-grp-a1b2-uuid"
}
```

---

### `PUT /groups/{groupId}`

Update group metadata (name, description, photo, settings). Restricted by `onlyAdminsCanEditInfo` setting.

**Request:**

```json
{
  "name": "Weekend Hikers 2025",
  "description": "Saturday morning hikes — meet at 7 AM",
  "profilePhotoMediaId": "media-9f8e-uuid",
  "settings": {
    "onlyAdminsCanSend": true
  }
}
```

**Response (200 OK):**

```json
{
  "groupId": "grp-a1b2-c3d4-uuid",
  "name": "Weekend Hikers 2025",
  "description": "Saturday morning hikes — meet at 7 AM",
  "updatedAt": "2025-06-15T11:00:00Z",
  "updatedBy": "usr-9d4e-a1b2-uuid"
}
```

---

### `POST /groups/{groupId}/participants`

Add members to a group. Only admins can add members (up to the 1024 limit). When new members are added, sender keys must be redistributed for E2E encryption.

**Request:**

```json
{
  "participantIds": [
    "usr-4444-uuid",
    "usr-5555-uuid"
  ]
}
```

**Response (200 OK):**

```json
{
  "groupId": "grp-a1b2-c3d4-uuid",
  "added": [
    { "userId": "usr-4444-uuid", "role": "MEMBER", "joinedAt": "2025-06-15T12:00:00Z" },
    { "userId": "usr-5555-uuid", "role": "MEMBER", "joinedAt": "2025-06-15T12:00:00Z" }
  ],
  "currentParticipantCount": 6,
  "senderKeyRotationRequired": true
}
```

---

### `DELETE /groups/{groupId}/participants/{userId}`

Remove a member from a group. Admins can remove anyone; a member can remove themselves (leave group). On removal, sender keys are rotated to maintain forward secrecy.

**Response (200 OK):**

```json
{
  "groupId": "grp-a1b2-c3d4-uuid",
  "removedUserId": "usr-3333-uuid",
  "removedBy": "usr-9d4e-a1b2-uuid",
  "removedAt": "2025-06-15T13:00:00Z",
  "currentParticipantCount": 5,
  "senderKeyRotationRequired": true
}
```

---

### `POST /groups/{groupId}/messages` ⭐

Send a message to a group. The client encrypts once with its Sender Key. The server fans out to all group members.

**Request:**

```json
{
  "messageId": "msg-grp-1234-uuid",
  "messageType": "TEXT",
  "encryptedPayload": "base64-encoded-sender-key-ciphertext",
  "senderKeyId": "sk-abcd-uuid",
  "timestamp": 1718451300000
}
```

**Response (202 Accepted):**

```json
{
  "messageId": "msg-grp-1234-uuid",
  "groupId": "grp-a1b2-c3d4-uuid",
  "serverTimestamp": 1718451300050,
  "sequenceNumber": 1205,
  "status": "SENT",
  "recipientCount": 5
}
```

**Fan-out behavior:** The server writes a copy of the message into each member's inbox (fan-out on write). For a 1024-member group, this means 1024 writes. This is bounded and acceptable at WhatsApp scale.

---

### `GET /groups/{groupId}/messages`

Paginated group message history. Same cursor-based pagination as 1:1 messages.

**Request:**

```
GET /groups/grp-a1b2-c3d4-uuid/messages?cursor=eyJzZXEiOjEyMDR9&limit=50
```

**Response (200 OK):**

```json
{
  "groupId": "grp-a1b2-c3d4-uuid",
  "messages": [
    {
      "messageId": "msg-grp-1234-uuid",
      "senderId": "usr-9d4e-a1b2-uuid",
      "messageType": "TEXT",
      "encryptedPayload": "base64-encoded-ciphertext",
      "serverTimestamp": 1718451300050,
      "sequenceNumber": 1205,
      "deliveryStatus": {
        "usr-1111-uuid": "READ",
        "usr-2222-uuid": "DELIVERED",
        "usr-4444-uuid": "SENT",
        "usr-5555-uuid": "SENT"
      }
    }
  ],
  "pagination": {
    "nextCursor": "eyJzZXEiOjEyMDN9",
    "hasMore": true
  }
}
```

---

## 5. Media APIs

Media is stored separately from messages. A message contains a reference (`mediaId` + encryption key). The client encrypts media with a random AES-256 key before upload — the server stores an opaque encrypted blob.

---

### `POST /media/upload` ⭐

Upload encrypted media. Supports chunked, resumable uploads for large files (videos, documents).

**Initiate upload:**

```
POST /media/upload
Content-Type: application/json
```

```json
{
  "fileName": "photo.jpg.enc",
  "mimeType": "application/octet-stream",
  "fileSizeBytes": 245760,
  "checksum": "sha256:a1b2c3d4e5f6...",
  "resumable": false
}
```

**Response (200 OK — small file, direct upload):**

```json
{
  "uploadUrl": "https://media.chatapp.io/upload/upl-xyz-uuid",
  "mediaId": "media-9f8e-7d6c-uuid",
  "expiresAt": "2025-06-15T11:00:00Z",
  "method": "PUT"
}
```

Client then PUTs the encrypted binary to `uploadUrl`.

**Initiate resumable upload (large file):**

```json
{
  "fileName": "video.mp4.enc",
  "mimeType": "application/octet-stream",
  "fileSizeBytes": 52428800,
  "checksum": "sha256:f6e5d4c3b2a1...",
  "resumable": true,
  "chunkSizeBytes": 262144
}
```

**Response (200 OK):**

```json
{
  "uploadId": "upl-resume-abc-uuid",
  "mediaId": "media-5a4b-3c2d-uuid",
  "uploadUrl": "https://media.chatapp.io/upload/upl-resume-abc-uuid",
  "chunkSizeBytes": 262144,
  "totalChunks": 200,
  "expiresAt": "2025-06-15T14:00:00Z"
}
```

**Upload chunk:**

```
PUT https://media.chatapp.io/upload/upl-resume-abc-uuid
Content-Range: bytes 0-262143/52428800
Content-Type: application/octet-stream

<binary chunk data>
```

**Chunk response:**

```json
{
  "uploadId": "upl-resume-abc-uuid",
  "chunkIndex": 0,
  "bytesReceived": 262144,
  "totalBytesReceived": 262144,
  "status": "IN_PROGRESS"
}
```

**Final chunk response:**

```json
{
  "uploadId": "upl-resume-abc-uuid",
  "mediaId": "media-5a4b-3c2d-uuid",
  "status": "COMPLETE",
  "fileSizeBytes": 52428800,
  "checksumVerified": true
}
```

---

### `GET /media/{mediaId}`

Download encrypted media blob. The client decrypts locally using the encryption key from the message payload.

**Request:**

```
GET /media/media-9f8e-7d6c-uuid
```

**Response (200 OK):**

```
Content-Type: application/octet-stream
Content-Length: 245760
Content-Disposition: attachment; filename="media-9f8e-7d6c-uuid.enc"
X-Media-Checksum: sha256:a1b2c3d4e5f6...

<encrypted binary data>
```

Supports `Range` header for partial downloads (resume interrupted downloads on mobile).

---

### `GET /media/{mediaId}/thumbnail`

Fetch a low-resolution thumbnail/preview. Used for the blurry preview effect while the full media downloads in the background.

**Request:**

```
GET /media/media-9f8e-7d6c-uuid/thumbnail
```

**Response (200 OK):**

```json
{
  "mediaId": "media-9f8e-7d6c-uuid",
  "thumbnailBase64": "/9j/4AAQSkZJRgABAQ...",
  "width": 100,
  "height": 75,
  "mimeType": "image/jpeg"
}
```

**Note:** The thumbnail is typically embedded directly in the message's encrypted payload (so it's also E2E encrypted). This REST endpoint is a fallback for cases where the inline thumbnail was not included.

---

## 6. Status/Stories APIs

Statuses (Stories) are ephemeral posts that auto-expire after 24 hours. Viewers list is tracked.

---

### `POST /status`

Post a new status update.

**Request:**

```json
{
  "statusId": "sts-a1b2-uuid",
  "statusType": "IMAGE",
  "encryptedPayload": "base64-encoded-encrypted-status-content",
  "mediaId": "media-sts-uuid",
  "caption": null,
  "backgroundColor": null,
  "visibility": "CONTACTS",
  "expiresAt": "2025-06-16T10:00:00Z"
}
```

| `statusType` | Description |
|---|---|
| `TEXT` | Text with background color |
| `IMAGE` | Photo with optional caption |
| `VIDEO` | Video clip (up to 30 seconds) |

| `visibility` | Who can see it |
|---|---|
| `CONTACTS` | All contacts |
| `CONTACTS_EXCEPT` | All contacts except a blocklist |
| `ONLY_SHARE_WITH` | Only specific contacts |

**Response (201 Created):**

```json
{
  "statusId": "sts-a1b2-uuid",
  "postedAt": "2025-06-15T10:00:00Z",
  "expiresAt": "2025-06-16T10:00:00Z",
  "viewerCount": 0
}
```

---

### `GET /status/contacts`

Fetch statuses from all contacts who have posted in the last 24 hours.

**Response (200 OK):**

```json
{
  "statuses": [
    {
      "userId": "usr-1111-uuid",
      "displayName": "Alice",
      "profilePhotoUrl": "https://media.chatapp.io/profile/...",
      "statusItems": [
        {
          "statusId": "sts-x1y2-uuid",
          "statusType": "IMAGE",
          "thumbnailBase64": "/9j/4AAQ...",
          "postedAt": "2025-06-15T08:30:00Z",
          "expiresAt": "2025-06-16T08:30:00Z",
          "viewed": false
        },
        {
          "statusId": "sts-x3y4-uuid",
          "statusType": "TEXT",
          "postedAt": "2025-06-15T09:15:00Z",
          "expiresAt": "2025-06-16T09:15:00Z",
          "viewed": true
        }
      ]
    }
  ],
  "lastRefreshedAt": "2025-06-15T10:30:00Z"
}
```

---

### `GET /status/{statusId}`

View a specific status. The server records the viewer.

**Response (200 OK):**

```json
{
  "statusId": "sts-x1y2-uuid",
  "userId": "usr-1111-uuid",
  "statusType": "IMAGE",
  "encryptedPayload": "base64-encoded-encrypted-content",
  "mediaId": "media-sts-img-uuid",
  "postedAt": "2025-06-15T08:30:00Z",
  "expiresAt": "2025-06-16T08:30:00Z",
  "viewers": [
    { "userId": "usr-9d4e-a1b2-uuid", "viewedAt": "2025-06-15T10:30:00Z" }
  ],
  "viewerCount": 12
}
```

---

### `DELETE /status/{statusId}`

Delete a status before it expires naturally.

**Response (200 OK):**

```json
{
  "statusId": "sts-x1y2-uuid",
  "deletedAt": "2025-06-15T11:00:00Z"
}
```

---

## 7. Presence & Typing APIs

Presence updates are pushed via WebSocket in real-time. These REST endpoints are supplementary — used when the client needs to explicitly query or update state.

---

### `PUT /presence` ⭐

Update the caller's online/offline presence.

**Request:**

```json
{
  "status": "ONLINE",
  "deviceId": "a3f8c2e1-uuid"
}
```

| `status` | Meaning |
|---|---|
| `ONLINE` | User is actively using the app |
| `OFFLINE` | User has backgrounded or closed the app |

**Response (200 OK):**

```json
{
  "userId": "usr-9d4e-a1b2-uuid",
  "status": "ONLINE",
  "updatedAt": 1718451200000
}
```

**Note:** In practice, presence is updated implicitly: opening a WebSocket connection sets `ONLINE`, and a heartbeat timeout sets `OFFLINE`. This explicit endpoint is for edge cases (e.g., app backgrounded but WebSocket still alive briefly).

---

### `POST /typing`

Send a typing indicator. This is typically sent via WebSocket (lower latency), but the REST endpoint exists for clients that cannot maintain a WebSocket connection.

**Request:**

```json
{
  "conversationId": "conv-a1b2-c3d4-uuid",
  "action": "START"
}
```

**Response (200 OK):**

```json
{
  "conversationId": "conv-a1b2-c3d4-uuid",
  "action": "START",
  "expiresAtMs": 1718451205000
}
```

---

### `GET /presence/{userId}` ⭐

Get a user's current presence status. Subject to the target user's privacy settings.

**Response (200 OK):**

```json
{
  "userId": "usr-5678-efgh-uuid",
  "status": "OFFLINE",
  "lastSeenAt": 1718448000000
}
```

**Response (200 OK — privacy restricted):**

```json
{
  "userId": "usr-5678-efgh-uuid",
  "status": "UNKNOWN",
  "lastSeenAt": null,
  "reason": "PRIVACY_RESTRICTED"
}
```

**Presence is eventually consistent.** A user's last seen time may be stale by a few seconds. This is acceptable — slight staleness is not a problem for the user experience, and strong consistency would require expensive distributed coordination.

---

## 8. Contact & Profile APIs

Contact sync uses hashed phone numbers for privacy. The server never receives raw phone contacts.

---

### `POST /contacts/sync`

Upload hashed phone contacts. Server returns which contacts are registered on the platform.

**Request:**

```json
{
  "hashedContacts": [
    "sha256:e3b0c44298fc1c149a...",
    "sha256:d7a8fbb307d7809469...",
    "sha256:5e884898da280471...",
    "sha256:9f86d081884c7d659a..."
  ],
  "hashAlgorithm": "SHA-256",
  "fullSync": false,
  "lastSyncTimestamp": 1718361600000
}
```

| Field | Description |
|---|---|
| `hashedContacts` | SHA-256 hashes of phone numbers (E.164 format before hashing) |
| `fullSync` | `true` for first sync, `false` for delta sync (only new contacts since last sync) |
| `lastSyncTimestamp` | Timestamp of last sync, for delta computation |

**Response (200 OK):**

```json
{
  "registeredContacts": [
    {
      "hashedPhone": "sha256:e3b0c44298fc1c149a...",
      "userId": "usr-1111-uuid",
      "displayName": "Alice",
      "profilePhotoUrl": "https://media.chatapp.io/profile/usr-1111.jpg",
      "about": "Hey there! I am using ChatApp"
    },
    {
      "hashedPhone": "sha256:d7a8fbb307d7809469...",
      "userId": "usr-2222-uuid",
      "displayName": "Bob",
      "profilePhotoUrl": null,
      "about": "Available"
    }
  ],
  "syncTimestamp": 1718451200000,
  "totalRegistered": 2,
  "totalChecked": 4
}
```

**Privacy note:** Hashing phone numbers is a baseline privacy measure, but SHA-256 of phone numbers is vulnerable to enumeration attacks (there are only ~10 billion possible phone numbers). Advanced implementations use techniques like private set intersection (PSI) to avoid revealing even hashed numbers. For interview purposes, the hash-based approach is sufficient.

---

### `GET /profile/{userId}`

Get a user's public profile.

**Response (200 OK):**

```json
{
  "userId": "usr-1111-uuid",
  "displayName": "Alice",
  "about": "Hey there! I am using ChatApp",
  "profilePhotoUrl": "https://media.chatapp.io/profile/usr-1111.jpg",
  "lastSeenAt": 1718448000000,
  "lastSeenVisibility": "CONTACTS"
}
```

---

### `PUT /profile`

Update the caller's own profile.

**Request:**

```json
{
  "displayName": "Alice Smith",
  "about": "Living my best life",
  "profilePhotoMediaId": "media-prof-uuid",
  "privacy": {
    "lastSeenVisibility": "CONTACTS",
    "profilePhotoVisibility": "EVERYONE",
    "aboutVisibility": "CONTACTS"
  }
}
```

| Visibility | Who can see |
|---|---|
| `EVERYONE` | All users |
| `CONTACTS` | Only mutual contacts |
| `NOBODY` | Hidden from everyone |

**Response (200 OK):**

```json
{
  "userId": "usr-9d4e-a1b2-uuid",
  "displayName": "Alice Smith",
  "about": "Living my best life",
  "profilePhotoUrl": "https://media.chatapp.io/profile/usr-9d4e.jpg",
  "updatedAt": "2025-06-15T12:00:00Z"
}
```

---

## 9. Call Signaling APIs

The backend only handles signaling (session setup, teardown, ICE candidate exchange). Actual voice/video data flows peer-to-peer via WebRTC (using STUN/TURN servers for NAT traversal).

---

### `POST /calls/initiate`

Start a voice or video call. Server sends a call offer to the callee via WebSocket.

**Request:**

```json
{
  "callId": "call-a1b2-uuid",
  "calleeId": "usr-5678-efgh-uuid",
  "callType": "VIDEO",
  "sdpOffer": "v=0\r\no=- 4611731400430051336 2 IN IP4 127.0.0.1\r\n...",
  "deviceId": "a3f8c2e1-uuid"
}
```

**Response (202 Accepted):**

```json
{
  "callId": "call-a1b2-uuid",
  "status": "RINGING",
  "calleeOnline": true,
  "createdAt": 1718451400000,
  "ringTimeoutSeconds": 45
}
```

**If callee is offline:**

```json
{
  "callId": "call-a1b2-uuid",
  "status": "CALLEE_UNAVAILABLE",
  "calleeOnline": false
}
```

---

### `POST /calls/{callId}/answer`

Callee accepts the call. Includes their SDP answer for WebRTC session establishment.

**Request:**

```json
{
  "sdpAnswer": "v=0\r\no=- 7614219264536042location 2 IN IP4 127.0.0.1\r\n...",
  "deviceId": "b4c5d6e7-uuid"
}
```

**Response (200 OK):**

```json
{
  "callId": "call-a1b2-uuid",
  "status": "CONNECTED",
  "connectedAt": 1718451405000
}
```

---

### `POST /calls/{callId}/reject`

Callee rejects the call.

**Request:**

```json
{
  "reason": "BUSY",
  "deviceId": "b4c5d6e7-uuid"
}
```

| `reason` | Meaning |
|---|---|
| `BUSY` | User is on another call |
| `DECLINED` | User explicitly declined |
| `TIMEOUT` | Ring timeout elapsed |

**Response (200 OK):**

```json
{
  "callId": "call-a1b2-uuid",
  "status": "REJECTED",
  "reason": "BUSY"
}
```

---

### `POST /calls/{callId}/end`

Either party ends an active call.

**Request:**

```json
{
  "deviceId": "a3f8c2e1-uuid"
}
```

**Response (200 OK):**

```json
{
  "callId": "call-a1b2-uuid",
  "status": "ENDED",
  "durationSeconds": 324,
  "endedAt": 1718451729000,
  "endedBy": "usr-9d4e-a1b2-uuid"
}
```

---

### `POST /calls/{callId}/ice-candidate`

Exchange ICE candidates for WebRTC NAT traversal. Both parties send their discovered ICE candidates to each other via the server.

**Request:**

```json
{
  "candidate": {
    "candidate": "candidate:842163049 1 udp 1677729535 203.0.113.5 24578 typ srflx raddr 192.168.1.5 rport 45678",
    "sdpMid": "0",
    "sdpMLineIndex": 0
  },
  "deviceId": "a3f8c2e1-uuid"
}
```

**Response (200 OK):**

```json
{
  "callId": "call-a1b2-uuid",
  "candidateReceived": true
}
```

The server relays the ICE candidate to the other party via WebSocket.

---

## 10. Encryption Key APIs (Internal)

These power the Signal Protocol (X3DH + Double Ratchet) that provides E2E encryption. The server acts as a key distribution service — it stores public keys only and cannot derive session keys.

---

### `POST /keys/prekeys`

Upload or replenish the pre-key bundle. The client should keep at least 100 one-time pre-keys on the server. When the count drops below a threshold, the server notifies the client to upload more.

**Request:**

```json
{
  "identityPublicKey": "base64-encoded-identity-public-key",
  "signedPreKey": {
    "keyId": 42,
    "publicKey": "base64-encoded-signed-prekey-public",
    "signature": "base64-encoded-signature-by-identity-key"
  },
  "oneTimePreKeys": [
    { "keyId": 101, "publicKey": "base64-encoded-otpk-101" },
    { "keyId": 102, "publicKey": "base64-encoded-otpk-102" },
    { "keyId": 103, "publicKey": "base64-encoded-otpk-103" }
  ]
}
```

**Response (200 OK):**

```json
{
  "userId": "usr-9d4e-a1b2-uuid",
  "storedOneTimePreKeys": 103,
  "signedPreKeyId": 42,
  "updatedAt": 1718451200000
}
```

---

### `GET /keys/{userId}/prekey`

Fetch a user's pre-key bundle to establish an E2E encrypted session. This is called when Alice wants to message Bob for the first time (or after session reset). The server returns one identity key, one signed pre-key, and one one-time pre-key (consumed on fetch — deleted from server).

**Response (200 OK):**

```json
{
  "userId": "usr-5678-efgh-uuid",
  "identityPublicKey": "base64-encoded-identity-public-key",
  "signedPreKey": {
    "keyId": 38,
    "publicKey": "base64-encoded-signed-prekey-public",
    "signature": "base64-encoded-signature"
  },
  "oneTimePreKey": {
    "keyId": 77,
    "publicKey": "base64-encoded-otpk-77"
  },
  "deviceId": "b4c5d6e7-uuid"
}
```

**If no one-time pre-keys remain:**

```json
{
  "userId": "usr-5678-efgh-uuid",
  "identityPublicKey": "base64-encoded-identity-public-key",
  "signedPreKey": {
    "keyId": 38,
    "publicKey": "base64-encoded-signed-prekey-public",
    "signature": "base64-encoded-signature"
  },
  "oneTimePreKey": null,
  "deviceId": "b4c5d6e7-uuid"
}
```

The session can still be established without a one-time pre-key (X3DH degrades gracefully), but forward secrecy for the first message is reduced.

---

### `GET /keys/{userId}/identity`

Get a user's identity key for safety number verification. Users compare safety numbers out-of-band (QR code scan, read digits aloud) to verify E2E encryption is not being intercepted.

**Response (200 OK):**

```json
{
  "userId": "usr-5678-efgh-uuid",
  "identityPublicKey": "base64-encoded-identity-public-key",
  "identityKeyFingerprint": "45321 98765 23456 78901 34567 89012",
  "keyChangedAt": "2025-03-01T08:00:00Z"
}
```

---

## 11. Admin/Ops APIs (Internal)

Internal operational endpoints. Not exposed to clients. Used by infrastructure tooling, monitoring, and deployment systems.

---

### `GET /health`

Liveness check.

**Response (200 OK):**

```json
{
  "status": "HEALTHY",
  "version": "2.24.1",
  "uptime": "14d 6h 23m",
  "timestamp": 1718451200000
}
```

---

### `GET /metrics`

Prometheus-compatible metrics endpoint.

**Response (200 OK):**

```json
{
  "activeConnections": 487293,
  "messagesPerSecond": 1152847,
  "messageDeliveryLatencyP50Ms": 42,
  "messageDeliveryLatencyP95Ms": 128,
  "messageDeliveryLatencyP99Ms": 312,
  "offlineQueueDepth": 2847561,
  "mediaUploadsPerSecond": 75214,
  "errorRate": 0.0012,
  "gatewayServers": {
    "total": 842,
    "healthy": 840,
    "draining": 2
  }
}
```

---

### `POST /config/feature-flags`

Update feature flags for gradual rollouts and kill switches.

**Request:**

```json
{
  "flags": {
    "multi_device_enabled": {
      "enabled": true,
      "rolloutPercentage": 25,
      "targetRegions": ["US", "EU"]
    },
    "video_call_max_participants": {
      "value": 8
    },
    "new_encryption_protocol": {
      "enabled": false,
      "rolloutPercentage": 0
    }
  }
}
```

**Response (200 OK):**

```json
{
  "updated": 3,
  "effectiveAt": "2025-06-15T12:00:00Z",
  "propagationEstimateSeconds": 30
}
```

---

## Contrast with Slack/Telegram/Discord

### Slack

| Aspect | WhatsApp | Slack |
|---|---|---|
| **Identity** | Phone number | Email (workspace-based) |
| **API style** | Minimal REST + WebSocket for real-time | Rich REST API (Web API) + Events API + Socket Mode for real-time |
| **Encryption** | E2E by default (Signal Protocol). Server is blind | TLS in transit, at-rest on server. No E2E. Enterprise compliance requires server-side access |
| **Message model** | Encrypted blob. Server stores opaque ciphertext | Plaintext. Server stores, indexes, and makes searchable |
| **Channels/Groups** | Groups up to 1024 members, flat | Channels (public/private), threads, shared channels across workspaces |
| **Integrations** | None (no bot API) | Extensive: Slack Apps, Workflows, incoming/outgoing webhooks, slash commands |
| **Message persistence** | Transient relay — deleted after delivery | Permanent storage. Full history search. Compliance exports |
| **Typing indicators** | WebSocket push, ephemeral | REST-based (`/users.typing`), also pushed via Events API |

### Telegram

| Aspect | WhatsApp | Telegram |
|---|---|---|
| **Identity** | Phone number only | Phone number + username |
| **Encryption** | E2E by default for all chats | Server-side encryption by default. E2E only in "Secret Chats" |
| **Protocol** | Custom XMPP-derived / WebSocket | MTProto 2.0 (custom binary protocol) |
| **Group size** | Up to 1024 members | Up to 200,000 members. Channels: unlimited subscribers |
| **Storage** | Server is transient relay | Cloud-based. All messages stored on server permanently |
| **Bot platform** | No | Extensive Bot API (HTTP-based, long polling or webhooks) |
| **Media** | E2E encrypted blobs, CDN delivery | Server-side storage in plaintext (not E2E), Telegram CDN |
| **Multi-device** | Phone + 4 linked devices (added 2021). Hard because of E2E | Seamless. Any device, any time. Easy because messages stored on server |

### Discord

| Aspect | WhatsApp | Discord |
|---|---|---|
| **Identity** | Phone number | Username + email |
| **Model** | 1:1 chats + small groups | Servers (communities) with channels, roles, permissions |
| **Encryption** | E2E by default | None. TLS in transit only |
| **Voice/Video** | 1:1 or small group calls (WebRTC P2P) | Persistent voice channels (server-relayed media via SFU), screen sharing |
| **Group size** | 1024 members max | Servers: millions of members. Voice channels: limited concurrent users |
| **Fan-out** | Write-time (bounded by 1024 max) | Read-time. Messages stored once per channel; each reader fetches from channel log |
| **Tech stack** | Erlang/BEAM VM | Elixir/BEAM VM (similar philosophy, different language) |
| **Message persistence** | Transient | Permanent. Full search. Pins, reactions, threads |

---

## Interview Subset — Which APIs to Focus On

In a 45-60 minute system design interview, you cannot cover all 11 API groups. Focus on the **critical path** — the APIs that exercise the most interesting architectural decisions.

### Must-Cover (Phase 3 of the interview) ⭐

| API | Why it matters |
|---|---|
| **`POST /messages/send`** | THE critical path. Forces discussion of: E2E encryption (payload is opaque blob), idempotency (client-generated message ID), delivery guarantees (202 Accepted, async delivery), server-assigned sequence numbers for ordering |
| **`WS /ws/connect` + frames** | Persistent connection management. Forces discussion of: stateful gateway servers, connection registry, heartbeat/keepalive, offline sync on reconnect, NAT traversal on mobile |
| **`GET /messages/{conversationId}`** | Pagination design. Forces discussion of: cursor-based vs offset-based, storage partitioning by conversationId, sequence number ordering |
| **`PUT /messages/{messageId}/status`** | Delivery receipts. Forces discussion of: status state machine (SENT -> DELIVERED -> READ), asynchronous receipt propagation, group delivery tracking (per-member) |
| **`POST /groups/{groupId}/messages`** | Fan-out. Forces discussion of: write-time vs read-time fan-out, Sender Keys for group E2E, bounded write amplification (1024 max) |
| **`PUT /presence`** | Presence at scale. Forces discussion of: lazy presence updates, subscription model, eventual consistency, throttling rapid toggles |

### Good to Mention (shows breadth)

| API | When to bring it up |
|---|---|
| **`POST /media/upload`** | When discussing media separation from messages. Mention chunked/resumable uploads, E2E encryption before upload, thumbnail preview |
| **`POST /keys/prekeys`** | When the interviewer asks about E2E encryption mechanics. Shows you understand the Signal Protocol key exchange |
| **`POST /contacts/sync`** | When discussing contact discovery. Mention hashed phone numbers, privacy concerns, enumeration attacks |
| **`POST /calls/initiate`** | When asked "what about voice/video?" — shows you know this is WebRTC signaling only, not media relay |

### Skip Unless Asked

| API | Why skip |
|---|---|
| Auth APIs | Standard OAuth/JWT flow — nothing architecturally interesting for the chat design |
| Status/Stories APIs | Separate feature, not core to the messaging pipeline |
| Admin/Ops APIs | Operational — mention monitoring exists but do not design the endpoints |
| Feature flags | Implementation detail — mention it in the context of gradual rollouts |

### The 60-Second API Pitch

> "The two most important surfaces are the **message send** REST endpoint and the **WebSocket connection**. Message send accepts an E2E encrypted blob, assigns a server timestamp and sequence number, and returns 202 Accepted — delivery is async. The WebSocket is the real-time delivery channel: the server pushes messages, receipts, and typing indicators to connected clients. For offline users, messages queue until reconnect. Groups fan out on write — one send becomes N WebSocket pushes. Everything is encrypted end-to-end; the server only sees metadata."
