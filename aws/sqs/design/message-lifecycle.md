# Deep Dive: SQS Message Lifecycle

*Companion document to [interview-simulation.md](interview-simulation.md)*

---

## 1. Message State Machine

Every SQS message transitions through a well-defined set of states. Understanding this state machine is the foundation of understanding SQS.

```
                                           ┌─────────────────────────────────────────────┐
   SendMessage()                           │                                             │
       │                                   │     visibility timeout expires               │
       │   DelaySeconds == 0               │              │                              │
       ▼                                   │              ▼                              │
   ┌────────────┐    ReceiveMessage()   ┌──┴───────────┐       ┌──────────────────┐      │
   │ AVAILABLE  ├──────────────────────►│  IN_FLIGHT   │       │    DELETED       │      │
   │ (visible)  │                       │  (invisible) ├──────►│  (permanent)     │      │
   └──────┬─────┘                       └──────────────┘       └──────────────────┘      │
          │                              DeleteMessage()                                  │
          │                                                                               │
          │   DelaySeconds > 0          ┌──────────────┐    delay expires                 │
          └────────────────────────────►│   DELAYED    ├──────────────────────────────────┘
                                        │  (invisible) │         (becomes AVAILABLE)
                                        └──────────────┘

   ┌────────────────────────────────────────────────────────────────────┐
   │  After retention period expires: message auto-deleted from ANY    │
   │  state (AVAILABLE, IN_FLIGHT, or DELAYED)                        │
   └────────────────────────────────────────────────────────────────────┘
```

### State Descriptions

| State | Visible to Consumers? | How to Enter | How to Exit |
|---|---|---|---|
| **AVAILABLE** | Yes | SendMessage (no delay), delay expires, visibility timeout expires | ReceiveMessage (→ IN_FLIGHT), retention expires (→ deleted) |
| **IN_FLIGHT** | No | ReceiveMessage | DeleteMessage (→ DELETED), visibility timeout expires (→ AVAILABLE), retention expires (→ deleted) |
| **DELAYED** | No | SendMessage with DelaySeconds > 0 | Delay period expires (→ AVAILABLE), retention expires (→ deleted) |
| **DELETED** | N/A | DeleteMessage, retention expiry, PurgeQueue | Terminal state |

---

## 2. Visibility Timeout — The Core Mechanism

### What It Solves

Without visibility timeout, message delivery has two failure modes:

1. **Pop-and-lose:** Consumer dequeues message, crashes before processing. Message is gone.
2. **Peek-and-duplicate:** Consumer reads but doesn't remove. Every consumer sees every message.

Visibility timeout solves both: the message is temporarily hidden after delivery, giving the consumer time to process and explicitly delete it. If the consumer fails, the message reappears.

### How It Works Internally

```
Message record:
{
  message_id:         "m-abc-123",
  body:               "{ order_id: 42, ... }",
  attributes:         { attr1: "val1", ... },       // up to 10 custom attributes
  sent_at:            T0,
  visible_at:         T0,                            // initially visible immediately
  receive_count:      0,
  first_received_at:  null,
  receipt_handle:     null,
  retention_deadline: T0 + 345600s                   // 4 days default
}
```

**On ReceiveMessage:**
```
1. Query: SELECT messages WHERE visible_at <= now() AND retention_deadline > now()
           ORDER BY sent_at LIMIT {MaxNumberOfMessages}

2. For each selected message, atomically:
   visible_at       = now() + queue.visibility_timeout   (default: 30 seconds)
   receive_count   += 1
   first_received_at = first_received_at ?? now()        (set only on first receive)
   receipt_handle   = generate_unique_token(message_id, receive_timestamp)

3. Return messages with receipt_handles to consumer
```

**On DeleteMessage(receipt_handle):**
```
1. Decode receipt_handle → message_id + receive_timestamp
2. Verify receipt_handle is still valid (message hasn't been re-received by another consumer)
3. Permanently delete message from storage
```

