Design Instagram (Photo/Video Sharing Social Network) as a system design interview simulation.

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
Create all files under: src/hld/instagram/design/

## Files to create

### 1. 01-interview-simulation.md — the main backbone
The full interview dialogue covering all phases and attempts.

### 2. 02-api-contracts.md — Instagram Platform APIs

This doc should list all the major API surfaces of an Instagram-like photo/video sharing platform. The interview simulation (Phase 3) will only cover a subset — this doc is the comprehensive reference.

**Structure**: For each API group, list every endpoint with HTTP method, path, request/response shape, and a brief description. Mark endpoints covered in the interview with a star or highlight.

**API groups to cover**:

- **Post APIs**: The core content creation path. `POST /posts` (create post — photo/video/carousel upload, caption, location tag, user tags, alt text), `GET /posts/{postId}` (full post detail: media URLs, caption, like count, comment count, timestamp, location), `DELETE /posts/{postId}`, `PUT /posts/{postId}` (edit caption/tags). Support for multiple media types: single photo, carousel (up to 10 photos/videos), video (up to 60 seconds for feed). Media is uploaded separately via a resumable upload endpoint, then referenced by media IDs in the post creation call.

- **Feed APIs**: The heart of the product. `GET /feed` (personalized home feed — paginated, cursor-based, ranked by ML model). Feed is NOT chronological — it's ranked by predicted interest. Each feed item includes: post data, author info, engagement counts, whether current user has liked/saved. `GET /feed/following` (chronological feed of followed accounts — added in 2022 as an option). Feed generation is the most architecturally interesting component — fan-out on write vs fan-out on read is the core design decision.

- **Stories APIs**: `POST /stories` (create story — photo/video, 15-second max per clip, stickers, music, polls, questions), `GET /stories/feed` (stories tray — ordered list of users who have active stories, ranked by closeness), `GET /stories/{userId}` (get all active stories for a user), `DELETE /stories/{storyId}`. Stories are **ephemeral** — auto-deleted after 24 hours. This has storage and TTL implications. Stories tray ordering is personalized.

- **Reels APIs**: `POST /reels` (create reel — short-form video up to 90 seconds, audio track, effects), `GET /reels/feed` (Reels tab — endless scroll of recommended short-form videos, TikTok-style), `GET /reels/{reelId}`. Reels feed is recommendation-driven (not social-graph-driven). Users mostly see content from accounts they do NOT follow. This is a fundamentally different feed paradigm from the home feed.

