# Distributed Key-Value Store — API Contracts

> Continuation of the interview simulation. The interviewer asked: "Walk me through the API contracts for your key-value store."

---

## Base URL & Conventions

```
Base URL: https://kv.internal.amazon.com/v1
Content-Type: application/json (for metadata/responses)
Content-Type: application/octet-stream (for raw value bodies)
Authorization: mTLS (service-to-service) or IAM Signature V4
```

**Common conventions:**
- All endpoints require authentication (mTLS for internal services, IAM Sig V4 for AWS-style access).
- Consistency level is specified per-request via the `X-Consistency-Level` header.
- All responses include a `X-Request-Id` header for tracing.
- Timestamps are Unix epoch milliseconds.
- Keys are UTF-8 strings, max 256 bytes.
- Values are opaque byte arrays, max 1 MB.

**Consistency Level Header:**

```
X-Consistency-Level: ONE | QUORUM | ALL
```

| Level | Behavior | Use Case |
|-------|----------|----------|
| `ONE` | Read/write to 1 replica. Fastest, eventual consistency. | Caching, non-critical reads |
| `QUORUM` | Read/write to majority (W=2, R=2 for N=3). Strong consistency when W+R>N. | Default — most operations |
| `ALL` | Read/write to all N replicas. Strongest consistency, lowest availability. | Critical reads where staleness is unacceptable |

---

## Interview-Focused APIs (Core — discussed in the interview)

### 1. PUT — Store a Key-Value Pair

```
PUT /v1/kv/{key}
```

**Request Headers:**
```
Authorization: <credentials>
Content-Type: application/octet-stream
X-Consistency-Level: QUORUM
X-TTL-Seconds: 86400                    // Optional. Key expires after this many seconds. 0 = no expiry.
X-Idempotency-Key: <client-uuid>        // Optional. Prevents duplicate writes on retry.
X-If-Match: <version>                    // Optional. Conditional write (CAS). Only write if current version matches.
```

**Request Body:**
```
<raw binary value — up to 1 MB>
```

**Response — 200 OK:**
```json
{
  "key": "user:12345:session",
  "version": "v_1719964800000_A1",
  "size_bytes": 4096,
  "created_at": 1719964800000,
  "expires_at": 1720051200000,
  "replicas_written": 3,
  "consistency_achieved": "QUORUM"
}
```

**What happens server-side:**
```
1. Client SDK hashes key → determines coordinator node from the ring
2. Coordinator receives the PUT request
3. Coordinator writes to local storage engine (WAL + memtable)
4. Coordinator forwards write to N-1 replica nodes (parallel)
5. Wait for W total ACKs (including self)
   - QUORUM (W=2): need 1 more ACK from replicas
   - ONE (W=1): return immediately after local write
   - ALL (W=3): wait for all replicas
6. If a replica is down → hinted handoff (store hint on a healthy node)
7. Return response to client with version info
```

**Error Responses:**

| Status | Code | Description |
|--------|------|-------------|
| 400 | `KEY_TOO_LONG` | Key exceeds 256 bytes |
| 400 | `VALUE_TOO_LARGE` | Value exceeds 1 MB |
| 409 | `VERSION_MISMATCH` | Conditional write failed — current version doesn't match `X-If-Match` |
| 429 | `RATE_LIMIT_EXCEEDED` | Client exceeded request rate quota |
| 503 | `INSUFFICIENT_REPLICAS` | Cannot achieve requested consistency level (not enough healthy nodes) |
| 504 | `WRITE_TIMEOUT` | Replicas didn't respond within timeout |

**Error Response Format:**
```json
{
  "error": {
    "code": "INSUFFICIENT_REPLICAS",
    "message": "Requested consistency level QUORUM requires 2 replicas, but only 1 is available for key 'user:12345:session'.",
    "details": {
      "requested_consistency": "QUORUM",
      "replicas_required": 2,
      "replicas_available": 1,
      "preference_list": ["node-b", "node-a", "node-c"],
      "unavailable_nodes": ["node-a", "node-c"]
    }
  }
}
```

