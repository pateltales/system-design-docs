# Payment System — Ledger & Double-Entry Bookkeeping Deep Dive

> Companion deep dive to the [interview simulation](01-interview-simulation.md). This document covers the ledger — the source of truth for all money movement in a payment system. Every cent must be accounted for.

---

## Table of Contents

1.  [Why a Ledger?](#1-why-a-ledger)
2.  [Double-Entry Bookkeeping Fundamentals](#2-double-entry-bookkeeping-fundamentals)
3.  [Account Model](#3-account-model)
4.  [Worked Examples with Ledger Entries](#4-worked-examples-with-ledger-entries)
5.  [Immutability — The Append-Only Invariant](#5-immutability--the-append-only-invariant)
6.  [Reconciliation](#6-reconciliation)
7.  [Currency Handling](#7-currency-handling)
8.  [Audit Trail](#8-audit-trail)
9.  [Ledger Database Schema](#9-ledger-database-schema)
10. [Contrast with General Accounting Software](#10-contrast-with-general-accounting-software)

---

## 1. Why a Ledger?

### 1.1 The Core Problem

A payment system moves money between parties. Without a rigorous bookkeeping
system, you face these failure modes:

```
Scenario: PSP processes 2 million transactions per day.
- 0.01% have discrepancies due to timeouts, partial failures, race conditions.
- That is 200 transactions/day with unexplained money movement.
- Over a month: 6,000 unresolved discrepancies.
- Over a year: 72,000 records where money went somewhere but you are not sure where.
```

Without a ledger, debugging these discrepancies means scanning application logs,
correlating timestamps across systems, and guessing. With a ledger, every cent is
accounted for at all times, and discrepancies are self-evident: the books do not
balance.

### 1.2 The Ledger is the Source of Truth

The payment state machine (CREATED -> AUTHORIZED -> CAPTURED -> SETTLED) tells you
**what happened**. The ledger tells you **where the money is**. These are different
questions:

```
Payment state machine:  "Payment #1234 was captured"
Ledger:                 "Payment #1234 moved $97.10 to Merchant X's balance
                         and $2.90 to PSP fee revenue"
```

If the payment state says "captured" but the ledger has no corresponding entries,
something is broken. If the ledger shows entries but the payment state is still
"authorized," something is broken. The two must be atomically consistent — more on
this in [Section 9](#9-ledger-database-schema).

---

## 2. Double-Entry Bookkeeping Fundamentals

### 2.1 The Core Principle

Double-entry bookkeeping has been the foundation of financial record-keeping since
Luca Pacioli formalized it in 1494. The principle:

> **Every financial event creates exactly two entries — a debit and a credit of
> equal amount.**

This means every movement of money is recorded from both sides: where it came from
(credit) and where it went to (debit).

### 2.2 The Fundamental Invariant

```
At all times:
    SUM(all debits) = SUM(all credits)
```

This is the invariant. If it ever fails, there is a bug. No exceptions, no
rounding tolerance, no "close enough." The books MUST balance. This makes errors
**detectable** — you may not immediately know what went wrong, but you will know
that something went wrong.

### 2.3 Debit vs Credit — Clearing Up Confusion

In accounting, "debit" and "credit" do NOT mean "subtract" and "add." Their
effect depends on the account type:

```
Account Type      | Debit Effect   | Credit Effect
------------------|----------------|----------------
Asset             | Increase (+)   | Decrease (-)
Liability         | Decrease (-)   | Increase (+)
Revenue           | Decrease (-)   | Increase (+)
Expense           | Increase (+)   | Decrease (-)
```

In a payment system context:

- **Merchant balance accounts** are liabilities (money the PSP owes to merchants).
  A credit increases the balance; a debit decreases it.
- **PSP fee accounts** are revenue. A credit increases fee revenue.
- **External accounts** (customer's card, bank accounts) represent the outside
  world. We do not control them, but we record our side of the transaction.

### 2.4 Why Double-Entry and Not Single-Entry?

Single-entry bookkeeping just records each transaction once (like a bank statement).
The problem:

```
Single-entry:
  "Payment #1234: +$100 to merchant account"
  Question: Where did the $100 come from? Silence.

  "Refund #5678: -$100 from merchant account"
  Question: Did we actually send $100 back to the customer? Silence.
```

Double-entry forces you to answer both questions. You cannot record money arriving
somewhere without also recording where it left. This makes the system
**self-auditing** — errors violate the invariant and are immediately detectable.

---

## 3. Account Model

### 3.1 Internal Account Hierarchy

A payment system maintains a chart of accounts. These are NOT bank accounts — they
are internal bookkeeping accounts that track the flow of money within the system.

```
                          +---------------------+
                          |   Chart of Accounts  |
                          +----------+----------+
                                     |
              +----------------------+----------------------+
              |                      |                      |
     +--------v--------+   +--------v--------+   +--------v--------+
     |   Asset Accounts |   | Liability Accts  |   | Revenue Accounts|
     +--------+--------+   +--------+--------+   +--------+--------+
              |                      |                      |
   +----------+------+     +--------+--------+    +-------+-------+
   |          |      |     |        |        |    |       |       |
   v          v      v     v        v        v    v       v       v
 PSP       Settle-  Charge- Merch.  Merch.  Refund PSP    Charge-  Tax
 Bank      ment     back   Balance Pending Reserve Fee    back    Collected
 Account   Account  Recv.  (per    (per    (per   Revenue Fee Rev.
                    able   merch.) merch.) merch.)
```

### 3.2 Account Descriptions

| Account | Type | Purpose |
|---------|------|---------|
| **PSP Bank Account** | Asset | The PSP's actual pooled bank account. Represents real money held. |
| **Settlement Account** | Asset | Tracks funds in transit during settlement (between capture and payout). |
| **Chargeback Receivable** | Asset | Funds expected back from a reversed chargeback (dispute won). |
| **Merchant Balance** (per merchant) | Liability | Money the PSP owes to the merchant. Available for payout. |
| **Merchant Pending** (per merchant) | Liability | Captured funds not yet available for payout (in settlement window). |
| **Refund Reserve** (per merchant) | Liability | Funds withheld to cover potential refunds/chargebacks. |
| **PSP Fee Revenue** | Revenue | Processing fees earned by the PSP (e.g., 2.9% + $0.30). |
| **Chargeback Fee Revenue** | Revenue | Fees charged to merchants for chargebacks (typically $15-$25 per chargeback). |
| **Tax Collected** | Liability | Sales tax/GST collected and owed to tax authorities. |

### 3.3 Per-Merchant Accounts

Each merchant gets their own set of accounts:

```
Merchant "Acme Corp" (ID: m_001):
  +-- acct:m_001:balance          (Liability -- available funds)
  +-- acct:m_001:pending          (Liability -- funds in settlement window)
  +-- acct:m_001:refund_reserve   (Liability -- withheld for refunds)

Merchant "Bob's Shop" (ID: m_002):
  +-- acct:m_002:balance          (Liability -- available funds)
  +-- acct:m_002:pending          (Liability -- funds in settlement window)
  +-- acct:m_002:refund_reserve   (Liability -- withheld for refunds)
```

Why per-merchant? Because each merchant's funds must be tracked independently.
Commingling funds without proper bookkeeping is a regulatory violation in most
jurisdictions. The PSP holds money **on behalf of** merchants in a pooled bank
account, but the ledger must track exactly how much belongs to whom.

---

## 4. Worked Examples with Ledger Entries

### 4.1 Card Payment Capture — $100.00 with 2.9% + $0.30 Fee

A customer pays $100.00 to Merchant Acme Corp. The PSP charges 2.9% + $0.30.

```
Fee calculation:
  Gross amount:  $100.00
  PSP fee:       ($100.00 x 0.029) + $0.30 = $2.90 + $0.30 = $3.20
  Net to merchant: $100.00 - $3.20 = $96.80
```

**Ledger entries (at capture time):**

| # | Debit Account | Credit Account | Amount | Reference |
|---|---------------|----------------|--------|-----------|
| 1 | PSP Bank Account | Merchant Pending (Acme) | $96.80 | pay_1234 |
| 2 | PSP Bank Account | PSP Fee Revenue | $3.20 | pay_1234 |

Entry 1: The PSP's bank account (asset) increases by $96.80, and the PSP now
owes $96.80 to the merchant (liability increases).

Entry 2: The PSP's bank account (asset) increases by $3.20, and the PSP
recognizes $3.20 in fee revenue.

Total debits: $96.80 + $3.20 = $100.00
Total credits: $96.80 + $3.20 = $100.00  (BALANCED)

**T-Account Diagram:**

```
    PSP Bank Account (Asset)        Merchant Pending - Acme (Liability)
  +----------+----------+        +----------+----------+
  |  Debit   |  Credit  |        |  Debit   |  Credit  |
  +----------+----------+        +----------+----------+
  |  $96.80  |          |        |          |  $96.80  |
  |   $3.20  |          |        |          |          |
  +----------+----------+        +----------+----------+
  | Bal:     |          |        |          |    Bal:  |
  | $100.00  |          |        |          |  $96.80  |
  +----------+----------+        +----------+----------+

    PSP Fee Revenue (Revenue)
  +----------+----------+
  |  Debit   |  Credit  |
  +----------+----------+
  |          |   $3.20  |
  +----------+----------+
  |          |    Bal:  |
  |          |   $3.20  |
  +----------+----------+
```

**Note:** Some PSPs record the full $100.00 as a single entry to the merchant
pending account first, then separately record the fee extraction. The end result
is the same — the approach above collapses it into fewer entries. Both are valid.
[INFERRED — specific PSP implementations vary]

### 4.2 Settlement — Moving from Pending to Available

After the settlement window (e.g., T+2 days), the merchant's funds move from
"pending" to "balance" (available for payout).

**Ledger entries:**

| # | Debit Account | Credit Account | Amount | Reference |
|---|---------------|----------------|--------|-----------|
| 1 | Merchant Pending (Acme) | Merchant Balance (Acme) | $96.80 | settle_5678 |

This is a reclassification — the money has not moved in the real world, but the
merchant can now request a payout.

**T-Account Diagram:**

```
  Merchant Pending - Acme (Liability)    Merchant Balance - Acme (Liability)
  +----------+----------+              +----------+----------+
  |  Debit   |  Credit  |              |  Debit   |  Credit  |
  +----------+----------+              +----------+----------+
  |          |  $96.80  | <- capture   |          |          |
  |  $96.80  |          | <- settle    |          |  $96.80  | <- settle
  +----------+----------+              +----------+----------+
  |    Bal:  |          |              |          |    Bal:  |
  |   $0.00  |          |              |          |  $96.80  |
  +----------+----------+              +----------+----------+
```

### 4.3 Payout to Merchant — $96.80

The merchant requests a payout of their available balance.

**Ledger entries:**

| # | Debit Account | Credit Account | Amount | Reference |
|---|---------------|----------------|--------|-----------|
| 1 | Merchant Balance (Acme) | PSP Bank Account | $96.80 | payout_9012 |

The PSP's liability to the merchant decreases (debit to liability), and the
PSP's bank account decreases (credit to asset) — real money leaves the system.

**T-Account Diagram:**

```
  Merchant Balance - Acme (Liability)    PSP Bank Account (Asset)
  +----------+----------+              +----------+----------+
  |  Debit   |  Credit  |              |  Debit   |  Credit  |
  +----------+----------+              +----------+----------+
  |          |  $96.80  | <- settle    | $100.00  |          | <- capture
  |  $96.80  |          | <- payout    |          |  $96.80  | <- payout
  +----------+----------+              +----------+----------+
  |    Bal:  |          |              |   Bal:   |          |
  |   $0.00  |          |              |   $3.20  |          |
  +----------+----------+              +----------+----------+
```

After the full cycle: the PSP's bank account retains $3.20 (the fee revenue).
The merchant received $96.80 in their real bank account. The books balance.

### 4.4 Partial Refund — $30.00

The customer requests a $30.00 refund on the original $100.00 payment.

**Fee handling on refunds:** Policies vary by PSP. Common approaches:
- **Stripe:** Does NOT refund the processing fee on refunds. The merchant absorbs
  the original fee. [This is documented in Stripe's pricing page]
- **Some PSPs:** Refund the proportional fee. [INFERRED]

We will show both approaches.

#### Approach A: Fee NOT refunded (Stripe's model)

```
Refund amount to customer: $30.00
Fee refunded to merchant:  $0.00 (merchant absorbs original fee)
Merchant balance impact:   -$30.00
```

**Ledger entries:**

| # | Debit Account | Credit Account | Amount | Reference |
|---|---------------|----------------|--------|-----------|
| 1 | Merchant Balance (Acme) | PSP Bank Account | $30.00 | refund_3456 |

The PSP owes the merchant $30.00 less (liability decreases), and $30.00 leaves
the PSP's bank to go back to the customer's card.

**T-Account Diagram:**

```
  Merchant Balance - Acme (Liability)    PSP Bank Account (Asset)
  +----------+----------+              +----------+----------+
  |  Debit   |  Credit  |              |  Debit   |  Credit  |
  +----------+----------+              +----------+----------+
  |          |  $96.80  | <- capture   | $100.00  |          | <- capture
  |  $30.00  |          | <- refund    |          |  $30.00  | <- refund
  +----------+----------+              +----------+----------+
  |          |    Bal:  |              |   Bal:   |          |
  |          |  $66.80  |              |  $70.00  |          |
  +----------+----------+              +----------+----------+
```

After the refund, the PSP's bank holds $70.00: $66.80 owed to the merchant +
$3.20 in fee revenue. Books balance.

#### Approach B: Proportional fee refund

```
Refund amount to customer:   $30.00
Proportional fee refund:     $30.00 / $100.00 x $3.20 = $0.96
Merchant balance impact:     -($30.00 - $0.96) = -$29.04
```

**Ledger entries:**

| # | Debit Account | Credit Account | Amount | Reference |
|---|---------------|----------------|--------|-----------|
| 1 | Merchant Balance (Acme) | PSP Bank Account | $29.04 | refund_3456 |
| 2 | PSP Fee Revenue | PSP Bank Account | $0.96 | refund_3456 |

Entry 2 is a fee reversal — the PSP gives back the proportional fee.

### 4.5 Chargeback — $100.00

A cardholder disputes the original $100.00 charge. The card network issues a
chargeback. The PSP is debited by the acquirer, and the PSP must recover the
funds from the merchant.

```
Chargeback amount:        $100.00
Chargeback fee to merchant: $15.00  [typical range: $15-$25 per chargeback]
```

**Ledger entries:**

| # | Debit Account | Credit Account | Amount | Reference |
|---|---------------|----------------|--------|-----------|
| 1 | Merchant Balance (Acme) | PSP Bank Account | $100.00 | chargeback_7890 |
| 2 | Merchant Balance (Acme) | Chargeback Fee Revenue | $15.00 | chargeback_7890 |

Entry 1: The PSP returns $100.00 to the card network (via the acquirer). The
merchant's balance is reduced by the full original amount.

Entry 2: The PSP charges the merchant a $15.00 chargeback fee.

**T-Account Diagram:**

```
  Merchant Balance - Acme (Liability)
  +----------+----------+
  |  Debit   |  Credit  |
  +----------+----------+
  |          |  $96.80  | <- original capture (net)
  | $100.00  |          | <- chargeback (gross amount returned)
  |  $15.00  |          | <- chargeback fee
  +----------+----------+
  |    Bal:  |          |
  |  -$18.20 |          |  <- Merchant now OWES the PSP $18.20!
  +----------+----------+

  PSP Bank Account (Asset)              Chargeback Fee Revenue (Revenue)
  +----------+----------+              +----------+----------+
  |  Debit   |  Credit  |              |  Debit   |  Credit  |
  +----------+----------+              +----------+----------+
  | $100.00  |          | <- capture   |          |  $15.00  |
  |          | $100.00  | <- chargeback|          |          |
  +----------+----------+              +----------+----------+
  |   Bal:   |          |              |          |    Bal:  |
  |   $0.00  |          |              |          |  $15.00  |
  +----------+----------+              +----------+----------+
```

**Key observation:** The merchant's balance went negative (-$18.20). This happens
because the chargeback reverses the gross amount ($100.00) while the original
capture only credited the net amount ($96.80) — plus the $15.00 chargeback fee.
The PSP must recover $18.20 from the merchant. This is why PSPs maintain
**refund reserves** — funds withheld from merchants to cover exactly this
scenario. High-chargeback merchants may have larger reserves.

### 4.6 Complete Lifecycle — All Entries Together

For a single $100.00 payment through its full lifecycle (capture -> settle ->
payout), here is the complete ledger:

```
Entry  Debit Account              Credit Account              Amount    Event
-----  -------------------------  --------------------------  --------  ----------
  1    PSP Bank Account           Merchant Pending (Acme)     $96.80   Capture
  2    PSP Bank Account           PSP Fee Revenue             $3.20    Capture
  3    Merchant Pending (Acme)    Merchant Balance (Acme)     $96.80   Settlement
  4    Merchant Balance (Acme)    PSP Bank Account            $96.80   Payout

Verification:
  Total debits:  $96.80 + $3.20 + $96.80 + $96.80 = $293.60
  Total credits: $96.80 + $3.20 + $96.80 + $96.80 = $293.60  BALANCED

Final account balances:
  PSP Bank Account:           +$100.00 - $96.80 = $3.20  (the PSP's fee)
  Merchant Pending (Acme):    +$96.80 - $96.80  = $0.00
  Merchant Balance (Acme):    +$96.80 - $96.80  = $0.00  (paid out)
  PSP Fee Revenue:            +$3.20             = $3.20  (PSP's earnings)
```

---

## 5. Immutability — The Append-Only Invariant

### 5.1 The Rule

```
Ledger entries are APPEND-ONLY.
  - You NEVER update an existing entry.
  - You NEVER delete an existing entry.
  - To correct an error, you add a NEW compensating entry.
```

This is not a design preference — it is a hard requirement for financial systems.

### 5.2 Why Immutability is Critical

**1. Audit trail completeness.** Regulators (PCI DSS, SOX, RBI for India) require
a complete history of all financial events. If you can modify past entries, you can
hide fraud. An append-only log makes it impossible to alter history without
detection.

**2. Debugging and forensics.** When something goes wrong (and it will), you need
to reconstruct exactly what happened, in what order, at what time. Mutable records
destroy this ability.

**3. Concurrent access safety.** If ledger entries are immutable, they never need
write locks for updates. Multiple processes can read historical entries without
contention.

**4. Reconciliation integrity.** Reconciliation compares your ledger against
external statements. If your ledger entries can change after the fact, a
reconciliation that passed yesterday might not pass today — making the entire
process unreliable.

### 5.3 Compensating Entries — How to Fix Mistakes

Suppose a payment was accidentally recorded with the wrong amount ($1,000.00
instead of $100.00):

```
WRONG approach (mutating):
  UPDATE ledger_entries SET amount = 10000 WHERE id = 42;
  -- History is destroyed. No trace of the error.

CORRECT approach (compensating entries):
  -- Original (incorrect) entry remains:
  Entry #42:  Debit PSP Bank    Credit Merchant Pending    $1,000.00

  -- Add compensating entry to reverse:
  Entry #99:  Debit Merchant Pending    Credit PSP Bank    $1,000.00
              (reason: "Correction -- reversal of entry #42, incorrect amount")

  -- Add correct entry:
  Entry #100: Debit PSP Bank    Credit Merchant Pending    $100.00
              (reason: "Correction -- correct amount for payment pay_5678")
```

Now the ledger shows: the error, the reversal, and the correction. A complete
paper trail. An auditor can see exactly what happened and when.

### 5.4 Soft Deletes vs Hard Deletes

In most software systems, "soft delete" (setting a `deleted_at` flag) is fine.
In a financial ledger, even soft deletes are wrong. A "deleted" entry might still
be referenced by a reconciliation report, an audit, or a dispute investigation.
The entry must remain visible and intact. Corrections are always done through
compensating entries.

---

## 6. Reconciliation

### 6.1 What is Reconciliation?

Reconciliation is the process of comparing multiple independent records of the same
financial events to detect discrepancies. If your ledger says you processed
$1,000,000 today, and the acquirer's statement says $999,500, there is a $500
discrepancy that must be investigated and resolved.

### 6.2 Three-Way Reconciliation

A payment system performs **three-way reconciliation** across three independent
sources of truth:

```
+------------------------------+
|     SOURCE 1: Internal       |
|     Ledger (PSP's DB)        |
|                              |
|  Every payment, refund,      |
|  chargeback, payout --       |
|  recorded in real-time       |
+--------------+---------------+
               |
               v
     +-----------------+
     |  RECONCILIATION |
     |     ENGINE      |<---------- Runs daily/hourly
     +-----------------+
               ^         ^
               |         |
+--------------+---+ +---+----------------------+
|  SOURCE 2:       | |  SOURCE 3:               |
|  Acquirer /      | |  Card Network            |
|  Bank Statements | |  Settlement Files        |
|                  | |                           |
|  End-of-day or   | |  Visa/Mastercard          |
|  real-time feed  | |  daily settlement         |
|  from acquiring  | |  reports (TC33/IPM)       |
|  bank            | |                           |
+------------------+ +---------------------------+
```

### 6.3 Reconciliation Flow

```
Step 1: EXTRACT
  - Pull all ledger entries for the reconciliation period (e.g., yesterday)
  - Download acquirer settlement file (often SFTP or API)
  - Download card network settlement file (Visa TC33, Mastercard IPM)

Step 2: NORMALIZE
  - Map each source to a common schema:
    {transaction_id, amount, currency, type, timestamp, status}
  - Handle format differences (amount in cents vs dollars, date formats)

Step 3: MATCH
  - For each internal transaction, find the matching record in acquirer
    and card network files.
  - Match key: typically authorization code + amount + date + card last 4

Step 4: CLASSIFY RESULTS
  +--------------------+-------------------------------------------------+
  | Category           | Meaning                                         |
  +--------------------+-------------------------------------------------+
  | Matched            | All three sources agree -- no action needed      |
  | Amount mismatch    | Same txn found in all sources, amounts differ   |
  | Missing in acquirer| Internal ledger has it, acquirer file does not  |
  | Missing internal   | Acquirer file has it, internal ledger does not  |
  | Timing difference  | Txn in one file today, in another file tomorrow |
  | Fee discrepancy    | Fee amounts differ between sources              |
  +--------------------+-------------------------------------------------+

Step 5: RESOLVE
  - Timing differences: Auto-resolve by carrying forward to next day's recon
  - Amount mismatches: Flag for manual review
  - Missing records: Escalate -- potential data loss or system bug
  - Fee discrepancies: Compare against fee schedule, escalate if outside tolerance
```

### 6.4 Common Discrepancy Types

**Timing differences** are the most common and least alarming:

```
Example:
  Your system:    Payment authorized at 11:58 PM on Jan 15
  Acquirer file:  Settlement file cut-off at 11:00 PM -- this payment
                  appears in the Jan 16 file instead
  Resolution:     Auto-resolves when Jan 16 recon runs
```

**Currency conversion differences:**

```
Example:
  Your system:    EUR 100.00 captured, converted at 1.0850 = USD 108.50
  Acquirer file:  EUR 100.00 settled at 1.0847 = USD 108.47
  Discrepancy:    $0.03 -- due to different exchange rate snapshots
  Resolution:     Accept within tolerance (e.g., 0.1%), log the difference
```

**Fee calculation differences:**

```
Example:
  Your system:    Fee = 2.9% x $100 + $0.30 = $3.20
  Acquirer file:  Fee = $3.25 (acquirer applied different rate for
                  international card, which has higher interchange)
  Resolution:     Review fee schedule. Update routing rules if needed.
```

### 6.5 Reconciliation at Scale

```
At Stripe-like scale:
  - Millions of transactions per day
  - Dozens of acquirers, each with different file formats
  - Multiple card networks (Visa, Mastercard, Amex, etc.)
  - Multiple currencies
  - Reconciliation must complete within hours, not days

  A manual reconciliation process is impossible at this scale.
  The reconciliation engine must be automated, with human review
  only for flagged discrepancies.
```

[INFERRED — specific reconciliation SLAs at major PSPs are not publicly documented]

---

## 7. Currency Handling

### 7.1 The Cardinal Rule: Never Use Floats for Money

```
WRONG -- floating point:
  double price = 0.1 + 0.2;
  // price = 0.30000000000000004  (IEEE 754 floating point)

  After 1 million transactions with $0.01 rounding errors:
  Cumulative error: up to $10,000  (unacceptable)
```

This is not a theoretical problem. Floating-point representation cannot exactly
represent most decimal fractions. In a system processing millions of transactions,
these tiny errors compound into significant discrepancies.

### 7.2 Store in Smallest Currency Unit as Integers

```
CORRECT:
  $100.00  ->  store as 10000 (cents, integer)
  EUR 49.99 -> store as 4999  (cents, integer)
  JPY 1000  -> store as 1000  (yen has no subunit -- already smallest unit)
  BHD 5.123 -> store as 5123  (Bahraini dinar has 3 decimal places -- fils)
```

**Why integers?** Integer arithmetic is exact. `10000 + 5000 = 15000` always.
No rounding, no precision loss, no surprises.

### 7.3 Currency Exponent Table

Different currencies have different numbers of decimal places:

```
Currency    Code   Exponent   Smallest Unit   $1 equivalent
----------  -----  --------   -------------   -------------
US Dollar   USD    2          cent            100
Euro        EUR    2          cent            100
Jap. Yen    JPY    0          yen             1
Brit. Pound GBP    2          penny           100
Indian Rup  INR    2          paisa           100
Bahrain Din BHD    3          fils            1000
```

The exponent tells you how to convert: `display_amount = stored_amount / 10^exponent`.

You MUST store the currency code alongside every amount. `10000` means nothing
without knowing it is `10000 USD cents` ($100.00) vs `10000 JPY` (10,000 yen,
roughly $67).

### 7.4 Multi-Currency Transactions

When a US customer pays a European merchant in EUR:

```
Transaction:
  Customer pays:       USD 108.50
  Merchant receives:   EUR 100.00
  Exchange rate:       1 EUR = 1.0850 USD (at transaction time)

Ledger entries (in both currencies):
  Debit:  PSP Bank Account (USD)         10850  (USD cents)
  Credit: Merchant Pending (Acme, EUR)   10000  (EUR cents)
  Metadata: exchange_rate = 1.0850, rate_source = "ECB", rate_timestamp = "..."

Key: Record the exchange rate at transaction time. Do NOT re-derive it later.
     Exchange rates change every second -- you must capture the exact rate used.
```

### 7.5 Foreign Exchange (FX) Gains and Losses

Between capture and settlement, the exchange rate may change. This creates FX
gains or losses for the PSP:

```
Capture:  1 EUR = 1.0850 USD  -> PSP received $108.50 for EUR 100.00
Settle:   1 EUR = 1.0900 USD  -> PSP needs $109.00 to pay EUR 100.00

FX loss for PSP: $0.50

This is recorded as a separate ledger entry:
  Debit:  FX Loss (Expense)         50  (USD cents)
  Credit: PSP Bank Account (USD)    50  (USD cents)
```

[INFERRED — specific FX handling varies by PSP. Some PSPs hedge FX risk, others
pass it through to merchants via dynamic currency conversion (DCC).]

---

## 8. Audit Trail

### 8.1 What Gets Logged

Every action that affects financial state must be logged:

```
Category               Examples
---------------------  ------------------------------------------
Payment state changes  CREATED -> AUTHORIZED -> CAPTURED -> SETTLED
Ledger entries         Every debit/credit entry (immutable by design)
Refund events          Refund requested, refund processed, refund failed
Chargeback events      Dispute opened, evidence submitted, dispute won/lost
Payout events          Payout initiated, payout completed, payout failed
Admin actions          Fee schedule changed, routing rule modified
Config changes         Risk threshold updated, acquirer enabled/disabled
Access events          API key created, dashboard login, PII accessed
```

### 8.2 Audit Log Schema

```sql
CREATE TABLE audit_log (
    id              BIGSERIAL PRIMARY KEY,
    event_type      VARCHAR(100)  NOT NULL,   -- 'PAYMENT_CAPTURED', 'REFUND_INITIATED', etc.
    entity_type     VARCHAR(50)   NOT NULL,   -- 'payment', 'refund', 'payout', 'config'
    entity_id       VARCHAR(100)  NOT NULL,   -- 'pay_1234', 'ref_5678'
    actor_type      VARCHAR(50)   NOT NULL,   -- 'system', 'merchant_api', 'admin_user'
    actor_id        VARCHAR(100)  NOT NULL,   -- 'system:settlement_engine', 'admin:jane@psp.com'
    action          VARCHAR(100)  NOT NULL,   -- 'state_transition', 'ledger_entry', 'config_change'
    old_state       JSONB,                     -- previous state (for state transitions)
    new_state       JSONB,                     -- new state
    reason          TEXT,                      -- human-readable reason
    ip_address      INET,                      -- for API/admin actions
    created_at      TIMESTAMPTZ   NOT NULL DEFAULT NOW()

    -- Append-only: no UPDATE or DELETE triggers allowed on this table
    -- Partitioned by created_at for retention management
);

CREATE INDEX idx_audit_entity ON audit_log (entity_type, entity_id, created_at);
CREATE INDEX idx_audit_actor  ON audit_log (actor_type, actor_id, created_at);
```

### 8.3 Compliance Requirements

**PCI DSS (Payment Card Industry Data Security Standard):**
- Requirement 10: "Track and monitor all access to network resources and
  cardholder data."
- All access to cardholder data must be logged with individual user attribution.
- Logs must be retained for at least 1 year, with 3 months immediately available.
- Logs must be tamper-evident (hash chaining or write-once storage).

**SOX (Sarbanes-Oxley Act):**
- Applies to publicly traded companies.
- Requires internal controls over financial reporting.
- Audit trail must demonstrate that financial records are accurate and complete.
- Changes to financial systems require change management documentation.

**RBI (Reserve Bank of India) — for Indian PSPs like Razorpay:**
- Mandate reconciliation of all transactions within T+1 day.
- Settlement to merchants within T+1 or T+2 depending on risk category.
- Complete audit trail for all fund movements.

[INFERRED — specific regulatory timelines may vary. The PCI DSS log retention
requirement of 1 year with 3 months immediate availability is from PCI DSS v3.2.1
Requirement 10.7.]

### 8.4 Tamper-Evidence

For high-security environments, the audit log can be made tamper-evident using
hash chaining:

```
Entry 1: hash_1 = SHA-256(entry_1_data)
Entry 2: hash_2 = SHA-256(entry_2_data + hash_1)
Entry 3: hash_3 = SHA-256(entry_3_data + hash_2)
...

If anyone modifies entry 2, hash_2 changes, which invalidates hash_3 and
every subsequent hash -- the tampering is immediately detectable.
```

This is the same principle as a blockchain, but without the distributed consensus
overhead. A centralized, append-only, hash-chained log is sufficient for most
payment systems. [INFERRED — not all PSPs implement hash chaining; many rely on
database-level access controls and write-once storage instead.]

---

## 9. Ledger Database Schema

### 9.1 Core Tables

```sql
-- ============================================================
-- ACCOUNTS TABLE
-- Each row represents a bookkeeping account (NOT a bank account)
-- ============================================================
CREATE TABLE accounts (
    id              VARCHAR(50)   PRIMARY KEY,  -- 'acct:m_001:balance', 'acct:psp:fee_revenue'
    account_type    VARCHAR(20)   NOT NULL,     -- 'ASSET', 'LIABILITY', 'REVENUE', 'EXPENSE'
    name            VARCHAR(200)  NOT NULL,     -- 'Merchant Balance - Acme Corp'
    currency        CHAR(3)       NOT NULL,     -- 'USD', 'EUR', 'INR'
    merchant_id     VARCHAR(50),                -- NULL for PSP-level accounts
    is_active       BOOLEAN       NOT NULL DEFAULT TRUE,
    created_at      TIMESTAMPTZ   NOT NULL DEFAULT NOW(),

    -- Per-account running balance for fast queries
    -- Updated atomically with each ledger entry (see trigger below)
    current_balance BIGINT        NOT NULL DEFAULT 0,  -- in smallest currency unit

    CONSTRAINT chk_account_type CHECK (
        account_type IN ('ASSET','LIABILITY','REVENUE','EXPENSE')
    )
);

CREATE INDEX idx_accounts_merchant ON accounts (merchant_id)
    WHERE merchant_id IS NOT NULL;


-- ============================================================
-- LEDGER ENTRIES TABLE
-- The core double-entry table. Each row represents a balanced
-- debit-credit pair. Every financial event creates one or more rows.
-- ============================================================
CREATE TABLE ledger_entries (
    id              BIGSERIAL     PRIMARY KEY,
    transaction_id  UUID          NOT NULL,     -- groups related entries
    debit_account   VARCHAR(50)   NOT NULL REFERENCES accounts(id),
    credit_account  VARCHAR(50)   NOT NULL REFERENCES accounts(id),
    amount          BIGINT        NOT NULL,     -- smallest currency unit, always positive
    currency        CHAR(3)       NOT NULL,     -- 'USD', 'EUR'
    reference_type  VARCHAR(50)   NOT NULL,     -- 'PAYMENT', 'REFUND', 'CHARGEBACK', 'PAYOUT', 'FEE'
    reference_id    VARCHAR(100)  NOT NULL,     -- 'pay_1234', 'ref_5678'
    description     TEXT,                       -- human-readable description
    created_at      TIMESTAMPTZ   NOT NULL DEFAULT NOW(),

    -- Immutability enforced: no UPDATE/DELETE allowed (enforced via DB trigger)
    CONSTRAINT chk_positive_amount CHECK (amount > 0),
    CONSTRAINT chk_different_accounts CHECK (debit_account <> credit_account)
);

CREATE INDEX idx_ledger_reference ON ledger_entries (reference_type, reference_id);
CREATE INDEX idx_ledger_debit     ON ledger_entries (debit_account, created_at);
CREATE INDEX idx_ledger_credit    ON ledger_entries (credit_account, created_at);
CREATE INDEX idx_ledger_txn       ON ledger_entries (transaction_id);


-- ============================================================
-- PAYMENTS TABLE (simplified -- payment state machine)
-- Lives in the SAME database as ledger_entries for ACID guarantee
-- ============================================================
CREATE TABLE payments (
    id              VARCHAR(50)   PRIMARY KEY,  -- 'pay_1234'
    merchant_id     VARCHAR(50)   NOT NULL,
    amount          BIGINT        NOT NULL,     -- gross amount in smallest unit
    currency        CHAR(3)       NOT NULL,
    status          VARCHAR(20)   NOT NULL,     -- 'CREATED','AUTHORIZED','CAPTURED','SETTLED'
    idempotency_key VARCHAR(100)  NOT NULL,
    payment_method  VARCHAR(20)   NOT NULL,     -- 'CARD', 'UPI', 'BANK_TRANSFER'
    created_at      TIMESTAMPTZ   NOT NULL DEFAULT NOW(),
    updated_at      TIMESTAMPTZ   NOT NULL DEFAULT NOW(),

    CONSTRAINT uq_idempotency UNIQUE (merchant_id, idempotency_key)
);
```

### 9.2 Alternative: Separate Debit/Credit Rows

Some systems store each side as a separate row instead of a paired entry:

```sql
-- Alternative design: one row per side
CREATE TABLE ledger_lines (
    id              BIGSERIAL     PRIMARY KEY,
    transaction_id  UUID          NOT NULL,     -- groups related lines
    account_id      VARCHAR(50)   NOT NULL REFERENCES accounts(id),
    entry_type      VARCHAR(6)    NOT NULL,     -- 'DEBIT' or 'CREDIT'
    amount          BIGINT        NOT NULL,     -- always positive
    currency        CHAR(3)       NOT NULL,
    reference_type  VARCHAR(50)   NOT NULL,
    reference_id    VARCHAR(100)  NOT NULL,
    created_at      TIMESTAMPTZ   NOT NULL DEFAULT NOW()
);

-- Invariant check: for every transaction_id, sum of DEBITs = sum of CREDITs
-- Enforced by application layer or database trigger
```

The first design (debit_account + credit_account in one row) is simpler and
guarantees balance by construction — each row is inherently balanced. The second
design (separate rows) is more flexible for multi-leg transactions (e.g., a
payment that touches 4 accounts creates 4 rows instead of 2 paired rows).

[INFERRED — Stripe's internal ledger design is not publicly documented. The
paired-entry model is common in payment systems based on public engineering blog
posts from companies like Square and Modern Treasury.]

### 9.3 Why Ledger and Payment State Must Share a Database

This is critical. The payment status update and ledger entry creation MUST be in
the same ACID transaction:

```sql
BEGIN;
  -- Step 1: Update payment status
  UPDATE payments SET status = 'CAPTURED', updated_at = NOW()
    WHERE id = 'pay_1234' AND status = 'AUTHORIZED';

  -- Step 2: Create ledger entries (in the SAME transaction)
  INSERT INTO ledger_entries (transaction_id, debit_account, credit_account,
                              amount, currency, reference_type, reference_id)
  VALUES
    (gen_random_uuid(), 'acct:psp:bank', 'acct:m_001:pending',
     9680, 'USD', 'PAYMENT', 'pay_1234'),
    (gen_random_uuid(), 'acct:psp:bank', 'acct:psp:fee_revenue',
     320, 'USD', 'FEE', 'pay_1234');

  -- Step 3: Update account balances
  UPDATE accounts SET current_balance = current_balance + 10000
    WHERE id = 'acct:psp:bank';
  UPDATE accounts SET current_balance = current_balance + 9680
    WHERE id = 'acct:m_001:pending';
  UPDATE accounts SET current_balance = current_balance + 320
    WHERE id = 'acct:psp:fee_revenue';
COMMIT;
```

If these were in separate databases:

```
FAILURE SCENARIO (separate DBs):
  1. Payment DB: UPDATE status = 'CAPTURED'    -- committed
  2. Ledger DB: INSERT ledger entries           -- network timeout!

  Result: Payment says "captured" but no ledger entries exist.
  The money is unaccounted for. The books do not balance.
  You now need a reconciliation process to detect and fix this,
  and until it runs, your financial records are wrong.
```

By keeping them in the same PostgreSQL database, the ACID guarantee ensures
both succeed or both fail. This is a deliberate architectural choice that
trades microservice purity for financial correctness.

### 9.4 Balance Query — Real-Time Merchant Balance

```sql
-- Fast balance query using pre-computed current_balance:
SELECT current_balance
FROM accounts
WHERE id = 'acct:m_001:balance';

-- Verification query (derive from ledger -- slower but authoritative):
SELECT
  COALESCE(SUM(CASE WHEN debit_account = 'acct:m_001:balance'
                     THEN amount ELSE 0 END), 0)
  -
  COALESCE(SUM(CASE WHEN credit_account = 'acct:m_001:balance'
                     THEN amount ELSE 0 END), 0)
  AS derived_balance
FROM ledger_entries
WHERE debit_account = 'acct:m_001:balance'
   OR credit_account = 'acct:m_001:balance';

-- If current_balance != derived_balance, something is wrong.
-- Run this as a periodic consistency check.
```

Note on account types and balance semantics: For liability accounts (like merchant
balance), a positive `current_balance` means the PSP owes the merchant that
amount. The sign convention depends on your design — some systems use signed
amounts in ledger entries (positive for debit, negative for credit), others use
unsigned amounts with explicit debit/credit columns. The schema above uses
unsigned amounts with explicit columns.

### 9.5 Immutability Enforcement

```sql
-- Trigger to prevent UPDATE/DELETE on ledger_entries
CREATE OR REPLACE FUNCTION prevent_ledger_mutation()
RETURNS TRIGGER AS $$
BEGIN
  RAISE EXCEPTION
    'Ledger entries are immutable. Use compensating entries for corrections.';
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER no_update_ledger
  BEFORE UPDATE ON ledger_entries
  FOR EACH ROW EXECUTE FUNCTION prevent_ledger_mutation();

CREATE TRIGGER no_delete_ledger
  BEFORE DELETE ON ledger_entries
  FOR EACH ROW EXECUTE FUNCTION prevent_ledger_mutation();
```

---

## 10. Contrast with General Accounting Software

### 10.1 Payment Ledgers vs Traditional Accounting Software

```
Dimension             Payment Ledger              Traditional (QuickBooks/SAP)
--------------------  --------------------------  --------------------------------
Transaction volume    Millions/day                Hundreds-thousands/day
Latency requirement   Sub-second (real-time)      Batch (end-of-day, end-of-month)
Balance queries       Real-time (is merchant      End-of-period (monthly P&L,
                      balance sufficient for       quarterly balance sheet)
                      payout? Answer NOW)
Entry creation        Automated -- every payment  Manual or semi-automated --
                      event auto-generates          accountant creates entries
                      ledger entries
Immutability          Strictly enforced --        Often mutable -- accountants
                      append-only, DB triggers      can edit/delete entries
                                                    (with audit log)
Reconciliation        Automated, continuous        Manual, periodic
                      (daily/hourly)               (monthly/quarterly)
Currency handling     Real-time FX rates,          Periodic revaluation,
                      per-transaction conversion    batch conversion
Users                 Machines (payment service)   Humans (accountants, CFOs)
Correctness model     Must be correct at every     Must be correct at
                      instant (real-time balance    reporting boundaries
                      query for payouts)            (month-end close)
Schema complexity     Few account types, high      Many account types (CoA with
                      volume per account            hundreds of accounts),
                                                    low volume per account
```

### 10.2 Why You Cannot Use QuickBooks as a Payment Ledger

1. **Latency:** QuickBooks is designed for humans clicking buttons, not for an
   API processing 1,000 transactions per second. A payment ledger must create
   entries and return in milliseconds.

2. **Real-time balance:** When a merchant requests a payout, you need their
   available balance *right now*. QuickBooks computes balances by aggregating
   journal entries — this is O(n) in the number of entries. A payment ledger
   maintains pre-computed running balances updated atomically with each entry.

3. **Concurrency:** QuickBooks assumes one or a few users. A payment ledger
   handles thousands of concurrent transactions, each creating ledger entries.
   Row-level locking on account balance rows becomes a bottleneck — payment
   ledgers use techniques like optimistic locking or partitioned balance
   counters.

4. **ACID integration:** QuickBooks is a standalone application. A payment ledger
   must be atomically consistent with the payment state machine — they must share
   a database transaction. You cannot call the QuickBooks API mid-transaction.

### 10.3 Modern Ledger-as-a-Service

Companies like Modern Treasury, Moov, and Stripe (with its internal ledger)
have built purpose-built financial ledgers designed for the payment use case.
These provide:

- Double-entry by construction (API rejects unbalanced entries)
- Immutability enforced at the infrastructure level
- Sub-millisecond balance queries
- Integration with payment processing pipelines
- Built-in reconciliation workflows

[INFERRED — specific performance characteristics of these platforms are based on
their public documentation and marketing materials, not independent benchmarks.]

---

## Summary: Key Takeaways for Interview

```
1. INVARIANT: Sum of all debits = Sum of all credits. Always. No exceptions.

2. IMMUTABILITY: Never update or delete ledger entries. Corrections via
   compensating entries. This is non-negotiable for financial systems.

3. ATOMICITY: Payment state change + ledger entries in the SAME DB transaction.
   Not separate microservices. Financial correctness > microservice purity.

4. INTEGERS: Store money as integers in smallest currency unit. Never floats.
   This is not pedantic -- floating point errors compound across millions of txns.

5. RECONCILIATION: Three-way recon (internal vs acquirer vs card network).
   Automated, continuous. The unsung hero of payment systems.

6. AUDIT: Every action logged with who, what, when, why. Required by PCI DSS,
   SOX. Tamper-evident where possible.

7. SCALE CONTRAST: Payment ledgers are real-time, automated, high-throughput.
   Traditional accounting software is batch, manual, low-throughput.
   Same principles (double-entry), completely different engineering.
```

---

## References

- Luca Pacioli, *Summa de Arithmetica* (1494) — origin of double-entry bookkeeping
- PCI DSS v3.2.1, Requirement 10 — audit trail requirements
- Stripe API documentation — fee structure, refund policies
- Modern Treasury documentation — ledger-as-a-service patterns
- Martin Kleppmann, *Designing Data-Intensive Applications* — ACID transactions, event sourcing
- ByteByteGo, "Design a Payment System" — system design reference

> **Note:** Web search was unavailable during the creation of this document.
> Claims about specific company internals (Stripe, Razorpay, Modern Treasury)
> are based on publicly known information from engineering blogs and official
> documentation available up to early 2025. Items marked [INFERRED] could not be
> verified against primary sources during this writing session.
