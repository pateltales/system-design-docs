# Amazon SNS + SQS Fan-Out Pattern Deep Dive

> Companion deep dive to the [interview simulation](interview-simulation.md). This document explores the most important architecture pattern in AWS messaging: using SNS topics to fan out messages to multiple SQS queues, enabling decoupled, independently scalable, failure-isolated microservice communication.

---

## Table of Contents

1. [Why This Pattern Exists](#1-why-this-pattern-exists)
2. [The Core Pattern](#2-the-core-pattern)
3. [Step-by-Step Message Flow](#3-step-by-step-message-flow)
4. [Setting Up the Pattern](#4-setting-up-the-pattern)
5. [Message Filtering in Fan-Out](#5-message-filtering-in-fan-out)
6. [Decoupling Benefits — The Five Independences](#6-decoupling-benefits--the-five-independences)
7. [FIFO SNS + FIFO SQS Fan-Out](#7-fifo-sns--fifo-sqs-fan-out)
8. [Raw Message Delivery in Fan-Out](#8-raw-message-delivery-in-fan-out)
9. [Cross-Account Fan-Out](#9-cross-account-fan-out)
10. [Error Handling and DLQs in Fan-Out](#10-error-handling-and-dlqs-in-fan-out)
11. [Cost Analysis](#11-cost-analysis)
12. [SNS+SQS vs Direct SQS vs EventBridge](#12-snssqs-vs-direct-sqs-vs-eventbridge)
13. [Real-World Architecture Examples](#13-real-world-architecture-examples)
14. [Anti-Patterns](#14-anti-patterns)
15. [Cross-References](#15-cross-references)

---

## 1. Why This Pattern Exists

### The Problem: One Event, Multiple Consumers

```
An "order placed" event needs to trigger:
    1. Inventory service: decrement stock
    2. Payment service: charge the card
    3. Email service: send confirmation
    4. Analytics service: record the event
    5. Fraud service: check for suspicious patterns
    6. Loyalty service: award points
```

### The Wrong Way: Direct Point-to-Point

```
Order Service
  │
  ├── sqs:SendMessage(inventory-queue)     ← knows about 6 downstream services
  ├── sqs:SendMessage(payment-queue)       ← adding service 7 requires code change
  ├── sqs:SendMessage(email-queue)         ← if payment-queue is down, does order service
  ├── sqs:SendMessage(analytics-queue)     │  need to retry? Handle errors?
  ├── sqs:SendMessage(fraud-queue)         ← 6 API calls per order = slow
  └── sqs:SendMessage(loyalty-queue)

Problems:
    1. Tight coupling: Order Service knows about every consumer
    2. Slow: 6 sequential API calls per order (or complex parallel code)
    3. Error complexity: Must handle partial failures (3 of 6 succeed)
    4. Change friction: Adding a new consumer requires Order Service code change + deploy
```

### The Right Way: SNS + SQS Fan-Out

```
Order Service
  │
  │  sns:Publish(order-events-topic, message)    ← ONE API call
  │  Returns immediately (< 20ms)
  ▼
┌───────────────────────┐
│  SNS Topic:           │
│  order-events         │
└───────────┬───────────┘
            │ Fan-out (parallel, independent)
  ┌─────────┼─────────┬─────────┬─────────┬─────────┐
  ▼         ▼         ▼         ▼         ▼         ▼
SQS:      SQS:      SQS:      SQS:      SQS:      SQS:
inventory payment   email     analytics  fraud     loyalty
queue     queue     queue     queue      queue     queue
  │         │         │         │         │         │
  ▼         ▼         ▼         ▼         ▼         ▼
Inventory Payment   Email     Analytics  Fraud     Loyalty
Service   Service   Service   Service    Service   Service

Order Service: sends ONE message. Done.
SNS: fans out to 6 queues in parallel. Handles retries independently.
Each service: polls its own queue at its own pace.
```

---

## 2. The Core Pattern

### Architecture Diagram

```
┌──────────────────────────────────────────────────────────────────┐
│                        PRODUCER SIDE                              │
│                                                                   │
│  ┌─────────────┐    sns:Publish     ┌───────────────────┐        │
│  │   Producer   │ ─────────────────► │    SNS Topic       │       │
│  │   Service    │    (1 API call)    │                    │       │
│  │              │    < 20ms          │  "order-events"    │       │
│  └─────────────┘                    └────────┬──────────┘        │
│                                              │                    │
└──────────────────────────────────────────────│────────────────────┘
                                               │
                           SNS Fan-Out (parallel, per-subscription)
                                               │
┌──────────────────────────────────────────────│────────────────────┐
│                        CONSUMER SIDE          │                    │
│                                               │                    │
│    ┌──────────────────────────────────────────┤                    │
│    │              │              │             │                    │
│    ▼              ▼              ▼             ▼                    │
│  ┌──────┐    ┌──────┐     ┌──────┐     ┌──────┐                  │
│  │ SQS  │    │ SQS  │     │ SQS  │     │ SQS  │                  │
│  │Queue │    │Queue │     │Queue │     │Queue │                   │
│  │  A   │    │  B   │     │  C   │     │  D   │                  │
│  └──┬───┘    └──┬───┘     └──┬───┘     └──┬───┘                  │
│     │           │            │            │                       │
│     ▼           ▼            ▼            ▼                       │
│  Service A   Service B   Service C   Service D                    │
│  (fast)      (slow)      (batch)     (real-time)                 │
│                                                                   │
│  Each service polls its own queue independently.                  │
│  Each has its own:                                                │
│    - Polling rate                                                  │
│    - Visibility timeout                                           │
│    - Dead letter queue                                            │
│    - Retry behavior                                               │
│    - Scaling policy                                               │
└──────────────────────────────────────────────────────────────────┘
```

---

## 3. Step-by-Step Message Flow

### Complete End-to-End Flow

```
1. PUBLISH
   Producer → sns:Publish(TopicArn, Message, MessageAttributes)
   SNS: validate, auth, store durably (multi-AZ), return 200 + MessageId
   Time: < 20ms

2. FAN-OUT COORDINATION
   SNS Fan-Out Coordinator:
     - Read subscription list for topic
     - Evaluate filter policies against message attributes
     - Create delivery tasks for matching subscriptions
   Time: ~5-10ms [INFERRED]

3. DELIVERY TO SQS
   SNS Delivery Worker → sqs:SendMessage for each matching SQS subscription
   Each delivery is independent and parallel:
     - Queue A: SendMessage → success ✓
     - Queue B: SendMessage → success ✓
     - Queue C: SendMessage → timeout → retry → success ✓
     - Queue D: SendMessage → success ✓
   Time: ~5ms per SQS delivery (same region, internal network)

4. CONSUMER PROCESSING
   Each service independently:
     - sqs:ReceiveMessage(QueueUrl, MaxNumberOfMessages=10, WaitTimeSeconds=20)
     - Process message(s)
     - sqs:DeleteMessage (or DeleteMessageBatch)
   Time: depends on consumer logic

5. FAILURE HANDLING
   If SQS delivery fails after 100,015 retries (23 days):
     → Message sent to SNS subscription's DLQ (if configured)
   If consumer fails to process message after SQS maxReceiveCount:
     → Message sent to SQS queue's DLQ (if configured)
```

### Two Levels of DLQ

```
This is a critical distinction:

Level 1: SNS DLQ (for delivery failures)
    SNS cannot deliver to the SQS queue
    → SNS subscription's DLQ catches it
    Cause: queue deleted, permission denied, throttled for 23 days

Level 2: SQS DLQ (for processing failures)
    Message delivered to SQS, but consumer fails to process it
    → SQS queue's DLQ catches it after maxReceiveCount
    Cause: consumer crashes, message format error, downstream dependency down

┌────────┐    ┌─────────┐    ┌──────────┐    ┌──────────┐
│  SNS   │───►│   SQS   │───►│ Consumer │    │ Consumer │
│ Topic  │    │  Queue   │    │ (fails)  │    │ (retry)  │
└────────┘    └────┬─────┘    └──────────┘    └──────────┘
    │              │
    │ Delivery     │ Processing
    │ failure      │ failure
    ▼              ▼
┌────────┐    ┌──────────┐
│SNS DLQ │    │ SQS DLQ  │
│(rare)  │    │(common)  │
└────────┘    └──────────┘
```

---

## 4. Setting Up the Pattern

### Step 1: Create the SNS Topic

```
aws sns create-topic --name order-events
→ TopicArn: arn:aws:sns:us-east-1:123456789012:order-events
```

### Step 2: Create SQS Queues

```
aws sqs create-queue --queue-name inventory-queue
aws sqs create-queue --queue-name email-queue
aws sqs create-queue --queue-name analytics-queue
```

### Step 3: Set SQS Queue Policy (Allow SNS to Send)

Each SQS queue needs a policy allowing the SNS topic to send messages:

```json
{
    "Version": "2012-10-17",
    "Statement": [{
        "Effect": "Allow",
        "Principal": { "Service": "sns.amazonaws.com" },
        "Action": "sqs:SendMessage",
        "Resource": "arn:aws:sqs:us-east-1:123456789012:inventory-queue",
        "Condition": {
            "ArnEquals": {
                "aws:SourceArn": "arn:aws:sns:us-east-1:123456789012:order-events"
            }
        }
    }]
}
```

### Step 4: Subscribe SQS Queues to the Topic

```
aws sns subscribe \
    --topic-arn arn:aws:sns:us-east-1:123456789012:order-events \
    --protocol sqs \
    --notification-endpoint arn:aws:sqs:us-east-1:123456789012:inventory-queue

aws sns subscribe \
    --topic-arn arn:aws:sns:us-east-1:123456789012:order-events \
    --protocol sqs \
    --notification-endpoint arn:aws:sqs:us-east-1:123456789012:email-queue
```

SQS subscriptions are **auto-confirmed** (no manual confirmation needed, unlike HTTP).

### Step 5: Publish

```
aws sns publish \
    --topic-arn arn:aws:sns:us-east-1:123456789012:order-events \
    --message '{"orderId": "12345", "amount": 99.99}' \
    --message-attributes '{"event_type":{"DataType":"String","StringValue":"order_placed"}}'
```

---

## 5. Message Filtering in Fan-Out

Filtering is where this pattern becomes truly powerful. Without filtering, every SQS queue gets every message. With filtering, each queue gets only the messages it cares about.

### Setting Filter Policies

```
aws sns set-subscription-attributes \
    --subscription-arn arn:aws:sns:us-east-1:123456789012:order-events:abc123 \
    --attribute-name FilterPolicy \
    --attribute-value '{"event_type": ["order_placed", "order_cancelled"]}'
```

### Example: Order Events with Filtering

```
Topic: order-events

Subscriptions and their filter policies:

    inventory-queue:
        {"event_type": ["order_placed", "order_cancelled"]}
        → Only gets order lifecycle events

    email-queue:
        {"event_type": ["order_placed", "order_shipped"]}
        → Only gets events requiring customer notification

    analytics-queue:
        (no filter — accepts everything)
        → Gets all events for analytics

    fraud-queue:
        {"amount": [{"numeric": [">", 500]}]}
        → Only gets high-value orders

    loyalty-queue:
        {"customer_tier": ["gold", "platinum"]}
        → Only gets events for premium customers
```

### Message Published

```json
{
    "Message": "{\"orderId\": \"12345\", \"amount\": 99.99}",
    "MessageAttributes": {
        "event_type": { "DataType": "String", "StringValue": "order_placed" },
        "amount": { "DataType": "Number", "StringValue": "99.99" },
        "customer_tier": { "DataType": "String", "StringValue": "silver" }
    }
}
```

### Filter Evaluation Results

```
inventory-queue: event_type=order_placed matches ["order_placed", "order_cancelled"] → DELIVER ✓
email-queue:     event_type=order_placed matches ["order_placed", "order_shipped"]   → DELIVER ✓
analytics-queue: no filter                                                           → DELIVER ✓
fraud-queue:     amount=99.99 NOT > 500                                              → SKIP ✗
loyalty-queue:   customer_tier=silver NOT in ["gold", "platinum"]                    → SKIP ✗

Result: 3 of 5 queues receive the message. 40% reduction in deliveries.
```

### Filtering Saves Money

```
Without filtering:
    1,000,000 publishes/day × 5 queues = 5,000,000 deliveries
    Each queue's consumers process 1,000,000 messages (including irrelevant ones)

With filtering (40% average filter rate):
    1,000,000 publishes/day × 3 avg matching queues = 3,000,000 deliveries
    Each consumer only processes relevant messages

    Savings:
    - 2,000,000 fewer SQS receives per day
    - 2,000,000 fewer consumer invocations per day
    - Lower compute cost for consumers
    - SNS delivery cost for SQS is free (so SNS-side savings are zero,
      but subscriber-side savings are significant)
```

### FilterPolicyScope: MessageAttributes vs MessageBody

```
MessageAttributes scope (default):
    Filter against structured attributes (key-value pairs)
    Attributes are separate from the message body
    Most common, simplest

MessageBody scope:
    Filter against properties IN the JSON message body
    No need to separately set message attributes
    More flexible but requires JSON body

    aws sns set-subscription-attributes \
        --subscription-arn ... \
        --attribute-name FilterPolicyScope \
        --attribute-value MessageBody

    Filter policy:
    {
        "order": {
            "amount": [{"numeric": [">", 500]}]
        }
    }

    Matches message body:
    { "order": { "amount": 750, "id": "12345" } }
```

---

## 6. Decoupling Benefits — The Five Independences

### Independence 1: Deployment Independence

```
Without SNS+SQS:
    Order Service deploys → must coordinate with all 6 downstream services
    Adding a new consumer → modify and redeploy Order Service

With SNS+SQS:
    Order Service deploys independently
    Adding a new consumer → subscribe new queue to topic
    No Order Service change. No Order Service deploy.
    The new team owns their queue and consumer.
```

### Independence 2: Rate Independence

```
Producer publishes 1,000 events/sec

Without SNS+SQS:
    All consumers must handle 1,000 events/sec or fail
    A slow consumer blocks the producer (or producer drops messages)

With SNS+SQS:
    SNS delivers to all queues at wire speed (~5ms per delivery)
    Each queue buffers independently:
        Inventory service: processes 1,000/sec (keeps up) ✓
        Email service: processes 10/sec (queue grows, catches up over time) ✓
        Analytics service: batch-processes every 5 minutes ✓
```

### Independence 3: Failure Independence

```
Email service goes down for 2 hours.

Without SNS+SQS:
    Producer must handle the failure
    Options: drop email notifications, retry, circuit break
    Complexity in the producer

With SNS+SQS:
    SNS delivers to email-queue → succeeds (SQS always accepts)
    email-queue buffers messages (up to 14 days retention)
    Other services unaffected
    When email service recovers → processes 2-hour backlog
    Zero impact on producer or other consumers
```

### Independence 4: Scaling Independence

```
Analytics traffic spikes 10×.

Without SNS+SQS:
    Analytics consumers must scale immediately or fail
    If they can't keep up, messages are lost or producer is throttled

With SNS+SQS:
    analytics-queue absorbs the burst (SQS has unlimited message backlog)
    Auto-scale analytics consumers based on queue depth
    Process the backlog at whatever rate you can
    Other services see zero impact
```

### Independence 5: Technology Independence

```
Each consuming service can use different:
    - Programming language (Java, Python, Go, Node)
    - Framework
    - Processing model (synchronous, batch, event-driven)
    - Cloud infrastructure (EC2, Lambda, ECS, EKS)

All they need is an SQS client to poll their queue.
The producer doesn't know or care.
```

---

## 7. FIFO SNS + FIFO SQS Fan-Out

### When You Need Ordered Fan-Out

```
Standard SNS + Standard SQS:
    Messages may arrive out of order at each queue
    Each queue may receive duplicates
    Sufficient for most use cases

FIFO SNS + FIFO SQS:
    Messages arrive in order (per MessageGroupId) at each FIFO queue
    Exactly-once processing (within 5-minute dedup window)
    Required for: financial transactions, state machines, audit trails
```

### Architecture

```
Producer
  │
  │ sns:Publish(
  │   TopicArn="order-events.fifo",
  │   Message="...",
  │   MessageGroupId="order-12345",
  │   MessageDeduplicationId="txn-abc"
  │ )
  ▼
┌───────────────────────────┐
│  SNS FIFO Topic:          │
│  order-events.fifo        │
│                           │
│  Max 100 subscriptions    │
│  300 msg/sec per group    │
│  30K msg/sec per topic    │
└─────────────┬─────────────┘
              │
    ┌─────────┼─────────┐
    ▼         ▼         ▼
SQS FIFO:  SQS FIFO:  SQS Standard:
inventory  payment    analytics
.fifo      .fifo      (no ordering
                       needed)

Each FIFO queue:
  - Receives messages in order per MessageGroupId
  - Deduplication per message group (5-min window)
  - Consumer processes in order within each group
```

### FIFO Fan-Out Limitations

| Limitation | Impact |
|------------|--------|
| Max 100 subscriptions per FIFO topic | Can't fan out to thousands of consumers |
| Only SQS FIFO and SQS Standard as subscribers | No Lambda, HTTP, email, SMS, or push |
| 300 msg/sec per message group | Hot groups become bottlenecks |
| 30,000 msg/sec per topic (high-throughput mode) | Lower than standard topics |
| Message filtering changes guarantee to at-most-once | Filtered messages may be lost in edge cases |

### FIFO Fan-Out: When to Use vs When to Avoid

```
USE FIFO fan-out when:
    ✓ Ordering is a hard requirement (financial, state machines)
    ✓ Exactly-once processing is needed
    ✓ Fan-out is small (< 100 consumers)
    ✓ All consumers need ordering guarantees

AVOID FIFO fan-out when:
    ✗ Need high fan-out (> 100 consumers)
    ✗ Need HTTP/Lambda/email subscribers
    ✗ Throughput > 30K msg/sec per topic
    ✗ Only some consumers need ordering (use standard + consumer-side ordering)
```

---

## 8. Raw Message Delivery in Fan-Out

### Why Use Raw Delivery

```
Without raw delivery:
    SQS message body = SNS JSON envelope wrapping the actual message

    Consumer code:
    1. Read SQS message body (SNS JSON envelope)
    2. Parse JSON
    3. Extract "Message" field (the actual payload)
    4. Parse actual payload

With raw delivery:
    SQS message body = just the actual message

    Consumer code:
    1. Read SQS message body (the actual payload)
    2. Process directly

    Simpler consumer. Fewer bytes in SQS. No double-parsing.
```

### Enabling Raw Delivery for a Subscription

```
aws sns set-subscription-attributes \
    --subscription-arn arn:aws:sns:...:order-events:abc123 \
    --attribute-name RawMessageDelivery \
    --attribute-value true
```

### The 10-Attribute Limitation

```
With raw delivery, SNS message attributes → SQS message attributes.
SQS message attributes have a HARD LIMIT of 10.

If your SNS message has > 10 attributes:
    → The message is SILENTLY DISCARDED for that subscription
    → No retry. No DLQ. Client-side error.
    → Only visible in CloudWatch metrics (NumberOfNotificationsFailed)

This is a dangerous pitfall. Always verify your message attribute count
if you enable raw delivery.
```

---

## 9. Cross-Account Fan-Out

### Architecture

```
Account A (Producer)                Account B (Consumer)
┌──────────────────────┐           ┌──────────────────────┐
│                      │           │                      │
│  Producer Service    │           │  Consumer Service    │
│       │              │           │       ▲              │
│       ▼              │           │       │              │
│  ┌──────────────┐    │           │  ┌──────────────┐    │
│  │  SNS Topic   │────│───────────│──│  SQS Queue   │    │
│  │ (Account A)  │    │    SNS    │  │ (Account B)  │    │
│  └──────────────┘    │  delivers │  └──────────────┘    │
│                      │           │                      │
└──────────────────────┘           └──────────────────────┘
```

### Permissions Required

**On the SNS Topic (Account A)** — topic policy allowing Account B to subscribe:

```json
{
    "Statement": [{
        "Effect": "Allow",
        "Principal": { "AWS": "222222222222" },
        "Action": "sns:Subscribe",
        "Resource": "arn:aws:sns:us-east-1:111111111111:order-events",
        "Condition": {
            "StringEquals": {
                "sns:Protocol": "sqs"
            }
        }
    }]
}
```

**On the SQS Queue (Account B)** — queue policy allowing SNS to send:

```json
{
    "Statement": [{
        "Effect": "Allow",
        "Principal": { "Service": "sns.amazonaws.com" },
        "Action": "sqs:SendMessage",
        "Resource": "arn:aws:sqs:us-east-1:222222222222:my-queue",
        "Condition": {
            "ArnEquals": {
                "aws:SourceArn": "arn:aws:sns:us-east-1:111111111111:order-events"
            }
        }
    }]
}
```

### Cross-Region Fan-Out

```
SNS Topic: us-east-1 (Account A)
SQS Queue: eu-west-1 (Account B)

This works. SNS delivers cross-region via AWS internal backbone.

Latency: ~10-50ms additional (cross-region network)
Reliability: Slightly lower (cross-region failures more likely)
Cost: No additional SNS cost. Standard data transfer costs apply.

Recommendation: Keep topic and queues in same region for
                lowest latency and highest reliability.
```

---

## 10. Error Handling and DLQs in Fan-Out

### Error Handling Architecture

```
                    ┌─────────────────┐
                    │    SNS Topic     │
                    └────────┬────────┘
                             │
              ┌──────────────┼──────────────┐
              ▼              ▼              ▼
         ┌─────────┐   ┌─────────┐   ┌─────────┐
         │  SQS Q1  │   │  SQS Q2  │   │  SQS Q3  │
         │          │   │          │   │          │
   ┌─────│ maxRecv  │   │ maxRecv  │   │ maxRecv  │
   │     │   = 3    │   │   = 5    │   │   = 10   │
   │     └────┬─────┘   └────┬─────┘   └────┬─────┘
   │          │              │              │
   │     Consumer 1     Consumer 2     Consumer 3
   │     (fails 3×)     (succeeds)    (fails 10×)
   │          │                            │
   │          ▼                            ▼
   │    ┌──────────┐                ┌──────────┐
   │    │ SQS DLQ1 │                │ SQS DLQ3 │
   │    └──────────┘                └──────────┘
   │
   ▼ (rare: SNS can't deliver to Q1)
┌──────────┐
│ SNS DLQ  │
│ (per sub)│
└──────────┘
```

### Best Practice: Configure Both DLQ Levels

```
Level 1: SNS subscription DLQ
    For: SNS cannot deliver to SQS queue
    When: Queue deleted, permission denied, throttled for 23 days
    How:  Set RedrivePolicy on the SNS subscription

Level 2: SQS queue DLQ
    For: Consumer cannot process the message
    When: Consumer crashes, business logic error, timeout
    How:  Set RedrivePolicy on the SQS queue (maxReceiveCount)

Always configure BOTH. Missing either creates a message loss gap.
```

---

## 11. Cost Analysis

### Pricing Components

```
1. SNS Publish:           $0.50 per million publishes
2. SNS→SQS delivery:     FREE (first 1M/month; $0.50/M after — but SQS delivery is free)
3. SQS operations:        $0.40 per million requests (or $0.50 for FIFO)
4. Data transfer:          FREE within same region

Note: SNS delivery to SQS is free (it's a "no-charge delivery").
The main costs are the Publish call and the SQS consumer operations.
```

### Cost Calculation Example

```
Scenario:
    1,000,000 messages/day published to topic
    5 SQS subscriptions (3 with filtering: avg 60% match rate)
    Consumers use long polling, batch receive (10 per call), batch delete

Cost breakdown:

    SNS Publishes:
        1M messages/day × 30 days = 30M publishes/month
        First 1M free, then: 29M × $0.50/M = $14.50/month

    SNS→SQS Delivery:
        FREE

    SQS Operations:
        Filtered deliveries: 1M × (2 unfiltered + 3 × 0.6 filtered) = 3.8M messages/day
        Per queue per day:
            Receive: 3.8M / 5 = 760K messages / 10 per batch = 76K ReceiveMessage calls
            Delete:  760K / 10 per batch = 76K DeleteMessageBatch calls
            Total:   152K SQS API calls per queue per day
        All queues: 152K × 5 = 760K SQS calls/day
        Monthly:    760K × 30 = 22.8M SQS requests
        Cost:       22.8M × $0.40/M = $9.12/month

    Total: $14.50 (SNS) + $9.12 (SQS) = ~$23.62/month

    For 1M messages/day with 5 consumers. That's remarkably cheap.
```

### Cost Comparison

| Architecture | Monthly Cost (1M msg/day, 5 consumers) | Ops Overhead |
|-------------|:--------------------------------------:|:------------:|
| SNS + SQS | ~$24 | None |
| Direct SQS (producer sends 5×) | ~$18 | Moderate (producer coupling) |
| EventBridge + SQS | ~$35 | None |
| Self-managed RabbitMQ | ~$100-300 (EC2 instances) | High |
| Self-managed Kafka (MSK) | ~$200-500 (brokers) | High |

---

## 12. SNS+SQS vs Direct SQS vs EventBridge

### Decision Matrix

| Factor | Direct SQS | SNS + SQS | EventBridge |
|--------|:----------:|:---------:|:-----------:|
| **Fan-out** | Manual (N sends) | Automatic (1 send) | Automatic (rules) |
| **Coupling** | Producer → every queue | Producer → one topic | Producer → one bus |
| **Adding consumers** | Producer code change | Subscribe to topic | Add rule |
| **Filtering** | Producer decides | SNS filter policies | Content-based rules (richer) |
| **Max throughput** | Unlimited (SQS) | 30K publishes/sec/topic | Varies (typically lower) |
| **Max targets** | N/A | 12.5M subs/topic | 5 targets/rule, 300 rules |
| **Ordering** | SQS FIFO | FIFO SNS + FIFO SQS | FIFO not supported |
| **Schema** | None | None | Schema registry |
| **Replay** | No | No (FIFO archive only) | Event archive + replay |
| **Cost** | $0.40/M per queue | $0.50/M publish + $0.40/M per queue | $1.00/M events |
| **Non-SQS targets** | No | Lambda, HTTP, email, SMS, push | 20+ targets |

### When to Use Each

```
Direct SQS:
    ✓ One producer, one consumer (point-to-point)
    ✓ No fan-out needed
    ✓ Maximum simplicity

SNS + SQS:
    ✓ One producer, multiple consumers (fan-out)
    ✓ High throughput (30K+ publishes/sec)
    ✓ Simple attribute-based filtering
    ✓ Mixed subscribers (SQS + Lambda + HTTP)
    ✓ Cost-sensitive workloads

EventBridge:
    ✓ Complex content-based routing (JSON path matching)
    ✓ Schema registry and validation needed
    ✓ Cross-account event buses
    ✓ Event replay from archive
    ✓ Wide range of targets (Step Functions, API Gateway, etc.)
    ✓ Lower throughput is acceptable
```

---

## 13. Real-World Architecture Examples

### Example 1: E-Commerce Order Pipeline

```
┌──────────────┐
│ Order Service │
│    Publish:   │
│    {          │
│     event:    │
│     "placed", │
│     orderId,  │
│     amount,   │
│     customer  │
│    }          │
└──────┬───────┘
       ▼
┌────────────────────┐
│ SNS: order-events  │
└────────┬───────────┘
         │
   ┌─────┼─────┬─────┬─────┬─────┐
   ▼     ▼     ▼     ▼     ▼     ▼
 SQS:  SQS:  SQS:  SQS:  SQS:  SQS:
 inv.  pay.  email  ship  fraud  audit

Filter policies:
  inventory: {event: ["placed","cancelled"]}
  payment:   {event: ["placed"]}
  email:     {event: ["placed","shipped","delivered"]}
  shipping:  {event: ["placed"], amount: [{"numeric":[">",0]}]}
  fraud:     {amount: [{"numeric":[">",500]}]}
  audit:     (no filter — receives everything)
```

### Example 2: Multi-Tenant SaaS Event Bus

```
┌─────────────────────────────────────┐
│  Tenant Services (publish events)    │
│                                      │
│  Tenant A: user.created              │
│  Tenant B: invoice.paid              │
│  Tenant C: report.generated          │
└──────────────┬──────────────────────┘
               ▼
┌─────────────────────────────────────┐
│  SNS: platform-events               │
│                                      │
│  MessageAttributes:                  │
│    tenant_id: "tenant-A"             │
│    event_type: "user.created"        │
│    severity: "info"                  │
└──────────────┬──────────────────────┘
               │
   ┌───────────┼───────────┬────────────────┐
   ▼           ▼           ▼                ▼
 SQS:        SQS:        SQS:            SQS:
 billing     analytics   notifications   compliance

 Filter:     Filter:     Filter:          Filter:
 (no filter  {event_type: {event_type:    {severity:
  — all       ["invoice.  ["user.created", ["critical",
  events)     paid",      "password.       "error"]}
              "payment.   reset"]}
              failed"]}
```

### Example 3: IoT Event Processing

```
┌──────────────────────┐
│  IoT Core Rule       │
│  (device telemetry)  │
└──────────┬───────────┘
           ▼
┌──────────────────────┐
│  SNS: device-events  │
└──────────┬───────────┘
           │
   ┌───────┼───────┬────────────┐
   ▼       ▼       ▼            ▼
 SQS:    SQS:    SQS:         SQS:
 real-   time-   anomaly      firmware
 time    series  detection    update
 alerts  ingest  queue        queue

 Filter: Filter: Filter:      Filter:
 {temp:  (no     {temp:       {device_type:
 [{num:  filter) [{num:       ["thermostat"],
 [">",           [">",80]}], firmware_ver:
  100]}]}        anomaly:    [{"anything-but":
                 [true]}      ["2.1.0"]}]}
```

---

## 14. Anti-Patterns

### Anti-Pattern 1: SNS Topic Per Consumer

```
BAD:
    order-events-for-inventory (topic)  → inventory-queue
    order-events-for-email (topic)      → email-queue
    order-events-for-analytics (topic)  → analytics-queue

    Producer publishes to 3 topics = 3 Publish calls
    Defeats the entire purpose of SNS fan-out

GOOD:
    order-events (1 topic) → inventory-queue
                           → email-queue
                           → analytics-queue

    Producer publishes once. SNS fans out.
```

### Anti-Pattern 2: No Message Filtering

```
BAD:
    All 5 queues receive all messages
    Each consumer filters in application code:
        if (event.type != "order_placed") return;

    Result: 80% of messages are discarded by consumers
    Wasted SQS receive cost + consumer compute

GOOD:
    Set SNS filter policies per subscription
    Each queue receives only relevant messages
    Consumer processes everything it receives
```

### Anti-Pattern 3: Forgetting DLQ on SQS Queues

```
BAD:
    SNS → SQS (no DLQ) → Consumer

    Consumer fails → message retried infinitely
    → Message ping-pongs between queue and consumer forever
    → Or: maxReceiveCount not set, message eventually expires silently

GOOD:
    SNS → SQS (DLQ configured, maxReceiveCount=3) → Consumer

    Consumer fails 3 times → message goes to DLQ
    Ops team investigates, fixes, redrives from DLQ
```

### Anti-Pattern 4: Using SNS+SQS When Direct SQS Suffices

```
BAD:
    Producer → SNS Topic → SQS Queue → Single Consumer

    If there's only ONE consumer and no plan for more:
    The SNS topic adds latency, cost, and complexity for no benefit.

GOOD:
    Producer → SQS Queue → Single Consumer

    Direct SQS for point-to-point.
    Add SNS later when you need fan-out.
```

### Anti-Pattern 5: Giant Messages Through SNS+SQS

```
BAD:
    Publishing 250 KB JSON payloads through SNS → SQS
    SNS wraps in JSON envelope → SQS message > 256 KB → FAILS

GOOD:
    Use claim-check pattern:
    1. Store large payload in S3
    2. Publish S3 reference (key + bucket) via SNS (tiny message)
    3. Consumer reads S3 reference from SQS, fetches full payload from S3

    Or: enable raw message delivery to avoid the SNS JSON envelope overhead
```

---

## 15. Cross-References

| Topic | Document |
|-------|----------|
| Fan-out engine internals | [fan-out-engine.md](fan-out-engine.md) |
| Delivery retry policies | [delivery-and-retries.md](delivery-and-retries.md) |
| FIFO topics deep dive | [fifo-topics.md](fifo-topics.md) |
| Message filtering | [message-filtering.md](message-filtering.md) |
| Mobile push delivery | [mobile-push.md](mobile-push.md) |
| Interview simulation | [interview-simulation.md](interview-simulation.md) |

### AWS Documentation References

- [Amazon SNS message filtering](https://docs.aws.amazon.com/sns/latest/dg/sns-message-filtering.html)
- [Amazon SNS raw message delivery](https://docs.aws.amazon.com/sns/latest/dg/sns-large-payload-raw-message-delivery.html)
- [Amazon SNS FIFO topics](https://docs.aws.amazon.com/sns/latest/dg/sns-fifo-topics.html)
- [Fanout to Amazon SQS queues](https://docs.aws.amazon.com/sns/latest/dg/sns-sqs-as-subscriber.html)
- [Cross-account delivery](https://docs.aws.amazon.com/sns/latest/dg/sns-send-message-to-sqs-cross-account.html)
