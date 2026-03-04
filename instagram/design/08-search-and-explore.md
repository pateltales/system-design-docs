# Search & Explore — Discovery Beyond the Social Graph

> How users find new content and accounts they don't already follow.
> Explore drives content discovery; Search enables intent-based lookup.

---

## Table of Contents

1. [Search Architecture](#1-search-architecture)
2. [Explore Page Recommendation Engine](#2-explore-page-recommendation-engine)
3. [Reels Recommendation Feed](#3-reels-recommendation-feed)
4. [Contrasts](#4-contrasts)

---

## 1. Search Architecture

Instagram Search supports three search types: **users** (by username/name), **hashtags**, and **places/locations**. In late 2020, Instagram added **keyword search** for English-speaking markets. [VERIFIED — Instagram product announcements]

### Search Surfaces

```
┌────────────────────────────────────────────────────────┐
│ Search Bar: user types "tok..."                        │
│                                                        │
│  ┌─ Typeahead (< 100ms) ───────────────────────────┐  │
│  │ 👤 tokyo_explorer          ✓ Verified           │  │
│  │ # tokyo                    45M posts             │  │
│  │ # tokyofood                2.3M posts            │  │
│  │ 📍 Tokyo, Japan                                  │  │
│  └──────────────────────────────────────────────────┘  │
│                                                        │
│  User taps "tokyo" hashtag:                            │
│  ┌─ Results ───────────────────────────────────────┐  │
│  │  [Top]  [Recent]                                │  │
│  │  Grid of posts tagged #tokyo                    │  │
│  │  Top: engagement-ranked (precomputed)           │  │
│  │  Recent: reverse-chronological                  │  │
│  └──────────────────────────────────────────────────┘  │
└────────────────────────────────────────────────────────┘
```

### Typeahead / Autocomplete

Must return results in **<100ms** because it runs on every keystroke.

**Architecture:**
```
Client types "tok"
    │
    ▼
┌──────────────────────────────┐
│ Prefix Index (in-memory)     │
│                              │
│ Options:                     │
│ • Trie data structure        │
│ • Inverted index with prefix │
│   matching                   │
│ • Meta uses Unicorn (custom  │
│   social-graph-aware search) │
│                              │
│ Results are PERSONALIZED:    │
│ • Accounts you've interacted │
│   with rank higher           │
│ • Accounts followed by your  │
│   friends rank higher        │
│ • Recently searched terms    │
│   rank higher                │
└──────────────┬───────────────┘
               │
               ▼
Merge: users + hashtags + places
Sort by: relevance × personalization
Return top 5-8 suggestions
```

**Why <100ms matters:** Typeahead runs on every keystroke. At 200ms, suggestions feel laggy. At 300ms+, users stop typing and wait — bad experience. This requires:
- In-memory index at the edge (not a database query per keystroke)
- Precomputed popularity scores for common prefixes
- Local caching of recent search history on the client

### Unicorn (Meta's Search Engine)

**VERIFIED — from VLDB 2013 paper "Unicorn: A System for Searching the Social Graph"**

Meta built Unicorn, a custom search engine that is **social-graph-aware**. Unlike traditional search engines that rank by relevance + popularity, Unicorn incorporates the social graph into ranking.

**Example:** When you search "John", Unicorn returns Johns you know before Johns you don't know:
1. People named John you follow
2. People named John followed by your friends
3. Popular people named John
4. Other Johns

### Keyword Search (2020-2021)

Instagram introduced keyword search (natural language queries beyond just usernames/hashtags):
- Uses NLP models to understand search intent
- Matches against post captions, alt text, hashtags, and location names
- Posts are indexed by text content using custom inverted indices
- Likely uses **embedding-based retrieval** (mapping queries and content into the same vector space) — [PARTIALLY VERIFIED, inferred from Meta's published Facebook Search work]

### Search Ranking Signals

**VERIFIED — from Adam Mosseri's transparency posts:**

| Signal Category | Examples |
|---|---|
| **Text match** | How well query matches usernames, bios, captions, hashtags, place names |
| **User activity** | Accounts you follow, posts you've interacted with, past searches |
| **Popularity** | Clicks, likes, shares, follows for a result |
| **Social proximity** | Accounts followed by people you follow (mutual connections) |

---

## 2. Explore Page Recommendation Engine

The Explore page is a grid of recommended posts/Reels from accounts the user does NOT follow. Entirely recommendation-driven.

### Architecture (Three-Stage Pipeline)

**VERIFIED — from Meta Engineering blog "Powered by AI: Instagram's Explore recommender system" (2019) and scaling updates (2023)**

```
All recent public posts/Reels (millions per day)
        │
        ▼
┌───────────────────────────────────────────────┐
│ STAGE 1: CANDIDATE GENERATION (Sourcing)       │
│                                                │
│ Narrow millions of posts to ~10K candidates    │
│ relevant to this specific user.                │
│                                                │
│ Methods:                                       │
│ • Account-level collaborative filtering:        │
│   "Users who liked accounts A and B also liked  │
│   account C" → surface C's posts                │
│                                                │
│ • IG2Vec (Instagram-to-Vec):                    │
│   Embed accounts in vector space using          │
│   word2vec-style training on user interaction    │
│   sequences. Similar accounts are nearby.       │
│                                                │
│ • IGQL (Instagram Graph Query Language):        │
│   Custom query language for traversing the      │
│   interest graph to find candidate content.     │
│                                                │
│ • Two-tower retrieval model (2023 update):      │
│   One tower encodes the user, one encodes the   │
│   media/post. Use FAISS (Facebook AI Similarity │
│   Search) for approximate nearest neighbor.     │
│                                                │
│ Output: ~10K candidate posts                    │
└────────────────────┬──────────────────────────┘
                     │
                     ▼
┌───────────────────────────────────────────────┐
│ STAGE 2: FIRST-PASS RANKING (Distillation)     │
│                                                │
│ A lightweight neural network (distillation      │
│ model — trained to approximate the full         │
│ ranking model) quickly scores ~10K candidates.  │
│                                                │
│ Narrows to ~150 candidates.                     │
│                                                │
│ Why a distillation model? The full ranking       │
│ model is too expensive to run on 10K items.     │
│ The distilled model has fewer parameters and    │
│ features but captures the essential ranking     │
│ behavior. It trades accuracy for speed.         │
└────────────────────┬──────────────────────────┘
                     │
                     ▼
┌───────────────────────────────────────────────┐
│ STAGE 3: FINAL RANKING (Full Model)            │
│                                                │
│ A deep neural network (MTML — Multi-Task,      │
│ Multi-Label) re-ranks ~150 candidates.          │
│                                                │
│ Predicts simultaneously:                        │
│ • P(like) — will the user like it?              │
│ • P(comment) — will they comment?               │
│ • P(save) — will they save/bookmark?            │
│ • P(share) — will they share via DM?            │
│ • P(see-fewer) — will they tap "See Fewer       │
│   Posts Like This"? (negative signal)           │
│                                                │
│ Final score = weighted combination of           │
│ predicted probabilities.                        │
│                                                │
│ P(see-fewer) is a NEGATIVE weight — predicted   │
│ disinterest demotes the post.                   │
└────────────────────┬──────────────────────────┘
                     │
                     ▼
┌───────────────────────────────────────────────┐
│ BUSINESS RULES & DIVERSITY                     │
│                                                │
│ • No >N posts from the same account            │
│ • Mix content types (photos, Reels, carousels) │
│ • Inject fresh content (posts without enough   │
│   engagement history for accurate scoring)     │
│ • Content safety filter: remove/demote flagged │
│   content. Borderline content gets "reduced    │
│   distribution" even if it passes moderation.  │
│ • Exploration: inject some content outside the │
│   user's typical interests to avoid filter     │
│   bubbles                                      │
└────────────────────┬──────────────────────────┘
                     │
                     ▼
             Top ~30 items per page
```

### Explore Signals

| Category | Signals |
|---|---|
| **User signals** | Past likes, saves, comments, followed accounts, time spent, recent session behavior |
| **Media signals** | Engagement velocity, post type (photo/video/carousel), visual content features |
| **Author signals** | Account topic, engagement rate, relationship to viewer's interest graph |
| **Context signals** | Time of day, device type, user's current session behavior |

---

## 3. Reels Recommendation Feed

The Reels tab is a dedicated recommendation feed (similar to TikTok's For You page) that shows short-form videos from accounts the user may NOT follow.

The recommendation engine for Reels is similar to Explore but tuned for short-form video:

**Key differences from Explore:**
- **Watch-through rate** is the dominant signal (unique to video)
- **Audio/music signals** matter (trending sounds boost reach)
- **Video understanding** via deep learning (frame-level + pixel-level analysis)
- **Survey feedback**: "Was this Reel entertaining?" responses train the model [VERIFIED — Mosseri confirmed Instagram uses in-app surveys]

**Reels-specific ranking signals (VERIFIED):**

| Signal | Why It Matters |
|---|---|
| **Watch-through rate** | Strongest signal. Watching a 30s Reel to completion = strong interest |
| **Replays** | User replays the Reel = very strong interest |
| **Shares** | Sharing via DM = content is worth passing along |
| **Go-to-audio** | User visits the audio page = inspired to create their own Reel |
| **Likes** | Standard engagement signal (weaker than watch-through) |
| **Comments** | Engagement, but less predictive than watch-through for entertainment |
| **Audio trending** | Rising usage of a sound = trending content, boosted in recommendations |

---

## 4. Contrasts

### Instagram Explore vs TikTok For You

| Dimension | Instagram Explore | TikTok For You |
|---|---|---|
| **Content mix** | Photos + videos + Reels | Video only |
| **Social graph influence** | Yes (recommendations influenced by who you follow) | Minimal |
| **Exploration aggressiveness** | Conservative (leverages existing social graph) | Aggressive (shows unknown creators freely) |
| **Cold start** | Easier (bootstraps from social graph) | Harder (must learn from scratch) |
| **Signal density** | Lower (photos = less signal per impression) | Higher (short videos = many signals per minute) |
| **Model architecture** | MTML + IG2Vec + two-tower | Monolith (rumored), deep learning heavy |

### Instagram Search vs Google Search

| Dimension | Instagram Search | Google Search |
|---|---|---|
| **Corpus** | Users, hashtags, places, post captions | The entire web |
| **Ranking** | Personalized (social-graph-aware) | Primarily content-relevance-based |
| **Real-time** | Must index new content within minutes | Crawl latency (hours to days) |
| **Infrastructure** | Unicorn (custom) | Custom (Caffeine, etc.) |
| **Typeahead** | Yes (<100ms, personalized) | Yes (<50ms, location-aware) |

### Instagram Explore vs YouTube Recommendations

| Dimension | Instagram Explore/Reels | YouTube Recommendations |
|---|---|---|
| **Optimization target** | Engagement + session frequency (open app more often) | Watch time (longer sessions = more ads) |
| **Content length** | Short (photos, 15-90s Reels) | Long (minutes to hours) |
| **Signals per session** | Many weak signals (short content = more impressions) | Fewer strong signals (long watch = strong signal) |
| **Creator incentive** | Followers, brand deals | Revenue sharing (AdSense) |
| **Recommendation maturity** | Improving (catching up to TikTok) | Most mature in industry |
