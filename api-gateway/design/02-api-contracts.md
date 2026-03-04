# API Contracts — Gateway Management & Configuration APIs

This document lists all major API surfaces of an API Gateway — both the **data-plane APIs** (how client requests flow through the gateway) and the **control-plane / admin APIs** (how operators configure the gateway).

---

## 1. Route Management APIs

Routes define how incoming requests are matched and forwarded to upstream services.

### POST /admin/routes

Create a new route.

**Request:**

```json
{
  "name": "user-service-v1",
  "match": {
    "hosts": ["api.example.com"],
    "paths": ["/api/v1/users"],
    "methods": ["GET", "POST", "PUT", "DELETE"],
    "headers": {
      "X-Api-Version": ["1"]
    }
  },
  "action": {
    "upstream": "svc-user-001",
    "strip_path_prefix": "/api/v1",
    "add_headers": {
      "X-Forwarded-Service": "user-service"
    }
  },
  "priority": 100,
  "enabled": true,
  "tags": ["production", "user-team"]
}
```

**Field notes:**

| Field | Purpose |
|---|---|
| `match.hosts` | Route by `Host` header. Multi-tenant: `api.tenant-a.com` → tenant A services. |
| `match.paths` | Path prefix, exact, or regex match. More specific matches take priority. |
| `match.methods` | Restrict to specific HTTP methods. Omit for all methods. |
| `match.headers` | Header-based routing. Useful for A/B testing: `X-Api-Version: 2` → v2 service. |
| `action.upstream` | Target service ID (references a service registered via `/admin/services`). |
| `action.strip_path_prefix` | Remove prefix before forwarding: `/api/v1/users` → `/users` on upstream. Decouples external versioned URL from internal service paths. |
| `priority` | Higher value = evaluated first. Resolves ambiguity when multiple routes match. |

**Route matching order** (evaluated by specificity):
1. Exact host match → prefix host → wildcard host
2. Exact path → longest prefix match → regex (in priority order)
3. Specific HTTP method → any method
4. Header conditions

**Response (201 Created):**

```json
{
  "id": "route-a1b2c3d4",
  "name": "user-service-v1",
  "match": { "..." : "..." },
  "action": { "..." : "..." },
  "priority": 100,
  "enabled": true,
  "created_at": "2025-06-15T10:30:00Z",
  "updated_at": "2025-06-15T10:30:00Z"
}
```

### GET /admin/routes

List all routes. Supports `?tag=production`, `?upstream=svc-user-001`, pagination (`?offset=0&limit=50`).

### GET /admin/routes/{routeId}

Fetch a single route by ID.

### PUT /admin/routes/{routeId}

Update a route. Changes propagate to all gateway nodes within seconds (database polling) or sub-second (xDS push).

### DELETE /admin/routes/{routeId}

Remove a route. Active connections using this route are not dropped — they complete normally. New requests receive 404.

### Weight-Based Routing for Canary Deployments

```json
{
  "name": "order-service-canary",
  "match": {
    "paths": ["/api/v1/orders"]
  },
  "action": {
    "weighted_upstreams": [
      { "upstream": "svc-order-v1", "weight": 95 },
      { "upstream": "svc-order-v2", "weight": 5 }
    ]
  }
}
```

Weights are percentage-based. The gateway uses weighted random selection: 95% of requests go to v1, 5% to v2. Used for gradual rollouts — monitor error rate on v2, increase weight if healthy.

---

## 2. Service / Upstream Management APIs

Services represent the backend applications that the gateway proxies to.

### POST /admin/services

Register an upstream service.

**Request:**

```json
{
  "name": "user-service",
  "host": "user-service.internal",
  "port": 8080,
  "protocol": "http",
  "connect_timeout_ms": 2000,
  "read_timeout_ms": 10000,
  "write_timeout_ms": 10000,
  "retries": 2,
  "targets": [
    { "host": "10.0.1.10", "port": 8080, "weight": 100 },
    { "host": "10.0.1.11", "port": 8080, "weight": 100 },
    { "host": "10.0.1.12", "port": 8080, "weight": 50 }
  ],
  "health_check": {
    "active": {
      "type": "http",
      "path": "/health",
      "interval_seconds": 5,
      "timeout_seconds": 2,
      "unhealthy_threshold": 3,
      "healthy_threshold": 2
    },
    "passive": {
      "unhealthy": {
        "http_statuses": [500, 502, 503],
        "tcp_failures": 3,
        "timeouts": 3
      }
    }
  },
  "load_balancing": {
    "algorithm": "round-robin"
  },
  "tags": ["production", "user-team"]
}
```

