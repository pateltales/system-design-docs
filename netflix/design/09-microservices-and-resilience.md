# 9. Microservices Architecture & Resilience Engineering

## 1. Microservices Architecture

Netflix runs **1,000+ microservices** in production. Each microservice:

- **Owns its data** -- no shared databases across service boundaries
- **Exposes a well-defined API** -- contracts between teams, not shared memory
- **Deploys independently** -- a team ships without coordinating with 100 other teams
- **Scales independently** -- the recommendation service scales differently than the billing service

### Evolution from Monolith

```
2007 ──────────────────────────────────────────────── 2012+

 ┌─────────────────────┐        ┌───────┐ ┌───────┐ ┌───────┐
 │   DVD Rental App    │        │ User  │ │ Play  │ │ Reco  │
 │   (Monolith)        │        │ Svc   │ │ Svc   │ │ Svc   │
 │                     │  ───>  └───┬───┘ └───┬───┘ └───┬───┘
 │  Single Oracle DB   │            │         │         │
 │  Single WAR deploy  │        ┌───┴───┐ ┌───┴───┐ ┌───┴───┐
 └─────────────────────┘        │  DB   │ │  DB   │ │  DB   │
                                └───────┘ └───────┘ └───────┘
```

**Why they migrated:**
- 2008: major database corruption took the site down for 3 days
- Monolith meant a single bug could take down everything
- Couldn't scale individual components (streaming vs. DVD queues)
- Deploys required the entire team to coordinate

**How they migrated (7 years):**
1. Started with non-critical services (movie metadata, ratings)
2. Strangler Fig pattern -- new features as microservices, old monolith gradually hollowed out
3. Last monolith component decommissioned ~2012

---

## 2. Netflix OSS Stack

Netflix open-sourced a full platform stack. This is one of the most influential contributions to the microservices ecosystem.

### Architecture Overview

```
                         ┌─────────────────────────────────────────┐
                         │              Internet / CDN             │
                         └────────────────────┬────────────────────┘
                                              │
                                              v
                    ┌─────────────────────────────────────────────────┐
                    │                   ZUUL                          │
                    │             (API Gateway)                       │
                    │  Dynamic routing | Auth | Rate limiting         │
                    │  Load balancing  | Canary testing               │
                    └────────────┬───────────────────┬────────────────┘
                                 │                   │
                    ┌────────────v────────┐          │
                    │      EUREKA         │          │
                    │ (Service Discovery) │<- - - - -│- - heartbeats
                    │  Registry + Cache   │          │
                    └──┬──────────────┬───┘          │
                       │              │              │
              ┌────────v──┐    ┌──────v────┐   ┌────v───────┐
              │ Service A │    │ Service B │   │ Service C  │
              │           │    │           │   │            │
              │ ┌───────┐ │    │ ┌───────┐ │   │ ┌───────┐  │
              │ │RIBBON │ │───>│ │RIBBON │ │──>│ │RIBBON │  │
              │ │(LB)   │ │    │ │(LB)   │ │   │ │(LB)   │  │
              │ └───────┘ │    │ └───────┘ │   │ └───────┘  │
              │ ┌───────┐ │    │ ┌───────┐ │   │ ┌───────┐  │
              │ │HYSTRIX│ │    │ │HYSTRIX│ │   │ │HYSTRIX│  │
              │ │(CB)   │ │    │ │(CB)   │ │   │ │(CB)   │  │
              │ └───────┘ │    │ └───────┘ │   │ └───────┘  │
              └─────┬─────┘    └─────┬─────┘   └─────┬──────┘
                    │                │               │
                    v                v               v
              ┌──────────────────────────────────────────────┐
              │                   ATLAS                       │
              │            (Telemetry / Metrics)              │
              │         1+ billion metrics / minute           │
              └──────────────────────────────────────────────┘

              ┌──────────────────────────────────────────────┐
              │                 SPINNAKER                     │
              │      (Continuous Delivery Platform)           │
              │    Canary | Blue-Green | Rolling deploys      │
              └──────────────────────────────────────────────┘
```

---

### 2.1 Zuul -- API Gateway

Zuul is the **front door** for all requests entering the Netflix backend.

