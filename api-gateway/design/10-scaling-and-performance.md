# Scaling & Performance — Deep Dive

---

## 1. Scale Numbers

### Request Throughput Per Node

| Gateway | Basic Proxy (RPS) | With Plugins (RPS) | Notes |
|---|---:|---:|---|
| Kong | ~137,000 | ~96,000 | Lua plugins add overhead per request |
| NGINX (raw) | 200K-500K+ | N/A | Depends on worker count, payload size |
| Envoy | 100K-300K+ | 80K-200K | Depends on filter chain complexity |
| AWS API Gateway | 10K default | 10K (soft cap) | Managed; can request increases |

### Latency Overhead Breakdown

| Stage | Latency | Notes |
|---|---|---|
| TLS termination | ~1 ms | Session resumption reduces to ~0.3 ms |
| Route matching | ~0.1 ms | Trie-based; negligible |
| Authentication (JWT) | 1-3 ms | Local validation; RSA ~2-3 ms, HMAC ~0.5 ms |
| Rate limiting (Redis) | 1-3 ms | Network RTT to Redis dominates |
| Request logging | ~0.1 ms | Async/buffered; off the hot path |
| **Total overhead** | **3-8 ms** | Typical with full plugin chain |

The gateway should add no more than 10 ms of total overhead.

### Concurrent Connections

| Metric | Range |
|---|---|
| Concurrent connections per node | 10,000 - 100,000 |
| File descriptors needed | ~2 per proxied request (client + upstream) |
| Kernel tuning required | `ulimit -n`, `net.core.somaxconn`, `net.ipv4.tcp_tw_reuse` |

---

## 2. Horizontal Scaling

The gateway is **stateless** by design. Every request is self-contained (URL, headers, auth tokens). No session state on the node.

```
                 Internet
                    │
              [ L4 NLB (TCP) ]
             /    │    │    \
        [GW-1] [GW-2] [GW-3] [GW-N]     ← stateless, identical
             \    │    │    /
        [ Shared External State ]
        - Redis (rate limits)
        - PostgreSQL (config)
```

**Scaling playbook:**
1. Monitor CPU, memory, connection count per node
2. When any node exceeds 70% CPU or 60K connections, add a node
3. L4 NLB distributes traffic automatically
4. No coordination between gateway nodes needed

---

## 3. L4 vs L7 Load Balancing in Front of the Gateway

| Aspect | L4 NLB (TCP) | L7 ALB (HTTP) |
|---|---|---|
| Operates at | TCP layer | HTTP layer |
| TLS termination | No (gateway handles it) | Yes (terminates, re-encrypts) |
| Routing capability | IP + port only | Path, host, headers |
| Latency added | Microseconds | Milliseconds |
| Connection capacity | Millions | Hundreds of thousands |
| Redundancy with GW | No — different responsibilities | Yes — duplicates gateway L7 logic |

### Recommendation: L4 NLB

```
Client → L4 NLB (TCP pass-through) → Gateway (ALL L7 work) → Upstream
```

**Why:**
1. No redundant processing — gateway already does TLS, routing, auth
2. Lowest latency — L4 is kernel-level, microseconds
3. Highest throughput — millions of connections
4. TLS ownership is clear — one place for certificates
5. Simpler debugging — gateway owns all L7 concerns

---

## 4. Performance Optimizations

### 4.1 Event-Driven Architecture

NGINX and Envoy use event-driven, non-blocking I/O:

```
One thread:
  epoll_wait() → events ready on 3,000 connections
  for each event:
    read data, process, write response
  loop
```

- **Linux:** `epoll` — O(1) event notification, scales to millions of FDs
- **BSD/macOS:** `kqueue` — equivalent for BSD systems
- NGINX: one worker per CPU core; 8 cores = handles 100K+ connections

### 4.2 Connection Pooling to Upstreams

```
Without pooling:  TCP + TLS handshake = ~4.5 ms per request
With pooling:     Reuse existing connection = 0 ms overhead
```

- HTTP/1.1: `keepalive` connections. NGINX: `upstream { keepalive 64; }`
- HTTP/2 multiplexing: single connection carries hundreds of concurrent streams

### 4.3 TLS Session Resumption

Full TLS 1.2: 2 RTT. With session tickets: 1 RTT. TLS 1.3: 1 RTT (0-RTT for repeats).

```nginx
ssl_session_cache   shared:SSL:50m;
ssl_session_timeout 1h;
ssl_session_tickets on;
```

At 100K RPS, session resumption saves enormous aggregate latency and CPU.

### 4.4 Response Caching

