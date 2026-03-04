# Design Trade-Offs in Payment Systems

Every architecture decision is a trade-off. This document does not present "best practices" in a vacuum. It explains what was chosen, why it was chosen, what was rejected, and under what circumstances the rejected alternative would actually be the better call. These are opinionated positions grounded in how production payment systems actually work.

---

## 1. Strong Consistency vs Eventual Consistency

### The Choice
Payment balances use strong consistency via PostgreSQL (a CP system in CAP terms). Every read of a merchant's balance reflects all committed writes. There is no window where a merchant sees $1,000 when they actually have $500.

### Why This
Money is not a domain where "close enough, eventually" is acceptable. If a merchant reads their balance and it says $10,000, they will make business decisions based on that number — paying suppliers, issuing refunds, withdrawing funds. A stale read that overstates a balance can lead to overdrafts, failed payouts, and broken trust. Strong consistency guarantees that every read reflects the latest committed state.

PostgreSQL with synchronous replication gives us serializable or read-committed isolation. Combined with row-level locking on balance updates, we get a system where concurrent debits and credits never produce phantom balances. This is table stakes for financial systems, not a nice-to-have.

### Why Not Eventual Consistency
Eventual consistency (Cassandra, DynamoDB in default mode) is designed for systems where temporary staleness is tolerable. Netflix uses eventual consistency for viewing history — if your "Continue Watching" list is 3 seconds stale, nobody loses money. Cassandra is an AP system: it prioritizes availability and partition tolerance over consistency.

For payments, the failure modes of eventual consistency are catastrophic:
- **Double-spending**: A merchant's balance reads as $1,000 on two nodes simultaneously. Two $800 payouts are approved. The merchant has effectively stolen $600.
- **Phantom balances**: A refund is processed on one node but not yet replicated. The merchant sees an inflated balance and withdraws funds that should have been reserved.
- **Reconciliation nightmares**: When "truth" is a moving target, reconciliation becomes a probabilistic exercise rather than a deterministic one.

### When the Alternative Wins
Eventual consistency wins when:
- **Read-heavy, write-light workloads** where staleness is harmless (product catalogs, user profiles, viewing history).
- **Global availability is non-negotiable** and the data is not financial. A social media feed being 2 seconds stale is invisible to users; a payment balance being 2 seconds stale can cause real financial harm.
- **Scale demands exceed what a single-leader database can handle** and the data model is append-only or last-write-wins (analytics events, logs, metrics).

The honest trade-off of strong consistency: it limits horizontal scalability. A single PostgreSQL leader handles all writes. You can scale reads with replicas, but write throughput has a ceiling. For most payment systems, this ceiling is far above actual transaction volume. Stripe processes millions of payments daily on PostgreSQL. You are almost certainly not bigger than Stripe.

---

## 2. Synchronous vs Asynchronous Processing

### The Choice
Authorization is synchronous. When a customer taps their card, the merchant needs a yes or no within 2 seconds. Settlement, reconciliation, merchant payouts, webhook delivery, and reporting are all asynchronous, processed via message queues and batch jobs.

### Why This
The merchant and cardholder are standing at a checkout counter. The POS terminal is waiting. The customer is waiting. This interaction has a hard real-time constraint — the card network mandates response within a few seconds. There is no "we'll get back to you" for an authorization decision. The system must check the balance, apply fraud rules, reserve funds, and return a response synchronously.

Everything downstream of authorization does not have this constraint. Settlement (moving money between banks) happens in batch windows — often end-of-day. Reconciliation runs on schedules. Webhooks are delivered with retry logic. Making these async gives us:
- **Resilience**: A slow webhook endpoint does not block the next payment.
- **Throughput**: Batch settlement processes thousands of transactions in bulk, far more efficiently than one-at-a-time.
- **Decoupling**: The authorization path does not depend on the settlement service being healthy.

### Why Not Fully Synchronous
Making everything synchronous creates a brittle chain of dependencies. If the settlement service is slow, authorization latency spikes. If the webhook delivery service is overwhelmed, the entire payment pipeline backs up. Synchronous end-to-end processing means the system is only as fast as its slowest component.

