# Datastore Internals — Part 7: Indexing Deep Dive

> This document explains how database indexes work — the most interview-relevant topic in storage internals. We cover primary indexes, secondary indexes, composite indexes, inverted indexes (full-text search), and geospatial indexes. Understanding indexes is crucial for answering "how would you make this query fast?" in system design interviews.

---

## Table of Contents

1. [What Is an Index and Why Do We Need One?](#1-what-is-an-index-and-why-do-we-need-one)
2. [Primary Index (Clustered Index)](#2-primary-index-clustered-index)
3. [Secondary Index (Non-Clustered Index)](#3-secondary-index-non-clustered-index)
4. [Composite (Multi-Column) Index](#4-composite-multi-column-index)
5. [Dense vs Sparse Indexes](#5-dense-vs-sparse-indexes)
6. [Inverted Index — How Full-Text Search Works](#6-inverted-index--how-full-text-search-works)
7. [Geospatial Indexes — How "Find Near Me" Works](#7-geospatial-indexes--how-find-near-me-works)
8. [Covering Indexes and Index-Only Scans](#8-covering-indexes-and-index-only-scans)
9. [The Cost of Indexes — Write Penalty](#9-the-cost-of-indexes--write-penalty)
10. [Index Selection in System Design Interviews](#10-index-selection-in-system-design-interviews)

---

## 1. What Is an Index and Why Do We Need One?

```
WITHOUT AN INDEX:

  Table "users" with 10 million rows. Query: SELECT * FROM users WHERE email = 'alice@example.com'
  
  The database must do a FULL TABLE SCAN:
    → Read row 1: email = "zara@..."      → no match
    → Read row 2: email = "bob@..."       → no match
    → Read row 3: email = "charlie@..."   → no match
    → ... 10 million rows ...
    → Read row 7,234,891: email = "alice@example.com" → MATCH!
    
  Average: scan 5 million rows before finding the match.
  Time: 5,000,000 rows × ~0.001ms/row = ~5 seconds on SSD
  
  That's TERRIBLE for a simple lookup!


WITH AN INDEX:

  Create an index on the "email" column:
    CREATE INDEX idx_email ON users(email);
  
  Now the database builds a B+ tree on the email column:
  
  B+ tree index:
                    [john@... | mike@...]
                   /        |           \
         [alice@...|bob@..] [charlie@..] [nancy@..|zara@..]
  
  Query: WHERE email = 'alice@example.com'
    → Walk the B+ tree: 2-3 levels → find pointer to row
    → Read the actual row from the table
    
  Time: 3 disk reads × 0.1ms = 0.3ms
  
  0.3ms vs 5,000ms = 16,000x faster!


AN INDEX IS:
  A separate data structure (usually a B+ tree) that maps 
  column values → row locations, allowing fast lookups without 
  scanning the entire table.
  
  Think of it as: the index at the back of a textbook.
  Instead of reading every page to find "B-tree", you look in the
  index: "B-tree: pages 42, 67, 103" → go directly to those pages.
```

---

## 2. Primary Index (Clustered Index)

```
A PRIMARY INDEX determines the PHYSICAL ORDER of data on disk.
There can be only ONE primary index per table (because data can 
only be physically sorted one way).

In MySQL (InnoDB), the PRIMARY KEY is the clustered index.
The data IS the index — the leaf nodes of the B+ tree contain 
the actual row data.


MYSQL (InnoDB) CLUSTERED INDEX:

  Table: users (id PRIMARY KEY, name, email, age)
  
  The B+ tree IS the table:
  
                        [50]
                       /    \
               [20|35]       [70|90]
              / |   \        / |   \
  ┌──────┐ ┌──────┐ ┌──────┐ ┌──────┐ ┌──────┐ ┌──────┐
  │id: 10│ │id: 20│ │id: 35│ │id: 50│ │id: 70│ │id: 90│
  │Alice │ │Bob   │ │Charlie│ │Diana│ │Eve   │ │Frank │
  │alice@│ │bob@  │ │charl@│ │diana@│ │eve@  │ │frank@│
  │age:30│ │age:25│ │age:35│ │age:28│ │age:32│ │age:29│
  └──────┘ └──────┘ └──────┘ └──────┘ └──────┘ └──────┘
  ← Leaf nodes contain FULL ROW DATA, sorted by primary key →
  
  SELECT * FROM users WHERE id = 35:
    → Walk B+ tree → find leaf → row data is RIGHT THERE!
    → No extra lookup needed. 2-3 disk reads total.
  
  SELECT * FROM users WHERE id BETWEEN 20 AND 50:
    → Find leaf for id=20 → scan linked list to id=50
    → All data is contiguous (sorted by id) → sequential I/O!
    → Very fast range query on primary key.


POSTGRESQL APPROACH (Heap + Index):

  PostgreSQL does NOT use clustered indexes by default.
  Data is stored in a "heap" (unsorted) and indexes point to heap locations.
  
  Heap file (unsorted rows):
    Row 1: (id:35, Charlie, charlie@, age:35)  ← at offset 0
    Row 2: (id:10, Alice, alice@, age:30)      ← at offset 100
    Row 3: (id:70, Eve, eve@, age:32)          ← at offset 200
    Row 4: (id:20, Bob, bob@, age:25)          ← at offset 300
    ... rows in INSERT order, not sorted!
    
  Primary key index (B+ tree):
    id=10 → heap offset 100
    id=20 → heap offset 300
    id=35 → heap offset 0
    id=70 → heap offset 200
    
  SELECT * FROM users WHERE id = 35:
    → Walk B+ tree → find: id=35 → heap offset 0
    → Read row from heap at offset 0
    → 3-4 disk reads (index traversal + 1 heap read)
    
  Slightly slower than MySQL's clustered approach for primary key lookups,
  but more flexible for other access patterns.


KEY TAKEAWAY:
  
  MySQL (InnoDB): Primary key lookup = 2-3 disk reads (data IN the tree)
  PostgreSQL:     Primary key lookup = 3-4 disk reads (index + heap read)
  
  Both are fast. MySQL is slightly faster for PK lookups.
  PostgreSQL is slightly faster for non-PK access patterns.
```

---

## 3. Secondary Index (Non-Clustered Index)

```
A SECONDARY INDEX is an index on a NON-PRIMARY-KEY column.
You can have MANY secondary indexes per table.

Example: Index on "email" column when "id" is the primary key.

  CREATE INDEX idx_email ON users(email);


HOW IT WORKS (MySQL/InnoDB):

  Secondary index B+ tree:
  
                  [diana@... | frank@...]
                 /          |            \
    [alice@|bob@]  [charlie@|diana@]  [eve@|frank@]
    
  Leaf nodes contain: email → PRIMARY KEY (not the full row!)
  
    alice@example.com → id=10
    bob@example.com   → id=20
    charlie@example.com → id=35
    
  SELECT * FROM users WHERE email = 'alice@example.com':
    Step 1: Walk secondary index → find: email='alice@...' → id=10
    Step 2: Walk PRIMARY index → find: id=10 → full row data
    
    This second lookup is called a "BOOKMARK LOOKUP" or "INDEX LOOKUP."
    Total: ~5-6 disk reads (3 for secondary index + 2-3 for primary)
    
    Still MUCH faster than a full table scan (10 million rows)!


HOW IT WORKS (PostgreSQL):

  Secondary index B+ tree:
  
    alice@example.com → heap offset 100
    bob@example.com   → heap offset 300
    charlie@example.com → heap offset 0
    
  Leaf nodes point DIRECTLY to heap offsets (not to primary key).
  
  SELECT * FROM users WHERE email = 'alice@example.com':
    Step 1: Walk secondary index → find: email='alice@...' → heap offset 100
    Step 2: Read heap at offset 100 → full row
    
    Total: ~4 disk reads (3 for index + 1 for heap)
    Slightly fewer lookups than MySQL for secondary index queries!


WHEN TO CREATE SECONDARY INDEXES:

  ✅ Columns frequently used in WHERE clauses
     WHERE email = '...'
     WHERE status = 'active'
     WHERE created_at > '2024-01-01'
     
  ✅ Columns used in JOIN conditions
     JOIN orders ON users.id = orders.user_id
     
  ✅ Columns used in ORDER BY (avoids sorting)
     ORDER BY created_at DESC
     
  ❌ DON'T index columns rarely queried
  ❌ DON'T index columns with very low cardinality 
     (e.g., boolean "is_active" with only true/false)
     Unless combined with other columns in a composite index.
  ❌ DON'T index small tables (full scan is fast enough)
```

---

## 4. Composite (Multi-Column) Index

```
A COMPOSITE INDEX indexes MULTIPLE columns together.
The order of columns MATTERS enormously.

  CREATE INDEX idx_country_age ON users(country, age);

This creates a B+ tree sorted by (country, age):

  (DE, 25) < (DE, 30) < (DE, 35) < (US, 20) < (US, 25) < (US, 30) < (UK, 28)
  
  Sorted first by country, then by age within each country.


WHICH QUERIES CAN USE THIS INDEX?

  ✅ WHERE country = 'US'                    
     → Uses the index (country is the FIRST column)
     
  ✅ WHERE country = 'US' AND age = 30       
     → Uses the index (both columns, in order)
     
  ✅ WHERE country = 'US' AND age > 25       
     → Uses the index (equality on country + range on age)
     
  ❌ WHERE age = 30                          
     → CANNOT use the index! Age is the SECOND column.
     → Must scan all countries first. Like looking up a phone book
       by first name when it's sorted by last name.
     
  ❌ WHERE age > 25 AND country = 'US'       
     → Can use index (optimizer reorders: country = 'US' AND age > 25)
     → Actually ✅ — modern optimizers are smart enough!
     
  ⚠️ WHERE country = 'US' OR age = 30        
     → Cannot efficiently use composite index for OR conditions
     → May need two separate indexes


THE LEFTMOST PREFIX RULE:

  Index on (A, B, C) can be used for:
    ✅ WHERE A = ?                    (uses A)
    ✅ WHERE A = ? AND B = ?          (uses A, B)
    ✅ WHERE A = ? AND B = ? AND C = ? (uses A, B, C — all 3!)
    ✅ WHERE A = ? AND B > ?          (uses A, B with range)
    ❌ WHERE B = ?                    (skips A — can't use index)
    ❌ WHERE C = ?                    (skips A and B — can't use index)
    ❌ WHERE B = ? AND C = ?          (skips A — can't use index)
    
  Think of it as a phone book:
    Sorted by (Last Name, First Name, City)
    ✅ Find all people named "Smith" → easy
    ✅ Find "John Smith" → easy  
    ❌ Find all people named "John" (any last name) → must scan everything
    

COLUMN ORDER STRATEGY:

  Put the most SELECTIVE (high cardinality) column first?
  → Not always. Put the column most often used in equality conditions first.
  
  For (country, age):
    - country first: good for "all users in US aged 20-30"
    - age first: good for "all 25-year-olds in any country"
    - Choose based on your MOST COMMON query pattern
```

---

## 5. Dense vs Sparse Indexes

```
DENSE INDEX:
  Has an entry for EVERY row in the table.
  
  Row 1: id=1 → offset 0
  Row 2: id=2 → offset 100
  Row 3: id=3 → offset 200
  Row 4: id=4 → offset 300
  ... one entry per row
  
  Pros: Can find any row directly
  Cons: Large index (one entry per row)
  
  Used by: Secondary indexes (they must index every row)


SPARSE INDEX:
  Has an entry for every DATA BLOCK (page), not every row.
  Only works if data is SORTED by the indexed column.
  
  Block 1 (rows 1-100):   first key = 1   → offset 0
  Block 2 (rows 101-200): first key = 101 → offset 4096
  Block 3 (rows 201-300): first key = 201 → offset 8192
  
  To find id=150:
    → Sparse index: 101 ≤ 150 < 201 → Block 2
    → Read Block 2 → scan within block for id=150
    
  Pros: Much smaller index (one entry per block, not per row)
  Cons: Only works on sorted data; requires scanning within a block
  
  Used by: SSTable indexes (in LSM trees), clustered indexes


REAL-WORLD USAGE:

  MySQL clustered index: sort of "sparse" — internal nodes 
    route to the right leaf page, which contains multiple rows.
    
  SSTable index: sparse — maps first key of each data block 
    to its offset. Very compact.
    
  Secondary indexes: dense — must have an entry for every indexed row
    because the data isn't physically sorted by this column.
```

---

## 6. Inverted Index — How Full-Text Search Works

```
An INVERTED INDEX is the data structure behind full-text search engines
like Elasticsearch, Apache Solr, and PostgreSQL's tsvector.

It maps WORDS → list of documents containing that word.
The opposite (inverse) of a normal index (document → words).


EXAMPLE:

  Documents:
    Doc 1: "Redis is a fast in-memory database"
    Doc 2: "Cassandra is a distributed database"
    Doc 3: "Redis and Cassandra are both fast"

  Normal index (forward):
    Doc 1 → ["Redis", "is", "a", "fast", "in-memory", "database"]
    Doc 2 → ["Cassandra", "is", "a", "distributed", "database"]
    Doc 3 → ["Redis", "and", "Cassandra", "are", "both", "fast"]

  INVERTED INDEX:
    "redis"       → [Doc 1, Doc 3]
    "cassandra"   → [Doc 2, Doc 3]
    "fast"        → [Doc 1, Doc 3]
    "database"    → [Doc 1, Doc 2]
    "distributed" → [Doc 2]
    "in-memory"   → [Doc 1]
    "both"        → [Doc 3]
    ...

  Search for "fast database":
    "fast"     → [Doc 1, Doc 3]
    "database" → [Doc 1, Doc 2]
    Intersection: [Doc 1]  ← appears in both!
    
    Return Doc 1: "Redis is a fast in-memory database" ✓


HOW IT'S BUILT:

  Step 1: TOKENIZATION — split text into words
    "Redis is a fast in-memory database"
    → ["redis", "is", "a", "fast", "in-memory", "database"]
    
  Step 2: NORMALIZATION — lowercase, stemming, stop-word removal
    → Remove stop words: "is", "a"
    → Lowercase: "Redis" → "redis"
    → Stemming: "running" → "run", "databases" → "database"
    → Result: ["redis", "fast", "in-memory", "database"]
    
  Step 3: Build inverted index
    For each word, record which documents contain it.
    Store as: word → sorted list of (doc_id, position, frequency)

  Step 4: Store on disk
    Inverted index is typically stored using a structure similar to 
    SSTables: sorted by term, with posting lists for each term.


POSTING LIST (detailed entry):

  "redis" → [
    {doc: 1, positions: [0], frequency: 1},    ← "Redis" is word 0 in doc 1
    {doc: 3, positions: [0], frequency: 1}     ← "Redis" is word 0 in doc 3
  ]
  
  Positions enable PHRASE queries:
    "fast database" → 
    "fast" in Doc 1 at position 3
    "database" in Doc 1 at position 5
    Not adjacent → NOT a phrase match (unless you use proximity)


TF-IDF SCORING (how search results are ranked):

  TF (Term Frequency): How often does the word appear in THIS document?
    More occurrences → higher score
    
  IDF (Inverse Document Frequency): How rare is this word across ALL docs?
    Rare words → higher score (they're more distinctive)
    Common words like "the" → low score
    
  Score = TF × IDF
  
  Search "distributed database":
    Doc 2: has both "distributed" (rare word!) and "database"
    Doc 1: has "database" but not "distributed"
    → Doc 2 scores higher → ranked first in results


REAL-WORLD USAGE:

  Elasticsearch: Inverted index built on Apache Lucene.
    Each "shard" is a Lucene index (which is really a set of 
    immutable segment files, similar to SSTables!)
    
  PostgreSQL: Built-in full-text search with tsvector/tsquery.
    Uses GIN (Generalized Inverted Index) internally.
    Good enough for simple search; Elasticsearch for heavy use.
```

---

## 7. Geospatial Indexes — How "Find Near Me" Works

```
"Find all restaurants within 5 km of my location"

This is a 2D range query — regular B-tree indexes can't handle it
efficiently because B-trees sort data in ONE dimension only.


THE PROBLEM:

  You have: latitude = 37.7749, longitude = -122.4194 (San Francisco)
  You want: all points within 5 km
  
  B-tree on latitude: can find all points with lat 37.73 to 37.82
  B-tree on longitude: can find all points with lon -122.46 to -122.37
  
  But combining them is inefficient:
    → Get all matching latitudes (maybe 100,000 points)
    → Get all matching longitudes (maybe 100,000 points)  
    → Intersect the two sets → this is slow!
    
  We need an index that handles BOTH dimensions together.


SOLUTION 1: GEOHASH

  Convert 2D coordinates into a 1D string that preserves locality.
  
  Geohash divides the world into a grid of cells, each with a string code.
  Nearby points share a common PREFIX.
  
  San Francisco: lat 37.7749, lon -122.4194 → geohash "9q8yy"
  Nearby point:  lat 37.7750, lon -122.4190 → geohash "9q8yy"  ← same prefix!
  Far point:     lat 40.7128, lon  -74.0060 → geohash "dr5ru"  ← different prefix
  
  Now we can use a regular B-tree index on the geohash string!
  
  "Find all points near 9q8yy":
    → WHERE geohash LIKE '9q8y%'  (prefix match)
    → B-tree range scan on the geohash column
    → Returns all points in that grid cell and neighbors
    
  Simple, works with any database that supports string indexes!
  Used by: Redis (GEOADD/GEOSEARCH), Elasticsearch, MongoDB


SOLUTION 2: R-TREE (Rectangle Tree)

  A tree structure where each node represents a BOUNDING RECTANGLE.
  
                   [World]
                  /       \
        [North America]  [Europe]
         /          \
    [California]  [New York]
      /      \
  [SF area] [LA area]
    / | \
  [restaurant1] [restaurant2] [restaurant3]
  
  "Find points within 5km of (37.77, -122.42)":
    1. Start at root → which child rectangle contains our area?
    2. Go to [North America] → [California] → [SF area]
    3. Check each point in [SF area]: is it within 5km?
    4. Return matching points.
    
  Instead of checking millions of points, we only check ~100 in the local area.
  
  Used by: PostGIS (PostgreSQL extension), MySQL spatial indexes


SOLUTION 3: QUADTREE / KD-TREE

  Recursively divide space into 4 quadrants (quadtree) or 
  along alternating dimensions (KD-tree).
  
  Quadtree:
    ┌─────────┬─────────┐
    │         │         │
    │   NW    │   NE    │
    │         │    ●    │  ← point here
    ├─────────┼─────────┤
    │         │  ●      │
    │   SW    │   SE    │  ← point here
    │         │         │
    └─────────┴─────────┘
    
  Each quadrant is recursively divided until each cell has few points.
  Query: start at root, only recurse into quadrants that overlap your search area.
  
  Used by: In-memory geospatial systems, game engines


WHICH TO USE?

  Geohash + B-tree: Simplest. Works with any DB. Good enough for most cases.
  R-tree (PostGIS):  Most powerful. Complex queries (polygons, intersections).
  Quadtree:          In-memory use. Very fast for dynamic point data.
  
  For system design interviews: mention geohash (simple) and note that
  PostGIS/R-tree exists for complex spatial queries.
```

---

## 8. Covering Indexes and Index-Only Scans

```
A COVERING INDEX contains ALL the columns needed by a query,
so the database never needs to read the actual table.

Example:
  Query: SELECT email, age FROM users WHERE country = 'US'
  
  Index: CREATE INDEX idx_cover ON users(country, email, age);
  
  This index contains country, email, AND age.
  The query only needs country, email, and age.
  → The index COVERS the query → no table access needed!
  
  Without covering index:
    1. Walk index → find rows where country='US' → get row pointers
    2. For EACH row pointer → read the actual row from table → get email, age
    → Many random I/O reads to the table!
    
  With covering index:
    1. Walk index → find entries where country='US'
    2. Email and age are IN the index → return directly
    → ZERO table reads! Index-only scan!
    
  Performance difference: 10-100x faster for large result sets.


WHEN TO USE:

  ✅ Queries that return a few specific columns frequently
  ✅ "Hot" queries that run thousands of times per second
  ✅ Dashboard queries (SELECT count, sum of specific columns)
  
  ❌ Queries that need SELECT * (all columns) — can't cover all columns
  ❌ Tables with frequent writes (more indexes = slower writes)
  
  
POSTGRESQL INCLUDE SYNTAX:

  CREATE INDEX idx_cover ON users(country) INCLUDE (email, age);
  
  "country" is the search key (used for WHERE/ORDER BY).
  "email" and "age" are included just for covering (not searchable).
  → More space-efficient than indexing all 3 columns.
```

---

## 9. The Cost of Indexes — Write Penalty

```
Every index has a WRITE COST. When you insert/update/delete a row,
EVERY index on that table must also be updated.

EXAMPLE: Table with 5 indexes

  INSERT INTO users (id, name, email, age, country) VALUES (...);
  
  Database must:
    1. Insert row into table (heap or clustered index)    → 1 write
    2. Insert into idx_email (secondary index)            → 1 write
    3. Insert into idx_age (secondary index)              → 1 write
    4. Insert into idx_country (secondary index)          → 1 write
    5. Insert into idx_name (secondary index)             → 1 write
    6. Insert into idx_country_age (composite index)      → 1 write
    
  Total: 6 writes instead of 1!
  
  Each index write involves:
    - Finding the right position in the B+ tree
    - Potentially splitting nodes
    - Writing to WAL
    
  5 indexes → writes are roughly 5-6x slower!


UPDATE is even worse:

  UPDATE users SET email = 'new@email.com' WHERE id = 42;
  
  Must:
    1. Update the row in the table
    2. REMOVE old email entry from idx_email → 1 write
    3. INSERT new email entry into idx_email → 1 write
    → 3 writes for updating 1 column!
    
  If the UPDATE touches a column that's in 3 indexes:
    → 1 table write + 3 remove + 3 insert = 7 writes!


GUIDELINES:

  ✅ Create indexes for columns that are frequently QUERIED
  ❌ Don't create indexes "just in case"
  ❌ Don't index columns that are frequently UPDATED
  
  Rule of thumb:
    Read-heavy table (95% reads, 5% writes): 5-10 indexes is fine
    Write-heavy table (50%+ writes): keep indexes to 2-3 max
    
  Monitor index usage:
    PostgreSQL: pg_stat_user_indexes → shows how often each index is used
    If an index is never used → DROP it! It's only costing write performance.


IN LSM-TREES (Cassandra):

  Indexes are LESS expensive to maintain because ALL writes are 
  sequential (append to memtable). No random I/O for index updates.
  
  But secondary indexes in Cassandra work differently:
    - Each node maintains a LOCAL secondary index
    - A query on secondary index may need to check ALL nodes (scatter-gather)
    - This is why Cassandra discourages secondary indexes for high-cardinality columns
    - Use MATERIALIZED VIEWS or denormalized tables instead
```

---

## 10. Index Selection in System Design Interviews

```
QUICK REFERENCE — Which index for which query pattern:


"Find user by ID" (point lookup, primary key):
  → Primary key / clustered index (automatic in all databases)
  → B+ tree, O(log n) = 2-3 disk reads


"Find user by email" (point lookup, non-PK):
  → Secondary B+ tree index on email column
  → CREATE INDEX idx_email ON users(email);
  → 3-5 disk reads


"Find all users aged 20-30" (range query):
  → B+ tree index on age column
  → Linked leaf scan for the range
  → CREATE INDEX idx_age ON users(age);


"Find all users in US aged 20-30" (multi-condition):
  → Composite index on (country, age)
  → Equality on country + range on age
  → CREATE INDEX idx_country_age ON users(country, age);


"Search for tweets containing 'distributed systems'" (full-text):
  → Inverted index
  → Elasticsearch / PostgreSQL tsvector
  → NOT a B-tree (B-tree can't search within text content)


"Find restaurants within 5 km" (geospatial):
  → Geohash + B-tree (simple)
  → R-tree / PostGIS (complex spatial queries)
  → Redis GEOADD/GEOSEARCH for simple proximity


"Get count of users by country" (analytics):
  → Covering index on (country) with COUNT
  → Or column store for heavy analytics (Redshift)


"Find latest 20 posts by user" (sorted + filtered):
  → Composite index on (user_id, created_at DESC)
  → B+ tree range scan starting from newest


"Autocomplete: find names starting with 'ali'" (prefix search):
  → B+ tree index on name column
  → WHERE name LIKE 'ali%'   → CAN use B-tree (prefix match)
  → WHERE name LIKE '%ali%'  → CANNOT use B-tree (need full-text index)


COMMON INTERVIEW PATTERN:

  Interviewer: "How would you make this query fast?"
  
  You: 
    1. Identify the query pattern (point lookup, range, search, geo)
    2. Propose the right index type
    3. Discuss the tradeoff (faster reads, slower writes)
    4. If high write volume → consider denormalization instead of more indexes
    5. If full-text search → Elasticsearch (inverted index)
    6. If geospatial → Geohash or PostGIS
```

---

### Key Takeaways

```
1. An INDEX is a separate sorted data structure that speeds up queries.
   Without indexes: full table scan = O(n). With indexes: O(log n).

2. PRIMARY INDEX (clustered): determines physical order on disk.
   One per table. Best for range queries on the primary key.

3. SECONDARY INDEX: separate B+ tree for non-PK columns.
   Can have many. Each adds write overhead.

4. COMPOSITE INDEX: multi-column index. Column order matters!
   Follow the leftmost prefix rule.

5. INVERTED INDEX: maps words → documents. Full-text search.
   Used by Elasticsearch, Solr, PostgreSQL tsvector.

6. GEOSPATIAL INDEX: 2D queries. Geohash, R-tree, Quadtree.
   Used by PostGIS, Redis GEO, MongoDB 2dsphere.

7. Every index has a WRITE COST. More indexes = slower writes.
   Only create indexes for columns that are frequently queried.
```

---

*Previous: [← Comparison & When to Use What](06-comparison-and-when-to-use-what.md) | Back to: [Fundamentals](01-fundamentals.md)*