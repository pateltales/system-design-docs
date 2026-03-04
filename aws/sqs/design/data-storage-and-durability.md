# Amazon SQS — Data Storage & Durability

> Companion deep dive to the [interview simulation](interview-simulation.md). This document explores how SQS stores messages, achieves durability across AZs, handles message retention and encryption, and what the durability guarantees actually mean.

---

## Table of Contents

1. [Storage Architecture Overview](#1-storage-architecture-overview)
2. [Multi-AZ Replication](#2-multi-az-replication)
3. [Message Storage Lifecycle](#3-message-storage-lifecycle)
4. [Message Size and Extended Storage](#4-message-size-and-extended-storage)
5. [Message Retention](#5-message-retention)
6. [In-Flight Message Limits](#6-in-flight-message-limits)
7. [Server-Side Encryption](#7-server-side-encryption)
8. [Durability Guarantees — What They Mean](#8-durability-guarantees--what-they-mean)
9. [Failure Modes and Recovery](#9-failure-modes-and-recovery)
10. [SQS vs Other Storage Systems — Durability Comparison](#10-sqs-vs-other-storage-systems--durability-comparison)
11. [Design Decisions Summary](#11-design-decisions-summary)

---

## 1. Storage Architecture Overview

### 1.1 What AWS Tells Us (Official)

AWS states the following about SQS storage:

> "Amazon SQS stores all message queues and messages within a single, highly-available AWS region with multiple redundant Availability Zones (AZs), so no single computer, network, or AZ failure can make messages inaccessible."

> "For the safety of your messages, Amazon SQS stores them on multiple servers."

> "The queue is distributed across Amazon SQS servers."

That's the extent of official documentation on SQS storage internals. Unlike S3 (which publishes its 11-nines durability and erasure coding details) or DynamoDB (which published a USENIX paper about Paxos replication), SQS has never disclosed its internal storage architecture in detail.

### 1.2 What We Can Infer [INFERRED]

Based on the official statements and SQS's operational characteristics, we can infer the following:

**Storage model:**
- SQS is NOT a traditional database or filesystem — it's a purpose-built message storage system
- Messages are stored on dedicated SQS infrastructure (not on DynamoDB or S3 internally, as far as is publicly known)
- Each queue is partitioned across multiple internal servers for throughput and availability
- Messages are replicated across multiple AZs before `SendMessage` returns success

**Partitioning:**
- Standard queues are partitioned across multiple internal servers/shards
- This explains why short polling queries "a subset of servers" — each server holds a portion of the queue's messages
- The number of partitions likely scales with throughput demand
- FIFO queues are also partitioned (since high-throughput mode mentions 3,000 messages/second per partition)

**Replication:**
- Messages are "redundantly stored across multiple AZs"
- The replication is synchronous — `SendMessage` blocks until replication is confirmed, then returns HTTP 200
- At minimum, messages are replicated to 2 AZs (since AWS guarantees no single AZ failure causes data loss)
- Likely 3 AZs for additional safety [INFERRED]

### 1.3 Architectural Diagram [INFERRED]

```
AWS Region (e.g., us-east-1)
┌─────────────────────────────────────────────────────────────────────────┐
│                                                                         │
│  ┌──────────────────┐   ┌──────────────────┐   ┌──────────────────┐    │
│  │  Availability     │   │  Availability     │   │  Availability     │    │
│  │  Zone A           │   │  Zone B           │   │  Zone C           │    │
│  │                   │   │                   │   │                   │    │
│  │  ┌─────────────┐  │   │  ┌─────────────┐  │   │  ┌─────────────┐  │    │
│  │  │ SQS Server  │  │   │  │ SQS Server  │  │   │  │ SQS Server  │  │    │
│  │  │ Partition 1 │  │   │  │ Partition 1  │  │   │  │ Partition 1  │  │    │
│  │  │ (replica)   │  │   │  │ (replica)    │  │   │  │ (replica)    │  │    │
│  │  └─────────────┘  │   │  └─────────────┘  │   │  └─────────────┘  │    │
│  │  ┌─────────────┐  │   │  ┌─────────────┐  │   │  ┌─────────────┐  │    │
│  │  │ SQS Server  │  │   │  │ SQS Server  │  │   │  │ SQS Server  │  │    │
│  │  │ Partition 2 │  │   │  │ Partition 2  │  │   │  │ Partition 2  │  │    │
│  │  │ (replica)   │  │   │  │ (replica)    │  │   │  │ (replica)    │  │    │
│  │  └─────────────┘  │   │  └─────────────┘  │   │  └─────────────┘  │    │
│  │                   │   │                   │   │                   │    │
│  └──────────────────┘   └──────────────────┘   └──────────────────┘    │
│                                                                         │
│  ┌─────────────────────────────────────────────────┐                    │
│  │ SQS Front-End Fleet (load-balanced, stateless)  │                    │
│  │ Routes requests to correct partition + replica   │                    │
│  └─────────────────────────────────────────────────┘                    │
│                                                                         │
└─────────────────────────────────────────────────────────────────────────┘
```

[INFERRED — this is a reasonable model based on official statements but NOT officially documented]

---

## 2. Multi-AZ Replication

### 2.1 The Durability Contract

When `SendMessage` returns HTTP 200, SQS guarantees:

1. **The message will survive any single AZ failure** — if an entire data center goes offline, your message is safe in other AZs
2. **The message will survive any single server failure** — messages are on multiple servers
3. **The message will be available for consumers** until the retention period expires or it's explicitly deleted

This is a strong guarantee, and it requires synchronous replication before the API responds.

### 2.2 Write Path (Replication) [INFERRED]

```
SendMessage arrives at SQS Front-End
       │
       ▼
┌──────────────────┐
│ 1. Validate &    │  Check message size, attributes, permissions.
│    Prepare       │  Encrypt if SSE is enabled.
└──────┬───────────┘
       │
       ▼
┌──────────────────┐
│ 2. Route to      │  Determine which partition handles this message.
│    Partition      │  Standard: hash-based or round-robin to spread load.
│                  │  FIFO: route by MessageGroupId for ordering.
└──────┬───────────┘
       │
       ▼
┌──────────────────────────────────────────────────┐
│ 3. Replicate across AZs (SYNCHRONOUS)            │
│                                                   │
│  ┌─────────┐    ┌─────────┐    ┌─────────┐      │
│  │  AZ-A   │    │  AZ-B   │    │  AZ-C   │      │
│  │  Write  │    │  Write  │    │  Write  │      │
│  │  ✓ ACK  │    │  ✓ ACK  │    │  ✓ ACK  │      │
│  └─────────┘    └─────────┘    └─────────┘      │
│                                                   │
│  Wait for sufficient ACKs (likely 2 of 3)        │
│  [INFERRED — could be all 3, or quorum of 2]    │
└──────┬───────────────────────────────────────────┘
       │
       ▼
┌──────────────────┐
│ 4. Return 200 OK │  Message is now durable.
│    with MessageId│  Cannot be lost by infrastructure failure.
└──────────────────┘
```

### 2.3 Why Synchronous Replication Matters

**Synchronous replication** means the producer doesn't get a success response until the message is safely stored in multiple AZs. This is in contrast to:

| Approach | Behavior | Risk |
|----------|----------|------|
| **Synchronous (SQS's approach)** | Write to multiple AZs before responding | Higher latency (~5-20ms), but message is guaranteed durable on success |
| **Asynchronous** | Write to one AZ, respond immediately, replicate in background | Lower latency, but message can be lost if the one AZ fails before replication completes |
| **Write-ahead log** | Write to local log, respond, replicate later | Even lower latency, but durability gap |

SQS chose synchronous replication because message loss is unacceptable for a queue service. If a producer gets HTTP 200 but the message is lost, the system's reliability contract is broken. The 5-20ms latency premium is the price of this guarantee.

### 2.4 Read Path [INFERRED]

```
ReceiveMessage arrives at SQS Front-End
       │
       ▼
┌──────────────────┐
│ 1. Route to      │  Standard: pick a subset of partitions (short poll)
│    Partition(s)   │  or all partitions (long poll).
│                  │  FIFO: route to partitions that have messages
│                  │  in available message groups.
└──────┬───────────┘
       │
       ▼
┌──────────────────┐
│ 2. Read from     │  Read from the nearest AZ replica for lowest latency.
│    nearest AZ    │  [INFERRED — SQS likely reads from the closest AZ
│                  │  to minimize cross-AZ data transfer]
└──────┬───────────┘
       │
       ▼
┌──────────────────┐
│ 3. Set visibility│  Mark message as in-flight across all replicas.
│    timeout       │  This must be replicated so that no AZ serves
│                  │  the message to another consumer.
│                  │  [INFERRED — visibility state must be consistent
│                  │  across replicas for correctness]
└──────┬───────────┘
       │
       ▼
┌──────────────────┐
│ 4. Return message│  Decrypt if encrypted. Return with ReceiptHandle.
└──────────────────┘
```

### 2.5 Consistency Model

SQS uses **eventual consistency** for some operations:

| Operation | Consistency | Why |
|-----------|-------------|-----|
| `SendMessage` → `ReceiveMessage` | Eventually consistent | A message just sent might not be immediately visible on all partitions. Short polling may miss it. Long polling queries all partitions. |
| `GetQueueAttributes` (message counts) | Eventually consistent | `ApproximateNumberOfMessages` is an approximation — aggregating counts across partitions is expensive |
| `DeleteMessage` | Strongly consistent (on the receiving partition) | Once deleted, the message won't be returned again from that partition. But in rare cases with standard queues, a different replica might still serve it (at-least-once). |
| FIFO ordering | Strongly consistent (within message group) | FIFO groups are managed on a single partition, ensuring strict ordering [INFERRED] |

---

## 3. Message Storage Lifecycle

### 3.1 States of a Message in Storage

A message in SQS goes through several storage states:

```
┌─────────────────────────────────────────────────────────────────────┐
│                    MESSAGE IN STORAGE                               │
│                                                                     │
│  State            Visible to     On disk?    Occupies space?       │
│  ─────────────    ──────────     ────────    ──────────────        │
│  DELAYED          No              Yes         Yes                   │
│  AVAILABLE        Yes             Yes         Yes                   │
│  IN-FLIGHT        No              Yes         Yes                   │
│  DELETED          No              Pending GC  Pending GC           │
│  EXPIRED          No              Pending GC  Pending GC           │
│                                                                     │
└─────────────────────────────────────────────────────────────────────┘
```

### 3.2 What "Deleted" Actually Means [INFERRED]

When you call `DeleteMessage`:

1. SQS marks the message as deleted in its internal state
2. The physical storage is NOT immediately reclaimed (would be too expensive to do synchronously)
3. Background garbage collection processes clean up deleted messages asynchronously
4. Until GC runs, the message occupies storage space but is no longer accessible via the API

This is similar to how most databases handle deletes — logical deletion is fast, physical reclamation happens in the background.

### 3.3 Storage Accounting

What counts toward your queue's storage:

| Counted | Not Counted |
|---------|-------------|
| Message body (up to 256 KB) | System attributes (`SenderId`, etc.) |
| Message attributes (names + values) | Message metadata managed by SQS |
| Encrypted message overhead | |

SQS does not publish per-queue storage quotas. The documentation says: "Messages per queue (backlog): **Unlimited**". This means SQS handles storage scaling transparently — you can accumulate billions of messages without hitting a storage limit.

---

## 4. Message Size and Extended Storage

### 4.1 Message Size Limits

| Limit | Value | Notes |
|-------|-------|-------|
| Minimum message body | 1 byte (1 character) | — |
| Maximum message body | 256 KB (262,144 bytes) | Includes message body ONLY |
| Maximum with attributes | 256 KB total | Body + all attribute names + values + data types must fit in 256 KB |
| Maximum attributes | 10 per message | Each attribute has a name (max 256 chars), type, and value |
| Batch total | 256 KB per batch | Combined size of all messages in a `SendMessageBatch` |

### 4.2 Why 256 KB?

[INFERRED — reasoning based on architectural tradeoffs]

The 256 KB limit is a deliberate design choice that enables several properties:

1. **Messages are stored inline** — no pointer indirection needed. A single read from storage retrieves the complete message. This keeps latency low (~5-20ms).

2. **Replication is fast** — replicating 256 KB across AZs takes < 1ms on AWS's internal network. Larger messages would increase replication latency linearly.

3. **Batch operations work** — 10 messages × 256 KB = 2.56 MB per batch. This is a reasonable network payload. If messages were 10 MB each, a batch would be 100 MB.

4. **Memory management** — SQS likely keeps message metadata (and possibly small messages) in memory for fast access. A 256 KB cap keeps per-message memory footprint predictable.

### 4.3 SQS Extended Client Library

For messages larger than 256 KB (up to 2 GB), AWS provides the SQS Extended Client Library:

```
Producer                    SQS                    S3
  │                          │                      │
  │  Large message (5 MB)    │                      │
  │                          │                      │
  │─── Upload body to S3 ───│──────────────────────>│
  │                          │                      │
  │<── S3 key returned ─────│──────────────────────│
  │                          │                      │
  │─── SendMessage ─────────>│                      │
  │   (body = S3 pointer:    │                      │
  │    {"s3BucketName":      │                      │
  │     "my-bucket",         │                      │
  │     "s3Key": "msg/123"}) │                      │
  │                          │                      │
  │<── 200 OK ───────────────│                      │
```

```
Consumer                    SQS                    S3
  │                          │                      │
  │─── ReceiveMessage ──────>│                      │
  │<── msg with S3 pointer ──│                      │
  │                          │                      │
  │─── GET S3 object ───────│──────────────────────>│
  │<── 5 MB body ───────────│──────────────────────│
  │                          │                      │
  │─── DeleteMessage ───────>│                      │
  │─── DELETE S3 object ────│──────────────────────>│
```

**Important tradeoffs:**

| Factor | SQS-only (≤256 KB) | SQS + S3 Extended (≤2 GB) |
|--------|--------------------|-----------------------------|
| Latency | 5-20ms | 50-500ms (S3 upload + download added) |
| Cost | SQS per-request only | SQS + S3 PUT/GET + S3 storage |
| Durability | SQS multi-AZ | SQS multi-AZ + S3 11-nines |
| Complexity | Simple | Must manage S3 cleanup (delete object after processing) |
| Atomic deletion | `DeleteMessage` removes everything | Must delete from both SQS and S3 |
| Encryption | SSE-SQS or SSE-KMS | SSE for SQS pointer + separate S3 encryption for body |

### 4.4 Message Content Restrictions

SQS only accepts specific Unicode characters in the message body:

```
Allowed: #x9 | #xA | #xD | #x20 to #xD7FF | #xE000 to #xFFFD | #x10000 to #x10FFFF
```

This means:
- **JSON:** ✓ (standard JSON uses allowed characters)
- **XML:** ✓
- **Plain text:** ✓
- **Binary data:** ✗ (raw bytes may contain disallowed characters)
- **Base64-encoded binary:** ✓ (only uses alphanumeric + /+=)

**For binary data:** Use a Binary message attribute (`DataType: "Binary"`) instead of putting it in the body. Or base64-encode it.

---

## 5. Message Retention

### 5.1 Retention Configuration

| Setting | Value |
|---------|-------|
| Default retention | 4 days (345,600 seconds) |
| Minimum retention | 60 seconds (1 minute) |
| Maximum retention | 14 days (1,209,600 seconds) |
| Configurable? | Yes, via `MessageRetentionPeriod` queue attribute |
| Granularity | Per-queue (not per-message) |

### 5.2 What Happens When Retention Expires

```
Message sent at T=0
Queue retention = 4 days

T=0 to T=4 days: Message available for receive (if not currently in-flight)
T=4 days: SQS automatically deletes the message
  → No notification to producer or consumer
  → No DLQ — just gone
  → This is silent data loss if nobody processed the message
```

**This is different from DLQ behavior.** A DLQ catches messages that FAIL processing (receive count exceeds `maxReceiveCount`). Retention expiry catches messages that were NEVER processed or not processed fast enough.

### 5.3 Retention and DLQ Interaction

```
Scenario: maxReceiveCount=5, source retention=4 days, DLQ retention=14 days

Case 1: Message fails 5 times, then moves to DLQ
  → Standard queue: Original SentTimestamp preserved.
    DLQ retention counts from original timestamp.
    If source retention=4 days and DLQ retention=14 days,
    message lives 14 days from send time total.

  → FIFO queue: Enqueue timestamp RESETS.
    DLQ retention counts from when message entered DLQ.
    Message gets a fresh 14 days in the DLQ.

Case 2: Message is never received (queue is backed up)
  → Source queue retention expires
  → Message is auto-deleted. NOT moved to DLQ.
  → DLQ only catches FAILED processing, not backlog overflow.
```

**Best practice:** Set `DLQ retention period ≥ source queue retention period`. Otherwise, a message that's been in the source queue for 3 days, then moves to a DLQ with 4-day retention, only has 1 day in the DLQ before being auto-deleted (in standard queues where the original timestamp is preserved).

### 5.4 Monitoring Retention Risk

Use CloudWatch to detect queues where messages are aging close to retention:

```
CloudWatch Metric: ApproximateAgeOfOldestMessage
  → Tracks the age of the oldest message in the queue
  → Alarm when age > 75% of retention period

Example:
  Retention = 4 days (345,600 seconds)
  Alarm when ApproximateAgeOfOldestMessage > 259,200 seconds (3 days)
  → Indicates consumers are falling behind and messages may be lost
```

---

## 6. In-Flight Message Limits

### 6.1 What "In-Flight" Means

A message is "in-flight" when it has been received by a consumer but not yet deleted. It's invisible to other consumers during the visibility timeout. The in-flight count is the total number of such messages across all consumers of a queue.

### 6.2 Limits

| Queue Type | In-Flight Limit | What Happens When Exceeded |
|------------|----------------|---------------------------|
| Standard | ~120,000 per queue | `OverLimit` error on `ReceiveMessage`. Long polling returns empty (no error). |
| FIFO | 120,000 per queue | Same behavior |

### 6.3 Why the Limit Exists [INFERRED]

In-flight messages require state tracking:
- For each in-flight message, SQS must track: which consumer received it, when it was received, when the visibility timeout expires, and the `ReceiptHandle`
- This state must be replicated across AZs (otherwise, an AZ failure could make in-flight messages visible again prematurely)
- 120,000 is a practical limit that balances memory/storage overhead against typical workloads

### 6.4 What Causes In-Flight Buildup

```
Healthy queue:
  Receive rate ≈ Delete rate
  In-flight count stays low (e.g., 100-1,000)

Unhealthy queue:
  Receive rate >> Delete rate
  Consumers are receiving but not deleting (or deleting slowly)
  In-flight count climbs toward 120,000

  Common causes:
  1. Consumer is crashing before DeleteMessage
  2. Visibility timeout is too long (messages stay in-flight longer than needed)
  3. Processing is genuinely slow (e.g., calling a slow downstream API)
  4. Consumer is receiving messages but not processing them (polling too aggressively)
```

### 6.5 Remediation

| Action | How It Helps |
|--------|-------------|
| Fix consumer crashes | Messages get deleted after processing, freeing in-flight capacity |
| Reduce visibility timeout | Messages become visible sooner after failure, but risk of duplicate processing increases |
| Scale up consumers | More consumers = faster processing = faster deletion = lower in-flight count |
| Use batch delete | Reduces per-message overhead, frees in-flight capacity faster |

---

## 7. Server-Side Encryption

### 7.1 Encryption Model

SQS uses **envelope encryption:**

```
┌─────────────────────────────────────────────┐
│           ENVELOPE ENCRYPTION               │
│                                             │
│  KMS Master Key (CMK)                       │
│       │                                     │
│       ▼ GenerateDataKey                     │
│  ┌──────────────┐                           │
│  │ Data Key     │  (plaintext + encrypted)  │
│  │ (DEK)        │                           │
│  └──────┬───────┘                           │
│         │                                   │
│         ▼ Encrypt                           │
│  ┌──────────────┐                           │
│  │ Encrypted    │  (stored alongside       │
│  │ Message Body │   encrypted DEK)          │
│  └──────────────┘                           │
│                                             │
│  To decrypt: KMS decrypts the DEK,          │
│  then DEK decrypts the message body.        │
└─────────────────────────────────────────────┘
```

### 7.2 SSE-SQS vs SSE-KMS

| Feature | SSE-SQS | SSE-KMS |
|---------|---------|---------|
| Key management | SQS manages keys internally | Customer or AWS managed KMS keys |
| Cost | Free (included with SQS) | KMS key cost + per-API-call cost |
| KMS API calls | None | `GenerateDataKey` on send, `Decrypt` on receive |
| Key rotation | Automatic, transparent | Customer-controlled or automatic |
| Audit trail | No CloudTrail for key usage | Full CloudTrail logging via KMS |
| Cross-account Lambda | Works | Does NOT work with default AWS-managed KMS key |
| Configuration | Default for new queues | Explicit opt-in |

### 7.3 What Is and Isn't Encrypted

| Component | Encrypted? | Notes |
|-----------|-----------|-------|
| Message body | **Yes** | Encrypted at rest and decrypted on receive |
| Message attributes (custom) | **No** | Names, types, and values are in plaintext |
| System attributes | **No** | `SenderId`, `SentTimestamp`, etc. are unencrypted |
| Queue metadata | **No** | Queue name, attributes, ARN are unencrypted |
| Message ID | **No** | Visible in API responses |
| Receipt Handle | **No** | Visible to consumer |

**Security implication:** If you put sensitive data in message attributes (e.g., `SSN: "123-45-6789"` as an attribute), it is NOT encrypted. Only the body is encrypted. Put sensitive data in the body.

### 7.4 Data Key Caching (`KmsDataKeyReusePeriodSeconds`)

Each `SendMessage` needs a data key to encrypt the body. Calling KMS for every message would be:
- Expensive ($0.03 per 10,000 KMS requests)
- Slow (adds 5-20ms per KMS call)
- Throttle-prone (KMS default limit: 5,500 requests/sec per key)

Solution: Cache the data key and reuse it.

| Setting | Value | Implication |
|---------|-------|-------------|
| Min | 60 seconds | New KMS key every minute. Most secure, but most KMS calls. |
| Default | 300 seconds (5 min) | Reasonable balance. |
| Max | 86,400 seconds (24 hours) | Fewest KMS calls, but key material lives in memory longer. |

**Resilience benefit:** If KMS becomes temporarily unreachable, SQS continues using the cached data key. This prevents a KMS outage from cascading into an SQS outage. [Documented]

### 7.5 Encryption and DLQ

When a message moves from a source queue to a DLQ:

| Source Queue | DLQ | Message Encryption State |
|-------------|-----|-------------------------|
| Encrypted | Unencrypted | Message REMAINS encrypted (it's not re-encrypted or decrypted) |
| Unencrypted | Encrypted | Message REMAINS unencrypted (DLQ encryption only applies to new messages sent to the DLQ) |
| Encrypted (Key A) | Encrypted (Key B) | Message stays encrypted with Key A (not re-encrypted with Key B) |

**Important:** Encryption is applied at send time and stays with the message. Moving to a DLQ is an internal transfer, not a new send.

### 7.6 Encryption and Backlog

If you enable encryption on an existing queue with messages already in it:

- **Existing messages:** NOT encrypted (they were stored before encryption was enabled)
- **New messages:** Encrypted
- **Disabling encryption:** Previously encrypted messages remain encrypted (you can still receive and decrypt them)

This means a queue can have a mix of encrypted and unencrypted messages during a transition period.

---

## 8. Durability Guarantees — What They Mean

### 8.1 SQS's Durability Promise

SQS guarantees:

1. **Message durability:** Once `SendMessage` returns HTTP 200, the message is stored across multiple AZs and will not be lost by infrastructure failure.

2. **Queue durability:** Queues persist until explicitly deleted. They are not affected by AZ failures.

3. **At-least-once delivery (standard):** Every message will be delivered at least once to a consumer (assuming the consumer calls `ReceiveMessage` within the retention period).

4. **Exactly-once delivery (FIFO):** FIFO queues additionally guarantee no duplicate delivery within a message group.

### 8.2 What SQS Does NOT Guarantee

| Not Guaranteed | Explanation |
|----------------|-------------|
| Specific durability number (e.g., 11-nines) | SQS does not publish a durability SLA like S3's 99.999999999%. It promises "stored across multiple AZs" without quantifying the probability of loss. |
| Zero message loss under all conditions | Messages can be lost by: retention expiry, PurgeQueue, DeleteQueue, or customer calling DeleteMessage. These are intentional, not infrastructure failures. |
| Ordering (standard queues) | Standard queues provide best-effort ordering only. Messages may arrive out of order. |
| Exactly-once delivery (standard) | Standard queues are at-least-once. Duplicates are possible. |
| Point-in-time recovery | There's no "undelete" or "restore to 5 minutes ago." Once a message is deleted or expired, it's gone. |

### 8.3 SQS vs S3 vs DynamoDB Durability Comparison

| Service | Durability Guarantee | Replication | Recovery Options |
|---------|---------------------|-------------|-----------------|
| **S3** | 99.999999999% (11 nines) | Erasure coding across ≥3 AZs | Versioning, MFA delete, cross-region replication |
| **DynamoDB** | Not published (implied high) | Paxos, 3 replicas across 3 AZs | Point-in-time recovery (PITR), on-demand backup, global tables |
| **SQS** | "Multiple AZs" (no number) | Synchronous multi-AZ replication | DLQ for failed messages. No backup/restore. |

**Why the difference?** S3 and DynamoDB store **permanent data** — your photos, your database records. Losing them is catastrophic. SQS stores **ephemeral messages** — they're processed and deleted. The durability concern is about the window between send and delete, not long-term storage.

---

## 9. Failure Modes and Recovery

### 9.1 AZ Failure

```
Before AZ failure:
  AZ-A: [msg-1, msg-2, msg-3]  ✓
  AZ-B: [msg-1, msg-2, msg-3]  ✓
  AZ-C: [msg-1, msg-2, msg-3]  ✓

AZ-B goes offline:
  AZ-A: [msg-1, msg-2, msg-3]  ✓
  AZ-B: [msg-1, msg-2, msg-3]  ✗ (unreachable)
  AZ-C: [msg-1, msg-2, msg-3]  ✓

Impact:
  - No message loss (messages exist in AZ-A and AZ-C)
  - Reads/writes continue via AZ-A and AZ-C
  - Slightly higher latency (fewer nodes to spread load)
  - No customer action needed

AZ-B recovers:
  - Catches up with missed writes
  - Returns to normal operation
```

[INFERRED — based on AWS's general multi-AZ architecture pattern]

### 9.2 Network Partition

```
Producer ──X── SQS Front-End (network timeout)

Impact:
  - SendMessage call times out
  - Producer gets a network error
  - Message may or may not have been stored (unknown state)

Recovery:
  - Standard queue: Retry SendMessage. May create a duplicate. Acceptable for at-least-once.
  - FIFO queue: Retry with same MessageDeduplicationId. If the original was stored, the retry
    is deduplicated. If not, the retry creates the message. Either way: exactly one message.
    This is why FIFO dedup IDs exist.
```

### 9.3 Consumer Failure

| Failure Type | Impact | Recovery |
|-------------|--------|----------|
| Consumer crashes | In-flight messages return to visible after timeout | Another consumer processes them |
| Consumer hangs | Same as crash (eventually times out) | Consider shorter visibility timeout or heartbeat pattern |
| Consumer processes but fails to delete | Message redelivered (at-least-once). Consumer must be idempotent. | Fix consumer to always delete after successful processing |
| Consumer network partition | ReceiveMessage times out. Consumer may be processing a message it can't delete. | Visibility timeout expires → redelivery to another consumer |

### 9.4 SQS Service Degradation [INFERRED]

In extreme cases (major regional infrastructure event), SQS might:

| Scenario | Behavior |
|----------|----------|
| Single AZ degraded | SQS continues from other AZs. May see slightly elevated latency. |
| Control plane degraded | Existing queues continue working (data plane is separate). Creating/modifying queues may fail. |
| Storage node failure | Individual messages on that node are served from replicas. No data loss. |
| KMS degraded (SSE-KMS) | Cached data keys keep working for `KmsDataKeyReusePeriodSeconds`. After that, sends/receives fail with KMS errors. SSE-SQS queues are unaffected. |

---

## 10. SQS vs Other Storage Systems — Durability Comparison

### 10.1 Fundamental Difference

SQS is **ephemeral storage** — messages are meant to be processed and deleted. This changes the durability calculus:

```
S3/DynamoDB: Data is stored indefinitely until explicitly removed
  → Durability is about surviving decades of hardware failures
  → Need erasure coding, Paxos, 11-nines

SQS: Messages are stored for minutes to days, then deleted
  → Durability is about surviving the send-to-delete window
  → Need multi-AZ replication for the duration of retention
  → Don't need 11-nines because messages aren't stored for years
```

### 10.2 Comparison Table

| Property | SQS | Kafka (MSK) | RabbitMQ | Redis Streams |
|----------|-----|-------------|----------|---------------|
| Storage model | Multi-AZ replicated, managed | Append-only log, replicated across brokers | Write-ahead log, optional disk persistence, mirrored queues | In-memory, optional AOF/RDB persistence |
| Durability guarantee | "Multiple AZs" | Depends on `acks` and `min.insync.replicas` | Depends on mirroring config | Depends on persistence config |
| Default durability | High (always multi-AZ) | Configurable (can be weak) | Configurable (can be weak) | Low (memory-only default) |
| Max retention | 14 days | Unlimited (disk-backed) | Until consumed + TTL | Unlimited (but memory-bound) |
| Message size | 256 KB | 1 MB default (configurable) | No hard limit (practical ~128 MB) | 512 MB default |
| Backlog capacity | Unlimited | Disk-bound | Memory/disk-bound | Memory-bound |

---

## 11. Design Decisions Summary

| Decision | SQS Choice | Why | Alternative Considered |
|----------|-----------|-----|----------------------|
| Replication | Synchronous multi-AZ | Durability guarantee on HTTP 200 response. No window for data loss. | Async replication. Faster writes but risk of loss. |
| Storage backend | Purpose-built (not S3/DynamoDB) [INFERRED] | Optimized for message lifecycle: fast write, fast read, fast delete, automatic expiry. General-purpose stores have wrong performance profile. | DynamoDB (too expensive for ephemeral data). S3 (too slow for queue semantics — no visibility timeout). |
| Message size limit | 256 KB | Keeps messages inline (no pointer chasing). Fast replication. Predictable latency. | Larger limit. Would slow replication and increase storage cost for ephemeral data. |
| Retention policy | Per-queue, max 14 days | Simple. Messages are ephemeral — 14 days is more than enough for any reasonable processing pipeline. | Per-message retention. More flexible but more complex to implement and manage. |
| Encryption | SSE-SQS as default | Security by default with zero configuration and zero cost. | No encryption default (legacy behavior). Risky for compliance. |
| Body-only encryption | Only body is encrypted, not attributes | Attributes are used for routing (SNS filters, Lambda event source mapping filters). Encrypting them would break these integrations. | Full encryption. Would require decrypting attributes at every routing decision point. |
| Approximate counts | `ApproximateNumberOfMessages` | Exact counts require distributed consensus reads across all partitions — too expensive for a metric that's queried frequently. | Exact counts. Would require a centralized counter or coordinated reads, hurting throughput. |
| In-flight limit | ~120,000 per queue | In-flight state tracking has memory cost. 120K is sufficient for virtually all workloads. | No limit. Would require unbounded memory for tracking. |
