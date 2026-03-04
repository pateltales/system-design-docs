# Message Delivery Pipeline — WhatsApp-Scale Chat

> **Deep-dive companion to:** [01-interview-simulation.md](./01-interview-simulation.md)
> **Scope:** How messages get from sender to receiver — reliably, in order, in real time, at 100B messages/day.

---

## Table of Contents

1. [Message Flow (1:1)](#1-message-flow-11)
2. [Message Ordering](#2-message-ordering)
3. [Delivery Guarantees](#3-delivery-guarantees)
4. [Offline Message Handling](#4-offline-message-handling)
5. [Fan-out for Group Messages](#5-fan-out-for-group-messages)
6. [Message Storage](#6-message-storage)
7. [Retry and ACK Protocol](#7-retry-and-ack-protocol)
8. [Failure Cases and Recovery](#8-failure-cases-and-recovery)
9. [Contrast with Email (Store-and-Forward)](#9-contrast-with-email-store-and-forward)
10. [Contrast with Slack and Discord](#10-contrast-with-slack-and-discord)

---

## 1. Message Flow (1:1)

### The Happy Path — Recipient Online

```
 Sender (Alice)                  Server Infrastructure                    Recipient (Bob)
 ─────────────                   ──────────────────────                   ───────────────
      │                                                                        │
      │  1. Encrypt message                                                    │
      │     (Signal Protocol —                                                 │
      │      Double Ratchet)                                                   │
      │                                                                        │
      │  2. Send via WebSocket                                                 │
      │─────────────────────────►│                                             │
      │   {msgId, convId,        │  Gateway                                    │
      │    encryptedPayload,     │  Server A                                   │
      │    timestamp}            │     │                                       │
      │                          │     │ 3. Write to Cassandra                 │
      │                          │     │    (offline queue — insurance)         │
      │                          │     │─────────────►[Cassandra]              │
      │                          │     │                                       │
      │                          │     │ 4. Lookup Bob's gateway               │
      │                          │     │    in connection registry              │
      │                          │     │─────────────►[Redis]                  │
      │                          │     │◄─────────────                         │
      │                          │     │  "Bob → Gateway B, conn_42"           │
      │                          │     │                                       │
      │                          │     │ 5. Route to Gateway B                 │
      │                          │     │─────────────────────────────►│        │
      │                          │                              Gateway        │
      │                          │                              Server B       │
      │                          │                                   │         │
      │                          │                          6. Push  │         │
      │                          │                             via   │         │
      │                          │                             WS    │         │
      │                          │                                   │────────►│
      │                          │                                   │         │
      │                          │                                   │  7. Bob decrypts
      │                          │                                   │     (Signal Protocol)
      │                          │                                   │         │
      │                          │                                   │  8. Bob sends ACK
      │                          │                                   │◄────────│
      │                          │                                   │         │
      │                          │     9. Delete from Cassandra      │         │
      │                          │        offline queue              │         │
      │                          │◄──────────────────────────────────│         │
      │                          │                                             │
      │  10. Delivery receipt    │                                             │
      │◄─────────────────────────│                                             │
      │      (status: delivered) │                                             │
      │                                                                        │
      │                          │  11. Bob opens chat, reads msg              │
      │                          │◄────────────────────────────────────────────│
      │  12. Read receipt        │      (status: read)                         │
      │◄─────────────────────────│                                             │
      │                                                                        │
```

### Step-by-Step Walkthrough

| Step | Component | Action | Latency Budget |
|------|-----------|--------|----------------|
| 1 | Sender client | Encrypt plaintext with Double Ratchet session key. Produce `encryptedPayload` + message envelope (`msgId`, `convId`, `senderId`, `timestamp`, `messageType`). The `msgId` is a client-generated UUID — critical for idempotency. | ~1-5 ms |
| 2 | Sender client -> Gateway A | Send over existing WebSocket connection. Binary frame (protobuf-encoded). | ~10-50 ms (network) |
| 3 | Gateway A -> Cassandra | **Insurance write.** Store the encrypted message in the offline message queue immediately, BEFORE attempting delivery. This ensures the message survives even if the routing layer or recipient's gateway crashes. Write is asynchronous (fire-and-forget with CL=ONE for speed). | ~5-10 ms |
| 4 | Gateway A -> Redis | Lookup recipient's connection: `userId -> {gatewayServerId, connectionId}`. Redis is the connection registry — sub-millisecond lookups. | ~1-2 ms |
| 5 | Gateway A -> Gateway B | Internal RPC (gRPC or Erlang message passing) to the gateway server holding Bob's connection. If same gateway, this is a local operation. | ~1-5 ms (intra-DC) |
| 6 | Gateway B -> Recipient | Push encrypted message over Bob's WebSocket connection. | ~10-50 ms (network) |
| 7 | Recipient client | Decrypt using Double Ratchet session key. Ratchet forward. Display in chat. | ~1-5 ms |
| 8 | Recipient -> Gateway B | Send delivery ACK: `{msgId, status: DELIVERED}`. | ~10-50 ms |
| 9 | Gateway B -> Cassandra | Delete the message from the offline queue. The server has done its job — the message is now only on the clients. | ~5-10 ms |
| 10 | Gateway B -> Gateway A -> Sender | Route delivery receipt back to the sender. Sender UI updates: single check -> double check. | ~20-60 ms |
| 11 | Recipient client -> Server | When Bob opens the chat and views the message, the client sends a read receipt: `{msgId, status: READ}`. | — |
| 12 | Server -> Sender | Route read receipt to Alice. Sender UI updates: double check -> blue double check. | ~20-60 ms |

**Total end-to-end latency (online recipient, same DC):** ~50-150 ms. Well within the sub-200 ms target.

### The Unhappy Path — Recipient Offline

```
 Sender (Alice)                  Server Infrastructure                    Recipient (Bob)
 ─────────────                   ──────────────────────                   ───────────────
      │                                                                   [OFFLINE]
      │  1-3. Same as above                                                    │
      │  (encrypt, send, store                                                 │
      │   in Cassandra)                                                        │
      │                                                                        │
      │                     4. Lookup Bob → NOT FOUND in                       │
      │                        connection registry                             │
      │                                                                        │
      │                     5. Message stays in Cassandra                      │
      │                        offline queue                                   │
      │                                                                        │
      │                     6. Send push notification                          │
      │                        via APNs/FCM                                    │
      │                        (metadata only — "New                           │
      │                         message from Alice",                           │
      │                         NOT the content — E2E!)                        │
      │                                                                        │
      │  7. Server ACKs to                                                     │
      │     sender: "stored"                                                   │
      │◄─────────────────────                                                  │
      │  (single check ✓)                                                      │
      │                                                                        │
      │                          ═══ TIME PASSES ═══                           │
      │                                                                        │
      │                                                                  [COMES ONLINE]
      │                     8. Bob connects via WebSocket                      │
      │                     9. Bob sends last-known seqNum                     │
      │                        per conversation                                │
      │                     10. Server drains offline queue                    │
      │                         → pushes queued messages                       │
      │                         in sequence order                              │
      │                                                           ◄────────────│
      │                     11. Bob ACKs each message                          │
      │                     12. Server deletes from queue                      │
      │                     13. Delivery receipts sent                         │
      │◄─────────────────────   to Alice                                       │
      │  (double check ✓✓)                                                     │
```

### Key Design Decisions in the Flow

**Why store in Cassandra BEFORE attempting delivery (step 3)?**
This is the "insurance write" pattern. Without it, if Gateway B crashes between receiving the routed message and pushing it to Bob, the message is lost. By writing first, the message is durable regardless of what happens downstream. The tradeoff is an extra write on every message — but at CL=ONE in Cassandra, this adds only ~5 ms.

**Why client-generated msgId (not server-generated)?**
Idempotency. If the sender's network drops after sending but before receiving the server's ACK, the sender will retry. The server uses the `msgId` to deduplicate — if it already has a message with that ID, it skips the duplicate write. Server-generated IDs would make dedup impossible because each retry would get a new ID.

**Why push notifications carry metadata only?**
E2E encryption means the server cannot read the message content. The push payload says "New message from Alice" but not the actual text. On iOS, a Notification Service Extension decrypts the message locally to show a preview. On Android, the app decrypts in the background. [This is documented in WhatsApp's security whitepaper.]

---

## 2. Message Ordering

### The Problem

Messages within a conversation must appear in the same order for all participants. Without ordering guarantees, you get:

```
Alice sends:    "Want to grab lunch?"    then    "At 12:30?"
Bob sees:       "At 12:30?"             then    "Want to grab lunch?"
```

This is unacceptable for a chat application.

### Server-Assigned Sequence Numbers

Each conversation (1:1 or group) maintains a **monotonically increasing sequence counter** on the server.

```
Conversation: Alice <-> Bob
──────────────────────────────
seqNum=1  │  Alice: "Hey!"
seqNum=2  │  Bob:   "Hi there"
seqNum=3  │  Alice: "Want to grab lunch?"
seqNum=4  │  Alice: "At 12:30?"
seqNum=5  │  Bob:   "Sure!"
```

**How it works:**

1. Server receives a message for `conversationId=conv_123`.
2. Atomically increments the sequence counter for `conv_123`.
3. Assigns the new sequence number to the message.
4. Stores the message keyed by `(conversationId, seqNum)`.
5. Delivers the message with the `seqNum` attached.

**Implementation:** The sequence counter is stored in a lightweight coordination service. For Cassandra, use a lightweight transaction (LWT) or a dedicated counter service (Redis INCR on `seq:{conversationId}`). Redis INCR is atomic, single-threaded, and sub-millisecond — ideal for this use case.

### Client-Side Gap Detection

The client tracks the latest `seqNum` it has received for each conversation. When a new message arrives:

```
Expected: seqNum = lastSeen + 1
Received: seqNum = lastSeen + 3

→ Gap detected! Missing seqNum = lastSeen+1, lastSeen+2
→ Client requests re-delivery: GET /messages/{convId}?after={lastSeen}&before={received}
```

This handles out-of-order delivery (which can happen if messages are routed through different gateway servers) and message loss (gateway crash before push).

### Causal Ordering in Groups

For group chats, simple sequence numbers maintain a **total order** — all members see messages in the same order. This is sufficient for most chat UX.

However, total ordering does not capture **causality**. Consider:

```
seqNum=7  │  Alice: "Where should we eat?"
seqNum=8  │  Charlie: "I love sushi"         (reply to Alice)
seqNum=9  │  Bob: "Italian place on 5th"     (also reply to Alice, but sent before seeing Charlie's message)
```

Bob's message (seqNum=9) appears after Charlie's (seqNum=8), which might look like Bob is responding to Charlie. But Bob never saw Charlie's message — he was replying to Alice.

**True causal ordering** would use vector clocks or Lamport timestamps to capture "happened-before" relationships. But in practice:

- WhatsApp uses server-assigned total ordering (not causal ordering). [INFERRED — not officially documented]
- The UX impact of causal misordering is minimal in small groups.
- Reply-to-message feature (quoting) solves the ambiguity problem at the application level.
- Vector clocks add significant complexity (each message carries an O(N) vector where N = group members) — not worth it for groups of up to 1,024.

**Contrast with academic distributed systems:** Systems like Google Spanner use TrueTime for global ordering. Chat does not need that level of precision — within the same conversation, a single sequence counter on a single coordinating node is sufficient. Cross-conversation ordering is irrelevant (no user cares if a message in Group A was "before" a message in a DM with Bob).

---

## 3. Delivery Guarantees

### At-Least-Once Delivery

The fundamental guarantee: **every message is delivered at least once**. The server retries until it receives an ACK from the recipient (or the 30-day retention window expires).

```
Guarantee Spectrum:
──────────────────────────────────────────────────────────────────
  At-most-once          At-least-once           Exactly-once
  (fire & forget)       (retry until ACK)       (impossible in
                                                 distributed systems,
                                                 but approximated)
        ▲                     ▲
        │                     │
     Email SMTP          WhatsApp's
     (best effort)       approach
```

**Why not exactly-once?** In a distributed system with unreliable networks, exactly-once delivery is theoretically impossible (Two Generals Problem). Instead, WhatsApp achieves **effectively-once** semantics through at-least-once delivery + client-side deduplication.

### Client-Side Deduplication

Every message carries a client-generated `msgId` (UUID). The client maintains a set of recently received `msgId` values:

```
Client receives message with msgId = "abc-123"
  → Check: is "abc-123" in recentMsgIds?
    → YES: Discard (duplicate). Send ACK anyway to stop retries.
    → NO:  Process message. Add "abc-123" to recentMsgIds. Send ACK.
```

The dedup window must be large enough to cover the server's retry window (see Section 7). In practice, the client can use a bounded LRU set or Bloom filter for the dedup check.

### Idempotent Writes on the Server

The server also deduplicates using `msgId` before writing to Cassandra:

```sql
-- Cassandra: INSERT IF NOT EXISTS (lightweight transaction)
INSERT INTO messages (conversation_id, sequence_num, message_id, ...)
VALUES (?, ?, ?, ...)
IF NOT EXISTS;
```

If the sender retries (network drop after send, before server ACK), the server sees the same `msgId` and skips the duplicate. The response to the sender is the same either way — "message accepted."

### Per-Recipient Delivery Tracking (Groups)

For group messages, delivery is tracked per member:

```
message_id: "msg-789"
group_id: "group-456"
delivery_status:
  alice:   DELIVERED  (ACK received)
  bob:     SENT       (pushed via WS, awaiting ACK)
  charlie: QUEUED     (offline, in Cassandra queue)
  diana:   DELIVERED  (ACK received)
```

The server retains the message in storage until ALL members have ACKed. The sender sees individual delivery/read receipts (visible in WhatsApp's "message info" screen).

---

## 4. Offline Message Handling

### Persistent Queue in Cassandra

When a recipient is offline (no entry in the connection registry, or connection marked stale), messages are stored in a dedicated offline queue:

```
Table: offline_messages
─────────────────────────────────────────────────────────────────
Partition Key    │ Clustering Key   │ Columns
─────────────────────────────────────────────────────────────────
recipient_id     │ sequence_num     │ message_id, conversation_id,
                 │ (ASC)            │ sender_id, encrypted_payload,
                 │                  │ timestamp, message_type,
                 │                  │ ttl (30 days)
```

**Why Cassandra for the offline queue?**

| Requirement | Why Cassandra Fits |
|-------------|-------------------|
| High write throughput | ~1.15M messages/sec across all users. Cassandra's LSM-tree storage is write-optimized. |
| Ordered range reads | Drain all messages for a user in sequence order. Clustering key on `sequence_num` makes this a single sequential read. |
| TTL-based expiry | Built-in TTL support. Messages auto-delete after 30 days without a background job. |
| Partitioned by recipient | Each user's queue is an independent partition. No cross-partition coordination needed. |
| Availability over consistency | AP system (tunable). A temporary inconsistency (duplicate message) is acceptable — client deduplicates. |

### Drain on Reconnect

When a user comes back online:

```
1. Client connects via WebSocket
2. Client sends: {lastSeenSeqNums: {conv_A: 47, conv_B: 112, conv_C: 5}}
3. Server queries offline queue:
   SELECT * FROM offline_messages
   WHERE recipient_id = 'bob'
   AND sequence_num > ?
   ORDER BY sequence_num ASC
   LIMIT 100;                       ← pagination
4. Server pushes messages to client over WebSocket
5. Client ACKs each batch
6. Server deletes ACKed messages from queue
7. Repeat until queue is drained
```

### Pagination for Long Offline Periods

A user offline for a week might have thousands of queued messages. Pushing them all at once would:
- Overwhelm the client (memory, rendering)
- Hog the WebSocket connection (blocking real-time messages)
- Risk timeout/disconnection

**Solution:** Batch delivery with pagination.

```
Batch 1: messages 1-100    → push → ACK → delete
Batch 2: messages 101-200  → push → ACK → delete
  ...interleave with real-time messages if they arrive...
Batch N: messages (N-1)*100+1 to N*100 → push → ACK → delete
```

The client shows a "Syncing messages..." indicator during the drain. Real-time messages arriving during the drain are interleaved — they take priority over historical catch-up.

### 30-Day Retention Limit

Messages that remain undelivered for 30 days are dropped (Cassandra TTL). This is consistent with WhatsApp's "server as transient relay" philosophy:

- The server is not a permanent store. It holds messages only long enough to deliver them.
- If a user is offline for 30+ days, those messages are gone from the server. The sender's device still has them (local storage).
- This drastically reduces server-side storage requirements. At WhatsApp's scale (100B messages/day), permanent storage would require petabytes per day. Transient storage with aggressive TTL keeps the steady-state manageable.

**Contrast with Slack:** Slack retains ALL messages forever. Their storage grows monotonically. This enables full-text search, compliance exports, and multi-device access to history — but requires a fundamentally different (and more expensive) storage architecture.

---

## 5. Fan-out for Group Messages

### The Core Problem

Alice sends "Hello everyone!" to a group with 500 members. How does that one message reach 500 people?

Two fundamental strategies exist.

### Strategy 1: Write-Time Fan-out (Fan-out on Write)

```
Alice sends 1 message
         │
         ▼
   ┌─────────────┐
   │   Server     │
   │  Fan-out     │
   │  Service     │
   └──────┬──────┘
          │
    ┌─────┼─────┬─────┬─────┬─── ... ───┐
    ▼     ▼     ▼     ▼     ▼           ▼
  Bob's  Carol's Dan's Eve's Frank's   User500's
  inbox  inbox  inbox inbox  inbox      inbox

  500 writes (one per member)
```

**How it works:**
1. Alice sends encrypted message to server.
2. Server looks up group membership: 500 members.
3. Server writes a copy of the message into each member's personal inbox/offline queue.
4. Each member reads from their own inbox — fast, single-partition read.

**Quantitative analysis at WhatsApp scale:**

```
Assumptions:
- 100B messages/day total
- ~30% are group messages = 30B group messages/day
- Average group size = 20 members [INFERRED]
- Fan-out multiplier = 20x

Write amplification:
  30B group messages × 20 fan-out = 600B inbox writes/day
  + 70B 1:1 messages = 70B inbox writes/day
  Total: ~670B writes/day = ~7.75M writes/sec

Worst case (1024-member group, active):
  1 message → 1024 writes
  100 messages/min in active group → 102,400 writes/min = ~1,707 writes/sec
  Still manageable for a single partition spread across the cluster.
```

**Pros:**
- Read path is trivial: each user reads from their own inbox (single partition).
- Delivery tracking is simple: each inbox entry has its own ACK status.
- Client logic is identical to 1:1 messages — no special group handling.
- Latency is predictable: one read per user, regardless of group activity.

**Cons:**
- Write amplification: 1 message becomes N writes (N = group size).
- Storage amplification: N copies of the same encrypted payload.
- Group size is bounded: at 1024 members, write amplification is tolerable. At 200K members (Telegram) or millions (Discord), it is not.

### Strategy 2: Read-Time Fan-out (Fan-out on Read)

```
Alice sends 1 message
         │
         ▼
   ┌─────────────┐
   │   Server     │
   │  Stores once │
   │  in group    │
   │  message log │
   └──────┬──────┘
          │
          ▼
   ┌──────────────┐
   │ Group Log    │
   │ conv_group1  │
   │  seq=1: ...  │
   │  seq=2: ...  │
   │  seq=3: msg  │◄── stored once
   └──────────────┘
          ▲  ▲  ▲  ▲         ▲
          │  │  │  │   ...   │
        Bob Carol Dan Eve  User500
        reads from group log on demand
```

**How it works:**
1. Alice sends message to server.
2. Server writes ONE copy to the group's message log.
3. When each member opens the group chat, they read from the group log.
4. Server pushes a lightweight notification ("new message in group X") to online members, who then fetch from the log.

**Quantitative analysis at Discord scale:**

```
Assumptions:
- Discord server with 1M members
- Active channel: 100 messages/min

Write-time fan-out cost:
  100 messages × 1M members = 100M writes/min → INFEASIBLE

Read-time fan-out cost:
  100 writes/min (one per message)
  Reads: only active viewers read. If 10K members are viewing the channel:
    10K reads per new message (or batched via pub/sub push)
  Much more manageable.
```

**Pros:**
- Write cost is O(1) per message, regardless of group size.
- Storage is O(1) per message — no duplication.
- Supports unbounded group/channel sizes.

**Cons:**
- Read path is more complex: each reader must fetch from the group log, track their own read position.
- Delivery tracking is harder: the server doesn't know who has read what unless members report it.
- Latency for the read path depends on the group log's read throughput — hot groups can be a bottleneck.
- Push notification for "new message" is still a fan-out problem (but the payload is tiny: just a notification, not the full message).

### Why WhatsApp Uses Write-Time Fan-out

```
┌────────────────────────────────────────────────────────────────┐
│                    DECISION MATRIX                             │
├────────────────────┬──────────────┬──────────────┬─────────────┤
│ Factor             │ Write-time   │ Read-time    │ WhatsApp    │
│                    │ fan-out      │ fan-out      │ choice      │
├────────────────────┼──────────────┼──────────────┼─────────────┤
│ Max group size     │ ~1-2K        │ Millions     │ 1,024 max   │
│ Write cost         │ O(N)         │ O(1)         │ Tolerable   │
│ Read cost          │ O(1)         │ O(N_active)  │ Fast reads  │
│ Read latency       │ Predictable  │ Variable     │ Sub-200ms   │
│ Delivery tracking  │ Simple       │ Complex      │ Simple ✓    │
│ Client complexity  │ Low          │ High         │ Low ✓       │
│ Offline delivery   │ Easy         │ Hard         │ Easy ✓      │
│ E2E encryption     │ Per-inbox    │ From log     │ Sender Keys │
│ Server role        │ Active relay │ Passive log  │ Active ✓    │
└────────────────────┴──────────────┴──────────────┴─────────────┘
```

WhatsApp's group size cap of 1,024 members makes write-time fan-out feasible:
- Maximum write amplification per message: 1,024x.
- At 1 KB per message: 1 MB of writes per group message in the worst case.
- Cassandra handles this easily with its write-optimized LSM-tree storage.

And the benefits are significant:
- Read latency is predictable and fast — critical for real-time chat.
- Offline delivery works identically to 1:1 — each user's queue is drained independently.
- E2E encryption with Sender Keys means the message is encrypted once by the sender and written (still encrypted) into each member's inbox.

### Why Discord Uses Read-Time Fan-out

Discord servers can have millions of members. A single message in a popular channel would require millions of inbox writes — unacceptable write amplification. Instead:

- Messages are stored once per channel.
- Online members in the channel receive a push via the pub/sub gateway.
- Members who open the channel later read from the channel log.
- Discord does not have delivery receipts for channels (only DMs) — this simplifies the model.
- Discord does not have E2E encryption — messages are plaintext on the server, so a single stored copy is readable by all members.

### Hybrid Approach

A system that supports both small groups and large channels could use a hybrid:

```
if group.memberCount <= THRESHOLD (e.g., 256):
    use write-time fan-out     # fast reads, simple delivery
else:
    use read-time fan-out      # bounded writes, scalable
```

Telegram likely uses something like this: small groups get fan-out on write; channels (unlimited subscribers) get fan-out on read. [INFERRED — not officially documented]

---

## 6. Message Storage

### Cassandra Schema

```sql
-- Primary message store (offline queue / transient storage)
CREATE TABLE messages (
    conversation_id  TEXT,          -- partition key
    sequence_num     BIGINT,        -- clustering key (ASC)
    message_id       TEXT,          -- client-generated UUID (for dedup)
    sender_id        TEXT,
    encrypted_payload BLOB,         -- E2E encrypted, server cannot read
    message_type     TEXT,          -- TEXT, IMAGE, VIDEO, AUDIO, DOCUMENT
    media_ref        TEXT,          -- mediaId for media messages (pointer to blob store)
    timestamp        TIMESTAMP,     -- server-assigned receipt time
    PRIMARY KEY (conversation_id, sequence_num)
) WITH CLUSTERING ORDER BY (sequence_num ASC)
  AND default_time_to_live = 2592000   -- 30-day TTL (30 × 24 × 3600)
  AND compaction = {'class': 'TimeWindowCompactionStrategy',
                    'compaction_window_size': '1',
                    'compaction_window_unit': 'DAYS'};

-- Per-recipient delivery status (for group messages)
CREATE TABLE delivery_status (
    message_id       TEXT,          -- partition key
    recipient_id     TEXT,          -- clustering key
    status           TEXT,          -- QUEUED, SENT, DELIVERED, READ
    updated_at       TIMESTAMP,
    PRIMARY KEY (message_id, recipient_id)
) WITH default_time_to_live = 2592000;

-- Offline message queue (per-user view)
CREATE TABLE offline_queue (
    recipient_id     TEXT,          -- partition key
    sequence_num     BIGINT,        -- clustering key (ASC) — per-user global seqnum
    conversation_id  TEXT,
    message_id       TEXT,
    encrypted_payload BLOB,
    sender_id        TEXT,
    timestamp        TIMESTAMP,
    PRIMARY KEY (recipient_id, sequence_num)
) WITH CLUSTERING ORDER BY (sequence_num ASC)
  AND default_time_to_live = 2592000;
```

### Why Partition by conversationId?

All messages in a conversation are co-located on the same Cassandra node (or set of replicas). This makes conversation history retrieval a single-partition range scan — the fastest read pattern in Cassandra.

```
Query: "Give me messages 50-100 in conversation conv_123"

SELECT * FROM messages
WHERE conversation_id = 'conv_123'
AND sequence_num >= 50
AND sequence_num <= 100;

→ Single partition read. Sequential disk I/O. Sub-10ms.
```

### Why Cluster by sequenceNumber?

Messages are stored on disk in sequence order within each partition. Fetching a range of messages (for pagination, gap filling, or offline drain) is a sequential read — no random I/O.

### Why TTL-Based Deletion?

WhatsApp deletes messages from the server after delivery (or after 30 days if undelivered). Cassandra's built-in TTL handles this automatically:

- Each row gets a TTL of 30 days at write time.
- Cassandra marks rows as tombstones when TTL expires.
- `TimeWindowCompactionStrategy` efficiently removes expired data by compacting time-window-aligned SSTables.
- No background cron job or manual deletion needed.

**Contrast with Slack:** Slack stores messages permanently in MySQL (via Vitess sharding). They need message indexes, full-text search (Elasticsearch), compliance archives, and ever-growing storage. WhatsApp's transient model avoids all of this.

### Why Cassandra?

| Requirement | Why Cassandra |
|-------------|--------------|
| Write throughput: ~7.75M writes/sec (including fan-out) | LSM-tree storage: writes are append-only, no read-before-write |
| Partition-local reads for conversation history | Wide-column model with partition + clustering keys |
| TTL-based auto-expiry | Built-in TTL, compacted by TimeWindowCompactionStrategy |
| Availability > consistency | AP system (tunable). Temporary inconsistency is fine — client deduplicates |
| Horizontal scaling | Add nodes to the ring. Data automatically rebalances |
| Replication | Configurable replication factor (RF=3 typical). Survives node failures |

**Why NOT a relational database (MySQL/Postgres)?**
- Cannot handle 7.75M writes/sec on a single cluster without extreme sharding.
- Row-level locking for sequence number assignment is a bottleneck.
- TTL requires application-level deletion jobs.
- Joins are unnecessary — the data model is denormalized by design.

**Why NOT Redis alone?**
- Redis is used for the connection registry (volatile, needs speed), but messages need durability. Redis persistence (RDB/AOF) is not designed for petabyte-scale message storage.
- Redis works perfectly for the connection registry (`userId -> gatewayServer`) and sequence counters (`INCR seq:{convId}`).

---

## 7. Retry and ACK Protocol

### Protocol Definition

```
┌─────────┐                    ┌──────────┐                    ┌──────────┐
│  Server  │                   │ Network  │                    │  Client  │
└────┬─────┘                   └────┬─────┘                    └────┬─────┘
     │                              │                               │
     │  PUSH message (attempt 1)    │                               │
     │─────────────────────────────►│──────────────────────────────►│
     │                              │                               │
     │  Start retry timer           │                               │
     │  (T = 2 seconds)             │                               │
     │                              │                               │
     │                              │     ACK {msgId}               │
     │◄─────────────────────────────│◄──────────────────────────────│
     │                              │                               │
     │  ACK received.               │                               │
     │  Cancel timer.               │                               │
     │  Delete from queue.          │                               │
     │                              │                               │
```

### Retry with Exponential Backoff

When the ACK is not received within the timeout:

```
Attempt  │  Timeout   │  Action
─────────┼────────────┼──────────────────────────────────
   1     │   2 sec    │  Push message via WebSocket
   2     │   4 sec    │  Re-push (connection may be stale)
   3     │   8 sec    │  Re-push (check if connection alive)
   4     │  16 sec    │  Re-push via new connection if available
   5     │  32 sec    │  Mark connection as dead. Stop retrying over WS.
         │            │  Message remains in Cassandra offline queue.
         │            │  Send push notification via APNs/FCM.
─────────┴────────────┴──────────────────────────────────
Max retries over WebSocket: 5
Total retry window: 2 + 4 + 8 + 16 + 32 = 62 seconds
After max retries: fall back to offline queue + push notification
```

### Jitter

Add random jitter to each retry interval to prevent thundering herd problems when many connections recover simultaneously:

```
actual_timeout = base_timeout × 2^attempt + random(0, base_timeout × 2^attempt × 0.5)
```

### Idempotent ACKs

ACKs are idempotent — receiving the same ACK multiple times is harmless:

```
Server receives ACK for msgId = "abc-123"
  → Check: is msgId still in the queue?
    → YES: Delete it. Log delivery.
    → NO:  Already deleted (duplicate ACK). No-op.
```

This is critical because the client might send multiple ACKs (network caused a retry of the ACK itself).

### Server-to-Sender ACK

The server also ACKs back to the sender when it has accepted the message:

```
Sender sends message → Server writes to Cassandra → Server ACKs to sender

Server ACK means: "I have durably stored your message and will deliver it."
This is the "single check" (✓) in WhatsApp's UI.
```

If the sender doesn't receive this ACK (network drop), the sender retries the entire send. The server deduplicates using `msgId`.

---

## 8. Failure Cases and Recovery

### Failure Case 1: Network Drop Between Sender and Gateway

```
Scenario: Alice sends a message, but the network drops before the server receives it.
Detection: Sender's WebSocket write fails or times out.
Recovery:
  1. Client buffers the message locally.
  2. Client detects disconnection (missing heartbeat response or write failure).
  3. Client reconnects (possibly to a different gateway).
  4. Client resends all buffered messages with original msgIds.
  5. Server deduplicates — if any were actually received, the duplicate is ignored.
Data loss: None. Message is in client's local buffer until ACKed by server.
```

### Failure Case 2: Gateway Server Crash After Receiving Message, Before Cassandra Write

```
Scenario: Gateway A receives Alice's message, crashes before writing to Cassandra.
Detection: Sender doesn't receive server ACK within timeout.
Recovery:
  1. Sender retries (same msgId).
  2. Sender's WebSocket is dead (gateway crashed). Client reconnects to Gateway C.
  3. Gateway C processes the message normally — writes to Cassandra, routes to recipient.
Data loss: None. The sender retried with the same msgId.
Risk: If Gateway A partially processed (e.g., wrote to Cassandra but crashed
       before ACKing sender), the sender retries and Cassandra deduplicates
       via IF NOT EXISTS on msgId.
```

### Failure Case 3: Gateway Crash After Cassandra Write, Before Delivery to Recipient

```
Scenario: Gateway A writes Alice's message to Cassandra, then crashes before
          routing to Bob's gateway.
Detection: The message sits in Cassandra's offline queue with no delivery ACK.
Recovery:
  1. Bob eventually comes online (or is already online on another gateway).
  2. On Bob's next reconnect, the offline queue is drained — the message is delivered.
  3. If Bob is currently online, a background queue monitor detects undelivered
     messages and re-attempts delivery.
Data loss: None. The insurance write to Cassandra saved the message.
Latency impact: Delivery is delayed until the queue is drained (seconds to minutes).
```

### Failure Case 4: Cassandra Partition Failure

```
Scenario: The Cassandra node holding Bob's offline queue partition goes down.
Detection: Write to offline queue fails (CL=ONE fails if all replicas for that
           partition are down; CL=QUORUM fails if majority are down).
Recovery:
  Option A (RF=3, CL=ONE): Two other replicas still accept the write.
           Single node failure is transparent.
  Option B (all replicas down): Gateway holds the message in memory and retries
           the Cassandra write with exponential backoff. If the outage is
           prolonged (>30 sec), fall back to writing to a different partition
           (e.g., a "spillover" queue) and reconcile later.
  Option C (read failure during drain): Client reconnects and retries the drain.
           Cassandra hinted handoff replays writes to the recovered node.
Data loss: With RF=3, data loss requires simultaneous failure of 3 nodes holding
           the same partition — extremely unlikely.
```

### Failure Case 5: Connection Registry (Redis) Failure

```
Scenario: Redis cluster holding the connection registry goes down.
          Server cannot look up which gateway holds Bob's connection.
Detection: Lookup for Bob's gateway returns error or timeout.
Recovery:
  1. Treat Bob as offline — write message to Cassandra offline queue.
  2. Send push notification via APNs/FCM.
  3. When Redis recovers, normal routing resumes.
  4. If Bob is actually online, his gateway will eventually drain the queue
     (or Bob receives the push notification, opens the app, triggering a drain).
Impact: Delivery is delayed but not lost. Messages degrade to offline-mode delivery.
Mitigation: Redis Cluster with replicas. Redis Sentinel for automatic failover.
            Multiple Redis shards — a single shard failure only affects a subset of users.
```

### Failure Case 6: Push Notification Service (APNs/FCM) Failure

```
Scenario: APNs or FCM is unreachable. Server cannot send push notifications
          to offline users.
Detection: Push service API returns errors or times out.
Recovery:
  1. Messages are already in Cassandra offline queue (push notification is a
     "best effort" hint, not part of the delivery guarantee).
  2. Queue the push notification for retry.
  3. When the user opens the app (even without the push), the client connects
     and drains the offline queue.
Impact: User doesn't know they have new messages until they open the app.
        No message loss — messages are safely in Cassandra.
Note: This is why the Cassandra write (step 3 in the message flow) happens
      BEFORE the push notification — the queue is the source of truth,
      not the push notification.
```

### Failure Case 7: Split Brain — User Connected to Two Gateways

```
Scenario: Bob's connection to Gateway B appears stale (missed heartbeats due to
          network issues), but Bob is actually still connected. Meanwhile, Bob
          reconnects to Gateway C. Now two gateways think they have Bob's connection.
Detection: Connection registry shows Bob on Gateway C (latest entry).
           Gateway B still has a lingering WebSocket.
Recovery:
  1. Messages are routed to Gateway C (latest registry entry).
  2. Gateway B's stale connection eventually times out (heartbeat failure).
  3. If a message is pushed to BOTH gateways (race condition), client deduplicates
     using msgId.
  4. Worst case: Bob receives the same message twice. Client ignores the duplicate.
Impact: No message loss. Possible duplicate delivery (handled by client dedup).
```

### Summary of Failure Recovery Principles

| Principle | Implementation |
|-----------|---------------|
| **Durability before routing** | Write to Cassandra BEFORE attempting delivery |
| **Idempotent operations** | Client msgId for dedup; ACKs are idempotent |
| **Retry with backoff** | Exponential backoff + jitter on all retries |
| **Graceful degradation** | If real-time delivery fails, fall back to offline queue |
| **Client is the backstop** | Client buffers unsent messages; retries on reconnect |
| **No single point of failure** | Cassandra RF=3, Redis cluster, multiple gateways |

---

## 9. Contrast with Email (Store-and-Forward)

### Fundamental Architectural Differences

```
                    EMAIL (SMTP)                          CHAT (WhatsApp)
                    ────────────                          ────────────────

Delivery model:     Store-and-forward                     Real-time push
                    (relay through MTAs)                  (direct via persistent conn)

Connection:         Transient TCP per send                Persistent WebSocket
                    (connect → send → disconnect)         (always-on while app is open)

Latency target:     Minutes to hours acceptable           Sub-200ms for online users

Protocol:           SMTP → relay → relay → IMAP/POP      WebSocket → route → WebSocket

Server role:        Permanent store (mailbox)             Transient relay (delete after ACK)

Encryption:         TLS in transit (server reads           E2E (server NEVER reads content)
                    plaintext), optional PGP/S-MIME

Ordering:           Loose (arrival time at mailbox)       Strict (sequence numbers)

Delivery receipt:   Optional, unreliable (MDN)            Built-in, reliable (ACK-based)

Offline handling:   Always offline — email IS              Exception — queue + drain on
                    store-and-forward by design            reconnect

Addressing:         DNS MX record lookup per domain        Connection registry lookup per user
```

### Why Chat Cannot Use Email's Architecture

1. **Latency:** Email's store-and-forward model involves DNS lookups, MTA relays, spam filtering, and mailbox delivery. Each hop adds seconds. Chat needs sub-200 ms — every millisecond matters.

2. **Persistent connections:** Email clients poll (IMAP IDLE is a partial exception). Chat uses always-on WebSocket connections. This is what enables real-time delivery, typing indicators, and presence updates — none of which are possible with email's model.

3. **Delivery guarantees:** Email delivery is best-effort. SMTP has no end-to-end ACK mechanism — the sender has no reliable way to know if the email was delivered, let alone read. Chat has a rigorous ACK protocol with guaranteed delivery tracking.

4. **Encryption model:** Email servers must read message headers (and often bodies) for routing, spam filtering, and compliance. Chat's E2E encryption means the server is a blind relay — it cannot inspect content. This precludes server-side features like search or spam filtering on message content.

5. **Server storage:** Email servers store messages permanently (mailboxes). Chat servers store messages transiently (delete after delivery). This is a philosophical difference: email is a filing system; chat is a conversation.

---

## 10. Contrast with Slack and Discord

### Slack's Delivery Model

```
WhatsApp                                    Slack
────────                                    ─────

Server as transient relay                   Server as permanent store
  → delete after delivery ACK                 → store forever, index, search

E2E encrypted                               Server-side encrypted (at rest)
  → server cannot read content                → server CAN read content
                                              → enables server-side search, compliance

Phone-number identity                       Workspace/email identity
  → consumer, contact-based                   → enterprise, team-based

Write-time fan-out (groups ≤1024)           Channel-based pub/sub
  → per-user inbox                            → messages stored per-channel
                                              → members read from channel log

At-least-once, ACK-based                    At-least-once, with catch-up
  → server retries until ACK                  → server stores permanently
  → client drains offline queue               → client fetches history on open

Custom protocol (XMPP-derived)              WebSocket + REST Events API
over persistent TCP                           → bots use Events API (HTTP callbacks)
                                              → humans use WebSocket

30-day message retention (undelivered)      Unlimited retention
                                              → paid plans: full history
                                              → free plans: limited history window

Cassandra (write-optimized,                 MySQL + Vitess (ACID, strong consistency)
  AP, TTL-based expiry)                       + Elasticsearch (full-text search)
                                              + Redis (caching, presence)
```

**Why Slack stores messages permanently:** Enterprise customers need compliance (legal hold, eDiscovery), audit trails, and searchable history. A "delete after delivery" model would make Slack useless for its core use case.

**Why Slack does not use E2E encryption:** Server-side access is a feature, not a bug, for Slack's audience. Admins need to search messages, moderate content, and export data. E2E encryption would prevent all of this.

### Discord's Delivery Model

```
WhatsApp                                    Discord
────────                                    ───────

Small groups (≤1024 members)                Servers with millions of members
  → write-time fan-out feasible               → read-time fan-out required

1:1 and group messaging                     Server → Channel model
  → flat conversation model                   → hierarchical: server/category/channel

Delivery receipts (per-message)             No delivery receipts in channels
  → sent, delivered, read                     → only in DMs
  → group: per-member receipts                → channels: fire-and-forget push

E2E encrypted                               No E2E encryption
  → Sender Keys for groups                    → plaintext on server
                                              → enables moderation, search

Persistent WebSocket (messaging)            Persistent WebSocket (messaging)
                                            + UDP (voice channels)
                                            + WebRTC (video/screen share)

Erlang/BEAM for connections                 Elixir/BEAM for connections
  → same underlying VM                        → same concurrency model
  → 2M connections per server                 → similar scale per node

Mobile-first (battery-conscious)            Desktop/gaming-first
  → aggressive connection management          → richer real-time features
  → minimal background data                   → presence includes game activity

Consumer/private messaging                  Community/public messaging
  → privacy is paramount                      → discoverability is paramount
```

**Why Discord uses read-time fan-out:** A Discord server can have millions of members. Write-time fan-out for a message in a channel with 1M viewers would mean 1M writes — impossible. Instead, Discord stores messages once per channel. When a user opens a channel, they fetch the latest messages. Online users get a real-time push via WebSocket, but there is no per-user inbox and no delivery guarantees for channel messages.

**Why Discord does not have delivery receipts in channels:** With potentially millions of members, tracking per-member delivery status would be prohibitively expensive. The cost would be `O(messages x members)` — scaling to trillions of status records. DMs are different: small fan-out, delivery receipts are feasible.

**Shared infrastructure:** Both WhatsApp and Discord use the BEAM VM (Erlang and Elixir, respectively) for connection handling. This is not a coincidence — the BEAM VM's lightweight process model (millions of processes per node, ~2 KB each, preemptive scheduling) is ideal for managing hundreds of thousands of concurrent WebSocket connections per server.

### Summary Comparison Table

| Dimension | WhatsApp | Slack | Discord |
|-----------|----------|-------|---------|
| **Delivery model** | Real-time push, transient relay | Persistent store + real-time push | Persistent store + channel pub/sub |
| **Fan-out** | Write-time (groups ≤ 1024) | Channel-based (read from channel) | Read-time (servers up to millions) |
| **Encryption** | E2E (Signal Protocol) | TLS in transit, at-rest on server | TLS in transit, at-rest on server |
| **Storage** | Cassandra, TTL 30 days | MySQL/Vitess, permanent | Cassandra + ScyllaDB, permanent |
| **Delivery receipts** | Yes (per-message, per-recipient) | Yes (per-message) | DMs only (not channels) |
| **Offline handling** | Queue + drain on reconnect | Catch-up from persistent store | Catch-up from persistent store |
| **Message retention** | Delete after delivery (30-day max) | Permanent (plan-dependent) | Permanent |
| **Ordering** | Sequence numbers per conversation | Sequence per channel | Snowflake IDs (time-ordered) |
| **Max group/channel** | 1,024 members | Unlimited (workspace) | Millions (server) |
| **Primary runtime** | Erlang/BEAM | Java (backend), PHP (legacy) | Elixir/BEAM |
| **Scale** | 2B+ users, 100B msg/day | ~40M+ DAU [UNVERIFIED] | 150M+ MAU [UNVERIFIED] |

---

## Key Takeaways for Interviews

1. **The insurance write pattern** (write to durable store BEFORE attempting real-time delivery) is the cornerstone of reliable messaging. It decouples durability from delivery.

2. **At-least-once + client dedup = effectively-once.** Do not claim exactly-once delivery in a distributed system — explain why it is approximated instead.

3. **Fan-out strategy is driven by group size limits.** WhatsApp's 1,024-member cap makes write-time fan-out feasible. Discord's unbounded servers require read-time fan-out. This is a product decision driving an architecture decision.

4. **Sequence numbers, not timestamps, for ordering.** Timestamps have clock skew issues. Server-assigned monotonic sequence numbers give a total order within each conversation.

5. **The server is a blind relay.** E2E encryption means the server cannot inspect, index, or search message content. Every feature that requires content access (spam filtering, compliance, search) must happen on the client.

6. **Failure recovery boils down to: retry + idempotency + durable queue.** Every failure case in Section 8 is resolved by some combination of these three primitives.
