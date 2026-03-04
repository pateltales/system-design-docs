# Plugin & Middleware Architecture — Deep Dive

---

## 1. Plugin Execution Model

Requests pass through an ordered chain of plugins (middleware). Each plugin can:
- **Inspect/modify** the request
- **Inspect/modify** the response
- **Short-circuit** the chain (return a response immediately, e.g., auth failure → 401)
- **Pass** to the next plugin

Similar to Express.js middleware or Java servlet filters.

```
Client Request
    │
    ▼
┌──────────────┐
│   Plugin 1   │ ──── Auth (JWT validation)
│  (priority   │      → On failure: 401 (short-circuit)
│   1000)      │      → On success: inject X-User-Id header, continue
└──────┬───────┘
       │
       ▼
┌──────────────┐
│   Plugin 2   │ ──── Rate Limiting
│  (priority   │      → On limit exceeded: 429 (short-circuit)
│   900)       │      → On pass: continue
└──────┬───────┘
       │
       ▼
┌──────────────┐
│   Plugin 3   │ ──── Request Transformer
│  (priority   │      → Strip path prefix, add headers
│   800)       │
└──────┬───────┘
       │
       ▼
  Forward to Upstream
       │
       ▼
  Response flows back through plugins in reverse
       │
       ▼
┌──────────────┐
│   Plugin 4   │ ──── Response Transformer
│  (priority   │      → Add CORS headers, strip internal headers
│   700)       │
└──────┬───────┘
       │
       ▼
┌──────────────┐
│   Plugin 5   │ ──── Logging / Metrics
│  (priority   │      → Write access log, export Prometheus metrics
│   100)       │
└──────┬───────┘
       │
       ▼
  Client Response
```

---

## 2. Plugin Phases

| Phase | When | Can Modify | Example Plugins |
|---|---|---|---|
| **Access** | Before proxying to upstream | Request headers, body, path. Can reject. | Auth, rate limiting, IP restriction, request validation |
| **Header Filter** | After receiving upstream response headers | Response headers | Add CORS headers, add security headers (HSTS, CSP) |
| **Body Filter** | As response body streams back | Response body (chunked) | Compression, encryption, PII scrubbing |
| **Log** | After response sent to client | Nothing (read-only) | Access logging, metrics export, analytics |

---

## 3. Common Plugin Types

### Authentication
JWT, API key, OAuth 2.0, mTLS, Basic auth, LDAP, SAML.

### Rate Limiting
Token bucket, sliding window, multi-dimensional. See [04-rate-limiting-and-throttling.md](./04-rate-limiting-and-throttling.md).

### Request Transformation
- Add/remove/rename headers
- Rewrite path (strip prefix, regex replace)
- Modify query parameters
- Transform body (JSON ↔ XML, add/remove fields)

### Response Transformation
- Modify response body
- Add security headers (CORS, CSP, HSTS)
- Strip internal headers (e.g., `X-Internal-Debug`)

### Logging / Observability
- Structured access logs (JSON)
- Request/response body capture (for debugging)
- Distributed tracing injection (`traceparent`, `X-B3-TraceId`)
- Prometheus metrics export

### Caching
- Cache GET responses with configurable TTL
- Cache key: URL + headers + query params
- Stale-while-revalidate: serve stale, refresh in background
- Purge API for manual invalidation

### CORS
- Handle preflight (`OPTIONS`) requests
- Set `Access-Control-Allow-Origin`, `Allow-Methods`, `Allow-Headers`

### IP Restriction
- Allow-list or deny-list of client IPs/CIDRs
- Geo-IP blocking

### Request Validation
- Validate request body against JSON Schema or OpenAPI spec
- Reject malformed requests before they hit the backend
- Reduces backend error handling burden

---

## 4. Custom Plugin Development

### Lua (Kong / OpenResty)

Runs inside the NGINX event loop. Low overhead, no IPC.

```lua
-- Kong custom plugin handler
local MyPlugin = {
  PRIORITY = 1000,
  VERSION = "1.0.0",
}

function MyPlugin:access(conf)
  local api_key = kong.request.get_header("X-Custom-Auth")
  if not api_key or api_key ~= conf.expected_key then
    return kong.response.exit(401, { message = "Unauthorized" })
  end
  kong.service.request.set_header("X-Validated", "true")
end

function MyPlugin:log(conf)
  kong.log.info("Request processed for route: ", kong.router.get_route().name)
end

return MyPlugin
```

