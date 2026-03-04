# Stories & Reels — Ephemeral & Short-Form Content

> Two distinct content formats with fundamentally different system design implications.
> Stories: ephemeral, social-graph-distributed, 500M+ daily users.
> Reels: permanent, recommendation-distributed, competing with TikTok.

---

## Table of Contents

1. [Stories — Ephemeral Content](#1-stories--ephemeral-content)
2. [Reels — Short-Form Video](#2-reels--short-form-video)
3. [Contrasts](#3-contrasts)

---

## 1. Stories — Ephemeral Content

Launched **August 2, 2016** [VERIFIED — Instagram official blog]. 500M+ daily active users as of January 2019 [VERIFIED — Instagram official announcement].

### Core Characteristic: 24-Hour TTL

Stories auto-delete 24 hours after creation. This is the defining design constraint — it affects storage, caching, distribution, and data modeling.

### Storage Design

```
┌─────────────────────────────────────────────────────┐
│ Ephemeral Storage Layer                              │
│                                                      │
│ Cassandra (metadata):                                │
│   Row key: userId                                    │
│   Columns: storyId, mediaUrl, createdAt, expiresAt,  │
│            stickers, closeFriendsOnly                │
│   TTL: 24 hours (Cassandra native column TTL)        │
│   → Cassandra automatically deletes expired columns  │
│                                                      │
│ Blob Storage (media):                                │
│   S3 lifecycle policy: delete objects after 24 hours  │
│   OR: Store in a separate "ephemeral" bucket with    │
│       automated cleanup                               │
│                                                      │
│ CDN:                                                 │
│   Story media URLs have embedded expiration tokens    │
│   CDN honors expiration — returns 404 after TTL      │
└─────────────────────────────────────────────────────┘
```

**Why Cassandra for Stories metadata?**
- Cassandra natively supports column-level TTL — when you write a column with `TTL=86400` (24 hours), Cassandra automatically deletes it after expiration
- No need for external cleanup jobs — the database handles garbage collection
- High write throughput for ~500M+ Stories per day
- Time-series-friendly data model: stories are naturally ordered by creation time

### Stories Tray (The Row of Circles)

The Stories tray at the top of the home screen is personalized and ranked.

```
┌──────────────────────────────────────────────────────────────┐
│ Stories Tray                                                  │
│                                                              │
│  ┌───┐  ┌───┐  ┌───┐  ┌───┐  ┌───┐  ┌───┐  ┌───┐          │
│  │You│  │ A │  │ B │  │ C │  │ D │  │ E │  │ F │  ...       │
│  └───┘  └───┘  └───┘  └───┘  └───┘  └───┘  └───┘          │
│  (own)  (unseen, ranked by closeness + recency)              │
└──────────────────────────────────────────────────────────────┘
```

**Tray assembly:**
1. Fetch list of followed accounts that have active (non-expired) Stories
2. Partition into: unseen Stories (colored ring) and seen Stories (gray ring)
3. Within each partition, rank by: `relationship_closeness × recency_weight`
4. Relationship closeness: how often you view their Stories, DM them, like their posts, visit their profile
5. Return top-N users with their first Story thumbnail (for prefetching)

**Tray ranking is critical** because most users only view Stories from the first 5-7 accounts in the tray. Being ranked higher → more views → more engagement.

### Seen State Tracking

Must track which Stories each user has seen to:
- Show colored vs gray ring in the tray
- Skip already-seen Stories during sequential playback
- Report view counts to the Story author

**Scale problem:** If 500M users view Stories daily, and each views ~50 accounts' Stories, that's **~25 billion seen-state entries per day**. Each entry is ~20 bytes (userId + storyId + timestamp). That's ~500GB of new seen-state data per day — and it's ephemeral (only needed for 24 hours).

**Storage approach:**
- Redis bitmaps or bloom filters per user — compact representation of "which Stories has this user seen"
- Alternative: Cassandra with TTL (same 24-hour TTL as the Stories themselves)
- Seen-state is eventually consistent — a brief delay before the ring turns gray is acceptable

### Highlights (Ephemeral → Permanent)

Users can save Stories to "Highlights" on their profile. This converts ephemeral content to permanent content.

**Migration flow:**
```
Story (ephemeral, TTL=24h)
    │
    User adds to Highlight
    │
    ├── Copy media from ephemeral storage to permanent storage (Haystack)
    ├── Create permanent metadata record (no TTL)
    ├── Link to Highlight collection on user's profile
    └── Original Story still expires normally after 24h
```

This is an interesting edge case: the same media object transitions from one storage tier (ephemeral, TTL-enabled) to another (permanent, replicated). The migration must happen before the TTL expires.

### Interactive Elements

Stories support interactive stickers: polls, questions, quizzes, countdowns, emoji sliders, music.

**Poll/Quiz architecture:**
```
Poll: "Beach or mountains?"
    │
    Viewer votes → POST /stories/{storyId}/poll-vote
    │
    ├── Increment vote counter (Redis INCR — eventually consistent)
    ├── Store individual vote (for preventing double-votes)
    └── Return updated percentages to all viewers
        (next viewer who loads the Story sees updated results)
```

- Real-time aggregation: vote counts update in near-real-time for subsequent viewers
- Individual vote tracking: prevent double-votes using a set of `(userId, storyId, pollId)`
- Results are visible to the Story author (who voted what)

---

## 2. Reels — Short-Form Video

Launched **August 5, 2020** [VERIFIED — Instagram official blog]. Directly competing with TikTok.

### Fundamental Difference from Feed Posts

| Aspect | Feed Posts | Reels |
|---|---|---|
| **Distribution** | Social-graph-based (your followers see it) | Recommendation-based (anyone can see it) |
| **Discovery** | Followers' feeds only | Reels tab, Explore, home feed suggestions |
| **Fan-out** | Write to followers' feed inboxes | Index in recommendation engine |
| **Content format** | Photo, video, carousel | Video only (15-90 seconds, 9:16 vertical) |
| **Lifecycle** | Permanent | Permanent |
| **Audio** | Optional | Central (trending sounds, music) |

### Reels Distribution Architecture

```
Creator posts a Reel
        │
        ▼
┌───────────────────────────┐
│ Media Processing Pipeline │
│ • Transcode (4 resolutions)│
│ • HLS segmentation         │
│ • Audio extraction          │
│ • Content moderation        │
│ • Thumbnail generation      │
└────────────┬──────────────┘
             │
             ├── Store in blob storage (permanent)
             │
             ├── Index in Recommendation Engine
             │   • Extract content features (video understanding ML)
             │   • Extract audio features (fingerprint, genre)
             │   • Generate content embeddings
             │   • Register in candidate pool
             │
             └── If shareToFeed=true, also fan-out to followers' feed inboxes
                 (same as a regular post)
```

**Key insight:** A Reel has TWO distribution paths:
1. **Social-graph path** (if shared to feed): fan-out to followers, appears in home feed
2. **Recommendation path** (always): indexed in the recommendation engine, appears in Reels tab + Explore to non-followers

### Reels Recommendation Engine

**VERIFIED — From Adam Mosseri's 2023 transparency posts:**

The Reels recommendation engine is Instagram's answer to TikTok's For You page.

**Ranking signals (officially disclosed):**
1. **Your activity** — what Reels you've liked, saved, shared, commented on recently
2. **Interaction history** — even with unknown accounts, prior interactions are a signal
3. **Reel information** — audio track, video understanding (pixel/frame analysis), popularity
4. **Creator information** — follower count, engagement rate, content topic

**Key predictions:**
- **P(watch-through)** — will the viewer watch the full Reel? (THE strongest signal)
- **P(like)** — will they like it?
- **P(entertaining)** — will they find it funny/entertaining? (from survey data)
- **P(go-to-audio)** — will they visit the audio page? (proxy for creative inspiration)

**Watch-through rate is the most important signal** because:
- A user watching a 30-second Reel to completion is a much stronger interest signal than a passive "like"
- Short videos → binary signal (watched vs skipped) is very informative
- Unlike likes (which require active interaction), watch-through is implicit — requires no user action

**Original content preference (VERIFIED):**
Instagram actively demotes:
- Content recycled from other apps (TikTok watermarks)
- Low-resolution or blurry videos
- Mostly text-based content without visual substance
- Unmodified reposts

### Audio Layer

Reels have a first-class audio layer that creates unique engineering challenges:

```
┌───────────────────────────────────────────┐
│ Audio Catalog                              │
│                                            │
│ • Licensed music catalog (deals with       │
│   labels/publishers)                       │
│ • Original audio from creators             │
│ • Trending sounds (tracked by usage count) │
│                                            │
│ Audio Fingerprinting:                      │
│ • When a Reel is uploaded, extract audio    │
│ • Fingerprint against music catalog        │
│ • If match: link to official track          │
│ • If no match: register as "original audio" │
│ • Enable other creators to "Use this audio" │
│                                            │
│ Trending Sounds:                            │
│ • Track usage count of each audio track     │
│ • Rising usage = trending sound             │
│ • Trending sounds get boosted in            │
│   recommendation ranking                    │
└───────────────────────────────────────────┘
```

### Prefetching Strategy

The Reels tab is an infinite scroll of full-screen videos. To eliminate buffering:

```
User watches Reel #1
        │
        ├── Background: Download HLS segments for Reel #2 (next)
        ├── Background: Download thumbnail + first segment for Reel #3
        └── Background: Pre-fetch metadata for Reels #4-#5

User swipes to Reel #2
        │
        ├── Reel #2 plays instantly (already buffered)
        ├── Background: Download segments for Reel #3
        └── ...
```

**Trade-off:** Prefetching wastes bandwidth when users exit before reaching prefetched Reels. Instagram likely buffers only 2-3 ahead (limited by mobile memory) and accepts ~20-30% wasted bandwidth.

---

## 3. Contrasts

### Stories: Instagram vs Snapchat

| Dimension | Instagram Stories | Snapchat Stories |
|---|---|---|
| **Launch** | August 2016 (copied from Snapchat) | October 2013 (pioneer) |
| **TTL** | 24 hours | 24 hours |
| **Platform design** | Bolted onto permanent-content platform | Built ephemeral-first |
| **Storage complexity** | High (dual storage tiers: permanent + ephemeral) | Lower (everything ephemeral by default) |
| **Highlights** | Yes (convert ephemeral → permanent) | Memories (similar concept) |
| **Distribution** | Social-graph (followers only) | Social-graph (friends only) |
| **Scale** | 500M+ daily users (2019) | ~400M+ daily users |

**Key architectural difference:** Snapchat was built ephemeral-first — its entire storage layer is optimized for content that expires. Instagram bolted ephemeral content onto a platform designed for permanent photos. This means Instagram must manage two distinct storage lifecycles, migration between them (Stories → Highlights), and different caching strategies for each.

### Reels: Instagram vs TikTok

| Dimension | Instagram Reels | TikTok For You |
|---|---|---|
| **Launch** | August 2020 | 2016 (as Musical.ly, rebranded 2018) |
| **Duration** | 15-90 seconds | 15s-10min |
| **Distribution** | Recommendation-based (Reels tab) + social-graph (if shared to feed) | 100% recommendation-based |
| **Recommendation quality** | Improving (catch-up to TikTok) | Industry-leading |
| **Product context** | One tab among many (Feed, Stories, Explore, Reels) | The ENTIRE product |
| **Social graph** | Shared with main Instagram (affects recommendations) | Minimal importance |
| **Content ecosystem** | Competes with photos, Stories, carousels for creator attention | Singular focus on short video |
| **Exploration strategy** | More conservative (leverages existing social graph) | More aggressive (shows unknown creators) |

**Key architectural difference:** TikTok was built recommendation-first. Its entire infrastructure is optimized for content discovery via ML. Instagram is retrofitting a recommendation engine onto a social-graph platform. This architectural debt shows: TikTok's recommendations feel more natural because every system component (from content indexing to serving) is designed for recommendation-driven distribution.

### Reels: Instagram vs YouTube Shorts

| Dimension | Instagram Reels | YouTube Shorts |
|---|---|---|
| **Max duration** | 90 seconds | 60 seconds |
| **Parent platform** | Photo/video social network | Long-form video platform |
| **Monetization** | Ads in Reels feed | Revenue sharing with creators |
| **Audio** | Strong music integration | Strong (YouTube Music catalog) |
| **Recommendation engine** | Instagram's ML stack | YouTube's recommendation (most mature) |
| **CDN** | Meta's CDN | Google's CDN |
