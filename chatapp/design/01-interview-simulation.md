# System Design Interview Simulation: Design WhatsApp (Real-Time Chat Application)

> **Interviewer:** Principal Engineer (L8)
> **Candidate:** SDE-3 / L6
> **Duration:** ~60 minutes
> **Date:** 2026-02-20

---

## PHASE 1: Opening & Problem Statement (~2 min)

**Interviewer:**

Let's design a messaging application like WhatsApp. We want to support real-time 1:1 and group messaging with strong privacy guarantees. Where would you like to start?

---

## PHASE 2: Requirements Gathering & Scoping (~5 min)

**Candidate:**

> Before jumping into design, I want to make sure we scope this properly. Let me break this into functional and non-functional requirements.
>
> **Functional Requirements:**
> - **1:1 messaging**: Send and receive text messages in real-time between two users
> - **Group messaging**: Create groups, add/remove members, send messages to all group members (up to 1,024 members)
> - **Media sharing**: Send images, videos, audio messages, and documents
> - **Delivery receipts**: Sent → Delivered → Read status for every message
> - **Presence**: Online/offline status, last seen timestamp
> - **Typing indicators**: Show when the other person is typing
> - **Push notifications**: Notify offline users of new messages
> - **End-to-end encryption**: Server must NEVER see plaintext message content
> - **Offline message delivery**: Messages queued while user is offline, delivered on reconnect
> - **Multi-device**: Phone + up to 4 companion devices (web, desktop)
>
> **Out of scope for this interview** (but worth mentioning): voice/video calls (WebRTC signaling), status/stories, payments, business API.

**Interviewer:**

Good scoping. What about non-functional requirements? What are the key constraints?

**Candidate:**

> | Dimension | Requirement | Rationale |
> |-----------|-------------|-----------|
> | **Latency** | Sub-200ms message delivery for online recipients | Chat must feel instant — anything over 500ms feels broken |
> | **Reliability** | At-least-once delivery with exactly-once semantics at client | Zero message loss — users trust chat as a reliable channel |
> | **Ordering** | Messages within a conversation appear in consistent order for all participants | Out-of-order messages in a group would be confusing |
> | **Availability** | 99.99% uptime (< 53 min downtime/year) | Messaging is critical communication infrastructure |
> | **Privacy** | E2E encryption — server is an untrusted relay | This is WhatsApp's core value proposition and defining architectural constraint |
> | **Scale** | 2B+ users, ~100B messages/day, 50-100M concurrent connections | WhatsApp-scale numbers; drives all infrastructure decisions |
> | **Efficiency** | Minimal battery/bandwidth on mobile devices | Users are mostly on mobile, often on poor networks |
> | **Consistency** | Eventual consistency for presence/last-seen; strong ordering per conversation | Slight staleness in presence is OK; message ordering is not negotiable |
>
> **Back-of-envelope math:**
> - 100B messages/day ÷ 86,400 sec = **~1.15M messages/second** average
> - Peak = 3-5x average = **~3.5-5.7M messages/second**
> - Average message ~1 KB encrypted → **~1.15 GB/sec** write throughput
> - 50M concurrent connections × ~100 bytes state each = **~5 GB** connection registry
> - 6.5B media items/day × ~200 KB average = **~1.3 PB/day** media throughput
>
> The key insight: this is a **connection-heavy, write-heavy, latency-sensitive** system. The primary challenge is not storage — it's managing millions of persistent connections and routing messages between them in real time. And everything is complicated by E2E encryption, which means the server can never inspect or index message content.
>
> **Contrast with Slack:** Slack is enterprise-focused, workspace-centric, stores all messages permanently on the server (searchable, compliant). WhatsApp is consumer-focused, phone-number-based, and the server is a transient relay that deletes messages after delivery. These are fundamentally different architectures driven by different threat models and user expectations.

---

### L5 vs L6 vs L7 — Phase 2 Rubric

| Aspect | L5 (SDE-2) | L6 (SDE-3) | L7 (Principal) |
|--------|------------|------------|-----------------|
| **Functional Reqs** | Lists basic features: send messages, groups, media | Adds E2E encryption as a core requirement, delivery receipts, multi-device, offline delivery | Frames E2E encryption as THE defining architectural constraint that shapes every other decision |
| **Non-Functional** | "Low latency, high availability" | Quantifies: sub-200ms delivery, 99.99% uptime, 1.15M msg/sec | Adds battery/bandwidth constraints for mobile, explains why eventual consistency is acceptable for presence but not for message ordering |
| **Scale Math** | "Lots of messages" | Calculates 1.15M msg/sec, connection registry size, media throughput | Identifies the system as connection-heavy (not storage-heavy), explains how E2E encryption limits server-side capabilities |
| **Contrast** | None | Contrasts with Slack (persistent store vs transient relay) | Contrasts Slack, Telegram, Discord — explains how different business models drive different architectures |

---

## PHASE 3: API Design (~5 min)

**Interviewer:**

Let's design the key APIs. Focus on the most critical paths.

**Candidate:**

> I'll focus on four critical API groups: message sending (the hot path), real-time connection, message history, and presence. Full API reference is in `02-api-contracts.md`.
>
> **1. Message Send — the most latency-critical path**
>
> ```
> POST /messages/send
> Headers: Authorization: Bearer <token>
>
> Request:
> {
>   "conversationId": "conv_abc123",
>   "messageId": "msg_uuid_client_generated",    // Client-generated UUID for idempotency
>   "type": "text | image | video | audio | document | location",
>   "encryptedPayload": "<base64 E2E encrypted blob>",
>   "timestamp": 1708444800000,
>   "mediaRef": {                                 // Only for media messages
>     "mediaId": "media_xyz",
>     "encryptedThumbnail": "<base64>",
>     "mimeType": "image/jpeg",
>     "fileSize": 245760
>   }
> }
>
> Response (202 Accepted):
> {
>   "messageId": "msg_uuid_client_generated",
>   "serverTimestamp": 1708444800042,
>   "sequenceNumber": 48291                       // Server-assigned ordering
> }
> ```
>
> Key design decisions:
> - **Client-generated messageId** — enables idempotent retries. If the client doesn't get a response (network drop), it retries with the same ID. Server deduplicates.
> - **202 Accepted** (not 200 OK) — the server accepted the message for delivery, but hasn't delivered it yet. Actual delivery confirmation comes via a separate delivery receipt pushed over WebSocket.
> - **encryptedPayload** — server never sees plaintext. This is a blob encrypted with the recipient's session key (Signal Protocol). The server just stores and forwards.
> - **sequenceNumber** — server-assigned monotonically increasing number per conversation. This is the source of truth for ordering. Clients use sequence numbers to detect gaps and request re-delivery.
>
> **2. Real-Time Connection — WebSocket**
>
> ```
> WS /ws/connect
> Headers: Authorization: Bearer <token>
>          X-Last-Sequence: {lastKnownSequencePerConversation}
>
> // Bidirectional frames:
>
> // Server → Client: new message
> {
>   "type": "message",
>   "conversationId": "conv_abc123",
>   "messageId": "msg_xyz",
>   "senderId": "user_456",
>   "encryptedPayload": "<base64>",
>   "sequenceNumber": 48292,
>   "serverTimestamp": 1708444800100
> }
>
> // Client → Server: ACK (delivery confirmation)
> {
>   "type": "ack",
>   "messageId": "msg_xyz"
> }
>
> // Server → Client: delivery receipt
> {
>   "type": "receipt",
>   "messageId": "msg_abc",
>   "status": "delivered" | "read",
>   "userId": "user_789",
>   "timestamp": 1708444800200
> }
>
> // Client → Server: typing indicator
> {
>   "type": "typing",
>   "conversationId": "conv_abc123",
>   "status": "started" | "stopped"
> }
>
> // Server → Client: presence update
> {
>   "type": "presence",
>   "userId": "user_456",
>   "status": "online" | "offline",
>   "lastSeen": 1708444800000
> }
>
> // Bidirectional: heartbeat
> {
>   "type": "ping" | "pong"
> }
> ```
>
> Why WebSocket over HTTP long-polling or SSE:
> - **Bidirectional** — both client and server push data. SSE is server-to-client only. Long-polling wastes bandwidth and adds latency.
> - **Low overhead** — after handshake, each frame has only 2-6 bytes overhead (vs HTTP headers on every poll).
> - **Sub-100ms delivery** — message is pushed the instant the server receives it. No polling interval delay.
> - WhatsApp actually uses a custom XMPP-derived protocol over persistent TCP, but WebSocket is the closest standard equivalent for our design.
>
> **3. Message History — Cursor-based Pagination**
>
> ```
> GET /messages/{conversationId}?before={sequenceNumber}&limit=50
>
> Response:
> {
>   "messages": [
>     {
>       "messageId": "msg_abc",
>       "senderId": "user_123",
>       "encryptedPayload": "<base64>",
>       "sequenceNumber": 48241,
>       "serverTimestamp": 1708440000000,
>       "type": "text"
>     },
>     ...
>   ],
>   "hasMore": true,
>   "oldestSequenceNumber": 48192
> }
> ```
>
> Cursor-based (not offset-based) pagination using sequence numbers. Why: offset pagination breaks when new messages arrive (items shift). Cursor-based is stable regardless of new writes.
>
> **4. Presence**
>
> ```
> PUT /presence
> {
>   "status": "online",
>   "lastSeen": 1708444800000
> }
>
> GET /presence/{userId}
> Response:
> {
>   "userId": "user_456",
>   "status": "offline",
>   "lastSeen": 1708430000000
> }
> ```
>
> Presence is also pushed via WebSocket (see above), but this REST endpoint allows on-demand queries when opening a chat.

