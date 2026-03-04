# Load Balancing & Service Discovery — Deep Dive

---

## 1. Load Balancing Algorithms

### Round-Robin

Simple rotation across upstream instances. Equal distribution.

**Pros:** Simple, predictable. **Cons:** No awareness of server load or response time. Works well when backends are homogeneous.

### Weighted Round-Robin

Assign weights to upstreams. Server with weight 3 gets 3x traffic of weight 1.

**Use cases:** Heterogeneous backends (bigger server = higher weight), canary deployments (new version gets low weight initially).

### Least Connections

Route to the upstream with fewest active connections.

**Pros:** Better for long-lived requests (WebSocket, file uploads). **Cons:** Requires gateway to track active connections per upstream. Slight overhead.

### Consistent Hashing

Hash a request attribute (IP, user ID, session cookie) to select an upstream. Same client always goes to the same server (sticky sessions).

```
Hash ring:

    0 ──── Server A ──── Server B ──── Server C ──── 2^32
                  |                |
          hash(user123)    hash(user456)

user123 → Server A (always, unless ring changes)
user456 → Server B (always, unless ring changes)
```

**Pros:** Minimizes redistribution when servers added/removed. **Use case:** When upstream caches are important (session affinity).

### Random with Two Choices (P2C)

Pick two random upstreams, route to the one with fewer active connections.

**Why it works:** Provably better than pure random and nearly as good as least-connections, with O(1) decision time. Avoids the "thundering herd" problem where all load balancers simultaneously pick the least-loaded server.

**Used by Envoy as default algorithm.**

### Latency-Based / EWMA

Track exponentially weighted moving average of response times per upstream. Route to the fastest.

**Pros:** Adapts to transient slowness. **Risk:** Thundering herd to the fastest server — everyone routes there, making it the slowest.

### Decision Guide

```
Session affinity needed?
  └── Yes → Consistent hashing
  └── No
        Backends homogeneous?
          └── Yes
          |     Latency sensitivity critical?
          |       └── Yes → EWMA or P2C with latency
          |       └── No  → P2C (Envoy default) or Round-robin
          └── No (different sizes)
                → Weighted round-robin or Weighted P2C
```

---

## 2. Health Checking

### Active Health Checks

Gateway periodically probes each upstream.

```
Gateway → GET /health → Upstream Instance

Config:
  interval:             5 seconds
  timeout:              2 seconds
  unhealthy_threshold:  3 consecutive failures → mark UNHEALTHY
  healthy_threshold:    2 consecutive successes → mark HEALTHY
```

**Pros:** Proactive detection — finds problems before real requests fail.
**Cons:** Consumes upstream bandwidth (one probe per instance per interval).

### Passive Health Checks (Outlier Detection)

Gateway monitors actual request success/failure rates.

```
If failure rate > 50% in last 10 requests → mark UNHEALTHY
  (stop sending traffic)

If marked unhealthy for 30 seconds → mark HEALTHY
  (try again)
```

**Pros:** No extra probe traffic. Detects real failures. **Cons:** Detects failures only after real requests fail.

### Hybrid (Recommended)

Use both. Active checks for baseline health. Passive checks for rapid detection of sudden failures. Mark unhealthy on either signal.

### Health Check Endpoint Design

```json
{
  "status": "healthy",
  "details": {
    "db": "ok",
    "cache": "ok",
    "downstream_service": "ok"
  }
}
```

- **Shallow check:** Just return 200 ("process is alive"). Fast, low overhead.
- **Deep check:** Verify DB, cache, downstream services. More useful but more expensive and can cascade failures.
- Return 503 if any critical dependency is down.

---

## 3. Service Discovery

### Static Configuration

Upstream addresses hardcoded in config file.

```yaml
upstream user-service:
  - 10.0.1.10:8080
  - 10.0.1.11:8080
  - 10.0.1.12:8080
```

**Pros:** Simple, no external dependency. **Cons:** Requires restart/reload to add/remove instances. Only for small, stable environments.

### DNS-Based

Upstream defined as a DNS name. DNS returns multiple A records.

```
user-service.internal → [10.0.1.10, 10.0.1.11, 10.0.1.12]
```

