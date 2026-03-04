# Data Storage & Data Model — WhatsApp-Like Chat Application

> **Deep-dive companion doc for Phase 5+ of the interview simulation.**
> **Context:** L6 candidate designing storage layer for a WhatsApp-scale real-time messaging system.

---

## Table of Contents

1. [Message Store](#1-message-store)
2. [Conversation Metadata Store](#2-conversation-metadata-store)
3. [User Profile Store](#3-user-profile-store)
4. [Pre-Key Store (E2E Encryption)](#4-pre-key-store-e2e-encryption)
5. [Media Store](#5-media-store)
6. [Offline Message Queue](#6-offline-message-queue)
7. [Connection Registry](#7-connection-registry)
8. [Scale Numbers & Storage Calculations](#8-scale-numbers--storage-calculations)
9. [Why Cassandra — Database Comparison](#9-why-cassandra--database-comparison)
10. [WhatsApp's Original Stack](#10-whatsapps-original-stack)
11. [Contrast with Slack](#11-contrast-with-slack)
12. [Contrast with Discord](#12-contrast-with-discord)

---

## 1. Message Store

The message store is the **largest, most write-heavy, and most latency-sensitive** data store in the entire system. Every single message flows through it. The defining constraint: the server never sees plaintext. It stores **encrypted blobs + metadata only**.

### 1.1 Schema

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                          messages table                                     │
├──────────────────┬──────────────────────────────────────────────────────────┤
│  PARTITION KEY   │  conversationId (TEXT/UUID)                              │
│  CLUSTERING KEY  │  sequenceNumber (BIGINT) ASC                            │
├──────────────────┼──────────────────────────────────────────────────────────┤
│  COLUMNS         │  messageId        (UUID)    — globally unique           │
│                  │  senderId         (UUID)    — who sent it               │
│                  │  encryptedPayload (BLOB)    — E2E encrypted content     │
│                  │  timestamp        (BIGINT)  — server-assigned epoch ms  │
│                  │  messageType      (TEXT)    — text|image|video|audio|doc │
│                  │  deliveryStatus   (MAP<UUID, TEXT>)                      │
│                  │                   — per-recipient: sent|delivered|read   │
│                  │  mediaRef         (TEXT)    — nullable, mediaId pointer  │
│                  │  ttl              (INT)     — seconds until expiry       │
└──────────────────┴──────────────────────────────────────────────────────────┘
```

**CQL representation:**

```sql
CREATE TABLE messages (
    conversation_id  UUID,
    sequence_number  BIGINT,
    message_id       UUID,
    sender_id        UUID,
    encrypted_payload BLOB,
    timestamp        BIGINT,
    message_type     TEXT,
    delivery_status  MAP<UUID, TEXT>,
    media_ref        TEXT,
    PRIMARY KEY (conversation_id, sequence_number)
) WITH CLUSTERING ORDER BY (sequence_number ASC)
  AND default_time_to_live = 2592000   -- 30 days TTL
  AND compaction = {'class': 'TimeWindowCompactionStrategy',
                    'compaction_window_unit': 'DAYS',
                    'compaction_window_size': 1};
```

### 1.2 Partitioning Strategy

```
                    Cassandra Ring
                 ┌─────────────────┐
                 │                 │
           ┌─────┤    Node A       ├─────┐
           │     │ conv_001-conv_333│     │
           │     └─────────────────┘     │
     ┌─────┴─────┐               ┌──────┴────┐
     │  Node C    │               │  Node B    │
     │conv_667-999│               │conv_334-666│
     └────────────┘               └────────────┘

     Each partition = one conversation's messages
     Within partition: ordered by sequenceNumber
     Range query: "give me messages 500-550 in conv_XYZ"
       → single partition scan, extremely efficient
```

**Why partition by conversationId:**
- **Co-location**: All messages in a conversation live on the same partition. Fetching message history (the most common read) is a single-partition range scan, which is the fastest query Cassandra can do.
- **Even distribution**: ConversationIds are UUIDs, so they hash evenly across the ring. No hot partitions from popular users (each 1:1 conversation is a separate partition).
- **Bounded partition size**: WhatsApp deletes messages after delivery. Undelivered messages are retained for 30 days max. So partitions stay small (a few hundred undelivered messages at most, not years of history).

**Why NOT partition by userId:**
- A user participates in many conversations. Partitioning by userId would co-locate all conversations for one user, making the partition unbounded and creating hot spots for active users.
- Cross-conversation queries ("show me all chats") are served by the conversation metadata store, not the message store.

### 1.3 Clustering by Sequence Number

Messages within a partition are clustered (sorted) by `sequenceNumber` in ascending order.

**Why sequenceNumber, not timestamp:**
- Timestamps can collide (two messages at the same millisecond)
- Timestamps from clients are unreliable (clock skew)
- Sequence numbers are server-assigned, monotonically increasing per conversation, and gap-free
- Clients use sequence numbers to detect missing messages: "I have 1-500, server says latest is 503 → I'm missing 501, 502, 503"

**Efficient range queries:**

```
-- Fetch latest 50 messages in a conversation (chat history screen)
SELECT * FROM messages
WHERE conversation_id = ?
  AND sequence_number >= ?
ORDER BY sequence_number DESC
LIMIT 50;

-- Fetch all messages after a sequence number (offline sync)
SELECT * FROM messages
WHERE conversation_id = ?
  AND sequence_number > ?
ORDER BY sequence_number ASC;
```

Both queries hit a **single partition** and scan a contiguous range of the clustering key. This is O(result_size), not O(partition_size).

### 1.4 TTL and Retention

WhatsApp's server is a **transient relay**, not a permanent store. Messages are deleted after delivery.

```
Message Lifecycle on Server:

  Sender sends message
       │
       ▼
  Server stores in message store (TTL = 30 days)
       │
       ├── Recipient ONLINE ──► Push via WebSocket
       │                              │
       │                              ▼
       │                        Recipient ACKs
       │                              │
       │                              ▼
       │                     DELETE from message store
       │
       └── Recipient OFFLINE ──► Store in offline queue
                                      │
                                 (up to 30 days)
                                      │
                                      ▼
                               Recipient connects
                                      │
                                      ▼
                               Deliver + ACK
                                      │
                                      ▼
                              DELETE from both stores
```

- **Delivered messages**: Deleted immediately after delivery ACK. The server has no reason to retain a delivered, encrypted blob it cannot read.
- **Undelivered messages**: Retained for a maximum of 30 days (enforced by Cassandra TTL). After 30 days, the TTL expires and Cassandra tombstones and eventually removes the data.
- **Compaction**: `TimeWindowCompactionStrategy` is ideal here. Messages are naturally time-ordered and expire in bulk (old windows compact efficiently, tombstones are cleaned up per window).

**Why this matters for storage:**
The server's steady-state storage is only the set of **currently undelivered messages**, not the cumulative history of all messages ever sent. This is a fundamentally different storage profile than Slack or Telegram, where storage grows monotonically forever.

---

## 2. Conversation Metadata Store

Stores conversation-level information. Used primarily for the **chat list screen** (the first screen users see when opening the app).

### 2.1 Schema

```
┌────────────────────────────────────────────────────────────────────┐
│                    conversations table                              │
├──────────────────┬─────────────────────────────────────────────────┤
│  PARTITION KEY   │  conversation_id (UUID)                         │
├──────────────────┼─────────────────────────────────────────────────┤
│  COLUMNS         │  type                  (TEXT)  — "1:1" | "group"│
│                  │  participants          (SET<UUID>)              │
│                  │  group_name            (TEXT)  — null for 1:1   │
│                  │  group_admin           (UUID)  — null for 1:1   │
│                  │  created_at            (BIGINT)                 │
│                  │  last_message_timestamp(BIGINT)                 │
│                  │  last_sequence_number  (BIGINT)                 │
└──────────────────┴─────────────────────────────────────────────────┘
```

### 2.2 User-Conversations Lookup Table

To render the chat list, we need: "give me all conversations for user X, sorted by most recent activity."

```
┌──────────────────────────────────────────────────────────────────────┐
│                  user_conversations table                             │
├──────────────────┬───────────────────────────────────────────────────┤
│  PARTITION KEY   │  user_id (UUID)                                   │
│  CLUSTERING KEY  │  last_message_timestamp (BIGINT) DESC             │
├──────────────────┼───────────────────────────────────────────────────┤
│  COLUMNS         │  conversation_id  (UUID)                          │
│                  │  conversation_type (TEXT)                          │
│                  │  display_name     (TEXT) — contact name or group  │
│                  │  unread_count     (INT)                            │
│                  │  last_message_preview (TEXT) — "[encrypted]"       │
└──────────────────┴───────────────────────────────────────────────────┘
```

**Note on `last_message_preview`**: Because messages are E2E encrypted, the server cannot store a plaintext preview. This field stores an encrypted snippet or simply a placeholder like `"[New message]"`. The client decrypts locally and renders the actual preview. This is why WhatsApp push notifications on iOS use a Notification Service Extension to decrypt and display the actual message text.

### 2.3 Access Patterns

| Operation | Query | Notes |
|-----------|-------|-------|
| Chat list screen | `SELECT * FROM user_conversations WHERE user_id = ? ORDER BY last_message_timestamp DESC LIMIT 50` | Single partition, clustered by recency |
| Open a conversation | `SELECT * FROM conversations WHERE conversation_id = ?` | Point lookup |
| New message arrives | `UPDATE user_conversations SET last_message_timestamp = ?, unread_count = unread_count + 1 WHERE user_id = ? AND ...` | Update for each participant |
| Create group | `INSERT INTO conversations ...` + `INSERT INTO user_conversations` for each participant | Batch write |

---

## 3. User Profile Store

### 3.1 Schema

```
┌────────────────────────────────────────────────────────────────────┐
│                      user_profiles table                           │
├──────────────────┬─────────────────────────────────────────────────┤
│  PARTITION KEY   │  user_id (UUID)                                 │
├──────────────────┼─────────────────────────────────────────────────┤
│  COLUMNS         │  phone_number      (TEXT)                       │
│                  │  display_name      (TEXT)                       │
│                  │  about             (TEXT)                       │
│                  │  profile_photo_url (TEXT)                       │
│                  │  last_seen         (BIGINT)                     │
│                  │  created_at        (BIGINT)                     │
│                  │  device_ids        (SET<UUID>)                  │
└──────────────────┴─────────────────────────────────────────────────┘
```

### 3.2 Phone Number Index

Contact sync is one of the most critical operations: "which of my phone contacts are on WhatsApp?"

```
┌────────────────────────────────────────────────────────┐
│              phone_lookup table                         │
├──────────────────┬─────────────────────────────────────┤
│  PARTITION KEY   │  phone_hash (TEXT)  — SHA-256 hash  │
├──────────────────┼─────────────────────────────────────┤
│  COLUMNS         │  user_id (UUID)                     │
└──────────────────┴─────────────────────────────────────┘
```

**Why hash, not plaintext:**
- Privacy: server stores hashed phone numbers, not raw numbers
- Client uploads hashed contacts, server returns matching user IDs
- Prevents bulk scraping of the user directory
- Note: phone number hashing has known limitations (small keyspace, rainbow table attacks). WhatsApp likely uses additional mitigations such as rate limiting and abuse detection on the sync endpoint

### 3.3 Scale

- ~2B user profiles
- Profile reads are cacheable (profiles change rarely)
- Last seen updates are frequent but eventually consistent (written to a separate fast path, not the main profile table)

---

## 4. Pre-Key Store (E2E Encryption)

The pre-key store powers the Signal Protocol's X3DH key exchange. Without it, two users cannot establish an encrypted session.

### 4.1 Schema

```
┌────────────────────────────────────────────────────────────────────────┐
│                       pre_keys table                                   │
├──────────────────┬─────────────────────────────────────────────────────┤
│  PARTITION KEY   │  user_id (UUID)                                     │
├──────────────────┼─────────────────────────────────────────────────────┤
│  COLUMNS         │  identity_key       (BLOB)  — long-term public key │
│                  │  signed_pre_key     (BLOB)  — medium-term, rotated │
│                  │  signed_pre_key_sig (BLOB)  — signature by identity│
│                  │  signed_pre_key_id  (INT)                           │
└──────────────────┴─────────────────────────────────────────────────────┘

┌────────────────────────────────────────────────────────────────────────┐
│                    one_time_pre_keys table                              │
├──────────────────┬─────────────────────────────────────────────────────┤
│  PARTITION KEY   │  user_id (UUID)                                     │
│  CLUSTERING KEY  │  pre_key_id (INT)                                   │
├──────────────────┼─────────────────────────────────────────────────────┤
│  COLUMNS         │  public_key (BLOB)                                  │
└──────────────────┴─────────────────────────────────────────────────────┘
```

### 4.2 One-Time Pre-Key Lifecycle

```
   Client registers / replenishes
            │
            ▼
   Upload batch of 100 one-time pre-keys
            │
            ▼
   Server stores in one_time_pre_keys table
            │
            │   Alice wants to message Bob (first time)
            │          │
            │          ▼
            │   Server fetches Bob's pre-key bundle:
            │     - identity_key
            │     - signed_pre_key
            │     - ONE one_time_pre_key (consumed + DELETED)
            │          │
            │          ▼
            │   Alice performs X3DH, derives shared secret
            │          │
            │          ▼
            │   Session established, Double Ratchet begins
            │
   When one-time pre-keys run low (< 10 remaining):
            │
            ▼
   Server notifies client to upload more
```

**Critical invariant**: Each one-time pre-key is used **exactly once**. After it is fetched for a key exchange, it is deleted from the server. This ensures each session has unique keying material, providing forward secrecy at the session level.

**What if one-time pre-keys are exhausted?** The X3DH protocol falls back to using only the identity key + signed pre-key (no one-time pre-key). The session still works but with slightly weaker forward secrecy guarantees.

---

## 5. Media Store

Media (images, videos, audio, documents) is stored **separately** from messages in an object storage system.

### 5.1 Architecture

```
┌──────────┐    encrypted    ┌──────────────┐    store    ┌──────────────────┐
│  Client   │───────────────►│ Media Server  │───────────►│  Object Storage   │
│           │   media blob   │  (stateless)  │            │  (S3 / Blob)      │
└──────────┘                 └──────────────┘            └──────────────────┘
     │                              │
     │  message with               │  returns mediaId
     │  {mediaId, AES key}         │
     ▼                              │
┌──────────────┐                    │
│ Message Store │◄──────────────────┘
│ (Cassandra)   │
└──────────────┘

Recipient flow:
1. Receive message with {mediaId, AES-256 key}
2. Download encrypted blob from object storage via CDN
3. Decrypt locally with the AES key from the message
```

### 5.2 Why Separate Media from Messages

| Concern | Inline (media in message store) | Separated (media in object storage) |
|---------|---------------------------------|-------------------------------------|
| Message delivery speed | Slow — large blobs clog the delivery pipeline | Fast — message is small (~100 bytes ref), media downloads lazily |
| Storage cost | Expensive (Cassandra storage for multi-MB blobs) | Cheap (object storage at ~$0.023/GB) |
| CDN cacheability | Not possible (Cassandra is not CDN-friendly) | Yes — encrypted blobs served via CDN edge |
| Offline queue size | Huge — offline queue holds media blobs | Small — offline queue holds only references |
| Deduplication | Hard | Easy — same mediaId for forwarded messages |

### 5.3 Media Metadata in Message Store

The message's `media_ref` field contains a JSON reference:

```json
{
  "media_id": "a1b2c3d4-...",
  "mime_type": "image/jpeg",
  "size_bytes": 245760,
  "encryption_key": "base64-encoded-AES-256-key",
  "encryption_iv": "base64-encoded-IV",
  "sha256_hash": "base64-encoded-hash",
  "thumbnail": "base64-encoded-blurred-thumbnail"
}
```

The encryption key is part of the E2E encrypted message payload, so the server never sees it in plaintext. The server sees only the `media_id` in metadata.

### 5.4 Retention

- Media blobs have a TTL (e.g., 30 days) similar to messages
- Once downloaded by all recipients, the blob can be garbage-collected
- Forwarded media is reference-counted: delete only when refcount reaches zero

---

## 6. Offline Message Queue

When a recipient is offline, messages are queued for delivery upon reconnection.

### 6.1 Schema

```
┌────────────────────────────────────────────────────────────────────┐
│                    offline_queue table                              │
├──────────────────┬─────────────────────────────────────────────────┤
│  PARTITION KEY   │  recipient_id (UUID)                            │
│  CLUSTERING KEY  │  sequence_number (BIGINT) ASC                   │
├──────────────────┼─────────────────────────────────────────────────┤
│  COLUMNS         │  conversation_id   (UUID)                       │
│                  │  message_id        (UUID)                       │
│                  │  sender_id         (UUID)                       │
│                  │  encrypted_payload (BLOB)                       │
│                  │  timestamp         (BIGINT)                     │
│                  │  message_type      (TEXT)                       │
└──────────────────┴─────────────────────────────────────────────────┘
```

### 6.2 Drain Protocol

```
   User comes online
        │
        ▼
   Client sends: "My last sequence = 4,502"
        │
        ▼
   Server queries offline_queue:
     SELECT * FROM offline_queue
     WHERE recipient_id = ?
       AND sequence_number > 4502
     ORDER BY sequence_number ASC;
        │
        ▼
   Server pushes messages in batches (e.g., 100 at a time)
        │
        ▼
   Client ACKs each batch
        │
        ▼
   Server DELETEs acknowledged messages from offline_queue
        │
        ▼
   All caught up → switch to real-time WebSocket delivery
```

### 6.3 Design Considerations

- **Partitioned by recipient**: All queued messages for a user are co-located. Drain is a single partition range scan.
- **Global sequence number per recipient**: Not per-conversation. This allows the client to say "give me everything after X" in one query, across all conversations.
- **Pagination for long-offline users**: If a user has been offline for days, they may have thousands of queued messages. Deliver in pages to avoid overwhelming the client (mobile device, limited memory).
- **TTL = 30 days**: Same as the message store. If a user doesn't connect for 30 days, their queued messages expire.
- **Duplicate with message store**: Yes, messages exist in both the message store (keyed by conversation) and the offline queue (keyed by recipient). This is intentional — the offline queue is optimized for the "drain on connect" access pattern, while the message store is optimized for "fetch history by conversation." The offline queue entry is deleted after ACK.

---

## 7. Connection Registry

Tracks which gateway server holds each user's active WebSocket connection. This is the routing table for real-time message delivery.

### 7.1 Schema (Redis)

```
Key:    conn:{userId}
Value:  {
          "gateway_server_id": "gw-us-east-042",
          "connection_id": "ws-conn-a7b3c9",
          "connected_at": 1708387200000,
          "device_id": "device-abc-123"
        }
TTL:    90 seconds (refreshed by heartbeat)
```

For multi-device support, use a Redis Hash:

```
Key:    conn:{userId}
Field:  {deviceId}
Value:  {"gateway_server_id": "...", "connection_id": "...", ...}

-- Routing a message to user X:
HGETALL conn:{userId}
-- Returns all active device connections → fan out to each
```

### 7.2 Why Redis

| Requirement | Why Redis fits |
|-------------|---------------|
| Sub-millisecond lookups | Every message delivery requires a lookup. Must be fast. |
| TTL-based expiry | Connections die silently (network loss). TTL auto-cleans stale entries. |
| High write throughput | Every heartbeat (every 30-60s per connection) refreshes the TTL. At 100M connections: ~1.5-3M writes/sec. |
| Small dataset | 100M connections x ~200 bytes = ~20 GB. Fits in memory. |
| Pub/Sub for invalidation | Gateway server crash → bulk invalidation of connections on that server. |

### 7.3 Failure Handling

```
   Gateway server crashes
          │
          ▼
   Health checker detects failure (missed heartbeats)
          │
          ▼
   Bulk-delete all connection entries for that gateway:
     SCAN for keys where gateway_server_id = "gw-042"
     DEL each key
          │
          ▼
   (Alternatively: entries expire naturally via TTL within 90s)
          │
          ▼
   Clients detect broken WebSocket → reconnect to a different gateway
          │
          ▼
   New connection registered in Redis
```

---

## 8. Scale Numbers & Storage Calculations

### 8.1 Raw Numbers

| Metric | Value | Source |
|--------|-------|--------|
| Registered users | 2B+ | [Meta public reporting] |
| Daily messages | ~100B | [Meta public reporting] |
| Daily media items | ~6.5B | [UNVERIFIED — commonly cited in system design literature] |
| Max group size | 1,024 members | [WhatsApp official FAQ] |
| Concurrent connections (peak) | 50-100M | [INFERRED — based on 2B users, ~5% concurrency ratio] |
| Messages per second (avg) | ~1.15M | 100B / 86,400 |
| Messages per second (peak) | ~3.5-5.7M | 3-5x average |
| Average text message size | ~1 KB (encrypted) | [INFERRED] |
| Average media size (compressed) | ~200 KB | [INFERRED — images ~100-300KB, video ~2-10MB, weighted avg] |

### 8.2 Back-of-Envelope Storage Calculations

**Daily message volume (text only):**

```
100B messages/day x 1 KB/message = 100 TB/day raw write throughput
```

**But steady-state storage is NOT 100 TB/day accumulating:**

```
Scenario 1: 95% of messages delivered within 1 minute
  → 95B messages delivered + deleted almost immediately
  → Only 5B messages in flight at any moment
  → In-flight storage: 5B x 1 KB = 5 TB

Scenario 2: 5% of users offline for hours (worst case)
  → 100M users offline x 50 messages each = 5B queued messages
  → Queued storage: 5B x 1 KB = 5 TB

Steady-state message storage: ~5-10 TB
(NOT 100 TB/day growing forever)
```

**This is the critical insight for the interview**: WhatsApp's storage is **bounded**, not monotonically growing, because the server deletes messages after delivery. Contrast with Slack, where storage grows by ~100 TB/day and never shrinks.

**Daily media volume:**

```
6.5B media items/day x 200 KB avg = 1.3 PB/day
```

Media is the dominant storage cost. Even with TTL-based deletion (30 days), steady state could be:

```
Worst case: 1.3 PB/day x 30 days = 39 PB
Realistic (most media downloaded within hours): ~5-10 PB steady state
```

**Connection registry:**

```
100M concurrent connections x 200 bytes/entry = 20 GB
→ Fits comfortably in a Redis cluster
```

**User profiles:**

```
2B users x 500 bytes/profile = 1 TB
→ Trivially small, easily cached
```

**Pre-key store:**

```
2B users x 1 identity key (32 bytes)
         x 1 signed pre-key (32 bytes)
         x 100 one-time pre-keys (32 bytes each)
= 2B x ~3.3 KB = ~6.6 TB
→ Manageable, mostly static except one-time key consumption
```

### 8.3 Summary Table

| Store | Steady-State Size | Growth Pattern | Technology |
|-------|-------------------|----------------|------------|
| Message store | 5-10 TB | Bounded (delete after delivery) | Cassandra |
| Offline queue | 5-10 TB | Bounded (drain on connect, TTL) | Cassandra |
| Media blobs | 5-10 PB | Bounded (TTL, delete after download) | S3 / Object Storage |
| Conversation metadata | ~500 GB | Slow growth (new conversations) | Cassandra |
| User profiles | ~1 TB | Slow growth (new users) | Cassandra + cache |
| Pre-key store | ~7 TB | Replenished (one-time keys consumed + refilled) | Cassandra |
| Connection registry | ~20 GB | Bounded (only active connections) | Redis |

---

## 9. Why Cassandra — Database Comparison

### 9.1 Requirements for the Message Store

1. **Extreme write throughput**: 1.15M writes/sec average, 5M+ at peak
2. **Multi-datacenter replication**: Active-active across geographies
3. **Partition tolerance**: Must survive node failures without downtime
4. **Time-series-like data model**: Messages are append-mostly, time-ordered, and expire
5. **Efficient range queries**: "Give me messages 500-550 in conversation X"
6. **TTL support**: Messages auto-expire after 30 days
7. **Deletion performance**: Messages are deleted after delivery ACK — deletion must be cheap

### 9.2 Comparison Table

| Dimension | Cassandra | MySQL (InnoDB) | MongoDB | HBase |
|-----------|-----------|----------------|---------|-------|
| **Write throughput** | Excellent. LSM-tree based, writes go to memtable + commit log. Append-only. Designed for write-heavy workloads. | Moderate. B-tree based, random I/O for writes. Write-ahead log helps, but clustered index updates are expensive at scale. | Good. WiredTiger uses LSM-trees optionally. But single-document transactions add overhead. | Excellent. Also LSM-tree based on HDFS. Comparable to Cassandra for raw writes. |
| **Multi-DC replication** | Native. Built-in `NetworkTopologyStrategy`. Eventual consistency across DCs. Zero-downtime DC failover. | Painful. MySQL replication is single-primary. Multi-primary (Galera, Group Replication) is fragile at scale. Vitess helps with sharding but not multi-DC. | Limited. MongoDB replica sets are single-primary. Multi-DC requires careful config. Cross-DC writes route to primary. | Possible but complex. HBase relies on HDFS replication. Cross-DC via HBase replication is operationally heavy. |
| **Partition tolerance** | Excellent. AP system (in CAP terms). Tunable consistency. Survives node/rack/DC failures gracefully. | Poor. CP system. Primary failure requires failover, which causes downtime. Sharding with Vitess helps but adds complexity. | Moderate. Replica set failover is automatic but causes brief unavailability during election. | Good. Relies on HDFS for fault tolerance (3x replication). RegionServer failures handled by HMaster reassignment. |
| **Data model fit** | Excellent. Wide-column model. (conversationId, seqNo) partition+clustering key maps perfectly to chat messages. | Poor fit. Relational model requires JOIN for conversation-message relationship. Sharding by conversationId is possible but not native. | Moderate. Document model works for messages, but lacks native clustering key ordering within a partition. | Good. Similar wide-column model to Cassandra. Row key = conversationId, column qualifier = seqNo. |
| **Deletion performance** | Moderate. Deletes create tombstones, which accumulate until compaction. `TimeWindowCompactionStrategy` mitigates this for TTL-based workloads. | Good. B-tree deletes are in-place. But at 1M+ deletes/sec, index maintenance is expensive. | Good. Document deletes are straightforward. But at scale, requires careful index management. | Moderate. Similar tombstone issue as Cassandra. Major compactions required for cleanup. |
| **TTL support** | Native. Per-row and per-column TTL. Cassandra handles expiration automatically during compaction. Zero application logic needed. | None native. Requires application-level cron jobs or event schedulers to delete expired rows. | Native (since 5.0). `expireAfterSeconds` index. But less battle-tested at chat scale. | Native. Column-level TTL via `setTimeToLive()`. Similar to Cassandra. |
| **Operational maturity at chat scale** | Proven. WhatsApp (before migration), Instagram DMs, Discord (before ScyllaDB migration) all used Cassandra. | Proven at Slack scale (with Vitess). Not proven for WhatsApp-scale write throughput. | Used by some chat systems but not at WhatsApp scale. | Used at Facebook (Messages was on HBase). Operational complexity is higher (HDFS dependency, ZooKeeper). |

### 9.3 Verdict

**Cassandra wins** for the message store because:

1. The data model is a natural fit: `(conversationId, sequenceNumber)` maps directly to partition key + clustering key
2. Write throughput at 1M+/sec is Cassandra's sweet spot
3. Native multi-DC replication with tunable consistency supports active-active deployments
4. Native TTL eliminates the need for application-level message expiration
5. The AP trade-off (availability over consistency) is correct for chat — a briefly stale read is better than an unavailable chat

**When NOT Cassandra:**
- If you need full-text search over messages → Elasticsearch (but WhatsApp doesn't need this — server can't read encrypted messages)
- If you need strong ACID transactions → PostgreSQL/MySQL (but chat doesn't need cross-message transactions)
- If you need complex queries/JOINs → relational DB (but chat access patterns are simple partition-key lookups)

---

## 10. WhatsApp's Original Stack

### 10.1 Erlang + Mnesia

WhatsApp was famously built on an **Erlang/OTP** stack, which was unusual for a consumer internet company but perfectly suited for a messaging system.

| Component | Technology | Why |
|-----------|-----------|-----|
| **Language/Runtime** | Erlang on BEAM VM | Lightweight processes (~2 KB each), preemptive scheduling, millions of concurrent processes per node. Each WebSocket connection = one Erlang process. |
| **Database** | Mnesia (Erlang's built-in DB) | In-memory with disk persistence. Distributed across Erlang nodes. Schema-less. Tight integration with Erlang processes — no serialization overhead. |
| **XMPP Server** | Ejabberd (forked + heavily modified) | Open-source XMPP server written in Erlang. WhatsApp forked it and stripped it down to a custom binary protocol, removing XML overhead. |
| **Web Server** | YAWS (Yet Another Web Server) | Erlang-based web server. Used for API endpoints, not for WebSocket (custom protocol handler for that). |
| **OS** | FreeBSD | WhatsApp preferred FreeBSD over Linux for its network stack performance and operational simplicity. [VERIFIED — multiple sources confirm FreeBSD usage] |
| **Hardware** | Commodity servers | Famously lean: ~50 engineers serving 900M+ users at the time of the Facebook acquisition ($19B, February 2014). |

### 10.2 Why Erlang Was Perfect for Chat

```
Traditional approach (Java/Go):
  1 WebSocket connection = 1 OS thread/goroutine
  Thread: ~1 MB stack (Java) / ~8 KB (Go goroutine)
  1M connections = 1 TB RAM (Java) / 8 GB (Go)

Erlang approach:
  1 WebSocket connection = 1 Erlang process
  Process: ~2 KB initial heap
  1M connections = 2 GB RAM
  2M connections per server = ~4 GB RAM
  (WhatsApp reportedly achieved ~2M connections per server)
```

**Key Erlang features exploited by WhatsApp:**

1. **Lightweight processes**: Millions of processes per node. Each connection is isolated — a crash in one process doesn't affect others.
2. **Let-it-crash philosophy**: Instead of defensive error handling, Erlang processes crash and are restarted by supervisor trees. Perfect for flaky mobile connections — if a connection handler crashes, the supervisor restarts it and the client reconnects.
3. **Hot code upgrades**: Erlang supports loading new code into a running system without stopping it. WhatsApp could deploy new code without disconnecting users.
4. **Distributed by default**: Erlang nodes form clusters natively. Mnesia replicates data across nodes. No external coordination service needed.
5. **Message passing**: Erlang processes communicate via message passing (no shared memory, no locks). This maps directly to chat: receiving a message from user A and forwarding it to user B's process.

### 10.3 Mnesia Specifics

Mnesia served as WhatsApp's primary database in the early days:

- **In-memory with replication**: Data replicated across Erlang nodes. Reads are local (fast). Writes are replicated.
- **Schema**: ETS/DETS tables. No SQL, no complex queries. Simple key-value lookups tuned for the BEAM runtime.
- **Limitations**: Mnesia doesn't scale well beyond a few nodes (replication overhead grows). As WhatsApp grew, they likely augmented or replaced Mnesia with other storage systems.

### 10.4 Evolution Post-Meta Acquisition (2014+)

After Meta acquired WhatsApp in 2014, the stack evolved:

- **Mnesia limitations**: Mnesia's full-mesh replication doesn't scale well past ~10-20 nodes. At WhatsApp's scale, they needed a more scalable storage layer. [INFERRED — Mnesia scaling limitations are well-documented; WhatsApp's specific migration path is not publicly detailed]
- **Multi-device support (2021)**: The introduction of linked devices (up to 4 companion devices without requiring the phone to be online) required significant architectural changes. Each device has its own encryption keys and connection. [VERIFIED — WhatsApp multi-device beta launched 2021]
- **Infrastructure integration with Meta**: Likely adopted some Meta infrastructure (network, data centers, monitoring) while keeping the core Erlang messaging engine. [INFERRED — not officially documented]
- **The Erlang core persists**: Despite the acquisition, WhatsApp continued to run Erlang for its connection-handling layer. The BEAM VM's properties (lightweight processes, fault tolerance) are too valuable to replace for the core messaging use case.

---

## 11. Contrast with Slack

Slack's storage model is **fundamentally different** from WhatsApp's because of different product requirements: Slack is an enterprise tool that **must** store all messages permanently, make them searchable, and support compliance/audit requirements.

### 11.1 Architecture Comparison

| Dimension | WhatsApp | Slack |
|-----------|----------|-------|
| **Storage engine** | Cassandra (wide-column, AP) | MySQL + Vitess (relational, CP) |
| **Message retention** | Transient — deleted after delivery ACK. 30-day max for undelivered. | Permanent — ALL messages stored forever. Searchable. |
| **Source of truth** | **Client** is the source of truth. Server is a relay. Chat history is on your phone (backed up to iCloud/Google Drive). | **Server** is the source of truth. You can log in from any device and see full history. |
| **Storage growth** | Bounded. Steady-state = undelivered messages only (~5-10 TB). | Monotonically growing. Every message ever sent is retained. Terabytes per day accumulating. |
| **Full-text search** | Impossible on server (E2E encrypted — server can't read content). Search happens on-device. | Server-side search via Elasticsearch. Users expect to search all channel history. |
| **Compliance** | No server-side compliance tools (messages are encrypted and ephemeral). | Enterprise compliance: data retention policies, legal hold, eDiscovery, data export. |
| **Sharding strategy** | Partition by conversationId (natural for chat, no cross-partition queries needed). | Shard by workspace (Vitess). Each workspace's messages are co-located. Cross-workspace queries are rare. |
| **Consistency model** | Eventual consistency (AP). Brief staleness is acceptable. | Strong consistency (ACID transactions per workspace). Users expect "I posted it, everyone sees it immediately." |
| **Schema** | Wide-column: `(conversationId, seqNo) → blob` | Relational: `messages` table with foreign keys to `channels`, `users`, `workspaces` tables. Supports rich queries and JOINs. |

### 11.2 Why Slack Chose MySQL + Vitess

Slack's access patterns are fundamentally different:

1. **Search**: "Find all messages mentioning 'deployment' in #engineering from last month." This requires a relational model with indexes and full-text search. Cassandra cannot do this efficiently.
2. **Threads**: Slack threads create parent-child relationships between messages. Relational model with foreign keys is natural.
3. **Reactions, edits, pins**: Rich metadata on messages. Relational model handles this cleanly.
4. **Compliance**: Enterprise customers require SQL-queryable audit logs. "Show me all messages from user X in workspace Y between dates A and B."
5. **Workspace isolation**: Each Slack workspace is a natural shard boundary. Vitess shards MySQL by workspace ID.

### 11.3 The Storage Growth Problem

```
WhatsApp storage over time:          Slack storage over time:

Storage                               Storage
  │                                     │                    /
  │    ┌──────────────────              │                  /
  │   /                                 │                /
  │  /                                  │              /
  │ /   (bounded — only undelivered)    │            /
  │/                                    │          /
  └──────────── Time                    │        /   (monotonically growing)
                                        │      /
                                        │    /
                                        │  /
                                        │/
                                        └──────────── Time
```

Slack's storage grows without bound. Every message, every edit, every reaction is retained. This is why Slack invested heavily in Vitess (MySQL sharding) and continues to face scaling challenges as workspaces grow over years.

WhatsApp's storage is self-cleaning. Messages flow through the server and are deleted. Steady-state storage is determined by the number of currently-offline users and their message backlog, not by the cumulative history.

---

## 12. Contrast with Discord

Discord's storage evolution is one of the most cited case studies in system design interviews. Discord stores messages **permanently** (like Slack) but at a much larger scale (trillions of messages across millions of servers).

### 12.1 Discord's Cassandra Era (2015-2022)

Discord initially chose Cassandra for message storage, with a schema similar to what we described:

```
Partition key: channel_id
Clustering key: message_id (Snowflake ID — encodes timestamp)
```

**Problems Discord encountered with Cassandra at scale:**

1. **Tombstone accumulation**: Discord's channels have widely varying activity levels. Some channels have millions of messages; others have a few. When messages are deleted (or channels are pruned), Cassandra creates tombstones. Tombstones accumulate and degrade read performance — reading from a channel with heavy deletions requires scanning through tombstones.

2. **Hot partitions**: Popular Discord servers (millions of members) have channels with enormous partitions. A single viral message in #general creates a read hot spot.

3. **Compaction pressure**: Cassandra's compaction runs in the background, merging SSTables and cleaning tombstones. At Discord's scale, compaction became a source of latency spikes and unpredictable performance.

4. **GC pauses (JVM)**: Cassandra runs on the JVM. At Discord's data volumes, garbage collection pauses caused tail latency spikes that degraded user experience.

### 12.2 Migration to ScyllaDB (2022-2023)

Discord migrated from Cassandra to **ScyllaDB**, a C++ rewrite of Cassandra that is API-compatible but eliminates the JVM.

| Metric | Cassandra (before) | ScyllaDB (after) |
|--------|-------------------|------------------|
| **Node count** | 177 nodes | 72 nodes |
| **Messages stored** | Trillions | Trillions (same data) |
| **P99 read latency** | 40-125 ms | 15 ms |
| **P99 write latency** | 5-70 ms | 5 ms |
| **GC pauses** | Frequent (JVM) | None (C++, no GC) |

[Source: Discord Engineering Blog, "How Discord Stores Trillions of Messages," 2023. Numbers should be verified against the original blog post.]

### 12.3 Why Discord's Problem Doesn't Apply to WhatsApp

| Concern | Discord | WhatsApp |
|---------|---------|----------|
| **Message retention** | Forever. Trillions of messages accumulating. | Transient. Deleted after delivery. |
| **Partition sizes** | Unbounded. A channel with years of history = huge partition. | Bounded. Only undelivered messages per conversation. |
| **Tombstone pressure** | High. Users delete messages, channels get pruned. | Low. Messages are TTL-expired, which compacts cleanly with TWCS. |
| **Read patterns** | "Scroll back through years of history" = deep partition scans. | "Fetch last 50 messages" or "fetch undelivered" = shallow scans. |
| **Data volume** | Trillions of messages, petabytes. | Only undelivered messages at any point, terabytes. |

WhatsApp could stay on Cassandra comfortably because its **transient relay model** means:
- Partitions stay small (only undelivered messages)
- Tombstones are manageable (TTL + TWCS handles expiration cleanly)
- No deep historical reads (no "scroll back to 2019" feature on the server side)

Discord needed ScyllaDB because its **permanent storage model** meant Cassandra's weaknesses (JVM GC, compaction overhead, tombstone accumulation) were amplified by the data volume.

### 12.4 Comparison Summary

```
                 WhatsApp              Slack                Discord
                 ────────              ─────                ───────
Storage Model    Transient relay       Permanent store      Permanent store
DB Engine        Cassandra             MySQL + Vitess       ScyllaDB (was Cassandra)
Message TTL      30 days max           Forever              Forever
Searchable       No (E2E encrypted)    Yes (Elasticsearch)  Yes (Elasticsearch)
Source of Truth   Client device         Server               Server
Scale Challenge  Connection mgmt       Storage growth        Storage + read perf
Deletion Model   TTL auto-expire       Soft delete/retain   Hard delete + tombstones
Encryption       E2E (Signal Protocol) At-rest only         At-rest only
```

---

## Appendix A: Complete Data Store Map

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                        WhatsApp Data Store Architecture                      │
├─────────────────────────────────────────────────────────────────────────────┤
│                                                                             │
│  ┌─────────────────────┐    ┌─────────────────────┐                        │
│  │   Connection         │    │   User Profile       │                        │
│  │   Registry           │    │   Store              │                        │
│  │   (Redis)            │    │   (Cassandra + Cache) │                        │
│  │   ~20 GB             │    │   ~1 TB              │                        │
│  │   TTL: 90s           │    │   TTL: none          │                        │
│  └─────────────────────┘    └─────────────────────┘                        │
│                                                                             │
│  ┌─────────────────────┐    ┌─────────────────────┐                        │
│  │   Message Store      │    │   Conversation       │                        │
│  │   (Cassandra)        │    │   Metadata           │                        │
│  │   ~5-10 TB           │    │   (Cassandra)        │                        │
│  │   TTL: 30 days       │    │   ~500 GB            │                        │
│  │   PK: conversationId │    │   TTL: none          │                        │
│  │   CK: sequenceNumber │    │                      │                        │
│  └─────────────────────┘    └─────────────────────┘                        │
│                                                                             │
│  ┌─────────────────────┐    ┌─────────────────────┐                        │
│  │   Offline Queue      │    │   Pre-Key Store      │                        │
│  │   (Cassandra)        │    │   (Cassandra)        │                        │
│  │   ~5-10 TB           │    │   ~7 TB              │                        │
│  │   TTL: 30 days       │    │   TTL: none          │                        │
│  │   PK: recipientId    │    │   PK: userId         │                        │
│  │   CK: sequenceNumber │    │   One-time keys      │                        │
│  └─────────────────────┘    │   consumed on use     │                        │
│                              └─────────────────────┘                        │
│                                                                             │
│  ┌─────────────────────────────────────────────────┐                        │
│  │   Media Store (S3 / Object Storage)              │                        │
│  │   ~5-10 PB steady state                          │                        │
│  │   Encrypted blobs, CDN-served                    │                        │
│  │   TTL: 30 days (or until downloaded)             │                        │
│  │   Referenced by mediaId in message metadata      │                        │
│  └─────────────────────────────────────────────────┘                        │
│                                                                             │
└─────────────────────────────────────────────────────────────────────────────┘
```

---

## Appendix B: Interview Tips — What an L6 Should Emphasize

| What to say | Why it matters |
|-------------|---------------|
| "The server is a transient relay, not a permanent store — this means steady-state storage is bounded." | Shows you understand the fundamental difference between WhatsApp and Slack/Discord. This is the #1 insight. |
| "Partition by conversationId with sequenceNumber as clustering key gives us efficient range queries for message history." | Shows you understand Cassandra data modeling and can map access patterns to schema. |
| "TTL + TimeWindowCompactionStrategy handles message expiration without tombstone accumulation." | Shows operational awareness — you know Cassandra's tombstone problem and how to mitigate it. |
| "Media is stored separately in object storage. The message only contains an encrypted reference." | Shows you understand the separation of concerns and why inline media would be a disaster. |
| "The offline queue is intentionally duplicated from the message store because it optimizes for a different access pattern." | Shows you're comfortable with denormalization and can justify it with concrete access patterns. |
| "Connection registry in Redis with TTL because it's small (20 GB), ephemeral, and needs sub-millisecond lookups." | Shows you pick the right tool for the job, not just "put everything in Cassandra." |
| "Discord migrated from Cassandra to ScyllaDB because their permanent storage model amplified Cassandra's weaknesses. WhatsApp wouldn't have the same problem." | Shows you can reason about when a technology's limitations matter and when they don't. |

---

*Last updated: 2026-02-20*
