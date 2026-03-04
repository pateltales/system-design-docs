Design WhatsApp (Real-Time Chat Application) as a system design interview simulation.

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
Create all files under: src/hld/chatapp/design/

## Files to create

### 1. 01-interview-simulation.md — the main backbone
The full interview dialogue covering all phases and attempts.

### 2. 02-api-contracts.md — WhatsApp Platform APIs

This doc should list all the major API surfaces of a WhatsApp-like chat application. The interview simulation (Phase 3) will only cover a subset — this doc is the comprehensive reference.

**Structure**: For each API group, list every endpoint with HTTP method, path, request/response shape, and a brief description. Mark endpoints covered in the interview with a star or highlight.

**API groups to cover**:

- **Authentication APIs**: `POST /auth/register` (phone number registration, SMS OTP verification), `POST /auth/verify-otp`, `POST /auth/refresh-token`, `POST /auth/logout`. WhatsApp uses phone numbers as identity — no email/password. Registration involves SMS verification + device key exchange for E2EE.

- **Messaging APIs**: The most critical path. `POST /messages/send` (send a message — text, image, video, audio, document, location, contact), `GET /messages/{conversationId}` (paginated message history, cursor-based), `PUT /messages/{messageId}/status` (delivery receipt: sent → delivered → read), `DELETE /messages/{messageId}` (delete for me / delete for everyone). Messages are E2E encrypted — server never sees plaintext. Server stores encrypted blobs + metadata (sender, recipient, timestamp, delivery status).

- **Real-Time Connection APIs**: `WS /ws/connect` (WebSocket connection for real-time message delivery, typing indicators, presence updates). This is the persistent connection that makes chat "real-time." MQTT or WebSocket — WhatsApp historically uses a custom XMPP-derived protocol over persistent TCP connections. For the interview, model as WebSocket. Include heartbeat/keepalive mechanism.

- **Group Chat APIs**: `POST /groups` (create group — name, participants, admin), `PUT /groups/{groupId}` (update group info, settings), `POST /groups/{groupId}/participants` (add members), `DELETE /groups/{groupId}/participants/{userId}` (remove member), `POST /groups/{groupId}/messages` (send group message — fan-out to all participants), `GET /groups/{groupId}/messages` (group message history). Groups can have up to 1024 members. Group messages require fan-out — one send triggers N deliveries.

- **Media APIs**: `POST /media/upload` (upload media — image, video, audio, document. Chunked upload for large files. Media is E2E encrypted before upload — server stores encrypted blob), `GET /media/{mediaId}` (download media — returns encrypted blob, client decrypts), `GET /media/{mediaId}/thumbnail` (low-res preview for images/videos). Media is stored separately from messages — message contains a reference (mediaId + encryption key). This separation allows lazy loading and reduces storage for undelivered messages.

- **Status/Stories APIs**: `POST /status` (post a status — image, video, text with background), `GET /status/contacts` (get statuses from contacts), `GET /status/{statusId}` (view a specific status), `DELETE /status/{statusId}`. Statuses auto-expire after 24 hours. Viewers list is tracked (who viewed your status).

