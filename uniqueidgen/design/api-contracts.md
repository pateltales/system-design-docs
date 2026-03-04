# Distributed Unique ID Generator — API Contracts

> Continuation of the interview simulation. The interviewer asked: "Walk me through the API contracts for your ID generation system."

---

## Base URL & Conventions

```
Base URL: https://idgen.internal.amazon.com/v1
Content-Type: application/json
Authorization: mTLS (service-to-service) or IAM Signature V4
```

**Common conventions:**
- All endpoints require authentication (mTLS for internal services, IAM Sig V4 for AWS-style access).
- All responses include an `X-Request-Id` header for distributed tracing.
- Timestamps are Unix epoch **milliseconds** (consistent with the Snowflake timestamp field).
- Worker IDs are 10-bit integers (0–1023).
- Generated IDs are 64-bit signed integers (positive only, sign bit = 0).

**Important architectural note:** The primary ID generation path (`generate_id()`) is a **local, in-process call** — not a network API. The REST APIs below are for:
1. **Batch pre-generation** — pre-allocate IDs for services that can't embed the library
2. **Metadata extraction** — parse timestamp/worker from existing IDs
3. **Administration** — worker registration, monitoring, health checks

The core generation happens via an **embedded library/SDK**, not an RPC call.

---

## Table of Contents

