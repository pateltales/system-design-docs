# System Design Interview Simulation: Design Amazon SNS (Simple Notification Service)

> **Interviewer:** Principal Engineer (L8), Amazon SNS Team
> **Candidate Level:** SDE-3 (L6 -- Senior Software Development Engineer)
> **Duration:** ~60 minutes
> **Date:** February 12, 2026

---

## PHASE 1: Opening & Problem Statement (~2 min)

**Interviewer:**
Hey, welcome. I'm [name], Principal Engineer on the SNS messaging team. For today's system design round, I'd like you to design a **pub/sub notification service** -- think Amazon SNS. A system where publishers send a message to a topic once, and that message is delivered to potentially thousands of heterogeneous subscribers -- SQS queues, Lambda functions, HTTP endpoints, email addresses, SMS numbers, mobile push. We're talking about the core fan-out infrastructure, not just an API wrapper.

I care about how you think through delivery guarantees, fan-out at scale, and the tradeoffs of pushing messages to wildly different endpoint types. I'll push on your decisions -- that's me calibrating depth, not a negative signal.

Take it away.

---

## PHASE 2: Requirements Gathering & Scoping (~5 min)

**Candidate:**
Thanks! SNS spans a wide design space -- from a simple pub/sub bus to mobile push to SMS delivery. Let me scope this down before I draw anything.

**Functional Requirements -- what operations do we need?**

> "The core API operations I'd expect:
> - **CreateTopic** -- create a named topic that acts as a logical channel for messages.
> - **Subscribe** -- register an endpoint (SQS, Lambda, HTTP/S, email, SMS, mobile push) to receive messages from a topic.
> - **Publish** -- send a message to a topic. SNS fans it out to all confirmed subscriptions.
> - **Unsubscribe** -- remove a subscription.
> - **SetSubscriptionAttributes** -- configure per-subscription settings like filter policies, delivery policies, and DLQ.
>
> A few clarifying questions:
> - **Do we need to support message filtering?** So subscribers receive only a subset of messages published to the topic?"

**Interviewer:** "Yes, message filtering is critical. Many customers have one topic with hundreds of subscribers, and each subscriber only cares about a fraction of the messages. Without filtering, every subscriber gets everything and has to discard what it doesn't need -- wasteful."

> "- **Standard topics vs FIFO topics?** Standard gives best-effort ordering and at-least-once delivery. FIFO gives strict ordering per message group and exactly-once processing."

**Interviewer:** "Cover both, but focus on standard topics first -- that's 99% of traffic. Discuss FIFO as a deep dive."

> "- **What subscriber types are in scope?** I'm counting: SQS queues, Lambda functions, HTTP/S endpoints, email, email-JSON, SMS, mobile push (APNs, FCM), and Firehose delivery streams."

**Interviewer:** "Yes, all of those. But the architectural challenge is really about app-to-app delivery -- SQS, Lambda, HTTP. The person-facing ones (email, SMS, mobile push) have different delivery mechanics but use the same fan-out infrastructure."

> "- **Cross-account and cross-region publishing?** Can an account publish to a topic owned by another account?"

**Interviewer:** "Mention it, but don't deep dive. Focus on the core fan-out path."

**Non-Functional Requirements:**

> "Now the constraints that shape the architecture:
>
> | Dimension | Requirement | Rationale |
> |---|---|---|
> | **Delivery guarantee** | At-least-once for standard topics | A published message must be delivered to every subscriber at least once. Duplicates are possible; subscribers must be idempotent. |
> | **Ordering** | Best-effort for standard; strict per message group for FIFO | Standard topics don't guarantee order. FIFO topics guarantee order within a message group. |
> | **Durability** | Messages stored across multiple AZs before returning success | A Publish that returns 200 must not lose the message, even if an AZ goes down before fan-out completes. |
> | **Availability** | 99.99%+ for the Publish API | Publishers must always be able to send. Fan-out can be slightly delayed but Publish must not block. |
> | **Latency** | < 20ms p50 for Publish; < 30ms for delivery to SQS/Lambda | Publishers need low latency. Delivery latency varies by endpoint type (SQS is fast; email is slow). |
> | **Fan-out scale** | Up to 12.5 million subscriptions per standard topic | One Publish call can trigger millions of deliveries. |
> | **Message size** | Up to 256 KB per message (up to 2 GB with Extended Client Library via S3) | Small payloads -- this is a notification service, not a data pipeline. |
> | **Throughput** | Up to 30,000 publishes/sec per standard topic (US East) | Verified from AWS quotas: region-dependent, soft limit. |
> | **Multi-tenancy** | Millions of topics across millions of customers | One customer's burst must not affect others. |
>
> The most important thing about SNS: **it's a push-based system**. Unlike SQS where consumers pull, SNS pushes to every subscriber. This fundamentally shapes the architecture -- the fan-out is done by SNS, not by the consumers."

**Interviewer:**
Good. You mentioned at-least-once delivery and idempotent subscribers. Why not exactly-once for standard topics?

**Candidate:**

> "Exactly-once delivery in a distributed push system is extremely expensive. Here's why:
>
> When SNS delivers a message to a subscriber (say, an HTTP endpoint), the endpoint might process the message but the 200 OK response gets lost due to a network partition. SNS has no way to know if the message was processed, so it retries -- delivering a duplicate.
>
> To prevent this, you'd need either:
> 1. **Two-phase commit** between SNS and every subscriber -- impossibly slow at scale with heterogeneous endpoints
> 2. **Idempotency tokens** -- which is what FIFO topics do, but only with SQS FIFO queues as subscribers, where both sides can coordinate deduplication using a 5-minute deduplication window
>
> For standard topics with arbitrary HTTP endpoints, email, SMS -- the only practical guarantee is at-least-once. The subscriber must be idempotent. This is the same tradeoff that every pub/sub system makes: Kafka, Google Pub/Sub, Azure Service Bus."

**Interviewer:**
Good reasoning. Let's get some numbers.

---

### L5 vs L6 vs L7 -- Phase 2 Rubric

| Aspect | L5 (SDE-2) | L6 (SDE-3) | L7 (Principal) |
|---|---|---|---|
| **Functional Reqs** | Lists Publish/Subscribe operations | Proactively raises filtering, FIFO vs standard, heterogeneous endpoint types, DLQ | Additionally discusses message archiving/replay, cross-account access patterns, EventBridge comparison |
| **Non-Functional** | Mentions "at-least-once delivery" | Quantifies limits (12.5M subs/topic, 256 KB message, 30K publishes/sec), explains why not exactly-once | Frames NFRs as SLA commitments, discusses cost-per-delivery, blast radius isolation per customer |
| **Scoping** | Accepts problem as given | Drives clarifying questions, distinguishes app-to-app vs app-to-person | Negotiates scope based on time, proposes phased deep dives, identifies which deep dives yield most signal |

---

## PHASE 3: Scale Estimation (~3 min)

**Candidate:**

> "Let me estimate SNS-scale numbers to ground our design decisions."

#### Traffic Estimates

> "SNS is one of the highest-throughput AWS services. Let me work with realistic numbers:
>
> - **Active topics**: 100 million topics across all accounts
> - **Active subscriptions**: 1 billion total subscriptions
> - **Publish rate**: 10 million messages/sec globally at peak
> - **Average subscriptions per topic**: ~10 (heavily skewed -- most topics have 1-5 subs, a few have millions)
> - **Fan-out deliveries**: 10M publishes x 10 avg subs = **100 million deliveries/sec** at peak
> - **This is the critical number** -- 100M deliveries/sec to heterogeneous endpoints (SQS, Lambda, HTTP, email, SMS, push)"

#### Message Size and Bandwidth

> "- **Average message size**: 2 KB (SNS messages are typically small -- metadata, events, notifications)
> - **Publish inbound bandwidth**: 10M msg/sec x 2 KB = **20 GB/sec** inbound
> - **Delivery outbound bandwidth**: 100M deliveries/sec x 2 KB = **200 GB/sec** outbound
> - **Fan-out amplification factor**: 10x -- for every byte published, we deliver 10 bytes. This is the core cost driver."

#### Metadata

> "- **Topic metadata**: topic ARN, owner, policy, attributes -- ~1 KB per topic
> - **Subscription metadata**: endpoint, protocol, filter policy, delivery policy, DLQ config -- ~2 KB per subscription
> - **Total metadata**: 100M topics x 1 KB + 1B subscriptions x 2 KB = **~2.1 TB** of metadata
> - This is manageable -- fits in a distributed metadata store. Much smaller than S3's 100 PB metadata problem.
>
> The real challenge in SNS is not storage -- it's **delivery throughput**. 100 million deliveries per second to endpoints that vary wildly in latency and reliability."

**Interviewer:**
Good. The delivery throughput number is the one that matters. And you're right that the metadata is relatively small compared to the delivery engine. Let's architect this.

---

## PHASE 4: High-Level Architecture (~5 min)

**Candidate:**

> "Let me start with the simplest thing that could work, find the problems, and evolve."

#### Attempt 0: Direct Point-to-Point

