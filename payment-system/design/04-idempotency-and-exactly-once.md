# Idempotency & Exactly-Once Semantics in Payment Systems

> The single most important property of a payment system: **a payment must never be charged twice.**

This document covers the design and implementation of idempotency — the mechanism that prevents duplicate charges — and the broader quest for exactly-once semantics in a distributed payment platform. Everything here applies to systems like Stripe, Razorpay, Adyen, and any PSP that processes real money.

Note: Web search and web fetch were unavailable during authoring. Claims sourced from Stripe's published engineering blog (by Brandur Leach) and API documentation are based on the author's prior reading of those sources. Specific internal implementation details that cannot be verified against a public source are marked with [INFERRED].

---

## Table of Contents

1. [Why Idempotency Is Critical](#1-why-idempotency-is-critical)
2. [Idempotency Key Design](#2-idempotency-key-design)
3. [Idempotency Key Storage](#3-idempotency-key-storage)
4. [Idempotency at Each Layer](#4-idempotency-at-each-layer)
5. [Exactly-Once Semantics](#5-exactly-once-semantics)
6. [Payment State Machine](#6-payment-state-machine)
7. [Saga Pattern for Distributed Transactions](#7-saga-pattern-for-distributed-transactions)
8. [Timeout Handling](#8-timeout-handling)
9. [Contrast with CRUD](#9-contrast-with-crud)

---

## 1. Why Idempotency Is Critical

### The core problem

In any distributed system, messages can be delivered **more than once**. In a payment system, "more than once" means "charged more than once." This is not a theoretical edge case — it happens constantly:

| Failure mode | What happens | Without idempotency |
|---|---|---|
| **Network timeout** | Client sends payment request, server processes it, response is lost in transit. Client retries. | Customer charged twice. |
| **Client retry** | Mobile app shows spinner, user taps "Pay" again. | Customer charged twice. |
| **Load balancer retry** | LB's connection to backend drops mid-response. LB retries to a different backend. | Customer charged twice. |
| **Queue redelivery** | Kafka consumer processes a payment message, crashes before committing offset. Message redelivered. | Customer charged twice. |
| **DNS failover** | DNS TTL expires mid-request, retry goes to a different server that has no memory of the first attempt. | Customer charged twice. |

### Why this is uniquely dangerous for payments

- **Financial liability**: If you charge a customer $500 twice, you owe them $500. At scale, duplicate charges can cost millions.
- **Legal liability**: Double-charging violates consumer protection laws in most jurisdictions. Regulatory bodies (CFPB in the US, RBI in India) can impose fines.
- **Reputational damage**: A single viral social media post about being double-charged can erode customer trust across an entire merchant base.
- **Chargeback cascading**: Double-charged customers file chargebacks. High chargeback rates (>1%) trigger card network penalties. Excessive chargebacks can result in the merchant being blacklisted by Visa/Mastercard.

### Real-world examples

- A ride-sharing company's payment integration had a bug where network retries during peak hours caused approximately 0.1% of rides to be double-charged. At millions of rides per day, this translated to thousands of duplicate charges daily, costing millions in refunds, support costs, and customer goodwill. [INFERRED — composite of publicly discussed incidents]
- Stripe's engineering blog (Brandur Leach, "Designing Robust and Predictable APIs with Idempotency") describes how they designed their entire API around idempotency keys precisely because network failures are **not exceptional** — they are the normal operating condition of distributed systems. Stripe's published API documentation requires idempotency keys on all mutating endpoints for this reason.

### The fundamental insight

> In a payment system, **the network is not reliable, but the financial outcome must be.** Idempotency is the bridge between unreliable infrastructure and reliable financial operations.

---

## 2. Idempotency Key Design

### How it works

The client generates a unique identifier (the **idempotency key**) for each intended payment operation and sends it with every request. The server uses this key to detect retries and return the stored result instead of re-executing the operation.

```
POST /v1/payments
Idempotency-Key: 550e8400-e29b-41d4-a716-446655440000
Content-Type: application/json

{
  "amount": 5000,
  "currency": "usd",
  "payment_method": "pm_card_visa_4242",
  "merchant_id": "merch_abc123"
}
```

On retry (same idempotency key):
- Server looks up the key in the idempotency store.
- If found with a completed response, return the stored response. **Do not re-execute.**
- If found with an in-progress status, return `409 Conflict` (request is still being processed).
- If not found, proceed with normal execution.

### Why client-generated keys (not server-generated dedup)

Stripe uses **client-provided** idempotency keys. This is a deliberate design choice:

| Approach | Pros | Cons |
|---|---|---|
| **Client-provided key** (Stripe's approach) | Client controls retry semantics. Clear contract: same key = same operation. Works across multiple server instances. | Client must generate and manage keys. |
| **Server-generated dedup** (heuristic matching) | No client burden. | Server must define "duplicate" heuristically (same amount + same card + same merchant within 5 min?). Fragile — legitimate identical payments get deduplicated. |

The server-generated approach is fundamentally flawed for payments: a customer might legitimately buy two $25.00 coffees at the same cafe within 5 minutes. Heuristic dedup would incorrectly suppress the second charge. Client-provided keys make the idempotency contract **explicit and unambiguous**.

### Database schema for idempotency keys

```sql
CREATE TABLE idempotency_keys (
    idempotency_key   VARCHAR(255) PRIMARY KEY,   -- Client-provided UUID
    merchant_id       VARCHAR(64)  NOT NULL,       -- Scoped to merchant
    request_path      VARCHAR(255) NOT NULL,       -- e.g., "/v1/payments"
    request_hash      VARCHAR(64)  NOT NULL,       -- SHA-256 of request body

    -- Lifecycle
    status            VARCHAR(20)  NOT NULL,       -- 'started', 'completed', 'failed'
    locked_at         TIMESTAMP,                   -- For concurrent request detection

    -- Stored response (returned on retry)
    response_code     INT,
    response_body     JSONB,

    -- Linked resource
    payment_id        VARCHAR(64),                 -- The payment created by this request

    -- Timestamps
    created_at        TIMESTAMP    NOT NULL DEFAULT NOW(),
    updated_at        TIMESTAMP    NOT NULL DEFAULT NOW(),
    expires_at        TIMESTAMP    NOT NULL,        -- TTL for cleanup

    -- Ensure key is scoped per merchant
    UNIQUE (merchant_id, idempotency_key)
);

-- Index for cleanup job
CREATE INDEX idx_idempotency_expires ON idempotency_keys (expires_at)
    WHERE status != 'started';
```

Key design decisions in the schema:
- **Scoped to merchant**: The same UUID from two different merchants represents two different operations. `UNIQUE (merchant_id, idempotency_key)` enforces this.
- **`request_hash`**: If a client reuses the same idempotency key with a **different** request body (e.g., different amount), reject with `422 Unprocessable Entity`. This prevents accidental key reuse from silently returning a stale response.
- **`status` field**: Distinguishes between in-progress (`started`) and completed (`completed`/`failed`) requests. Critical for handling concurrent retries.
- **`locked_at`**: Enables detection of stale locks (if a server crashes mid-processing, the lock can be reclaimed after a timeout).

### Pseudocode: idempotency check flow

This is the core algorithm. It runs at the very beginning of every mutating API handler, **before any business logic**.

```
function handlePaymentRequest(request):
    key = request.header("Idempotency-Key")
    merchantId = request.authenticatedMerchantId()
    requestHash = sha256(canonicalize(request.body))

    if key is null:
        return error(400, "Idempotency-Key header is required")

    // ── STEP 1: Try to insert new idempotency record ──
    // Uses DB unique constraint to handle races atomically
    BEGIN TRANSACTION

    try:
        INSERT INTO idempotency_keys (
            idempotency_key, merchant_id, request_path,
            request_hash, status, locked_at, expires_at
        ) VALUES (
            key, merchantId, request.path,
            requestHash, 'started', NOW(),
            NOW() + INTERVAL '24 hours'
        )
        COMMIT
        // Key is new — proceed with normal processing (STEP 3)

    catch UniqueConstraintViolation:
        ROLLBACK
        // Key already exists — handle retry (STEP 2)

    // ── STEP 2: Key exists — determine what to do ──
    existingRecord = SELECT * FROM idempotency_keys
                     WHERE merchant_id = merchantId
                       AND idempotency_key = key
                     FOR UPDATE  // Row-level lock

    // 2a: Request body mismatch — client reused key incorrectly
    if existingRecord.request_hash != requestHash:
        return error(422, "Idempotency key reused with different request parameters")

    // 2b: Previous request completed — return stored response
    if existingRecord.status == 'completed':
        return response(existingRecord.response_code, existingRecord.response_body)

    // 2c: Previous request failed — allow retry (re-execute)
    if existingRecord.status == 'failed':
        UPDATE idempotency_keys
           SET status = 'started', locked_at = NOW()
         WHERE idempotency_key = key AND merchant_id = merchantId
        // Fall through to STEP 3

    // 2d: Previous request still in progress
    if existingRecord.status == 'started':
        // Check if the lock is stale (server crashed mid-processing)
        if existingRecord.locked_at < NOW() - INTERVAL '5 minutes':
            // Stale lock — reclaim and retry
            UPDATE idempotency_keys
               SET locked_at = NOW()
             WHERE idempotency_key = key AND merchant_id = merchantId
            // Fall through to STEP 3
        else:
            // Genuinely in-progress — tell client to wait
            return error(409, "A request with this idempotency key is already in progress")

    // ── STEP 3: Execute the actual payment logic ──
    try:
        result = executePayment(request)

        UPDATE idempotency_keys
           SET status = 'completed',
               response_code = result.statusCode,
               response_body = result.body,
               payment_id = result.paymentId,
               updated_at = NOW()
         WHERE idempotency_key = key AND merchant_id = merchantId

        return response(result.statusCode, result.body)

    catch Exception as e:
        UPDATE idempotency_keys
           SET status = 'failed',
               response_code = 500,
               response_body = {"error": e.message},
               updated_at = NOW()
         WHERE idempotency_key = key AND merchant_id = merchantId

        return error(500, e.message)
```

### Stripe's "atomic phases" pattern

Stripe's engineering blog (Brandur Leach) describes a more sophisticated version of the above called **atomic phases**. The idea is that a payment request progresses through multiple phases (validate, create record, call acquirer, update status), and each phase is wrapped in a database transaction that also updates the idempotency record's "recovery point." If the server crashes between phases, it can resume from the last completed recovery point instead of re-executing from scratch.

```
Recovery Points:
    RP_START           --> Idempotency key inserted
    RP_PAYMENT_CREATED --> Payment record created in DB
    RP_ACQUIRER_CALLED --> Acquirer returned auth code
    RP_COMPLETED       --> Final status updated, ledger entries created

On crash recovery:
    Read recovery point from idempotency record.
    Resume from that point (skip already-completed phases).
```

This is important because calling the acquirer is a **non-idempotent external side effect** — you do not want to call it twice. By recording that the acquirer call completed (with its result), a retry can skip the acquirer call entirely. [INFERRED — the exact internal recovery point names and implementation are not public; the pattern is described conceptually in the blog post.]

---

## 3. Idempotency Key Storage

### Storage requirements

| Requirement | Why |
|---|---|
| **Durable** | If the idempotency store loses data, retries will re-execute and double-charge. |
| **Fast reads** | Every single API request checks the idempotency store. Must be sub-millisecond. |
| **Consistent** | Two concurrent requests with the same key must not both "win" the insert race. |
| **Auto-expiring** | Keys cannot accumulate forever. Need TTL-based cleanup. |

### Two-tier storage architecture

```
┌─────────────────────────────────────────────────────────┐
│                    API Request                          │
│                        │                                │
│                        v                                │
│              ┌─────────────────┐                        │
│              │   Redis Cluster  │  Tier 1: Hot cache    │
│              │   (TTL: 24-48h)  │  Sub-ms lookup        │
│              │                   │  Handles 99%+ of      │
│              │   key --> status  │  dedup checks         │
│              └────────┬──────────┘                       │
│                       │ miss                             │
│                       v                                  │
│              ┌─────────────────┐                        │
│              │   PostgreSQL     │  Tier 2: Durable store │
│              │   (persistent)   │  Source of truth        │
│              │                   │  Audit trail           │
│              │   Full schema     │  Cleanup via           │
│              │   from above      │  expires_at index      │
│              └──────────────────┘                        │
└─────────────────────────────────────────────────────────┘
```

**Tier 1 — Redis** (hot path):
- Sub-millisecond lookups for the common case (new key, no collision).
- TTL of 24-48 hours. After TTL expires, the key is evicted from Redis. This is fine because retries after 48 hours are not legitimate retries — they are new operations.
- Redis is used as a **cache**, not the source of truth. The DB is authoritative.

**Tier 2 — PostgreSQL** (durable):
- The `idempotency_keys` table is the source of truth.
- On cache miss in Redis, check PostgreSQL.
- Retained longer than Redis TTL for audit purposes (30-90 days, depending on compliance requirements).
- Cleaned up by a background job that deletes expired, completed records.

### Why 24-48 hour TTL?

- **Too short** (e.g., 5 minutes): A payment that times out at minute 4 cannot be safely retried at minute 6.
- **Too long** (e.g., 30 days): Storage costs grow linearly. At millions of payments per day, 30 days of idempotency keys is enormous.
- **24-48 hours** covers all reasonable retry windows: client retries (seconds to minutes), queue redelivery (minutes to hours), and manual retry by operations (hours). Stripe's documentation states that idempotency keys are guaranteed to work for at least 24 hours, and recommends treating them as ephemeral after that.

### Race condition: two requests with the same key arrive simultaneously

This is the hardest problem in idempotency implementation. Two identical requests arrive at two different API servers at the same millisecond.

**Solution 1: Database unique constraint (preferred)**

```sql
INSERT INTO idempotency_keys (idempotency_key, merchant_id, status, ...)
VALUES ($key, $merchantId, 'started', ...);
-- If this succeeds, you own the key. Proceed.
-- If UniqueConstraintViolation, the other request owns it. Return 409.
```

The database's unique constraint provides an **atomic test-and-set**. Only one INSERT can succeed. The loser gets a constraint violation and knows to return 409 or wait.

This is exactly what the pseudocode in Section 2 does. It is the approach described in Stripe's engineering blog — using PostgreSQL's unique constraint as a distributed lock.

**Solution 2: Redis SETNX (for the hot path)**

```
SETNX idempotency:{merchantId}:{key} "started" EX 86400
-- Returns 1 if key was set (you own it). Proceed.
-- Returns 0 if key already existed (someone else owns it). Check status.
```

Redis `SETNX` (SET if Not eXists) is atomic and provides the same test-and-set semantics as a DB unique constraint, but faster. However, Redis is not durable — if Redis loses the key (crash before replication), both requests might succeed. This is why the DB constraint is the **authoritative** guard, and Redis is a fast-path optimization.

**Solution 3: Distributed lock (e.g., Redlock)**

Use a distributed lock (Redlock or ZooKeeper-based lock) on the idempotency key. The winner acquires the lock, processes the request, and releases the lock. The loser waits or returns 409.

This is heavier-weight than Solutions 1 and 2 and introduces lock management complexity (what if the lock holder crashes?). Generally not preferred when a DB unique constraint suffices. [INFERRED — Stripe appears to prefer DB-based locking over distributed locks for this use case.]

---

## 4. Idempotency at Each Layer

Idempotency is not a single check at the API layer. Duplicate execution can occur at **every layer** of the payment stack. Each layer needs its own idempotency mechanism.

```
┌───────────────────────────────────────────────────────────┐
│  CLIENT (Mobile App / Web)                                │
│  |-- Generates Idempotency-Key (UUID v4)                  │
│  |-- Retries on timeout with SAME key                     │
│  +-- NEVER retries with a NEW key (that would create a    │
│      new payment)                                         │
├───────────────────────────────────────────────────────────┤
│  LAYER 1: API GATEWAY / LOAD BALANCER                     │
│  |-- May auto-retry on 502/503 from backend               │
│  |-- Forwards Idempotency-Key header to backend           │
│  +-- Protection: Backend's idempotency check (Layer 2)    │
├───────────────────────────────────────────────────────────┤
│  LAYER 2: PAYMENT API SERVICE                             │
│  |-- Idempotency key check (Redis + DB)                   │
│  |-- This is the PRIMARY idempotency enforcement point    │
│  +-- Returns cached response on duplicate key             │
├───────────────────────────────────────────────────────────┤
│  LAYER 3: MESSAGE QUEUE (Kafka)                           │
│  |-- At-least-once delivery means duplicates are normal   │
│  |-- Consumer dedup via payment_id (unique constraint)    │
│  +-- Idempotent consumers: processing the same message    │
│      twice produces the same result                       │
├───────────────────────────────────────────────────────────┤
│  LAYER 4: DATABASE                                        │
│  |-- UNIQUE constraint on payment_id                      │
│  |-- UNIQUE constraint on (payment_id, state_transition)  │
│  |-- Prevents double-insertion even if application logic   │
│  |   somehow bypasses higher layers                       │
│  +-- This is the LAST LINE OF DEFENSE                     │
├───────────────────────────────────────────────────────────┤
│  LAYER 5: DOWNSTREAM PROCESSOR (Acquirer / Card Network)  │
│  |-- PSP sends a unique reference ID with each auth       │
│  |   request to the acquirer                              │
│  |-- Acquirer deduplicates on this reference ID           │
│  |-- If PSP retries the same auth (same reference ID),    │
│  |   acquirer returns the original auth response          │
│  +-- This prevents double-auth at the network level       │
└───────────────────────────────────────────────────────────┘
```

### Layer-by-layer details

**Layer 2 — API service (the main idempotency gate):**
This is where the `idempotency_keys` table and the pseudocode from Section 2 live. Every `POST /v1/payments` request is checked here.

**Layer 3 — Message queue consumers:**
Kafka guarantees at-least-once delivery. If a consumer crashes after processing a payment event but before committing its offset, Kafka redelivers the message. The consumer must be idempotent:

```
function onPaymentEvent(event):
    // Try to insert ledger entry with payment_id as natural key
    try:
        INSERT INTO ledger_entries (payment_id, type, amount, ...)
        VALUES (event.paymentId, 'CAPTURE', event.amount, ...)
    catch UniqueConstraintViolation:
        // Already processed — skip
        log.info("Duplicate event for payment {}, skipping", event.paymentId)
        return ACK
```

**Layer 4 — Database constraints:**
Even if all application-level checks fail (bugs, race conditions, configuration errors), the database's unique constraints are the **last line of defense** against duplicate financial records:

```sql
-- Only one payment per idempotency key per merchant
ALTER TABLE payments ADD CONSTRAINT uq_payment_idempotency
    UNIQUE (merchant_id, idempotency_key);

-- Only one ledger entry per payment per type (prevent double-posting)
ALTER TABLE ledger_entries ADD CONSTRAINT uq_ledger_entry
    UNIQUE (payment_id, entry_type, direction);

-- Only one state transition of a given type per payment
ALTER TABLE payment_state_transitions ADD CONSTRAINT uq_state_transition
    UNIQUE (payment_id, to_state);
```

**Layer 5 — Downstream acquirer:**
When the PSP sends an authorization request to the acquirer, it includes a unique reference identifier. If the PSP retries (because the response was lost), the acquirer uses this reference to return the original response. The exact mechanism varies by acquirer and card network. Most acquirers support some form of merchant-provided reference for deduplication. [INFERRED — exact field names and protocols vary by acquirer integration spec.]

### Defense in depth

The key principle is **defense in depth**: no single layer is trusted to be the sole guarantor of idempotency. Each layer has its own mechanism, and they overlap. If Layer 2 fails (bug in idempotency check), Layer 4 (DB constraint) catches it. If Layer 4 fails (schema migration removed the constraint), Layer 5 (acquirer dedup) catches it at the network level.

---

## 5. Exactly-Once Semantics

### The impossibility result

**True exactly-once delivery is impossible in distributed systems.** This is a consequence of the FLP impossibility result (Fischer, Lynch, Paterson, 1985), which proves that no deterministic protocol can guarantee consensus in an asynchronous system where even one process can fail.

In practical terms: if a client sends a payment request and the server processes it but the response is lost, neither the client nor the server can know with certainty whether the other party received the message. The client must retry, and the server must handle the retry. There is no protocol that avoids this.

### What payment systems actually achieve: "effectively-once"

Payment systems do not achieve true exactly-once. They achieve **effectively-once** through a combination of three mechanisms:

```
┌─────────────────────────────────────────────────────────┐
│              EFFECTIVELY-ONCE SEMANTICS                  │
│                                                         │
│   ┌─────────────────┐                                   │
│   │  At-Least-Once   │  Messages/requests ARE delivered  │
│   │  Delivery        │  at least once (via retries).     │
│   └────────┬─────────┘                                   │
│            │                                             │
│            v                                             │
│   ┌─────────────────┐                                   │
│   │  Idempotent      │  Processing the same request      │
│   │  Handlers        │  multiple times produces the      │
│   │                   │  same result as processing it     │
│   │                   │  once.                            │
│   └────────┬─────────┘                                   │
│            │                                             │
│            v                                             │
│   ┌─────────────────┐                                   │
│   │  Idempotency     │  Unique keys identify "the same   │
│   │  Keys            │  request" unambiguously.           │
│   └─────────────────┘                                   │
│                                                         │
│   Together: the operation EXECUTES exactly once          │
│   (even if the request is DELIVERED multiple times)      │
└─────────────────────────────────────────────────────────┘
```

**At-least-once delivery** ensures the request eventually reaches the server (via client retries, queue redelivery, etc.). Without this, requests can be silently lost.

**Idempotent handlers** ensure that processing a duplicate request is harmless — it returns the same result as the original without re-executing side effects (no second charge, no second ledger entry).

**Idempotency keys** provide the mechanism for handlers to identify duplicates — without explicit keys, the handler has no reliable way to distinguish a retry from a new request.

### The formula

```
Effectively-Once = At-Least-Once Delivery + Idempotent Processing
```

This is the standard pattern used by Stripe, Kafka (with exactly-once semantics enabled), and virtually every production payment system. Kafka's "exactly-once semantics" (introduced in KIP-98) is itself implemented as at-least-once delivery + idempotent producers + transactional consumers — the same pattern.

### What can go wrong

Even with effectively-once semantics, edge cases exist:

1. **Idempotency store failure**: If the idempotency store (Redis + DB) is unavailable, the system cannot check for duplicates. Options: (a) reject all requests until the store recovers (safe but causes downtime), or (b) proceed without dedup and accept risk of duplicates (unsafe). Most payment systems choose (a) — better to be unavailable than to double-charge.

2. **Key expiry before retry**: If the idempotency key expires (after 24-48 hours) and the client retries with the same key, the server treats it as a new request. This is by design — retries after 48 hours are not considered legitimate retries. The client should generate a new key and accept that this is a new operation.

3. **Partial execution**: The server creates a payment record but crashes before calling the acquirer. On retry, the idempotency check finds a `started` record with no response. The server must resume from the last recovery point (Stripe's atomic phases pattern from Section 2), not start over.

---

## 6. Payment State Machine

### Why a state machine

A payment is not a single event — it is a **lifecycle** with multiple stages. Each stage has specific rules about what transitions are valid. A state machine enforces these rules and prevents invalid transitions (e.g., you cannot capture a payment that was already refunded).

### State diagram

```
                         PAYMENT STATE MACHINE
  ┌──────────────────────────────────────────────────────────────────┐
  │                                                                  │
  │    ┌──────────┐                                                  │
  │    │          │                                                  │
  │    │ CREATED  ├──────────────────────┐                           │
  │    │          │                      │                           │
  │    └────┬─────┘                      │                           │
  │         │                            │                           │
  │     authorize                    auth_decline                    │
  │         │                            │                           │
  │         v                            v                           │
  │   ┌───────────┐              ┌────────────┐                      │
  │   │           │              │            │                      │
  │   │AUTHORIZED ├──────┐       │  DECLINED  │  (terminal)          │
  │   │           │      │       │            │                      │
  │   └─────┬─────┘      │       └────────────┘                      │
  │         │            │                                           │
  │     capture      void/cancel                                     │
  │         │            │                                           │
  │         v            v                                           │
  │   ┌───────────┐  ┌──────────┐                                    │
  │   │           │  │          │                                    │
  │   │ CAPTURED  │  │  VOIDED  │  (terminal)                        │
  │   │           │  │          │                                    │
  │   └──┬────┬───┘  └──────────┘                                    │
  │      │    │                                                      │
  │      │    └──── settle ──────────────────┐                       │
  │      │                                   │                       │
  │  partial_refund                          v                       │
  │   or full_refund                 ┌───────────────┐               │
  │      │                           │               │               │
  │      v                           │   SETTLED     │  (terminal)   │
  │  ┌───────────────┐               │               │               │
  │  │  PARTIALLY    │               └───────────────┘               │
  │  │  REFUNDED     ├────┐                                          │
  │  │               │    │                                          │
  │  └───────┬───────┘    │                                          │
  │          │         full_refund                                   │
  │     more_refund       │                                          │
  │          │            v                                          │
  │          │     ┌──────────────┐                                   │
  │          └────>│    FULLY     │                                   │
  │                │   REFUNDED   │  (terminal)                      │
  │                │              │                                   │
  │                └──────────────┘                                   │
  │                                                                  │
  │  TERMINAL STATES: DECLINED, VOIDED, FULLY_REFUNDED, SETTLED      │
  │  NON-TERMINAL:    CREATED, AUTHORIZED, CAPTURED,                 │
  │                   PARTIALLY_REFUNDED                              │
  └──────────────────────────────────────────────────────────────────┘
```

### Valid transitions table

| From State | To State | Trigger | Reversible? |
|---|---|---|---|
| `CREATED` | `AUTHORIZED` | Successful auth from issuer | No (can only void) |
| `CREATED` | `DECLINED` | Auth declined by issuer | No (terminal) |
| `AUTHORIZED` | `CAPTURED` | Merchant captures payment | No |
| `AUTHORIZED` | `VOIDED` | Merchant cancels before capture | No (terminal) |
| `CAPTURED` | `PARTIALLY_REFUNDED` | Partial refund issued | Can refund more |
| `CAPTURED` | `FULLY_REFUNDED` | Full refund issued | No (terminal) |
| `CAPTURED` | `SETTLED` | Settlement batch runs | Can still refund post-settlement |
| `PARTIALLY_REFUNDED` | `PARTIALLY_REFUNDED` | Additional partial refund | Can refund more |
| `PARTIALLY_REFUNDED` | `FULLY_REFUNDED` | Remaining amount refunded | No (terminal) |

### Implementation: atomic, monotonic transitions

```
function transitionPaymentState(paymentId, expectedCurrentState, newState):
    BEGIN TRANSACTION

    -- Lock the payment row
    current = SELECT status FROM payments
              WHERE payment_id = paymentId
              FOR UPDATE

    -- Validate transition
    if current.status != expectedCurrentState:
        ROLLBACK
        throw InvalidStateTransitionError(
            "Expected " + expectedCurrentState + ", found " + current.status)

    if not isValidTransition(current.status, newState):
        ROLLBACK
        throw InvalidStateTransitionError(
            current.status + " -> " + newState + " is not allowed")

    -- Perform transition
    UPDATE payments
       SET status = newState, updated_at = NOW()
     WHERE payment_id = paymentId

    -- Record the transition in the audit log
    INSERT INTO payment_state_transitions (
        payment_id, from_state, to_state, transitioned_at, actor
    ) VALUES (
        paymentId, expectedCurrentState, newState, NOW(), currentActor()
    )

    COMMIT
```

### Key properties

1. **Atomic**: State transition + audit log entry happen in a single DB transaction. Either both succeed or neither does.

2. **Monotonic**: States only move forward. You cannot go from `CAPTURED` back to `AUTHORIZED`. This prevents a class of bugs where concurrent operations (capture + void) race and produce an inconsistent state.

3. **Guarded**: The `expectedCurrentState` parameter implements optimistic locking. If two concurrent requests try to transition the same payment, only one succeeds — the other gets an `InvalidStateTransitionError` and must re-read the current state.

4. **Auditable**: Every transition is recorded with a timestamp and actor. This creates a complete timeline of every payment's lifecycle, essential for dispute resolution and compliance.

---

## 7. Saga Pattern for Distributed Transactions

### Why not 2PC (two-phase commit)?

A payment operation touches multiple systems: the PSP's database, the acquirer, the card network, and the issuer. Traditional distributed transactions (2PC) coordinate these with a prepare/commit protocol. This is problematic for payments:

| Problem with 2PC | Why it matters for payments |
|---|---|
| **Latency**: 2PC adds round trips. | Payment auth must complete in <2 seconds. 2PC over WAN to an acquirer is too slow. |
| **Availability**: If the coordinator crashes, all participants are blocked (holding locks). | A crashed coordinator means ALL payments stop. Unacceptable for a 99.99% SLA. |
| **Heterogeneous systems**: 2PC requires all participants to support the XA protocol. | Visa's card network does not implement XA. Neither do most acquirers. |
| **Lock duration**: 2PC holds locks for the duration of the transaction. | At thousands of TPS, long-held locks cause massive contention. |

### Saga pattern: compensating transactions

A **saga** is a sequence of local transactions where each step has a **compensating action** that undoes its effect if a later step fails. There is no global lock — each step commits independently.

### Payment capture saga: step by step

```
SAGA: Capture a previously authorized payment

Step 1: Update payment status to CAPTURE_INITIATED
  |-- Action:       UPDATE payments SET status = 'CAPTURE_INITIATED'
  |                 WHERE payment_id = ? AND status = 'AUTHORIZED'
  |-- Compensation: UPDATE payments SET status = 'AUTHORIZED'
  |                 WHERE payment_id = ? AND status = 'CAPTURE_INITIATED'
  +-- Notes:        Local DB transaction. Fast. Reversible.

Step 2: Send capture request to acquirer
  |-- Action:       POST /acquirer/capture { auth_code, amount, reference_id }
  |                 Wait for response (timeout: 30 seconds)
  |-- Compensation: POST /acquirer/void { auth_code, reference_id }
  |                 (void the authorization — release the hold on
  |                  cardholder's funds)
  +-- Notes:        External call. May timeout. reference_id enables
                    acquirer-side idempotency.

Step 3: Create ledger entries (double-entry)
  |-- Action:       INSERT INTO ledger_entries:
  |                   Debit:  customer_funds_receivable  $100.00
  |                   Credit: merchant_pending_balance    $97.10
  |                   Credit: psp_fee_revenue             $2.90
  |-- Compensation: INSERT reversing ledger entries:
  |                   Debit:  merchant_pending_balance    $97.10
  |                   Debit:  psp_fee_revenue             $2.90
  |                   Credit: customer_funds_receivable   $100.00
  +-- Notes:        Local DB transaction. Append-only ledger —
                    compensation adds new entries, never deletes.

Step 4: Update payment status to CAPTURED
  |-- Action:       UPDATE payments SET status = 'CAPTURED'
  |-- Compensation: (not needed — if we get here, previous steps succeeded)
  +-- Notes:        Final state update. At this point the saga is complete.

Step 5: Emit payment.captured event (async)
  |-- Action:       Publish to Kafka: { event: "payment.captured", ... }
  |-- Compensation: (not needed — downstream consumers are idempotent)
  +-- Notes:        Triggers webhook delivery, analytics, reconciliation.
                    Failure here does NOT roll back the capture.
```

### Saga execution: what happens on failure

**Scenario A: Acquirer returns explicit failure at Step 2**

```
Timeline:
  Step 1: OK  -- Payment status --> CAPTURE_INITIATED (committed)
  Step 2: FAIL -- Acquirer returns HTTP 402 "Capture Failed"

  Saga orchestrator detects failure at Step 2.
  Compensation begins (reverse order):

  Compensate Step 1: Payment status --> AUTHORIZED (restored)

  Result: Payment is back in AUTHORIZED state.
  Merchant is notified of capture failure.
  No money moved. No ledger entries created.
```

**Scenario B: Timeout at Step 2 (the hardest case)**

```
Timeline:
  Step 1: OK  -- Payment status --> CAPTURE_INITIATED (committed)
  Step 2: TIMEOUT -- Acquirer call times out after 30 seconds

  THIS IS THE HARDEST CASE.

  The saga orchestrator CANNOT compensate because the acquirer
  might have processed the capture. Voiding could void a
  successful capture.

  Resolution:
  1. Mark payment as CAPTURE_UNKNOWN
  2. Query acquirer for capture status:
     GET /acquirer/transactions/{reference_id}
  3a. If acquirer says "captured" --> proceed to Step 3 (ledger)
  3b. If acquirer says "not found" --> compensate Step 1 (safe to retry)
  3c. If acquirer query ALSO times out --> mark as PENDING_RESOLUTION
      and escalate to async reconciliation (see Section 8)
```

### Saga orchestrator pseudocode

```
class PaymentCaptureSaga:
    steps = [
        SagaStep(
            name="update_status_to_capture_initiated",
            action=updatePaymentStatus(paymentId, 'AUTHORIZED', 'CAPTURE_INITIATED'),
            compensation=updatePaymentStatus(paymentId, 'CAPTURE_INITIATED', 'AUTHORIZED')
        ),
        SagaStep(
            name="call_acquirer_capture",
            action=acquirer.capture(authCode, amount, referenceId),
            compensation=acquirer.void(authCode, referenceId)
        ),
        SagaStep(
            name="create_ledger_entries",
            action=ledger.createCaptureEntries(paymentId, amount, fees),
            compensation=ledger.createReversalEntries(paymentId, amount, fees)
        ),
        SagaStep(
            name="update_status_to_captured",
            action=updatePaymentStatus(paymentId, 'CAPTURE_INITIATED', 'CAPTURED'),
            compensation=None  // No compensation needed for final step
        ),
        SagaStep(
            name="emit_event",
            action=kafka.publish("payment.captured", paymentId),
            compensation=None  // Async, failure does not roll back saga
        )
    ]

    function execute():
        completedSteps = []

        for step in steps:
            try:
                step.action()
                completedSteps.append(step)
            catch TimeoutException:
                if step.name == "call_acquirer_capture":
                    handleAcquirerTimeout(completedSteps)
                    return
                else:
                    compensate(completedSteps)
                    throw SagaFailedException(step.name, "timeout")
            catch Exception as e:
                compensate(completedSteps)
                throw SagaFailedException(step.name, e.message)

        // All steps completed successfully
        return SUCCESS

    function compensate(completedSteps):
        // Compensate in REVERSE order
        for step in reverse(completedSteps):
            if step.compensation is not None:
                try:
                    step.compensation()
                catch Exception as e:
                    // Compensation failed — this is CRITICAL
                    // Log, alert, and escalate to manual resolution
                    alertOps("Saga compensation failed", step.name, e)
                    // Do NOT skip — continue compensating remaining steps

    function handleAcquirerTimeout(completedSteps):
        // Mark payment in unknown state
        db.updatePaymentStatus(paymentId, 'CAPTURE_INITIATED', 'CAPTURE_UNKNOWN')

        // Schedule async status check
        scheduler.schedule(
            task=AcquirerStatusCheck(paymentId, referenceId, completedSteps),
            delay=30_SECONDS,
            maxRetries=5,
            backoff=EXPONENTIAL
        )
```

### Choreography vs. orchestration

There are two styles of saga implementation:

| Style | How it works | Pros | Cons |
|---|---|---|---|
| **Orchestration** (shown above) | A central saga orchestrator coordinates all steps. | Easy to understand, centralized error handling, clear compensation flow. | Single point of failure (the orchestrator), tight coupling. |
| **Choreography** | Each service emits events; the next service listens and acts. | Loose coupling, no single coordinator. | Hard to debug, compensation flow is implicit and scattered across services, "what happened?" requires tracing events across multiple services. |

For payment systems, **orchestration** is generally preferred because the compensation flow must be explicit, auditable, and debuggable. When a $10,000 capture fails halfway, you need to know exactly which compensations ran and which did not. An orchestrator provides this visibility. [INFERRED — Stripe does not publicly state their internal saga implementation style, but their atomic phases pattern aligns more closely with orchestration.]

---

## 8. Timeout Handling

### The unknown state problem

A timeout on a downstream call (e.g., acquirer, card network) is the most dangerous scenario in payment processing. Unlike a success (proceed) or failure (compensate), a timeout means:

- **The request might have succeeded** (the acquirer captured the payment, but the response was lost).
- **The request might have failed** (the acquirer never received it, or it crashed mid-processing).
- **The request might still be in-progress** (the acquirer is slow but will eventually respond).

**You cannot assume either success or failure.** Assuming success and proceeding risks double-capture if you retry later. Assuming failure and compensating risks voiding a successful capture.

### The resolution protocol

```
TIMEOUT RESOLUTION PROTOCOL

Step 1: Immediately mark payment as UNKNOWN state
  |-- UPDATE payments SET status = 'CAPTURE_UNKNOWN'
  |-- This prevents any OTHER operation on this payment
  |   (no concurrent retry, no void, no refund)
  +-- Alert: log at WARN level, increment timeout metric

Step 2: Query downstream for authoritative status
  |-- GET /acquirer/transactions/{reference_id}
  |-- The acquirer knows whether it processed the capture
  |
  |-- Case A: Acquirer says "captured successfully"
  |   +-- Proceed with saga (create ledger entries, update to CAPTURED)
  |
  |-- Case B: Acquirer says "not found" or "declined"
  |   +-- Safe to compensate. Revert to AUTHORIZED.
  |
  +-- Case C: Acquirer query ALSO times out
      +-- Proceed to Step 3

Step 3: Mark as PENDING_RESOLUTION
  |-- UPDATE payments SET status = 'PENDING_RESOLUTION'
  |-- Insert into resolution_queue table with:
  |   - payment_id
  |   - last_known_state
  |   - timeout_timestamp
  |   - retry_count
  |   - next_retry_at (exponential backoff)
  +-- Alert: page on-call if resolution queue depth > threshold

Step 4: Async reconciliation
  |-- Background job polls resolution_queue every N minutes
  |-- For each pending payment:
  |   (a) Query acquirer again
  |   (b) If acquirer responds --> resolve (see Cases A/B above)
  |   (c) If still no response after MAX_RETRIES --> escalate
  |       to manual resolution by ops team
  +-- Manual resolution: ops contacts acquirer's support,
      verifies status, manually transitions payment state
```

### Critical rules for timeout handling

1. **NEVER auto-retry without checking status first.** If the original request succeeded, a retry with a new reference ID would create a second capture. Always query status before retrying.

2. **NEVER assume failure on timeout.** The downstream system might have processed the request. Voiding "just in case" could void a successful capture, leaving the merchant unpaid and the customer confused.

3. **NEVER leave a payment in UNKNOWN state indefinitely.** Every unknown payment must be resolved — automatically via status queries, or manually by operations. A dashboard should show all payments in UNKNOWN/PENDING_RESOLUTION states with time-in-state.

4. **Use the downstream's idempotency mechanism for safe retries.** If you must retry the acquirer call, use the SAME reference_id. The acquirer's own idempotency will return the original result if it already processed the request.

### Timeout budget allocation

A payment authorization has a total timeout budget (e.g., 30 seconds end-to-end). This budget must be divided across all downstream calls:

```
Total budget: 30 seconds

  |-- Fraud engine:       5 seconds  (circuit breaker at 3s)
  |-- Acquirer call:     20 seconds  (most variable, network hops)
  |-- Ledger write:       2 seconds  (local DB, should be <100ms)
  |-- State update:       2 seconds  (local DB)
  +-- Buffer:             1 second
```

If the fraud engine times out at 5 seconds, you still have 25 seconds for the acquirer. If the acquirer times out at 20 seconds, you enter the unknown state protocol. The key insight: **each downstream call has its own timeout, and exceeding any one triggers the appropriate fallback** (circuit breaker for non-critical services, unknown-state protocol for the acquirer).

---

## 9. Contrast with CRUD

### Why payment idempotency is fundamentally harder than CRUD

| Property | CRUD (e-commerce order) | Payment |
|---|---|---|
| **Duplicate creation** | Annoying but fixable. Customer gets two orders; cancel one. | Catastrophic. Customer charged twice. Cannot "un-charge" instantly — refund takes 5-10 business days for cards. |
| **Retry safety** | Generally safe. Worst case: extra row in database (clean up later). | Unsafe without idempotency. Retry = real money moves twice. |
| **Compensation** | Delete the duplicate record. Trivial. | Issue refund. Takes days. Incurs fees. Customer sees two charges on statement. Merchant may lose interchange fee on refund. |
| **State machine** | Simple: CREATED, CONFIRMED, SHIPPED, DELIVERED. Status can be updated freely. | Complex: CREATED, AUTHORIZED, CAPTURED, SETTLED. Transitions are irreversible. CAPTURED cannot go back to AUTHORIZED. |
| **External side effects** | Order creation is internal to your system. You control the database. | Payment authorization hits the acquirer, card network, and issuer. You do NOT control these external systems. A retry creates a real second authorization at the issuer. |
| **Timeout behavior** | If order creation times out, create a new one. No harm. | If authorization times out, you are in UNKNOWN state. Cannot create a new one (might double-charge). Cannot assume failure (might lose a valid auth). |
| **Financial regulation** | No regulatory requirements for order dedup. | PCI DSS, PSD2, RBI guidelines — all require controls against duplicate processing. Audit trails are mandatory. |
| **Customer impact** | Customer sees two orders, contacts support, one is cancelled. Mild inconvenience. | Customer sees two charges on credit card statement. May trigger fraud alert on their account. May overdraft their bank account. May file chargeback. Severe impact. |

### The core difference

In CRUD, the worst case of a duplicate is an extra database row that you can clean up at your leisure. In payments, the worst case of a duplicate is **real money leaving a real person's bank account**, with cascading consequences (overdraft fees, fraud alerts, chargebacks, regulatory scrutiny, and loss of customer trust).

This is why payment systems invest disproportionately in idempotency compared to typical web applications. The idempotency infrastructure described in this document — multi-layer dedup, database constraints, state machines, saga compensations, timeout protocols, reconciliation — would be massive overkill for a CRUD application. For payments, it is the **minimum viable correctness**.

### The interview takeaway

When discussing payment system design in an interview, idempotency should be the **first** design concern you raise, not an afterthought. An L6 candidate recognizes that idempotency shapes the entire architecture: it determines the API contract (client-provided keys), the database schema (unique constraints), the processing model (atomic phases, sagas), and the operational model (reconciliation, unknown state resolution). It is not a feature — it is the **foundation**.

---

## Summary

| Concept | Key Takeaway |
|---|---|
| **Why idempotency** | Network failures cause duplicates. Duplicates cause double-charges. Double-charges are financial/legal liability. |
| **Idempotency keys** | Client-generated UUID, server stores key-to-response mapping, returns cached response on retry. |
| **Storage** | Redis (hot cache, sub-ms) + PostgreSQL (durable, source of truth). 24-48h TTL. |
| **Race conditions** | DB unique constraint is the atomic test-and-set. Redis SETNX for fast path. |
| **Multi-layer dedup** | API layer, queue consumer layer, DB constraints, acquirer reference IDs. Defense in depth. |
| **Exactly-once** | Impossible (FLP). Achieve effectively-once via at-least-once + idempotent handlers. |
| **State machine** | CREATED, AUTHORIZED, CAPTURED, SETTLED. Atomic, monotonic, auditable transitions. |
| **Saga pattern** | Each step has a compensating action. No 2PC. Orchestration preferred for payments. |
| **Timeouts** | UNKNOWN state. Query downstream. Never auto-retry. Never assume failure. Reconcile async. |
| **vs. CRUD** | Payment duplicates move real money. CRUD duplicates create extra rows. Fundamentally different severity. |

---

## References

- Brandur Leach, "Designing Robust and Predictable APIs with Idempotency," Stripe Engineering Blog. Describes atomic phases, recovery points, and PostgreSQL-based idempotency key implementation.
- Stripe API Documentation — Idempotent Requests: https://stripe.com/docs/api/idempotent_requests
- Fischer, Lynch, Paterson, "Impossibility of Distributed Consensus with One Faulty Process" (FLP), 1985.
- Chris Richardson, "Saga Pattern," microservices.io — Compensating transactions for distributed systems.
- Kafka Improvement Proposal KIP-98 — Exactly-Once Delivery and Transactional Messaging.
- PCI DSS v4.0 — Requirements for payment data handling and audit trails.