---

### 2. GET — Retrieve a Value by Key

```
GET /v1/kv/{key}
```

**Request Headers:**
```
Authorization: <credentials>
X-Consistency-Level: QUORUM
```

**Response — 200 OK:**

```
HTTP/1.1 200 OK
Content-Type: application/octet-stream
X-Key: user:12345:session
X-Version: v_1719964800000_A1
X-Created-At: 1719964800000
X-Expires-At: 1720051200000
X-Consistency-Achieved: QUORUM
X-Replicas-Read: 2

<raw binary value>
```

**Why return value as raw bytes in the body (not JSON)?**
- Values are opaque binary — could be serialized protobuf, JSON, images, etc.
- Wrapping binary in JSON (base64 encoding) adds ~33% overhead and encoding/decoding cost.
- Metadata is in response headers — clean separation of concerns.

**What happens server-side:**
```
1. Client SDK hashes key → determines coordinator node
2. Coordinator reads locally + sends read requests to R-1 replicas (parallel)
3. Wait for R total responses
4. Compare versions across responses:
   - If all agree → return the value
   - If versions differ → return the newest version (highest timestamp or dominant vector clock)
5. If stale replicas detected → trigger READ REPAIR (async)
   - Send the newest value to stale replicas
6. If key has expired (TTL) → return 404 NOT_FOUND
```

**Error Responses:**

| Status | Code | Description |
|--------|------|-------------|
| 404 | `KEY_NOT_FOUND` | Key doesn't exist or has expired |
| 503 | `INSUFFICIENT_REPLICAS` | Cannot achieve requested consistency level |
| 504 | `READ_TIMEOUT` | Replicas didn't respond within timeout |

---

### 3. DELETE — Remove a Key

```
DELETE /v1/kv/{key}
```

**Request Headers:**
```
Authorization: <credentials>
X-Consistency-Level: QUORUM
```

**Response — 200 OK:**
```json
{
  "key": "user:12345:session",
  "deleted": true,
  "version": "v_1719964800000_A1_tombstone"
}
```

**What happens server-side:**
```
1. Coordinator writes a TOMBSTONE marker (not an actual deletion)
2. Tombstone is replicated to N-1 replicas (same as a PUT)
3. Wait for W ACKs
4. Tombstone prevents the key from being "resurrected" by read repair
5. Tombstone is garbage-collected during compaction after gc_grace_seconds (default: 10 days)
```

**Why tombstones instead of immediate deletion?**
- SSTables are immutable — can't remove data in-place.
- Without tombstones, a replica that missed the delete would still have the old value and could "resurrect" it during read repair or anti-entropy sync.
- Tombstones are the "proof of deletion" that propagates across replicas.

| Status | Code | Description |
|--------|------|-------------|
| 404 | `KEY_NOT_FOUND` | Key doesn't exist (delete is idempotent — this is a soft error) |
| 503 | `INSUFFICIENT_REPLICAS` | Cannot achieve requested consistency level |

---

## Full API Surface (Beyond the Interview Scope)

The APIs below are what a production-grade distributed KV store would need. They weren't deep-dived in the interview but are documented here for completeness.

---

### 4. BATCH GET — Retrieve Multiple Keys

```
POST /v1/kv/_batch/get
```

**Request Body:**
```json
{
  "keys": [
    "user:12345:session",
    "user:12345:preferences",
    "user:12345:cart",
    "product:99999:metadata"
  ],
  "consistency": "QUORUM"
}
```

**Response — 200 OK:**
```json
{
  "results": [
    {
      "key": "user:12345:session",
      "status": "FOUND",
      "value_base64": "eyJzZXNzaW9uX2lkIjoiYWJjMTIzIn0=",
      "version": "v_1719964800000_A1",
      "size_bytes": 28
    },
    {
      "key": "user:12345:preferences",
      "status": "FOUND",
      "value_base64": "eyJ0aGVtZSI6ImRhcmsifQ==",
      "version": "v_1719950400000_B1",
      "size_bytes": 18
    },
    {
      "key": "user:12345:cart",
      "status": "NOT_FOUND"
    },
    {
      "key": "product:99999:metadata",
      "status": "FOUND",
      "value_base64": "eyJuYW1lIjoiV2lkZ2V0In0=",
      "version": "v_1719936000000_C1",
      "size_bytes": 20
    }
  ],
  "found_count": 3,
  "not_found_count": 1
}
```

