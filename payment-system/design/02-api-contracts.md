# Payment Platform API Contracts — Comprehensive Reference

> **Purpose**: This is the full API surface of a payment platform (modeled after Stripe and Razorpay). The interview simulation (Phase 3 of `01-interview-simulation.md`) covers only a subset — this doc is the comprehensive reference.
>
> **Convention**: Endpoints marked with a star (**) are covered in the interview simulation.

---

## Table of Contents

1. [Payment Intent / Order APIs](#1-payment-intent--order-apis)
2. [Refund APIs](#2-refund-apis)
3. [Payment Method APIs](#3-payment-method-apis)
4. [Customer APIs](#4-customer-apis)
5. [Webhook / Event APIs](#5-webhook--event-apis)
6. [Dispute / Chargeback APIs](#6-dispute--chargeback-apis)
7. [Payout / Settlement APIs](#7-payout--settlement-apis)
8. [Subscription / Recurring Payment APIs](#8-subscription--recurring-payment-apis)
9. [Ledger / Reporting APIs (Internal)](#9-ledger--reporting-apis-internal)
10. [Admin / Ops APIs (Internal)](#10-admin--ops-apis-internal)
11. [API Design Philosophy — Stripe vs PayPal vs Razorpay](#11-api-design-philosophy--stripe-vs-paypal-vs-razorpay)
12. [Interview Subset Summary](#12-interview-subset-summary)

---

## Authentication & Common Headers

All APIs use bearer token authentication (API key in the `Authorization` header). Stripe uses `Bearer sk_live_...` or `Bearer sk_test_...` keys. Razorpay uses HTTP Basic Auth with `key_id:key_secret`.

```
Authorization: Bearer sk_live_abc123xyz
Content-Type: application/json
Idempotency-Key: <client-generated UUID>   # Required for all POST mutations
```

**Common response envelope**:
```json
{
  "id": "pay_abc123",
  "object": "payment",
  "created_at": "2025-07-15T10:30:00Z",
  "livemode": true,
  ...
}
```

Every resource has an `id` (prefixed by type: `pay_`, `ref_`, `cus_`, `pm_`, `evt_`, `dis_`, `po_`, `sub_`), an `object` field indicating the resource type, and a `created_at` timestamp. This follows Stripe's convention exactly — every Stripe API object includes `id`, `object`, and `created` fields.

Razorpay uses a similar pattern but with different prefixes (`pay_`, `rfnd_`, `order_`, `cust_`).

---

## 1. Payment Intent / Order APIs

The core of any payment platform. A "Payment Intent" (Stripe's term) or "Order + Payment" (Razorpay's term) represents the merchant's intention to collect a payment.

### Stripe vs Razorpay terminology

| Concept | Stripe | Razorpay |
|---------|--------|----------|
| Payment initiation | PaymentIntent | Order (created first), then Payment (created on checkout) |
| Two-step flow | `capture_method: "manual"` on PaymentIntent | `payment.capture` set to `0` on Order |
| Idempotency | `Idempotency-Key` header | `Idempotency-Key` header (added later, not on all endpoints) |

Stripe's PaymentIntent is a single object that tracks the entire lifecycle. Razorpay separates the concept into an Order (merchant-side intent) and a Payment (customer-side action). The Razorpay Order must be created server-side first, then the payment is initiated client-side and linked to the order.

---

### ** `POST /payments` — Create a Payment Intent

Creates a new payment intent. This is the starting point for every payment.

**Request**:
```json
POST /v1/payments
Idempotency-Key: "550e8400-e29b-41d4-a716-446655440000"

{
  "amount": 10000,
  "currency": "usd",
  "payment_method": "pm_card_visa_4242",
  "customer_id": "cus_abc123",
  "capture_method": "automatic",
  "description": "Order #12345 — 2x Widget Pro",
  "metadata": {
    "order_id": "ord_12345",
    "merchant_reference": "INV-2025-0042"
  },
  "statement_descriptor": "ACME WIDGETS",
  "receipt_email": "buyer@example.com",
  "return_url": "https://merchant.com/payment/complete"
}
```

**Key fields**:

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `amount` | integer | Yes | Amount in **smallest currency unit** (cents for USD, paise for INR). `10000` = $100.00. Stripe and Razorpay both use this convention. Never use floats for money. |
| `currency` | string | Yes | ISO 4217 currency code (lowercase). Stripe supports 135+ currencies [UNVERIFIED — Stripe docs state "100+" as of 2024]. |
| `payment_method` | string | Conditional | Token referencing a saved payment method. If not provided, the client-side SDK collects payment details. |
| `customer_id` | string | No | Link this payment to a customer record. Required for saved-card / recurring flows. |
| `capture_method` | enum | No | `"automatic"` (default) — authorize and capture in one step. `"manual"` — authorize only, capture later. |
| `description` | string | No | Arbitrary description. Shown in dashboard, not on card statement. |
| `metadata` | object | No | Up to 50 key-value pairs (Stripe's limit). Merchant can store anything — order IDs, internal references, tags. |
| `statement_descriptor` | string | No | Text that appears on the cardholder's bank statement. Max 22 characters (Stripe's limit, set by card network rules). |
| `idempotency_key` | string | Yes | Sent as a header (`Idempotency-Key`). UUID generated by the client. If the server has already processed a request with this key, it returns the stored response without re-executing. Stripe stores idempotency keys for 24 hours. |

**Response** (success — `201 Created`):
```json
{
  "id": "pay_1NqYkR2eZvKYlo2C",
  "object": "payment_intent",
  "status": "requires_confirmation",
  "amount": 10000,
  "amount_capturable": 0,
  "amount_received": 0,
  "currency": "usd",
  "capture_method": "automatic",
  "payment_method": "pm_card_visa_4242",
  "customer_id": "cus_abc123",
  "description": "Order #12345 — 2x Widget Pro",
  "metadata": {
    "order_id": "ord_12345",
    "merchant_reference": "INV-2025-0042"
  },
  "statement_descriptor": "ACME WIDGETS",
  "created_at": "2025-07-15T10:30:00Z",
  "client_secret": "pay_1NqYkR2eZvKYlo2C_secret_abc123",
  "next_action": null,
  "last_payment_error": null,
  "charges": []
}
```

**Status lifecycle** (Stripe's PaymentIntent statuses):

```
requires_payment_method → requires_confirmation → requires_action (3DS) → processing →
  ├── succeeded (if capture_method = automatic)
  └── requires_capture (if capture_method = manual) → succeeded (after capture)

At any point: → canceled
After succeeded: → partially_refunded / fully_refunded (via Refund API)
```

Stripe's actual statuses are: `requires_payment_method`, `requires_confirmation`, `requires_action`, `processing`, `requires_capture`, `succeeded`, `canceled`. Razorpay's payment statuses are: `created`, `authorized`, `captured`, `refunded`, `failed`.

**Error response** (`400 Bad Request`):
```json
{
  "error": {
    "type": "invalid_request_error",
    "code": "amount_too_small",
    "message": "Amount must be at least 50 cents (or equivalent in the given currency).",
    "param": "amount",
    "doc_url": "https://docs.psp.com/errors/amount_too_small"
  }
}
```

Stripe uses a structured error object with `type` (one of: `api_error`, `card_error`, `idempotency_error`, `invalid_request_error`), `code`, `message`, `param` (which request parameter caused the error), and `doc_url`. This is a well-designed error model that other PSPs have adopted.

---

### `GET /payments/{paymentId}` — Retrieve Payment Details

**Request**:
```
GET /v1/payments/pay_1NqYkR2eZvKYlo2C
```

**Response** (`200 OK`):
```json
{
  "id": "pay_1NqYkR2eZvKYlo2C",
  "object": "payment_intent",
  "status": "succeeded",
  "amount": 10000,
  "amount_capturable": 0,
  "amount_received": 10000,
  "currency": "usd",
  "capture_method": "automatic",
  "payment_method": "pm_card_visa_4242",
  "customer_id": "cus_abc123",
  "charges": [
    {
      "id": "ch_3NqYkR2eZvKYlo2C",
      "amount": 10000,
      "status": "succeeded",
      "outcome": {
        "network_status": "approved_by_network",
        "risk_level": "normal",
        "risk_score": 12,
        "seller_message": "Payment complete."
      },
      "payment_method_details": {
        "type": "card",
        "card": {
          "brand": "visa",
          "last4": "4242",
          "exp_month": 12,
          "exp_year": 2026,
          "funding": "credit",
          "country": "US",
          "network": "visa"
        }
      },
      "balance_transaction": "txn_1NqYkR2eZvKYlo2C",
      "created_at": "2025-07-15T10:30:02Z"
    }
  ],
  "metadata": {
    "order_id": "ord_12345"
  },
  "created_at": "2025-07-15T10:30:00Z"
}
```

In Stripe's actual API, the `charges` array is nested under `latest_charge` (for the newer API version) or as an expandable `charges.data` list. The charge object contains the `outcome` with `risk_score` (Stripe Radar's fraud score, 0-100) and `network_status`.

Razorpay's equivalent: `GET /v1/payments/pay_FHR9UMPNat35wU`. Response includes `method` (card/upi/netbanking/wallet), `bank`, `wallet`, `vpa` (for UPI), and India-specific fields.

---

### ** `POST /payments/{paymentId}/capture` — Capture an Authorized Payment

Used in the two-step (authorize-then-capture) flow when `capture_method` was set to `"manual"`.

**Request**:
```json
POST /v1/payments/pay_1NqYkR2eZvKYlo2C/capture
Idempotency-Key: "capture-550e8400-e29b-41d4-a716-446655440001"

{
  "amount_to_capture": 8500
}
```

**Key fields**:

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `amount_to_capture` | integer | No | Amount to capture. If omitted, captures the full authorized amount. If less than the authorized amount, this is a **partial capture** — the remaining authorization is automatically released. |

**Response** (`200 OK`):
```json
{
  "id": "pay_1NqYkR2eZvKYlo2C",
  "object": "payment_intent",
  "status": "succeeded",
  "amount": 10000,
  "amount_capturable": 0,
  "amount_received": 8500,
  "currency": "usd",
  "charges": [
    {
      "id": "ch_3NqYkR2eZvKYlo2C",
      "amount": 8500,
      "amount_refunded": 0,
      "captured": true
    }
  ]
}
```

**Why two-step (authorize then capture) exists**:

1. **Hotels / car rentals**: Authorize $500 at check-in, capture actual amount ($423.50) at checkout. The remaining $76.50 hold is released.
2. **E-commerce with delayed fulfillment**: Authorize at order time. Only capture when the item actually ships. If item is out of stock, cancel the auth instead of refunding (refunds take 5-10 business days to return to cardholder; canceling an auth releases the hold immediately).
3. **Tips / gratuity**: Restaurant authorizes the bill amount ($80), then captures bill + tip ($96) after the customer signs.
4. **Marketplace verification**: Authorize to verify the card is valid and has funds, but don't capture until the seller confirms the order.

**Authorization validity window**: Stripe's docs note that authorizations expire after 7 days for most card payments [UNVERIFIED — exact window depends on card network and issuer]. After expiry, the capture will fail. Razorpay auto-captures payments after 5 days if not manually captured, with a default auto-capture window configurable per merchant.

**Partial capture**: Not all acquirers and card networks support partial capture. Stripe supports it. In Stripe, if you capture less than the authorized amount, the remaining auth is released. You cannot do multiple partial captures — it's one capture per PaymentIntent.

---

### `POST /payments/{paymentId}/cancel` — Cancel / Void an Authorization

Cancels a payment intent that has not yet been captured. If the payment was authorized, this releases the hold on the cardholder's funds immediately (no refund timeline — the hold simply disappears).

**Request**:
```json
POST /v1/payments/pay_1NqYkR2eZvKYlo2C/cancel
Idempotency-Key: "cancel-550e8400-e29b-41d4-a716-446655440002"

{
  "cancellation_reason": "requested_by_customer"
}
```

**Key fields**:

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `cancellation_reason` | enum | No | One of: `duplicate`, `fraudulent`, `requested_by_customer`, `abandoned`. Stripe supports these exact values on PaymentIntent cancellation. |

**Response** (`200 OK`):
```json
{
  "id": "pay_1NqYkR2eZvKYlo2C",
  "object": "payment_intent",
  "status": "canceled",
  "cancellation_reason": "requested_by_customer",
  "amount": 10000,
  "amount_capturable": 0,
  "amount_received": 0,
  "canceled_at": "2025-07-15T11:00:00Z"
}
```

**Cancel vs Refund**: Canceling an uncaptured authorization is free and instant. Refunding a captured payment costs the PSP processing fees and takes 5-10 business days to reach the cardholder. Always prefer cancel over refund when possible.

---

### `GET /payments` — List Payments (Paginated)

**Request**:
```
GET /v1/payments?customer_id=cus_abc123&status=succeeded&limit=25&starting_after=pay_xyz789
```

**Query parameters**:

| Param | Type | Description |
|-------|------|-------------|
| `customer_id` | string | Filter by customer |
| `status` | string | Filter by status |
| `created[gte]` | timestamp | Created at or after |
| `created[lte]` | timestamp | Created at or before |
| `limit` | integer | 1-100, default 10. Stripe's default is 10, max 100. |
| `starting_after` | string | Cursor-based pagination. Pass the `id` of the last object from the previous page. |

**Response** (`200 OK`):
```json
{
  "object": "list",
  "url": "/v1/payments",
  "has_more": true,
  "data": [
    { "id": "pay_abc001", "status": "succeeded", "amount": 5000, ... },
    { "id": "pay_abc002", "status": "requires_capture", "amount": 12000, ... }
  ]
}
```

Stripe uses cursor-based pagination (`starting_after` / `ending_before`) rather than offset-based pagination. This is more efficient for large datasets and avoids the "shifting window" problem when new records are inserted during pagination. Razorpay uses `skip` and `count` parameters (offset-based).

---

## 2. Refund APIs

Refunds create a **new transaction in the opposite direction** — they do NOT reverse the original transaction. The original payment record remains unchanged (status stays `succeeded`); a separate refund record is created. This is a critical distinction: the original charge and the refund are separate financial events with separate ledger entries.

### ** `POST /payments/{paymentId}/refund` — Create a Refund

**Request**:
```json
POST /v1/payments/pay_1NqYkR2eZvKYlo2C/refund
Idempotency-Key: "refund-550e8400-e29b-41d4-a716-446655440003"

{
  "amount": 5000,
  "reason": "requested_by_customer",
  "metadata": {
    "return_id": "ret_789",
    "support_ticket": "TICKET-4567"
  }
}
```

**Key fields**:

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `amount` | integer | No | Amount to refund in smallest currency unit. If omitted, refunds the full remaining unreturned amount. For partial refunds, specify the amount. Multiple partial refunds allowed up to the original charge amount. |
| `reason` | enum | No | One of: `duplicate`, `fraudulent`, `requested_by_customer`. Stripe supports exactly these three values. Helps with analytics and dispute prevention. |
| `metadata` | object | No | Merchant-defined key-value pairs. |

In Stripe's actual API, you create a refund via `POST /v1/refunds` with a `payment_intent` parameter (or `charge` parameter for the older API). Our design nests it under the payment for clarity, but the concept is identical. Razorpay uses `POST /v1/payments/{paymentId}/refund` — nested under the payment, which is what we model here.

**Response** (`201 Created`):
```json
{
  "id": "ref_1NqYkR2eZvKYlo2C",
  "object": "refund",
  "status": "pending",
  "amount": 5000,
  "currency": "usd",
  "payment_id": "pay_1NqYkR2eZvKYlo2C",
  "reason": "requested_by_customer",
  "created_at": "2025-07-15T12:00:00Z",
  "metadata": {
    "return_id": "ret_789",
    "support_ticket": "TICKET-4567"
  },
  "balance_transaction": "txn_refund_abc123"
}
```

**Refund statuses**: `pending` → `succeeded` or `failed`. In Stripe's API, refund statuses are `pending`, `succeeded`, `failed`, `canceled`. The refund moves to `succeeded` once the funds have been returned to the payment method. For card refunds, this does NOT mean the cardholder has received the funds yet — the refund has been submitted to the card network, and it takes additional time to reach the cardholder.

**Refund timelines by payment method**:

| Payment Method | Timeline | Notes |
|---------------|----------|-------|
| Credit/debit card | 5-10 business days | Depends on issuing bank processing time [UNVERIFIED — Stripe docs say "5-10 business days"] |
| UPI (India) | Instant to 1-3 business days | Razorpay docs state instant for some banks |
| Netbanking (India) | 5-7 business days | [UNVERIFIED] |
| Bank transfer (ACH/SEPA) | 3-5 business days | [UNVERIFIED — varies by bank and region] |
| Wallets (PayPal, etc.) | Instant to 1-3 business days | Wallet balance credited immediately |

**Refunds are NOT free**: When a payment is refunded, the PSP has already paid interchange and network fees on the original transaction. Stripe does not return its processing fee (2.9% + 30 cents) on refunds as of December 2023. The interchange fee may or may not be returned depending on the card network and timing — Visa and Mastercard updated their rules to allow interchange refunds for refunds processed within a certain window [UNVERIFIED — policies change frequently].

---

### `GET /refunds/{refundId}` — Retrieve Refund Details

**Request**:
```
GET /v1/refunds/ref_1NqYkR2eZvKYlo2C
```

**Response** (`200 OK`):
```json
{
  "id": "ref_1NqYkR2eZvKYlo2C",
  "object": "refund",
  "status": "succeeded",
  "amount": 5000,
  "currency": "usd",
  "payment_id": "pay_1NqYkR2eZvKYlo2C",
  "reason": "requested_by_customer",
  "receipt_number": "1234-5678",
  "created_at": "2025-07-15T12:00:00Z",
  "arrival_date": "2025-07-22T00:00:00Z"
}
```

---

### `GET /refunds` — List Refunds (Paginated)

```
GET /v1/refunds?payment_id=pay_1NqYkR2eZvKYlo2C&limit=10
```

Same pagination model as the payments list endpoint.

---

## 3. Payment Method APIs

Payment methods represent the instruments a customer uses to pay — cards, bank accounts, wallets, UPI VPAs. The PSP tokenizes raw payment credentials and stores them in a PCI-compliant vault. The merchant never sees raw card numbers — only tokens.

### `POST /payment-methods` — Create / Tokenize a Payment Method

In practice, this endpoint is rarely called by the merchant's backend directly. Instead:

- **Stripe**: The client-side SDK (Stripe.js / Stripe Elements) collects card details in an iframe hosted by Stripe. The raw PAN never touches the merchant's servers. Stripe.js calls Stripe's API directly to create the PaymentMethod, and only the token (`pm_...`) is sent to the merchant's backend. This is called **client-side tokenization** and is the primary mechanism for PCI DSS scope reduction.
- **Razorpay**: Similarly, Razorpay Checkout.js handles card collection. The merchant's backend never sees raw card data.

**Request** (server-side — only for PCI DSS Level 1 compliant merchants):
```json
POST /v1/payment-methods

{
  "type": "card",
  "card": {
    "number": "4242424242424242",
    "exp_month": 12,
    "exp_year": 2026,
    "cvc": "123"
  },
  "billing_details": {
    "name": "John Doe",
    "email": "john@example.com",
    "address": {
      "line1": "123 Main St",
      "city": "San Francisco",
      "state": "CA",
      "postal_code": "94105",
      "country": "US"
    }
  }
}
```

**Response** (`201 Created`):
```json
{
  "id": "pm_1NqYkR2eZvKYlo2C",
  "object": "payment_method",
  "type": "card",
  "card": {
    "brand": "visa",
    "last4": "4242",
    "exp_month": 12,
    "exp_year": 2026,
    "funding": "credit",
    "country": "US",
    "fingerprint": "Xt5EWLLDS7FJjR1c",
    "networks": {
      "available": ["visa"],
      "preferred": null
    }
  },
  "billing_details": {
    "name": "John Doe",
    "email": "john@example.com",
    "address": { ... }
  },
  "created_at": "2025-07-15T09:00:00Z"
}
```

Note: The `fingerprint` field is a unique identifier for the physical card. The same physical card will always produce the same fingerprint regardless of who tokenizes it. This is useful for detecting multiple accounts using the same card (fraud signal). Stripe generates fingerprints per-account (not globally) for privacy reasons.

**UPI payment method** (Razorpay-style):
```json
POST /v1/payment-methods

{
  "type": "upi",
  "upi": {
    "vpa": "customer@upi"
  }
}
```

---

### `GET /payment-methods/{token}` — Retrieve a Payment Method

Returns masked details — never the full PAN.

```
GET /v1/payment-methods/pm_1NqYkR2eZvKYlo2C
```

**Response** (`200 OK`):
```json
{
  "id": "pm_1NqYkR2eZvKYlo2C",
  "object": "payment_method",
  "type": "card",
  "card": {
    "brand": "visa",
    "last4": "4242",
    "exp_month": 12,
    "exp_year": 2026,
    "funding": "credit"
  },
  "customer_id": "cus_abc123",
  "created_at": "2025-07-15T09:00:00Z"
}
```

The full card number is NEVER returned. The `last4` plus `brand` is enough for display purposes. The raw PAN lives in the token vault (PCI CDE — Cardholder Data Environment), encrypted at rest with AES-256 and accessible only to the tokenization service.

---

### `DELETE /payment-methods/{token}` — Detach / Delete a Payment Method

Detaches the payment method from the customer. If the payment method is the default for an active subscription, the deletion will fail.

```
DELETE /v1/payment-methods/pm_1NqYkR2eZvKYlo2C
```

**Response** (`200 OK`):
```json
{
  "id": "pm_1NqYkR2eZvKYlo2C",
  "object": "payment_method",
  "type": "card",
  "card": {
    "brand": "visa",
    "last4": "4242"
  },
  "customer_id": null
}
```

In Stripe's API, the equivalent operation is `POST /v1/payment_methods/{pm_id}/detach` — it detaches but does not permanently delete. The PaymentMethod object still exists (for audit trails / dispute evidence) but is no longer usable.

---

### `GET /customers/{customerId}/payment-methods` — List Customer's Saved Payment Methods

```
GET /v1/customers/cus_abc123/payment-methods?type=card&limit=10
```

**Response** (`200 OK`):
```json
{
  "object": "list",
  "url": "/v1/customers/cus_abc123/payment-methods",
  "has_more": false,
  "data": [
    {
      "id": "pm_card_visa_4242",
      "type": "card",
      "card": { "brand": "visa", "last4": "4242", "exp_month": 12, "exp_year": 2026 }
    },
    {
      "id": "pm_card_mc_5555",
      "type": "card",
      "card": { "brand": "mastercard", "last4": "5555", "exp_month": 6, "exp_year": 2027 }
    }
  ]
}
```

---

## 4. Customer APIs

Customer objects tie together payment methods, subscriptions, and transaction history. In Stripe, a Customer is optional for one-off payments but required for saving cards and subscriptions.

### `POST /customers` — Create a Customer

**Request**:
```json
POST /v1/customers

{
  "name": "John Doe",
  "email": "john@example.com",
  "phone": "+14155551234",
  "description": "Premium merchant user",
  "metadata": {
    "internal_id": "user_789",
    "signup_source": "mobile_app"
  },
  "address": {
    "line1": "123 Main St",
    "city": "San Francisco",
    "state": "CA",
    "postal_code": "94105",
    "country": "US"
  }
}
```

**Response** (`201 Created`):
```json
{
  "id": "cus_abc123",
  "object": "customer",
  "name": "John Doe",
  "email": "john@example.com",
  "phone": "+14155551234",
  "description": "Premium merchant user",
  "default_payment_method": null,
  "metadata": {
    "internal_id": "user_789",
    "signup_source": "mobile_app"
  },
  "balance": 0,
  "currency": "usd",
  "created_at": "2025-07-15T08:00:00Z",
  "livemode": true
}
```

Stripe's Customer object includes a `balance` field (credit balance that can be applied to future invoices) and an `invoice_settings` field (default payment method for subscriptions). Razorpay's Customer object is simpler — `name`, `email`, `contact`, `gstin` (India GST number), `notes`.

---

### `GET /customers/{customerId}` — Retrieve a Customer

```
GET /v1/customers/cus_abc123
```

Returns the full customer object as shown above, plus expandable fields for `subscriptions`, `payment_methods`, and `charges` (Stripe supports `?expand[]=subscriptions`).

---

### `PUT /customers/{customerId}` — Update a Customer

```json
PUT /v1/customers/cus_abc123

{
  "default_payment_method": "pm_card_visa_4242",
  "metadata": {
    "tier": "gold"
  }
}
```

Only the fields provided are updated (partial update / PATCH semantics). Stripe actually uses `POST` for updates (not PUT or PATCH), which is unconventional but consistent across their API.

---

### `DELETE /customers/{customerId}` — Delete a Customer

```
DELETE /v1/customers/cus_abc123
```

**Response** (`200 OK`):
```json
{
  "id": "cus_abc123",
  "object": "customer",
  "deleted": true
}
```

Deleting a customer in Stripe cancels active subscriptions and detaches payment methods. The customer's past payments and invoices are retained for record-keeping (financial records cannot be deleted).

---

## 5. Webhook / Event APIs

Webhooks are the primary mechanism for asynchronous notifications. Payments are inherently async — authorization might succeed instantly, but settlement happens days later. Disputes can arrive weeks later. Webhooks deliver these state changes to the merchant's server.

### ** `POST /webhooks` — Register a Webhook Endpoint

**Request**:
```json
POST /v1/webhooks

{
  "url": "https://merchant.com/webhooks/payments",
  "events": [
    "payment.succeeded",
    "payment.failed",
    "payment.requires_action",
    "refund.created",
    "refund.succeeded",
    "refund.failed",
    "dispute.created",
    "dispute.closed",
    "payout.paid",
    "payout.failed"
  ],
  "api_version": "2025-01-15",
  "description": "Production payment webhooks"
}
```

**Key fields**:

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `url` | string | Yes | HTTPS endpoint that will receive POST requests. Must be HTTPS (Stripe requires HTTPS for live mode). |
| `events` | array | Yes | Event types to subscribe to. Stripe has 200+ event types organized by resource (`payment_intent.succeeded`, `charge.refunded`, `customer.subscription.deleted`, etc.). |
| `api_version` | string | No | Pin to a specific API version so the webhook payload format is stable even if the merchant upgrades later. |

**Response** (`201 Created`):
```json
{
  "id": "whk_1NqYkR2eZvKYlo2C",
  "object": "webhook_endpoint",
  "url": "https://merchant.com/webhooks/payments",
  "events": [
    "payment.succeeded",
    "payment.failed",
    "refund.created",
    "refund.succeeded",
    "dispute.created",
    "dispute.closed"
  ],
  "status": "enabled",
  "secret": "whsec_abc123xyz789",
  "api_version": "2025-01-15",
  "created_at": "2025-07-14T00:00:00Z"
}
```

The `secret` is used to verify webhook signatures. Stripe signs every webhook payload with HMAC-SHA256 using this secret. The merchant must verify the signature before processing the webhook to prevent spoofing. Stripe includes the signature in the `Stripe-Signature` header with format: `t=timestamp,v1=signature`.

---

### Webhook Delivery Payload (What the Merchant Receives)

When an event occurs, the PSP sends a POST request to the registered URL:

```json
POST https://merchant.com/webhooks/payments
Content-Type: application/json
Stripe-Signature: t=1689422400,v1=abc123signaturehash

{
  "id": "evt_1NqYkR2eZvKYlo2C",
  "object": "event",
  "type": "payment.succeeded",
  "api_version": "2025-01-15",
  "created_at": "2025-07-15T10:30:02Z",
  "data": {
    "object": {
      "id": "pay_1NqYkR2eZvKYlo2C",
      "object": "payment_intent",
      "status": "succeeded",
      "amount": 10000,
      "currency": "usd",
      "customer_id": "cus_abc123",
      "metadata": {
        "order_id": "ord_12345"
      }
    },
    "previous_attributes": {
      "status": "processing"
    }
  },
  "livemode": true,
  "pending_webhooks": 1,
  "request": {
    "id": "req_abc123",
    "idempotency_key": "550e8400-e29b-41d4-a716-446655440000"
  }
}
```

Stripe's event object includes `data.previous_attributes` which shows what changed — extremely useful for debugging. The `pending_webhooks` field indicates how many webhook endpoints still need to receive this event. The `request` field links the event to the API request that triggered it.

---

### Webhook Delivery Semantics

**At-least-once delivery**: The PSP guarantees every event will be delivered at least once. It does NOT guarantee exactly-once. This means the merchant's webhook handler **must be idempotent** — receiving the same event twice should not cause duplicate side effects.

**Retry schedule** (Stripe's actual retry behavior):
- Stripe retries webhook deliveries over approximately 3 days with exponential backoff.
- Retry intervals: approximately 1 min, 5 min, 30 min, 2 hours, 5 hours, 10 hours, 10 hours (up to ~72 hours total) [UNVERIFIED — Stripe docs describe "up to 3 days" but exact intervals are not publicly specified].
- A delivery is considered failed if the endpoint returns a non-2xx HTTP status code or times out (Stripe's webhook timeout is ~20 seconds [UNVERIFIED]).
- After all retries are exhausted, the event is marked as failed. Stripe disables webhook endpoints that have been failing consistently (after about 7 days of consecutive failures [UNVERIFIED]).

**Merchant-side idempotent processing**:
```
// Pseudocode for webhook handler
function handleWebhook(event):
    // 1. Verify signature
    if !verifySignature(event, webhookSecret):
        return 401

    // 2. Check if we've already processed this event
    if eventAlreadyProcessed(event.id):
        return 200  // Acknowledge, do nothing

    // 3. Process the event
    switch event.type:
        case "payment.succeeded":
            fulfillOrder(event.data.object.metadata.order_id)
        case "refund.succeeded":
            updateRefundStatus(event.data.object)
        ...

    // 4. Record that we processed this event
    markEventProcessed(event.id)

    return 200  // Must return 2xx quickly (within 5-20 seconds)
```

---

### `GET /webhooks/{webhookId}` — Retrieve Webhook Endpoint

```
GET /v1/webhooks/whk_1NqYkR2eZvKYlo2C
```

**Response** (`200 OK`):
```json
{
  "id": "whk_1NqYkR2eZvKYlo2C",
  "object": "webhook_endpoint",
  "url": "https://merchant.com/webhooks/payments",
  "events": ["payment.succeeded", "payment.failed", "refund.created"],
  "status": "enabled",
  "api_version": "2025-01-15",
  "created_at": "2025-07-14T00:00:00Z"
}
```

Note: the `secret` is only returned on creation (POST response). It cannot be retrieved again. If lost, the merchant must rotate it (create a new endpoint or roll the secret).

---

### `DELETE /webhooks/{webhookId}` — Delete a Webhook Endpoint

```
DELETE /v1/webhooks/whk_1NqYkR2eZvKYlo2C
```

**Response** (`200 OK`):
```json
{
  "id": "whk_1NqYkR2eZvKYlo2C",
  "object": "webhook_endpoint",
  "deleted": true
}
```

---

### `GET /events` — List Events (Paginated)

The event log serves as a fallback when webhooks fail. Merchants can poll this endpoint to reconcile — "did I miss any events?"

```
GET /v1/events?type=payment.succeeded&created[gte]=2025-07-15T00:00:00Z&limit=50
```

**Response** (`200 OK`):
```json
{
  "object": "list",
  "url": "/v1/events",
  "has_more": true,
  "data": [
    {
      "id": "evt_abc001",
      "type": "payment.succeeded",
      "created_at": "2025-07-15T10:30:02Z",
      "data": { "object": { "id": "pay_1NqYkR2eZvKYlo2C", ... } }
    },
    {
      "id": "evt_abc002",
      "type": "payment.succeeded",
      "created_at": "2025-07-15T10:31:15Z",
      "data": { "object": { "id": "pay_2MqZlS3fAWLZmp3D", ... } }
    }
  ]
}
```

Stripe retains events for 30 days. Razorpay provides a similar `/events` endpoint but also offers a webhook dashboard with delivery logs.

**The three notification channels**: A well-designed PSP provides all three:
1. **Webhooks** — push notifications for real-time processing
2. **Event API** — pull-based for reconciliation and missed events
3. **Dashboard** — UI for debugging and manual checks

---

## 6. Dispute / Chargeback APIs

A dispute (chargeback) occurs when a cardholder contacts their issuing bank to contest a charge. The issuer notifies the card network, which notifies the acquirer, which notifies the PSP. The PSP must notify the merchant and manage the evidence submission process.

### `GET /disputes/{disputeId}` — Retrieve Dispute Details

```
GET /v1/disputes/dis_1NqYkR2eZvKYlo2C
```

**Response** (`200 OK`):
```json
{
  "id": "dis_1NqYkR2eZvKYlo2C",
  "object": "dispute",
  "status": "needs_response",
  "amount": 10000,
  "currency": "usd",
  "payment_id": "pay_1NqYkR2eZvKYlo2C",
  "reason": "fraudulent",
  "evidence_details": {
    "due_by": "2025-08-05T23:59:59Z",
    "has_evidence": false,
    "past_due": false,
    "submission_count": 0
  },
  "evidence": {
    "customer_name": null,
    "customer_email": null,
    "billing_address": null,
    "receipt": null,
    "shipping_documentation": null,
    "customer_communication": null,
    "uncategorized_text": null
  },
  "balance_transactions": [
    {
      "id": "txn_dispute_debit_001",
      "type": "dispute",
      "amount": -10000,
      "description": "Dispute funds withdrawn"
    },
    {
      "id": "txn_dispute_fee_001",
      "type": "dispute_fee",
      "amount": -1500,
      "description": "Dispute fee"
    }
  ],
  "created_at": "2025-07-20T14:00:00Z",
  "is_charge_refundable": false
}
```

**Dispute statuses** (Stripe's actual statuses): `warning_needs_response`, `warning_under_review`, `warning_closed`, `needs_response`, `under_review`, `won`, `lost`. The `warning_*` statuses are for Early Fraud Warnings (Visa's TC40 / Mastercard's SAFE reports) which notify the merchant of potential fraud before a formal dispute.

**Reason codes**: `fraudulent`, `product_not_received`, `product_unacceptable`, `duplicate`, `subscription_canceled`, `unrecognized`, `credit_not_processed`, `general`. These map to card network reason codes (Visa has ~20 reason codes, Mastercard has ~40 [UNVERIFIED — exact counts vary]).

**Dispute fee**: Stripe charges $15.00 per dispute regardless of outcome [UNVERIFIED — Stripe's dispute fee was $15 as of 2024, may have changed]. This fee is in addition to the disputed amount being withdrawn from the merchant's balance. If the merchant wins the dispute, the disputed amount is returned, but the dispute fee is NOT refunded (as of Stripe's current policy [UNVERIFIED]).

---

### `POST /disputes/{disputeId}/respond` — Submit Dispute Evidence

**Request**:
```json
POST /v1/disputes/dis_1NqYkR2eZvKYlo2C/respond

{
  "evidence": {
    "customer_name": "John Doe",
    "customer_email": "john@example.com",
    "billing_address": "123 Main St, San Francisco, CA 94105",
    "receipt": "file_receipt_abc123",
    "shipping_documentation": "file_tracking_xyz789",
    "shipping_carrier": "fedex",
    "shipping_tracking_number": "794644790132",
    "shipping_date": "2025-07-16",
    "customer_communication": "file_email_thread_001",
    "uncategorized_text": "Customer received the order on July 18. Delivery confirmation attached. The customer's claim of 'product not received' is contradicted by the signed delivery proof.",
    "service_documentation": null,
    "refund_policy": "file_refund_policy_002",
    "refund_policy_disclosure": "file_checkout_screenshot_003"
  },
  "submit": true
}
```

**Key fields**:

| Field | Type | Description |
|-------|------|-------------|
| `evidence.*` | various | Evidence fields. File references (`file_...`) point to uploaded documents (images, PDFs). Text fields for narrative evidence. |
| `submit` | boolean | If `true`, submits the evidence to the card network (final — cannot be edited after). If `false`, saves as draft (can be updated). Stripe allows only ONE submission per dispute. |

**Response** (`200 OK`):
```json
{
  "id": "dis_1NqYkR2eZvKYlo2C",
  "object": "dispute",
  "status": "under_review",
  "evidence_details": {
    "due_by": "2025-08-05T23:59:59Z",
    "has_evidence": true,
    "submission_count": 1
  }
}
```

**Deadlines**: The merchant typically has 7-21 days to respond to a dispute, depending on the card network and reason code [UNVERIFIED — Stripe docs mention that evidence must be submitted before `evidence_details.due_by`]. If the deadline passes without a response, the merchant automatically loses the dispute.

**Win rates**: Industry average dispute win rate is approximately 20-30% [UNVERIFIED]. Win rates vary significantly by reason code — "product not received" with delivery proof wins more often than "fraudulent" without 3DS authentication.

---

### `POST /disputes/{disputeId}/accept` — Accept the Chargeback

The merchant acknowledges the chargeback and does not contest it.

```json
POST /v1/disputes/dis_1NqYkR2eZvKYlo2C/accept
```

**Response** (`200 OK`):
```json
{
  "id": "dis_1NqYkR2eZvKYlo2C",
  "object": "dispute",
  "status": "lost",
  "amount": 10000,
  "currency": "usd"
}
```

In Stripe's API, the equivalent is `POST /v1/disputes/{id}/close`. Once a dispute is accepted/closed, the disputed amount remains debited from the merchant's balance.

---

### `GET /disputes` — List Disputes (Paginated)

```
GET /v1/disputes?status=needs_response&limit=25
```

Useful for merchants to see all open disputes that need attention.

---

## 7. Payout / Settlement APIs

Settlement is the process of moving captured funds from the PSP's pooled account to the merchant's bank account. The PSP holds all captured payments in a pooled (FBO — For Benefit Of) account and periodically pays out to merchants based on their settlement schedule.

### `POST /payouts` — Initiate a Payout

**Request**:
```json
POST /v1/payouts
Idempotency-Key: "payout-550e8400-e29b-41d4-a716-446655440004"

{
  "amount": 500000,
  "currency": "usd",
  "destination": "ba_bank_account_001",
  "description": "Weekly settlement — Week 29",
  "method": "standard",
  "metadata": {
    "settlement_period": "2025-07-08_to_2025-07-14"
  }
}
```

**Key fields**:

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `amount` | integer | Yes | Amount in smallest currency unit. |
| `currency` | string | Yes | Must match the merchant's payout currency. |
| `destination` | string | No | Bank account or card to pay out to. If omitted, uses the merchant's default payout destination. |
| `method` | enum | No | `"standard"` (1-2 business days) or `"instant"` (minutes, for eligible banks, higher fee). Stripe supports both. |
| `description` | string | No | Appears on the merchant's bank statement. |

**Response** (`201 Created`):
```json
{
  "id": "po_1NqYkR2eZvKYlo2C",
  "object": "payout",
  "status": "pending",
  "amount": 500000,
  "currency": "usd",
  "destination": "ba_bank_account_001",
  "method": "standard",
  "arrival_date": "2025-07-17T00:00:00Z",
  "description": "Weekly settlement — Week 29",
  "balance_transaction": "txn_payout_abc123",
  "created_at": "2025-07-15T06:00:00Z"
}
```

**Payout statuses**: `pending` → `in_transit` → `paid` or `failed` or `canceled`. Stripe's actual payout statuses are these.

**Automatic vs manual payouts**: Stripe defaults to automatic daily payouts (the platform calculates the available balance and initiates a payout every day). Merchants can switch to manual payouts for more control. Razorpay defaults to automatic settlement on a T+2 schedule for most merchants.

---

### `GET /payouts/{payoutId}` — Retrieve Payout Details

```
GET /v1/payouts/po_1NqYkR2eZvKYlo2C
```

**Response** (`200 OK`):
```json
{
  "id": "po_1NqYkR2eZvKYlo2C",
  "object": "payout",
  "status": "paid",
  "amount": 500000,
  "currency": "usd",
  "arrival_date": "2025-07-17T00:00:00Z",
  "method": "standard",
  "type": "bank_account",
  "created_at": "2025-07-15T06:00:00Z"
}
```

---

### `GET /balance` — Retrieve Account Balance

**Request**:
```
GET /v1/balance
```

**Response** (`200 OK`):
```json
{
  "object": "balance",
  "available": [
    {
      "amount": 500000,
      "currency": "usd",
      "source_types": {
        "card": 450000,
        "bank_account": 50000
      }
    }
  ],
  "pending": [
    {
      "amount": 150000,
      "currency": "usd",
      "source_types": {
        "card": 150000
      }
    }
  ],
  "reserved": [
    {
      "amount": 25000,
      "currency": "usd"
    }
  ],
  "livemode": true
}
```

**Balance types**:

| Type | Description |
|------|-------------|
| `available` | Funds that can be paid out to the merchant's bank account right now. These are captured payments that have cleared the settlement cycle. |
| `pending` | Funds from recent payments that haven't completed the settlement cycle yet. Will move to `available` after the settlement period (T+1 to T+7 depending on merchant tier). |
| `reserved` | Funds held back as a reserve against potential disputes, refunds, or risk. The PSP determines the reserve percentage based on merchant risk assessment. |

**Settlement cycles**:

| Merchant Tier | Typical Cycle | Notes |
|--------------|---------------|-------|
| Low risk (established) | T+1 to T+2 | Standard for established merchants on Stripe [UNVERIFIED] |
| Medium risk (new merchants) | T+3 to T+5 | Longer hold for newer merchants with limited history |
| High risk (certain verticals) | T+7 or longer | Travel, gambling, adult content — higher chargeback risk verticals |
| India (Razorpay) | T+2 to T+3 | Standard settlement cycle for most Indian merchants [UNVERIFIED] |

---

## 8. Subscription / Recurring Payment APIs

Subscriptions automate recurring charges. The PSP creates invoices on a schedule, charges the customer's saved payment method, and handles failed payment retries (dunning).

### `POST /subscriptions` — Create a Subscription

**Request**:
```json
POST /v1/subscriptions

{
  "customer_id": "cus_abc123",
  "plan_id": "plan_pro_monthly",
  "payment_method": "pm_card_visa_4242",
  "billing_cycle_anchor": "2025-08-01T00:00:00Z",
  "trial_period_days": 14,
  "proration_behavior": "create_prorations",
  "default_tax_rates": ["txr_gst_18"],
  "metadata": {
    "referral_code": "FRIEND50"
  },
  "cancel_at_period_end": false,
  "collection_method": "charge_automatically"
}
```

**Key fields**:

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `customer_id` | string | Yes | Must have a saved payment method. |
| `plan_id` | string | Yes | References a pre-created pricing plan (amount, interval, currency). Stripe calls these "Prices" (the newer API) or "Plans" (the older API). |
| `payment_method` | string | No | Override the customer's default payment method for this subscription. |
| `billing_cycle_anchor` | timestamp | No | The date the billing cycle starts. Useful for aligning all customers to the 1st of the month. |
| `trial_period_days` | integer | No | Free trial period. No charge until trial ends. |
| `proration_behavior` | enum | No | `"create_prorations"` (default — charge/credit for mid-cycle plan changes), `"none"` (no proration), `"always_invoice"` (immediately invoice prorated amounts). |
| `collection_method` | enum | No | `"charge_automatically"` (default — auto-charge the payment method) or `"send_invoice"` (send an invoice email, customer pays manually). |

**Response** (`201 Created`):
```json
{
  "id": "sub_1NqYkR2eZvKYlo2C",
  "object": "subscription",
  "status": "trialing",
  "customer_id": "cus_abc123",
  "plan": {
    "id": "plan_pro_monthly",
    "amount": 2999,
    "currency": "usd",
    "interval": "month",
    "interval_count": 1,
    "product": "prod_pro_plan"
  },
  "current_period_start": "2025-08-01T00:00:00Z",
  "current_period_end": "2025-09-01T00:00:00Z",
  "trial_start": "2025-07-15T00:00:00Z",
  "trial_end": "2025-07-29T00:00:00Z",
  "default_payment_method": "pm_card_visa_4242",
  "latest_invoice": "inv_abc001",
  "cancel_at_period_end": false,
  "canceled_at": null,
  "created_at": "2025-07-15T00:00:00Z"
}
```

**Subscription statuses**: `trialing` → `active` → `past_due` → `canceled` or `unpaid`. Stripe's actual statuses include `incomplete`, `incomplete_expired`, `trialing`, `active`, `past_due`, `canceled`, `unpaid`, `paused`.

---

### `GET /subscriptions/{subscriptionId}` — Retrieve Subscription

```
GET /v1/subscriptions/sub_1NqYkR2eZvKYlo2C
```

Returns the full subscription object as shown above.

---

### `PUT /subscriptions/{subscriptionId}` — Update / Change Plan

Used for upgrades, downgrades, and configuration changes.

**Request** (upgrade from Pro Monthly to Enterprise Monthly):
```json
PUT /v1/subscriptions/sub_1NqYkR2eZvKYlo2C

{
  "plan_id": "plan_enterprise_monthly",
  "proration_behavior": "create_prorations"
}
```

**Proration**: If a customer upgrades mid-cycle, proration calculates the credit for the unused portion of the old plan and the charge for the remaining portion of the new plan. Example:
- Old plan: $29.99/month, 15 days used → credit $15.00
- New plan: $99.99/month, 15 days remaining → charge $50.00
- Net charge on next invoice: $35.00

**Response** (`200 OK`):
```json
{
  "id": "sub_1NqYkR2eZvKYlo2C",
  "object": "subscription",
  "status": "active",
  "plan": {
    "id": "plan_enterprise_monthly",
    "amount": 9999,
    "currency": "usd",
    "interval": "month"
  },
  "pending_update": null
}
```

---

### `POST /subscriptions/{subscriptionId}/cancel` — Cancel a Subscription

**Request**:
```json
POST /v1/subscriptions/sub_1NqYkR2eZvKYlo2C/cancel

{
  "cancel_at_period_end": true,
  "cancellation_reason": "too_expensive"
}
```

| Field | Type | Description |
|-------|------|-------------|
| `cancel_at_period_end` | boolean | If `true`, the subscription stays active until the current billing period ends, then cancels. If `false` (or `cancel_immediately: true`), cancels immediately with a prorated credit. |
| `cancellation_reason` | string | For analytics — `too_expensive`, `missing_features`, `switched_service`, `unused`, `customer_service`, `other`. |

**Response** (`200 OK`):
```json
{
  "id": "sub_1NqYkR2eZvKYlo2C",
  "object": "subscription",
  "status": "active",
  "cancel_at_period_end": true,
  "canceled_at": "2025-07-15T14:00:00Z",
  "cancel_at": "2025-09-01T00:00:00Z"
}
```

In Stripe's actual API, cancellation is done via `DELETE /v1/subscriptions/{id}` for immediate cancellation, or `POST /v1/subscriptions/{id}` with `cancel_at_period_end: true` for end-of-period cancellation.

---

### Dunning (Failed Payment Retry Logic)

When a subscription payment fails (e.g., expired card, insufficient funds), the PSP enters a **dunning** cycle:

1. **First attempt fails** — send email notification to customer ("Update your payment method").
2. **Retry 1** — 3 days later [UNVERIFIED — Stripe's default Smart Retries timing varies].
3. **Retry 2** — 5 days after first retry.
4. **Retry 3** — 7 days after second retry.
5. **Final failure** — mark subscription as `past_due` or `unpaid`. Optionally cancel.

Stripe's "Smart Retries" uses ML to determine the optimal retry time (e.g., retry on payday, retry when the issuer is more likely to approve). Stripe claims Smart Retries recover up to 41% of failed subscription payments [UNVERIFIED — this figure is from Stripe's marketing materials, exact recovery rate varies].

**Grace period**: The merchant can configure a grace period (e.g., 7 days) during which the subscription remains active even if payment is past due. During grace period, the service continues while retries happen.

---

## 9. Ledger / Reporting APIs (Internal)

These are internal APIs — not exposed to merchants. They power the financial backbone of the PSP.

### ** `GET /ledger/entries` — Query Ledger Entries

Every financial event (payment captured, refund issued, fee charged, payout sent, dispute deducted) creates a pair of balanced ledger entries (double-entry bookkeeping).

**Request**:
```
GET /internal/v1/ledger/entries?payment_id=pay_1NqYkR2eZvKYlo2C&limit=20
```

**Response** (`200 OK`):
```json
{
  "object": "list",
  "data": [
    {
      "id": "le_001",
      "type": "payment_capture",
      "payment_id": "pay_1NqYkR2eZvKYlo2C",
      "entries": [
        {
          "account": "merchant_pending:cus_abc123",
          "direction": "credit",
          "amount": 9710,
          "currency": "usd",
          "description": "Payment captured (net of fees)"
        },
        {
          "account": "psp_revenue:fees",
          "direction": "credit",
          "amount": 290,
          "currency": "usd",
          "description": "Processing fee (2.9% of $100)"
        },
        {
          "account": "acquirer_receivable:acq_001",
          "direction": "debit",
          "amount": 10000,
          "currency": "usd",
          "description": "Funds receivable from acquirer"
        }
      ],
      "balance_check": {
        "total_debits": 10000,
        "total_credits": 10000,
        "balanced": true
      },
      "created_at": "2025-07-15T10:30:02Z",
      "event_id": "evt_abc001"
    },
    {
      "id": "le_002",
      "type": "refund",
      "payment_id": "pay_1NqYkR2eZvKYlo2C",
      "entries": [
        {
          "account": "merchant_balance:cus_abc123",
          "direction": "debit",
          "amount": 5000,
          "currency": "usd",
          "description": "Partial refund deducted from merchant balance"
        },
        {
          "account": "refund_payable:customer",
          "direction": "credit",
          "amount": 5000,
          "currency": "usd",
          "description": "Refund payable to customer's card"
        }
      ],
      "balance_check": {
        "total_debits": 5000,
        "total_credits": 5000,
        "balanced": true
      },
      "created_at": "2025-07-15T12:00:00Z",
      "event_id": "evt_abc002"
    }
  ]
}
```

**The golden rule**: `total_debits == total_credits` for EVERY ledger entry set. If this invariant ever breaks, something is seriously wrong. Reconciliation will catch it.

Ledger entries are **append-only and immutable**. Corrections are made by creating new compensating entries, never by editing or deleting existing entries. This provides a complete, tamper-evident audit trail.

---

### `GET /reports/settlement` — Settlement Report

**Request**:
```
GET /internal/v1/reports/settlement?merchant_id=merch_001&date=2025-07-15
```

**Response** (`200 OK`):
```json
{
  "merchant_id": "merch_001",
  "settlement_date": "2025-07-15",
  "currency": "usd",
  "summary": {
    "gross_volume": 5000000,
    "refunds": -150000,
    "disputes": -25000,
    "dispute_fees": -750,
    "processing_fees": -145000,
    "net_settlement": 4679250
  },
  "transaction_count": {
    "payments_captured": 1520,
    "refunds_issued": 45,
    "disputes_opened": 5,
    "disputes_won": 2,
    "disputes_lost": 1
  },
  "payout": {
    "id": "po_settlement_0715",
    "amount": 4679250,
    "status": "in_transit",
    "expected_arrival": "2025-07-17T00:00:00Z"
  }
}
```

---

### `GET /reports/reconciliation` — Reconciliation Report

Three-way reconciliation: (1) internal ledger vs (2) acquirer/bank statements vs (3) card network settlement files.

**Request**:
```
GET /internal/v1/reports/reconciliation?date=2025-07-15&acquirer=acq_001
```

**Response** (`200 OK`):
```json
{
  "date": "2025-07-15",
  "acquirer": "acq_001",
  "status": "discrepancies_found",
  "summary": {
    "internal_ledger_total": 5000000,
    "acquirer_statement_total": 4998500,
    "card_network_total": 5000000,
    "discrepancy_amount": 1500
  },
  "discrepancies": [
    {
      "type": "missing_in_acquirer",
      "payment_id": "pay_xyz789",
      "internal_amount": 1500,
      "acquirer_amount": 0,
      "likely_reason": "timing_difference",
      "resolution": "pending",
      "notes": "Transaction captured at 23:58 UTC, likely in next day's acquirer batch"
    }
  ],
  "matched_transactions": 1519,
  "unmatched_transactions": 1
}
```

**Common discrepancy causes**:
- **Timing differences**: Transaction captured late in the day, appears in next day's acquirer batch
- **Currency conversion differences**: PSP and acquirer use different exchange rate snapshots
- **Fee calculation differences**: Rounding differences in interchange + markup calculation
- **Partial captures**: Amount captured differs from amount authorized
- **Genuine errors**: Double processing, missing settlements (rare but serious)

Reconciliation is the "unsung hero" of payment systems. It catches problems that no amount of real-time monitoring can find.

---

## 10. Admin / Ops APIs (Internal)

Internal APIs for platform operations, configuration, and monitoring. Not exposed to merchants.

### `GET /health` — Health Check

```
GET /internal/v1/health
```

**Response** (`200 OK`):
```json
{
  "status": "healthy",
  "timestamp": "2025-07-15T10:35:00Z",
  "components": {
    "api_server": { "status": "healthy", "latency_ms": 2 },
    "database_primary": { "status": "healthy", "latency_ms": 5, "replication_lag_ms": 12 },
    "database_replica": { "status": "healthy", "latency_ms": 3 },
    "redis_cluster": { "status": "healthy", "latency_ms": 1 },
    "kafka_cluster": { "status": "healthy", "lag": 150 },
    "token_vault": { "status": "healthy", "latency_ms": 8 },
    "acquirer_a": { "status": "healthy", "success_rate_1h": 0.967, "p99_latency_ms": 450 },
    "acquirer_b": { "status": "degraded", "success_rate_1h": 0.891, "p99_latency_ms": 1200 },
    "fraud_engine": { "status": "healthy", "p99_latency_ms": 35 }
  }
}
```

### `GET /metrics` — Platform Metrics

```
GET /internal/v1/metrics?window=1h
```

**Response** (`200 OK`):
```json
{
  "window": "1h",
  "timestamp": "2025-07-15T10:35:00Z",
  "payments": {
    "total_attempts": 45230,
    "succeeded": 43120,
    "failed": 1890,
    "requires_action": 220,
    "success_rate": 0.9534,
    "p50_latency_ms": 180,
    "p95_latency_ms": 420,
    "p99_latency_ms": 780,
    "total_volume_usd": 12500000
  },
  "refunds": {
    "total": 1250,
    "total_volume_usd": 350000
  },
  "disputes": {
    "opened": 12,
    "rate": 0.00028
  },
  "webhooks": {
    "delivered": 89500,
    "failed": 340,
    "delivery_rate": 0.9962,
    "p99_delivery_latency_ms": 2500
  },
  "acquirer_breakdown": {
    "acq_001": { "volume": 28000, "success_rate": 0.967, "avg_latency_ms": 195 },
    "acq_002": { "volume": 17230, "success_rate": 0.932, "avg_latency_ms": 240 }
  }
}
```

---

### `POST /config/routing-rules` — Configure Payment Routing

**Request**:
```json
POST /internal/v1/config/routing-rules

{
  "rules": [
    {
      "name": "visa_domestic_us",
      "conditions": {
        "card_network": "visa",
        "card_country": "US",
        "merchant_country": "US"
      },
      "acquirer_priority": ["acq_001", "acq_002"],
      "cascade_on_soft_decline": true
    },
    {
      "name": "mastercard_international",
      "conditions": {
        "card_network": "mastercard",
        "card_country": { "$ne": "US" }
      },
      "acquirer_priority": ["acq_003", "acq_001"],
      "cascade_on_soft_decline": true
    },
    {
      "name": "amex_all",
      "conditions": {
        "card_network": "amex"
      },
      "acquirer_priority": ["acq_amex_direct"],
      "cascade_on_soft_decline": false
    },
    {
      "name": "high_value_transactions",
      "conditions": {
        "amount": { "$gte": 100000 }
      },
      "acquirer_priority": ["acq_001"],
      "cascade_on_soft_decline": false,
      "additional_fraud_check": true
    }
  ],
  "fallback_acquirer": "acq_001",
  "dynamic_routing_enabled": true,
  "dynamic_routing_window_minutes": 30
}
```

**Key concepts**:
- **Priority-based routing**: Try acquirers in order. If the first declines (soft decline), cascade to the next.
- **Cascade on soft decline only**: Hard declines (stolen card, insufficient funds) should NOT be retried — they will fail again and may trigger card network monitoring.
- **Dynamic routing**: Override static rules based on real-time acquirer success rates over a sliding window.

---

### `POST /config/risk-rules` — Configure Fraud Detection Rules

**Request**:
```json
POST /internal/v1/config/risk-rules

{
  "rules": [
    {
      "name": "velocity_check",
      "type": "velocity",
      "condition": "same_card_fingerprint",
      "threshold": 5,
      "window_minutes": 10,
      "action": "block"
    },
    {
      "name": "high_amount_3ds",
      "type": "amount_threshold",
      "condition": { "amount": { "$gte": 50000 } },
      "action": "require_3ds"
    },
    {
      "name": "geo_mismatch",
      "type": "geo_mismatch",
      "condition": "card_country != ip_country",
      "action": "flag_for_review",
      "risk_score_adjustment": 30
    },
    {
      "name": "known_fraud_bins",
      "type": "blocklist",
      "condition": { "bin": { "$in": ["411111", "400000"] } },
      "action": "block"
    }
  ],
  "ml_model_enabled": true,
  "ml_model_version": "v3.2.1",
  "ml_block_threshold": 85,
  "ml_review_threshold": 65
}
```

**Risk rule actions**:
- `block` — decline the transaction immediately
- `require_3ds` — trigger 3D Secure authentication challenge
- `flag_for_review` — allow the transaction but flag for manual review
- `allow` — override and allow (used for whitelisting)

---

## 11. API Design Philosophy — Stripe vs PayPal vs Razorpay

### Stripe: Developer/Merchant-Centric, API-First

- **Philosophy**: The API IS the product. Every feature is API-accessible. The dashboard is built on top of the same API merchants use.
- **Design principles**: Predictable resource-oriented URLs, consistent JSON responses, versioned APIs (date-based versioning like `2025-01-15`), comprehensive error messages with `doc_url`.
- **Payment method agnostic**: Stripe abstracts away the payment method. Whether it's a card, bank transfer, UPI, or wallet, the PaymentIntent API works the same way. The payment method is a pluggable parameter.
- **Identity separation**: Stripe handles payments. It does NOT bundle buyer identity, shipping addresses, or buyer protection. The merchant owns the customer relationship.
- **Idempotency first-class**: Every mutating endpoint accepts an `Idempotency-Key` header. The documentation extensively covers retry behavior and idempotency semantics.

### PayPal: Buyer-Centric, Wallet + Identity Bundle

- **Philosophy**: PayPal is a **buyer platform** that also processes payments. The buyer has a PayPal account, a PayPal balance, buyer protection, and a PayPal checkout experience.
- **API model**: PayPal's API revolves around "Orders" (buyer-side intent) rather than "Payment Intents" (merchant-side intent). The flow is: create an Order → redirect buyer to PayPal to approve → capture the order. This three-party redirect flow is fundamentally different from Stripe's in-context (no redirect) model.
- **Buyer protection**: PayPal offers buyer protection (refund if item not received). This is a financial guarantee that neither Stripe nor Razorpay offers — they are merchant tools, not buyer tools.
- **Bundled identity**: PayPal knows the buyer's name, email, shipping address, and payment method. In PayPal checkout, the buyer logs in and all this information is pre-filled. For Stripe, the merchant must collect all this information.
- **Trade-off**: PayPal's model gives buyers confidence (especially for transactions with unknown merchants) but gives merchants less control over the UX. Stripe gives merchants full UX control but no buyer trust/protection layer.

### Razorpay: Stripe-like API + India-Specific Payment Infrastructure

- **Philosophy**: Similar to Stripe's API-first, developer-centric approach but deeply integrated with India's payment ecosystem.
- **India-specific payment methods**: UPI (Unified Payments Interface — real-time P2P/P2M, handled via intent or collect flow), Netbanking (direct bank login), Wallets (PayTM, PhonePe, Mobikwik), EMI (equated monthly installments on credit cards), Pay Later (credit lines), and RuPay cards.
- **Order-first flow**: Razorpay requires creating an Order server-side before initiating payment client-side. This is similar to PayPal's order model but without the redirect — Razorpay's checkout is an embedded modal.
- **Settlement infrastructure**: Razorpay handles merchant settlement within India's banking infrastructure, including NEFT/RTGS/IMPS transfers and integration with Indian banking rails.
- **API differences from Stripe**: Razorpay uses HTTP Basic Auth (not Bearer token), offset pagination (not cursor), and has some endpoints that don't support idempotency keys. Razorpay's API is well-designed but slightly less consistent than Stripe's.

### Summary Comparison

| Dimension | Stripe | PayPal | Razorpay |
|-----------|--------|--------|----------|
| **Orientation** | Merchant/developer | Buyer | Merchant/developer |
| **Core API object** | PaymentIntent | Order | Order + Payment |
| **Authentication** | Bearer token (API key) | OAuth 2.0 | HTTP Basic Auth |
| **Checkout UX** | Embedded (no redirect) | Redirect to PayPal | Embedded modal |
| **Buyer identity** | Not bundled | Bundled (PayPal account) | Not bundled |
| **Buyer protection** | No | Yes | No |
| **Idempotency** | First-class (`Idempotency-Key`) | Supported (`PayPal-Request-Id`) | Partial support |
| **Pagination** | Cursor-based | Cursor-based (HATEOAS links) | Offset-based |
| **Versioning** | Date-based (`2025-01-15`) | Date-based | Version in URL (`/v1/`) |
| **Payment methods** | Global (cards, bank transfers, wallets) | PayPal wallet + cards | India-focused (UPI, Netbanking, Wallets) + cards |
| **Geographic focus** | Global (40+ countries) [UNVERIFIED] | Global | India (primary), expanding |

---

## 12. Interview Subset Summary

In the interview simulation (Phase 3), the candidate should design these endpoints in depth. The remaining endpoints in this doc provide context but are not expected in a 45-minute interview.

### Covered in Interview (**)

| # | Endpoint | Why It's in the Interview |
|---|----------|--------------------------|
| 1 | `POST /payments` (create payment intent) | Core payment flow. Tests: idempotency, amount handling, state machine design. |
| 2 | `POST /payments/{id}/capture` | Two-step payment flow. Tests: authorize vs capture, partial capture, real-world use cases. |
| 3 | `POST /payments/{id}/refund` | Reverse flow. Tests: understanding that refunds are new transactions, partial refunds, timelines. |
| 4 | `POST /webhooks` + delivery semantics | Async notifications. Tests: at-least-once delivery, idempotent processing, retry design. |
| 5 | `GET /ledger/entries` | Financial integrity. Tests: double-entry bookkeeping, immutability, reconciliation awareness. |

### Not Covered in Interview (But Good to Know)

| API Group | Why It Matters | When to Mention |
|-----------|---------------|-----------------|
| Payment Methods | PCI DSS compliance, tokenization architecture | Deep dive on security |
| Customers | Data model design | If asked about merchant integration |
| Disputes | Chargeback lifecycle, deadlines, evidence | Deep dive on financial operations |
| Payouts / Balance | Settlement cycles, merchant cash flow | Deep dive on settlement |
| Subscriptions | Dunning, proration, billing complexity | If asked about recurring payments |
| Routing rules | Multi-acquirer optimization | Deep dive on payment routing |
| Risk rules | Fraud detection configuration | Deep dive on fraud |
| Reconciliation | Financial integrity verification | Deep dive on ledger |

---

## Appendix: Verified Sources and Accuracy Notes

This document was written with reference to Stripe's public API documentation (docs.stripe.com/api), Razorpay's API documentation (razorpay.com/docs/api), and PayPal's developer documentation (developer.paypal.com). Web search and fetch tools were unavailable during authoring, so the following accuracy caveats apply:

**Verified from known API documentation**:
- Stripe PaymentIntent statuses and lifecycle
- Stripe's idempotency key mechanism (24-hour retention)
- Stripe's error object structure (type, code, message, param, doc_url)
- Stripe's cursor-based pagination model
- Stripe's webhook signature verification (HMAC-SHA256, `Stripe-Signature` header)
- Razorpay's Order-first payment flow
- Razorpay's HTTP Basic Auth authentication
- Double-entry bookkeeping principles for payment ledgers
- PCI DSS tokenization and CDE isolation requirements

**Marked [UNVERIFIED]** where noted inline:
- Exact Stripe webhook retry intervals and timeout durations
- Exact refund timeline durations by payment method
- Stripe's current processing fee on refunds
- Stripe dispute fee amount ($15)
- Smart Retries recovery rate (41%)
- Exact currency count (135+)
- Settlement cycle specifics by merchant tier
- Authorization validity windows
- Dispute win rate averages
- Card network reason code counts
