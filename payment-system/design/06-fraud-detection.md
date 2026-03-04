# Payment System — Fraud Detection & Risk Engine Deep Dive

> Companion deep dive to the payment system interview simulation. This document covers the
> design of fraud detection systems, risk scoring, dispute management, and how fraud engines
> fit into the payment authorization flow.
>
> **Verification note**: WebSearch and WebFetch were unavailable during authoring. All claims
> about specific company internals (Stripe Radar, etc.) are based on publicly known information
> as of mid-2025. Specific numbers and internal architecture details are marked where
> unverifiable.

---

## Table of Contents

1. [Why Fraud Detection Is Existential](#1-why-fraud-detection-is-existential)
2. [Types of Payment Fraud](#2-types-of-payment-fraud)
3. [Rule-Based Engine (Layer 1)](#3-rule-based-engine-layer-1)
4. [ML-Based Scoring (Layer 2)](#4-ml-based-scoring-layer-2)
5. [3D Secure Challenge (Layer 3)](#5-3d-secure-challenge-layer-3)
6. [Post-Authorization Checks](#6-post-authorization-checks)
7. [Architecture — Where Fraud Checks Happen](#7-architecture--where-fraud-checks-happen)
8. [Dispute and Chargeback Handling](#8-dispute-and-chargeback-handling)
9. [Precision vs Recall Trade-off](#9-precision-vs-recall-trade-off)
10. [Stripe Radar as Reference Architecture](#10-stripe-radar-as-reference-architecture)
11. [PSP vs Issuer Fraud Detection — Two Sides of the Same Coin](#11-psp-vs-issuer-fraud-detection--two-sides-of-the-same-coin)
12. [Operational Concerns](#12-operational-concerns)
13. [Interview Tips — Fraud Detection Questions](#13-interview-tips--fraud-detection-questions)

---

## 1. Why Fraud Detection Is Existential

Fraud is not a "nice to have" concern for payment systems — it is an existential threat that
can destroy a PSP or merchant.

**The financial damage chain:**

1. Fraudulent transaction is processed.
2. Legitimate cardholder notices unauthorized charge.
3. Cardholder files dispute with issuing bank.
4. Issuer initiates chargeback — reverses the funds from the merchant/PSP.
5. PSP or merchant absorbs the loss (the goods/services are already delivered to the fraudster).
6. Card network charges a chargeback fee ($15-$100 per dispute) [UNVERIFIED — fee varies by network and acquirer agreement].
7. If chargeback rate exceeds thresholds, card networks impose penalties or terminate the merchant account entirely.

**The false positive damage chain:**

1. Legitimate customer attempts a purchase.
2. Fraud engine blocks the transaction.
3. Customer abandons the purchase — revenue lost.
4. Customer has a negative experience — lifetime value destroyed.
5. Merchant blames the PSP — churn risk.

A fraud engine must navigate between these two failure modes simultaneously. The goal is not
"block all fraud" — it is "maximize revenue while keeping fraud losses and chargeback rates
within acceptable bounds."

---

## 2. Types of Payment Fraud

### 2.1 Card-Not-Present (CNP) Fraud

**What it is:** A fraudster uses stolen card details (card number, expiry, CVV) to make
purchases online. The cardholder never authorized the transaction. This is the dominant
fraud type in e-commerce because the merchant cannot verify physical card possession.

**Concrete example:** A data breach at a retailer leaks 10 million card numbers. Fraudsters
buy these on dark web marketplaces for $5-$20 per card. They use the stolen cards to purchase
electronics from an online store, ship to a drop address, and resell the goods for cash.

**Why it is hard to detect:** The fraudster has all the card details that a legitimate
cardholder would provide. There is no physical card swipe, no PIN, no signature. The only
signals are behavioral — does this transaction fit the cardholder's normal pattern?

**Scale:** CNP fraud accounts for the vast majority of card fraud losses globally.
[UNVERIFIED — the exact percentage varies by source and year, but industry reports
consistently cite CNP as the largest category, often over 70% of total card fraud.]

### 2.2 Account Takeover (ATO)

**What it is:** A fraudster gains access to a legitimate customer's account on a merchant
platform (via credential stuffing, phishing, SIM swapping, etc.). They then make purchases
using the victim's saved payment methods.

**Concrete example:** A customer reuses their password across multiple sites. One site is
breached. Fraudsters use automated tools to try the same email/password combination on
thousands of e-commerce sites. When they find a match, they log in, change the shipping
address, and place orders using the victim's saved card.

**Why it is hard to detect:** The payment method itself is not stolen — it is legitimately
on file. The transaction may even match the cardholder's historical patterns (same merchant,
similar amounts). The only signals are account-level anomalies: new device, new IP, changed
shipping address, rapid succession of actions.

**Detection signals:** New device fingerprint, unusual login time, login from new geography,
shipping address change followed immediately by a large purchase, multiple failed login
attempts before success.

### 2.3 Friendly Fraud (First-Party Fraud)

**What it is:** The legitimate cardholder makes a genuine purchase but then disputes the
charge with their issuing bank, claiming they did not authorize it or did not receive the
goods. The cardholder gets a refund via the chargeback process while keeping the goods.

**Concrete example:** A customer buys a $500 pair of headphones online. After receiving them,
they call their bank and say "I did not make this purchase." The bank initiates a chargeback.
The merchant loses both the headphones and the $500.

**Why it is hard to detect:** The transaction IS legitimate at the time of authorization.
There are no fraud signals because the cardholder genuinely made the purchase. Detection
is retrospective — you can only identify patterns after repeated disputes from the same
cardholder.

**Detection signals:** Cardholder with history of disputes, disputes filed shortly after
delivery confirmation, disputes on digital goods (where "I didn't receive it" is harder
for the merchant to refute).

**Scale:** Friendly fraud is estimated to account for 40-80% of all chargebacks
[UNVERIFIED — percentages vary widely across industry reports]. It is the most frustrating
type because traditional fraud detection cannot prevent it at authorization time.

### 2.4 Merchant Fraud

**What it is:** A fraudulent merchant signs up with a PSP and processes fake transactions
using stolen card details, or colludes with cardholders to process transactions and split
the proceeds.

**Concrete example:** A fraudster creates a fake online store, signs up with a PSP, and
processes thousands of small transactions using stolen cards. Before the chargebacks arrive
(which can take 30-120 days), the fraudster requests a payout and disappears.

**Why it matters for PSPs:** If the fraudulent merchant has already been paid out, the PSP
absorbs the chargeback losses. This is why PSPs hold reserves, delay payouts for new
merchants, and monitor merchant behavior closely.

**Detection signals:** New merchant with sudden spike in volume, unusually high transaction
counts relative to business size, high proportion of international cards, transactions
clustering from a small number of BINs, high velocity of refunds/chargebacks appearing
after an initial period.

### 2.5 Refund Abuse

**What it is:** Customers exploit refund policies — claiming items were not received
(when they were), claiming items were defective (when they are not), or using return fraud
schemes (returning empty boxes, returning counterfeits, "wardrobing" — wearing clothes
once and returning them).

**Concrete example:** A customer orders 10 items from an online retailer, keeps 8, and
returns 2 empty boxes claiming "items missing from package." The retailer processes the
refund because investigating every claim is cost-prohibitive.

**Where the PSP fits:** Refund abuse is primarily a merchant-side problem, but PSPs see
the refund patterns. Excessive refund rates from a merchant can signal either refund abuse
by customers or fraud by the merchant itself.

### 2.6 Testing Fraud (Card Testing)

**What it is:** Fraudsters test stolen card numbers by making small transactions ($0.50-$1.00)
to verify which cards are active before making large fraudulent purchases. They often target
merchants with weak fraud controls (donation sites, digital subscriptions).

**Concrete example:** A fraudster runs 1,000 stolen card numbers through a charity donation
page at $1.00 each. 200 succeed (valid cards). They then use those 200 cards at electronics
retailers for $500+ purchases.

**Detection signals:** High velocity of small transactions from the same IP or device,
sequential card numbers being tested, high decline rate (most stolen cards are already
cancelled), BIN patterns suggesting bulk stolen cards.

---

## 3. Rule-Based Engine (Layer 1)

The rule-based engine is the first line of defense. It evaluates deterministic conditions
that can be checked in microseconds.

### 3.1 How It Works

Rules are `IF condition THEN action` statements evaluated sequentially or in parallel.
Each rule produces a verdict: **allow**, **block**, or **review**. Rules can be combined
with AND/OR logic.

```
Rule evaluation flow:

Transaction comes in
    │
    ├── Rule 1: Velocity check ──── BLOCK if >5 txns/min from same card
    ├── Rule 2: Amount threshold ── REVIEW if amount > $10,000
    ├── Rule 3: Geo mismatch ────── BLOCK if card country != txn country AND amount > $500
    ├── Rule 4: BIN blocklist ────── BLOCK if BIN is on blocklist
    ├── Rule 5: IP blocklist ─────── BLOCK if IP is on blocklist
    ├── Rule 6: Card testing ─────── BLOCK if >10 declines from same IP in 5 min
    └── Rule 7: Time-of-day ─────── REVIEW if txn at 3am local time AND new card on file
         │
         ▼
    Combine verdicts → final rule verdict
```

### 3.2 Common Rule Categories

**Velocity checks:**
- More than N transactions from the same card in M minutes.
- More than N transactions from the same IP in M minutes.
- More than N distinct cards used from the same device fingerprint in M hours.
- More than N transactions to the same merchant from the same card in M hours.

**Amount thresholds:**
- Single transaction exceeding a threshold (e.g., >$10,000).
- Cumulative spend from the same card in 24 hours exceeding a threshold.
- Transaction amount significantly higher than the cardholder's historical average.

**Geographic rules:**
- Card issuing country does not match transaction country (geo mismatch).
- BIN country does not match IP geolocation country (BIN-country mismatch).
- Transaction originates from a high-risk country (configurable list).
- Shipping address country does not match card country.

**Blocklists:**
- Blocked BINs (ranges known for high fraud).
- Blocked IPs or IP ranges (known proxies, VPNs, data centers, Tor exit nodes).
- Blocked email domains (disposable email services).
- Blocked device fingerprints (devices previously associated with fraud).
- Blocked shipping addresses (drop addresses previously flagged).

**Identity checks:**
- Email address uses a disposable domain (mailinator.com, guerrillamail.com, etc.).
- Mismatch between billing name and email name.
- New account created less than N hours ago attempting a high-value purchase.

### 3.3 Rule Configuration

Rules should be configurable per merchant. A luxury goods merchant may want a lower amount
threshold ($2,000) while a digital subscription merchant may tolerate higher velocity.

```
RuleConfig:
  merchant_id: "merch_abc123"
  rules:
    - type: VELOCITY_CARD
      window_seconds: 60
      max_count: 3
      action: BLOCK

    - type: AMOUNT_THRESHOLD
      max_amount_cents: 500000   # $5,000
      action: REVIEW

    - type: GEO_MISMATCH
      card_country_vs: IP_COUNTRY
      action: REVIEW

    - type: BIN_BLOCKLIST
      list_id: "global_high_risk_bins"
      action: BLOCK
```

### 3.4 Strengths and Weaknesses

**Strengths:**
- Extremely fast — can evaluate in <1ms.
- Deterministic and explainable — easy to audit and debug.
- Easy to add new rules in response to emerging fraud patterns.
- No training data required.
- Merchants can understand and customize rules.

**Weaknesses:**
- Brittle — fraudsters adapt to specific rules (if you block >5 txns/min, they do 4).
- Binary decisions — rules cannot express "this is 73% likely to be fraud."
- Rule explosion — as you add more rules, interactions between rules become unpredictable.
- No generalization — rules only catch patterns you have explicitly defined.
- Maintenance burden — stale rules accumulate and cause false positives.

Rules are necessary but not sufficient. They catch the obvious fraud and serve as a
fast-path filter before the more expensive ML model is invoked.

---

## 4. ML-Based Scoring (Layer 2)

The ML model provides the nuanced scoring that rules cannot — it learns patterns from
historical data and generalizes to novel fraud patterns.

### 4.1 The Risk Score

Every transaction is scored by the ML model on a continuous scale, typically 0-100:
- **0-20**: Low risk — auto-approve.
- **20-65**: Medium risk — approve but flag for post-auth monitoring.
- **65-85**: High risk (gray zone) — trigger 3D Secure challenge.
- **85-100**: Very high risk — auto-decline.

The thresholds are configurable per merchant and tuned based on the merchant's risk appetite.

### 4.2 Feature Engineering

The quality of features determines model performance far more than the choice of algorithm.
Features fall into several categories:

**Transaction-level features:**
- Transaction amount (raw and normalized by merchant average).
- Currency.
- Merchant Category Code (MCC) — some MCCs are inherently higher risk (gambling, crypto,
  adult content).
- Card entry mode (manual entry vs. tokenized vs. recurring).
- Is this a first-time card on this merchant?

**Velocity features (aggregated over time windows):**
- Number of transactions from this card in the last 1 min / 5 min / 1 hour / 24 hours.
- Total spend from this card in the last 24 hours.
- Number of distinct merchants this card has been used at in the last 24 hours.
- Number of distinct cards from this device/IP in the last 24 hours.
- Number of declined transactions from this card/IP in the last hour.

**Device and network features:**
- Device fingerprint (browser fingerprint, mobile device ID).
- IP address geolocation (country, city, ISP).
- Is the IP a known proxy, VPN, or Tor exit node?
- Is the IP from a data center (not residential)?
- User agent string anomalies.
- Screen resolution, timezone, language settings.

**Historical pattern features:**
- Cardholder's historical average transaction amount.
- Cardholder's typical transaction times (morning vs. night).
- Cardholder's typical merchant categories.
- Cardholder's typical geographic locations.
- Days since this card was first seen.
- Historical chargeback rate for this BIN range.

**Email and identity features:**
- Email domain (free email vs. corporate vs. disposable).
- Email-to-name match score.
- Phone number carrier type (mobile vs. VoIP — VoIP is higher risk).
- Shipping address vs. billing address match.

**Network-level features (PSP advantage):**
- Has this card been seen across other merchants on the PSP's network?
- Has this card been involved in chargebacks at other merchants?
- Has this device/IP been flagged at other merchants?
- Has this email been associated with fraud at other merchants?
- What is the fraud rate for transactions with similar characteristics across the network?

This last category — network-level features — is the key advantage that large PSPs have.
A single merchant sees only their own transactions. A PSP like Stripe sees transactions
across millions of merchants. A card that was just used fraudulently at Merchant A can be
flagged instantly when it appears at Merchant B.

### 4.3 Model Architecture

**Gradient Boosted Trees (XGBoost / LightGBM):**
- The workhorse of tabular fraud detection.
- Handles mixed feature types (numerical + categorical) natively.
- Fast inference (sub-millisecond for a single prediction).
- Interpretable — feature importance, SHAP values for individual predictions.
- Robust to missing features and outliers.
- Widely used in production fraud systems across the industry.

**Neural Networks (for sequence modeling):**
- A cardholder's transaction history is a sequence — sequence models (LSTMs, Transformers)
  can learn temporal patterns.
- Example: a sequence of small transactions at gas stations followed by a large online
  purchase is a known fraud pattern (gas station is used to test the card).
- More powerful but slower inference, harder to interpret, and requires more training data.
- Typically used as an ensemble member alongside gradient boosted trees, not as a replacement.

**Ensemble approach:**
- Combine rule engine score, XGBoost score, and neural network score.
- Final risk score is a weighted combination, or a meta-model that takes individual scores
  as input.

### 4.4 Training Pipeline

```
Training pipeline:

┌─────────────┐    ┌────────────────┐    ┌─────────────────┐    ┌─────────────┐
│ Transaction  │    │  Label         │    │  Feature         │    │   Model     │
│ Event Store  │───▶│  Assignment    │───▶│  Engineering     │───▶│  Training   │
│ (Kafka/S3)   │    │  (fraud/legit) │    │  (aggregations,  │    │  (XGBoost)  │
└─────────────┘    └────────────────┘    │   velocity,      │    └──────┬──────┘
                                          │   device, etc.)  │           │
                                          └─────────────────┘           ▼
                                                                  ┌─────────────┐
                                                                  │  Evaluation  │
                                                                  │  (precision, │
                                                                  │   recall,    │
                                                                  │   AUC-ROC)   │
                                                                  └──────┬──────┘
                                                                         │
                                                                         ▼
                                                                  ┌─────────────┐
                                                                  │  Shadow      │
                                                                  │  Deployment  │
                                                                  │  (score but  │
                                                                  │  don't act)  │
                                                                  └──────┬──────┘
                                                                         │
                                                                         ▼
                                                                  ┌─────────────┐
                                                                  │  Production  │
                                                                  │  Rollout     │
                                                                  │  (gradual %) │
                                                                  └─────────────┘
```

**Label assignment** is the hard part:
- A transaction confirmed as fraud (via chargeback reason code = fraud) is labeled positive.
- A transaction with no chargeback after 120 days is labeled negative.
- Problem: labels arrive with a 30-120 day delay (the "label delay" problem). The model
  is always training on data that is weeks or months old.
- Problem: only a small fraction of fraud is reported via chargebacks. Some fraudulent
  transactions go undetected. This means the training data has label noise.

**Retraining cadence:**
- Daily or weekly retraining on the latest labeled data.
- Fraud patterns evolve — a model trained on last month's data will miss this month's
  new attack vectors.
- Shadow deployment before production: the new model scores live traffic in parallel with
  the production model, and its decisions are logged but not acted upon. Compare precision
  and recall before promoting.

### 4.5 Class Imbalance

Fraud is rare — typically 0.1-1% of all transactions [UNVERIFIED — exact rates vary by
merchant vertical and geography]. This severe class imbalance means:

- A model that predicts "not fraud" for every transaction achieves 99%+ accuracy but catches
  zero fraud.
- Solutions: oversampling (SMOTE), undersampling, class weights, focal loss.
- The primary evaluation metric should be precision-recall AUC, not accuracy.

### 4.6 Real-Time Inference Requirements

The ML model runs in the synchronous authorization path. It must be fast:
- **Latency budget: <50ms** for feature computation + model inference (within the total
  ~100ms budget for the fraud check).
- Features that require aggregation (velocity counts over time windows) must be pre-computed
  and stored in a low-latency data store (Redis, in-memory feature store).
- Model inference for gradient boosted trees is inherently fast (sub-millisecond for a
  single prediction).
- Batch feature computation (historical averages, BIN-level statistics) is done offline
  and cached.

---

## 5. 3D Secure Challenge (Layer 3)

### 5.1 What Is 3D Secure?

3D Secure (3DS) is an authentication protocol that adds a cardholder verification step
during online payment. The "3D" refers to three domains: the acquirer domain, the issuer
domain, and the interoperability domain (the card network).

- **3DS 1.0** (legacy): Redirects the cardholder to the issuing bank's website for
  authentication (typically an OTP or static password). Known for terrible UX — full-page
  redirects caused high cart abandonment rates (10-15% drop-off) [UNVERIFIED — exact rates
  varied by implementation and merchant].
- **3DS 2.0** (current): Risk-based authentication performed in-browser or in-app. The
  issuer receives rich transaction data (device info, transaction history) and can approve
  low-risk transactions without any challenge to the cardholder ("frictionless flow"). Only
  high-risk transactions trigger an active challenge (OTP, biometric, push notification).
  Drop-off rates are significantly lower than 3DS 1.0.

### 5.2 How 3DS Fits Into Fraud Detection

3DS is not the first line of defense — it is the safety net for gray-zone transactions.

```
Transaction risk score
    │
    ├── Score 0-20   → Auto-approve (no 3DS)
    ├── Score 20-65  → Approve (no 3DS, but monitor post-auth)
    ├── Score 65-85  → Trigger 3DS challenge ← This is the gray zone
    └── Score 85-100 → Auto-decline (3DS cannot save this)
```

The PSP's fraud engine decides whether to request 3DS. The issuer's own risk engine then
decides whether to challenge the cardholder or approve frictionlessly. Two independent
fraud evaluations happen:
1. PSP-side: "Should we request 3DS for this transaction?"
2. Issuer-side: "Given the 3DS data, should we challenge or approve frictionlessly?"

### 5.3 Liability Shift

The most important business aspect of 3DS is the **liability shift**:

- **Without 3DS:** If a fraudulent transaction is processed and the cardholder disputes it,
  the merchant (or PSP) bears the loss.
- **With 3DS (authenticated):** If the transaction was authenticated via 3DS and is later
  disputed as fraud, the liability shifts to the **issuer**. The issuer's bank absorbs
  the loss, not the merchant.

This liability shift is the primary financial incentive for merchants to implement 3DS.
However, 3DS adds friction, which reduces conversion. The trade-off is:
- Low-risk transactions: Skip 3DS to maximize conversion.
- High-risk transactions: Trigger 3DS to shift liability.
- Very-high-risk transactions: Decline outright (3DS cannot fix a clearly fraudulent transaction).

### 5.4 Risk-Based Authentication (3DS 2.0)

3DS 2.0 sends over 100 data elements to the issuer, including:
- Device information (screen size, language, timezone, Java/JS enabled).
- Cardholder account information (age of account, recent activity).
- Transaction specifics (amount, currency, merchant category).
- Shipping address and billing address match.
- Authentication history (has this card been authenticated before at this merchant?).

The issuer's Access Control Server (ACS) evaluates this data and decides:
- **Frictionless flow:** Approve without challenge. The cardholder sees no additional
  authentication step. Conversion is preserved and liability still shifts to the issuer.
- **Challenge flow:** Require the cardholder to authenticate (OTP, biometric, push
  notification to banking app).
- **Decline:** The issuer refuses authentication entirely.

The frictionless flow is the key innovation of 3DS 2.0 — it provides the liability shift
benefit of 3DS without the conversion cost, for the majority of low-risk transactions.

---

## 6. Post-Authorization Checks

Not all fraud signals are available at authorization time. Some checks are inherently
asynchronous and run after the transaction is authorized but before capture.

### 6.1 Why Post-Auth Checks Exist

- Some data sources are slow (>100ms) and cannot fit within the synchronous authorization
  latency budget.
- Some checks require enrichment from third-party APIs that have variable latency.
- Some signals are only meaningful in combination with post-authorization events (e.g.,
  shipping address added after payment).

### 6.2 Common Post-Auth Checks

**Device reputation services:**
- Query third-party device intelligence providers for a device risk score.
- Checks: Is this device associated with known fraud? Is it a virtual machine? Is it
  using a rooted/jailbroken OS? Has it been seen in fraud rings across other platforms?

**Email reputation:**
- Is the email address from a disposable domain (mailinator.com, temp-mail.org)?
- How old is the email address? (Newly created emails are higher risk.)
- Is the email associated with data breaches? (haveibeenpwned-style checks.)
- Does the email domain match the cardholder's corporate domain?

**Phone number intelligence:**
- Is the phone number a VoIP number (Google Voice, Twilio) vs. mobile carrier?
- VoIP numbers are higher risk because they are cheap and disposable.
- Does the phone number's carrier country match the card country?

**Shipping address analysis:**
- Is the shipping address a known freight forwarder or reshipping service?
- Does the shipping address match any known drop addresses?
- Is the shipping address a PO box? (Higher risk for high-value goods.)
- Distance between billing and shipping address.

**Graph-based analysis:**
- Build a graph connecting cards, devices, IPs, emails, addresses, and phone numbers.
- If a new transaction shares a device fingerprint with a previously confirmed fraud
  transaction, it is high risk — even if the card itself is new to the system.
- Graph analysis is powerful but computationally expensive — typically runs async.

### 6.3 Actions on Post-Auth Signals

If post-auth checks reveal high risk, the PSP can:
1. **Hold capture:** Delay the capture request and place the payment in manual review.
   The merchant is notified that the payment requires review.
2. **Flag for manual review:** Add the transaction to a review queue. A human analyst
   reviews the signals and decides to capture or void.
3. **Void the authorization:** If the fraud signal is very strong, void the authorization
   before capture. The cardholder is never charged. This is the cleanest outcome — no
   chargeback, no dispute.
4. **Notify the merchant:** Some PSPs expose risk signals to merchants and let the merchant
   decide. Stripe Radar, for example, surfaces the risk score and contributing factors in
   the Dashboard [INFERRED — based on publicly available Stripe Radar documentation].

---

## 7. Architecture — Where Fraud Checks Happen

### 7.1 Full Payment Flow with Fraud Checks

```
                         SYNCHRONOUS PATH (<100ms fraud budget)
                         ==========================================

Customer ──▶ Merchant ──▶ PSP API Gateway
                              │
                              ▼
                    ┌──────────────────┐
                    │  Payment Service  │
                    │  (orchestrator)   │
                    └────────┬─────────┘
                             │
               ┌─────────────┼─────────────┐
               │             │             │
               ▼             ▼             ▼
        ┌────────────┐ ┌──────────┐ ┌─────────────┐
        │ Idempotency │ │  Fraud   │ │ Payment     │
        │ Check       │ │  Engine  │ │ Method      │
        │ (Redis)     │ │          │ │ Resolution  │
        └────────────┘ │          │ └─────────────┘
                       │  ┌──────┴──────┐
                       │  │             │
                       ▼  ▼             ▼
                 ┌──────────┐   ┌────────────┐
                 │ Rule     │   │ ML Model   │
                 │ Engine   │   │ Scoring    │
                 │ (<1ms)   │   │ (<50ms)    │
                 └──────────┘   └────────────┘
                       │             │
                       └──────┬──────┘
                              │
                              ▼
                    ┌──────────────────┐
                    │  Risk Decision   │
                    │  ┌─────────────┐ │
                    │  │ ALLOW       │ │──▶ Proceed to acquirer
                    │  │ BLOCK       │ │──▶ Decline transaction
                    │  │ CHALLENGE   │ │──▶ Trigger 3D Secure
                    │  │ REVIEW      │ │──▶ Auth + hold for review
                    │  └─────────────┘ │
                    └──────────────────┘
                              │
                              │ (if ALLOW or REVIEW)
                              ▼
                    ┌──────────────────┐
                    │  Payment Router   │──▶ Acquirer ──▶ Card Network ──▶ Issuer
                    │  (smart routing)  │◀── Auth Response ◀── ─── ◀──────
                    └──────────────────┘
                              │
                              ▼
                    ┌──────────────────┐
                    │  Store Result     │
                    │  (DB + Event Log) │
                    └──────┬───────────┘
                           │
                           │ (async)
                           ▼

                    ASYNCHRONOUS PATH (no latency constraint)
                    ==========================================

               ┌───────────┬────────────┬──────────────┐
               │           │            │              │
               ▼           ▼            ▼              ▼
        ┌────────────┐ ┌──────────┐ ┌──────────┐ ┌──────────────┐
        │ Device     │ │ Email    │ │ Address  │ │ Graph        │
        │ Reputation │ │ Reputa-  │ │ Analysis │ │ Analysis     │
        │ Check      │ │ tion     │ │          │ │ (card-device │
        └────────┬───┘ └────┬─────┘ └────┬─────┘ │  IP links)  │
                 │          │            │       └──────┬───────┘
                 └──────────┴─────┬──────┘              │
                                  │                     │
                                  ▼                     ▼
                        ┌──────────────────┐  ┌─────────────────┐
                        │  Post-Auth Risk   │  │ Manual Review   │
                        │  Decision         │  │ Queue           │
                        │  (hold/void/flag) │  │ (human analyst) │
                        └──────────────────┘  └─────────────────┘
```

### 7.2 Latency Budget Breakdown

The total latency for a payment authorization (end-to-end from merchant request to response)
should be under 1-3 seconds. The fraud engine gets a fraction of that budget.

```
Total authorization latency budget: ~1000-3000ms

Component breakdown:
  ┌──────────────────────────────────────────────────────┐
  │ API Gateway + parsing              │     5-10ms      │
  │ Idempotency check (Redis)          │     1-5ms       │
  │ ─── FRAUD ENGINE ───────────────── │     ─────       │
  │   Rule engine evaluation           │     <1ms        │
  │   Feature retrieval (Redis/cache)  │     5-20ms      │
  │   ML model inference               │     5-30ms      │
  │   Risk decision logic              │     <1ms        │
  │ ─── FRAUD ENGINE TOTAL ─────────── │     10-50ms     │
  │ Payment routing decision           │     1-5ms       │
  │ Acquirer API call (network I/O)    │     200-2000ms  │
  │ DB write (payment record + event)  │     5-20ms      │
  │ Response serialization             │     1-5ms       │
  └──────────────────────────────────────────────────────┘
  Total:                                    ~250-2100ms

  Note: Acquirer API call dominates. Fraud engine must be <100ms
  to avoid adding perceptible latency.
```

### 7.3 Fraud Engine Internal Architecture

```
┌─────────────────────────────────────────────────────────────┐
│                       FRAUD ENGINE                           │
│                                                              │
│  ┌─────────────────┐    ┌──────────────────────────────┐    │
│  │  Feature Store   │    │  Rule Configuration Store    │    │
│  │  (Redis Cluster) │    │  (cached from DB)            │    │
│  │                  │    │                               │    │
│  │  - velocity      │    │  - per-merchant rules         │    │
│  │    counters      │    │  - global rules               │    │
│  │  - BIN stats     │    │  - blocklists                 │    │
│  │  - historical    │    │                               │    │
│  │    aggregates    │    └──────────────────────────────┘    │
│  └────────┬────────┘                  │                      │
│           │                           │                      │
│           ▼                           ▼                      │
│  ┌────────────────┐         ┌──────────────────┐            │
│  │  Feature        │         │  Rule Engine      │            │
│  │  Computation    │         │  (evaluate all    │            │
│  │  (enrich txn    │         │   applicable      │            │
│  │   with features)│         │   rules)          │            │
│  └────────┬────────┘         └────────┬─────────┘            │
│           │                           │                      │
│           ▼                           │                      │
│  ┌────────────────┐                   │                      │
│  │  ML Model       │                   │                      │
│  │  Server         │                   │                      │
│  │  (XGBoost /     │                   │                      │
│  │   LightGBM)     │                   │                      │
│  └────────┬────────┘                   │                      │
│           │                           │                      │
│           ▼                           ▼                      │
│  ┌─────────────────────────────────────────────────────┐    │
│  │               Risk Decision Engine                    │    │
│  │                                                       │    │
│  │  Inputs: rule_verdict, ml_risk_score, 3ds_eligible   │    │
│  │                                                       │    │
│  │  Logic:                                               │    │
│  │    if rule_verdict == BLOCK → DECLINE                 │    │
│  │    if ml_score > 85        → DECLINE                  │    │
│  │    if ml_score > 65        → CHALLENGE (3DS)          │    │
│  │    if rule_verdict == REVIEW → ALLOW + flag           │    │
│  │    else                    → ALLOW                    │    │
│  │                                                       │    │
│  │  Output: { decision, risk_score, reasons[], 3ds? }   │    │
│  └─────────────────────────────────────────────────────┘    │
│                                                              │
└─────────────────────────────────────────────────────────────┘
```

### 7.4 Graceful Degradation

What happens if the fraud engine is down?

This is a critical design decision. Options:
1. **Fail closed (block all transactions):** Safe but catastrophic for business — all
   payments stop.
2. **Fail open (allow all transactions):** Keeps revenue flowing but exposes the system to
   fraud.
3. **Fall back to rules only (recommended):** If the ML model service is unreachable, evaluate
   rules only. Rules are typically embedded in the payment service itself (no external call),
   so they are always available. Accept higher risk temporarily, alert the on-call team
   immediately, and restore ML scoring ASAP.

The circuit breaker pattern is essential here. If the ML model service fails N times in M
seconds, trip the circuit breaker and stop calling it. Fall back to rules. Periodically
attempt a health check to restore the circuit.

---

## 8. Dispute and Chargeback Handling

### 8.1 Chargeback Lifecycle

A chargeback is the card network's mechanism for reversing a transaction when the cardholder
disputes it. The lifecycle has strict deadlines enforced by the card networks.

```
Chargeback lifecycle:

Day 0-120: Cardholder notices issue
    │
    ▼
Cardholder contacts issuing bank
    │
    ▼
Issuer initiates chargeback
    │  (Reason code assigned — fraud, merchandise not received,
    │   not as described, duplicate, etc.)
    │
    ▼
Card network routes chargeback to acquirer
    │
    ▼
Acquirer forwards to PSP/merchant
    │
    ▼
┌──────────────────────────────────────────────────────────────┐
│  PSP/Merchant has 7-30 days to respond                       │
│  (deadline depends on card network and reason code)           │
│                                                               │
│  Option A: Accept the chargeback                              │
│    → Funds reversed, merchant absorbs loss + fee              │
│                                                               │
│  Option B: Dispute the chargeback (submit evidence)           │
│    → Submit compelling evidence to issuer via card network     │
│    → Evidence: receipts, delivery proof, communication logs,  │
│      AVS match, CVV match, 3DS authentication proof,          │
│      IP logs, device fingerprint, customer communication      │
└──────────────────────────────────────────────────────────────┘
    │
    ▼ (if disputed)
Issuer reviews evidence
    │
    ├── Merchant wins → chargeback reversed, funds returned
    │
    └── Merchant loses → chargeback stands, funds permanently reversed
         │
         ▼ (some networks allow)
    Pre-arbitration / arbitration (final appeal)
         │
         ├── Merchant wins → reversed
         └── Merchant loses → final (plus arbitration fee: $250-$500)
              [UNVERIFIED — arbitration fees vary by card network]
```

### 8.2 Chargeback Reason Codes

Card networks assign reason codes that categorize the dispute. The reason code determines
what evidence is most effective.

**Visa reason codes (selected):**
- 10.4 — Fraud / Card Absent Environment (CNP fraud).
- 13.1 — Merchandise / Services Not Received.
- 13.3 — Not as Described or Defective Merchandise.
- 13.6 — Credit Not Processed (refund was promised but not issued).
- 12.6 — Duplicate Processing.

**Mastercard reason codes (selected):**
- 4837 — No Cardholder Authorization (fraud).
- 4853 — Cardholder Dispute (goods/services).
- 4834 — Duplicate Processing.

[UNVERIFIED — specific reason codes should be verified against the latest Visa and
Mastercard documentation, as they are periodically updated.]

### 8.3 Evidence That Wins Disputes

The quality of evidence directly determines win rates. Key evidence types:

| Evidence Type | When It Helps | Example |
|---|---|---|
| **AVS match** | Fraud disputes | Billing address matches card's address on file |
| **CVV match** | Fraud disputes | CVV was provided and verified |
| **3DS authentication** | Fraud disputes | Transaction was authenticated via 3DS — liability shifted to issuer |
| **Delivery confirmation** | Not-received disputes | Tracking number showing delivery to billing/shipping address |
| **Signed delivery** | Not-received, high-value | Signature confirmation of delivery |
| **Customer communication** | All disputes | Emails/chats showing customer acknowledged receipt |
| **Refund policy** | All disputes | Customer agreed to refund policy at checkout |
| **IP/device logs** | Fraud disputes | IP address matches cardholder's known location |
| **Usage logs** | Digital goods | Logs showing the customer used the digital product after purchase |

**Win rates by evidence quality:**
- Dispute with no evidence submitted: ~0% win rate.
- Dispute with basic evidence (AVS + CVV match): 20-30% win rate [UNVERIFIED].
- Dispute with strong evidence (3DS + delivery proof + customer communication): 60-80%
  win rate [UNVERIFIED].
- Dispute with 3DS authentication proof (fraud reason code): very high win rate because
  the liability shift means the issuer should not have accepted the dispute in the first
  place [INFERRED].

### 8.4 Chargeback Rate Thresholds

Card networks monitor merchants' chargeback rates and impose escalating penalties:

**Visa Dispute Monitoring Programs:**
- **Standard:** Chargeback rate < 0.9% and fewer than 100 disputes/month — no action.
- **Visa Dispute Monitoring Program (VDMP):** Chargeback rate >= 0.9% OR >= 100
  disputes/month — merchant enters monitoring. Fines start at $50/dispute after 4 months
  in the program.
- **Visa Fraud Monitoring Program (VFMP):** Fraud-specific chargeback rate >= 0.9% AND
  fraud amount >= $75,000 — merchant enters fraud monitoring with escalating fines.

[UNVERIFIED — these thresholds are based on publicly discussed figures as of mid-2025.
Visa updates its programs periodically. The exact current thresholds should be verified
against Visa's official Core Rules documentation.]

**Mastercard Excessive Chargeback Program:**
- Similar thresholds — chargeback rate >= 1.0% triggers enrollment.
- Escalating monthly fines ($1,000-$200,000) depending on duration in the program.

[UNVERIFIED — verify against Mastercard's Security Rules and Procedures manual.]

**Consequences of sustained high chargeback rates:**
1. Monthly fines from card networks.
2. Acquirer may increase processing fees or require a reserve (hold-back of settlement funds).
3. Acquirer may terminate the merchant account.
4. Merchant placed on the MATCH list (Member Alert to Control High-Risk Merchants) —
   effectively blacklisted from getting a new merchant account with any acquirer for 5 years.
   [UNVERIFIED — MATCH list duration should be verified.]

### 8.5 PSP's Role in Dispute Management

A PSP helps merchants manage disputes by:
1. **Automated evidence collection:** When a dispute is received, automatically compile
   available evidence (AVS result, CVV result, 3DS authentication, IP logs, delivery info
   if integrated with shipping providers).
2. **Evidence submission API:** Provide merchants with an API and dashboard to upload
   additional evidence (receipts, communication logs).
3. **Deadline tracking:** Track response deadlines and alert merchants before they expire.
4. **Analytics:** Dashboard showing dispute rates, win rates, reason code breakdown, and
   trends over time.
5. **Prevention signals:** Feed dispute data back into the fraud engine so that future
   transactions with similar patterns are flagged.

---

## 9. Precision vs Recall Trade-off

### 9.1 The Core Tension

In fraud detection, precision and recall have direct business consequences:

- **Precision** = (True Positives) / (True Positives + False Positives)
  - "Of the transactions I blocked, what fraction were actually fraud?"
  - Low precision means many false positives — legitimate customers are blocked.

- **Recall** = (True Positives) / (True Positives + False Negatives)
  - "Of all the fraud that occurred, what fraction did I catch?"
  - Low recall means fraud slips through — financial losses and chargebacks.

You cannot maximize both simultaneously. Moving the risk score threshold changes the trade-off:

```
Risk Score Threshold: 50 (aggressive — block more)
  ┌──────────────────────────────────┐
  │  Blocked: 5% of all transactions  │
  │  Precision: 60%                   │  ← 40% of blocked txns were legitimate
  │  Recall: 95%                      │  ← Caught 95% of fraud
  │  False positive rate: 2%          │
  │  Revenue impact: -2% (blocked     │
  │    legitimate customers)           │
  └──────────────────────────────────┘

Risk Score Threshold: 80 (conservative — block less)
  ┌──────────────────────────────────┐
  │  Blocked: 0.5% of all txns       │
  │  Precision: 90%                   │  ← Only 10% of blocked txns were legitimate
  │  Recall: 60%                      │  ← Only caught 60% of fraud
  │  False positive rate: 0.05%       │
  │  Revenue impact: -0.05% (very few │
  │    legitimate customers blocked)   │
  │  Fraud loss: higher               │
  └──────────────────────────────────┘
```

### 9.2 Optimal Threshold by Merchant Vertical

The right threshold depends on the cost structure:

| Vertical | Optimal Bias | Reasoning |
|---|---|---|
| **Luxury goods** (Rolex, jewelry) | High recall (catch more fraud) | Each fraud incident costs thousands. False positives are annoying but customers will retry. |
| **Digital subscriptions** ($9.99/mo) | High precision (fewer false positives) | Each fraud incident costs $10. Blocking a legitimate customer costs months of LTV ($120+/yr). |
| **Travel / airlines** | High recall | Tickets are high-value and non-recoverable once the flight departs. |
| **Gaming / micro-transactions** ($0.99-$4.99) | High precision | Very low per-transaction fraud cost. Blocking legitimate gamers destroys engagement. |
| **Crypto exchanges** | Very high recall | Crypto is irreversible — once sent, it cannot be recovered via chargeback. |
| **Physical goods (standard e-commerce)** | Balanced | Moderate per-transaction fraud cost. Goods can sometimes be intercepted before delivery. |

### 9.3 Dollar-Optimal Decision Making

Rather than a binary block/allow, an economically optimal fraud system weighs the expected
costs:

```
Expected cost of ALLOWING a transaction:
  = P(fraud) * fraud_cost
  = P(fraud) * (transaction_amount + chargeback_fee + operational_cost)

Expected cost of BLOCKING a transaction:
  = P(legitimate) * blocked_revenue_cost
  = (1 - P(fraud)) * (transaction_amount * margin + customer_LTV_impact)

Decision:
  ALLOW if Expected_cost_of_allowing < Expected_cost_of_blocking
  BLOCK if Expected_cost_of_allowing > Expected_cost_of_blocking
```

This framework explains why:
- Blocking a $5,000 transaction with P(fraud) = 30% makes sense (expected fraud cost =
  $1,500+ vs. expected blocked revenue = $3,500 * margin).
- Blocking a $10 transaction with P(fraud) = 30% rarely makes sense (expected fraud cost =
  $3 + fees vs. expected blocked revenue = $7 * margin + customer LTV impact).

### 9.4 Monitoring the Trade-off in Production

Key metrics to track:
- **Fraud rate:** Percentage of transactions that result in fraud chargebacks (target: <0.5%).
- **False positive rate:** Percentage of legitimate transactions blocked (target: <1%).
- **Review rate:** Percentage of transactions sent to manual review (target: <2% —
  manual review does not scale).
- **3DS challenge rate:** Percentage of transactions that trigger 3DS (target: varies).
- **Conversion impact:** A/B test the fraud engine — what is the conversion rate with
  fraud engine ON vs. OFF? The difference is the cost of fraud prevention.

---

## 10. Stripe Radar as Reference Architecture

### 10.1 What Is Stripe Radar?

Stripe Radar is Stripe's fraud detection product, integrated directly into Stripe's payment
processing pipeline. It is available to all Stripe merchants and runs automatically on every
transaction.

[NOTE: The following is based on publicly available Stripe documentation and blog posts.
Web sources could not be verified live during authoring. Specific internal implementation
details are marked as inferred.]

### 10.2 Network-Level Advantage

Stripe's primary competitive advantage in fraud detection is its **network-level view**:

- Stripe processes payments for millions of merchants across many countries.
- When a card is used fraudulently at Merchant A, Stripe can flag that card immediately
  when it appears at Merchant B — even if Merchant B has never seen that card before.
- This network effect means Stripe's fraud model has access to signals that no individual
  merchant could have.

Publicly, Stripe has stated that Radar evaluates signals from across their entire network
to detect fraud. A card involved in a chargeback at one merchant contributes to the risk
score for that card at every other merchant on the Stripe network.
[INFERRED — the exact mechanism is not publicly documented, but the network-level
advantage is prominently described in Stripe's Radar marketing and documentation.]

### 10.3 Stripe Radar's Layered Approach

Based on publicly available documentation, Stripe Radar uses:

1. **Machine learning models** trained on data from across the Stripe network. Stripe has
   described using features such as card details, device information, IP address, customer
   behavior patterns, and network-wide signals.

2. **Radar Rules** — a configurable rule engine where merchants can write custom rules
   using a domain-specific language. Examples from Stripe's documentation:
   ```
   Block if :card_country: != :ip_country:
   Review if :amount_in_usd: > 1000
   Block if :is_disposable_email:
   Allow if :customer: is on @trusted_customers
   ```
   Merchants can create rules to block, allow, or send to review based on over 100
   attributes.

3. **Radar for Fraud Teams** (paid tier) — adds manual review workflows, custom allow/block
   lists, and deeper analytics.
   [UNVERIFIED — pricing tiers and exact feature set should be verified against current
   Stripe documentation.]

### 10.4 Stripe Radar Risk Scores

Stripe provides a risk score (0-100) and a risk level (normal, elevated, highest) for
every charge. Merchants can use these signals to make their own decisions or let Stripe's
default rules handle it.

Stripe's documentation indicates that the risk assessment includes:
- Whether the card number, CVC, and postal code checks passed.
- The card's chargeback history across the Stripe network.
- Spending patterns associated with the card.
- Device and behavioral signals from Stripe.js / Stripe Elements.

### 10.5 3DS Integration

Stripe integrates 3DS (called "3D Secure" in Stripe's API) with Radar. Merchants can
configure Radar rules to trigger 3DS for transactions above a certain risk score.
Stripe supports both 3DS 1.0 and 3DS 2.0, and recommends 3DS 2.0 for better conversion
rates and the frictionless authentication flow.

### 10.6 Lessons from Stripe Radar for System Design Interviews

When referencing Stripe Radar in an interview:
- Emphasize the **network effect** — this is the single most important architectural insight.
  A PSP that processes payments for many merchants can build a fraud model that no single
  merchant can match.
- Mention the **layered approach** — ML model + configurable rules + 3DS. Each layer
  addresses a different part of the risk spectrum.
- Highlight that Radar runs in the **synchronous payment path** — it must be fast. This
  implies real-time feature stores, low-latency model serving, and aggressive caching.
- Note that Stripe exposes fraud signals to merchants via the API and Dashboard, enabling
  merchant-specific customization on top of Stripe's global model.

---

## 11. PSP vs Issuer Fraud Detection — Two Sides of the Same Coin

### 11.1 The Two Perspectives

Fraud detection happens at two independent points in the payment flow, with fundamentally
different perspectives:

```
                    PSP/Acquirer Side                    Issuer Side
                    ─────────────────                   ─────────────
┌─────────────────────────────────────────────────────────────────────────────┐
│                                                                             │
│  Customer ──▶ Merchant ──▶ PSP ──▶ Acquirer ──▶ Card Network ──▶ Issuer    │
│                            ▲                                      ▲         │
│                            │                                      │         │
│                    PSP fraud engine                     Issuer fraud engine  │
│                    evaluates HERE                       evaluates HERE       │
│                                                                             │
└─────────────────────────────────────────────────────────────────────────────┘
```

### 11.2 PSP-Side (Acquirer-Side) Fraud Detection

**Question being answered:** "Is this transaction legitimate? Is this merchant trustworthy?
Should I process this transaction?"

**Signals available:**
- Merchant identity and history (is this merchant high-risk?).
- Transaction details (amount, currency, MCC).
- Customer identity signals (device, IP, email, phone).
- Network-wide card behavior (has this card been seen in fraud across other merchants?).
- Velocity at the merchant level (is this merchant suddenly processing 10x normal volume?).

**Signals NOT available:**
- The cardholder's account balance or credit limit.
- The cardholder's full transaction history (only what the PSP has seen).
- The cardholder's personal information on file with the issuer.
- The cardholder's stated spending preferences or travel notifications.

### 11.3 Issuer-Side Fraud Detection

**Question being answered:** "Is my cardholder actually making this purchase? Is their card
being misused?"

**Signals available:**
- Cardholder's full transaction history (across all merchants, all PSPs).
- Cardholder's typical spending patterns (geography, amount, category, time of day).
- Cardholder's account status (credit limit, recent payments, account age).
- Whether the cardholder has filed a travel notification.
- Whether the cardholder has enabled transaction alerts.
- Real-time location data (some issuers compare transaction location with the cardholder's
  phone GPS location via their banking app) [UNVERIFIED — availability varies by issuer].

**Signals NOT available:**
- Merchant-side signals (device fingerprint, IP, email).
- Network-wide merchant behavior.
- Whether the customer is a new or returning customer at this specific merchant.

### 11.4 Why Both Are Needed

Neither side alone can catch all fraud:

| Fraud Type | PSP Detects | Issuer Detects | Who Detects Better |
|---|---|---|---|
| Stolen card used at many merchants rapidly | Yes (network-wide velocity) | Yes (unusual velocity for cardholder) | **Both, complementary** |
| Stolen card used once, high-value | Maybe (amount threshold) | **Yes** (unusual amount for this cardholder) | **Issuer** |
| ATO (account takeover at merchant) | **Yes** (new device, changed shipping address) | Maybe (transaction looks normal) | **PSP** |
| Friendly fraud | No (txn is legitimate at auth time) | No (txn is legitimate at auth time) | **Neither at auth time** |
| Merchant fraud (fake merchant) | **Yes** (merchant behavior patterns) | No (each individual txn looks normal) | **PSP** |
| Card testing (bulk small txns) | **Yes** (velocity + decline patterns) | Yes (unusual pattern for cardholder) | **PSP** (sees pattern across cards) |

### 11.5 The 3DS Handshake Between PSP and Issuer

3D Secure is the formal mechanism where PSP-side and issuer-side fraud detection collaborate:

1. PSP's fraud engine evaluates the transaction and decides it is in the gray zone.
2. PSP initiates a 3DS request, sending rich transaction and device data to the issuer.
3. Issuer's fraud engine evaluates its own signals plus the data from the PSP.
4. Issuer decides: frictionless approve, challenge, or decline.

This is the only point in the payment flow where both sides' fraud intelligence is combined.

---

## 12. Operational Concerns

### 12.1 Model Monitoring and Drift

Fraud patterns change constantly. A model that was effective last month may be ineffective
today. Key operational concerns:

- **Feature drift:** The distribution of input features changes over time (e.g., during
  COVID-19, online spending patterns changed dramatically — models trained on pre-COVID
  data produced many false positives).
- **Concept drift:** The relationship between features and fraud changes (e.g., transactions
  from a new country may shift from high-risk to low-risk as a merchant legitimately expands
  to that market).
- **Adversarial adaptation:** Fraudsters actively probe the system to find its weaknesses.
  If the model blocks transactions from VPN IPs, fraudsters switch to residential proxies.

**Monitoring metrics:**
- Model risk score distribution over time (sudden shifts indicate drift).
- Precision and recall computed on rolling windows of labeled data.
- False positive rate monitored via merchant complaints and customer contacts.
- A/B testing new models against the production model on live traffic.

### 12.2 Human-in-the-Loop Review

Some transactions are too risky to auto-approve but not risky enough to auto-decline.
These go to manual review.

**The manual review queue:**
- Staffed by trained fraud analysts.
- Analysts see the transaction details, risk score, contributing factors, customer history,
  and device/IP intelligence.
- Analyst decides: approve, decline, or escalate.
- Analyst decisions are fed back as training labels for the ML model.

**The scaling problem:** Manual review does not scale. If 5% of transactions go to review
and you process 1 million transactions per day, that is 50,000 reviews per day. At 5 minutes
per review and 8-hour shifts, you need ~520 analysts. The goal is to keep the review rate
under 1-2% [INFERRED — exact industry benchmarks vary].

### 12.3 Feature Store Architecture

Real-time fraud scoring requires pre-computed features available with sub-millisecond latency:

```
Feature computation pipeline:

┌─────────────┐     ┌──────────────┐     ┌──────────────────────┐
│ Transaction  │     │ Stream       │     │ Real-time Feature    │
│ Events       │────▶│ Processor    │────▶│ Store (Redis)        │
│ (Kafka)      │     │ (Flink/Kafka │     │                      │
└─────────────┘     │  Streams)    │     │ - velocity counters   │
                     └──────────────┘     │   (card, IP, device) │
                                          │ - running aggregates  │
                                          │ - last-seen timestamps│
                                          └──────────────────────┘
                                                    ▲
                                                    │ query at
                                                    │ scoring time
                                                    │
┌─────────────┐     ┌──────────────┐               │
│ Historical   │     │ Batch        │     ┌─────────┴────────────┐
│ Transaction  │────▶│ Feature      │────▶│ Offline Feature       │
│ Data (S3/DB) │     │ Pipeline     │     │ Store (Redis/DB)      │
└─────────────┘     │ (Spark)      │     │                       │
                     └──────────────┘     │ - BIN-level stats     │
                                          │ - merchant-level stats│
                                          │ - historical averages │
                                          └───────────────────────┘
```

### 12.4 Explainability

Regulators and merchants require explanations for why a transaction was blocked:
- Gradient boosted trees provide feature importance (SHAP values) for each prediction.
- The fraud engine should return a list of contributing factors with each decision:
  `["velocity_high", "geo_mismatch", "new_device", "amount_unusual"]`.
- This transparency enables merchants to whitelist trusted patterns and reduces complaints
  from false positives.

### 12.5 Feedback Loops

The fraud engine must incorporate feedback to improve over time:

```
Feedback loop:

Transaction → Fraud Engine → Decision → Outcome
                                           │
                     ┌─────────────────────┘
                     │
              ┌──────┴──────────────────────────────────────────┐
              │                                                  │
              ▼                                                  ▼
    Chargeback received                              No chargeback (120 days)
    (label = fraud)                                  (label = legitimate)
              │                                                  │
              └──────────────┬───────────────────────────────────┘
                             │
                             ▼
                      Training data
                             │
                             ▼
                      Model retraining
                             │
                             ▼
                      Updated model deployed
```

**The delayed label problem:** You do not know if a transaction is fraudulent for 30-120
days (until the chargeback window closes). This means:
- The model is always training on stale labels.
- New fraud patterns may go undetected for weeks before labeled data accumulates.
- Mitigation: Use early signals (customer disputes via merchant, card reported stolen) as
  early labels before chargebacks arrive.

---

## 13. Interview Tips — Fraud Detection Questions

### 13.1 What Interviewers Want to Hear

1. **Layered defense:** Do not describe only one technique. Show the full stack:
   rules (fast, brittle) + ML (nuanced, slower) + 3DS (authentication) + post-auth (async).

2. **Trade-offs:** Blocking fraud vs. blocking legitimate customers. Every decision has a
   cost on both sides. Show you understand the business impact, not just the technical
   implementation.

3. **Latency awareness:** Fraud checks are on the synchronous authorization path. You
   cannot add 500ms of latency. Show you understand the latency budget and how to stay
   within it (pre-computed features, fast model inference, circuit breakers).

4. **Network effect:** A PSP that sees transactions across millions of merchants has a
   massive advantage over any individual merchant. This is the single most important
   architectural insight in payment fraud detection.

5. **Feedback loop:** Fraud models need labeled data. Labels arrive with a 30-120 day
   delay. Show you understand this constraint and how it affects model freshness.

### 13.2 Common Mistakes

- **Describing only ML without rules:** Rules are essential for speed, explainability, and
  as a fallback when ML is down.
- **Ignoring friendly fraud:** It is a huge portion of chargebacks and cannot be prevented
  at authorization time.
- **Ignoring the latency constraint:** "We run a deep learning model on 200 features" is
  meaningless if inference takes 2 seconds.
- **Not mentioning graceful degradation:** What happens when the fraud engine is down?
  This is a production reality.
- **Treating fraud detection as a standalone system:** It is integrated into the payment
  flow. Show where it fits in the architecture.

### 13.3 L5/L6/L7 Answer Differentiation

| Aspect | L5 (SDE-2) | L6 (SDE-3) | L7 (Principal) |
|---|---|---|---|
| **Architecture** | "We use ML to score transactions." | "Layer 1 rules (<1ms) + Layer 2 ML scoring (<50ms) in the sync path. Async post-auth checks for device/email reputation. Circuit breaker falls back to rules if ML is down." | "The fraud engine is a co-processor on the authorization path with a strict 50ms budget. Feature store uses dual-write (stream + batch) to handle both real-time velocity and historical aggregates. Model serving uses pre-compiled decision trees for sub-ms inference. Network-wide embedding vectors are pre-computed hourly." |
| **Trade-offs** | "We want to block fraud." | "Precision-recall trade-off. Threshold tuned per merchant vertical — luxury goods need high recall, digital subscriptions need high precision." | "Dollar-optimal decision framework: compare expected cost of allowing (P(fraud) * loss) vs. expected cost of blocking (P(legitimate) * margin + LTV impact). Different thresholds for different transaction risk tiers. A/B testing the conversion impact of fraud rules." |
| **Data/ML** | "XGBoost model." | "XGBoost on tabular features + velocity counters in Redis. Retraining weekly on labeled chargeback data. Shadow deployment before production rollout. Class imbalance handled via sample weights." | "Dual model architecture: fast decision tree for p50 transactions, slower neural net for gray-zone transactions only. Feature store on Flink with exactly-once semantics. Label delay mitigated by early signal integration (merchant-reported fraud). Concept drift monitored via PSI (Population Stability Index) on feature distributions." |
| **Operational** | "We monitor the model." | "Model drift monitoring. Alert on precision/recall drops. Manual review queue for gray-zone transactions. Feedback loop from chargebacks to training data." | "Canary deployment of new models on 1% of traffic with automatic rollback if false positive rate increases. Fraud model versioning with A/B testing framework. Red team exercises where internal team simulates novel fraud patterns to test model robustness." |

---

## Summary

The fraud detection system in a payment platform is a multi-layered, latency-constrained,
continuously evolving system that sits on the critical path of every transaction.

**Key takeaways:**

1. **Three synchronous layers:** Rules (<1ms) + ML scoring (<50ms) + 3DS challenge.
   Total fraud engine budget: <100ms.

2. **Async post-auth layer:** Device reputation, email reputation, address analysis, graph
   analysis. Can trigger capture holds or manual review.

3. **Network effect is the moat:** A PSP processing payments for millions of merchants has
   fraud signals that no individual merchant can match.

4. **Precision vs. recall is a business decision:** The optimal threshold depends on
   merchant vertical, transaction size, and the relative cost of fraud vs. false positives.

5. **Chargeback management is fraud prevention's downstream counterpart:** High chargeback
   rates (>1%) threaten the merchant's ability to accept card payments at all.

6. **PSP and issuer detect fraud from different perspectives:** PSP sees merchant/network
   signals. Issuer sees cardholder signals. 3DS is where they collaborate.

7. **Graceful degradation is essential:** When the ML model is down, fall back to rules.
   Never fail closed (block all payments) unless there is evidence of a systemic attack.

8. **The fraud engine must be explainable:** Merchants and regulators need to know why a
   transaction was blocked. Black-box models are not acceptable as the sole decision maker.
