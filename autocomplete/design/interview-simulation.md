# System Design Interview Simulation: Design an Autocomplete / Typeahead Suggestion System

> **Interviewer:** Principal Engineer (L7), Amazon Search Infrastructure
> **Candidate Level:** SDE-2 (L5 — Software Development Engineer II)
> **Duration:** ~60 minutes
> **Date:** February 8, 2026

---

## PHASE 1: Opening & Problem Statement (~2 min)

**Interviewer:**
Hey, welcome. I'm [name], Principal Engineer on the search infrastructure team here at Amazon. For today's system design round, I'd like you to design an **autocomplete / typeahead suggestion system** — the kind of thing you see when you start typing in the Amazon search bar and suggestions appear in real-time as you type.

I care about how you decompose the problem, the data structure choices you make, and how you think about latency at scale. I'll push on your decisions — that's me calibrating depth, not a negative signal.

Take it away.

---

## PHASE 2: Requirements Gathering & Scoping (~5 min)

**Candidate:**
Thanks! Before I jump into architecture, I want to align on scope. Autocomplete can range from a simple prefix-match system to a full-blown search-as-you-type engine with ML-based ranking. Let me ask some clarifying questions.

**Functional Requirements — what behavior do we need?**

> "The core operation is: user types a prefix, we return the top-K most relevant query completions. But I want to clarify a few things:
> - **Are we doing prefix matching or fuzzy matching?** Like if the user types 'amzon', do we correct to 'amazon'?"

**Interviewer:** "Good question. Let's focus on **prefix matching** only. If the user types 'amaz', we return queries that start with 'amaz'. No spell correction for now."

> "- **How many suggestions should we return per keystroke?**"

**Interviewer:** "Top **10** suggestions."

> "- **Should suggestions be personalized?** Like showing different results for different users based on their search history?"

**Interviewer:** "Good instinct. Let's treat personalization as a **stretch goal** — design the system with global popularity first, then tell me how you'd layer personalization on top."

> "- **Do we need to support trending queries?** Like 'Prime Day deals' spiking on the day of the event?"

**Interviewer:** "Yes. Trending queries should surface within **15-30 minutes** of spiking."

> "- **Content filtering — do we need to filter offensive or inappropriate suggestions?**"

**Interviewer:** "Absolutely. That's a hard requirement. An offensive suggestion in the Amazon search bar is a brand-damaging incident."

> "- **Multi-language support?**"

**Interviewer:** "Mention it but don't deep-dive. Focus on English."

**Non-Functional Requirements:**

> "Now the critical part — the performance and scale requirements:
>
> | Dimension | My Proposal |
> |---|---|
> | **Latency** | < 100ms p99 end-to-end. Users perceive anything over 100ms as laggy. Google's research shows that even 200ms of added latency reduces search engagement. |
> | **Availability** | 99.99% — the search bar is one of the highest-traffic surfaces on Amazon. If autocomplete is down, search still works, but the experience degrades significantly. |
> | **Scalability** | Billions of search queries per day. Every keystroke triggers a suggestion request (with debouncing). This is read-heavy at massive scale. |
> | **Freshness** | Trending queries within 15-30 min. General query popularity updated hourly. |
> | **Fault Tolerance** | Graceful degradation — if the suggestion backend is down, show cached results or an empty dropdown. Search itself should never be blocked by autocomplete. |

**Interviewer:**
I like that you called out the latency budget explicitly. 100ms is aggressive — walk me through how you'll hit that.

**Candidate:**
> "That 100ms budget includes everything: the user's keystroke, debounce delay, network round-trip, server processing, and rendering. The debounce alone eats 100-200ms, so the actual server response needs to be **well under 50ms**. That's going to drive some key design decisions — particularly around precomputation and caching. I'll come back to this."

**Interviewer:**
Good. Let's get some numbers.

---

## PHASE 3: Scale Estimation (~3 min)

**Candidate:**

> "Let me estimate scale for an Amazon-grade autocomplete system."

#### Traffic Estimates

> "Assumptions:
> - **Total searches per day**: ~5 billion (Amazon + subsidiaries)
> - **Average query length**: ~4 words, ~20 characters
> - **Keystrokes per query triggering a suggestion request**: ~10 (with 150ms debounce, not every keystroke fires)
> - **Total suggestion requests/day**: 5B × 10 = **50 billion/day**
> - **Requests per second**: 50B / 86,400 ≈ **580,000 req/sec**
> - **Peak (3x)**: **~1.7 million req/sec**"

#### Storage Estimates

> "The key insight here: I'm storing **queries**, not documents. Queries are short strings.
>
> - **Unique queries**: ~500 million (after dedup and normalization — lowercase, trim whitespace)
> - **Average query size**: ~20 bytes
> - **Per-query metadata** (frequency, timestamp, category): ~80 bytes
> - **Total per query**: ~100 bytes
> - **Raw query data**: 500M × 100B = **50 GB**"

#### Bandwidth

> "Read bandwidth: 1.7M req/sec × 1 KB response (10 suggestions × ~100 bytes each) = **1.7 GB/sec** outbound at peak.
>
> This is manageable with horizontal scaling across multiple servers."

**Interviewer:**
Good numbers. Those will be important as you iterate on your design. Let's start with a simple approach and evolve it.

### Interviewer's Internal Assessment:

✅ *Strong scale estimation. The 580K req/sec number and 50 GB raw data size are realistic. Good for L5.*
📝 *I'll push the candidate to start simple and iterate — that's how I calibrate depth.*

---

## PHASE 4: Architecture — Iterative Design (~15 min)

**Interviewer:**
Let's start simple. Give me the most basic system that works, and then we'll find the problems and fix them.

### Iteration 1: The Simplest Thing That Works — SQL Database

**Candidate:**

> "Alright, let me start with the most straightforward approach. I have 500M queries with frequencies stored in a database. When a user types a prefix, I query the database."

```
┌─────────────┐         ┌──────────────┐         ┌──────────────────┐
│   User      │         │  Web Server  │         │  SQL Database    │
│  (Browser)  │────────▶│  (API)       │────────▶│  (MySQL/Postgres)│
│             │         │              │         │                  │
│  types      │         │  GET /suggest│         │  SELECT query    │
│  "amaz"     │         │  ?prefix=    │         │  FROM suggestions│
│             │◀────────│  amaz        │◀────────│  WHERE query     │
│  [results]  │         │              │         │  LIKE 'amaz%'    │
│             │         │              │         │  ORDER BY freq   │
└─────────────┘         └──────────────┘         │  DESC LIMIT 10   │
                                                  └──────────────────┘
```

