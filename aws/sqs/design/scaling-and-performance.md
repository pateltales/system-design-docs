# Amazon SQS — Scaling & Performance Deep Dive

> Companion deep dive to the [interview simulation](interview-simulation.md). This document explores how SQS scales to handle virtually unlimited throughput for standard queues, the throughput mechanics of FIFO queues, and the performance optimization strategies that matter in production.

---

## Table of Contents

1. [SQS Scale by the Numbers](#1-sqs-scale-by-the-numbers)
2. [Standard Queue — Internal Partitioning Model](#2-standard-queue--internal-partitioning-model)
3. [FIFO Queue — Throughput Tiers](#3-fifo-queue--throughput-tiers)
4. [FIFO High-Throughput Mode Deep Dive](#4-fifo-high-throughput-mode-deep-dive)
5. [Horizontal Scaling — Producers](#5-horizontal-scaling--producers)
6. [Horizontal Scaling — Consumers](#6-horizontal-scaling--consumers)
7. [Batching — The Single Biggest Performance Lever](#7-batching--the-single-biggest-performance-lever)
8. [Client-Side Buffering](#8-client-side-buffering)
9. [Long Polling vs Short Polling — Performance Impact](#9-long-polling-vs-short-polling--performance-impact)
10. [Connection Management & Latency](#10-connection-management--latency)
11. [Auto-Scaling Consumers Based on Queue Depth](#11-auto-scaling-consumers-based-on-queue-depth)
12. [CloudWatch Metrics for Performance Monitoring](#12-cloudwatch-metrics-for-performance-monitoring)
13. [Lambda as SQS Consumer — Scaling Behavior](#13-lambda-as-sqs-consumer--scaling-behavior)
14. [Performance Anti-Patterns](#14-performance-anti-patterns)
15. [Standard vs FIFO Performance Comparison](#15-standard-vs-fifo-performance-comparison)
16. [Capacity Planning & Cost Optimization](#16-capacity-planning--cost-optimization)
17. [Cross-References](#17-cross-references)

---

## 1. SQS Scale by the Numbers

Amazon SQS is one of the oldest AWS services (launched 2006) and operates at massive scale.
Understanding the headline numbers informs every performance decision.

### Headline Figures

| Metric | Standard Queue | FIFO Queue |
|--------|---------------|------------|
| Throughput (API calls/sec) | **Virtually unlimited** | 300 TPS per API action (default) |
| Throughput with batching | Virtually unlimited | 3,000 messages/sec (10 per batch × 300 TPS) |
| Throughput with high-throughput mode | N/A | Up to 70,000 TPS (US East Virginia) |
| In-flight messages | ~120,000 | 120,000 |
| Message backlog | **Unlimited** | **Unlimited** |
| Max message size | 256 KB | 256 KB |
| Max batch size | 10 messages | 10 messages |
| Max message retention | 14 days | 14 days |
| Max visibility timeout | 12 hours | 12 hours |
| Max delay | 15 minutes | 15 minutes |
| Long poll max wait | 20 seconds | 20 seconds |
| Message groups | N/A | **Unlimited** |

> **Source**: [Amazon SQS quotas](https://docs.aws.amazon.com/AWSSimpleQueueService/latest/SQSDeveloperGuide/sqs-quotas.html)

### What "Virtually Unlimited" Actually Means

When AWS says standard queues support "nearly unlimited" throughput, they mean:

1. **No hard TPS cap** — There is no documented upper limit on API calls per second
2. **Auto-partitioning** — SQS automatically adds internal partitions as throughput increases [INFERRED]
3. **No provisioning** — You don't need to pre-configure capacity
4. **Per-account soft limits** — There may be per-account API rate limits that can be raised via support

This is fundamentally different from FIFO queues, which have explicit per-partition throughput caps.

### Why the Difference?

The unlimited throughput of standard queues comes from relaxed guarantees:

| Guarantee | Standard Queue | FIFO Queue | Performance Impact |
|-----------|---------------|------------|-------------------|
| Ordering | Best-effort | Strict per message group | Ordering requires coordination → limits throughput |
| Delivery | At-least-once | Exactly-once processing | Dedup requires state tracking → limits throughput |
| Partitioning | Free to spread anywhere | Must route by MessageGroupId | Hash-based routing can create hot partitions |

**Key insight**: Every guarantee you add (ordering, dedup) requires coordination between distributed nodes,
and coordination is the enemy of throughput.

---

## 2. Standard Queue — Internal Partitioning Model

> **[INFERRED — not officially documented]**: AWS does not publicly document the internal
> architecture of SQS standard queues. The following is inferred from observed behavior,
> AWS blog posts, and re:Invent talks.

### How Standard Queues Achieve "Unlimited" Throughput

```
                        ┌──────────────────┐
                        │   SQS Front End  │
                        │  (Load Balancer) │
                        └────────┬─────────┘
                                 │
                    ┌────────────┼────────────┐
                    ▼            ▼             ▼
             ┌───────────┐ ┌───────────┐ ┌───────────┐
             │Partition 1│ │Partition 2│ │Partition N│
             │           │ │           │ │           │
             │  msg msg  │ │  msg msg  │ │  msg msg  │
             │  msg msg  │ │  msg msg  │ │  msg msg  │
             └───────────┘ └───────────┘ └───────────┘
                    │            │             │
                    │    Each partition is      │
                    │    replicated across      │
                    │    3 Availability Zones   │
                    ▼            ▼             ▼
              ┌──────────────────────────────────┐
              │     Multi-AZ Replication Layer    │
              └──────────────────────────────────┘
```

### Inferred Partitioning Behavior [INFERRED]

1. **New queue starts with few partitions** — A freshly created queue likely starts with a small number of internal partitions (perhaps 1-3)

2. **Auto-splitting under load** — As throughput increases, SQS automatically splits partitions. This is transparent to the user.

3. **Messages spread across partitions randomly** — Unlike FIFO queues (which use MessageGroupId hashing), standard queues distribute messages across partitions without regard to ordering. This is why ordering is "best-effort."

4. **ReceiveMessage queries a subset** — When a consumer calls `ReceiveMessage` with short polling, SQS queries a weighted random subset of partitions. This is why:
   - Short polling can return "false empties" (messages exist but weren't on the queried partitions)
   - The same message might be returned to multiple consumers (at-least-once)
   - Message order is not preserved

5. **Partitions are the unit of scale** — More partitions = more throughput, but also more "shuffling" of message order.

### Why "Best-Effort Ordering" Is a Feature, Not a Bug

```
Traditional queue (single partition):

    Producer → [msg1, msg2, msg3, msg4, msg5] → Consumer
                    Single bottleneck

SQS standard (multiple partitions):

    Producer ──→ Partition A: [msg1, msg3, msg5]
           └───→ Partition B: [msg2, msg4]

    Consumer reads from Partition B first → gets msg2 before msg1
```

By giving up strict ordering, SQS can:
- **Scale horizontally** — Add partitions without coordination
- **Tolerate partition failures** — Other partitions continue serving
- **Avoid head-of-line blocking** — A slow consumer on one partition doesn't block others

### Observed Behavior That Confirms Partitioning [INFERRED]

| Observation | Explanation |
|-------------|------------|
| Short polling returns empty even when messages exist | Queried subset of partitions that happened to be empty |
| Same message delivered twice | Two partitions independently made the message available |
| Message ordering not preserved | Messages spread across partitions, consumed in different order |
| Throughput increases linearly with more producers | More requests hit more partitions in parallel |
| New queues sometimes throttle briefly, then scale up | SQS is splitting partitions to handle increased load |

---

## 3. FIFO Queue — Throughput Tiers

FIFO queues have explicit throughput limits because ordering and deduplication require coordination.

### Three Throughput Tiers

```
Tier 1: Default FIFO
─────────────────────
  300 API calls/sec per action (Send, Receive, Delete)
  × 10 messages per batch
  = 3,000 messages/sec maximum

Tier 2: High-Throughput Mode (standard regions)
────────────────────────────────────────────────
  Up to 70,000 API calls/sec (US East N. Virginia)
  × 10 messages per batch
  = Up to 700,000 messages/sec (theoretical max with batching)

Tier 3: Regional variation
──────────────────────────
  Limits vary by region (see table below)
```

### High-Throughput FIFO Limits by Region

| Region | API Calls/sec | With Batching (msg/sec) |
|--------|:------------:|:----------------------:|
| US East (N. Virginia) | 70,000 | 700,000 |
| US West (Oregon) | 70,000 | 700,000 |
| Europe (Ireland) | 70,000 | 700,000 |
| US East (Ohio) | 19,000 | 190,000 |
| Europe (Frankfurt) | 19,000 | 190,000 |
| Asia Pacific (Tokyo, Singapore, Sydney, Mumbai) | 9,000 | 90,000 |
| Europe (London) | 4,500 | 45,000 |
| South America (Sao Paulo) | 4,500 | 45,000 |
| Other regions | 2,400 | 24,000 |

> **Source**: [Amazon SQS message quotas](https://docs.aws.amazon.com/AWSSimpleQueueService/latest/SQSDeveloperGuide/quotas-messages.html)

### Why the Regional Variation?

The variation almost certainly reflects the infrastructure capacity in each region [INFERRED]:

- **US East (N. Virginia)** is AWS's largest region with the most capacity
- **Newer/smaller regions** have fewer available hosts, hence lower limits
- Limits can be raised over time as AWS expands regional capacity

### Per-Partition vs Per-Queue Limits

This is a critical distinction:

```
Default FIFO (FifoThroughputLimit = perQueue):

    Entire queue shares 300 TPS limit
    ┌─────────────────────────────┐
    │         FIFO Queue          │
    │   300 TPS total for ALL     │
    │   message groups combined   │
    └─────────────────────────────┘

High-Throughput FIFO (FifoThroughputLimit = perMessageGroupId):

    Each partition gets its own 300 TPS limit
    ┌──────────────────────────────────────────────────┐
    │                  FIFO Queue                       │
    │  ┌──────────┐  ┌──────────┐  ┌──────────┐       │
    │  │Partition 1│  │Partition 2│  │Partition N│      │
    │  │ 300 TPS  │  │ 300 TPS  │  │ 300 TPS  │       │
    │  │GroupA    │  │GroupB    │  │GroupC    │        │
    │  └──────────┘  └──────────┘  └──────────┘       │
    │  Total = 300 × N partitions                      │
    └──────────────────────────────────────────────────┘
```

---

## 4. FIFO High-Throughput Mode Deep Dive

### How to Enable High-Throughput Mode

Two queue attributes must be set together:

```
DeduplicationScope     = "messageGroup"    (default: "queue")
FifoThroughputLimit    = "perMessageGroupId"  (default: "perQueue")
```

### What These Settings Do

#### `DeduplicationScope`

| Value | Behavior |
|-------|----------|
| `queue` (default) | Deduplication checked across the **entire queue**. A MessageDeduplicationId must be unique queue-wide within the 5-minute window. |
| `messageGroup` | Deduplication checked **only within the same MessageGroupId**. Two different message groups can have the same dedup ID without conflict. |

**Why this matters for throughput**: Queue-wide dedup requires checking a global dedup store. Per-message-group dedup only checks within the partition handling that group — much less coordination.

#### `FifoThroughputLimit`

| Value | Behavior |
|-------|----------|
| `perQueue` (default) | The 300 TPS limit applies to the **entire queue**. All message groups share this limit. |
| `perMessageGroupId` | The 300 TPS limit applies **per partition**. Each partition (containing one or more message groups) gets its own 300 TPS. |

### The Partition Model Under High-Throughput Mode

```
                    ┌─────────────────────────┐
                    │     FIFO Queue           │
                    │  High-Throughput Mode    │
                    └───────────┬─────────────┘
                                │
           ┌────────────────────┼────────────────────┐
           ▼                    ▼                     ▼
    ┌──────────────┐   ┌──────────────┐    ┌──────────────┐
    │  Partition 1  │   │  Partition 2  │    │  Partition N  │
    │               │   │               │    │               │
    │  GroupID: A    │   │  GroupID: B    │    │  GroupID: C    │
    │  GroupID: D    │   │  GroupID: E    │    │  GroupID: F    │
    │               │   │               │    │               │
    │  300 TPS each │   │  300 TPS each │    │  300 TPS each │
    │  3K msg/s     │   │  3K msg/s     │    │  3K msg/s     │
    │  (with batch) │   │  (with batch) │    │  (with batch) │
    └──────────────┘   └──────────────┘    └──────────────┘

    Message Group → MD5 hash → Partition assignment
```

### How Message Groups Map to Partitions

SQS uses an **internal hash function** on the MessageGroupId to determine partition placement:

```
hash(MessageGroupId) → partition number
```

This means:
1. Messages with the **same MessageGroupId** always go to the **same partition** → ordering preserved
2. Messages with **different MessageGroupIds** may go to **different partitions** → parallel processing
3. More distinct MessageGroupIds → better distribution across partitions → higher throughput

### The Hot Partition Problem

```
BAD: Few message group IDs → hot partition

    MessageGroupId = "orders"  → All traffic to Partition 1
    Partition 1: 300 TPS (bottleneck!)
    Partition 2: 0 TPS (idle)
    Partition 3: 0 TPS (idle)

GOOD: Many message group IDs → even distribution

    MessageGroupId = "order-12345"  → Partition 1
    MessageGroupId = "order-67890"  → Partition 2
    MessageGroupId = "order-11111"  → Partition 3
    Each partition: ~100 TPS (balanced)
```

### Recommendations for Maximum FIFO Throughput

| Strategy | Impact |
|----------|--------|
| Use many distinct MessageGroupIds | Distributes load across partitions |
| Enable high-throughput mode | Changes limit from per-queue to per-partition |
| Use batch APIs | 10× message throughput per API call |
| Batch messages with same GroupId together | Reduces cross-partition overhead in `SendMessageBatch` |
| Use content-based dedup when possible | Avoids explicit dedup ID management |
| Monitor `ApproximateNumberOfGroupsWithInflightMessages` | Detects hot-group problems |

---

## 5. Horizontal Scaling — Producers

### The Fundamental Throughput Equation

```
Single thread, single connection:

    If round-trip latency = 20 ms
    Then max TPS = 1000 ms / 20 ms = 50 TPS per thread

    To achieve 500 TPS:
        Option A: 10 threads on 1 host
        Option B: 1 thread on 10 hosts
        Option C: 5 threads on 2 hosts
```

> **Source**: [Increasing throughput using horizontal scaling and action batching](https://docs.aws.amazon.com/AWSSimpleQueueService/latest/SQSDeveloperGuide/sqs-throughput-horizontal-scaling-and-batching.html)

### Linear Scaling Property

AWS documents that SQS throughput scales **linearly** with the number of clients:

```
Clients     TPS (single message sends)     TPS (with batching × 10)
──────      ──────────────────────────      ────────────────────────
  1                 50                              500
  2                100                            1,000
  5                250                            2,500
 10                500                            5,000
 20              1,000                           10,000
100              5,000                           50,000
```

### Producer Scaling Architecture

```
┌────────────────────────────────────────────────────────┐
│                    Producer Fleet                        │
│                                                          │
│   ┌──────────┐  ┌──────────┐  ┌──────────┐             │
│   │Producer 1│  │Producer 2│  │Producer N│              │
│   │ Thread 1 │  │ Thread 1 │  │ Thread 1 │             │
│   │ Thread 2 │  │ Thread 2 │  │ Thread 2 │             │
│   │ Thread 3 │  │ Thread 3 │  │ Thread 3 │             │
│   └────┬─────┘  └────┬─────┘  └────┬─────┘             │
│        │              │              │                    │
│   Each thread         │         Each thread              │
│   does ~50 TPS        │         does ~50 TPS             │
│   (at 20ms latency)   │                                  │
│        │              │              │                    │
│   3 threads ×         │         N producers ×            │
│   50 TPS =            │         150 TPS each =           │
│   150 TPS             │         150N TPS total           │
└────────┼──────────────┼──────────────┼───────────────────┘
         │              │              │
         ▼              ▼              ▼
┌─────────────────────────────────────────────────────────┐
│                    SQS Queue                             │
│          (standard: absorbs all of it)                   │
│          (FIFO: limited by partition TPS)                 │
└─────────────────────────────────────────────────────────┘
```

### Key Factors Affecting Producer Throughput

| Factor | Impact | Optimization |
|--------|--------|-------------|
| **Network latency** | Directly limits TPS per thread. 20ms = 50 TPS, 10ms = 100 TPS, 5ms = 200 TPS | Keep producers in the same region as the queue |
| **Thread count** | Linear scaling up to CPU/connection limits | Use thread pools, don't over-subscribe |
| **Message size** | Larger messages = more network time = lower TPS | Compress payloads, use claim-check pattern for large messages |
| **Batch vs single** | 10× throughput with same number of API calls | Always use `SendMessageBatch` in production |
| **Connection pooling** | Reusing HTTP connections avoids TLS handshake overhead | Use SDK connection pool settings |
| **SDK retry config** | Retries on throttle eat into throughput | Use exponential backoff with jitter |

### Same-Region vs Cross-Region Latency Impact

```
Same AZ:        ~1-2 ms  →  500-1000 TPS per thread
Same region:    ~5-20 ms →  50-200 TPS per thread
Cross-region:   ~50-200 ms → 5-20 TPS per thread
Internet:       ~100-500 ms → 2-10 TPS per thread
```

**Takeaway**: Always place producers in the same region as the queue. Cross-region sends
are 10-100× slower per thread.

---

## 6. Horizontal Scaling — Consumers

### The Consumer Throughput Equation

Consumer throughput depends on three phases:

```
Total time per message = Receive time + Process time + Delete time

    Receive: ~20 ms (API call latency)
    Process: Variable (your business logic)
    Delete:  ~20 ms (API call latency)

For 100ms processing time:
    Total = 20 + 100 + 20 = 140 ms per message per thread
    TPS = 1000 / 140 ≈ 7 messages/sec per thread
```

### Why Consumers Are Usually the Bottleneck

```
Producer side:
    SendMessage = network latency only (~20ms)
    50 TPS per thread

Consumer side:
    ReceiveMessage + Process + DeleteMessage = ~140ms (at minimum)
    ~7 messages/sec per thread (with 100ms processing)

    Ratio: Producers can send ~7× faster than consumers can process
    → Queue depth grows → Need more consumers
```

### Consumer Scaling Strategies

#### Strategy 1: More Threads Per Host

```
Host with 4 consumer threads, each processing independently:

    Thread 1: Receive → Process → Delete → Receive → ...
    Thread 2: Receive → Process → Delete → Receive → ...
    Thread 3: Receive → Process → Delete → Receive → ...
    Thread 4: Receive → Process → Delete → Receive → ...

    Total: 4 × 7 msg/sec = 28 msg/sec per host
```

#### Strategy 2: More Hosts

```
3 hosts × 4 threads = 12 parallel consumers

    Total: 12 × 7 msg/sec = 84 msg/sec
```

#### Strategy 3: Batch Receive + Batch Delete

```
Without batching:
    Receive 1 msg  (20ms)
    Process 1 msg  (100ms)
    Delete 1 msg   (20ms)
    Total: 140ms for 1 message

With batching:
    Receive 10 msgs    (20ms)      ← same API call, up to 10 messages
    Process 10 msgs    (1000ms)    ← 10 × 100ms, or parallel
    Delete 10 msgs     (20ms)      ← single DeleteMessageBatch call
    Total: 1040ms for 10 messages = 104ms per message

    If processing in parallel across threads:
    Receive 10 msgs    (20ms)
    Process 10 msgs    (100ms)     ← all 10 in parallel
    Delete 10 msgs     (20ms)
    Total: 140ms for 10 messages = 14ms per message = ~71 msg/sec per thread
```

### Consumer Scaling for FIFO Queues

FIFO queues have a critical constraint: **messages within the same MessageGroupId
are processed in order**. This limits parallelism:

```
Standard Queue Consumer Scaling:
    Any consumer can process any message → full parallelism

FIFO Queue Consumer Scaling:
    Messages in GroupA must be processed in order
    Messages in GroupB must be processed in order
    BUT GroupA and GroupB can be processed in parallel

    Max parallelism = number of distinct active MessageGroupIds
```

```
Example:

    3 MessageGroupIds: A, B, C
    3 consumers: Consumer1, Consumer2, Consumer3

    Consumer1 ← all messages from GroupA (in order)
    Consumer2 ← all messages from GroupB (in order)
    Consumer3 ← all messages from GroupC (in order)

    If you add Consumer4, it sits idle — only 3 groups to process.
    If GroupA has 90% of messages, Consumer1 is the bottleneck.
```

### FIFO Consumer Scaling Recommendations

| Problem | Solution |
|---------|----------|
| Too few message groups | Redesign partition key to have more groups (e.g., per-customer instead of per-region) |
| Hot message group | Split into sub-groups if ordering between sub-groups isn't needed |
| Consumer count > group count | Extra consumers are idle; match consumer count to active group count |
| Slow processing blocks group | Use heartbeat pattern: extend visibility timeout while processing |

---

## 7. Batching — The Single Biggest Performance Lever

Batching is the most impactful optimization for SQS performance. It affects
throughput, latency, and cost simultaneously.

### Batch Operations Available

| Operation | Batch Variant | Max per Batch | Notes |
|-----------|--------------|:------------:|-------|
| SendMessage | **SendMessageBatch** | 10 | Total batch payload ≤ 256 KB |
| DeleteMessage | **DeleteMessageBatch** | 10 | Uses receipt handles from ReceiveMessage |
| ChangeMessageVisibility | **ChangeMessageVisibilityBatch** | 10 | Extend/reduce timeout for multiple messages |
| ReceiveMessage | *(built-in)* | 10 | Set `MaxNumberOfMessages=10`; no separate batch API needed |

### Throughput Impact of Batching

```
Without batching (single sends):

    API calls/sec: 50 (limited by 20ms latency)
    Messages/sec:  50

With batching (10 per batch):

    API calls/sec: 50 (same latency limit)
    Messages/sec:  500 (10× improvement)

    Same number of API calls, 10× the throughput
```

### Cost Impact of Batching

SQS charges per API request (not per message):

```
Without batching:
    1,000,000 messages = 1,000,000 API calls
    Cost: 1,000,000 × $0.40/million = $0.40

With batching (10 per batch):
    1,000,000 messages = 100,000 API calls
    Cost: 100,000 × $0.40/million = $0.04

    90% cost reduction
```

> **Note**: Each 64 KB chunk of a message payload is billed as one request. A 256 KB message
> counts as 4 requests. Batch pricing applies to the API call, not the data volume.

### Batching Best Practices

#### Producer-Side Batching

```java
// Good: Batch 10 messages per API call
SendMessageBatchRequest batchRequest = SendMessageBatchRequest.builder()
    .queueUrl(queueUrl)
    .entries(
        SendMessageBatchRequestEntry.builder()
            .id("msg1").messageBody("body1").build(),
        SendMessageBatchRequestEntry.builder()
            .id("msg2").messageBody("body2").build(),
        // ... up to 10 entries
    )
    .build();

SendMessageBatchResponse response = sqsClient.sendMessageBatch(batchRequest);

// IMPORTANT: Check for partial failures
for (BatchResultErrorEntry error : response.failed()) {
    // Retry individual failed messages
    log.error("Failed: {} - {}", error.id(), error.message());
}
```

#### Consumer-Side Batching

```java
// Step 1: Receive up to 10 messages
ReceiveMessageRequest receiveRequest = ReceiveMessageRequest.builder()
    .queueUrl(queueUrl)
    .maxNumberOfMessages(10)  // Request up to 10
    .waitTimeSeconds(20)       // Long polling
    .build();

List<Message> messages = sqsClient.receiveMessage(receiveRequest).messages();

// Step 2: Process all messages
for (Message msg : messages) {
    processMessage(msg);
}

// Step 3: Batch delete all successfully processed messages
List<DeleteMessageBatchRequestEntry> deleteEntries = messages.stream()
    .map(msg -> DeleteMessageBatchRequestEntry.builder()
        .id(msg.messageId())
        .receiptHandle(msg.receiptHandle())
        .build())
    .collect(Collectors.toList());

sqsClient.deleteMessageBatch(DeleteMessageBatchRequest.builder()
    .queueUrl(queueUrl)
    .entries(deleteEntries)
    .build());
```

### Partial Batch Failures

Batch operations can have **partial failures** — some messages in the batch succeed while others fail:

```
SendMessageBatch with 10 messages:
    Successful: [msg1, msg2, msg3, msg5, msg6, msg7, msg8, msg9, msg10]
    Failed:     [msg4 — InternalError]

DeleteMessageBatch with 10 messages:
    Successful: [msg1, msg2, msg3, msg4, msg5, msg6, msg8, msg9]
    Failed:     [msg7 — ReceiptHandleIsInvalid, msg10 — ReceiptHandleIsInvalid]
```

**You MUST check the `Failed` list** in every batch response. If you ignore it:
- Messages that failed to send are silently lost
- Messages that failed to delete will become visible again (redelivery)

### FIFO Batching Subtlety

For FIFO queues with `SendMessageBatch`:

```
Batch with messages for DIFFERENT message groups:

    Entry 1: MessageGroupId = "A", body = "msg1"
    Entry 2: MessageGroupId = "B", body = "msg2"
    Entry 3: MessageGroupId = "A", body = "msg3"

    SQS processes entries in order within the batch.
    For GroupA: msg1 is sequenced before msg3 ✓
    GroupB is independent of GroupA ✓
```

**AWS recommends**: When possible, batch messages with the **same MessageGroupId** together.
This reduces cross-partition overhead and improves batching efficiency.

---

## 8. Client-Side Buffering

### AmazonSQSBufferedAsyncClient (Java SDK v1)

The AWS SDK provides a client-side buffering layer that automatically batches requests:

```
Without buffering:

    sendMessage("msg1") → API call (20ms)
    sendMessage("msg2") → API call (20ms)
    sendMessage("msg3") → API call (20ms)
    Total: 3 API calls, 60ms

With buffering:

    sendMessage("msg1") → buffered
    sendMessage("msg2") → buffered     ← waits up to 200ms
    sendMessage("msg3") → buffered
    [200ms timer fires] → SendMessageBatch(msg1, msg2, msg3) → 1 API call (20ms)
    Total: 1 API call, ~220ms
```

> **Source**: [Client-side buffering and request batching](https://docs.aws.amazon.com/AWSSimpleQueueService/latest/SQSDeveloperGuide/sqs-client-side-buffering-request-batching.html)

### Configuration Parameters

| Parameter | Default | Description |
|-----------|---------|-------------|
| `maxBatchOpenMs` | 200 ms | Max time to wait before sending a batch. Higher = better batching, worse latency |
| `maxBatchSize` | 10 | Max messages per batch (SQS limit is 10) |
| `maxBatchSizeBytes` | 1 MiB | Max total batch size in bytes (SQS limit is 256 KB per message) |
| `longPoll` | true | Use long polling for receives |
| `longPollWaitTimeoutSeconds` | 20 sec | Long poll wait time |
| `maxDoneReceiveBatches` | 10 | Prefetched receive batches stored client-side |
| `maxInflightOutboundBatches` | 5 | Max concurrent outbound batches being sent |
| `maxInflightReceiveBatches` | 10 | Max concurrent receive batches being processed |

### The Latency-Throughput Tradeoff

```
maxBatchOpenMs = 0:
    No buffering. Every sendMessage is an immediate API call.
    Latency: ~20ms (just network)
    Throughput: 50 TPS per thread
    Cost: 1 API call per message

maxBatchOpenMs = 200 (default):
    Buffers up to 200ms to collect messages.
    Latency: up to 220ms (200ms buffer + 20ms network)
    Throughput: up to 500 msg/sec per thread (10 per batch)
    Cost: up to 90% reduction

maxBatchOpenMs = 1000:
    Very aggressive buffering.
    Latency: up to 1020ms
    Throughput: nearly guaranteed full batches
    Cost: maximum reduction
```

### Java SDK v2 Alternative: SqsAsyncBatchManager

```java
// Java SDK v2
SqsAsyncClient sqsAsync = SqsAsyncClient.builder()
    .region(Region.US_EAST_1)
    .build();

SqsAsyncBatchManager batchManager = sqsAsync.batchManager();

// Sends are automatically batched
batchManager.sendMessage(SendMessageRequest.builder()
    .queueUrl(queueUrl)
    .messageBody("your message")
    .build());
```

### Limitations

- **Does NOT support FIFO queues** — FIFO ordering constraints conflict with client-side batching
- **Prefetch can waste visibility timeout** — If prefetched messages sit in the buffer too long,
  their visibility timeout may expire and they'll be redelivered to another consumer
- **Not all SDKs have it** — Primarily available in Java SDK; other SDKs require manual batching

---

## 9. Long Polling vs Short Polling — Performance Impact

### How Short Polling Works

```
Consumer → ReceiveMessage(WaitTimeSeconds=0) → SQS

    SQS queries a SUBSET of internal partitions (weighted random)
    Returns immediately, even if no messages found

    Result possibilities:
    1. Messages found on queried partitions → return them
    2. Messages exist but on OTHER partitions → empty response (FALSE EMPTY)
    3. No messages anywhere → empty response (TRUE EMPTY)
```

> **Source**: [Amazon SQS short and long polling](https://docs.aws.amazon.com/AWSSimpleQueueService/latest/SQSDeveloperGuide/sqs-short-and-long-polling.html)

### How Long Polling Works

```
Consumer → ReceiveMessage(WaitTimeSeconds=20) → SQS

    SQS queries ALL internal partitions
    If messages found → return immediately
    If no messages → wait up to 20 seconds for a message to arrive
    If message arrives during wait → return it immediately
    If timeout expires → return empty response

    Result possibilities:
    1. Messages found → return them (same as short polling, but checked ALL partitions)
    2. No messages now, but arrives in 5 seconds → return after 5 seconds
    3. No messages for 20 seconds → return empty after 20 seconds
```

### Performance Comparison

| Aspect | Short Polling | Long Polling |
|--------|:------------:|:----------:|
| **Partitions queried** | Subset | ALL |
| **False empties** | Yes (common) | No |
| **Empty responses** | Many | Few |
| **API calls/min (idle queue)** | 3,000 (1 per 20ms loop) | 3 (1 per 20sec wait) |
| **Cost (idle queue)** | High | ~1,000× lower |
| **Latency to first message** | ~20ms | ~20ms (if messages exist) |
| **Latency when queue empty** | ~20ms (empty response) | Up to 20 sec |
| **CPU usage** | High (busy loop) | Low (blocking wait) |

### When to Use Each

| Use Long Polling When | Use Short Polling When |
|----------------------|----------------------|
| Cost matters (almost always) | You need sub-second empty-response latency |
| You want all available messages | You're doing lightweight health-check polling |
| Queue has variable load | You have a specific short-poll architecture requirement |
| Default recommendation for most workloads | Rare in practice |

### The Long Polling Cost Savings Math

```
Scenario: Queue receives 100 messages/minute with variable gaps

Short polling (250ms loop):
    API calls/min: 240 (4 per second × 60 seconds)
    Messages received: 100
    Empty responses: 140
    Cost per day: 240 × 60 × 24 = 345,600 API calls = $0.14/day

Long polling (20s wait):
    API calls/min: ~103 (100 with messages + ~3 empty waits)
    Messages received: 100
    Empty responses: ~3
    Cost per day: 103 × 60 × 24 = 148,320 API calls = $0.06/day

    At higher idle ratios, the savings are even more dramatic.
```

### Setting Long Polling

```
Option 1: Per-queue default (affects all consumers)
    SetQueueAttributes:
        ReceiveMessageWaitTimeSeconds = 20

Option 2: Per-request override
    ReceiveMessage:
        WaitTimeSeconds = 20

Note: If both are set, the per-request value takes precedence.
      Setting WaitTimeSeconds = 0 on the request overrides queue default → short polling.
```

---

## 10. Connection Management & Latency

### HTTP Connection Overhead

Every SQS API call uses HTTPS. Connection establishment has overhead:

```
New connection:
    DNS resolution:    ~5ms
    TCP handshake:     ~5ms (1 RTT)
    TLS handshake:     ~10-20ms (2 RTTs)
    HTTP request:      ~5ms
    ─────────────────────────
    Total first request: ~25-35ms

Reused connection (keep-alive):
    HTTP request:      ~5ms
    ─────────────────────────
    Total: ~5ms

    Connection reuse is 5-7× faster
```

### Connection Pooling Best Practices

```java
// AWS SDK v2 — configure HTTP client with connection pool
SqsClient sqsClient = SqsClient.builder()
    .httpClient(ApacheHttpClient.builder()
        .maxConnections(50)                 // Pool size
        .connectionTimeout(Duration.ofSeconds(5))
        .socketTimeout(Duration.ofSeconds(30))
        .build())
    .region(Region.US_EAST_1)
    .build();

// Reuse this client across threads — it's thread-safe
// Do NOT create a new client per request
```

### Connection Pool Sizing

```
Rule of thumb:
    Pool size ≥ number of concurrent threads making SQS calls

Example:
    10 producer threads + 20 consumer threads = 30 concurrent SQS callers
    Set maxConnections ≥ 30 (add headroom → 50)

Too small:
    Threads block waiting for a connection → throughput drops
    You'll see connection timeout errors

Too large:
    Wastes memory and file descriptors
    Each idle connection consumes a socket
```

### Keep-Alive and Idle Timeout

```
SQS uses HTTP/1.1 keep-alive by default.

    Connection idle timeout (server-side): not documented [INFERRED ~60s]
    Recommendation: Set client idle timeout < server timeout
    to avoid "connection reset" errors on stale connections.
```

---

## 11. Auto-Scaling Consumers Based on Queue Depth

### The Core Auto-Scaling Formula

```
                     ApproximateNumberOfMessagesVisible
Backlog per host = ─────────────────────────────────────
                           number of consumer hosts

Target: Keep backlog per host at an acceptable level
(e.g., each host should have ≤ 100 messages to process)
```

> **Source**: [Scaling based on Amazon SQS](https://docs.aws.amazon.com/autoscaling/ec2/userguide/scale-sqs-queue-cli.html)

### Auto-Scaling Architecture

```
                ┌────────────────────┐
                │   CloudWatch Alarm │
                │                    │
                │  IF backlog/host   │
                │  > threshold       │
                │  THEN scale up     │
                └─────────┬──────────┘
                          │
                          ▼
                ┌────────────────────┐
                │  EC2 Auto Scaling  │
                │      Group         │
                │                    │
                │  Min: 2            │
                │  Max: 20           │
                │  Desired: ?        │
                └─────────┬──────────┘
                          │
           ┌──────────────┼──────────────┐
           ▼              ▼              ▼
    ┌──────────┐   ┌──────────┐   ┌──────────┐
    │Consumer 1│   │Consumer 2│   │Consumer N│
    └────┬─────┘   └────┬─────┘   └────┬─────┘
         │              │              │
         └──────────────┼──────────────┘
                        ▼
              ┌──────────────────┐
              │    SQS Queue     │
              │                  │
              │  Visible: 5,000  │
              │  In-flight: 200  │
              └──────────────────┘
```

### Custom Metric for Target Tracking

AWS recommends a custom CloudWatch metric:

```
Custom metric: BacklogPerInstance

    Value = ApproximateNumberOfMessagesVisible / RunningCapacity

    Target tracking policy:
        TargetValue = 100 (acceptable backlog per instance)

    Behavior:
        If BacklogPerInstance > 100 → Scale OUT (add instances)
        If BacklogPerInstance < 100 → Scale IN (remove instances)
```

### Why Not Just Use ApproximateNumberOfMessagesVisible?

```
Problem with raw queue depth:

    Queue depth = 10,000 messages
    If you have 100 consumers → 100 msgs each (fine)
    If you have 2 consumers → 5,000 msgs each (not fine)

    Raw queue depth doesn't account for existing capacity.

Solution — backlog per instance:

    10,000 messages / 100 consumers = 100 per instance ✓
    10,000 messages / 2 consumers = 5,000 per instance → SCALE UP
```

### ApproximateAgeOfOldestMessage as Scaling Signal

An alternative/complementary signal:

```
ApproximateAgeOfOldestMessage > threshold → Scale up

    Advantage: Directly measures consumer lag
    Disadvantage: Can spike during burst even if catching up

    Recommended: Use BOTH queue depth AND message age
```

### Scale-In Protection

When scaling in, ensure consumers finish processing before termination:

```
1. Consumer receives Spot/ASG termination signal
2. Consumer stops calling ReceiveMessage (no new work)
3. Consumer finishes processing in-flight messages
4. Consumer explicitly deletes processed messages
5. Consumer signals ready for termination

Without this: In-flight messages become visible again after
visibility timeout → reprocessed by other consumers (wasteful but safe)
```

---

## 12. CloudWatch Metrics for Performance Monitoring

### Available Metrics

All metrics are in the `AWS/SQS` namespace with dimension `QueueName`.

#### Standard Metrics (All Queues)

| Metric | Description | Key Use |
|--------|-------------|---------|
| `ApproximateNumberOfMessagesVisible` | Messages available for retrieval | Queue depth monitoring, auto-scaling |
| `ApproximateNumberOfMessagesNotVisible` | In-flight messages (received but not deleted) | Consumer processing backlog |
| `ApproximateNumberOfMessagesDelayed` | Messages in delay period | Delay queue monitoring |
| `ApproximateAgeOfOldestMessage` | Age of oldest message (seconds) | Consumer lag detection |
| `NumberOfMessagesSent` | Messages sent per period | Producer throughput monitoring |
| `NumberOfMessagesReceived` | Messages received per period | Consumer throughput monitoring |
| `NumberOfMessagesDeleted` | Messages deleted per period | Successful processing rate |
| `NumberOfEmptyReceives` | ReceiveMessage calls returning nothing | Polling efficiency (switch to long polling if high) |
| `SentMessageSize` | Size of sent messages (bytes) | Payload size monitoring |

> **Source**: [Available CloudWatch metrics for Amazon SQS](https://docs.aws.amazon.com/AWSSimpleQueueService/latest/SQSDeveloperGuide/sqs-available-cloudwatch-metrics.html)

#### FIFO-Only Metrics

| Metric | Description | Key Use |
|--------|-------------|---------|
| `ApproximateNumberOfGroupsWithInflightMessages` | Message groups with in-flight messages | Parallelism measurement — if this is low, you have hot groups |
| `NumberOfDeduplicatedSentMessages` | Messages rejected as duplicates | Dedup effectiveness monitoring |

#### Fair Queue Metrics (Standard Queues with Fair Queuing)

| Metric | Description |
|--------|-------------|
| `ApproximateNumberOfNoisyGroups` | Message groups considered "noisy" |
| `ApproximateNumberOfMessagesVisibleInQuietGroups` | Visible messages excluding noisy groups |
| `ApproximateNumberOfMessagesNotVisibleInQuietGroups` | In-flight messages excluding noisy groups |
| `ApproximateNumberOfMessagesDelayedInQuietGroups` | Delayed messages excluding noisy groups |
| `ApproximateAgeOfOldestMessageInQuietGroups` | Oldest message age excluding noisy groups |

### Key Alarms to Set

| Alarm | Condition | Action |
|-------|-----------|--------|
| **Queue depth too high** | `ApproximateNumberOfMessagesVisible` > 10,000 for 5 min | Scale out consumers or investigate consumer failures |
| **Consumer lag too high** | `ApproximateAgeOfOldestMessage` > 300 sec | Scale out consumers, check processing errors |
| **DLQ receiving messages** | `NumberOfMessagesSent` > 0 on DLQ | Investigate consumer failures, check maxReceiveCount |
| **Too many empty receives** | `NumberOfEmptyReceives` > 1000/min | Switch to long polling |
| **In-flight limit approaching** | `ApproximateNumberOfMessagesNotVisible` > 100,000 | Speed up processing, increase consumer count, check for stuck consumers |
| **FIFO hot group** | `ApproximateNumberOfGroupsWithInflightMessages` = 1 | Redesign message group strategy |

### Monitoring Dashboard Design

```
┌──────────────────────────────────────────────────────────┐
│                    SQS Dashboard                          │
├──────────────────────┬───────────────────────────────────┤
│                      │                                    │
│  Queue Depth         │  Consumer Lag                      │
│  ┌─────────────────┐ │  ┌─────────────────┐              │
│  │ ▃▃▅▇▇█████▇▅▃▃ │ │  │ ▁▁▂▃▅▇███▇▅▃▁▁ │              │
│  │ Visible msgs    │ │  │ Oldest msg age   │              │
│  └─────────────────┘ │  └─────────────────┘              │
│                      │                                    │
├──────────────────────┼───────────────────────────────────┤
│                      │                                    │
│  Throughput          │  Error Rate                        │
│  ┌─────────────────┐ │  ┌─────────────────┐              │
│  │ ▂▃▃▄▅▅▅▄▃▃▂▂▂▂ │ │  │ ▁▁▁▁▁▂▁▁▁▁▁▁▁▁ │              │
│  │ Sent / Received │ │  │ Empty receives   │              │
│  └─────────────────┘ │  └─────────────────┘              │
│                      │                                    │
├──────────────────────┼───────────────────────────────────┤
│                      │                                    │
│  DLQ Monitor         │  In-Flight Messages                │
│  ┌─────────────────┐ │  ┌─────────────────┐              │
│  │ ▁▁▁▁▁▁▃▁▁▁▁▁▁▁ │ │  │ ▂▃▃▃▃▃▃▃▃▃▃▃▂▂ │              │
│  │ DLQ depth       │ │  │ Not visible      │              │
│  └─────────────────┘ │  └─────────────────┘              │
│                      │                                    │
└──────────────────────┴───────────────────────────────────┘
```

### Important: Metrics Are Approximate

All SQS metrics prefixed with "Approximate" are **eventually consistent**:

- `ApproximateNumberOfMessagesVisible` may lag by a few seconds
- Due to SQS's distributed architecture, the count is aggregated across partitions
- For auto-scaling decisions, this is accurate enough
- For exact counts, there is no reliable way — this is by design

---

## 13. Lambda as SQS Consumer — Scaling Behavior

### How Lambda Polls SQS

Lambda uses an **event source mapping** to poll SQS on your behalf:

```
┌────────────────────────┐
│    Lambda Service       │
│                         │
│  ┌──────────────────┐  │
│  │ Event Source      │  │
│  │ Mapping (Poller)  │  │
│  │                   │  │
│  │ Batch size: 10    │  │
│  │ Batch window: 5s  │  │
│  │ Concurrency: auto │  │
│  └────────┬──────────┘  │
│           │              │
│     Long-polls SQS      │
│     on your behalf       │
└───────────┼──────────────┘
            │
            ▼
┌────────────────────────┐
│       SQS Queue         │
└────────────────────────┘
```

### Standard Queue Scaling

```
Phase 1 (initial):
    Lambda starts with 5 concurrent batches of long-polling

Phase 2 (scaling up):
    If messages are still available, Lambda adds up to
    60 more instances per minute

Phase 3 (steady state):
    Scales up to 1,000 concurrent Lambda invocations (default)
    Can be increased to tens of thousands via reserved concurrency

Phase 4 (scaling down):
    When queue empties, Lambda reduces polling frequency
    Scales down automatically
```

> **Source**: [Configuring scaling behavior for SQS event source mappings](https://docs.aws.amazon.com/lambda/latest/dg/services-sqs-scaling.html)

### FIFO Queue Scaling with Lambda

```
FIFO scaling is different:

    Number of concurrent Lambda invocations =
        number of active message groups

    If you have 10 active message groups:
        Lambda runs up to 10 concurrent invocations
        Each invocation processes messages from one group in order

    If you have 1 message group:
        Lambda runs 1 invocation at a time
        No parallelism possible
```

### Lambda Batch Configuration

| Parameter | Default | Range | Description |
|-----------|---------|-------|-------------|
| `BatchSize` | 10 | 1–10,000 (standard), 1–10 (FIFO) | Messages per Lambda invocation |
| `MaximumBatchingWindowSeconds` | 0 | 0–300 | Wait time to accumulate batch |
| `MaximumConcurrency` | 1,000 | 2–1,000 | Max concurrent Lambda invocations |
| `FunctionResponseTypes` | — | `ReportBatchItemFailures` | Return partial failures |

### Partial Batch Failure Handling

Without `ReportBatchItemFailures`:
```
Lambda receives batch of 10 messages.
Messages 1-7 succeed, message 8 fails.
Result: ENTIRE batch is returned to the queue.
Messages 1-7 are reprocessed (wasteful).
```

With `ReportBatchItemFailures`:
```
Lambda receives batch of 10 messages.
Messages 1-7 succeed, message 8 fails.
Lambda returns: { "batchItemFailures": [{"itemIdentifier": "msg8-id"}] }
Result: Only message 8 is returned to the queue.
Messages 1-7 are deleted. Messages 9-10 are deleted.
```

---

## 14. Performance Anti-Patterns

### Anti-Pattern 1: Single-Threaded Consumer Loop

```
BAD:
    while (true) {
        msg = receiveMessage(maxMessages=1)  // Only 1 message!
        process(msg)
        deleteMessage(msg)
    }
    Result: ~7 msg/sec with 100ms processing

GOOD:
    while (true) {
        msgs = receiveMessage(maxMessages=10, waitTimeSeconds=20)
        for (msg : msgs) { process(msg) }
        deleteMessageBatch(msgs)
    }
    Result: ~70 msg/sec with 100ms processing (10× improvement)

BETTER:
    while (true) {
        msgs = receiveMessage(maxMessages=10, waitTimeSeconds=20)
        parallelProcess(msgs)  // Process all 10 in parallel
        deleteMessageBatch(msgs)
    }
    Result: ~300+ msg/sec with thread pool
```

### Anti-Pattern 2: Short Polling on Idle Queue

```
BAD:
    while (true) {
        msg = receiveMessage(waitTimeSeconds=0)  // Short poll
        if (msg == null) {
            Thread.sleep(100)  // Busy wait
        }
    }
    Result: ~600 empty API calls/minute on idle queue

GOOD:
    while (true) {
        msg = receiveMessage(waitTimeSeconds=20)  // Long poll
        if (msg != null) { process(msg) }
    }
    Result: ~3 API calls/minute on idle queue (200× fewer)
```

### Anti-Pattern 3: Creating New SQS Client Per Request

```
BAD:
    void handleRequest() {
        SqsClient client = SqsClient.create()  // New client every time!
        client.sendMessage(...)
        client.close()
    }
    Result: TLS handshake on every call → 30ms+ overhead per request

GOOD:
    // Create once at startup, reuse across all requests
    private static final SqsClient client = SqsClient.builder()
        .httpClient(ApacheHttpClient.builder()
            .maxConnections(50)
            .build())
        .build();

    void handleRequest() {
        client.sendMessage(...)  // Reuses pooled connection
    }
    Result: ~5ms per request (connection reuse)
```

### Anti-Pattern 4: Single MessageGroupId for FIFO

```
BAD:
    All messages use MessageGroupId = "default"
    Result: Everything goes to one partition → max 300 TPS
    Adding consumers beyond 1 provides no benefit

GOOD:
    Messages use MessageGroupId = customerId (or orderId, etc.)
    Result: Distributes across partitions → up to 70K TPS
    Each consumer processes a different group in parallel
```

### Anti-Pattern 5: Not Extending Visibility Timeout for Long Processing

```
BAD:
    msg = receiveMessage()          // visibility timeout = 30s
    result = longRunningJob(msg)     // Takes 5 minutes!
    deleteMessage(msg)               // ReceiptHandle expired!

    Result: Message redelivered after 30s while still being processed
    → Duplicate processing, wasted work

GOOD:
    msg = receiveMessage()           // visibility timeout = 30s
    // Start heartbeat thread
    heartbeat = scheduleEvery(20s, () -> {
        changeMessageVisibility(msg, 30s)  // Extend by 30s
    })
    result = longRunningJob(msg)
    heartbeat.cancel()
    deleteMessage(msg)

    Result: Visibility extended as needed, no redelivery
```

### Anti-Pattern 6: Ignoring Partial Batch Failures

```
BAD:
    response = sendMessageBatch(messages)
    // Assume all succeeded ← WRONG

    Result: Some messages silently lost

GOOD:
    response = sendMessageBatch(messages)
    if (!response.failed().isEmpty()) {
        for (error : response.failed()) {
            retryQueue.add(messages.get(error.id()))
        }
    }

    Result: Failed messages are retried
```

### Anti-Pattern 7: Using SQS as a Database

```
BAD:
    // Using SQS to "peek" at messages without processing
    while (true) {
        msgs = receiveMessage(visibilityTimeout=0)  // Peek
        if (containsTarget(msgs)) {
            process(target)
            deleteMessage(target)
        }
    }

    Problems:
    - Receives random subset of messages
    - No way to query by attribute
    - Wasteful API calls
    - Messages may be delivered to other consumers

SQS is a queue, not a database. If you need query/search, use DynamoDB or a database.
```

---

## 15. Standard vs FIFO Performance Comparison

### Decision Framework

```
                    Need ordering?
                    ┌────┴────┐
                   Yes        No
                    │          │
            Need dedup?    ┌───┴───┐
            ┌───┴───┐     Standard
           Yes      No    Queue ✓
            │        │     (unlimited
        FIFO Queue   │      throughput)
        ✓            │
                Can you handle
                dedup at consumer?
                ┌───┴───┐
               Yes      No
                │        │
            Standard   FIFO Queue
            Queue +    ✓
            idempotent
            consumer ✓
```

### Side-by-Side Performance Table

| Dimension | Standard Queue | FIFO Queue | FIFO High-Throughput |
|-----------|:-------------:|:----------:|:-------------------:|
| **Max throughput (API calls/sec)** | Unlimited | 300 | 70,000 (us-east-1) |
| **Max throughput (msgs/sec with batching)** | Unlimited | 3,000 | 700,000 (us-east-1) |
| **Consumer parallelism** | Unlimited | Limited by message group count | Limited by message group count |
| **In-flight limit** | ~120,000 | 120,000 | 120,000 |
| **Auto-partitioning** | Yes (transparent) | Yes (by MessageGroupId hash) | Yes (by MessageGroupId hash) |
| **Hot partition risk** | None (random distribution) | High (if few group IDs) | High (if few group IDs) |
| **Duplicate messages** | Possible | Prevented (5-min window) | Prevented (5-min window) |
| **Message ordering** | Best-effort | Strict per group | Strict per group |
| **Client-side buffering support** | Yes | No | No |
| **Lambda max batch size** | 10,000 | 10 | 10 |
| **Cost per million requests** | $0.40 | $0.50 | $0.50 |

### When Standard Queue Wins

1. **High-throughput event streaming** — No TPS ceiling to worry about
2. **Fan-out to many consumers** — No message group bottleneck
3. **Cost-sensitive workloads** — $0.40 vs $0.50 per million, plus client-side buffering support
4. **Consumer can handle duplicates** — Idempotent processing is often simpler than FIFO constraints

### When FIFO Queue Wins

1. **Financial transactions** — Must process in order, no duplicates
2. **State machine transitions** — Events must arrive in order
3. **Audit trails** — Ordering is a regulatory requirement
4. **Command queues** — Commands must execute in sequence

### The "Middle Ground" Pattern

```
Use Standard Queue + Consumer-Side Ordering:

    Producer:
        sendMessage(body={...}, attributes={sequenceNumber=42, entityId="order-123"})

    Consumer:
        1. Receive messages
        2. Group by entityId
        3. Sort by sequenceNumber within each group
        4. Process in order
        5. Use idempotency key to handle duplicates

    Advantage: Unlimited throughput + ordering where needed
    Disadvantage: More complex consumer logic, eventual ordering (not real-time)
```

---

## 16. Capacity Planning & Cost Optimization

### SQS Pricing Model

```
Standard Queue:
    First 1 million requests/month:  FREE
    After:  $0.40 per million requests

FIFO Queue:
    First 1 million requests/month:  FREE
    After:  $0.50 per million requests

Data transfer:
    First 1 GB/month:  FREE
    Up to 10 TB/month:  $0.09/GB (out to internet)
    Cross-AZ:  FREE (SQS handles multi-AZ internally)

Important: Each 64 KB chunk of payload counts as 1 request.
    256 KB message = 4 requests billed
```

### Cost Optimization Strategies

#### Strategy 1: Batching (most impactful)

```
Before batching:
    1,000,000 messages/day × 3 API calls each (send + receive + delete)
    = 3,000,000 requests/day
    = $1.20/day = $36/month

After batching (10 per batch):
    1,000,000 messages/day × 3 batch calls / 10 messages per batch
    = 300,000 requests/day
    = $0.12/day = $3.60/month

    Savings: 90% ($32.40/month)
```

#### Strategy 2: Long Polling

```
Before (short polling, 4 calls/sec when idle):
    Idle 20 hours/day × 3600 sec × 4 calls/sec = 288,000 empty receives/day

After (long polling, 20s wait):
    Idle 20 hours/day × 3600 sec / 20 sec = 3,600 calls/day

    Savings: 284,400 API calls/day = ~$3.40/month
```

#### Strategy 3: Right-Sizing Message Payloads

```
256 KB message = 4 requests billed
64 KB message = 1 request billed

If you can compress or use claim-check pattern:
    Reduce 256 KB → 64 KB = 75% cost reduction on send
```

#### Strategy 4: Extended Client Library for Large Messages

```
Without Extended Client:
    2 MB message → Cannot send (exceeds 256 KB limit)
    Must split into ~8 messages → 8× send cost + reassembly complexity

With Extended Client:
    2 MB message → Stored in S3, SQS holds reference (~1 KB)
    Cost: 1 SQS request + 1 S3 PUT + 1 S3 GET
    Often cheaper than 8 SQS requests for large payloads
```

### Capacity Planning Checklist

| Question | Why It Matters |
|----------|---------------|
| What is peak messages/second? | Determines if FIFO limits are a concern |
| What is average message size? | Affects billing (64 KB chunks) |
| How many distinct consumers? | Affects in-flight message limits |
| What is processing time per message? | Determines consumer fleet size |
| What is acceptable processing lag? | Determines auto-scaling thresholds |
| Standard or FIFO? | 300 TPS vs unlimited |
| How many message groups (FIFO)? | Determines max consumer parallelism |
| Retention period needed? | 4 days default vs up to 14 days |
| Need encryption? | SSE-SQS (free) vs SSE-KMS (costly — KMS call per send/receive) |

### Cost Comparison: SQS vs Self-Managed Alternatives

| Solution | Monthly Cost (1M msgs/day) | Operational Overhead |
|----------|:-------------------------:|:-------------------:|
| **SQS Standard** | ~$4 | None — fully managed |
| **SQS FIFO** | ~$5 | None — fully managed |
| **RabbitMQ on EC2** | ~$50-200 (instance cost) | High — manage clusters, upgrades, monitoring |
| **Amazon MQ (RabbitMQ)** | ~$100-400 | Medium — managed but limited scaling |
| **MSK (Kafka)** | ~$200-500 | Medium — manage topics, partitions, retention |
| **Redis Streams** | ~$50-200 | Medium — manage cluster, memory, persistence |

**SQS is almost always the cheapest option** for pure queuing workloads. Self-managed
alternatives only win when you need features SQS doesn't have (e.g., Kafka's log replay
with weeks of retention, RabbitMQ's routing topologies).

---

## 17. Cross-References

| Topic | Document |
|-------|----------|
| API contracts and error handling | [api-contracts.md](api-contracts.md) |
| End-to-end message flows | [flow.md](flow.md) |
| Storage architecture and durability | [data-storage-and-durability.md](data-storage-and-durability.md) |
| Delivery semantics and ordering | [delivery-guarantees-and-ordering.md](delivery-guarantees-and-ordering.md) |
| Message lifecycle states | [message-lifecycle.md](message-lifecycle.md) |
| Interview simulation | [interview-simulation.md](interview-simulation.md) |

### AWS Documentation References

- [Increasing throughput using horizontal scaling and action batching](https://docs.aws.amazon.com/AWSSimpleQueueService/latest/SQSDeveloperGuide/sqs-throughput-horizontal-scaling-and-batching.html)
- [High throughput for FIFO queues](https://docs.aws.amazon.com/AWSSimpleQueueService/latest/SQSDeveloperGuide/high-throughput-fifo.html)
- [Client-side buffering and request batching](https://docs.aws.amazon.com/AWSSimpleQueueService/latest/SQSDeveloperGuide/sqs-client-side-buffering-request-batching.html)
- [Amazon SQS short and long polling](https://docs.aws.amazon.com/AWSSimpleQueueService/latest/SQSDeveloperGuide/sqs-short-and-long-polling.html)
- [Available CloudWatch metrics for Amazon SQS](https://docs.aws.amazon.com/AWSSimpleQueueService/latest/SQSDeveloperGuide/sqs-available-cloudwatch-metrics.html)
- [Amazon SQS quotas](https://docs.aws.amazon.com/AWSSimpleQueueService/latest/SQSDeveloperGuide/sqs-quotas.html)
- [Amazon SQS message quotas](https://docs.aws.amazon.com/AWSSimpleQueueService/latest/SQSDeveloperGuide/quotas-messages.html)
- [Amazon SQS FIFO queue quotas](https://docs.aws.amazon.com/AWSSimpleQueueService/latest/SQSDeveloperGuide/quotas-fifo.html)
- [Configuring scaling behavior for SQS event source mappings (Lambda)](https://docs.aws.amazon.com/lambda/latest/dg/services-sqs-scaling.html)
- [Scaling based on Amazon SQS (EC2 Auto Scaling)](https://docs.aws.amazon.com/autoscaling/ec2/userguide/scale-sqs-queue-cli.html)
