# API Contracts — Facebook Reactions System

> **Context**: Facebook serves 3.07 billion MAU / 2.11 billion DAU. The reactions
> feature launched February 24, 2016 with six types (Like, Love, Haha, Wow, Sad,
> Angry), replacing the original single Like button from 2009. Care was added in
> 2020 as a seventh type. The underlying storage is TAO (Objects + Associations),
> handling billions of reads/sec and millions of writes/sec with eventual
> consistency.
>
> **Core invariant**: A user may have **at most one** reaction per entity. All
> write APIs use **upsert semantics** enforced by a unique constraint on
> `(userId, entityId)`.

---

## Endpoint Summary

| # | Method | Path | Group | Latency Target | Interview |
|---|--------|------|-------|----------------|-----------|
| 1 | `POST` | `/v1/reactions` | React (write) | < 50 ms | ★ |
| 2 | `DELETE` | `/v1/reactions` | React (write) | < 50 ms | ★ |
| 3 | `GET` | `/v1/entities/{entityId}/reactions/summary` | Counts (read) | < 10 ms | ★ |
| 4 | `GET` | `/v1/entities/{entityId}/reactions` | List (read) | < 100 ms | |
| 5 | `POST` | `/internal/notifications/reaction-event` | Notifications | async | |
| 6 | — | Kafka topic `reaction-events` | Activity Feed | async | |
| 7 | `GET` | `/internal/analytics/reactions/trending` | Analytics | < 500 ms | |
| 8 | `GET` | `/internal/analytics/reactions/sentiment` | Analytics | < 500 ms | |

---

## Common Types

```jsonc
// Enum: reactionType
"like" | "love" | "haha" | "wow" | "sad" | "angry" | "care"

// Enum: entityType
"post" | "comment" | "message" | "story"

// Enum: action (internal events)
"add" | "remove" | "change"
```

### Authentication

All external (`/v1/`) endpoints require a valid OAuth 2.0 bearer token in the
`Authorization` header. The **userId is extracted from the auth token** on the
server side — it is never accepted in the request body. This prevents
impersonation and simplifies the client contract.

```
Authorization: Bearer <access_token>
```

### Standard Error Envelope

```json
{
  "error": {
    "code": "INVALID_REACTION_TYPE",
    "message": "Reaction type 'clap' is not supported.",
    "requestId": "req-8a3f-4b2c"
  }
}
```

| HTTP Status | Meaning |
|-------------|---------|
| 400 | Bad request / validation failure |
| 401 | Missing or invalid auth token |
| 403 | User does not have permission on the entity |
| 404 | Entity not found |
| 409 | Conflict (rare — race condition on concurrent upsert) |
| 429 | Rate-limited |
| 500 | Internal server error |

---

## 1. React APIs (Core Write Path) ★

These are the **only two mutations** in the public API surface. Both flow
through the same write pipeline: validate -> upsert into TAO -> publish event
to Kafka -> return response. The Kafka event triggers downstream updates to
pre-aggregated counts, notifications, and activity feeds asynchronously.

### 1.1 Add / Change Reaction ★

Upsert semantics: if the user has no existing reaction on the entity, one is
created. If the user already reacted, the old reaction type is **replaced**.
The response includes `previousReactionType` so the client can animate the
transition.

```
POST /v1/reactions
Content-Type: application/json
Authorization: Bearer <token>
```

**Request Body**

```json
{
  "entityId": "post_98765432",
  "entityType": "post",
  "reactionType": "love"
}
```

| Field | Type | Required | Notes |
|-------|------|----------|-------|
| `entityId` | string | yes | Globally unique entity identifier |
| `entityType` | enum | yes | `"post"`, `"comment"`, `"message"`, `"story"` |
| `reactionType` | enum | yes | One of the seven supported reaction types |

**Response — 200 OK** (upsert replaced existing) or **201 Created** (new reaction)

```json
{
  "reactionId": "rxn_1a2b3c4d",
  "userId": "user_12345678",
  "entityId": "post_98765432",
  "entityType": "post",
  "reactionType": "love",
  "timestamp": "2024-01-15T08:30:00.000Z",
  "previousReactionType": "like"
}
```