### Why Not Fully Asynchronous
If authorization were async, the merchant would have to say "payment is processing, we'll email you." This is unacceptable for in-person payments and creates terrible UX for online checkout. Some payment methods (bank transfers, crypto) are inherently async, and that is fine for those flows. But card payments are expected to be instant.

### The Boundary Challenge
The hardest problem in sync/async payment design is the timeout boundary. What happens when a synchronous authorization call to the card network times out?

- The payment might have been approved (network processed it, response was lost).
- The payment might have been declined (network never received it).
- The payment might be in an unknown state (network received it, is still processing).

This is the "unknown state" problem, and it is genuinely hard. The correct approach:
1. **Record the attempt** with a unique idempotency key before sending to the network.
2. **On timeout**, return "unknown" to the caller (not "declined" — that would be a lie).
3. **Async reconciliation** queries the network to determine the actual outcome.
4. **Resolve** the unknown state and notify the merchant.

Never guess. Never assume. Unknown means unknown until proven otherwise.

### When the Alternative Wins
Fully async wins for:
- **Bank-to-bank transfers** (ACH, SEPA) where settlement inherently takes days.
- **Cryptocurrency payments** where confirmation requires block validation.
- **Invoice-based B2B payments** where the buyer reviews and approves later.

Fully sync wins for:
- **Simple, low-volume systems** where the engineering cost of async infrastructure (queues, workers, retry logic, dead-letter handling) exceeds the benefit.

---

## 3. Idempotency Keys: Client-Generated vs Server-Generated Dedup

### The Choice
Client-provided idempotency keys, following Stripe's model. The client generates a unique key (typically a UUID) and includes it with every payment request. The server guarantees that replaying the same key returns the original response without re-executing the operation.

### Why This
Client-generated keys put retry semantics under the client's control, which is where they belong. The client knows:
- Whether a request is a retry of a failed call or a genuinely new payment.
- What scope the idempotency should cover (per-order, per-cart, per-user-action).
- When to generate a new key vs reuse an existing one.

The server's contract is simple: same key = same response. The server stores a mapping of `idempotency_key -> response` and returns the cached response on duplicate requests. This is clean, deterministic, and easy to reason about.

Implementation: store the key, request hash, response, and status in a dedicated table. Use a unique constraint on the key. On duplicate key with matching request hash, return the stored response. On duplicate key with different request hash, return a 422 — the client is misusing the key.

### Why Not Server-Generated Dedup
The alternative is server-side duplicate detection: the server examines incoming requests and decides if two requests are "the same" based on heuristics (same amount, same card, same merchant, within a time window).

This approach is fragile because:
- **Defining "duplicate" is ambiguous**. Is buying two $25.00 coffees at the same Starbucks within 30 seconds a duplicate? Maybe. Maybe the customer bought two. The server cannot know intent.
- **Time windows are arbitrary**. 5 seconds? 30 seconds? 5 minutes? Every threshold is wrong for some legitimate use case.
- **False positives block legitimate payments**. A customer buying the same item twice (gift + personal) gets their second payment rejected.
- **False negatives allow true duplicates through**. A retry that arrives 31 seconds later (just outside the window) gets processed twice.

### When the Alternative Wins
Server-generated dedup wins when:
- **The client cannot be trusted or modified** (legacy integrations, third-party systems that do not support idempotency keys).
- **The API is public-facing to unsophisticated consumers** who will never implement proper idempotency key management.
- **As a safety net alongside client keys** — defense in depth. Even with client keys, a server-side "same card, same amount, same merchant, within 5 seconds" check can catch accidental double-charges from buggy clients.

The pragmatic answer: use client-generated idempotency keys as the primary mechanism, and layer server-side heuristic dedup as a secondary safety net that errs on the side of flagging (not blocking) potential duplicates.

---

## 4. Event Sourcing vs Traditional CRUD

### The Choice
Hybrid approach — traditional CRUD (PostgreSQL) for current state, plus an append-only event log for audit trail and state reconstruction. Not pure event sourcing, not pure CRUD.

