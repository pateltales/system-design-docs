# News Feed Generation — Feed Ranking & Delivery

> This is THE core system design problem for Instagram.
> How does the feed get assembled and delivered to 2B+ users?

---

## Table of Contents

1. [The Fundamental Problem](#1-the-fundamental-problem)
2. [Fan-out on Write (Push Model)](#2-fan-out-on-write-push-model)
3. [Fan-out on Read (Pull Model)](#3-fan-out-on-read-pull-model)
4. [Hybrid Approach (What Instagram Does)](#4-hybrid-approach-what-instagram-does)
5. [Feed Ranking (ML Model)](#5-feed-ranking-ml-model)
6. [Feed Pagination](#6-feed-pagination)
7. [Feed Invalidation](#7-feed-invalidation)
8. [Suggested Posts & Ads Injection](#8-suggested-posts--ads-injection)
9. [The "Following" Chronological Feed](#9-the-following-chronological-feed)
10. [Contrasts](#10-contrasts)

---

## 1. The Fundamental Problem

User opens Instagram. We need to show them a ranked feed of posts from accounts they follow — plus suggested content and ads — in under 500ms.

**The math:**
- A typical user follows ~200-500 accounts
- Each followed account posts at different rates (some daily, some weekly)
- We need to collect recent posts from all followed accounts, rank them by predicted interest, and return the top ~20
- This must happen for billions of feed loads per day

**Why this is hard:**
- Simple SQL approach: `SELECT * FROM posts WHERE author_id IN (SELECT followee_id FROM follows WHERE follower_id = ?) ORDER BY created_at DESC LIMIT 20` — this is O(following_count × posts_per_user) and requires a massive JOIN that doesn't scale
- The social graph has extreme degree variance: a regular user has 500 followers; Cristiano Ronaldo has 650M+

---

## 2. Fan-out on Write (Push Model)

**How it works:**

When user A creates a post:
1. Look up A's follower list
2. For each follower, write a reference `(postId, timestamp)` to that follower's **feed inbox** (pre-materialized feed list in Redis/Memcache)
3. When any follower opens their feed, just fetch their pre-built inbox — O(1) lookup

```
User A posts
    │
    ▼
Follower list: [B, C, D, E, ...]  (1,000 followers)
    │
    ├──> Write (postId, ts) to feed:B
    ├──> Write (postId, ts) to feed:C
    ├──> Write (postId, ts) to feed:D
    ├──> Write (postId, ts) to feed:E
    └──> ... (1,000 writes)

Later, User B opens feed:
    │
    ▼
Fetch feed:B → [(postId-1, ts), (postId-2, ts), ...]
    │
    ▼
Hydrate post metadata (author, media, engagement counts)
    │
    ▼
Return feed to client
```

**Storage: Redis sorted sets** (VERIFIED — Instagram's 2013 engineering blog describes using Redis for this exact purpose)

```
Key:   feed:{userId}
Value: Sorted set of (score=timestamp, member=postId)

ZADD feed:user-B 1705312800 post-uuid-xyz    // Add post to B's feed
ZREVRANGE feed:user-B 0 19                    // Get top 20 most recent
```

**Pros:**
- **Blazing fast reads** — just a Redis `ZREVRANGE` call (~1ms)
- Read path is simple and predictable
- Works perfectly for 99%+ of users (those with <500K followers)

**Cons:**
- **Write amplification** — one post → N writes (where N = follower count)
- **Celebrity problem**: Cristiano Ronaldo has 650M followers. One post → 650M Redis writes. At ~10µs per write, that's ~1.8 hours of sequential writes. Even parallelized across thousands of Redis shards, this takes minutes.
- **Wasted work**: Most followers won't open Instagram for hours/days after the post. We eagerly wrote to their inbox for nothing.
- **Memory cost**: Each user's feed inbox must be stored in memory. 2B users × 500 entries × ~50 bytes/entry = ~50TB of Redis memory just for feed inboxes.

---

## 3. Fan-out on Read (Pull Model)

**How it works:**

When user B opens their feed:
1. Look up B's following list: [A, C, D, E, ...] (500 accounts)
2. For each followed account, fetch their recent posts
3. Merge all posts, sort/rank, return top 20

```
User B opens feed
    │
    ▼
Following list: [A, C, D, E, ...]  (500 accounts)
    │
    ├──> Fetch recent posts from A → [post-1, post-2]
    ├──> Fetch recent posts from C → [post-3]
    ├──> Fetch recent posts from D → [post-4, post-5, post-6]
    ├──> Fetch recent posts from E → [post-7]
    └──> ... (500 lookups)
    │
    ▼
Merge all posts → [post-1, ..., post-N]
    │
    ▼
Rank by predicted interest → top 20
    │
    ▼
Return feed to client
```

**Pros:**
- **No write amplification** — posting is cheap (just store the post)
- No wasted work — only compute the feed when the user actually opens the app
- Celebrity posting is instant — no fan-out at all

**Cons:**
- **Slow reads** — 500 lookups + merge + rank at request time
- Even with caching, 500 parallel lookups + merge is too slow for the common case (~200-500ms just for the lookups, before ranking)
- Read latency scales with following count — users who follow 7,500 accounts (the max) would have terrible feed load times
- Every feed load is compute-intensive, and feeds are loaded billions of times per day

---

## 4. Hybrid Approach (What Instagram Does)

Instagram uses a **hybrid** approach that combines the best of both models:

- **Fan-out on write** for normal users (followers < threshold)
- **Fan-out on read** for celebrity/high-follower accounts (followers > threshold)

```
                    ┌──────────────────────────┐
                    │  User creates a post      │
                    └──────────┬───────────────┘
                               │
                    ┌──────────▼───────────────┐
                    │  Check follower count     │
                    └──────────┬───────────────┘
                               │
                ┌──────────────┴──────────────┐
                │                             │
     ┌──────────▼──────────┐     ┌────────────▼──────────┐
     │  < threshold         │     │  >= threshold          │
     │  (e.g., <500K)       │     │  (e.g., >=500K)        │
     │                      │     │                        │
     │  FAN-OUT ON WRITE    │     │  STORE POST ONLY       │
     │  Write postId to     │     │  No fan-out.           │
     │  every follower's    │     │  Post will be fetched  │
     │  feed inbox in Redis │     │  at READ time when     │
     │                      │     │  followers open feed.  │
     └──────────────────────┘     └────────────────────────┘
```

**On feed read:**

```
                    ┌──────────────────────────┐
                    │  User opens feed          │
                    └──────────┬───────────────┘
                               │
                    ┌──────────▼───────────────┐
                    │  Step 1: Fetch feed       │
                    │  inbox from Redis         │
                    │  (pre-materialized posts) │
                    └──────────┬───────────────┘
                               │
                    ┌──────────▼───────────────┐
                    │  Step 2: Identify which   │
                    │  celebrity accounts the   │
                    │  user follows             │
                    └──────────┬───────────────┘
                               │
                    ┌──────────▼───────────────┐
                    │  Step 3: Fetch latest     │
                    │  posts from each          │
                    │  celebrity (fan-out on    │
                    │  read). Typically 5-20    │
                    │  celebrity accounts.       │
                    └──────────┬───────────────┘
                               │
                    ┌──────────▼───────────────┐
                    │  Step 4: Merge inbox +    │
                    │  celebrity posts into     │
                    │  candidate set (~500)     │
                    └──────────┬───────────────┘
                               │
                    ┌──────────▼───────────────┐
                    │  Step 5: ML ranking       │
                    │  Score ~500 candidates,   │
                    │  return top ~20           │
                    └──────────┬───────────────┘
                               │
                    ┌──────────▼───────────────┐
                    │  Step 6: Inject suggested │
                    │  posts + ads at specific  │
                    │  positions                │
                    └──────────┬───────────────┘
                               │
                    ┌──────────▼───────────────┐
                    │  Return ranked feed       │
                    └──────────────────────────┘
```

**Celebrity threshold:**
- Instagram has never publicly disclosed the exact threshold. [UNVERIFIED]
- Industry estimates range from ~10K to ~1M followers
- The threshold is likely dynamic — tuned based on system load, not a fixed number
- Martin Kleppmann's "Designing Data-Intensive Applications" cites Twitter's approach with a similar hybrid model

**Why this works:**
- 99%+ of users have <500K followers → their posts are fanned out on write → fast reads for the common case
- The handful of celebrity accounts followed by a given user (typically <20) are fetched at read time → affordable read-time cost
- The total read-time work is: 1 Redis fetch (inbox) + 5-20 lookups (celebrity posts) + merge + rank → <100ms total

---

## 5. Feed Ranking (ML Model)

Instagram's feed has NOT been chronological since **June 2016**. [VERIFIED — Instagram official blog, March 2016 announcement, fully rolled out June 2016]

**Why the switch?** Instagram stated that users were missing 70% of posts in their chronological feed. With hundreds of followed accounts posting daily, the feed scrolled too fast for users to keep up. Algorithmic ranking ensures the most relevant posts surface first.

### Ranking Pipeline

**VERIFIED — From Adam Mosseri's 2021/2023 transparency posts and Instagram engineering talks:**

```
~500 candidate posts (from inbox + celebrity pull)
        │
        ▼
┌───────────────────────────────────────┐
│ SIGNAL EXTRACTION                      │
│ Extract thousands of features per post:│
│                                        │
│ Post signals:                          │
│ • Engagement velocity (likes/min)      │
│ • Post type (photo/video/carousel)     │
│ • Post age (recency)                   │
│ • Location tag                         │
│ • Caption length, hashtags             │
│                                        │
│ Author signals:                        │
│ • Relationship closeness score         │
│ • How often viewer interacts with      │
│   this author (likes, comments, DMs)   │
│ • Is this a Close Friend?              │
│                                        │
│ Viewer signals:                        │
│ • Content type preferences             │
│ • Session context (time of day, device)│
│ • Recent engagement patterns           │
│                                        │
│ Interaction history:                   │
│ • Did viewer like author's last N posts?│
│ • Did viewer comment? DM? Visit profile?│
└───────────────────┬───────────────────┘
                    │
                    ▼
┌───────────────────────────────────────┐
│ PREDICTION (Multi-Objective ML Model) │
│                                        │
│ The model predicts probabilities:      │
│                                        │
│ P(like)     — will the viewer like it? │
│ P(comment)  — will they comment?       │
│ P(save)     — will they save/bookmark? │
│ P(share)    — will they share via DM?  │
│ P(dwell)    — will they spend time     │
│               looking at it?            │
│ P(profile)  — will they visit the      │
│               author's profile?         │
│                                        │
│ As of 2023, saves, shares, and dwell   │
│ time are increasingly weighted over    │
│ simple likes.                           │
└───────────────────┬───────────────────┘
                    │
                    ▼
┌───────────────────────────────────────┐
│ SCORING                                │
│                                        │
│ final_score = w1 * P(like)             │
│             + w2 * P(comment)          │
│             + w3 * P(save)             │
│             + w4 * P(share)            │
│             + w5 * P(dwell)            │
│             + w6 * P(profile_visit)    │
│                                        │
│ Weights (w1-w6) are learned via        │
│ multi-objective optimization.           │
│ Recent updates have increased w3       │
│ (save) and w5 (dwell) relative to      │
│ w1 (like) to favor meaningful          │
│ engagement over passive double-taps.   │
└───────────────────┬───────────────────┘
                    │
                    ▼
┌───────────────────────────────────────┐
│ BUSINESS RULES                         │
│                                        │
│ • Diversity: No >2 consecutive posts   │
│   from the same author                 │
│ • Content type mixing: Alternate       │
│   photos, videos, carousels            │
│ • Demotion: Demote posts from accounts │
│   that repeatedly violate guidelines   │
│ • Recency boost: Post age > 48 hours   │
│   gets a penalty                       │
│ • "Already seen" filter: Don't show    │
│   posts the user has already seen      │
└───────────────────┬───────────────────┘
                    │
                    ▼
        Top ~20 posts returned as page 1
```

### Model Architecture

**VERIFIED (partially) — From Instagram engineering talks and Meta AI publications:**

- Instagram's feed ranking historically used **logistic regression** models, then transitioned to **deep neural networks**
- The current model is a **multi-task, multi-label (MTML) neural network** that simultaneously predicts all engagement types
- Some ranking surfaces use **two-tower architectures** (one tower encodes the user, one encodes the item) — confirmed for Explore, likely used for Feed as well
- Models are retrained on fresh engagement data regularly (daily or sub-daily)

---

## 6. Feed Pagination

Instagram uses **cursor-based pagination**, not offset-based.

**Why cursor-based?**

```
Offset-based (problematic):
    Page 1: GET /feed?offset=0&limit=20  → posts [1..20]
    Page 2: GET /feed?offset=20&limit=20 → posts [21..40]

    But if 5 new posts are inserted between page loads:
    Page 2: GET /feed?offset=20&limit=20 → posts [16..35]
    → User sees duplicates of posts [16..20]!

Cursor-based (correct):
    Page 1: GET /feed?limit=20           → posts [1..20], cursor=ABC
    Page 2: GET /feed?cursor=ABC&limit=20 → posts [21..40]

    The cursor encodes the last seen post's score + timestamp.
    New posts don't shift the cursor position.
```

**Cursor structure** (opaque to client, base64-encoded):
```json
{
  "lastPostId": "post-uuid-xyz",
  "lastScore": 0.94,
  "lastTimestamp": 1705312800,
  "feedVersion": "v3"
}
```

---

## 7. Feed Invalidation

Feed invalidation is the **reverse** of fan-out on write — and it's expensive.

**When does invalidation happen?**
1. **Unfollow**: User A unfollows User B → B's posts must be removed from A's feed inbox
2. **Post deletion**: User B deletes a post → the post must be removed from every follower's feed inbox it was fanned out to
3. **Account deletion/suspension**: All posts from the deleted account must be removed from all feed inboxes
4. **Content moderation**: A post is removed for policy violation → same as deletion

**Unfollow invalidation:**
```
User A unfollows User B
    │
    ▼
Fetch all of B's recent posts → [post-1, post-2, ..., post-N]
    │
    ▼
For each post, remove from A's feed inbox:
    ZREM feed:user-A post-1
    ZREM feed:user-A post-2
    ...
```

This is a read-heavy operation (fetch B's posts, then write to A's inbox). It's done asynchronously — the unfollow is confirmed immediately, and the feed cleanup happens in the background. If the user refreshes their feed before cleanup completes, they might briefly see posts from the unfollowed account.

**Post deletion invalidation:**
```
User B deletes post-1
    │
    ▼
Fetch B's follower list → [A, C, D, ...]  (potentially millions)
    │
    ▼
For each follower, remove post-1 from their feed inbox:
    ZREM feed:user-A post-1
    ZREM feed:user-C post-1
    ...
```

This is the reverse fan-out problem. For a user with 1M followers, deleting a post requires 1M Redis writes. For celebrities (650M followers), this is handled lazily — the deleted post is filtered out at read time rather than eagerly removed from all inboxes.

---

## 8. Suggested Posts & Ads Injection

Instagram's feed is no longer purely posts from followed accounts. The feed includes:

**Suggested Posts:**
- Posts from accounts the user does NOT follow, recommended based on interest
- Injected at specific positions in the feed (e.g., every 5th-10th post)
- Powered by the same recommendation engine as Explore
- Labeled with "Suggested for you"

**Ads:**
- Sponsored posts injected at regular intervals
- Ad placement is determined by an ad auction system (separate from organic ranking)
- Ads must blend naturally with organic content — same visual format (photo/video/carousel)
- Ad frequency is capped to avoid degrading user experience

**Architecture:**
```
┌──────────────────────┐   ┌──────────────────────┐
│ Organic Feed         │   │ Ad Server             │
│ (fan-out + ranking)  │   │ (auction + targeting) │
└──────────┬───────────┘   └──────────┬────────────┘
           │                          │
           └──────────┬───────────────┘
                      │
           ┌──────────▼───────────────┐
           │ Feed Mixer                │
           │ Interleave organic posts, │
           │ suggested posts, and ads  │
           │ at specified positions    │
           └──────────┬───────────────┘
                      │
                      ▼
               Final feed response
```

---

## 9. The "Following" Chronological Feed

In **2022**, Instagram re-introduced a chronological "Following" feed option after years of user backlash against the purely algorithmic feed. [VERIFIED — publicly announced by Instagram]

**How it differs:**
- No ML ranking — posts are ordered purely by timestamp (reverse-chronological)
- No suggested posts or ads mixed in (only posts from followed accounts)
- No celebrity fan-out optimization — all posts are treated equally
- Still uses cursor-based pagination

**Architecture:** Simpler than the algorithmic feed. Just fetch the feed inbox (fan-out on write data) + celebrity posts (fan-out on read), merge by timestamp, return.

**Why Instagram added it back:** User trust. The algorithmic feed felt opaque — users couldn't understand why certain posts appeared or didn't appear. The chronological option gives users a sense of control. Instagram's data showed that most users prefer the algorithmic feed for discovery but use the chronological feed to ensure they don't miss posts from close friends.

---

## 10. Contrasts

### Instagram vs Twitter — Feed Generation

| Dimension | Instagram | Twitter (X) |
|---|---|---|
| **Default feed** | Algorithmic (since 2016) | "For You" algorithmic + "Following" chronological |
| **Fan-out model** | Hybrid (write for normal, read for celebrities) | Hybrid (same approach) |
| **Feed item size** | Heavy (media URLs, thumbnails, blurhash, engagement) ~2-5KB | Light (text + optional media) ~0.5-1KB |
| **Ranking signals** | Visual engagement: dwell time, save, share | Text engagement: retweet, reply, like |
| **Resharing** | No native reshare in feed | Retweet (amplification built-in) |
| **Chronological option** | "Following" tab (added 2022) | "Following" tab (always available) |
| **Celebrity threshold** | Unknown (estimated ~500K) | Unknown (estimated ~10K-100K) |

**Key architectural difference:** Twitter's retweet mechanism creates cascading fan-out — a retweet of a popular tweet can reach millions of additional users beyond the original author's followers. Instagram has no equivalent mechanism (no native reshare in feed), which simplifies the fan-out model but limits viral distribution.

### Instagram vs TikTok — Feed Generation

| Dimension | Instagram Home Feed | TikTok For You Page |
|---|---|---|
| **Distribution model** | Social-graph-based (posts from followed accounts) | Recommendation-based (posts from anyone) |
| **Fan-out** | Required (write to followers' inboxes) | NOT required (no follower-based distribution) |
| **Social graph dependency** | Critical (feed is built from follow graph) | Minimal (feed ignores follow graph) |
| **Infrastructure** | Graph store + fan-out + feed inbox + ranking | Recommendation engine + content index |
| **Cold start** | Easy (follow accounts → get their posts) | Harder (must learn preferences from scratch) |
| **Creator equity** | Biased toward large accounts (more followers = more reach) | More egalitarian (any video can go viral) |

**Key architectural difference:** TikTok doesn't need fan-out infrastructure at all. Its entire feed is generated by a recommendation engine that scores all recent public content per-user at request time. This is fundamentally different from Instagram's social-graph-based approach and requires a completely different infrastructure stack.

### Instagram vs Facebook — Feed Generation

| Dimension | Instagram | Facebook |
|---|---|---|
| **Feed model** | Social-graph based, algorithmic | Social-graph based, algorithmic (EdgeRank → News Feed) |
| **Social graph** | Directed (follow) | Undirected (friendship) |
| **Fan-out symmetry** | Asymmetric (A follows B, B doesn't follow A) | Symmetric (A friends B, B always sees A's posts) |
| **Ranking pioneer** | Adopted algorithmic ranking in 2016 | Pioneered EdgeRank in 2009 |
| **Content signals** | Primarily visual (dwell time on images/videos) | Richer (reactions, shares, comments, groups, pages) |
| **Shared infrastructure** | Uses TAO, Memcache, Meta's ML platform | Same infrastructure (TAO, Memcache, etc.) |