```sql
-- Schema
CREATE TABLE suggestions (
    query VARCHAR(255) PRIMARY KEY,
    frequency BIGINT,
    last_seen TIMESTAMP,
    INDEX idx_query (query)   -- B-tree index for LIKE 'prefix%'
);

-- Query
SELECT query, frequency
FROM suggestions
WHERE query LIKE 'amaz%'
ORDER BY frequency DESC
LIMIT 10;
```

> "**Pros:**
> - Dead simple to implement. Single table, single query.
> - `LIKE 'prefix%'` can use a B-tree index — it's essentially a range scan.
> - Easy to update frequencies (just `UPDATE ... SET frequency = frequency + 1`).
>
> **Cons:**
> - **Latency**: Even with an index, `LIKE 'prefix%'` on 500M rows scans a potentially large range. For a short prefix like 'a', it matches 50M+ rows, then sorts by frequency. This is **100ms-1s** — way too slow.
> - **Scale**: 580K req/sec on a SQL database? A single MySQL instance handles ~10K simple queries/sec. We'd need 60+ read replicas.
> - **No precomputation**: Every request recomputes the ranking from scratch. Wasteful when the data changes at most hourly."

**Interviewer:**
Right. The SQL approach works for a prototype but not at Amazon scale. What's the core bottleneck?

**Candidate:**
> "The bottleneck is that `LIKE 'prefix%' ORDER BY frequency DESC LIMIT 10` requires two things: (1) finding all matching rows (range scan), and (2) sorting them by frequency. The database can't use the B-tree index for both the prefix filter AND the frequency sort simultaneously — it has to scan all matches and sort. For popular short prefixes, that's millions of rows."

### Interviewer's Internal Assessment:

✅ *Good that the candidate started simple and identified the specific bottleneck (range scan + sort conflict). Shows systematic thinking.*

---

### Iteration 2: In-Memory Hash Map — Precomputed Prefix → Results

**Candidate:**

> "The SQL bottleneck is computing the answer at query time. What if I precompute the answers? I can build a hash map where every possible prefix maps to its top-10 results."

```
┌─────────────┐         ┌──────────────┐         ┌───────────────────┐
│   User      │         │  Web Server  │         │  In-Memory        │
│  (Browser)  │────────▶│  (API)       │────────▶│  HashMap          │
│             │         │              │         │                   │
│  types      │         │  GET /suggest│         │  "a" → [amazon    │
│  "amaz"     │         │  ?prefix=    │         │         prime,...]│
│             │◀────────│  amaz        │◀────────│  "am" → [amazon   │
│  [results]  │         │              │         │          prime,..]│
│             │         │              │         │  "ama" → [amazon  │
└─────────────┘         └──────────────┘         │          prime,..]│
                                                  │  "amaz" → [amazon│
                                                  │           prime, │
                                                  │           kindle]│
                                                  │  ...             │
                                                  │  (billions of    │
                                                  │   prefix entries)│
                                                  └───────────────────┘
```

```
// Precompute: for every query, generate all prefixes, store top-K for each
HashMap<String, List<Suggestion>> prefixMap = new HashMap<>();

for each query in sorted_by_frequency:
    for i in 1..len(query):
        prefix = query[0:i]
        if prefixMap[prefix].size() < K:
            prefixMap[prefix].add(query)

// Query time: O(1) lookup!
results = prefixMap.get("amaz")  // instant
```

> "**Pros:**
> - **O(1) query time** — just a hash map lookup. Can't get faster than this.
> - Precomputation happens offline — no impact on serving latency.
>
> **Cons:**
> - **Massive memory**: 500M unique queries × average 20 characters = 10 billion prefix entries. Each entry stores 10 suggestions. That's:
>   - 10B entries × (20 bytes key + 10 × 20 bytes values) ≈ **2 TB** of memory. Doesn't fit on any single machine.
> - **Redundancy**: The prefixes 'a', 'am', 'ama', 'amaz', 'amazon' share most of their top-10 results. We're storing the same suggestions billions of times.
> - **Update cost**: When frequencies change, we need to regenerate ALL prefix entries — expensive."

**Interviewer:**
So the hash map has the right idea — precompute — but the space is prohibitive. Is there a data structure that gives you prefix lookup without this redundancy?

**Candidate:**
> "Yes — a trie. A trie shares prefixes structurally, so 'a', 'am', 'ama', 'amaz', 'amazon' are all represented as a single path through the tree, not 5 separate hash entries."

### Interviewer's Internal Assessment:

✅ *Good progression. The candidate identified that precomputation is the key insight but the hash map has prohibitive space. The natural bridge to a trie is clean.*

---

### Iteration 3: Standard Trie — Prefix-Optimized Structure

**Candidate:**

> "A trie (prefix tree) is specifically designed for prefix lookups. Each path from root to a node represents a prefix, and shared prefixes share the same nodes."

```
┌─────────────┐         ┌──────────────┐         ┌───────────────────┐
│   User      │         │  Web Server  │         │  In-Memory Trie   │
│  (Browser)  │────────▶│  (API)       │────────▶│                   │
│             │         │              │         │       ROOT        │
│  types      │         │  GET /suggest│         │      / | \        │
│  "amaz"     │         │  ?prefix=    │         │    a   i   s      │
│             │◀────────│  amaz        │◀────────│    |   |   |      │
│  [results]  │         │              │         │    m   p   a      │
│             │         │              │         │    |   |   |      │
└─────────────┘         └──────────────┘         │    a   h   m      │
                                                  │    |   |   |      │
                                                  │    z   o   s      │
                                                  │    |   |   |      │
                                                  │    o   n   u      │
                                                  │    |   |   |      │
                                                  │    n   e   n      │
                                                  │    ★       |      │
                                                  │    |       g      │
                                                  │    ...     ★      │
                                                  │                   │
                                                  │   ★ = terminal    │
                                                  └───────────────────┘
```

> "**How the query works:**
>
> 1. Start at ROOT
> 2. Follow edge 'a' → node for 'a'
> 3. Follow edge 'm' → node for 'am'
> 4. Follow edge 'a' → node for 'ama'
> 5. Follow edge 'z' → node for 'amaz'
> 6. Now I'm at the node for prefix 'amaz' — I need the top-10 queries in this subtree
> 7. **DFS/BFS the entire subtree** rooted at 'amaz', collect all terminal nodes, sort by frequency, return top 10

