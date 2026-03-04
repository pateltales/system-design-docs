# Twitter System Design — API Contracts

> Continuation of the interview simulation. The interviewer asked: "How's the API contract going to look like for the core functional requirements?"

---

## Base URL & Conventions

```
Base URL: https://api.twitter.com/v1
Content-Type: application/json
Authorization: Bearer <JWT token>
```

**Common conventions:**
- All endpoints require authentication via `Authorization` header (except signup/login).
- User identity is extracted from the JWT — no need to pass `user_id` in the request body for "acting as" operations.
- Pagination uses cursor-based pagination (not offset-based) — more efficient for real-time feeds where items are constantly being inserted.
- Rate limits are returned in response headers: `X-RateLimit-Limit`, `X-RateLimit-Remaining`, `X-RateLimit-Reset`.
- All timestamps are in ISO 8601 format (UTC).

---

## 1. Tweet Service APIs

### 1.1 Post a Tweet

```
POST /v1/tweets
```

**Request Headers:**
```
Authorization: Bearer <token>
Content-Type: application/json
Idempotency-Key: <client-generated-uuid>   // Prevents duplicate tweets on retry
```

**Request Body:**
```json
{
  "content": "Hello world! This is my first tweet 🐦",
  "media_ids": ["med_8a7f3b2c", "med_1d4e5f6a"],   // Optional. Pre-uploaded via media endpoint.
  "reply_to_tweet_id": "tw_1234567890",               // Optional. If this is a reply.
  "quote_tweet_id": "tw_9876543210"                    // Optional. If this is a quote tweet.
}
```

**Validation Rules:**
- `content`: Required (unless media_ids is non-empty). Max 280 characters.
- `media_ids`: Optional. Max 4 images or 1 video. Each must be a valid, previously uploaded media ID owned by the authenticated user.
- `reply_to_tweet_id`: Optional. Must reference an existing, non-deleted tweet.
- `Idempotency-Key`: Strongly recommended. Server deduplicates within a 24-hour window.

**Response — 201 Created:**
```json
{
  "data": {
    "tweet_id": "tw_7291038475610",
    "user_id": "usr_4820193847",
    "content": "Hello world! This is my first tweet 🐦",
    "media": [
      {
        "media_id": "med_8a7f3b2c",
        "type": "image",
        "url": "https://media.twitter.com/img/8a7f3b2c.jpg",
        "width": 1200,
        "height": 800
      }
    ],
    "reply_to_tweet_id": null,
    "quote_tweet_id": null,
    "created_at": "2026-06-02T22:05:30.000Z",
    "like_count": 0,
    "retweet_count": 0,
    "reply_count": 0,
    "is_liked_by_me": false,
    "is_retweeted_by_me": false
  }
}
```

**Error Responses:**

| Status | Code | Description |
|--------|------|-------------|
| 400 | `CONTENT_TOO_LONG` | Content exceeds 280 characters |
| 400 | `EMPTY_TWEET` | No content and no media provided |
| 400 | `INVALID_MEDIA_ID` | One or more media_ids are invalid or expired |
| 401 | `UNAUTHORIZED` | Missing or invalid auth token |
| 404 | `TWEET_NOT_FOUND` | reply_to_tweet_id references a non-existent tweet |
| 429 | `RATE_LIMIT_EXCEEDED` | Exceeded 300 tweets per 3-hour window |

**Error Response Format:**
```json
{
  "error": {
    "code": "CONTENT_TOO_LONG",
    "message": "Tweet content must not exceed 280 characters. Received: 312.",
    "details": {
      "max_length": 280,
      "actual_length": 312
    }
  }
}
```

---

### 1.2 Delete a Tweet

```
DELETE /v1/tweets/{tweet_id}
```

**Response — 200 OK:**
```json
{
  "data": {
    "tweet_id": "tw_7291038475610",
    "deleted": true
  }
}
```