**Responsibilities:**
- **Dynamic routing** -- route requests to different service clusters (A/B tests, canary deployments)
- **Authentication & security** -- terminate TLS, validate tokens
- **Rate limiting** -- protect backends from traffic spikes
- **Load balancing** -- distribute across instances
- **Request/response transformation** -- header injection, payload modification
- **Canary testing** -- route a percentage of traffic to new service versions

**Zuul 1 vs Zuul 2:**

| Aspect | Zuul 1 | Zuul 2 |
|--------|--------|--------|
| I/O Model | Blocking (Servlet) | Non-blocking (Netty) |
| Threading | Thread-per-connection | Event loop |
| Connections | ~Thousands | ~Tens of thousands |
| Use case | Simple proxying | Long-lived connections, WebSockets, SSE |

**Zuul 2 architecture:**
```
  Client Request
       │
       v
 ┌─────────────┐
 │  Netty I/O   │  <── Non-blocking event loop
 │  Front-end   │
 └──────┬───────┘
        │
        v
 ┌─────────────┐
 │  Inbound     │  <── Pre-filters: auth, rate-limit, routing decisions
 │  Filters     │
 └──────┬───────┘
        │
        v
 ┌─────────────┐
 │  Endpoint    │  <── Proxy filter: async HTTP call to origin
 │  Filter      │
 └──────┬───────┘
        │
        v
 ┌─────────────┐
 │  Outbound    │  <── Post-filters: metrics, headers, compression
 │  Filters     │
 └──────┬───────┘
        │
        v
 ┌─────────────┐
 │  Netty I/O   │
 │  Back-end    │
 └──────────────┘
```

The non-blocking model is critical: Netflix's API gateway handles **millions of requests/second**. A blocking model would require an impractical number of threads.

---

### 2.2 Eureka -- Service Discovery

Eureka is a **RESTful service registry**. Instead of hardcoding IP addresses, services find each other dynamically.

**How it works:**

```
                    ┌─────────────────────┐
                    │   Eureka Server      │
         ┌────────>│   (Registry)         │<────────┐
         │         │                      │         │
         │  ┌──────┤  Service A: [i1, i2] │         │
 Register│  │      │  Service B: [i3]     │  Register│
 + Heart-│  │Fetch │  Service C: [i4, i5] │  + Heart-│
   beat  │  │Registry                     │    beat  │
         │  │      └──────────────────────┘         │
         │  │                                       │
         │  v                                       │
    ┌────┴──────┐                           ┌───────┴───┐
    │ Service X │ ── direct call ──────────>│ Service A  │
    │           │    (using cached          │ instance 1 │
    │ [local    │     registry)             └────────────┘
    │  cache]   │
    └───────────┘

    Sequence:
    1. Service A registers with Eureka on startup
    2. Service A sends heartbeats every 30 seconds
    3. Service X fetches registry, caches locally
    4. Service X calls Service A directly (client-side LB)
    5. If Service A dies and misses heartbeats → evicted after 90s
```

**Key design decisions:**
- **AP over CP** (in CAP theorem terms) -- availability over consistency. A stale registry is better than no registry.
- **Client-side caching** -- every client keeps a local copy. If Eureka goes down, services still communicate using the last known registry.
- **Peer-to-peer replication** -- Eureka servers replicate among themselves. No single point of failure. No leader election.
- **Self-preservation mode** -- if too many heartbeats are missed simultaneously (likely network partition, not mass failure), Eureka stops evicting instances to prevent cascading de-registrations.

**No SPOF:**
```
  ┌──────────┐     replicate     ┌──────────┐
  │ Eureka-1 │ <──────────────>  │ Eureka-2 │
  │ (AZ-1)   │                   │ (AZ-2)   │
  └──────────┘                   └──────────┘
       ^                              ^
       │          replicate           │
       │     ┌──────────┐            │
       └────>│ Eureka-3 │<───────────┘
             │ (AZ-3)   │
             └──────────┘

  Each Eureka node is a full peer. Clients can
  register/query any node. Loss of one node is
  invisible to callers.
```

---

### 2.3 Ribbon -- Client-Side Load Balancing

Ribbon runs **inside the caller's process** (no separate proxy or sidecar).

