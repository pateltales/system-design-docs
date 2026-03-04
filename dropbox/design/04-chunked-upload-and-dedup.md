# Deep Dive: Chunked Upload & Block-Level Deduplication

> **Context:** The upload pipeline is the most compute-intensive client-side operation and the foundation of Dropbox's storage efficiency. Block-level dedup is Dropbox's core technical moat — it's what makes storing exabytes affordable.

---

## Opening

**Interviewer:**

Walk me through the upload pipeline in detail — from a file change on the client to the data being durably stored on the server.

**Candidate:**

> The upload pipeline has 8 stages. Each stage exists for a specific reason, and the order matters:
>
> ```
> ┌──────────────────────────────────────────────────────────────────┐
> │                    UPLOAD PIPELINE                               │
> │                                                                  │
> │  ① Detect  → ② Chunk  → ③ Hash  → ④ Dedup  → ⑤ Compress       │
> │  Change      File       Blocks    Check      New Blocks         │
> │                                                                  │
> │  → ⑥ Upload  → ⑦ Commit  → ⑧ Notify                           │
> │    New Blocks   Metadata    Clients                              │
> └──────────────────────────────────────────────────────────────────┘
>
> Stage details:
>
> ① DETECT:  File watcher fires (inotify/FSEvents/RDCW)
>            Debounce 500ms to let saves complete
>
> ② CHUNK:   Split file into 4 MB (4,194,304 byte) blocks
>            Last block may be smaller
>
> ③ HASH:    SHA-256 hash each block → 32-byte fingerprint
>            Also compute content_hash (hash of all block hashes)
>
> ④ DEDUP:   Send block hashes to server: "which of these do you have?"
>            Server responds with boolean array
>            Skip uploading blocks server already has
>
> ⑤ COMPRESS: zlib/lz4 compress each NEW block
>             (only blocks that need uploading)
>             Compression AFTER hashing (hash must be of raw content)
>
> ⑥ UPLOAD:  Upload new blocks via chunked upload session
>            Parallel uploads (4-8 concurrent connections)
>            Resumable — track last successful offset
>
> ⑦ COMMIT:  POST /upload_session/finish
>            Atomically: create metadata entry (file → ordered block list)
>            Increment revision, update change journal
>
> ⑧ NOTIFY:  Server wakes longpoll listeners for this namespace
>            Other clients discover the change and sync
> ```

---

## 1. Chunking Strategy

**Interviewer:**

Why 4 MB blocks specifically? And how do you handle the chunking?

**Candidate:**

> ### Fixed-Size Chunking (Dropbox's Primary Approach)
>
> ```python
> BLOCK_SIZE = 4 * 1024 * 1024  # 4 MB = 4,194,304 bytes
>
> def chunk_file(file_path):
>     blocks = []
>     with open(file_path, 'rb') as f:
>         while True:
>             block = f.read(BLOCK_SIZE)
>             if not block:
>                 break
>             blocks.append(block)
>     return blocks
>
> # Example: 100 MB file
> # blocks[0]  = bytes 0 to 4,194,303          (4 MB)
> # blocks[1]  = bytes 4,194,304 to 8,388,607  (4 MB)
> # ...
> # blocks[24] = bytes 100,663,296 to 104,857,599 (4 MB)
> # Total: 25 blocks
> ```
>
> **Why 4 MB?**
>
> | Block Size | Blocks per 1 GB | Metadata per file | Dedup granularity | Upload unit |
> |-----------|----------------|-------------------|-------------------|-------------|
> | 64 KB | 16,384 | 512 KB of hashes | Very fine — great dedup | Too small — HTTP overhead dominates |
> | 1 MB | 1,024 | 32 KB of hashes | Fine — good dedup | Reasonable upload unit |
> | **4 MB** | **256** | **8 KB of hashes** | **Good balance** | **Good upload unit, resumable** |
> | 16 MB | 64 | 2 KB of hashes | Coarse — less dedup | Large — slow retry on failure |
> | 64 MB | 16 | 512 bytes of hashes | Very coarse — poor dedup | Very large — impractical for mobile |
>
> **4 MB is the sweet spot:**
> - **Metadata overhead**: 256 hashes (8 KB) per GB of data — manageable even for millions of files
> - **Dedup granularity**: A 4 MB change in a 1 GB file uploads 4 MB, not 1 GB. Good enough for most edits.
> - **Upload efficiency**: 4 MB is large enough to amortize HTTP overhead but small enough to resume without re-uploading much data on failure
> - **Memory**: Client can hold one block in memory at a time — 4 MB is negligible
>
> ### Content-Defined Chunking (CDC) — The Alternative
>
> ```
> Rolling hash (Rabin fingerprint) algorithm:
>
> Slide a 48-byte window across the file.
> At each byte position i:
>   hash = rabin_fingerprint(file[i:i+48])
>   if hash % (2^13) == 0:     // Lower 13 bits are zero
>     mark position i as block boundary
>
> Average block size ≈ 2^13 = 8192 bytes (8 KB)
> (For Dropbox-scale, this would be tuned to larger averages)
>
> Key property: boundaries depend on LOCAL content, not global position.
> Inserting data shifts only the nearest boundary.
> ```
>
> **CDC example:**
>
> ```
> Original file:
>   "The quick brown fox jumps|over the lazy dog|and then rests"
>   (| marks CDC boundaries based on content patterns)
>   Block A: "The quick brown fox jumps"
>   Block B: "over the lazy dog"
>   Block C: "and then rests"
>
> After inserting "HELLO " at the beginning:
>   "HELLO The quick brown fox jumps|over the lazy dog|and then rests"
>   Block A': "HELLO The quick brown fox jumps"  ← changed
>   Block B:  "over the lazy dog"                ← UNCHANGED (same content → same hash!)
>   Block C:  "and then rests"                   ← UNCHANGED
>
> With CDC: upload 1 block (Block A')
> With fixed-size: ALL blocks shifted → upload everything
> ```

