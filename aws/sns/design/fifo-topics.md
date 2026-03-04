# Amazon SNS — FIFO Topics Deep Dive

> Companion deep dive to the [interview simulation](interview-simulation.md). This document covers FIFO topic ordering guarantees, message group IDs, deduplication mechanics, throughput limits, high-throughput mode, and how FIFO topics interact with FIFO SQS queues.

---

## Table of Contents

1. [Why FIFO Topics Exist](#1-why-fifo-topics-exist)
2. [Standard vs FIFO Topics — Complete Comparison](#2-standard-vs-fifo-topics--complete-comparison)
3. [Message Group IDs — The Ordering Unit](#3-message-group-ids--the-ordering-unit)
4. [Deduplication — Exactly-Once Processing](#4-deduplication--exactly-once-processing)
5. [Content-Based vs Explicit Deduplication](#5-content-based-vs-explicit-deduplication)
6. [FIFO Topic Throughput Limits](#6-fifo-topic-throughput-limits)
7. [High-Throughput Mode (FifoThroughputScope)](#7-high-throughput-mode-fifothroughputscope)
8. [FIFO Fan-Out Architecture](#8-fifo-fan-out-architecture)
9. [FIFO + Message Filtering — The At-Most-Once Caveat](#9-fifo--message-filtering--the-at-most-once-caveat)
10. [Creating and Configuring FIFO Topics](#10-creating-and-configuring-fifo-topics)
11. [FIFO SNS + FIFO SQS End-to-End Flow](#11-fifo-sns--fifo-sqs-end-to-end-flow)
12. [FIFO Internal Architecture](#12-fifo-internal-architecture)
13. [When to Use FIFO vs Standard + Idempotent Consumer](#13-when-to-use-fifo-vs-standard--idempotent-consumer)
14. [Common Pitfalls](#14-common-pitfalls)
15. [Cross-References](#15-cross-references)

---

## 1. Why FIFO Topics Exist

Standard SNS topics provide:
- **At-least-once delivery**: duplicates are possible
- **Best-effort ordering**: messages may arrive out of order

For many use cases, this is fine. But some use cases **require strict ordering and no duplicates**:

| Use Case | Why Ordering Matters | Why Dedup Matters |
|----------|---------------------|-------------------|
| **Financial transactions** | Debit $100 then credit $50 ≠ Credit $50 then debit $100 | Duplicate debit = customer charged twice |
| **Inventory updates** | Add 10 then remove 5 ≠ Remove 5 then add 10 (negative stock) | Duplicate add = phantom inventory |
| **State machine transitions** | CREATED → APPROVED → SHIPPED must be in order | Duplicate APPROVED → idempotency issues |
| **Audit trails** | Regulatory requirement: events in chronological order | Duplicate entries violate audit integrity |
| **Price updates** | Set price to $10 then $15 → final price must be $15 | Duplicate old price → reverts to stale value |

FIFO topics solve these by guaranteeing:
1. **Strict ordering** within a message group
2. **Exactly-once processing** within a 5-minute deduplication window

---

## 2. Standard vs FIFO Topics — Complete Comparison

| Dimension | Standard Topic | FIFO Topic |
|-----------|:-------------:|:----------:|
| **Topic name** | Any name | Must end with `.fifo` |
| **Ordering** | Best-effort | Strict per MessageGroupId |
| **Delivery** | At-least-once (duplicates possible) | Exactly-once (within 5-min dedup window) |
| **Throughput** | 30,000 publishes/sec per topic (soft) | 300 msg/sec per message group; 30,000 per topic |
| **Max subscriptions** | 12,500,000 | 100 |
| **Subscriber types** | SQS, Lambda, HTTP, email, SMS, push, Firehose | SQS FIFO queues and SQS standard queues **only** |
| **Message filtering** | Full support | Supported, but changes guarantee to at-most-once |
| **PublishBatch** | Supported (10 per batch) | Supported (10 per batch, each with GroupId + DedupId) |
| **Deduplication** | Not available | 5-minute window, content-based or explicit |
| **Message groups** | Optional (forwarded to SQS standard only) | Required (MessageGroupId on every publish) |
| **DLQ** | Standard SQS queue | FIFO SQS queue |
| **Convertible** | Cannot convert to FIFO | Cannot convert to standard |
| **Cost** | $0.50 per million publishes | $0.50 per million publishes |

---

## 3. Message Group IDs — The Ordering Unit

### The Key Insight: Per-Group Ordering, Not Global Ordering

FIFO topics do NOT order all messages globally. That would be a single-partition bottleneck. Instead, ordering is per **MessageGroupId**:

```
Publisher sends in this order:
    1. Publish(group="order-A", body="placed")
    2. Publish(group="order-B", body="placed")
    3. Publish(group="order-A", body="shipped")
    4. Publish(group="order-B", body="shipped")
    5. Publish(group="order-A", body="delivered")

Subscriber receives:
    order-A: placed → shipped → delivered    (guaranteed order within group A)
    order-B: placed → shipped                (guaranteed order within group B)

    No guarantee between groups:
    order-B:placed might arrive before order-A:placed
    That's by design — groups are independent.
```

### Choosing a MessageGroupId

The MessageGroupId should be a **business entity identifier** that defines the scope of ordering:

| Domain | MessageGroupId | Ordering Scope |
|--------|---------------|---------------|
| E-commerce | `order-12345` | All events for one order are in order |
| Banking | `account-67890` | All transactions for one account are in order |
| IoT | `device-sensor-42` | All readings from one sensor are in order |
| User events | `user-abc123` | All events for one user are in order |
| Inventory | `sku-WIDGET-001` | All stock changes for one SKU are in order |

### How MessageGroupId Affects Parallelism

```
Parallelism = number of distinct active MessageGroupIds

1 message group:
    All messages serialized → max 300 msg/sec, 1 consumer at a time
    ┌─────────────────────────────┐
    │  group="all" → Consumer 1   │  Consumer 2: idle
    └─────────────────────────────┘

100 message groups:
    Each group processed independently → up to 100 consumers in parallel
    ┌──────────────────┐  ┌──────────────────┐
    │ group="A" → C1   │  │ group="B" → C2   │  ...
    └──────────────────┘  └──────────────────┘

10,000 message groups:
    Maximum parallelism and throughput
    Up to 30,000 msg/sec aggregate (300 × 100 groups per partition)
```

### Anti-Pattern: Single MessageGroupId

```
BAD:
    All messages use MessageGroupId = "default"

    Result:
    - All messages go to one partition
    - Max throughput: 300 msg/sec
    - Only one consumer can process at a time
    - You've turned a distributed system into a single-threaded queue

GOOD:
    Use natural business key as MessageGroupId
    E.g., orderId, accountId, deviceId

    Result:
    - Messages distributed across partitions
    - Thousands of groups processed in parallel
    - Each consumer handles different groups concurrently
```

---

## 4. Deduplication — Exactly-Once Processing

### The Problem Dedup Solves

```
Publisher sends:
    Publish(group="order-A", body="debit $100", dedupId="txn-001")

Network hiccup: publisher doesn't receive 200 OK

Publisher retries:
    Publish(group="order-A", body="debit $100", dedupId="txn-001")

Without dedup:
    Two deliveries to subscriber → customer debited $200 (WRONG)

With dedup:
    SNS recognizes dedupId="txn-001" was seen in last 5 minutes
    Second publish is accepted (returns 200) but NOT delivered again
    One delivery → customer debited $100 (CORRECT)
```

### Deduplication Window: 5 Minutes

```
Timeline:

    t=0:00    Publish(dedupId="txn-001") → delivered ✓
    t=0:01    Publish(dedupId="txn-001") → accepted but NOT delivered (dedup) ✓
    t=2:30    Publish(dedupId="txn-001") → accepted but NOT delivered (dedup) ✓
    t=4:59    Publish(dedupId="txn-001") → accepted but NOT delivered (dedup) ✓
    t=5:01    Publish(dedupId="txn-001") → delivered AGAIN! (window expired)

    After 5 minutes, the same dedupId can cause a new delivery.
    The 5-minute window is a practical tradeoff:
    - Long enough to catch retries from network hiccups
    - Short enough to keep the dedup store bounded
```

### What Gets Deduplicated

```
Deduplication checks these fields:
    - MessageDeduplicationId (or content hash if content-based)
    - MessageGroupId (dedup is scoped to the group, not global to topic)

Same dedupId in DIFFERENT groups:
    Publish(group="A", dedupId="txn-001") → delivered ✓
    Publish(group="B", dedupId="txn-001") → delivered ✓ (different group!)

    Dedup is PER MESSAGE GROUP, not per topic.
```

### Conditions for Exactly-Once Delivery

Exactly-once is guaranteed only when ALL of these conditions are met:

| Condition | Why |
|-----------|-----|
| SQS FIFO queue subscriber | SQS FIFO queues enforce dedup on their end too |
| Proper permissions | Permission denied = client error = no retry |
| No message filtering | Filtering can cause at-most-once (see section 9) |
| Consumer processes + deletes before visibility timeout | Re-delivery after timeout = duplicate |
| Network stable for acknowledgment | Lost ack = SNS retries = potential duplicate |

If any condition is not met, the guarantee degrades to at-least-once or at-most-once.

---

## 5. Content-Based vs Explicit Deduplication

### Two Dedup Modes

#### Mode 1: Explicit MessageDeduplicationId

```
Publish(
    TopicArn = "order-events.fifo",
    Message = "debit $100",
    MessageGroupId = "account-123",
    MessageDeduplicationId = "txn-abc-001"    ← you provide this
)
```

Publisher must generate a unique dedup ID per logical message. Options:
- Transaction ID from your database
- UUID (guarantees uniqueness but can't dedup true retries)
- Business-logic key (e.g., `orderId-eventType`)

#### Mode 2: Content-Based Deduplication

```
Topic attribute: ContentBasedDeduplication = true

Publish(
    TopicArn = "order-events.fifo",
    Message = "debit $100",
    MessageGroupId = "account-123"
    // No MessageDeduplicationId needed
)

SNS automatically generates dedupId = SHA-256(MessageBody)
```

### Comparison

| Aspect | Explicit Dedup ID | Content-Based Dedup |
|--------|:-----------------:|:-------------------:|
| Publisher responsibility | Must provide unique ID | None (SNS handles it) |
| Dedup scope | The ID you provide | SHA-256 hash of message body |
| **Message attributes included in hash?** | N/A | **NO — only message body** |
| Same body, different attributes | Different dedup ID → both delivered | Same hash → **second is deduped** |
| Different body, same semantics | Same dedup ID → deduped | Different hash → **both delivered** |
| Best for | Idempotent retries with explicit keys | Messages where body uniqueness = semantic uniqueness |

### The Attribute Gotcha

```
Content-based dedup uses ONLY the message body for hashing.
Message attributes are NOT included.

Publish 1: body="debit $100", attributes={region: "us-east"}
Publish 2: body="debit $100", attributes={region: "eu-west"}

SHA-256("debit $100") == SHA-256("debit $100")

Result: Publish 2 is DEDUPED even though attributes differ!
This may or may not be what you want.

If attributes carry semantic meaning, use explicit dedup IDs.
```

---

## 6. FIFO Topic Throughput Limits

### Default Limits

```
Per message group:   300 messages/sec
                     300 publishes/sec
                     300 subscriptions deliveries/sec per group

Per topic (default): 300 API calls/sec (same as per-group when scope is Topic)

Per topic (high-throughput): Up to 30,000 API calls/sec
```

### With Batching

```
PublishBatch with 10 messages per batch:

Default mode:
    300 API calls/sec × 10 messages/batch = 3,000 messages/sec per group

High-throughput mode:
    30,000 API calls/sec × 10 messages/batch = 300,000 messages/sec per topic
    (still 300 API calls/sec per individual group)
```

### Why the 300 msg/sec Per Group Limit?

[INFERRED — not officially documented]

Within a message group, messages must be strictly ordered. This means:

1. **Sequential processing** — Each message must be assigned a sequence number before the next
2. **Dedup check** — Each message must be checked against the dedup store
3. **Single-partition constraint** — All messages for one group go to one partition

These serial operations limit throughput per group to ~300 msg/sec. The way to get higher aggregate throughput is to use more message groups.

---

## 7. High-Throughput Mode (FifoThroughputScope)

### What It Changes

```
Default (FifoThroughputScope = "Topic"):
    300 API calls/sec shared across ALL message groups in the topic
    Adding more groups doesn't increase throughput

High-Throughput (FifoThroughputScope = "MessageGroup"):
    300 API calls/sec PER MESSAGE GROUP
    More groups = proportionally more throughput
    Up to 30,000 API calls/sec per topic (regional limit)
```

### How to Enable

```java
Map<String, String> topicAttributes = Map.of(
    "FifoTopic", "true",
    "ContentBasedDeduplication", "false",
    "FifoThroughputScope", "MessageGroup"
);

CreateTopicRequest request = CreateTopicRequest.builder()
    .name("order-events.fifo")
    .attributes(topicAttributes)
    .build();
```

### Throughput Math

```
FifoThroughputScope = "Topic" (default):
    1 group, 300 msg/sec:        300 msg/sec total
    10 groups, 300 msg/sec each: 300 msg/sec total (shared limit!)
    100 groups:                  300 msg/sec total

FifoThroughputScope = "MessageGroup":
    1 group, 300 msg/sec:        300 msg/sec total
    10 groups, 300 msg/sec each: 3,000 msg/sec total
    100 groups:                  30,000 msg/sec total (topic limit)
    1000 groups:                 30,000 msg/sec total (topic limit caps it)
```

### Partitioning Under High-Throughput Mode

[INFERRED — not officially documented]

```
SNS hashes MessageGroupId to assign groups to partitions:

    hash("order-123") mod N → Partition 1
    hash("order-456") mod N → Partition 2
    hash("order-789") mod N → Partition 3

Each partition:
    - Handles 300 msg/sec per group
    - May host multiple groups
    - Has its own dedup store for its groups
    - Has its own sequence number generator

More groups → better distribution across partitions → higher aggregate throughput
```

---

## 8. FIFO Fan-Out Architecture

### FIFO vs Standard Fan-Out

```
Standard fan-out:
    Topic has 10,000 subs
    Publish → deliver to all 10,000 in parallel
    No ordering between deliveries
    Fire-and-forget delivery model

FIFO fan-out:
    Topic has 100 subs (max)
    Publish → deliver to all subs preserving order per MessageGroupId
    Each SQS FIFO queue must receive messages in sequence order
    Delivery cannot be truly fire-and-forget (ordering requires confirmation)
```

### How FIFO Delivery Works

```
Publish(group="order-A", seq=47, body="shipped")

SNS Fan-Out:
    For each FIFO SQS subscription:
        1. sqs:SendMessage(
             QueueUrl = "https://sqs.../inventory.fifo",
             MessageBody = "shipped",
             MessageGroupId = "order-A",
             MessageDeduplicationId = <derived from SNS dedup>
           )
        2. SQS FIFO queue receives with group="order-A", maintains order

    For SQS Standard subscriptions (if any):
        1. sqs:SendMessage(
             QueueUrl = "https://sqs.../analytics",
             MessageBody = "shipped"
             // MessageGroupId forwarded as message attribute
           )
        2. Standard queue receives — no ordering guarantee

    All subscriptions receive independently and in parallel.
    Within each FIFO queue, ordering per MessageGroupId is preserved.
```

### Subscriber Types for FIFO Topics

| Subscriber Type | Supported? | Notes |
|----------------|:----------:|-------|
| SQS FIFO Queue | Yes | Full ordering + dedup. Primary use case. |
| SQS Standard Queue | Yes | No ordering guarantee. Useful for analytics/audit. |
| Lambda | **No** | Not supported as direct FIFO subscriber |
| HTTP/S | **No** | Not supported |
| Email | **No** | Not supported |
| SMS | **No** | Not supported |
| Mobile Push | **No** | Not supported |
| Firehose | **No** | Not supported |

### Lambda with FIFO Topics (Workaround)

```
Since Lambda can't subscribe directly to FIFO topics:

    SNS FIFO Topic → SQS FIFO Queue → Lambda (event source mapping)

    Lambda polls the SQS FIFO queue.
    Lambda processes messages in order per MessageGroupId.
    Lambda scales concurrency = number of active message groups.

    This is the standard pattern for Lambda with FIFO ordering.
```

---

## 9. FIFO + Message Filtering — The At-Most-Once Caveat

### The Problem

When you combine message filtering with FIFO topics, the exactly-once guarantee changes to **at-most-once** for filtered-out messages.

```
Scenario:
    FIFO Topic: order-events.fifo
    Sub A: filter = {event_type: ["order_placed"]}
    Sub B: filter = (no filter — receives everything)

Publish sequence:
    1. Publish(group="X", body="placed", event_type="order_placed",   dedupId="d1")
    2. Publish(group="X", body="shipped", event_type="order_shipped",  dedupId="d2")
    3. Publish(group="X", body="delivered", event_type="order_delivered", dedupId="d3")

Sub A (filter: event_type=order_placed):
    Receives: "placed" ✓
    Skips:    "shipped", "delivered" (filtered out by SNS)

Sub B (no filter):
    Receives: "placed", "shipped", "delivered" ✓
```

### Why This Is "At-Most-Once" for Filtered Messages

```
The filtered-out messages are NEVER delivered to Sub A.
If the filtered message was somehow needed (e.g., filter policy was wrong),
it cannot be re-delivered. It's gone from Sub A's perspective.

Standard topic: filtered messages are also not delivered, but since delivery
                is at-least-once anyway, the semantics are consistent.

FIFO topic: the expectation is exactly-once. But filtering means some messages
            are delivered ZERO times to filtered subscribers = at-most-once.
```

### Recommendation

```
If you use FIFO topics for exactly-once guarantees:
    - Be very careful with filter policies
    - Ensure filters are correct BEFORE publishing
    - Remember: filter propagation delay is up to 15 minutes
    - During propagation, a subscriber might miss messages it should receive

If possible:
    - Use separate FIFO topics for different event types instead of filtering
    - OR: accept all messages and filter at the consumer side
```

---

## 10. Creating and Configuring FIFO Topics

### CLI

```bash
# Create FIFO topic
aws sns create-topic \
    --name order-events.fifo \
    --attributes FifoTopic=true,ContentBasedDeduplication=false,FifoThroughputScope=MessageGroup

# Create FIFO SQS queue
aws sqs create-queue \
    --queue-name inventory.fifo \
    --attributes FifoQueue=true,ContentBasedDeduplication=true

# Set queue policy (allow SNS to send)
aws sqs set-queue-attributes \
    --queue-url https://sqs.us-east-1.amazonaws.com/123456789012/inventory.fifo \
    --attributes '{
        "Policy": "{\"Statement\":[{\"Effect\":\"Allow\",\"Principal\":{\"Service\":\"sns.amazonaws.com\"},\"Action\":\"sqs:SendMessage\",\"Resource\":\"arn:aws:sqs:us-east-1:123456789012:inventory.fifo\",\"Condition\":{\"ArnEquals\":{\"aws:SourceArn\":\"arn:aws:sns:us-east-1:123456789012:order-events.fifo\"}}}]}"
    }'

# Subscribe FIFO queue to FIFO topic
aws sns subscribe \
    --topic-arn arn:aws:sns:us-east-1:123456789012:order-events.fifo \
    --protocol sqs \
    --notification-endpoint arn:aws:sqs:us-east-1:123456789012:inventory.fifo

# Publish to FIFO topic
aws sns publish \
    --topic-arn arn:aws:sns:us-east-1:123456789012:order-events.fifo \
    --message '{"orderId":"12345","status":"shipped"}' \
    --message-group-id "order-12345" \
    --message-deduplication-id "$(uuidgen)"
```

### Java SDK 2.x

```java
// Create FIFO topic
CreateTopicResponse topicResponse = snsClient.createTopic(
    CreateTopicRequest.builder()
        .name("order-events.fifo")
        .attributes(Map.of(
            "FifoTopic", "true",
            "ContentBasedDeduplication", "false",
            "FifoThroughputScope", "MessageGroup"
        ))
        .build()
);

// Publish
PublishResponse pubResponse = snsClient.publish(
    PublishRequest.builder()
        .topicArn(topicResponse.topicArn())
        .message("{\"orderId\":\"12345\",\"status\":\"shipped\"}")
        .messageGroupId("order-12345")
        .messageDeduplicationId(UUID.randomUUID().toString())
        .messageAttributes(Map.of(
            "event_type", MessageAttributeValue.builder()
                .dataType("String")
                .stringValue("order_shipped")
                .build()
        ))
        .build()
);

// Response includes SequenceNumber (FIFO only)
System.out.println("SequenceNumber: " + pubResponse.sequenceNumber());
```

### FIFO Topic Attributes

| Attribute | Values | Description |
|-----------|--------|-------------|
| `FifoTopic` | `true` | Required. Cannot be changed after creation. |
| `ContentBasedDeduplication` | `true` / `false` | If true, SHA-256 of body is used as dedup ID. If false, publisher must provide `MessageDeduplicationId`. |
| `FifoThroughputScope` | `Topic` / `MessageGroup` | `Topic` = 300 TPS shared across all groups. `MessageGroup` = 300 TPS per group (high-throughput mode). |

---

## 11. FIFO SNS + FIFO SQS End-to-End Flow

### Complete Sequence Diagram

```
Publisher                SNS FIFO Topic          SQS FIFO Queue A       SQS FIFO Queue B
    │                        │                        │                        │
    │  Publish(              │                        │                        │
    │   group="X",           │                        │                        │
    │   dedupId="d1",        │                        │                        │
    │   body="placed")       │                        │                        │
    │───────────────────────►│                        │                        │
    │                        │                        │                        │
    │                        │  1. Dedup check:       │                        │
    │                        │     "d1" not seen       │                        │
    │                        │     in last 5 min       │                        │
    │                        │                        │                        │
    │                        │  2. Assign seq #47     │                        │
    │                        │     for group "X"       │                        │
    │                        │                        │                        │
    │                        │  3. Store durably      │                        │
    │                        │     (multi-AZ)         │                        │
    │                        │                        │                        │
    │◄── 200 OK ─────────────│                        │                        │
    │    MessageId + SeqNum  │                        │                        │
    │                        │                        │                        │
    │                        │  4. Fan-out:           │                        │
    │                        │     Filter eval        │                        │
    │                        │     for each sub       │                        │
    │                        │                        │                        │
    │                        │── SendMessage ────────►│                        │
    │                        │   (group="X",          │  Receives with         │
    │                        │    dedupId="d1",       │  seq order preserved   │
    │                        │    body="placed")      │                        │
    │                        │                        │                        │
    │                        │── SendMessage ─────────│───────────────────────►│
    │                        │   (group="X",          │                        │
    │                        │    dedupId="d1",       │                        │
    │                        │    body="placed")      │                        │
    │                        │                        │                        │

Later: Publisher sends seq #48

    │  Publish(              │                        │                        │
    │   group="X",           │                        │                        │
    │   dedupId="d2",        │                        │                        │
    │   body="shipped")      │                        │                        │
    │───────────────────────►│                        │                        │
    │                        │  Assign seq #48        │                        │
    │◄── 200 OK ─────────────│                        │                        │
    │                        │                        │                        │
    │                        │── SendMessage ────────►│                        │
    │                        │   (group="X",          │  Queue A: "placed"     │
    │                        │    body="shipped")     │  then "shipped" ✓      │
    │                        │                        │                        │
    │                        │── SendMessage ─────────│───────────────────────►│
    │                        │                        │  Queue B: "placed"     │
    │                        │                        │  then "shipped" ✓      │
```

### What the Consumer Sees

```
Consumer polling SQS FIFO Queue A:

    ReceiveMessage(QueueUrl=".../inventory.fifo", MaxNumberOfMessages=10)

    Response:
    [
        {
            "Body": "{\"Type\":\"Notification\",\"Message\":\"placed\",...}",
            "Attributes": {
                "MessageGroupId": "X",
                "SequenceNumber": "...",
                "MessageDeduplicationId": "d1"
            }
        },
        {
            "Body": "{\"Type\":\"Notification\",\"Message\":\"shipped\",...}",
            "Attributes": {
                "MessageGroupId": "X",
                "SequenceNumber": "...",
                "MessageDeduplicationId": "d2"
            }
        }
    ]

    Messages for group "X" are guaranteed in order: placed before shipped.
```

---

## 12. FIFO Internal Architecture

[INFERRED — not officially documented]

### How Ordering Is Maintained

```
                    ┌──────────────────────────────────┐
                    │     FIFO Topic: order-events.fifo  │
                    │                                    │
                    │  Partition Map:                    │
                    │   hash(groupId) → partition        │
                    │                                    │
                    │  ┌────────────┐  ┌────────────┐   │
                    │  │ Partition 1 │  │ Partition 2 │  │
                    │  │             │  │             │  │
                    │  │ Groups:     │  │ Groups:     │  │
                    │  │  order-A    │  │  order-B    │  │
                    │  │  order-C    │  │  order-D    │  │
                    │  │             │  │             │  │
                    │  │ Seq counter │  │ Seq counter │  │
                    │  │ per group   │  │ per group   │  │
                    │  │             │  │             │  │
                    │  │ Dedup store │  │ Dedup store │  │
                    │  │ per group   │  │ per group   │  │
                    │  └────────────┘  └────────────┘   │
                    └──────────────────────────────────┘
```

### Per-Partition Components

| Component | Purpose |
|-----------|---------|
| **Sequence counter** | Assigns monotonically increasing sequence numbers per MessageGroupId |
| **Dedup store** | In-memory hash map of {dedupId → timestamp} per group, TTL 5 minutes |
| **Message log** | Ordered log of messages per group, used for fan-out |
| **Delivery tracker** | Tracks which subscriptions have received each message |

### Why This Limits Throughput

```
For each message in a group, the partition must:
    1. Check dedup store (sub-millisecond, in-memory)
    2. Assign sequence number (atomic increment, single-threaded per group)
    3. Store message (disk write, multi-AZ replication)
    4. Trigger fan-out to all subscriptions (parallel, but ordered per group)

Steps 2 and 3 are inherently serial within a group.
This is why throughput is ~300 msg/sec per group.

Across groups, different partitions process in parallel.
This is why high-throughput mode works: more groups → more partitions → more parallelism.
```

---

## 13. When to Use FIFO vs Standard + Idempotent Consumer

### The Decision Framework

```
                         Need strict ordering?
                         ┌─────┴─────┐
                        Yes          No
                         │            │
                  Need exactly-once?  └─► Standard Topic ✓
                  ┌─────┴─────┐
                 Yes          No
                  │            │
            Fan-out > 100?     │
            ┌─────┴─────┐     │
           Yes          No    Standard Topic +
            │            │    Consumer-side ordering
        Standard       FIFO   (sort by sequence number)
        Topic +        Topic ✓
        Consumer-side
        ordering +
        idempotent consumer
```

### Option A: FIFO Topic

```
Pros:
    ✓ Ordering guaranteed by infrastructure
    ✓ Exactly-once within 5-min window
    ✓ Simple consumer code (no ordering/dedup logic)

Cons:
    ✗ Max 100 subscriptions
    ✗ Only SQS as subscriber (no Lambda, HTTP, email)
    ✗ 300 msg/sec per group
    ✗ Filtering changes to at-most-once
    ✗ Topic name must end with .fifo
```

### Option B: Standard Topic + Idempotent Consumer

```
Pros:
    ✓ 12.5M subscriptions
    ✓ All subscriber types (Lambda, HTTP, email, SMS, push)
    ✓ 30,000 msg/sec per topic
    ✓ Full filtering support
    ✓ No throughput limit per group

Cons:
    ✗ Consumer must implement ordering (sequence numbers + sorting)
    ✗ Consumer must implement deduplication (idempotency key + state store)
    ✗ More complex consumer code
    ✗ Ordering is eventual (not real-time — consumer may see out-of-order then reorder)
```

### How Consumer-Side Ordering Works

```
Publisher adds ordering metadata to standard topic messages:

    Publish(
        TopicArn = "order-events",    ← standard topic
        Message = '{"orderId":"A","status":"shipped"}',
        MessageAttributes = {
            "entity_id": "order-A",
            "sequence_number": "2",
            "event_id": "evt-abc123"   ← for idempotency
        }
    )

Consumer logic:
    1. Receive batch of messages from SQS
    2. Group by entity_id
    3. Sort by sequence_number within each group
    4. For each message, check idempotency key (event_id) against database
    5. If already processed → skip (dedup)
    6. If new → process in sorted order, record event_id

This gives you ordering and dedup at the application level,
with unlimited fan-out and throughput.
```

---

## 14. Common Pitfalls

### Pitfall 1: Forgetting MessageGroupId

```
ERROR:
    Publish to FIFO topic without MessageGroupId
    → SNS returns InvalidParameterException

Fix:
    Always include MessageGroupId.
    If you don't need per-entity ordering, use a constant (but accept the throughput limit).
```

### Pitfall 2: Forgetting MessageDeduplicationId

```
ERROR (ContentBasedDeduplication = false):
    Publish to FIFO topic without MessageDeduplicationId
    → SNS returns InvalidParameterException

Fix:
    Either:
    a) Provide MessageDeduplicationId on every publish
    b) Enable ContentBasedDeduplication on the topic
```

### Pitfall 3: Expecting Cross-Group Ordering

```
WRONG assumption:
    "FIFO means ALL messages are in order"

CORRECT:
    "FIFO means messages within the SAME MessageGroupId are in order"
    Messages across different groups have NO ordering guarantee.
```

### Pitfall 4: Subscribing Non-SQS Endpoints

```
ERROR:
    aws sns subscribe \
        --topic-arn arn:aws:sns:...:my-topic.fifo \
        --protocol https \
        --notification-endpoint https://example.com/webhook
    → SNS returns InvalidParameterException

FIFO topics only support:
    - protocol: sqs (to FIFO or standard SQS queues)
```

### Pitfall 5: Standard DLQ for FIFO Subscription

```
ERROR:
    FIFO topic subscription with RedrivePolicy pointing to a standard SQS DLQ
    → InvalidParameterException

Fix:
    FIFO topic subscription → FIFO SQS DLQ (.fifo suffix)
    Standard topic subscription → Standard SQS DLQ
```

### Pitfall 6: Hot Message Group

```
Problem:
    90% of messages use MessageGroupId = "high-priority"
    This group is limited to 300 msg/sec
    Even with high-throughput mode, this group is the bottleneck

Fix:
    Redesign the group key:
    Instead of "high-priority" → use "high-priority-{orderId}" or similar
    Split the hot group into sub-groups that can be processed independently
    Only do this if ordering between sub-groups isn't required.
```

---

## 15. Cross-References

| Topic | Document |
|-------|----------|
| Fan-out engine internals | [fan-out-engine.md](fan-out-engine.md) |
| Delivery retry policies | [delivery-and-retries.md](delivery-and-retries.md) |
| SNS + SQS fan-out pattern | [sns-sqs-fanout.md](sns-sqs-fanout.md) |
| Message filtering | [message-filtering.md](message-filtering.md) |
| Mobile push delivery | [mobile-push.md](mobile-push.md) |
| Interview simulation | [interview-simulation.md](interview-simulation.md) |

### AWS Documentation References

- [Amazon SNS FIFO topics](https://docs.aws.amazon.com/sns/latest/dg/sns-fifo-topics.html)
- [Amazon SNS FIFO topic code examples](https://docs.aws.amazon.com/sns/latest/dg/fifo-topic-code-examples.html)
- [Amazon SNS quotas](https://docs.aws.amazon.com/general/latest/gr/sns.html)
- [Amazon SQS FIFO queues](https://docs.aws.amazon.com/AWSSimpleQueueService/latest/SQSDeveloperGuide/FIFO-queues.html)