**Notes:**
- Soft delete — marks `is_deleted = true` in the Tweet Store.
- Only the tweet owner can delete their tweet.
- Asynchronous cleanup: removes tweet from followers' timeline caches via a `TweetDeleted` event on Kafka.

| Status | Code | Description |
|--------|------|-------------|
| 403 | `FORBIDDEN` | Authenticated user is not the tweet owner |
| 404 | `TWEET_NOT_FOUND` | Tweet doesn't exist or already deleted |

---

### 1.3 Get a Tweet by ID

```
GET /v1/tweets/{tweet_id}
```

**Response — 200 OK:**
```json
{
  "data": {
    "tweet_id": "tw_7291038475610",
    "user": {
      "user_id": "usr_4820193847",
      "username": "johndoe",
      "display_name": "John Doe",
      "avatar_url": "https://media.twitter.com/avatar/4820193847.jpg",
      "is_verified": true
    },
    "content": "Hello world! This is my first tweet 🐦",
    "media": [],
    "reply_to_tweet_id": null,
    "quote_tweet": null,
    "created_at": "2026-06-02T22:05:30.000Z",
    "like_count": 1542,
    "retweet_count": 312,
    "reply_count": 87,
    "is_liked_by_me": true,
    "is_retweeted_by_me": false
  }
}
```

**Notes:**
- The `user` object is embedded (denormalized) to avoid a client-side join.
- `is_liked_by_me` and `is_retweeted_by_me` are personalized fields computed from the authenticated user's context.

---

### 1.4 Upload Media (Pre-signed Upload)

```
POST /v1/media/upload/init
```

**Request Body:**
```json
{
  "media_type": "image/jpeg",
  "file_size_bytes": 2048576,
  "filename": "photo.jpg"
}
```

**Response — 200 OK:**
```json
{
  "data": {
    "media_id": "med_8a7f3b2c",
    "upload_url": "https://upload.twitter.com/s3-presigned-url?...",
    "expires_at": "2026-06-02T22:35:30.000Z"
  }
}
```

**Flow:**
1. Client calls `POST /v1/media/upload/init` → gets a pre-signed S3 URL.
2. Client uploads the file directly to S3 via `PUT` to the `upload_url` (bypasses our app servers — offloads bandwidth).
3. Client receives the `media_id` and includes it in the tweet creation request.
4. Server-side: S3 triggers a Lambda to validate the upload (file type, size, content moderation), and marks the `media_id` as `ready`.

| Status | Code | Description |
|--------|------|-------------|
| 400 | `UNSUPPORTED_MEDIA_TYPE` | File type not in [image/jpeg, image/png, image/gif, video/mp4] |
| 400 | `FILE_TOO_LARGE` | Exceeds 5MB for images, 512MB for video |

---

## 2. Follow / Unfollow APIs

### 2.1 Follow a User

```
POST /v1/users/{target_user_id}/follow
```

**Request Body:** *(empty — the target is in the URL, the actor is in the JWT)*

**Response — 200 OK:**
```json
{
  "data": {
    "source_user_id": "usr_4820193847",
    "target_user_id": "usr_9182736450",
    "following": true,
    "followed_at": "2026-06-02T22:10:00.000Z"
  }
}
```

**Side Effects (async, via Kafka event `UserFollowed`):**
1. Increment `follower_count` for target user.
2. Increment `following_count` for source user.
3. Write to both `user_followers` and `user_following` tables in Cassandra.
4. If target is a non-celebrity, trigger a timeline backfill — inject the target's recent tweets into the source user's timeline cache.

| Status | Code | Description |
|--------|------|-------------|
| 400 | `CANNOT_FOLLOW_SELF` | User tried to follow themselves |
| 400 | `ALREADY_FOLLOWING` | Already following this user |
| 404 | `USER_NOT_FOUND` | Target user doesn't exist |
| 429 | `RATE_LIMIT_EXCEEDED` | Exceeded 400 follows per 24-hour window |

---

### 2.2 Unfollow a User

```
DELETE /v1/users/{target_user_id}/follow
```

