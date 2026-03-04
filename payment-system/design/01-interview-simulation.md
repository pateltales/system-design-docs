# System Design Interview Simulation: Design a Payment System (like Stripe / Razorpay)

> **Interviewer:** Principal Engineer (L8), Payment Infrastructure Team
> **Candidate Level:** SDE-3 (L6 — Senior Software Development Engineer)
> **Duration:** ~60 minutes
> **Date:** February 25, 2026

---

## PHASE 1: Opening & Problem Statement (~2 min)

**Interviewer:**
Hey, welcome. I'm a Principal Engineer on the payment infrastructure team. For today's system design round, I'd like you to design a **payment system** — think Stripe or Razorpay. Not just a checkout form — I'm talking about the full end-to-end platform: accepting payments from customers, routing to acquirers, fraud detection, ledger management, settlement, and the reliability engineering that ensures money is never lost or double-charged.

Payments are a domain where mistakes are measured in dollars, not just error rates. I care about how you think about correctness, idempotency, and the tradeoffs that make financial systems different from typical web applications. I'll push on your decisions — that's calibration, not criticism.

Take it away.

---

## PHASE 2: Requirements Gathering & Scoping (~5 min)

**Candidate:**
Thanks! A payment system is complex because it sits at the intersection of software engineering and financial infrastructure. Let me scope this before drawing anything.

**Functional Requirements — what operations do we need?**

> "Core merchant-facing operations:
>
> - **Create Payment** — Merchant submits a payment request (amount, currency, payment method, idempotency key). The system authorizes with the card network/bank and returns success/failure.
> - **Capture Payment** — For two-step flows (authorize now, capture later). Hotels, e-commerce with delayed shipment.
> - **Refund** — Full or partial refund of a captured payment. Creates a new transaction in the opposite direction.
> - **Void/Cancel** — Cancel an authorized-but-uncaptured payment. Releases the hold on the customer's card.
>
> Supporting operations:
> - **Tokenize Payment Method** — Store card details securely (PCI compliant vault), return a token. Merchant never sees raw PAN.
> - **Webhook Notifications** — Async notifications to merchants on payment state changes (captured, failed, refunded, disputed).
> - **View Payment Status** — Merchant queries current state of a payment.
> - **Payout/Settlement** — Transfer captured funds to merchant's bank account on a schedule (T+1, T+2, etc.).
>
> Clarifying questions:
> - **Which payment methods?** Cards (Visa, Mastercard, Amex), bank transfers (ACH, SEPA), UPI (India), wallets (Apple Pay, Google Pay)?"

**Interviewer:** "Focus on card payments for the architecture — that's the most complex flow. Mention alternatives for comparison."

> "- **Do we need subscription/recurring billing?** That adds a billing engine on top."

**Interviewer:** "Mention it, but focus on one-time payments. Subscriptions are an extension."

> "- **Are we building the PSP or integrating with one?** Building Stripe, or building a merchant that uses Stripe?"

**Interviewer:** "You're building the PSP — the platform that merchants integrate with. You sit between the merchant and the acquirer."

**Non-Functional Requirements:**

> "Now the critical constraints. Payment systems are defined by correctness more than throughput:
>
> | Dimension | Requirement | Rationale |
> |---|---|---|
> | **Idempotency** | A payment must NEVER be charged twice | The #1 requirement. Network retries, client retries, queue redelivery — all must be safe. Double-charging is financial and legal liability. |
> | **Consistency** | Strong consistency for payment state and ledger | Cannot show merchant $1000 available when they have $500. Eventual consistency is unacceptable for money. |
> | **Availability** | 99.99% uptime (52 min/year) | Payment downtime = lost revenue for every merchant. Black Friday traffic must be handled. |
> | **Latency** | p99 < 500ms for authorization | Merchant's checkout page is waiting synchronously. > 3 seconds = abandoned cart. |
> | **Durability** | Zero data loss (RPO = 0) | Losing a payment record is losing money. Literally. |
> | **Compliance** | PCI DSS Level 1 | Raw card data (PAN, CVV) must be isolated in a hardened vault. Shapes the entire architecture. |
> | **Auditability** | Complete audit trail for every cent | Every state change, every ledger entry logged with actor + timestamp. Required by PCI, SOX, RBI. |

**Interviewer:**
You put idempotency as your #1 requirement, above even availability. Why?

**Candidate:**

> "Because a payment system that's down for 5 minutes loses revenue. A payment system that double-charges loses trust, triggers chargebacks, and faces regulatory action. You can recover from downtime — you can't easily recover from thousands of customers seeing duplicate charges on their credit card statements. The asymmetry of consequences makes idempotency more critical than availability.
>
> Also, idempotency shapes the entire architecture — it determines the API contract (client-provided keys), the database schema (unique constraints), the processing model (state machines, sagas), and the operational model (reconciliation). It's not a feature bolted on at the end — it's the foundation."

**Interviewer:**
Strong reasoning. That's exactly the kind of 'why' I want to hear. Let's estimate scale.

---

### L5 vs L6 vs L7 — Phase 2 Rubric

| Aspect | L5 (SDE-2) | L6 (SDE-3) | L7 (Principal) |
|---|---|---|---|
| **Functional Reqs** | Lists CRUD: create payment, refund, view status | Proactively raises idempotency, two-step auth+capture, webhooks, tokenization | Additionally discusses settlement, reconciliation, multi-currency, dispute handling |
| **Non-Functional** | Mentions high availability and low latency | Quantifies 99.99%, p99 <500ms. Explains why idempotency > availability | Frames NFRs in business terms: "double-charge costs $X in chargebacks + regulatory risk" |
| **Scoping** | Accepts problem as given | Asks clarifying questions (PSP vs merchant? Which payment methods?) | Negotiates scope: "Let me focus on card payments for depth, then extend to UPI/ACH" |
| **PCI DSS** | Doesn't mention compliance | Mentions tokenization and PCI scope | Explains how PCI shapes architecture: CDE boundary, vault isolation, scope minimization |

---

## PHASE 3: Scale Estimation & API Design (~5 min)

**Candidate:**

> "Let me estimate Stripe-scale numbers to ground our design."

### Scale Estimates

> | Metric | Estimate | Notes |
> |---|---|---|
> | API requests/day | ~500 million | Status checks, webhook acks, payment ops |
> | Payment transactions/day | ~10 million | Authorization + capture events |
> | TPS (payments, average) | ~115 TPS | 10M / 86400 |
> | TPS (payments, peak) | ~1,000-2,000 TPS | Black Friday, flash sales: 10-20x average |
> | Merchants | ~100,000+ | Each with different routing rules, risk config |
> | Currencies | 135+ | Multi-currency support |
> | Payment methods | 50+ | Cards, bank transfers, wallets, local methods |
> | Storage per transaction | ~5 KB | Payment record + ledger entries + events |
> | Storage per day | ~50 GB | 10M × 5KB |
> | Annual storage | ~18 TB | Plus audit logs, events, analytics |

### Core API Design

> "Let me define the critical APIs. I'll focus on the payment lifecycle."

