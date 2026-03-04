# Push Notifications & Offline Sync — Deep Dive

> **Context:** This document is a deep-dive companion to the [main interview simulation](01-interview-simulation.md). It covers how a WhatsApp-like chat application handles push notifications for offline users and synchronizes messages when users reconnect.
>
> **Core tension:** E2E encryption means the server cannot include message content in push notifications. The push payload is metadata-only. Actual content decryption must happen on the client device. This constraint shapes the entire notification architecture.

---

## Table of Contents

1. [Push Notification Flow](#1-push-notification-flow)
2. [Offline Sync Protocol](#2-offline-sync-protocol)
3. [Offline Message Retention](#3-offline-message-retention)
4. [Badge Count / Unread Count](#4-badge-count--unread-count)
5. [Multi-Device Offline Sync](#5-multi-device-offline-sync)
6. [Push Notification Optimization](#6-push-notification-optimization)
7. [Silent Push / Background Fetch](#7-silent-push--background-fetch)
8. [Notification Content Privacy](#8-notification-content-privacy)
9. [Contrast with Slack](#9-contrast-with-slack)
10. [Contrast with Telegram](#10-contrast-with-telegram)

---

## 1. Push Notification Flow

When a recipient has no active WebSocket connection (offline), the server must notify them via platform push services. The fundamental constraint: **the server cannot include message content** because messages are E2E encrypted and the server never holds plaintext.

### Architecture

```
                         SENDER                          RECIPIENT (OFFLINE)
                           |                                    |
                           |  1. Send encrypted msg             |
                           |  via WebSocket                     |
                           v                                    |
                   +---------------+                            |
                   |   Gateway     |                            |
                   |   Server      |                            |
                   +-------+-------+                            |
                           |                                    |
                           | 2. Recipient offline?              |
                           |    Check connection registry       |
                           v                                    |
                   +---------------+                            |
                   |   Message     |  3. Store in offline queue |
                   |   Router      |---------------------------+|
                   +-------+-------+                           ||
                           |                                   ||
                           | 4. Trigger push                   ||
                           v                                   ||
                   +---------------+                           ||
                   |   Push        |                           ||
                   |   Service     |                           ||
                   +---+-------+---+                           ||
                       |       |                               ||
          5a. APNs     |       | 5b. FCM                      ||
                       v       v                               ||
                +--------+ +--------+                          ||
                | Apple  | | Google |                          ||
                | APNs   | | FCM   |                          ||
                +---+----+ +---+----+                          ||
                    |          |                                ||
                    +----+-----+                                ||
                         |  6. Push notification                ||
                         |  (metadata only:                     ||
                         |   "New message from Alice")          ||
                         v                                      ||
                  +--------------+                              ||
                  |  Recipient   |  7. User taps notification   ||
                  |  Device      |                              ||
                  |              |  8. App opens, establishes   ||
                  |              |     WebSocket connection      ||
                  |              |                              ||
                  |              |  9. Offline sync begins <----+|
                  |              |     (see Section 2)           |
                  +--------------+                              +
```

### Push Payload Structure

The push payload is intentionally minimal. The server knows WHO sent the message but NOT what it says.

**APNs payload (iOS):**
```json
{
  "aps": {
    "alert": {
      "title": "Alice",
      "body": "New message"
    },
    "badge": 5,
    "mutable-content": 1,
    "sound": "default",
    "category": "MESSAGE"
  },
  "metadata": {
    "senderId": "user_12345",
    "conversationId": "conv_abc",
    "messageId": "msg_xyz",
    "messageType": "text",
    "timestamp": 1708444800000
  }
}
```

**FCM payload (Android):**
```json
{
  "message": {
    "token": "<device_fcm_token>",
    "data": {
      "senderId": "user_12345",
      "senderName": "Alice",
      "conversationId": "conv_abc",
      "messageId": "msg_xyz",
      "messageType": "text",
      "timestamp": "1708444800000"
    },
    "android": {
      "priority": "high",
      "notification": {
        "channel_id": "messages"
      }
    }
  }
}
```

### Key Design Decisions

| Decision | Rationale |
|----------|-----------|
| **Metadata-only payload** | E2E encryption means the server has no plaintext to include. Even if it did, push services (Apple, Google) would see the content — violating the E2E guarantee. |
| **`mutable-content: 1` on iOS** | Enables the Notification Service Extension to intercept and modify the notification before display. This is how WhatsApp decrypts notification content locally (see [Section 8](#8-notification-content-privacy)). |
| **FCM `data` message (not `notification`)** | Data messages are always delivered to the app (even in background on Android), giving the app control over display. `notification` messages are handled by the system when the app is in the background, which doesn't allow decryption. |
| **`messageType` in payload** | Allows the notification to say "Alice sent a photo" or "Alice sent a voice message" without revealing content. The server knows the type (text/image/video) from the message metadata, even though it can't read the content. |
| **High priority for FCM** | Android's Doze mode and App Standby restrict background processing. High-priority FCM messages bypass these restrictions for immediate delivery. [INFERRED -- exact priority handling may vary by Android version and OEM] |

### Push Service Registration Flow

```
Client Device                     Chat Server                Push Service (APNs/FCM)
     |                                |                              |
     |  1. Register with push service |                              |
     |--------------------------------------------------------------->
     |                                |                              |
     |  2. Receive device push token  |                              |
     |<---------------------------------------------------------------
     |                                |                              |
     |  3. Send push token to server  |                              |
     |------------------------------->|                              |
     |                                |                              |
     |  4. Server stores:             |                              |
     |     userId -> [pushToken,      |                              |
     |                platform,       |                              |
     |                deviceId]       |                              |
     |                                |                              |
```

The server maintains a mapping of `userId -> [{pushToken, platform (ios/android), deviceId}]`. With multi-device support, a single user may have multiple push tokens (one per device).

---

## 2. Offline Sync Protocol

When a user reconnects after being offline, the client must receive all messages that arrived during the offline period. This is the most latency-sensitive moment in the user experience -- the user opens the app and expects to see all their messages immediately.

### Sync Flow

```
Client                              Server                      Offline Queue
  |                                    |                              |
  |  1. WebSocket handshake            |                              |
  |  + auth token                      |                              |
  |  + deviceId                        |                              |
  |----------------------------------->|                              |
  |                                    |                              |
  |  2. AUTH_SUCCESS                   |                              |
  |<-----------------------------------|                              |
  |                                    |                              |
  |  3. SYNC_REQUEST                   |                              |
  |  {                                 |                              |
  |    "lastSequences": {              |                              |
  |      "conv_abc": 48291,           |                              |
  |      "conv_def": 12044,           |                              |
  |      "conv_ghi": 7833             |                              |
  |    },                              |                              |
  |    "maxBatchSize": 100             |                              |
  |  }                                 |                              |
  |----------------------------------->|                              |
  |                                    |  4. Query offline queue      |
  |                                    |     for userId + seqNum >    |
  |                                    |     lastKnown per conv       |
  |                                    |------------------------------>
  |                                    |                              |
  |                                    |  5. Return messages          |
  |                                    |     ordered by seqNum        |
  |                                    |<------------------------------
  |                                    |                              |
  |  6. SYNC_RESPONSE (batch 1/3)      |                              |
  |  {                                 |                              |
  |    "messages": [...100 msgs...],   |                              |
  |    "hasMore": true,                |                              |
  |    "batchNumber": 1,               |                              |
  |    "totalBatches": 3               |                              |
  |  }                                 |                              |
  |<-----------------------------------|                              |
  |                                    |                              |
  |  7. BATCH_ACK { batch: 1 }        |                              |
  |----------------------------------->|  8. Remove ACK'd messages    |
  |                                    |------------------------------>
  |                                    |                              |
  |  9. SYNC_RESPONSE (batch 2/3)      |                              |
  |<-----------------------------------|                              |
  |                                    |                              |
  |  ... (repeat until all batches)    |                              |
  |                                    |                              |
  |  10. SYNC_COMPLETE                 |                              |
  |<-----------------------------------|                              |
  |                                    |                              |
  |  === Normal real-time flow ===     |                              |
  |                                    |                              |
```

### Protocol Details

**Step 3 -- SYNC_REQUEST:** The client sends the last-known sequence number for each conversation it has locally. This is crucial: the sequence number is per-conversation and monotonically increasing (assigned by the server). The client says: "I have up to sequence 48291 for conversation conv_abc -- send me everything after that."

**Step 4-5 -- Server query:** The server queries the offline queue: `SELECT * FROM offline_queue WHERE recipientId = ? AND conversationId = ? AND sequenceNumber > ? ORDER BY sequenceNumber ASC`. This is efficient because the offline queue is partitioned by `(recipientId, conversationId)` and ordered by `sequenceNumber`.

**Step 6 -- Pagination:** If the user has been offline for a long time (e.g., days), there may be thousands of messages. Sending them all at once would:
- Overwhelm the client (memory, rendering)
- Block the WebSocket connection (no new real-time messages during bulk transfer)
- Risk timeout/disconnection on slow networks

Solution: paginate. The server sends messages in batches (e.g., 100 messages per batch). The client processes each batch, renders messages, and ACKs before requesting the next.

**Step 7-8 -- ACK and removal:** When the client ACKs a batch, the server removes those messages from the offline queue. This is critical for exactly-once delivery semantics. If the client disconnects mid-sync, the next reconnect will resume from the last ACKed batch (the un-ACKed messages are still in the queue).

### Edge Cases

| Scenario | Handling |
|----------|----------|
| **Client disconnects mid-sync** | Server retains un-ACKed messages. Next connect resumes from last ACKed sequence number. |
| **New messages arrive during sync** | Server buffers new real-time messages. After sync completes (`SYNC_COMPLETE`), buffered messages are delivered normally via WebSocket. |
| **Conversation deleted during offline period** | Server includes a `CONVERSATION_DELETED` event in the sync stream. Client removes the conversation locally. |
| **Sequence gap detected by client** | Client requests re-delivery for the specific gap range. Server re-sends from offline queue or message store. |
| **Very long offline (>30 days)** | Some messages may have been dropped (see [Section 3](#3-offline-message-retention)). Server sends a `MESSAGES_EXPIRED` indicator for affected conversations. Client shows "Some messages may be missing." |

### Ordering Guarantee

Messages within a conversation are delivered in `sequenceNumber` order. The server guarantees this because:

1. `sequenceNumber` is assigned atomically at the server when the message is accepted.
2. The offline queue is ordered by `sequenceNumber`.
3. The sync protocol delivers messages in order within each batch.

Cross-conversation ordering is NOT guaranteed (and doesn't need to be). The client renders each conversation independently.

---

## 3. Offline Message Retention

WhatsApp follows a "server as transient relay" philosophy. The server is not a permanent store -- it holds messages only long enough to deliver them.

### Retention Policy

| State | Retention | Rationale |
|-------|-----------|-----------|
| **Delivered (ACKed)** | Deleted immediately | Server has no reason to keep a message after the recipient confirmed receipt. Minimizes attack surface. |
| **Undelivered (recipient offline)** | Up to 30 days | Gives the user a reasonable window to come back online. After 30 days, the user likely has a new device or abandoned the account. |
| **Media (undelivered)** | Up to 30 days | Same as text messages. Encrypted media blobs in object storage are TTL'd. |
| **Media (delivered)** | Deleted after download + grace period | Once the recipient downloads the media, the server copy is no longer needed. A short grace period (e.g., 24-48 hours) covers multi-device scenarios. [INFERRED -- exact grace period not officially documented] |

### Why 30 Days?

- **Too short (e.g., 7 days):** Users who travel, lose their phone, or are in areas with poor connectivity could miss messages permanently.
- **Too long (e.g., 90 days):** Increases server-side storage costs for undelivered messages. More data at risk if the server is compromised.
- **30 days** is a pragmatic middle ground. It covers most real-world offline scenarios while keeping storage bounded.

### Contrast with Other Systems

| System | Message Retention | Philosophy |
|--------|-------------------|------------|
| **WhatsApp** | Deleted after delivery; 30 days max for undelivered | Server is a transient relay. Client is the source of truth. Backups are to iCloud/Google Drive (encrypted). |
| **Slack** | Retained indefinitely (searchable, compliant) | Server is the permanent store. Enterprise compliance requires retention. |
| **Telegram** | Retained indefinitely in the cloud | Server is the permanent store. Enables seamless multi-device access. Trade-off: server can read regular chat messages. |
| **Discord** | Retained indefinitely | Server is the permanent store. Community history must be preserved. |

---

## 4. Badge Count / Unread Count

The server tracks unread message counts so that push notifications and reconnect flows can display accurate badge numbers.

### Data Model

```
UnreadCount:
  (userId, conversationId) -> {
    count: int,           // Number of unread messages
    lastReadSeqNum: int,  // Last sequence number the user read
    mentionCount: int     // Number of unread @mentions (groups)
  }
```

### How Unread Counts Are Updated

| Event | Server Action | Client Action |
|-------|---------------|---------------|
| **New message arrives (user offline)** | Increment `count` for that conversation | N/A (offline) |
| **New message arrives (user online, chat not open)** | Increment `count` | Client receives message via WebSocket, increments local count |
| **User opens a conversation** | Client sends `READ` receipt with latest `sequenceNumber` | Reset local count to 0 |
| **Server receives READ receipt** | Set `count = 0`, update `lastReadSeqNum` | N/A |
| **Push notification sent** | Include current total unread count as badge number | N/A |
| **User reconnects** | Send unread counts per conversation in sync response | Client updates local counts |

### Badge Number in Push Notifications

The `badge` field in the APNs payload (and equivalent on Android) reflects the **total unread count across all conversations**. The server computes this as `SUM(count) WHERE userId = ?`.

This must be computed at push-send time, not cached, because other conversations may have had their counts change since the last push. However, for performance, the server can maintain a materialized total:

```
TotalUnread:
  userId -> totalUnreadCount
```

Updated atomically whenever any conversation's unread count changes. This avoids a SUM query on every push.

### Sync on Reconnect

When the client reconnects, the sync response includes per-conversation unread counts:

```json
{
  "type": "SYNC_COUNTS",
  "conversations": [
    { "conversationId": "conv_abc", "unreadCount": 12, "lastReadSeqNum": 48279 },
    { "conversationId": "conv_def", "unreadCount": 3, "lastReadSeqNum": 12041 },
    { "conversationId": "conv_ghi", "unreadCount": 0, "lastReadSeqNum": 7833 }
  ]
}
```

The client uses this to update its local state and render the conversation list with accurate unread badges.

---

## 5. Multi-Device Offline Sync

WhatsApp supports linked devices: a primary phone + up to 4 companion devices (web, desktop). Each device has its own WebSocket connection and its own offline state.

### Per-Device Sequence Tracking

Each device independently tracks its last-received sequence number per conversation:

```
DeviceSync:
  (userId, deviceId, conversationId) -> lastDeliveredSeqNum
```

This means: if Device A is online and Device B is offline, Device A receives the message immediately while Device B receives it on next connect. The server must deliver to ALL devices, not just the first one that ACKs.

### Delivery Flow (Multi-Device)

```
                    Sender
                      |
                      v
               +-------------+
               |   Server    |
               +------+------+
                      |
          +-----------+-----------+
          |           |           |
          v           v           v
     Device A     Device B    Device C
     (online)     (offline)   (online)
          |           |           |
     Immediate    Store in    Immediate
     delivery     offline     delivery
     via WS       queue       via WS
          |           |           |
       ACK A       (later)     ACK C
          |           |           |
          |      Reconnects      |
          |      Sync + ACK B    |
          |           |           |
     All 3 devices have the message
```

### Key Rules

1. **Message is "delivered" only when ALL devices ACK.** The server tracks per-device delivery status. A message is not removed from the offline queue until the specific device ACKs it.

2. **Each device sends its own SYNC_REQUEST.** When Device B reconnects, it sends its own `lastSequences` map. Device B's sequence numbers may lag behind Device A's (since B was offline).

3. **Read receipts are per-user, not per-device.** When the user reads a message on any device, the READ receipt is sent to the sender. But internally, the server still tracks per-device delivery for the purpose of offline queue management.

4. **Companion device key management.** Each device has its own encryption keys (Signal Protocol handles this). Messages are encrypted per-device (or using Sender Keys for groups). The server fans out encrypted copies to each device. This is the fundamental reason WhatsApp was slow to add multi-device: each message requires per-device encryption, multiplying the encryption work. [INFERRED -- exact implementation of multi-device encryption is not fully documented publicly, but the Signal Protocol's multi-device approach is well-known]

### Conflict: Read on One Device, Unread on Another

If the user reads a message on Device A (phone), Device B (desktop) should also mark it as read. This is handled by syncing read state:

- Device A sends READ receipt to server.
- Server pushes a `READ_SYNC` event to all other online devices.
- Offline devices receive it on next sync.

```json
{
  "type": "READ_SYNC",
  "conversationId": "conv_abc",
  "lastReadSeqNum": 48295,
  "timestamp": 1708444800300
}
```

---

## 6. Push Notification Optimization

Sending a separate push notification for every message is wasteful and annoying. Optimization is critical at WhatsApp scale (~100 billion messages/day).

### Batching / Consolidation

If a user is offline and receives 50 messages from Alice, the server should NOT send 50 individual push notifications. Instead:

```
Strategy: Consolidation Window

  Message 1 arrives     -> Start consolidation timer (e.g., 2-3 seconds)
  Message 2 arrives     -> Reset timer, update count
  Message 3 arrives     -> Reset timer, update count
  ...
  Timer expires         -> Send ONE push: "Alice: 50 new messages"

  OR

  Message from Bob      -> Different sender, send separate push
                           (but still consolidated per-sender)
```

**Rules:**
- Consolidate per-sender within a short time window (2-5 seconds).
- After the window, send one notification: "Alice: 50 new messages" (or "3 new messages in Family Group").
- If messages come from multiple senders, send at most one notification per sender (or one aggregated: "3 new messages from 2 chats").
- Cap: never more than N push notifications per minute per user (e.g., 30/minute). Beyond that, aggregate.

### Rate Limiting

| Level | Limit | Rationale |
|-------|-------|-----------|
| **Per-user** | Max 30 pushes/minute | Prevent notification flood from active group chats |
| **Per-conversation** | Max 5 pushes/minute | Don't spam the user for a single chatty conversation |
| **Global (server-side)** | Throttle based on APNs/FCM rate limits | Apple and Google impose per-app rate limits. Exceeding them causes pushes to be dropped or delayed. |

### Priority Levels

| Event Type | Push Priority | Rationale |
|------------|---------------|-----------|
| **New message** | High | User expects immediate notification for messages |
| **Delivery/read receipt** | None (no push) | Receipts are only relevant when the user is looking at the chat. Delivered via WebSocket when online. |
| **Typing indicator** | None (no push) | Ephemeral, only relevant in real-time. Never pushed via APNs/FCM. |
| **Group info change** | Low | "Bob changed the group name" can be slightly delayed |
| **Missed call** | High | Time-sensitive, similar to messages |
| **Contact joined** | Low / Silent | "Alice joined WhatsApp" is informational, not urgent |

### APNs/FCM Efficiency

- **APNs:** Supports coalescing via `apns-collapse-id`. Multiple notifications with the same collapse ID replace each other on the device. Use `collapse-id = conversationId` so that 50 messages in the same chat result in one visible notification (the latest one).
- **FCM:** Similar coalescing via `collapse_key` (for `notification` messages) or app-side handling (for `data` messages). Limit of 4 stored messages per collapse key.

---

## 7. Silent Push / Background Fetch

### iOS: Silent Push + Notification Service Extension

iOS is restrictive about background execution. Two mechanisms work together:

**1. Silent Push (`content-available: 1`):**
- Wakes the app in the background for ~30 seconds of execution time.
- The app can fetch data, update local state, but CANNOT show a visible notification from this alone.
- Throttled by iOS: if the app doesn't use background time efficiently, iOS reduces delivery frequency.
- Used for: pre-fetching messages, updating badge counts, syncing state.

**2. Notification Service Extension (NSE) (`mutable-content: 1`):**
- A separate extension process that intercepts push notifications BEFORE they are displayed.
- Has ~30 seconds to modify the notification content.
- This is where WhatsApp decrypts the notification: the NSE receives the push (with sender metadata), fetches the encrypted message from the server, decrypts it using locally stored keys, and replaces the notification body with the decrypted content.
- If decryption fails or times out, falls back to the generic "New message" text.

```
APNs                    iOS Device
  |                        |
  | Push notification      |
  | (mutable-content: 1)   |
  |----------------------->|
  |                        |
  |                  +-----+------+
  |                  | Notification|
  |                  | Service     |
  |                  | Extension   |
  |                  +-----+------+
  |                        |
  |                  1. Receive push metadata
  |                     (sender: "Alice", msgId: "xyz")
  |                        |
  |                  2. Fetch encrypted message
  |                     from server (short-lived HTTPS)
  |                        |
  |                  3. Load encryption keys from
  |                     shared Keychain (app group)
  |                        |
  |                  4. Decrypt message content
  |                        |
  |                  5. Modify notification:
  |                     title: "Alice"
  |                     body: "Hey, are you free tonight?"
  |                        |
  |                  6. Display decrypted notification
  |                        |
  |                  (If decrypt fails: show
  |                   "New message from Alice")
  |                        |
```

**Constraints of the NSE:**
- Runs in a separate process from the main app, with limited memory (~24 MB).
- Must complete within ~30 seconds or iOS terminates it and shows the original (generic) notification.
- Cannot access the main app's in-memory state -- must use shared storage (Keychain, App Groups shared container).
- This is why WhatsApp occasionally shows "New message" instead of the actual content on iOS -- the NSE timed out or failed to decrypt.

### Android: FCM High-Priority Data Messages

Android is more permissive with background execution (though increasingly restricted in newer versions):

**High-priority FCM data messages:**
- Delivered immediately, even in Doze mode.
- Triggers the app's `FirebaseMessagingService.onMessageReceived()`.
- The app has ~20 seconds to process (10 seconds on Android 12+). [INFERRED -- exact limits vary by Android version]
- The app can fetch the encrypted message, decrypt it, and display a notification with actual content.

**Flow:**
1. FCM delivers high-priority data message to the device.
2. App wakes in background.
3. App fetches encrypted message from server (or decrypts from payload if included).
4. App decrypts using locally stored keys.
5. App displays notification with decrypted content via `NotificationManager`.

**Android advantages over iOS:**
- More background execution time.
- Direct access to app's storage (no separate extension process).
- Less aggressive throttling of background wake-ups.

**Android challenges:**
- OEM-specific battery optimizations (Samsung, Xiaomi, Huawei) aggressively kill background apps. Users must manually whitelist the chat app.
- Doze mode batches non-high-priority messages (another reason to use high priority for chat messages).

---

## 8. Notification Content Privacy

### The E2E Encryption Constraint

Push notifications travel through third-party infrastructure (Apple's APNs, Google's FCM). If the push payload contained the actual message text, these providers could read it -- breaking the E2E guarantee.

```
WRONG (violates E2E):
  Server -> APNs: { body: "Hey, are you free tonight?" }
  Apple can read this. E2E is broken.

CORRECT (preserves E2E):
  Server -> APNs: { body: "New message", mutable-content: 1 }
  Notification Service Extension decrypts locally on device.
  Apple never sees plaintext.
```

### Privacy Options for Notification Display

| Option | Example Display | Privacy Level | User Experience |
|--------|----------------|---------------|-----------------|
| **Generic** | "New message" | Highest | Least useful -- user doesn't know who or what |
| **Sender only** | "New message from Alice" | High | User knows who but not what |
| **Sender + type** | "Alice sent a photo" | Medium-high | Useful without revealing content |
| **Decrypted content** | "Alice: Hey, are you free tonight?" | Medium (device-local decryption) | Best UX, but requires NSE/background processing |
| **No notification** | (silent) | Highest | User misses messages |

WhatsApp defaults to showing decrypted content (via NSE on iOS, background processing on Android). Users can configure notifications to hide content in the app settings ("Show Preview" toggle). When disabled, notifications show "New message from Alice" or just "New message."

### Lock Screen Considerations

Even with decrypted notifications, the lock screen is a privacy concern:

- **iOS:** Users can set "Show Previews" to "When Unlocked" (system setting). WhatsApp respects this.
- **Android:** Sensitive notification content can be marked with `Notification.VISIBILITY_PRIVATE`, showing a redacted version on the lock screen.

WhatsApp provides an in-app setting to control notification preview on the lock screen independently of the system setting.

### Metadata Leakage

Even without message content, push notification metadata reveals:
- **Who** is messaging the user (sender name).
- **When** messages arrive (timing patterns).
- **How often** (frequency patterns).

This metadata is visible to Apple/Google. Signal goes further by using a technique where the push notification contains no metadata at all -- just a "wake up" signal. The app then fetches everything directly from the Signal server. This provides even stronger privacy at the cost of slightly higher latency. [INFERRED -- Signal's exact approach may have evolved]

---

## 9. Contrast with Slack

Slack's notification and offline sync architecture is fundamentally different because Slack does NOT use E2E encryption and stores all messages permanently on the server.

### Push Notifications

| Aspect | WhatsApp | Slack |
|--------|----------|-------|
| **Content in push** | Metadata only (E2E encrypted). Decrypted locally via NSE. | Full message preview in push payload. Server has plaintext. |
| **Rich notifications** | Limited: sender name, message type | Rich: channel name, thread context, user avatar, message preview, action buttons (mark as read, reply) |
| **Push logic** | Simple: user offline + new message = push | Complex: respects "Do Not Disturb" schedules, channel-level mute, thread-only notifications, keyword highlights, "notify for all messages" vs "mentions only" per channel |
| **Notification grouping** | Per-conversation | Per-workspace, per-channel, per-thread |

### Offline Sync / "Catch-Up"

Slack's reconnect experience is fundamentally different:

```
WhatsApp Reconnect:              Slack Reconnect:

1. Drain offline queue           1. Load workspace state
   (messages queued while           (channels, DMs, threads)
    offline)                     2. For each channel: fetch
2. Done. That's it.                 unread messages since
                                    last active timestamp
                                 3. Load thread updates
                                 4. Load mention highlights
                                 5. Load reactions/edits
                                 6. Build "Catch Up" view
                                    (All Unreads, Threads,
                                     Mentions & Reactions)
```

**Why so different?**
- WhatsApp's server is a transient relay -- it only has the offline queue. Once drained, there's nothing left on the server.
- Slack's server is the permanent store -- it has EVERYTHING. The catch-up experience reads from persistent, indexed, searchable storage. Slack can offer "All Unreads" across all channels because the server holds the complete message history.

### Notification Preferences

Slack has far more granular notification controls because of its workspace/channel model:

- Per-channel: notify for all messages, mentions only, or nothing.
- Per-thread: follow/unfollow specific threads.
- Keyword notifications: get notified when specific words appear (e.g., your name, "deploy", "incident").
- DND schedules: suppress notifications during configured hours.
- Device-specific: different settings for mobile vs desktop.

WhatsApp's controls are simpler: per-conversation mute (8 hours, 1 week, always) and a global notification toggle.

---

## 10. Contrast with Telegram

Telegram sits between WhatsApp and Slack: it's consumer-focused like WhatsApp but cloud-based like Slack.

### Push Notifications

| Aspect | WhatsApp | Telegram |
|--------|----------|----------|
| **Content in push** | Metadata only (E2E). Decrypted locally. | Full message preview in push payload. Server has plaintext for regular chats. |
| **Secret Chat notifications** | N/A (all chats are E2E) | Secret Chat pushes do NOT include content (similar to WhatsApp). Regular chat pushes DO include content. |
| **Server-side notification logic** | Minimal: offline + new message = push | Rich: server applies user preferences (mute, exceptions, DND), channel notification settings, pinned message notifications, scheduled message delivery |
| **Notification for channels** | N/A (no channels concept) | Push for channel posts (subscribers can configure per-channel). Channels can have thousands/millions of subscribers -- requires efficient fan-out for push. |

### Offline Sync

| Aspect | WhatsApp | Telegram |
|--------|----------|----------|
| **Where messages live** | Offline queue (transient) | Server (permanent cloud storage) |
| **Sync model** | Drain offline queue using sequence numbers | Client fetches from server's permanent store. No "offline queue" concept -- messages are always on the server. |
| **Long offline period** | Messages older than 30 days are dropped | ALL messages are available, no matter how long offline. Cloud storage means nothing expires. |
| **New device setup** | Chat history must be restored from client backup (iCloud/Google Drive) | Full history available immediately from the cloud. Log in on a new device, all messages appear. |
| **Search** | Client-side only (server can't search encrypted content) | Server-side full-text search across all chats (server has plaintext for regular chats) |

### The Fundamental Trade-off

```
Privacy <────────────────────────────────────────────> Convenience

  WhatsApp                                        Telegram
  (E2E always,                                    (Cloud-based,
   server sees nothing,                            server has plaintext,
   30-day offline limit,                           unlimited history,
   no cloud search,                                full-text search,
   new device = start fresh                        new device = full sync
   unless backup exists)                           instantly)
```

Telegram chose convenience: cloud storage enables features that WhatsApp architecturally cannot offer (server-side search, instant multi-device sync, unlimited history). The cost is that Telegram's servers hold plaintext messages for regular chats.

WhatsApp chose privacy: the server is an untrusted relay that never sees content. The cost is limited server-side features and a harder multi-device problem (each device needs its own encryption keys, and there's no cloud history to sync from).

### Telegram's Secret Chats

Telegram's Secret Chats ARE E2E encrypted (using MTProto 2.0), and they behave more like WhatsApp:
- Not stored on Telegram's cloud.
- Device-specific (not available on other devices).
- Self-destruct timers.
- Push notifications do NOT include content (just like WhatsApp).
- No server-side search.

This proves the architectural constraint: E2E encryption inherently limits server-side capabilities. Telegram offers both models; WhatsApp chose to make E2E the only model.

---

## Summary: Key Design Decisions

| Decision | Choice | Alternative | Why This Choice |
|----------|--------|-------------|-----------------|
| Push payload content | Metadata only | Include encrypted payload | APNs/FCM would see content if unencrypted. Even encrypted payloads increase push size (APNs limit: 4 KB). |
| Notification decryption | iOS NSE / Android background service | Always show "New message" | Users expect to see message content in notifications. Decryption improves UX while preserving E2E. |
| Offline sync protocol | Sequence-number-based with pagination | Timestamp-based / full re-sync | Sequence numbers are gap-free and ordered. Timestamps can have collisions and clock skew. Pagination prevents client overload. |
| Offline retention | 30 days max | Unlimited (like Telegram) / 7 days | 30 days balances storage cost, privacy (less data at rest), and user experience. |
| Push consolidation | Time-window batching | Individual push per message | 50 individual pushes are annoying and waste battery/bandwidth. Batching is better UX. |
| Multi-device delivery | Per-device offline queue | Single queue with multi-device fanout on connect | Per-device queues are simpler to reason about. Each device independently tracks its state. |
| Badge count tracking | Server-maintained materialized count | Compute on push (SUM query) | Materialized count avoids expensive SUM on every push send. Updated incrementally. |

---

## Related Documents

- [01 — Interview Simulation (main backbone)](01-interview-simulation.md)
- [03 — Messaging and Delivery Pipeline](03-messaging-and-delivery.md)
- [04 — Connection and Presence](04-connection-and-presence.md)
- [05 — End-to-End Encryption (Signal Protocol)](05-end-to-end-encryption.md)
- [06 — Storage and Data Model](06-storage-and-data-model.md)