- **Presence & Typing APIs**: `PUT /presence` (update online/offline status, last seen timestamp), `POST /typing` (send typing indicator to a conversation — ephemeral, not persisted), `GET /presence/{userId}` (get user's last seen / online status). Presence updates are pushed via WebSocket, not polled. Last seen is eventually consistent — slight staleness is acceptable.

- **Contact & Profile APIs**: `POST /contacts/sync` (upload phone contacts hash list, server returns which contacts are registered on the platform), `GET /profile/{userId}` (get user profile — name, about, profile photo URL), `PUT /profile` (update own profile). Contact sync is privacy-sensitive — WhatsApp uses hashed phone numbers to avoid uploading raw contacts.

- **Call Signaling APIs**: `POST /calls/initiate` (start a voice/video call — sends signaling data to callee via WebSocket), `POST /calls/{callId}/answer`, `POST /calls/{callId}/reject`, `POST /calls/{callId}/end`, `POST /calls/{callId}/ice-candidate` (WebRTC ICE candidate exchange). Actual voice/video data flows peer-to-peer via WebRTC (STUN/TURN servers for NAT traversal) — the backend only handles signaling, not media relay.

- **Encryption Key APIs** (internal): `POST /keys/prekeys` (upload pre-key bundle for Signal Protocol — identity key, signed pre-key, one-time pre-keys), `GET /keys/{userId}/prekey` (fetch a user's pre-key bundle to establish E2E session), `GET /keys/{userId}/identity` (get identity key for safety number verification). These power the Signal Protocol (Double Ratchet) that provides E2E encryption.

- **Admin / Ops APIs** (internal): `GET /health`, `GET /metrics`, `POST /config/feature-flags`, `POST /cache/invalidate/{userId}`.

**Contrast with Slack/Telegram/Discord**:
- **Slack**: Workspace-centric (not phone-number-based), channels instead of groups, no E2E encryption by default, rich integrations/bots API, threaded conversations, enterprise-focused. Slack's API is heavily REST-based with Events API for real-time.
- **Telegram**: Cloud-based (messages stored on server in plaintext by default, E2E only in "Secret Chats"), supports large groups (200K members vs WhatsApp's 1024), bot platform, channels (broadcast to unlimited subscribers). Telegram's MTProto protocol is custom-built.
- **Discord**: Server/channel model (like Slack but for communities), voice channels (persistent voice rooms, unlike WhatsApp's 1:1 calls), screen sharing, no E2E encryption, designed for gaming communities.

**Interview subset**: In the interview (Phase 3), focus on: message send (the most latency-sensitive path — E2E encryption, delivery guarantees, fan-out for groups), real-time delivery (WebSocket connection management), message history (pagination, offline sync), and presence (online/last seen).

### 3. 03-messaging-and-delivery.md — Message Delivery Pipeline

The core of a chat app — how messages get from sender to receiver reliably and in real-time.

- **Message flow (1:1)**: Sender encrypts message (Signal Protocol) → sends to server via WebSocket → server stores encrypted message in message store → server checks if recipient is online (has active WebSocket connection) → if online: push via WebSocket immediately → if offline: store in offline message queue → when recipient connects: deliver queued messages → recipient sends delivery ACK → server updates delivery status → sender receives delivery/read receipt.
- **Message ordering**: Messages within a conversation must be ordered. Use server-assigned monotonically increasing sequence numbers per conversation. Client uses sequence numbers to detect gaps (missing messages) and request re-delivery. Causal ordering (Lamport timestamps or vector clocks) for group chats to maintain "happened-before" relationships.
- **Delivery guarantees**: At-least-once delivery — server retries until ACK received. Client-side deduplication using message IDs (idempotency). Server stores message until all recipients ACK. For groups: per-recipient delivery tracking.
- **Offline message handling**: Messages for offline users are stored in a persistent queue (Cassandra or similar). When user comes online, server pushes all queued messages in order. If user has been offline for a long time, batch delivery with pagination to avoid overwhelming the client.
- **Fan-out for group messages**: Sender sends one message → server fans out to N group members. Two strategies:
  - **Write-time fan-out (fan-out on write)**: Write a copy of the message into each recipient's inbox. Fast reads (each user reads from their own inbox). Expensive writes for large groups (1024 copies). WhatsApp-scale groups (max 1024) make this feasible.
  - **Read-time fan-out (fan-out on read)**: Store message once in the group's message log. Each reader fetches from the group log. Cheap writes, expensive reads. Better for very large groups (Discord servers, Telegram channels with 200K+ members).
  - WhatsApp likely uses write-time fan-out for groups (max 1024 members) — the write amplification is bounded and read latency is critical for real-time chat.
  - **Contrast with Discord**: Discord uses read-time fan-out because servers can have millions of members. Writing to each member's inbox would be prohibitively expensive.
- **Message storage**: Messages are stored encrypted (E2E). Server stores: messageId, conversationId, senderId, encryptedPayload, timestamp, sequenceNumber, deliveryStatus per recipient. Storage is partitioned by conversationId for efficient range queries (message history).
- **Retry and ACK protocol**: Server → client delivery uses a simple ACK protocol. Server sends message → starts retry timer → if no ACK within timeout → retry with exponential backoff → after max retries → store for next connection. Client ACKs are idempotent.
- **Contrast with email (store-and-forward)**: Chat requires sub-second delivery when online. Email tolerates minutes/hours of delay. Chat uses persistent connections (WebSocket); email uses SMTP relay. Chat has real-time presence; email does not. This fundamental difference drives the entire architecture.

### 4. 04-connection-and-presence.md — WebSocket Connection Management & Presence

Managing millions of persistent connections is the defining infrastructure challenge of a chat app.

- **Connection gateway architecture**: Stateful WebSocket servers that maintain persistent connections with clients. Each gateway server holds 100K-500K concurrent connections. Clients connect via load balancer (L4, not L7 — WebSocket connections are long-lived, L7 load balancers add overhead). Connection is authenticated via token on handshake.
- **Connection state management**: Server must track: userId → gatewayServer → connectionId mapping. This mapping is stored in a distributed registry (Redis cluster or similar). When a message needs to be delivered, the routing layer looks up the recipient's gateway server and forwards the message.
- **Heartbeat / keepalive**: Client sends periodic heartbeat (every 30-60 seconds). If server doesn't receive heartbeat within timeout → mark connection as dead → update presence to offline. Heartbeat also keeps NAT mappings alive (mobile networks aggressively close idle TCP connections).
- **Reconnection handling**: Mobile clients frequently disconnect (network switches, sleep mode, signal loss). On reconnect: client presents last-seen sequence number → server delivers all messages since that sequence number → resume normal flow. Must handle: duplicate detection, gap detection, and out-of-order delivery.
- **Presence system**: Tracks online/offline/last-seen for every user. Presence updates are pushed to contacts via WebSocket. Challenge: updating all contacts on every status change is expensive (user with 500 contacts → 500 push notifications on every online/offline toggle). Solutions:
  - **Lazy presence**: Only push presence to users who have the contact's chat open. Reduces fan-out dramatically.
  - **Presence subscription**: Client subscribes to presence for specific users (open chats, favorites). Server only pushes to subscribers.
  - **Throttling**: Coalesce rapid online/offline toggles (e.g., user walks through spotty coverage → don't send 20 presence flips in a minute).
- **Typing indicators**: Ephemeral signals — not persisted. Sent via WebSocket, short TTL (3-5 seconds). If no refresh → indicator disappears. Typing indicators are best-effort — loss is acceptable (unlike messages).
- **Multi-device support**: WhatsApp now supports linked devices (phone + up to 4 companion devices). Each device has its own WebSocket connection. Messages must be delivered to ALL active devices. Encryption keys are per-device (Signal Protocol handles multi-device via sender keys or per-device encryption).
- **Scale**: WhatsApp handles 2+ billion users, ~100 billion messages/day. Assume 50-100 million concurrent WebSocket connections at peak. Each gateway server handles 100K-500K connections. Need 200-1000 gateway servers.
- **Contrast with Slack**: Slack uses WebSocket for real-time but also has a REST-based Events API for bots and integrations. Slack's connection scale is smaller (enterprise users, not consumer-scale). Discord uses WebSocket gateways similarly but adds voice channel persistent connections (UDP for voice, WebSocket for signaling).

### 5. 05-end-to-end-encryption.md — E2E Encryption (Signal Protocol)

E2E encryption is WhatsApp's defining security feature. The server NEVER sees plaintext messages.

- **Signal Protocol overview**: Developed by Open Whisper Systems (Moxie Marlinspike). Used by WhatsApp, Signal, Facebook Messenger (optional). Provides: forward secrecy (compromise of long-term keys doesn't reveal past messages), post-compromise security (recovery after key compromise), deniability (cannot cryptographically prove who sent a message).
- **Key hierarchy**:
  - **Identity Key Pair**: Long-term key pair per device. Generated at registration. Never changes.
  - **Signed Pre-Key**: Medium-term key pair. Rotated periodically (e.g., weekly). Signed by the identity key.
  - **One-Time Pre-Keys**: Ephemeral key pairs uploaded to the server in batches. Each is used exactly once to establish a session. Server stores 100+ one-time pre-keys per user.
  - **Session keys**: Derived from the key exchange (X3DH). Used for the Double Ratchet.
- **X3DH (Extended Triple Diffie-Hellman)**: The initial key exchange protocol. When Alice wants to message Bob for the first time: Alice fetches Bob's pre-key bundle from server (identity key + signed pre-key + one-time pre-key) → performs 3 DH computations → derives a shared secret → uses it to initialize the Double Ratchet. The one-time pre-key is consumed (deleted from server) — ensures each session has unique keying material.
- **Double Ratchet Algorithm**: After initial key exchange, every message uses a new encryption key. Two ratchets:
  - **Symmetric ratchet (chain ratchet)**: Derives a new message key from the previous one using HMAC-based KDF. Each message in the same "sending turn" uses the next key in the chain.
  - **DH ratchet**: When the conversation turns change (Alice → Bob → Alice), a new DH exchange occurs using ephemeral keys. This updates the root key and resets the chain. Provides forward secrecy per message turn.
  - Result: Every message is encrypted with a unique key. Compromising one key reveals nothing about past or future messages.
- **Group E2E encryption**: WhatsApp uses **Sender Keys** for groups. Each member generates a sender key and distributes it to all group members (via pairwise E2E encrypted channels). When sending a group message, the sender encrypts once with their sender key → all recipients can decrypt. This is O(1) encryption per message (vs O(N) if encrypting per-recipient). Trade-off: less forward secrecy than pairwise Double Ratchet — if a sender key is compromised, all future messages from that sender are compromised until the key is rotated.
- **Server's role**: The server is an untrusted relay. It stores: encrypted message blobs, pre-key bundles (public keys only), delivery metadata (who sent to whom, when). It CANNOT decrypt message content. This is by design — even if the server is compromised, message content is safe.
- **Safety numbers / Security codes**: Users can verify E2E encryption by comparing "safety numbers" (a hash of both users' identity keys). If the safety number changes, it means the recipient's identity key changed (new device, reinstalled app) — possible MitM attack.
- **Contrast with Telegram**: Telegram does NOT use E2E encryption by default — regular chats are client-server encrypted (Telegram can read them). Only "Secret Chats" use E2E (MTProto 2.0 protocol). This architectural choice enables Telegram's cloud sync (messages accessible from any device) but sacrifices privacy. WhatsApp chose the opposite trade-off: privacy over convenience (multi-device was hard to add because of E2E).
- **Contrast with Slack/Discord**: Neither uses E2E encryption. Messages are stored in plaintext on their servers. Enterprise compliance and content moderation require server-side access to messages. Different threat model — enterprise customers want admin visibility, not user privacy from the platform.

### 6. 06-storage-and-data-model.md — Data Storage & Data Model

- **Message store**: The largest and most critical data store. Stores encrypted message blobs + metadata.
  - **Partitioning**: Partition by conversationId (1:1 or group). All messages in a conversation are co-located for efficient range queries (message history). Within a partition, ordered by sequenceNumber.
  - **Storage engine**: Cassandra or HBase (wide-column stores). WhatsApp originally used Erlang + Mnesia, later migrated to custom storage. For interview purposes, model as Cassandra.
  - **Schema**: `(conversationId, sequenceNumber) → {messageId, senderId, encryptedPayload, timestamp, messageType, deliveryStatus}`
  - **TTL / retention**: WhatsApp doesn't store messages indefinitely on the server — messages are deleted after delivery (server is just a relay). Undelivered messages may be retained for 30 days, then dropped. This minimizes server-side storage. Contrast with Slack (retains all messages forever, searchable) and Telegram (cloud storage, messages retained indefinitely).
- **Conversation metadata store**: Stores conversation-level data.
  - `conversationId → {type (1:1 / group), participants[], groupName, groupAdmin, createdAt, lastMessageTimestamp}`
  - Used for: listing conversations on the chat list screen, sorted by lastMessageTimestamp.
- **User profile store**:
  - `userId → {phoneNumber, displayName, about, profilePhotoUrl, lastSeen, createdAt}`
  - Indexed by phoneNumber for contact sync lookups.
- **Pre-key store** (for E2E encryption):
  - `userId → {identityKey, signedPreKey, oneTimePreKeys[]}`
  - One-time pre-keys are consumed on use (deleted after fetch).
- **Media store**: Object storage (S3 / blob store) for encrypted media files. Metadata in the message store contains a reference (mediaId + encryption key for the recipient to decrypt).
- **Offline message queue**:
  - Messages for offline users. Keyed by `(recipientId, sequenceNumber)`. Drained when user connects. Deleted after delivery ACK.
- **Connection registry**:
  - `userId → {gatewayServerId, connectionId, connectedAt}`. Stored in Redis for fast lookups. TTL-based expiry (connection timeout).
- **Scale numbers** (WhatsApp-scale):
  - 2+ billion registered users
  - ~100 billion messages per day
  - ~6.5 billion media shared per day (images, videos, documents)
  - Message size: average ~1 KB encrypted (text), media references ~100 bytes (pointer to blob store)
  - Storage per day (text messages only): ~100 TB/day (but messages are deleted after delivery, so steady-state storage is much smaller — only undelivered messages + media blobs)
- **Contrast with Slack storage model**: Slack retains ALL messages forever (searchable, compliance). Slack uses MySQL + Vitess for message storage (strong consistency, ACID). WhatsApp deletes messages after delivery — server is a transient relay, not a permanent store. This fundamental difference drives storage architecture: Slack needs massive, indexed, searchable storage; WhatsApp needs high-throughput transient storage with fast writes and fast drains.

### 7. 07-group-messaging.md — Group Chat Architecture

- **Group creation and management**: Creator becomes admin. Can add/remove members, change group name/photo, set group settings (who can send messages, who can edit group info). Max 1024 members.
- **Fan-out strategies** (detailed comparison):
  - **Fan-out on write**: Server writes message to each member's inbox. WhatsApp's approach for groups (max 1024). Pros: fast reads, simple client logic. Cons: write amplification (1 message → 1024 writes), storage amplification.
  - **Fan-out on read**: Message stored once in group log. Each member reads from the group log. Discord/Telegram's approach for large groups/channels. Pros: minimal write amplification. Cons: read amplification, complex sync logic on client.
  - **Hybrid**: Fan-out on write for small groups (< 100 members), fan-out on read for large groups. Reduces worst-case write amplification.
- **Group message delivery**: Sender → server → fan-out to each member's gateway server (if online) or offline queue (if offline). Delivery tracking is per-member: the sender can see individual delivery/read receipts for each group member.
- **Group E2E encryption with Sender Keys**: (see 05-end-to-end-encryption.md). Each member has a sender key. When membership changes (member added/removed), sender keys are rotated to maintain forward secrecy.
- **Ordering in groups**: Server assigns a single monotonic sequence number per group. All members see messages in the same order. This is a simplification — true causal ordering is harder but not necessary for chat UX.
- **Admin controls**: Only admins can: add/remove members, change group info, promote/demote admins, toggle "only admins can send" mode. These are metadata operations, not message operations.
- **Contrast with Discord servers**: Discord servers can have millions of members. Channels within servers are the messaging unit. Discord uses read-time fan-out for channels. Discord has roles and permissions (much more complex than WhatsApp's admin/member model). Discord's channels are persistent (messages retained forever); WhatsApp groups behave like 1:1 chats (messages deleted from server after delivery).
- **Contrast with Telegram groups/channels**: Telegram groups support up to 200K members. Telegram channels are broadcast-only (unlimited subscribers). Telegram stores all messages on the server (cloud-based). This enables features WhatsApp can't offer: searchable group history from any device, seamless device switching. Trade-off: Telegram sacrifices E2E encryption for convenience.

### 8. 08-media-handling.md — Media Upload, Storage & Delivery

- **Upload flow**: Client encrypts media (E2E, using a random AES-256 key) → uploads encrypted blob to media server → server returns mediaId → client sends a message containing {mediaId, encryptionKey, mimeType, thumbnail} to the recipient via normal message flow. Recipient downloads the encrypted blob using mediaId, decrypts with the key from the message.
- **Chunked upload**: Large files (videos, documents) use chunked, resumable uploads. Client splits file into chunks (e.g., 256 KB), uploads each chunk, server reassembles. Resume from last successful chunk on failure. Important for mobile networks.
- **Media compression**: Client-side compression before upload. Images: JPEG compression, resize to max dimension (e.g., 1600px). Videos: re-encode to H.264/AAC, lower bitrate. Audio messages: Opus codec, low bitrate (~16 kbps). This reduces upload time and storage.
- **Thumbnail generation**: Client generates and sends a low-res thumbnail (blurred placeholder) with the message. Recipient sees the thumbnail immediately while downloading the full media in the background. WhatsApp's characteristic "blurry preview" effect.
- **Media storage**: Object storage (S3 or equivalent). Encrypted blobs — server cannot decrypt. CDN for delivery. Media has TTL — WhatsApp may delete media from servers after it's been downloaded or after a retention period (e.g., 30 days).
- **Forward and re-upload**: When a user forwards media, the mediaId is reused (no re-upload needed). The server stores one copy, multiple messages reference it. Deduplication at the content level.
- **Scale**: ~6.5 billion media items shared per day. Average media size: images ~100-300 KB (after compression), videos ~2-10 MB, audio messages ~50-200 KB.
- **Contrast with Telegram**: Telegram stores media in the cloud permanently (not E2E encrypted). Users can access media from any device, any time. WhatsApp's E2E model means media can only be decrypted on the recipient's device — no cloud access. Telegram's approach is more convenient but less private.

### 9. 09-notifications-and-offline.md — Push Notifications & Offline Sync

- **Push notification flow**: When recipient is offline (no active WebSocket), server sends a push notification via platform push services:
  - **APNs** (Apple Push Notification service) for iOS
  - **FCM** (Firebase Cloud Messaging) for Android
  - Push payload contains: notification metadata (sender name, "New message" — NOT the actual message content, since it's E2E encrypted and the push service shouldn't see it). On iOS, notification content is decrypted locally by a Notification Service Extension.
- **Offline sync**: When a user comes back online:
  1. Client establishes WebSocket connection
  2. Sends last-known sequence number per conversation
  3. Server delivers all messages since that sequence number from the offline queue
  4. Client ACKs each message → server removes from offline queue
  5. If too many messages accumulated (long offline period), paginate delivery
- **Offline message retention**: Server retains undelivered messages for a limited period (e.g., 30 days). After that, messages are dropped. This is consistent with WhatsApp's "server as transient relay" philosophy.
- **Badge count / unread count**: Server tracks unread message count per conversation per user. Sent in push notifications and on reconnect. Client maintains local unread count and syncs with server.
- **Multi-device offline sync**: With linked devices, a message must be delivered to ALL devices. If one device is online and another is offline, the online device gets immediate delivery; the offline device gets the message on next connect. Each device tracks its own sequence number independently.
- **Contrast with Slack**: Slack has a full "catch-up" experience — when you open Slack, it loads all channels, threads, mentions, reactions since you were last active. Slack's server stores everything, so catch-up is a read from persistent storage. WhatsApp's catch-up is draining the offline queue — fundamentally different because WhatsApp doesn't have persistent server-side storage.

### 10. 10-scaling-and-reliability.md — Scaling & Reliability

Cross-cutting doc that ties together scaling decisions from all deep dives.

- **Scale numbers** (WhatsApp-scale):
  - **2+ billion registered users**
  - **~100 billion messages per day** (~1.15 million messages/second average, peak 3-5x)
  - **50-100 million concurrent WebSocket connections** at peak
  - **~6.5 billion media items shared per day**
  - **Famously lean team**: WhatsApp served 900 million users with only ~50 engineers (pre-Facebook acquisition). Erlang/FreeBSD stack. This is an extreme example of choosing the right technology and architecture for operational simplicity.
- **Erlang / BEAM VM**: WhatsApp's original tech stack. Erlang's actor model (lightweight processes, message-passing) is ideal for chat: each connection is an Erlang process, millions of processes per node, fault-tolerant (let-it-crash philosophy, supervisor trees). WhatsApp reportedly handled 2 million connections per server using Erlang.
- **Horizontal scaling**:
  - **Gateway servers**: Stateful (hold WebSocket connections). Scale by adding more servers. User-to-server mapping stored in a distributed registry.
  - **Message routing**: Stateless routing layer between gateway servers. Looks up recipient's gateway, forwards message. Can scale independently.
  - **Message store**: Partitioned by conversationId. Add more partitions/nodes for throughput. Cassandra-style ring for even distribution.
  - **Media servers**: Stateless upload/download handlers backed by object storage. Scale horizontally.
- **Availability and fault tolerance**:
  - **Gateway server failure**: Connections are lost. Clients reconnect to a different gateway. Offline queue ensures no message loss. Reconnection + sync restores state.
  - **Message store failure**: Replication factor of 3 (Cassandra). Survive single-node failures without data loss. Quorum reads/writes for consistency.
  - **Data center failover**: Active-active across multiple data centers. If one DC goes down, traffic is rerouted. DNS-based or load-balancer-based failover.
- **Rate limiting**: Per-user rate limits on message sends (prevent spam), connection attempts (prevent abuse), media uploads (prevent storage abuse). Group message rate limits to prevent broadcast spam.
- **Monitoring and alerting**: Track: message delivery latency (P50, P95, P99), WebSocket connection count, offline queue depth, media upload/download latency, error rates. Alert on: delivery latency spikes, connection drops, queue growth (indicates delivery failures).
- **Back-of-envelope math**:
  - 100 billion messages/day ÷ 86400 seconds = ~1.15 million messages/second
  - Average message size ~1 KB → ~1.15 GB/second write throughput (just messages)
  - 100 million concurrent connections × 100 bytes state per connection = ~10 GB connection registry
  - 6.5 billion media/day × 200 KB average = ~1.3 PB/day media throughput
- **Contrast with Discord scaling**: Discord serves 150+ million MAU but the challenge is different — persistent voice channels (UDP streams), large servers with millions of members (read fan-out), rich presence (gaming activity). Discord uses Elixir (also BEAM VM, like Erlang) for real-time features. Similar philosophy to WhatsApp's Erlang choice.

### 11. 11-design-trade-offs.md — Design Philosophy & Trade-off Analysis

Opinionated analysis of WhatsApp's design choices — not just "what" but "why this and not that."

- **E2E encryption by default vs server-side encryption**: WhatsApp chose E2E encryption for all chats by default. Telegram chose server-side encryption by default (E2E only in Secret Chats). Trade-off: WhatsApp sacrifices server-side features (search across devices, seamless cloud sync) for privacy. Telegram sacrifices privacy for convenience. Neither is objectively "better" — different user value propositions.
- **Server as transient relay vs persistent store**: WhatsApp deletes messages from server after delivery. Slack/Telegram store messages permanently. Trade-off: WhatsApp minimizes server-side storage and attack surface (nothing to breach if messages aren't stored). Slack/Telegram enable search, compliance, multi-device history. WhatsApp's model requires the client to be the source of truth (backup to iCloud/Google Drive for persistence).
- **Phone number identity vs email/username identity**: WhatsApp uses phone numbers as identity. Slack uses email. Discord uses username. Trade-off: Phone numbers enable frictionless contact discovery (sync your phone contacts), but tie identity to a phone number (privacy concern, can't change easily). Email/username requires explicit "add friend" flow but decouples identity from phone.
- **Fan-out on write vs fan-out on read**: WhatsApp uses fan-out on write for group messages (max 1024 members). Discord uses fan-out on read for server channels (millions of members). Trade-off: fan-out on write gives fast reads but limits group size. Fan-out on read gives unlimited group size but slower reads. The choice follows from the product: WhatsApp is for intimate groups, Discord is for large communities.
- **WebSocket vs HTTP long polling vs Server-Sent Events**: WhatsApp uses persistent connections (WebSocket / custom protocol over TCP). Some chat systems use long polling. Trade-off: WebSocket gives lowest latency (sub-100ms delivery) but requires stateful servers and connection management. Long polling is simpler but adds latency (seconds) and wastes bandwidth. For a chat app where real-time matters, WebSocket is the right choice.
- **Erlang/BEAM vs Java/Go for connection handling**: WhatsApp chose Erlang. Discord chose Elixir (also BEAM). Many other chat systems use Go or Java. Trade-off: Erlang's lightweight processes (2 KB each, millions per node) and preemptive scheduling make it ideal for high-concurrency connection handling. Java/Go require more memory per connection (threads/goroutines are heavier). Erlang's "let it crash" philosophy simplifies error handling for unreliable mobile connections.
- **Sender Keys (groups) vs pairwise encryption (1:1)**: WhatsApp uses pairwise Double Ratchet for 1:1 (maximum forward secrecy) and Sender Keys for groups (O(1) encryption, weaker forward secrecy). Trade-off: pairwise for groups would mean O(N) encryptions per message (expensive for 1024-member groups). Sender Keys reduce this to O(1) but if a sender key is compromised, all future messages from that sender are readable until key rotation.
- **Minimal metadata vs rich metadata**: WhatsApp stores minimal server-side metadata (who messaged whom, when — but not content). Some argue even this metadata is sensitive. Signal goes further by using techniques like sealed sender to hide sender identity from the server. Trade-off: less metadata = more privacy but harder to operate (can't debug issues, can't detect abuse).

## CRITICAL: The design must be WhatsApp-centric
WhatsApp is the reference implementation. The design should reflect how WhatsApp actually works — its E2E encryption (Signal Protocol), message delivery pipeline, connection management, Erlang-based infrastructure, and server-as-transient-relay philosophy. Where Slack, Telegram, or Discord made different design choices, call those out explicitly as contrasts.

## CRITICAL: Iterative build-up (DO NOT skip this)
The architecture MUST evolve iteratively. Each attempt builds on the previous one by identifying a concrete problem and solving it. The candidate should NOT jump to the final architecture.

### Attempt 0: Single server with HTTP polling
- Client sends HTTP POST to send a message. Receiver polls with HTTP GET every few seconds to check for new messages. Messages stored in a SQL database.
- **Problems found**: High latency (polling interval = delivery delay), wasted bandwidth (most polls return empty), server overwhelmed by poll requests, no delivery guarantees, no encryption, single point of failure.

### Attempt 1: Persistent connections (WebSocket) + message queue
- Replace polling with WebSocket connections. Server pushes messages to recipients in real-time.
- Add a message queue between sender and receiver — server stores messages temporarily, delivers when recipient is connected, ACK-based delivery confirmation.
- **Problems found**: Single WebSocket server can only handle ~100K connections. No encryption (server sees all messages). No group messaging. No media support. Single server = SPOF.

### Attempt 2: Gateway server fleet + message routing + offline delivery
- Multiple gateway servers, each handling 100K-500K connections. Distributed connection registry (userId → gatewayServer).
- Stateless routing layer: looks up recipient's gateway, forwards message. If recipient offline → store in offline queue → deliver on reconnect.
- Add push notifications (APNs/FCM) for offline users.
- **Problems found**: Messages are plaintext on server (privacy risk). No group chat support. Media sent inline with messages (slow, wasteful). Connection registry is a hot spot.

### Attempt 3: E2E encryption + group messaging + media separation
- Implement Signal Protocol (X3DH + Double Ratchet) for 1:1 E2E encryption. Server becomes an untrusted relay — stores only encrypted blobs.
- Add group messaging with fan-out on write. Sender Keys for group E2E encryption.
- Separate media from messages: media uploaded to blob store, message contains reference + encryption key. Thumbnails for preview.
- **Problems found**: No presence / typing indicators. Connection registry is still a potential bottleneck. No multi-device support. Limited fault tolerance (single data center).

### Attempt 4: Presence system + typing indicators + contact sync
- Add presence system (online/offline/last seen). Lazy presence updates (only push to users with open chats) to reduce fan-out.
- Typing indicators via WebSocket (ephemeral, not persisted).
- Contact sync: client uploads hashed phone numbers → server returns matches.
- **Problems found**: Still single data center. Gateway server failure loses all connections. No chaos testing. Limited monitoring.

### Attempt 5: Production hardening (multi-DC, fault tolerance, monitoring)
- Active-active multi-data-center deployment. Message store replicated across DCs. Connection failover — client reconnects to different DC if current DC fails.
- Erlang-based infrastructure: lightweight processes for connection handling, supervisor trees for fault tolerance, hot code upgrades for zero-downtime deploys.
- Comprehensive monitoring: message delivery latency, connection counts, queue depths, error rates.
- Rate limiting: per-user message rate, connection rate, media upload rate.
- Chaos testing: randomly kill gateway servers, simulate DC failures, verify message delivery continues.

## CRITICAL: Accuracy requirements

I DO NOT TRUST AI-GENERATED FACTS ABOUT SPECIFIC SYSTEMS. Every concrete claim about WhatsApp internals must be verifiable against official sources. Specifically:

1. **Use WebSearch and WebFetch tools** to look up WhatsApp engineering blog, Signal Protocol documentation, and relevant tech talks BEFORE writing. Search for:
   - "WhatsApp architecture Erlang"
   - "WhatsApp scaling 2 million connections per server"
   - "Signal Protocol Double Ratchet specification"
   - "WhatsApp E2E encryption whitepaper"
   - "WhatsApp message delivery architecture"
   - "WhatsApp group messaging sender keys"
   - "WhatsApp media sharing architecture"
   - "WhatsApp multi-device architecture"
   - "WhatsApp users messages per day statistics"
   - "Discord Elixir architecture"
   - "Telegram MTProto protocol"
   - "Slack architecture messaging"

   **BLANKET PERMISSION**: You have full permission to use WebSearch and WebFetch to read any online documentation, blog posts, or research papers. Do NOT ask the user for permission to read — just read. Read as many pages as needed to verify facts.

2. **For every concrete number** (messages per day, concurrent connections, team size, group size limits), verify against official sources. If you cannot verify a number, explicitly write "[UNVERIFIED — check official sources]" next to it.

3. **For every claim about WhatsApp internals** (delivery pipeline, encryption flow, storage model), if it's not from an official source, mark it as "[INFERRED — not officially documented]".

4. **CRITICAL: Do NOT confuse WhatsApp with Slack/Telegram/Discord.** These are different systems:
   - WhatsApp: phone-based identity, E2E encryption by default, server as transient relay, small groups, Erlang stack
   - Telegram: cloud-based, no default E2E, large groups/channels, MTProto protocol
   - Slack: enterprise/workspace-centric, no E2E, persistent storage, rich integrations
   - Discord: community/server model, voice channels, no E2E, Elixir/BEAM stack

## Key WhatsApp topics to cover

### Requirements & Scale
- Real-time 1:1 and group messaging with sub-200ms delivery latency (online recipients)
- 2+ billion users, ~100 billion messages/day, ~6.5 billion media items/day
- E2E encryption by default (Signal Protocol) — server never sees plaintext
- Groups up to 1024 members
- Presence, typing indicators, delivery/read receipts
- Media sharing (images, videos, audio, documents)
- Push notifications for offline users
- Multi-device support (phone + 4 companion devices)

### Architecture deep dives (create separate docs as listed above)

### Design evolution (iterative build-up — the most important part)
- Attempt 0: HTTP polling + SQL database
- Attempt 1: WebSocket + message queue
- Attempt 2: Gateway fleet + routing + offline delivery
- Attempt 3: E2E encryption + groups + media separation
- Attempt 4: Presence + typing + contact sync
- Attempt 5: Production hardening (multi-DC, Erlang, monitoring, chaos testing)

Each attempt MUST:
1. Start by identifying concrete problems with the previous attempt
2. Propose a solution
3. Explain WHY this approach (and why not the alternative — mention Telegram/Slack/Discord where relevant)
4. End with "what's still broken?" to motivate the next attempt

### Consistency & Data
- Messages: at-least-once delivery with client-side dedup (idempotent message IDs)
- Ordering: server-assigned sequence numbers per conversation
- Storage: server is transient relay — messages deleted after delivery ACK
- Presence: eventually consistent (slight staleness acceptable)
- E2E encryption: pre-key bundle consistency (one-time pre-keys are consumed exactly once)
- Media: content-addressed encrypted blobs, reference in message

## What NOT to do
- Do NOT treat this as "just a REST API" — the core challenge is real-time delivery over persistent connections, E2E encryption, and connection management at scale.
- Do NOT confuse WhatsApp with Telegram/Slack/Discord. Highlight differences at every layer.
- Do NOT jump to the final architecture. Build it step by step (Attempt 0 → 5).
- Do NOT make up internal implementation details — verify against official sources or mark as inferred.
- Do NOT skip E2E encryption — it is THE defining architectural constraint of WhatsApp. Every design decision must account for the fact that the server cannot read messages.
- Do NOT describe features without explaining WHY they exist (what problem they solve).
- Do NOT ask the user for permission to read online documentation — blanket permission is granted.
- Do NOT write a Wikipedia article. This is an INTERVIEW SIMULATION with dialogue.