| Field | Type | Notes |
|-------|------|-------|
| `reactionId` | string | Server-generated unique ID |
| `userId` | string | Extracted from auth token |
| `entityId` | string | Echo of request |
| `entityType` | string | Echo of request |
| `reactionType` | string | The newly applied reaction |
| `timestamp` | ISO 8601 | Server-side timestamp |
| `previousReactionType` | string or `null` | `null` if this is the first reaction; otherwise the replaced type |

**Latency target**: < 50 ms p99

**Notes**:
- The unique constraint on `(userId, entityId)` in TAO guarantees at most one
  reaction per user per entity. The write is an **assoc_put** that overwrites
  any existing association.
- The server publishes a Kafka event with `action: "add"` (new) or
  `action: "change"` (replaced) immediately after the TAO write succeeds.
- Clients should **optimistically update** the UI before the response arrives
  and reconcile on the response.

---

### 1.2 Remove Reaction ★

Removes the caller's reaction from the specified entity. Idempotent: calling
DELETE when no reaction exists returns 204 without error.

```
DELETE /v1/reactions
Content-Type: application/json
Authorization: Bearer <token>
```

**Request Body**

```json
{
  "entityId": "post_98765432",
  "entityType": "post"
}
```

| Field | Type | Required | Notes |
|-------|------|----------|-------|
| `entityId` | string | yes | Target entity |
| `entityType` | enum | yes | Must match the entity's actual type |

**Response — 204 No Content**

No response body. The `Content-Length` header is `0`.

**Latency target**: < 50 ms p99

**Notes**:
- Internally issues a TAO **assoc_delete** on the `(userId, entityId)` edge.
- A Kafka event with `action: "remove"` is published so downstream consumers
  can decrement pre-aggregated counts and retract notifications.
- `reactionType` is not required in the request because the constraint is
  one-reaction-per-user-per-entity — the server knows which reaction to remove.

---

## 2. Reaction Count APIs (Hot Read Path) ★

This is the single most latency-critical endpoint in the entire system. It is
called on **every post render** in News Feed. With 2.11 billion DAU scrolling
through dozens of posts, this endpoint handles on the order of **billions of
requests per second** globally. Counts must be **pre-aggregated** in a cache
layer (TAO count index + Memcache), never computed on the fly.

### 2.1 Get Reaction Summary ★

```
GET /v1/entities/{entityId}/reactions/summary
Authorization: Bearer <token>
```

**Path Parameters**

| Param | Type | Notes |
|-------|------|-------|
| `entityId` | string | The entity to summarize |

**Response — 200 OK**

```json
{
  "entityId": "post_98765432",
  "total": 24730,
  "counts": {
    "like": 18200,
    "love": 3400,
    "haha": 1850,
    "wow": 620,
    "sad": 410,
    "angry": 200,
    "care": 50
  },
  "topReactionTypes": ["like", "love", "haha"],
  "viewerReaction": "love",
  "topFriendReactors": [
    {
      "userId": "user_55501",
      "name": "Alice Johnson",
      "reactionType": "like"
    },
    {
      "userId": "user_55502",
      "name": "Bob Smith",
      "reactionType": "love"
    },
    {
      "userId": "user_55503",
      "name": "Carol Lee",
      "reactionType": "haha"
    }
  ]
}
```

| Field | Type | Notes |
|-------|------|-------|
| `entityId` | string | Echo of path param |
| `total` | integer | Sum of all reaction counts |
| `counts` | map<string, int> | Per-type breakdown. Zero-count types may be omitted. |
| `topReactionTypes` | string[] | Top 3 reaction types by count, descending. Rendered as emoji icons next to the count in the UI. |
| `viewerReaction` | string or `null` | The current user's reaction on this entity, or `null` if none. Requires a TAO point-lookup on `(viewerId, entityId)`. |
| `topFriendReactors` | object[] | Up to 3 friends of the viewer who reacted. Powers the "Alice, Bob, and 24,728 others" display. Looked up from the friend-reactions index. |

