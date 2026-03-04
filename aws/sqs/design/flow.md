# Amazon SQS — System Flows

> Companion deep dive to the [interview simulation](interview-simulation.md). This document traces 10 end-to-end system flows through SQS's architecture, from producer request to consumer acknowledgment.

---

## Table of Contents

1. [SendMessage Flow (Standard Queue)](#1-sendmessage-flow-standard-queue)
2. [ReceiveMessage + Visibility Timeout Flow](#2-receivemessage--visibility-timeout-flow)
3. [DeleteMessage Flow (Successful Processing)](#3-deletemessage-flow-successful-processing)
4. [Consumer Failure & Redelivery Flow](#4-consumer-failure--redelivery-flow)
5. [Dead Letter Queue Flow](#5-dead-letter-queue-flow)
6. [DLQ Redrive Flow (StartMessageMoveTask)](#6-dlq-redrive-flow-startmessagemovetask)
7. [FIFO Send + Deduplication Flow](#7-fifo-send--deduplication-flow)
8. [FIFO Receive + Message Group Ordering Flow](#8-fifo-receive--message-group-ordering-flow)
9. [Long Polling Flow](#9-long-polling-flow)
10. [Delay Queue Flow](#10-delay-queue-flow)
11. [Batch Send/Receive Flow](#11-batch-sendreceive-flow)
12. [Complete Message Lifecycle (End-to-End)](#12-complete-message-lifecycle-end-to-end)
13. [Flow Summary Table](#13-flow-summary-table)

---

## 1. SendMessage Flow (Standard Queue)

The SendMessage flow is the foundational write path for SQS. A single SendMessage request stores a message durably across multiple AZs before returning success.

### 1.1 Sequence Diagram

```
Producer              SQS Front-End         Auth/IAM           KMS                Storage Nodes
  |                      |                    |                  |                  (Multi-AZ)
  |                      |                    |                  |                      |
  |--POST SendMessage--->|                    |                  |                      |
  |  (QueueUrl,          |                    |                  |                      |
  |   MessageBody,       |                    |                  |                      |
  |   Attributes)        |                    |                  |                      |
  |                      |                    |                  |                      |
  |                      |--verify SigV4----->|                  |                      |
  |                      |  (check signature, |                  |                      |
  |                      |   clock skew)      |                  |                      |
  |                      |<--auth result------|                  |                      |
  |                      |                    |                  |                      |
  |                      |--check IAM-------->|                  |                      |
  |                      |  (sqs:SendMessage  |                  |                      |
  |                      |   on queue ARN)    |                  |                      |
  |                      |<--allowed----------|                  |                      |
  |                      |                    |                  |                      |
  |                      |--validate message--|                  |                      |
  |                      |  (size ≤ 256KB,    |                  |                      |
  |                      |   valid unicode,   |                  |                      |
  |                      |   ≤ 10 attributes) |                  |                      |
  |                      |                    |                  |                      |
  |                      |  [If SSE-KMS]      |                  |                      |
  |                      |--GenerateDataKey---|----------------->|                      |
  |                      |  (KMS key ID)      |                  |                      |
  |                      |<--data key---------|------------------|                      |
  |                      |--encrypt body------|                  |                      |
  |                      |                    |                  |                      |
  |                      |--compute MD5-------|                  |                      |
  |                      |  (body + attrs)    |                  |                      |
  |                      |                    |                  |                      |
  |                      |--store message-----|------------------|---write to AZ-1----->|
  |                      |                    |                  |---write to AZ-2----->|
  |                      |                    |                  |---write to AZ-3----->|
  |                      |                    |                  |                      |
  |                      |                    |                  |<--ACK from AZ-1------|
  |                      |                    |                  |<--ACK from AZ-2------|
  |                      |                    |                  |  (wait for           |
  |                      |                    |                  |   sufficient ACKs)   |
  |                      |                    |                  |                      |
  |                      |--assign MessageId--|                  |                      |
  |                      |  (UUID)            |                  |                      |
  |                      |                    |                  |                      |
  |<--200 OK ------------|                    |                  |                      |
  |  (MessageId,         |                    |                  |                      |
  |   MD5OfBody,         |                    |                  |                      |
  |   MD5OfAttributes)   |                    |                  |                      |
```

### 1.2 Step-by-Step with Latencies

| Step | Operation | Latency | Details |
|------|-----------|---------|---------|
| 1 | Producer sends POST over HTTPS | ~1-5ms | TLS handshake if new connection. SQS endpoint is regional. SDK reuses connections. Message body is in the JSON request body. |
| 2 | SigV4 authentication | ~0.5ms | Verify signature, check timestamp within 15-min window. Reject with `InvalidSecurity` if not HTTPS or unsigned. |
| 3 | IAM authorization | ~1ms | Check caller has `sqs:SendMessage` on the queue ARN. Check queue resource policy if cross-account. If SSE-KMS: also check `kms:GenerateDataKey`. |
| 4 | Message validation | ~0.1ms | Body size ≤ 256 KB, valid unicode characters, ≤ 10 message attributes, attribute names valid. FIFO: `MessageGroupId` must be present. |
| 5 | KMS encryption (if SSE-KMS) | ~5-20ms | Call KMS `GenerateDataKey`. KMS returns plaintext + encrypted data key. Encrypt message body with plaintext key. Store encrypted data key alongside message. This is the most expensive step — can be cached via `KmsDataKeyReusePeriodSeconds` (default 300s). |
| 6 | Compute MD5 checksums | ~0.1ms | MD5 of body, MD5 of message attributes. These go in the response for client-side integrity verification. |
| 7 | Store message across AZs | ~5-15ms | Write message to storage nodes in multiple AZs. SQS stores messages "redundantly across multiple AZs." [INFERRED — AWS doesn't publish whether it's 2 or 3 AZs, or the replication protocol. Likely synchronous replication to at least 2 AZs before returning.] |
| 8 | Wait for durability confirmation | Included above | The API blocks until sufficient ACKs are received. Once `SendMessage` returns HTTP 200, the message is durable. |
| 9 | Assign MessageId and return | ~0.1ms | Generate UUID as `MessageId`. FIFO: also assign monotonically increasing `SequenceNumber` within the `MessageGroupId`. Return response. |

### 1.3 Latency Summary

- **Typical (no encryption):** ~5-20ms end-to-end
- **With SSE-KMS (first call, no cached key):** ~20-50ms (KMS call dominates)
- **With SSE-KMS (cached key):** ~5-20ms (no KMS call needed)
- **With SSE-SQS:** ~5-20ms (encryption handled internally, no external call)

### 1.4 Failure Handling

| Failure | Response | Recovery |
|---------|----------|----------|
| Auth failure (bad signature) | `400 InvalidSecurity` | Client fixes credentials |
| Permission denied | `400 AccessDenied` | Add `sqs:SendMessage` to IAM policy or queue policy |
| Message too large (> 256 KB) | `400 InvalidMessageContents` | Use SQS Extended Client Library (stores body in S3) |
| Invalid unicode characters | `400 InvalidMessageContents` | Encode binary as base64, put in Binary message attribute |
| Queue doesn't exist | `400 QueueDoesNotExist` | Check queue URL, verify region |
| KMS permission denied | `400 KmsAccessDenied` | Add `kms:GenerateDataKey` to caller's policy |
| KMS throttled | `400 KmsThrottled` | Backoff. Increase `KmsDataKeyReusePeriodSeconds` or switch to SSE-SQS |
| Storage node failure | Transparent | SQS retries internally to other nodes. Client doesn't see this. |
| All storage nodes fail | `500 InternalError` | Client retries with exponential backoff |

### 1.5 Durability Guarantee

**Once SendMessage returns HTTP 200, the message WILL NOT BE LOST.** It is durably stored across multiple AZs. Even if an entire AZ goes down, the message survives. This is a hard guarantee — SQS does not return success until replication is confirmed.

**What can still lose the message after it's stored:**
- Message retention period expires (default 4 days, max 14 days) — auto-deleted
- PurgeQueue is called — all messages deleted
- DeleteQueue is called — queue and messages deleted
- Consumer receives and deletes it (normal processing)

None of these are "loss" — they're intentional removal.

---

## 2. ReceiveMessage + Visibility Timeout Flow

The ReceiveMessage flow is SQS's most complex operation — it combines message retrieval with state transition (visible → invisible).

### 2.1 Sequence Diagram

```
Consumer              SQS Front-End         Storage Nodes         Visibility Timer
  |                      |                      |                      |
  |--POST ReceiveMessage>|                      |                      |
  |  (MaxNumber=10,      |                      |                      |
  |   WaitTime=20,       |                      |                      |
  |   VisTimeout=60)     |                      |                      |
  |                      |                      |                      |
  |                      |--authenticate------->|                      |
  |                      |  (SigV4 + IAM)       |                      |
  |                      |                      |                      |
  |               [SHORT POLL: WaitTimeSeconds=0]                      |
  |                      |--query SUBSET of---->|                      |
  |                      |  storage partitions  |                      |
  |                      |  (may miss messages  |                      |
  |                      |   on unqueried       |                      |
  |                      |   partitions)        |                      |
  |                      |                      |                      |
  |               [LONG POLL: WaitTimeSeconds>0]                       |
  |                      |--query ALL ---------->|                      |
  |                      |  storage partitions  |                      |
  |                      |                      |                      |
  |               [If no messages and long poll:]                      |
  |                      |  ...hold connection...|                      |
  |                      |  (up to WaitTime sec) |                      |
  |                      |                      |                      |
  |               [Messages found:]             |                      |
  |                      |<--messages-----------|                      |
  |                      |                      |                      |
  |                      |--set visibility------|--------------------->|
  |                      |  timeout for each    |  start countdown     |
  |                      |  message             |  (60 seconds in      |
  |                      |                      |   this example)      |
  |                      |                      |                      |
  |                      |--generate unique-----|                      |
  |                      |  ReceiptHandle for   |                      |
  |                      |  each message        |                      |
  |                      |                      |                      |
  |                      |  [If SSE-KMS]        |                      |
  |                      |--KMS Decrypt-------->|                      |
  |                      |  (decrypt body)      |                      |
  |                      |                      |                      |
  |<--200 OK ------------|                      |                      |
  |  (Messages array:    |                      |                      |
  |   MessageId,         |                      |                      |
  |   ReceiptHandle,     |                      |                      |
  |   Body, Attributes)  |                      |                      |
  |                      |                      |                      |
  |                      |                      |   [60 seconds pass   |
  |                      |                      |    without delete]   |
  |                      |                      |                      |
  |                      |                      |   message becomes    |
  |                      |                      |   VISIBLE again      |
```

### 2.2 Step-by-Step

| Step | Operation | Latency | Details |
|------|-----------|---------|---------|
| 1 | Consumer sends ReceiveMessage | ~1-5ms | SDK typically sets `WaitTimeSeconds=20` and `MaxNumberOfMessages=10` for efficiency. |
| 2 | Authentication + authorization | ~1.5ms | Verify SigV4, check `sqs:ReceiveMessage` permission. If SSE-KMS: also need `kms:Decrypt`. |
| 3a | Short poll: query subset | ~1-5ms | SQS queries a random subset of internal storage partitions. May return 0 messages even if messages exist on other partitions. Returns immediately. |
| 3b | Long poll: query all partitions | ~1-20,000ms | Queries all partitions. If messages found: returns immediately (~1-5ms). If no messages: holds connection for up to `WaitTimeSeconds`. Returns as soon as any message arrives, or when timeout expires. |
| 4 | Set visibility timeout | ~0.5ms | For each selected message, mark as "in-flight." Start countdown timer. If `VisibilityTimeout` specified in request, use that; otherwise use queue default (30s). |
| 5 | Generate ReceiptHandle | ~0.1ms | Each message gets a unique `ReceiptHandle` tied to this specific receipt. If the same message is received again later (after visibility timeout), it gets a DIFFERENT `ReceiptHandle`. |
| 6 | Decrypt (if SSE-KMS) | ~5-20ms | Call KMS `Decrypt` to get the plaintext data key, then decrypt message body. Cached if within `KmsDataKeyReusePeriodSeconds`. |
| 7 | Return messages | ~0.1ms | Return up to `MaxNumberOfMessages` messages. Standard queue: messages may come from different partitions, ordering is best-effort. FIFO: messages from the same `MessageGroupId` are in strict order. |

### 2.3 Visibility Timeout Timeline

```
Time ────────────────────────────────────────────────────────────────>

 0s         Consumer A receives msg        60s          Message visible
 |          ReceiptHandle = RH_A           |            again
 |                                         |
 ├─────────── INVISIBLE ──────────────────►├────── VISIBLE ──────────>
 |                                         |
 |  Consumer A processes...                |  Consumer B (or A) can
 |                                         |  receive it now with a
 |  If Consumer A calls                    |  NEW ReceiptHandle = RH_B
 |  DeleteMessage(RH_A) before 60s:        |
 |  → Message permanently deleted ✓        |  RH_A is now stale.
 |                                         |  DeleteMessage(RH_A) may
 |  If Consumer A calls                    |  succeed but has ambiguous
 |  ChangeMessageVisibility(RH_A, 120)     |  behavior.
 |  at 45s:                                |
 |  → Timeout extends to 45+120 = 165s    |
 |                                         |
```

### 2.4 What Can Go Wrong

| Scenario | What Happens | Consequence |
|----------|--------------|-------------|
| Consumer processes before timeout | Calls `DeleteMessage` → message removed permanently | Happy path ✓ |
| Consumer crashes during processing | Visibility timeout expires → message becomes visible → another consumer receives it | Message is processed by a different consumer. At-least-once delivery. |
| Processing takes longer than timeout | Message becomes visible while first consumer still processing. Second consumer receives it. Now two consumers process the same message. | **Duplicate processing.** This is why consumers MUST be idempotent. |
| Consumer finishes after timeout | Calls `DeleteMessage` with old `ReceiptHandle`. May succeed (HTTP 200) but behavior is ambiguous — message may have already been re-received. | Potentially lost message (deleted after another consumer received it). |
| In-flight limit reached (~120K) | `ReceiveMessage` returns `OverLimit` error | Scale up consumers to delete messages faster |

### 2.5 The "Empty Response Even When Messages Exist" Problem

With **short polling** (`WaitTimeSeconds=0`), SQS queries a subset of its internal servers:

```
Standard queue with 4 internal partitions:

  Partition A: [msg-1, msg-2]     Partition C: [msg-5]
  Partition B: [msg-3, msg-4]     Partition D: []

Short poll query hits Partition C and D:
  → Returns: [msg-5]  (missed msg-1 through msg-4)

Short poll query hits Partition D only:
  → Returns: []  (0 messages — even though 5 messages exist!)
```

**Why SQS does this:** [INFERRED] Standard queues support "virtually unlimited" throughput. To achieve this, messages are distributed across many partitions. Querying all partitions on every request would be expensive and would limit throughput. Short polling trades completeness for speed.

**Fix:** Use long polling (`WaitTimeSeconds=20`). It queries all partitions and waits for messages.

---

## 3. DeleteMessage Flow (Successful Processing)

The simplest flow, but critical — without deletion, messages are redelivered.

### 3.1 Sequence Diagram

```
Consumer              SQS Front-End         Storage Nodes
  |                      |                      |
  |--POST DeleteMessage->|                      |
  |  (QueueUrl,          |                      |
  |   ReceiptHandle)     |                      |
  |                      |                      |
  |                      |--authenticate------->|
  |                      |--authorize---------->|
  |                      |  (sqs:DeleteMessage) |
  |                      |                      |
  |                      |--validate----------->|
  |                      |  ReceiptHandle       |
  |                      |                      |
  |                      |--mark deleted------->|
  |                      |  across AZs          |
  |                      |<--ACK----------------|
  |                      |                      |
  |<--200 OK ------------|                      |
  |  (empty body)        |                      |
```

### 3.2 Key Points

- **Deletion is permanent.** There's no undo, no recycle bin.
- **Deletion is idempotent.** Deleting an already-deleted message returns HTTP 200 (no error). Safe to retry.
- **ReceiptHandle must be current.** If the visibility timeout expired and another consumer received the message, your `ReceiptHandle` is stale. The delete may succeed (HTTP 200) but might not actually remove the message from the other consumer's perspective. [INFERRED]
- **Latency:** ~2-10ms typical. Just auth + a write to mark the message as deleted.
- **No response data.** Unlike `SendMessage`, `DeleteMessage` returns an empty body. There's nothing to confirm beyond the HTTP 200 status.

### 3.3 When Delete Fails

| Failure | Error Code | What It Means |
|---------|-----------|---------------|
| Invalid receipt handle format | `ReceiptHandleIsInvalid` | Corrupted or truncated handle |
| Receipt handle expired | No error — HTTP 200 | The message may or may not be actually deleted. Ambiguous. |
| Queue doesn't exist | `QueueDoesNotExist` | Queue was deleted |
| Permission denied | `AccessDenied` | Missing `sqs:DeleteMessage` permission |

---

## 4. Consumer Failure & Redelivery Flow

This is the flow that makes SQS "at-least-once" — when processing fails, the message is automatically redelivered.

### 4.1 Sequence Diagram

```
Consumer A            SQS                    Consumer B
  |                    |                         |
  |--ReceiveMessage--->|                         |
  |<--msg (RH_A)-------|                         |
  |                    |                         |
  |  [Processing...]   |                         |
  |  [CRASH! / OOM /   |                         |
  |   network error]   |                         |
  |                    |                         |
  |  (no DeleteMessage |                         |
  |   ever sent)       |                         |
  |                    |                         |
  |                    |  [Visibility timeout     |
  |                    |   expires (30s default)] |
  |                    |                         |
  |                    |  [Message becomes        |
  |                    |   VISIBLE again]         |
  |                    |                         |
  |                    |<--ReceiveMessage---------|
  |                    |---msg (RH_B)----------->|
  |                    |                         |
  |                    |   [Consumer B processes  |
  |                    |    successfully]         |
  |                    |                         |
  |                    |<--DeleteMessage(RH_B)----|
  |                    |---200 OK--------------->|
  |                    |                         |
  |                    |   [Message permanently   |
  |                    |    deleted]              |
```

### 4.2 ApproximateReceiveCount Progression

Each time a message is received, `ApproximateReceiveCount` increments:

```
Send:     ApproximateReceiveCount = 0 (not yet received)
Receive:  ApproximateReceiveCount = 1
  → Consumer crashes
  → Visibility timeout expires
Receive:  ApproximateReceiveCount = 2
  → Consumer throws exception, doesn't delete
  → Visibility timeout expires
Receive:  ApproximateReceiveCount = 3
  → Consumer crashes again
  → Visibility timeout expires
Receive:  ApproximateReceiveCount = 4
  → Still failing

If maxReceiveCount = 5 in redrive policy:
Receive:  ApproximateReceiveCount = 5
  → Consumer fails again
  → On next receive attempt: MESSAGE MOVED TO DLQ
```

### 4.3 The Poison Message Problem

Some messages are inherently unprocessable (malformed JSON, references a deleted resource, triggers a bug). Without a DLQ, a poison message creates an infinite loop:

```
receive → fail → visibility timeout → receive → fail → ... (forever)
```

The message consumes receive capacity, wastes compute, and never gets resolved. This is why **every production queue should have a DLQ** with a reasonable `maxReceiveCount` (typically 3-5).

---

## 5. Dead Letter Queue Flow

When a message fails processing too many times, SQS automatically moves it to the DLQ.

### 5.1 Sequence Diagram

```
Consumer              SQS Source Queue       SQS DLQ               Operations
  |                      |                      |                      |
  |--ReceiveMessage----->|                      |                      |
  |<--msg (count=5)------|                      |                      |
  |                      |                      |                      |
  |  [Processing fails]  |                      |                      |
  |  (no DeleteMessage)  |                      |                      |
  |                      |                      |                      |
  |                      |  [Visibility timeout  |                      |
  |                      |   expires]            |                      |
  |                      |                      |                      |
  |                      |  [SQS checks          |                      |
  |                      |   ApproxReceiveCount  |                      |
  |                      |   (5) > maxReceive-   |                      |
  |                      |   Count (5) in        |                      |
  |                      |   redrive policy]     |                      |
  |                      |                      |                      |
  |                      |--move message-------->|                      |
  |                      |  (original body,      |                      |
  |                      |   original attributes,|                      |
  |                      |   original MessageId) |                      |
  |                      |                      |                      |
  |                      |--delete from source-->|                      |
  |                      |                      |                      |
  |                      |                      |  [Message sits in DLQ |
  |                      |                      |   until retention     |
  |                      |                      |   expires or operator |
  |                      |                      |   processes it]       |
  |                      |                      |                      |
  |                      |                      |  [CloudWatch alarm    |
  |                      |                      |   triggers on         |
  |                      |                      |   ApproxNumberOf-     |
  |                      |                      |   Messages > 0]       |
  |                      |                      |                      |
  |                      |                      |<----operator----------|
  |                      |                      |     investigates      |
  |                      |                      |     DLQ messages      |
```

### 5.2 What Gets Preserved in the DLQ

| Data | Preserved? | Notes |
|------|-----------|-------|
| Message body | Yes | Identical to the original |
| Message attributes | Yes | All custom attributes preserved |
| `MessageId` | Yes | Same as original (for correlation) |
| `ApproximateReceiveCount` | Reset to 0 | Starts fresh in the DLQ |
| `SentTimestamp` | Preserved | Original send time |
| `ApproximateFirstReceiveTimestamp` | Reset | Reflects first receive from DLQ |
| Original queue information | Via `DeadLetterQueueSourceArn` | System attribute on received messages |

### 5.3 Message Retention Gotcha

**Standard queues:** The message retention timer does NOT reset when moved to the DLQ. The original `SentTimestamp` is preserved, and the retention period is calculated from that.

**Example:**
```
Message sent to source queue at T=0
Source queue retention = 4 days
DLQ retention = 14 days

Message fails processing for 3 days
Message moved to DLQ at T=3 days

Standard queue: Message's "age" is still 3 days (original timestamp)
  → If DLQ retention is 14 days, message expires at T=14 days (11 more days in DLQ)
  → BUT if DLQ retention is only 4 days, message expires at T=4 days (only 1 more day in DLQ!)

BEST PRACTICE: Set DLQ retention ≥ source queue retention. Ideally set DLQ to 14 days (max).
```

**FIFO queues:** The enqueue timestamp IS reset when moved to the DLQ. The message gets a fresh retention period.

### 5.4 DLQ Monitoring Pattern

```json
// CloudWatch Alarm for DLQ
{
    "AlarmName": "OrderDLQ-HasMessages",
    "MetricName": "ApproximateNumberOfMessagesVisible",
    "Namespace": "AWS/SQS",
    "Dimensions": [
        { "Name": "QueueName", "Value": "order-processing-dlq" }
    ],
    "ComparisonOperator": "GreaterThanThreshold",
    "Threshold": 0,
    "EvaluationPeriods": 1,
    "Period": 60,
    "AlarmActions": ["arn:aws:sns:us-east-1:123456789012:ops-alerts"]
}
```

**Every DLQ should have this alarm.** Messages in the DLQ represent processing failures that need human attention.

---

## 6. DLQ Redrive Flow (StartMessageMoveTask)

After investigating and fixing the issue that caused messages to fail, you can move them back to the source queue for reprocessing.

### 6.1 Sequence Diagram

```
Operator              SQS API                DLQ                 Source Queue
  |                      |                    |                      |
  |--StartMessageMove--->|                    |                      |
  |  Task(SourceArn=DLQ, |                    |                      |
  |   DestArn=source,    |                    |                      |
  |   Rate=50/s)         |                    |                      |
  |                      |                    |                      |
  |<--TaskHandle---------|                    |                      |
  |                      |                    |                      |
  |                      |  [Async: SQS reads |                      |
  |                      |   messages from DLQ]|                      |
  |                      |                    |                      |
  |                      |<---read msg 1------|                      |
  |                      |----send msg 1------|--------------------->|
  |                      |<---delete msg 1----|                      |
  |                      |                    |                      |
  |                      |<---read msg 2------|                      |
  |                      |----send msg 2------|--------------------->|
  |                      |<---delete msg 2----|                      |
  |                      |                    |                      |
  |                      |  [... repeats at   |                      |
  |                      |   50 msgs/s rate]  |                      |
  |                      |                    |                      |
  |--GetMessageMoveTask->|                    |                      |
  |<--status: RUNNING----|                    |                      |
  |  (moved: 150,        |                    |                      |
  |   remaining: 50)     |                    |                      |
  |                      |                    |                      |
  |                      |  [All messages     |                      |
  |                      |   moved]           |                      |
  |                      |                    |                      |
  |--GetMessageMoveTask->|                    |                      |
  |<--status: COMPLETED--|                    |                      |
  |  (moved: 200,        |                    |                      |
  |   remaining: 0)      |                    |                      |
```

### 6.2 Key Behaviors

- **Rate limiting:** `MaxNumberOfMessagesPerSecond` prevents overwhelming the destination queue's consumers. Start low (10-50/s) and increase.
- **Cancellable:** Call `CancelMessageMoveTask` to stop mid-redrive. Already-moved messages stay in the destination.
- **FIFO caveat:** When redriving FIFO messages, the original `SequenceNumber` is lost. Messages get new sequence numbers in the destination queue. Order within a message group is preserved only if messages are redrived in order.

---

## 7. FIFO Send + Deduplication Flow

FIFO queues add deduplication and ordering on top of the standard send flow.

### 7.1 Sequence Diagram

```
Producer              SQS Front-End         Dedup Store          Storage Nodes
  |                      |                      |                      |
  |--SendMessage-------->|                      |                      |
  |  (GroupId="cust-123", |                     |                      |
  |   DedupId="ord-789", |                      |                      |
  |   Body="...")         |                      |                      |
  |                      |                      |                      |
  |                      |--auth + validate---->|                      |
  |                      |                      |                      |
  |                      |--check dedup-------->|                      |
  |                      |  (has "ord-789" been |                      |
  |                      |   seen in last       |                      |
  |                      |   5 minutes?)        |                      |
  |                      |<--NOT seen-----------|                      |
  |                      |                      |                      |
  |                      |--store dedup ID----->|                      |
  |                      |  ("ord-789", TTL=5m) |                      |
  |                      |                      |                      |
  |                      |--assign SequenceNum--|                      |
  |                      |  (monotonic within   |                      |
  |                      |   "cust-123" group)  |                      |
  |                      |                      |                      |
  |                      |--store message-------|--------------------->|
  |                      |                      |  (replicate across   |
  |                      |                      |   AZs)               |
  |                      |                      |                      |
  |<--200 OK ------------|                      |                      |
  |  (MessageId,         |                      |                      |
  |   SequenceNumber)    |                      |                      |
  |                      |                      |                      |
  |                      |                      |                      |
  |  [RETRY: same DedupId]                      |                      |
  |--SendMessage-------->|                      |                      |
  |  (DedupId="ord-789") |                      |                      |
  |                      |--check dedup-------->|                      |
  |                      |<--ALREADY SEEN-------|                      |
  |                      |                      |                      |
  |<--200 OK ------------|  [No new message stored.                    |
  |  (same MessageId     |   Returns original MessageId.               |
  |   as before)         |   Duplicate silently dropped.]              |
```

### 7.2 Deduplication Window

```
Timeline:
 T=0s          T=60s         T=300s (5 min)      T=301s
  |             |              |                   |
  |─── Send ───>|              |                   |
  |  DedupId=   |              |                   |
  |  "ord-789"  |              |                   |
  |             |              |                   |
  |  ├──────── DEDUP ACTIVE ──────────────────────>|
  |  |                         |                   |
  |  | Retry with same         |                   |
  |  | DedupId → DROPPED ✓     |                   |
  |  |                         |                   |
  |  |                         | DEDUP EXPIRED     |
  |  |                         |                   |
  |  |                         | Same DedupId      |
  |  |                         | → NEW MESSAGE     |
  |  |                         |   (not a dup!)    |
```

**The 5-minute window** covers typical network retry scenarios (timeouts, transient errors). It does NOT provide permanent deduplication. If your application needs deduplication beyond 5 minutes, implement it at the consumer level (e.g., idempotency key in a database).

### 7.3 Content-Based Deduplication

When `ContentBasedDeduplication=true` on the queue:

```
Message body: '{"orderId":"ORD-789","amount":99.99}'
  → SHA-256 hash of body = abc123...
  → This hash is used as the MessageDeduplicationId

Same body sent again within 5 minutes:
  → Same SHA-256 hash → duplicate detected → dropped

Different body (even slightly):
  → Different hash → treated as a new message
```

**Caveat:** Only the body is hashed. If you send the same body with different message attributes, it's treated as a duplicate (attributes are ignored in the hash).

---

## 8. FIFO Receive + Message Group Ordering Flow

The most nuanced flow — FIFO queues lock an entire message group when any message from it is in-flight.

### 8.1 Sequence Diagram

```
Consumer A            SQS FIFO Queue         Consumer B
  |                      |                         |
  |  Queue state:        |                         |
  |  Group "cust-123": [msg-1, msg-2, msg-3]      |
  |  Group "cust-456": [msg-4, msg-5]              |
  |                      |                         |
  |--ReceiveMessage----->|                         |
  |  (MaxNumber=10)      |                         |
  |                      |                         |
  |<--[msg-1, msg-4]-----|                         |
  |  (one from each      |                         |
  |   group, in order)   |                         |
  |                      |                         |
  |  Group "cust-123": LOCKED (msg-1 in-flight)    |
  |  Group "cust-456": LOCKED (msg-4 in-flight)    |
  |                      |                         |
  |                      |<--ReceiveMessage--------|
  |                      |  (MaxNumber=10)         |
  |                      |                         |
  |                      |---[] (empty!)---------->|
  |                      |  Both groups locked.     |
  |                      |  No messages available.  |
  |                      |                         |
  |--DeleteMessage(msg1)->|                         |
  |                      |                         |
  |  Group "cust-123": UNLOCKED                    |
  |  msg-2 now available                           |
  |                      |                         |
  |                      |<--ReceiveMessage--------|
  |                      |---[msg-2]-------------->|
  |                      |  (next msg in           |
  |                      |   cust-123 group)       |
  |                      |                         |
  |--DeleteMessage(msg4)->|                         |
  |                      |                         |
  |  Group "cust-456": UNLOCKED                    |
  |  msg-5 now available                           |
```

### 8.2 The Message Group Locking Rule

**Rule:** When any message from a message group is in-flight (received but not deleted), **NO other message from that same group** can be received by ANY consumer.

This is the fundamental mechanism that ensures strict ordering within a group:
- Message 1 must be processed and deleted before message 2 becomes available
- This guarantees that if you process msg-1 then msg-2, you process them in order
- No risk of msg-2 being processed while msg-1 is still being processed

**Cost:** A slow consumer on one message blocks the entire group. If processing msg-1 takes 60 seconds, msg-2 and msg-3 are delayed by 60 seconds even if other consumers are idle.

### 8.3 Parallelism Pattern

```
Goal: Process 10,000 orders per second, each order must be processed in sequence.

WRONG approach:
  All messages use MessageGroupId = "all-orders"
  → Entire queue is serialized. Throughput = 1 order per visibility timeout.
  → If timeout = 30s, throughput = 1/30 = 0.03 orders/second. Terrible.

RIGHT approach:
  Each order uses MessageGroupId = order_id (e.g., "ORD-12345")
  → Each order's events are processed in sequence
  → Different orders processed in parallel by different consumers
  → If 10,000 distinct order IDs: up to 10,000 messages in parallel

Even better:
  MessageGroupId = customer_id
  → All of customer-123's orders are in sequence (cross-order ordering)
  → Different customers are in parallel
  → Natural parallelism = number of active customers
```

### 8.4 What Happens with Multiple Groups in a Batch

```
ReceiveMessage(MaxNumberOfMessages=10)

SQS tries to return 10 messages, preferring same group:

Case 1: Group "cust-123" has 10+ available messages
  → Returns 10 messages, all from "cust-123", in strict order

Case 2: Group "cust-123" has 3 available, "cust-456" has 7 available
  → Returns 10 messages: first 3 from "cust-123", then 7 from "cust-456"
  → Order is strict WITHIN each group
  → No ordering guarantee BETWEEN groups

Case 3: All groups are locked (all have in-flight messages)
  → Returns 0 messages (empty response)
```

---

## 9. Long Polling Flow

Long polling eliminates empty responses and reduces API calls.

### 9.1 Sequence Diagram

```
Consumer              SQS Front-End         Storage Partitions    Producer
  |                      |                      |                    |
  |--ReceiveMessage----->|                      |                    |
  |  (WaitTime=20)       |                      |                    |
  |                      |                      |                    |
  |                      |--query ALL ---------->|                    |
  |                      |  partitions          |                    |
  |                      |<--0 messages---------|                    |
  |                      |                      |                    |
  |                      |  [Hold connection...] |                    |
  |                      |  [No messages yet...] |                    |
  |                      |                      |                    |
  |                      |     (7 seconds pass)  |                    |
  |                      |                      |                    |
  |                      |                      |<--SendMessage------|
  |                      |                      |  (new msg arrives) |
  |                      |                      |                    |
  |                      |<--message available--|                    |
  |                      |                      |                    |
  |<--[message]----------|  (returns at t=7s,   |                    |
  |                      |   NOT at t=20s)      |                    |
  |                      |                      |                    |
  |  Total wait: 7s      |                      |                    |
  |  Messages received: 1|                      |                    |
```

### 9.2 Long Poll with Timeout Expiry

```
Consumer              SQS Front-End         Storage Partitions
  |                      |                      |
  |--ReceiveMessage----->|                      |
  |  (WaitTime=20)       |                      |
  |                      |                      |
  |                      |--query ALL ---------->|
  |                      |<--0 messages---------|
  |                      |                      |
  |                      |  [Hold connection...] |
  |                      |  [20 seconds pass...] |
  |                      |  [No messages arrived]|
  |                      |                      |
  |<--[] (empty)---------|  (returns at t=20s   |
  |                      |   with empty array)  |
  |                      |                      |
  |  Total wait: 20s     |                      |
  |  Messages: 0         |                      |
  |                      |                      |
  |  (Consumer immediately sends another         |
  |   ReceiveMessage to start a new long poll)   |
```

### 9.3 Cost Comparison

Processing 100 messages that arrive over 100 seconds:

| Method | API Calls | Wait Time | Cost (at $0.40/million) |
|--------|-----------|-----------|------------------------|
| Short poll every 100ms | 1,000 polls (900 empty) | 0ms per call | $0.0004 |
| Short poll every 1s | 100 polls (0 empty if lucky) | 0ms per call | $0.00004 |
| Long poll (20s wait) | ~5 polls | Up to 20s per call | $0.000002 |

Long polling reduces cost by **200x** compared to aggressive short polling.

### 9.4 HTTP Timeout Configuration

**Critical operational detail:** Your HTTP client timeout must be **longer** than `WaitTimeSeconds`. Otherwise:

```
Consumer sets WaitTimeSeconds=20
Consumer HTTP client timeout = 15 seconds

t=0:  ReceiveMessage sent
t=15: HTTP client times out → connection dropped
      SQS is still waiting (up to 20s)
      Consumer sees a timeout error, retries
t=0:  New ReceiveMessage sent
t=15: HTTP client times out again
...infinite loop of timeouts, never receiving messages
```

**Best practice:** Set HTTP client timeout to `WaitTimeSeconds + 5 seconds` (e.g., 25 seconds for a 20-second long poll). AWS SDKs handle this automatically.

---

## 10. Delay Queue Flow

Delay queues postpone message delivery, useful for scheduled processing.

### 10.1 Sequence Diagram

```
Producer              SQS Queue              Delay Timer          Consumer
  |                   (DelaySeconds=60)          |                    |
  |                      |                       |                    |
  |--SendMessage-------->|                       |                    |
  |                      |                       |                    |
  |<--200 OK ------------|                       |                    |
  |  (MessageId)         |                       |                    |
  |                      |                       |                    |
  |                      |--start delay timer--->|                    |
  |                      |  (60 seconds)         |                    |
  |                      |                       |                    |
  |                      |                       |                    |
  |                      |  [Message is INVISIBLE|                    |
  |                      |   for 60 seconds]     |                    |
  |                      |                       |                    |
  |                      |      (Consumer polls) |<--ReceiveMessage---|
  |                      |                       |---[] (empty)------>|
  |                      |                       |  (message still    |
  |                      |                       |   in delay period) |
  |                      |                       |                    |
  |                      |  [60 seconds pass]    |                    |
  |                      |<--delay expired-------|                    |
  |                      |  [Message becomes     |                    |
  |                      |   VISIBLE]            |                    |
  |                      |                       |                    |
  |                      |                       |<--ReceiveMessage---|
  |                      |                       |---[message]------->|
  |                      |                       |                    |
```

### 10.2 Delay Queue vs Visibility Timeout

```
Timeline for a message with BOTH delay and visibility timeout:

Phase 1: DELAY (queue-level or per-message DelaySeconds)
├───────── INVISIBLE (delay) ──────────────────────>

Phase 2: AVAILABLE (message is visible, waiting for consumer)
├──── VISIBLE (can be received) ───>

Phase 3: VISIBILITY TIMEOUT (after ReceiveMessage)
├──── INVISIBLE (in-flight, being processed) ────>

Phase 4: EITHER deleted (success) OR visible again (timeout)
```

| Property | Delay | Visibility Timeout |
|----------|-------|--------------------|
| When | **Before** first receive | **After** each receive |
| Purpose | Postpone initial availability | Prevent duplicate processing |
| Who triggers | Producer (at send time) | Consumer (at receive time) |
| Max duration | 15 minutes | 12 hours |
| Applies to | New messages only | Each receive independently |

### 10.3 Per-Message vs Per-Queue Delay

**Per-queue delay (DelaySeconds attribute):**
- Applies to ALL messages sent to the queue
- Cannot override per-message on FIFO queues

**Per-message delay (DelaySeconds on SendMessage):**
- Overrides the queue default for this specific message
- **Standard queues only** — FIFO queues cannot set per-message delay
- Changing the queue-level delay does NOT affect messages already in delay (standard queues)
- Changing the queue-level delay DOES affect messages already in delay (FIFO queues)

### 10.4 Use Cases for Delay Queues

| Use Case | Delay Duration | Why |
|----------|---------------|-----|
| Retry with backoff | 30-60 seconds | On processing failure, re-send with increasing delay instead of immediate retry |
| Scheduled notifications | 15 minutes (max) | "Remind user in 15 minutes" |
| Order confirmation wait | 5 minutes | Wait for potential cancellation before processing |
| Rate smoothing | Variable | Spread burst of messages over time |

**For delays > 15 minutes:** Use Amazon EventBridge Scheduler instead of SQS delay. It supports arbitrary scheduling up to a year in advance.

---

## 11. Batch Send/Receive Flow

Batching is critical for performance and cost optimization.

### 11.1 Batch Send Flow

```
Producer              SQS Front-End         Storage
  |                      |                      |
  |--SendMessageBatch--->|                      |
  |  (10 messages)       |                      |
  |                      |                      |
  |                      |--validate all 10---->|
  |                      |  (total size ≤256KB, |
  |                      |   each msg valid)    |
  |                      |                      |
  |                      |--store all 10------->|
  |                      |  (replicate across   |
  |                      |   AZs)               |
  |                      |                      |
  |<--200 OK ------------|                      |
  |  Successful: [msg-1..msg-8]                 |
  |  Failed: [msg-9 (invalid), msg-10 (throttled)]
```

### 11.2 The Receive-Process-Delete Batch Pattern

The canonical SQS consumer loop:

```
while True:
    # 1. Batch receive (long poll)
    response = ReceiveMessage(
        QueueUrl=queue_url,
        MaxNumberOfMessages=10,
        WaitTimeSeconds=20,
        VisibilityTimeout=60
    )

    if not response.Messages:
        continue  # Long poll returned empty after 20s

    # 2. Process each message
    successful = []
    for msg in response.Messages:
        try:
            process(msg.Body)
            successful.append({
                "Id": msg.MessageId,
                "ReceiptHandle": msg.ReceiptHandle
            })
        except Exception as e:
            log.error(f"Failed to process {msg.MessageId}: {e}")
            # Don't add to successful — let visibility timeout expire
            # Message will be retried

    # 3. Batch delete successful messages
    if successful:
        result = DeleteMessageBatch(
            QueueUrl=queue_url,
            Entries=successful
        )
        if result.Failed:
            log.error(f"Failed to delete: {result.Failed}")
            # These messages will be re-received and re-processed
            # Consumer must be idempotent!
```

### 11.3 Why This Pattern Works

```
10 messages received
├── 8 processed successfully → batch delete → gone permanently
├── 1 threw exception → NOT deleted → visibility timeout expires → re-received → retry
└── 1 delete failed → NOT deleted → visibility timeout expires → re-received → retry

After maxReceiveCount failures → moved to DLQ → operator investigates
```

This is the **at-least-once, eventually-processed** pattern. Every message is either:
1. Successfully processed and deleted, or
2. Moved to the DLQ after repeated failures

No messages are silently lost (assuming the retention period is long enough and a DLQ is configured).

---

## 12. Complete Message Lifecycle (End-to-End)

The full lifecycle of a single message through a production SQS setup:

```
┌──────────────────────────────────────────────────────────────────────────┐
│                        MESSAGE LIFECYCLE                                 │
│                                                                          │
│  ┌─────────┐     ┌─────────────┐     ┌──────────────┐                   │
│  │ CREATED │────>│ AVAILABLE   │────>│ IN-FLIGHT    │                   │
│  │         │     │ (visible)   │     │ (invisible)  │                   │
│  └─────────┘     └─────────────┘     └──────────────┘                   │
│       │               ▲                    │    │                        │
│       │               │                    │    │                        │
│  [If delay > 0]       │              ┌─────┘    └─────┐                 │
│       │               │              │                │                  │
│       ▼               │              ▼                ▼                  │
│  ┌─────────┐          │         ┌─────────┐    ┌──────────┐            │
│  │ DELAYED │──────────┘         │ DELETED │    │ TIMEOUT  │            │
│  │(invisible│   (delay          │(permanent│    │ EXPIRED  │            │
│  │ for N s) │    expires)       │  removal)│    │          │            │
│  └─────────┘                    └─────────┘    └──────────┘            │
│                                                     │                   │
│                                                     │                   │
│                                          ┌──────────┘                   │
│                                          │                              │
│                                          ▼                              │
│                                    ┌──────────────┐                     │
│                                    │ AVAILABLE    │  (back to visible,  │
│                                    │ (again)      │   receive count +1) │
│                                    └──────────────┘                     │
│                                          │                              │
│                                          │ (receive count >             │
│                                          │  maxReceiveCount)            │
│                                          ▼                              │
│                                    ┌──────────────┐                     │
│                                    │ MOVED TO DLQ │                     │
│                                    └──────────────┘                     │
│                                          │                              │
│                                    ┌─────┴─────┐                       │
│                                    ▼           ▼                        │
│                              ┌──────────┐ ┌──────────┐                 │
│                              │ REDRIVEN │ │ EXPIRED  │                 │
│                              │ (back to │ │ (retention│                 │
│                              │  source) │ │  period   │                 │
│                              └──────────┘ │  elapsed) │                 │
│                                           └──────────┘                 │
└──────────────────────────────────────────────────────────────────────────┘
```

### State Transitions

| From | To | Trigger | Latency |
|------|----|---------|---------|
| Created → Available | Immediate (no delay) | SendMessage returns 200 | ~5-20ms |
| Created → Delayed | DelaySeconds > 0 | SendMessage with delay | ~5-20ms to store, then N seconds delay |
| Delayed → Available | Delay expires | Timer | Exactly N seconds |
| Available → In-flight | ReceiveMessage | Consumer receives | ~1-20ms |
| In-flight → Deleted | DeleteMessage | Consumer deletes | ~2-10ms |
| In-flight → Available | Visibility timeout | Timer expires without delete | Exactly N seconds (default 30s) |
| In-flight → In-flight | ChangeMessageVisibility | Consumer extends timeout | ~2-5ms |
| Available → Moved to DLQ | maxReceiveCount exceeded | SQS internal check on next receive attempt | Automatic |
| DLQ → Redriven to source | StartMessageMoveTask | Operator action | Rate-limited |
| Any → Expired | Retention period (max 14 days) | Timer | Automatic |

---

## 13. Flow Summary Table

| Flow | Typical Latency | Key Components | Failure Mode |
|------|----------------|----------------|--------------|
| **SendMessage** | 5-20ms | Front-end → Auth → Validate → Store (multi-AZ) | Retry on 500; fix request on 400 |
| **ReceiveMessage (short poll)** | 1-5ms | Front-end → Auth → Query subset of partitions | May return 0 msgs when msgs exist |
| **ReceiveMessage (long poll)** | 1ms-20s | Front-end → Auth → Query all partitions → Wait | Returns when msg arrives or timeout |
| **DeleteMessage** | 2-10ms | Front-end → Auth → Mark deleted (multi-AZ) | Idempotent; safe to retry |
| **Consumer failure** | 30s (default timeout) | Visibility timeout expires → msg re-visible | At-least-once redelivery |
| **DLQ move** | Automatic | ApproxReceiveCount > maxReceiveCount | Message preserved in DLQ |
| **DLQ redrive** | Rate-limited | StartMessageMoveTask → batch move | Cancellable; already-moved msgs stay |
| **FIFO send + dedup** | 5-25ms | Dedup check → Store → Assign SequenceNumber | Duplicate silently dropped |
| **FIFO receive** | 1-20ms | Group lock check → Return in-order → Lock group | Group blocked until delete |
| **Delay queue** | N seconds | Store → Wait DelaySeconds → Become visible | Max 15 minutes; use EventBridge for longer |
| **Batch operations** | Similar per-op | Up to 10 per batch → partial failure possible | MUST check Failed array |