**Field notes:**

| Field | Purpose |
|---|---|
| `targets` | Individual backend instances. Can be static IPs or DNS names. Weighted for heterogeneous instances. |
| `health_check.active` | Gateway probes `/health` every 5 seconds. 3 consecutive failures → mark unhealthy. 2 successes → mark healthy. |
| `health_check.passive` | Gateway monitors actual request outcomes. 500/502/503 responses or TCP failures increment failure count. |
| `load_balancing.algorithm` | `round-robin`, `least-connections`, `consistent-hashing`, `random-two-choices` (P2C). |

### GET /admin/services

List all registered services.

### PUT /admin/services/{serviceId}

Update service configuration. Changing targets triggers immediate health checks on new targets.

### DELETE /admin/services/{serviceId}

Remove a service. Fails if any routes reference it (409 Conflict).

---

## 3. Plugin / Middleware APIs

Plugins add cross-cutting behavior (auth, rate limiting, logging) to routes, services, or globally.

### POST /admin/plugins

Attach a plugin.

**Request:**

```json
{
  "name": "rate-limiting",
  "scope": {
    "route_id": "route-a1b2c3d4"
  },
  "config": {
    "minute": 100,
    "hour": 5000,
    "policy": "redis",
    "redis_host": "redis.internal",
    "redis_port": 6379,
    "fault_tolerant": true,
    "limit_by": "consumer"
  },
  "enabled": true,
  "priority": 900
}
```

**Scope precedence** (most specific wins):
1. **Route-level**: `{ "route_id": "route-abc" }` — applies only to this route
2. **Service-level**: `{ "service_id": "svc-user" }` — applies to all routes targeting this service
3. **Global**: `{}` (empty scope) — applies to all requests

**Plugin types:**

| Plugin | Purpose | Phase |
|---|---|---|
| `jwt` | Validate JWT token, extract claims | Access |
| `key-auth` | Validate API key | Access |
| `rate-limiting` | Enforce request limits | Access |
| `cors` | Handle CORS preflight, set headers | Access + Header Filter |
| `request-transformer` | Add/remove/rename headers, rewrite path | Access |
| `response-transformer` | Modify response headers, body | Header Filter + Body Filter |
| `ip-restriction` | Allow/deny by IP/CIDR | Access |
| `prometheus` | Export Prometheus metrics | Log |
| `zipkin` | Inject/propagate trace headers | Access + Log |
| `request-size-limiting` | Reject payloads above size limit | Access |

**Plugin execution order**: Determined by `priority` field. Higher priority runs first. Example: auth (priority 1000) runs before rate-limiting (priority 900), which runs before logging (priority 100).

### GET /admin/plugins

List all plugins. Filter by `?route_id=...`, `?service_id=...`, `?name=rate-limiting`.

### PUT /admin/plugins/{pluginId}

Update plugin configuration. Changes take effect within seconds.

### DELETE /admin/plugins/{pluginId}

Remove a plugin instance.

---

## 4. Consumer / API Key APIs

Consumers represent API clients — mobile apps, partner integrations, internal services.

### POST /admin/consumers

**Request:**

```json
{
  "username": "mobile-app-ios",
  "custom_id": "app-ios-prod",
  "groups": ["premium-tier"],
  "tags": ["mobile", "production"]
}
```

### POST /admin/consumers/{consumerId}/credentials

Generate credentials for a consumer.

**API Key:**

```json
{
  "type": "key-auth",
  "key": "auto-generated-or-specified"
}
```

**Response:**

```json
{
  "id": "cred-x1y2z3",
  "consumer_id": "consumer-m1n2o3",
  "type": "key-auth",
  "key": "ak_live_7f8e9d0c1b2a3456",
  "created_at": "2025-06-15T10:30:00Z"
}
```

### DELETE /admin/consumers/{consumerId}/credentials/{credId}

Revoke a credential. Takes effect immediately across all gateway nodes.

### Consumer Groups

```json
{
  "name": "premium-tier",
  "rate_limit_policy": "rl-premium-001",
  "plugins": [
    { "name": "rate-limiting", "config": { "minute": 10000 } }
  ]
}
```

Consumers in the same group share rate limit policies and plugin configurations. Changing the group config updates all members.

---

## 5. Rate Limiting Configuration APIs

