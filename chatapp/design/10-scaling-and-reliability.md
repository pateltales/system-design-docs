# Scaling & Reliability — WhatsApp-Scale Chat Application

Cross-cutting document that ties together scaling decisions from all deep dives. This is about operating at the scale of billions of users and hundreds of billions of messages per day — and doing it with a surprisingly small team.

---

## Table of Contents

1. [Scale Numbers](#1-scale-numbers)
2. [Erlang / BEAM VM — Why It Matters](#2-erlang--beam-vm--why-it-matters)
3. [Horizontal Scaling Strategy](#3-horizontal-scaling-strategy)
4. [Availability and Fault Tolerance](#4-availability-and-fault-tolerance)
5. [Rate Limiting](#5-rate-limiting)
6. [Monitoring and Alerting](#6-monitoring-and-alerting)
7. [Back-of-Envelope Math](#7-back-of-envelope-math)
8. [Chaos Testing](#8-chaos-testing)
9. [Thundering Herd Mitigation](#9-thundering-herd-mitigation)
10. [Active-Active Multi-DC](#10-active-active-multi-dc)
11. [Contrast with Discord](#11-contrast-with-discord)
12. [Contrast with Slack](#12-contrast-with-slack)

---

## 1. Scale Numbers

WhatsApp operates at a scale that is difficult to overstate. The numbers below define the engineering constraints that drive every architectural decision.

| Metric | Value | Notes |
|--------|-------|-------|
| Registered users | 2B+ | Phone-number-based identity, global |
| Messages per day | ~100B | ~1.15M msg/sec average |
| Peak message rate | ~3.5-5.75M msg/sec | 3-5x average during peak hours (New Year's Eve, regional holidays) |
| Concurrent WebSocket connections | 50-100M | At peak, across all gateway servers |
| Media items shared per day | ~6.5B | Images, videos, audio messages, documents |
| Group size limit | 1,024 members | Fan-out on write is bounded by this |
| Multi-device | 1 phone + 4 companion devices | Each device maintains its own WebSocket connection |
| Engineering team (2014) | ~32 engineers for 450M users | Rick Reed's talk at Erlang Factory 2014 |
| Engineering team (2015) | ~50 engineers for 900M users | Widely reported by Wired, Sept 2015 |

**Why these numbers matter for design**: At 1.15M msg/sec average, you cannot rely on a single write path. At 50-100M concurrent connections, connection management is itself a distributed systems problem. At 6.5B media items/day, media storage dwarfs message storage by orders of magnitude. And the lean team constraint means the architecture must be operationally simple — you cannot afford a system that requires 500 engineers to babysit.

---

## 2. Erlang / BEAM VM — Why It Matters

WhatsApp's original technology stack was Erlang running on FreeBSD. This was not an arbitrary choice — it was the single most consequential architectural decision WhatsApp made.

### The Actor Model

Erlang's concurrency model is based on lightweight **processes** (not OS threads, not goroutines — Erlang processes):

| Property | Erlang Process | OS Thread | Go Goroutine |
|----------|---------------|-----------|--------------|
| Memory footprint | ~2 KB initial | ~1 MB (stack) | ~2-8 KB initial |
| Max per node | Millions (tested to 268M) | Thousands | Hundreds of thousands |
| Scheduling | Preemptive (per-reduction) | Preemptive (OS) | Cooperative (runtime) |
| Communication | Message-passing (mailbox) | Shared memory + locks | Channels (CSP) |
| Failure isolation | Process crash is isolated | Thread crash can corrupt process | Goroutine panic can crash process |

Each WebSocket connection is handled by its own Erlang process. A server with 2-3M connections has 2-3M Erlang processes — this is normal and expected on the BEAM VM.

### Preemptive Scheduling

The BEAM VM uses **reduction-based preemptive scheduling**. Every Erlang process gets a budget of ~4,000 reductions (roughly function calls). When the budget is exhausted, the scheduler preempts it and runs the next process. This means:

- No single connection can starve others (unlike cooperative scheduling where a busy goroutine can hog the thread).
- Latency is bounded — even under load, every connection gets fair CPU time.
- This is critical for a chat app where you have millions of connections and each one expects sub-100ms message delivery.

### Let-It-Crash and Supervisor Trees

Erlang's error handling philosophy is fundamentally different from Java/Go:

- **Let it crash**: Instead of defensive programming (try/catch everywhere), Erlang processes are designed to crash on unexpected errors. The crash is isolated to that single process (one connection), not the entire server.
- **Supervisor trees**: Supervisors monitor worker processes and restart them according to a strategy (one-for-one, one-for-all, rest-for-one). A crashed connection process is restarted in milliseconds — the client reconnects and resumes.
- **Result**: The system self-heals. A bug affecting one connection does not cascade. This is ideal for mobile clients with unreliable networks — connections die all the time, and the server must handle that gracefully.

### Hot Code Upgrades

The BEAM VM supports **hot code loading** — you can deploy new code to a running node without dropping connections. The old code continues to serve existing processes, new processes use the new code. Two versions can coexist simultaneously.

For WhatsApp, this means **zero-downtime deployments**. You do not need rolling restarts that force millions of clients to reconnect. [INFERRED — WhatsApp's actual deployment strategy is not publicly documented in detail, but hot code loading is a well-known BEAM capability and Rick Reed's talks reference it.]

### Rick Reed's Numbers

Rick Reed (WhatsApp engineer) presented at Erlang Factory SF 2014:

- **2 million connections per server** was the initial milestone.
- Later pushed to **~2.8 million connections per server** through BEAM VM tuning and FreeBSD kernel optimizations.
- WhatsApp ran a **patched version of the BEAM VM** on FreeBSD (not Linux). They contributed patches upstream and tuned the FreeBSD kernel for their workload — specifically network stack tuning for handling millions of concurrent TCP connections.

[Note: The 2M number is from Rick Reed's 2012 blog post "1 million is so 2011." The 2.8M number is from his 2014 Erlang Factory talk.]

### Why Not Java/Go?

| Concern | Erlang/BEAM | Java | Go |
|---------|-------------|------|----|
| Memory per connection | ~2 KB process | ~1 MB thread (or NIO event loop) | ~2-8 KB goroutine |
| 2M connections memory | ~4 GB | ~2 TB (threads) or ~4 GB (NIO) | ~4-16 GB |
| Fault isolation | Process-level | Thread-level (shared heap) | Goroutine-level (shared heap) |
| Hot upgrades | Built-in | Not native (rolling restart) | Not native (rolling restart) |
| Operational simplicity | High (OTP patterns) | Medium (needs frameworks) | Medium (simpler than Java) |

Java with NIO (Netty) or Go could achieve similar connection counts, but you would need to build your own supervisor trees, your own process isolation, and your own hot upgrade mechanism. Erlang gives you these out of the box via OTP (Open Telecom Platform). For a team of 32-50 engineers, this operational leverage is the difference between feasible and impossible.

---

## 3. Horizontal Scaling Strategy

Every layer of the architecture must scale independently. The key insight is separating **stateful** components (that hold connections) from **stateless** components (that process messages).

### 3.1 Gateway Servers (Stateful)

Gateway servers are the only stateful component — they hold WebSocket connections.

```
                    ┌──────────────┐
                    │   L4 Load    │
                    │   Balancer   │
                    └──────┬───────┘
                           │
            ┌──────────────┼──────────────┐
            ▼              ▼              ▼
     ┌─────────────┐ ┌─────────────┐ ┌─────────────┐
     │  Gateway 1  │ │  Gateway 2  │ │  Gateway N  │
     │  500K conn  │ │  500K conn  │ │  500K conn  │
     └─────────────┘ └─────────────┘ └─────────────┘
```

| Parameter | Value | Rationale |
|-----------|-------|-----------|
| Connections per server | 500K-2.8M | Depends on message volume per connection and server hardware |
| Total gateway servers (100M connections) | 36-200 | 100M / 2.8M = ~36 (aggressive) to 100M / 500K = 200 (conservative) |
| Load balancer type | L4 (TCP) | L7 adds per-message overhead on long-lived WebSocket connections |
| Connection routing | Consistent hashing or least-connections | New connections go to least-loaded server |

**Scaling**: Add more gateway servers. Update the connection registry so the routing layer knows about the new servers. Clients connecting (or reconnecting) will be assigned to new servers by the load balancer.

**State**: The only state on a gateway server is the set of open connections. This state is **reconstructable** — if a gateway dies, clients reconnect to other gateways and re-establish their sessions. No persistent state is lost.

### 3.2 Message Routing (Stateless)

The routing layer sits between gateway servers and the message store. When a message arrives:

1. Look up recipient's gateway server in the connection registry (Redis).
2. Forward message to that gateway server.
3. If recipient is offline (not in registry), write to the offline message queue.

```
Gateway A ──► Routing Layer ──► Connection Registry (Redis)
                   │                    │
                   │              userId → gatewayId
                   │
                   ├──► Gateway B (recipient online)
                   │
                   └──► Offline Queue (recipient offline)
```

**Scaling**: Routing servers are stateless — any routing server can handle any message. Scale by adding more instances behind a load balancer. There is no coordination between routing instances.

### 3.3 Message Store (Partitioned)

Messages are stored in Cassandra (or equivalent wide-column store), partitioned by `conversationId`.

| Aspect | Design |
|--------|--------|
| Partition key | `conversationId` |
| Clustering key | `sequenceNumber` (ascending) |
| Replication factor | 3 |
| Consistency level | Quorum (W=2, R=2 for RF=3) |
| Compaction strategy | Leveled (read-optimized, since messages are written once and read/deleted) |

**Scaling**: Add more Cassandra nodes. The ring automatically rebalances token ranges. Conversational data is co-located — all messages in a conversation live on the same partition, enabling efficient range queries for message history.

**Why Cassandra?**
- Write-optimized (LSM-tree): handles 1.15M msg/sec writes.
- Partitioned: conversationId-based partitioning distributes load evenly (billions of conversations).
- Tunable consistency: quorum writes for durability, quorum reads for consistency.
- No single master: any node can accept writes for any partition (no write bottleneck).
- Linear scalability: double nodes, double throughput.

### 3.4 Media Servers (Stateless)

Media servers handle upload and download of encrypted media blobs.

```
Client ──► Media Server ──► Object Storage (S3 / equivalent)
              │
              └──► CDN (for download)
```

**Scaling**: Media servers are stateless request handlers. Scale by adding more instances. The heavy lifting is done by the object storage layer (S3) and CDN, both of which scale independently and elastically.

---

## 4. Availability and Fault Tolerance

### 4.1 Gateway Server Failure

**Impact**: All connections on that server are lost. Clients experience a disconnect.

**Recovery**:
1. Client detects disconnect (missed heartbeat or TCP RST).
2. Client reconnects to a different gateway server (load balancer routes to healthy server).
3. Client sends last-known sequence number per conversation.
4. Server delivers all messages since that sequence number from the offline queue.
5. No message loss — messages were already persisted in the message store or offline queue before being forwarded to the gateway.

**Key insight**: The gateway server is ephemeral. It does not hold any data that is not also stored elsewhere. The offline queue is the safety net.

### 4.2 Message Store Failure

**Impact**: Depends on the failure mode.

| Failure | Impact | Recovery |
|---------|--------|----------|
| Single node failure (RF=3) | No data loss, no downtime | Cassandra hinted handoff, read repair, anti-entropy repair |
| Quorum unavailable (2 of 3 nodes down for a partition) | Reads/writes to that partition fail | Client retries; operator repairs cluster; data still on surviving node |
| Full DC failure | See DC failover below | Cross-DC replication ensures data survives |

**Quorum reads/writes (W=2, R=2 for RF=3)**:
- A write succeeds when 2 of 3 replicas acknowledge.
- A read succeeds when 2 of 3 replicas respond (and the most recent version is returned).
- This tolerates 1 node failure with no data loss and no downtime.

### 4.3 Connection Registry (Redis) Failure

**Impact**: Routing layer cannot determine which gateway server a user is connected to. Messages cannot be delivered in real-time.

**Recovery**:
- Redis is deployed as a cluster with replication (Redis Cluster or Redis Sentinel).
- If a Redis master fails, a replica is promoted automatically.
- During the failover window (seconds), messages are written to the offline queue as a fallback.
- When the registry recovers, gateway servers re-register their connections.

### 4.4 Data Center Failover

See [Section 10: Active-Active Multi-DC](#10-active-active-multi-dc) for the full treatment.

**Summary**: Active-active across 2+ DCs. If one DC goes down:
1. DNS or load balancer detects failure and reroutes traffic.
2. Clients reconnect to the surviving DC.
3. The surviving DC absorbs the additional load (requires ~33% headroom).
4. Cassandra cross-DC replication ensures messages written to the failed DC are still readable from the surviving DC.

---

## 5. Rate Limiting

Rate limiting protects the system from abuse and prevents runaway clients from causing cascading failures.

### Per-User Rate Limits

| Action | Rate Limit | Rationale |
|--------|-----------|-----------|
| Message send (1:1) | ~60 messages/minute | Normal human typing speed is ~30-40 messages/minute in rapid conversation |
| Message send (group, per user) | ~30 messages/minute per group | Prevents one user from flooding a group |
| Group message fan-out (total) | Bounded by group size x rate | 1024 members x 30 msg/min = 30K fan-out writes/min per group |
| Connection attempts | ~5 per minute per IP | Prevents reconnection storms and credential stuffing |
| Media uploads | ~20 per minute | Prevents storage abuse |
| Media upload size | 16 MB (images), 2 GB (documents) [UNVERIFIED — check official sources] | WhatsApp has documented size limits, but exact values vary by media type |
| Status posts | ~30 per day [UNVERIFIED] | Prevents status spam |

### Implementation

Rate limiting is implemented at the gateway layer (per-connection) and the routing layer (per-user across connections):

```
Client ──► Gateway (per-connection rate limit)
               │
               ▼
         Routing Layer (per-user rate limit via distributed counter)
               │
               ▼
         Message Store
```

- **Gateway-level**: Simple token bucket per connection. Cheap, no external state.
- **Routing-level**: Distributed rate limiting using Redis (INCR with TTL). Handles the case where a user has multiple connections (multi-device).
- **Response to rate-limited requests**: Return an error code with a retry-after header. The client backs off. Do NOT silently drop messages — the sender must know delivery failed.

### Group-Specific Rate Limits

Groups introduce amplification: one message becomes up to 1,024 deliveries. Without group-level rate limiting, a few active groups could dominate the message pipeline.

- **Per-group message rate**: Cap total messages per group per minute (e.g., 100 messages/minute regardless of sender). [INFERRED]
- **Per-user-per-group rate**: Cap how many messages a single user can send to a group per minute.
- **Fan-out budget**: The routing layer tracks the total fan-out budget. If the system is under pressure, large-group fan-outs can be deprioritized in favor of 1:1 messages (which are higher-priority for user experience).

---

## 6. Monitoring and Alerting

You cannot operate a system at this scale without comprehensive observability. The monitoring system must answer: **Are messages being delivered? How fast? What's broken?**

### Key Metrics

| Metric | Granularity | What It Tells You |
|--------|-------------|-------------------|
| **Message delivery latency** (P50/P95/P99) | Per-DC, per-region | Core user experience. P50 < 100ms, P95 < 300ms, P99 < 1s for online recipients |
| **End-to-end delivery latency** | Sender send → recipient ACK | Includes gateway hops, routing, and delivery. The "real" latency number |
| **WebSocket connection count** | Per gateway server, total | Capacity planning. Alert if total drops suddenly (gateway failure) |
| **Connection churn rate** | Per minute | Elevated churn indicates network issues or client bugs |
| **Offline queue depth** | Per user, total | Growing queue = delivery failures. Alert if total depth exceeds threshold |
| **Offline queue age** | Oldest undelivered message | If messages are sitting undelivered for hours, something is wrong |
| **Media upload latency** (P50/P95/P99) | Per-DC | Slow uploads degrade user experience |
| **Media download latency** | Per-DC, per CDN POP | CDN cache miss rates |
| **Message store write latency** | Per Cassandra node | Cassandra compaction storms, hardware degradation |
| **Message store read latency** | Per Cassandra node | Hot partitions, slow replicas |
| **Error rates** | Per API, per error code | Spikes in 5xx = server-side failures |
| **Push notification delivery rate** | APNs/FCM success rate | Platform push service issues |
| **Redis (connection registry) latency** | P50/P95/P99 | Registry slowness blocks message routing |

### Alert Thresholds

| Alert | Threshold | Severity | Action |
|-------|-----------|----------|--------|
| P99 delivery latency > 2s | Sustained for 5 minutes | P1 (Critical) | Page on-call. Check gateway health, routing layer, message store |
| Connection count drop > 10% in 1 minute | Immediate | P1 | Gateway server crash or network partition |
| Offline queue total depth > 100M | Sustained for 10 minutes | P2 (High) | Delivery pipeline backed up. Check routing and gateway health |
| Offline queue oldest message > 1 hour | Any occurrence | P2 | Specific users not reconnecting, or delivery bug |
| Message store write latency P99 > 500ms | Sustained for 5 minutes | P2 | Cassandra compaction, disk pressure, or node failure |
| Error rate > 1% on message send | Sustained for 2 minutes | P1 | Something in the write path is broken |
| Push notification failure rate > 5% | Sustained for 10 minutes | P3 (Medium) | APNs/FCM issues (often external) |

### Dashboards

Three primary dashboards:

1. **Message Health**: Delivery latency distributions, send/receive rates, error rates, delivery success rate.
2. **Connection Health**: Total connections, connection churn, per-gateway distribution, reconnection rates.
3. **Infrastructure Health**: Cassandra latency/throughput, Redis latency, media storage, CPU/memory/network per server.

---

## 7. Back-of-Envelope Math

Detailed calculations that an L6 candidate should be able to perform during an interview.

### 7.1 Messages Per Second

```
Messages per day:          100,000,000,000  (100 billion)
Seconds per day:                    86,400
Average msg/sec:           100B / 86,400 ≈ 1,157,407 ≈ 1.15M msg/sec

Peak multiplier:           3-5x average
Peak msg/sec:              3.5M - 5.75M msg/sec
```

### 7.2 Write Throughput (Message Store)

```
Average message size:      ~1 KB (encrypted payload + metadata)
Average write throughput:  1.15M msg/sec × 1 KB = 1.15 GB/sec

Peak write throughput:     5.75M msg/sec × 1 KB = 5.75 GB/sec

With replication (RF=3):   5.75 GB/sec × 3 = 17.25 GB/sec total disk write
```

For Cassandra: each node can sustain ~20-50 MB/sec write throughput depending on hardware. At 17.25 GB/sec total:

```
Nodes needed (write-only): 17.25 GB/sec / 30 MB/sec per node ≈ 575 Cassandra nodes
```

This is a large cluster but well within Cassandra's operational range (clusters of 1000+ nodes are common at major tech companies).

### 7.3 Connection Registry Size

```
Concurrent connections:    100,000,000 (100M)
State per connection:      ~100 bytes (userId: 16B, gatewayId: 16B, connectionId: 16B,
                           timestamp: 8B, metadata: ~44B)
Total registry size:       100M × 100 bytes = 10 GB

Redis memory (with overhead): ~10 GB × 2 (Redis overhead) = ~20 GB
```

A single Redis node can hold ~20 GB in memory. However, for availability and throughput, you would deploy a Redis cluster with sharding:

```
Lookups per second:        ≈ msg/sec = 1.15M/sec (each message requires a registry lookup)
Peak lookups:              5.75M/sec

Redis throughput:          ~100K-200K ops/sec per node (single thread)
Redis nodes needed:        5.75M / 150K ≈ 38 Redis shards (for throughput)
```

### 7.4 Media Throughput

```
Media items per day:       6,500,000,000  (6.5 billion)
Average media size:        ~200 KB (weighted average: images ~200 KB, videos larger but less frequent)
Total media per day:       6.5B × 200 KB = 1.3 PB/day

Media per second:          6.5B / 86,400 ≈ 75,231 media/sec
Throughput:                75K × 200 KB = 15 GB/sec sustained upload/download

Peak (3-5x):              45-75 GB/sec
```

This is the primary reason for CDN and object storage — no custom server fleet can economically handle 1.3 PB/day. Object storage (S3-like) with CDN caching for popular media is the only viable approach.

### 7.5 Gateway Server Count

```
Concurrent connections:    100M
Connections per server:    500K (conservative) to 2.8M (WhatsApp's reported peak with Erlang)

Conservative:              100M / 500K = 200 gateway servers
Aggressive (Erlang):       100M / 2.8M = ~36 gateway servers
Practical (with headroom): ~75-150 gateway servers
```

### 7.6 Summary Table

| Resource | Calculation | Result |
|----------|------------|--------|
| Messages/sec (avg) | 100B / 86,400 | 1.15M msg/sec |
| Messages/sec (peak) | 1.15M x 5 | 5.75M msg/sec |
| Write throughput (peak, replicated) | 5.75M x 1KB x 3 | 17.25 GB/sec |
| Cassandra nodes (write path) | 17.25 GB/sec / 30 MB/node | ~575 nodes |
| Connection registry size | 100M x 100B | 10 GB |
| Redis shards (throughput) | 5.75M / 150K | ~38 shards |
| Media throughput (avg) | 6.5B x 200KB / 86,400 | 15 GB/sec |
| Media storage per day | 6.5B x 200KB | 1.3 PB/day |
| Gateway servers | 100M / 500K-2.8M | 36-200 servers |

---

## 8. Chaos Testing

Inspired by Netflix's Chaos Monkey, chaos testing verifies that the system's fault tolerance actually works — not just in theory, but in production.

### Philosophy

> "Everything fails all the time." — Werner Vogels

At WhatsApp's scale, hardware failures are not exceptional events — they are **routine**. With hundreds of servers, you will lose a server every day. The system must handle this without human intervention and without message loss.

### Chaos Experiments

| Experiment | What You Kill | Expected Behavior | Verification |
|-----------|---------------|-------------------|--------------|
| **Kill gateway server** | Terminate a random gateway process | Clients reconnect to other gateways. Messages in-flight are retried. Offline queue delivers pending messages on reconnect. | Zero message loss. Reconnection time < 30s. No P99 latency spike lasting > 2 minutes. |
| **Kill Cassandra node** | Stop a Cassandra node in the ring | Quorum reads/writes continue on remaining 2 replicas. Hinted handoff queues writes for the dead node. | No failed writes. Read latency may increase slightly. Repair after node returns. |
| **Kill Redis node** | Stop a Redis master in the cluster | Sentinel promotes replica to master. During failover (~5s), routing falls back to offline queue. | Messages are queued, not lost. Delivery latency spike of ~5-10s for affected users. |
| **Simulate DC failure** | Block all traffic to/from one DC | DNS/LB detects failure. Clients reconnect to surviving DC. Surviving DC absorbs 2x load. | Message delivery continues. No data loss (cross-DC replication). Failover time < 60s. |
| **Network partition between DCs** | Drop inter-DC traffic | Each DC operates independently. Cross-DC replication queues up. On partition heal, replication catches up. | No data loss. Possible message ordering anomalies (resolved by sequence numbers). |
| **Slow Cassandra node** | Inject 500ms latency on one node | Speculative retry hits another replica. Slow node is eventually marked as down by the failure detector. | No user-visible impact if speculative retry is configured. |
| **Media storage failure** | Block access to S3/blob store | Media uploads fail. Text messages continue normally. Client retries media uploads with backoff. | Text message delivery unaffected. Media uploads resume when storage recovers. |

### Runbook for Chaos Tests

1. **Pre-condition**: All monitoring dashboards green. No ongoing incidents.
2. **Execute**: Run chaos experiment in one DC during low-traffic window.
3. **Observe**: Watch message delivery latency, connection counts, error rates.
4. **Verify**: After experiment, confirm zero message loss by checking delivery receipts.
5. **Expand**: Gradually run experiments during higher-traffic windows. Eventually run in production during peak hours (the real test).

### Key Invariant

The most important invariant to verify across all chaos experiments:

**No message loss.** A message that the sender has received a server ACK for must eventually be delivered to the recipient (or all recipients, for group messages), even if every server in the delivery path fails at some point during the process.

---

## 9. Thundering Herd Mitigation

When a gateway server dies or a DC recovers from a failure, millions of clients attempt to reconnect simultaneously. This is a **thundering herd** — and without mitigation, the reconnection storm can be worse than the original failure.

### The Problem

```
Gateway server dies
   → 500K clients disconnect
   → 500K clients immediately try to reconnect
   → Load balancer sends 500K SYN packets to remaining gateways
   → Remaining gateways are overwhelmed
   → More gateways fail (cascading failure)
```

### Mitigation 1: Exponential Backoff with Jitter

Every client implements **exponential backoff with full jitter** on reconnection:

```
base_delay = 1 second
max_delay  = 60 seconds
attempt    = 0, 1, 2, 3, ...

delay = random(0, min(max_delay, base_delay * 2^attempt))
```

**Why full jitter (not just exponential backoff)?**

Without jitter, all 500K clients would retry at exactly the same intervals (1s, 2s, 4s, 8s...), creating periodic spikes. Full jitter randomizes the retry time within the window, spreading the load evenly.

| Attempt | Backoff Window | Average Delay |
|---------|---------------|---------------|
| 0 | 0-1s | 0.5s |
| 1 | 0-2s | 1.0s |
| 2 | 0-4s | 2.0s |
| 3 | 0-8s | 4.0s |
| 4 | 0-16s | 8.0s |
| 5 | 0-32s | 16.0s |
| 6+ | 0-60s | 30.0s |

With 500K clients, after the first attempt, reconnections are spread over a 1-second window (500K/s). After the second attempt, spread over 2 seconds (250K/s). After 5 attempts, spread over 32 seconds (~15K/s). The thundering herd dissipates exponentially.

### Mitigation 2: Connection Rate Limiting (Server-Side)

The gateway layer implements a **connection admission rate limit**:

```
max_new_connections_per_second = 10,000 per gateway server
```

When the rate is exceeded:
- New TCP connections are accepted but the WebSocket handshake is delayed.
- The server sends a `503 Service Unavailable` with a `Retry-After` header.
- This provides **server-side backpressure** in addition to client-side backoff.

### Mitigation 3: Priority-Based Queue Drain

When a DC recovers from failure, the offline queue contains millions of pending messages. Draining all of them at once would overwhelm recovering infrastructure.

**Priority order for queue drain**:

1. **Recent messages** (last 5 minutes) — users are likely still waiting for these.
2. **1:1 messages** — higher perceived urgency than group messages.
3. **Group messages** — these have fan-out amplification, so they are drained more slowly.
4. **Old messages** (> 1 hour) — users have likely given up waiting; deliver at lower priority.

The drain rate is throttled and gradually increases as the infrastructure stabilizes:

```
Time after recovery:    Drain rate:
0-1 minutes            10% of normal throughput
1-5 minutes            30% of normal throughput
5-15 minutes           60% of normal throughput
15+ minutes            100% of normal throughput
```

---

## 10. Active-Active Multi-DC

### Why Not Active-Passive?

In an active-passive setup, the standby DC sits idle until a failover event. This is problematic for several reasons:

| Problem | Explanation |
|---------|-------------|
| **Standby rot** | An idle DC is an untested DC. Configuration drift, expired certificates, stale data, untested failover scripts — when you need it most, it does not work. |
| **Wasted capacity** | 50% of your infrastructure sits idle. At WhatsApp's scale, that is hundreds of millions of dollars in idle hardware. |
| **Slow failover** | Bringing up a cold standby takes minutes to hours: warm caches, rebuild connection registry, drain offline queues. Users experience extended downtime. |
| **No confidence** | You have never run production traffic on the standby. You do not know if it can actually handle the load. |

Active-passive is a lie — it gives the illusion of redundancy without the reality.

### Active-Active Design

Both (or all) DCs serve production traffic at all times.

```
           ┌────────────────────────────────────────────┐
           │            Global DNS / LB                  │
           │    (GeoDNS routes users to nearest DC)      │
           └──────────────┬─────────────────┬───────────┘
                          │                 │
                ┌─────────▼──────┐ ┌────────▼─────────┐
                │     DC-West    │ │     DC-East      │
                │                │ │                   │
                │  Gateways      │ │  Gateways         │
                │  Routing       │ │  Routing          │
                │  Cassandra     │◄──► Cassandra       │
                │  Redis         │ │  Redis            │
                │  Media Store   │ │  Media Store      │
                └────────────────┘ └───────────────────┘
                        │                    │
                        └────── Async ───────┘
                           Replication
```

### Cassandra Cross-DC Async Replication

Cassandra natively supports multi-datacenter replication:

- **NetworkTopologyStrategy**: Configure replication factor per DC (e.g., RF=3 in each DC).
- **Local writes**: Writes go to the local DC with `LOCAL_QUORUM` consistency (2 of 3 local replicas).
- **Async replication**: The write is asynchronously replicated to the remote DC. This adds ~50-100ms of replication lag (cross-DC network latency) but does not block the write.
- **Conflict resolution**: Cassandra uses last-write-wins (LWW) with timestamps. For chat messages, conflicts are rare — messages have unique IDs, and the same message is not written from both DCs.

### Client Failover

When a client's DC becomes unreachable:

1. Client detects disconnect (missed heartbeat).
2. Client attempts reconnection with exponential backoff + jitter.
3. DNS returns the IP of the surviving DC (GeoDNS TTL should be low, e.g., 30-60 seconds).
4. Client connects to the surviving DC.
5. Surviving DC has the client's data (replicated from the failed DC).
6. Offline queue delivers any messages that were pending.

### Capacity Planning: The ~33% Headroom Rule

Each DC must have enough headroom to absorb the other DC's traffic during a failover.

```
Normal operation:
  DC-West: 50% of global traffic
  DC-East: 50% of global traffic

During DC-East failure:
  DC-West: 100% of global traffic (2x normal load)

Required capacity per DC: At least 100% of global traffic
Normal utilization per DC: ≤ 50% of capacity
Headroom: 50% (100% capacity - 50% utilization)
```

In practice, with 3 DCs:

```
Normal operation:
  DC-A: 33% of traffic
  DC-B: 33% of traffic
  DC-C: 33% of traffic

During DC-A failure:
  DC-B: 50% of traffic
  DC-C: 50% of traffic

Required capacity per DC: At least 50% of global traffic
Normal utilization per DC: 33% of capacity
Headroom: ~33% (50% capacity needed - 33% utilization)
```

**The rule**: With N DCs, each DC operates at `1/N` of capacity and can absorb `1/(N-1)` of global traffic during failover. The headroom per DC is `1/N` of total capacity.

### Cross-DC Message Routing

When sender and recipient are connected to different DCs:

```
Sender (DC-West) → Gateway-West → Routing-West → [recipient not in DC-West registry]
                                       │
                                       ▼
                              Cross-DC message forward
                                       │
                                       ▼
                              Routing-East → Gateway-East → Recipient (DC-East)
```

The routing layer first checks the local connection registry. If the recipient is not in the local DC, it forwards the message to the remote DC's routing layer. This adds one cross-DC hop (~50-100ms) but avoids requiring global state synchronization.

---

## 11. Contrast with Discord

Discord serves a different use case (communities and gaming) but faces similar scaling challenges and made interesting technology choices.

### Scale Comparison

| Metric | WhatsApp | Discord |
|--------|----------|---------|
| Monthly active users | 2B+ | 150M+ |
| Messages per day | ~100B | Billions (exact number not published) |
| Concurrent connections | 50-100M | Millions (exact number not published) |
| Max group/server size | 1,024 members | 1M+ members per server |
| Core language | Erlang | Elixir (also BEAM VM) |
| Message persistence | Transient (deleted after delivery) | Permanent (all messages retained) |
| E2E encryption | Yes (all chats) | No |
| Voice/video | 1:1 and group calls (WebRTC) | Persistent voice channels (WebRTC + custom) |

### Technology Stack: Elixir on BEAM

Discord chose **Elixir** — a modern language that runs on the same BEAM VM as Erlang. This gives Discord the same advantages WhatsApp gets from Erlang:

- Lightweight processes for connection handling.
- Supervisor trees for fault tolerance.
- Hot code upgrades.
- Preemptive scheduling for fair connection handling.

Elixir adds modern language features (macros, better tooling, the Phoenix framework) while retaining full BEAM VM compatibility. [Discord has published blog posts about their Elixir usage.]

### Cassandra to ScyllaDB Migration

Discord famously **migrated from Cassandra to ScyllaDB** (a C++ rewrite of Cassandra that is API-compatible).

**Why they migrated** [Based on Discord's published engineering blog post, 2023]:
- Cassandra's JVM-based garbage collection caused latency spikes (GC pauses).
- At Discord's scale, these GC pauses caused cascading delays in message delivery.
- ScyllaDB, being written in C++, has no GC pauses and provides more predictable latency.
- ScyllaDB's shard-per-core architecture provides better utilization of modern hardware.

**Key difference from WhatsApp**: WhatsApp stores messages transiently (delete after delivery), so storage growth is bounded. Discord stores messages permanently, so they need a storage engine that handles unbounded growth efficiently. This makes the storage engine choice more critical for Discord.

### Rich Presence

Discord has a **rich presence** system that is far more complex than WhatsApp's online/offline status:

- What game a user is playing.
- How long they have been playing.
- Custom status messages.
- Streaming status (linked to Twitch, YouTube).
- Spotify listening activity.

This presence data is updated frequently and fanned out to all users who can see the person — a significant real-time data distribution challenge.

### Voice Channels

Discord's **persistent voice channels** are architecturally different from WhatsApp's voice calls:

- WhatsApp: Voice calls are ephemeral, 1:1 or small group, peer-to-peer via WebRTC (STUN/TURN for NAT traversal). The server handles only signaling.
- Discord: Voice channels are persistent rooms. Users join and leave freely. Audio is mixed server-side (Selective Forwarding Unit — SFU architecture). This requires dedicated media servers with significant bandwidth and CPU for audio routing.

---

## 12. Contrast with Slack

Slack serves enterprise/workplace communication — a fundamentally different use case that drives different architectural decisions.

### Scale Comparison

| Metric | WhatsApp | Slack |
|--------|----------|-------|
| Target user base | Consumer, global | Enterprise, workplace |
| Concurrent WebSocket sessions | 50-100M | 5M+ [UNVERIFIED — based on public reports] |
| Message persistence | Transient | Permanent (searchable, compliant) |
| E2E encryption | Yes (all chats) | No (enterprise compliance requires server-side access) |
| Identity model | Phone number | Email + workspace |
| Core storage | Cassandra (wide-column) | MySQL + Vitess (relational, sharded) |
| Sharding model | By conversationId | By workspace (team_id) |
| Group model | Flat group, max 1,024 | Channels (public/private), threads |
| Rich integrations | Minimal (Business API) | Extensive (apps, bots, workflows) |

### MySQL + Vitess Sharding

Slack uses **MySQL** as its primary data store, with **Vitess** for horizontal sharding.

**Why MySQL, not Cassandra?**
- Slack needs strong consistency (ACID) for enterprise features: message editing, threaded replies, reactions, search indexing, compliance exports.
- Slack's data model is relational: workspaces contain channels, channels contain messages, messages have threads, threads have replies. Foreign key relationships matter.
- Cassandra's eventual consistency and lack of relational features would make Slack's feature set harder to implement.

### Workspace-Based Sharding

Slack shards data by **workspace (team_id)**:

```
Workspace A → Shard 1 (MySQL instance)
Workspace B → Shard 2 (MySQL instance)
Workspace C → Shard 1 (MySQL instance)  // co-located with A
```

- All data for a workspace lives on one shard: channels, messages, users, files.
- This enables efficient queries within a workspace (joins across tables).
- Cross-workspace operations (Slack Connect, shared channels) require cross-shard queries — these are more expensive.

**Contrast with WhatsApp**: WhatsApp shards by conversationId (a single conversation is the unit of locality). Slack shards by workspace (an entire organization is the unit of locality). This reflects the product: WhatsApp conversations are independent, Slack workspaces are deeply interconnected (channels, threads, mentions, search).

### Why Slack Does Not Use E2E Encryption

Enterprise customers **require** server-side access to messages:

- **Compliance**: Legal holds, e-discovery, audit logs. Regulations like SOX, HIPAA, GDPR require that enterprises can produce communication records.
- **Admin controls**: Workspace admins need to moderate content, manage data retention policies, export data.
- **Search**: Full-text search across all messages requires server-side indexing. E2E encryption would make server-side search impossible.
- **Integrations**: Slack's bot and app ecosystem reads and processes messages. E2E encryption would break the entire integration model.

This is not a shortcoming — it is a deliberate design choice driven by the enterprise use case. WhatsApp optimizes for individual privacy; Slack optimizes for organizational visibility and compliance.

---

## Summary: Platform Comparison Table

| Dimension | WhatsApp | Discord | Slack |
|-----------|----------|---------|-------|
| **Scale** | 2B+ users, 100B msg/day | 150M+ MAU | 5M+ concurrent WS |
| **Runtime** | Erlang/BEAM on FreeBSD | Elixir/BEAM | Java, PHP (legacy), Go |
| **Message store** | Cassandra (transient) | ScyllaDB (permanent) | MySQL + Vitess (permanent) |
| **Sharding key** | conversationId | channel_id / guild_id | workspace (team_id) |
| **Fan-out** | Write (groups <= 1024) | Read (servers up to 1M+) | Write (channels, typically < 10K) |
| **E2E encryption** | Yes (Signal Protocol) | No | No |
| **Persistence** | Delete after delivery | Retain forever | Retain forever |
| **Presence** | Online/offline/last seen | Rich (game, music, streaming) | Online/away/DND |
| **Voice** | P2P calls (WebRTC) | Persistent channels (SFU) | Huddles (SFU) |
| **Team size (notable)** | 32-50 for 450M-900M users | Not publicly notable | Not publicly notable |
| **Key scaling challenge** | Connection handling at 2B scale | Persistent storage + voice at community scale | Search + compliance at enterprise scale |

---

## Key Takeaways for Interview

1. **The Erlang choice was not about language preference — it was about operational leverage.** A team of 32-50 engineers served hundreds of millions of users because the BEAM VM handles concurrency, fault tolerance, and hot upgrades out of the box.

2. **Separate stateful from stateless.** Gateway servers are stateful (connections), everything else is stateless. This makes scaling and failure recovery tractable.

3. **The offline queue is the safety net.** Every fault tolerance story ends with "messages are in the offline queue." The queue decouples message acceptance from message delivery.

4. **Active-active is not optional at this scale.** Active-passive is a ticking time bomb. If you have not tested failover, you do not have failover.

5. **Back-of-envelope math separates L5 from L6.** An L6 candidate can derive gateway server count, Cassandra cluster size, and Redis shard count from first principles. An L5 says "we'll use Cassandra" without knowing if it can handle the load.

6. **Thundering herd is a second-order failure.** The original failure (gateway crash) is survivable. The reconnection storm that follows is what actually kills you. Exponential backoff with jitter is not optional.

7. **Different products, different architectures.** WhatsApp, Discord, and Slack all handle real-time messaging, but their scaling challenges are fundamentally different because their products are different. Do not cargo-cult one architecture for a different problem.