> "**Pros:**
> - Prefix lookup is O(L) where L = prefix length — fast!
> - Shared prefixes = shared nodes. No redundancy like the hash map.
> - Space is proportional to total characters across all queries, not all prefix combinations.
>
> **Cons:**
> - **Subtree traversal at query time**: After finding the prefix node, I still need to traverse the entire subtree to find and rank all matches. For prefix 'a', the subtree contains millions of queries. This traversal + sort is **O(subtree_size × log(subtree_size))** — could be hundreds of milliseconds for popular prefixes.
> - **Space waste**: Each node has 26+ child pointers (one per character). Most nodes have only 1-2 children, so 90% of pointers are null. With 10B nodes × 26 pointers × 8 bytes = **2 TB**. Same problem as before!
> - We haven't actually solved the query-time performance problem. We just moved it from the database to an in-memory tree traversal."

**Interviewer:**
Two problems then: space waste and query-time subtree traversal. Let's address them one at a time. What about the space problem first?

### Interviewer's Internal Assessment:

✅ *The candidate correctly identified BOTH problems with the standard trie — not just the obvious space issue, but also the subtree traversal bottleneck. This shows understanding that just using a trie doesn't solve everything.*

---

### Iteration 4: Compressed Trie (Radix Tree) — Fix the Space Problem

**Candidate:**

> "The space problem comes from single-child chains. In the word 'amazon', the path a→m→a→z→o→n is 6 nodes where each has exactly one child. A **compressed trie (radix tree)** merges these chains into a single node with a multi-character edge label."

```
   STANDARD TRIE                              COMPRESSED TRIE (Radix Tree)
   (wasteful chains)                          (chains merged into edges)

       ROOT                                        ROOT
      / | \                                       / | \
    a   i   s                                "am"  "ip"  "sam"
    |   |   |                                / \     |      |
    m   p   a                          "azon" "erican" "hone" "sung"
    |   |   |                            |       |       |       |
    a   h   m                        [amazon]★ [american]★ [iphone]★ [samsung]★
    |   |   |                            |
    z   o   s                        " prime"
    |   |   |                            |
    o   n   u                      [amazon prime]★
    |   |   |
    n   e   n                      ★ = terminal node
    |       |
  [amazon]★ g
    |       |
  ' '    [samsung]★
    |
    p
    |
    ...

  Nodes: ~10 billion                        Nodes: ~50-100 million
  Space: ~2 TB                              Space: ~5 GB
```

> "**How compression works:**
> - Identify chains of nodes where each node has exactly one child
> - Merge the chain into a single edge with a multi-character label
> - Result: 10B nodes → ~50M nodes (only branching points and terminals remain)
>
> **Space savings:**
> - Standard trie: ~2 TB (10B nodes × 26 pointers × 8 bytes)
> - Compressed trie: ~5 GB (50M nodes × ~100 bytes per node)
> - **~400x reduction** — now fits entirely in memory on a single server!"

```
CompressedTrieNode {
    children: Map<char, CompressedTrieNode>  // first char of edge → child
    edge_label: string                        // multi-character label, e.g., "azon"
    is_terminal: bool                         // represents a complete query?
    frequency: long                           // query frequency (if terminal)
}
```

> "**Pros:**
> - **Fits in memory** (~5 GB). This is the breakthrough — we can serve from RAM.
> - Same O(L) prefix lookup time.
> - Fewer nodes = fewer pointer chases = better cache locality.
>
> **Cons:**
> - **Still need subtree traversal for top-K!** After finding the prefix node, I still traverse the subtree to find and rank all matching queries. For short prefixes, this is still slow.
> - Insert/delete requires edge splitting/merging — more complex than standard trie.
>
> We solved the space problem, but the query-time problem remains."

**Interviewer:**
Good. So now the trie fits in memory — that's a major win. But the query-time problem is still there. For the prefix 'a', you'd traverse millions of nodes. How do you fix that?

### Interviewer's Internal Assessment:

✅ *Clean progression. The candidate solved the space problem (compression) but correctly identified that the query-time problem persists. Doesn't claim the problem is fully solved.*

---

### Iteration 5: Compressed Trie + Precomputed Top-K — The Final Design

**Candidate:**

> "The insight is: **if the data changes at most every hour, why recompute the top-K on every query?** I should precompute the top-K suggestions at every node during trie construction, not at query time."

```
                              ┌──────────────────────────────────────────────────────────┐
                              │                        ROOT                              │
                              │  top_k: [amazon prime, iphone 15, samsung galaxy, ...]   │
                              └──────────────┬─────────────────────┬──────────────────────┘
                                             │                     │
                                       edge: "am"            edge: "ip"
                                             │                     │
                              ┌──────────────▼───────┐    ┌────────▼──────────────┐
                              │  Node: "am"          │    │  Node: "ip"           │
                              │  top_k: [amazon      │    │  top_k: [iphone 15,   │
                              │    prime, amazon      │    │    ipad, iphone case]  │
                              │    kindle, american   │    └───────┬──────┬────────┘
                              │    express]           │           │      │
                              └────┬─────────┬───────┘     edge:"hone" edge:"ad"
                                   │         │                │         │
                             edge:"azon" edge:"erican"        │         │
                                   │         │         ┌──────▼───┐ ┌───▼──────┐
                         ┌─────────▼───┐ ┌───▼──────┐  │"iphone"  │ │"ipad"    │
                         │ "amazon"     │ │"american"│  │ top_k:   │ │ top_k:   │
                         │ top_k:      │ │ top_k:   │  │[iphone   │ │[ipad,    │
                         │[amazon prime│ │[american  │  │ 15, ...]│  │ipad air] │
                         │ amazon      │ │ express,  │  └─────────┘ └──────────┘
                         │ kindle,...] │ │ airlines] │
                         └──┬─────────┘ └──────────┘
                            │
                      edge:" prime"
                            │
                  ┌─────────▼──────────┐
                  │ "amazon prime"      │
                  │ top_k: [amazon     │
                  │  prime, amazon     │
                  │  prime video, ...]  │
                  │ is_terminal: true   │
                  │ frequency: 5000000  │
                  └────────────────────┘
```

> "**How it works:**
>
> At **build time** (offline, hourly):
> 1. Build the compressed trie from sorted queries
> 2. Bottom-up pass: at each node, merge top-K lists from all children, keep top K=10
> 3. Every node now has its answer precomputed
>
> At **query time**:
> 1. Traverse the trie to the prefix node — O(L)
> 2. Return `node.top_k` — O(1). **No subtree traversal!**"

