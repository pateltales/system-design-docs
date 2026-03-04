# Amazon SNS — Message Filtering Deep Dive

> Companion deep dive to the [interview simulation](interview-simulation.md). This document covers filter policy syntax, all matching operators, AND/OR logic, filter policy scope (MessageAttributes vs MessageBody), propagation behavior, performance impact, and cost savings from filtering.

---

## Table of Contents

1. [Why Message Filtering Matters](#1-why-message-filtering-matters)
2. [Filter Policy Basics](#2-filter-policy-basics)
3. [Filter Policy Scope: MessageAttributes vs MessageBody](#3-filter-policy-scope-messageattributes-vs-messagebody)
4. [String Matching Operators](#4-string-matching-operators)
5. [Numeric Matching Operators](#5-numeric-matching-operators)
6. [Existence Matching](#6-existence-matching)
7. [IP Address CIDR Matching](#7-ip-address-cidr-matching)
8. [AND/OR Logic](#8-andor-logic)
9. [Filter Policy Limits](#9-filter-policy-limits)
10. [Filter Propagation Delay — The 15-Minute Window](#10-filter-propagation-delay--the-15-minute-window)
11. [Filtering Architecture — Where It Happens](#11-filtering-architecture--where-it-happens)
12. [Cost Savings from Filtering](#12-cost-savings-from-filtering)
13. [Filtering + FIFO Topics](#13-filtering--fifo-topics)
14. [Common Filtering Patterns](#14-common-filtering-patterns)
15. [Anti-Patterns](#15-anti-patterns)
16. [Cross-References](#16-cross-references)

---

## 1. Why Message Filtering Matters

### Without Filtering

```
Topic: order-events (5 subscriptions)

Every publish delivers to ALL 5 subscribers:

    Publisher: Publish(event_type="order_placed", region="us-east")
        → inventory-queue: receives ✓ (needs this)
        → email-queue:     receives ✓ (needs this)
        → fraud-queue:     receives ✓ (doesn't need this — amount < $500)
        → analytics-queue: receives ✓ (needs everything)
        → eu-queue:        receives ✓ (doesn't need this — wrong region)

    5 deliveries. 2 are wasted (fraud, eu-queue).

    Each wasteful consumer must:
    1. Receive the message (SQS API call = cost)
    2. Deserialize and check (compute = cost)
    3. Discard (delete from queue = cost)
    4. All for nothing
```

### With Filtering

```
Same topic with filter policies:

    inventory-queue: {event_type: ["order_placed", "order_cancelled"]}
    email-queue:     {event_type: ["order_placed", "order_shipped"]}
    fraud-queue:     {amount: [{numeric: [">", 500]}]}
    analytics-queue: (no filter — receives everything)
    eu-queue:        {region: ["eu-west", "eu-central"]}

    Publisher: Publish(event_type="order_placed", region="us-east", amount=99.99)
        → inventory-queue: event_type matches → DELIVER ✓
        → email-queue:     event_type matches → DELIVER ✓
        → fraud-queue:     amount 99.99 NOT > 500 → SKIP ✗
        → analytics-queue: no filter → DELIVER ✓
        → eu-queue:        region "us-east" NOT in ["eu-west","eu-central"] → SKIP ✗

    3 deliveries instead of 5. 40% reduction.
```

### The Three Savings

| Saving | Without Filtering | With Filtering |
|--------|:-----------------:|:--------------:|
| **Delivery cost** | SNS delivers to all subs | SNS skips non-matching subs |
| **Consumer compute** | Consumers process + discard irrelevant messages | Consumers only process relevant messages |
| **SQS API cost** | All queues receive/delete all messages | Fewer messages = fewer API calls |

---

## 2. Filter Policy Basics

### What Is a Filter Policy?

A filter policy is a JSON object attached to an SNS **subscription** (not topic). It defines conditions that a published message must meet for delivery to occur.

```
Subscription: inventory-queue subscribed to order-events topic
Filter policy: {"event_type": ["order_placed", "order_cancelled"]}

Meaning: Only deliver messages where the event_type attribute equals
         "order_placed" OR "order_cancelled"
```

### Setting a Filter Policy

```bash
aws sns set-subscription-attributes \
    --subscription-arn arn:aws:sns:us-east-1:123456789012:order-events:abc123 \
    --attribute-name FilterPolicy \
    --attribute-value '{"event_type": ["order_placed", "order_cancelled"]}'
```

### No Filter = Accept All

```
A subscription with no filter policy receives EVERY message published to the topic.
This is the default behavior.

To receive everything: don't set a filter policy.
To receive nothing: this is not possible — subscriptions must receive something.
                    If you want to pause delivery, unsubscribe.
```

---

## 3. Filter Policy Scope: MessageAttributes vs MessageBody

### Two Scopes

SNS filter policies can match against two different parts of the published message:

#### Scope 1: MessageAttributes (default)

Filter against structured key-value attributes sent alongside the message body.

```
Publisher:
    sns:Publish(
        Message = '{"orderId":"12345","amount":99.99}',
        MessageAttributes = {
            "event_type": { DataType: "String", StringValue: "order_placed" },
            "region":     { DataType: "String", StringValue: "us-east" },
            "amount":     { DataType: "Number", StringValue: "99.99" }
        }
    )

Filter policy (scope: MessageAttributes):
    {"event_type": ["order_placed"]}

Evaluation: Checks MessageAttributes.event_type → matches "order_placed" → deliver
```

#### Scope 2: MessageBody

Filter against properties within the JSON message body itself.

```
Publisher:
    sns:Publish(
        Message = '{"event_type":"order_placed","order":{"amount":99.99,"region":"us-east"}}'
    )

Filter policy (scope: MessageBody):
    {"event_type": ["order_placed"], "order": {"region": ["us-east"]}}

Evaluation: Checks body.event_type AND body.order.region → both match → deliver
```

### Setting the Scope

```bash
aws sns set-subscription-attributes \
    --subscription-arn arn:aws:sns:...:abc123 \
    --attribute-name FilterPolicyScope \
    --attribute-value MessageBody
```

### When to Use Each

| Use MessageAttributes When | Use MessageBody When |
|---------------------------|---------------------|
| Message body is opaque (binary, encrypted) | Don't want to maintain separate attributes |
| Want to keep filter metadata separate from payload | Message already has structured properties |
| Need maximum filter evaluation speed | Need to filter on nested JSON properties |
| Using raw message delivery to SQS | Message body is well-structured JSON |

---

## 4. String Matching Operators

### Exact Match

```json
{"event_type": ["order_placed", "order_cancelled"]}
```

Matches if `event_type` is exactly "order_placed" OR "order_cancelled".

### Prefix Match

```json
{"event_type": [{"prefix": "order_"}]}
```

Matches: "order_placed", "order_cancelled", "order_shipped", "order_anything"

### Suffix Match

```json
{"event_type": [{"suffix": "_placed"}]}
```

Matches: "order_placed", "item_placed", "booking_placed"

### Wildcard Match

```json
{"event_type": [{"wildcard": "order_*_v2"}]}
```

Matches: "order_placed_v2", "order_cancelled_v2"
Does not match: "order_placed", "order_placed_v3"

### Anything-But Match

```json
{"event_type": [{"anything-but": ["test", "debug"]}]}
```

Matches everything EXCEPT "test" and "debug".

### Anything-But with Prefix

```json
{"event_type": [{"anything-but": {"prefix": "test_"}}]}
```

Matches everything that does NOT start with "test_".
Matches: "order_placed", "prod_event"
Does not match: "test_event", "test_123"

### Anything-But with Suffix

```json
{"event_type": [{"anything-but": {"suffix": "_test"}}]}
```

Matches everything that does NOT end with "_test".

### Anything-But with Wildcard

```json
{"event_type": [{"anything-but": {"wildcard": "test_*_v*"}}]}
```

Matches everything that does NOT match the wildcard pattern.

### Equals-Ignore-Case

```json
{"region": [{"equals-ignore-case": "us-east"}]}
```

Matches: "us-east", "US-EAST", "Us-East", "US-east"

---

## 5. Numeric Matching Operators

### Exact Numeric Match

```json
{"price": [{"numeric": ["=", 100]}]}
```

Matches: price = 100, price = 1e2, price = 100.0

### Numeric Range

```json
{"price": [{"numeric": [">", 0, "<=", 500]}]}
```

Matches: 0 < price ≤ 500
Operators: `=`, `>`, `>=`, `<`, `<=`

Can combine up to two operators for a range:

```json
{"price": [{"numeric": [">=", 100, "<", 200]}]}
```

Matches: 100 ≤ price < 200

### Numeric Anything-But

```json
{"price": [{"anything-but": [100, 500]}]}
```

Matches any numeric value that is NOT 100 and NOT 500.

### Negative Values

```json
{"temperature": [{"numeric": ["<", 0]}]}
```

Matches any negative temperature.

---

## 6. Existence Matching

### Attribute Exists

```json
{"priority": [{"exists": true}]}
```

Matches any message that HAS the "priority" attribute, regardless of its value.

### Attribute Does Not Exist

```json
{"priority": [{"exists": false}]}
```

Matches any message that does NOT have the "priority" attribute.

### Use Cases

```
Exists = true:
    Route all "prioritized" messages to a priority queue
    Any message with a "priority" attribute (regardless of value) → priority-queue

Exists = false:
    Route "non-prioritized" messages to a default queue
    Any message WITHOUT a "priority" attribute → default-queue
```

---

## 7. IP Address CIDR Matching

```json
{"source_ip": [{"cidr": "10.0.0.0/24"}]}
```

Matches: "10.0.0.0", "10.0.0.1", ..., "10.0.0.255"
Does not match: "10.0.1.0", "10.1.0.0"

```json
{"source_ip": [{"cidr": "192.168.0.0/16"}]}
```

Matches any IP in the 192.168.x.x range.

Use case: Route messages from specific VPC CIDR ranges to region-specific queues.

---

## 8. AND/OR Logic

### OR: Multiple Values for Same Attribute

```json
{"event_type": ["order_placed", "order_cancelled", "order_shipped"]}
```

Logic: event_type = "order_placed" **OR** "order_cancelled" **OR** "order_shipped"

### AND: Multiple Attributes

```json
{
    "event_type": ["order_placed"],
    "region": ["us-east", "us-west"]
}
```

Logic: (event_type = "order_placed") **AND** (region = "us-east" **OR** "us-west")

### Explicit OR Operator (Across Attributes)

```json
{
    "$or": [
        {"event_type": ["order_placed"]},
        {"region": ["eu-west"]}
    ]
}
```

Logic: (event_type = "order_placed") **OR** (region = "eu-west")

This delivers the message if EITHER condition is true, even if they're different attributes.

### Complex Nested Logic

```json
{
    "$or": [
        {
            "event_type": ["order_placed"],
            "amount": [{"numeric": [">", 100]}]
        },
        {
            "priority": ["high"]
        }
    ]
}
```

Logic: ((event_type = "order_placed" **AND** amount > 100)) **OR** (priority = "high")

### Summary of Logic Rules

```
Within one attribute (multiple values):         OR
Across different attributes (default):          AND
Explicit $or operator:                          OR across attribute groups

Default behavior:
    {
        "A": ["v1", "v2"],          ← A = v1 OR v2
        "B": ["v3"]                  ← AND B = v3
    }

    = (A = v1 OR A = v2) AND (B = v3)

With $or:
    {
        "$or": [
            {"A": ["v1"]},           ← A = v1
            {"B": ["v3"]}            ← OR B = v3
        ]
    }

    = (A = v1) OR (B = v3)
```

---

## 9. Filter Policy Limits

| Limit | Value | Notes |
|-------|:-----:|-------|
| Filter policies per topic | 200 | Across all subscriptions on the topic |
| Filter policies per account | 10,000 | Across all topics in the account |
| Filter policy max size | 256 KB | The JSON filter policy document |
| Nested depth (MessageBody scope) | 5 levels | For nested JSON property matching |
| Max combinations in a policy | 150 | Product of all value arrays. E.g., 3 values × 5 values × 10 values = 150 combinations |
| Propagation delay | Up to 15 minutes | After SetSubscriptionAttributes |

### The 150 Combination Limit

```
Filter policy combination count = product of the number of values per attribute

Example:
    {
        "event_type": ["placed", "cancelled", "shipped"],    ← 3 values
        "region": ["us-east", "us-west", "eu-west"],         ← 3 values
        "priority": ["high", "low"]                           ← 2 values
    }

    Combinations: 3 × 3 × 2 = 18 ✓ (under 150)

Example that EXCEEDS limit:
    {
        "event_type": [10 values],
        "region": [5 values],
        "category": [4 values]
    }

    Combinations: 10 × 5 × 4 = 200 ✗ (exceeds 150 limit)
    → InvalidParameterException
```

---

## 10. Filter Propagation Delay — The 15-Minute Window

### What Happens During Propagation

```
t=0:00   SetSubscriptionAttributes(FilterPolicy = NEW_FILTER)
         API returns 200 OK immediately

t=0:00 to t=15:00   INCONSISTENT STATE
    Some SNS fan-out coordinators have the OLD filter
    Some have the NEW filter

    Behavior during this window:
    - Message might be evaluated against OLD filter by some coordinators
    - Same message might be evaluated against NEW filter by other coordinators
    - Depending on which coordinator handles the publish:
        * May deliver when it shouldn't (old filter was more permissive)
        * May NOT deliver when it should (old filter was more restrictive)

t=15:00  All coordinators have the new filter policy. Consistent.
```

### Why 15 Minutes?

[INFERRED — not officially documented]

The subscription metadata (including filter policies) is cached at fan-out coordinator nodes for performance. At 30,000 publishes/sec, reloading metadata from the store on every publish would be too expensive. The cache TTL (or propagation delay for distributed metadata replication) results in up to 15 minutes of inconsistency.

### Mitigations

```
1. Plan filter policy changes during low-traffic periods
2. Don't rely on filter changes taking effect immediately
3. For critical changes, consider using separate topics instead of changing filters
4. Monitor delivery counts after filter changes to verify propagation
5. If you need instant filter changes: filter at the consumer side (application-level)
```

---

## 11. Filtering Architecture — Where It Happens

### In the Fan-Out Path

```
Publisher → Publish(topic, message, attributes)
                │
                ▼
    ┌───────────────────────────┐
    │    Fan-Out Coordinator     │
    │                           │
    │  1. Read subscription list│
    │     (50,000 subs)         │
    │                           │
    │  2. For each subscription:│
    │     Load cached filter    │
    │     policy                │
    │                           │
    │  3. Evaluate filter       │◄── This is where filtering happens
    │     against message       │
    │     attributes/body       │
    │                           │
    │  4. If match: include in  │
    │     delivery task         │
    │     If no match: SKIP     │
    │                           │
    │  Result:                  │
    │     50,000 subs evaluated │
    │     15,000 match          │
    │     35,000 skipped        │
    │                           │
    │  5. Partition 15,000      │
    │     matching subs into    │
    │     delivery tasks        │
    └───────────────────────────┘
                │
                ▼
    Delivery Workers (only 15,000 deliveries, not 50,000)
```

### Filter Evaluation Cost

```
Filter evaluation per subscription:
    - Parse filter policy JSON: cached (done once, not per message)
    - Compare attributes/body against policy: microseconds
    - Per-subscription cost: ~1-10 microseconds [INFERRED]

Total for 50,000 subscriptions:
    50,000 × 5μs = 250ms

Compare with delivery cost per subscription:
    SQS delivery: ~5ms (network I/O)
    HTTP delivery: ~50ms-30s (external call)

Filter eval is 1000× cheaper than delivery.
Filtering 35,000 subs saves 35,000 × 5ms = 175 seconds of SQS delivery time.
The 250ms filter evaluation cost is negligible.
```

---

## 12. Cost Savings from Filtering

### Scenario: E-Commerce Platform

```
Topic: platform-events
Subscriptions: 100 SQS queues (100 microservices)
Publish rate: 500,000 messages/day
Average filter match rate: 30% (each queue only cares about ~30% of events)

WITHOUT filtering:
    Deliveries/day: 500,000 × 100 = 50,000,000
    SQS receives:   50,000,000 (each queue processes everything)
    SQS deletes:    50,000,000
    SQS API calls:  100,000,000 (receive + delete)

    Consumer compute: 100 services × 500,000 messages = processing 50M messages
    70% are discarded after processing → 35M wasted processing operations

WITH filtering:
    Deliveries/day: 500,000 × 100 × 0.30 = 15,000,000
    SQS receives:   15,000,000
    SQS deletes:    15,000,000
    SQS API calls:  30,000,000 (receive + delete)

    Consumer compute: each service processes only relevant messages
    0% wasted processing

SAVINGS:
    SQS API calls: 70M fewer/day × 30 days = 2.1B fewer/month
    Cost: 2.1B × $0.40/M = $840/month saved on SQS alone
    Plus: 70% less consumer compute (fewer Lambda invocations, smaller EC2 fleet)
```

---

## 13. Filtering + FIFO Topics

### The At-Most-Once Caveat (Revisited)

```
FIFO topic with filter policy:

    Standard (no filter): exactly-once delivery guaranteed
    With filter:          filtered-out messages are NEVER delivered
                          = at-most-once for filtered messages

    This is by design — filtering means "don't send me this message."
    But be aware:
    - If filter policy is wrong → you lose messages permanently
    - During the 15-minute propagation window → unpredictable filtering
    - Content-based dedup + filtering can have surprising interactions
```

### Recommendation for FIFO Topics

```
Option 1: No filtering on FIFO topics (safest)
    Accept all messages at every subscriber
    Filter at the consumer application level
    → Guarantees exactly-once for all messages

Option 2: Use filtering carefully
    Test filter policies thoroughly before deploying
    Use separate FIFO topics when possible instead of filters
    Monitor delivery counts to detect filtering issues
```

---

## 14. Common Filtering Patterns

### Pattern 1: Event Type Routing

```
Topic: platform-events

    Order service queue:
        {"event_type": ["order.placed", "order.cancelled", "order.shipped"]}

    Payment service queue:
        {"event_type": ["order.placed", "payment.failed"]}

    Notification service queue:
        {"event_type": [{"prefix": "order."}, {"prefix": "alert."}]}
```

### Pattern 2: Geographic Routing

```
Topic: global-events

    US queue:     {"region": ["us-east-1", "us-west-2"]}
    EU queue:     {"region": [{"prefix": "eu-"}]}
    APAC queue:   {"region": [{"prefix": "ap-"}]}
```

### Pattern 3: Priority-Based Routing

```
Topic: task-events

    High-priority queue (fast processing):
        {"priority": ["critical", "high"]}

    Default queue (normal processing):
        {"priority": [{"anything-but": ["critical", "high"]}]}

    Or using exists:
    High-priority queue: {"priority": [{"exists": true}]}
    Default queue:       {"priority": [{"exists": false}]}
```

### Pattern 4: Amount-Based Routing

```
Topic: transaction-events

    Fraud review queue:
        {"amount": [{"numeric": [">", 10000]}]}

    Standard processing queue:
        {"amount": [{"numeric": [">=", 0, "<=", 10000]}]}

    Refund processing queue:
        {"amount": [{"numeric": ["<", 0]}]}
```

### Pattern 5: Multi-Tenant Routing

```
Topic: saas-events

    Tenant A queue: {"tenant_id": ["tenant-A"]}
    Tenant B queue: {"tenant_id": ["tenant-B"]}
    Admin queue:    (no filter — receives everything)
    Audit queue:    {"event_type": [{"suffix": ".deleted"}]}
```

### Pattern 6: Combining OR Across Attributes

```
Topic: alerts

    On-call queue (receives critical alerts OR any database event):
    {
        "$or": [
            {"severity": ["critical"]},
            {"source": [{"prefix": "database."}]}
        ]
    }
```

---

## 15. Anti-Patterns

### Anti-Pattern 1: Filtering at the Consumer Instead of SNS

```
BAD:
    No filter policy on subscription
    Consumer code:
        msg = sqs.receiveMessage()
        if msg.attributes["event_type"] != "order_placed":
            sqs.deleteMessage(msg)  # Discard
            return
        process(msg)

    Result: Consumer receives and discards 70% of messages.
    Wasted SQS API calls, wasted compute.

GOOD:
    Filter policy: {"event_type": ["order_placed"]}
    Consumer code:
        msg = sqs.receiveMessage()
        process(msg)  # Every message is relevant

    Result: Consumer only receives messages it needs.
```

### Anti-Pattern 2: Too Many Filter Policy Changes

```
BAD:
    Changing filter policies frequently (e.g., every minute)
    During 15-minute propagation window, behavior is unpredictable

GOOD:
    Treat filter policies as semi-static configuration
    Change during low-traffic periods
    If you need dynamic routing, use EventBridge (faster rule updates)
```

### Anti-Pattern 3: Exceeding the 150 Combination Limit

```
BAD:
    {
        "color": ["red", "blue", "green", "yellow", "purple"],    ← 5
        "size": ["S", "M", "L", "XL", "XXL"],                    ← 5
        "category": ["shirt", "pants", "jacket", "hat", "shoes",
                     "socks", "belt"]                              ← 7
    }
    Combinations: 5 × 5 × 7 = 175 ✗ (exceeds 150)

GOOD:
    Split into multiple subscriptions with simpler filters:
    Subscription 1: {"category": ["shirt", "pants", "jacket"], "color": ["red", "blue"]}
    Subscription 2: {"category": ["hat", "shoes", "socks", "belt"]}
```

### Anti-Pattern 4: Using Filter Policy as Access Control

```
BAD:
    Using filter policies to prevent unauthorized access to messages
    Filter policies are NOT a security mechanism
    They can be changed by anyone with sns:SetSubscriptionAttributes permission
    During propagation delay, messages may leak

GOOD:
    Use IAM policies and topic/queue policies for access control
    Use filter policies for routing optimization only
```

---

## 16. Cross-References

| Topic | Document |
|-------|----------|
| Fan-out engine (where filtering happens) | [fan-out-engine.md](fan-out-engine.md) |
| Delivery retry policies | [delivery-and-retries.md](delivery-and-retries.md) |
| SNS + SQS fan-out pattern | [sns-sqs-fanout.md](sns-sqs-fanout.md) |
| FIFO topics (filtering caveat) | [fifo-topics.md](fifo-topics.md) |
| Mobile push delivery | [mobile-push.md](mobile-push.md) |
| Interview simulation | [interview-simulation.md](interview-simulation.md) |

### AWS Documentation References

- [Amazon SNS subscription filter policies](https://docs.aws.amazon.com/sns/latest/dg/sns-subscription-filter-policies.html)
- [Amazon SNS string value matching](https://docs.aws.amazon.com/sns/latest/dg/string-value-matching.html)
- [Amazon SNS numeric value matching](https://docs.aws.amazon.com/sns/latest/dg/numeric-value-matching.html)
- [Amazon SNS filter policy constraints](https://docs.aws.amazon.com/sns/latest/dg/subscription-filter-policy-constraints.html)
