# OpenSearch Indexing and Inverted Index Internals

Deep-dive into how OpenSearch (and the underlying Lucene engine) indexes documents,
structures data on disk, and serves queries at scale. Understanding these internals
is critical for diagnosing performance issues and designing search-heavy systems.

---

## 1. Inverted Index Fundamentals

An inverted index is the core data structure behind full-text search. It maps every
unique term to the list of documents that contain it, much like the index at the back
of a textbook maps a topic to page numbers.

### Structure

```
Term Dictionary          Posting Lists
--------------          -----------------------------------------------
"database"       --->   [(doc1, tf=3, pos=[5,12,40]), (doc7, tf=1, pos=[2])]
"distributed"    --->   [(doc1, tf=1, pos=[3]),        (doc3, tf=2, pos=[1,8])]
"opensearch"     --->   [(doc2, tf=4, pos=[0,6,11,20])]
"shard"          --->   [(doc1, tf=2, pos=[7,15]),     (doc2, tf=1, pos=[9])]
```

### What a Posting List Entry Contains

| Component        | Purpose                                           |
|------------------|---------------------------------------------------|
| Document ID      | Which document contains the term                  |
| Term Frequency   | How many times the term appears (for scoring)     |
| Positions        | Where in the document the term appears (for phrase queries) |
| Offsets          | Character start/end offsets (for highlighting)    |
| Payloads         | Optional per-position metadata                    |

### Why O(1) Lookup

The term dictionary is stored as a **Finite State Transducer (FST)** -- a compact,
memory-mapped automaton. Looking up a term is essentially walking a state machine
character by character. For practical purposes this gives O(k) lookup where k is the
length of the term, which is bounded and small. Once you reach the term, you have a
direct pointer to its posting list on disk. No table scan required.

### The Back-of-Book Analogy

```
Traditional Book Index              Inverted Index
-------------------------------     --------------------------------
"Concurrency" .... pages 45, 89     "concurrency" -> [doc3, doc17, doc42]
"Deadlock" ........ page 92         "deadlock"    -> [doc42, doc88]
"Mutex" ........... pages 45, 46    "mutex"       -> [doc3, doc17]

Same idea: term -> locations. Lucene just makes it fast, compressed, and queryable.
```

### Forward Index vs Inverted Index

A **forward index** maps document -> terms (like a table of contents).
An **inverted index** maps term -> documents (like a back-of-book index).

A relational database row is essentially a forward index. To find all rows containing
"database" in a text column, you must scan every row. An inverted index flips this:
given "database", you immediately know which documents contain it.

---

## 2. Text Analysis Pipeline

Before any term enters the inverted index, it passes through the **analysis pipeline**.
This pipeline determines what tokens are actually indexed. Getting this wrong is the
number one cause of "my search doesn't return results" bugs.

### Pipeline Stages

```
Original Text
     |
     v
+-------------------+
| Character Filters |  Strip HTML, map characters, normalize unicode
+-------------------+
     |
     v
+-------------------+
|    Tokenizer      |  Split text into individual tokens
+-------------------+
     |
     v
+-------------------+
|  Token Filters    |  Lowercase, remove stopwords, stem, synonyms
+-------------------+
     |
     v
Terms added to inverted index
```

### Concrete Examples

**Standard Analyzer** (the default):

```
Input:   "The Quick Brown FOX! jumped-over 2 lazy dogs."
          |
          | standard tokenizer (splits on whitespace + punctuation)
          v
         ["The", "Quick", "Brown", "FOX", "jumped", "over", "2", "lazy", "dogs"]
          |
          | lowercase token filter
          v
Output:  ["the", "quick", "brown", "fox", "jumped", "over", "2", "lazy", "dogs"]
```

**Keyword Analyzer** (no tokenization at all):

```
Input:   "order-12345-ABC"
Output:  ["order-12345-ABC"]

Use case: exact-match fields like order IDs, SKUs, email addresses.
```

**English Language Analyzer** (stemming + stopwords):