Reusable rate limit policies that can be referenced by plugins and consumer groups.

### POST /admin/rate-limits

**Request:**

```json
{
  "name": "premium-api-plan",
  "limits": [
    { "window": "second", "max_requests": 200 },
    { "window": "minute", "max_requests": 10000 },
    { "window": "hour", "max_requests": 500000 }
  ],
  "policy": "sliding-window",
  "scope": "per-consumer",
  "burst_multiplier": 1.5,
  "response_headers": {
    "include_limit": true,
    "include_remaining": true,
    "include_reset": true
  },
  "exceeded_response": {
    "status_code": 429,
    "body": {
      "error": "rate_limit_exceeded",
      "message": "Rate limit exceeded. Retry after the time in Retry-After header."
    }
  }
}
```

**Multiple windows**: A request is rejected if ANY window is exceeded. This prevents per-second bursts while also capping daily usage.

**Rate limit response headers** (sent on every response):

```
X-RateLimit-Limit: 10000
X-RateLimit-Remaining: 7842
X-RateLimit-Reset: 1718467200
Retry-After: 45            (only on 429 responses)
```

### GET /admin/rate-limits

List all rate limit policies.

### PUT /admin/rate-limits/{policyId}

Update a policy. Changes take effect immediately for all consumers using this policy.

### DELETE /admin/rate-limits/{policyId}

Remove a policy. Fails (409 Conflict) if referenced by consumers or plugin instances.

---

## 6. Certificate / TLS APIs

Manage TLS certificates for HTTPS termination at the gateway.

### POST /admin/certificates

Upload a TLS certificate.

**Request:**

```json
{
  "cert": "-----BEGIN CERTIFICATE-----\nMIIE...\n-----END CERTIFICATE-----",
  "key": "-----BEGIN PRIVATE KEY-----\nMIIE...\n-----END PRIVATE KEY-----",
  "cert_chain": "-----BEGIN CERTIFICATE-----\nMIID...\n-----END CERTIFICATE-----",
  "snis": ["api.example.com", "*.example.com"]
}
```

| Field | Purpose |
|---|---|
| `cert` | Leaf certificate in PEM format. |
| `key` | Private key in PEM format. Stored encrypted at rest. Never returned in API responses. |
| `cert_chain` | Intermediate CA certificates for chain validation. |
| `snis` | Server Name Indication values. When TLS handshake SNI = `api.example.com`, use this cert. Enables multi-domain hosting on a single gateway. |

### PUT /admin/certificates/{certId}

Replace a certificate (e.g., during renewal). Gateway hot-swaps without dropping connections.

### DELETE /admin/certificates/{certId}

Remove a certificate. SNIs fall back to the default certificate.

### POST /admin/certificates/acme