**How it integrates:**
```
  ┌────────────────────────────────┐
  │         Service X              │
  │                                │
  │  ┌────────────┐  ┌─────────┐  │
  │  │ App Logic  │─>│ Ribbon  │  │
  │  └────────────┘  │         │  │
  │                  │ ┌─────┐ │  │
  │                  │ │Eurek│ │  │──> Service A (instance 1)
  │                  │ │Cache│ │  │──> Service A (instance 2)
  │                  │ └─────┘ │  │──> Service A (instance 3)
  │                  │         │  │
  │                  │ LB Rule │  │
  │                  └─────────┘  │
  └────────────────────────────────┘
```

**Load balancing strategies:**

| Strategy | Behavior | When to use |
|----------|----------|-------------|
| Round Robin | Cycle through instances sequentially | Default, uniform instances |
| Weighted Response Time | Favor faster instances | Heterogeneous hardware |
| Zone Aware | Prefer same-AZ instances to reduce latency & cross-AZ costs | Multi-AZ deployments |
| Availability Filtering | Skip instances that are down or have too many active connections | High-traffic services |
| Random | Random selection | Simple scenarios |

**Why client-side (not server-side)?**
- No extra network hop (no HAProxy/Nginx in the middle)
- No central load balancer as a bottleneck or SPOF
- Caller has context about which zone it's in for zone-aware routing
- Tradeoff: every client needs the Ribbon library (solved via shared platform libraries)

---

### 2.4 Hystrix -- Circuit Breaker

Hystrix prevents **cascading failures**. When a downstream service is slow or down, Hystrix fails fast instead of letting the caller hang.

> **Note:** Hystrix is in maintenance mode since 2018. Netflix moved to internal solutions. The community replacement is **Resilience4j** (lightweight, functional, Java 8+). The patterns remain identical.

#### Circuit Breaker State Machine

```
                    success / under threshold
                 ┌──────────────────────────────┐
                 │                              │
                 v                              │
          ┌──────────┐    failure rate     ┌────┴─────┐
          │          │    exceeds          │          │
          │  CLOSED  │    threshold        │  CLOSED  │
          │          │────────────────────>│          │
          │ (normal  │                     │(tracking)│
          │  flow)   │                     │          │
          └──────────┘                     └──────────┘
                                                │
                                                │ threshold breached
                                                v
                                          ┌──────────┐
                                          │          │
                                          │   OPEN   │
                                          │          │
                                          │ (fail    │
                                          │  fast,   │
                                          │  return  │
                                          │  fallback│
                                          │  )       │
                                          └────┬─────┘
                                               │
                                               │ sleep window expires
                                               v
                                         ┌───────────┐
                                         │           │
                                         │ HALF-OPEN │
                                    ┌───>│           │<───┐
                                    │    │(allow one │    │
                                    │    │ request)  │    │
                                    │    └─────┬─────┘    │
                                    │          │          │
                                    │     ┌────┴────┐     │
                                    │     │         │     │
                                  fail    v         v   success
                                    │  ┌──────┐ ┌──────┐  │
                                    │  │OPEN  │ │CLOSED│  │
                                    └──│      │ │      │──┘
                                       └──────┘ └──────┘
```

**Simplified state transitions:**

```
  CLOSED ──(failures exceed threshold)──> OPEN
  OPEN ────(sleep window expires)───────> HALF-OPEN
  HALF-OPEN ──(test request succeeds)──> CLOSED
  HALF-OPEN ──(test request fails)─────> OPEN
```

**States explained:**

| State | Behavior |
|-------|----------|
| **CLOSED** | Normal operation. Requests pass through. Failures are counted in a sliding window. |
| **OPEN** | All requests are **short-circuited** -- return fallback immediately. No calls to the downstream service. Timer starts. |
| **HALF-OPEN** | After the sleep window (e.g., 5 seconds), allow **one** test request through. If it succeeds, circuit closes. If it fails, circuit re-opens. |

**Configuration parameters:**
```
circuitBreaker.requestVolumeThreshold = 20    // min requests before tripping
circuitBreaker.errorThresholdPercentage = 50  // % failures to trip
circuitBreaker.sleepWindowInMilliseconds = 5000  // time before half-open
metrics.rollingStats.timeInMilliseconds = 10000  // sliding window
```