```
Input:   "The runners were running quickly through the forest"
          |
          | standard tokenizer
          v
         ["The", "runners", "were", "running", "quickly", "through", "the", "forest"]
          |
          | lowercase filter
          | English stopword removal ("the", "were", "through")
          | English stemmer
          v
Output:  ["runner", "run", "quick", "forest"]

"running" -> "run", "runners" -> "runner", "quickly" -> "quick"
```

**Custom Analyzer Example** (e-commerce product search):

```json
{
  "settings": {
    "analysis": {
      "analyzer": {
        "product_analyzer": {
          "type": "custom",
          "char_filter": ["html_strip"],
          "tokenizer": "standard",
          "filter": ["lowercase", "english_stemmer", "synonym_filter"]
        }
      },
      "filter": {
        "synonym_filter": {
          "type": "synonym",
          "synonyms": ["laptop,notebook", "phone,mobile,cell"]
        },
        "english_stemmer": {
          "type": "stemmer",
          "language": "english"
        }
      }
    }
  }
}
```

### Wrong Analyzer = Broken Search

Common scenario:

```
1. Index a field with "keyword" analyzer  -> stores "Running Shoes" as one token
2. Query with "standard" analyzer         -> searches for "running" and "shoes"
3. No match. User files a bug.

Fix: ensure index-time and query-time analyzers are compatible.
     Use the _analyze API to debug:

POST /my_index/_analyze
{
  "field": "product_name",
  "text": "Running Shoes"
}
```

**Rule of thumb**: The analyzer used at index time and query time must produce
overlapping tokens, or the search will silently return nothing.

---

## 3. Lucene Segments

Every OpenSearch shard is a Lucene index, and every Lucene index is composed of
**segments**. Segments are the fundamental unit of storage and search.

### Key Properties

- **Immutable**: Once written, a segment is never modified. This is the single
  most important design decision in Lucene.
- **Self-contained**: Each segment is a complete, independent inverted index with
  its own term dictionary, posting lists, stored fields, doc values, and norms.
- **Lock-free reads**: Because segments never change, any number of threads can
  read them concurrently without locks or coordination.

### Segment Anatomy

```
+----------------------------------------------------------+
|                    Lucene Segment                         |
|                                                          |
|  +------------------+   +-----------------------------+  |
|  | Term Dictionary  |   |       Posting Lists         |  |
|  |   (FST in RAM)   |-->|  term -> [docIDs, freq,     |  |
|  |                  |   |           positions, offsets] |  |
|  +------------------+   +-----------------------------+  |
|                                                          |
|  +------------------+   +-----------------------------+  |
|  |  Stored Fields   |   |       Doc Values             |  |
|  |  (_source JSON)  |   |  (columnar, for sort/agg)   |  |
|  +------------------+   +-----------------------------+  |
|                                                          |
|  +------------------+   +-----------------------------+  |
|  |     Norms        |   |     Live Docs Bitset         |  |
|  | (field lengths   |   |  (tracks deletes:            |  |
|  |  for scoring)    |   |   1=live, 0=deleted)         |  |
|  +------------------+   +-----------------------------+  |
|                                                          |
+----------------------------------------------------------+
```

### How New Documents Become Segments

```
        Index Request
             |
             v
   +-------------------+
   |  In-Memory Buffer |   (not yet searchable)
   |  (indexing buffer) |
   +-------------------+
             |
             | refresh (default every 1 second)
             v
   +-------------------+
   |   New Segment     |   (now searchable, on filesystem cache)
   |   (immutable)     |
   +-------------------+
             |
             | flush / fsync
             v
   +-------------------+
   |   Segment on Disk |   (durable)
   +-------------------+
```

### How Deletes and Updates Work with Immutable Segments

Since segments are immutable, you cannot modify a document in place:

- **Delete**: Mark the document as deleted in the segment's live docs bitset.
  The document still physically exists but is filtered out of search results.
- **Update**: Delete the old version + index a new version. This is why updates
  are as expensive as delete + insert.

