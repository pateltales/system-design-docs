# System Design Interview Simulation: Design Instagram (Photo/Video Sharing Social Network)

> **Interviewer:** Principal Engineer (L8), Meta Infrastructure Team
> **Candidate Level:** SDE-3 (L6 — Senior Software Development Engineer)
> **Duration:** ~60 minutes
> **Date:** February 20, 2026

---

## PHASE 1: Opening & Problem Statement (~2 min)

**Interviewer:**
Hey, welcome. I'm on the infrastructure team at Meta. For today's system design round, I'd like you to design **Instagram** — a photo and video sharing social network. Not just a photo gallery — I'm talking about the full end-to-end system: media upload and processing, feed generation and delivery to 2 billion users, the social graph, Stories, Reels, content discovery, and real-time features.

I care about how you think about scale, content delivery, and the trade-offs that make a media-heavy social network work at this level. I'll push on your choices — that's calibration, not criticism.

Take it away.

---

## PHASE 2: Requirements Gathering & Scoping (~5 min)

**Candidate:**
Thanks! Instagram is a massive system with many surfaces — feed, Stories, Reels, Explore, DMs, search — so let me scope this properly before diving into architecture.

**Functional Requirements — what operations do we need?**

> "Let me identify the core user-facing operations:
>
> - **Post Creation** — Upload photos/videos/carousels with captions, location tags, user tags. This triggers a media processing pipeline (resize, transcode, moderate) before the post goes live.
> - **Feed** — The home feed: a personalized, ranked feed of posts from accounts I follow. This is THE core system design problem — assembling a ranked feed for 2B+ users in <500ms.
> - **Stories** — Ephemeral 24-hour content. 500M+ daily users. Different storage model (TTL-based), different distribution (sequential playback, Stories tray ranking).
> - **Reels** — Short-form video (up to 90 seconds), recommendation-driven distribution. This is Instagram's answer to TikTok — fundamentally different from the social-graph-based home feed.
> - **Social Graph** — Follow/unfollow, directed graph (asymmetric — A follows B doesn't mean B follows A). Extreme degree variance: 1 to 650M+ followers.
> - **Engagement** — Likes, comments (threaded), saves/bookmarks, shares via DM.
> - **Search & Explore** — Discover content beyond the social graph. Search by username, hashtag, location. Explore page: recommendation-driven content grid.
> - **Direct Messages** — Real-time messaging with text, media, post shares. Not E2E encrypted by default.
>
> Clarifying questions:
> - **Should I focus on any specific surface?**"

**Interviewer:** "The feed is the most architecturally interesting — the fan-out problem. Cover Stories and Reels at a high level. Don't deep-dive DMs."

> "- **What about content moderation?**"

**Interviewer:** "Mention it in the media processing pipeline but don't deep-dive the ML models."

**Non-Functional Requirements:**

> "Now the critical constraints. Instagram is defined by its media-heavy, read-heavy workload:
>
> | Dimension | Requirement | Rationale |
> |---|---|---|
> | **Feed latency** | <500ms from app open to rendered feed | Budget: DNS (~20ms) → API (~50ms) → feed assembly (~50ms) → media URL resolution (~20ms) → first image from CDN (~200ms) → render (~50ms) |
> | **Upload latency** | <5s for photo, <30s for video (processing) | Users expect near-instant publishing. Post shows as 'processing' then flips to 'published' |
> | **Availability** | 99.99% | ~52 min downtime/year. 2B+ users — outage = global headlines |
> | **Scale** | 2B+ MAU, 500M+ daily Stories users, 100M+ uploads/day | ~1,200 uploads/sec sustained, billions of feed loads/day |
> | **Read:Write ratio** | 100,000:1+ for popular content | A post is written once, read millions of times. Justifies fan-out on write + aggressive caching |
> | **CDN efficiency** | >90% cache hit rate for hot content | Instagram is image-first — every feed load triggers 5-20 image downloads |
> | **Storage** | Hundreds of petabytes | Every photo ever posted, in 4 resolutions, stored permanently. Videos in 4 resolutions with HLS segments |
>
> One critical distinction from Twitter: **Instagram is media-first**. Every post has at least one photo or video. The CDN load per feed item is 50-300KB (image) vs <1KB (tweet). This means Instagram's bottleneck is media delivery AND metadata operations, not just timeline assembly.
>
> **Contrast with TikTok:** TikTok's primary feed is 100% recommendation-driven — users see content from strangers. Instagram's home feed is social-graph-driven with algorithmic ranking on top. Instagram also has Reels (recommendation-driven), so it must run BOTH distribution models. TikTok only runs one."

**Interviewer:**
Good scoping. You've clearly distinguished the media-heavy workload from text-first platforms. The read:write ratio justifies fan-out on write — we'll dig into that. Let's talk APIs.

---

### L5 vs L6 vs L7 — Phase 2 Rubric

| Aspect | L5 (SDE-2) | L6 (SDE-3) | L7 (Principal) |
|---|---|---|---|
| **Functional Reqs** | Lists feed, upload, like, follow | Proactively separates Stories (ephemeral, TTL) from feed (permanent), identifies Reels as a fundamentally different distribution model (recommendation vs social-graph) | Additionally discusses DM reliability model (best-effort vs guaranteed delivery), content moderation pipeline, creator monetization, regional content restrictions |
| **Non-Functional** | Mentions latency and scale | Quantifies latency budget breakdown, cites specific scale numbers (2B MAU, 100M uploads/day), calculates read:write ratio and its architectural implications | Frames NFRs in business impact: CDN bandwidth cost at scale, why read:write ratio drives the entire caching/fan-out strategy, storage cost of permanent vs ephemeral content |
| **Contrasts** | Doesn't mention other platforms | Contrasts with Twitter (text-first, lighter CDN load) and TikTok (recommendation-only, no fan-out needed) | Explains how Instagram's position between Twitter (social-graph) and TikTok (recommendation) forces a dual-infrastructure model — the most expensive but most complete approach |

---

## PHASE 3: API Design (~5 min)

**Candidate:**

> "Let me focus on the APIs that matter most architecturally — post creation (the write path), feed (the read path), and the social graph (fan-out driver). The full API surface is documented in [02-api-contracts.md](02-api-contracts.md)."

### Post Creation APIs (Write Path)

> "```
> POST /media/upload/init
> Request:  { mediaType: "photo", mimeType: "image/jpeg", sizeBytes: 4200000 }
> Response: { uploadId: "upl-uuid-123", uploadUrl: "https://upload.instagram.com/..." }
>
> PUT /media/upload/{uploadId}    (resumable chunked upload)
> Request:  Binary data (chunk)
> Response: { bytesReceived: 4200000, status: "complete" }
>
> POST /posts
> Request:  {
>     mediaIds: ["media-uuid-1"],       // from upload step
>     caption: "Sunset at the beach",
>     locationId: "loc-uuid-456",
>     taggedUsers: ["user-uuid-789"],
>     altText: "Orange sunset over ocean waves"
> }
> Response: {
>     postId: "post-uuid-abc",
>     status: "processing",    // media pipeline running
>     createdAt: 1705312800
> }
> ```
>
> **Why two-step upload?** Separating media upload from post creation allows:
> 1. **Resumable uploads** — if network drops, resume from last chunk (critical on mobile)
> 2. **Parallel processing** — media pipeline starts on upload finalize, before the post is created
> 3. **Carousel support** — upload 10 items independently, reference all in one POST /posts call
>
> The post starts in `processing` status. The media pipeline (resize 4 resolutions, generate blurhash, moderate content) runs asynchronously. On completion, status flips to `published` and fan-out begins."

### Feed APIs (Read Path)

> "```
> GET /feed?cursor={cursor}&limit=20
> Response: {
>     posts: [
>         {
>             postId: "post-uuid-abc",
>             author: { userId, username, avatarUrl },
>             media: [{ url, width, height, blurhash, type }],
>             caption: "Sunset at the beach",
>             likeCount: 1234,
>             commentCount: 56,
>             hasLiked: false,
>             hasSaved: false,
>             createdAt: 1705312800
>         },
>         ...
>     ],
>     nextCursor: "cursor-encoded-score-timestamp",
>     hasSuggestedPosts: true     // non-followed account suggestions injected
> }
> ```
>
> **Key design choices:**
> - **Cursor-based pagination** (not offset): Cursor encodes the last post's ranking score + timestamp. Handles new posts inserted while paginating. Offset-based would shift all positions when new posts arrive.
> - **Blurhash in response**: The 20-30 byte blurhash string is included inline — the client renders a colored blur placeholder immediately while the real image downloads from CDN. No extra HTTP request.
> - **Media URLs point to CDN**: The `url` field points to the nearest CDN edge PoP, not the origin server. Signed with expiration token.
>
> **Contrast with Twitter:** Twitter's feed API returns mostly text (inline, <1KB per tweet). Instagram's response references media URLs that trigger 50-300KB image downloads from CDN. The API payload is similar in size, but the downstream load is 100x heavier."

### Social Graph APIs (Fan-out Driver)

> "```
> POST /users/{userId}/follow
> Response: { status: "following" }   // or "requested" for private accounts
>
> DELETE /users/{userId}/follow
> Response: { status: "unfollowed" }
>
> GET /users/{userId}/followers?cursor={cursor}&limit=20
> Response: { users: [...], nextCursor: "...", totalCount: 650000000 }
> ```
>
> **Why this matters architecturally:** The follow/unfollow operation is the most consequential mutation in the system. When A follows B:
> 1. Write edge `(A, FOLLOWS, B)` to TAO
> 2. Write reverse edge `(B, FOLLOWED_BY, A)` to TAO
> 3. Increment follower_count(B), following_count(A) — async
> 4. **Backfill A's feed inbox with B's recent posts** — async
> 5. Send notification to B — async
> 6. Update A's recommendation profile — async
>
> Steps 3-6 are async because the follow should feel instant to the user. The feed backfill (step 4) is the expensive part — we need to fetch B's recent posts and write them into A's feed inbox."

**Interviewer:**
Good. You've identified the follow operation as the fan-out trigger — that's important. And the two-step upload is clean. Let's build the architecture. Start simple.

---

### L5 vs L6 vs L7 — Phase 3 Rubric

| Aspect | L5 (SDE-2) | L6 (SDE-3) | L7 (Principal) |
|---|---|---|---|
| **Post Creation** | Single `POST /posts` with file upload | Two-step resumable upload → post creation, explains why (network resilience, parallel processing, carousel support) | Additionally discusses upload quotas, abuse prevention (rate limiting uploads), pre-signed URLs for direct-to-S3 upload bypassing application servers |
| **Feed API** | Returns list of posts | Explains cursor-based pagination (why not offset), blurhash inline, CDN-pointed media URLs with signed expiration | Discusses API versioning across 1000s of client versions, backward-compatible field additions, response size budget (keep <50KB per page for mobile) |
| **Social Graph** | POST /follow, GET /followers | Traces the 6-step side-effect chain of a follow, identifies feed backfill as the expensive step | Discusses consistency guarantees per step (follow edge must be synchronous, counters can be eventual), partial failure handling (what if backfill fails?) |

---

## PHASE 4: High-Level Architecture — Iterative Build-Up (~15 min)

**Candidate:**

> "Let me start with the simplest thing that works, find what breaks, and evolve. This iterative build-up is how I'd actually approach designing this system."

---

### Attempt 0: Single Server with SQL and Local Disk

> "The simplest possible design — one machine with a SQL database and photos on local disk:
>
> ```
>     Mobile App
>         │
>         │  Upload photo / View feed
>         ▼
>     ┌─────────────────────────────────┐
>     │       Single Web Server          │
>     │       (Django, originally)       │
>     │                                  │
>     │   /photos/                       │
>     │     photo_001.jpg  (3 MB)        │
>     │     photo_002.jpg  (5 MB)        │
>     │                                  │
>     │   PostgreSQL:                    │
>     │     users, posts, follows,       │
>     │     likes, comments              │
>     │                                  │
>     │   Feed query:                    │
>     │   SELECT * FROM posts            │
>     │   WHERE author_id IN (           │
>     │     SELECT followee_id           │
>     │     FROM follows                 │
>     │     WHERE follower_id = ?        │
>     │   ) ORDER BY created_at DESC     │
>     │   LIMIT 20                       │
>     └─────────────────────────────────┘
> ```
>
> This is actually how Instagram started in 2010 — Django + PostgreSQL + local disk, 13 employees, ~100 EC2 instances."

**Interviewer:**
What's wrong with this?

**Candidate:**

> "Everything, at scale:
>
> | Problem | Impact |
> |---------|--------|
> | **Single point of failure** | Server dies = Instagram is down |
> | **Local disk for photos** | Disk fills up. No redundancy. Photos lost if disk fails |
> | **Photos served from app server** | Image downloads block request threads — 3MB photo ties up a thread for seconds on slow connections |
> | **Feed query is O(following × posts)** | Subquery scans the follows table, then joins with posts. For a user following 500 accounts: expensive JOIN at request time |
> | **No thumbnails** | Full-resolution 3MB images sent to mobile on 3G — terrible performance |
> | **No caching** | Every feed load hits the database. Same post viewed by 1,000 followers = 1,000 identical DB queries |
>
> The most fundamental problem is that the application server is doing everything: serving photos, running queries, handling uploads. Let me separate concerns."

---

### Attempt 1: Separate Media Storage + Basic Processing

> "**Key changes:** Object storage for media, processing workers for resizing, CDN for delivery.
>
> ```
>     Mobile App                                    Mobile App
>         │                                             │
>         │  Upload photo                                │  View feed
>         ▼                                             ▼
>     ┌──────────────┐                          ┌──────────────┐
>     │  App Server   │                          │  App Server   │
>     │  (upload)     │                          │  (read)       │
>     └──────┬───────┘                          └──────┬───────┘
>            │                                         │
>            ▼                                         │  Feed query (SQL)
>     ┌──────────────┐                                 │
>     │  Processing   │                                 ▼
>     │  Workers      │                          ┌──────────────┐
>     │               │                          │ PostgreSQL   │
>     │  Resize to:   │                          │ (sharded)    │
>     │  • 150×150    │                          └──────────────┘
>     │  • 320px      │
>     │  • 640px      │                    CDN (images)
>     │  • 1080px     │                         ▲
>     └──────┬───────┘                          │
>            │                                  │
>            ▼                                  │
>     ┌─────────────────────────────────────────┘
>     │           S3 (Object Storage)
>     │  /photos/post-uuid/
>     │      thumb_150.jpg    (~8 KB)
>     │      small_320.jpg    (~30 KB)
>     │      medium_640.jpg   (~70 KB)
>     │      large_1080.jpg   (~200 KB)
>     └────────────────────────────────────────┘
> ```
>
> **What's better:**
> - **S3 for media** — durable, scalable, no local disk limits
> - **Processing workers** — resize to 4 resolutions asynchronously (thumbnail for grid, small/medium for feed, large for full-screen)
> - **CDN** in front of S3 — app server no longer serves images. CDN edge serves from cache or fetches from S3
> - Client requests the appropriate resolution based on device screen and network quality
>
> **Contrast with Twitter's early architecture:** Twitter's early stack (Ruby on Rails + MySQL) had a similar shape. But Twitter is text-first — no media processing pipeline needed. Instagram's media pipeline is a critical path component that Twitter didn't face.
>
> **What's still broken:**"

> "| Problem | Impact |
> |---------|--------|
> | **Feed query is still a SQL JOIN** | Fan-out on read: for every feed load, join follows with posts, sort by time. Gets slower as users follow more accounts |
> | **No caching** | Every feed load hits the database. With millions of users, database becomes the bottleneck |
> | **Chronological feed only** | Users follow 500 accounts posting multiple times per day — they miss 70% of posts |
> | **Single database** | PostgreSQL master is a bottleneck and SPOF |
> | **No feed materialization** | Feed is computed on every request — same work repeated millions of times |"

---

### Attempt 2: Fan-out on Write + Caching

> "This is the fundamental shift from 'compute the feed on every read' to 'pre-build the feed on every write.'
>
> ```
>     Creator posts a photo
>         │
>         │  POST /posts
>         ▼
>     ┌──────────────┐     ┌──────────────┐
>     │  App Server   │────>│ Media        │──> S3 + CDN
>     │               │     │ Processing   │
>     │               │     └──────────────┘
>     │               │
>     │  Fan-out:     │
>     │  For each     │
>     │  follower:    │
>     │  ZADD feed:   │
>     │  {followerId} │
>     │  {timestamp}  │
>     │  {postId}     │
>     └──────┬───────┘
>            │
>            ▼
>     ┌──────────────┐     ┌──────────────┐
>     │    Redis      │     │   Memcache    │
>     │               │     │              │
>     │  feed:{uid}   │     │  User profiles│
>     │  = sorted set │     │  Post metadata│
>     │  of postIds   │     │  Follower lists│
>     └──────────────┘     └──────────────┘
>            │
>            ▼
>     Reader opens app:
>       ZREVRANGE feed:{userId} 0 19  → top 20 postIds
>       Multi-GET post metadata from Memcache
>       Return feed with CDN image URLs
>       ──> O(1) per feed load (just a cache read!)
> ```
>
> **What's better:**
> - **Pre-materialized feeds**: When a user posts, write `(postId, timestamp)` to every follower's feed inbox in Redis sorted sets. On read: `ZREVRANGE feed:{userId} 0 19` — fetch the top 20 posts in O(log N + 20). Lightning fast.
> - **Cache layer**: Memcache for user profiles, post metadata, follower lists. Most reads served from cache — database only for cache misses.
> - **Database sharding**: Shard PostgreSQL by userId. Each user's data (profile, posts) lives on one shard → single-shard queries.
>
> **The math:**
> ```
> Average user has ~200 followers
> 100M posts/day × 200 followers = 20B fan-out writes/day
> 20B ÷ 86,400 = ~230K fan-out writes/sec
> Redis handles 100K+ ops/sec per instance → ~3-5 Redis instances for fan-out (naive)
> In practice, sharded across thousands of Redis instances
> ```
>
> **Contrast with Twitter:** Twitter implemented fan-out on write around 2012 for the same reason — computing timelines on every read was too slow. Same problem, same solution, around the same timeframe.
>
> **What's still broken:**"

> "| Problem | Impact |
> |---------|--------|
> | **Celebrity fan-out** | Cristiano Ronaldo has 650M followers. Writing to 650M feed inboxes per post → takes minutes, massive write amplification. A single celebrity post generates 650M Redis writes |
> | **Chronological ordering** | Feed is sorted by timestamp — users miss important posts from close friends buried under less relevant posts |
> | **No content discovery** | Users only see content from people they follow — no way to discover new creators or content |
> | **No ephemeral content** | All content is permanent — no Stories-style quick sharing |
> | **Single CDN region** | All images served from one CDN region — users far from origin get high latency |"

**Interviewer:**
Good — the celebrity fan-out problem is exactly what I wanted you to find. How do you solve it?

---

### Attempt 3: Hybrid Fan-out + Algorithmic Ranking

**Candidate:**

> "Two major changes: solve the celebrity problem and make the feed smarter.
>
> **1. Hybrid Fan-out:**
>
> ```
> Post created by author with F followers:
>
>   If F < 500K (99%+ of users):
>     → Fan-out on WRITE: write postId to all followers' feed inboxes
>     → Cost: F writes per post (manageable)
>
>   If F >= 500K (celebrities):
>     → Fan-out on READ: store post in celebrity's post list only
>     → When a follower loads their feed:
>         1. Fetch pre-built inbox from Redis (fan-out-on-write posts)
>         2. Fetch recent posts from celebrities they follow (fan-out-on-read)
>         3. Merge, rank, return top 20
>     → Cost: ~5-20 extra lookups per feed load (only for followed celebrities)
> ```
>
> ```
>     ┌────────────────────────────────────────────────────┐
>     │                FEED ASSEMBLY                        │
>     │                                                    │
>     │  Step 1: ZREVRANGE feed:{userId} 0 499             │
>     │          → ~500 candidate posts from fan-out inbox  │
>     │                                                    │
>     │  Step 2: For each celebrity this user follows:      │
>     │          GET recent_posts:{celebrityId} LIMIT 10    │
>     │          → ~50-100 additional candidates            │
>     │                                                    │
>     │  Step 3: Merge candidates (~500-600 total)          │
>     │                                                    │
>     │  Step 4: ML Ranking Model scores each candidate     │
>     │          Predicts: P(like), P(comment), P(save),    │
>     │                    P(share), P(dwell), P(see-less) │
>     │          Score = weighted combination               │
>     │                                                    │
>     │  Step 5: Business rules                             │
>     │          • No >2 posts from same author             │
>     │          • Mix content types (photo, video, carousel)│
>     │          • Inject suggested posts from non-followed  │
>     │          • Inject ads (1 per ~5 organic posts)       │
>     │                                                    │
>     │  Step 6: Return top 20                              │
>     └────────────────────────────────────────────────────┘
> ```
>
> **2. Algorithmic Feed Ranking:**
>
> Instagram switched from chronological to algorithmic in June 2016. The stated reason: users were missing 70% of posts in their feed. The ML model predicts which posts the user is most likely to engage with.
>
> **Ranking signals (VERIFIED — from Adam Mosseri's 2021/2023 transparency posts):**
>
> | Signal Category | Examples |
> |---|---|
> | **Relationship** | How often you view their Stories, DM them, like their posts, visit their profile |
> | **Interest** | Content topics you engage with (detected by ML), post type (photo/video/carousel) |
> | **Recency** | Newer posts scored higher (decay function) |
> | **Popularity** | Engagement velocity — how fast the post is accumulating likes/comments |
> | **Session context** | Time of day, session depth (first open vs deep scroll) |
>
> **3. Social Graph Service:**
>
> Dedicated service for graph operations backed by TAO (Meta's distributed graph store).
>
> ```
> TAO Architecture:
>   Client → L1 Leaf Cache → L2 Root Cache → MySQL (sharded)
>
>   Objects: User, Post, Comment (nodes with key-value data)
>   Associations: FOLLOWS, FOLLOWED_BY, LIKED, COMMENTED_ON (typed directed edges)
>
>   Key property: association lists are time-ordered
>   FOLLOWERS(userId) returns followers in reverse-chronological order
>   without an explicit sort — it's built into the data model.
>
>   Read:write ratio: 500:1 — the two-tier cache handles this elegantly
> ```
>
> **Contrast with TikTok:** TikTok skipped the social-graph-based feed entirely. Its For You page is 100% recommendation-driven — no fan-out needed at all. TikTok's architecture is simpler: every video goes to the recommendation engine, regardless of creator's follower count. No celebrity threshold, no hybrid fan-out. Instagram evolved from social-graph to hybrid; TikTok was born recommendation-first.
>
> **What's still broken:**"

> "| Problem | Impact |
> |---------|--------|
> | **No ephemeral content** | Users want to share casual, low-stakes content that disappears — Snapchat is eating this market (2016) |
> | **No content discovery** | Feed only shows followed accounts. No Explore page, no recommendation-based discovery |
> | **CDN not optimized** | Images served in JPEG only, no modern formats (WebP, AVIF). No blurhash placeholders — blank white boxes while images load |
> | **No video support** | Feed is photo-only. No short-form video, no adaptive bitrate streaming |
> | **Single-region backend** | All servers in one data center — regional failure takes everything down |"

---

### L5 vs L6 vs L7 — Phase 4 (Attempts 0-3) Rubric

| Aspect | L5 (SDE-2) | L6 (SDE-3) | L7 (Principal) |
|---|---|---|---|
| **Fan-out** | "Use a message queue to notify followers" | Explains hybrid fan-out with celebrity threshold, quantifies the math (200 avg followers × 100M posts = 20B writes), explains why hybrid solves both extremes | Discusses dynamic threshold tuning based on system load, handling the transition when a user crosses the threshold, partial fan-out failures and recovery |
| **Feed Ranking** | "Sort by timestamp" | Explains ML ranking model with specific signals (relationship, interest, recency, popularity), multi-objective predictions, business rules for diversity | Discusses model retraining pipeline, A/B testing framework for ranking changes, calibration of prediction probabilities, exploration vs exploitation trade-off in feed |
| **Social Graph** | "Store follows in a database" | Names TAO, explains the 2-tier cache architecture, explains why association lists are time-ordered by design | Discusses TAO's consistency model across regions (leader-follower, eventual consistency, cache invalidation), sharding by id1 vs id2 trade-offs |
| **Contrasts** | Mentions Twitter | Contrasts with Twitter (same solution) and TikTok (different approach — recommendation-only, no fan-out) | Explains why TikTok's architectural simplification (no fan-out) is a competitive advantage — all engineering effort into one recommendation engine vs Instagram's dual model |

---

### Architecture After Attempt 3

```
┌──────────┐     ┌──────────┐     ┌──────────────┐     ┌──────────┐
│  Mobile  │────>│  API     │────>│ Feed Service  │────>│  Redis   │
│  App     │<────│  Gateway │     │ (hybrid       │     │ (feed    │
│          │     │          │     │  fan-out +     │     │  inboxes)│
│          │     │          │     │  ML ranking)   │     │          │
└──────────┘     └──────────┘     └──────────────┘     └──────────┘
     │                                   │
     │ Images                            │
     ▼                                   ▼
┌──────────┐                    ┌──────────────┐     ┌──────────┐
│  CDN     │                    │ Social Graph  │────>│  TAO     │
│  (Meta   │                    │ Service       │     │ (MySQL + │
│  PoPs)   │                    │ (follow,      │     │  Cache)  │
│          │                    │  unfollow)    │     │          │
└──────────┘                    └──────────────┘     └──────────┘
     ▲
     │
┌──────────────┐     ┌──────────────┐
│  S3 / Blob   │<────│ Media        │
│  Storage     │     │ Processing   │
│              │     │ Pipeline     │
└──────────────┘     └──────────────┘
```

---

### Attempt 4: Stories + Explore + Enhanced Media Delivery

**Candidate:**

> "Three major additions to address the remaining gaps.
>
> **1. Stories — Ephemeral Content (Launched August 2016):**
>
> Stories are 24-hour TTL content. This changes the storage model:
>
> ```
> ┌───────────────────────────────────────────────────────────┐
> │ Stories Storage                                           │
> │                                                           │
> │ Metadata: Cassandra with column-level TTL                 │
> │   Row key: userId                                         │
> │   Columns: storyId, mediaUrl, createdAt, expiresAt,       │
> │            stickers, closeFriendsOnly                     │
> │   TTL: 86400 seconds (24 hours)                           │
> │   → Cassandra auto-deletes expired columns. No cleanup job│
> │                                                           │
> │ Media: S3 with lifecycle policy                           │
> │   Objects auto-deleted after 24 hours                     │
> │   OR: separate ephemeral bucket with automated cleanup    │
> │                                                           │
> │ CDN URLs: embedded expiration tokens                      │
> │   After TTL → 404 (content gone from CDN too)             │
> └───────────────────────────────────────────────────────────┘
> ```
>
> **Why Cassandra for Stories?** Cassandra natively supports column-level TTL — write a column with `TTL=86400` and Cassandra deletes it automatically. No external cleanup job needed. High write throughput for 500M+ Stories/day. Time-series-friendly data model.
>
> **Stories Tray** (the row of circles at the top of the feed):
> 1. Fetch list of followed accounts with active (non-expired) Stories
> 2. Partition: unseen (colored ring) vs seen (gray ring)
> 3. Rank by: `relationship_closeness × recency_weight`
> 4. Return top-N with first Story thumbnail for prefetching
>
> **Seen-state tracking is a scale problem:**
> - 500M users view Stories daily, each views ~50 accounts' Stories
> - = **25 billion seen-state entries per day**, each ~20 bytes
> - = ~500GB new seen-state data per day (also ephemeral — only needed 24 hours)
> - Storage: Redis bitmaps or Cassandra with TTL
>
> **Contrast with Snapchat:** Snapchat was built ephemeral-first — its entire storage layer is optimized for expiring content. Instagram bolted ephemeral content onto a platform designed for permanent photos. This means Instagram must manage two storage lifecycles (permanent + TTL-based), migration between them (Stories → Highlights), and different caching strategies for each. More complex, but captures both use cases.
>
> **2. Explore Page — Content Discovery:**
>
> ```
> All recent public posts (millions/day)
>         │
>         ▼
> ┌───────────────────────────────────────┐
> │ STAGE 1: Candidate Generation         │
> │   ~10K candidates per user             │
> │                                        │
> │ • IG2Vec: embed accounts in vector     │
> │   space using interaction sequences    │
> │ • Collaborative filtering: users who   │
> │   liked A and B also liked C           │
> │ • Two-tower model (2023): one tower    │
> │   encodes user, one encodes content    │
> │   → FAISS approximate nearest neighbor │
> └────────────────┬──────────────────────┘
>                  ▼
> ┌───────────────────────────────────────┐
> │ STAGE 2: First-Pass Ranking            │
> │   Distillation model → ~150 candidates │
> │   (lightweight model approximating     │
> │    the full ranker — speed over accuracy)│
> └────────────────┬──────────────────────┘
>                  ▼
> ┌───────────────────────────────────────┐
> │ STAGE 3: Full Ranking (MTML)           │
> │   Deep NN scores ~150 candidates       │
> │   Predicts: P(like), P(comment),       │
> │   P(save), P(share), P(see-fewer)      │
> │   → weighted combination               │
> └────────────────┬──────────────────────┘
>                  ▼
> ┌───────────────────────────────────────┐
> │ Business Rules + Diversity             │
> │   • Max N posts from same account      │
> │   • Mix content types                  │
> │   • Inject fresh/exploration content   │
> │   • Content safety filtering           │
> └────────────────┬──────────────────────┘
>                  ▼
>          Top ~30 items per page
> ```
>
> **VERIFIED — from Meta Engineering blog 'Powered by AI: Instagram's Explore recommender system' (2019).**
>
> **3. Enhanced CDN and Image Delivery:**
>
> ```
> Image optimization stack:
>   • Progressive JPEG: loads blurry → sharpens progressively
>     (vs baseline JPEG which loads top-to-bottom)
>   • WebP: ~25-30% smaller than JPEG at same quality
>     (default for Android clients)
>   • AVIF: ~50% smaller than JPEG (where supported)
>   • Content negotiation: client sends Accept header, CDN returns
>     most efficient supported format
>   • Blurhash: ~30 bytes inline in API response
>     → colored blur placeholder rendered instantly, no extra HTTP request
>
> Bandwidth impact at scale:
>   1B image loads/day × 20KB savings per image (WebP vs JPEG)
>   = ~20TB bandwidth saved per day
> ```
>
> **What's still broken:**"

> "| Problem | Impact |
> |---------|--------|
> | **No short-form video** | TikTok is eating Instagram's lunch with recommendation-driven short videos. Instagram has no competing surface |
> | **No real-time features** | DMs have no typing indicators, no real-time delivery. Notifications are pull-based only |
> | **Single-region** | Backend in one DC — regional failure takes everything down |
> | **Custom photo storage needed** | At hundreds of billions of images, filesystem metadata overhead becomes dominant — 3-4 disk I/Os per photo read |
> | **Notification storms** | Celebrity posts generate millions of notifications. No aggregation — phone vibrates continuously |"

---

### Attempt 5: Reels + Real-Time + Production Hardening

**Candidate:**

> "This is the production-grade system. Five major additions.
>
> **1. Reels — Short-Form Video (Launched August 2020):**
>
> Reels are fundamentally different from feed posts in distribution:
>
> | Aspect | Feed Posts | Reels |
> |---|---|---|
> | **Distribution** | Social-graph (fan-out to followers) | Recommendation-driven (anyone can see it) |
> | **Discovery** | Followers' feeds only | Reels tab, Explore, home feed suggestions |
> | **Fan-out** | Write to followers' inboxes | Index in recommendation engine |
> | **Content** | Photo, video, carousel | Video only (15-90s, vertical 9:16) |
>
> ```
> Creator posts a Reel
>         │
>         ├── Media Pipeline: transcode (4 resolutions), HLS segmentation,
>         │   audio extraction, content moderation, thumbnail generation
>         │
>         ├── Index in Recommendation Engine:
>         │   • Extract content features (video understanding ML)
>         │   • Extract audio features (fingerprint, trending sounds)
>         │   • Generate content embeddings
>         │   • Register in candidate pool
>         │
>         └── If shareToFeed=true:
>             Also fan-out to followers' feed inboxes (same as regular post)
> ```
>
> **Key insight:** A Reel has TWO distribution paths. The recommendation path (always) indexes it for the Reels tab and Explore. The social-graph path (optional) fans it out to followers' feeds. This is why Instagram runs two distribution systems.
>
> **Reels ranking signals (VERIFIED — Mosseri 2023):**
> - **Watch-through rate** — did user watch the full Reel? (THE strongest signal)
> - **Replays** — user replays = very strong interest
> - **Shares** — sharing via DM = content worth passing along
> - **Go-to-audio** — user visits the audio page = creative inspiration
> - **Likes** — weaker than watch-through (passive vs implicit signal)
>
> **Prefetching:**
> ```
> User watches Reel #1
>     ├── Background: download HLS segments for Reel #2 (next)
>     ├── Background: download thumbnail + first segment for Reel #3
>     └── Background: prefetch metadata for Reels #4-#5
> → Reel #2 plays instantly when user swipes
> ```
>
> **2. Real-Time Infrastructure (MQTT):**
>
> **VERIFIED — from Facebook Engineering blog (2011) and @Scale conference talks (2015).**
>
> ```
> ┌──────────┐     ┌──────────────┐     ┌──────────────────┐
> │Instagram │────>│ MQTT Broker  │<────│ Backend Services │
> │App       │<────│ (Meta infra) │     │ (Notifications,  │
> │(iOS/     │     │              │     │  DM delivery,    │
> │ Android) │     │              │     │  typing, feed    │
> └──────────┘     └──────────────┘     │  updates)        │
>   Persistent        Pub/Sub           └──────────────────┘
>   TCP connection    routing
> ```
>
> **Why MQTT (not WebSocket)?**
> - 2-byte minimum header (vs 2-14 bytes for WebSocket frames)
> - Designed for unreliable mobile networks (3G, spotty WiFi)
> - QoS levels (at-most-once, at-least-once, exactly-once)
> - Battery-efficient: tiny keepalive packets, minimal radio wake-ups
> - At 500M+ mobile users, even 1% battery efficiency improvement = billions of battery-hours saved
>
> **What flows over MQTT:** DM delivery, typing indicators, notification badges, feed updates (\"new posts available\"), Stories updates, live video alerts.
>
> **For web (instagram.com):** WebSocket instead — natively supported by browsers, no MQTT library needed.
>
> **3. Notification Aggregation (Thundering Herd):**
>
> ```
> Celebrity posts a photo → 1M likes in 5 minutes
>
> Without aggregation: 1M individual push notifications → phone explodes
>
> With aggregation:
>   Time 0:01 — 10K likes → \"user_1 liked your post\"
>   Time 0:02 — 50K more  → \"user_1, user_2, and 49,998 others liked your post\"
>                             (replace previous notification, don't add)
>   Time 0:05 — 200K more → count-only update via MQTT (no push)
>   Time 0:30 — 1M total  → one final aggregated push
>
> Implementation: Redis aggregation buffer
>   Key: notif-buffer:{postId}:{type}
>   Value: {count, first_actors[], window_start}
>   On each like: INCR count
>   If count == 1: schedule push in 30 seconds
>   After 30s: flush buffer, send aggregated notification, reset
> ```
>
> **4. Multi-Datacenter Active-Active:**
>
> ```
> ┌──────────────┐                    ┌──────────────┐
> │  US-East DC  │                    │  US-West DC  │
> │  (Leader)    │                    │  (Follower)  │
> │              │                    │              │
> │  TAO (MySQL  │──async repl──────>│  TAO (MySQL  │
> │   master)    │                    │   replica)   │
> │              │                    │              │
> │  Cassandra   │──multi-master────>│  Cassandra   │
> │  (ring)      │                    │  (ring)      │
> │              │                    │              │
> │  Memcache    │──invalidation────>│  Memcache    │
> │  (regional)  │                    │  (regional)  │
> └──────────────┘                    └──────────────┘
>
> DNS routes users to nearest DC. Both serve production traffic.
> Writes: forwarded to leader region for TAO, local for Cassandra.
> Reads: served locally (eventual consistency acceptable for feeds,
>        counters, notifications).
> ```
>
> **5. Custom Storage (Haystack/f4):**
>
> **VERIFIED — Haystack: USENIX OSDI 2010. f4: USENIX OSDI 2014.**
>
> ```
> The problem at 260B+ images:
>   Traditional filesystem: 3-4 disk I/Os per photo read
>     (directory inode → directory entry → file inode → file data)
>   At billions of reads/day: metadata cache misses are frequent
>
> Haystack solution:
>   Pack multiple photos into large volume files (100GB each)
>   In-memory index: photoId → (volume, offset, size)
>   Index is ~10 bytes per photo → billions fit in RAM
>   Result: 1 disk I/O per photo read (always)
>   3-4x improvement in read throughput
>
> f4 (warm storage):
>   For older, rarely accessed photos (>90 days old)
>   Reed-Solomon erasure coding (14,10)
>   Effective replication: 2.1x (vs Haystack's 3.6x)
>   Saves ~65% storage cost for cold content
>   Trade-off: higher read latency (reconstruction needed for some reads)
> ```"

**Interviewer:**
This is a solid production architecture. Let me push on a few decisions. You mentioned the celebrity threshold for hybrid fan-out — what happens when a user's follower count crosses the threshold? Say they had 490K followers yesterday and hit 510K today.

**Candidate:**

> "Good edge case. There's a transition period:
>
> 1. Their older posts were already fanned out on write to 490K inboxes — those stay.
> 2. New posts (after crossing threshold) go to the fan-out-on-read path — stored in celebrity's post list, fetched at read time.
> 3. Feed assembly merges both: the pre-existing inbox entries (fan-out-on-write) + new celebrity posts (fan-out-on-read).
> 4. Over time, the old inbox entries age out (Redis sorted set trimmed to last 500 posts), and all posts from this user are on the read path.
>
> The transition is gradual and invisible to the user. The feed assembly code already handles both paths — it always checks the inbox AND the celebrity list. It's not a hard switch; it's an asymptotic convergence."

**Interviewer:**
And the reverse? A celebrity's follower count drops below the threshold?

**Candidate:**

> "Practically, this is rare — users almost never go from 1M to 400K followers. But architecturally, the same logic works in reverse:
>
> 1. New posts start getting fanned out on write again
> 2. Old posts remain in the celebrity list (fetched on read)
> 3. Gradually converges to the write path
>
> In practice, I'd add hysteresis — use 400K as the 'drop back to write' threshold and 500K as the 'switch to read' threshold. Prevents oscillation for users hovering around the boundary."

---

### L5 vs L6 vs L7 — Phase 4 (Attempts 4-5) Rubric

| Aspect | L5 (SDE-2) | L6 (SDE-3) | L7 (Principal) |
|---|---|---|---|
| **Stories** | "Store Stories with a TTL" | Explains Cassandra column TTL, seen-state scale (25B entries/day), Stories tray ranking by closeness, Highlights migration (ephemeral → permanent) | Discusses the dual-storage-lifecycle complexity, cost model (ephemeral vs permanent storage tiers), why Cassandra's native TTL is superior to external cleanup jobs |
| **Explore/Reels** | "Recommend popular content" | Explains 3-stage pipeline (candidate generation → distillation → full ranking), names specific techniques (IG2Vec, FAISS, MTML), identifies watch-through rate as strongest Reels signal | Discusses cold-start for new users (bootstrap from demographic data, social graph), exploration vs exploitation, how content safety filtering interacts with ranking, feedback loops and filter bubbles |
| **Real-Time** | "Use WebSocket" | Explains why MQTT over WebSocket for mobile (battery, header overhead, QoS, unreliable networks), notification aggregation for thundering herd | Discusses MQTT broker scaling (topic partitioning, connection limits per broker), graceful degradation when MQTT broker fails (fallback to APNs/FCM), connection migration during DC failover |
| **Production** | "Add more servers" | Multi-DC active-active, TAO leader-follower replication, Cassandra multi-master, Haystack/f4 custom storage with I/O analysis, HyperLogLog for view counts | Discusses failure modes (DC failover — recent writes lost in async replication lag window), cache warming strategies, blast radius containment, capacity planning models |

---

### Final Architecture

```
┌─────────────────────────────────────────────────────────────────────────┐
│                          INSTAGRAM ARCHITECTURE                          │
│                                                                          │
│  ┌──────────┐     ┌──────────┐     ┌────────────────────────────────┐   │
│  │  Mobile   │────>│  API     │────>│  Service Layer                 │   │
│  │  App      │<────│  Gateway │     │                                │   │
│  │  (iOS/    │     │  (Load   │     │  ┌────────────┐ ┌───────────┐ │   │
│  │  Android) │     │  Balancer)     │  │ Feed       │ │ Post      │ │   │
│  └──────────┘     └──────────┘     │  │ Service    │ │ Service   │ │   │
│       │                            │  └────────────┘ └───────────┘ │   │
│       │ MQTT                       │  ┌────────────┐ ┌───────────┐ │   │
│       ▼                            │  │ Stories    │ │ Reels     │ │   │
│  ┌──────────┐                      │  │ Service    │ │ Service   │ │   │
│  │  MQTT    │                      │  └────────────┘ └───────────┘ │   │
│  │  Broker  │                      │  ┌────────────┐ ┌───────────┐ │   │
│  └──────────┘                      │  │ Social     │ │ Search &  │ │   │
│                                    │  │ Graph Svc  │ │ Explore   │ │   │
│  ┌──────────┐                      │  └────────────┘ └───────────┘ │   │
│  │  CDN     │                      │  ┌────────────┐ ┌───────────┐ │   │
│  │  (Meta   │                      │  │ Notif.     │ │ Media     │ │   │
│  │  Edge    │                      │  │ Service    │ │ Processing│ │   │
│  │  PoPs)   │                      │  └────────────┘ └───────────┘ │   │
│  └──────────┘                      └────────────────────────────────┘   │
│       ▲                                         │                       │
│       │                                         ▼                       │
│  ┌────┴──────────────────────────────────────────────────────────────┐  │
│  │                      DATA LAYER                                    │  │
│  │                                                                    │  │
│  │  ┌──────────┐  ┌──────────┐  ┌──────────┐  ┌──────────────────┐  │  │
│  │  │  TAO     │  │ Cassandra│  │  Redis   │  │  Memcache        │  │  │
│  │  │ (Social  │  │ (Stories,│  │ (Feed    │  │  (Profiles, post │  │  │
│  │  │  Graph,  │  │  Activity│  │  inboxes,│  │   metadata,      │  │  │
│  │  │  MySQL)  │  │  Feed)   │  │  counters│  │   general cache) │  │  │
│  │  └──────────┘  └──────────┘  │  rate    │  └──────────────────┘  │  │
│  │                              │  limits) │                         │  │
│  │  ┌──────────┐  ┌──────────┐  └──────────┘                        │  │
│  │  │ Haystack │  │  f4      │                                       │  │
│  │  │ (Hot     │  │ (Warm   │                                       │  │
│  │  │  photos) │  │  photos) │                                       │  │
│  │  └──────────┘  └──────────┘                                       │  │
│  └───────────────────────────────────────────────────────────────────┘  │
└─────────────────────────────────────────────────────────────────────────┘
```

### Architecture Evolution Summary

| Attempt | Key Addition | Problem Solved | New Problem Introduced |
|---|---|---|---|
| **0** | Single server + SQL + local disk | Nothing (starting point) | Everything — SPOF, no scaling, no processing |
| **1** | S3 + media processing + CDN | Media storage and delivery separated from app server | Feed still computed on every read (O(following × posts)) |
| **2** | Fan-out on write + Redis + caching | Feed reads are O(1) — pre-materialized inboxes | Celebrity fan-out (650M writes per post) |
| **3** | Hybrid fan-out + ML ranking + TAO | Celebrity posting is instant; feed is ranked by relevance | No ephemeral content, no discovery, no video |
| **4** | Stories + Explore + enhanced CDN | Ephemeral content, content discovery, optimized images | No short-form video, no real-time, single-region |
| **5** | Reels + MQTT + multi-DC + Haystack/f4 | Competes with TikTok, real-time features, resilient, optimized storage | Operational complexity of running two distribution models |

---

## PHASE 5: Deep Dive — Feed Generation (~8 min)

**Interviewer:**
Let's go deeper on feed generation. Walk me through what happens from the moment a user posts a photo to when it appears in a follower's feed.

**Candidate:**

> "End-to-end flow — let's trace a concrete example. User A has 10,000 followers and posts a photo.
>
> ```
> User A taps 'Share'
>     │
>     │  POST /media/upload/init → PUT /media/upload/{id} → POST /posts
>     ▼
> ┌─────────────────────────────────────────────────────────────────┐
> │ STEP 1: Media Processing Pipeline (~1-3 seconds for photo)      │
> │                                                                  │
> │ Decode JPEG → strip EXIF (privacy) → auto-orient →              │
> │ resize to 4 variants (150×150, 320px, 640px, 1080px) →          │
> │ compress (WebP for Android, JPEG fallback) →                    │
> │ generate blurhash (~30 bytes) →                                 │
> │ content moderation (PDQ hash, nudity detection ML) →            │
> │ upload all variants to Haystack →                               │
> │ return CDN URLs for each variant                                │
> │                                                                  │
> │ Post status: 'processing' → 'published'                         │
> └─────────────────────────────┬───────────────────────────────────┘
>                               │
>                               ▼
> ┌─────────────────────────────────────────────────────────────────┐
> │ STEP 2: Fan-out on Write                                        │
> │                                                                  │
> │ User A has 10,000 followers (< 500K threshold → fan-out write)  │
> │                                                                  │
> │ For each follower F:                                             │
> │   ZADD feed:{F} {timestamp} {postId}                             │
> │                                                                  │
> │ 10,000 Redis ZADD operations                                    │
> │ Batched and pipelined → completes in <1 second                  │
> │                                                                  │
> │ Also: ZADD into Cassandra (durable backup of feed inbox)         │
> └─────────────────────────────┬───────────────────────────────────┘
>                               │
>                               ▼
> ┌─────────────────────────────────────────────────────────────────┐
> │ STEP 3: Follower Opens App → Feed Assembly                      │
> │                                                                  │
> │ Follower B opens Instagram:                                     │
> │                                                                  │
> │ 1. ZREVRANGE feed:{B} 0 499 → ~500 candidate postIds from inbox │
> │ 2. For each celebrity B follows:                                 │
> │    GET recent_posts:{celebrityId} → ~10 posts each              │
> │    → ~50-100 additional candidates                              │
> │ 3. Merge: ~500-600 total candidates                             │
> │ 4. Batch fetch post metadata from Memcache (parallel multi-GET) │
> │ 5. ML ranking model scores each candidate:                      │
> │    - Extract features (relationship signals, content signals,    │
> │      recency, popularity)                                        │
> │    - Predict: P(like), P(comment), P(save), P(share),            │
> │              P(dwell>3s), P(see-less)                            │
> │    - Score = w1·P(like) + w2·P(comment) + w3·P(save)            │
> │            + w4·P(share) + w5·P(dwell) - w6·P(see-less)         │
> │ 6. Business rules: max 2 per author, mix content types,          │
> │    inject suggested posts, inject ads                            │
> │ 7. Return top 20 with CDN image URLs + blurhash                 │
> │                                                                  │
> │ Total latency: ~50ms (Redis + Memcache + ranking model)          │
> └─────────────────────────────────────────────────────────────────┘
> ```"

**Interviewer:**
What about feed invalidation? User A deletes a post, or User B unfollows User A.

**Candidate:**

> "Both require **reverse fan-out** — the opposite of what we did during posting:
>
> **Post deletion:**
> ```
> User A deletes a post
>     │
>     ├── Delete post metadata from database
>     ├── Delete media from Haystack/CDN (or mark for lazy deletion)
>     └── Reverse fan-out: For each follower F:
>         ZREM feed:{F} {postId}
>         → 10,000 Redis ZREM operations (async, non-blocking)
>
> The post disappears from feeds immediately for users who
> haven't loaded their feed yet. Users who already loaded and
> are viewing the post may see it briefly — the client handles
> the 'post deleted' error gracefully when the user interacts.
> ```
>
> **Unfollow:**
> ```
> User B unfollows User A
>     │
>     ├── Delete edge (B, FOLLOWS, A) from TAO
>     ├── Delete reverse edge (A, FOLLOWED_BY, B) from TAO
>     └── Remove A's posts from B's feed inbox (async, expensive):
>         → Scan feed:{B} for posts authored by A
>         → ZREM each one
>
> This is expensive because we need to scan B's entire inbox
> to find A's posts. In practice, this is done lazily — on
> B's next feed load, posts from unfollowed accounts are
> filtered out during the ranking step. The inbox cleanup
> happens asynchronously in the background.
> ```"

**Interviewer:**
How do you handle the "Following" chronological feed that Instagram added in 2022?

**Candidate:**

> "The Following feed is simpler — same fan-out infrastructure, different ranking:
>
> 1. Fetch the same inbox: `ZREVRANGE feed:{userId} 0 19`
> 2. Sort by timestamp (the sorted set score IS the timestamp) — already in order
> 3. No ML ranking, no suggested posts, no ads
> 4. Return posts in pure chronological order
>
> The infrastructure cost is minimal — we're reusing the same fan-out inbox. The only difference is skipping the ranking model. Instagram offers this as an option to satisfy users who prefer chronological ordering, while keeping the algorithmic Home feed as the default."

---

### L5 vs L6 vs L7 — Phase 5 Rubric

| Aspect | L5 (SDE-2) | L6 (SDE-3) | L7 (Principal) |
|---|---|---|---|
| **Feed Assembly** | "Fetch posts from database" | Traces the full 3-step flow (fan-out write → inbox read + celebrity merge → ML ranking), quantifies latency at each step | Discusses cache warming on cold start (new user has empty inbox), handling stale cache entries (post deleted but still in inbox), ranking model latency budget (~10ms for 500 candidates) |
| **Invalidation** | "Delete the post" | Explains reverse fan-out for deletion and lazy cleanup for unfollows, acknowledges the cost asymmetry (fan-out write is async but reverse fan-out is expensive) | Discusses eventual consistency windows (how long until a deleted post disappears from all feeds?), tombstone management, impact on cache invalidation pipeline |
| **Ranking Model** | "Sort by likes" | Names specific predictions (P(like), P(save), P(dwell)), explains the weighted scoring function, mentions P(see-less) as a negative weight | Discusses model retraining cadence, A/B testing framework for weight tuning, offline evaluation metrics vs online engagement metrics, the tension between engagement optimization and user wellbeing |

---

## PHASE 6: Deep Dive — Social Graph & TAO (~5 min)

**Interviewer:**
Tell me about the social graph. How do you store and query a graph with 2B+ nodes and hundreds of billions of edges?

**Candidate:**

> "The social graph is stored in TAO — Meta's distributed graph store, purpose-built for the 'objects and associations' pattern.
>
> **VERIFIED — from 'TAO: Facebook's Distributed Data Store for the Social Graph' USENIX ATC 2013.**
>
> ```
> TAO Data Model:
>
>   Objects (nodes):
>     User(id=42, username='travelphotographer', ...)
>     Post(id=999, authorId=42, caption='sunset', ...)
>
>   Associations (typed, directed edges):
>     (user_42, FOLLOWS, user_99, timestamp=1705312800)
>     (user_99, FOLLOWED_BY, user_42, timestamp=1705312800)
>
>   Association lists are time-ordered by design:
>     FOLLOWERS(user_99) → [(user_42, ts), (user_55, ts), ...]
>     → Returns followers in reverse-chronological order
>     → No explicit sort needed — the data model provides it
>
> Architecture:
>
>   Client → L1 Leaf Cache → L2 Root Cache → MySQL (sharded)
>            (many per       (few per         (persistent
>             region,         region,          source of
>             serve reads)    coordinate       truth)
>                             writes)
>
> Read path:  Client → L1 hit? return : L2 hit? return : MySQL → populate caches
> Write path: Client → Leader L2 → MySQL → invalidate L1 caches (async)
> ```
>
> **Key design decisions:**
>
> 1. **Sharding by id1:** Associations `(id1, type, id2)` are sharded by `id1`. This means all of user X's outgoing relationships are on one shard — efficient for 'who does X follow?' But 'who follows X?' requires the reverse association `FOLLOWED_BY(X)`, stored redundantly on X's shard.
>
> 2. **Consistency model:** Eventual consistency across regions, read-after-write within the leader region. For social graph mutations (follow/unfollow), writes go to the leader, then cache invalidation propagates to followers. A user in Europe follows someone — the edge is written in the US leader, then replicated. Read-after-write is guaranteed in the leader region but there's a brief window where follower regions see stale data.
>
> 3. **Hot shard mitigation:** Celebrity accounts create hot shards. Mitigation: denormalized counters (don't COUNT(FOLLOWED_BY)), paginated access only (never load full 650M follower list), multiple L1 cache replicas for hot data.
>
> **Scale numbers:**
>
> | Operation | Typical Latency | How |
> |---|---|---|
> | Check if A follows B | ~1ms | L1 cache hit (single association lookup) |
> | Get A's following list (page 1) | ~2ms | L1 cache hit (association list scan) |
> | Get B's follower count | ~1ms | Denormalized counter in L1 cache |
> | Get mutual followers | ~5-10ms | Two list fetches + in-memory intersection |"

**Interviewer:**
Why TAO over a general-purpose graph database like Neo4j?

**Candidate:**

> "Three reasons:
>
> 1. **Scale:** TAO is designed for Meta's scale — trillions of cache operations/day, hundreds of billions of edges. Neo4j's community edition is single-server; enterprise edition scales to a cluster but not to this magnitude.
>
> 2. **Access pattern fit:** Instagram's graph queries are almost entirely local lookups: 'does A follow B?', 'who does A follow?', 'who follows A?'. These are association lookups and association list scans — exactly what TAO optimizes. Instagram doesn't need Cypher-style multi-hop traversals (find all users within 3 degrees of A) that graph databases like Neo4j are designed for.
>
> 3. **Integration:** TAO is shared infrastructure at Meta. Facebook, Instagram, WhatsApp, Messenger all use TAO. The caching layer, operational tooling, monitoring, multi-region replication — all battle-tested at Meta's scale. Building on shared infrastructure is cheaper than running a separate graph database."

---

## PHASE 7: Deep Dive — Data Storage (~5 min)

**Interviewer:**
You've mentioned several storage systems — TAO, Cassandra, Redis, Memcache, Haystack, f4. Walk me through how you decide which store to use for what.

**Candidate:**

> "Each storage system is chosen for a specific access pattern. There's no one-size-fits-all:
>
> | Data | Access Pattern | Storage | Why This Store |
> |---|---|---|---|
> | Social graph (follows, likes) | Graph traversal, association lists | TAO (MySQL + 2-tier cache) | Purpose-built for objects + associations; time-ordered lists; 500:1 read:write ratio handled by cache |
> | Feed inboxes | Write-heavy (fan-out), read-heavy (feed load) | Redis (sorted sets) + Cassandra (durable) | Redis: sub-ms reads via ZREVRANGE; Cassandra: durable backup, survives Redis eviction |
> | Stories metadata | Write-once, read-often, auto-expire | Cassandra (column TTL) | Native 24-hour TTL — DB handles deletion; high write throughput for 500M+ Stories/day |
> | User accounts, post metadata | Structured records, read-heavy | MySQL/MyRocks | ACID for account mutations; MyRocks gives 50% compression vs InnoDB |
> | General caching | Ultra-high-throughput key-value | Memcache | Trillions of ops/day; lease mechanism for thundering herd; gutter servers for failover |
> | Hot photos | Billion-scale small-file reads | Haystack | 1 disk I/O per read (in-memory index); eliminates filesystem metadata overhead |
> | Warm photos (>90 days) | Infrequent reads of older content | f4 | Erasure coding (2.1x vs 3.6x replication); 65% storage savings |
> | Search | Prefix matching, full-text | Elasticsearch / Unicorn | Typeahead <100ms; social-graph-aware ranking (Unicorn) |
> | Analytics/ML training | Batch processing, data warehouse | Scribe → Hive/Presto | Log transport → warehouse → ML training pipelines |
>
> **The honest answer to 'why not just use PostgreSQL for everything?':** You CAN, up to maybe 50M users. Beyond that, different data has fundamentally different access patterns. Graph traversal needs association-list caching (TAO). Feed writes need linear write scalability (Cassandra). Feed reads need sub-ms latency (Redis). Photos need minimized disk I/O (Haystack). Each specialized system solves a specific bottleneck that a general-purpose DB hits at scale.
>
> **Memory budget for feed inboxes:**
> ```
> 700M DAU × 500 posts per inbox × 40 bytes per entry
> = ~14TB of Redis memory
> Spread across thousands of Redis instances
> Each instance: ~10-20GB RAM → ~700-1,400 instances for feed inboxes alone
> ```"

**Interviewer:**
How do you handle cache consistency? Specifically, a post is deleted — how does Memcache stay in sync?

**Candidate:**

> "Meta's cache invalidation pipeline:
>
> **VERIFIED — from 'Scaling Memcache at Facebook' USENIX NSDI 2013.**
>
> ```
> Write path:
>   1. Application writes to MySQL (source of truth)
>   2. mcsqueal: daemon that tails MySQL's binlog
>   3. On detecting a row change → publishes invalidation event
>   4. Invalidation event → Memcache DELETE for the affected key
>   5. Stale cache entry is removed
>   6. Next read: cache miss → fetch from DB → populate cache
>
> This is a DELETE-based invalidation (not UPDATE):
>   • Simpler: just delete the key, let the next read repopulate
>   • Avoids race conditions from concurrent writes
>   • Lease mechanism prevents thundering herd on cache miss:
>     → First requester gets a 'lease' to populate the cache
>     → Other requesters wait for the lease holder to finish
>     → No stampede to the database
> ```
>
> For cross-datacenter consistency: invalidation events are propagated to all DCs. Within the leader DC: strong read-after-write consistency. Across DCs: eventual consistency (lag window of milliseconds to seconds)."

---

### L5 vs L6 vs L7 — Phase 7 Rubric

| Aspect | L5 (SDE-2) | L6 (SDE-3) | L7 (Principal) |
|---|---|---|---|
| **Storage Selection** | "Use PostgreSQL and Redis" | Explains WHY each storage is chosen for its access pattern, names specific systems (TAO, Haystack, f4, MyRocks), quantifies memory budget for Redis | Discusses storage evolution over time (PostgreSQL → specialized stores), migration strategies, operational cost of running 7+ storage systems, when to consolidate vs specialize |
| **Cache Consistency** | "Use TTL to expire cache" | Explains mcsqueal binlog tailing → invalidation → lease mechanism, distinguishes delete-based vs update-based invalidation | Discusses cross-DC invalidation lag, partial failure modes (what if mcsqueal falls behind?), monitoring cache hit rates as a health signal, thundering herd at startup (cold cache) |
| **Haystack** | Doesn't mention | Explains the filesystem metadata overhead problem, in-memory index (10 bytes/photo), 1 disk I/O per read | Discusses compaction strategy, volume sizing trade-offs, RAID configuration for Haystack servers, failure domain isolation |

---

## PHASE 8: Deep Dive — Stories & Reels (~5 min)

**Interviewer:**
You mentioned Stories and Reels are fundamentally different distribution models. Expand on the architectural implications.

**Candidate:**

> "The key distinction is **distribution mechanism**:
>
> **Stories = Social-graph-distributed, ephemeral:**
> - Only followers see your Stories (same graph as feed)
> - 24-hour TTL eliminates long-term storage concerns
> - Stories tray ordering is personalized (ranked by relationship closeness)
> - No fan-out to feed inboxes — Stories have their own tray
> - Seen-state must be tracked (colored vs gray ring)
>
> **Reels = Recommendation-distributed, permanent:**
> - ANYONE can see your Reel (not limited to followers)
> - Content lives forever (permanent storage)
> - Reels tab is an infinite scroll of ML-ranked videos
> - No fan-out needed — content indexed in recommendation engine
> - Watch-through rate is the dominant signal (not likes)
>
> The architectural implication is that Instagram must run **two complete distribution systems:**
>
> ```
> ┌─────────────────────────┐     ┌─────────────────────────┐
> │ SOCIAL-GRAPH PATH       │     │ RECOMMENDATION PATH      │
> │                         │     │                          │
> │ Used by: Feed, Stories  │     │ Used by: Reels, Explore  │
> │                         │     │                          │
> │ • Fan-out on write      │     │ • Content indexing       │
> │ • Redis feed inboxes    │     │ • Candidate generation   │
> │ • Social graph queries  │     │   (IG2Vec, two-tower,    │
> │ • Ranked by relationship│     │    FAISS)                │
> │   + recency + engagement│     │ • 3-stage ranking        │
> │                         │     │ • No fan-out needed      │
> │ Scale driver:           │     │ Scale driver:            │
> │   Follower count        │     │   Content catalog size   │
> │   (fan-out cost)        │     │   (indexing cost)        │
> └─────────────────────────┘     └─────────────────────────┘
> ```
>
> **Contrast with TikTok:** TikTok only runs the recommendation path. This is architecturally simpler — no fan-out infrastructure, no feed inboxes, no celebrity threshold. All engineering effort concentrated on one distribution system → better recommendations. Instagram's dual model is more complex but serves both use cases (friends' content + discovery).
>
> **Stories Highlights** — the interesting edge case:
>
> When a user adds a Story to Highlights, ephemeral content becomes permanent:
> 1. Copy media from ephemeral S3 bucket to Haystack (permanent)
> 2. Create permanent metadata record (no TTL)
> 3. Link to Highlights collection on profile
> 4. Original Story still expires after 24h
>
> This migration must happen before the TTL expires. If the user adds a Story to Highlights at hour 23, we have 1 hour to migrate the media before it's auto-deleted."

---

## PHASE 9: Wrap-Up (~3 min)

**Interviewer:**
Good design overall. Last question: if you were on-call for Instagram, what keeps you up at night?

**Candidate:**

> "Three things:
>
> **1. Celebrity-triggered cascading failures:**
> When a top celebrity (100M+ followers) posts + immediately goes viral, three systems spike simultaneously:
> - CDN: millions of users fetch the same image within seconds (request coalescing helps but edge caches need warming)
> - Notifications: millions of likes/comments → aggregation buffer must handle the throughput
> - Feed ranking: the post's engagement velocity signal spikes → model must not over-index on short-term popularity
>
> If any one of these systems degrades, it can cascade. The notification service backing up → increased latency on the MQTT broker → DM delivery slows → users perceive Instagram as broken.
>
> **2. Cross-DC consistency during failover:**
> TAO uses async replication (leader → follower). If the leader DC fails, recent writes (last few seconds) may be lost. For social graph mutations (follow/unfollow), this means:
> - A user followed someone, the edge was written to the leader, leader fails before replication → the follow is lost
> - When the leader comes back, state diverges from the new leader (promoted follower)
> - Reconciliation is non-trivial — need to detect and resolve conflicts
>
> **3. Recommendation feedback loops:**
> Reels recommendation can create filter bubbles — the model shows content the user engages with, the user engages more with that type, the model doubles down. Over time, the user's Reels feed becomes extremely narrow. This isn't a system reliability concern — it's a product/ethical concern. But it manifests as a system design problem: how do you inject exploration (diverse content) into a recommendation system optimized for exploitation (showing what works)?
>
> Instagram uses 'exploration slots' — a percentage of the Reels feed is reserved for content outside the user's typical interests. This trades short-term engagement for long-term user satisfaction and content diversity."

**Interviewer:**
Strong finish. You showed iterative architecture evolution, clear trade-off reasoning, and awareness of production concerns. Let's wrap.

---

### L5 vs L6 vs L7 — Wrap-Up Rubric

| Aspect | L5 (SDE-2) | L6 (SDE-3) | L7 (Principal) |
|---|---|---|---|
| **Operational Concerns** | "Server might crash" | Identifies cascading failures from celebrity events, cross-DC consistency edge cases, recommendation feedback loops | Additionally discusses blast radius containment (can we limit a failure to one DC? one service? one content type?), graceful degradation strategies (serve stale feed if ranking model is slow), canary deployments for ML model changes |
| **Trade-off Awareness** | Acknowledges trade-offs when asked | Proactively identifies trade-offs before the interviewer asks (dual distribution model cost, eventual consistency windows, exploration vs exploitation) | Frames trade-offs in terms of organizational cost (two distribution models = two teams, two on-call rotations, two monitoring dashboards) and proposes mitigation strategies |
| **System Maturity** | Focuses on feature completeness | Focuses on operational maturity: monitoring, alerting, failure modes, cache warming, capacity planning | Discusses system evolution: when to migrate to new storage systems, when to deprecate legacy paths, how to run parallel systems during migration without doubling operational burden |

---

## Supporting Deep-Dive Documents

| # | Document | Key Topic |
|---|---|---|
| 02 | [API Contracts](02-api-contracts.md) | Full API surface: 12 API groups, request/response shapes |
| 03 | [Media Processing Pipeline](03-media-processing-pipeline.md) | Photo/video upload, resize, transcode, moderation |
| 04 | [News Feed Generation](04-news-feed-generation.md) | Fan-out on write/read, hybrid approach, ML ranking |
| 05 | [Social Graph](05-social-graph.md) | TAO, directed graph, fan-out implications, hot shards |
| 06 | [Stories & Reels](06-stories-and-reels.md) | Ephemeral vs permanent, recommendation vs social-graph |
| 07 | [Content Delivery (CDN)](07-content-delivery-cdn.md) | Meta CDN, image optimization, HLS, caching tiers |
| 08 | [Search & Explore](08-search-and-explore.md) | Typeahead, Unicorn, Explore 3-stage pipeline, Reels ranking |
| 09 | [Data Storage & Caching](09-data-storage-and-caching.md) | TAO, Cassandra, Redis, Memcache, Haystack, f4, MyRocks |
| 10 | [Notifications & Real-Time](10-notifications-and-real-time.md) | MQTT, push notifications, aggregation, live video |
| 11 | [Scaling & Performance](11-scaling-and-performance.md) | Scale numbers, sharding, caching layers, latency budget |
| 12 | [Design Trade-offs](12-design-trade-offs.md) | Hybrid fan-out, algorithmic feed, directed graph, storage evolution |
