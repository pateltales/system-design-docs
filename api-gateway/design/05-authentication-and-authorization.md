# Authentication & Authorization — Deep Dive

Authentication (who are you?) and authorization (what can you do?) are the gateway's most critical security functions. By centralizing auth at the gateway, backend services are relieved of duplicating security logic.

---

## 1. Authentication Methods

### 1.1 API Key

Simplest form. Client sends a static key, gateway looks it up.

- **How:** Client sends `X-API-Key: abc123` header (or query param). Gateway hashes the key (SHA-256) and performs O(1) lookup in a key store.
- **Pros:** Simple, low latency.
- **Cons:** No expiration semantics (must be manually rotated), easily leaked in URLs/logs if sent as query param.
- **Use case:** Server-to-server calls, public APIs with usage tracking.

### 1.2 JWT (JSON Web Token)

Self-contained, stateless token. Gateway validates locally without calling any external service.

**Validation flow:**

```
Client                    Gateway                         IdP (JWKS)
  |                         |                                |
  |-- Bearer <JWT> -------->|                                |
  |                         |-- GET /.well-known/jwks.json ->| (cached)
  |                         |<-- Public keys ----------------|
  |                         |                                |
  |                         | 1. Decode header, find "kid"   |
  |                         | 2. Match kid to cached JWKS key|
  |                         | 3. Verify signature (RSA/ECDSA)|
  |                         | 4. Check exp, iss, aud claims  |
  |                         | 5. Extract user claims         |
  |                         |                                |
  |<-- 200 + X-User-Id ----|                                |
```

**Key properties:**
- **No DB call needed** — validation is purely cryptographic + clock check
- **Revocation is hard** — token is self-contained, can't be "deleted." Mitigations:
  - Short TTL (5-15 minutes) + refresh tokens
  - Token blacklist in Redis (trades away stateless benefit)
  - Token versioning — bump user-level version counter, reject tokens with old version

### 1.3 OAuth 2.0 (Resource Server Mode)

Gateway acts as OAuth 2.0 Resource Server, validating access tokens.

| Strategy | How | Latency | Freshness |
|---|---|---|---|
| **JWT validation** | Token is a signed JWT; validate locally | ~1 ms | Stale until expiry |
| **Token introspection** | Call `/introspect` endpoint on AuthZ server | ~5-50 ms | Real-time (supports revocation) |

**When to use which:**
- JWT validation: High-throughput APIs where millisecond latency matters and short TTLs are acceptable
- Token introspection: When immediate revocation is required (financial, healthcare)

### 1.4 mTLS (Mutual TLS)

Both client and server present X.509 certificates. Gateway verifies client certificate against a trusted CA.

- **Use cases:** Zero-trust networks, service-to-service (east-west), B2B integrations
- **Istio/service mesh:** Automatic mTLS between sidecars via SPIFFE identities
- **Pros:** Extremely strong authentication (cryptographic identity), no tokens to steal
- **Cons:** Certificate management is operationally complex (rotation, revocation, CA infrastructure)

### 1.5 Basic Auth

`Authorization: Basic base64(username:password)`. Simple but insecure (credentials with every request). Only over TLS. Only for internal tools / legacy systems.

### 1.6 HMAC Signature

Client signs the request (method + path + timestamp + body hash) with a secret key. Gateway recomputes and compares.

| Implementation | What Is Signed | Header |
|---|---|---|
| **AWS Signature V4** | Method, URI, query, headers, payload hash, timestamp | `Authorization: AWS4-HMAC-SHA256 ...` |
| **Stripe Webhooks** | Timestamp + raw request body | `Stripe-Signature: t=...,v1=...` |

**Pros:** Replay protection (timestamp), request integrity (body signed), secret never transmitted.
**Cons:** Complex for clients, clock synchronization required.

---

## 2. Authorization Models

### 2.1 RBAC (Role-Based Access Control)

Most common. Users assigned roles, roles have permissions.

```
User "alice" → Role "editor" → Permissions: [articles:read, articles:write]
User "bob"   → Role "viewer" → Permissions: [articles:read]

Route: POST /api/articles — Required role: "editor"
Gateway checks JWT claims → roles: ["editor"] → ALLOW
```

**Pros:** Simple, well-understood, easy to audit.
**Cons:** Role explosion in complex systems.

### 2.2 ABAC (Attribute-Based Access Control)

Decisions based on attributes of subject, resource, action, and environment.

```
ALLOW if:
  subject.department == "engineering"
  AND action == "read"
  AND resource.classification != "top-secret"
  AND environment.time.hour BETWEEN 9 AND 17
```

**Pros:** Fine-grained, context-aware.
**Cons:** Hard to audit, complex policy authoring.

### 2.3 OPA (Open Policy Agent)

Externalized policy engine. Policies in Rego, gateway queries OPA for allow/deny.

```
Client → Gateway → OPA Sidecar → Decision

POST /v1/data/authz/allow
{
  "input": {
    "method": "GET",
    "path": "/api/users",
    "user": "alice",
    "roles": ["admin"]
  }
}

Response: { "result": true }
```

