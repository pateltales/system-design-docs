# Trip Lifecycle & Payments — The Backbone of Every Ride

> Every interaction between rider and driver maps to a state transition.
> Every state transition is an immutable event.
> Every event flows through the payment system.
> The trip state machine is the core domain model of Uber.

---

## Table of Contents

1. [Trip State Machine](#1-trip-state-machine)
2. [Event Sourcing](#2-event-sourcing)
3. [Payment Processing](#3-payment-processing)
4. [Ledger-Based Accounting](#4-ledger-based-accounting)
5. [Driver Payouts](#5-driver-payouts)
6. [Fraud Detection](#6-fraud-detection)
7. [Fare Disputes](#7-fare-disputes)
8. [Contrasts](#8-contrasts)

---

## 1. Trip State Machine

```
                              ┌─────────────┐
                              │    IDLE      │
                              │ (rider opens │
                              │  the app)    │
                              └──────┬───────┘
                                     │ rider requests ride
                                     ▼
                              ┌─────────────┐
                         ┌────│  MATCHING    │────┐
                         │    │ (finding a   │    │
                         │    │  driver)     │    │
                         │    └──────┬───────┘    │
                         │           │            │
                    no drivers/      │ driver     rider cancels
                    timeout          │ assigned
                         │           ▼            │
                         │    ┌─────────────┐     │
                         │    │ DRIVER_EN_  │     │
                         │    │ ROUTE       │─────┤
                         │    │ (driving to │     │ rider cancels
                         │    │  pickup)    │     │ (may incur fee)
                         │    └──────┬───────┘    │
                         │           │            │
                         │      driver cancels ───┤──→ MATCHING
                         │           │                 (re-dispatch)
                         │      driver arrives
                         │           │
                         │           ▼
                         │    ┌─────────────┐
                         │    │  ARRIVED    │
                         │    │ (waiting    │
                         │    │  for rider) │─────┐
                         │    └──────┬───────┘    │
                         │           │            │
                         │      rider boards,     rider no-show
                         │      driver starts     (5 min timeout)
                         │      trip              │
                         │           │            │
                         │           ▼            ▼
                         │    ┌─────────────┐  CANCELLED
                         │    │ IN_PROGRESS │  (no-show fee
                         │    │ (trip       │   charged)
                         │    │  underway)  │
                         │    └──────┬───────┘
                         │           │
                         │      driver ends trip ─────┐
                         │      (at dropoff)          │
                         │           │                 │
                         │      safety ────→ EMERGENCY │
                         │      incident              │
                         │           │                 │
                         ▼           ▼                 │
                    CANCELLED  ┌─────────────┐         │
                               │ COMPLETED   │◄────────┘
                               │ (trip done, │
                               │  fare calc) │
                               └──────┬───────┘
                                      │
                          ┌───────────┼───────────┐
                          │           │           │
                     payment OK   dispute     adjustment
                          │        filed      needed
                          ▼           ▼           │
                    ┌───────────┐ ┌──────────┐    │
                    │ FINALIZED │ │ DISPUTED │────┘
                    │           │ │          │
                    └───────────┘ └──────────┘
```

### State Transition Rules

```
Critical invariants:

1. A trip can only move FORWARD through the state machine
   (no going back from IN_PROGRESS to MATCHING)

2. Every transition requires specific conditions:
   MATCHING → DRIVER_EN_ROUTE: driver must accept the offer
   DRIVER_EN_ROUTE → ARRIVED: driver GPS must be within
     ~100m of the pickup location
   ARRIVED → IN_PROGRESS: explicit "start trip" action by driver
   IN_PROGRESS → COMPLETED: explicit "end trip" action by driver
     OR GPS proximity to dropoff + confirmation

3. Cancellation has different rules by state:
   MATCHING: free cancel (no driver assigned yet)
   DRIVER_EN_ROUTE: cancel fee if driver already traveled
     significantly (>2 min or >0.5 miles)
   ARRIVED: cancel fee after no-show timer (5 min)
   IN_PROGRESS: cannot cancel — must complete or handle
     as safety incident

4. Each state has a TIMEOUT:
   MATCHING: 60 seconds → auto-cancel (no driver found)
   DRIVER_EN_ROUTE: based on ETA + buffer → alert if driver
     hasn't arrived (may indicate driver is lost or stuck)
   ARRIVED: 5 minutes → rider no-show → auto-cancel with fee
   IN_PROGRESS: no hard timeout, but extremely long trips
     (>4 hours) trigger a safety check
```

---

## 2. Event Sourcing

Every state transition is recorded as an immutable event.

```
Event schema:
{
  "eventId": "evt_abc123",
  "tripId": "trip_xyz789",
  "eventType": "DRIVER_ASSIGNED",
  "timestamp": "2024-03-15T14:30:45.123Z",
  "gpsLocation": { "lat": 40.7484, "lng": -73.9856 },
  "metadata": {
    "driverId": "drv_456",
    "vehicleId": "veh_789",
    "etaToPickup": 180,
    "matchScore": 0.92
  },
  "previousState": "MATCHING",
  "newState": "DRIVER_EN_ROUTE"
}

Events for a complete trip:
  1. RIDE_REQUESTED       { riderId, pickup, dropoff, rideType, surgeMultiplier }
  2. DRIVER_ASSIGNED      { driverId, vehicleId, etaToPickup }
  3. DRIVER_ARRIVED       { gpsLocation, actualPickupTime }
  4. TRIP_STARTED         { gpsLocation, odometerStart }
  5. GPS_UPDATE           { gpsLocation } (every 3-4 seconds, ~300-500 per trip)
  6. TRIP_COMPLETED       { gpsLocation, odometerEnd, actualDistance, actualDuration }
  7. FARE_CALCULATED      { upfrontPrice, actualFare, driverEarnings, platformFee }
  8. PAYMENT_CHARGED      { paymentMethodId, amount, transactionId }
  9. RATING_SUBMITTED     { riderRating, driverRating }
```

### Why Event Sourcing?

```
Benefits of event sourcing for trips:

1. AUDIT TRAIL
   Every action is permanently recorded with timestamp and GPS.
   Critical for:
     • Rider disputes ("the driver took a longer route")
     • Safety investigations ("where was the driver at 3:47 PM?")
     • Insurance claims (reconstruct exact trip path)
     • Regulatory compliance (cities require trip data)

2. REPLAY CAPABILITY
   Found a bug in fare calculation? Replay events to recompute fares.
   Changed the fare formula? Recompute all affected trips.
   This is impossible with mutable state (CRUD) — you've overwritten
   the intermediate states.

3. TEMPORAL QUERIES
   "What was the driver's location at any point during the trip?"
   → Replay events up to that timestamp.

   "How long did the rider wait after the driver arrived?"
   → DRIVER_ARRIVED.timestamp - TRIP_STARTED.timestamp

4. DECOUPLED CONSUMERS
   Events flow through Kafka. Multiple consumers process them independently:
     • Trip Service: manages state machine
     • Payment Service: charges rider, credits driver
     • Analytics Service: aggregates trip statistics
     • Safety Service: monitors for anomalies
     • ETA Service: uses completed trips as training data

5. IDEMPOTENCY
   Events are immutable and have unique IDs.
   If a consumer crashes and restarts, it can replay from the last
   processed event without duplicating side effects.
```

### Current State from Events

```
The trip's current state is derived by replaying its events:

function getCurrentState(tripId):
    events = eventStore.getEvents(tripId)  // ordered by timestamp
    state = IDLE
    for event in events:
        state = applyTransition(state, event)
    return state

In practice, Uber materializes the current state into a read-optimized
projection (trip table in MySQL/Schemaless) and updates it on each event.
The event log is the source of truth; the projection is a cache.

  Event Log (Kafka / Cassandra)     →    Projection (MySQL)
  [immutable, append-only]               [mutable, current state]
  Source of truth                         Read-optimized view
  Used for replay, audit, analytics      Used for API queries
```

---

## 3. Payment Processing

### Payment Flow

```
Trip completes → payment is processed ASYNCHRONOUSLY.
The rider exits the car immediately. Payment happens in the background.

Timeline:
  T+0s:   Driver taps "End Trip"
  T+0s:   TRIP_COMPLETED event published
  T+1s:   Fare calculated (upfront price vs actual)
  T+2s:   FARE_CALCULATED event published
  T+3s:   Payment Service picks up the event
  T+5s:   Charge rider's payment method (Stripe/Braintree API call)
  T+10s:  PAYMENT_CHARGED event published
  T+10s:  Push notification to rider: "Your trip cost $18.50"
  T+10s:  Receipt generated and emailed

Why asynchronous?
  1. Rider shouldn't wait for payment to exit the car
  2. Payment gateway (Stripe) may have latency (1-5 seconds)
  3. If payment fails, we can retry without blocking the user
  4. Decouples trip experience from payment processing

Payment failure handling:
  If charge fails (card declined):
    → Retry with same card (transient error)
    → Try backup payment method (if rider has one)
    → Send notification: "Update your payment method"
    → Trip is still COMPLETED (driver gets paid regardless)
    → Outstanding balance added to rider's account
    → Rider can't request new rides until balance is cleared
```

### Payment Method Hierarchy

```
Rider's payment methods (ordered by priority):
  1. Primary card (default)
  2. Backup card
  3. Digital wallet (Apple Pay, Google Pay)
  4. Uber Cash (prepaid balance)
  5. Split fare (shared with co-riders)
  6. Business profile (charged to employer)

Selection logic:
  Use primary card by default.
  If rider selected a specific method for this ride → use that.
  If primary card fails → fall back to backup card.
  If all cards fail → deduct from Uber Cash balance.
  If no funds available → create outstanding balance.
```

---

## 4. Ledger-Based Accounting

Every financial transaction is recorded as a double-entry in a ledger.

```
Double-entry principle:
  For every debit, there is an equal credit.
  The books always balance.

Trip fare: $18.50
  Platform commission: 25% = $4.63
  Driver earnings: $13.87
  Tip: $3.00

Ledger entries:
  ┌────────────────────────────────────────────────────────┐
  │ Entry 1: Rider charged                                 │
  │   Debit:  Rider Account          $18.50                │
  │   Credit: Trip Revenue Account   $18.50                │
  │                                                        │
  │ Entry 2: Driver credited                               │
  │   Debit:  Trip Revenue Account   $13.87                │
  │   Credit: Driver Account         $13.87                │
  │                                                        │
  │ Entry 3: Platform commission                           │
  │   Debit:  Trip Revenue Account   $4.63                 │
  │   Credit: Platform Revenue       $4.63                 │
  │                                                        │
  │ Entry 4: Tip                                           │
  │   Debit:  Rider Account          $3.00                 │
  │   Credit: Driver Account         $3.00                 │
  │   (Tips pass through 100% to driver — not commissionable) │
  │                                                        │
  │ Verification:                                          │
  │   Total debits  = $18.50 + $3.00 = $21.50             │
  │   Total credits = $13.87 + $4.63 + $3.00 = $21.50    │
  │   ✓ Balanced                                          │
  └────────────────────────────────────────────────────────┘

Why ledger-based?
  1. Auditable: every dollar movement is traced
  2. Regulatory compliance: financial records are immutable
  3. Reconciliation: can verify platform books balance at any time
  4. Dispute resolution: trace exactly where money went
  5. Tax reporting: driver earnings, platform revenue, sales tax
```

### Toll and Fee Handling

```
Tolls:
  Detected from the trip route (map data includes toll roads/bridges).
  Charged to rider at cost (platform does not mark up tolls).
  Not included in driver earnings or commission calculation.

  Ledger:
    Debit:  Rider Account       $6.12 (toll amount)
    Credit: Toll Passthrough    $6.12
    (later settled with toll authority)

Booking fee:
  Flat fee per ride (e.g., $2.75).
  Goes 100% to the platform.
  Covers: insurance, safety features, regulatory costs.

  Ledger:
    Debit:  Rider Account       $2.75
    Credit: Platform Revenue    $2.75

Cancellation fee:
  Charged to rider when they cancel after driver is assigned
  and has traveled toward the pickup.
  Typically $5-10. Partially passed to driver as compensation.

  Ledger:
    Debit:  Rider Account       $5.00
    Credit: Driver Account      $3.75
    Credit: Platform Revenue    $1.25
```

---

## 5. Driver Payouts

```
Drivers accumulate earnings throughout the week.

Earning sources:
  • Trip fares (minus platform commission)
  • Tips (100% to driver)
  • Surge bonuses
  • Quest bonuses ("complete 50 trips this week for $100 extra")
  • Consecutive trip bonuses
  • Referral bonuses (refer a new driver)

Payout options:

  1. Weekly payout (default):
     Every Monday, accumulated earnings are transferred to
     the driver's linked bank account via ACH.
     ACH: 1-3 business days, free.

  2. Instant Pay (on-demand):
     Driver requests immediate transfer to their debit card.
     Processed in minutes via debit push (Visa Direct / Mastercard Send).
     Fee: $0.50 per transfer.
     Available up to 5 times per day.

  3. Uber Pro Card (driver debit card):
     Uber issues a debit card linked to the driver's earnings balance.
     Earnings available immediately after each trip.
     No transfer fee.

Payout ledger:
  When driver is paid out:
    Debit:  Driver Account (platform)    $450.00
    Credit: Driver Bank Account          $450.00

  After payout, driver's platform balance resets to $0.
```

---

## 6. Fraud Detection

```
Payment fraud is a significant cost center for ride-sharing platforms.

Types of fraud:

  1. Stolen credit cards
     Fraudster uses stolen card → requests rides → card owner disputes → chargeback.
     Detection: new account, high-value trips, unusual patterns.

  2. Promo abuse
     Create multiple fake accounts to use first-ride promos repeatedly.
     Detection: same device ID, same IP, similar GPS patterns.

  3. Driver fraud (ghost rides)
     Driver and a confederate rider create fake trips to earn surge bonuses.
     Detection: trips with no actual movement (GPS stays in same location),
     same rider-driver pair repeatedly, trips at unusual hours.

  4. GPS spoofing
     Driver fakes GPS location to appear in a surge zone or inflate trip distance.
     Detection: compare reported GPS with cell tower triangulation,
     look for impossible speed/teleportation events.

  5. Collusion
     Driver-rider pair split the fare refund from fraudulent disputes.
     Detection: same rider always disputes trips with same driver,
     disputes from riders with unusually high dispute rates.

ML fraud detection:
  Real-time scoring: every trip gets a fraud score at request time.
  Features: account age, payment method age, device fingerprint,
            location history, trip patterns, social graph analysis.

  High-risk signals:
    • New account (<24 hours) requesting high-value trip
    • Payment method added <1 hour ago
    • Device previously associated with a banned account
    • Trip pattern matches known fraud patterns

  Response:
    Low risk: process normally
    Medium risk: require additional verification (SMS code, selfie)
    High risk: block the transaction, flag for manual review
```

---

## 7. Fare Disputes

```
Riders can dispute fares for several reasons:

Common disputes:
  1. "Driver took a longer route" (most common)
  2. "I was charged for a ride I didn't take"
  3. "The fare is much higher than the estimate"
  4. "I was charged a cancellation fee but it wasn't my fault"
  5. "Wrong toll charges"

Automated dispute resolution:

  Step 1: Compare actual route to optimal route
    actual_route = map-matched GPS trace of the trip
    optimal_route = routing engine's best route at trip start time
    deviation = (actual_distance - optimal_distance) / optimal_distance

    If deviation > 25%:
      → Likely driver took a longer route
      → Adjust fare to what it would have been on the optimal route

  Step 2: Check for GPS anomalies
    If GPS trace shows impossible speeds (>200 km/h) or teleportation:
      → GPS error → recalculate distance from map-matched route

  Step 3: Check for driver-side detour reasons
    Did the routing engine reroute the driver mid-trip (traffic, road closure)?
    If yes: the detour was justified → no fare adjustment.
    Did the rider request a stop or route change?
    If yes: adjustment based on the agreed-upon route.

  Step 4: Automatic resolution or escalation
    If automated logic can determine fault:
      → Apply credit automatically (usually within minutes)
    If ambiguous:
      → Escalate to human support agent
      → Agent reviews GPS trace, event log, rider/driver messages
```

---

## 8. Contrasts

### Uber vs Traditional Taxi Meter

| Dimension | Uber (GPS-Based) | Traditional Taxi (Meter) |
|---|---|---|
| **Distance measurement** | GPS traces + map matching | Wheel rotation sensor (mechanical) |
| **Accuracy** | ±5-10m (GPS), corrected by map matching | ±1% (mechanical, very accurate) |
| **Manipulation risk** | GPS spoofing (software attack) | Meter tampering (hardware attack — heavily regulated) |
| **Fare calculation** | Server-side (driver can't manipulate) | Meter in the cab (driver controls) |
| **Price model** | Upfront (fixed before trip) | Metered (accumulates during trip) |
| **Route transparency** | Full GPS trace recorded, rider can review | No route recording (rider can't verify) |
| **Dispute resolution** | Automated (GPS comparison to optimal route) | Manual (he said / she said) |

### Uber vs Food Delivery Payments

| Dimension | Uber Rides | DoorDash / Uber Eats |
|---|---|---|
| **Parties to pay** | 2: Driver + Platform | 3: Restaurant + Courier + Platform |
| **Price components** | Fare + surge + booking fee + tolls + tip | Food price + delivery fee + service fee + tax + tip |
| **Payment timing** | Asynchronous after trip ends | At order time (charged upfront before food is even prepared) |
| **Tipping** | Post-trip (rider tips after seeing the service quality) | Pre-delivery (tip set before courier picks up — controversial) |
| **Refund triggers** | Route deviation, cancellation | Missing items, wrong order, cold food, late delivery |
| **Revenue split** | Platform: ~25% commission | Restaurant: 15-30% commission to platform, Courier: delivery fee + tip |
| **Fraud surface** | GPS spoofing, fake rides | Order never delivered (marked delivered), missing items fraud |

### Event Sourcing vs CRUD for Trips

| Dimension | Event Sourcing (Uber's choice) | CRUD (simpler alternative) |
|---|---|---|
| **State model** | Sequence of immutable events | Current state in a row (mutable) |
| **Audit trail** | Built-in (events are the history) | Requires separate audit log table |
| **Replay** | Replay events to recompute anything | Impossible — intermediate states lost |
| **Storage** | Higher (all events stored forever) | Lower (only current state) |
| **Complexity** | Higher (event store + projections) | Lower (simple CRUD operations) |
| **Query simplicity** | Need projections for current state | Direct query on the current state |
| **When to use** | Financial transactions, audit-critical flows, multi-consumer event-driven systems | Simple domains where history doesn't matter |
| **Why Uber chose event sourcing** | Trips involve money, safety, legal liability. Full history is non-negotiable for disputes, insurance, and regulatory compliance. Multiple services consume trip events independently. | — |