Request automatic certificate provisioning via ACME (Let's Encrypt).

```json
{
  "domains": ["api.example.com"],
  "acme_provider": "letsencrypt",
  "challenge_type": "http-01",
  "auto_renew": true,
  "renew_before_days": 30
}
```

Certificate provisioning is asynchronous. Poll `poll_url` until status changes to `issued`.

---

## 7. Health & Status APIs

Data-plane APIs used by load balancers, monitoring, and operators.

### GET /health

Simple health check for load balancers. Must be fast (no DB queries on critical path).

**Response (200 OK):**

```json
{
  "status": "healthy"
}
```

**Response (503 Service Unavailable):**

```json
{
  "status": "unhealthy",
  "reason": "config database unreachable"
}
```

### GET /status

Detailed gateway status for dashboards and debugging.

```json
{
  "node_id": "gw-node-us-east-1a-01",
  "version": "2.8.1",
  "uptime_seconds": 1209600,
  "config_version": "cfg-v47",
  "connections": {
    "active": 12847,
    "idle": 3200,
    "total_accepted": 98271634
  },
  "requests": {
    "total": 98271634,
    "per_second_current": 4521,
    "status_codes": {
      "2xx": 94521000,
      "4xx": 2400000,
      "5xx": 150634
    },
    "latency_ms": {
      "p50": 12,
      "p95": 78,
      "p99": 230
    }
  },
  "routes_configured": 127,
  "services_configured": 23,
  "plugins_configured": 42
}
```

### GET /upstreams/{serviceId}/health

Health of all targets within a service.

```json
{
  "service_id": "svc-user-001",
  "overall_status": "degraded",
  "targets": [
    {
      "host": "10.0.1.10",
      "port": 8080,
      "health": "healthy",
      "active_check": { "last_status": 200, "consecutive_successes": 47 }
    },
    {
      "host": "10.0.1.12",
      "port": 8080,
      "health": "unhealthy",
      "active_check": { "last_status": 503, "consecutive_failures": 5 },
      "removed_from_pool_at": "2025-06-18T14:15:00Z"
    }
  ]
}
```

| Status | Meaning |
|---|---|
| `healthy` | All targets healthy |
| `degraded` | At least one target unhealthy, at least one still healthy |
| `unhealthy` | All targets unhealthy — requests will fail |

---

## 8. Configuration Sync APIs

For multi-node gateway clusters — propagate configuration changes across all nodes.

### POST /admin/config/sync

Force-trigger configuration sync.

```json
{
  "target_nodes": ["gw-node-us-east-1a-01"],
  "force": false
}
```

### GET /admin/config/version

Current config version on this node. Used to verify all nodes are on the same version.

```json
{
  "config_version": "cfg-v47",
  "config_hash": "sha256:a1b2c3d4e5f6...",
  "cluster_versions": [
    { "node_id": "gw-node-us-east-1a-01", "version": "cfg-v47", "synced": true },
    { "node_id": "gw-node-us-west-2a-01", "version": "cfg-v46", "synced": false }
  ]
}
```

A node with `synced: false` is running stale configuration.

### GET /admin/config/export

Export entire gateway configuration as JSON. Used for backup, disaster recovery, and environment migration.

### POST /admin/config/import

Import configuration. Supports `strategy: "merge"` (add/update only) and `strategy: "replace"` (make state match import exactly). Always use `dry_run: true` first in production.

**Dry-run response:**

```json
{
  "dry_run": true,
  "changes": {
    "services": { "create": 2, "update": 1, "delete": 0, "unchanged": 20 },
    "routes": { "create": 5, "update": 3, "delete": 1, "unchanged": 118 }
  }
}
```

---

## 9. Contrasts

### Self-Managed Gateway vs AWS API Gateway vs Service Mesh Sidecar

| Dimension | Self-Managed (Kong, APISIX) | AWS API Gateway | Service Mesh Sidecar (Envoy/Istio) |
|---|---|---|---|
| **Traffic direction** | North-south (external → internal) | North-south | East-west (service-to-service) |
| **Deployment** | You deploy, scale, operate | Fully managed by AWS | Sidecar injected per pod |
| **Customization** | Full control: custom plugins, any protocol | Limited: Lambda authorizers, VTL templates | Full control via Envoy filters, complex config |
| **Pricing** | Infra cost (compute + storage) | Pay per request ($3.50/million) + data transfer | Infra cost (sidecar CPU/memory per pod) |
| **Timeout limits** | Configurable (no hard limit) | 29-second hard timeout (REST APIs) | Configurable |
| **Protocol support** | HTTP, gRPC, TCP, WebSocket | HTTP, WebSocket, REST | HTTP, gRPC, TCP, any L4 |
| **Rate limiting** | Built-in, highly configurable | Usage plans + API keys (less flexible) | External rate limit service required |
| **Config management** | Admin API + database | CloudFormation / CDK / Console | Kubernetes CRDs |
| **Latency overhead** | 1-5ms per hop | 10-30ms per hop (multi-tenant) | <1ms per hop (localhost sidecar) |
| **When to use** | Full control, multi-cloud, custom plugins | AWS-native, zero ops, simple APIs | Internal service-to-service, zero-trust |

**Key design insight:** Most production systems use BOTH an API gateway (north-south) and a service mesh (east-west). They are complementary, not competing solutions.

**AWS API Gateway limitations to know:**
- 29-second hard timeout (not configurable)
- REST APIs and HTTP APIs are different products with different feature sets
- Lambda authorizer cold starts add 100-500ms latency
- 10,000 RPS default per account per region (soft limit, can be raised)
- No native gRPC support on REST APIs

---

## 10. Interview Subset

In a system design interview (Phase 3 — API Design), focus on three key API groups:

1. **Route configuration** — show you understand how clients define routing rules (path matching, host routing, weight-based splitting)
2. **Rate limiting configuration** — the most commonly asked feature; show multi-dimensional limits, response headers, and 429 semantics
3. **Health/status APIs** — demonstrate operational awareness; show you think about how operators monitor and debug the gateway

The full API list in this document provides depth for follow-up questions.