### Why This
Payments are one of the best natural fits for event sourcing because:
- **Audit trail is mandatory**. Regulators, dispute resolution, and fraud investigation all require knowing exactly what happened, in what order, with what data, at what time. An event log provides this for free — it is the system of record, not a secondary artifact.
- **State reconstruction is valuable**. "What was this payment's status at 3:47 PM on Tuesday?" is a question that event sourcing answers trivially (replay events up to that timestamp) and CRUD answers painfully (hope you logged it somewhere).
- **Payments are naturally event-driven**. A payment's lifecycle is a sequence of events: created, authorized, captured, settled, refunded. This maps directly to an event stream.

But pure event sourcing for the entire payment system introduces significant complexity, so a hybrid approach is more practical: the authoritative current state lives in PostgreSQL (easy to query, easy to reason about, supports ACID transactions), and every state transition is also appended to an immutable event log.

### Why Not Pure Event Sourcing
Pure event sourcing means the event log is the only source of truth, and current state is derived by replaying events. The problems:

- **Schema evolution is painful**. Event v1 has fields A, B, C. Event v2 adds field D. Now your replay logic must handle both versions forever. This compounds over years.
- **Snapshots are required for performance**. Replaying 10 million events to compute a merchant's current balance is not viable. You need periodic snapshots, which adds infrastructure and complexity.
- **Projections are eventually consistent**. The "current state" view (projection) lags behind the event log. For payments, this means a brief window where the projection shows stale data — exactly the problem we chose strong consistency to avoid.
- **Querying is hard**. "Show me all payments over $100 for merchant X in the last 30 days" is a simple SQL query against a CRUD database. Against an event store, it requires a materialized projection that must be maintained.
- **Operational complexity is high**. Engineers must understand event replay, projection rebuilds, snapshot management, and event versioning. This is a steep learning curve for a payments team.

### Why Not Pure CRUD
Pure CRUD loses history. An UPDATE overwrites the previous value. When a payment moves from "authorized" to "captured," the fact that it was ever "authorized" is lost unless you explicitly log it somewhere. In payments, losing history is a compliance violation.

### When Pure Event Sourcing Wins
- **Systems where the event history IS the product** (financial ledgers, trading platforms, version control systems).
- **Systems that need extensive temporal queries** ("what was the state at time T?").
- **Teams with deep event sourcing expertise** who have solved the schema evolution and projection consistency problems.
- **Greenfield projects** where you can design the entire stack around event sourcing from day one, without legacy CRUD assumptions.

### When Pure CRUD Wins
- **Simple payment integrations** (single payment provider, no complex lifecycle management).
- **Early-stage startups** where shipping speed matters more than architectural elegance.
- **Teams without event sourcing experience** — the learning curve is real, and getting it wrong in a payment system has financial consequences.

---

## 5. Monolith vs Microservices for Payments

### The Choice
Start as a monolith. Extract services only when there is a clear, demonstrated need. This is not a concession to simplicity — it is a deliberate architectural decision grounded in the transactional requirements of payment systems.

### Why This
Payments involve multiple tightly coupled operations that must succeed or fail atomically:
1. Validate the idempotency key.
2. Check fraud rules.
3. Reserve funds (debit the ledger).
4. Send authorization to the card network.
5. Record the result.
6. Update the idempotency key with the response.

In a monolith, steps 1-3 and 5-6 happen in a single database transaction. ACID guarantees that either all of them commit or none of them do. This is straightforward, well-understood, and correct by construction.

Stripe started as a Ruby monolith. They ran that monolith for years, processing billions of dollars. They gradually extracted services (fraud detection, payouts, reporting) only when the organizational and scaling pressures justified the distributed systems complexity. This is the playbook.

### Why Not Microservices (Initially)
Microservices for payments means distributed transactions. Steps 1-6 above now span multiple services and multiple databases. You need:

- **Saga pattern**: A choreography or orchestration-based saga that coordinates the multi-step payment flow across services. Each step must have a compensating action (rollback). If step 4 succeeds but step 5 fails, you must reverse step 4 — which means issuing a void to the card network. This is complex, error-prone, and hard to test.
- **Distributed idempotency**: The idempotency key must be checked across service boundaries. Where does it live? In its own service? Now you have a network call before every payment.
- **Eventual consistency between services**: The payment service says "authorized" but the ledger service has not yet recorded the debit. During this window, the system is in an inconsistent state.
- **Operational overhead**: Service discovery, inter-service authentication, distributed tracing, circuit breakers, retry policies, dead-letter queues — all of this for a system that a single PostgreSQL instance could handle.

Premature microservices in payments is not just unnecessary complexity — it is dangerous. Every service boundary is a potential consistency gap, and consistency gaps in payments mean lost money.

### When Microservices Win
- **At scale**: When the monolith's deployment frequency, team ownership boundaries, or performance characteristics genuinely cannot serve the business. This is typically hundreds of engineers and millions of transactions per day.
- **For peripheral services**: Fraud detection, reporting, analytics, merchant dashboards — these can be extracted without touching the core payment transaction path.
- **When regulatory boundaries require isolation**: PCI-scoped services separated from non-PCI services is a valid microservice boundary driven by compliance, not architecture preference.
- **When different services have different scaling profiles**: The authorization service needs low-latency, high-throughput compute. The settlement service needs batch processing capability. These have fundamentally different infrastructure requirements.

The rule: extract a service when the cost of keeping it in the monolith exceeds the cost of distributed systems complexity. Not before.

---

## 6. Build vs Buy for Acquirer Integrations

### The Choice
This depends heavily on business stage. Early stage: buy (use a payment aggregator like Stripe or Adyen that abstracts acquirer complexity). Growth stage: build, incrementally, starting with your highest-volume acquirer.

### Why Building Gives You Control
Each acquirer (Chase Paymentech, First Data, Worldpay, Adyen) has:
- Different API formats (SOAP, REST, ISO 8583).
- Different error codes and their meanings (a "soft decline" from one acquirer is a "retry" from another).
- Different retry semantics (some are safe to retry, some are not).
- Different settlement windows and reconciliation file formats.
- Different fee structures based on transaction routing.

Building your own acquirer integration layer gives you:
- **Intelligent routing**: Route transactions to the acquirer with the highest approval rate for a given card type, geography, and amount. This directly increases revenue.
- **Failover**: If Acquirer A is down, automatically route to Acquirer B. This requires understanding both APIs and normalizing their responses.
- **Cost optimization**: Route to the cheapest acquirer for a given transaction profile. Basis points matter at scale.
- **Custom retry logic**: Retry soft declines on a different acquirer. This can recover 2-5% of otherwise lost revenue.

### Why Buying Gets You to Market Faster
Building acquirer integrations is a multi-month engineering effort per acquirer. Each integration requires:
- PCI DSS compliance for handling card data.
- Certification with the acquirer (test transactions, security review).
- Ongoing maintenance as acquirers change their APIs.
- 24/7 operations to handle acquirer outages and incidents.

A payment aggregator (Stripe, Adyen, Braintree) has already done this work for dozens of acquirers. You get:
- Multi-acquirer routing out of the box.
- PCI compliance handled by the aggregator.
- A single, well-documented API instead of a dozen proprietary ones.

### When Building Wins
- **At scale** (>$100M annual processing volume), where basis-point savings on routing justify engineering investment.
- **When you need routing control** that aggregators do not expose (industry-specific optimization, geographic routing preferences).
- **When you are a payment company** and acquirer management is core to your value proposition.

### When Buying Wins
- **Early stage**: Any startup spending engineering time on acquirer integrations instead of product features is making a mistake.
- **Low transaction volume**: The cost savings from smart routing do not justify the engineering and compliance investment.
- **Non-payment companies**: If payments are a means to an end (e-commerce, SaaS billing), the acquirer layer is not your competitive advantage.

---

## 7. PCI DSS Scope Minimization