---

## 2. Block-Level Deduplication

**Interviewer:**

Explain how deduplication works end-to-end. How does the server know it already has a block?

**Candidate:**

> ### Content-Addressable Storage (CAS)
>
> The fundamental principle: **blocks are stored by their content hash, not by file path or upload time.** Two different files containing identical content share the same blocks.
>
> ```
> ┌─────────────────────────────────────────────────────────┐
> │                 CONTENT-ADDRESSABLE STORE                │
> │                                                         │
> │  Block Hash (SHA-256)              → Block Data          │
> │  ─────────────────────────────────────────────────       │
> │  e3b0c44298fc1c149afbf4c8996fb924 → [4 MB of bytes]    │
> │  9f86d081884c7d659a2feaa0c55ad015 → [4 MB of bytes]    │
> │  d7a8fbb307d7809469ca9abcb0082e4f → [4 MB of bytes]    │
> │  ...                                                    │
> │                                                         │
> │  File Metadata:                                         │
> │  ─────────────                                          │
> │  /alice/report.pdf → [hash_0, hash_1, hash_2, hash_3]  │
> │  /bob/report.pdf   → [hash_0, hash_1, hash_2, hash_3]  │
> │  ← Same block list! Both files share ALL 4 blocks.      │
> │  ← Stored once, referenced twice.                       │
> │                                                         │
> │  /alice/report_v2.pdf → [hash_0, hash_1, hash_2_new, hash_3]
> │  ← Shares 3 blocks with v1. Only hash_2_new is unique. │
> └─────────────────────────────────────────────────────────┘
> ```
>
> ### The Dedup Check (has_blocks)
>
> ```
> Sequence: Block Dedup Check
>
> Client                                   Server
>   │                                         │
>   │  File changed: report.pdf (100 MB)      │
>   │  Chunk into 25 blocks                   │
>   │  SHA-256 hash each block                │
>   │                                         │
>   │  Block hashes:                          │
>   │  [h0, h1, h2, h3, ..., h24]            │
>   │                                         │
>   │── POST /internal/blocks/has ───────────>│
>   │   { block_hashes: [h0..h24] }           │
>   │                                         │
>   │                    Server lookups in     │
>   │                    block index:          │
>   │                    h0 → EXISTS           │
>   │                    h1 → EXISTS           │
>   │                    h2 → NOT FOUND        │
>   │                    h3 → EXISTS           │
>   │                    ...                   │
>   │                    h24 → EXISTS          │
>   │                                         │
>   │<── { has: [T,T,F,T,...,T] } ───────────│
>   │                                         │
>   │  Only block h2 is new.                  │
>   │  Upload ONLY 4 MB instead of 100 MB.    │
>   │                                         │
>   │── Upload session (block h2 only) ──────>│
>   │                                         │
>   │── Commit metadata ─────────────────────>│
>   │   file → [h0, h1, h2_new, h3, ..., h24]│
> ```
>
> ### Dedup Scenarios and Savings:
>
> | Scenario | Without Dedup | With Dedup | Savings |
> |----------|--------------|------------|---------|
> | **Same file, new version** (edited 1 block of 25) | 100 MB | 4 MB | 96% |
> | **1000 users share same PDF** (email attachment) | 1000 × 5 MB = 5 GB | 5 MB (stored once) | 99.9% |
> | **OS update** (thousands of users download same update) | N × 4 GB | 4 GB | (N-1)/N × 100% |
> | **Completely unique file** (new photo) | 8 MB | 8 MB | 0% |
> | **File copy** (user copies file within Dropbox) | 0 (metadata-only copy) | 0 | N/A (no blocks transferred) |
>
> **Aggregate dedup ratio**: At Dropbox's scale, block-level dedup reportedly achieves significant storage savings across the entire user base. The exact ratio is not publicly disclosed, but estimates suggest 50-60% of blocks are duplicates that don't require additional storage. [INFERRED — exact ratio not officially published]