**On visibility timeout expiry (no explicit action needed):**
```
No daemon or timer fires. The message simply becomes eligible for the
next ReceiveMessage query because visible_at is now in the past.

This is the key design insight: visibility timeout is a QUERY PREDICATE,
not a scheduled event. It's stateless and infinitely scalable.
```

### Concrete Numbers (Verified from AWS Docs)

| Parameter | Value |
|---|---|
| Default visibility timeout | 30 seconds |
| Minimum visibility timeout | 0 seconds |
| Maximum visibility timeout | 12 hours (43,200 seconds) |
| Maximum measured from | First ReceiveMessage time (extending does not reset this clock) |

### ChangeMessageVisibility — The Heartbeat Pattern

For long-running processing tasks, the consumer can extend the visibility timeout:

```
Processing loop with heartbeat:

   consumer receives message (visibility_timeout = 300 seconds)
        │
        ├── process chunk 1 ... (120 seconds elapsed)
        │
        ├── ChangeMessageVisibility(receipt_handle, new_timeout=300)
        │   visible_at = now + 300s (extends from current time)
        │
        ├── process chunk 2 ... (120 seconds elapsed)
        │
        ├── ChangeMessageVisibility(receipt_handle, new_timeout=300)
        │
        ├── process chunk 3 ... (60 seconds elapsed)
        │
        └── DeleteMessage(receipt_handle) ✓ done
```

**Critical rule:** The total visibility timeout from the initial receive cannot exceed 12 hours. If a consumer calls ChangeMessageVisibility and the new timeout would extend beyond 12 hours from the first receive, SQS returns an error.

**Setting visibility timeout to 0:**
- Immediately makes the message visible again
- Useful when a consumer decides it cannot process a message and wants to release it for another consumer
- The receipt handle from the original receive becomes invalid after this

---

## 3. Delay Queues and Message Timers

### Queue-Level Delay (Delay Queues)

```
Queue configuration:
  DelaySeconds: 60   (all new messages delayed by 60 seconds)

Timeline:
  T0:     SendMessage() → message enters DELAYED state, visible_at = T0 + 60s
  T0+60s: Message transitions to AVAILABLE, visible_at = now
  T0+60s: ReceiveMessage() can now return this message
```

| Parameter | Value |
|---|---|
| Minimum delay | 0 seconds (default — no delay) |
| Maximum delay | 900 seconds (15 minutes) |

### Per-Message Delay (Message Timers)

Individual messages can override the queue's delay:

```
SendMessage(
  QueueUrl: 'my-queue',
  MessageBody: '...',
  DelaySeconds: 120   // this message delayed 2 minutes, regardless of queue setting
)
```

### Standard vs FIFO Behavior Difference

| Change to DelaySeconds | Standard Queue | FIFO Queue |
|---|---|---|
| Affect existing messages? | **No** — only new messages use the new delay | **Yes** — retroactively affects messages already in the queue |

### Beyond 15 Minutes

SQS delay is limited to 15 minutes. For longer delays:
- **EventBridge Scheduler** — supports scheduling billions of one-time or recurring API actions with no time limit
- **Step Functions** with a Wait state — can wait hours or days before sending to SQS
- **Application-level pattern** — send message immediately with a `process_after` attribute; consumers check the timestamp and re-enqueue if not ready

---

## 4. Message Retention and Auto-Expiry

### How Retention Works

```
Message sent at T0, retention_period = 4 days (default)

  retention_deadline = T0 + 345,600 seconds

  At any time after retention_deadline:
    - Message is permanently deleted from storage
    - No notification or event is generated
    - Message cannot be recovered
    - This happens regardless of the message's current state
      (AVAILABLE, IN_FLIGHT, or DELAYED)
```

| Parameter | Value |
|---|---|
| Minimum retention | 60 seconds (1 minute) |
| Maximum retention | 1,209,600 seconds (14 days) |
| Default retention | 345,600 seconds (4 days) |

### Retention + DLQ Interaction

When a message is moved to a DLQ:

- **Standard queues:** The original enqueue timestamp is **preserved**. If the message spent 3 days in the source queue and the DLQ has 4-day retention, it will expire from the DLQ in 1 day (not 4 days).
- **FIFO queues:** The enqueue timestamp **resets** when the message enters the DLQ. Full retention period starts fresh.

**Best practice:** Always set the DLQ's retention period **longer** than the source queue's. Otherwise, messages may expire from the DLQ before an operator can investigate.

### Storage Cleanup

[INFERRED — not officially documented]

Messages are likely cleaned up through a combination of:
1. **TTL-based compaction** — The log-structured storage engine can drop entire segments (SSTables) when all messages in the segment have expired
2. **Lazy deletion** — Expired messages are not actively deleted; they're simply skipped during ReceiveMessage queries and cleaned up during compaction
3. **Background reaper** — A periodic process scans for and removes expired messages to reclaim disk space

---

## 5. Receipt Handles

### What They Are

A receipt handle is an opaque token returned by ReceiveMessage that uniquely identifies a specific delivery of a message. It is required for:
- `DeleteMessage` — confirming successful processing
- `ChangeMessageVisibility` — extending or shortening the timeout

### Why Not Just Use MessageId?

```
Problem scenario without receipt handles:

  T0: Consumer A receives message M1 (starts processing)
  T1: Consumer A is slow, visibility timeout expires
  T2: Consumer B receives message M1 (different delivery)
  T3: Consumer A finishes, calls DeleteMessage(MessageId = M1)

  Question: Whose delivery is being deleted? A's (which is done) or B's (which is still processing)?
```

Receipt handles solve this by binding to a **specific delivery** of a message, not just the message itself. Each time a message is received, a new receipt handle is generated. Only the most recent receipt handle is valid.

```
  T0: Consumer A receives M1 → receipt_handle = "rh-AAA"
  T1: Visibility timeout expires → receipt_handle "rh-AAA" becomes invalid
  T2: Consumer B receives M1 → receipt_handle = "rh-BBB"
  T3: Consumer A calls DeleteMessage("rh-AAA") → ERROR: invalid receipt handle
  T4: Consumer B calls DeleteMessage("rh-BBB") → SUCCESS: message deleted
```

### Receipt Handle Design

[INFERRED — not officially documented]

A receipt handle likely encodes:
- Message ID
- Partition ID
- Receive timestamp
- Server-side nonce (to prevent guessing/replay)
- Possibly the visibility timeout deadline

This allows the storage layer to validate the receipt handle without a lookup table — the handle is self-describing and authenticated (possibly via HMAC).

---

## 6. PurgeQueue

### What It Does

Deletes all messages from a queue. The queue itself is not deleted.

### Constraints

- Can only be called once every 60 seconds
- Is not instantaneous — messages "in flight" at the time of the purge may still be returned to consumers briefly
- There is no way to selectively purge (e.g., purge only messages older than X)

### When to Use

- Testing/development environments
- Resetting a queue after a bug caused bad messages to be enqueued
- Never in production under normal circumstances — usually signals a design problem

---

## 7. Batch Operations

### SendMessageBatch

- Send up to **10 messages** in a single API call
- Each message can have its own delay, attributes, and dedup ID
- Total batch payload must stay within limits
- Partial failures are possible — some messages succeed, others fail. Check the response.

### DeleteMessageBatch

- Delete up to **10 messages** in a single API call
- Each delete uses its own receipt handle
- Partial failures are possible

### ChangeMessageVisibilityBatch

- Change visibility for up to **10 messages** in a single API call

### Why Batch Size is 10

[INFERRED — not officially documented]

The batch limit of 10 balances:
- **Atomicity:** Each batch entry is independent (not atomic as a group), so the overhead is per-entry
- **Payload size:** 10 messages x 256 KB = 2.56 MB max — reasonable for a single HTTP request
- **Latency:** Processing 10 entries per request keeps response time bounded
- **FIFO throughput:** With batching, FIFO queues can reach 3,000 msg/sec (10 messages x 300 API calls/sec)

---

*Back to [interview-simulation.md](interview-simulation.md)*
