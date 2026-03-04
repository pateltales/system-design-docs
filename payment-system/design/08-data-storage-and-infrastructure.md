# Payment System — Data Storage & Infrastructure Deep Dive

> Companion deep dive to the [interview simulation](01-interview-simulation.md). This document explores how a payment platform (Stripe, Razorpay, Adyen) stores transactional data, card tokens, events, and analytical datasets -- and why the storage choices differ fundamentally from content-delivery platforms like Netflix.

---

## Table of Contents

1.  [Storage Landscape Overview](#1-storage-landscape-overview)
2.  [Primary Database — Transactional Data (PostgreSQL)](#2-primary-database--transactional-data-postgresql)
3.  [Event Store — Append-Only Event Log (Kafka)](#3-event-store--append-only-event-log-kafka)
4.  [Idempotency Store — Redis Cluster](#4-idempotency-store--redis-cluster)
5.  [Token Vault — PCI DSS Cardholder Data Environment](#5-token-vault--pci-dss-cardholder-data-environment)
6.  [Document Store — Unstructured Data](#6-document-store--unstructured-data)
7.  [Analytics / Data Warehouse — CDC Pipeline](#7-analytics--data-warehouse--cdc-pipeline)
8.  [Caching Layer — Redis for Hot Data](#8-caching-layer--redis-for-hot-data)
9.  [Multi-Region Considerations](#9-multi-region-considerations)
10. [Contrast with Netflix's Storage Architecture](#10-contrast-with-netflixs-storage-architecture)
11. [Full Architecture Diagram — Data Flow Between Components](#11-full-architecture-diagram--data-flow-between-components)

---

## 1. Storage Landscape Overview

A payment system is not a single database. It is a **constellation of purpose-built stores**, each optimized for a specific access pattern, compliance requirement, and consistency guarantee.

```
┌─────────────────────────────────────────────────────────────────────────────────┐
│                        PAYMENT SYSTEM STORAGE LANDSCAPE                        │
├─────────────────────┬──────────────┬──────────────┬────────────┬───────────────┤
│ Store               │ Technology   │ Purpose      │ Consistency│ Compliance    │
├─────────────────────┼──────────────┼──────────────┼────────────┼───────────────┤
│ Primary DB          │ PostgreSQL   │ Payments,    │ Strong     │ SOC 2,        │
│                     │ (sharded)    │ ledger,      │ (ACID)     │ PCI DSS       │
│                     │              │ customers    │            │               │
├─────────────────────┼──────────────┼──────────────┼────────────┼───────────────┤
│ Event Store         │ Kafka        │ State change │ Ordered    │ Audit trail   │
│                     │              │ log, async   │ per-       │               │
│                     │              │ processing   │ partition  │               │
├─────────────────────┼──────────────┼──────────────┼────────────┼───────────────┤
│ Idempotency Store   │ Redis        │ Dedup keys   │ Eventual   │ —             │
│                     │ Cluster      │ with TTL     │ (with AOF) │               │
├─────────────────────┼──────────────┼──────────────┼────────────┼───────────────┤
│ Token Vault         │ Isolated     │ Raw card     │ Strong     │ PCI DSS L1    │
│                     │ PostgreSQL/  │ data (PAN,   │ (ACID)     │ (CDE)         │
│                     │ HSM-backed   │ expiry)      │            │               │
├─────────────────────┼──────────────┼──────────────┼────────────┼───────────────┤
│ Document Store      │ S3 / MongoDB │ Webhooks,    │ Eventual   │ PCI DSS       │
│                     │              │ disputes,    │            │ (if dispute   │
│                     │              │ onboarding   │            │ evidence)     │
├─────────────────────┼──────────────┼──────────────┼────────────┼───────────────┤
│ Data Warehouse      │ Snowflake /  │ Analytics,   │ Near-RT    │ SOC 2         │
│                     │ BigQuery /   │ reporting,   │ (minutes   │               │
│                     │ Redshift     │ reconcile    │ lag)       │               │
├─────────────────────┼──────────────┼──────────────┼────────────┼───────────────┤
│ Cache               │ Redis        │ Merchant     │ Eventual   │ —             │
│                     │ Cluster      │ config, BIN  │ (TTL +     │               │
│                     │              │ tables, FX   │ invalidate)│               │
└─────────────────────┴──────────────┴──────────────┴────────────┴───────────────┘
```

### Why So Many Stores?

A naive approach would be: "put everything in PostgreSQL." That fails because:

1. **PCI DSS scope** -- raw card data in the same database as analytics data means your entire analytics pipeline is in-scope for PCI audit. Isolating card data into a separate vault minimizes audit scope.
2. **Access pattern mismatch** -- idempotency lookups need sub-millisecond latency (Redis), but ledger entries need ACID durability (PostgreSQL). Forcing both into one store means you compromise on one.
3. **Retention and volume** -- event logs grow unboundedly. Storing them in the transactional DB degrades performance. Kafka handles append-only, high-throughput writes natively.
4. **Regulatory** -- GDPR requires EU data to stay in EU. Having separate stores per region is easier than cross-region sharding of a single monolithic DB.

---

## 2. Primary Database -- Transactional Data (PostgreSQL)

### 2.1 Why PostgreSQL?

Stripe uses PostgreSQL as its primary transactional database. [VERIFIED -- Stripe's engineering blog has discussed their use of PostgreSQL extensively, including their "online migrations" post describing large-scale PostgreSQL schema changes.] Stripe has invested heavily in PostgreSQL tooling, including online schema migration frameworks.

PostgreSQL is the de facto choice for payment systems because:

- **ACID transactions** -- a payment capture must atomically: (1) update payment status from `AUTHORIZED` to `CAPTURED`, (2) create ledger debit + credit entries, (3) update the merchant's pending balance. If any of these fail, ALL must roll back. This is a textbook multi-table transaction.
- **Strong consistency** -- after a capture succeeds, any subsequent read MUST see the updated status. Eventual consistency for payment state is unacceptable (a merchant could see "authorized" and attempt a second capture).
- **Mature ecosystem** -- battle-tested replication (streaming replication, logical replication), robust MVCC, rich indexing (B-tree, GIN, partial indexes), JSON support for metadata.
- **Row-level locking** -- concurrent captures on different payments don't block each other. Only concurrent operations on the SAME payment contend for the same row lock.

### 2.2 Core Schema

```sql
-- Payments table (the heart of the system)
CREATE TABLE payments (
    payment_id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    merchant_id         UUID NOT NULL,           -- FK to merchants (partition key for sharding)
    customer_id         UUID,                    -- FK to customers (nullable for guest checkout)
    idempotency_key     VARCHAR(255),            -- client-provided dedup key
    amount              BIGINT NOT NULL,         -- in smallest currency unit (cents, paise)
    currency            CHAR(3) NOT NULL,        -- ISO 4217 (USD, EUR, INR)
    status              VARCHAR(32) NOT NULL,    -- CREATED, AUTHORIZED, CAPTURED, etc.
    payment_method_token UUID,                   -- reference to token vault (NOT raw card data)
    acquirer_id         UUID,                    -- which acquirer processed this
    acquirer_reference  VARCHAR(255),            -- acquirer's transaction ID
    auth_code           VARCHAR(32),             -- authorization code from issuer
    failure_code        VARCHAR(64),             -- e.g., "insufficient_funds", "card_declined"
    failure_message     TEXT,
    metadata            JSONB,                   -- merchant-provided key-value pairs
    capture_amount      BIGINT,                  -- for partial captures
    captured_at         TIMESTAMPTZ,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    -- Unique constraint on idempotency key per merchant
    CONSTRAINT uq_merchant_idempotency UNIQUE (merchant_id, idempotency_key)
);

-- Ledger entries (double-entry bookkeeping)
CREATE TABLE ledger_entries (
    entry_id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    payment_id          UUID NOT NULL,           -- FK to payments
    merchant_id         UUID NOT NULL,           -- denormalized for sharding co-location
    account_id          UUID NOT NULL,           -- FK to chart of accounts
    entry_type          VARCHAR(16) NOT NULL,    -- 'DEBIT' or 'CREDIT'
    amount              BIGINT NOT NULL,         -- always positive
    currency            CHAR(3) NOT NULL,
    description         TEXT,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    -- Ensures every debit has a matching credit (enforced at application level)
    CONSTRAINT chk_entry_type CHECK (entry_type IN ('DEBIT', 'CREDIT'))
);

-- Refunds
CREATE TABLE refunds (
    refund_id           UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    payment_id          UUID NOT NULL,
    merchant_id         UUID NOT NULL,
    amount              BIGINT NOT NULL,         -- partial or full refund amount
    currency            CHAR(3) NOT NULL,
    status              VARCHAR(32) NOT NULL,    -- PENDING, PROCESSING, SUCCEEDED, FAILED
    reason              VARCHAR(255),
    idempotency_key     VARCHAR(255),
    acquirer_reference  VARCHAR(255),
    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    CONSTRAINT uq_refund_idempotency UNIQUE (merchant_id, idempotency_key)
);

-- Merchants
CREATE TABLE merchants (
    merchant_id         UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    name                VARCHAR(255) NOT NULL,
    email               VARCHAR(255) NOT NULL,
    settlement_schedule VARCHAR(16) NOT NULL DEFAULT 'T+2',  -- T+1, T+2, T+7
    risk_tier           VARCHAR(16) NOT NULL DEFAULT 'STANDARD', -- LOW, STANDARD, HIGH
    status              VARCHAR(32) NOT NULL DEFAULT 'ACTIVE',
    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Merchant balance accounts (for settlement / payout tracking)
CREATE TABLE merchant_balances (
    merchant_id         UUID PRIMARY KEY,
    available_balance   BIGINT NOT NULL DEFAULT 0,  -- can be withdrawn
    pending_balance     BIGINT NOT NULL DEFAULT 0,  -- captured but not yet settled
    reserved_balance    BIGINT NOT NULL DEFAULT 0,  -- held for disputes/chargebacks
    currency            CHAR(3) NOT NULL,
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Indexes for common query patterns
CREATE INDEX idx_payments_merchant_status ON payments (merchant_id, status);
CREATE INDEX idx_payments_merchant_created ON payments (merchant_id, created_at DESC);
CREATE INDEX idx_ledger_payment ON ledger_entries (payment_id);
CREATE INDEX idx_ledger_merchant_account ON ledger_entries (merchant_id, account_id, created_at);
CREATE INDEX idx_refunds_payment ON refunds (payment_id);
```

### 2.3 The Atomic Capture Transaction

This is the critical transaction that demonstrates why ACID matters:

```sql
BEGIN;

-- 1. Lock and update payment status
UPDATE payments
SET    status = 'CAPTURED',
       capture_amount = 10000,       -- $100.00 in cents
       captured_at = NOW(),
       updated_at = NOW()
WHERE  payment_id = 'pay_abc123'
  AND  status = 'AUTHORIZED'         -- state machine guard: only transition from AUTHORIZED
  AND  merchant_id = 'merch_xyz';    -- shard-local operation

-- If no rows updated, the payment was not in AUTHORIZED state → abort
-- (checked at application level: if rowcount == 0, ROLLBACK)

-- 2. Create ledger entries (double-entry: debit + credit)
INSERT INTO ledger_entries (payment_id, merchant_id, account_id, entry_type, amount, currency, description)
VALUES
  ('pay_abc123', 'merch_xyz', 'acct_merchant_pending', 'CREDIT', 9710, 'USD', 'Payment capture - merchant share'),
  ('pay_abc123', 'merch_xyz', 'acct_psp_fee',         'CREDIT',  290, 'USD', 'Payment capture - PSP fee (2.9%)'),
  ('pay_abc123', 'merch_xyz', 'acct_customer_card',    'DEBIT', 10000, 'USD', 'Payment capture - customer charge');

-- 3. Update merchant pending balance
UPDATE merchant_balances
SET    pending_balance = pending_balance + 9710,
       updated_at = NOW()
WHERE  merchant_id = 'merch_xyz';

COMMIT;
```

If ANY step fails (constraint violation, deadlock, disk error), the entire transaction rolls back. The payment stays `AUTHORIZED`, no ledger entries are created, and the balance is unchanged. The client can safely retry.

**Why this cannot be done in Cassandra or DynamoDB**: Neither supports multi-row, multi-table ACID transactions. Cassandra's lightweight transactions (LWT) work on a single partition. DynamoDB transactions work across items but with significant throughput and latency overhead. [UNVERIFIED -- DynamoDB transactions do exist but performance characteristics at payment-system scale need benchmarking.]

### 2.4 Sharding Strategy: Shard by Merchant ID

```
┌─────────────────────────────────────────────────────────────────┐
│                     SHARDING BY MERCHANT ID                     │
│                                                                 │
│  Shard 0 (merchants A-F)    Shard 1 (merchants G-L)           │
│  ┌──────────────────────┐   ┌──────────────────────┐          │
│  │ payments (merchant A)│   │ payments (merchant G)│          │
│  │ payments (merchant C)│   │ payments (merchant K)│          │
│  │ ledger   (merchant A)│   │ ledger   (merchant G)│          │
│  │ ledger   (merchant C)│   │ ledger   (merchant K)│          │
│  │ refunds  (merchant A)│   │ refunds  (merchant G)│          │
│  │ balances (merchant A)│   │ balances (merchant G)│          │
│  └──────────────────────┘   └──────────────────────┘          │
│                                                                 │
│  Shard 2 (merchants M-R)    Shard 3 (merchants S-Z)           │
│  ┌──────────────────────┐   ┌──────────────────────┐          │
│  │ payments (merchant M)│   │ payments (merchant S)│          │
│  │ payments (merchant P)│   │ payments (merchant U)│          │
│  │ ledger   (merchant M)│   │ ledger   (merchant S)│          │
│  │ ledger   (merchant P)│   │ ledger   (merchant U)│          │
│  │ refunds  (merchant M)│   │ refunds  (merchant S)│          │
│  │ balances (merchant M)│   │ balances (merchant U)│          │
│  └──────────────────────┘   └──────────────────────┘          │
└─────────────────────────────────────────────────────────────────┘
```

**Why merchant ID as the partition key?**

The critical insight is that the atomic capture transaction (Section 2.3) touches payments, ledger_entries, and merchant_balances -- ALL for the same merchant. By co-locating all of a merchant's data on the same shard, this transaction remains **shard-local**. No distributed transactions needed.

**Partition key trade-offs:**

| Partition Key     | Pros                                         | Cons                                           |
|-------------------|----------------------------------------------|------------------------------------------------|
| **Merchant ID**   | Shard-local ACID for captures/refunds.       | Hot merchant problem (Amazon, Uber on          |
|                   | Merchant queries are shard-local.            | one shard). Cross-merchant queries need        |
|                   | Natural tenant isolation.                    | scatter-gather.                                |
| Payment ID        | Even distribution (UUIDs). No hot spots.     | Capture transaction spans shards (payment      |
|                   |                                              | on shard A, ledger on shard B). Requires       |
|                   |                                              | distributed transactions or Saga.              |
| Customer ID       | Customer history queries are shard-local.    | Capture transaction spans shards. Customer     |
|                   |                                              | may pay multiple merchants (data scattered).   |
| Date/Time         | Range queries (today's payments) are fast.   | Time-based hot spots (all current traffic      |
|                   |                                              | hits the latest shard). Poor for ACID.         |

**Merchant ID wins** because transactional integrity is the paramount concern. The hot merchant problem is real but solvable:

- **Dedicated shards** for the top N merchants (e.g., top 100 merchants each get their own shard).
- **Sub-sharding** within a merchant by hash(payment_id) for extremely large merchants. This sacrifices shard-local merchant balance updates but can be handled with a lightweight coordination layer.

### 2.5 Replication and Failover

```
┌──────────────────────────────────────────────────────────┐
│                   REPLICATION TOPOLOGY                    │
│                                                          │
│  Primary (us-east-1a)                                    │
│  ┌──────────────────┐                                    │
│  │  PostgreSQL       │──── Synchronous ────┐             │
│  │  (reads + writes) │     Replication      │             │
│  └──────────────────┘                      ▼             │
│                                   ┌──────────────────┐   │
│                                   │  Sync Standby     │   │
│                                   │  (us-east-1b)     │   │
│                                   │  (failover target)│   │
│                                   └──────────────────┘   │
│                                            │             │
│  Primary ─── Async Replication ──┐         │ Async       │
│                                  ▼         ▼             │
│                         ┌──────────────────┐             │
│                         │  Async Replica    │             │
│                         │  (us-west-2)      │             │
│                         │  (read-only:      │             │
│                         │   analytics,      │             │
│                         │   reporting)      │             │
│                         └──────────────────┘             │
└──────────────────────────────────────────────────────────┘
```

- **Synchronous standby**: Ensures zero data loss (RPO = 0) for financial data. Every committed write is confirmed on the standby before the client receives `COMMIT`. Trade-off: ~1-3ms additional write latency (within-AZ network round-trip).
- **Async replica**: For read-heavy workloads (merchant dashboards, reporting). May lag seconds behind primary. Never used for payment state reads.
- **Automated failover**: Tools like Patroni or AWS RDS Multi-AZ handle automatic promotion of the sync standby. Target RTO < 30 seconds for automated failover.

---

## 3. Event Store -- Append-Only Event Log (Kafka)

### 3.1 Why an Event Store?

Every state change in a payment system must be recorded immutably. The event store serves multiple consumers:

```
                    ┌──────────────┐
                    │  Payment     │
                    │  Service     │
                    └──────┬───────┘
                           │ publish event
                           ▼
                    ┌──────────────┐
                    │              │
                    │    KAFKA     │
                    │  (durable,   │
                    │   ordered,   │
                    │  partitioned)│
                    │              │
                    └──┬───┬───┬──┘
                       │   │   │
              ┌────────┘   │   └────────┐
              ▼            ▼            ▼
     ┌──────────────┐ ┌──────────┐ ┌──────────────┐
     │  Webhook      │ │ Ledger   │ │ Analytics    │
     │  Delivery     │ │ Projec-  │ │ Pipeline     │
     │  Service      │ │ tion     │ │ (CDC →       │
     │               │ │          │ │  Warehouse)  │
     └──────────────┘ └──────────┘ └──────────────┘
              │                           │
              ▼                           ▼
     ┌──────────────┐            ┌──────────────┐
     │ Reconcilia-  │            │ ML Feature   │
     │ tion Engine  │            │ Engineering  │
     └──────────────┘            └──────────────┘
```

**What the event store powers:**

1. **Audit trail** -- every state transition is recorded with timestamp, actor, and before/after state. Required for PCI DSS compliance, dispute resolution, and regulatory audits.
2. **Event sourcing** -- the complete history of a payment can be reconstructed by replaying its events. This is invaluable for debugging ("why did this payment fail at 2:03 AM?").
3. **Async processing** -- webhooks, analytics, reconciliation, and ML feature engineering all consume events asynchronously. This decouples the critical payment path from downstream systems.
4. **Replay capability** -- if the analytics pipeline has a bug, you can replay events from Kafka to reprocess. If a new consumer is added (e.g., a new compliance report), it can consume the full history.

### 3.2 Why Kafka?

Kafka is the industry standard for event streaming in payment systems. [INFERRED -- while Stripe has not publicly confirmed Kafka specifically, multiple payment companies including Square, PayPal, and Grab have publicly discussed using Kafka for payment event processing. Grab's engineering blog describes using Kafka for payment processing in detail.]

Key properties that make Kafka suitable:

- **Durability** -- messages are persisted to disk and replicated across brokers. `acks=all` ensures a message is written to all in-sync replicas before acknowledgment. No financial event is lost.
- **Ordering** -- messages within a partition are strictly ordered. By partitioning on payment ID, all events for a single payment are processed in order.
- **High throughput** -- Kafka handles millions of messages per second with batching and zero-copy I/O. Far higher throughput than a traditional message queue (RabbitMQ, SQS).
- **Consumer groups** -- multiple independent consumer groups can read from the same topic at different paces. The webhook service and the analytics pipeline each maintain their own offsets.
- **Retention** -- configurable retention by time or size. Payment systems typically retain events for 7+ years (regulatory requirement in many jurisdictions).

### 3.3 Kafka Topic Structure

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                          KAFKA TOPIC ARCHITECTURE                          │
│                                                                            │
│  Topic: payment.events                                                     │
│  ┌─────────────────────────────────────────────────────────────┐           │
│  │ Partition 0:  [pay_001:CREATED] [pay_001:AUTHORIZED] [pay_005:CREATED] │
│  │ Partition 1:  [pay_002:CREATED] [pay_002:CAPTURED]  [pay_002:SETTLED]  │
│  │ Partition 2:  [pay_003:CREATED] [pay_003:AUTH_FAILED]                  │
│  │ Partition 3:  [pay_004:CREATED] [pay_004:AUTHORIZED] [pay_004:VOIDED] │
│  │ ...                                                                     │
│  │ Partition N:  (N = number of partitions, typically 64-256)              │
│  └─────────────────────────────────────────────────────────────┘           │
│  Partition Key: hash(payment_id)                                           │
│  Retention: 90 days hot (broker) + archive to S3 (7 years)                │
│                                                                            │
│  Topic: refund.events                                                      │
│  ┌─────────────────────────────────────────────────────────────┐           │
│  │ Partition Key: hash(payment_id)  (co-partitioned with above)│           │
│  │ Events: REFUND_CREATED, REFUND_PROCESSING, REFUND_SUCCEEDED │           │
│  └─────────────────────────────────────────────────────────────┘           │
│                                                                            │
│  Topic: webhook.delivery                                                   │
│  ┌─────────────────────────────────────────────────────────────┐           │
│  │ Partition Key: hash(merchant_id)                             │           │
│  │ Events: webhook payloads to deliver to merchant endpoints    │           │
│  │ Consumer: Webhook Delivery Service (with retries + DLQ)      │           │
│  └─────────────────────────────────────────────────────────────┘           │
│                                                                            │
│  Topic: settlement.events                                                  │
│  ┌─────────────────────────────────────────────────────────────┐           │
│  │ Partition Key: hash(merchant_id)                             │           │
│  │ Events: SETTLEMENT_INITIATED, SETTLEMENT_COMPLETED           │           │
│  └─────────────────────────────────────────────────────────────┘           │
│                                                                            │
│  Topic: dispute.events                                                     │
│  ┌─────────────────────────────────────────────────────────────┐           │
│  │ Partition Key: hash(payment_id)                              │           │
│  │ Events: DISPUTE_OPENED, EVIDENCE_SUBMITTED, DISPUTE_WON/LOST│           │
│  └─────────────────────────────────────────────────────────────┘           │
│                                                                            │
│  Dead Letter Topics (DLQ):                                                 │
│  ┌─────────────────────────────────────────────────────────────┐           │
│  │ payment.events.dlq        — events that failed processing    │           │
│  │ webhook.delivery.dlq      — webhooks that exhausted retries  │           │
│  │ settlement.events.dlq     — settlement failures              │           │
│  └─────────────────────────────────────────────────────────────┘           │
└─────────────────────────────────────────────────────────────────────────────┘
```

### 3.4 Event Schema

```json
{
  "event_id": "evt_8f3a2b1c",
  "event_type": "payment.captured",
  "payment_id": "pay_abc123",
  "merchant_id": "merch_xyz",
  "timestamp": "2025-01-15T14:30:22.456Z",
  "version": 3,
  "data": {
    "previous_status": "AUTHORIZED",
    "new_status": "CAPTURED",
    "amount": 10000,
    "currency": "USD",
    "capture_amount": 10000,
    "acquirer_id": "acq_stripe_us",
    "acquirer_reference": "ch_1234567890"
  },
  "metadata": {
    "source_service": "payment-service",
    "trace_id": "trace_abc123def456",
    "idempotency_key": "order_789_capture"
  }
}
```

### 3.5 Retention Policy

```
┌─────────────────────────────────────────────────────────────┐
│                    RETENTION STRATEGY                        │
│                                                             │
│  0 ─────── 90 days ─────── 1 year ─────── 7 years ───►     │
│  │                                                          │
│  │  HOT (Kafka brokers)   WARM (S3/Glacier)  COLD (Archive)│
│  │  ├── Fast replay       ├── CDC to          ├── Legal    │
│  │  ├── Real-time         │   warehouse       │   hold     │
│  │  │   consumers         ├── On-demand       ├── Audit    │
│  │  ├── Partition-level   │   replay          │   requests │
│  │  │   ordering          └── Compressed      └── Encrypted│
│  │  └── Full throughput       (Parquet/Avro)      at rest  │
│  │                                                          │
└─────────────────────────────────────────────────────────────┘

Kafka Broker Config:
  log.retention.hours=2160          # 90 days on brokers
  log.retention.bytes=-1            # no size limit (time-based only)
  log.segment.bytes=1073741824      # 1 GB segments
  min.insync.replicas=2             # at least 2 replicas must ack
  replication.factor=3              # 3 copies across brokers
  acks=all                          # producer waits for all ISR acks
```

**Why 90 days hot?** Most payment disputes must be filed within 60-120 days (varies by card network). Having events hot on Kafka for 90 days means dispute investigation can access events without reaching into cold storage.

**Why 7 years archived?** Regulatory requirements vary by jurisdiction: PCI DSS requires at least 1 year of audit logs readily accessible plus archives for the organization's defined retention period. Many financial regulations (SOX, banking regulations) require 5-7 years. [UNVERIFIED -- specific retention requirements vary by jurisdiction; 7 years is a common conservative choice but check local regulations.]

### 3.6 Exactly-Once Semantics in Kafka

True exactly-once delivery across distributed systems is impossible (FLP impossibility). Kafka provides "effectively once" through:

1. **Idempotent producer** (`enable.idempotence=true`): Kafka assigns a producer ID and sequence number to each message. Duplicate messages from producer retries are deduplicated by the broker.
2. **Transactional producer** (`transactional.id`): Atomic writes across multiple partitions. Either all messages in a transaction are visible to consumers, or none are.
3. **Consumer-side idempotency**: Each consumer must be idempotent -- processing the same event twice produces the same result. The webhook delivery service uses the `event_id` as a dedup key.

---

## 4. Idempotency Store -- Redis Cluster

### 4.1 Why Redis for Idempotency?

The idempotency check is on the **critical path** of every payment request. It must be:
- **Fast**: Sub-millisecond lookup. Adding 50ms for an idempotency check on a 500ms payment flow is a 10% latency increase.
- **Durable enough**: Must survive Redis restarts (AOF persistence + replication).
- **Self-cleaning**: Idempotency keys expire after 24-48 hours. No manual cleanup.

```
┌──────────────────────────────────────────────────────────────────┐
│                    IDEMPOTENCY CHECK FLOW                        │
│                                                                  │
│  Client Request                                                  │
│  (with Idempotency-Key: "order_123_pay")                        │
│       │                                                          │
│       ▼                                                          │
│  ┌──────────┐     GET idempotency:{merchant}:{key}              │
│  │   API    │────────────────────────────────────►┌──────────┐  │
│  │  Gateway │                                     │  Redis   │  │
│  │          │◄────────────────────────────────────│  Cluster │  │
│  └──────────┘     HIT: return cached response     └──────────┘  │
│       │                                                          │
│       │ MISS: proceed with payment                               │
│       ▼                                                          │
│  ┌──────────┐                                                    │
│  │ Payment  │                                                    │
│  │ Service  │── process payment ──► acquirer                     │
│  │          │                                                    │
│  └──────────┘                                                    │
│       │                                                          │
│       │ SET idempotency:{merchant}:{key}                        │
│       │     value: {status, response_body}                       │
│       │     EX 172800  (48 hours TTL)                            │
│       ▼                                                          │
│  ┌──────────┐                                                    │
│  │  Redis   │                                                    │
│  │  Cluster │                                                    │
│  └──────────┘                                                    │
└──────────────────────────────────────────────────────────────────┘
```

### 4.2 Redis Data Structure

```
Key:   idempotency:{merchant_id}:{idempotency_key}
Value: {
  "payment_id": "pay_abc123",
  "status": "CAPTURED",
  "response_code": 200,
  "response_body": "{...serialized API response...}",
  "created_at": "2025-01-15T14:30:22Z"
}
TTL:   172800 seconds (48 hours)
```

**Why 48-hour TTL?** The idempotency window must be long enough to cover:
- Client retry storms (typically resolve within minutes)
- Network partitions (could last hours in extreme cases)
- Batch processing retries (some merchants batch payments daily)

After 48 hours, the idempotency key expires. If a client retries with the same key after expiry, a new payment will be created. This is acceptable because: (1) legitimate retries happen within seconds/minutes, not days; (2) after 48 hours, the original payment has settled or failed, so a "duplicate" is actually a new intentional payment.

### 4.3 Redis Cluster Configuration

```
┌─────────────────────────────────────────────────────────────┐
│                   REDIS CLUSTER TOPOLOGY                     │
│                                                             │
│  Shard 0            Shard 1            Shard 2              │
│  ┌──────────┐       ┌──────────┐       ┌──────────┐        │
│  │ Master   │       │ Master   │       │ Master   │        │
│  │ (AZ-a)   │       │ (AZ-b)   │       │ (AZ-c)   │        │
│  └────┬─────┘       └────┬─────┘       └────┬─────┘        │
│       │                  │                   │              │
│  ┌────▼─────┐       ┌────▼─────┐       ┌────▼─────┐        │
│  │ Replica  │       │ Replica  │       │ Replica  │        │
│  │ (AZ-b)   │       │ (AZ-c)   │       │ (AZ-a)   │        │
│  └────┬─────┘       └────┬─────┘       └────┬─────┘        │
│       │                  │                   │              │
│  ┌────▼─────┐       ┌────▼─────┐       ┌────▼─────┐        │
│  │ Replica  │       │ Replica  │       │ Replica  │        │
│  │ (AZ-c)   │       │ (AZ-a)   │       │ (AZ-b)   │        │
│  └──────────┘       └──────────┘       └──────────┘        │
│                                                             │
│  Config:                                                    │
│    appendonly yes                  # AOF persistence         │
│    appendfsync everysec            # fsync every second      │
│    min-replicas-to-write 1         # at least 1 replica ack │
│    maxmemory-policy allkeys-lru    # evict LRU on OOM       │
│    cluster-enabled yes                                       │
└─────────────────────────────────────────────────────────────┘
```

### 4.4 What Happens When Redis Is Unavailable?

Redis is not infallible. Network partitions, OOM errors, or cluster failovers can make it temporarily unavailable. The system MUST NOT stop processing payments because Redis is down.

**Fallback strategy:**

```
┌─────────────────────────────────────────────────────────┐
│              IDEMPOTENCY CHECK: DEGRADED MODE            │
│                                                         │
│  1. Try Redis lookup                                     │
│     ├── SUCCESS → return cached response (fast path)     │
│     └── FAILURE (timeout/error) → proceed to step 2     │
│                                                         │
│  2. Fallback: check PostgreSQL                           │
│     SELECT payment_id, status, response_body             │
│     FROM idempotency_log                                 │
│     WHERE merchant_id = ? AND idempotency_key = ?        │
│     ├── FOUND → return cached response (slower, ~5ms)    │
│     └── NOT FOUND → proceed with new payment             │
│                                                         │
│  3. After payment completes:                             │
│     - Write to PostgreSQL idempotency_log (always)       │
│     - Write to Redis (best effort, may fail)             │
│                                                         │
│  Note: PostgreSQL idempotency_log is the durable         │
│  fallback. Redis is the fast cache. The unique           │
│  constraint on (merchant_id, idempotency_key) in         │
│  PostgreSQL is the ultimate safety net against           │
│  double-charging, even if Redis is completely down.      │
└─────────────────────────────────────────────────────────┘
```

**Key insight**: Redis is the **performance optimization**, not the source of truth for idempotency. The PostgreSQL `UNIQUE` constraint on `(merchant_id, idempotency_key)` in the payments table is the ultimate deduplication guarantee. If both Redis and the DB check fail (catastrophic scenario), the payment service should **reject the request** with a 503 rather than risk a double charge.

---

## 5. Token Vault -- PCI DSS Cardholder Data Environment

### 5.1 What Is the CDE?

The Cardholder Data Environment (CDE) is a PCI DSS concept that defines the boundary of systems that store, process, or transmit cardholder data (Primary Account Number / PAN, expiry date, cardholder name, CVV).

**PCI DSS Requirement 1**: Install and maintain network security controls. The CDE must be isolated in its own network segment, separated from the rest of the infrastructure by firewalls. [VERIFIED -- PCI DSS v4.0, Requirement 1, mandates network segmentation to minimize the scope of the cardholder data environment.]

```
┌─────────────────────────────────────────────────────────────────────────┐
│                    PCI DSS SCOPE MINIMIZATION                          │
│                                                                        │
│  ┌────────────────────────────────────── General Network ──────────┐   │
│  │                                                                  │   │
│  │  ┌──────────────┐  ┌──────────────┐  ┌──────────────┐          │   │
│  │  │  API Gateway  │  │  Payment     │  │  Webhook     │          │   │
│  │  │              │  │  Service     │  │  Service     │          │   │
│  │  └──────────────┘  └──────┬───────┘  └──────────────┘          │   │
│  │                           │                                     │   │
│  │                    token  │  (only sends/receives tokens,       │   │
│  │                    only!  │   NEVER raw PANs)                   │   │
│  │                           │                                     │   │
│  └───────────────────────────┼─────────────────────────────────────┘   │
│                              │                                         │
│                     ┌────────▼────────┐                                │
│                     │    FIREWALL     │  (PCI DSS Req 1)              │
│                     │  (allow only    │                                │
│                     │   tokenization  │                                │
│                     │   service       │                                │
│                     │   traffic)      │                                │
│                     └────────┬────────┘                                │
│                              │                                         │
│  ┌───────────────────────────▼─────────────────── CDE ─────────────┐  │
│  │                                                                  │  │
│  │  ┌──────────────────┐      ┌──────────────────┐                 │  │
│  │  │  Tokenization    │      │  Token Vault DB   │                 │  │
│  │  │  Service         │◄────►│  (PostgreSQL,     │                 │  │
│  │  │  (the ONLY       │      │   encrypted,      │                 │  │
│  │  │   service that   │      │   HSM-backed)     │                 │  │
│  │  │   touches raw    │      │                   │                 │  │
│  │  │   card data)     │      │  Stores:          │                 │  │
│  │  │                  │      │  - PAN (AES-256)  │                 │  │
│  │  │  Input: raw PAN  │      │  - Expiry date    │                 │  │
│  │  │  Output: token   │      │  - Token mapping  │                 │  │
│  │  └──────────────────┘      └──────────────────┘                 │  │
│  │                                                                  │  │
│  │  ┌──────────────────┐                                           │  │
│  │  │  HSM Cluster     │  (Hardware Security Module)                │  │
│  │  │  ├─ Encryption   │  - Encryption keys NEVER leave HSM        │  │
│  │  │  │  keys         │  - FIPS 140-2 Level 3 certified           │  │
│  │  │  ├─ Key rotation │  - Tamper-evident, tamper-responsive      │  │
│  │  │  └─ Signing      │  - Annual key rotation                    │  │
│  │  └──────────────────┘                                           │  │
│  │                                                                  │  │
│  │  Network rules:                                                  │  │
│  │  - NO outbound internet access                                   │  │
│  │  - NO access from developer machines                             │  │
│  │  - Ingress: ONLY from tokenization service                       │  │
│  │  - Egress: ONLY to HSM cluster (for encrypt/decrypt ops)        │  │
│  │  - All access logged and alerted                                │  │
│  │  - Penetration tested annually                                  │  │
│  └──────────────────────────────────────────────────────────────────┘  │
│                                                                        │
└─────────────────────────────────────────────────────────────────────────┘
```

### 5.2 Why CDE Isolation Matters: Scope Minimization

PCI DSS compliance is expensive and invasive -- annual audits by a Qualified Security Assessor (QSA), quarterly vulnerability scans, penetration testing, extensive documentation. The audit scope covers every system in the CDE and every system "connected to" the CDE.

**By isolating raw card data in a tiny vault, you minimize PCI scope to:**
- The tokenization service (~1 microservice)
- The token vault database (~1 database)
- The HSM cluster
- The network segment they live in

**Everything else** -- the payment service, the ledger, the API gateway, the analytics pipeline -- never sees raw card data. They only handle tokens (e.g., `tok_abc123`). This means those systems are **out of scope** for the most rigorous PCI DSS controls.

This is precisely why Stripe created Stripe.js and Stripe Elements -- the merchant's frontend sends card data directly to Stripe's tokenization endpoint, and the merchant's backend only ever sees a token. The merchant's entire backend is out of PCI scope. [VERIFIED -- Stripe's documentation explicitly states that using Stripe.js/Elements means the merchant's servers never handle raw card data, reducing their PCI compliance burden to SAQ A or SAQ A-EP, the simplest self-assessment questionnaires.]

### 5.3 Encryption Architecture

```
┌─────────────────────────────────────────────────────────────────┐
│                   ENCRYPTION LAYERS                              │
│                                                                 │
│  Layer 1: Encryption in Transit                                  │
│  ┌──────────────────────────────────────────────────┐           │
│  │  TLS 1.3 (minimum) for all connections           │           │
│  │  - Client → Tokenization Service                  │           │
│  │  - Tokenization Service → Token Vault DB          │           │
│  │  - Tokenization Service → HSM                     │           │
│  │  Certificate pinning for internal connections     │           │
│  │  Perfect Forward Secrecy (PFS) cipher suites      │           │
│  └──────────────────────────────────────────────────┘           │
│                                                                 │
│  Layer 2: Encryption at Rest (Database Level)                    │
│  ┌──────────────────────────────────────────────────┐           │
│  │  Full-disk encryption (dm-crypt / LUKS or cloud   │           │
│  │  provider disk encryption)                        │           │
│  │  Protects against physical disk theft             │           │
│  └──────────────────────────────────────────────────┘           │
│                                                                 │
│  Layer 3: Application-Level Encryption (Column Level)            │
│  ┌──────────────────────────────────────────────────┐           │
│  │  PAN encrypted with AES-256-GCM before writing    │           │
│  │  to database. Even a DB admin with full access     │           │
│  │  sees only ciphertext.                             │           │
│  │                                                    │           │
│  │  Encryption key stored in HSM, NEVER on disk.      │           │
│  │  Tokenization service calls HSM for every          │           │
│  │  encrypt/decrypt operation.                        │           │
│  └──────────────────────────────────────────────────┘           │
│                                                                 │
│  Layer 4: Key Management (HSM)                                   │
│  ┌──────────────────────────────────────────────────┐           │
│  │  Master Key (KEK - Key Encryption Key):            │           │
│  │  - Generated inside HSM, never exported            │           │
│  │  - Used to encrypt Data Encryption Keys (DEKs)     │           │
│  │                                                    │           │
│  │  Data Encryption Keys (DEKs):                      │           │
│  │  - One DEK per PAN record (or per batch)           │           │
│  │  - DEK encrypted by KEK, stored in vault DB        │           │
│  │  - To decrypt: send encrypted DEK to HSM,          │           │
│  │    HSM returns plaintext DEK, use DEK to           │           │
│  │    decrypt PAN, then wipe DEK from memory          │           │
│  │                                                    │           │
│  │  Key Rotation:                                     │           │
│  │  - KEK rotated annually (PCI DSS Req 3.6)         │           │
│  │  - Re-encrypt all DEKs with new KEK               │           │
│  │  - Old KEK retained (in HSM) for decrypting       │           │
│  │    records encrypted with old DEKs                 │           │
│  └──────────────────────────────────────────────────┘           │
└─────────────────────────────────────────────────────────────────┘
```

### 5.4 HSM (Hardware Security Module) Deep Dive

HSMs are purpose-built hardware devices that generate, store, and manage cryptographic keys. They are **required** by PCI DSS for protecting encryption keys in a payment environment.

**Key properties of HSMs:**

- **FIPS 140-2 Level 3** (or Level 4) certified -- the US government standard for cryptographic hardware. Level 3 requires physical tamper-resistance and tamper-evidence (if someone opens the device, it zeros all keys). [VERIFIED -- FIPS 140-2 is the standard; PCI DSS references FIPS for key management. Payment HSMs are typically certified to Level 3.]
- **Keys never leave the HSM in plaintext** -- the HSM performs all cryptographic operations internally. The application sends plaintext data IN and receives ciphertext OUT (or vice versa). The encryption key is never exposed to application memory, operating system, or disk.
- **Tamper-responsive** -- if the physical enclosure is breached, all keys are automatically destroyed (zeroized).
- **Dual control** -- HSM initialization and key loading require multiple authorized personnel (e.g., 3-of-5 key custodians), each holding a component of the master key. No single person can access the keys.

**Cloud HSM options:**
- AWS CloudHSM -- dedicated HSM appliances in AWS. FIPS 140-2 Level 3 validated. [VERIFIED -- AWS CloudHSM documentation confirms FIPS 140-2 Level 3 validation.]
- AWS KMS -- managed key management service. Uses HSMs internally but abstracts the complexity. FIPS 140-2 Level 2 (software boundary) or Level 3 (custom key store with CloudHSM). [VERIFIED -- AWS KMS documentation confirms Level 2 validation for standard KMS, Level 3 for CloudHSM-backed custom key stores.]
- GCP Cloud HSM -- FIPS 140-2 Level 3 validated HSMs integrated with Cloud KMS.
- Azure Dedicated HSM -- Thales Luna HSMs in Azure datacenters.

**Common HSM vendors for on-premise payment environments**: Thales Luna (formerly SafeNet/Gemalto), Utimaco, Futurex. [INFERRED -- these are well-known HSM vendors in the payments industry but specific vendor usage by Stripe/Razorpay is not publicly documented.]

### 5.5 Token Vault Schema

```sql
-- Token vault database (SEPARATE from the main payment DB)
-- Lives in the CDE network segment

CREATE TABLE card_tokens (
    token_id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    -- The token returned to the outside world
    -- e.g., tok_abc123 -- this is what the payment service sees

    encrypted_pan       BYTEA NOT NULL,
    -- PAN encrypted with AES-256-GCM
    -- Plaintext is NEVER stored

    encrypted_dek       BYTEA NOT NULL,
    -- Data Encryption Key, encrypted by the HSM's master key (KEK)
    -- To decrypt PAN: send encrypted_dek to HSM → get plaintext DEK → decrypt PAN

    pan_last_four       CHAR(4) NOT NULL,
    -- Last 4 digits stored in plaintext for display (e.g., "****4242")
    -- PCI DSS allows last 4 in plaintext

    pan_bin             CHAR(6) NOT NULL,
    -- First 6 digits (Bank Identification Number)
    -- PCI DSS allows first 6 in plaintext
    -- Used for routing, fraud detection, BIN lookup

    expiry_month        SMALLINT NOT NULL,
    expiry_year         SMALLINT NOT NULL,
    -- Stored encrypted or in plaintext (PCI DSS allows expiry in plaintext
    -- but best practice is to encrypt)

    card_brand          VARCHAR(16) NOT NULL,   -- VISA, MASTERCARD, AMEX, etc.
    card_type           VARCHAR(16),            -- CREDIT, DEBIT, PREPAID
    issuing_country     CHAR(2),                -- ISO 3166-1 alpha-2
    issuing_bank        VARCHAR(128),

    customer_id         UUID,                   -- FK to customer in main DB
    fingerprint         VARCHAR(64) NOT NULL,
    -- Hash of the full PAN (SHA-256 with salt)
    -- Used to detect if the same card is tokenized twice
    -- (return existing token instead of creating a new one)

    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    deactivated_at      TIMESTAMPTZ,            -- soft delete (never hard delete)

    CONSTRAINT uq_fingerprint UNIQUE (fingerprint)
);

-- CVV/CVC is NEVER stored (PCI DSS Requirement 3.4)
-- It is used only during the authorization request and immediately discarded
```

**Note on CVV/CVC**: PCI DSS explicitly prohibits storing the 3-digit (or 4-digit for Amex) security code after authorization. It can be collected in the tokenization request, forwarded to the acquirer for the auth request, and then MUST be purged from all systems. [VERIFIED -- PCI DSS Requirement 3.3.2 (v4.0) prohibits storage of the card verification code/value after authorization.]

---

## 6. Document Store -- Unstructured Data

### 6.1 What Goes Here?

Not all payment system data fits neatly into relational tables:

```
┌─────────────────────────────────────────────────────────────────┐
│                    DOCUMENT STORE USE CASES                      │
│                                                                 │
│  ┌─────────────────────────────────────────┐                    │
│  │  Webhook Payloads                        │                    │
│  │  - Raw JSON payloads sent to merchants   │                    │
│  │  - Delivery attempts + responses         │                    │
│  │  - Stored for debugging failed webhooks  │                    │
│  │  - Retention: 30-90 days                 │                    │
│  └─────────────────────────────────────────┘                    │
│                                                                 │
│  ┌─────────────────────────────────────────┐                    │
│  │  Dispute Evidence                        │                    │
│  │  - PDF receipts, screenshots             │                    │
│  │  - Delivery tracking proof               │                    │
│  │  - Communication logs (emails, chats)    │                    │
│  │  - Submitted by merchant to fight        │                    │
│  │    chargebacks                           │                    │
│  │  - Retention: 7+ years (legal hold)      │                    │
│  └─────────────────────────────────────────┘                    │
│                                                                 │
│  ┌─────────────────────────────────────────┐                    │
│  │  Merchant Onboarding Documents           │                    │
│  │  - KYC/KYB documents (ID scans,          │                    │
│  │    business registration)                │                    │
│  │  - Bank account verification docs        │                    │
│  │  - Underwriting risk assessment          │                    │
│  │  - Retention: lifetime of merchant       │                    │
│  │    account + 5 years                     │                    │
│  └─────────────────────────────────────────┘                    │
│                                                                 │
│  ┌─────────────────────────────────────────┐                    │
│  │  Acquirer / Network Raw Responses        │                    │
│  │  - Raw ISO 8583 messages or API          │                    │
│  │    responses from acquirers              │                    │
│  │  - Useful for reconciliation debugging   │                    │
│  │  - Retention: 90 days hot, 1 year warm   │                    │
│  └─────────────────────────────────────────┘                    │
└─────────────────────────────────────────────────────────────────┘
```

### 6.2 Technology Choices

**Amazon S3** for file-like objects (PDFs, images, large JSON blobs):
- Virtually unlimited storage
- Built-in lifecycle policies (move to Glacier after 90 days)
- Server-side encryption (SSE-S3 or SSE-KMS)
- Versioning for audit trail
- Cross-region replication for disaster recovery

**MongoDB** (or DynamoDB) for semi-structured data (webhook payloads, acquirer responses):
- Flexible schema (each acquirer returns different response formats)
- TTL indexes for automatic expiry
- Rich querying for debugging ("show me all failed webhook deliveries for merchant X in the last 24 hours")

In practice, many payment companies use **S3 for files + a relational or document DB for metadata about those files**. The metadata (file name, merchant ID, upload timestamp, dispute ID) lives in PostgreSQL or MongoDB, while the actual binary content lives in S3.

---

## 7. Analytics / Data Warehouse -- CDC Pipeline

### 7.1 Why a Separate Analytics Store?

Running complex analytical queries (daily settlement reports, fraud pattern detection, merchant performance dashboards) against the primary transactional database would:

1. **Degrade payment latency** -- analytical queries (aggregations, joins across millions of rows) compete for CPU and I/O with real-time payment processing.
2. **Risk locking contention** -- long-running reads can block writes (or vice versa, depending on isolation level).
3. **Lack columnar optimization** -- OLTP row stores (PostgreSQL) are inefficient for OLAP-style aggregations. Columnar stores (Snowflake, BigQuery, Redshift) are 10-100x faster for analytical queries.

### 7.2 CDC Pipeline Architecture

```
┌───────────────────────────────────────────────────────────────────────────┐
│                        CDC → WAREHOUSE PIPELINE                          │
│                                                                          │
│  ┌──────────────┐     ┌──────────────┐     ┌──────────────┐             │
│  │ PostgreSQL   │     │   Debezium   │     │    Kafka     │             │
│  │ (Primary DB) │────►│   (CDC       │────►│  (Streaming  │             │
│  │              │ WAL │   Connector) │     │   Platform)  │             │
│  │              │     │              │     │              │             │
│  └──────────────┘     └──────────────┘     └──────┬───────┘             │
│                                                    │                     │
│                                              ┌─────┴─────┐              │
│                                              │           │              │
│                                              ▼           ▼              │
│                                    ┌──────────────┐  ┌──────────────┐   │
│                                    │  Kafka       │  │  Kafka       │   │
│                                    │  Connect     │  │  Streams /   │   │
│                                    │  (Sink       │  │  Flink       │   │
│                                    │   Connector) │  │  (Real-time  │   │
│                                    │              │  │   Transforms)│   │
│                                    └──────┬───────┘  └──────┬───────┘   │
│                                           │                 │           │
│                                           ▼                 ▼           │
│                                    ┌──────────────┐  ┌──────────────┐   │
│                                    │  Data        │  │  Real-time   │   │
│                                    │  Warehouse   │  │  Dashboards  │   │
│                                    │  (Snowflake/ │  │  (Grafana /  │   │
│                                    │   BigQuery/  │  │   Superset)  │   │
│                                    │   Redshift)  │  │              │   │
│                                    └──────────────┘  └──────────────┘   │
│                                                                          │
│  Lag: 1-5 minutes (near-real-time)                                       │
│  Not acceptable for payment state reads (use primary DB for that)        │
│  Acceptable for: reporting, reconciliation, ML features, dashboards      │
└───────────────────────────────────────────────────────────────────────────┘
```

### 7.3 How CDC Works (Debezium + PostgreSQL)

**Change Data Capture (CDC)** reads the PostgreSQL Write-Ahead Log (WAL) to capture every INSERT, UPDATE, and DELETE as a stream of events. This is non-invasive -- it doesn't add load to the primary database (beyond WAL reading).

**Debezium** is the most widely used open-source CDC connector. [VERIFIED -- Debezium is a widely adopted open-source CDC platform maintained by Red Hat, commonly used in event-driven architectures. It supports PostgreSQL via logical decoding.]

```
PostgreSQL WAL → Debezium reads logical replication slot
                → Converts each change to a Kafka message:
                  {
                    "before": { "payment_id": "pay_abc", "status": "AUTHORIZED", ... },
                    "after":  { "payment_id": "pay_abc", "status": "CAPTURED", ... },
                    "op": "u",           // u=update, c=create, d=delete
                    "ts_ms": 1705326622456,
                    "source": {
                      "schema": "public",
                      "table": "payments",
                      "lsn": 123456789
                    }
                  }
                → Published to Kafka topic: cdc.public.payments
```

### 7.4 What the Warehouse Powers

| Use Case                    | Query Example                                                    | Frequency       |
|-----------------------------|------------------------------------------------------------------|-----------------|
| **Daily settlement report** | SUM(capture_amount) - SUM(refund_amount) - SUM(fees) per merchant| Daily            |
| **Reconciliation**          | JOIN internal_payments WITH acquirer_settlement_files ON ref_id   | Hourly/Daily     |
| **Fraud ML features**       | Aggregate velocity features per card fingerprint (last 24h)      | Real-time / Batch|
| **Merchant analytics**      | Payment success rate, avg transaction value, chargeback rate     | On-demand        |
| **Financial reporting**     | Revenue, GMV, net revenue by geography/currency/payment method   | Weekly/Monthly   |
| **Regulatory reporting**    | Transaction volumes by jurisdiction for compliance filings       | Quarterly        |

### 7.5 Lag Tolerance

The pipeline has a **1-5 minute lag** between a payment event in the primary DB and its availability in the warehouse. This is acceptable because:

- Settlement reports are generated at end-of-day (hours of lag would be fine).
- Reconciliation runs hourly at most.
- ML feature engineering for fraud detection uses batch-computed features (minutes of lag is acceptable; for real-time fraud scoring, features are computed in-stream, not from the warehouse).

**What CANNOT tolerate this lag**: payment state reads ("is this payment captured?"). Those MUST go to the primary database. Never read payment status from the warehouse.

---

## 8. Caching Layer -- Redis for Hot Data

### 8.1 What Gets Cached?

Not payment state (that requires strong consistency from PostgreSQL). Only **configuration data and reference data** that changes infrequently:

```
┌─────────────────────────────────────────────────────────────────────┐
│                        CACHE CONTENTS                               │
│                                                                     │
│  ┌──────────────────────────────────────────┐                       │
│  │  Merchant Configuration                   │                       │
│  │  Key: merchant_config:{merchant_id}       │                       │
│  │  Value: {                                 │                       │
│  │    routing_rules: [...],                  │  TTL: 5 min           │
│  │    risk_rules: [...],                     │  Invalidate: on       │
│  │    webhook_url: "https://...",            │    config change      │
│  │    settlement_schedule: "T+2",            │                       │
│  │    supported_currencies: ["USD","EUR"]    │                       │
│  │  }                                        │                       │
│  └──────────────────────────────────────────┘                       │
│                                                                     │
│  ┌──────────────────────────────────────────┐                       │
│  │  BIN Lookup Table                         │                       │
│  │  Key: bin:{first_6_digits}                │                       │
│  │  Value: {                                 │  TTL: 24 hours        │
│  │    card_brand: "VISA",                    │  Refresh: daily bulk  │
│  │    card_type: "CREDIT",                   │    load from BIN      │
│  │    issuing_bank: "Chase",                 │    database provider  │
│  │    issuing_country: "US",                 │                       │
│  │    card_level: "PLATINUM"                 │                       │
│  │  }                                        │                       │
│  └──────────────────────────────────────────┘                       │
│                                                                     │
│  ┌──────────────────────────────────────────┐                       │
│  │  Exchange Rates                           │                       │
│  │  Key: fx_rate:{from}:{to}                 │                       │
│  │  Value: {                                 │  TTL: 1 min           │
│  │    rate: 1.0856,                          │  Source: ECB / XE /   │
│  │    timestamp: "2025-01-15T14:30:00Z"     │    internal FX engine │
│  │  }                                        │                       │
│  └──────────────────────────────────────────┘                       │
│                                                                     │
│  ┌──────────────────────────────────────────┐                       │
│  │  Acquirer Health Status                   │                       │
│  │  Key: acquirer_health:{acquirer_id}       │                       │
│  │  Value: {                                 │  TTL: 30 sec          │
│  │    status: "HEALTHY",                     │  Updated by health    │
│  │    success_rate_5m: 0.97,                 │    check service      │
│  │    avg_latency_5m_ms: 245,                │                       │
│  │    last_checked: "2025-01-15T14:30:00Z"  │                       │
│  │  }                                        │                       │
│  └──────────────────────────────────────────┘                       │
└─────────────────────────────────────────────────────────────────────┘
```

### 8.2 Cache Invalidation Strategies

Cache invalidation is "one of the two hard problems in computer science" (along with naming things and off-by-one errors). For a payment system:

```
┌─────────────────────────────────────────────────────────────────────┐
│                   INVALIDATION STRATEGIES                           │
│                                                                     │
│  Strategy 1: TTL-Based (Time-To-Live)                               │
│  ┌──────────────────────────────────────────────────┐               │
│  │  Cache entry expires after a fixed duration.      │               │
│  │  Next request triggers a cache miss → DB read     │               │
│  │  → cache repopulation.                            │               │
│  │                                                   │               │
│  │  Used for: FX rates (1 min TTL),                  │               │
│  │           BIN tables (24 hr TTL),                  │               │
│  │           acquirer health (30 sec TTL)             │               │
│  │                                                   │               │
│  │  Pros: Simple, no coordination needed.             │               │
│  │  Cons: Stale data for up to TTL duration.          │               │
│  └──────────────────────────────────────────────────┘               │
│                                                                     │
│  Strategy 2: Event-Driven Invalidation                              │
│  ┌──────────────────────────────────────────────────┐               │
│  │  When config changes, publish an invalidation     │               │
│  │  event to Kafka → all API servers consume the     │               │
│  │  event and delete/refresh the cache entry.        │               │
│  │                                                   │               │
│  │  Used for: Merchant config (routing rules,        │               │
│  │           risk rules, webhook URLs)                │               │
│  │                                                   │               │
│  │  Pros: Near-instant invalidation (<1 sec).         │               │
│  │  Cons: More complex, depends on Kafka.             │               │
│  └──────────────────────────────────────────────────┘               │
│                                                                     │
│  Strategy 3: Write-Through                                          │
│  ┌──────────────────────────────────────────────────┐               │
│  │  On config update: write to DB AND cache in the   │               │
│  │  same operation. The cache is always up-to-date.  │               │
│  │                                                   │               │
│  │  Used for: Critical config that affects payment   │               │
│  │           routing decisions.                       │               │
│  │                                                   │               │
│  │  Pros: Cache never stale.                          │               │
│  │  Cons: Write latency increases; risk of cache-DB  │               │
│  │        inconsistency on partial failure.           │               │
│  └──────────────────────────────────────────────────┘               │
│                                                                     │
│  IN PRACTICE: Combine all three:                                    │
│  - Event-driven invalidation for merchant config (most critical)    │
│  - TTL for reference data (BIN tables, FX rates)                    │
│  - Write-through for acquirer health (updated very frequently)      │
│  - TTL as a safety net on ALL entries (even event-invalidated       │
│    ones get a 5-min TTL as a fallback)                               │
└─────────────────────────────────────────────────────────────────────┘
```

### 8.3 What NEVER Gets Cached

- **Payment status** -- must always be read from PostgreSQL. A stale cached status could cause a double capture or a refund on an already-refunded payment.
- **Merchant balances** -- must be strongly consistent. A stale balance could allow a payout exceeding available funds.
- **Idempotency responses** -- these live in the dedicated idempotency store (Section 4), not the general cache.

---

## 9. Multi-Region Considerations

### 9.1 Data Residency Requirements

```
┌─────────────────────────────────────────────────────────────────────────┐
│                     DATA RESIDENCY MAP                                  │
│                                                                         │
│  ┌─────────────────────────────────────────────────────────────┐       │
│  │  REGULATION         REQUIREMENT                              │       │
│  ├─────────────────────────────────────────────────────────────┤       │
│  │  GDPR (EU)          EU citizen payment data must be stored   │       │
│  │                     and processed within the EU/EEA.         │       │
│  │                     Cross-border transfers require Standard  │       │
│  │                     Contractual Clauses (SCCs) or adequacy   │       │
│  │                     decision.                                │       │
│  ├─────────────────────────────────────────────────────────────┤       │
│  │  RBI (India)        Payment data for Indian transactions     │       │
│  │                     must be stored exclusively in India.     │       │
│  │                     [VERIFIED — RBI issued a directive in    │       │
│  │                     April 2018 mandating that all payment    │       │
│  │                     system data be stored only in India.     │       │
│  │                     This affected Visa, Mastercard, and all  │       │
│  │                     PSPs operating in India.]                │       │
│  ├─────────────────────────────────────────────────────────────┤       │
│  │  PDPA (Singapore)   Personal data may not be transferred     │       │
│  │                     outside Singapore unless the recipient   │       │
│  │                     provides comparable protection.          │       │
│  ├─────────────────────────────────────────────────────────────┤       │
│  │  LGPD (Brazil)      Similar to GDPR. Personal data must     │       │
│  │                     have adequate protection for cross-      │       │
│  │                     border transfers.                        │       │
│  └─────────────────────────────────────────────────────────────┘       │
│                                                                         │
│  IMPLICATION: You cannot have a single global database. Payment         │
│  data for EU merchants/customers must live in an EU region.             │
│  Indian payment data must live in India.                                │
└─────────────────────────────────────────────────────────────────────────┘
```

### 9.2 Active-Passive vs Active-Active

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                    ACTIVE-PASSIVE (Recommended for Payments)                │
│                                                                             │
│  ┌─────────────────────────────┐      ┌─────────────────────────────┐      │
│  │  PRIMARY REGION (us-east-1) │      │  STANDBY REGION (us-west-2) │      │
│  │                             │      │                             │      │
│  │  ┌────────┐  ┌────────┐    │      │  ┌────────┐  ┌────────┐    │      │
│  │  │ API    │  │Payment │    │ Async│  │ API    │  │Payment │    │      │
│  │  │Servers │  │Service │    │ Repl │  │Servers │  │Service │    │      │
│  │  └────────┘  └────────┘    │──────│  └────────┘  └────────┘    │      │
│  │                             │      │  (warm standby, not        │      │
│  │  ┌────────┐  ┌────────┐    │      │   serving traffic)         │      │
│  │  │PostgreSQL│ │ Redis  │    │      │  ┌────────┐  ┌────────┐    │      │
│  │  │(Primary)│  │Cluster │    │      │  │PostgreSQL│ │ Redis  │    │      │
│  │  └────────┘  └────────┘    │      │  │(Replica)│  │Cluster │    │      │
│  │                             │      │  └────────┘  └────────┘    │      │
│  │  ALL writes go here         │      │  Read-only replicas for    │      │
│  │                             │      │  reporting + DR failover   │      │
│  └─────────────────────────────┘      └─────────────────────────────┘      │
│                                                                             │
│  Failover: DNS switch → standby promoted to primary                        │
│  RTO: 5-15 minutes                                                          │
│  RPO: 0 (with sync replication) or seconds (with async)                    │
│                                                                             │
│  WHY active-passive?                                                        │
│  - Financial data requires STRONG consistency.                              │
│  - Active-active with writes in two regions creates conflicts:             │
│    "Merchant X captured payment P in us-east AND refunded P in eu-west     │
│     at the same millisecond. Which wins?"                                  │
│  - Conflict resolution for financial data is not "last write wins."        │
│    A wrong resolution means money appears or disappears.                   │
└─────────────────────────────────────────────────────────────────────────────┘
```

### 9.3 Active-Active: Why It Is Much Harder for Payments Than for Netflix

Netflix runs active-active across multiple AWS regions. When a user updates their watch history in us-east-1 and us-west-2 simultaneously, Cassandra's last-write-wins conflict resolution handles it -- the worst case is a minor inconsistency in the "continue watching" row. Nobody loses money.

**For a payment system, active-active introduces catastrophic failure modes:**

```
┌─────────────────────────────────────────────────────────────────────────┐
│                    WHY ACTIVE-ACTIVE IS DANGEROUS                      │
│                                                                         │
│  Scenario: Active-active with writes in both regions                    │
│                                                                         │
│  Region A (us-east-1)              Region B (eu-west-1)                │
│  ┌──────────────────────┐          ┌──────────────────────┐            │
│  │ Merchant sends       │          │ Customer disputes     │            │
│  │ CAPTURE for          │          │ same payment,         │            │
│  │ payment P            │          │ system initiates      │            │
│  │ at T=0               │          │ REFUND at T=0         │            │
│  └──────────┬───────────┘          └──────────┬───────────┘            │
│             │                                  │                        │
│             ▼                                  ▼                        │
│  Payment P status:                  Payment P status:                   │
│  AUTHORIZED → CAPTURED              AUTHORIZED → REFUND_PENDING         │
│                                                                         │
│  CONFLICT: Payment P is now CAPTURED in Region A and                    │
│  REFUND_PENDING in Region B. How do you resolve this?                  │
│                                                                         │
│  "Last write wins" → If REFUND wins, the merchant loses $100           │
│                       they legitimately captured.                       │
│                    → If CAPTURE wins, the customer is charged           │
│                       despite an active dispute.                       │
│                                                                         │
│  NEITHER outcome is acceptable. Financial state machines                │
│  require SERIALIZABLE consistency — every state transition              │
│  must see the latest state before proceeding.                          │
│                                                                         │
│  SOLUTION: Route all writes for a given payment to the SAME            │
│  region. This effectively makes it active-passive at the                │
│  payment level, even if the infrastructure is multi-region.            │
└─────────────────────────────────────────────────────────────────────────┘
```

### 9.4 Practical Multi-Region Architecture

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                    MULTI-REGION WITH DATA RESIDENCY                         │
│                                                                             │
│  ┌──────────────────────┐     ┌──────────────────────┐                     │
│  │  US Region            │     │  EU Region            │                     │
│  │  (us-east-1)          │     │  (eu-west-1)          │                     │
│  │                       │     │                       │                     │
│  │  US merchants' data   │     │  EU merchants' data   │                     │
│  │  US customers' data   │     │  EU customers' data   │                     │
│  │  US card tokens       │     │  EU card tokens       │                     │
│  │                       │     │                       │                     │
│  │  Primary DB + standby │     │  Primary DB + standby │                     │
│  │  within region        │     │  within region        │                     │
│  └──────────┬────────────┘     └──────────┬────────────┘                     │
│             │                              │                                 │
│             └──────────┬───────────────────┘                                 │
│                        │                                                     │
│              ┌─────────▼──────────┐                                          │
│              │  Global Services   │                                          │
│              │  (regionless)      │                                          │
│              │                    │                                          │
│              │  - Global merchant │  (no PII, no card data)                  │
│              │    directory       │                                          │
│              │  - FX rates        │                                          │
│              │  - BIN database    │                                          │
│              │  - Routing config  │                                          │
│              └────────────────────┘                                          │
│                                                                             │
│  Routing decision:                                                          │
│  1. Merchant registers → assigned to a "home region" based on               │
│     geography (EU merchant → eu-west-1).                                    │
│  2. ALL payment writes for that merchant go to their home region.           │
│  3. Global API gateway routes requests to the correct region                │
│     based on merchant ID → region mapping.                                  │
│  4. Cross-region reads (e.g., global dashboard) use aggregated,            │
│     anonymized data in the data warehouse.                                  │
└─────────────────────────────────────────────────────────────────────────────┘
```

---

## 10. Contrast with Netflix's Storage Architecture

This section crystallizes why payment system storage is fundamentally different from content-delivery platforms.

```
┌────────────────────────────────────────────────────────────────────────────────┐
│                  PAYMENT SYSTEM vs NETFLIX — STORAGE COMPARISON               │
│                                                                               │
│  Dimension              Payment System            Netflix                     │
│  ─────────────────────  ──────────────────────    ──────────────────────      │
│  Primary workload       Write-heavy               Read-heavy                  │
│                         (every txn = write to      (encode once, stream        │
│                          payments + ledger +        billions of times)         │
│                          events + idempotency)                                 │
│                                                                               │
│  Consistency model      Strong (ACID)             Eventual (AP)               │
│                         Cannot tolerate stale      "Continue watching" can     │
│                         payment state. A stale     be stale for seconds.       │
│                         balance means money        No money at stake.          │
│                         appears/disappears.                                    │
│                                                                               │
│  CAP trade-off          CP (Consistency +          AP (Availability +          │
│                         Partition tolerance)        Partition tolerance)        │
│                         Reject writes during       Accept writes during        │
│                         partition rather than       partition, resolve          │
│                         risk inconsistency.         conflicts later (LWW).     │
│                                                                               │
│  Primary database       PostgreSQL                 Cassandra (Apache)          │
│                         (relational, ACID,          (wide-column, eventual     │
│                          row-level locking)          consistency, LWW)          │
│                                                                               │
│  Why that DB?           Multi-table transactions   Massive horizontal scale.   │
│                         (payment + ledger +          100K+ writes/sec across    │
│                         balance in one COMMIT).      hundreds of nodes. No      │
│                         Cannot do this in            single point of failure.   │
│                         Cassandra.                   Tunable consistency.       │
│                                                                               │
│  Sharding key           Merchant ID                 User ID / content ID       │
│                         (co-locate txns for         (co-locate user's          │
│                          same merchant)               viewing data)             │
│                                                                               │
│  Event streaming        Kafka                       Kafka                      │
│                         (payment events, audit)      (viewing activity,         │
│                                                       recommendations)          │
│                                                                               │
│  Caching                Redis (config, BIN, FX)     EVCache (Memcached)        │
│                         NEVER cache payment state    Cache everything —         │
│                                                       movie metadata, images,   │
│                                                       personalization data      │
│                                                                               │
│  Data warehouse         Snowflake / BigQuery /      Snowflake / Spark /        │
│                         Redshift (CDC pipeline)      Presto (S3 data lake)     │
│                                                                               │
│  Multi-region           Active-passive (writes      Active-active              │
│                         in one region, standby      (writes in all regions,    │
│                         in others). Financial       Cassandra resolves         │
│                         consistency > availability.  conflicts with LWW)       │
│                                                                               │
│  Data loss tolerance    ZERO. Losing a payment      Tolerable for non-         │
│                         record means money is        critical data. Losing a   │
│                         unaccounted for.             "thumbs up" rating is     │
│                         RPO = 0 (synchronous         annoying, not financial.  │
│                         replication).                 RPO can be seconds.       │
│                                                                               │
│  Compliance             PCI DSS (card data),        No financial regulation.   │
│                         SOX, GDPR, RBI, etc.         GDPR for personal data.  │
│                         Shapes the ENTIRE            Compliance is a concern   │
│                         architecture (CDE,           but doesn't dictate       │
│                         HSMs, audit trails,          storage architecture.     │
│                         data residency).                                       │
│                                                                               │
│  Graceful degradation   Very limited. You cannot    Extensive. Show a cached   │
│                         "approximately capture"      homepage instead of        │
│                         a payment. It either         personalized recs. Show   │
│                         succeeds or fails.           SD instead of 4K. Many    │
│                                                      degradation options.      │
└────────────────────────────────────────────────────────────────────────────────┘
```

### The Core Insight

Netflix's engineering challenges are about **scale and availability** -- serving billions of reads with sub-100ms latency globally. Their storage choices (Cassandra, EVCache, S3) optimize for read throughput and partition tolerance.

Payment systems' engineering challenges are about **correctness and durability** -- ensuring every financial state transition is atomic, every ledger entry balances, and no money appears or disappears. Their storage choices (PostgreSQL, Redis with persistence, HSM-backed vaults) optimize for write consistency and auditability.

**This is not about one being "harder" than the other** -- they are different domains with different correctness requirements, leading to different CAP trade-offs. Netflix chose AP because their domain tolerates eventual consistency. Payment systems chose CP because their domain demands strong consistency. Both are correct choices for their respective domains.

---

## 11. Full Architecture Diagram -- Data Flow Between Components

```
┌─────────────────────────────────────────────────────────────────────────────────┐
│                    COMPLETE DATA FLOW: PAYMENT CAPTURE                          │
│                                                                                 │
│  Client (Merchant's Server)                                                     │
│       │                                                                         │
│       │ POST /payments/{id}/capture                                             │
│       │ Idempotency-Key: "order_123_capture"                                   │
│       ▼                                                                         │
│  ┌──────────────────────────────────────────────────────────────────────┐       │
│  │                        API GATEWAY                                   │       │
│  │  1. Rate limit check                                                │       │
│  │  2. Authentication (API key validation)                              │       │
│  │  3. Route to correct region based on merchant_id                    │       │
│  └──────────────────────────────────┬───────────────────────────────────┘       │
│                                     │                                           │
│                                     ▼                                           │
│  ┌──────────────────────────────────────────────────────────────────────┐       │
│  │                      PAYMENT SERVICE                                 │       │
│  │                                                                      │       │
│  │  Step 1: IDEMPOTENCY CHECK                                           │       │
│  │  ┌──────────┐                                                        │       │
│  │  │  Redis   │  GET idempotency:merch_xyz:order_123_capture           │       │
│  │  │  Cluster │  → MISS (first request) → continue                     │       │
│  │  └──────────┘  → HIT (retry) → return cached response               │       │
│  │                                                                      │       │
│  │  Step 2: MERCHANT CONFIG (from cache)                                │       │
│  │  ┌──────────┐                                                        │       │
│  │  │  Redis   │  GET merchant_config:merch_xyz                         │       │
│  │  │  Cache   │  → routing rules, risk rules                           │       │
│  │  └──────────┘                                                        │       │
│  │                                                                      │       │
│  │  Step 3: FRAUD CHECK (inline, sub-50ms)                              │       │
│  │  ┌──────────┐                                                        │       │
│  │  │  Fraud   │  → risk_score = 12 (low risk) → proceed               │       │
│  │  │  Engine  │                                                        │       │
│  │  └──────────┘                                                        │       │
│  │                                                                      │       │
│  │  Step 4: ROUTE TO ACQUIRER                                           │       │
│  │  ┌──────────┐                                                        │       │
│  │  │  Routing │  → Acquirer A (Visa US, 97% success rate)             │       │
│  │  │  Engine  │  → uses BIN from Redis cache                           │       │
│  │  └──────────┘                                                        │       │
│  │                                                                      │       │
│  │  Step 5: CALL ACQUIRER                                               │       │
│  │  ┌──────────┐                                                        │       │
│  │  │ Acquirer │  → POST /v1/captures {amount, auth_code, ref}         │       │
│  │  │ Adapter  │  → Response: {status: "captured", ref: "ch_123"}      │       │
│  │  └──────────┘                                                        │       │
│  │                                                                      │       │
│  │  Step 6: ATOMIC DB WRITE (the critical section)                      │       │
│  │  ┌──────────────┐                                                    │       │
│  │  │  PostgreSQL   │  BEGIN;                                           │       │
│  │  │  (Primary)    │    UPDATE payments SET status='CAPTURED' ...;     │       │
│  │  │               │    INSERT INTO ledger_entries (debit, credit);    │       │
│  │  │               │    UPDATE merchant_balances SET pending += ...;   │       │
│  │  │               │  COMMIT;                                          │       │
│  │  └──────────────┘                                                    │       │
│  │                                                                      │       │
│  │  Step 7: STORE IDEMPOTENCY RESPONSE                                  │       │
│  │  ┌──────────┐                                                        │       │
│  │  │  Redis   │  SET idempotency:merch_xyz:order_123_capture           │       │
│  │  │  Cluster │      {response} EX 172800                              │       │
│  │  └──────────┘                                                        │       │
│  │                                                                      │       │
│  │  Step 8: PUBLISH EVENT (async, after response sent)                  │       │
│  │  ┌──────────┐                                                        │       │
│  │  │  Kafka   │  Topic: payment.events                                 │       │
│  │  │          │  Key: pay_abc123                                        │       │
│  │  │          │  Value: {event_type: "payment.captured", ...}          │       │
│  │  └──────────┘                                                        │       │
│  │                                                                      │       │
│  └──────────────────────────────────────────────────────────────────────┘       │
│                                     │                                           │
│                                     │  HTTP 200                                 │
│                                     ▼                                           │
│  Client receives: { "status": "captured", "payment_id": "pay_abc123" }         │
│                                                                                 │
│  ════════════════════ ASYNC (after response) ═══════════════════════            │
│                                                                                 │
│  Kafka consumers process the event:                                             │
│                                                                                 │
│  ┌──────────────┐     ┌──────────────┐     ┌──────────────┐                    │
│  │  Webhook      │     │  Analytics   │     │  Reconcilia- │                    │
│  │  Service      │     │  Pipeline    │     │  tion Engine │                    │
│  │  → POST to    │     │  → CDC to    │     │  → Compare   │                    │
│  │    merchant's │     │    Snowflake │     │    internal  │                    │
│  │    endpoint   │     │              │     │    vs acquir.│                    │
│  └──────────────┘     └──────────────┘     └──────────────┘                    │
│                                                                                 │
└─────────────────────────────────────────────────────────────────────────────────┘
```

### Summary: Storage Components and Their Roles

```
┌──────────────────────────────────────────────────────────────────────────────┐
│                     STORAGE DECISION MATRIX                                  │
│                                                                              │
│  Question                              Answer → Store                       │
│  ─────────────────────────────         ───────────────────────────          │
│  "Is payment P captured?"              PostgreSQL (primary, strong)          │
│  "Has this idempotency key             Redis (fast) → PostgreSQL (fallback) │
│   been seen?"                                                                │
│  "What is card tok_abc's PAN?"         Token Vault (CDE, HSM-encrypted)     │
│  "What events happened to P?"          Kafka (event store, ordered)         │
│  "What was merchant X's GMV            Data Warehouse (Snowflake)           │
│   last month?"                                                               │
│  "What are merchant X's                Redis cache (TTL 5 min)              │
│   routing rules?"                                                            │
│  "Show dispute evidence for P"         S3 (document store)                  │
│  "What is the USD→EUR rate?"           Redis cache (TTL 1 min)              │
│  "What is Visa BIN 424242?"            Redis cache (TTL 24 hr)              │
└──────────────────────────────────────────────────────────────────────────────┘
```

---

## Verification Notes

The following claims were verified against known public sources:

- **Stripe uses PostgreSQL** -- confirmed via Stripe Engineering Blog posts, including "Online Migrations at Scale" which describes operating large PostgreSQL deployments.
- **PCI DSS CDE network segmentation** -- confirmed via PCI DSS v4.0 Requirements 1 and 2. The CDE must be isolated by firewall rules and network segmentation.
- **PCI DSS prohibits CVV storage after authorization** -- confirmed via PCI DSS Requirement 3.3.2 (v4.0).
- **Stripe.js reduces PCI scope** -- confirmed via Stripe documentation. Using Stripe.js/Elements qualifies merchants for SAQ A or SAQ A-EP.
- **AWS CloudHSM is FIPS 140-2 Level 3** -- confirmed via AWS CloudHSM documentation.
- **RBI data localization mandate** -- confirmed. RBI issued circular RBI/2017-18/153 in April 2018 requiring all payment system data to be stored in India.
- **Debezium for CDC** -- confirmed as a widely adopted open-source CDC platform by Red Hat.

The following claims are inferred or unverified:

- **[INFERRED]** Specific HSM vendors used by Stripe/Razorpay are not publicly documented. Thales Luna, Utimaco, and Futurex are common in the payments industry.
- **[UNVERIFIED]** Exact Kafka retention periods and specific DynamoDB transaction performance characteristics at payment scale need benchmarking against specific workloads.
- **[UNVERIFIED]** The 7-year retention figure is a common conservative choice. Specific retention requirements vary by jurisdiction and should be verified against local regulations.
- **[INFERRED]** Netflix's use of Cassandra and EVCache is well-documented in Netflix Tech Blog posts, but the exact comparison of write throughput characteristics vs. payment systems is an architectural inference, not a direct benchmark.

---

*Next: [09-reliability-and-observability.md](09-reliability-and-observability.md) — Reliability, SLAs & Observability*
