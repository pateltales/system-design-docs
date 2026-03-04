# Request Routing Engine — Deep Dive

## Table of Contents
1. [Why Routing is the Core of an API Gateway](#1-why-routing-is-the-core-of-an-api-gateway)
2. [Request Matching Pipeline](#2-request-matching-pipeline)
3. [Trie-Based Path Matching](#3-trie-based-path-matching)
4. [Path Rewriting](#4-path-rewriting)
5. [Host-Based Routing](#5-host-based-routing)
6. [Header-Based Routing](#6-header-based-routing)
7. [Weight-Based Traffic Splitting](#7-weight-based-traffic-splitting)
8. [gRPC Routing](#8-grpc-routing)
9. [WebSocket Routing](#9-websocket-routing)
10. [Hot Reload of Routing Configuration](#10-hot-reload-of-routing-configuration)
11. [Comparison: NGINX vs Envoy vs Kong vs AWS API Gateway](#11-comparison-nginx-vs-envoy-vs-kong-vs-aws-api-gateway)

---

## 1. Why Routing is the Core of an API Gateway

Every other gateway feature — authentication, rate limiting, load balancing, observability — depends
on routing. Before the gateway can apply a rate limit policy or select a load balancing algorithm,
it must first answer a fundamental question: **which upstream service should this request go to?**

Routing is on the critical path of every single request. If routing adds 2ms of latency, every
API call across the entire platform pays that 2ms tax. A gateway handling 500K RPS at 2ms routing
overhead means **1,000 CPU-seconds per second** are spent just deciding where to send requests.
This is why routing data structures and algorithms matter deeply.

The routing engine is also the first component to evaluate a request, which means it must be
resilient to malformed input (malicious paths, oversized headers, encoding attacks) — it is the
gateway's first line of defense.

---

## 2. Request Matching Pipeline

### The Problem

An API gateway might have thousands of configured routes. When a request arrives, the gateway
must determine which route matches. The matching criteria are multi-dimensional — path, host,
method, headers, query parameters — and they have a priority order.

### Matching Criteria (Order of Specificity)

The gateway evaluates match conditions in descending order of specificity:

```
+---------------------------------------------------------------------+
|                     INCOMING HTTP REQUEST                            |
|  GET https://api.tenant-a.com/api/v2/users/123/orders?status=open   |
|  Headers: X-Api-Version: 2, Authorization: Bearer xxx               |
+--------------------------+------------------------------------------+
                           |
                           v
+--------------------------------------------------------------+
|  Step 1: HOST MATCH                                          |
|  -----------------                                           |
|  Match: api.tenant-a.com                                     |
|  Narrows candidate routes from 5,000 -> 200                  |
|  (Only tenant-a's routes remain)                             |
+--------------------------+-----------------------------------+
                           |
                           v
+--------------------------------------------------------------+
|  Step 2: PATH MATCH (most important, most complex)           |
|  -----------------                                           |
|  Priority: exact > prefix > regex                            |
|                                                              |
|  1. Exact:  /api/v2/users/123/orders  -> no exact match      |
|  2. Prefix: /api/v2/users/{id}/orders -> MATCH (prefix trie) |
|  3. Regex:  /api/v[0-9]+/.*          -> would match, but     |
|             prefix already matched (higher priority)          |
|                                                              |
|  Narrows candidates from 200 -> 3                            |
+--------------------------+-----------------------------------+
                           |
                           v
+--------------------------------------------------------------+
|  Step 3: HTTP METHOD MATCH                                   |
|  ------------------------                                    |
|  Route A: GET  /api/v2/users/{id}/orders -> service-orders   |
|  Route B: POST /api/v2/users/{id}/orders -> service-orders   |
|  Route C: ANY  /api/v2/users/{id}/orders -> service-fallback |
|                                                              |
|  Request is GET -> Route A matches (exact method > ANY)      |
|  Narrows candidates from 3 -> 1                             |
+--------------------------+-----------------------------------+
                           |
                           v
+--------------------------------------------------------------+
|  Step 4: HEADER CONDITIONS (optional refinement)             |
|  ---------------------                                       |
|  Route A has condition: X-Api-Version == "2"                 |
|  Request has header X-Api-Version: 2 -> condition satisfied  |
|                                                              |
|  If condition failed, would fall back to a route without     |
|  header conditions, or return 404.                           |
+--------------------------+-----------------------------------+
                           |
                           v
+--------------------------------------------------------------+
|  Step 5: QUERY PARAMETER CONDITIONS (optional refinement)    |
|  ------------------------------                              |
|  Some routes match only when specific query params exist.    |
|  Example: ?format=xml -> XML transformation route            |
|  Rarely used for primary routing; more common in plugins.    |
+--------------------------+-----------------------------------+
                           |
                           v
+--------------------------------------------------------------+
|  RESULT: Route A matched                                     |
|  Forward to: service-orders (upstream)                       |
|  Path params extracted: { id: "123" }                        |
|  Apply route-level plugins: rate-limit, auth, transform      |
+--------------------------------------------------------------+
```

### First Match Wins (with Priority Ordering)

Routes are evaluated by priority. When multiple routes could match, the most specific one wins.
The priority ordering is:

| Priority | Match Type | Example |
|----------|-----------|---------|
| 1 (highest) | Exact path + exact host + method | `GET api.example.com/api/v1/health` |
| 2 | Parameterized path + host | `GET api.example.com/api/v1/users/{id}` |
| 3 | Prefix path + host | `GET api.example.com/api/v1/*` |
| 4 | Regex path + host | `GET api.example.com/api/v[0-9]+/.*` |
| 5 | Exact path, any host | `GET */api/v1/health` |
| 6 | Prefix path, any host | `GET */api/*` |
| 7 (lowest) | Default / catch-all | `ANY */*` |

**Why this order?** More specific matches should always take precedence. If you have both
`/users/admin` (exact) and `/users/{id}` (parameterized), a request to `/users/admin` should
hit the exact route — not be treated as `id=admin`. This prevents accidental routing where
a reserved keyword like "admin" gets treated as a dynamic parameter.

### No Match Behavior

When no route matches, the gateway has three options:

1. **Return 404 Not Found** — safest default, explicit failure
2. **Forward to a default upstream** — useful for catch-all services
3. **Return a custom error page** — branded 404 for public-facing APIs

NGINX returns 404 by default. Envoy returns 404 with a `NR` (no route) flag in access logs.
Kong returns `{ "message": "no Route matched" }` with HTTP 404.

### Gateway Comparison: Route Matching

| Feature | NGINX | Envoy | Kong | AWS API Gateway |
|---------|-------|-------|------|-----------------|
| Match evaluation | Sequential in config order | First match by virtual host, then route priority | Priority field on route object | Longest prefix match |
| Path match types | exact, prefix, regex | exact, prefix, regex, path-separated prefix | exact, prefix, regex | exact, prefix, greedy |
| Host matching | `server_name` directive | Virtual host domains | Route `hosts` field | Custom domain mappings |
| Header matching | Via `if` (discouraged) or map | Native route header matchers | Native route header matchers | Not supported on routes |
| Method matching | Via `limit_except` | Native route method matcher | Route `methods` field | Resource + method combo |
| Query param matching | Via `if` or Lua | Query parameter matchers | Not native (plugin) | Not supported on routes |

---

## 3. Trie-Based Path Matching

### The Problem with Linear Scan

The naive approach to route matching is iterating through all configured routes and checking
each one against the incoming request path. For a gateway with 5,000 routes:

```
Linear scan: O(n) per request, where n = number of routes
5,000 routes x 500K RPS = 2.5 billion comparisons per second
```

Each comparison involves string matching (potentially regex), making this prohibitively expensive.
Even with compiled regexes, scanning thousands of patterns per request adds unacceptable latency.

### Radix Trie (Patricia Tree)

A radix trie compresses common prefixes into shared nodes, giving O(k) lookup where k = number
of path segments (not number of routes). Whether you have 100 routes or 100,000 routes, looking
up `/api/v1/users/123/orders` always traverses exactly 5 nodes.

### Trie Construction Example

Given these routes:
```
/api/v1/users                    -> user-service
/api/v1/users/{id}               -> user-service
/api/v1/users/{id}/orders        -> order-service
/api/v1/users/{id}/orders/{oid}  -> order-service
/api/v1/products                 -> product-service
/api/v1/products/{id}            -> product-service
/api/v2/users                    -> user-service-v2
/health                          -> health-service
```

The radix trie looks like:

```
root
|-- /api/
|   |-- v1/
|   |   |-- users ---------------------------> user-service (exact)
|   |   |   '-- /{id} -----------------------> user-service (param)
|   |   |       '-- /orders ------------------> order-service (exact)
|   |   |           '-- /{oid} ---------------> order-service (param)
|   |   '-- products ------------------------> product-service (exact)
|   |       '-- /{id} -----------------------> product-service (param)
|   '-- v2/
|       '-- users ---------------------------> user-service-v2 (exact)
'-- /health ---------------------------------> health-service (exact)
```

### Trie Traversal Example

Request: `GET /api/v1/users/42/orders`

```
Step 1: Split path -> ["api", "v1", "users", "42", "orders"]

Step 2: Traverse trie:
  root -> "api" (exact child match)
       -> "v1"  (exact child match)
       -> "users" (exact child match)
       -> "42"  (no exact child "42", but wildcard child {id} exists)
              -> Extract: params["id"] = "42"
       -> "orders" (exact child match)
              -> MATCH: order-service

Step 3: Return match result:
  { upstream: "order-service", params: { id: "42" }, path: "/api/v1/users/{id}/orders" }
```

Total nodes traversed: 5 (equal to path segment count), regardless of total route count.

### Wildcard Node Semantics

Wildcard nodes (path parameters) match any single path segment:

```
Node type       Matches               Priority
---------       -------               --------
Exact "users"   Only "users"          Highest
Param {id}      Any single segment    Medium
Glob  *         Remaining path        Lowest
```

**Precedence rule:** When both an exact child and a wildcard child exist, the exact child
is checked first. This ensures `/users/admin` matches the exact route before falling back
to `/users/{id}`.

```
Request: GET /api/v1/users/admin

Trie traversal:
  -> "api" -> "v1" -> "users" ->
    Check exact child "admin"?  YES -> route to admin-service
    (Would only check {id} if no exact child "admin" existed)
```

### Implementation Considerations

**Memory layout matters.** Each trie node should store:
- The path segment (or compressed prefix)
- A hashmap of exact children (for O(1) child lookup)
- A pointer to the wildcard child (at most one per node)
- A pointer to the glob child (at most one per node)
- The route handler (null for intermediate nodes)
- Attached metadata (plugins, rewrite rules, method filters)

```
struct TrieNode {
    segment:         String              // "users", "{id}", etc.
    exact_children:  HashMap<String, TrieNode>  // O(1) child lookup
    param_child:     Option<TrieNode>    // {id} wildcard
    glob_child:      Option<TrieNode>    // * catch-all
    handler:         Option<RouteHandler>  // null for intermediate nodes
    methods:         HashMap<Method, RouteHandler>  // per-method handlers
    priority:        u32                 // for ordering
}
```

**Thread safety.** The trie is read-heavy (every request reads it) and rarely written (route
config changes are infrequent). This is a textbook case for:
- **Read-write locks (RwLock):** Multiple readers, exclusive writers. Low write frequency
  means readers rarely block.
- **Copy-on-write:** Build a new trie, atomically swap the pointer. Zero contention on the
  read path. Envoy uses this approach.
- **Lock-free reads with epoch-based reclamation:** Most performant, but complex to implement
  correctly.

### Gateway Comparison: Path Matching Data Structure

| Gateway | Data Structure | Path Param Syntax | Regex Support |
|---------|---------------|-------------------|---------------|
| NGINX | Sorted list + hash (exact) + regex list | N/A (uses regex capture groups) | PCRE, evaluated in order |
| Envoy | Trie (route_config) | `{variable}` in path | RE2 (safe, no backtracking) |
| Kong | Radix tree (atc-router in Rust) | `{variable}` in path | PCRE via route `regex_priority` |
| AWS API Gateway | Prefix tree | `{proxy+}` greedy, `{param}` | Not supported |

**Why Envoy uses RE2 instead of PCRE:** PCRE supports backtracking, which means a maliciously
crafted regex or input string can cause exponential evaluation time (ReDoS attack). RE2 guarantees
linear-time evaluation by disallowing backtracking. For a security-critical component like an API
gateway, this is a critical safety property.

---

## 4. Path Rewriting

### The Problem

External clients access APIs at paths that include versioning, tenant prefixes, or gateway-specific
structure. Internal services should not need to know about these external conventions. Path
rewriting bridges the gap.

```
External (client-facing):     /api/v1/users/123
Internal (service-facing):    /users/123

Without rewriting: Every internal service must handle the /api/v1 prefix.
With rewriting:    The gateway strips it. Services remain clean and version-unaware.
```

### Rewrite Operations

#### 1. Strip Prefix

The most common operation. Remove a known prefix before forwarding.

```
Rule:    strip_prefix("/api/v1")
Request: GET /api/v1/users/123/orders
Result:  GET /users/123/orders

Rule:    strip_prefix("/tenant-a")
Request: GET /tenant-a/dashboard
Result:  GET /dashboard
```

**Use case:** API versioning. The external path includes `/v1`, `/v2`, etc. The internal
service just handles `/users`, `/orders`. The gateway determines which service version to
route to based on the prefix, then strips it.

#### 2. Add Prefix

Prepend a prefix before forwarding.

```
Rule:    add_prefix("/internal")
Request: GET /users/123
Result:  GET /internal/users/123
```

**Use case:** Internal services that serve multiple contexts. The service needs the prefix
to distinguish gateway traffic from direct internal traffic.

#### 3. Regex Replacement

Full regex-based path transformation.

```
Rule:    regex_replace("^/api/v([0-9]+)/(.*)", "/version-$1/$2")
Request: GET /api/v2/users/123
Result:  GET /version-2/users/123

Rule:    regex_replace("/users/([0-9]+)/profile", "/profiles?user_id=$1")
Request: GET /users/42/profile
Result:  GET /profiles?user_id=42
```

**Use case:** Complex path restructuring during API migrations. While migrating from a
monolith to microservices, the external URL structure may not match the new service's
URL structure.

### Path Rewriting Pitfalls

1. **Double-encoding:** If the gateway URL-decodes the path before rewriting and then
   re-encodes, special characters can be corrupted. `/users/hello%20world` might become
   `/users/hello+world` or `/users/hello%2520world`.

2. **Path traversal attacks:** A rewrite rule that naively strips a prefix could be
   exploited: `/api/v1/../../../etc/passwd` after stripping `/api/v1` becomes
   `/../../../etc/passwd`. The gateway must normalize paths (resolve `..`) before rewriting.

3. **Query string preservation:** Rewriting the path must not drop the query string.
   `GET /api/v1/users?page=2` rewritten to `/users` must become `/users?page=2`.

### Gateway Comparison: Path Rewriting

| Feature | NGINX | Envoy | Kong | AWS API Gateway |
|---------|-------|-------|------|-----------------|
| Strip prefix | `rewrite ^/api/v1(.*) $1 break;` | `prefix_rewrite: "/"` | Route `strip_path=true` | Stage variables |
| Add prefix | `rewrite` or `proxy_pass` with URI | `prefix_rewrite` | Request Transformer plugin | Not supported |
| Regex rewrite | `rewrite` with PCRE | `regex_rewrite` with RE2 | Route `regex_priority` + plugin | Not supported |
| Multiple rewrites | First matching `rewrite` | Single rewrite per route | Plugin chain | Not supported |

---

## 5. Host-Based Routing

### The Problem

A single API gateway deployment must serve multiple tenants, brands, or API products, each
with their own domain. Without host-based routing, you would need separate gateway deployments
per domain — wasting resources and complicating operations.

### How It Works

The gateway inspects the `Host` header (HTTP/1.1) or `:authority` pseudo-header (HTTP/2) and
uses it as the first routing dimension.

```
+----------------------------------+
|         API Gateway              |
|   (single deployment, 1 IP)     |
|                                  |
|  Host: api.tenant-a.com -------> Tenant A service cluster
|  Host: api.tenant-b.com -------> Tenant B service cluster
|  Host: api.public.com   -------> Public API service cluster
|  Host: admin.internal.io ------> Admin dashboard services
|  Host: *.preview.dev    -------> Preview environment services
+----------------------------------+
```

### Multi-Tenant Architecture

In a multi-tenant SaaS platform, host-based routing is the primary isolation mechanism at the
gateway layer:

```
Tenant A: api.tenant-a.example.com
  |-- GET /users     -> tenant-a-user-service (isolated instance)
  |-- GET /orders    -> tenant-a-order-service (isolated instance)
  '-- Rate limit: 10,000 req/min (Enterprise plan)

Tenant B: api.tenant-b.example.com
  |-- GET /users     -> shared-user-service (shared instance, tenant header injected)
  |-- GET /orders    -> shared-order-service (shared instance)
  '-- Rate limit: 1,000 req/min (Starter plan)
```

**Why host-first, not path-first?** Host-based routing provides the strongest isolation
guarantee. If Tenant A's route configuration has a bug (e.g., a regex that matches everything),
it only affects `api.tenant-a.com` — not Tenant B. Path-based multi-tenancy (`/tenant-a/users`,
`/tenant-b/users`) on a shared host offers no such isolation.

### Wildcard Host Matching

Gateways support wildcard patterns for host matching:

```
Exact:     api.example.com         -> matches only this exact host
Prefix:    *.example.com           -> matches foo.example.com, bar.example.com
Suffix:    api.*                   -> matches api.com, api.dev, api.internal
Regex:     ^[a-z]+-api\.prod\..*$  -> matches team1-api.prod.example.com
```

**Priority:** Exact host > prefix wildcard > suffix wildcard > regex > default.

### Combined Host + Path Routing

The real power emerges when host and path routing combine:

```
+---------------------------------------------------------------+
|  Host: api.example.com                                        |
|  |-- /v1/users/*        -> user-service-v1                    |
|  |-- /v2/users/*        -> user-service-v2                    |
|  '-- /v1/orders/*       -> order-service                      |
|                                                               |
|  Host: admin.example.com                                      |
|  |-- /dashboard         -> admin-dashboard-service            |
|  '-- /api/*             -> admin-api-service                  |
|                                                               |
|  Host: *.preview.dev                                          |
|  '-- /*                 -> preview-environment-router         |
|         (extracts branch name from subdomain,                 |
|          routes to the correct preview deployment)            |
+---------------------------------------------------------------+
```

### Gateway Comparison: Host-Based Routing

| Feature | NGINX | Envoy | Kong | AWS API Gateway |
|---------|-------|-------|------|-----------------|
| Configuration | `server_name` block | `virtual_hosts` in RDS | Route `hosts` array | Custom domain mappings |
| Wildcard support | `*.example.com` | `*.example.com` | `*.example.com` | No wildcard domains |
| Multiple hosts | Multiple `server_name` | Multiple domains per vhost | Multiple hosts per route | One domain per mapping |
| SNI support | `ssl_server_name` | `filter_chain_match` | Via certificate plugin | ACM certificate |
| Default host | `default_server` | Default virtual host | Route without `hosts` | Default stage |

---

## 6. Header-Based Routing

### The Problem

Sometimes the same URL path should route to different services based on non-URL context:
API version (without changing the path), A/B test group, client type, geographic region,
or feature flags. Header-based routing enables this without URL proliferation.

### Common Use Cases

#### A/B Testing

```
Route: GET /checkout
  Condition: X-Experiment-Group == "new-checkout"   -> checkout-service-v2
  Condition: X-Experiment-Group == "control"         -> checkout-service-v1
  Default (no header):                               -> checkout-service-v1
```

The experiment assignment happens at a higher layer (e.g., CDN edge, client SDK). The
gateway just routes based on the assigned group. This separation of concerns means the
routing layer does not need to know about experiment sampling logic.

#### API Versioning via Headers

```
Route: GET /users/{id}
  Condition: X-Api-Version == "2"     -> user-service-v2
  Condition: X-Api-Version == "1"     -> user-service-v1
  Condition: Accept: application/vnd.api.v2+json  -> user-service-v2
  Default (no version header):        -> user-service-v1 (backward compatible)
```

**Why header-based versioning instead of path-based (`/v1/users`, `/v2/users`)?**
- Path-based versioning duplicates the entire route table per version
- Header-based versioning keeps one route with conditional backends
- The URL remains stable — clients can upgrade by changing a header, not their URL
- Drawback: less visible, harder to test in a browser (can't just change the URL)

#### Canary Deployments by Internal Header

```
Route: GET /api/*
  Condition: X-Canary == "true"   -> canary-service-cluster
  Default:                         -> stable-service-cluster

# Internal load balancer or service mesh injects X-Canary: true
# for 5% of requests selected by consistent hashing on user ID
```

### Header Match Types

| Match Type | Example | Use Case |
|-----------|---------|----------|
| Exact | `X-Version: 2` | Version routing |
| Prefix | `Accept: application/vnd.myapi.*` | Content negotiation |
| Regex | `User-Agent: .*Mobile.*` | Mobile vs desktop routing |
| Present | `X-Debug` (any value) | Debug routing |
| Absent | No `Authorization` header | Route to login page |
| Range | `X-Priority: [1-5]` | Priority-based routing |

### Security Consideration

Header-based routing introduces a risk: **clients can inject routing headers.** If the
gateway routes based on `X-Canary: true`, a malicious client could send that header and
access canary infrastructure that isn't production-ready.

**Mitigations:**
1. **Strip untrusted headers at the edge.** Before routing, the gateway removes any headers
   that should only be set internally (e.g., `X-Canary`, `X-Internal-User-Id`).
2. **Use non-guessable header names.** Instead of `X-Canary`, use `X-Internal-Route-Override-7f3a`.
3. **Restrict header-based routing to internal traffic.** External-facing routes use path/host
   routing only. Header routing is limited to east-west (service-to-service) traffic.

### Gateway Comparison: Header-Based Routing

| Feature | NGINX | Envoy | Kong | AWS API Gateway |
|---------|-------|-------|------|-----------------|
| Native support | Via `map` + `if` (limited) | Native `headers` matcher in route | Native `headers` field on routes | Not supported on routes |
| Match types | Exact via `map` | Exact, prefix, suffix, regex, present, range | Exact, regex | N/A |
| Multiple conditions | Complex `if` chains | AND logic within route | AND logic within route | N/A |
| Performance | Adds overhead per `if` | Compiled into match tree | Evaluated in atc-router | N/A |

---

## 7. Weight-Based Traffic Splitting

### The Problem

Deploying a new version of a service is risky. Even with thorough testing, production traffic
has patterns that staging environments cannot replicate. Weight-based traffic splitting allows
gradual rollout: send 1% of traffic to the new version, monitor for errors, then ramp up.

### How It Works

```
Route: GET /api/v1/orders/*
  Upstream A (v1.2 -- stable):   weight = 95
  Upstream B (v1.3 -- canary):   weight = 5

For each request:
  1. Generate random number r in [0, 100)
  2. If r < 95: route to Upstream A
  3. If r >= 95: route to Upstream B
```

### Weighted Random Selection

The simplest implementation uses weighted random selection:

```
function selectUpstream(upstreams, weights):
    total = sum(weights)                    // 95 + 5 = 100
    r = random() * total                    // r in [0, 100)

    cumulative = 0
    for i in 0..len(upstreams):
        cumulative += weights[i]
        if r < cumulative:
            return upstreams[i]

    return upstreams[last]                  // fallback (rounding)
```

### Statistical Accuracy at Low Volume

At 1,000 RPS with a 5% canary weight, the canary receives ~50 requests per second — enough
for statistical significance. But at 10 RPS, the canary gets ~0.5 requests per second. Over
a 1-minute window, that is 30 requests — barely enough to detect a problem.

**Challenge:** With pure random selection, the actual distribution over short windows can
deviate significantly from the configured weights. At 10 requests, getting 0 or 3 canary
requests (instead of the expected 0.5) is common.

**Solutions:**

1. **Deterministic selection with modular arithmetic:**
   ```
   counter = atomic_increment()
   if counter % 20 == 0:    // exactly 1 in 20 = 5%
       route to canary
   else:
       route to stable
   ```
   Guarantees exact distribution but creates a predictable pattern.

2. **Consistent hashing with user ID:**
   ```
   hash = murmurhash(user_id)
   if hash % 100 < 5:
       route to canary
   else:
       route to stable
   ```
   Same user always goes to the same version (session stickiness for canary).
   Prevents a single user from experiencing version flapping between requests.

3. **Weighted round-robin with smooth distribution (Envoy's approach):**
   Envoy uses a smooth weighted round-robin that distributes canary requests evenly
   across time rather than clustering them. For weights [95, 5], instead of sending
   requests 1-95 to stable and 96-100 to canary, it intersperses canary requests:
   stable, stable, ..., stable, canary, stable, stable, ..., stable, canary, ...

### Canary Deployment Workflow

```
Step 1: Deploy canary (0% traffic)
+------------------------------+
|  100% --> v1.2 (stable)      |
|    0% --> v1.3 (canary)      |
+------------------------------+

Step 2: Route 1% to canary, monitor error rate
+------------------------------+
|   99% --> v1.2 (stable)      |
|    1% --> v1.3 (canary)      |
|                              |
|  Monitor: error_rate(v1.3)   |
|  < error_rate(v1.2) * 1.1?  |
|  Latency p99 acceptable?    |
+------------------------------+

Step 3: Ramp to 10%, then 50%, then 100%
+------------------------------+
|   50% --> v1.2 (stable)      |
|   50% --> v1.3 (canary)      |
+------------------------------+

Step 4: Promote canary to stable
+------------------------------+
|  100% --> v1.3 (now stable)  |
|    0% --> v1.2 (decomm)      |
+------------------------------+

Rollback: At any step, if metrics degrade, set canary weight to 0%
```

### Mirroring vs Splitting

Traffic **splitting** routes live requests — the canary handles real user traffic and
returns real responses. Traffic **mirroring** (shadowing) sends a copy of the request to
the canary but discards the response. The user always gets the response from stable.

```
Traffic Splitting (canary):
  Client --> Gateway --> canary-service --> Response to client
                                           (canary's response IS the user's response)

Traffic Mirroring (shadow):
  Client --> Gateway --> stable-service --> Response to client
                   '--> canary-service --> Response discarded
                        (fire-and-forget, no impact on user)
```

**When to use mirroring:** When the canary might have destructive side effects (writes to
a database) that you want to test in a production-like environment without affecting users.
The mirrored request hits a shadow database, not the production one.

### Gateway Comparison: Traffic Splitting

| Feature | NGINX | Envoy | Kong | AWS API Gateway |
|---------|-------|-------|------|-----------------|
| Weight-based split | `split_clients` or `upstream` with `weight` | `weighted_clusters` in route config | Canary plugin (community) | Canary release settings |
| Granularity | Integer weights | 0-100 per upstream | Percentage-based | Percentage-based |
| Session stickiness | Via `ip_hash` or `sticky` | Hash policy on route | Via hash-based balancing | Not supported |
| Traffic mirroring | `mirror` directive | `request_mirror_policies` | Not native (plugin) | Not supported |
| Automated rollback | Not built-in | Not built-in (use Flagger/Argo) | Not built-in | Auto-rollback on alarm |

---

## 8. gRPC Routing

### The Problem

Modern microservices architectures increasingly use gRPC for internal service-to-service
communication due to its performance advantages (binary serialization, HTTP/2 multiplexing,
streaming). The API gateway must route gRPC traffic alongside REST traffic, and often needs
to translate between the two protocols.

### How gRPC Routing Differs from REST

gRPC requests are HTTP/2 requests with specific conventions:

```
HTTP/2 Request:
  :method:       POST
  :path:         /com.example.UserService/GetUser
  :scheme:       https
  content-type:  application/grpc
  grpc-timeout:  5S

Body: Protocol Buffer encoded message
```

The `:path` pseudo-header encodes both the service name and method:
`/package.ServiceName/MethodName`

Unlike REST, where the HTTP method (GET, POST, PUT, DELETE) carries semantic meaning,
**all gRPC calls are POST**. The routing decision is based entirely on the path (service +
method) and the `content-type: application/grpc` header.

### gRPC Route Matching

```
Route table:
  /com.example.UserService/*          -> user-service:50051
  /com.example.OrderService/*         -> order-service:50052
  /com.example.UserService/GetUser    -> user-read-replica:50051  (specific method)
  /grpc.health.v1.Health/Check        -> any-service (health check)
```

The trie structure works identically for gRPC paths:

```
root
|-- /com.example.UserService/
|   |-- GetUser      -> user-read-replica:50051  (exact method match)
|   '-- *            -> user-service:50051       (catch-all for other methods)
|-- /com.example.OrderService/
|   '-- *            -> order-service:50052
'-- /grpc.health.v1.Health/
    '-- Check        -> any-service
```

### Protocol Translation: REST to gRPC

The gateway exposes a REST API externally but communicates with internal services via gRPC.
This is called **gRPC transcoding**.

```
External REST Client:
  GET /api/v1/users/42
  Accept: application/json

       |
       v

+------------------------------------------+
|  API Gateway -- Protocol Translation     |
|                                          |
|  1. Match REST route: GET /users/{id}    |
|  2. Map to gRPC: UserService.GetUser     |
|  3. Build protobuf request:              |
|     GetUserRequest { user_id: 42 }       |
|  4. Send gRPC request to upstream        |
|  5. Receive protobuf response            |
|  6. Transcode to JSON response           |
+------------------------------------------+

       |
       v

Internal gRPC Service:
  POST /com.example.UserService/GetUser
  content-type: application/grpc
  Body: <protobuf: GetUserRequest { user_id: 42 }>
```

**How the mapping is defined:**

Google's `google.api.http` annotation in the `.proto` file defines the REST-to-gRPC mapping:

```protobuf
service UserService {
  rpc GetUser(GetUserRequest) returns (User) {
    option (google.api.http) = {
      get: "/api/v1/users/{user_id}"
    };
  }

  rpc CreateUser(CreateUserRequest) returns (User) {
    option (google.api.http) = {
      post: "/api/v1/users"
      body: "*"
    };
  }
}
```

Envoy's `grpc_json_transcoder` filter reads the proto descriptor and automatically generates
REST-to-gRPC mappings. No manual route configuration needed.

### gRPC-Specific Challenges for the Gateway

1. **HTTP/2 requirement:** gRPC requires HTTP/2. If the gateway terminates TLS and re-establishes
   a connection to the upstream, it must use HTTP/2 (h2c or h2). Some gateways (older NGINX
   versions) only support HTTP/1.1 to upstreams, breaking gRPC.

2. **Streaming:** gRPC supports four streaming modes: unary, server-streaming, client-streaming,
   bidirectional. The gateway must not buffer the full stream before forwarding (that would
   defeat the purpose of streaming). It must proxy the HTTP/2 frames incrementally.

3. **Deadline propagation:** gRPC uses the `grpc-timeout` header to propagate deadlines. The
   gateway must subtract its own processing time and forward the reduced timeout to the
   upstream. If the gateway takes 3ms and the client set a 5-second timeout, the upstream
   should receive `grpc-timeout: 4997m` (4997 milliseconds).

4. **Error code mapping:** gRPC uses its own status codes (0=OK, 3=INVALID_ARGUMENT,
   5=NOT_FOUND, 13=INTERNAL, etc.) separate from HTTP status codes. The gateway's error
   handling must understand both systems when performing protocol translation.

### Gateway Comparison: gRPC Support

| Feature | NGINX | Envoy | Kong | AWS API Gateway |
|---------|-------|-------|------|-----------------|
| gRPC proxying | `grpc_pass` (since 1.13.10) | Native HTTP/2, full support | gRPC plugin | Not supported |
| gRPC-JSON transcoding | Not native (plugin) | `grpc_json_transcoder` filter | grpc-gateway plugin | Not supported |
| Streaming support | All 4 modes | All 4 modes | Unary + server-streaming | N/A |
| HTTP/2 to upstream | `grpc_pass` uses h2c | Native h2 and h2c | Via upstream protocol config | N/A |
| Load balancing gRPC | Per-connection (L4) | Per-request (L7, multiplexed) | Per-request | N/A |

**Critical difference:** NGINX's `grpc_pass` load balances at the connection level — once a
connection is established, all RPCs on that connection go to the same upstream. Envoy load
balances at the request level — each RPC on a multiplexed HTTP/2 connection can go to a
different upstream. This is critical for gRPC because a single HTTP/2 connection carries many
concurrent RPCs.

---

## 9. WebSocket Routing

### The Problem

WebSocket connections start as HTTP requests (with an `Upgrade` header) but then become
persistent, bidirectional, full-duplex TCP connections. The gateway must handle the initial
HTTP routing and then maintain the upgraded connection for its entire lifetime — which could
be minutes, hours, or even days.

### WebSocket Connection Lifecycle

```
+--------+         +-----------+         +--------------+
| Client |         |  Gateway  |         |   Upstream   |
+---+----+         +-----+-----+         +------+-------+
    |                    |                       |
    | GET /ws/chat       |                       |
    | Upgrade: websocket |                       |
    | Connection: Upgrade|                       |
    | Sec-WebSocket-Key  |                       |
    |------------------->|                       |
    |                    |                       |
    |                    | Route: /ws/chat ->     |
    |                    |   chat-service         |
    |                    |                       |
    |                    | GET /ws/chat           |
    |                    | Upgrade: websocket     |
    |                    |---------------------->|
    |                    |                       |
    |                    | 101 Switching Protocols|
    |                    |<----------------------|
    |                    |                       |
    | 101 Switching      |                       |
    | Protocols          |                       |
    |<-------------------|                       |
    |                    |                       |
    | <=== WebSocket frames (bidirectional) ===> |
    |      Gateway proxies frames transparently  |
    |                    |                       |
    | ... minutes/hours later ...                |
    |                    |                       |
    | Close frame        |                       |
    |------------------->|---------------------->|
    |                    | Close frame            |
    |<-------------------|<----------------------|
    +--------------------+---------- ------------+
```

### Routing the Initial Handshake

The WebSocket upgrade request is a standard HTTP request — it goes through the same routing
pipeline as any other request. The gateway matches on path, host, and headers just like REST.

```
Route configuration:
  Path: /ws/chat           -> chat-service:8080
  Path: /ws/notifications  -> notification-service:8081
  Path: /ws/live-data      -> streaming-service:8082

  Condition: Upgrade == "websocket"
  (Only match WebSocket routes for upgrade requests)
```

### Resource Pinning Challenge

A normal HTTP request occupies gateway resources for milliseconds. A WebSocket connection
occupies resources for the entire connection lifetime:

```
HTTP request lifecycle:
  |-- Receive request      ~0.1ms
  |-- Route + forward      ~1ms
  |-- Wait for upstream    ~50ms
  |-- Send response        ~0.1ms
  '-- Connection released  Total: ~51ms

WebSocket connection lifecycle:
  |-- Upgrade handshake    ~5ms
  |-- Proxy frames         ~minutes to hours
  |   |-- Gateway holds 2 file descriptors (client + upstream)
  |   |-- Gateway holds memory for frame buffers
  |   '-- Gateway holds state for the connection
  '-- Close                Total: minutes to hours
```

**Impact on capacity planning:**

| Metric | HTTP-only Gateway | Gateway with WebSockets |
|--------|-------------------|------------------------|
| Connections per node | ~50K concurrent (short-lived, high turnover) | ~10K concurrent (long-lived, low turnover) |
| Memory per connection | ~8 KB (request buffer) | ~16 KB (bidirectional buffers) |
| File descriptors | High churn, FDs recycled | Pinned for duration |
| Scale-out trigger | RPS-based | Connection-count-based |

### WebSocket-Specific Gateway Concerns

1. **Idle timeout:** Long-lived connections with no traffic should be reaped. But WebSocket
   connections may legitimately be idle (waiting for events). The gateway must support
   per-route idle timeouts and WebSocket ping/pong frames to distinguish idle from dead.

2. **Connection draining:** When the gateway restarts or reconfigures, it must drain
   existing WebSocket connections gracefully. Abruptly closing thousands of WebSocket
   connections causes a reconnection storm.
   ```
   Draining process:
   1. Stop accepting new WebSocket connections on old worker
   2. Continue proxying existing connections
   3. Wait up to drain_timeout (e.g., 30 seconds)
   4. Force-close remaining connections
   5. Shutdown old worker
   ```

3. **Load balancing:** Once a WebSocket connection is established, it is pinned to a specific
   upstream instance. The gateway cannot re-route frames to a different upstream mid-connection.
   If the upstream instance becomes unhealthy, the connection must be closed and re-established.

4. **Authentication:** The initial HTTP handshake is the only opportunity to authenticate.
   Once upgraded, there is no standard way to re-validate credentials. Long-lived connections
   may outlive token expiration. Solutions:
   - Check token expiration in custom WebSocket frames
   - Close connection on token expiry, force re-connect
   - Use a separate auth channel alongside the WebSocket

### Gateway Comparison: WebSocket Support

| Feature | NGINX | Envoy | Kong | AWS API Gateway |
|---------|-------|-------|------|-----------------|
| WebSocket proxying | `proxy_set_header Upgrade` | Native, auto-detected | Native support | WebSocket APIs |
| Idle timeout | `proxy_read_timeout` | `idle_timeout` per route | Plugin configurable | 10 min (hard limit) |
| Connection draining | Worker shutdown drains | Graceful drain with deadline | Worker drain | Managed (transparent) |
| Max connection time | Configurable | `max_connection_duration` | Configurable | 2 hours (hard limit) |
| Per-message inspection | Via Lua or stream module | Via custom filter | Via plugin | Not supported |

---

## 10. Hot Reload of Routing Configuration

### The Problem

In a dynamic microservices environment, route configuration changes frequently:
- New services are deployed (new routes added)
- Services are decommissioned (routes removed)
- Canary weights are adjusted
- A/B test configurations change
- Emergency routing changes during incidents

The gateway must apply these changes **without restarting** and **without dropping active
connections**. A gateway handling 500K RPS that restarts to apply a config change drops
thousands of in-flight requests — unacceptable for a 99.99% availability target.

### Approach 1: NGINX — Signal-Based Reload

NGINX uses a process-based model. Route configuration lives in static config files.

```
Reload process:

1. Operator modifies /etc/nginx/nginx.conf
2. Operator runs: nginx -s reload (sends SIGHUP to master)

+--------------------------------------------------------+
|  Master Process (PID 1)                                |
|                                                        |
|  Receives SIGHUP:                                      |
|  1. Parse new config file                              |
|  2. If valid: spawn new worker processes               |
|  3. Signal old workers to stop accepting new conns     |
|  4. Old workers finish processing in-flight requests   |
|  5. Old workers exit when all connections drain         |
|                                                        |
|  Timeline:                                             |
|  t=0:  Old workers handling all traffic                |
|  t=1:  New workers spawned, start accepting new conns  |
|  t=2:  Old workers in drain mode (no new conns)        |
|  t=5:  Old workers finish remaining requests, exit     |
|                                                        |
|  Result: Zero dropped connections                      |
+--------------------------------------------------------+
```

**Advantages:**
- Battle-tested, simple mental model
- Zero downtime for the reload itself
- New config is fully validated before any workers change

**Disadvantages:**
- Requires file system access (can't update from API)
- Spawn + drain cycle takes seconds — too slow for frequent changes
- Thousands of long-lived connections (WebSocket, gRPC streams) may take minutes to drain
- Not suitable for environments where routes change every few seconds (Kubernetes pod scaling)

### Approach 2: Envoy — xDS Dynamic Configuration

Envoy was designed from the ground up for dynamic configuration. It uses the **xDS protocol**
(a family of discovery service APIs) to receive configuration updates from a control plane
without any reload or restart.

```
xDS Protocol Family:
  LDS — Listener Discovery Service (ports, protocols)
  RDS — Route Discovery Service (routing rules)      <-- routing config
  CDS — Cluster Discovery Service (upstream clusters)
  EDS — Endpoint Discovery Service (individual endpoints)
  SDS — Secret Discovery Service (TLS certificates)
```

```
+--------------+          gRPC stream          +--------------------+
|   Envoy      |<---------------------------->|   Control Plane    |
|   (data      |   RDS: push route updates     |   (Istio Pilot,    |
|    plane)    |   CDS: push cluster updates   |    custom xDS      |
|              |   EDS: push endpoint updates  |    server)         |
|  +--------+  |                               |                    |
|  | Routes |  |   On update:                  |  Route config      |
|  | (in    |  |   1. Receive new RouteConfig  |  stored in:        |
|  | memory)|  |   2. Validate                 |  - Kubernetes CRDs |
|  |        |  |   3. Atomic swap in memory    |  - Consul KV       |
|  +--------+  |   4. No restart, no drain     |  - Custom DB       |
|              |   5. Immediate effect          |                    |
+--------------+                               +--------------------+
```

**How the atomic swap works:**

Envoy uses a **copy-on-write** approach for route configuration:
1. Current route table: pointer to immutable RouteConfig A
2. New config arrives: build immutable RouteConfig B in memory
3. Atomically swap the pointer: RouteConfig A -> RouteConfig B
4. In-flight requests that already resolved their route continue with the old route
5. New requests use the new route table
6. Once all references to RouteConfig A are released, it is garbage collected

```
Time ->
  t=0: All requests use RouteConfig A
  t=1: New config arrives, RouteConfig B built
  t=2: Pointer swapped to RouteConfig B
       In-flight request R1 (started at t=0): still uses RouteConfig A (held reference)
       New request R2: uses RouteConfig B
  t=3: R1 completes, releases reference to RouteConfig A
       RouteConfig A is freed
```

**No worker restart, no connection drain, no downtime.** This is why Envoy is preferred in
Kubernetes environments where endpoints change every few seconds as pods scale up and down.

**Advantages:**
- Sub-second propagation (gRPC streaming, push-based)
- No connection disruption
- Fine-grained updates (change one route without touching others)
- Control plane can be centralized (single source of truth for all Envoy instances)

**Disadvantages:**
- Requires a control plane (additional infrastructure to build/operate)
- xDS protocol is complex (versioning, ACK/NACK, incremental vs state-of-the-world)
- Debugging is harder (config is not in a file you can inspect; must query Envoy's admin API)

### Approach 3: Kong — Database-Backed Configuration

Kong stores all routing configuration in a database (PostgreSQL or Cassandra) and uses a
polling/event mechanism to detect changes.

```
+-------------+     +-------------+     +-------------+
|   Kong      |     |   Kong      |     |   Kong      |
|   Node 1    |     |   Node 2    |     |   Node 3    |
|             |     |             |     |             |
| +----------+|     | +----------+|     | +----------+|
| |  Route   ||     | |  Route   ||     | |  Route   ||
| |  Cache   ||     | |  Cache   ||     | |  Cache   ||
| +----+-----+|     | +----+-----+|     | +----+-----+|
|      | poll  |     |      | poll  |     |      | poll  |
+------+-------+     +------+-------+     +------+-------+
       |                    |                    |
       v                    v                    v
+----------------------------------------------------------+
|                    PostgreSQL / Cassandra                 |
|                                                          |
|  routes table:                                           |
|  +-----+--------------+-----------------+-------+       |
|  | id  | path         | upstream        | updated|       |
|  +-----+--------------+-----------------+-------+       |
|  | 1   | /api/users   | user-service    | 12:01 |       |
|  | 2   | /api/orders  | order-service   | 12:05 |       |
|  +-----+--------------+-----------------+-------+       |
+----------------------------------------------------------+

Update flow:
  1. Admin API call: PUT /routes/1 (update a route)
  2. Kong writes to PostgreSQL
  3. Kong triggers invalidation event (via DB or cluster event)
  4. Other Kong nodes poll or receive event
  5. Each node rebuilds its in-memory route cache
  6. Propagation delay: 0-5 seconds (depends on polling interval)
```

**Kong DB-less mode:** Kong 1.1+ supports a declarative, DB-less mode where configuration
is loaded from a YAML file and stored entirely in memory. Changes require re-loading the
declarative config via the Admin API. This is closer to NGINX's model but with an API
instead of file-based config.

**Advantages:**
- Familiar model (REST API for config changes)
- Configuration is auditable (database has full history)
- Admin API + GUI (Kong Manager) for operational ease

**Disadvantages:**
- Polling-based propagation is slower than push-based (seconds, not milliseconds)
- Database is a SPOF (if PostgreSQL goes down, config changes cannot propagate)
- Database adds latency to the control plane (not the data plane — routes are cached in memory)

### Comparison: Hot Reload Approaches

| Dimension | NGINX (SIGHUP) | Envoy (xDS) | Kong (DB-backed) |
|-----------|----------------|-------------|------------------|
| Propagation speed | Seconds (worker spawn) | Sub-second (gRPC push) | 1-5 seconds (polling) |
| Connection impact | Old connections drain | Zero impact | Zero impact (cache swap) |
| Config source | File on disk | gRPC control plane | PostgreSQL / Cassandra |
| Validation | At parse time | At receipt (NACK on error) | At write time (Admin API) |
| Partial update | No (full config reload) | Yes (per-route, per-cluster) | Yes (per-resource) |
| Debugging | Read the config file | Query `/config_dump` API | Query Admin API or DB |
| Suitable for K8s | Marginal (too slow) | Excellent (designed for it) | Good (with polling tuned) |
| Operational complexity | Low | Medium-High (control plane) | Medium (database ops) |

---

## 11. Comparison: NGINX vs Envoy vs Kong vs AWS API Gateway

### Full Feature Comparison

| Feature | NGINX | Envoy | Kong | AWS API Gateway |
|---------|-------|-------|------|-----------------|
| **Architecture** | Process-based (master + workers) | Thread-based (single process) | Lua on NGINX (OpenResty) | Fully managed (serverless) |
| **Config model** | Static file + reload | Dynamic xDS APIs | DB-backed + Admin REST API | CloudFormation / Console |
| **Path matching** | Sorted list + regex | Trie-based, RE2 regex | Radix tree (atc-router, Rust) | Prefix tree |
| **Path rewriting** | `rewrite` directive (PCRE) | `prefix_rewrite`, `regex_rewrite` (RE2) | Route config + plugins | Stage variables only |
| **Host routing** | `server_name` | Virtual hosts | Route `hosts` field | Custom domain mappings |
| **Header routing** | `map` + `if` (limited) | Native header matchers | Native header matchers | Not supported |
| **Weight-based split** | `split_clients`, `upstream weight` | `weighted_clusters` | Canary plugin | Canary deployments |
| **gRPC support** | `grpc_pass` (since 1.13) | Native, full HTTP/2 | gRPC plugin | Not supported |
| **gRPC transcoding** | Not native | `grpc_json_transcoder` | grpc-gateway plugin | Not supported |
| **WebSocket** | `proxy_set_header Upgrade` | Native auto-detection | Native | WebSocket APIs (2hr limit) |
| **Hot reload** | SIGHUP (worker drain) | xDS (zero-downtime swap) | DB poll + cache invalidation | Managed (transparent) |
| **gRPC LB granularity** | Per-connection (L4) | Per-request (L7) | Per-request | N/A |
| **Regex engine** | PCRE (backtracking, ReDoS risk) | RE2 (linear time, safe) | PCRE (via OpenResty) | N/A |
| **Extensibility** | C modules, Lua (OpenResty) | C++ filters, Lua, WASM | Lua plugins, Go plugins | Lambda authorizers/transforms |
| **Best for** | Static sites, simple proxying | Service mesh, K8s, dynamic envs | API management, developer portal | Serverless, quick setup |

### When to Use Each

**NGINX:** You have a stable set of routes that change infrequently (weekly or monthly). You
need raw performance for static content serving alongside API proxying. Your team is comfortable
with config-file-based operations. You don't need advanced header-based routing or dynamic
reconfiguration.

**Envoy:** You are in a Kubernetes/microservices environment where endpoints change every few
seconds. You need per-request gRPC load balancing. You want a service mesh sidecar that also
serves as the edge gateway. You need dynamic configuration without restarts. You are willing
to invest in building or deploying a control plane (Istio, custom xDS server).

**Kong:** You need an API management platform, not just a proxy. You want a developer portal,
API key management, rate limiting policies, and analytics out of the box. You need a REST API
and GUI for managing routes. Your team prefers operational simplicity over raw performance.

**AWS API Gateway:** You are all-in on AWS serverless (Lambda, DynamoDB). You want zero
infrastructure management. Your API traffic is moderate (not millions of RPS). You don't need
gRPC, WebSocket beyond 2 hours, or advanced routing. You accept vendor lock-in for operational
simplicity.

### Decision Matrix

```
Need dynamic config + K8s?                    -> Envoy
Need API management + developer portal?       -> Kong
Need raw performance + simple static config?  -> NGINX
Need serverless + zero ops on AWS?            -> AWS API Gateway
Need gRPC transcoding?                        -> Envoy
Need WebSocket > 2 hours?                     -> NGINX or Envoy (not AWS)
Need per-request gRPC load balancing?         -> Envoy (not NGINX)
Budget for control plane infrastructure?
  Yes -> Envoy
  No  -> Kong or NGINX
```

---

## Summary

The request routing engine is the heart of an API gateway. Every request must pass through it,
making its performance and correctness critical to the entire platform.

**Key takeaways:**

1. **Matching pipeline is hierarchical:** Host -> Path -> Method -> Headers -> Query params.
   More specific matches always win.

2. **Trie-based path matching eliminates the O(n) problem:** With thousands of routes, a radix
   trie provides O(k) lookup (k = path segments) regardless of route count.

3. **Path rewriting decouples external and internal URL structures:** Services remain unaware
   of API versioning and tenant prefixes.

4. **Weight-based splitting enables safe deployments:** Canary rollouts with 1-5% traffic catch
   production issues before full rollout.

5. **gRPC routing requires L7 awareness:** Per-request load balancing (not per-connection) is
   essential for gRPC's multiplexed HTTP/2 connections.

6. **WebSocket connections pin resources:** Capacity planning must account for long-lived
   connections consuming file descriptors and memory for hours.

7. **Hot reload is the dividing line between gateways:** NGINX's process-based reload works
   for infrequent changes. Envoy's xDS enables sub-second updates without any connection
   disruption. Kong's database-backed model falls in between.

8. **Choose your gateway based on your operational model:** Dynamic Kubernetes environments
   favor Envoy. API management platforms favor Kong. Simple, stable configurations favor NGINX.
   Serverless architectures favor AWS API Gateway.