**Pros:** Low-overhead, runs in-process.
**Cons:** Lua is a niche language, sandboxed environment limits blocking I/O, limited library access.

### Wasm (Envoy)

Compile plugins from C++, Rust, Go, or AssemblyScript to WebAssembly. Runs in a sandboxed VM inside Envoy.

**Pros:** Safe (can't crash the gateway), portable, polyglot (write in any language that compiles to Wasm).
**Cons:** Higher overhead than native code, debugging is harder, Wasm ecosystem for proxy plugins is still maturing.

### External Processing (Envoy ext_proc)

Gateway sends the request to an external gRPC service for processing. The service can modify headers, body, or reject.

```
Client → Envoy ──ext_proc──→ External gRPC Service
                                    │
                                    │ Modify headers?
                                    │ Modify body?
                                    │ Allow/deny?
                                    │
                    Envoy ←─────────┘
                      │
                      ▼
                   Upstream
```

**Pros:** Any language, fully decoupled, good for complex plugins (ML-based bot detection, PII scrubbing).
**Cons:** Adds network latency (~1-5ms per call), additional service to deploy/manage.

### Lambda Functions (AWS API Gateway)

Custom logic via Lambda authorizers (auth) or Lambda integrations (backend).

**Pros:** Any language, fully managed, no infrastructure.
**Cons:** Cold-start latency (~100-500ms), limited to AWS ecosystem, not a true plugin architecture (functions in the path, not middleware).

---

## 5. Plugin Configuration Storage

| Model | Gateway | How It Works | Change Propagation |
|---|---|---|---|
| **File-based** | NGINX | Config in `nginx.conf`. Requires `nginx -s reload`. | Seconds (worker drain) |
| **Database-backed** | Kong | Config in PostgreSQL/Cassandra. Polling for changes. | 1-5 seconds |
| **xDS / control plane** | Envoy/Istio | Config pushed via xDS gRPC. Fully dynamic. | Sub-second |
| **API + Console** | AWS API Gateway | CloudFormation / CDK / Console. | Seconds to minutes |

---

## 6. Gateway Contrasts

### Kong

- Built on OpenResty (NGINX + Lua)
- 100+ plugins available in the plugin hub
- Custom plugins in Lua or Go
- DB-backed config (PostgreSQL/Cassandra) or DB-less mode (declarative YAML)
- Plugin priority determines execution order (higher runs first)
- Plugins can be attached per-route, per-service, or globally

### Envoy

- Filter chain model (built-in filters + custom)
- Built-in filters: routing, auth, rate limiting, CORS, health checking
- Custom filters: C++ (compiled into Envoy), Wasm (sandboxed), ext_proc (gRPC call)
- xDS-driven configuration (no config files, no database)
- Filter ordering defined in the listener config

### AWS API Gateway

- Limited extensibility
- Custom logic via Lambda authorizers (auth) or Lambda integrations
- VTL (Velocity Template Language) for request/response transformation — powerful but arcane
- No plugin architecture — extend by putting Lambda functions in the path

### Comparison

| Feature | Kong | Envoy | AWS API Gateway |
|---|---|---|---|
| Plugin language | Lua, Go | C++, Wasm, ext_proc (any) | Lambda (any language) |
| Plugin sandboxing | Limited (Lua sandbox) | Strong (Wasm VM) | Full (Lambda isolation) |
| Configuration | Admin API + DB | xDS / control plane | CloudFormation / Console |
| Plugin hot-reload | Yes (DB sync) | Yes (xDS push) | Yes (deploy new Lambda) |
| Plugin marketplace | Kong Hub (100+) | N/A (filters are code) | AWS Marketplace (limited) |
| Custom plugin effort | Low (Lua is simple) | Medium (Wasm toolchain) | Medium (Lambda + IAM) |
| Latency overhead | Low (in-process Lua) | Low (in-process), Medium (ext_proc) | High (Lambda cold-start) |
