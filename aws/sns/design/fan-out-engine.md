# Amazon SNS — Fan-Out Engine Deep Dive

> Companion deep dive to the [interview simulation](interview-simulation.md). This document explores how SNS delivers a single published message to potentially millions of subscribers in parallel, the internal architecture of the fan-out engine, and the design decisions that make massive fan-out tractable.

---

## Table of Contents

1. [The Core Problem: One-to-Many Delivery](#1-the-core-problem-one-to-many-delivery)
2. [Fan-Out Numbers at Scale](#2-fan-out-numbers-at-scale)
3. [Publish Path vs Delivery Path — The Separation](#3-publish-path-vs-delivery-path--the-separation)
4. [The Publish Path in Detail](#4-the-publish-path-in-detail)
5. [The Fan-Out Coordinator](#5-the-fan-out-coordinator)
6. [Subscription Partitioning and Chunking](#6-subscription-partitioning-and-chunking)
7. [Delivery Workers — Parallel Async Delivery](#7-delivery-workers--parallel-async-delivery)
8. [Message Filtering in the Fan-Out Path](#8-message-filtering-in-the-fan-out-path)
9. [Back-Pressure and Flow Control](#9-back-pressure-and-flow-control)
10. [Cell-Based Architecture](#10-cell-based-architecture)
11. [Raw Message Delivery](#11-raw-message-delivery)
12. [PublishBatch — Batched Fan-Out](#12-publishbatch--batched-fan-out)
13. [Fan-Out for FIFO Topics](#13-fan-out-for-fifo-topics)
14. [Failure Modes and Recovery](#14-failure-modes-and-recovery)
15. [Fan-Out Performance Characteristics](#15-fan-out-performance-characteristics)
16. [Comparison with Other Fan-Out Systems](#16-comparison-with-other-fan-out-systems)
17. [Cross-References](#17-cross-references)

---

## 1. The Core Problem: One-to-Many Delivery

SNS solves a fundamentally different problem than SQS. SQS is point-to-point: one message, one consumer. SNS is one-to-many: one published message must be **pushed** to every subscriber on a topic.

```
SQS (point-to-point):
    Producer → [Queue] → Consumer
    One message, one consumer pulls it.

SNS (fan-out):
    Publisher → [Topic] → Subscriber A (SQS)
                        → Subscriber B (Lambda)
                        → Subscriber C (HTTP)
                        → Subscriber D (Email)
                        → ... up to 12.5 million subscribers

    One Publish, N deliveries. N can be 12,500,000.
```

The fan-out engine is the component that turns that single Publish call into N parallel deliveries. It is the heart of SNS.

### Why Fan-Out Is Hard

| Challenge | Why It's Hard |
|-----------|--------------|
| **Scale** | A single topic can have 12.5M subscribers. One Publish → 12.5M deliveries. |
| **Heterogeneity** | Subscribers have wildly different protocols: SQS (5ms), Lambda (10ms), HTTP (5 seconds), Email (minutes), SMS (seconds). |
| **Independence** | A slow HTTP subscriber must NOT block a fast SQS subscriber. Each delivery is independent. |
| **Failure isolation** | If Subscriber C is down, Subscribers A, B, D must still receive the message. |
| **Durability** | If the fan-out engine crashes mid-delivery, completed deliveries must NOT be repeated (or if they are, subscribers handle duplicates). |
| **Latency** | Publisher latency (Publish API response time) must be independent of subscriber count. Publishing to a topic with 10M subs should be as fast as publishing to a topic with 1 sub. |

---

## 2. Fan-Out Numbers at Scale

### Headline Figures

| Metric | Value | Source |
|--------|-------|--------|
| Max subscriptions per standard topic | 12,500,000 | AWS SNS Quotas |
| Max subscriptions per FIFO topic | 100 | AWS SNS Quotas |
| Publish rate (standard, US East) | 30,000 msg/sec per topic (soft limit) | AWS SNS Quotas |
| Publish rate (FIFO) | 300 msg/sec per message group; 30,000 per topic | AWS SNS Quotas |
| PublishBatch max entries | 10 per batch, 256 KB aggregate | AWS SNS Quotas |
| Subscribe/Unsubscribe API rate | 100 TPS | AWS SNS Quotas |

### Fan-Out Amplification Math

```
Worst case for a single topic:
    Publish rate:    30,000 msg/sec
    Subscribers:     12,500,000
    Deliveries/sec:  30,000 × 12,500,000 = 375 BILLION deliveries/sec

    This is obviously impossible on a per-topic basis.
    In practice, topics with 12.5M subs don't get 30K publishes/sec.

Realistic high-load scenario:
    Publish rate:    1,000 msg/sec
    Subscribers:     100,000
    Deliveries/sec:  100,000,000 (100M)

    This is the scale the fan-out engine must handle per topic.

Global aggregate (estimated):
    Active topics:         ~100 million
    Avg subs per topic:    ~10
    Global publish rate:   ~10 million msg/sec
    Global deliveries:     ~100 million deliveries/sec
```

### The Fan-Out Amplification Factor

```
Amplification factor = total deliveries / total publishes

    Typical topic (5 subs):          5×
    High-fan-out topic (1000 subs):  1,000×
    Extreme topic (12.5M subs):      12,500,000×

    This amplification is the core cost driver of SNS.
    Every byte published costs amplification × delivery cost.
```

---

## 3. Publish Path vs Delivery Path — The Separation

The single most important architectural decision in SNS: **separate accepting the message from delivering it.**

```
┌──────────────────────────────────────────────────────────────────┐
│                       PUBLISH PATH                                │
│                    (synchronous, fast)                             │
│                                                                   │
│   Publisher → API Server → Validate → Auth → Store (Multi-AZ)    │
│                                         → Return 200 + MessageId │
│                                                                   │
│   Latency target: < 20ms p50                                     │
│   What happens: Message is durably stored. Publisher is done.     │
│   Publisher latency is INDEPENDENT of subscriber count.           │
└──────────────────────────────┬───────────────────────────────────┘
                               │
                               │ (asynchronous trigger)
                               ▼
┌──────────────────────────────────────────────────────────────────┐
│                       DELIVERY PATH                               │
│                    (asynchronous, parallel)                        │
│                                                                   │
│   Message Store → Fan-Out Coordinator → Partition Subscriptions   │
│                                       → Enqueue Delivery Tasks    │
│                                                                   │
│   Delivery Workers → Evaluate Filters → Deliver to Endpoint      │
│                    → Track Status     → Retry on Failure          │
│                    → DLQ on Exhaustion                            │
│                                                                   │
│   Latency: varies by protocol (5ms SQS, seconds for HTTP/email)  │
│   Duration: milliseconds (SQS) to 23 days (AWS-managed retries)  │
└──────────────────────────────────────────────────────────────────┘
```

### Why This Separation Matters

| Without separation | With separation |
|---|---|
| Publisher blocks until all N subscribers receive the message | Publisher returns in < 20ms regardless of subscriber count |
| If any subscriber is slow, publisher is slow | Publisher latency is independent of subscriber performance |
| If fan-out crashes, publisher gets an error | If fan-out crashes, message is in durable store; another worker resumes |
| Can't scale Publish API independently of delivery | Publish fleet and delivery fleet scale independently |

---

## 4. The Publish Path in Detail

### Step-by-Step Publish Flow

```
Publisher
  │
  │  POST https://sns.us-east-1.amazonaws.com/
  │  Action=Publish
  │  TopicArn=arn:aws:sns:us-east-1:123456789012:my-topic
  │  Message=Hello World
  │  MessageAttributes.entry.1.Name=event_type
  │  MessageAttributes.entry.1.Value.DataType=String
  │  MessageAttributes.entry.1.Value.StringValue=order_placed
  │
  ▼
┌─────────────────────────────────────────────────────────┐
│  API Server (Stateless Fleet)                            │
│                                                          │
│  1. Parse request                                        │
│  2. Authenticate (SigV4 signature verification)          │
│  3. Authorize (IAM policy + topic policy check)          │
│  4. Validate:                                            │
│     - Topic exists?                                      │
│     - Message size ≤ 256 KB?                             │
│     - Message attributes valid? (max 10 for raw delivery)│
│     - For FIFO: MessageGroupId present?                  │
│     - For FIFO: Dedup ID present (or content-based)?     │
│  5. Generate MessageId (UUID)                            │
│  6. Store message durably (multi-AZ replication)         │
│  7. Return HTTP 200 + MessageId                          │
│                                                          │
│  Total latency: < 20ms p50                               │
└──────────────────────────┬──────────────────────────────┘
                           │
                           │  (async: trigger fan-out)
                           ▼
                    Fan-Out Engine
```

### What Gets Stored

```
Message Record (in durable message store):
{
    messageId:        "dc1e94d9-56c5-5e96-808d-cc7f68faa162",
    topicArn:         "arn:aws:sns:us-east-1:123456789012:my-topic",
    message:          "Hello World",
    subject:          null,
    messageAttributes: {
        "event_type": { dataType: "String", stringValue: "order_placed" }
    },
    timestamp:        "2026-02-13T10:30:00.000Z",
    publisherAccountId: "123456789012",

    // For FIFO topics only:
    messageGroupId:       "order-123",
    messageDeduplicationId: "txn-abc-001",
    sequenceNumber:       47
}
```

### Multi-AZ Durability on Publish

[INFERRED — not officially documented]

```
API Server (AZ-a)
  │
  ├── Write to message store replica (AZ-a) ✓
  ├── Synchronous replicate to (AZ-b)       ✓  ← quorum (2/3)
  │   ════════════════════════════════════════
  │   Quorum met. Return 200 OK to publisher.
  │
  └── Async replicate to (AZ-c)             ✓  ← extra durability
```

Once the publisher receives 200 OK, the message is durable. Even if AZ-a goes down, AZ-b has the message and the fan-out engine can proceed.

---

## 5. The Fan-Out Coordinator

The fan-out coordinator is the brain of the delivery path. Its job: given a newly stored message, figure out WHO needs to receive it, PARTITION the work, and DISTRIBUTE it to delivery workers.

### Coordinator Architecture

```
                    ┌──────────────────────────────────┐
                    │       Message Store                │
                    │                                    │
                    │  New message notification:         │
                    │  "msg dc1e94d9 for topic X"        │
                    └──────────────┬───────────────────┘
                                   │
                                   ▼
                    ┌──────────────────────────────────┐
                    │     Fan-Out Coordinator            │
                    │     (for topic X's partition)      │
                    │                                    │
                    │  1. Read subscription list for     │
                    │     topic X from metadata store    │
                    │     → Returns 50,000 subscriptions │
                    │                                    │
                    │  2. For each subscription:         │
                    │     - Evaluate filter policy       │
                    │     - Skip non-matching subs       │
                    │     → 15,000 match                 │
                    │                                    │
                    │  3. Partition matching subs into    │
                    │     chunks of ~1,000               │
                    │     → 15 chunks                    │
                    │                                    │
                    │  4. Enqueue 15 delivery tasks      │
                    │     to internal work queue         │
                    └──────────────┬───────────────────┘
                                   │
                    ┌──────────────┼──────────────┐
                    ▼              ▼              ▼
             ┌───────────┐ ┌───────────┐ ┌───────────┐
             │ Delivery  │ │ Delivery  │ │ Delivery  │
             │ Worker 1  │ │ Worker 2  │ │ Worker 15 │
             │           │ │           │ │           │
             │ Subs 1-1K │ │ Subs 1K-2K│ │ Subs 14K- │
             │           │ │           │ │    15K    │
             └───────────┘ └───────────┘ └───────────┘
```

### Coordinator Responsibilities

| Responsibility | Detail |
|---------------|--------|
| **Subscription lookup** | Read from metadata store: all subscriptions for the topic, including endpoint, protocol, filter policy, delivery policy, DLQ config, raw message delivery flag |
| **Filter evaluation** | Evaluate each subscription's filter policy against the message attributes. Skip non-matching subscriptions entirely — they never see a delivery task. |
| **Subscription partitioning** | Break the matching subscription list into chunks. Each chunk becomes one delivery task. |
| **Task enqueuing** | Push delivery tasks to an internal work queue that delivery workers consume. |
| **NOT delivery** | The coordinator does NOT deliver messages. It only plans the work. |

### Coordinator Scalability

[INFERRED — not officially documented]

The coordinator must itself be horizontally scalable:

```
Topics are hash-partitioned across coordinator instances:

    hash(topicArn) mod N → coordinator instance

    Topic A → Coordinator 1
    Topic B → Coordinator 2
    Topic C → Coordinator 1  (hash collision, same coordinator)
    Topic D → Coordinator 3

    If Coordinator 2 fails:
        Its topic partition is reassigned to another coordinator
        (coordination via consensus service)

    The coordinator is lightweight (read metadata + enqueue tasks)
    so a single coordinator can handle many topics.
```

### Why Not Skip the Coordinator?

You might ask: why not have delivery workers directly read the subscription list?

```
Without coordinator:
    Worker 1 reads all 50,000 subs → processes 1-1000
    Worker 2 reads all 50,000 subs → processes 1001-2000
    ...
    Worker 50 reads all 50,000 subs → processes 49001-50000

    Problem: 50 workers each read 50,000 subs = 2.5 MILLION subscription reads
    per message. This hammers the metadata store.

With coordinator:
    Coordinator reads 50,000 subs ONCE → partitions → enqueues 50 tasks
    Workers only see their chunk of ~1,000 subs

    Subscription reads: 50,000 (once, by coordinator)
    Metadata store load: 50× lower
```

---

## 6. Subscription Partitioning and Chunking

### How Subscriptions Are Chunked

```
Topic: "order-events" with 50,000 subscriptions

Coordinator reads all 50,000 subscriptions:

    Sub 1:     SQS queue, filter={event_type: [order_placed]}
    Sub 2:     Lambda, filter={event_type: [order_placed]}
    Sub 3:     HTTP endpoint, no filter
    ...
    Sub 50000: SQS queue, filter={region: [eu-west]}

Message published with attributes: {event_type: "order_placed", region: "us-east"}

Step 1: Filter evaluation
    Sub 1:     event_type matches → INCLUDE
    Sub 2:     event_type matches → INCLUDE
    Sub 3:     no filter → INCLUDE (accept all)
    ...
    Sub 50000: region=eu-west ≠ us-east → EXCLUDE

    Result: 15,000 of 50,000 subscriptions match

Step 2: Chunk matching subscriptions
    Chunk 1:  Subs [1, 2, 3, ..., 1000]      → Delivery Task 1
    Chunk 2:  Subs [1001, 1002, ..., 2000]    → Delivery Task 2
    ...
    Chunk 15: Subs [14001, ..., 15000]         → Delivery Task 15

Step 3: Enqueue 15 tasks to internal work queue
```

### Chunk Size Considerations

[INFERRED — not officially documented]

| Chunk size | Pros | Cons |
|-----------|------|------|
| Small (100) | Fine-grained parallelism, fast per-chunk completion | More tasks = more queue overhead, more coordination |
| Medium (1,000) | Good balance of parallelism and overhead | — |
| Large (10,000) | Fewer tasks, less queue overhead | Less parallelism, one slow delivery blocks more subs |

A chunk size of ~1,000 is a reasonable estimate. It keeps each delivery task manageable
(a worker can do 1,000 async deliveries in parallel) while limiting the number of tasks.

### Protocol-Aware Chunking

[INFERRED — not officially documented]

A smarter chunking strategy groups subscriptions by protocol:

```
Chunk 1:  [SQS sub 1, SQS sub 2, ..., SQS sub 1000]        → SQS delivery worker
Chunk 2:  [Lambda sub 1, Lambda sub 2, ..., Lambda sub 500]  → Lambda delivery worker
Chunk 3:  [HTTP sub 1, HTTP sub 2, ..., HTTP sub 200]        → HTTP delivery worker
Chunk 4:  [Email sub 1, ..., Email sub 50]                   → Email delivery worker

Why: SQS deliveries are fast (5ms) and reliable. HTTP deliveries are slow (seconds)
and flaky. Mixing them in one chunk means the worker's async I/O capacity is unevenly
consumed. Protocol-specific workers can be tuned for their protocol's characteristics.
```

---

## 7. Delivery Workers — Parallel Async Delivery

### How a Delivery Worker Processes a Chunk

```
Delivery Worker pulls a task from the work queue:

    Task: {
        messageId: "dc1e94d9...",
        topicArn: "arn:aws:sns:...:order-events",
        message: "Hello World",
        messageAttributes: {event_type: "order_placed"},
        subscriptions: [sub1, sub2, ..., sub1000]
    }

Worker processes all 1,000 subscriptions in parallel:

    ┌────────────────────────────────────────────┐
    │  Delivery Worker                            │
    │                                             │
    │  Async event loop (not thread-per-delivery):│
    │                                             │
    │  for sub in subscriptions:                  │
    │      asyncDeliver(message, sub)             │
    │                                             │
    │  Each asyncDeliver:                         │
    │    1. Format message for protocol           │
    │       (SQS: SendMessage, HTTP: POST, etc.)  │
    │    2. Send delivery request (non-blocking)  │
    │    3. On success: mark delivered             │
    │    4. On failure: schedule retry             │
    │                                             │
    │  All 1,000 deliveries in flight concurrently│
    └────────────────────────────────────────────┘
```

### Protocol-Specific Delivery Formatting

When SNS delivers to a subscriber, the message format depends on the protocol and whether raw message delivery is enabled:

#### Default SNS Message Envelope

```json
{
    "Type": "Notification",
    "MessageId": "dc1e94d9-56c5-5e96-808d-cc7f68faa162",
    "TopicArn": "arn:aws:sns:us-east-1:123456789012:order-events",
    "Subject": null,
    "Message": "Hello World",
    "Timestamp": "2026-02-13T10:30:00.000Z",
    "SignatureVersion": "1",
    "Signature": "EXAMPLEpH+...",
    "SigningCertURL": "https://sns.us-east-1.amazonaws.com/...",
    "UnsubscribeURL": "https://sns.us-east-1.amazonaws.com/?Action=Unsubscribe&...",
    "MessageAttributes": {
        "event_type": {
            "Type": "String",
            "Value": "order_placed"
        }
    }
}
```

#### By Protocol

| Protocol | Delivery Method | Message Format |
|----------|----------------|---------------|
| **SQS** | Internal `SendMessage` API | SNS JSON envelope (or raw body if raw delivery enabled) |
| **Lambda** | Internal async `Invoke` | SNS JSON envelope as Lambda event payload |
| **HTTP/S** | HTTP POST to endpoint URL | SNS JSON envelope as POST body |
| **Email** | SMTP via internal relay | Plain text or JSON body |
| **Email-JSON** | SMTP via internal relay | Full SNS JSON envelope |
| **SMS** | Carrier gateway | Plain text message body only |
| **Mobile Push** | Platform service (APNs/FCM) | Platform-specific JSON payload |
| **Firehose** | Internal `PutRecord` API | SNS JSON envelope (or raw body) |

### Async I/O — Why Not Thread-Per-Delivery

```
Thread-per-delivery:
    1,000 subscriptions → 1,000 threads
    Each thread blocked on network I/O

    Problems:
    - 1,000 threads per chunk × many chunks = thread explosion
    - OS thread overhead: ~1 MB stack per thread = 1 GB for 1,000 threads
    - Context switching overhead dominates CPU

Async I/O (event loop):
    1,000 subscriptions → 1,000 non-blocking requests
    Single event loop handles all completions

    Benefits:
    - ~1,000 concurrent deliveries with minimal threads (1-4)
    - Memory efficient: each pending request is a small state object
    - CPU efficient: no thread context switching
    - Standard approach for high-concurrency I/O (like Netty, Node.js)
```

### Delivery Tracking Per Subscription

```
For each subscription in the chunk, the worker tracks:

    {
        subscriptionArn: "arn:aws:sns:...:order-events:abc123",
        status: "PENDING" | "DELIVERED" | "FAILED" | "RETRYING" | "DLQ",
        attempts: 0,
        lastError: null,
        lastAttemptTime: null
    }

    Status transitions:
    PENDING → DELIVERED    (success on first try)
    PENDING → RETRYING     (first attempt failed, will retry)
    RETRYING → DELIVERED   (retry succeeded)
    RETRYING → RETRYING    (retry failed, more retries left)
    RETRYING → FAILED      (all retries exhausted)
    FAILED → DLQ           (message sent to subscription's dead letter queue)
    FAILED → LOST          (no DLQ configured — message dropped)
```

---

## 8. Message Filtering in the Fan-Out Path

### Where Filtering Happens

Filtering is evaluated at the fan-out coordinator, **before** delivery tasks are created:

```
Without filtering:
    50,000 subs → 50,000 deliveries
    Cost: 50,000 × (network I/O + endpoint processing)

With filtering:
    50,000 subs → evaluate 50,000 filters (microseconds each)
    → 15,000 match → 15,000 deliveries
    Cost: 50,000 × filter eval (cheap) + 15,000 × (network I/O + endpoint processing)

    Savings: 35,000 avoided deliveries × $0.50/million = money saved
    Plus: 35,000 fewer endpoint invocations = reduced subscriber load
```

### Filter Policy Evaluation

SNS supports filtering against two scopes:

```
Scope 1: MessageAttributes (default)
    Filter against structured key-value attributes sent alongside the message body.

    Message:
        MessageAttributes = { "event_type": "order_placed", "region": "us-east" }

    Filter policy:
        { "event_type": ["order_placed"] }

    Evaluation: Does "order_placed" match ["order_placed"]? Yes → deliver.

Scope 2: MessageBody
    Filter against properties within the JSON message body itself.

    Message body:
        { "event_type": "order_placed", "order": { "amount": 99.99 } }

    Filter policy (on body):
        { "event_type": ["order_placed"], "order": { "amount": [{ "numeric": [">", 50] }] } }

    Evaluation: event_type matches AND amount > 50? Yes → deliver.
```

### Filter Operators

| Operator | Example | Matches |
|----------|---------|---------|
| **Exact string match** | `["order_placed"]` | Only "order_placed" |
| **Prefix** | `[{"prefix": "order_"}]` | "order_placed", "order_cancelled" |
| **Suffix** | `[{"suffix": "_placed"}]` | "order_placed", "item_placed" |
| **Anything-but** | `[{"anything-but": ["test"]}]` | Everything except "test" |
| **Equals-ignore-case** | `[{"equals-ignore-case": "ORDER"}]` | "order", "ORDER", "Order" |
| **Numeric range** | `[{"numeric": [">", 100, "<=", 500]}]` | Numbers between 100 (exclusive) and 500 (inclusive) |
| **IP address** | `[{"cidr": "10.0.0.0/24"}]` | IPs in the 10.0.0.0/24 range |
| **Exists** | `[{"exists": true}]` | Attribute is present (any value) |
| **Does not exist** | `[{"exists": false}]` | Attribute is absent |

### Filter Policy Logic

```
Multiple attributes: AND logic between different attribute names
Multiple values:    OR logic for values of the same attribute

Example filter:
{
    "event_type": ["order_placed", "order_shipped"],     ← OR: either value
    "region":     ["us-east", "us-west"]                 ← OR: either value
}

Evaluation:
    (event_type = "order_placed" OR event_type = "order_shipped")
    AND
    (region = "us-east" OR region = "us-west")
```

### Filter Limits

| Limit | Value |
|-------|-------|
| Max filter policies per topic | 200 |
| Max filter policies per account | 10,000 |
| Max nested depth (MessageBody scope) | 5 levels |
| Filter policy propagation delay | Up to 15 minutes |
| Max filter policy size | 256 KB |

### Filter Propagation Delay — The 15-Minute Problem

```
Time 0:00  — You update Sub A's filter policy from {event_type: ["order_placed"]}
              to {event_type: ["order_cancelled"]}

Time 0:00 to 15:00 — INCONSISTENT STATE
    Some coordinator instances have the old filter policy
    Some have the new filter policy

    Sub A might receive:
    - order_placed messages (old policy, from stale coordinators)
    - order_cancelled messages (new policy, from updated coordinators)
    - BOTH (depending on which coordinator handles each publish)

Time 15:00 — All coordinators have the new policy. Consistent.
```

**Why 15 minutes?** [INFERRED] The subscription metadata is cached at coordinator nodes for performance. Cache TTL or propagation delay accounts for the 15-minute window. Reloading metadata from the store on every publish would be too expensive at 30K publishes/sec.

---

## 9. Back-Pressure and Flow Control

### The Amplification Problem

```
A topic with 100,000 subscribers receiving 1,000 publishes/sec generates:
    100,000 × 1,000 = 100,000,000 deliveries/sec

If the delivery fleet can only handle 50,000,000 deliveries/sec:
    The delivery queue grows unboundedly
    Memory pressure on the delivery queue
    Eventually: OOM or degraded performance for ALL topics
```

### How SNS Handles Back-Pressure

#### Level 1: Publish API Rate Limits

```
Per-topic publish rate limits:
    Standard topics:  30,000 msg/sec (US East, soft limit)
    FIFO topics:      300 msg/sec per message group; 30,000 per topic

    These limits protect the system from unbounded fan-out amplification.
    Publishers receive HTTP 429 (Throttling) when over the limit.
```

#### Level 2: Per-Subscriber Delivery Rate Control

```
For HTTP/S endpoints, the delivery policy supports:

    "throttlePolicy": {
        "maxReceivesPerSecond": 10
    }

    This prevents SNS from overwhelming an HTTP endpoint.
    Without this, a topic burst could send thousands of requests
    per second to a subscriber that can only handle 10 req/sec.
```

#### Level 3: Internal Delivery Queue Depth Monitoring

[INFERRED — not officially documented]

```
If the internal delivery task queue exceeds a depth threshold:
    1. Fan-out coordinators slow down task creation (back-pressure upstream)
    2. Low-priority deliveries (email, SMS) may be deprioritized
    3. Alarms fire for the SNS operations team
```

#### Level 4: Protocol Prioritization

[INFERRED — not officially documented]

```
Delivery priority (likely):
    1. SQS / Lambda / Firehose  — AWS-managed, fast, reliable
    2. HTTP/S endpoints          — Customer-managed, variable
    3. Email / SMS               — External gateways, rate-limited

    Why: If the delivery fleet is overloaded, it's better to deliver
    to SQS (which buffers durably) than to spend capacity on slow
    HTTP endpoints that might time out anyway.
```

---

## 10. Cell-Based Architecture

[INFERRED — not officially documented, but consistent with AWS architecture patterns described at re:Invent]

### Why Cells?

```
Without cells:
    All topics share the same coordinator and delivery fleet.
    A burst on Topic A (10M subs, 1K publishes/sec) starves Topic B.

    ┌──────────────────────────────────────┐
    │          Shared Fleet                 │
    │                                       │
    │  Topic A (10M subs) → consumes 90%   │
    │  Topic B (10 subs)  → starved         │
    │  Topic C (100 subs) → starved         │
    └──────────────────────────────────────┘

With cells:
    Topics are partitioned across isolated cells.
    Each cell has its own coordinator + delivery fleet.

    ┌─────────────────┐  ┌─────────────────┐
    │     Cell 1       │  │     Cell 2       │
    │                  │  │                  │
    │  Topic A (10M)   │  │  Topic B (10)    │
    │  Own coordinator │  │  Topic C (100)   │
    │  Own workers     │  │  Own coordinator │
    │                  │  │  Own workers     │
    │  Blast radius:   │  │                  │
    │  only Cell 1     │  │  Unaffected by   │
    │                  │  │  Topic A's burst │
    └─────────────────┘  └─────────────────┘
```

### Cell Architecture

```
                    ┌────────────────────┐
                    │   Control Plane     │
                    │                    │
                    │   Topic → Cell     │
                    │   mapping          │
                    │                    │
                    │   Cell health      │
                    │   monitoring       │
                    │                    │
                    │   Cell rebalancing │
                    └────────┬───────────┘
                             │
              ┌──────────────┼──────────────┐
              ▼              ▼              ▼
       ┌──────────┐   ┌──────────┐   ┌──────────┐
       │  Cell 1   │   │  Cell 2   │   │  Cell N   │
       │           │   │           │   │           │
       │ Coord.    │   │ Coord.    │   │ Coord.    │
       │ Workers   │   │ Workers   │   │ Workers   │
       │ Queue     │   │ Queue     │   │ Queue     │
       │           │   │           │   │           │
       │ Topics:   │   │ Topics:   │   │ Topics:   │
       │ A, D, G   │   │ B, E, H   │   │ C, F, I   │
       └──────────┘   └──────────┘   └──────────┘
```

### Benefits of Cell Isolation

| Benefit | Detail |
|---------|--------|
| **Blast radius reduction** | A failure in Cell 1 only affects topics in Cell 1 |
| **Noisy neighbor protection** | A high-throughput topic in Cell 1 doesn't affect Cell 2 |
| **Independent scaling** | Cells with high-fan-out topics get more workers |
| **Incremental deployment** | New code can be deployed to one cell at a time (canary) |
| **Fault isolation** | A bug that crashes coordinators in Cell 1 doesn't affect Cell 2 |

---

## 11. Raw Message Delivery

Raw message delivery strips the SNS JSON envelope and delivers only the message body.

### With vs Without Raw Delivery

```
Published message: "Hello World"
Published attributes: { event_type: "order_placed" }

WITHOUT raw delivery (default for SQS):
    SQS receives:
    {
        "Type": "Notification",
        "MessageId": "dc1e94d9...",
        "TopicArn": "arn:aws:sns:...",
        "Message": "Hello World",
        "Timestamp": "2026-02-13T10:30:00Z",
        "Signature": "...",
        "MessageAttributes": { "event_type": { "Type": "String", "Value": "order_placed" } }
    }

    Consumer must parse JSON envelope to get "Hello World".

WITH raw delivery:
    SQS receives:
    "Hello World"

    Message attributes delivered as SQS message attributes (not in body).
    Consumer gets the raw payload directly.
```

### Supported Protocols for Raw Delivery

| Protocol | Raw Delivery Supported | Notes |
|----------|:---------------------:|-------|
| SQS | Yes | Message attributes become SQS message attributes (max 10) |
| HTTP/S | Yes | Header `x-amz-sns-rawdelivery: true` added; attributes NOT sent |
| Firehose | Yes | Attributes NOT sent |
| Lambda | No | Always receives SNS JSON envelope |
| Email | No | — |
| SMS | No | Already receives plain text only |
| Mobile Push | No | — |

### The 10-Attribute Limit with Raw Delivery

```
With raw delivery enabled for SQS:
    Message attributes are mapped to SQS message attributes.
    SQS has a limit of 10 message attributes.

    If your SNS message has > 10 attributes:
        The message is DISCARDED as a client-side error.
        It does NOT go to the DLQ.
        It is silently lost (except for CloudWatch metrics).

    This is a common pitfall. Always check attribute count
    when enabling raw message delivery for SQS subscriptions.
```

---

## 12. PublishBatch — Batched Fan-Out

### How PublishBatch Works

```
Single Publish:
    1 API call → 1 message → fan-out to N subscribers

PublishBatch:
    1 API call → up to 10 messages → each fans out to N subscribers

    10× throughput with the same number of API calls.
    10× cost reduction on the publish side.
```

### PublishBatch Constraints

| Constraint | Value |
|-----------|-------|
| Max messages per batch | 10 |
| Max aggregate payload | 256 KB total across all messages |
| Batch ID per entry | Required, up to 80 characters, unique within batch |
| FIFO: MessageGroupId | Required per message (can differ within batch) |
| FIFO: MessageDeduplicationId | Required per message (unless content-based dedup) |

### Partial Failure Handling

```
PublishBatch with 10 messages:

    Response:
    {
        "Successful": [
            { "Id": "msg1", "MessageId": "uuid-1" },
            { "Id": "msg2", "MessageId": "uuid-2" },
            ... 8 more
        ],
        "Failed": [
            { "Id": "msg4", "Code": "InternalError", "SenderFault": false, "Message": "..." }
        ]
    }

    Messages in "Successful" → fan-out triggered for each
    Messages in "Failed" → NOT published, NOT fanned out
    Caller MUST check "Failed" and retry those messages
```

### How Batched Fan-Out Works Internally

[INFERRED — not officially documented]

```
PublishBatch(msg1, msg2, ..., msg10)
    │
    ▼
API Server:
    1. Validate all 10 messages
    2. Store all 10 messages (possibly in a single batch write)
    3. Return batch response
    │
    ▼
Fan-Out Coordinator:
    For each of the 10 messages:
        1. Read subscription list (cached — same topic for all 10)
        2. Evaluate filters per message (each message may match different subs)
        3. Partition and enqueue delivery tasks

    Optimization: subscription list is read ONCE for the batch,
    not once per message (since all 10 go to the same topic).
```

---

## 13. Fan-Out for FIFO Topics

FIFO fan-out is fundamentally different from standard fan-out because of ordering constraints.

### Standard vs FIFO Fan-Out

```
Standard fan-out:
    Message arrives → deliver to ALL matching subs in parallel
    No ordering constraint between deliveries
    Sub A might receive before Sub B — that's fine

FIFO fan-out:
    Message arrives → deliver to all subs, but:
    1. Within each subscription, messages must arrive IN ORDER
       for the same MessageGroupId
    2. Message N+1 should not be delivered until Message N is confirmed
       (or at least, the subscriber's FIFO queue enforces ordering)
    3. Only SQS FIFO queues (and SQS standard queues) are valid subscribers
```

### FIFO Fan-Out Architecture

```
Publisher: Publish(topic.fifo, msg, group="acct-123", seq=47)

    Fan-Out Coordinator:
        1. Read subscription list (max 100 subs for FIFO)
        2. Evaluate filters
        3. For each matching sub:
           - Deliver via SQS SendMessage to FIFO queue
           - Include MessageGroupId and MessageDeduplicationId
           - SQS FIFO queue enforces ordering within the group
        4. Delivery is parallel across subscriptions
           (ordering is per-subscription, per-group — not global)

    ┌─────────────────┐
    │  FIFO Topic      │
    │  (100 subs max) │
    └────────┬────────┘
             │
    ┌────────┼────────┐
    ▼        ▼        ▼
  SQS.fifo SQS.fifo SQS.fifo
  (Sub A)  (Sub B)  (Sub C)

  Each queue independently maintains ordering per MessageGroupId.
  Sub A and Sub B may process the same message at slightly different times,
  but within each queue, messages for "acct-123" are in order.
```

### Why Only 100 Subscriptions for FIFO?

| Reason | Explanation |
|--------|------------|
| **Sequential ordering cost** | Each subscription must receive messages in order within a group — more subscriptions = more sequential work |
| **Deduplication state** | Each subscription needs dedup tracking within the 5-minute window |
| **Exactly-once semantics** | Confirming exactly-once delivery to each subscription is expensive |
| **SQS FIFO throughput** | Each SQS FIFO queue has its own throughput limits; many queues compounds the load |

Standard topics trade ordering and dedup guarantees for massive fan-out (12.5M subs). FIFO topics trade fan-out scale for strict ordering and exactly-once delivery.

---

## 14. Failure Modes and Recovery

### Failure Mode 1: API Server Crash After Store, Before Response

```
Publisher → API Server → Store message (success) → CRASH (before 200 OK)

What happens:
    - Message is in the durable store → fan-out will proceed
    - Publisher never got 200 OK → publisher retries
    - Standard topic: duplicate publish → duplicate fan-out → at-least-once
    - FIFO topic: dedup ID catches the retry → no duplicate delivery

Recovery: Automatic. Publisher retry handles it.
```

### Failure Mode 2: Fan-Out Coordinator Crash

```
Coordinator reads 50,000 subs, creates chunks, enqueues 15 tasks → CRASH after 10 tasks

What happens:
    - 10 of 15 delivery tasks are in the work queue → workers process them
    - 5 chunks of subscribers never get delivery tasks
    - Message is still in the durable store

Recovery:
    - The coordinator's crash is detected
    - Another coordinator picks up the topic's partition
    - New coordinator checks: which subscriptions haven't been delivered?
    - Creates delivery tasks for the remaining 5 chunks

    [INFERRED] Delivery tracking per subscription enables this recovery.
```

### Failure Mode 3: Delivery Worker Crash

```
Worker pulls a task (1,000 subs), delivers to 700, CRASH

What happens:
    - 700 subscribers received the message ✓
    - 300 subscribers did not
    - The delivery task was "in flight" — the work queue re-queues it after a visibility timeout

Recovery:
    - Another worker picks up the re-queued task
    - Delivers to all 1,000 subs again
    - 700 subs receive a DUPLICATE (at-least-once delivery — subscribers handle it)
    - 300 subs receive it for the first time

    This is why at-least-once is the guarantee: crash recovery causes duplicates.
```

### Failure Mode 4: AZ Outage

```
AZ-a goes down (hosts API servers, some coordinators, some workers)

What happens:
    - Messages already in the durable store (replicated to AZ-b, AZ-c) are safe
    - API servers in AZ-b and AZ-c handle new publishes
    - Coordinators and workers in AZ-b and AZ-c take over
    - In-flight delivery tasks on AZ-a workers are re-queued after timeout
    - Some deliveries may be duplicated (at-least-once)

Recovery: Automatic. Multi-AZ replication ensures durability.
    Fan-out resumes from surviving AZs.
```

### Failure Mode 5: Subscriber Endpoint Down

```
HTTP subscriber returns 503 for all deliveries

What happens:
    - Delivery worker records failure
    - Retry policy kicks in (50 retries over 6 hours for customer-managed)
    - If endpoint recovers within 6 hours: messages delivered on retry
    - If not: messages sent to DLQ (if configured) or lost

Recovery: Depends on subscriber. SNS retries patiently.
    See [delivery-and-retries.md](delivery-and-retries.md) for full retry details.
```

---

## 15. Fan-Out Performance Characteristics

### Latency Breakdown

```
Publisher Publish latency (what the publisher experiences):
    SigV4 auth:        ~1ms
    Validation:        ~1ms
    Store (multi-AZ):  ~10-15ms
    Return response:   ~1ms
    ─────────────────────────────
    Total:             ~15-20ms (p50)

    This is INDEPENDENT of subscriber count.
    Topic with 1 sub: ~15ms
    Topic with 12.5M subs: ~15ms

Delivery latency (time from Publish to subscriber receiving):
    Fan-out coordination:    ~5-10ms [INFERRED]
    Queue + worker pickup:   ~5-10ms [INFERRED]
    Protocol-specific delivery:
        SQS:     ~5ms
        Lambda:  ~10ms
        HTTP:    50ms - 30 sec (depends on endpoint)
        Email:   seconds to minutes
        SMS:     seconds

    Total (SQS subscriber): ~20-30ms (p50 from publish to SQS receive)
    Total (HTTP subscriber): ~100ms - 30 sec
```

### Throughput Characteristics

| Scenario | Throughput | Bottleneck |
|----------|-----------|-----------|
| Standard topic, 10 subs | 30,000 publishes/sec = 300,000 deliveries/sec | Publish rate limit |
| Standard topic, 10,000 subs | 30,000 publishes/sec = 300M deliveries/sec | Delivery fleet capacity |
| Standard topic, 12.5M subs | ~10-100 publishes/sec practical | Fan-out amplification limit |
| FIFO topic, 100 subs, 1 group | 300 msg/sec = 30,000 deliveries/sec | Message group throughput |
| FIFO topic, 100 subs, 100 groups | 30,000 msg/sec = 3M deliveries/sec | Per-topic throughput limit |

### Cost of Fan-Out

```
SNS pricing:
    Publish:               $0.50 per million publishes
    SQS delivery:          $0.00 (free — first million/month)
    HTTP/S delivery:        $0.60 per million deliveries
    Email/Email-JSON:       $2.00 per 100,000 deliveries
    SMS:                   $0.00645+ per message (US), varies by country
    Mobile push:           $0.50 per million deliveries

Example: Topic with 1,000 SQS subs, 100,000 publishes/day:
    Publish cost:    100,000 / 1M × $0.50 = $0.05/day
    Delivery cost:   100,000 × 1,000 = 100M SQS deliveries/day
                     Beyond free tier: ~$0/day (SNS to SQS is free after first 1M)
    Total:           ~$0.05/day = ~$1.50/month

    Fan-out to SQS is remarkably cheap.
```

---

## 16. Comparison with Other Fan-Out Systems

| System | Fan-Out Model | Max Subscribers | Throughput | Delivery Guarantee |
|--------|--------------|:---------------:|-----------|-------------------|
| **SNS** | Push to subscribers | 12.5M/topic | 30K publishes/sec/topic | At-least-once (standard) |
| **Kafka** | Pull by consumers | Unlimited consumer groups | 1M+ msg/sec/partition | At-least-once (configurable) |
| **Google Pub/Sub** | Push or pull | 10,000 subs/topic | Millions msg/sec | At-least-once |
| **Azure Service Bus** | Push (subscriptions) | 2,000 subs/topic | Thousands msg/sec | At-least-once or exactly-once |
| **EventBridge** | Push to targets | 5 targets/rule (300 rules) | Varies by region | At-least-once |
| **RabbitMQ** | Push via exchanges | Unlimited bindings | 10K-100K msg/sec | At-least-once |

### Key Differences

| Aspect | SNS | Kafka |
|--------|-----|-------|
| **Delivery** | Push — SNS delivers to subscribers | Pull — consumers fetch from partitions |
| **Fan-out** | One Publish → N deliveries by SNS | One produce → N consumer groups each pull independently |
| **Retention** | Transient — message deleted after delivery | Log-based — messages retained for days/weeks |
| **Replay** | No (except FIFO archive) | Yes — consumers can rewind offset |
| **Protocol diversity** | SQS, Lambda, HTTP, email, SMS, push | Only Kafka consumers |
| **Managed** | Fully serverless | Self-managed or MSK |

**When to use SNS**: Simple fan-out to diverse AWS endpoints. No need for replay. Serverless. Multi-protocol.

**When to use Kafka**: Need replay, log retention, consumer-controlled offset, very high throughput per topic.

---

## 17. Cross-References

| Topic | Document |
|-------|----------|
| Delivery retry policies and DLQ | [delivery-and-retries.md](delivery-and-retries.md) |
| SNS + SQS fan-out pattern | [sns-sqs-fanout.md](sns-sqs-fanout.md) |
| FIFO topics ordering and dedup | [fifo-topics.md](fifo-topics.md) |
| Message filtering deep dive | [message-filtering.md](message-filtering.md) |
| Mobile push and app-to-person | [mobile-push.md](mobile-push.md) |
| Interview simulation | [interview-simulation.md](interview-simulation.md) |

### AWS Documentation References

- [Amazon SNS message publishing](https://docs.aws.amazon.com/sns/latest/dg/sns-publishing.html)
- [Amazon SNS raw message delivery](https://docs.aws.amazon.com/sns/latest/dg/sns-large-payload-raw-message-delivery.html)
- [Amazon SNS message batching](https://docs.aws.amazon.com/sns/latest/dg/sns-batch-api-actions.html)
- [Amazon SNS message filtering](https://docs.aws.amazon.com/sns/latest/dg/sns-message-filtering.html)
- [Amazon SNS quotas](https://docs.aws.amazon.com/general/latest/gr/sns.html)