**Response — 200 OK:**
```json
{
  "data": {
    "source_user_id": "usr_4820193847",
    "target_user_id": "usr_9182736450",
    "following": false
  }
}
```

**Side Effects (async):**
1. Decrement `follower_count` / `following_count`.
2. Remove from Cassandra follow tables.
3. Optionally: remove the unfollowed user's tweets from the source's timeline cache (not strictly necessary — they'll age out naturally, and new tweets won't be fanned out).

---

### 2.3 Get Followers of a User

```
GET /v1/users/{user_id}/followers?cursor={cursor}&limit={limit}
```

**Query Parameters:**

| Param | Type | Default | Description |
|-------|------|---------|-------------|
| `cursor` | string | `null` | Opaque cursor from previous response. Omit for first page. |
| `limit` | int | 20 | Number of results per page. Max 200. |

**Response — 200 OK:**
```json
{
  "data": [
    {
      "user_id": "usr_1111111111",
      "username": "alice",
      "display_name": "Alice",
      "avatar_url": "https://media.twitter.com/avatar/1111111111.jpg",
      "is_verified": false,
      "is_followed_by_me": true,
      "followed_at": "2026-05-20T10:30:00.000Z"
    },
    {
      "user_id": "usr_2222222222",
      "username": "bob",
      "display_name": "Bob",
      "avatar_url": "https://media.twitter.com/avatar/2222222222.jpg",
      "is_verified": true,
      "is_followed_by_me": false,
      "followed_at": "2026-05-18T14:15:00.000Z"
    }
  ],
  "pagination": {
    "next_cursor": "eyJ1c2VyX2lkIjoiMjIyMjIiLCJ0cyI6MTcxNjAzNTcwMH0=",
    "has_more": true
  }
}
```

**Why cursor-based pagination?**
- Offset-based (`?page=5&limit=20`) breaks when new follows are added/removed between pages — you get duplicates or skipped results.
- The cursor encodes the last seen `(follower_id, followed_at)` — the query resumes from exactly where it left off, regardless of insertions/deletions.
- In Cassandra, this maps directly to a `WHERE followee_id = ? AND follower_id > ? LIMIT 20` query on the clustering key.

---

### 2.4 Get Following (users that a user follows)

```
GET /v1/users/{user_id}/following?cursor={cursor}&limit={limit}
```

*(Same response shape as followers, but reads from the `user_following` Cassandra table.)*

---

## 3. Home Timeline (News Feed) API

### 3.1 Get Home Timeline

```
GET /v1/timeline/home?cursor={cursor}&limit={limit}
```

**Query Parameters:**

| Param | Type | Default | Description |
|-------|------|---------|-------------|
| `cursor` | string | `null` | Opaque cursor for pagination. Encodes the last tweet's `(tweet_id, created_at)`. |
| `limit` | int | 50 | Number of tweets per page. Max 200. |

**Response — 200 OK:**
```json
{
  "data": [
    {
      "tweet_id": "tw_7291038475610",
      "user": {
        "user_id": "usr_9182736450",
        "username": "janedoe",
        "display_name": "Jane Doe",
        "avatar_url": "https://media.twitter.com/avatar/9182736450.jpg",
        "is_verified": true
      },
      "content": "Just shipped a new feature! 🚀",
      "media": [],
      "reply_to_tweet_id": null,
      "quote_tweet": null,
      "created_at": "2026-06-02T22:00:00.000Z",
      "like_count": 234,
      "retweet_count": 45,
      "reply_count": 12,
      "is_liked_by_me": false,
      "is_retweeted_by_me": false
    },
    {
      "tweet_id": "tw_7291038475590",
      "user": {
        "user_id": "usr_5555555555",
        "username": "elonmusk",
        "display_name": "Elon Musk",
        "avatar_url": "https://media.twitter.com/avatar/5555555555.jpg",
        "is_verified": true
      },
      "content": "The future is now 🌍",
      "media": [
        {
          "media_id": "med_abc123",
          "type": "image",
          "url": "https://media.twitter.com/img/abc123.jpg",
          "width": 1920,
          "height": 1080
        }
      ],
      "reply_to_tweet_id": null,
      "quote_tweet": null,
      "created_at": "2026-06-02T21:55:00.000Z",
      "like_count": 150432,
      "retweet_count": 32100,
      "reply_count": 8721,
      "is_liked_by_me": true,
      "is_retweeted_by_me": false
    }
  ],
  "pagination": {
    "next_cursor": "eyJ0d2VldF9pZCI6Inr3MjkxMDM4NDc1NTkwIiwidHMiOjE3MTcz...",
    "has_more": true
  }
}
```

