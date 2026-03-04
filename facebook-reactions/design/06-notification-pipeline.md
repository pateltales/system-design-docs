# Notification Coalescing & Delivery Pipeline

## 1. The Coalescing Problem

When a post goes viral and 500 people react in 5 minutes, the notification system must
not bombard the post owner with 500 separate alerts.

| Approach | User Experience | Example |
|----------|----------------|---------|
| **BAD** | 500 separate push notifications | "Alice liked your post" x 500 |
| **GOOD** | 1 coalesced notification | "Alice, Bob, and 498 others liked your post" |
| **BETTER** | 1 coalesced notification with relevance-ranked names | Close friends and frequent interactors named first |

The core idea: **buffer, aggregate, then deliver once.** This is a classic
batch-vs-stream tradeoff applied to the notification domain.

---

## 2. Coalescing Window

Reaction events for a given `(entityId, entityOwnerId)` pair are buffered for a
configurable window before a single notification is emitted.

### Basic Window Mechanics

```
t=0s    First reaction arrives  -->  Start 30-second timer
t=5s    10 more reactions       -->  Accumulate in buffer
t=12s   50 more reactions       -->  Accumulate in buffer
t=30s   Timer fires             -->  Emit coalesced notification (61 reactions)
t=35s   5 more reactions        -->  Start NEW 30-second timer
t=65s   Timer fires             -->  Emit second coalesced notification (5 reactions)
```

### Adaptive Window

Not all posts have the same reaction velocity. A one-size-fits-all window either
delays normal posts or spams viral ones.

```
Reaction Rate (events/sec)    Window Duration
─────────────────────────────────────────────
< 1                           15 seconds   (prompt delivery for normal posts)
1 - 10                        30 seconds   (default)
10 - 100                      60 seconds   (moderate virality)
> 100                         120 seconds  (viral, aggressive coalescing)
```

The coalescing service tracks the event rate per buffer and dynamically extends or
shortens the window.

### The Fundamental Tradeoff

```
Longer Window ──────────────────────── Shorter Window
   Fewer notifications                    More notifications
   Higher latency                         Lower latency
   Better coalescing                      Worse coalescing
   Less spam                              More spam
```

For most social platforms, users prefer fewer, richer notifications over rapid-fire
alerts. Facebook leans toward longer windows for high-activity content.

---

## 3. Coalescing Architecture

### End-to-End Pipeline

```
┌──────────────────────────────────────────────────────────────────────────────────┐
│                        NOTIFICATION PIPELINE                                     │
│                                                                                  │
│  ┌─────────────┐    ┌──────────────────┐    ┌──────────────────┐                │
│  │  Reaction    │    │   Coalescing     │    │  Notification    │                │
│  │  Service     │    │   Service        │    │  Service         │                │
│  │             ─┼───>│                  ├───>│                  │                │
│  │  (writes to  │    │  - Per-entity    │    │  - Renders text  │                │
│  │   Kafka)     │    │    buffers       │    │  - Applies       │                │
│  └─────────────┘    │  - Timer mgmt    │    │    suppression   │                │
│         │            │  - Adaptive      │    │  - Deduplicates  │                │
│         │            │    windows       │    │                  │                │
│         v            └────────┬─────────┘    └───────┬──────────┘                │
│  ┌─────────────┐              │                      │                           │
│  │   Kafka     │              │                      v                           │
│  │  reactions  │         ┌────┴─────┐       ┌────────────────────┐               │
│  │  topic      │         │  Redis   │       │  Delivery Router   │               │
│  │             │         │  (state  │       │                    │               │
│  └─────────────┘         │  backup) │       │  ┌──────┐ ┌─────┐ │               │
│                          └──────────┘       │  │ Push │ │Badge│ │               │
│                                             │  │ (APN/│ │     │ │               │
│                                             │  │ FCM) │ │     │ │               │
│                                             │  └──┬───┘ └──┬──┘ │               │
│                                             │     │        │    │               │
│                                             │  ┌──┴────┐   │    │               │
│                                             │  │ Email │   │    │               │
│                                             │  │Digest │   │    │               │
│                                             │  └───────┘   │    │               │
│                                             └──────────────┼────┘               │
│                                                            │                     │
│                                                            v                     │
│                                                    ┌──────────────┐              │
│                                                    │   User's     │              │
│                                                    │   Device     │              │
│                                                    └──────────────┘              │
└──────────────────────────────────────────────────────────────────────────────────┘
```

### Detailed Data Flow

