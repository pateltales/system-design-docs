# WebSocket Connection Management & Presence

> **Deep-dive companion doc for the WhatsApp system design interview simulation.**
> Referenced from: [01-interview-simulation.md](./01-interview-simulation.md)

---

## Table of Contents

1. [Connection Gateway Architecture](#1-connection-gateway-architecture)
2. [Connection State Management](#2-connection-state-management)
3. [Heartbeat / Keepalive](#3-heartbeat--keepalive)
4. [Reconnection Handling](#4-reconnection-handling)
5. [Presence System](#5-presence-system)
6. [Typing Indicators](#6-typing-indicators)
7. [Multi-Device Support](#7-multi-device-support)
8. [Scale Numbers](#8-scale-numbers)
9. [Erlang/BEAM Deep Dive](#9-erlangbeam-deep-dive)
10. [Contrast: Slack and Discord](#10-contrast-slack-and-discord)

---

## 1. Connection Gateway Architecture

### The Core Problem

A chat application is fundamentally a connection management problem. Every active user holds an open, persistent connection to a server. Unlike HTTP request-response where a server handles a request in milliseconds and moves on, a WebSocket connection lives for minutes, hours, or days. The server must maintain state for every single connected user simultaneously.

At WhatsApp's scale (50-100 million concurrent connections at peak), this is the defining infrastructure challenge.

### Architecture Overview

```
                           +----------------------------------+
                           |         Client Devices           |
                           |  (iOS, Android, Web, Desktop)    |
                           +----------|------|----------------+
                                      |      |
                                      v      v
                           +-------------------------+
                           |    L4 Load Balancer     |
                           |  (TCP-level, NOT L7)    |
                           |  - Consistent hashing   |
                           |  - Health checks        |
                           +-----|--------|----------+
                                 |        |
                    +------------+        +------------+
                    v                                  v
         +-------------------+              +-------------------+
         | Gateway Server 1  |              | Gateway Server N  |
         | (Erlang/BEAM)     |              | (Erlang/BEAM)     |
         |                   |              |                   |
         | +--+ +--+ +--+   |              | +--+ +--+ +--+   |
         | |P1| |P2| |P3|   |              | |P1| |P2| |P3|   |
         | +--+ +--+ +--+   |              | +--+ +--+ +--+   |
         | ...100K-2.8M     |              | ...100K-2.8M     |
         | BEAM processes    |              | BEAM processes    |
         +---------|--------+              +---------|--------+
                   |                                 |
                   +----------------+----------------+
                                    |
                                    v
                     +-----------------------------+
                     |   Connection Registry       |
                     |   (Redis Cluster)           |
                     |                             |
                     |  userId -> {gateway, connId,|
                     |             deviceId, ts}   |
                     +-----------------------------+
                                    |
                                    v
                     +-----------------------------+
                     |   Message Routing Layer     |
                     |   (Stateless)               |
                     |                             |
                     |  Lookup recipient gateway   |
                     |  Forward encrypted message  |
                     +-----------------------------+
```

### Why L4 Load Balancing, Not L7

This is a critical architectural decision that an L6 candidate must understand deeply.

**L7 (application-layer) load balancers** inspect HTTP headers, cookies, and URLs to make routing decisions. They terminate the TCP connection at the load balancer and create a new one to the backend. For WebSocket, this means:

- The L7 LB must maintain state for every WebSocket connection (it is in the middle of the connection).
- It must parse WebSocket frames, which adds latency to every message.
- It becomes a bottleneck and a single point of failure for all connections passing through it.
- It doubles the number of TCP connections (client-to-LB + LB-to-backend).

**L4 (transport-layer) load balancers** operate at the TCP level. They route packets based on IP and port, without inspecting application-level content:

- The TCP connection goes directly from client to gateway server (L4 LB just forwards packets).
- No per-connection state at the load balancer (or very minimal state for connection tracking).
- No frame parsing overhead.
- Much higher throughput: a single L4 LB can handle millions of concurrent connections.

| Property | L4 Load Balancer | L7 Load Balancer |
|----------|-----------------|-----------------|
| Connection termination | Pass-through (client connects directly to backend) | Terminates at LB, new connection to backend |
| Per-connection state | Minimal (IP/port tuple) | Full (HTTP headers, WebSocket frames) |
| Throughput | Millions of concurrent connections | Hundreds of thousands (limited by frame parsing) |
| Routing granularity | IP hash, round-robin, least connections | URL path, headers, cookies |
| WebSocket awareness | None needed (just forwards TCP packets) | Must understand WebSocket upgrade, frame protocol |
| Latency overhead | Near zero (packet forwarding) | Measurable (frame parsing, buffering) |

For long-lived WebSocket connections, L4 is the clear winner. WhatsApp and similar systems use L4 load balancing (e.g., IPVS, Maglev, or hardware LBs) for gateway traffic.

### Token Authentication on Handshake

The WebSocket connection is established with a standard HTTP upgrade request. Authentication happens during the handshake:

1. Client sends HTTP upgrade request with a JWT or session token in the `Authorization` header (or as a query parameter for environments that do not support custom headers on WebSocket upgrade).
2. Gateway server validates the token (checks signature, expiry, revocation status).
3. If valid: complete the WebSocket upgrade, register the connection in the registry.
4. If invalid: reject the upgrade with HTTP 401, close the connection.

After the handshake, no further authentication is needed for the lifetime of the connection. The connection itself is the authentication context. Every message sent on this connection is implicitly authenticated as coming from the user who established it.

**Security consideration**: Token validation must be fast (sub-millisecond). The gateway server should cache the public keys used for JWT verification and avoid making a remote call to an auth service on every connection attempt. During a reconnection storm (e.g., after a gateway failure), millions of connections may authenticate simultaneously.

### Erlang/BEAM Process Model for Connections

Each WebSocket connection is managed by a dedicated Erlang process (not an OS thread, not a goroutine --- an Erlang process). This is the key enabler for WhatsApp's connection density:

- **Memory per process**: ~2 KB initial heap. A process representing an idle connection consumes roughly 2-5 KB.
- **Process count**: A single BEAM VM node can run millions of processes. Rick Reed reported 2 million concurrent connections on a single server at WhatsApp (Erlang Factory SF 2012), later pushing to 2.8 million connections per server with tuning.
- **Supervisor trees**: Processes are organized in supervision hierarchies. If a connection process crashes (malformed message, protocol error), its supervisor restarts it cleanly. The crash does NOT affect any other connection.
- **Preemptive scheduling**: The BEAM scheduler gives each process a fixed number of reductions (roughly corresponding to function calls). No single connection can starve others, even if it is processing a large message or doing expensive work.

```
          +---------------------+
          |   Application       |
          |   Supervisor        |
          +---------|----------+
                    |
          +---------+---------+
          |                   |
  +-------|------+    +-------|------+
  | Connection   |    | Registry     |
  | Supervisor   |    | Supervisor   |
  +-------|------+    +--------------+
          |
  +-------+-------+-------+
  |       |       |       |
+---+  +---+  +---+  +---+
|C1 |  |C2 |  |C3 |  |CN |    <-- One process per connection
+---+  +---+  +---+  +---+
```

Each connection process (`C1`, `C2`, ..., `CN`) is an independent actor that:
- Holds the WebSocket state (user ID, device ID, encryption session info).
- Receives incoming WebSocket frames from the client.
- Receives messages from the routing layer destined for this user.
- Sends outgoing WebSocket frames to the client.
- Manages heartbeat timers.
- Handles graceful shutdown on disconnect.

---

## 2. Connection State Management

### The Mapping Problem

When User A sends a message to User B, the message routing layer must answer: **"Which gateway server is User B connected to?"** This requires a global mapping:

```
userId -> { gatewayServerId, connectionId, deviceId, connectedAt }
```

This mapping must be:
- **Fast to read**: Every message delivery requires a lookup (sub-millisecond).
- **Fast to write**: Every connect/disconnect updates the mapping.
- **Highly available**: If the registry is down, no messages can be routed.
- **Eventually consistent is acceptable**: A stale entry (user disconnected but registry not yet updated) is handled by the gateway returning a "user not connected" response, triggering offline queue fallback.

### Redis Cluster as Connection Registry

WhatsApp's internal implementation uses Erlang's Mnesia (a distributed database built into OTP), but for a system design interview, Redis Cluster is the standard answer and a good mental model.

**Data structure** (per user, per device):

```
Key:    conn:{userId}:{deviceId}
Value:  { "gateway": "gw-server-042",
          "connId":  "conn-a1b2c3d4",
          "ts":      1708444800 }
TTL:    120 seconds (refreshed on every heartbeat)
```

**Why TTL-based expiry?**

If a gateway server crashes, it cannot clean up its own registry entries. Without TTL, those entries become stale --- the routing layer would keep trying to deliver messages to a dead server. TTL solves this automatically:

- On every heartbeat (every 30-60 seconds), the gateway refreshes the TTL on the connection's registry entry.
- If the gateway crashes, heartbeats stop, and the entry expires within 2 minutes.
- The routing layer detects the stale entry (gateway does not respond or the key is gone) and falls back to the offline queue.

**Sharding strategy**:

Redis Cluster shards by key hash. Since keys are `conn:{userId}:{deviceId}`, the sharding is naturally distributed across users. To ensure all of a user's device connections land on the same shard (enabling atomic multi-device lookup), use Redis hash tags:

```
Key:    conn:{userId}:device1    -- all keys with same {userId} hash tag
Key:    conn:{userId}:device2    -- land on the same Redis shard
```

This allows a single `SCAN` or `MGET` to retrieve all active connections for a user across devices.

**Scale math**:

- 100 million concurrent connections.
- Each registry entry: ~200 bytes (key + value + overhead).
- Total registry size: 100M x 200 bytes = ~20 GB.
- A Redis Cluster with 10-20 shards handles this comfortably, with each shard holding 1-2 GB of connection data.
- Read throughput: routing layer performs ~1.15 million lookups/second (one per message). Each Redis shard handles ~100K lookups/second, so 12+ shards are sufficient.

### Lookup Flow for Message Routing

```
1. Message arrives at routing layer: "deliver to userId=Bob"
2. Routing layer queries Redis: GET conn:Bob:*
3. Redis returns: [
     { gateway: "gw-042", connId: "conn-x", device: "phone" },
     { gateway: "gw-017", connId: "conn-y", device: "web" }
   ]
4. Routing layer forwards message to gw-042 AND gw-017 (all active devices)
5. Each gateway delivers to the specific connection process
6. If gateway returns "connection not found" (stale entry):
   - Delete the stale registry entry
   - Queue message in offline queue for that device
```

---

## 3. Heartbeat / Keepalive

### Why Heartbeats Are Essential

Heartbeats solve three distinct problems:

1. **Dead connection detection**: TCP connections can die silently. If a client loses network connectivity (enters a tunnel, switches to airplane mode), the server has no way to know --- TCP does not send a notification. Without heartbeats, the server would hold a dead connection indefinitely, wasting resources and causing message delivery failures (messages sent to a dead connection are lost).

2. **NAT timeout prevention**: This is the most critical reason for mobile chat applications. Mobile networks use Network Address Translation (NAT) to share IP addresses. NAT devices maintain a mapping table of internal-to-external address translations. These mappings have timeouts --- typically 30-120 seconds for TCP on cellular networks (some aggressive carriers use as little as 30 seconds). If no traffic flows on the connection within the timeout window, the NAT mapping is deleted, and the connection is effectively severed --- any subsequent packets from the server will be dropped because the NAT device no longer knows where to route them. Regular heartbeats keep the NAT mapping alive.

3. **Connection registry TTL refresh**: As described in section 2, the connection registry uses TTL-based expiry. Heartbeats trigger TTL refresh.

### Protocol Design

```
Client                          Server
  |                               |
  |--- PING (timestamp) -------->|  Every 30-60 seconds
  |                               |  Server refreshes:
  |                               |  - Connection state
  |                               |  - Registry TTL
  |                               |  - Last-seen timestamp
  |<------ PONG (timestamp) -----|
  |                               |
  |  ... 30-60 seconds pass ...   |
  |                               |
  |--- PING (timestamp) -------->|
  |<------ PONG (timestamp) -----|
  |                               |
  |  ... client loses network ... |
  |                               |
  |  (no PING arrives)            |
  |                               |  After 2 missed intervals
  |                               |  (60-120 seconds):
  |                               |  - Mark connection dead
  |                               |  - Update presence: offline
  |                               |  - Clean up resources
  |                               |  - Registry entry expires via TTL
```

### Adaptive Heartbeat Intervals

Not all network conditions are the same. An aggressive 30-second interval works well on Wi-Fi (low battery cost, reliable network), but on cellular networks, every heartbeat wakes the radio, consuming battery. WhatsApp and similar apps use adaptive intervals:

| Network Condition | Heartbeat Interval | Rationale |
|---|---|---|
| Wi-Fi, stable | 60 seconds | Battery-friendly, NAT timeouts are generous on home routers |
| Cellular (4G/5G) | 30-45 seconds | Carrier NAT timeouts vary; 30s is safe for most |
| Cellular (poor signal) | 30 seconds | Aggressive to detect dead connections quickly |
| Background (mobile OS) | OS-managed push | iOS/Android suspend background connections; rely on push notifications |

**Mobile OS complications**: iOS aggressively suspends background connections. When a chat app goes to the background, iOS may terminate the WebSocket connection after ~30 seconds to ~5 minutes (varies by OS version and battery state). The app must rely on APNs push notifications to wake up and reconnect. Android is more lenient (allows background services with restrictions), but battery optimization (Doze mode) still interferes. This is why push notifications are essential even though the app has WebSocket capability.

### Dead Connection Detection

The server uses a simple state machine per connection:

```
ALIVE ---(missed 1 heartbeat)---> SUSPECT ---(missed 2nd heartbeat)---> DEAD
  ^                                   |
  |                                   |
  +---(heartbeat received)------------+
```

- **ALIVE**: Heartbeats arriving on schedule. Connection is healthy.
- **SUSPECT**: One heartbeat missed. Do not declare offline yet (network jitter, temporary congestion). Continue trying to deliver messages.
- **DEAD**: Two consecutive heartbeats missed (60-120 seconds with no communication). Close the connection, update presence to offline, let the registry entry expire.

The two-miss threshold avoids false positives from transient network issues (a single dropped UDP/TCP packet, brief congestion).

---

## 4. Reconnection Handling

### Why Reconnection Is the Norm, Not the Exception

On mobile networks, disconnections are constant:

- User walks into an elevator (signal loss for 30 seconds).
- Phone switches from Wi-Fi to cellular (connection drops during handoff).
- User enters a subway tunnel (minutes of no connectivity).
- Mobile OS kills the background connection to save battery.
- Carrier performs network maintenance (brief outage).

A chat app that does not handle reconnection gracefully is unusable on mobile. The reconnection protocol must be fast, efficient, and robust.

### Reconnection Protocol

```
Client                                        Server
  |                                              |
  |  (connection lost)                           |
  |                                              |
  |  ... time passes ...                         |
  |                                              |
  |--- WebSocket handshake + auth token -------->|
  |<--- 101 Switching Protocols ----------------|
  |                                              |
  |--- SYNC {                                    |
  |      conversations: {                        |
  |        "conv-abc": { lastSeqNum: 4527 },     |
  |        "conv-xyz": { lastSeqNum: 891 },      |
  |        "conv-def": { lastSeqNum: 15003 }     |
  |      }                                       |
  |    } ---------------------------------------->|
  |                                              |
  |                                 Server looks up offline queue:
  |                                 - conv-abc: messages 4528-4531
  |                                 - conv-xyz: no new messages
  |                                 - conv-def: messages 15004-15009
  |                                              |
  |<--- MSG conv-abc seq=4528 ------------------|
  |<--- MSG conv-abc seq=4529 ------------------|
  |<--- MSG conv-abc seq=4530 ------------------|
  |<--- MSG conv-abc seq=4531 ------------------|
  |<--- MSG conv-def seq=15004 -----------------|
  |<--- MSG conv-def seq=15005 -----------------|
  |  ... (all missed messages) ...               |
  |<--- MSG conv-def seq=15009 -----------------|
  |                                              |
  |--- ACK { conv-abc: 4531,                    |
  |          conv-def: 15009 } ----------------->|
  |                                              |
  |                                 Server removes delivered
  |                                 messages from offline queue
```

### Duplicate Detection

At-least-once delivery means the server may deliver the same message more than once (e.g., the ACK was lost, so the server retries). The client MUST handle duplicates:

- Every message has a globally unique `messageId` (UUID, generated by the sender).
- The client maintains a set of recently seen `messageId` values (last 10,000 or so).
- On receiving a message, the client checks: "Have I seen this `messageId` before?"
- If yes: discard the duplicate, still send an ACK (so the server stops retrying).
- If no: process the message, add `messageId` to the seen set, send ACK.

### Gap Detection

Sequence numbers enable gap detection. If a client has received messages with sequence numbers `[100, 101, 103]`, it knows message `102` is missing. The client can:

1. Wait briefly (100-500 ms) for the missing message to arrive (out-of-order delivery is possible).
2. If the gap persists, explicitly request the missing message: `GET /messages/conv-abc?after=101&before=103`.
3. Fill the gap and display messages in order.

### Thundering Herd After Gateway Failure

When a gateway server crashes, all its connections (potentially 1-2 million) are severed simultaneously. Every affected client will attempt to reconnect at roughly the same time. This is a classic thundering herd problem.

**Without mitigation**: 2 million clients all reconnect within seconds. The load balancer distributes them to remaining gateway servers, which are suddenly hit with a massive spike in:
- TCP handshakes (CPU-intensive: TLS negotiation).
- Authentication requests (token validation).
- Sync requests (offline message delivery for 2 million users).
- Registry writes (2 million new entries).

This can cascade and take down additional gateway servers.

**Mitigation: Exponential backoff with jitter**

Each client independently calculates a random reconnection delay:

```
delay = min(base * 2^attempt, max_delay) + random(0, jitter)

Where:
  base      = 1 second
  max_delay = 60 seconds
  jitter    = random(0, base * 2^attempt)  -- full jitter
  attempt   = number of consecutive failed reconnection attempts
```

| Attempt | Base Delay | Jitter Range | Effective Delay Range |
|---------|-----------|--------------|----------------------|
| 0 | 1s | 0-1s | 1-2s |
| 1 | 2s | 0-2s | 2-4s |
| 2 | 4s | 0-4s | 4-8s |
| 3 | 8s | 0-8s | 8-16s |
| 4 | 16s | 0-16s | 16-32s |
| 5+ | 60s (cap) | 0-60s | 60-120s |

**Why full jitter (not equal jitter)?** Full jitter provides the widest spread of reconnection times. With 2 million clients, full jitter distributes reconnection attempts roughly uniformly over the delay window, preventing spikes. AWS's architecture blog has documented that full jitter outperforms equal jitter and decorrelated jitter in reducing contention.

**Server-side protection**: The gateway servers also implement admission control. If the connection rate exceeds a threshold (e.g., 10,000 new connections/second), the server responds with HTTP 503 + `Retry-After` header, signaling clients to back off further.

---

## 5. Presence System

### The Problem Statement

Presence tracks whether a user is online, offline, or when they were last active. It sounds simple, but at scale it is one of the most expensive features in a chat application.

**The naive approach**: When User A comes online, push a "User A is online" notification to all 500 of A's contacts. When A goes offline, push "User A is offline" to all 500 contacts.

### Quantitative Analysis of Naive Presence

Let us calculate the cost of naive presence at WhatsApp scale:

```
Given:
- 100 million concurrent users at peak
- Average user has 500 contacts
- Average user connects/disconnects 10 times per day
  (morning commute, entering buildings, Wi-Fi/cellular switches, app backgrounded)

Naive fan-out per status change:
  1 status change -> 500 push notifications to contacts

Total presence events per day:
  100M users x 10 status changes/day = 1 billion status changes/day

Total presence notifications per day (naive):
  1 billion status changes x 500 contacts = 500 billion notifications/day

For comparison:
  Total chat messages per day: ~100 billion

Naive presence generates 5x more traffic than actual chat messages.
```

This is clearly unsustainable. Presence would consume more bandwidth and processing power than the core messaging functionality.

### Solution: Lazy Presence (Subscribe-on-View)

Instead of pushing presence to all contacts, only push to users who are actively viewing the contact's information:

```
                    NAIVE PRESENCE                    LAZY PRESENCE

  User A comes      Push to ALL 500               Push ONLY to users who
  online            contacts immediately           have A's chat open

                    +---+                           +---+
                    | B | <-- online notification   | B | <-- has A's chat open
                    +---+                           +---+     gets notification
                    +---+                           +---+
                    | C | <-- online notification   | C |     chat closed,
                    +---+                           +---+     no notification
                    +---+                           +---+
                    | D | <-- online notification   | D |     chat closed,
                    +---+                           +---+     no notification
                    ...                             ...
                    +---+                           +---+
                    |500| <-- online notification   |500|     chat closed,
                    +---+                           +---+     no notification

  Notifications     500                             ~3-5 (only active viewers)
  per event:
```

**How it works**:

1. When User B opens a chat with User A, B's client sends a presence subscription: `SUBSCRIBE presence:A`.
2. The gateway server adds B to A's presence subscriber list (stored in-memory on A's gateway server, or in Redis).
3. When A's status changes, the server only notifies the subscriber list (not all 500 contacts).
4. When B closes the chat or navigates away, B unsubscribes: `UNSUBSCRIBE presence:A`.

**Fan-out reduction**:

```
Assume:
- At any given moment, only 1% of a user's contacts have their chat open
  (this is generous --- most people have 0-2 chats open at a time)

Lazy fan-out per status change:
  500 contacts x 1% = 5 notifications per status change

Total presence notifications per day (lazy):
  1 billion status changes x 5 = 5 billion notifications/day

Reduction: 500 billion -> 5 billion = 99% reduction in presence traffic
```

This 99% reduction makes presence viable at WhatsApp scale.

### Presence for the Contact List Screen

When a user opens the app and sees their contact list, they want to see who is online. With lazy presence, the client does not automatically receive presence for all contacts. Two approaches:

1. **Pull-on-demand**: When the contact list screen loads, the client sends a batch request: `GET /presence?userIds=B,C,D,...` for the visible contacts. The server responds with current status. This is a one-time read, not a persistent subscription.

2. **Subscribe to visible**: The client subscribes to presence for the contacts currently visible on screen. As the user scrolls, subscriptions are updated. This is more complex but provides live updates on the contact list.

WhatsApp likely uses a hybrid: pull presence on load, subscribe to the currently open chat, and rely on eventual consistency for the contact list (a few seconds of staleness is acceptable for the list view).

### Throttling Rapid Toggles

A user driving through an area with spotty cellular coverage might toggle between online and offline dozens of times per minute. Without throttling, this generates a burst of presence events.

**Debounce strategy**:

```
State Machine:

  ONLINE ----(disconnect)----> GRACE_PERIOD (30 seconds)
    ^                               |
    |                               |
    +---(reconnect within 30s)------+
                                    |
                            (30s elapsed, still disconnected)
                                    |
                                    v
                                 OFFLINE
                                    |
                            Push "offline" to subscribers
                            Update lastSeen timestamp
```

- When a user disconnects, do NOT immediately broadcast "offline."
- Enter a 30-second grace period.
- If the user reconnects within 30 seconds (common for network blips), silently restore "online" without any presence broadcast.
- Only after 30 seconds of sustained disconnection, broadcast "offline" and update `lastSeen`.

This eliminates the vast majority of rapid toggle noise. A user experiencing flaky connectivity will appear continuously online (with brief delivery delays) rather than flipping between online/offline every few seconds.

### Presence Data Model

```
Key:    presence:{userId}
Value:  {
          "status":     "online" | "offline",
          "lastSeen":   1708444800,      // Unix timestamp
          "gateway":    "gw-server-042", // which gateway (for routing)
          "updatedAt":  1708444805       // when presence was last updated
        }
TTL:    300 seconds (5 minutes, refreshed by heartbeats)
Store:  Redis (same cluster as connection registry, or dedicated presence cluster)
```

**Privacy note**: WhatsApp allows users to hide their "last seen" from specific contacts or everyone. The presence system must check the user's privacy settings before pushing presence to a subscriber. This is a per-subscriber filter applied at fan-out time.

---

## 6. Typing Indicators

### Design Philosophy: Ephemeral and Best-Effort

Typing indicators are fundamentally different from messages:

| Property | Messages | Typing Indicators |
|----------|----------|-------------------|
| Persistence | Stored until delivered + ACKed | Never persisted |
| Delivery guarantee | At-least-once | Best-effort (fire-and-forget) |
| Retry on failure | Yes, with exponential backoff | No retry |
| Offline queueing | Yes (offline queue) | No --- meaningless if recipient is offline |
| Ordering | Strict (sequence numbers) | Not meaningful |
| TTL | Until delivered | 3-5 seconds |

If a typing indicator is lost, the worst case is that the recipient does not see "User A is typing..." for a few seconds. This is vastly preferable to the overhead of guaranteeing delivery for ephemeral signals.

### Protocol

```
Client A (typing)                Server                    Client B (viewing)
  |                                |                            |
  |--- TYPING_START               |                            |
  |    { convId: "abc",           |                            |
  |      userId: "A" }           |                            |
  |------------------------------>|                            |
  |                                |--- TYPING_INDICATOR       |
  |                                |    { convId: "abc",       |
  |                                |      userId: "A",         |
  |                                |      action: "start" }    |
  |                                |--------------------------->|
  |                                |                            |
  |                                |              Client B shows "A is typing..."
  |                                |              Starts 5-second auto-expire timer
  |                                |                            |
  |  (user still typing,          |                            |
  |   refresh every 3 seconds)    |                            |
  |                                |                            |
  |--- TYPING_START ------------->|--- TYPING_INDICATOR ------>|
  |                                |              Reset 5-second timer
  |                                |                            |
  |  (user stops typing           |                            |
  |   or sends message)           |                            |
  |                                |                            |
  |--- TYPING_STOP -------------->|--- TYPING_INDICATOR ------>|
  |    { action: "stop" }         |    { action: "stop" }      |
  |                                |              Hide "A is typing..."
```

### WebSocket Frame Format

Typing indicators use a lightweight frame format on the existing WebSocket connection:

```json
{
  "type": "typing",
  "conversationId": "conv-abc123",
  "userId": "user-A",
  "action": "start",
  "timestamp": 1708444800
}
```

The frame is small (~100 bytes), does not contain encrypted payload (it is metadata, not message content), and is not acknowledged by the server.

### Rate Limiting

To prevent abuse (or buggy clients flooding typing events), the server enforces:

- Maximum 1 typing event per conversation per second per user.
- Typing events are only forwarded if the recipient has an active presence subscription to the conversation (i.e., the chat is open). If the recipient's chat is not open, the typing event is silently dropped.

### Group Typing Indicators

In a group chat, multiple people may type simultaneously. The server forwards typing indicators from all typing members, and the client displays: "A and B are typing..." or "3 people are typing..."

For large groups (500+ members), the server limits typing indicator fan-out to a maximum of ~5 active typers shown. Additional typing events are suppressed to avoid flooding group members with indicator updates.

---

## 7. Multi-Device Support

### WhatsApp's Model: 1 Phone + 4 Companion Devices

WhatsApp originally required the phone to be online for companion devices (WhatsApp Web) to work --- the phone was the relay. In 2021, WhatsApp launched a true multi-device architecture where companion devices work independently, even when the phone is offline.

### Connection Architecture for Multi-Device

Each device maintains its own independent WebSocket connection:

```
                    +------------+
                    |  User A    |
                    +------|-----+
                           |
            +--------------+--------------+
            |              |              |
     +------+-----+ +-----+------+ +-----+------+
     | Phone      | | Web Client | | Desktop    |
     | (primary)  | | (companion)| | (companion)|
     +------+-----+ +-----+------+ +-----+------+
            |              |              |
            v              v              v
     +------+-----+ +-----+------+ +-----+------+
     | Gateway 12 | | Gateway 7  | | Gateway 7  |
     +-----------+ +-----------+ +-----------+
```

All three connections are independent. They may connect to different gateway servers. The connection registry stores all active connections for User A:

```
conn:{userA}:phone    -> { gateway: "gw-12", connId: "conn-p1" }
conn:{userA}:web      -> { gateway: "gw-7",  connId: "conn-w1" }
conn:{userA}:desktop  -> { gateway: "gw-7",  connId: "conn-d1" }
```

### Message Delivery to All Devices

When User B sends a message to User A, the routing layer must deliver to ALL of A's active devices:

1. Look up all registry entries for User A: 3 connections found.
2. Forward the message to Gateway 12 (for phone) AND Gateway 7 (for web and desktop).
3. Each gateway delivers to the respective connection process.
4. Each device independently decrypts and displays the message.
5. Each device independently sends a delivery ACK.

**Outgoing message sync**: When User A sends a message from their phone, the message must also appear on the web and desktop clients. The server (or the phone itself) sends a copy of the outgoing message to all other active devices so their conversation history stays in sync.

### Per-Device Encryption Keys

With multi-device E2EE, each device has its own identity key pair. When User B sends a message to User A, B must encrypt the message separately for each of A's devices (using each device's public key). This is the "per-device encryption" approach:

```
User B sending to User A (3 devices):

  Plaintext message M
       |
       +-- Encrypt with A.phone.publicKey  --> ciphertext_phone
       +-- Encrypt with A.web.publicKey    --> ciphertext_web
       +-- Encrypt with A.desktop.publicKey --> ciphertext_desktop

  Server receives 3 ciphertexts, delivers each to the corresponding device.
```

This means a message to a user with 5 devices requires 5 encryption operations and 5 ciphertext transmissions. For group messages with 1024 members, each having up to 5 devices, the worst case is 5120 encryption operations. WhatsApp uses Sender Keys for groups to avoid this per-device overhead within the group fan-out (each device of each member shares the group's sender key).

### Device Registration and Linking

When a user links a new companion device:

1. The companion device generates its own identity key pair.
2. The phone and companion device perform a secure pairing (QR code scan containing a cryptographic challenge).
3. The phone encrypts and transfers recent message history to the companion device (end-to-end encrypted, not through the server).
4. The companion device's public key is uploaded to the server's pre-key store.
5. Contacts are notified that the user has a new device (safety number change notification).

---

## 8. Scale Numbers

### WhatsApp's Known Scale

| Metric | Number | Source / Confidence |
|--------|--------|---------------------|
| Registered users | 2+ billion (as of 2020+) | Official WhatsApp/Meta announcements |
| Daily active users | ~500M+ | [INFERRED -- not officially broken out separately in recent years] |
| Messages per day | ~100 billion | Meta earnings calls, widely reported |
| Concurrent connections (peak) | 50-100 million | [INFERRED from user base and typical concurrency ratios] |
| Connections per server (baseline) | ~200K-500K | Reasonable for production with headroom |
| Connections per server (peak, tuned) | 2 million (2012), later ~2.8 million | Rick Reed, Erlang Factory SF 2012; "1 Million is so 2011" WhatsApp blog post (2012) |
| Gateway servers needed | 200-1000 | [INFERRED: 100M connections / 200K-500K per server] |
| Engineering team (2013, pre-acquisition) | ~32 engineers serving 450M users | Widely reported at time of Facebook acquisition announcement (Feb 2014) |
| Engineering team (post-acquisition) | ~50 engineers for 900M+ users | Reported around 2015-2016 |
| Server hardware (2012) | Commodity servers, FreeBSD, each with 100+ GB RAM | Rick Reed's talks |
| Media shared per day | ~6.5 billion items | Meta/WhatsApp official stats |
| Group size limit | 1024 members | WhatsApp product documentation |
| Linked devices | 1 phone + 4 companion devices | WhatsApp product documentation |

### Rick Reed's Connection Benchmarks

Rick Reed, a senior WhatsApp engineer, gave several talks at Erlang conferences documenting their scaling journey:

**Erlang Factory SF 2012 -- "That's 'Billion' with a 'B': Scaling to the Next Level at WhatsApp"**:
- Reached 1 million concurrent TCP connections per server, then pushed to 2 million.
- Each connection was an Erlang process (~2 KB memory).
- Used FreeBSD (not Linux) for its superior network stack performance at high connection counts.
- Key tuning: increased file descriptor limits, tuned the FreeBSD kernel's TCP/IP stack parameters, optimized Erlang's schedulers for NUMA architectures.

**"1 Million is so 2011" (WhatsApp Blog, 2012)**:
- Documented reaching 2 million connections on a single server.
- Server: a single commodity box running FreeBSD and Erlang.

**Later reports** (various industry discussions):
- Pushing toward 2.8 million connections per server with continued optimization.
- At this density: 2.8M connections x 5 KB/connection = ~14 GB of RAM just for connection state. With 100+ GB RAM per server, this is feasible with substantial headroom for message buffering and routing.

### Back-of-Envelope: Gateway Fleet Sizing

```
Peak concurrent connections:  100,000,000
Connections per server:       500,000 (conservative production target,
                                       well below the 2.8M theoretical max,
                                       leaving headroom for spikes and failures)

Servers needed:               100,000,000 / 500,000 = 200 servers

With 50% headroom for:
  - Rolling deploys (drain 1 server at a time)
  - Failure tolerance (lose 10% of fleet without impact)
  - Traffic spikes (holidays, New Year's Eve)

Total gateway fleet:          ~300 servers

Cost context:
  300 servers x ~$10K/month (high-memory cloud instances) = ~$3M/month
  For 2B+ user platform doing $15B+/year revenue, this is negligible.
```

### The "50 Engineers" Lesson

WhatsApp's ability to serve 900 million users with ~50 engineers is a direct consequence of their technology choices:

1. **Erlang/BEAM**: The runtime handles concurrency, fault tolerance, and distribution. Engineers do not write thread synchronization code, connection pool management, or crash recovery logic --- the platform provides these.
2. **FreeBSD**: Mature, stable, excellent networking performance. Fewer operational surprises than bleeding-edge Linux kernels.
3. **Simple architecture**: Server is a relay, not a feature platform. No server-side message search, no rich-text rendering, no bot integrations. Fewer features means less code to maintain.
4. **E2E encryption**: Paradoxically simplifies the server. Since the server cannot read messages, there is no content moderation system, no spam filtering on message content, no search indexing. The server just moves encrypted blobs.

---

## 9. Erlang/BEAM Deep Dive

### Why Erlang Is Ideal for Connection Servers

The BEAM virtual machine (Erlang's runtime) was designed for telecom switches --- systems that must handle millions of concurrent connections with five-nines availability and zero downtime. A chat application has nearly identical requirements.

### Lightweight Processes

| Property | Erlang Process | OS Thread | Go Goroutine | Java Virtual Thread |
|----------|---------------|-----------|--------------|---------------------|
| Initial memory | ~2 KB | ~1-8 MB (stack) | ~4-8 KB | ~1 KB |
| Creation time | ~1-3 microseconds | ~50-100 microseconds | ~1 microsecond | ~1 microsecond |
| Max per node | Millions (tested to 10M+) | Thousands | Hundreds of thousands to low millions | Millions |
| Scheduling | Preemptive (reduction-based) | Preemptive (OS) | Cooperative (goroutine must yield) | Cooperative/platform-dependent |
| GC scope | Per-process (no global GC pause) | Shared heap (global GC pauses) | Shared heap (global GC pauses) | Shared heap (global GC pauses) |

**Per-process garbage collection** is Erlang's killer feature for connection servers. Each Erlang process has its own small heap. When that process's heap fills up, only that process is paused for GC --- every other process continues unaffected. This means:

- No global GC pauses. A server handling 2 million connections never pauses all 2 million processes for GC.
- GC pauses are microseconds (tiny heaps, ~2-5 KB), not milliseconds or seconds.
- For a chat server, this means consistent sub-millisecond message forwarding latency at the P99, regardless of server load.

In contrast, a JVM-based server with 2 million connections sharing a single heap could experience GC pauses of hundreds of milliseconds or more, causing noticeable message delivery stalls.

### Preemptive Scheduling

Erlang's scheduler is preemptive at the process level. Each process gets a budget of "reductions" (roughly one reduction per function call). After ~4000 reductions, the process is preempted and the scheduler runs the next process. This guarantees:

- **No single connection can starve others**. A connection processing a large message or doing expensive decoding is preempted after its reduction budget, allowing other connections to make progress.
- **Consistent latency**. Even under load, every connection gets regular CPU time.
- **No cooperative yielding needed**. Programmers do not need to insert explicit yield points (unlike Go goroutines, where a goroutine that does not call into the runtime can monopolize a thread).

### Let-It-Crash Philosophy

Traditional programming approaches defensive coding: check every error, handle every edge case, recover from every failure. Erlang takes the opposite approach:

- **Let the process crash**. If a connection process encounters an unexpected state (malformed frame, protocol violation, resource exhaustion), it simply crashes.
- **The supervisor restarts it**. The supervisor tree detects the crash and starts a new process. For a WebSocket connection, this means the connection is dropped and the client reconnects (which it does anyway due to mobile network conditions).
- **Crash isolation**. A crashed process does not affect any other process. If one of 2 million connections crashes, the other 1,999,999 are unaffected.

This dramatically simplifies code. Instead of writing:

```
try {
    parseFrame(data);
} catch (MalformedFrameException e) {
    log.warn("Malformed frame from user {}", userId);
    try { sendErrorFrame(conn, e); } catch (IOException io) { ... }
    try { closeConnection(conn); } catch (IOException io) { ... }
    try { cleanupRegistry(userId); } catch (RegistryException re) { ... }
    try { updatePresence(userId, OFFLINE); } catch (...) { ... }
}
```

The Erlang approach is:

```erlang
%% Just parse it. If it fails, the process crashes.
%% The supervisor handles cleanup.
Frame = parse_frame(Data),
handle_frame(Frame, State).
```

The supervisor tree handles cleanup: removing the crashed process from the registry, updating presence, and so on. This is not just simpler --- it is more reliable, because the cleanup logic is centralized in the supervisor, not scattered across every error path.

### Hot Code Upgrades

Erlang supports replacing code in a running system without stopping it. This is critical for a chat server:

- Deploy a bug fix to the WebSocket frame parser.
- The new code module is loaded into the BEAM VM.
- Existing processes begin using the new code on their next function call.
- No connections are dropped. No downtime. No reconnection storm.

In practice, WhatsApp used hot code upgrades extensively for minor patches and configuration changes. Major upgrades still used rolling restarts (drain connections from one server, restart it, let connections migrate back).

### FreeBSD over Linux

WhatsApp chose FreeBSD over Linux for their gateway servers. The reasons are pragmatic:

1. **kqueue vs epoll**: FreeBSD's kqueue is the native event notification mechanism. At the time of WhatsApp's early scaling (2010-2013), kqueue was considered more mature and efficient than Linux's epoll for very high connection counts (millions of file descriptors). Both are capable today, but FreeBSD had the edge in this era.

2. **Network stack**: FreeBSD's network stack was well-tuned for high-concurrency TCP workloads out of the box. Fewer kernel parameters to tune for millions of concurrent connections.

3. **Stability**: FreeBSD's conservative release cycle meant fewer kernel regressions. For a 24/7 chat server, stability is more valuable than the latest features.

4. **Jails**: FreeBSD jails (lightweight containers, predating Docker by a decade) provided process isolation without the overhead of full virtualization.

This is an unusual choice --- most internet companies use Linux. But WhatsApp's team (including Rick Reed) had deep FreeBSD expertise, and the combination of FreeBSD + Erlang was proven in their specific workload.

---

## 10. Contrast: Slack and Discord

### Slack: Gateway Servers + Channel Servers

Slack's real-time architecture uses a different model reflecting its workspace-centric design:

```
Slack Architecture (simplified):

  +----------+     +------------------+     +------------------+
  | Client   |---->| Gateway Server   |---->| Channel Server   |
  | (browser,|<----| (WebSocket)      |<----| (pub/sub per     |
  |  app)    |     |                  |     |  channel)        |
  +----------+     +------------------+     +------------------+
                                                    |
                                            +-------+-------+
                                            |               |
                                     +------+----+   +------+----+
                                     | Message   |   | Presence  |
                                     | Service   |   | Service   |
                                     +-----------+   +-----------+
```

**Key differences from WhatsApp**:

| Aspect | WhatsApp | Slack |
|--------|----------|-------|
| Connection model | Gateway routes directly to recipient's gateway | Gateway connects to channel servers (pub/sub) |
| Routing unit | User (1:1) or group | Channel (workspace-centric) |
| Message persistence | Transient relay (delete after delivery) | Permanent storage (searchable, compliance) |
| E2E encryption | Yes (server cannot read messages) | No (server stores plaintext for search, compliance) |
| Scale | 2B+ users, consumer | Millions of workspace users, enterprise |
| Connection density | 2M+ per server (Erlang) | Lower density (Go/Java, heavier per-connection state) |
| Presence model | Lazy (only active chat viewers) | Workspace-wide (show all channel members' status) |

**Slack's Channel Server**: Each Slack channel has an associated server process (or set of processes) that manages subscriptions. When a message is posted to #general, the channel server knows all members subscribed to #general and fans out the message. This is a classic pub/sub model.

WhatsApp does not have this concept because WhatsApp does not have channels or workspaces. Every conversation is either 1:1 (two participants, no fan-out) or a group (up to 1024 members, fan-out bounded).

### Discord: Elixir (BEAM) + Guild Processes

Discord chose Elixir, which runs on the same BEAM VM as Erlang. This gives Discord the same concurrency advantages (lightweight processes, preemptive scheduling, per-process GC).

```
Discord Architecture (simplified):

  +----------+     +------------------+     +------------------+
  | Client   |---->| Gateway Server   |---->| Guild Process    |
  | (browser,|<----| (WebSocket,      |<----| (one per server/ |
  |  app)    |     |  Elixir/BEAM)    |     |  guild, Elixir)  |
  +----------+     +------------------+     +------------------+
                                                    |
                                            +-------+-------+
                                            |               |
                                     +------+----+   +------+----+
                                     | Message   |   | Voice     |
                                     | Service   |   | Server    |
                                     | (Cassandra|   | (UDP/     |
                                     |  + Scylla)|   |  WebRTC)  |
                                     +-----------+   +-----------+
```

**Key differences from WhatsApp**:

| Aspect | WhatsApp | Discord |
|--------|----------|---------|
| Runtime | Erlang/BEAM | Elixir/BEAM (same VM) |
| Unit of fan-out | Group (max 1024) | Server/guild (millions of members possible) |
| Fan-out strategy | Fan-out on write | Fan-out on read (for large servers) |
| Voice/video | Separate WebRTC calls | Persistent voice channels (always-on UDP rooms) |
| Encryption | E2E by default | No E2E (server stores plaintext) |
| Storage | Transient relay | Permanent (Cassandra/ScyllaDB) |
| Presence | Online/offline/last-seen | Rich presence (playing game X, listening to Y) |

**Discord's Guild Process**: Each Discord server (guild) is managed by an Elixir process. This process handles:
- Maintaining the member list and their online/offline state.
- Fan-out of messages to online members in a channel.
- Tracking voice channel participants.
- Role and permission management.

For large guilds (millions of members), Discord shards the guild process across multiple nodes. This is analogous to how WhatsApp would shard a hypothetical 1M-member group --- except WhatsApp avoids this problem entirely by capping groups at 1024 members.

**Voice channels** are Discord's unique infrastructure challenge. A voice channel is a persistent UDP session --- participants send and receive audio packets in real-time. This requires:
- Dedicated voice servers (media relay nodes, not connection gateways).
- Low-latency UDP routing (not TCP/WebSocket).
- Selective forwarding unit (SFU) architecture --- the server receives audio from all participants and selectively forwards to each listener, mixing or prioritizing as needed.

WhatsApp's voice/video calls are peer-to-peer (WebRTC with STUN/TURN for NAT traversal), so the server only handles call signaling, not media relay. Discord's persistent voice rooms require always-on server-side media processing, which is a fundamentally different infrastructure challenge.

### Summary Comparison Table

| Dimension | WhatsApp | Slack | Discord |
|-----------|----------|-------|---------|
| Primary runtime | Erlang/BEAM | Go, Java | Elixir/BEAM |
| Connection protocol | Custom (XMPP-derived) over TCP; modeled as WebSocket | WebSocket + Events API (REST) | WebSocket + UDP (voice) |
| Connections/server | 2M-2.8M (Erlang) | ~100K-200K [INFERRED] | ~1M+ (Elixir/BEAM) [INFERRED] |
| L4 vs L7 LB | L4 (TCP pass-through) | L7 (Envoy/similar for HTTP routing) | L4 for gateway, L7 for REST APIs [INFERRED] |
| Presence model | Lazy (subscribers only) | Workspace-wide | Guild-wide + rich status |
| E2E encryption | Yes (Signal Protocol) | No | No |
| Message storage on server | Transient (deleted after delivery) | Permanent (searchable) | Permanent (Cassandra/ScyllaDB) |
| Max group/channel size | 1024 | Workspace-wide channels (1000s) | Millions per server/guild |
| Fan-out strategy | Write-time (bounded by 1024) | Channel pub/sub | Read-time for large guilds |
| Team size at scale | ~50 engineers for 900M+ users | Hundreds of engineers | Hundreds of engineers |
| OS choice | FreeBSD | Linux | Linux |

---

## Key Takeaways for the Interview

1. **Connection management IS the architecture**. In a chat application, the gateway layer that manages millions of persistent connections is the most critical and hardest-to-scale component. Everything else (storage, routing, encryption) is secondary to keeping those connections alive, healthy, and routable.

2. **L4, not L7**. Long-lived WebSocket connections must bypass application-layer load balancers. This is a concrete detail that demonstrates systems understanding.

3. **Presence is more expensive than messaging (if done naively)**. The quantitative analysis showing 500 billion presence notifications vs 100 billion messages is the kind of back-of-envelope math that distinguishes L6 from L5. The solution (lazy presence with 99% reduction) flows naturally from the math.

4. **Erlang/BEAM is not a niche choice --- it is the optimal technology for this exact problem**. Lightweight processes, per-process GC, preemptive scheduling, let-it-crash, and hot code upgrades are all directly relevant to managing millions of concurrent connections. This is why both WhatsApp (Erlang) and Discord (Elixir) chose the BEAM platform.

5. **Reconnection is the normal case on mobile**. The reconnection protocol (sync with sequence numbers, duplicate detection, gap detection) and thundering herd mitigation (exponential backoff with full jitter) are not edge cases --- they are the primary code path for mobile clients.

6. **Numbers matter**. Know the key numbers: 2B users, 100B messages/day, 2M+ connections/server, 32-50 engineers. These ground the discussion in reality and demonstrate that you have studied the system, not just theorized about it.
