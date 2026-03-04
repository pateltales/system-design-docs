# Amazon SQS — API Contracts & Design

> Companion deep dive to the [interview simulation](interview-simulation.md). This document details SQS's API design, request/response formats, and design decisions.

---

## Table of Contents

1. [API Design Philosophy](#1-api-design-philosophy)
2. [Core Message Operations](#2-core-message-operations)
3. [Batch Operations](#3-batch-operations)
4. [Queue Management Operations](#4-queue-management-operations)
5. [Visibility Timeout Operations](#5-visibility-timeout-operations)
6. [Dead Letter Queue Operations](#6-dead-letter-queue-operations)
7. [Long Polling vs Short Polling](#7-long-polling-vs-short-polling)
8. [FIFO-Specific API Behavior](#8-fifo-specific-api-behavior)
9. [Authentication & Authorization](#9-authentication--authorization)
10. [Error Handling](#10-error-handling)
11. [Rate Limits & Throttling](#11-rate-limits--throttling)
12. [Design Decisions Summary](#12-design-decisions-summary)

---

## 1. API Design Philosophy

### 1.1 Why JSON Protocol over REST

SQS uses a **JSON-based RPC protocol** (via `X-Amz-Target` header), not a resource-oriented REST API like S3. This is a deliberate design choice:

| Factor | S3's REST Approach | SQS's JSON RPC Approach |
|--------|-------------------|------------------------|
| Resource model | Clear resources (bucket, object) mapped to URIs | Operations on a queue — less natural as REST resources |
| Verb mapping | PUT/GET/DELETE/HEAD map cleanly to CRUD | "ReceiveMessage" doesn't map to a single HTTP verb — it's a read that has side effects (visibility timeout) |
| Batch operations | Awkward in REST (what URI for "delete 10 messages"?) | Natural — just send an array in the request body |
| Side effects | PUT is idempotent, GET is safe | ReceiveMessage changes message state (makes it invisible). DeleteMessage removes a message. These are inherently stateful operations |
| Discoverability | URIs are self-documenting | Requires API reference |

**The core insight:** S3 manages **static resources** (objects) where CRUD semantics are natural. SQS manages **message lifecycle transitions** (invisible → visible → deleted) where operations have side effects. An RPC-style API better captures these state transitions than REST verbs.

### 1.2 Queue URL as Resource Identifier

Every SQS operation requires a **QueueUrl** — the fully qualified URL of the queue:

```
https://sqs.{region}.amazonaws.com/{account-id}/{queue-name}
```

Example:
```
https://sqs.us-east-1.amazonaws.com/123456789012/my-order-queue
```

For FIFO queues, the queue name must end with `.fifo`:
```
https://sqs.us-east-1.amazonaws.com/123456789012/my-order-queue.fifo
```

**Why a URL instead of just a name?**
- **Region-scoped:** The URL embeds the region, so you can't accidentally send messages to the wrong region's queue
- **Account-scoped:** The URL embeds the AWS account ID, enabling cross-account access patterns (Account A publishes to Account B's queue)
- **HTTP-routable:** The URL is a valid HTTP endpoint — the SDK can route the request directly

**Queue ARN vs Queue URL:**
- ARN: `arn:aws:sqs:us-east-1:123456789012:my-order-queue` — used for IAM policies, SNS subscriptions
- URL: `https://sqs.us-east-1.amazonaws.com/123456789012/my-order-queue` — used for API calls

You cannot use ARN in API calls. You must use the URL. This is a common interview gotcha.

### 1.3 Idempotency Analysis

| Operation | Idempotent? | Explanation |
|-----------|-------------|-------------|
| `SendMessage` (Standard) | **No** | Every call creates a new message, even with identical body. Two calls = two messages. |
| `SendMessage` (FIFO) | **Yes** (within 5-min dedup window) | If `MessageDeduplicationId` matches a recent message, the duplicate is silently dropped. Returns the original `MessageId`. |
| `ReceiveMessage` | **No** | Has side effects — makes messages invisible via visibility timeout. Same call at different times returns different messages. |
| `DeleteMessage` | **Yes** | Deleting an already-deleted message succeeds silently (no error). Safe to retry. |
| `ChangeMessageVisibility` | **Yes** | Setting the same timeout twice has the same effect. Safe to retry. |
| `CreateQueue` | **Yes** (if same attributes) | Creating a queue with the same name and identical attributes returns the existing queue URL. If attributes differ, returns `QueueNameExists` error. |
| `PurgeQueue` | **Yes** | Purging an already-empty queue succeeds. But note: can only be called once every 60 seconds. |

**Why this matters in interviews:** The interviewer may ask "what happens if SendMessage times out and the client retries?"

- **Standard queue:** You'll get a duplicate message. The consumer must be idempotent.
- **FIFO queue:** If the client sends the same `MessageDeduplicationId`, the duplicate is dropped. This is exactly-once *delivery* (not exactly-once *processing* — the consumer can still fail and re-receive).

### 1.4 Request Format

All SQS API calls use the same HTTP structure:

```http
POST / HTTP/1.1
Host: sqs.us-east-1.amazonaws.com
X-Amz-Target: AmazonSQS.{ActionName}
Content-Type: application/x-amz-json-1.0
Authorization: AWS4-HMAC-SHA256 Credential=.../sqs/aws4_request, ...
X-Amz-Date: 20260213T010000Z

{
    "QueueUrl": "https://sqs.us-east-1.amazonaws.com/123456789012/MyQueue",
    ...action-specific parameters...
}
```

Key observations:
- **All operations are POST** — no GET/PUT/DELETE verb differentiation
- **Action is in the header** (`X-Amz-Target`), not the URL
- **QueueUrl is in the body**, not the URL path
- **Content type is always `application/x-amz-json-1.0`**

---

## 2. Core Message Operations

### 2.1 SendMessage

**Purpose:** Deliver a message to a queue. The message is durably stored across multiple AZs before the API returns success.

#### Request

```json
POST / HTTP/1.1
Host: sqs.us-east-1.amazonaws.com
X-Amz-Target: AmazonSQS.SendMessage
Content-Type: application/x-amz-json-1.0

{
    "QueueUrl": "https://sqs.us-east-1.amazonaws.com/123456789012/order-processing",
    "MessageBody": "{\"orderId\": \"ORD-12345\", \"amount\": 99.99, \"currency\": \"USD\"}",
    "DelaySeconds": 0,
    "MessageAttributes": {
        "OrderType": {
            "DataType": "String",
            "StringValue": "STANDARD"
        },
        "Priority": {
            "DataType": "Number",
            "StringValue": "1"
        }
    },
    "MessageSystemAttributes": {
        "AWSTraceHeader": {
            "DataType": "String",
            "StringValue": "Root=1-5f4a2c1d-example"
        }
    }
}
```

#### Request Parameters

| Parameter | Type | Required | Constraints | Notes |
|-----------|------|----------|-------------|-------|
| `QueueUrl` | String | Yes | Valid SQS queue URL, case-sensitive | — |
| `MessageBody` | String | Yes | Min: 1 byte. Max: 256 KB (262,144 bytes). Allowed: XML, JSON, unformatted text. Unicode: `#x9`, `#xA`, `#xD`, `#x20` to `#xD7FF`, `#xE000` to `#xFFFD`, `#x10000` to `#x10FFFF` | Binary data must be base64-encoded and put in a Binary message attribute |
| `DelaySeconds` | Integer | No | 0–900 (0 to 15 minutes). Default: queue's `DelaySeconds` attribute. **Cannot set per-message on FIFO queues** — must use queue-level delay. | Delayed messages are invisible for this duration after being sent |
| `MessageAttributes` | Map | No | Max 10 attributes per message. Attribute names: alphanumeric, underscore, hyphen, period. Max 256 chars. Cannot start with `AWS.` or `Amazon.` (reserved). Attribute value + name + type count toward 256 KB message size limit. | Useful for SNS filter policies, Lambda event source mapping filters |
| `MessageDeduplicationId` | String | No (required for FIFO if content-based dedup is off) | Max 128 chars. Alphanumeric + punctuation. 5-minute deduplication window. | FIFO only. If content-based dedup is enabled on the queue, SQS uses SHA-256 hash of message body |
| `MessageGroupId` | String | Yes (FIFO), Optional (Standard) | Max 128 chars. Alphanumeric + punctuation. | FIFO: messages within the same group are delivered in strict order. Standard: used for message grouping in Amazon X-Ray traces. |
| `MessageSystemAttributes` | Map | No | Currently only `AWSTraceHeader` (for X-Ray). Does not count toward message size limit. | — |

#### Response — Success (HTTP 200)

```json
{
    "MessageId": "219f8380-5770-4cc2-8c3e-5c715e145f5e",
    "MD5OfMessageBody": "fafb00f5732ab283681e124bf8747ed1",
    "MD5OfMessageAttributes": "c48838208d2b4e14e3ca0093a8443f09",
    "MD5OfMessageSystemAttributes": "a1b2c3d4e5f6...",
    "SequenceNumber": "18850568838606834816"
}
```

| Field | Description |
|-------|-------------|
| `MessageId` | Unique ID assigned by SQS. Useful for logging, but **not** for deduplication (use `MessageDeduplicationId` for that). Not guaranteed unique in standard queues (in rare cases, you might get the same ID for different messages). |
| `MD5OfMessageBody` | MD5 hash of the message body. The SDK should verify this matches the body it sent — detects corruption in transit. |
| `MD5OfMessageAttributes` | MD5 hash of attributes. Same integrity check purpose. |
| `SequenceNumber` | **FIFO only.** 128-bit number assigned by SQS. Increases within a `MessageGroupId` — this is how SQS enforces ordering. Not globally unique — unique per message group. |

#### Server-Side Behavior

```
Client sends SendMessage
       │
       ▼
┌──────────────────┐
│ 1. AUTHENTICATE  │  Verify SigV4 signature. Check x-amz-date
│                  │  within 15-min clock skew.
└──────┬───────────┘
       │
       ▼
┌──────────────────┐
│ 2. AUTHORIZE     │  Evaluate IAM policies. Check:
│                  │  - Caller has sqs:SendMessage permission
│                  │  - Queue's resource-based policy allows caller
│                  │  - If encrypted: caller has kms:GenerateDataKey
└──────┬───────────┘
       │
       ▼
┌──────────────────┐
│ 3. VALIDATE      │  Check message body size ≤ 256 KB.
│                  │  Check unicode characters are valid.
│                  │  Check ≤ 10 message attributes.
│                  │  FIFO: check MessageGroupId present.
│                  │  FIFO: check dedup ID present or content-based dedup enabled.
└──────┬───────────┘
       │
       ▼
┌──────────────────┐
│ 4. ENCRYPT       │  If SSE-SQS or SSE-KMS is enabled:
│ (if configured)  │  - SSE-SQS: SQS manages keys internally
│                  │  - SSE-KMS: Call KMS to get data key, encrypt body
│                  │  Message attributes are NOT encrypted.
└──────┬───────────┘
       │
       ▼
┌──────────────────┐
│ 5. DEDUP CHECK   │  FIFO only. Check if MessageDeduplicationId
│ (FIFO only)      │  was seen in the last 5 minutes.
│                  │  If duplicate: return success with original MessageId.
│                  │  Do NOT store the message again.
└──────┬───────────┘
       │
       ▼
┌──────────────────┐
│ 6. STORE &       │  Write message to multiple storage nodes across
│    REPLICATE     │  at least 2 AZs (3 AZ replication typical).
│                  │  [INFERRED — SQS states "redundantly stored across
│                  │  multiple AZs" but doesn't specify exact count]
│                  │  Wait for durability confirmation before returning.
└──────┬───────────┘
       │
       ▼
┌──────────────────┐
│ 7. COMPUTE MD5   │  Calculate MD5 of body, attributes, system attributes.
│ & RESPOND        │  Assign MessageId (UUID).
│                  │  FIFO: assign monotonically increasing SequenceNumber
│                  │  within the MessageGroupId.
│                  │  Return HTTP 200 with response body.
└──────────────────┘
```

**Critical design point:** Step 6 is synchronous — the API blocks until the message is durably replicated. This is why SQS can promise that "once SendMessage returns HTTP 200, the message will not be lost." This is the same durability-before-response pattern as S3's PUT Object.

#### Status Codes

| Code | Meaning | Action |
|------|---------|--------|
| `200` | Message stored successfully, durably replicated | — |
| `400 InvalidMessageContents` | Message body contains invalid Unicode characters | Fix the message body |
| `400 RequestThrottled` | Exceeded queue's API rate limit | Exponential backoff, retry |
| `400 QueueDoesNotExist` | Queue URL is wrong or queue was deleted | Check URL, recreate queue |
| `400 KmsAccessDenied` | Missing KMS permissions for encrypted queue | Add `kms:GenerateDataKey` permission |
| `400 KmsThrottled` | KMS is throttling requests | Backoff. Consider SSE-SQS instead of SSE-KMS if KMS is a bottleneck |

---

### 2.2 ReceiveMessage

**Purpose:** Retrieve one or more messages from a queue. This is where SQS gets interesting — unlike a simple GET, ReceiveMessage has **side effects**: it makes messages invisible.

#### Request

```json
POST / HTTP/1.1
Host: sqs.us-east-1.amazonaws.com
X-Amz-Target: AmazonSQS.ReceiveMessage
Content-Type: application/x-amz-json-1.0

{
    "QueueUrl": "https://sqs.us-east-1.amazonaws.com/123456789012/order-processing",
    "MaxNumberOfMessages": 10,
    "WaitTimeSeconds": 20,
    "VisibilityTimeout": 60,
    "MessageSystemAttributeNames": [
        "SenderId",
        "SentTimestamp",
        "ApproximateReceiveCount",
        "ApproximateFirstReceiveTimestamp"
    ],
    "MessageAttributeNames": [
        "All"
    ]
}
```

#### Request Parameters

| Parameter | Type | Required | Constraints | Notes |
|-----------|------|----------|-------------|-------|
| `QueueUrl` | String | Yes | Valid SQS queue URL | — |
| `MaxNumberOfMessages` | Integer | No | 1–10. Default: 1. | SQS may return fewer than this — it samples from a subset of servers (standard queues). FIFO queues try harder to return up to this count. |
| `WaitTimeSeconds` | Integer | No | 0–20 seconds. Default: queue's `ReceiveMessageWaitTimeSeconds`. | 0 = short polling (returns immediately, possibly empty). 1–20 = long polling (holds connection until a message arrives or timeout). |
| `VisibilityTimeout` | Integer | No | 0–43,200 seconds (0 to 12 hours). Default: queue's `VisibilityTimeout` (default 30s). | Overrides queue-level default for this specific receive. |
| `MessageAttributeNames` | Array | No | `"All"`, specific names, or prefix patterns like `"Order.*"`. Max 256 chars per name. Cannot start with `AWS.` or `Amazon.`. | Controls which custom attributes are returned. Reduces response size if you only need specific attributes. |
| `MessageSystemAttributeNames` | Array | No | `"All"`, or specific: `SenderId`, `SentTimestamp`, `ApproximateReceiveCount`, `ApproximateFirstReceiveTimestamp`, `SequenceNumber`, `MessageDeduplicationId`, `MessageGroupId`, `AWSTraceHeader`, `DeadLetterQueueSourceArn` | System-level metadata about each message. |
| `ReceiveRequestAttemptId` | String | No | Max 128 chars. FIFO only. Valid for 5 minutes. | Deduplication token for the receive call itself — ensures retry of a failed ReceiveMessage returns the same messages. |

#### Response — Success (HTTP 200)

```json
{
    "Messages": [
        {
            "MessageId": "219f8380-5770-4cc2-8c3e-5c715e145f5e",
            "ReceiptHandle": "AQEBwJnKyrHigUMZj6rYigCgxlaS3SLy0a+JK...",
            "MD5OfBody": "fafb00f5732ab283681e124bf8747ed1",
            "Body": "{\"orderId\": \"ORD-12345\", \"amount\": 99.99}",
            "Attributes": {
                "SenderId": "AIDAIO23YDLYA7EXAMPLE",
                "SentTimestamp": "1707753600000",
                "ApproximateReceiveCount": "1",
                "ApproximateFirstReceiveTimestamp": "1707753601000"
            },
            "MessageAttributes": {
                "OrderType": {
                    "DataType": "String",
                    "StringValue": "STANDARD"
                }
            }
        }
    ]
}
```

#### Key Response Fields

| Field | Description | Interview Insight |
|-------|-------------|-------------------|
| `MessageId` | The ID assigned when the message was sent. Stays the same across re-receives. | NOT sufficient for deduplication — use `MessageDeduplicationId` for FIFO. |
| `ReceiptHandle` | **Critical.** A unique token for this specific receipt of this message. Required for `DeleteMessage` and `ChangeMessageVisibility`. Changes every time you receive the same message. | If you receive a message, fail to process it, and it becomes visible again, the new `ReceiveMessage` returns a **different** `ReceiptHandle`. You must use the latest one. |
| `Body` | The message body as sent by the producer. | Verify `MD5OfBody` matches to detect corruption. AWS SDKs do this automatically. |
| `ApproximateReceiveCount` | How many times this message has been received across all consumers. | Used by DLQ. When this exceeds `maxReceiveCount` on the redrive policy, the message is moved to the DLQ. |
| `ApproximateFirstReceiveTimestamp` | When the message was first received (epoch millis). | Useful for monitoring consumer lag. |
| `SequenceNumber` | FIFO only. The sequence number assigned at send time. | — |

#### Server-Side Behavior

```
Client sends ReceiveMessage
       │
       ▼
┌──────────────────────┐
│ 1. AUTHENTICATE &    │  SigV4, IAM check for sqs:ReceiveMessage
│    AUTHORIZE         │  If encrypted: check kms:Decrypt permission
└──────┬───────────────┘
       │
       ▼
┌──────────────────────┐
│ 2. POLL STRATEGY     │  If WaitTimeSeconds = 0 (short poll):
│                      │    → Query a SUBSET of storage partitions
│                      │    → Return immediately (may return 0 messages
│                      │      even if messages exist on other partitions)
│                      │  If WaitTimeSeconds > 0 (long poll):
│                      │    → Query ALL storage partitions
│                      │    → If no messages, hold connection open
│                      │    → Return when message arrives OR timeout
│                      │  [INFERRED — AWS docs say short poll queries
│                      │  "a subset of servers" but don't specify which]
└──────┬───────────────┘
       │
       ▼
┌──────────────────────┐
│ 3. SELECT MESSAGES   │  Select up to MaxNumberOfMessages.
│                      │  Standard: messages selected from sampled partitions.
│                      │    → Best-effort ordering (approximately FIFO).
│                      │    → May skip recently arrived messages.
│                      │  FIFO: messages selected in strict order per
│                      │    MessageGroupId. A group is "locked" while any
│                      │    message in it is in-flight.
└──────┬───────────────┘
       │
       ▼
┌──────────────────────┐
│ 4. SET VISIBILITY    │  For each selected message:
│    TIMEOUT           │  → Mark as "in-flight" (invisible to other consumers)
│                      │  → Start visibility timeout countdown
│                      │  → Generate unique ReceiptHandle
│                      │  Standard: up to ~120,000 in-flight per queue
│                      │  FIFO: up to 120,000 in-flight per queue
│                      │  If limit exceeded: OverLimit error
└──────┬───────────────┘
       │
       ▼
┌──────────────────────┐
│ 5. DECRYPT           │  If SSE-KMS: call KMS to decrypt message body
│ (if encrypted)       │  using the data key.
└──────┬───────────────┘
       │
       ▼
┌──────────────────────┐
│ 6. RETURN            │  Return Messages array with body, receipt handle,
│                      │  requested attributes, MD5 checksums.
│                      │  If no messages found (and short poll or long
│                      │  poll timeout expired): return empty array.
└──────────────────────┘
```

**Critical design point (Interview Gold):** Step 2 explains why standard queues can return 0 messages even when messages exist. Short polling samples a subset of partitions. This is the price of unlimited throughput — the queue is partitioned across many servers, and a single short-poll request doesn't query all of them. Long polling fixes this by querying all partitions but costs you up to 20 seconds of connection time.

#### Status Codes

| Code | Meaning |
|------|---------|
| `200` | Success. May contain 0 messages (empty `Messages` array). This is normal — not an error. |
| `400 OverLimit` | In-flight message limit reached (~120,000 standard, 120,000 FIFO). Messages exist but cannot be received until some are deleted or their visibility timeout expires. |
| `400 QueueDoesNotExist` | Queue URL is wrong or queue was deleted |
| `400 RequestThrottled` | Rate limit exceeded |
| `400 KmsAccessDenied` | Missing KMS decrypt permission |

---

### 2.3 DeleteMessage

**Purpose:** Permanently remove a message from the queue after successful processing. This completes the message lifecycle.

#### Request

```json
{
    "QueueUrl": "https://sqs.us-east-1.amazonaws.com/123456789012/order-processing",
    "ReceiptHandle": "AQEBwJnKyrHigUMZj6rYigCgxlaS3SLy0a+JK..."
}
```

#### Request Parameters

| Parameter | Type | Required | Constraints |
|-----------|------|----------|-------------|
| `QueueUrl` | String | Yes | Valid SQS queue URL |
| `ReceiptHandle` | String | Yes | The receipt handle from the most recent `ReceiveMessage` call. Must be the **latest** receipt handle — old handles from previous receives may fail or delete the wrong instance. |

#### Response — Success

HTTP 200 with empty body. No data returned.

#### Key Design Points

**Why ReceiptHandle instead of MessageId?**

This is a common interview question. The answer reveals SQS's distributed design:

1. **A message can be received multiple times** (at-least-once delivery in standard queues). Each receive generates a different `ReceiptHandle`. If you used `MessageId` to delete, you'd have ambiguity: which receive's processing succeeded?

2. **ReceiptHandle encodes context** about which specific receive operation this delete corresponds to. It ensures the consumer deleting the message is the one that successfully processed it, not a stale consumer that timed out.

3. **Stale receipt handles:** If your visibility timeout expires and another consumer receives the message with a new `ReceiptHandle`, your old `ReceiptHandle` becomes invalid. Attempting to delete with the old handle succeeds (no error) but may not actually delete the message if it's been re-received. [INFERRED — AWS docs are vague on this edge case]

```
Consumer A receives message → ReceiptHandle_A
       │
  (Consumer A is slow, visibility timeout expires)
       │
Consumer B receives same message → ReceiptHandle_B
       │
Consumer A calls DeleteMessage with ReceiptHandle_A
  → May succeed (HTTP 200) but the message has already been
    re-received by Consumer B with ReceiptHandle_B.
    The actual delete behavior depends on SQS internals.
    [INFERRED — not officially documented]
       │
Consumer B finishes, calls DeleteMessage with ReceiptHandle_B
  → Definitively deletes the message.
```

**Lesson:** Always delete messages promptly after processing. If your processing takes longer than the visibility timeout, extend it with `ChangeMessageVisibility` before it expires.

---

## 3. Batch Operations

SQS supports batch variants of three operations. Batching is **critical** for cost and performance optimization.

### 3.1 Why Batching Matters

| Metric | Without Batching | With Batching (10 per batch) |
|--------|------------------|------------------------------|
| API calls to process 10,000 messages | 30,000 (10K send + 10K receive + 10K delete) | 3,000 (1K send + 1K receive + 1K delete) |
| Cost | $0.012 (at $0.40/million) | $0.0012 |
| Network round trips | 30,000 | 3,000 |
| FIFO throughput | 300 msg/s (300 API calls/s × 1 msg) | 3,000 msg/s (300 API calls/s × 10 msgs) |

**For FIFO queues, batching is the only way to exceed 300 messages/second.** Without high-throughput mode, you're limited to 300 API calls/second. With batching, each call can carry 10 messages = 3,000 msg/s.

### 3.2 SendMessageBatch

**Purpose:** Send up to 10 messages in a single API call.

```json
{
    "QueueUrl": "https://sqs.us-east-1.amazonaws.com/123456789012/order-processing",
    "Entries": [
        {
            "Id": "msg-1",
            "MessageBody": "{\"orderId\": \"ORD-001\"}",
            "DelaySeconds": 0
        },
        {
            "Id": "msg-2",
            "MessageBody": "{\"orderId\": \"ORD-002\"}",
            "DelaySeconds": 5
        },
        {
            "Id": "msg-3",
            "MessageBody": "{\"orderId\": \"ORD-003\"}",
            "MessageAttributes": {
                "Priority": { "DataType": "Number", "StringValue": "1" }
            }
        }
    ]
}
```

#### Constraints

| Constraint | Value |
|------------|-------|
| Max entries per batch | **10** |
| Max total payload size | **256 KB** (combined size of all messages in the batch) |
| `Id` field | Required. Must be unique within the batch. Alphanumeric, hyphens, underscores. Max 80 chars. This is a client-assigned ID for correlating results — not the SQS `MessageId`. |
| FIFO behavior | All messages in the batch must have the same `MessageGroupId` to maintain ordering guarantees. Messages with different group IDs can be in the same batch but ordering is per-group. |

#### Response — Partial Failure

**This is the key design point about batch operations: they can partially succeed.**

```json
{
    "Successful": [
        {
            "Id": "msg-1",
            "MessageId": "219f8380-5770-4cc2-8c3e-5c715e145f5e",
            "MD5OfMessageBody": "fafb00f5732ab283681e124bf8747ed1"
        },
        {
            "Id": "msg-3",
            "MessageId": "5f9a3c8b-e4d2-4f1a-b7c6-example",
            "MD5OfMessageBody": "a1b2c3d4e5f6..."
        }
    ],
    "Failed": [
        {
            "Id": "msg-2",
            "Code": "InvalidMessageContents",
            "Message": "Invalid Unicode characters in message body",
            "SenderFault": true
        }
    ]
}
```

**Interview insight:** The batch API never returns a top-level error for individual message failures. Instead, it returns HTTP 200 with a `Successful` array and a `Failed` array. Your code **must** check the `Failed` array — ignoring it silently loses messages. This is a common production bug.

### 3.3 DeleteMessageBatch

```json
{
    "QueueUrl": "https://sqs.us-east-1.amazonaws.com/123456789012/order-processing",
    "Entries": [
        { "Id": "del-1", "ReceiptHandle": "AQEBwJnKyrHi..." },
        { "Id": "del-2", "ReceiptHandle": "AQEBxYzAbCdE..." }
    ]
}
```

Same partial-failure pattern as `SendMessageBatch`. Max 10 entries.

**Best practice:** Accumulate receipt handles during processing, then delete in a single batch call. Pattern:

```
receive_response = ReceiveMessage(MaxNumberOfMessages=10)
successful_handles = []
for msg in receive_response.Messages:
    try:
        process(msg)
        successful_handles.append(msg.ReceiptHandle)
    except:
        # Don't add to batch — let visibility timeout expire for retry
        pass
DeleteMessageBatch(Entries=successful_handles)
```

### 3.4 ChangeMessageVisibilityBatch

Same pattern. Up to 10 entries. Commonly used to extend visibility timeout for all messages in a batch when processing is slow.

---

## 4. Queue Management Operations

### 4.1 CreateQueue

**Purpose:** Create a new standard or FIFO queue.

```json
{
    "QueueName": "order-processing",
    "Attributes": {
        "VisibilityTimeout": "60",
        "MessageRetentionPeriod": "1209600",
        "ReceiveMessageWaitTimeSeconds": "20",
        "DelaySeconds": "0"
    }
}
```

For FIFO queue:

```json
{
    "QueueName": "order-processing.fifo",
    "Attributes": {
        "FifoQueue": "true",
        "ContentBasedDeduplication": "true",
        "DeduplicationScope": "messageGroup",
        "FifoThroughputLimit": "perMessageGroupId"
    }
}
```

#### Queue Attributes Reference

| Attribute | Default | Range | Notes |
|-----------|---------|-------|-------|
| `VisibilityTimeout` | 30 seconds | 0–43,200 (12 hours) | How long a message stays invisible after being received |
| `MessageRetentionPeriod` | 345,600 (4 days) | 60–1,209,600 (1 min to 14 days) | How long SQS keeps unprocessed messages before auto-deleting |
| `DelaySeconds` | 0 | 0–900 (15 min) | How long new messages are invisible before becoming available |
| `ReceiveMessageWaitTimeSeconds` | 0 (short poll) | 0–20 | Long polling timeout. Set to 20 for maximum efficiency. |
| `MaximumMessageSize` | 262,144 (256 KB) | 1,024–262,144 (1 KB to 256 KB) | Max message body size |
| `Policy` | — | — | JSON resource-based access policy |
| `RedrivePolicy` | — | — | JSON: `{"deadLetterTargetArn": "...", "maxReceiveCount": N}` |
| `RedriveAllowPolicy` | — | — | Controls which queues can use this queue as a DLQ |
| `KmsMasterKeyId` | — | — | KMS key for SSE-KMS encryption |
| `KmsDataKeyReusePeriodSeconds` | 300 | 60–86,400 | How long to cache the KMS data key |
| `SqsManagedSseEnabled` | `true` (for new queues) | — | SSE-SQS (managed encryption) |
| `FifoQueue` | `false` | — | FIFO only. Cannot change after creation. |
| `ContentBasedDeduplication` | `false` | — | FIFO only. Uses SHA-256 of body as dedup ID. |
| `DeduplicationScope` | `queue` | `queue` or `messageGroup` | FIFO high-throughput: `messageGroup` allows per-group dedup instead of queue-wide |
| `FifoThroughputLimit` | `perQueue` | `perQueue` or `perMessageGroupId` | FIFO high-throughput: `perMessageGroupId` allows independent partitioning per group |

#### Queue Naming Rules

| Rule | Standard Queue | FIFO Queue |
|------|---------------|------------|
| Max length | 80 characters | 80 characters (including `.fifo` suffix) |
| Allowed characters | Alphanumeric, hyphens, underscores | Same |
| Case sensitivity | Case-sensitive (`MyQueue` ≠ `myqueue`) | Same |
| Suffix | None required | **Must** end with `.fifo` |

#### Idempotency of CreateQueue

```
CreateQueue("my-queue", attributes={VisibilityTimeout: 30})
  → Returns QueueUrl. Queue created.

CreateQueue("my-queue", attributes={VisibilityTimeout: 30})  // same attributes
  → Returns same QueueUrl. No error. Idempotent.

CreateQueue("my-queue", attributes={VisibilityTimeout: 60})  // DIFFERENT attributes
  → Returns QueueNameExists error. NOT idempotent with different attributes.
```

**Why this design?** It prevents accidentally changing queue settings by re-running infrastructure-as-code that creates the queue. If you want to change attributes, use `SetQueueAttributes` explicitly.

### 4.2 SetQueueAttributes / GetQueueAttributes

**SetQueueAttributes** — Change any mutable queue attribute after creation.

```json
{
    "QueueUrl": "https://sqs.us-east-1.amazonaws.com/123456789012/order-processing",
    "Attributes": {
        "VisibilityTimeout": "120",
        "RedrivePolicy": "{\"deadLetterTargetArn\":\"arn:aws:sqs:us-east-1:123456789012:order-dlq\",\"maxReceiveCount\":\"5\"}"
    }
}
```

**GetQueueAttributes** — Read queue attributes.

```json
{
    "QueueUrl": "https://sqs.us-east-1.amazonaws.com/123456789012/order-processing",
    "AttributeNames": [
        "ApproximateNumberOfMessages",
        "ApproximateNumberOfMessagesNotVisible",
        "ApproximateNumberOfMessagesDelayed"
    ]
}
```

The three "Approximate" attributes are the most commonly queried:

| Attribute | Meaning | Why "Approximate"? |
|-----------|---------|-------------------|
| `ApproximateNumberOfMessages` | Messages available for receive | SQS is distributed — exact count requires querying all partitions, which is expensive. The count is eventually consistent. |
| `ApproximateNumberOfMessagesNotVisible` | Messages in-flight (received but not deleted) | Same — distributed sampling |
| `ApproximateNumberOfMessagesDelayed` | Messages in delay period (not yet available) | Same |

**Interview insight:** There is no way to get an exact message count from SQS. This is a fundamental consequence of SQS's distributed architecture — the queue is partitioned across multiple servers, and getting an exact count would require a distributed consensus read, which would hurt throughput. The approximation is typically within a few seconds of the true count.

### 4.3 GetQueueUrl

**Purpose:** Get the queue URL from a queue name. Useful when you know the name but not the full URL.

```json
{
    "QueueName": "order-processing",
    "QueueOwnerAWSAccountId": "123456789012"
}
```

The `QueueOwnerAWSAccountId` is optional — needed only for cross-account access (accessing another account's queue).

### 4.4 ListQueues

**Purpose:** List queues in the current account/region.

```json
{
    "QueueNamePrefix": "order-",
    "MaxResults": 100,
    "NextToken": "..."
}
```

Returns up to 1,000 queue URLs per request. Supports pagination with `NextToken`. The `QueueNamePrefix` filter is server-side — efficient for finding queues by naming convention.

### 4.5 PurgeQueue

**Purpose:** Delete all messages from a queue. Irreversible.

```json
{
    "QueueUrl": "https://sqs.us-east-1.amazonaws.com/123456789012/order-processing"
}
```

**Constraints:**
- Can only be called **once every 60 seconds** per queue
- Message deletion takes up to 60 seconds to complete
- Messages in-flight (received but not deleted) are also purged
- Returns HTTP 200 immediately, but deletion is asynchronous

**When to use:** Test environment cleanup, emergency drain. In production, prefer consuming and discarding over purging.

### 4.6 DeleteQueue

**Purpose:** Delete a queue and all its messages.

```json
{
    "QueueUrl": "https://sqs.us-east-1.amazonaws.com/123456789012/order-processing"
}
```

**Important behaviors:**
- Queue deletion is **not instantaneous** — SQS may take up to 60 seconds
- If you create a queue with the same name immediately after deleting, you might get the old queue (within the 60s window)
- All messages in the queue are lost
- Any in-flight messages become undeliverable

---

## 5. Visibility Timeout Operations

### 5.1 ChangeMessageVisibility

**Purpose:** Extend (or shorten) the visibility timeout for a specific message that's currently in-flight.

```json
{
    "QueueUrl": "https://sqs.us-east-1.amazonaws.com/123456789012/order-processing",
    "ReceiptHandle": "AQEBwJnKyrHigUMZj6rYigCgxlaS3SLy0a+JK...",
    "VisibilityTimeout": 300
}
```

#### Parameters

| Parameter | Type | Required | Constraints |
|-----------|------|----------|-------------|
| `QueueUrl` | String | Yes | — |
| `ReceiptHandle` | String | Yes | From the most recent `ReceiveMessage` |
| `VisibilityTimeout` | Integer | Yes | 0–43,200 (12 hours). Set to 0 to make the message immediately visible again (nack). |

#### Key Behaviors

**Setting to 0 — the "nack" pattern:**
```json
{
    "VisibilityTimeout": 0
}
```
This immediately makes the message visible to other consumers. Use this when you've received a message but realize you can't process it (wrong consumer, dependency unavailable, etc.). This is SQS's version of a negative acknowledgment.

**Timeout reset behavior:**
- The new timeout starts counting from the moment `ChangeMessageVisibility` is called, not from the original receive time
- If you don't delete the message, the visibility timeout **reverts to the queue's default** on the next receive — NOT to the value you set with `ChangeMessageVisibility`
- This is a subtle but important distinction. The extended timeout is ephemeral — it applies only to the current in-flight period.

**Heartbeat pattern for long-running processing:**
```
while processing:
    if time_remaining < 15_seconds:
        ChangeMessageVisibility(timeout=60)  # extend by 60s
    do_work_chunk()
DeleteMessage(receipt_handle)
```

This is the standard pattern for tasks that take longer than the default visibility timeout. Your consumer periodically "heartbeats" by extending the timeout. If the consumer crashes, the heartbeat stops, the timeout expires, and the message becomes visible for another consumer to pick up.

#### Error: MessageNotInflight

If you call `ChangeMessageVisibility` on a message whose visibility timeout has already expired (it's back in the queue), you get `MessageNotInflight`. This means another consumer may have already received it. Your processing is now racing with another consumer — you should stop and let the other consumer handle it.

---

## 6. Dead Letter Queue Operations

### 6.1 Configuring a DLQ (via RedrivePolicy)

A Dead Letter Queue is configured as an attribute on the **source queue**, not on the DLQ itself:

```json
// SetQueueAttributes on the SOURCE queue
{
    "QueueUrl": "https://sqs.us-east-1.amazonaws.com/123456789012/order-processing",
    "Attributes": {
        "RedrivePolicy": "{\"deadLetterTargetArn\":\"arn:aws:sqs:us-east-1:123456789012:order-processing-dlq\",\"maxReceiveCount\":\"5\"}"
    }
}
```

**RedrivePolicy fields:**

| Field | Description |
|-------|-------------|
| `deadLetterTargetArn` | ARN of the DLQ. Must be in the same AWS account and region. Standard queue → standard DLQ. FIFO queue → FIFO DLQ (must match). |
| `maxReceiveCount` | How many times a message can be received before SQS moves it to the DLQ. Range: 1–1,000. When `ApproximateReceiveCount` exceeds this, the message is moved on the next receive attempt. |

### 6.2 RedriveAllowPolicy (who can use this queue as a DLQ)

Configured on the **DLQ itself**:

```json
{
    "QueueUrl": "https://sqs.us-east-1.amazonaws.com/123456789012/order-processing-dlq",
    "Attributes": {
        "RedriveAllowPolicy": "{\"redrivePermission\":\"byQueue\",\"sourceQueueArns\":[\"arn:aws:sqs:us-east-1:123456789012:order-processing\",\"arn:aws:sqs:us-east-1:123456789012:payment-processing\"]}"
    }
}
```

| `redrivePermission` | Meaning |
|---------------------|---------|
| `allowAll` | Any queue in the account can use this as a DLQ |
| `denyAll` | No queue can use this as a DLQ |
| `byQueue` | Only queues listed in `sourceQueueArns` can use this |

### 6.3 StartMessageMoveTask (Redrive from DLQ)

**Purpose:** Move messages from a DLQ back to the source queue (or another queue) for reprocessing.

```json
{
    "SourceArn": "arn:aws:sqs:us-east-1:123456789012:order-processing-dlq",
    "DestinationArn": "arn:aws:sqs:us-east-1:123456789012:order-processing",
    "MaxNumberOfMessagesPerSecond": 50
}
```

| Parameter | Description |
|-----------|-------------|
| `SourceArn` | The DLQ to move messages from |
| `DestinationArn` | Where to move messages to. If omitted, moves to the original source queue (determined from the redrive policy). |
| `MaxNumberOfMessagesPerSecond` | Rate limiting for the redrive. Prevents overwhelming the destination queue's consumers. |

**Why this exists:** Before this API (launched 2023), the only way to redrive messages was to write a custom consumer that reads from the DLQ and re-sends to the source queue. This was error-prone and operationally painful. The native redrive handles all the edge cases (duplicate detection, error handling, rate limiting).

---

## 7. Long Polling vs Short Polling

### 7.1 The Problem with Short Polling

With `WaitTimeSeconds = 0` (short polling), SQS queries a **subset** of its internal servers:

```
Queue partitioned across 4 servers:
  [Server A: 0 msgs] [Server B: 3 msgs] [Server C: 0 msgs] [Server D: 2 msgs]

Short poll hits Server A and Server C:
  → Returns 0 messages (even though 5 messages exist!)

Short poll hits Server B:
  → Returns up to 3 messages ✓
```

**Consequences:**
- Empty responses waste API calls (you're charged per request)
- You must poll frequently to achieve low latency
- Many empty responses before finding messages → inefficient

### 7.2 Long Polling Solution

With `WaitTimeSeconds > 0` (long polling), SQS queries **all** servers and holds the connection:

```
Queue partitioned across 4 servers:
  [Server A: 0 msgs] [Server B: 0 msgs] [Server C: 0 msgs] [Server D: 0 msgs]

Long poll (WaitTimeSeconds=20):
  → Query ALL servers. No messages found.
  → Hold connection open...
  → (3 seconds later) Message arrives on Server B
  → Return message immediately.
  → Total wait: 3 seconds (not 20)
```

### 7.3 Comparison Table

| Dimension | Short Polling | Long Polling |
|-----------|--------------|--------------|
| `WaitTimeSeconds` | 0 (default) | 1–20 seconds |
| Servers queried | Subset | All |
| Empty responses | Frequent — even when messages exist | Only when no messages arrive within timeout |
| Cost | Higher (many wasted API calls) | Lower (fewer calls, each more productive) |
| Latency (message → consumer) | Variable (depends on poll frequency) | Low (message delivered as soon as it arrives, while connection is open) |
| Connection usage | Quick request/response | Held open up to 20 seconds |
| Use case | Rarely useful. Legacy. | **Always preferred.** Set queue default to 20. |

### 7.4 Setting Long Polling

**Per-queue default (recommended):**
```json
// CreateQueue or SetQueueAttributes
{
    "Attributes": {
        "ReceiveMessageWaitTimeSeconds": "20"
    }
}
```

**Per-request override:**
```json
// ReceiveMessage
{
    "WaitTimeSeconds": 20
}
```

The per-request value overrides the queue default. A `WaitTimeSeconds=0` in the request forces short polling even if the queue default is 20.

**Best practice:** Set queue default to 20 seconds. Only override per-request when you have a specific reason (e.g., a time-critical consumer that can't wait).

---

## 8. FIFO-Specific API Behavior

### 8.1 FIFO vs Standard: API Differences

| Behavior | Standard Queue | FIFO Queue |
|----------|---------------|------------|
| `MessageGroupId` | Optional (for X-Ray) | **Required** on every SendMessage |
| `MessageDeduplicationId` | Ignored | Required (or enable content-based dedup on queue) |
| `SequenceNumber` in response | Not present | Present — monotonically increasing per message group |
| `DelaySeconds` per message | Allowed (0–900) | **Not allowed** — must use queue-level delay |
| Throughput | Virtually unlimited | 300 API calls/s (no batching), 3,000 msg/s (with batching), up to 70,000 msg/s (high throughput mode) |
| In-flight limit | ~120,000 | 120,000 |
| Ordering | Best-effort (approximately FIFO) | Strict FIFO within each MessageGroupId |
| Deduplication | None — duplicates are expected | 5-minute dedup window by MessageDeduplicationId |

### 8.2 Message Group IDs — The Ordering Primitive

The `MessageGroupId` is the most important FIFO concept:

```
MessageGroupId = "customer-123"
  → All messages for customer-123 are delivered in strict order
  → While ANY message for customer-123 is in-flight (received, not deleted),
    NO OTHER message for customer-123 is delivered
  → This is a BLOCKING behavior — one slow consumer blocks the group

MessageGroupId = "customer-456"
  → Independent ordering. Not blocked by customer-123's messages.
  → Can be processed in parallel by a different consumer.
```

**Design pattern — partition key as message group ID:**
```
Each customer = one MessageGroupId
  → Strict per-customer ordering (order events arrive in sequence)
  → Parallelism across customers (different consumers handle different customers)
  → If one customer's processing is slow, only that customer is blocked
```

**Anti-pattern — single message group ID:**
```
All messages use MessageGroupId = "all"
  → Entire queue is strictly ordered
  → Only ONE message can be in-flight at a time
  → Throughput = 1 message per visibility-timeout period
  → Queue becomes a bottleneck. Do not do this.
```

### 8.3 Deduplication Mechanics

**Option 1: Explicit MessageDeduplicationId**
```json
{
    "MessageBody": "...",
    "MessageGroupId": "customer-123",
    "MessageDeduplicationId": "order-ORD-789-v1"
}
```
- You control the dedup key
- Useful when you have a natural idempotency key (order ID, transaction ID)
- 5-minute window: same dedup ID within 5 minutes is silently dropped

**Option 2: Content-based deduplication (queue attribute)**
```json
// CreateQueue
{
    "Attributes": {
        "ContentBasedDeduplication": "true"
    }
}
```
- SQS computes SHA-256 hash of message body as dedup ID
- Caveat: if you send the same body with different attributes, it's treated as a duplicate (only body is hashed, not attributes)
- Useful for simple cases where body uniquely identifies the message

### 8.4 High Throughput Mode

FIFO queues have two attributes that unlock higher throughput:

```json
{
    "Attributes": {
        "DeduplicationScope": "messageGroup",
        "FifoThroughputLimit": "perMessageGroupId"
    }
}
```

| Setting | Default | High Throughput |
|---------|---------|-----------------|
| `DeduplicationScope` | `queue` (dedup checked across all groups) | `messageGroup` (dedup checked only within the group) |
| `FifoThroughputLimit` | `perQueue` (300 calls/s shared) | `perMessageGroupId` (300 calls/s per group, up to 70,000 msg/s total) |

**How it works internally:** [INFERRED] When `FifoThroughputLimit` is `perMessageGroupId`, SQS can partition the queue by message group ID across multiple internal servers. Each server handles 300 API calls/s for its assigned groups. With enough distinct message group IDs spread across enough partitions, you approach the 70,000 msg/s aggregate limit.

**Caveat:** High throughput only helps if you have many distinct message group IDs. If you have 1 group ID, you're still limited to 300 calls/s.

---

## 9. Authentication & Authorization

### 9.1 SigV4 Authentication

Every SQS API call must be signed with AWS Signature Version 4. The SDK handles this transparently:

```
Authorization: AWS4-HMAC-SHA256
  Credential=AKIAIOSFODNN7EXAMPLE/20260213/us-east-1/sqs/aws4_request,
  SignedHeaders=content-type;host;x-amz-date;x-amz-target,
  Signature=fe5f80f77d5fa3beca038a248ff027d0445342fe2855ddc963176630326f1024
```

Key elements:
- **Service name:** `sqs` (in the credential scope)
- **Date:** Request must be within 15 minutes of server time
- **HTTPS required:** SQS rejects non-HTTPS requests with `InvalidSecurity`

### 9.2 IAM Policies

IAM policies control who can perform which operations on which queues:

```json
{
    "Version": "2012-10-17",
    "Statement": [
        {
            "Effect": "Allow",
            "Action": [
                "sqs:SendMessage",
                "sqs:SendMessageBatch"
            ],
            "Resource": "arn:aws:sqs:us-east-1:123456789012:order-processing"
        }
    ]
}
```

**Common SQS IAM actions:**

| Action | Description |
|--------|-------------|
| `sqs:SendMessage` | Send (includes SendMessageBatch automatically) |
| `sqs:ReceiveMessage` | Receive messages |
| `sqs:DeleteMessage` | Delete (includes DeleteMessageBatch) |
| `sqs:ChangeMessageVisibility` | Extend/shorten visibility timeout |
| `sqs:GetQueueAttributes` | Read queue metadata |
| `sqs:SetQueueAttributes` | Modify queue configuration |
| `sqs:GetQueueUrl` | Look up queue URL by name |
| `sqs:ListQueues` | List queues |
| `sqs:PurgeQueue` | Delete all messages |
| `sqs:CreateQueue` | Create new queue |
| `sqs:DeleteQueue` | Delete a queue |
| `sqs:TagQueue` / `sqs:UntagQueue` | Manage tags |
| `sqs:ListQueueTags` | Read tags |

**Note:** Setting `sqs:SendMessage` permission automatically grants `sqs:SendMessageBatch`. Same for Delete and ChangeMessageVisibility — the batch variants don't need separate permissions.

### 9.3 Queue Resource-Based Policies

Queues can have resource-based policies (like S3 bucket policies) for cross-account access and service integration:

```json
{
    "Version": "2012-10-17",
    "Statement": [
        {
            "Sid": "AllowSNSToSendMessage",
            "Effect": "Allow",
            "Principal": {
                "Service": "sns.amazonaws.com"
            },
            "Action": "sqs:SendMessage",
            "Resource": "arn:aws:sqs:us-east-1:123456789012:order-processing",
            "Condition": {
                "ArnEquals": {
                    "aws:SourceArn": "arn:aws:sns:us-east-1:123456789012:order-topic"
                }
            }
        }
    ]
}
```

Common use cases:
- **SNS → SQS:** Allow SNS topic to publish messages to the queue
- **S3 → SQS:** Allow S3 bucket to send event notifications
- **Cross-account:** Allow another AWS account to send messages
- **Lambda trigger:** Lambda role needs `sqs:ReceiveMessage`, `sqs:DeleteMessage`, `sqs:GetQueueAttributes`

### 9.4 Encryption

SQS supports two encryption modes:

| Mode | Key Management | Cost | KMS API Calls |
|------|---------------|------|---------------|
| **SSE-SQS** (default for new queues) | SQS manages keys internally | Free | None |
| **SSE-KMS** | Customer-managed KMS key | KMS key cost + per-API-call cost | Every Send/Receive triggers KMS Decrypt/GenerateDataKey |

**SSE-KMS pitfall:** Every `SendMessage` calls `kms:GenerateDataKey`. Every `ReceiveMessage` calls `kms:Decrypt`. At high throughput, this can hit KMS throttling limits (default: 5,500 requests/s per key in most regions). This is a real production issue.

**Mitigation:** Use `KmsDataKeyReusePeriodSeconds` (default 300s, range 60–86,400s) to cache the data key and reduce KMS API calls. Or use SSE-SQS instead if you don't need customer-managed keys.

**What's encrypted:**
- Message body ✓
- Message attributes ✗ (NOT encrypted — this is a gotcha)
- System attributes ✗

---

## 10. Error Handling

### 10.1 Error Response Format

All SQS errors return HTTP 200 at the transport layer (for the JSON protocol — this is different from S3 which uses proper HTTP status codes). The error is in the response body:

```json
{
    "__type": "com.amazonaws.sqs#QueueDoesNotExist",
    "message": "The specified queue does not exist."
}
```

**Wait — HTTP 200 for errors?** Yes. SQS's JSON protocol returns HTTP 200 for the transport and uses the `__type` field to signal errors. The AWS SDKs handle this transparently, but if you're using raw HTTP, you must check the response body. This is unlike S3, which returns proper HTTP 4xx/5xx status codes.

[Note: The newer AWS JSON protocol may return HTTP 400 for client errors. Both patterns exist in the wild depending on SDK version.]

### 10.2 Common Error Codes

| Error | Cause | Action |
|-------|-------|--------|
| `QueueDoesNotExist` | Queue URL is wrong, queue was deleted, or queue is in a different region | Verify URL, check region |
| `RequestThrottled` | Exceeded API rate limit for this queue or account | Exponential backoff with jitter |
| `OverLimit` | In-flight message limit reached (~120K standard, 120K FIFO) | Wait for consumers to delete messages, or increase consumer capacity |
| `InvalidMessageContents` | Message body has invalid Unicode | Encode binary as base64 in a message attribute |
| `MessageNotInflight` | Tried to `ChangeMessageVisibility` on a message that's no longer in-flight | Stop processing — another consumer likely received it |
| `ReceiptHandleIsInvalid` | Receipt handle expired or was for a different message | Get a fresh receipt handle from a new `ReceiveMessage` |
| `QueueNameExists` | `CreateQueue` with same name but different attributes | Use `SetQueueAttributes` to change attributes |
| `PurgeQueueInProgress` | Called `PurgeQueue` twice within 60 seconds | Wait 60 seconds |
| `KmsAccessDenied` | Missing KMS permissions | Add `kms:GenerateDataKey` and/or `kms:Decrypt` to the caller's IAM policy |
| `KmsThrottled` | Too many KMS calls | Increase `KmsDataKeyReusePeriodSeconds`, or switch to SSE-SQS |

### 10.3 Retry Strategy

SQS API errors fall into two categories:

| Error Type | Examples | Retry? | Strategy |
|------------|----------|--------|----------|
| Client errors (sender fault) | `InvalidMessageContents`, `QueueDoesNotExist` | No | Fix the request |
| Throttling errors | `RequestThrottled`, `KmsThrottled` | Yes | Exponential backoff with jitter. Start at 100ms, cap at 20s. |
| Server errors | `InternalError` (rare) | Yes | Exponential backoff. Start at 500ms. |
| `OverLimit` | In-flight limit | Wait | Reduce consumer concurrency or increase delete rate |

AWS SDKs implement automatic retries with exponential backoff for throttling and server errors. The default retry count varies by SDK (typically 3–5 retries).

---

## 11. Rate Limits & Throttling

### 11.1 API Request Rates

| Queue Type | Limit | Notes |
|------------|-------|-------|
| **Standard queue** | **Virtually unlimited** API calls/second | SQS auto-scales. No published hard limit for standard queue API throughput. |
| **FIFO queue (default)** | **300 API calls/second** (send + receive + delete combined) | Across all operations. With batching (10 per batch): effectively 3,000 msg/s. |
| **FIFO queue (high throughput)** | **Up to 70,000 messages/second** | With `FifoThroughputLimit=perMessageGroupId`, limit is per message group. Requires many distinct message group IDs. |

### 11.2 Account-Level Limits

| Quota | Default | Adjustable? |
|-------|---------|-------------|
| Queues per account per region | No hard limit published | — |
| In-flight messages (standard) | ~120,000 per queue | Contact AWS support |
| In-flight messages (FIFO) | 120,000 per queue | Contact AWS support |
| Max message size | 256 KB | No (use SQS Extended Client Library for up to 2 GB via S3) |
| Max message retention | 14 days | No |
| Max visibility timeout | 12 hours | No |
| Max delay | 15 minutes | No |
| Max batch size | 10 | No |
| Max message attributes | 10 per message | No |
| Max long poll wait | 20 seconds | No |
| Tags per queue | 50 (recommended max) | — |

### 11.3 Throughput Optimization Strategies

| Strategy | Throughput Gain | When to Use |
|----------|-----------------|-------------|
| **Batching** | 10x (batch of 10) | Always. No reason not to batch. |
| **Long polling** | No throughput gain, but reduces wasted calls | Always. Set `ReceiveMessageWaitTimeSeconds=20`. |
| **Multiple consumers** | Linear with consumer count | When single consumer can't keep up |
| **Multiple queues** | Horizontal sharding | When a single queue's throughput is insufficient (rare for standard) |
| **FIFO high throughput mode** | Up to 70K msg/s | When FIFO is needed but default 300 calls/s is too low |
| **Avoid SSE-KMS** | Eliminates KMS bottleneck | When KMS call rate is the bottleneck |

---

## 12. Design Decisions Summary

| Decision | SQS Choice | Why | Alternative Considered |
|----------|-----------|-----|----------------------|
| Protocol | JSON RPC (POST + X-Amz-Target) | Message operations have side effects — doesn't fit REST verbs | REST (like S3). Awkward for stateful operations like ReceiveMessage. |
| Resource ID | Queue URL (region + account + name) | Prevents cross-region mistakes, enables cross-account access | Queue name only. Ambiguous across regions/accounts. |
| Deletion mechanism | ReceiptHandle (not MessageId) | Handles duplicate receives gracefully. Ties delete to specific receive. | MessageId. Ambiguous when message is received multiple times. |
| Message size | 256 KB hard limit | Keeps SQS fast — messages stored inline. Large payloads go to S3 (Extended Client Library). | Larger limit. Would slow storage/replication. |
| Polling model | Short poll (default) + long poll (opt-in) | Backward compatibility. Long poll wasn't available at launch. | Long poll as default. Would be better, but breaking change. |
| Visibility timeout | Per-message on receive, extendable | Different consumers may need different processing times | Per-queue only. Inflexible for heterogeneous workloads. |
| FIFO ordering scope | Per MessageGroupId (not per queue) | Allows parallelism across groups while maintaining per-group order | Per-queue ordering. Too restrictive — one slow message blocks everything. |
| Deduplication | 5-minute window with explicit ID or content hash | Covers network retries (< 5 min) without unbounded memory | Longer window (more memory). Shorter window (misses more dupes). Permanent dedup (infinite memory). |
| DLQ | Configured on source queue, not on the DLQ | Source queue owns the retry policy — makes sense organizationally | Configure on DLQ. Then DLQ would need to know about all sources. |
| Encryption | SSE-SQS (managed) as default | Security by default, zero configuration | No encryption default (legacy behavior). Risky. |

---

## Appendix: Complete API Action List

| Action | Category | Description |
|--------|----------|-------------|
| `SendMessage` | Message | Send a single message |
| `SendMessageBatch` | Message | Send up to 10 messages |
| `ReceiveMessage` | Message | Receive up to 10 messages |
| `DeleteMessage` | Message | Delete a single message |
| `DeleteMessageBatch` | Message | Delete up to 10 messages |
| `ChangeMessageVisibility` | Message | Extend/shorten visibility timeout |
| `ChangeMessageVisibilityBatch` | Message | Batch visibility change |
| `CreateQueue` | Queue Mgmt | Create a queue |
| `DeleteQueue` | Queue Mgmt | Delete a queue |
| `PurgeQueue` | Queue Mgmt | Delete all messages |
| `GetQueueUrl` | Queue Mgmt | Get URL from name |
| `GetQueueAttributes` | Queue Mgmt | Read queue config/metrics |
| `SetQueueAttributes` | Queue Mgmt | Modify queue config |
| `ListQueues` | Queue Mgmt | List queues |
| `ListQueueTags` | Tagging | List tags |
| `TagQueue` | Tagging | Add tags |
| `UntagQueue` | Tagging | Remove tags |
| `ListDeadLetterSourceQueues` | DLQ | List queues using this queue as DLQ |
| `StartMessageMoveTask` | DLQ Redrive | Start moving messages from DLQ |
| `GetMessageMoveTask` | DLQ Redrive | Check redrive task status |
| `CancelMessageMoveTask` | DLQ Redrive | Cancel a running redrive |
