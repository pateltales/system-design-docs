# Smart Payment Routing & Acquiring Strategy

> **Scope**: How a PSP (Payment Service Provider) like Stripe or Razorpay selects the optimal acquirer for each transaction. This doc covers routing factors, static vs dynamic routing, cascade retries, failover, cost optimization, local acquiring, and routing engine architecture.

> **Verification note**: WebSearch and WebFetch were unavailable during creation. Claims are based on well-established industry knowledge. Specific numbers and company internals are marked with `[UNVERIFIED]` or `[INFERRED]` where appropriate per project accuracy requirements.

---

## Table of Contents

1. [Why Routing Matters](#1-why-routing-matters)
2. [Routing Factors](#2-routing-factors)
3. [Static Routing Rules](#3-static-routing-rules)
4. [Dynamic / Smart Routing](#4-dynamic--smart-routing)
5. [Cascade Retries](#5-cascade-retries)
6. [Failover & Circuit Breakers](#6-failover--circuit-breakers)
7. [Cost Optimization](#7-cost-optimization)
8. [Local Acquiring](#8-local-acquiring)
9. [Routing Engine Architecture](#9-routing-engine-architecture)
10. [Contrast: Single-Acquirer vs Multi-Acquirer PSP](#10-contrast-single-acquirer-vs-multi-acquirer-psp)

---

## 1. Why Routing Matters

A PSP is not a single pipe to a single bank. A mature PSP integrates with **dozens of acquirers** across geographies. When a merchant calls `POST /payments`, the PSP must decide *which acquirer* processes this specific transaction.

This decision is not arbitrary. It directly impacts four dimensions:

| Dimension | Impact of Routing | Magnitude |
|-----------|------------------|-----------|
| **Success rate** | Different acquirers have different approval rates for the same card type / geography | 5-15% variance `[UNVERIFIED — commonly cited in PSP marketing; exact range varies by card type and geography]` |
| **Cost** | Interchange + acquirer markup differ by acquirer, card type, and geography | Can vary by 0.5-1.5% of transaction value `[UNVERIFIED]` |
| **Latency** | Acquirer response times vary; some acquirers route through more network hops | 100ms-2s variance `[UNVERIFIED]` |
| **Currency conversion** | Cross-border transactions incur FX markup; local acquiring avoids this | 1-3% FX markup avoided with local acquiring `[UNVERIFIED]` |

**Why 5-15% success rate variance exists**: Different acquirers have different relationships with different issuing banks. Acquirer A might have a direct connection to Bank X's authorization system, while Acquirer B routes through an intermediary. Direct connections yield faster responses and higher approval rates. Additionally, acquirers have different fraud scoring thresholds, different retry logic, and different BIN routing tables.

**The business case is clear**: If a PSP processes $1 billion/month and smart routing improves success rates by even 2%, that is $20 million/month in additional approved transactions — a massive revenue lift for merchants.

```
Without smart routing:

  Merchant ──── POST /payments ────► PSP ────► Single Acquirer ────► Card Network ────► Issuer
                                                    │
                                              If decline → done.
                                              No alternatives.

With smart routing:

  Merchant ──── POST /payments ────► PSP ──┬──► Acquirer A (primary, selected by routing engine)
                                           │
                                           ├──► Acquirer B (cascade retry on soft decline)
                                           │
                                           └──► Acquirer C (failover if A & B are down)
```

---

## 2. Routing Factors

The routing engine evaluates multiple signals to select the optimal acquirer. These fall into two categories: **transaction attributes** (known at request time) and **acquirer performance metrics** (tracked continuously).

### Transaction Attributes (Known at Request Time)

| Factor | Description | Why It Matters |
|--------|-------------|----------------|
| **Card network** | Visa, Mastercard, Amex, Discover, RuPay, UnionPay | Some acquirers only support specific networks. Amex is often direct (acquirer = Amex itself). RuPay is India-only (NPCI network). |
| **Card type** | Credit, debit, prepaid, corporate | Interchange rates differ dramatically. Debit interchange is regulated (lower) in many markets. Prepaid cards have higher decline rates. |
| **Issuing country** | Country of the bank that issued the card (derived from BIN) | Determines whether transaction is domestic or cross-border. Local acquirer in the issuing country yields higher success rates. |
| **Card BIN** | First 6-8 digits of card number | BIN identifies the issuer. Some acquirers have better relationships with specific issuers. BIN tables map BIN ranges to issuer + country + card type. |
| **Currency** | Transaction currency vs card's billing currency | If they differ, FX conversion is needed. Some acquirers offer better FX rates. Local acquiring can avoid FX entirely. |
| **Transaction amount** | Value of the transaction | High-value transactions may have different decline patterns. Some acquirers have per-transaction limits. |
| **MCC** | Merchant Category Code (e.g., 5411 = grocery, 7011 = hotel) | Some acquirers specialize in specific verticals. Certain MCCs have higher chargeback rates, affecting acquirer willingness. |
| **3DS status** | Whether the transaction has been authenticated via 3D Secure | 3DS-authenticated transactions have liability shift; some acquirers prefer them. |
| **Recurring vs one-time** | Whether this is a subscription/recurring charge | Recurring transactions use stored credentials; some acquirers handle MIT (Merchant Initiated Transactions) better. |

### Acquirer Performance Metrics (Tracked Continuously)

| Metric | How It's Tracked | Update Frequency |
|--------|-----------------|------------------|
| **Success rate** | Per acquirer, segmented by card network + issuing country + card type | Sliding window (last 1 hour, 6 hours, 24 hours) |
| **Latency** | p50, p95, p99 response time per acquirer | Real-time (updated per transaction) |
| **Uptime** | Health check pass/fail rate | Periodic health checks (every 10-30 seconds) |
| **Error rate** | Rate of timeouts and system errors (distinct from business declines) | Sliding window |
| **Cost** | Effective interchange + markup per acquirer per card type | Updated when acquirer pricing changes (monthly/quarterly) |

---

## 3. Static Routing Rules

The simplest routing approach: deterministic rules that map transaction attributes to acquirers.

### Example Static Rule Set

```
RULE 1:  IF card_network = "Visa"     AND issuing_country = "US"   THEN route_to = "Acquirer_A"
RULE 2:  IF card_network = "MC"       AND issuing_country = "US"   THEN route_to = "Acquirer_A"
RULE 3:  IF card_network = "Amex"                                  THEN route_to = "Amex_Direct"
RULE 4:  IF card_network = "RuPay"                                 THEN route_to = "Acquirer_India"
RULE 5:  IF issuing_country = "IN"    AND card_network != "RuPay"  THEN route_to = "Acquirer_India"
RULE 6:  IF issuing_country IN ("GB", "DE", "FR", ...)             THEN route_to = "Acquirer_EU"
RULE 7:  DEFAULT                                                   THEN route_to = "Acquirer_Global"
```

### Pros and Cons

| Pros | Cons |
|------|------|
| Simple to implement and understand | Does not adapt to real-time acquirer performance |
| Deterministic — easy to audit and debug | Ignores success rate differences within a rule |
| Low computational overhead | No failover logic (if Acquirer_A is down, rule still routes to it) |
| Good starting point for early-stage PSPs | Cannot optimize for cost vs success rate trade-off |

**When static rules are sufficient**: Early-stage PSPs with 1-2 acquirers. The rules simply determine *which* acquirer can process this card type, not which is *optimal*. With a single domestic acquirer and a single international acquirer, there is no real "choice" — just capability-based routing.

**When static rules break down**: As soon as you have 2+ acquirers that can both process the same card type in the same geography, static rules leave success rate and cost improvements on the table.

---

## 4. Dynamic / Smart Routing

Dynamic routing replaces static rules with a **real-time optimization engine** that selects the acquirer with the highest expected success probability for each specific transaction.

### Core Concept

For each incoming transaction, the routing engine:

1. Identifies all **eligible acquirers** (those that can process this card network + currency + geography)
2. For each eligible acquirer, calculates an **expected success score** based on recent performance for similar transactions
3. Optionally applies **cost weighting** (penalize expensive acquirers)
4. Selects the acquirer with the highest weighted score

### The Decision Algorithm

```
FUNCTION select_acquirer(transaction):
    eligible = filter_acquirers_by_capability(transaction)

    IF eligible is empty:
        RETURN error("No acquirer supports this transaction")

    best_acquirer = null
    best_score = -1

    FOR EACH acquirer IN eligible:
        // Step 1: Get recent success rate for this transaction profile
        segment_key = (acquirer, card_network, issuing_country, card_type)
        success_rate = get_sliding_window_success_rate(segment_key, window=1_HOUR)

        // Step 2: If insufficient data in 1h window, widen to 24h
        IF sample_count(segment_key, window=1_HOUR) < MIN_SAMPLE_THRESHOLD:
            success_rate = get_sliding_window_success_rate(segment_key, window=24_HOURS)

        // Step 3: Check acquirer health
        IF acquirer.circuit_breaker == OPEN:
            CONTINUE  // skip unhealthy acquirers

        // Step 4: Calculate weighted score
        cost_per_txn = get_effective_cost(acquirer, transaction)
        latency_p99 = get_latency_p99(acquirer)

        score = (W_SUCCESS * success_rate)
              - (W_COST * normalize(cost_per_txn))
              - (W_LATENCY * normalize(latency_p99))

        IF score > best_score:
            best_score = score
            best_acquirer = acquirer

    RETURN best_acquirer
```

### Weight Configuration

The weights (`W_SUCCESS`, `W_COST`, `W_LATENCY`) are configurable per merchant or globally:

| Strategy | W_SUCCESS | W_COST | W_LATENCY | Use Case |
|----------|-----------|--------|-----------|----------|
| **Maximize success** | 0.9 | 0.05 | 0.05 | Default — most merchants want highest approval rate |
| **Minimize cost** | 0.5 | 0.4 | 0.1 | High-volume, low-margin merchants (grocery, utilities) |
| **Minimize latency** | 0.5 | 0.1 | 0.4 | Real-time / gaming payments where UX is critical |

### Sliding Window Success Rate Tracking

The routing engine maintains per-segment success rates using a **sliding window**:

```
Segment key: (Acquirer_A, Visa, US, Credit)

Time Window:  [------- last 1 hour -------]
Transactions:  ✓ ✓ ✗ ✓ ✓ ✓ ✗ ✓ ✓ ✓ ✓ ✗ ✓ ✓ ✓
Success rate:  12/15 = 80.0%

Segment key: (Acquirer_B, Visa, US, Credit)

Time Window:  [------- last 1 hour -------]
Transactions:  ✓ ✓ ✓ ✓ ✗ ✓ ✓ ✓ ✓ ✓ ✓ ✓ ✓ ✗ ✓
Success rate:  13/15 = 86.7%

Routing decision: Route Visa/US/Credit to Acquirer_B (86.7% > 80.0%)
```

**Implementation**: Typically a **time-bucketed counter** in Redis or an in-memory data structure. Each bucket tracks (success_count, total_count) for a time window (e.g., 5-minute buckets). The sliding window sums the last N buckets.

### Decision Tree View

```
                          Incoming Transaction
                                  │
                    ┌─────────────┴─────────────┐
                    │ Extract: card_network,     │
                    │ issuing_country, card_type, │
                    │ currency, amount, MCC       │
                    └─────────────┬─────────────┘
                                  │
                    ┌─────────────┴─────────────┐
                    │ Filter: Which acquirers    │
                    │ support this combination?  │
                    └─────────────┬─────────────┘
                                  │
                  ┌───────────────┼───────────────┐
                  ▼               ▼               ▼
           Acquirer A       Acquirer B       Acquirer C
           ┌────────┐      ┌────────┐      ┌────────┐
           │Health OK│      │Health OK│      │ CIRCUIT │
           │SR: 82%  │      │SR: 91%  │      │ OPEN   │
           │Cost: 2.1%│     │Cost: 2.4%│     │(skip)  │
           │Lat: 200ms│     │Lat: 150ms│     └────────┘
           └────┬────┘      └────┬────┘
                │                │
           Score: 0.78      Score: 0.85
                │                │
                └───────┬────────┘
                        │
                        ▼
                ┌──────────────┐
                │ SELECT: B    │
                │ (score 0.85) │
                └──────────────┘
```

### Cold Start Problem

When a new acquirer is onboarded or a new card type/geography becomes available, there is no historical success rate data. Solutions:

1. **Exploration budget**: Route a small percentage (e.g., 5-10%) of traffic to new acquirers to gather data (similar to multi-armed bandit exploration/exploitation).
2. **Prior from similar segments**: Use success rates from similar segments (e.g., same acquirer + same card network but different country) as an initial estimate.
3. **Manual override**: Ops team can set an initial estimated success rate based on acquirer-provided benchmarks.

`[INFERRED — exploration/exploitation approach is commonly described in PSP marketing materials (e.g., Spreedly, Primer) but internal implementations are not publicly documented]`

---

## 5. Cascade Retries

When the primary acquirer **declines** a transaction, the PSP can automatically retry with a different acquirer. This is called **cascade routing** or **cascade retries**.

### Critical Distinction: Soft Decline vs Hard Decline

Not all declines are retryable. Retrying a hard decline is wasteful (and potentially a compliance violation).

| Decline Type | Meaning | Retry? | Examples (ISO 8583 Response Codes) |
|-------------|---------|--------|-------------------------------------|
| **Soft decline** | Temporary issue; transaction *might* succeed with a different acquirer or at a different time | YES — cascade to next acquirer | `05` (Do not honor — often a generic soft decline), `51` (Insufficient funds — sometimes retryable with a different acquirer due to routing differences `[UNVERIFIED]`), `91` (Issuer unavailable), `96` (System malfunction) |
| **Hard decline** | Permanent issue; retrying will not help and may be flagged as abuse | NO — do not retry | `14` (Invalid card number), `54` (Expired card), `41` (Lost card), `43` (Stolen card), `57` (Transaction not permitted to cardholder) |
| **Referral** | Issuer wants voice authorization or additional verification | MAYBE — depends on context | `01` (Refer to card issuer), `02` (Refer to card issuer — special condition) |

> **Important nuance**: Response code interpretation is not universal. Different acquirers may return different codes for the same underlying reason. The PSP must maintain a **response code mapping table** per acquirer that classifies each code as soft/hard/referral. This mapping is maintained by the payments operations team and updated based on experience.

> **"Insufficient funds" (code 51)**: This is often debated. Traditional view: hard decline (the cardholder simply does not have the money). Modern view: sometimes retryable because (a) a different acquirer might route to a different issuer authorization path that applies different hold calculations, or (b) real-time balance checks can differ between acquirer paths. In practice, most PSPs treat code 51 as a **hard decline** and do not cascade. `[UNVERIFIED — classification varies by PSP]`

### Cascade Retry Flow

```
                        Transaction Request
                              │
                              ▼
                    ┌──────────────────┐
                    │  Routing Engine   │
                    │  Select Primary:  │
                    │  Acquirer A       │
                    └────────┬─────────┘
                             │
                             ▼
                    ┌──────────────────┐
                    │  Acquirer A       │
                    │  (Primary)       │
                    └────────┬─────────┘
                             │
                    ┌────────┴────────┐
                    │                 │
                    ▼                 ▼
               APPROVED          DECLINED
                    │           (response code)
                    │                 │
                    ▼         ┌──────┴──────┐
              Return success  │             │
              to merchant     ▼             ▼
                         SOFT DECLINE   HARD DECLINE
                              │             │
                              ▼             ▼
                    ┌──────────────┐   Return decline
                    │ Cascade to   │   to merchant
                    │ Acquirer B   │   (do NOT retry)
                    │ (Secondary)  │
                    └──────┬──────┘
                           │
                  ┌────────┴────────┐
                  │                 │
                  ▼                 ▼
             APPROVED          DECLINED
                  │           (response code)
                  │                 │
                  ▼         ┌──────┴──────┐
            Return success  │             │
            to merchant     ▼             ▼
                       SOFT DECLINE   HARD / NO MORE
                            │         ACQUIRERS
                            ▼             │
                  ┌──────────────┐        ▼
                  │ Cascade to   │   Return decline
                  │ Acquirer C   │   to merchant
                  │ (Tertiary)   │
                  └──────┬──────┘
                         │
                         ▼
                    (same pattern)
```

### Cascade Retry Rules

1. **Maximum cascade depth**: Typically 2-3 retries maximum. Beyond that, latency becomes unacceptable (each acquirer call adds 1-3 seconds).
2. **Latency budget**: Total cascade must complete within the merchant's timeout expectation (typically 10-30 seconds for a payment call).
3. **No duplicate auth holds**: Each cascade attempt creates a new authorization request. If Acquirer A approved but the PSP decides to use Acquirer B instead (not a normal cascade scenario), the PSP must void the Acquirer A auth. In a normal cascade (A declined, try B), there is no auth hold to void from A.
4. **Idempotency across cascades**: The PSP's idempotency key is per-merchant-request, not per-acquirer-attempt. The merchant sees one payment attempt; the PSP may internally make 2-3 acquirer calls.
5. **Logging**: Every cascade attempt is logged with the acquirer, response code, response time, and reason for cascade. This data feeds back into the routing engine's success rate tracking.

### Cascade Impact on Success Rates

```
Example scenario (hypothetical numbers):

Primary acquirer success rate:           85%
Cascade to secondary (on soft decline):  +4% (secondary approves ~30% of primary's soft declines)
Cascade to tertiary (on soft decline):   +1% (tertiary approves ~20% of remaining soft declines)
                                         ─────
Effective success rate with cascade:     ~90%

[UNVERIFIED — these numbers are illustrative. Actual cascade lift varies significantly
by card type, geography, and acquirer mix. PSPs like Spreedly and Primer cite 2-8%
improvement from cascade routing in their marketing materials.]
```

---

## 6. Failover & Circuit Breakers

Cascade retries handle **business declines** (issuer says no). Failover handles **infrastructure failures** (acquirer is down, timing out, or returning errors).

### Acquirer Health Monitoring

The routing engine continuously monitors each acquirer's health:

```
Health Check System:

  ┌─────────────────────────────────────────────────────┐
  │                Health Monitor Service                │
  │                                                     │
  │  For each acquirer, track:                          │
  │    - Active health checks (ping every 10-30s)       │
  │    - Passive health (error rate from live traffic)   │
  │    - Latency percentiles (p50, p95, p99)            │
  │                                                     │
  │  Health status:                                     │
  │    HEALTHY  — error rate < 5%, p99 latency < 2s     │
  │    DEGRADED — error rate 5-20% or p99 latency 2-5s  │
  │    UNHEALTHY — error rate > 20% or p99 > 5s         │
  │    DOWN     — health check fails or 100% errors     │
  └─────────────────────────────────────────────────────┘
```

### Circuit Breaker Pattern

The circuit breaker prevents the routing engine from sending traffic to an acquirer that is failing, avoiding wasted latency and cascading failures.

```
Circuit Breaker State Machine:

         ┌──────────────────────────────────────┐
         │                                      │
         ▼                                      │
    ┌─────────┐    error_count > threshold  ┌───┴──────┐
    │ CLOSED  │ ─────────────────────────► │  OPEN    │
    │ (normal)│                             │ (reject  │
    │         │                             │  all)    │
    └─────────┘                             └───┬──────┘
         ▲                                      │
         │  probe succeeds                      │ after cooldown_period
         │                                      │
    ┌────┴──────┐                               │
    │HALF-OPEN  │ ◄─────────────────────────────┘
    │(allow 1   │
    │ probe txn)│ ── probe fails ──► back to OPEN
    └───────────┘

Parameters (per acquirer):
  - error_threshold: 10 errors in 60 seconds → OPEN
  - cooldown_period: 30 seconds before HALF-OPEN
  - probe_count: 1-3 test transactions in HALF-OPEN
  - recovery: if probe succeeds → CLOSED (resume normal traffic)
```

### Failover Flow

```
Transaction arrives → Routing engine selects Acquirer A
                              │
                              ▼
                     Acquirer A circuit breaker?
                      ┌───────┴───────┐
                      │               │
                   CLOSED          OPEN
                      │               │
                      ▼               ▼
              Send to Acquirer A   Skip A, select next
                      │           best acquirer (B)
                      │               │
               ┌──────┴──────┐        ▼
               │             │   Send to Acquirer B
            Success       Timeout/
               │          5xx Error
               ▼               │
          Return result     Increment A's
                           error counter
                               │
                        ┌──────┴──────┐
                        │ Threshold   │
                        │ exceeded?   │
                        └──────┬──────┘
                          YES  │  NO
                          │    │
                          ▼    ▼
                     Open A's  Cascade to
                     circuit   Acquirer B
                     breaker   (failover)
```

### Key Distinction: Failover vs Cascade

| | Cascade Retry | Failover |
|---|---|---|
| **Trigger** | Business decline (soft decline response code) | Infrastructure failure (timeout, 5xx, connection error) |
| **Acquirer responded?** | Yes — acquirer responded with a decline | No — acquirer did not respond or responded with a system error |
| **Acquirer health** | Acquirer is healthy; issuer declined | Acquirer may be unhealthy |
| **Circuit breaker involvement** | No | Yes — failure counts toward circuit breaker threshold |

---

## 7. Cost Optimization

Smart routing is not just about success rate. For high-volume merchants, even small cost differences compound into significant savings.

### Payment Processing Cost Breakdown

```
Total cost to merchant (MDR = Merchant Discount Rate):

  MDR = Interchange Fee + Network Fee + Acquirer Markup + PSP Markup

  Where:
  - Interchange Fee: Paid by acquirer to issuer (largest component)
    - US credit cards: ~1.5-2.5% [UNVERIFIED — varies by card type, MCC, and network]
    - US debit cards: ~0.05% + $0.21 (Durbin Amendment cap) [UNVERIFIED]
    - EU credit cards: ~0.3% (EU IFR cap) [UNVERIFIED]
    - India (RuPay/UPI): 0% (government mandate for certain categories) [UNVERIFIED]

  - Network Fee (Visa/MC assessment): ~0.13-0.15% [UNVERIFIED]

  - Acquirer Markup: ~0.1-0.5% (negotiated, varies by volume)

  - PSP Markup: ~0.2-0.5% on top (Stripe charges ~2.9% + $0.30 total
    for US cards, which includes all components) [UNVERIFIED — check stripe.com/pricing]
```

### How Routing Affects Cost

Different acquirers negotiate different interchange rates and charge different markups:

```
Example: US Visa Credit transaction, $100

  Acquirer A: Interchange 1.80% + Network 0.13% + Markup 0.25% = 2.18%  ($2.18)
  Acquirer B: Interchange 1.65% + Network 0.13% + Markup 0.40% = 2.18%  ($2.18)
  Acquirer C: Interchange 1.80% + Network 0.13% + Markup 0.15% = 2.08%  ($2.08)

  Routing to Acquirer C saves $0.10 per $100 transaction.
  At $100M/month volume: $100,000/month savings.
```

### The Success Rate vs Cost Trade-Off

This is the central tension in payment routing:

```
                     Success Rate
                          ▲
                    95% ──┤         ★ Acquirer B (high SR, high cost)
                          │
                    92% ──┤    ★ Acquirer A (balanced)
                          │
                    88% ──┤  ★ Acquirer C (low cost, lower SR)
                          │
                          └───┬────────┬────────┬──────► Cost
                            1.8%     2.1%     2.5%

  The optimal choice depends on the merchant's priorities:

  - Luxury e-commerce (high ticket, low volume): Maximize success rate.
    A declined $5,000 transaction costs far more than the extra 0.3% fee.

  - Utility payments (low ticket, high volume): Minimize cost.
    A declined $15 bill payment is annoying but the customer will retry.
    The 0.3% savings across millions of transactions is significant.

  - Default for most merchants: Maximize success rate with cost as tiebreaker.
    When two acquirers have similar success rates (within 1-2%), pick the cheaper one.
```

### Cost-Aware Routing Algorithm

```
FUNCTION select_acquirer_cost_aware(transaction, strategy):
    eligible = filter_acquirers_by_capability(transaction)

    FOR EACH acquirer IN eligible:
        sr = get_success_rate(acquirer, transaction.segment)
        cost = get_cost(acquirer, transaction)

        IF strategy == "maximize_success":
            // Only consider cost as tiebreaker
            score = sr * 1000 - cost  // success rate dominates

        ELIF strategy == "minimize_cost":
            // Only route to acquirers above minimum success rate threshold
            IF sr < MIN_ACCEPTABLE_SUCCESS_RATE:
                CONTINUE
            score = -cost * 1000 + sr  // cost dominates

        ELIF strategy == "balanced":
            score = (0.7 * normalize(sr)) - (0.3 * normalize(cost))

    RETURN acquirer with highest score
```

---

## 8. Local Acquiring

**Local acquiring** means processing a transaction through an acquirer that is in the **same country as the card issuer**. This is one of the most impactful routing optimizations.

### Why Local Acquiring Matters

```
Cross-border transaction (card issued in India, acquirer in US):

  Cardholder (India) ──► Merchant ──► PSP ──► US Acquirer ──► Visa Network ──► Indian Issuer
                                                                    │
                                                            Cross-border flag
                                                            on the transaction
                                                                    │
                                                            Higher interchange
                                                            FX conversion fee
                                                            Lower approval rate


Local transaction (card issued in India, acquirer in India):

  Cardholder (India) ──► Merchant ──► PSP ──► Indian Acquirer ──► Visa Network ──► Indian Issuer
                                                                        │
                                                                Domestic flag
                                                                on the transaction
                                                                        │
                                                                Lower interchange
                                                                No FX conversion
                                                                Higher approval rate
```

### Impact Comparison

| Dimension | Cross-Border | Local Acquiring | Delta |
|-----------|-------------|-----------------|-------|
| **Interchange rate** | 1.5-2.5% (cross-border surcharge applies) | 0.8-1.8% (domestic rate) | ~0.5-1.0% lower `[UNVERIFIED]` |
| **FX conversion fee** | 1-3% (network + acquirer FX markup) | 0% (same currency) | 1-3% savings `[UNVERIFIED]` |
| **Authorization success rate** | 70-85% (issuers are more cautious with cross-border) | 85-95% (issuers trust domestic acquirers more) | +5-15% higher `[UNVERIFIED — commonly cited range, actual delta varies by market]` |
| **Latency** | Higher (more network hops, potentially intercontinental) | Lower (domestic routing) | Variable |

### Why Issuers Decline Cross-Border More Often

1. **Fraud signal**: Cross-border transactions have statistically higher fraud rates. Issuers' risk models weight this heavily.
2. **Regulatory restrictions**: Some countries restrict or add friction to cross-border card transactions (e.g., India's RBI mandates additional authentication for international transactions).
3. **Routing path**: Cross-border transactions may go through more intermediary processors, each adding latency and potential points of failure.

### Building a Global Local Acquiring Network

For a PSP to offer local acquiring globally, it needs acquiring relationships in every major market:

```
PSP Global Acquiring Network (illustrative):

  ┌─────────────────────────────────────────────────────────────┐
  │                      PSP Routing Engine                      │
  │                                                              │
  │  Card issued in US?  ──────► US Acquirer (JPMorgan Chase,   │
  │                               Wells Fargo, etc.)             │
  │                                                              │
  │  Card issued in UK?  ──────► UK Acquirer (Barclays,         │
  │                               Worldpay, etc.)                │
  │                                                              │
  │  Card issued in India? ────► India Acquirer (HDFC Bank,     │
  │                               ICICI Bank, etc.)              │
  │                                                              │
  │  Card issued in Brazil? ───► Brazil Acquirer (Cielo,        │
  │                               Stone, etc.)                   │
  │                                                              │
  │  Card issued in Japan? ────► Japan Acquirer (JCB,           │
  │                               SMBC, etc.)                    │
  │                                                              │
  │  Card issued elsewhere? ───► Global Acquirer (fallback)     │
  └─────────────────────────────────────────────────────────────┘

  [INFERRED — specific acquirer names are illustrative. Stripe's actual
   acquiring partners are not fully public. Stripe has disclosed acquiring
   licenses in multiple markets and has publicly mentioned partners like
   BBVA (Mexico) and local banks in various markets.]
```

### Challenges of Local Acquiring

1. **Regulatory compliance per market**: Each country has different banking regulations, licensing requirements, and data residency rules. The PSP (or its local acquiring partner) must comply with local regulations.
2. **Settlement in local currency**: Local acquiring means settlement happens in local currency. The PSP must handle multi-currency treasury operations.
3. **Acquirer integration cost**: Each acquirer has a different API, different message formats (ISO 8583 variants), different error codes. Maintaining 20+ acquirer integrations is a significant engineering burden.
4. **BIN table accuracy**: To route locally, you need an accurate BIN table that maps card BIN ranges to issuing countries. BIN tables must be updated regularly as banks issue new BIN ranges. `[Visa and Mastercard publish official BIN tables to licensed participants]`

---

## 9. Routing Engine Architecture

### Component Diagram

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                         PAYMENT ROUTING ENGINE                               │
│                                                                              │
│  ┌───────────────┐    ┌────────────────────┐    ┌─────────────────────┐     │
│  │  BIN Lookup    │    │  Rule Engine        │    │  Scoring Engine     │     │
│  │  Service       │    │  (Static Rules)     │    │  (Dynamic Scoring)  │     │
│  │                │    │                     │    │                     │     │
│  │ Card BIN ──►   │    │ Config-driven rules │    │ Real-time success   │     │
│  │ - Issuing      │    │ stored in DB/Redis  │    │ rate calculation    │     │
│  │   country      │    │                     │    │ per segment         │     │
│  │ - Card network │    │ Evaluates:          │    │                     │     │
│  │ - Card type    │    │ - Capability filter │    │ Inputs:             │     │
│  │ - Issuing bank │    │ - Merchant overrides│    │ - Success rate      │     │
│  │                │    │ - Blocked routes    │    │   (sliding window)  │     │
│  └───────┬───────┘    └─────────┬──────────┘    │ - Cost data         │     │
│          │                      │                │ - Latency data      │     │
│          ▼                      ▼                │ - Circuit breaker   │     │
│  ┌──────────────────────────────────────────┐    │   state             │     │
│  │          Routing Orchestrator             │◄───┤                     │     │
│  │                                           │    └─────────────────────┘     │
│  │  1. Enrich transaction (BIN lookup)       │                               │
│  │  2. Apply static rules (filter eligible)  │    ┌─────────────────────┐     │
│  │  3. Score eligible acquirers              │    │  Health Monitor     │     │
│  │  4. Select best acquirer                  │    │                     │     │
│  │  5. Execute transaction                   │    │ - Active probes     │     │
│  │  6. Handle response (cascade if needed)   │    │   (every 10-30s)    │     │
│  │                                           │◄───┤ - Passive tracking  │     │
│  └──────────────┬────────────────────────────┘    │   (from live txns)  │     │
│                 │                                  │ - Circuit breaker   │     │
│                 │                                  │   state management  │     │
│                 ▼                                  └─────────────────────┘     │
│  ┌──────────────────────────────────────────┐                                │
│  │         Acquirer Adapter Layer            │    ┌─────────────────────┐     │
│  │                                           │    │  Analytics &        │     │
│  │  ┌──────────┐ ┌──────────┐ ┌──────────┐  │    │  Feedback Loop      │     │
│  │  │Adapter A │ │Adapter B │ │Adapter C │  │    │                     │     │
│  │  │(Acquirer │ │(Acquirer │ │(Acquirer │  │    │ - Log every routing │     │
│  │  │ A API)   │ │ B API)   │ │ C API)   │  │    │   decision          │     │
│  │  │          │ │          │ │          │  │    │ - Track outcome     │     │
│  │  │Translate │ │Translate │ │Translate │  │◄───┤ - Feed success/fail │     │
│  │  │to/from   │ │to/from   │ │to/from   │  │    │   back to scoring   │     │
│  │  │acquirer's│ │acquirer's│ │acquirer's│  │    │   engine            │     │
│  │  │format    │ │format    │ │format    │  │    │ - Generate reports  │     │
│  │  └──────────┘ └──────────┘ └──────────┘  │    └─────────────────────┘     │
│  └──────────────────────────────────────────┘                                │
│                                                                              │
└──────────────────────────────────────────────────────────────────────────────┘

External Dependencies:
  ┌──────────────┐  ┌──────────────┐  ┌──────────────┐
  │   Redis       │  │  PostgreSQL   │  │  Kafka        │
  │               │  │               │  │               │
  │ - Sliding     │  │ - Routing     │  │ - Transaction │
  │   window      │  │   rules       │  │   events      │
  │   counters    │  │ - Acquirer    │  │ - Routing     │
  │ - Circuit     │  │   configs     │  │   decisions   │
  │   breaker     │  │ - BIN tables  │  │   (for async  │
  │   state       │  │ - Cost data   │  │    analytics) │
  │ - Acquirer    │  │               │  │               │
  │   health      │  │               │  │               │
  └──────────────┘  └──────────────┘  └──────────────┘
```

### Component Responsibilities

#### BIN Lookup Service
- Maps card BIN (first 6-8 digits) to issuing country, card network, card type, and issuing bank
- Uses BIN tables from card networks (Visa/MC publish these to licensed participants)
- Cached in Redis (BIN tables change infrequently — updated weekly/monthly)
- Critical for determining domestic vs cross-border and selecting the right acquirer

#### Rule Engine (Static Rules)
- Config-driven rules stored in PostgreSQL, cached in Redis
- Updated by the payments operations team via an admin UI or API (`POST /config/routing-rules`)
- Handles:
  - **Capability filtering**: Which acquirers support this card network / currency / geography?
  - **Merchant overrides**: Merchant X wants all traffic routed to Acquirer A (contractual requirement)
  - **Blocklist rules**: Never route card type X to Acquirer Y (known low success rate)
  - **Regulatory rules**: EU-issued cards must be processed by an EU acquirer (data residency)

#### Scoring Engine (Dynamic Scoring)
- Calculates a real-time score for each eligible acquirer
- Inputs: sliding window success rate, cost, latency, circuit breaker state
- Weights are configurable per merchant or globally
- This is the "smart" part of smart routing

#### Health Monitor
- **Active probes**: Sends lightweight health check requests to each acquirer every 10-30 seconds
- **Passive monitoring**: Tracks error rates and latency from live transaction traffic
- **Circuit breaker management**: Opens/closes circuit breakers based on error thresholds
- Publishes health state changes to the routing orchestrator in real-time

#### Acquirer Adapter Layer
- Each acquirer has a different API (REST, SOAP, ISO 8583 over TCP)
- The adapter translates the PSP's internal transaction format to/from the acquirer's format
- Handles acquirer-specific quirks: different field names, different error codes, different timeout behaviors
- Maps acquirer response codes to the PSP's canonical response code taxonomy (soft decline, hard decline, error)

#### Analytics & Feedback Loop
- Every routing decision is logged: which acquirer was selected, why, what was the outcome
- Outcome data feeds back into the scoring engine (closing the feedback loop)
- Powers dashboards: per-acquirer success rate over time, routing distribution, cascade effectiveness
- Enables A/B testing of routing strategies

### Data Flow for a Single Transaction

```
1. Payment request arrives at Payment Service
       │
2. Payment Service calls Routing Engine with transaction details
       │
3. Routing Engine:
       │
       ├─ 3a. BIN Lookup: card BIN → issuing_country=IN, network=Visa, type=credit
       │
       ├─ 3b. Rule Engine: filter eligible acquirers
       │       → Acquirer_India (supports Visa/IN), Acquirer_Global (supports all)
       │
       ├─ 3c. Scoring Engine: score each eligible acquirer
       │       → Acquirer_India: SR=92%, cost=1.8%, latency_p99=180ms → score=0.91
       │       → Acquirer_Global: SR=78%, cost=2.4%, latency_p99=350ms → score=0.72
       │
       ├─ 3d. Health Monitor: check circuit breaker state
       │       → Both CLOSED (healthy)
       │
       └─ 3e. Select: Acquirer_India (score 0.91 > 0.72)
              Cascade order: [Acquirer_India, Acquirer_Global]
       │
4. Acquirer Adapter translates request to Acquirer_India's API format
       │
5. Send to Acquirer_India → Response: APPROVED (auth_code=A12345)
       │
6. Log routing decision + outcome → Kafka → Analytics
       │
7. Return success to Payment Service
```

---

## 10. Contrast: Single-Acquirer vs Multi-Acquirer PSP

Most small-to-medium merchants do not use a PSP with smart routing. They use a **single acquirer** — typically their bank or a bank's payment gateway.

### Single-Acquirer Setup

```
Small Merchant ──► Bank's Payment Gateway ──► Single Acquirer (the bank) ──► Card Network ──► Issuer

  - No routing decisions — there is only one acquirer
  - No failover — if the acquirer is down, payments stop
  - No cascade retries — if declined, that's the final answer
  - No local acquiring optimization — the bank acquires domestically only
  - No cost optimization — merchant accepts the bank's pricing
  - Simple integration — one API, one contract, one settlement
```

### Multi-Acquirer PSP Setup

```
Merchant ──► PSP (Stripe/Razorpay) ──► Routing Engine ──┬──► Acquirer A (US domestic)
                                                         ├──► Acquirer B (EU domestic)
                                                         ├──► Acquirer C (India domestic)
                                                         ├──► Acquirer D (LATAM)
                                                         └──► Acquirer E (Global fallback)

  - Smart routing selects optimal acquirer per transaction
  - Automatic failover if an acquirer goes down
  - Cascade retries on soft declines
  - Local acquiring in 40+ countries [UNVERIFIED — Stripe's actual count]
  - Cost optimization across acquirers
  - Complex integration — but abstracted by PSP's single API
```

### Comparison Table

| Aspect | Single Acquirer | Multi-Acquirer PSP |
|--------|----------------|-------------------|
| **Integration complexity** | Simple — one API, one contract | Complex internally, but PSP abstracts it. Merchant sees one API. |
| **Success rate** | Baseline — whatever the single acquirer achieves | +5-15% higher through smart routing + cascade `[UNVERIFIED]` |
| **Failover** | None — single point of failure | Automatic — route to healthy acquirer |
| **Cost** | Fixed pricing from one bank | Optimized — route to cheapest eligible acquirer |
| **Geographic coverage** | Limited to bank's acquiring markets | Global — local acquiring in many countries |
| **Maintenance burden** | Low — one acquirer relationship | High — but PSP bears this, not the merchant |
| **Suitable for** | Small merchants, single-market businesses | Any merchant processing internationally or at scale |

### The PSP's Value Proposition

The entire value of a PSP's routing layer can be summarized:

> **The merchant calls one API. The PSP handles the complexity of selecting the right acquirer, retrying on soft declines, failing over on acquirer outages, optimizing for cost, and processing locally in the card's country. The merchant does not need to know or care which acquirer processed their transaction.**

This abstraction is why merchants pay the PSP's markup on top of interchange + acquirer fees. For a merchant processing $10M/year across 30 countries, building and maintaining direct acquirer integrations in each market would cost far more in engineering time than the PSP's markup.

---

## Summary: Routing Engine Decision Framework

```
┌─────────────────────────────────────────────────────────────────┐
│                    ROUTING DECISION FRAMEWORK                    │
│                                                                  │
│  INPUT: Transaction (card BIN, amount, currency, MCC, merchant) │
│                                                                  │
│  STEP 1: ENRICH                                                  │
│    BIN lookup → issuing_country, card_network, card_type         │
│                                                                  │
│  STEP 2: FILTER (static rules)                                   │
│    Which acquirers CAN process this transaction?                 │
│    (network support, currency support, geo support, blocklists)  │
│                                                                  │
│  STEP 3: SCORE (dynamic)                                         │
│    For each eligible acquirer, calculate:                        │
│    score = W1*success_rate - W2*cost - W3*latency               │
│    Skip acquirers with open circuit breakers                     │
│                                                                  │
│  STEP 4: SELECT                                                  │
│    Primary = highest score                                       │
│    Cascade order = remaining acquirers sorted by score           │
│                                                                  │
│  STEP 5: EXECUTE                                                 │
│    Send to primary acquirer                                      │
│    On soft decline → cascade to next acquirer                    │
│    On hard decline → return decline to merchant                  │
│    On timeout/error → failover to next acquirer + update         │
│                        circuit breaker                           │
│                                                                  │
│  STEP 6: FEEDBACK                                                │
│    Log decision + outcome                                        │
│    Update sliding window success rate counters                   │
│    Feed into analytics for continuous optimization               │
│                                                                  │
│  OUTPUT: Authorization result (approved/declined + details)      │
└─────────────────────────────────────────────────────────────────┘
```

---

## Key Takeaways for Interview

1. **Routing is a core differentiator for PSPs** — it directly impacts success rate, cost, and reliability. This is one of the main reasons merchants use Stripe/Adyen/Razorpay instead of integrating with a single bank.

2. **Smart routing is a feedback loop** — track per-segment success rates, score acquirers in real-time, execute, observe outcome, update scores. The system continuously learns which acquirer works best for which transaction profile.

3. **Cascade retries require careful decline classification** — retrying a hard decline is wasteful and potentially a compliance risk. The PSP must maintain per-acquirer response code mappings.

4. **Local acquiring is the single biggest routing optimization** — processing domestically vs cross-border can improve success rates by 5-15% and reduce costs by 1-3%. Building a global acquiring network is expensive but provides a structural advantage.

5. **Circuit breakers prevent cascading failures** — if an acquirer is unhealthy, fail fast and route elsewhere. Do not wait for timeouts.

6. **The merchant sees none of this complexity** — the PSP abstracts the entire routing, cascade, failover, and local acquiring layer behind a single `POST /payments` API. This abstraction is the PSP's core value.

---

*Related docs:*
- `03-payment-flow-lifecycle.md` — End-to-end payment authorization, capture, and settlement flow
- `06-fraud-detection.md` — Fraud detection engine (runs before/alongside routing)
- `09-reliability-and-observability.md` — Circuit breakers, health monitoring, and SLAs in more detail
- `08-data-storage-and-infrastructure.md` — Redis, PostgreSQL, and Kafka infrastructure powering the routing engine
