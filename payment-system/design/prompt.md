Design a Payment System (like Stripe / Razorpay) as a system design interview simulation.

## Template
Follow the EXACT same format as the S3 interview simulation at:
src/hld/aws/s3/design/interview-simulation.md

Same structure:
- Interviewer/Candidate dialogue format
- PHASE 1: Opening & Problem Statement
- PHASE 2: Requirements Gathering & Scoping (functional + non-functional + scale numbers)
- PHASE 3: API Design
- PHASE 4: High-Level Architecture (iterative build-up: Attempt 0 → 1 → 2 → ..., starting from single server, finding problems, evolving)
- PHASE 5+: Deep dives on each component
- L5/L6/L7 rubric table after EACH deep dive phase — showing exactly how an L5, L6, and L7 candidate would answer differently. This is critical for understanding what "good" looks like at each level.
- Architecture evolution table after each phase
- Wrap-up phase with "what keeps you up at night"
- Links to supporting deep-dive docs

## Candidate Level
**L6 SDE-3 (Senior SDE)** candidate being interviewed by a Principal Engineer (L8).

The candidate's answers should demonstrate L6-level depth:
- Not just "what" but "why" and "why not the alternative"
- Quantitative reasoning (back-of-envelope math, concrete numbers)
- Awareness of operational concerns (monitoring, failure modes, blast radius)
- Proactive identification of tradeoffs before the interviewer asks

The L5/L6/L7 rubric tables after each phase should make it crystal clear how an L5 answer differs from L6 differs from L7.

## Output location
Create all files under: src/hld/payment-system/design/

## Files to create

### 1. 01-interview-simulation.md — the main backbone
The full interview dialogue covering all phases and attempts.

### 2. 02-api-contracts.md — Payment Platform APIs

This doc should list all the major API surfaces of a payment platform (like Stripe). The interview simulation (Phase 3) will only cover a subset — this doc is the comprehensive reference.

**Structure**: For each API group, list every endpoint with HTTP method, path, request/response shape, and a brief description. Mark endpoints covered in the interview with a star or highlight.

**API groups to cover**:

- **Payment Intent / Order APIs**: The core payment flow. `POST /payments` (create a payment intent — amount, currency, payment method, idempotency key, metadata), `GET /payments/{paymentId}` (get payment status and history), `POST /payments/{paymentId}/capture` (capture an authorized payment — supports partial capture), `POST /payments/{paymentId}/cancel` (cancel/void an uncaptured authorization). Include the distinction between **authorize-only** vs **authorize-and-capture** flows. Explain why two-step (auth + capture) exists — hotels, e-commerce with delayed fulfillment, tips/gratuity.

- **Refund APIs**: `POST /payments/{paymentId}/refund` (full or partial refund — amount, reason, idempotency key), `GET /refunds/{refundId}` (refund status). Explain that refunds are NOT reverse payments — they create new transactions in the opposite direction. Refund timelines differ by payment method (cards: 5-10 business days, UPI: instant, bank transfer: 3-5 days).

- **Payment Method APIs**: `POST /payment-methods` (tokenize a card/bank account — PAN, expiry, CVV are never stored raw), `GET /payment-methods/{token}` (retrieve masked card details), `DELETE /payment-methods/{token}` (detach/delete a saved method), `GET /customers/{customerId}/payment-methods` (list saved methods). Tokenization is critical for PCI DSS compliance — the PSP stores card data in a secure vault, the merchant only sees a token.

- **Customer APIs**: `POST /customers` (create a customer record), `GET /customers/{customerId}`, `PUT /customers/{customerId}`, `DELETE /customers/{customerId}`. Customers link to payment methods, subscriptions, and transaction history.

- **Webhook / Event APIs**: `POST /webhooks` (register a webhook URL + events to subscribe to), `GET /webhooks/{webhookId}`, `DELETE /webhooks/{webhookId}`, `GET /events` (paginated event log). Webhooks are the primary mechanism for async notifications — payment succeeded, payment failed, refund completed, dispute opened. Webhook delivery must be **at-least-once** with retries and exponential backoff. Merchants must handle **idempotent webhook processing** (same event delivered multiple times).

- **Dispute / Chargeback APIs**: `GET /disputes/{disputeId}` (dispute details — reason code, amount, deadline), `POST /disputes/{disputeId}/respond` (submit evidence — receipt, delivery proof, communication logs), `POST /disputes/{disputeId}/accept` (accept the chargeback). Disputes have strict deadlines (typically 7-21 days). The PSP must track dispute lifecycle: opened → evidence submitted → won/lost.