```
┌───────────┐     ┌─────────┐     ┌───────────────┐     ┌──────────────┐     ┌────────────┐
│ Reaction  │     │  Kafka  │     │  Coalescing   │     │ Notification │     │  Delivery  │
│ Service   │     │  Topic  │     │  Service      │     │  Service     │     │  Channels  │
└─────┬─────┘     └────┬────┘     └───────┬───────┘     └──────┬───────┘     └─────┬──────┘
      │                │                  │                    │                   │
      │  Publish       │                  │                    │                   │
      │  ReactionEvent │                  │                    │                   │
      ├───────────────>│                  │                    │                   │
      │                │  Consume         │                    │                   │
      │                ├─────────────────>│                    │                   │
      │                │                  │                    │                   │
      │                │                  │ Buffer exists      │                   │
      │                │                  │ for this entity?   │                   │
      │                │                  │                    │                   │
      │                │                  │──┐ NO: Create      │                   │
      │                │                  │  │ buffer, start   │                   │
      │                │                  │  │ timer           │                   │
      │                │                  │<─┘                 │                   │
      │                │                  │                    │                   │
      │                │                  │──┐ YES: Append     │                   │
      │                │                  │  │ to existing     │                   │
      │                │                  │  │ buffer          │                   │
      │                │                  │<─┘                 │                   │
      │                │                  │                    │                   │
      │                │                  │  Timer fires       │                   │
      │                │                  │                    │                   │
      │                │                  │  CoalescedEvent    │                   │
      │                │                  ├───────────────────>│                   │
      │                │                  │                    │                   │
      │                │                  │                    │ Apply suppression  │
      │                │                  │                    │ rules, render text │
      │                │                  │                    │                   │
      │                │                  │                    │  Route to         │
      │                │                  │                    │  channels         │
      │                │                  │                    ├──────────────────>│
      │                │                  │                    │                   │
      │                │                  │                    │                   │ Push / Badge
      │                │                  │                    │                   │ / Email
      │                │                  │                    │                   │
```

### Coalescing Service Internals

The coalescing service maintains an in-memory map of active buffers:

```
Key:    (entityId, entityOwnerId)
Value:  CoalescingBuffer {
            List<ReactionEvent> events
            Instant windowStart
            Instant windowEnd
            ScheduledFuture<?> timerHandle
            int currentRate       // events per second
        }
```

**State durability:** Every buffer mutation is also written to Redis so that if the
service crashes and restarts, the Kafka consumer replays events, but the Redis state
prevents re-emitting notifications for already-coalesced events.

```
Redis Key:    "coalesce:{entityId}:{ownerId}"
Redis Value:  JSON { reactorIds: [...], windowStart: ..., lastEmittedAt: ... }
Redis TTL:    5 minutes (auto-cleanup of stale buffers)
```

---

## 4. Notification Content

### Text Rendering

The notification service renders human-readable text from the coalesced event.

**Single reaction type:**
```
"Alice, Bob, and 498 others liked your post"
```

**Mixed reaction types (small count):**
```
"Alice loved and Bob liked your post"
```

**Mixed reaction types (large count):**
```
"500 people reacted to your post"
```

### Name Selection Priority

Not all reactors are equally interesting to the post owner. The notification should
lead with the most relevant names.

```
Priority    Source                     Rationale
────────────────────────────────────────────────────────────────────
1           Close friends              Highest social relevance
2           Frequent interactions      People the owner engages with often
3           Most recent reactors       Recency as a tiebreaker
```

This requires a **social graph lookup** at notification generation time:

```
Coalescing Service emits:
    { entityId, ownerId, reactorIds: [u1, u2, ..., u500] }

Notification Service:
    1. Query Social Graph Service for owner's close friends
    2. Intersect close friends with reactorIds --> named first
    3. Query Interaction Service for frequent interactors
    4. Intersect frequent interactors with remaining --> named second
    5. Remaining sorted by reaction timestamp (most recent first)
    6. Pick top 2 names, rest become "and N others"
```

---

## 5. Delivery Channels

Each coalesced notification is routed to one or more delivery channels based on
user preferences and channel-specific rules.

| Channel | Mechanism | Latency | Behavior |
|---------|-----------|---------|----------|
| **Push notification** | APNs (iOS), FCM (Android) | Real-time (~1s) | Delivered immediately after coalescing window closes |
| **In-app badge** | WebSocket or long-poll to client | Real-time (~1s) | Badge counter incremented, notification appears in tray |
| **Email digest** | Batched email job | Hours | Aggregated across many posts, sent as periodic digest |

### Push Notification Payload

```json
{
  "notification": {
    "title": "Reactions on your post",
    "body": "Alice, Bob, and 498 others liked your post",
    "badge": 12
  },
  "data": {
    "entityId": "post_123",
    "notificationType": "REACTION_COALESCED",
    "deepLink": "fb://post/123"
  }
}
```