Deleted documents are truly purged only during **segment merges**.

---

## 4. Near-Real-Time (NRT) Search

OpenSearch is not a real-time search engine. It is a **near-real-time** one.
There is a gap (by default ~1 second) between indexing a document and that document
becoming visible in search results.

### The NRT Timeline

```
Time ------>

t=0          t=0.5s              t=1s               t=1.5s
 |             |                   |                   |
 v             v                   v                   v
Index doc   Doc sits in         Refresh fires        Doc now
request     in-memory buffer    (buffer -> segment)   appears in
arrives     (NOT searchable)    (searchable!)         search results
```

### Configuration

```json
PUT /my_index/_settings
{
  "index.refresh_interval": "1s"    // default
}
```

Common configurations:

| Setting   | Use Case                                                |
|-----------|---------------------------------------------------------|
| `1s`      | Default. Good for most interactive search workloads     |
| `5s`      | High-write indexes where slight staleness is acceptable |
| `30s`     | Logging/metrics where freshness matters less            |
| `-1`      | Disable auto-refresh entirely (manual refresh only)     |

### Why This Matters for System Design

If you are designing a system where a user creates a record and then immediately
searches for it, you may get a "read-your-own-write" inconsistency. Solutions:

1. Use the `?refresh=wait_for` parameter (blocks until next refresh).
2. Use the `?refresh=true` parameter (forces immediate refresh -- expensive).
3. Use a GET by `_id` (bypasses the inverted index, reads from translog).
4. Accept eventual consistency and design the UI around it.

---

## 5. Write Path Flow

This is a critical section for system design interviews. Know this flow cold.

### End-to-End Write Path

```
Client (SDK / REST)
       |
       |  HTTP PUT /my_index/_doc/123
       v
+--------------------+
| Coordinating Node  |  (any node can be the coordinator)
+--------------------+
       |
       |  Route to correct shard: shard = hash(_id) % num_primary_shards
       v
+--------------------+
|   Primary Shard    |
+--------------------+
       |
       |  1. Write to TRANSLOG (Write-Ahead Log) -- durability
       |  2. Add doc to in-memory indexing buffer -- not yet searchable
       |  3. Return success to coordinating node? No, not yet...
       |
       |  Replicate to replica shards (in parallel)
       v
+--------------------+    +--------------------+
|  Replica Shard 1   |    |  Replica Shard 2   |
+--------------------+    +--------------------+
       |                          |
       |  Each replica:           |
       |  1. Write to own translog|
       |  2. Add to own buffer    |
       |  3. Ack back to primary  |
       v                          v
       +--------+---------+-------+
                |
                v
       Primary acks to coordinating node
                |
                v
       Coordinating node acks to client
```

### Translog (Write-Ahead Log)

The translog is the durability guarantee. If the node crashes before a flush,
uncommitted documents can be replayed from the translog on recovery.

```
Translog fsync behavior (index.translog.durability):

"request"  (default) -- fsync translog after every index operation
                        Safest. Every ack means the data is on disk.
                        Higher latency per write.

"async"              -- fsync translog every 5 seconds (configurable)
                        Faster writes. Risk of losing up to 5 seconds
                        of data on crash.
                        Use for logging/metrics where some loss is OK.
```

### The Full Lifecycle

```
Index Request
     |
     v
[Translog Write + Buffer Write]      -- durable but not searchable
     |
     | refresh (every 1s)
     v
[New Segment in Filesystem Cache]    -- searchable but not fsync'd to disk
     |
     | flush (every 30 min or translog too large)
     v
[Segment fsync'd to Disk]           -- durable AND searchable
[Translog Cleared]                   -- no longer needed for recovery
```

---

## 6. Bulk Indexing Performance

Single-document indexing involves per-request overhead: HTTP connection setup,
routing, waiting for replica acks. The `_bulk` API amortizes this overhead across
many operations.

### _bulk API Format (NDJSON)