**Interviewer:**

Why is `messageId` client-generated rather than server-generated?

**Candidate:**

> Critical for reliability on unreliable mobile networks. Consider the failure case: client sends a message, server receives and stores it, but the response is lost (network drop). The client doesn't know if the message was sent. With a server-generated ID, the client has no way to retry safely — it might create a duplicate. With a client-generated UUID, the client retries with the same ID, and the server deduplicates on messageId. This gives us **idempotent retries** — the foundation of at-least-once delivery with exactly-once semantics.
>
> **Contrast with Slack:** Slack also uses client-generated nonces for deduplication, but Slack's architecture is different — messages go through a REST API, are persisted to MySQL, and then fan out via their pub/sub system. WhatsApp's messages go through WebSocket, are stored transiently, and are deleted after delivery. The idempotency key serves the same purpose but in different architectural contexts.

---

### L5 vs L6 vs L7 — Phase 3 Rubric

| Aspect | L5 (SDE-2) | L6 (SDE-3) | L7 (Principal) |
|--------|------------|------------|-----------------|
| **API Design** | REST endpoints for send/receive, basic request/response | WebSocket for real-time + REST for history, explains why 202 vs 200, cursor pagination | Discusses protocol choice tradeoffs (WebSocket vs XMPP vs MQTT), explains how E2E encryption constrains API design |
| **Idempotency** | "Use a unique ID" | Client-generated UUID, explains the network-drop retry scenario, server deduplicates | Discusses exactly-once semantics at application level, how ACK protocol interacts with idempotency |
| **Message Ordering** | "Sort by timestamp" | Server-assigned sequence numbers, explains why client timestamps are unreliable (clock skew) | Discusses causal ordering in groups, vector clocks vs sequence numbers tradeoff |
| **Encryption** | "Encrypt the payload" | encryptedPayload is opaque blob, server stores and forwards without reading | Explains how E2E limits server-side search, spam detection, and content moderation — the operational cost of privacy |

---

## PHASE 4: High-Level Architecture — Iterative Build-Up (~15 min)

**Interviewer:**

Let's build the architecture. Start simple and evolve.

### Attempt 0: Single Server with HTTP Polling

**Candidate:**

> Let me start with the simplest possible thing that could work.
>
> ```
> +--------+     HTTP POST /send      +------------------+
> | Client | -----------------------> |                  |
> | (Send) |                          |   Single Server  |
> +--------+                          |   (Node.js/etc)  |
>                                     |                  |
> +--------+     HTTP GET /poll       |   + PostgreSQL   |
> | Client | -----------------------> |     (messages    |
> | (Recv) | <-- messages (or empty)  |      table)      |
> +--------+                          +------------------+
> ```
>
> Sender sends a POST request with the message. Server stores it in a SQL table. Receiver polls every 2-3 seconds with GET to check for new messages. Server queries `SELECT * FROM messages WHERE recipient = ? AND delivered = false`.
>
> This "works" in the sense that messages get from A to B. But it's terrible.
>
> | Problem | Impact |
> |---------|--------|
> | High latency (2-3 second polling interval) | Chat feels sluggish — not "real-time" |
> | Wasted bandwidth (most polls return empty) | At 100M users polling every 3s = 33M requests/sec doing nothing |
> | Server overwhelmed by poll requests | 33M QPS just for empty polls — the DB can't handle this |
> | No encryption | Server stores and reads plaintext messages — privacy disaster |
> | No delivery confirmation | Sender doesn't know if message was delivered or read |
> | Single point of failure | Server dies = entire system is down |
> | No media support | Only text messages |
> | SQL database won't scale | Single PostgreSQL can't handle 1M+ writes/sec |

**Interviewer:**

The polling overhead alone would kill this. How do we fix the real-time problem?

### Attempt 1: Persistent Connections (WebSocket) + Message Queue

**Candidate:**