> "Before there was pub/sub, there was direct notification. The publisher calls each subscriber directly:
>
> ```
>     Publisher
>       │
>       ├──── HTTP POST ──► Subscriber A (SQS queue)
>       ├──── HTTP POST ──► Subscriber B (Lambda)
>       └──── HTTP POST ──► Subscriber C (HTTP endpoint)
>
>     Publisher knows about all subscribers.
>     Publisher calls each one directly.
> ```
>
> **Problems:**
> 1. **Tight coupling** -- Publisher must know about every subscriber, their endpoint type, and their protocol
> 2. **Publisher blocked** -- If Subscriber C is slow (HTTP endpoint with 5 sec timeout), the publisher is stuck waiting before notifying others
> 3. **No durability** -- If the publisher crashes after notifying A but before B and C, those messages are lost
> 4. **No filtering** -- Publisher sends everything to everyone
> 5. **Adding/removing subscribers requires publisher changes**"

**Interviewer:**
Right. So introduce an intermediary.

#### Attempt 1: Topic as Intermediary

> "Introduce a topic server that decouples publishers from subscribers:
>
> ```
>     Publisher
>       │
>       │  Publish(topic, message)
>       ▼
>   ┌────────────────────┐
>   │   Topic Server     │
>   │                    │
>   │   topic → [sub_a,  │
>   │            sub_b,  │
>   │            sub_c]  │
>   │                    │
>   │   For each sub:    │
>   │     deliver(msg)   │
>   └────────────────────┘
>       │       │       │
>       ▼       ▼       ▼
>     SQS    Lambda   HTTP
>     (A)      (B)     (C)
> ```
>
> **This is better:**
> - Publisher sends once, topic fans out
> - Subscribers can be added/removed without publisher knowing
> - Topic server handles protocol differences
>
> **But still serious problems:**
> 1. **Single point of failure** -- Topic server goes down, all notifications stop
> 2. **No durability** -- If topic server crashes mid-fan-out, some subscribers miss the message
> 3. **Sequential delivery blocks** -- If we deliver to subs serially and one is slow, others wait
> 4. **Can't scale** -- One server can't handle 10M publishes/sec with 100M fan-out deliveries/sec"

**Interviewer:**
Good. Now fix the durability problem first -- what happens if the server crashes mid-fan-out?

#### Attempt 2: Durable Message Store + Async Fan-Out

> "The key insight: **separate accepting the message from delivering the message.**
>
> ```
>     Publisher
>       │
>       │  Publish(topic, message)
>       ▼
>   ┌────────────────────┐       ┌──────────────────────┐
>   │   API Layer        │       │   Message Store       │
>   │   (Stateless)      │──────►│   (Durable, Multi-AZ) │
>   │                    │       │                       │
>   │   1. Validate      │       │   msg_id → {topic,   │
>   │   2. Auth          │       │    body, attributes,  │
>   │   3. Store message │       │    timestamp}         │
>   │   4. Return 200 OK │       │                       │
>   └────────────────────┘       └───────────┬───────────┘
>                                            │
>                                 ┌──────────▼───────────┐
>                                 │   Fan-Out Engine      │
>                                 │   (Async Workers)     │
>                                 │                       │
>                                 │   Read message        │
>                                 │   Look up subs        │
>                                 │   Deliver to each     │
>                                 │   Track delivery      │
>                                 │   status              │
>                                 └───────────────────────┘
>                                    │       │       │
>                                    ▼       ▼       ▼
>                                  SQS    Lambda   HTTP
> ```
>
> **How a Publish works now:**
> 1. Publisher calls `Publish(topic, message)`
> 2. API layer authenticates, validates, stores the message durably (replicated across AZs)
> 3. Returns `200 OK` with `MessageId` -- **fast, publisher is unblocked**
> 4. Asynchronously, the fan-out engine picks up the message
> 5. Looks up all subscriptions for the topic
> 6. Delivers to each subscriber in parallel (each delivery is independent)
> 7. Failed deliveries are retried per the delivery policy
> 8. After all retries exhausted, failed messages go to the subscription's DLQ (if configured)
>
> **Why this is fundamentally better:**
> - Publisher latency is just the store latency (~10ms), not dependent on subscriber count or speed
> - Durable store means no message loss even if fan-out workers crash -- they pick up where they left off
> - Fan-out is parallel and independent per subscriber -- slow HTTP endpoint doesn't block SQS delivery
> - Scales horizontally by adding more fan-out workers"

**Interviewer:**
Good -- I like that you separated the acceptance path from the delivery path. That's the right architectural instinct. But I see several things to improve. How does the fan-out engine scale? How does it handle a topic with 12 million subscribers? How does filtering work? And what about different delivery retry behaviors per protocol?

**Candidate:**

> "Exactly -- three areas to evolve:
>
> | Layer | Current (Naive) | Problem |
> |-------|----------------|---------|
> | **Fan-out** | Single fleet of workers | Can't handle 12.5M subs per topic; need partitioned parallel fan-out |
> | **Filtering** | None -- deliver to all subs | Wasteful; subscribers process and discard messages they don't want |
> | **Delivery** | Same retry for all protocols | HTTP needs exponential backoff; SQS retries 100K+ times over 23 days; email/SMS have rate limits |
>
> Let's deep-dive each one."

**Interviewer:**
Let's start with the fan-out engine -- that's the heart of SNS.

---

### L5 vs L6 vs L7 -- Phase 4 Rubric

| Aspect | L5 (SDE-2) | L6 (SDE-3) | L7 (Principal) |
|---|---|---|---|
| **Architecture evolution** | Jumps to final architecture | Iterative build-up from direct → topic → durable store + async delivery | Frames each evolution step in terms of failure mode it fixes; discusses blast radius at each stage |
| **Separation of concerns** | Single monolithic service | Separates acceptance (API + store) from delivery (fan-out engine) | Adds control plane vs data plane separation, discusses deployment independence |
| **Durability insight** | "Replicate messages" | Explains store-then-fan-out: publisher returns before delivery, crash recovery | Discusses write-ahead log semantics, exactly-once delivery to the store via idempotent publish |

---

## PHASE 5: Deep Dive -- Fan-Out Engine (~10 min)

**Candidate:**

> "The fan-out engine is the core of SNS. Its job: given a message and a topic, deliver that message to every subscription on that topic, in parallel, handling failures independently per subscriber.
>
> Let me stress-test our current design:
> - A topic with **12.5 million subscriptions** -- one Publish must trigger 12.5M deliveries
> - **100 million deliveries/sec** globally
> - Subscribers have **wildly different latencies**: SQS delivery takes ~5ms, HTTP endpoints might take 5 seconds, email goes through SMTP gateways
> - **Slow or failing subscribers must not block fast ones**
>
> Our current single fleet of fan-out workers won't cut it. Let me evolve it."

#### Attempt 0: Serial Delivery

> "Walk the subscription list, deliver one by one:
>
> ```
> for sub in topic.subscriptions:
>     deliver(message, sub)  # blocking call
> ```
>
> Time for 12.5M subs at 5ms each = 62,500 seconds = **17 hours**. Absurd. Next."

#### Attempt 1: Thread Pool Per Message

> "Parallelize with a thread pool:
>
> ```
> thread_pool.submit_all([
>     deliver(message, sub) for sub in topic.subscriptions
> ])
> ```
>
> Better, but:
> - 12.5M threads? No. Even 10K threads on one machine is pushing it.
> - One machine can't hold 12.5M subscription records in memory
> - If this machine crashes, the entire fan-out is lost"

#### The Real Design: Partitioned Parallel Fan-Out

> "SNS partitions the subscription list and distributes fan-out across many workers:
>
> ```
>                     ┌──────────────────────────┐
>                     │     Message Store         │
>                     │  msg_id: {topic, body}    │
>                     └────────────┬──────────────┘
>                                  │
>                     ┌────────────▼──────────────┐
>                     │    Fan-Out Coordinator     │
>                     │                            │
>                     │  1. Look up topic's subs   │
>                     │  2. Partition sub list      │
>                     │     into chunks of ~1000   │
>                     │  3. Enqueue delivery tasks  │
>                     │     to worker fleet        │
>                     └────────────┬──────────────┘
>                                  │
>              ┌───────────────────┼───────────────────┐
>              │                   │                   │
>    ┌─────────▼────────┐ ┌──────▼─────────┐ ┌──────▼─────────┐
>    │ Delivery Worker 1│ │Delivery Worker 2│ │Delivery Worker N│
>    │                  │ │                 │ │                 │
>    │ Subs 1-1000:     │ │ Subs 1001-2000: │ │ Subs 12.499M-  │
>    │  deliver(msg,s1) │ │  deliver(msg,   │ │   12.5M:        │
>    │  deliver(msg,s2) │ │    s1001)       │ │  deliver(msg,   │
>    │  ...             │ │  ...            │ │    s12499001)   │
>    │                  │ │                 │ │  ...            │
>    └──────────────────┘ └─────────────────┘ └────────────────┘
>         │  │  │              │  │  │              │  │  │
>         ▼  ▼  ▼              ▼  ▼  ▼              ▼  ▼  ▼
>       SQS Lambda HTTP     SQS Lambda HTTP      SQS Lambda HTTP
> ```
>
> **How it works step by step:**
>
> 1. **Message arrives** in the message store (durable, multi-AZ)
> 2. **Fan-out coordinator** reads the topic's subscription list from the metadata store
> 3. **Partitions** the subscription list into chunks (e.g., 1000 subs per chunk)
> 4. **Enqueues a delivery task** per chunk onto an internal work queue
> 5. **Delivery workers** pull tasks from the queue, each handling one chunk
> 6. Within each chunk, the worker delivers in parallel (async I/O, not thread-per-delivery)
> 7. Each delivery is tracked independently -- if sub #47 fails, it's retried without affecting subs #1-46 or #48-1000
>
> **Why this scales:**
> - 12.5M subs / 1000 per chunk = 12,500 delivery tasks -- easily distributed across hundreds of workers
> - Each worker handles ~1000 concurrent deliveries using async I/O -- no thread explosion
> - Workers are stateless -- if one crashes, its tasks are re-queued to another worker
> - Fan-out coordinator is lightweight -- it just partitions and enqueues, it doesn't deliver"

