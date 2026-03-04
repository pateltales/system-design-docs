# Amazon SQS — Delivery Guarantees & Ordering

> Companion deep dive to the [interview simulation](interview-simulation.md). This document explores SQS's delivery semantics, the fundamental tradeoff between throughput, ordering, and exactly-once delivery, and how FIFO queues enforce strict ordering.

---

## Table of Contents

1. [The Three Delivery Semantics](#1-the-three-delivery-semantics)
2. [Standard Queues — At-Least-Once, Best-Effort Ordering](#2-standard-queues--at-least-once-best-effort-ordering)
3. [FIFO Queues — Exactly-Once, Strict Ordering](#3-fifo-queues--exactly-once-strict-ordering)
4. [The Impossibility Triangle](#4-the-impossibility-triangle)
5. [Message Group IDs — The Ordering Primitive](#5-message-group-ids--the-ordering-primitive)
6. [Deduplication Deep Dive](#6-deduplication-deep-dive)
7. [Idempotent Consumer Patterns](#7-idempotent-consumer-patterns)
8. [Ordering Across Multiple Consumers](#8-ordering-across-multiple-consumers)
9. [Standard vs FIFO — Decision Framework](#9-standard-vs-fifo--decision-framework)
10. [Common Pitfalls](#10-common-pitfalls)

---

## 1. The Three Delivery Semantics

Every message queue must choose from three delivery guarantees:

| Semantic | Definition | Implication |
|----------|-----------|-------------|
| **At-most-once** | Message delivered zero or one times. Never duplicated. | Messages can be LOST. If delivery fails, no retry. |
| **At-least-once** | Message delivered one or more times. Never lost. | Messages can be DUPLICATED. Consumer must handle duplicates. |
| **Exactly-once** | Message delivered exactly one time. Never lost, never duplicated. | The gold standard — hardest to implement. |

### 1.1 Why Exactly-Once Is Hard

In a distributed system, exactly-once delivery requires solving two independent problems:

1. **No loss:** The message must be durably stored and retried on delivery failure. This requires: durable storage, retry logic, acknowledgment protocol.

2. **No duplicates:** The system must detect and suppress duplicate deliveries. This requires: unique message identifiers, deduplication state storage, a bounded dedup window.

The challenge is that these two requirements **conflict at the edge**:

```
Producer sends message → Network timeout
  → Did SQS receive it? Unknown.
  → Producer retries (to avoid loss)
  → If SQS already stored it: DUPLICATE
  → If SQS didn't store it: NEEDED

Without dedup ID: at-least-once (may be duplicate)
With dedup ID: exactly-once within the 5-minute window
```

---

## 2. Standard Queues — At-Least-Once, Best-Effort Ordering

### 2.1 At-Least-Once Delivery

Standard queues guarantee that every message will be delivered at least once. The "at least" part means duplicates are possible.

**Why duplicates happen:**

```
Scenario 1: Infrastructure-level duplication
  Message is stored on multiple internal servers (for durability).
  [INFERRED] In rare cases, a ReceiveMessage request might retrieve
  the message from multiple servers before the visibility timeout
  propagates. Result: same message delivered to multiple consumers.

Scenario 2: Visibility timeout race condition
  Consumer A receives message (visibility timeout = 30s)
  Consumer A processes for 31 seconds (1 second over timeout)
  At t=30s: message becomes visible again
  Consumer B receives the same message
  Both Consumer A and B now have the message.

Scenario 3: Delete fails after processing
  Consumer receives and processes message successfully
  DeleteMessage call fails (network error)
  Visibility timeout expires → message redelivered
```

**AWS documentation states:**

> "Amazon SQS stores copies of your messages on multiple servers for redundancy and high availability. On rare occasions, one of the servers that stores a copy of a message might be unavailable when you receive or delete a message. If this occurs, the copy of the message isn't deleted on that unavailable server, and you might get that message copy again when you receive messages."

### 2.2 Best-Effort Ordering

Standard queues provide **approximately FIFO** ordering but do NOT guarantee it.

```
Producer sends: A, B, C, D, E (in this order)

Possible receive orders:
  A, B, C, D, E  ← most likely (approximate FIFO)
  A, C, B, D, E  ← possible (C overtook B)
  A, B, D, C, E  ← possible (D overtook C)
  E, A, B, C, D  ← theoretically possible but unlikely
```

**Why ordering is broken:**

1. **Partitioning:** Messages are distributed across multiple internal servers. Messages on different servers may be received in any order.

2. **Replication lag:** Multi-AZ replication may cause messages to become available at slightly different times on different replicas.

3. **Consumer concurrency:** Multiple consumers polling the same queue receive different subsets of messages from different partitions, with no coordination on order.

[INFERRED — AWS doesn't specify why ordering is broken, but partitioning is the most likely cause]

### 2.3 When At-Least-Once Is Acceptable

Many workloads don't need strict ordering or exactly-once:

| Use Case | Why At-Least-Once Works |
|----------|------------------------|
| Email notifications | Sending the same email twice is annoying but not catastrophic |
| Log ingestion | Duplicate log entries can be filtered downstream |
| Image processing | Processing the same image twice wastes compute but doesn't corrupt data |
| Metrics collection | Duplicate data points can be deduplicated at the aggregation layer |
| Cache invalidation | Invalidating a cache key twice is harmless |

**Rule of thumb:** If processing a message is **idempotent** (same result regardless of how many times you process it), at-least-once is fine.

---

## 3. FIFO Queues — Exactly-Once, Strict Ordering

### 3.1 Exactly-Once Processing

FIFO queues guarantee that:
1. **Each message is delivered exactly once** — no duplicates within the dedup window
2. **Messages within a message group are delivered in strict FIFO order**

**"Exactly-once processing" vs "Exactly-once delivery":**

These terms are often confused. SQS FIFO provides:
- **Exactly-once delivery:** SQS will not deliver duplicate messages to consumers (assuming dedup ID is used correctly)
- **NOT exactly-once processing:** If your consumer crashes AFTER processing the message but BEFORE calling `DeleteMessage`, the message will be redelivered. The processing happened, but SQS doesn't know that.

```
Exactly-once DELIVERY (SQS guarantees):
  SQS → Consumer: Message delivered once (no dup from SQS side)

NOT exactly-once PROCESSING (your responsibility):
  Consumer processes message
  Consumer CRASHES before DeleteMessage
  SQS redelivers message (visibility timeout expired)
  Consumer processes AGAIN — this is duplicate processing

  Your consumer must be idempotent even with FIFO queues.
```

### 3.2 Strict Ordering Within Message Groups

```
Message Group "order-123":
  Sent: [msg-A, msg-B, msg-C] (in this order)

  FIFO guarantee:
  → msg-A is received BEFORE msg-B
  → msg-B is received BEFORE msg-C
  → This order is guaranteed, always, without exception.

  While msg-A is in-flight (received, not deleted):
  → msg-B and msg-C CANNOT be received by any consumer
  → The group is "locked" until msg-A is deleted or visibility timeout expires

  This means:
  → Only ONE message per group can be in-flight at a time
  → This is how ordering is enforced — sequential processing
```

### 3.3 No Ordering Across Groups

```
Message Group "order-123": [A, B]
Message Group "order-456": [X, Y]

Receive order could be:
  [A, X] → then [B, Y]  ← A before B, X before Y ✓
  [X, A] → then [Y, B]  ← X before Y, A before B ✓ (groups interleaved)
  [A, X] → then [B] → then [Y]  ← also valid

Invalid orders:
  [B, A]  ← B before A within same group. NEVER happens.
  [Y, X]  ← Y before X within same group. NEVER happens.
```

---

## 4. The Impossibility Triangle

You can have at most two of three properties simultaneously:

```
                    THROUGHPUT
                   (unlimited)
                      ╱╲
                     ╱  ╲
                    ╱    ╲
                   ╱      ╲
    Standard SQS  ╱        ╲  ???
   (at-least-    ╱          ╲ (does not
    once, un-   ╱     SQS    ╲  exist)
    ordered)   ╱   can have   ╲
              ╱   any 2 of 3   ╲
             ╱                  ╲
            ╱────────────────────╲
      ORDERING              EXACTLY-ONCE
      (strict FIFO)         (no duplicates)
           ╲                    ╱
            ╲     FIFO SQS    ╱
             ╲  (ordered +   ╱
              ╲  exactly-   ╱
               ╲  once,    ╱
                ╲ limited ╱
                 ╲ 300/s)╱
                  ╲    ╱
                   ╲╱
```

| Choice | What You Get | What You Sacrifice | Example |
|--------|-------------|-------------------|---------|
| Standard SQS | Unlimited throughput | Ordering + exactly-once | High-volume log ingestion |
| FIFO SQS | Ordering + exactly-once | Throughput (capped at 300-70K msg/s) | Financial transactions |
| ??? | Throughput + exactly-once | Ordering | Doesn't exist in SQS (some event streaming systems approximate this) |

### 4.1 Why This Triangle Exists

**Throughput requires partitioning.** To handle millions of messages per second, the queue must be split across many servers. But partitioning destroys global ordering — messages on different servers arrive at different times.

**Ordering requires serialization.** To guarantee FIFO, messages must be processed one at a time (within a group). Serialization is inherently limited — you can only process one message per group per visibility-timeout period.

**Exactly-once requires state.** To detect duplicates, you must remember every message ID you've seen. This state grows with throughput and must be consistent across replicas. At high throughput, maintaining this state becomes the bottleneck.

### 4.2 How FIFO Queues Navigate the Triangle

FIFO queues achieve ordering + exactly-once by sacrificing throughput, but they're clever about minimizing the impact:

1. **Message Group IDs partition the ordering constraint.** You don't need global ordering — you need per-entity ordering. Each message group is independently ordered, allowing parallelism across groups.

2. **High-throughput mode relaxes the throughput constraint.** By partitioning dedup and throughput limits per message group (`FifoThroughputLimit=perMessageGroupId`), FIFO queues can reach 70,000 msg/s — IF you have enough distinct message group IDs.

3. **5-minute dedup window bounds the state.** Instead of remembering every message ID forever, SQS only keeps dedup state for 5 minutes. This bounds memory/storage usage at the cost of not catching duplicates older than 5 minutes.

---

## 5. Message Group IDs — The Ordering Primitive

### 5.1 How Message Group IDs Work

The `MessageGroupId` is the unit of ordering in FIFO queues. Think of it as "which entity does this message belong to?"

```
Each MessageGroupId = an independent ordered stream

Queue with 3 message groups:
  ┌─────────────────────┐
  │ Group "user-A"      │ → [msg1, msg2, msg3] (strict order)
  │ Group "user-B"      │ → [msg4, msg5]       (strict order)
  │ Group "order-789"   │ → [msg6, msg7, msg8]  (strict order)
  └─────────────────────┘

  user-A's messages are ALWAYS processed in order: msg1 → msg2 → msg3
  user-B's messages are independent of user-A
  order-789's messages are independent of both
```

### 5.2 Choosing Message Group IDs

The message group ID should be the **entity that needs ordered processing:**

| Domain | Message Group ID | Why |
|--------|-----------------|-----|
| E-commerce orders | Order ID (`ORD-12345`) | Each order's events must be in sequence (created → paid → shipped) |
| User actions | User ID (`user-789`) | Each user's actions should be processed in order |
| IoT devices | Device ID (`sensor-42`) | Each device's readings should be in order |
| Bank accounts | Account ID (`acct-001`) | Transactions on the same account must be ordered |
| Chat rooms | Room ID (`room-general`) | Messages in the same room must be in order |

### 5.3 The Single-Group Anti-Pattern

```
BAD: All messages use MessageGroupId = "all"
  → Entire queue is a single ordered stream
  → Only ONE message can be in-flight at a time
  → Throughput = 1 message per visibility timeout
  → If timeout = 30s: throughput = 2 messages/minute = 120/hour

GOOD: Each entity has its own MessageGroupId
  → 1,000 entities = 1,000 independent streams
  → Up to 1,000 messages in-flight simultaneously
  → Throughput = 1,000 messages per visibility timeout period
  → 1,000 consumers can process in parallel
```

### 5.4 How Many Message Group IDs?

The number of distinct message group IDs directly determines your parallelism:

| # of Distinct Groups | Max Parallelism | Effective Throughput |
|---------------------|-----------------|---------------------|
| 1 | 1 message at a time | ~2/minute (if timeout=30s) |
| 10 | 10 concurrent | ~20/minute |
| 100 | 100 concurrent | ~200/minute |
| 1,000 | 1,000 concurrent | ~2,000/minute |
| 100,000 | 100,000 concurrent | Limited by FIFO API rate (70K msg/s with high throughput) |

---

## 6. Deduplication Deep Dive

### 6.1 The Dedup Window

```
┌──────────────────────────── 5 minute window ────────────────────────────┐
│                                                                          │
│  T=0        T=30s       T=60s       T=120s      T=299s     T=300s      │
│  │           │           │           │           │           │          │
│  Send(A,     │           │           │           │           │          │
│  dedup=X)    │           │           │           │           │          │
│  ✓ stored    │           │           │           │           │          │
│              │           │           │           │           │          │
│              Send(B,     │           │           │           │          │
│              dedup=X)    │           │           │           │          │
│              ✗ DROPPED   │           │           │           │          │
│              (duplicate) │           │           │           │          │
│                          │           │           │           │          │
│                          Send(C,     │           │           │          │
│                          dedup=Y)    │           │           │          │
│                          ✓ stored    │           │           │          │
│                          (different  │           │           │          │
│                           dedup ID)  │           │           │          │
│                                      │           │           │          │
│                                      │           │           │          │
│                                      │           │           │          │
│                                      │           │           DEDUP      │
│                                      │           │           EXPIRED    │
│                                      │           │           for X      │
│                                      │           │                      │
│                                      │           │           Send(D,    │
│                                      │           │           dedup=X)   │
│                                      │           │           ✓ stored   │
│                                      │           │           (X expired,│
│                                      │           │            treated   │
│                                      │           │            as new)   │
└──────────────────────────────────────────────────────────────────────────┘
```

### 6.2 Content-Based vs Explicit Dedup

| Method | How It Works | Pros | Cons |
|--------|-------------|------|------|
| **Content-based** (`ContentBasedDeduplication=true`) | SHA-256(message body) = dedup ID | No client-side logic needed. Simple. | Same body = same dedup ID, even if semantically different. Attributes are NOT hashed. |
| **Explicit** (`MessageDeduplicationId` on SendMessage) | Client provides a unique dedup ID | Full control. Can use business keys (order ID, txn ID). | Client must generate meaningful dedup IDs. |

### 6.3 Edge Cases

**Edge Case 1: Same body, different meaning**
```
Content-based dedup enabled:
  Send: {"action": "heartbeat"}  → stored, dedup=SHA256("heartbeat")
  Send: {"action": "heartbeat"}  → DROPPED (same body = same hash)

But these are two separate heartbeats! Content-based dedup is wrong here.
Solution: Use explicit dedup ID (e.g., "heartbeat-{timestamp}")
```

**Edge Case 2: Different body, same dedup ID**
```
Explicit dedup:
  Send: body="Order v1", dedupId="order-123"  → stored
  Send: body="Order v2", dedupId="order-123"  → DROPPED (same dedup ID)

The second send is treated as a retry of the first, even though the body is different.
The dedup ID takes precedence over the body content.
```

**Edge Case 3: Dedup window expiry**
```
T=0: Send body="data", dedupId="msg-001"  → stored
T=6min: Send body="data", dedupId="msg-001"  → STORED (dedup expired!)

Now there are TWO messages with the same dedup ID in the queue.
The 5-minute window only prevents SEND-TIME duplicates.
It does NOT retroactively deduplicate existing messages.
```

### 6.4 Dedup Scope: Queue vs Message Group

With high-throughput mode:

| `DeduplicationScope` | Behavior | Impact |
|---------------------|----------|--------|
| `queue` (default) | Dedup ID checked across ALL message groups | If group-A and group-B both send dedup="X", the second is dropped |
| `messageGroup` | Dedup ID checked only within the same group | group-A dedup="X" and group-B dedup="X" are both stored (different groups) |

**`messageGroup` scope is required for high-throughput mode.** It allows SQS to partition dedup state by message group, enabling independent scaling.

---

## 7. Idempotent Consumer Patterns

Even with FIFO queues, consumers must be idempotent because:
- Consumer can crash after processing but before `DeleteMessage`
- `ReceiveMessage` can timeout and the message is redelivered
- SQS's exactly-once is at the DELIVERY level, not PROCESSING level

### 7.1 Database Idempotency Key

```
Consumer receives message:
  {orderId: "ORD-789", action: "charge", amount: 99.99}

Before processing:
  SELECT * FROM processed_messages WHERE message_id = 'msg-uuid-123';
  If found → skip (already processed)
  If not found → process

After processing:
  INSERT INTO processed_messages (message_id, processed_at) VALUES ('msg-uuid-123', NOW());
  DELETE from SQS

The INSERT must be in the SAME transaction as the business logic:
  BEGIN;
    INSERT INTO processed_messages (...);
    UPDATE accounts SET balance = balance - 99.99 WHERE id = 'ORD-789';
  COMMIT;
  DeleteMessage(receipt_handle);
```

### 7.2 Conditional Write (Optimistic)

```
DynamoDB example:
  PutItem(
    TableName: "orders",
    Item: {orderId: "ORD-789", status: "CHARGED", amount: 99.99},
    ConditionExpression: "attribute_not_exists(orderId) OR status <> 'CHARGED'"
  )

If the item already exists with status=CHARGED:
  → ConditionCheckFailedException → skip (already processed)
If the item doesn't exist or has different status:
  → Write succeeds → proceed
```

### 7.3 Idempotency with Side Effects

```
Non-idempotent operations:
  ✗ Increment counter: counter += 1 (each call adds 1)
  ✗ Send email (each call sends another email)
  ✗ Charge credit card (each call charges again)

Make them idempotent:
  ✓ Set counter to specific value: counter = 42
  ✓ Send email with dedup key: if not already sent for this order
  ✓ Charge with idempotency key: Stripe's Idempotency-Key header
```

### 7.4 The "Process + Delete" Atomicity Problem

```
The fundamental problem:
  1. Process message (e.g., write to database)     ← can succeed
  2. DeleteMessage from SQS                         ← can fail

These are TWO separate systems. There's no distributed transaction between
your database and SQS. If step 1 succeeds and step 2 fails:
  → Message is redelivered
  → Consumer must detect the duplicate (idempotency)

This is unavoidable. Even with FIFO exactly-once delivery, the
process+delete sequence is NOT atomic. Idempotent consumers are required.
```

---

## 8. Ordering Across Multiple Consumers

### 8.1 Standard Queues: No Ordering Guarantee

With multiple consumers on a standard queue, ordering is essentially random:

```
Producer sends: [1, 2, 3, 4, 5]

Consumer A receives: [1, 3]   (from partition X)
Consumer B receives: [2, 4]   (from partition Y)
Consumer C receives: [5]      (from partition Z)

Processing order: 1, 2, 3, 5, 4  (depends on consumer speed)
```

### 8.2 FIFO Queues: Per-Group Sequential

With FIFO queues and multiple consumers:

```
Group "user-A": [A1, A2, A3]
Group "user-B": [B1, B2]

Consumer X receives: [A1]     → group "user-A" is now LOCKED
Consumer Y receives: [B1]     → group "user-B" is now LOCKED

Consumer X finishes A1, deletes it → group "user-A" unlocks
Consumer X receives: [A2]     → processes in order ✓

Consumer Y finishes B1, deletes it → group "user-B" unlocks
Consumer Y receives: [B2]     → processes in order ✓

Result:
  user-A's messages: A1 → A2 → A3 (strict order ✓)
  user-B's messages: B1 → B2 (strict order ✓)
  Cross-group: A1 and B1 may process in any relative order (OK)
```

### 8.3 Scaling Consumers with FIFO

The number of useful consumers for a FIFO queue is limited by the number of active message groups:

```
1 message group  → 1 useful consumer (others idle)
10 message groups → up to 10 useful consumers
1000 message groups → up to 1000 useful consumers (limited by FIFO throughput)
```

Adding more consumers than active message groups provides no benefit — the extra consumers will receive empty responses because all groups are locked.

---

## 9. Standard vs FIFO — Decision Framework

### 9.1 Decision Matrix

| Question | If Yes → Standard | If Yes → FIFO |
|----------|-------------------|---------------|
| Do I need > 70,000 msg/s? | ✓ Standard | — |
| Must messages be processed in exact order? | — | ✓ FIFO |
| Is duplicate processing catastrophic? | — | ✓ FIFO (but still need idempotent consumers) |
| Can my consumer handle duplicates? | ✓ Standard | — |
| Do I need per-message delay? | ✓ Standard | — (FIFO only supports queue-level delay) |
| Is cost a primary concern? | ✓ Standard (cheaper) | — |
| Do I use SNS → SQS fan-out? | Both work | FIFO topic → FIFO queue |

### 9.2 Cost Comparison

| Dimension | Standard | FIFO |
|-----------|----------|------|
| API request pricing | $0.40 per million requests (first 1M free/month) | $0.50 per million requests (higher) |
| Throughput | Unlimited (no cost ceiling from throttling) | Capped — may need multiple queues for higher throughput |
| Batch benefit | Cost savings from fewer API calls | Same + throughput multiplier |

### 9.3 Migration: Standard → FIFO

You cannot convert a standard queue to FIFO or vice versa. You must:

1. Create a new FIFO queue (name must end with `.fifo`)
2. Update producers to include `MessageGroupId` and optionally `MessageDeduplicationId`
3. Update consumers to handle the stricter ordering semantics
4. Switch traffic from old queue to new queue
5. Drain the old queue, then delete it

---

## 10. Common Pitfalls

### 10.1 Pitfall: Assuming Standard Queue Is Ordered

```
WRONG assumption:
  "I send messages A, B, C and receive them in that order"

REALITY:
  Standard queues are APPROXIMATELY FIFO.
  You might receive B, C, A or A, C, B.
  If order matters, use FIFO.
```

### 10.2 Pitfall: Assuming FIFO = No Duplicate Processing

```
WRONG assumption:
  "FIFO queues guarantee exactly-once processing, so my consumer
   doesn't need to handle duplicates"

REALITY:
  FIFO guarantees exactly-once DELIVERY from SQS.
  If your consumer crashes after processing but before DeleteMessage,
  the message is redelivered. Your processing ran twice.
  You MUST still be idempotent.
```

### 10.3 Pitfall: Single Message Group ID on FIFO

```
WRONG:
  All messages use MessageGroupId = "default"
  → Queue is fully serialized
  → Throughput = 1 message per visibility timeout
  → Adding more consumers doesn't help

RIGHT:
  MessageGroupId = entity_id (order ID, user ID, etc.)
  → Parallelism scales with number of distinct entities
```

### 10.4 Pitfall: Dedup ID Collision Across Groups

```
With DeduplicationScope = "queue" (default):

  Group "user-A": Send(dedupId="001", body="payment")  → stored ✓
  Group "user-B": Send(dedupId="001", body="refund")   → DROPPED ✗ (dedup collision!)

  Different groups, different meanings, but same dedup ID → collision.

Fix: Use DeduplicationScope = "messageGroup" or prefix dedup IDs
  with the group ID: "user-A:001", "user-B:001"
```

### 10.5 Pitfall: Relying on Dedup Window for Business Logic

```
WRONG:
  "We use FIFO dedup to prevent double-charging customers.
   The 5-minute window is enough."

REALITY:
  What if the same charge is retried 6 minutes later?
  → Dedup window expired → SQS treats it as a new message
  → Customer charged twice

  The 5-minute window covers NETWORK RETRIES, not business-level dedup.
  Business-level dedup must be in your application (idempotency key in DB).
```

### 10.6 Pitfall: Ignoring the ReceiptHandle Problem

```
Consumer A receives message → ReceiptHandle_A
Consumer A processes slowly (exceeds visibility timeout)
Consumer B receives same message → ReceiptHandle_B

Consumer A calls DeleteMessage(ReceiptHandle_A)
  → HTTP 200 (success!)
  → But the message may NOT be deleted because ReceiptHandle_A
    corresponds to an expired receive.

Consumer B finishes processing → calls DeleteMessage(ReceiptHandle_B)
  → This is the definitive delete.

LESSON: Always extend visibility timeout (heartbeat) before it expires
  if processing takes longer than expected.
```

---

## Summary: Delivery Guarantees at a Glance

| Property | Standard Queue | FIFO Queue |
|----------|---------------|------------|
| **Delivery** | At-least-once (duplicates possible) | Exactly-once delivery (within 5-min dedup window) |
| **Ordering** | Best-effort (approximately FIFO) | Strict FIFO within MessageGroupId |
| **Throughput** | Virtually unlimited | 300 API calls/s (default), 3,000 msg/s (batching), 70,000 msg/s (high throughput) |
| **Deduplication** | None — each send creates a new message | 5-minute window via MessageDeduplicationId or content hash |
| **Consumer requirement** | MUST be idempotent | MUST be idempotent (exactly-once is at delivery level, not processing level) |
| **Parallelism** | Unlimited consumers | Limited by # of distinct MessageGroupIds |
| **Per-message delay** | Yes (0-900 seconds) | No (queue-level only) |
| **Cost** | $0.40/million requests | $0.50/million requests |
