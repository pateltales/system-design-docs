# Social Graph — Storage & Fan-out

> The social graph is the backbone of Instagram. Every follow, unfollow, block, and mute is a graph operation.
> Scale: 2B+ nodes, hundreds of billions of edges, extreme degree variance (1 to 650M+).

---

## Table of Contents

1. [Graph Structure](#1-graph-structure)
2. [Scale Numbers](#2-scale-numbers)
3. [Storage: TAO (Meta's Graph Store)](#3-storage-tao-metas-graph-store)
4. [Fan-out Implications](#4-fan-out-implications)
5. [Special Graph Features](#5-special-graph-features)
6. [Sharding & Performance](#6-sharding--performance)
7. [Contrasts](#7-contrasts)

---

## 1. Graph Structure

Instagram's social graph is a **directed graph**.

```
A ──follows──> B      (A follows B — does NOT mean B follows A)
B ──follows──> A      (B follows A — explicitly separate edge)
```

**Edges have metadata:**
- `timestamp` — when the follow happened
- `close_friend` — boolean, is this person on A's Close Friends list
- `notification_preferences` — does A want notifications from B
- `status` — CONFIRMED or PENDING (for private accounts)

**This is different from Facebook's undirected friendship graph:**
```
Facebook:  A ──friends── B    (symmetric, mutual consent required)
Instagram: A ──follows──> B   (asymmetric, no consent needed for public accounts)
```

The directed graph enables the **creator/audience model** — a celebrity can have 650M followers without following anyone back. This asymmetry is fundamental to Instagram's product and has deep architectural implications for fan-out.

---

## 2. Scale Numbers

| Metric | Value | Confidence |
|---|---|---|
| Total users (nodes) | 2B+ MAU | HIGH (Meta official) |
| Total follow edges | Hundreds of billions | INFERRED (2B users × avg ~200 following) |
| Max followers (single node) | ~650M+ (Cristiano Ronaldo) | HIGH (public data) |
| Max following (per user) | 7,500 (platform limit) | HIGH (Instagram Help Center) |
| Following rate limit | ~60-100 follows/hour | INFERRED (anti-spam measure) |
| Degree distribution | Power-law (most users have few followers, few have millions) | HIGH |

---

## 3. Storage: TAO (Meta's Graph Store)

Instagram, as part of Meta, uses **TAO (The Associations and Objects)** for social graph storage. [VERIFIED — from TAO paper, USENIX ATC 2013, by Bronson et al.]

### TAO Data Model

TAO stores two primitives:
- **Objects** — typed nodes with a 64-bit ID and key-value data fields (e.g., a User, a Photo, a Comment)
- **Associations (edges)** — typed, directed edges between two objects, identified by `(id1, atype, id2)`, with a 32-bit timestamp enabling time-ordered association lists

For Instagram's social graph:
```
Object: User(id=42, username="travelphotographer", ...)
Object: User(id=99, username="chef_maria", ...)

Association: (user_42, FOLLOWS, user_99, timestamp=1705312800)
    → "user 42 follows user 99"

Association list: FOLLOWERS(user_99) → [(user_42, ts), (user_55, ts), ...]
    → "all followers of user 99, ordered by time"
```

### TAO Architecture

```
┌─────────────┐     ┌─────────────┐     ┌─────────────┐
│  Client App │────>│ Leaf Cache  │────>│ Root Cache  │────> MySQL
│             │     │ (L1)        │     │ (L2)        │     (sharded)
└─────────────┘     └─────────────┘     └─────────────┘
                    Many per region      Few per region     Persistent store
                    Serve reads          Coordinate writes  Source of truth
```

**Read path:** Client → L1 leaf cache (on hit, return) → L2 root cache (on hit, return) → MySQL (on miss, read and populate caches)

**Write path:** Client → Leader region's L2 root cache → MySQL → root cache invalidates L1 leaf caches asynchronously

**Multi-region:**
- One region is the **leader** (MySQL master)
- Other regions are **followers** (MySQL replicas)
- Writes from follower regions are forwarded to the leader
- After commit, leader sends **cache invalidation messages** to follower regions
- Consistency: **eventual consistency** across regions, **read-after-write consistency** within the leader region

**Why TAO instead of a generic key-value store?**
- TAO is purpose-built for the "objects and associations" pattern — `(id1, type, id2)` with time-ordered lists
- Association lists are naturally ordered by time — `FOLLOWERS(user_99)` returns followers in reverse-chronological order without an explicit sort
- The two-tier cache architecture handles the read-heavy workload (500:1 read-to-write ratio)
- TAO handles cache invalidation and consistency across regions — developers don't think about caching

---

## 4. Fan-out Implications

The social graph's **degree distribution** directly determines fan-out cost.

### Degree Distribution (Power-Law)

```
Follower count distribution (approximate):

  Follower Count   |  % of Users  |  Fan-out Cost per Post
  -----------------+--------------+------------------------
  0 - 100          |  ~60%        |  Trivial (0-100 writes)
  100 - 1,000      |  ~25%        |  Low (100-1K writes)
  1,000 - 10,000   |  ~10%        |  Moderate (1K-10K writes)
  10,000 - 100,000 |  ~4%         |  Significant (10K-100K writes)
  100K - 1M        |  ~0.9%       |  Heavy (100K-1M writes)
  1M - 10M         |  ~0.09%      |  Very heavy (1M-10M writes)
  10M - 100M       |  ~0.009%     |  Celebrity (10M-100M writes)
  100M+            |  ~0.001%     |  Mega-celebrity (100M+ writes)
```

**The key insight:** 99%+ of users have <100K followers. Fan-out on write works perfectly for them. The 0.01% with 1M+ followers are the ones that break the model — these are handled via fan-out on read.

### Follow/Unfollow Side Effects

**Follow:**
```
User A follows User B
    │
    ├── Write association: (A, FOLLOWS, B) to TAO
    ├── Write reverse association: (B, FOLLOWED_BY, A) to TAO
    ├── Increment follower_count(B) — async, approximate counter
    ├── Increment following_count(A) — async, approximate counter
    ├── Backfill A's feed inbox with B's recent posts — async
    ├── Send notification to B: "A started following you" — async
    └── Update A's recommendation profile (social graph changed) — async
```

**Unfollow:**
```
User A unfollows User B
    │
    ├── Delete association: (A, FOLLOWS, B) from TAO
    ├── Delete reverse association: (B, FOLLOWED_BY, A) from TAO
    ├── Decrement follower_count(B) — async
    ├── Decrement following_count(A) — async
    ├── Remove B's posts from A's feed inbox — async (expensive)
    └── No notification sent (unfollows are silent)
```

---

## 5. Special Graph Features

### Close Friends

A labeled subgraph where users curate a list of followers who see special Stories.

```
Association: (user_A, CLOSE_FRIEND, user_B)
```

- Close Friends Stories are only distributed to users in the Close Friends list
- This creates a separate fan-out path for Stories: check if `is_close_friend` before including in the Stories tray
- Stored as a separate association type in TAO

### Private Accounts

For private accounts, the follow edge goes through a **pending** state:

```
State machine:
    ┌──────────────┐     ┌──────────────┐     ┌──────────────┐
    │  NO_RELATION  │────>│  REQUESTED    │────>│  FOLLOWING    │
    └──────────────┘     └──────────────┘     └──────────────┘
         POST /follow         Approve              Confirmed
                              └───────────────────>│  DENIED     │
                                    Deny           └──────────────┘
```

- Feed fan-out only happens for CONFIRMED followers
- Pending follow requests are stored as a separate association type
- Private account followers are a subset of total followers — the "follower" count only includes confirmed followers

### Block and Restrict

- **Block**: Removes all edges between the two users. Blocks are stored as a separate association type. Blocked users cannot follow, view posts, DM, or appear in search for the blocker.
- **Restrict**: Soft block. The restricted user's comments are hidden from others but visible to themselves. They don't know they're restricted. Stored as an edge attribute.

### Mutual Followers

`GET /users/{userId}/mutual-followers` requires set intersection:

```
mutual = FOLLOWERS(target_user) ∩ FOLLOWING(current_user)
```

At scale, this is done by:
1. Fetching both sets from TAO's cache
2. Computing the intersection in-memory on the application server
3. For large sets, capping at the first N mutual followers (the UI only shows a few)

---

## 6. Sharding & Performance

### TAO Sharding

TAO shards data by `id1` (the source object ID):
- **Object sharding**: Objects are sharded by their own ID
- **Association sharding**: Associations `(id1, type, id2)` are sharded by `id1`
- This means: all of user X's outgoing associations (who X follows, what X liked) are on the same shard → efficient single-shard queries for "who does X follow?"
- But: "who follows X?" (incoming associations) requires reading from the shard of each follower — this is why TAO uses the reverse association `FOLLOWED_BY(X)` stored on X's shard

### Hot Shard Problem

Celebrity accounts create **hot shards**:
- Cristiano Ronaldo's `FOLLOWED_BY` association list has 650M+ entries
- Any query that touches this shard is expensive
- Mitigations:
  - **Follower count**: Use a separate denormalized counter, not `COUNT(FOLLOWED_BY)`
  - **Follower list pagination**: Never load the full list — always paginated
  - **Caching**: The L1/L2 cache hierarchy absorbs most reads
  - **Read replicas**: Multiple cache replicas for hot data

### Read Performance

| Operation | Typical Latency | Path |
|---|---|---|
| Check if A follows B | ~1ms | L1 cache hit (single association lookup) |
| Get A's following list (page 1) | ~2ms | L1 cache hit (association list scan) |
| Get B's follower count | ~1ms | Denormalized counter in L1 cache |
| Get mutual followers | ~5-10ms | Two list fetches + in-memory intersection |

---

## 7. Contrasts

### Instagram vs Twitter — Social Graph

| Dimension | Instagram | Twitter (X) |
|---|---|---|
| **Graph type** | Directed (follow) | Directed (follow) |
| **Max following** | 7,500 | 5,000 (historically) |
| **Private accounts** | Yes (follow request required) | No (all accounts public by default, protected accounts exist but rare) |
| **Close Friends** | Yes (labeled subgraph) | No equivalent |
| **Lists** | No equivalent | Yes (curated subsets of following) |
| **Storage** | TAO (Meta's graph store) | FlockDB → Manhattan |
| **Degree variance** | Extreme (1 to 650M+) | Extreme (1 to 100M+) |

### Instagram vs Facebook — Social Graph

| Dimension | Instagram | Facebook |
|---|---|---|
| **Graph type** | Directed (follow) | Undirected (friendship) |
| **Symmetry** | Asymmetric (A follows B ≠ B follows A) | Symmetric (A friends B = B friends A) |
| **Consent** | No consent needed (public accounts) | Mutual consent required |
| **Creator model** | Enables creator/audience (one-to-many) | Friend model (many-to-many) |
| **Fan-out complexity** | Higher (degree variance is extreme) | Lower (friendships are bounded, max 5,000) |
| **Infrastructure** | TAO (shared with Facebook) | TAO |

### Instagram vs TikTok — Social Graph

| Dimension | Instagram | TikTok |
|---|---|---|
| **Graph importance** | Critical (feed is graph-based) | Minimal (feed is recommendation-based) |
| **Follow graph** | Directed, essential for feed | Directed, but For You page ignores it |
| **Fan-out** | Required (posts go to followers) | Not required (posts go to recommendation engine) |
| **Creator discovery** | Graph-limited (need followers to get views) | Graph-independent (any video can go viral) |

**Key insight:** TikTok's minimal dependence on the social graph is a fundamental architectural simplification. TikTok doesn't need fan-out infrastructure, feed inboxes, or the hybrid celebrity threshold. Instead, it invests all that engineering effort into its recommendation engine. Instagram needs BOTH because it runs two distribution models (social-graph feed + recommendation-based Reels).