**Interviewer:**
Good. How do you handle the case where a topic gets a burst of publishes -- say, 10,000 messages/sec to a topic with 10,000 subscribers?

**Candidate:**

> "That's 10,000 x 10,000 = **100 million deliveries per second** from a single topic. This is where the fan-out amplification really bites.
>
> **Back-pressure and flow control:**
>
> 1. **Rate limiting at the Publish API** -- The SNS quota for standard topics is up to 30,000 messages/sec per topic in US East (N. Virginia), lower in other regions. This is a soft limit that can be raised, but it's there to protect the system.
>
> 2. **Internal delivery queue depth monitoring** -- If the delivery queue backs up (too many tasks, not enough workers), the system signals the fan-out coordinator to slow down task creation.
>
> 3. **Per-subscriber delivery rate** -- SNS doesn't blast all messages at a subscriber simultaneously. For HTTP/S endpoints, you can configure `maxReceivesPerSecond` in the delivery policy. For SQS, delivery is essentially unlimited because SQS can absorb messages at high throughput.
>
> 4. **Prioritization** -- [INFERRED -- not officially documented] Internally, SNS likely prioritizes delivery to SQS and Lambda (which are fast and reliable) over HTTP endpoints (which are slow and flaky). This prevents slow HTTP subscribers from consuming all the delivery capacity.
>
> 5. **Cell-based isolation** -- [INFERRED -- not officially documented] SNS likely uses cell-based architecture where each cell handles a subset of topics. A burst on one topic in Cell A doesn't starve topics in Cell B."

**Interviewer:**
What about the fan-out coordinator -- isn't that a bottleneck? If one coordinator handles all topics, it's a single point of failure.

**Candidate:**

> "Right -- the fan-out coordinator must be distributed too. Here's how I'd design it:
>
> - **Topics are hash-partitioned** across coordinator nodes. Each coordinator owns a partition of topics.
> - When a message is stored, the message store notifies the coordinator responsible for that topic (using the partition map).
> - **Coordinator failure**: If a coordinator node dies, its topic partition is reassigned to another node (using a consensus-based coordination service like ZooKeeper or an internal equivalent).
> - **Stateless fan-out**: The coordinator doesn't store delivery state -- it reads the subscription list and enqueues tasks. The delivery workers are the ones tracking per-subscription delivery status.
>
> This means the coordinator is lightweight and horizontally scalable. The heavy lifting is in the delivery workers."

**Interviewer:**
How does message filtering fit into this fan-out?

**Candidate:**

