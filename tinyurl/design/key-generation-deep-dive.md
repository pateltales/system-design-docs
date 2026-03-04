# Key Generation Deep Dive: The Core Algorithm of URL Shortening

> **System context**: 600M URL creations/month (~230 writes/sec, peak ~1,000), 10B redirects/month (~3,800 reads/sec, peak ~15,000). Key space: 36^8 = 2.82 trillion. Total URLs over 100 years: 720 billion. Storage: ~48TB. Database: PostgreSQL with Citus for horizontal partitioning. Chosen approach: pre-allocated keys with `FOR UPDATE SKIP LOCKED`.

---

## Table of Contents

1. [Why Key Generation is the Core Problem](#section-1-why-key-generation-is-the-core-problem)
2. [Approach 1 -- Hash the Long URL](#section-2-approach-1--hash-the-long-url)
3. [Approach 2 -- Distributed Counter (Snowflake-Style ID)](#section-3-approach-2--distributed-counter-snowflake-style-id)
4. [Approach 3 -- Pre-allocated Key Table + FOR UPDATE SKIP LOCKED (The Winner)](#section-4-approach-3--pre-allocated-key-table--for-update-skip-locked-the-winner)
5. [Evolution Summary Table](#section-5-evolution-summary-table)
6. [Concurrency Analysis with Numbers](#section-6-concurrency-analysis-with-numbers)
7. [Collision Probability Math (Birthday Problem)](#section-7-collision-probability-math-birthday-problem)
8. [Comparison with Real-World Systems](#section-8-comparison-with-real-world-systems)

---

## Section 1: Why Key Generation is the Core Problem

### The Central Insight

Every system design problem has a "core algorithm" -- the single technical challenge that, if solved well, makes everything else straightforward, and if solved poorly, turns every other decision into damage control. In URL shortening, that core algorithm is **key generation**.

Consider what happens when a user submits `https://www.example.com/very/long/path?with=parameters&and=more`:

1. We need to produce a short, unique suffix like `k7f2m9x1`
2. This suffix must be globally unique across billions of existing URLs
3. It must be generated under high concurrency (1,000 writes/sec at peak)
4. It should be unpredictable (so attackers cannot enumerate URLs)
5. It must be generated quickly (sub-10ms to not bottleneck the write path)
6. The generation mechanism must not become a single point of failure

Storage is solved by any modern database. Retrieval is a simple key-value lookup. Caching is well-understood. But generating that 8-character string -- quickly, uniquely, concurrently, and without coordination overhead -- is where the real engineering challenge lives.

### Analogy to Other Systems' Core Challenges

Every classic system design question has its own "core algorithm":

```
+-------------------------------+-------------------------------+-------------------------------------------+
| System                        | Core Algorithm                | Why It's Hard                             |
+-------------------------------+-------------------------------+-------------------------------------------+
| URL Shortener                 | Key Generation                | Global uniqueness at high concurrency     |
|                               |                               | without coordination overhead             |
+-------------------------------+-------------------------------+-------------------------------------------+
| Autocomplete / Typeahead      | Trie / Prefix Tree            | Sub-millisecond prefix matching across    |
|                               |                               | millions of suggestions with ranking      |
+-------------------------------+-------------------------------+-------------------------------------------+
| Rate Limiter                  | Token Bucket / Sliding Window | Distributed counting with atomic          |
|                               |                               | increment-and-check semantics             |
+-------------------------------+-------------------------------+-------------------------------------------+
| Chat / Messaging              | Message Ordering (Vector      | Causal ordering across distributed nodes  |
|                               | Clocks / Lamport Timestamps)  | without global synchronization            |
+-------------------------------+-------------------------------+-------------------------------------------+
| Web Crawler                   | URL Frontier / Politeness     | Prioritized crawling with per-domain      |
|                               |                               | rate limiting and deduplication           |
+-------------------------------+-------------------------------+-------------------------------------------+
| Notification System           | Fan-out Strategy              | Delivering to millions of subscribers     |
|                               |                               | with bounded latency                      |
+-------------------------------+-------------------------------+-------------------------------------------+
| Distributed Cache             | Consistent Hashing            | Minimizing redistribution when nodes      |
|                               |                               | join or leave                             |
+-------------------------------+-------------------------------+-------------------------------------------+
```

Just as you cannot design a performant autocomplete system without deeply understanding tries, you cannot design a production-grade URL shortener without deeply understanding key generation strategies, their failure modes, and their scalability limits.

### What Makes Key Generation Uniquely Challenging

The difficulty arises from the intersection of several constraints:

```
                     Uniqueness
                        /\
                       /  \
                      /    \
                     /      \
                    / SWEET  \
                   /  SPOT    \
                  /____________\
         Speed /                \ Unpredictability
              /                  \
             /                    \
```

- **Uniqueness**: No two URLs can share a suffix. With 720 billion URLs over 100 years, collisions are not a theoretical concern -- they are a certainty if not handled correctly.
- **Speed**: At 1,000 writes/sec peak, key generation must complete in single-digit milliseconds. Any blocking or serialization collapses throughput.
- **Unpredictability**: Sequential IDs let attackers scrape every URL by incrementing. For a service that may shorten private or sensitive URLs, this is unacceptable.
- **Simplicity**: Additional services (ID generators, Zookeeper coordination) increase operational complexity, failure modes, and deployment burden.

The "sweet spot" is an approach that satisfies all four simultaneously. Let us evaluate three canonical approaches.

---

## Section 2: Approach 1 -- Hash the Long URL

### Architecture

```
                                                    ┌──────────────────────┐
                                                    │   Database (PG)      │
                                                    │                      │
Client ──► LB ──► App Server ──► Hash(longURL)      │  Check if suffix     │
                       │         │                  │  exists:             │
                       │         ▼                  │                      │
                       │    Take first 8 chars      │  ├─ NOT EXISTS       │
                       │    of hex output            │  │  → INSERT         │
                       │         │                  │  │  → Return short   │
                       │         ▼                  │  │    URL             │
                       │    Convert to base-36       │  │                   │
                       │         │                  │  └─ EXISTS           │
                       │         ▼                  │     (collision!)     │
                       └────────►│──────────────────│     → Append counter │
                                 │                  │     → Re-hash        │
                                 │                  │     → Retry          │
                                 │                  └──────────────────────┘
                                 ▼
                          Return short URL
```

### Algorithm Detail

**Step 1**: Compute the hash of the long URL.

```
Input:  "https://www.example.com/very/long/path?with=parameters"
MD5:    "5d41402abc4b2a76b9719d911017c592"   (32 hex chars = 128 bits)
SHA-256: "2cf24dba5fb0a30e26e83b2ac5b9e29e..."  (64 hex chars = 256 bits)
```

**Step 2**: Take the first 8 characters of the hex output.

```
MD5 first 8 hex chars:    "5d41402a"
SHA-256 first 8 hex chars: "2cf24dba"
```

**Step 3**: Convert from hexadecimal (base-16) to base-36.

```
"5d41402a" (hex) = 1,564,180,522 (decimal) → base-36 encoding → "q3k7m2" (6 chars)
```

Wait -- 8 hex characters give us only 32 bits of entropy (4 bits per hex char x 8 = 32 bits), which means a key space of 2^32 = ~4.3 billion. That is far too small for our 720 billion URL requirement. To get 8 base-36 characters (key space = 36^8 = 2.82 trillion), we need:

```
log2(2.82 trillion) = ~41.4 bits of entropy

To get 41.4 bits from hex characters: ceil(41.4 / 4) = 11 hex characters needed
```

So we actually need to take the first **11 hex characters** of the hash output, interpret them as an integer, and then encode in base-36 to get an 8-character suffix. The math:

```
11 hex chars = 44 bits of entropy = 2^44 = 17.6 trillion possible values
Modulo 36^8 (2.82 trillion) → maps into our 8-char base-36 space
```

Alternatively, we can take the raw binary output (MD5 = 16 bytes, SHA-256 = 32 bytes), interpret the first 6 bytes as a 48-bit integer, and encode directly in base-36:

```
6 bytes = 48 bits = 2^48 = 281 trillion possible values
281 trillion mod 36^8 (2.82 trillion) → 8-char base-36 suffix
```

### Collision Probability: The Birthday Problem

The birthday problem tells us: given n items placed into m buckets uniformly at random, the probability that at least two items land in the same bucket is:

```
P(at least one collision) = 1 - e^(-n^2 / (2 * m))

Where:
  n = number of URLs created
  m = key space size = 36^8 = 2,821,109,907,456 (2.82 trillion)
```

The expected number of collisions (for large m) is approximately:

```
E(collisions) ~ n^2 / (2 * m)
```

Let us compute this at various scales:

| URLs Created (n) | n^2 / (2m) | P(collision) | Expected Collisions | Practical Impact |
|---|---|---|---|---|
| 1 million (10^6) | 1.77 x 10^-7 | 0.0000177% | ~0.000000177 | No impact whatsoever |
| 10 million (10^7) | 1.77 x 10^-5 | 0.00177% | ~0.0000177 | No impact |
| 100 million (10^8) | 1.77 x 10^-3 | 0.177% | ~0.00177 | Negligible |
| 1 billion (10^9) | 0.177 | 16.2% | ~0.177 | Might see first collision |
| 10 billion (10^10) | 17.7 | ~100% (1 - e^-17.7) | ~17.7 | Collisions on ~18 out of every 10B inserts |
| 100 billion (10^11) | 1,773 | ~100% | ~1,773 | Collisions are routine |
| 720 billion (7.2 x 10^11) | 91,907 | ~100% | ~91,907 | Collision on nearly every insert |

**Critical threshold**: At ~1 billion URLs, you start seeing your first collisions. At 10 billion (our ~1.5 year mark), collisions happen on roughly 18 out of every batch of 10 billion inserts. At 720 billion (our 100-year projection), approximately 92,000 collisions occur, meaning your retry logic fires tens of thousands of times.

### Collision Resolution Strategies

When a hash collision occurs (the generated suffix already exists in the database), you need a resolution strategy:

**Strategy A: Append Counter and Re-hash**

```python
def generate_key(long_url):
    for attempt in range(MAX_RETRIES):   # typically 5-10
        if attempt == 0:
            candidate = hash_and_encode(long_url)
        else:
            candidate = hash_and_encode(long_url + str(attempt))

        if not db.exists(candidate):
            db.insert(candidate, long_url)
            return candidate

    raise KeyGenerationError("Too many collisions")
```

Problem: Each retry requires a DB round-trip to check existence. At 2ms per query, 5 retries = 10ms of added latency. Under high concurrency, multiple threads might simultaneously check and find the same slot empty, leading to INSERT conflicts (requiring another retry).

**Strategy B: Double Hashing**

```
h1 = SHA256(longURL)[:8]  → first candidate
h2 = MD5(longURL)[:8]     → step size

Probe sequence: h1, h1+h2, h1+2*h2, h1+3*h2, ...
```

Problem: Requires careful modular arithmetic in base-36 space. Doesn't fundamentally solve the DB round-trip issue.

**Strategy C: Linear Probing with Database**

```sql
-- Try to INSERT; on conflict, let DB find next available
INSERT INTO url_mappings (suffix, long_url)
VALUES (:candidate, :longUrl)
ON CONFLICT (suffix)
DO NOTHING
RETURNING *;

-- If RETURNING gives NULL, collision occurred; application retries with new candidate
```

Problem: "Next available" in a hash table requires maintaining a separate data structure; you cannot easily do linear probing within a database without custom logic.

### Pros and Cons Summary

```
PROS:                                      CONS:
+ Deterministic: same URL → same key       - Collision handling adds latency
+ No pre-computation needed                - Extra DB lookups per collision
+ Simple to understand                     - Retry logic adds code complexity
+ Stateless (no counter to maintain)       - Same URL = same key (may NOT
                                             be desired: different users may
                                             want different short URLs for
                                             the same long URL)
                                           - Hash computation is CPU-bound
                                             (SHA-256 at ~500MB/s is fine for
                                             URLs, but adds CPU load)
```

### Real-World Usage

**Bitly** originally used a hash-based approach in its early days (circa 2008-2010). As their URL volume grew into the billions, the collision rate became operationally burdensome:
- Retry storms during traffic spikes
- Increased p99 latency due to multi-retry paths
- Monitoring complexity (tracking collision rates, retry counts)

They eventually migrated to a counter-based approach with base-62 encoding.

### Verdict

**Not viable at scale**. The birthday problem is unforgiving: collision probability grows quadratically with the number of URLs. At our scale (720 billion URLs over 100 years), hash-based generation degrades into a system where collision handling logic dominates the write path. The deterministic property (same URL = same hash) is a double-edged sword -- it prevents the same user from creating multiple short URLs for the same destination, which is often a product requirement.

---

## Section 3: Approach 2 -- Distributed Counter (Snowflake-Style ID)

### Architecture

```
                                        ┌─────────────────────────────────────┐
                                        │   ID Generator Service (Snowflake)  │
                                        │                                     │
Client ──► LB ──► App Server ──────────►│   ┌─────────────────────────────┐   │
                       │                │   │ 64-bit ID:                  │   │
                       │                │   │ [timestamp 41b][machine 10b]│   │
                       │                │   │ [sequence 12b][unused 1b]   │   │
                       │                │   └─────────────────────────────┘   │
                       │                └─────────────┬───────────────────────┘
                       │                              │
                       │                              ▼
                       │                   Base-36 encode the 64-bit ID
                       │                              │
                       │                              ▼
                       │                   Take last 8 chars (or pad)
                       │                              │
                       ▼                              ▼
                  PostgreSQL ◄──────────── INSERT (suffix, long_url)
                       │
                       ▼
                 Return short URL
```

### Snowflake ID Format (64 bits)

```
┌──────────────────────────────────────────────────────────────────┐
│ 0 │       41 bits: timestamp (ms)      │ 10 bits │  12 bits     │
│   │   (milliseconds since epoch)       │ machine │  sequence    │
│   │   = ~69.7 years of unique time     │   ID    │  counter     │
│   │                                    │ (1024   │  (4096 IDs   │
│   │                                    │ machines│  per ms per  │
│   │                                    │  max)   │  machine)    │
└──────────────────────────────────────────────────────────────────┘
Bit: 63                                   22     12             0
```

**Capacity per machine**: 4,096 IDs per millisecond = 4,096,000 IDs per second.
**Capacity with 1,024 machines**: 4,096,000 x 1,024 = ~4.2 billion IDs per second.
**Time range**: 2^41 milliseconds = ~69.7 years from the custom epoch.

This vastly exceeds our peak requirement of 1,000 writes/sec.

### Base-36 Encoding of 64-bit IDs

A 64-bit unsigned integer has a maximum value of 2^64 - 1 = 18,446,744,073,709,551,615.

In base-36:

```
log36(2^64) = 64 * log36(2) = 64 * (log10(2) / log10(36)) = 64 * (0.301 / 1.556) = ~12.4
```

So a 64-bit ID encodes to **up to 13 base-36 characters**. We only want 8 characters for our short URL suffix.

**Option A: Take the last 8 characters** (modulo 36^8):

```
snowflake_id = 1738920345123456789
base36_full  = "gk7f2m9x1p3b2"  (13 chars)
base36_last8 = "x1p3b2gk"       (8 chars, effectively snowflake_id mod 36^8)
```

This maps 2^64 values into 36^8 = 2.82 trillion buckets. Since 2^64 / 36^8 ~ 6,538, approximately 6,538 different Snowflake IDs map to the same 8-char suffix. **Collisions are possible** (though rare, since IDs are sequential and spread across the space).

**Option B: Use base-62 encoding** (a-z, A-Z, 0-9) for shorter URLs:

```
log62(2^64) = 64 * (log10(2) / log10(62)) = 64 * (0.301 / 1.792) = ~10.7
```

A 64-bit ID encodes to **up to 11 base-62 characters**. Taking the last 7 gives 62^7 = ~3.5 trillion key space.

### The Predictability Problem

Snowflake IDs are **roughly sequential** because the most significant bits are a timestamp. This creates a serious security vulnerability:

```
Scenario: Attacker creates two short URLs 10 seconds apart

Short URL 1: tinyurl.com/k7f2m9x1  (created at T)
Short URL 2: tinyurl.com/k7f2m9x8  (created at T + 10s)

Observation: suffixes differ by only 7 in the last character

Attack: enumerate k7f2m9x2, k7f2m9x3, ..., k7f2m9x7
         to discover all URLs created between T and T+10s
```

In practice, the enumeration attack is even simpler:

```
Step 1: Create a short URL. Note the suffix. Decode from base-36 to integer.
Step 2: Wait 1 minute. Create another short URL. Decode.
Step 3: The difference tells you how many URLs were created in that minute.
Step 4: Enumerate ALL integer values between the two to discover every URL
        created in that time window.

If 200 URLs were created per minute:
  - An attacker only needs to try ~200 values per minute of time
  - Scanning an entire day: 200 * 60 * 24 = 288,000 attempts
  - At 100 requests/sec: just 48 minutes to scrape every URL from a full day
```

This is catastrophic for privacy. If the service shortens internal company documents, private health records, or confidential business URLs, sequential IDs enable mass discovery.

### Predictability Mitigations

**Mitigation A: XOR with a Secret Key**

```python
SECRET = 0xDEADBEEFCAFE1234  # 64-bit secret

def obfuscate(snowflake_id):
    return snowflake_id ^ SECRET

def deobfuscate(obfuscated_id):
    return obfuscated_id ^ SECRET
```

Problem: XOR is trivially reversible. An attacker who discovers two consecutive IDs can XOR them and recover the pattern. XOR does not change the **distance** between consecutive IDs -- if ID_n and ID_{n+1} differ by 1, their XOR-obfuscated versions differ by `(n^S) XOR ((n+1)^S)`, which still leaks sequential structure.

**Mitigation B: Feistel Cipher (Format-Preserving Encryption)**

A Feistel cipher transforms a 64-bit input into a 64-bit output that is:
- Indistinguishable from random to anyone without the key
- Bijective (every input maps to a unique output, so no collisions)
- Reversible (needed if you ever need to recover the original Snowflake ID)

```python
import struct
import hashlib

def feistel_encrypt(value, key, rounds=4):
    """Format-preserving encryption using a balanced Feistel network."""
    left = (value >> 32) & 0xFFFFFFFF
    right = value & 0xFFFFFFFF

    for i in range(rounds):
        round_key = hashlib.sha256(
            struct.pack('>II', key, i) + struct.pack('>I', right)
        ).digest()
        f = struct.unpack('>I', round_key[:4])[0]
        left, right = right, left ^ f

    return (left << 32) | right
```

This is the most theoretically sound approach, but it adds computational overhead (~10 microseconds per encryption on modern CPUs -- negligible) and code complexity.

**Mitigation C: Random Bit Shuffle (Permutation)**

Pre-compute a fixed permutation of the 64 bit positions:

```
Original:   bit positions [63, 62, 61, ..., 2, 1, 0]
Shuffled:   bit positions [17, 42, 3, 58, ..., 29, 7, 51]

Apply this permutation to every Snowflake ID before base-36 encoding.
```

Problem: This is security through obscurity. An attacker who collects enough (input, output) pairs can reverse-engineer the permutation. However, it may be "good enough" for a URL shortener where the threat model is casual enumeration rather than determined cryptanalysis.

### The Service Dependency Issue

Snowflake requires a **running ID generation service**:

```
                    ┌──────────────────────────────┐
                    │      ID Generator Cluster     │
                    │                                │
                    │  ┌─────────┐  ┌─────────┐     │
                    │  │ Node 1  │  │ Node 2  │     │
                    │  │ (ID 001)│  │ (ID 002)│     │
                    │  └─────────┘  └─────────┘     │
                    │  ┌─────────┐  ┌─────────┐     │
                    │  │ Node 3  │  │ Node 4  │     │
                    │  │ (ID 003)│  │ (ID 004)│     │
                    │  └─────────┘  └─────────┘     │
                    └──────────────────────────────┘
                                 ▲
                                 │ gRPC / REST
                                 │
                    ┌────────────┴────────────┐
                    │     App Server Pool     │
                    │   (requests IDs from    │
                    │    generator cluster)   │
                    └─────────────────────────┘
```

**Failure modes:**
- If the ID generator cluster goes down, the entire write path halts
- Network partitions between app servers and the ID generator cause timeouts
- Clock skew between generator nodes can produce duplicate timestamps (Snowflake handles this with sequence counters, but clock sync is an operational burden)
- You need Zookeeper or etcd to coordinate machine IDs (ensuring no two generators have the same machine_id)

**Operational burden:**
- Deploy, monitor, and maintain a separate service
- Handle failover and leader election for machine ID assignment
- Monitor clock drift (NTP sync must be tight)
- Capacity plan the generator cluster separately from the application

### Real-World Usage

**Twitter** developed Snowflake specifically for tweet ID generation, where:
- Sequential ordering is actually desired (for timeline pagination)
- Predictability is not a concern (tweets are public)
- Massive scale required (~500M tweets/day in 2023)

**Twitter's t.co** URL shortener uses Snowflake-derived IDs, which makes sense in Twitter's context because:
- They already operate Snowflake infrastructure
- Most shortened URLs link to public tweets (privacy less critical)
- The marginal cost of adding t.co to existing Snowflake is near zero

For a **standalone** URL shortener, the calculus is different -- you are taking on the operational cost of a distributed ID generator purely for key generation.

### Verdict

**Viable but suboptimal for our use case**. Snowflake-style IDs solve the uniqueness problem definitively (zero collisions ever) and provide excellent throughput. However, they introduce:
1. A new service dependency (the ID generator cluster)
2. A predictability problem requiring cryptographic mitigation
3. Operational complexity (clock sync, machine ID coordination)

If you already run Snowflake infrastructure (like Twitter), this is a natural fit. For a greenfield URL shortener, there is a simpler approach that provides the same guarantees without the overhead.

---

## Section 4: Approach 3 -- Pre-allocated Key Table + FOR UPDATE SKIP LOCKED (The Winner)

### Architecture

```
                                                    ┌─────────────────────────────────────────────────┐
                                                    │              PostgreSQL                          │
Client ──► LB ──► App Server ──────────────────────►│                                                 │
                                                    │   ┌─────────────────────────────────────────┐   │
                                                    │   │  WITH candidate AS (                     │   │
                                                    │   │    SELECT * FROM url_mappings            │   │
                                                    │   │    WHERE expiry_time < NOW()             │   │
                                                    │   │    LIMIT 1                               │   │
                                                    │   │    FOR UPDATE SKIP LOCKED                │   │
                                                    │   │  )                                       │   │
                                                    │   │  UPDATE url_mappings                     │   │
                                                    │   │  SET long_url = :longUrl,                │   │
                                                    │   │      expiry_time = NOW() + INTERVAL      │   │
                                                    │   │                   '1 year',              │   │
                                                    │   │      user_id = :userId,                  │   │
                                                    │   │      created_at = NOW()                  │   │
                                                    │   │  FROM candidate                          │   │
                                                    │   │  WHERE url_mappings.id = candidate.id    │   │
                                                    │   │  RETURNING *;                            │   │
                                                    │   └─────────────────────────────────────────┘   │
                                                    │                                                 │
                                                    │   Result: One row claimed atomically.            │
                                                    │   The suffix column of that row IS the short     │
                                                    │   URL key.                                       │
                                                    └─────────────────────────────────────────────────┘
                                                                        │
                                                                        ▼
                                                            Return short URL:
                                                            tinyurl.com/{suffix}
```

### The Core Idea

Instead of generating a key at write time, **pre-generate all keys** and store them in the database as rows with `expiry_time` set to a past date (marking them as "available"). When a user creates a short URL, we simply **claim** the next available row by updating its `long_url` and `expiry_time`. The suffix column was populated during pre-generation and never changes.

```
Table: url_mappings

BEFORE claiming (row is "available"):
┌────────┬──────────┬──────────────┬──────────────────────────┬─────────┬────────────┐
│   id   │  suffix  │   long_url   │       expiry_time        │ user_id │ created_at │
├────────┼──────────┼──────────────┼──────────────────────────┼─────────┼────────────┤
│ 100042 │ k7f2m9x1 │ NULL         │ 1970-01-01 00:00:00      │ NULL    │ NULL       │
└────────┴──────────┴──────────────┴──────────────────────────┴─────────┴────────────┘

AFTER claiming:
┌────────┬──────────┬──────────────────────────────────┬──────────────────────────┬──────────┬──────────────────────────┐
│   id   │  suffix  │            long_url              │       expiry_time        │ user_id  │       created_at         │
├────────┼──────────┼──────────────────────────────────┼──────────────────────────┼──────────┼──────────────────────────┤
│ 100042 │ k7f2m9x1 │ https://www.example.com/long/url │ 2027-02-09 12:00:00      │ user_789 │ 2026-02-09 12:00:00      │
└────────┴──────────┴──────────────────────────────────┴──────────────────────────┴──────────┴──────────────────────────┘

AFTER expiry (row becomes "available" again):
┌────────┬──────────┬──────────────────────────────────┬──────────────────────────┬──────────┬──────────────────────────┐
│   id   │  suffix  │            long_url              │       expiry_time        │ user_id  │       created_at         │
├────────┼──────────┼──────────────────────────────────┼──────────────────────────┼──────────┼──────────────────────────┤
│ 100042 │ k7f2m9x1 │ https://www.example.com/long/url │ 2027-02-09 12:00:00      │ user_789 │ 2026-02-09 12:00:00      │
│        │          │ (stale, will be overwritten)      │ (in the past!)           │          │                          │
└────────┴──────────┴──────────────────────────────────┴──────────────────────────┴──────────┴──────────────────────────┘
```

### 4.1 FOR UPDATE SKIP LOCKED -- Deep Explanation

#### What `FOR UPDATE` Does

`FOR UPDATE` is a row-level locking clause in SQL `SELECT` statements. When a transaction executes:

```sql
SELECT * FROM url_mappings WHERE expiry_time < NOW() LIMIT 1 FOR UPDATE;
```

PostgreSQL:
1. Finds a row matching the `WHERE` clause
2. Acquires an **exclusive row-level lock** on that row
3. Returns the row to the calling transaction
4. **Holds the lock** until the transaction commits or rolls back

Any other transaction that tries to `SELECT ... FOR UPDATE` the **same row** will **block** (wait) until the first transaction releases its lock.

```
Timeline WITHOUT SKIP LOCKED (just FOR UPDATE):

T=0ms   Request A: SELECT ... FOR UPDATE → locks Row 1001 → proceeds
T=1ms   Request B: SELECT ... FOR UPDATE → tries Row 1001 → BLOCKED (waiting for A)
T=2ms   Request C: SELECT ... FOR UPDATE → tries Row 1001 → BLOCKED (waiting for A)
T=3ms   Request D: SELECT ... FOR UPDATE → tries Row 1001 → BLOCKED (waiting for A)
T=5ms   Request A: COMMIT → releases lock on Row 1001
T=5ms   Request B: UNBLOCKED → acquires lock on Row 1001 → but Row 1001 is now
                    claimed! → WHERE clause no longer matches → gets NEXT row (1002)
...and so on, serialized.

Result: Requests effectively serialize. Throughput = 1 / (transaction_time).
At 5ms per transaction: max 200 writes/sec. Not enough for our 1,000/sec peak.
```

#### What `SKIP LOCKED` Changes

`SKIP LOCKED` modifies the behavior: instead of **waiting** for a locked row, the query **silently skips** it and moves to the next matching row:

```
Timeline WITH FOR UPDATE SKIP LOCKED:

T=0ms   Request A: SELECT ... FOR UPDATE SKIP LOCKED → locks Row 1001 → proceeds
T=0ms   Request B: SELECT ... FOR UPDATE SKIP LOCKED → Row 1001 locked, SKIP
                    → locks Row 1002 → proceeds
T=0ms   Request C: SELECT ... FOR UPDATE SKIP LOCKED → Rows 1001-1002 locked, SKIP
                    → locks Row 1003 → proceeds
T=0ms   Request D: SELECT ... FOR UPDATE SKIP LOCKED → Rows 1001-1003 locked, SKIP
                    → locks Row 1004 → proceeds
T=0ms   Request E: SELECT ... FOR UPDATE SKIP LOCKED → Rows 1001-1004 locked, SKIP
                    → locks Row 1005 → proceeds
T=5ms   All requests: COMMIT → all locks released simultaneously

Result: All 5 requests execute in PARALLEL. Zero waiting. Zero contention.
```

#### Visualizing Concurrent Requests

```
Available Key Pool (rows where expiry_time < NOW()):
┌──────┬──────┬──────┬──────┬──────┬──────┬──────┬──────┬──────┬──────┐
│ 1001 │ 1002 │ 1003 │ 1004 │ 1005 │ 1006 │ 1007 │ 1008 │ 1009 │ 1010 │ ...
└──────┴──────┴──────┴──────┴──────┴──────┴──────┴──────┴──────┴──────┘

5 concurrent requests arrive at T=0ms:

Request A ──────► locks Row 1001 ──► UPDATE ──► COMMIT ──► returns "a8k3p2m7"
Request B ──────────► locks Row 1002 ──► UPDATE ──► COMMIT ──► returns "f9j1n4x6"
Request C ──────────────► locks Row 1003 ──► UPDATE ──► COMMIT ──► returns "b2w5r8q3"
Request D ──────────────────► locks Row 1004 ──► UPDATE ──► COMMIT ──► returns "h6t9c1y4"
Request E ──────────────────────► locks Row 1005 ──► UPDATE ──► COMMIT ──► returns "m3v7e2k8"

After all 5 complete:
┌──────┬──────┬──────┬──────┬──────┬──────┬──────┬──────┬──────┬──────┐
│ 1001 │ 1002 │ 1003 │ 1004 │ 1005 │ 1006 │ 1007 │ 1008 │ 1009 │ 1010 │ ...
│TAKEN │TAKEN │TAKEN │TAKEN │TAKEN │ free │ free │ free │ free │ free │
└──────┴──────┴──────┴──────┴──────┴──────┴──────┴──────┴──────┴──────┘
```

#### PostgreSQL Internals: How SKIP LOCKED Works with MVCC

PostgreSQL uses **Multi-Version Concurrency Control (MVCC)**, where each row can have multiple versions visible to different transactions. `SKIP LOCKED` interacts with MVCC as follows:

1. **Lock discovery**: When PostgreSQL encounters a row during an index scan, it checks the row's lock status in the `pg_locks` system catalog (actually via in-memory lock tables). This check is an O(1) operation -- it does not require scanning a lock table.

2. **Skip decision**: If the row has an exclusive lock held by another transaction, PostgreSQL immediately moves to the next row in the index scan. No waiting, no retry, no notification.

3. **Index utilization**: The query planner uses the **partial index** on `expiry_time` to efficiently find candidate rows:

```sql
-- This partial index ONLY indexes rows where expiry_time is in the past
-- (available rows). It's much smaller than a full index.
CREATE INDEX idx_available_keys ON url_mappings (expiry_time)
WHERE expiry_time < NOW();

-- Actually, since NOW() changes, we use a functional approach:
-- The planner will use a regular B-tree index on expiry_time
-- and scan from the left (earliest expiry) looking for rows < NOW()
CREATE INDEX idx_expiry ON url_mappings (expiry_time);
```

4. **Lock duration**: The exclusive row lock is held **only** for the duration of the transaction. Since our CTE + UPDATE is a single statement executed in an implicit transaction (with autocommit) or an explicit short transaction, the lock duration is ~2-5ms.

5. **No phantom reads**: Because `FOR UPDATE` locks at the tuple (row) level, and `SKIP LOCKED` skips locked tuples, there is no risk of two transactions claiming the same row. The lock is acquired **before** the row is returned to the CTE, ensuring mutual exclusion.

#### Database Support Matrix

| Database | Syntax | Available Since | Notes |
|---|---|---|---|
| **PostgreSQL** | `FOR UPDATE SKIP LOCKED` | 9.5 (2016) | Full support, well-optimized |
| **MySQL (InnoDB)** | `FOR UPDATE SKIP LOCKED` | 8.0 (2018) | Works with InnoDB only |
| **Oracle** | `FOR UPDATE SKIP LOCKED` | 10g (2003) | Earliest implementation |
| **SQL Server** | `WITH (UPDLOCK, READPAST)` | 2005 | `READPAST` hint is equivalent |
| **MariaDB** | `FOR UPDATE SKIP LOCKED` | 10.6 (2021) | Later than MySQL |
| **CockroachDB** | `FOR UPDATE SKIP LOCKED` | 23.1 (2023) | Recent addition |
| **SQLite** | Not supported | N/A | No row-level locking |

### 4.2 Pre-population Strategy

#### Why Not Pre-populate All 2.82 Trillion Keys?

```
2.82 trillion rows x 74 bytes per row = ~209 TB

That's:
- ~209 TB of SSD storage just for the key table
- Months of insert time
- Impractical index sizes
- Far beyond what any single PostgreSQL instance can handle

We don't need all possible keys. We need ENOUGH keys for our operational window.
```

#### Working Set Calculation

```
Monthly URL creation rate:  600 million / month
Keys needed for 1 year:    600M x 12 = 7.2 billion
Keys needed for 1.5 years: 600M x 18 = 10.8 billion

Pre-populate: 10 billion keys (buffer for ~1.4 years without recycling)

With recycling (URLs expire after 1 year):
- After Year 1: ~7.2B keys expire and become available again
- Net consumption rate: ~0 (creation rate ~ expiry rate at steady state)
- The 10B pre-populated keys are effectively permanent
```

#### Pre-population Process

**Step 1: Generate 10 billion random 8-character base-36 strings**

```python
import random
import string

BASE36_CHARS = string.digits + string.ascii_lowercase  # '0123456789abcdefghijklmnopqrstuvwxyz'

def generate_random_suffix():
    return ''.join(random.choices(BASE36_CHARS, k=8))

# Generate in batches of 1 million, write to CSV files
# 10,000 CSV files x 1,000,000 rows each = 10 billion rows
```

**Probability of duplicate generation**: With 10 billion randomly generated strings in a space of 2.82 trillion, the birthday problem gives:

```
P(at least one duplicate) = 1 - e^(-(10^10)^2 / (2 * 2.82 * 10^12))
                          = 1 - e^(-10^20 / (5.64 * 10^12))
                          = 1 - e^(-17,730,496)
                          ≈ 1 (essentially certain)

Expected number of duplicates ≈ n^2 / (2m) = 10^20 / (5.64 * 10^12) ≈ 17.7 million
```

So among 10 billion randomly generated suffixes, we expect ~17.7 million duplicates (~0.18%). This is handled gracefully by the UNIQUE constraint during insertion.

**Step 2: Bulk insert using PostgreSQL COPY**

```sql
-- Create the table first
CREATE TABLE url_mappings (
    id          BIGSERIAL PRIMARY KEY,
    suffix      CHAR(8) NOT NULL,
    long_url    TEXT,
    expiry_time TIMESTAMPTZ NOT NULL DEFAULT '1970-01-01 00:00:00+00',
    user_id     BIGINT,
    created_at  TIMESTAMPTZ
);

-- Temporarily drop indexes for faster bulk loading
-- (We will create them after the load)

-- Use COPY for maximum insert throughput
COPY url_mappings (suffix) FROM '/path/to/batch_0001.csv';
COPY url_mappings (suffix) FROM '/path/to/batch_0002.csv';
-- ... repeat for all 10,000 files
```

**Performance estimates**:

```
PostgreSQL COPY throughput: ~100,000 - 500,000 rows/sec (depending on hardware)
Conservative estimate: 100,000 rows/sec

10,000,000,000 rows / 100,000 rows/sec = 100,000 seconds = ~27.8 hours

With parallelism (4 COPY streams to different partitions):
100,000 seconds / 4 = 25,000 seconds = ~6.9 hours
```

**Step 3: Add UNIQUE constraint and indexes AFTER bulk insert**

```sql
-- This is MUCH faster than inserting with the constraint active
-- Building an index on 10B rows: ~2-4 hours depending on hardware

ALTER TABLE url_mappings ADD CONSTRAINT uq_suffix UNIQUE (suffix);
-- Any duplicates from random generation will cause this to fail.
-- Solution: first deduplicate.

-- Better approach: insert with ON CONFLICT to skip duplicates
-- during COPY, or deduplicate the CSV files beforehand.

-- Practical approach: Generate 10.2B keys, insert into a staging table
-- with UNIQUE constraint using ON CONFLICT DO NOTHING, expect ~10B unique.

CREATE INDEX idx_expiry ON url_mappings (expiry_time)
    WHERE long_url IS NULL OR expiry_time < NOW();
```

**Step 4: Storage calculation**

```
Row size estimation:
  id (BIGINT):          8 bytes
  suffix (CHAR(8)):     8 bytes (+ 1 byte overhead)
  long_url (TEXT):      NULL (0 bytes + 1 byte null bitmap)
  expiry_time (TSTZ):   8 bytes
  user_id (BIGINT):     NULL (0 bytes + 1 byte null bitmap)
  created_at (TSTZ):    NULL (0 bytes + 1 byte null bitmap)
  Row header:           23 bytes (HeapTupleHeader)
  Alignment padding:    ~4 bytes
  Item pointer:         4 bytes
  ─────────────────────────────────
  Total per row:        ~58 bytes (empty/available row)

  After claiming (long_url populated, avg 100 chars):
  Total per row:        ~160 bytes

Initial storage (10B rows, all available):
  10,000,000,000 x 58 bytes = ~580 GB

  Plus indexes:
  - Primary key (id): ~80 GB
  - UNIQUE (suffix): ~80 GB
  - Partial index (expiry_time): ~80 GB (only indexes available rows)
  ─────────────────────────────────
  Total initial: ~820 GB

After all rows are claimed (long_url populated):
  10,000,000,000 x 160 bytes = ~1.6 TB (data)
  Plus indexes: ~300 GB
  Total: ~1.9 TB
```

This fits comfortably on a single large PostgreSQL instance (or a small Citus cluster with 2-4 shards).

### 4.3 Background Refill Mechanism

As the system operates for years and potentially grows beyond the initial 10 billion keys, new keys must be generated and added to the pool.

#### Monitoring Available Keys

```sql
-- FAST estimate (uses table statistics, no sequential scan)
-- Returns in < 1ms regardless of table size
SELECT reltuples AS approximate_row_count
FROM pg_class
WHERE relname = 'url_mappings';

-- More accurate but slower: count available rows
-- Use a partial index scan; still fast if index is maintained
SELECT COUNT(*)
FROM url_mappings
WHERE expiry_time < NOW();
-- With a partial index, this is an index-only scan: fast even for billions of rows.

-- BEST approach: maintain a materialized counter
-- A separate table or application-level counter tracking:
--   total_available = (pre-populated but unclaimed) + (expired and available for recycling)
```

#### Refill Trigger and Process

```
Monitoring Flow:

  ┌─────────────────────────────────────────────────────────┐
  │              Background Refill Worker                    │
  │  (runs every hour as a cron job or pg_cron task)        │
  │                                                          │
  │  1. Check available key count (pg_class.reltuples        │
  │     filtered estimate or materialized counter)           │
  │                                                          │
  │  2. Decision tree:                                       │
  │     ├── available > 2 billion  → DO NOTHING              │
  │     ├── 1B < available <= 2B   → LOG warning             │
  │     ├── 500M < available <= 1B → GENERATE 500M new keys  │
  │     ├── 100M < available <= 500M → GENERATE 1B new keys  │
  │     └── available <= 100M      → ALERT on-call + GENERATE│
  │                                    2B new keys urgently   │
  │                                                          │
  │  3. Generation process:                                  │
  │     a. Generate random base-36 strings in memory         │
  │     b. Write to temp CSV files (100M per file)           │
  │     c. COPY into url_mappings with staging table:        │
  │                                                          │
  │        INSERT INTO url_mappings (suffix, expiry_time)    │
  │        SELECT suffix, '1970-01-01'::timestamptz          │
  │        FROM staging_new_keys                             │
  │        ON CONFLICT (suffix) DO NOTHING;                  │
  │                                                          │
  │     d. Drop staging table                                │
  │     e. Log: "Refilled X new keys. Available: Y total."   │
  └─────────────────────────────────────────────────────────┘
```

#### Refill Performance

```
Generating 500M random strings:
  Python: ~500M x 1 microsecond = ~500 seconds (~8 minutes)
  Rust:   ~500M x 50 nanoseconds = ~25 seconds

COPY into staging table: 500M / 100K per sec = ~5,000 sec (~83 min)
With parallel COPY: ~21 minutes

INSERT ... ON CONFLICT DO NOTHING from staging to main table:
  ~500M / 50K per sec (due to unique index checks) = ~10,000 sec (~2.8 hours)
  With batch sizes of 10K and parallel workers: ~45 minutes

Total refill time for 500M keys: ~1-3 hours (depends on hardware and parallelism)
Frequency: roughly every 25 days (if consuming 600M/month and not yet recycling)
```

#### Alerting Thresholds

```
┌─────────────────────────┬───────────┬──────────────────────────────────────────────┐
│ Available Keys          │ Severity  │ Action                                       │
├─────────────────────────┼───────────┼──────────────────────────────────────────────┤
│ > 2 billion             │ OK        │ No action needed                             │
│ 1B - 2B                 │ INFO      │ Log for capacity planning                    │
│ 500M - 1B               │ WARNING   │ Trigger refill job; Slack notification        │
│ 100M - 500M             │ CRITICAL  │ Urgent refill; page on-call engineer          │
│ < 100M                  │ EMERGENCY │ Page on-call + management; emergency refill   │
│ < 10M                   │ FATAL     │ System at risk of running out within hours;   │
│                         │           │ consider rate-limiting new URL creation        │
└─────────────────────────┴───────────┴──────────────────────────────────────────────┘

Time-to-exhaustion at different available key levels:
  100M keys / 230 writes/sec (avg) = 434,783 seconds = ~5 days
  10M keys  / 230 writes/sec (avg) = 43,478 seconds  = ~12 hours
  1M keys   / 230 writes/sec (avg) = 4,348 seconds   = ~72 minutes
```

### 4.4 Key Recycling

#### How Recycling Works

The beauty of this design is that key recycling is **automatic and built into the claim query**:

```sql
WITH candidate AS (
    SELECT * FROM url_mappings
    WHERE expiry_time < NOW()       -- Finds BOTH:
                                    --   1. Never-claimed rows (expiry = epoch)
                                    --   2. Expired rows (expiry in the past)
    LIMIT 1
    FOR UPDATE SKIP LOCKED
)
UPDATE url_mappings
SET long_url = :newLongUrl,
    expiry_time = NOW() + INTERVAL '1 year',
    user_id = :newUserId,
    created_at = NOW()
FROM candidate
WHERE url_mappings.id = candidate.id
RETURNING *;
```

There is no separate "reclaim" or "garbage collection" process. Expired rows naturally become eligible for the next `SELECT ... WHERE expiry_time < NOW()` query. The row is simply **overwritten** with new data.

#### Lifecycle of a Key

```
Phase 1: Pre-populated (available)
┌──────────┬──────────┬──────────┬──────────────────────┬─────────┬────────────┐
│ id       │ suffix   │ long_url │ expiry_time          │ user_id │ created_at │
│ 5000042  │ k7f2m9x1 │ NULL     │ 1970-01-01 00:00:00  │ NULL    │ NULL       │
└──────────┴──────────┴──────────┴──────────────────────┴─────────┴────────────┘
                      ↓ (User A creates short URL on 2026-03-15)

Phase 2: Claimed (active)
┌──────────┬──────────┬───────────────────────────┬──────────────────────┬──────────┬──────────────────────┐
│ 5000042  │ k7f2m9x1 │ https://example.com/pageA │ 2027-03-15 00:00:00  │ user_101 │ 2026-03-15 10:00:00  │
└──────────┴──────────┴───────────────────────────┴──────────────────────┴──────────┴──────────────────────┘
                      ↓ (Time passes... 2027-03-15 arrives. URL expires.)

Phase 3: Expired (available for recycling)
┌──────────┬──────────┬───────────────────────────┬──────────────────────┬──────────┬──────────────────────┐
│ 5000042  │ k7f2m9x1 │ https://example.com/pageA │ 2027-03-15 00:00:00  │ user_101 │ 2026-03-15 10:00:00  │
│          │          │ (stale, about to be       │ (IN THE PAST!)       │          │                      │
│          │          │  overwritten)             │                      │          │                      │
└──────────┴──────────┴───────────────────────────┴──────────────────────┴──────────┴──────────────────────┘
                      ↓ (User B creates short URL on 2027-04-20)

Phase 4: Reclaimed (active again, different URL)
┌──────────┬──────────┬───────────────────────────┬──────────────────────┬──────────┬──────────────────────┐
│ 5000042  │ k7f2m9x1 │ https://other.com/pageB   │ 2028-04-20 00:00:00  │ user_555 │ 2027-04-20 14:30:00  │
└──────────┴──────────┴───────────────────────────┴──────────────────────┴──────────┴──────────────────────┘

The suffix "k7f2m9x1" is REUSED. The row's id never changes. Only the content changes.
```

#### Cool-Down Period

A user who bookmarked `tinyurl.com/k7f2m9x1` might try to visit it after the URL has expired but before they realize it is gone. If we immediately reclaim the suffix, the user would be redirected to an entirely different URL -- a confusing and potentially harmful experience.

**Solution**: Add a cool-down period to the claim query:

```sql
WITH candidate AS (
    SELECT * FROM url_mappings
    WHERE expiry_time < NOW() - INTERVAL '7 days'   -- Must be expired for 7+ days
    LIMIT 1
    FOR UPDATE SKIP LOCKED
)
...
```

This ensures a suffix is not reused for at least 7 days after expiry. During those 7 days, requests to the short URL would return a "This link has expired" page rather than silently redirecting to a different URL.

**Impact on capacity planning**:

```
Without cool-down: Keys become available immediately upon expiry
With 7-day cool-down: Keys become available 7 days after expiry

At 600M URLs/month with 1-year expiry:
  - URLs expiring per day: 600M / 365 = ~1.64M per day
  - In cool-down at any time: 1.64M * 7 = ~11.5M keys in cool-down
  - This is negligible compared to the ~10B available key pool
```

#### Long-Term Steady State

```
Year 1:
  - Start: 10B available keys
  - Created: ~7.2B URLs
  - Expired: 0 (nothing is 1 year old yet)
  - Available at end of Year 1: 10B - 7.2B = ~2.8B

Year 2:
  - Start: 2.8B available + 7.2B expiring throughout the year
  - Created: ~7.2B URLs
  - Expired: ~7.2B (Year 1's URLs expire)
  - Net change: ~0
  - Available at end of Year 2: ~2.8B (steady state!)

Year 3+:
  - Creation rate ≈ Expiration rate
  - Available keys remain stable at ~2.8B
  - No refill needed unless growth rate increases

If growth rate increases (e.g., doubles to 1.2B/month):
  - Year 1 at new rate: creates 14.4B, but only 7.2B expire → deficit of 7.2B
  - Available keys: 2.8B - 7.2B = -4.4B → NEED REFILL
  - Refill trigger fires when available < 1B → well before exhaustion
  - Generate additional keys to handle the increased rate
```

### 4.5 Why This Approach Wins

Let us systematically compare this approach against every concern a system designer faces:

```
┌──────────────────────────────────┬──────────────────────────────────────────────────────────┐
│ Concern                          │ How Pre-allocated + SKIP LOCKED Addresses It             │
├──────────────────────────────────┼──────────────────────────────────────────────────────────┤
│ Collision risk                   │ ZERO. Suffixes are pre-generated with a UNIQUE           │
│                                  │ constraint. Collisions are impossible by construction.    │
├──────────────────────────────────┼──────────────────────────────────────────────────────────┤
│ Separate services needed         │ NONE. The database IS the key generation service.         │
│                                  │ No Snowflake, no Zookeeper, no Redis, no separate KGS.   │
├──────────────────────────────────┼──────────────────────────────────────────────────────────┤
│ Coordination overhead            │ ZERO. SKIP LOCKED eliminates contention entirely.         │
│                                  │ N concurrent requests each get their own row.             │
├──────────────────────────────────┼──────────────────────────────────────────────────────────┤
│ Write path complexity            │ SINGLE atomic operation. One CTE + UPDATE statement.      │
│                                  │ No retry logic, no conditional branching, no multi-step.  │
├──────────────────────────────────┼──────────────────────────────────────────────────────────┤
│ Key recycling                    │ AUTOMATIC. Expired rows match the WHERE clause and are    │
│                                  │ claimed on the next write. No background GC process.      │
├──────────────────────────────────┼──────────────────────────────────────────────────────────┤
│ Predictability / security        │ UNPREDICTABLE. Suffixes are random strings generated      │
│                                  │ during pre-population. No sequential pattern.             │
├──────────────────────────────────┼──────────────────────────────────────────────────────────┤
│ Operational complexity           │ MINIMAL. One table, standard PostgreSQL. No exotic        │
│                                  │ infrastructure. Any DBA can understand and maintain it.    │
├──────────────────────────────────┼──────────────────────────────────────────────────────────┤
│ Latency                          │ ~2-5ms per write (single index lookup + row update).      │
│                                  │ Consistent regardless of concurrency level.               │
├──────────────────────────────────┼──────────────────────────────────────────────────────────┤
│ Horizontal scalability           │ PostgreSQL with Citus: shard by suffix. Each shard        │
│                                  │ independently handles SKIP LOCKED. Linear scale-out.      │
├──────────────────────────────────┼──────────────────────────────────────────────────────────┤
│ Failure mode                     │ If PostgreSQL is down, the entire write path is down      │
│                                  │ (same as any approach using PostgreSQL for storage).       │
│                                  │ No ADDITIONAL failure modes from key generation.           │
└──────────────────────────────────┴──────────────────────────────────────────────────────────┘
```

**In one sentence**: Pre-allocated keys with `FOR UPDATE SKIP LOCKED` collapse the key generation problem into the storage problem, eliminating an entire class of complexity.

---

## Section 5: Evolution Summary Table

| Criterion | 1. Hash Truncation | 2. Snowflake Counter | 3. Pre-allocated + SKIP LOCKED |
|---|---|---|---|
| **Collision Risk** | High at scale (birthday problem: 17% at 10B URLs) | None (unique by construction) | None (unique by DB constraint) |
| **Write Latency** | O(1) base + retries on collision (~2ms base, ~10ms+ with retries) | O(1) single ID generation (~0.1ms) + DB insert (~2ms) | O(1) single atomic query (~2-5ms) |
| **Extra Services** | None | ID Generator Service (Snowflake) + Zookeeper/etcd for machine ID coordination | None (DB is the KGS) |
| **Concurrency Model** | Retry on collision (serialization under contention) | Lock-free (unique IDs, no contention at generator level) | Zero-contention (SKIP LOCKED, each request gets its own row) |
| **Predictable?** | No (hash output appears random) | Yes (sequential timestamps) -- needs cryptographic mitigation | No (random pre-generated strings) |
| **Same URL = Same Key?** | Yes (deterministic hash) | No (different ID each time) | No (different row claimed each time) |
| **Operational Complexity** | Low (no extra services) but high (retry monitoring, collision tracking) | High (ID generator cluster, clock sync, machine ID coordination) | Low (one table, one query, one background refill job) |
| **Scales to 720B URLs?** | No -- collision rate makes it impractical beyond ~10B | Yes -- ID space is 2^63, far beyond our needs | Yes -- key recycling makes the pool effectively infinite |
| **Verdict** | **Not viable** at >1B URLs | **Viable** but adds dependency and needs predictability fix | **The answer** -- simplest, most robust, zero collisions, zero contention |

---

## Section 6: Concurrency Analysis with Numbers

### Baseline Numbers

```
Average write rate:   230 writes/sec
Peak write rate:      1,000 writes/sec
Extreme peak (10x):   10,000 writes/sec (flash sale, viral event)

Transaction duration: 2-5ms (query planning + index scan + row lock + UPDATE + COMMIT)
  Breakdown:
    - Index scan to find candidate row:     0.5-1ms
    - Acquire row lock:                     0.01ms (in-memory operation)
    - Skip locked rows (if any):            0.01ms per skip
    - UPDATE the row:                       0.5-1ms
    - WAL write (fsync):                    0.5-2ms
    - COMMIT + release lock:                0.1ms
    ─────────────────────────────────────────────
    Total:                                  ~2-5ms
```

### Contention Analysis

At any given instant, the number of **simultaneously locked rows** equals:

```
locked_rows = write_rate x transaction_duration

At average load (230 writes/sec):
  locked_rows = 230 x 0.005 = 1.15 rows locked simultaneously

At peak load (1,000 writes/sec):
  locked_rows = 1,000 x 0.005 = 5 rows locked simultaneously

At extreme peak (10,000 writes/sec):
  locked_rows = 10,000 x 0.005 = 50 rows locked simultaneously
```

### Probability of Contention

"Contention" occurs when a request's initial candidate row is already locked, forcing SKIP LOCKED to find the next row. The probability of this is:

```
P(contention) = locked_rows / available_rows

Available rows (keys where expiry_time < NOW()): ~1,000,000,000 (1 billion minimum)

At average load:
  P(contention) = 1.15 / 1,000,000,000 = 0.00000000115 = 0.000000115%
  Interpretation: Essentially never happens

At peak load:
  P(contention) = 5 / 1,000,000,000 = 0.000000005 = 0.0000005%
  Interpretation: Essentially never happens

At extreme peak (10x):
  P(contention) = 50 / 1,000,000,000 = 0.00000005 = 0.000005%
  Interpretation: Still essentially never happens

Even at 100x peak (100,000 writes/sec -- far beyond any realistic scenario):
  P(contention) = 500 / 1,000,000,000 = 0.0000005 = 0.00005%
  Interpretation: 1 in 2 million chance of needing to skip a row. Negligible.
```

### What Contention Would Look Like (If It Happened)

Even when a row IS locked and needs to be skipped, the cost is trivial:

```
Without contention:
  1. Index scan finds Row A (available, unlocked)
  2. Lock Row A
  3. Return Row A
  Total: ~0.5ms for the scan

With one skip:
  1. Index scan finds Row A (available, but LOCKED by another transaction)
  2. SKIP Row A (0.01ms)
  3. Index scan continues to Row B (available, unlocked)
  4. Lock Row B
  5. Return Row B
  Total: ~0.55ms for the scan (0.05ms overhead)

The overhead per skip is ~0.01-0.05ms. Even with 50 skips (extreme case),
total overhead: ~50 x 0.03ms = ~1.5ms. Still under 5ms total transaction time.
```

### Comparison: Without SKIP LOCKED

To appreciate what SKIP LOCKED gives us, consider the alternative:

```
WITHOUT SKIP LOCKED (just FOR UPDATE, which WAITS for locks):

  Request 1: locks Row A → starts 5ms transaction
  Request 2: tries Row A → BLOCKED, waits for Request 1 (up to 5ms wait)
  Request 3: tries Row A → BLOCKED, waits for Request 2 (up to 10ms wait)
  Request 4: tries Row A → BLOCKED, waits for Request 3 (up to 15ms wait)
  ...

At 1,000 writes/sec:
  - Effective throughput: 1 / 0.005 = 200 transactions/sec (one at a time per hot row)
  - Queue depth: 1,000 - 200 = 800 requests queueing per second
  - Average wait time: grows linearly → 800 / 200 = 4 seconds average wait
  - p99 latency: >10 seconds → timeouts → cascading failures

WITH SKIP LOCKED:
  - All 1,000 requests execute in parallel (each gets its own row)
  - Zero queueing
  - Consistent 2-5ms latency at any percentile
  - p99 latency: ~5ms (limited by WAL fsync variance)
```

### Throughput Ceiling

What is the theoretical maximum write throughput with this approach?

```
The limit is NOT row-level contention (which is effectively zero).
The limit is PostgreSQL's ability to process transactions:

Single PostgreSQL instance:
  - WAL write throughput: ~10,000-50,000 commits/sec (depends on fsync settings)
  - With synchronous_commit = off: up to 100,000 commits/sec
  - Connection pool: 100-200 connections
  - Each connection handles 200-500 transactions/sec

Conservative single-instance throughput: ~20,000 writes/sec
Aggressive (tuned) single-instance: ~50,000 writes/sec

Our peak requirement: 1,000 writes/sec
Headroom: 20x - 50x above peak on a SINGLE instance

With Citus (4 shards):
  Theoretical max: 80,000 - 200,000 writes/sec
  Headroom: 80x - 200x above peak
```

---

## Section 7: Collision Probability Math (Birthday Problem)

This section provides a rigorous mathematical treatment for interviewers who want to verify the collision claims.

### The Classic Birthday Problem

**Setup**: n items placed independently and uniformly at random into m buckets.

**Question**: What is the probability that at least two items share the same bucket?

**Exact formula**:

```
P(at least one collision) = 1 - P(no collisions)

P(no collisions) = (m/m) * ((m-1)/m) * ((m-2)/m) * ... * ((m-n+1)/m)
                 = m! / ((m-n)! * m^n)

For large m, this is well-approximated by:
P(no collisions) ≈ e^(-n(n-1)/(2m)) ≈ e^(-n^2/(2m))
```

**Expected number of collisions** (for n << m):

```
E(collisions) ≈ n^2 / (2m)

This counts the expected number of (i,j) pairs where item i and item j collide.
More precisely, it's the expected number of pairs, which equals C(n,2) * (1/m)
= n(n-1)/(2m) ≈ n^2/(2m)
```

### Application to URL Shortening

Our key space: m = 36^8 = 2,821,109,907,456 (2.82 trillion)

**Detailed calculation table**:

| URLs Created (n) | n^2 | n^2 / (2m) | P(no collision) = e^(-n^2/(2m)) | P(>=1 collision) | Expected Collisions |
|---|---|---|---|---|---|
| 10^4 (10K) | 10^8 | 1.77 x 10^-5 | 0.999982 | 0.0018% | ~0.0000177 |
| 10^5 (100K) | 10^10 | 1.77 x 10^-3 | 0.998 | 0.177% | ~0.00177 |
| 10^6 (1M) | 10^12 | 0.177 | 0.838 | 16.2% | ~0.177 |
| 10^7 (10M) | 10^14 | 17.73 | 2.0 x 10^-8 | ~100% | ~17.7 |
| 10^8 (100M) | 10^16 | 1,773 | ~0 | ~100% | ~1,773 |
| 10^9 (1B) | 10^18 | 177,305 | ~0 | ~100% | ~177,305 |
| 10^10 (10B) | 10^20 | 17,730,496 | ~0 | ~100% | ~17.7M |
| 7.2 x 10^11 (720B) | 5.18 x 10^23 | 9.19 x 10^10 | ~0 | ~100% | ~91.9B |

**Wait -- this seems to say collisions are CERTAIN even at 10 million URLs?**

No. There is a subtlety. The birthday problem table above shows the probability **in a simple birthday problem** where all n items are placed simultaneously. In our hash-based URL shortener, we **check for existence** before inserting, and retry on collision. This changes the math:

**With collision checking and retry**:

Each insertion succeeds on the first try with probability:

```
P(first try succeeds for insertion k) = (m - k + 1) / m

After n successful insertions, the expected number of FAILED first attempts
(collisions that required retry) is:

E(retries) = sum_{k=1}^{n} (k-1)/m = n(n-1)/(2m) ≈ n^2/(2m)
```

So the expected number of retries is the same as the expected number of collisions in the birthday problem! The table remains valid for estimating how many retry operations the system will need.

**Corrected interpretation**:

| URLs Created (n) | Expected Retries (Total) | Retry Rate (per insert) | Practical Impact |
|---|---|---|---|
| 1 million | ~0.000177 | 1 in 5.6M | No impact whatsoever |
| 10 million | ~0.0177 | 1 in 565M | No impact |
| 100 million | ~1.77 | 1 in 56.5M | ~2 retries total. Negligible. |
| 1 billion | ~177 | 1 in 5.65M | 177 retries out of 1B inserts. Tolerable. |
| 10 billion | ~17.7M | 1 in 565 | 1 retry every 565 inserts. Noticeable. |
| 100 billion | ~1.77B | 1 in 56 | Retry on ~2% of inserts. Concerning. |
| 720 billion | ~91.9B | 1 in 8 | Retry on ~12.5% of inserts. Unacceptable. |

**The 50% collision threshold** (when do we expect at least one collision?):

```
P(at least one collision) = 0.5
1 - e^(-n^2/(2m)) = 0.5
e^(-n^2/(2m)) = 0.5
-n^2/(2m) = ln(0.5) = -0.693
n^2 = 2 * 0.693 * m = 1.386 * m
n = sqrt(1.386 * m) = sqrt(1.386 * 2.82 * 10^12)
n = sqrt(3.908 * 10^12)
n ≈ 1,977,000 ≈ 1.98 million

So with just ~2 million URLs, there's a 50% chance of at least one collision!
```

This is the "birthday paradox" in action: collisions occur far sooner than intuition suggests.

### Why the Pre-allocated Approach Avoids This Entirely

```
Hash-based approach:
  Key generation: hash(longURL) → might collide with existing key
  Collision probability: grows with n^2 (birthday problem)
  Must check existence on every insert
  Must retry on every collision

Pre-allocated approach:
  Key generation: claim pre-existing row with unique suffix
  Collision probability: ZERO (suffix was already verified unique at pre-generation time)
  No existence check needed
  No retry logic needed

The pre-allocated approach transforms the collision problem from a RUNTIME concern
into a ONE-TIME setup concern (ensuring uniqueness during pre-generation, which is
handled by the database's UNIQUE constraint).
```

---

## Section 8: Comparison with Real-World Systems

### Detailed System Comparison

| System | Key Gen Method | Key Format | Key Length | Key Space | Write Scale | Notes |
|---|---|---|---|---|---|---|
| **Bitly** (early, ~2008) | MD5 hash truncation | Base-62 | 6 chars | 62^6 = 56.8B | ~100 writes/sec | Simple hash, collision retry. Worked at early scale. |
| **Bitly** (later, ~2012+) | Counter + base-62 encoding | Base-62 | 6-7 chars | 62^7 = 3.5T | ~10,000 writes/sec | Migrated to counter-based as collision rate grew. Uses separate counter service. |
| **TinyURL** (original) | Auto-increment counter + base-62 | Base-62 | Variable (grows over time) | Effectively unlimited | Low-moderate | Simplest possible approach. IDs are sequential (security concern for private URLs). |
| **Twitter t.co** | Snowflake-derived | Custom encoding | 10 chars | Very large (~10^18) | ~50,000+ writes/sec | Leverages existing Snowflake infra. Sequential IDs are acceptable since tweets are public. |
| **Google goo.gl** (deprecated 2019) | Hash-based | Base-62 | 5-6 chars | 62^5 = 916M to 62^6 = 56.8B | ~100,000 writes/sec (Google scale) | Small key space! Required aggressive collision handling. Deprecated partly due to abuse. |
| **Rebrandly** | Random generation + uniqueness check | Base-62 | 5-8 chars (user-configurable) | Varies | Moderate | Generates random string, checks DB, retries on collision. Simple but O(retries) at scale. |
| **Kutt.it** (open-source) | Random generation | Alphanumeric | 6 chars | 62^6 = 56.8B | Low | Open-source, not designed for extreme scale. |
| **Our design** | Pre-allocated + SKIP LOCKED | Base-36 | 8 chars | 36^8 = 2.82T | 1,000+/sec peak, ceiling ~50K/sec | Zero collisions, zero contention, no extra service, automatic recycling. |

### Key Insights from Real-World Evolution

**Insight 1: Every high-scale system migrated AWAY from hashing.**

Bitly started with hash truncation and migrated to counter-based generation. Google's goo.gl used hashing and eventually deprecated the service. The pattern is clear: hashing works at small scale but becomes operationally painful as collision rates grow.

**Insight 2: Counter-based systems work but require infrastructure.**

Twitter's t.co works well because Twitter already operates Snowflake at massive scale. The marginal cost of adding t.co to Snowflake is near-zero. For a standalone URL shortener, building and operating a distributed counter service is significant overhead.

**Insight 3: Key space size matters more than key generation algorithm.**

Google's goo.gl had a relatively small key space (62^5 = 916M for 5-char URLs). Even with a perfect key generation algorithm, a small key space means:
- Higher collision rates (for hash-based approaches)
- Faster exhaustion (for counter-based approaches)
- More aggressive recycling needed

Our choice of 36^8 = 2.82 trillion provides ample headroom:
- 720 billion URLs over 100 years uses only 25.5% of the space
- Collision probability for hashing is manageable up to ~1 billion URLs
- Pre-allocated approach makes key space utilization irrelevant (we only pre-generate what we need)

**Insight 4: The best approach depends on existing infrastructure.**

```
If you already have:                  Then use:
─────────────────────────────────────────────────────────
Snowflake / distributed ID service  → Snowflake IDs + Feistel cipher
Redis cluster                       → Redis INCR-based counter + base encoding
Just PostgreSQL/MySQL                → Pre-allocated + SKIP LOCKED  ← OUR CASE
A very simple setup                 → Hash-based (if scale < 1B URLs)
```

Our design assumes PostgreSQL as the primary (and only) database, making the pre-allocated approach the natural choice -- it requires zero additional infrastructure.

### Architecture Comparison: Failure Modes

```
Hash-Based:
  ┌───────────────┐     ┌──────────────┐
  │   App Server   │────►│  PostgreSQL   │
  └───────────────┘     └──────────────┘
  Failure modes:
    1. DB down → all writes fail (same for all approaches)
    2. Hash collision storm → latency spike → possible cascading timeout

Snowflake-Based:
  ┌───────────────┐     ┌──────────────────┐     ┌──────────────┐
  │   App Server   │────►│ Snowflake Cluster │────►│  PostgreSQL   │
  └───────────────┘     └──────────────────┘     └──────────────┘
  Failure modes:
    1. Snowflake cluster down → all writes fail (even if DB is healthy)
    2. Clock skew → potential duplicate IDs (rare but possible)
    3. DB down → all writes fail
    4. Network partition between App and Snowflake → writes fail
    Total additional failure modes: 3

Pre-Allocated + SKIP LOCKED:
  ┌───────────────┐     ┌──────────────┐
  │   App Server   │────►│  PostgreSQL   │
  └───────────────┘     └──────────────┘
  Failure modes:
    1. DB down → all writes fail (same for all approaches)
    2. Key pool exhaustion → writes fail (mitigated by monitoring + refill)
    Total additional failure modes: 1 (and it's easily mitigated)
```

---

## Interview Tips and Common Follow-Up Questions

### Q: "What if PostgreSQL goes down? Isn't this a single point of failure?"

**A**: PostgreSQL is the datastore for ALL three approaches. If PostgreSQL goes down, writes fail regardless of key generation strategy. The pre-allocated approach does not add any ADDITIONAL points of failure -- unlike Snowflake, which adds the ID generator cluster as a second point of failure.

For high availability: use PostgreSQL streaming replication with automatic failover (Patroni + etcd). The failover time is ~5-10 seconds. During failover, in-flight SKIP LOCKED transactions will fail and can be retried by the application.

### Q: "What if two pre-generated suffixes collide?"

**A**: This cannot happen at runtime because each suffix occupies its own row in the database. The UNIQUE constraint on the `suffix` column prevents duplicate suffixes from being inserted during pre-generation. Any duplicates generated randomly are discarded (via `ON CONFLICT DO NOTHING`).

### Q: "Why base-36 instead of base-62?"

**A**: Base-36 uses only lowercase letters and digits (`0-9a-z`), making URLs case-insensitive and human-friendly. Base-62 adds uppercase letters (`A-Z`), creating case-sensitive URLs that are harder to communicate verbally ("Is that a capital I or lowercase L?"). The trade-off: base-36 needs 8 characters for 2.82 trillion key space, while base-62 needs only 6 characters for 56.8 billion (or 7 for 3.5 trillion). We chose readability over brevity.

### Q: "How does this work with database sharding (Citus)?"

**A**: Shard by the `suffix` column (hash-based sharding in Citus). Each shard independently maintains its own pool of available keys. The `FOR UPDATE SKIP LOCKED` operates locally within each shard -- the coordinator routes the query to a shard, and that shard handles the lock-and-claim atomically. No cross-shard coordination is needed.

```
Shard 1 (suffixes starting with 0-8):
  Available keys: ~333M
  Handles: ~33% of writes

Shard 2 (suffixes starting with 9-j):
  Available keys: ~333M
  Handles: ~33% of writes

Shard 3 (suffixes starting with k-z):
  Available keys: ~334M
  Handles: ~34% of writes

Each shard independently handles FOR UPDATE SKIP LOCKED.
Total system throughput = sum of per-shard throughput.
```

### Q: "What about the read path? How do redirects work?"

**A**: The read path (redirect) is a simple primary key lookup:

```sql
SELECT long_url FROM url_mappings
WHERE suffix = :suffix AND expiry_time > NOW();
```

This is an indexed lookup on the UNIQUE `suffix` column: O(log n) B-tree traversal, ~0.1-0.5ms. With a Redis cache in front (cache hit rate ~95-99%), most redirects never touch the database at all. The key generation strategy has no impact on read performance.

### Q: "Can this approach handle burst traffic (e.g., viral event)?"

**A**: Yes. At 10x peak (10,000 writes/sec), only ~50 rows are locked simultaneously out of ~1 billion available. The database processes these as independent transactions with zero contention. The bottleneck shifts to PostgreSQL's transaction processing capacity (~20,000-50,000 per second on a single instance), which provides 20-50x headroom over our peak requirement.

For even higher bursts: add a write-back queue (Kafka/SQS) in front of the database, buffering bursts and draining at the database's comfortable processing rate.

---

## Summary

The key generation problem in URL shortening is analogous to the trie data structure in autocomplete: it is the foundational algorithmic decision that determines system behavior at scale. The three canonical approaches -- hash truncation, distributed counters, and pre-allocated keys -- represent an evolution from simplicity-at-small-scale to robustness-at-any-scale.

**Pre-allocated keys with `FOR UPDATE SKIP LOCKED`** is the optimal approach for a PostgreSQL-backed URL shortener because it:

1. **Eliminates collisions** by construction (UNIQUE constraint)
2. **Eliminates contention** via SKIP LOCKED (zero waiting)
3. **Eliminates service dependencies** (no separate ID generator)
4. **Provides automatic recycling** (expired URLs become available keys)
5. **Maintains unpredictability** (random pre-generated suffixes)
6. **Stays simple** (one table, one query, one background job)

It is the rare design decision that improves every dimension simultaneously: correctness, performance, simplicity, and operability.

---

*This document complements the [Interview Simulation](interview-simulation.md). For database choice rationale, see [SQL vs NoSQL Tradeoffs](sql-vs-nosql-tradeoffs.md). For caching and scaling details, see [Scaling & Caching](scaling-and-caching.md).*
