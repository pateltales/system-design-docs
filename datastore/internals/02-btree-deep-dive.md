# Datastore Internals — Part 2: B-Tree Deep Dive

> This document builds up to B-trees step by step. We start with the simplest possible structure (a sorted array) and gradually add complexity until we arrive at the B-tree — the data structure that powers MySQL, PostgreSQL, SQLite, and most SQL databases.

---

## Table of Contents

1. [Starting Simple: The Sorted Array](#1-starting-simple-the-sorted-array)
2. [Binary Search Trees (BST)](#2-binary-search-trees-bst)
3. [The Problem With BSTs on Disk](#3-the-problem-with-bsts-on-disk)
4. [B-Tree: The Disk-Friendly Tree](#4-b-tree-the-disk-friendly-tree)
5. [B-Tree Structure — What a Node Looks Like](#5-b-tree-structure--what-a-node-looks-like)
6. [B-Tree SEARCH — Step by Step](#6-b-tree-search--step-by-step)
7. [B-Tree INSERT — Step by Step (with Node Splitting)](#7-b-tree-insert--step-by-step-with-node-splitting)
8. [B-Tree DELETE — Step by Step](#8-b-tree-delete--step-by-step)
9. [B-Tree on Disk — Pages and Buffer Pool](#9-b-tree-on-disk--pages-and-buffer-pool)
10. [B+ Tree — The Variant Databases Actually Use](#10-b-tree--the-variant-databases-actually-use)
11. [Write Amplification and the Cost of In-Place Updates](#11-write-amplification-and-the-cost-of-in-place-updates)
12. [B-Trees in Real Databases](#12-b-trees-in-real-databases)
13. [Strengths and Weaknesses Summary](#13-strengths-and-weaknesses-summary)

---

## 1. Starting Simple: The Sorted Array

The simplest way to store data so we can find it quickly: **keep it sorted**.

```
Sorted array of (key, value) pairs:

Index:  0        1        2        3        4        5        6
      ┌────────┬────────┬────────┬────────┬────────┬────────┬────────┐
      │ age:20 │ age:25 │ age:30 │ age:35 │ age:40 │ age:45 │ age:50 │
      │ Alice  │ Bob    │ Charlie│ Diana  │ Eve    │ Frank  │ Grace  │
      └────────┴────────┴────────┴────────┴────────┴────────┴────────┘

SEARCH for age:35:
  → Binary search: check middle (index 3) → found! O(log n) ✓

SEARCH for age:28:
  → Binary search: check 3 (35, too big) → check 1 (25, too small) 
    → check 2 (30, too big) → not found, would be between 1 and 2

Range query "all ages 25-40":
  → Binary search to find 25 → scan right until 40 → [Bob, Charlie, Diana, Eve] ✓
```

**Search is great! O(log n) with binary search. Range queries are natural.**

But what about inserting?

```
INSERT age:32, "Hank":

  Step 1: Find where 32 goes: between index 2 (30) and index 3 (35)
  
  Step 2: SHIFT everything from index 3 onward to the right:
  
  BEFORE:  [20, 25, 30, 35, 40, 45, 50]
                       ↑ insert here
  
  SHIFT:   [20, 25, 30, __, 35, 40, 45, 50]
                       ↑
  INSERT:  [20, 25, 30, 32, 35, 40, 45, 50]
  
  We had to MOVE 4 elements (35, 40, 45, 50) to make room!
  
  With 1 million elements, inserting near the beginning means
  moving ~500,000 elements. That's O(n) per insert. TERRIBLE.
```

**Problem: Inserts are O(n) because we have to shift data.**

On disk, this is even worse — shifting means REWRITING huge portions of the file.

---

## 2. Binary Search Trees (BST)

To fix the insert problem, we use a **tree** instead of an array.

```
A Binary Search Tree (BST) for our data:

                    35 (Diana)
                   /          \
              25 (Bob)      45 (Frank)
             /      \       /       \
        20 (Alice) 30(Charlie) 40(Eve) 50(Grace)

RULE: For every node:
  - All keys in the LEFT subtree are SMALLER
  - All keys in the RIGHT subtree are LARGER

SEARCH for 30:
  35 → 30 < 35, go LEFT
  25 → 30 > 25, go RIGHT
  30 → FOUND! ✓
  
  Steps: 3 (which is log₂(7) ≈ 2.8) → O(log n) ✓

INSERT 32:
  35 → 32 < 35, go LEFT
  25 → 32 > 25, go RIGHT
  30 → 32 > 30, go RIGHT → empty spot! Insert here.
  
                    35
                   /    \
              25        45
             /    \     /   \
           20    30   40    50
                   \
                   32  ← NEW!
  
  Steps: 3 + 1 = O(log n) ✓  No shifting needed!
```

**BSTs fix the insert problem: O(log n) for both search AND insert!**

But there's a catch...

```
PROBLEM: If we insert keys in sorted order (20, 25, 30, 35, 40, 45, 50):

  20
    \
    25
      \
      30
        \
        35
          \
          40
            \
            45
              \
              50

This is just a linked list! Search is O(n), not O(log n).
This is called a "degenerate tree" or "unbalanced tree."
```

**Solution: Self-balancing trees** (AVL tree, Red-Black tree) that automatically rebalance after inserts. These guarantee O(log n) height.

**But even balanced BSTs have a problem for databases...**

---

## 3. The Problem With BSTs on Disk

BSTs work great in memory. But databases store data on disk, and BSTs are TERRIBLE for disk access. Here's why:

```
A balanced BST with 1 million keys has height ≈ 20 (log₂(1,000,000) ≈ 20).

To search for a key, we traverse 20 nodes.

In MEMORY:
  Each node access: ~100 nanoseconds
  Total: 20 × 100 ns = 2,000 ns = 0.002 ms
  → Blazing fast ✓

On DISK (each node is a separate disk read):
  Each node access: ~0.1 ms (SSD) or ~10 ms (HDD)
  Total: 20 × 0.1 ms = 2 ms (SSD)
  Total: 20 × 10 ms  = 200 ms (HDD)
  → SLOW. 200ms for a single search on HDD!

WHY is each node a separate disk read?
  - BST nodes are SMALL (one key + two pointers ≈ 50 bytes)
  - Disk reads in PAGES (4 KB minimum)
  - So each disk read fetches 4 KB but only uses 50 bytes → 99% wasted!
  - AND the 20 nodes are likely on 20 DIFFERENT pages (random I/O)

              Node 1 (page 42)
             /                \
     Node 2 (page 7891)     Node 3 (page 203)
      /         \               /        \
  Node 4        Node 5      Node 6      Node 7
  (page 5002)  (page 108)  (page 9923) (page 41)

  Each arrow = a RANDOM disk seek to a different page. 20 seeks!
```

**The core problem: BSTs are TALL and NARROW. Each level = one disk read. Too many levels = too many disk reads.**

---

## 4. B-Tree: The Disk-Friendly Tree

**The key insight: make the tree SHORT and WIDE.**

```
BST: Each node has 1 key and 2 children → tree is TALL
     Height for 1M keys: ~20 levels → 20 disk reads

B-Tree: Each node has HUNDREDS of keys and children → tree is SHORT
        Height for 1M keys: ~3-4 levels → 3-4 disk reads!

WHY does this help?
  - Disk reads in 4 KB pages anyway
  - A B-tree node is sized to be EXACTLY ONE PAGE (4 KB)
  - One 4 KB page can hold ~200 keys + 201 child pointers
  - So EACH disk read gives us 200 keys to compare (not just 1!)

BST with 1M keys:            B-Tree with 1M keys:
  Height: 20                    Height: 3
  Disk reads per search: 20     Disk reads per search: 3
  Time (SSD): 2ms               Time (SSD): 0.3ms
  Time (HDD): 200ms             Time (HDD): 30ms

That's a 7x improvement! And the root node is always cached in RAM,
so it's really just 2 disk reads in practice.
```

### The B-Tree Idea in Plain English

```
Think of it like a phone book.

BST approach: 
  One name per page. To find "Smith", flip through 20 pages
  one at a time.

B-Tree approach:
  The phone book has an INDEX at the front:
  Page 1 (INDEX): "A-D: see page 10", "E-H: see page 20", 
                   "I-L: see page 30", ...
  
  Page 30 (INDEX): "I-Ja: page 31", "Jb-K: page 32", 
                    "L: page 33"
  
  Page 33 (DATA): Actual entries for names starting with L.
  
  To find "Lee": 
    1. Check page 1 → "L" is in "I-L" → go to page 30
    2. Check page 30 → "Lee" is in "L" → go to page 33
    3. Check page 33 → found "Lee"!
  
  3 page reads, not 20!
```

---

## 5. B-Tree Structure — What a Node Looks Like

```
A B-tree of ORDER m means each node can have UP TO m children
and UP TO m-1 keys.

For our examples, let's use ORDER 4 (a small B-tree for illustration).
Real databases use ORDER ~200-500 (one 4KB page worth of keys).


A B-TREE NODE (order 4):
┌──────────────────────────────────────────┐
│         B-Tree Node (Page)               │
│                                          │
│  Keys:     [ K1 | K2 | K3 ]             │
│  Children: [P0 | P1 | P2 | P3]          │
│                                          │
│  Rules:                                  │
│  - Keys are SORTED: K1 < K2 < K3        │
│  - Child P0 → all keys < K1             │
│  - Child P1 → all keys between K1 & K2  │
│  - Child P2 → all keys between K2 & K3  │
│  - Child P3 → all keys > K3             │
│                                          │
│  Max keys per node: 3 (= m-1 for m=4)   │
│  Min keys per node: 1 (= ⌈m/2⌉-1)       │
│  (root can have 0 keys if tree is empty) │
│                                          │
└──────────────────────────────────────────┘

Example B-Tree of order 4:

                       [30]                          ← ROOT (1 key)
                      /    \
              [10 | 20]    [40 | 50]                 ← INTERNAL nodes
              / |  \        / |  \
           [5] [15] [25] [35] [45] [55|60]           ← LEAF nodes

Reading the tree:
  - Root has key 30
  - Left child has keys 10, 20 (all < 30)
  - Right child has keys 40, 50 (all > 30)
  - Leaf [5]: all keys < 10
  - Leaf [15]: keys between 10 and 20
  - Leaf [25]: keys between 20 and 30
  - Leaf [35]: keys between 30 and 40
  - Leaf [45]: keys between 40 and 50
  - Leaf [55,60]: keys > 50
```

---

## 6. B-Tree SEARCH — Step by Step

```
Search for key 45 in this B-Tree (order 4):

                       [30]                         
                      /    \
              [10 | 20]    [40 | 50]                
              / |  \        / |  \
           [5] [15] [25] [35] [45] [55|60]          


Step 1: Start at ROOT [30]
        Is 45 in this node? No.
        45 > 30 → go to RIGHT child
        DISK READ #1 ✓

Step 2: At node [40 | 50]
        Is 45 in this node? No.
        40 < 45 < 50 → go to MIDDLE child
        DISK READ #2 ✓

Step 3: At leaf [45]
        Is 45 in this node? YES! Found it!
        DISK READ #3 ✓

Total disk reads: 3 (= height of the tree)


Search for key 22 (not in the tree):

Step 1: ROOT [30] → 22 < 30 → go LEFT
Step 2: Node [10 | 20] → 22 > 20 → go to rightmost child
Step 3: Leaf [25] → 22 not found in [25]
        → Key 22 does NOT exist.

Total disk reads: 3 (same — we always go root to leaf)
```

### Search with Real Database Sizes

```
Real B-tree (order 500, typical for 4KB pages):

Level 0 (root):     1 node,      ~499 keys
Level 1:            ~500 nodes,   ~250,000 keys
Level 2:            ~250,000 nodes, ~125,000,000 keys
Level 3:            ~125M nodes,   ~62.5 BILLION keys

So a 3-level B-tree can hold 62.5 BILLION keys!
And we need at most 3 disk reads to find any of them.

In practice:
  - Level 0 (root) is ALWAYS in RAM (cached) → 0 disk reads
  - Level 1 is often in RAM too → 0 disk reads
  - Level 2-3 → 1-2 disk reads

So even with billions of keys, a search = 1-2 disk reads = ~0.1-0.2ms on SSD.
This is why B-trees are so good for reads!
```

---

## 7. B-Tree INSERT — Step by Step (with Node Splitting)

### Case 1: Simple Insert (Node Has Room)

```
Insert key 42 into this B-tree (order 4, max 3 keys per node):

BEFORE:
                       [30]                         
                      /    \
              [10 | 20]    [40 | 50]                
              / |  \        / |  \
           [5] [15] [25] [35] [45] [55|60]          

Step 1: Search for where 42 should go
        ROOT [30] → 42 > 30 → go RIGHT
        Node [40 | 50] → 40 < 42 < 50 → go to MIDDLE child
        Leaf [45] → 42 goes here

Step 2: Insert 42 into leaf [45]
        [45] → [42 | 45]  (still has room — max 3 keys, we have 2)
        ✓ DONE!

AFTER:
                       [30]                         
                      /    \
              [10 | 20]    [40 | 50]                
              / |  \        / |   \
           [5] [15] [25] [35] [42|45] [55|60]       

Simple! Just insert into the leaf node. One disk write.
```

### Case 2: Node Splitting (Node Is Full)

This is the interesting case — and the key mechanism that keeps B-trees balanced.

```
Insert key 57 into this B-tree:

BEFORE:
                       [30]                         
                      /    \
              [10 | 20]    [40 | 50]                
              / |  \        / |   \
           [5] [15] [25] [35] [45] [55|58|60]  ← FULL! (3 keys = max)

Step 1: Search for where 57 should go
        ROOT [30] → 57 > 30 → RIGHT
        Node [40 | 50] → 57 > 50 → rightmost child
        Leaf [55 | 58 | 60] → 57 goes here
        
        BUT the leaf is FULL (3 keys already, max is 3)!

Step 2: SPLIT the leaf node
        Current: [55 | 57 | 58 | 60]  (4 keys — too many!)
        
        Split in half:
          Left half:  [55 | 57]
          MIDDLE key: 58         ← this goes UP to the parent
          Right half: [60]
        
        ┌────────┐    58 goes up    ┌────────┐
        │ 55 | 57│ ───────────────▶ │   60   │
        └────────┘                  └────────┘

Step 3: Push the middle key (58) up to the PARENT
        Parent was: [40 | 50]
        Now becomes: [40 | 50 | 58]  (3 keys — still fits!)
        
        Parent now has 4 children:
        [35] [45] [55|57] [60]

AFTER:
                       [30]                         
                      /    \
              [10 | 20]    [40 | 50 | 58]           
              / |  \        / |   |    \
           [5] [15] [25] [35] [45] [55|57] [60]    

The tree is still balanced! Every leaf is at the same depth.
```

### Case 3: Splitting Propagates Upward

```
What if the PARENT is also full when we try to push a key up?
Then we split the parent too! And push a key up to ITS parent.
In the worst case, the split propagates all the way to the root,
and the ROOT splits — creating a NEW root and making the tree 1 level taller.

This is the ONLY way a B-tree gets taller: when the root splits.
This guarantees all leaves are always at the same depth (balanced!).

Example: Split propagates to root

BEFORE (root is full):
                    [20 | 40 | 60]            ← ROOT (full! 3 keys)
                   / |       |     \
              [10]  [30]   [50]   [70|80]

Insert 75: goes to [70|80] → becomes [70|75|80] (full) → SPLIT!
  Left: [70|75], Middle: 80, Right: (empty, but that's ok)
  
  Push 80 up to root: [20 | 40 | 60 | 80] → ROOT IS FULL! SPLIT ROOT!
  
  Left child:  [20]      Middle key: 40      Right child: [60 | 80]
  
  New root: [40]
  
AFTER:
                          [40]                ← NEW ROOT
                        /      \
                   [20]         [60 | 80]     ← internal nodes
                  / |   \      / |      \
              [10] [30]  ...  [50] [70|75] (empty)

The tree grew from height 2 to height 3.
This is rare — it only happens when the root splits.
For a tree with 1 billion keys, the root splits maybe 4-5 times ever.
```

---

## 8. B-Tree DELETE — Step by Step

Deletion is more complex but the idea is: remove the key, and if a node becomes too empty (below minimum keys), borrow from a sibling or merge nodes.

```
Delete key 45 from this B-tree (order 4, min 1 key per node):

BEFORE:
                       [30]                         
                      /    \
              [10 | 20]    [40 | 50]                
              / |  \        / |   \
           [5] [15] [25] [35] [45] [55|60]

Step 1: Find key 45
        ROOT → RIGHT → MIDDLE child → leaf [45]

Step 2: Remove 45 from leaf
        [45] → [] (empty!)
        
        PROBLEM: Node has 0 keys (below minimum of 1)!

Step 3: Try to BORROW from a sibling
        Left sibling: [35] — has only 1 key (minimum), can't borrow
        Right sibling: [55 | 60] — has 2 keys (> minimum), CAN borrow!
        
        Borrow process (rotate through parent):
        1. Take parent's separator key (50) and put it in our node
        2. Take sibling's first key (55) and put it in parent's place
        
AFTER:
                       [30]                         
                      /    \
              [10 | 20]    [40 | 55]                
              / |  \        / |   \
           [5] [15] [25] [35] [50] [60]

Balanced! Every leaf still at same depth. ✓


If borrowing isn't possible (both siblings at minimum), we MERGE:

  Merge our empty node + parent separator + sibling into one node.
  This may cause the parent to underflow, propagating upward.
  If the root becomes empty → tree shrinks by one level.
  (Mirror of root splitting during insert.)
```

---

## 9. B-Tree on Disk — Pages and Buffer Pool

Now let's connect B-trees to actual disk storage:

```
HOW B-TREES LIVE ON DISK
═════════════════════════

A B-tree file on disk is divided into fixed-size PAGES (typically 4 KB or 16 KB).
Each B-tree node = exactly one page.

┌─────────────────────────────────────────────────────────────┐
│  Database file on disk: btree.dat                            │
│                                                              │
│  ┌──────────┬──────────┬──────────┬──────────┬────────────┐ │
│  │  Page 0  │  Page 1  │  Page 2  │  Page 3  │  Page 4... │ │
│  │  (Root)  │ (Internal│ (Internal│  (Leaf)  │  (Leaf)    │ │
│  │          │  node)   │  node)   │          │            │ │
│  └──────────┴──────────┴──────────┴──────────┴────────────┘ │
│                                                              │
│  Each page (4 KB) contains:                                  │
│  ┌──────────────────────────────────────────────────────┐   │
│  │ Page Header (24 bytes):                               │   │
│  │   - Page number                                       │   │
│  │   - Page type (root/internal/leaf)                    │   │
│  │   - Number of keys                                    │   │
│  │   - Free space offset                                 │   │
│  │                                                       │   │
│  │ Keys + Values (or child pointers):                    │   │
│  │   - Key 1 | Value 1 (or child page number)           │   │
│  │   - Key 2 | Value 2 (or child page number)           │   │
│  │   - ...                                               │   │
│  │   - Key N | Value N                                   │   │
│  │                                                       │   │
│  │ Free space (unused portion of the page)               │   │
│  └──────────────────────────────────────────────────────┘   │
│                                                              │
└─────────────────────────────────────────────────────────────┘

"Child pointers" in a B-tree are really PAGE NUMBERS.

  Node [30] with children:
    - Left child pointer: page_number = 5
    - Right child pointer: page_number = 12
    
  To follow the left child: read page 5 from disk.
```

### The Buffer Pool (Page Cache)

```
Databases don't read from disk every time. They maintain a BUFFER POOL
in RAM that caches recently-used pages.

┌───────────────────────────────────────────────────────┐
│  RAM: Buffer Pool (e.g., 8 GB of RAM)                  │
│                                                        │
│  ┌────────┐ ┌────────┐ ┌────────┐ ┌────────┐        │
│  │ Page 0 │ │ Page 1 │ │ Page 3 │ │ Page 7 │  ...   │
│  │ (root) │ │ (hot)  │ │ (hot)  │ │ (warm) │        │
│  │ ★ dirty│ │        │ │        │ │        │        │
│  └────────┘ └────────┘ └────────┘ └────────┘        │
│                                                        │
│  Page 0 is "dirty" — modified in memory but not yet    │
│  written back to disk. It will be flushed eventually.  │
│                                                        │
│  When the buffer pool is full and we need a new page:  │
│  → Evict the LEAST RECENTLY USED page (LRU policy)    │
│  → If evicted page is dirty → write it to disk first   │
│                                                        │
└───────────────────────────────────────────────────────┘

Read path:
  1. Need page 42
  2. Is page 42 in the buffer pool? 
     YES → return it (0 disk I/O!) ← cache hit
     NO  → read page 42 from disk, put it in buffer pool, return it

For a frequently-accessed B-tree:
  - Root and top levels are ALWAYS in the buffer pool
  - Hot leaf pages are often in the buffer pool
  - Only "cold" reads go to disk
  
  Result: Most B-tree reads need 0-1 disk I/O, not 3-4.
```

---

## 10. B+ Tree — The Variant Databases Actually Use

Almost every real database uses a **B+ tree**, not a plain B-tree. The difference is small but important:

```
PLAIN B-TREE:
  - Keys AND values stored in BOTH internal nodes and leaf nodes
  - Internal node: [Key1+Value1 | Key2+Value2 | ...]
  
B+ TREE:
  - Values stored ONLY in LEAF nodes
  - Internal nodes ONLY have keys (used for routing/navigation)
  - Leaf nodes are LINKED together (doubly-linked list)

Why B+ tree is better:

1. Internal nodes are SMALLER (no values, just keys)
   → More keys fit per page → tree is SHORTER → fewer disk reads
   
   B-tree internal node (4 KB page):
     Key (8 bytes) + Value (100 bytes) + pointer (8 bytes) = 116 bytes per entry
     4096 / 116 ≈ 35 entries per node
     
   B+ tree internal node (4 KB page):
     Key (8 bytes) + pointer (8 bytes) = 16 bytes per entry
     4096 / 16 ≈ 256 entries per node
     
   256 vs 35 → B+ tree is 7x wider → 1-2 fewer levels!

2. Range queries are FAST (linked leaf nodes)
   "Find all keys between 20 and 50":
   → Find leaf with key 20 → follow the linked list → stop at 50
   → Sequential scan! No need to go back up the tree.

3. All values at the leaf level → uniform access pattern
   → Every lookup traverses the same number of levels
   → Predictable performance


B+ TREE STRUCTURE:

                      [30 | 60]                      ← Internal (keys only)
                     /    |    \
              [10|20]   [40|50]   [70|80]             ← Internal (keys only)
              / | \      / | \     / | \
            ┌───┬───┬───┬───┬───┬───┬───┬───┐
            │ 5 │10 │15 │20 │25 │30 │35 │40 │...     ← Leaf nodes (keys + values)
            └───┴───┴───┴───┴───┴───┴───┴───┘
             ←→  ←→  ←→  ←→  ←→  ←→  ←→  ←→          ← Linked list!
             
  Range query [15, 35]:
    1. Search for 15 → land on leaf containing 15
    2. Scan right through linked list: 15 → 20 → 25 → 30 → 35
    3. Stop. Return [15, 20, 25, 30, 35]. 
    4. Only touched 2-3 leaf pages. Super efficient!
```

**From here on, when we say "B-tree" we mean "B+ tree" — that's what everyone in the database world means.**

---

## 11. Write Amplification and the Cost of In-Place Updates

Here's the fundamental weakness of B-trees:

```
WHAT HAPPENS ON A WRITE (INSERT OR UPDATE):

  Step 1: Search for the right leaf page → 2-3 disk READS
  Step 2: Modify the page in the buffer pool (in memory)
  Step 3: Write the modified page to WAL → 1 disk WRITE (sequential)
  Step 4: Eventually write the modified page back to disk → 1 disk WRITE (random!)

  Total per write: 2-3 reads + 2 writes

  The RANDOM WRITE in step 4 is the expensive part.
  Remember: random writes are the slowest disk operation.


WRITE AMPLIFICATION:
  
  To write 100 bytes of user data, we actually write:
  1. WAL entry: ~200 bytes (includes metadata)
  2. Full 4 KB page (even if we only changed 100 bytes out of 4096)
  
  Write amplification = Total bytes written / User bytes written
                      = (200 + 4096) / 100 = 43x
  
  We write 43 TIMES more data than the user asked for!
  
  (B-trees have ~5-10x write amplification in practice,
   because not every write touches a full page.)


WHY THIS MATTERS:

  At 100,000 writes/sec × 4 KB per page = 400 MB/sec of random disk I/O
  That's pushing the limits of even a fast NVMe SSD.
  
  For comparison, an LSM-tree (next chapter) converts all writes to
  SEQUENTIAL I/O → can handle 100K+ writes/sec with room to spare.
  
  This is THE main reason write-heavy systems choose LSM over B-tree.
```

### When B-Tree Writes Are Actually Fine

```
B-tree writes are not always bad:

1. Buffer pool absorbs most writes
   If the same page is modified 100 times before being flushed,
   → 100 writes to memory, only 1 write to disk
   → Effective write amplification drops dramatically for hot pages

2. SSDs handle random writes much better than HDDs
   SSD random write: ~0.1ms (vs HDD: ~10ms)
   Modern SSDs: 100K+ random writes/sec with NVMe

3. Small-to-medium write loads (< 10K writes/sec) are fine
   A single PostgreSQL instance handles 10K writes/sec easily

Bottom line:
  - Low to moderate writes → B-tree is fine (and reads are faster)
  - Very high writes (100K+/sec) → LSM-tree is better
```

---

## 12. B-Trees in Real Databases

```
┌──────────────────┬─────────────────────────────────────────────────┐
│ Database          │ How it uses B-trees                              │
├──────────────────┼─────────────────────────────────────────────────┤
│ MySQL (InnoDB)   │ B+ tree for both PRIMARY KEY and secondary       │
│                  │ indexes. Data is stored IN the primary key tree  │
│                  │ (clustered index). Page size: 16 KB.             │
│                  │                                                   │
│ PostgreSQL       │ B+ tree for all indexes. Data stored separately  │
│                  │ in a "heap" file. Page size: 8 KB.               │
│                  │                                                   │
│ SQLite           │ B+ tree for tables (each table is a B-tree).     │
│                  │ Page size: 4 KB (default). The whole DB is one   │
│                  │ file!                                             │
│                  │                                                   │
│ MongoDB          │ WiredTiger engine uses B+ trees (and optionally  │
│ (WiredTiger)     │ LSM trees). Page size: varies.                   │
│                  │                                                   │
│ SQL Server       │ B+ tree for clustered and non-clustered indexes. │
│                  │ Page size: 8 KB.                                  │
│                  │                                                   │
│ Oracle           │ B+ tree (called "B*tree" — a variant with higher │
│                  │ fill factor). Page size: 8 KB.                    │
└──────────────────┴─────────────────────────────────────────────────┘

Interesting fact: Despite being invented in 1970, B-trees are STILL
the default storage structure for most databases. That's over 50 years
of dominance. They're really, really good for reads.
```

---

## 13. Strengths and Weaknesses Summary

```
B-TREE STRENGTHS:
═════════════════
  ✅ Fast reads: O(log n) with very large branching factor
     → 2-3 disk reads for billions of keys
  
  ✅ Fast range queries: linked leaf nodes → sequential scan
     → "All users aged 20-30" is very efficient
  
  ✅ Predictable performance: every operation is O(log n)
     → No background compaction surprises (unlike LSM)
  
  ✅ Good space efficiency: no duplicate data, in-place updates
     → Uses close to the actual data size (no temporary copies)
  
  ✅ Strong transaction support: page-level locking, MVCC
     → ACID transactions are natural on B-trees
  
  ✅ Mature: 50+ years of optimization, well-understood


B-TREE WEAKNESSES:
══════════════════
  ❌ Random I/O on writes: each write touches a random page
     → Slow on HDD, acceptable on SSD, but still the bottleneck
  
  ❌ Write amplification: write a full page for a small change
     → Can be 5-40x more data written than the user's data
  
  ❌ Page splits during inserts: may need to write 2-3 pages
     → Causes brief latency spikes
  
  ❌ Fragmentation over time: as pages split, data gets scattered
     → Periodic VACUUM/OPTIMIZE needed (PostgreSQL, MySQL)
  
  ❌ Concurrency overhead: page-level locks can cause contention
     → High-concurrency writes may bottleneck on hot pages


WHEN TO USE B-TREES:
  → Read-heavy workloads (10:1 read-to-write ratio or higher)
  → Need range queries and sorting
  → Need ACID transactions
  → Dataset fits on SSD (random I/O is tolerable)
  → Moderate write load (< 10K writes/sec per instance)

WHEN NOT TO USE B-TREES:
  → Write-heavy workloads (> 50K writes/sec)
  → Append-only workloads (time-series, logs)
  → Don't need range queries (point lookups only)
  → Data is too large for SSD (HDD random I/O is terrible)
  → In these cases: use LSM-tree instead (next chapter!)
```

---

*Previous: [← Fundamentals](01-fundamentals.md) | Next: [LSM-Tree Deep Dive →](03-lsm-tree-deep-dive.md)*