**Why base64 for batch GET?**
- Single GET returns raw bytes (optimal for single values).
- Batch GET must return multiple values in one JSON response — base64 encoding is necessary.
- Alternative: multipart response, but JSON is simpler for clients.

**Server-side optimization:**
- Keys are grouped by coordinator node (based on the ring).
- Requests are sent in parallel to each coordinator.
- Reduces round trips: 4 keys on 3 different nodes = 3 parallel coordinator calls instead of 4 sequential.

**Limits:**
- Max 100 keys per batch request.
- Max total response size: 10 MB.

---

### 5. BATCH PUT — Store Multiple Key-Value Pairs

```
POST /v1/kv/_batch/put
```

**Request Body:**
```json
{
  "items": [
    {
      "key": "user:12345:session",
      "value_base64": "eyJzZXNzaW9uX2lkIjoiYWJjMTIzIn0=",
      "ttl_seconds": 86400
    },
    {
      "key": "user:12345:preferences",
      "value_base64": "eyJ0aGVtZSI6ImRhcmsifQ==",
      "ttl_seconds": 0
    }
  ],
  "consistency": "QUORUM"
}
```

**Response — 200 OK:**
```json
{
  "results": [
    {
      "key": "user:12345:session",
      "status": "OK",
      "version": "v_1719964800000_A1"
    },
    {
      "key": "user:12345:preferences",
      "status": "OK",
      "version": "v_1719964800001_B1"
    }
  ],
  "success_count": 2,
  "failure_count": 0
}
```

**Important: NOT transactional.**
- Each key is written independently. Some may succeed while others fail.
- The response includes per-key status so the client can retry failed items.
- For transactional batch writes, see the Transaction API (Section 9).

**Limits:**
- Max 25 items per batch.
- Max total request size: 10 MB.

---

### 6. BATCH DELETE — Remove Multiple Keys

```
POST /v1/kv/_batch/delete
```

**Request Body:**
```json
{
  "keys": [
    "user:12345:session",
    "user:12345:cart"
  ],
  "consistency": "QUORUM"
}
```

**Response — 200 OK:**
```json
{
  "results": [
    {
      "key": "user:12345:session",
      "status": "DELETED"
    },
    {
      "key": "user:12345:cart",
      "status": "NOT_FOUND"
    }
  ],
  "deleted_count": 1,
  "not_found_count": 1
}
```

---

### 7. Compare-and-Swap (CAS) — Conditional Write

```
PUT /v1/kv/{key}
```

Uses the `X-If-Match` header for optimistic concurrency control:

```
PUT /v1/kv/user:12345:session
X-If-Match: v_1719964800000_A1
X-Consistency-Level: QUORUM
Content-Type: application/octet-stream

<new value bytes>
```

**Behavior:**
```
1. Coordinator reads the current version of the key
2. If current version == X-If-Match value → proceed with write
3. If current version != X-If-Match value → return 409 CONFLICT
4. If key doesn't exist and X-If-Match: * → always write (create-if-not-exists)
5. If key doesn't exist and X-If-Match: <specific version> → return 409
```

**Response — 200 OK (version matched, write succeeded):**
```json
{
  "key": "user:12345:session",
  "version": "v_1719964800500_A2",
  "previous_version": "v_1719964800000_A1",
  "cas_result": "SUCCESS"
}
```

**Response — 409 Conflict:**
```json
{
  "error": {
    "code": "VERSION_MISMATCH",
    "message": "CAS failed: expected version v_1719964800000_A1, but current version is v_1719964800200_B1.",
    "details": {
      "expected_version": "v_1719964800000_A1",
      "current_version": "v_1719964800200_B1"
    }
  }
}
```