### Channel Selection Logic

```
Is user online (has active session)?
├── YES: In-app badge only (don't push if they're already looking)
└── NO:
    ├── Push enabled? --> Send push notification
    ├── Email digest enabled? --> Queue for next digest batch
    └── Neither? --> Store in notification tray (visible on next app open)
```

---

## 6. Notification Deduplication

If a notification for the same post's reactions already exists in the user's tray,
the system should **update** the existing notification rather than creating a new one.

### Notification ID Strategy

```
notificationId = hash(entityId + entityOwnerId + notificationType)

Example:
    entityId        = "post_123"
    entityOwnerId   = "user_456"
    notificationType = "REACTION"
    notificationId   = hash("post_123:user_456:REACTION") = "notif_abc789"
```

### Update-in-Place Flow

```
Window 1 fires (t=30s):
    Create notification "notif_abc789":
        "Alice and 48 others liked your post"

Window 2 fires (t=65s):
    Find existing "notif_abc789"
    UPDATE notification:
        "Alice, Bob, and 498 others liked your post"
    Re-surface notification to top of tray
    Send updated push notification
```

### Implementation

```
Notification DB (Cassandra / MySQL):

    notif_abc789 | user_456 | REACTION | post_123 | {
        "reactorCount": 500,
        "topReactorIds": ["alice", "bob"],
        "text": "Alice, Bob, and 498 others liked your post",
        "updatedAt": "2025-01-15T10:05:00Z"
    }
```

The notification service performs an **upsert**: insert if not exists, update if it does.

---

## 7. Notification Suppression

Before delivering any notification, the notification service applies a chain of
suppression rules. If any rule triggers, the notification is silently dropped.

```
┌──────────────────────────────────────────────────────────┐
│               SUPPRESSION RULE CHAIN                     │
│                                                          │
│  CoalescedEvent                                          │
│       │                                                  │
│       v                                                  │
│  ┌────────────────────────┐                              │
│  │ Post age > 30 days?    │── YES ──> DROP               │
│  └────────────┬───────────┘                              │
│               │ NO                                       │
│               v                                          │
│  ┌────────────────────────┐                              │
│  │ User muted this post?  │── YES ──> DROP               │
│  └────────────┬───────────┘                              │
│               │ NO                                       │
│               v                                          │
│  ┌────────────────────────┐                              │
│  │ Reaction notifications │── YES ──> DROP               │
│  │ turned off?            │                              │
│  └────────────┬───────────┘                              │
│               │ NO                                       │
│               v                                          │
│  ┌────────────────────────┐                              │
│  │ All reactors blocked   │── YES ──> DROP               │
│  │ by owner?              │                              │
│  └────────────┬───────────┘                              │
│               │ NO                                       │
│               v                                          │
│  ┌────────────────────────┐                              │
│  │ Owner reacted to own   │── YES ──> DROP               │
│  │ post? (self-reaction)  │          (filter out self,   │
│  └────────────┬───────────┘           keep others)       │
│               │ NO                                       │
│               v                                          │
│         DELIVER notification                             │
│                                                          │
└──────────────────────────────────────────────────────────┘
```

### Rule Details

| Rule | Check | Data Source |
|------|-------|-------------|
| **Stale post** | `post.createdAt < now() - 30 days` | Post metadata cache |
| **Muted post** | `mutes.contains(entityId, ownerId)` | User preferences store |
| **Notifications off** | `settings.reactionNotifications == false` | User settings service |
| **Blocked reactors** | Filter out blocked user IDs from reactor list; if none remain, drop | Block list service |
| **Self-reaction** | `reactorIds.remove(ownerId)`; if empty after removal, drop | Inline check |

For the **blocked reactors** rule, note that some reactors may be blocked while others
are not. The service filters out blocked reactors from the list and proceeds with the
remaining ones. Only if all reactors are blocked is the notification dropped entirely.

---

## 8. At-Least-Once Delivery

The pipeline uses Kafka's **at-least-once** delivery semantics. This means a reaction
event may be delivered to the coalescing service more than once (e.g., after a consumer
restart before offset commit).

### Why At-Least-Once (Not Exactly-Once)

- **At-most-once**: Risk losing reaction notifications entirely. Unacceptable.
- **Exactly-once**: Requires Kafka transactions + idempotent producers. Higher complexity
  and latency for marginal benefit in this use case.
- **At-least-once**: Simple, reliable. Duplicates are handled downstream.

### Deduplication Strategy

```
Coalescing Service receives event:
    reactionId = "rxn_789"

    1. Check Redis set: SISMEMBER "coalesce:{entityId}:{ownerId}:seen" "rxn_789"
    2. If already seen --> skip (deduplicate)
    3. If new --> SADD to seen set, add to buffer
```

