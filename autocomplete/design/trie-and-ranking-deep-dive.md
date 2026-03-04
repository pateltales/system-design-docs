# Autocomplete System — Trie & Ranking Deep Dive

> This document covers the core data structure (trie) and ranking algorithms for the autocomplete system. The compressed trie with precomputed top-K at each node is **the** defining design decision — it determines query latency, memory footprint, and build complexity.

---

## Table of Contents

1. [Why Trie Over Other Data Structures](#1-why-trie-over-other-data-structures)
2. [Standard Trie: Basics and Limitations](#2-standard-trie-basics-and-limitations)
3. [Compressed Trie (Radix Tree / Patricia Tree)](#3-compressed-trie-radix-tree--patricia-tree)
4. [Precomputed Top-K at Each Node](#4-precomputed-top-k-at-each-node)
5. [Trie Operations: Search, Insert, Delete](#5-trie-operations-search-insert-delete)
6. [Ranking Algorithms](#6-ranking-algorithms)
7. [Personalization Layer](#7-personalization-layer)
8. [Content Moderation in the Trie](#8-content-moderation-in-the-trie)
9. [Alternative Approaches](#9-alternative-approaches)
10. [Trie Serialization and Deployment](#10-trie-serialization-and-deployment)

---

## 1. Why Trie Over Other Data Structures

The fundamental question is: given a prefix string (e.g., "amaz"), return the top-K most relevant query completions as fast as possible. Several data structures can support prefix-based lookups, but they differ dramatically in performance characteristics.

| Data Structure | Prefix Search | Lookup Time | Space | Top-K Support | Verdict |
|---|---|---|---|---|---|
| **Hash Map** | No native prefix | O(1) exact, O(n) prefix scan | O(n) | Separate structure | ❌ Can't do prefix lookups efficiently |
| **Sorted Array** | Yes (binary search for range) | O(log n + k) | O(n) | Sort/scan needed | ⚠️ Works but slow for large datasets |
| **B-Tree / B+ Tree** | Yes (range scan) | O(log n + k) | O(n) | Range scan | ⚠️ Disk-optimized, overhead for in-memory |
| **Standard Trie** | Yes | O(L) | O(ALPHABET × N) | Subtree traversal | ⚠️ Space-inefficient |
| **Compressed Trie** | Yes | O(L) | O(n) | Subtree traversal | ✅ Space-efficient prefix search |
| **Compressed Trie + Top-K** | Yes | O(L) | O(n + K per node) | **Precomputed O(1)** | ⭐ Best for autocomplete |

### Why Not a Hash Map?

Hash maps are optimized for exact-key lookups. To find all queries starting with "amaz", you'd need to iterate over all 500M keys — O(n) per query. At 580K req/sec, that's catastrophically slow.

You could build a hash map keyed by every possible prefix of every query, but for 500M queries with average length 20, that's 10 billion prefix entries — 10x the data, with massive redundancy.

### Why Not a Sorted Array?

A sorted array supports prefix search via binary search (find the first element ≥ "amaz" and scan forward). This is O(log n + k) where k is the number of matches. For 500M entries, log₂(500M) ≈ 29 comparisons for the binary search — not bad. But:

1. Finding the top-K by score requires scanning all matches (could be millions for short prefixes like "a") and sorting.
2. Updates require O(n) array shifting.

### Why a Trie Wins

A trie provides O(L) prefix lookup where L is the prefix length — **independent of the total number of queries**. Whether you have 1M or 1B queries, looking up a 4-character prefix takes 4 node hops. Combined with precomputed top-K at each node, the total query time is O(L) — typically microseconds.

---

## 2. Standard Trie: Basics and Limitations

A standard trie (from "re**trie**val") is a tree where each node represents one character, and paths from root to leaves represent complete strings.

### Example

For the queries: "amazon", "amazon prime", "apple", "app", "application":

```
                            ROOT
                           /    \
                         'a'     ...
                        /
                      'm'─────────'p'
                      /              \
                    'a'              'p'────────────'l'
                    /                  \              \
                  'z'              [app]★            'i'
                  /                  |                \
                'o'                'l'              'c'
                /                    \                \
              'n'                   'e'              'a'
              /                      \                \
          [amazon]★              [apple]★            't'
              |                                        \
             ' '                                      'i'
              |                                        \
             'p'                                      'o'
              |                                        \
             'r'                                      'n'
              |                                        \
             'i'                                   [application]★
              |
             'm'
              |
             'e'
              |
         [amazon prime]★

★ = terminal node (complete query)
```

### Space Analysis

Each node has up to 26 children (for lowercase a-z) + 10 digits + space + special characters ≈ 40 possible children.

If each child pointer is 8 bytes (64-bit pointer):
- Per node: 40 × 8 = **320 bytes** just for child pointers
- For 500M queries with average length 20: worst case ~10 billion characters → ~10 billion nodes
- Total: 10B × 320 bytes = **3.2 TB** — doesn't fit in memory!

In practice, most nodes have only 1-3 children, so the 40-slot array is massively wasteful. Using a hash map or sorted list for children helps, but the fundamental problem remains: **single-character chains waste nodes**.

In the example above, the path "a→m→a→z→o→n" is a chain of 6 nodes where each node has exactly one child. This is space-inefficient and creates unnecessary pointer chasing.

---

## 3. Compressed Trie (Radix Tree / Patricia Tree)

A compressed trie (also called a radix tree or Patricia tree) merges chains of single-child nodes into a single node with a multi-character edge label.

### Transformation: Standard Trie → Compressed Trie

```
STANDARD TRIE                          COMPRESSED TRIE

      ROOT                                  ROOT
     /    \                                /    \
   'a'     ...                        "am"    "app"
   /                                  /    \      \
 'm'──── 'p'                     "azon"  ...   [app]★──"l"
  |        |                      /                      \
 'a'      'p'──'l'          [amazon]★               "e"──"ication"
  |        |    |               |                    /        \
 'z'    [app]★  'i'          " prime"           [apple]★  [application]★
  |        |    |               |
 'o'      'l'  'c'        [amazon prime]★
  |        |    |
 'n'      'e'  'a'→'t'→'i'→'o'→'n'
  |        |
[amazon]★ [apple]★
  |
 ' '→'p'→'r'→'i'→'m'→'e'
  |
[amazon prime]★
```

### How Compression Works

1. **Identify chains**: Find sequences of nodes where each node has exactly one child.
2. **Merge into edges**: Replace the chain with a single edge labeled with the concatenated characters.
3. **Result**: Fewer nodes, more information per node.

### Space Savings

For 500M queries:
- Standard trie: ~10 billion nodes (one per character) → multi-TB
- Compressed trie: ~50-100M nodes (only at branching points and terminals) → **10-20x fewer nodes**

With the compressed trie:
- 50M nodes × (edge label ~10 bytes + children map ~50 bytes + metadata ~40 bytes) ≈ **5 GB**
- This fits comfortably in memory on a 32-64 GB server.

### Node Structure

```
CompressedTrieNode {
    children: Map<char, CompressedTrieNode>   // first char of edge → child node
    edge_label: string                         // characters on the edge TO this node
    is_terminal: bool                          // does this node represent a complete query?
    frequency: long                            // query frequency (if terminal)
    top_k: List<ScoredSuggestion>             // precomputed top-K (K=10)
}
```

### Edge Label Optimization

Instead of storing the full edge label string (which duplicates characters from the original query), we can store an offset + length into a separate string table:

```
// String table: all queries concatenated
string_table = "amazon\0amazon prime\0apple\0app\0application\0..."

// Edge label as reference
EdgeLabel {
    string_table_offset: int    // starting position in string table
    length: int                 // number of characters
}
```

This saves ~30-40% of memory for edge labels since many queries share substrings.

---

## 4. Precomputed Top-K at Each Node

This is the **single most important optimization** for autocomplete latency.

### The Problem Without Precomputation

Without precomputed top-K, a prefix query requires:
1. Traverse the trie to the node matching the prefix — O(L)
2. **Traverse the entire subtree** to find all terminal nodes — O(subtree size)
3. Sort all terminal nodes by score — O(m log m) where m = number of matches
4. Return the top K

For a short prefix like "a", the subtree could contain **millions of queries**. This means:
- Traversal of millions of nodes
- Sorting millions of results
- Total time: potentially **hundreds of milliseconds**

This violates our sub-50ms latency requirement.

### The Solution: Precompute Top-K at Every Node

At trie build time, for every node in the trie, store the top-K highest-scored suggestions from its entire subtree:

```
                              ROOT
                   top_k: [amazon prime, iphone 15, samsung galaxy,
                           nike shoes, playstation 5, macbook pro,
                           air jordan, kindle, echo dot, fire stick]
                              │
                    ┌─────────┼──────────┐
                    │         │          │
               "am"          "ip"      "sa"
                │             │          │
         ┌──────▼──────┐  ┌──▼──────┐  ┌▼──────────┐
         │  top_k:     │  │ top_k:  │  │ top_k:    │
         │ [amazon     │  │[iphone  │  │[samsung   │
         │  prime,     │  │ 15,     │  │ galaxy,   │
         │  amazon     │  │ ipad,   │  │ samsung   │
         │  kindle,    │  │ iphone  │  │ tv, ...]  │
         │  american   │  │ case]   │  │           │
         │  express,   │  │         │  └───────────┘
         │  ...]       │  └─────────┘
         └──────┬──────┘
                │
          "azon"│
                │
         ┌──────▼──────────┐
         │  top_k:         │
         │ [amazon prime,  │
         │  amazon kindle, │
         │  amazon music,  │
         │  amazon fresh,  │
         │  ...]           │
         └──────┬──────────┘
                │
         " prime"
                │
         ┌──────▼──────────┐
         │  top_k:         │
         │ [amazon prime,  │
         │  amazon prime   │
         │   video,        │
         │  amazon prime   │
         │   day, ...]     │
         │  is_terminal: ✓ │
         │  freq: 5000000  │
         └─────────────────┘
```

### Build Algorithm (Bottom-Up Propagation)

```
function buildTopK(node):
    if node is terminal:
        node.top_k = [{query: node.query, score: node.score}]

    // Recursively build top-K for all children
    for each child in node.children:
        buildTopK(child)

    // Merge top-K from all children (and self if terminal)
    candidates = []
    if node.is_terminal:
        candidates.append({query: node.query, score: node.score})
    for each child in node.children:
        candidates.extend(child.top_k)

    // Sort by score descending, keep top K
    candidates.sort(by: score, descending)
    node.top_k = candidates[0:K]     // K = 10
```

**Time complexity**: O(n × K × log K) where n = number of nodes
- For each of the 50M nodes, we merge up to (children_count × K) candidates and take top K
- In practice, most nodes have 2-5 children, so each merge is small

**Build time**: ~10-15 minutes for 50M nodes on a 32-core machine — perfectly acceptable for an hourly batch job.

### Query-Time Behavior

```
function search(prefix):
    node = root
    i = 0

    while i < len(prefix):
        // Find child edge starting with prefix[i]
        child = node.children.get(prefix[i])
        if child is null:
            return []    // no matches

        edge = child.edge_label

        // Match prefix characters against edge label
        j = 0
        while j < len(edge) and i < len(prefix):
            if prefix[i] != edge[j]:
                return []    // mismatch
            i++
            j++

        // If we consumed the entire prefix mid-edge, we're at the right node
        // If we consumed the entire edge, continue to the child
        node = child

    return node.top_k    // O(1) — just return the precomputed list!
```

**Query time**: O(L) where L = prefix length. For a typical 4-character prefix, this is 2-3 node hops — **microseconds**.

### Space Analysis

| Component | Size Calculation | Total |
|---|---|---|
| Trie structure (50M nodes) | 50M × 100 bytes (edge, children, metadata) | ~5 GB |
| Top-K per node (index approach) | 50M × 10 × 4 bytes (indices into string table) | ~2 GB |
| String table (500M queries) | 500M × 20 bytes average | ~10 GB |
| **Total** | | **~17 GB** |

With optimization (deduplicated string table, shared prefixes):
- String table shrinks to ~3-4 GB (shared prefixes)
- **Optimized total: ~7-10 GB**

### Tradeoff Summary

| Factor | Without Top-K | With Top-K |
|---|---|---|
| Query Time | O(L + subtree) — milliseconds for popular prefixes | O(L) — microseconds |
| Build Time | O(n) | O(n × K) — minutes, offline |
| Space | O(n) — ~5 GB | O(n + n × K) — ~10 GB |
| Real-time Updates | Easy — insert/delete at any time | Hard — need top-K re-propagation |
| **Best for** | Systems with frequent updates | Systems with infrequent updates, fast reads |

**Why the tradeoff is worth it**: Query latency is the critical metric. We're trading ~5 GB of extra RAM (cheap) and a few minutes of build time (offline, irrelevant) for **orders-of-magnitude faster queries**. Since we rebuild the trie hourly anyway, the update cost is amortized.

---

## 5. Trie Operations: Search, Insert, Delete

### Search (Query Time)

Already covered above. Key points:
- O(L) traversal + O(1) top-K read
- Handle edge cases: empty prefix (return global top-K from root), no match (return empty list)
- Partial edge matching: if the prefix ends in the middle of an edge, we're at the correct subtree

### Insert (Build Time Only)

```
function insert(root, query, score):
    node = root
    i = 0

    while i < len(query):
        char = query[i]

        if char not in node.children:
            // Create new child with remaining query as edge label
            newNode = TrieNode(edge_label: query[i:], is_terminal: true, score: score)
            node.children[char] = newNode
            return

        child = node.children[char]
        edge = child.edge_label

        // Find where the query and edge diverge
        j = 0
        while j < len(edge) and i < len(query) and edge[j] == query[i]:
            j++
            i++

        if j == len(edge):
            // Consumed entire edge — continue to child
            node = child
        else:
            // Divergence mid-edge — need to split
            // Create a split node at the divergence point
            splitNode = TrieNode(edge_label: edge[0:j])

            // Old child becomes a child of the split node
            child.edge_label = edge[j:]
            splitNode.children[edge[j]] = child

            // New query branch
            if i < len(query):
                newNode = TrieNode(edge_label: query[i:], is_terminal: true, score: score)
                splitNode.children[query[i]] = newNode
            else:
                splitNode.is_terminal = true
                splitNode.score = score

            node.children[char] = splitNode
            return

    // Query exactly matches existing path
    node.is_terminal = true
    node.score = score
```

### Why We Don't Do Real-Time Inserts

Real-time inserts into the serving trie are problematic:

1. **Concurrency**: Insert can split edges, which requires modifying the parent node's children map and creating new nodes. If a concurrent read is traversing the same path, it could see an inconsistent state.

2. **Top-K invalidation**: Inserting a new high-frequency query requires re-propagating top-K from the insertion point up to the root. This is O(depth × K) per insert, and at 100K inserts/sec, it becomes a bottleneck.

3. **Memory management**: Creating and destroying nodes at high frequency leads to memory fragmentation and GC pressure.

**Solution**: Batch rebuild. Build a new trie from scratch every hour, swap atomically using blue-green deployment.

### Delete

Deletion follows the reverse of insertion — remove the terminal marker, and if the node has no children, merge it with its parent. In practice, we don't delete individual queries from the serving trie. Instead, we exclude them from the next batch build (via the content filter or by their frequency dropping below threshold).

---

## 6. Ranking Algorithms

### 6.1 Raw Frequency

The simplest ranking: `score(q) = count(q)` — how many times was this query searched.

**Problem**: Historical queries dominate. "iPhone 14" might have more total searches than "iPhone 16" just because it's been around longer. Doesn't capture recency.

### 6.2 Time-Decayed Frequency

Apply exponential decay to older searches:

```
score(q) = Σ (count_per_day(q, d) × e^(-λ × age(d)))
```

Where:
- `count_per_day(q, d)` = number of times query q was searched on day d
- `age(d)` = number of days ago day d was (0 = today, 1 = yesterday, ...)
- `λ` = decay constant. We use λ = 0.1 → half-life ≈ 7 days

#### Worked Example

Query: "iphone 15 case"

| Day | Age (days) | Count | Decay Factor (e^(-0.1 × age)) | Weighted Count |
|---|---|---|---|---|
| Today | 0 | 50,000 | 1.000 | 50,000 |
| Yesterday | 1 | 45,000 | 0.905 | 40,722 |
| 2 days ago | 2 | 48,000 | 0.819 | 39,304 |
| 3 days ago | 3 | 42,000 | 0.741 | 31,113 |
| 7 days ago | 7 | 40,000 | 0.497 | 19,880 |
| 14 days ago | 14 | 55,000 | 0.247 | 13,576 |
| 30 days ago | 30 | 60,000 | 0.050 | 2,988 |

**Time-decayed score**: 50,000 + 40,722 + 39,304 + ... ≈ **250,000** (vs raw count of ~2,000,000 over 30 days)

The decay ensures that a query's score is dominated by its recent popularity, not historical volume.

### 6.3 Multi-Signal Weighted Scoring

In production, we combine multiple signals:

```
final_score(q) = w1 × frequency_decayed(q)
               + w2 × trending_boost(q)
               + w3 × conversion_rate(q)
               + w4 × freshness_bonus(q)
```

| Signal | Weight | Calculation | Rationale |
|---|---|---|---|
| **Frequency (time-decayed)** | 0.50 | Exponential decay formula above | Core popularity signal |
| **Trending boost** | 0.20 | `min(current_rate / historical_avg, 10.0)` — capped at 10x | Surface spiking queries quickly |
| **Conversion rate** | 0.20 | `purchases / searches` for this query | Queries that lead to purchases are more valuable (Amazon-specific) |
| **Freshness bonus** | 0.10 | `1.0 / (1 + days_since_first_seen)` | New queries get a small discovery boost |

#### Why These Weights?

- **Frequency at 0.5**: Most users expect popular queries. "amazon prime" should almost always appear for prefix "ama".
- **Trending at 0.2**: Captures real-world events. On Black Friday, "black friday deals" should spike into top suggestions.
- **Conversion at 0.2**: Amazon-specific — queries that drive purchases are business-critical.
- **Freshness at 0.1**: Small weight to help new products/brands get discovered.

These weights are tunable via A/B testing. We'd run experiments with different weight configurations and measure suggestion click-through rate (CTR) and downstream conversion.

### 6.4 Trending Boost Algorithm

The streaming pipeline (Flink) computes trending scores in real-time:

```
function trendingScore(query, current_window):
    current_count = count(query) in last 5 minutes

    // Historical baseline: average count for this time-of-day over last 7 days
    historical_avg = average(
        count(query) at same_time_of_day
        for last 7 days
    )

    if historical_avg == 0:
        // Brand new query — never seen before
        if current_count > MIN_THRESHOLD (e.g., 100):
            return current_count * NEW_QUERY_BOOST (e.g., 2.0)
        return 0

    ratio = current_count / historical_avg

    if ratio > SPIKE_THRESHOLD (e.g., 5.0):
        // Query is spiking — apply trending boost
        return min(ratio, MAX_BOOST (e.g., 10.0))

    return 0    // not trending
```

**Example**: "Prime Day deals" normally gets 1,000 searches per 5-minute window. On Prime Day, it spikes to 50,000. Ratio = 50x → trending boost = 10.0 (capped).

### 6.5 Category-Aware Ranking

If the user's current context provides a category signal (e.g., they're browsing the Electronics page), we can boost relevant suggestions:

```
context_boost(query, user_context):
    query_category = category_map.get(query)    // "iphone 15" → "Electronics"

    if query_category == user_context.current_category:
        return CATEGORY_BOOST (e.g., 1.5)
    return 1.0
```

This is applied at **serving time** (not baked into the trie) because it depends on the user's current session context.

---

## 7. Personalization Layer

### Architecture

Personalization is a **serving-time overlay** on top of the global trie:

```
┌─────────────────────────────────────────┐
│              Autocomplete Service        │
│                                          │
│  1. Fetch top-K from global trie (base) │
│                                          │
│  2. Fetch user profile from Redis       │
│     (last 50 searches + preferences)     │
│                                          │
│  3. Re-rank:                             │
│     boosted_score = base_score            │
│       × (1 + personal_boost)             │
│                                          │
│  4. Return re-ranked top-K               │
└─────────────────────────────────────────┘
```

### User Profile Structure (in Redis)

```
Key: user:{hashed_user_id}:profile
TTL: 30 days

Value (JSON):
{
    "recent_searches": [
        {"query": "iphone 15 pro max", "ts": 1707350400, "category": "Electronics"},
        {"query": "usb c cable", "ts": 1707264000, "category": "Electronics"},
        {"query": "running shoes", "ts": 1707177600, "category": "Clothing"},
        ...
    ],
    "top_categories": ["Electronics", "Clothing", "Books"],
    "search_count": 247
}
```

**Memory per user**: ~500 bytes (50 recent searches × ~10 bytes each)
**Total for 100M active users**: 100M × 500B = **50 GB** — a modest Redis cluster

### Personalization Scoring

```
function personalBoost(suggestion, user_profile):
    boost = 0.0

    // Signal 1: Category match — does the suggestion match user's preferred categories?
    suggestion_category = categoryOf(suggestion.query)
    if suggestion_category in user_profile.top_categories:
        boost += 0.3   // 30% boost for matching category

    // Signal 2: Query similarity — has the user searched for similar queries?
    for recent in user_profile.recent_searches:
        similarity = prefixOverlap(suggestion.query, recent.query)
        recency_weight = e^(-0.1 * days_since(recent.ts))
        boost += 0.2 * similarity * recency_weight

    // Signal 3: Repeat query — user searched this exact query before?
    if suggestion.query in user_profile.recent_searches:
        boost += 0.5   // 50% boost for repeat queries

    return min(boost, MAX_PERSONAL_BOOST (e.g., 2.0))
```

### Cold Start

For new users with no search history:
1. **No personalization** — fall back to global ranking. The system works perfectly without personalization; it's a quality enhancement, not a requirement.
2. **Category context** — if the user is browsing a specific category page, use that as a weak signal.
3. **Device-based defaults** — mobile users might prefer shorter, more action-oriented queries.

### Privacy Considerations

- User IDs are SHA-256 hashed before storage
- Retention policy: 30-day TTL on user profiles in Redis
- User opt-out: if user disables personalization, their profile is deleted and not rebuilt
- The trie itself contains **no PII** — only aggregated query frequencies
- Query logs are anonymized (hashed user_id) and retained for 30 days maximum

---

## 8. Content Moderation in the Trie

### Defense in Depth

Content filtering operates at three levels:

```
                    LEVEL 1: BUILD-TIME                    LEVEL 2: SERVE-TIME              LEVEL 3: EMERGENCY
                    (strongest, slowest)                   (fast safety net)                 (immediate)

               ┌─────────────────────┐              ┌──────────────────┐              ┌──────────────┐
               │ During trie build:  │              │ At query time:   │              │ Admin API:   │
               │                     │              │                  │              │              │
               │ 1. Check each query │              │ 1. Filter results│              │ 1. Add term  │
Query ────────▶│    against blocklist│──────────────▶│    against Redis │──────────────▶│    to Redis  │
               │ 2. ML classifier    │   In trie    │    blocklist     │  To user     │    blocklist │
               │    (flag suspicious)│              │ 2. Return clean  │              │ 2. Effective │
               │ 3. Human review     │              │    results       │              │    immediately│
               │    (flagged items)  │              │                  │              │              │
               └─────────────────────┘              └──────────────────┘              └──────────────┘

               Catches: known bad                   Catches: anything                 Catches: newly
               queries before they                   that slipped through              discovered
               enter the trie                        build-time filter                 offensive terms

               Latency: hours                       Latency: +0.1ms                   Latency: seconds
               (next trie build)                     (Redis lookup)                    (Redis update)
```

### Blocklist Structure

```
// Redis-based blocklist
Key: content:blocklist
Type: Set (for O(1) lookup)

Members:
- "offensive_term_1"
- "offensive_term_2"
- "regex:pattern_.*_bad"     // regex patterns for flexible matching
- ...

// Separate set for category-level blocks
Key: content:blocklist:categories
Members:
- "adult_content"
- "hate_speech"
- "violence"
```

### Edge Cases

1. **Substring problem**: Blocking "bad" shouldn't block "badminton" or "not bad movie review". Solution: blocklist entries are matched as **complete queries or complete words within queries**, not arbitrary substrings.

2. **Unicode tricks**: Users might use lookalike characters (е instead of e). Solution: normalize to ASCII/canonical form before blocklist checking.

3. **Compound queries**: "how to make a [blocked term]" — the complete query might not be in the blocklist. Solution: tokenize the query and check each token against the blocklist.

4. **New offensive content**: ML classifier (trained on known offensive content) flags new queries for human review. Flagged queries are held out of the trie until reviewed.

### ML Classifier Integration

```
function classifyQuery(query):
    // Fast check: exact match against blocklist
    if query in blocklist:
        return BLOCKED

    // ML check: is this query potentially offensive?
    score = offensiveness_model.predict(query)    // pre-trained NLP model

    if score > HIGH_THRESHOLD (e.g., 0.9):
        return BLOCKED      // very likely offensive
    elif score > MEDIUM_THRESHOLD (e.g., 0.5):
        return FLAGGED      // needs human review
    else:
        return ALLOWED
```

The ML model runs during the trie build phase (offline) — it doesn't add latency to serving.

---

## 9. Alternative Approaches

### Finite State Transducers (FSTs)

FSTs (used by Apache Lucene) are like compressed tries but even more space-efficient:

| Aspect | Compressed Trie | FST |
|---|---|---|
| Space | O(n) — good | O(n) — **better** (shares suffixes too) |
| Build time | O(n log n) | O(n) — requires sorted input |
| Prefix search | O(L) | O(L) |
| Top-K support | Can precompute | Harder to precompute (shared structure) |
| Mutability | Immutable (rebuild) | Immutable (rebuild) |
| Implementation | Straightforward | Complex (Lucene's FST builder is ~2000 lines of Java) |

**Verdict**: FSTs are better for disk-based indices (Lucene's use case) where space is critical. For in-memory autocomplete where we have plenty of RAM, the compressed trie is simpler and supports precomputed top-K more naturally.

### ElasticSearch Prefix Queries

ElasticSearch supports `prefix` and `completion` suggesters:

```json
{
    "suggest": {
        "query-suggest": {
            "prefix": "amaz",
            "completion": {
                "field": "suggest",
                "size": 10
            }
        }
    }
}
```

| Aspect | Custom Trie | ElasticSearch |
|---|---|---|
| Latency | ~0.1ms (in-memory) | ~5-20ms (network + disk I/O) |
| Fuzzy matching | No (prefix only) | Yes (edit distance) |
| Customization | Full control | Limited to ES features |
| Operational overhead | Build + deploy pipeline | Manage ES cluster |
| Scale | Simple (in-memory, replicate) | Complex (sharding, replication, JVM tuning) |

**Verdict**: ElasticSearch is overkill for prefix-only autocomplete. Its strength is fuzzy matching and full-text search. For our use case (exact prefix, top-K, sub-50ms), a custom trie is simpler and faster.

### Ternary Search Tree (TST)

A TST is a space-efficient alternative to a standard trie where each node has three children (less than, equal, greater than):

| Aspect | Compressed Trie | TST |
|---|---|---|
| Space | O(n) — compressed | O(n) — no array per node |
| Lookup | O(L) | O(L × log ALPHABET) |
| Prefix search | Natural | Natural |
| Cache behavior | Good (compressed edges) | Poor (many pointer chases) |
| Top-K | Easy to precompute | Same as trie |

**Verdict**: TSTs save space over standard tries but are outperformed by compressed tries (radix trees) in both space and lookup time. Not recommended for this use case.

---

## 10. Trie Serialization and Deployment

### Binary Serialization Format

The trie is serialized for transport (S3) and fast loading on trie servers:

```
TRIE FILE FORMAT (v1)
=====================

Header (32 bytes):
┌──────────────────────────────────────────────┐
│ magic: "TRIE" (4 bytes)                      │
│ version: 1 (4 bytes)                         │
│ node_count: uint64 (8 bytes)                 │
│ string_table_offset: uint64 (8 bytes)        │
│ checksum: uint32 (4 bytes)                   │
│ padding: (4 bytes)                           │
└──────────────────────────────────────────────┘

Node Array (breadth-first order):
┌──────────────────────────────────────────────┐
│ Node 0 (ROOT):                               │
│   edge_label_offset: uint32                  │
│   edge_label_length: uint16                  │
│   children_count: uint8                      │
│   is_terminal: bool                          │
│   frequency: uint64                          │
│   top_k_offset: uint32 (into top-K table)    │
│   children: [(char, node_index), ...]        │
├──────────────────────────────────────────────┤
│ Node 1: ...                                  │
├──────────────────────────────────────────────┤
│ Node 2: ...                                  │
└──────────────────────────────────────────────┘

Top-K Table:
┌──────────────────────────────────────────────┐
│ [string_table_offset, score] × K per node    │
└──────────────────────────────────────────────┘

String Table:
┌──────────────────────────────────────────────┐
│ "amazon\0amazon prime\0apple\0..."           │
└──────────────────────────────────────────────┘
```

### Memory-Mapped Loading (mmap)

Instead of reading the entire file into memory and parsing it, we use `mmap`:

```
// Loading the trie
fd = open("trie_v42.bin", O_RDONLY)
trie_data = mmap(fd, file_size, PROT_READ, MAP_PRIVATE)

// Accessing a node is just pointer arithmetic
node = (TrieNode*)(trie_data + header_size + node_index * node_size)
```

**Benefits**:
- **Fast startup**: No deserialization — the file IS the in-memory representation
- **OS page cache**: The OS manages which pages are in RAM
- **Shared memory**: Multiple processes can mmap the same file (useful for multi-process serving)

**Caveat**: On first access, pages are faulted in from disk. Pre-touch all pages on startup:

```
// Pre-fault all pages to avoid page faults during serving
for (offset = 0; offset < file_size; offset += PAGE_SIZE):
    volatile_read(trie_data[offset])    // touch each page
```

### Blue-Green Deployment

```
DEPLOYMENT FLOW:

     ┌────────────┐     ┌────────────┐     ┌────────────┐
     │ Trie       │     │    S3      │     │ Trie Server │
     │ Builder    │────▶│ (artifact) │────▶│ Pool: BLUE  │◄──── Traffic (ACTIVE)
     │            │     │            │     │ (v41)       │
     └────────────┘     └─────┬──────┘     └────────────┘
                              │
                              │ Pull v42
                              ▼
                        ┌────────────┐
                        │ Trie Server │
                        │ Pool: GREEN │◄──── No traffic (STANDBY)
                        │ (loading    │
                        │  v42...)    │
                        └──────┬─────┘
                               │
                         Health check ✓
                               │
                        ┌──────▼─────┐
                        │ Load       │
                        │ Balancer   │
                        │ SWITCH!    │──── Traffic now → GREEN (v42)
                        └──────┬─────┘
                               │
                        ┌──────▼─────┐
                        │ BLUE (v41) │──── Now standby (drains, becomes next GREEN)
                        └────────────┘
```

**Rollback**: If v42 shows degraded quality metrics (CTR drop > 20% in first 10 minutes), switch traffic back to BLUE (v41). Takes seconds — just a load balancer config change.

### Version Management

- Keep last 5 versions in S3 for rollback
- Each version is identified by build timestamp: `trie_2026020814.bin` (Feb 8, 2026, 2 PM build)
- Trie servers report their current version via health check endpoint
- Dashboard shows which version each server is running

---

*This deep dive complements the [Interview Simulation](interview-simulation.md) and is referenced by the [System Flows](flow.md) and [Scaling & Caching](scaling-and-caching.md) documents.*