```
POST /v1/payments
  Headers: Idempotency-Key: <uuid>
  Body: { amount: 5000, currency: "usd", payment_method: "pm_card_visa",
          capture: true, metadata: { order_id: "ord_123" } }
  Response: { id: "pay_abc", status: "captured", amount: 5000, ... }

POST /v1/payments/{id}/capture
  Headers: Idempotency-Key: <uuid>
  Body: { amount: 5000 }  // supports partial capture
  Response: { id: "pay_abc", status: "captured", ... }

POST /v1/payments/{id}/cancel
  Response: { id: "pay_abc", status: "voided", ... }

POST /v1/payments/{id}/refund
  Headers: Idempotency-Key: <uuid>
  Body: { amount: 2000, reason: "customer_request" }
  Response: { id: "ref_xyz", status: "succeeded", amount: 2000, ... }

GET /v1/payments/{id}
  Response: { id: "pay_abc", status: "captured", amount: 5000, ... }

POST /v1/payment_methods
  Body: { type: "card", card: { token: "tok_from_js_sdk" } }
  Response: { id: "pm_card_visa", type: "card", card: { last4: "4242", brand: "visa" } }
```

> "Key design decisions in the API:
>
> 1. **Idempotency-Key header on all mutating endpoints** — client-generated UUID. Server deduplicates on this key. Without it, retries create duplicate payments.
>
> 2. **`capture: true/false`** on create — one-step (auth + capture) vs two-step (auth only, capture later). Default true for simplicity. Hotels, e-commerce use false.
>
> 3. **Amounts in smallest currency unit** (cents, paise) as integers — never floats. `5000` = $50.00. Prevents floating-point precision errors.
>
> 4. **Card data never in API** — merchant uses our JS SDK (like Stripe.js) to tokenize card → gets a token → sends token to their server → their server sends token to us. Raw PAN never touches merchant's server. This minimizes PCI scope."

**Interviewer:**
Why did you make the idempotency key client-generated rather than server-generated?

**Candidate:**

> "Because server-generated dedup is fragile for payments. If the server tries to detect duplicates heuristically — 'same amount + same card + same merchant within 5 minutes' — it breaks on legitimate identical transactions. A customer might buy two $25 coffees at the same cafe within 5 minutes. That's not a duplicate.
>
> Client-provided keys make the contract explicit: same key = same intended operation. The client controls what counts as a retry vs a new payment. Stripe documented this design choice extensively — it's the right approach for financial APIs."

**Interviewer:**
Good. Let's build the architecture.

---

### L5 vs L6 vs L7 — Phase 3 Rubric

| Aspect | L5 | L6 | L7 |
|---|---|---|---|
| **API Design** | Basic CRUD endpoints | Idempotency keys, two-step capture, amounts-as-integers | Additionally discusses API versioning, backward compatibility, rate limiting per merchant |
| **Scale** | "Millions of payments" | Concrete TPS, storage estimates, peak vs average | Connects scale to sharding strategy, DB sizing, Kafka partition count |
| **Tokenization** | "Encrypt card data" | Explains JS SDK tokenization, PCI scope minimization | Explains CDE boundary, HSM key management, why this is architecture not just security |

---

## PHASE 4: High-Level Architecture — Iterative Build-Up (~15 min)

**Candidate:**

> "Let me build this up iteratively, starting simple and finding problems."

---

### Attempt 0: Single Server + Direct Bank API

```
┌──────────┐     ┌──────────────────────────┐     ┌──────────────┐
│ Merchant │────▶│ Payment Server            │────▶│ Acquirer     │
│          │     │ (monolith + PostgreSQL)   │     │ (Bank API)   │
│          │     └──────────────────────────┘     └──────────────┘
```

> "Simplest possible design. Merchant calls our API. Our monolith validates the request, calls the acquirer's API (bank that processes card transactions), stores the result in PostgreSQL, returns response."

**Interviewer:** What breaks?

**Candidate:**

> "Everything:
>
> 1. **No idempotency** — if the network drops after we charge the card but before the merchant gets our response, they retry, and we charge twice.
> 2. **Card data stored in plain text** — PCI DSS violation. One breach = catastrophic.
> 3. **Single acquirer** — if that acquirer goes down, ALL payments fail. No failover.
> 4. **No async notifications** — merchant must poll for status. No webhooks.
> 5. **No ledger** — we're tracking payments but not money flow. Can't reconcile, can't settle.
> 6. **No fraud detection** — we accept every transaction. Fraudsters love us."

---

### Attempt 1: Idempotency + Tokenization + State Machine

```
┌──────────┐     ┌───────────────────────────────────────────────┐
│ Merchant │────▶│ API Gateway                                    │
│          │     │   ├── Idempotency Check (Redis + PostgreSQL)  │
│          │     │   ├── Payment Service                          │
│          │     │   │     └── State Machine (CREATED → AUTH →   │
│          │     │   │         CAPTURED → SETTLED)                │
│          │     │   ├── Token Vault (PCI CDE, HSM-backed)       │
│          │     │   └── Webhook Service (async notifications)   │
│          │     └───────────────────────────────────────────────┘
│          │                        │
│ Stripe.js│───── tokenize ──▶ Token Vault
│ (client) │                        │
│          │     ┌──────────────┐   │
│          │     │  Acquirer    │◀──┘
│          │     └──────────────┘
```

> "Three critical additions:
>
> **1. Idempotency keys:**
> Client sends `Idempotency-Key` header (UUID). Before processing, we check:
> - Insert key into `idempotency_keys` table with UNIQUE constraint
> - If insert succeeds → new request, proceed
> - If UniqueConstraintViolation → duplicate, return stored response
> - This is an atomic test-and-set using the DB's own constraint. No race conditions.
> - Redis cache in front for sub-ms lookups (99% of checks). DB is the source of truth.
>
> **2. Tokenization + PCI scope minimization:**
> Card data captured via client-side JS SDK (like Stripe.js). Raw PAN goes directly to our Token Vault — an isolated, hardened service in a separate network segment (Cardholder Data Environment). Returns a token like `pm_card_visa_4242`. Merchant's server only sees tokens. Our main backend only sees tokens. Only the Token Vault touches raw card data. This reduces PCI audit scope from the entire system to just the vault.
>
> **3. Payment state machine:**
> Every payment follows: `CREATED → AUTHORIZED → CAPTURED → SETTLED`.
> State transitions are atomic (single DB transaction), monotonic (no going backward), and audited (every transition logged).
> Invalid transitions rejected: you can't capture a payment that's already refunded."

**Interviewer:** What about webhooks?

**Candidate:**

> "After each state transition, we emit an event to a webhook service. It calls the merchant's registered URL with the event payload. Delivery is at-least-once with exponential backoff retries. The merchant must process webhooks idempotently — we may deliver the same event multiple times. We also provide a `GET /v1/events` API as a fallback for merchants whose webhook endpoints were down."

**Interviewer:** What's still broken?

**Candidate:**