**Use cases:**
- Distributed locks: `PUT lock:resource X-If-Match: *` (create if not exists)
- Optimistic concurrency: read → modify → write-back-if-unchanged
- Counter increments without race conditions

**Important caveat:** CAS in a leaderless system requires `QUORUM` consistency (or `ALL`) to be safe. With `ONE`, the CAS check is done on a single node — another node may have a newer version.

---

### 8. Key Metadata — Get Key Info Without Value

```
HEAD /v1/kv/{key}
```

**Response — 200 OK (no body):**
```
HTTP/1.1 200 OK
X-Key: user:12345:session
X-Version: v_1719964800000_A1
X-Size-Bytes: 4096
X-Created-At: 1719964800000
X-Expires-At: 1720051200000
X-TTL-Remaining: 43200
```

**Use cases:**
- Check if a key exists without transferring the value (bandwidth-efficient)
- Get TTL remaining, size, version for monitoring/debugging
- Pre-check before CAS operations

---

### 9. Scan Keys — List Keys by Prefix

```
GET /v1/kv/_scan?prefix={prefix}&limit={limit}&cursor={cursor}
```

**Query Parameters:**

| Param | Type | Default | Description |
|-------|------|---------|-------------|
| `prefix` | string | required | Key prefix to scan (e.g., `user:12345:`) |
| `limit` | int | 100 | Max keys to return per page. Max 1000. |
| `cursor` | string | null | Opaque cursor for pagination. |
| `include_values` | boolean | false | Whether to include values in the response. |

**Response — 200 OK:**
```json
{
  "keys": [
    {
      "key": "user:12345:cart",
      "version": "v_1719964800000_A1",
      "size_bytes": 512,
      "created_at": 1719964800000,
      "expires_at": null
    },
    {
      "key": "user:12345:preferences",
      "version": "v_1719950400000_B1",
      "size_bytes": 256,
      "created_at": 1719950400000,
      "expires_at": null
    },
    {
      "key": "user:12345:session",
      "version": "v_1719964800000_A1",
      "size_bytes": 4096,
      "created_at": 1719964800000,
      "expires_at": 1720051200000
    }
  ],
  "count": 3,
  "pagination": {
    "next_cursor": "eyJsYXN0X2tleSI6InVzZXI6MTIzNDU6c2Vzc2lvbiJ9",
    "has_more": false
  }
}
```

**Important trade-offs for scan in a hash-partitioned system:**
- In a **hash-partitioned** KV store (like ours), keys with the same prefix are scattered across different nodes.
- Scan requires a **scatter-gather** — query ALL nodes and merge results.
- This is **expensive** and should be rate-limited and used sparingly.
- For efficient prefix scans, you'd need **range-based partitioning** (out of scope for this design).

---

### 10. TTL Management — Update TTL on an Existing Key

```
PATCH /v1/kv/{key}/ttl
```

**Request Body:**
```json
{
  "ttl_seconds": 172800
}
```

**Response — 200 OK:**
```json
{
  "key": "user:12345:session",
  "previous_expires_at": 1720051200000,
  "new_expires_at": 1720137600000,
  "ttl_seconds": 172800
}
```

**Use case:** Extend a session's TTL without rewriting the value. The TTL update is replicated like a normal write.

---

## Cluster Administration APIs

These APIs are for operators, not application clients. They manage the cluster topology and health.

---

### 11. Cluster Health

```
GET /v1/admin/cluster/health
```

**Response — 200 OK:**
```json
{
  "cluster_name": "kv-prod-us-east-1",
  "status": "HEALTHY",
  "nodes": {
    "total": 100,
    "up": 98,
    "down": 1,
    "suspect": 1
  },
  "ring": {
    "total_vnodes": 25600,
    "assigned_vnodes": 25600,
    "pending_rebalance": 256
  },
  "storage": {
    "total_capacity_tb": 200,
    "used_tb": 148.5,
    "utilization_percent": 74.25
  },
  "traffic": {
    "reads_per_sec": 1050000,
    "writes_per_sec": 98000,
    "read_latency_p99_ms": 4.2,
    "write_latency_p99_ms": 8.7
  }
}
```