#### Bulkhead Isolation

Hystrix uses **thread pool isolation** per dependency, so one slow service cannot consume all threads:

```
  ┌─────────────────────────────────────────────────┐
  │               Service X                          │
  │                                                  │
  │  ┌───────────────┐  ┌───────────────┐           │
  │  │ Thread Pool:  │  │ Thread Pool:  │           │
  │  │ Service A     │  │ Service B     │           │
  │  │               │  │               │           │
  │  │ [t1] [t2] [t3]│  │ [t1] [t2]    │           │
  │  │ max=10        │  │ max=5         │           │
  │  └───────┬───────┘  └───────┬───────┘           │
  │          │                  │                    │
  │          │                  │                    │
  │  ┌───────────────┐  ┌───────────────┐           │
  │  │ Thread Pool:  │  │ Semaphore:    │           │
  │  │ Service C     │  │ Service D     │           │
  │  │               │  │ (in-memory    │           │
  │  │ [t1] [t2]    │  │  cache calls) │           │
  │  │ max=8         │  │ max=20        │           │
  │  └───────────────┘  └───────────────┘           │
  └─────────────────────────────────────────────────┘

  If Service A becomes slow and exhausts its 10 threads,
  Services B, C, D are completely unaffected.
  This is the bulkhead pattern (like compartments in a ship).
```

**Thread pool vs. semaphore isolation:**