> "1. **Single acquirer** — if they go down, all payments fail. No routing optimization.
> 2. **No fraud detection** — accepting every transaction. Chargebacks will eat us alive.
> 3. **No ledger** — we track payment status but not money flow. Can't reconcile with bank statements.
> 4. **Single region** — no disaster recovery."

---

### Attempt 2: Multi-Acquirer Routing + Fraud Detection

```
┌──────────┐     ┌──────────────────────────────────────────────────────┐
│ Merchant │────▶│ API Gateway                                          │
│          │     │   ├── Idempotency Check                              │
│          │     │   ├── Payment Service                                │
│          │     │   │     ├── Fraud Engine ─┬─ Rule Engine (Layer 1)   │
│          │     │   │     │                 └─ ML Scoring (Layer 2)    │
│          │     │   │     │                                            │
│          │     │   │     └── Routing Engine ─┬─ Acquirer A            │
│          │     │   │                         ├─ Acquirer B            │
│          │     │   │                         └─ Acquirer C            │
│          │     │   ├── Token Vault                                    │
│          │     │   └── Webhook Service                                │
│          │     └──────────────────────────────────────────────────────┘
```

> "Two major additions:
>
> **1. Smart Payment Routing:**
> We integrate with multiple acquirers (acquiring banks). The routing engine selects the best acquirer for each transaction based on:
> - Card network (Visa → Acquirer A, Mastercard → Acquirer B)
> - Issuing country (local acquiring has higher success rates than cross-border)
> - Acquirer success rate (track per-acquirer auth success rate over sliding windows)
> - Cost (interchange + acquirer markup differs)
> - Acquirer health (circuit breaker — if acquirer is down, route to backup)
>
> **Cascade retries:** On a soft decline (processing error, issuer unavailable), auto-retry with a secondary acquirer. On hard decline (insufficient funds, stolen card), don't retry — the answer won't change.
>
> This alone can improve overall authorization success rates by 5-15%.
>
> **2. Fraud Detection Engine:**
> Two-layer approach, both must execute within ~100ms:
>
> - **Layer 1 — Rule engine:** Deterministic rules. Velocity checks (>5 transactions/minute from same card → block). Amount thresholds (>$10K → review). Geographic mismatch (card issued in US, IP from Nigeria → flag). BIN-country mismatch. Blocked card/IP lists.
>
> - **Layer 2 — ML scoring:** Real-time model (XGBoost/LightGBM) scores each transaction 0-100. Features: amount, MCC, time of day, device fingerprint, IP geolocation, historical transaction patterns. High score → decline or trigger 3DS challenge. Low score → approve. Gray zone → 3DS challenge (adds authentication, shifts fraud liability to issuer).
>
> The fraud engine runs synchronously in the authorization path. It must be fast — we budget ~50-100ms for it."

**Interviewer:** What happens when the fraud engine is down?

**Candidate:**

> "Graceful degradation. If the ML model is unavailable, fall back to rule engine only. Rules are simpler but still catch the most obvious fraud patterns. We accept slightly higher risk temporarily rather than blocking all payments. A circuit breaker on the ML service handles this — if latency exceeds 100ms, we short-circuit to rules-only.
>
> This is a conscious trade-off: slightly higher fraud risk for a few minutes vs blocking every payment. For a payment system, blocking all payments is always worse than accepting slightly elevated fraud risk."

**Interviewer:** What's still broken?

**Candidate:**

> "1. **No financial accounting** — we track payment status but not money flow. We can't tell a merchant 'you have $5,000 available for payout.' We can't reconcile against bank statements.
> 2. **No settlement engine** — captured payments sit in limbo. No mechanism to calculate what we owe each merchant and initiate bank transfers.
> 3. **Single region** — if our DB goes down, ALL payments stop."

---

### L5 vs L6 vs L7 — Attempt 0-2 Rubric

| Aspect | L5 | L6 | L7 |
|---|---|---|---|
| **Idempotency** | "Add a unique ID to prevent duplicates" | Explains DB unique constraint as atomic test-and-set, Redis cache for fast path, idempotency key scoped per merchant | Discusses Stripe's atomic phases pattern, recovery points for crash resilience, multi-layer dedup at API/queue/DB/acquirer layers |
| **Tokenization** | "Encrypt card data" | Explains client-side tokenization, CDE boundary, PCI scope minimization | Discusses HSM key management, key rotation, why tokenization is architecture not just security |
| **Routing** | "Have a backup payment processor" | Multi-acquirer routing with success rate tracking, cascade retries, soft vs hard declines | Discusses cost optimization vs success rate trade-off, local acquiring, dynamic routing ML |
| **Fraud** | "Check for suspicious transactions" | Two-layer (rules + ML), latency budget, 3DS challenge for gray zone | Discusses precision/recall trade-off by merchant vertical, network-level signals (Stripe Radar), model retraining cadence |

---

### Attempt 3: Ledger + Reconciliation + Settlement

```
┌──────────────────────────────────────────────────────────────────────────┐
│                        PAYMENT PLATFORM                                  │
│                                                                          │
│  ┌──────────┐  ┌───────────┐  ┌────────────┐  ┌──────────────────────┐  │
│  │   API     │  │ Payment   │  │  Fraud     │  │  Routing Engine      │  │
│  │ Gateway   │─▶│ Service   │─▶│  Engine    │─▶│  ├─ Acquirer A       │  │
│  │           │  │           │  │            │  │  ├─ Acquirer B       │  │
│  │           │  │           │  │            │  │  └─ Acquirer C       │  │
│  └──────────┘  └─────┬─────┘  └────────────┘  └──────────────────────┘  │
│                       │                                                   │
│                       ▼                                                   │
│  ┌──────────────────────────────────────────────────────────────────────┐ │
│  │              FINANCIAL ENGINE (NEW)                                   │ │
│  │                                                                      │ │
│  │  ┌────────────────┐  ┌───────────────────┐  ┌────────────────────┐  │ │
│  │  │ Double-Entry    │  │  Reconciliation   │  │  Settlement        │  │ │
│  │  │ Ledger          │  │  Engine           │  │  Engine            │  │ │
│  │  │                 │  │                   │  │                    │  │ │
│  │  │ Every payment   │  │ Internal ledger   │  │ Batch job:         │  │ │
│  │  │ = debit+credit  │  │ vs acquirer file  │  │ Net amounts owed   │  │ │
│  │  │ entries.        │  │ vs card network   │  │ per merchant.      │  │ │
│  │  │ Append-only.    │  │ file.             │  │ Initiate payouts.  │  │ │
│  │  │ Immutable.      │  │ Flag mismatches.  │  │ T+1, T+2 cycles.  │  │ │
│  │  └────────────────┘  └───────────────────┘  └────────────────────┘  │ │
│  └──────────────────────────────────────────────────────────────────────┘ │
│                                                                          │
│  ┌───────────────┐  ┌────────────┐  ┌──────────┐                        │
│  │ Token Vault   │  │ Webhook    │  │ Kafka    │                        │
│  │ (PCI CDE)     │  │ Service    │  │ (Events) │                        │
│  └───────────────┘  └────────────┘  └──────────┘                        │
└──────────────────────────────────────────────────────────────────────────┘
```