**Latency target**: < 10 ms p99

**Notes**:
- `counts` is read from a **pre-aggregated counter** in TAO (atype count
  index), not computed by scanning individual reaction associations. Writes
  atomically increment/decrement these counters.
- `topReactionTypes` is derived from `counts` at read time (trivial sort of
  at most 7 elements).
- `viewerReaction` requires a single point-lookup in TAO: `assoc_get(viewer,
  entityId, REACTED_TO)`. This is cached in Memcache and is sub-millisecond.
- `topFriendReactors` is the most expensive field. It intersects the viewer's
  friend list with the entity's reactor list. Facebook uses a **precomputed
  friend-reaction index** (maintained async via Kafka consumers) to avoid a
  fan-out join at read time.
- For feed rendering, this endpoint is typically called in **batch** via an
  internal multiplexing layer — a single feed page may fetch summaries for
  20-50 posts in one round trip.

---

## 3. Reaction List APIs

These power the "reaction details" dialog — when a user taps the reaction
count to see the full list of who reacted and with what type.

### 3.1 List Reactions (Paginated)

```
GET /v1/entities/{entityId}/reactions?type=love&cursor=eyJsYXN0SWQiOiAicnhuXzk4NyJ9&limit=20
Authorization: Bearer <token>
```

**Path Parameters**

| Param | Type | Notes |
|-------|------|-------|
| `entityId` | string | The entity whose reactions to list |

**Query Parameters**

| Param | Type | Required | Default | Notes |
|-------|------|----------|---------|-------|
| `type` | enum | no | (all types) | Filter to a single reaction type |
| `cursor` | string | no | (start) | Opaque cursor from a previous response |
| `limit` | integer | no | 20 | Page size. Max 100. |

**Response — 200 OK**

```json
{
  "reactions": [
    {
      "reactionId": "rxn_1a2b3c4d",
      "userId": "user_12345678",
      "name": "Jane Doe",
      "profilePicUrl": "https://scontent.xx.fbcdn.net/v/...",
      "reactionType": "love",
      "timestamp": "2024-01-15T08:30:00.000Z",
      "mutualFriendCount": 12
    },
    {
      "reactionId": "rxn_2b3c4d5e",
      "userId": "user_87654321",
      "name": "John Roe",
      "profilePicUrl": "https://scontent.xx.fbcdn.net/v/...",
      "reactionType": "love",
      "timestamp": "2024-01-15T08:28:45.000Z",
      "mutualFriendCount": 3
    }
  ],
  "cursor": "eyJsYXN0SWQiOiAicnhuXzJiM2M0ZDVlIn0=",
  "hasMore": true
}
```

| Field | Type | Notes |
|-------|------|-------|
| `reactions` | array | Ordered by timestamp descending (most recent first) |
| `reactions[].reactionId` | string | Unique reaction ID |
| `reactions[].userId` | string | Reactor's user ID |
| `reactions[].name` | string | Display name |
| `reactions[].profilePicUrl` | string | Thumbnail URL for the reactor's avatar |
| `reactions[].reactionType` | string | The type of reaction |
| `reactions[].timestamp` | ISO 8601 | When the reaction was created/last changed |
| `reactions[].mutualFriendCount` | integer | Number of mutual friends with the viewer |
| `cursor` | string or `null` | Opaque cursor for the next page. `null` if no more results. |
| `hasMore` | boolean | `true` if more pages exist beyond the cursor |

**Latency target**: < 100 ms p99

**Notes**:
- Uses **cursor-based pagination** (not offset-based) for consistency under
  concurrent writes. The cursor encodes the last-seen `reactionId` so the
  next page starts deterministically even if new reactions arrive between
  requests.
- The sort order within a reaction type tab defaults to friends-first, then
  recency. This requires a lightweight merge of two indexes.
- Results are enriched with profile data via a batch user-info lookup
  (typically cached in Memcache).

---

## 4. Notification APIs (Internal)

These are **internal-only** endpoints not exposed to external clients. They are
invoked by Kafka consumers processing reaction events from the `reaction-events`
topic.

### 4.1 Publish Reaction Notification