```
CompressedTrieNode {
    children: Map<char, CompressedTrieNode>
    edge_label: string
    is_terminal: bool
    frequency: long
    top_k: List<ScoredSuggestion>   // ← NEW: precomputed top-K for this subtree
}

ScoredSuggestion {
    query: string       // "amazon prime"
    score: float        // 0.95
}
```

> "Let me trace a query for 'amaz':"

```
search("amaz"):

1. Start at ROOT
2. Look for child edge starting with 'a' → Found edge "am"
3. Consume "am" from prefix → remaining: "az"
4. At Node "am", look for child edge starting with 'a' → Found edge "azon"
5. Consume "az" from "azon" — partial match through the edge
6. Return node.top_k: ["amazon prime", "amazon kindle", "amazon music", ...]

Total: 2 node hops. Microseconds.
```

**Interviewer:**
What's the cost of this precomputation?

**Candidate:**
> "Let me calculate:
>
> - **Build-time cost**: O(n × K) bottom-up pass where n = 50M nodes, K = 10
> - **Space cost**: 50M nodes × 10 suggestions × 4 bytes (index into string table) = **2 GB** extra on top of the 5 GB trie = **~7 GB total**
> - **Query-time cost**: O(L) trie traversal + O(1) top-K read = **microseconds**"

#### Evolution Summary Table

| Iteration | Approach | Query Time | Space | Update Cost | Verdict |
|---|---|---|---|---|---|
| **1. SQL DB** | `LIKE 'prefix%' ORDER BY freq` | O(n log n) — 100ms-1s | 50 GB (disk) | Easy (`UPDATE`) | ❌ Too slow at scale |
| **2. Hash Map** | Prefix → top-K lookup | O(1) | **~2 TB** (all prefix combos) | Rebuild all entries | ❌ Space is prohibitive |
| **3. Standard Trie** | Traverse to prefix, DFS subtree | O(L + subtree) — ms for short prefixes | **~2 TB** (sparse pointers) | Easy (insert/delete) | ❌ Both slow and huge |
| **4. Compressed Trie** | Traverse to prefix, DFS subtree | O(L + subtree) — same problem | **~5 GB** ✅ | Edge split/merge | ⚠️ Space solved, query still slow |
| **5. Compressed Trie + Top-K** | Traverse to prefix, read top_k | **O(L) — microseconds** ✅ | **~7 GB** ✅ | Requires rebuild | ✅ **The answer** |

> "Each iteration solved a specific problem:
> - Iteration 2 solved the recomputation problem (precompute!) but exploded space
> - Iteration 3 introduced the right data structure but didn't solve either problem
> - Iteration 4 solved space with compression
> - Iteration 5 solved query time by precomputing top-K at each node
>
> The tradeoff for Iteration 5 is that we can't update the trie in-place — we need to rebuild it. But since we're rebuilding hourly anyway (for freshness), this is free."

**Interviewer:**
Excellent walkthrough. I like that you showed the evolution. Now tell me about the overall system architecture — you've been focused on the data structure. What does the full system look like?

### Interviewer's Internal Assessment:

✅ *Outstanding iterative approach. The candidate started simple, identified specific bottlenecks, and evolved the design through 5 iterations. Each iteration solved exactly one problem.*
✅ *The summary table is clean and shows the tradeoff analysis at each step.*
⭐ *For L5, this exceeds expectations. The iterative thinking is what I expect from strong L5s growing toward L6.*

---

## PHASE 5: Full System Architecture — Read & Write Paths (~5 min)

**Candidate:**

> "Now that we have the data structure, let me sketch the full system. The key design principle is **separation of read and write paths**."

#### Read Path (Hot Path)

```
                            ┌─────────────┐
                            │   User      │
                            │  (Browser)  │
                            └──────┬──────┘
                                   │ keystroke → debounce (150ms)
                                   │
                            ┌──────▼──────┐
                            │    CDN      │
                            │ (CloudFront)│
                            │  TTL: 5 min │
                            └──────┬──────┘
                                   │ cache miss
                                   │
                            ┌──────▼──────┐
                            │ API Gateway │
                            │ (rate limit,│
                            │  auth)      │
                            └──────┬──────┘
                                   │
                            ┌──────▼──────┐
                            │ Autocomplete│
                            │  Service    │
                            │ (stateless) │
                            └──────┬──────┘
                                   │
                      ┌────────────┼────────────┐
                      │            │            │
               ┌──────▼──────┐ ┌──▼──┐  ┌──────▼──────┐
               │ Redis Cache │ │     │  │ Trie Server │
               │  (L3 cache) │ │     │  │ (in-memory  │
               │ TTL: 10 min │ │     │  │  trie)      │
               └─────────────┘ │     │  └─────────────┘
                               │     │
                        ┌──────▼──────┐
                        │Personalization│
                        │  Service     │
                        │ (user history│
                        │  in Redis)   │
                        └──────────────┘
```

#### Write Path (Offline Data Pipeline)

```
┌──────────────┐     ┌─────────┐     ┌──────────────┐     ┌──────────────┐
│ Search Query │     │  Kafka  │     │   S3 / HDFS  │     │ Spark (Batch)│
│   Logs       │────▶│ (ingest)│────▶│  (raw logs)  │────▶│  Aggregation │
└──────────────┘     └─────────┘     └──────────────┘     └──────┬───────┘
                                                                  │
                          ┌───────────────────────────────────────┘
                          │
                    ┌─────▼──────┐     ┌──────────────┐     ┌──────────────┐
                    │  Content   │     │ Trie Builder │     │ Trie Servers │
                    │  Filter    │────▶│  (build +    │────▶│ (blue-green  │
                    │ (blocklist)│     │  serialize)  │     │  deploy)     │
                    └────────────┘     └──────────────┘     └──────────────┘

                          ┌─────────┐     ┌─────────────┐
                          │  Kafka  │     │ Flink       │
     (real-time path) ───▶│ (stream)│────▶│ (5-min      │──▶ Trending Overlay
                          │         │     │  windows)   │    (Redis)
                          └─────────┘     └─────────────┘
```

> "Two completely separate paths:
>
> 1. **Read path**: User keystroke → debounce → CDN (cache check) → API Gateway → Autocomplete Service → Redis cache check → Trie Server (in-memory lookup) → return top-K. Target: **< 50ms** server-side.
>
> 2. **Write path**: Query logs flow into Kafka → stored in S3 → Spark aggregates frequencies hourly → content filter removes offensive queries → Trie Builder constructs a new compressed trie with precomputed top-K → deployed to Trie Servers via blue-green deployment. For trending: a parallel Flink streaming pipeline detects spikes and updates a trending overlay in Redis."