```
POST /_bulk
{"index": {"_index": "products", "_id": "1"}}
{"name": "Laptop", "price": 999}
{"index": {"_index": "products", "_id": "2"}}
{"name": "Phone", "price": 699}
{"delete": {"_index": "products", "_id": "3"}}
{"update": {"_index": "products", "_id": "1"}}
{"doc": {"price": 899}}
```

Each line is a separate JSON object. No wrapping array. Newline-delimited.
This format allows OpenSearch to parse and route each sub-request individually
without buffering the entire payload in memory.

### Optimal Bulk Size

| Factor              | Guidance                                        |
|---------------------|-------------------------------------------------|
| Payload size        | 5-15 MB per bulk request (not document count)   |
| Thread count        | 2-4 concurrent bulk threads per node            |
| Document count      | Varies; measure by total payload size, not count|
| Too small           | Overhead dominates. Throughput suffers.          |
| Too large           | Memory pressure, long GC pauses, timeouts       |

### Tuning Knobs for Bulk Loads

```json
// 1. Increase refresh interval (fewer segments created during load)
PUT /my_index/_settings
{ "index.refresh_interval": "60s" }

// 2. Increase translog flush threshold (fewer disk flushes)
PUT /my_index/_settings
{ "index.translog.flush_threshold_size": "1gb" }

// 3. Set replica count to 0 during initial load (no replication overhead)
PUT /my_index/_settings
{ "index.number_of_replicas": 0 }

// 4. After load completes, restore settings
PUT /my_index/_settings
{
  "index.refresh_interval": "1s",
  "index.number_of_replicas": 2
}

// 5. Force merge to optimize segment count
POST /my_index/_forcemerge?max_num_segments=1
```

### Auto-Generated _id

When you do not supply an `_id`, OpenSearch generates a random one (time-based UUID).
This is **faster** than supplying your own because:

- No version lookup required (no need to check if doc already exists).
- UUIDs with time prefix have better write locality in the ID lookup structures.

If you need your own IDs, supply them. If not, let OpenSearch generate them.

---

## 7. Segment Merge

Over time, refreshes create many small segments. Too many segments slow down search
because every query must check every segment. Segment merging consolidates small
segments into larger ones.

### Why Merge

```
Before Merge (20 small segments):
Search query must:
  1. Search segment_0  (500 docs)
  2. Search segment_1  (200 docs)
  3. Search segment_2  (800 docs)
  ...
  20. Search segment_19 (100 docs)
  21. Merge 20 result sets

After Merge (3 larger segments):
Search query must:
  1. Search segment_A  (50,000 docs)
  2. Search segment_B  (40,000 docs)
  3. Search segment_C  (30,000 docs)
  4. Merge 3 result sets

Fewer segments = fewer file handles, less memory, faster queries.
```

### Tiered Merge Policy (Default)

The tiered merge policy groups segments of similar size into tiers and merges
segments within the same tier:

```
Tier 0 (tiny):      [seg0] [seg1] [seg2] [seg3] [seg4]
                           \       |       /
                            \      |      /
                             v     v     v
Tier 1 (medium):          [    seg_merged_A    ]  [seg5] [seg6]
                                         \          |      /
                                          \         |     /
                                           v        v    v
Tier 2 (large):                        [    seg_merged_B       ]
```

Key parameters:

| Parameter                            | Default | Meaning                        |
|--------------------------------------|---------|--------------------------------|
| `max_merge_at_once`                  | 10      | Max segments merged at once    |
| `segments_per_tier`                  | 10      | Target segments per tier       |
| `max_merged_segment`                 | 5 GB    | Never create segments larger than this |
| `floor_segment`                      | 2 MB    | Segments below this always eligible for merge |

### Merge and Deletes

This is where deleted documents are truly purged. When segments merge, the new
merged segment simply omits any documents flagged as deleted. This is the only
mechanism that reclaims disk space from deletes.

### Force Merge

```
POST /my_index/_forcemerge?max_num_segments=1
```

Use **only** on read-only indices (e.g., time-based indices that have rolled over).
Never force merge an actively-written index -- it wastes I/O because new small
segments will immediately appear again.