---

## 3. The Content Hash Algorithm

**Interviewer:**

Walk me through the content_hash computation with a concrete example.

**Candidate:**

> ### Step-by-step with a real example:
>
> ```
> File: vacation_photos.zip (10,485,760 bytes = exactly 10 MB)
>
> Step 1: Split into 4 MB blocks
>   Block 0: bytes[0 .. 4,194,303]        = 4,194,304 bytes (4 MB)
>   Block 1: bytes[4,194,304 .. 8,388,607] = 4,194,304 bytes (4 MB)
>   Block 2: bytes[8,388,608 .. 10,485,759] = 2,097,152 bytes (2 MB, last block)
>
> Step 2: SHA-256 each block (producing 32-byte raw digests)
>   hash_0 = SHA256(block_0) = a1b2c3d4e5f6...  (32 bytes raw)
>   hash_1 = SHA256(block_1) = f7e8d9c0b1a2...  (32 bytes raw)
>   hash_2 = SHA256(block_2) = 1234567890ab...  (32 bytes raw)
>
> Step 3: Concatenate all block hashes (raw bytes, NOT hex strings)
>   all_hashes = hash_0 || hash_1 || hash_2
>              = 32 + 32 + 32 = 96 bytes
>
> Step 4: SHA-256 the concatenation
>   content_hash = SHA256(all_hashes)
>                = "e5c67b8a..." (hex string, 64 characters)
> ```
>
> ### Python implementation:
>
> ```python
> import hashlib
>
> BLOCK_SIZE = 4 * 1024 * 1024  # 4 MB
>
> def dropbox_content_hash(file_path):
>     """
>     Compute Dropbox's content_hash for a file.
>     Matches the content_hash field returned in file metadata.
>     """
>     block_hashes = b""  # Raw bytes, not hex
>
>     with open(file_path, "rb") as f:
>         while True:
>             block = f.read(BLOCK_SIZE)
>             if not block:
>                 break
>             # SHA-256 of this block (32-byte raw digest)
>             block_hash = hashlib.sha256(block).digest()
>             block_hashes += block_hash
>
>     # SHA-256 of concatenated block hashes
>     return hashlib.sha256(block_hashes).hexdigest()
>
> # Verification:
> # The content_hash returned by the Dropbox API for a given file
> # should exactly match this function's output for the same file.
> ```
>
> ### Why this specific scheme?
>
> 1. **Incremental computation**: You process one 4 MB block at a time — never load the entire file into memory. Essential for multi-GB files on memory-constrained devices.
>
> 2. **Dedup-aligned**: The individual block hashes (step 2) are **exactly** what's used for the dedup lookup. The content_hash computation naturally produces the dedup keys as a byproduct.
>
> 3. **Verifiable**: Client computes content_hash, server returns content_hash in metadata. If they match, the file was transferred and stored correctly. If they don't match → corruption detected, retry.
>
> 4. **Deterministic**: Same file content → same content_hash, always. Independent of:
>    - File name or path (irrelevant — only content matters)
>    - Upload time or order
>    - Which user uploaded it
>    - Which server processed it
>
> 5. **Two-level Merkle tree**: The content_hash is essentially a two-level Merkle tree:
>    - Leaf level: block hashes
>    - Root level: hash of all leaves
>    - This allows efficient verification: if block 7 is suspect, re-hash block 7 and check against the stored block hash — no need to re-hash the entire file.

