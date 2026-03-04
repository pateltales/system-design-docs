# DynamoDB Streams — Deep Dive

## Table of Contents

1. [Overview](#1-overview)
2. [Stream Records](#2-stream-records)
3. [Shard Model](#3-shard-model)
4. [Ordering Guarantees](#4-ordering-guarantees)
5. [Stream Processing](#5-stream-processing)
6. [Lambda Triggers](#6-lambda-triggers)
7. [Use Cases](#7-use-cases)
8. [Streams and Global Tables](#8-streams-and-global-tables)
9. [Operational Concerns](#9-operational-concerns)
10. [Interview Angles](#10-interview-angles)

---

## 1. Overview

DynamoDB Streams captures a **time-ordered sequence of item-level modifications** in a
DynamoDB table and stores them in a log for up to **24 hours**.

```
┌──────────────────┐     ┌──────────────────┐     ┌──────────────────┐
│  DynamoDB Table   │     │  DynamoDB Stream  │     │  Consumers       │
│                   │     │                   │     │                  │
│  PutItem ────────▶│────▶│  Stream Record    │────▶│  Lambda          │
│  UpdateItem ─────▶│────▶│  Stream Record    │────▶│  Kinesis Adapter │
│  DeleteItem ─────▶│────▶│  Stream Record    │────▶│  Custom App      │
│                   │     │                   │     │                  │
│  (writes only)    │     │  24-hour retention │     │                  │
└──────────────────┘     └──────────────────┘     └──────────────────┘
```

**Key facts:**

| Property | Value |
|----------|-------|
| Retention | 24 hours |
| Latency | Near real-time |
| Ordering | Per-partition-key ordered |
| Readers per shard | 2 maximum (recommended: 1 per shard) |
| View types | KEYS_ONLY, NEW_IMAGE, OLD_IMAGE, NEW_AND_OLD_IMAGES |
| Encryption | At rest (same as table) |
| Performance impact | None on table (operates asynchronously) |
| Endpoint | Separate from DynamoDB table endpoint |

---

## 2. Stream Records

### 2.1 What's Captured

A stream record is created for every **item-level modification** (create, update, delete):

```
┌────────────────────────────────────────────────────────┐
│                    Stream Record                        │
├────────────────────────────────────────────────────────┤
│                                                        │
│  eventID: "abc123..."           ← unique record ID    │
│  eventName: "INSERT" | "MODIFY" | "REMOVE"            │
│  eventSource: "aws:dynamodb"                           │
│  eventVersion: "1.1"                                   │
│  dynamodb:                                             │
│    Keys:                        ← always present       │
│      UserId: {S: "U001"}                               │
│      OrderId: {S: "O123"}                               │
│    NewImage: {...}              ← if configured        │
│    OldImage: {...}              ← if configured        │
│    SequenceNumber: "12345"                              │
│    SizeBytes: 256                                       │
│    StreamViewType: "NEW_AND_OLD_IMAGES"                │
│  eventSourceARN: "arn:aws:dynamodb:..."                │
│                                                        │
└────────────────────────────────────────────────────────┘
```

### 2.2 Stream View Types

Configure at stream enablement time (cannot change without disabling/re-enabling):

| View Type | Content | Size | Use Case |
|-----------|---------|------|----------|
| `KEYS_ONLY` | Primary key attributes only | Smallest | Track which items changed |
| `NEW_IMAGE` | Full item after modification | Medium | Replicate current state |
| `OLD_IMAGE` | Full item before modification | Medium | Audit trail, undo |
| `NEW_AND_OLD_IMAGES` | Both before and after | Largest | Diff detection, full audit |

### 2.3 Event Types

| Event | Triggered By | OldImage | NewImage |
|-------|-------------|----------|----------|
| `INSERT` | PutItem (new item) | None | New item |
| `MODIFY` | PutItem (existing), UpdateItem | Previous item | Updated item |
| `REMOVE` | DeleteItem | Deleted item | None |

**Important:** If a PutItem or UpdateItem doesn't change any data (write is identical
to existing item), **no stream record is written**.

### 2.4 Stream Record Example

```json
{
  "eventID": "1",
  "eventName": "MODIFY",
  "eventVersion": "1.1",
  "eventSource": "aws:dynamodb",
  "dynamodb": {
    "Keys": {
      "UserId": {"S": "U001"},
      "OrderId": {"S": "O123"}
    },
    "OldImage": {
      "UserId": {"S": "U001"},
      "OrderId": {"S": "O123"},
      "Status": {"S": "PENDING"},
      "Amount": {"N": "50"}
    },
    "NewImage": {
      "UserId": {"S": "U001"},
      "OrderId": {"S": "O123"},
      "Status": {"S": "SHIPPED"},
      "Amount": {"N": "50"}
    },
    "SequenceNumber": "111",
    "SizeBytes": 256,
    "StreamViewType": "NEW_AND_OLD_IMAGES"
  }
}
```

---

## 3. Shard Model

### 3.1 Shard Architecture

Streams are organized into **shards**, which are containers for stream records:

```
┌─────────────────────────────────────────────────────────┐
│                   DynamoDB Stream                        │
├─────────────────────────────────────────────────────────┤
│                                                         │
│  ┌─────────────┐ ┌─────────────┐ ┌─────────────┐      │
│  │  Shard 1    │ │  Shard 2    │ │  Shard 3    │      │
│  │             │ │             │ │             │      │
│  │ Records for │ │ Records for │ │ Records for │      │
│  │ Partition A │ │ Partition B │ │ Partition C │      │
│  │             │ │             │ │             │      │
│  │ [r1][r2][r3]│ │ [r1][r2]   │ │ [r1][r2][r3]│      │
│  └─────────────┘ └─────────────┘ └─────────────┘      │
│                                                         │
│  Shards map to table partitions [INFERRED]              │
│  When partitions split, shards split too                │
│                                                         │
└─────────────────────────────────────────────────────────┘
```

### 3.2 Shard Properties

| Property | Detail |
|----------|--------|
| Creation | Automatic, tied to table partitions [INFERRED] |
| Splitting | Automatic on high write activity (follows partition splits) |
| Lifecycle | Ephemeral — created and deleted automatically |
| Parent-child | Shards have lineage; child inherits from parent on split |
| Records per shard | Variable, depends on write rate |
| 24-hour window | Records removed after 24 hours |

### 3.3 Shard Splitting

When a table partition splits, the corresponding stream shard also splits:

```
Before split:
  Partition P1 → Shard S1 (records for hash range [0x0000, 0xFFFF])

After split:
  Partition P1a → Shard S1a (records for [0x0000, 0x7FFF])  ← child
  Partition P1b → Shard S1b (records for [0x8000, 0xFFFF])  ← child

  Shard S1: closed (no new records)
  S1a.parentShardId = S1
  S1b.parentShardId = S1
```

**Critical:** Consumers must process parent shards before child shards to maintain
correct ordering.

### 3.4 Shard Discovery

**Method 1: Poll entire stream topology**
```
DescribeStream → returns all shards (active and closed)
Compare results over time to detect new shards
```

**Method 2: Discover child shards**
```
DescribeStream with ShardFilter → returns child shards of a specific parent
More efficient for tracking shard lineage
```

---

## 4. Ordering Guarantees

### 4.1 What's Guaranteed

| Guarantee | Scope |
|-----------|-------|
| Each record appears **exactly once** | Entire stream |
| Records for the **same item** appear in order | Within a shard (same partition key) |
| **Sequence numbers** reflect publication order | Within a shard |
| **Parent shards** before child shards | Application must enforce |

### 4.2 Per-Partition-Key Ordering

```
All writes to PK = "U001" are in the same partition → same shard:

  Shard for Partition containing "U001":
    seq 1: INSERT  {UserId: "U001", OrderId: "O001"}
    seq 2: MODIFY  {UserId: "U001", OrderId: "O001"}  Status → SHIPPED
    seq 3: INSERT  {UserId: "U001", OrderId: "O002"}
    seq 4: REMOVE  {UserId: "U001", OrderId: "O001"}

  A consumer processing this shard sees these in order: 1, 2, 3, 4
  → Correct chronological view of changes to PK "U001"
```

### 4.3 Cross-Partition Ordering

Records for **different partition keys** in different shards have **no ordering guarantee**:

```
Shard 1 (Partition A):     Shard 2 (Partition B):
  seq 1: INSERT U001/O001    seq 1: INSERT U002/O003
  seq 2: MODIFY U001/O001    seq 2: MODIFY U002/O003

  No guarantee that Shard 1 seq 1 happened before Shard 2 seq 1
  → Ordering is only meaningful within a shard
```

### 4.4 Exactly-Once Delivery

Each stream record appears **exactly once** in the stream. However, a consumer may
process it more than once if:
- Consumer crashes after processing but before checkpointing
- Lambda function times out and retries
- Consumer reads from a shard iterator that was already processed

**Consumers must be idempotent** to handle duplicate processing.

---

## 5. Stream Processing

### 5.1 API Operations

| API | Purpose |
|-----|---------|
| `ListStreams` | List stream descriptors for account, optionally filtered by table |
| `DescribeStream` | Get stream details: status, ARN, shard composition |
| `GetShardIterator` | Get iterator for a position in a shard |
| `GetRecords` | Read stream records from a shard using iterator |

### 5.2 Shard Iterator Types

| Type | Starting Position |
|------|------------------|
| `TRIM_HORIZON` | Oldest available record in the shard |
| `LATEST` | Most recent record (only new records going forward) |
| `AT_SEQUENCE_NUMBER` | Specific sequence number |
| `AFTER_SEQUENCE_NUMBER` | After a specific sequence number |

### 5.3 Processing Flow

```
┌──────────────────────────────────────────────────────────┐
│           Stream Processing Flow                          │
├──────────────────────────────────────────────────────────┤
│                                                          │
│  1. DescribeStream → get list of shards                 │
│                                                          │
│  2. For each shard:                                      │
│     a. GetShardIterator(TRIM_HORIZON) → iterator        │
│     b. Loop:                                             │
│        - GetRecords(iterator) → records + nextIterator   │
│        - Process records                                 │
│        - Checkpoint sequence number                      │
│        - iterator = nextIterator                         │
│        - If nextIterator is null → shard is closed       │
│                                                          │
│  3. When shard closes, discover child shards             │
│     → Process child shards                               │
│     → MUST process parent before children                │
│                                                          │
│  4. Repeat continuously                                  │
│                                                          │
└──────────────────────────────────────────────────────────┘
```

### 5.4 Kinesis Client Library (KCL) / Kinesis Adapter

The **DynamoDB Streams Kinesis Adapter** wraps the Streams API to provide a KCL-like
experience:

- Automatically discovers and processes shards
- Handles shard splits (parent before child)
- Manages checkpointing
- Distributes shards across workers
- Abstracts low-level API details

### 5.5 Reader Limit

**Maximum 2 concurrent readers per shard.** Exceeding this causes throttling.

For Global Tables, one reader slot is consumed by the replication process,
leaving only 1 slot for application consumers.

---

## 6. Lambda Triggers

### 6.1 How Lambda Integration Works

```
┌────────────┐     ┌─────────────┐     ┌──────────────┐
│  DynamoDB   │     │  DynamoDB    │     │   Lambda     │
│  Table      │────▶│  Stream      │────▶│   Function   │
│             │     │              │     │              │
│  PutItem    │     │  Stream      │     │  Invoked per │
│  UpdateItem │     │  Records     │     │  batch of    │
│  DeleteItem │     │              │     │  records     │
└────────────┘     └─────────────┘     └──────────────┘
```

### 6.2 Event Source Mapping Configuration

```json
{
  "EventSourceArn": "arn:aws:dynamodb:us-east-1:123456789:table/Orders/stream/...",
  "FunctionName": "ProcessOrderChanges",
  "Enabled": true,
  "BatchSize": 100,
  "MaximumBatchingWindowInSeconds": 5,
  "StartingPosition": "TRIM_HORIZON",
  "MaximumRetryAttempts": 3,
  "BisectBatchOnFunctionError": true,
  "MaximumRecordAgeInSeconds": 3600,
  "ParallelizationFactor": 1,
  "DestinationConfig": {
    "OnFailure": {
      "Destination": "arn:aws:sqs:us-east-1:123456789:dlq"
    }
  }
}
```

### 6.3 Key Parameters

| Parameter | Default | Range | Description |
|-----------|---------|-------|-------------|
| `BatchSize` | 100 | 1-10,000 | Max records per Lambda invocation |
| `MaximumBatchingWindowInSeconds` | 0 | 0-300 | Wait time to accumulate batch |
| `StartingPosition` | — | TRIM_HORIZON / LATEST | Where to start reading |
| `MaximumRetryAttempts` | -1 (infinite) | 0-10,000 | Retries on function error |
| `BisectBatchOnFunctionError` | false | — | Split batch in half on error |
| `MaximumRecordAgeInSeconds` | -1 (infinite) | 60-604,800 | Max age before discarding |
| `ParallelizationFactor` | 1 | 1-10 | Concurrent Lambda per shard |

### 6.4 Error Handling

```
Lambda invocation fails:
  │
  ├─ BisectBatchOnFunctionError = true?
  │   ├─ YES → Split batch in half, retry each half
  │   │         (isolates the poisonous record)
  │   └─ NO  → Retry entire batch
  │
  ├─ MaximumRetryAttempts reached?
  │   ├─ YES → Send to DLQ (if configured) or discard
  │   └─ NO  → Retry
  │
  └─ MaximumRecordAgeInSeconds exceeded?
      ├─ YES → Discard (too old to process)
      └─ NO  → Continue retrying
```

### 6.5 Parallelization Factor

By default, Lambda processes each shard with 1 concurrent invocation.
With `ParallelizationFactor > 1`:

```
Shard with records for PKs: A, B, C, D, E, F

ParallelizationFactor = 1:
  Lambda-1: processes A, B, C, D, E, F sequentially

ParallelizationFactor = 3:
  Lambda-1: processes A, D  (partition A, D)
  Lambda-2: processes B, E  (partition B, E)
  Lambda-3: processes C, F  (partition C, F)

Records for the SAME partition key are always processed in order.
Different partition keys can be processed in parallel.
```

---

## 7. Use Cases

### 7.1 Change Data Capture (CDC)

```
DynamoDB Table → Stream → Lambda → Elasticsearch / OpenSearch
                                → Redshift (via Firehose)
                                → S3 (archive)
                                → Another DynamoDB table
```

### 7.2 Materialized Views

```
Base Table: Orders (PK=UserId, SK=OrderId)
  │
  ▼ Stream
  │
  Lambda: Aggregate order totals
  │
  ▼
Aggregate Table: UserStats (PK=UserId)
  {UserId: "U001", TotalOrders: 42, TotalSpent: 1500.00}
```

### 7.3 Cross-Region Replication (Global Tables)

DynamoDB Streams is the backbone of Global Tables replication:

```
Region A: Table writes → Stream → Replication process → Region B: Table
Region B: Table writes → Stream → Replication process → Region A: Table
```

### 7.4 Event-Driven Architectures

```
Order Table write → Stream → Lambda:
  ├─ Send confirmation email (via SES)
  ├─ Update inventory (via another DynamoDB table)
  ├─ Publish to SNS for downstream services
  └─ Emit CloudWatch custom metric
```

### 7.5 Audit Trail

```
Stream with NEW_AND_OLD_IMAGES → Lambda → S3 / CloudWatch Logs

Captures:
  - Who changed what (via Lambda context)
  - Before/after values
  - Timestamp
  - Retained indefinitely (S3) vs 24h (stream)
```

---

## 8. Streams and Global Tables

### 8.1 How Global Tables Use Streams

Global Tables replication is built on top of DynamoDB Streams:

```
┌──────────────────┐                    ┌──────────────────┐
│  Region A Table   │                    │  Region B Table   │
│                   │                    │                   │
│  Write: X = 10   │                    │                   │
│       │          │                    │                   │
│       ▼          │                    │                   │
│  Stream Record   │                    │                   │
│       │          │                    │                   │
│       ▼          │                    │                   │
│  Replication     │──── async ────────▶│  Write: X = 10   │
│  Process         │   (0.5-2.5s)       │                   │
│                   │                    │                   │
└──────────────────┘                    └──────────────────┘
```

### 8.2 Stream Reader Limit Impact

With Global Tables:
- 1 of the 2 reader slots per shard is used by the replication process
- Only 1 slot remaining for application consumers
- This limits your ability to run custom stream consumers alongside Global Tables

### 8.3 Filtering Replication Events

When consuming streams on a Global Tables table, you'll see both local writes and
replicated writes. Use the `aws:rep:updateregion` attribute to filter:

```python
def handler(event, context):
    for record in event['Records']:
        # Check if this is a local write or replicated
        new_image = record['dynamodb'].get('NewImage', {})
        source_region = new_image.get('aws:rep:updateregion', {}).get('S', '')

        if source_region == 'us-east-1':  # Our region
            # Process local write
            pass
        else:
            # Skip replicated write (already processed in source region)
            pass
```

---

## 9. Operational Concerns

### 9.1 Stream Lag Monitoring

```
Key metric: IteratorAge
  → Time difference between current time and when the last record was written
  → High iterator age = consumer is falling behind

CloudWatch alarm:
  IteratorAge > 60000 ms (1 minute) → Warning
  IteratorAge > 300000 ms (5 minutes) → Critical

Root causes of high lag:
  1. Lambda function too slow (increase memory/timeout)
  2. Batch size too small (increase to process more per invocation)
  3. Too many records (increase ParallelizationFactor)
  4. Function errors causing retries
```

### 9.2 Common Failure Scenarios

| Scenario | Impact | Resolution |
|----------|--------|-----------|
| Lambda error | Retries block shard processing | Fix function, use DLQ, enable bisect |
| Consumer too slow | Iterator age grows, may lose records after 24h | Increase parallelization, optimize consumer |
| Shard throttling | GetRecords returns empty | Reduce readers to ≤ 2 per shard |
| Poison record | Single bad record blocks entire shard | Enable BisectBatchOnFunctionError |

### 9.3 Record Size Considerations

Stream record size depends on view type and item size:

```
KEYS_ONLY:           ~100-200 bytes per record (just PK + SK)
NEW_IMAGE:           ~item size + overhead
OLD_IMAGE:           ~item size + overhead
NEW_AND_OLD_IMAGES:  ~2× item size + overhead

For 400 KB items with NEW_AND_OLD_IMAGES:
  ~800 KB per stream record
  At 1,000 writes/sec → ~800 MB/sec of stream data
  → Significant Lambda cost for processing
```

### 9.4 24-Hour Retention Limitation

```
If your consumer is down for > 24 hours:
  → Records from before the outage are LOST
  → Stream trimming removes records older than 24h
  → No way to recover

Mitigation:
  1. Monitor IteratorAge — alert before 24h
  2. Archive stream records to S3 in real-time (Lambda → S3)
  3. Use DynamoDB export for full table snapshots
  4. Design consumers for resilience (auto-restart, auto-scaling)
```

---

## 10. Interview Angles

### 10.1 "How do DynamoDB Streams work?"

"DynamoDB Streams captures every item-level modification as a stream record in a
time-ordered log with 24-hour retention. Stream records are organized into shards
that map to table partitions. Within a shard, records for the same partition key
are strictly ordered by sequence number. Consumers can read via the Streams API,
Lambda triggers, or the Kinesis adapter. The stream supports four view types
ranging from keys-only to full before/after images."

### 10.2 "What ordering guarantees does DynamoDB Streams provide?"

```
1. Per-item ordering: All changes to the same item (same PK+SK) appear
   in the order they were committed. This is guaranteed because all writes
   to the same partition key go through the same partition leader and
   are assigned monotonically increasing sequence numbers.

2. No cross-partition ordering: Changes to different partition keys
   (different shards) have no ordering relationship.

3. Parent-before-child: When shards split, consumers must process the
   parent shard completely before processing child shards to maintain
   the ordering guarantee during partition splits.

4. Exactly-once publication: Each record appears exactly once in the stream.
   But consumers may process it multiple times (at-least-once delivery
   from the consumer's perspective).
```

### 10.3 "How would you handle a poison record in a stream?"

```
Problem: One bad record causes Lambda to fail repeatedly,
blocking all subsequent records in the shard.

Solution layers:
  1. BisectBatchOnFunctionError = true
     → Splits batch in half on error, isolates the bad record
     → Eventually narrows down to the single bad record

  2. MaximumRetryAttempts = 3
     → Don't retry forever, limit retries

  3. DLQ destination (SQS or SNS)
     → Failed records sent to DLQ for manual inspection
     → Remaining records continue processing

  4. MaximumRecordAgeInSeconds = 3600
     → Discard records older than 1 hour (prevents infinite blocking)

Best practice: Enable ALL four safeguards simultaneously.
```

### 10.4 "What's the relationship between Streams and Global Tables?"

"Global Tables is built on top of DynamoDB Streams. Each regional replica's stream
captures local writes, and DynamoDB's replication process reads these stream records
and applies them to other regional replicas. This is asynchronous replication with
typical 0.5-2.5 second latency. Because the replication process consumes one of the
two reader slots per shard, Global Tables tables can only support one additional
custom stream consumer."

### 10.5 Design Decision: Why 24-Hour Retention?

```
Why not longer?
  1. Storage cost: Stream data is redundant with the table itself
  2. Operational simplicity: Shorter retention = less data to manage
  3. Use case fit: Most CDC consumers process in near-real-time
  4. For longer retention: archive to S3 via Lambda

Why not shorter (e.g., 1 hour)?
  1. Consumer recovery: 24h gives time to fix failures and catch up
  2. Reprocessing: Can replay up to 24h of changes
  3. Global Tables: Cross-region replication may need hours during
     network partitions

Trade-off: 24 hours balances cost, recovery time, and operational needs.
```

---

## Appendix: Key Numbers

| Property | Value |
|----------|-------|
| Retention | 24 hours |
| Readers per shard | 2 maximum |
| Readers per shard (Global Tables) | 1 for application (1 used by replication) |
| Lambda batch size | 1-10,000 records |
| Lambda batching window | 0-300 seconds |
| Parallelization factor | 1-10 per shard |
| Maximum retry attempts | 0-10,000 (or infinite) |
| Maximum record age | 60-604,800 seconds (or infinite) |
| View types | KEYS_ONLY, NEW_IMAGE, OLD_IMAGE, NEW_AND_OLD_IMAGES |
| Endpoint | Separate from DynamoDB table endpoint |
| Stream record | Contains keys + optional before/after images |
