# Amazon SNS — Delivery Engine & Retry Policies Deep Dive

> Companion deep dive to the [interview simulation](interview-simulation.md). This document covers how SNS delivers messages to each protocol type, the two-tier retry model, custom HTTP delivery policies, dead letter queues, and delivery status monitoring.

---

## Table of Contents

1. [Protocol-Specific Delivery Overview](#1-protocol-specific-delivery-overview)
2. [SQS Delivery — The Fast Path](#2-sqs-delivery--the-fast-path)
3. [Lambda Delivery](#3-lambda-delivery)
4. [HTTP/S Delivery — The Complex Path](#4-https-delivery--the-complex-path)
5. [Email and Email-JSON Delivery](#5-email-and-email-json-delivery)
6. [SMS Delivery](#6-sms-delivery)
7. [Mobile Push Delivery](#7-mobile-push-delivery)
8. [Firehose Delivery](#8-firehose-delivery)
9. [The Two-Tier Retry Model](#9-the-two-tier-retry-model)
10. [Custom HTTP/S Delivery Policies](#10-custom-https-delivery-policies)
11. [Backoff Functions — The Four Algorithms](#11-backoff-functions--the-four-algorithms)
12. [Dead Letter Queues](#12-dead-letter-queues)
13. [Client-Side vs Server-Side Errors](#13-client-side-vs-server-side-errors)
14. [Delivery Status Logging](#14-delivery-status-logging)
15. [Delivery Performance and Throttling](#15-delivery-performance-and-throttling)
16. [Common Delivery Failure Patterns](#16-common-delivery-failure-patterns)
17. [Cross-References](#17-cross-references)

---

## 1. Protocol-Specific Delivery Overview

SNS delivers to 8 different endpoint types. Each has fundamentally different delivery mechanics:

| Protocol | Delivery Mechanism | Typical Latency | Reliability | Rate Limit |
|----------|-------------------|:---------------:|:-----------:|:----------:|
| **SQS** | Internal `sqs:SendMessage` | < 5ms | Very high | N/A (SQS absorbs) |
| **Lambda** | Internal async `lambda:Invoke` | < 10ms | High | Lambda concurrency limits |
| **HTTP/S** | HTTP POST to customer endpoint | 50ms - 30s | Variable | Custom `maxReceivesPerSecond` |
| **Email** | SMTP via AWS relay | Seconds - minutes | Medium | 10 msg/sec per subscription |
| **Email-JSON** | SMTP via AWS relay | Seconds - minutes | Medium | 10 msg/sec per subscription |
| **SMS** | Carrier gateway | Seconds | Low-medium | 20 msg/sec |
| **Mobile Push** | APNs/FCM/ADM/WNS/MPNS/Baidu | Seconds | Medium | Platform-dependent |
| **Firehose** | Internal `firehose:PutRecord` | < 10ms | Very high | Firehose throughput limits |

### The Two Categories

```
AWS-Managed Endpoints (fast, reliable):
    SQS, Lambda, Firehose
    → Retry: 100,015 attempts over 23 days
    → Delivery via internal AWS APIs (no public network)
    → Almost always succeed on first attempt

Customer-Managed Endpoints (slow, variable):
    HTTP/S, Email, SMS, Mobile Push
    → Retry: 50 attempts over 6 hours
    → Delivery via external networks/gateways
    → Failure is common (endpoints down, rate limited, unreachable)
```

---

## 2. SQS Delivery — The Fast Path

### How It Works

```
SNS Delivery Worker
  │
  │  Internal API call: sqs:SendMessage
  │  (NOT over public internet — internal AWS network)
  │
  ▼
┌─────────────────────────────────────────┐
│  SQS Queue                               │
│                                          │
│  Message body: SNS JSON envelope         │
│  (or raw message body if raw delivery)   │
│                                          │
│  Message attributes: SNS → SQS mapping   │
│  (only with raw delivery, max 10)        │
└─────────────────────────────────────────┘
```

### Message Format — SQS Receives

**Without raw message delivery (default):**
```json
{
    "Type": "Notification",
    "MessageId": "dc1e94d9-56c5-5e96-808d-cc7f68faa162",
    "TopicArn": "arn:aws:sns:us-east-1:123456789012:my-topic",
    "Subject": "Order Update",
    "Message": "{\"orderId\": \"12345\", \"status\": \"shipped\"}",
    "Timestamp": "2026-02-13T10:30:00.000Z",
    "SignatureVersion": "1",
    "Signature": "EXAMPLEpH+...",
    "SigningCertURL": "https://sns.us-east-1.amazonaws.com/SimpleNotificationService-...",
    "UnsubscribeURL": "https://sns.us-east-1.amazonaws.com/?Action=Unsubscribe&...",
    "MessageAttributes": {
        "event_type": {
            "Type": "String",
            "Value": "order_shipped"
        }
    }
}
```

The actual business payload is **inside the `Message` field** as an escaped JSON string. The SQS consumer must:
1. Parse the outer SNS JSON envelope
2. Extract the `Message` field
3. Parse the inner JSON

**With raw message delivery:**
```
{\"orderId\": \"12345\", \"status\": \"shipped\"}
```

Just the raw message body. Message attributes become SQS message attributes (max 10).

### Why SQS Is the Most Reliable Subscriber

1. **SQS always accepts** — SQS queues are designed to absorb unlimited writes. It's extremely rare for a `SendMessage` to fail.
2. **Internal API** — No public network traversal. Sub-millisecond routing within the AWS datacenter.
3. **No authentication complexity** — SNS uses internal service-to-service auth (not SigV4 over HTTPS).
4. **Natural buffer** — Even if the downstream consumer of the SQS queue is slow, SQS buffers messages for up to 14 days.

### Cross-Account SQS Delivery

```
SNS Topic:  Account A, us-east-1
SQS Queue:  Account B, us-east-1

Requirements:
    1. SQS queue policy must allow sns:SendMessage from the topic ARN
    2. SNS topic must allow the subscription

    Queue policy example:
    {
        "Statement": [{
            "Effect": "Allow",
            "Principal": {"Service": "sns.amazonaws.com"},
            "Action": "sqs:SendMessage",
            "Resource": "arn:aws:sqs:us-east-1:222222222222:my-queue",
            "Condition": {
                "ArnEquals": {
                    "aws:SourceArn": "arn:aws:sns:us-east-1:111111111111:my-topic"
                }
            }
        }]
    }
```

---

## 3. Lambda Delivery

### How It Works

```
SNS Delivery Worker
  │
  │  Internal API call: lambda:Invoke (async invocation)
  │  InvocationType: Event (fire-and-forget from SNS's perspective)
  │
  ▼
┌─────────────────────────────────────────┐
│  Lambda Service                          │
│                                          │
│  1. Accepts invocation                   │
│  2. Queues internally                    │
│  3. Invokes function when capacity       │
│     available                            │
│  4. If function fails, Lambda retries    │
│     (Lambda's own retry, separate from   │
│     SNS retry)                           │
└─────────────────────────────────────────┘
```

### Lambda Event Payload

```json
{
    "Records": [
        {
            "EventSource": "aws:sns",
            "EventVersion": "1.0",
            "EventSubscriptionArn": "arn:aws:sns:us-east-1:123456789012:my-topic:abc123",
            "Sns": {
                "Type": "Notification",
                "MessageId": "dc1e94d9-56c5-5e96-808d-cc7f68faa162",
                "TopicArn": "arn:aws:sns:us-east-1:123456789012:my-topic",
                "Subject": "Order Update",
                "Message": "{\"orderId\": \"12345\"}",
                "Timestamp": "2026-02-13T10:30:00.000Z",
                "SignatureVersion": "1",
                "Signature": "EXAMPLEpH+...",
                "SigningCertUrl": "https://sns.us-east-1.amazonaws.com/...",
                "UnsubscribeUrl": "https://sns.us-east-1.amazonaws.com/?Action=Unsubscribe&...",
                "MessageAttributes": {
                    "event_type": {
                        "Type": "String",
                        "Value": "order_shipped"
                    }
                }
            }
        }
    ]
}
```

### Double Retry Behavior

Lambda has its own retry mechanism in addition to SNS's retry:

```
SNS delivers to Lambda (async invocation):
    │
    ├── Lambda accepts the event ← SNS considers this "delivered" ✓
    │   │
    │   ├── Lambda invokes function → SUCCESS ✓
    │   │
    │   ├── Lambda invokes function → FAILURE
    │   │   └── Lambda retries (Lambda's own retry: 2 attempts by default)
    │   │       ├── Retry 1 → SUCCESS ✓
    │   │       └── Retry 2 → FAILURE → Lambda's DLQ (if configured)
    │   │
    │   └── Lambda throttled (concurrency limit) → Lambda queues internally
    │       └── Lambda retries for up to 6 hours
    │
    └── Lambda rejects the event (throttled at accept time)
        └── SNS retries (100,015 attempts over 23 days)
```

**Key insight**: Once Lambda accepts the async invocation, SNS considers delivery complete. Lambda's own retry and DLQ mechanisms take over. This means a Lambda subscriber effectively has TWO DLQ opportunities:
1. **SNS DLQ** — if SNS can't even deliver to Lambda
2. **Lambda DLQ** — if Lambda accepts but the function fails

---

## 4. HTTP/S Delivery — The Complex Path

HTTP/S is the most failure-prone and configurable delivery protocol.

### How It Works

```
SNS Delivery Worker
  │
  │  HTTP POST to subscriber's endpoint URL
  │  Content-Type: text/plain; charset=UTF-8 (default)
  │                application/json (configurable)
  │
  │  Headers:
  │    x-amz-sns-message-type: Notification
  │    x-amz-sns-message-id: <MessageId>
  │    x-amz-sns-topic-arn: <TopicArn>
  │    x-amz-sns-subscription-arn: <SubscriptionArn>
  │
  ▼
┌─────────────────────────────────────────┐
│  Customer HTTP Endpoint                  │
│  (public internet)                       │
│                                          │
│  Must return HTTP 2xx within timeout     │
│  to confirm receipt                      │
└─────────────────────────────────────────┘
```

### Success and Failure Responses

| HTTP Status | SNS Interpretation |
|:-----------:|-------------------|
| 200-299 | **Success** — Message delivered, no retry |
| 429 | **Throttled** — Retryable. SNS backs off. |
| 400-428, 430-499 | **Client error** — Non-retryable. Delivery fails permanently. Goes to DLQ if configured. |
| 500-599 | **Server error** — Retryable. SNS uses retry policy. |
| Timeout | **Retryable** — Treated like a server error. |
| Connection refused | **Retryable** — Endpoint unreachable. |

### Subscription Confirmation

HTTP/S subscriptions require explicit confirmation before they receive messages:

```
1. Subscribe(TopicArn, Protocol="https", Endpoint="https://example.com/sns")

2. SNS sends a confirmation request to the endpoint:
   POST https://example.com/sns
   {
       "Type": "SubscriptionConfirmation",
       "MessageId": "...",
       "Token": "2336412f37f...",
       "TopicArn": "arn:aws:sns:...:my-topic",
       "SubscribeURL": "https://sns.us-east-1.amazonaws.com/?Action=ConfirmSubscription&..."
   }

3. Endpoint must call the SubscribeURL (or call ConfirmSubscription API with the token)

4. Only after confirmation does the endpoint receive Notification messages
```

This prevents SNS from being used to DOS arbitrary HTTP endpoints.

---

## 5. Email and Email-JSON Delivery

### Email Protocol

```
SNS → SMTP relay → recipient's mail server → inbox

Message format: Plain text
    Subject: <SNS Subject field or topic name>
    Body:    <Message body>
    Footer:  Unsubscribe link

Rate limit: 10 messages/sec per subscription (hard limit)
```

### Email-JSON Protocol

```
Same delivery mechanism, but body is the full SNS JSON envelope:

{
    "Type": "Notification",
    "MessageId": "...",
    "TopicArn": "...",
    "Subject": "...",
    "Message": "...",
    "Timestamp": "...",
    ...
}
```

### Email Subscription Confirmation

Email subscriptions also require opt-in confirmation:

```
1. Subscribe(TopicArn, Protocol="email", Endpoint="user@example.com")
2. SNS sends confirmation email with a link
3. User clicks the link to confirm
4. Only then do notifications flow
```

This is a regulatory requirement (CAN-SPAM, GDPR) — you cannot subscribe arbitrary email addresses without consent.

---

## 6. SMS Delivery

```
SNS → AWS SMS gateway → carrier network → mobile device

Message format: Plain text (message body only, no JSON envelope)
Max length:     140 characters (standard SMS) or up to 1600 chars (multi-part)
Rate limit:     20 messages/sec (account-level spending limit may also apply)

Cost varies dramatically by country:
    US:     ~$0.00645/message
    India:  ~$0.0200/message
    Japan:  ~$0.0448/message
    UK:     ~$0.0400/message
```

### SMS Types

| Type | Behavior | Use Case |
|------|----------|----------|
| **Promotional** | Can be throttled or filtered by carriers | Marketing messages |
| **Transactional** | Higher priority, less likely filtered | OTPs, alerts, confirmations |

### SMS Limitations

- No delivery guarantee — carriers can silently drop messages
- No retry for carrier-level failures (only SNS-level retries for gateway errors)
- Opt-out management via STOP keyword
- Spending limits per account (default $1/month, raise via support)

---

## 7. Mobile Push Delivery

```
SNS → Platform Service (APNs/FCM/ADM/WNS/MPNS/Baidu) → Mobile Device

Message format: Platform-specific JSON payload
Latency:        Seconds (depends on platform and device connectivity)
```

### Supported Platforms

| Platform | Service | Devices |
|----------|---------|---------|
| APNs | Apple Push Notification service | iOS, macOS |
| FCM | Firebase Cloud Messaging | Android, iOS, Web |
| ADM | Amazon Device Messaging | Kindle Fire |
| WNS | Windows Push Notification Service | Windows |
| MPNS | Microsoft Push Notification Service | Windows Phone (legacy) |
| Baidu | Baidu Cloud Push | Android (China) |

### Multi-Platform Message Format

```json
{
    "default": "Default notification message",
    "APNS": "{\"aps\":{\"alert\":\"iOS notification\"}}",
    "APNS_SANDBOX": "{\"aps\":{\"alert\":\"iOS dev notification\"}}",
    "GCM": "{\"notification\":{\"title\":\"Android\",\"body\":\"Android notification\"}}"
}
```

SNS selects the right payload based on the platform endpoint's type.

### Device Token Lifecycle

```
1. App installs → device registers with platform (APNs/FCM)
2. Platform returns device token
3. App sends token to your server
4. Server creates SNS Platform Endpoint (CreatePlatformEndpoint)
5. SNS uses token to push via platform service

Token invalidation:
    - User uninstalls app → token becomes stale
    - Platform rotates token (APNs does this periodically)
    - SNS detects stale token from platform error → disables endpoint
    - Must re-register with new token on next app launch

Problem: Stale tokens waste delivery attempts until detected
```

---

## 8. Firehose Delivery

```
SNS → Internal firehose:PutRecord → Firehose Delivery Stream → S3/Redshift/OpenSearch

Message format: SNS JSON envelope (or raw with raw delivery)
Latency:        < 10ms to Firehose accept
Reliability:    Very high (AWS-managed endpoint)
```

Firehose delivery enables streaming SNS messages directly to data lakes and analytics systems without an intermediate SQS queue.

**Note**: For Firehose throttling errors, SNS uses the **customer-managed retry policy** (50 attempts/6 hours), not the AWS-managed policy. This is an exception to the general rule.

---

## 9. The Two-Tier Retry Model

This is the most important section of this document. SNS uses fundamentally different retry behavior for AWS-managed vs customer-managed endpoints.

### Tier 1: AWS-Managed Endpoints (SQS, Lambda, Firehose)

```
Phase 1: Immediate retry        3 attempts, no delay
Phase 2: Pre-backoff            2 attempts, 1 second apart
Phase 3: Backoff                10 attempts, exponential 1-20 seconds
Phase 4: Post-backoff           100,000 attempts, 20 seconds apart

Total: 100,015 attempts over approximately 23 days
```

```
Timeline visualization:

  0s        1s    2s     12s              ~23 days
  │─────────│─────│──────│───────────────────│
  ▲▲▲       ▲ ▲   ▲▲▲▲▲▲▲▲▲▲             ▲ (final attempt)
  │         │     │                        │
  Immediate Pre-  Backoff                  Post-backoff
  (3)       backoff (exponential,          (100,000 attempts,
            (2)    10 attempts)             20s apart)
```

**Why so aggressive?** SQS and Lambda are almost always available. A failure is nearly always a brief transient issue (AZ hiccup, momentary overload). The 23-day retry window means SNS will keep trying through even multi-day AWS outages.

### Tier 2: Customer-Managed Endpoints (HTTP/S, Email, SMS, Mobile Push)

```
Phase 1: Immediate retry        0 attempts (no immediate retry)
Phase 2: Pre-backoff            2 attempts, 10 seconds apart
Phase 3: Backoff                10 attempts, exponential 10-600 seconds
Phase 4: Post-backoff           38 attempts, 600 seconds (10 min) apart

Total: 50 attempts over approximately 6 hours
```

```
Timeline visualization:

  0s     10s   20s       ~90min              ~6 hours
  │──────│─────│─────────│───────────────────│
  ▲      ▲     ▲  ▲▲▲▲▲▲▲▲▲               ▲ (final attempt)
  │      │       │                          │
  First  Pre-    Backoff                    Post-backoff
  attempt backoff (exponential,             (38 attempts,
          (2)    10 attempts)                10 min apart)
```

**Why more conservative?** Customer HTTP endpoints might be down for extended periods. Hammering them with 100,000 retries would waste resources and could worsen their condition (retry storm). The 6-hour window gives reasonable recovery time without indefinite retry.

### Side-by-Side Comparison

| Aspect | AWS-Managed | Customer-Managed |
|--------|:-----------:|:----------------:|
| Total attempts | 100,015 | 50 |
| Total duration | ~23 days | ~6 hours |
| Immediate retries | 3 | 0 |
| Pre-backoff retries | 2 (1s apart) | 2 (10s apart) |
| Backoff retries | 10 (1-20s) | 10 (10-600s) |
| Post-backoff retries | 100,000 (20s apart) | 38 (600s apart) |
| Custom policy? | No | HTTP/S only |

### Jitter

SNS applies **jitter** to all retry delays. Instead of all retries for a failed endpoint firing at exactly the same interval, each retry has a random offset. This prevents the thundering herd problem where thousands of retries converge on the same second.

```
Without jitter:
    Retry 1: t=10s
    Retry 2: t=20s
    Retry 3: t=40s
    → If 1000 subscriptions fail at t=0, all retry at t=10, 20, 40...
    → Thundering herd

With jitter:
    Retry 1: t=10s + random(0, 5s) = ~12.3s
    Retry 2: t=20s + random(0, 10s) = ~24.7s
    Retry 3: t=40s + random(0, 20s) = ~51.2s
    → Retries spread out, no thundering herd
```

---

## 10. Custom HTTP/S Delivery Policies

HTTP/S is the **only** protocol that supports custom delivery policies. All other protocols use the fixed AWS-defined policies above.

### Full Policy JSON Structure

```json
{
    "healthyRetryPolicy": {
        "minDelayTarget": 1,
        "maxDelayTarget": 60,
        "numRetries": 50,
        "numNoDelayRetries": 3,
        "numMinDelayRetries": 2,
        "numMaxDelayRetries": 35,
        "backoffFunction": "exponential"
    },
    "throttlePolicy": {
        "maxReceivesPerSecond": 10
    },
    "requestPolicy": {
        "headerContentType": "application/json"
    }
}
```

### Parameter Reference

#### Healthy Retry Policy

| Parameter | Default | Range | Description |
|-----------|:-------:|-------|-------------|
| `minDelayTarget` | 20 | 1 to maxDelay | Minimum retry delay (seconds). Used in pre-backoff phase. |
| `maxDelayTarget` | 20 | minDelay to 3600 | Maximum retry delay (seconds). Used in post-backoff phase. |
| `numRetries` | 3 | 0-100 | Total retries across ALL phases |
| `numNoDelayRetries` | 0 | 0+ | Immediate retries with no delay |
| `numMinDelayRetries` | 0 | 0+ | Pre-backoff retries with `minDelayTarget` delay |
| `numMaxDelayRetries` | 0 | 0+ | Post-backoff retries with `maxDelayTarget` delay |
| `backoffFunction` | linear | 4 options | Algorithm for the backoff phase |

**Hard limit**: Total retry time cannot exceed **3,600 seconds (1 hour)** for HTTP/S.

#### How Retry Phases Are Calculated

```
Total retries = numNoDelayRetries + numMinDelayRetries + [backoff retries] + numMaxDelayRetries

Where [backoff retries] = numRetries - numNoDelayRetries - numMinDelayRetries - numMaxDelayRetries

Example with the policy above:
    numRetries = 50
    numNoDelayRetries = 3
    numMinDelayRetries = 2
    numMaxDelayRetries = 35
    backoff retries = 50 - 3 - 2 - 35 = 10

    Phase 1: 3 immediate retries
    Phase 2: 2 retries at 1s delay (minDelayTarget)
    Phase 3: 10 retries with exponential backoff from 1s to 60s
    Phase 4: 35 retries at 60s delay (maxDelayTarget)
```

#### Throttle Policy

| Parameter | Default | Description |
|-----------|:-------:|-------------|
| `maxReceivesPerSecond` | No limit | Maximum average delivery rate to this subscription |

**Important**: This is an average rate, not a strict cap. Brief spikes above the limit may occur. But over time, SNS maintains the average.

#### Request Policy

| Parameter | Default | Description |
|-----------|:-------:|-------------|
| `headerContentType` | `text/plain; charset=UTF-8` | Content-Type header on the POST |

Supported content types depend on whether raw message delivery is enabled:
- **Raw delivery disabled**: `application/json`, `text/plain`
- **Raw delivery enabled**: Wide range including `text/xml`, `application/octet-stream`, `text/html`, etc.

### Policy Scope: Topic-Level vs Subscription-Level

```
Topic-level delivery policy:
    Applies to ALL HTTP/S subscriptions on the topic.
    Set via SetTopicAttributes(AttributeName="DeliveryPolicy")

Subscription-level delivery policy:
    Applies to ONE specific subscription.
    Overrides topic-level policy for that subscription.
    Set via SetSubscriptionAttributes(AttributeName="DeliveryPolicy")

Precedence: Subscription-level > Topic-level > AWS default
```

### Example: Aggressive Retry for Critical Webhook

```json
{
    "healthyRetryPolicy": {
        "minDelayTarget": 5,
        "maxDelayTarget": 300,
        "numRetries": 80,
        "numNoDelayRetries": 5,
        "numMinDelayRetries": 5,
        "numMaxDelayRetries": 40,
        "backoffFunction": "exponential"
    },
    "throttlePolicy": {
        "maxReceivesPerSecond": 50
    }
}

Timeline:
    5 immediate retries (0s)
    5 retries at 5s delay
    30 retries exponential 5s → 300s  (backoff = 80 - 5 - 5 - 40 = 30)
    40 retries at 300s (5 min) delay
    Total: ~80 retries over ~3.5 hours
```

### Example: Gentle Retry for Non-Critical Endpoint

```json
{
    "healthyRetryPolicy": {
        "minDelayTarget": 30,
        "maxDelayTarget": 600,
        "numRetries": 10,
        "numNoDelayRetries": 1,
        "numMinDelayRetries": 2,
        "numMaxDelayRetries": 4,
        "backoffFunction": "linear"
    },
    "throttlePolicy": {
        "maxReceivesPerSecond": 5
    }
}

Timeline:
    1 immediate retry
    2 retries at 30s
    3 retries linear 30s → 600s  (backoff = 10 - 1 - 2 - 4 = 3)
    4 retries at 600s (10 min)
    Total: ~10 retries over ~45 minutes
```

---

## 11. Backoff Functions — The Four Algorithms

SNS supports four backoff functions for the backoff phase. The choice affects how quickly delay increases from `minDelayTarget` to `maxDelayTarget`.

### Visual Comparison

```
Delay
(sec)
  │
600│                              ┌──── exponential
  │                         ┌────┘
  │                    ┌────┘
  │               ┌────┘
  │          ┌────┘───────────────── geometric
  │     ┌────┘
  │┌────┘────────────────────────── arithmetic
  ││────────────────────────────── linear
  │
  └──────────────────────────────── retry attempt →
    1   2   3   4   5   6   7   8   9  10
```

### The Four Algorithms

| Function | Growth | Formula (approximate) | Best For |
|----------|--------|----------------------|----------|
| **linear** | Slowest, constant increment | delay = min + (max-min) × (i/n) | Endpoints with predictable recovery time |
| **arithmetic** | Moderate | delay = min + (max-min) × (i/n)² | General purpose |
| **geometric** | Moderate-fast | delay = min × (max/min)^(i/n) | Endpoints that usually recover quickly but sometimes take long |
| **exponential** | Fastest | delay = min × e^(ln(max/min) × i/n) | Endpoints that may be down for extended periods |

Where `i` is the current retry attempt and `n` is the total backoff retries.

### How to Choose

```
exponential:
    → Best for unreliable endpoints (customer HTTP, external services)
    → Quickly backs off to max delay
    → Minimizes wasted retries on a down endpoint

linear:
    → Best for endpoints with known recovery time
    → Steady, predictable retry spacing
    → More retries at shorter intervals

arithmetic / geometric:
    → Middle ground between linear and exponential
    → Rarely used in practice; exponential is the default recommendation
```

---

## 12. Dead Letter Queues

### What Is a DLQ in SNS Context?

A Dead Letter Queue (DLQ) is an **SQS queue** attached to an **SNS subscription** that captures messages that could not be delivered after all retries are exhausted.

```
Key distinction:
    SNS DLQ is per SUBSCRIPTION, not per topic.

    Topic: "order-events"
        Sub A: SQS queue → DLQ-A (SQS queue)
        Sub B: Lambda    → DLQ-B (SQS queue)
        Sub C: HTTP      → DLQ-C (SQS queue)

    If Sub C's HTTP endpoint is down:
        Sub C's messages go to DLQ-C
        Sub A and Sub B are unaffected
        DLQ-A and DLQ-B remain empty
```

### Configuration — Redrive Policy

```json
{
    "deadLetterTargetArn": "arn:aws:sqs:us-east-1:123456789012:MyDeadLetterQueue"
}
```

Set via: `SetSubscriptionAttributes(AttributeName="RedrivePolicy")`

### Requirements

| Requirement | Detail |
|-------------|--------|
| Same account | DLQ and subscription must be in the same AWS account |
| Same region | DLQ and subscription must be in the same region |
| Queue type match | Standard topic subscription → standard SQS DLQ. FIFO topic subscription → FIFO SQS DLQ. |
| SQS permissions | SQS queue policy must allow `sqs:SendMessage` from `sns.amazonaws.com` |
| KMS (if encrypted) | Must use custom KMS key (not AWS-managed). KMS key policy must grant SNS service principal access. |
| Retention | Recommended: set SQS queue retention to 14 days (maximum) |

### What Ends Up in the DLQ

Two paths lead to the DLQ:

#### Path 1: Server-Side Error → Retries Exhausted → DLQ

```
SNS delivers to HTTP endpoint → 500 error
    → Retry 1 → 500
    → Retry 2 → 500
    → ... (50 retries over 6 hours) ...
    → Retry 50 → 500
    → ALL RETRIES EXHAUSTED
    → Send to DLQ ✓

DLQ message contains the original SNS message
```

#### Path 2: Client-Side Error → Immediately to DLQ

```
SNS delivers to Lambda → Lambda function deleted (404)
    → Client-side error: no retries
    → Immediately sent to DLQ ✓

SNS delivers to SQS → SQS queue policy denies access (403)
    → Client-side error: no retries
    → Immediately sent to DLQ ✓
```

### What If No DLQ Is Configured?

```
If no DLQ:
    Server-side error → retries exhausted → MESSAGE LOST
    Client-side error → MESSAGE LOST

    The only trace is CloudWatch metrics:
        NumberOfNotificationsFailed

    This is a major operational risk.
    ALWAYS configure DLQs for important subscriptions.
```

### DLQ Message Metadata

When SNS sends a message to the DLQ, it includes metadata as SQS message attributes:

| Attribute | Value |
|-----------|-------|
| `RequestID` | The original SNS Publish request ID |
| `ErrorCode` | The error code from the failed delivery |
| `ErrorMessage` | Human-readable error description |
| `TopicArn` | The source topic ARN |
| `SubscriptionArn` | The subscription that failed delivery |
| `NumberOfRetries` | How many retries were attempted |

### Monitoring DLQs

```
CloudWatch Alarm:
    Metric:    ApproximateNumberOfMessagesVisible
    Queue:     Your DLQ
    Threshold: 1 (any message means something failed)
    Action:    SNS notification to ops team

    DO NOT use NumberOfMessagesSent — it doesn't capture all failure scenarios.

    Use ApproximateNumberOfMessagesVisible:
    - Includes messages from failed deliveries
    - Updates in near-real-time
    - Works even if messages are sent very infrequently
```

### Processing DLQ Messages

```
Option 1: Lambda-based automatic drain
    Set DLQ as Lambda event source → Lambda automatically processes messages
    → Investigate, fix, and republish

Option 2: Manual investigation
    Use SQS console or CLI to view messages
    Examine error metadata to determine root cause
    Fix the subscription endpoint
    Republish messages or redrive
```

---

## 13. Client-Side vs Server-Side Errors

This distinction is critical because it determines whether SNS retries or sends directly to DLQ.

### Client-Side Errors (No Retry)

```
Client-side errors indicate the request is fundamentally wrong — retrying won't help.

Examples:
    - Endpoint deleted (Lambda function removed, SQS queue deleted)
    - Permission denied (endpoint policy changed)
    - HTTP 4xx (except 429) from customer endpoint
    - Invalid endpoint format
    - Subscription metadata stale (endpoint changed)

SNS behavior:
    1. No retries
    2. If DLQ configured → message sent to DLQ immediately
    3. If no DLQ → message lost
    4. Subscription status remains active (not disabled)

Why no retry? The error is permanent. The endpoint doesn't exist or won't accept
the message no matter how many times you try.
```

### Server-Side Errors (Full Retry)

```
Server-side errors indicate a transient problem — retrying may succeed.

Examples:
    - HTTP 5xx from customer endpoint (server error)
    - HTTP 429 from customer endpoint (throttled)
    - Connection timeout (endpoint unreachable)
    - SQS throttled (rare but possible at extreme scale)
    - Lambda throttled (concurrency limit reached)
    - Internal SNS error

SNS behavior:
    1. Full retry policy applied (100K/23d or 50/6h depending on endpoint type)
    2. If all retries exhausted and DLQ configured → message sent to DLQ
    3. If all retries exhausted and no DLQ → message lost

Why retry? The error is likely temporary. The endpoint might recover in seconds,
minutes, or hours.
```

### The 429 Exception

HTTP 429 (Too Many Requests) is technically a 4xx status code, but SNS treats it as **retryable** because it indicates throttling, not a permanent error:

```
HTTP 400 → Client error → No retry
HTTP 403 → Client error → No retry
HTTP 404 → Client error → No retry
HTTP 429 → Throttling  → RETRY (treated as server-side error)
HTTP 500 → Server error → RETRY
HTTP 503 → Server error → RETRY
```

---

## 14. Delivery Status Logging

SNS can log delivery status to CloudWatch Logs for supported protocols.

### Supported Protocols for Status Logging

| Protocol | Logging Supported |
|----------|:-----------------:|
| SQS | Yes |
| Lambda | Yes |
| HTTP/S | Yes |
| Firehose | Yes |
| Mobile Push (APNs, FCM, etc.) | Yes |
| SMS | Yes (via SMS delivery reports) |
| Email | No |

### How to Enable

```
SetTopicAttributes:
    AttributeName = "<Protocol>SuccessFeedbackRoleArn"
    AttributeValue = "arn:aws:iam::123456789012:role/SNSDeliveryLoggingRole"

    AttributeName = "<Protocol>FailureFeedbackRoleArn"
    AttributeValue = "arn:aws:iam::123456789012:role/SNSDeliveryLoggingRole"

    AttributeName = "<Protocol>SuccessFeedbackSampleRate"
    AttributeValue = "100"  (log 100% of successes; 0-100)

Protocols: SQSSuccessFeedback, LambdaSuccessFeedback, HTTPSSuccessFeedback, etc.
```

### Log Content

```
CloudWatch Log Group: sns/<region>/<account-id>/<topic-name>

Log entry (success):
{
    "notification": {
        "messageMD5Sum": "...",
        "messageId": "dc1e94d9...",
        "topicArn": "arn:aws:sns:...",
        "timestamp": "2026-02-13 10:30:00.000"
    },
    "delivery": {
        "deliveryId": "...",
        "destination": "arn:aws:sqs:...",
        "providerResponse": "{\"sqsRequestId\": \"...\"}",
        "dwellTimeMs": 45,
        "attemptsMade": 1,
        "statusCode": 200
    },
    "status": "SUCCESS"
}

Log entry (failure):
{
    "notification": { ... },
    "delivery": {
        "deliveryId": "...",
        "destination": "https://example.com/webhook",
        "providerResponse": "Connection refused",
        "dwellTimeMs": 30000,
        "attemptsMade": 50,
        "statusCode": 0
    },
    "status": "FAILURE"
}
```

### Key Metrics from Delivery Logs

| Metric | Meaning |
|--------|---------|
| `dwellTimeMs` | Time from publish to delivery attempt (including queue time) |
| `attemptsMade` | Number of delivery attempts (1 = first attempt, >1 = retries) |
| `statusCode` | HTTP status code (200 = success, 0 = connection failure) |
| `providerResponse` | Response from the endpoint (helpful for debugging) |

---

## 15. Delivery Performance and Throttling

### End-to-End Delivery Latency

```
Publish → Delivery to subscriber:

    SQS:      ~20-30ms (publish + internal routing + SendMessage)
    Lambda:   ~25-50ms (publish + internal routing + async invoke)
    HTTP/S:   ~50ms - 30s (publish + routing + HTTP round-trip to endpoint)
    Firehose: ~25-50ms (publish + routing + PutRecord)
    Email:    seconds to minutes (SMTP relay + mail server processing)
    SMS:      seconds (gateway + carrier network)
    Push:     seconds (APNs/FCM processing + device delivery)
```

### Throttling by Endpoint Type

| Protocol | Throttle Mechanism | Configurable? |
|----------|-------------------|:-------------:|
| SQS | SQS absorbs at any rate | N/A |
| Lambda | Lambda concurrency limits | Via Lambda reserved concurrency |
| HTTP/S | `maxReceivesPerSecond` in delivery policy | Yes |
| Email | 10 msg/sec per subscription (hard limit) | No |
| SMS | 20 msg/sec account-level | No (raise via support) |
| Mobile Push | Platform-dependent (APNs/FCM rate limits) | No |
| Firehose | Firehose throughput limits | Via Firehose scaling |

### What Happens When a Subscriber Is Throttled

```
Scenario: HTTP endpoint with maxReceivesPerSecond=10
          Topic receives 100 publishes/sec

    SNS delivery rate to this subscription: 10 msg/sec (throttled)
    Remaining 90 messages: queued internally, delivered at 10/sec
    Time to clear: ~9 seconds of additional latency

    If the backlog grows too large:
        Oldest messages in the internal queue may approach retry timeout
        If not delivered before timeout → treated as delivery failure → retry

    Other subscriptions on the same topic: UNAFFECTED by this throttle
    The throttle is per-subscription, not per-topic.
```

---

## 16. Common Delivery Failure Patterns

### Pattern 1: Endpoint Down for Extended Period

```
Cause:  Customer's HTTP server goes down for maintenance
Effect: All deliveries fail, retries consume resources
Result: After 6 hours (50 retries), messages go to DLQ

Mitigation:
    1. Configure DLQ to capture messages
    2. Monitor DLQ depth
    3. After fixing endpoint, redrive messages from DLQ
    4. Use maxReceivesPerSecond to limit blast during recovery
```

### Pattern 2: Endpoint Slow but Not Failing

```
Cause:  HTTP endpoint responds in 25 seconds instead of 100ms
Effect: Delivery workers tied up waiting for responses
        Reduced delivery capacity for other subscriptions [INFERRED]
Result: Increased latency for all deliveries through the same workers

Mitigation:
    1. Set connection timeout on the delivery side [INFERRED]
    2. Use maxReceivesPerSecond throttle
    3. Consider SQS as subscriber instead (always fast)
```

### Pattern 3: Permission Change Breaks Delivery

```
Cause:  Customer updates IAM/queue policy, accidentally removes SNS access
Effect: Client-side error (403 Forbidden)
Result: No retries. Messages go straight to DLQ (or lost if no DLQ)

Mitigation:
    1. Always configure DLQ
    2. Test policy changes in staging first
    3. Monitor NumberOfNotificationsFailed CloudWatch metric
```

### Pattern 4: Lambda Concurrency Exhaustion

```
Cause:  Lambda function reaches reserved concurrency limit
Effect: Lambda rejects async invocations with throttle error
Result: SNS retries (100,015 times over 23 days)
        Meanwhile, backlog grows in Lambda's internal queue

Mitigation:
    1. Increase Lambda reserved concurrency
    2. Use SQS queue between SNS and Lambda to buffer
       (SNS → SQS → Lambda event source mapping)
    3. Monitor Lambda throttle metrics
```

### Pattern 5: Stale Mobile Push Tokens

```
Cause:  User uninstalls app, device token becomes invalid
Effect: Platform service (APNs/FCM) returns error for stale token
Result: Wasted delivery attempts until SNS detects and disables endpoint

Mitigation:
    1. Periodically check endpoint status (GetEndpointAttributes)
    2. Use platform feedback service (APNs Feedback) to proactively prune
    3. Re-register tokens on app launch
```

### Pattern 6: Fan-Out Amplification Cost Surprise

```
Cause:  Topic has 10,000 SQS subscribers
        Someone publishes 1M messages/day (10M deliveries/day)
Effect: SNS charges for publishes ($0.50/M) but SQS to SQS delivery is free
        However: SQS charges for receives on all 10,000 queues
Result: Unexpected SQS bill from all the consumer queues

Mitigation:
    1. Use message filtering to reduce deliveries per subscriber
    2. Monitor delivery volume per subscription
    3. Consider consolidating subscribers where possible
```

---

## 17. Cross-References

| Topic | Document |
|-------|----------|
| Fan-out engine architecture | [fan-out-engine.md](fan-out-engine.md) |
| SNS + SQS fan-out pattern | [sns-sqs-fanout.md](sns-sqs-fanout.md) |
| FIFO topics ordering and dedup | [fifo-topics.md](fifo-topics.md) |
| Message filtering | [message-filtering.md](message-filtering.md) |
| Mobile push deep dive | [mobile-push.md](mobile-push.md) |
| Interview simulation | [interview-simulation.md](interview-simulation.md) |

### AWS Documentation References

- [Amazon SNS message delivery retries](https://docs.aws.amazon.com/sns/latest/dg/sns-message-delivery-retries.html)
- [Amazon SNS dead-letter queues](https://docs.aws.amazon.com/sns/latest/dg/sns-dead-letter-queues.html)
- [Amazon SNS raw message delivery](https://docs.aws.amazon.com/sns/latest/dg/sns-large-payload-raw-message-delivery.html)
- [Amazon SNS message delivery status](https://docs.aws.amazon.com/sns/latest/dg/sns-topic-attributes.html)
- [Amazon SNS quotas](https://docs.aws.amazon.com/general/latest/gr/sns.html)