---

## 4. Resumable Uploads

**Interviewer:**

What happens when the network drops in the middle of a 2 GB upload?

**Candidate:**

> Without resumable uploads, the user must start over. With a 2 GB file on a 10 Mbps upload connection, that's ~27 minutes of upload lost. Resumability is critical.
>
> ### How resumable uploads work:
>
> ```
> Timeline: 2 GB file upload (500 blocks × 4 MB)
>
> Client                              Server
>   │                                    │
>   │── start_session ──────────────────>│
>   │<── session_id: "S123" ────────────│
>   │                                    │
>   │── append(offset=0, block_0) ─────>│  ✓ Block 0 stored
>   │── append(offset=4MB, block_1) ───>│  ✓ Block 1 stored
>   │── append(offset=8MB, block_2) ───>│  ✓ Block 2 stored
>   │                                    │
>   │   ╳ NETWORK FAILURE ╳              │
>   │   (connection dropped at block 3)  │
>   │                                    │
>   │   ... 30 seconds later ...         │
>   │   Network recovers.                │
>   │                                    │
>   │   Client knows: last ACK was       │
>   │   for offset 8MB (block 2).        │
>   │   Resume from offset 12MB (block 3)│
>   │                                    │
>   │── append(offset=12MB, block_3) ──>│  ✓ Block 3 stored
>   │── append(offset=16MB, block_4) ──>│  ✓ Block 4 stored
>   │── ... continue from where we left  │
>   │── append(offset=1996MB, block_499)>│ ✓ Block 499 stored
>   │                                    │
>   │── finish(commit) ────────────────>│
>   │<── 200 OK (file created) ─────────│
>
> Result: Only block 3 was retried. Blocks 0-2 were NOT re-uploaded.
> For a 2 GB file, we saved re-uploading 12 MB (blocks 0-2).
> ```
>
> ### Client-side session tracking:
>
> ```python
> class UploadSession:
>     def __init__(self, file_path, session_id=None):
>         self.file_path = file_path
>         self.session_id = session_id
>         self.uploaded_offset = 0
>         self.total_size = os.path.getsize(file_path)
>
>     def resume(self):
>         """Resume upload from last successful offset."""
>         with open(self.file_path, 'rb') as f:
>             f.seek(self.uploaded_offset)  # Skip already-uploaded bytes
>
>             while True:
>                 block = f.read(BLOCK_SIZE)
>                 if not block:
>                     break
>
>                 try:
>                     if self.session_id is None:
>                         resp = api.upload_session_start(block)
>                         self.session_id = resp['session_id']
>                     else:
>                         api.upload_session_append(
>                             session_id=self.session_id,
>                             offset=self.uploaded_offset,
>                             data=block
>                         )
>
>                     self.uploaded_offset += len(block)
>                     self.save_progress()  # Persist to disk for crash recovery
>
>                 except NetworkError:
>                     # Will retry from self.uploaded_offset on next call
>                     return False
>
>         # All blocks uploaded — commit
>         api.upload_session_finish(self.session_id, self.commit_info)
>         return True
> ```
>
> ### Session persistence:
> Upload session state (session_id, offset, file_path) is persisted to a local SQLite database. Even if the Dropbox app crashes or the machine reboots, the upload resumes from the last successful offset. Sessions expire after 7 days on the server side — if the user doesn't complete within 7 days, they must restart.

---

## 5. Cross-User Deduplication

**Interviewer:**

How does dedup work across different users? If 1000 users each upload the same file, is it really stored once?

**Candidate:**