**Pros:** Simple, widely supported. **Cons:** DNS TTL delays discovery of new instances. DNS doesn't indicate health. Many DNS clients cache aggressively.

### Consul / etcd / ZooKeeper

Service registers itself on startup, deregisters on shutdown, sends heartbeats. Gateway watches for changes.

```
user-service registers:
  Consul key: services/user-service/10.0.1.10:8080
  With health check: HTTP GET /health every 10s

Gateway watches: services/user-service/*
  → Gets real-time updates when instances join/leave
```

**Used by Kong** (with DNS or Consul integration).

### Kubernetes Service / kube-proxy

Services discovered via Kubernetes API (Endpoints resource) or DNS (`service.namespace.svc.cluster.local`).

Envoy in Kubernetes uses **EDS (Endpoint Discovery Service)** via Istio's control plane to get real-time endpoint lists. This is more dynamic than Kubernetes DNS.

### Eureka (Netflix)

RESTful service registry. Services self-register, send heartbeats. Clients cache the registry. Peer-to-peer replication between Eureka instances. Used by Netflix's Zuul gateway.

---

## 4. Connection Pooling

Gateway maintains persistent connections (keep-alive) to upstream services.

```
Without pooling (per-request connection):
  TCP handshake:  ~1.5 ms (same region)
  TLS handshake:  ~3 ms (same region)
  Total overhead: ~4.5 ms per request

With connection pooling:
  Reuse existing: 0 ms handshake overhead
```

**HTTP/1.1:** Pool of keep-alive connections. Size = expected concurrency to that upstream. NGINX: `upstream { keepalive 64; }`.

**HTTP/2 multiplexing:** Single connection carries hundreds of concurrent streams. Dramatically reduces pool size needed.

**Tuning:** Too small pool → connection contention (requests wait for a free connection). Too large → exhausts upstream resources (file descriptors, memory).

---

## 5. Retry and Timeout

### Per-route timeout configuration

| Timeout | Typical Value | Purpose |
|---|---|---|
| Connect timeout | 1-3 seconds | Time to establish TCP connection. Short — fail fast if unreachable. |
| Request timeout | 5-60 seconds | Time for full response. Route-specific (health check: 2s, reports: 60s). |
| Idle timeout | 30-120 seconds | Close keep-alive connections idle for N seconds. Prevent resource leaks. |

### Retry policy

- Retry on: 502, 503, 504, connection failure, timeout
- Do NOT retry: 4xx (client error), 500 (server bug — will fail again)
- **Retry budget:** Max 20% of requests can be retries — prevents retry storms
- **Exponential backoff with jitter:** `delay = min(cap, base × 2^attempt) + random_jitter`

---

## 6. Gateway Contrasts

### NGINX

- `upstream` block: round-robin, `least_conn`, `ip_hash`, `hash` (generic key)
- **Active health checks:** NGINX Plus only (commercial). Open-source has passive only.
- Service discovery: DNS or third-party modules

### Envoy

- First-class service discovery via **EDS (Endpoint Discovery Service)**
- Real-time endpoint updates from control plane
- All LB algorithms built in, plus custom via extensions
- Built-in **outlier detection** (passive health checking)
- Connection pool management per upstream cluster
- Retry budgets, circuit breaking, locality-aware routing built in
- **P2C (Power of Two Choices)** as default LB algorithm

### AWS API Gateway

- No load balancing concept — routes to a single integration (Lambda, HTTP endpoint, VPC link)
- For load balancing behind AWS API Gateway, use ALB/NLB as the integration target
- No service discovery — you specify the backend URL

### Comparison

| Feature | NGINX | Envoy | AWS API Gateway |
|---|---|---|---|
| LB algorithms | RR, least_conn, ip_hash | RR, LC, consistent hash, P2C, EWMA, Maglev | None (single target) |
| Active health checks | NGINX Plus only | Built-in | N/A |
| Passive health checks | Built-in | Outlier detection | N/A |
| Service discovery | DNS / modules | EDS (xDS), DNS | N/A (specify URL) |
| Connection pooling | `keepalive` directive | Per-cluster pools, HTTP/2 mux | Managed |
| Retry budgets | Not built-in | Built-in | 2 retries (Lambda, fixed) |