---

### 12. Node Status

```
GET /v1/admin/nodes/{node_id}
```

**Response — 200 OK:**
```json
{
  "node_id": "node-b-us-east-1a",
  "status": "UP",
  "address": "10.0.1.42:7000",
  "availability_zone": "us-east-1a",
  "rack": "rack-3",
  "vnodes_owned": 256,
  "data_size_gb": 1850,
  "load": {
    "cpu_percent": 45,
    "memory_percent": 72,
    "disk_io_percent": 38,
    "network_mbps": 450
  },
  "storage_engine": {
    "memtable_size_mb": 58,
    "sstable_count": 142,
    "pending_compactions": 3,
    "bloom_filter_memory_mb": 120
  },
  "gossip": {
    "heartbeat_generation": 1042,
    "last_seen_ms_ago": 250
  }
}
```

---

### 13. Ring State — View the Partition Ring

```
GET /v1/admin/ring
```

**Response — 200 OK:**
```json
{
  "ring": [
    {
      "token_start": "0x0000000000000000",
      "token_end": "0x0040000000000000",
      "primary_node": "node-a",
      "replica_nodes": ["node-b", "node-c"],
      "status": "NORMAL"
    },
    {
      "token_start": "0x0040000000000001",
      "token_end": "0x0080000000000000",
      "primary_node": "node-b",
      "replica_nodes": ["node-c", "node-d"],
      "status": "NORMAL"
    }
  ],
  "total_token_ranges": 25600
}
```

---

### 14. Add Node to Cluster

```
POST /v1/admin/nodes
```

**Request Body:**
```json
{
  "node_address": "10.0.1.55:7000",
  "availability_zone": "us-east-1c",
  "rack": "rack-7",
  "num_vnodes": 256
}
```

**Response — 202 Accepted:**
```json
{
  "node_id": "node-e-us-east-1c",
  "status": "JOINING",
  "message": "Node is joining the ring. Data streaming will begin shortly.",
  "estimated_completion_minutes": 45,
  "task_id": "task_join_12345"
}
```

**Side effects:**
1. New node is announced via gossip.
2. Vnodes are assigned (taken from the most loaded nodes).
3. Data streaming begins in the background.
4. During streaming: reads served by old owners, writes dual-written.
5. Once complete: ring state updated, new node goes to NORMAL status.

---

### 15. Remove Node (Decommission)

```
DELETE /v1/admin/nodes/{node_id}
```

**Response — 202 Accepted:**
```json
{
  "node_id": "node-c-us-east-1b",
  "status": "DECOMMISSIONING",
  "message": "Node is leaving the ring. Data will be streamed to remaining nodes.",
  "estimated_completion_minutes": 60,
  "task_id": "task_decommission_67890"
}
```

**Side effects:**
1. Node streams all its data to the next owners on the ring.
2. Once streaming is complete, vnodes are released.
3. Node is removed from the gossip membership.

---

### 16. Trigger Anti-Entropy Repair

```
POST /v1/admin/repair
```

**Request Body:**
```json
{
  "key_range_start": "user:",
  "key_range_end": "user:~",
  "nodes": ["node-a", "node-b"],
  "type": "FULL"
}
```

**Response — 202 Accepted:**
```json
{
  "repair_id": "repair_2026070200001",
  "status": "RUNNING",
  "message": "Merkle tree comparison started between node-a and node-b for key range [user:, user:~].",
  "estimated_keys_to_compare": 500000
}
```

---

### 17. Compaction Management

```
POST /v1/admin/nodes/{node_id}/compact
```

**Request Body:**
```json
{
  "type": "MAJOR",
  "priority": "LOW"
}
```

**Response — 202 Accepted:**
```json
{
  "node_id": "node-a",
  "compaction_id": "compact_12345",
  "type": "MAJOR",
  "status": "RUNNING",
  "estimated_duration_minutes": 30
}
```

---

## API Summary Table