### Crash Recovery

```
Scenario: Coalescing service crashes mid-window

1. Service had buffer: { entityId: post_123, reactors: [u1, u2, u3] }
2. State was persisted to Redis on each mutation
3. Service restarts, Kafka consumer replays from last committed offset
4. Replayed events for u1, u2, u3 are deduplicated via Redis seen-set
5. New events (u4, u5) are added to buffer normally
6. Timer is restarted for remaining window duration
```

---

## 9. Contrast with Other Platforms

Different platforms make different tradeoffs based on their notification volumes
and user expectations.

```
┌─────────────┬──────────────────┬─────────────┬────────────────────────────────┐
│ Platform    │ Coalescing?      │ Window Size │ Rationale                      │
├─────────────┼──────────────────┼─────────────┼────────────────────────────────┤
│ Facebook    │ Yes, aggressive  │ 30-120s     │ High reaction volume per post. │
│             │                  │ (adaptive)  │ Viral posts common. Must       │
│             │                  │             │ coalesce to avoid spam.        │
├─────────────┼──────────────────┼─────────────┼────────────────────────────────┤
│ WhatsApp    │ No               │ N/A         │ Reaction rate per message is   │
│             │                  │             │ low (small groups). Individual │
│             │                  │             │ notifications are acceptable.  │
├─────────────┼──────────────────┼─────────────┼────────────────────────────────┤
│ YouTube     │ Yes, similar     │ Longer      │ Video comments/likes can spike │
│             │                  │ (minutes)   │ but YouTube is less real-time  │
│             │                  │             │ than Facebook. Delay is OK.    │
├─────────────┼──────────────────┼─────────────┼────────────────────────────────┤
│ Slack       │ No               │ N/A         │ Emoji reactions are per-       │
│             │                  │             │ workspace. Volume is low       │
│             │                  │             │ enough that individual notifs  │
│             │                  │             │ work. Users expect immediacy.  │
├─────────────┼──────────────────┼─────────────┼────────────────────────────────┤
│ Instagram   │ Yes, same as FB  │ Similar     │ Same Meta infrastructure.      │
│             │                  │ to Facebook │ Shared notification platform   │
│             │                  │             │ across Meta apps.              │
└─────────────┴──────────────────┴─────────────┴────────────────────────────────┘
```

### Key Insight

The decision to coalesce depends on the **reaction rate per entity**:

- **Low rate** (WhatsApp message, Slack emoji): Individual notifications are fine.
  Coalescing would add unnecessary latency.
- **High rate** (Facebook post, YouTube video): Coalescing is essential. Without it,
  viral content would generate notification storms.

Facebook's adaptive window is the sophisticated middle ground: it behaves like a
low-latency system for normal posts and like a high-coalescing system for viral ones.

---

## Full Pipeline Summary

```
                         FACEBOOK REACTIONS NOTIFICATION PIPELINE

  ┌──────────┐   ┌───────────┐   ┌────────────────┐   ┌────────────────┐   ┌──────────┐
  │  User    │   │ Reaction  │   │   Kafka        │   │  Coalescing   │   │  Redis   │
  │  taps    ├──>│ Service   ├──>│  reactions      ├──>│  Service      ├──>│  State   │
  │  "Like"  │   │           │   │  topic         │   │               │   │  Backup  │
  └──────────┘   └───────────┘   └────────────────┘   └───────┬───────┘   └──────────┘
                                                              │
                                                     Timer fires (30-120s)
                                                              │
                                                              v
                                                    ┌─────────────────┐
                                                    │  Notification   │
                                                    │  Service        │
                                                    │                 │
                                                    │  1. Suppression │
                                                    │     rules       │
                                                    │  2. Social graph│
                                                    │     lookup      │
                                                    │  3. Text render │
                                                    │  4. Dedup/upsert│
                                                    └────────┬────────┘
                                                             │
                                      ┌──────────────────────┼──────────────────────┐
                                      │                      │                      │
                                      v                      v                      v
                               ┌─────────────┐     ┌──────────────┐      ┌─────────────────┐
                               │  APNs / FCM │     │  In-App      │      │  Email Digest   │
                               │  Push       │     │  Badge +     │      │  (batched,      │
                               │  (real-time)│     │  Notif Tray  │      │   hourly/daily) │
                               └──────┬──────┘     └──────┬───────┘      └────────┬────────┘
                                      │                   │                       │
                                      v                   v                       v
                               ┌──────────────────────────────────────────────────────────┐
                               │                    USER'S DEVICE                         │
                               │  "Alice, Bob, and 498 others liked your post"            │
                               └──────────────────────────────────────────────────────────┘
```