> "The financial engine is what separates a toy payment system from a real one.
>
> **1. Double-Entry Ledger:**
> Every financial event creates exactly two entries — a debit and a credit of equal amount. The invariant `sum(debits) = sum(credits)` is maintained at all times. If it breaks, something is wrong.
>
> Example — card payment capture of $100 (PSP fee 2.9% + $0.30 = $3.20):
>
> ```
> Entry 1: DR funds_receivable     $96.80
>          CR merchant:abc:pending  $96.80
>
> Entry 2: DR funds_receivable     $3.20
>          CR psp_processing_fee   $3.20
>
> Total debits: $100.00, Total credits: $100.00 ✓
> ```
>
> Entries are **append-only** — never update, never delete. Corrections are made by adding compensating entries. This gives us a complete audit trail.
>
> **Critical:** Ledger entries and payment status updates happen in the **same database transaction**. When we capture a payment:
>
> ```sql
> BEGIN;
>   UPDATE payments SET status = 'CAPTURED' WHERE id = 'pay_abc';
>   INSERT INTO ledger_entries (...) VALUES (...), (...);
> COMMIT;
> ```
>
> Same DB, same transaction. ACID guarantees that payment state and ledger are always consistent. This is why we use PostgreSQL (not Cassandra) for payment data — we need ACID.
>
> **2. Reconciliation Engine:**
> Three-way daily reconciliation:
> - Source 1: Our internal ledger
> - Source 2: Acquirer settlement statements
> - Source 3: Card network clearing files
>
> Match by transaction reference ID. Flag discrepancies: timing differences (captured today, settled tomorrow), amount mismatches (currency conversion), missing transactions. Auto-match rate target: >99.5%.
>
> **3. Settlement Engine:**
> Batch job that runs on schedule (daily for most merchants). For each merchant:
> - Sum captured payments minus refunds minus chargebacks minus PSP fees
> - Net amount = what we owe the merchant
> - Initiate bank transfer (ACH/SEPA/NEFT) to merchant's bank account
> - Create ledger entries for the payout
> - Settlement cycle: T+1 (next business day) for low-risk merchants, T+7 for high-risk."

**Interviewer:** Why is the ledger in the same database as payments? Couldn't you use a separate ledger service?

**Candidate:**

> "Because they must be atomically consistent. If the payment status changes to CAPTURED but the ledger entry fails (separate service, network issue), we have money that moved but isn't accounted for. That's the kind of discrepancy that causes reconciliation nightmares.
>
> By keeping them in the same PostgreSQL database, we get a single ACID transaction. The tradeoff is that this couples the payment service to the ledger — but for financial correctness, that coupling is worth it.
>
> If we ever need to split them (at extreme scale), we'd use the Saga pattern with compensation — but that's a last resort, not a first choice."

**Interviewer:** What's still broken?

**Candidate:**

> "1. **Single region, no DR** — if the database goes down, ALL payments stop. No disaster recovery.
> 2. **No observability** — hard to debug why a specific payment failed across 5 services.
> 3. **No circuit breakers** — one slow acquirer can cascade latency to the entire system."

---

### Attempt 4: Reliability Hardening + Multi-Region + Observability

```
┌───────────────────────────────────────────────────────────────────────────────┐
│                         REGION 1 (PRIMARY)                                    │
│                                                                               │
│  ┌─────────┐  ┌───────────┐  ┌──────────┐  ┌────────────┐  ┌─────────────┐  │
│  │ API GW  │  │ Payment   │  │ Fraud    │  │  Routing   │  │  Financial  │  │
│  │ +Auth   │─▶│ Service   │─▶│ Engine   │─▶│  Engine    │  │  Engine     │  │
│  │ +Rate   │  │           │  │          │  │            │  │  (Ledger/   │  │
│  │  Limit  │  │           │  │          │  │            │  │  Recon/     │  │
│  └─────────┘  └─────┬─────┘  └──────────┘  └────────────┘  │  Settle)   │  │
│                      │                                       └─────────────┘  │
│                      ▼                                                        │
│  ┌──────────────────────────────────────────────────────────────────────────┐ │
│  │  PostgreSQL Primary                                                      │ │
│  │  ├── payments, ledger_entries, idempotency_keys                         │ │
│  │  ├── Synchronous replication ──▶ Standby (same region)                  │ │
│  │  └── Async replication ──▶ Region 2 (read replica)                      │ │
│  └──────────────────────────────────────────────────────────────────────────┘ │
│                                                                               │
│  ┌──────────┐  ┌──────────┐  ┌──────────┐  ┌──────────┐                     │
│  │ Redis    │  │ Kafka    │  │ Webhook  │  │ Token    │                     │
│  │ Cluster  │  │ Cluster  │  │ Service  │  │ Vault    │                     │
│  └──────────┘  └──────────┘  └──────────┘  └──────────┘                     │
│                                                                               │
│  ┌──────────────────────────────────────────────────────────────────────────┐ │
│  │  OBSERVABILITY                                                           │ │
│  │  ├── Distributed Tracing (OpenTelemetry / Jaeger)                       │ │
│  │  ├── Metrics (Prometheus / Grafana)                                     │ │
│  │  │     ├── Per-acquirer success rate                                     │ │
│  │  │     ├── p50/p95/p99 latency per endpoint                             │ │
│  │  │     └── Reconciliation discrepancy amount                            │ │
│  │  └── Alerting                                                            │ │
│  │        ├── p99 > 500ms → page on-call                                   │ │
│  │        ├── Success rate drop > 5% → page on-call                        │ │
│  │        ├── Payment stuck "pending" > 10min → alert                      │ │
│  │        └── Recon discrepancy > $1000 → finance team                     │ │
│  └──────────────────────────────────────────────────────────────────────────┘ │
└───────────────────────────────────────────────────────────────────────────────┘

┌───────────────────────────────────────────────────────────────────────────────┐
│                         REGION 2 (WARM STANDBY)                               │
│  ┌──────────────────────────────────────────────────────────────────────────┐ │
│  │  PostgreSQL Read Replica (async from Region 1)                           │ │
│  │  Used for: reporting, analytics, read-only queries                      │ │
│  │  Promoted to primary on Region 1 failure (manual/automated failover)    │ │
│  └──────────────────────────────────────────────────────────────────────────┘ │
└───────────────────────────────────────────────────────────────────────────────┘
```

