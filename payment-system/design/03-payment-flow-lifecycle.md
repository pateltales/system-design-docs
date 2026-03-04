# 03 - Payment Flow Lifecycle: Initiation to Settlement

> End-to-end lifecycle of a payment transaction, covering card networks, alternative
> payment methods, interchange economics, and 3D Secure authentication.

---

## Table of Contents

1. [Payment Flow Overview](#1-payment-flow-overview)
2. [Key Actors](#2-key-actors)
3. [Authorization Flow](#3-authorization-flow)
4. [Capture Flow](#4-capture-flow)
5. [Settlement Flow](#5-settlement-flow)
6. [Interchange Economics](#6-interchange-economics)
7. [3D Secure Authentication](#7-3d-secure-authentication)
8. [Alternative Payment Methods](#8-alternative-payment-methods)
9. [Contrast with Amazon Pay](#9-contrast-with-amazon-pay)

---

## 1. Payment Flow Overview

A card payment passes through a chain of intermediaries. Every hop adds latency,
cost, and a trust boundary. The full round-trip for authorization typically completes
in **1-3 seconds**.

### High-Level Request Flow (Card Payment)

```
 CARDHOLDER          MERCHANT           PSP / GATEWAY        ACQUIRER          CARD NETWORK        ISSUER
     |                  |                    |                   |                  |                  |
     |  1. Pay $100     |                    |                   |                  |                  |
     |----------------->|                    |                   |                  |                  |
     |                  |  2. Auth Request   |                   |                  |                  |
     |                  |------------------->|                   |                  |                  |
     |                  |                    |  3. Forward Auth  |                  |                  |
     |                  |                    |------------------>|                  |                  |
     |                  |                    |                   | 4. Route to      |                  |
     |                  |                    |                   |    Network       |                  |
     |                  |                    |                   |----------------->|                  |
     |                  |                    |                   |                  | 5. Auth Request  |
     |                  |                    |                   |                  |----------------->|
     |                  |                    |                   |                  |                  |
     |                  |                    |                   |                  |  6. Check:       |
     |                  |                    |                   |                  |  - Card valid?   |
     |                  |                    |                   |                  |  - Funds avail?  |
     |                  |                    |                   |                  |  - Fraud check?  |
     |                  |                    |                   |                  |                  |
     |                  |                    |                   |                  | 7. Auth Response |
     |                  |                    |                   |                  |<-----------------|
     |                  |                    |                   | 8. Response      |                  |
     |                  |                    |                   |<-----------------|                  |
     |                  |                    | 9. Response       |                  |                  |
     |                  |                    |<------------------|                  |                  |
     |                  | 10. Auth Result    |                   |                  |                  |
     |                  |<-------------------|                   |                  |                  |
     | 11. Confirmation |                    |                   |                  |                  |
     |<-----------------|                    |                   |                  |                  |
```

### Three Phases of a Card Payment

```
  TIME ------>

  [AUTHORIZATION]          [CAPTURE]              [SETTLEMENT]
   Real-time               Same day or            T+1 to T+3
   1-3 seconds             within auth window     Batch process
                           (up to 7 days)

   "Can they pay?" ----->  "Charge them." ------>  "Move the money."
```

**Why three phases?** Because real money movement is slow and expensive. Authorization
is a fast "promise to pay." Capture converts that promise into a charge. Settlement
is the actual inter-bank fund transfer that happens in daily batches.

---

## 2. Key Actors

This is where interview candidates consistently confuse roles. The six parties
below have **distinct** responsibilities, and several are often conflated.

### Actor Definitions

| Actor | Also Called | Role | Example |
|-------|-----------|------|---------|
| **Cardholder** | Customer, Payer | Person initiating payment using their card | You, buying on Amazon |
| **Merchant** | Seller, Payee | Business accepting payment for goods/services | Amazon, Starbucks |
| **Acquirer** | Acquiring Bank, Merchant's Bank | Bank that holds the merchant's account and processes card transactions on their behalf | Chase Paymentech, Worldpay, First Data |
| **Issuer** | Issuing Bank, Cardholder's Bank | Bank that issued the card to the cardholder; decides whether to approve/decline | Chase (as card issuer), HDFC Bank, Citi |
| **Card Network** | Card Scheme, Card Brand | Routes messages between acquirer and issuer; sets rules and interchange rates | Visa, Mastercard, Amex, RuPay |
| **PSP** | Payment Service Provider, Payment Gateway | Aggregates payment methods, handles PCI compliance, provides API to merchants | Stripe, Razorpay, Adyen, Braintree |

### Common Confusions

**Confusion 1: PSP vs Acquirer**
A PSP is NOT a bank. It sits between the merchant and the acquirer, providing:
- A developer-friendly API
- PCI DSS compliance so the merchant never touches raw card data
- Multi-acquirer routing (smart routing)
- Tokenization, retry logic, fraud scoring

Some PSPs (like Adyen) also hold acquiring licenses, blurring the line.

**Confusion 2: Acquirer vs Issuer**
Both are banks, but they serve different sides:
- **Acquirer** = merchant's bank. Receives the payment.
- **Issuer** = cardholder's bank. Sends the payment.

A single bank (e.g., Chase) can be BOTH acquirer and issuer for different transactions.

**Confusion 3: Card Network vs PSP**
Visa/Mastercard do NOT process payments directly for merchants. They are the
**network/rails** that connect acquirers to issuers. They:
- Route authorization messages
- Set interchange rates
- Operate the settlement/clearing system
- Define card rules (chargebacks, disputes, etc.)

**Exception: American Express** operates as a "closed loop" network -- it acts as
both the card network AND the issuer (and sometimes the acquirer). This is why
Amex can charge higher merchant fees.

### Actor Relationship Diagram

```
                    +------------------+
                    |   CARD NETWORK   |
                    | (Visa/Mastercard)|
                    |                  |
                    | - Routes msgs    |
                    | - Sets rules     |
                    | - Runs clearing  |
                    +--------+---------+
                             |
              +--------------+--------------+
              |                             |
    +---------v----------+       +----------v---------+
    |     ACQUIRER       |       |      ISSUER        |
    | (Merchant's Bank)  |       | (Cardholder's Bank)|
    |                    |       |                    |
    | - Settles funds    |       | - Approves/Declines|
    |   to merchant      |       | - Manages credit   |
    | - Bears merchant   |       |   line / balance   |
    |   risk             |       | - Bears fraud risk |
    +---------+----------+       +----------+---------+
              |                             |
    +---------v----------+       +----------v---------+
    |       PSP          |       |    CARDHOLDER      |
    | (Stripe/Razorpay)  |       | (Customer)         |
    |                    |       |                    |
    | - API for merchant |       | - Initiates payment|
    | - PCI compliance   |       | - Authenticates    |
    | - Smart routing    |       |   (3DS, OTP, etc.) |
    +---------+----------+       +--------------------+
              |
    +---------v----------+
    |     MERCHANT       |
    | (Amazon/Starbucks) |
    |                    |
    | - Sells goods      |
    | - Integrates PSP   |
    +--------------------+
```

---

## 3. Authorization Flow

Authorization is the real-time "yes/no" decision. The issuer checks card validity,
available funds/credit, and fraud signals, then returns an approval or decline.

### Step-by-Step Authorization

```
Step  Hop                           Latency      What Happens
----  ----                          -------      ------------
 1    Cardholder -> Merchant        ~0ms         Card details entered / tapped / swiped
 2    Merchant -> PSP               ~50-100ms    PSP receives card data (tokenized)
                                                 PSP runs its own fraud scoring
 3    PSP -> Acquirer               ~50-100ms    Auth request formatted as ISO 8583 message
                                                 Includes: PAN, amount, currency, MCC, CVV
 4    Acquirer -> Card Network      ~50-100ms    Network identifies issuer from BIN (first 6-8 digits)
                                                 Routes message to correct issuer
 5    Card Network -> Issuer        ~50-200ms    Network applies its own risk checks
                                                 Forwards to issuer
 6    Issuer (internal processing)  ~100-500ms   Issuer checks:
                                                 - Is the card active and not reported stolen?
                                                 - Is there sufficient credit/balance?
                                                 - Does this trip fraud rules? (velocity, geo, etc.)
                                                 - Is 3DS required? (triggers challenge if yes)
                                                 If approved: places a "hold" on funds
 7    Issuer -> Card Network        ~50-100ms    Returns auth response with response code
 8    Card Network -> Acquirer      ~50-100ms    Forwards response
 9    Acquirer -> PSP               ~50-100ms    Forwards response
10    PSP -> Merchant               ~50-100ms    PSP webhook/callback with result
11    Merchant -> Cardholder        ~0ms         "Payment successful" or "Payment declined"

TOTAL END-TO-END: ~500ms to 3000ms (typical: 1-2 seconds)
```

### Authorization Response Codes

The issuer returns a **response code** (ISO 8583 field 39) indicating the result.

| Code | Meaning | Merchant Action |
|------|---------|-----------------|
| `00` | Approved | Proceed with order |
| `01` | Refer to issuer | Decline (do not retry) |
| `05` | Do not honor | Generic decline; may retry with different payment method |
| `12` | Invalid transaction | Check request format |
| `14` | Invalid card number | Ask customer to re-enter |
| `41` | Lost card (pick up) | Decline and flag |
| `43` | Stolen card (pick up) | Decline and flag |
| `51` | Insufficient funds | Ask customer for another card |
| `54` | Expired card | Ask customer for another card |
| `55` | Incorrect PIN | Retry allowed (limited attempts) |
| `61` | Exceeds withdrawal limit | Customer must contact their bank |
| `65` | Exceeds frequency limit | Customer must contact their bank |
| `91` | Issuer unavailable | Retry after delay (issuer system down) |

### Auth Code

On approval, the issuer returns a **6-character alphanumeric auth code** (e.g., `A4B2C9`).
This code is proof that the issuer approved the transaction and is critical for:
- Linking authorization to capture
- Dispute resolution (merchant can prove authorization existed)
- Reconciliation between acquirer and network

### Authorization Hold

When approved, the issuer places a **hold** (also called a "pending charge") on the
cardholder's available credit or balance. The hold:
- Reduces available balance by the authorized amount
- Does NOT actually move money yet
- Has an expiry window (typically **7 days** for e-commerce, **30 days** for hotels/car rentals [UNVERIFIED])
- Is visible to the cardholder as a "pending" transaction on their statement

---

## 4. Capture Flow

Capture converts an authorization hold into an actual charge. It tells the acquirer:
"I've fulfilled the order; now claim the money."

### Why Is Capture Separate from Auth?

This is a common interview question. The answer is rooted in real-world business needs:

| Scenario | Auth Timing | Capture Timing | Why Separate? |
|----------|-------------|----------------|---------------|
| **E-commerce** | When customer places order | When item ships | Merchant may not ship for days; regulations in many jurisdictions prohibit charging before shipment |
| **Hotels** | At check-in (estimated stay) | At checkout (actual stay) | Final amount may differ from auth (minibar, extended stay) |
| **Gas stations** | Pre-auth for $1 or $100 | After pump stops | Final amount unknown at auth time |
| **Restaurants** | When card is swiped | After tip is added | Tip changes the total |
| **Subscriptions** | Monthly recurring auth | Immediately after auth | Auth and capture happen together ("auth-capture" or "sale") |

### Capture Mechanics

```
 MERCHANT                PSP              ACQUIRER           CARD NETWORK
     |                    |                   |                  |
     | 1. Capture $95     |                   |                  |
     |    (auth was $100) |                   |                  |
     |------------------->|                   |                  |
     |                    | 2. Capture req    |                  |
     |                    |    with auth code |                  |
     |                    |------------------>|                  |
     |                    |                   | 3. Submit to     |
     |                    |                   |    clearing file |
     |                    |                   |----------------->|
     |                    |                   |                  |
     |                    | 4. Capture ACK    |                  |
     |                    |<------------------|                  |
     | 5. Confirmed       |                   |                  |
     |<-------------------|                   |                  |
```

### Key Capture Rules

- **Partial capture**: Merchant can capture LESS than the auth amount (e.g., auth $100,
  capture $95 because one item was out of stock). The remaining hold is released.
- **No over-capture**: You generally cannot capture MORE than the authorized amount
  (some networks allow small over-capture for tips, typically up to 20% [UNVERIFIED]).
- **Auth validity window**: If capture doesn't happen within the window (typically
  **7 days** for Visa, **7 days** for Mastercard [UNVERIFIED]), the auth expires and
  the hold is released. A new auth is needed.
- **Void**: If the merchant decides not to capture at all, they should send a **void**
  (also called "auth reversal") to release the hold immediately rather than waiting
  for expiry.

### Batch Capture

Most merchants don't capture transactions one by one in real-time. Instead:

1. Throughout the day, authorizations accumulate
2. At end of day (or configurable schedule), the merchant/PSP submits a **batch file**
   to the acquirer containing all captures
3. The acquirer forwards the batch to the card network for clearing

This batch approach is more efficient and is how most traditional POS systems work.
Modern e-commerce PSPs (Stripe, Adyen) support both real-time and batch capture.

---

## 5. Settlement Flow

Settlement is the actual movement of money between banks. It happens in **daily
batch cycles** operated by the card network.

### Settlement Timeline

```
Day 0 (Transaction Day)
  |
  |  Authorization happens (real-time)
  |  Capture happens (real-time or end-of-day batch)
  |
Day 0 - End of Day
  |
  |  Acquirer submits capture batch to Card Network
  |  Card Network aggregates all captures globally
  |
Day 1 (T+1)
  |
  |  Card Network runs CLEARING:
  |    - Matches authorizations to captures
  |    - Calculates interchange fees
  |    - Calculates network fees
  |    - Produces clearing files for all participants
  |
  |  Card Network runs SETTLEMENT:
  |    - Nets all obligations between each pair of banks
  |    - Issues settlement instructions
  |
Day 1-2 (T+1 to T+2)
  |
  |  Actual fund transfer:
  |    Issuer debits cardholder's account
  |    Issuer sends (transaction amount - interchange fee) to card network
  |    Card Network sends (transaction amount - interchange - network fee) to acquirer
  |    Acquirer deposits (amount - interchange - network fee - acquirer fee) to merchant
  |
Day 2-3
  |
  |  Merchant sees funds in their bank account
  |  Cardholder sees charge move from "pending" to "posted"
```

### Netting

Card networks don't transfer money for every single transaction. They use **netting**
to minimize the number of actual fund transfers.

**Example without netting (inefficient):**
```
Bank A owes Bank B: $10,000 (across 200 transactions)
Bank B owes Bank A: $8,000  (across 150 transactions)
= 350 individual transfers
```

**Example with netting (what actually happens):**
```
Bank A owes Bank B: $10,000
Bank B owes Bank A:  $8,000
NET: Bank A sends Bank B $2,000 (ONE transfer)
```

Across thousands of banks and millions of daily transactions, netting reduces the
number of actual inter-bank transfers from millions to thousands.

### Settlement Flow Diagram

```
+------------------------------------------------------------------+
|                    CARD NETWORK (Visa/MC)                         |
|                                                                  |
|  Clearing Engine:                                                |
|  +------------------------------------------------------------+ |
|  | For each transaction:                                       | |
|  |   Gross Amount:              $100.00                        | |
|  |   - Interchange Fee (to Issuer):  -$1.80  (1.80%)          | |
|  |   - Network Fee (to Visa/MC):     -$0.13  (0.13%)          | |
|  |   = Amount to Acquirer:           $98.07                    | |
|  +------------------------------------------------------------+ |
|                                                                  |
|  Netting Engine:                                                 |
|  +------------------------------------------------------------+ |
|  | Bank A net position: -$250,000  (owes the network)          | |
|  | Bank B net position: +$180,000  (network owes them)         | |
|  | Bank C net position: +$70,000   (network owes them)         | |
|  | (Sum always = 0, minus network fees retained by Visa/MC)    | |
|  +------------------------------------------------------------+ |
+------------------------------------------------------------------+
         |                    |                    |
    Fund Transfer        Fund Transfer        Fund Transfer
    (via central bank    (via central bank    (via central bank
     or correspondent)    or correspondent)    or correspondent)
         |                    |                    |
    +----v-----+        +----v-----+        +----v-----+
    |  Bank A  |        |  Bank B  |        |  Bank C  |
    | (Issuer) |        | (Acquirer)|       | (Acquirer)|
    +----------+        +----------+        +----------+
```

### What the Merchant Actually Receives

For a $100 transaction on a Visa credit card:

```
  Gross transaction amount:            $100.00
  - Interchange fee (goes to issuer):   -$1.80  [~1.5-2.5% for credit]
  - Network/scheme fee (goes to Visa):  -$0.13  [~0.10-0.15%]
  - Acquirer/PSP fee (goes to PSP):     -$0.40  [~0.20-0.50%]
  ----------------------------------------
  Net to merchant:                      $97.67
  ----------------------------------------
  Merchant Discount Rate (MDR):          2.33%
```

---

## 6. Interchange Economics

Interchange is the largest component of payment processing costs and is the fee
that flows from the **acquirer to the issuer** on every card transaction. It is set
by the card network (not negotiated by individual banks).

### Why Does Interchange Exist?

Interchange solves a **chicken-and-egg problem**:
- Merchants want to accept cards because customers have them
- Customers want cards because merchants accept them
- Issuers bear the cost of issuing cards, extending credit, and absorbing fraud
- Interchange **compensates issuers** for these costs and risks, incentivizing them
  to issue more cards, which benefits the entire network

### Fee Breakdown by Payment Method

```
+-------------------+----------------+-------------+-------------+-------------+
| Component         | Credit Card    | Debit Card  | Debit (PIN) | Premium/    |
|                   | (US, Visa/MC)  | (US, sig.)  | (US)        | Rewards Card|
+-------------------+----------------+-------------+-------------+-------------+
| Interchange       | 1.50 - 2.50%   | 0.50 - 1.05%| 0.05% +     | 2.00 - 3.50%|
| (to issuer)       |                |             | $0.21-0.22  |             |
+-------------------+----------------+-------------+-------------+-------------+
| Network fee       | 0.10 - 0.15%   | 0.10 - 0.15%| 0.10-0.15%  | 0.10 - 0.15%|
| (to Visa/MC)      |                |             |             |             |
+-------------------+----------------+-------------+-------------+-------------+
| Acquirer/PSP fee  | 0.20 - 0.50%   | 0.20 - 0.50%| 0.20-0.50%  | 0.20 - 0.50%|
| (to Stripe etc.)  |                |             |             |             |
+-------------------+----------------+-------------+-------------+-------------+
| TOTAL MDR         | ~1.80 - 3.15%  | ~0.80-1.70% | ~0.35-0.87% | ~2.30-4.15% |
| (merchant pays)   |                |             |             |             |
+-------------------+----------------+-------------+-------------+-------------+
```

**Notes on the table above:**
- US debit card interchange is capped by the **Durbin Amendment** (part of
  Dodd-Frank Act, 2010) at approximately $0.21 + 0.05% for issuers with assets
  over $10 billion. This cap does not apply to credit cards.
- Premium/rewards cards have **higher** interchange because the issuer funds the
  rewards from interchange revenue. This is why some merchants prefer debit over credit.
- The numbers above are US-centric. EU interchange is capped at **0.30%** for
  credit and **0.20%** for debit under the EU Interchange Fee Regulation (IFR, 2015).

### Interchange Variation Factors

Interchange is NOT a single rate. It varies based on:

| Factor | Lower Interchange | Higher Interchange |
|--------|------------------|--------------------|
| Card type | Debit | Credit (especially rewards/premium) |
| Transaction type | Card-present (chip/tap) | Card-not-present (online) |
| Merchant category | Grocery, utilities, education | Travel, entertainment |
| Transaction size | Larger amounts (for flat-fee component) | Smaller amounts |
| Data quality | Level 2/3 data submitted (B2B) | Basic data only |
| Region | Regulated markets (EU, AU) | Unregulated (US credit) |

### Who Profits from What?

```
$100 card payment fee flow:

  MERCHANT pays $2.33 total (MDR)
       |
       +---> $1.80 INTERCHANGE ---> ISSUER
       |     (largest piece)         Uses for: credit risk, rewards programs,
       |                             fraud prevention, card issuance costs
       |
       +---> $0.13 NETWORK FEE ---> VISA / MASTERCARD
       |     (scheme fee)            Uses for: network operations, brand,
       |                             innovation, clearing/settlement
       |
       +---> $0.40 ACQUIRER FEE --> PSP / ACQUIRER
             (markup)                Uses for: API, fraud tools, support,
                                     PCI compliance, smart routing
```

### Contrast: Interchange in India (UPI)

India's **Unified Payments Interface (UPI)** operates at **zero MDR** for
merchants (since January 2020, mandated by the Indian government). The government
subsidizes UPI transaction costs from its budget to promote digital payments.

| | Card (US) | UPI (India) |
|---|---|---|
| Interchange | 1.5-3% | 0% |
| Network fee | 0.1-0.15% | 0% (subsidized) |
| PSP fee | 0.2-0.5% | 0% for P2M |
| **Total MDR** | **~2-3.5%** | **0%** |
| Settlement | T+1 to T+3 | Near real-time |

This zero-MDR model is economically unusual and is a deliberate policy choice to
drive digital payment adoption. The tradeoff is that UPI app providers (PhonePe,
Google Pay, Paytm) struggle to monetize directly from payment transactions.

---

## 7. 3D Secure Authentication

3D Secure (3DS) is an additional authentication layer for **card-not-present (CNP)**
transactions (i.e., online payments). The "3 Domains" are: Issuer Domain,
Acquirer Domain, and Interoperability Domain (the card network).

### 3DS 1.0 (Legacy -- Avoid)

Introduced in the early 2000s (Visa called it "Verified by Visa," Mastercard called
it "SecureCode").

**Flow:**
```
 CARDHOLDER         MERCHANT          PSP         CARD NETWORK (Directory)     ISSUER (ACS)
     |                  |              |                  |                        |
     | 1. Enter card    |              |                  |                        |
     |   details        |              |                  |                        |
     |----------------->|              |                  |                        |
     |                  | 2. Enrollment|check              |                        |
     |                  |------------->|----------------->|                        |
     |                  |              |                  | 3. Is card enrolled    |
     |                  |              |                  |    in 3DS?             |
     |                  |              |                  |----------------------->|
     |                  |              |                  | 4. Yes + ACS URL       |
     |                  |              |                  |<-----------------------|
     |                  |              |<-----------------|                        |
     |                  |<-------------|                  |                        |
     |                  |              |                  |                        |
     | 5. FULL-PAGE REDIRECT to issuer's ACS page                                 |
     |----------------------------------------------------------------------->    |
     |                                                                            |
     | 6. Enter OTP / password on issuer's page                                   |
     |     (completely different look & feel, often broken on mobile)              |
     |----------------------------------------------------------------------->    |
     |                                                                            |
     | 7. Redirect back to merchant with auth result                              |
     |<-----------------------------------------------------------------------    |
     |                  |              |                  |                        |
     |                  | 8. Proceed   |with authorization (now with 3DS proof)    |
     |                  |------------->|----------------->|----------------------->|
```

**Problems with 3DS 1.0:**
- **Full-page redirect**: Customer leaves the merchant site entirely
- **Terrible mobile experience**: Issuer pages often not responsive
- **High cart abandonment**: 10-25% of customers dropped off at the 3DS step [UNVERIFIED]
- **No risk-based decisioning**: Every transaction got a challenge (password/OTP)
- **Static passwords**: Many issuers used static passwords, which customers forgot

### 3DS 2.0 (Current Standard -- EMV 3DS)

Introduced by EMVCo (2017-2019 rollout). Major improvements over 1.0.

**Flow:**
```
 CARDHOLDER         MERCHANT          PSP         CARD NETWORK (DS)       ISSUER (ACS)
     |                  |              |                  |                     |
     | 1. Enter card    |              |                  |                     |
     |   details        |              |                  |                     |
     |----------------->|              |                  |                     |
     |                  | 2. 3DS2 Auth |Request            |                     |
     |                  |   + RICH DATA|              |                     |
     |                  |   (device info, browser,   |                     |
     |                  |    IP, purchase history,   |                     |
     |                  |    shipping addr match)    |                     |
     |                  |------------->|----------------->|                     |
     |                  |              |                  |-------------------->|
     |                  |              |                  |                     |
     |                  |              |                  | 3. RISK-BASED       |
     |                  |              |                  |    ASSESSMENT:      |
     |                  |              |                  |                     |
     |                  |              |    +--------------+---------+          |
     |                  |              |    |                        |          |
     |                  |              |    v                        v          |
     |                  |              | FRICTIONLESS           CHALLENGE       |
     |                  |              | (Low risk:             (High risk:     |
     |                  |              |  ~90-95% of txns)       ~5-10%)        |
     |                  |              |                                        |
     |                  |              | 4a. If FRICTIONLESS:                   |
     |                  |              |     Auth result returned immediately   |
     |                  |              |     Customer sees NOTHING extra        |
     |                  |              |<---------------------------------------|
     |                  |              |                                        |
     |                  |              | 4b. If CHALLENGE:                      |
     |                  |              |     In-app modal / iframe (NOT redirect)|
     |                  |              |     OTP or biometric                   |
     | 5. Enter OTP     |              |     (native look & feel)              |
     |    in IFRAME     |              |                                        |
     |    (stays on     |              |                                        |
     |     merchant's   |              |                                        |
     |     page)        |              |                                        |
     |------------------------------------+                                    |
     |                  |              |   |                                    |
     |                  |              |   +----------------------------------->|
     |                  |              |                                        |
     |                  |              | 6. Final auth result                   |
     |                  |              |<---------------------------------------|
     |                  |<-------------|                                        |
     | 7. Done          |              |                                        |
     |<-----------------|              |                                        |
```

### 3DS 1.0 vs 3DS 2.0 Comparison

| Feature | 3DS 1.0 | 3DS 2.0 |
|---------|---------|---------|
| UX | Full-page redirect | Inline iframe / in-app SDK |
| Mobile support | Poor (non-responsive pages) | Native SDK for iOS/Android |
| Risk-based auth | No (challenge every time) | Yes (frictionless for low-risk) |
| Data shared with issuer | Minimal | 100+ data elements (device, behavior, history) |
| Challenge rate | ~100% | ~5-10% [UNVERIFIED] |
| Cart abandonment due to 3DS | 10-25% [UNVERIFIED] | ~1-5% [UNVERIFIED] |
| Authentication methods | Static password, OTP | OTP, biometric, push notification, app-based |
| In-app payments | Not supported | Natively supported via SDK |
| Mandate | Being phased out | Required by PSD2 (EU), RBI (India) |

### Liability Shift

3D Secure creates a **liability shift** -- one of the most important concepts:

```
WITHOUT 3DS:
  Fraudulent transaction --> Merchant is liable for chargeback
  (Card-not-present fraud is the merchant's problem)

WITH 3DS (successful authentication):
  Fraudulent transaction --> Issuer is liable for chargeback
  (Liability shifts from merchant to issuer because the issuer
   authenticated the cardholder)

WITH 3DS (authentication attempted but unavailable):
  If issuer doesn't support 3DS --> Issuer is still liable
  (Attempted authentication counts as a liability shift)
```

This liability shift is a major incentive for merchants to implement 3DS:
it transfers chargeback fraud risk to the issuer.

### PSD2 and SCA (Europe)

The EU's **Payment Services Directive 2 (PSD2)** requires **Strong Customer
Authentication (SCA)** for electronic payments. SCA requires at least 2 of:
1. **Knowledge** -- something the user knows (password, PIN)
2. **Possession** -- something the user has (phone, card)
3. **Inherence** -- something the user is (fingerprint, face)

3DS 2.0 is the primary mechanism to satisfy SCA for online card payments.

**Exemptions** (transactions that can skip SCA):
- Low-value transactions (under EUR 30, cumulative limit EUR 100)
- Recurring payments (after initial SCA)
- Trusted beneficiaries (whitelist)
- Transaction Risk Analysis (TRA) -- if the PSP's fraud rate is below thresholds
- Merchant-initiated transactions

---

## 8. Alternative Payment Methods

Card payments dominate in the US/UK but are NOT the global default. Each payment
method has a fundamentally different flow, cost structure, and settlement timeline.

### 8.1 UPI (India)

**Unified Payments Interface** -- operated by **NPCI** (National Payments Corporation
of India). Built on top of IMPS (Immediate Payment Service) infrastructure.

```
 PAYER                   PAYER'S         NPCI             PAYEE'S          PAYEE
 (PhonePe app)           PSP BANK        (UPI Switch)     PSP BANK         (Merchant)
     |                      |                |                |                |
     | 1. Scan QR /         |                |                |                |
     |    enter UPI ID      |                |                |                |
     |--------------------->|                |                |                |
     |                      | 2. Collect     |                |                |
     |                      |    request     |                |                |
     |                      |--------------->|                |                |
     |                      |                | 3. Route to    |                |
     |                      |                |    payer's bank|                |
     |                      |                |                |                |
     | 4. Enter UPI PIN     |                |                |                |
     |    (on device)       |                |                |                |
     |--------------------->|                |                |                |
     |                      | 5. Debit payer |                |                |
     |                      |    account     |                |                |
     |                      |--------------->|                |                |
     |                      |                | 6. Credit payee|                |
     |                      |                |    account     |                |
     |                      |                |--------------->|                |
     |                      |                |                |--------------->|
     |                      |                |                |                |
     |                      |                | 7. Confirmation|                |
     |                      |<---------------|                |                |
     | 8. "Payment          |                |                |                |
     |    successful"       |                |                |                |
     |<---------------------|                |                |                |
     |                      |                |                |                |

 TOTAL TIME: 2-5 seconds (real-time)
 SETTLEMENT: Near real-time (funds move immediately via IMPS)
 MDR: 0% for person-to-merchant (government subsidized)
 DAILY LIMIT: INR 1,00,000 (~$1,200) per transaction [UNVERIFIED]
```

**Key UPI characteristics:**
- **Account-to-account**: No card network intermediary; money moves directly
  between bank accounts via NPCI
- **VPA (Virtual Payment Address)**: e.g., `user@upi` -- abstracts away bank
  account numbers
- **Zero MDR**: Government mandated since Jan 2020; costs subsidized from budget
- **Real-time settlement**: Unlike cards (T+1 to T+3), UPI settles in seconds
- **Scale**: ~12-14 billion transactions per month (as of 2024) [UNVERIFIED]
- **Interoperable**: Works across all UPI apps (PhonePe, Google Pay, Paytm, etc.)

### 8.2 SEPA (Europe)

**Single Euro Payments Area** -- bank-to-bank transfers within the Eurozone
(36 countries).

```
 PAYER              PAYER'S BANK      CLEARING HOUSE       PAYEE'S BANK       PAYEE
     |                  |            (EBA/ECB)                 |                |
     | 1. Initiate      |                |                    |                |
     |    SEPA transfer  |                |                    |                |
     |----------------->|                |                    |                |
     |                  | 2. Submit to   |                    |                |
     |                  |    clearing    |                    |                |
     |                  |--------------->|                    |                |
     |                  |                | 3. Batch process   |                |
     |                  |                |    (or instant)    |                |
     |                  |                |------------------->|                |
     |                  |                |                    | 4. Credit      |
     |                  |                |                    |    payee       |
     |                  |                |                    |--------------->|

 SEPA Credit Transfer (SCT):
   Settlement: 1 business day (by regulation)
   Cost: Very low (~EUR 0.20-0.30 per transaction) [UNVERIFIED]

 SEPA Instant Credit Transfer (SCT Inst):
   Settlement: <10 seconds (max 20 seconds by regulation)
   Cost: Slightly higher (~EUR 0.50) [UNVERIFIED]
   Availability: 24/7/365
   Limit: EUR 100,000 per transaction [UNVERIFIED]
```

### 8.3 ACH (United States)

**Automated Clearing House** -- operated by **Nacha** (National Automated Clearing
House Association). The US backbone for bank-to-bank transfers.

```
 ORIGINATOR          ODFI              ACH OPERATOR         RDFI              RECEIVER
 (Payer/Payee)       (Originating      (Fed/EPN)            (Receiving        (Payer/Payee)
                      Bank)                                  Bank)
     |                  |                  |                    |                |
     | 1. Submit ACH    |                  |                    |                |
     |    entry         |                  |                    |                |
     |----------------->|                  |                    |                |
     |                  | 2. Batch to      |                    |                |
     |                  |    ACH operator   |                    |                |
     |                  |    (in batches)   |                    |                |
     |                  |----------------->|                    |                |
     |                  |                  | 3. Sort & route    |                |
     |                  |                  |------------------->|                |
     |                  |                  |                    | 4. Credit/debit|
     |                  |                  |                    |    receiver    |
     |                  |                  |                    |--------------->|

 Standard ACH:
   Settlement: 1-3 business days
   Cost: $0.20 - $1.50 per transaction [UNVERIFIED]

 Same Day ACH:
   Settlement: Same business day (submissions by 4:45 PM ET)
   Cost: Additional $0.026 per transaction fee [UNVERIFIED]
   Limit: $1,000,000 per transaction [UNVERIFIED]

 ACH is used for:
   - Payroll (direct deposit)
   - Bill payments
   - Government benefits (Social Security, tax refunds)
   - Subscription/recurring payments
   - Bank-to-bank transfers (Venmo, Zelle use ACH underneath)
```

### 8.4 Digital Wallets (Apple Pay, Google Pay)

Digital wallets are **NOT separate payment rails**. They are a **front-end layer**
on top of existing card networks. When you pay with Apple Pay, a Visa/Mastercard
transaction still happens underneath.

```
 CARDHOLDER            WALLET            MERCHANT           PSP           CARD NETWORK
 (iPhone)              (Apple Pay)                                        (Visa/MC)
     |                    |                 |                 |               |
     | 1. Double-click    |                 |                 |               |
     |    + Face ID       |                 |                 |               |
     |------------------->|                 |                 |               |
     |                    | 2. Generate     |                 |               |
     |                    |    DPAN         |                 |               |
     |                    |    (Device PAN, |                 |               |
     |                    |     tokenized)  |                 |               |
     |                    |                 |                 |               |
     |                    | 3. NFC / online |                 |               |
     |                    |    payment with |                 |               |
     |                    |    DPAN +       |                 |               |
     |                    |    cryptogram   |                 |               |
     |                    |---------------->|---------------->|               |
     |                    |                 |                 |  4. Detokenize|
     |                    |                 |                 |     DPAN to   |
     |                    |                 |                 |     real PAN  |
     |                    |                 |                 |-------------->|
     |                    |                 |                 |               |
     |                    |                 |                 | 5. Normal card|
     |                    |                 |                 |    auth flow  |
     |                    |                 |                 |    (to issuer)|
     |                    |                 |                 |               |
```

**Key wallet characteristics:**
- **Tokenization**: The wallet never stores or transmits the real card number (PAN).
  It uses a **Device PAN (DPAN)** or **token** that is worthless if stolen.
- **Biometric auth**: Face ID / fingerprint satisfies SCA requirements, often
  bypassing 3DS challenges entirely.
- **Same interchange**: Merchant pays the same (or very similar) interchange as a
  regular card transaction.
- **Card network still involved**: Apple Pay on Visa = a Visa transaction. The
  money flows through the same card network rails.

### Payment Method Comparison Table

```
+------------------+------------+-------------+----------+-----------+---------------+
| Method           | Settlement | MDR / Cost  | Latency  | Region    | Reversible?   |
+------------------+------------+-------------+----------+-----------+---------------+
| Credit Card      | T+1 to T+3| 1.5 - 3.5%  | 1-3s     | Global    | Yes (chargeback|
|                  |            |             | (auth)   |           | up to 120 days)|
+------------------+------------+-------------+----------+-----------+---------------+
| Debit Card       | T+1 to T+2| 0.5 - 1.5%  | 1-3s     | Global    | Yes (chargeback)|
+------------------+------------+-------------+----------+-----------+---------------+
| UPI (India)      | Real-time  | 0%          | 2-5s     | India     | Disputes only  |
|                  |            | (subsidized)|          |           | (no chargeback)|
+------------------+------------+-------------+----------+-----------+---------------+
| SEPA Transfer    | 1 day      | ~EUR 0.20   | 1 day    | Eurozone  | Not after sent |
| SEPA Instant     | <10 sec    | ~EUR 0.50   | <10s     | (36 ctry) | Not after sent |
+------------------+------------+-------------+----------+-----------+---------------+
| ACH (US)         | 1-3 days   | $0.20-1.50  | 1-3 days | US        | Can be returned|
| Same-Day ACH     | Same day   | + $0.026    | Same day |           | (within window)|
+------------------+------------+-------------+----------+-----------+---------------+
| Apple Pay /      | Same as    | Same as     | 1-3s     | Global    | Same as card   |
| Google Pay       | card       | card        | (auth)   |           |                |
+------------------+------------+-------------+----------+-----------+---------------+
| Wire Transfer    | Same day   | $15-45      | Hours    | Global    | Not reversible |
| (SWIFT)          | to T+1     | (flat fee)  |          |           |                |
+------------------+------------+-------------+----------+-----------+---------------+
```

---

## 9. Contrast with Amazon Pay

This is a common interview topic when designing payment systems for a company
like Amazon. Amazon Pay is fundamentally different from a pure PSP like Stripe.

### Pure PSP (Stripe, Razorpay, Adyen)

A PSP is **payment-method agnostic** and **merchant agnostic**. Its job is:

```
  MERCHANT  ---API--->  PSP  ---routes--->  [Card Network / Bank / UPI / etc.]

  PSP provides:
  - Unified API for all payment methods
  - PCI compliance
  - Smart routing across acquirers
  - Fraud detection
  - Tokenization and vault
  - Reconciliation and reporting

  PSP does NOT:
  - Know the customer's identity
  - Store shipping addresses
  - Manage customer relationships
  - Own the checkout experience
```

### Amazon Pay

Amazon Pay **bundles identity + address + payment** into a single checkout button
that external merchants can embed on their sites.

```
  CUSTOMER                  MERCHANT SITE           AMAZON PAY
     |                          |                       |
     | 1. Click "Pay with      |                       |
     |    Amazon"               |                       |
     |------------------------->|                       |
     |                          | 2. Redirect to        |
     |                          |    Amazon auth        |
     |                          |---------------------->|
     |                          |                       |
     | 3. Log into Amazon       |                       |
     |    (already logged in    |                       |
     |     on most devices)     |                       |
     |<------------------------------------------------|
     |                          |                       |
     | 4. Select shipping       |                       |
     |    address from Amazon   |                       |
     |    account               |                       |
     |------------------------------------------------>|
     |                          |                       |
     | 5. Select payment method |                       |
     |    from Amazon account   |                       |
     |    (card, bank, etc.)    |                       |
     |------------------------------------------------>|
     |                          |                       |
     |                          | 6. Amazon processes    |
     |                          |    payment & shares    |
     |                          |    shipping address    |
     |                          |<----------------------|
     |                          |                       |
     | 7. Order confirmed       |                       |
     |<-------------------------|                       |
```

### Key Differences

```
+---------------------+----------------------------+---------------------------+
| Dimension           | Pure PSP (Stripe)          | Amazon Pay                |
+---------------------+----------------------------+---------------------------+
| Customer identity   | Merchant owns it           | Amazon owns it            |
+---------------------+----------------------------+---------------------------+
| Shipping address    | Not involved               | Amazon provides it        |
|                     |                            | (from customer's account) |
+---------------------+----------------------------+---------------------------+
| Payment methods     | All methods supported      | Only methods stored in    |
|                     | (cards, UPI, ACH, etc.)    | customer's Amazon account |
+---------------------+----------------------------+---------------------------+
| Checkout UX         | Merchant controls fully    | Amazon-branded overlay    |
+---------------------+----------------------------+---------------------------+
| Trust model         | Customer trusts merchant   | Customer trusts Amazon    |
|                     | with card details (via PSP)| (no card shared w/ merch.)|
+---------------------+----------------------------+---------------------------+
| Conversion benefit  | None (payment only)        | High -- saved address &   |
|                     |                            | payment = 1-click checkout|
+---------------------+----------------------------+---------------------------+
| Data ownership      | Merchant gets full txn data| Amazon retains customer   |
|                     |                            | relationship data         |
+---------------------+----------------------------+---------------------------+
| Use on Amazon.com   | Not applicable             | Same system powers        |
|                     |                            | Amazon's own checkout     |
+---------------------+----------------------------+---------------------------+
| Analogous to        | A "plumber" -- connects    | A "landlord" -- provides  |
|                     | payment pipes              | the whole building        |
+---------------------+----------------------------+---------------------------+
```

### For Interview: Why Does This Matter?

When designing a payment system:

1. **If building for a marketplace (like Amazon)**: You likely need an Amazon Pay-like
   system that owns the customer identity, stores payment methods, and handles
   internal wallet/balance. You ARE the platform.

2. **If building a PSP (like Stripe)**: You need to be payment-method agnostic,
   support multi-tenant merchant onboarding, and focus on routing, reliability,
   and compliance. You serve merchants.

3. **If building for a merchant**: You integrate WITH a PSP. Your system needs to
   handle payment intents, webhook processing, idempotency, and reconciliation.
   You don't touch card data directly.

The architectural implications are fundamentally different:

```
  Amazon-style:     Monolith with customer identity at the center
                    Internal payment orchestration
                    Internal wallet and stored payment methods
                    Seller payouts as a separate batch system

  Stripe-style:     Multi-tenant platform
                    Plugin architecture for payment methods
                    Smart routing across multiple acquirers
                    Merchant-facing API with strong idempotency guarantees

  Merchant-style:   Thin integration layer with PSP
                    Webhook consumers for async payment events
                    Order state machine driven by payment state
                    Reconciliation cron jobs
```

---

## Summary: Complete Payment Lifecycle

```
 TIME ────────────────────────────────────────────────────────────────────────>

 ┌─────────────┐   ┌──────────────┐   ┌──────────────┐   ┌──────────────┐
 │  INITIATION │   │AUTHORIZATION │   │   CAPTURE     │   │  SETTLEMENT  │
 │             │   │              │   │              │   │              │
 │ Customer    │   │ Real-time    │   │ When goods   │   │ Daily batch  │
 │ clicks      │──>│ yes/no from  │──>│ are shipped  │──>│ net clearing │
 │ "Pay"       │   │ issuer       │   │ or delivered │   │ between banks│
 │             │   │ (~1-3 sec)   │   │ (0-7 days)   │   │ (T+1 to T+3)│
 │ Card/UPI/   │   │              │   │              │   │              │
 │ bank details│   │ Fraud check  │   │ Partial OK   │   │ Interchange  │
 │ collected   │   │ Balance check│   │ Void if no   │   │ fees deducted│
 │ (tokenized) │   │ Auth code    │   │ fulfillment  │   │ Netting      │
 │             │   │ returned     │   │              │   │ applied      │
 └─────────────┘   └──────────────┘   └──────────────┘   └──────────────┘
                         │                                       │
                         │            ┌──────────────┐           │
                         │            │   3D SECURE  │           │
                         └───────────>│  (if needed) │───────────┘
                                      │              │
                                      │ Risk-based:  │
                                      │ 90-95%       │
                                      │ frictionless │
                                      │ 5-10%        │
                                      │ challenge    │
                                      └──────────────┘

 For UPI: Auth + Capture + Settlement happen in ONE step (~2-5 seconds)
 For ACH: No real-time auth; everything is batch (1-3 days)
 For SEPA Instant: Real-time but no auth/capture distinction
```

---

## Appendix: Numbers Reference (Quick Lookup)

| Metric | Value | Verified? |
|--------|-------|-----------|
| Card auth latency | 1-3 seconds | Well-established industry standard |
| Auth validity window (Visa/MC) | ~7 days (e-commerce) | Generally accurate; varies by MCC |
| Card settlement | T+1 to T+3 | Well-established |
| Credit card interchange (US) | 1.50 - 2.50% | Well-established range |
| Debit card interchange (US, regulated) | ~$0.21 + 0.05% | Durbin Amendment cap |
| Network fee (Visa/MC) | 0.10 - 0.15% | [UNVERIFIED] -- varies by product |
| Acquirer/PSP markup | 0.20 - 0.50% | Varies widely by PSP and volume |
| Total MDR (US credit) | ~2.0 - 3.5% | Well-established range |
| EU interchange cap (credit) | 0.30% | EU IFR regulation |
| EU interchange cap (debit) | 0.20% | EU IFR regulation |
| UPI MDR (India) | 0% (government subsidized) | Government mandate since Jan 2020 |
| UPI monthly volume | ~12-14 billion txns (2024) | [UNVERIFIED] -- growing rapidly |
| 3DS 2.0 frictionless rate | ~90-95% | [UNVERIFIED] -- varies by issuer |
| 3DS 1.0 drop-off rate | 10-25% | [UNVERIFIED] -- widely cited range |
| 3DS 2.0 drop-off rate | ~1-5% | [UNVERIFIED] |
| Same-Day ACH limit | $1,000,000 per transaction | [UNVERIFIED] -- raised from $100K |
| SEPA Instant limit | EUR 100,000 | [UNVERIFIED] -- may have been raised |
| SEPA Instant latency | <10 seconds | Regulation mandates max 20 seconds |