> Yes — and this is one of Dropbox's biggest storage optimizations.
>
> ### How cross-user dedup works:
>
> ```
> User Alice uploads "annual_report.pdf" (20 MB, 5 blocks):
>   Block hashes: [h_A, h_B, h_C, h_D, h_E]
>   All blocks are new → upload all 5 → store in block store
>   Metadata: /alice/annual_report.pdf → [h_A, h_B, h_C, h_D, h_E]
>
> User Bob uploads the SAME "annual_report.pdf":
>   Block hashes: [h_A, h_B, h_C, h_D, h_E]  (identical!)
>   Dedup check: server has ALL 5 blocks already
>   Upload: ZERO bytes transferred
>   Metadata: /bob/annual_report.pdf → [h_A, h_B, h_C, h_D, h_E]
>   (Different metadata entry, same block references)
>
> Storage used: 20 MB (not 40 MB)
> Block reference counts: h_A:2, h_B:2, h_C:2, h_D:2, h_E:2
> ```
>
> ### Common cross-user dedup scenarios:
>
> | Scenario | Files | Without dedup | With dedup | Savings |
> |----------|-------|--------------|------------|---------|
> | Email attachment shared in company (500 recipients save it) | 500 × 10 MB | 5 GB | 10 MB | 99.8% |
> | Popular software installer (100K users download same .exe) | 100K × 500 MB | 50 TB | 500 MB | 99.999% |
> | OS default wallpapers (millions of users) | M × 5 MB | 5M GB | 5 MB | ~100% |
> | Course materials (professor shares with 200 students) | 200 × 50 MB | 10 GB | 50 MB | 99.5% |
>
> ### Reference counting:
>
> Each block has a reference count — how many file metadata entries point to it. When a file is deleted (or a version expires), the reference count is decremented. When the count reaches zero, the block is eligible for garbage collection.
>
> ```
> Block h_A:
>   ref_count: 500  (500 users have files referencing this block)
>
> User #347 deletes their copy:
>   ref_count: 499
>
> All 500 users delete their copies:
>   ref_count: 0 → block eligible for garbage collection
>   (after a grace period for safety)
> ```
>
> **Critical correctness requirement**: The reference count must be **exactly correct**. If a block is garbage-collected while a file still references it → **permanent data loss**. This is one of the hardest correctness problems in the system. Dropbox uses careful two-phase garbage collection with grace periods to prevent premature collection.

---

## 6. Security Considerations

**Interviewer:**

Doesn't cross-user dedup create a security risk? Can an attacker probe for file existence?

**Candidate:**

