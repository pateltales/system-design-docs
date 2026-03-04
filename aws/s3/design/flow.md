# Amazon S3 — System Flows

> Companion deep dive to the [interview simulation](interview-simulation.md). This document traces 12 end-to-end system flows through S3's architecture, from client request to response.

---

## Table of Contents

1. [PUT Object (Simple Upload)](#1-put-object-simple-upload)
2. [GET Object (with Consistency Check)](#2-get-object-with-consistency-check)
3. [GET Object with Byte-Range Request](#3-get-object-with-byte-range-request)
4. [DELETE Object](#4-delete-object)
5. [LIST Objects](#5-list-objects)
6. [Multipart Upload (Complete Flow)](#6-multipart-upload-complete-flow)
7. [Cross-Region Replication Flow](#7-cross-region-replication-flow)
8. [Storage Class Transition Flow](#8-storage-class-transition-flow)
9. [Presigned URL Flow](#9-presigned-url-flow)
10. [Event Notification Flow](#10-event-notification-flow)
11. [Data Integrity Check (Background Scrubbing)](#11-data-integrity-check-background-scrubbing)
12. [AZ Failure and Recovery](#12-az-failure-and-recovery)
13. [Flow Summary Table](#13-flow-summary-table)

---

## 1. PUT Object (Simple Upload)

The PUT Object flow is the foundational write path for S3. A single PUT request can
upload objects up to 5 GB (objects larger than ~100 MB should use multipart upload).

### 1.1 Sequence Diagram

```
Client                Front-End           Witness          Metadata           Data Layer
  |                      |                   |               |                   |
  |--PUT /key ---------->|                   |               |                   |
  |  (headers + body)    |                   |               |                   |
  |                      |--authenticate-----|               |                   |
  |                      |  (SigV4 verify)   |               |                   |
  |                      |--authorize--------|               |                   |
  |                      |  (IAM + bucket    |               |                   |
  |                      |   policy check)   |               |                   |
  |                      |                   |               |                   |
  |                      |--compute checksum-|               |                   |
  |                      |  (MD5 / SHA-256 / |               |                   |
  |                      |   CRC32C of body) |               |                   |
  |                      |                   |               |                   |
  |                      |--erasure encode---|---------------|--store chunks---->|
  |                      |  (8 data +        |               |  (11 chunks to   |
  |                      |   3 parity)       |               |   11 nodes,      |
  |                      |                   |               |   3 AZs)         |
  |                      |                   |               |                   |
  |                      |                   |               |<--chunk ACKs-----|
  |                      |                   |               |  (wait for       |
  |                      |                   |               |   sufficient     |
  |                      |                   |               |   ACKs)          |
  |                      |                   |               |                   |
  |                      |--write metadata-->|               |                   |
  |                      |  (etag, size,     |               |                   |
  |                      |   chunk_map,      |               |                   |
  |                      |   storage_class,  |               |                   |
  |                      |   user_metadata)  |               |                   |
  |                      |                   |               |                   |
  |                      |--update witness-->|               |                   |
  |                      |  (key->version_id)|               |                   |
  |                      |<--ACK------------|               |                   |
  |                      |                   |               |                   |
  |<--200 OK ------------|                   |               |                   |
  |  (ETag, version-id)  |                   |               |                   |
```

### 1.2 Step-by-Step with Latencies

| Step | Operation | Latency | Details |
|------|-----------|---------|---------|
| 1 | Client sends PUT over HTTPS | Variable | Depends on object size and network. For a 1 MB object on a 1 Gbps link: ~8ms for data transfer alone. TLS handshake adds ~5ms if not reusing a connection. |
| 2 | Front-end authenticates (SigV4) | ~0.5ms | Parses Authorization header, reconstructs canonical request, computes HMAC-SHA256 signature, compares. Signing keys are cached in memory. |
| 3 | Front-end authorizes (IAM + bucket policy) | ~1ms | Evaluates IAM user/role policies, bucket policy, bucket ACL, and any VPC endpoint policies. Policy documents are cached locally and refreshed periodically. |
| 4 | Compute body checksum | ~1-5ms | For small objects (< 1 MB), compute MD5/SHA-256 of the body. If client sent `Content-MD5` or `x-amz-checksum-sha256`, verify it matches. Reject with `400 BadDigest` on mismatch. |
| 5 | Erasure encode | ~1-5ms | Split the object into 8 data chunks. Compute 3 parity chunks via Reed-Solomon. For a 1 MB object, each chunk is ~128 KB. For a 100 MB object, each chunk is ~12.5 MB. |
| 6 | Write 11 chunks to storage nodes | ~10-30ms | Send chunks to 11 storage nodes across 3 AZs in parallel. Intra-AZ latency ~0.5ms, cross-AZ latency ~1-2ms. The wall-clock time is dominated by the slowest write. Each storage node writes the chunk to disk and computes a local checksum. |
| 7 | Wait for sufficient chunk ACKs | Included above | Typically wait for all 11 ACKs. In degraded mode, proceed if at least 8 data + sufficient parity ACKs have arrived (enough for reconstruction). Remaining chunks are written asynchronously. |
| 8 | Write metadata to metadata store | ~2-5ms | Write to the partition that owns this key (prefix-based routing). The metadata entry includes: ETag, object size, storage class, chunk_map (list of chunk_id -> node_id mappings), user metadata, creation timestamp. Cross-AZ write for durability. |
| 9 | Update witness | ~0.5ms | Write `(bucket+key) -> version_id` to the witness register. This is the linearization point: once the witness is updated, any subsequent GET will see this version. |
| 10 | Return 200 OK | ~0.1ms | Response includes `ETag` (MD5 of object, or MD5 of part ETags for multipart) and `x-amz-version-id` if versioning is enabled. |

### 1.3 Latency Summary

- **Small object (< 1 MB):** ~20-50ms end-to-end
- **Medium object (10 MB):** ~50-200ms (dominated by data transfer and chunk writes)
- **Large object (100 MB):** ~1-5 seconds (should use multipart upload instead)
- **Very large object (5 GB, max for single PUT):** ~30-60 seconds (strongly recommended to use multipart)

### 1.4 Failure Handling

| Failure | Response | Recovery |
|---------|----------|----------|
| Auth failure | `403 Forbidden` | Client fixes credentials/policies |
| Body checksum mismatch | `400 BadDigest` | Client retries upload |
| Chunk write failure (< 4 nodes) | Transparent retry to alternate node | Front-end selects replacement node, writes chunk there |
| Chunk write failure (>= 4 nodes) | `500 InternalError` | Client retries entire PUT (idempotent) |
| Metadata write failure | `500 InternalError` | Orphaned chunks cleaned up by GC; client retries |
| Witness update failure | `500 InternalError` | Metadata is written but invisible; GC handles orphan; client retries |

### 1.5 Important Ordering Guarantee

The write path commits in this exact order: **data chunks -> metadata -> witness**. This
ordering ensures that if a reader sees a version in the witness, the metadata and data
are guaranteed to already exist. Reversing the order (e.g., updating witness before data
is written) would create a window where a reader sees a version but the data does not yet
exist, resulting in a read error.

---

## 2. GET Object (with Consistency Check)

The GET Object flow is the primary read path. S3 guarantees strong read-after-write
consistency: after a successful PUT returns 200 OK, any subsequent GET is guaranteed to
return the new version (or a newer version, if another PUT happened in between).

### 2.1 Sequence Diagram (Slow Path — Cache Stale)

```
Client                Front-End           Witness          Metadata           Data Layer
  |                      |                   |               |                   |
  |--GET /key ---------->|                   |               |                   |
  |                      |--authenticate-----|               |                   |
  |                      |--authorize--------|               |                   |
  |                      |                   |               |                   |
  |                      |--check cache------|               |                   |
  |                      |  cached: v1       |               |                   |
  |                      |                   |               |                   |
  |                      |--check witness--->|               |                   |
  |                      |  "latest version  |               |                   |
  |                      |   of this key?"   |               |                   |
  |                      |<--"v2" ----------|               |                   |
  |                      |                   |               |                   |
  |                      |  cache=v1, witness=v2 --> STALE   |                   |
  |                      |                   |               |                   |
  |                      |--read metadata--->|-------------->|                   |
  |                      |  (fetch v2 meta)  |               |                   |
  |                      |<--metadata v2 ---|<--------------|                   |
  |                      |  (chunk_map,      |               |                   |
  |                      |   etag, size)     |               |                   |
  |                      |                   |               |                   |
  |                      |  update cache: v2 |               |                   |
  |                      |                   |               |                   |
  |                      |--fetch chunks---->|---------------|--read chunks----->|
  |                      |  (request 11,     |               |  (read from      |
  |                      |   use first 8)    |               |   11 nodes)      |
  |                      |                   |               |                   |
  |                      |<--8+ chunks ------|---------------|<-----------------|
  |                      |                   |               |                   |
  |                      |--decode + verify--|               |                   |
  |                      |  (reconstruct +   |               |                   |
  |                      |   checksum verify)|               |                   |
  |                      |                   |               |                   |
  |<--200 OK + body -----|                   |               |                   |
  |  (stream data)       |                   |               |                   |
```

### 2.2 Sequence Diagram (Fast Path — Cache Fresh)

```
Client                Front-End           Witness          Metadata           Data Layer
  |                      |                   |               |                   |
  |--GET /key ---------->|                   |               |                   |
  |                      |--authenticate-----|               |                   |
  |                      |--authorize--------|               |                   |
  |                      |                   |               |                   |
  |                      |--check cache------|               |                   |
  |                      |  cached: v2       |               |                   |
  |                      |                   |               |                   |
  |                      |--check witness--->|               |                   |
  |                      |  "latest version  |               |                   |
  |                      |   of this key?"   |               |                   |
  |                      |<--"v2" ----------|               |                   |
  |                      |                   |               |                   |
  |                      |  cache=v2, witness=v2 --> FRESH   |                   |
  |                      |                   |               |                   |
  |                      |  skip metadata read               |                   |
  |                      |  use cached chunk_map             |                   |
  |                      |                   |               |                   |
  |                      |--fetch chunks---->|---------------|--read chunks----->|
  |                      |  (request 11,     |               |  (use first 8    |
  |                      |   use first 8)    |               |   to arrive)     |
  |                      |                   |               |                   |
  |                      |<--8 chunks -------|---------------|<-----------------|
  |                      |                   |               |                   |
  |                      |--decode + verify--|               |                   |
  |                      |                   |               |                   |
  |<--200 OK + body -----|                   |               |                   |
```

### 2.3 Step-by-Step with Latencies

**Fast Path (cache is fresh):**

| Step | Operation | Latency |
|------|-----------|---------|
| 1 | Client sends GET over HTTPS | ~1ms (request is small) |
| 2 | Authenticate (SigV4) | ~0.5ms |
| 3 | Authorize (IAM + bucket policy) | ~1ms |
| 4 | Check local metadata cache | ~0.01ms (in-memory) |
| 5 | Check witness for latest version | ~0.5ms (lightweight RPC) |
| 6 | Cache matches witness — use cached chunk_map | ~0ms |
| 7 | Fetch chunks from storage nodes (parallel, hedged) | ~5-20ms |
| 8 | Decode erasure-coded data + verify checksum | ~1-3ms |
| 9 | Stream response to client | Variable (depends on object size) |

**Fast path total (small object): ~10-30ms first-byte latency**

**Slow Path (cache is stale):**

| Step | Operation | Latency |
|------|-----------|---------|
| 1-5 | Same as fast path through witness check | ~3ms |
| 6 | Cache is stale — fetch metadata from primary | ~2-5ms (cross-AZ) |
| 7 | Update local cache with new metadata | ~0.01ms |
| 8 | Fetch chunks from storage nodes (parallel, hedged) | ~5-20ms |
| 9 | Decode + verify | ~1-3ms |
| 10 | Stream response | Variable |

**Slow path total (small object): ~15-50ms first-byte latency**

### 2.4 Tail-Latency Hedging

When reading chunks, the front-end uses a technique called **hedged reads** to minimize
tail latency:

```
Front-end needs 8 of 11 chunks to reconstruct:

  Send request to all 11 chunk holders simultaneously:
    Node 1 (D1): responds in 3ms   --> USE
    Node 2 (D2): responds in 4ms   --> USE
    Node 3 (D3): responds in 2ms   --> USE
    Node 4 (D4): responds in 5ms   --> USE
    Node 5 (D5): responds in 3ms   --> USE
    Node 6 (D6): responds in 4ms   --> USE
    Node 7 (D7): responds in 15ms  --> USE
    Node 8 (D8): responds in 6ms   --> USE  (8th response -- can decode now)
    Node 9 (P1): responds in 20ms  --> DISCARD (not needed)
    Node 10(P2): responds in 50ms  --> CANCEL (not needed)
    Node 11(P3): timed out         --> CANCEL (not needed)

  Wall-clock time = time of 8th fastest = 6ms (not 50ms or timeout)
```

This means the p50 latency is dominated by the 8th-fastest of 11 parallel reads,
effectively cutting off the tail of the latency distribution. The 3 slowest nodes
(which might be experiencing disk I/O contention, GC pauses, or network congestion)
are simply ignored.

### 2.5 Conditional GET Support

S3 supports conditional GETs that can avoid data transfer entirely:

```
Client --> Front-End: GET /key
  If-None-Match: "etag-v2"

Front-End: current version has ETag "etag-v2" --> MATCH
Front-End --> Client: 304 Not Modified (no body transferred)

Latency: ~5-10ms (no chunk reads, no data transfer)
```

```
Client --> Front-End: GET /key
  If-Modified-Since: Wed, 21 Oct 2025 07:28:00 GMT

Front-End: object last modified after that date --> NO MATCH
Front-End proceeds with full GET, returns 200 OK + body
```

---

## 3. GET Object with Byte-Range Request

Byte-range requests allow clients to download a specific portion of an object rather than
the entire thing. This is critical for video streaming (seeking), resuming interrupted
downloads, and reading specific sections of large files (e.g., Parquet footer).

### 3.1 Sequence Diagram

```
Client                Front-End           Metadata           Data Layer
  |                      |                   |                   |
  |--GET /key ---------->|                   |                   |
  |  Range: bytes=       |                   |                   |
  |  0-999999            |                   |                   |
  |                      |                   |                   |
  |                      |--read metadata--->|                   |
  |                      |  (get chunk_map,  |                   |
  |                      |   object size)    |                   |
  |                      |<--chunk_map ------|                   |
  |                      |                   |                   |
  |                      |--compute chunk    |                   |
  |                      |  mapping:         |                   |
  |                      |  Object: 8 MB     |                   |
  |                      |  8 data chunks    |                   |
  |                      |  each 1 MB        |                   |
  |                      |  Bytes 0-999999   |                   |
  |                      |  = chunk D1       |                   |
  |                      |  (no decode       |                   |
  |                      |   needed!)        |                   |
  |                      |                   |                   |
  |                      |--fetch chunk D1-->|------------------>|
  |                      |  (single chunk    |                   |
  |                      |   read)           |                   |
  |                      |<--chunk D1 data---|<-----------------|
  |                      |                   |                   |
  |                      |--verify checksum--|                   |
  |                      |  of chunk D1      |                   |
  |                      |                   |                   |
  |<--206 Partial -------|                   |                   |
  |  Content             |                   |                   |
  |  Content-Range:      |                   |                   |
  |  bytes 0-999999/     |                   |                   |
  |  8388608             |                   |                   |
```

### 3.2 Chunk Mapping Logic

The key insight is that with erasure coding (8+3 scheme), the original object is split
into 8 data chunks in order. Each data chunk maps to a contiguous byte range of the
original object:

```
Object: 8 MB (8,388,608 bytes)
Chunk size: 8 MB / 8 = 1 MB (1,048,576 bytes) per data chunk

Chunk D1: bytes [0 .. 1,048,575]           --> Storage Node 1
Chunk D2: bytes [1,048,576 .. 2,097,151]   --> Storage Node 5
Chunk D3: bytes [2,097,152 .. 3,145,727]   --> Storage Node 9
Chunk D4: bytes [3,145,728 .. 4,194,303]   --> Storage Node 2
Chunk D5: bytes [4,194,304 .. 5,242,879]   --> Storage Node 6
Chunk D6: bytes [5,242,880 .. 6,291,455]   --> Storage Node 10
Chunk D7: bytes [6,291,456 .. 7,340,031]   --> Storage Node 3
Chunk D8: bytes [7,340,032 .. 8,388,607]   --> Storage Node 7

Parity:
Chunk P1: parity chunk                     --> Storage Node 11
Chunk P2: parity chunk                     --> Storage Node 4
Chunk P3: parity chunk                     --> Storage Node 8
```

**Range request mapping examples:**

| Range Requested | Chunks Needed | Full Decode Required? |
|-----------------|---------------|----------------------|
| `bytes=0-999999` | D1 only | No -- read D1 directly, return first 1,000,000 bytes |
| `bytes=0-2097151` | D1, D2 | No -- read D1 and D2 directly, concatenate |
| `bytes=500000-1500000` | D1, D2 | No -- read D1 (partial), D2 (partial), concatenate |
| `bytes=7000000-8388607` | D7, D8 | No -- read D7 (partial), D8, concatenate |
| Entire object | D1-D8 (or any 8 of 11) | Technically yes if using parity, but no if all data chunks are available |

### 3.3 Degraded Byte-Range Read

If the specific chunk needed for a byte-range request is unavailable (node down, chunk
corrupted), the front-end falls back to a full erasure decode:

```
Request: Range: bytes=0-999999 (needs chunk D1)
Chunk D1 is UNAVAILABLE (Node 1 is down)

Fallback:
  Fetch any 8 of the remaining 10 chunks (D2-D8, P1-P3)
  Perform full erasure decode to reconstruct all 8 data chunks
  Extract bytes 0-999999 from reconstructed D1
  Return to client

Latency impact: ~2-3x slower due to full decode instead of single chunk read
```

### 3.4 Latency Estimates

| Scenario | Latency | Why |
|----------|---------|-----|
| Single chunk range, cache fresh | ~5-15ms | Witness check + single chunk read |
| Multi-chunk range (2-3 chunks) | ~8-20ms | Parallel chunk reads, no decode needed |
| Degraded range (chunk unavailable) | ~15-40ms | Full decode required |
| Large range (entire object, 1 GB) | ~seconds | Dominated by data transfer |

---

## 4. DELETE Object

DELETE has two distinct behaviors depending on whether versioning is enabled on the bucket.

### 4.1 DELETE Without Versioning

```
Client                Front-End           Witness          Metadata           Data Layer
  |                      |                   |               |                   |
  |--DELETE /key ------->|                   |               |                   |
  |                      |--authenticate-----|               |                   |
  |                      |--authorize--------|               |                   |
  |                      |                   |               |                   |
  |                      |--write tombstone->|               |                   |
  |                      |  to metadata      |               |                   |
  |                      |  (mark as deleted)|               |                   |
  |                      |                   |               |                   |
  |                      |--update witness-->|               |                   |
  |                      |  (key -> deleted) |               |                   |
  |                      |                   |               |                   |
  |<--204 No Content ----|                   |               |                   |
  |                      |                   |               |                   |
  |                      |                   |               |                   |
  |   ===== ASYNC GARBAGE COLLECTION (minutes to hours later) =====            |
  |                      |                   |               |                   |
  |                      GC Service          |               |                   |
  |                      |--scan tombstones->|               |                   |
  |                      |                   |               |                   |
  |                      |--look up chunk----|-------------->|                   |
  |                      |  map for object   |               |                   |
  |                      |                   |               |                   |
  |                      |--delete chunks----|---------------|--delete from----->|
  |                      |  from 11 nodes    |               |  local disk      |
  |                      |                   |               |                   |
  |                      |--remove tombstone>|               |                   |
  |                      |  from metadata    |               |                   |
```

**Step-by-step:**

| Step | Operation | Latency | Sync/Async |
|------|-----------|---------|------------|
| 1 | Authenticate + authorize | ~1.5ms | Synchronous |
| 2 | Write tombstone to metadata | ~2-5ms | Synchronous |
| 3 | Update witness (key -> deleted) | ~0.5ms | Synchronous |
| 4 | Return 204 No Content | ~0.1ms | Synchronous |
| 5 | GC scans for tombstones | Minutes-hours | Asynchronous |
| 6 | GC deletes data chunks from storage nodes | Seconds per object | Asynchronous |
| 7 | GC removes tombstone from metadata after retention | Hours-days | Asynchronous |

**Total synchronous latency: ~5-10ms**

### 4.2 DELETE With Versioning Enabled

```
Client                Front-End           Witness          Metadata
  |                      |                   |               |
  |--DELETE /key ------->|                   |               |
  |  (no version-id)     |                   |               |
  |                      |--authenticate-----|               |
  |                      |--authorize--------|               |
  |                      |                   |               |
  |                      |  Versioning is ON |               |
  |                      |  --> insert delete |               |
  |                      |  marker, do NOT   |               |
  |                      |  remove data      |               |
  |                      |                   |               |
  |                      |--write delete---->|               |
  |                      |  marker as new    |               |
  |                      |  version (v4)     |               |
  |                      |                   |               |
  |                      |  Version chain:   |               |
  |                      |  v4: DELETE MARKER|               |
  |                      |  v3: data (12 KB) |               |
  |                      |  v2: data (8 KB)  |               |
  |                      |  v1: data (5 KB)  |               |
  |                      |                   |               |
  |                      |--update witness-->|               |
  |                      |  (key -> v4,      |               |
  |                      |   type=delete)    |               |
  |                      |                   |               |
  |<--204 No Content ----|                   |               |
  |  x-amz-delete-       |                   |               |
  |  marker: true        |                   |               |
  |  x-amz-version-id:   |                   |               |
  |  v4                  |                   |               |
```

**Key points about versioned DELETE:**
- Data chunks for v1, v2, v3 are NOT deleted. They remain on storage nodes.
- A GET /key (without version-id) now returns 404 because the latest version is a delete marker.
- A GET /key?versionId=v3 still returns the v3 data successfully.
- To permanently delete a specific version: `DELETE /key?versionId=v3` removes v3's metadata and triggers GC for v3's data chunks.

### 4.3 Permanent Version Delete

```
Client                Front-End           Metadata           Data Layer
  |                      |                   |                   |
  |--DELETE /key ------->|                   |                   |
  |  ?versionId=v2       |                   |                   |
  |                      |                   |                   |
  |                      |--remove v2 from-->|                   |
  |                      |  version chain    |                   |
  |                      |                   |                   |
  |                      |  Version chain:   |                   |
  |                      |  v4: DELETE MARKER|                   |
  |                      |  v3: data (12 KB) |                   |
  |                      |  (v2 removed)     |                   |
  |                      |  v1: data (5 KB)  |                   |
  |                      |                   |                   |
  |<--204 No Content ----|                   |                   |
  |  x-amz-version-id:v2 |                   |                   |
  |                      |                   |                   |
  |   ===== ASYNC GC =====                  |                   |
  |                      GC Service          |                   |
  |                      |--delete v2 chunks-|------------------>|
  |                      |  from 11 nodes    |                   |
```

---

## 5. LIST Objects

LIST is the most expensive core operation because it requires range-scanning the metadata
index rather than a point lookup. S3's prefix-based auto-partitioning makes this efficient
for well-structured key prefixes.

### 5.1 Sequence Diagram

```
Client                Front-End                  Metadata (Partitions)
  |                      |                          |
  |--GET /?list-type=2-->|                          |
  |  &prefix=photos/    |                          |
  |  &delimiter=/       |                          |
  |  &max-keys=1000     |                          |
  |                      |                          |
  |                      |--authenticate------------|
  |                      |--authorize---------------|
  |                      |                          |
  |                      |--lookup partition-------->|
  |                      |  map for prefix          |
  |                      |  'photos/'               |
  |                      |                          |
  |                      |  --> Partitions P3, P4   |
  |                      |  cover this prefix range |
  |                      |                          |
  |                      |--range scan P3---------->|  Partition P3
  |                      |  prefix='photos/'        |  [photos/a* .. photos/m*]
  |                      |  delimiter='/'           |
  |                      |  max-keys=1000           |
  |                      |                          |
  |                      |--range scan P4---------->|  Partition P4
  |                      |  (same params)           |  [photos/n* .. photos/z*]
  |                      |                          |  (parallel with P3)
  |                      |                          |
  |                      |<--results P3:------------|
  |                      |  photos/beach.jpg        |
  |                      |  photos/city.jpg         |
  |                      |  photos/2024/img1.jpg    |
  |                      |  photos/2024/img2.jpg    |
  |                      |  photos/dogs/             |
  |                      |  ...                     |
  |                      |                          |
  |                      |<--results P4:------------|
  |                      |  photos/sunset.jpg       |
  |                      |  photos/vacation/         |
  |                      |  ...                     |
  |                      |                          |
  |                      |--merge sort results------|
  |                      |  by key (lexicographic)  |
  |                      |                          |
  |                      |--apply delimiter logic---|
  |                      |  (collapse sub-prefixes  |
  |                      |   into CommonPrefixes)   |
  |                      |                          |
  |                      |--truncate to 1000 keys---|
  |                      |  (set IsTruncated=true   |
  |                      |   if more remain)        |
  |                      |                          |
  |<--200 OK ------------|                          |
  |  <ListBucketResult>  |                          |
  |    <Contents>...     |                          |
  |    <CommonPrefixes>  |                          |
  |    <NextContToken>   |                          |
```

### 5.2 Delimiter Processing and CommonPrefixes

The delimiter is what makes S3's flat namespace feel like a directory hierarchy. Here
is how it works:

```
Stored keys:
  photos/beach.jpg
  photos/city.jpg
  photos/2024/january/img1.jpg
  photos/2024/january/img2.jpg
  photos/2024/february/img3.jpg
  photos/dogs/rex.jpg
  photos/dogs/buddy.jpg
  photos/sunset.jpg

LIST request: prefix=photos/ delimiter=/

Step 1: Find all keys matching prefix 'photos/'
  --> all 8 keys above match

Step 2: For each key, look for the delimiter '/' AFTER the prefix:
  photos/beach.jpg         --> no '/' after 'photos/' before end --> CONTENT
  photos/city.jpg          --> no '/' after 'photos/' before end --> CONTENT
  photos/2024/january/... --> '/' found at 'photos/2024/' --> COMMON PREFIX: 'photos/2024/'
  photos/2024/february/.. --> '/' found at 'photos/2024/' --> COMMON PREFIX: 'photos/2024/'
  photos/dogs/rex.jpg      --> '/' found at 'photos/dogs/' --> COMMON PREFIX: 'photos/dogs/'
  photos/dogs/buddy.jpg    --> '/' found at 'photos/dogs/' --> COMMON PREFIX: 'photos/dogs/'
  photos/sunset.jpg        --> no '/' after 'photos/' before end --> CONTENT

Step 3: Deduplicate CommonPrefixes:
  COMMON PREFIXES: ['photos/2024/', 'photos/dogs/']
  CONTENTS: ['photos/beach.jpg', 'photos/city.jpg', 'photos/sunset.jpg']

Response:
  <ListBucketResult>
    <Contents>
      <Key>photos/beach.jpg</Key>
      <Key>photos/city.jpg</Key>
      <Key>photos/sunset.jpg</Key>
    </Contents>
    <CommonPrefixes>
      <Prefix>photos/2024/</Prefix>
      <Prefix>photos/dogs/</Prefix>
    </CommonPrefixes>
  </ListBucketResult>
```

### 5.3 Pagination with Continuation Tokens

```
First request:
  GET /?list-type=2&prefix=logs/&max-keys=1000

Response (1000 keys returned, more exist):
  <IsTruncated>true</IsTruncated>
  <NextContinuationToken>encoded_opaque_token_ABC</NextContinuationToken>

  The token encodes: last_key_returned = "logs/2024-06-15-request-999.log"
  (opaque to client -- S3 can encode it however it wants)

Second request:
  GET /?list-type=2&prefix=logs/&max-keys=1000&continuation-token=encoded_opaque_token_ABC

Front-end decodes token:
  Resume range scan from key > "logs/2024-06-15-request-999.log"
  Return next 1000 keys

Response (500 keys returned, no more):
  <IsTruncated>false</IsTruncated>
  (no NextContinuationToken)
```

### 5.4 Latency Estimates

| Scenario | Latency | Notes |
|----------|---------|-------|
| Small prefix, 1 partition, 100 keys | ~10-30ms | Single partition scan |
| Medium prefix, 2 partitions, 1000 keys | ~50-100ms | Parallel scans + merge |
| Large prefix, 10 partitions, 1000 keys | ~100-300ms | Fan-out to many partitions + merge |
| Full bucket scan (no prefix), 1000 keys | ~200-500ms | May touch many partitions |
| Bucket with billions of keys, full scan | Seconds-minutes per page | Pagination required |

---

## 6. Multipart Upload (Complete Flow)

Multipart upload is the mechanism for uploading large objects (typically > 100 MB, up to
5 TB). It splits the upload into independently uploadable parts that can be parallelized
and retried individually.

### 6.1 Phase 1: Initiate Multipart Upload

```
Client                Front-End           Metadata
  |                      |                   |
  |--POST /key?uploads-->|                   |
  |  Content-Type:       |                   |
  |  application/zip     |                   |
  |                      |--authenticate-----|
  |                      |--authorize--------|
  |                      |                   |
  |                      |--create temp----->|
  |                      |  upload entry:    |
  |                      |  upload_id=abc123 |
  |                      |  key=my-object    |
  |                      |  initiated=now()  |
  |                      |  parts={}         |
  |                      |                   |
  |<--200 OK ------------|                   |
  |  <UploadId>          |                   |
  |  abc123              |                   |
  |  </UploadId>         |                   |

Latency: ~5-10ms
```

### 6.2 Phase 2: Upload Parts (Parallel)

```
Client                Front-End           Metadata           Data Layer
  |                      |                   |                   |
  |==== Part 1 (100 MB) ======================================================|
  |--PUT /key -----------|                   |                   |
  |  ?partNumber=1       |                   |                   |
  |  &uploadId=abc123    |                   |                   |
  |  Body: <100 MB>      |                   |                   |
  |                      |--validate---------|                   |
  |                      |  upload_id exists  |                   |
  |                      |                   |                   |
  |                      |--erasure encode---|                   |
  |                      |  100 MB -> 8 data |                   |
  |                      |  chunks (12.5 MB  |                   |
  |                      |  each) + 3 parity |                   |
  |                      |                   |                   |
  |                      |--store chunks-----|------------------>|
  |                      |  (11 chunks to    |                   |
  |                      |   11 nodes, 3 AZs)|                   |
  |                      |                   |                   |
  |                      |--record part----->|                   |
  |                      |  metadata:        |                   |
  |                      |  part_number=1    |                   |
  |                      |  etag="etag1"     |                   |
  |                      |  size=100MB       |                   |
  |                      |  chunk_map=[...]  |                   |
  |                      |                   |                   |
  |<--200 OK ------------|                   |                   |
  |  ETag: "etag1"       |                   |                   |
  |                      |                   |                   |
  |==== Part 2 (100 MB) === IN PARALLEL ===================================== |
  |--PUT /key -----------|                   |                   |
  |  ?partNumber=2       |                   |                   |
  |  &uploadId=abc123    |                   |                   |
  |  Body: <100 MB>      |                   |                   |
  |                      | (same flow as     |                   |
  |                      |  part 1)          |                   |
  |<--200 OK ------------|                   |                   |
  |  ETag: "etag2"       |                   |                   |
  |                      |                   |                   |
  |==== Part 3 (50 MB) === IN PARALLEL ====================================== |
  |--PUT /key -----------|                   |                   |
  |  ?partNumber=3       |                   |                   |
  |  &uploadId=abc123    |                   |                   |
  |  Body: <50 MB>       |                   |                   |
  |                      | (same flow)       |                   |
  |<--200 OK ------------|                   |                   |
  |  ETag: "etag3"       |                   |                   |

Latency per part: dominated by data transfer time
  100 MB on 1 Gbps: ~800ms transfer + ~50-200ms S3 processing
  With 10 Gbps: ~80ms transfer + ~50-200ms S3 processing
```

### 6.3 Phase 3: Complete Multipart Upload

```
Client                Front-End           Witness          Metadata           Data Layer
  |                      |                   |               |                   |
  |--POST /key?--------->|                   |               |                   |
  |  uploadId=abc123     |                   |               |                   |
  |  Body:               |                   |               |                   |
  |  <Part>              |                   |               |                   |
  |   <PartNumber>1      |                   |               |                   |
  |   </PartNumber>      |                   |               |                   |
  |   <ETag>etag1</ETag> |                   |               |                   |
  |  </Part>             |                   |               |                   |
  |  <Part>              |                   |               |                   |
  |   <PartNumber>2      |                   |               |                   |
  |   </PartNumber>      |                   |               |                   |
  |   <ETag>etag2</ETag> |                   |               |                   |
  |  </Part>             |                   |               |                   |
  |  <Part>              |                   |               |                   |
  |   <PartNumber>3      |                   |               |                   |
  |   </PartNumber>      |                   |               |                   |
  |   <ETag>etag3</ETag> |                   |               |                   |
  |  </Part>             |                   |               |                   |
  |                      |                   |               |                   |
  |                      |--validate---------|               |                   |
  |                      |  all part ETags   |               |                   |
  |                      |  match stored     |               |                   |
  |                      |  values           |               |                   |
  |                      |                   |               |                   |
  |                      |--compose final    |               |                   |
  |                      |  object metadata: |               |                   |
  |                      |  merge chunk_maps |               |                   |
  |                      |  from parts 1,2,3 |               |                   |
  |                      |                   |               |                   |
  |                      |--compute final    |               |                   |
  |                      |  ETag:            |               |                   |
  |                      |  MD5(etag1+etag2  |               |                   |
  |                      |  +etag3) + "-3"   |               |                   |
  |                      |                   |               |                   |
  |                      |--write object---->|               |                   |
  |                      |  metadata to      |               |                   |
  |                      |  primary store    |               |                   |
  |                      |                   |               |                   |
  |                      |--update witness-->|               |                   |
  |                      |  (key->version_id)|               |                   |
  |                      |                   |               |                   |
  |                      |--delete temp----->|               |                   |
  |                      |  upload metadata  |               |                   |
  |                      |  (upload_id entry)|               |                   |
  |                      |                   |               |                   |
  |<--200 OK ------------|                   |               |                   |
  |  ETag: "abc...-3"    |                   |               |                   |
  |  x-amz-version-id    |                   |               |                   |
```

### 6.4 Abort Multipart Upload

```
Client                Front-End           Metadata           Data Layer
  |                      |                   |                   |
  |--DELETE /key?------->|                   |                   |
  |  uploadId=abc123     |                   |                   |
  |                      |                   |                   |
  |                      |--mark upload----->|                   |
  |                      |  as aborted       |                   |
  |                      |                   |                   |
  |<--204 No Content ----|                   |                   |
  |                      |                   |                   |
  |   ===== ASYNC CLEANUP =====            |                   |
  |                      |                   |                   |
  |                      GC Service          |                   |
  |                      |--find all parts-->|                   |
  |                      |  for upload_id    |                   |
  |                      |                   |                   |
  |                      |--delete chunks----|------------------>|
  |                      |  for each part    |                   |
  |                      |                   |                   |
  |                      |--delete upload--->|                   |
  |                      |  metadata entry   |                   |
```

### 6.5 Incomplete Upload Lifecycle Cleanup

Incomplete multipart uploads consume storage (parts are stored but the object is never
finalized). S3 supports lifecycle rules to automatically abort stale uploads:

```
Lifecycle Rule:
  <AbortIncompleteMultipartUpload>
    <DaysAfterInitiation>7</DaysAfterInitiation>
  </AbortIncompleteMultipartUpload>

Lifecycle Evaluator (daily scan):
  1. Scan temporary upload entries in metadata
  2. Find uploads where: now() - initiated_at > 7 days AND status != completed
  3. For each stale upload:
     a. Mark as aborted
     b. Queue part chunks for GC deletion
     c. Remove temporary metadata

  This prevents storage leaks from abandoned uploads (e.g., client crashes mid-upload).
```

### 6.6 Latency Summary

| Phase | Latency | Notes |
|-------|---------|-------|
| Initiate | ~5-10ms | Metadata write only |
| Upload Part (100 MB) | ~1-5 seconds | Dominated by data transfer |
| Upload Part (5 GB, max) | ~40-60 seconds | Large data transfer |
| Complete (3 parts) | ~10-50ms | Metadata composition + write |
| Complete (10,000 parts) | ~100-500ms | More ETags to validate, larger metadata |
| Abort | ~5-10ms | Metadata update only; GC is async |

---

## 7. Cross-Region Replication Flow

Cross-Region Replication (CRR) asynchronously copies objects from a source bucket in one
AWS region to a destination bucket in another region. It is used for compliance, latency
reduction, and disaster recovery.

### 7.1 Standard Replication Flow

```
Source Region (us-east-1)                          Dest Region (eu-west-1)
  |                                                   |
  Client --> PUT /key --> 200 OK                      |
  |                                                   |
  S3 Internal Event Stream:                           |
  {                                                   |
    "eventName": "s3:ObjectCreated:Put",              |
    "bucket": "my-bucket",                            |
    "key": "reports/2025-Q4.pdf",                     |
    "versionId": "v5",                                |
    "size": 52428800                                  |
  }                                                   |
  |                                                   |
  Replication Controller:                             |
  |--match against replication rules                  |
  |  Rule: prefix "reports/" --> replicate            |
  |  to eu-west-1:my-bucket-replica                   |
  |                                                   |
  Replication Worker:                                 |
  |--GET /key?versionId=v5 from source                |
  |  (internal read, bypasses public API)             |
  |                                                   |
  |--PUT /key to destination bucket ---------------->|
  |  Headers:                                         |
  |  x-amz-replication-status: REPLICA                |
  |  x-amz-version-id: v5 (preserve version)         |
  |  x-amz-meta-* (preserve all user metadata)       |
  |  Content-Type (preserve)                          |
  |  Storage class (as configured in rule)            |
  |                                                   |
  |  Data: <50 MB object body, transferred            |
  |  over AWS backbone network>                       |
  |                                                   |
  |<--200 OK ----------------------------------------|
  |                                                   |
  Source metadata updated:                            |
  x-amz-replication-status: COMPLETED                |
  |                                                   |
  Dest metadata:                                      |
  x-amz-replication-status: REPLICA                   |
```

### 7.2 Replication Time Control (RTC)

Standard CRR is best-effort with no SLA on replication time. Replication Time Control
provides a 15-minute SLA:

```
Without RTC:
  PUT at T=0 --> replicated "eventually" (usually minutes, can be hours)
  No SLA, no monitoring

With RTC enabled ($$$):
  PUT at T=0
  |
  T+30s:  Replication worker picks up event (prioritized queue)
  T+60s:  Worker begins cross-region transfer
  T+5min: Object arrives in destination region
  T+5min: Source status: COMPLETED
  |
  SLA: 99.99% of objects replicated within 15 minutes

  CloudWatch Metrics:
  - ReplicationLatency: time from PUT to COMPLETED (seconds)
  - OperationsPendingReplication: count of objects waiting
  - OperationsFailedReplication: count of failed replications
  - BytesPendingReplication: bytes waiting to transfer

  S3 Replication Metrics (enabled with RTC):
  If replication exceeds 15 minutes:
    --> S3 emits s3:Replication:OperationMissedThreshold event
    --> Triggers SNS notification (if configured)
```

### 7.3 Replication of DELETE Operations

```
Versioning-enabled source bucket:

Case 1: DELETE /key (no version-id) --> creates delete marker
  Source: delete marker v6 created
  Replication: delete marker IS replicated to destination
  Destination: delete marker v6 created
  Result: object appears deleted in both regions

Case 2: DELETE /key?versionId=v3 --> permanent version delete
  Source: v3 permanently deleted
  Replication: permanent deletes are NOT replicated by default
  Destination: v3 still exists!
  Reason: prevents accidental cross-region data loss from malicious deletes

  To replicate permanent deletes:
  Set DeleteMarkerReplication and include "delete" in replication config
  (opt-in, not default, for safety)
```

### 7.4 Latency Estimates

| Scenario | Replication Latency | Notes |
|----------|-------------------|-------|
| Small object (< 1 MB), same continent | 30s - 2 min | Event propagation + transfer |
| Medium object (50 MB), cross-continent | 1 - 5 min | Data transfer over backbone |
| Large object (5 GB), cross-continent | 5 - 15 min | Dominated by data transfer |
| With RTC enabled | SLA: < 15 min for 99.99% | Prioritized worker queue |
| Burst (1M objects created) | Minutes to hours | Replication workers scale up, but queue builds |

---

## 8. Storage Class Transition Flow

S3 Lifecycle rules automatically transition objects between storage classes to optimize
cost. This is a background process that runs continuously.

### 8.1 Metadata-Only Transition (Standard to Standard-IA)

```
Lifecycle Evaluator                Metadata            Data Layer
  |                                   |                   |
  |--scan partition P3 ------------->|                   |
  |  (daily partition scan)          |                   |
  |                                   |                   |
  |<--objects matching rule:---------|                   |
  |  key: "logs/app-2024-01.log"     |                   |
  |  age: 35 days                    |                   |
  |  current class: STANDARD         |                   |
  |  rule: transition to IA at 30d   |                   |
  |                                   |                   |
  |--check: is transition valid?     |                   |
  |  - object size >= 128 KB? YES    |                   |
  |  - min 30 days in current class? |                   |
  |    YES (35 days)                 |                   |
  |                                   |                   |
  |--update metadata:--------------->|                   |
  |  storage_class: STANDARD_IA      |                   |
  |  (ONLY metadata change --        |                   |
  |   physical data location         |                   |
  |   does NOT change)               |                   |
  |                                   |                   |
  |  Data chunks remain on the       |                   |
  |  same storage nodes, same        |                   |
  |  disks, same AZs.                |                   |
  |                                   |                   |
  |  What changes:                   |                   |
  |  - Billing: lower storage $/GB   |                   |
  |  - Billing: per-GB retrieval fee |                   |
  |  - Min storage duration: 30 days |                   |
  |  - Min object size: 128 KB       |                   |
```

### 8.2 Data-Movement Transition (Standard to Glacier)

```
Lifecycle Evaluator                Metadata            Data Layer (Hot)      Data Layer (Cold)
  |                                   |                   |                      |
  |--scan partition P3 ------------->|                   |                      |
  |                                   |                   |                      |
  |<--objects matching rule:---------|                   |                      |
  |  key: "archive/report-2023.zip"  |                   |                      |
  |  age: 95 days                    |                   |                      |
  |  current class: STANDARD         |                   |                      |
  |  rule: transition to GLACIER     |                   |                      |
  |  at 90 days                      |                   |                      |
  |                                   |                   |                      |
  |--read current chunks----------->|------------------>|                      |
  |  (fetch all 11 chunks from       |                   |                      |
  |   hot storage nodes)             |                   |                      |
  |<--chunk data--------------------|<------------------|                      |
  |                                   |                   |                      |
  |--decode erasure coding           |                   |                      |
  |  (reconstruct original object)   |                   |                      |
  |                                   |                   |                      |
  |--re-encode for cold storage      |                   |                      |
  |  (possibly different EC scheme,  |                   |                      |
  |   optimized for cold: higher     |                   |                      |
  |   compression, deeper EC)        |                   |                      |
  |                                   |                   |                      |
  |--write to cold storage---------->|-------------------|--------------------->|
  |  backend (tape, cold HDD,        |                   |                      |
  |   or dedicated cold storage      |                   |                      |
  |   infrastructure)                |                   |                      |
  |                                   |                   |                      |
  |<--ACK from cold storage----------|-------------------|<--------------------|
  |                                   |                   |                      |
  |--update metadata:--------------->|                   |                      |
  |  storage_class: GLACIER          |                   |                      |
  |  chunk_map: new cold locations   |                   |                      |
  |                                   |                   |                      |
  |--delete old Standard chunks----->|------------------>|                      |
  |  (free hot storage space)        |                   |                      |
```

### 8.3 Glacier Restore Flow

Objects in Glacier cannot be read directly. A restore request must be issued first:

```
Client                Front-End           Metadata           Cold Storage        Hot Storage
  |                      |                   |                   |                   |
  |--POST /key?restore-->|                   |                   |                   |
  |  <Days>7</Days>      |                   |                   |                   |
  |  <Tier>Standard</Tier>                   |                   |                   |
  |                      |                   |                   |                   |
  |                      |--check class----->|                   |                   |
  |                      |  GLACIER --> OK   |                   |                   |
  |                      |                   |                   |                   |
  |                      |--initiate-------->|                   |                   |
  |                      |  restore job      |                   |                   |
  |                      |                   |                   |                   |
  |<--202 Accepted ------|                   |                   |                   |
  |  (restore in progress)                   |                   |                   |
  |                      |                   |                   |                   |
  |  ==== ASYNC RESTORE (3-5 hours for Standard tier) ====     |                   |
  |                      |                   |                   |                   |
  |                      Restore Worker      |                   |                   |
  |                      |--read from cold-->|------------------>|                   |
  |                      |  storage          |                   |                   |
  |                      |<--object data ----|<-----------------|                   |
  |                      |                   |                   |                   |
  |                      |--write temp copy->|-------------------|------------------>|
  |                      |  to hot storage   |                   |                   |
  |                      |  (Standard class) |                   |                   |
  |                      |                   |                   |                   |
  |                      |--update metadata->|                   |                   |
  |                      |  restore_status:  |                   |                   |
  |                      |  completed        |                   |                   |
  |                      |  restore_expiry:  |                   |                   |
  |                      |  now() + 7 days   |                   |                   |
  |                      |                   |                   |                   |
  |  ==== CLIENT CAN NOW GET THE OBJECT ====|                   |                   |
  |                      |                   |                   |                   |
  |--GET /key ---------->|                   |                   |                   |
  |                      |  (reads from      |                   |                   |
  |                      |   hot temp copy)  |                   |                   |
  |<--200 OK + body -----|                   |                   |                   |
  |                      |                   |                   |                   |
  |  ==== AFTER 7 DAYS ====                 |                   |                   |
  |                      |                   |                   |                   |
  |                      Cleanup Service     |                   |                   |
  |                      |--delete temp----->|-------------------|------------------>|
  |                      |  hot copy         |                   |  (temp removed)   |
  |                      |                   |                   |                   |
  |                      |--update metadata->|                   |                   |
  |                      |  restore_status:  |                   |                   |
  |                      |  none             |                   |                   |
```

### 8.4 Restore Tier Latency

| Tier | Latency | Cost | Use Case |
|------|---------|------|----------|
| Expedited | 1-5 minutes | $$$ | Urgent access to archived data |
| Standard | 3-5 hours | $$ | Normal archive retrieval |
| Bulk | 5-12 hours | $ | Large-scale batch retrieval |
| Glacier Deep Archive Standard | 12 hours | $ | Cheapest long-term archive |
| Glacier Deep Archive Bulk | 48 hours | Cheapest | Lowest cost, can wait |

---

## 9. Presigned URL Flow

Presigned URLs allow application servers to grant time-limited access to S3 objects
without exposing AWS credentials to end users. The signature is computed client-side
(by the application server's SDK) and embedded in the URL.

### 9.1 Generation and Upload Flow

```
App Server               Client (Browser)             S3 (Front-End)
  |                          |                            |
  |  Step 1: App server      |                            |
  |  generates presigned URL |                            |
  |  using AWS SDK:          |                            |
  |                          |                            |
  |  url = s3.generate_      |                            |
  |  presigned_url(          |                            |
  |    method='PUT',         |                            |
  |    bucket='my-bucket',   |                            |
  |    key='uploads/file.zip'|                            |
  |    expires_in=3600,      |                            |
  |    conditions=[          |                            |
  |      content_length<100MB|                            |
  |    ]                     |                            |
  |  )                       |                            |
  |                          |                            |
  |  Result:                 |                            |
  |  https://my-bucket.      |                            |
  |  s3.amazonaws.com/       |                            |
  |  uploads/file.zip?       |                            |
  |  X-Amz-Algorithm=        |                            |
  |  AWS4-HMAC-SHA256&       |                            |
  |  X-Amz-Credential=      |                            |
  |  AKIA.../20260212/       |                            |
  |  us-east-1/s3/           |                            |
  |  aws4_request&           |                            |
  |  X-Amz-Date=             |                            |
  |  20260212T120000Z&       |                            |
  |  X-Amz-Expires=3600&    |                            |
  |  X-Amz-Signature=abc... |                            |
  |                          |                            |
  |--return URL to client -->|                            |
  |  (via your own API)      |                            |
  |                          |                            |
  |                          |  Step 2: Client uploads    |
  |                          |  directly to S3            |
  |                          |                            |
  |                          |--PUT /uploads/file.zip --->|
  |                          |  ?X-Amz-Signature=abc...   |
  |                          |  Body: <file data>         |
  |                          |                            |
  |                          |                            |--validate:
  |                          |                            |  1. Extract sig params
  |                          |                            |     from query string
  |                          |                            |  2. Check: not expired
  |                          |                            |     (current time <
  |                          |                            |      X-Amz-Date +
  |                          |                            |      X-Amz-Expires)
  |                          |                            |  3. Reconstruct canonical
  |                          |                            |     request from URL
  |                          |                            |  4. Compute signature
  |                          |                            |     using credentials
  |                          |                            |  5. Compare: computed
  |                          |                            |     sig == provided sig
  |                          |                            |  6. Check IAM permissions
  |                          |                            |     for the signing user
  |                          |                            |
  |                          |                            |--process as normal PUT
  |                          |                            |  (erasure encode, store
  |                          |                            |   chunks, write metadata,
  |                          |                            |   update witness)
  |                          |                            |
  |                          |<--200 OK ------------------|
  |                          |  ETag: "..."               |
```

### 9.2 Presigned GET Flow

```
App Server               Client (Browser)             S3 (Front-End)
  |                          |                            |
  |  Generate presigned      |                            |
  |  GET URL (1 hour expiry) |                            |
  |                          |                            |
  |--return URL to client -->|                            |
  |                          |                            |
  |                          |--GET /photos/img.jpg ----->|
  |                          |  ?X-Amz-Signature=...      |
  |                          |                            |
  |                          |                            |--validate signature
  |                          |                            |--process as normal GET
  |                          |                            |
  |                          |<--200 OK + image data -----|
  |                          |                            |
  |                          |  Browser renders image     |
  |                          |  directly from S3          |
```

### 9.3 Security Properties

```
What the presigned URL grants:
  - Access to ONE specific object (bucket + key baked into signature)
  - For ONE specific HTTP method (PUT or GET, baked into signature)
  - For a LIMITED time (X-Amz-Expires, max 7 days for IAM user, 36 hours for STS)
  - With optional CONDITIONS (content-length-range, content-type, etc.)

What it does NOT expose:
  - AWS credentials (only the access key ID is in the URL, not the secret key)
  - Access to other objects (signature is key-specific)
  - Permanent access (time-limited)

Revocation:
  - Cannot revoke a specific presigned URL once issued
  - CAN revoke the signing credentials (deactivate the IAM access key)
  - CAN add a bucket policy deny rule for the signing user
  - Both of these immediately invalidate all presigned URLs signed by those credentials
```

### 9.4 Latency

Presigned URL generation is a **client-side operation** (no S3 API call):

| Operation | Latency | Where |
|-----------|---------|-------|
| Generate presigned URL | < 1ms | App server (local crypto) |
| Client upload via presigned URL | Same as normal PUT | S3 |
| Client download via presigned URL | Same as normal GET | S3 |

---

## 10. Event Notification Flow

S3 Event Notifications allow you to trigger downstream processing when objects are
created, deleted, or transitioned. Three delivery targets are supported: SNS, SQS,
and Lambda (plus EventBridge as a universal router).

### 10.1 Notification Delivery Flow

```
Client                S3 (Front-End)            Event System             Targets
  |                      |                          |                      |
  |--PUT /key ---------->|                          |                      |
  |                      |                          |                      |
  |                      |--process PUT (normal)----|                      |
  |                      |  (data + metadata +      |                      |
  |                      |   witness update)         |                      |
  |                      |                          |                      |
  |<--200 OK ------------|                          |                      |
  |                      |                          |                      |
  |  NOTE: 200 OK is returned BEFORE notification   |                      |
  |  delivery. Notifications are async -- they do    |                      |
  |  not block the PUT response.                     |                      |
  |                      |                          |                      |
  |                      |--emit event ------------>|                      |
  |                      |  {                        |                      |
  |                      |   "Records": [{           |                      |
  |                      |    "eventName":            |                      |
  |                      |    "s3:ObjectCreated:Put", |                      |
  |                      |    "s3": {                 |                      |
  |                      |     "bucket": {            |                      |
  |                      |      "name": "my-bucket"   |                      |
  |                      |     },                     |                      |
  |                      |     "object": {            |                      |
  |                      |      "key": "data/file.csv"|                      |
  |                      |      "size": 1048576,      |                      |
  |                      |      "eTag": "abc...",      |                      |
  |                      |      "versionId": "v2"      |                      |
  |                      |     }                       |                      |
  |                      |    }                        |                      |
  |                      |   }]                        |                      |
  |                      |  }                          |                      |
  |                      |                          |                      |
  |                      |                          |--match notification   |
  |                      |                          |  configuration:       |
  |                      |                          |                      |
  |                      |                          |  Rule 1: s3:Object    |
  |                      |                          |  Created:* prefix=    |
  |                      |                          |  "data/" --> SNS      |
  |                      |                          |                      |
  |                      |                          |  Rule 2: s3:Object    |
  |                      |                          |  Created:* suffix=    |
  |                      |                          |  ".csv" --> Lambda    |
  |                      |                          |                      |
  |                      |                          |  Both rules match!    |
  |                      |                          |                      |
  |                      |                          |--publish to SNS ----->| SNS Topic
  |                      |                          |                      |  |
  |                      |                          |                      |  |--> SQS subscriber
  |                      |                          |                      |  |--> Email subscriber
  |                      |                          |                      |  |--> HTTP endpoint
  |                      |                          |                      |
  |                      |                          |--invoke Lambda ------>| Lambda
  |                      |                          |                      |  |
  |                      |                          |                      |  |--> process CSV
  |                      |                          |                      |  |--> load into DB
```

### 10.2 EventBridge Integration

```
S3                      EventBridge                    Downstream
  |                          |                            |
  |--emit event ----------->|                            |
  |  (all S3 events go to   |                            |
  |   EventBridge if enabled)|                            |
  |                          |                            |
  |                          |--evaluate rules:           |
  |                          |                            |
  |                          |  Rule 1:                   |
  |                          |  {                         |
  |                          |   "source": ["aws.s3"],    |
  |                          |   "detail-type":           |
  |                          |   ["Object Created"],      |
  |                          |   "detail": {              |
  |                          |    "bucket": {"name":      |
  |                          |    ["my-bucket"]},         |
  |                          |    "object": {"key":       |
  |                          |    [{"prefix":"images/"}]} |
  |                          |   }                        |
  |                          |  }                         |
  |                          |  --> Target: Step Function |
  |                          |                            |
  |                          |--start Step Function ----->| Step Function
  |                          |                            |  |
  |                          |                            |  |--> Resize image
  |                          |                            |  |--> Generate thumbnail
  |                          |                            |  |--> Update database
  |                          |                            |  |--> Send notification
```

### 10.3 Delivery Guarantees

| Target | Delivery | Ordering | Deduplication |
|--------|----------|----------|---------------|
| SNS | At-least-once | No ordering guarantee | Consumer must deduplicate |
| SQS | At-least-once | No ordering (Standard queue) | Consumer must deduplicate |
| SQS FIFO | Exactly-once | Ordered per message group | Built-in deduplication |
| Lambda | At-least-once | No ordering guarantee | Lambda may be invoked multiple times |
| EventBridge | At-least-once | No ordering guarantee | Consumer must deduplicate |

### 10.4 Latency Estimates

| Stage | Latency |
|-------|---------|
| PUT completes to event emission | ~100-500ms |
| Event emission to SNS delivery | ~100ms-1s |
| Event emission to SQS delivery | ~100ms-1s |
| Event emission to Lambda invocation | ~100ms-5s (includes cold start) |
| End-to-end (PUT to Lambda processing) | ~1-10 seconds |

**Important:** Notifications are best-effort async. They are not transactional with
the PUT. In rare cases (S3 internal failures), a notification may be delayed or lost.
For critical workflows, use EventBridge with dead-letter queues for reliability.

---

## 11. Data Integrity Check (Background Scrubbing)

Background scrubbing is S3's immune system. It continuously reads every chunk stored on
every disk, verifies checksums, and initiates repair when corruption is detected. This
is what catches silent data corruption (bit rot) before it causes data loss.

### 11.1 Normal Scrub Cycle (No Corruption)

```
Integrity Checker (per storage node, continuous loop)
  |
  |--read chunk C1 from disk ---------------------------------->| Disk
  |<--chunk data (128 KB) + stored checksum (SHA-256) ----------|
  |
  |--compute SHA-256 of chunk data
  |  computed: a3f2b8c9d1e4f5...
  |  stored:   a3f2b8c9d1e4f5...
  |
  |--compare: computed == stored --> MATCH
  |  chunk C1 is HEALTHY
  |
  |--read chunk C2 from disk ---------------------------------->| Disk
  |<--chunk data + stored checksum -----------------------------|
  |
  |--compute checksum
  |  computed == stored --> MATCH
  |  chunk C2 is HEALTHY
  |
  |  ... continue through all chunks on this disk ...
  |
  |--complete scrub cycle for disk /dev/sda1
  |  Total chunks: 2,500,000
  |  Healthy: 2,500,000
  |  Corrupted: 0
  |  Duration: ~48 hours (paced to not impact foreground I/O)
  |
  |--start next cycle immediately
```

### 11.2 Corruption Detection and Repair

```
Integrity Checker                Repair Coordinator       Storage Nodes        Metadata
  |                                   |                      |                   |
  |--read chunk C7 from disk -------->|                      |                   |
  |<--chunk data + stored checksum----|                      |                   |
  |                                   |                      |                   |
  |--compute checksum                 |                      |                   |
  |  computed: 7b2e4f1a...            |                      |                   |
  |  stored:   a3f2b8c9...            |                      |                   |
  |                                   |                      |                   |
  |--MISMATCH! CORRUPTION DETECTED    |                      |                   |
  |                                   |                      |                   |
  |--report to Repair Coordinator---->|                      |                   |
  |  {                                |                      |                   |
  |   chunk_id: C7,                   |                      |                   |
  |   node_id: Node3,                 |                      |                   |
  |   disk_id: /dev/sdb2,             |                      |                   |
  |   expected_checksum: a3f2b8c9..., |                      |                   |
  |   actual_checksum: 7b2e4f1a...    |                      |                   |
  |  }                                |                      |                   |
  |                                   |                      |                   |
  |                                   |--look up which------>|                   |
  |                                   |  object owns C7      |                   |
  |                                   |  (reverse index:     |                   |
  |                                   |   chunk -> object)   |                   |
  |                                   |                      |                   |
  |                                   |<--object: bucket/key-|                   |
  |                                   |  chunk_map: [C1..C11]|                   |
  |                                   |  C7 is data chunk D7 |                   |
  |                                   |                      |                   |
  |                                   |--fetch 8 healthy---->|                   |
  |                                   |  chunks (parallel):  |                   |
  |                                   |  D1 from Node1       |                   |
  |                                   |  D2 from Node5       |                   |
  |                                   |  D3 from Node9       |                   |
  |                                   |  D4 from Node2       |                   |
  |                                   |  D5 from Node6       |                   |
  |                                   |  D6 from Node10      |                   |
  |                                   |  D8 from Node7       |                   |
  |                                   |  P1 from Node11      |                   |
  |                                   |                      |                   |
  |                                   |<--8 healthy chunks---|                   |
  |                                   |                      |                   |
  |                                   |--erasure decode:     |                   |
  |                                   |  reconstruct D7 from |                   |
  |                                   |  D1-D6,D8,P1         |                   |
  |                                   |                      |                   |
  |                                   |--verify reconstructed|                   |
  |                                   |  D7 checksum matches |                   |
  |                                   |  expected checksum   |                   |
  |                                   |                      |                   |
  |                                   |--write repaired D7-->|                   |
  |                                   |  to Node3 (same node,|                   |
  |                                   |  different disk) or  |                   |
  |                                   |  to Node12 (new node)|                   |
  |                                   |                      |                   |
  |                                   |--update metadata:----|------------------>|
  |                                   |  chunk_map[D7] =     |                   |
  |                                   |  new_location        |                   |
  |                                   |                      |                   |
  |                                   |--delete corrupted--->|                   |
  |                                   |  chunk from Node3    |                   |
  |                                   |  /dev/sdb2           |                   |
  |                                   |                      |                   |
  |                                   |--log repair event    |                   |
  |                                   |  to operations       |                   |
  |                                   |  dashboard           |                   |
```

### 11.3 Scrub Rate and Pacing

```
Scrub pacing strategy:
  Goal: scrub every chunk on every disk within a target cycle time
  Constraint: do not degrade foreground read/write performance

  Example:
    Disk capacity: 16 TB
    Average chunk size: 128 KB
    Chunks per disk: ~125 million
    Target scrub cycle: 14 days (2 weeks)

    Required scrub rate: 125M chunks / (14 * 24 * 3600) seconds
                       = ~103 chunks/second
                       = ~13 MB/second sustained background read

    For a disk with 200 MB/s sequential read throughput:
    Background scrub uses ~6.5% of disk bandwidth

  Adaptive pacing:
    - During low-traffic periods (night): increase scrub rate to 200 chunks/sec
    - During peak traffic: decrease to 50 chunks/sec
    - If disk I/O latency exceeds threshold: pause scrubbing entirely
    - Priority: foreground requests always win over background scrubbing
```

### 11.4 Multi-Corruption Scenario

```
What if multiple chunks of the same object are corrupted?

Object with 8+3 erasure coding (11 chunks, tolerate 3 failures):

Scenario: 2 chunks corrupted (D3 and P2)
  Scrubber detects D3 is corrupt
  Repair Coordinator: fetch 8 of remaining 9 healthy chunks --> reconstruct D3
  Scrubber later detects P2 is corrupt
  Repair Coordinator: fetch 8 of remaining 10 healthy chunks --> reconstruct P2
  Result: REPAIRED, no data loss

Scenario: 4 chunks corrupted before any repair
  D1, D5, D8, P3 all corrupt (node failures, not bit rot)
  Only 7 healthy chunks remain -- need 8 to decode
  Result: DATA LOSS (this is what the durability math models)

  This is extremely unlikely:
  P(4 simultaneous failures before repair) ~ 10^-16
  = beyond 11 nines of durability
```

---

## 12. AZ Failure and Recovery

This flow analyzes what happens when an entire Availability Zone goes down and how S3's
erasure coding scheme must be designed to survive this scenario.

### 12.1 The Naive Distribution Problem

With an 8+3 erasure coding scheme (11 chunks, need any 8 to reconstruct), the chunks
must be distributed across 3 AZs. But 11 chunks across 3 AZs means at least one AZ
must hold 4 chunks (since 11 / 3 = 3 remainder 2):

```
Attempt 1: Even distribution (4-4-3)

  AZ-a: [D1, D4, D7, P2]     (4 chunks)
  AZ-b: [D2, D5, D8, P3]     (4 chunks)
  AZ-c: [D3, D6, P1]         (3 chunks)

  AZ-a goes down: remaining = 4 + 3 = 7 chunks
  Need 8 to reconstruct --> CANNOT READ!

  AZ-b goes down: remaining = 4 + 3 = 7 chunks
  Need 8 to reconstruct --> CANNOT READ!

  AZ-c goes down: remaining = 4 + 4 = 8 chunks
  Need 8 to reconstruct --> barely OK (zero margin)

  PROBLEM: Losing either of the two 4-chunk AZs is fatal.
```

### 12.2 Attempting Max-3-Per-AZ Distribution

```
Attempt 2: Limit each AZ to at most 3 chunks

  AZ-a: [D1, D4, P2]         (3 chunks)
  AZ-b: [D2, D5, D7]         (3 chunks)
  AZ-c: [D3, D6, D8, P1, P3] (5 chunks)

  Wait -- that puts 5 chunks in AZ-c. If AZ-c goes down, we have 6 chunks.
  Need 8 --> CANNOT READ.

  Attempt 2b: truly even at 3-3-?
  11 chunks with max 3 per AZ requires: ceil(11/3) = 4 AZs minimum.
  With only 3 AZs, at least one must hold 4+ chunks.

  CONCLUSION: 8+3 across exactly 3 AZs cannot guarantee single-AZ
  failure tolerance if any AZ can hold 4 chunks.
```

### 12.3 The Fundamental Constraint

```
For an (k + m) erasure coding scheme across Z availability zones:

  Requirement: survive any single AZ failure
  This means: no single AZ can hold more than m chunks
  (because if we lose m+1 chunks, we cannot reconstruct)

  With 8+3 (m=3, total=11, Z=3):
    Max chunks per AZ = 3
    3 AZs * 3 chunks = 9 max < 11 total --> IMPOSSIBLE

  The math is clear: 8+3 across 3 AZs CANNOT survive losing any AZ
  if we distribute 4-4-3.

  Options to resolve this tension:
```

### 12.4 Option A: Use a Higher Parity Ratio

```
Scheme: 8+4 (8 data + 4 parity = 12 chunks, need any 8 to reconstruct)

  AZ-a: [D1, D4, D7, P3]     (4 chunks)
  AZ-b: [D2, D5, D8, P4]     (4 chunks)
  AZ-c: [D3, D6, P1, P2]     (4 chunks)

  Perfectly balanced: 4-4-4

  AZ-a goes down: remaining = 4 + 4 = 8 chunks --> CAN RECONSTRUCT
  AZ-b goes down: remaining = 4 + 4 = 8 chunks --> CAN RECONSTRUCT
  AZ-c goes down: remaining = 4 + 4 = 8 chunks --> CAN RECONSTRUCT

  Storage overhead: 12/8 = 1.5x (vs 1.375x for 8+3)
  Durability: tolerates 4 chunk failures (vs 3 for 8+3)
  AZ resilience: survives loss of ANY single AZ

  TRADEOFF: 9% more storage overhead (1.5x vs 1.375x) but guaranteed AZ resilience.
  At exabyte scale, 9% is enormous -- but AZ failure tolerance is non-negotiable.
```

### 12.5 Option B: Use More AZs (if Available)

```
Scheme: 8+3 across 4 AZs (if the region has 4+ AZs)

  AZ-a: [D1, D4, D7]         (3 chunks)
  AZ-b: [D2, D5, P2]         (3 chunks)
  AZ-c: [D3, D6, P3]         (3 chunks)
  AZ-d: [D8, P1]             (2 chunks)

  Max chunks per AZ: 3
  m = 3 (can tolerate 3 failures)

  AZ-a goes down: remaining = 3 + 3 + 2 = 8 --> CAN RECONSTRUCT
  AZ-b goes down: remaining = 3 + 3 + 2 = 8 --> CAN RECONSTRUCT
  AZ-c goes down: remaining = 3 + 3 + 2 = 8 --> CAN RECONSTRUCT
  AZ-d goes down: remaining = 3 + 3 + 3 = 9 --> CAN RECONSTRUCT

  Storage overhead: 11/8 = 1.375x (unchanged)
  AZ resilience: survives loss of ANY single AZ

  TRADEOFF: Requires 4 AZs (not all regions have 4+).
  Cross-AZ write latency may increase slightly.
```

### 12.6 Option C: 6+3 Scheme

```
Scheme: 6+3 (6 data + 3 parity = 9 chunks, need any 6 to reconstruct)

  AZ-a: [D1, D4, P2]         (3 chunks)
  AZ-b: [D2, D5, P3]         (3 chunks)
  AZ-c: [D3, D6, P1]         (3 chunks)

  Perfectly balanced: 3-3-3

  AZ-a goes down: remaining = 3 + 3 = 6 --> CAN RECONSTRUCT (exactly 6 needed)
  AZ-b goes down: remaining = 3 + 3 = 6 --> CAN RECONSTRUCT
  AZ-c goes down: remaining = 3 + 3 = 6 --> CAN RECONSTRUCT

  Storage overhead: 9/6 = 1.5x
  AZ resilience: survives loss of ANY single AZ (zero margin)

  TRADEOFF: Same 1.5x overhead as 8+4, but with zero margin on AZ failure.
  If one additional chunk is lost within the surviving AZs during an AZ outage,
  reconstruction fails. The 8+4 scheme is strictly better because it has the
  same overhead but more data chunks (higher read throughput, larger minimum
  part size before chunking becomes wasteful).
```

### 12.7 What S3 Likely Uses

```
S3 has never publicly disclosed the exact erasure coding parameters.
Based on the constraints analyzed above, the most likely scheme is:

  8+4 across 3 AZs (4-4-4 distribution)

  Rationale:
  1. 3 AZs is the standard per-region minimum in AWS (some regions have more)
  2. 1.5x overhead is acceptable (S3 advertises ~1.4x overhead, which is close)
  3. 4-4-4 distribution survives any single AZ loss with margin
  4. 4 parity chunks means tolerating 4 failures before data loss
  5. Durability math:
     P(5 failures in repair window) = C(12,5) * (1.37*10^-5)^5
                                    = 792 * (4.8*10^-24)
                                    = 3.8*10^-21
     That is ~20 nines of durability -- well beyond the advertised 11

  It is also possible S3 uses different EC schemes for different object sizes:
  - Very small objects (< 256 KB): 3x replication (simpler, EC overhead not worth it)
  - Medium objects (256 KB - 100 MB): 6+3 or 8+4
  - Large objects (100 MB+): 8+4 or higher ratios for better efficiency

  The key insight for interviews: the erasure coding ratio is constrained by
  AZ failure tolerance, not just individual chunk failure tolerance.
```

### 12.8 AZ Recovery Flow

```
Time T=0: AZ-a experiences a failure (power outage, network partition)

Scheme: 8+4 across 3 AZs (4-4-4)
AZ-a held 4 chunks per object: [D1, D4, D7, P3]

Step 1: Detection (~seconds)
  |
  |  Health monitoring detects AZ-a is unreachable
  |  Storage nodes in AZ-a stop responding to heartbeats
  |  Front-end marks AZ-a nodes as UNAVAILABLE
  |
  |  ALL reads continue working:
  |  8 chunks still available in AZ-b + AZ-c --> can reconstruct
  |  (but reads are now slower: only 8 of 12 available,
  |   no hedging margin, p99 latency increases)

Step 2: Read Path Adaptation (~seconds)
  |
  |  Front-end detects chunk read failures from AZ-a nodes
  |  Automatically falls back to reading from AZ-b + AZ-c only
  |  Erasure decode from 8 chunks instead of cherry-picking 8 fastest of 12
  |  Latency increases by ~20-50% during degraded mode

Step 3: Determine AZ-a Status (~minutes)
  |
  |  Is this a transient blip or a real outage?
  |  Wait for confirmation from health monitoring
  |
  |  IF TRANSIENT (< 5 minutes): AZ-a comes back, chunks are fine, resume normal
  |
  |  IF PROLONGED (> threshold, e.g., 30 minutes):
  |  Initiate re-replication

Step 4: Re-replication (if prolonged outage) (~minutes to hours)
  |
  Repair Coordinator for EVERY affected object:
  |
  |  Object: bucket/key
  |  Lost chunks: D1, D4, D7, P3 (all 4 chunks that were in AZ-a)
  |  Available: D2, D3, D5, D6, D8, P1, P2, P4 (8 chunks in AZ-b, AZ-c)
  |
  |--fetch 8 available chunks (all of them, since exactly 8 remain)
  |--erasure decode: reconstruct D1, D4, D7, P3
  |--write reconstructed chunks to NEW nodes in AZ-b and AZ-c
  |  (temporarily over-weight these AZs)
  |--update metadata: chunk_map points to new locations
  |
  |  Repeat for every object that had chunks in AZ-a
  |  At S3 scale: trillions of objects, billions of chunks to reconstruct
  |  This takes hours to days, limited by disk I/O and network bandwidth
  |
  |  Prioritization:
  |  - Objects with fewer surviving chunks are repaired first
  |  - Frequently accessed objects are prioritized
  |  - Background repair is paced to avoid saturating surviving nodes

Step 5: AZ-a Recovers (~hours to days)
  |
  |  AZ-a comes back online
  |  Chunks on AZ-a disks are verified (scrubbed)
  |  Duplicates are identified (chunks that were re-replicated)
  |  Metadata is reconciled
  |  Excess replicas are cleaned up
  |  Distribution is rebalanced back to 4-4-4 across 3 AZs
```

### 12.9 Latency Impact During AZ Failure

| Metric | Normal (3 AZ) | Degraded (2 AZ) | Notes |
|--------|---------------|------------------|-------|
| GET p50 | ~10ms | ~15ms | Fewer chunk sources, less hedging |
| GET p99 | ~30ms | ~60ms | No ability to drop 4 slowest |
| PUT | ~25ms | ~35ms | Must write all chunks to 2 AZs |
| Availability | 99.99% | 99.9% (estimated) | Reduced redundancy |
| Durability | > 11 nines | Still > 11 nines (repair in progress) | Marginally reduced |

---

## 13. Flow Summary Table

| # | Flow | Trigger | Latency | Sync? | Key Components |
|---|------|---------|---------|-------|----------------|
| 1 | PUT Object | Client request | 20-50ms (small obj) | Yes | Front-End, Data Layer, Metadata, Witness |
| 2 | GET Object | Client request | 10-50ms | Yes | Front-End, Witness, Metadata (if stale), Data Layer |
| 3 | GET Byte-Range | Client request | 5-20ms (single chunk) | Yes | Front-End, Metadata, Data Layer (subset) |
| 4 | DELETE Object | Client request | 5-10ms (sync) | Partial | Front-End, Metadata, Witness (sync); GC (async) |
| 5 | LIST Objects | Client request | 50-500ms | Yes | Front-End, Metadata (range scan across partitions) |
| 6 | Multipart Upload | Client request | Variable per part | Parts: sync; Complete: sync | Front-End, Data Layer, Metadata, Witness |
| 7 | Cross-Region Repl. | Object event | Minutes (std), < 15min (RTC) | No (async) | Replication Worker, source S3, dest S3 |
| 8 | Storage Transition | Lifecycle timer | Background (24-48h scan) | No (async) | Lifecycle Evaluator, Metadata, Data Layer |
| 9 | Presigned URL | SDK generation | < 1ms (local crypto) | N/A | AWS SDK (client-side only) |
| 10 | Event Notification | Object event | 1-10 seconds | No (async) | S3 Event System, SNS/SQS/Lambda/EventBridge |
| 11 | Integrity Check | Continuous scan | Background (~14 day cycle) | No (async) | Integrity Checker, Repair Coordinator |
| 12 | AZ Recovery | AZ failure | Minutes (detection) to hours (full repair) | No (async) | Health Monitor, Repair Coordinator, all layers |

---

## Cross-References

| Document | Coverage |
|----------|----------|
| [Interview Simulation](interview-simulation.md) | Full 60-minute mock interview covering architecture, tradeoffs, and deep dives |
| [API Contracts](api-contracts.md) | REST API design, request/response formats, SigV4 authentication, error handling |

---

*This document covers the 12 primary system flows in Amazon S3. Each flow traces the full
path from trigger to completion, including component interactions, latency estimates, failure
handling, and the tradeoffs that shape S3's design. The analysis of AZ failure resilience
(Flow 12) demonstrates how infrastructure constraints (3 AZs per region) directly constrain
the erasure coding scheme choice, a detail often overlooked in system design interviews.*
