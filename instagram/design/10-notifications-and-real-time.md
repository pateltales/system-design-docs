# Notifications & Real-Time Features

> Push notifications, real-time messaging, activity feed, and live video.
> Handling the thundering herd when a celebrity post gets millions of likes.

---

## Table of Contents

1. [Push Notifications](#1-push-notifications)
2. [Thundering Herd Problem](#2-thundering-herd-problem)
3. [Real-Time Infrastructure (MQTT)](#3-real-time-infrastructure-mqtt)
4. [Activity Feed](#4-activity-feed)
5. [Live Video](#5-live-video)
6. [Contrasts](#6-contrasts)

---

## 1. Push Notifications

Instagram delivers notifications via two channels:
- **Push notifications** (APNs for iOS, FCM for Android) — delivered even when the app is closed
- **In-app real-time** (MQTT/WebSocket) — delivered while the app is open

### Notification Types

| Type | Trigger | Priority |
|---|---|---|
| **Like** | Someone likes your post | Low (aggregated) |
| **Comment** | Someone comments on your post | Medium |
| **Follow** | Someone follows you | Medium |
| **Mention** | Someone mentions you in a post/comment/Story | High |
| **Tagged** | Someone tags you in a photo | High |
| **DM** | Someone sends you a direct message | High |
| **Story reply** | Someone replies to your Story | Medium |
| **Live** | Someone you follow starts a live video | Medium |
| **Reel interaction** | Someone shares/saves your Reel | Low (aggregated) |

### Notification Delivery Pipeline

```
Event occurs (e.g., User B likes User A's post)
        │
        ▼
┌───────────────────────────────────────┐
│ Notification Service                   │
│                                        │
│ 1. Check recipient's notification      │
│    preferences (has A disabled         │
│    like notifications?)                │
│                                        │
│ 2. Check aggregation rules             │
│    (has A already received a like      │
│    notification for this post          │
│    recently? → aggregate, don't        │
│    send a new push)                    │
│                                        │
│ 3. Store notification in activity feed │
│    (Cassandra/Redis)                   │
│                                        │
│ 4. Determine delivery channel:         │
│    ├── App open? → MQTT push           │
│    └── App closed? → APNs/FCM push     │
└───────────────────┬───────────────────┘
                    │
                    ├──> MQTT broker (if online)
                    └──> APNs/FCM (if offline)
```

---

## 2. Thundering Herd Problem

When a celebrity posts, millions of likes/comments arrive within minutes. Each like generates a notification for the post author.

**Naive approach:** 1M likes → 1M individual push notifications → phone vibrates 1M times → phone explodes.

### Solution: Notification Aggregation

```
Time 0:00 — Celebrity posts a photo

Time 0:01 — 10,000 likes arrive
         → First notification: "user_1 liked your post"

Time 0:02 — 50,000 more likes arrive
         → Aggregated notification: "user_1, user_2, and 49,998 others liked your post"
         → Replace (not add to) the previous notification

Time 0:05 — 200,000 more likes arrive
         → Update count only (no new push — too frequent)
         → Activity feed shows updated count when user opens it

Time 0:30 — 1,000,000 total likes
         → One final aggregated push: "user_1, user_2, and 999,998 others liked your post"
```

**Aggregation rules:**
- **Time window**: Batch notifications within a 30-second window during high-traffic spikes
- **Count threshold**: After N likes on the same post, switch to count-only updates (no individual user names)
- **Push frequency cap**: Max 1 push notification per post per 30-second window
- **In-app update**: Real-time counter update via MQTT (lightweight, no push notification)

**Architecture:**

```
Likes stream for post P
        │
        ▼
┌───────────────────────────────────────┐
│ Aggregation Buffer (Redis)             │
│                                        │
│ Key: notif-buffer:{postId}:{type}      │
│ Value: {count, first_actors[], window} │
│                                        │
│ On each new like:                      │
│   INCR count                           │
│   If count == 1: schedule push in 30s  │
│   If count > threshold: no-op (already │
│     scheduled)                         │
│                                        │
│ After 30s window:                      │
│   Flush: send aggregated notification  │
│   Reset buffer                         │
│   "user1, user2, and {count-2} others  │
│    liked your post"                    │
└───────────────────────────────────────┘
```

---

## 3. Real-Time Infrastructure (MQTT)

**VERIFIED — from Facebook Engineering blog (2011) and @Scale conference talks (2015)**

Meta uses **MQTT** (Message Queuing Telemetry Transport) for mobile real-time communication.

### Why MQTT?

| Aspect | MQTT | HTTP Polling | WebSocket |
|---|---|---|---|
| **Header overhead** | 2 bytes minimum | 100-500+ bytes per request | 2-14 bytes per frame |
| **Connection type** | Persistent TCP | New connection per poll | Persistent TCP |
| **Battery impact** | Minimal (small keepalives) | High (frequent wake-ups) | Moderate |
| **Mobile networks** | Designed for unreliable networks | Poor on flaky connections | Good |
| **QoS levels** | 3 levels (at-most-once, at-least-once, exactly-once) | None (manual retry) | None |
| **Session awareness** | Built-in (clean session flag, retained messages) | None | None |

### MQTT at Instagram

```
┌──────────────┐     ┌──────────────┐     ┌──────────────────┐
│ Instagram App│────>│ MQTT Broker  │<────│ Backend Services │
│ (iOS/Android)│<────│ (Meta infra) │     │ (Notification,   │
│              │     │              │     │  DM, Feed update)│
└──────────────┘     └──────────────┘     └──────────────────┘
    Persistent            Pub/Sub              Publish events
    TCP connection        routing              to user topics
```

**What flows over MQTT:**
- **DM delivery**: New messages delivered in real-time
- **Typing indicators**: "user is typing..." in DM conversations
- **Notification badges**: Updated like/comment/follow counts
- **Feed updates**: "New posts available" indicator (without full-page reload)
- **Stories updates**: New Stories from followed accounts
- **Live video alerts**: "user is live" notifications

**Connection lifecycle:**
1. App opens → establish MQTT connection to broker
2. Subscribe to user's topic (all events for this user)
3. Broker pushes events in real-time
4. App goes to background → connection kept alive with small keepalive packets
5. App killed → fallback to APNs/FCM for push notifications

### WebSocket for Web

Instagram's web version uses **WebSocket** instead of MQTT (WebSocket is natively supported by browsers). Same real-time features, different transport layer.

---

## 4. Activity Feed

The notifications/activity tab displays recent interactions.

### Data Model

```
{
  type: "LIKE",
  actors: [userId_1, userId_2],
  aggregatedCount: 47,
  targetPostId: "post-uuid-abc",
  targetThumbnailUrl: "...",
  timestamp: 1705312800,
  isRead: false
}
```

### Storage

Activity feed entries are stored in a time-series-friendly store:
- **Cassandra**: Wide rows keyed by userId, with timestamp-based clustering columns
- **Redis sorted sets**: For the hot/recent portion (fast reads for the first page)

```
Cassandra table:
  Partition key: userId
  Clustering columns: timestamp (DESC), type
  Columns: actors, targetPostId, aggregatedCount, isRead

  Allows: SELECT * FROM activity WHERE userId = ? ORDER BY timestamp DESC LIMIT 20
```

### Pagination

Cursor-based, same as the feed. The cursor encodes the last notification's timestamp.

---

## 5. Live Video

Instagram Live — real-time video streaming to followers.

### Architecture

```
Creator's phone                                              Viewers' phones
      │                                                           │
      │  RTMP ingest                                              │
      ▼                                                           │
┌───────────────┐     ┌──────────────────┐     ┌──────────────────▼┐
│ Ingest Server │────>│ Transcoding      │────>│ Distribution      │
│ (receives     │     │ (real-time,      │     │ (HLS/DASH to      │
│  RTMP stream) │     │  low-latency     │     │  viewers via CDN) │
│               │     │  encoding)       │     │                   │
└───────────────┘     └──────────────────┘     └───────────────────┘
                                                        │
                                                        │
                                               ┌────────▼─────────┐
                                               │ Real-time        │
                                               │ comments/reactions│
                                               │ (WebSocket/MQTT) │
                                               └──────────────────┘
```

**Key differences from pre-recorded video:**
- **Latency**: Must be <5 seconds end-to-end (creator action → viewer sees it)
- **Real-time transcoding**: Can't pre-transcode — must encode on the fly as the stream arrives
- **Adaptive bitrate**: Still needed (viewers on different networks) but encoding ladder is simpler (fewer resolutions due to latency constraints)
- **Comments/reactions**: Displayed in real-time overlay — separate real-time channel (MQTT/WebSocket) alongside the video stream

**Scale challenge:** When a celebrity with 100M followers goes live, potentially millions of concurrent viewers need the stream. This is handled by CDN edge caching of HLS segments — each segment is small (2-4 seconds) and highly cacheable.

---

## 6. Contrasts

### Instagram vs Twitter — Notifications

| Dimension | Instagram | Twitter |
|---|---|---|
| **Cascading notifications** | No reshare → no cascading | Retweets cascade (a retweet of a popular tweet generates notifications for millions) |
| **Primary notification type** | Likes, comments, follows | Likes, retweets, replies, quote tweets |
| **Aggregation need** | High (celebrity likes) | Very high (viral retweet cascades) |
| **Real-time transport** | MQTT (mobile) + WebSocket (web) | WebSocket |

### Instagram vs WhatsApp — Real-Time

| Dimension | Instagram DMs | WhatsApp |
|---|---|---|
| **Encryption** | Not E2E by default (opt-in) | E2E encrypted (default) |
| **Delivery guarantee** | Best-effort | Guaranteed delivery (store-and-forward) |
| **Read receipts** | Blue checkmarks | Blue checkmarks (with E2E verification) |
| **Transport** | MQTT (shared Meta infra) | MQTT-like (custom protocol) |
| **Scale focus** | Throughput (billions of DMs, not all critical) | Reliability (every message must arrive) |

**Key insight:** WhatsApp is a communication tool where every message MUST be delivered. Instagram DMs are a social feature where occasional delays are tolerable. This difference in reliability requirements affects architecture — WhatsApp invests heavily in guaranteed delivery (store-and-forward, acknowledgments, retry logic), while Instagram optimizes for throughput and low latency on the common path.