> The fundamental problem is polling. We need the server to **push** messages to clients instead of clients pulling. This means persistent connections.
>
> ```
> +--------+    WebSocket     +------------------+     +---------------+
> | Client | <=============> |   Server         |     |  Message      |
> | (Send) |    (persistent) |   (WebSocket     | --> |  Queue        |
> +--------+                 |    handler)      |     |  (in-memory)  |
>                            |                  |     +---------------+
> +--------+    WebSocket     |                  |
> | Client | <=============> |   + SQL DB       |
> | (Recv) |    (persistent) |   (messages)     |
> +--------+                 +------------------+
> ```
>
> Replace HTTP polling with WebSocket connections. Client connects once, keeps the connection open. When a message arrives for a user, the server pushes it immediately over the WebSocket.
>
> Add a message queue: if the recipient is offline (no active connection), the message goes into a queue. When the recipient connects, the server drains the queue and delivers all pending messages.
>
> **What's better:**
> - Sub-100ms delivery (push, not poll)
> - No wasted bandwidth (data flows only when there's something to send)
> - Server resources proportional to actual traffic, not polling frequency
>
> **Contrast with Telegram:** Telegram uses a custom protocol (MTProto) over TCP. The principle is the same — persistent connections for real-time push. But Telegram's MTProto handles encryption at the transport layer (client-to-server), not E2E. Different encryption model, same connection model.
>
> | Problem | Impact |
> |---------|--------|
> | Single server handles all connections | One server can handle ~100K WebSocket connections at most — we need 50M+ |
> | No encryption | Server still reads plaintext messages |
> | No group messaging | Can only do 1:1 |
> | No media support | Text only |
> | Single point of failure | Server dies = all connections lost, all messages lost |
> | In-memory queue loses data on crash | Undelivered messages are gone if server restarts |

**Interviewer:**

100K connections on one server vs 50M needed — that's a 500x gap. How do we scale the connection layer?

### Attempt 2: Gateway Server Fleet + Message Routing + Offline Delivery

**Candidate:**

> We need to split the system into layers: a **connection layer** (gateway servers) that handles WebSocket connections, a **routing layer** that knows which user is on which gateway, and a **persistence layer** for offline messages.
>
> ```
>                              +-------------------+
>                              |   Connection      |
>                              |   Registry        |
>                              |   (Redis Cluster) |
>                              |  userId→gateway   |
>                              +-------------------+
>                                    ^     |
>                     register       |     | lookup
>                                    |     v
> +--------+  WS   +-----------+   +-----------+   +-----------+  WS   +--------+
> | Sender | ====> | Gateway   |-->| Message   |-->| Gateway   | ====> | Recvr  |
> | Client |       | Server 1  |   | Router    |   | Server 2  |       | Client |
> +--------+       +-----------+   | (Stateless|   +-----------+       +--------+
>                                  |  layer)   |
>                                  +-----------+
>                                       |
>                               offline |
>                                       v
>                              +-------------------+     +---------+
>                              |  Offline Message  |     |  Push   |
>                              |  Queue            |     |  Notif  |
>                              |  (Cassandra)      |     |  (APNs/ |
>                              +-------------------+     |   FCM)  |
>                                                        +---------+
> ```
>
> **Gateway servers**: Each handles 100K-500K WebSocket connections. Stateful (holds connections). Scale by adding more servers. WhatsApp's Erlang-based servers reportedly handled ~2 million connections per server thanks to BEAM VM's lightweight processes — each connection is an Erlang process at ~2 KB overhead.
>
> **Connection registry** (Redis cluster): Maps `userId → {gatewayServerId, connectionId}`. When Gateway 1 receives a message for user B, the router looks up user B's gateway (Gateway 2) and forwards the message.
>
> **Message router**: Stateless layer between gateways. Receives a message, looks up recipient's gateway in the registry, forwards it. If recipient is offline (not in registry) → stores in the offline message queue and sends push notification.
>
> **Offline message queue** (Cassandra): Durable storage for undelivered messages. When user reconnects, gateway drains the queue. Messages are deleted after delivery ACK.
>
> **Push notifications**: APNs (iOS) / FCM (Android) for offline users. Notification says "New message from Alice" — NOT the actual content (it's E2E encrypted, the push service can't see it).
>
> **What's better:**
> - Scales to 50M+ connections (100-500 gateway servers)
> - Offline delivery with durability (Cassandra, not in-memory)
> - Push notifications wake up mobile devices
> - Stateless routing layer scales independently
>
> **Contrast with Slack:** Slack uses a similar gateway architecture (they call them "Gateway Servers" too) with Channel Servers for pub/sub fan-out. But Slack routes through workspace-scoped channels, while WhatsApp routes per-user. Slack's Gateway Servers maintain channel subscriptions; WhatsApp's maintain per-user connections.
>
> | Problem | Impact |
> |---------|--------|
> | Messages are plaintext on server | Privacy disaster — any server compromise exposes all messages |
> | No group chat | Can only do 1:1 messaging |
> | Media sent inline with messages | Slow — a 5 MB image blocks the WebSocket connection |
> | Connection registry is a hot spot | Every message send requires a registry lookup — Redis becomes the bottleneck |
> | Single data center | DC goes down = entire system is offline |

**Interviewer:**

The plaintext problem is critical for a WhatsApp-like system. How do you solve privacy?

### Attempt 3: E2E Encryption + Group Messaging + Media Separation

**Candidate:**

> This is where the architecture fundamentally diverges from Slack/Discord. We make the server an **untrusted relay** — it stores and forwards encrypted blobs it cannot read.
>
> ```
>                    +--------------------+
>                    |   Pre-Key Server   |
>                    |   (Identity keys,  |
>                    |    signed pre-keys,|
>                    |    one-time keys)  |
>                    +--------------------+
>                            |
>              key exchange  |  (X3DH)
>                            v
> +--------+  E2E encrypted  +-----------+   +-----------+   +-----------+  decrypt
> | Sender | ===============>| Gateway 1 |-->| Router    |-->| Gateway 2 |========> | Recvr |
> | Client |  (Signal Proto) |           |   |           |   |           |          +-------+
> +--------+                 +-----------+   +-----------+   +-----------+
>                                                 |
>                                                 | media ref
>                                                 v
>                                        +------------------+
>                                        |   Media Store    |
>                                        |   (S3 / Blob)    |
>                                        |   encrypted blobs|
>                                        +------------------+
>
> Group fan-out (write-time):
>
>  Sender ──> Server ──┬──> Member 1 inbox
>                      ├──> Member 2 inbox
>                      ├──> Member 3 inbox
>                      └──> ... (up to 1024)
> ```
>
> **E2E Encryption (Signal Protocol):**
> - **X3DH key exchange**: When Alice first messages Bob, she fetches Bob's pre-key bundle (identity key + signed pre-key + one-time pre-key) from the pre-key server. Performs three Diffie-Hellman computations to derive a shared secret. One-time pre-key is consumed (deleted from server).
> - **Double Ratchet**: After X3DH, every message uses a new encryption key. Symmetric ratchet (KDF chain) for consecutive messages; DH ratchet when conversation turns change. Provides forward secrecy — compromising today's key reveals nothing about past messages.
> - **Server sees only**: `{messageId, conversationId, senderId, recipientId, encryptedBlob, timestamp, sequenceNumber}`. It cannot read the content.
>
> **Group messaging (fan-out on write):**
> - Sender sends one encrypted message → server fans out to each member's inbox (up to 1024 copies).
> - Uses **Sender Keys** for group E2E encryption: each member generates a sender key, distributes it to group members via pairwise-encrypted channels. Sender encrypts once with their sender key → O(1) encryption per message.
> - Why fan-out on write (not read)? WhatsApp groups max at 1,024 members — bounded write amplification. Read latency is critical for real-time chat. Discord uses fan-out on read because servers can have millions of members — unbounded fan-out on write would be catastrophic.
>
> **Media separation:**
> - Client encrypts media with a random AES-256 key → uploads encrypted blob to blob store → gets mediaId → sends message with `{mediaId, encryptionKey, thumbnail}`. Recipient downloads blob, decrypts with key from message.
> - Why separate? Decouples message delivery from media download. Message arrives instantly (small payload); media loads in background. If a user never opens the chat, media is never downloaded — saves bandwidth.
>
> **What's better:**
> - Server is an untrusted relay — zero-knowledge of message content
> - Group messaging with bounded fan-out
> - Media doesn't block message delivery
> - Forward secrecy via Double Ratchet
>
> **Contrast with Telegram:** Telegram does NOT use E2E encryption by default. Regular chats are client-to-server encrypted (Telegram can read them). Only "Secret Chats" use E2E. This is a deliberate trade-off: Telegram sacrifices privacy for cloud sync (messages accessible from any device, searchable from the server). WhatsApp sacrifices cloud sync for privacy (multi-device was extremely hard to add because of E2E).
>
> | Problem | Impact |
> |---------|--------|
> | No presence / typing indicators | Users can't see if contacts are online or typing |
> | Connection registry is still a potential bottleneck | High-QPS Redis cluster needs careful sharding |
> | No multi-device support | E2E encryption is tied to one device — how do linked devices work? |
> | Single data center | DC failure = total outage |
> | No monitoring or chaos testing | No visibility into message delivery health |

**Interviewer:**

Good. Let's add the real-time UX features — presence and typing.

### Attempt 4: Presence System + Typing Indicators + Contact Sync

**Candidate:**

> ```
>                              +-------------------+
>                              |  Presence Store   |
>                              |  (Redis)          |
>                              |  userId→{status,  |
>                              |   lastSeen}       |
>                              +-------------------+
>                                    ^       |
>                       update       |       | subscribe
>                                    |       v
> +--------+  WS   +-----------+   +-----------+   +-----------+  WS   +--------+
> | Client | ====> | Gateway 1 |-->| Router +  |-->| Gateway 2 | ====> | Client |
> |        |       |           |   | Presence  |   |           |       |        |
> +--------+       +-----------+   | Manager   |   +-----------+       +--------+
>                                  +-----------+
>                                       |
>                              +-------------------+
>                              | Contact Sync Svc  |
>                              | (hash-based phone |
>                              |  number matching)  |
>                              +-------------------+
> ```
>
> **Presence system:**
> - Tracks online/offline/last-seen for every user. Stored in Redis (fast reads, TTL-based expiry).
> - On WebSocket connect: mark online. On disconnect (or heartbeat timeout): mark offline + update lastSeen.
> - **Lazy presence** (critical optimization): Don't push presence updates to ALL contacts. Only push to users who currently have this contact's chat open. User with 500 contacts → only 2-3 have the chat open → 2-3 pushes instead of 500. Reduces fan-out by 99%.
> - **Throttling**: Coalesce rapid online/offline toggles. User walking through spotty coverage → don't send 20 presence flips in a minute. Debounce: only emit presence change if status has been stable for 5+ seconds.
>
> **Typing indicators:**
> - Ephemeral — not persisted, not stored anywhere. Sent via WebSocket with a 3-5 second TTL.
> - Client sends "typing started" → server forwards to the other party → if no refresh within 5 seconds → indicator disappears.
> - Best-effort delivery — losing a typing indicator is harmless (unlike losing a message).
>
> **Contact sync:**
> - Client uploads hashed phone numbers (SHA-256) → server matches against registered users → returns matches.
> - Privacy consideration: hashing alone isn't sufficient (phone numbers have low entropy — rainbow table attack is trivial). WhatsApp uses additional techniques. For interview purposes, hash-based matching is a reasonable starting point with the caveat that production systems need more sophisticated private set intersection.
>
> **What's better:**
> - Real-time presence with minimal fan-out (lazy presence)
> - Typing indicators for conversational UX
> - Contact discovery without uploading raw phone numbers
>
> **Contrast with Discord:** Discord has rich presence (shows what game you're playing, what you're listening to). Discord pushes presence to entire server member lists — much larger fan-out than WhatsApp's contact-based model. Discord mitigates this with guild-scoped presence subscriptions and rate limiting.
>
> | Problem | Impact |
> |---------|--------|
> | Single data center | DC failure = total outage for all users |
> | Gateway server failure loses all its connections | 500K users suddenly disconnected, no graceful failover |
> | No chaos testing | Unknown failure modes lurking |
> | No monitoring | No visibility into delivery latency, queue depths, error rates |
> | No multi-device support | Can't use WhatsApp on web and phone simultaneously with E2E |

**Interviewer:**

Let's harden this for production. How do we handle failures at scale?

### Attempt 5: Production Hardening (Multi-DC, Fault Tolerance, Monitoring)

**Candidate:**

> This is where we go from "works on my laptop" to "serves 2 billion users across the planet."
>
> ```
> +==================== Data Center 1 (US-East) ====================+
> |                                                                  |
> |  +----------+  +----------+  +----------+                       |
> |  | Gateway  |  | Gateway  |  | Gateway  |  (100K-2M conns each)|
> |  | Server 1 |  | Server 2 |  | Server N |                       |
> |  +----+-----+  +----+-----+  +----+-----+                       |
> |       |             |             |                              |
> |       v             v             v                              |
> |  +----------------------------------------+                     |
> |  |        Message Router (Stateless)       |                     |
> |  +----+-----------------------------------+                     |
> |       |                                                          |
> |  +----v-----------+  +----------------+  +------------------+   |
> |  | Connection     |  | Offline Msg    |  | Pre-Key Server   |   |
> |  | Registry       |  | Queue          |  | (E2E keys)       |   |
> |  | (Redis Cluster)|  | (Cassandra)    |  |                  |   |
> |  +----------------+  +----------------+  +------------------+   |
> |       |                    |                                     |
> +======|====================|=====================================+
>        |                    |
>        |    Cross-DC Replication (async)
>        |                    |
> +======|====================|=====================================+
> |       |                    |                                     |
> |  +----v-----------+  +----v-----------+  +------------------+   |
> |  | Connection     |  | Offline Msg    |  | Pre-Key Server   |   |
> |  | Registry       |  | Queue          |  | (E2E keys)       |   |
> |  | (Redis Cluster)|  | (Cassandra)    |  |                  |   |
> |  +----------------+  +----------------+  +------------------+   |
> |       ^             ^             ^                              |
> |       |             |             |                              |
> |  +----+-----+  +----+-----+  +----+-----+                       |
> |  | Gateway  |  | Gateway  |  | Gateway  |                       |
> |  | Server 1 |  | Server 2 |  | Server N |                       |
> |  +----------+  +----------+  +----------+                       |
> |                                                                  |
> +==================== Data Center 2 (EU-West) ====================+
>
>
>   Monitoring & Observability:
>   +------------+  +------------+  +------------+
>   | Metrics    |  | Alerting   |  | Distributed|
>   | (msg lat,  |  | (P99 > 1s, |  | Tracing    |
>   |  conn cnt, |  |  queue     |  | (per-msg   |
>   |  queue dep)|  |  growth)   |  |  flow)     |
>   +------------+  +------------+  +------------+
> ```
>
> #### Multi-DC Active-Active
> - Deploy across **multiple data centers** (at least 3 for quorum-based failover). All DCs serve live traffic simultaneously (active-active, not active-passive).
> - **Why active-active over active-passive?** Same reason as Netflix: standby DCs rot. If a DC never handles real traffic, you don't know if it actually works until it's too late. Active-active means every DC is tested continuously with real users.
> - **Cassandra** for offline message queue: multi-DC async replication is a first-class feature. Tunable consistency (LOCAL_QUORUM for writes within a DC, eventual consistency across DCs).
> - **Redis** for connection registry: replicated across DCs. If a DC fails, other DCs don't have that DC's connections (those users reconnect to a surviving DC), but they have the rest of the registry.
> - **Client failover**: If a DC goes down, clients lose their WebSocket connection. Client reconnects to a different DC (DNS-based or load-balancer-based failover). On reconnect, client provides last-known sequence numbers → server delivers missed messages from the offline queue.
>
> #### Erlang / BEAM VM
> - WhatsApp chose Erlang/OTP for their connection servers. Why?
>   - **Lightweight processes**: Each connection is an Erlang process (~2 KB memory). A single server can run millions of processes. Rick Reed's talk: WhatsApp achieved **2.8 million connections per server** on FreeBSD + BEAM.
>   - **Preemptive scheduling**: No connection can starve others. Important when some connections are active (sending) and most are idle (waiting).
>   - **Let-it-crash philosophy**: If a connection process crashes, only that one connection is affected. Supervisor trees automatically restart crashed processes. Perfect for unreliable mobile connections.
>   - **Hot code upgrades**: Deploy new code without dropping connections. Zero-downtime deploys.
> - **Contrast with Discord:** Discord chose Elixir (also BEAM VM) for the same reasons. Different language, same runtime. Discord scaled to 5M+ concurrent users per Elixir cluster. The BEAM VM is the secret weapon for connection-heavy systems.
>
> #### Multi-Device (E2E-compatible)
> - Each companion device gets its own **Identity Key pair**.
> - Primary device signs the companion's identity key (Account Signature). Companion signs the primary's key (Device Signature).
> - When sending a message, the **sender client** encrypts the message N times — once per recipient device. Server routes each encrypted copy to the correct device.
> - This preserves E2E: server never sees plaintext. Each device has an independent Double Ratchet session.
> - **1 phone + up to 4 companion devices** (web, desktop).
>
> #### Monitoring & Alerting
> - **Key metrics**: message delivery latency (P50, P95, P99), WebSocket connection count per gateway, offline queue depth per user, media upload/download latency, error rates per API.
> - **Alerts**: P99 delivery latency > 1 second, offline queue growing (delivery failures), gateway connection drops > threshold, Cassandra write latency spikes.
> - **Distributed tracing**: Trace a single message from sender → gateway → router → recipient gateway → client ACK. Identify bottlenecks.
>
> #### Chaos Testing
> - Kill random gateway servers during business hours → verify clients reconnect and no messages are lost.
> - Simulate DC failure → verify other DCs absorb traffic and offline queues drain correctly.
> - Kill Redis nodes → verify connection registry recovers via Cassandra fallback or re-registration.
> - Inspired by Netflix's Chaos Monkey — the same philosophy applies to chat infrastructure.
>
> #### Rate Limiting
> - Per-user message send rate (prevent spam bots)
> - Per-user connection rate (prevent connection flooding)
> - Per-group message rate (prevent broadcast spam)
> - Media upload size and rate limits
>
> **What's better:**
> - Survives DC failures (active-active, no single point of failure)
> - Erlang/BEAM handles millions of connections per server efficiently
> - Multi-device with E2E encryption
> - Observable: every message flow is traceable, every failure is detected
> - Chaos-tested: known failure modes, not surprises
>
> **Final architecture handles:**
> - 2B+ users, 100B+ messages/day
> - Sub-200ms delivery for online recipients
> - E2E encryption (Signal Protocol) — server is an untrusted relay
> - Groups up to 1,024 members with bounded fan-out
> - Media sharing with lazy loading
> - Presence and typing indicators with minimal fan-out
> - Multi-device with per-device encryption
> - Multi-DC active-active with automatic failover
> - Comprehensive monitoring and chaos testing

---

### Architecture Evolution Table

| Attempt | Key Addition | Problem Solved | Key Technology |
|---------|-------------|---------------|----------------|
| 0 | HTTP polling + SQL | Baseline — "simplest thing that works" | PostgreSQL, HTTP |
| 1 | WebSocket + message queue | Eliminated polling overhead, real-time push | WebSocket, in-memory queue |
| 2 | Gateway fleet + routing + offline delivery | Scaled connections to millions, durable offline delivery | Redis, Cassandra, APNs/FCM |
| 3 | E2E encryption + groups + media separation | Privacy (untrusted relay), group messaging, media decoupling | Signal Protocol, Sender Keys, S3 |
| 4 | Presence + typing + contact sync | Real-time UX features with minimal fan-out | Lazy presence, ephemeral typing, hash-based contact sync |
| 5 | Multi-DC + Erlang + monitoring + chaos | Production resilience, multi-device, observability | BEAM VM, active-active DC, distributed tracing |

---

### L5 vs L6 vs L7 — Phase 4 Rubric

| Aspect | L5 (SDE-2) | L6 (SDE-3) | L7 (Principal) |
|--------|------------|------------|-----------------|
| **Architecture Evolution** | Jumps to "gateway + queue + DB" in one step | Builds iteratively, each attempt motivated by concrete problems from the previous | Each attempt includes quantitative reasoning, contrast with alternatives, and operational concerns |
| **E2E Encryption** | "Encrypt the messages" | Explains Signal Protocol (X3DH, Double Ratchet), Sender Keys for groups, why server is untrusted | Discusses the operational cost of E2E (can't search, can't moderate, can't debug content), how multi-device was the hardest problem |
| **Connection Scaling** | "Add more servers" | Explains gateway fleet, connection registry, stateless routing | Discusses Erlang/BEAM as architectural choice, 2.8M connections/server, let-it-crash, hot code upgrades |
| **Fan-out** | "Send to all members" | Explains write-time vs read-time fan-out, why write-time for WhatsApp (bounded groups) | Compares WhatsApp (write fan-out, 1024 max) vs Discord (read fan-out, millions of members) vs Telegram channels (broadcast) |
| **Reliability** | "Replicate the database" | Active-active multi-DC, Cassandra async replication, client reconnect + sequence sync | Chaos testing methodology, explains why active-passive DCs rot, blast radius analysis |

---

## PHASE 5: Deep Dive — Message Delivery Pipeline (~8 min)

**Interviewer:**

Let's go deep on message delivery. Walk me through the exact path a message takes from sender to receiver, including failure cases.

**Candidate:**

> The full message delivery path for a 1:1 message:
>
> ```
> Sender Phone                    Server Infrastructure                 Recipient Phone
> ============                    ======================                ===============
>
> 1. Compose msg
> 2. Encrypt (Signal Proto)
>    - Get recipient's session
>    - Double Ratchet → new msg key
>    - AES-256 encrypt payload
> 3. Send via WebSocket ──────> 4. Gateway 1 receives
>                                5. Validate auth token
>                                6. Assign sequenceNumber
>                                7. Store in offline queue
>                                   (Cassandra) as insurance
>                                8. Look up recipient in
>                                   connection registry (Redis)
>                                   ┌─ ONLINE ──────────────> 9. Forward to Gateway 2
>                                   │                         10. Push via WebSocket ──> 11. Receive encrypted blob
>                                   │                                                    12. Decrypt (Double Ratchet)
>                                   │                                                    13. Display message
>                                   │                         14. Client sends ACK ────> 15. Gateway 2 receives ACK
>                                   │                                                    16. Delete from offline queue
>                                   │                         17. Send "delivered" receipt
>                                   │                             back to sender via WS
>                                   │
>                                   └─ OFFLINE ─────────────> 9. Message stays in
>                                                                offline queue
>                                                             10. Send push notification
>                                                                 (APNs/FCM) — metadata
>                                                                 only, NOT content
>                                                             ... later, recipient connects ...
>                                                             11. Drain offline queue
>                                                             12. Deliver all pending msgs
>                                                                 in sequence order
>                                                             13-17. Same ACK flow as above
> ```
>
> **Step 7 is critical** — we write to the offline queue BEFORE attempting real-time delivery. Why? If the gateway crashes between receiving the message and delivering it, the message is safe in Cassandra. The recipient will get it on reconnect. This is the "store-and-forward" pattern — the queue is our durability guarantee.
>
> **Failure cases:**
>
> 1. **Network drop after send, before server ACK**: Client retries with same messageId. Server deduplicates (idempotent write to Cassandra using messageId as primary key). No duplicate messages.
>
> 2. **Gateway crash after storing but before forwarding**: Message is in the offline queue. Recipient reconnects to a different gateway. New gateway drains the queue. Message delivered.
>
> 3. **Recipient's gateway crashes**: Recipient's WebSocket is lost. Recipient reconnects to a new gateway. Provides last-known sequence number. Server delivers all messages since that sequence.
>
> 4. **Cassandra write failure**: Server returns error to sender. Sender retries. We do NOT deliver a message we haven't durably stored — that would risk message loss.
>
> 5. **Message delivered but ACK lost**: Server doesn't receive ACK → retries delivery on next connection. Client deduplicates based on messageId. Slight duplicate delivery but no message loss.
>
> The principle: **never lose a message, tolerate duplicates at the transport level, deduplicate at the application level.**
>
> **Ordering guarantees**: Server-assigned sequence numbers per conversation. The server is the single source of truth for ordering within a conversation. Client detects gaps (missing sequence numbers) and requests re-delivery. For groups, a single sequence counter per group ensures all members see the same order.
>
> See `03-messaging-and-delivery.md` for the complete deep dive.

---

### L5 vs L6 vs L7 — Phase 5 Rubric

| Aspect | L5 (SDE-2) | L6 (SDE-3) | L7 (Principal) |
|--------|------------|------------|-----------------|
| **Delivery Path** | "Client sends, server forwards" | Full step-by-step flow with store-and-forward, explains why write-to-queue-first | Discusses latency budget per step, identifies that Redis lookup is on the critical path |
| **Failure Handling** | "Retry on failure" | Enumerates 5 specific failure cases, explains recovery for each | Discusses partial failure modes (Cassandra write succeeds in 1 replica but not quorum), consistency implications |
| **Ordering** | "Sort by timestamp" | Server-assigned sequence numbers, gap detection | Discusses causal ordering trade-offs, why total order per conversation is sufficient for chat UX |
| **Delivery Semantics** | "At-most-once" or "exactly-once" | At-least-once with client-side dedup for exactly-once semantics | Explains why true exactly-once is impossible in distributed systems, pragmatic approach |

---

## PHASE 6: Deep Dive — Connection Management & Presence (~8 min)

**Interviewer:**

How do you manage 50-100 million concurrent WebSocket connections? What happens when things go wrong?

**Candidate:**

> Connection management is the defining infrastructure challenge. Let me break it down.
>
> **Gateway server internals (Erlang/BEAM model):**
>
> ```
> +================= Gateway Server (FreeBSD + BEAM) ==================+
> |                                                                     |
> |   Erlang Supervisor Tree:                                           |
> |   +-- Connection Supervisor                                        |
> |       +-- Connection Process (user_123) ── 2 KB memory             |
> |       +-- Connection Process (user_456) ── 2 KB memory             |
> |       +-- Connection Process (user_789) ── 2 KB memory             |
> |       +-- ... (up to 2.8 million per server)                       |
> |                                                                     |
> |   Each process:                                                     |
> |   - Holds WebSocket state                                          |
> |   - Handles send/receive for one user                              |
> |   - Manages heartbeat timer                                        |
> |   - Crashes independently (supervisor restarts it)                 |
> |                                                                     |
> |   OS: FreeBSD (not Linux) — WhatsApp chose FreeBSD for             |
> |   its ports collection and single-distribution model               |
> +====================================================================+
> ```
>
> **Why Erlang/BEAM?**
> - 2 KB per process (vs ~1 MB per Java thread, ~8 KB per Go goroutine)
> - Preemptive scheduling — no process can hog the CPU
> - Let-it-crash: if a connection process dies, only that user's connection is affected. Supervisor automatically restarts it. No cascading failures.
> - Rick Reed's talk: WhatsApp reached **2.8 million TCP connections on a single server** after BEAM optimizations on FreeBSD.
>
> **Heartbeat protocol:**
> - Client sends ping every 30-60 seconds. Server responds with pong.
> - If server doesn't receive ping within 2× interval → mark connection dead → update presence to offline → clean up connection registry.
> - Heartbeats also keep NAT mappings alive — mobile carriers aggressively close idle TCP connections (sometimes after 30 seconds). Without heartbeats, the connection silently dies.
>
> **Reconnection storm handling:**
> - Scenario: a gateway server dies, 500K connections lost simultaneously. All 500K clients try to reconnect at the same time → thundering herd on remaining gateways.
> - Mitigation: **exponential backoff with jitter** on client reconnect. Each client waits `random(0, min(cap, base × 2^attempt))` seconds before reconnecting. Spreads the reconnection load over 30-60 seconds.
> - Additional: connection rate limiting per IP at the load balancer level.
>
> **Presence — the fan-out challenge:**
>
> Naive approach: when user A comes online, push "A is online" to all of A's contacts (say 500 people). A has 500 contacts, each contact has 500 contacts → 250,000 presence updates just from one user. At 100M concurrent users, this is catastrophic.
>
> Smart approach (lazy presence):
> 1. Client tells server "I'm currently viewing chat with user X"
> 2. Server subscribes client to X's presence updates
> 3. Presence updates are only pushed to active subscribers (typically 1-5 per user)
> 4. When client navigates away from the chat, server unsubscribes
>
> Result: fan-out drops from 500 to 1-5. **99%+ reduction** in presence traffic.
>
> See `04-connection-and-presence.md` for the complete deep dive.

---

### L5 vs L6 vs L7 — Phase 6 Rubric

| Aspect | L5 (SDE-2) | L6 (SDE-3) | L7 (Principal) |
|--------|------------|------------|-----------------|
| **Connection Scale** | "Use multiple servers" | Explains Erlang process model, 2 KB per connection, 2.8M per server, supervisor trees | Discusses OS-level tuning (FreeBSD kernel params, file descriptor limits), BEAM scheduler lock contention |
| **Heartbeat** | "Ping/pong every N seconds" | Explains NAT timeout issue, why heartbeats are necessary on mobile networks | Discusses adaptive heartbeat intervals based on network type (WiFi vs cellular), battery impact |
| **Reconnection** | "Client reconnects" | Exponential backoff with jitter, thundering herd mitigation | Load balancer-level rate limiting, graceful degradation during reconnection storms |
| **Presence** | "Store online/offline in DB" | Lazy presence with subscription model, 99% fan-out reduction | Discusses presence consistency model (eventual), debouncing, privacy implications |

---

## PHASE 7: Deep Dive — E2E Encryption (~8 min)

**Interviewer:**

E2E encryption is WhatsApp's defining feature. Walk me through how it works and the architectural implications.

**Candidate:**

> Signal Protocol has three layers: key distribution, session establishment, and message encryption.
>
> **Key distribution (pre-key server):**
> - On registration, each device generates:
>   - **Identity Key Pair** — long-term, never changes
>   - **Signed Pre-Key** — medium-term, rotated weekly, signed by identity key
>   - **100 One-Time Pre-Keys** — ephemeral, each used exactly once
> - Device uploads the public parts to the server's pre-key store
>
> **Session establishment (X3DH):**
> ```
> Alice                          Server                         Bob
>   |                              |                              |
>   |  1. Fetch Bob's pre-key      |                              |
>   |     bundle                   |                              |
>   |----------------------------->|                              |
>   |  {IdentityKey_B,             |                              |
>   |   SignedPreKey_B,            |                              |
>   |   OneTimePreKey_B}           |                              |
>   |<-----------------------------|                              |
>   |                              | (deletes used OneTimePreKey) |
>   |  2. Compute shared secret:   |                              |
>   |     DH1 = DH(IK_A, SPK_B)   |                              |
>   |     DH2 = DH(EK_A, IK_B)    |                              |
>   |     DH3 = DH(EK_A, SPK_B)   |                              |
>   |     DH4 = DH(EK_A, OPK_B)   |                              |
>   |     SK = KDF(DH1‖DH2‖DH3‖DH4)|                             |
>   |                              |                              |
>   |  3. Initialize Double Ratchet|                              |
>   |     with SK as root key      |                              |
>   |                              |                              |
>   |  4. Encrypt first msg with   |                              |
>   |     first chain key          |                              |
>   |------ encrypted blob ------->|------- encrypted blob ------>|
>   |                              |                              |
>   |                              |     5. Bob receives, computes|
>   |                              |        same SK from his keys |
>   |                              |        Decrypts message      |
> ```
>
> **Double Ratchet (per-message key rotation):**
> - **Symmetric ratchet**: Each message in the same "turn" uses the next key from a KDF chain: `key_n+1 = HMAC-SHA256(key_n, constant)`. Forward secrecy — can't derive key_n from key_n+1.
> - **DH ratchet**: When the conversation turn changes (Alice → Bob → Alice), a new ephemeral DH exchange happens. Updates the root key, resets the chain. Post-compromise security — if an attacker got a chain key, they lose access when the DH ratchet advances.
> - Result: **every message has a unique encryption key.** Compromise of one key reveals exactly one message.
>
> **Group E2E (Sender Keys):**
> - Each member generates a Sender Key and distributes it to all group members via pairwise-encrypted messages.
> - Sending a group message: encrypt once with sender's key → server fans out the same encrypted blob to all members → each member decrypts with the sender's key.
> - O(1) encryption per message (vs O(N) with pairwise encryption). For a 1024-member group, this is 1 encryption vs 1024.
> - **Trade-off**: weaker forward secrecy than pairwise Double Ratchet. If a sender key is compromised, all future messages from that sender are readable until key rotation. Keys are rotated when members are added/removed.
>
> **Architectural implications of E2E:**
> 1. **Server cannot search messages** — no server-side full-text search. Contrast with Slack where server-side search is a core feature.
> 2. **Server cannot moderate content** — can't detect spam, abuse, or illegal content at the server level. WhatsApp must rely on client-side reporting.
> 3. **Server cannot debug message content** — when a user reports "my message didn't arrive," engineers can only trace metadata (routing, timing), never content.
> 4. **Multi-device is hard** — each device needs its own encryption keys. Sender must encrypt once per recipient device. WhatsApp took years to add multi-device because of this.
> 5. **Backup encryption** — if messages are backed up to iCloud/Google Drive, the backup must also be E2E encrypted. WhatsApp added encrypted backups in 2021 using an HSM-based Backup Key Vault.
>
> See `05-end-to-end-encryption.md` for the complete deep dive.

---

### L5 vs L6 vs L7 — Phase 7 Rubric

| Aspect | L5 (SDE-2) | L6 (SDE-3) | L7 (Principal) |
|--------|------------|------------|-----------------|
| **Encryption** | "Use AES to encrypt messages" | Explains Signal Protocol: X3DH for key exchange, Double Ratchet for per-message keys, Sender Keys for groups | Discusses forward secrecy vs post-compromise security trade-offs, why Sender Keys weaken forward secrecy for groups |
| **Key Management** | "Store keys on the server" | Pre-key bundle structure, one-time pre-key consumption, rotation schedule | Discusses key exhaustion (what happens when one-time pre-keys run out?), safety number verification, identity key trust model |
| **Architectural Impact** | "Encrypt before sending" | Lists 5 specific implications (no search, no moderation, hard multi-device, backup encryption) | Discusses the business trade-off: privacy limits product features, explains how Telegram chose the opposite trade-off |
| **Multi-device** | "Send to all devices" | Client-side fan-out, per-device encryption, Account/Device Signatures | Discusses key consistency across devices, race conditions in multi-device delivery, history sync challenges |

---

## PHASE 8: Deep Dive — Storage & Data Model (~5 min)

**Interviewer:**

WhatsApp's storage model is unique — the server is a transient relay. Walk me through the data model.

**Candidate:**

> This is a fundamentally different storage philosophy from Slack or Discord. Let me lay out what we store and why.
>
> ```
> +--------------------------+
> | Message Store (Cassandra)|   Partition key: conversationId
> |--------------------------|   Clustering key: sequenceNumber
> | conversationId (PK)      |
> | sequenceNumber (CK)      |   Data: encrypted blob + metadata
> | messageId (unique)       |   TTL: deleted after delivery ACK
> | senderId                 |   (or 30-day max retention)
> | encryptedPayload (blob)  |
> | timestamp                |
> | messageType              |
> | deliveryStatus (per-user)|
> +--------------------------+
>
> +---------------------------+
> | Conversation Metadata     |   What conversations exist
> |---------------------------|
> | conversationId (PK)       |
> | type (1:1 | group)        |
> | participants[]            |
> | groupName, groupAdmin     |
> | lastMessageTimestamp      |
> | lastSequenceNumber        |
> +---------------------------+
>
> +---------------------------+
> | User Profile Store        |   User identity + settings
> |---------------------------|
> | userId (PK)               |
> | phoneNumber (indexed)     |
> | displayName, about        |
> | profilePhotoUrl           |
> | lastSeen                  |
> +---------------------------+
>
> +---------------------------+
> | Pre-Key Store             |   E2E encryption keys
> |---------------------------|
> | userId (PK)               |
> | identityKey               |
> | signedPreKey              |
> | oneTimePreKeys[] (consume |
> |   on use, replenish)      |
> +---------------------------+
>
> +---------------------------+
> | Connection Registry       |   Who is connected where
> | (Redis - ephemeral)       |
> |---------------------------|
> | userId → {gatewayId,      |
> |   connectionId, deviceId} |
> | TTL: heartbeat timeout    |
> +---------------------------+
>
> +---------------------------+
> | Media Store (S3/Blob)     |   Encrypted media files
> |---------------------------|
> | mediaId → encrypted blob  |
> | TTL: 30 days after upload |
> +---------------------------+
> ```
>
> **Why Cassandra for message storage?**
>
> | Requirement | Cassandra | MySQL/Aurora | MongoDB |
> |------------|-----------|-------------|---------|
> | Write throughput | Millions/sec (linear scaling) | Limited by single-master | Good but less proven at extreme scale |
> | Multi-DC replication | First-class (async, tunable) | Aurora Global DB (slower) | Available but operationally complex |
> | Partition tolerance | AP (available during partitions) | CP (unavailable during partitions) | Configurable |
> | Data model fit | Wide-column, great for time-series (conversationId → messages) | Relational, needs JOINs | Document, OK fit |
> | Deletion performance | TTL-based automatic expiry | Manual DELETE, fragmentation | TTL available |
>
> Cassandra wins because: extreme write throughput, multi-DC replication, and TTL-based auto-deletion align perfectly with the "store temporarily, delete after delivery" pattern.
>
> **The key insight**: WhatsApp's server stores almost nothing permanently. Messages are deleted after delivery. Media expires after 30 days. The server is a **transient relay**, not a database. The client's local storage (+ encrypted backups to iCloud/Google Drive) is the source of truth.
>
> **Contrast with Slack:** Slack stores EVERYTHING permanently. Messages, files, reactions, threads — all in MySQL via Vitess (sharded). Searchable, auditable, compliant. Slack's storage grows monotonically. WhatsApp's server-side storage is bounded by the number of undelivered messages at any given time — it's a queue, not an archive.
>
> See `06-storage-and-data-model.md` for the complete deep dive.

---

### L5 vs L6 vs L7 — Phase 8 Rubric

| Aspect | L5 (SDE-2) | L6 (SDE-3) | L7 (Principal) |
|--------|------------|------------|-----------------|
| **Data Model** | "Messages table with sender, receiver, content" | Partitioned by conversationId, clustered by sequenceNumber, TTL-based deletion, separate stores for different data types | Discusses how the transient relay model affects capacity planning (bounded by concurrent undelivered messages, not total history) |
| **Database Choice** | "Use a NoSQL database" | Compares Cassandra vs MySQL vs MongoDB on 5 dimensions, explains why Cassandra wins | Discusses Cassandra tuning (compaction strategy, read-repair, LOCAL_QUORUM), Mnesia as WhatsApp's original choice |
| **Storage Philosophy** | "Store messages in a database" | Explains transient relay vs permanent store, contrasts with Slack | Discusses how this philosophy limits features (no server-side search, no compliance/audit), the business implications |

---

## PHASE 9: Wrap-Up (~3 min)

**Interviewer:**

What keeps you up at night with this system?

**Candidate:**

> Three things:
>
> **1. Thundering herd on reconnection storms.**
> If a major gateway cluster fails or a network partition resolves, millions of clients reconnect simultaneously. Each one needs: WebSocket handshake → auth token validation → connection registry write → offline queue drain. The offline queue drain is the killer — millions of Cassandra reads at once. Mitigation: exponential backoff with jitter on clients, connection rate limiting at LB, priority-based queue drain (deliver most recent messages first, batch older ones).
>
> **2. Pre-key exhaustion under load.**
> One-time pre-keys are consumed on first contact. If a celebrity or business account is messaged by thousands of new users simultaneously, their one-time pre-keys deplete rapidly. When pre-keys run out, X3DH falls back to using only the signed pre-key (no one-time pre-key), which weakens the initial forward secrecy. Mitigation: aggressive pre-key replenishment (client uploads new batches when count drops below threshold), server-side monitoring of pre-key inventory.
>
> **3. Group message fan-out amplification.**
> A 1024-member group with 100 messages/minute = 102,400 message deliveries/minute for that one group. If 100 such groups are active simultaneously = 10.24 million deliveries/minute just from groups. Now multiply by groups worldwide. The write amplification is bounded per-group (max 1024) but the aggregate can be enormous. Mitigation: rate-limit messages per group, batch fan-out writes to Cassandra, use Sender Keys to avoid per-recipient encryption overhead.

---

### L5 vs L6 vs L7 — Phase 9 Rubric

| Aspect | L5 (SDE-2) | L6 (SDE-3) | L7 (Principal) |
|--------|------------|------------|-----------------|
| **Operational Concerns** | "Server might go down" | Three specific scenarios with quantified impact and concrete mitigations | Adds: encryption key compromise response plan, regulatory compliance (EU data residency), abuse detection without reading content |

---

## Final Architecture Summary

```
+======================== WhatsApp Architecture ========================+
|                                                                        |
|  Clients (iOS/Android/Web/Desktop)                                     |
|  - Signal Protocol (X3DH + Double Ratchet + Sender Keys)               |
|  - WebSocket persistent connection                                     |
|  - Client-generated message UUIDs (idempotent retries)                 |
|  - Local message storage + encrypted cloud backup                      |
|                                                                        |
|  ┌──── L4 Load Balancer (TCP passthrough) ────┐                        |
|  │                                             │                        |
|  v                                             v                        |
|  +------------------+    +------------------+                           |
|  | Gateway Server 1 |    | Gateway Server N |  (Erlang/BEAM,           |
|  | 100K-2.8M conns  |    | 100K-2.8M conns  |   FreeBSD)              |
|  +--------+---------+    +--------+---------+                           |
|           |                       |                                     |
|           v                       v                                     |
|  +--------------------------------------------+                        |
|  |     Message Router (Stateless)              |                        |
|  +-----+-------------+---------------+--------+                        |
|        |             |               |                                  |
|        v             v               v                                  |
|  +-----------+ +-----------+ +----------------+                         |
|  | Connection| | Offline   | | Pre-Key        |                         |
|  | Registry  | | Msg Queue | | Server         |                         |
|  | (Redis)   | | (Cassandra| | (E2E keys)     |                         |
|  +-----------+ |  + TTL)   | +----------------+                         |
|                +-----------+                                            |
|        |             |               |                                  |
|        v             v               v                                  |
|  +-----------+ +-----------+ +----------------+                         |
|  | Presence  | | Media     | | Push Notif     |                         |
|  | Store     | | Store     | | (APNs / FCM)   |                         |
|  | (Redis)   | | (S3/Blob) | +----------------+                         |
|  +-----------+ +-----------+                                            |
|                                                                        |
|  Monitoring: Delivery latency, Connection count, Queue depth           |
|  Chaos: Gateway kills, DC failover, Redis node failure                 |
|  Multi-DC: Active-active, Cassandra cross-DC replication               |
+========================================================================+
```

---

## Supporting Deep-Dive Documents

| Doc | Topic | File |
|-----|-------|------|
| 02 | API Contracts | [02-api-contracts.md](./02-api-contracts.md) |
| 03 | Messaging & Delivery Pipeline | [03-messaging-and-delivery.md](./03-messaging-and-delivery.md) |
| 04 | Connection Management & Presence | [04-connection-and-presence.md](./04-connection-and-presence.md) |
| 05 | E2E Encryption (Signal Protocol) | [05-end-to-end-encryption.md](./05-end-to-end-encryption.md) |
| 06 | Storage & Data Model | [06-storage-and-data-model.md](./06-storage-and-data-model.md) |
| 07 | Group Messaging | [07-group-messaging.md](./07-group-messaging.md) |
| 08 | Media Handling | [08-media-handling.md](./08-media-handling.md) |
| 09 | Push Notifications & Offline Sync | [09-notifications-and-offline.md](./09-notifications-and-offline.md) |
| 10 | Scaling & Reliability | [10-scaling-and-reliability.md](./10-scaling-and-reliability.md) |
| 11 | Design Trade-offs | [11-design-trade-offs.md](./11-design-trade-offs.md) |

---

## Verified Sources

- [WhatsApp Security Whitepaper](https://www.whatsapp.com/security/WhatsApp-Security-Whitepaper.pdf) — E2E encryption protocol details
- [Signal Protocol Specifications](https://signal.org/docs/) — X3DH and Double Ratchet specifications
- [Meta Engineering: WhatsApp Multi-Device](https://engineering.fb.com/2021/07/14/security/whatsapp-multi-device/) — Multi-device architecture
- [Meta Engineering: WhatsApp E2EE Backups](https://engineering.fb.com/2021/09/10/security/whatsapp-e2ee-backups/) — Encrypted backup design
- [Rick Reed: Scaling to Millions of Connections](https://www.erlang-factory.com/conference/SFBay2012/speakers/RickReed) — Erlang/BEAM scaling at WhatsApp
- [High Scalability: WhatsApp Architecture](https://highscalability.com/the-whatsapp-architecture-facebook-bought-for-19-billion/) — Infrastructure overview
- [Discord Blog: Scaled Elixir to 5M Users](https://discord.com/blog/how-discord-scaled-elixir-to-5-000-000-concurrent-users) — BEAM VM comparison
