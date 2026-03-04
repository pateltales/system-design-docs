# Instagram Platform API Contracts

> Comprehensive reference for all Instagram platform APIs.
> Endpoints marked with &#11088; are those most likely to come up in a system design interview.

---

## Table of Contents

1. [Post APIs](#1-post-apis)
2. [Feed APIs](#2-feed-apis)
3. [Stories APIs](#3-stories-apis)
4. [Reels APIs](#4-reels-apis)
5. [Social Graph APIs](#5-social-graph-apis)
6. [Engagement APIs](#6-engagement-apis)
7. [Search & Explore APIs](#7-search--explore-apis)
8. [Direct Messaging APIs](#8-direct-messaging-apis)
9. [Notification APIs](#9-notification-apis)
10. [User Profile APIs](#10-user-profile-apis)
11. [Media Upload APIs (Internal)](#11-media-upload-apis-internal)
12. [Content Moderation APIs (Internal)](#12-content-moderation-apis-internal)
13. [Instagram vs Twitter vs TikTok: API Model Comparison](#13-instagram-vs-twitter-vs-tiktok-api-model-comparison)

---

## 1. Post APIs

> The core content creation path. Every Instagram post starts here.

### POST /posts &#11088;

Creates a new post (photo, video, or carousel). Media must be uploaded separately via the Media Upload API and referenced by media IDs. This two-phase approach (upload media → create post) allows for resumable uploads and async processing.

**Request:**

```json
{
  "mediaIds": ["media-uuid-1"],
  "mediaType": "PHOTO",
  "caption": "Sunset at the beach #travel #photography",
  "location": {
    "id": "loc-123",
    "name": "Santa Monica Beach",
    "lat": 34.0195,
    "lng": -118.4912
  },
  "userTags": [
    { "userId": "user-456", "position": { "x": 0.45, "y": 0.62 } }
  ],
  "altText": "A golden sunset over the Pacific Ocean with waves crashing on the shore",
  "disableComments": false,
  "shareToStory": false
}
```

**Response:**

```json
{
  "postId": "post-uuid-abc",
  "status": "PUBLISHED",
  "createdAt": "2025-01-15T18:30:00Z",
  "permalink": "https://instagram.com/p/abc123",
  "media": [
    {
      "mediaId": "media-uuid-1",
      "type": "PHOTO",
      "urls": {
        "thumbnail": "https://cdn.instagram.com/t/150x150/abc.jpg",
        "small": "https://cdn.instagram.com/s/320/abc.jpg",
        "medium": "https://cdn.instagram.com/m/640/abc.jpg",
        "large": "https://cdn.instagram.com/l/1080/abc.jpg"
      },
      "dimensions": { "width": 1080, "height": 1350 },
      "blurhash": "LGF5]+Yk^6#M@-5c,1J5@[or[Q6."
    }
  ]
}
```

**Post Creation Flow:**

```
Client App                  API Gateway              Media Service           Feed Service
  |                             |                         |                      |
  |-- POST /media/upload ------>|                         |                      |
  |                             |-- store raw media ----->|                      |
  |                             |                         |-- resize/compress -->|
  |                             |                         |-- generate blurhash ->|
  |                             |                         |-- upload to CDN ---->|
  |<-- mediaId, status ---------|<-- processing done -----|                      |
  |                             |                         |                      |
  |-- POST /posts ------------->|                         |                      |
  |                             |-- validate media IDs -->|                      |
  |                             |-- store post metadata ->|                      |
  |                             |-- trigger fan-out -------------------------------->|
  |<-- postId, permalink -------|                         |                      |
  |                             |                         |       [async fan-out to
  |                             |                         |        followers' feeds]
```

**Why two-phase (upload then create)?**
- Media processing (resize, compress, transcode) is CPU-intensive and takes 1-10 seconds
- Users see a progress bar during upload, then instant publishing on "Share"
- If upload fails mid-way, resumable upload avoids re-uploading from scratch
- Media can be reused (e.g., same photo shared to Feed and Story)

### POST /posts (Carousel) &#11088;

Carousel posts support up to 10 photos/videos in a single swipeable post.

**Request:**

```json
{
  "mediaIds": ["media-uuid-1", "media-uuid-2", "media-uuid-3"],
  "mediaType": "CAROUSEL",
  "caption": "Trip highlights from Japan",
  "location": { "id": "loc-789", "name": "Tokyo, Japan" },
  "userTags": [],
  "altText": null
}
```

**Response:** Same structure as single-post, but `media` array contains multiple items.

### GET /posts/{postId}

Returns full post details including engagement counts, author info, and media URLs.

**Response:**

```json
{
  "postId": "post-uuid-abc",
  "author": {
    "userId": "user-123",
    "username": "travelphotographer",
    "displayName": "Travel Photographer",
    "avatarUrl": "https://cdn.instagram.com/avatar/user-123.jpg",
    "isVerified": true
  },
  "media": [ ... ],
  "caption": "Sunset at the beach #travel #photography",
  "location": { "id": "loc-123", "name": "Santa Monica Beach" },
  "engagement": {
    "likeCount": 12453,
    "commentCount": 342,
    "shareCount": 89,
    "saveCount": 1205,
    "viewCount": 45000
  },
  "viewer": {
    "hasLiked": false,
    "hasSaved": true,
    "hasCommented": false
  },
  "createdAt": "2025-01-15T18:30:00Z",
  "tags": ["travel", "photography"],
  "userTags": [ ... ]
}
```

**Note on engagement counts:** Like counts are eventually consistent — during high traffic (viral posts), they may lag by a few seconds. Instagram uses approximate counters during spikes for performance. The exact count converges within seconds. No user can perceive a 0.1% error on a post with 100K likes.

### PUT /posts/{postId}

Edit an existing post's caption, location, or user tags. Media cannot be changed after publishing.

**Request:**

```json
{
  "caption": "Updated caption #travel",
  "location": null,
  "userTags": []
}
```

### DELETE /posts/{postId}

Deletes a post and all associated media. Triggers reverse fan-out — the post must be removed from every follower's feed inbox it was pushed to. This is the expensive inverse of the fan-out on write.

---

## 2. Feed APIs

> The heart of Instagram. Feed generation is the most architecturally interesting component.

### GET /feed &#11088;

Returns the user's personalized home feed. This is the most frequently called API on Instagram — billions of requests per day.

**Request (Query Parameters):**

```
GET /feed?cursor={opaqueCursor}&limit=20
```

**Response:**

```json
{
  "posts": [
    {
      "postId": "post-uuid-xyz",
      "author": {
        "userId": "user-456",
        "username": "chef_maria",
        "avatarUrl": "https://cdn.instagram.com/avatar/user-456.jpg",
        "isVerified": false
      },
      "media": [
        {
          "type": "PHOTO",
          "urls": {
            "thumbnail": "https://cdn.instagram.com/t/150x150/xyz.jpg",
            "medium": "https://cdn.instagram.com/m/640/xyz.jpg",
            "large": "https://cdn.instagram.com/l/1080/xyz.jpg"
          },
          "blurhash": "LEHV6nWB2yk8pyo0adR*.7kCMdnj",
          "dimensions": { "width": 1080, "height": 1080 }
        }
      ],
      "caption": "Homemade pasta from scratch",
      "engagement": {
        "likeCount": 892,
        "commentCount": 45,
        "viewCount": null
      },
      "viewer": { "hasLiked": true, "hasSaved": false },
      "createdAt": "2025-01-15T14:20:00Z",
      "rankingScore": 0.94,
      "rankingReason": "CLOSE_FRIEND"
    },
    ...
  ],
  "cursor": "eyJsYXN0UG9zdElkIjoicG9zdC11dWlkLXh5eiIsInNjb3JlIjowLjk0fQ==",
  "hasMore": true,
  "injectedContent": [
    {
      "type": "SUGGESTED_POST",
      "position": 5,
      "postId": "post-suggested-1",
      "reason": "Based on posts you've liked"
    }
  ]
}
```

**Why cursor-based pagination (NOT offset-based)?**
- Offset-based: `GET /feed?offset=20&limit=20`. If new posts are inserted between page loads, items shift — user sees duplicates or misses posts.
- Cursor-based: The cursor encodes the last seen post's ranking score + timestamp. New posts don't affect pagination position.
- At Instagram's scale (billions of feed loads/day), offset-based pagination would cause millions of duplicate-post impressions daily — bad user experience and wasted bandwidth.

**Feed assembly — the two code paths:**

```
                    ┌──────────────────────┐
                    │   GET /feed request  │
                    └──────────┬───────────┘
                               │
                    ┌──────────▼───────────┐
                    │  Fetch feed inbox    │  ← Pre-materialized via fan-out on write
                    │  (Redis sorted set)  │    for users with <500K followers
                    └──────────┬───────────┘
                               │
                    ┌──────────▼───────────┐
                    │  Fetch celebrity     │  ← Fan-out on read: pull latest posts
                    │  posts at read time  │    from high-follower accounts the
                    │                      │    user follows
                    └──────────┬───────────┘
                               │
                    ┌──────────▼───────────┐
                    │  Merge + Rank (ML)   │  ← Score ~500 candidates, return top 20
                    │  by predicted interest│
                    └──────────┬───────────┘
                               │
                    ┌──────────▼───────────┐
                    │  Inject suggested    │  ← Mix in recommended posts from
                    │  posts + ads         │    accounts user doesn't follow
                    └──────────┬───────────┘
                               │
                    ┌──────────▼───────────┐
                    │  Return ranked feed  │
                    └──────────────────────┘
```

### GET /feed/following

Chronological feed showing only posts from accounts the user follows. Added in 2022 after user backlash against purely algorithmic feed. No ranking — just reverse-chronological order.

**Response:** Same structure as `/feed` but `rankingScore` is always the timestamp and `rankingReason` is always `"CHRONOLOGICAL"`.

---

## 3. Stories APIs

> Ephemeral content with a 24-hour TTL. Different storage and delivery model from feed posts.

### POST /stories &#11088;

Creates a new Story (photo or video clip, max 15 seconds per clip).

**Request:**

```json
{
  "mediaId": "media-uuid-story-1",
  "mediaType": "PHOTO",
  "stickers": [
    { "type": "POLL", "question": "Beach or mountains?", "options": ["Beach", "Mountains"], "position": { "x": 0.5, "y": 0.7 } },
    { "type": "MUSIC", "trackId": "track-123", "startMs": 15000, "durationMs": 15000 },
    { "type": "MENTION", "userId": "user-789", "position": { "x": 0.3, "y": 0.4 } }
  ],
  "closeFriendsOnly": false,
  "allowReplies": "EVERYONE"
}
```

**Response:**

```json
{
  "storyId": "story-uuid-def",
  "expiresAt": "2025-01-16T18:30:00Z",
  "media": {
    "type": "PHOTO",
    "url": "https://cdn.instagram.com/story/story-uuid-def.jpg",
    "dimensions": { "width": 1080, "height": 1920 }
  },
  "stickers": [ ... ],
  "viewCount": 0
}
```

**TTL implications:**
- Story media is stored with a 24-hour TTL. After expiration:
  - Metadata record is deleted from Cassandra (TTL column)
  - Media files are deleted from CDN and blob storage via lifecycle policy
  - Seen-state records for this story are garbage collected
- Exception: if the user adds the Story to **Highlights**, the media is moved to permanent storage before TTL expires

### GET /stories/feed &#11088;

Returns the Stories tray — the row of circles at the top of the home screen. Ordered by recency and relationship closeness.

**Response:**

```json
{
  "tray": [
    {
      "userId": "user-456",
      "username": "chef_maria",
      "avatarUrl": "https://cdn.instagram.com/avatar/user-456.jpg",
      "hasUnseenStories": true,
      "latestStoryTimestamp": "2025-01-15T17:45:00Z",
      "storyCount": 3,
      "previewMedia": {
        "type": "PHOTO",
        "thumbnailUrl": "https://cdn.instagram.com/story-thumb/xyz.jpg"
      }
    },
    ...
  ],
  "cursor": "...",
  "hasMore": true
}
```

**Why is the Stories tray personalized?**
- A user might follow 500 accounts, 50 of which have active Stories. Showing all 50 in chronological order wastes tray real estate.
- Instagram ranks the tray by: recency x relationship closeness (how often you view their Stories, how often you DM them, whether they're a Close Friend).
- The first 5-7 items in the tray are the most important — most users only swipe through the first few.

### GET /stories/{userId}

Returns all active (non-expired) Stories for a specific user.

**Response:**

```json
{
  "userId": "user-456",
  "stories": [
    {
      "storyId": "story-uuid-1",
      "media": { "type": "PHOTO", "url": "...", "dimensions": { "width": 1080, "height": 1920 } },
      "stickers": [ ... ],
      "createdAt": "2025-01-15T14:00:00Z",
      "expiresAt": "2025-01-16T14:00:00Z",
      "viewCount": 234,
      "isSeen": false
    },
    ...
  ]
}
```

### DELETE /stories/{storyId}

Deletes a Story before its natural expiration. Immediate removal from CDN + database.

---

## 4. Reels APIs

> Short-form video with recommendation-driven distribution. Fundamentally different from the social-graph-based feed.

### POST /reels &#11088;

Creates a new Reel (short-form video, up to 90 seconds).

**Request:**

```json
{
  "mediaId": "media-uuid-reel-1",
  "caption": "How to make perfect espresso #coffee #barista",
  "audioTrackId": "audio-trending-456",
  "audioStartMs": 5000,
  "effects": ["SLOW_MO", "TIMER"],
  "coverFrameMs": 3500,
  "location": null,
  "userTags": [],
  "shareToFeed": true
}
```

**Response:**

```json
{
  "reelId": "reel-uuid-ghi",
  "status": "PROCESSING",
  "createdAt": "2025-01-15T19:00:00Z",
  "media": {
    "type": "VIDEO",
    "duration": 30000,
    "processingStatus": "TRANSCODING",
    "estimatedReadyMs": 15000
  }
}
```

**Why `status: PROCESSING`?**
- Reels require video transcoding (multiple resolutions + HLS segments), audio extraction, thumbnail generation, and content moderation ML scan
- This takes 5-30 seconds depending on video length
- The Reel is created with status `PROCESSING` → transitions to `PUBLISHED` when pipeline completes
- The client polls or receives a push notification when ready
- Content moderation runs in parallel with transcoding — if flagged, the Reel is held for review before publishing

### GET /reels/feed &#11088;

Returns an endless stream of recommended Reels. This is the TikTok-style feed — content from accounts the user does NOT follow, ranked by predicted engagement.

**Request:**

```
GET /reels/feed?cursor={opaqueCursor}&limit=5
```

**Response:**

```json
{
  "reels": [
    {
      "reelId": "reel-uuid-xyz",
      "author": {
        "userId": "user-unknown-1",
        "username": "coffee_guru",
        "avatarUrl": "...",
        "followerCount": 45000,
        "isFollowed": false
      },
      "media": {
        "type": "VIDEO",
        "duration": 22000,
        "hlsUrl": "https://cdn.instagram.com/reel/xyz/master.m3u8",
        "thumbnailUrl": "https://cdn.instagram.com/reel/xyz/thumb.jpg",
        "dimensions": { "width": 1080, "height": 1920 }
      },
      "audio": {
        "trackId": "audio-trending-456",
        "title": "Morning Coffee",
        "artist": "LoFi Beats",
        "isOriginal": false
      },
      "caption": "Perfect latte art every time",
      "engagement": {
        "likeCount": 89000,
        "commentCount": 1200,
        "shareCount": 5600,
        "playCount": 1200000
      },
      "viewer": { "hasLiked": false, "hasSaved": false }
    },
    ...
  ],
  "cursor": "...",
  "hasMore": true
}
```

**Key difference from /feed:**
- `/feed` is social-graph-based: you see posts from accounts you follow
- `/reels/feed` is recommendation-based: you see Reels from ANYONE, ranked by predicted interest
- Different infrastructure: Feed uses fan-out from social graph. Reels uses a recommendation engine that indexes all public Reels content.

**Prefetching strategy:**
- Client prefetches the next 2-3 Reels while user watches the current one
- HLS segments for the next Reel start downloading in the background
- This eliminates buffering delay when user swipes to the next Reel
- Trade-off: wastes bandwidth if user exits before reaching prefetched Reels (~20-30% waste rate is acceptable)

### GET /reels/{reelId}

Returns full details for a specific Reel (used for deep links, shares).

---

## 5. Social Graph APIs

> The directed follow graph. Every follow/unfollow has fan-out implications.

### POST /users/{userId}/follow &#11088;

Follow a user. This is a graph edge creation with downstream side effects.

**Request:**

```json
{
  "source": "PROFILE_PAGE"
}
```

**Response:**

```json
{
  "status": "FOLLOWING",
  "followedAt": "2025-01-15T19:15:00Z"
}
```

**For private accounts:**

```json
{
  "status": "REQUESTED",
  "requestedAt": "2025-01-15T19:15:00Z"
}
```

**What happens on follow (side effects):**

```
POST /users/{userId}/follow
        |
        +-- 1. Write edge to social graph (TAO/Cassandra)
        |       follower:user-A -> followee:user-B
        |       following:user-B <- follower:user-A
        |
        +-- 2. Update follower/following counts (async, approximate)
        |
        +-- 3. Backfill feed inbox (async)
        |       Fetch user-B's recent posts -> write to user-A's feed inbox
        |       [INFERRED -- not officially documented]
        |
        +-- 4. Send notification to user-B (async)
        |       "user-A started following you"
        |
        +-- 5. Update recommendation features (async)
                User-A's social graph changed -> recalculate feed ranking weights
```

### DELETE /users/{userId}/follow

Unfollow a user. Reverse of follow — must clean up the feed inbox.

**Side effects:**
1. Delete edge from social graph
2. Update counts (async)
3. Remove user-B's posts from user-A's feed inbox (async — this is expensive reverse fan-out)
4. No notification sent (unfollows are silent)

### GET /users/{userId}/followers

Paginated follower list. For users with millions of followers, this is a large dataset.

**Request:**

```
GET /users/{userId}/followers?cursor={cursor}&limit=50
```

**Response:**

```json
{
  "followers": [
    {
      "userId": "user-789",
      "username": "photo_lover",
      "displayName": "Photo Lover",
      "avatarUrl": "...",
      "isFollowed": true,
      "isMutual": true
    },
    ...
  ],
  "totalCount": 12500,
  "cursor": "...",
  "hasMore": true
}
```

**Scale concern:** Cristiano Ronaldo has 650M+ followers. Paginating through 650M entries is infeasible in practice — the UI shows the first few pages and `totalCount` as the displayed number. The count itself is an approximate counter updated asynchronously.

### GET /users/{userId}/following

Same structure as followers, but for the accounts this user follows. Max 7,500 (Instagram's following limit).

### GET /users/{userId}/mutual-followers

Returns followers who are also followed by the requesting user. Requires set intersection.

**Response:**

```json
{
  "mutualFollowers": [
    { "userId": "user-111", "username": "common_friend", "avatarUrl": "..." },
    ...
  ],
  "totalMutualCount": 23
}
```

**How is this computed?** Intersection of two follower sets. Options:
1. **At read time:** Fetch both follower sets from cache, intersect in-memory. Fast for small sets, expensive for large ones.
2. **Precomputed:** For frequently accessed pairs, precompute mutual followers and cache the result. [INFERRED — not officially documented]

### POST /users/{userId}/block

Block a user. Removes bidirectional edges and prevents future interactions.

### POST /users/{userId}/restrict

Restrict a user. Softer than block — their comments are hidden from others but visible to them (they don't know they're restricted).

---

## 6. Engagement APIs

> Likes, comments, saves, shares — the social signals that drive feed ranking.

### POST /posts/{postId}/like &#11088;

Like a post. The most frequent write operation on Instagram (~4.2 billion likes per day estimated).

**Response:**

```json
{
  "liked": true,
  "newLikeCount": 12454
}
```

**Scale challenge:** A post from a celebrity can receive millions of likes per minute. Writing each like to a counter in real-time would create a write hotspot. Solution:
- Likes are buffered in-memory (Redis) and flushed to persistent storage in batches
- The count returned to the user is approximate (eventual consistency)
- Individual like records (who liked what) are written asynchronously to a log
- The notification to the post author is aggregated: "user1, user2, and 998 others liked your post"

### DELETE /posts/{postId}/like

Unlike a post. Decrements the counter (eventually consistent).

### POST /posts/{postId}/comments &#11088;

Add a comment to a post. Supports threaded replies (reply to a comment).

**Request:**

```json
{
  "text": "This is amazing! Where was this taken?",
  "replyToCommentId": null,
  "mentionedUserIds": []
}
```

**Response:**

```json
{
  "commentId": "comment-uuid-123",
  "text": "This is amazing! Where was this taken?",
  "author": { "userId": "user-456", "username": "traveler" },
  "createdAt": "2025-01-15T19:30:00Z",
  "likeCount": 0,
  "replyCount": 0,
  "replyToCommentId": null
}
```

### GET /posts/{postId}/comments

Paginated, threaded comment list. Top-level comments are ranked by engagement; replies are chronological.

**Response:**

```json
{
  "comments": [
    {
      "commentId": "comment-uuid-100",
      "text": "Love this shot!",
      "author": { ... },
      "createdAt": "...",
      "likeCount": 45,
      "replyCount": 3,
      "replies": [
        {
          "commentId": "comment-uuid-101",
          "text": "Thank you!",
          "author": { ... },
          "replyToCommentId": "comment-uuid-100"
        }
      ]
    },
    ...
  ],
  "totalCount": 342,
  "cursor": "...",
  "hasMore": true
}
```

### POST /posts/{postId}/save

Save/bookmark a post to a private collection. Saved posts persist indefinitely.

### DELETE /posts/{postId}/save

Remove a saved post.

### POST /posts/{postId}/share

Share a post via DM or to Stories. Increments share count.

**Request:**

```json
{
  "shareType": "DM",
  "recipientUserIds": ["user-789", "user-012"],
  "message": "Check this out!"
}
```

---

## 7. Search & Explore APIs

> Discovery beyond the social graph.

### GET /search &#11088;

Search for users, hashtags, or locations.

**Request:**

```
GET /search?q=tokyo&type=PLACES&cursor={cursor}&limit=20
```

**Response (type=USERS):**

```json
{
  "results": [
    {
      "type": "USER",
      "userId": "user-tokyo",
      "username": "tokyo_explorer",
      "displayName": "Tokyo Explorer",
      "avatarUrl": "...",
      "followerCount": 150000,
      "isVerified": true,
      "isFollowed": false,
      "mutualFollowerCount": 3
    },
    ...
  ],
  "cursor": "...",
  "hasMore": true
}
```

**Response (type=TAGS):**

```json
{
  "results": [
    {
      "type": "HASHTAG",
      "tag": "tokyo",
      "postCount": 45000000
    },
    ...
  ]
}
```

**Personalization in search:** Same query, different ranking per user. If you frequently interact with food accounts, "tokyo" returns food bloggers in Tokyo before travel photographers. Search results are personalized based on interaction history.

### GET /search/suggestions

Typeahead autocomplete. Must return results in <100ms.

**Request:**

```
GET /search/suggestions?q=tok
```

**Response:**

```json
{
  "suggestions": [
    { "type": "USER", "username": "tokyo_explorer", "avatarUrl": "..." },
    { "type": "HASHTAG", "tag": "tokyo", "postCount": 45000000 },
    { "type": "HASHTAG", "tag": "tokyofood", "postCount": 2300000 },
    { "type": "PLACE", "name": "Tokyo, Japan", "locationId": "loc-tokyo" }
  ]
}
```

**Why <100ms?** Typeahead runs on every keystroke. At 200ms latency, suggestions feel laggy and users stop using search. This requires an in-memory prefix index (trie or inverted index) at the edge, not a database query per keystroke.

### GET /explore &#11088;

The Explore page — a grid of recommended posts/Reels from accounts the user doesn't follow.

**Request:**

```
GET /explore?cursor={cursor}&limit=30
```

**Response:**

```json
{
  "items": [
    {
      "type": "POST",
      "postId": "post-explore-1",
      "author": { "userId": "user-unknown", "username": "nature_photo", "isFollowed": false },
      "media": {
        "type": "PHOTO",
        "thumbnailUrl": "https://cdn.instagram.com/explore/thumb-1.jpg",
        "dimensions": { "width": 1080, "height": 1080 }
      },
      "engagement": { "likeCount": 56000 },
      "topic": "NATURE"
    },
    {
      "type": "REEL",
      "reelId": "reel-explore-1",
      "author": { ... },
      "media": {
        "type": "VIDEO",
        "duration": 15000,
        "thumbnailUrl": "..."
      },
      "engagement": { "playCount": 2300000 }
    },
    ...
  ],
  "cursor": "...",
  "hasMore": true
}
```

**Explore ranking pipeline:**

```
All recent public posts/Reels (millions)
        |
        v
+-------------------------+
| Candidate Generation    |  Narrow to ~10K candidates relevant to this user
| (collaborative filter   |  Signals: topics user engages with, accounts similar
|  + content-based)       |  to accounts user follows, posts liked by user's friends
+------------+------------+
             |
             v
+-------------------------+
| ML Scoring              |  Score each candidate: P(like), P(save), P(comment)
|                         |  Blend into final ranking score
+------------+------------+
             |
             v
+-------------------------+
| Diversity Rules         |  No >N posts from same account
|                         |  Mix content types (photos, Reels, carousels)
|                         |  Inject fresh content without engagement history
+------------+------------+
             |
             v
+-------------------------+
| Content Safety Filter   |  Remove flagged content
|                         |  Reduce distribution of borderline content
+------------+------------+
             |
             v
        Top ~30 items returned per page
```

### GET /tags/{tagName}/posts

Posts with a specific hashtag. Two views: "Top" (engagement-ranked) and "Recent" (chronological).

### GET /locations/{locationId}/posts

Posts geotagged at a specific location. Same Top/Recent views.

---

## 8. Direct Messaging APIs

> Real-time messaging with persistent connections (MQTT on mobile, WebSocket on web).

### GET /inbox

Returns the user's conversation list, ordered by most recent message.

**Response:**

```json
{
  "conversations": [
    {
      "conversationId": "conv-uuid-1",
      "type": "ONE_ON_ONE",
      "participants": [
        { "userId": "user-789", "username": "best_friend", "avatarUrl": "..." }
      ],
      "lastMessage": {
        "text": "See you tomorrow!",
        "senderId": "user-789",
        "timestamp": "2025-01-15T19:45:00Z",
        "type": "TEXT"
      },
      "unreadCount": 2,
      "isMuted": false
    },
    ...
  ],
  "cursor": "...",
  "hasMore": true
}
```

### POST /messages

Send a message. Delivered in real-time via MQTT/WebSocket.

**Request:**

```json
{
  "conversationId": "conv-uuid-1",
  "type": "TEXT",
  "text": "Hey, check out this post!",
  "sharedPostId": "post-uuid-abc",
  "replyToMessageId": null,
  "disappearing": false
}
```

**Message types:** TEXT, PHOTO, VIDEO, VOICE_NOTE, POST_SHARE, REEL_SHARE, STORY_REPLY, LOCATION, GIF.

**Real-time delivery flow:**

```
Sender App          API Server          MQTT Broker         Recipient App
   |                    |                    |                    |
   |-- POST /messages ->|                    |                    |
   |                    |-- store in DB ---->|                    |
   |                    |-- publish to ------+---> MQTT topic --->|
   |<-- 200 OK ---------|    MQTT broker     |                    |
   |                    |                    |    [recipient gets |
   |                    |                    |     real-time push]|
   |                    |                    |                    |
   |                    |              (if recipient offline:     |
   |                    |               push notification via     |
   |                    |               APNs/FCM)                 |
```

### GET /conversations/{conversationId}/messages

Paginated message history. Chronological, cursor-based.

### PUT /messages/{messageId}/react

React to a message with an emoji.

### DELETE /messages/{messageId}

Unsend a message. Removes for both sender and recipient(s).

---

## 9. Notification APIs

> Activity feed + real-time push notifications.

### GET /notifications

Activity feed showing recent interactions.

**Response:**

```json
{
  "notifications": [
    {
      "type": "LIKE",
      "actors": [
        { "userId": "user-111", "username": "fan_1" },
        { "userId": "user-222", "username": "fan_2" }
      ],
      "aggregatedCount": 47,
      "displayText": "fan_1, fan_2, and 45 others liked your post",
      "targetPostId": "post-uuid-abc",
      "targetThumbnailUrl": "...",
      "timestamp": "2025-01-15T19:50:00Z",
      "isRead": false
    },
    {
      "type": "FOLLOW",
      "actors": [{ "userId": "user-333", "username": "new_follower" }],
      "aggregatedCount": 1,
      "displayText": "new_follower started following you",
      "timestamp": "2025-01-15T19:48:00Z",
      "isRead": false
    },
    {
      "type": "COMMENT",
      "actors": [{ "userId": "user-444", "username": "commenter" }],
      "aggregatedCount": 1,
      "displayText": "commenter commented: \"Great shot!\"",
      "targetPostId": "post-uuid-def",
      "timestamp": "2025-01-15T19:45:00Z",
      "isRead": true
    },
    ...
  ],
  "cursor": "...",
  "hasMore": true
}
```

**Notification aggregation:** Without aggregation, a celebrity receiving 1M likes on a post would get 1M individual notifications. Instead, likes are aggregated within a time window: "user1, user2, and 999,998 others liked your post." Only one push notification per batch (e.g., every 30 seconds during a spike).

### PUT /notifications/settings

Configure notification preferences per type (likes, comments, follows, DMs, live, etc.).

---

## 10. User Profile APIs

### GET /users/{userId} &#11088;

Returns public profile information.

**Response:**

```json
{
  "userId": "user-123",
  "username": "travelphotographer",
  "displayName": "Travel Photographer",
  "bio": "Capturing the world one photo at a time",
  "avatarUrl": "https://cdn.instagram.com/avatar/user-123.jpg",
  "isVerified": true,
  "isPrivate": false,
  "postCount": 847,
  "followerCount": 125000,
  "followingCount": 892,
  "viewer": {
    "isFollowing": true,
    "isFollowedBy": false,
    "isBlocked": false,
    "isRestricted": false,
    "isMuted": false,
    "isCloseFriend": false
  },
  "externalUrl": "https://travelphotographer.com",
  "category": "Photographer"
}
```

**Note on counts:** `followerCount`, `followingCount`, and `postCount` are denormalized counters stored alongside the profile — NOT computed by counting rows at read time. They're updated asynchronously and may be slightly stale (eventual consistency). This is acceptable: no user cares if their follower count is 125,000 vs 125,003.

### PUT /users/me

Edit the authenticated user's profile (bio, display name, external URL, category, private flag).

### GET /users/{userId}/posts

Paginated grid of a user's posts. Returns thumbnails for the grid view.

**Response:**

```json
{
  "posts": [
    {
      "postId": "post-uuid-1",
      "thumbnailUrl": "https://cdn.instagram.com/t/150x150/post-1.jpg",
      "mediaType": "CAROUSEL",
      "mediaCount": 5,
      "engagement": { "likeCount": 3400, "commentCount": 89 }
    },
    ...
  ],
  "cursor": "...",
  "hasMore": true
}
```

### PUT /users/me/avatar

Update profile picture. Triggers resize pipeline (multiple sizes for different contexts).

### POST /users/{userId}/follow-request (private accounts)

Send a follow request to a private account.

### PUT /follow-requests/{requestId}/approve

Approve a follow request. Triggers: graph edge creation, feed inbox backfill, notification.

### PUT /follow-requests/{requestId}/deny

Deny a follow request. Silent — no notification sent.

---

## 11. Media Upload APIs (Internal)

> Resumable upload protocol for photos and videos.

### POST /media/upload/init &#11088;

Initialize a resumable upload session.

**Request:**

```json
{
  "fileSize": 15728640,
  "mimeType": "image/jpeg",
  "mediaType": "PHOTO",
  "checksum": "sha256:abc123..."
}
```

**Response:**

```json
{
  "uploadId": "upload-uuid-xyz",
  "uploadUrl": "https://upload.instagram.com/v1/upload-uuid-xyz",
  "chunkSize": 5242880,
  "expiresAt": "2025-01-15T20:30:00Z"
}
```

### PUT /media/upload/{uploadId}

Upload a chunk. Supports resume on failure — client tracks which chunks have been uploaded.

**Request Headers:**

```
Content-Range: bytes 0-5242879/15728640
Content-Type: application/octet-stream
```

**Response:**

```json
{
  "uploadId": "upload-uuid-xyz",
  "bytesReceived": 5242880,
  "totalBytes": 15728640,
  "status": "IN_PROGRESS"
}
```

### POST /media/upload/{uploadId}/finalize

Finalize the upload and trigger the media processing pipeline.

**Response:**

```json
{
  "mediaId": "media-uuid-1",
  "status": "PROCESSING",
  "processingSteps": [
    { "step": "DECODE", "status": "COMPLETED" },
    { "step": "STRIP_EXIF", "status": "COMPLETED" },
    { "step": "RESIZE", "status": "IN_PROGRESS" },
    { "step": "COMPRESS", "status": "PENDING" },
    { "step": "GENERATE_BLURHASH", "status": "PENDING" },
    { "step": "UPLOAD_TO_CDN", "status": "PENDING" },
    { "step": "CONTENT_MODERATION", "status": "PENDING" }
  ]
}
```

**Processing pipeline (detailed in [03-media-processing-pipeline.md](03-media-processing-pipeline.md)):**

```
Raw Upload (JPEG/HEIF/PNG/MP4/MOV)
        |
        +-- DECODE: Parse image/video format
        +-- STRIP_EXIF: Remove GPS, camera info (privacy)
        +-- AUTO_ORIENT: Fix rotation from EXIF orientation tag
        +-- RESIZE: Generate multiple resolutions
        |     Photos: 150x150, 320px, 640px, 1080px
        |     Videos: 360p, 480p, 720p, 1080p
        +-- COMPRESS: JPEG quality optimization / video transcoding (H.264)
        +-- GENERATE_BLURHASH: Low-res placeholder string (~30 bytes)
        +-- GENERATE_THUMBNAILS: Video -> extract keyframes for cover
        +-- CONTENT_MODERATION: ML scan for nudity, violence, hate speech
        +-- UPLOAD_TO_CDN: Push all variants to blob storage + CDN
```

---

## 12. Content Moderation APIs (Internal)

> Automated ML moderation + human review pipeline.

### POST /moderation/review

Submit content for automated review. Called automatically during the media processing pipeline.

**Request:**

```json
{
  "contentId": "media-uuid-1",
  "contentType": "PHOTO",
  "mediaUrl": "https://internal-storage/raw/media-uuid-1",
  "context": {
    "caption": "Beach day!",
    "authorId": "user-123",
    "authorAccountAge": 365,
    "authorPriorViolations": 0
  }
}
```

**Response:**

```json
{
  "verdict": "APPROVED",
  "confidence": 0.97,
  "flags": [],
  "reviewType": "AUTOMATED",
  "modelVersion": "mod-v4.2"
}
```

**Possible verdicts:** `APPROVED`, `HELD_FOR_REVIEW` (sent to human review queue), `REJECTED` (violates guidelines), `AGE_GATED` (sensitive but allowed with age restriction), `REDUCED_DISTRIBUTION` (borderline — allowed but won't appear in Explore/Reels recommendations).

### GET /moderation/queue

Human review queue for content flagged by automated models or user reports.

### PUT /moderation/{contentId}/action

Take moderation action: remove, restrict, age-gate, or approve.

---

## 13. Instagram vs Twitter vs TikTok: API Model Comparison

| Dimension | Instagram | Twitter (X) | TikTok |
|---|---|---|---|
| **Content model** | Media-first (photo/video required, caption optional) | Text-first (280 chars, media optional) | Video-only (15s-10min) |
| **Feed API** | `GET /feed` — algorithmic, social-graph-based, hybrid fan-out | `GET /timeline` — "For You" (algorithmic) + "Following" (chronological) | `GET /foryou` — 100% recommendation, no social graph |
| **Content creation** | Two-phase: upload media then create post | Single call: `POST /tweets` with inline text + optional media | Two-phase: upload video then create post |
| **Resharing** | No native reshare in feed (share to Stories/DMs only) | Retweet/Quote Tweet (reshare with amplification) | Duet/Stitch (creative reshare) |
| **Social graph** | Directed (follow). 7,500 following limit | Directed (follow). No practical following limit | Directed (follow). Minimal social graph importance |
| **Ephemeral content** | Stories (24-hour TTL) | Fleets (discontinued 2021) | No ephemeral content |
| **Recommendation surface** | Explore page + Reels tab | "For You" tab, trending | For You page (the entire product) |
| **Real-time** | MQTT (mobile) + WebSocket (web) | WebSocket for streaming API | WebSocket |
| **Pagination** | Cursor-based throughout | Cursor-based (since API v2) | Cursor-based |
| **DMs** | Full messaging with media, reactions, groups | Basic DMs, recently opened to all | In-app messaging |
| **Fan-out strategy** | Hybrid (write for normal, read for celebrities) | Hybrid (similar to Instagram) | No fan-out — pure recommendation |
| **Key bottleneck** | Media processing + fan-out + CDN bandwidth | Timeline fan-out + text delivery speed | Recommendation latency + video CDN |

### Key Architectural Differences

**Instagram vs Twitter — why fan-out cost differs:**
- Twitter fans out text (tiny payload: ~1KB per tweet reference). Instagram fans out media-rich posts (postId + author + media URLs + engagement counts ~ 2-5KB).
- Twitter's fan-out is faster per-item but equally challenging at celebrity scale (100M+ followers).
- Instagram's feed items are heavier → more cache memory per feed inbox → more aggressive eviction policies needed.

**Instagram vs TikTok — why feed infrastructure differs:**
- Instagram's home feed is social-graph-based → needs a social graph store, fan-out infrastructure, and feed inbox management.
- TikTok's For You page has NO social graph dependency → needs a massive recommendation engine that indexes ALL content and scores it per-user at request time.
- Instagram runs BOTH (home feed + Reels) → double the infrastructure complexity.

**Instagram vs Twitter — media serving:**
- Instagram is media-first: CDN bandwidth is the dominant cost. Every feed load triggers 5-20 image downloads.
- Twitter is text-first: CDN serves occasional images/videos. The bottleneck is API throughput and timeline assembly, not media bandwidth.
- This is why Instagram invests heavily in image optimization (WebP, AVIF, blurhash, progressive JPEG) while Twitter focuses on API response time optimization.
