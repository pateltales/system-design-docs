# Real-Time Communication — Push Infrastructure & Live Tracking

> A rider requests a ride. Within seconds, they see a driver's car icon appear on their map
> and watch it move toward them in real-time. The driver sees turn-by-turn navigation.
> Both receive instant notifications of trip events.
> This real-time experience is what makes Uber feel like magic — and it's an enormous
> infrastructure challenge.

---

## Table of Contents

1. [Real-Time Features Overview](#1-real-time-features-overview)
2. [WebSocket Gateway](#2-websocket-gateway)
3. [Push Notifications (APNs / FCM)](#3-push-notifications-apns--fcm)
4. [Live Driver Tracking](#4-live-driver-tracking)
5. [Ride Offer Delivery](#5-ride-offer-delivery)
6. [Chat & Masked Calling](#6-chat--masked-calling)
7. [Scaling the WebSocket Gateway](#7-scaling-the-websocket-gateway)
8. [Contrasts](#8-contrasts)

---

## 1. Real-Time Features Overview

```
Real-time data flows in Uber:

  Driver → Server (upstream):
    • GPS location (every 3-4 seconds)
    • Ride offer acceptance/decline
    • Trip start/end actions
    • Chat messages

  Server → Driver (downstream):
    • Ride offers (accept within 15 seconds)
    • Navigation updates (turn-by-turn)
    • Reroute suggestions
    • Trip event notifications
    • Surge zone heat map updates

  Server → Rider (downstream):
    • Driver location during DRIVER_EN_ROUTE (every 1-2 seconds on map)
    • Driver location during IN_PROGRESS (every 1-2 seconds on map)
    • ETA updates (every few seconds)
    • Trip event notifications (driver arrived, trip started, trip completed)
    • Fare receipt (post-trip)
    • Chat messages from driver

  Rider → Server (upstream):
    • Ride request
    • Ride cancellation
    • Chat messages
    • Destination changes (mid-trip)

Latency requirements:
  • Ride offer delivery: <1 second (driver must see it ASAP)
  • Driver location update on rider's map: <2 seconds
    (GPS captured → server → rider's phone → rendered on map)
  • Trip event notification: <3 seconds
  • Chat message delivery: <2 seconds
```

---

## 2. WebSocket Gateway

```
Architecture:

┌──────────┐       ┌──────────────────┐       ┌────────────────┐
│  Mobile  │──────>│  Load Balancer   │──────>│  WebSocket     │
│  App     │<──────│  (L4, sticky     │<──────│  Gateway       │
│          │  WS   │   sessions via   │  WS   │  Instance #1   │
└──────────┘       │   connection ID) │       │                │
                   └──────────────────┘       │  Connections:  │
                                              │  userId_A ──── │
                                              │  userId_B ──── │
                                              │  userId_C ──── │
                                              └────────┬───────┘
                                                       │
                                              ┌────────▼───────┐
                                              │  Redis          │
                                              │                 │
                                              │  userId → {     │
                                              │    gatewayId,   │
                                              │    connectionId │
                                              │  }              │
                                              └────────┬───────┘
                                                       │
                                              ┌────────▼───────┐
                                              │  Backend       │
                                              │  Services      │
                                              │  (Dispatch,    │
                                              │   Trip, etc.)  │
                                              └────────────────┘

How it works:
  1. App opens → establishes WebSocket connection to the gateway
  2. Gateway registers: userId → (gatewayInstanceId, connectionId) in Redis
  3. When a backend service needs to push to userId:
     a. Look up userId in Redis → find gatewayInstanceId
     b. Send message to that specific gateway instance
     c. Gateway pushes the message to the user's WebSocket connection
  4. When the user sends a message (upstream):
     a. Gateway receives on the WebSocket
     b. Routes to the appropriate backend service via internal RPC

Connection lifecycle:
  CONNECT: App establishes WebSocket, authenticates with JWT
  HEARTBEAT: Every 30 seconds, both sides send pings to detect dead connections
  DISCONNECT: App closes connection, or heartbeat times out (60s)
  RECONNECT: App detects disconnect → reconnects to any gateway instance
             → re-registers in Redis → resumes receiving events
```

### Message Format

```
WebSocket messages use a lightweight binary protocol (not JSON)
to minimize bandwidth on mobile networks.

Message structure:
  ┌──────────┬───────────┬──────────┬──────────────┐
  │ msg_type │ msg_id    │ payload  │ timestamp    │
  │ (1 byte) │ (8 bytes) │ (varies) │ (8 bytes)    │
  └──────────┴───────────┴──────────┴──────────────┘

Message types:
  0x01: LOCATION_UPDATE (driver → server)
  0x02: RIDE_OFFER (server → driver)
  0x03: OFFER_RESPONSE (driver → server)
  0x04: DRIVER_LOCATION (server → rider — for live tracking)
  0x05: TRIP_EVENT (server → rider/driver)
  0x06: ETA_UPDATE (server → rider)
  0x07: CHAT_MESSAGE (bidirectional)
  0x08: HEARTBEAT (bidirectional)
  0x09: NAVIGATION (server → driver)

Why binary, not JSON?
  A GPS location update in JSON: ~200 bytes
    {"lat":40.748817,"lng":-73.985428,"ts":1710512345,"heading":180,"speed":12.5}

  Same update in binary: ~25 bytes
    [0x01][int32 lat][int32 lng][int32 ts][int16 heading][int16 speed]

  At 1.5M updates/second, this saves ~250 MB/second of bandwidth.
```

---

## 3. Push Notifications (APNs / FCM)

```
Push notifications are the fallback channel when the app is in
the background or the WebSocket connection is not active.

           ┌──────────┐
           │  Backend  │
           │  Service  │
           └─────┬─────┘
                 │
        ┌────────▼────────┐
        │  Push Service   │
        │                 │
        │  Determines:    │
        │  1. Is WebSocket│
        │     connected?  │
        │     → Send via  │
        │     WebSocket   │
        │  2. If not →    │
        │     send push   │
        │     via APNs/   │
        │     FCM         │
        │  3. For critical│
        │     events →    │
        │     send BOTH   │
        └───┬────────┬────┘
            │        │
      ┌─────▼──┐  ┌──▼──────┐
      │  APNs  │  │  FCM    │
      │ (iOS)  │  │(Android)│
      └────────┘  └─────────┘

Delivery strategy by event type:
  ┌───────────────────────┬──────────┬──────────┬────────────┐
  │ Event                 │ WebSocket│ Push     │ Strategy   │
  ├───────────────────────┼──────────┼──────────┼────────────┤
  │ Ride offer (to driver)│ ✓        │ ✓        │ BOTH       │
  │ Driver location       │ ✓        │ ✗        │ WS only    │
  │ Trip started          │ ✓        │ ✓        │ WS + push  │
  │ Trip completed        │ ✓        │ ✓        │ WS + push  │
  │ ETA update            │ ✓        │ ✗        │ WS only    │
  │ Payment receipt       │ ✗        │ ✓        │ Push only  │
  │ Promo notification    │ ✗        │ ✓        │ Push only  │
  │ Safety alert          │ ✓        │ ✓        │ BOTH       │
  └───────────────────────┴──────────┴──────────┴────────────┘

Deduplication:
  When both WebSocket and push are sent for the same event,
  the app deduplicates by event ID (msgId).
  Whichever arrives first is processed; the duplicate is ignored.

Push notification limitations:
  • Latency: 1-5 seconds (APNs/FCM are best-effort, not real-time)
  • Reliability: not guaranteed (push can be delayed or dropped)
  • Rate limits: APNs and FCM have rate limits per device
  • No streaming: push is one-shot (can't stream GPS updates via push)
  → Push is only for discrete events, not continuous data streams
```

---

## 4. Live Driver Tracking

The signature Uber experience: watching the car icon move on your map.

```
Data flow:

  Driver's phone          Uber backend              Rider's phone
  ┌───────────┐          ┌──────────────┐          ┌───────────┐
  │ GPS chip  │──3-4s───>│ Location     │──1-2s───>│ Map view  │
  │ captures  │  update  │ Service      │  push    │ renders   │
  │ location  │          │              │  via WS  │ car icon  │
  │           │          │ • Map-match  │          │           │
  │           │          │ • Filter     │          │ • Interp- │
  │           │          │ • Route snap │          │   olate   │
  │           │          │              │          │   between │
  │           │          │              │          │   updates │
  └───────────┘          └──────────────┘          └───────────┘

  Total latency: ~2-4 seconds from driver's GPS to rider's map.

Server-side processing:
  1. Receive driver GPS (lat, lng, heading, speed, accuracy, timestamp)
  2. Map-match to road segment (HMM/Viterbi — see 05-eta-and-routing.md)
  3. Snap to road geometry (the car icon should be ON the road, not in a building)
  4. Filter out noisy points (GPS jump > 100m in 3 seconds = noise, discard)
  5. Compute updated ETA to rider's location
  6. Package: { snappedLat, snappedLng, heading, eta, roadName }
  7. Push to rider via WebSocket

Client-side rendering:
  The rider's app receives location updates every 1-2 seconds.
  But rendering at 1-2 FPS would look choppy.

  Solution: INTERPOLATION
  The app animates the car icon between received positions at 30-60 FPS.

  When update arrives: { lat: 40.7490, lng: -73.9854, heading: 180° }
  Next update expected in ~2 seconds.
  App smoothly animates the car icon along the road geometry
  toward the estimated next position (based on heading and speed).
  When the actual next update arrives, the app corrects the position.

  This creates a smooth, continuous animation even though actual
  GPS updates are discrete and infrequent.
```

### What Riders See vs What's Real

```
What the rider sees:
  A car icon moving smoothly along roads on their map.
  "Your driver is 3 minutes away."

What's actually happening:
  • Driver's phone GPS has ±5-10m accuracy
  • GPS updates arrive every 3-4 seconds (not continuously)
  • Server map-matches and snaps to road (correcting GPS noise)
  • Server sends to rider every 1-2 seconds
  • Rider's app interpolates between positions (animation)
  • The car icon on the map is an educated guess of where the driver
    is RIGHT NOW, based on the last known position + heading + speed

This is "good enough" for the user experience:
  The rider knows approximately where the driver is.
  The ETA is continuously refined with each GPS update.
  The driver arrives at the predicted time ±30 seconds.
```

---

## 5. Ride Offer Delivery

```
When the dispatch system selects a driver for a ride, the offer
must reach the driver's phone within ~1 second.

Ride offer delivery flow:
  1. Dispatch Service decides: "Driver D1 gets this ride offer"
  2. Dispatch Service publishes RIDE_OFFER event to Kafka
     AND makes direct RPC to Push Service (for latency)
  3. Push Service checks Redis: is D1 connected via WebSocket?
     YES → find D1's gateway instance → send via WebSocket
     ALSO → send via APNs/FCM as backup
  4. Driver's app receives the offer (displays ride details, timer)
  5. Driver has 15 seconds to accept or decline
  6. Response sent back via WebSocket (or HTTP if WS is down)
  7. If no response in 15 seconds → timeout → treated as decline
  8. If declined or timed out → Dispatch cascades to next driver

Why both WebSocket AND push notification?
  • WebSocket is faster (~100ms) but may be disconnected
    (driver's phone lost signal briefly, app was backgrounded)
  • Push notification is slower (~1-5s) but more reliable
    (works even when app is in background)
  • For ride offers, missing it = lost revenue for driver, delayed
    ride for rider. So we send both and let the app deduplicate.

Ride offer payload:
  {
    "offerId": "offer_abc123",
    "riderId": "rider_xyz",
    "pickup": { "lat": 40.7484, "lng": -73.9856, "address": "350 5th Ave" },
    "rideType": "UberX",
    "etaToPickup": 180,  // seconds
    "heading": "SSW",     // rider is southwest of driver
    "surgeMultiplier": 1.5,
    "estimatedFare": { "min": 12, "max": 18 },
    "expiresAt": "2024-03-15T14:31:00Z"  // 15-second window
  }
```

---

## 6. Chat & Masked Calling

```
In-app communication between rider and driver:

Chat:
  • Rider and driver can exchange text messages via in-app chat
  • Messages routed through Uber's servers (not direct phone-to-phone)
  • Delivered via WebSocket (real-time) + push notification (background)
  • Chat is only available during an active trip (MATCHING → COMPLETED)
  • Chat history is stored and accessible for safety/support investigations

Masked calling:
  • Rider and driver can call each other through the app
  • Neither party sees the other's real phone number
  • How it works:
    1. Uber assigns a temporary proxy phone number to the trip
       (e.g., a Twilio number)
    2. When rider calls the proxy number, Twilio routes to driver's
       real phone number (and vice versa)
    3. Caller ID shows the proxy number, not the real number
    4. After the trip ends (+ grace period), the proxy number
       is released and recycled for another trip

  Why masked numbers?
    • Privacy: driver doesn't have rider's personal number (and vice versa)
    • Safety: prevents unwanted post-trip contact
    • Recyclable: Uber doesn't need a permanent number per user,
      just a pool of numbers for active trips
    • Audit: all calls through the proxy are logged (duration, timestamp)
```

---

## 7. Scaling the WebSocket Gateway

```
Scale challenge:
  • ~5M active drivers sending GPS every 3-4 seconds
  • ~10-20M riders with open apps (checking ETA, tracking driver)
  • Each needs a persistent WebSocket connection
  • Peak: ~15-25M concurrent connections

A single server can handle ~50,000-100,000 WebSocket connections
(limited by file descriptors, memory for connection state).

For 20M connections: need ~200-400 gateway instances.

Scaling strategy:

1. HORIZONTAL SCALING with connection routing:
   • Add more gateway instances behind the load balancer
   • Each instance manages its own set of connections
   • Redis stores the mapping: userId → gatewayInstanceId
   • When a service needs to push to userId:
     look up in Redis → route to the correct gateway

2. STICKY SESSIONS at the load balancer:
   • WebSocket requires sticky sessions (the connection
     must stay with the same gateway instance)
   • L4 load balancer hashes on connection ID
   • If the gateway instance dies → connection drops
     → client reconnects → gets a new gateway instance

3. PARTITIONING by geography:
   • Gateway clusters per region (US-East, US-West, EU, APAC)
   • Reduces cross-region latency
   • A driver in NYC connects to US-East gateway
   • A rider tracking that driver also connects to US-East

4. CONNECTION STATE MANAGEMENT:
   • Connection state is minimal (userId, auth token, subscriptions)
   • All trip state is in backend services (not in the gateway)
   • Gateway is "dumb" — just a message router
   • If a gateway dies, the client reconnects to a new one
     and resumes where it left off (subscriptions re-established)

5. BACKPRESSURE:
   • If a gateway is overwhelmed (too many messages to push):
     → Buffer messages in memory (limited)
     → Drop old location updates (newer ones supersede)
     → Never drop ride offers or trip events (critical)
   • Priority queue: ride offers > trip events > ETA updates > location updates
```

### Gateway Failure Handling

```
When a gateway instance crashes:

  1. All connections on that instance drop (~50K-100K connections)
  2. Clients detect disconnect (heartbeat timeout or TCP reset)
  3. Clients reconnect to load balancer → assigned to a new gateway
  4. New gateway registers the connection in Redis
  5. Client re-subscribes to its trip/driver updates
  6. Brief gap (2-5 seconds) where updates are missed
     → Client requests latest state via HTTP (catch-up)
     → Resumes real-time updates via new WebSocket

Impact:
  • 50K-100K users experience a 2-5 second gap in live tracking
  • No trip state is lost (all state is in backend services)
  • No ride offers are lost (pushed via both WS and push notification)
  • Transparent to the user in most cases (auto-reconnect is fast)
```

---

## 8. Contrasts

### Uber (WebSocket) vs Instagram (MQTT)

| Dimension | Uber (WebSocket) | Instagram (MQTT) |
|---|---|---|
| **Primary use** | Driver GPS tracking, ride offers, trip events | Notifications, DM delivery, presence |
| **Data volume per connection** | High (GPS every 1-2s, route polylines) | Low (sporadic notifications) |
| **Direction** | Bidirectional (driver sends GPS, receives offers) | Mostly server→client (notifications) |
| **App state** | Foreground during trips (user is actively watching) | Often background (notifications arrive while user is away) |
| **Battery priority** | Lower (app is in foreground, phone is usually charging in car) | Higher (must preserve battery — Instagram shouldn't drain battery in background) |
| **Header overhead** | WebSocket: 2-14 bytes per frame | MQTT: 2 bytes minimum per packet |
| **Connection count** | ~15-25M concurrent | ~500M+ concurrent (much larger user base) |
| **Update frequency** | Every 1-2 seconds (continuous stream) | Sporadic (triggered by user actions, not continuous) |
| **Why this choice?** | WebSocket: bidirectional, high throughput, fine for foreground apps | MQTT: battery-efficient, tiny overhead, designed for unreliable mobile networks |

### Uber vs WhatsApp Message Delivery

| Dimension | Uber Real-Time Tracking | WhatsApp Messaging |
|---|---|---|
| **Delivery guarantee** | Best-effort (missed GPS update replaced by next one) | Exactly-once (message must be delivered and acknowledged) |
| **Stale data** | Acceptable (a 3-second-old position is fine for map display) | Unacceptable (a 3-day-old undelivered message is a bug) |
| **Store-and-forward** | No (old GPS updates are useless) | Yes (messages queued when recipient is offline, delivered on reconnect) |
| **Ordering** | Desirable but not critical (out-of-order GPS → slight jitter) | Critical (messages must appear in order) |
| **Offline behavior** | No tracking possible when offline | Messages stored server-side, delivered when user comes online |
| **End-to-end encryption** | Not needed (Uber server processes the data) | Essential (privacy requirement) |

### Real-Time Architecture Trade-offs

```
WebSocket vs Server-Sent Events (SSE) vs Long Polling:

  WebSocket:
    + Bidirectional (driver sends AND receives through same connection)
    + Low latency (<100ms per message)
    + Efficient for high-frequency updates
    - Stateful connections → complex scaling
    - Sticky sessions needed at load balancer
    - Connection management overhead

  SSE (Server-Sent Events):
    + Simpler than WebSocket (HTTP-based, no special protocol)
    + Works through HTTP proxies (easier to deploy)
    + Automatic reconnection built into the browser/client
    - Unidirectional (server → client only)
    - Driver would need a separate channel to send GPS updates
    - Limited to text data (no binary support in standard SSE)

  Long Polling:
    + Simplest to implement (regular HTTP requests)
    + Works everywhere (no special protocol support needed)
    - High overhead (new HTTP connection for each update)
    - Higher latency (must wait for poll interval)
    - Wastes bandwidth (empty responses when no updates)

  Why Uber chose WebSocket:
    Both driver and rider need bidirectional communication.
    High update frequency (GPS every 1-2s) makes polling wasteful.
    Binary protocol support reduces bandwidth on mobile networks.
    The complexity of connection management is worth the performance gains
    at Uber's scale.
```