**Interviewer:**
Why separate read and write paths? Why not update the trie in real-time as queries come in?

**Candidate:**
> "Three reasons:
>
> 1. **Concurrency**: Updating the trie in-place while serving reads creates race conditions. The precomputed top-K at every node means a single insert could cascade changes up to the root.
>
> 2. **Quality control**: The offline pipeline lets us apply content filtering, deduplication, and normalization before building the trie. In a real-time system, an offensive query could briefly appear in suggestions before being caught.
>
> 3. **Simplicity**: The trie is a read-only, immutable data structure at serving time. No locks, no concurrent modification, no GC pressure from constant allocation. Just fast reads.
>
> The tradeoff is freshness — but the 15-30 minute trending overlay handles the most time-sensitive case."

**Interviewer:**
Good reasoning. Let's talk about how you get data into the trie.

### Interviewer's Internal Assessment:

✅ *Clean architecture. The CQRS-like separation makes sense for a system where the data changes slowly but reads are at massive scale.*
📝 *For L5, this is strong. The three justifications for read/write separation show depth.*

---

## PHASE 6: Data Collection & Aggregation Pipeline (~8 min)

**Interviewer:**
Walk me through the pipeline. How does raw user activity become a trie?

**Candidate:**

> "The data pipeline has three stages: **collection**, **aggregation**, and **building**."

#### Stage 1: Query Log Collection

> "Every time a user completes a search (presses Enter or clicks a suggestion), we log:
>
> ```
> {
>     query: 'amazon prime video',
>     timestamp: 1707350400000,
>     user_id: 'sha256:abc123...',     // hashed for privacy
>     device: 'MOBILE',
>     region: 'us-east-1',
>     clicked_suggestion: true,
>     suggestion_position: 2
> }
> ```
>
> These events go into a **Kafka topic** (`search-queries`), partitioned by `hash(user_id)` for ordering guarantees per user.
>
> Volume: 5B queries/day ≈ 58K events/sec on Kafka. Each event is ~200 bytes, so ~1 TB/day of raw log data."

**Interviewer:**
Do you log every intermediate keystroke too?

**Candidate:**
> "No — that would be 50B events/day and mostly noise. I only log **completed searches** (100%) and **sample 10% of abandoned queries** (where the user typed but didn't search). The abandoned queries are useful signals — if many users type 'iphone 15 pro max' but don't complete the search, maybe our search results for that query are poor."

#### Stage 2: Aggregation (Batch + Streaming)

> "I use a dual pipeline:
>
> **Batch (Spark)** — runs hourly:
> - Reads last 30 days of query logs from S3
> - Normalizes: lowercase, trim whitespace, collapse multiple spaces
> - Groups by normalized query text
> - Computes time-decayed frequency score:
>
>   `score(q) = Σ (count_per_day × e^(-0.1 × age_in_days))`
>
>   This gives recent queries higher weight. Half-life ≈ 7 days.
>
> - Output: sorted list of (query, score) — typically 500M rows → Parquet on S3"

```
                        BATCH PIPELINE (hourly)
  ┌───────┐     ┌───────┐     ┌───────────┐     ┌──────────┐     ┌──────────┐
  │  S3   │────▶│ Spark │────▶│ Normalize │────▶│ GroupBy  │────▶│ Time-    │
  │ (raw  │     │  Job  │     │ + Dedup   │     │  Query   │     │ Decay    │
  │ logs) │     │       │     │           │     │ + Count  │     │ Scoring  │
  └───────┘     └───────┘     └───────────┘     └──────────┘     └────┬─────┘
                                                                      │
                                                               ┌──────▼──────┐
                                                               │ Scored Query│
                                                               │ List (S3)   │
                                                               └─────────────┘
```

> "**Streaming (Flink)** — for trending detection:
> - Reads from Kafka in real-time
> - 5-minute tumbling windows
> - Counts queries per window
> - Compares against historical average (same time-of-day, last 7 days)
> - If `current_count / historical_avg > 5x` → mark as **trending**
> - Output: trending queries list (~1000-5000 queries) written to Redis"

**Interviewer:**
How do you combine the batch and streaming outputs?

**Candidate:**
> "The batch pipeline produces the **base trie** — rebuilt every hour with stable, high-quality frequency data.
>
> The streaming pipeline produces a **trending overlay** — a small set of queries that just started spiking. These get injected into a separate Redis hash that the autocomplete service checks at query time.
>
> At serving time: fetch top-K from the trie (batch), then merge in any trending queries that match the prefix (streaming overlay). Trending queries get a score boost so they appear near the top."

#### Stage 3: Trie Building

> "The trie builder is a standalone service that:
>
> 1. Reads the scored query list from S3 (batch output)
> 2. Reads the blocklist from the content filter service
> 3. Filters out blocked queries
> 4. Sorts queries alphabetically
> 5. Builds the compressed trie by iterating the sorted list
> 6. Bottom-up pass: propagate top-K from leaves to root
> 7. Serializes the trie to a binary format
> 8. Uploads to S3
> 9. Trie servers pull the new version and load into memory
> 10. Health check → traffic switch (blue-green deployment)
>
> Total pipeline time: Spark aggregation ~20 min + trie build ~15 min + deploy ~5 min = **~40 min**. On an hourly schedule, that leaves 20 min of buffer."

**Interviewer:**
What if the trie build produces a bad output — say, an empty trie or one with offensive content that slipped through?

**Candidate:**
> "Good concern. I'd add safety checks:
>
> 1. **Size sanity check**: if the new trie is <50% or >200% the size of the current trie, abort and alert.
> 2. **Canary deployment**: deploy the new trie to 5% of servers first, monitor suggestion click-through rate (CTR) for 10 minutes. If CTR drops >20%, auto-rollback.
> 3. **Blocklist verification**: sample 1000 random suggestions from the new trie and verify none match the blocklist.
> 4. **Version pinning**: keep the last 5 trie versions in S3. One-click rollback."

### Interviewer's Internal Assessment:

✅ *Solid pipeline design. The dual-pipeline (batch + streaming) approach is the right answer.*
✅ *Good operational awareness — the safety checks for trie deployment show production-minded thinking.*
📝 *For L5, this is at the bar — an L6 would have proactively discussed exactly-once semantics in the streaming pipeline or discussed how to handle late-arriving events.*

---

## PHASE 7: Ranking & Relevance (~5 min)

**Interviewer:**
You mentioned frequency and time-decay for ranking. Is that sufficient? What about a query like "iphone 14" versus "iphone 15" — both might have similar historical frequency, but one is clearly more relevant today.