### I/O Throttling

Merges are I/O intensive. OpenSearch throttles merge I/O to avoid starving
search and indexing:

```json
PUT /_cluster/settings
{
  "persistent": {
    "indices.store.throttle.max_bytes_per_sec": "50mb"
  }
}
```

---

## 8. Doc Values and Column Stores

The inverted index is optimized for answering: **"Which documents contain term X?"**

But sorting, aggregations, and scripting need to answer: **"What is the value of
field Y in document Z?"** The inverted index is terrible at this -- you would need
to un-invert it, iterating over every term to reconstruct per-document values.

**Doc values** solve this with a column-oriented data structure.

### Row Store vs Column Store

```
Row-oriented (inverted index / _source):
  doc1: { "price": 999, "brand": "Apple",   "rating": 4.5 }
  doc2: { "price": 699, "brand": "Samsung",  "rating": 4.2 }
  doc3: { "price": 299, "brand": "OnePlus",  "rating": 4.0 }

Column-oriented (doc values):
  price:  [999, 699, 299]       <-- contiguous on disk
  brand:  ["Apple", "Samsung", "OnePlus"]
  rating: [4.5, 4.2, 4.0]
```

Column orientation means that sorting by price reads only the price column
sequentially from disk -- no need to load entire documents.

### When Doc Values Are Used

| Operation               | Data Structure Used     |
|-------------------------|-------------------------|
| Full-text search        | Inverted index          |
| Term filtering          | Inverted index          |
| Sorting                 | Doc values              |
| Aggregations            | Doc values              |
| Scripting (field access)| Doc values              |

### Disabling Doc Values

If you know a field will never be sorted/aggregated on, disable doc values to save
disk and indexing time:

```json
{
  "mappings": {
    "properties": {
      "description": {
        "type": "text",
        "doc_values": false    // text fields have this off by default anyway
      },
      "internal_code": {
        "type": "keyword",
        "doc_values": false    // saves space if you only filter, never sort/agg
      }
    }
  }
}
```

Note: `text` fields do not have doc values by default (they use `fielddata` if
needed, which is heap-based and dangerous). `keyword`, `numeric`, `date`, `boolean`,
`ip`, and `geo_point` fields all have doc values enabled by default.

---

## 9. Stored Fields vs _source

### _source Field

By default, OpenSearch stores the entire original JSON document in a special field
called `_source`. This is what you get back in search results.

```
_source stores: {"name": "Laptop", "price": 999, "brand": "Apple", "desc": "A great laptop"}
```

**Pros**: Lets you retrieve the full document. Required for update, reindex, and
highlight operations. Acts as the single source of truth.

**Cons**: Takes disk space. For large documents with many fields, this can be
significant.

### Stored Fields

Individual fields can be marked as `"store": true` to store them separately from
`_source`. When you retrieve a stored field, OpenSearch reads just that field's
stored data rather than decompressing the entire `_source`.

```json
{
  "mappings": {
    "properties": {
      "title":   { "type": "text", "store": true },
      "content": { "type": "text" }
    }
  }
}
```

### Trade-offs

| Approach                  | Disk Usage | Retrieval of All Fields | Retrieval of One Field |
|---------------------------|------------|-------------------------|------------------------|
| `_source` enabled (default) | Baseline   | Fast (one decompress)   | Must decompress entire doc |
| `_source` disabled + stored fields | Lower if few fields stored | Must fetch each field individually | Fast (one field read) |
| `_source` with `_source.includes/excludes` | Same disk, less network | Filtered at retrieval time | Filtered at retrieval time |

### Practical Guidance

- **Almost always keep `_source` enabled.** You lose update, reindex, and highlight
  without it. These are hard to live without.
- Use stored fields only when documents are very large and you frequently need
  to retrieve just one or two small fields.
- Use `_source` filtering (`includes`/`excludes`) on the query to reduce network
  transfer without sacrificing functionality.

---

