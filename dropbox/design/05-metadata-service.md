# Deep Dive: Metadata Service & Namespace Management (Edgestore)

> **Context:** The metadata service is the "brain" of Dropbox — it knows where every file is, who owns it, what version it's at, and what blocks compose it. Dropbox's metadata system is called **Edgestore**, a MySQL-based graph store handling trillions of entries at millions of QPS.

---

## Opening

**Interviewer:**

Walk me through the metadata architecture. How does Dropbox track billions of files across hundreds of millions of users?

**Candidate:**

> The metadata service is the most critical component for correctness. Block storage can be eventually consistent (a few seconds of replication lag is fine), but metadata must be **strongly consistent** — two users listing the same shared folder must see the same files, always.
>
> Dropbox's metadata system, **Edgestore**, is a MySQL-based graph store with these verified scale numbers:
> - **Trillions** of metadata entries
> - **Millions** of queries per second
> - **95%** cache hit rate
> - **10 million** cross-shard transactions per second
> - **550 billion+** content pieces tracked
> - **75 billion** API calls per month

---

## 1. File Metadata Model

**Candidate:**

> Every file and folder in Dropbox has a metadata entry. Here's the schema:
>
> ```sql
> -- Core file metadata table (simplified)
> -- Sharded by namespace_id
>
> CREATE TABLE file_metadata (
>     file_id         BIGINT PRIMARY KEY,      -- Globally unique, immutable
>     namespace_id    BIGINT NOT NULL,          -- Which namespace (user root or shared folder)
>     parent_file_id  BIGINT,                   -- Parent folder's file_id
>     path_lower      VARCHAR(4096) NOT NULL,   -- Lowercased full path within namespace
>     file_name       VARCHAR(255) NOT NULL,    -- Display name (preserves case)
>     is_folder       BOOLEAN NOT NULL,
>
>     -- Block storage link (THE critical field)
>     block_list      JSON,                     -- Ordered list of block hashes: ["hash0", "hash1", ...]
>                                                -- NULL for folders
>
>     -- Version tracking
>     rev             VARCHAR(64) NOT NULL,      -- Opaque revision ID, changes on every edit
>     size            BIGINT,                    -- File size in bytes (NULL for folders)
>     content_hash    CHAR(64),                  -- Dropbox content hash (hex SHA-256)
>
>     -- Timestamps
>     server_modified TIMESTAMP NOT NULL,        -- When server received this version (TRUSTWORTHY)
>     client_modified TIMESTAMP,                 -- From client's local clock (UNTRUSTWORTHY)
>
>     -- Soft delete
>     is_deleted      BOOLEAN DEFAULT FALSE,     -- TRUE = in trash
>     deleted_at      TIMESTAMP,
>
>     -- Sharing
>     sharing_info    JSON,                      -- Shared folder ID, permissions, link info
>
>     -- Indexes
>     INDEX idx_namespace_path (namespace_id, path_lower),
>     INDEX idx_namespace_parent (namespace_id, parent_file_id),
>     INDEX idx_content_hash (content_hash)
> );
> ```
>
> **Key design decisions:**
>
> 1. **`file_id` is the identity, NOT path.** A file keeps its `file_id` forever, even when moved or renamed. This makes move/rename a metadata-only operation.
>
> 2. **`block_list` links metadata to storage.** This ordered list of block hashes is the bridge between "what the user sees" (file path, name, size) and "what's physically stored" (content-addressed blocks in Magic Pocket).
>
> 3. **`rev` for optimistic concurrency.** Every edit increments `rev`. Upload includes the base `rev` — if it doesn't match the server's current `rev`, a conflict is detected.
>
> 4. **`server_modified` vs `client_modified`.** Never trust `client_modified` for ordering decisions — client clocks can be wrong, set to the past, or manipulated. `server_modified` is the source of truth for "when did this actually happen."

---

## 2. Namespace Management

**Interviewer:**

Explain the namespace concept. Why not just have one big file table per user?

**Candidate:**