### Core Data APIs (Interview Focus)

| # | Method | Endpoint | Description | Consistency |
|---|--------|----------|-------------|-------------|
| 1 | `PUT` | `/v1/kv/{key}` | Store a key-value pair | Tunable |
| 2 | `GET` | `/v1/kv/{key}` | Retrieve a value by key | Tunable |
| 3 | `DELETE` | `/v1/kv/{key}` | Delete a key (tombstone) | Tunable |

### Extended Data APIs (Production)

| # | Method | Endpoint | Description | Consistency |
|---|--------|----------|-------------|-------------|
| 4 | `POST` | `/v1/kv/_batch/get` | Batch get multiple keys | Tunable |
| 5 | `POST` | `/v1/kv/_batch/put` | Batch put multiple keys | Tunable |
| 6 | `POST` | `/v1/kv/_batch/delete` | Batch delete multiple keys | Tunable |
| 7 | `PUT` | `/v1/kv/{key}` + `X-If-Match` | Compare-and-swap (CAS) | QUORUM+ |
| 8 | `HEAD` | `/v1/kv/{key}` | Get key metadata (no value) | Tunable |
| 9 | `GET` | `/v1/kv/_scan?prefix=...` | Scan keys by prefix | Scatter-gather |
| 10 | `PATCH` | `/v1/kv/{key}/ttl` | Update TTL on existing key | Tunable |

### Cluster Administration APIs

| # | Method | Endpoint | Description |
|---|--------|----------|-------------|
| 11 | `GET` | `/v1/admin/cluster/health` | Cluster health overview |
| 12 | `GET` | `/v1/admin/nodes/{node_id}` | Individual node status |
| 13 | `GET` | `/v1/admin/ring` | View the partition ring |
| 14 | `POST` | `/v1/admin/nodes` | Add a node to the cluster |
| 15 | `DELETE` | `/v1/admin/nodes/{node_id}` | Decommission a node |
| 16 | `POST` | `/v1/admin/repair` | Trigger anti-entropy repair |
| 17 | `POST` | `/v1/admin/nodes/{node_id}/compact` | Trigger compaction |

---

## Design Decisions in the API

### Why REST over gRPC for the External API?

| Aspect | REST | gRPC |
|--------|------|------|
| **Simplicity** | ✅ Universal, any HTTP client works | Requires protobuf tooling |
| **Debugging** | ✅ curl-friendly, readable headers | Binary protocol, harder to inspect |
| **Streaming** | ❌ No native streaming | ✅ Bidirectional streaming |
| **Performance** | HTTP/1.1 overhead per request | ✅ HTTP/2 multiplexing, smaller payloads |
| **Schema** | Loose (JSON schema) | ✅ Strict (protobuf contracts) |

**Our choice**: REST for the external client API (simplicity, universality). gRPC for **internal node-to-node communication** (replication, gossip, streaming) where performance matters and both ends are controlled by us.

### Why Consistency Level in Headers (not URL/body)?

- Headers are **cross-cutting concerns** — consistency is orthogonal to the data being read/written.
- The same key can be read at different consistency levels by different clients.
- Headers don't affect caching semantics of the URL.
- Follows the pattern of HTTP `Accept`, `Authorization`, etc.

### Why Return Raw Bytes for GET (not JSON)?

- Values are opaque — we don't know or care about the encoding.
- Wrapping in JSON with base64 adds 33% overhead + encode/decode CPU.
- For batch operations, we accept the base64 overhead because multiple values must coexist in one JSON response.
- Metadata is returned in response headers — zero overhead on the value body.

### Why Idempotency Keys for PUT?

- Network failures between client → coordinator are common.
- Without idempotency: client retries → potential duplicate writes (harmless for KV stores with LWW, but problematic if vector clocks are in use — each retry creates a new "version").
- Idempotency key: coordinator checks `idempotency:{key}` in local memory/cache. If found, returns cached response. If not, processes write and caches response for 24h.

---

*This API contract document complements the [interview simulation](interview-simulation.md) and [datastore design](datastore-design.md).*