**Candidate:**

> "You're right — raw frequency with time-decay handles the recency problem somewhat (the 7-day half-life downweights 'iphone 14'), but it's not enough for all cases. Let me expand the ranking model."

#### Multi-Signal Scoring

> "I'd combine multiple signals:
>
> ```
> score(query) = w1 × frequency_decayed
>              + w2 × trending_boost
>              + w3 × conversion_rate
>              + w4 × freshness
> ```
>
> | Signal | Weight (w) | Description |
> |---|---|---|
> | **Frequency (time-decayed)** | 0.5 | How often this query is searched, with exponential decay |
> | **Trending boost** | 0.2 | Spike detection: is this query surging right now? |
> | **Conversion rate** | 0.2 | Do users who search this actually buy something? (Amazon-specific) |
> | **Freshness** | 0.1 | When was this query first seen? Newer queries get a small boost |
>
> The weights are tunable — we'd A/B test different configurations."

**Interviewer:**
What about personalization? You mentioned it as a stretch goal.

**Candidate:**
> "Right. Personalization adds a per-user re-ranking layer on top of the global scoring."

*(Note: Interviewer had to nudge the candidate toward personalization.)*

> "At query time, the flow would be:
>
> 1. Fetch top-K from the global trie (scored by the multi-signal formula above)
> 2. Fetch the user's recent search history from Redis: last 50 searches with timestamps
> 3. Re-rank: boost suggestions that match the user's interests
>
> For example, if a user frequently searches for electronics, and the prefix 'ap' could suggest both 'apple watch' and 'apron', we'd boost 'apple watch' for that user.
>
> The personalization score could be:
> ```
> personal_boost(query, user) = α × category_match(query, user.history)
>                              + β × prefix_overlap(query, user.history)
> ```
>
> The user history is small — 50 queries × ~50 bytes = 2.5 KB per user in Redis. Even for 100M active users, that's 250 GB in a Redis cluster — feasible."

**Interviewer:**
How do you handle cold-start — a new user with no history?

**Candidate:**
> "Fall back to the global ranking — no personalization. The system degrades gracefully. As the user searches more, their profile builds up. I'd also consider using the user's **browsing category** as an initial signal — if they're on the Electronics page, boost electronics queries even without search history."

### Interviewer's Internal Assessment:

✅ *The multi-signal scoring model is good for L5. The weights table shows structured thinking.*
⚠️ *Needed a nudge on personalization — an L6 would have proactively discussed it. But once prompted, the candidate gave a solid answer including cold-start handling.*
📝 *The conversion_rate signal is a nice Amazon-specific touch. Shows product sense.*

---

## PHASE 8: Scaling & Caching — Iterative Design (~8 min)

**Interviewer:**
We're at 1.7M requests/sec at peak. Right now you have a single trie server. How do you evolve this to handle that load?

### Scaling Iteration 1: Single Trie Server + No Caching

**Candidate:**

> "Let me start with the simplest deployment and find the bottleneck."

```
┌─────────────┐         ┌──────────────┐         ┌──────────────┐
│  1.7M       │         │  API Gateway │         │ Trie Server  │
│  req/sec    │────────▶│              │────────▶│ (single, 7GB)│
│  (peak)     │         │              │         │              │
│             │◀────────│              │◀────────│ 50K req/sec  │
│             │         │              │         │ max capacity │
└─────────────┘         └──────────────┘         └──────────────┘

Problem: 1.7M req/sec ÷ 50K per server = 34 servers needed.
         Plus 3x replication = 102 servers.
         All serving the same data. Wasteful.
```

> "**Problem**: Every single request hits a trie server. Even though each lookup is microseconds, the overhead (network, serialization, connection handling) limits each server to ~50K req/sec. We'd need 34+ servers — all holding identical copies of the same 7 GB trie. That works, but we're paying for 102 servers to serve data that doesn't change for an hour. There must be a better way."

---

### Scaling Iteration 2: Add a Redis Cache Layer

**Candidate:**

> "The trie data changes at most hourly. Millions of users type the same prefixes ('a', 'am', 'ip', etc.). Cache the results."

```
┌─────────────┐         ┌──────────────┐     ┌──────────┐     ┌──────────────┐
│  1.7M       │         │  API Gateway │     │  Redis   │     │ Trie Server  │
│  req/sec    │────────▶│              │────▶│  Cache   │────▶│ (on miss)    │
│             │         │              │     │ TTL: 10m │     │              │
│             │◀────────│              │◀────│ Hit: 80% │◀────│              │
└─────────────┘         └──────────────┘     └──────────┘     └──────────────┘

Redis absorbs 80% → Trie sees 340K req/sec.
Trie servers needed: 340K ÷ 50K = 7 servers (+ replicas = ~21).
Better, but still a lot.
```

> "**Improvement**: Redis absorbs 80% of requests (common prefixes like 'a', 'am', 'ama' are cached). Trie servers now handle only 340K req/sec.
>
> **Remaining problem**: We're still sending 1.7M req/sec over the network from browsers to our backend. The network, API gateway, and Redis all need to handle this full load."

---

### Scaling Iteration 3: Add a CDN Layer

**Candidate:**

> "Most autocomplete requests are identical across users — user A and user B both typing 'amaz' should get the same non-personalized results. A CDN can serve these from edge locations close to the user."

```
┌─────────────┐     ┌──────────┐     ┌──────────┐     ┌──────────┐     ┌──────────┐
│  1.7M       │     │   CDN    │     │   API    │     │  Redis   │     │  Trie    │
│  req/sec    │────▶│CloudFront│────▶│ Gateway  │────▶│  Cache   │────▶│ Server   │
│             │     │ TTL: 5m  │     │          │     │ TTL: 10m │     │          │
│             │◀────│ Hit: 60% │◀────│          │◀────│ Hit: 80% │◀────│          │
└─────────────┘     └──────────┘     └──────────┘     └──────────┘     └──────────┘

CDN absorbs 60% (1.02M) → 680K pass through
Redis absorbs 80% of remainder → 136K hit trie
Trie servers: 136K ÷ 50K = 3 servers (+ replicas = ~9)
```

> "**Improvement**: CDN absorbs 60% of requests, Redis absorbs 80% of the remainder. Only 136K req/sec reach trie servers — that's 3 servers + replicas.
>
> **Remaining problem**: 1.7M req/sec still leave the user's browser. Can we reduce that too?"

---

### Scaling Iteration 4: Add Browser-Side Caching + Client Optimizations (Final)