Cache GET responses at the gateway. Eliminate upstream calls for cacheable responses.

- **Cache hit rate:** 80-90% achievable for read-heavy workloads
- **Cache key:** method + path + query params + relevant headers
- **Invalidation:** TTL-based, event-based, or stale-while-revalidate

Impact: 90% cache hit rate at 100K RPS → backend sees only 10K RPS.

### 4.5 Hot Path Optimizations

| Optimization | Technique | Impact |
|---|---|---|
| Auth token caching | LRU cache (TTL = token expiry) | Eliminates JWT re-verification |
| Trie-based route lookup | Prefix tree | O(path length) vs O(routes) |
| Local rate limit counters | In-memory; sync to Redis periodically | Eliminates Redis RTT on hot path |
| Async logging | Buffer log entries; flush in background | Removes I/O from request path |
| Pre-compiled regex | Compile at startup, not per-request | Avoids compilation overhead |

### 4.6 Zero-Copy Proxying

When the gateway purely proxies (no body transformation):
- **`sendfile()`:** File data → socket, no user-space copy
- **`splice()`:** Socket → socket, entirely in kernel space

Eliminates two memory copies per request at high throughput.

### 4.7 Kernel Bypass (DPDK)

For extreme performance (10M+ packets/sec):
- DPDK bypasses kernel network stack entirely
- Application polls NIC directly from user space
- Used by Cloudflare, telecom, HFT
- Overkill for 99.9% of API gateways

---

## 5. High Availability

### Active-Active Multi-Node

All gateway nodes are active simultaneously. No primary/secondary.

```
       [ L4 NLB ]
      /    │     \
 [GW-1] [GW-2] [GW-3]     ← all active, all serving
```

If GW-2 dies: NLB detects (health check), removes from rotation. GW-1 and GW-3 absorb traffic.

### Config Database HA

If config DB (PostgreSQL for Kong) goes down:
- Gateways **continue serving** with last-known cached config
- New config changes cannot be applied until DB recovers
- **Critical property:** data plane must never depend on control plane being available

### Redis HA for Rate Limiting

| Mode | Failover Time |
|---|---|
| Redis Sentinel | 10-30 seconds |
| Redis Cluster | 1-5 seconds |

**If Redis is completely down:**
- **Option A (permissive):** Disable rate limiting, allow all traffic
- **Option B (local fallback):** In-memory per-node limiting (less accurate but functional)
- **Option C (strict):** Reject requests

**Recommendation:** Option B — degraded-but-functional rate limiting.

### Graceful Shutdown

```
1. SIGTERM received
2. Stop accepting NEW connections (deregister from NLB)
3. Finish IN-FLIGHT requests (drain period, e.g., 30s)
4. Close all connections
5. Exit
```

### Zero-Downtime Deployments

| Strategy | How | Risk |
|---|---|---|
| Rolling update | Replace one node at a time. N-1 always serving. | Low |
| Blue-green | New fleet, switch NLB. Keep old as rollback. | Very low |
| Canary | 5% traffic to new version. Monitor. Gradually increase. | Lowest |

---

## 6. Gateway Contrasts

### NGINX

- **Master + worker model.** One worker per core. Event-driven (epoll/kqueue).
- `nginx -s reload` is graceful: new workers start, old drain and exit. Zero dropped connections.
- Dynamic upstreams: Open-source requires reload. NGINX Plus has dynamic upstream API.

### Envoy

- **Multi-threaded event loops.** Thread-local storage, no lock contention.
- **xDS protocol:** Fully dynamic, no reload, no restart. Sub-second config updates.
- **Hot restart:** New process inherits listen sockets from old process. Zero dropped connections during binary upgrade.
- Richer observability out of the box but higher operational complexity.

### AWS API Gateway

- **Fully managed, auto-scales.** Zero capacity planning.
- **10,000 RPS** default per region (soft limit).
- **29-second timeout** (REST APIs). Not configurable.
- **10 MB** payload limit.
- Cost: $3.50 per million requests + data transfer. At 100K RPS ≈ ~$9M/year. Self-hosted is dramatically cheaper at scale.

### When to Choose

| Requirement | NGINX | Envoy | AWS API Gateway |
|---|---|---|---|
| Simplest config | Yes (file-based) | No | Yes (console) |
| Fully dynamic | NGINX Plus only | Yes (xDS) | Yes (managed) |
| Best for K8s/mesh | No | Yes | No |
| Custom plugins | Lua (OpenResty) | C++/Wasm/Lua | Lambda only |
| Cost at >100K RPS | Low (self-hosted) | Low (self-hosted) | Very high |
| Ops burden | Medium | High | None |