```
POST /internal/notifications/reaction-event
Content-Type: application/json
X-Internal-Auth: <service-token>
```

**Request Body**

```json
{
  "recipientUserId": "user_00001111",
  "entityId": "post_98765432",
  "entityType": "post",
  "reactorUserId": "user_12345678",
  "reactionType": "love",
  "action": "add",
  "timestamp": "2024-01-15T08:30:00.000Z"
}
```

| Field | Type | Notes |
|-------|------|-------|
| `recipientUserId` | string | The entity owner who receives the notification |
| `entityId` | string | The entity that was reacted to |
| `entityType` | string | Type of the entity |
| `reactorUserId` | string | The user who performed the reaction |
| `reactionType` | string | The reaction type applied |
| `action` | enum | `"add"`, `"remove"`, or `"change"` |
| `timestamp` | ISO 8601 | Event time |

**Response — 202 Accepted**

```json
{
  "status": "queued",
  "coalescingGroupId": "notif_group_post_98765432"
}
```

**Notification Coalescing Logic**:

Reactions on popular posts can generate thousands of events per second. Sending
a separate push notification for each reaction would be unusable. The
notification service applies **coalescing** with the following rules:

1. **Grouping key**: `(recipientUserId, entityId)`. All reactions on the same
   entity for the same recipient are grouped.
2. **Debounce window**: 30 seconds. After the first reaction event in a group,
   the service waits 30 seconds, collecting all subsequent events, before
   dispatching a single notification.
3. **Notification template** uses the most recent reactors and a count:
   - 1 reactor: *"Alice reacted {love} to your post"*
   - 2 reactors: *"Alice and Bob reacted to your post"*
   - 3+ reactors: *"Alice, Bob, and 47 others reacted to your post"*
4. **Deduplication**: If a user changes their reaction (e.g., Like -> Love)
   within the debounce window, only the final state is included.
5. **Suppression**: `"remove"` actions cancel any pending un-dispatched
   notification for that reactor. If all reactors in a group remove their
   reactions before dispatch, no notification is sent.

---

## 5. Activity Feed Integration

There is no dedicated HTTP endpoint for activity feed integration. Instead,
the write path publishes a structured event to a Kafka topic that multiple
downstream services consume.

### 5.1 Kafka Topic: `reaction-events`

**Topic configuration**:
- Partitions: 256 (partitioned by `entityId` hash for ordering guarantees per entity)
- Retention: 7 days
- Replication factor: 3

**Event Schema (Avro)**

```json
{
  "userId": "user_12345678",
  "entityId": "post_98765432",
  "entityType": "post",
  "reactionType": "love",
  "previousReactionType": "like",
  "action": "change",
  "timestamp": "2024-01-15T08:30:00.000Z"
}
```

| Field | Type | Notes |
|-------|------|-------|
| `userId` | string | The user who reacted |
| `entityId` | string | Target entity |
| `entityType` | enum | `"post"`, `"comment"`, `"message"`, `"story"` |
| `reactionType` | string | Current reaction type. `null` if `action` is `"remove"`. |
| `previousReactionType` | string or `null` | Previous reaction type. `null` if `action` is `"add"`. |
| `action` | enum | `"add"` (new reaction), `"remove"` (deleted), `"change"` (type changed) |
| `timestamp` | ISO 8601 | Server-side event time |

**Consumers**:

| Consumer | Purpose |
|----------|---------|
| **Count Aggregator** | Increments/decrements pre-aggregated counters in TAO. On `"change"`, decrements old type and increments new type atomically. |
| **Notification Service** | Feeds into the coalescing pipeline described in Section 4. |
| **News Feed Ranker** | Reaction signals (especially on recent posts) influence feed ranking scores. A post accumulating reactions rapidly gets a boost. |
| **Analytics Pipeline** | Writes to the data warehouse (Hive/Spark) for offline analysis, A/B test metrics, and trending computation. |
| **Friend-Reaction Indexer** | Maintains the precomputed index that powers `topFriendReactors` in the summary endpoint. |
| **Search Indexer** | Updates reaction signals in the search relevance model. |