1. [Core ID Generation API](#1-core-id-generation-api)
2. [Batch ID Generation](#2-batch-id-generation)
3. [Timestamp Extraction (Reverse Lookup)](#3-timestamp-extraction-reverse-lookup)
4. [ID Validation](#4-id-validation)
5. [Worker Registration](#5-worker-registration)
6. [Worker Deregistration](#6-worker-deregistration)
7. [Worker Health & Status](#7-worker-health--status)
8. [Clock Drift Monitoring](#8-clock-drift-monitoring)
9. [Sequence Statistics](#9-sequence-statistics)
10. [Cluster Overview](#10-cluster-overview)

---

## Interview-Focused APIs (Core — discussed in the interview)

### 1. Core ID Generation API

**Primary interface: In-process SDK call (NOT an RPC)**

```
// Java SDK
SnowflakeGenerator generator = new SnowflakeGenerator(workerId);
long id = generator.nextId();

// Go SDK
gen := snowflake.NewGenerator(workerID)
id := gen.NextID()

// Python SDK
gen = SnowflakeGenerator(worker_id=42)
id = gen.next_id()
```

**What happens internally:**
```
1. Read current timestamp (System.currentTimeMillis() or equivalent)
2. Subtract custom epoch → get relative timestamp
3. Compare with last_timestamp:
   a. If greater → reset sequence to 0
   b. If equal → increment sequence
   c. If less → handle clock skew (error or wait)
4. Compose 64-bit ID: (timestamp << 22) | (worker_id << 12) | sequence
5. Return ID
```

**Performance characteristics:**
```
┌──────────────────────────┬───────────────────┐
│ Metric                   │ Value             │
├──────────────────────────┼───────────────────┤
│ Latency (p50)            │ ~0.5 microseconds │
│ Latency (p99)            │ ~1-2 microseconds │
│ Throughput per worker    │ 4,096,000 IDs/sec │
│ Memory footprint         │ ~64 bytes         │
│ Thread safety            │ Mutex-protected   │
│ Network calls            │ Zero              │
│ Disk I/O                 │ Zero              │
└──────────────────────────┴───────────────────┘
```

**Error conditions:**

| Error | Cause | Handling |
|-------|-------|----------|
| `ClockMovedBackwardsError` | System clock went backwards (NTP step correction) | Throw exception; caller retries or uses fallback |
| `SequenceExhaustedError` | 4,096 IDs generated in same millisecond | Busy-wait until next millisecond (transparent to caller) |
| `InvalidWorkerIdError` | Worker ID not in range [0, 1023] | Fail fast at initialization |

---

**REST API (for services that cannot embed the SDK):**

```
POST /v1/id/generate
```

**Request Headers:**
```
Authorization: <credentials>
Content-Type: application/json
X-Request-Id: <trace-id>
```

**Request Body:**
```json
{
  "count": 1
}
```

**Response — 200 OK:**
```json
{
  "ids": [7159358969602048007],
  "count": 1,
  "worker_id": 42,
  "generated_at_ms": 1706745600000,
  "latency_us": 3
}
```

**What happens server-side:**
```
1. Request arrives at a Snowflake worker service via load balancer
2. Worker calls its local SnowflakeGenerator.nextId()
3. Returns the generated ID in the response
4. Total latency: ~0.5-2ms (dominated by network RTT, not ID generation)
```

**Error Responses:**

| Status | Code | Description |
|--------|------|-------------|
| 429 | `RATE_LIMIT_EXCEEDED` | Client exceeded request rate quota |
| 500 | `CLOCK_ERROR` | System clock moved backwards — cannot generate IDs safely |
| 503 | `WORKER_NOT_READY` | Worker is starting up or has lost its worker ID registration |

**Error Response Format:**
```json
{
  "error": {
    "code": "CLOCK_ERROR",
    "message": "System clock moved backwards by 1247ms. Refusing to generate IDs to prevent duplicates.",
    "details": {
      "last_timestamp_ms": 1706745601247,
      "current_timestamp_ms": 1706745600000,
      "drift_ms": -1247,
      "worker_id": 42,
      "recommendation": "Check NTP synchronization. Worker will auto-recover when clock catches up."
    }
  }
}
```

---

### 2. Batch ID Generation

```
POST /v1/id/batch
```

**Request Body:**
```json
{
  "count": 100,
  "purpose": "tweet_creation"
}
```

**Response — 200 OK:**
```json
{
  "ids": [
    7159358969602048007,
    7159358969602048008,
    7159358969602048009,
    7159358969602052103,
    7159358969602052104
  ],
  "count": 100,
  "worker_id": 42,
  "timestamp_range": {
    "first_ms": 1706745600000,
    "last_ms": 1706745600000
  },
  "latency_us": 28
}
```

**Why batch generation?**
- Some services need to **pre-allocate** IDs before creating objects (e.g., assigning an ID to a tweet before it's fully processed)
- Reduces RPC overhead — one call instead of 100
- Batch of 100 IDs takes ~25 microseconds (still within a single millisecond)

**Limits:**
- Max `count`: 1,000 per request
- IDs are contiguous within a millisecond when possible; may span milliseconds for large batches

**Error Responses:**

| Status | Code | Description |
|--------|------|-------------|
| 400 | `INVALID_COUNT` | Count < 1 or > 1000 |
| 429 | `RATE_LIMIT_EXCEEDED` | Request rate quota exceeded |
| 500 | `CLOCK_ERROR` | System clock issue |

---

### 3. Timestamp Extraction (Reverse Lookup)

```
GET /v1/id/{id}/decode
```

**Response — 200 OK:**
```json
{
  "id": 7159358969602048007,
  "id_binary": "0110001101100000011001110011001000000000000000000010101000000111",
  "components": {
    "timestamp_ms": 1706745600000,
    "timestamp_iso": "2024-02-01T00:00:00.000Z",
    "worker_id": 42,
    "datacenter_id": 5,
    "machine_id": 10,
    "sequence": 7
  },
  "metadata": {
    "age_seconds": 86400,
    "custom_epoch": "2020-01-01T00:00:00.000Z",
    "relative_timestamp_ms": 186745600000
  }
}
```

**What happens server-side:**
```
1. Parse the 64-bit ID
2. Extract timestamp: (id >> 22) + EPOCH  → absolute timestamp in ms
3. Extract worker_id: (id >> 12) & 0x3FF  → 10-bit worker ID
4. Extract sequence: id & 0xFFF           → 12-bit sequence number
5. Optionally split worker_id into datacenter (5 bits) + machine (5 bits)
6. Return decomposed components
```

**Bit extraction detail:**
```
Given ID: 7159358969602048007 (decimal)

Binary: 0 | 11000110100000011011001000000000000000000 | 0000101010 | 000000000111
        ↑            ↑                                     ↑              ↑
      sign     timestamp (41 bits)                   worker (10)    sequence (12)

Extraction:
  timestamp = id >> 22                    = 1706745600000 + EPOCH
  worker_id = (id >> 12) & 0x3FF         = 42
  sequence  = id & 0xFFF                  = 7
```

**Use cases:**
- **Debugging**: "When was this tweet/order created?" — extract timestamp from the ID directly, no DB lookup needed
- **Analytics**: Estimate traffic patterns by extracting timestamps from IDs
- **Routing**: Determine which worker/datacenter generated an ID for troubleshooting
- **Archiving**: Identify IDs from a specific time range for data lifecycle management

---

### 4. ID Validation

```
POST /v1/id/validate
```

**Request Body:**
```json
{
  "ids": [
    7159358969602048007,
    -1,
    0,
    9999999999999999999
  ]
}
```

**Response — 200 OK:**
```json
{
  "results": [
    {
      "id": 7159358969602048007,
      "valid": true,
      "reason": null
    },
    {
      "id": -1,
      "valid": false,
      "reason": "NEGATIVE_ID: Sign bit is set. Snowflake IDs must be positive."
    },
    {
      "id": 0,
      "valid": false,
      "reason": "ZERO_ID: ID is 0, which is reserved."
    },
    {
      "id": 9999999999999999999,
      "valid": false,
      "reason": "FUTURE_TIMESTAMP: Extracted timestamp is in the future (year 2087). Possible corruption."
    }
  ],
  "valid_count": 1,
  "invalid_count": 3
}
```

**Validation checks performed:**
```
1. id > 0                              (sign bit must be 0)
2. id != 0                             (0 is reserved / sentinel)
3. extracted_timestamp <= current_time  (not in the future)
4. extracted_timestamp >= 0             (not before custom epoch)
5. extracted_worker_id <= 1023          (within valid range — always true for 10-bit field)
6. extracted_sequence <= 4095           (within valid range — always true for 12-bit field)
```

---

## Extended Production APIs (Beyond Interview Scope)

---

### 5. Worker Registration

```
POST /v1/admin/workers/register
```

**Request Body:**
```json
{
  "hostname": "ip-10-0-1-42.ec2.internal",
  "datacenter": "us-east-1a",
  "process_id": 12345,
  "service_name": "tweet-service",
  "requested_worker_id": null
}
```

**Response — 201 Created:**
```json
{
  "worker_id": 42,
  "registration_id": "reg_2026020700001",
  "registered_at": 1707264000000,
  "lease_ttl_seconds": 300,
  "lease_expiry": 1707264300000,
  "zk_node": "/snowflake/workers/worker-0000000042",
  "config": {
    "custom_epoch_ms": 1577836800000,
    "timestamp_bits": 41,
    "worker_id_bits": 10,
    "sequence_bits": 12,
    "max_clock_backwards_ms": 5000
  }
}
```

**What happens server-side:**
```
1. Connect to ZooKeeper / etcd
2. Create ephemeral sequential node under /snowflake/workers/
3. Assigned sequence number = worker_id (mod 1024 if needed)
4. If requested_worker_id specified:
   a. Check if that ID is available
   b. If available: create node with that ID
   c. If taken: return 409 CONFLICT
5. Return assigned worker_id and configuration
6. Worker must send heartbeats every lease_ttl/3 seconds (100s)
   to keep the ephemeral node alive
```

**Error Responses:**

| Status | Code | Description |
|--------|------|-------------|
| 409 | `WORKER_ID_TAKEN` | Requested worker ID is already assigned to another host |
| 503 | `COORDINATOR_UNAVAILABLE` | ZooKeeper/etcd is unreachable |
| 507 | `WORKER_IDS_EXHAUSTED` | All 1,024 worker IDs are currently assigned |

**Worker ID exhaustion response:**
```json
{
  "error": {
    "code": "WORKER_IDS_EXHAUSTED",
    "message": "All 1,024 worker IDs are currently in use. Cannot register new worker.",
    "details": {
      "total_worker_ids": 1024,
      "active_workers": 1024,
      "stale_workers": 3,
      "recommendation": "Wait for stale workers to expire (TTL: 300s) or manually deregister inactive workers."
    }
  }
}
```

---

### 6. Worker Deregistration

```
DELETE /v1/admin/workers/{worker_id}
```

**Response — 200 OK:**
```json
{
  "worker_id": 42,
  "status": "DEREGISTERED",
  "deregistered_at": 1707264600000,
  "ids_generated": 15482903,
  "uptime_seconds": 86400,
  "last_timestamp_used": 1707264599999,
  "message": "Worker ID 42 is now available for reassignment. Warning: ensure no in-flight ID generation is occurring."
}
```

**Why explicit deregistration matters:**
- Ephemeral ZK nodes auto-expire when the session dies, but session timeout can be 30-60s
- Explicit deregistration releases the worker ID immediately
- Important during rolling deployments: old worker deregisters → new worker registers with same ID
- **Safety check**: if the deregistering worker has generated IDs in the last 5 seconds, warn the caller (risk of duplicate worker ID during handoff)

---

### 7. Worker Health & Status

```
GET /v1/admin/workers/{worker_id}/status
```

**Response — 200 OK:**
```json
{
  "worker_id": 42,
  "status": "HEALTHY",
  "hostname": "ip-10-0-1-42.ec2.internal",
  "datacenter": "us-east-1a",
  "uptime_seconds": 86400,
  "clock": {
    "current_timestamp_ms": 1707264000000,
    "last_id_timestamp_ms": 1707263999985,
    "ntp_offset_ms": 0.3,
    "ntp_stratum": 2,
    "ntp_server": "169.254.169.123",
    "clock_backwards_events_24h": 0
  },
  "generation": {
    "ids_generated_total": 15482903,
    "ids_generated_last_minute": 2547,
    "ids_per_second_avg": 42.5,
    "ids_per_second_peak": 3891,
    "sequence_exhaustions_24h": 0,
    "current_sequence": 7
  },
  "registration": {
    "registration_id": "reg_2026020700001",
    "registered_at": 1707177600000,
    "lease_expiry": 1707264300000,
    "lease_remaining_seconds": 289,
    "zk_session_state": "CONNECTED"
  }
}
```

---

### 8. Clock Drift Monitoring

```
GET /v1/admin/clock/status
```

**Response — 200 OK:**
```json
{
  "cluster_clock_health": "HEALTHY",
  "workers_reporting": 847,
  "ntp_sync": {
    "synced_count": 845,
    "unsynced_count": 2,
    "unsynced_workers": [
      {
        "worker_id": 731,
        "hostname": "ip-10-0-3-88.ec2.internal",
        "ntp_offset_ms": 47.2,
        "last_sync_age_seconds": 3600,
        "severity": "WARNING"
      },
      {
        "worker_id": 199,
        "hostname": "ip-10-0-2-14.ec2.internal",
        "ntp_offset_ms": 312.8,
        "last_sync_age_seconds": 7200,
        "severity": "CRITICAL"
      }
    ]
  },
  "clock_stats": {
    "max_offset_ms": 312.8,
    "median_offset_ms": 0.8,
    "p99_offset_ms": 5.2,
    "backwards_events_24h": 1,
    "workers_with_backwards_events": [199]
  },
  "recommendation": "Worker 199 has critical clock drift (312.8ms). Consider draining and restarting this worker."
}
```

**Why this API matters:**
- Clock drift is the #1 operational risk for Snowflake-style systems
- A worker with drifted clock generates IDs with incorrect timestamps
- Small drift (< 10ms): IDs are slightly misordered — usually acceptable
- Large drift (> 100ms): IDs can collide with IDs from the same worker at a different time — **uniqueness violation risk**
- This API enables proactive detection before collisions occur

---

### 9. Sequence Statistics

```
GET /v1/admin/workers/{worker_id}/sequence-stats
```

**Response — 200 OK:**
```json
{
  "worker_id": 42,
  "period": "last_24h",
  "sequence_usage": {
    "max_sequence_per_ms": 347,
    "avg_sequence_per_ms": 2.1,
    "p99_sequence_per_ms": 89,
    "exhaustion_events": 0,
    "exhaustion_total_wait_ms": 0
  },
  "throughput": {
    "peak_ids_per_second": 3891,
    "peak_ids_per_millisecond": 347,
    "capacity_utilization_percent": 8.47,
    "headroom_ids_per_ms": 3749
  },
  "histogram_ids_per_ms": {
    "0": 72.3,
    "1-10": 21.5,
    "11-100": 5.8,
    "101-1000": 0.4,
    "1001-4096": 0.0
  }
}
```

**Use cases:**
- Capacity planning: determine if a worker needs to be split
- Alerting: detect when sequence utilization approaches the 4,096 limit
- Traffic analysis: understand burst patterns

---

### 10. Cluster Overview

```
GET /v1/admin/cluster
```

**Response — 200 OK:**
```json
{
  "cluster_name": "idgen-prod-global",
  "status": "HEALTHY",
  "config": {
    "custom_epoch": "2020-01-01T00:00:00.000Z",
    "custom_epoch_ms": 1577836800000,
    "timestamp_bits": 41,
    "worker_id_bits": 10,
    "sequence_bits": 12,
    "max_workers": 1024,
    "ids_per_ms_per_worker": 4096,
    "epoch_expires": "2089-09-06T00:00:00.000Z",
    "epoch_remaining_years": 63.6
  },
  "workers": {
    "total_registered": 847,
    "healthy": 845,
    "warning": 1,
    "critical": 1,
    "available_ids": 177
  },
  "traffic": {
    "total_ids_per_second": 42500,
    "total_ids_per_second_peak_24h": 187000,
    "system_capacity_ids_per_second": 4194304000,
    "utilization_percent": 0.001
  },
  "coordination": {
    "zookeeper_status": "CONNECTED",
    "zk_ensemble": [
      "zk1.internal:2181",
      "zk2.internal:2181",
      "zk3.internal:2181"
    ],
    "zk_session_timeout_ms": 30000
  }
}
```

---

## API Summary Table

### Core ID Generation APIs (Interview Focus)

| # | Method | Endpoint | Description | Latency |
|---|--------|----------|-------------|---------|
| 1 | SDK call | `generator.nextId()` | Generate a single ID (primary path) | ~1-2 us |
| 1b | `POST` | `/v1/id/generate` | Generate via REST (fallback) | ~1-2 ms |
| 2 | `POST` | `/v1/id/batch` | Generate batch of IDs | ~1-2 ms |
| 3 | `GET` | `/v1/id/{id}/decode` | Extract timestamp, worker, sequence from ID | ~0.5 ms |
| 4 | `POST` | `/v1/id/validate` | Validate one or more IDs | ~0.5 ms |

### Administration APIs (Production)

| # | Method | Endpoint | Description |
|---|--------|----------|-------------|
| 5 | `POST` | `/v1/admin/workers/register` | Register a new worker, get worker ID |
| 6 | `DELETE` | `/v1/admin/workers/{worker_id}` | Deregister a worker |
| 7 | `GET` | `/v1/admin/workers/{worker_id}/status` | Worker health and statistics |
| 8 | `GET` | `/v1/admin/clock/status` | Fleet-wide clock drift monitoring |
| 9 | `GET` | `/v1/admin/workers/{worker_id}/sequence-stats` | Sequence usage statistics |
| 10 | `GET` | `/v1/admin/cluster` | Cluster overview and configuration |

---

## Design Decisions in the API

### Why an Embedded SDK (Not an RPC Service) for the Hot Path?

| Aspect | Embedded SDK | RPC Service |
|--------|-------------|-------------|
| **Latency** | ~1-2 microseconds (in-process) | ~1-2 milliseconds (network RTT) |
| **Availability** | No network dependency | Depends on service being up |
| **Throughput** | 4M IDs/sec per worker | Limited by network + serialization |
| **Complexity** | Library dependency | Service deployment + LB + monitoring |
| **Consistency** | Single-process state (simple) | Must route to same worker (session affinity) |

**Our choice**: Embedded SDK for the hot path. REST API as a fallback for languages/platforms that can't embed the library, or for batch pre-generation.

### Why 64-bit Integers (Not Strings)?

- **Storage**: 8 bytes vs 36 bytes (UUID string) — 4.5x more compact
- **Indexing**: Integers are faster to compare than strings in B-trees
- **Network**: Smaller payload, no encoding overhead
- **Language support**: Every language has native 64-bit integer support
- **Database compatibility**: `BIGINT` is universal (MySQL, PostgreSQL, DynamoDB)

### Why a Custom Epoch?

- Unix epoch (1970) wastes 50+ years of timestamp bits on time before our system existed
- Custom epoch (e.g., 2020) maximizes the usable range of 41-bit timestamps
- **Trade-off**: All clients must agree on the epoch — it's a shared constant baked into the SDK
- **Risk**: If someone uses a different epoch, their IDs will decode to wrong timestamps

### Why REST Over gRPC for the Fallback API?

| Aspect | REST | gRPC |
|--------|------|------|
| **Simplicity** | Universal — any HTTP client | Requires protobuf tooling |
| **Debugging** | curl-friendly, readable | Binary protocol, harder to inspect |
| **Performance** | Adequate (ID generation is fast; network is the bottleneck) | Faster serialization, but not needed here |
| **Adoption** | Every team can use it immediately | Requires gRPC client library |

**Our choice**: REST for the external API. The ID generation itself is sub-microsecond — the network overhead of REST vs gRPC is negligible compared to the RTT. Simplicity wins.

### Why Include `purpose` in Batch Requests?

- **Auditing**: Track which service/use-case consumes the most IDs
- **Rate limiting**: Different purposes can have different quotas
- **Debugging**: "Where did these IDs come from?" — trace back to the requesting service and use case
- **Capacity planning**: Forecast ID consumption by purpose

---

## SDK Interface Specification

### Java SDK

```java
// Initialization (once, at service startup)
SnowflakeConfig config = SnowflakeConfig.builder()
    .customEpochMs(1577836800000L)  // Jan 1, 2020
    .zkConnectString("zk1:2181,zk2:2181,zk3:2181")
    .datacenterBits(5)
    .machineBits(5)
    .build();

SnowflakeGenerator generator = SnowflakeGenerator.create(config);
// Internally: connects to ZK, registers, gets worker ID

// Generate IDs (hot path — no network calls)
long id = generator.nextId();
long[] batch = generator.nextIds(100);

// Decode an existing ID
SnowflakeId decoded = SnowflakeId.parse(id);
long timestamp = decoded.getTimestampMs();     // absolute ms since Unix epoch
int workerId = decoded.getWorkerId();           // 0-1023
int sequence = decoded.getSequence();           // 0-4095

// Shutdown (deregisters from ZK)
generator.shutdown();
```

### Go SDK

```go
// Initialization
gen, err := snowflake.NewGenerator(snowflake.Config{
    CustomEpochMs: 1577836800000,
    ZKConnect:     "zk1:2181,zk2:2181,zk3:2181",
})
if err != nil {
    log.Fatal(err)
}
defer gen.Shutdown()

// Generate
id := gen.NextID()
ids := gen.NextIDs(100)

// Decode
parsed := snowflake.Parse(id)
fmt.Println(parsed.Timestamp, parsed.WorkerID, parsed.Sequence)
```

---

*This API contract document complements the [interview simulation](interview-simulation.md), [system flows](flow.md), and [naive-to-scale evolution](naive-to-scale-evolution.md).*