> Namespaces are how Dropbox solves the shared folder problem. Without namespaces, sharing a folder would require duplicating all its files into each collaborator's file tree. With namespaces, the shared folder exists once and is **mounted** into multiple users' trees.
>
> ```
> ┌──────────────────────────────────────────────────────────┐
> │                    NAMESPACE MODEL                        │
> │                                                          │
> │  Alice's root namespace (NS: 1001)                       │
> │  ├── /Documents/                                         │
> │  │   ├── personal.txt                                    │
> │  │   └── notes.pdf                                       │
> │  ├── /Photos/                                            │
> │  └── /SharedProject/ ──── MOUNT POINT ─┐                │
> │                                          │                │
> │  Bob's root namespace (NS: 1002)         │                │
> │  ├── /Work/                              │                │
> │  │   └── /SharedProject/ ── MOUNT ──────┤                │
> │  └── /Personal/                          │                │
> │                                          │                │
> │                                          ▼                │
> │  Shared namespace (NS: 2001)                             │
> │  ├── /design.psd                                         │
> │  ├── /spec.docx                                          │
> │  └── /assets/                                            │
> │      ├── logo.png                                        │
> │      └── banner.jpg                                      │
> │                                                          │
> │  Alice sees: /SharedProject/design.psd                   │
> │  Bob sees:   /Work/SharedProject/design.psd              │
> │  SAME file, SAME namespace, different mount paths.       │
> └──────────────────────────────────────────────────────────┘
> ```
>
> ### Why namespaces?
>
> | Without namespaces | With namespaces |
> |-------------------|-----------------|
> | Share folder = copy all files to each user's tree | Share folder = create one namespace, mount in each user's tree |
> | Change in shared folder = update N copies (one per collaborator) | Change = update once in namespace, visible to all via mount |
> | Storage: N copies of metadata | Storage: 1 copy of metadata + N mount points |
> | Permissions: per-file ACLs (complex) | Permissions: per-namespace (simple, inherited) |
> | Sync cursor: per-user (must track each user's view separately) | Sync cursor: per-namespace (one cursor serves all collaborators) |
>
> ### Namespace types:
>
> ```sql
> CREATE TABLE namespaces (
>     namespace_id    BIGINT PRIMARY KEY,
>     namespace_type  ENUM('user_root', 'shared_folder', 'team_folder'),
>     owner_id        BIGINT,                -- User who created it (NULL for team folders)
>     created_at      TIMESTAMP,
>
>     -- For shared namespaces
>     sharing_policy  JSON,                   -- ACL update policy, member policy, link policy
> );
>
> -- Mount table: maps namespaces into user file trees
> CREATE TABLE namespace_mounts (
>     user_id         BIGINT,
>     namespace_id    BIGINT,
>     mount_path      VARCHAR(4096),          -- Where this namespace appears in user's tree
>     access_level    ENUM('owner', 'editor', 'viewer'),
>     PRIMARY KEY (user_id, namespace_id)
> );
>
> -- Examples:
> -- Alice's mount: (user=alice, ns=2001, path="/SharedProject/", access=owner)
> -- Bob's mount:   (user=bob,   ns=2001, path="/Work/SharedProject/", access=editor)
> ```
>
> ### Namespace is the unit of:
> - **Sharding**: All files in a namespace are on the same MySQL shard (critical for ACID operations within a folder)
> - **Sync cursor**: Each namespace has its own change journal and cursor position
> - **Permissions**: ACL is defined at the namespace level and inherited by all files within
> - **Consistency boundary**: Operations within a namespace are ACID. Cross-namespace operations require distributed transactions.

---

## 3. Edgestore Architecture

**Interviewer:**

Why MySQL for metadata at this scale? Why not Cassandra or DynamoDB?

**Candidate:**

> This is one of Dropbox's most important architectural decisions. Let me explain the reasoning:
>
> ### Why MySQL (and not NoSQL)?
>
> | Requirement | MySQL | Cassandra | DynamoDB |
> |-------------|-------|-----------|----------|
> | **Strong consistency** | ✅ ACID transactions | ❌ Eventually consistent (tunable) | ⚠️ Strong consistency available but at 2x cost |
> | **Relational queries** | ✅ JOINs, complex WHERE | ❌ Limited query model | ❌ Key-value + secondary indexes |
> | **Directory listing** | ✅ `SELECT WHERE namespace_id=? AND parent=?` | ⚠️ Requires careful partition key design | ⚠️ Query on partition key only |
> | **Transactions** | ✅ Multi-row ACID (within shard) | ❌ No multi-row transactions | ⚠️ TransactWriteItems (limited) |
> | **Battle-tested** | ✅ Decades of production use | ✅ Good, but different failure modes | ✅ AWS managed |
> | **Expertise** | ✅ Dropbox team has deep MySQL expertise | — | — |
>
> **The critical requirement is strong consistency.** Consider this scenario:
>
> ```
> Without strong consistency:
>
> Alice creates /SharedFolder/budget.xlsx
> Bob lists /SharedFolder/ 100ms later
>
> If eventually consistent: Bob might NOT see budget.xlsx yet.
> If strongly consistent: Bob ALWAYS sees budget.xlsx.
>
> For a collaborative product, users expect: "I shared the file,
> my colleague should see it immediately." Eventual consistency
> violates this expectation and creates confusion.
> ```
>
> **Contrast with WhatsApp choosing Cassandra (AP):** WhatsApp stores chat messages in Cassandra (eventually consistent). This works because chat messages tolerate brief staleness — if a message appears 500ms late, the user doesn't notice. File metadata does NOT tolerate this — a file that doesn't appear in a directory listing is "missing," not "slightly delayed."
>
> ### Edgestore: MySQL as a Graph Store
>
> Edgestore isn't just raw MySQL — it's an abstraction layer that models metadata as a **graph**:
>
> ```
> Graph model:
>
> Nodes: files, folders, users, namespaces, shared links
> Edges: "contains" (folder→file), "owns" (user→namespace),
>        "mounts" (user→namespace), "shares_with" (namespace→user)
>
> Under the hood: MySQL tables with carefully designed indexes
> that efficiently traverse this graph.
>
> Example query: "List all files in /SharedProject/"
>   → Graph traversal: user → mount → namespace → folder → children
>   → SQL: SELECT * FROM file_metadata
>          WHERE namespace_id = 2001 AND parent_file_id = 42
>          ORDER BY file_name
> ```
>
> ### Sharding Strategy
>
> ```
> ┌─────────────────────────────────────────────────────────┐
> │              MYSQL SHARDING (by namespace_id)            │
> │                                                         │
> │  Shard 0: namespace_ids 0-999                           │
> │    ├── NS 101 (Alice's root): all Alice's personal files│
> │    ├── NS 202 (Bob's root): all Bob's personal files    │
> │    └── NS 500 (Shared folder): all files in this share  │
> │                                                         │
> │  Shard 1: namespace_ids 1000-1999                       │
> │    ├── NS 1001 (Charlie's root)                         │
> │    └── NS 1500 (Another shared folder)                  │
> │                                                         │
> │  Shard 2: namespace_ids 2000-2999                       │
> │    └── ...                                              │
> │                                                         │
> │  ... thousands of shards ...                            │
> │                                                         │
> │  Key insight: ALL files in a namespace are on the SAME  │
> │  shard. This means:                                     │
> │  - "List folder" is a single-shard query (fast)         │
> │  - "Create file in folder" is a single-shard transaction│
> │  - "Move within namespace" is a single-shard update     │
> │                                                         │
> │  Cross-namespace operations (move file between personal │
> │  and shared folder) require CROSS-SHARD transactions.   │
> │  Edgestore handles 10M cross-shard txns/sec.           │
> └─────────────────────────────────────────────────────────┘
> ```
>
> **Why shard by namespace (not by user)?**
> - If sharded by user, a shared folder's files would be spread across shards (one per collaborator) — listing the folder would require a scatter-gather across all collaborators' shards.
> - Sharding by namespace keeps all files in a shared folder co-located — listing is a single-shard query.

---

## 4. Caching Layer

**Candidate:**

> With millions of QPS, MySQL alone can't handle the read load. Dropbox uses a multi-tier caching strategy:
>
> ```
> ┌──────────────┐     ┌──────────────┐     ┌──────────────┐
> │   Client      │────>│   Cache      │────>│   MySQL      │
> │   Request     │     │   (Memcached/│     │   Shard      │
> │               │     │    Redis)    │     │              │
> │               │     │   95% hit    │     │   5% of      │
> │               │     │   rate       │     │   queries    │
> └──────────────┘     └──────────────┘     └──────────────┘
>
> Cache key design:
>   file metadata:  "file:{file_id}" → full metadata JSON
>   directory:      "dir:{namespace_id}:{parent_file_id}" → list of children
>   namespace:      "ns:{namespace_id}" → namespace metadata + ACL
>
> Cache invalidation:
>   On ANY write (create, update, delete, move):
>     1. Write to MySQL (source of truth)
>     2. Invalidate affected cache keys
>     3. Next read populates cache from MySQL
>
>   Cache invalidation is the hardest problem:
>     - Move file: invalidate old parent dir cache, new parent dir cache, file cache
>     - Share folder: invalidate namespace cache for all members
>     - Delete folder: invalidate recursively (all children, all subdirectories)
> ```
>
> **95% cache hit rate** means only 5% of the millions of QPS actually hit MySQL. At 5M QPS total, that's ~250K QPS hitting MySQL across thousands of shards — roughly 50-100 QPS per shard, which MySQL handles easily.

---

## 5. Versioning

**Interviewer:**

How does versioning work under the hood?

**Candidate:**

> Every edit creates a new revision. Versioning is metadata-only — blocks are never duplicated.
>
> ```
> File: report.pdf
>
> Version history (stored in metadata service):
>
> Rev 1 (created Feb 15): block_list = [h_A, h_B, h_C]
> Rev 2 (edited Feb 17): block_list = [h_A, h_B_new, h_C]  ← Block B changed
> Rev 3 (edited Feb 19): block_list = [h_A, h_B_new, h_C_new] ← Block C changed
> Rev 4 (edited Feb 20): block_list = [h_A, h_B_new2, h_C_new] ← Block B changed again
>
> Block reference counts:
>   h_A:     4 (referenced by all 4 revisions)
>   h_B:     1 (only rev 1)
>   h_B_new: 2 (rev 2 and rev 3)
>   h_B_new2:1 (only rev 4)
>   h_C:     1 (only rev 1)
>   h_C_new: 2 (rev 3 and rev 4)
>
> Total unique blocks: 6 (not 12 = 3 blocks × 4 versions)
> Storage saving from version dedup: 50%
> ```
>
> ### Restore is metadata-only:
>
> ```
> User wants to restore Rev 1:
>
> Before restore:
>   Current: Rev 4, block_list = [h_A, h_B_new2, h_C_new]
>
> Restore operation:
>   Create Rev 5 with block_list = [h_A, h_B, h_C]  (same as Rev 1)
>   This is just a new metadata entry pointing to old blocks.
>   Blocks h_A, h_B, h_C already exist (never deleted).
>
> After restore:
>   Current: Rev 5, block_list = [h_A, h_B, h_C]
>   (Instant operation, regardless of file size)
> ```
>
> ### Version retention and garbage collection:
>
> ```
> Retention policy:
>   Free accounts:     30 days
>   Professional:      180 days
>   Business:          180 days (configurable, up to 10 years for compliance)
>
> When a version expires:
>   1. Metadata entry marked for deletion
>   2. Block reference counts decremented
>   3. Blocks with ref_count = 0 → eligible for garbage collection
>   4. Grace period (e.g., 7 days) before actual block deletion
>      (safety net — if ref count was wrong, blocks can be recovered)
>   5. After grace period → blocks permanently deleted from Magic Pocket
> ```

---

## 6. Move/Rename and Copy Semantics

**Candidate:**

> ### Move/Rename = O(1) Metadata Update
>
> ```sql
> -- Move /Documents/report.pdf → /Archive/2026/report.pdf
>
> -- This is a SINGLE ROW UPDATE:
> UPDATE file_metadata
> SET path_lower = '/archive/2026/report.pdf',
>     file_name = 'report.pdf',
>     parent_file_id = (SELECT file_id FROM file_metadata
>                       WHERE path_lower = '/archive/2026/' AND namespace_id = 1001)
> WHERE file_id = 42 AND namespace_id = 1001;
>
> -- That's it. No blocks copied. No data moved.
> -- A 10 GB file moves in < 1ms.
> -- Same file_id (42) — identity preserved.
> -- Same content_hash — same blocks.
> ```
>
> This is only possible because file identity is `file_id`, not path. The block store doesn't even know about paths.
>
> ### Copy = New Metadata, Same Blocks
>
> ```sql
> -- Copy /Templates/invoice.xlsx → /Q1/invoice.xlsx
>
> -- Create NEW metadata entry pointing to SAME block_list:
> INSERT INTO file_metadata (
>     file_id, namespace_id, path_lower, file_name,
>     block_list, rev, size, content_hash
> ) VALUES (
>     NEW_FILE_ID, 1001, '/q1/invoice.xlsx', 'invoice.xlsx',
>     '["h_A","h_B","h_C"]',  -- SAME block list as original
>     'new_rev_1', 15728640, 'same_content_hash'
> );
>
> -- Increment block reference counts:
> -- h_A: ref_count += 1
> -- h_B: ref_count += 1
> -- h_C: ref_count += 1
>
> -- Result: 0 bytes of additional block storage
> -- Both files share the same physical blocks
> -- When one file is edited, ONLY the edited blocks diverge
> -- (copy-on-write)
> ```

---

## 7. Change Journal

**Candidate:**

> Every mutation is recorded in a per-namespace change journal — the foundation of the sync protocol.
>
> ```sql
> CREATE TABLE change_journal (
>     namespace_id    BIGINT NOT NULL,
>     cursor_id       BIGINT AUTO_INCREMENT,   -- Monotonically increasing within namespace
>     change_type     ENUM('create', 'update', 'delete', 'move', 'rename'),
>     file_id         BIGINT NOT NULL,
>     new_path        VARCHAR(4096),
>     old_path        VARCHAR(4096),            -- For move/rename
>     new_rev         VARCHAR(64),
>     timestamp       TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
>
>     PRIMARY KEY (namespace_id, cursor_id),
>     INDEX idx_namespace_cursor (namespace_id, cursor_id)
> );
>
> -- Sync query: "What changed in namespace 2001 since cursor 5000?"
> SELECT * FROM change_journal
> WHERE namespace_id = 2001 AND cursor_id > 5000
> ORDER BY cursor_id ASC
> LIMIT 1000;
> ```
>
> **Why per-namespace journals?**
> - A user with 5 shared folders has 6 namespaces (1 root + 5 shared). The sync cursor is per-namespace.
> - When a collaborator changes a file in shared folder X, only that namespace's cursor advances. The user's root namespace cursor is unaffected.
> - The client tracks: `{ns_1001: cursor_5000, ns_2001: cursor_3200, ns_2002: cursor_800}`
> - The longpoll watches ALL of the user's namespace cursors — responds when ANY advances.
>
> **The opaque cursor**: The cursor returned by the API is not just a simple integer — it's an encoded token that contains cursor positions for all of the user's namespaces, plus metadata about which namespaces the user has access to. This allows the server to change the encoding without breaking clients.

---

## 8. Cross-Shard Transactions

**Interviewer:**

What about operations that span namespaces? Like moving a file from personal folder to a shared folder?

**Candidate:**

> Cross-namespace operations are inherently cross-shard (since each namespace is on a specific shard). Edgestore handles **10 million cross-shard transactions per second**.
>
> ```
> Scenario: Move /Personal/report.pdf → /SharedProject/report.pdf
>
> This involves TWO namespaces on potentially DIFFERENT shards:
>   Shard A: namespace 1001 (Alice's root)  → DELETE file from here
>   Shard B: namespace 2001 (SharedProject)  → INSERT file here
>
> Cross-shard transaction protocol (2PC variant):
>
> 1. PREPARE on Shard A: Lock the file row, verify file exists
> 2. PREPARE on Shard B: Lock the target path, verify no conflict
> 3. COMMIT on both shards:
>    - Shard A: Mark file as deleted from namespace 1001
>    - Shard B: Insert file into namespace 2001
>    - Both: Update change journals
> 4. If either PREPARE fails → ABORT both
>
> This is expensive (cross-DC round trips) but rare:
>   - Most operations are within a single namespace (fast, single-shard)
>   - Cross-namespace moves are uncommon user actions
>   - 10M cross-shard txns/sec capacity handles the load
> ```
>
> **Optimization**: Edgestore batches cross-shard transactions and uses pipelining to amortize the round-trip cost across multiple transactions.

---

## 9. Consistency Model

**Candidate:**

> Dropbox uses a **split consistency model** — different consistency levels for different data:
>
> | Data Type | Consistency | Why |
> |-----------|-------------|-----|
> | **File metadata** (Edgestore) | **Strongly consistent** (read-after-write) | Users must see consistent directory listings. Two users listing the same shared folder must see the same files. |
> | **Block storage** (Magic Pocket) | **Eventually consistent** (seconds of lag) | Newly uploaded blocks may take seconds to replicate across DCs. This is OK because metadata gates access — you can't reference a block until the metadata commit succeeds. |
> | **Sync cursors** | **Strongly consistent** (monotonically increasing per namespace) | A client must never go backward — once you've seen cursor 5000, the next sync must start from ≥5000. |
> | **Search index** | **Eventually consistent** (minutes of lag) | Search results being slightly stale is acceptable — users understand search takes time to index. |
>
> ### Why not strong consistency everywhere?
>
> Strong consistency for block storage would require synchronous cross-DC replication for every block write — adding ~50-100ms of latency per block upload. At 100K-175K new block writes/sec, this would be extremely expensive and slow. Since blocks are only accessible after metadata commit (which IS strongly consistent), the eventual consistency of block replication is invisible to users.
>
> **Contrast with Google Spanner**: Google uses Spanner for globally consistent metadata. Spanner uses TrueTime (atomic clocks + GPS) for global ordering. This gives stronger guarantees than MySQL replication but at higher cost and complexity. Dropbox chose MySQL for pragmatism — it's simpler, cheaper, and the team has deep expertise.

---

## Contrast: Edgestore vs Cassandra vs S3

| Aspect | Edgestore (MySQL) | Cassandra (WhatsApp) | S3 Metadata |
|--------|-------------------|---------------------|-------------|
| **Consistency** | Strong (ACID) | Eventual (tunable) | Strong (since Dec 2020) |
| **Data model** | Relational / graph | Wide-column | Key-value (bucket/key) |
| **Namespace** | Hierarchical (true folders) | Flat (partition key → rows) | Flat (bucket + key prefix) |
| **Transactions** | Multi-row ACID + cross-shard | No multi-row | No transactions |
| **Query flexibility** | Full SQL (JOINs, WHERE, ORDER BY) | Limited (by partition key) | GetObject by key only |
| **Sharding** | By namespace_id | By partition key | By key hash (internal) |
| **Why chosen** | Collaborative file system needs ACID + hierarchical queries | Chat messages tolerate staleness, need high write throughput | Object store — simple put/get |

---

## L5 vs L6 vs L7 — Metadata Service Rubric

| Aspect | L5 (SDE-2) | L6 (SDE-3) | L7 (Principal) |
|--------|------------|------------|-----------------|
| **Data model** | "Store file name, size, path in a database" | Designs full schema with file_id, namespace_id, block_list, rev. Explains why file_id (not path) is the identity. | Explains Edgestore as a graph store, namespace-based sharding for co-location, the mount table for shared folders |
| **Consistency** | "Use a consistent database" | Explains strong metadata + eventual blocks split model, why this combination works | Discusses cross-shard transaction protocol (2PC), 10M txns/sec, trade-off vs Spanner's global consistency |
| **Sharding** | "Shard by user ID" | Shards by namespace_id, explains why (co-locate shared folder files). Discusses hot shard mitigation. | Calculates QPS per shard (millions total / thousands of shards), designs cache invalidation strategy, explains 95% cache hit rate economics |
| **Versioning** | "Keep old versions" | Designs revision-based versioning with metadata-only restore, reference-counted blocks | Explains garbage collection correctness (grace periods, two-phase deletion), calculates storage impact of version retention policies |
| **Contrast** | None | Contrasts with Cassandra (AP, eventual) | Three-way comparison: Edgestore (CP, relational, graph), Cassandra (AP, wide-column, flat), S3 (key-value, flat) — ties each to product requirements |

---

> **Summary:** The metadata service (Edgestore) is the most critical component for user-facing correctness. It's built on MySQL (not NoSQL) because collaborative file management requires strong consistency, relational queries, and ACID transactions — things that Cassandra and DynamoDB sacrifice for availability and write throughput. The namespace model elegantly solves shared folders by creating a single source of truth that's mounted into multiple users' file trees. Sharding by namespace ensures all operations within a shared folder are single-shard (fast, ACID), while Edgestore handles 10M cross-shard transactions/sec for the less common cross-namespace operations. The 95% cache hit rate means MySQL only sees 5% of the total query load, making this architecture scalable to trillions of entries.