---

## 6. Analytics APIs (Internal)

Internal endpoints consumed by dashboards, data science tooling, and automated
alerting systems. Not latency-critical but must handle large time-range queries
efficiently.

### 6.1 Trending Posts by Reaction Velocity

```
GET /internal/analytics/reactions/trending?window=1h&limit=50&entityType=post
X-Internal-Auth: <service-token>
```

**Query Parameters**

| Param | Type | Required | Default | Notes |
|-------|------|----------|---------|-------|
| `window` | string | no | `"1h"` | Time window: `"5m"`, `"15m"`, `"1h"`, `"6h"`, `"24h"` |
| `limit` | integer | no | 50 | Number of results. Max 500. |
| `entityType` | enum | no | (all) | Filter by entity type |
| `region` | string | no | (global) | ISO country code for regional trending |

**Response — 200 OK**

```json
{
  "window": "1h",
  "computedAt": "2024-01-15T09:00:00.000Z",
  "trending": [
    {
      "entityId": "post_98765432",
      "entityType": "post",
      "reactionVelocity": 14520,
      "totalReactions": 283400,
      "dominantReactionType": "haha",
      "reactionDistribution": {
        "like": 0.35,
        "love": 0.12,
        "haha": 0.38,
        "wow": 0.08,
        "sad": 0.04,
        "angry": 0.02,
        "care": 0.01
      }
    }
  ]
}
```

| Field | Type | Notes |
|-------|------|-------|
| `reactionVelocity` | integer | Reactions per hour in the given window |
| `dominantReactionType` | string | Most common reaction type in the window |
| `reactionDistribution` | map<string, float> | Proportional breakdown (sums to 1.0) |

**Latency target**: < 500 ms p99

---

### 6.2 Reaction Sentiment Analysis

```
GET /internal/analytics/reactions/sentiment?entityId=post_98765432&window=24h
X-Internal-Auth: <service-token>
```

**Query Parameters**

| Param | Type | Required | Notes |
|-------|------|----------|-------|
| `entityId` | string | no | Specific entity. If omitted, aggregates over `topic`. |
| `topic` | string | no | Topic tag or hashtag for broad sentiment |
| `window` | string | no | Time window (default `"24h"`) |

**Response — 200 OK**

```json
{
  "entityId": "post_98765432",
  "window": "24h",
  "computedAt": "2024-01-15T09:00:00.000Z",
  "totalReactions": 283400,
  "sentimentScore": 0.72,
  "distribution": {
    "like": 98190,
    "love": 34008,
    "haha": 22672,
    "wow": 14170,
    "sad": 8502,
    "angry": 5668,
    "care": 190
  },
  "sentimentBreakdown": {
    "positive": 0.73,
    "neutral": 0.13,
    "negative": 0.14
  },
  "trend": "stable"
}
```

| Field | Type | Notes |
|-------|------|-------|
| `sentimentScore` | float | Range [-1, 1]. Computed as weighted average: Like/Love/Care = +1, Haha/Wow = 0, Sad/Angry = -1. |
| `sentimentBreakdown` | map | Positive (like, love, care), Neutral (haha, wow), Negative (sad, angry) as proportions. |
| `trend` | string | `"rising"`, `"falling"`, or `"stable"` compared to the previous equivalent window. |

**Latency target**: < 500 ms p99

---

## Platform Comparison: Reaction Models

Understanding how Facebook's reaction model differs from competitors clarifies
key design decisions around cardinality, storage, and aggregation.