**Candidate:**

> "Three client-side optimizations eliminate a huge chunk of requests before they even leave the browser:"

```
User keystroke "ama"
       │
       ▼
┌──────────────────┐
│  Browser Cache   │───── HIT (30%) ────▶ Return cached results (0ms)
│  TTL: 1 minute   │
│  Cache-Control:  │
│  max-age=60      │
└───────┬──────────┘
        │ MISS (70%)
        ▼
┌──────────────────┐
│  CDN (CloudFront)│───── HIT (60% of remaining = 42% total) ────▶ Return (~5ms)
│  TTL: 5 minutes  │
└───────┬──────────┘
        │ MISS (28% of total)
        ▼
┌──────────────────┐
│  Redis Cache     │───── HIT (80% of remaining = 22.4% total) ──▶ Return (~10ms)
│  TTL: 10 minutes │
└───────┬──────────┘
        │ MISS (5.6% of total)
        ▼
┌──────────────────┐
│  Trie Server     │───── Always hits (in-memory) ──▶ Return (~1ms) + populate Redis
│  (in-memory trie)│
└──────────────────┘
```

> "**Client optimizations:**
> 1. **Debouncing (150ms)**: Only fire a request after the user stops typing for 150ms. Reduces requests by ~50%.
> 2. **Browser cache (HTTP Cache-Control: max-age=60)**: If the user typed 'ama' in the last minute, reuse the cached result. ~30% hit rate.
> 3. **Local result filtering**: If we cached results for 'am', and the user types 'ama', filter the cached results locally instead of making a new request."

#### Scaling Evolution Summary

| Iteration | Layers | Trie Server Load | Servers Needed (with 3x replication) |
|---|---|---|---|
| **1. No cache** | Browser → Trie | 1,700K req/sec | ~102 |
| **2. + Redis** | Browser → Redis → Trie | 340K req/sec | ~21 |
| **3. + CDN** | Browser → CDN → Redis → Trie | 136K req/sec | ~9 |
| **4. + Browser cache** | Browser cache → CDN → Redis → Trie | **95K req/sec** | **~6** |

> "From 102 servers down to 6 — a **17x reduction** — just by adding caching layers. The key insight: autocomplete data changes slowly (hourly), but is queried billions of times. Every cache layer dramatically reduces backend load."

**Interviewer:**
Good progression. You mentioned the trie fits in ~7 GB. What if it grows to 70 GB?

**Candidate:**
> "Then I'd shard by prefix. Three options:
>
> | Strategy | Pros | Cons |
> |---|---|---|
> | Shard by first character (26 shards) | Simple routing | Uneven: 's' gets 10x traffic of 'x' |
> | Shard by first 2 characters (676 shards) | More even | More shards to manage |
> | Consistent hashing on prefix | Even distribution | Complex routing |
>
> I'd go with **first 2 characters** — 676 shards gives reasonable balance. But honestly, with 500M queries the trie is only 7 GB. I'd start with **full replication** (every server has the complete trie, round-robin load balancing) and only shard if we hit memory limits."

### Interviewer's Internal Assessment:

✅ *Excellent iterative scaling progression. Started with no caching, added layers one at a time, showed the quantitative impact of each.*
✅ *The reduction from 102 servers to 6 is a compelling story — demonstrates the power of caching.*
✅ *Pragmatic decision to start with full replication and shard only if needed.*
📝 *An L6 would discuss cache warming strategies, thundering herd prevention during trie deployments, and cache coherence across regions.*

---

## PHASE 9: Operational Concerns (~5 min)

**Interviewer:**
What are your biggest operational concerns?

**Candidate:**

> "Let me walk through what keeps me up at night:"

#### 1. Content Filtering Gaps

> "An offensive suggestion reaching the Amazon search bar is a PR disaster. I'd implement **defense in depth**:
>
> - **Build-time**: blocklist applied during trie construction — most effective, catches everything known
> - **Serve-time**: results filtered against a Redis-based blocklist before returning — safety net for anything that slipped through
> - **Emergency block**: admin API to instantly add terms to the serve-time blocklist without waiting for a trie rebuild
> - **ML classifier**: for new queries not on the blocklist, flag potentially offensive queries for human review before they enter the trie"

#### 2. Monitoring & Alerting

> "Key metrics I'd track:
>
> | Metric | Alert Threshold | Why |
> |---|---|---|
> | p99 latency | > 100ms | Violates SLA |
> | Cache hit rate (CDN) | < 50% | Something is wrong with caching |
> | Suggestion CTR | Drop > 20% | Trie quality regression |
> | Trie build time | > 55 min (on hourly schedule) | Risk of pipeline falling behind |
> | Content filter trigger rate | Spike > 3x | Possible attack or new offensive pattern |
> | Empty result rate | > 30% | Trie coverage problem |"

#### 3. A/B Testing

> "We'd A/B test:
> - Different ranking weight configurations
> - K values (10 vs 15 suggestions)
> - Trie rebuild frequency (hourly vs every 30 min)
> - Personalization on/off
>
> The API response includes an `experiment_id` field so we can attribute CTR to specific experiments."

#### 4. Failure Modes & Graceful Degradation

> "The autocomplete system should **never** block the search experience:
>
> | Failure | Fallback | User Impact |
> |---|---|---|
> | Trie server down | Serve from Redis cache (stale) | Slightly stale suggestions |
> | Redis down | Serve from CDN cache (staler) | More stale suggestions |
> | CDN + Redis + Trie all down | Return empty suggestions | Search bar works, no autocomplete |
> | Bad trie deployed | Automated rollback | 10-15 min of degraded quality |
>
> Circuit breaker pattern on trie server calls — if failures > 50% in a 10-second window, stop calling trie servers and fall back to cache."

#### 5. Internationalization (Brief)

> "For CJK (Chinese, Japanese, Korean), prefix-based completion is more complex — no clear word boundaries, different tokenization. We'd deploy **language-specific trie servers** with appropriate tokenizers. For now, English-only, but the architecture supports this with a `language` parameter on the API."

**Interviewer:**
Good operational awareness. One last thing — how do you handle thundering herd when a new trie deploys and all caches are invalidated?

**Candidate:**
> "Great question. I'd use **versioned cache keys**:
>
> - Cache key includes trie version: `suggestions:v42:ama`
> - When trie v43 deploys, old cache entries (`v42:*`) are simply stale — they expire via TTL
> - New requests with v43 generate new cache entries
> - No mass invalidation = no thundering herd
> - For a brief period, some users get v42 (cached) and some get v43 (fresh) — acceptable for autocomplete"

