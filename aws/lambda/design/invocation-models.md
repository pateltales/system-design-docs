# Lambda Invocation Models — Deep Dive

> Companion doc for [interview-simulation.md](interview-simulation.md)
> Covers: Synchronous, asynchronous, and event-source-mapping invocation; retry semantics; error handling; destinations; payload contracts

---

## Table of Contents

1. [Overview — Three Invocation Models](#1-overview--three-invocation-models)
2. [Synchronous Invocation (RequestResponse)](#2-synchronous-invocation-requestresponse)
3. [Asynchronous Invocation (Event)](#3-asynchronous-invocation-event)
4. [Event Source Mappings (Poll-Based)](#4-event-source-mappings-poll-based)
5. [SQS Event Source Mapping — Deep Dive](#5-sqs-event-source-mapping--deep-dive)
6. [Kinesis Event Source Mapping — Deep Dive](#6-kinesis-event-source-mapping--deep-dive)
7. [DynamoDB Streams Event Source Mapping — Deep Dive](#7-dynamodb-streams-event-source-mapping--deep-dive)
8. [Kafka Event Source Mapping — Deep Dive](#8-kafka-event-source-mapping--deep-dive)
9. [Amazon MQ and DocumentDB Event Source Mappings](#9-amazon-mq-and-documentdb-event-source-mappings)
10. [Retry Semantics — Unified View](#10-retry-semantics--unified-view)
11. [Error Handling Patterns](#11-error-handling-patterns)
12. [Destinations and Dead-Letter Queues](#12-destinations-and-dead-letter-queues)
13. [Payload Contracts and Limits](#13-payload-contracts-and-limits)
14. [Throttling Behavior Across Models](#14-throttling-behavior-across-models)
15. [Three-Model Comparison Matrix](#15-three-model-comparison-matrix)
16. [Design Decisions and Trade-offs](#16-design-decisions-and-trade-offs)
17. [Interview Angles](#17-interview-angles)

---

## 1. Overview — Three Invocation Models

Lambda supports three fundamentally different invocation patterns. Each has distinct retry semantics, error handling, payload contracts, and scaling behavior.

```
┌─────────────────────────────────────────────────────────────────────┐
│                    Lambda Invocation Models                         │
├─────────────────────┬──────────────────────┬────────────────────────┤
│   SYNCHRONOUS       │   ASYNCHRONOUS       │   EVENT SOURCE MAPPING │
│   (RequestResponse) │   (Event)            │   (Poll-Based)         │
├─────────────────────┼──────────────────────┼────────────────────────┤
│ Caller waits        │ Caller gets 202      │ Lambda polls source    │
│ Lambda returns      │ Lambda queues event  │ Lambda manages batches │
│ response directly   │ and retries          │ and retries            │
├─────────────────────┼──────────────────────┼────────────────────────┤
│ API Gateway         │ S3                   │ SQS                    │
│ ALB                 │ SNS                  │ Kinesis                │
│ CloudFront          │ EventBridge          │ DynamoDB Streams       │
│ Cognito             │ SES                  │ MSK / Kafka            │
│ SDK / CLI           │ CloudWatch Logs      │ Amazon MQ              │
│ Function URL        │ CloudFormation       │ DocumentDB             │
│ Step Functions      │ CodeCommit           │                        │
│ (RequestResponse)   │ IoT                  │                        │
│                     │ Step Functions (Fire  │                        │
│                     │   and Forget)        │                        │
└─────────────────────┴──────────────────────┴────────────────────────┘
```

### The Core Distinction

| Aspect | Synchronous | Asynchronous | Event Source Mapping |
|--------|-------------|--------------|---------------------|
| **Who invokes Lambda?** | Caller directly | Caller → internal queue → Lambda | Lambda polls → invokes itself |
| **Who owns retries?** | Caller | Lambda (internal queue) | Lambda (event source mapping) |
| **Response to caller** | Function output | 202 Accepted | N/A (no external caller) |
| **Error visibility** | Caller sees error | Destination / DLQ | Source-specific (visibility timeout, iterator) |

---

## 2. Synchronous Invocation (RequestResponse)

### 2.1 How It Works

```
┌──────────┐    Invoke (RequestResponse)    ┌──────────────┐
│  Caller  │ ──────────────────────────────► │   Lambda     │
│ (SDK/API │                                 │   Frontend   │
│  Gateway │                                 │   Service    │
│  /CLI)   │ ◄────────────────────────────── │              │
│          │    Response (payload + status)   │              │
└──────────┘                                 └──────┬───────┘
                                                    │
                                             ┌──────▼───────┐
                                             │  Execution   │
                                             │ Environment  │
                                             │              │
                                             │ Run function │
                                             │ Return result│
                                             └──────────────┘
```

**Sequence:**
1. Caller sends `Invoke` API call with `InvocationType: RequestResponse`
2. Lambda Frontend Service receives the request
3. Frontend routes to an execution environment (cold start if needed)
4. Function executes synchronously
5. Response payload returned to caller through the entire chain
6. Connection held open for the duration of execution

### 2.2 Payload Contract

| Parameter | Limit |
|-----------|-------|
| Request payload (synchronous) | 6 MB |
| Response payload | 6 MB |
| Request payload (function URL streaming) | 6 MB request, 20 MB response (soft limit) |

### 2.3 Response Format

```json
{
    "StatusCode": 200,
    "ExecutedVersion": "$LATEST",
    "LogResult": "U1RBUlQgUmVxdWVzdElkOi4uLg==",
    "FunctionError": "Unhandled",
    "Payload": "{ \"errorMessage\": \"...\", \"errorType\": \"...\" }"
}
```

**Key fields:**
- **StatusCode 200**: Indicates Lambda *platform* successfully invoked the function — even if the function threw an error
- **FunctionError**: Present only when the function returned an error (`"Handled"` or `"Unhandled"`)
- **Payload**: The actual function response (or error details)
- **LogResult**: Base64-encoded last 4 KB of logs (only with `--log-type Tail`)

### 2.4 Error Handling

| Error Type | HTTP Status | Retry? | Example |
|------------|-------------|--------|---------|
| Function error (handled) | 200 + FunctionError | Caller's responsibility | `throw new Error("bad input")` |
| Function error (unhandled) | 200 + FunctionError | Caller's responsibility | Uncaught exception, OOM |
| Throttling | 429 TooManyRequestsException | SDK auto-retries | Concurrency limit reached |
| Invalid request | 400 | No retry | Malformed payload |
| Service error | 500/502/503 | SDK auto-retries | Internal Lambda error |
| Timeout | 200 + FunctionError | Caller's responsibility | Function exceeds timeout |

**Critical insight**: Lambda returns HTTP 200 for function errors. The caller must inspect `FunctionError` in the response to detect failures. This is a common source of bugs in API Gateway integrations.

### 2.5 Retry Behavior

**Lambda does NOT retry synchronous invocations.** The caller owns retry logic.

However, AWS SDKs have built-in retry policies:
- **Default SDK retries**: 3 attempts (varies by SDK)
- **Retried errors**: Throttling (429), server errors (500/502/503), timeouts
- **NOT retried**: Function errors (200 with FunctionError), client errors (400)

### 2.6 Connection and Timeout Considerations

- Client must keep connection open for the full function duration (up to 900 seconds)
- API Gateway has a **29-second hard timeout** for synchronous Lambda proxy integrations
- ALB has a configurable idle timeout (default 60 seconds)
- Function URL: no intermediate timeout, but client connection timeout applies
- Lambda does NOT wait for external extensions to complete before sending the response

### 2.7 Which Services Use Synchronous Invocation?

| Service | Invocation Pattern | Notes |
|---------|-------------------|-------|
| API Gateway (REST/HTTP) | Sync | 29s timeout, 6 MB payload |
| Application Load Balancer | Sync | Multi-value headers supported |
| CloudFront (Lambda@Edge) | Sync | 5s viewer / 30s origin timeout |
| Amazon Cognito | Sync | User pool triggers |
| Amazon Alexa | Sync | Skill invocation |
| AWS Step Functions (RequestResponse) | Sync | Waits for result |
| Function URL | Sync | Direct HTTPS endpoint |
| AWS SDK / CLI | Sync (default) | `InvocationType: RequestResponse` |

---

## 3. Asynchronous Invocation (Event)

### 3.1 How It Works

```
┌──────────┐   Invoke (Event)   ┌──────────────┐   Enqueue   ┌─────────────┐
│  Caller  │ ─────────────────► │   Lambda     │ ──────────► │  Internal   │
│ (S3/SNS/ │                    │   Frontend   │             │  Async      │
│  SDK)    │ ◄──── 202 ──────── │   Service    │             │  Queue      │
└──────────┘   (Accepted)       └──────────────┘             └──────┬──────┘
                                                                    │
                                                              Dequeue & Invoke
                                                                    │
                                                             ┌──────▼──────┐
                                                             │  Execution  │
                                                             │ Environment │
                                                             └──────┬──────┘
                                                                    │
                                                        ┌───────────┼───────────┐
                                                        │           │           │
                                                   ┌────▼────┐ ┌───▼────┐ ┌───▼──────┐
                                                   │ Success │ │ Retry  │ │ Discard  │
                                                   │  Dest   │ │ (up to │ │ → DLQ /  │
                                                   │         │ │  2x)   │ │ Failure  │
                                                   └─────────┘ └────────┘ │ Dest     │
                                                                          └──────────┘
```

**Sequence:**
1. Caller sends `Invoke` API call with `InvocationType: Event`
2. Lambda Frontend validates the event and **immediately returns 202 Accepted**
3. Event is placed on an internal asynchronous queue [INFERRED: SQS-based internal queue]
4. Lambda's async invocation service dequeues events and invokes functions
5. On success: optionally sends to success destination
6. On failure: retries up to 2 times, then sends to DLQ or failure destination

### 3.2 Payload Contract

| Parameter | Limit |
|-----------|-------|
| Request payload (asynchronous) | 256 KB |
| Response | None (caller already received 202) |

### 3.3 The Internal Queue

The async queue is a critical piece of infrastructure:

- **Eventually consistent**: Functions may receive duplicate events even without errors
- **Not FIFO**: Events may be processed out of order
- **Backpressure**: When the queue grows long, Lambda increases retry intervals and reduces read rate
- **Event aging**: Events can age out of the queue without being processed if the queue is congested
- **Unprocessed events**: May be deleted from the queue without being sent to the function

[INFERRED] The internal queue is likely built on SQS or a similar durable queuing system within the Lambda service, providing at-least-once delivery semantics.

### 3.4 Retry Behavior — Defaults and Configuration

| Configuration | Default | Range |
|---------------|---------|-------|
| Maximum retry attempts | 2 (3 total invocations) | 0–2 |
| Maximum event age | 6 hours | 60 seconds – 6 hours |

**Retry timing:**
- **1st retry**: ~1 minute after initial failure
- **2nd retry**: ~2 minutes after 1st retry

**What triggers retries:**
- Function errors (exceptions, timeouts)
- Runtime errors
- Throttling (429) — with exponential backoff
- System errors (500-series)

**Exponential backoff for throttling/system errors:**
- Starts at 1 second
- Increases exponentially up to 5 minutes maximum
- Applies throughout the 6-hour (default) maximum event age window

### 3.5 Reserved Concurrency = Zero Behavior

When reserved concurrency is set to 0:
- Lambda **immediately sends new events to DLQ or on-failure destination WITHOUT retries**
- Events already queued while concurrency was zero must be consumed from DLQ/destination
- This is effectively a "pause" mechanism for async processing

### 3.6 Which Services Use Asynchronous Invocation?

| Service | Notes |
|---------|-------|
| Amazon S3 | Object events (Created, Deleted, etc.) |
| Amazon SNS | Topic subscriptions |
| Amazon EventBridge | Rules targeting Lambda |
| Amazon SES | Email receiving |
| Amazon CloudWatch Logs | Subscription filters |
| AWS CloudFormation | Custom resources |
| AWS CodeCommit | Repository triggers |
| AWS IoT | Rule actions |
| AWS Config | Config rules evaluation |
| AWS Step Functions | Fire-and-forget (`InvocationType: Event`) |

### 3.7 Async Invocation Configuration API

```bash
# Configure retry behavior
aws lambda put-function-event-invoke-config \
    --function-name my-function \
    --maximum-retry-attempts 1 \
    --maximum-event-age-in-seconds 3600

# Configure destinations
aws lambda put-function-event-invoke-config \
    --function-name my-function \
    --destination-config '{
        "OnSuccess": {"Destination": "arn:aws:sqs:us-east-1:123456789012:success-queue"},
        "OnFailure": {"Destination": "arn:aws:sqs:us-east-1:123456789012:failure-queue"}
    }'
```

---

## 4. Event Source Mappings (Poll-Based)

### 4.1 How It Works

```
┌──────────────┐         ┌──────────────────┐         ┌──────────────┐
│  Event       │  Poll   │  Event Source     │ Invoke  │  Lambda      │
│  Source      │◄────────│  Mapping          │────────►│  Function    │
│              │         │  (Event Pollers)  │         │              │
│  SQS /       │ Records │                   │ Batch   │              │
│  Kinesis /   │────────►│  Batching logic:  │ payload │              │
│  DynamoDB /  │         │  - batch size     │         │              │
│  Kafka /     │         │  - batch window   │         │              │
│  MQ /        │         │  - 6 MB limit     │         │              │
│  DocumentDB  │         │                   │         │              │
└──────────────┘         └──────────────────┘         └──────────────┘
```

**Key distinction**: Unlike sync/async where an external caller invokes Lambda, here **Lambda itself polls** the event source and invokes the function with batches of records.

### 4.2 Event Pollers

Lambda uses dedicated compute units called **event pollers** to actively poll event sources:

- **Default mode**: Lambda automatically scales pollers based on message volume
- **Provisioned mode**: You configure minimum and maximum pollers for predictable throughput
- Each poller is a separate compute resource managed by the Lambda service [INFERRED]

### 4.3 Batching — The Three Triggers

Lambda invokes the function when **any** of these conditions is met:

| Trigger | Description |
|---------|-------------|
| **Batch size reached** | Maximum number of records collected |
| **Batching window expires** | Maximum time to buffer records |
| **Payload size reaches 6 MB** | Hard limit, non-configurable |

**Batching window defaults by source:**

| Source | Default Window | Configurable Range |
|--------|---------------|-------------------|
| Kinesis | 0 seconds (invoke immediately) | 0–300 seconds |
| DynamoDB Streams | 0 seconds | 0–300 seconds |
| SQS | 0 seconds | 0–300 seconds |
| MSK / Self-managed Kafka | 500 ms | 0–300 seconds |
| Amazon MQ | 500 ms | 0–300 seconds |
| DocumentDB | 500 ms | 0–300 seconds |

**Important**: For Kafka sources, once you change the default 500 ms window, you cannot revert to 500 ms — you must create a new event source mapping.

### 4.4 Two Categories of Event Sources

Event source mappings behave very differently depending on whether the source is a **stream** or a **queue**:

| Aspect | Stream Sources (Kinesis, DynamoDB) | Queue Sources (SQS) | Streaming Sources (Kafka, MQ, DocumentDB) |
|--------|-----------------------------------|---------------------|------------------------------------------|
| **Polling unit** | Shard | Queue | Partition / Channel |
| **Ordering** | Per-shard (per-partition-key) | No ordering (Standard) / Per-group (FIFO) | Per-partition |
| **Checkpoint** | Shard iterator (sequence number) | Message deletion | Consumer group offset |
| **On error** | Block shard processing | Message returns to queue | Varies by source |
| **Parallelization** | ParallelizationFactor (1–10) | Concurrency scales with queue depth | Scales with partitions |
| **Data retention** | In stream (24h–365d for Kinesis) | In queue (visibility timeout) | In topic (retention policy) |

### 4.5 Event Source Mapping States

```
CREATING ──► ENABLING ──► ENABLED ──► UPDATING ──► ENABLED
                                         │
                                    DISABLING ──► DISABLED
```

States:
- **Creating**: Being provisioned
- **Enabling/Enabled**: Active polling
- **Disabling/Disabled**: Polling stopped
- **Updating**: Configuration change in progress

### 4.6 Event Source Mapping API

```bash
# Create
aws lambda create-event-source-mapping \
    --function-name my-function \
    --event-source-arn arn:aws:sqs:us-east-1:123456789012:my-queue \
    --batch-size 10 \
    --maximum-batching-window-in-seconds 5

# List
aws lambda list-event-source-mappings --function-name my-function

# Update
aws lambda update-event-source-mapping \
    --uuid "a1b2c3d4-5678-90ab-cdef-11111EXAMPLE" \
    --batch-size 20

# Delete
aws lambda delete-event-source-mapping --uuid "a1b2c3d4-5678-90ab-cdef-11111EXAMPLE"
```

---

## 5. SQS Event Source Mapping — Deep Dive

### 5.1 Architecture

```
┌──────────────┐    Long Poll     ┌─────────────────┐    Invoke    ┌─────────────┐
│              │◄─────────────────│  Event Pollers   │────────────►│   Lambda    │
│  SQS Queue   │                  │  (auto-scaled    │             │   Function  │
│              │    Messages      │   or provisioned)│             │             │
│  Standard /  │─────────────────►│                  │             │             │
│  FIFO        │                  │  Batch:          │             │             │
│              │                  │  - size: 1-10K   │             │             │
│              │   Delete on      │  - window: 0-300s│             │             │
│              │◄── success ──────│  - max 6 MB      │             │             │
└──────────────┘                  └─────────────────┘             └─────────────┘
```

### 5.2 Configuration Parameters

| Parameter | Default | Range | Notes |
|-----------|---------|-------|-------|
| Batch size | 10 | 1–10,000 | Higher = fewer invocations, more latency |
| Batching window | 0 seconds | 0–300 seconds | Trade latency for efficiency |
| Maximum concurrency | — | 2–1,000 | Controls function scaling for this source |

### 5.3 Scaling Behavior

**Standard queue (default mode):**
- Lambda uses long polling to read messages
- Scales up pollers based on queue depth and message arrival rate
- With very low traffic, Lambda may wait up to 20 seconds before invoking
- Concurrency scales with queue depth

**Standard queue (provisioned mode):**
- **Minimum pollers**: 2–200 (default: 2)
- **Maximum pollers**: 2–2,000 (default: 200)
- 3x faster auto-scaling for traffic spikes
- Up to 20,000 concurrent invokes [UNVERIFIED: based on "16x higher capacity"]
- Scaling: up to 1,000 concurrency per minute

**Event poller capacity (SQS):**

| Metric | Per Poller |
|--------|-----------|
| Throughput | Up to 1 MB/sec |
| Concurrent invocations | Up to 10 |
| Polling API calls | Up to 10/sec |

**Capacity planning formula:**
```
EPS per poller = min(
    ceil(1024 / avg_event_size_KB),
    ceil(10 / avg_function_duration_sec) × batch_size,
    min(100, 10 × batch_size)
)

Required pollers = Peak_EPS / EPS_per_poller
```

**Example**: 1,000 events/sec, 3 KB avg, 100 ms duration, batch size 10 → ~100 EPS/poller → 10 minimum pollers.

### 5.4 Error Handling

**Default behavior (no ReportBatchItemFailures):**
1. Function fails → entire batch fails
2. All messages become visible again after visibility timeout
3. Messages retry until they succeed or reach `maxReceiveCount` → DLQ

**With ReportBatchItemFailures:**
```json
{
    "batchItemFailures": [
        { "itemIdentifier": "message-id-2" },
        { "itemIdentifier": "message-id-5" }
    ]
}
```
- Only failed messages return to the queue
- Successfully processed messages are deleted
- Dramatically reduces reprocessing

### 5.5 FIFO Queue Differences

| Aspect | Standard Queue | FIFO Queue |
|--------|---------------|------------|
| Ordering | Best-effort | Strict within MessageGroupId |
| Deduplication | None | MessageDeduplicationId |
| Throughput | Virtually unlimited | 300 msg/s (3,000 with batching) |
| Lambda scaling | Scales freely | One concurrent invocation per MessageGroupId |
| Event attributes | Basic | Includes SequenceNumber, MessageGroupId, MessageDeduplicationId |

### 5.6 Visibility Timeout Considerations

- When Lambda reads a message, it becomes invisible for the visibility timeout duration
- If function succeeds → message is deleted
- If function fails → message reappears after visibility timeout
- **Best practice**: Set visibility timeout to 6× function timeout

---

## 6. Kinesis Event Source Mapping — Deep Dive

### 6.1 Architecture

```
┌──────────────┐                  ┌─────────────────┐             ┌─────────────┐
│  Kinesis     │   GetRecords /   │  Event Source    │  Invoke     │   Lambda    │
│  Stream      │   SubscribeToShard│  Mapping        │────────────►│   Function  │
│              │◄─────────────────│                  │             │             │
│  Shard 1 ───►│   Records        │  Per shard:      │             │             │
│  Shard 2 ───►│─────────────────►│  - 1 poller      │             │             │
│  Shard 3 ───►│                  │  - Parallel: 1-10│             │             │
│              │                  │  - Iterator      │             │             │
│              │                  │    tracking      │             │             │
└──────────────┘                  └─────────────────┘             └─────────────┘
```

### 6.2 Configuration Parameters

| Parameter | Default | Range | Notes |
|-----------|---------|-------|-------|
| Batch size | 100 | 1–10,000 | Records per invocation |
| Batching window | 0 seconds | 0–300 seconds | Buffer time |
| Parallelization factor | 1 | 1–10 | Concurrent batches per shard |
| Bisect batch on error | false | true/false | Split failed batch in half |
| Maximum retry attempts | -1 (infinite) | 0–10,000 | -1 = retry forever |
| Maximum record age | -1 (infinite) | 60–604,800 seconds (7 days) | -1 = never expire |
| Starting position | LATEST or TRIM_HORIZON | — | Where to begin reading |
| On-failure destination | None | SQS or SNS ARN | Capture discarded batches |
| Tumbling window | None | 0–900 seconds | Stateful aggregation |

### 6.3 Polling Behavior

**Standard iterator (shared throughput):**
- Lambda polls each shard at a base rate of **once per second**
- Shares the 2 MB/sec read throughput with other consumers
- Maximum of 5 GetRecords calls per second per shard (shared across all consumers)

**Enhanced fan-out (dedicated throughput):**
- Dedicated 2 MB/sec per shard per consumer
- Uses HTTP/2 push (SubscribeToShard) — records pushed over long-lived connections
- Compresses request headers
- Reduces latency compared to standard polling

### 6.4 Parallelization Factor

```
Without parallelization (factor = 1):
┌─────────┐     ┌────────────┐     ┌──────────┐
│ Shard 1 │────►│ 1 batch at │────►│ Lambda   │
│         │     │ a time     │     │ instance │
└─────────┘     └────────────┘     └──────────┘

With parallelization (factor = 5):
┌─────────┐     ┌────────────┐     ┌──────────┐
│ Shard 1 │────►│ Batch 1    │────►│ Lambda 1 │
│         │     │ Batch 2    │────►│ Lambda 2 │
│         │     │ Batch 3    │────►│ Lambda 3 │
│         │     │ Batch 4    │────►│ Lambda 4 │
│         │     │ Batch 5    │────►│ Lambda 5 │
└─────────┘     └────────────┘     └──────────┘
```

- Records are split by partition key hash into sub-batches
- **In-order processing guaranteed at the partition-key level** (not shard level)
- Concurrent invocations per shard = parallelization factor
- Total max concurrent invocations = number of shards × parallelization factor
- Example: 100 shards × factor 10 = 1,000 concurrent invocations

### 6.5 Error Handling — Stream Behavior

**Default (no error configuration):**
1. Function fails on a batch
2. Event source mapping **blocks the entire shard** — no new records processed
3. Retries the same batch indefinitely
4. IteratorAge increases, alerting to processing delay

**Why blocking?** Streams require in-order processing. Skipping a failed batch would break ordering guarantees.

**Configurable error handling:**

| Configuration | Effect |
|---------------|--------|
| `BisectBatchOnFunctionError: true` | Split failed batch in half, retry each half separately — binary search for the poison record |
| `MaximumRetryAttempts: N` | Limit retries (0 = no retries, discard immediately) |
| `MaximumRecordAgeInSeconds: N` | Discard records older than N seconds |
| `DestinationConfig.OnFailure` | Send discarded batch metadata to SQS/SNS |

**Bisect-on-error deep dive:**
```
Original batch: [A, B, C, D, E, F, G, H] → FAILS
                        │
             ┌──────────┴──────────┐
             ▼                     ▼
    [A, B, C, D] → OK    [E, F, G, H] → FAILS
                                   │
                          ┌────────┴────────┐
                          ▼                 ▼
                   [E, F] → OK     [G, H] → FAILS
                                         │
                                   ┌─────┴─────┐
                                   ▼           ▼
                              [G] → OK    [H] → FAILS (poison record!)
                                              │
                                         Discard + send to
                                         on-failure destination
```

### 6.6 Tumbling Windows (Stateful Processing)

- Enables aggregating records over a time window (0–900 seconds)
- Lambda maintains a **state** object across invocations within the window
- When the window closes, Lambda invokes with `isFinalInvokeForWindow: true`
- Use case: per-minute aggregations, running totals

### 6.7 Maximum Concurrent Invocations

```
Concurrent invocations = Number of shards × Parallelization factor

Example:
- 100 shards × 1 (default) = 100 concurrent invocations
- 100 shards × 10 (max)    = 1,000 concurrent invocations
```

---

## 7. DynamoDB Streams Event Source Mapping — Deep Dive

### 7.1 Architecture

Very similar to Kinesis, but with important differences:

```
┌────────────────────┐          ┌─────────────────┐         ┌─────────────┐
│  DynamoDB Table    │          │  Event Source    │         │   Lambda    │
│                    │          │  Mapping         │         │   Function  │
│  ┌──────────────┐  │  Poll    │                  │ Invoke  │             │
│  │ DDB Stream   │  │◄────────│  Per shard:      │────────►│             │
│  │              │  │ Records  │  - 4 polls/sec   │         │             │
│  │  Shard 1 ──►│  │────────►│  - Parallel: 1-10│         │             │
│  │  Shard 2 ──►│  │          │  - Bisect on err │         │             │
│  │  Shard 3 ──►│  │          │                  │         │             │
│  └──────────────┘  │          └─────────────────┘         └─────────────┘
└────────────────────┘
```

### 7.2 Configuration Parameters

| Parameter | Default | Range | Notes |
|-----------|---------|-------|-------|
| Batch size | 100 | 1–10,000 | Records per invocation |
| Batching window | 0 seconds | 0–300 seconds | Buffer time (up to 5 min) |
| Parallelization factor | 1 | 1–10 | Concurrent batches per shard |
| Bisect batch on error | false | true/false | Split failed batch |
| Maximum retry attempts | -1 (infinite) | 0–10,000 | |
| Maximum record age | -1 (infinite) | 60–604,800 seconds | |
| Starting position | LATEST or TRIM_HORIZON | — | TRIM_HORIZON recommended |
| On-failure destination | None | SQS or SNS ARN | |
| Tumbling window | None | 0–900 seconds | |

### 7.3 Key Differences from Kinesis

| Aspect | DynamoDB Streams | Kinesis |
|--------|-----------------|---------|
| Polling rate | **4 times/second** per shard | **1 time/second** per shard |
| Throughput per shard | ~40 KB/sec read | 2 MB/sec read (standard) |
| Enhanced fan-out | Not available | Available |
| Simultaneous readers | **2 per shard** (1 for global tables) | 5 per shard (standard) |
| Record content | DynamoDB item change (old/new image) | Arbitrary data blob |
| Shard lifecycle | Auto-managed by DynamoDB | Managed via resharding |
| Starting position | LATEST or TRIM_HORIZON | LATEST, TRIM_HORIZON, or AT_TIMESTAMP |
| Stream retention | 24 hours (fixed) | 24 hours–365 days (configurable) |

### 7.4 Ordering Guarantees

- **Per-item ordering**: Within a shard, changes to the same item (same partition key + sort key) are strictly ordered
- **Cross-item**: Items in the same shard are ordered by modification time, but items in different shards have no ordering guarantee
- **With parallelization factor > 1**: In-order processing maintained at the **item level** (partition key + sort key), not shard level

### 7.5 Simultaneous Reader Limits

| Table Type | Max Lambda Functions per Shard |
|------------|-------------------------------|
| Single-region table | 2 |
| Global table | 1 (to avoid throttling) |

Exceeding these limits causes `ProvisionedThroughputExceededException` on the stream.

---

## 8. Kafka Event Source Mapping — Deep Dive

### 8.1 Supported Sources

| Source | Description |
|--------|-------------|
| Amazon MSK | Managed Kafka clusters in your VPC |
| Self-managed Apache Kafka | Any Kafka cluster (on-premises, other clouds, EC2) |

### 8.2 Architecture

```
┌──────────────────┐          ┌─────────────────┐         ┌─────────────┐
│  Kafka Cluster   │  Poll    │  Event Pollers   │ Invoke  │   Lambda    │
│                  │◄─────────│                  │────────►│   Function  │
│  Topic:          │ Records  │  Consumer Group: │         │             │
│   Partition 0 ──►│────────►│  UUID-based      │         │             │
│   Partition 1 ──►│          │                  │         │             │
│   Partition 2 ──►│          │  Batch:          │         │             │
│                  │          │  - size: 1-10K   │         │             │
│                  │          │  - window: 0-300s│         │             │
│                  │          │  - max 6 MB      │         │             │
└──────────────────┘          └─────────────────┘         └─────────────┘
```

### 8.3 Configuration Parameters

| Parameter | Default | Range | Notes |
|-----------|---------|-------|-------|
| Batch size | 100 | 1–10,000 | Records per invocation |
| Batching window | 500 ms | 0–300 seconds | Cannot revert to 500 ms after changing |
| Starting position | TRIM_HORIZON or LATEST | — | |

### 8.4 Consumer Group Behavior

- Lambda creates a **consumer group** per event source mapping
- Consumer group ID is auto-generated (UUID-based)
- Kafka manages partition assignment within the consumer group
- Lambda commits offsets after successful processing

### 8.5 Provisioned Mode for Kafka

| Metric | Per Poller |
|--------|-----------|
| Throughput | 5 MB/sec |
| Concurrent invocations | 5 |

| Configuration | Default | Range |
|---------------|---------|-------|
| Minimum pollers | 1 | 1–200 |
| Maximum pollers | 200 | 1–2,000 |
| Scaling rate | Up to 1,000 concurrency/min | — |

**Low-latency optimization**: Set `MaximumBatchingWindowInSeconds` to 0 in provisioned mode — Lambda starts processing the next batch immediately after the current invocation completes.

### 8.6 Authentication Methods

| Source | Supported Auth |
|--------|---------------|
| MSK | IAM, SASL/SCRAM, mTLS, unauthenticated |
| Self-managed Kafka | SASL/SCRAM, SASL/PLAIN, mTLS |

### 8.7 VPC Requirements

| Source | VPC Required? | Notes |
|--------|---------------|-------|
| MSK | Yes | Lambda must access cluster's VPC via VPC configuration or VPC Lattice |
| Self-managed Kafka | Only if cluster is in VPC | Public endpoints work without VPC config |

---

## 9. Amazon MQ and DocumentDB Event Source Mappings

### 9.1 Amazon MQ

| Aspect | Details |
|--------|---------|
| Supported brokers | ActiveMQ, RabbitMQ |
| Batch size | 1–10,000 (default varies by broker) |
| Batching window | 500 ms default, 0–300 seconds |
| Auth | Secrets Manager (username/password) |
| VPC | Required (MQ brokers are in VPC) |
| Scaling | Concurrency scales with message volume |

### 9.2 Amazon DocumentDB (with MongoDB compatibility)

| Aspect | Details |
|--------|---------|
| Mechanism | Change streams |
| Batch size | 1–10,000 (default 100) |
| Batching window | 500 ms default, 0–300 seconds |
| Auth | Secrets Manager |
| VPC | Required |
| Starting position | LATEST or TRIM_HORIZON |

---

## 10. Retry Semantics — Unified View

### 10.1 Retry Comparison Table

| Aspect | Synchronous | Asynchronous | Stream ESM (Kinesis/DDB) | Queue ESM (SQS) | Kafka ESM |
|--------|-------------|--------------|--------------------------|-----------------|-----------|
| **Who retries?** | Caller / SDK | Lambda async service | Lambda ESM | SQS (visibility timeout) | Lambda ESM |
| **Default retries** | SDK: 3 attempts | 2 retries (3 total) | Infinite | Until maxReceiveCount | Varies |
| **Max configurable retries** | SDK-dependent | 0–2 | 0–10,000 | Via SQS redrive policy | — |
| **Retry timing** | Immediate / exponential | 1 min, then 2 min | Immediate (blocks shard) | After visibility timeout | Immediate |
| **Throttle handling** | SDK exponential backoff | Exponential backoff (1s → 5 min max) | Retries, doesn't count toward retry limit | Message returns to queue | Retries |
| **On exhaustion** | Error to caller | DLQ / failure destination | On-failure destination | SQS DLQ (redrive policy) | Skip offset |
| **Blocks processing?** | N/A | No | **Yes** (entire shard) | No | No [INFERRED] |
| **Bisect on error?** | N/A | N/A | Configurable | N/A | N/A |

### 10.2 Retry Decision Flowchart

```
Function invocation fails
         │
         ▼
┌─────────────────────┐
│ Invocation type?    │
└────┬────┬────┬──────┘
     │    │    │
  Sync  Async  ESM
     │    │    │
     ▼    │    │
  Return  │    │
  error   │    │
  to      │    │
  caller  │    │
          ▼    │
    Retries    │
    left?      │
    ┌──┴──┐    │
   Yes    No   │
    │     │    │
    ▼     ▼    │
  Retry  DLQ / │
  (1min  Dest  │
   2min)       │
               ▼
        ┌──────────────┐
        │ Stream or    │
        │ Queue?       │
        └──┬───────┬───┘
        Stream   Queue
           │       │
           ▼       │
        Block      ▼
        shard    Message
        & retry  returns
        (bisect  to queue
        if       (visibility
        enabled) timeout)
```

---

## 11. Error Handling Patterns

### 11.1 Pattern: Idempotent Consumer

**Applies to**: All invocation models (all provide at-least-once delivery)

```python
def handler(event, context):
    for record in event['Records']:
        message_id = record['messageId']

        # Check if already processed (DynamoDB conditional write)
        try:
            table.put_item(
                Item={'id': message_id, 'processed_at': now()},
                ConditionExpression='attribute_not_exists(id)'
            )
        except ConditionalCheckFailedException:
            continue  # Already processed — skip

        process(record)
```

### 11.2 Pattern: Partial Batch Response (SQS)

**Applies to**: SQS event source mapping

```python
def handler(event, context):
    failures = []
    for record in event['Records']:
        try:
            process(record)
        except Exception:
            failures.append({'itemIdentifier': record['messageId']})

    return {'batchItemFailures': failures}
```

Requirements:
- Event source mapping must have `FunctionResponseTypes: ["ReportBatchItemFailures"]`
- Return empty `batchItemFailures` list for full success
- Return failed message IDs for partial failure

### 11.3 Pattern: Bisect on Error (Streams)

**Applies to**: Kinesis and DynamoDB Streams

Enable `BisectBatchOnFunctionError` to automatically split failing batches. Combined with `MaximumRetryAttempts`, this isolates poison records:

```
Configuration:
  BisectBatchOnFunctionError: true
  MaximumRetryAttempts: 3
  DestinationConfig:
    OnFailure: arn:aws:sqs:...:failed-records

Behavior:
1. Batch of 100 records fails → split into 2 × 50
2. First 50 succeeds, second 50 fails → split into 2 × 25
3. Continue bisecting until single-record batch is isolated
4. After 3 retries of single record → send to SQS destination
5. Resume processing next records
```

### 11.4 Pattern: DLQ + Alarm for Async

**Applies to**: Asynchronous invocation

```
Lambda Function → (fails 3 times) → DLQ (SQS Queue)
                                          │
                                    CloudWatch Alarm
                                    (ApproximateNumberOfMessagesVisible > 0)
                                          │
                                      SNS → Ops Team
```

### 11.5 Pattern: Destinations vs DLQ

| Feature | Destinations | Dead-Letter Queue |
|---------|-------------|-------------------|
| Trigger conditions | Success AND/OR failure | Failure only |
| Supported targets | SQS, SNS, Lambda, EventBridge, S3 | SQS, SNS only |
| Payload content | Full invocation record (request + response + metadata) | Event payload only (no response details) |
| FIFO support | Standard only (SQS/SNS) | Standard only |
| Configuration scope | Per function + qualifier | Per function |
| Recommended for | New applications | Legacy compatibility |

**The invocation record sent to destinations includes:**
```json
{
    "version": "1.0",
    "timestamp": "2024-01-15T10:30:00.000Z",
    "requestContext": {
        "requestId": "e4b46cbf-b738-xmpl-8880-a18cdf61200e",
        "functionArn": "arn:aws:lambda:us-east-1:123456789012:function:my-func:$LATEST",
        "condition": "RetriesExhausted",
        "approximateInvokeCount": 3
    },
    "requestPayload": { "original": "event" },
    "responseContext": {
        "statusCode": 200,
        "executedVersion": "$LATEST",
        "functionError": "Unhandled"
    },
    "responsePayload": {
        "errorMessage": "Something went wrong",
        "errorType": "RuntimeError"
    }
}
```

**DLQ message attributes (limited):**

| Attribute | Type | Description |
|-----------|------|-------------|
| RequestID | String | Invocation request ID |
| ErrorCode | Number | HTTP status code |
| ErrorMessage | String | First 1 KB of error message |

---

## 12. Destinations and Dead-Letter Queues

### 12.1 Destination Configuration

**Supported destination types for async invocation:**

| Destination | On Success | On Failure | Required Permission |
|-------------|-----------|-----------|-------------------|
| Amazon SQS | Yes (standard only) | Yes (standard only) | `sqs:SendMessage` |
| Amazon SNS | Yes (standard only) | Yes (standard only) | `sns:Publish` |
| Amazon S3 | No | Yes | `s3:PutObject`, `s3:ListBucket` |
| AWS Lambda | Yes | Yes | `lambda:InvokeFunction` |
| Amazon EventBridge | Yes | Yes | `events:PutEvents` |

**Note**: FIFO queues and FIFO topics are NOT supported as destinations.

### 12.2 Event Source Mapping Failure Destinations

For stream-based event source mappings (Kinesis, DynamoDB Streams):

| Destination | Supported | Required Permission |
|-------------|-----------|-------------------|
| Amazon SQS | Yes | `sqs:SendMessage` |
| Amazon SNS | Yes | `sns:Publish` |

These capture metadata about discarded batches (not the full records — records remain in the stream).

### 12.3 DLQ Configuration

```bash
# Configure DLQ for async invocation
aws lambda update-function-configuration \
    --function-name my-function \
    --dead-letter-config TargetArn=arn:aws:sqs:us-east-1:123456789012:my-dlq

# For SQS event source mapping — configure on the SQS queue itself
aws sqs set-queue-attributes \
    --queue-url https://sqs.us-east-1.amazonaws.com/123456789012/my-queue \
    --attributes '{"RedrivePolicy": "{\"deadLetterTargetArn\":\"arn:aws:sqs:us-east-1:123456789012:my-dlq\",\"maxReceiveCount\":\"3\"}"}'
```

**Key distinction:**
- **Async invocation DLQ**: Configured on the Lambda function
- **SQS ESM DLQ**: Configured on the SQS queue (standard SQS redrive policy)
- **Stream ESM failure destination**: Configured on the event source mapping

---

## 13. Payload Contracts and Limits

### 13.1 Invocation Payload Limits

| Invocation Type | Request Payload | Response Payload |
|-----------------|----------------|-----------------|
| Synchronous (RequestResponse) | 6 MB | 6 MB |
| Synchronous (Function URL streaming) | 6 MB | 20 MB (soft limit) |
| Asynchronous (Event) | 256 KB | N/A (202 immediately) |
| Event source mapping batch | 6 MB (hard limit) | N/A (internal) |

### 13.2 Event Source Mapping Batch Limits

| Source | Default Batch Size | Max Batch Size | Payload Limit |
|--------|-------------------|----------------|---------------|
| SQS | 10 | 10,000 | 6 MB |
| Kinesis | 100 | 10,000 | 6 MB |
| DynamoDB Streams | 100 | 10,000 | 6 MB |
| MSK / Kafka | 100 | 10,000 | 6 MB |
| Amazon MQ | Varies by broker | 10,000 | 6 MB |
| DocumentDB | 100 | 10,000 | 6 MB |

### 13.3 Event Payload Formats by Source

**SQS event:**
```json
{
    "Records": [
        {
            "messageId": "059f36b4-87a3-44ab-83d2-661975830a7d",
            "receiptHandle": "AQEBwJnKyrHigUMZj6rYigCg...",
            "body": "Hello from SQS!",
            "attributes": {
                "ApproximateReceiveCount": "1",
                "SentTimestamp": "1545082649636",
                "SenderId": "AROAXXXXXXXXXX:sender",
                "ApproximateFirstReceiveTimestamp": "1545082649636"
            },
            "messageAttributes": {},
            "md5OfBody": "e4e68fb7bd0e697a0ae8f1bb342846b3",
            "eventSource": "aws:sqs",
            "eventSourceARN": "arn:aws:sqs:us-east-1:123456789012:my-queue",
            "awsRegion": "us-east-1"
        }
    ]
}
```

**Kinesis event:**
```json
{
    "Records": [
        {
            "kinesis": {
                "kinesisSchemaVersion": "1.0",
                "partitionKey": "1",
                "sequenceNumber": "49590338271490256608559692538...",
                "data": "SGVsbG8sIHRoaXMgaXMgYSB0ZXN0Lg==",
                "approximateArrivalTimestamp": 1545084650.987
            },
            "eventSource": "aws:kinesis",
            "eventSourceARN": "arn:aws:kinesis:us-east-1:123456789012:stream/my-stream",
            "eventName": "aws:kinesis:record",
            "awsRegion": "us-east-1"
        }
    ]
}
```

**DynamoDB Streams event:**
```json
{
    "Records": [
        {
            "eventID": "1",
            "eventName": "INSERT",
            "eventVersion": "1.0",
            "eventSource": "aws:dynamodb",
            "awsRegion": "us-east-1",
            "dynamodb": {
                "Keys": { "Id": { "N": "101" } },
                "NewImage": { "Id": { "N": "101" }, "Message": { "S": "New item!" } },
                "SequenceNumber": "111",
                "SizeBytes": 26,
                "StreamViewType": "NEW_AND_OLD_IMAGES"
            },
            "eventSourceARN": "arn:aws:dynamodb:us-east-1:123456789012:table/MyTable/stream/..."
        }
    ]
}
```

**Kafka event (MSK / self-managed):**
```json
{
    "eventSource": "aws:kafka",
    "eventSourceArn": "arn:aws:kafka:us-east-1:123456789012:cluster/my-cluster/...",
    "bootstrapServers": "broker1:9092,broker2:9092",
    "records": {
        "my-topic-0": [
            {
                "topic": "my-topic",
                "partition": 0,
                "offset": 15,
                "timestamp": 1545084650987,
                "timestampType": "CREATE_TIME",
                "key": "a2V5",
                "value": "dmFsdWU=",
                "headers": []
            }
        ]
    }
}
```

---

## 14. Throttling Behavior Across Models

### 14.1 Throttling Responses by Invocation Type

| Invocation Type | Throttle Response | What Happens Next |
|-----------------|-------------------|-------------------|
| Synchronous | 429 TooManyRequestsException | SDK auto-retries with backoff |
| Asynchronous | Event queued, retried later | Exponential backoff (1s → 5 min), up to 6 hours |
| ESM (SQS) | Pollers slow down | Messages stay visible in queue |
| ESM (Kinesis/DDB) | Blocks shard iterator | IteratorAge increases |
| ESM (Kafka) | Pollers slow down | Consumer lag increases |

### 14.2 Concurrency and Throttling

```
Total account concurrency (e.g., 1,000)
    │
    ├── Function A: reserved concurrency = 100
    │   └── Can only use 100, throttled beyond that
    │
    ├── Function B: reserved concurrency = 200
    │   └── Can only use 200, throttled beyond that
    │
    └── Unreserved pool: 700
        ├── Function C: uses up to 700 (shared)
        └── Function D: uses up to 700 (shared)

    If C uses 600, D can only use 100 before throttling
```

### 14.3 Burst Concurrency

- **Scaling rate**: 1,000 additional execution environments every 10 seconds
- **Not instantaneous**: Large traffic spikes can cause throttling before scaling catches up
- **Provisioned concurrency**: Pre-initialized environments avoid both cold starts AND throttling

### 14.4 ESM-Specific Throttling Behavior

**SQS**: When Lambda is throttled, messages remain in the queue (visibility timeout expires, message becomes visible again). No data loss.

**Kinesis/DynamoDB Streams**: When throttled, the shard iterator advances more slowly. IteratorAge metric increases. Stream retention prevents data loss (24h DDB, configurable for Kinesis).

**Kafka**: Consumer group pauses consumption. Kafka retains messages per topic retention policy. Consumer lag increases.

---

## 15. Three-Model Comparison Matrix

### 15.1 Complete Feature Comparison

| Feature | Synchronous | Asynchronous | Event Source Mapping |
|---------|-------------|--------------|---------------------|
| **API InvocationType** | `RequestResponse` | `Event` | N/A (auto-managed) |
| **Response to caller** | Function output | 202 Accepted | N/A |
| **Request payload limit** | 6 MB | 256 KB | 6 MB (batch) |
| **Response payload limit** | 6 MB | N/A | N/A |
| **Retry owner** | Caller | Lambda | Lambda |
| **Default retries** | 0 (SDK: 3) | 2 | Varies by source |
| **Max event age** | N/A | 6 hours (configurable) | Source retention |
| **DLQ support** | No | Yes (on function) | Varies by source |
| **Destinations** | No | Yes (success + failure) | Failure only (streams) |
| **Ordering guarantee** | N/A | No | Source-dependent |
| **Batching** | No (1 event) | No (1 event) | Yes (configurable) |
| **Scaling** | Concurrent invocations | Internal queue rate | Source-specific |
| **Cold start impact** | Caller waits | Queue absorbs | Source-specific |
| **Idempotency needed?** | Optional | Yes (duplicates possible) | Yes (at-least-once) |

### 15.2 When to Use Each Model

| Use Case | Recommended Model | Why |
|----------|-------------------|-----|
| REST API backend | Synchronous (API Gateway) | Client needs response |
| Real-time data transformation | Synchronous (Function URL) | Low latency, direct response |
| S3 event processing | Asynchronous | Decoupled, retries built-in |
| Queue consumer | ESM (SQS) | Batching, scaling, error handling |
| Stream processing | ESM (Kinesis/DDB) | Ordered, checkpointed, scalable |
| Event-driven workflow | Asynchronous + Destinations | Chaining with routing |
| ETL pipeline | ESM (Kafka/Kinesis) | High throughput, exactly-once semantics |
| Webhook receiver | Synchronous (Function URL) | HTTPS endpoint, quick response |
| Scheduled job | Async (EventBridge rule) | Fire-and-forget, retry on failure |

---

## 16. Design Decisions and Trade-offs

### 16.1 Why Three Models Instead of One?

| Decision | Rationale |
|----------|-----------|
| Synchronous exists | Many use cases need request-response (APIs, user-facing) |
| Asynchronous exists | Decoupling enables retry without caller waiting; absorbs bursts |
| ESM exists | Streams/queues need polling, batching, and checkpoint management — too complex for callers |

If Lambda only had synchronous invocation:
- S3 events would need the caller to retry on Lambda errors
- Stream processing would need custom polling + checkpoint code
- Queue consumption would lose batching optimization

### 16.2 Why 256 KB Async vs 6 MB Sync?

| Factor | Sync (6 MB) | Async (256 KB) |
|--------|-------------|----------------|
| Storage cost | None (pass-through) | Every event stored in internal queue |
| Throughput impact | One connection at a time | Millions of events in flight |
| Design intent | Full payload for request-response | Lightweight trigger with reference to data |

**Pattern**: For large async payloads, store data in S3 and pass the S3 key as the event (claim-check pattern).

### 16.3 Why ESM Blocks Shard Processing on Error (Streams)

**The ordering guarantee creates a fundamental constraint:**

```
Stream: [Record A] → [Record B] → [Record C]

If Record A fails:
  Option 1: Skip A, process B, C → BREAKS ORDERING
  Option 2: Block and retry A    → PRESERVES ORDERING (chosen)
```

This is the correct trade-off for stream processing because:
- Stream consumers typically depend on ordering (aggregations, state machines)
- Kinesis and DynamoDB Streams retain data, so blocking doesn't lose data
- IteratorAge metric provides visibility into the delay
- Bisect-on-error + max-retry-attempts provide escape hatches

### 16.4 Why SQS ESM Doesn't Block on Error

SQS has different semantics:
- Standard queues have **no ordering guarantee** → skipping a message is safe
- Visibility timeout is the natural retry mechanism → message reappears automatically
- `maxReceiveCount` + DLQ provide the poison-message escape hatch
- FIFO queues DO maintain per-group ordering, and Lambda respects this

### 16.5 Why Destinations Over DLQ?

Destinations were introduced as an evolution of DLQ:

| Evolution | DLQ (Original) | Destinations (Newer) |
|-----------|----------------|---------------------|
| AWS launch | 2016 | 2019 |
| Success routing | No | Yes |
| Payload | Event only | Event + response + metadata |
| Targets | SQS, SNS | SQS, SNS, Lambda, EventBridge, S3 |
| Use case | Error capture | Event-driven orchestration |

AWS recommends destinations for new applications. DLQ remains for backwards compatibility and simplicity.

### 16.6 Provisioned Mode vs Default Scaling (ESM)

| Aspect | Default (Auto-Scaled) | Provisioned Mode |
|--------|----------------------|-----------------|
| Scaling speed | Reactive (slower) | 3x faster |
| Capacity | Auto-determined | You define min/max pollers |
| Cost | Pay per poll | Pay per provisioned poller |
| Best for | Steady, predictable load | Spiky, latency-sensitive |
| Available for | All ESM sources | SQS, MSK, Self-managed Kafka |

---

## 17. Interview Angles

### 17.1 Likely Questions

**Q: "A customer's API Gateway → Lambda integration returns 502. What's happening?"**

The 502 means API Gateway received an invalid response from Lambda. Check:
1. Function returned a response that doesn't match the proxy integration format (missing `statusCode`, `body`)
2. Function timed out (29-second API Gateway limit, not the Lambda timeout)
3. Function returned a payload > 6 MB
4. Lambda returned an unhandled error

Key insight: Lambda returns HTTP 200 even when the function errors. API Gateway maps `FunctionError` in the response to 502.

**Q: "How would you design a system to process S3 events reliably?"**

- S3 → Lambda (async invocation) → process object
- Configure async invoke: max retries = 2, max event age = 1 hour
- On-failure destination → SQS DLQ
- CloudWatch alarm on DLQ depth
- Function must be idempotent (S3 can send duplicate events)
- For large objects: Lambda reads from S3 in the function, event is just the key

**Q: "You're processing Kinesis records and one poison record keeps failing. How do you handle it?"**

1. Enable `BisectBatchOnFunctionError: true`
2. Set `MaximumRetryAttempts: 3`
3. Configure on-failure destination to SQS
4. The poison record will be isolated through binary bisection, retried 3 times, then sent to SQS
5. Remaining records continue processing
6. Monitor IteratorAge to detect when the shard is blocked

**Q: "FIFO SQS → Lambda vs Kinesis → Lambda. When would you choose each?"**

| Factor | FIFO SQS | Kinesis |
|--------|----------|---------|
| Ordering scope | Per MessageGroupId | Per shard (per partition key with parallelization) |
| Throughput | 300 msg/s (3,000 with batching) | 1 MB/sec write, 2 MB/sec read per shard |
| Scaling | Auto (Lambda scales pollers) | Manual resharding (or on-demand) |
| Retention | 4 days (max 14 days) | 24 hours–365 days |
| Replay | No (consumed once) | Yes (rewind iterator) |
| Cost model | Per-request | Per-shard-hour + per-PUT |
| Best for | Low-to-medium throughput, simple ordering | High throughput, replay, multiple consumers |

**Q: "Why does async invocation have a 256 KB payload limit while sync has 6 MB?"**

Every async event is stored in Lambda's internal queue. At scale, millions of events are in flight. The smaller limit keeps queue storage costs manageable and ensures high throughput. For large payloads, use the claim-check pattern: store data in S3, pass the S3 key as the event.

**Q: "How do you handle partial failures in an SQS batch?"**

Return `batchItemFailures` with the message IDs of failed items. Lambda will only return those messages to the queue. Without this, the entire batch is retried, causing successfully processed messages to be reprocessed (requiring idempotency). Enable `ReportBatchItemFailures` in the event source mapping configuration.

### 17.2 Red Flags in Interviews

| Red Flag | Why It's Wrong |
|----------|---------------|
| "Lambda retries synchronous invocations" | Lambda doesn't retry sync — the SDK/caller does |
| "Async invocation returns the function response" | It returns 202 immediately, no function output |
| "Event source mapping is async invocation" | ESM is a separate model — Lambda polls, not the caller |
| "Stream processing skips failed records by default" | Default is infinite retry, blocking the shard |
| "DLQ and destinations are the same thing" | Destinations include success routing and richer payloads |
| "All event sources have the same batch size limits" | Default batch sizes vary (10 for SQS, 100 for Kinesis/DDB/Kafka) |

### 17.3 Numbers to Know

| Metric | Value |
|--------|-------|
| Sync payload limit | 6 MB request + 6 MB response |
| Async payload limit | 256 KB |
| ESM batch payload limit | 6 MB (all sources) |
| Async default retries | 2 (3 total invocations) |
| Async default max event age | 6 hours |
| Async retry timing | 1 min → 2 min |
| Async throttle backoff | 1 sec → 5 min (exponential) |
| Kinesis polling rate | 1/sec/shard (standard) |
| DynamoDB polling rate | 4/sec/shard |
| Parallelization factor | 1–10 (Kinesis/DDB only) |
| SQS default batch size | 10 |
| Kinesis/DDB default batch size | 100 |
| Kafka default batch size | 100 |
| SQS max batch size | 10,000 |
| Batching window range | 0–300 seconds |
| API Gateway Lambda timeout | 29 seconds |
| Lambda max timeout | 900 seconds (15 min) |
| SQS provisioned pollers | 2–2,000 |
| Kafka provisioned pollers | 1–2,000 |
| Stream max retry attempts | 0–10,000 |
| Stream max record age | 60–604,800 seconds (7 days) |
| Tumbling window | 0–900 seconds |

---

*Cross-references:*
- [Execution Environment Lifecycle](execution-environment-lifecycle.md) — Cold starts, Init/Invoke/Shutdown phases, concurrency model
- [Worker Fleet and Placement](worker-fleet-and-placement.md) — How invocations are routed to execution environments
- [VPC Networking](vpc-networking.md) — ENI creation for VPC-connected functions, Kafka/MQ connectivity
- [Firecracker Deep Dive](firecracker-deep-dive.md) — MicroVM isolation for multi-tenant execution