**Backend Flow (what the Timeline Service does):**

```
┌──────────┐     ┌────────────────────┐     ┌──────────────┐
│  Client   │────▶│  Timeline Service   │────▶│ Redis Cache  │
│           │     │                    │     │ (ZREVRANGE)  │
└──────────┘     │                    │     └──────┬───────┘
                 │                    │            │ tweet_ids
                 │                    │     ┌──────▼───────┐
                 │  Merge + Sort +    │◀────│ Tweet Cache   │
                 │  Hydrate           │     │ (multi-get)  │
                 │                    │     └──────────────┘
                 │                    │
                 │  If user follows   │     ┌──────────────┐
                 │  celebrities ──────│────▶│ Tweet Store   │
                 │  (fanout-on-read)  │     │ (celebrity    │
                 │                    │     │  tweets)      │
                 └────────────────────┘     └──────────────┘
```

1. Read `tweet_ids` from `ZREVRANGEBYSCORE timeline:{user_id}` in Redis.
2. If the user follows celebrity accounts → also fetch latest tweets from those users directly from Tweet Store.
3. Merge the two lists by `created_at` (or ranking score).
4. Hydrate `tweet_ids` → full tweet objects via batch lookup from Tweet Cache (Redis/Memcached) or Tweet Store.
5. Hydrate user objects for each tweet author (from User Cache).
6. Compute personalized fields (`is_liked_by_me`, `is_retweeted_by_me`) by checking the user's like/retweet sets.
7. Return paginated response.

**Cache Miss Flow (cold user):**
- If `timeline:{user_id}` key doesn't exist in Redis → fall back to fanout-on-read.
- Fetch following list → for each followed user, get their recent tweets → merge → return.
- Async backfill the cache.

---

### 3.2 New Tweets Indicator (Polling / Long-Poll)

```
GET /v1/timeline/home/updates?since_tweet_id={tweet_id}
```

**Response — 200 OK:**
```json
{
  "data": {
    "new_tweet_count": 14,
    "latest_tweet_id": "tw_7291038475700"
  }
}
```

**Notes:**
- Lightweight endpoint for the "14 new tweets" banner.
- Simply does `ZCOUNT timeline:{user_id} (last_seen_score +inf` on Redis — O(log N), very fast.
- For real-time push: use WebSockets or Server-Sent Events (SSE) instead of polling. The Timeline Service pushes a notification to a connected client's WebSocket when new tweets are fanned out to their timeline.

---

## 4. User Timeline API

### 4.1 Get User Timeline (all tweets by a specific user)

```
GET /v1/users/{user_id}/tweets?cursor={cursor}&limit={limit}
```

**Query Parameters:**

| Param | Type | Default | Description |
|-------|------|---------|-------------|
| `cursor` | string | `null` | Opaque cursor (encodes `tweet_id`). |
| `limit` | int | 20 | Tweets per page. Max 200. |
| `include_replies` | boolean | `false` | Whether to include replies in the timeline. |