> "Four additions to make this production-grade:
>
> **1. Database replication + failover:**
> PostgreSQL with synchronous replication to a standby in the same region (RPO = 0). Async replication to a read replica in Region 2 (for reporting and DR). Automated failover to standby with <5 minute RTO. Post-failover reconciliation to ensure no transactions were lost.
>
> **2. Multi-region — active-passive (NOT active-active):**
> Payment systems need strong consistency for financial data. Active-active across regions means dealing with write conflicts on payment state and ledger — extremely complex and risky. Active-passive is safer: Region 1 handles all writes, Region 2 is a warm standby promoted on failure.
>
> This is a deliberate contrast with Netflix, which uses active-active. Netflix can tolerate brief staleness in viewing history. We cannot tolerate staleness in payment balances — showing a merchant $1000 when they have $500 could cause an over-payout.
>
> **3. Distributed tracing:**
> Every payment request gets a trace ID (OpenTelemetry) that flows through: API gateway → payment service → fraud engine → routing engine → acquirer adapter → ledger service. When a merchant asks 'why did payment X fail?', we can trace the entire journey in seconds.
>
> **4. Circuit breakers + graceful degradation:**
> - Circuit breakers on every acquirer connection. If Acquirer A's error rate exceeds 20%, the circuit opens — requests are immediately routed to Acquirer B instead of waiting for timeouts.
> - Circuit breaker on fraud ML service. If ML is down, fall back to rule engine.
> - Circuit breaker on webhook delivery. If a merchant's endpoint is down, exponential backoff + DLQ.
> - Dead letter queues (DLQ) for all async operations. No financial event is silently dropped."

**Interviewer:** You mentioned active-passive instead of active-active. Won't that limit your availability?

**Candidate:**

> "Yes, it limits theoretical availability. But for payment systems, correctness is more important than availability. Active-active requires solving write conflicts for financial data — what if Region 1 captures a payment while Region 2 processes a refund for the same payment? With eventual consistency, you could temporarily show inconsistent balances.
>
> For Netflix, a user seeing a slightly stale viewing history for 5 seconds is fine. For us, a merchant seeing a wrong balance and initiating a $50,000 payout based on it is a financial disaster.
>
> The right answer is active-passive with fast failover (<5 min RTO). The 99.99% SLA gives us 52 minutes/year of downtime — plenty of headroom for occasional failovers. If we outgrow this, we'd shard by merchant and use active-active per-shard (each shard writes to a single region), which avoids cross-region write conflicts."

---

### L5 vs L6 vs L7 — Attempt 3-4 Rubric

| Aspect | L5 | L6 | L7 |
|---|---|---|---|
| **Ledger** | "Store transactions in a database" | Double-entry bookkeeping, append-only, same-DB ACID transaction with payment state | Discusses ledger as event source, materialized balance views, three-way reconciliation |
| **Consistency** | "Use a database" | Explains why PostgreSQL (CP) not Cassandra (AP) for payments. Strong consistency for financial data | Discusses consistency boundaries — strong for payments/ledger, eventual for analytics/reporting |
| **Multi-region** | "Have a backup region" | Active-passive with justification (financial consistency > availability). Contrast with Netflix active-active | Discusses per-shard active-active, CRDT-based approaches, regulatory data residency (GDPR) |
| **Observability** | "Add logging" | Distributed tracing, per-acquirer metrics, alerting hierarchy with escalation | Discusses SLI/SLO framework, error budgets, runbooks for common payment incidents |

---

### Attempt 5: Scale + Performance Optimization

```
┌──────────────────────────────────────────────────────────────────────────────┐
│                        PRODUCTION ARCHITECTURE                                │
│                                                                              │
│  ┌────────────────────────────────────────────────────────────────────────┐  │
│  │ API Layer (stateless, auto-scaled)                                     │  │
│  │  ├── Rate limiter (per-merchant, per-IP, adaptive)                    │  │
│  │  ├── Idempotency check (Redis → PostgreSQL fallback)                  │  │
│  │  └── Authentication + merchant config cache (Redis)                   │  │
│  └───────────────────────────────┬────────────────────────────────────────┘  │
│                                  │                                           │
│  ┌───────────────────────────────▼────────────────────────────────────────┐  │
│  │ Payment Service (stateless)                                            │  │
│  │  ├── Fraud Engine (rules + ML, circuit-breakered)                     │  │
│  │  ├── Routing Engine (smart routing + cascade retry)                   │  │
│  │  ├── Acquirer Adapters (per-acquirer protocol translation)            │  │
│  │  └── Saga Orchestrator (capture saga with compensations)              │  │
│  └───────────────────────────────┬────────────────────────────────────────┘  │
│                                  │                                           │
│  ┌───────────────────────────────▼────────────────────────────────────────┐  │
│  │ Data Layer                                                             │  │
│  │                                                                        │  │
│  │  PostgreSQL (sharded by merchant_id)                                   │  │
│  │  ┌──────────────┐ ┌──────────────┐ ┌──────────────┐ ┌──────────────┐  │  │
│  │  │  Shard 0     │ │  Shard 1     │ │  Shard 2     │ │  Hot Shard   │  │  │
│  │  │  merch 0-999 │ │  merch 1K-2K │ │  merch 2K-3K │ │  (Amazon,    │  │  │
│  │  │              │ │              │ │              │ │   Uber)      │  │  │
│  │  └──────────────┘ └──────────────┘ └──────────────┘ └──────────────┘  │  │
│  │                                                                        │  │
│  │  Kafka (partitioned by payment_id)                                     │  │
│  │  ┌──────────────────────────────────────────┐                          │  │
│  │  │ Topics: payment.events, webhook.delivery, │                          │  │
│  │  │         ledger.entries, settlement.batch   │                          │  │
│  │  └──────────────────────────────────────────┘                          │  │
│  │                                                                        │  │
│  │  Redis Cluster (idempotency + merchant config + caching)               │  │
│  │  Token Vault (PCI CDE, HSM-backed, isolated network)                  │  │
│  └────────────────────────────────────────────────────────────────────────┘  │
│                                                                              │
│  ┌────────────────────────────────────────────────────────────────────────┐  │
│  │ Async Processing                                                       │  │
│  │  ├── Webhook Delivery Workers (from Kafka, at-least-once + DLQ)       │  │
│  │  ├── Settlement Batch (daily, nets amounts, initiates payouts)        │  │
│  │  ├── Reconciliation Workers (daily, three-way matching)               │  │
│  │  ├── Timeout Resolution Workers (unknown-state payment resolution)    │  │
│  │  └── Analytics Pipeline (CDC → Kafka → data warehouse)               │  │
│  └────────────────────────────────────────────────────────────────────────┘  │
│                                                                              │
│  ┌────────────────────────────────────────────────────────────────────────┐  │
│  │ Observability                                                          │  │
│  │  ├── Distributed Tracing (OpenTelemetry)                              │  │
│  │  ├── Metrics + Dashboards (Prometheus/Grafana)                        │  │
│  │  ├── Alerting (PagerDuty — tiered escalation)                         │  │
│  │  └── Chaos Engineering (regular acquirer failover + DB failover drills)│  │
│  └────────────────────────────────────────────────────────────────────────┘  │
└──────────────────────────────────────────────────────────────────────────────┘
```

