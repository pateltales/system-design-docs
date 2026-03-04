# System Design Interview Simulation: Design Amazon SQS (Simple Queue Service)

> **Interviewer:** Principal Engineer (L8), Amazon SQS Team
> **Candidate Level:** SDE-3 (L6 — Senior Software Development Engineer)
> **Duration:** ~60 minutes
> **Date:** February 12, 2026

---

## PHASE 1: Opening & Problem Statement (~2 min)

**Interviewer:**
Hey, welcome. I'm [name], Principal Engineer on the SQS team. For today's system design round, I'd like you to design a **distributed message queue service** — think Amazon SQS. A fully managed service where producers send messages and consumers receive and process them asynchronously. We're talking about the core infrastructure: how messages get stored, delivered, and how we handle millions of queues at enormous scale.

I'm interested in how you reason about delivery guarantees, ordering, visibility semantics, and the tradeoffs between throughput and correctness. I'll push on your decisions — that's me calibrating depth, not a negative signal.

Take it away.

---

## PHASE 2: Requirements Gathering & Scoping (~5 min)

**Candidate:**
Thanks! A managed message queue service is a broad design space — SQS covers everything from simple decoupling to strict FIFO ordering with exactly-once processing. Let me scope this carefully before drawing anything.

**Functional Requirements — what operations do we need?**

> "The core API operations:
> - **SendMessage** — producer sends a message (body + optional attributes) to a named queue.
> - **ReceiveMessage** — consumer polls the queue, receives one or more messages. Messages become temporarily invisible to other consumers (visibility timeout).
> - **DeleteMessage** — consumer confirms successful processing by deleting the message.
> - **CreateQueue / DeleteQueue** — queue lifecycle management.
> - **ChangeMessageVisibility** — extend or shorten the visibility timeout for an in-flight message.
> - **PurgeQueue** — delete all messages from a queue.
>
> A few clarifying questions:
> - **Do we need to support both Standard and FIFO queue types?** These have fundamentally different guarantees."

**Interviewer:** "Yes, both. Standard queues are the bread and butter — high throughput, at-least-once delivery. FIFO queues are a different beast with strict ordering and exactly-once processing. Understand the tradeoffs between them."

> "- **Batching?** SendMessageBatch and DeleteMessageBatch for throughput optimization?"

**Interviewer:** "Mention batching, but focus on the single-message path first. Batching is an optimization on top."

> "- **What about message size limits?** Are we talking small control messages or large payloads?"

**Interviewer:** "Good question. What's your understanding?"

> "SQS messages are limited to **256 KB** per message body. The total message size including all message attributes counts toward this limit. For larger payloads, the pattern is to store the payload in S3 and put a pointer in SQS — the Amazon SQS Extended Client Library does exactly this. But the queue itself handles small-to-medium messages, not large blobs."

*[Note: AWS documentation as of late 2024 references a 1 MiB per-message limit in some contexts. Earlier SQS documentation consistently states 256 KB. The 1 MiB figure may reflect a recent increase or include attribute overhead differently. I will use 256 KB as the well-established limit and note the discrepancy.]*

**Interviewer:** "Right. Keep going."

> "- **Dead letter queues?** For messages that fail processing repeatedly?"

**Interviewer:** "Absolutely — DLQs are critical for operational health. Cover them."

**Non-Functional Requirements:**