> "Great question -- this is where filtering saves enormous resources.
>
> **Without filtering:** Topic has 1000 subs, publish a message, deliver to all 1000.
> **With filtering:** 800 subs have filter policies. Only 200 match this message. Deliver to 200 instead of 1000.
>
> **Where filtering happens:**
>
> ```
> Publisher: Publish(topic, message, attributes={event_type: 'order_placed', region: 'us-east'})
>
>   Fan-Out Coordinator:
>     1. Read subscription list (1000 subs)
>     2. For each sub, evaluate filter policy against message attributes:
>
>        Sub A: filter = {event_type: ['order_placed']}         → MATCH ✓ → deliver
>        Sub B: filter = {event_type: ['order_cancelled']}      → NO MATCH ✗ → skip
>        Sub C: filter = {region: ['eu-west']}                  → NO MATCH ✗ → skip
>        Sub D: no filter policy                                → MATCH ✓ → deliver (no filter = accept all)
>
>     3. Only create delivery tasks for matching subs
> ```
>
> **Key design decisions:**
>
> 1. **Filter at the SNS side, not the subscriber side** -- This is the entire point. If subscribers filter themselves, they still receive and process every message. SNS filtering prevents the message from ever being sent to non-matching subscribers. This saves delivery bandwidth, reduces subscriber load, and reduces cost (you pay per delivery).
>
> 2. **Filter policy scope** -- SNS supports two scopes:
>    - `MessageAttributes` (default): filter against structured message attributes (key-value pairs separate from the body)
>    - `MessageBody`: filter against properties in the JSON message body itself
>
> 3. **Filter operators** -- String exact match, prefix, suffix, anything-but, equals-ignore-case, numeric ranges, IP address matching, exists. These are evaluated as AND across different attribute names and OR across values for the same attribute.
>
> 4. **Filter evaluation cost** -- Evaluating a filter policy per subscription is cheap (microseconds -- it's just JSON pattern matching). The cost is linear in the number of subscriptions, but filtering prevents the much more expensive delivery I/O. Net savings are massive.
>
> 5. **Limit**: Up to 200 filter policies per topic, 10,000 per AWS account.
>
> 6. **Propagation delay**: Filter policy changes take up to 15 minutes to fully propagate due to eventual consistency in the distributed subscription metadata."

> *For the full deep dive on the fan-out engine, see [fan-out-engine.md](fan-out-engine.md).*

#### Architecture Update After Phase 5

> "Our fan-out layer has evolved:
>
> | | Before (Phase 4) | After (Phase 5) |
> |---|---|---|
> | **Fan-out** | Single fleet of workers | **Partitioned parallel fan-out: coordinator partitions subs into chunks, distributed workers deliver in parallel** |
> | **Filtering** | None | **Filter evaluation at fan-out coordinator, before delivery -- eliminates unmatched deliveries** |
> | **Delivery** | Same for all protocols | *(still uniform -- let's fix this next)* |

---

### L5 vs L6 vs L7 -- Phase 5 Rubric

| Aspect | L5 (SDE-2) | L6 (SDE-3) | L7 (Principal) |
|---|---|---|---|
| **Fan-out design** | "Use a message queue and workers" | Partitioned parallel fan-out with subscription chunking, async I/O per worker, crash recovery via re-queue | Discusses cell-based isolation, per-topic vs per-partition capacity planning, preemptive scaling for known large topics |
| **Filtering** | "Subscribers can filter messages" | Explains filter-at-SNS vs filter-at-subscriber tradeoff, filter policy evaluation in fan-out path, 200/topic limit | Discusses filter policy compilation/caching for hot topics, filter evaluation cost amortization, eventual consistency of filter propagation (15 min) |
| **Back-pressure** | "Rate limit publishers" | Explains Publish API quotas (30K/sec), per-subscriber delivery rate control, queue depth monitoring | Discusses adaptive throttling, prioritization of AWS-managed endpoints over customer HTTP, goodput vs throughput metrics |

---

## PHASE 6: Deep Dive -- Delivery Engine & Retry Policies (~10 min)

**Interviewer:**
The fan-out engine figures out WHAT to deliver and to WHOM. Now let's talk about the delivery engine -- HOW we actually deliver to each endpoint type, and what happens when delivery fails.

**Candidate:**

> "This is where SNS's heterogeneous endpoint support creates real complexity. Delivering to an SQS queue is fundamentally different from delivering to a customer's HTTP endpoint or sending an SMS. Each has different latency, reliability, and retry characteristics.
>
> Let me break this down by protocol."

#### Protocol-Specific Delivery

> "
> | Protocol | Mechanism | Typical Latency | Reliability | Notes |
> |---|---|---|---|---|
> | **SQS** | Internal AWS API call (SendMessage) | < 5ms | Very high -- SQS is designed to always accept | Most common subscriber. Effectively 'guaranteed' delivery. |
> | **Lambda** | Internal AWS invoke | < 10ms | High -- Lambda manages retries internally | SNS invokes asynchronously; Lambda's own retry and DLQ apply after that. |
> | **HTTP/S** | HTTP POST to customer endpoint | 50ms - 30sec | Highly variable -- depends on customer's infra | Most failure-prone. Custom delivery policies supported. |
> | **Email** | Via internal SMTP relay | Seconds to minutes | Medium -- SMTP can bounce, throttle, or silently drop | Rate-limited to 10 messages/sec per subscription. |
> | **SMS** | Via carrier gateways | Seconds | Low -- carrier-dependent, international routing | Rate-limited to 20 msg/sec. Costs vary by country. |
> | **Mobile Push** | Via APNs/FCM/ADM/WNS/MPNS/Baidu | Seconds | Medium -- depends on platform and device status | Token-based. Device tokens can become stale. |
> | **Firehose** | Internal AWS API call | < 10ms | Very high | Streaming delivery for analytics. |
>
> The key insight: **SQS and Lambda are 'easy' subscribers** -- they're AWS-managed, fast, reliable, and SNS talks to them via internal APIs. **HTTP, email, SMS, and mobile push are 'hard' subscribers** -- external, slow, unreliable, and require complex retry/backoff logic."

#### Retry Policies

> "SNS has fundamentally different retry behavior for AWS-managed vs customer-managed endpoints. This is a critical design decision.
>
> **AWS-managed endpoints (SQS, Lambda):**
>
> ```
> Phase 1: Immediate retry     → 3 retries, no delay
> Phase 2: Pre-backoff         → 2 retries, 1 second apart
> Phase 3: Backoff             → 10 retries, exponential 1-20 seconds
> Phase 4: Post-backoff        → 100,000 retries, 20 seconds apart
>
> Total: 100,015 attempts over ~23 days
> ```
>
> This is aggressive because SQS and Lambda are almost always available. A failure here usually means a transient issue (brief overload, network hiccup) that resolves quickly. The 23-day total window means SNS is incredibly patient with AWS-managed endpoints.
>
> **Customer-managed endpoints (HTTP/S, email, SMS, mobile push):**
>
> ```
> Phase 1: Immediate retry     → 0 retries
> Phase 2: Pre-backoff         → 2 retries, 10 seconds apart
> Phase 3: Backoff             → 10 retries, exponential 10-600 seconds
> Phase 4: Post-backoff        → 38 retries, 600 seconds apart
>
> Total: 50 attempts over ~6 hours
> ```
>
> This is much more conservative. Customer HTTP endpoints might be down for extended periods, and hammering them with retries would waste resources and potentially worsen their condition. The 6-hour window gives reasonable time for recovery without indefinite retry.
>
> **HTTP/S custom delivery policies:**
>
> HTTP/S is the only protocol that supports fully customizable delivery policies:
>
> ```json
> {
>     \"healthyRetryPolicy\": {
>         \"minDelayTarget\": 1,
>         \"maxDelayTarget\": 60,
>         \"numRetries\": 50,
>         \"numNoDelayRetries\": 3,
>         \"numMinDelayRetries\": 2,
>         \"numMaxDelayRetries\": 35,
>         \"backoffFunction\": \"exponential\"
>     },
>     \"throttlePolicy\": {
>         \"maxReceivesPerSecond\": 10
>     }
> }
> ```
>
> Four backoff functions available: **exponential** (fastest growth), **geometric**, **arithmetic**, **linear** (slowest growth). The `maxReceivesPerSecond` throttle prevents SNS from overwhelming the endpoint during retry storms.
>
> **Retryable vs non-retryable errors:**
> - HTTP 5xx and 429 (Too Many Requests) → retryable
> - HTTP 4xx (except 429) → non-retryable, considered permanent failure
> - This distinction is important -- retrying a 400 Bad Request forever would be pointless"

**Interviewer:**
What happens after all retries are exhausted?

**Candidate:**

> "This is where Dead Letter Queues (DLQs) come in.
>
> **Dead Letter Queues:**
>
> ```
>     Publisher
>       │
>       ▼
>   ┌─────────┐     ┌──────────────┐
>   │  Topic   │────►│ Subscription │──── deliver ──► HTTP endpoint (FAILS)
>   └─────────┘     │              │                      │
>                   │  Retry 1-50  │◄─────────────────────┘
>                   │  (6 hours)   │
>                   │              │
>                   │  All retries │
>                   │  exhausted   │
>                   │      │       │
>                   │      ▼       │
>                   │  Send to DLQ │
>                   └──────┬───────┘
>                          │
>                          ▼
>                   ┌──────────────┐
>                   │  SQS Queue   │  ← Dead Letter Queue
>                   │  (DLQ)       │
>                   │              │
>                   │  Message +   │
>                   │  metadata:   │
>                   │  - topic ARN │
>                   │  - sub ARN   │
>                   │  - error     │
>                   │  - timestamp │
>                   └──────────────┘
>                          │
>                     Process / Alert
> ```
>
> **Key design decisions for DLQs:**
>
> 1. **DLQs are attached to subscriptions, not topics** -- This is critical. Each subscription can have its own DLQ. If Sub A fails, its messages go to Sub A's DLQ. Sub B and Sub C are unaffected. This lets you isolate and debug failures per subscriber.
>
> 2. **DLQ is an SQS queue** -- Using SQS as the DLQ backend is smart because SQS already provides durable, scalable message storage with up to 14 days retention.
>
> 3. **FIFO topics use FIFO DLQs; standard topics use standard DLQs** -- The queue type must match the topic type.
>
> 4. **DLQ and subscription must be in the same account and region** -- Cross-account DLQs are not supported.
>
> 5. **If no DLQ is configured, the message is lost** -- This is a major operational concern. Without a DLQ, there's no trace of failed deliveries except CloudWatch metrics. I'd recommend always configuring DLQs for critical subscriptions.
>
> 6. **Client-side errors vs server-side errors:**
>    - Client-side errors (deleted endpoint, policy denial): No retries. Sent directly to DLQ.
>    - Server-side errors (endpoint unavailable, timeout): Full retry policy. DLQ only after exhaustion.
>
> **Monitoring DLQs:**
> - CloudWatch alarm on `ApproximateNumberOfMessagesVisible` metric on the DLQ
> - Set threshold to 1 -- any message in the DLQ means something is wrong
> - Use the SQS message metadata (topic ARN, subscription ARN, error) to diagnose"

**Interviewer:**
How does the delivery engine handle the case where an SQS subscriber is in a different region than the topic?

**Candidate:**

> "Cross-region delivery adds latency but is architecturally straightforward:
>
> 1. SNS stores the SQS queue ARN in the subscription, which includes the region
> 2. The delivery worker makes a `SendMessage` call to the SQS queue's regional endpoint
> 3. This goes over AWS's internal backbone network, not the public internet -- so latency is 10-50ms instead of 100ms+
> 4. If the cross-region call fails, the same retry policy applies
>
> The operational concern is that cross-region failures are more likely than same-region (network partitions between regions). If you need low-latency, reliable fan-out, keep the topic and SQS queues in the same region. For multi-region architectures, consider one SNS topic per region with cross-region event replication at the application level."

> *For the full deep dive on delivery and retry policies, see [delivery-and-retries.md](delivery-and-retries.md).*

#### Architecture Update After Phase 6

> "Our delivery layer has evolved:
>
> | | Before (Phase 5) | After (Phase 6) |
> |---|---|---|
> | **Fan-out** | Partitioned parallel fan-out with filtering | *(unchanged)* |
> | **Delivery** | Same for all protocols | **Protocol-specific delivery: AWS-managed (100K retries/23 days) vs customer-managed (50 retries/6 hours). Custom delivery policies for HTTP/S.** |
> | **Failure handling** | None | **Dead Letter Queues per subscription. Client errors → direct to DLQ. Server errors → full retry then DLQ.** |

---

### L5 vs L6 vs L7 -- Phase 6 Rubric

| Aspect | L5 (SDE-2) | L6 (SDE-3) | L7 (Principal) |
|---|---|---|---|
| **Retry policies** | "Retry with exponential backoff" | Knows the two-tier retry model (100K/23 days for AWS-managed, 50/6 hours for customer-managed), explains why they differ | Discusses retry budget management across the fleet, per-subscriber circuit breakers, retry storm mitigation |
| **DLQ design** | "Use a dead letter queue" | DLQ per subscription (not topic), client vs server error distinction, FIFO DLQ for FIFO topics | Discusses DLQ message enrichment (original topic ARN, error codes), automated redrive workflows, DLQ monitoring as SLI |
| **Protocol heterogeneity** | Lists endpoint types | Compares delivery characteristics (latency, reliability) per protocol, custom delivery policies for HTTP | Discusses delivery fleet segmentation by protocol, capacity planning for SMS carrier gateways, APNs token lifecycle management |

---

## PHASE 7: Deep Dive -- SNS + SQS Fan-Out Pattern (~8 min)

**Interviewer:**
The SNS + SQS fan-out pattern is probably the most important architecture pattern in AWS messaging. Walk me through it.

**Candidate:**

> "Absolutely. This is the pattern that makes SNS + SQS the backbone of event-driven architectures on AWS. Let me build it up from first principles.
>
> **The problem:** A microservice publishes an event (say, 'order placed'). Multiple downstream services need to process it, each in their own way and at their own pace:
> - Service A: update inventory (fast)
> - Service B: send confirmation email (slow)
> - Service C: update analytics (batch-oriented)
> - Service D: notify fraud detection (real-time)
>
> **Without SNS+SQS -- the coupling problem:**
>
> ```
> Order Service
>   │
>   ├── SendMessage(inventory-queue)    ← must know about every consumer
>   ├── SendMessage(email-queue)        ← adding a new consumer requires
>   ├── SendMessage(analytics-queue)       code change in Order Service
>   └── SendMessage(fraud-queue)
> ```
>
> The Order Service is coupled to every downstream service. Adding Service E requires modifying the Order Service. This violates the Open-Closed Principle.
>
> **With SNS+SQS fan-out:**
>
> ```
>                 Order Service
>                      │
>                      │ Publish('order-placed', {order_id: 123, amount: 99.99})
>                      ▼
>              ┌───────────────┐
>              │  SNS Topic:   │
>              │  order-events │
>              └───────┬───────┘
>                      │ Fan-out (parallel, filtered)
>          ┌───────────┼────────────┬────────────┐
>          │           │            │            │
>          ▼           ▼            ▼            ▼
>    ┌──────────┐ ┌──────────┐ ┌──────────┐ ┌──────────┐
>    │ SQS:     │ │ SQS:     │ │ SQS:     │ │ SQS:     │
>    │ inventory│ │ email    │ │ analytics│ │ fraud    │
>    │ queue    │ │ queue    │ │ queue    │ │ queue    │
>    └────┬─────┘ └────┬─────┘ └────┬─────┘ └────┬─────┘
>         │            │            │            │
>         ▼            ▼            ▼            ▼
>    Inventory     Email         Analytics    Fraud
>    Service       Service       Service      Service
>    (polls fast)  (polls slow)  (batch poll) (polls fast)
> ```
>
> **Why this pattern is so powerful:**
>
> 1. **Decoupling**: Order Service publishes once to a topic. It doesn't know or care about the subscribers. Adding Service E means subscribing a new queue to the topic -- zero changes to Order Service.
>
> 2. **Independent consumption rates**: Each SQS queue buffers messages independently. The analytics service can batch-process every 5 minutes while the fraud service processes in real-time. Slow consumers don't block fast ones because SQS absorbs the backlog.
>
> 3. **Independent failure isolation**: If the email service goes down, its SQS queue buffers messages (up to 14 days retention). When it recovers, it processes the backlog. Other services are completely unaffected.
>
> 4. **Filtering reduces load**: Using SNS filter policies, each SQS queue receives only the messages it cares about:
>
> ```
> Inventory queue filter:   {event_type: ['order_placed', 'order_cancelled']}
> Email queue filter:       {event_type: ['order_placed', 'order_shipped']}
> Analytics queue filter:   (no filter -- receives everything)
> Fraud queue filter:       {amount: [{numeric: ['>', 500]}]}
> ```
>
> The fraud service only gets high-value orders. The inventory service only gets order lifecycle events. SNS does the filtering -- the queues and consumers don't have to.
>
> 5. **Retry semantics per consumer**: Each SQS queue has its own visibility timeout, dead letter queue, and retry settings. The email service can retry 3 times; the analytics service can retry 10 times. Independent per consumer."

**Interviewer:**
Why not just use SQS directly? Why do you need SNS in the middle?

**Candidate:**

> "This is the key question. Let me compare:
>
> | | SQS Only | SNS + SQS |
> |---|---|---|
> | **Fan-out** | Publisher must send to each queue individually (N calls) | Publisher sends once, SNS fans out (1 call) |
> | **Coupling** | Publisher knows about every queue | Publisher knows about one topic |
> | **Adding consumers** | Modify publisher code | Subscribe new queue to topic (no publisher change) |
> | **Filtering** | Publisher must decide what to send to each queue | SNS filters based on subscription policies |
> | **Mixed subscribers** | Can only send to SQS queues | Can fan out to SQS + Lambda + HTTP + email simultaneously |
> | **Failure** | Publisher must handle per-queue failures | SNS handles delivery and retries independently |
>
> **SQS alone is fine** when you have a single consumer per event. It's the 'point-to-point' pattern.
>
> **SNS + SQS is needed** when you have **fan-out** -- one event, multiple consumers. The SNS topic is the multiplier.
>
> **The cost:** Adding SNS adds ~$0.50 per million messages published + $0.00 for SQS delivery (first 1M/month free, then $0.50 per million). For most workloads, the decoupling benefit far outweighs the cost."

**Interviewer:**
What about SNS + SQS vs EventBridge?

**Candidate:**

> "EventBridge is a newer service that overlaps significantly with SNS + SQS. Here's how I think about the choice:
>
> | | SNS + SQS | EventBridge |
> |---|---|---|
> | **Filtering** | Attribute-based (string, numeric, prefix/suffix, anything-but) | Content-based (JSON path matching, more expressive) |
> | **Throughput** | Very high (30K+ publishes/sec per topic) | Lower (custom bus soft limit varies by region, typically lower than SNS) |
> | **Subscriber types** | SQS, Lambda, HTTP, email, SMS, push | 20+ target types including Step Functions, API Gateway, Kinesis |
> | **Schema** | No schema enforcement | Schema registry with discovery and validation |
> | **Replay** | Only with FIFO topics (message archiving) | Event replay from archive |
> | **Cost** | $0.50/million publishes | $1.00/million events |
> | **Best for** | High-throughput fan-out, simple filtering | Complex routing, schema validation, cross-account event buses |
>
> **My rule of thumb:**
> - Need raw throughput and simple fan-out to SQS → **SNS + SQS**
> - Need content-based routing with complex JSON matching → **EventBridge**
> - Need first-class schema registry and cross-account event buses → **EventBridge**
> - Need SMS/email/mobile push → **SNS** (EventBridge doesn't do app-to-person)"

> *For the full deep dive on the SNS + SQS fan-out pattern, see [sns-sqs-fanout.md](sns-sqs-fanout.md).*

---

### L5 vs L6 vs L7 -- Phase 7 Rubric

| Aspect | L5 (SDE-2) | L6 (SDE-3) | L7 (Principal) |
|---|---|---|---|
| **Fan-out pattern** | "Use SNS topic with SQS subscribers" | Explains decoupling, independent consumption rates, failure isolation, and filtering per consumer | Discusses cost modeling at scale, per-queue DLQ monitoring as SLI, pattern evolution as team grows |
| **SNS vs SQS** | Knows they're different | Compares fan-out (SNS) vs point-to-point (SQS), explains why SNS+SQS not SQS-only | Discusses when to consolidate topics vs split them, topic design as domain boundary |
| **EventBridge comparison** | Doesn't mention | Compares throughput, filtering expressiveness, and cost | Discusses service evolution (SNS → EventBridge migration strategy), schema governance at org scale |

---

## PHASE 8: Deep Dive -- FIFO Topics (~8 min)

**Interviewer:**
Let's talk about FIFO topics. Standard topics are the workhorse, but FIFO topics solve a different class of problems. Walk me through the design.

**Candidate:**

> "FIFO topics address the limitation of standard topics: **no ordering guarantee and potential duplicates**. Some use cases absolutely require strict ordering:
>
> - Financial transactions: debits and credits must be applied in order
> - Inventory updates: add 10 then remove 5 is different from remove 5 then add 10
> - State machine transitions: events must arrive in the sequence they occurred
>
> **Standard vs FIFO comparison:**
>
> | | Standard Topics | FIFO Topics |
> |---|---|---|
> | **Ordering** | Best-effort | Strict ordering per message group |
> | **Delivery** | At-least-once (duplicates possible) | Exactly-once processing (within dedup window) |
> | **Throughput** | Up to 30,000 msg/sec per topic (US East) | Up to 30,000 msg/sec per topic with high throughput mode; 300 msg/sec per message group |
> | **Subscribers** | SQS, Lambda, HTTP, email, SMS, push, Firehose | SQS FIFO queues and SQS standard queues only |
> | **Max subscriptions** | 12,500,000 per topic | 100 per topic |
> | **Filtering** | Full filter policy support | Supported, but filtering with FIFO gives at-most-once (not exactly-once) |
> | **Topic name** | Any name | Must end with `.fifo` suffix |

#### Message Group IDs -- The Ordering Unit

> "FIFO topics don't order ALL messages globally -- that would be a bottleneck. Instead, ordering is per **message group**:
>
> ```
> Publisher sends:
>   Publish(topic.fifo, msg='Debit $100', MessageGroupId='account-123')
>   Publish(topic.fifo, msg='Credit $50',  MessageGroupId='account-123')
>   Publish(topic.fifo, msg='Debit $200', MessageGroupId='account-456')
>
> Subscriber receives (in order within each group):
>   account-123: Debit $100 → Credit $50    (guaranteed order)
>   account-456: Debit $200                  (independent group)
>
> No ordering guarantee BETWEEN groups:
>   account-456's Debit $200 might arrive before account-123's messages
> ```
>
> **Why this design:**
> - Global ordering would serialize all messages through a single partition -- can't scale beyond ~1000 msg/sec
> - Per-group ordering parallelizes: messages for different groups can be processed by different partitions simultaneously
> - The message group ID is typically a business entity ID: account ID, order ID, customer ID
>
> **Throughput scope:**
> - `FifoThroughputScope: MessageGroup` -- throughput limits apply per message group (300 msg/sec per group, but many groups can be processed in parallel, up to 30,000 msg/sec per topic)
> - `FifoThroughputScope: Topic` -- throughput limits apply to the entire topic (limits total throughput)"

#### Deduplication -- Exactly-Once Processing

> "FIFO topics provide exactly-once message processing (under specific conditions) using deduplication:
>
> **How deduplication works:**
>
> ```
> Publisher publishes:
>   Publish(topic.fifo, msg='Debit $100',
>           MessageGroupId='account-123',
>           MessageDeduplicationId='txn-abc-001')
>
> Network hiccup -- publisher doesn't get 200 OK
>
> Publisher retries:
>   Publish(topic.fifo, msg='Debit $100',
>           MessageGroupId='account-123',
>           MessageDeduplicationId='txn-abc-001')   ← SAME dedup ID
>
> SNS: "I've seen dedup ID 'txn-abc-001' within the 5-minute window.
>       Accept the publish (return 200), but DON'T deliver again."
> ```
>
> **Deduplication window: 5 minutes.** If a message with the same deduplication ID is published within 5 minutes, it's accepted but not re-delivered.
>
> **Content-based deduplication:** If enabled (`ContentBasedDeduplication: true`), SNS automatically generates a deduplication ID by hashing the message body. The publisher doesn't need to provide an explicit dedup ID.
>
> **Important caveat:** Message attributes are NOT included in the content hash. So two messages with the same body but different attributes are considered duplicates when content-based dedup is enabled.
>
> **Conditions for exactly-once delivery:**
> 1. SQS FIFO queue subscriber with proper permissions
> 2. Consumer processes and deletes messages before visibility timeout expires
> 3. No message filtering enabled (filtering changes the guarantee to at-most-once)
> 4. No network disruptions preventing message acknowledgment"

#### FIFO Architecture -- How It Works Internally

> "[INFERRED -- not officially documented] Here's how I'd design the FIFO ordering mechanism:
>
> ```
>     Publisher
>       │
>       │ Publish(topic.fifo, msg, group='account-123', dedup='txn-001')
>       ▼
>   ┌─────────────────────┐
>   │   FIFO Publish API  │
>   │                     │
>   │  1. Dedup check:    │
>   │     Has 'txn-001'   │
>   │     been seen in    │
>   │     last 5 min?     │
>   │     → No, accept    │
>   │                     │
>   │  2. Assign sequence │
>   │     number within   │
>   │     group           │
>   │     'account-123'   │
>   │     → seq #47       │
>   │                     │
>   │  3. Store message   │
>   │     with (group,    │
>   │     seq) ordering   │
>   └─────────┬───────────┘
>             │
>             ▼
>   ┌─────────────────────┐
>   │  FIFO Fan-Out       │
>   │                     │
>   │  Deliver to SQS     │
>   │  FIFO queues in     │
>   │  sequence order     │
>   │  within each group  │
>   │                     │
>   │  group 'account-123'│
>   │  → seq 45, 46, 47   │
>   │  (in order)         │
>   └─────────────────────┘
> ```
>
> **The dedup store** must be fast (sub-millisecond lookup) and durable. [INFERRED] It's likely an in-memory hash table replicated across AZs, partitioned by message group ID. The 5-minute TTL keeps it bounded.
>
> **The ordering mechanism** requires a sequence number generator per message group. [INFERRED] This is likely a per-partition counter, where each partition handles a subset of message groups. Within a partition, messages are totally ordered by sequence number."

**Interviewer:**
Why is the subscription limit only 100 per FIFO topic, compared to 12.5 million for standard?

**Candidate:**

> "Because FIFO delivery is inherently more expensive:
>
> 1. **Ordering requires sequential delivery** -- Within a message group, the next message can only be delivered after the previous one is confirmed. This means SNS can't do fully parallel fan-out to all subscribers the way standard topics do.
>
> 2. **Deduplication state** -- Each subscriber needs its own dedup tracking. With 12.5M subscribers and millions of dedup IDs per 5-minute window, the state would be enormous.
>
> 3. **Exactly-once delivery** -- The delivery-confirmation-advance cycle is more expensive per subscriber than fire-and-forget at-least-once delivery.
>
> 4. **SQS FIFO queue throughput** -- SQS FIFO queues themselves have throughput limits. More subscribers means more FIFO queues to deliver to sequentially.
>
> The 100 subscription limit keeps FIFO topics tractable. For high fan-out use cases, standard topics (with idempotent subscribers) are the right choice."

> *For the full deep dive on FIFO topics, see [fifo-topics.md](fifo-topics.md).*

#### Architecture Update After Phase 8

> "Our architecture now supports two topic types:
>
> | | Standard Topics | FIFO Topics |
> |---|---|---|
> | **Fan-out** | Partitioned parallel (Phase 5) | Sequential per message group, parallel across groups |
> | **Delivery** | At-least-once, protocol-specific retries (Phase 6) | Exactly-once within dedup window, SQS-only subscribers |
> | **Filtering** | Full support | Supported but changes guarantee to at-most-once |
> | **Scale** | 12.5M subs/topic, 30K publishes/sec | 100 subs/topic, 300 msg/sec per group, 30K msg/sec per topic |

---

### L5 vs L6 vs L7 -- Phase 8 Rubric

| Aspect | L5 (SDE-2) | L6 (SDE-3) | L7 (Principal) |
|---|---|---|---|
| **FIFO design** | "Use FIFO for ordering" | Explains message group IDs as ordering unit, 5-min dedup window, content-based dedup, at-most-once with filtering | Discusses per-partition sequencing, dedup store design (in-memory + replicated), why 100 sub limit exists |
| **Standard vs FIFO** | Knows both exist | Compares throughput, subscription limits, delivery guarantees, subscriber types with specific numbers | Discusses when to use FIFO vs application-level idempotency + standard topics; cost analysis |
| **Exactly-once** | "FIFO gives exactly-once" | Knows the four conditions for exactly-once, explains why filtering breaks the guarantee | Discusses impossibility of exactly-once across heterogeneous endpoints, distributed systems theory (Two Generals) |

---

## PHASE 9: Deep Dive -- Message Durability & Multi-AZ (~5 min)

**Interviewer:**
You mentioned earlier that messages are stored across multiple AZs before returning success. Let's dive into that. How does SNS ensure a published message isn't lost?

**Candidate:**

> "Message durability in SNS is conceptually simpler than S3 (where data is stored forever), but still critical. The requirement: **a message that gets a successful Publish response must be delivered to every subscriber, even if infrastructure fails during fan-out.**
>
> **The durability model:**
>
> ```
> Publisher → Publish(topic, message)
>     │
>     ▼
> ┌────────────────────────────────────────────┐
> │              Publish Path                   │
> │                                             │
> │  1. Accept message                          │
> │  2. Replicate to multiple AZs              │
> │     (synchronous write to ≥2 AZs)          │
> │  3. Return 200 OK with MessageId            │
> │                                             │
> │  Message is now DURABLE.                   │
> │  Even if the accepting server crashes,     │
> │  another AZ has the message.               │
> └──────────────────┬─────────────────────────┘
>                    │
>                    ▼ (asynchronous)
> ┌────────────────────────────────────────────┐
> │              Fan-Out Path                   │
> │                                             │
> │  1. Read message from durable store        │
> │  2. Look up subscriptions                  │
> │  3. Deliver to each subscriber             │
> │  4. Mark delivery complete per subscriber  │
> │  5. After all subs delivered (or DLQ'd),   │
> │     delete message from store              │
> └────────────────────────────────────────────┘
> ```
>
> **Key insight: the message store is a write-ahead log, not long-term storage.** Messages are stored just long enough to ensure complete fan-out. Once every subscriber has either received the message or had it sent to their DLQ, the message is deleted.
>
> **How multi-AZ replication works:**
>
> [INFERRED -- not officially documented] The message store uses synchronous replication to at least 2 of 3 AZs before acknowledging the publish. This is similar to how EBS, SQS, and other AWS services achieve durability:
>
> ```
>     Publisher → API Server (AZ-a)
>                   │
>                   ├── Write to local store (AZ-a) ✓
>                   ├── Replicate to store (AZ-b)    ✓  (synchronous)
>                   │   (2 AZs confirmed — quorum met)
>                   ├── Return 200 OK to publisher
>                   │
>                   └── Replicate to store (AZ-c)    ✓  (async, for extra durability)
> ```
>
> **Failure scenarios:**
>
> | Failure | What happens |
> |---|---|
> | API server crashes after store, before response | Publisher retries; message is in store; dedup prevents double-delivery (for FIFO) or subscriber handles duplicate (for standard) |
> | AZ-a goes down after Publish returns | Message is in AZ-b; fan-out continues from AZ-b |
> | Fan-out worker crashes mid-delivery | Message stays in store; another worker picks it up and resumes delivery to remaining subscribers |
> | All AZs lose the message | Shouldn't happen with quorum writes to 2/3 AZs. If it does, the message is lost. This is the scenario SNS guards against with multi-AZ replication. |
>
> **Message lifetime:**
> - A message exists in the store from the moment of publish until all deliveries complete (or are DLQ'd)
> - For fast subscribers (SQS), this might be milliseconds
> - For slow subscribers with retries (HTTP endpoint down for 6 hours), the message persists for up to 23 days (for AWS-managed endpoints)
> - The message store must handle a wide range of message lifetimes"

**Interviewer:**
Good. How does this differ from S3's durability model?

**Candidate:**

> "Fundamentally different in purpose and design:
>
> | | SNS | S3 |
> |---|---|---|
> | **Purpose** | Transient: store message until fan-out completes | Permanent: store object indefinitely |
> | **Lifetime** | Milliseconds to 23 days | Years to forever |
> | **Durability target** | 'Don't lose a message in transit' | 11 9's (99.999999999%) |
> | **Storage volume** | Small (messages are 256 KB max, transient) | Exabytes of permanent data |
> | **Redundancy scheme** | Quorum replication (2 of 3 AZs) | Erasure coding (8+3 Reed-Solomon) |
>
> SNS doesn't need erasure coding because messages are small and transient. Simple replication across AZs is sufficient and much simpler."

---

### L5 vs L6 vs L7 -- Phase 9 Rubric

| Aspect | L5 (SDE-2) | L6 (SDE-3) | L7 (Principal) |
|---|---|---|---|
| **Durability model** | "Replicate messages" | Explains store-then-fan-out, quorum writes to 2/3 AZs, message lifetime tied to delivery completion | Discusses write-ahead log compaction, storage sizing for worst-case (23 days of undeliverable messages), interaction with DLQ |
| **Failure scenarios** | Mentions "what if a server crashes" | Tables out specific failures (API crash, AZ down, worker crash) with recovery behavior | Discusses correlated AZ failures, grey failures (slow AZ), blast radius of message store outage |
| **SNS vs S3 comparison** | Doesn't compare | Explains transient vs permanent storage, replication vs erasure coding | Discusses cost optimization of transient storage, when to use SNS message archiving vs direct S3 storage |

---

## PHASE 10: Mobile Push, SMS & App-to-Person Delivery (~4 min)

**Interviewer:**
Let's briefly cover app-to-person delivery -- mobile push, SMS, email. How is this architecturally different?

**Candidate:**

> "App-to-person delivery uses the same fan-out engine but delivers through fundamentally different backends:
>
> #### Mobile Push Notifications
>
> ```
>     Publisher
>       │
>       │ Publish(topic, msg)
>       ▼
>   ┌─────────┐
>   │  Topic   │
>   └────┬────┘
>        │
>        ▼
>   ┌─────────────────────────────────┐
>   │  SNS Delivery Worker            │
>   │                                 │
>   │  Subscription has a             │
>   │  Platform Endpoint ARN          │
>   │       │                         │
>   │       ▼                         │
>   │  ┌──────────────────────────┐   │
>   │  │ Platform Application     │   │
>   │  │ (stores push credentials)│   │
>   │  │                          │   │
>   │  │  APNs → Apple cert/token │   │
>   │  │  FCM  → Server key       │   │
>   │  │  ADM  → Client ID/Secret │   │
>   │  │  WNS  → Package SID      │   │
>   │  │  MPNS → Certificate      │   │
>   │  │  Baidu → API Key         │   │
>   │  └──────────┬───────────────┘   │
>   │             │                   │
>   │             ▼                   │
>   │  Push to platform service       │
>   │  (APNs/FCM/ADM/WNS/MPNS/Baidu)│
>   └─────────────────────────────────┘
>        │
>        ▼
>   Mobile Device
> ```
>
> **The SNS abstraction for mobile push:**
>
> 1. **Platform Application**: You register your app's push credentials (APNs certificate, FCM server key) with SNS. This creates a Platform Application ARN.
>
> 2. **Platform Endpoint**: When a mobile device registers for push, you get a device token from the platform (APNs/FCM). You register this token with SNS, creating a Platform Endpoint ARN.
>
> 3. **Publish**: You can publish directly to an endpoint ARN (one device) or subscribe endpoint ARNs to a topic for fan-out to many devices.
>
> **Supported platforms:** APNs (iOS/macOS), FCM (Android/cross-platform), ADM (Amazon devices), WNS (Windows), MPNS (Windows Phone), Baidu (Chinese devices).
>
> **Operational challenges with mobile push:**
> - **Stale device tokens**: When a user uninstalls the app, the token becomes invalid. APNs/FCM return errors for stale tokens. SNS disables the endpoint, but there's a lag.
> - **Platform rate limits**: APNs and FCM have their own rate limits. SNS must respect these to avoid being throttled or blocked.
> - **Payload format differences**: Each platform has a different JSON structure for push notifications. SNS supports a multi-platform message structure that formats differently per platform."

> "#### SMS and Email
>
> - **SMS**: Delivered via carrier gateways. Rate-limited to 20 messages/sec. Costs vary dramatically by country ($0.00645/msg in US, $0.09+/msg for some international destinations). SMS is unreliable by nature -- carrier-dependent, no delivery guarantee.
>
> - **Email**: Delivered via SMTP. Rate-limited to 10 messages/sec per subscription. Email confirmations use double opt-in (subscriber must confirm via email link).
>
> These are important for completeness but architecturally simpler -- SNS just translates the message format and hands off to the appropriate gateway (carrier for SMS, SMTP for email). The fan-out engine and retry logic are the same."

---

## PHASE 11: Wrap-Up & Summary (~3 min)

**Interviewer:**
We're running low on time. Summarize your design and tell me what keeps you up at night operating this system.

**Candidate:**

> "**Design evolution -- how we got here:**
>
> | Component | Started With | Evolved To | Why |
> |---|---|---|---|
> | **Architecture** | Direct point-to-point notifications | 3-layer: API → durable message store → async fan-out engine | Decouple acceptance from delivery; publisher doesn't block on delivery |
> | **Fan-out** | Single server delivering sequentially | Partitioned parallel fan-out: coordinator chunks subscription list, distributed workers deliver in parallel | Single server can't handle 12.5M subs or 100M deliveries/sec |
> | **Filtering** | Deliver everything to everyone | Filter policy evaluation at fan-out time, before delivery | Eliminates wasteful deliveries; SNS filters, not subscribers |
> | **Delivery** | Same retry for all protocols | Protocol-specific: 100K retries/23 days (AWS-managed) vs 50 retries/6 hours (customer-managed) | HTTP endpoints and SQS queues have fundamentally different reliability profiles |
> | **Failure handling** | Message lost on failure | DLQ per subscription + CloudWatch monitoring | No message silently dropped; failed deliveries are traceable |
> | **Ordering** | Best-effort only | FIFO topics with per-group ordering, 5-min dedup window, exactly-once processing | Financial/state-machine use cases require strict ordering |
> | **Durability** | In-memory only | Quorum writes to 2/3 AZs before 200 OK | A successful Publish must guarantee delivery even during AZ failure |
>
> **Final Architecture:**
>
> ```
>                           ┌───────────────────────┐
>                           │      Publishers        │
>                           │  (SDKs, CLI, Console)  │
>                           └──────────┬────────────┘
>                                      │ HTTPS (Publish API)
>                           ┌──────────▼────────────┐
>                           │    API Layer           │
>                           │  (Stateless fleet)     │
>                           │  Auth (SigV4)          │
>                           │  Rate limiting         │
>                           │  Validation            │
>                           └──────────┬────────────┘
>                                      │
>                    ┌─────────────────┼─────────────────┐
>                    │                 │                 │
>         ┌──────────▼────────┐ ┌─────▼──────┐ ┌──────▼──────────┐
>         │  Metadata Store   │ │  Message   │ │   Fan-Out       │
>         │                   │ │  Store     │ │   Engine        │
>         │  Topics           │ │            │ │                 │
>         │  Subscriptions    │ │  Multi-AZ  │ │  Coordinator:   │
>         │  Filter Policies  │ │  durable   │ │  partition subs │
>         │  Delivery Policies│ │  write-    │ │  enqueue tasks  │
>         │  DLQ configs      │ │  ahead log │ │                 │
>         │                   │ │            │ │  Workers:       │
>         │                   │ │  Message   │ │  parallel       │
>         │                   │ │  persists  │ │  delivery per   │
>         │                   │ │  until     │ │  protocol       │
>         │                   │ │  fan-out   │ │                 │
>         │                   │ │  complete  │ │  Filter eval    │
>         └───────────────────┘ └────────────┘ │  before deliver │
>                                              └────────┬────────┘
>                                                       │
>                          ┌──────────┬─────────┬───────┼────────┬──────────┐
>                          │          │         │       │        │          │
>                          ▼          ▼         ▼       ▼        ▼          ▼
>                        SQS     Lambda     HTTP/S   Email     SMS     Mobile
>                       queues   functions  endpts            carriers  Push
>                                                                    (APNs/FCM)
>                          │          │         │       │        │          │
>                          ▼          ▼         ▼       ▼        ▼          ▼
>                       ┌─────┐   ┌─────┐   ┌─────┐                   ┌─────┐
>                       │ DLQ │   │ DLQ │   │ DLQ │   ...             │ DLQ │
>                       └─────┘   └─────┘   └─────┘                   └─────┘
> ```
>
> **What keeps me up at night:**
>
> 1. **Fan-out amplification attacks** -- A malicious or misconfigured publisher can publish to a topic with millions of subscribers, creating an amplification effect that consumes enormous delivery resources. SNS must have per-account and per-topic rate limits, and the delivery fleet must be isolated so one topic's fan-out doesn't starve others. [INFERRED] Cell-based architecture with per-cell capacity limits is the likely mitigation.
>
> 2. **Slow subscriber cascading** -- If a popular HTTP endpoint becomes slow (responding in 25 seconds instead of 100ms), the delivery workers servicing that subscriber tie up connections and threads. If enough delivery workers are blocked on slow subscribers, the entire delivery fleet degrades. Solution: per-subscriber circuit breakers and connection timeouts. The `maxReceivesPerSecond` throttle in delivery policies helps, but only for subscribers that have configured it.
>
> 3. **DLQ monitoring gap** -- If a subscriber doesn't configure a DLQ, failed messages are silently lost after retries are exhausted. The subscriber may not even know they're missing messages. This is a major operational risk. CloudWatch metrics (`NumberOfNotificationsFailed`) can alert, but many customers don't monitor them. I'd advocate for DLQ-by-default with an opt-out for non-critical subscriptions.
>
> 4. **Filter policy propagation delay** -- Filter policy changes take up to 15 minutes to propagate. During this window, a subscriber might receive messages it should filter out (or miss messages it should receive). For customers changing filter policies during a deployment, this 15-minute window is a source of bugs. This needs to be prominently documented and ideally reduced.
>
> 5. **FIFO topic dedup store sizing** -- The 5-minute dedup window means the dedup store must hold all dedup IDs from the last 5 minutes. At 30,000 messages/sec, that's 9 million dedup IDs per topic. [INFERRED] If the dedup store is in-memory, a burst in dedup IDs could cause memory pressure. Partitioning the dedup store by message group helps, but hot groups could still be problematic.
>
> 6. **Mobile push token rot** -- Device tokens for mobile push become stale when users uninstall apps. If you have a topic with 10 million mobile subscribers and 30% are stale, SNS is wasting delivery attempts on 3 million dead endpoints per message. APNs provides feedback on stale tokens, but there's always a lag. Regular endpoint hygiene (probing and disabling stale endpoints) is essential.
>
> **Potential extensions:**
> - **Message archiving and replay** -- FIFO topics support archiving messages and replaying them to subscribers. This enables event sourcing patterns.
> - **Firehose delivery streams** -- Direct delivery to Kinesis Firehose for analytics pipelines without intermediate SQS.
> - **Cross-account subscriptions** -- Topic in Account A, SQS queue in Account B. Requires topic policy allowing cross-account subscribe.
> - **SNS + EventBridge integration** -- Using SNS for high-throughput fan-out and EventBridge for complex routing, in the same event pipeline."

---

## Interviewer's Final Assessment

**Hire Recommendation: Strong Hire (L6 -- solid Senior SDE)**

| Dimension | Rating | Notes |
|---|---|---|
| **Problem Decomposition** | Exceeds Bar | Clean separation of acceptance path from delivery path from the start. Separated fan-out coordination from delivery workers. |
| **Requirements & Scoping** | Exceeds Bar | Drove scoping proactively. Quantified all key limits from AWS docs (12.5M subs, 256 KB, 30K/sec). Explained why not exactly-once for standard. |
| **Scale Estimation** | Meets Bar | Good estimates: 100M deliveries/sec, 200 GB/sec outbound. Identified fan-out amplification as the key scaling challenge. |
| **Fan-Out Engine** | Exceeds Bar | Iterative build-up from serial to thread pool to partitioned parallel. Subscription chunking, async I/O, stateless workers, crash recovery. |
| **Message Filtering** | Exceeds Bar | Filter-at-SNS-not-subscriber insight. Filter operators. 15-minute propagation delay. Cost savings analysis. |
| **Delivery & Retries** | Exceeds Bar | Two-tier retry model with exact numbers. Custom HTTP delivery policies. Retryable vs non-retryable errors. DLQ design per subscription. |
| **SNS + SQS Pattern** | Exceeds Bar | Built from first principles. Independent consumption rates, failure isolation, filtering. Compared with SQS-only and EventBridge. |
| **FIFO Topics** | Exceeds Bar | Message group IDs as ordering unit. 5-min dedup window. Content-based dedup. Why 100 sub limit. Filtering + FIFO = at-most-once. |
| **Durability** | Meets Bar | Store-then-fan-out. Quorum writes to 2/3 AZs. Message lifetime tied to delivery. Good comparison with S3. |
| **Operational Maturity** | Exceeds Bar | "What keeps you up at night" was outstanding -- amplification attacks, slow subscriber cascading, DLQ gap, filter propagation delay, dedup store sizing. |
| **Communication** | Exceeds Bar | Structured, used diagrams and tables, iterative progression. Proactively identified tradeoffs before asked. |
| **LP: Dive Deep** | Exceeds Bar | Went deep on retry numbers, FIFO dedup mechanics, filter evaluation path, all without prompting. |
| **LP: Invent and Simplify** | Exceeds Bar | Good pattern recognition (SNS+SQS vs EventBridge, standard vs FIFO). Didn't over-engineer. |
| **LP: Think Big** | Meets Bar | Extensions section showed awareness of broader ecosystem. |

**What would push this to L7:**
- Proposing the cell-based architecture in detail -- how topics map to cells, blast radius quantification
- Deeper discussion of delivery fleet capacity planning: how many delivery workers for 100M deliveries/sec, fleet sizing by protocol
- Discussing operational runbooks: what happens when an entire delivery fleet falls behind? How to drain and rebalance?
- Cost modeling: cost per delivery by protocol, how fan-out amplification drives the cost structure, how filtering saves cost at scale
- Proposing observability architecture: what dashboards exist, what alarms fire, how to trace a single message through the system
- Discussing the evolution from SNS to EventBridge: when should new services use which, and how to migrate

---

## Key Differences: L5 vs L6 vs L7 Expectations for This Problem

| Aspect | L5 (SDE-2) | L6 (SDE-3) | L7 (Principal) |
|---|---|---|---|
| **Requirements** | Lists Publish/Subscribe operations, mentions at-least-once | Quantifies limits (12.5M subs, 256 KB, 30K/sec), explains standard vs FIFO, proactively raises filtering and DLQ | Frames requirements around customer SLAs and cost. Discusses when SNS is the wrong choice (use EventBridge, Kafka, etc.) |
| **Architecture** | Topic server with subscribers | 3-layer (API + store + fan-out), iterative build-up from naive to partitioned parallel | Cell-based architecture, failure domain isolation, deployment topology, control plane vs data plane |
| **Fan-out** | "Workers deliver messages" | Partitioned parallel fan-out, subscription chunking, coordinator + workers, back-pressure | Per-protocol delivery fleet segmentation, capacity planning math (workers x throughput = fleet size), auto-scaling |
| **Filtering** | "Subscribers can filter messages" | Filter-at-SNS design, filter operators, 200/topic limit, 15-min propagation | Filter policy compilation/caching, filter evaluation amortization for hot topics, cost-per-filtered-message |
| **Delivery & Retries** | "Retry with backoff" | Two-tier retry (100K/23d vs 50/6h), custom HTTP policies, DLQ per subscription | Per-subscriber circuit breakers, retry budget management, delivery fleet health as SLI |
| **FIFO** | "Use FIFO for ordering" | Message groups, dedup window, content-based dedup, 100 sub limit, filtering caveat | Per-partition sequencing design, dedup store sizing, FIFO throughput optimization |
| **SNS + SQS** | "Use SNS with SQS queues" | Explains decoupling, independent consumption, filtering per consumer. Compares with EventBridge | Discusses topic design as domain boundary, multi-account SNS+SQS topology, organizational governance |
| **Durability** | "Replicate messages across AZs" | Store-then-fan-out, quorum writes, message lifetime, failure scenarios | Write-ahead log compaction, storage sizing for worst case (23 days undeliverable), gray failure detection |
| **Operational** | Mentions monitoring | Identifies specific failure modes with mitigations (amplification, slow subs, DLQ gap, filter propagation) | Proposes blast radius isolation strategy, game days, automated remediation, cost observability dashboards |
| **Communication** | Responds to questions | Drives the conversation, uses diagrams/tables, iterative naive→refined | Negotiates scope, manages time, proposes phased deep dives, identifies highest-signal areas |

---

## Appendix: Key AWS SNS Numbers Reference

All numbers below are verified against AWS documentation unless marked otherwise.

| Metric | Value | Source |
|---|---|---|
| Max topics per account (standard) | 100,000 (soft limit) | AWS SNS Quotas |
| Max topics per account (FIFO) | 1,000 (soft limit) | AWS SNS Quotas |
| Max subscriptions per standard topic | 12,500,000 | AWS SNS Quotas |
| Max subscriptions per FIFO topic | 100 | AWS SNS Quotas |
| Max message size | 256 KB (up to 2 GB with Extended Client Library) | AWS SNS Quotas |
| Max message attributes | 10 (with raw message delivery) | AWS SNS Docs |
| Max message header size | 16 KB | AWS SNS Quotas |
| Publish rate (standard, US East) | 30,000 msg/sec (soft limit) | AWS SNS Quotas |
| Publish rate (FIFO) | Up to 30,000 msg/sec per topic; 300 msg/sec per message group | AWS SNS Quotas |
| Filter policies per topic | 200 | AWS SNS Quotas |
| Filter policies per account | 10,000 | AWS SNS Quotas |
| Filter policy propagation delay | Up to 15 minutes | AWS SNS Docs |
| Retry attempts (AWS-managed endpoints) | 100,015 over 23 days | AWS SNS Docs |
| Retry attempts (customer-managed endpoints) | 50 over 6 hours | AWS SNS Docs |
| HTTP/S max retry duration | 3,600 seconds (hard limit) | AWS SNS Docs |
| FIFO deduplication window | 5 minutes | AWS SNS Docs |
| Batch publish max entries | 10 per PublishBatch | AWS SNS Quotas |
| Email delivery rate | 10 msg/sec per subscription (hard limit) | AWS SNS Quotas |
| SMS delivery rate | 20 msg/sec | AWS SNS Quotas |
| Subscribe/Unsubscribe API rate | 100 TPS | AWS SNS Quotas |
| DLQ message retention | Up to 14 days (SQS limit, recommended max) | AWS SNS Docs |

---

*For detailed deep dives on each component, see the companion documents:*
- [Fan-Out Engine](fan-out-engine.md) -- Partitioned parallel fan-out, subscription chunking, filtering evaluation, back-pressure
- [Delivery & Retries](delivery-and-retries.md) -- Protocol-specific delivery, retry policies, DLQ design, delivery monitoring
- [SNS + SQS Fan-Out Pattern](sns-sqs-fanout.md) -- The canonical architecture pattern, with filtering, cost analysis, EventBridge comparison
- [FIFO Topics](fifo-topics.md) -- Message group ordering, deduplication, exactly-once guarantees, throughput limits
- [Message Filtering](message-filtering.md) -- Filter policy syntax, evaluation mechanics, propagation, cost savings
- [Mobile Push & App-to-Person](mobile-push.md) -- APNs/FCM/ADM integration, SMS delivery, email confirmation

*End of interview simulation.*