> "Final production hardening:
>
> **1. Database sharding by merchant_id:**
> All of a merchant's data (payments, ledger entries, idempotency keys) lives on the same shard. This preserves transactional integrity — the ACID transaction that updates payment status + creates ledger entries stays within a single shard.
>
> Hot merchant problem: A few large merchants (Amazon, Uber) generate disproportionate traffic. Solutions:
> - Dedicated shards for top merchants
> - Sub-sharding within a hot merchant by payment_id hash (if we can relax the 'all merchant data on one shard' constraint for very large merchants)
>
> **2. Event-driven architecture with Kafka:**
> Kafka as the event backbone. Payment state changes emit events to Kafka topics. Downstream consumers:
> - Webhook workers → deliver to merchant endpoints
> - Ledger workers → (if we split ledger to async, which is a trade-off)
> - Analytics → CDC to data warehouse
> - Reconciliation → consume and match
>
> Kafka is partitioned by payment_id → ensures ordering within a single payment's lifecycle.
>
> **3. Peak traffic handling:**
> Black Friday, Diwali — 10-50x normal traffic. Auto-scaling alone is too slow (DB connection limits, acquirer rate limits are hard to scale instantly).
>
> Strategy:
> - Pre-provision infrastructure 2 weeks before known peaks
> - Load shedding: under extreme load, reject low-priority requests (analytics, reporting) to protect the payment critical path
> - Adaptive rate limiting: tighten per-merchant limits during degradation
>
> **4. Chaos engineering:**
> Regular drills:
> - Simulate acquirer failure → verify routing failover works
> - Simulate DB standby promotion → verify failover + zero data loss
> - Simulate Kafka broker failure → verify consumer resiliency
> - Simulate Redis failure → verify fallback to DB for idempotency checks
>
> 'The best way to avoid failure is to fail constantly.'"

---

### Architecture Evolution Summary

| Attempt | Key Addition | Problem Solved |
|---------|-------------|----------------|
| 0 | Single server + direct bank API | Basic payment processing |
| 1 | Idempotency + tokenization + state machine + webhooks | Prevents double-charge, PCI compliance, merchant notifications |
| 2 | Multi-acquirer routing + fraud detection | Higher auth success rate, reduced fraud/chargebacks |
| 3 | Double-entry ledger + reconciliation + settlement | Financial integrity, money tracking, merchant payouts |
| 4 | Multi-region DR + observability + circuit breakers | High availability, debuggability, resilience |
| 5 | Sharding + Kafka + peak handling + chaos engineering | Scale, performance, operational maturity |

---

## PHASE 5: Deep Dive — Idempotency & State Machine (~8 min)

**Interviewer:**
Let's deep dive on idempotency. Walk me through what happens when two identical requests arrive at the same millisecond.

**Candidate:**

> "This is the hardest problem in idempotency implementation. Two requests with the same idempotency key hit two different API servers simultaneously.
>
> **Solution: Database unique constraint as atomic test-and-set.**
>
> ```sql
> INSERT INTO idempotency_keys (idempotency_key, merchant_id, status)
> VALUES ($key, $merchant, 'started');
> ```
>
> Only one INSERT succeeds — the other gets a `UniqueConstraintViolation`. The loser knows to wait or return 409.
>
> This is exactly the approach Stripe describes in their engineering blog. The DB's unique constraint provides an atomic test-and-set. No distributed locks, no Redis race conditions — the database does it for you.
>
> **The full flow:**
>
> ```
> Request arrives:
>   1. Try INSERT into idempotency_keys (status = 'started')
>   2. If INSERT succeeds → we own the key, proceed with payment
>   3. If UniqueConstraintViolation → key already exists:
>      a. Fetch existing record
>      b. If request_hash differs → 422 (key reused with different params)
>      c. If status = 'completed' → return stored response (cached result)
>      d. If status = 'failed' → allow retry (re-execute)
>      e. If status = 'started' AND locked_at < 5 min ago → 409 (in progress)
>      f. If status = 'started' AND locked_at > 5 min ago → stale lock,
>         reclaim and retry (server crashed mid-processing)
>   4. After processing, UPDATE idempotency_keys with response
> ```
>
> **Redis as fast-path cache:**
> Redis SETNX in front of the DB check for the 99% case (new key, no collision). But Redis is NOT the source of truth — the DB unique constraint is the authoritative guard. If Redis is unavailable, we fall back to DB-only (slightly slower, still correct).
>
> **Request hash validation:**
> We SHA-256 the request body and store it. If a client reuses an idempotency key with different parameters (different amount, different card), we return 422 rather than silently returning a stale response. This catches accidental key reuse."

**Interviewer:**
What about Stripe's 'atomic phases' pattern? How does that help with crash resilience?

**Candidate:**

> "Stripe's engineering blog describes wrapping the payment flow into phases, each committed to the DB. The idempotency record tracks which phase was last completed — the 'recovery point.'
>
> ```
> Phase 1 → RP_START:           Key inserted, payment record created
> Phase 2 → RP_FRAUD_CHECKED:   Fraud engine passed
> Phase 3 → RP_ACQUIRER_CALLED: Acquirer returned auth code
> Phase 4 → RP_COMPLETED:       Ledger entries created, status updated
> ```
>
> If the server crashes between phases, on retry, it reads the recovery point and resumes from there — skipping already-completed phases. This is critical because Phase 3 (acquirer call) is a non-idempotent external side effect. By recording that the acquirer call completed (with its result), a retry skips the acquirer call entirely and moves straight to Phase 4."

---

### L5 vs L6 vs L7 — Phase 5 Rubric

| Aspect | L5 | L6 | L7 |
|---|---|---|---|
| **Race handling** | "Use a lock" | DB unique constraint as atomic test-and-set, Redis SETNX for fast path | Discusses request hash validation, stale lock reclamation, atomic phases with recovery points |
| **Crash resilience** | Not discussed | "If server crashes, retry re-processes" | Describes recovery points, skip-completed-phases pattern, acquirer call is the non-idempotent boundary |
| **Multi-layer dedup** | API-level only | API + DB constraints | API + Redis SETNX + DB constraint + acquirer reference ID (4 layers of defense in depth) |

---

## PHASE 6: Deep Dive — Payment Routing & Acquirer Integration (~5 min)

**Interviewer:**
Tell me more about the routing engine. How do you decide which acquirer to send a transaction to?

**Candidate:**