### Interviewer's Internal Assessment:

✅ *Good operational thinking — content filtering defense-in-depth, monitoring metrics with thresholds, failure modes with fallbacks.*
✅ *The thundering herd answer (versioned cache keys) is solid — the candidate handled a curveball well.*
📝 *For L5, this is at or slightly above bar. Good production instincts.*

---

## PHASE 10: Wrap-Up & Final Assessment (~3 min)

**Interviewer:**
Great discussion. Can you summarize your final design?

**Candidate:**

> "Here's the complete design summary:"

| Component | Choice | Why |
|---|---|---|
| **Data Structure** | Compressed trie (radix tree) with precomputed top-K at each node | O(L) query time, fits in ~7 GB memory. Evolved through 5 iterations from SQL → hash map → standard trie → compressed trie → +precomputed top-K. |
| **Read Path** | Browser → CDN → Redis → Trie Server | 4-layer caching: only 5.6% of requests hit the backend. Reduced servers from 102 to 6. |
| **Write Path** | Query logs → Kafka → Spark (batch) + Flink (streaming) → Trie Builder | Offline processing, no impact on read path |
| **Ranking** | Frequency (time-decayed) + trending boost + conversion rate + personalization overlay | Balances popularity, freshness, and relevance |
| **Sharding** | Full replication initially; shard by first 2 chars if trie grows beyond single-server memory | Start simple, scale when needed |
| **Freshness** | Hourly batch rebuild + 5-minute trending overlay in Redis | 15-30 min for trending, 1 hour for general updates |
| **Content Filtering** | Build-time blocklist + serve-time safety net + emergency admin API | Defense in depth against offensive suggestions |
| **Availability** | Cascading fallback: trie → Redis → CDN → empty results | Autocomplete degrades gracefully, never blocks search |

> "**What keeps me up at night:**
>
> 1. **Content filtering gaps** — an offensive suggestion reaching users is a brand-damaging incident
> 2. **Trie rebuild failures** — if the pipeline breaks, suggestions get stale (but not broken — we serve the old trie)
> 3. **Cache stampede during trie deployment** — versioned cache keys mitigate this
> 4. **Personalization data privacy** — we hash user IDs and apply retention policies, but privacy compliance is an ongoing concern"

**Interviewer:**
Good. Thanks for the thorough walkthrough. I particularly appreciated the iterative approach — starting from SQL and building up to the final trie design.

---

## Interviewer's Final Assessment

### Hire Recommendation: **Hire (L5 / SDE-2)**

| Dimension | Rating | Notes |
|---|---|---|
| **Problem Decomposition** | ✅ Meets Bar | Clean separation of read/write paths. Good functional/non-functional requirements. |
| **Iterative Design** | ⭐ Exceeds Bar | Excellent progression: SQL → hash map → standard trie → compressed trie → +top-K. Each iteration solved exactly one problem. Similarly for scaling: no cache → Redis → CDN → browser cache. |
| **Scale Estimation** | ✅ Meets Bar | Solid math: 580K req/sec, 7 GB trie, 50B suggestion requests/day. Key insight: trie fits in memory. |
| **Data Structure Design** | ⭐ Exceeds Bar | Compressed trie + precomputed top-K is the core insight. Strong space analysis. Evolution showed depth. |
| **Data Pipeline** | ✅ Meets Bar | Dual pipeline (batch + streaming) is correct. Good safety checks for deployment. |
| **Ranking & Relevance** | ✅ Meets Bar | Multi-signal scoring model is solid. Needed nudge on personalization — handled well once prompted. |
| **Scaling & Caching** | ✅ Meets Bar | Multi-layer caching with quantitative progression (102 → 6 servers). Pragmatic full-replication choice. |
| **Operational Maturity** | ✅ Meets Bar | Content filtering defense-in-depth, monitoring metrics, failure modes, versioned cache keys. |
| **Communication** | ⭐ Exceeds Bar | Structured, iterative approach with clear diagrams and summary tables at each step. |
| **LP: Dive Deep** | ✅ Meets Bar | Strong on trie internals and caching math. Could go deeper on distributed systems. |
| **LP: Think Big** | ✅ Meets Bar | Mentioned personalization, internationalization, trending. |
| **LP: Bias for Action** | ✅ Meets Bar | Made decisive choices (full replication, CQRS) with clear reasoning. |

### L5 vs L6 Comparison: What Would an L6 Do Differently?

| Aspect | L5 (This Candidate) | L6 Expectation |
|---|---|---|
| **Requirements** | Identified prefix search, top-K, latency. Needed nudge on personalization. | Would proactively drive personalization, trending, content filtering, internationalization, and privacy. |
| **Data Structure** | Iterated through 5 approaches to reach compressed trie with top-K — excellent. | Would also compare with FSTs and ternary search trees. Discuss cache-line optimization, SIMD prefix matching. |
| **Pipeline** | "Spark for batch, Flink for streaming" — correct architecture. | Would discuss exactly-once semantics, late-arriving events, pipeline idempotency, data lineage. |
| **Caching** | Iterative scaling with quantitative analysis — strong. | Would design cache warming strategy, analyze thundering herd in depth, discuss cache coherency across regions. |
| **Scaling** | "Full replication, shard if needed" — pragmatic. | Would discuss blue-green deployment mechanics, zero-downtime trie swaps, shard migration strategies. |
| **Ranking** | "Frequency + time-decay + conversion" — good baseline. | Would discuss learning-to-rank (ML models), online A/B testing frameworks, counterfactual evaluation. |
| **Operational** | Good monitoring metrics, failure modes. | Would design quality metrics pipeline, anomaly detection on suggestion quality, automated root cause analysis. |

### Summary

A strong L5 candidate who demonstrated excellent iterative design thinking. The progression from SQL → hash map → standard trie → compressed trie → precomputed top-K showed systematic problem-solving — each iteration identified a specific bottleneck and solved it. The scaling evolution (102 servers → 6 via layered caching) was equally strong. The candidate needed some guidance on personalization and didn't proactively discuss some deeper distributed systems concerns, which is expected at L5. Clear growth trajectory toward L6.

---

*This interview simulation is complemented by the following deep-dive documents:*
- *[Trie & Ranking Deep Dive](trie-and-ranking-deep-dive.md)*
- *[Data Collection Pipeline](data-collection-pipeline.md)*
- *[Scaling & Caching](scaling-and-caching.md)*
- *[System Flows](flow.md)*
- *[API Contracts](api-contracts.md)*