| Dimension | Facebook | Twitter / X | Reddit | Instagram | Slack |
|-----------|----------|-------------|--------|-----------|-------|
| **Reaction types** | 7 fixed types (Like, Love, Haha, Wow, Sad, Angry, Care) | Single type (Like / heart) | 2 types (Upvote / Downvote) | Single type (Like / heart) | Arbitrary emoji (any Unicode emoji) |
| **Cardinality** | Bounded (7) | Bounded (1) | Bounded (2) | Bounded (1) | Unbounded |
| **Semantics** | Upsert: one reaction per user per entity | Boolean toggle: on/off | Boolean per direction: can upvote OR downvote | Boolean toggle: on/off | Additive: user can add multiple distinct emoji to same message |
| **Counter model** | 7 pre-aggregated counters per entity | 1 counter per tweet | 1 net score (upvotes - downvotes) | 1 counter per post (hidden in some regions) | 1 counter per emoji per message |
| **Storage shape** | Fixed-width — exactly 7 counter columns | Single counter column | Two counters (up, down) or net score | Single counter column | Variable-width — one counter per unique emoji used |
| **Schema evolution** | Requires migration when adding a type (e.g., Care in 2020) | N/A — single type | N/A — fixed two types | N/A — single type | No migration needed — new emoji appear dynamically |
| **Display complexity** | High — top-3 emoji icons, friend names, per-type counts | Low — single heart + count | Medium — net score + vote arrows | Low — heart + count (sometimes hidden) | Medium to high — emoji bar under message |
| **Write fan-out** | Moderate — update 1 of 7 counters + Kafka event | Minimal — toggle 1 counter | Minimal — toggle 1 of 2 counters | Minimal — toggle 1 counter | Higher — may create new counter column |

### Key Design Implications

**Facebook vs. Twitter/X**: Twitter's single-type model means the write path
is a simple increment/decrement and the read path returns one number. Facebook
must track *which* type the user selected, maintain 7 separate counters, and
compute the top-3 display types. This bounded-but-greater-than-one cardinality
is the core complexity driver.

**Facebook vs. Reddit**: Reddit's upvote/downvote model introduces a *net
score* abstraction and vote fuzzing for anti-manipulation. Facebook has no
concept of negative sentiment aggregation — each reaction type is counted
independently. Reddit also hides individual vote breakdowns publicly, while
Facebook exposes per-type counts.

**Facebook vs. Instagram**: Despite being under the same parent company (Meta),
Instagram deliberately chose a simpler like-only model. Instagram also
experimented with **hiding like counts** in certain regions (Canada 2019,
expanded globally as an option in 2021) — a UX decision that Facebook's richer
reaction model has not adopted.

**Facebook vs. Slack**: Slack represents the opposite end of the spectrum with
**unbounded reaction types** — any Unicode emoji can be used. This means
Slack's storage schema must be fully dynamic (a map of emoji -> count rather
than fixed columns), and aggregation cannot rely on a known enum. Facebook's
fixed enum of 7 types enables pre-allocated counter columns and simpler cache
invalidation strategies.

---

## Rate Limits

| Endpoint | Limit | Window | Notes |
|----------|-------|--------|-------|
| `POST /v1/reactions` | 60 | per minute per user | Prevents reaction spam / flip-flopping |
| `DELETE /v1/reactions` | 60 | per minute per user | Paired with POST limit |
| `GET .../summary` | 10,000 | per minute per user | Very high — called on every feed render |
| `GET .../reactions` | 300 | per minute per user | Paginated list views |

Rate limits return `429 Too Many Requests` with a `Retry-After` header
indicating seconds until the next permitted request.

---

## Idempotency

| Endpoint | Idempotent? | Mechanism |
|----------|-------------|-----------|
| `POST /v1/reactions` | Yes | Upsert on `(userId, entityId)`. Repeating the same call produces the same final state. |
| `DELETE /v1/reactions` | Yes | Deleting a non-existent reaction returns 204 without error. |
| `GET` endpoints | Yes (by definition) | Read-only. |

Clients should include an `X-Request-ID` header for request tracing and
deduplication in the write pipeline.

---

## Versioning

The API uses **URI-based versioning** (`/v1/`). Breaking changes (e.g., adding
a new required field, changing response shape) increment the version. Additive
changes (new optional fields, new reaction types) are backward-compatible and
do not require a version bump.

When a new reaction type is added (as happened with Care in 2020), the change
is backward-compatible: older clients that do not recognize the new type
simply ignore it in `counts` and `topReactionTypes`. The API contract is
designed so that the set of reaction types is an **open enum** in responses
even though it is a **closed enum** in requests (the server validates against
the currently supported set).