> "The routing engine is a decision function: given a transaction's attributes, select the acquirer that maximizes probability of authorization while minimizing cost.
>
> **Input features for routing:**
>
> | Feature | Why it matters |
> |---------|---------------|
> | Card network (Visa/MC/Amex) | Some acquirers have better Visa rates, others better MC |
> | Card issuing country | Local acquiring (same country as issuer) has 5-15% higher success rate |
> | Transaction amount | Some acquirers have per-transaction limits |
> | Currency | Cross-currency transactions need acquirers that support the currency pair |
> | Merchant MCC (category) | Acquirer specialization — some are better for travel, others for retail |
> | Time of day | Acquirer success rates can vary by time (batch processing windows) |
> | Acquirer health (real-time) | Circuit breaker state, recent error rates over sliding window |
> | Acquirer cost | Interchange + acquirer markup. Route to cheapest if success rates are comparable |
>
> **Routing algorithm:**
>
> ```
> function selectAcquirer(transaction):
>     candidates = getHealthyAcquirers()  // Filter out circuit-breakered acquirers
>     candidates = filterByCapability(candidates, transaction)  // Must support card network, currency
>
>     // Score each candidate
>     for acquirer in candidates:
>         successRate = getRecentSuccessRate(acquirer, transaction.cardNetwork,
>                                           transaction.issuingCountry, window=1h)
>         cost = getEffectiveCost(acquirer, transaction)
>         score = w1 * successRate - w2 * cost  // Weighted optimization
>         acquirer.score = score
>
>     return candidates.sortByScore().first()
> ```
>
> **Cascade retry on soft decline:**
>
> ```
> function processPayment(transaction):
>     acquirerList = rankAcquirers(transaction)  // Ordered by score
>
>     for acquirer in acquirerList:
>         result = acquirer.authorize(transaction)
>
>         if result.approved:
>             return result
>         if result.isHardDecline():  // insufficient_funds, stolen_card
>             return result  // Don't retry — answer won't change
>         if result.isSoftDecline():  // processing_error, issuer_unavailable
>             continue  // Try next acquirer
>
>     return DECLINED  // All acquirers failed
> ```
>
> Soft decline → retry with next acquirer. Hard decline → stop. This distinction is critical — retrying a 'stolen card' decline just wastes time."

---

## PHASE 7: Deep Dive — Ledger & Settlement (~5 min)

**Interviewer:**
Walk me through what happens when a merchant requests a payout. How does the settlement engine work?

**Candidate:**

> "Settlement is the process of calculating what we owe each merchant and transferring it to their bank.
>
> **Settlement calculation:**
>
> ```
> For merchant merch_abc, settlement period: 2025-01-14 to 2025-01-15:
>
>   Captured payments:     +$10,000.00  (sum of all captures)
>   Refunds:               -$500.00     (sum of all refunds)
>   Chargebacks:           -$200.00     (sum of lost disputes)
>   Chargeback fees:       -$30.00      (2 disputes × $15 each)
>   PSP processing fees:   -$320.00     (captured × fee rate)
>   ──────────────────────────────────
>   Net settlement amount: $8,950.00
> ```
>
> **Settlement process:**
>
> 1. **Batch job** runs at end of settlement period (e.g., daily at midnight UTC)
> 2. **Query** all captured/refunded/disputed payments for each merchant in the period
> 3. **Calculate** net amount (captured - refunded - chargebacks - fees)
> 4. **Create ledger entries:**
>    ```
>    DR merchant:merch_abc:pending   $8,950  (liability ↓)
>    CR merchant:merch_abc:available $8,950  (liability ↑, ready for payout)
>    ```
> 5. **Initiate bank transfer** (ACH/SEPA/NEFT) for $8,950 to merchant's bank account
> 6. **On bank confirmation**, create final ledger entries:
>    ```
>    DR merchant:merch_abc:available $8,950  (liability ↓)
>    CR settlement_bank_account     $8,950  (asset ↓ — money left PSP's bank)
>    ```
>
> **Settlement cycles by risk tier:**
>
> | Merchant Risk Tier | Settlement Cycle | Why |
> |---|---|---|
> | Low risk (established) | T+1 (next business day) | Trust built over time, low chargeback rate |
> | Medium risk (new) | T+2 to T+3 | Need time to detect fraud patterns |
> | High risk (gambling, adult) | T+7 to T+14 | Higher chargeback rates, need reserve |
>
> We hold a **rolling reserve** (e.g., 10% of captured amount) for high-risk merchants to cover chargebacks that arrive after settlement."

---

### L5 vs L6 vs L7 — Phase 6-7 Rubric

| Aspect | L5 | L6 | L7 |
|---|---|---|---|
| **Routing** | "Pick the cheapest acquirer" | Success rate optimization, cascade retry, soft vs hard decline, local acquiring | ML-based routing, A/B testing routing strategies, multi-variate optimization |
| **Settlement** | "Transfer money to merchant" | Net calculation (captures - refunds - fees), settlement cycles by risk tier, rolling reserves | Discusses netting across merchants, cross-border settlement, multi-currency netting, interchange optimization |
| **Ledger** | "Record transactions" | Double-entry with debit/credit pairs, same-DB ACID, immutable append-only | Discusses event sourcing duality, materialized balance views, snapshot + replay, cross-currency ledger entries |

---

## PHASE 8: Deep Dive — Fraud Detection (~5 min)

**Interviewer:**
Your fraud engine has rule-based and ML layers. How do you balance false positives vs false negatives?

**Candidate:**

> "This is the central tension in fraud detection. False positives (blocking legitimate transactions) cost revenue. False negatives (accepting fraud) cost chargebacks + reputation.
>
> **The three-layer approach:**
>
> ```
> Transaction arrives
>     │
>     ▼
> ┌────────────────────┐
> │ Layer 1: Rules     │ ◀── Hard rules. Deterministic. <5ms.
> │ (blocklist, velocity│    Block known-bad BINs, IPs.
> │  limits, geo check) │    Velocity: >5 txns/min from same card.
> └─────────┬──────────┘
>           │ passed
>           ▼
> ┌────────────────────┐
> │ Layer 2: ML Score  │ ◀── XGBoost model. Features: amount, MCC,
> │ (risk score 0-100) │    device fingerprint, time of day, history.
> │                    │    Score < 30 → approve. Score > 80 → decline.
> └─────────┬──────────┘    Score 30-80 → gray zone → Layer 3.
>           │ gray zone
>           ▼
> ┌────────────────────┐
> │ Layer 3: 3DS       │ ◀── Trigger 3D Secure challenge.
> │ Challenge          │    OTP or biometric authentication.
> │                    │    Shifts liability to issuer.
> └────────────────────┘
> ```
>
> **Tuning the threshold per merchant vertical:**
>
> | Vertical | Risk Tolerance | Why |
> |----------|---------------|-----|
> | Digital subscriptions | Higher tolerance (lower threshold) | Low ticket size, instant delivery, low chargeback cost |
> | Luxury goods | Lower tolerance (higher threshold) | High ticket size, physical goods, high chargeback cost |
> | Travel | Much lower tolerance | Very high ticket size, advance booking = delayed fraud detection |
>
> A luxury goods merchant wants us to be aggressive — block more, even if it means some false positives. A digital subscription merchant wants minimal friction — approve more, accept slightly higher fraud rate. We expose configurable risk thresholds per merchant.
>
> **Network-level signals (Stripe Radar-like):**
> Because we process transactions across thousands of merchants, we see patterns invisible to any single merchant. If a card is used fraudulently at Merchant A, we can block it at Merchant B before the chargeback arrives. This cross-merchant intelligence is a major competitive advantage for a PSP."

---

## PHASE 9: Deep Dive — Data Storage & PCI Compliance (~5 min)

**Interviewer:**
Talk me through the data storage architecture. How does PCI DSS shape your decisions?

**Candidate:**