- **Payout / Settlement APIs**: `POST /payouts` (initiate payout to merchant's bank account), `GET /payouts/{payoutId}`, `GET /balance` (available balance, pending balance, reserved balance). Settlement is the process of moving captured funds from the PSP's pooled account to the merchant's bank account. Settlement cycles vary: T+1, T+2, T+7 depending on merchant risk tier and geography.

- **Subscription / Recurring Payment APIs**: `POST /subscriptions` (plan, customer, payment method, billing anchor date), `GET /subscriptions/{subscriptionId}`, `POST /subscriptions/{subscriptionId}/cancel`, `PUT /subscriptions/{subscriptionId}` (upgrade/downgrade plan). Must handle: proration on plan changes, dunning (retry failed payments with backoff), grace periods, trial periods.

- **Ledger / Reporting APIs** (internal): `GET /ledger/entries` (double-entry ledger — every financial event creates a debit + credit pair), `GET /reports/settlement` (daily/weekly settlement reports), `GET /reports/reconciliation` (match internal records against bank/network statements). Reconciliation is the unsung hero of payment systems — it catches discrepancies between what you think happened and what actually happened.

- **Admin / Ops APIs** (internal): `GET /health`, `GET /metrics`, `POST /config/routing-rules` (configure payment routing — which acquirer/processor to use for which card type/geography), `POST /config/risk-rules` (configure fraud detection rules).

**Contrast with PayPal's API model**: PayPal's API is buyer-centric (PayPal wallet, buyer protection, PayPal checkout flow). Stripe's API is developer/merchant-centric (embeddable, API-first, payment method agnostic). PayPal bundles identity + wallet + payments; Stripe separates them. Razorpay is similar to Stripe but adds India-specific payment methods (UPI, Netbanking, Wallets) and settlement infrastructure.

**Interview subset**: In the interview (Phase 3), focus on: create payment (the core idempotent payment flow), capture (two-step payment), refund (reverse flow), webhooks (async notification), and ledger entries (financial integrity). The full API list lives in this doc.

### 3. 03-payment-flow-lifecycle.md — Payment Processing Pipeline

The end-to-end lifecycle of a payment from initiation to settlement.

- **Payment flow overview**: Merchant → PSP (Payment Service Provider) → Payment Gateway → Card Network (Visa/Mastercard) → Issuing Bank → Acquiring Bank → Settlement.
- **Key actors**: Cardholder, Merchant, Acquirer (merchant's bank), Issuer (cardholder's bank), Card Network (Visa/Mastercard/RuPay), PSP (Stripe/Razorpay).
- **Authorization flow**: Merchant sends auth request → PSP routes to acquirer → acquirer forwards to card network → card network forwards to issuer → issuer checks funds, fraud rules, 3DS → issuer returns auth code → response flows back. Total latency: 1-3 seconds.
- **Capture flow**: Merchant sends capture request (within auth validity window — typically 7 days for cards). Capture creates a clearing record. Batch capture is common — merchants capture at end of day.
- **Settlement flow**: Card network runs batch settlement (typically daily). Nets out transactions between issuers and acquirers. Acquirer credits merchant's account minus interchange fee + network fee + acquirer fee. Settlement cycles: T+1 to T+7 depending on geography and risk.
- **Interchange economics**: Interchange fee (paid by merchant's bank to cardholder's bank, ~1.5-3% for credit cards, ~0.5-1% for debit). Network fee (Visa/Mastercard, ~0.1-0.15%). Acquirer markup (~0.2-0.5%). Total merchant discount rate (MDR): ~2-3.5% for credit cards. Contrast: UPI in India has zero MDR (government mandate), which drives adoption.
- **3D Secure (3DS)**: Additional authentication step. 3DS 1.0 (redirect to bank page — high drop-off). 3DS 2.0 (in-app/in-browser challenge, risk-based — low-risk transactions skip challenge, ~10x lower drop-off than 3DS 1.0). Liability shift: if 3DS is used, fraud liability shifts from merchant to issuer.
- **Alternative payment methods**: UPI (India — real-time, P2P/P2M, zero MDR), SEPA (Europe — bank-to-bank, 1-2 day settlement), ACH (US — batch-based bank transfer, 1-3 day settlement), wallets (PayPal, Apple Pay, Google Pay — tokenized card-on-file). Each method has a different flow, latency, and cost profile.
- **Contrast with e-commerce payment (Amazon Pay)**: Amazon Pay bundles shipping address + payment method + trust. Pure PSPs like Stripe are payment-method agnostic and don't bundle identity/address.

### 4. 04-idempotency-and-exactly-once.md — Idempotency & Reliability

The single most important property of a payment system: **a payment must never be charged twice**.

- **Why idempotency is critical**: Network failures, client retries, load balancer retries, message queue redelivery — all can cause duplicate requests. Without idempotency, a retry can charge the customer twice. This is a financial and legal liability.
- **Idempotency key design**: Client provides a unique key (UUID) with each request. Server stores key → response mapping. On retry, server returns the stored response without re-executing the operation.
- **Idempotency key storage**: Must be durable (not in-memory cache). Redis with TTL for short-term dedup + persistent DB for long-term audit. Key expiry: typically 24-48 hours (long enough to cover all retry windows).
- **Idempotency at each layer**: API layer (dedup client retries), message queue layer (dedup consumer retries), database layer (unique constraints on transaction IDs), downstream processor layer (each PSP-to-acquirer call has its own idempotency mechanism).
- **Exactly-once semantics**: True exactly-once is impossible in distributed systems (FLP impossibility). Payment systems achieve **effectively-once** through: idempotency keys + at-least-once delivery + idempotent handlers.
- **State machine for payments**: `CREATED → AUTHORIZED → CAPTURED → SETTLED` (happy path). `CREATED → AUTHORIZED → VOIDED` (cancellation). `CAPTURED → PARTIALLY_REFUNDED → FULLY_REFUNDED`. State transitions must be **atomic** and **monotonic** (no going backward). Invalid transitions are rejected.
- **Distributed transaction handling**: Payment involves multiple systems (PSP DB, acquirer, card network). No distributed transactions (2PC is too slow and fragile). Instead: **Saga pattern** — each step has a compensating action. If capture fails after auth succeeded, the compensating action is to void the auth.
- **Timeout handling**: If a downstream call times out, the payment is in an **unknown state**. You cannot assume success OR failure. Resolution: (1) query the downstream system for status, (2) if that also fails, mark as "pending_resolution" and reconcile asynchronously, (3) NEVER auto-retry a timed-out payment without checking status first.
- **Contrast with e-commerce order processing**: An e-commerce order can be retried or resubmitted. A payment cannot — double-charging is unacceptable. This makes payment systems far more sensitive to idempotency than typical CRUD applications.

### 5. 05-ledger-and-double-entry.md — Ledger & Financial Integrity

The ledger is the source of truth for all money movement. Every cent must be accounted for.

- **Double-entry bookkeeping**: Every financial event creates exactly two entries — a debit and a credit of equal amount. The sum of all debits must equal the sum of all credits at all times. This invariant is the foundation of financial integrity.
- **Account model**: Internal accounts — merchant balance account, merchant pending account, PSP fee account, refund reserve account, settlement account, tax account. Each payment creates entries across multiple accounts.
- **Example — card payment capture**:
  - Debit: Customer's card (external) — $100
  - Credit: Merchant pending account — $97.10 (after fees)
  - Credit: PSP fee account — $2.90 (PSP's revenue)
- **Example — refund**:
  - Debit: Merchant balance account — $97.10
  - Credit: Customer's card (external) — $100
  - Debit: PSP fee account — $2.90 (fee reversal, depending on policy)
- **Immutability**: Ledger entries are **append-only**. You never update or delete an entry. Corrections are made by adding new compensating entries. This creates a complete audit trail.
- **Reconciliation**: Three-way reconciliation — (1) internal ledger vs (2) acquirer/bank statements vs (3) card network settlement files. Discrepancies are flagged for manual review. Reconciliation runs daily/hourly. Common discrepancies: timing differences (transaction posted today, settled tomorrow), currency conversion differences, fee calculation differences.
- **Currency handling**: Store amounts in the **smallest currency unit** (cents for USD, paise for INR) as integers — never floats. Multi-currency transactions require recording the exchange rate at the time of transaction.
- **Audit trail**: Every state change, every ledger entry, every admin action is logged with timestamp, actor, and reason. Required for PCI DSS, SOX compliance, and dispute resolution.
- **Contrast with general-purpose accounting software**: Payment ledgers must handle millions of transactions per day with sub-second latency. Traditional accounting software (QuickBooks, SAP) is designed for batch processing. Payment ledgers need real-time balance queries (to check if a merchant has sufficient balance for a payout).

### 6. 06-fraud-detection.md — Fraud Detection & Risk Engine

Fraud is an existential threat to payment systems. Too little detection → financial losses. Too much detection → legitimate transactions blocked (false positives → lost revenue).

- **Types of payment fraud**: Card-not-present (CNP) fraud (stolen card details used online), account takeover (ATO), friendly fraud (legitimate cardholder disputes a valid charge), merchant fraud (fake merchants processing fraudulent transactions), refund abuse.
- **Rule-based engine (Layer 1)**: Deterministic rules — velocity checks (>5 transactions in 1 minute from same card), amount thresholds (single transaction >$10,000), geographic mismatch (card issued in US, transaction from Nigeria), BIN-country mismatch, blocked BIN/IP lists. Rules are fast but brittle — fraudsters adapt quickly.
- **ML-based scoring (Layer 2)**: Real-time ML model scores each transaction (0-100 risk score). Features: transaction amount, merchant category, time of day, device fingerprint, IP geolocation, historical patterns, velocity features. Models: gradient boosted trees (XGBoost/LightGBM) for tabular data, neural networks for sequence modeling (transaction history). Model retraining: daily/weekly on labeled fraud data.
- **3D Secure challenge (Layer 3)**: For transactions in the "gray zone" (not clearly fraud, not clearly legitimate), trigger 3DS challenge (OTP, biometric). This shifts liability to the issuer and provides additional authentication.
- **Post-authorization checks**: Even after authorization, run async checks — device reputation, email reputation (is the email from a disposable domain?), shipping address analysis. Can trigger delayed capture hold or manual review.
- **Dispute/chargeback handling**: When a cardholder disputes a charge, the PSP must provide evidence to the issuer within the deadline. Win rate depends on evidence quality. High chargeback rates (>1%) can result in card network penalties or merchant account termination.
- **Balancing precision vs recall**: High precision (few false positives) → more fraud slips through → financial losses. High recall (catch all fraud) → many false positives → legitimate customers blocked → lost revenue and bad UX. The optimal threshold depends on merchant vertical (luxury goods need tighter controls than digital subscriptions).
- **Contrast with banking fraud detection**: Banks detect fraud on the issuer side (is the cardholder's card being misused?). PSPs detect fraud on the acquirer side (is this merchant/transaction legitimate?). Different perspectives, different signals, complementary.

### 7. 07-payment-routing.md — Smart Payment Routing & Acquiring

Not all payment processors are equal. Smart routing optimizes for success rate, cost, and latency.

- **Why routing matters**: A payment platform integrates with multiple acquirers/processors (e.g., Stripe connects to dozens of acquiring banks worldwide). The same transaction can be routed to different acquirers. Routing decisions affect: authorization success rate (can vary 5-15% between acquirers), processing cost (interchange + acquirer markup), latency, and currency conversion costs.
- **Routing factors**: Card type (credit/debit/prepaid), card network (Visa/Mastercard/Amex/RuPay), issuing country, merchant category code (MCC), transaction amount, currency, acquirer success rate history, acquirer cost, acquirer latency, acquirer uptime.
- **Static routing rules**: Route Visa to Acquirer A, Mastercard to Acquirer B, domestic transactions to local acquirer, international to global acquirer. Simple but suboptimal.
- **Dynamic/smart routing**: Real-time optimization. Track per-acquirer success rates over sliding windows. Route to the acquirer with the highest expected success rate for this specific card type + geography + amount combination. Cascade: if primary acquirer declines, auto-retry with secondary acquirer (only for soft declines — "insufficient funds" is a hard decline, don't retry).
- **Failover**: If an acquirer is down (health check fails), automatically route to backup acquirer. Circuit breaker pattern on acquirer connections.
- **Cost optimization**: Some acquirers offer lower interchange for specific card types or geographies. Route to minimize total cost while maintaining acceptable success rate. Trade-off: cheapest acquirer may not have the highest success rate.
- **Local acquiring**: Processing a transaction through a local acquirer (same country as the card issuer) typically has higher success rates and lower fees than cross-border acquiring. A global PSP needs acquiring relationships in every major market.
- **Contrast with single-acquirer setup**: Small merchants use a single acquirer (e.g., their bank). No routing optimization, no failover. A PSP's value proposition is abstracting acquirer complexity — the merchant sends one API call, the PSP handles routing, retries, and failover.

### 8. 08-data-storage-and-infrastructure.md — Data Storage & Infrastructure

- **Primary database (transactions)**: Relational DB (PostgreSQL/MySQL) for transactional data — payments, refunds, customers, ledger entries. ACID transactions are essential (a payment capture must atomically update the payment status AND create ledger entries). Sharding strategy: shard by merchant ID (all of a merchant's data on the same shard for transactional integrity).
- **Event store**: Append-only event log for every state change. Powers: audit trail, event sourcing (rebuild state from events), async processing (drive webhooks, analytics, reconciliation). Implementation: Kafka (durable, ordered, partitioned by payment ID).
- **Idempotency store**: Redis cluster with TTL-based expiry for idempotency keys. Fast lookup (sub-ms), durable enough (Redis persistence + replication), auto-cleanup via TTL.
- **Token vault (PCI DSS)**: Isolated, hardened database for storing raw card data (PAN, expiry). Encrypted at rest (AES-256) and in transit (TLS 1.3). Access restricted to tokenization service only. Separate network segment (CDE — Cardholder Data Environment). Annual PCI DSS audit required. Most PSPs use HSMs (Hardware Security Modules) for key management.
- **Document store**: For unstructured/semi-structured data — webhook payloads, dispute evidence, merchant onboarding documents. MongoDB or S3.
- **Analytics / Data warehouse**: Transaction data replicated to analytical store (Snowflake, BigQuery, or Redshift) for reporting, reconciliation, ML feature engineering. CDC (Change Data Capture) from primary DB → Kafka → warehouse.
- **Caching**: Redis for hot data — merchant config (routing rules, risk rules), exchange rates, BIN lookup tables. Cache invalidation on config changes.
- **Multi-region considerations**: Payment data has residency requirements (EU data must stay in EU — GDPR). Active-passive or active-active depending on scale. Active-active is harder because of financial consistency requirements — unlike Netflix (eventual consistency is OK for viewing history), payment balances must be strongly consistent.
- **Contrast with Netflix's storage**: Netflix is read-heavy (encode once, stream billions of times) and tolerates eventual consistency. Payment systems are write-heavy (every transaction is a write) and require strong consistency for financial data. Netflix uses Cassandra (AP); payment systems use PostgreSQL/MySQL (CP). Different CAP trade-offs driven by different correctness requirements.

### 9. 09-reliability-and-observability.md — Reliability, SLAs & Observability

Payment systems have the highest reliability bar — downtime directly equals lost revenue for every merchant.

- **SLA targets**: 99.99% uptime (52 minutes downtime/year). p99 latency <500ms for payment authorization. Zero data loss (RPO = 0). RTO < 5 minutes.
- **Health monitoring**: Per-acquirer success rate dashboards (if Acquirer A's success rate drops from 95% to 80%, alert immediately — likely an acquirer-side issue). Per-payment-method success rates. Latency percentiles (p50, p95, p99) per API endpoint.
- **Alerting hierarchy**: p99 latency spike → page on-call. Success rate drop >5% → page on-call. Any payment stuck in "pending" >10 minutes → alert. Reconciliation discrepancy >$1000 → alert finance team.
- **Distributed tracing**: Every payment request gets a trace ID that flows through all services — API gateway → payment service → fraud engine → acquirer adapter → ledger service. Essential for debugging "why did this payment fail?" across 5+ services.
- **Dead letter queues (DLQ)**: Failed webhook deliveries, failed ledger postings, failed settlement records → DLQ. Ops team reviews and replays. No financial event is silently dropped.
- **Circuit breakers**: On acquirer connections (if acquirer is unhealthy, fail fast and route to backup). On webhook delivery (if merchant's webhook endpoint is down, back off exponentially, don't hammer it).
- **Graceful degradation**: If fraud engine is down, fall back to rule-based checks only (higher risk tolerance temporarily). If non-critical services (analytics, reporting) are down, payments continue unaffected. Critical path must be minimal: API → payment service → acquirer. Everything else is async.
- **Disaster recovery**: Primary DB with synchronous replication to standby. Automated failover. Regular DR drills. Backup reconciliation — after failover, reconcile primary vs standby to ensure no transactions were lost.
- **Contrast with Netflix's reliability**: Netflix can show a generic "Popular" row if recommendations are down — degraded but functional. A payment system cannot "degrade" a payment — it either succeeds or fails. There's no "generic payment." This makes payment system reliability fundamentally harder.

### 10. 10-scaling-and-performance.md — Scaling & Performance

- **Scale numbers (Stripe-like scale)**:
  - Hundreds of millions of API requests per day.
  - Millions of payment transactions per day.
  - Sub-second authorization latency (p99 <500ms).
  - Thousands of merchants, each with different routing/risk configurations.
  - Multi-currency: 135+ currencies.
  - Global presence: acquiring relationships in 40+ countries.
- **Read vs write characteristics**: Unlike Netflix (extreme read-heavy), payment systems are write-heavy on the critical path (every transaction creates writes: payment record, ledger entries, event log, idempotency key). Reads are important for status queries and dashboards but not on the critical path.
- **Horizontal scaling**: Stateless API servers behind load balancers. Database sharded by merchant ID. Kafka partitioned by payment ID (ensures ordering per payment). Redis cluster for idempotency and caching.
- **Hot merchant problem**: A few large merchants (Amazon, Uber) generate disproportionate traffic. Shard-level hot spots. Solution: dedicated shards for large merchants, or sub-sharding within a merchant by payment ID hash.
- **Batch vs real-time processing**: Authorization/capture — real-time (synchronous, latency-sensitive). Settlement — batch (daily, nightly batch jobs net out transactions). Reconciliation — batch (hourly/daily). Reporting — near-real-time (CDC → warehouse with minutes of lag).
- **Peak traffic handling**: Black Friday, Diwali sales, flash sales — 10-50x normal traffic. Must pre-scale infrastructure. Auto-scaling alone is too slow (acquirer rate limits, DB connection limits). Pre-provisioning + load shedding for graceful degradation.
- **Rate limiting**: Per-merchant rate limits (prevent one merchant from consuming all capacity). Per-IP rate limits (prevent abuse). Adaptive rate limiting (tighten during degradation).
- **Contrast with Netflix scaling**: Netflix scales by adding CDN capacity (more OCAs, more bandwidth). Payment systems scale by adding DB capacity, acquirer connections, and compute. Netflix's bottleneck is bandwidth; payment systems' bottleneck is transactional throughput and consistency.

### 11. 11-design-trade-offs.md — Design Philosophy & Trade-off Analysis

Opinionated analysis of payment system design choices — not just "what" but "why this and not that."

- **Strong consistency vs eventual consistency**: Payment balances MUST be strongly consistent (you cannot show a merchant $1000 available when they only have $500 — they'd initiate a payout and you'd be short). Viewing history (Netflix) can be eventually consistent. This is why payment systems use PostgreSQL (CP) not Cassandra (AP). Trade-off: strong consistency limits horizontal scalability and cross-region replication options.
- **Synchronous vs asynchronous processing**: Authorization must be synchronous (merchant needs an immediate yes/no). Settlement, reconciliation, webhooks are asynchronous. The challenge is the boundary — what happens when a synchronous call times out? You're in an unknown state. Design for it.
- **Idempotency keys (client-generated) vs server-generated dedup**: Stripe uses client-provided idempotency keys. Alternative: server generates a request ID and deduplicates. Client-provided keys give the client control over retry semantics. Server-generated dedup requires the server to define what "duplicate" means (same amount + same card + same merchant within 5 minutes? fragile).
- **Event sourcing vs traditional CRUD**: Event sourcing (append-only event log, derive state from events) is ideal for payments — you get a complete audit trail for free, and you can rebuild state at any point in time. Trade-off: event sourcing adds complexity (event schema evolution, snapshot management, eventual consistency of projections). Many payment systems use a hybrid — traditional DB for current state + event log for audit trail.
- **Monolith vs microservices for payments**: Payment systems often start as monoliths (easier to maintain ACID transactions across payment + ledger + idempotency in a single DB). Microservices introduce distributed transaction complexity (Saga pattern, eventual consistency between services). Stripe started as a monolith and gradually extracted services. Premature microservices in a payment system is dangerous — distributed consistency is hard.
- **Build vs buy (acquirer integrations)**: Each acquirer has a different API, different error codes, different retry semantics. Building and maintaining acquirer adapters is a massive ongoing cost. Some PSPs buy white-label gateway software; others build from scratch. Building gives you control over routing optimization; buying gives you faster time-to-market.
- **PCI DSS scope minimization**: The less of your system that touches raw card data, the smaller your PCI audit scope. Tokenize early (at the client via Stripe.js / Razorpay checkout.js), and the PSP's backend never sees raw PANs — only tokens. This is why Stripe.js exists — it's not just UX, it's compliance architecture.
- **Webhook reliability vs polling**: Webhooks (PSP pushes events to merchant) vs polling (merchant pulls events from PSP). Webhooks are more efficient but unreliable (merchant's server may be down). Best practice: webhooks for real-time notification + polling as fallback + event API for reconciliation. Stripe does all three.

## CRITICAL: Iterative build-up (DO NOT skip this)
The architecture MUST evolve iteratively. Each attempt builds on the previous one by identifying a concrete problem and solving it. The candidate should NOT jump to the final architecture. The progression should feel natural:

### Attempt 0: Single server with direct bank integration
- A monolithic application that directly calls the bank/acquirer API when a merchant submits a payment. Stores payment records in a single PostgreSQL database.
- **Problems found**: No idempotency (network retry = double charge), no support for multiple payment methods, single acquirer (if it's down, all payments fail), card data stored in plain text (PCI violation), no async notifications to merchant.

### Attempt 1: Idempotency + tokenization + basic payment flow
- **Idempotency keys**: Client provides a unique key, server deduplicates. Prevents double charges on retries.
- **Tokenization**: Card data captured via client-side JS SDK, tokenized immediately, raw PAN never hits the server. PCI scope minimized.
- **Payment state machine**: `CREATED → AUTHORIZED → CAPTURED → SETTLED` with atomic transitions.
- **Webhook notifications**: Async notifications to merchants on payment state changes.
- **Problems found**: Single acquirer — no failover, no routing optimization. No fraud detection — accepting everything. Single database — not scalable. No ledger — money movement is tracked ad-hoc.

### Attempt 2: Multi-acquirer routing + fraud detection
- **Smart payment routing**: Integrate with multiple acquirers. Route based on card type, geography, cost, and success rate. Auto-failover on acquirer downtime.
- **Fraud detection engine**: Rule-based engine (velocity checks, amount thresholds, geo mismatch) + ML scoring (risk score per transaction). Transactions above risk threshold → 3DS challenge or decline.
- **Cascade retries**: On soft decline from primary acquirer, auto-retry with secondary acquirer.
- **Problems found**: No financial accounting — can't reconcile, can't generate settlement reports, can't track fees. Single region — regulatory issues (data residency), latency for global merchants.

### Attempt 3: Ledger + reconciliation + settlement engine
- **Double-entry ledger**: Every financial event creates balanced debit + credit entries. Append-only, immutable. Source of truth for all money movement.
- **Reconciliation engine**: Daily/hourly reconciliation — internal ledger vs acquirer statements vs card network files. Flag discrepancies for review.
- **Settlement engine**: Batch job that calculates net amounts owed to each merchant (captured - refunded - fees - chargebacks). Initiates payouts on configured schedule (T+1, T+2, etc.).
- **Reporting**: Real-time dashboards for merchants — payment volume, success rates, fees, settlement status.
- **Problems found**: Single region, no DR. If the database goes down, ALL payments stop. No observability — hard to debug payment failures across multiple services.

### Attempt 4: Reliability hardening + multi-region + observability
- **Database replication**: Synchronous replication to standby. Automated failover with <5 minute RTO.
- **Multi-region**: Active-passive (financial consistency is too critical for active-active in most payment systems). Primary region handles all writes, secondary region is warm standby. Read replicas in secondary region for reporting/analytics.
- **Distributed tracing**: Trace ID flows through every service hop. End-to-end visibility for any payment.
- **Circuit breakers + graceful degradation**: Circuit breakers on acquirer connections, webhook delivery. If fraud engine is down, fall back to rules-only (accept slightly higher risk temporarily).
- **Dead letter queues**: No financial event is silently dropped. Failed operations go to DLQ for manual review.
- **Chaos engineering**: Regularly test acquirer failover, database failover, service failures.
- **Problems found**: Scaling bottlenecks under peak load (Black Friday). Hot merchant problem (large merchants cause shard hotspots).

### Attempt 5: Scale + performance optimization
- **Database sharding**: Shard by merchant ID. Dedicated shards for hot merchants.
- **Event-driven architecture**: Kafka as the event backbone. Payment events → ledger, → webhooks, → analytics, → reconciliation. Decouples critical path from downstream processing.
- **Pre-scaling for peak traffic**: Capacity planning for 10-50x spikes (Black Friday, flash sales). Pre-provisioned infrastructure + load shedding.
- **Async settlement optimization**: Parallel settlement processing. Batch netting to reduce number of bank transfers.
- **Global acquiring**: Local acquiring in major markets for higher success rates and lower costs.

Each attempt MUST:
1. Start by identifying concrete problems with the previous attempt
2. Propose a solution
3. Explain WHY this approach (and why not the alternative — mention Stripe/PayPal/Razorpay choices where relevant)
4. End with "what's still broken?" to motivate the next attempt

## CRITICAL: Accuracy requirements

I DO NOT TRUST AI-GENERATED FACTS ABOUT SPECIFIC SYSTEMS. Every concrete claim about payment system internals must be verifiable against official sources. Specifically:

1. **Use WebSearch and WebFetch tools** to look up Stripe Engineering Blog, Razorpay Engineering Blog, and payment industry documentation BEFORE writing. Search for:
   - "Stripe idempotency keys design"
   - "Stripe payment intents API design"
   - "payment system double entry ledger"
   - "PCI DSS tokenization architecture"
   - "payment gateway routing optimization"
   - "3D Secure 2.0 payment authentication"
   - "payment system reconciliation"
   - "Stripe engineering blog infrastructure"
   - "Razorpay engineering blog architecture"
   - "payment system saga pattern"
   - "interchange fee economics Visa Mastercard"
   - "payment fraud detection ML"
   - "payment system event sourcing"
   - "Stripe webhook delivery reliability"

   **BLANKET PERMISSION**: You have full permission to use WebSearch and WebFetch to read any online documentation, blog posts, or research papers. Do NOT ask the user for permission to read — just read. This applies to stripe.com/blog, engineering blogs, PCI DSS documentation, and any other reference source. Read as many pages as needed to verify facts.

2. **For every concrete number** (interchange rates, settlement timelines, SLA targets, transaction volumes), verify against official sources. If you cannot verify a number, explicitly write "[UNVERIFIED — check source]" next to it.

3. **For every claim about specific company internals** (Stripe's architecture, Razorpay's routing), if it's not from an official source, mark it as "[INFERRED — not officially documented]".

4. **CRITICAL: Do NOT confuse payment system actors.** PSP vs Acquirer vs Issuer vs Card Network are different entities with different roles:
   - PSP (Stripe/Razorpay): Merchant-facing, abstracts payment complexity
   - Acquirer: Merchant's bank, processes transactions on behalf of merchant
   - Issuer: Cardholder's bank, authorizes transactions
   - Card Network (Visa/Mastercard): Routes transactions between acquirer and issuer, sets interchange rules

## Key payment system topics to cover

### Requirements & Scale
- Sub-second payment authorization, 99.99% uptime
- Millions of transactions per day across hundreds of payment methods
- Idempotency as the #1 design requirement — never charge twice
- Multi-currency, multi-geography, multi-acquirer
- PCI DSS compliance as an architectural constraint, not an afterthought

### Architecture deep dives (create separate docs as listed in "Files to create" above)

### Design evolution (iterative build-up — the most important part)
- Attempt 0: Single server + direct bank API
- Attempt 1: Idempotency + tokenization + state machine + webhooks
- Attempt 2: Multi-acquirer routing + fraud detection
- Attempt 3: Ledger + reconciliation + settlement engine
- Attempt 4: Reliability hardening + multi-region + observability
- Attempt 5: Scale + performance optimization

### Consistency & Data
- Strong consistency for payment state and ledger (PostgreSQL, ACID)
- Event sourcing for audit trail (Kafka, append-only)
- Idempotency store (Redis with TTL)
- Token vault (HSM-backed, PCI DSS CDE)
- Eventual consistency acceptable ONLY for non-financial data (analytics, reporting)

## What NOT to do
- Do NOT treat a payment system as "just CRUD with money" — it's a financial system with strict consistency, idempotency, and compliance requirements. Frame it accordingly.
- Do NOT confuse PSP, acquirer, issuer, and card network roles. Each has a distinct role in the payment flow.
- Do NOT jump to the final architecture. Build it step by step (Attempt 0 → 5).
- Do NOT make up internal implementation details — verify against engineering blogs or mark as inferred.
- Do NOT skip the iterative build-up.
- Do NOT write a Wikipedia article. This is an INTERVIEW SIMULATION with dialogue.
- Do NOT describe components without explaining WHY they exist (what problem they solve).
- Do NOT ignore PCI DSS — it's not a checkbox, it shapes the architecture (tokenization, network segmentation, vault isolation).
- Do NOT ask the user for permission to read online documentation — blanket permission is granted (see Accuracy requirements section).