**Rego policy example:**

```rego
package authz

default allow = false

allow {
    input.method == "GET"
    input.roles[_] == "viewer"
}

allow {
    input.method == "POST"
    input.roles[_] == "editor"
}

allow {
    input.roles[_] == "admin"
}
```

**Deployment:** Sidecar (~1ms), centralized service (~5-10ms), or embedded library.
**Pros:** Decoupled from code, auditable, testable, technology-agnostic.

### 2.4 Scope-Based (OAuth Scopes)

Scopes are coarse-grained permissions in the JWT `scope` claim.

```
JWT: { "scope": "read:articles write:articles read:users" }

Route: DELETE /api/articles/:id — Required: "write:articles"
Gateway: token.scope contains "write:articles" → ALLOW
```

---

## 3. Token Caching

JWT signature verification is CPU-intensive (RSA-2048: ~0.3ms, ECDSA P-256: ~0.1ms). At 100K+ RPS, this adds up.

```
Token Validation Cache:
  Key:      SHA-256(token)
  Value:    { valid: true, claims: {...}, exp: ... }
  Eviction: LRU + TTL (min of token exp, max 5 minutes)
  Size:     10,000 - 100,000 entries
```

| Scenario | Latency |
|---|---|
| No cache (RSA verify) | ~0.3 ms |
| Cached (hash lookup) | ~0.01 ms |
| Introspection (no cache) | ~10-50 ms |
| Introspection (cached) | ~0.01 ms |

**Rules:** Cache key is a hash of the token (never store raw token). TTL must not exceed token's `exp`. For introspection, use short TTL (30-60s) to balance freshness vs load.

---

## 4. Auth Flow in the Gateway

```
Client                     Gateway                         Upstream
  |                          |                                |
  |-- Authorization: Bearer →|                                |
  |                          | 1. Extract token from header   |
  |                          | 2. Check token cache           |
  |                          |    HIT → use cached claims     |
  |                          |    MISS → verify signature     |
  |                          | 3. Check exp, iss, aud         |
  |                          | 4. Run authorization (RBAC/OPA)|
  |                          | 5. Inject identity headers:    |
  |                          |    X-User-Id: user123          |
  |                          |    X-User-Roles: admin         |
  |                          |    X-Tenant-Id: acme           |
  |                          | 6. Strip Authorization header  |
  |                          |                                |
  |                          |-- Proxied request + headers --→|
  |                          |                                |
  |←-- 200 OK --------------|←-- 200 OK --------------------|
```

**Security requirement:** Upstream services must only accept `X-User-Id` / `X-User-Roles` headers from the gateway. Enforce by: network policy (upstream only reachable from gateway), or shared HMAC signature on injected headers.

---

## 5. Gateway Comparison

### Kong

| Plugin | Mechanism | Notes |
|---|---|---|
| `key-auth` | API key in header/query | Keys in Kong's database |
| `jwt` | JWT signature verification | RS256, ES256; configurable claims |
| `oauth2` | Full OAuth 2.0 AuthZ Server | Kong issues tokens (rare in practice) |
| `basic-auth` | Username + password | Credentials in Kong's database |
| `hmac-auth` | HMAC signature | Shared secret per consumer |
| `openid-connect` | OIDC (Enterprise) | Introspection + JWT validation |

Authorization via ACL plugin (group-based) or custom plugins (Lua/Go) or OPA (community plugin).

### AWS API Gateway

| Mechanism | How | Use Case |
|---|---|---|
| API Keys | `x-api-key` header | Usage plans, rate limiting |
| Lambda Authorizer | Gateway invokes Lambda with token; Lambda returns IAM policy | Custom auth logic (any provider) |
| Cognito Authorizer | Validates JWT from Cognito User Pool | AWS-native user management |
| IAM Auth | AWS Signature V4 | Service-to-service within AWS |

Lambda Authorizer adds cold-start latency (~100-500ms on first call). Cache results (TTL up to 1 hour) to mitigate.

### Envoy / Istio

| Feature | How |
|---|---|
| `jwt_authn` filter | Validates JWT signature (JWKS), checks claims |
| `ext_authz` filter | Calls external authorization service (gRPC/HTTP) — for OPA, custom auth |
| Istio `RequestAuthentication` | CRD configuring Envoy's `jwt_authn` per workload |
| Istio `AuthorizationPolicy` | CRD for allow/deny based on source, operation, conditions |

### Comparison

| Dimension | Kong | AWS API Gateway | Envoy / Istio |
|---|---|---|---|
| API Key | `key-auth` plugin | Native (`x-api-key`) | Custom via `ext_authz` |
| JWT validation | `jwt` plugin | Cognito Authorizer | `jwt_authn` filter |
| mTLS | Kong Mesh / config | Not supported (client-side) | Native (Istio auto-mTLS) |
| External policy (OPA) | Community plugin | Lambda Authorizer | `ext_authz` filter |
| Authorization model | ACL plugin / custom | IAM policy (ABAC) | `AuthorizationPolicy` (RBAC/ABAC) |