## 10. Contrast with RDBMS: B-tree vs Inverted Index

### Data Structure Comparison

```
B-tree (RDBMS):                          Inverted Index (OpenSearch):

     +-------+                           Term Dictionary
     | root  |                           +-----------+
     +---+---+                           | "apple"   | --> [doc2, doc5, doc9]
    /    |    \                           | "banana"  | --> [doc1, doc3]
   v     v     v                         | "cherry"  | --> [doc5, doc7, doc9]
 +---+ +---+ +---+                       +-----------+
 |   | |   | |   |
 +---+ +---+ +---+                       Optimized for: "find all docs with term X"
  / \   / \   / \
 v   v v   v v   v
[leaf nodes with row pointers]

Optimized for: "find row with key = X"
   or range: "find rows where X <= key <= Y"
```

### Access Pattern Comparison

| Access Pattern                  | RDBMS (B-tree)                    | OpenSearch (Inverted Index)         |
|---------------------------------|-----------------------------------|-------------------------------------|
| Find by primary key             | O(log N) -- B-tree traversal      | O(1) -- hash lookup on _id          |
| Find all rows containing "word" | O(N) -- full table scan or FTS    | O(1) -- term dictionary lookup      |
| Phrase search "quick brown fox" | Clumsy or impossible              | Native -- position data in postings |
| Range query (price 10-50)       | O(log N + k) -- B-tree range scan | O(terms in range) -- term iteration |
| Sorting by column               | Index scan or filesort            | Doc values (column store)           |
| Aggregation (GROUP BY)          | Index scan or temp table          | Doc values (column store)           |
| Joins                           | Native (nested loops, hash join)  | Not supported (denormalize instead) |
| Transactions / ACID             | Full ACID support                 | Not supported                       |
| Update single field             | In-place update                   | Delete + re-index entire document   |

### When to Use Which

| Use Case                          | Best Fit     |
|-----------------------------------|--------------|
| Primary data store with ACID      | RDBMS        |
| Full-text search over large corpus| OpenSearch   |
| Complex joins across many tables  | RDBMS        |
| Log analytics and aggregations    | OpenSearch   |
| Faceted search (e-commerce)       | OpenSearch   |
| Transactions (banking, orders)    | RDBMS        |
| Fuzzy matching, typo tolerance    | OpenSearch   |

### The Common Architecture

In practice, most production systems use **both**:

```
+----------+       +----------+       +----------------+
|  Client  | ----> |  RDBMS   | ----> |  OpenSearch    |
+----------+       | (source  |  CDC  | (search index) |
                   | of truth)|  or   |                |
                   +----------+  sync +----------------+
                                pipe
                                line

Write path: Client -> RDBMS -> CDC/sync -> OpenSearch
Read path (transactional): Client -> RDBMS
Read path (search): Client -> OpenSearch
```

The RDBMS is the source of truth. OpenSearch is a derived, eventually-consistent
search index. Changes flow from RDBMS to OpenSearch via Change Data Capture (CDC),
a message queue, or an application-level dual-write.

---

## Summary: What to Know for Interviews

| Concept               | One-Liner                                                         |
|-----------------------|-------------------------------------------------------------------|
| Inverted index        | Term -> posting list. O(1) lookup. The core of full-text search.  |
| Analysis pipeline     | Char filters -> tokenizer -> token filters. Controls what gets indexed. |
| Segments              | Immutable mini-indexes. Enable lock-free concurrent reads.        |
| NRT search            | ~1 second gap between index and searchable. Configurable.         |
| Write path            | Coord node -> primary shard (translog + buffer) -> replicas -> ack. |
| Bulk API              | 5-15 MB batches. Increase refresh_interval. Auto-generate _id.   |
| Segment merge         | Background consolidation. Tiered policy. Purges deletes.          |
| Doc values            | Column store for sort/agg. Complements the inverted index.        |
| _source               | Stores original JSON. Almost always keep it enabled.              |
| B-tree vs inverted    | Different structures for different access patterns. Use both.     |
