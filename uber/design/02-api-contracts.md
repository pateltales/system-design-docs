# Uber Platform APIs — Comprehensive Reference

> Every rider tap, driver GPS ping, and surge recalculation maps to an API call.
> This doc covers the full API surface. The interview simulation (Phase 3) covers the critical subset.

---

## Table of Contents

1. [Ride Request APIs](#1-ride-request-apis)
2. [Driver Location APIs](#2-driver-location-apis)
3. [Matching / Dispatch APIs](#3-matching--dispatch-apis)
4. [Pricing / Fare APIs](#4-pricing--fare-apis)
5. [ETA / Routing APIs](#5-eta--routing-apis)
6. [Trip Lifecycle APIs](#6-trip-lifecycle-apis)
7. [Payment APIs](#7-payment-apis)
8. [Rating & Feedback APIs](#8-rating--feedback-apis)
9. [Driver Management APIs](#9-driver-management-apis)
10. [Safety APIs](#10-safety-apis)
11. [Maps & Geocoding APIs](#11-maps--geocoding-apis)
12. [API Model Contrasts](#12-api-model-contrasts)

---

## 1. Ride Request APIs

The core rider flow — requesting, tracking, and completing a ride.

### `POST /rides/request` — Request a Ride ★

```json
Request:
{
  "pickupLocation": { "lat": 40.7484, "lng": -73.9857 },
  "dropoffLocation": { "lat": 40.7614, "lng": -73.9776 },
  "rideType": "UberX",          // UberX, XL, Black, Pool, Comfort
  "paymentMethodId": "pm-uuid-123",
  "scheduledAt": null,           // null = now, ISO timestamp = scheduled
  "passengerCount": 1,
  "promoCode": "SAVE20"
}

Response:
{
  "rideId": "ride-uuid-abc",
  "status": "MATCHING",
  "estimatedFare": {
    "min": 1200,                 // cents ($12.00)
    "max": 1600,                 // cents ($16.00)
    "currency": "USD",
    "surgeMultiplier": 1.3,
    "breakdown": {
      "baseFare": 250,
      "distanceFare": 680,
      "timeFare": 320,
      "surgePremium": 250,
      "bookingFee": 200,
      "promoDiscount": -300
    }
  },
  "estimatedPickupEta": 180,    // seconds (3 minutes)
  "estimatedTripDuration": 720, // seconds (12 minutes)
  "estimatedDistance": 3200,    // meters
  "createdAt": "2026-02-20T14:30:00Z"
}
```

**Why this matters architecturally:** This single API call triggers the most complex flow in the system:
1. Geocode/validate pickup and dropoff locations
2. Query surge pricing for the pickup cell → determine multiplier
3. Query routing service for ETA and distance estimate
4. Calculate upfront fare from route + surge + promotions
5. Create ride record (status: MATCHING)
6. Trigger the dispatch system → find and assign a driver

### `GET /rides/{rideId}` — Ride Status

```json
Response:
{
  "rideId": "ride-uuid-abc",
  "status": "DRIVER_EN_ROUTE",  // MATCHING | DRIVER_EN_ROUTE | ARRIVED |
                                 // IN_PROGRESS | COMPLETED | CANCELLED
  "driver": {
    "driverId": "drv-uuid-456",
    "name": "Maria S.",
    "rating": 4.92,
    "photoUrl": "https://...",
    "vehicleDescription": "White Toyota Camry",
    "licensePlate": "ABC 1234"
  },
  "pickupEta": 120,             // seconds until driver arrives
  "driverLocation": { "lat": 40.7490, "lng": -73.9860 },
  "route": {                    // polyline of driver's route to pickup
    "encodedPolyline": "a~l~Fjk~uOwHJy@..."
  },
  "fare": { ... },
  "createdAt": "2026-02-20T14:30:00Z",
  "updatedAt": "2026-02-20T14:30:45Z"
}
```

### `PUT /rides/{rideId}/cancel` — Cancel a Ride

```json
Request:
{
  "reason": "CHANGED_PLANS"     // CHANGED_PLANS | DRIVER_TOO_FAR | OTHER
}

Response:
{
  "status": "CANCELLED",
  "cancellationFee": 500,       // cents — charged if driver was already en route
  "waived": false
}
```

### `GET /rides/{rideId}/receipt` — Trip Receipt

```json
Response:
{
  "rideId": "ride-uuid-abc",
  "fare": {
    "total": 1450,
    "breakdown": {
      "baseFare": 250,
      "distanceFare": 700,       // actual distance
      "timeFare": 350,           // actual duration
      "surgePremium": 250,
      "bookingFee": 200,
      "tip": 300,
      "tolls": 0,
      "promoDiscount": -300
    },
    "upfrontPrice": 1400,        // what rider was quoted
    "actualCost": 1500,          // what the trip actually cost Uber
    "riderCharged": 1400         // rider pays upfront price (Uber absorbs diff)
  },
  "distance": 3450,             // meters (actual)
  "duration": 840,              // seconds (actual)
  "route": { "encodedPolyline": "..." },
  "driver": { ... },
  "paymentMethod": "Visa •••• 4242",
  "ratedDriver": true,
  "tripStartedAt": "2026-02-20T14:35:00Z",
  "tripEndedAt": "2026-02-20T14:49:00Z"
}
```

---

## 2. Driver Location APIs

The real-time heartbeat — drivers send GPS every 3-4 seconds.

### `PUT /drivers/location` — Update Driver Location ★

```json
Request:
{
  "lat": 40.7489,
  "lng": -73.9856,
  "heading": 270,              // degrees (0=north, 90=east)
  "speed": 12.5,               // m/s
  "accuracy": 8.0,             // GPS accuracy in meters
  "timestamp": "2026-02-20T14:30:02.345Z"
}

Response:
{
  "ack": true
}
```

**Scale:** ~5 million active drivers × 1 update per 3-4 seconds = **~1.5 million updates/second** at peak. Each update is ~100 bytes. This is ~150 MB/sec of raw GPS data, or **~13 TB/day**.

**Architecture:** These updates flow through a high-throughput ingestion pipeline:
```
Driver app → Load Balancer → Location Ingestion Service → Kafka
                                     │
                                     ├── Update in-memory Geospatial Index
                                     ├── Write to Cassandra (location history)
                                     └── Feed to Surge Pricing Pipeline
```

### `GET /drivers/nearby` — Find Nearby Drivers (Internal) ★

```json
Request (query params):
  lat=40.7484&lng=-73.9857&radius=5000&type=UberX&status=AVAILABLE&limit=20

Response:
{
  "drivers": [
    {
      "driverId": "drv-uuid-456",
      "location": { "lat": 40.7490, "lng": -73.9860 },
      "heading": 270,
      "speed": 12.5,
      "distanceMeters": 85,
      "vehicleType": "UberX",
      "rating": 4.92,
      "lastUpdated": "2026-02-20T14:30:02Z"
    },
    ...
  ],
  "totalAvailable": 47
}
```

**Why radius, not count?** We query by radius because drivers 10km away are useless even if they're available. The dispatch system needs drivers within a reasonable pickup ETA (~5-8 minutes), which translates to a radius based on city density and traffic.

### `PUT /drivers/status` — Toggle Driver Status

```json
Request:
{
  "status": "AVAILABLE"         // OFFLINE | AVAILABLE | ON_TRIP | BUSY
}

Response:
{
  "status": "AVAILABLE",
  "region": "new-york-manhattan"
}
```

---

## 3. Matching / Dispatch APIs

Internal APIs that power the brain of Uber — matching riders with drivers.

### `POST /dispatch/match` — Match a Ride ★ (Internal)

```json
Request:
{
  "rideId": "ride-uuid-abc",
  "pickupLocation": { "lat": 40.7484, "lng": -73.9857 },
  "dropoffLocation": { "lat": 40.7614, "lng": -73.9776 },
  "rideType": "UberX",
  "passengerCount": 1,
  "surgeMultiplier": 1.3
}

Response:
{
  "matchId": "match-uuid-def",
  "candidateDrivers": [
    {
      "driverId": "drv-uuid-456",
      "etaToPickup": 180,       // seconds (route-based, not straight-line)
      "distanceToPickup": 1200, // meters
      "heading": 270,
      "headingBonus": 0.9,      // driver is heading toward pickup
      "rating": 4.92,
      "score": 0.87             // composite match score
    },
    ...
  ],
  "selectedDriver": "drv-uuid-456",
  "offerExpiresAt": "2026-02-20T14:30:15Z"  // 15-second acceptance window
}
```

### `POST /dispatch/offer-response` — Driver Accepts/Declines

```json
Request:
{
  "matchId": "match-uuid-def",
  "driverId": "drv-uuid-456",
  "response": "ACCEPT"          // ACCEPT | DECLINE | TIMEOUT
}

Response:
{
  "rideId": "ride-uuid-abc",
  "status": "DRIVER_EN_ROUTE",
  "rider": {
    "name": "Alex K.",
    "rating": 4.85,
    "pickupLocation": { "lat": 40.7484, "lng": -73.9857 },
    "pickupAddress": "350 5th Ave, New York, NY",
    "dropoffAddress": "1000 5th Ave, New York, NY"
  },
  "navigationRoute": { "encodedPolyline": "..." }
}
```

**Dispatch flow on decline/timeout:**
```
Driver 1 declines (or 15s timeout)
    → Dispatch cascades to Driver 2 (next best match)
    → Driver 2 declines
    → Dispatch cascades to Driver 3
    → Driver 3 accepts → ride status: DRIVER_EN_ROUTE

If all candidates exhausted:
    → Expand search radius (5km → 8km → 12km)
    → Re-query geospatial index for more candidates
    → If still no match → notify rider "No drivers available"
```

---

## 4. Pricing / Fare APIs

### `GET /rides/estimate` — Fare Estimate ★

```json
Request (query params):
  pickupLat=40.7484&pickupLng=-73.9857
  &dropoffLat=40.7614&dropoffLng=-73.9776
  &rideType=UberX

Response:
{
  "estimates": [
    {
      "rideType": "UberX",
      "displayName": "UberX",
      "estimatedFare": { "min": 1200, "max": 1600, "currency": "USD" },
      "surgeMultiplier": 1.3,
      "estimatedDuration": 720,
      "estimatedDistance": 3200,
      "pickupEta": 180,
      "capacity": 4
    },
    {
      "rideType": "UberXL",
      "displayName": "UberXL",
      "estimatedFare": { "min": 1800, "max": 2400, "currency": "USD" },
      "surgeMultiplier": 1.0,
      "pickupEta": 300,
      "capacity": 6
    },
    ...
  ]
}
```

**Upfront pricing model:**
```
upfrontPrice = baseFare
             + (perMile × estimatedMiles)
             + (perMinute × estimatedMinutes)
             + (surgeMultiplier - 1.0) × basePortion
             + bookingFee
             + estimatedTolls
             - promotions

If actual trip cost > upfrontPrice: Uber absorbs the difference
If actual trip cost < upfrontPrice: Uber keeps the margin
```

### `GET /pricing/surge` — Current Surge (Internal)

```json
Request (query params):
  lat=40.7484&lng=-73.9857

Response:
{
  "surgeMultiplier": 1.3,
  "h3Cell": "892a100d2c3ffff",  // H3 cell ID at resolution 9
  "supply": 47,                  // available drivers in this cell
  "demand": 62,                  // ride requests in last 5 min
  "updatedAt": "2026-02-20T14:29:00Z",
  "expiresAt": "2026-02-20T14:31:00Z"  // surge recomputed every ~2 min
}
```

---

## 5. ETA / Routing APIs

### `GET /eta` — Estimated Time of Arrival ★

```json
Request (query params):
  originLat=40.7490&originLng=-73.9860
  &destLat=40.7484&destLng=-73.9857

Response:
{
  "eta": 180,                   // seconds
  "distance": 1200,             // meters
  "trafficLevel": "MODERATE",   // LOW | MODERATE | HEAVY | SEVERE
  "confidence": 0.85,           // model confidence in the prediction
  "computedAt": "2026-02-20T14:30:00Z"
}
```

**Performance:** This is the most frequently called API — used by dispatch (ETA for each candidate driver), fare estimation, rider-facing ETA display. Target latency: **<100ms** at millions of QPS. Achieved via Contraction Hierarchies preprocessing on the road graph.

### `GET /route` — Navigation Route

```json
Request (query params):
  originLat=40.7490&originLng=-73.9860
  &destLat=40.7614&destLng=-73.9776

Response:
{
  "duration": 720,              // seconds
  "distance": 3200,             // meters
  "route": {
    "encodedPolyline": "a~l~Fjk~uOwHJy@...",
    "legs": [
      {
        "instruction": "Head west on 34th St",
        "distance": 400,
        "duration": 60,
        "maneuver": "STRAIGHT"
      },
      {
        "instruction": "Turn right onto 5th Ave",
        "distance": 2800,
        "duration": 660,
        "maneuver": "TURN_RIGHT"
      }
    ]
  },
  "trafficSegments": [
    { "startIndex": 0, "endIndex": 5, "congestion": "moderate" },
    { "startIndex": 5, "endIndex": 12, "congestion": "heavy" }
  ]
}
```

---

## 6. Trip Lifecycle APIs

### `POST /trips/start` — Start Trip (Driver confirms pickup)

```json
Request:
{
  "rideId": "ride-uuid-abc",
  "driverId": "drv-uuid-456",
  "location": { "lat": 40.7484, "lng": -73.9857 },
  "odometerReading": null       // optional, for metered markets
}

Response:
{
  "tripId": "trip-uuid-ghi",
  "status": "IN_PROGRESS",
  "startedAt": "2026-02-20T14:35:00Z",
  "navigationRoute": { "encodedPolyline": "..." }
}
```

### `POST /trips/end` — End Trip (Driver confirms dropoff)

```json
Request:
{
  "tripId": "trip-uuid-ghi",
  "location": { "lat": 40.7614, "lng": -73.9776 },
  "odometerReading": null
}

Response:
{
  "status": "COMPLETED",
  "fare": {
    "total": 1400,
    "currency": "USD",
    "riderCharged": 1400,
    "driverEarnings": 1050,     // fare minus platform commission
    "platformCommission": 350   // ~25%
  },
  "actualDistance": 3450,
  "actualDuration": 840,
  "endedAt": "2026-02-20T14:49:00Z",
  "ratingPrompt": true
}
```

### Trip Event Stream (Internal — Event Sourcing)

Each trip state transition is recorded as an immutable event:

```json
[
  { "event": "RIDE_REQUESTED",    "ts": "14:30:00", "location": {...}, "rideType": "UberX" },
  { "event": "DRIVER_MATCHED",    "ts": "14:30:03", "driverId": "drv-456", "etaToPickup": 180 },
  { "event": "DRIVER_EN_ROUTE",   "ts": "14:30:04", "location": {...} },
  { "event": "DRIVER_ARRIVED",    "ts": "14:33:15", "location": {...} },
  { "event": "TRIP_STARTED",      "ts": "14:35:00", "location": {...} },
  { "event": "TRIP_COMPLETED",    "ts": "14:49:00", "location": {...}, "fare": 1400 },
  { "event": "PAYMENT_CHARGED",   "ts": "14:49:05", "amount": 1400, "method": "visa-4242" },
  { "event": "DRIVER_RATED",      "ts": "14:50:30", "rating": 5, "comment": "Great ride!" }
]
```

---

## 7. Payment APIs

### `POST /payments/charge` — Charge Rider (Internal, Async)

```json
Request:
{
  "tripId": "trip-uuid-ghi",
  "riderId": "rider-uuid-xyz",
  "amount": 1400,
  "currency": "USD",
  "paymentMethodId": "pm-uuid-123",
  "breakdown": { ... }
}

Response:
{
  "chargeId": "chg-uuid-jkl",
  "status": "COMPLETED",        // COMPLETED | PENDING | FAILED | REFUNDED
  "processedAt": "2026-02-20T14:49:05Z"
}
```

### `POST /payments/tip` — Add Tip

```json
Request:
{
  "rideId": "ride-uuid-abc",
  "amount": 300,               // cents ($3.00)
  "currency": "USD"
}
```

### `GET /payments/methods` — List Payment Methods

```json
Response:
{
  "methods": [
    { "id": "pm-uuid-123", "type": "CARD", "brand": "Visa", "last4": "4242", "isDefault": true },
    { "id": "pm-uuid-456", "type": "PAYPAL", "email": "user@email.com" },
    { "id": "pm-uuid-789", "type": "UBER_CASH", "balance": 2500 }
  ]
}
```

---

## 8. Rating & Feedback APIs

### `POST /rides/{rideId}/rate` — Rate a Ride

```json
Request:
{
  "rating": 5,                  // 1-5 stars
  "comment": "Great ride!",
  "compliments": ["GREAT_CONVERSATION", "CLEAN_CAR"],
  "ratedBy": "RIDER"            // RIDER | DRIVER
}

Response:
{
  "submitted": true
}
```

**Bidirectional ratings:** Both riders AND drivers rate each other. Driver ratings below ~4.6 trigger deactivation warnings. Rider ratings affect whether drivers accept their ride requests (drivers see rider rating before accepting).

---

## 9. Driver Management APIs

### `GET /drivers/earnings` — Earnings Summary

```json
Response:
{
  "period": "2026-02-17 to 2026-02-23",
  "totalEarnings": 85000,       // cents ($850.00)
  "trips": 42,
  "onlineHours": 38.5,
  "breakdown": {
    "fares": 72000,
    "tips": 8500,
    "surgeBonus": 3500,
    "questBonus": 1000           // incentive for completing N trips
  },
  "nextPayout": "2026-02-24",
  "instantPayAvailable": true
}
```

### `GET /drivers/heat-map` — Demand Heat Map

```json
Response:
{
  "cells": [
    { "h3Cell": "892a100d2c3ffff", "demandLevel": "HIGH", "surgeMultiplier": 1.5 },
    { "h3Cell": "892a100d2c7ffff", "demandLevel": "MEDIUM", "surgeMultiplier": 1.0 },
    ...
  ],
  "updatedAt": "2026-02-20T14:29:00Z"
}
```

---

## 10. Safety APIs

### `POST /safety/share-trip` — Share Live Trip

```json
Request:
{
  "tripId": "trip-uuid-ghi",
  "contacts": [
    { "name": "Mom", "phone": "+1234567890" }
  ]
}

Response:
{
  "shareUrl": "https://uber.com/trip/share/token-abc",  // live tracking link
  "expiresAt": "2026-02-20T16:00:00Z"
}
```

### `POST /safety/emergency` — Trigger Emergency

```json
Request:
{
  "tripId": "trip-uuid-ghi",
  "type": "CALL_911"            // CALL_911 | REPORT_INCIDENT | RECORD_AUDIO
}

Response:
{
  "emergencyId": "emg-uuid-mno",
  "status": "INITIATED",
  "locationSharedWith911": true,
  "audioRecordingStarted": true
}
```

---

## 11. Maps & Geocoding APIs

### `GET /places/autocomplete` — Address Autocomplete ★

```json
Request (query params):
  q=350+5th&lat=40.7484&lng=-73.9857&limit=5

Response:
{
  "predictions": [
    {
      "placeId": "place-uuid-001",
      "description": "350 5th Ave, New York, NY 10118",
      "mainText": "350 5th Ave",
      "secondaryText": "New York, NY",
      "location": { "lat": 40.7484, "lng": -73.9857 }
    },
    ...
  ]
}
```

**Latency:** <100ms — runs on every keystroke. Uses prefix index with location bias (results near the rider ranked higher).

### `GET /geocode` — Forward Geocode

```json
Request: ?address=Empire+State+Building
Response:
{
  "location": { "lat": 40.7484, "lng": -73.9857 },
  "formattedAddress": "350 5th Ave, New York, NY 10118"
}
```

### `GET /reverse-geocode` — Reverse Geocode

```json
Request: ?lat=40.7484&lng=-73.9857
Response:
{
  "formattedAddress": "350 5th Ave, New York, NY 10118",
  "neighborhood": "Midtown Manhattan",
  "city": "New York",
  "country": "US"
}
```

---

## 12. API Model Contrasts

### Uber vs Traditional Taxi

| Dimension | Uber | Traditional Taxi |
|---|---|---|
| **Dispatch** | Automated, real-time, GPS-based | Manual, radio-based, memory-based |
| **Pricing** | Dynamic (surge), upfront quote | Fixed meter or flat rate |
| **Matching** | Algorithmic (ETA, rating, heading) | Manual (dispatcher assigns by proximity guess) |
| **Tracking** | Real-time GPS on map | None — rider waits and hopes |
| **Ratings** | Bidirectional (rider ↔ driver) | None (or one-directional) |
| **Payment** | Cashless, in-app, automatic | Cash or card at end |
| **ETA** | Route-based with traffic + ML | "About 10 minutes" (dispatcher guess) |

### Uber vs Food Delivery (DoorDash / Uber Eats)

| Dimension | Uber Ride-Sharing | DoorDash / Uber Eats |
|---|---|---|
| **Matching** | Two-party (rider ↔ driver) | Three-party (customer ↔ restaurant ↔ courier) |
| **Prep time** | None — trip starts on pickup | Restaurant cooking time (unpredictable) |
| **Batching** | Rare (Pool has 2-3 riders) | Aggressive (courier picks up from 2-3 restaurants) |
| **Route** | Pickup → dropoff (one segment) | Pickup → restaurant → dropoff (two segments) |
| **ETA complexity** | Travel time only | Prep time + travel time (prep dominates variance) |
| **Supply** | Elastic (drivers go online/offline) | Partially elastic (couriers) + fixed (restaurants) |
| **Payment flow** | Rider → platform → driver | Customer → platform → restaurant + courier |

### Uber vs Google Maps

| Dimension | Uber | Google Maps |
|---|---|---|
| **Purpose** | Transportation marketplace (matching + payments) | Navigation and information tool |
| **Spatial data** | Dynamic (millions of moving drivers) | Mostly static (roads, POIs, businesses) |
| **Update rate** | GPS every 3-4 seconds per driver | Road data updated daily/weekly |
| **ETA** | Driver-specific (considers driver position, heading, traffic) | General (route-based, traffic-aware) |
| **Traffic source** | Driver GPS traces (dense, ride-specific roads) | Android phones (broad, all roads) |
| **Routing** | Ride-specific (pickup side of road, no U-turns) | General-purpose navigation |
| **Monetization** | Platform commission on rides | Ads, enterprise API fees |
