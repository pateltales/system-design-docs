# Amazon SNS — Mobile Push & App-to-Person Delivery Deep Dive

> Companion deep dive to the [interview simulation](interview-simulation.md). This document covers mobile push notifications (APNs, FCM), SMS delivery, email delivery, platform endpoint management, and the architectural differences between app-to-app (A2A) and app-to-person (A2P) delivery.

---

## Table of Contents

1. [A2A vs A2P — Two Delivery Models](#1-a2a-vs-a2p--two-delivery-models)
2. [Mobile Push Architecture](#2-mobile-push-architecture)
3. [Platform Applications and Endpoints](#3-platform-applications-and-endpoints)
4. [Push Notification Platforms](#4-push-notification-platforms)
5. [Device Token Lifecycle](#5-device-token-lifecycle)
6. [Multi-Platform Message Formatting](#6-multi-platform-message-formatting)
7. [Direct Publish vs Topic-Based Push](#7-direct-publish-vs-topic-based-push)
8. [SMS Delivery](#8-sms-delivery)
9. [Email and Email-JSON Delivery](#9-email-and-email-json-delivery)
10. [Delivery Status Monitoring for A2P](#10-delivery-status-monitoring-for-a2p)
11. [Common A2P Failure Modes](#11-common-a2p-failure-modes)
12. [Cost Analysis for A2P](#12-cost-analysis-for-a2p)
13. [Cross-References](#13-cross-references)

---

## 1. A2A vs A2P — Two Delivery Models

SNS serves two fundamentally different use cases:

| Aspect | A2A (App-to-App) | A2P (App-to-Person) |
|--------|:-----------------:|:-------------------:|
| **Purpose** | System-to-system messaging | Notification to humans |
| **Endpoints** | SQS, Lambda, HTTP, Firehose | Mobile devices, phones, email inboxes |
| **Protocols** | SQS, Lambda, HTTP/S, Firehose | Mobile Push (APNs/FCM), SMS, Email |
| **Latency** | Milliseconds | Seconds to minutes |
| **Reliability** | Very high (AWS-managed) | Variable (carrier networks, device state) |
| **Cost** | $0.50/M publishes | Per-message (SMS: $0.006+, Push: $0.50/M) |
| **Retry** | 100,015 attempts / 23 days | 50 attempts / 6 hours |
| **Delivery confirmation** | HTTP status from endpoint | Platform-dependent (APNs/FCM feedback) |

Both use the same fan-out engine, but deliver through different backends.

---

## 2. Mobile Push Architecture

### End-to-End Push Flow

```
Your Server                  AWS SNS                     Platform Service          Device
    │                          │                              │                     │
    │  1. CreatePlatformApp    │                              │                     │
    │     (APNs cert or        │                              │                     │
    │      FCM server key)     │                              │                     │
    │─────────────────────────►│                              │                     │
    │                          │                              │                     │
    │  2. CreatePlatformEndpoint                              │                     │
    │     (device token)       │                              │                     │
    │─────────────────────────►│                              │                     │
    │                          │                              │                     │
    │  3. Publish(             │                              │                     │
    │     EndpointArn or       │                              │                     │
    │     TopicArn,            │                              │                     │
    │     message)             │                              │                     │
    │─────────────────────────►│                              │                     │
    │                          │  4. Format message           │                     │
    │                          │     for platform             │                     │
    │                          │─────────────────────────────►│                     │
    │                          │                              │  5. Deliver to      │
    │                          │                              │     device          │
    │                          │                              │────────────────────►│
    │                          │                              │                     │
    │                          │  6. Platform response        │                     │
    │                          │     (success/failure)        │                     │
    │                          │◄─────────────────────────────│                     │
    │                          │                              │                     │
    │  7. Delivery status      │                              │                     │
    │     (CloudWatch Logs)    │                              │                     │
    │◄─────────────────────────│                              │                     │
```

---

## 3. Platform Applications and Endpoints

### Hierarchy

```
AWS Account
  │
  └── SNS Platform Application (one per app × platform)
       │
       │  ARN: arn:aws:sns:us-east-1:123456789012:app/APNS/MyiOSApp
       │
       │  Stores: Push credentials (APNs cert/token, FCM key)
       │
       ├── Platform Endpoint (one per device)
       │    ARN: arn:aws:sns:us-east-1:123456789012:endpoint/APNS/MyiOSApp/abc123
       │    Stores: Device token, enabled flag, user data
       │
       ├── Platform Endpoint (another device)
       │    ARN: arn:aws:sns:...:endpoint/APNS/MyiOSApp/def456
       │
       └── ... (millions of endpoints possible)
```

### Creating a Platform Application

```bash
# For Apple Push Notification service (APNs)
aws sns create-platform-application \
    --name MyiOSApp \
    --platform APNS \
    --attributes '{
        "PlatformCredential": "<APNs private key>",
        "PlatformPrincipal": "<APNs certificate>"
    }'

# For Firebase Cloud Messaging (FCM)
aws sns create-platform-application \
    --name MyAndroidApp \
    --platform GCM \
    --attributes '{
        "PlatformCredential": "<FCM server key>"
    }'
```

### Creating a Platform Endpoint

```bash
# When a device registers, your app server creates an endpoint
aws sns create-platform-endpoint \
    --platform-application-arn arn:aws:sns:us-east-1:123456789012:app/APNS/MyiOSApp \
    --token "DEVICE_TOKEN_FROM_APNS"

# Returns:
# EndpointArn: arn:aws:sns:us-east-1:123456789012:endpoint/APNS/MyiOSApp/abc123
```

---

## 4. Push Notification Platforms

### Supported Platforms

| Platform ID | Service | Devices | Credential Type |
|------------|---------|---------|----------------|
| **APNS** | Apple Push Notification service | iOS, macOS (production) | Certificate or token-based auth |
| **APNS_SANDBOX** | Apple Push Notification service | iOS, macOS (development) | Certificate or token-based auth |
| **GCM** | Firebase Cloud Messaging (legacy name) | Android, iOS, Web | Server key or service account |
| **ADM** | Amazon Device Messaging | Kindle Fire tablets | Client ID + Client Secret |
| **WNS** | Windows Push Notification Service | Windows 10/11 | Package SID + Secret |
| **MPNS** | Microsoft Push Notification Service | Windows Phone (legacy) | Certificate |
| **Baidu** | Baidu Cloud Push | Android devices in China | API Key + Secret Key |

> Note: GCM is the legacy platform identifier. Google renamed the service to Firebase Cloud Messaging (FCM), but SNS still uses "GCM" as the platform identifier.

### Platform-Specific Constraints

| Platform | Max Payload Size | TTL Support | Silent Push | Rich Media |
|----------|:----------------:|:-----------:|:-----------:|:----------:|
| APNs | 4 KB (5 KB for VoIP) | Yes (expiration date) | Yes | Yes (images, video) |
| FCM | 4 KB (data), unlimited (notification) | Yes (time_to_live) | Yes | Yes |
| ADM | 6 KB | Yes (expiresAfter) | Yes | Limited |
| WNS | 5 KB (toast) | Yes | Yes | Yes (tiles, badges) |

---

## 5. Device Token Lifecycle

### The Token Registration Flow

```
1. APP INSTALL
   User installs your app → App requests push permission

2. TOKEN REGISTRATION
   App → Platform (APNs/FCM): "Register me for push"
   Platform → App: "Here's your device token: ABC123XYZ..."

3. TOKEN UPLOAD
   App → Your Server: "My device token is ABC123XYZ"
   Your Server → SNS: CreatePlatformEndpoint(token=ABC123XYZ)
   SNS → Your Server: EndpointArn = arn:aws:sns:...:endpoint/APNS/.../abc123

4. PUSH NOTIFICATION
   Your Server → SNS: Publish(EndpointArn, message)
   SNS → APNs/FCM: Push to ABC123XYZ
   APNs/FCM → Device: Display notification
```

### Token Invalidation

```
Tokens become invalid when:
    1. User uninstalls the app → token immediately invalid
    2. User reinstalls the app → NEW token issued, old token invalid
    3. Platform rotates token → APNs periodically issues new tokens
    4. Device reset → all tokens invalidated
    5. OS upgrade → tokens may change (platform-dependent)
```

### How SNS Handles Stale Tokens

```
SNS pushes to stale token:
    │
    ├── APNs/FCM returns error:
    │   "InvalidToken" or "NotRegistered"
    │
    ├── SNS sets endpoint Enabled = false
    │
    └── Future publishes to this endpoint:
        → SNS checks Enabled flag
        → If false: skips delivery (no API call to platform)
        → Reduces wasted delivery attempts

Problem: There's a LAG between token becoming stale and SNS detecting it.
During this lag, SNS wastes delivery attempts to dead tokens.
```

### Token Refresh Pattern

```java
// On every app launch, re-register the token
// This handles: token rotation, reinstalls, OS upgrades

void onAppLaunch() {
    String token = getDeviceTokenFromPlatform();  // APNs or FCM

    try {
        // CreatePlatformEndpoint is idempotent:
        // If endpoint with this token exists → returns existing ARN
        // If token is new → creates new endpoint
        CreatePlatformEndpointResult result = sns.createPlatformEndpoint(
            new CreatePlatformEndpointRequest()
                .withPlatformApplicationArn(APP_ARN)
                .withToken(token)
        );
        String endpointArn = result.getEndpointArn();

        // Update your backend with the current endpointArn
        updateBackend(userId, endpointArn);

    } catch (InvalidParameterException e) {
        // Token already registered to a different endpoint
        // May need to delete old endpoint and re-create
        handleTokenConflict(token);
    }
}
```

### Endpoint Hygiene — Pruning Stale Endpoints

```
Over time, a platform application accumulates disabled endpoints:
    Active endpoints:   1,000,000
    Disabled endpoints: 500,000 (stale tokens from uninstalls)

These disabled endpoints consume:
    - Metadata storage
    - Subscription list size (if subscribed to topics)
    - Fan-out evaluation time (even though delivery is skipped)

Recommendation:
    1. Periodically list endpoints: ListEndpointsByPlatformApplication
    2. Check each endpoint's Enabled attribute
    3. Delete disabled endpoints: DeleteEndpoint
    4. Or: use CloudWatch delivery failure logs to identify stale endpoints
```

---

## 6. Multi-Platform Message Formatting

### The Problem

Different platforms require different JSON payload structures:

```
APNs expects:
    {"aps": {"alert": {"title": "New Order", "body": "Order #123 confirmed"}}}

FCM expects:
    {"notification": {"title": "New Order", "body": "Order #123 confirmed"}}

ADM expects:
    {"data": {"message": "Order #123 confirmed"}}
```

### SNS Multi-Platform Message

```json
{
    "default": "Order #123 confirmed",
    "APNS": "{\"aps\":{\"alert\":{\"title\":\"New Order\",\"body\":\"Order #123 confirmed\"},\"sound\":\"default\"}}",
    "APNS_SANDBOX": "{\"aps\":{\"alert\":{\"title\":\"New Order\",\"body\":\"Order #123 confirmed\"},\"sound\":\"default\"}}",
    "GCM": "{\"notification\":{\"title\":\"New Order\",\"body\":\"Order #123 confirmed\"},\"data\":{\"orderId\":\"123\"}}",
    "ADM": "{\"data\":{\"title\":\"New Order\",\"message\":\"Order #123 confirmed\"}}"
}
```

**Key details:**
- The `default` key is required (fallback for platforms not explicitly specified)
- Each platform key contains a **string** (not a JSON object) — the inner JSON must be escaped
- SNS selects the appropriate payload based on the endpoint's platform
- If publishing to a topic with mixed platform endpoints, SNS formats differently per subscriber

### Publishing with Multi-Platform Format

```bash
aws sns publish \
    --target-arn arn:aws:sns:us-east-1:123456789012:endpoint/APNS/MyiOSApp/abc123 \
    --message-structure json \
    --message '{
        "default": "Order confirmed",
        "APNS": "{\"aps\":{\"alert\":\"Order confirmed\",\"sound\":\"default\"}}"
    }'
```

The `--message-structure json` flag tells SNS to interpret the message as platform-specific JSON rather than a plain string.

---

## 7. Direct Publish vs Topic-Based Push

### Direct Publish (to one device)

```
Publish(TargetArn = EndpointArn, message)

Use when:
    - Targeting a specific user/device
    - Personalized notifications
    - User-triggered events (e.g., "your order shipped")

    Publisher → SNS → APNs/FCM → One device
```

### Topic-Based Push (to many devices)

```
Subscribe device endpoints to a topic, then Publish(TopicArn, message)

Use when:
    - Broadcasting to all users (app updates, global alerts)
    - Segment-based notifications (subscribe to "sports" topic)
    - Group notifications (subscribe team members to a project topic)

    Publisher → SNS Topic → Fan-out → APNs/FCM → Many devices

                 ┌──────────────────────┐
                 │  Topic: sports-news   │
                 └──────────┬───────────┘
                            │ Fan-out
              ┌─────────────┼─────────────┐
              ▼             ▼             ▼
         Endpoint 1    Endpoint 2    Endpoint N
         (iOS)         (Android)     (iOS)
              │             │             │
              ▼             ▼             ▼
           APNs           FCM          APNs
              │             │             │
              ▼             ▼             ▼
          Device 1     Device 2     Device N
```

### Considerations for Large-Scale Push

```
If you have 10 million devices subscribed to a topic:
    One Publish → 10 million fan-out deliveries to platform services

    Each delivery goes through APNs or FCM:
        APNs: ~100ms per delivery [INFERRED]
        FCM:  ~100ms per delivery [INFERRED]

    SNS handles the fan-out in parallel via delivery workers.
    Platform services have their own rate limits.

    For very large topics (millions of endpoints):
        - Fan-out takes seconds to minutes [INFERRED]
        - Not all devices receive simultaneously
        - Platform rate limits may cause delivery delays
```

---

## 8. SMS Delivery

### How SMS Works in SNS

```
Publisher → SNS → AWS SMS Gateway → Carrier Network → Mobile Device

Two publishing modes:
    1. Publish to a phone number: Publish(PhoneNumber="+12025551234", message)
    2. Publish to a topic: Subscribe phone numbers to topic, then Publish(TopicArn)
```

### SMS Message Types

| Type | Priority | Use Case | Cost |
|------|:--------:|----------|------|
| **Transactional** | High | OTPs, alerts, confirmations | Higher |
| **Promotional** | Normal | Marketing, general notifications | Lower |

Transactional messages bypass carrier-level spam filters and are prioritized for delivery. Promotional messages may be throttled or filtered by carriers.

### SMS Limits and Costs

| Limit | Value |
|-------|:-----:|
| Send rate | 20 messages/sec (default) |
| Max message length | 160 chars (GSM), 70 chars (Unicode) |
| Multi-part messages | Up to 1600 chars (concatenated) |
| Monthly spending limit | $1/month (default, raise via support) |
| Opt-out | Automatic via STOP keyword |

### SMS Cost by Country (Sample)

| Country | Cost per SMS |
|---------|:------------:|
| US | $0.00645 |
| Canada | $0.00683 |
| UK | $0.04000 |
| India | $0.02000 |
| Japan | $0.04480 |
| Brazil | $0.03440 |
| Australia | $0.04010 |

### SMS Reliability Challenges

```
SMS delivery is NOT guaranteed:

    1. Carrier filtering: carriers may silently drop messages (especially promotional)
    2. Number portability: carrier routing may be incorrect
    3. International routing: messages traverse multiple carrier networks
    4. Device off/unreachable: message may expire before delivery
    5. Opt-out: user may have opted out via STOP keyword
    6. Regulatory: some countries require sender registration

SNS provides delivery status logging for SMS:
    - Success/failure per message
    - Carrier response codes
    - Price charged
```

### Sender ID and Short Codes

```
Sender types:
    Long code:   +12025551234 (standard phone number)
    Short code:  12345 (5-6 digit number, higher throughput)
    Toll-free:   +18005551234
    Sender ID:   "MyBrand" (alphanumeric, not supported in all countries)

Short codes:
    - Higher throughput (100+ msg/sec)
    - Lower filtering by carriers
    - Must be provisioned in advance ($500-1000/month)
    - Country-specific (US short code doesn't work in UK)
```

---

## 9. Email and Email-JSON Delivery

### Email Protocol

```
SNS → AWS SMTP Relay → Recipient Mail Server → Inbox

Message format:
    Subject: <SNS Subject field or topic display name>
    Body:    <Message body as plain text>
    Footer:  "If you wish to stop receiving notifications from this topic,
              click or visit the link below to unsubscribe:
              [Unsubscribe link]"
```

### Email-JSON Protocol

```
Same delivery path, but body is the full SNS JSON envelope:

{
    "Type": "Notification",
    "MessageId": "...",
    "TopicArn": "...",
    "Subject": "Order Update",
    "Message": "Your order has shipped",
    "Timestamp": "...",
    "UnsubscribeURL": "..."
}

Useful for automated email processors that need structured data.
```

### Email Subscription Confirmation (Double Opt-In)

```
1. Subscribe(TopicArn, Protocol="email", Endpoint="user@example.com")
2. SNS sends confirmation email:
   "You have chosen to subscribe to the topic: order-events
    To confirm the subscription, visit the URL below:
    [Confirm subscription link]"
3. User clicks the link
4. Subscription confirmed. Notifications start flowing.

This is REQUIRED by anti-spam regulations (CAN-SPAM, GDPR).
Unconfirmed subscriptions do not receive messages.
```

### Email Limits

| Limit | Value |
|-------|:-----:|
| Delivery rate | 10 messages/sec per subscription (hard limit) |
| Confirmation | Required (double opt-in) |
| Unsubscribe | Mandatory link in every email |
| Message size | 256 KB (including SNS envelope) |

---

## 10. Delivery Status Monitoring for A2P

### CloudWatch Metrics

| Metric | Description |
|--------|-------------|
| `NumberOfNotificationsDelivered` | Successfully delivered to platform/carrier |
| `NumberOfNotificationsFailed` | Failed deliveries (all retries exhausted) |
| `NumberOfNotificationsFilteredOut` | Filtered by subscription filter policy |
| `SMSSuccessRate` | Percentage of SMS messages successfully delivered |

### Delivery Status Logging

For mobile push, SMS, and other A2P protocols, SNS can log delivery status to CloudWatch Logs:

```
Enable via topic attributes:

    Application (mobile push):
        ApplicationSuccessFeedbackRoleArn
        ApplicationFailureFeedbackRoleArn
        ApplicationSuccessFeedbackSampleRate (0-100%)

    SMS:
        Uses separate SMS delivery reporting

Log entry format:
{
    "notification": {
        "messageId": "...",
        "timestamp": "..."
    },
    "delivery": {
        "destination": "arn:aws:sns:...:endpoint/APNS/.../abc123",
        "deliveryId": "...",
        "providerResponse": "...",     ← Platform response (APNs/FCM error)
        "dwellTimeMs": 150,            ← Time from publish to delivery attempt
        "attemptsMade": 1,
        "statusCode": 200
    },
    "status": "SUCCESS"
}
```

### SMS Delivery Reports

```
SMS has dedicated delivery reporting:

    aws sns set-sms-attributes \
        --attributes '{
            "DeliveryStatusSuccessSamplingRate": "100",
            "DeliveryStatusIAMRole": "arn:aws:iam::123456789012:role/SNSSMSRole"
        }'

Log entry for SMS:
{
    "notification": {
        "messageId": "...",
        "timestamp": "..."
    },
    "delivery": {
        "phoneCarrier": "Verizon Wireless",
        "providerResponse": "Message has been accepted by phone carrier",
        "dwellTimeMs": 1200,
        "dwellTimeMsUntilDeviceAck": 5400     ← Time until device acknowledged
    },
    "status": "SUCCESS"
}
```

---

## 11. Common A2P Failure Modes

### Mobile Push Failures

| Failure | Cause | SNS Behavior |
|---------|-------|-------------|
| **InvalidToken** | App uninstalled, token stale | Endpoint disabled. No further delivery. |
| **PayloadTooLarge** | Message > 4 KB (APNs) | Client-side error. No retry. DLQ if configured. |
| **TopicDisabled** | APNs certificate expired | All deliveries to this platform app fail |
| **PlatformRateLimited** | Too many requests to APNs/FCM | Retries with backoff |
| **DeviceOffline** | Device not connected | Platform queues the message (APNs: up to 30 days) |
| **Expired** | Message TTL expired before delivery | Message dropped by platform |

### SMS Failures

| Failure | Cause | SNS Behavior |
|---------|-------|-------------|
| **CarrierBlocked** | Carrier filters the message | Delivery fails. No retry on carrier block. |
| **InvalidPhoneNumber** | Wrong format or non-existent | Client-side error. No retry. |
| **SpendingLimitExceeded** | Monthly SMS budget exceeded | All SMS stopped until limit raised or next month |
| **OptedOut** | User sent STOP keyword | Delivery blocked. Must re-opt-in. |
| **UnknownError** | Carrier-level failure | Retries per customer-managed policy (50 attempts / 6 hours) |

### Email Failures

| Failure | Cause | SNS Behavior |
|---------|-------|-------------|
| **Bounce** | Email address doesn't exist | Soft bounce: retry. Hard bounce: mark as failed. |
| **Complaint** | Recipient marked as spam | Future emails may be blocked |
| **Throttled** | 10 msg/sec per subscription limit | Queued internally, delivered at limit rate |
| **Unconfirmed** | Subscription not confirmed | No delivery at all |

---

## 12. Cost Analysis for A2P

### Mobile Push Pricing

```
Mobile push notifications:
    $0.50 per million pushes (to APNs, FCM, ADM, WNS, MPNS, Baidu)
    First 1 million per month: FREE

Example:
    5 million push notifications/month
    First 1M free, then 4M × $0.50/M = $2.00/month

    Extremely cheap. The platform services (APNs/FCM) don't charge for delivery.
```

### SMS Pricing

```
SMS costs vary by country and message type:

    US example:
        1 million transactional SMS/month
        1M × $0.00645/msg = $6,450/month

    Plus:
        Long code: ~$1/month per number
        Short code: ~$500-1000/month per number
        Toll-free: ~$2/month per number

    SMS is EXPENSIVE compared to push notifications.
    At scale, SMS costs dominate the SNS bill.
```

### Cost Comparison: Push vs SMS vs Email

| Channel | Cost per 1M messages | Delivery Speed | Reliability |
|---------|:--------------------:|:--------------:|:-----------:|
| Mobile Push | $0.50 | Seconds | Medium (device must be online) |
| SMS | $6,450 (US) | Seconds | Medium (carrier dependent) |
| Email | $2.00 per 100K = $20.00 | Minutes | Medium (spam filters) |

**Recommendation**: Use mobile push as the primary notification channel. Reserve SMS for critical messages (OTPs, security alerts). Use email for non-urgent communications.

---

## 13. Cross-References

| Topic | Document |
|-------|----------|
| Fan-out engine | [fan-out-engine.md](fan-out-engine.md) |
| Delivery retry policies | [delivery-and-retries.md](delivery-and-retries.md) |
| SNS + SQS fan-out pattern | [sns-sqs-fanout.md](sns-sqs-fanout.md) |
| FIFO topics | [fifo-topics.md](fifo-topics.md) |
| Message filtering | [message-filtering.md](message-filtering.md) |
| Interview simulation | [interview-simulation.md](interview-simulation.md) |

### AWS Documentation References

- [Amazon SNS mobile push notifications](https://docs.aws.amazon.com/sns/latest/dg/sns-mobile-application-as-subscriber.html)
- [Amazon SNS SMS messaging](https://docs.aws.amazon.com/sns/latest/dg/sns-mobile-phone-number-as-subscriber.html)
- [Amazon SNS SMS pricing](https://aws.amazon.com/sns/sms-pricing/)
- [Creating platform endpoints](https://docs.aws.amazon.com/sns/latest/dg/mobile-push-send-devicetoken.html)
- [Amazon SNS delivery status logging](https://docs.aws.amazon.com/sns/latest/dg/sns-topic-attributes.html)