> "PCI DSS is not a checkbox — it's an architectural constraint that shapes the entire system.
>
> **The CDE (Cardholder Data Environment) boundary:**
>
> ```
> ┌──────────────────────────────────────────────────────────────┐
> │                    MAIN INFRASTRUCTURE                       │
> │  (NOT in PCI scope if card data never touches it)           │
> │                                                              │
> │  ┌────────────┐  ┌──────────┐  ┌──────────┐  ┌──────────┐  │
> │  │ API Gateway │  │ Payment  │  │ Fraud    │  │ Ledger   │  │
> │  │            │  │ Service  │  │ Engine   │  │ Service  │  │
> │  │ Sees tokens│  │ Sees     │  │ Sees     │  │ Sees     │  │
> │  │ only       │  │ tokens   │  │ tokens   │  │ amounts  │  │
> │  │            │  │ only     │  │ + BIN    │  │ only     │  │
> │  └────────────┘  └──────────┘  └──────────┘  └──────────┘  │
> └───────────────────────────┬──────────────────────────────────┘
>                             │ token
>                             ▼
> ┌──────────────────────────────────────────────────────────────┐
> │  CDE (Cardholder Data Environment)                          │
> │  Separate network segment. Heavily restricted access.       │
> │  Annual PCI DSS audit on THIS segment only.                │
> │                                                              │
> │  ┌────────────────────────────────────────────────────────┐  │
> │  │ Token Vault Service                                    │  │
> │  │                                                        │  │
> │  │ token ──▶ decrypt ──▶ raw PAN ──▶ send to acquirer     │  │
> │  │                                                        │  │
> │  │ Encrypted at rest (AES-256)                            │  │
> │  │ Keys managed by HSM (Hardware Security Module)         │  │
> │  │ TLS 1.3 in transit                                     │  │
> │  │ Access logs for every read                             │  │
> │  └────────────────────────────────────────────────────────┘  │
> └──────────────────────────────────────────────────────────────┘
> ```
>
> **Storage decisions:**
>
> | Data | Store | Why |
> |------|-------|-----|
> | Payment state, ledger entries, idempotency keys | **PostgreSQL** (sharded by merchant_id) | ACID transactions essential. Payment + ledger must be atomic. |
> | Raw card data (PAN, expiry) | **Token Vault** (encrypted DB in CDE) | PCI DSS requires isolation. HSM for key management. |
> | Idempotency fast-path | **Redis cluster** | Sub-ms lookup for 99% of idempotency checks. TTL for auto-cleanup. |
> | Payment events | **Kafka** | Durable, ordered event stream. Partitioned by payment_id. |
> | Webhook payloads, dispute evidence | **S3** | Unstructured data, long-term retention. |
> | Analytics, reporting | **Data warehouse** (CDC from PostgreSQL → Kafka → warehouse) | Near-real-time analytics without impacting transactional DB. |
>
> **Why PostgreSQL and not Cassandra:**
> Payment systems need ACID transactions (payment status + ledger entries in one transaction). Cassandra gives you AP (available, partition-tolerant) with eventual consistency. Payment balances cannot be eventually consistent. PostgreSQL gives you CP (consistent, partition-tolerant) with strong consistency. The trade-off: horizontal scaling is harder (sharding), but correctness is guaranteed."

---

## PHASE 10: Wrap-Up & Summary (~5 min)

**Interviewer:**
Good deep dives. Last question: what keeps you up at night with this system?

**Candidate:**

> "Five things:
>
> **1. The unknown state problem.** When an acquirer call times out, we don't know if the payment was captured or not. We can't assume success (double-capture risk) or failure (lost revenue risk). We have timeout resolution protocols, but every unknown-state payment is a potential financial discrepancy until resolved. This is the hardest problem in payment engineering.
>
> **2. Reconciliation gaps.** Even with three-way reconciliation, discrepancies happen — timing differences, currency conversion rounding, fee calculation differences. At scale (millions of transactions/day), even a 0.01% discrepancy rate means hundreds of transactions to investigate daily. Reconciliation is never 'done.'
>
> **3. PCI scope creep.** Every new feature, every new service that touches card data expands the CDE and the audit scope. There's constant pressure to add features that require card data access. One engineer accidentally logging a PAN to a general-purpose logging system means our entire logging infrastructure is now in PCI scope.
>
> **4. Hot merchant concentration risk.** If one large merchant (10% of our volume) has a sudden traffic spike or is targeted by a fraud ring, it can affect infrastructure shared with other merchants. Shard isolation helps, but shared resources (Kafka, Redis, network) are still contention points.
>
> **5. Regulatory divergence.** PSD2/SCA in Europe, RBI guidelines in India, PCI DSS globally, GDPR data residency — each jurisdiction adds requirements. A transaction from a US card used in Europe with the merchant in India touches multiple regulatory regimes simultaneously."

**Interviewer:**
Strong answer. You've covered idempotency, financial integrity, routing, fraud, and operational concerns. I particularly liked how you justified design choices — not just 'what' but 'why this and not that.' The active-passive vs active-active reasoning was strong, as was the idempotency-over-availability prioritization.

---

### L5 vs L6 vs L7 — Wrap-Up Rubric

| Aspect | L5 | L6 | L7 |
|---|---|---|---|
| **Operational concerns** | "Need monitoring and backups" | Unknown state problem, reconciliation gaps, PCI scope creep | All of L6 + regulatory divergence across jurisdictions, compliance automation, multi-party financial risk |
| **System thinking** | Thinks in components | Thinks in interactions between components and failure modes | Thinks in organizational implications — how the system shapes team structure, on-call burden, vendor relationships |
| **Trade-off awareness** | Makes choices without discussing alternatives | Explains "why this AND why not the alternative" (PostgreSQL vs Cassandra, active-passive vs active-active) | Discusses when the alternative wins and what would change the decision (e.g., "if we hit 100K TPS, active-active per-shard becomes necessary") |

---

## Supporting Deep-Dive Documents

| Doc | Topic | Link |
|-----|-------|------|
| 02 | Payment Platform API Contracts | [02-api-contracts.md](02-api-contracts.md) |
| 03 | Payment Flow Lifecycle (auth → capture → settlement) | [03-payment-flow-lifecycle.md](03-payment-flow-lifecycle.md) |
| 04 | Idempotency & Exactly-Once Semantics | [04-idempotency-and-exactly-once.md](04-idempotency-and-exactly-once.md) |
| 05 | Ledger & Double-Entry Bookkeeping | [05-ledger-and-double-entry.md](05-ledger-and-double-entry.md) |
| 06 | Fraud Detection & Risk Engine | [06-fraud-detection.md](06-fraud-detection.md) |
| 07 | Smart Payment Routing & Acquiring | [07-payment-routing.md](07-payment-routing.md) |
| 08 | Data Storage & Infrastructure | [08-data-storage-and-infrastructure.md](08-data-storage-and-infrastructure.md) |
| 09 | Reliability, SLAs & Observability | [09-reliability-and-observability.md](09-reliability-and-observability.md) |
| 10 | Scaling & Performance | [10-scaling-and-performance.md](10-scaling-and-performance.md) |
| 11 | Design Philosophy & Trade-offs | [11-design-trade-offs.md](11-design-trade-offs.md) |