**Response — 200 OK:**
```json
{
  "data": [
    {
      "tweet_id": "tw_7291038475610",
      "user": {
        "user_id": "usr_4820193847",
        "username": "johndoe",
        "display_name": "John Doe",
        "avatar_url": "https://media.twitter.com/avatar/4820193847.jpg",
        "is_verified": false
      },
      "content": "Working on something exciting...",
      "media": [],
      "reply_to_tweet_id": null,
      "quote_tweet": null,
      "created_at": "2026-06-02T20:00:00.000Z",
      "like_count": 23,
      "retweet_count": 2,
      "reply_count": 5,
      "is_liked_by_me": false,
      "is_retweeted_by_me": false
    }
  ],
  "pagination": {
    "next_cursor": "eyJ0d2VldF9pZCI6Inr3MjkxMDM4NDc1NjAwIn0=",
    "has_more": true
  }
}
```

**Backend Flow:**
- This is a **single-shard query** since we shard the Tweet Store by `user_id`.
- Query: `SELECT * FROM tweets WHERE user_id = ? AND is_deleted = false ORDER BY created_at DESC LIMIT ?`
- If `include_replies = false`, add filter: `AND reply_to_id IS NULL`
- This endpoint does NOT use the Redis timeline cache — it goes directly to the Tweet Store (with a read replica for performance).
- Results are cached at the application level (short TTL, ~30 seconds) to handle repeated profile views.

---

## API Summary Table

| # | Method | Endpoint | Description | Auth |
|---|--------|----------|-------------|------|
| 1 | `POST` | `/v1/tweets` | Post a new tweet | ✅ |
| 2 | `DELETE` | `/v1/tweets/{tweet_id}` | Delete a tweet | ✅ |
| 3 | `GET` | `/v1/tweets/{tweet_id}` | Get a single tweet | ✅ |
| 4 | `POST` | `/v1/media/upload/init` | Initialize media upload | ✅ |
| 5 | `POST` | `/v1/users/{user_id}/follow` | Follow a user | ✅ |
| 6 | `DELETE` | `/v1/users/{user_id}/follow` | Unfollow a user | ✅ |
| 7 | `GET` | `/v1/users/{user_id}/followers` | List followers | ✅ |
| 8 | `GET` | `/v1/users/{user_id}/following` | List following | ✅ |
| 9 | `GET` | `/v1/timeline/home` | Get home timeline (feed) | ✅ |
| 10 | `GET` | `/v1/timeline/home/updates` | Check for new tweets | ✅ |
| 11 | `GET` | `/v1/users/{user_id}/tweets` | Get user's tweets | ✅ |

---

## Design Decisions in the API

### Why RESTful over GraphQL?
- Twitter's read patterns are well-defined and predictable — timeline, user tweets, single tweet. REST maps cleanly.
- REST is simpler to cache at the CDN/edge layer (GET requests are trivially cacheable by URL).
- GraphQL would make sense if clients needed flexible field selection (mobile wanting fewer fields than web), but we handle this with optional query params like `fields=user,media` if needed.

### Why cursor-based pagination over offset-based?
- **Real-time data**: New tweets are constantly being inserted. With offset pagination (`page=3`), inserting a new tweet shifts everything — page 3 now shows items that were on page 2.
- **Performance**: Cursor pagination translates to `WHERE tweet_id < ? ORDER BY tweet_id DESC LIMIT N` — uses the primary key index directly. Offset pagination requires `OFFSET N` which scans and discards rows.
- **Consistency**: The cursor is a stable pointer — even if data changes, you resume exactly where you left off.

### Why Idempotency-Key on POST /tweets?
- Network failures happen. A client may retry a tweet POST without knowing if the first request succeeded.
- Without idempotency: the user sees their tweet posted twice.
- With `Idempotency-Key`: the server checks a Redis key `idempotency:{key}` — if it exists, return the cached response. If not, process the request and store the response with a 24-hour TTL.

### Why pre-signed URLs for media upload?
- Uploading large files (images, videos) through our API servers is wasteful — it consumes bandwidth and CPU on application servers that should be doing business logic.
- Pre-signed S3 URLs let the client upload directly to object storage, bypassing our servers entirely.
- The `media_id` is then referenced in the tweet creation — a lightweight JSON call.

---

*This API contract document complements the [interview simulation](../interview-simulation.md) with the detailed endpoint specifications.*