- **Social Graph APIs**: `POST /users/{userId}/follow` (follow a user), `DELETE /users/{userId}/follow` (unfollow), `GET /users/{userId}/followers` (paginated follower list), `GET /users/{userId}/following` (paginated following list), `GET /users/{userId}/mutual-followers` (mutual connections). The social graph is **directed** (A follows B doesn't mean B follows A). Celebrity accounts can have 500M+ followers — the follower list must handle extreme fan-out. `POST /users/{userId}/block`, `POST /users/{userId}/restrict`.

- **Engagement APIs**: `POST /posts/{postId}/like`, `DELETE /posts/{postId}/like` (unlike), `POST /posts/{postId}/comments` (add comment), `GET /posts/{postId}/comments` (paginated, threaded comments — supports replies to comments), `DELETE /comments/{commentId}`, `POST /posts/{postId}/save` (save/bookmark), `DELETE /posts/{postId}/save` (unsave), `POST /posts/{postId}/share` (share via DM or to Stories).

- **Search & Explore APIs**: `GET /search?q={query}&type={users|tags|places}` (search users, hashtags, locations), `GET /search/suggestions?q={prefix}` (typeahead autocomplete), `GET /explore` (Explore page — grid of recommended posts from accounts you don't follow, personalized). Explore is recommendation-driven, similar to Reels but for photos. `GET /tags/{tagName}/posts` (posts with a specific hashtag), `GET /locations/{locationId}/posts`.

- **Direct Messaging APIs**: `GET /inbox` (conversation list), `POST /messages` (send message — text, photo, video, post share, voice note), `GET /conversations/{conversationId}/messages` (paginated message history), `PUT /messages/{messageId}/react` (emoji reactions on messages), `DELETE /messages/{messageId}` (unsend). DMs support group chats (up to 32 members), disappearing messages, read receipts. Real-time delivery via persistent connections (WebSocket or MQTT).

- **Notification APIs**: `GET /notifications` (activity feed — likes, comments, follows, mentions, tagged), `PUT /notifications/settings` (configure notification preferences per type). Notifications are delivered in real-time via push (APNs/FCM) and available via pull (activity feed). Must handle thundering herd: a celebrity post can generate millions of likes/comments within minutes.

- **User Profile APIs**: `GET /users/{userId}` (profile info: bio, avatar, post count, follower/following count, public/private flag), `PUT /users/me` (edit profile), `GET /users/{userId}/posts` (user's post grid — paginated). `PUT /users/me/avatar` (update profile picture). Private accounts require follow requests: `POST /users/{userId}/follow-request`, `PUT /follow-requests/{requestId}/approve`, `PUT /follow-requests/{requestId}/deny`.

- **Media Upload APIs** (internal): `POST /media/upload/init` (initialize resumable upload — returns uploadId), `PUT /media/upload/{uploadId}` (upload chunk), `POST /media/upload/{uploadId}/finalize` (finalize upload, trigger processing pipeline). Supports photos (JPEG, PNG, HEIF) and videos (MP4, MOV). Server-side processing: resize, compress, generate thumbnails, extract video frames, apply filters if requested.

- **Content Moderation APIs** (internal): `POST /moderation/review` (submit content for automated review), `GET /moderation/queue` (human review queue), `PUT /moderation/{contentId}/action` (take action: remove, restrict, age-gate). Automated moderation uses ML models to detect: nudity, violence, hate speech, spam, fake accounts.

**Contrast with Twitter's API model**: Twitter is text-first (280 chars) with optional media. Instagram is media-first (photo/video required, caption optional). Twitter's feed is primarily chronological with algorithmic ranking as an option. Instagram's feed is fully algorithmic by default. Twitter has retweets (resharing); Instagram has no native reshare in feed (only share to Stories/DMs). Twitter's social graph is lighter-weight (text fan-out is cheap); Instagram's media fan-out is heavier (photo/video URLs, thumbnails).

**Contrast with TikTok**: TikTok's primary feed is 100% recommendation-driven (For You page) — users see content from strangers. Instagram's home feed is social-graph-driven (posts from followed accounts). Instagram's Reels tab copies TikTok's model. TikTok has no "following" feed as a primary surface. TikTok's content is exclusively short-form video; Instagram supports photos, carousels, Stories, Reels, and long-form video.

**Interview subset**: In the interview (Phase 3), focus on: post creation (upload + processing pipeline), feed generation (the fan-out problem), Stories (ephemeral content with TTL), and social graph (follow/unfollow, fan-out implications). The full API list lives in this doc.

### 3. 03-media-processing-pipeline.md — Photo & Video Processing

The upload and processing pipeline is the write path's most critical component. This doc should cover:

- **Photo processing pipeline**: User uploads a photo → server receives raw image (JPEG/HEIF/PNG, up to 30MB) → pipeline: decode → strip EXIF metadata (privacy) → auto-orient based on EXIF rotation → resize to multiple resolutions (thumbnail 150×150, small 320px, medium 640px, large 1080px) → compress with quality optimization (Instagram targets ~75% JPEG quality for balance of size vs quality) → apply filters if selected (server-side filter application for consistency across devices) → generate blurhash placeholder (low-res placeholder shown while loading) → upload all variants to object storage (S3) → store media metadata in database → return media IDs.
- **Video processing pipeline**: Similar to photo but more complex. User uploads video (MP4/MOV, up to 60s for feed, 90s for Reels, 15s per Story clip) → pipeline: extract metadata (duration, resolution, codec, audio) → transcode to multiple resolutions (360p, 480p, 720p, 1080p) using H.264 (universal) and potentially VP9/AV1 for bandwidth savings → generate HLS segments (for adaptive streaming) → extract keyframes for thumbnail generation → generate preview thumbnails at regular intervals → compress audio (AAC) → upload all variants to S3.
- **Thumbnail generation**: Every post needs a thumbnail for the grid view (profile page, Explore page, hashtag pages). Photos: center-crop to square (1:1). Videos: extract first few frames, pick the most visually interesting frame using saliency detection, or let user select cover frame.
- **Content-aware processing**: Instagram uses ML to auto-adjust brightness, contrast, and sharpness. Face detection for auto-focus and smart cropping. Object detection for generating alt text (accessibility).
- **Carousel handling**: Up to 10 photos/videos per carousel post. Each media item goes through the full pipeline independently. All items must complete processing before the post is published. Pipeline must handle mixed media (some photos, some videos in one carousel).
- **Processing infrastructure**: Async processing via task queue (similar to Celery/SQS workers). Post is created with status "processing" → media pipeline runs asynchronously → on completion, post status flips to "published" → feed fan-out begins. User sees a progress indicator while processing.
- **Scale numbers**: Instagram has **2+ billion monthly active users**, **100+ million photos/videos uploaded per day**. Processing pipeline must handle ~1,200 uploads/second sustained, with peaks much higher.
- **Contrast with YouTube**: YouTube processes long-form video (hours) with per-title encoding optimization. Instagram processes short-form content (seconds to minutes) with fixed encoding ladders — per-title optimization is infeasible at 100M+ uploads/day. YouTube prioritizes encoding quality; Instagram prioritizes processing speed (users expect near-instant publishing).
- **Contrast with Snapchat**: Snapchat processes ephemeral content — storage optimization matters even more because content is viewed briefly and deleted. Instagram Stories are similar (24-hour TTL) but Instagram also has permanent posts that need long-term multi-resolution storage.

### 4. 04-news-feed-generation.md — Feed Ranking & Delivery

This is THE core system design problem for Instagram. How does the feed get assembled and delivered to 2B+ users?

- **The fundamental problem**: User opens Instagram. We need to show them a ranked feed of posts from accounts they follow. A user may follow 500 accounts, each posting at different rates. We need to collect, rank, and serve this feed in <200ms.
- **Fan-out on Write (push model)**:
  - When a user creates a post, immediately write a reference (postId, timestamp) to every follower's feed inbox (a pre-materialized feed list in cache/DB).
  - On read: just fetch the user's pre-built feed inbox. Super fast reads.
  - Problem: **celebrity fan-out**. Cristiano Ronaldo has 650M+ followers. Writing to 650M inboxes on every post is extremely expensive and slow. Fan-out latency: minutes to hours for mega-celebrities.
  - Instagram's approach for most users: fan-out on write for users with fewer followers (say <500K). This covers 99%+ of users.
- **Fan-out on Read (pull model)**:
  - On read: collect the latest posts from all accounts the user follows, merge, rank, return.
  - No write amplification — posting is cheap.
  - Problem: slow reads. If a user follows 500 accounts, we need 500 lookups, merge, rank — at request time. Latency is too high for the common case.
  - Instagram's approach for celebrities: fan-out on read for users with millions of followers. Their posts are fetched at read time and merged into followers' feeds.
- **Hybrid approach (what Instagram actually does)**:
  - **Fan-out on write** for normal users (write to followers' feed inboxes in Redis/Memcached).
  - **Fan-out on read** for celebrities/high-follower accounts (merge at read time).
  - Threshold: ~500K-1M followers (exact threshold is tuned based on system load).
  - This is the classic solution taught in system design interviews, and it's what Instagram/Twitter actually use.
- **Feed ranking (ML model)**:
  - Instagram's feed has NOT been chronological since 2016. Posts are ranked by predicted interest.
  - Ranking signals: relationship closeness (how often you interact with this person), post recency, post type (photo vs video vs carousel), engagement velocity (how fast the post is getting likes), content topic relevance, user's historical engagement patterns.
  - Two-pass ranking: (1) candidate generation — collect ~500 candidate posts from fan-out inbox + celebrity pull, (2) lightweight ML model scores each candidate, (3) top ~50 are returned as the first page.
  - The ranking model is retrained regularly on engagement data: likes, comments, saves, time-spent-viewing, profile visits after viewing a post.
- **Feed pagination**: Cursor-based pagination (NOT offset-based). The cursor encodes the last seen post's score/timestamp. This handles the "new posts inserted while paginating" problem gracefully.
- **Feed invalidation**: When a user unfollows someone, their posts should disappear from the feed. When a post is deleted, it must be removed from all feed inboxes it was fanned out to. This is the reverse fan-out problem — expensive but necessary.
- **Contrast with Twitter**: Twitter uses a similar hybrid fan-out approach. Twitter's "For You" tab is fully algorithmic; "Following" tab is chronological. Instagram went algorithmic-only initially (2016), then added a "Following" chronological option (2022) after user backlash.
- **Contrast with TikTok**: TikTok's For You page is NOT based on social graph at all. It's a pure recommendation engine — content is surfaced based on viewing behavior, not who you follow. Instagram's home feed is fundamentally social-graph-based with algorithmic ranking ON TOP. Instagram's Reels tab is the TikTok-style recommendation feed.
- **Contrast with Facebook**: Facebook's News Feed was the pioneer of algorithmic feeds (EdgeRank, 2009). Instagram adopted the same philosophy. Key difference: Facebook has a richer interaction model (reactions, shares, comments, groups, pages) providing more ranking signals. Instagram's ranking relies more on visual engagement signals (time spent viewing, double-tap like, save).

### 5. 05-social-graph.md — Social Graph Storage & Fan-out

The social graph is the backbone of Instagram. Every follow, unfollow, block, and mute is a graph operation.

- **Graph structure**: Directed graph. A→B means "A follows B." Edges have metadata: timestamp, notification preferences, close-friends flag. Instagram's graph is asymmetric (unlike Facebook's symmetric friendship).
- **Scale**: 2B+ users (nodes), hundreds of billions of edges (follow relationships). Some nodes have extreme degree: 650M+ followers (Cristiano Ronaldo), users following 7,500 accounts (Instagram's following limit).
- **Storage options**:
  - **Adjacency list in key-value store**: For each user, store: `followers:{userId} → Set[followerId]` and `following:{userId} → Set[followeeId]`. Simple, fast lookups. Instagram uses this model backed by Cassandra + caching.
  - **Graph database (e.g., TAO at Facebook/Meta)**: Facebook/Meta built TAO — a distributed graph store optimized for the social graph. TAO stores objects (nodes) and associations (edges) with a cache layer on top of MySQL. Instagram, being part of Meta, likely uses TAO or a TAO-like system for its social graph.
  - **Why not a relational DB?** A `follows` table with (follower_id, followee_id) works at small scale. At Instagram's scale (hundreds of billions of rows), JOINs become prohibitive. Sharding a relational follows table is complex — do you shard by follower_id or followee_id? Either way, one direction requires cross-shard queries.
- **Fan-out implications**: The social graph directly determines fan-out cost. When user A posts, we must look up A's follower list and write to each follower's feed inbox. For a user with 1,000 followers: 1,000 writes. For a celebrity with 100M followers: 100M writes. The social graph's degree distribution (power-law) is why the hybrid fan-out approach exists.
- **Mutual followers / mutual connections**: `GET /users/{userId}/mutual-followers` requires intersection of two follower sets. At scale, this is done by precomputing or by caching follower sets and intersecting in-memory.
- **Close Friends**: Instagram's "Close Friends" feature creates a subgraph — a curated list of followers who see special Stories. This is a labeled edge in the graph (close_friend: true/false).
- **Privacy (private accounts)**: Private accounts require follow requests. The graph edge is in a "pending" state until approved. Feed fan-out only happens for confirmed followers.
- **Contrast with Twitter**: Twitter's social graph is also directed but simpler (no private accounts requiring approval, no Close Friends subgraph). Twitter has a 5,000 following limit (lower than Instagram's 7,500). Twitter has lists (curated subsets of following); Instagram doesn't.
- **Contrast with Facebook**: Facebook's social graph is undirected (symmetric friendship). This simplifies fan-out (both parties always see each other's content) but requires mutual consent. Instagram's directed graph allows one-way following, enabling the creator/audience model.

### 6. 06-stories-and-reels.md — Ephemeral & Short-Form Content

Two distinct content formats with different system design implications.

- **Stories (ephemeral content)**:
  - 24-hour TTL — auto-deleted after expiration. This is the defining characteristic.
  - Storage optimization: since content expires, aggressive cleanup is possible. Use TTL in storage (Cassandra columns with TTL, S3 lifecycle policies).
  - Stories tray: the row of circles at the top of the feed. Ordered by recency + relationship closeness. Loading the tray requires: (1) fetch list of followed accounts with active stories, (2) rank by closeness, (3) prefetch first story from top-N accounts.
  - Stories are viewed sequentially (swipe through), not scrolled. This changes the data access pattern — prefetch the next user's stories while current user's stories are being viewed.
  - **Seen state tracking**: Track which stories each user has seen. Scale: if 500M users view stories daily, and each user sees stories from ~50 accounts, that's 25B seen-state entries per day. Must be stored efficiently (bitmap or bloom filter per user, not individual rows).
  - Stories highlights: Users can save stories permanently as "Highlights" on their profile. This converts ephemeral content to permanent content — media must be moved from TTL-enabled storage to permanent storage.
  - Interactive elements: Polls, questions, quizzes, countdowns, music — each requires real-time aggregation and display. Poll results must update in near-real-time as viewers vote.
- **Reels (short-form video)**:
  - Up to 90 seconds. Always vertical (9:16 aspect ratio).
  - **Recommendation-driven distribution**: Unlike feed posts (distributed to followers), Reels are distributed to ANYONE based on predicted interest. This is a fundamentally different distribution model — closer to TikTok than to Instagram's traditional social-graph model.
  - Reels feed is an infinite scroll of full-screen videos. Prefetching is critical — buffer the next 2-3 Reels while user watches current one.
  - **Audio/music**: Reels have an audio layer (trending sounds, original audio). Need an audio catalog, audio fingerprinting (to identify songs), and tracking of trending sounds.
  - **Duets/Remixes**: Users can create Reels alongside or in response to other Reels. This creates content chains — need to track parent-child relationships between Reels.
  - Video processing for Reels: transcode to multiple resolutions, generate HLS segments for adaptive streaming, extract thumbnails, run content moderation ML models (nudity, violence, copyright).
- **Contrast with TikTok**: TikTok is Reels-only — no photo posts, no permanent feed, no Stories (TikTok Stories was deprecated). TikTok's entire product is the recommendation-driven video feed. Instagram has Reels as ONE tab among many (Feed, Stories, Explore, Reels, Shop, Profile). TikTok's recommendation algorithm is widely considered superior — it uses a more aggressive exploration strategy and has more engagement signal density (videos are short → more signals per minute of usage).
- **Contrast with Snapchat**: Snapchat pioneered ephemeral content (Stories, disappearing messages). Instagram famously copied Stories in 2016. Key architectural difference: Snapchat was built ephemeral-first (everything expires by default); Instagram bolted ephemeral content onto a platform designed for permanent content. This creates storage complexity — Instagram must manage both permanent and ephemeral content pipelines.

### 7. 07-content-delivery-cdn.md — CDN & Media Serving

Instagram serves billions of images and videos daily. CDN strategy is critical for performance.

- **CDN architecture**: Instagram (as part of Meta) uses **Meta's global CDN infrastructure** — a combination of owned edge PoPs (Points of Presence) and peering agreements. Unlike Netflix's Open Connect (purpose-built for video), Meta's CDN serves a mix of content types: images, videos, static assets, API responses.
- **Image serving optimization**:
  - Multiple resolutions stored per image (150px, 320px, 640px, 1080px). Client requests the appropriate size based on device screen and connection quality.
  - **Progressive JPEG**: Images load in layers — blurry preview first, then sharpening. Better perceived performance than baseline JPEG (which loads top-to-bottom).
  - **WebP/AVIF**: Modern image formats. WebP gives ~25-30% smaller files than JPEG at same quality. AVIF gives ~50% savings. Serve based on client support (Accept header).
  - **Blurhash placeholders**: When scrolling feed, show a colored blur placeholder (encoded as a short string, ~20-30 bytes) while the real image loads. Eliminates the jarring "blank box" experience.
- **Video serving**: HLS adaptive bitrate streaming for feed videos and Reels. Similar to Netflix's ABR approach but with simpler encoding profiles (Instagram doesn't need per-title optimization at 100M+ uploads/day).
- **CDN caching strategy**:
  - Hot content (recent posts from popular accounts, trending Reels): cached at edge PoPs close to users. Very high cache hit ratio.
  - Warm content (older posts, moderate-popularity accounts): cached at regional mid-tier caches.
  - Cold content (old posts, rarely viewed): served from origin (S3). Cache miss → fetch from origin → cache at edge.
  - Unlike Netflix (proactive push), Instagram uses **reactive caching** because the content volume is too large and the access patterns too unpredictable (user-generated content follows a long-tail distribution).
- **Image URL structure**: Instagram uses CDN URLs with embedded metadata: content hash (for cache busting), size variant, format, and a signed token (expiration + access control). URLs are typically long-lived but can be invalidated.
- **Contrast with Netflix**: Netflix builds its own CDN (Open Connect) with OCAs inside ISPs. Instagram/Meta uses a traditional CDN model with owned edge PoPs + commercial CDN partnerships. Netflix's curated catalog enables proactive push; Instagram's UGC volume requires reactive caching. Netflix serves mostly video (heavy bandwidth); Instagram serves mostly images (lighter per-request, but enormous volume).
- **Contrast with Twitter**: Twitter serves mostly text with optional media. Twitter's CDN load is lighter — the bottleneck is tweet delivery and timeline assembly, not media serving. Instagram's CDN must handle 10x-100x the media bandwidth.

### 8. 08-search-and-explore.md — Search, Explore & Recommendations

Discovery beyond the social graph — how users find new content and accounts.

- **Search**:
  - Three search types: users (by username/name), hashtags, places/locations.
  - **Typeahead / autocomplete**: As user types, show matching results in real-time. Must return results in <100ms. Uses prefix index (trie or inverted index with prefix matching). Results are personalized — accounts you've interacted with rank higher.
  - **Hashtag search**: `#travel` → posts tagged with #travel, sorted by Top (engagement-ranked) or Recent (chronological). Top posts for popular hashtags are precomputed.
  - **Location search**: Posts geotagged at a specific location. Uses spatial index (geohash or R-tree).
  - Powered by Elasticsearch or a custom search index.
- **Explore page**:
  - A grid of recommended posts/Reels from accounts the user does NOT follow. Entirely recommendation-driven.
  - **Candidate generation**: From the pool of all recent public posts, generate ~10K candidates relevant to the user. Signals: topics the user engages with, accounts similar to accounts the user follows, posts engaged with by people the user follows (collaborative filtering).
  - **Ranking**: ML model scores each candidate by predicted engagement (P(like), P(save), P(comment), P(share)). Blend scores into a final ranking. Top ~100 are shown, paginated.
  - **Diversity and freshness**: Without constraints, the model would show only the most popular content. Instagram adds diversity rules: no more than N posts from the same account, mix content types (photos, videos, Reels), inject fresh content that hasn't had time to accumulate engagement.
  - **Content safety**: Filter out content flagged by moderation models before ranking. Sensitive content (e.g., borderline-allowed but potentially harmful) gets reduced distribution even if it passes moderation.
- **Recommendations for Reels feed**: Similar to Explore but exclusively video. The Reels recommendation engine is Instagram's answer to TikTok's For You page. Key signals: watch-through rate (did user watch the full Reel?), replays, shares, audio usage (trending sounds). Watch-through rate is the strongest signal — a user watching a 30-second Reel to completion is a much stronger interest signal than a quick "like."
- **Contrast with TikTok**: TikTok's recommendation is widely regarded as best-in-class. Key differences: TikTok uses a more aggressive exploration strategy (shows content from unknown creators more aggressively), TikTok's content is exclusively short video (simpler ranking model), TikTok has more signal density per minute of usage (short videos = more implicit feedback). Instagram must balance recommendations with social-graph content (Reels tab is recommendations, but home feed is social-graph).
- **Contrast with YouTube**: YouTube's recommendation optimizes for watch time (longer sessions = more ads). Instagram's Explore/Reels recommendation optimizes for engagement and session frequency (get users to open the app more often, not necessarily spend hours in one session). YouTube's content is long-form → fewer but stronger signals per session; Instagram's Reels are short-form → many weak signals per session.

### 9. 09-data-storage-and-caching.md — Data Storage & Caching

- **PostgreSQL (early days)**: Instagram famously ran on PostgreSQL in its early days (2010-2012). The initial architecture used a single PostgreSQL database for users, posts, likes, comments, follows. When Instagram was acquired by Facebook (2012), it had 40M users on ~12 database servers.
- **Cassandra**: As Instagram scaled, Cassandra was adopted for high-write-throughput data: feed inboxes (fan-out writes), activity/notifications, direct message histories. Cassandra's strengths: linearly scalable writes, tunable consistency, time-series-friendly data model (great for feeds and timelines).
- **TAO (Facebook/Meta's graph store)**: Instagram likely uses TAO for social graph storage (follows, likes, comments). TAO is a distributed data store for the social graph built on top of MySQL with a massive cache layer (Memcache-based). Optimized for the "objects and associations" model — perfect for "User A follows User B" and "User A likes Post P."
- **MySQL + MyRocks**: Meta uses MySQL with the MyRocks storage engine (RocksDB-based) for structured data. Better compression and write performance than InnoDB for Meta's workloads. Used for user accounts, post metadata, content metadata.
- **Memcache (TAO cache layer + general caching)**: Meta's Memcache deployment is legendary — thousands of servers, trillions of operations per day. Used for: social graph caching (TAO), session data, feed caching, feature flags, configuration.
- **Redis**: Used for feed inboxes (sorted sets of postIds per user), real-time counters (like counts, view counts), rate limiting, and ephemeral data (Stories seen state). Redis sorted sets are ideal for feed inboxes: `ZADD feed:{userId} {timestamp} {postId}` with `ZREVRANGE` for retrieval.
- **Amazon S3 / Meta's blob storage (Haystack/f4)**: All media (photos, videos, thumbnails) stored in blob storage.
  - **Haystack**: Meta's custom photo storage system. Designed to minimize metadata overhead for billions of small images. Traditional filesystems waste I/O on metadata lookups — Haystack stores multiple images in a single large file with an in-memory index. Dramatically reduces disk I/O per image read.
  - **f4 (warm storage)**: Warm blob storage for older, less-frequently-accessed media. Uses Reed-Solomon erasure coding for space efficiency (1.4x replication factor vs 3x for hot storage). Saves ~65% storage cost for cold content.
- **Elasticsearch**: Search index for users, hashtags, locations. Also used for content moderation (searching for banned content patterns).
- **Data pipeline**: Instagram generates massive amounts of behavioral data: impressions, taps, scrolls, time-on-screen. This data flows through Scribe (Meta's log transport) → data warehouse (Hive/Presto on HDFS) → ML training pipelines. Real-time data flows through a stream processing system for live dashboards and real-time feature computation.
- **Scale numbers**: 2B+ MAU, 100M+ photos/videos uploaded per day, ~500M Stories created per day, estimated hundreds of petabytes of media storage, trillions of cache operations per day.
- **Contrast with Twitter**: Twitter stores primarily text (small payloads, high volume). Twitter's storage bottleneck is timeline assembly and delivery, not media storage. Twitter uses Manhattan (internal KV store) and eventually consistent systems for timelines. Instagram's bottleneck is media storage and serving.
- **Contrast with Netflix**: Netflix stores tens of thousands of titles (curated) but each title at ~120 encoding profiles → large per-title storage. Instagram stores billions of items but each item is small (a few MB for photos, tens of MB for short videos). Different storage shape: Netflix = few items × many variants; Instagram = many items × few variants.

### 10. 10-notifications-and-real-time.md — Notifications & Real-Time Features

- **Push notifications**: Delivered via APNs (Apple) and FCM (Google). Types: likes, comments, follows, mentions, tagged, live video, Stories reactions. Each notification type has user-configurable preferences.
- **Thundering herd problem**: When a celebrity posts, millions of followers will like/comment within minutes. Each like generates a notification for the post author. Naive approach: 1M likes → 1M individual notifications → phone explodes. Solution: **aggregation**. "user1, user2, and 999,998 others liked your post." Aggregate notifications within a time window (e.g., batch every 30 seconds). Only send one push notification per batch.
- **Real-time feed updates**: When the app is open, new posts should appear without requiring a pull-to-refresh. Options: (1) short polling (client polls every N seconds — simple but wasteful), (2) long polling, (3) Server-Sent Events (SSE), (4) WebSocket. Instagram uses a **persistent connection** (MQTT-based for mobile, WebSocket for web) for real-time updates: new posts, typing indicators in DMs, Stories updates, live video notifications.
- **MQTT (Meta's approach)**: Meta uses MQTT (lightweight pub/sub messaging protocol) for mobile real-time communication. MQTT is designed for unreliable mobile networks — small packet overhead, persistent connections with keep-alive, QoS levels. Instagram's real-time notifications, DM delivery, and typing indicators all flow over MQTT.
- **Activity feed**: The notifications/activity tab. Displays recent interactions. Data model: `{type, actor, target, timestamp}`. Example: `{type: "like", actor: userId123, target: postId456, timestamp: ...}`. Stored in a time-series-friendly store (Cassandra or Redis sorted sets). Paginated by cursor.
- **Live video**: Instagram Live — real-time video streaming to followers. Uses RTMP for ingest (creator → server) and HLS/DASH for distribution (server → viewers). Viewers can comment and react in real-time. Different from pre-recorded content — requires real-time transcoding and distribution with <5 second latency.
- **Contrast with Twitter**: Twitter has a similar notification system but with higher volume per event (retweets cascade — a retweet of a popular tweet can generate notifications for millions). Twitter also uses a persistent connection for real-time tweet delivery.
- **Contrast with WhatsApp**: WhatsApp (also Meta) uses end-to-end encryption and guarantees message delivery. Instagram DMs do not use E2E encryption by default (opt-in). WhatsApp's real-time infrastructure is optimized for guaranteed delivery; Instagram's is optimized for best-effort with high throughput.

### 11. 11-scaling-and-performance.md — Scaling & Performance

Cross-cutting doc that ties together scaling decisions from all deep dives.

- **Scale numbers**:
  - **2+ billion monthly active users** (as of 2024).
  - **500+ million daily active users** of Stories.
  - **100+ million photos/videos uploaded per day**.
  - **~4.2 billion likes per day** (estimated).
  - **Media storage**: Hundreds of petabytes of photos and videos.
  - **Feed reads**: Billions of feed loads per day.
- **Read vs write asymmetry**: Instagram is read-heavy. A single post is read (viewed in feeds, profiles, Explore) thousands to millions of times but written once. Ratio: easily 1:100,000+ for popular content. This justifies fan-out on write (expensive write → cheap reads) and aggressive caching.
- **Database sharding strategy**:
  - **User-based sharding**: Most data (posts, feeds, profiles) is sharded by userId. All of a user's data lives on the same shard → single-shard queries for profile and user-specific feed operations.
  - **Post-based sharding**: Post metadata can be sharded by postId for direct lookups. Trade-off: fetching a user's posts requires a cross-shard query unless you also store a userId→postIds index.
  - **Celebrity sharding**: High-follower accounts create hot shards. Solution: dedicated shards or further sub-sharding for celebrity data.
- **Caching strategy**: Multi-layer caching:
  - **Client-side cache**: App caches feed data, profile data, and images locally. Reduces API calls on app open.
  - **CDN cache**: Media (images, videos) cached at edge PoPs. >90% cache hit rate for hot content.
  - **Application cache (Memcache/Redis)**: Feed inboxes, social graph edges, user profiles, post metadata. Trillions of ops/day.
  - **Database query cache**: Frequently accessed queries cached at the DB proxy layer.
- **Feed latency budget**: From "user opens app" to "feed rendered" should be <500ms. Budget: DNS (~20ms) → API call (~50ms) → feed assembly from cache (~50ms) → media URL resolution (~20ms) → first image download from CDN (~200ms) → render (~50ms). Every component on this path is optimized for latency.
- **Handling viral content**: When a post goes viral, it generates: massive fan-out (likes, comments, shares), thundering herd on media CDN (everyone viewing the same image/video), notification storms for the author. Solutions: rate-limit notification delivery, pre-cache viral content at CDN edge, use approximate counters (HyperLogLog) for view counts during spikes.
- **Multi-datacenter / multi-region**: Instagram runs across multiple Meta data centers. Active-active configuration — all data centers serve production traffic. Data replication: Cassandra (async multi-DC), TAO (async leader-follower with cache invalidation), MySQL (async replication with leader in one region).
- **Contrast with Netflix**: Netflix's scaling challenge is CDN bandwidth (video streaming is bandwidth-heavy). Instagram's scaling challenge is fan-out and metadata operations (billions of graph lookups, feed assemblies, and small media serves per day). Different bottlenecks require different optimization strategies.
- **Contrast with Twitter**: Twitter's scale challenge is timeline fan-out for viral tweets (a single tweet can reach 100M+ timelines). Instagram has a similar challenge but with heavier payloads (media URLs, thumbnails, multiple image formats). Twitter optimized for text delivery speed; Instagram optimized for media delivery quality.

### 12. 12-design-trade-offs.md — Design Philosophy & Trade-off Analysis

Opinionated analysis of Instagram's design choices — not just "what" but "why this and not that."

- **Hybrid fan-out (write + read) vs pure fan-out on write vs pure fan-out on read**: Instagram uses hybrid — fan-out on write for normal users, fan-out on read for celebrities. Pure fan-out on write breaks at celebrity scale (650M follower writes per post). Pure fan-out on read is too slow for the common case (assembling feed from 500 sources at read time). The hybrid approach adds complexity (two code paths, routing logic based on follower count) but handles both extremes.
- **Algorithmic feed vs chronological feed**: Instagram switched to algorithmic ranking in 2016. Rationale: users were missing 70% of posts in their chronological feed (couldn't keep up with volume). Algorithmic ranking ensures the most relevant posts surface first. Trade-off: users lose the sense of "completeness" and control. Instagram added a chronological "Following" tab in 2022 as a compromise.
- **Directed social graph vs undirected**: Instagram uses a directed graph (follow, not friend). This enables the creator/audience model — Cristiano Ronaldo can have 650M followers without following them back. Undirected (Facebook-style friendship) would limit this asymmetry. Trade-off: directed graphs have harder fan-out analysis (follower count varies wildly).
- **Reactive CDN caching vs proactive push**: Instagram uses reactive caching because UGC volume makes proactive push infeasible (100M+ uploads/day with long-tail access patterns). Netflix uses proactive push because its curated catalog has predictable demand. Trade-off: reactive caching has cache misses on first access; proactive push wastes bandwidth pushing content that might not be requested.
- **Ephemeral content (Stories) vs permanent content**: Instagram supports both. Ephemeral content requires TTL-based storage, which is cheaper but adds complexity (two storage tiers, migration for Highlights). Snapchat is ephemeral-only (simpler storage model but less content longevity). Instagram's dual model increases engineering complexity but captures both use cases.
- **Reels (recommendation-driven) vs Feed (social-graph-driven)**: Instagram runs two fundamentally different content distribution systems. The home feed is social-graph-based (you see content from people you follow). The Reels tab is recommendation-based (you see content from anyone, optimized for engagement). These require different infrastructure: feed uses fan-out from social graph; Reels uses a recommendation engine that indexes all public content. Running both is expensive but necessary to compete with TikTok.
- **Haystack/f4 (custom blob storage) vs off-the-shelf object storage (S3)**: Meta built Haystack because standard filesystems waste I/O on metadata for billions of small files. The overhead of opening a file, reading inode, then reading data adds up at billion-photo scale. Haystack eliminates this by packing multiple photos into large files with an in-memory index → one disk I/O per photo read. Trade-off: custom storage is expensive to build and maintain, but at Meta's scale, the I/O savings justify it.
- **MQTT vs WebSocket for mobile real-time**: Meta chose MQTT for mobile because it's designed for unreliable networks (mobile), has tiny packet overhead (2 bytes minimum header), supports QoS levels, and maintains persistent connections efficiently. WebSocket is better for web (browser-native support). Trade-off: MQTT requires a custom client library on mobile; WebSocket is universally supported.
- **PostgreSQL → Cassandra → TAO evolution**: Instagram started with PostgreSQL (simple, familiar, ACID). Scaled to Cassandra for write-heavy workloads (feeds, activity). Adopted TAO for social graph operations (optimized for objects + associations pattern). Each migration added operational complexity but solved a specific scaling bottleneck. Lesson: there's no one-size-fits-all database — use the right tool for each access pattern.
- **Approximate counting vs exact counting**: Instagram uses approximate counters (like HyperLogLog for view counts, eventual consistency for like counts) during high-traffic events. A like count being off by 0.1% is imperceptible to users. Exact counting would require serialized writes (bottleneck) or distributed transactions (slow). Trade-off: simpler system, higher throughput, at the cost of slight inaccuracy that no user will ever notice.

## CRITICAL: The design must be Instagram-centric
Instagram is the reference implementation. The design should reflect how Instagram actually works — its feed generation (hybrid fan-out), social graph (directed, TAO-based), media processing pipeline, CDN strategy (Meta's infrastructure), Stories (ephemeral, TTL-based), Reels (recommendation-driven), and storage evolution (PostgreSQL → Cassandra → TAO + Haystack). Where Twitter, TikTok, or other social platforms made different design choices, call those out explicitly as contrasts.

## CRITICAL: Iterative build-up (DO NOT skip this)
The architecture MUST evolve iteratively. Each attempt builds on the previous one by identifying a concrete problem and solving it. The candidate should NOT jump to the final architecture. The progression should feel natural:

### Attempt 0: Single server serving photos from disk
- A web server with a SQL database. Users upload photos to local disk. Feed is a `SELECT * FROM posts WHERE author_id IN (SELECT followee_id FROM follows WHERE follower_id = ?) ORDER BY created_at DESC`.
- **Problems found**: Single point of failure, local disk runs out of space, SQL query for feed is O(following_count × posts_per_user), can't handle concurrent uploads, photos served from application server (blocks request threads), no thumbnails (full-size images sent to mobile).

### Attempt 1: Separate media storage + basic processing
- **Object storage (S3)** for photos — separates media storage from application server.
- **Media processing workers**: Resize uploaded photos to multiple resolutions (thumbnail, small, medium, large). Store all variants in S3.
- **CDN** in front of S3 for photo delivery — application server no longer serves media.
- Feed is still a SQL query (chronological, fan-out on read).
- **Contrast with Twitter**: Twitter's early architecture (Ruby on Rails + MySQL) had a similar shape — single DB, chronological timeline, pull-based. Both hit the same scaling wall.
- **Problems found**: Feed query is slow — requires joining follows table with posts table, sorting. As user count grows, this query gets slower. Database is a single point of failure and a bottleneck. No caching — every feed load hits the DB.

### Attempt 2: Fan-out on write + caching
- **Pre-materialize feeds**: When a user posts, write a reference (postId, timestamp) to every follower's feed inbox (stored in Redis sorted sets or Memcache). On read: just fetch the pre-built feed list. O(1) per feed load.
- **Cache layer (Memcache/Redis)**: Cache user profiles, post metadata, follower lists. Most reads served from cache — DB only for cache misses.
- **Database sharding**: Shard by userId. Each user's data (profile, posts, feed) lives on one shard.
- Feed is now fast (pre-built in cache) but **chronological** — no ranking.
- **Contrast with Twitter**: Twitter implemented fan-out on write early (2012). Twitter's challenge was tweet delivery speed (200M+ users, 500M+ tweets/day). Same concept, similar timeline.
- **Problems found**: Celebrity problem — when a user with 100M followers posts, we need to write to 100M feed inboxes. This takes minutes and creates massive write amplification. Also, chronological feed causes users to miss important posts (users follow hundreds of accounts, can't keep up).

### Attempt 3: Hybrid fan-out + algorithmic ranking
- **Hybrid fan-out**: Fan-out on write for normal users (<500K followers). Fan-out on read for celebrities (>500K followers). Celebrity posts are fetched and merged at read time.
- **Feed ranking model**: Replace chronological ordering with ML-based ranking. Signals: relationship closeness, recency, post type, engagement velocity. Two-pass: candidate generation → scoring → top-N.
- **Social graph service**: Dedicated service for follow/unfollow operations, follower list lookups, fan-out routing (is this user a celebrity?).
- **Contrast with TikTok**: TikTok skipped the social-graph-based feed entirely — it went straight to recommendation-driven distribution. Instagram evolved from social-graph to social-graph+recommendations. Different starting points, converging on hybrid models.
- **Problems found**: Photos/videos are large (MB each). Serving billions of media requests from a single CDN region creates latency for distant users. Stories (ephemeral content) don't fit the permanent post model. No content discovery beyond the social graph (Explore page needed).

### Attempt 4: Stories + Explore + enhanced media delivery
- **Stories**: Ephemeral content with 24-hour TTL. Separate storage tier (Cassandra with TTL columns). Stories tray personalization (ranked by closeness).
- **Explore page**: Recommendation engine for content discovery beyond the social graph. Candidate generation → ML ranking → diversity injection. Powered by collaborative filtering + content-based signals.
- **Enhanced CDN**: Multi-tier caching (edge PoPs → regional caches → origin). Multiple image formats (JPEG, WebP, AVIF) served based on client support. Progressive loading with blurhash placeholders.
- **Media processing pipeline hardening**: Async processing via task queue. Support for video transcoding (multiple resolutions, HLS segments). Carousel posts (up to 10 items).
- **Contrast with Snapchat**: Snapchat pioneered Stories (2013). Instagram copied the concept (2016). Snapchat's storage is ephemeral-first; Instagram bolted ephemeral storage onto a permanent-content platform.
- **Problems found**: No short-form video discovery (TikTok is eating Instagram's lunch). DMs are basic — no real-time features. Notification storms from viral content. Single-region backend — regional failure takes down everything.

### Attempt 5: Reels + real-time + production hardening
- **Reels**: Short-form video with recommendation-driven distribution. Separate recommendation engine from the social-graph-based feed. Video processing pipeline for Reels (transcode, HLS, audio extraction, content moderation).
- **Real-time infrastructure**: MQTT for mobile real-time (DM delivery, typing indicators, notifications). Notification aggregation for thundering herd (celebrity posts). Live video (RTMP ingest, HLS distribution).
- **Multi-datacenter active-active**: Multiple Meta data centers serving production traffic simultaneously. Async data replication (TAO, Cassandra, MySQL). Regional failover with cache warming.
- **Production hardening**: Rate limiting, abuse detection, content moderation pipeline (automated ML + human review), approximate counters for high-traffic events, circuit breakers for dependent services.
- **Custom storage (Haystack/f4)**: Replace generic blob storage with Haystack (optimized for billions of small photos — one disk I/O per read) and f4 (warm storage with erasure coding for older content).
- **Contrast with TikTok**: TikTok built its recommendation engine from day one — it's the core product. Instagram is retrofitting a recommendation engine onto a social-graph platform. This architectural difference shows: TikTok's recommendations feel more natural because the entire system is designed around them.

## CRITICAL: Accuracy requirements

I DO NOT TRUST AI-GENERATED FACTS ABOUT SPECIFIC SYSTEMS. Every concrete claim about Instagram internals must be verifiable against official sources. Specifically:

1. **Use WebSearch and WebFetch tools** to look up Meta Engineering blog, Instagram Engineering blog, and official documentation BEFORE writing. Search for:
   - "Instagram engineering blog feed ranking"
   - "Instagram engineering architecture"
   - "Meta TAO social graph"
   - "Meta Haystack photo storage"
   - "Meta f4 warm storage"
   - "Instagram stories architecture"
   - "Instagram Reels recommendation"
   - "Meta CDN infrastructure"
   - "Meta Memcache scaling"
   - "Instagram fan-out on write"
   - "Instagram PostgreSQL early architecture"
   - "Meta MQTT mobile real-time"
   - "Instagram explore recommendation"
   - "Instagram monthly active users 2024 2025"
   - "Meta data center architecture"
   - "Instagram media processing pipeline"

   **BLANKET PERMISSION**: You have full permission to use WebSearch and WebFetch to read any online documentation, blog posts, or research papers. Do NOT ask the user for permission to read — just read. This applies to engineering.fb.com, engineering.instagram.com, research.facebook.com, and any other reference source. Read as many pages as needed to verify facts.

2. **For every concrete number** (MAU, DAU, posts per day, storage volume, cache stats), verify against official Meta/Instagram sources or reputable third-party reports. If you cannot verify a number, explicitly write "[UNVERIFIED — check Meta Engineering Blog]" next to it.

3. **For every claim about Instagram internals** (feed algorithm, storage architecture, processing pipeline), if it's not from an official source, mark it as "[INFERRED — not officially documented]".

4. **CRITICAL: Do NOT confuse Instagram with Twitter or TikTok.** These are different systems with different philosophies:
   - Instagram: media-first, social-graph-based feed + recommendation-based Reels, directed follow graph, part of Meta (shared infra with Facebook)
   - Twitter: text-first, chronological + algorithmic feed, directed follow graph, independent infra
   - TikTok: video-only, 100% recommendation-driven, minimal social graph, ByteDance infra
   - When discussing design decisions, ALWAYS explain WHY Instagram chose its approach and how Twitter/TikTok's different choices reflect different product models.

## Key Instagram topics to cover

### Requirements & Scale
- Photo/video sharing social network with sub-500ms feed load time
- 2B+ MAU, 500M+ daily Stories users, 100M+ uploads/day
- Media-first: every post has at least one photo or video
- Feed: social-graph-based, algorithmically ranked, hybrid fan-out
- Stories: ephemeral (24-hour TTL), 500M+ daily users
- Reels: recommendation-driven short-form video (competing with TikTok)
- Directed social graph with extreme degree variance (1 follower to 650M followers)

### Architecture deep dives (create separate docs as listed in "Files to create" above)

### Design evolution (iterative build-up — the most important part)
- Attempt 0: Single server + SQL database + photos on disk
- Attempt 1: Object storage (S3) + media processing + CDN
- Attempt 2: Fan-out on write + caching + database sharding
- Attempt 3: Hybrid fan-out + algorithmic feed ranking + social graph service
- Attempt 4: Stories + Explore page + enhanced CDN + video support
- Attempt 5: Reels + real-time (MQTT) + multi-DC + custom storage (Haystack/f4)

Each attempt MUST:
1. Start by identifying concrete problems with the previous attempt
2. Propose a solution
3. Explain WHY this approach (and why not the alternative — mention Twitter/TikTok/Snapchat's choice where relevant)
4. End with "what's still broken?" to motivate the next attempt

### Consistency & Data
- TAO for social graph (leader-follower, cached, eventually consistent reads)
- Cassandra for write-heavy data (feeds, activity, Stories)
- MySQL/MyRocks for structured data (accounts, post metadata)
- Redis for feed inboxes (sorted sets), counters, ephemeral state
- Memcache for general caching (trillions of ops/day across Meta)
- Haystack for hot photo storage (optimized for one-disk-I/O reads)
- f4 for warm photo storage (erasure coding, 65% space savings)
- Eventual consistency acceptable for feeds, likes, counters
- Strong consistency needed for follow/unfollow (graph mutations), account data

## What NOT to do
- Do NOT treat Instagram as "just a photo gallery" — it's a social network with feed generation, social graph, Stories, Reels, recommendations, DMs, and real-time infrastructure. Frame it accordingly.
- Do NOT confuse Instagram with Twitter or TikTok. Highlight differences at every layer, don't blur them.
- Do NOT jump to the final architecture. Build it step by step (Attempt 0 → 5).
- Do NOT make up internal implementation details — verify against Meta Engineering Blog or mark as inferred.
- Do NOT skip the iterative build-up.
- Do NOT write a Wikipedia article. This is an INTERVIEW SIMULATION with dialogue.
- Do NOT describe features without explaining WHY they exist (what problem they solve).
- Do NOT ask the user for permission to read online documentation — blanket permission is granted (see Accuracy requirements section).
- Do NOT ignore the Meta/Facebook shared infrastructure — Instagram runs on Meta's infra (TAO, Memcache, Haystack, MQTT). This is a key architectural reality.