> Yes — this is a well-known attack called **hash manipulation** or **side-channel dedup attack**:
>
> ### The attack:
>
> ```
> Attacker wants to know if Target has file X on their Dropbox.
>
> 1. Attacker obtains file X through other means.
> 2. Attacker computes SHA-256 block hashes of file X.
> 3. Attacker uploads file X to their own Dropbox.
> 4. Attacker measures: was the upload instant (dedup hit, blocks existed)
>    or slow (blocks actually uploaded)?
>
> If instant → someone else already has file X on Dropbox.
> (Attacker can't tell WHO, but knows it exists in the system.)
> ```
>
> ### Mitigations:
>
> 1. **Convergent encryption (theoretical)**: Encrypt each block with a key derived from the block's content hash: `encrypted_block = AES(SHA-256(block), block)`. Same content → same key → same ciphertext → dedup still works. But: vulnerable to confirmation attacks (attacker who has the plaintext can compute the key). Used by some systems (Tahoe-LAFS) but not Dropbox.
>
> 2. **Server-side dedup only (Dropbox's approach)**: The dedup check is done server-side — the client uploads block hashes, and the server decides whether to skip storage. The server doesn't reveal to the client whether a block already existed. The timing difference (instant vs slow) is harder to exploit because:
>    - Network latency variation masks the timing signal
>    - Server processes dedup asynchronously
>    - Multiple blocks in a single session make per-block timing impractical
>
> 3. **Rate limiting**: Limit the number of dedup checks per user per time window to prevent bulk probing.
>
> 4. **User-specific encryption (Dropbox Business)**: Enterprise accounts can use user-specific encryption keys, which eliminates cross-user dedup but provides stronger privacy guarantees. Trade-off: less storage efficiency for more security.
>
> **In practice**: Dropbox accepts the theoretical risk of dedup side-channels because:
> - The practical exploitability is low (requires precise timing measurements)
> - The storage savings are enormous (50-60% at their scale = hundreds of petabytes)
> - Enterprise customers who need stronger guarantees can use per-team encryption

---

## 7. Compression in the Pipeline

**Candidate:**

> Blocks are compressed before upload to reduce bandwidth. The compression step has a specific position in the pipeline:
>
> ```
> Pipeline order and WHY:
>
> ① Chunk  → ② Hash  → ③ Dedup check  → ④ Compress  → ⑤ Upload
>
> Why hash BEFORE compress?
>   - The hash must be of the ORIGINAL content (for dedup to work)
>   - If you hash after compression, different compression algorithms/levels
>     would produce different hashes for the same content → no dedup
>
> Why compress AFTER dedup check?
>   - Why waste CPU compressing a block you won't upload?
>   - Compress only the blocks that actually need transferring
>
> Why compress BEFORE upload?
>   - Reduces bytes on the wire → faster upload, less bandwidth cost
>   - Especially valuable on slow mobile networks
> ```
>
> **Compression algorithm choice:**
>
> | Algorithm | Compression ratio | Speed | Used for |
> |-----------|-------------------|-------|----------|
> | **zlib** (deflate) | High (~50-70% for text) | Medium | Default for most block types |
> | **lz4** | Medium (~40-50%) | Very fast | Time-sensitive transfers, already-compressed data detection |
> | **None** | 0% | Instant | Already-compressed files (JPEG, MP4, ZIP) |
>
> **Already-compressed detection**: The client checks if a block is already compressed (JPEG, PNG, ZIP, MP4, etc.) by looking at file extension and/or attempting compression — if the compressed size is ≥ original size, skip compression. Compressing an already-compressed file wastes CPU and can actually increase size.

---

## 8. Contrast: Dropbox vs S3 Multipart vs Git

**Interviewer:**

How does Dropbox's chunked upload compare to S3's multipart upload and Git's object store?

**Candidate:**

> | Aspect | Dropbox Chunked Upload | S3 Multipart Upload | Git Objects |
> |--------|----------------------|---------------------|-------------|
> | **Purpose** | Dedup + resume + delta sync | Reliability + parallelism | Version control + dedup |
> | **Block size** | Fixed 4 MB | Variable 5 MB - 5 GB | Variable (whole files, delta-packed) |
> | **Block identity** | Content hash (SHA-256) | Part number (positional) | Content hash (SHA-1 → SHA-256) |
> | **Dedup** | Yes — cross-file, cross-user, cross-version | **No** — each upload independent | Yes — identical objects stored once |
> | **Delta sync** | Yes — upload only changed blocks | No — re-upload entire object | Yes — git packfiles use delta compression |
> | **Resumable** | Yes — per-block resume | Yes — per-part resume | N/A (push is all-or-nothing) |
> | **Content addressing** | Yes (CAS by SHA-256) | No (S3 key is path, not content) | Yes (CAS by SHA-1/SHA-256) |
> | **Cross-user** | Yes (blocks shared globally) | No (each user's uploads are independent) | Yes (within same repo) |
>
> ### S3 Multipart Upload — similar mechanics, different purpose:
>
> ```
> S3 Multipart:
>   1. CreateMultipartUpload → upload_id
>   2. UploadPart(upload_id, part_number=1, data) → ETag
>   3. UploadPart(upload_id, part_number=2, data) → ETag
>   4. CompleteMultipartUpload(upload_id, [ETags])
>
> Similarities with Dropbox: chunked, resumable, parallel parts
> Key difference: S3 does NOT deduplicate parts across uploads.
>   - User A uploads 1 GB file → 200 parts stored
>   - User B uploads same 1 GB file → 200 MORE parts stored (no dedup!)
>   - Dropbox: second upload stores ZERO new blocks
> ```
>
> ### Git Objects — same principle, different domain:
>
> ```
> Git stores objects by content hash:
>   blob (file content)  → SHA-1 hash
>   tree (directory)     → SHA-1 hash
>   commit              → SHA-1 hash
>
> Identical files across branches/commits share the same blob object.
> Git packfiles use delta compression (store diffs between similar objects).
>
> Dropbox and Git share the CAS principle:
>   Content → Hash → Store by hash → Dedup automatically
>
> Difference: Git operates on entire files as objects.
> Dropbox splits files into 4 MB blocks — finer granularity.
> A 1 MB edit to a 1 GB file:
>   Git: new 1 GB blob (delta-packed later)
>   Dropbox: 1 new 4 MB block (immediate dedup)
> ```

---

## 9. Worked Example: End-to-End Upload

**Candidate:**

> Let me trace a complete upload scenario with actual (example) values:
>
> ```
> Scenario: Alice edits "quarterly_report.pdf" (52 MB)
>           She added 2 pages in the middle (affected blocks 5 and 6)
>
> Step 1: File watcher detects change
>   Event: IN_CLOSE_WRITE on /home/alice/Dropbox/quarterly_report.pdf
>   Debounce: wait 500ms to ensure save is complete
>
> Step 2: Chunk into blocks
>   Block 0:  bytes[0 .. 4,194,303]         4 MB
>   Block 1:  bytes[4,194,304 .. 8,388,607] 4 MB
>   ...
>   Block 5:  bytes[20,971,520 .. 25,165,823] 4 MB ← CHANGED
>   Block 6:  bytes[25,165,824 .. 29,360,127] 4 MB ← CHANGED
>   ...
>   Block 12: bytes[50,331,648 .. 52,428,799] 2 MB (last block, partial)
>   Total: 13 blocks
>
> Step 3: SHA-256 hash each block
>   h_0  = "a1b2c3d4..."  (matches previous version)
>   h_1  = "e5f6g7h8..."  (matches)
>   ...
>   h_5  = "NEW_HASH_5"   (different from previous!)
>   h_6  = "NEW_HASH_6"   (different from previous!)
>   ...
>   h_12 = "q9r0s1t2..."  (matches)
>
> Step 4: Dedup check
>   Client → Server: has_blocks([h_0, h_1, ..., h_12])
>   Server → Client: [T, T, T, T, T, F, F, T, T, T, T, T, T]
>                                        ↑  ↑
>                                   Blocks 5 and 6 are new
>
> Step 5: Compress new blocks
>   Block 5 (4 MB) → zlib → 3.2 MB compressed
>   Block 6 (4 MB) → zlib → 3.1 MB compressed
>
> Step 6: Upload (only 2 blocks)
>   POST /upload_session/start     → session_id, upload block 5 (3.2 MB)
>   POST /upload_session/append_v2 → upload block 6 (3.1 MB)
>   POST /upload_session/finish    → commit
>
> Step 7: Server commits
>   New metadata: quarterly_report.pdf (rev: 7) →
>     [h_0, h_1, h_2, h_3, h_4, NEW_HASH_5, NEW_HASH_6, h_7, ..., h_12]
>   Change journal: cursor incremented
>   Previous version (rev: 6) still has its block list (for version history)
>
> Step 8: Notification
>   Server wakes Bob's longpoll (Bob has this file in a shared folder)
>   Bob's client fetches changes, downloads blocks 5 and 6, reconstructs file
>
> Summary:
>   File size: 52 MB
>   Data uploaded: 6.3 MB (compressed blocks 5 and 6)
>   Bandwidth saved: 45.7 MB (88% reduction)
>   Upload time at 10 Mbps: ~5 seconds (vs ~42 seconds for full file)
> ```

---

## L5 vs L6 vs L7 — Upload & Dedup Rubric

| Aspect | L5 (SDE-2) | L6 (SDE-3) | L7 (Principal) |
|--------|------------|------------|-----------------|
| **Chunking** | "Split file into chunks and upload" | Specifies 4 MB blocks, explains why this size (metadata overhead vs dedup granularity) | Discusses CDC vs fixed-size trade-offs, Rabin fingerprint, insertion resilience problem |
| **Dedup** | "Don't upload duplicate files" | Explains block-level dedup with has_blocks check, content-addressable storage | Calculates cross-user dedup savings at scale, discusses reference counting and GC correctness |
| **Content hash** | "Hash the file for integrity" | Explains the two-level scheme (hash blocks → hash concatenation), why incremental | Connects to Merkle tree concept, explains why hash before compress, discusses collision probability |
| **Resumability** | "Retry on failure" | Designs upload session protocol (start/append/finish), offset tracking | Discusses session persistence across app crashes, session expiry, idempotent retry semantics |
| **Security** | Not mentioned | Mentions that dedup could leak information | Designs complete threat model (confirmation attacks, timing side-channels), discusses convergent encryption and per-team encryption trade-offs |
| **Contrast** | None | Contrasts with S3 multipart (no dedup) | Three-way comparison: Dropbox (dedup + delta), S3 (reliability only), Git (CAS + delta packs) — explains how each reflects its product's priorities |

---

> **Summary:** The upload pipeline is Dropbox's most complex client-side operation and the foundation of its storage economics. Block-level deduplication (via content-addressable storage) eliminates redundant storage across file versions, across users, and across the entire system — reportedly saving 50-60% of total storage at Dropbox's exabyte scale. Combined with delta sync (only upload changed blocks) and resumable uploads (survive network failures), the upload pipeline transforms a naive "upload entire file on every change" into an optimized "upload only the unique, changed bytes." This is the technical moat that makes Dropbox's business model viable.