> "Now the critical constraints. A message queue is defined by its delivery and ordering guarantees:
>
> | Dimension | Requirement | Rationale |
> |---|---|---|
> | **Durability** | Messages must not be lost after a successful SendMessage response | Messages are replicated redundantly across multiple servers and AZs |
> | **Availability** | 99.99%+ (4+ 9's) | SQS is a foundational building block — if SQS is down, workflows across AWS halt |
> | **Delivery (Standard)** | At-least-once | A message may be delivered more than once; consumers must be idempotent |
> | **Delivery (FIFO)** | Exactly-once processing | Deduplication within a 5-minute window ensures no duplicate delivery |
> | **Ordering (Standard)** | Best-effort | Messages are generally delivered in the order sent, but no strict guarantee |
> | **Ordering (FIFO)** | Strict within a message group | Messages with the same MessageGroupId are delivered in exact send order |
> | **Latency** | Single-digit milliseconds for SendMessage, low tens of ms for ReceiveMessage | Producers must not be blocked; consumers need timely delivery |
> | **Throughput (Standard)** | Nearly unlimited | Standard queues support an effectively unlimited number of transactions per second |
> | **Throughput (FIFO)** | Up to 300 msg/sec without batching, 3,000 msg/sec with batching per partition | Ordering constraints limit parallelism |
> | **Multi-tenancy** | Millions of queues across millions of customers | Noisy neighbor isolation is critical |
> | **Retention** | Configurable: 60 seconds to 14 days (default: 4 days) | Messages auto-deleted after retention expires |
> | **Visibility Timeout** | 0 seconds to 12 hours (default: 30 seconds) | Time window for consumer to process before message reappears |

**Interviewer:**
Good. You mentioned at-least-once for Standard and exactly-once for FIFO. Why the difference? Why not just make everything exactly-once?

**Candidate:**

> "Because exactly-once delivery in a distributed system requires **coordination**, and coordination kills throughput. Here's the fundamental tradeoff:
>
> - **Standard queues** sacrifice ordering and deduplication for throughput. The system can deliver a message from whichever server has a copy, in any order, without checking if another server already delivered it. This makes Standard queues essentially unlimited in throughput — they can fan out reads across many servers.
>
> - **FIFO queues** maintain a total order within each message group and deduplicate within a 5-minute window using a MessageDeduplicationId. This requires the system to serialize reads within a message group and track delivered message IDs — both of which bottleneck on a single point of coordination per message group.
>
> You fundamentally cannot have unlimited throughput + strict ordering + exactly-once all at the same time in a distributed system. Standard queues pick throughput; FIFO queues pick correctness."

**Interviewer:**
That's the right framing. Let's get some scale numbers.

---

### L5 vs L6 vs L7 — Phase 2 Rubric

| Aspect | L5 (SDE-2) | L6 (SDE-3) | L7 (Principal) |
|---|---|---|---|
| **Functional Reqs** | Lists send/receive/delete | Proactively raises FIFO vs Standard, DLQs, visibility timeout, batching | Additionally discusses delay queues, message timers, event-driven patterns (SQS-triggered Lambda), FIFO high-throughput mode |
| **Non-Functional** | "Messages shouldn't be lost" | Quantifies delivery guarantees, explains *why* Standard is at-least-once vs FIFO exactly-once, knows the 5-minute dedup window | Frames NFRs as a CAP/PACELC tradeoff, discusses how ordering constraints bound throughput, proposes SLA commitments |
| **Scoping** | Accepts problem as given | Drives clarifying questions, scopes Standard vs FIFO as separate design paths | Negotiates scope based on time, proposes covering Standard first then FIFO as a constraint overlay |

---

## PHASE 3: Scale Estimation (~3 min)

**Candidate:**

> "Let me estimate SQS-scale numbers to ground our design decisions."

#### Queue and Message Estimates

> "SQS is one of the oldest and most heavily used AWS services. Let me reason about scale:
>
> - **Total queues**: Tens of millions of active queues across all AWS customers
> - **Messages sent per day**: Likely hundreds of billions (SQS has publicly stated handling trillions of messages)
> - **Peak messages per second (globally)**: Let's estimate 50 million msg/sec across all queues
> - **Average message size**: ~4 KB (many are small JSON events; some approach the 256 KB limit)
> - **In-flight messages per Standard queue**: Up to 120,000 (documented limit)
> - **In-flight messages per FIFO queue**: Up to 20,000 [UNVERIFIED — check AWS docs for current FIFO in-flight limit]"

#### Storage Estimates

> "If we retain messages for 4 days (default) at 50M msg/sec:
>
> - **Messages in storage**: 50M/sec x 86,400 sec/day x 4 days = **17.3 trillion messages** at any time
> - **Storage per message**: 4 KB body + ~1 KB metadata = ~5 KB
> - **Total storage**: 17.3T x 5 KB = **86.4 PB** of message data in flight at any moment
> - **With replication** (across multiple AZs): 86.4 PB x 3 = ~**260 PB** of replicated storage
>
> These are enormous numbers — but unlike S3, messages are ephemeral. The storage system must optimize for high write throughput and TTL-based expiration, not long-term durability."

#### Throughput

> "- **SendMessage**: 50M/sec globally (our estimate)
> - **ReceiveMessage**: Higher than sends — consumers poll frequently, many polls return empty (especially with short polling)
> - **DeleteMessage**: Roughly equal to SendMessage (every sent message should eventually be deleted)
> - **Read:Write ratio**: Unlike S3 (read-heavy), SQS is roughly balanced — every message is sent once and received at least once
> - **Key insight**: The hot path is ReceiveMessage, not SendMessage. Consumers poll continuously, and most polls (with short polling) return empty."

**Interviewer:**
Good point about ReceiveMessage being the hot path. Short polling's empty responses are a significant cost driver — that's why long polling exists. Let's architect this.

---

## PHASE 4: High-Level Architecture (~5 min)

**Candidate:**

> "Let me start with the simplest thing that works, find the problems, and fix them."

#### Attempt 0: Single Server with In-Memory Queue

> "Simplest possible design — one server, one queue in memory:
>
> ```
>     Producer                          Consumer
>       │                                  │
>       │  SendMessage("order-123")        │  ReceiveMessage()
>       ▼                                  ▼
>   ┌──────────────────────────────────────────┐
>   │           Single Server                   │
>   │                                           │
>   │   Queue: [msg1, msg2, msg3, msg4]         │
>   │   (LinkedList in memory)                  │
>   │                                           │
>   │   SendMessage → append to tail            │
>   │   ReceiveMessage → dequeue from head      │
>   │   DeleteMessage → remove from list        │
>   └──────────────────────────────────────────┘
> ```
>
> This works for a tutorial. One producer, one consumer, one queue."

**Interviewer:**
What breaks?

**Candidate:**

> "Everything:
>
> 1. **Durability** — Server crash = all messages lost. There's no persistence.
> 2. **Scalability** — One server can only handle so much throughput. With millions of queues and billions of messages per day, we need horizontal scaling.
> 3. **Availability** — If the server is down, nobody can send or receive.
> 4. **Visibility timeout** — We're doing a simple dequeue, so there's no way to handle consumer failure. If a consumer receives a message and crashes before processing, the message is gone.
>
> The visibility timeout problem is actually the most interesting one — it's what makes a message queue fundamentally different from a simple FIFO data structure."

#### Attempt 1: Add Persistence and Visibility Timeout

> "Let's fix durability and add proper message lifecycle:
>
> ```
>   ┌──────────────────────────────────────────────┐
>   │            Single Server + Disk               │
>   │                                               │
>   │   Messages Table (on disk):                   │
>   │   ┌────────┬──────┬────────────┬───────────┐  │
>   │   │ msg_id │ body │ status     │ visible_at│  │
>   │   ├────────┼──────┼────────────┼───────────┤  │
>   │   │ m1     │ ...  │ AVAILABLE  │ now       │  │
>   │   │ m2     │ ...  │ IN_FLIGHT  │ T+30s     │  │
>   │   │ m3     │ ...  │ AVAILABLE  │ now       │  │
>   │   │ m4     │ ...  │ DELAYED    │ T+60s     │  │
>   │   └────────┴──────┴────────────┴───────────┘  │
>   │                                               │
>   │   SendMessage → INSERT (status=AVAILABLE)     │
>   │   ReceiveMessage → SELECT WHERE status=       │
>   │     AVAILABLE AND visible_at <= now            │
>   │     → UPDATE status=IN_FLIGHT,                │
>   │       visible_at = now + visibility_timeout    │
>   │   DeleteMessage → DELETE row                  │
>   │   Timeout expiry → revert IN_FLIGHT to        │
>   │     AVAILABLE when visible_at passes           │
>   └──────────────────────────────────────────────┘
> ```
>
> Now messages survive server restarts (they're on disk) and we have visibility timeout — if a consumer receives a message and doesn't delete it within 30 seconds, it becomes visible again for another consumer.
>
> **But still broken:**
> - Single server = single point of failure
> - One disk = if disk dies, messages are lost
> - Can't scale beyond one machine's capacity"

#### Attempt 2: Replicate Across AZs

> "Add replication for durability and availability:
>
> ```
>     Producer                                    Consumer
>       │                                            │
>       ▼                                            ▼
>   ┌────────────┐                            ┌────────────┐
>   │  Front-End │ ── routes to correct ──→   │  Front-End │
>   │  (Fleet)   │    queue servers            │  (Fleet)   │
>   └─────┬──────┘                            └──────┬─────┘
>         │                                          │
>    ┌────┼──────────────────┐                       │
>    │    │                  │                       │
>    ▼    ▼                  ▼                       │
>  ┌──────┐  ┌──────┐  ┌──────┐                     │
>  │ AZ-a │  │ AZ-b │  │ AZ-c │  ◄─────────────────┘
>  │Queue │  │Queue │  │Queue │
>  │Server│  │Server│  │Server│
>  │(copy)│  │(copy)│  │(copy)│
>  └──────┘  └──────┘  └──────┘
> ```
>
> Every message is written to multiple servers across AZs before acknowledging the send. A consumer can receive from any server that has the message."

**Interviewer:**
Better. But I see problems — can you identify them?

**Candidate:**

> "Two critical problems:
>
> 1. **How does the front-end know which servers host a given queue?** We have millions of queues. We need a metadata layer that maps queue names to server locations — essentially a queue placement service.
>
> 2. **Replication consistency for ReceiveMessage** — When a consumer receives a message from AZ-a and it becomes in-flight (invisible), how do AZ-b and AZ-c know not to deliver that same message? If we don't coordinate, multiple consumers could receive the same message simultaneously. For Standard queues, this is tolerable (at-least-once), but for FIFO, it's a correctness violation.
>
> 3. **Scaling hot queues** — One queue might receive 100,000 msg/sec while another gets 1 msg/day. A single set of replicas per queue can't handle a hot queue. We need to partition hot queues across multiple servers.
>
> Let me restructure the architecture to address these."

#### Attempt 3: Layered Architecture

> "The key insight: **queue management, message routing, and message storage are separate concerns that scale differently.** Let me split them:
>
> ```
>                            ┌──────────────────────┐
>                            │       Clients         │
>                            │  (SDKs, CLI, Lambda)  │
>                            └──────────┬───────────┘
>                                       │ HTTPS (REST API)
>                            ┌──────────▼───────────┐
>                            │    Front-End Layer    │
>                            │  (Stateless fleet)    │
>                            │  Auth (SigV4)         │
>                            │  Rate limiting        │
>                            │  Request routing      │
>                            └──────────┬───────────┘
>                                       │
>                    ┌──────────────────┼──────────────────┐
>                    │                                     │
>         ┌──────────▼─────────┐            ┌─────────────▼────────────┐
>         │  Queue Metadata    │            │   Message Storage        │
>         │  Service           │            │   Layer                  │
>         │                    │            │                          │
>         │  queue_url →       │            │   Replicated across      │
>         │  {queue_config,    │            │   multiple AZs           │
>         │   partition_map,   │            │                          │
>         │   DLQ_config,      │            │   AZ-a   AZ-b   AZ-c    │
>         │   attributes}      │            │   ┌───┐  ┌───┐  ┌───┐   │
>         │                    │            │   │   │  │   │  │   │   │
>         │  (DynamoDB-like    │            │   └───┘  └───┘  └───┘   │
>         │   metadata store)  │            │                          │
>         └────────────────────┘            │   Messages stored on     │
>                                          │   disk with WAL          │
>                                          └──────────────────────────┘
> ```
>
> **How a SendMessage works:**
> 1. Client sends `SendMessage(queue_url, body)` to front-end
> 2. Front-end authenticates (SigV4), checks IAM permissions
> 3. Front-end looks up queue metadata: which partition(s) store this queue's messages?
> 4. Front-end routes the message to the appropriate storage partition
> 5. Storage partition writes message to multiple replicas across AZs
> 6. Once durably written (quorum ack), returns 200 OK with MessageId and MD5
>
> **How a ReceiveMessage works:**
> 1. Client sends `ReceiveMessage(queue_url, max_messages, wait_time)` to front-end
> 2. Front-end looks up queue metadata → finds storage partition(s)
> 3. Routes to storage partition: "give me up to N visible messages"
> 4. Storage partition selects messages where `visible_at <= now`, marks them in-flight with new `visible_at = now + visibility_timeout`
> 5. Returns messages with ReceiptHandles (opaque tokens needed for delete)
> 6. If long polling (WaitTimeSeconds > 0) and no messages available, holds the connection open for up to 20 seconds
>
> **How a DeleteMessage works:**
> 1. Client sends `DeleteMessage(queue_url, receipt_handle)` to front-end
> 2. Receipt handle encodes which partition and message to delete
> 3. Storage partition permanently removes the message"

**Interviewer:**
Good — I like the separation. But this is still somewhat hand-wavy. Let's go deeper on each layer. What does the message storage layer actually look like? How do you handle visibility timeout across replicas? How do you partition hot queues?

**Candidate:**

> "Exactly — three areas to evolve:
>
> | Layer | Current (Naive) | Problem |
> |-------|----------------|---------|
> | **Message Storage** | Replicated across AZs (hand-wavy) | What's the replication protocol? How is visibility timeout coordinated? |
> | **Queue Partitioning** | One partition per queue | Hot queues will overwhelm a single partition |
> | **Delivery Guarantees** | Undefined | Standard = at-least-once, FIFO = exactly-once — how do we implement each? |
>
> Let's deep-dive each one."

**Interviewer:**
Let's start with message storage and the visibility timeout mechanism — that's the heart of SQS.

---

## PHASE 5: Deep Dive — Message Storage & Visibility Timeout (~10 min)

**Candidate:**

> "The message storage layer is where SQS fundamentally differs from other distributed systems. Unlike S3 where objects are immutable after write, SQS messages have a complex lifecycle with state transitions. Let me map out the lifecycle first, then talk about how to implement it."

#### Message Lifecycle

> "A message goes through these states:
>
> ```
>                                    ┌─────────────────────────────────────────────┐
>   SendMessage()                    │                                             │
>       │                            │     visibility timeout expires              │
>       ▼                            │              │                              │
>   ┌────────┐    ReceiveMessage() ┌─┴──────────┐  │   ┌──────────────────┐       │
>   │AVAILABLE├───────────────────►│ IN_FLIGHT   │──┘   │ DELETED          │       │
>   │(visible)│                    │ (invisible) │─────►│ (permanently     │       │
>   └────┬───┘                     └─────────────┘      │  removed)        │       │
>        │                          DeleteMessage()     └──────────────────┘       │
>        │                                                                         │
>        │   DelaySeconds > 0       ┌────────────┐    delay expires                │
>        └─────────────────────────►│  DELAYED    ├────────────────────────────────┘
>                                   │ (invisible) │         (becomes AVAILABLE)
>                                   └─────────────┘
>
>   After retention period (default 4 days, max 14 days): message auto-deleted regardless of state
> ```
>
> **Key numbers (verified from AWS docs):**
> - Visibility timeout: 0 seconds to 12 hours (43,200 seconds), default 30 seconds
> - Delay: 0 to 15 minutes (900 seconds), default 0
> - Retention: 60 seconds to 14 days (1,209,600 seconds), default 4 days
> - In-flight limit (Standard queue): approximately 120,000 messages
> - Message size: 256 KB (body + attributes)"

#### How to Store Messages Durably

> "Let me reason about the storage engine. Each queue partition needs:
>
> 1. **Durable write** — Messages must survive server crashes
> 2. **Efficient visibility queries** — ReceiveMessage needs to find messages where `visible_at <= now`
> 3. **TTL expiration** — Messages must auto-delete after retention period
> 4. **High write throughput** — 50M+ msg/sec globally
>
> **Storage engine choice:**
>
> SQS messages are write-heavy, short-lived, and need efficient time-based queries. This is a great fit for a **log-structured storage engine** (like an LSM tree) rather than a B-tree:
>
> | Requirement | LSM Tree (Log-Structured) | B-Tree |
> |---|---|---|
> | Write throughput | Excellent — sequential writes to WAL + memtable | Good — random I/O for in-place updates |
> | Time-based queries | Good — can use `visible_at` as sort key | Good — B-tree index on `visible_at` |
> | TTL expiration | Excellent — old SSTables can be dropped entirely | Expensive — need to scan and delete expired rows |
> | Space reclamation | Compaction reclaims deleted/expired message space | Fragmentation from deletes |
>
> [INFERRED — not officially documented] SQS likely uses a custom log-structured storage engine optimized for the message lifecycle pattern. The key insight is that messages are append-only (never updated in place — state changes like visibility are tracked separately) and have natural TTLs."

#### Replication Model

> "SQS stores messages redundantly across multiple servers and data centers — this is stated in the official AWS documentation. Let me reason about the replication model:
>
> ```
> SendMessage("order-123") flow:
>
>   Front-End
>       │
>       ▼
>   Queue Partition Leader (AZ-a)
>       │
>       ├── Write to local WAL ─────── ✓
>       │
>       ├── Replicate to Follower (AZ-b) ── ✓
>       │
>       ├── Replicate to Follower (AZ-c) ── ✓
>       │
>       │   (wait for quorum: 2-of-3 acks)
>       │
>       ▼
>   Return 200 OK + MessageId
> ```
>
> [INFERRED — not officially documented] SQS likely uses a leader-based replication model with quorum writes:
>
> - Each queue partition has a **leader** and **followers** across AZs
> - Writes go to the leader, which replicates to followers
> - A **quorum** (2-of-3) acknowledgment is sufficient to confirm the write — this balances durability with latency
> - If the leader fails, a follower is promoted (similar to Raft leader election)
>
> This gives us:
> - **Durability**: A message survives any single AZ failure
> - **Availability**: Reads and writes continue even if one AZ is down
> - **Latency**: Don't need to wait for all 3 AZs — quorum (2-of-3) is sufficient"

#### Visibility Timeout Mechanism

> "This is where SQS gets interesting. Visibility timeout is not just a timer — it's a distributed coordination problem.
>
> **How it works at the storage level:**
>
> ```
> Message record in storage:
> {
>   message_id: "m-abc-123",
>   body: "{ order_id: 42, ... }",
>   sent_at: T0,
>   visible_at: T0,               // when message becomes visible
>   receive_count: 0,
>   receipt_handle: null,          // set on receive
>   retention_deadline: T0 + 4d   // auto-delete after this
> }
>
> On ReceiveMessage:
>   1. Find message where visible_at <= now
>   2. Atomically update:
>      visible_at = now + 30s (default visibility timeout)
>      receive_count += 1
>      receipt_handle = generate_unique_token()
>   3. Return message + receipt_handle to consumer
>
> On DeleteMessage(receipt_handle):
>   1. Look up message by receipt_handle
>   2. Permanently delete from storage
>
> On visibility timeout expiry:
>   1. No active process needed — the message naturally becomes
>      visible again because visible_at is now in the past
>   2. Next ReceiveMessage query (visible_at <= now) will pick it up
> ```
>
> **The elegance:** There's no separate timer or expiry daemon needed. The visibility timeout is encoded as a future timestamp in the `visible_at` field. Messages naturally reappear when the clock passes `visible_at`. This is stateless and scalable.
>
> **ChangeMessageVisibility:**
> - Updates `visible_at` to a new future time
> - The receipt handle must match (only the consumer who received the message can extend it)
> - The 12-hour maximum visibility timeout is measured from the time the message was first received from the queue — extending does not reset this clock
> - This enables a **heartbeat pattern**: consumer periodically calls ChangeMessageVisibility to extend the timeout while still processing"

**Interviewer:**
What about the in-flight limit? You mentioned 120,000 for Standard queues. Why does that limit exist?

**Candidate:**

> "The in-flight limit exists because the system must **track** every in-flight message — it needs to know which messages are invisible and when they become visible again. This tracking costs memory.
>
> Each in-flight message requires storing:
> - The message ID and receipt handle mapping
> - The `visible_at` timestamp
> - Which consumer received it (for receipt handle validation)
>
> At roughly 200-500 bytes per in-flight record, 120,000 messages per queue = ~24-60 MB of in-flight metadata per queue. That's manageable.
>
> When you hit the 120,000 limit, SQS returns an `OverLimit` error on short polling, or returns no error but no messages on long polling. The consumer must delete or wait for visibility timeout expiry to free up in-flight slots.
>
> For FIFO queues, the in-flight tracking is even more constrained because messages within the same message group must be processed sequentially — you can't have two in-flight messages from the same group."

**Interviewer:**
Good. What about long polling vs short polling? What happens at the infrastructure level?

**Candidate:**

> "This is one of my favorite SQS details because it exposes how the distributed polling works.
>
> **Short polling** (default, WaitTimeSeconds = 0):
> - The ReceiveMessage request queries a **subset** of the servers that store the queue's messages
> - Uses weighted random distribution to select which servers to query
> - Returns immediately, even if no messages were found on the sampled servers
> - This means: **a short poll can return empty even when messages exist** — they might be on servers that weren't sampled
> - Subsequent polls will eventually hit the right servers and return messages
>
> **Long polling** (WaitTimeSeconds > 0, maximum 20 seconds):
> - The ReceiveMessage request queries **all** servers that store the queue's messages
> - If no messages are found, the connection is held open
> - As soon as a message arrives on any server, it's returned to the waiting consumer
> - If no message arrives within WaitTimeSeconds, an empty response is returned
> - Eliminates false empty responses
>
> ```
> Short Polling:
>   Consumer → Front-End → sample 3 of 10 servers → no messages on those 3 → empty response
>   (messages might exist on the other 7 servers)
>
> Long Polling:
>   Consumer → Front-End → query ALL 10 servers → hold connection open
>   → message arrives on server 7 → immediately return to consumer
> ```
>
> **Why short polling exists at all:** It's faster when messages are abundant (no need to query all servers) and simpler to implement. But for low-throughput queues, long polling is strictly better — fewer API calls, lower cost, faster message delivery.
>
> **The infrastructure implication:** Long polling requires the front-end to maintain open connections to storage servers. At scale, this means front-end servers hold thousands of open connections per consumer, which has memory and file descriptor implications. The max of 20 seconds prevents connections from being held indefinitely."

> *For the full deep dive on the message lifecycle and visibility timeout, see [message-lifecycle.md](message-lifecycle.md).*

#### Architecture Update After Phase 5

> "Our message storage layer has evolved:
>
> | | Before (Phase 4) | After (Phase 5) |
> |---|---|---|
> | **Message Storage** | Hand-wavy "replicated across AZs" | **Leader-follower replication with quorum writes, log-structured storage engine, visibility timeout via `visible_at` timestamps** [INFERRED] |
> | **Polling** | Undefined | **Short polling (subset sampling) vs long polling (all servers, held connections, max 20s)** |
> | **Queue Partitioning** | One partition per queue | *(still one partition — let's fix this next)* |
> | **Delivery Guarantees** | Undefined | *(still undefined for FIFO — we'll address this)* |

---

### L5 vs L6 vs L7 — Phase 5 Rubric

| Aspect | L5 (SDE-2) | L6 (SDE-3) | L7 (Principal) |
|---|---|---|---|
| **Message lifecycle** | "Messages go in and come out" | Draws the full state machine (AVAILABLE → IN_FLIGHT → DELETED/reappear), explains `visible_at` timestamp approach | Additionally models delayed messages, retention TTL, discusses clock skew across replicas affecting visibility |
| **Visibility timeout** | "Message is hidden for 30 seconds" | Explains the `visible_at` field trick (no timer daemon needed), ChangeMessageVisibility, 12-hour max, heartbeat pattern | Discusses in-flight limit memory cost, receipt handle design, how visibility works across replicas during leader failover |
| **Long vs short polling** | "Long polling waits for messages" | Explains subset-sampling vs all-server query, why short polling returns false empties, 20-second max | Discusses connection management at scale, back-pressure on front-end servers, adaptive polling strategies |
| **Storage engine** | "Store messages in a database" | Reasons about LSM tree vs B-tree for message workloads, explains why log-structured fits | Discusses compaction strategies for expired messages, WAL design, storage node capacity planning |

---

## PHASE 6: Deep Dive — Queue Partitioning & Scaling (~10 min)

**Interviewer:**
Good, we understand how messages are stored and delivered within a single partition. But you mentioned hot queues — a single queue receiving 100K msg/sec. How do we scale that?

**Candidate:**

> "Right. In our current design, each queue lives on a single partition — a leader + followers. This creates two bottlenecks:
>
> 1. **Throughput ceiling**: A single partition can handle maybe 10K-50K msg/sec before the leader saturates.
> 2. **Storage ceiling**: A queue with millions of messages (deep backlog) might exceed one node's storage.
>
> We need to **partition hot queues across multiple storage nodes**."

#### Standard Queue Partitioning

> "For Standard queues, partitioning is straightforward because ordering doesn't matter:
>
> ```
> Queue: 'order-processing' (hot queue, 100K msg/sec)
>
> Partition into 10 shards:
>
>   Shard 0: messages hash(msg_id) % 10 == 0   → Node Group A
>   Shard 1: messages hash(msg_id) % 10 == 1   → Node Group B
>   Shard 2: messages hash(msg_id) % 10 == 2   → Node Group C
>   ...
>   Shard 9: messages hash(msg_id) % 10 == 9   → Node Group J
>
> SendMessage: hash(message_id) → pick shard → write to that shard's leader
> ReceiveMessage: randomly pick a shard (or round-robin) → read from it
> ```
>
> **Why this works for Standard queues:**
> - Standard queues don't guarantee ordering, so reading from any shard is fine
> - Standard queues allow at-least-once delivery, so even if two consumers hit different shards and get different messages, that's correct behavior
> - Each shard has its own leader-follower group, so throughput scales linearly with shards
>
> **Auto-scaling:**
> - The queue metadata service monitors throughput per queue
> - When a queue's request rate exceeds a threshold, it splits into more shards
> - When traffic drops, shards can be merged (but this is less urgent — empty shards are cheap)
> - The partition map is cached by front-end servers and updated asynchronously"

**Interviewer:**
What about FIFO queues? You can't just randomly shard those.

**Candidate:**

> "Exactly — FIFO queues require strict ordering within a **message group**. This is where the MessageGroupId becomes the partitioning key.
>
> ```
> FIFO Queue: 'payment-processing.fifo'
>
> Messages:
>   {body: 'payment-1', MessageGroupId: 'user-A'}  ← must be ordered with other user-A msgs
>   {body: 'payment-2', MessageGroupId: 'user-B'}  ← must be ordered with other user-B msgs
>   {body: 'payment-3', MessageGroupId: 'user-A'}  ← must come AFTER payment-1 for user-A
>
> Partitioning by MessageGroupId:
>
>   Partition 0: MessageGroupId hash → 0   (e.g., user-A, user-C, ...)
>   Partition 1: MessageGroupId hash → 1   (e.g., user-B, user-D, ...)
>   ...
>
> Within each partition:
>   Messages with the same MessageGroupId are strictly ordered
>   Messages with different MessageGroupIds can be delivered in parallel
> ```
>
> SQS uses a hash of the MessageGroupId to determine which partition stores each message. This is confirmed by the AWS high-throughput FIFO documentation which states: 'Amazon SQS uses a hash function applied to each message's message group ID to determine which partition stores the message.'
>
> **FIFO throughput limits:**
> - Without high-throughput mode: 300 messages/second for send, receive, and delete (per API action)
> - With high-throughput mode and batching: up to 3,000 messages/second per partition
> - SQS automatically adds partitions as request rates increase (up to regional quota)
>
> **The critical constraint:** Within a single message group, only ONE message can be in-flight at a time. While message 'payment-1' for user-A is in-flight, 'payment-3' (also for user-A) is blocked. This is how FIFO preserves ordering — sequential processing per group.
>
> **Best practice for FIFO throughput:** Use a large number of distinct MessageGroupIds. If you use only one group ID, you've serialized your entire queue through a single thread. If you use one group ID per user/entity, you get parallelism across entities while preserving per-entity ordering."

**Interviewer:**
How does the partition map work? What happens during a partition split?

**Candidate:**

> "The queue metadata service maintains a **partition map** for each queue:
>
> ```
> Queue: 'order-processing'
> Partition Map:
>   [hash range 0-999]     → Partition P0 → Nodes {A1, A2, A3} (across AZs)
>   [hash range 1000-1999]  → Partition P1 → Nodes {B1, B2, B3}
>   [hash range 2000-2999]  → Partition P2 → Nodes {C1, C2, C3}
>
> Front-end cache:
>   Queue 'order-processing' → partition map (cached, refreshed periodically)
> ```
>
> **During a partition split:**
> 1. Queue metadata service decides to split P0 into P0a and P0b
> 2. Creates new partition P0b, assigns node group
> 3. Begins migrating messages in the split range from P0 to P0b
> 4. Updates the partition map: P0 now covers [0-499], P0b covers [500-999]
> 5. Front-end servers refresh their cached partition map
> 6. During the migration window, requests to the old range may be redirected
>
> **Key concern: split latency.** If a queue suddenly goes from 1K msg/sec to 100K msg/sec (burst), the split process takes time. During this lag, the existing partition throttles. SQS mitigates this with:
> - Pre-warming: If a queue consistently grows, proactively add partitions before hitting limits
> - Throttling with backoff: Return 429/throttle responses, clients back off and retry
> - The high-throughput FIFO documentation confirms SQS 'automatically allocates additional partitions as request rates approach or exceed current partition capacity'"

#### Queue Placement Service

> "With millions of queues, we need a service that maps queue URLs to partition locations:
>
> ```
> Queue Placement Service (metadata):
>
>   queue_url → {
>     queue_type: STANDARD | FIFO,
>     partition_count: 5,
>     partitions: [
>       {id: P0, hash_range: [0, 199], nodes: [AZ-a:N1, AZ-b:N4, AZ-c:N7]},
>       {id: P1, hash_range: [200, 399], nodes: [AZ-a:N2, AZ-b:N5, AZ-c:N8]},
>       ...
>     ],
>     config: {
>       visibility_timeout: 30,
>       retention_period: 345600,  // 4 days
>       delay_seconds: 0,
>       max_message_size: 262144,  // 256 KB
>       redrive_policy: {dlq_arn: '...', max_receive_count: 5}
>     }
>   }
> ```
>
> This metadata is small (~1 KB per queue) and heavily cached. The front-end servers keep a local cache of recently accessed queues' partition maps, refreshing every few seconds or on cache miss."

> *For the full deep dive on partitioning and scaling, see [scaling-and-performance.md](scaling-and-performance.md).*

#### Architecture Update After Phase 6

> "Our architecture has evolved:
>
> | | Before (Phase 5) | After (Phase 6) |
> |---|---|---|
> | **Queue Partitioning** | One partition per queue | **Hash-based partitioning: Standard queues shard by msg_id; FIFO queues shard by MessageGroupId** |
> | **Hot Queue Handling** | Would throttle | **Auto-split partitions as throughput grows; pre-warming for predictable bursts** |
> | **Queue Metadata** | Simple lookup | **Queue Placement Service with partition maps, cached by front-ends** |

---

### L5 vs L6 vs L7 — Phase 6 Rubric

| Aspect | L5 (SDE-2) | L6 (SDE-3) | L7 (Principal) |
|---|---|---|---|
| **Partitioning strategy** | "Shard the queue across servers" | Differentiates Standard (hash by msg_id) from FIFO (hash by MessageGroupId), explains why FIFO constrains parallelism | Discusses partition rebalancing mechanics, split lag during burst traffic, pre-warming strategies, cross-AZ partition placement for latency |
| **FIFO ordering** | "Messages come out in order" | Explains MessageGroupId as the unit of ordering, one in-flight per group, parallel across groups | Discusses how MessageGroupId cardinality affects throughput, hot message group mitigation, and the throughput/ordering tradeoff curve |
| **Auto-scaling** | "Add more servers when busy" | Describes the partition split process, partition map propagation to front-ends | Discusses split heuristics (throughput vs storage triggers), migration coordination, back-pressure during splits, capacity planning |

---

## PHASE 7: Deep Dive — Delivery Guarantees & FIFO Semantics (~8 min)

**Interviewer:**
Let's talk about correctness. You mentioned Standard queues are at-least-once and FIFO queues are exactly-once. Walk me through how each is implemented, and specifically what "exactly-once processing" means.

**Candidate:**

> "Let me start with why Standard queues are at-least-once, then explain how FIFO achieves exactly-once processing."

#### Standard Queues: At-Least-Once Delivery

> "In our partitioned, replicated architecture, a Standard queue message can be delivered more than once in several scenarios:
>
> ```
> Scenario 1: Replication lag
>   1. Producer sends message → written to leader (AZ-a) and follower (AZ-b)
>   2. Follower in AZ-c has slight replication lag
>   3. Consumer A reads from leader → message marked in-flight on leader
>   4. Consumer B's ReceiveMessage is routed to AZ-c (follower) → message is still visible there
>   5. Both consumers get the same message
>
> Scenario 2: Visibility timeout ambiguity
>   1. Consumer receives message, starts processing
>   2. Processing takes longer than visibility timeout
>   3. Message becomes visible again
>   4. Another consumer receives and processes it
>   5. Original consumer finishes and deletes → but it was already processed twice
>
> Scenario 3: Network partition
>   1. Consumer receives message, processes it, sends DeleteMessage
>   2. DeleteMessage response is lost due to network issue
>   3. Consumer thinks delete failed, retries or message reappears
> ```
>
> **Why Standard queues tolerate this:** The design philosophy is that idempotent consumers are easier to build than distributed deduplication. Making DeleteMessage synchronous across all replicas would add latency and reduce availability. Instead, SQS pushes deduplication responsibility to the consumer (using the message's MessageId or a business-level idempotency key)."

#### FIFO Queues: Exactly-Once Processing

> "FIFO queues solve both ordering and deduplication. The key mechanisms:
>
> **1. Message Deduplication (exactly-once send):**
>
> ```
> Producer sends:
>   SendMessage(
>     QueueUrl: 'my-queue.fifo',
>     MessageBody: '{"order": 42}',
>     MessageGroupId: 'user-A',
>     MessageDeduplicationId: 'order-42-v1'   // explicit dedup ID
>   )
>
> SQS deduplication logic:
>   - Check if MessageDeduplicationId 'order-42-v1' was seen in the last 5 minutes
>   - If yes → return the original MessageId (don't store a duplicate)
>   - If no → store the message, record the dedup ID with a 5-minute TTL
> ```
>
> The 5-minute deduplication interval is a documented SQS guarantee. Within this window, sending the same MessageDeduplicationId is idempotent.
>
> **Content-based deduplication** is an alternative: enable it on the queue, and SQS computes a SHA-256 hash of the message body as the deduplication ID. Useful when you don't have a natural dedup key.
>
> **2. Ordered Delivery within Message Groups:**
>
> ```
> Messages in group 'user-A' arrive in this order:
>   M1 (sent at T1) → M2 (sent at T2) → M3 (sent at T3)
>
> Delivery guarantees:
>   - M1 must be delivered before M2, M2 before M3
>   - While M1 is in-flight, M2 and M3 are NOT deliverable
>   - Only after M1 is deleted (or visibility timeout expires) does M2 become available
>   - This means: one in-flight message per message group at a time
> ```
>
> **3. Exactly-Once Processing (exactly-once receive):**
>
> FIFO queues don't deliver a message to a second consumer while it's in-flight with the first consumer. Combined with send-side deduplication, this achieves exactly-once processing:
> - Send-side dedup prevents duplicates from entering the queue
> - Receive-side serialization (one in-flight per group) prevents duplicate delivery
>
> **The throughput cost:**
>
> | Guarantee | Standard Queue | FIFO Queue |
> |---|---|---|
> | Ordering | None (best-effort) | Strict per MessageGroupId |
> | Deduplication | None | 5-minute window via MessageDeduplicationId |
> | In-flight per group | No limit | 1 message |
> | Throughput | Unlimited | 300/sec (no batch) or 3,000/sec (with batch) per partition |
>
> The fundamental reason FIFO throughput is lower: serializing delivery within a message group means you can't parallelize reads for that group. The system throughput scales with the number of *distinct* message groups, not the total message rate."

**Interviewer:**
What are the edge cases that break exactly-once?

**Candidate:**

> "Good question — 'exactly-once processing' has a specific meaning in SQS, and there are edge cases:
>
> 1. **Dedup window expiry:** If a producer retries a message more than 5 minutes after the original send, the dedup window has passed and the message will be stored as a new message. The consumer sees it twice. Mitigation: ensure retries happen within 5 minutes.
>
> 2. **Consumer visibility timeout expiry:** If a consumer takes too long and the visibility timeout expires, the message re-appears and another consumer processes it. This is 'at-least-once at the consumer level.' Mitigation: use ChangeMessageVisibility heartbeat to extend timeout, set visibility timeout higher than expected processing time.
>
> 3. **Application-level non-idempotency:** SQS guarantees the message is delivered exactly once to a consumer. But if the consumer processes the message, crashes before calling DeleteMessage, and the message reappears — the *application side-effect* has already happened. SQS doesn't know about your application's state. Mitigation: make the processing idempotent even with FIFO, or use the message's SequenceNumber for application-level dedup.
>
> So 'exactly-once processing' in SQS really means: **within the dedup window and visibility timeout, the system guarantees each message is delivered to exactly one consumer exactly once.** But the consumer still needs to handle edge cases at the application level."

> *For the full deep dive on delivery guarantees and FIFO semantics, see [consistency-and-delivery.md](consistency-and-delivery.md).*

#### Architecture Update After Phase 7

> "Our delivery guarantee layer is now well-defined:
>
> | | Before (Phase 6) | After (Phase 7) |
> |---|---|---|
> | **Standard Delivery** | "At-least-once" (hand-wavy) | **At-least-once due to replication lag, visibility timeout races, and network partitions; consumers must be idempotent** |
> | **FIFO Deduplication** | Undefined | **5-minute dedup window using MessageDeduplicationId or content-based SHA-256 hash** |
> | **FIFO Ordering** | Undefined | **Strict per MessageGroupId; one in-flight per group; throughput scales with group cardinality** |

---

### L5 vs L6 vs L7 — Phase 7 Rubric

| Aspect | L5 (SDE-2) | L6 (SDE-3) | L7 (Principal) |
|---|---|---|---|
| **At-least-once** | "Messages might be delivered twice" | Enumerates specific scenarios (replication lag, visibility timeout, network partition) that cause duplicates | Discusses why synchronous cross-replica delete would fix this but kill availability, frames it as CAP tradeoff |
| **FIFO exactly-once** | "FIFO queues don't have duplicates" | Explains the two mechanisms (send-side dedup with 5-min window, receive-side serialization per group) and the throughput cost | Discusses edge cases that break exactly-once (dedup window expiry, consumer-side failures), proposes application-level idempotency patterns |
| **MessageGroupId** | "Groups messages together" | Explains it as the partitioning and ordering key, one in-flight per group, throughput scales with distinct groups | Discusses cardinality tuning (too few groups = serialized, too many = lose meaningful ordering), hot group mitigation |

---

## PHASE 8: Deep Dive — Dead Letter Queues & Operational Patterns (~5 min)

**Interviewer:**
Let's talk about what happens when things go wrong. A consumer keeps failing to process a message. Walk me through the dead letter queue mechanism.

**Candidate:**

> "Dead letter queues (DLQs) are one of the most important operational features in SQS. Without them, a 'poison message' — a message that always fails processing — blocks your queue forever (in FIFO) or creates an infinite retry loop (in Standard).
>
> **How DLQs work:**
>
> ```
> Source Queue: 'order-processing'
>   Redrive Policy: { deadLetterTargetArn: 'arn:...order-processing-dlq', maxReceiveCount: 5 }
>
> Message lifecycle with DLQ:
>
>   1. Message M1 sent to 'order-processing'
>   2. Consumer receives M1 (receive_count = 1) → processing fails → doesn't delete
>   3. Visibility timeout expires → M1 reappears (receive_count = 1)
>   4. Consumer receives M1 again (receive_count = 2) → fails again
>   5. ... repeats ...
>   6. Consumer receives M1 (receive_count = 5) → fails again
>   7. receive_count reaches maxReceiveCount (5)
>   8. SQS moves M1 to 'order-processing-dlq' ← the dead letter queue
>   9. M1 no longer blocks 'order-processing'; an operator can inspect the DLQ
> ```
>
> **Key details:**
>
> - **maxReceiveCount** — The number of times a message can be received before being moved to the DLQ. Must be set thoughtfully: too low and transient failures (network hiccup, consumer restart) cause messages to DLQ prematurely. Too high and poison messages block processing for too long. Starting point: 3-5.
>
> - **Redrive allow policy** — Controls which source queues can use a specific queue as their DLQ. Options: `allowAll` (default), `byQueue` (up to 10 source queue ARNs), or `denyAll`.
>
> - **Retention period interaction** — The message's original enqueue timestamp is **preserved** when moved to a DLQ (for Standard queues). This means if the source queue has 4-day retention, the message has already used some of that retention time in the source queue. The DLQ's retention period should be **longer** than the source queue's to avoid premature expiration. For FIFO queues, the enqueue timestamp **resets** when moved to the DLQ.
>
> - **DLQ redrive** — SQS supports moving messages from the DLQ back to the source queue (or a custom destination) after the issue is fixed. This is the 'redrive' operation — essentially re-enqueuing failed messages for reprocessing."

**Interviewer:**
What about monitoring and alarming? How would you operate this at scale?

**Candidate:**

> "Operating SQS at scale requires monitoring at two levels: the service level (are queues healthy?) and the customer level (is this specific queue behaving normally?).
>
> **Key metrics to monitor (CloudWatch):**
>
> | Metric | What It Tells You | Alarm Threshold |
> |---|---|---|
> | `ApproximateNumberOfMessagesVisible` | Queue depth — how many messages are waiting | Sustained growth = consumers can't keep up |
> | `ApproximateNumberOfMessagesNotVisible` | In-flight messages — being processed | Near 120,000 = approaching in-flight limit |
> | `ApproximateAgeOfOldestMessage` | How long the oldest message has been waiting | Growing = consumer lag, risk of hitting retention limit |
> | `NumberOfMessagesSent` | Send throughput | Baseline deviation detection |
> | `NumberOfMessagesReceived` | Receive throughput | Compare with sent — should be similar |
> | `NumberOfMessagesDeleted` | Delete throughput | Much lower than received = consumers failing to process |
> | `NumberOfEmptyReceives` | Polling efficiency | High rate = use long polling |
> | `ApproximateNumberOfMessagesDelayed` | Messages in delay state | Expected for delay queues |
>
> **Critical alarms:**
> 1. **DLQ message count > 0** — Any message in a DLQ deserves investigation
> 2. **Queue depth growing over time** — Consumers can't keep up; need to scale out
> 3. **Age of oldest message approaching retention** — Messages at risk of being expired before processing
> 4. **In-flight count near 120,000** — Need more consumers or faster processing
>
> **Operational patterns:**
>
> - **Auto-scaling consumers based on queue depth:** Use CloudWatch alarms to trigger EC2 Auto Scaling or ECS service scaling when `ApproximateNumberOfMessagesVisible` exceeds a threshold.
>
> - **Lambda event source mapping:** SQS can trigger Lambda functions directly. Lambda automatically scales the number of concurrent invocations based on queue depth, polling in batches.
>
> - **Backpressure via visibility timeout:** If consumers are overwhelmed, they can set visibility timeout to 0 on messages they can't process, returning them to the queue immediately for another consumer."

---

## PHASE 9: Deep Dive — Delay Queues & Advanced Features (~3 min)

**Candidate:**

> "Let me briefly cover delay queues and a few other features that round out the design."

#### Delay Queues

> "Delay queues postpone delivery of **all new messages** for a configurable period:
>
> - **DelaySeconds**: 0 to 900 seconds (15 minutes). Default: 0.
> - All messages sent to the queue are invisible for the delay period before becoming available.
> - **Per-message delay** (message timers): Override the queue's delay for individual messages. A message can have its own DelaySeconds value.
>
> **Standard vs FIFO behavior difference:**
> - Standard: Changing the queue's DelaySeconds does NOT affect messages already in the queue (not retroactive)
> - FIFO: Changing the queue's DelaySeconds DOES affect messages already in the queue (retroactive)
>
> **Implementation:** Same `visible_at` mechanism as visibility timeout. When a message is sent with a delay, `visible_at` is set to `sent_at + delay_seconds` instead of `sent_at`. The message sits in DELAYED state until `visible_at` passes.
>
> **For delays beyond 15 minutes**, AWS recommends EventBridge Scheduler, which supports scheduling billions of one-time or recurring API actions with no time limitations."

#### Message Attributes

> "Each message can carry up to **10 custom attributes** alongside the body:
>
> - Attributes have a Name (up to 256 chars), Type (String, Number, or Binary), and Value
> - Attribute data counts toward the overall message size limit
> - System attributes (like `AWSTraceHeader` for X-Ray tracing) are separate and do NOT count toward the message size limit
> - Attributes enable consumers to filter/route messages without parsing the body"

#### Server-Side Encryption

> "SQS supports encryption at rest:
> - **SSE-SQS**: SQS-managed keys (similar to SSE-S3)
> - **SSE-KMS**: Customer-managed keys via AWS KMS — provides audit trail, key rotation, and fine-grained key policies
> - Encryption is transparent to producers and consumers — they send/receive plaintext; SQS handles encrypt/decrypt"

---

## PHASE 10: Wrap-Up & Summary (~3 min)

**Interviewer:**
We're running low on time. Summarize your design and tell me what keeps you up at night operating this system.

**Candidate:**

> "**Design evolution — how we got here:**
>
> | Component | Started With | Evolved To | Why |
> |---|---|---|---|
> | **Architecture** | Single server, in-memory queue | 3-layer: front-end, queue metadata, message storage | Separate concerns that scale differently |
> | **Message Storage** | In-memory, no persistence | Replicated log-structured storage across 3 AZs, quorum writes [INFERRED] | Durability + availability under AZ failure |
> | **Visibility Timeout** | Simple dequeue (message gone forever) | `visible_at` timestamp field; no timer needed; extends via ChangeMessageVisibility; 12-hour max | Handles consumer failures gracefully, scalable |
> | **Queue Partitioning** | One partition per queue | Auto-partitioning: Standard by msg hash, FIFO by MessageGroupId hash | Hot queues need horizontal scaling |
> | **Standard Delivery** | Undefined | At-least-once; consumers must be idempotent | Replication lag + visibility races make exactly-once impractical at unlimited throughput |
> | **FIFO Delivery** | Undefined | Exactly-once processing: 5-min dedup window + one-in-flight per message group | Coordination cost limits throughput to 300/3,000 msg/sec per partition |
> | **Polling** | Undefined | Short (subset sampling, immediate) vs Long (all servers, up to 20s wait) | Long polling eliminates false empties, saves cost |
>
> **Final Architecture:**
>
> ```
> FINAL ARCHITECTURE:
>
>                            ┌──────────────────────┐
>                            │       Clients         │
>                            │  (SDKs, CLI, Lambda)  │
>                            └──────────┬───────────┘
>                                       │ HTTPS (REST API)
>                            ┌──────────▼───────────┐
>                            │    Front-End Layer    │
>                            │  Auth (SigV4)         │
>                            │  Rate Limiting        │
>                            │  Request Routing      │
>                            │  Long-Poll Manager    │
>                            └──────────┬───────────┘
>                                       │
>                    ┌──────────────────┼──────────────────┐
>                    │                                     │
>         ┌──────────▼─────────┐            ┌─────────────▼────────────┐
>         │  Queue Metadata    │            │   Message Storage        │
>         │  Service           │            │   Layer                  │
>         │                    │            │                          │
>         │  queue_url →       │            │   Per-partition:         │
>         │  {config,          │            │   Leader + Followers     │
>         │   partition_map}   │            │   across 3 AZs           │
>         │                    │            │                          │
>         │  Partition maps    │            │   Log-structured engine  │
>         │  cached by         │            │   [INFERRED]             │
>         │  front-ends        │            │                          │
>         │                    │            │   Standard: hash-sharded │
>         │  DLQ configs,      │            │   FIFO: group-sharded   │
>         │  encryption keys,  │            │                          │
>         │  IAM policies      │            │   Visibility via         │
>         └────────────────────┘            │   visible_at timestamps  │
>                                          │                          │
>                                          │   AZ-a  AZ-b  AZ-c      │
>                                          └──────────────────────────┘
> ```
>
> **What keeps me up at night:**
>
> 1. **Poison messages and DLQ overflow** — A single malformed message can block a FIFO message group forever if it always fails processing. Even with DLQs, if the DLQ fills up or its retention expires before an operator notices, messages are lost. I'd want automated alerting on DLQ depth with page-level severity.
>
> 2. **Noisy neighbor / hot queue isolation** — One customer's queue receiving 1M msg/sec can saturate a storage node, affecting other queues on the same node. This requires **shuffle sharding** — spreading each customer's queues across random subsets of storage nodes so no single customer can take down a large fraction of the fleet. Cell-based architecture limits blast radius.
>
> 3. **Clock skew and visibility timeout** — The `visible_at` mechanism relies on clocks being reasonably synchronized across storage nodes. If a leader and follower disagree on the current time, a message might be invisible on the leader but visible on a follower (or vice versa). NTP + bounded clock drift is critical, but in a degraded state (NTP server unreachable), clock drift could cause message delivery anomalies.
>
> 4. **Partition split lag during traffic bursts** — When a queue suddenly receives 10x its normal traffic (flash sale, incident response), the auto-partitioning system needs time to detect, split, and migrate. During this lag, the queue throttles. Pre-warming via API or predictive scaling based on historical patterns would help, but there's always a cold-start risk.
>
> 5. **FIFO message group hot spots** — If a FIFO queue has one message group receiving 90% of traffic and that group's consumer is slow, it creates head-of-line blocking. The throughput limit (300/3,000 msg/sec) is per partition, but within a single group, it's sequential. Educating customers about message group cardinality is an operational concern.
>
> 6. **Data plane / control plane separation** — Queue creation (CreateQueue), configuration changes (SetQueueAttributes), and message operations (Send/Receive/Delete) must be on separate infrastructure. A surge in CreateQueue calls should never impact message delivery. Control plane and data plane must have independent scaling and failure domains.
>
> **Potential extensions:**
>
> - **SQS-Lambda integration** — Event source mapping where Lambda polls SQS and auto-scales invocations
> - **Message filtering** — Filter messages at the subscription level (already available with SNS → SQS)
> - **FIFO high-throughput mode** — Automatic partition management for FIFO queues (already shipped)
> - **Cross-region queue replication** — For disaster recovery (not yet a native SQS feature, unlike S3's CRR)
> - **Server-side message transformation** — Modify message format in-transit (like S3 Object Lambda)
> - **Priority queues** — Native support for message priorities (currently simulated with multiple queues)"

---

## Interviewer's Final Assessment

**Hire Recommendation: Strong Hire (L6 — solid SDE-3)**

| Dimension | Rating | Notes |
|---|---|---|
| **Problem Decomposition** | Exceeds Bar | Clean layered architecture. Immediately separated queue metadata from message storage, identified visibility timeout as the core challenge. |
| **Requirements & Scoping** | Exceeds Bar | Strong Standard vs FIFO framing from the start. Explained *why* the tradeoff exists (coordination cost vs throughput) before being asked. |
| **Scale Estimation** | Meets Bar | Reasonable estimates. Correctly identified ReceiveMessage as the hot path and ephemeral storage as a key design constraint. |
| **Message Lifecycle** | Exceeds Bar | Full state machine with AVAILABLE/IN_FLIGHT/DELAYED/DELETED. The `visible_at` timestamp approach was clean and scalable. |
| **Visibility Timeout** | Exceeds Bar | Explained the mechanism, ChangeMessageVisibility heartbeat pattern, 12-hour max, and in-flight limit reasoning. |
| **Partitioning** | Exceeds Bar | Distinguished Standard (hash by msg_id) from FIFO (hash by MessageGroupId). Correctly identified MessageGroupId cardinality as the throughput lever for FIFO. |
| **Delivery Guarantees** | Exceeds Bar | Enumerated specific scenarios causing duplicate delivery in Standard. Explained both halves of FIFO exactly-once (send dedup + receive serialization) and the edge cases. |
| **Long vs Short Polling** | Exceeds Bar | Explained the subset-sampling vs all-server difference, infrastructure implications of held connections. |
| **DLQ & Operations** | Meets Bar | Covered DLQ mechanics, monitoring metrics, auto-scaling consumers. |
| **Communication** | Exceeds Bar | Structured, iterative (Attempt 0 → 3), used diagrams and tables, proactively identified tradeoffs before asked. |
| **LP: Dive Deep** | Exceeds Bar | Went deep on FIFO dedup edge cases, visibility timeout implementation, and partition split lag unprompted. |
| **LP: Think Big** | Meets Bar | Good extensions section, awareness of operational concerns at scale. |

**What would push this to L7:**
- Cell-based architecture design with explicit failure domain boundaries and blast radius calculation
- Formal analysis of the consistency model (linearizability of visibility timeout operations across replicas)
- Detailed shuffle sharding mechanics: how many cells, how to map customers to cells, what happens during cell failure
- Cost modeling: $/message at different throughput tiers, how storage engine choice affects cost per message
- Multi-region strategy: active-active vs active-passive, message replication lag SLAs, conflict resolution
- Deeper operational maturity: game days, chaos engineering for SQS, automated remediation runbooks

---

## Key Differences: L5 vs L6 vs L7 Expectations for This Problem

| Aspect | L5 (SDE-2) | L6 (SDE-3) | L7 (Principal) |
|---|---|---|---|
| **Requirements** | Lists send/receive/delete, mentions durability | Quantifies delivery guarantees, explains Standard vs FIFO tradeoff, knows specific numbers (5-min dedup, 120K in-flight, 300/3000 FIFO throughput) | Frames as distributed systems impossibility result (can't have unlimited throughput + ordering + exactly-once), proposes SLA commitments with error budgets |
| **Architecture** | "Queue backed by a database" | 3-layer architecture with clear separation of metadata/storage, identifies visibility timeout as core challenge | Cell-based architecture with failure domains, data plane/control plane separation, discusses deployment topology |
| **Message Storage** | "Write to disk with replication" | Reasons about storage engine choice (LSM vs B-tree), explains quorum writes, TTL-based expiration | Discusses WAL design, compaction strategies, storage capacity planning, replica divergence handling |
| **Visibility Timeout** | "Message is hidden for a while" | Full mechanism: `visible_at` timestamp, no timer needed, ChangeMessageVisibility, 12-hour max, heartbeat pattern | Discusses clock skew across replicas, visibility consistency during leader failover, in-flight limit memory modeling |
| **FIFO Semantics** | "Messages come out in order" | Explains MessageGroupId as ordering/partitioning key, dedup via MessageDeduplicationId with 5-min window, one-in-flight-per-group | Discusses dedup storage scaling, group rebalancing during partition splits, edge cases that break exactly-once, comparison with Kafka consumer groups |
| **Polling** | "Consumer asks for messages" | Differentiates short (subset sampling) vs long (all servers, 20s max), explains cost implications | Discusses adaptive polling, connection management at scale, back-pressure signaling from storage to front-end |
| **DLQ** | "Failed messages go to another queue" | Explains maxReceiveCount, retention timestamp behavior (Standard vs FIFO difference), redrive | Discusses DLQ monitoring strategy, automated remediation, DLQ-as-debugging-tool patterns, queue topology design |
| **Operational Thinking** | "Monitor queue depth" | Identifies specific metrics, alarm thresholds, auto-scaling patterns | Proposes runbooks, game days, blast radius isolation, discusses control plane/data plane separation as operational boundary |
| **Communication** | Answers questions when asked | Drives conversation, iterative build-up, uses diagrams, proactively identifies tradeoffs | Negotiates scope, proposes phased approach, manages interview time, connects design decisions to business impact |

---

*For detailed deep dives on each component, see the companion documents:*
- [Message Lifecycle](message-lifecycle.md) — State machine, visibility timeout, delay queues, retention, ChangeMessageVisibility
- [Scaling & Performance](scaling-and-performance.md) — Partition strategies, auto-scaling, long polling, batching, hot queue mitigation
- [Consistency & Delivery](consistency-and-delivery.md) — At-least-once vs exactly-once, FIFO dedup, MessageGroupId ordering, edge cases
- [Dead Letter Queues & Operations](dlq-and-operations.md) — DLQ mechanics, redrive, monitoring, alarming, operational patterns

*End of interview simulation.*