| Aspect | Thread Pool | Semaphore |
|--------|-------------|-----------|
| Isolation | Full (separate threads) | Partial (caller's thread) |
| Timeout | Enforced (thread interrupt) | Not enforced |
| Overhead | Higher (context switching) | Lower |
| Use case | Network calls | In-memory / cache lookups |

#### Hystrix vs. Resilience4j

| Aspect | Hystrix | Resilience4j |
|--------|---------|-------------|
| Status | Maintenance mode (2018+) | Actively maintained |
| Design | Object-oriented, HystrixCommand | Functional, decorators |
| Dependencies | Heavy (Archaius, RxJava) | Lightweight (Vavr only) |
| Configuration | Archaius | YAML/code |
| Metrics | Built-in dashboard | Micrometer integration |
| Patterns | Circuit breaker, bulkhead | Circuit breaker, bulkhead, rate limiter, retry, time limiter |

---

### 2.5 Atlas -- Telemetry

Atlas is Netflix's **time-series telemetry system**, processing **1+ billion metrics per minute**.

**Key properties:**
- **In-memory** -- metrics are kept in-memory on each instance, queried on demand
- **Dimensional** -- metrics have tags (name=request.count, status=200, service=api)
- **Stack language** -- Atlas uses a stack-based query language for complex aggregations
- **Designed for operational insight** -- not long-term storage. Alert on real-time signals.

**Why not Prometheus/Graphite?**

At Netflix's scale (millions of instances, billions of time series), a central time-series database would collapse. Atlas's model keeps data distributed and queries fan out to instances.

---

### 2.6 Spinnaker -- Continuous Delivery

Spinnaker is a **multi-cloud continuous delivery platform** open-sourced by Netflix.

**Deployment strategies supported:**

```
  Blue-Green:
  ┌──────────┐     ┌──────────┐
  │  Blue    │     │  Green   │
  │  (live)  │     │  (new)   │
  └────┬─────┘     └────┬─────┘
       │                │
  ─────┴────────────────┴──── Load Balancer
  Traffic on Blue ──> instant switch to Green

  Canary:
  ┌──────────────────────────┐  ┌─────────┐
  │  Baseline (95% traffic)  │  │ Canary  │
  │                          │  │ (5%)    │
  └──────────────────────────┘  └─────────┘
  Compare metrics (latency, error rate) → promote or rollback

  Rolling:
  [v1] [v1] [v1] [v1] [v1]
  [v2] [v1] [v1] [v1] [v1]   ← replace one at a time
  [v2] [v2] [v1] [v1] [v1]
  [v2] [v2] [v2] [v1] [v1]
  [v2] [v2] [v2] [v2] [v2]
```

**Spinnaker pipeline example:**
```
  Code Merge → Build AMI → Bake → Deploy to Test
       → Integration Tests → Canary in Prod (5%)
       → Automated Analysis (ACA) → Full Rollout
       → or Automatic Rollback
```

Netflix uses **Automated Canary Analysis (ACA)** -- Kayenta -- to statistically compare canary vs. baseline metrics and make promote/rollback decisions without human judgment.

---

## 3. Chaos Engineering

### Philosophy

> "The best way to avoid failure is to fail constantly."

Netflix does not hope their systems are resilient. They **prove** it by injecting failures in production, during business hours, with engineers watching.

**Core principle:** If you only test resilience when a real failure happens, you are testing for the first time in the worst possible moment.

### The Simian Army

```
  ┌─────────────────────────────────────────────────────────────┐
  │                    THE SIMIAN ARMY                           │
  │                                                             │
  │  ┌──────────────┐  Kills random VM instances in production  │
  │  │ Chaos Monkey │  during business hours.                   │
  │  └──────────────┘  Question: Does the service handle it?    │
  │                                                             │
  │  ┌───────────────┐  Simulates an entire Availability Zone   │
  │  │ Chaos Gorilla │  going down (e.g., us-east-1a fails).   │
  │  └───────────────┘  Question: Does traffic shift to other   │
  │                     AZs automatically?                      │
  │                                                             │
  │  ┌──────────────┐  Simulates an entire AWS Region going     │
  │  │ Chaos Kong   │  down (e.g., all of us-east-1 fails).   │
  │  └──────────────┘  Question: Does traffic failover to       │
  │                    another region within a minute?           │
  │                                                             │
  │  ┌──────────────┐  Introduces artificial latency into       │
  │  │ Latency      │  RESTful calls between services.         │
  │  │ Monkey       │  Question: Do timeouts and fallbacks      │
  │  └──────────────┘  work correctly?                          │
  │                                                             │
  │  ┌──────────────┐  Detects instances not conforming to      │
  │  │ Conformity   │  best practices and shuts them down.     │
  │  │ Monkey       │  Question: Are all instances properly     │
  │  └──────────────┘  configured?                              │
  └─────────────────────────────────────────────────────────────┘
```

### How Chaos Monkey Works

```
  Schedule: Business hours only (Mon-Fri, 9am-3pm)
  Scope:    One random instance per service group

  ┌──────────┐      ┌──────────┐      ┌──────────┐
  │ Instance │      │ Instance │      │ Instance │
  │    A     │      │    B     │      │    C     │
  └──────────┘      └────┬─────┘      └──────────┘
                         │
                    Chaos Monkey
                    terminates B
                         │
                         v
  ┌──────────┐      ┌──────────┐      ┌──────────┐
  │ Instance │      │ Instance │      │ Instance │
  │    A     │      │    B     │      │    C     │
  │ (alive)  │      │  (DEAD)  │      │ (alive)  │
  └──────────┘      └──────────┘      └──────────┘
                         │
              Auto Scaling Group detects
              failure, launches new instance
                         │
                         v
  ┌──────────┐      ┌──────────┐      ┌──────────┐
  │ Instance │      │ Instance │      │ Instance │
  │    A     │      │    D     │      │    C     │
  │ (alive)  │      │  (new)   │      │ (alive)  │
  └──────────┘      └──────────┘      └──────────┘
```

**Why business hours?** Engineers are at their desks, alert, and able to observe the impact. Running chaos at 3am means failures go unnoticed until morning.

### Netflix vs. Google: Chaos Engineering Approaches

| Aspect | Netflix | Google |
|--------|---------|--------|
| Program | Chaos Engineering (public) | DiRT -- Disaster Recovery Testing |
| Approach | Continuous, automated, production | Scheduled exercises, large-scale |
| Tooling | Open-sourced (Simian Army, Chaos Monkey) | Internal, not publicly documented |
| Philosophy | "Fail constantly" | "Test disaster recovery rigorously" |
| Visibility | Extensive public talks, papers, blog posts | Limited public documentation |
| Community impact | Spawned an entire industry (chaos engineering as a discipline) | Influenced SRE practices internally |

---

## 4. Active-Active Multi-Region

### Architecture

Netflix serves production traffic from **multiple AWS regions simultaneously**. This is not active-passive (one live, one standby). Every region handles real user traffic at all times.

```
  ┌─────────────────────────────────────────────────────────────────┐
  │                        AWS Global                               │
  │                                                                 │
  │    ┌─────────────┐   ┌─────────────┐   ┌─────────────┐        │
  │    │ us-east-1   │   │ us-west-2   │   │ eu-west-1   │        │
  │    │             │   │             │   │             │        │
  │    │ ┌─────────┐ │   │ ┌─────────┐ │   │ ┌─────────┐ │        │
  │    │ │  Zuul   │ │   │ │  Zuul   │ │   │ │  Zuul   │ │        │
  │    │ └────┬────┘ │   │ └────┬────┘ │   │ └────┬────┘ │        │
  │    │      │      │   │      │      │   │      │      │        │
  │    │ ┌────v────┐ │   │ ┌────v────┐ │   │ ┌────v────┐ │        │
  │    │ │Services │ │   │ │Services │ │   │ │Services │ │        │
  │    │ │(1000+)  │ │   │ │(1000+)  │ │   │ │(1000+)  │ │        │
  │    │ └────┬────┘ │   │ └────┬────┘ │   │ └────┬────┘ │        │
  │    │      │      │   │      │      │   │      │      │        │
  │    │ ┌────v────┐ │   │ ┌────v────┐ │   │ ┌────v────┐ │        │
  │    │ │Cassandra│<──────>│Cassandra│<──────>│Cassandra│ │        │
  │    │ │ EVCache │ │   │ │ EVCache │ │   │ │ EVCache │ │        │
  │    │ │ Aurora  │ │   │ │ Aurora  │ │   │ │ Aurora  │ │        │
  │    │ └─────────┘ │   │ └─────────┘ │   │ └─────────┘ │        │
  │    └─────────────┘   └─────────────┘   └─────────────┘        │
  │          ^                  ^                  ^                │
  │          │                  │                  │                │
  │    ┌─────┴──────────────────┴──────────────────┴──────┐        │
  │    │              Route 53 (DNS)                       │        │
  │    │         Latency-based routing                     │        │
  │    │    + health checks + failover policy              │        │
  │    └──────────────────────────────────────────────────┘        │
  └─────────────────────────────────────────────────────────────────┘
```

### Why Active-Active Over Active-Passive?

**The bit-rot problem:**

```
  Active-Passive:
  ┌──────────────┐           ┌──────────────┐
  │  us-east-1   │           │  us-west-2   │
  │  (ACTIVE)    │           │  (STANDBY)   │
  │              │           │              │
  │  Real traffic│           │  No traffic  │
  │  Real load   │           │  No load     │
  │  Tested daily│           │  Never tested│
  │  Caches warm │           │  Caches cold │
  │  Configs     │           │  Config drift│
  │  current     │           │  possible    │
  └──────────────┘           └──────────────┘

  When us-east-1 fails and you failover to us-west-2:
  - Cold caches → thundering herd on databases
  - Config drift → services may not start correctly
  - Untested code paths → unknown bugs surface
  - You are now testing disaster recovery FOR THE FIRST TIME
    during an actual disaster
```

**Active-active eliminates this entirely:**
- Every region serves real traffic, so problems are caught immediately
- Caches are always warm
- Configurations are always current
- Failover is just "send more traffic to a region that already works"

### Capacity Headroom

Each region runs **below maximum capacity** to absorb failover traffic.

```
  Normal operation (3 regions):

  ┌──────────┐  ┌──────────┐  ┌──────────┐
  │us-east-1 │  │us-west-2 │  │eu-west-1 │
  │          │  │          │  │          │
  │ ████░░░░ │  │ ████░░░░ │  │ ████░░░░ │
  │  ~67%    │  │  ~67%    │  │  ~67%    │
  │          │  │          │  │          │
  └──────────┘  └──────────┘  └──────────┘

  ~33% headroom in each region

  After us-east-1 fails:

  ┌──────────┐  ┌──────────┐  ┌──────────┐
  │us-east-1 │  │us-west-2 │  │eu-west-1 │
  │          │  │          │  │          │
  │ ░░░░░░░░ │  │ ████████ │  │ ████████ │
  │  DOWN    │  │  ~100%   │  │  ~100%   │
  │          │  │          │  │          │
  └──────────┘  └──────────┘  └──────────┘

  Traffic redistributed. No region exceeds capacity.
  Users experience sub-minute failover.
```

**The math:**
- With N active regions, each runs at `(N-1)/N` capacity
- 3 regions: each at ~67% (33% headroom)
- 4 regions: each at ~75% (25% headroom)
- Tradeoff: more regions = less headroom needed per region, but higher infrastructure cost

### Data Replication

The hardest part of multi-region is **keeping data consistent**. Netflix uses different strategies for different data stores:

```
  ┌─────────────────────────────────────────────────────────┐
  │                  Data Replication                        │
  │                                                         │
  │  Cassandra (Multi-directional async replication)        │
  │  ┌──────────┐  async  ┌──────────┐  async ┌──────────┐│
  │  │ C* East  │ <────> │ C* West  │ <────> │ C* EU    ││
  │  └──────────┘         └──────────┘        └──────────┘│
  │  - Eventual consistency (LOCAL_QUORUM for reads/writes)│
  │  - Conflict resolution: last-write-wins (LWW)         │
  │  - Write locally, replicate asynchronously             │
  │                                                         │
  │  EVCache (Zone-aware replication)                       │
  │  ┌──────────┐  copy   ┌──────────┐  copy  ┌──────────┐│
  │  │ EC East  │ ─────> │ EC West  │ ─────> │ EC EU    ││
  │  └──────────┘         └──────────┘        └──────────┘│
  │  - Write to local zone, replicate to others            │
  │  - Read from local zone only (low latency)             │
  │  - On miss: fetch from Cassandra, populate cache       │
  │                                                         │
  │  Aurora Global Database                                 │
  │  ┌──────────┐  <1s   ┌──────────┐  <1s   ┌──────────┐│
  │  │ Primary  │ ─────> │ Replica  │ ─────> │ Replica  ││
  │  │ (East)   │  lag   │ (West)   │   lag  │ (EU)     ││
  │  └──────────┘         └──────────┘        └──────────┘│
  │  - Single primary writer, multi-region read replicas   │
  │  - Sub-second replication lag                          │
  │  - Used for data requiring stronger consistency        │
  └─────────────────────────────────────────────────────────┘
```

### Regional Failover Sequence

```
  Time 0:00  - Health checks detect us-east-1 degradation
  Time 0:10  - Automated decision: failover us-east-1
  Time 0:15  - Route 53 DNS weight for us-east-1 set to 0
  Time 0:20  - DNS TTL expires, clients resolve to us-west-2 / eu-west-1
  Time 0:30  - Traffic fully drained from us-east-1
  Time 0:45  - Remaining regions absorb traffic within headroom
  Time <1:00 - Full failover complete, user impact minimal

  Total: sub-minute failover for most users
  (DNS TTL is the dominant factor)
```

---

## 5. Fallback Strategies

Netflix's core UX principle: **show something, never show an error page**.

### The Fallback Hierarchy

```
  Request: Get personalized recommendations for user U

  ┌──────────────────────────────────────────────┐
  │ 1. IDEAL: Full personalized recommendations  │
  │    Recommendation service returns tailored    │
  │    rows based on viewing history, preferences │
  └────────────────────┬─────────────────────────┘
                       │ fails
                       v
  ┌──────────────────────────────────────────────┐
  │ 2. DEGRADED: Cached recommendations          │
  │    Return the last known good recommendations │
  │    from EVCache (may be hours old)            │
  └────────────────────┬─────────────────────────┘
                       │ fails
                       v
  ┌──────────────────────────────────────────────┐
  │ 3. GENERIC: "Popular on Netflix" rows        │
  │    Pre-computed, static rows. Same for all   │
  │    users. Updated daily. Stored in S3/CDN.   │
  └────────────────────┬─────────────────────────┘
                       │ fails (extremely unlikely)
                       v
  ┌──────────────────────────────────────────────┐
  │ 4. MINIMAL: Hardcoded fallback catalog       │
  │    Baked into the client app at build time.  │
  │    Absolute last resort.                     │
  └──────────────────────────────────────────────┘
```

### Fallback Examples Across Services

```
  ┌─────────────────┬────────────────────┬──────────────────────┐
  │ Service         │ Normal Response    │ Fallback Response    │
  ├─────────────────┼────────────────────┼──────────────────────┤
  │ Recommendations │ Personalized rows  │ "Popular" / "Top 10" │
  │                 │ (ML models)        │ (pre-computed)       │
  ├─────────────────┼────────────────────┼──────────────────────┤
  │ Artwork         │ Personalized art   │ Default artwork      │
  │                 │ (A/B tested)       │ (static asset)       │
  ├─────────────────┼────────────────────┼──────────────────────┤
  │ Search          │ Full search with   │ Cached popular       │
  │                 │ ranking + filters  │ searches / titles    │
  ├─────────────────┼────────────────────┼──────────────────────┤
  │ User Profile    │ Full viewing       │ Cached profile,      │
  │                 │ history + prefs    │ last known state     │
  ├─────────────────┼────────────────────┼──────────────────────┤
  │ Playback        │ Optimal bitrate    │ Lower bitrate,       │
  │                 │ from nearest CDN   │ fallback CDN         │
  ├─────────────────┼────────────────────┼──────────────────────┤
  │ Billing         │ Process payment    │ Queue for retry,     │
  │                 │ in real-time       │ extend grace period  │
  └─────────────────┴────────────────────┴──────────────────────┘
```

### Implementing Fallbacks with Hystrix/Resilience4j

```
  Conceptual pseudocode:

  @HystrixCommand(
      fallbackMethod = "getDefaultRecommendations",
      commandProperties = {
          @HystrixProperty(name = "circuitBreaker.errorThresholdPercentage", value = "50"),
          @HystrixProperty(name = "execution.isolation.thread.timeoutInMilliseconds", value = "1000")
      }
  )
  public List<Row> getRecommendations(String userId) {
      return recommendationService.getPersonalized(userId);   // may fail
  }

  public List<Row> getDefaultRecommendations(String userId) {
      // Tier 1 fallback: cached recommendations
      List<Row> cached = evCache.get("recs:" + userId);
      if (cached != null) return cached;

      // Tier 2 fallback: generic popular content
      return staticContentService.getPopularRows();
  }
```

### Why This Matters

```
  Traditional approach:             Netflix approach:

  Service down?                     Service down?
       │                                 │
       v                                 v
  ┌──────────┐                    ┌──────────────┐
  │  500      │                    │  Degraded    │
  │  Internal │                    │  but         │
  │  Server   │                    │  functional  │
  │  Error    │                    │  response    │
  └──────────┘                    └──────────────┘
       │                                 │
       v                                 v
  User sees error page             User sees content
  User leaves                      User keeps watching
  Revenue lost                     Revenue preserved
```

At Netflix's scale (250M+ subscribers), even a 1% error rate during a partial outage means **2.5 million users** seeing errors. Fallbacks convert those errors into slightly-less-perfect-but-functional experiences.

---

## Summary: How It All Fits Together

```
  User opens Netflix app
       │
       v
  Route 53 (DNS) ── latency-based routing to nearest region
       │
       v
  Zuul (API Gateway) ── auth, rate limit, route
       │
       v
  Eureka (Discovery) ── resolve service name to instances
       │
       v
  Ribbon (Load Balancer) ── pick best instance (zone-aware)
       │
       v
  Target Service ── wrapped in Hystrix circuit breaker
       │
       ├── Success? Return response
       │
       └── Failure? Hystrix opens circuit
              │
              v
           Fallback: cached/generic response
              │
              v
           User sees content (never an error page)

  Meanwhile:
  - Atlas collects 1B+ metrics/minute
  - Chaos Monkey kills a random instance
  - Spinnaker deploys a canary of the next version
  - Cassandra replicates data across regions
  - Everything is observable, everything is tested
```

The entire philosophy: **assume everything will fail, design every component with a fallback, prove it works by breaking things constantly, and serve from multiple regions so no single failure takes down Netflix.**