### The Choice
Tokenize card data at the client side using a hosted field or JavaScript library (Stripe.js, Adyen's Drop-in). The backend never sees, transmits, or stores raw Primary Account Numbers (PANs). The backend only handles tokens — opaque strings that reference the card data stored in the payment provider's PCI-compliant vault.

### Why This
PCI DSS compliance has four levels based on transaction volume, and the audit requirements are substantial:
- **Level 1** (>6M transactions/year): Annual on-site audit by a Qualified Security Assessor (QSA), quarterly network scans, penetration testing.
- **Self-Assessment Questionnaires (SAQs)** range from SAQ A (13 requirements, for merchants who fully outsource card handling) to SAQ D (over 300 requirements, for merchants who handle card data directly).

Every system, server, network segment, and employee that touches raw card data is "in scope" for PCI audit. Minimizing scope means:
- **Fewer servers to harden and audit**.
- **Fewer employees who need PCI training and background checks**.
- **Lower audit costs** (a SAQ A costs orders of magnitude less than SAQ D).
- **Smaller attack surface** — if your server never has card data, a breach of your server does not expose card data.

Stripe.js is not a UX convenience. It is a compliance architecture decision. When the customer types their card number into a Stripe-hosted iframe, that data goes directly from the customer's browser to Stripe's PCI-compliant servers. Your server receives a token. Your server is never in the data flow for raw card numbers.

### Why Not Handling Card Data Directly
Handling raw PANs means:
- Your database stores card numbers (encrypted, but still in scope).
- Your application servers process card numbers (in-memory, in logs if you are not careful).
- Your network transmits card numbers (requiring TLS everywhere, network segmentation, intrusion detection).
- Your developers have access to systems that handle card data (background checks, access controls, training).
- Your entire infrastructure is in PCI scope.

The cost of full PCI compliance is measured in hundreds of thousands of dollars annually for a mid-size company. The engineering burden of maintaining a PCI-compliant environment is ongoing and significant.

### When Handling Card Data Directly Wins
- **You are a payment processor** (Stripe, Adyen, Square). Handling card data is literally your business.
- **You need card-on-file for complex routing** across multiple acquirers that do not support network tokens or cross-provider tokenization.
- **Regulatory requirements** in some jurisdictions mandate that card data be stored within the country. If no PCI-compliant vault provider operates in that jurisdiction, you may need to handle it yourself.

For everyone else: tokenize at the edge. Your backend should never see a raw card number. This is not optional security hygiene — it is the single highest-leverage compliance decision you will make.

---

## 8. Webhook Reliability vs Polling

### The Choice
Webhooks as the primary notification mechanism, with polling as a fallback, and a full event API for reconciliation. All three, not just one.

### Why Webhooks (Push) as Primary
Webhooks provide near-real-time notification to merchants when payment events occur (payment succeeded, refund processed, dispute opened). The merchant registers a URL, and the payment system POSTs event data to that URL when something happens.

Advantages:
- **Low latency**: The merchant knows about events within seconds, not minutes.
- **Efficient**: No wasted requests. The merchant only receives data when there is data to receive.
- **Simple merchant integration**: Register a URL, handle incoming POSTs. Conceptually straightforward.

### Why Webhooks Alone Are Insufficient
Webhooks are fundamentally unreliable because the payment system does not control the merchant's server:
- **Merchant server is down**: The webhook fails. You retry. The merchant server is still down. You retry again. How many times? For how long? With what backoff?
- **Merchant server is slow**: The webhook times out. Did the merchant process it? Unknown.
- **Network issues**: The webhook was sent, the merchant processed it, but the ACK was lost. You retry. The merchant processes a duplicate.
- **Ordering**: Webhooks may arrive out of order. A "payment.refunded" webhook arriving before "payment.captured" confuses the merchant's state machine.

Retry strategies (exponential backoff, dead-letter queues) mitigate but do not solve these problems. After exhausting retries, the event is lost from the merchant's perspective.

### Why Polling (Pull) as Fallback
Polling lets the merchant ask "what happened since my last check?" This is reliable because:
- **The merchant controls the timing**: No dependency on the merchant's server being available at the moment the event occurs.
- **Idempotent by nature**: Polling the same endpoint twice returns the same data. No duplicate processing risk.
- **Handles ordering**: Events are returned in order, with pagination.

The downside: polling is wasteful (most requests return no new data) and introduces latency (events are not discovered until the next poll interval).

### Why the Event API for Reconciliation
The event API (Stripe's `/v1/events` endpoint) is the system of record. It provides:
- **Complete history**: Every event that occurred, regardless of webhook delivery status.
- **Filtering**: By event type, date range, resource ID.
- **Pagination**: For processing large volumes of historical events.

Merchants use this for end-of-day reconciliation: "Does my local state match the payment system's state?" Any discrepancies indicate missed webhooks or processing bugs.

### When Polling Alone Wins
- **Low-volume merchants** who process a handful of payments per day. A cron job polling every 5 minutes is simpler than standing up a webhook endpoint.
- **Backend-only systems** (batch processors, ETL pipelines) that do not need real-time notification.

### When Webhooks Alone Win
- Essentially never, for payments. Webhooks alone are acceptable for non-critical notifications (marketing events, analytics) where missing an event has no financial consequence.

---

## 9. SQL vs NoSQL for Payment Data

### The Choice
PostgreSQL (SQL) for all transactional payment data — payments, refunds, ledger entries, merchant accounts, idempotency keys. NoSQL and specialized stores for non-transactional concerns.

### Why SQL (PostgreSQL)
Payment data is inherently relational and transactional:
- A payment belongs to a merchant, is associated with a customer, has a card, produces ledger entries, may have refunds, may have disputes. These are relationships, and relational databases model relationships.
- A payment authorization must atomically: check idempotency, debit the ledger, record the payment, and update the merchant's balance. This is a multi-table transaction. ACID guarantees make this correct by construction.
- Financial auditors and regulators expect SQL-queryable data. "Show me all refunds over $10,000 in Q3" is a SQL query, not a MapReduce job.

PostgreSQL specifically offers:
- **Mature ACID implementation** with multiple isolation levels.
- **Row-level locking** for concurrent balance updates without full table locks.
- **JSONB columns** for semi-structured data (payment metadata, acquirer-specific fields) when you need flexibility without abandoning relational integrity.
- **Proven at scale** in financial systems (Stripe, Square, and many banks use PostgreSQL).

### Why Not NoSQL for Core Payment Data
NoSQL databases are designed around trade-offs that are wrong for payments:

- **Cassandra**: Tunable consistency, but the default (and performant) mode is eventual consistency. Strong consistency in Cassandra (QUORUM reads/writes) sacrifices availability — negating Cassandra's primary advantage. You get worse-than-PostgreSQL consistency with more operational complexity.
- **MongoDB**: Document model is convenient but transactions across collections were bolted on late (v4.0+) and have performance implications. WiredTiger's document-level locking is not the same as PostgreSQL's mature MVCC.
- **DynamoDB**: Single-table design is an unnatural fit for relational payment data. Transactions are limited to 25 items. The pricing model (read/write capacity units) makes complex queries expensive.

The core issue: NoSQL databases optimize for scale and availability at the expense of consistency. Payments optimize for consistency at the expense of scale. These are opposing forces.

### Where NoSQL Fits in a Payment System
NoSQL is not wrong — it is wrong for the core transactional path. It excels elsewhere:

| Use Case | Technology | Why |
|---|---|---|
| Event streaming | Kafka | Append-only, high-throughput, durable event log for audit trail and async processing |
| Caching | Redis | Hot data (rate limits, session data, idempotency key lookups) with sub-millisecond reads |
| Document storage | S3 | Settlement files, reconciliation reports, PCI audit logs — write-once, read-occasionally |
| Search / Analytics | Elasticsearch | Full-text search over payment descriptions, merchant names, transaction metadata |
| Time-series metrics | InfluxDB / TimescaleDB | Payment volume over time, latency percentiles, error rates |

### When NoSQL Wins for Core Data
- **Append-only event stores** where you never update, only insert. An event-sourced payment ledger could use Kafka or a purpose-built event store.
- **Extremely high write volume** (millions of micro-transactions per second) where PostgreSQL's single-leader architecture genuinely cannot keep up. This is rare outside of ad-tech and gaming.
- **Schema-free exploration** in early prototyping before the data model is stable. But migrate to SQL before handling real money.

---

## 10. Active-Passive vs Active-Active Multi-Region

### The Choice
Active-passive with fast failover. One region handles all payment traffic (active). A second region maintains a hot standby with synchronous or near-synchronous replication (passive). On failure of the active region, traffic fails over to the passive region.

### Why Active-Passive
Financial consistency makes active-active extremely difficult for payments. The fundamental problem: if two regions can independently process payments, they must independently maintain consistent balances. This requires either:

1. **Synchronous cross-region replication** (every write waits for confirmation from both regions): This adds cross-region latency (50-200ms) to every transaction. Authorization latency budgets are tight (sub-second). Adding 200ms of replication latency per transaction is often unacceptable.

2. **Conflict resolution for concurrent writes**: If Region A and Region B both process a $500 debit against a $600 balance simultaneously, both approve (each sees $600). The actual balance is now -$400. Last-write-wins, vector clocks, and CRDTs do not work for financial balances — you cannot "merge" two debits.

Active-passive avoids this entirely. One region is authoritative. There is no conflict because there is no concurrent authority.

Fast failover (under 30 seconds) is achievable with:
- **Synchronous replication** to the standby (zero data loss on failover).
- **Health checks** with aggressive timeouts.
- **DNS-based or load-balancer-based traffic shifting**.
- **Automated runbooks** that promote the standby and redirect traffic.

### Why Not Active-Active
The problems above, plus:
- **Split-brain scenarios**: Network partition between regions. Both regions think the other is down. Both promote themselves to active. Two authoritative sources of truth for financial balances. This is a catastrophic failure mode.
- **Distributed locking**: Preventing double-processing requires cross-region distributed locks (ZooKeeper, etcd). These locks add latency and introduce their own availability concerns.
- **Complexity**: Active-active for payments requires solving distributed consensus for every transaction. This is Paxos/Raft-level complexity in the hot path.

Netflix runs active-active across three AWS regions. This works because streaming data is eventually consistent — if a user's viewing history is 5 seconds stale in one region, the user does not notice. Payments do not have this luxury.

### When Active-Active Wins
- **Non-financial workloads** in the payment ecosystem: merchant dashboards, reporting, analytics. These can be active-active with eventual consistency because stale reads are tolerable.
- **Read-heavy, write-light financial queries**: Balance inquiries (not balance modifications) can be served from read replicas in multiple regions.
- **When regulatory requirements mandate in-region processing**: If EU payments must be processed in EU and US payments must be processed in US, you have two independent payment systems, each active-passive within its region. This is regional isolation, not true active-active.
- **When the business absolutely requires zero-downtime** and is willing to accept the engineering cost. Some payment processors (Visa, Mastercard) operate active-active, but they have dedicated teams of hundreds of engineers maintaining the distributed consistency layer. This is not a startup architecture.

### The Pragmatic Middle Ground
Most production payment systems use a nuanced approach:
- **Core transaction processing**: Active-passive.
- **API gateway and routing**: Active-active (stateless, no consistency concerns).
- **Read replicas for dashboards**: Multi-region (eventual consistency acceptable).
- **Event streaming**: Multi-region Kafka (append-only, no conflicts).

This gives you multi-region resilience where it is safe and single-region authority where consistency demands it.

---

## Summary: The Meta-Trade-Off

Every trade-off in this document follows a pattern: **payments optimize for correctness over performance, consistency over availability, simplicity over scalability**. This is the opposite of most distributed systems advice, which assumes you are building a social network or a content delivery platform.

The reason is simple: bugs in a social network show the wrong photo. Bugs in a payment system lose real money. The cost of a consistency violation in payments is measured in dollars, lawsuits, and regulatory fines — not user complaints.

When in doubt, choose the boring technology, the simpler architecture, and the stronger consistency guarantee. You can always add complexity later when you have evidence that you need it. You cannot un-lose money.
